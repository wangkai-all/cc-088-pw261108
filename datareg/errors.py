"""datareg 异常类型。"""


class DataRegError(Exception):
    """所有 datareg 业务异常的基类。"""


class ReleaseNotFound(DataRegError):
    """指定的发布号不存在。"""


class SnapshotConflict(DataRegError):
    """打包期间源快照发生变化，本次发布必须终止并重新生成清单。"""


class StoreCorrupted(DataRegError):
    """登记台存储结构或不可变产物损坏。"""


class ReleaseExistsConflict(DataRegError):
    """同一发布号下出现了不同内容（写一次语义被违反）。"""
