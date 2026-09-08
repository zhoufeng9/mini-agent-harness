"""文件持久化任务图：依赖检查、原子认领，以及跨工具调用保留的 cwd 租约。

任务完成和租约释放分成两步。模型可能在同一条响应中先 complete_task 再写文件，
因此 completed 任务的工作目录仍有效，直到宿主执行完整组工具后调用
release_completed。owner 保留为完成记录，lease_active 才表示正在使用目录。
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from uuid import uuid4

from ..core.types import ExecutionContext, ToolSpec, object_schema
from .storage import FileMutex, write_json_atomic


@dataclass
class Task:
    """稳定 ID 的任务记录；version 每次租约身份变化时递增，使旧审批失效。"""

    id: str
    subject: str
    description: str = ""
    status: str = "pending"
    owner: str | None = None
    blocked_by: list[str] = field(default_factory=list)
    worktree: str | None = None
    version: int = 0
    lease_active: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


class TaskStore:
    """每个 Harness 显式持有一份存储；导入本模块不会创建文件或访问模型。"""

    def __init__(self, state_dir: Path, workspace: Path):
        self.workspace = Path(workspace).resolve()
        self.state_dir = Path(state_dir).resolve()
        self.root = self.state_dir / "tasks"
        self._mutex = FileMutex(self.root / ".lock")

    def transaction(self):
        """供 worktree 等跨记录事务使用；所有修改共用同一锁序。"""
        if self.root.resolve() != self.root:
            raise ValueError("任务目录不能是指向其他位置的符号链接")
        return self._mutex.hold()

    def _path(self, task_id: str) -> Path:
        if not isinstance(task_id, str) or not re.fullmatch(r"task_[0-9a-f]{12}", task_id):
            raise ValueError(f"无效任务 ID：{task_id!r}")
        path = self.root / f"{task_id}.json"
        if path.resolve() != path:
            raise ValueError("任务文件不能是符号链接")
        return path

    def _save(self, task: Task) -> None:
        """调用方必须处于 transaction 中，避免覆盖其他线程/进程的更新。"""
        write_json_atomic(self._path(task.id), task.to_dict())

    def get(self, task_id: str) -> Task:
        with self.transaction():
            task = Task(**json.loads(self._path(task_id).read_text(encoding="utf-8")))
            if task.id != task_id or task.status not in {"pending", "in_progress", "completed"}:
                raise ValueError(f"损坏的任务记录：{task_id}")
            if task.lease_active and (not task.owner or task.status == "pending"):
                raise ValueError(f"任务租约与状态不一致：{task_id}")
            return task

    def list(self) -> list[Task]:
        with self.transaction():
            return [self.get(path.stem) for path in sorted(self.root.glob("task_*.json"))]

    def create(self, subject: str, description: str = "") -> Task:
        """先创建全部节点，再 update 添加边；不接受未经存在性验证的依赖 ID。"""
        if not isinstance(subject, str) or not subject.strip():
            raise ValueError("任务标题不能为空")
        with self.transaction():
            task = Task(id=f"task_{uuid4().hex[:12]}", subject=subject.strip(),
                        description=description)
            while self._path(task.id).exists():
                task.id = f"task_{uuid4().hex[:12]}"
            self._save(task)
            return task

    def update(self, task_id: str, add_blocked_by: list[str]) -> Task:
        """只给未认领任务添加依赖；任何新边都不能导致有向环。"""
        with self.transaction():
            task = self.get(task_id)
            if task.status != "pending" or task.owner or task.lease_active:
                raise ValueError("只能修改未认领 pending 任务的依赖")
            if not isinstance(add_blocked_by, list):
                raise ValueError("依赖必须是任务 ID 列表")
            for dependency in add_blocked_by:
                self.get(dependency)
                seen: set[str] = set()
                pending = [dependency]
                while pending:
                    current = pending.pop()
                    if current == task_id:
                        raise ValueError("此依赖会形成任务环")
                    if current not in seen:
                        seen.add(current)
                        pending.extend(self.get(current).blocked_by)
                if dependency not in task.blocked_by:
                    task.blocked_by.append(dependency)
            self._save(task)
            return task

    def can_start(self, task_id: str) -> bool:
        with self.transaction():
            task = self.get(task_id)
            return all(self.get(dep).status == "completed" for dep in task.blocked_by)

    def assignment(self, owner: str) -> Task | None:
        """查询有效租约；完成但尚未离开当前工具组的任务也会返回。"""
        with self.transaction():
            assignments = [task for task in self.list()
                           if task.owner == owner and task.lease_active]
            if len(assignments) > 1:
                raise ValueError(f"{owner} 存在多个有效租约，需修复任务记录")
            return assignments[0] if assignments else None

    def resolve_cwd(self, task: Task) -> Path:
        """worktree 名称、实际路径和 Git registry 必须一致，不能仅相信磁盘目录。"""
        if not task.worktree:
            return self.workspace
        from .worktrees import registered_worktree_path
        return registered_worktree_path(self.workspace, task.worktree)

    def claim(self, task_id: str, owner: str) -> Task:
        """一个文件事务内检查依赖、owner 和目录再写入，竞争者最多一个获胜。"""
        if not isinstance(owner, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", owner):
            raise ValueError("无效任务 owner")
        with self.transaction():
            task = self.get(task_id)
            if self.assignment(owner):
                raise ValueError(f"{owner} 必须先结束当前 assignment 的工具轮次")
            if task.status != "pending" or task.owner or task.lease_active:
                raise ValueError(f"任务 {task.id} 已被认领或完成")
            if not self.can_start(task_id):
                raise ValueError(f"任务依赖尚未完成：{task.blocked_by}")
            self.resolve_cwd(task)
            task.owner, task.status, task.lease_active = owner, "in_progress", True
            task.version += 1
            self._save(task)
            return task

    def claim_next(self, owner: str) -> Task | None:
        """IDLE worker 自动领取一个 ready task；无效 worktree 留给宿主修复。"""
        with self.transaction():
            if self.assignment(owner):
                return None
            for task in self.list():
                if task.status == "pending" and task.owner is None and self.can_start(task.id):
                    try:
                        return self.claim(task.id, owner)
                    except ValueError:
                        continue
            return None

    def complete(self, task_id: str, owner: str) -> Task:
        """仅 owner 能完成当前租约；计划审批由 TeamsManager 的工具守卫检查。"""
        with self.transaction():
            task = self.get(task_id)
            if task.status != "in_progress" or task.owner != owner or not task.lease_active:
                raise PermissionError(f"只有任务 {task_id} 的当前 owner 可以完成它")
            self.resolve_cwd(task)
            task.status = "completed"
            self._save(task)
            return task

    def release_completed(self, owner: str) -> bool:
        """只在模型响应的全部工具执行完毕后调用，释放已完成任务的目录租约。"""
        with self.transaction():
            task = self.assignment(owner)
            if task is None or task.status != "completed":
                return False
            task.lease_active = False
            task.version += 1
            self._save(task)
            return True

    def release_owner(self, owner: str) -> bool:
        """worker 失败或退出时归还未完成任务；完成记录保留，只释放目录租约。"""
        with self.transaction():
            task = self.assignment(owner)
            if task is None:
                return False
            if task.status == "in_progress":
                task.status, task.owner = "pending", None
            task.lease_active = False
            task.version += 1
            self._save(task)
            return True

    def bind_context(self, ctx: ExecutionContext) -> Task | None:
        """每次执行前刷新身份；subagent 独立上下文不参与共享任务认领。"""
        if ctx.role == "subagent":
            return None
        with self.transaction():
            task = self.assignment(ctx.agent_id)
            ctx.task_id = task.id if task else None
            ctx.cwd = self.resolve_cwd(task) if task else self.workspace
            return task

    def register_tools(self, registry) -> None:
        """模型不能自行指定 owner；工具身份只能来自宿主 ExecutionContext。"""
        lead = frozenset({"lead"})
        workers = frozenset({"lead", "teammate"})
        text = {"type": "string"}

        def claim(ctx, args):
            task = self.claim(args["task_id"], ctx.agent_id)
            self.bind_context(ctx)
            return json.dumps(task.to_dict(), ensure_ascii=False)

        specs = [
            ToolSpec("create_task", "创建持久任务节点，然后用 update_task 添加依赖。",
                     object_schema({"subject": text, "description": text}, ["subject"]),
                     lambda ctx, a: json.dumps(self.create(**a).to_dict(), ensure_ascii=False),
                     roles=lead),
            ToolSpec("update_task", "给未认领任务添加依赖，拒绝缺失节点或依赖环。",
                     object_schema({"task_id": text, "addBlockedBy": {
                         "type": "array", "items": text}}, ["task_id", "addBlockedBy"]),
                     lambda ctx, a: json.dumps(self.update(a["task_id"], a["addBlockedBy"])
                                               .to_dict(), ensure_ascii=False), roles=lead),
            ToolSpec("list_tasks", "列举任务图、状态、owner 和 worktree。", object_schema(),
                     lambda ctx, a: json.dumps([t.to_dict() for t in self.list()],
                                              ensure_ascii=False), roles=workers),
            ToolSpec("get_task", "读取一个任务记录。", object_schema({"task_id": text},
                                                                           ["task_id"]),
                     lambda ctx, a: json.dumps(self.get(a["task_id"]).to_dict(),
                                              ensure_ascii=False), roles=workers),
            ToolSpec("claim_task", "原子认领一个就绪任务，并绑定当前执行目录。",
                     object_schema({"task_id": text}, ["task_id"]), claim, roles=workers),
            ToolSpec("complete_task", "完成自己认领的任务；本组后续工具仍使用其工作目录。",
                     object_schema({"task_id": text}, ["task_id"]),
                     lambda ctx, a: json.dumps(self.complete(a["task_id"], ctx.agent_id)
                                               .to_dict(), ensure_ascii=False), roles=workers),
        ]
        for spec in specs:
            registry.register(spec)
