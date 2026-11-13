# datareg —— 数据集发布登记台

纯 Python 标准库实现（无第三方依赖），为数据集发布提供可追溯、可复用、可回滚的
本地登记能力。

## 能力对照

| 需求 | 实现 |
| --- | --- |
| 每次记录输入文件指纹、分区范围、转换代码版本、产物位置 | 发布记录含 `content_signature`、逐文件 SHA-256 清单、`partition_range`、`transform_version`、`artifact_uri` |
| 新旧发布按文件和分区比较 | `diff` 命令 / `Registry.compare`，支持 `--partition` 分区过滤 |
| 内容未变时复用 | 内容签名 + 转换版本 + 分区三者一致即复用，不产生新版本、不推进 head |
| 变更可追到输入和处理版本 | 发布记录保存源目录、完整清单与转换版本；append-only 事件日志串联全过程 |
| 文件缺失 / 指纹不一致停在待检查 | 发布进入 `PENDING_CHECK`，记录差异明细，head 不推进、不产物化 |
| 同一发布号内容不同禁止覆盖 | 发布记录写一次（write-once），冲突抛 `ReleaseExistsConflict` |
| 回滚先选目标并预览影响范围 | `rollback preview` 纯模拟，列出会变旧的发布与下游引用、受影响文件 |
| 只模拟状态，不删文件 | 预览零产物写入；回滚执行也只新增版本，历史产物全部保留 |
| 稳定相对路径顺序的清单摘要 | 清单条目按 POSIX 相对路径字典序排列，内容签名基于规范化 JSON |
| 区分移动 / 内容变化 / 元数据变化 | 五类差异：`ADDED` / `REMOVED` / `CONTENT_CHANGED` / `METADATA_CHANGED` / `MOVED` |
| 打包期间源变化即终止、不混快照 | 两阶段清单 + 逐文件校验式拷贝，漂移抛 `SnapshotConflict` 并记 `PACKAGE_ABORTED` |
| 回滚前列出会变旧的下游引用 | 消费者登记（`consumer register`），预览自动列出受影响引用 |
| 执行模拟后新增回滚版本、保留时间线 | 回滚生成全新发布号（`rollback_of` 指回目标），旧发布标记 `ROLLED_BACK` 但不删除 |

## 存储结构

```
<store>/
├── config.json            # 存储版本
├── events.log             # append-only 事件日志（审计链）
├── head.json              # 当前生效发布号
├── consumers.json         # 下游引用登记
├── objects/ab/<sha256>    # 内容寻址不可变对象（去重存储）
├── manifests/R000001.json # 每次发布的完整文件清单（含 mtime/权限/分区）
└── releases/
    ├── R000001.json       # 发布登记（指纹、分区、转换版本、产物位置…）
    └── R000001/           # 物化产物（硬链接到 objects，跨设备退化为拷贝）
```

## 快速开始

```bash
# 端到端演示（10 个场景，使用隔离模式，无需安装依赖）
python3 -I demo/demo.py

# 单元测试
python3 -I -m unittest discover -s tests -v

# 命令行
python3 -m datareg init --store ./demo-store
python3 -m datareg publish --store ./demo-store --source ./data \
    --transform 'transform@git:8f3a21c' \
    --partition-range '{"dt":"2026-11-13"}'
python3 -m datareg list --store ./demo-store
python3 -m datareg diff --store ./demo-store R000001 R000002 --partition dt=2026-11-13
python3 -m datareg consumer register --store ./demo-store \
    --name daily-report --release-id R000002 --note 日报任务
python3 -m datareg rollback preview --store ./demo-store --target R000001
python3 -m datareg rollback commit  --store ./demo-store --target R000001
python3 -m datareg events --store ./demo-store
```

期望指纹清单（`--expected`）格式：

```json
[
  {"rel_path": "dt=2026-11-13/a.csv", "sha256": "…"},
  {"rel_path": "dt=2026-11-13/b.csv", "sha256": "…"}
]
```

出现 `MISSING_FILE` / `FINGERPRINT_MISMATCH` 时发布停在 `PENDING_CHECK`；
`UNEXPECTED_FILE` 仅警告不阻塞。

## 关键设计

### 两阶段快照，杜绝混入不同快照

1. 扫描清单①（相对路径字典序、逐文件 SHA-256、mtime、权限、分区）
2. 期望指纹核对；通过后校验式拷贝到内容寻址存储（拷贝后立即复核哈希）
3. 再次扫描清单②，内容签名与清单①不一致即终止，记 `PACKAGE_ABORTED`
4. 快照确认稳定后才判定复用、物化产物、写入登记

复用判定刻意放在二次扫描之后：否则打包期间的源变化可能被“内容恰好可复用”
绕过（该竞态有专门回归测试 `test_hook_adding_new_file_must_not_fall_through_to_reuse`）。

### 文件级差异分类

- **移动（MOVED）**：内容指纹相同、相对路径变化，按 sha256 分组确定性配对
- **内容变化（CONTENT_CHANGED）**：同路径 SHA-256 不同
- **元数据变化（METADATA_CHANGED）**：指纹相同，仅 mtime / 权限 / 大小变化
- **新增 / 删除（ADDED / REMOVED）**：仅出现在一侧且无法配对为移动
- 输出严格按路径排序；内容签名不包含元数据，纯元数据变化不产生新内容版本

### 回滚语义

- `preview` 只计算并记录预览事件，不创建/修改/删除任何产物
- `commit` 用目标发布的不可变对象**新建**一个回滚发布并推进 head
- 目标之后的已发布发布在时间线中标记 `ROLLED_BACK`，登记与产物原样保留
- 引用了变旧发布的消费者在预览中列出，供下游主动跟进

## 作为库使用

```python
from datareg import Registry
from datareg.errors import SnapshotConflict

reg = Registry.create("./demo-store")
result = reg.publish(
    source_root="./data",
    transform_version="transform@git:8f3a21c",
    partition_range={"dt": "2026-11-13"},
)
print(result.release["content_signature"], result.reused)

plan = reg.rollback_preview("R000001")   # 模拟
record = reg.rollback_commit("R000001")  # 执行
```
