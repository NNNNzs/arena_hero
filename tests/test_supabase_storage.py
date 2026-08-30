from __future__ import annotations

from arena_tactic.memory import AgentMemory, MemoryStore
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
