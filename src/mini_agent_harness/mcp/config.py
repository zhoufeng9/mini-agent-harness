"""读取宿主提供的 MCP 配置；服务器返回的 annotations 不具有授权效力。"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

_VARIABLE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


@dataclass(frozen=True)
class ServerConfig:
    name: str
    transport: str
    command: str = field(default="", repr=False)
    args: tuple[str, ...] = field(default=(), repr=False)
    cwd: Path | None = field(default=None, repr=False)
    env: dict[str, str] = field(default_factory=dict, repr=False)
    url: str = field(default="", repr=False)
    headers: dict[str, str] = field(default_factory=dict, repr=False)
    read_only_tools: frozenset[str] = frozenset()
    timeout: float = 30.0
    secrets: tuple[str, ...] = field(default=(), repr=False)


def load_config(path: Path, env: dict[str, str] | None = None) -> dict[str, ServerConfig]:
    """${NAME} 仅从当前进程环境展开，不执行 shell，也不自动读取其他 env 文件。

    Harness 可把本地 .env 内容作为 env 显式传入，不必污染 os.environ。
    参数中缺失变量时报变量名，不打印字段值。
    stdio SDK 只继承其保守的默认环境，再叠加显式 env；模型 API 密钥不会
    因为我们复制整个 os.environ 而意外发送给任意 MCP 子进程。
    """
    path = Path(path)
    environment = dict(os.environ) if env is None else dict(env)
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    servers = raw.get("mcpServers")
    if not isinstance(servers, dict):
        raise ValueError("MCP 配置必须含 mcpServers 对象")
    result = {}
    for name, item in servers.items():
        if not isinstance(name, str) or not name or not isinstance(item, dict):
            raise ValueError("MCP 服务器名称和配置格式错误")
        secrets: set[str] = set()

        def expand(value):
            if isinstance(value, str):
                def replace(match):
                    variable = match.group(1)
                    if variable not in environment:
                        raise ValueError(f"MCP 配置缺少环境变量：{variable}")
                    expanded = environment[variable]
                    if not isinstance(expanded, str):
                        raise ValueError(f"MCP 配置环境变量不是字符串：{variable}")
                    if expanded:
                        secrets.add(expanded)
                    return expanded
                return _VARIABLE.sub(replace, value)
            if isinstance(value, list):
                return [expand(v) for v in value]
            if isinstance(value, dict):
                return {k: expand(v) for k, v in value.items()}
            return value

        item = expand(item)
        transport = item.get("transport", "stdio")
        if transport not in {"stdio", "streamable-http"}:
            raise ValueError("MCP transport 仅支持 stdio 或 streamable-http")
        command, args = item.get("command", ""), item.get("args", [])
        if not isinstance(command, str) or not isinstance(args, list) or not all(
            isinstance(a, str) for a in args
        ):
            raise ValueError("MCP command 必须是字符串，args 必须是字符串数组")
        if transport == "stdio" and not command:
            raise ValueError("stdio MCP 缺少 command")
        url = item.get("url", "")
        if transport == "streamable-http":
            if not isinstance(url, str) or urlparse(url).scheme not in {"http", "https"}:
                raise ValueError("Streamable HTTP MCP 需要 http/https URL")
            if not urlparse(url).hostname or urlparse(url).username or urlparse(url).password:
                raise ValueError("MCP URL 必须含主机且不得内嵌登录凭证；请使用 headers")
        env, headers = item.get("env", {}), item.get("headers", {})
        for values in (env, headers):
            if not isinstance(values, dict) or not all(
                isinstance(k, str) and isinstance(v, str) for k, v in values.items()
            ):
                raise ValueError("MCP env 和 headers 必须是字符串键值对象")
            secrets.update(v for v in values.values() if v)
        allowed = item.get("read_only_tools", [])
        if not isinstance(allowed, list) or not all(isinstance(v, str) for v in allowed):
            raise ValueError("read_only_tools 必须是原始工具名字符串数组")
        timeout = item.get("timeout", 30.0)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 600:
            raise ValueError("MCP timeout 必须在 0 到 600 秒之间")
        cwd = Path(item.get("cwd", "."))
        if not cwd.is_absolute():
            cwd = path.parent / cwd
        result[name] = ServerConfig(
            name=name, transport=transport, command=command, args=tuple(args), cwd=cwd.resolve(),
            env=env, url=url, headers=headers, read_only_tools=frozenset(allowed),
            timeout=float(timeout), secrets=tuple(sorted(secrets, key=len, reverse=True)),
        )
    return result
