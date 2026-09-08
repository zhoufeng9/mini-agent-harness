"""网络恢复策略与厂商转换分离；工具执行本身绝不放入此重试器。"""

from __future__ import annotations

import random
import time
from collections.abc import Callable

from mini_agent_harness.core.types import Message, ModelProvider, ModelResponse


class ContextOverflowError(RuntimeError):
    """提示 Agent Loop 保存历史并压缩一次，再发起新的模型请求。"""


def is_context_overflow(error: Exception) -> bool:
    body = str(getattr(error, "body", ""))
    text = (str(error) + " " + body).lower()
    return isinstance(error, ContextOverflowError) or any(marker in text for marker in (
        "context_length_exceeded", "prompt is too long", "prompt too long",
        "maximum context length", "exceeds the context window", "context window exceeded",
        "input is too long", "request_too_large"))


def _transient(error: Exception) -> bool:
    status = getattr(error, "status_code", None)
    if status in {408, 409, 429} or isinstance(status, int) and 500 <= status < 600:
        return True
    return isinstance(error, (ConnectionError, TimeoutError)) or type(error).__name__ in {
        "APIConnectionError", "APITimeoutError", "ConnectError", "ReadTimeout",
        "ConnectTimeout", "RemoteProtocolError"}


class RetryProvider:
    """每个 provider 最多 1 + max_retries 次；只在瞬态错误耗尽后尝试 fallback。

    401、schema 错误和上下文溢出不会被换模型掩盖。sleep 可注入，单测不会等待。
    不记住全局「当前 fallback」状态，因此多个 teammate 可安全共用本适配器。
    """

    def __init__(self, primary: ModelProvider, fallback: ModelProvider | None = None,
                 max_retries: int = 3, sleep: Callable[[float], None] = time.sleep):
        if max_retries < 0:
            raise ValueError("max_retries 不能为负数")
        self.primary, self.fallback = primary, fallback
        self.max_retries, self.sleep = max_retries, sleep

    def generate(self, messages: list[Message], *, system: str,
                 tools: list[dict], max_tokens: int) -> ModelResponse:
        providers = [self.primary] + ([self.fallback] if self.fallback else [])
        for provider_index, provider in enumerate(providers):
            for attempt in range(self.max_retries + 1):
                try:
                    return provider.generate(messages, system=system, tools=tools,
                                             max_tokens=max_tokens)
                except Exception as error:
                    if is_context_overflow(error):
                        raise ContextOverflowError("模型上下文超出限制，需要压缩历史") from error
                    if not _transient(error):
                        raise
                    if attempt == self.max_retries:
                        if provider_index == len(providers) - 1:
                            raise
                        break
                    delay = min(30.0, 2 ** attempt + random.uniform(0, 0.25))
                    headers = getattr(getattr(error, "response", None), "headers", {})
                    try:
                        delay = min(30.0, max(delay, float(headers.get("retry-after", 0))))
                    except (TypeError, ValueError):
                        pass
                    self.sleep(delay)
        raise RuntimeError("没有可用的模型服务")  # 构造器始终要求 primary，正常不可达。

    def close(self) -> None:
        seen = set()
        for provider in (self.primary, self.fallback):
            if provider is not None and id(provider) not in seen:
                seen.add(id(provider))
                provider.close()
