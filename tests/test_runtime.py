"""验证通知隔离与崩溃恢复语义，不调用付费模型服务。"""

from datetime import datetime, timedelta
from threading import Event as ThreadEvent
from threading import Thread

import pytest

from mini_agent_harness.core.types import ExecutionContext
from mini_agent_harness.runtime import BackgroundManager, EventBus, Scheduler


def test_event_mailboxes_are_isolated_and_wait_does_not_consume():
    bus = EventBus()
    metadata = {"nested": {"value": 1}}
    bus.publish("a", "done", "A", metadata)
    metadata["nested"]["value"] = 2
    bus.publish("b", "done", "B")
    assert bus.wait("a", 0)
    assert bus.drain("a")[0].metadata["nested"]["value"] == 1
    assert not bus.wait("a", 0)
    assert bus.drain("b")[0].content == "B"


def test_wait_wakes_when_another_thread_publishes():
    bus = EventBus()
    worker = Thread(target=lambda: bus.publish("lead", "done", "hello"))
    worker.start()
    assert bus.wait("lead", 2)
    worker.join()
    assert len(bus.drain("lead")) == 1


def test_background_completion_and_failure_route_to_owner(tmp_path):
    bus, release = EventBus(), ThreadEvent()
    manager = BackgroundManager(bus)
    ctx = ExecutionContext(tmp_path, tmp_path, agent_id="worker")

    def run():
        release.wait(2)
        return "result", 0

    job_id = manager.start(ctx, "example", run)
    assert manager.has_pending("worker")
    assert not manager.has_pending("lead")
    release.set()
    assert bus.wait("worker", 3)
    event = bus.drain("worker")[0]
    assert event.metadata["background_job_id"] == job_id
    assert event.content == "result"
    assert not bus.drain("lead")

    def fail():
        raise ValueError("expected")

    manager.start(ctx, "failure", fail)
    assert bus.wait("worker", 3)
    assert bus.drain("worker")[0].metadata["exit_code"] == -1
    manager.close()
    assert not manager.has_pending()
    with pytest.raises(RuntimeError):
        manager.start(ctx, "closed", run)


def test_queued_background_work_reserves_its_original_directory(tmp_path):
    bus, release = EventBus(), ThreadEvent()
    manager = BackgroundManager(bus, max_workers=1)
    blocker_ctx = ExecutionContext(tmp_path, tmp_path)
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    ctx = ExecutionContext(tmp_path, worktree, task_id="task-example")
    executed = ThreadEvent()
    try:
        manager.start(blocker_ctx, "blocker", lambda: (str(release.wait(3)), 0))
        manager.start(ctx, "queued", lambda: (str(executed.set()), 0))
        ctx.cwd = tmp_path  # 后续认领变化不能把先前作业的目录占用移走。
        assert not executed.is_set()
        assert manager.is_busy(worktree)
        assert manager.list_jobs()[1]["task_id"] == "task-example"
        release.set()
        assert executed.wait(3)
    finally:
        release.set()
        manager.close()
    assert executed.is_set()
    assert not manager.is_busy(worktree)


def test_scheduler_recovery_replays_unacknowledged_one_shot(tmp_path):
    path, bus = tmp_path / "cron.json", EventBus()
    scheduler = Scheduler(path, bus)
    job = scheduler.schedule("*/5 * * * *", "检查结果", one_shot=True)
    due = datetime.fromisoformat(job["next_run"])
    scheduler.tick(due)
    scheduler.tick(due + timedelta(seconds=1))
    events = bus.drain("lead")
    assert len(events) == 1
    assert events[0].metadata["cron_job_id"] == job["id"]
    scheduler.close()  # 模拟事件已取走、但模型尚未成功处理时退出。

    recovery_bus = EventBus()
    recovered = Scheduler(path, recovery_bus)
    recovered.tick(due + timedelta(seconds=2))
    assert recovery_bus.drain("lead")[0].content == "检查结果"
    recovered.acknowledge(job["id"])
    assert recovered.list_jobs() == []
    recovered.close()
    assert Scheduler(path, EventBus()).list_jobs() == []


def test_scheduler_retry_durable_and_catchup(tmp_path):
    path, bus = tmp_path / "cron.json", EventBus()
    scheduler = Scheduler(path, bus, timezone="UTC")
    job = scheduler.schedule("0 * * * *", "check")
    scheduler.schedule("0 * * * *", "temporary", durable=False)
    now = datetime.fromisoformat(job["next_run"]) + timedelta(days=3)
    scheduler.tick(now)
    assert len(bus.drain("lead")) == 2  # 合并错过的周期，不能补发 72 次。
    scheduler.retry(job["id"])
    scheduler.tick(now)
    assert len(bus.drain("lead")) == 1
    scheduler.ack(job["id"])
    scheduler.tick(now)
    assert not bus.drain("lead")
    scheduler.close()
    recovered = Scheduler(path, EventBus())
    assert len(recovered.list_jobs()) == 1
    assert recovered.list_jobs()[0]["timezone"] == "UTC"
    assert recovered.cancel(job["id"])
    assert not recovered.cancel(job["id"])


def test_scheduler_write_failure_never_publishes_before_retry_saves(tmp_path, monkeypatch):
    scheduler, bus = Scheduler(tmp_path / "cron.json", EventBus()), EventBus()
    scheduler._bus = bus
    job = scheduler.schedule("* * * * *", "check")
    save = scheduler._save

    def fail():
        raise OSError("disk unavailable")

    monkeypatch.setattr(scheduler, "_save", fail)
    for _ in range(2):
        with pytest.raises(OSError):
            scheduler.tick(datetime.fromisoformat(job["next_run"]))
        assert not bus.drain("lead")
    monkeypatch.setattr(scheduler, "_save", save)
    scheduler.tick(datetime.fromisoformat(job["next_run"]))
    assert len(bus.drain("lead")) == 1


def test_failed_ack_keeps_pending_one_shot_retriable(tmp_path, monkeypatch):
    bus = EventBus()
    scheduler = Scheduler(tmp_path / "cron.json", bus)
    job = scheduler.schedule("* * * * *", "check", one_shot=True)
    due = datetime.fromisoformat(job["next_run"])
    scheduler.tick(due)
    bus.drain("lead")
    save = scheduler._save

    def fail():
        raise OSError("disk unavailable")

    monkeypatch.setattr(scheduler, "_save", fail)
    with pytest.raises(OSError):
        scheduler.acknowledge(job["id"])
    assert scheduler.list_jobs()[0]["pending_delivery"]
    monkeypatch.setattr(scheduler, "_save", save)
    scheduler.retry(job["id"])
    scheduler.tick(due)
    assert len(bus.drain("lead")) == 1
    scheduler.acknowledge(job["id"])
    assert not scheduler.list_jobs()


def test_scheduler_deduplicates_persistent_errors_until_recovery(tmp_path, monkeypatch):
    bus, reached = EventBus(), ThreadEvent()
    scheduler = Scheduler(tmp_path / "cron.json", bus, tick_seconds=0.005)
    count = 0

    def tick():
        nonlocal count
        count += 1
        if count >= 5:
            reached.set()
        if count != 3:  # 两次连续失败、一次恢复、再次连续失败。
            raise OSError("disk unavailable")

    monkeypatch.setattr(scheduler, "tick", tick)
    try:
        scheduler.start()
        assert reached.wait(2)
    finally:
        scheduler.close()
    assert [event.kind for event in bus.drain("lead")] == ["scheduler_error", "scheduler_error"]


@pytest.mark.parametrize("expression", ["@daily", "* * * * * *", "99 * * * *", "bad"])
def test_scheduler_rejects_non_five_field_cron(tmp_path, expression):
    with pytest.raises(ValueError):
        Scheduler(tmp_path / "cron.json", EventBus()).schedule(expression, "check")
