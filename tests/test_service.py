from __future__ import annotations

import json
from pathlib import Path

from arena_tactic.dashboard import DashboardDataStore
from tactic import ServiceStatus, _http_response, _runtime_config_from_environment


def _response(path: str, status: ServiceStatus, replay: Path) -> tuple[int, str, bytes]:
    result = _http_response(path, status, DashboardDataStore(replay, cache_seconds=0))
    assert result is not None
    code, body, content_type = result
    return code, content_type, body


def test_health_endpoints_distinguish_process_liveness_from_sdk_connection(tmp_path: Path):
    status = ServiceStatus()
    replay = tmp_path / "missing.jsonl"
    live_code, _, live_body = _response("/livez", status, replay)
    health_code, _, health_body = _response("/healthz", status, replay)
    live_payload, health_payload = json.loads(live_body), json.loads(health_body)
    assert live_code == 200
    assert live_payload["running"] is True
    assert health_code == 503
    assert health_payload["connected"] is False

    status.update(connected=True, last_tick=123)
    health_code, _, health_body = _response("/healthz", status, replay)
    assert health_code == 200
    assert json.loads(health_body)["last_tick"] == 123


def test_root_serves_chinese_dashboard_and_json_status_remains_compatible(tmp_path: Path):
    status = ServiceStatus(connected=True, last_tick=42, accepted=7, rejected=2)
    replay = tmp_path / "missing.jsonl"
    code, content_type, body = _response("/", status, replay)
    assert code == 200
    assert content_type.startswith("text/html")
    assert "作战指挥中心" in body.decode()
    assert "policyPosture" in body.decode()
    static = _http_response("/static/command-center.js", status, DashboardDataStore(replay, cache_seconds=0))
    assert static is not None and static[2].startswith("application/javascript") and b"setPolicy" in static[1]
    map_asset = _http_response("/static/tactical-map.js", status, DashboardDataStore(replay, cache_seconds=0))
    assert map_asset is not None and map_asset[2].startswith("application/javascript")

    status_code, _, body = _response("/status", status, replay)
    payload = json.loads(body)
    assert status_code == 200
    assert payload["last_tick"] == 42
    assert payload["accepted"] == 7


def test_dashboard_api_tolerates_missing_and_truncated_replay(tmp_path: Path):
    replay = tmp_path / "replay.jsonl"
    status = ServiceStatus(connected=True, last_tick=9)
    code, _, body = _response("/api/dashboard", status, replay)
    empty = json.loads(body)
    assert code == 200
    assert empty["current"] is None
    assert empty["recent"] == []

    replay.write_text(
            json.dumps({
                "tick": 9,
                "mode": "ECONOMY",
                "state": {"resources": 8, "resource_capacity": 15, "population": 3},
                "intents": [{"action": "HARVEST"}],
                "events": [{"type": "HARVEST_SUCCEEDED", "reason": None}],
                "decision_ms": 12.5,
                "accepted": True,
            }) + "\n{\"tick\":10,",
            encoding="utf-8",
        )
    code, _, body = _response("/api/dashboard", status, replay)
    payload = json.loads(body)
    assert code == 200
    assert payload["current"]["tick"] == 9
    assert payload["current"]["mode_label"] == "发展经济"
    assert payload["current"]["actions"] == [{"type": "HARVEST", "label": "采集", "count": 1}]


def test_dashboard_map_projection_is_bounded_and_allowlisted(tmp_path: Path):
    replay = tmp_path / "replay.jsonl"
    replay.write_text(json.dumps({"tick": 1, "mode": "DEFEND", "state": {
        "resources": 2, "resource_capacity": 10, "population": 1,
        "core": {"id": "corealias", "kind": "CORE", "position": [0, 0], "hp": 5},
        "units": [{"id": "unitalias", "kind": "UNIT", "unit_type": "WORKER", "position": [1, 0], "hp": 2, "cargo": 0}],
        "visible_enemies": [{"id": "enemyalias", "kind": "UNIT", "unit_type": "RANGER", "position": [2, 0], "hp": 2}],
        "resource_cells": [[1, 1]], "obstacle_cells": [[0, 1]], "beacon": {"position": [3, 0], "status": "GROUND"},
    }, "intents": [], "events": [], "secret": "do-not-expose"}) + "\n", encoding="utf-8")
    _, _, body = _response("/api/dashboard", ServiceStatus(), replay)
    payload = json.loads(body)
    assert payload["current"]["map"]["friendly"][0]["position"] == [0, 0]
    assert payload["current"]["map"]["enemies"][0]["kind"] == "RANGER"
    assert "do-not-expose" not in body.decode()


def test_dashboard_projects_only_redacted_command_center_trace_fields(tmp_path: Path):
    replay = tmp_path / "replay.jsonl"
    trace = tmp_path / "decision-trace.jsonl"
    trace.write_text(json.dumps({
        "record_type": "decision_trace", "tick": 11, "planner_version": "test",
        "entity_traces": [{"actor_alias": "entity_0123456789ab", "entity_kind": "WORKER",
                           "current_task": "HARVEST", "action": "MOVE", "status": "RUNNING",
                           "reason_codes": ["current_resource"], "node_path": [{"node_id": "worker.root", "status": "RUNNING", "reason": "ok"}],
                           "secret": "do-not-expose"}],
        "goal_summaries": [{"goal": "CONTROL_BEACON", "status": "SHADOW", "stage": "PICKUP", "uuid": "raw-id"}],
        "task_transitions": [{"task_id": "beacon_escort", "goal": "beacon_goal", "kind": "BEACON_ESCORT",
                              "status": "BLOCKED", "lock": "beacon:3,0", "assigned_tick": 7,
                              "lease_until_tick": 13, "target": [3, 0], "waited_ticks": 4,
                              "reason": "TARGET_LOCKED", "raw": "nope"}],
        "command_results": [{"command_id": "cmd_1", "type": "EMERGENCY_STOP", "status": "STAGED", "payload": "secret"}],
    }) + "\n", encoding="utf-8")
    code, _, body = _response("/api/dashboard", ServiceStatus(), replay)
    payload = json.loads(body)
    assert code == 200
    assert payload["command_center"]["entities"][0]["alias"] == "entity_0123456789ab"
    assert payload["command_center"]["tasks"][0]["task_id"] == "beacon_escort"
    task = payload["command_center"]["tasks"][0]
    assert task["goal"] == "beacon_goal" and task["lock"] == "beacon:3,0"
    assert task["lease_until_tick"] == 13 and task["target"] == [3, 0]
    assert payload["command_center"]["timeline"] == [{"tick": 11, **task}]
    assert "do-not-expose" not in body.decode()
    assert "raw-id" not in body.decode()


def test_dashboard_api_does_not_expose_sensitive_or_unapproved_fields(tmp_path: Path):
    secret = "super-secret-token"
    replay = tmp_path / "replay.jsonl"
    replay.write_text(json.dumps({
        "tick": 3,
        "mode": "DEFEND",
        "api_key": secret,
        "Authorization": f"Bearer {secret}",
        "state": {"resources": 1, "resource_capacity": 10, "population": 1, "cookie": secret},
        "intents": [{"action": "WAIT", "target": "12345678-1234-1234-1234-123456789012"}],
        "events": [{"type": "WAITED", "actor": "12345678-1234-1234-1234-123456789012"}],
    }) + "\n", encoding="utf-8")
    status = ServiceStatus(last_error=f"Authorization: Bearer {secret}; object=12345678-1234-1234-9234-123456789012")
    _, _, body = _response("/api/dashboard", status, replay)
    text = body.decode()
    assert secret not in text
    assert "12345678-1234-1234-1234-123456789012" not in text
    assert "api_key" not in text.lower()
    assert "[已脱敏]" in text
    _, _, status_body = _response("/status", status, replay)
    assert secret not in status_body.decode()
    assert "12345678-1234-1234-9234-123456789012" not in status_body.decode()


def test_full_canary_environment_flag_is_explicit_and_enables_the_complete_opt_in_pipeline(monkeypatch):
    monkeypatch.setenv("ARENA_HERO_FULL_CANARY", "1")

    config = _runtime_config_from_environment()

    assert config.planner_canary and config.scheduler_canary
    assert config.worker_bt_canary and config.vanguard_bt_canary and config.ranger_bt_canary and config.core_bt_canary
    assert config.beacon_campaign_v1 and config.core_migration_v1 and config.core_attack_campaign_v1
