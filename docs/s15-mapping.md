# s15 机制与新框架的对应关系

学完 [shareAI-lab/learn-claude-code](https://github.com/shareAI-lab/learn-claude-code) 后，我以 `s15_integrated_harness` 为基础，把集成在一个文件里的代码整理成了这个项目。这份对照记录了原来的功能分别搬到了哪里，方便回头阅读和比较。参考版本是 **`0dcafa2ae053a1ddd6a72f265431104b08a5aa13`**，链接都固定到这次提交。

- [该版本的 s15 代码](https://github.com/shareAI-lab/learn-claude-code/blob/0dcafa2ae053a1ddd6a72f265431104b08a5aa13/s15_integrated_harness/code.py)
- [该版本的 s15 中文说明](https://github.com/shareAI-lab/learn-claude-code/blob/0dcafa2ae053a1ddd6a72f265431104b08a5aa13/s15_integrated_harness/README.zh.md)
- [s15 原先引用的 s09 记忆代码](https://github.com/shareAI-lab/learn-claude-code/blob/0dcafa2ae053a1ddd6a72f265431104b08a5aa13/s09_memory/code.py)
- [上游 MIT 许可证](https://github.com/shareAI-lab/learn-claude-code/blob/0dcafa2ae053a1ddd6a72f265431104b08a5aa13/LICENSE)

本项目沿用 MIT 许可，并保留上游来源和版权声明。状态文件和部分工具参数在拆分时做了调整，旧数据的迁移说明见下文。

## 1. 原版 26 个 Lead 内置工具

按该版本的 `BUILTIN_TOOLS` 定义，Lead 有 26 个内置工具。我保留了这些工具名，将注册和实现放到各自负责的模块中。

| # | s15 工具 | 新框架的注册/执行入口 | 说明 |
|---:|---|---|---|
| 1 | `bash` | [`app.py`](../src/mini_agent_harness/app.py) → [`tools/shell.py`](../src/mini_agent_harness/tools/shell.py) | 显式前台审批；`run_in_background` 交给 `BackgroundManager`；执行时固定 cwd |
| 2 | `read_file` | [`tools/filesystem.py`](../src/mini_agent_harness/tools/filesystem.py) | 按身份目录读取，增加带行号分页；offset 从 1 开始 |
| 3 | `write_file` | [`tools/filesystem.py`](../src/mini_agent_harness/tools/filesystem.py) | 创建或覆盖工作目录内文件，阻止修改宿主状态和 Git 元数据 |
| 4 | `edit_file` | [`tools/filesystem.py`](../src/mini_agent_harness/tools/filesystem.py) | 原文须唯一匹配，避免无意替换多个位置 |
| 5 | `glob` | [`tools/filesystem.py`](../src/mini_agent_harness/tools/filesystem.py) | cwd 内查找，过滤常见内部目录并限制返回数量 |
| 6 | `todo_write` | [`tools/todo.py`](../src/mini_agent_harness/tools/todo.py) | 按 session/agent 分开保存当前清单；最多一个 in_progress |
| 7 | `task` | [`app.py`](../src/mini_agent_harness/app.py) 的 `_subagent()` | 一次性隔离 subagent；名称中的 task 不是持久任务记录 |
| 8 | `load_skill` | [`app.py`](../src/mini_agent_harness/app.py) → [`context/skills.py`](../src/mini_agent_harness/context/skills.py) | 根据目录按需读取技能全文，不执行技能中的代码 |
| 9 | `compact` | [`app.py`](../src/mini_agent_harness/app.py) → [`core/agent.py`](../src/mini_agent_harness/core/agent.py) / [`context/manager.py`](../src/mini_agent_harness/context/manager.py) | 先回填完整工具组，再归档并压缩历史 |
| 10 | `create_task` | [`tasks/store.py`](../src/mini_agent_harness/tasks/store.py) | Lead 创建稳定 ID 的持久任务节点 |
| 11 | `update_task` | [`tasks/store.py`](../src/mini_agent_harness/tasks/store.py) | Lead 通过 `addBlockedBy` 添加依赖，拒绝缺失节点与环 |
| 12 | `list_tasks` | [`tasks/store.py`](../src/mini_agent_harness/tasks/store.py) | Lead/teammate 读取任务图 |
| 13 | `get_task` | [`tasks/store.py`](../src/mini_agent_harness/tasks/store.py) | 读取单条持久任务 |
| 14 | `claim_task` | [`tasks/store.py`](../src/mini_agent_harness/tasks/store.py) | 以 ctx.agent_id 原子认领，绑定任务目录与租约 |
| 15 | `complete_task` | [`tasks/store.py`](../src/mini_agent_harness/tasks/store.py) + [`teams/manager.py`](../src/mini_agent_harness/teams/manager.py) | owner 校验与计划守卫分层；目录租约在完整工具组后释放 |
| 16 | `schedule_cron` | [`runtime/scheduler.py`](../src/mini_agent_harness/runtime/scheduler.py) | 五字段 cron、时区、durable/one_shot；需前台审批 |
| 17 | `list_crons` | [`runtime/scheduler.py`](../src/mini_agent_harness/runtime/scheduler.py) | 显示调度状态与 pending_delivery |
| 18 | `cancel_cron` | [`runtime/scheduler.py`](../src/mini_agent_harness/runtime/scheduler.py) | 取消后续调度；已被主循环取走的事件不能撤回；需前台审批 |
| 19 | `spawn_teammate` | [`teams/manager.py`](../src/mini_agent_harness/teams/manager.py) | 独立线程、可选 task_id、可选 require_plan |
| 20 | `list_teammates` | [`teams/manager.py`](../src/mini_agent_harness/teams/manager.py) | 查看工作、IDLE、待审批、失败与停止状态 |
| 21 | `send_message` | [`teams/manager.py`](../src/mini_agent_harness/teams/manager.py) | 普通消息；不改变任务身份或审批权限 |
| 22 | `request_shutdown` | [`teams/manager.py`](../src/mini_agent_harness/teams/manager.py) / [`teams/protocols.py`](../src/mini_agent_harness/teams/protocols.py) | 请求/确认关闭，下一安全点退出并归还未完成任务 |
| 23 | `request_plan` | [`teams/manager.py`](../src/mini_agent_harness/teams/manager.py) | Lead 收紧队友 plan gate，要求先提交计划 |
| 24 | `review_plan` | [`teams/manager.py`](../src/mini_agent_harness/teams/manager.py) / [`teams/protocols.py`](../src/mini_agent_harness/teams/protocols.py) | 核对 request_id 与当前任务版本后批准/拒绝 |
| 25 | `create_worktree` | [`tasks/worktrees.py`](../src/mini_agent_harness/tasks/worktrees.py) | 为未认领任务建立并验证独立 checkout，分支为 harness/name |
| 26 | `connect_mcp` | [`mcp/client.py`](../src/mini_agent_harness/mcp/client.py) | 由 mock 改为连接宿主配置中的真实服务器；连接本身需前台审批 |

### MCP 管理工具与角色分工

新框架额外注册两个 MCP 管理工具：

| 新工具 | 模块 | 用途 |
|---|---|---|
| `list_mcp_servers` | `mcp/client.py` | 查询已配置服务器、连接状态和当前工具名；不返回凭证或完整连接参数 |
| `disconnect_mcp` | `mcp/client.py` | 关闭连接并注销该服务器工具；模型入口限 Lead 且需前台审批 |

因此，未连接远程服务器时 Lead 的工具集是 **26 + 2 = 28** 个。远程工具在连接后动态增加，数量由服务器发现结果决定。

`submit_plan` 是**队友专用工具**，原 s15 在 `spawn_teammate_thread()` 内定义。现在它也放进统一注册表，但只对 `teammate` 开放。连接 MCP 前，所有角色合计有 29 个工具定义，Lead 实际可用的是其中 28 个。

## 2. 原有代码段如何拆分

| s15 中的机制/代表函数 | 新位置 | 结构变化 |
|---|---|---|
| 模块级 `.env`、`client`、`MODEL`、路径常量 | `config.py`、`app.py` | 显式配置和实例装配；导入模块不需要 API key |
| `ConsoleBroker`、readline、主输入循环 | `cli.py` | `prompt_toolkit` 管理单一输入口；同步模型工作交给线程，终端用 asyncio 等待输入/事件 |
| `agent_loop()`、`call_llm()` | `core/agent.py`、`models/` | 统一可注入的 provider 协议，循环与 SDK 对象解耦 |
| `BUILTIN_TOOLS` / `BUILTIN_HANDLERS` | `core/tools.py` 与各服务 `register_tools()` | schema、handler、角色、审批属性同源定义 |
| `HOOKS` / `trigger_hooks()` | `core/hooks.py` | 每个 Harness 独立 registry；前置拒绝与后置审计异常分开处理 |
| `permission_hook()` | `tools/permissions.py`、文件路径校验、团队守卫 | 按执行身份区分前台/异步；不同职责不再挤在一个函数里 |
| 任务文件、跨进程锁、assignment 字典 | `tasks/store.py`、`tasks/storage.py` | task 及 lease/version 持久化；目录身份从租约刷新 |
| worktree 创建/认领目录/宿主删除 | `tasks/worktrees.py` | Git registry 验证保留；删除不暴露给模型 |
| `MessageBus` | `teams/mailbox.py` 与 `runtime/events.py` | 队友控制消息持久化；通用运行时通知用独立内存事件总线 |
| `ProtocolState`、计划/关闭请求 | `teams/protocols.py`、`teams/manager.py` | 请求与 task ID/version 绑定，拒绝迟到审批 |
| `spawn_teammate_thread()`、IDLE 扫描 | `teams/manager.py` | 模型执行通过 runner 注入，共用 AgentLoop |
| `spawn_subagent()` | `app.py` 的 `_subagent()` / `_run_child()` | 明确 role 与独立 history，不再复制一套模型循环 |
| `scan_skills()` / `load_skill()` | `context/skills.py` | 技能目录和完整内容按需读取 |
| `load_memory_runtime()` | `context/memory.py` | 独立移植 s09 记忆机制，删除跨章节动态导入和全局覆写 |
| `assemble_system_prompt()` | `prompt.py` 与 `Harness._system_prompt()` | 身份、技能目录、选中记忆显式输入 |
| `tool_result_budget` / snip / micro / compact | `context/manager.py` | 工具调用与结果按组保留；可恢复输出与历史归档统一管理 |
| `with_retry()` / `RecoveryState` | `models/retry.py`、`core/agent.py` | 网络重试和响应截断/上下文恢复分层，不共享可变 fallback 状态 |
| 后台任务字典、通知收集 | `runtime/background.py`、`tools/shell.py` | 后台身份/通知与真实进程的管理分离 |
| cron 匹配、文件保存、轮询线程 | `runtime/scheduler.py` | croniter + 时区；保留 pending_delivery 与至少一次交付 |
| `MCPClient` / mock server / 工具池拼装 | `mcp/config.py`、`mcp/client.py` | 真实传输、完整工具发现、动态注册/注销、连接生命周期 |
| `async_event_loop()` | `Harness.poll()` + `cli.py` | 嵌入式 API 显式提供事件泵；CLI 等待通知后自动唤醒 |

## 3. 把 s09 记忆整理进项目

梳理依赖时，我发现 s15 的 `load_memory_runtime()` 会用 `importlib` 加载相邻章节 `s09_memory/code.py`，再覆盖它的 `WORKDIR`、`MEMORY_DIR`、`client` 和 `MODEL`。要让项目独立运行，就需要把这部分记忆代码一起整理进来。

我把记录格式、索引、相关记录选择、读取正文、信息提取、去重和合并放进 `MemoryStore`，由调用方传入目录与 provider。它保留 s09 的四类知识记录，只保存对后续会话有价值的信息，同时增加数据校验和合并时的并发检查。运行时不再依赖课程仓库。

旧 `.memory` 以及任务、邮箱、定时和会话文件目前没有自动迁移入口。保留旧状态时，需要先备份，再按新记录结构转换后写入 `.harness/`。

## 4. 新增的模型接口与真实 MCP

模型接口新增了 `OpenAIProvider`，支持 `responses` 与 `chat_completions` 两种明确配置的模式。Responses 使用完整本地历史、`store=False` 和原生输出回传；Chat 模式把内部工具结果转换为 `role=tool`。Anthropic Messages 仍是完整适配器，兼容自定义 base URL。切换 provider 发生在应用配置处，不改变文件工具、任务或团队代码。

使用第三方网关时，需要确认它支持所选 API、工具 schema 和模型参数。返回无效工具参数时，框架会报告协议错误并停止这次工具执行。

MCP 从两个进程内 mock 服务变为真实 `stdio` 和 Streamable HTTP 客户端：

1. 宿主从 `mcp.json` 读取服务器配置和环境变量引用。
2. `connect_mcp` 获准后初始化连接，分页读取完整工具目录。
3. 原始工具映射为 `mcp__<server>__<tool>`，冲突或超长名称明确拒绝。
4. 处理函数经统一工具权限和参数校验后发出真实 `tools/call`。
5. `disconnect_mcp` 关闭传输并移除工具；同一连接的 SDK 上下文始终由同一个长期协程管理。

只有宿主配置 `read_only_tools` 中的原始工具名可免去逐次确认。远程 `readOnlyHint` 或 description 都不能作为授权来源。当前工具结果主要是文本；图片、音频等非文本 MCP 结果保留类型提示，没有自动接入多模态展示。工具目录在连接时发现，服务器目录变化后需重新连接。

## 5. 拆分时保留和调整了什么

保留的主线是单一模型循环、工具结果配对、hooks、两层计划、长期记忆与技能、分层上下文压缩、错误恢复、后台执行、cron、一次性 subagent、持久队友、任务 worktree，以及动态外部工具。

为了让各模块能独立运行和测试，我也调整了一些细节：

- **运行状态集中在 `.harness/`。** worktree 仍在 `.worktrees/`；任务 ID、字段命名和部分持久格式改变，租约/版本成为任务记录的一部分。
- **配置与生命周期显式。** Python API 与 CLI 共用 `Harness`，不再依赖脚本顶层的客户端、线程和环境副作用。
- **工具参数和角色执行时验证。** 可见性之外仍有 JSON Schema 与权限检查；文件读取加入分页，todo 按会话/Agent 隔离。
- **目录租约在完整工具组结束时释放。** 宿主有明确的 `before_step`、`before_tool`、`after_step` 协作点；旧计划不能跨任务生效。
- **恢复策略重新整理。** 瞬态网络错误通过独立 provider 装饰器重试，超时和部分 5xx 也有处理；鉴权/参数错误直接返回。步骤数、输出预算和上下文预算统一放进配置。
- **cron 使用五字段解析库和显式时区。** durable 默认为启用；创建与取消属于需要前台批准的模型工具。交付确认指模型接收，不是任务完成。
- **审批由宿主交互状态决定。** CLI 的模型循环可以运行在工作线程，仍由唯一的终端审批界面处理前台请求；异步 Agent 不争抢 stdin。
- **MCP 改为真实接入。** 增加服务器配置、连接和断开、超时及资源清理，工具请求通过网络或子进程发给服务器。
- **运行限制。** worktree 只隔离工作副本，整个框架按单宿主运行；普通内存通知在进程退出后丢失，durable cron 才有持久化重投机制。

拆分后，测试可以直接针对模型接口、任务认领、工具权限和消息交接等具体行为来写，排查问题时也更容易定位。模块之间怎么连接、一次请求按什么顺序执行，记录在 [架构说明](architecture.md) 中。
