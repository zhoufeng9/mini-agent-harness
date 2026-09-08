"""单元测试加真实本地 MCP 集成测试：不需要 API 密钥或外部服务器。"""

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from mini_agent_harness.core.tools import ToolRegistry
from mini_agent_harness.core.types import ToolSpec, object_schema
from mini_agent_harness.mcp import MCPManager, load_config
from mini_agent_harness.mcp.client import list_all_tools, tool_name

SERVER = Path(__file__).resolve().parents[1] / "examples" / "mcp_server.py"


def config_file(tmp_path, servers):
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")
    return path


def stdio_config(**extra):
    return {"command": sys.executable, "args": [str(SERVER)], "timeout": 5, **extra}


def test_explicit_env_expansion_and_secret_metadata_redaction(tmp_path):
    path = config_file(tmp_path, {"remote": {
        "transport": "streamable-http", "url": "https://example.com/mcp",
        "headers": {"Authorization": "Bearer ${MCP_TEST_TOKEN}"},
    }})
    config = load_config(path, env={"MCP_TEST_TOKEN": "private-example-value"})["remote"]
    assert config.headers["Authorization"] == "Bearer private-example-value"
    assert "private-example-value" not in repr(config)
    manager = MCPManager(path, ToolRegistry(), env={"MCP_TEST_TOKEN": "private-example-value"})
    assert "private-example-value" not in json.dumps(manager.list_servers())
    assert "headers" not in json.dumps(manager.list_servers())
    manager.close()
    with pytest.raises(ValueError, match="MCP_TEST_TOKEN"):
        load_config(path, env={})


def test_tool_name_normalization_and_maximum_length():
    assert tool_name("a.b", "read/file") == "mcp__a_b__read_file"
    with pytest.raises(ValueError, match="64"):
        tool_name("a" * 60, "b")


def test_normalized_remote_names_cannot_collide(tmp_path, monkeypatch):
    class FakeConnection:
        def __init__(self, config):
            self.closed = False

        def tools(self):
            return [SimpleNamespace(name=name, description="demo", inputSchema=object_schema())
                    for name in ("read/file", "read.file")]

        def close(self):
            self.closed = True

    monkeypatch.setattr("mini_agent_harness.mcp.client._Connection", FakeConnection)
    registry = ToolRegistry()
    manager = MCPManager(config_file(tmp_path, {"demo": stdio_config()}), registry)
    with pytest.raises(ValueError, match="冲突"):
        manager.connect("demo")
    assert not manager.list_servers()[0]["connected"]
    with pytest.raises(KeyError):
        registry.get("mcp__demo__read_file")
    manager.close()


def test_pagination_and_repeated_cursor():
    class Session:
        async def list_tools(self, cursor=None):
            if cursor is None:
                return SimpleNamespace(tools=["first"], nextCursor="page2")
            assert cursor == "page2"
            return SimpleNamespace(tools=["second"], nextCursor=None)

    assert asyncio.run(list_all_tools(Session())) == ["first", "second"]

    class BrokenSession:
        async def list_tools(self, cursor=None):
            return SimpleNamespace(tools=[], nextCursor="repeat")

    with pytest.raises(ValueError, match="cursor"):
        asyncio.run(list_all_tools(BrokenSession()))


def test_registration_conflict_rolls_back_without_overwriting(tmp_path):
    registry = ToolRegistry()
    registry.register(ToolSpec("mcp__demo__add", "original", object_schema(), lambda c, a: "old"))
    manager = MCPManager(config_file(tmp_path, {"demo": stdio_config()}), registry)
    try:
        with pytest.raises(ValueError, match="冲突"):
            manager.connect("demo")
        assert registry.get("mcp__demo__add").description == "original"
        assert not manager.list_servers()[0]["connected"]
    finally:
        manager.close()


def test_real_stdio_discovery_errors_and_clean_shutdown(tmp_path):
    registry = ToolRegistry()
    path = config_file(tmp_path, {"demo": stdio_config(read_only_tools=["add", "echo"])})
    manager = MCPManager(path, registry)
    manager.register_tools()
    assert registry.get("connect_mcp").requires_approval
    try:
        names = manager.connect("demo")
        assert "mcp__demo__add" in names
        assert not registry.get("mcp__demo__add").requires_approval
        # 即使远程自报 readOnlyHint=True，宿主白名单未列出仍要审批。
        assert registry.get("mcp__demo__fail").requires_approval
        assert "7" in manager.call("demo", "add", {"a": 3, "b": 4})
        assert "中文" in manager.call("demo", "echo", {"text": "中文"})
        assert manager.call("demo", "fail", {}).startswith("Error:")
        assert manager.connect("demo") == names
        connection = manager._connections["demo"]
    finally:
        manager.close()
    assert not connection._thread.is_alive()
    assert connection._failure is None  # 特别检测 AnyIO 跨 Task 退出错误。
    with pytest.raises(KeyError):
        registry.get("mcp__demo__add")
    manager.close()


def test_stdio_timeout_leaves_connection_usable(tmp_path):
    manager = MCPManager(config_file(tmp_path, {"demo": stdio_config(timeout=2)}), ToolRegistry())
    try:
        manager.connect("demo")
        assert manager.call("demo", "wait_seconds", {"seconds": 5}).startswith("Error:")
        assert "after" in manager.call("demo", "echo", {"text": "after"})
    finally:
        manager.close()


def test_real_streamable_http(tmp_path):
    # 只监听本机；选择临时空闲端口以避免占用用户正在使用的服务端口。
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    with open(os.devnull, "w") as sink:
        process = subprocess.Popen([sys.executable, str(SERVER), "--transport", "streamable-http",
                                    "--port", str(port)], stdout=sink, stderr=sink)
        manager = None
        try:
            deadline = time.monotonic() + 10
            while True:
                if process.poll() is not None:
                    pytest.fail("本地 HTTP MCP 示例进程启动失败")
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                        break
                except OSError:
                    if time.monotonic() > deadline:
                        pytest.fail("本地 HTTP MCP 示例未及时启动")
                    time.sleep(0.05)
            path = config_file(tmp_path, {"local": {"transport": "streamable-http",
                "url": f"http://127.0.0.1:{port}/mcp", "timeout": 5,
                "read_only_tools": ["add"]}})
            registry = ToolRegistry()
            manager = MCPManager(path, registry)
            assert "mcp__local__add" in manager.connect("local")
            assert "42" in manager.call("local", "add", {"a": 20, "b": 22})
            connection = manager._connections["local"]
            manager.close()
            assert not connection._thread.is_alive()
            assert connection._failure is None
        finally:
            if manager is not None:
                manager.close()
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
