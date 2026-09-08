"""嵌入自己的应用；先在当前目录 .env 中填好模型配置。"""

from mini_agent_harness import Harness, Settings
from mini_agent_harness.core.types import ToolSpec, object_schema


def approve(question: str) -> bool:
    """审批 UI 属于调用方；也可以在这里接入桌面确认框。"""
    return input(question + " [y/N] ").strip().lower() == "y"


if __name__ == "__main__":
    with Harness(Settings.load(), approve=approve) as app:
        app.registry.register(ToolSpec(
            "count_characters", "Count Unicode characters in text.",
            object_schema({"text": {"type": "string"}}, ["text"]),
            lambda ctx, args: str(len(args["text"])),
        ))
        result = app.run("调用 count_characters 计算‘你好世界’的字符数", interactive=True)
        print(result.text)
        # 常驻应用可在自己的事件循环中调用 app.poll() 处理后台通知。
