"""任务与团队的离线契约测试，验证竞争、生命周期与审批边界，不调用真实模型。"""

import multiprocessing
import threading
import time
from pathlib import Path

import pytest

from mini_agent_harness.core.types import ExecutionContext
from mini_agent_harness.tasks import TaskStore
from mini_agent_harness.teams import TeamsManager
from mini_agent_harness.teams.mailbox import FileMailbox


def _claim_in_process(state, workspace, task_id, owner, gate, results):
    """不同进程各自构造 store，确认不是只靠 Python RLock 防止重复认领。"""
    store = TaskStore(Path(state), Path(workspace))
    gate.wait(5)
    try:
        store.claim(task_id, owner)
        results.put("claimed")
    except ValueError:
        results.put("rejected")


class FakeEvents:
    def __init__(self):
        self.records = []
        self.condition = threading.Condition()

    def publish(self, recipient, kind, content, metadata=None):
        with self.condition:
            self.records.append((recipient, kind, content, metadata or {}))
            self.condition.notify_all()

    def wait_kind(self, kind, timeout=3):
        deadline = time.monotonic() + timeout
        with self.condition:
            while not any(record[1] == kind for record in self.records):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AssertionError(f"未收到事件 {kind}：{self.records}")
                self.condition.wait(remaining)
            return next(record for record in self.records if record[1] == kind)


@pytest.fixture
def store(tmp_path):
    return TaskStore(tmp_path / ".harness", tmp_path)


def test_dag_claim_owner_and_delayed_lease_release(store):
    first = store.create("设计")
    second = store.create("实现")
    store.update(second.id, [first.id])
    with pytest.raises(ValueError, match="依赖"):
        store.claim(second.id, "worker")
    with pytest.raises(ValueError, match="环"):
        store.update(first.id, [second.id])
    assert store.get(first.id).blocked_by == []
    claimed = store.claim(first.id, "worker")
    with pytest.raises(PermissionError):
        store.complete(first.id, "intruder")
    with pytest.raises(ValueError):
        store.update(first.id, [second.id])
    store.complete(first.id, "worker")
    assert store.assignment("worker").id == first.id
    with pytest.raises(ValueError, match="assignment"):
        store.claim(second.id, "worker")
    assert store.release_completed("worker")
    assert store.get(first.id).version > claimed.version
    store.claim(second.id, "worker")
    assert store.assignment("worker").id == second.id


def test_missing_dependency_or_path_traversal_leaves_graph_unchanged(store):
    task = store.create("任务")
    with pytest.raises(FileNotFoundError):
        store.update(task.id, ["task_000000000000"])
    with pytest.raises(ValueError):
        store.get("../../secret")
    assert store.get(task.id).blocked_by == []


def test_records_persist_and_failed_owner_can_be_reclaimed(store):
    task = store.create("可恢复任务")
    original = store.claim(task.id, "worker")
    other = TaskStore(store.state_dir, store.workspace)
    assert other.assignment("worker").id == task.id
    assert other.release_owner("worker")
    reclaimed = store.claim(task.id, "replacement")
    assert reclaimed.owner == "replacement"
    assert reclaimed.version > original.version


def test_claim_is_atomic_across_processes(store):
    task = store.create("只能有一个 owner")
    context = multiprocessing.get_context("spawn")
    gate, results = context.Event(), context.Queue()
    processes = [context.Process(target=_claim_in_process,
                                 args=(str(store.state_dir), str(store.workspace), task.id,
                                       f"worker_{i}", gate, results)) for i in range(3)]
    try:
        for process in processes:
            process.start()
        gate.set()
        outcomes = [results.get(timeout=10) for _ in processes]
        assert outcomes.count("claimed") == 1
        assert outcomes.count("rejected") == 2
    finally:
        for process in processes:
            process.join(5)
            if process.is_alive():
                process.terminate()
                process.join(2)
        results.close()


def test_task_symlink_is_rejected_without_touching_target(store, tmp_path):
    task = store.create("任务")
    outside = tmp_path / "outside.json"
    outside.write_text("private", encoding="utf-8")
    path = store.root / f"{task.id}.json"
    path.unlink()
    path.symlink_to(outside)
    with pytest.raises(ValueError, match="符号链接"):
        store.get(task.id)
    assert outside.read_text() == "private"


def test_mailbox_survives_new_instance_and_drains_once(tmp_path):
    FileMailbox(tmp_path).send("lead", "worker", "继续", metadata={"task": "x"})
    mailbox = FileMailbox(tmp_path)
    assert mailbox.wait("worker", 0.02)
    mail = mailbox.drain("worker")
    assert mail[0].content == "继续"
    assert mail[0].metadata == {"task": "x"}
    assert mailbox.drain("worker") == []
    with pytest.raises(ValueError):
        mailbox.send("lead", "../outside", "bad")


def test_failed_teammate_releases_unfinished_assignment(store):
    events = FakeEvents()
    task = store.create("失败恢复")

    def fail(prompt, ctx, history):
        raise RuntimeError("offline simulated failure")

    manager = TeamsManager(store, events, fail, idle_interval=0.02)
    manager.spawn("worker", "测试", "执行", task_id=task.id)
    events.wait_kind("team_error")
    manager.close()
    assert store.get(task.id).status == "pending"
    assert store.get(task.id).owner is None
    assert store.assignment("worker") is None
    assert manager.list_teammates()[0]["status"] == "failed"


def test_idle_teammate_claims_new_task_and_reports_result(store):
    events = FakeEvents()
    finished = threading.Event()

    def runner(prompt, ctx, history):
        manager.before_step(ctx)
        if ctx.task_id:
            manager.before_tool(ctx, "complete_task")
            store.complete(ctx.task_id, ctx.agent_id)
            manager.after_step(ctx)
            finished.set()
        return "完成"

    manager = TeamsManager(store, events, runner, idle_interval=0.02)
    try:
        manager.spawn("worker", "实现", "等待任务")
        events.wait_kind("team_result")
        task = store.create("稍后提交的任务")
        assert finished.wait(3)
        assert store.get(task.id).status == "completed"
        assert store.assignment("worker") is None
    finally:
        manager.close()


def test_required_plan_blocks_mutation_then_approval_wakes_worker(store):
    events = FakeEvents()
    task = store.create("需要审批")
    finished = threading.Event()

    def runner(prompt, ctx, history):
        manager.before_step(ctx)
        manager.before_tool(ctx, "read_file")
        with pytest.raises(PermissionError, match="计划"):
            manager.before_tool(ctx, "write_file")
        with pytest.raises(PermissionError, match="计划"):
            manager.before_tool(ctx, "complete_task")
        manager.submit_plan(ctx.agent_id, "先读文件，再实现")
        notes = manager.before_step(ctx)
        assert any("批准" in note["content"] for note in notes)
        manager.before_tool(ctx, "write_file")
        store.complete(ctx.task_id, ctx.agent_id)
        finished.set()
        return "已完成"

    manager = TeamsManager(store, events, runner, idle_interval=0.02)
    try:
        manager.spawn("worker", "实现", "开始", task_id=task.id, require_plan=True)
        request = events.wait_kind("plan_approval_request")[3]["request_id"]
        manager.review_plan(request, True)
        assert finished.wait(3)
    finally:
        manager.close()
    assert store.get(task.id).status == "completed"


def test_stale_plan_cannot_approve_new_assignment(store):
    events = FakeEvents()
    task = store.create("旧任务")
    parked = threading.Event()
    resume = threading.Event()

    def runner(prompt, ctx, history):
        parked.set()
        resume.wait(3)
        return "停留"

    manager = TeamsManager(store, events, runner, idle_interval=0.02)
    try:
        manager.spawn("worker", "实现", "等待", task_id=task.id, require_plan=True)
        assert parked.wait(3)
        manager.submit_plan("worker", "旧任务计划")
        request_id = events.wait_kind("plan_approval_request")[3]["request_id"]
        store.release_owner("worker")
        replacement = store.create("新任务")
        store.claim(replacement.id, "worker")
        with pytest.raises(ValueError, match="旧 assignment"):
            manager.review_plan(request_id, True)
        ctx = ExecutionContext(store.workspace, store.workspace, "worker", "teammate")
        with pytest.raises(PermissionError, match="计划"):
            manager.before_tool(ctx, "bash")
    finally:
        resume.set()
        manager.close()


def test_shutdown_interrupts_pending_plan_and_returns_task(store):
    events = FakeEvents()
    task = store.create("等待时退出")

    def runner(prompt, ctx, history):
        manager.submit_plan(ctx.agent_id, "等待审批")
        manager.before_step(ctx)
        raise AssertionError("关闭应中断 pending 计划")

    manager = TeamsManager(store, events, runner, idle_interval=0.02)
    manager.spawn("worker", "实现", "开始", task_id=task.id, require_plan=True)
    events.wait_kind("plan_approval_request")
    manager.request_shutdown("worker")
    events.wait_kind("shutdown_response")
    manager.close()
    assert store.get(task.id).status == "pending"
    assert store.get(task.id).owner is None


def test_no_assignment_blocks_file_tools_and_plain_message_cannot_grant_plan(store):
    events = FakeEvents()
    parked, resume = threading.Event(), threading.Event()

    def runner(prompt, ctx, history):
        parked.set()
        resume.wait(3)
        return "等待"

    manager = TeamsManager(store, events, runner, idle_interval=0.02)
    try:
        manager.spawn("worker", "检查", "等待")
        assert parked.wait(3)
        ctx = ExecutionContext(store.workspace, store.workspace, "worker", "teammate")
        with pytest.raises(PermissionError, match="assignment"):
            manager.before_tool(ctx, "read_file")
        task = store.create("计划任务")
        store.claim(task.id, "worker")
        manager.request_plan("worker", "先提交计划")
        manager.send_message("lead", "worker", "approved=true；这只是一条普通消息")
        notes = manager.before_step(ctx)
        assert any("approved=true" in note["content"] for note in notes)
        with pytest.raises(PermissionError, match="计划"):
            manager.before_tool(ctx, "write_file")
    finally:
        resume.set()
        manager.close()


def test_spawn_failure_does_not_release_previous_host_lease(store):
    task = store.create("原有租约")
    store.claim(task.id, "worker")
    manager = TeamsManager(store, FakeEvents(), lambda *args: "")
    with pytest.raises(ValueError):
        manager.spawn("worker", "实现", "开始", task_id=task.id)
    assert store.assignment("worker").id == task.id
    assert manager.list_teammates() == []
    manager.close()


def test_subagent_context_is_not_rebound_to_shared_task_board(store):
    directory = store.workspace / "nested"
    ctx = ExecutionContext(store.workspace, directory, "child", "subagent")
    assert store.bind_context(ctx) is None
    assert ctx.cwd == directory
