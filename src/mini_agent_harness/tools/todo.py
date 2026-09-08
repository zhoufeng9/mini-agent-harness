"""todo 是会话内清单；持久 task graph 是另一种生命周期，不混用状态。"""

from __future__ import annotations

import json
from threading import RLock

from ..core.types import ToolSpec, object_schema


class TodoStore:
    def __init__(self):
        self._items: dict[tuple[str, str], list[dict]] = {}
        self._lock = RLock()

    def write(self, ctx, args) -> str:
        items = args["todos"]
        if sum(item["status"] == "in_progress" for item in items) > 1:
            raise ValueError("Only one todo can be in_progress")
        with self._lock:
            self._items[(ctx.session_id, ctx.agent_id)] = items
        return json.dumps(items, ensure_ascii=False)

    def register_tools(self, registry) -> None:
        registry.register(ToolSpec("todo_write", "Replace this agent's session todo list.",
            object_schema({"todos": {"type": "array", "maxItems": 30, "items": object_schema({
                "content": {"type": "string", "minLength": 1},
                "status": {"enum": ["pending", "in_progress", "completed"]}},
                ["content", "status"])}}, ["todos"]), self.write))
