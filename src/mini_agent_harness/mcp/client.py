"""同步 Agent 与异步 MCP SDK 的边界：每个连接由一个长期协程独占生命周期。"""

from __future__ import annotations

import asyncio
import json
import os
import re
from concurrent.futures import Future
from contextlib import AsyncExitStack
from datetime import timedelta
from pathlib import Path
from threading import RLock, Thread
from typing import Any

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client

from mini_agent_harness.core.types import ToolSpec, object_schema

from .config import ServerConfig, load_config


def tool_name(server: str, remote_name: str) -> str:
    """同时满足 Anthropic/OpenAI 常用工具名约束；碰撞由 manager 显式拒绝。

    不静默截断长名字，否则两个不同远程工具可能被路由到同一个本地入口。
    """
    def normalize(part: str) -> str:
        return re.sub(r"[^A-Za-z0-9_-]", "_", part)
    result = f"mcp__{normalize(server)}__{normalize(remote_name)}"
    if len(result) > 64:
        raise ValueError("MCP 工具名规范化后超过 64 字符，请缩短服务器名或远程工具名")
    return result


async def list_all_tools(session) -> list:
    """完整读取分页；重复 cursor 通常意味着服务器错误，避免无限请求。"""
    tools, cursor, seen = [], None, set()
    while True:
        page = await session.list_tools(cursor=cursor)
        tools.extend(page.tools)
        cursor = page.nextCursor
        if not cursor:
            return tools
        if cursor in seen:
            raise ValueError("MCP list_tools 返回重复分页 cursor")
        seen.add(cursor)


class _Connection:
    """私有连接 actor：sync 调用入队，唯一 _serve 协程接收请求。

    AnyIO 的 CancelScope/TaskGroup 必须在进入它的同一个 asyncio Task 中
    退出。不能 connect/close 各用一次 asyncio.run，也不能让多个独立
    run_coroutine_threadsafe 协程分别 __aenter__/__aexit__。这里 transport、
    ClientSession 的进入、使用、退出都由 _serve 完成，close 只投递哨兵。
    """

    def __init__(self, config: ServerConfig) -> None:
        self.config = config
        self._ready: Future = Future()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queue: asyncio.Queue | None = None
        self._closed = False
        self._lock = RLock()
        self._failure: str | None = None
        self._thread = Thread(target=self._run, name=f"mcp-{config.name}", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        asyncio.run(self._serve())

    async def _serve(self) -> None:
        self._loop, self._queue = asyncio.get_running_loop(), asyncio.Queue()
        active: Future | None = None
        try:
            async with AsyncExitStack() as stack:
                if self.config.transport == "stdio":
                    params = StdioServerParameters(command=self.config.command,
                        args=list(self.config.args), cwd=self.config.cwd, env=self.config.env)
                    # 远程 stderr 可能含凭证；不把未经筛选的子进程日志写入会话或终端。
                    errlog = stack.enter_context(open(os.devnull, "w"))
                    streams = await stack.enter_async_context(stdio_client(params, errlog=errlog))
                else:
                    http = await stack.enter_async_context(httpx.AsyncClient(
                        headers=self.config.headers, timeout=self.config.timeout,
                        follow_redirects=False,
                    ))
                    streams = await stack.enter_async_context(streamable_http_client(
                        self.config.url, http_client=http,
                    ))
                session = await stack.enter_async_context(ClientSession(
                    streams[0], streams[1], read_timeout_seconds=timedelta(seconds=self.config.timeout)
                ))
                # timeout 在同一个 task 内建立、退出；没有跨 Task 关闭 SDK 上下文。
                async with asyncio.timeout(self.config.timeout):
                    await session.initialize()
                    remote_tools = await list_all_tools(session)
                self._ready.set_result(remote_tools)
                while True:
                    request = await self._queue.get()
                    if request is None:
                        break
                    name, arguments, response = request
                    active = response
                    if response.cancelled():
                        continue
                    try:
                        async with asyncio.timeout(self.config.timeout):
                            value = await session.call_tool(name, arguments=arguments)
                        if not response.done():
                            response.set_result(value)
                    except Exception as exc:
                        if not response.done():
                            response.set_exception(RuntimeError(
                                f"MCP 调用失败（{type(exc).__name__}）"
                            ))
        except BaseException as exc:
            # 不传播底层异常 repr：HTTP URL、header 或 subprocess 参数可能带密钥。
            self._failure = f"MCP 连接失败（{type(exc).__name__}）"
            if not self._ready.done():
                self._ready.set_exception(RuntimeError(self._failure))
        finally:
            with self._lock:
                self._closed = True
            if active is not None and not active.done():
                active.set_exception(RuntimeError(self._failure or "MCP 连接已关闭"))
            while not self._queue.empty():
                request = self._queue.get_nowait()
                if request is not None and not request[2].done():
                    request[2].set_exception(RuntimeError(self._failure or "MCP 连接已关闭"))

    def tools(self) -> list:
        try:
            return self._ready.result(timeout=self.config.timeout + 5)
        except Exception:
            self.close()
            raise

    def call(self, name: str, arguments: dict) -> Any:
        response: Future = Future()
        with self._lock:
            if self._closed or self._loop is None or self._queue is None:
                raise RuntimeError("MCP 连接已关闭")
            self._loop.call_soon_threadsafe(self._queue.put_nowait, (name, arguments, response))
        try:
            return response.result(timeout=self.config.timeout + 5)
        except TimeoutError:
            response.cancel()
            raise RuntimeError("MCP 调用超时") from None

    def close(self) -> None:
        with self._lock:
            if not self._closed and self._loop is not None and self._queue is not None:
                self._loop.call_soon_threadsafe(self._queue.put_nowait, None)
                self._closed = True
        self._thread.join(timeout=self.config.timeout + 10)
        if self._thread.is_alive():
            raise RuntimeError("MCP 连接未能按时清理")


class MCPManager:
    """动态注册真实远程工具，所有状态均属于这个 manager 实例。

    只有配置文件 read_only_tools 中的原始工具名可以省去审批。远程声明
    readOnlyHint=True 只是远程数据，不能提升权限。connect() 是宿主 API，
    调用方负责授权；模型入口 connect_mcp 则始终 requires_approval=True。
    """

    def __init__(self, config_path: Path, registry, env: dict[str, str] | None = None) -> None:
        self._config = load_config(Path(config_path), env=env)
        self._registry = registry
        self._connections: dict[str, _Connection] = {}
        self._registered: dict[str, list[str]] = {}
        self._lock = RLock()
        self._closed = False

    def list_servers(self) -> list[dict]:
        with self._lock:
            # 不返回 command、args、URL、headers、env；配置是宿主控制信息。
            return [{"name": name, "transport": config.transport,
                     "connected": name in self._connections,
                     "tools": list(self._registered.get(name, []))}
                    for name, config in self._config.items()]

    def connect(self, name: str) -> list[str]:
        with self._lock:
            if self._closed:
                raise RuntimeError("MCPManager 已关闭")
            if name in self._connections:
                return list(self._registered[name])
            if name not in self._config:
                raise ValueError(f"未配置 MCP 服务器：{name}")
            config = self._config[name]
            connection = _Connection(config)
            registered: list[str] = []
            try:
                specs, seen = [], set()
                for remote in connection.tools():
                    local_name = tool_name(name, remote.name)
                    try:
                        existing = self._registry.get(local_name)
                    except KeyError:
                        existing = None
                    if local_name in seen or existing is not None:
                        raise ValueError(f"MCP 工具名称冲突：{local_name}")
                    seen.add(local_name)
                    specs.append(ToolSpec(
                        local_name, f"[MCP {name}] {remote.description or remote.name}",
                        remote.inputSchema,
                        lambda ctx, args, server=name, tool=remote.name: self.call(server, tool, args),
                        requires_approval=remote.name not in config.read_only_tools,
                    ))
                for spec in specs:
                    self._registry.register(spec)
                    registered.append(spec.name)
                self._connections[name] = connection
                self._registered[name] = registered
                return list(registered)
            except Exception:
                for local_name in registered:
                    self._registry.unregister(local_name)
                connection.close()
                raise

    def call(self, server: str, name: str, arguments: dict) -> str:
        with self._lock:
            connection = self._connections.get(server)
        if connection is None:
            return "Error: MCP 服务器尚未连接"
        try:
            result = connection.call(name, arguments)
            parts = []
            for block in result.content:
                if block.type == "text":
                    parts.append(block.text)
                elif block.type == "resource" and hasattr(block.resource, "text"):
                    parts.append(block.resource.text)
                else:
                    # 当前内部工具协议是文本。保留非文本类型提示，不注入大段 base64。
                    parts.append(f"[MCP {block.type} 内容，文本接口未展开]")
            if result.structuredContent is not None:
                parts.append(json.dumps(result.structuredContent, ensure_ascii=False))
            rendered = "\n".join(parts) or "(empty MCP result)"
            for secret in connection.config.secrets:
                rendered = rendered.replace(secret, "[REDACTED]")
            return ("Error: " if result.isError else "") + rendered
        except Exception as exc:
            return f"Error: MCP 调用失败（{type(exc).__name__}）"

    def disconnect(self, name: str) -> bool:
        with self._lock:
            connection = self._connections.pop(name, None)
            for local_name in self._registered.pop(name, []):
                self._registry.unregister(local_name)
        if connection is not None:
            connection.close()
            return True
        return False

    def close(self) -> None:
        with self._lock:
            self._closed = True
            names = list(self._connections)
        errors = []
        for name in names:
            try:
                self.disconnect(name)
            except Exception as exc:
                errors.append(type(exc).__name__)
        if errors:
            raise RuntimeError(f"{len(errors)} 个 MCP 连接清理失败")

    def register_tools(self, registry=None) -> None:
        registry = registry or self._registry
        registry.register(ToolSpec("list_mcp_servers", "查看宿主配置的 MCP 服务器及连接状态。",
            object_schema(), lambda ctx, args: json.dumps(self.list_servers(), ensure_ascii=False)))
        registry.register(ToolSpec("connect_mcp", "连接已配置 MCP 服务器；stdio 会启动本地进程。",
            object_schema({"name": {"type": "string"}}, ["name"]),
            lambda ctx, args: json.dumps(self.connect(args["name"]), ensure_ascii=False),
            requires_approval=True, roles=frozenset({"lead"})))
        registry.register(ToolSpec("disconnect_mcp", "断开服务器并移除它注册的工具。",
            object_schema({"name": {"type": "string"}}, ["name"]),
            lambda ctx, args: json.dumps({"disconnected": self.disconnect(args["name"])}),
            requires_approval=True, roles=frozenset({"lead"})))
