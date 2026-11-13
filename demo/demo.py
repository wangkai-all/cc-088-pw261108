#!/usr/bin/env python3
"""本地数据端到端演示：数据集发布登记台 datareg。

用隔离模式运行（不依赖当前目录的第三方模块）：
    python3 -I demo/demo.py

演示场景：
  1. 初始化登记台与带分区的本地数据
  2. R000001 首次发布（登记指纹 / 分区 / 转换版本 / 产物位置）
  3. R000002 内容未变 -> 复用，不产生新版本
  4. 数据变化后 R000003 发布，文件级差异（移动 / 内容变化 / 元数据变化 / 新增 / 删除）
  5. 按分区比较
  6. 期望指纹缺失 / 不一致 -> R000004 停在待检查，head 不推进
  7. 打包期间源文件变化 -> 两阶段快照终止（不混入不同快照）
  8. 同发布号写一次保护（内容不同禁止覆盖）
  9. 回滚预览：列出会变旧的下游引用与受影响文件（只模拟、不删文件）
 10. 回滚执行：新增回滚版本，完整时间线保留
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

# 隔离模式 (-I) 下 sys.path 不含 cwd，用脚本位置把仓库根加进去
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from datareg.errors import ReleaseExistsConflict, SnapshotConflict
from datareg.manifest import Manifest
from datareg.registry import Registry
from datareg.model import DiffKind, ReleaseStatus

BANNER = "=" * 72


def banner(title: str) -> None:
    print(f"\n{BANNER}\n{title}\n{BANNER}")


def write(path: Path, text: str, mtime: float | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if mtime is not None:
        import os
        os.utime(path, (mtime, mtime))


def main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="datareg-demo-"))
    store = workdir / "registry-store"
    data = workdir / "input-data"
    print(f"演示工作目录：{workdir}")

    # ---------------------------------------------------------------
    banner("场景 1：初始化登记台，准备带 Hive 分区的本地数据")
    # ---------------------------------------------------------------
    reg = Registry.create(store)
    base_mtime = 1_700_000_000.0
    write(data / "dt=2026-11-12/region=cn/users.csv",
          "id,name\n1,alice\n2,bob\n", base_mtime)
    write(data / "dt=2026-11-12/region=us/users.csv",
          "id,name\n10,carol\n", base_mtime)
    write(data / "dt=2026-11-13/region=cn/orders.csv",
          "oid,amount\n100,9.9\n101,19.9\n", base_mtime)
    write(data / "docs/schema.json", '{"tables": ["users", "orders"]}\n', base_mtime)
    print("源数据：")
    for p in sorted(data.rglob("*")):
        if p.is_file():
            print(f"  {p.relative_to(data)}")

    # ---------------------------------------------------------------
    banner("场景 2：R000001 首次发布")
    # ---------------------------------------------------------------
    r1 = reg.publish(
        data,
        transform_version="transform@git:8f3a21c",
        partition_range={"dt": "2026-11-12,2026-11-13"},
    )
    rec1 = r1.release
    print(f"发布号   : {rec1['release_id']}  状态: {rec1['status']}")
    print(f"内容指纹 : {rec1['content_signature']}")
    print(f"分区范围 : {rec1['partition_range']}")
    print(f"转换版本 : {rec1['transform_version']}")
    print(f"产物位置 : {rec1['artifact_uri']}（硬链接到内容寻址对象）")
    print("产物清单：")
    for f in rec1["artifact_files"]:
        print(f"  {f['rel_path']:<42} {f['sha256'][:12]}…  {f['materialization']}")
    assert rec1["release_id"] == "R000001"
    assert reg.head() == "R000001"

    # ---------------------------------------------------------------
    banner("场景 3：R000002 输入与转换均未变化 -> 复用，不产生新版本")
    # ---------------------------------------------------------------
    r2 = reg.publish(
        data,
        transform_version="transform@git:8f3a21c",
        partition_range={"dt": "2026-11-12,2026-11-13"},
    )
    print(f"reused={r2.reused}，复用来源={r2.reused_from}，head 仍为 {reg.head()}")
    assert r2.reused and r2.reused_from == "R000001"
    assert not (store / "releases" / "R000002.json").exists()

    # ---------------------------------------------------------------
    banner("场景 4：数据变化后 R000003，展示五类文件级差异")
    # ---------------------------------------------------------------
    # 内容变化
    write(data / "dt=2026-11-13/region=cn/orders.csv",
          "oid,amount\n100,9.9\n101,19.9\n102,29.9\n", base_mtime)
    # 移动文件（同内容换路径）
    shutil.move(str(data / "docs/schema.json"), str(data / "schema.json"))
    # 仅元数据变化（内容不动，mtime 改变）
    import os
    os.utime(data / "dt=2026-11-12/region=us/users.csv",
             (base_mtime + 86400, base_mtime + 86400))
    # 新增文件
    write(data / "dt=2026-11-13/region=us/orders.csv",
          "oid,amount\n200,5.0\n", base_mtime)
    # 删除文件（覆盖演示：删掉 cn 旧分区 users）
    (data / "dt=2026-11-12/region=cn/users.csv").unlink()

    r3 = reg.publish(
        data,
        transform_version="transform@git:8f3a21c",  # 转换代码不变，纯输入变化
        partition_range={"dt": "2026-11-12,2026-11-13"},
    )
    rec3 = r3.release
    id3 = rec3["release_id"]
    print(f"新发布号 : {id3}（转换版本未变，输入变化可追溯到新指纹）")
    comparison = reg.compare("R000001", id3)
    cn = {
        DiffKind.ADDED: "新增", DiffKind.REMOVED: "删除",
        DiffKind.CONTENT_CHANGED: "内容变化",
        DiffKind.METADATA_CHANGED: "元数据变化", DiffKind.MOVED: "移动",
    }
    for d in comparison["diffs"]:
        if d["kind"] == DiffKind.MOVED:
            print(f"  [{cn[d['kind']]}] {d['from_path']}  ->  {d['to_path']}")
        else:
            print(f"  [{cn[d['kind']]}] {d['path']}")
    kinds = {d["kind"] for d in comparison["diffs"]}
    assert {DiffKind.ADDED, DiffKind.REMOVED, DiffKind.CONTENT_CHANGED,
            DiffKind.METADATA_CHANGED, DiffKind.MOVED} <= kinds

    # ---------------------------------------------------------------
    banner("场景 5：按分区比较（只看 dt=2026-11-13）")
    # ---------------------------------------------------------------
    scoped = reg.compare("R000001", id3, partition="dt=2026-11-13")
    print(f"分区比较条目数：{len(scoped['diffs'])}（全量为 {len(comparison['diffs'])}）")
    for d in scoped["diffs"]:
        print(f"  [{cn[d['kind']]}] {d['path']}")
    assert all("dt=2026-11-13" in d["path"] or d["kind"] == DiffKind.MOVED
               and "dt=2026-11-13" in d["to_path"] for d in scoped["diffs"])

    # ---------------------------------------------------------------
    banner("场景 6：文件缺失 / 指纹不一致 -> R000004 停在待检查")
    # ---------------------------------------------------------------
    good_manifest = Manifest.scan(data)
    expected = [
        {"rel_path": e.rel_path, "sha256": e.sha256} for e in good_manifest.entries
    ]
    # 篡改：一个指纹改错；再加一个实际不存在的文件
    expected[0]["sha256"] = "0" * 64
    expected.append({"rel_path": "dt=2026-11-13/region=cn/ghost.csv",
                     "sha256": "f" * 64})
    r4 = reg.publish(
        data,
        transform_version="transform@git:8f3a21c",
        partition_range={"dt": "2026-11-12,2026-11-13"},
        expected=expected,
    )
    rec4 = r4.release
    id4 = rec4["release_id"]
    print(f"发布号 {id4} 状态：{rec4['status']}（head 不推进）")
    for d in rec4["discrepancies"]:
        print(f"  - {d['kind']}: {d['rel_path']}")
    assert rec4["status"] == ReleaseStatus.PENDING_CHECK
    assert reg.head() == id3

    # ---------------------------------------------------------------
    banner("场景 7：打包期间源文件变化 -> 两阶段快照检测并终止")
    # ---------------------------------------------------------------
    def mutate_after_first_scan(_manifest: Manifest) -> None:
        # 清单①生成之后、打包过程中改写源文件，并新增一个清单①之外的文件，
        # 模拟“打包期间另一个进程写入了新快照”
        write(data / "dt=2026-11-13/region=cn/orders.csv",
              "oid,amount\n100,9.9\n101,19.9\n102,29.9\n999,0.1\n", base_mtime)
        write(data / "dt=2026-11-13/region=cn/racing.csv",
          "oid,amount\nrace,true\n", base_mtime)

    try:
        reg.publish(
            data,
            transform_version="transform@git:8f3a21c",
            partition_range={"dt": "2026-11-12,2026-11-13"},
            mutate_hook=mutate_after_first_scan,
        )
        raise AssertionError("应当抛出 SnapshotConflict")
    except SnapshotConflict as exc:
        print(f"[已终止] {exc}")
    aborted = [e for e in reg.read_events() if e["type"] == "PACKAGE_ABORTED"]
    print(f"事件日志中的 PACKAGE_ABORTED 记录：{len(aborted)} 条")
    assert aborted
    assert reg.head() == id3  # head 未受影响

    # 恢复稳定后重新发布成功（待检查/中止都不推进 head，新号继续递增）
    r5 = reg.publish(
        data,
        transform_version="transform@git:8f3a21c",
        partition_range={"dt": "2026-11-12,2026-11-13"},
    )
    id5 = r5.release["release_id"]
    print(f"源目录恢复稳定后重新发布：{id5}，head={reg.head()}")
    assert r5.release["status"] == ReleaseStatus.PUBLISHED
    assert reg.head() == id5

    # ---------------------------------------------------------------
    banner("场景 8：同发布号内容不同禁止覆盖（写一次语义）")
    # ---------------------------------------------------------------
    target = store / "releases" / "R000001.json"
    try:
        reg._write_json_if_absent(target, {"forbidden": True})
        raise AssertionError("应当拒绝覆盖")
    except ReleaseExistsConflict as exc:
        print(f"[拒绝] {exc}")

    # ---------------------------------------------------------------
    banner("场景 9：登记下游引用，回滚预览（只模拟状态、不删文件）")
    # ---------------------------------------------------------------
    reg.register_consumer("daily-report", id5, note="日报任务")
    reg.register_consumer("ml-feature-pipeline", id3, note="特征工程")
    before_files = sorted(str(p.relative_to(store)) for p in store.rglob("*") if p.is_file())

    plan = reg.rollback_preview("R000001")
    print(f"回滚目标      : {plan.target_release_id}")
    print(f"当前 head     : {plan.head_release_id}")
    print(f"拟新增回滚号  : {plan.new_rollback_release_id}")
    print(f"将变旧的发布  : {plan.stale_release_ids}")
    print("会变旧的下游引用：")
    for c in plan.stale_consumers:
        print(f"  - {c['name']} 引用 {c['release_id']}（{c['note']}）")
    print(f"受影响文件数  : {plan.files_affected}")
    for d in plan.diffs:
        label = cn.get(d["kind"], d["kind"])
        print(f"  [{label}] {d['path']}")
    print(plan.note)

    after_files = sorted(str(p.relative_to(store)) for p in store.rglob("*") if p.is_file())
    assert before_files == after_files, "预览阶段存储目录发生了文件变化！"
    print("校验：预览前后存储文件列表完全一致（纯模拟，零写入产物）")

    # ---------------------------------------------------------------
    banner("场景 10：执行回滚 -> 新增回滚版本，完整时间线保留、不删文件")
    # ---------------------------------------------------------------
    rollback_rec = reg.rollback_commit("R000001")
    print(f"新增回滚版本 : {rollback_rec['release_id']}，rollback_of={rollback_rec['rollback_of']}")
    print(f"head 推进至  : {reg.head()}")
    print("\n完整发布时间线（历史发布全部保留）：")
    for rec in reg.timeline():
        note = f"  <- 回滚自 {rec['rollback_of']}" if rec.get("rollback_of") else ""
        print(f"  {rec['release_id']}  {rec['status']:<13} "
              f"文件 {rec['file_count']} 个  {rec['transform_version']}{note}")

    # 回滚产物与 R000001 内容一致，但发布号是新的
    cmp_rb = reg.compare("R000001", rollback_rec["release_id"])
    assert not cmp_rb["diffs"], "回滚版本内容应与目标发布一致"
    assert (store / "releases" / "R000001.json").exists()
    assert (store / "releases" / f"{id3}.json").exists()
    print(f"\n校验：R000001/{id3} 等历史登记与产物均保留，回滚版本与 R000001 零差异")

    # 事件日志佐证完整审计链
    print("\n事件日志（append-only 审计链）：")
    for ev in reg.read_events():
        payload = json.dumps(ev["payload"], ensure_ascii=False)
        print(f"  #{ev['seq']:<3} {ev['type']:<22} {payload[:80]}")

    print(f"\n演示全部通过。登记台位于：{store}")
    print("可用 `python3 -m datareg --help` 查看命令，或直接检查该目录内容。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
