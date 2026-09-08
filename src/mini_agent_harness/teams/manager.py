"""线程队友的 WORK → result → IDLE 生命周期及宿主协议守卫。

运行模型的方式由 runner 注入，本模块不依赖 SDK。runner 每次模型调用前调用
before_step，并把返回的消息加入 history；每次工具前调用 before_tool；完整响应
中的所有工具执行完后调用 after_step。这样审批、关闭消息不会被连续工具轮饿死。
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from ..core.types import ExecutionContext, ToolSpec, object_schema
from ..tasks.store import TaskStore
from .mailbox import FileMailbox, Mail, validate_name
from .protocols import ProtocolRequest


class TeamStopped(RuntimeError):
    """协作式中断：宿主应退出该队友模型循环，由 worker finally 归还租约。"""


@dataclass
class Teammate:
    name: str
    role: str
    prompt: str
    ctx: ExecutionContext
    require_plan: bool = False
    status: str = "working"
    plan_status: str = "not_required"
    identity: tuple[str | None, int] = (None, 0)
    plan_request_id: str | None = None
    history: list[dict] = field(default_factory=list)
    thread: threading.Thread | None = None


class TeamsManager:
    """状态属于当前宿主；持久任务/邮箱可恢复，线程和审批记录不会跨进程复活。"""

    def __init__(self, store: TaskStore, event_bus,
                 runner: Callable[[str, ExecutionContext, list[dict]], str] | None = None,
                 *, idle_interval: float = 2.0):
        self.store = store
        self.events = event_bus
        self.runner = runner
        self.mailbox = FileMailbox(store.state_dir)
        self.idle_interval = max(0.02, idle_interval)
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._members: dict[str, Teammate] = {}
        self._requests: dict[str, ProtocolRequest] = {}

    def set_runner(self, runner: Callable[[str, ExecutionContext, list[dict]], str]) -> None:
        """应用组合完成后注入 AgentLoop.run，避免模块导入环。"""
        self.runner = runner

    def _member(self, name: str) -> Teammate:
        try:
            return self._members[name]
        except KeyError as exc:
            raise ValueError(f"队友不存在：{name}") from exc

    def _identity(self, name: str) -> tuple[str | None, int]:
        task = self.store.assignment(name)
        return (task.id, task.version) if task else (None, 0)

    def _sync_identity(self, member: Teammate) -> None:
        """总是先任务锁再团队锁；新认领/释放使旧的 plan request 永久失效。"""
        with self.store.transaction():
            identity = self._identity(member.name)
            with self._lock:
                if identity != member.identity:
                    if member.plan_request_id:
                        self._requests[member.plan_request_id].status = "stale"
                    member.identity = identity
                    member.plan_request_id = None
                    member.plan_status = "required" if member.require_plan else "not_required"

    def spawn(self, name: str, role: str, prompt: str, task_id: str | None = None,
              require_plan: bool = False) -> str:
        """指定 task_id 时必须在线程启动前认领成功；不指定时可先运行再进入 IDLE。"""
        validate_name(name)
        if name.casefold() in {"lead", "agent"}:
            raise ValueError("lead/agent 是保留名称")
        if self.runner is None:
            raise RuntimeError("尚未注入队友 runner")
        if self._stop.is_set():
            raise RuntimeError("团队管理器已经关闭")
        # 先建立成员再开线程，且全部异常路径清理自己的占位和任务租约。
        with self.store.transaction():
            with self._lock:
                if any(key.casefold() == name.casefold() for key in self._members):
                    raise ValueError(f"队友名称已存在：{name}")
                ctx = ExecutionContext(self.store.workspace, self.store.workspace,
                                       agent_id=name, role="teammate", interactive=False)
                member = Teammate(name, role, prompt, ctx, require_plan=require_plan,
                                  plan_status="required" if require_plan else "not_required")
                self._members[name] = member
            claimed_here = False
            try:
                if task_id:
                    self.store.claim(task_id, name)
                    claimed_here = True
                elif self.store.assignment(name):
                    raise ValueError("该名称存在上次宿主遗留租约；请先显式恢复或释放")
                self.store.bind_context(ctx)
                self._sync_identity(member)
                member.thread = threading.Thread(target=self._worker, args=(member,),
                                                 name=f"harness-team-{name}", daemon=True)
                member.thread.start()
            except Exception:
                # 只释放本次 spawn 认领的任务，不篡改上次进程遗留租约。
                if claimed_here:
                    self.store.release_owner(name)
                with self._lock:
                    self._members.pop(name, None)
                raise
        assigned = f"，任务 {task_id}" if task_id else "，初始无任务"
        return f"已启动队友 {name}（{role}）{assigned}。可以结束当前轮次，事件会自动送达。"

    def _publish(self, member: Teammate, kind: str, content: str,
                 metadata: dict | None = None) -> None:
        data = {"teammate": member.name, "task_id": member.ctx.task_id, **(metadata or {})}
        self.events.publish("lead", kind, content, data)

    def _worker(self, member: Teammate) -> None:
        """没有固定工具轮上限；一个任务返回后先 IDLE，消息或新任务才再次唤醒。"""
        next_prompt: str | None = member.prompt
        try:
            while not self._stop.is_set():
                if next_prompt is not None:
                    self.store.bind_context(member.ctx)
                    self._sync_identity(member)
                    task = self.store.assignment(member.name)
                    instruction = f"你是队友 {member.name}，职责：{member.role}。\n{next_prompt}"
                    if task:
                        instruction += (f"\n[当前任务 {task.id}] {task.subject}\n{task.description}"
                                        f"\n工作目录：{member.ctx.cwd}。完成后调用 complete_task。")
                    else:
                        instruction += "\n当前没有 assignment；先认领任务才能调用文件或 Shell 工具。"
                    if member.require_plan:
                        instruction += "\n先 submit_plan 并等待 Lead 批准，再执行写入、Shell 或完成任务。"
                    with self._lock:
                        member.status = "working"
                    result = self.runner(instruction, member.ctx, member.history)
                    self.after_step(member.ctx)
                    self._publish(member, "team_result", result or "队友本轮已结束。")
                    next_prompt = None
                with self._lock:
                    if member.status == "stopping":
                        raise TeamStopped(member.name)
                    member.status = ("waiting_approval" if member.plan_status == "pending"
                                     else "idle")
                # 先响应邮箱；只有等待超时且没有当前租约才从任务图领取一个任务。
                if self.mailbox.wait(member.name, self.idle_interval):
                    messages = self.before_step(member.ctx)
                    if messages:
                        next_prompt = "\n".join(m["content"] for m in messages)
                elif self.store.assignment(member.name) is None:
                    task = self.store.claim_next(member.name)
                    if task:
                        self.store.bind_context(member.ctx)
                        self._sync_identity(member)
                        next_prompt = "IDLE 自动认领了新的就绪任务，请开始处理。"
        except TeamStopped:
            self._publish(member, "team_stopped", f"队友 {member.name} 已接受关闭请求。")
        except Exception as exc:
            with self._lock:
                member.status = "failed"
            self._publish(member, "team_error", f"{type(exc).__name__}: {exc}")
        finally:
            self.store.release_owner(member.name)
            self._sync_identity(member)
            self.store.bind_context(member.ctx)
            with self._lock:
                if member.status != "failed":
                    member.status = "stopped"

    def _consume_mail(self, member: Teammate) -> list[dict]:
        messages = []
        for mail in self.mailbox.drain(member.name):
            if mail.kind == "message":
                messages.append({"role": "user", "content": f"[{mail.sender}] {mail.content}"})
            elif mail.kind == "plan_request" and mail.sender == "lead":
                messages.append({"role": "user", "content": f"[需要计划] {mail.content}"})
            elif mail.kind == "plan_response":
                accepted = self._apply_plan_response(member, mail)
                if accepted:
                    messages.append({"role": "user", "content": f"[计划审批] {mail.content}"})
            elif mail.kind == "shutdown_request":
                self._apply_shutdown(member, mail)
        return messages

    def before_step(self, ctx: ExecutionContext) -> list[dict]:
        """模型调用边界检查邮箱；pending 计划在此等待，避免空转请求模型。

        Lead 的通知归 EventBus，subagent 不参与团队协议，因此不会抢它们的事件。
        """
        if ctx.role != "teammate":
            self.store.bind_context(ctx)
            return []
        member = self._member(ctx.agent_id)
        self.store.bind_context(ctx)
        self._sync_identity(member)
        messages = self._consume_mail(member)
        while True:
            with self._lock:
                if self._stop.is_set() or member.status == "stopping":
                    raise TeamStopped(member.name)
                pending = member.plan_status == "pending"
                if pending:
                    member.status = "waiting_approval"
            if not pending:
                return messages
            self.mailbox.wait(member.name, min(self.idle_interval, 0.2))
            messages.extend(self._consume_mail(member))
            self._sync_identity(member)

    def after_step(self, ctx: ExecutionContext) -> None:
        """完整 tool_use 组之后释放已完成租约；绝不能在每个工具之后调用。"""
        if ctx.role == "subagent":
            return
        self.store.release_completed(ctx.agent_id)
        self.store.bind_context(ctx)
        if ctx.role == "teammate":
            self._sync_identity(self._member(ctx.agent_id))

    def before_tool(self, ctx: ExecutionContext, name: str) -> None:
        """目录身份和 plan gate 是宿主规则，不能被消息文本或模型参数更改。"""
        self.store.bind_context(ctx)
        if ctx.role != "teammate":
            return
        member = self._member(ctx.agent_id)
        self._sync_identity(member)
        filesystem_tools = {"bash", "read_file", "write_file", "edit_file", "glob"}
        allowed_before_plan = {"read_file", "glob", "list_tasks", "get_task", "load_skill",
                               "send_message", "submit_plan", "todo_write", "claim_task"}
        with self._lock:
            if self._stop.is_set() or member.status == "stopping":
                raise TeamStopped(member.name)
            if name in filesystem_tools and not ctx.task_id:
                raise PermissionError("队友没有 assignment，不能使用文件或 Shell 工具")
            if member.plan_status not in {"not_required", "approved"}:
                if name not in allowed_before_plan:
                    raise PermissionError(f"当前计划状态 {member.plan_status}，请先获得 Lead 审批")

    def send_message(self, sender: str, to: str, content: str) -> str:
        """普通消息不改变任务身份，不具有审批或关闭权限。"""
        if sender != "lead":
            self._member(sender)
        if to == "lead":
            self.events.publish("lead", "team_message", content, {"sender": sender})
        else:
            target = self._member(to)
            if target.status in {"failed", "stopped"}:
                raise ValueError(f"队友 {to} 已停止")
            self.mailbox.send(sender, to, content)
        return f"消息已发送给 {to}"

    def submit_plan(self, name: str, plan: str) -> str:
        """记录绑定当前任务版本的审批请求；不自动授予执行权。"""
        if not isinstance(plan, str) or not plan.strip():
            raise ValueError("计划不能为空")
        member = self._member(name)
        with self.store.transaction():
            self._sync_identity(member)
            with self._lock:
                if member.identity[0] is None:
                    raise ValueError("先认领具体任务，再提交该任务的计划")
                if member.plan_status == "pending":
                    raise ValueError("已有计划等待审批")
                request = ProtocolRequest("plan", name, "lead", member.identity, plan)
                self._requests[request.id] = request
                member.require_plan = True
                member.plan_status = "pending"
                member.plan_request_id = request.id
                member.status = "waiting_approval"
        self._publish(member, "plan_approval_request", plan, {"request_id": request.id})
        return f"计划已提交，request_id={request.id}。请等待 Lead 审批。"

    def request_plan(self, teammate: str, task: str) -> str:
        """Lead 可立即收紧权限；已经运行中的单条工具不会被强行回滚。"""
        member = self._member(teammate)
        with self.store.transaction():
            self._sync_identity(member)
            with self._lock:
                if member.plan_request_id:
                    self._requests[member.plan_request_id].status = "stale"
                member.require_plan = True
                member.plan_status = "required"
                member.plan_request_id = None
        self.mailbox.send("lead", teammate, task, "plan_request")
        return f"已要求 {teammate} 提交计划"

    def review_plan(self, request_id: str, approve: bool, feedback: str = "") -> str:
        """仅接受当前 assignment 的未决请求，禁止迟到审批放行新任务。"""
        with self.store.transaction():
            with self._lock:
                request = self._requests.get(request_id)
                if request is None or request.kind != "plan":
                    raise ValueError("计划请求不存在")
                member = self._member(request.sender)
            self._sync_identity(member)
            with self._lock:
                if (request.status != "pending" or member.plan_request_id != request.id
                        or member.identity != request.identity):
                    raise ValueError("审批请求已处理、已被替换，或属于旧 assignment")
                request.status = "approved" if approve else "rejected"
        content = feedback or ("计划已批准。" if approve else "计划未通过，请修订后重新提交。")
        self.mailbox.send("lead", member.name, content, "plan_response",
                          {"request_id": request_id, "approve": approve})
        return f"计划 {request_id} 已{request.status}"

    def _apply_plan_response(self, member: Teammate, mail: Mail) -> bool:
        with self.store.transaction():
            self._sync_identity(member)
            with self._lock:
                request_id = mail.metadata.get("request_id")
                request = self._requests.get(request_id)
                if not (mail.sender == "lead" and mail.recipient == member.name and request
                        and request.kind == "plan" and request.sender == member.name
                        and request.target == "lead" and request.identity == member.identity
                        and request_id == member.plan_request_id
                        and request.status in {"approved", "rejected"}
                        and mail.metadata.get("approve") == (request.status == "approved")):
                    return False
                member.plan_status = request.status
                member.plan_request_id = None
                member.status = "working"
                return True

    def request_shutdown(self, teammate: str) -> str:
        member = self._member(teammate)
        with self.store.transaction():
            identity = self._identity(teammate)
            with self._lock:
                if member.status in {"failed", "stopped", "stopping"}:
                    raise ValueError("队友已停止或正在关闭")
                request = ProtocolRequest("shutdown", "lead", teammate, identity)
                self._requests[request.id] = request
        self.mailbox.send("lead", teammate, "请结束当前步骤并退出。", "shutdown_request",
                          {"request_id": request.id})
        return f"已请求关闭 {teammate}，request_id={request.id}"

    def _apply_shutdown(self, member: Teammate, mail: Mail) -> None:
        with self._lock:
            request = self._requests.get(mail.metadata.get("request_id"))
            if (mail.sender != "lead" or mail.recipient != member.name or request is None
                    or request.kind != "shutdown" or request.sender != "lead"
                    or request.target != member.name or request.status != "pending"):
                return
            request.status = "approved"
            member.status = "stopping"
        self._publish(member, "shutdown_response", "已确认关闭请求。", {"request_id": request.id})
        raise TeamStopped(member.name)

    def list_teammates(self) -> list[dict]:
        with self._lock:
            return [{"name": member.name, "role": member.role, "status": member.status,
                     "plan_status": member.plan_status, "task_id": member.identity[0]}
                    for member in self._members.values()]

    def close(self, timeout: float | None = 5.0) -> None:
        """协作式停止并等待线程。正在进行的模型/工具调用由其自身超时终止。

        不会提前释放仍在运行的线程租约，否则其他 worker 可能与旧调用写同一目录。
        """
        self._stop.set()
        self.mailbox.wake()
        deadline = time.monotonic() + timeout if timeout is not None else None
        with self._lock:
            threads = [member.thread for member in self._members.values() if member.thread]
        for thread in threads:
            if thread is not threading.current_thread():
                thread.join(max(0, deadline - time.monotonic()) if deadline is not None else None)

    def register_tools(self, registry) -> None:
        """建队/改图/审批只有 Lead；队友只能传消息、提交自己的计划。"""
        text = {"type": "string"}
        lead = frozenset({"lead"})
        workers = frozenset({"lead", "teammate"})
        specs = [
            ToolSpec("spawn_teammate", "启动持久队友，可指定 ready task 及先计划后执行。",
                     object_schema({"name": text, "role": text, "prompt": text,
                                    "task_id": text, "require_plan": {"type": "boolean"}},
                                   ["name", "role", "prompt"]),
                     lambda ctx, a: self.spawn(**a), roles=lead),
            ToolSpec("list_teammates", "查询队友状态；通常等待自动通知即可，无需反复轮询。",
                     object_schema(), lambda ctx, a: json.dumps(self.list_teammates(),
                                                                ensure_ascii=False), roles=lead),
            ToolSpec("send_message", "给 Lead 或活动队友发送普通消息，不能通过消息授予审批。",
                     object_schema({"to": text, "content": text}, ["to", "content"]),
                     lambda ctx, a: self.send_message(ctx.agent_id, a["to"], a["content"]),
                     roles=workers),
            ToolSpec("submit_plan", "提交当前任务计划并等待 Lead 审批。",
                     object_schema({"plan": text}, ["plan"]),
                     lambda ctx, a: self.submit_plan(ctx.agent_id, a["plan"]),
                     roles=frozenset({"teammate"})),
            ToolSpec("request_plan", "要求队友在继续写入或 Shell 之前先提交计划。",
                     object_schema({"teammate": text, "task": text}, ["teammate", "task"]),
                     lambda ctx, a: self.request_plan(**a), roles=lead),
            ToolSpec("review_plan", "批准或驳回与队友当前任务版本匹配的计划。",
                     object_schema({"request_id": text, "approve": {"type": "boolean"},
                                    "feedback": text}, ["request_id", "approve"]),
                     lambda ctx, a: self.review_plan(**a), roles=lead),
            ToolSpec("request_shutdown", "请求队友在下一个模型边界确认退出，并归还未完成任务。",
                     object_schema({"teammate": text}, ["teammate"]),
                     lambda ctx, a: self.request_shutdown(**a), roles=lead),
        ]
        for spec in specs:
            registry.register(spec)
