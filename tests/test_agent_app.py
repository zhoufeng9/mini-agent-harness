"""验证真实装配后的行为；脚本模型让测试无需密钥、联网或付费调用。"""

from dataclasses import replace

import pytest

from mini_agent_harness.app import Harness
from mini_agent_harness.config import Settings
from mini_agent_harness.core.types import ModelResponse
from mini_agent_harness.models import ContextOverflowError


class ScriptedProvider:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []
        self.closed = False

    def generate(self, messages, **kwargs):
        self.requests.append((list(messages), kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def close(self):
        self.closed = True


def tool(name, args, ident="one"):
    return {"type": "tool_use", "id": ident, "name": name, "input": args}


def done(text="done"):
    return ModelResponse([{"type": "text", "text": text}])


def settings(root):
    return Settings(workspace=root, model="test", memory_enabled=False, max_steps=8)


def test_complete_file_tool_roundtrip_and_transcript(tmp_path):
    provider = ScriptedProvider(ModelResponse([
        tool("write_file", {"path": "hello.txt", "content": "你好"}),
        tool("read_file", {"path": "hello.txt"}, "two")]), done("已完成"))
    with Harness(settings(tmp_path), provider=provider) as app:
        result = app.run("创建并读取 hello.txt")
        assert result.text == "已完成"
        assert (tmp_path / "hello.txt").read_text() == "你好"
        results = result.messages[2]["content"]
        assert [item["tool_use_id"] for item in results] == ["one", "two"]
        assert not any(item["is_error"] for item in results)
        assert "你好" in results[1]["content"]
        assert list((tmp_path / ".harness/sessions").glob("*.json"))
    assert provider.closed


def test_denied_shell_and_invalid_arguments_do_not_execute(tmp_path):
    provider = ScriptedProvider(ModelResponse([
        tool("bash", {"command": "touch should-not-exist"}),
        tool("write_file", {"path": "bad"}, "two"),
        tool("missing_tool", {}, "three")]), done())
    with Harness(settings(tmp_path), provider=provider) as app:
        result = app.run("check")
        assert all(item["is_error"] for item in result.messages[2]["content"])
    assert not (tmp_path / "should-not-exist").exists()
    assert not (tmp_path / "bad").exists()


def test_foreground_approval_hooks_and_background_completion(tmp_path):
    provider = ScriptedProvider(ModelResponse([tool("bash", {
        "command": "printf hello", "run_in_background": True})]), done(), done("background received"))
    approvals = []
    with Harness(settings(tmp_path), provider=provider,
                 approve=lambda prompt: approvals.append(prompt) or True) as app:
        calls = []
        app.hooks.register("PostToolUse", lambda **p: calls.append(p["spec"].name))
        app.run("run", interactive=True)
        assert approvals and calls == ["bash"]
        app.background.close()
        # 可能在同一轮第二次模型请求前注入，也可能在随后 poll 中注入。
        if app.events.wait("lead", timeout=0):
            assert app.poll().text == "background received"
        assert "background_complete" in str(app.history)


def test_hook_failure_blocks_execution(tmp_path):
    provider = ScriptedProvider(ModelResponse([tool("write_file", {"path": "x", "content": "x"})]), done())
    with Harness(settings(tmp_path), provider=provider) as app:
        def broken(**kwargs):
            raise RuntimeError("broken")
        app.hooks.register("PreToolUse", broken)
        result = app.run("try")
        assert result.messages[2]["content"][0]["is_error"]
        assert not (tmp_path / "x").exists()


def test_truncated_tool_is_never_executed_and_gets_error_result(tmp_path):
    cut = ModelResponse([tool("write_file", {"path": "x", "content": "bad"})], "max_tokens")
    provider = ScriptedProvider(cut, cut, done("recovered"))
    with Harness(settings(tmp_path), provider=provider) as app:
        result = app.run("try")
        assert result.text == "recovered"
        assert not (tmp_path / "x").exists()
        assert provider.requests[1][1]["max_tokens"] == 16000
        assert result.messages[2]["content"][0]["is_error"]


def test_step_budget_and_close_are_explicit(tmp_path):
    provider = ScriptedProvider(ModelResponse([tool("glob", {"pattern": "*"})]))
    app = Harness(replace(settings(tmp_path), max_steps=1), provider=provider)
    assert app.run("loop").status == "step_limit"
    app.close()
    app.close()
    with pytest.raises(RuntimeError, match="closed"):
        app.run("again")


def test_isolated_subagent_uses_same_loop_without_sharing_history(tmp_path):
    provider = ScriptedProvider(
        ModelResponse([tool("task", {"description": "inspect separately"})]),
        done("child answer"), done("parent answer"))
    with Harness(settings(tmp_path), provider=provider) as app:
        result = app.run("parent request")
        assert result.text == "parent answer"
        assert provider.requests[1][0] == [{"role": "user", "content": "inspect separately"}]
        assert "child answer" == result.messages[2]["content"][0]["content"]


def test_context_overflow_retries_once(tmp_path):
    provider = ScriptedProvider(ContextOverflowError("overflow"), done("recovered"))
    with Harness(settings(tmp_path), provider=provider) as app:
        # 避免摘要调用消费脚本模型响应，本测试聚焦循环的恢复分支。
        app.context.reactive_compact = lambda messages, request: messages
        assert app.run("try").text == "recovered"
        assert len(provider.requests) == 2


def test_constructor_failure_closes_existing_resources(tmp_path):
    (tmp_path / "mcp.json").write_text("invalid json")
    provider = ScriptedProvider()
    with pytest.raises(ValueError):
        Harness(settings(tmp_path), provider=provider)
    assert provider.closed


def test_runtime_event_includes_identity_for_plan_approval(tmp_path):
    provider = ScriptedProvider(done())
    with Harness(settings(tmp_path), provider=provider) as app:
        app.events.publish("lead", "plan_approval_request", "inspect code", {"request_id": "plan-123"})
        app.poll()
        assert "plan-123" in provider.requests[0][0][0]["content"]
        assert provider.requests[0][0][0]["origin"] == "runtime"


def test_state_subdirectory_symlink_rejected_before_provider_creation(tmp_path):
    root = tmp_path / "workspace"
    (root / ".harness").mkdir(parents=True)
    (root / ".harness/sessions").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError, match="escapes"):
        Harness(settings(root), provider=ScriptedProvider())
