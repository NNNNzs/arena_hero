from __future__ import annotations

import json
from pathlib import Path

import arena_tactic.dashboard as dashboard_module
from arena_tactic.dashboard import DashboardDataStore
import tactic as tactic_module
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
    assert "taskPriority" in body.decode()
    assert "单位状态与决策链" in body.decode()
    assert "map-axis-x" in body.decode()
    assert "visionMode" in body.decode()
    assert "mapDebugHud" in body.decode()
    assert "当前任务" not in body.decode()  # labels are rendered from live entity cards
    static = _http_response("/static/command-center.js", status, DashboardDataStore(replay, cache_seconds=0))
    assert static is not None and static[2].startswith("application/javascript") and b"setPolicy" in static[1]
    assert b"cancelEntity" in static[1] and b"syncEntityChoices" in static[1]
    assert "下一步" in static[1].decode() and b"state_synced" in static[1]
    static_text = static[1].decode()
    for chinese_label in ("当前任务", "当前动作", "下一步", "触发条件", "决策链", "执行中", "移动", "采集资源", "紧急停机"):
        assert chinese_label in static_text
    for dictionary_key in ("actionLabels", "statusLabels", "goalLabels", "reasonLabels", "wakeLabels", "commandLabels"):
        assert dictionary_key in static_text
    map_asset = _http_response("/static/tactical-map/main.js", status, DashboardDataStore(replay, cache_seconds=0))
    assert map_asset is not None and map_asset[2].startswith("application/javascript")
    map_text = map_asset[1].decode()
    assert "renderTacticalMap" in map_text and "TacticalMap" in map_text
    assert "getTacticalMapStats" in map_text and "removeChildren()" not in map_text
    assert "refreshInFlight" in static_text and "visibilitychange" in static_text

    status_code, _, body = _response("/status", status, replay)
    payload = json.loads(body)
    assert status_code == 200
    assert payload["last_tick"] == 42
    assert payload["accepted"] == 7


def test_built_vue_assets_are_isolated_and_have_content_types(tmp_path: Path, monkeypatch):
    app_root = tmp_path / "app"
    (app_root / "assets").mkdir(parents=True)
    (app_root / "index.html").write_text("<div id='app'></div>", encoding="utf-8")
    (app_root / "assets" / "main.js").write_text("console.log('ok')", encoding="utf-8")
    monkeypatch.setattr(dashboard_module, "_APP_ROOT", app_root)

    index = dashboard_module.dashboard_static_asset("/static/app/index.html")
    script = dashboard_module.dashboard_static_asset("/static/app/assets/main.js")
    assert index == (b"<div id='app'></div>", "text/html; charset=utf-8")
    assert script == (b"console.log('ok')", "application/javascript; charset=utf-8")
    assert dashboard_module.dashboard_static_asset("/static/app/../secret.txt") is None


def test_missing_vue_build_is_generated_with_locked_pnpm_commands(tmp_path: Path, monkeypatch):
    frontend_root = tmp_path / "frontend"
    frontend_root.mkdir()
    (frontend_root / "package.json").write_text("{}", encoding="utf-8")
    app_index = tmp_path / "app" / "index.html"
    commands: list[tuple[list[str], Path, bool]] = []

    def fake_run(command: list[str], *, cwd: Path, check: bool):
        commands.append((command, cwd, check))
        if command[-1] == "build":
            app_index.parent.mkdir(parents=True)
            app_index.write_text("built", encoding="utf-8")

    monkeypatch.setattr(tactic_module, "_FRONTEND_ROOT", frontend_root)
    monkeypatch.setattr(tactic_module, "_FRONTEND_APP_INDEX", app_index)
    monkeypatch.setattr(tactic_module.shutil, "which", lambda name: "/usr/local/bin/pnpm")
    monkeypatch.setattr(tactic_module.subprocess, "run", fake_run)

    tactic_module.ensure_frontend_built()

    assert [command[0] for command in commands] == [
        ["/usr/local/bin/pnpm", "install", "--frozen-lockfile"],
        ["/usr/local/bin/pnpm", "build"],
    ]
    assert all(cwd == frontend_root and check for _, cwd, check in commands)


def test_dashboard_api_tolerates_missing_and_truncated_replay(tmp_path: Path):
    replay = tmp_path / "replay.jsonl"
    status = ServiceStatus(connected=True, last_tick=9)
    code, _, body = _response("/api/dashboard", status, replay)
    empty = json.loads(body)
    assert code == 200
    assert empty["current"] is None
    assert "recent" not in empty
    assert "replay" not in empty

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
        "core": {"id": "corealias", "kind": "CORE", "position": [0, 0], "hp": 5, "vision_radius": 5},
        "units": [{"id": "unitalias", "kind": "UNIT", "unit_type": "WORKER", "position": [1, 0], "hp": 2, "cargo": 0}],
        "visible_enemies": [{"id": "enemyalias", "kind": "UNIT", "unit_type": "RANGER", "position": [2, 0], "hp": 2}],
        "resource_cells": [[1, 1]], "obstacle_cells": [[0, 1]], "observed_cells": [[0, 0], [1, 0], [0, 1]], "beacon": {"position": [3, 0], "status": "GROUND"},
    }, "intents": [], "events": [], "secret": "do-not-expose"}) + "\n", encoding="utf-8")
    _, _, body = _response("/api/dashboard", ServiceStatus(), replay)
    payload = json.loads(body)
    assert payload["current"]["map"]["friendly"][0]["position"] == [0, 0]
    assert payload["current"]["map"]["enemies"][0]["kind"] == "RANGER"
    assert payload["current"]["map"]["friendly"][0]["vision_radius"] == 5
    assert payload["current"]["map"]["observed"] == [[0, 0], [1, 0], [0, 1]]
    assert "do-not-expose" not in body.decode()


def test_dashboard_replay_frames_align_decisions_and_classify_event_markers(tmp_path: Path):
    replay = tmp_path / "replay.jsonl"
    trace = tmp_path / "decision-trace.jsonl"
    replay.write_text("\n".join(json.dumps(record) for record in (
        {"tick": 20, "state": {"units": []}, "intents": [{"action": "MOVE"}],
         "events": [{"type": "UNIT_DAMAGED"}]},
        {"tick": 21, "state": {"units": []}, "intents": [{"action": "SPAWN"}],
         "events": [{"type": "CORE_SPAWN_SUCCEEDED"}]},
    )) + "\n", encoding="utf-8")
    trace.write_text(json.dumps({"record_type": "decision_trace", "tick": 20,
        "entity_traces": [], "goal_summaries": [], "task_transitions": [], "command_results": []}) + "\n", encoding="utf-8")

    store = DashboardDataStore(replay, trace_path=trace, cache_seconds=0)
    result = _http_response("/api/replay", ServiceStatus(), store)
    assert result is not None
    _, body, _ = result
    payload = json.loads(body)
    frames = payload["frames"]
    assert [frame["tick"] for frame in frames] == [20, 21]
    assert {marker["kind"] for marker in frames[0]["markers"]} == {"DAMAGE", "MOVE"}
    assert {marker["kind"] for marker in frames[1]["markers"]} == {"SPAWN"}
    assert frames[0]["command_center"]["state_synced"] is True
    assert frames[1]["command_center"] is None
    assert payload["ticks"] == [20, 21]


def test_dashboard_default_replay_window_is_32_ticks(tmp_path: Path):
    replay = tmp_path / "replay.jsonl"
    replay.write_text("\n".join(
        json.dumps({"tick": tick, "state": {"units": []}, "intents": [], "events": []})
        for tick in range(1, 41)
    ) + "\n", encoding="utf-8")

    store = DashboardDataStore(replay, cache_seconds=0)
    _, body, _ = _http_response("/api/replay", ServiceStatus(), store)
    payload = json.loads(body)
    frames = payload["frames"]

    assert len(frames) == 32
    assert [frames[0]["tick"], frames[-1]["tick"]] == [9, 40]
    assert payload["ticks"][0] == 9 and payload["ticks"][-1] == 40


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


def test_dashboard_projects_mode_and_squad_causality_without_raw_trace_fields(tmp_path: Path):
    replay = tmp_path / "replay.jsonl"
    trace = tmp_path / "decision-trace.jsonl"
    replay.write_text(json.dumps({"tick": 11, "mode": "ATTACK", "state": {
        "resources": 8, "resource_capacity": 10, "population": 1,
        "core": {"id": "entity_000000000001", "kind": "CORE", "position": [0, 0], "hp": 5},
        "units": [{"id": "entity_0123456789ab", "kind": "UNIT", "unit_type": "RANGER", "position": [2, 1], "hp": 2}],
    }, "intents": [], "events": []}) + "\n", encoding="utf-8")
    trace.write_text(json.dumps({"record_type": "decision_trace", "tick": 11,
        "entity_traces": [], "goal_summaries": [], "task_transitions": [], "command_results": [],
        "causality": {"mode": {"mode": "ATTACK", "previous_mode": "BEACON", "changed": True,
            "duration_ticks": 1, "rule_id": "MODE_ATTACK", "summary": "visible enemy core",
            "exit_condition": "ENEMY_CORE_LOST", "source_cell": [8, 9],
            "preconditions": {"combat_count": 3, "secret": {"not": "allowed"}}}},
    }) + "\n", encoding="utf-8")
    memory = tmp_path / "agent-state.json"
    memory.write_text(json.dumps({"explored": [[0, 0]], "unit_tasks": {}}), encoding="utf-8")

    result = _http_response("/api/dashboard", ServiceStatus(), DashboardDataStore(
        replay, trace_path=trace, memory_path=memory, cache_seconds=0,
    ))
    assert result is not None
    payload = json.loads(result[1])
    mode = payload["command_center"]["causality"]["mode"]
    assert mode["rule_id"] == "MODE_ATTACK" and mode["source_cell"] == [8, 9]
    assert mode["preconditions"] == {"combat_count": 3}
    squad = payload["squads"]["squads"][3]
    assert squad["causality"]["member_aliases"] == ["entity_0123456789ab"]
    assert "ATTACK（进攻模式）" in squad["causality"]["flow_reason"]


def test_dashboard_merges_same_tick_state_into_unit_decision_card(tmp_path: Path):
    replay = tmp_path / "replay.jsonl"
    trace = tmp_path / "decision-trace.jsonl"
    replay.write_text(json.dumps({"tick": 11, "state": {
        "resources": 4, "resource_capacity": 10, "population": 1,
        "core": {"id": "entity_000000000001", "kind": "CORE", "position": [0, 0], "hp": 5, "shield": 3, "state": "NORMAL"},
        "units": [{"id": "entity_0123456789ab", "kind": "UNIT", "unit_type": "WORKER", "position": [2, 1], "hp": 2, "cargo": 1}],
    }, "intents": [], "events": []}) + "\n", encoding="utf-8")
    trace.write_text(json.dumps({"record_type": "decision_trace", "tick": 11,
        "entity_traces": [{"actor_alias": "entity_0123456789ab", "entity_kind": "WORKER",
                           "current_task": "HARVEST_RESOURCE", "goal": "ECONOMY", "action": "MOVE",
                           "status": "RUNNING", "task_status": "RUNNING", "assignment_status": "SCHEDULED",
                           "current_cell": [2, 1], "target_cell": [3, 1], "next_step": "HARVEST",
                           "wake_condition": "arrive_at_resource", "wait_kind": "ROUTE_BLOCKED", "eta_ticks": 2,
                           "reason_codes": ["path_to_resource"], "node_path": [],
                           "candidate_intents": [{"action": "MOVE", "direction": "RIGHT", "score": 750}] }],
        "goal_summaries": [], "task_transitions": [], "command_results": []}) + "\n", encoding="utf-8")
    result = _http_response("/api/dashboard", ServiceStatus(), DashboardDataStore(replay, trace_path=trace, cache_seconds=0))
    assert result is not None
    _, body, _ = result
    entity = json.loads(body)["command_center"]["entities"][0]
    assert entity["state_synced"] is True and entity["position"] == [2, 1]
    assert entity["hp"] == 2 and entity["cargo"] == 1
    assert entity["next_step"] == "HARVEST" and entity["wake_condition"] == "arrive_at_resource"
    assert entity["wait_kind"] == "ROUTE_BLOCKED"
    assert entity["candidate_intents"][0]["action"] == "MOVE"


def test_dashboard_marks_stale_trace_without_merging_state(tmp_path: Path):
    replay = tmp_path / "replay.jsonl"
    trace = tmp_path / "decision-trace.jsonl"
    replay.write_text(json.dumps({"tick": 12, "state": {"resources": 1, "resource_capacity": 10, "population": 1,
        "units": [{"id": "entity_0123456789ab", "kind": "UNIT", "unit_type": "WORKER", "position": [8, 8], "hp": 2, "cargo": 0}]}, "intents": [], "events": []}) + "\n", encoding="utf-8")
    trace.write_text(json.dumps({"record_type": "decision_trace", "tick": 11,
        "entity_traces": [{"actor_alias": "entity_0123456789ab", "entity_kind": "WORKER", "current_task": "HARVEST",
                           "goal": "ECONOMY", "action": "MOVE", "status": "RUNNING", "reason_codes": ["stale"]}],
        "goal_summaries": [], "task_transitions": [], "command_results": []}) + "\n", encoding="utf-8")
    result = _http_response("/api/dashboard", ServiceStatus(), DashboardDataStore(replay, trace_path=trace, cache_seconds=0))
    assert result is not None
    _, body, _ = result
    entity = json.loads(body)["command_center"]["entities"][0]
    assert entity["state_synced"] is False and entity["position"] is None
    assert entity["state_sync_label"] == "等待下一份权威状态"


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


def test_dashboard_payload_injects_known_resources_from_memory(tmp_path: Path):
    replay = tmp_path / "replay.jsonl"
    memory = tmp_path / "agent-state.json"
    replay.write_text(json.dumps({
        "tick": 5, "mode": "ECONOMY", "state": {
            "resources": 3, "resource_capacity": 10, "population": 1,
            "core": {"id": "c1", "kind": "CORE", "position": [0, 0], "hp": 5},
            "units": [], "resource_cells": [[2, 0]],
        }, "intents": [], "events": [],
    }) + "\n", encoding="utf-8")
    memory.write_text(json.dumps({
        "explored": [[0, 0], [1, 0], [2, 0]],
        "mined_cells": [[2, 0]],
        "resource_observations": {"2,0": 5, "10,20": 3, "-1,3": 1},
    }), encoding="utf-8")
    store = DashboardDataStore(replay, memory_path=memory, cache_seconds=0)
    _, body, _ = _http_response(
        "/api/dashboard", ServiceStatus(connected=True, last_tick=5), store,
    )
    payload = json.loads(body)
    assert payload["current"]["map"]["memory_version"] != 0
    assert payload["map_memory_version"] != 0

    # Static memory endpoint verifies compressed segments and full memory
    res = _http_response("/api/map/memory", ServiceStatus(), store)
    assert res is not None
    _, mem_body, _ = res
    mem_payload = json.loads(mem_body)
    assert mem_payload["version"] == payload["current"]["map"]["memory_version"]
    assert mem_payload["explored_segments"] == [[0, 2, 0]]
    assert mem_payload["mined"] == [[2, 0]]
    kr = mem_payload["known_resources"]
    assert [2, 0] in kr
    assert [10, 20] in kr
    assert [-1, 3] in kr


def test_replay_endpoint_returns_default_32_frames(tmp_path: Path):
    replay = tmp_path / "replay.jsonl"
    replay.write_text("\n".join(
        json.dumps({"tick": tick, "state": {"units": []}, "intents": [], "events": []})
        for tick in range(1, 51)
    ) + "\n", encoding="utf-8")
    store = DashboardDataStore(replay, cache_seconds=0)
    result = _http_response("/api/replay", ServiceStatus(), store)
    assert result is not None
    _, body, _ = result
    payload = json.loads(body)
    assert len(payload["frames"]) == 32
    assert len(payload["ticks"]) == 32
    assert payload["ticks"][0] == 19 and payload["ticks"][-1] == 50


def test_replay_endpoint_respects_limit_parameter(tmp_path: Path):
    replay = tmp_path / "replay.jsonl"
    replay.write_text("\n".join(
        json.dumps({"tick": tick, "state": {"units": []}, "intents": [], "events": []})
        for tick in range(1, 21)
    ) + "\n", encoding="utf-8")
    store = DashboardDataStore(replay, cache_seconds=0)
    result = _http_response("/api/replay?limit=5", ServiceStatus(), store)
    assert result is not None
    _, body, _ = result
    payload = json.loads(body)
    assert len(payload["frames"]) == 5
    assert len(payload["ticks"]) == 5
    assert payload["ticks"][-1] == 20


def test_replay_endpoint_filters_by_from_tick(tmp_path: Path):
    replay = tmp_path / "replay.jsonl"
    replay.write_text("\n".join(
        json.dumps({"tick": tick, "state": {"units": []}, "intents": [], "events": []})
        for tick in range(1, 21)
    ) + "\n", encoding="utf-8")
    store = DashboardDataStore(replay, cache_seconds=0)
    result = _http_response("/api/replay?from_tick=15&limit=32", ServiceStatus(), store)
    assert result is not None
    _, body, _ = result
    payload = json.loads(body)
    frames = payload["frames"]
    assert all(frame["tick"] > 15 for frame in frames)
    assert len(frames) == 5  # ticks 16..20
    assert payload["ticks"] == [16, 17, 18, 19, 20]


def test_replay_endpoint_without_from_tick_returns_full_batch(tmp_path: Path):
    replay = tmp_path / "replay.jsonl"
    replay.write_text("\n".join(
        json.dumps({"tick": tick, "state": {"units": []}, "intents": [], "events": []})
        for tick in range(1, 11)
    ) + "\n", encoding="utf-8")
    store = DashboardDataStore(replay, cache_seconds=0)
    result = _http_response("/api/replay?limit=10", ServiceStatus(), store)
    assert result is not None
    _, body, _ = result
    payload = json.loads(body)
    assert len(payload["frames"]) == 10
    assert payload["ticks"] == list(range(1, 11))


def test_dashboard_api_no_longer_contains_replay_or_recent_keys(tmp_path: Path):
    replay = tmp_path / "replay.jsonl"
    replay.write_text(json.dumps({
        "tick": 5, "mode": "EXPLORE", "state": {"resources": 3, "resource_capacity": 10, "population": 1},
        "intents": [], "events": [],
    }) + "\n", encoding="utf-8")
    code, _, body = _response("/api/dashboard", ServiceStatus(connected=True, last_tick=5), replay)
    payload = json.loads(body)
    assert code == 200
    assert "replay" not in payload
    assert "recent" not in payload
    assert "schema_version" in payload
    assert "generated_at" in payload
    assert "service" in payload
    assert "current" in payload
    assert "command_center" in payload


def test_events_endpoint_pagination_and_filter(tmp_path: Path):
    replay = tmp_path / "replay.jsonl"
    replay.write_text("\n".join([
        json.dumps({"tick": 10, "events": [{"type": "CORE_DAMAGED", "position": [1, 1], "target": "core"}]}),
        json.dumps({"tick": 15, "events": [{"type": "HARVEST_SUCCEEDED", "position": [2, 2], "values": {"source": "RESOURCE_NODE"}}]}),
        json.dumps({"tick": 20, "events": [{"type": "DEPOSIT_FAILED", "position": [1, 1], "reason": "FULL"}]}),
    ]) + "\n", encoding="utf-8")
    store = DashboardDataStore(replay, cache_seconds=0)

    # All events
    res = _http_response("/api/events?limit=50", ServiceStatus(), store)
    assert res is not None
    _, body, _ = res
    payload = json.loads(body)
    assert payload["total"] >= 3
    assert len(payload["events"]) >= 3

    # Filter by category
    res_combat = _http_response("/api/events?category=combat", ServiceStatus(), store)
    assert res_combat is not None
    payload_combat = json.loads(res_combat[1])
    assert payload_combat["matched"] == 1
    assert payload_combat["events"][0]["type"] == "CORE_DAMAGED"

    # Filter by from_tick and to_tick
    res_range = _http_response("/api/events?from_tick=12&to_tick=18", ServiceStatus(), store)
    assert res_range is not None
    payload_range = json.loads(res_range[1])
    assert payload_range["matched"] == 2
    assert any(item["type"] == "HARVEST_SUCCEEDED" for item in payload_range["events"])


def test_replay_timeline_endpoint(tmp_path: Path):
    replay = tmp_path / "replay.jsonl"
    replay.write_text("\n".join(
        json.dumps({"tick": tick, "state": {"units": []}, "intents": [], "events": [{"type": "CORE_DAMAGED"}] if tick % 5 == 0 else []})
        for tick in range(1, 11)
    ) + "\n", encoding="utf-8")
    store = DashboardDataStore(replay, cache_seconds=0)
    res = _http_response("/api/replay/timeline?limit=10", ServiceStatus(), store)
    assert res is not None
    _, body, _ = res
    payload = json.loads(body)
    assert payload["ticks"] == list(range(1, 11))
    assert payload["latest_tick"] == 10
    assert len(payload["timeline"]) == 10
    markers_at_5 = next(item["markers"] for item in payload["timeline"] if item["tick"] == 5)
    assert any(m["kind"] == "DAMAGE" for m in markers_at_5)
