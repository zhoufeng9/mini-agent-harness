"""应用装配入口：把服务接起来，同时明确谁负责创建和关闭资源。

这是 composition root。底层服务不会反向 import Harness，因而无需循环
导入、运行时 monkey patch 或把原版全局变量搬进一个共享万能字典。
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import replace
from uuid import uuid4

from dotenv import dotenv_values

from .config import Settings
from .context import ContextManager, MemoryStore, SkillsCatalog
from .core.agent import AgentLoop, TurnResult
from .core.hooks import HookRegistry
from .core.tools import ToolRegistry
from .core.types import ExecutionContext, ModelProvider, ToolSpec, object_schema
from .mcp import MCPManager
from .models import AnthropicProvider, OpenAIProvider, RetryProvider
from .prompt import assemble_prompt
from .runtime.background import BackgroundManager
from .runtime.events import EventBus
from .runtime.scheduler import Scheduler
from .tasks import TaskStore, WorktreeManager
from .teams import TeamsManager
from .tools.filesystem import FileTools
from .tools.permissions import PermissionPolicy
from .tools.shell import ShellRunner
from .tools.todo import TodoStore

logger = logging.getLogger(__name__)


def create_provider(settings: Settings) -> ModelProvider:
    if not settings.api_key:
        raise ValueError(f"请在本地 .env 设置 {settings.provider.upper()}_API_KEY")

    def build(model):
        kwargs = dict(model=model, api_key=settings.api_key, base_url=settings.base_url,
                      timeout=settings.request_timeout)
        if settings.provider == "anthropic":
            return AnthropicProvider(**kwargs)
        return OpenAIProvider(**kwargs, api_mode=settings.openai_api_mode)

    return RetryProvider(build(settings.model),
        fallback=build(settings.fallback_model) if settings.fallback_model else None,
        max_retries=settings.max_retries)


class Harness:
    """公开 Python API。推荐 `with Harness(settings) as app: app.run(...)`。

    provider 可以注入测试替身；approve 是宿主的审批 UI，业务模块不接触 stdin。
    同一实例的 lead 会话串行，队友各自拥有 history，可与 lead 并行。
    """

    def __init__(self, settings: Settings, *, provider: ModelProvider | None = None,
                 approve=None):
        self._closing = threading.Event()
        self._closed = False
        try:
            self._initialize(settings, provider=provider, approve=approve)
        except BaseException:
            # 装配中途失败也关闭已创建的资源，尤其是可注入的 SDK client。
            self.close()
            raise

    def _initialize(self, settings: Settings, *, provider=None, approve=None):
        self.settings = settings
        self.workspace = settings.workspace.resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.state_dir = self.workspace / ".harness"
        if not self.state_dir.resolve().is_relative_to(self.workspace):
            raise ValueError(".harness directory escapes workspace")
        self.state_dir.mkdir(exist_ok=True)
        for name in ("tasks", "shell", "memory", "context", "sessions", "mailboxes"):
            if not (self.state_dir / name).resolve().is_relative_to(self.state_dir.resolve()):
                raise ValueError(f"Runtime directory escapes state root: {name}")
        self.provider = provider or create_provider(settings)
        self.registry = ToolRegistry()
        self.hooks = HookRegistry()
        self.events = EventBus()
        self._closing = threading.Event()
        self._lead_lock = threading.RLock()
        self._started = False
        self._closed = False
        self.history: list[dict] = []
        self._active_request = "Handle runtime events related to the user's task."
        self._ctx = ExecutionContext(self.workspace, self.workspace)
        self.files = FileTools()
        self.todos = TodoStore()
        self.shell = ShellRunner(self.state_dir / "shell", settings.shell_timeout)
        self.background = BackgroundManager(self.events)
        self.scheduler = Scheduler(self.state_dir / "cron.json", self.events, settings.timezone)
        self.tasks = TaskStore(self.state_dir, self.workspace)
        self.worktrees = WorktreeManager(self.tasks,
            is_busy=lambda cwd: self.shell.is_busy(cwd) or self.background.is_busy(cwd))
        self.teams = TeamsManager(self.tasks, self.events)
        self.skills = SkillsCatalog(self.workspace / "skills")
        self.memory = MemoryStore(self.state_dir / "memory", self.provider)
        self.context = ContextManager(self.state_dir / "context", self.provider,
                                      max_chars=settings.context_chars)
        env = {**(dotenv_values(settings.env_file) if settings.env_file else {}), **os.environ}
        self.mcp = MCPManager(settings.mcp_config or self.workspace / "mcp.json", self.registry,
                              env={k: v for k, v in env.items() if v is not None})
        self.permissions = PermissionPolicy(approve)
        self.hooks.register("PreToolUse", self.permissions.check)
        for service in (self.files, self.todos, self.tasks, self.worktrees, self.teams, self.scheduler):
            service.register_tools(self.registry)
        self.mcp.register_tools()
        self._register_host_tools()
        self.agent = AgentLoop(
            self.provider, self.registry, self.hooks, self.context,
            system_prompt=self._system_prompt, events=self.events,
            scheduler=self.scheduler, teams=self.teams, max_tokens=settings.max_tokens,
            max_steps=settings.max_steps, stopping=self._closing.is_set,
        )
        self.teams.set_runner(self._run_child)

    def _system_prompt(self, ctx, messages) -> str:
        recalled = ""
        if self.settings.memory_enabled:
            try:
                recalled = self.memory.recall(messages)
            except Exception as exc:
                logger.warning("Memory recall skipped: %s", type(exc).__name__)
        return assemble_prompt(ctx, skills=self.skills.catalog(), memory=recalled)

    def _register_host_tools(self) -> None:
        self.registry.register(ToolSpec("bash", "Run a shell command; optional explicit background mode.",
            object_schema({"command": {"type": "string", "minLength": 1},
                "run_in_background": {"type": "boolean"}}, ["command"]),
            self._bash, requires_approval=True))
        self.registry.register(ToolSpec("load_skill", "Load the complete text of a named skill on demand.",
            object_schema({"name": {"type": "string"}}, ["name"]),
            lambda ctx, args: self.skills.load(args["name"])))
        self.registry.register(ToolSpec("compact", "Summarize history after all current tool results return.",
            object_schema(), lambda ctx, args: "Compaction queued after this tool batch."))
        self.registry.register(ToolSpec("task", "Delegate a focused task to an isolated one-shot subagent.",
            object_schema({"description": {"type": "string", "minLength": 1}}, ["description"]),
            self._subagent, roles=frozenset({"lead"})))

    def _bash(self, ctx, args) -> str:
        # 捕获 cwd，后台启动后即使 assignment 更新也不会漂移到新的任务目录。
        cwd, command = ctx.cwd, args["command"]
        if args.get("run_in_background", False):
            # 与 worktree.remove 使用相同事务锁，消除检查空闲与后台入队之间的窗口。
            with self.tasks.transaction():
                if not cwd.is_dir():
                    raise ValueError("Assigned directory no longer exists")
                job_id = self.background.start(ctx, command, lambda: self.shell.run(command, cwd))
            return f"Background task {job_id} started. Completion will arrive as a runtime event."
        output, exit_code = self.shell.run(command, cwd)
        return f"exit_code={exit_code}\n{output}"

    def _subagent(self, ctx, args) -> str:
        child = replace(ctx, agent_id=f"subagent-{uuid4().hex[:8]}", role="subagent",
                        interactive=False, depth=ctx.depth + 1, session_id=uuid4().hex)
        if child.depth > 2:
            raise ValueError("Subagent depth limit reached")
        return self._run_child(args["description"], child, [])

    def _run_child(self, prompt, ctx, history) -> str:
        history.append({"role": "user", "content": prompt})
        result = self.agent.run(history, ctx, prompt)
        if result.status != "completed":
            raise RuntimeError(f"Agent stopped with {result.status}: {result.text}")
        return result.text

    def start(self):
        if self._closed:
            raise RuntimeError("Harness is closed")
        if not self._started:
            self.scheduler.start()
            self._started = True
        return self

    def run(self, prompt: str, *, interactive: bool = False) -> TurnResult:
        """执行用户请求；非交互 API 默认拒绝需要人工批准的工具。"""
        if not prompt.strip():
            raise ValueError("Prompt cannot be empty")
        with self._lead_lock:
            self.start()
            self._active_request = prompt
            self.hooks.emit("UserPromptSubmit", ctx=self._ctx, query=prompt)
            self.history.append({"role": "user", "content": prompt})
            return self._turn(interactive)

    def poll(self) -> TurnResult | None:
        """宿主事件泵：有通知才唤醒。CLI 自动调用；嵌入式应用自行决定调用时机。"""
        with self._lead_lock:
            self.start()
            if not self.events.wait("lead", timeout=0):
                return None
            return self._turn(False)

    def _turn(self, interactive: bool) -> TurnResult:
        self._ctx.interactive = interactive
        try:
            result = self.agent.run(self.history, self._ctx, self._active_request)
            if self.settings.memory_enabled and result.status == "completed":
                try:
                    if self.memory.extract(self.history):
                        self.memory.consolidate()
                except Exception as exc:
                    logger.warning("Memory extraction skipped: %s", type(exc).__name__)
            return result
        finally:
            self._ctx.interactive = False
            self._save_transcript()

    def _save_transcript(self) -> None:
        root = self.state_dir / "sessions"
        if not root.resolve().is_relative_to(self.state_dir.resolve()):
            raise ValueError("Session directory escapes state root")
        root.mkdir(exist_ok=True)
        destination = root / f"{self._ctx.session_id}.json"
        staging = destination.with_suffix(".tmp")
        staging.write_text(json.dumps(self.history, ensure_ascii=False, indent=2), encoding="utf-8")
        staging.replace(destination)

    def new_session(self) -> None:
        with self._lead_lock:
            self.history.clear()
            self._ctx.session_id = uuid4().hex
            self._active_request = "Handle runtime events related to the user's task."

    def close(self) -> None:
        if self._closed:
            return
        self._closing.set()
        # 先停止新事件与命令，再等工作线程，最后释放传输层。
        for name in ("scheduler", "shell", "background", "teams", "mcp", "provider"):
            service = getattr(self, name, None)
            if service is None:
                continue
            try:
                if name == "teams":
                    # 不提前关闭正在被队友请求使用的客户端。阻塞调用受 SDK 超时约束。
                    service.close(timeout=None)
                else:
                    service.close()
            except Exception as exc:
                logger.warning("Cleanup failed: %s", type(exc).__name__)
        self._closed = True

    def __enter__(self):
        return self.start()

    def __exit__(self, *_):
        self.close()
