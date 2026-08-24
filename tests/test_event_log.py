from __future__ import annotations

import json
from pathlib import Path

from arena_tactic.event_log import EventLogCollector
from arena_tactic.dashboard import DashboardDataStore
from tactic import ServiceStatus, _http_response


def _write(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def _row(tick: int, *, enemies=None, events=None, mode="ECONOMY") -> dict:
    return {"tick": tick, "mode": mode, "state": {"visible_enemies": enemies or []}, "events": events or []}


def test_incremental_offset_enemy_window_and_tick_aggregation(tmp_path: Path):
    replay = tmp_path / "replay.jsonl"
    _write(replay, [_row(1, enemies=[{"id": "enemy_a", "position": [2, 3]}], events=[
        {"type": "SHOT_MISSED", "target": "enemy_a", "position": [2, 3]},
        {"type": "SHOT_MISSED", "target": "enemy_a", "position": [2, 3]},
    ])])
    collector = EventLogCollector(replay, enemy_reappear_ticks=2)
    first = collector.collect()
    assert [(item["type"], item["count"]) for item in first] == [("ENEMY_SPOTTED", 1), ("SHOT_MISSED", 2)]
    assert collector.collect() == []  # offset checkpoint prevents re-reading.
    with replay.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(_row(4, enemies=[])) + "\n")
        stream.write(json.dumps(_row(5, enemies=[{"id": "enemy_a", "position": [3, 3]}])) + "\n")
    assert [item["type"] for item in collector.collect()] == ["ENEMY_SPOTTED"]


def test_rotation_refresh_tick_and_ring_limit(tmp_path: Path):
    replay = tmp_path / "replay.jsonl"
    _write(replay, [_row(3, events=[{"type": "HARVEST_SUCCEEDED", "position": [0, 0], "values": {"source": "RESOURCE_NODE"}}])])
    collector = EventLogCollector(replay, max_events=2)
    first = collector.collect()
    depleted = next(item for item in first if item["type"] == "RESOURCE_DEPLETED")
    assert depleted["values"] == {"refresh_tick": 4, "quota": 16}
    replay.rename(tmp_path / "replay.jsonl.1")
    _write(replay, [_row(4, events=[{"type": "CORE_SPAWN_SUCCEEDED", "position": [1, 1]}]), _row(5, events=[{"type": "DEPOSIT_FAILED", "position": [1, 1], "reason": "CORE_RESOURCE_FULL"}])])
    second = collector.collect()
    assert [item["type"] for item in second] == ["UNIT_SPAWNED", "DEPOSIT_FAILED"]
    assert [item["type"] for item in collector.read(limit=10)] == ["UNIT_SPAWNED", "DEPOSIT_FAILED"]


def test_dashboard_event_log_fields_and_limit_query(tmp_path: Path):
    replay = tmp_path / "replay.jsonl"
    _write(replay, [_row(10, events=[{"type": "CORE_DAMAGED", "target": "core", "position": [1, 2]}]), _row(11, events=[{"type": "DEPOSIT_FAILED", "position": [1, 2], "reason": "CORE_RESOURCE_FULL"}])])
    response = _http_response("/api/dashboard?event_limit=1", ServiceStatus(), DashboardDataStore(replay, cache_seconds=0))
    assert response is not None
    _, body, _ = response
    event_log = json.loads(body)["event_log"]
    assert len(event_log["events"]) == 1
    event = event_log["events"][0]
    assert set(event) >= {"tick", "type", "category", "description", "position", "target", "count"}
    assert event_log["counts"] == {"DEPOSIT_FAILED": 1}
    assert event_log["category_counts"] == {"anomaly": 1}
