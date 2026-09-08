"""协议契约测试：只注入内存 SDK stub，不发送请求、不读取 .env。"""

import copy
import json
from types import SimpleNamespace

import pytest

from mini_agent_harness.core.types import ModelResponse
from mini_agent_harness.models import (
    AnthropicProvider,
    ContextOverflowError,
    ModelProtocolError,
    OpenAIProvider,
    RetryProvider,
)


class Endpoint:
    def __init__(self, response):
        self.response = response
        self.requests = []

    def create(self, **kwargs):
        self.requests.append(copy.deepcopy(kwargs))
        return copy.deepcopy(self.response)


def openai_client(response, mode="responses"):
    endpoint = Endpoint(response)
    client = SimpleNamespace(responses=endpoint) if mode == "responses" else SimpleNamespace(
        chat=SimpleNamespace(completions=endpoint))
    return client, endpoint


TOOLS = [{"name": "lookup", "description": "Lookup data", "input_schema": {
    "type": "object", "properties": {"query": {"type": "string"}}, "required": []}}]
PARAMS = {"system": "Test", "tools": TOOLS, "max_tokens": 500}


def test_responses_preserves_reasoning_and_native_call_without_duplication():
    output = [
        {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "opaque"},
        {"type": "function_call", "id": "fc_1", "call_id": "call_1", "name": "lookup",
         "arguments": '{"query":"abc"}', "status": "completed"},
    ]
    client, endpoint = openai_client({"status": "completed", "output": output,
                                     "usage": {"input_tokens": 5, "output_tokens": 6}})
    provider = OpenAIProvider("model", client=client)
    history = [{"role": "user", "content": "find abc"}]
    response = provider.generate(history, **PARAMS)
    assert response.stop_reason == "tool_use"
    assert response.usage == {"input_tokens": 5, "output_tokens": 6}
    assert [b for b in response.content if b["type"] == "tool_use"] == [
        {"type": "tool_use", "id": "call_1", "name": "lookup", "input": {"query": "abc"}}]
    history += [{"role": "assistant", "content": response.content}, {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "call_1", "content": "found"}]}]
    provider.generate(history, **PARAMS)
    request = endpoint.requests[-1]
    assert request["input"][1:3] == output
    assert len(request["input"]) == 4
    assert request["input"][-1] == {"type": "function_call_output", "call_id": "call_1", "output": "found"}
    assert request["include"] == ["reasoning.encrypted_content"]
    assert request["store"] is False
    assert request["tools"][0]["strict"] is False
    assert request["tools"][0]["parameters"]["required"] == []


def test_responses_foreign_native_blocks_are_not_sent():
    client, endpoint = openai_client({"output": [{"type": "message", "content": [
        {"type": "output_text", "text": "done"}]}]})
    provider = OpenAIProvider("fallback-model", client=client)
    response = provider.generate([{"role": "assistant", "content": [
        {"type": "openai_response_item", "source": "openai:official:primary-model",
         "item": {"type": "reasoning", "encrypted_content": "private"}},
        {"type": "tool_use", "id": "call_1", "name": "lookup", "input": {}},
    ]}], **PARAMS)
    assert response.text == "done"
    assert endpoint.requests[0]["input"] == [
        {"type": "function_call", "call_id": "call_1", "name": "lookup", "arguments": "{}"}]


def test_anthropic_preserves_thinking_and_filters_foreign_protocol():
    output = [{"type": "thinking", "thinking": "internal", "signature": "signed"},
              {"type": "tool_use", "id": "toolu_1", "name": "lookup", "input": {}}]
    endpoint = Endpoint({"content": output, "stop_reason": "tool_use"})
    provider = AnthropicProvider("claude", client=SimpleNamespace(messages=endpoint))
    response = provider.generate([{"role": "user", "content": "hi"}], **PARAMS)
    history = [{"role": "assistant", "content": response.content + [
        {"type": "openai_response_item", "source": "foreign", "item": {"type": "reasoning"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "ok"}]},
        {"role": "user", "content": "continue"}]
    provider.generate(history, **PARAMS)
    sent = endpoint.requests[-1]["messages"]
    assert sent[0]["content"] == output
    assert len(sent) == 2
    assert sent[1]["content"][-1] == {"type": "text", "text": "continue"}


def test_chat_multiple_tool_results_and_notification():
    client, endpoint = openai_client({"choices": [{"message": {"content": "done"}, "finish_reason": "stop"}]},
                                     "chat_completions")
    provider = OpenAIProvider("gateway-model", client=client, api_mode="chat_completions")
    history = [{"role": "assistant", "content": [
        {"type": "tool_use", "id": "a", "name": "lookup", "input": {}},
        {"type": "tool_use", "id": "b", "name": "lookup", "input": {"query": "b"}},
        {"type": "openai_response_item", "source": "foreign", "item": {"type": "reasoning"}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "a", "content": "A"},
            {"type": "tool_result", "tool_use_id": "b", "content": "B"},
            {"type": "text", "text": "background done"}]}]
    provider.generate(history, **PARAMS)
    sent = endpoint.requests[0]["messages"]
    assert [m["role"] for m in sent] == ["system", "assistant", "tool", "tool", "user"]
    assert len(sent[1]["tool_calls"]) == 2
    assert sent[-1]["content"] == "background done"


@pytest.mark.parametrize("arguments", ["{bad", "[]", "null"])
def test_invalid_tool_json_never_defaults_to_empty_dict(arguments):
    client, _ = openai_client({"output": [{"type": "function_call", "call_id": "c",
                                          "name": "lookup", "arguments": arguments}]})
    with pytest.raises(ModelProtocolError):
        OpenAIProvider("model", client=client).generate([], **PARAMS)


class ServiceError(Exception):
    def __init__(self, status_code, message="failed"):
        super().__init__(message)
        self.status_code = status_code


class SequenceProvider:
    def __init__(self, responses):
        self.responses, self.calls, self.closed = list(responses), 0, 0

    def generate(self, *args, **kwargs):
        self.calls += 1
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    def close(self):
        self.closed += 1


def test_retry_then_fallback_is_bounded():
    primary = SequenceProvider([ServiceError(429), ServiceError(529)])
    fallback = SequenceProvider([ConnectionError("offline"), ModelResponse([])])
    delays = []
    provider = RetryProvider(primary, fallback, max_retries=1, sleep=delays.append)
    assert provider.generate([], **PARAMS) == ModelResponse([])
    assert primary.calls == fallback.calls == 2
    assert len(delays) == 2
    provider.close()
    assert primary.closed == fallback.closed == 1


def test_context_overflow_does_not_retry_or_fallback():
    primary = SequenceProvider([ServiceError(400, "context_length_exceeded")])
    fallback = SequenceProvider([ModelResponse([])])
    with pytest.raises(ContextOverflowError):
        RetryProvider(primary, fallback, sleep=lambda _: pytest.fail("should not wait")).generate([], **PARAMS)
    assert primary.calls == 1 and fallback.calls == 0


def test_authentication_failure_is_not_retried():
    primary = SequenceProvider([ServiceError(401)])
    with pytest.raises(ServiceError):
        RetryProvider(primary, sleep=lambda _: pytest.fail("should not wait")).generate([], **PARAMS)
    assert primary.calls == 1


def test_provider_does_not_mutate_input_history():
    client, _ = openai_client({"output": []})
    provider = OpenAIProvider("model", client=client)
    history = [{"role": "user", "content": [{"type": "text", "text": "original"}]}]
    before = json.dumps(history)
    provider.generate(history, **PARAMS)
    assert json.dumps(history) == before


@pytest.mark.parametrize("mode", ["responses", "chat_completions", "anthropic"])
def test_truncated_arguments_return_signal_without_incomplete_tool_items(mode):
    if mode == "anthropic":
        endpoint = Endpoint({"stop_reason": "max_tokens", "content": [
            {"type": "tool_use", "id": "a", "name": "lookup", "input": '{"query":'}]})
        provider = AnthropicProvider("claude", client=SimpleNamespace(messages=endpoint))
    elif mode == "responses":
        client, _ = openai_client({"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"},
                                   "output": [{"type": "function_call", "call_id": "a", "name": "lookup",
                                               "arguments": '{"query":'}]})
        provider = OpenAIProvider("model", client=client)
    else:
        client, _ = openai_client({"choices": [{"finish_reason": "length", "message": {
            "tool_calls": [{"id": "a", "function": {"name": "lookup", "arguments": '{"query":'}}]}}]}, mode)
        provider = OpenAIProvider("model", client=client, api_mode=mode)
    response = provider.generate([], **PARAMS)
    assert response.stop_reason == "max_tokens"
    assert response.content == []
