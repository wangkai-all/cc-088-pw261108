"""状态与事件常量。"""


class ReleaseStatus:
    PENDING_CHECK = "PENDING_CHECK"   # 待检查：缺失 / 指纹不一致 / 快照漂移
    PUBLISHED = "PUBLISHED"           # 已发布
    ROLLED_BACK = "ROLLED_BACK"       # 历史发布被回滚（时间线保留，不删除）


class EventType:
    PUBLISH_REQUESTED = "PUBLISH_REQUESTED"
    CHECK_FAILED = "CHECK_FAILED"
    PACKAGE_ABORTED = "PACKAGE_ABORTED"
    PUBLISHED = "PUBLISHED"
    VERIFY_DRIFT = "VERIFY_DRIFT"
    ROLLBACK_PREVIEW = "ROLLBACK_PREVIEW"
    ROLLBACK_COMMITTED = "ROLLBACK_COMMITTED"
    CONSUMER_REGISTERED = "CONSUMER_REGISTERED"


class DiffKind:
    """文件级差异分类。"""
    ADDED = "ADDED"                # 新发布新增
    REMOVED = "REMOVED"            # 新发布删除
    CONTENT_CHANGED = "CONTENT_CHANGED"      # 内容变化（指纹不同）
    METADATA_CHANGED = "METADATA_CHANGED"    # 仅元数据变化（mtime/权限）
    MOVED = "MOVED"                # 移动文件：指纹相同、相对路径变化
    UNCHANGED = "UNCHANGED"        # 完全未变（默认不展示）


CHUNK_SIZE = 1 << 20  # 1 MiB 分块哈希
