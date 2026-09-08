"""跨模块的少量稳定协议；不持有客户端、线程、文件句柄等运行状态。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol
from uuid import uuid4

Message = dict[str, Any]


@dataclass
class ExecutionContext:
    """一次执行的身份。cwd 可指向任务 worktree，workspace 始终是宿主根目录。"""

    workspace: Path
    cwd: Path
    agent_id: str = "lead"
    role: str = "lead"
    interactive: bool = False
    task_id: str | None = None
    depth: int = 0
    session_id: str = field(default_factory=lambda: uuid4().hex)


@dataclass
class ModelResponse:
    """内部采用 text/tool_use 内容块，与 SDK 对象解耦，能直接序列化到磁盘。"""

    content: list[dict[str, Any]]
    stop_reason: str = "end_turn"
    usage: dict[str, int] = field(default_factory=dict)

    @property
    def text(self) -> str:
        return "\n".join(b.get("text", "") for b in self.content if b.get("type") == "text")


class ModelProvider(Protocol):
    def generate(self, messages: list[Message], *, system: str,
                 tools: list[dict], max_tokens: int) -> ModelResponse: ...

    def close(self) -> None: ...


ToolHandler = Callable[[ExecutionContext, dict[str, Any]], str]


@dataclass(frozen=True)
class ToolSpec:
    """描述、参数和执行入口在一起注册，避免 s15 中定义表与分发表不同步。"""

    name: str
    description: str
    input_schema: dict[str, Any]
    handler: ToolHandler
    requires_approval: bool = False
    roles: frozenset[str] = frozenset({"lead", "teammate", "subagent"})

    def schema(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description,
                "input_schema": self.input_schema}


def object_schema(properties: dict | None = None, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": properties or {},
            "required": required or [], "additionalProperties": False}
