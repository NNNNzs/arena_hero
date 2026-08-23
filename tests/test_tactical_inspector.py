from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts" / "tactical_inspector.py"
SPEC = importlib.util.spec_from_file_location("tactical_inspector", SCRIPT)
assert SPEC and SPEC.loader
inspector = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(inspector)


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def _row(tick: int, position: list[int], *, mode: str = "ECONOMY", damaged: bool = False, migration_event: str | None = None) -> dict:
    events = []
    if damaged:
        events.append({"type": "CORE_DAMAGED", "target": "core", "values": {"damage": 1, "hp_damage": 1}})
    if migration_event:
        events.append({"type": migration_event, "actor": "core", "values": {}})
    return {
        "tick": tick,
        "mode": mode,
        "events": events,
        "intents": [
            {"actor": "guard", "action": "WAIT", "reason": "holding_defense_ring"},
            {"actor": "worker", "action": "MOVE", "reason": "explore_sector_frontier"},
        ],
        "state": {
            "core": {"id": "core", "position": [0, 0], "hp": 4, "shield": 0, "state": "NORMAL", "destination": None},
            "units": [
                {"id": "guard", "unit_type": "VANGUARD", "position": [1, 0], "hp": 4},
                {"id": "worker", "unit_type": "WORKER", "position": position, "hp": 2, "cargo": 0},
            ],
            "visible_enemies": [], "resource_cells": [], "resources": 3,
            "resource_capacity": 10, "population": 2,
        },
    }


def test_detects_oscillation_hidden_attack_and_migration_loop(tmp_path, monkeypatch):
    rows = []
    modes = ["ECONOMY", "DEFEND"] * 4
    for offset in range(8):
        rows.append(_row(100 + offset, [2 + offset % 2, 0], mode=modes[offset], damaged=offset in {2, 5}, migration_event="CORE_MOVE_START_FAILED" if offset < 3 else None))
    _write_jsonl(tmp_path / "replay.jsonl", rows)
    _write_jsonl(tmp_path / "decision-trace.jsonl", [])
    (tmp_path / "agent-state.json").write_text(json.dumps({"no_resource_ticks": 20, "last_mode": "DEFEND"}))
    monkeypatch.setattr(inspector, "_health", lambda *args: {"reachable": False, "http_status": None})

    report = inspector.inspect(tmp_path, 100, 1024 * 1024, "unused", .1)
    codes = {finding["code"] for finding in report["findings"]}
    assert {"HIDDEN_CORE_ATTACK", "CORE_MIGRATION_LOOP"} <= codes
    assert "INEFFECTIVE_STATIONARY" not in codes  # intentional ring defense is not a deadlock
    assert report["economy"]["worker_status_counts"] == {"moving_or_exploring": 1}


def test_rotated_tail_is_bounded_deduplicated_and_tolerates_bad_lines(tmp_path):
    old = [_row(tick, [tick % 2, 0]) for tick in range(1, 6)]
    current = [_row(tick, [tick % 2, 0]) for tick in range(5, 10)]
    _write_jsonl(tmp_path / "replay.jsonl.1", old)
    _write_jsonl(tmp_path / "replay.jsonl", current)
    with (tmp_path / "replay.jsonl").open("a", encoding="utf-8") as handle:
        handle.write("{partial\n")

    records, quality = inspector.read_rotated_jsonl(tmp_path, "replay.jsonl", 5, 1024 * 1024)
    assert [record["tick"] for record in records] == [5, 6, 7, 8, 9]
    assert quality["malformed_lines"] == 1


def test_text_and_json_model_are_serializable(tmp_path, monkeypatch):
    _write_jsonl(tmp_path / "replay.jsonl", [_row(1, [1, 1])])
    (tmp_path / "agent-state.json").write_text("{}")
    monkeypatch.setattr(inspector, "_health", lambda *args: {"reachable": True, "http_status": 200, "payload": "ok"})
    report = inspector.inspect(tmp_path, 50, 4096, "unused", .1)
    assert "Arena Hero 深度战术态势摘要" in inspector.render_text(report)
    assert json.loads(json.dumps(report))["schema_version"] == 1


def _inspect_rows(tmp_path: Path, monkeypatch, rows: list[dict], traces: list[dict] | None = None) -> dict:
    _write_jsonl(tmp_path / "replay.jsonl", rows)
    _write_jsonl(tmp_path / "decision-trace.jsonl", traces or [])
    (tmp_path / "agent-state.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(inspector, "_health", lambda *args: {"reachable": True, "http_status": 200})
    return inspector.inspect(tmp_path, 100, 1024 * 1024, "unused", .1)


def test_detects_cargo_delivery_stagnation(tmp_path, monkeypatch):
    rows = []
    for tick in range(1, 7):
        row = _row(tick, [2, 0])
        row["state"]["units"][1]["cargo"] = 3
        rows.append(row)

    report = _inspect_rows(tmp_path, monkeypatch, rows)
    finding = next(f for f in report["findings"] if f["code"] == "CARGO_DELIVERY_STAGNATION")
    assert finding["entities"] == ["worker"]
    assert finding["evidence"][0]["distance_progress"] == 0


def test_detects_production_freeze(tmp_path, monkeypatch):
    rows = []
    for tick in range(10, 16):
        row = _row(tick, [tick, 0], mode="ECONOMY")
        row["state"].update(resources=15, resource_capacity=50, population=5)
        rows.append(row)

    report = _inspect_rows(tmp_path, monkeypatch, rows)
    finding = next(f for f in report["findings"] if f["code"] == "PRODUCTION_FREEZE")
    assert finding["evidence"][0]["tick_range"] == [10, 15]


def test_detects_unanswered_unit_damage(tmp_path, monkeypatch):
    rows = []
    for tick, hp in enumerate((4, 3, 2), start=20):
        row = _row(tick, [5, 5], mode="DEFEND")
        row["state"]["units"][0]["hp"] = hp
        row["state"]["visible_enemies"] = [{"id": "enemy", "unit_type": "RANGER", "position": [3, 0], "hp": 2}]
        rows.append(row)

    report = _inspect_rows(tmp_path, monkeypatch, rows)
    finding = next(f for f in report["findings"] if f["code"] == "UNANSWERED_DAMAGE")
    assert finding["entities"] == ["guard"]
    assert finding["evidence"][0]["hp_start"] == 4
    assert finding["evidence"][0]["hp_end"] == 2


def test_detects_isolated_beacon_carrier(tmp_path, monkeypatch):
    row = _row(30, [1, 1], mode="BEACON")
    row["state"]["units"][0]["position"] = [8, 0]
    row["state"]["units"].append({"id": "carrier", "unit_type": "VANGUARD", "position": [0, 0], "hp": 4})
    row["state"]["beacon"] = {"position": [0, 0], "status": "CARRIED", "carrier": "carrier"}

    report = _inspect_rows(tmp_path, monkeypatch, [row])
    finding = next(f for f in report["findings"] if f["code"] == "BEACON_CARRIER_ISOLATED")
    assert finding["entities"] == ["carrier"]
    assert finding["evidence"][0]["nearest_combat_ally_distance"] == 8


def test_detects_decision_latency_spike(tmp_path, monkeypatch):
    traces = [
        {"tick": 40, "timings": {"decision_ms": 120.0}, "entity_traces": []},
        {"tick": 41, "timings": {"decision_ms": 2500.0}, "entity_traces": []},
    ]
    report = _inspect_rows(tmp_path, monkeypatch, [_row(40, [1, 1]), _row(41, [2, 1])], traces)
    finding = next(f for f in report["findings"] if f["code"] == "DECISION_LATENCY_SPIKE")
    assert finding["ticks"] == [41]
    assert finding["evidence"]["threshold_ms"] == 2000.0


def _exploration_stall_rows(num_ticks: int, positions: list[list[int]], *, start_tick: int = 100) -> list[dict]:
    """Build replay rows where a worker visits the given positions cyclically."""
    rows = []
    for i in range(num_ticks):
        pos = positions[i % len(positions)]
        rows.append(_row(start_tick + i, pos))
    return rows


def test_detects_exploration_stall_worker_high_steps_low_displacement(tmp_path, monkeypatch):
    """Worker bounces between two cells for 50 ticks → steps=49, net=1."""
    # Worker oscillates between [5,0] and [6,0] — many steps, near-zero displacement
    positions = [[5, 0], [6, 0]] * 25  # 50 rows, alternating
    rows = _exploration_stall_rows(50, positions)
    report = _inspect_rows(tmp_path, monkeypatch, rows)
    finding = next(f for f in report["findings"] if f["code"] == "EXPLORATION_STALL")
    worker_evidence = [e for e in finding["evidence"] if e["kind"] == "WORKER"]
    assert len(worker_evidence) == 1
    assert worker_evidence[0]["entity"] == "worker"
    assert worker_evidence[0]["steps"] >= 40
    assert worker_evidence[0]["net_displacement"] <= worker_evidence[0]["steps"] * 0.1


def test_detects_exploration_stall_resource_exploration_dead(tmp_path, monkeypatch):
    """no_resource_ticks ≥ 600 and no resource cells visible → resource exploration stall."""
    rows = [_row(tick, [1, 1]) for tick in range(100, 110)]
    _write_jsonl(tmp_path / "replay.jsonl", rows)
    _write_jsonl(tmp_path / "decision-trace.jsonl", [])
    (tmp_path / "agent-state.json").write_text(
        json.dumps({"no_resource_ticks": 800, "last_mode": "ECONOMY"})
    )
    monkeypatch.setattr(inspector, "_health", lambda *args: {"reachable": True, "http_status": 200})
    report = inspector.inspect(tmp_path, 100, 1024 * 1024, "unused", .1)
    finding = next(f for f in report["findings"] if f["code"] == "EXPLORATION_STALL")
    resource_evidence = [e for e in finding["evidence"] if e["kind"] == "RESOURCE_EXPLORATION"]
    assert len(resource_evidence) == 1
    assert resource_evidence[0]["no_resource_ticks"] == 800


def test_no_exploration_stall_when_worker_makes_progress(tmp_path, monkeypatch):
    """Worker moves consistently in one direction → no stall."""
    # Worker at (0,0) moves right each tick: high displacement, no stall
    rows = [_row(tick, [tick, 0]) for tick in range(100, 150)]
    report = _inspect_rows(tmp_path, monkeypatch, rows)
    stall = [f for f in report["findings"] if f["code"] == "EXPLORATION_STALL"]
    assert stall == []  # no stall detected


def test_no_exploration_stall_below_step_threshold(tmp_path, monkeypatch):
    """Only 20 ticks of oscillation → below 40-step threshold, no stall."""
    positions = [[3, 3], [4, 3]] * 10  # 20 rows
    rows = _exploration_stall_rows(20, positions)
    report = _inspect_rows(tmp_path, monkeypatch, rows)
    stall = [f for f in report["findings"] if f["code"] == "EXPLORATION_STALL"]
    assert stall == []


def test_no_exploration_stall_when_worker_deposits(tmp_path, monkeypatch):
    """Worker oscillating between mine and core with successful deposits is not stalled."""
    positions = [[0, 0], [1, 0]] * 25  # 50 rows
    rows = _exploration_stall_rows(50, positions)
    # Add a DEPOSIT_SUCCEEDED event for the worker
    rows[10]["events"] = [{"type": "DEPOSIT_SUCCEEDED", "actor": "worker", "values": {"amount": 2}}]
    report = _inspect_rows(tmp_path, monkeypatch, rows)
    stall = [f for f in report["findings"] if f["code"] == "EXPLORATION_STALL"]
    assert stall == []


def test_detects_defense_disengaged_when_threat_is_near(tmp_path, monkeypatch):
    """Combat unit WAITs when enemy Vanguard is within local threat distance (7-16)."""
    rows = []
    for tick in range(10, 15):
        row = _row(tick, [0, 0], mode="DEFEND")
        row["state"]["units"] = [{"id": "vanguard_1", "unit_type": "VANGUARD", "position": [0, 0], "hp": 10}]
        row["state"]["visible_enemies"] = [{"id": "enemy_vg", "unit_type": "VANGUARD", "position": [8, 0], "hp": 10}]
        row["intents"] = [{"actor": "vanguard_1", "action": "WAIT", "reason": "holding_position"}]
        rows.append(row)
    report = _inspect_rows(tmp_path, monkeypatch, rows)
    finding = next((f for f in report["findings"] if f["code"] == "DEFENSE_DISENGAGED"), None)
    assert finding is not None
    assert finding["evidence"][0]["units"][0]["enemy_distance"] == 8


def test_no_defense_disengaged_when_threat_is_distant(tmp_path, monkeypatch):
    """Combat unit guarding Core (distance > 16 from distant scout enemy) is not disengaged."""
    rows = []
    for tick in range(10, 15):
        row = _row(tick, [0, 0], mode="ECONOMY")
        row["state"]["units"] = [{"id": "vanguard_1", "unit_type": "VANGUARD", "position": [0, 0], "hp": 10}]
        # Enemy is 50 tiles away (seen by distant scout)
        row["state"]["visible_enemies"] = [{"id": "enemy_vg", "unit_type": "VANGUARD", "position": [50, 0], "hp": 10}]
        row["intents"] = [{"actor": "vanguard_1", "action": "WAIT", "reason": "holding_defense_ring"}]
        rows.append(row)
    report = _inspect_rows(tmp_path, monkeypatch, rows)
    finding = next((f for f in report["findings"] if f["code"] == "DEFENSE_DISENGAGED"), None)
    assert finding is None


def _main_report(severity: str) -> dict:
    return {
        "window": {"tick_start": 1, "tick_end": 2},
        "service": {"reachable": True},
        "economy": {"core_resources": 3, "capacity": 10},
        "battlefield": {"visible_enemy_count": 1},
        "strategy": {"current_mode": "DEFEND"},
        "findings": [{"code": "CORE_UNDER_ATTACK" if severity == "critical" else "PRODUCTION_FREEZE", "severity": severity, "summary": "test"}],
        "tactical_clues": [],
    }


@pytest.mark.parametrize("force_alert", [False, True])
def test_quiet_hours_always_emit_critical_alert(monkeypatch, capsys, force_alert):
    class QuietDatetime:
        @classmethod
        def now(cls):
            return type("Now", (), {"hour": 2})()

    monkeypatch.setattr(inspector, "datetime", QuietDatetime)
    monkeypatch.setattr(inspector, "inspect", lambda *args: _main_report("critical"))
    argv = ["--alert-only"] + (["--force-alert"] if force_alert else [])
    assert inspector.main(argv) == 0
    assert "CORE_UNDER_ATTACK (核心正在遭受攻击)" in capsys.readouterr().out


def test_quiet_hours_keep_warning_silent_with_force_alert(monkeypatch, capsys):
    class QuietDatetime:
        @classmethod
        def now(cls):
            return type("Now", (), {"hour": 2})()

    monkeypatch.setattr(inspector, "datetime", QuietDatetime)
    monkeypatch.setattr(inspector, "inspect", lambda *args: _main_report("warning"))
    assert inspector.main(["--alert-only", "--force-alert"]) == 0
    assert capsys.readouterr().out == ""


def test_non_quiet_alert_behavior_is_unchanged(monkeypatch, capsys):
    class DayDatetime:
        @classmethod
        def now(cls):
            return type("Now", (), {"hour": 12})()

    monkeypatch.setattr(inspector, "datetime", DayDatetime)
    monkeypatch.setattr(inspector, "inspect", lambda *args: _main_report("warning"))
    assert inspector.main(["--alert-only"]) == 0
    assert "PRODUCTION_FREEZE (兵营生产冻结)" in capsys.readouterr().out


def test_list_rules_prints_all_registered_rules(capsys):
    assert inspector.main(["--list-rules"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 14
    assert {line.split("\t", 1)[0] for line in lines} == set(inspector.ALERT_RULES)


def test_json_findings_and_registry_use_rule_metadata(tmp_path, monkeypatch):
    report = _inspect_rows(tmp_path, monkeypatch, [_row(1, [1, 1], damaged=True)])
    finding = next(f for f in report["findings"] if f["code"] == "CORE_UNDER_ATTACK")
    assert finding["zh_label"] == "核心正在遭受攻击"
    assert finding["recommendation"]
    assert len(report["alert_rules"]) == 14
