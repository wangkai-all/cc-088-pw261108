"""文件系统原子操作与哈希工具（仅标准库）。"""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from pathlib import Path

from .model import CHUNK_SIZE


def sha256_file(path: Path) -> str:
    """对文件内容做 SHA-256，分块读取以支持大文件。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            block = f.read(CHUNK_SIZE)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """同目录临时文件 + rename，保证 path 处只会出现完整内容。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


def hardlink_or_copy(src: Path, dst: Path) -> None:
    """优先硬链接复用 inode（不复制字节），跨文件系统时退化为拷贝。

    目标路径的父目录必须已存在；dst 不应已存在。
    """
    try:
        os.link(src, dst)
    except FileExistsError:
        raise
    except OSError:
        shutil.copy2(src, dst)


def copy_verified(src: Path, dst: Path, expected_sha256: str) -> None:
    """拷贝到临时文件 -> 校验指纹一致 -> 原子落位，防止半写入产物。"""
    dst.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".obj-", dir=dst.parent)
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        with open(src, "rb") as fsrc, open(tmp, "wb") as fdst:
            shutil.copyfileobj(fsrc, fdst, length=CHUNK_SIZE)
        actual = sha256_file(tmp)
        if actual != expected_sha256:
            raise SnapshotConflictWrite(
                f"打包过程中源文件内容变化：{src}（期望 {expected_sha256[:12]}…，"
                f"实际 {actual[:12]}…）"
            )
        os.replace(tmp, dst)
    except BaseException:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        raise


class SnapshotConflictWrite(Exception):
    """copy_verified 内部分类用，registry 层统一转成 SnapshotConflict。"""
