"""用一个线程安全的邮箱，把后台完成通知送回对应 Agent 的主循环。"""

from __future__ import annotations

from collections import defaultdict, deque
from copy import deepcopy
from dataclasses import dataclass, field
from threading import Condition
from typing import Any


@dataclass(frozen=True)
class Event:
    """事件是数据，不能在生产者线程直接改变模型的对话消息。"""

    kind: str
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)


class EventBus:
    """每个 recipient 拥有独立 FIFO 队列，Condition 同时解决互斥和唤醒。

    wait() 只等待，不取走事件；主循环在下一个安全点调用 drain()。
    因此不会出现“等待函数抢走通知，真正处理函数却看不见”的竞争。
    这只是内存邮箱，持久投递由 Scheduler 的 pending_delivery 状态负责。
    """

    def __init__(self) -> None:
        self._condition = Condition()
        self._mailboxes: dict[str, deque[Event]] = defaultdict(deque)

    def publish(self, recipient: str, kind: str, content: str,
                metadata: dict | None = None) -> None:
        # 拷贝 metadata，避免调用方后续修改字典影响已经入队的事件。
        event = Event(kind, content, deepcopy(metadata or {}))
        with self._condition:
            self._mailboxes[recipient].append(event)
            self._condition.notify_all()

    def drain(self, recipient: str) -> list[Event]:
        with self._condition:
            return list(self._mailboxes.pop(recipient, ()))

    def wait(self, recipient: str, timeout: float | None = None) -> bool:
        with self._condition:
            return self._condition.wait_for(
                lambda: bool(self._mailboxes.get(recipient)), timeout=timeout
            )
