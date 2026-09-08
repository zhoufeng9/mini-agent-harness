# 配置、模型与 MCP

## 配置从哪里来

`Settings.load()` 只解析明确给定的 `.env`，不会在导入包时读取环境，也不修改 `os.environ`。
这样多个 Harness 实例可以使用不同工作目录和配置，单元测试也不必放置真实 key。
`Settings.api_key` 不出现在对象 repr 中。不要把 `.env` 或包含明文认证信息的 `mcp.json` 提交到仓库。

| 配置项 | 说明 |
| --- | --- |
| `HARNESS_PROVIDER` | `anthropic` 或 `openai` |
| `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` | 对应服务的本地密钥 |
| `ANTHROPIC_MODEL` / `OPENAI_MODEL` | 对应模型 ID；命令行 `--model` 可以覆盖 |
| `ANTHROPIC_BASE_URL` / `OPENAI_BASE_URL` | 留空使用官方接口，否则填网关文档提供的地址 |
| `OPENAI_API_MODE` | `responses` 或 `chat_completions` |
| `HARNESS_MAX_TOKENS` | 初始输出 token 上限；截断时最多尝试翻倍 |
| `HARNESS_MAX_STEPS` | 每次 Agent 调用的模型轮数上限，默认 100 |
| `HARNESS_CONTEXT_CHARS` | 历史字符估算预算，不是精确 tokenizer token 数 |
| `HARNESS_REQUEST_TIMEOUT` | SDK 单次网络请求超时秒数 |
| `HARNESS_MAX_RETRIES` | 网络/限流等瞬态失败的有限重试次数 |
| `HARNESS_FALLBACK_MODEL` | 可选，同一 provider 的备用模型 |
| `HARNESS_SHELL_TIMEOUT` | 每条命令的最长运行秒数 |
| `HARNESS_MEMORY` | 是否启用长期记忆的选择、提取和合并 |
| `HARNESS_TIMEZONE` | cron 时区，默认 `Asia/Shanghai` |
| `HARNESS_MCP_CONFIG` | 相对于 workspace 的配置路径，默认 `mcp.json` |

保留旧教程 `MODEL_ID` 作为未设置 provider 专属模型时的兼容回退。
不同 provider 的网关协议互不推断；填写 Anthropic key 并不代表能使用 OpenAI 协议的 URL。

## 两种 OpenAI 接口

`responses` 适用于官方 Responses API，工具调用以 `function_call` / `function_call_output`
交换。适配器使用无服务端存储的请求，并在本地历史保留需要回传的原生输出和加密推理项。
模型循环只执行规范化后的 `tool_use` 块，因而不会重复执行原生项中的同一调用。
相关协议见 [OpenAI Function calling](https://developers.openai.com/api/docs/guides/function-calling)。

`chat_completions` 适用于实现 `/chat/completions` 的兼容网关：assistant 的 `tool_calls`
与 tool 角色的 `tool_call_id` 在适配层转换。官方接口使用 `max_completion_tokens`；
指定网关 URL 时使用较通用的 `max_tokens`。网关必须支持工具调用；仅支持文本生成的接口不足以运行本框架。

```dotenv
HARNESS_PROVIDER=openai
OPENAI_API_MODE=chat_completions
OPENAI_BASE_URL=https://your-provider.example/v1
OPENAI_MODEL=your-provider-model-id
OPENAI_API_KEY=在本地填写
```

并非每个模型都支持两种 API；例如官方 GPT-6 Astra 的工具调用要求 Responses。
请按模型和服务商的实际协议配置，而不是只修改模型名称。

Anthropic 使用 Messages API；原生响应内容被保存，便于后续工具轮回传。
定义格式依据 [Anthropic tool definitions](https://platform.claude.com/docs/en/agents-and-tools/tool-use/define-tools)。

## 真实 MCP 配置

参考 `examples/mcp.sample.json`。默认不开启任何服务器；Agent 只能按已配置的名字连接，
不能把任意命令作为 `connect_mcp` 参数启动。连接 stdio 服务器本身需要宿主批准，因为它会执行进程。

```json
{
  "mcpServers": {
    "local_demo": {
      "transport": "stdio",
      "command": "python",
      "args": ["examples/mcp_server.py"],
      "cwd": ".",
      "timeout": 30,
      "read_only_tools": ["add", "echo"]
    },
    "remote_docs": {
      "transport": "streamable-http",
      "url": "https://your-server.example/mcp",
      "headers": {"Authorization": "Bearer ${DOCS_TOKEN}"},
      "timeout": 30,
      "read_only_tools": ["search"]
    }
  }
}
```

`cwd` 相对于配置文件所在目录解析。`command` 用 PATH 查找，所以运行自带示例前需要激活 `.venv`，
也可以直接填 Python 可执行文件的绝对路径。`env` 字段可以把指定变量传给 stdio 子进程。
`${NAME}` 使用配置解析时传入的环境映射；Harness 将本地 `.env` 与进程环境合并后传入，进程环境优先。

`read_only_tools` 是**宿主维护的精确工具名列表**，不是服务器自报的 `readOnlyHint`。
不在列表内的外部工具需要用户批准；队友和异步轮次无法自行越过该审批。
服务器工具名被规范化成 `mcp__server__tool`；重复或规范化冲突会拒绝连接，并回滚该次注册。

客户端处理分页工具发现、结果错误、超时、断连以及退出时清理。MCP 传输在专用线程内的
单一异步任务中进入和退出资源上下文，避免跨任务关闭 AnyIO cancel scope。
当前依赖范围为维护中的 `mcp>=1.28,<2`，实际验证版本见 `requirements.lock`；
升级 v2 应独立验证适配，参考 [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk/tree/v1.x)。

## 长期运行的配置含义

记忆功能会产生额外模型请求：有已有条目时选择相关记录，一轮完成后提取，达到阈值后合并。
排错、快速原型或预算敏感时可以设置 `HARNESS_MEMORY=false`。
上下文摘要、subagent 和 teammate 也会调用模型；它们不是免费的本地字符串操作。

cron 仅在 Harness 进程运行时触发。`durable=true` 会保留任务，重启后投递待确认工作；
这不等于注册系统 crontab。`run` 一轮结束就退出，需要持续调度时请使用 `chat` 或嵌入宿主事件泵。
