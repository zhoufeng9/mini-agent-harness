"""显式配置：只读指定 .env，不在 import 时修改进程环境或创建客户端。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values


@dataclass(frozen=True)
class Settings:
    workspace: Path
    provider: str = "anthropic"
    model: str = ""
    api_key: str = field(default="", repr=False)
    base_url: str | None = None
    openai_api_mode: str = "responses"
    max_tokens: int = 8000
    max_steps: int = 100
    context_chars: int = 160000
    shell_timeout: float = 120
    request_timeout: float = 120
    max_retries: int = 3
    fallback_model: str = ""
    memory_enabled: bool = True
    timezone: str = "Asia/Shanghai"
    mcp_config: Path | None = None
    env_file: Path | None = None

    @property
    def state_dir(self) -> Path:
        return self.workspace / ".harness"

    @classmethod
    def load(cls, workspace: str | Path = ".", *, env_file: str | Path | None = None,
             provider: str | None = None, model: str | None = None) -> Settings:
        """优先级：显式参数 > 进程环境 > 指定 .env > 默认值。

        不向父目录搜索 .env，避免意外使用另一个项目的账号。
        dotenv_values 只解析值，不把密钥写入日志或 dataclass repr。
        """
        root = Path(workspace).expanduser().resolve()
        source = Path(env_file).expanduser().resolve() if env_file else root / ".env"
        env = {**(dotenv_values(source) if source.is_file() else {}), **os.environ}
        selected = provider or env.get("HARNESS_PROVIDER", "anthropic")
        if selected not in {"anthropic", "openai"}:
            raise ValueError("HARNESS_PROVIDER 必须为 anthropic 或 openai")
        prefix = selected.upper()
        chosen_model = model or env.get(f"{prefix}_MODEL") or env.get("MODEL_ID", "")
        if not chosen_model:
            raise ValueError(f"请在 {source} 设置 {prefix}_MODEL")
        api_mode = env.get("OPENAI_API_MODE", "responses")
        if api_mode not in {"responses", "chat_completions"}:
            raise ValueError("OPENAI_API_MODE 必须为 responses 或 chat_completions")
        numeric = {
            "max_tokens": int(env.get("HARNESS_MAX_TOKENS", 8000)),
            "max_steps": int(env.get("HARNESS_MAX_STEPS", 100)),
            "context_chars": int(env.get("HARNESS_CONTEXT_CHARS", 160000)),
            "shell_timeout": float(env.get("HARNESS_SHELL_TIMEOUT", 120)),
            "request_timeout": float(env.get("HARNESS_REQUEST_TIMEOUT", 120)),
            "max_retries": int(env.get("HARNESS_MAX_RETRIES", 3)),
        }
        if any(v <= 0 for k, v in numeric.items() if k != "max_retries"):
            raise ValueError("预算、超时和步数必须大于 0")
        if numeric["max_retries"] < 0:
            raise ValueError("重试次数不能小于 0")
        mcp_path = Path(env.get("HARNESS_MCP_CONFIG") or "mcp.json")
        return cls(
            workspace=root, provider=selected, model=chosen_model,
            api_key=env.get(f"{prefix}_API_KEY") or "",
            base_url=env.get(f"{prefix}_BASE_URL") or None,
            openai_api_mode=api_mode,
            fallback_model=env.get("HARNESS_FALLBACK_MODEL") or "",
            memory_enabled=str(env.get("HARNESS_MEMORY", "true")).lower() in {"1", "true", "yes"},
            timezone=env.get("HARNESS_TIMEZONE") or "Asia/Shanghai",
            mcp_config=(root / mcp_path).resolve(), env_file=source, **numeric,
        )
