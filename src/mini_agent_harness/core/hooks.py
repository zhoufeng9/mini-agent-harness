"""扩展点围绕循环存在；插件不需要改写主循环。

PreToolUse 回调返回非空字符串即拒绝。前置回调异常也拒绝执行；
其他事件异常记日志，不将一次成功执行误报为未执行并诱发重试。
"""

from __future__ import annotations

import logging
from collections import defaultdict
from threading import RLock
from typing import Callable

logger = logging.getLogger(__name__)


class HookRegistry:
    def __init__(self) -> None:
        self._callbacks: dict[str, list[Callable]] = defaultdict(list)
        self._lock = RLock()

    def register(self, event: str, callback: Callable) -> None:
        with self._lock:
            self._callbacks[event].append(callback)

    def emit(self, event: str, **payload) -> str | None:
        with self._lock:
            callbacks = list(self._callbacks[event])
        for callback in callbacks:
            try:
                result = callback(**payload)
                if event == "PreToolUse" and result:
                    return str(result)
            except Exception as exc:
                if event == "PreToolUse":
                    return f"Permission hook failed: {type(exc).__name__}"
                logger.warning("Hook %s failed: %s", event, type(exc).__name__)
        return None
