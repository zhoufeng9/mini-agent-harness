"""小型文件存储的同步原语；锁与临时文件都属于明确的存储实例。"""

from __future__ import annotations

import fcntl
import json
import os
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from uuid import uuid4


class FileMutex:
    """RLock 保护本进程线程，flock 保护操作同一文件的其他宿主进程。

    同一实例允许嵌套事务，最外层才获取/释放文件锁。此实现面向 macOS/Linux，
    不声称文件锁可以替代远程数据库或适用于任意网络文件系统。
    """

    def __init__(self, path: Path):
        self.path = path
        self._threads = threading.RLock()
        self._local = threading.local()

    @contextmanager
    def hold(self) -> Iterator[None]:
        with self._threads:
            depth = getattr(self._local, "depth", 0)
            if depth == 0:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                # 拒绝把锁文件链接到工作区以外；flock 必须锁定真实的本地文件。
                flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
                fd = os.open(self.path, flags, 0o600)
                self._local.handle = os.fdopen(fd, "a+")
                fcntl.flock(fd, fcntl.LOCK_EX)
            self._local.depth = depth + 1
            try:
                yield
            finally:
                self._local.depth -= 1
                if self._local.depth == 0:
                    handle = self._local.handle
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                    handle.close()
                    del self._local.handle


def write_json_atomic(path: Path, data: object) -> None:
    """先完整写入同目录临时文件再替换，读者不会看见半截 JSON。"""
    temp = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temp.open("x", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)
