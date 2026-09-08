"""供队友控制消息使用的持久邮箱，与主循环通知 EventBus 分离。

每个收件人一份 JSON inbox；发送/消费共用文件锁，多个进程不会覆盖对方消息。
队友进程重启不会自动重建线程，但未消费消息仍保留，宿主可检查或恢复。
"""

from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from uuid import uuid4

from ..tasks.storage import FileMutex, write_json_atomic


def validate_name(name: str) -> str:
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name):
        raise ValueError("Agent 名称须为 1–64 位字母、数字、横线或下划线")
    return name


@dataclass
class Mail:
    sender: str
    recipient: str
    kind: str
    content: str
    metadata: dict = field(default_factory=dict)
    id: str = field(default_factory=lambda: uuid4().hex)


class FileMailbox:
    """Condition 用于本进程即时唤醒，短间隔文件检查兼容其他进程发送消息。"""

    def __init__(self, state_dir: Path):
        self.root = Path(state_dir).resolve() / "mailboxes"
        self._mutex = FileMutex(self.root / ".lock")
        self._condition = threading.Condition()

    def _path(self, recipient: str) -> Path:
        path = self.root / f"{validate_name(recipient)}.json"
        if self.root.resolve() != self.root or path.resolve() != path:
            raise ValueError("邮箱路径不能经过符号链接")
        return path

    def _read(self, path: Path) -> list[dict]:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []

    def send(self, sender: str, recipient: str, content: str,
             kind: str = "message", metadata: dict | None = None) -> Mail:
        validate_name(sender)
        path = self._path(recipient)
        mail = Mail(sender, recipient, kind, content, metadata or {})
        with self._mutex.hold():
            records = self._read(path)
            records.append(asdict(mail))
            write_json_atomic(path, records)
        self.wake()
        return mail

    def drain(self, recipient: str) -> list[Mail]:
        """一次事务读出并清空；协议消息在持锁外处理，避免锁的嵌套反转。"""
        path = self._path(recipient)
        with self._mutex.hold():
            records = self._read(path)
            if records:
                write_json_atomic(path, [])
        return [Mail(**record) for record in records]

    def pending(self, recipient: str) -> bool:
        path = self._path(recipient)
        with self._mutex.hold():
            return bool(self._read(path))

    def wait(self, recipient: str, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        with self._condition:
            while not self.pending(recipient):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(min(remaining, 0.2))
            return True

    def wake(self) -> None:
        with self._condition:
            self._condition.notify_all()
