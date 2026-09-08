# mini-agent-harness

这是我学完 [learn-claude-code](https://github.com/shareAI-lab/learn-claude-code) 后做的一个实战项目。
我以 s15 为基础，把原来放在一个文件里的代码按职责拆开，保留了 26 个工具，
也把它依赖的 s09 记忆模块整理了进来，做成一个可以独立运行、方便继续修改的 Python Agent Harness。

搭建过程和一些体会写在 [实战复盘](docs/retrospective.md) 里，具体模块关系见 [架构说明](docs/architecture.md)。
想顺着一次输入看代码和数据怎么流转，可以读 [完整工作流程](docs/workflow.md)。

支持 **Anthropic、OpenAI Responses、OpenAI 兼容 Chat Completions**，以及真实 **MCP stdio / Streamable HTTP**。
入口为命令行与 Python API；面向 **Python 3.11+、macOS / Linux**。

## 从这里开始

```bash
git clone https://github.com/zhoufeng9/mini-agent-harness.git
cd mini-agent-harness
python3.11 -m venv .venv    # 也可以使用 python3.12 等更新版本
source .venv/bin/activate
python -m pip install -e '.[dev]'
cp .env.sample .env
chmod 600 .env
```

编辑 `.env`，填写自己实际使用的密钥、模型 ID 和可选网关地址。两套密钥可以同时填写，
`HARNESS_PROVIDER` 决定本次使用哪一套。模型 ID 按账号实际可用的模型填写。

```dotenv
HARNESS_PROVIDER=anthropic
ANTHROPIC_API_KEY=在本地填写
ANTHROPIC_MODEL=claude-sonnet-5

OPENAI_API_KEY=在本地填写
OPENAI_MODEL=gpt-5.6
OPENAI_API_MODE=responses
```

```bash
mini-agent doctor
mini-agent chat
mini-agent run '阅读 README，解释这个项目的模块关系'
mini-agent --provider openai chat
```

`doctor` 检查本地配置是否齐全，只显示密钥是否已设置。
无密钥也能运行完整的离线循环示例：

```bash
python examples/offline_demo.py
```

## 代码结构

| 模块 | 作用 |
| --- | --- |
| `core/` | 一个模型循环、消息协议、工具注册、JSON Schema 校验、hooks |
| `models/` | Anthropic / OpenAI 格式适配、原生推理项保留、重试和备用模型 |
| `tools/` | 文件、Shell、todo、前台审批；执行上下文决定 cwd |
| `context/` | 技能目录与按需加载，长期记忆，输出归档、历史压缩与恢复 |
| `tasks/` | JSON 任务图、依赖、跨进程原子认领、owner 和 worktree 租约 |
| `teams/` | 一直存在的队友线程、文件邮箱、计划审批、关闭与 IDLE 认领 |
| `runtime/` | 通知总线、后台命令、可持久化 cron 和成功交付确认 |
| `mcp/` | 真实服务器连接、工具发现、名称隔离、宿主白名单和连接清理 |
| `app.py` / `cli.py` | 服务装配、资源生命周期、Python API 和终端输入协调 |

阅读代码可以先沿着 `core/types.py → core/tools.py → core/agent.py → app.py`
看完主流程，再进入各个功能模块。

## 命令行与工作目录

交互模式支持 `/tasks`、`/new`、`/quit`。后台通知、队友消息和 cron 会自动唤醒下一轮；
不会为了等待队友而反复请求模型。一次性 `run` 返回后关闭当前宿主，持续任务应使用 `chat`。

```bash
mini-agent --workspace /path/to/your/repository \
  --env-file /path/to/mini-agent-harness/.env chat
```

默认只读取 **workspace 下的 `.env`**，不会自动搜索父目录。配置优先级为：
命令行参数 > 进程环境变量 > `.env` > 默认值。MCP 配置默认位于 workspace 的 `mcp.json`。

每条 Shell 命令、MCP 连接以及未列入宿主只读白名单的 MCP 工具都需要前台批准。
后台轮次和队友不会争抢终端输入，需要审批的调用会返回拒绝结果。
文件操作可在指定 cwd 内执行；worktree 分离工作副本，Shell 进程组用于清理，二者均不提供 OS 沙箱。

## 接入真实 MCP

无需模型密钥即可验证自带的真实协议服务器：

```bash
source .venv/bin/activate
cp examples/mcp.sample.json mcp.json
mini-agent mcp-check demo
```

会看到 `mcp__demo__add` 等工具名。之后在交互 Agent 中请求“连接 demo MCP，计算 12 + 30”。
模型调用 `connect_mcp`，用户批准后，下一个模型轮次即可看到新工具。

HTTP 演示需要在另一个终端启动服务器：

```bash
python examples/mcp_server.py --transport streamable-http --port 8765
mini-agent mcp-check demo_http
```

服务器地址、命令和只读工具名单由本地 `mcp.json` 配置。真实授权信息使用 `${VARIABLE}`
从 `.env` 或进程环境读取。完整说明见 [配置与 MCP](docs/configuration.md)。

## Python API

```python
from mini_agent_harness import Harness, Settings

settings = Settings.load("/path/to/workspace", env_file="/path/to/.env")
with Harness(settings) as app:
    result = app.run("列出项目文件，并说明入口在哪里")
    print(result.text)
    print(result.status)  # completed / step_limit / length_limit / stopped
```

Python API 默认非交互，遇到需要人工批准的工具会拒绝。需要前台审批时，由调用方传入
`approve(question) -> bool`，并用 `app.run(..., interactive=True)` 发起请求。
后台事件由嵌入应用主动调用 `app.poll()` 处理；CLI 已包含这个事件泵。
另见 [嵌入与扩展示例](examples/use_api.py)。

## 验证与文档

```bash
python -m pytest -q
ruff check .
python -m build --no-isolation
```

模型适配使用脚本模型或 SDK 替身测试；MCP 集成测试会启动本地服务器，
worktree 测试会创建临时 Git 仓库。HTTP 测试需要允许绑定 `127.0.0.1` 端口。

- [实战复盘](docs/retrospective.md)
- [完整工作流程：从用户输入到结果返回](docs/workflow.md)
- [架构与执行流程](docs/architecture.md)
- [s15 到模块的映射](docs/s15-mapping.md)
- [配置、模型与 MCP](docs/configuration.md)
- [开发、测试与运行边界](docs/development.md)

`.harness/` 存放本地任务、记忆、调度和会话，`.worktrees/` 存放任务工作副本。
它们与 `.env`、本地 `mcp.json` 均被 Git 忽略。仓库只分发 `.env.sample` 和 MCP 示例配置。

## 来源与许可证

感谢 shareAI Lab 的开源教程。本项目基于其 MIT 许可代码整理与扩展，保留原作者版权声明，
详见 [LICENSE](LICENSE) 和 [NOTICE](NOTICE)。参考提交为 `0dcafa2ae053a1ddd6a72f265431104b08a5aa13`。
