"""清单（Manifest）：稳定相对路径顺序的文件指纹快照与文件级差异。"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .fsutil import sha256_file
from .model import DiffKind


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def derive_partitions(rel_parts: tuple[str, ...]) -> tuple[str, dict[str, str]]:
    """从相对路径组件中提取 Hive 风格分区，如 dt=2026-11-13/region=cn。

    返回 (主分区 "k=v" 拼接字符串, {k: v})；无分区组件时为 ("", {})。
    """
    kv: dict[str, str] = {}
    order: list[str] = []
    for part in rel_parts[:-1]:  # 文件名本身不算分区
        if "=" in part:
            k, v = part.split("=", 1)
            if k and k not in kv:
                kv[k] = v
                order.append(f"{k}={v}")
    return "/".join(order), kv


@dataclass(frozen=True)
class FileEntry:
    rel_path: str
    sha256: str
    size_bytes: int
    mtime_ns: int
    mode: int
    partition: str = ""
    partitions: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "rel_path": self.rel_path,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "mtime_ns": self.mtime_ns,
            "mode": self.mode,
            "partition": self.partition,
            "partitions": dict(self.partitions),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "FileEntry":
        return cls(
            rel_path=d["rel_path"],
            sha256=d["sha256"],
            size_bytes=d["size_bytes"],
            mtime_ns=d["mtime_ns"],
            mode=d["mode"],
            partition=d.get("partition", ""),
            partitions=dict(d.get("partitions", {})),
        )


@dataclass
class Manifest:
    """某一时刻源目录的完整文件清单，条目严格按相对路径字典序排列。"""

    root: str
    created_at: str
    entries: list[FileEntry]

    @classmethod
    def scan(cls, root: Path) -> "Manifest":
        """扫描源目录生成清单。仅收录普通文件，相对路径使用 POSIX 风格。"""
        root = Path(root).resolve()
        entries: list[FileEntry] = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames.sort()
            for name in sorted(filenames):
                p = Path(dirpath) / name
                if not p.is_file() or p.is_symlink():
                    continue
                st = p.stat()
                rel = p.relative_to(root).as_posix()
                partition, kv = derive_partitions(p.relative_to(root).parts)
                entries.append(
                    FileEntry(
                        rel_path=rel,
                        sha256=sha256_file(p),
                        size_bytes=st.st_size,
                        mtime_ns=st.st_mtime_ns,
                        mode=st.st_mode & 0o7777,
                        partition=partition,
                        partitions=kv,
                    )
                )
        entries.sort(key=lambda e: e.rel_path)
        return cls(root=str(root), created_at=_utc_now_iso(), entries=entries)

    def to_dict(self) -> dict:
        return {
            "root": self.root,
            "created_at": self.created_at,
            "entries": [e.to_dict() for e in self.entries],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Manifest":
        return cls(
            root=d["root"],
            created_at=d["created_at"],
            entries=[FileEntry.from_dict(x) for x in d["entries"]],
        )

    def by_path(self) -> dict[str, FileEntry]:
        return {e.rel_path: e for e in self.entries}

    def content_signature(self) -> str:
        """内容指纹：只取 (相对路径, 内容 sha256) 的规范化 JSON 再哈希。

        元数据（mtime/权限/mode）不参与内容签名，因此纯元数据变化
        不会产生新的内容版本，可被安全复用。
        """
        payload = sorted((e.rel_path, e.sha256) for e in self.entries)
        canonical = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    def filter_partition(self, partition: str | None) -> "Manifest":
        """返回仅包含指定分区（匹配任一 k=v 路径组件）的清单视图。"""
        if not partition:
            return self
        kept = [
            e
            for e in self.entries
            if partition in e.partitions
            or any(f"{k}={v}" == partition for k, v in e.partitions.items())
        ]
        return Manifest(root=self.root, created_at=self.created_at, entries=kept)


def make_manifest(root: Path) -> Manifest:
    """便捷入口：扫描目录生成清单。"""
    return Manifest.scan(root)


def _entry_brief(e: FileEntry | None) -> dict | None:
    return None if e is None else e.to_dict()


def diff_manifests(old: Manifest, new: Manifest) -> list[dict]:
    """文件级差异，区分移动 / 内容变化 / 元数据变化 / 新增 / 删除 / 未变。

    移动识别：内容指纹相同但相对路径变化，按 sha256 分组后确定性配对
    （同指纹的旧路径与新路径各自排序后一一配对）。
    结果按展示路径字典序返回，保证输出稳定。
    """
    old_map = old.by_path()
    new_map = new.by_path()

    # 1. 同路径比较
    common = sorted(set(old_map) & set(new_map))
    results: list[dict] = []
    moved_old: dict[str, str] = {}   # old_path -> new_path
    moved_new: dict[str, str] = {}   # new_path -> old_path

    # 2. 移动配对：只在“旧路径消失 + 新路径出现”且指纹相同的集合中进行
    removed_paths = sorted(p for p in old_map if p not in new_map)
    added_paths = sorted(p for p in new_map if p not in old_map)
    old_by_hash: dict[str, list[str]] = {}
    new_by_hash: dict[str, list[str]] = {}
    for p in removed_paths:
        old_by_hash.setdefault(old_map[p].sha256, []).append(p)
    for p in added_paths:
        new_by_hash.setdefault(new_map[p].sha256, []).append(p)

    for digest in sorted(set(old_by_hash) & set(new_by_hash)):
        olds = sorted(old_by_hash[digest])
        news = sorted(new_by_hash[digest])
        for op, np_ in zip(olds, news):
            moved_old[op] = np_
            moved_new[np_] = op
        # 配对后剩余的仍按删除/新增处理（数量不对称时）

    for p in common:
        o, n = old_map[p], new_map[p]
        if o.sha256 != n.sha256:
            kind = DiffKind.CONTENT_CHANGED
        elif o.mtime_ns != n.mtime_ns or o.mode != n.mode or o.size_bytes != n.size_bytes:
            kind = DiffKind.METADATA_CHANGED
        else:
            continue  # 完全未变的文件不属于差异，直接跳过
        results.append(
            {
                "kind": kind,
                "path": p,
                "partition": n.partition or o.partition,
                "old": _entry_brief(o),
                "new": _entry_brief(n),
            }
        )

    for op, np_ in sorted(moved_old.items()):
        results.append(
            {
                "kind": DiffKind.MOVED,
                "path": np_,
                "from_path": op,
                "to_path": np_,
                "partition": new_map[np_].partition,
                "old": _entry_brief(old_map[op]),
                "new": _entry_brief(new_map[np_]),
            }
        )

    for p in removed_paths:
        if p in moved_old:
            continue
        results.append(
            {
                "kind": DiffKind.REMOVED,
                "path": p,
                "partition": old_map[p].partition,
                "old": _entry_brief(old_map[p]),
                "new": None,
            }
        )

    for p in added_paths:
        if p in moved_new:
            continue
        results.append(
            {
                "kind": DiffKind.ADDED,
                "path": p,
                "partition": new_map[p].partition,
                "old": None,
                "new": _entry_brief(new_map[p]),
            }
        )

    results.sort(key=lambda d: d["path"])
    return results


def summarize_diff(diffs: Iterable[dict]) -> dict[str, int]:
    """按差异类型计数摘要。"""
    summary = {
        DiffKind.ADDED: 0,
        DiffKind.REMOVED: 0,
        DiffKind.CONTENT_CHANGED: 0,
        DiffKind.METADATA_CHANGED: 0,
        DiffKind.MOVED: 0,
        DiffKind.UNCHANGED: 0,
    }
    for d in diffs:
        summary[d["kind"]] += 1
    return summary
