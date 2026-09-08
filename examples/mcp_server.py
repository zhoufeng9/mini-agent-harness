"""可直接运行的真实 MCP 示例；无需密钥，不读文件、不访问外部网络。

stdio：激活项目虚拟环境后运行 python examples/mcp_server.py
HTTP：python examples/mcp_server.py --transport streamable-http --port 8765
客户端配置参见 mcp.sample.json。stdio 的 stdout 专供协议使用，请勿 print。
"""

import argparse
import asyncio

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    server = FastMCP("mini-harness-demo", host="127.0.0.1", port=args.port, json_response=True)

    @server.tool()
    def add(a: int, b: int) -> int:
        """计算两个整数之和，用来验证工具发现、参数传递和结构化返回值。"""
        return a + b

    @server.tool()
    def echo(text: str) -> str:
        """原样返回输入文本；适用于确认中文编码和协议连通。"""
        return text

    @server.tool()
    async def wait_seconds(seconds: float) -> str:
        """最多等待 10 秒；演示客户端超时和取消，不产生外部副作用。"""
        if not 0 <= seconds <= 10:
            raise ValueError("seconds 必须介于 0 和 10")
        await asyncio.sleep(seconds)
        return "done"

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def fail() -> str:
        """故意失败；readOnlyHint 仅描述行为，宿主仍需要显式白名单才能免审批。"""
        raise ValueError("这是示例服务器的预期错误")

    server.run(transport=args.transport)


if __name__ == "__main__":
    main()
