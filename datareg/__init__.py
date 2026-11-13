"""datareg —— 数据集发布登记台

核心能力：
- 每次发布登记输入文件指纹、分区范围、转换代码版本、产物位置
- 新旧发布按文件 / 分区比较，内容未变则复用，变更可追溯输入与处理版本
- 文件缺失 / 指纹不一致 -> 停在 PENDING_CHECK；发布号内容不同禁止覆盖
- 两阶段快照清单，打包期间源文件变化即终止，不混入不同快照
- 回滚先预览影响范围（只模拟状态、不删文件），执行后新增回滚版本并保留完整时间线
"""

from .errors import (
    DataRegError,
    ReleaseNotFound,
    SnapshotConflict,
    StoreCorrupted,
)
from .manifest import FileEntry, Manifest, diff_manifests, make_manifest
from .registry import Registry, RollbackPlan

__all__ = [
    "DataRegError",
    "ReleaseNotFound",
    "SnapshotConflict",
    "StoreCorrupted",
    "FileEntry",
    "Manifest",
    "diff_manifests",
    "make_manifest",
    "Registry",
    "RollbackPlan",
]

__version__ = "1.0.0"
