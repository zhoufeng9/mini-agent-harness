"""任务绑定的 Git worktree；它隔离 working copy，并不是命令执行沙箱。

创建失败保留 Git 已产生的部分状态，避免猜测性回滚误删用户内容。
删除只提供宿主 API，模型工具池没有删除入口。
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from ..core.types import ToolSpec, object_schema

if TYPE_CHECKING:
    from .store import TaskStore


def git(workspace: Path, *args: str) -> subprocess.CompletedProcess:
    """所有 Git 参数按 argv 传入，不拼接 shell；超时交给宿主显式处理。"""
    return subprocess.run(["git", "-C", str(workspace), *args], text=True,
                          capture_output=True, timeout=60, check=False)


def worktree_path(workspace: Path, name: str) -> Path:
    """拒绝目录穿越和符号链接，并限制名称可映射为正常 Git branch。"""
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", name):
        raise ValueError("worktree 名称须为 1–64 位字母、数字、点、横线或下划线")
    if ".." in name or name.endswith((".", ".lock")):
        raise ValueError("worktree 名称不能包含 '..' 或以 '.'/'.lock' 结尾")
    root = workspace.resolve() / ".worktrees"
    path = root / name
    if root.resolve() != root or path.resolve() != path:
        raise ValueError("worktree 目录不能经过符号链接")
    return path


def registered_worktrees(workspace: Path) -> dict[Path, dict[str, str]]:
    """使用 -z 解析真实路径，路径中的空格和换行不会破坏记录边界。"""
    result = git(workspace, "worktree", "list", "--porcelain", "-z")
    if result.returncode:
        raise ValueError(f"无法读取 Git worktree registry：{result.stderr.strip()}")
    records: dict[Path, dict[str, str]] = {}
    current: dict[str, str] = {}
    for token in result.stdout.split("\0"):
        if not token:
            if "worktree" in current:
                records[Path(current["worktree"]).resolve()] = current
            current = {}
            continue
        key, _, value = token.partition(" ")
        current[key] = value
    if "worktree" in current:
        records[Path(current["worktree"]).resolve()] = current
    return records


def registered_worktree_path(workspace: Path, name: str) -> Path:
    path = worktree_path(workspace, name)
    record = registered_worktrees(workspace).get(path)
    if not record or not path.is_dir():
        raise ValueError(f"worktree {name!r} 不存在或未在 Git registry 注册")
    if record.get("branch") != f"refs/heads/harness/{name}":
        raise ValueError(f"worktree {name!r} 的分支与任务约定不一致")
    if "prunable" in record:
        raise ValueError(f"worktree {name!r} 已失效，需宿主检查")
    return path


class WorktreeManager:
    """TaskStore 的事务覆盖 Git 操作与绑定写入，禁止并发认领半创建目录。"""

    def __init__(self, store: TaskStore, is_busy: Callable[[Path], bool] | None = None):
        self.store = store
        self.workspace = store.workspace
        self.is_busy = is_busy or (lambda path: False)

    def create(self, name: str, task_id: str) -> Path:
        """只有未被认领的 pending task 能绑定全新 worktree 和 harness/name 分支。"""
        path = worktree_path(self.workspace, name)
        branch = f"harness/{name}"
        with self.store.transaction():
            task = self.store.get(task_id)
            if task.status != "pending" or task.owner or task.lease_active or task.worktree:
                raise ValueError("只能为未认领且未绑定 worktree 的 pending task 创建目录")
            records = registered_worktrees(self.workspace)
            if path.exists() or path in records:
                raise ValueError(f"worktree 路径已存在或已注册：{path}")
            ref = git(self.workspace, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}")
            if ref.returncode != 1:
                raise ValueError(f"分支 {branch} 已存在，或无法验证分支状态")
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                result = git(self.workspace, "worktree", "add", "-b", branch, str(path))
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError("Git worktree 创建超时；可能存在部分 checkout，任务未绑定，"
                                   "请检查 Git registry 后恢复") from exc
            if result.returncode:
                raise RuntimeError(f"Git worktree 创建失败，任务未绑定；部分目录/分支保留供检查："
                                   f"{result.stderr.strip()}")
            registered_worktree_path(self.workspace, name)
            task.worktree = name
            task.version += 1
            self.store._save(task)
            return path

    def remove(self, name: str, *, discard_changes: bool = False,
               confirmed: bool = False) -> str:
        """宿主删除 checkout，保留 branch。

        discard_changes=True 必须同时传 confirmed=True，表示宿主已展示变更并获得
        用户明确授权。即使确认，也不允许删除仍有租约、任务 owner 或后台作业的目录。
        """
        if discard_changes and not confirmed:
            raise PermissionError("丢弃 worktree 修改需要宿主先取得明确确认")
        with self.store.transaction():
            path = registered_worktree_path(self.workspace, name)
            bound = [task for task in self.store.list() if task.worktree == name]
            if any(t.lease_active or (t.owner and t.status != "completed") for t in bound):
                raise ValueError("worktree 仍被任务 owner 或执行租约占用")
            if self.is_busy(path):
                raise ValueError("worktree 中仍有后台任务运行")
            status = git(path, "status", "--porcelain", "--untracked-files=all")
            if status.returncode:
                raise ValueError(f"无法检查 worktree 状态：{status.stderr.strip()}")
            if status.stdout.strip() and not discard_changes:
                raise ValueError("worktree 有未提交或未跟踪文件；请先提交或明确确认丢弃")
            args = ["worktree", "remove"]
            if discard_changes:
                args.append("--force")
            result = git(self.workspace, *args, str(path))
            if result.returncode:
                raise RuntimeError(f"worktree 删除失败：{result.stderr.strip()}")
            # Git 删除成功后才解除绑定；分支保留，所以已提交的工作仍可恢复。
            for task in bound:
                task.worktree = None
                task.version += 1
                self.store._save(task)
            return f"已移除 worktree {name}；分支 harness/{name} 保留。"

    def register_tools(self, registry) -> None:
        registry.register(ToolSpec(
            "create_worktree", "给未认领任务创建独立 Git worktree，并绑定该任务的执行目录。",
            object_schema({"name": {"type": "string"}, "task_id": {"type": "string"}},
                          ["name", "task_id"]),
            lambda ctx, a: str(self.create(a["name"], a["task_id"])),
            roles=frozenset({"lead"}),
        ))
