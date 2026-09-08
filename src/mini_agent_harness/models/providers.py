"""把不同厂商协议转换为 core.types 中的消息协议。

Agent Loop 只关心 text、tool_use、tool_result。厂商专有的 reasoning / thinking
内容另外保存在带来源标记的块里：同协议下一轮原样回传，切换模型时不泄漏到
另一协议。这既保留推理模型工具调用的连续性，也避免核心代码依赖 SDK 类。
"""

from __future__ import annotations

import copy
import json
from typing import Any

from mini_agent_harness.core.types import Message, ModelResponse


class ModelProtocolError(RuntimeError):
    """服务端响应不能安全转换；尤其不能把损坏的工具参数默认为空对象。"""


def _plain(value: Any) -> Any:
    """SDK Pydantic 对象只在适配器边界出现，落盘之前全部转成 JSON 数据。"""
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", exclude_none=True)
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _blocks(message: Message) -> list[dict]:
    content = message.get("content", "")
    return [{"type": "text", "text": content}] if isinstance(content, str) else content


def _output_text(content: Any) -> str:
    return content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)


def _arguments(raw: str | dict) -> dict:
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except (ValueError, TypeError) as error:
        raise ModelProtocolError("模型返回了无效的工具参数 JSON；未执行工具") from error
    if not isinstance(value, dict):
        raise ModelProtocolError("模型工具参数必须是 JSON 对象；未执行工具")
    return value


def _usage(data: dict) -> dict[str, int]:
    return {key: value for key, value in (data.get("usage") or {}).items()
            if isinstance(value, int) and not isinstance(value, bool)}


class AnthropicProvider:
    """Anthropic Messages API；注入 client 可完全离线测试。

    SDK 自带重试关闭，由 RetryProvider 统一决定重试和 fallback，防止重试次数
    相乘。构造时才 import SDK，不会因为导入整个框架而读取密钥或建立客户端。
    """

    def __init__(self, model: str, api_key: str | None = None,
                 base_url: str | None = None, client: Any = None,
                 timeout: float = 120):
        self.model = model
        self.source = f"anthropic:{base_url or 'official'}:{model}"
        self._owns_client = client is None
        if client is None:
            from anthropic import Anthropic
            client = Anthropic(api_key=api_key, base_url=base_url,
                               timeout=timeout, max_retries=0)
        self.client = client

    def generate(self, messages: list[Message], *, system: str,
                 tools: list[dict], max_tokens: int) -> ModelResponse:
        converted = []
        for message in messages:
            blocks = _blocks(message)
            # 原生块包含 thinking signature、citations 等，必须整组按原顺序回传。
            native = [b["item"] for b in blocks
                      if b.get("type") == "anthropic_response_item"
                      and b.get("source") == self.source]
            content = native or [copy.deepcopy(b) for b in blocks
                                 if b.get("type") in {"text", "tool_use", "tool_result"}]
            if content:
                # 例如归档标记与用户输入可能相邻，合并以兼容严格的 Messages 网关。
                if converted and converted[-1]["role"] == message["role"]:
                    converted[-1]["content"].extend(content)
                else:
                    converted.append({"role": message["role"], "content": content})
        kwargs = dict(model=self.model, system=system, messages=converted,
                      max_tokens=max_tokens)
        if tools:
            kwargs["tools"] = tools
        data = _plain(self.client.messages.create(**kwargs))
        content = []
        truncated = data.get("stop_reason") == "max_tokens"
        for item in data.get("content", []):
            # 未完整生成的原生 tool/thinking 块不能成为下一轮请求的一部分。
            # 仅交付可见文本及截断信号，让 AgentLoop 用更大预算重试同一输入。
            if not truncated:
                content.append({"type": "anthropic_response_item", "source": self.source,
                                "item": item})
            if item.get("type") == "text":
                content.append({"type": "text", "text": item.get("text", "")})
            elif item.get("type") == "tool_use" and not truncated:
                content.append({"type": "tool_use", "id": item["id"],
                                "name": item["name"], "input": _arguments(item["input"])})
        return ModelResponse(content, data.get("stop_reason") or "end_turn", _usage(data))

    def close(self) -> None:
        if self._owns_client:
            self.client.close()


class OpenAIProvider:
    """官方默认 Responses；第三方网关可显式选 chat_completions。

    Responses 采用完整历史回传，不使用 previous_response_id。这样本地保存的
    历史就是唯一会话状态，恢复、压缩、fallback 都无需同步远端会话指针。
    """

    def __init__(self, model: str, api_key: str | None = None,
                 base_url: str | None = None, client: Any = None,
                 timeout: float = 120, api_mode: str = "responses"):
        if api_mode not in {"responses", "chat_completions"}:
            raise ValueError("api_mode 必须是 responses 或 chat_completions")
        self.model, self.api_mode = model, api_mode
        self.source = f"openai:{base_url or 'official'}:{model}"
        self._owns_client = client is None
        if client is None:
            from openai import OpenAI
            client = OpenAI(api_key=api_key, base_url=base_url,
                            timeout=timeout, max_retries=0)
        self.client = client

    def generate(self, messages: list[Message], *, system: str,
                 tools: list[dict], max_tokens: int) -> ModelResponse:
        if self.api_mode == "chat_completions":
            return self._chat(messages, system, tools, max_tokens)
        return self._responses(messages, system, tools, max_tokens)

    def _responses(self, messages: list[Message], system: str,
                   tools: list[dict], max_tokens: int) -> ModelResponse:
        items = []
        for message in messages:
            blocks = _blocks(message)
            native = [copy.deepcopy(b["item"]) for b in blocks
                      if b.get("type") == "openai_response_item"
                      and b.get("source") == self.source]
            if native:
                # 包括 reasoning 在内的所有输出都回传；不能只留下 function_call。
                items.extend(native)
                continue
            for block in blocks:
                kind = block.get("type")
                if kind == "text" and block.get("text"):
                    items.append({"role": message["role"], "content": block["text"]})
                elif kind == "tool_use":
                    items.append({"type": "function_call", "call_id": block["id"],
                                  "name": block["name"],
                                  "arguments": json.dumps(block["input"], ensure_ascii=False)})
                elif kind == "tool_result":
                    items.append({"type": "function_call_output",
                                  "call_id": block["tool_use_id"],
                                  "output": _output_text(block.get("content", ""))})
        # strict=False 保留原工具 schema 中 optional 参数的含义；否则 Responses
        # 会对 schema 做 strict 规范化，许多 MCP 工具的可选字段会意外变成必填。
        definitions = [{"type": "function", "name": tool["name"],
                        "description": tool.get("description", ""),
                        "parameters": tool["input_schema"], "strict": False}
                       for tool in tools]
        kwargs = dict(model=self.model, instructions=system, input=items,
                      max_output_tokens=max_tokens,
                      include=["reasoning.encrypted_content"], store=False)
        if definitions:
            kwargs["tools"] = definitions
        data = _plain(self.client.responses.create(**kwargs))
        content = []
        incomplete = data.get("status") == "incomplete"
        for item in data.get("output", []):
            if not incomplete:
                content.append({"type": "openai_response_item", "source": self.source,
                                "item": item})
            if item.get("type") == "message":
                for block in item.get("content", []):
                    if block.get("type") == "output_text":
                        content.append({"type": "text", "text": block.get("text", "")})
                    elif block.get("type") == "refusal":
                        content.append({"type": "text", "text": block.get("refusal", "")})
            elif item.get("type") == "function_call" and not incomplete:
                content.append({"type": "tool_use", "id": item["call_id"],
                                "name": item["name"],
                                "input": _arguments(item.get("arguments", ""))})
        if data.get("status") == "failed":
            raise ModelProtocolError("Responses 返回 failed 状态")
        stop = "tool_use" if any(b["type"] == "tool_use" for b in content) else "end_turn"
        if incomplete:
            reason = (data.get("incomplete_details") or {}).get("reason")
            stop = "end_turn" if reason == "content_filter" else "max_tokens"
        return ModelResponse(content, stop, _usage(data))

    def _chat(self, messages: list[Message], system: str,
              tools: list[dict], max_tokens: int) -> ModelResponse:
        converted = [{"role": "system", "content": system}]
        for message in messages:
            blocks = _blocks(message)
            texts = [b["text"] for b in blocks if b.get("type") == "text"]
            calls = [{"id": b["id"], "type": "function",
                      "function": {"name": b["name"],
                                   "arguments": json.dumps(b["input"], ensure_ascii=False)}}
                     for b in blocks if b.get("type") == "tool_use"]
            # 一个 user 消息可能同时包含多个结果和后台通知。Chat 协议需要把结果
            # 展开为连续的 role=tool 消息，再追加普通文本通知，不能丢失混合块。
            for block in blocks:
                if block.get("type") == "tool_result":
                    converted.append({"role": "tool", "tool_call_id": block["tool_use_id"],
                                      "content": _output_text(block.get("content", ""))})
            if texts or calls:
                item = {"role": message["role"], "content": "\n".join(texts) or None}
                if calls:
                    item["tool_calls"] = calls
                converted.append(item)
        definitions = [{"type": "function", "function": {
            "name": t["name"], "description": t.get("description", ""),
            "parameters": t["input_schema"]}} for t in tools]
        # 兼容网关通常接受 max_tokens；官方 Chat 推理模型使用更新的参数名。
        token_key = "max_completion_tokens" if self.source.startswith("openai:official:") else "max_tokens"
        kwargs = dict(model=self.model, messages=converted, **{token_key: max_tokens})
        if definitions:
            kwargs["tools"] = definitions
        data = _plain(self.client.chat.completions.create(**kwargs))
        if not data.get("choices"):
            raise ModelProtocolError("Chat Completions 响应缺少 choices")
        choice = data["choices"][0]
        message = choice.get("message", {})
        content = []
        if message.get("content") or message.get("refusal"):
            content.append({"type": "text", "text": message.get("content") or message["refusal"]})
        for call in (message.get("tool_calls") or []) if choice.get("finish_reason") != "length" else []:
            content.append({"type": "tool_use", "id": call["id"],
                            "name": call["function"]["name"],
                            "input": _arguments(call["function"]["arguments"])})
        stop = {"tool_calls": "tool_use", "length": "max_tokens"}.get(
            choice.get("finish_reason"), "end_turn")
        return ModelResponse(content, stop, _usage(data))

    def close(self) -> None:
        if self._owns_client:
            self.client.close()
