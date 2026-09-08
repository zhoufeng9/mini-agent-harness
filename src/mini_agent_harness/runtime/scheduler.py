"""持久 cron 调度：时间到达只投递提示，模型工作仍在 Agent 主循环执行。"""

from __future__ import annotations

import json
import os
import tempfile
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from threading import Event as ThreadEvent
from threading import RLock, Thread
from uuid import uuid4
from zoneinfo import ZoneInfo

from croniter import croniter

from mini_agent_harness.core.types import ToolSpec, object_schema

from .events import EventBus


class Scheduler:
    """单宿主进程使用的定时器，支持五字段 cron 和至少一次投递。

    顺序是：写 pending_delivery 到磁盘 → 发布事件 → 主循环成功请求模型
    → acknowledge()。进程在中间崩溃时，重启会重新投递 pending 任务。
    因此业务操作必须能接受重复，不能把这称为 exactly-once。每个 job 最多
    挂起一次；宕机错过多个周期时合并为一次，避免恢复时大量补跑。
    durable=False 的任务只存在当前实例内。该文件不支持多进程共同写入。
    """

    def __init__(self, path: Path, bus: EventBus, timezone: str = "Asia/Shanghai",
                 recipient: str = "lead", tick_seconds: float = 1.0) -> None:
        self.path = Path(path)
        self._bus, self._recipient = bus, recipient
        self._zone = ZoneInfo(timezone)
        self._tick_seconds = tick_seconds
        self._lock = RLock()
        self._stop = ThreadEvent()
        self._thread: Thread | None = None
        self._closed = False
        self._jobs: dict[str, dict] = {}
        self._delivered: set[str] = set()
        if self.path.exists():
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if data.get("version") != 1:
                raise ValueError("不支持的 cron 状态文件版本")
            for job in data["jobs"]:
                self._validate_cron(job["cron"])
                ZoneInfo(job["timezone"])
                self._jobs[job["id"]] = job

    @staticmethod
    def _validate_cron(expression: str) -> None:
        if len(expression.split()) != 5 or not croniter.is_valid(expression):
            raise ValueError("cron 必须是有效的五字段表达式：分 时 日 月 周")

    def _now(self) -> datetime:
        return datetime.now(self._zone)

    @staticmethod
    def _next(expression: str, now: datetime) -> str:
        return croniter(expression, now).get_next(datetime).isoformat()

    def _save(self) -> None:
        """同目录临时文件 + fsync + 原子替换，避免半写入 JSON。调用方持锁。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "jobs": [j for j in self._jobs.values() if j["durable"]]}
        fd, temporary = tempfile.mkstemp(prefix=".cron-", suffix=".json", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def schedule(self, cron: str, prompt: str, durable: bool = True,
                 one_shot: bool = False) -> dict:
        self._validate_cron(cron)
        if not prompt.strip():
            raise ValueError("定时任务提示不能为空")
        with self._lock:
            if self._closed:
                raise RuntimeError("Scheduler 已关闭")
            job = {"id": uuid4().hex[:12], "cron": cron, "prompt": prompt,
                   "timezone": str(self._zone), "durable": durable, "one_shot": one_shot,
                   "next_run": self._next(cron, self._now()), "pending_delivery": False}
            self._jobs[job["id"]] = job
            try:
                self._save()
            except Exception:
                del self._jobs[job["id"]]
                raise
            return dict(job)

    def list_jobs(self) -> list[dict]:
        with self._lock:
            return [dict(job) for job in self._jobs.values()]

    def cancel(self, job_id: str) -> bool:
        """取消后续调度；已经被主循环取走的事件无法撤回。"""
        with self._lock:
            removed = self._jobs.pop(job_id, None)
            try:
                self._save()
            except Exception:
                if removed is not None:
                    self._jobs[job_id] = removed
                raise
            self._delivered.discard(job_id)
            return removed is not None

    def tick(self, now: datetime | None = None) -> None:
        """公开一次 tick 方便嵌入宿主与确定性测试；生产通常由 start 驱动。"""
        now = now or self._now()
        if now.tzinfo is None:
            raise ValueError("tick 时间必须带时区")
        with self._lock:
            if self._closed:
                return
            for job in self._jobs.values():
                if not job["pending_delivery"] and datetime.fromisoformat(job["next_run"]) <= now:
                    job["pending_delivery"] = True
                    job["next_run"] = self._next(job["cron"], now.astimezone(ZoneInfo(job["timezone"])))
                if job["pending_delivery"] and job["id"] not in self._delivered:
                    # 即使上一次写盘失败，重试也必须先保存，不能凭内存标记直接投递。
                    self._save()
                    self._bus.publish(self._recipient, "cron", job["prompt"], {
                        "cron_job_id": job["id"], "scheduled": True,
                    })
                    self._delivered.add(job["id"])

    def acknowledge(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or not job["pending_delivery"]:
                return
            before = deepcopy(job)
            if job["one_shot"]:
                del self._jobs[job_id]
            else:
                job["pending_delivery"] = False
            try:
                self._save()
            except Exception:
                # ack 未写盘成功就仍属于待确认，不能在当前进程默默丢失。
                self._jobs[job_id] = before
                raise
            self._delivered.discard(job_id)

    ack = acknowledge

    def retry(self, job_id: str) -> None:
        """模型请求失败后允许下次 tick 重投，pending 标记一直保留在磁盘。"""
        with self._lock:
            self._delivered.discard(job_id)

    def start(self) -> None:
        with self._lock:
            if self._closed:
                raise RuntimeError("Scheduler 已关闭")
            if self._thread is not None:
                return
            self._thread = Thread(target=self._run, name="harness-cron", daemon=True)
            self._thread.start()

    def _run(self) -> None:
        last_error: type[Exception] | None = None
        while not self._stop.is_set():
            try:
                self.tick()
                last_error = None
            except Exception as exc:
                # CLI 会把 runtime event 送给模型。磁盘持续不可写时不能每秒
                # 发同一错误，否则空闲宿主会不断产生无意义的模型请求。
                if type(exc) is not last_error:
                    self._bus.publish(self._recipient, "scheduler_error",
                                      f"定时调度失败：{type(exc).__name__}")
                    last_error = type(exc)
            self._stop.wait(self._tick_seconds)

    def close(self) -> None:
        self._stop.set()
        with self._lock:
            self._closed = True
        if self._thread is not None:
            self._thread.join(timeout=max(5.0, self._tick_seconds + 1))
            if self._thread.is_alive():
                raise RuntimeError("定时调度线程未能按时关闭")

    def register_tools(self, registry) -> None:
        registry.register(ToolSpec(
            "schedule_cron", "创建五字段 cron 定时提示；仅 Harness 运行时触发。", object_schema({
                "cron": {"type": "string"}, "prompt": {"type": "string"},
                "durable": {"type": "boolean"}, "one_shot": {"type": "boolean"},
            }, ["cron", "prompt"]),
            lambda ctx, args: json.dumps(self.schedule(**args), ensure_ascii=False),
            requires_approval=True, roles=frozenset({"lead"}),
        ))
        registry.register(ToolSpec(
            "list_crons", "列出当前定时任务和待确认投递。", object_schema(),
            lambda ctx, args: json.dumps(self.list_jobs(), ensure_ascii=False),
            roles=frozenset({"lead"}),
        ))
        registry.register(ToolSpec(
            "cancel_cron", "删除指定任务的后续调度。", object_schema({
                "job_id": {"type": "string"},
            }, ["job_id"]),
            lambda ctx, args: json.dumps({"cancelled": self.cancel(args["job_id"])}),
            requires_approval=True, roles=frozenset({"lead"}),
        ))
