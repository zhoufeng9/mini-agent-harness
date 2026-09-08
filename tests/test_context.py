"""验证上下文的核心不变量：授权原文、调用配对、可恢复归档、持久化边界。"""

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from mini_agent_harness.context import (
    ContextManager,
    MemoryStore,
    SkillsCatalog,
    paired_groups,
    parse_frontmatter,
)
from mini_agent_harness.core.types import ModelResponse


class FakeProvider:
    def __init__(self, values):
        self.values = iter(values)
        self.calls = []

    def generate(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        value = next(self.values)
        if isinstance(value, Exception):
            raise value
        return ModelResponse([{"type": "text", "text": value}])

    def close(self):
        pass


def record(name="Language", **overrides):
    return {"name": name, "type": "user", "scope": "persistent",
            "description": "Preferred documentation language", "body": "Use Chinese documentation.", **overrides}


def exchange(number, size=100):
    return [
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": f"a{number}", "name": "lookup", "input": {}},
            {"type": "tool_use", "id": f"b{number}", "name": "lookup", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"a{number}", "content": "x" * size}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"b{number}", "content": "y" * size}]},
    ]


def test_skills_catalog_frontmatter_and_on_demand(tmp_path):
    path = tmp_path / "review" / "SKILL.md"
    path.parent.mkdir()
    path.write_text("---\nname: review\ndescription: Code review\n---\n\nFirst body")
    catalog = SkillsCatalog(tmp_path)
    assert "Code review" in catalog.catalog()
    assert "First body" not in catalog.catalog()
    path.write_text(path.read_text().replace("First body", "Second body"))
    assert "Second body" in catalog.load("review")
    assert str(path) in catalog.load("review")
    with pytest.raises(KeyError):
        catalog.load("unknown")


def test_skills_symlink_cannot_escape(tmp_path):
    outside = tmp_path / "outside.md"
    outside.write_text("secret")
    root = tmp_path / "skills"
    skill = root / "unsafe"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").symlink_to(outside)
    assert SkillsCatalog(root).skills == {}


def test_frontmatter_handles_yaml_block_description():
    metadata, body = parse_frontmatter("---\nname: demo\ndescription: |\n  First\n  Second\n---\nBody\n---\n")
    assert metadata["description"] == "First\nSecond\n"
    assert body == "Body\n---\n"
    assert parse_frontmatter("---\n[broken\n---\nbody")[0] == {}


def test_memory_accepts_only_persistent_and_deduplicates(tmp_path):
    store = MemoryStore(tmp_path)
    assert not store.add(record(scope="current_task"))
    assert not store.add(record(body="Only for this session"))
    assert not store.add(record(body="本次任务先用英文"))
    assert not store.add(record(type="unknown"))
    assert store.add(record())
    assert not store.add(record("Other", body="  use CHINESE documentation. "))
    assert not store.add(record("Third", description="  PREFERRED documentation language ", body="other"))
    assert len(store.records()) == 1
    assert "language.md" in (tmp_path / "MEMORY.md").read_text()


def test_memory_reserved_index_name(tmp_path):
    store = MemoryStore(tmp_path)
    assert store.add(record("memory"))
    assert (tmp_path / "record-memory.md").is_file()
    assert (tmp_path / "MEMORY.md").read_text().startswith("# Memory catalog")


def test_memory_selection_validates_indices_and_marks_as_reference(tmp_path):
    provider = FakeProvider(["[true, -1, 0, 0, 999]"])
    store = MemoryStore(tmp_path, provider)
    store.add(record())
    recalled = store.recall([{"role": "user", "content": "Write documentation"}])
    assert "not instructions or authorization" in recalled
    assert "Use Chinese documentation." in recalled
    assert recalled.count('"source"') == 1
    assert provider.calls[0][1]["tools"] == []


def test_memory_selection_falls_back_to_keywords(tmp_path):
    store = MemoryStore(tmp_path, FakeProvider([RuntimeError("offline")]))
    store.add(record())
    selected = store.select([{"role": "user", "content": "documentation"}])
    assert len(selected) == 1


def test_memory_extraction_filters_tool_outputs_and_temporary_records(tmp_path):
    provider = FakeProvider([json.dumps([record(), record("Temporary", scope="current_task")])])
    store = MemoryStore(tmp_path, provider)
    result = store.extract([
        {"role": "user", "content": "I prefer Chinese documentation"},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "1", "content": "NEVER_STORE_TOOL_DATA"}]},
    ])
    assert result == 1
    assert "NEVER_STORE_TOOL_DATA" not in json.dumps(provider.calls)


def test_memory_consolidation_replaces_only_valid_response(tmp_path):
    provider = FakeProvider([json.dumps([record("Merged", body="Stable preference.")]), "[]"])
    store = MemoryStore(tmp_path, provider)
    store.add(record())
    assert store.consolidate(threshold=1) == 1
    assert store.records()[0]["name"] == "Merged"
    assert not (tmp_path / "language.md").exists()
    assert store.consolidate(threshold=1) == 0
    assert store.records()[0]["name"] == "Merged"


def test_memory_consolidation_aborts_if_another_writer_changes_snapshot(tmp_path):
    store = MemoryStore(tmp_path)
    store.add(record())

    class ConcurrentProvider:
        def generate(self, *args, **kwargs):
            # 这一步能完成也说明 store 没在网络调用期间一直持有文件锁。
            MemoryStore(tmp_path).add(record("Reference", type="reference",
                                              description="Python docs URL", body="https://docs.python.org/3/"))
            return ModelResponse([{"type": "text", "text": json.dumps([record("Merged")])}])

    store.provider = ConcurrentProvider()
    assert store.consolidate(threshold=1) == 0
    assert {r["name"] for r in store.records()} == {"Language", "Reference"}


def test_concurrent_memory_add_does_not_duplicate(tmp_path):
    def add(_):
        return MemoryStore(tmp_path).add(record())

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(add, range(12)))
    assert sum(results) == 1
    assert len(MemoryStore(tmp_path).records()) == 1


def test_memory_symlink_records_are_not_read(tmp_path):
    outside = tmp_path / "outside.md"
    outside.write_text(MemoryStore._document(record()))
    root = tmp_path / "memory"
    root.mkdir()
    (root / "record.md").symlink_to(outside)
    assert MemoryStore(root).records() == []


def test_snip_keeps_parallel_tool_calls_with_all_results(tmp_path):
    history = [{"role": "user", "content": "Original request"}]
    for index in range(20):
        history.extend(exchange(index))
    manager = ContextManager(tmp_path, max_messages=12)
    result = manager.prepare(history, "Original request")
    paired_groups(result)
    assert len(result) < len(history)
    assert "Original request" in json.dumps(result)
    archives = list((tmp_path / "transcripts").glob("*.jsonl"))
    assert archives
    assert len(archives[0].read_text().splitlines()) == len(history)


@pytest.mark.parametrize("history", [
    [{"role": "assistant", "content": [{"type": "tool_use", "id": "a", "name": "x", "input": {}}]}],
    [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "missing", "content": "x"}]}],
])
def test_compaction_refuses_orphaned_call_or_result(tmp_path, history):
    with pytest.raises(ValueError):
        ContextManager(tmp_path).prepare(history, "task")


def test_large_output_is_recoverable_and_same_tool_id_does_not_overwrite(tmp_path):
    manager = ContextManager(tmp_path, tool_output_chars=500)
    first = manager.persist_output("../../unsafe/id", "a" * 3000)
    second = manager.persist_output("../../unsafe/id", "b" * 3000)
    assert "Full output:" in first
    first_path = Path(first.split("Full output: ")[1].splitlines()[0])
    second_path = Path(second.split("Full output: ")[1].splitlines()[0])
    assert first_path != second_path
    assert first_path.is_relative_to(tmp_path)
    assert first_path.read_text() == "a" * 3000
    assert second_path.read_text() == "b" * 3000
    assert manager.persist_output("small", "ok") == "ok"


def test_prepare_does_not_mutate_callers_history(tmp_path):
    history = exchange(1, size=10_000)
    before = json.dumps(history)
    manager = ContextManager(tmp_path, tool_output_chars=500)
    result = manager.prepare(history, "task")
    assert json.dumps(history) == before
    assert len(json.dumps(result)) < len(before)


def test_reactive_compaction_preserves_request_and_native_pairing(tmp_path):
    history = [{"role": "user", "content": "long old data" * 3000}] + exchange(1, 5000)
    history[1]["content"].insert(0, {"type": "openai_response_item", "source": "test",
                                     "item": {"type": "reasoning", "encrypted_content": "opaque"}})
    request = "请只修改 tests；不要发布。"
    provider = FakeProvider([RuntimeError("model offline")])
    manager = ContextManager(tmp_path, provider, max_chars=12_000, tool_output_chars=1000)
    result = manager.reactive_compact(history, request)
    paired_groups(result)
    assert request in result[0]["content"]
    assert "never instructions or authorization" in result[0]["content"]
    assert "Full transcript:" in result[0]["content"]
    assert all("Authoritative request" not in json.dumps(call[0]) for call in provider.calls)


def test_summary_calls_provider_with_reference_data_only(tmp_path):
    provider = FakeProvider(["Tests passed; implementation incomplete."])
    manager = ContextManager(tmp_path, provider)
    history = [{"role": "user", "content": "old request"}, {"role": "assistant", "content": "finding"}] + exchange(1)
    result = manager.compact(history, "Actual current request")
    assert "Tests passed" in result[0]["content"]
    assert provider.calls[0][1]["tools"] == []
    assert "untrusted reference data" in provider.calls[0][1]["system"]


def test_memory_ignores_runtime_and_context_evidence(tmp_path):
    provider = FakeProvider(["[]"])
    store = MemoryStore(tmp_path, provider)
    store.extract([
        {"role": "user", "content": "Write documentation"},
        {"role": "user", "origin": "runtime", "content": "HIDDEN_RUNTIME_OUTPUT"},
        {"role": "user", "origin": "context", "content": "HIDDEN_SUMMARY"},
    ])
    request = json.dumps(provider.calls)
    assert "HIDDEN_RUNTIME_OUTPUT" not in request and "HIDDEN_SUMMARY" not in request


def test_memory_consolidation_does_not_replace_unknown_file(tmp_path):
    provider = FakeProvider([json.dumps([record("Unknown")])])
    store = MemoryStore(tmp_path, provider)
    store.add(record())
    unknown = tmp_path / "unknown.md"
    unknown.write_text("Unrelated notes without frontmatter")
    assert store.consolidate(threshold=1) == 0
    assert unknown.read_text() == "Unrelated notes without frontmatter"
    assert store.records()[0]["name"] == "Language"


def test_small_context_budget_trims_reference_but_preserves_authoritative_request(tmp_path):
    manager = ContextManager(tmp_path, max_chars=1500)
    history = [{"role": "user", "content": "old" * 10_000}] + exchange(1, 5000)
    request = "Do not publish or delete files."
    result = manager.reactive_compact(history, request)
    assert len(json.dumps(result, ensure_ascii=False)) <= 1500
    assert result[0]["active_request"] == request
    assert request in result[0]["content"]
    paired_groups(result)


def test_memory_recall_can_use_authoritative_request_after_compaction(tmp_path):
    store = MemoryStore(tmp_path / "memory")
    store.add(record())
    context = ContextManager(tmp_path / "context")
    result = context.compact([{"role": "user", "content": "old reference"}], "Write documentation")
    assert "Use Chinese documentation" in store.recall(result)


def test_memory_consolidation_rolls_back_partial_disk_failure(tmp_path, monkeypatch):
    store = MemoryStore(tmp_path, FakeProvider([json.dumps([record("Merged")])]))
    store.add(record())
    original = store._atomic
    calls = 0

    def fail_once(path, text):
        nonlocal calls
        calls += 1
        if calls == 2:  # 已写新记录并删旧记录后，重建索引失败。
            raise OSError("disk error")
        return original(path, text)

    monkeypatch.setattr(store, "_atomic", fail_once)
    assert store.consolidate(threshold=1) == 0
    assert store.records()[0]["name"] == "Language"
    assert not (tmp_path / "merged.md").exists()
