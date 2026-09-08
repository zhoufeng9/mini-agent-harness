"""配置优先级、路径边界、Shell 超时和输出归档的行为验证。"""

import os

import pytest

from mini_agent_harness.config import Settings
from mini_agent_harness.core.types import ExecutionContext
from mini_agent_harness.tools.filesystem import FileTools, safe_path
from mini_agent_harness.tools.shell import ShellRunner


def test_env_file_precedence_without_mutating_environment(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("HARNESS_PROVIDER=openai\nOPENAI_MODEL=file-model\nOPENAI_API_KEY=local-secret\n")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_MODEL", "env-model")
    config = Settings.load(tmp_path)
    assert config.model == "env-model"
    assert config.api_key == "local-secret"
    assert "local-secret" not in repr(config)
    assert "OPENAI_API_KEY" not in os.environ
    assert Settings.load(tmp_path, model="cli-model").model == "cli-model"


def test_no_parent_dotenv_autodiscovery(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("ANTHROPIC_MODEL=should-not-inherit\n")
    monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
    monkeypatch.delenv("MODEL_ID", raising=False)
    monkeypatch.delenv("HARNESS_PROVIDER", raising=False)
    with pytest.raises(ValueError, match="ANTHROPIC_MODEL"):
        Settings.load(tmp_path / "other")


def test_path_escape_and_host_metadata_guard(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "escape").symlink_to(tmp_path, target_is_directory=True)
    ctx = ExecutionContext(workspace, workspace)
    for path in ["../bad", str(tmp_path / "absolute"), "escape/outside"]:
        with pytest.raises(PermissionError):
            safe_path(ctx, path)
    for path in [".git/config", ".harness/tasks/fake.json"]:
        with pytest.raises(PermissionError):
            safe_path(ctx, path, write=True)


def test_file_edits_are_unique_and_use_assigned_worktree(tmp_path):
    assigned = tmp_path / "assigned"
    assigned.mkdir()
    ctx = ExecutionContext(tmp_path, assigned)
    files = FileTools()
    files.write(ctx, {"path": "x", "content": "a a"})
    with pytest.raises(ValueError, match="exactly once"):
        files.edit(ctx, {"path": "x", "old_text": "a", "new_text": "b"})
    files.edit(ctx, {"path": "x", "old_text": "a a", "new_text": "你好"})
    assert (assigned / "x").read_text() == "你好"
    assert not (tmp_path / "x").exists()


def test_shell_timeout_and_large_output_archive(tmp_path):
    shell = ShellRunner(tmp_path / "outputs", timeout=0.1)
    output, code = shell.run("sleep 5", tmp_path)
    assert code == 124 and "Timed out" in output
    shell.timeout = 5
    output, code = shell.run("yes x | head -c 80000", tmp_path)
    assert code == 0 and "Full output saved" in output
    assert len(output) < 41000
    shell.close()
    with pytest.raises(RuntimeError, match="closed"):
        shell.run("true", tmp_path)


def test_worktree_can_recover_host_archives_but_cannot_write_them(tmp_path):
    assigned = tmp_path / "worktree"
    assigned.mkdir()
    archive = tmp_path / ".harness/context/tool-results/large.txt"
    archive.parent.mkdir(parents=True)
    archive.write_text("retained output")
    ctx = ExecutionContext(tmp_path, assigned, role="teammate", agent_id="worker")
    assert "retained output" in FileTools().read(ctx, {"path": str(archive)})
    with pytest.raises(PermissionError):
        safe_path(ctx, str(archive), write=True)
    with pytest.raises(PermissionError):
        safe_path(ctx, str(tmp_path / "other.txt"))
