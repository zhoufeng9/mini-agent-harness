"""Skills 的渐进加载：先给模型目录，命中后再读取正文。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """只识别文件顶部、独占一行的 ---，避免正文中的分隔线被误切分。"""
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return {}, text
    end = next((i for i, line in enumerate(lines[1:], 1) if line.strip() == "---"), None)
    if end is None:
        return {}, text
    try:
        metadata = yaml.safe_load("".join(lines[1:end])) or {}
    except yaml.YAMLError:
        return {}, text
    if not isinstance(metadata, dict):
        return {}, text
    return metadata, "".join(lines[end + 1:]).lstrip()


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    path: Path


class SkillsCatalog:
    """目录由用户安装的本地 SKILL.md 构成；符号链接不能逃出 skills 根目录。

    扫描只缓存元数据；load 再次读取文件，因此更改技能正文无须重启进程。
    缺目录是合法的空目录状态，不在构造时创建用户文件。
    """

    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.skills: dict[str, Skill] = {}
        self.scan()

    def scan(self) -> None:
        found = {}
        for path in sorted(self.root.glob("*/SKILL.md")):
            if not path.resolve().is_relative_to(self.root) or not path.is_file():
                continue
            metadata, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
            name = metadata.get("name", path.parent.name)
            description = metadata.get("description", "")
            if not isinstance(name, str) or not isinstance(description, str):
                continue
            name = name.strip()
            if not name or name in found:
                continue
            found[name] = Skill(name, description, path)
        self.skills = found

    def catalog(self) -> str:
        return "\n".join(f"- {' '.join(s.name.split())}: {' '.join(s.description.split())}"
                         for s in self.skills.values()) or "(no skills found)"

    def load(self, name: str) -> str:
        skill = self.skills.get(name)
        if skill is None:
            raise KeyError(f"Unknown skill: {name}")
        if not skill.path.resolve().is_relative_to(self.root):
            raise ValueError("Skill path escapes its root")
        # 返回来源路径，帮助模型相对 skill 目录定位额外资料。
        return f"Skill source: {skill.path}\n\n{skill.path.read_text(encoding='utf-8')}"
