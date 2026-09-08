"""前台审批与异步拒绝：只有正在等待用户的前台执行可以请求批准。"""

from __future__ import annotations

from typing import Callable

from ..core.types import ExecutionContext, ToolSpec


class PermissionPolicy:
    def __init__(self, approve: Callable[[str], bool] | None = None):
        self.approve = approve

    def check(self, *, ctx: ExecutionContext, spec: ToolSpec, args: dict) -> str | None:
        sensitive_file = spec.name == "read_file" and any(
            part.startswith(".env") for part in str(args.get("path", "")).split("/")
        )
        if not spec.requires_approval and not sensitive_file:
            return None
        if not ctx.interactive or self.approve is None:
            return f"Denied: {spec.name} requires foreground user approval"
        detail = str(args.get("command") or args.get("path") or args.get("name") or "")
        # MCP 参数可能含敏感值；审批展示工具身份，不把整份参数打印到终端。
        prompt = f"Allow {spec.name} {detail} in {ctx.cwd}?"
        return None if self.approve(prompt) else f"Denied by user: {spec.name}"
