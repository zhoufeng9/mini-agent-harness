"""用临时 Git 仓库验证 worktree 的 registry、cwd 租约与宿主删除边界。"""

import subprocess

import pytest

from mini_agent_harness.core.types import ExecutionContext
from mini_agent_harness.tasks import TaskStore, WorktreeManager


@pytest.fixture
def repository(tmp_path):
    def git(*args):
        return subprocess.run(["git", "-C", str(tmp_path), *args], capture_output=True,
                              text=True, check=True)

    git("init")
    git("config", "user.name", "Harness Test")
    git("config", "user.email", "harness-test@example.invalid")
    (tmp_path / ".gitignore").write_text(".harness/\n.worktrees/\n", encoding="utf-8")
    (tmp_path / "example.txt").write_text("original\n", encoding="utf-8")
    git("add", ".")
    git("commit", "-m", "initial fixture")
    store = TaskStore(tmp_path / ".harness", tmp_path)
    return store, WorktreeManager(store), git


def test_worktree_stays_bound_until_full_tool_group_finishes(repository):
    store, manager, git = repository
    task = store.create("worktree 任务")
    path = manager.create("isolated", task.id)
    store.claim(task.id, "worker")
    ctx = ExecutionContext(store.workspace, store.workspace, "worker", "teammate")
    store.bind_context(ctx)
    assert ctx.cwd == path
    store.complete(task.id, "worker")
    store.bind_context(ctx)
    assert ctx.cwd == path
    with pytest.raises(ValueError, match="租约"):
        manager.remove("isolated")
    store.release_completed("worker")
    store.bind_context(ctx)
    assert ctx.cwd == store.workspace
    manager.remove("isolated")
    assert not path.exists()
    assert store.get(task.id).worktree is None
    assert "harness/isolated" in git("branch", "--list").stdout


def test_dirty_removal_requires_both_discard_and_host_confirmation(repository):
    store, manager, _ = repository
    task = store.create("dirty")
    path = manager.create("dirty", task.id)
    (path / "new.txt").write_text("uncommitted", encoding="utf-8")
    with pytest.raises(ValueError, match="未提交"):
        manager.remove("dirty")
    with pytest.raises(PermissionError, match="确认"):
        manager.remove("dirty", discard_changes=True)
    assert path.exists()
    manager.remove("dirty", discard_changes=True, confirmed=True)
    assert not path.exists()


def test_busy_worktree_cannot_be_removed_even_with_confirmation(repository):
    store, manager, _ = repository
    task = store.create("busy")
    manager.create("busy", task.id)
    busy = WorktreeManager(store, is_busy=lambda path: True)
    with pytest.raises(ValueError, match="后台"):
        busy.remove("busy", discard_changes=True, confirmed=True)


def test_unregistered_directory_or_changed_branch_cannot_be_claimed(repository):
    store, manager, _ = repository
    task = store.create("invalid registry")
    path = manager.create("checked", task.id)
    subprocess.run(["git", "-C", str(path), "checkout", "--detach"],
                   capture_output=True, check=True)
    with pytest.raises(ValueError, match="分支"):
        store.claim(task.id, "worker")
    assert store.get(task.id).status == "pending"


def test_worktree_path_escape_and_shared_binding_are_rejected(repository):
    store, manager, _ = repository
    task = store.create("escape")
    with pytest.raises(ValueError):
        manager.create("../../escape", task.id)
    manager.create("first", task.id)
    with pytest.raises(ValueError, match="未绑定"):
        manager.create("second", task.id)
