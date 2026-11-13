"""登记台：发布登记、快照打包、复用、校验与回滚时间线。"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .errors import (
    ReleaseExistsConflict,
    ReleaseNotFound,
    SnapshotConflict,
    StoreCorrupted,
)
from .fsutil import (
    SnapshotConflictWrite,
    atomic_write_bytes,
    hardlink_or_copy,
    copy_verified,
    sha256_file,
)
from .manifest import Manifest, diff_manifests, summarize_diff
from .model import DiffKind, EventType, ReleaseStatus

STORE_VERSION = 1


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _canonical(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass
class RollbackPlan:
    """回滚预览结果——纯数据模拟，不触碰任何文件。"""

    target_release_id: str
    head_release_id: str | None
    new_rollback_release_id: str
    stale_release_ids: list[str]          # 将被标记为 ROLLED_BACK 的下游发布
    stale_consumers: list[dict]           # 会变旧的下游引用
    diffs: list[dict]                     # head -> target 的文件级差异
    diff_summary: dict[str, int]
    files_affected: int
    note: str = "预览仅模拟状态变化，不删除任何产物文件"


@dataclass
class PublishResult:
    release: dict
    manifest: Manifest
    reused: bool = False
    reused_from: str | None = None


class Registry:
    """本地文件系统上的数据集发布登记台。"""

    def __init__(self, store: Path):
        self.store = Path(store).resolve()
        self.objects_dir = self.store / "objects"
        self.releases_dir = self.store / "releases"
        self.manifests_dir = self.store / "manifests"
        self.events_path = self.store / "events.log"
        self.head_path = self.store / "head.json"
        self.consumers_path = self.store / "consumers.json"
        self._event_seq = 0

    # ---------- 生命周期 ----------

    @classmethod
    def create(cls, store: Path | str) -> "Registry":
        store = Path(store).resolve()
        if (store / "config.json").exists():
            raise FileExistsError(f"登记台已存在：{store}")
        for sub in ("objects", "releases", "manifests"):
            (store / sub).mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(
            store / "config.json",
            json.dumps(
                {"store_version": STORE_VERSION, "created_at": _utc_now_iso()},
                ensure_ascii=False, indent=2,
            ).encode("utf-8"),
        )
        atomic_write_bytes(store / "events.log", b"")
        atomic_write_bytes(
            store / "head.json",
            json.dumps({"release_id": None}, ensure_ascii=False).encode("utf-8"),
        )
        atomic_write_bytes(store / "consumers.json", b"[]")
        reg = cls(store)
        reg._append_event(
            EventType.PUBLISH_REQUESTED,
            {"message": "登记台初始化", "store": str(store)},
            internal=True,
        )
        return reg

    @classmethod
    def open(cls, store: Path | str) -> "Registry":
        store = Path(store).resolve()
        if not (store / "config.json").exists():
            raise FileNotFoundError(f"登记台不存在：{store}")
        reg = cls(store)
        reg._reload_event_seq()
        return reg

    # ---------- 事件日志（append-only） ----------

    def _reload_event_seq(self) -> None:
        seq = 0
        if self.events_path.exists():
            with open(self.events_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        seq = max(seq, json.loads(line)["seq"])
        self._event_seq = seq

    def _append_event(self, event_type: str, payload: dict, internal: bool = False) -> dict:
        self._reload_event_seq()
        self._event_seq += 1
        event = {
            "seq": self._event_seq,
            "ts": _utc_now_iso(),
            "type": event_type,
            "payload": payload,
        }
        with open(self.events_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        return event

    def read_events(self) -> list[dict]:
        if not self.events_path.exists():
            return []
        with open(self.events_path, "r", encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]

    # ---------- 发布号与基础读写 ----------

    def _next_release_id(self) -> str:
        max_n = 0
        if self.releases_dir.exists():
            for p in self.releases_dir.glob("R*.json"):
                try:
                    max_n = max(max_n, int(p.stem[1:]))
                except ValueError:
                    continue
        return f"R{max_n + 1:06d}"

    def _object_path(self, digest: str) -> Path:
        return self.objects_dir / digest[:2] / digest

    def _write_json_if_absent(self, path: Path, data: dict) -> None:
        """写一次语义：目标已存在即拒绝，保证发布号内容不可覆盖。"""
        if path.exists():
            raise ReleaseExistsConflict(
                f"{path.name} 已存在且内容不同：同一发布号禁止覆盖"
            )
        atomic_write_bytes(
            path, json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        )

    def get_release(self, release_id: str) -> dict:
        path = self.releases_dir / f"{release_id}.json"
        if not path.exists():
            raise ReleaseNotFound(f"发布号不存在：{release_id}")
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _load_manifest(self, release: dict) -> Manifest:
        with open(self.store / release["manifest_path"], "r", encoding="utf-8") as f:
            return Manifest.from_dict(json.load(f))

    def head(self) -> str | None:
        with open(self.head_path, "r", encoding="utf-8") as f:
            return json.load(f)["release_id"]

    def _set_head(self, release_id: str) -> None:
        atomic_write_bytes(
            self.head_path,
            json.dumps({"release_id": release_id}, ensure_ascii=False).encode("utf-8"),
        )

    # ---------- 期望指纹核对 ----------

    @staticmethod
    def _check_expected(manifest: Manifest, expected: list[dict] | None) -> list[dict]:
        """缺失文件与指纹不一致 -> 阻塞性差异；清单中多出的文件 -> 警告。"""
        discrepancies: list[dict] = []
        by_path = manifest.by_path()
        seen: set[str] = set()
        for item in expected or []:
            rel = item["rel_path"]
            seen.add(rel)
            entry = by_path.get(rel)
            if entry is None:
                discrepancies.append(
                    {"kind": "MISSING_FILE", "rel_path": rel,
                     "expected_sha256": item.get("sha256"), "actual": None}
                )
                continue
            want = item.get("sha256")
            if want and want != entry.sha256:
                discrepancies.append(
                    {"kind": "FINGERPRINT_MISMATCH", "rel_path": rel,
                     "expected_sha256": want, "actual_sha256": entry.sha256}
                )
        for rel in sorted(set(by_path) - seen):
            discrepancies.append(
                {"kind": "UNEXPECTED_FILE", "rel_path": rel,
                 "actual_sha256": by_path[rel].sha256, "blocking": False}
            )
        return discrepancies

    # ---------- 发布 ----------

    def publish(
        self,
        source_root: Path | str,
        transform_version: str,
        partition_range: dict | None = None,
        expected: list[dict] | None = None,
        mutate_hook=None,
    ) -> PublishResult:
        """登记一次发布。

        流程：扫描清单① -> 期望核对（缺失/不一致即 PENDING_CHECK）
        -> 内容寻址打包（逐文件校验）-> 扫描清单②（快照漂移即终止）
        -> 物化产物 -> 写一次登记 -> 推进 head。
        mutate_hook(manifest) 在清单①之后被调用，仅用于演示打包竞态。
        """
        source_root = Path(source_root).resolve()
        if not source_root.is_dir():
            raise FileNotFoundError(f"源目录不存在：{source_root}")
        partition_range = partition_range or {}
        requested_id = self._next_release_id()
        self._append_event(
            EventType.PUBLISH_REQUESTED,
            {"release_id": requested_id, "source_root": str(source_root),
             "transform_version": transform_version,
             "partition_range": partition_range},
        )

        # ① 第一次扫描
        manifest1 = Manifest.scan(source_root)
        if mutate_hook is not None:
            mutate_hook(manifest1)

        # 期望指纹核对：缺失 / 不一致 -> 停在待检查
        discrepancies = self._check_expected(manifest1, expected)
        blocking = [d for d in discrepancies if d.get("blocking", True)]
        if blocking:
            manifest_path = self.manifests_dir / f"{requested_id}.json"
            atomic_write_bytes(
                manifest_path,
                json.dumps(manifest1.to_dict(), ensure_ascii=False, indent=2).encode("utf-8"),
            )
            record = self._release_record(
                requested_id, source_root, transform_version, partition_range,
                manifest1, expected, discrepancies, status=ReleaseStatus.PENDING_CHECK,
            )
            self._write_json_if_absent(self.releases_dir / f"{requested_id}.json", record)
            self._append_event(
                EventType.CHECK_FAILED,
                {"release_id": requested_id, "discrepancies": discrepancies},
            )
            return PublishResult(record, manifest1)

        # ② 内容寻址打包（对象已存在则复核指纹，否则校验式拷贝）
        signature = manifest1.content_signature()

        try:
            for entry in manifest1.entries:
                obj = self._object_path(entry.sha256)
                if obj.exists():
                    if sha256_file(obj) != entry.sha256:
                        raise StoreCorrupted(f"对象存储内容损坏：{obj}")
                    continue
                obj.parent.mkdir(parents=True, exist_ok=True)
                copy_verified(source_root / entry.rel_path, obj, entry.sha256)
        except SnapshotConflictWrite as exc:
            self._append_event(
                EventType.PACKAGE_ABORTED,
                {"release_id": requested_id, "reason": str(exc),
                 "stage": "object_copy"},
            )
            raise SnapshotConflict(str(exc)) from exc

        # ③ 第二次扫描：打包期间源文件变化 -> 终止，不能混入不同快照
        manifest2 = Manifest.scan(source_root)
        if manifest2.content_signature() != signature:
            self._append_event(
                EventType.PACKAGE_ABORTED,
                {"release_id": requested_id,
                 "reason": "打包期间源快照发生变化（清单②与清单①内容指纹不一致）",
                 "stage": "rescan",
                 "manifest1_signature": signature,
                 "manifest2_signature": manifest2.content_signature()},
            )
            raise SnapshotConflict(
                "打包期间源文件发生变化，已终止本次发布，请重新生成清单后重试"
            )

        # ④ 快照确认稳定后才判定复用：内容、转换版本、分区范围三者一致
        reused = self._find_reusable(signature, transform_version, partition_range)
        if reused is not None:
            return PublishResult(reused, self._load_manifest(reused),
                                 reused=True, reused_from=reused["release_id"])

        # ⑤ 物化产物目录（硬链接优先，不复制字节；跨设备退化为拷贝）
        artifact_dir = self.releases_dir / requested_id
        artifact_dir.mkdir(parents=True, exist_ok=False)
        files_meta = []
        for entry in manifest1.entries:
            obj = self._object_path(entry.sha256)
            dst = artifact_dir / entry.rel_path
            dst.parent.mkdir(parents=True, exist_ok=True)
            kind = "hardlink"
            try:
                hardlink_or_copy(obj, dst)
                if dst.stat().st_ino != obj.stat().st_ino:
                    kind = "copy"
            except OSError:
                shutil.copy2(obj, dst)
                kind = "copy"
            files_meta.append(
                {"rel_path": entry.rel_path, "sha256": entry.sha256,
                 "partition": entry.partition,
                 "object": str(obj.relative_to(self.store)),
                 "artifact": str(dst.relative_to(self.store)),
                 "materialization": kind}
            )

        manifest_path = self.manifests_dir / f"{requested_id}.json"
        atomic_write_bytes(
            manifest_path,
            json.dumps(manifest1.to_dict(), ensure_ascii=False, indent=2).encode("utf-8"),
        )
        record = self._release_record(
            requested_id, source_root, transform_version, partition_range,
            manifest1, expected, [], status=ReleaseStatus.PUBLISHED,
            artifact_files=files_meta, signature=signature,
        )
        self._write_json_if_absent(self.releases_dir / f"{requested_id}.json", record)
        self._append_event(
            EventType.PUBLISHED,
            {"release_id": requested_id, "content_signature": signature,
             "transform_version": transform_version,
             "partition_range": partition_range,
             "artifact_uri": record["artifact_uri"], "reused_objects": len(files_meta)},
        )
        self._set_head(requested_id)
        return PublishResult(record, manifest1)

    def _release_record(
        self, release_id, source_root, transform_version, partition_range,
        manifest, expected, discrepancies, status, artifact_files=None, signature=None,
        rollback_of=None,
    ) -> dict:
        return {
            "release_id": release_id,
            "seq": int(release_id[1:]),
            "status": status,
            "created_at": _utc_now_iso(),
            "source_root": str(source_root),
            "partition_range": partition_range,
            "transform_version": transform_version,
            "manifest_path": f"manifests/{release_id}.json",
            "content_signature": signature or manifest.content_signature(),
            "artifact_uri": f"releases/{release_id}",
            "artifact_files": artifact_files or [],
            "pinned_expected": expected or [],
            "discrepancies": discrepancies,
            "rollback_of": rollback_of,
            "file_count": len(manifest.entries),
        }

    def _find_reusable(self, signature: str, transform_version: str,
                       partition_range: dict) -> dict | None:
        """内容、转换版本、分区范围三者一致 -> 复用既有发布。"""
        for rid in sorted(self._release_ids()):
            rec = self.get_release(rid)
            if rec["status"] != ReleaseStatus.PUBLISHED:
                continue
            if (
                rec["content_signature"] == signature
                and rec["transform_version"] == transform_version
                and _canonical(rec.get("partition_range", {})) == _canonical(partition_range)
            ):
                return rec
        return None

    def _release_ids(self) -> list[str]:
        if not self.releases_dir.exists():
            return []
        return sorted(p.stem for p in self.releases_dir.glob("R*.json"))

    # ---------- 校验 ----------

    def verify(self, release_id: str) -> dict:
        """重新核对源指纹与产物完整性；漂移则置 PENDING_CHECK 并记事件。"""
        rec = self.get_release(release_id)
        manifest = self._load_manifest(rec)
        problems: list[dict] = []

        source_root = Path(rec["source_root"])
        if source_root.is_dir():
            current = Manifest.scan(source_root)
            if current.content_signature() != rec["content_signature"]:
                problems.append({"kind": "SOURCE_DRIFT",
                                 "detail": "源目录内容指纹与发布时不一致"})
        else:
            problems.append({"kind": "SOURCE_ROOT_MISSING", "path": str(source_root)})

        for f in rec.get("artifact_files", []):
            ap = self.store / f["artifact"]
            if not ap.exists():
                problems.append({"kind": "ARTIFACT_MISSING", "path": f["artifact"]})
            elif sha256_file(ap) != f["sha256"]:
                problems.append({"kind": "ARTIFACT_CORRUPT", "path": f["artifact"]})
            op = self.store / f["object"]
            if not op.exists() or sha256_file(op) != f["sha256"]:
                problems.append({"kind": "OBJECT_MISSING_OR_CORRUPT",
                                 "path": f["object"]})

        report = {"release_id": release_id, "ok": not problems, "problems": problems,
                  "checked_at": _utc_now_iso()}
        if problems:
            self._append_event(EventType.VERIFY_DRIFT, report)
            rec["status"] = ReleaseStatus.PENDING_CHECK
            rec["verify_problems"] = problems
            atomic_write_bytes(
                self.releases_dir / f"{release_id}.json",
                json.dumps(rec, ensure_ascii=False, indent=2).encode("utf-8"),
            )
        return report

    # ---------- 比较 ----------

    def compare(self, old_id: str, new_id: str, partition: str | None = None) -> dict:
        old_rec, new_rec = self.get_release(old_id), self.get_release(new_id)
        old_m = self._load_manifest(old_rec).filter_partition(partition)
        new_m = self._load_manifest(new_rec).filter_partition(partition)
        diffs = diff_manifests(old_m, new_m)
        changed = [d for d in diffs if d["kind"] != DiffKind.UNCHANGED]
        return {
            "old_release_id": old_id,
            "new_release_id": new_id,
            "partition": partition,
            "diffs": changed,
            "summary": summarize_diff(diffs),
            "old_transform_version": old_rec["transform_version"],
            "new_transform_version": new_rec["transform_version"],
        }

    # ---------- 时间线（合并事件状态覆盖） ----------

    def timeline(self) -> list[dict]:
        status_override: dict[str, str] = {}
        for ev in self.read_events():
            t, p = ev["type"], ev["payload"]
            if t == EventType.ROLLBACK_COMMITTED:
                for rid in p.get("rolled_back_release_ids", []):
                    status_override[rid] = ReleaseStatus.ROLLED_BACK
                status_override[p["release_id"]] = ReleaseStatus.PUBLISHED
            elif t == EventType.VERIFY_DRIFT:
                status_override[p["release_id"]] = ReleaseStatus.PENDING_CHECK
        out = []
        for rid in self._release_ids():
            rec = self.get_release(rid)
            rec = dict(rec)
            rec["status"] = status_override.get(rid, rec["status"])
            out.append(rec)
        return sorted(out, key=lambda r: r["seq"])

    # ---------- 下游引用（消费者登记） ----------

    def register_consumer(self, name: str, release_id: str, note: str = "") -> dict:
        self.get_release(release_id)  # 必须存在
        with open(self.consumers_path, "r", encoding="utf-8") as f:
            consumers = json.load(f)
        consumers = [c for c in consumers if c["name"] != name]
        record = {"name": name, "release_id": release_id, "note": note,
                  "registered_at": _utc_now_iso()}
        consumers.append(record)
        atomic_write_bytes(
            self.consumers_path,
            json.dumps(consumers, ensure_ascii=False, indent=2).encode("utf-8"),
        )
        self._append_event(EventType.CONSUMER_REGISTERED, record)
        return record

    def list_consumers(self) -> list[dict]:
        with open(self.consumers_path, "r", encoding="utf-8") as f:
            return json.load(f)

    # ---------- 回滚 ----------

    def rollback_preview(self, target_id: str) -> RollbackPlan:
        target = self.get_release(target_id)
        if target["status"] not in (ReleaseStatus.PUBLISHED,):
            # 时间线状态可能已被事件覆盖
            tl = {r["release_id"]: r["status"] for r in self.timeline()}
            if tl.get(target_id) != ReleaseStatus.PUBLISHED:
                raise ReleaseNotFound(
                    f"回滚目标 {target_id} 当前状态为 {tl.get(target_id)}，不可作为回滚目标"
                )
        head_id = self.head()
        new_id = self._next_release_id()
        stale = [
            r["release_id"]
            for r in self.timeline()
            if r["status"] == ReleaseStatus.PUBLISHED and r["seq"] > target["seq"]
        ]
        stale_consumers = [
            c for c in self.list_consumers()
            if c["release_id"] in stale or c["release_id"] == head_id
        ]
        diffs: list[dict] = []
        summary: dict[str, int] = {}
        if head_id and head_id != target_id:
            head_rec = self.get_release(head_id)
            diffs = diff_manifests(self._load_manifest(head_rec),
                                   self._load_manifest(target))
            diffs = [d for d in diffs if d["kind"] != DiffKind.UNCHANGED]
            summary = summarize_diff(diffs)
        plan = RollbackPlan(
            target_release_id=target_id,
            head_release_id=head_id,
            new_rollback_release_id=new_id,
            stale_release_ids=stale,
            stale_consumers=stale_consumers,
            diffs=diffs,
            diff_summary=summary,
            files_affected=len(diffs),
        )
        self._append_event(
            EventType.ROLLBACK_PREVIEW,
            {"target_release_id": target_id, "head_release_id": head_id,
             "simulated_new_release_id": new_id,
             "stale_release_ids": stale,
             "stale_consumers": [c["name"] for c in stale_consumers],
             "files_affected": plan.files_affected},
        )
        return plan

    def rollback_commit(self, target_id: str) -> dict:
        """执行回滚：新增一个回滚版本，完整保留发布时间线，不删除任何文件。"""
        plan = self.rollback_preview(target_id)  # 先重新模拟一次
        target = self.get_release(target_id)
        new_id = self._next_release_id()

        artifact_dir = self.releases_dir / new_id
        artifact_dir.mkdir(parents=True, exist_ok=False)
        files_meta = []
        for f in target["artifact_files"]:
            obj = self.store / f["object"]
            if not obj.exists():
                raise StoreCorrupted(f"回滚所需对象缺失：{obj}")
            dst = artifact_dir / f["rel_path"]
            dst.parent.mkdir(parents=True, exist_ok=True)
            kind = "hardlink"
            hardlink_or_copy(obj, dst)
            if dst.stat().st_ino != obj.stat().st_ino:
                kind = "copy"
            files_meta.append({**f, "artifact": str(dst.relative_to(self.store)),
                               "materialization": kind,
                               "sourced_from_release": target_id})

        target_manifest = self._load_manifest(target)
        manifest_path = self.manifests_dir / f"{new_id}.json"
        atomic_write_bytes(
            manifest_path,
            json.dumps(target_manifest.to_dict(), ensure_ascii=False, indent=2).encode("utf-8"),
        )
        record = self._release_record(
            new_id, Path(target["source_root"]), target["transform_version"],
            target.get("partition_range", {}), target_manifest,
            target.get("pinned_expected", []), [],
            status=ReleaseStatus.PUBLISHED, artifact_files=files_meta,
            signature=target["content_signature"], rollback_of=target_id,
        )
        self._write_json_if_absent(self.releases_dir / f"{new_id}.json", record)
        self._append_event(
            EventType.ROLLBACK_COMMITTED,
            {"release_id": new_id, "rollback_of": target_id,
             "rolled_back_release_ids": plan.stale_release_ids,
             "stale_consumers": [c["name"] for c in plan.stale_consumers],
             "artifact_uri": record["artifact_uri"],
             "note": "仅新增回滚版本并推进 head，未删除任何历史产物文件"},
        )
        self._set_head(new_id)
        return record
