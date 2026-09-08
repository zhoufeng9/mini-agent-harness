"""后台命令只执行宿主已经审核过的 callable，不绕过 ShellRunner 的策略。"""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from threading import RLock
from typing import Callable
from uuid import uuid4

from mini_agent_harness.core.types import ExecutionContext

from .events import EventBus


class BackgroundManager:
    """管理后台工作的身份、结果和通知；具体进程终止由 ShellRunner 管理。

    start() 返回后模型可以继续工作。后台线程仅写自己的 job 和 EventBus，
    绝不直接调用模型或修改共享 messages。close(wait=False) 无法强杀 Python
    线程，因此宿主关闭时应先终止 ShellRunner 子进程，再等待本管理器退出。
    """

    def __init__(self, bus: EventBus, max_workers: int = 4) -> None:
        self._bus = bus
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="harness-bg")
        self._lock = RLock()
        self._jobs: dict[str, dict] = {}
        self._futures: dict[str, Future] = {}
        self._closed = False

    def start(self, ctx: ExecutionContext, command: str,
              run: Callable[[], tuple[str, int]]) -> str:
        with self._lock:
            if self._closed:
                raise RuntimeError("BackgroundManager 已关闭")
            job_id = uuid4().hex[:12]
            self._jobs[job_id] = {"id": job_id, "owner": ctx.agent_id,
                                  "command": command, "status": "running",
                                  "cwd": str(ctx.cwd.resolve()), "task_id": ctx.task_id}
            future = self._pool.submit(run)
            self._futures[job_id] = future
            future.add_done_callback(lambda done: self._complete(job_id, done))
            return job_id

    def _complete(self, job_id: str, future: Future) -> None:
        try:
            output, code = future.result()
        except Exception as exc:
            output, code = f"后台工作失败：{type(exc).__name__}: {exc}", -1
        with self._lock:
            job = self._jobs[job_id]
            job.update(status="completed" if code == 0 else "failed", exit_code=code,
                       output=output)
            self._futures.pop(job_id, None)
            self._bus.publish(job["owner"], "background_complete", output, {
                "background_job_id": job_id, "command": job["command"], "exit_code": code,
            })

    def list_jobs(self, owner: str | None = None) -> list[dict]:
        with self._lock:
            return [dict(j) for j in self._jobs.values() if owner is None or j["owner"] == owner]

    def has_pending(self, owner: str | None = None) -> bool:
        with self._lock:
            return any(j["status"] == "running" and (owner is None or j["owner"] == owner)
                       for j in self._jobs.values())

    def is_busy(self, cwd: Path) -> bool:
        """目录占用从入队即开始，而非等 ShellRunner 真正创建 Popen 才开始。

        队列里排队的命令也会写入 cwd。宿主删除 worktree 前必须同时检查这里
        和 ShellRunner.is_busy；入队与删除检查还应使用同一 TaskStore 事务锁。
        """
        directory = str(Path(cwd).resolve())
        with self._lock:
            return any(job["status"] == "running" and job["cwd"] == directory
                       for job in self._jobs.values())

    def close(self, wait: bool = True) -> None:
        with self._lock:
            self._closed = True
        self._pool.shutdown(wait=wait, cancel_futures=True)
