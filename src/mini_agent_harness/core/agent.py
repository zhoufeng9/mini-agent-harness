"""一个循环，多个机制：模型决定行动；宿主校验、执行、回填和恢复。

主 Agent、隔离 subagent、持久 teammate 共用这个循环。每次调用的计数器、
messages 和执行身份都在栈上，服务实例只持有其职责范围内的共享状态。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Callable

from ..models import ContextOverflowError
from .types import ExecutionContext, Message, ModelProvider

logger = logging.getLogger(__name__)


@dataclass
class TurnResult:
    text: str
    messages: list[Message]
    steps: int
    status: str = "completed"


class AgentLoop:
    def __init__(self, provider: ModelProvider, registry, hooks, context_manager,
                 *, system_prompt: Callable[[ExecutionContext, list[Message]], str],
                 events=None, scheduler=None, teams=None, max_tokens: int = 8000,
                 max_steps: int = 100, stopping: Callable[[], bool] | None = None):
        self.provider = provider
        self.registry = registry
        self.hooks = hooks
        self.context_manager = context_manager
        self.system_prompt = system_prompt
        self.events = events
        self.scheduler = scheduler
        self.teams = teams
        self.max_tokens = max_tokens
        self.max_steps = max_steps
        self.stopping = stopping or (lambda: False)

    def run(self, messages: list[Message], ctx: ExecutionContext,
            active_request: str) -> TurnResult:
        pending_crons: set[str] = set()
        reactive_attempted = False
        generation_budget = self.max_tokens
        continuations = 0
        last_text = ""
        try:
            for step in range(1, self.max_steps + 1):
                if self.stopping():
                    return TurnResult(last_text, messages, step - 1, "stopped")
                if self.teams:
                    messages.extend({**message, "origin": "runtime"}
                                    for message in self.teams.before_step(ctx))
                if self.events:
                    for event in self.events.drain(ctx.agent_id):
                        messages.append({"role": "user", "origin": "runtime", "content":
                            f"[Runtime event: {event.kind}]\n"
                            f"Metadata: {json.dumps(event.metadata, ensure_ascii=False)}\n{event.content}"})
                        if event.metadata.get("cron_job_id"):
                            pending_crons.add(event.metadata["cron_job_id"])
                            active_request += f"\nScheduled request: {event.content}"
                messages[:] = self.context_manager.prepare(messages, active_request)
                try:
                    system = self.system_prompt(ctx, messages)
                    if self.stopping():
                        return TurnResult(last_text, messages, step - 1, "stopped")
                    response = self.provider.generate(
                        messages, system=system,
                        tools=self.registry.schemas(ctx), max_tokens=generation_budget,
                    )
                except ContextOverflowError:
                    if reactive_attempted:
                        raise
                    messages[:] = self.context_manager.reactive_compact(messages, active_request)
                    reactive_attempted = True
                    continue
                # 收到成功模型响应才确认 durable cron，失败则由 finally 重新投递。
                if self.scheduler:
                    for job_id in pending_crons:
                        self.scheduler.acknowledge(job_id)
                pending_crons.clear()
                truncated = response.stop_reason in {"max_tokens", "length", "incomplete"}
                if truncated and generation_budget == self.max_tokens:
                    generation_budget = self.max_tokens * 2
                    continue  # 丢弃尚未执行的截断输出，以更大预算重试同一输入。
                messages.append({"role": "assistant", "content": response.content})
                last_text = response.text or last_text
                calls = [block for block in response.content if block.get("type") == "tool_use"]
                if not calls and not truncated:
                    if self.teams:
                        self.teams.after_step(ctx)
                    return TurnResult(last_text, messages, step)
                results = []
                compact_requested = False
                # 同一响应内顺序执行，保证 write→read 等有依赖的工具不会竞态。
                # 并行由独立队友或显式后台工具提供。
                for call in calls:
                    error = False
                    name = call.get("name", "")
                    args = call.get("input", {})
                    try:
                        if truncated:
                            raise ValueError("Tool call was truncated; regenerate complete arguments")
                        if self.stopping():
                            raise RuntimeError("Harness is stopping")
                        spec = self.registry.validate(name, args, ctx)
                        if self.teams:
                            self.teams.before_tool(ctx, name)
                        blocked = self.hooks.emit("PreToolUse", ctx=ctx, spec=spec, args=args)
                        if blocked:
                            raise PermissionError(blocked)
                        output = str(spec.handler(ctx, args))
                        self.hooks.emit("PostToolUse", ctx=ctx, spec=spec, args=args, output=output)
                        compact_requested |= name == "compact"
                    except Exception as exc:
                        error = True
                        output = f"{type(exc).__name__}: {exc}"
                    results.append({"type": "tool_result", "tool_use_id": call["id"],
                                    "content": output, "is_error": error})
                if results:
                    messages.append({"role": "user", "content": results})
                if self.teams:
                    self.teams.after_step(ctx)
                if compact_requested:
                    messages[:] = self.context_manager.compact(messages, active_request)
                if truncated:
                    continuations += 1
                    if continuations >= 2:
                        return TurnResult(last_text, messages, step, "length_limit")
                    messages.append({"role": "user", "content":
                        "Continue from the previous response. Reissue truncated tools with complete "
                        "arguments. Do not repeat completed work."})
                else:
                    generation_budget = self.max_tokens
            return TurnResult(last_text, messages, self.max_steps, "step_limit")
        finally:
            if self.scheduler:
                for job_id in pending_crons:
                    self.scheduler.retry(job_id)
            self.hooks.emit("Stop", ctx=ctx, messages=messages)
