"""无需 API key 的完整主循环：工具调用 → 文件写入 → 读取 → 最终回答。"""

from pathlib import Path
from tempfile import TemporaryDirectory

from mini_agent_harness import Harness, Settings
from mini_agent_harness.core.types import ModelResponse


class ScriptedModel:
    def __init__(self):
        self.step = 0

    def generate(self, messages, *, system, tools, max_tokens):
        self.step += 1
        if self.step == 1:
            return ModelResponse([{
                "type": "tool_use", "id": "write-1", "name": "write_file",
                "input": {"path": "hello.txt", "content": "你好，Agent Harness！"},
            }])
        if self.step == 2:
            return ModelResponse([{
                "type": "tool_use", "id": "read-1", "name": "read_file",
                "input": {"path": "hello.txt"},
            }])
        observed = messages[-1]["content"][0]["content"]
        return ModelResponse([{"type": "text", "text": f"离线循环验证成功，读取结果：\n{observed}"}])

    def close(self):
        pass


if __name__ == "__main__":
    with TemporaryDirectory(prefix="mini-harness-demo-") as directory:
        settings = Settings(workspace=Path(directory), model="offline", memory_enabled=False)
        with Harness(settings, provider=ScriptedModel()) as app:
            result = app.run("创建并读取一个 UTF-8 文件")
            print(result.text)
            print(f"status={result.status}, steps={result.steps}")
