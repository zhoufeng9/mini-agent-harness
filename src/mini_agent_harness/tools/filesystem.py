"""文件操作仅在执行身份的 cwd 内解析；worktree 中的相对路径不会落到主仓库。"""

from __future__ import annotations

from pathlib import Path

from ..core.types import ExecutionContext, ToolSpec, object_schema


def safe_path(ctx: ExecutionContext, path: str, *, write: bool = False) -> Path:
    root = ctx.cwd.resolve()
    target = (root / path).resolve()
    if not target.is_relative_to(root):
        # worktree 的 agent 仍可按归档标记读回工具输出，但不能写宿主状态。
        # 只接受已存在的归档文件；不因此放开其他 workspace 内容。
        state = ctx.workspace.resolve() / ".harness"
        artifact_roots = [state / "context" / "tool-results", state / "context" / "transcripts",
                          state / "shell"]
        recoverable = not write and target.is_file() and any(
            directory.resolve().is_relative_to(state)
            and target.is_relative_to(directory.resolve()) for directory in artifact_roots
        )
        if recoverable:
            return target
        raise PermissionError("Path escapes the assigned working directory")
    # 工具不能绕过 TaskStore 的锁直接改宿主状态；Git 元数据也由宿主管理。
    if write and any(part in {".git", ".harness"} for part in target.relative_to(root).parts):
        raise PermissionError("Host runtime state and Git metadata are not editable file tools")
    return target


class FileTools:
    def read(self, ctx: ExecutionContext, args: dict) -> str:
        path = safe_path(ctx, args["path"])
        offset = args.get("offset", 1)
        limit = args.get("limit", 200)
        # 有界读取支持随后翻页，避免读取一个大文件立即撑爆上下文。
        result = []
        size = 0
        with path.open(encoding="utf-8", errors="replace") as handle:
            for number, line in enumerate(handle, 1):
                if number < offset:
                    continue
                if number >= offset + limit or size > 60000:
                    result.append(f"[More content: read from line {number}]\n")
                    break
                line = line[:12000]
                result.append(f"{number:5d} | {line}")
                size += len(line)
        return "".join(result) or "(empty file or no lines at this offset)"

    def write(self, ctx: ExecutionContext, args: dict) -> str:
        path = safe_path(ctx, args["path"], write=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(args["content"], encoding="utf-8")
        return f"Wrote {len(args['content'])} characters to {path}"

    def edit(self, ctx: ExecutionContext, args: dict) -> str:
        path = safe_path(ctx, args["path"], write=True)
        original = path.read_text(encoding="utf-8")
        old = args["old_text"]
        if original.count(old) != 1:
            raise ValueError("old_text must match exactly once; read the file and choose more context")
        path.write_text(original.replace(old, args["new_text"], 1), encoding="utf-8")
        return f"Edited {path}"

    def glob(self, ctx: ExecutionContext, args: dict) -> str:
        pattern = args["pattern"]
        if Path(pattern).is_absolute() or ".." in Path(pattern).parts:
            raise PermissionError("Glob pattern must stay inside cwd")
        root = ctx.cwd.resolve()
        found = []
        for path in root.glob(pattern):
            if any(p in {".git", ".venv", "__pycache__"} for p in path.relative_to(root).parts):
                continue
            if path.resolve().is_relative_to(root):
                found.append(str(path.relative_to(root)))
            if len(found) == 500:
                found.append("[Truncated at 500 matches; narrow the pattern]")
                break
        return "\n".join(sorted(found)) or "No matches"

    def register_tools(self, registry) -> None:
        path = {"type": "string", "minLength": 1}
        registry.register(ToolSpec("read_file", "Read UTF-8 text with line numbers and pagination.",
            object_schema({"path": path, "offset": {"type": "integer", "minimum": 1},
                "limit": {"type": "integer", "minimum": 1, "maximum": 1000}}, ["path"]), self.read))
        registry.register(ToolSpec("write_file", "Create or overwrite a file in the assigned workspace.",
            object_schema({"path": path, "content": {"type": "string"}}, ["path", "content"]), self.write))
        registry.register(ToolSpec("edit_file", "Replace one unique exact text occurrence in a file.",
            object_schema({"path": path, "old_text": {"type": "string", "minLength": 1},
                "new_text": {"type": "string"}}, ["path", "old_text", "new_text"]), self.edit))
        registry.register(ToolSpec("glob", "Find workspace paths matching a glob pattern.",
            object_schema({"pattern": {"type": "string", "minLength": 1}}, ["pattern"]), self.glob))
