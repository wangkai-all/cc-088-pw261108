"""datareg 单元测试：python3 -I -m unittest discover -s tests -v"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from datareg.errors import ReleaseExistsConflict, SnapshotConflict
from datareg.manifest import Manifest, diff_manifests
from datareg.model import DiffKind, ReleaseStatus
from datareg.registry import Registry


def write(path: Path, text: str, mtime: float | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))


class RegistryTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="datareg-test-"))
        self.store = self.tmp / "store"
        self.data = self.tmp / "data"
        self.reg = Registry.create(self.store)
        write(self.data / "dt=2026-11-13/a.csv", "a,b\n1,2\n", 1_700_000_000)
        write(self.data / "dt=2026-11-13/b.csv", "x,y\n3,4\n", 1_700_000_000)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def publish(self, **kw):
        kw.setdefault("transform_version", "t@1")
        kw.setdefault("partition_range", {"dt": "2026-11-13"})
        return self.reg.publish(self.data, **kw)


class TestPublishAndReuse(RegistryTestBase):
    def test_first_publish_records_fingerprint_partition_transform_artifact(self):
        result = self.publish()
        rec = result.release
        self.assertEqual(rec["release_id"], "R000001")
        self.assertEqual(rec["status"], ReleaseStatus.PUBLISHED)
        self.assertEqual(rec["partition_range"], {"dt": "2026-11-13"})
        self.assertEqual(rec["transform_version"], "t@1")
        self.assertEqual(rec["file_count"], 2)
        self.assertTrue((self.store / rec["artifact_uri"] / "dt=2026-11-13/a.csv").exists())
        self.assertEqual(self.reg.head(), "R000001")
        # 产物与对象存储硬链接同一 inode
        f = rec["artifact_files"][0]
        self.assertEqual(
            (self.store / f["artifact"]).stat().st_ino,
            (self.store / f["object"]).stat().st_ino,
        )

    def test_unchanged_content_is_reused_no_new_release(self):
        self.publish()
        result = self.publish()
        self.assertTrue(result.reused)
        self.assertEqual(result.reused_from, "R000001")
        self.assertFalse((self.store / "releases" / "R000002.json").exists())

    def test_transform_change_creates_new_release(self):
        self.publish()
        result = self.publish(transform_version="t@2")
        self.assertFalse(result.reused)
        self.assertEqual(result.release["release_id"], "R000002")
        self.assertEqual(self.reg.head(), "R000002")

    def test_write_once_blocks_overwrite(self):
        self.publish()
        with self.assertRaises(ReleaseExistsConflict):
            self.reg._write_json_if_absent(
                self.store / "releases" / "R000001.json", {"x": 1}
            )


class TestPendingCheck(RegistryTestBase):
    def test_missing_and_mismatch_stop_at_pending_check(self):
        m = Manifest.scan(self.data)
        expected = [{"rel_path": e.rel_path, "sha256": e.sha256} for e in m.entries]
        expected[0]["sha256"] = "0" * 64
        expected.append({"rel_path": "ghost.csv", "sha256": "f" * 64})
        result = self.publish(expected=expected)
        rec = result.release
        self.assertEqual(rec["status"], ReleaseStatus.PENDING_CHECK)
        kinds = {d["kind"] for d in rec["discrepancies"]}
        self.assertIn("MISSING_FILE", kinds)
        self.assertIn("FINGERPRINT_MISMATCH", kinds)
        # 待检查发布不推进 head，也不产物化
        self.assertIsNone(self.reg.head())
        self.assertEqual(rec["artifact_files"], [])


class TestSnapshotConflict(RegistryTestBase):
    def test_mutation_during_packaging_aborts_and_no_release(self):
        def hook(_m):
            write(self.data / "dt=2026-11-13/a.csv", "a,b\n1,2\n3,3\n")

        with self.assertRaises(SnapshotConflict):
            self.publish(mutate_hook=hook)
        aborted = [e for e in self.reg.read_events() if e["type"] == "PACKAGE_ABORTED"]
        self.assertEqual(len(aborted), 1)
        self.assertIsNone(self.reg.head())
        # 没有产生任何发布登记
        self.assertEqual(self.reg.timeline(), [])

    def test_retry_after_stabilization_succeeds(self):
        def hook(_m):
            write(self.data / "dt=2026-11-13/a.csv", "changed")

        with self.assertRaises(SnapshotConflict):
            self.publish(mutate_hook=hook)
        result = self.publish()
        self.assertEqual(result.release["release_id"], "R000001")
        self.assertEqual(self.reg.head(), "R000001")

    def test_hook_adding_new_file_must_not_fall_through_to_reuse(self):
        # 回归：复用判定不得早于二次快照扫描，否则打包期新增文件会被漏检
        self.publish()
        def hook(_m):
            write(self.data / "dt=2026-11-13/racing.csv", "born during packaging\n")
        with self.assertRaises(SnapshotConflict):
            self.publish(mutate_hook=hook)
        self.assertEqual(self.reg.head(), "R000001")


class TestDiffClassification(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="datareg-diff-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_five_diff_kinds_and_stable_order(self):
        old_root = self.tmp / "old"
        new_root = self.tmp / "new"
        write(old_root / "keep.csv", "same\n", 1_000)
        write(old_root / "meta.csv", "meta\n", 1_000)
        write(old_root / "move.csv", "moved-content\n", 1_000)
        write(old_root / "gone.csv", "bye\n", 1_000)

        write(new_root / "keep.csv", "same\n", 1_000)
        write(new_root / "meta.csv", "meta\n", 2_000)          # 仅 mtime 变
        write(new_root / "nested/move.csv", "moved-content\n", 1_000)  # 移动
        write(new_root / "fresh.csv", "hi\n", 1_000)           # 新增
        # keep 之外再造一个内容变化
        write(old_root / "content.csv", "v1\n", 1_000)
        write(new_root / "content.csv", "v2-with-different-bytes\n", 1_000)

        diffs = diff_manifests(Manifest.scan(old_root), Manifest.scan(new_root))
        kinds = {d["kind"] for d in diffs}
        self.assertEqual(
            kinds,
            {DiffKind.ADDED, DiffKind.REMOVED, DiffKind.CONTENT_CHANGED,
             DiffKind.METADATA_CHANGED, DiffKind.MOVED},
        )
        # 稳定顺序：按展示路径排序
        paths = [d["path"] for d in diffs]
        self.assertEqual(paths, sorted(paths))
        moved = next(d for d in diffs if d["kind"] == DiffKind.MOVED)
        self.assertEqual(moved["from_path"], "move.csv")
        self.assertEqual(moved["to_path"], "nested/move.csv")
        # 未变化条目不出现在结果里
        self.assertNotIn("keep.csv", [d["path"] for d in diffs if d["kind"] != DiffKind.UNCHANGED])


class TestRollback(RegistryTestBase):
    def _three_releases(self):
        r1 = self.publish().release
        write(self.data / "dt=2026-11-13/c.csv", "new-file\n")
        r2 = self.publish().release
        write(self.data / "dt=2026-11-13/a.csv", "totally different content\n")
        r3 = self.publish(transform_version="t@2").release
        return r1, r2, r3

    def test_preview_is_simulation_only_and_lists_stale_consumers(self):
        r1, r2, r3 = self._three_releases()
        self.reg.register_consumer("job-a", "R000003", note="下游")
        before = sorted(p for p in self.store.rglob("*"))
        plan = self.reg.rollback_preview("R000001")
        after = sorted(p for p in self.store.rglob("*"))
        self.assertEqual(before, after)  # 零文件变化（事件日志除外：预览事件）
        # 预览只允许追加 events.log
        real_before = [p for p in before if p.name != "events.log"]
        real_after = [p for p in after if p.name != "events.log"]
        self.assertEqual(real_before, real_after)
        self.assertEqual(plan.stale_release_ids, ["R000002", "R000003"])
        self.assertEqual([c["name"] for c in plan.stale_consumers], ["job-a"])
        self.assertGreater(plan.files_affected, 0)

    def test_commit_adds_rollback_release_and_keeps_timeline(self):
        r1, r2, r3 = self._three_releases()
        rec = self.reg.rollback_commit("R000001")
        self.assertEqual(rec["rollback_of"], "R000001")
        self.assertEqual(rec["release_id"], "R000004")
        self.assertEqual(self.reg.head(), "R000004")
        ids = [r["release_id"] for r in self.reg.timeline()]
        self.assertEqual(ids, ["R000001", "R000002", "R000003", "R000004"])
        statuses = {r["release_id"]: r["status"] for r in self.reg.timeline()}
        self.assertEqual(statuses["R000002"], ReleaseStatus.ROLLED_BACK)
        self.assertEqual(statuses["R000003"], ReleaseStatus.ROLLED_BACK)
        self.assertEqual(statuses["R000004"], ReleaseStatus.PUBLISHED)
        # 历史产物一个都没删
        for rid in ("R000001", "R000002", "R000003", "R000004"):
            self.assertTrue((self.store / "releases" / f"{rid}.json").exists())
        # 回滚版本与目标内容零差异
        cmp_ = self.reg.compare("R000001", "R000004")
        self.assertEqual(cmp_["diffs"], [])

    def test_rollback_target_must_be_published(self):
        from datareg.errors import ReleaseNotFound
        m = Manifest.scan(self.data)
        expected = [{"rel_path": "nope.csv", "sha256": "0" * 64}]
        self.publish(expected=expected)  # R000001 待检查
        with self.assertRaises(ReleaseNotFound):
            self.reg.rollback_preview("R000001")


class TestVerify(RegistryTestBase):
    def test_drift_moves_release_to_pending_check(self):
        rec = self.publish().release
        report = self.reg.verify(rec["release_id"])
        self.assertTrue(report["ok"])
        write(self.data / "dt=2026-11-13/a.csv", "drifted content\n")
        report = self.reg.verify(rec["release_id"])
        self.assertFalse(report["ok"])
        statuses = {r["release_id"]: r["status"] for r in self.reg.timeline()}
        self.assertEqual(statuses["R000001"], ReleaseStatus.PENDING_CHECK)


class TestPartitionScoping(RegistryTestBase):
    def test_partition_filter_only_matches_scope(self):
        write(self.data / "dt=2026-11-12/old.csv", "old\n")
        self.publish()
        write(self.data / "dt=2026-11-12/old.csv", "old-changed\n")
        # 内容保持不变，同时固定 mtime 避免被识别为元数据变化
        write(self.data / "dt=2026-11-13/a.csv", "a,b\n1,2\n", 1_700_000_000)
        r2 = self.publish().release
        cmp_ = self.reg.compare("R000001", r2["release_id"], partition="dt=2026-11-13")
        self.assertEqual(cmp_["diffs"], [])
        full = self.reg.compare("R000001", r2["release_id"])
        self.assertTrue(any(d["kind"] == DiffKind.CONTENT_CHANGED for d in full["diffs"]))


if __name__ == "__main__":
    unittest.main()
