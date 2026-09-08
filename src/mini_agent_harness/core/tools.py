"""线程安全工具注册表：schema、可见角色和参数校验共用一个事实来源。"""

from __future__ import annotations

import re
from threading import RLock

from jsonschema import Draft202012Validator

from .types import ExecutionContext, ToolSpec


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}
        self._lock = RLock()

    def register(self, spec: ToolSpec) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", spec.name):
            raise ValueError(f"Invalid tool name: {spec.name}")
        Draft202012Validator.check_schema(spec.input_schema)
        with self._lock:
            if spec.name in self._tools:
                raise ValueError(f"Tool already registered: {spec.name}")
            self._tools[spec.name] = spec

    def unregister(self, name: str) -> None:
        with self._lock:
            self._tools.pop(name, None)

    def get(self, name: str) -> ToolSpec:
        with self._lock:
            return self._tools[name]

    def schemas(self, ctx: ExecutionContext) -> list[dict]:
        with self._lock:
            return [s.schema() for s in self._tools.values() if ctx.role in s.roles]

    def validate(self, name: str, args: dict, ctx: ExecutionContext) -> ToolSpec:
        spec = self.get(name)
        if ctx.role not in spec.roles:
            raise PermissionError(f"{ctx.role} cannot use {name}")
        Draft202012Validator(spec.input_schema).validate(args)
        return spec
