# 开发、测试与运行边界

## 怎样增加一个工具

工具同时注册描述、JSON Schema、执行函数和可见角色，避免原版两张表逐渐不一致。

```python
from mini_agent_harness.core.types import ToolSpec, object_schema

def count_characters(ctx, args):
    return str(len(args["text"]))

app.registry.register(ToolSpec(
    name="count_characters",
    description="Count Unicode characters in text.",
    input_schema=object_schema({"text": {"type": "string"}}, ["text"]),
    handler=count_characters,
))
```

处理函数返回字符串；异常由主循环转成 `is_error=true` 的结果。工具参数首先经过 Schema 校验，
然后经过身份守卫与 PreToolUse，批准后才执行。修改文件时使用 `ExecutionContext.cwd`，
不要直接使用 `Path.cwd()`，因为队友可能正在任务 worktree 中工作。
有外部副作用且需要人工批准的工具设置 `requires_approval=True`。

## 怎样增加 hook 或模型适配器

`app.hooks.register("PreToolUse", callback)` 的 callback 接收 `ctx/spec/args`，
返回非空字符串会拒绝执行。异常同样阻止执行，避免检查失败时变成默认允许。
PostToolUse 还接收 `output`；UserPromptSubmit 接收 `ctx/query`；Stop 接收 `ctx/messages`。
后置 hook 的异常只记日志，因为工具副作用可能已经完成，不能让模型误以为它没有发生。

模型适配器实现 `ModelProvider.generate(messages, *, system, tools, max_tokens)` 和 `close()`，
返回 `ModelResponse`。循环只认识 `text`、`tool_use`、`tool_result`，SDK 对象转换留在适配层。
参考 `examples/offline_demo.py` 的脚本模型即可测试新的宿主行为。

## 运行状态存在哪里

| 位置 | 内容 | 是否提交 Git |
| --- | --- | --- |
| `.env`、`mcp.json` | 本地凭据和连接配置 | 否 |
| `.harness/tasks/` | 任务记录、依赖、owner、worktree 租约 | 否 |
| `.harness/mailboxes/` | 队友文件邮箱 | 否 |
| `.harness/cron.json` | 持久 cron 与 pending delivery | 否 |
| `.harness/memory/` | 长期记忆 Markdown 与目录 | 否 |
| `.harness/context/` | 压缩历史与完整工具输出 | 否 |
| `.harness/shell/` | 命令完整输出 | 否 |
| `.harness/sessions/` | 当前 lead 会话 JSON 快照 | 否 |
| `.worktrees/` | 任务绑定工作副本 | 否 |
| `skills/` | 可版本管理的参考技能 | 是 |

会话快照是归档，不实现 CLI 自动恢复会话；任务、记忆和 durable cron 则由对应服务读取。
不要仅因为主进程退出就删除遗留任务租约：异常终止后需先检查是否仍有进程在写目录，
再通过宿主 `app.tasks.release_owner(owner)` 显式释放。队友线程与待审批协议不会跨重启自动复活。

worktree 需要工作目录本身是已有提交的 Git 仓库。创建会产生 `harness/<name>` 分支。
删除接口只提供给 Python 宿主：`app.worktrees.remove(name)` 会检查 owner、租约、后台占用与 Git 状态；
丢弃改动需要 `discard_changes=True, confirmed=True`。删除工作副本后保留分支。

## 并发与交付边界

同一 lead 的消息列表通过锁串行访问；队友各持独立历史。每一组模型工具调用顺序执行，
一次性 subagent 为上下文隔离，队友线程和显式后台命令提供并发。
任务文件更新使用可重入线程锁和 POSIX 文件锁；cron 是单宿主调度器，
不支持多个 Harness 同时对同一份 cron.json 竞争调度。

一项任务完成时，其 cwd 租约保持到当前整组工具结果回填，防止后续工具突然切换目录。
后台任务从排队开始占用 cwd，因此尚未创建 Shell 进程的排队工作也会阻止 worktree 删除。

durable cron 在投递前写入 pending 标记，收到成功模型响应才 ack。模型调用失败会重新投递；
崩溃发生在响应成功与 ack 落盘之间也可能重复投递。业务动作需要幂等。
停机期间多个漏掉的周期合并为一次，不逐个追补。

关闭顺序先停止调度与 Shell，再等待后台任务和队友，最后关闭 MCP 与模型客户端。
SDK 请求不可立即强制取消，正常退出可能需要等待其超时和有限重试。
Ctrl-C、SIGTERM 经过 CLI 的清理路径；SIGKILL、断电无法执行 Python finally。

## 测试策略

```bash
python -m pip install -e '.[dev]'
python -m pytest -q
ruff check .
python -m build --no-isolation
```

`requirements.lock` 记录本项目验证环境中的精确依赖版本，包含测试/构建工具。
需要复现该环境时可先 `pip install -r requirements.lock`，再 `pip install -e . --no-deps --no-build-isolation`。

| 测试 | 验证内容 |
| --- | --- |
| `test_agent_app.py` | 真实装配、工具轮、审批、子 Agent、后台通知、长度与步数恢复 |
| `test_config_tools.py` | `.env` 优先级、路径边界、唯一文本编辑、Shell 超时与归档 |
| `test_providers.py` | 两家 SDK 载荷、工具参数、原生推理项、截断与重试 |
| `test_context.py` | 记忆、技能、调用/结果配对、大输出与压缩恢复 |
| `test_tasks_teams.py` | DAG、owner、多进程认领、任务租约、计划审批和队友生命周期 |
| `test_worktrees.py` | 临时真实 Git 仓库中的创建、绑定、删除与保护 |
| `test_runtime.py` | 后台队列、cron 校验、重启恢复、ack 与失败重投 |
| `test_mcp.py` | 真实 stdio/HTTP、分页、超时、错误、清理和名称冲突 |

模型测试没有调用真实付费 API；验证的是转换契约和宿主行为，不代表用户账号连通性已验证。
MCP 测试则执行真正的本地协议通信，HTTP 测试必须有本机端口绑定权限。

## 范围

这是可继续开发的单机框架，包含 s15 的主要机制，未加入 Web UI、HTTP 宿主服务、Windows、
分布式任务队列、容器隔离或 s16/s17 的 workflow/goal runtime。
这些扩展可以通过现有服务协议实现，不需要把业务编排重新塞回模型循环。
