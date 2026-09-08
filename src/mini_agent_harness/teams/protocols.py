"""协议记录只保存状态；消息文本本身不能授予计划权限或冒充 Lead。"""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import uuid4


@dataclass
class ProtocolRequest:
    """审批绑定 task ID + version，旧 assignment 的响应无法放行新任务。"""

    kind: str
    sender: str
    target: str
    identity: tuple[str | None, int]
    payload: str = ""
    status: str = "pending"
    id: str = field(default_factory=lambda: uuid4().hex)
