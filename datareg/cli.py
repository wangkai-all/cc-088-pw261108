"""datareg 命令行入口。

用法示例：
  python -m datareg init --store ./demo-store
  python -m datareg publish --store ./demo-store --source ./data \
      --transform transform@git:abc123 --partition-range '{"dt":"2026-11-13"}'
  python -m datareg diff --store ./demo-store R000001 R000002
  python -m datareg rollback preview --store ./demo-store --target R000001
  python -m datareg rollback commit --store ./demo-store --target R000001
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .errors import DataRegError, SnapshotConflict
from .manifest import summarize_diff
from .model import DiffKind, ReleaseStatus
from .registry import Registry

STATUS_CN = {
    ReleaseStatus.PENDING_CHECK: "待检查",
    ReleaseStatus.PUBLISHED: "已发布",
    ReleaseStatus.ROLLED_BACK: "已回滚",
}

DIFF_CN = {
    DiffKind.ADDED: "新增",
    DiffKind.REMOVED: "删除",
    DiffKind.CONTENT_CHANGED: "内容变化",
    DiffKind.METADATA_CHANGED: "元数据变化",
    DiffKind.MOVED: "移动",
    DiffKind.UNCHANGED: "未变",
}


def _print_json(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def _open_store(args) -> Registry:
    return Registry.open(args.store)


def cmd_init(args) -> int:
    try:
        Registry.create(args.store)
    except FileExistsError as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 2
    print(f"[OK] 登记台已初始化：{Path(args.store).resolve()}")
    return 0


def cmd_publish(args) -> int:
    reg = _open_store(args)
    partition_range = json.loads(args.partition_range) if args.partition_range else {}
    expected = json.loads(Path(args.expected).read_text(encoding="utf-8")) if args.expected else None
    try:
        result = reg.publish(
            source_root=args.source,
            transform_version=args.transform,
            partition_range=partition_range,
            expected=expected,
        )
    except SnapshotConflict as exc:
        print(f"[终止] {exc}", file=sys.stderr)
        print("[终止] 本次发布未生成任何发布记录，源目录恢复稳定后请重试。", file=sys.stderr)
        return 3
    rec = result.release
    if args.json:
        _print_json(rec)
        return 0
    if result.reused:
        print(f"[复用] 内容/转换版本/分区完全一致，复用发布 {result.reused_from}，未生成新版本")
        return 0
    if rec["status"] == ReleaseStatus.PENDING_CHECK:
        print(f"[待检查] 发布号 {rec['release_id']} 已停在待检查状态，head 未推进")
        for d in rec["discrepancies"]:
            print(f"  - {d['kind']}: {d['rel_path']}")
        return 4
    print(f"[OK] 已发布 {rec['release_id']}")
    print(f"  源目录      : {rec['source_root']}")
    print(f"  分区范围    : {json.dumps(rec['partition_range'], ensure_ascii=False)}")
    print(f"  转换版本    : {rec['transform_version']}")
    print(f"  内容指纹    : {rec['content_signature'][:16]}…")
    print(f"  产物位置    : {rec['artifact_uri']}（{rec['file_count']} 个文件）")
    return 0


def cmd_list(args) -> int:
    reg = _open_store(args)
    timeline = reg.timeline()
    if args.json:
        _print_json(timeline)
        return 0
    print(f"{'发布号':<10}{'状态':<8}{'转换版本':<24}{'文件':<6}{'分区':<20}说明")
    for rec in timeline:
        note = f"回滚自 {rec['rollback_of']}" if rec.get("rollback_of") else ""
        if rec["status"] == ReleaseStatus.PENDING_CHECK:
            note = "待检查：" + "; ".join(d["kind"] for d in rec["discrepancies"])
        print(
            f"{rec['release_id']:<10}{STATUS_CN.get(rec['status'], rec['status']):<8}"
            f"{rec['transform_version']:<24}{rec['file_count']:<6}"
            f"{json.dumps(rec['partition_range'], ensure_ascii=False):<20}{note}"
        )
    print(f"\n当前 head：{reg.head() or '（无）'}")
    return 0


def cmd_show(args) -> int:
    reg = _open_store(args)
    rec = reg.get_release(args.release_id)
    if args.json:
        _print_json(rec)
        return 0
    print(json.dumps(rec, ensure_ascii=False, indent=2))
    return 0


def cmd_verify(args) -> int:
    reg = _open_store(args)
    report = reg.verify(args.release_id)
    if args.json:
        _print_json(report)
        return 0
    if report["ok"]:
        print(f"[OK] {args.release_id} 源指纹与全部产物校验通过")
        return 0
    print(f"[漂移] {args.release_id} 已转入待检查状态：", file=sys.stderr)
    for p in report["problems"]:
        print(f"  - {p}", file=sys.stderr)
    return 4


def _render_diff(comparison: dict) -> None:
    print(
        f"比较 {comparison['old_release_id']} -> {comparison['new_release_id']}"
        + (f"（分区 {comparison['partition']}）" if comparison.get("partition") else "")
    )
    print(
        f"转换版本：{comparison['old_transform_version']} -> "
        f"{comparison['new_transform_version']}\n"
    )
    if not comparison["diffs"]:
        print("无文件级差异（内容、元数据、路径均一致）。")
        return
    for d in comparison["diffs"]:
        label = DIFF_CN.get(d["kind"], d["kind"])
        if d["kind"] == DiffKind.MOVED:
            print(f"  [{label}] {d['from_path']}  ->  {d['to_path']}（分区 {d['partition'] or '-'}）")
        else:
            extra = ""
            if d["kind"] == DiffKind.CONTENT_CHANGED:
                extra = f"  {d['old']['sha256'][:10]}… -> {d['new']['sha256'][:10]}…"
            print(f"  [{label}] {d['path']}（分区 {d.get('partition') or '-'}）{extra}")
    s = comparison["summary"]
    print(
        "\n摘要："
        f"新增 {s[DiffKind.ADDED]} / 删除 {s[DiffKind.REMOVED]} / "
        f"内容变化 {s[DiffKind.CONTENT_CHANGED]} / 元数据变化 {s[DiffKind.METADATA_CHANGED]} / "
        f"移动 {s[DiffKind.MOVED]}"
    )


def cmd_diff(args) -> int:
    reg = _open_store(args)
    comparison = reg.compare(args.old, args.new, partition=args.partition)
    if args.json:
        _print_json(comparison)
    else:
        _render_diff(comparison)
    return 0


def cmd_rollback_preview(args) -> int:
    reg = _open_store(args)
    try:
        plan = reg.rollback_preview(args.target)
    except DataRegError as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 2
    if args.json:
        _print_json(plan.__dict__)
        return 0
    print("=== 回滚影响预览（只模拟状态，不删除任何文件）===")
    print(f"回滚目标      : {plan.target_release_id}")
    print(f"当前 head     : {plan.head_release_id or '（无）'}")
    print(f"拟新增回滚号  : {plan.new_rollback_release_id}")
    print(f"将变旧的发布  : {', '.join(plan.stale_release_ids) or '（无）'}")
    print("会变旧的下游引用：")
    if not plan.stale_consumers:
        print("  （无已登记消费者）")
    for c in plan.stale_consumers:
        print(f"  - {c['name']} 当前引用 {c['release_id']}（{c.get('note', '')}）")
    print(f"\n受影响文件数  : {plan.files_affected}")
    for d in plan.diffs:
        label = DIFF_CN.get(d["kind"], d["kind"])
        if d["kind"] == DiffKind.MOVED:
            print(f"  [{label}] {d['from_path']} -> {d['to_path']}")
        else:
            print(f"  [{label}] {d['path']}")
    print(f"\n{plan.note}")
    return 0


def cmd_rollback_commit(args) -> int:
    reg = _open_store(args)
    try:
        rec = reg.rollback_commit(args.target)
    except DataRegError as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 2
    if args.json:
        _print_json(rec)
        return 0
    print(f"[OK] 已新增回滚版本 {rec['release_id']}（回滚自 {rec['rollback_of']}）")
    print(f"  head 已推进至 {rec['release_id']}；历史发布与产物全部保留，未删除任何文件")
    return 0


def cmd_consumer_register(args) -> int:
    reg = _open_store(args)
    rec = reg.register_consumer(args.name, args.release_id, args.note or "")
    if args.json:
        _print_json(rec)
    else:
        print(f"[OK] 消费者 {rec['name']} 已登记引用 {rec['release_id']}")
    return 0


def cmd_consumer_list(args) -> int:
    reg = _open_store(args)
    consumers = reg.list_consumers()
    if args.json:
        _print_json(consumers)
        return 0
    if not consumers:
        print("（暂无下游引用登记）")
    for c in consumers:
        print(f"  {c['name']:<16} -> {c['release_id']}   {c.get('note', '')}")
    return 0


def cmd_events(args) -> int:
    reg = _open_store(args)
    events = reg.read_events()
    if args.json:
        _print_json(events)
        return 0
    for ev in events:
        print(f"#{ev['seq']:<4} {ev['ts']}  {ev['type']:<22} "
              f"{json.dumps(ev['payload'], ensure_ascii=False)}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="datareg", description="数据集发布登记台")
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("init", help="初始化登记台")
    sp.add_argument("--store", required=True)
    sp.set_defaults(func=cmd_init)

    sp = sub.add_parser("publish", help="登记一次发布")
    sp.add_argument("--store", required=True)
    sp.add_argument("--source", required=True, help="输入文件源目录")
    sp.add_argument("--transform", required=True, help="转换代码版本，如 transform@git:abc123")
    sp.add_argument("--partition-range", default="", help="分区范围 JSON，如 '{\"dt\":\"2026-11-13\"}'")
    sp.add_argument("--expected", default=None, help="期望指纹清单 JSON 文件（缺失/不一致即待检查）")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_publish)

    sp = sub.add_parser("list", help="发布时间线")
    sp.add_argument("--store", required=True)
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_list)

    sp = sub.add_parser("show", help="查看单个发布登记详情")
    sp.add_argument("--store", required=True)
    sp.add_argument("release_id")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_show)

    sp = sub.add_parser("verify", help="重新校验发布的源指纹与产物")
    sp.add_argument("--store", required=True)
    sp.add_argument("release_id")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_verify)

    sp = sub.add_parser("diff", help="两个发布的文件级差异")
    sp.add_argument("--store", required=True)
    sp.add_argument("old")
    sp.add_argument("new")
    sp.add_argument("--partition", default=None, help="只比较指定分区，如 dt=2026-11-13")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_diff)

    rb = sub.add_parser("rollback", help="回滚（先预览后执行）")
    rbsub = rb.add_subparsers(dest="rb_command", required=True)
    rbp = rbsub.add_parser("preview", help="预览影响范围（模拟，不删文件）")
    rbp.add_argument("--store", required=True)
    rbp.add_argument("--target", required=True)
    rbp.add_argument("--json", action="store_true")
    rbp.set_defaults(func=cmd_rollback_preview)
    rbc = rbsub.add_parser("commit", help="执行回滚（新增回滚版本，保留时间线）")
    rbc.add_argument("--store", required=True)
    rbc.add_argument("--target", required=True)
    rbc.add_argument("--json", action="store_true")
    rbc.set_defaults(func=cmd_rollback_commit)

    cs = sub.add_parser("consumer", help="下游引用登记")
    cssub = cs.add_subparsers(dest="cs_command", required=True)
    cr = cssub.add_parser("register", help="登记消费者引用的发布号")
    cr.add_argument("--store", required=True)
    cr.add_argument("--name", required=True)
    cr.add_argument("--release-id", required=True)
    cr.add_argument("--note", default="")
    cr.add_argument("--json", action="store_true")
    cr.set_defaults(func=cmd_consumer_register)
    cl = cssub.add_parser("list", help="列出下游引用")
    cl.add_argument("--store", required=True)
    cl.add_argument("--json", action="store_true")
    cl.set_defaults(func=cmd_consumer_list)

    sp = sub.add_parser("events", help="查看 append-only 事件日志")
    sp.add_argument("--store", required=True)
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_events)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except DataRegError as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 2
    except FileNotFoundError as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
