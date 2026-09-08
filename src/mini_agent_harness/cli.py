"""终端宿主。异步等待输入/通知；实际 Agent Loop 保持易读的同步接口。

任何时刻只有一个 prompt_toolkit 提示在读 stdin。执行前台模型轮时暂停主
输入框，审批通过事件循环转回同一终端，后台 Agent 无权弹出审批。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import sys
import threading

from prompt_toolkit import PromptSession
from prompt_toolkit.patch_stdout import patch_stdout

from .app import Harness
from .config import Settings


def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(description="Mini Agent Harness — 模块化编程 Agent")
    cli.add_argument("--workspace", default=".", help="Agent 工作目录，默认当前目录")
    cli.add_argument("--env-file", help="显式 .env 路径，默认 workspace/.env")
    cli.add_argument("--provider", choices=["anthropic", "openai"])
    cli.add_argument("--model", help="覆盖所选 provider 的模型 ID")
    cli.add_argument("--verbose", action="store_true")
    commands = cli.add_subparsers(dest="command")
    commands.add_parser("chat", help="交互终端（默认）")
    run = commands.add_parser("run", help="执行一轮请求；非 TTY 自动拒绝需要审批的工具")
    run.add_argument("prompt")
    commands.add_parser("doctor", help="检查本地配置，不连接模型、不显示密钥")
    mcp = commands.add_parser("mcp-check", help="连接配置的真实 MCP 并列出工具，无模型调用")
    mcp.add_argument("name")
    return cli


async def chat(settings: Settings) -> int:
    session: PromptSession = PromptSession()
    loop = asyncio.get_running_loop()

    async def confirm(question: str) -> bool:
        try:
            answer = await session.prompt_async(f"{question} [y/N] ")
            return answer.strip().lower() in {"y", "yes"}
        except (EOFError, KeyboardInterrupt):
            return False

    def approve(question: str) -> bool:
        return asyncio.run_coroutine_threadsafe(confirm(question), loop).result()

    with Harness(settings, approve=approve) as app, patch_stdout():
        print(f"mini-agent-harness | {settings.provider} / {settings.model}")
        print("输入请求；/tasks 查看任务，/new 新会话，/quit 退出。")
        saved_input = ""

        async def wait_event():
            while not await asyncio.to_thread(app.events.wait, "lead", 0.5):
                pass

        while True:
            prompt_task = asyncio.create_task(session.prompt_async("agent › ", default=saved_input))
            event_task = asyncio.create_task(wait_event())
            try:
                done, _ = await asyncio.wait({prompt_task, event_task},
                                             return_when=asyncio.FIRST_COMPLETED)
                if prompt_task in done:
                    event_task.cancel()
                    query = prompt_task.result().strip()
                    saved_input = ""
                    if query in {"/quit", "/exit"}:
                        return 0
                    if query == "/new":
                        app.new_session()
                        continue
                    if query == "/tasks":
                        print(json.dumps([task.to_dict() for task in app.tasks.list()],
                                         ensure_ascii=False, indent=2))
                        continue
                    if not query:
                        continue
                    result = await asyncio.to_thread(app.run, query, interactive=True)
                else:
                    saved_input = session.default_buffer.text
                    prompt_task.cancel()
                    await asyncio.gather(prompt_task, return_exceptions=True)
                    result = await asyncio.to_thread(app.poll)
                if result:
                    print(result.text)
                    if result.status != "completed":
                        print(f"[Stopped: {result.status}; steps={result.steps}]")
            except EOFError:
                return 0
            except KeyboardInterrupt:
                return 130
            except Exception as exc:
                # 不输出完整 SDK 请求/响应，避免错误 body 夹带密钥或上下文。
                print(f"[Error: {type(exc).__name__}] 请检查配置、网络或服务状态。", file=sys.stderr)
            finally:
                prompt_task.cancel()
                event_task.cancel()
                await asyncio.gather(prompt_task, event_task, return_exceptions=True)


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")
    # 第三方 HTTP 日志可能包含 URL 查询参数；verbose 也不打开它们的 DEBUG。
    for library in ("httpx", "openai", "anthropic"):
        logging.getLogger(library).setLevel(logging.WARNING)
    previous_sigterm = None
    if threading.current_thread() is threading.main_thread():
        previous_sigterm = signal.getsignal(signal.SIGTERM)

        def terminate(signum, frame):
            raise KeyboardInterrupt

        signal.signal(signal.SIGTERM, terminate)
    try:
        settings = Settings.load(args.workspace, env_file=args.env_file,
                                 provider=args.provider,
                                 model=args.model or ("mcp-only" if args.command == "mcp-check" else None))
        if args.command == "doctor":
            print(f"workspace: {settings.workspace}\nprovider: {settings.provider}\nmodel: {settings.model}")
            print(f".env: {settings.env_file}\nAPI key: {'configured' if settings.api_key else 'missing'}")
            print(f"MCP config: {'present' if settings.mcp_config.is_file() else 'absent (optional)'}")
            return 0 if settings.api_key else 1
        if args.command == "mcp-check":
            # MCP 验证不要求 LLM key；独立创建管理器，退出时关闭其子进程/连接。
            import os

            from dotenv import dotenv_values

            from .core.tools import ToolRegistry
            from .mcp import MCPManager
            env = {**dotenv_values(settings.env_file), **os.environ}
            manager = MCPManager(settings.mcp_config, ToolRegistry(), env=env)
            try:
                print("\n".join(manager.connect(args.name)))
            finally:
                manager.close()
            return 0
        if args.command == "run":
            def approve(question):
                return input(f"{question} [y/N] ").strip().lower() in {"y", "yes"}
            with Harness(settings, approve=approve) as app:
                result = app.run(args.prompt, interactive=sys.stdin.isatty())
                print(result.text)
                if result.status != "completed":
                    print(f"Stopped: {result.status}", file=sys.stderr)
                return 0 if result.status == "completed" else 2
        return asyncio.run(chat(settings))
    except (KeyboardInterrupt, EOFError):
        return 130
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"{type(exc).__name__}: 启动或执行失败，请检查本地配置与服务状态。", file=sys.stderr)
        return 1
    finally:
        if previous_sigterm is not None:
            signal.signal(signal.SIGTERM, previous_sigterm)


if __name__ == "__main__":
    sys.exit(main())
