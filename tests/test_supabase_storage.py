from __future__ import annotations

from arena_tactic.memory import AgentMemory, MemoryStore
import json
import time

from arena_tactic.storage import AsyncSupabaseWriter, SupabaseStorage


class FakeSupabase(SupabaseStorage):
    def __init__(self) -> None:
        super().__init__("https://example.invalid", "test-key")
        self.calls: list[tuple[str, str, dict | None]] = []
        self.remote_memory = None

    def _request(self, method, table, *, params=None, payload=None, prefer=None):
        self.calls.append((method, table, payload))
        if method == "GET" and table == "arena_agent_state":
            return [{"scheduler_state": self.remote_memory}] if self.remote_memory else []
        return True


def test_memory_store_prefers_supabase_and_keeps_file_fallback(tmp_path):
    remote = FakeSupabase()
    remote.remote_memory = AgentMemory(last_tick=9).to_dict()
    store = MemoryStore(tmp_path / "agent-state.json", supabase=remote)

    assert store.load().last_tick == 9
    store.save(AgentMemory(last_tick=10, explored={(1, 2)}))
    assert store.path.exists()
    assert any(table == "arena_agent_state" and method == "POST" for method, table, _ in remote.calls)


def test_async_writer_maps_all_runtime_records_without_blocking():
    remote = FakeSupabase()
    writer = AsyncSupabaseWriter(remote)
    assert writer.submit("memory", AgentMemory(last_tick=1).to_dict())
    assert writer.submit("replay", {"tick": 1, "state": {}})
    assert writer.submit("event", {"tick": 1, "type": "HIT", "values": {}})
    assert writer.submit("trace", {"tick": 1, "planner_version": "test"})
    assert writer.submit("report", {"tick_end": 1, "metrics": {}})
    writer.close()

    assert {table for _, table, _ in remote.calls} >= {
        "arena_agent_state", "arena_replays", "arena_events", "arena_decision_traces", "arena_reports",
    }


def test_async_writer_prevents_discard_until_history_file_backfill_completes(tmp_path):
    remote = FakeSupabase()
    writer = AsyncSupabaseWriter(remote)
    path = tmp_path / "replay.jsonl.11"
    path.write_text(json.dumps({"tick": 17, "state": {}}) + "\n", encoding="utf-8")

    assert not writer.can_discard_history_file("replay", path)
    deadline = time.monotonic() + 2
    while not writer.can_discard_history_file("replay", path) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert writer.can_discard_history_file("replay", path)
    writer.close()
    assert any(table == "arena_replays" and payload and payload.get("tick") == 17 for _, table, payload in remote.calls)


def test_history_backfill_does_not_write_trace_drop_summaries(tmp_path):
    remote = FakeSupabase()
    writer = AsyncSupabaseWriter(remote)
    path = tmp_path / "decision-trace.jsonl.11"
    path.write_text('{"record_type":"trace_drop_summary","tick":17}\n', encoding="utf-8")
    assert not writer.can_discard_history_file("trace", path)
    deadline = time.monotonic() + 2
    while not writer.can_discard_history_file("trace", path) and time.monotonic() < deadline:
        time.sleep(0.01)
    writer.close()
    assert not any(table == "arena_decision_traces" for _, table, _ in remote.calls)
