#!/usr/bin/env python3
"""Summarize recent Arena Hero logs for a tactical watchdog/LLM agent.

The inspector is deliberately dependency-free and reads only bounded tails of
JSONL files.  It tolerates partial lines, rotations, older schemas, and missing
runtime files; findings are evidence with confidence, not invented game state.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

DEFAULT_RUNTIME = Path(__file__).resolve().parents[1] / "runtime"
LIVEZ_URL = "http://127.0.0.1:8787/livez"
MODES = {"DEFEND", "ATTACK", "BEACON", "RECOVER", "ECONOMY"}
CARGO_STAGNATION_TICKS = 5
PRODUCTION_FREEZE_TICKS = 5
UNANSWERED_DAMAGE_TICKS = 2
BEACON_ESCORT_DISTANCE = 4
DECISION_SPIKE_MS = 2_000.0


def _tail_lines(path: Path, limit: int, max_bytes: int) -> list[bytes]:
    """Read at most max_bytes from a file's end and return its last lines."""
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            start = max(0, size - max_bytes)
            handle.seek(start)
            data = handle.read(max_bytes)
        lines = data.splitlines()
        if start and lines:
            lines = lines[1:]  # first line can be a truncated JSON object
        return lines[-limit:]
    except OSError:
        return []


def read_rotated_jsonl(runtime: Path, name: str, limit: int, max_bytes: int) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Load newest records across ``name``, ``name.1`` ... without full scans."""
    paths = [runtime / name]
    paths.extend(sorted(runtime.glob(name + ".*"), key=lambda p: int(p.suffix[1:]) if p.suffix[1:].isdigit() else 10_000))
    records: list[dict[str, Any]] = []
    malformed = 0
    # Current file is newest, then .1, .2. Collect backwards and sort/dedupe.
    for path in paths:
        if len(records) >= limit * 2:
            break
        for raw in _tail_lines(path, limit, max_bytes):
            try:
                value = json.loads(raw)
                if isinstance(value, dict):
                    records.append(value)
            except (json.JSONDecodeError, UnicodeDecodeError):
                malformed += 1
    by_tick: dict[int, dict[str, Any]] = {}
    unticked: list[dict[str, Any]] = []
    for record in records:
        tick = record.get("tick")
        if isinstance(tick, int):
            by_tick.setdefault(tick, record)  # current file wins because it was read first
        else:
            unticked.append(record)
    ordered = sorted(by_tick.values(), key=lambda item: item["tick"])[-limit:]
    return (unticked[-limit:] + ordered)[-limit:], {"files_seen": sum(p.exists() for p in paths), "malformed_lines": malformed}


def _load_json(path: Path) -> tuple[dict[str, Any], str | None]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return (value if isinstance(value, dict) else {}), None
    except (OSError, json.JSONDecodeError) as exc:
        return {}, f"{type(exc).__name__}: {exc}"


def _health(url: str, timeout: float) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            body = response.read(4096).decode("utf-8", "replace")
            try:
                payload: Any = json.loads(body)
            except json.JSONDecodeError:
                payload = body.strip()
            return {"reachable": True, "http_status": response.status, "payload": payload}
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return {"reachable": False, "http_status": None, "error": str(exc)}


def _pos(value: Any) -> tuple[int, int] | None:
    if isinstance(value, (list, tuple)) and len(value) == 2 and all(isinstance(v, int) for v in value):
        return value[0], value[1]
    return None


def _distance(a: Any, b: Any) -> int | None:
    pa, pb = _pos(a), _pos(b)
    return abs(pa[0] - pb[0]) + abs(pa[1] - pb[1]) if pa and pb else None


def _event_type(event: dict[str, Any]) -> str:
    return str(event.get("type") or event.get("event_type") or "UNKNOWN")


def _event_reason(event: dict[str, Any]) -> str | None:
    value = event.get("reason") if "reason" in event else event.get("reason_code")
    return str(value) if value is not None else None


def _event_actor(event: dict[str, Any]) -> str:
    """Return the best available actor/target id across replay schema versions."""
    return str(event.get("actor") or event.get("actor_id") or event.get("target") or event.get("target_id") or "unknown")


def _runs(samples: list[dict[str, Any]], minimum: int) -> list[list[dict[str, Any]]]:
    """Split ordered samples into bounded, consecutive-Tick runs."""
    result: list[list[dict[str, Any]]] = []
    run: list[dict[str, Any]] = []
    for sample in samples:
        if run and sample["tick"] != run[-1]["tick"] + 1:
            if len(run) >= minimum:
                result.append(run)
            run = []
        run.append(sample)
    if len(run) >= minimum:
        result.append(run)
    return result


def _finding(code: str, severity: str, summary: str, *, ticks: Iterable[int] = (), entities: Iterable[str] = (), evidence: Any = None, confidence: str = "high") -> dict[str, Any]:
    result = {"code": code, "severity": severity, "summary": summary, "confidence": confidence}
    tick_list, entity_list = list(ticks), list(entities)
    if tick_list:
        result["ticks"] = tick_list[-12:]
    if entity_list:
        result["entities"] = entity_list[:12]
    if evidence is not None:
        result["evidence"] = evidence
    return result


def _oscillation(positions: list[tuple[int, tuple[int, int]]]) -> dict[str, Any] | None:
    if len(positions) < 12:
        return None
    coords = [p for _, p in positions]
    unique_cells = set(coords[-16:])
    # If the unit is exploring across more than 3 distinct cells, it's normal navigation/turning, not a trapped oscillation.
    if len(unique_cells) > 3 or len(unique_cells) < 2:
        return None
    reversals = sum(coords[i] == coords[i - 2] and coords[i] != coords[i - 1] for i in range(2, len(coords)))
    # Require sustained high-frequency back-and-forth trapped in 2~3 cells
    if reversals < max(8, len(coords) // 3):
        return None
    period = next((p for p in (2, 3, 4) if len(coords) >= p * 3 and sum(coords[i] == coords[i-p] for i in range(p, len(coords))) >= len(coords) * 0.6), None)
    return {"period": period or 2, "reversals": reversals, "samples": len(coords), "cells": [list(p) for p in dict.fromkeys(coords[-12:])], "tick_range": [positions[0][0], positions[-1][0]]}


def inspect(runtime: Path, window: int, max_bytes: int, health_url: str, health_timeout: float) -> dict[str, Any]:
    replay, replay_quality = read_rotated_jsonl(runtime, "replay.jsonl", window, max_bytes)
    traces, trace_quality = read_rotated_jsonl(runtime, "decision-trace.jsonl", window, max_bytes)
    agent_state, state_error = _load_json(runtime / "agent-state.json")
    traces_by_tick = {r.get("tick"): r for r in traces if isinstance(r.get("tick"), int)}
    findings: list[dict[str, Any]] = []
    event_counts: Counter[str] = Counter()
    event_ticks: defaultdict[str, list[int]] = defaultdict(list)
    event_reasons: defaultdict[str, Counter[str]] = defaultdict(Counter)
    positions: defaultdict[str, list[tuple[int, tuple[int, int]]]] = defaultdict(list)
    kinds: dict[str, str] = {}
    actions: defaultdict[str, Counter[str]] = defaultdict(Counter)
    reasons: defaultdict[str, Counter[str]] = defaultdict(Counter)
    failed_moves: Counter[str] = Counter()
    modes: list[tuple[int, str]] = []
    visible_resources: list[int] = []
    core_hp: list[tuple[int, int]] = []
    hidden_damage_ticks: list[int] = []
    disengaged: list[dict[str, Any]] = []
    worker_cargo_history: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    unit_combat_history: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    production_candidates: list[dict[str, Any]] = []
    beacon_isolation: list[dict[str, Any]] = []

    for row in replay:
        tick = row.get("tick")
        if not isinstance(tick, int):
            continue
        mode = str(row.get("mode", "UNKNOWN")).upper()
        if mode in MODES:
            modes.append((tick, mode))
        state = row.get("state") if isinstance(row.get("state"), dict) else {}
        visible_resources.append(len(state.get("resource_cells") or []))
        core = state.get("core") if isinstance(state.get("core"), dict) else None
        if core:
            cid, cpos = str(core.get("id", "core")), _pos(core.get("position"))
            kinds[cid] = "CORE"
            if cpos:
                positions[cid].append((tick, cpos))
            if isinstance(core.get("hp"), int):
                core_hp.append((tick, core["hp"]))
        units = [unit for unit in state.get("units") or [] if isinstance(unit, dict)]
        intents = [i for i in row.get("intents") or [] if isinstance(i, dict)]
        intents_by_actor = {str(i.get("actor") or i.get("actor_alias") or "unknown"): i for i in intents}
        combat_units = [unit for unit in units if str(unit.get("unit_type") or unit.get("kind")) in {"VANGUARD", "RANGER"}]
        enemy_positions = [enemy.get("position") for enemy in state.get("visible_enemies") or [] if isinstance(enemy, dict) and _pos(enemy.get("position"))]
        for unit in units:
            if not isinstance(unit, dict):
                continue
            uid, upos = str(unit.get("id", "unknown")), _pos(unit.get("position"))
            kind = str(unit.get("unit_type") or unit.get("kind") or "UNIT")
            kinds[uid] = kind
            if upos:
                positions[uid].append((tick, upos))
            intent_action = str(intents_by_actor.get(uid, {}).get("action") or "UNKNOWN").upper()
            if kind == "WORKER" and isinstance(unit.get("cargo"), (int, float)) and unit.get("cargo", 0) > 0 and core:
                worker_cargo_history[uid].append({"tick": tick, "cargo": unit["cargo"], "core_distance": _distance(unit.get("position"), core.get("position")), "action": intent_action})
            if isinstance(unit.get("hp"), int):
                nearest_enemy = min((_distance(unit.get("position"), pos) for pos in enemy_positions), default=None)
                unit_combat_history[uid].append({"tick": tick, "hp": unit["hp"], "action": intent_action, "nearest_enemy": nearest_enemy})
        tick_events = [e for e in row.get("events") or [] if isinstance(e, dict)]
        for event in tick_events:
            etype = _event_type(event)
            event_counts[etype] += 1
            event_ticks[etype].append(tick)
            if _event_reason(event):
                event_reasons[etype][_event_reason(event) or ""] += 1
            if etype == "UNIT_MOVE_FAILED":
                failed_moves[str(event.get("actor") or event.get("actor_id") or "unknown")] += 1
        for intent in intents:
            actor = str(intent.get("actor") or intent.get("actor_alias") or "unknown")
            actions[actor][str(intent.get("action") or "UNKNOWN")] += 1
            reasons[actor][str(intent.get("reason") or "unknown")] += 1
        resources, population = state.get("resources"), state.get("population")
        core_action = str(intents_by_actor.get(str(core.get("id")) if core else "", {}).get("action") or "UNKNOWN").upper()
        peaceful = mode == "ECONOMY" or (not enemy_positions and mode not in {"ATTACK", "DEFEND", "BEACON"})
        spawned = core_action == "SPAWN" or any(_event_type(event) == "CORE_SPAWN_SUCCEEDED" for event in tick_events)
        if core and isinstance(resources, (int, float)) and resources >= 10 and isinstance(population, int) and population < 20 and peaceful and not spawned:
            production_candidates.append({"tick": tick, "resources": resources, "population": population, "mode": mode, "core_action": core_action})
        beacon = state.get("beacon") if isinstance(state.get("beacon"), dict) else {}
        carrier_id = (beacon.get("carrier") or beacon.get("carrier_id")) if str(beacon.get("status", "")).upper() == "CARRIED" else None
        carrier = next((unit for unit in units if str(unit.get("id")) == str(carrier_id)), None)
        if carrier:
            escorts = [unit for unit in combat_units if str(unit.get("id")) != str(carrier_id)]
            nearest = min((_distance(carrier.get("position"), unit.get("position")) for unit in escorts), default=None)
            if nearest is None or nearest > BEACON_ESCORT_DISTANCE:
                beacon_isolation.append({"tick": tick, "carrier": str(carrier_id), "carrier_type": carrier.get("unit_type"), "nearest_combat_ally_distance": nearest, "combat_allies": len(escorts)})
        enemies = [e for e in state.get("visible_enemies") or [] if isinstance(e, dict)]
        damaged = any(_event_type(e) == "CORE_DAMAGED" for e in tick_events)
        if damaged and not enemies:
            held = [i for i in intents if i.get("reason") == "holding_defense_ring" and i.get("action") == "WAIT"]
            if held:
                hidden_damage_ticks.append(tick)
        if enemies and core:
            # Only combat-capable enemies (enemy VANGUARD/RANGER) count as a
            # real engagement threat.  An enemy CORE or WORKER scouting near
            # our base is not worth chasing — holding the defense ring is the
            # correct response, so those must not trigger this alarm.
            threat_cells = [
                e.get("position") for e in enemies
                if _pos(e.get("position")) and e.get("unit_type") in {"VANGUARD", "RANGER"}
                and e.get("kind") != "CORE"
            ]
            enemy_cells = [p for p in threat_cells if p]
            if not enemy_cells:
                continue
            combat = [u for u in state.get("units") or [] if isinstance(u, dict) and u.get("unit_type") in {"VANGUARD", "RANGER"}]
            far_waiting = []
            for unit in combat:
                nearest = min((_distance(unit.get("position"), p) for p in enemy_cells), default=None)
                intent = next((i for i in intents if str(i.get("actor")) == str(unit.get("id"))), {})
                if nearest is not None and nearest > 6 and intent.get("action") == "WAIT":
                    far_waiting.append({"id": unit.get("id"), "type": unit.get("unit_type"), "enemy_distance": nearest, "reason": intent.get("reason")})
            if far_waiting:
                disengaged.append({"tick": tick, "units": far_waiting})

    # Trace positions are useful if replay is absent, and expose Core decisions too.
    if not replay:
        for trace in traces:
            tick = trace.get("tick")
            if not isinstance(tick, int):
                continue
            for entity in trace.get("entity_traces") or []:
                if not isinstance(entity, dict):
                    continue
                actor, cell = str(entity.get("actor_alias", "unknown")), _pos(entity.get("current_cell"))
                kinds[actor] = str(entity.get("entity_kind") or "UNKNOWN")
                if cell:
                    positions[actor].append((tick, cell))
                actions[actor][str(entity.get("action") or "UNKNOWN")] += 1
                for reason in entity.get("reason_codes") or []:
                    reasons[actor][str(reason)] += 1

    oscillators, stuck = [], []
    for actor, samples in positions.items():
        samples = sorted(dict(samples).items())
        osc = _oscillation(samples)
        if osc:
            oscillators.append({"entity": actor, "kind": kinds.get(actor, "UNKNOWN"), **osc})
        if len(samples) >= 6:
            last = samples[-1][1]
            run = 1
            for _, cell in reversed(samples[:-1]):
                if cell != last:
                    break
                run += 1
            blocked_waits = sum(
                count for reason, count in reasons[actor].items()
                if any(token in reason.lower() for token in ("blocked", "stuck", "invalid", "failed", "deadlock"))
            )
            ineffective = failed_moves[actor] + blocked_waits
            if run >= 6 and ineffective >= 3:
                stuck.append({"entity": actor, "kind": kinds.get(actor, "UNKNOWN"), "cell": list(last), "stationary_ticks": run, "blocked_waits": blocked_waits, "failed_moves": failed_moves[actor]})
    if oscillators:
        findings.append(_finding("UNIT_OSCILLATION", "warning", f"{len(oscillators)} 个对象出现 2~4 格周期性往复", entities=(o["entity"] for o in oscillators), evidence=oscillators))
    if stuck:
        findings.append(_finding("INEFFECTIVE_STATIONARY", "warning", f"{len(stuck)} 个对象长期原地等待或移动失败", entities=(s["entity"] for s in stuck), evidence=stuck))

    # ── EXPLORATION_STALL: workers moving a lot but not making progress,
    #    or resource exploration completely dead.  Complements UNIT_OSCILLATION
    #    which targets tight 2-4 cell loops; this catches broader "running in
    #    circles" patterns where net displacement is near zero over ≥40 steps.
    exploration_stall: list[dict[str, Any]] = []
    for actor, samples in positions.items():
        if kinds.get(actor) != "WORKER":
            continue
        ordered = sorted(dict(samples).items())
        if len(ordered) < 2:
            continue
        steps = sum(
            1 for i in range(1, len(ordered)) if ordered[i][1] != ordered[i - 1][1]
        )
        if steps < 40:
            continue
        first_pos, last_pos = ordered[0][1], ordered[-1][1]
        net_disp = abs(first_pos[0] - last_pos[0]) + abs(first_pos[1] - last_pos[1])
        if net_disp <= steps * 0.1:
            # Find current target from latest intent
            current_target = None
            for intent in (replay[-1].get("intents") or []) if replay else []:
                if str(intent.get("actor") or "") == actor:
                    current_target = intent.get("target_position") or intent.get("target")
                    break
            exploration_stall.append({
                "entity": actor,
                "kind": "WORKER",
                "steps": steps,
                "net_displacement": net_disp,
                "first_pos": list(first_pos),
                "last_pos": list(last_pos),
                "current_target": current_target,
                "tick_range": [ordered[0][0], ordered[-1][0]],
            })
    no_resource_ticks = int(agent_state.get("no_resource_ticks") or 0)
    if no_resource_ticks >= 600:
        explored_growth = 0
        resource_counts = [v for v in visible_resources if v is not None]
        if resource_counts and max(resource_counts) > 0:
            explored_growth = max(resource_counts) - min(resource_counts)
        if explored_growth == 0:
            exploration_stall.append({
                "entity": "_global",
                "kind": "RESOURCE_EXPLORATION",
                "steps": 0,
                "net_displacement": 0,
                "no_resource_ticks": no_resource_ticks,
                "explored_growth": explored_growth,
                "current_target": None,
            })
    if exploration_stall:
        findings.append(_finding(
            "EXPLORATION_STALL",
            "warning",
            f"{sum(1 for e in exploration_stall if e['kind'] == 'WORKER')} 名工人大幅移动但净位移≈0"
            + (f"；资源探索停滞 {no_resource_ticks} ticks" if no_resource_ticks >= 600 else ""),
            entities=(e["entity"] for e in exploration_stall),
            evidence=exploration_stall,
        ))

    cargo_stagnation = []
    successful_deposits = {(_event_actor(event), row.get("tick")) for row in replay for event in row.get("events", []) if isinstance(event, dict) and _event_type(event) == "DEPOSIT_SUCCEEDED"}
    for worker, samples in worker_cargo_history.items():
        for run in _runs(samples, CARGO_STAGNATION_TICKS):
            # Resolution events are published with the following authoritative
            # Turn, so include the Tick immediately after the carrying run.
            if any((worker, tick) in successful_deposits for tick in range(run[0]["tick"], run[-1]["tick"] + 2)):
                continue
            distances = [sample["core_distance"] for sample in run if sample["core_distance"] is not None]
            progress = distances[0] - distances[-1] if len(distances) >= 2 else None
            if distances and ((progress is not None and progress < 2) or distances[-1] <= 1):
                cargo_stagnation.append({"worker": worker, "tick_range": [run[0]["tick"], run[-1]["tick"]], "ticks": len(run), "cargo": run[-1]["cargo"], "distance_start": distances[0], "distance_end": distances[-1], "distance_progress": progress, "near_core": distances[-1] <= 1})
                break
    if cargo_stagnation:
        findings.append(_finding("CARGO_DELIVERY_STAGNATION", "critical", f"{len(cargo_stagnation)} 名载货工人连续无法完成回矿", ticks=(item["tick_range"][-1] for item in cargo_stagnation), entities=(item["worker"] for item in cargo_stagnation), evidence=cargo_stagnation))

    production_freezes = _runs(production_candidates, PRODUCTION_FREEZE_TICKS)
    if production_freezes:
        evidence = [{"tick_range": [run[0]["tick"], run[-1]["tick"]], "ticks": len(run), "resource_range": [min(s["resources"] for s in run), max(s["resources"] for s in run)], "population_range": [min(s["population"] for s in run), max(s["population"] for s in run)]} for run in production_freezes]
        findings.append(_finding("PRODUCTION_FREEZE", "warning", "和平/经济模式下资源与人口空间充足，但连续未生产", ticks=(run[-1]["tick"] for run in production_freezes), evidence=evidence))

    unanswered_damage = []
    for unit, samples in unit_combat_history.items():
        damage_samples = [{**current, "previous_hp": previous["hp"]} for previous, current in zip(samples, samples[1:]) if current["tick"] == previous["tick"] + 1 and current["hp"] < previous["hp"]]
        for run in _runs(damage_samples, UNANSWERED_DAMAGE_TICKS):
            attacked = any(sample["action"] in {"ATTACK", "SHOOT"} for sample in run)
            escaped = run[-1]["nearest_enemy"] is not None and run[-1]["nearest_enemy"] > 3
            if not attacked and not escaped:
                unanswered_damage.append({"unit": unit, "kind": kinds.get(unit), "tick_range": [run[0]["tick"], run[-1]["tick"]], "hp_start": run[0]["previous_hp"], "hp_end": run[-1]["hp"], "nearest_enemy_end": run[-1]["nearest_enemy"], "enemy_visibility": "visible" if run[-1]["nearest_enemy"] is not None else "unknown_or_blind_spot"})
                break
    if unanswered_damage:
        findings.append(_finding("UNANSWERED_DAMAGE", "critical", f"{len(unanswered_damage)} 个单位连续受损且未反击或确认脱离射程", ticks=(item["tick_range"][-1] for item in unanswered_damage), entities=(item["unit"] for item in unanswered_damage), evidence=unanswered_damage))

    if beacon_isolation:
        findings.append(_finding("BEACON_CARRIER_ISOLATED", "critical", "信标持有者距最近战斗友军超过 4 格或无护卫", ticks=(item["tick"] for item in beacon_isolation), entities=(item["carrier"] for item in beacon_isolation), evidence=beacon_isolation[-12:]))

    latency_spikes = [{"tick": trace.get("tick"), "decision_ms": trace.get("timings", {}).get("decision_ms")} for trace in traces if isinstance(trace.get("timings"), dict) and isinstance(trace["timings"].get("decision_ms"), (int, float)) and trace["timings"]["decision_ms"] > DECISION_SPIKE_MS]
    if latency_spikes:
        findings.append(_finding("DECISION_LATENCY_SPIKE", "critical", f"决策耗时出现 {len(latency_spikes)} 次超过 {DECISION_SPIKE_MS:.0f}ms 的毛刺", ticks=(item["tick"] for item in latency_spikes if isinstance(item["tick"], int)), evidence={"threshold_ms": DECISION_SPIKE_MS, "spikes": latency_spikes[-12:]}))

    deposit_failed = event_counts["DEPOSIT_FAILED"]
    deposit_success = event_counts["DEPOSIT_SUCCEEDED"]
    carrying_now = 0
    workers: list[dict[str, Any]] = []
    latest = replay[-1].get("state", {}) if replay else {}
    latest_tick = replay[-1].get("tick") if replay else None
    latest_trace = traces_by_tick.get(latest_tick, {})
    trace_entities = {str(e.get("actor_alias")): e for e in latest_trace.get("entity_traces") or [] if isinstance(e, dict)}
    for unit in latest.get("units") or []:
        if not isinstance(unit, dict) or unit.get("unit_type") != "WORKER":
            continue
        uid, cargo = str(unit.get("id")), int(unit.get("cargo") or 0)
        intent = next((i for i in replay[-1].get("intents", []) if str(i.get("actor")) == uid), {}) if replay else {}
        trace = trace_entities.get("entity_" + uid, trace_entities.get(uid, {}))
        action, reason = intent.get("action") or trace.get("action"), intent.get("reason") or next(iter(trace.get("reason_codes") or []), None)
        if cargo > 0:
            status = "carrying"
            carrying_now += 1
        elif action == "HARVEST": status = "harvesting"
        elif "migration" in str(reason).lower() or reason in {"CORE_MOVING", "waiting_core_migration"}: status = "waiting_core_migration"
        elif uid in {s["entity"] for s in stuck}: status = "stuck"
        elif action == "WAIT": status = "idle"
        else: status = "moving_or_exploring"
        workers.append({"id": uid, "status": status, "cargo": cargo, "action": action, "reason": reason})
    status_counts = Counter(w["status"] for w in workers)
    if deposit_failed:
        failure_ratio = deposit_failed / max(1, deposit_failed + deposit_success)
        severity = "critical" if failure_ratio >= .5 and deposit_failed >= 3 else "warning"
        findings.append(_finding("DEPOSIT_FAILURES", severity, f"窗口内存款失败 {deposit_failed} 次（失败率 {failure_ratio:.0%}）", ticks=event_ticks["DEPOSIT_FAILED"], evidence={"reasons": dict(event_reasons["DEPOSIT_FAILED"]), "successes": deposit_success}))
    # Core attack / destruction findings (CRITICAL)
    last_respawn_tick = max(event_ticks["CORE_RESPAWNED"]) if event_ticks["CORE_RESPAWNED"] else None
    latest_window_tick = replay[-1].get("tick", 0) if replay else 0
    # Damage that happened before the most recent respawn belongs to the previous life cycle; ignore it.
    live_damage_ticks = [t for t in event_ticks["CORE_DAMAGED"] if last_respawn_tick is None or t > last_respawn_tick]
    if live_damage_ticks:
        findings.append(_finding("CORE_UNDER_ATTACK", "critical", f"核心在当前生命周期内遭受直接攻击 {len(live_damage_ticks)} 次", ticks=live_damage_ticks, evidence={"count": len(live_damage_ticks), "ticks": live_damage_ticks[-12:]}))
    
    # Only alert on destruction/respawn if it occurred recently (within last 12 ticks), preventing repeated stale alarms across the whole sliding window.
    recent_respawns = [t for t in event_ticks["CORE_RESPAWNED"] if latest_window_tick - t <= 12]
    recent_destructions = [t for t in event_ticks["CORE_DESTROYED"] if latest_window_tick - t <= 12]
    if recent_destructions or recent_respawns:
        findings.append(_finding("CORE_LOST_OR_RESPAWNED", "critical", f"核心发生战损或重生（摧毁={len(recent_destructions)}, 重生={len(recent_respawns)}）", ticks=recent_destructions + recent_respawns, evidence={"destroyed": len(recent_destructions), "respawned": len(recent_respawns)}))

    no_resource_ticks = int(agent_state.get("no_resource_ticks") or 0)
    empty_ratio = sum(v == 0 for v in visible_resources) / max(1, len(visible_resources))

    if hidden_damage_ticks:
        findings.append(_finding("HIDDEN_CORE_ATTACK", "critical", "核心在无可见敌人时受击，且环防单位仍 WAIT", ticks=hidden_damage_ticks, evidence={"occurrences": len(hidden_damage_ticks), "interpretation": "可能为视野外 Ranger 火力或防线朝向错误"}))
    if disengaged:
        findings.append(_finding("DEFENSE_DISENGAGED", "critical", "可见威胁出现时有战斗单位远离敌人并等待", ticks=(d["tick"] for d in disengaged), evidence=disengaged[-8:]))

    switches = []
    for (tick0, old), (tick1, new) in zip(modes, modes[1:]):
        if old != new:
            switches.append({"tick": tick1, "from": old, "to": new, "duration": tick1 - tick0})

    migration_events = sum(event_counts[e] for e in ("CORE_MOVE_STARTED", "CORE_MOVE_PROGRESS", "CORE_MOVE_SUCCEEDED", "CORE_MOVE_FAILED", "CORE_MOVE_CANCELLED", "CORE_MOVE_START_FAILED"))
    migration_failures = event_counts["CORE_MOVE_FAILED"] + event_counts["CORE_MOVE_START_FAILED"] + event_counts["CORE_MOVE_CANCELLED"]
    migration_state = (latest.get("core") or {}).get("state") if isinstance(latest.get("core"), dict) else None
    if migration_failures >= 3 or (event_counts["CORE_MOVE_STARTED"] >= 3 and not event_counts["CORE_MOVE_SUCCEEDED"]):
        findings.append(_finding("CORE_MIGRATION_LOOP", "critical", "核心迁移多次失败/取消或反复启动而未成功", evidence={"started": event_counts["CORE_MOVE_STARTED"], "succeeded": event_counts["CORE_MOVE_SUCCEEDED"], "failed": event_counts["CORE_MOVE_FAILED"], "start_failed": event_counts["CORE_MOVE_START_FAILED"], "cancelled": event_counts["CORE_MOVE_CANCELLED"]}))

    lifecycle_types = ["CORE_DAMAGED", "CORE_DESTROYED", "CORE_RESPAWNED", "UNIT_DAMAGED", "UNIT_DESTROYED"]
    lifecycle = {name: {"count": event_counts[name], "ticks": event_ticks[name][-12:]} for name in lifecycle_types}
    lifecycle["UNIT_DESTROYED_INFERRED"] = {"count": sum(1 for r in replay for e in r.get("events", []) if isinstance(e, dict) and _event_type(e) == "UNIT_DAMAGED" and isinstance(e.get("values"), dict) and e["values"].get("hp") == 0), "note": "协议无独立 UNIT_DESTROYED；由 UNIT_DAMAGED hp=0 推断"}

    findings.sort(key=lambda f: {"critical": 0, "warning": 1, "info": 2}.get(f["severity"], 3))
    report = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window": {"requested_ticks": window, "replay_records": len(replay), "trace_records": len(traces), "tick_start": replay[0].get("tick") if replay else None, "tick_end": replay[-1].get("tick") if replay else None},
        "service": _health(health_url, health_timeout),
        "economy": {"core_resources": latest.get("resources"), "capacity": latest.get("resource_capacity"), "population": latest.get("population"), "workers": workers, "worker_status_counts": dict(status_counts), "carrying_workers": carrying_now, "deposit": {"succeeded": deposit_success, "failed": deposit_failed, "failure_reasons": dict(event_reasons["DEPOSIT_FAILED"])}, "resources": {"no_resource_ticks": no_resource_ticks, "latest_visible_cells": visible_resources[-1] if visible_resources else None, "empty_visibility_ratio": round(empty_ratio, 3)}},
        "movement": {"oscillating_entities": oscillators, "stationary_ineffective_entities": stuck, "move_failures": event_counts["UNIT_MOVE_FAILED"], "move_failure_reasons": dict(event_reasons["UNIT_MOVE_FAILED"])},
        "battlefield": {"core": latest.get("core"), "visible_enemy_count": len(latest.get("visible_enemies") or []), "recent_lifecycle_events": lifecycle, "hidden_attack_ticks": hidden_damage_ticks, "defense_disengagement": disengaged[-8:]},
        "strategy": {"current_mode": modes[-1][1] if modes else agent_state.get("last_mode"), "mode_history": [{"tick": t, "mode": m} for t, m in modes[-20:]], "switch_count": len(switches), "switches": switches[-20:], "migration": {"current_state": migration_state, "destination": (latest.get("core") or {}).get("destination") if isinstance(latest.get("core"), dict) else None, "events": migration_events, "failures_or_cancels": migration_failures, "cooldown_until_tick": agent_state.get("migration_cooldown_until_tick")}},
        "findings": findings,
        "tactical_clues": [f["summary"] for f in findings[:8]] or ["窗口内未发现达到阈值的战术异常；仍应结合更长时间窗复核。"],
        "data_quality": {"replay": replay_quality, "decision_trace": trace_quality, "agent_state_error": state_error, "notes": ["只分析当前可见敌人；无敌不等于无威胁。", "轮转文件按 Tick 去重，单文件读取受 --max-bytes 限制。"]},
    }
    return report


def render_text(report: dict[str, Any]) -> str:
    w, e, b, s = report["window"], report["economy"], report["battlefield"], report["strategy"]
    health = report["service"]
    lines = [
        "Arena Hero 深度战术态势摘要",
        f"时间窗: Tick {w['tick_start']}..{w['tick_end']}（replay={w['replay_records']}, trace={w['trace_records']}） | 服务: {'UP' if health['reachable'] else 'DOWN'}",
        "",
        "[经济与资源]",
        f"核心资源 {e['core_resources']}/{e['capacity']}，人口 {e['population']}；工人状态 {json.dumps(e['worker_status_counts'], ensure_ascii=False)}",
        f"存款成功/失败 {e['deposit']['succeeded']}/{e['deposit']['failed']}；no_resource_ticks={e['resources']['no_resource_ticks']}；当前可见资源格={e['resources']['latest_visible_cells']}",
        "",
        "[战场与战略]",
        f"当前模式 {s['current_mode']}，窗口切换 {s['switch_count']} 次；核心迁移={s['migration']['current_state']}，失败/取消={s['migration']['failures_or_cancels']}",
        f"当前可见敌人 {b['visible_enemy_count']}；核心受击 {b['recent_lifecycle_events']['CORE_DAMAGED']['count']} 次；核心重生 {b['recent_lifecycle_events']['CORE_RESPAWNED']['count']} 次；隐蔽受击 {len(b['hidden_attack_ticks'])} 次",
        "",
        f"[异常发现：{len(report['findings'])} 项]",
    ]
    for i, finding in enumerate(report["findings"], 1):
        ticks = f" ticks={finding.get('ticks')}" if finding.get("ticks") else ""
        lines.append(f"{i}. [{finding['severity'].upper()}] {finding['code']}: {finding['summary']}{ticks}")
    if not report["findings"]:
        lines.append("未发现达到阈值的异常。")
    lines.extend(("", "[战术研判线索]"))
    lines.extend(f"- {clue}" for clue in report["tactical_clues"])
    return "\n".join(lines)


def render_alert_text(report: dict[str, Any]) -> str:
    w, e, b, s = report["window"], report["economy"], report["battlefield"], report["strategy"]
    health = report["service"]
    findings = report.get("findings", [])
    
    status_icon = "🚨" if any(f.get("severity") == "critical" for f in findings) else "⚠️"
    lines = [
        f"{status_icon} 【Arena Hero 战术异常告警】",
        f"时间窗: Tick {w['tick_start']}..{w['tick_end']} | 服务: {'UP' if health['reachable'] else 'DOWN (无法连通)'}",
        f"态势: 模式={s['current_mode']} | 核心资源={e['core_resources']}/{e['capacity']} | 可见敌军={b['visible_enemy_count']}",
        "",
        f"[检测到 {len(findings)} 项战术异常]:",
    ]
    for i, finding in enumerate(findings, 1):
        ticks = f" (Ticks: {finding.get('ticks')})" if finding.get("ticks") else ""
        lines.append(f"{i}. [{finding['severity'].upper()}] {finding['code']}: {finding['summary']}{ticks}")
    
    if report.get("tactical_clues"):
        lines.extend(("", "[研判建议与破局线索]:"))
        lines.extend(f"- {clue}" for clue in report["tactical_clues"])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Extract recent Arena Hero tactical state and anomalies")
    parser.add_argument("--runtime", type=Path, default=DEFAULT_RUNTIME, help="runtime directory")
    parser.add_argument("--ticks", type=int, default=100, help="recent Tick records (50-100 recommended)")
    parser.add_argument("--max-bytes", type=int, default=16 * 1024 * 1024, help="maximum bytes read from each JSONL file")
    parser.add_argument("--health-url", default=LIVEZ_URL)
    parser.add_argument("--health-timeout", type=float, default=0.75)
    parser.add_argument("--json", action="store_true", help="emit pure JSON")
    parser.add_argument("--record-nightly", action="store_true", help="record snapshot to nightly reports log during quiet hours (23:00-08:00)")
    parser.add_argument("--alert-only", action="store_true", help="only emit output when warnings/critical findings are detected (silent on healthy/normal)")
    parser.add_argument("--min-severity", choices=["info", "warning", "critical"], default="warning", help="minimum severity to trigger alert")
    args = parser.parse_args(argv)
    if not 1 <= args.ticks <= 10_000 or args.max_bytes < 4096 or args.health_timeout <= 0:
        parser.error("invalid --ticks, --max-bytes, or --health-timeout")
    report = inspect(args.runtime, args.ticks, args.max_bytes, args.health_url, args.health_timeout)

    # Filter findings by severity if required
    severity_rank = {"info": 1, "warning": 2, "critical": 3}
    min_rank = severity_rank.get(args.min_severity, 2)
    active_findings = [f for f in report.get("findings", []) if severity_rank.get(f.get("severity", "info"), 1) >= min_rank]
    service_down = not report.get("service", {}).get("reachable", True)
    has_alert = bool(active_findings) or service_down

    # If --record-nightly requested, record during night hours
    current_hour = datetime.now().hour
    is_quiet_hours = current_hour >= 23 or current_hour < 8
    report["is_quiet_hours"] = is_quiet_hours

    if args.record_nightly and is_quiet_hours:
        try:
            nightly_log = args.runtime / "nightly_tactical_reports.jsonl"
            nightly_log.parent.mkdir(parents=True, exist_ok=True)
            with nightly_log.open("a", encoding="utf-8") as f:
                f.write(json.dumps(report, ensure_ascii=False) + "\n")
        except Exception as e:
            sys.stderr.write(f"Warning: failed to record nightly log: {e}\n")

    if args.alert_only:
        # In quiet hours, stay silent unless it's a catastrophic service failure
        if is_quiet_hours and not service_down:
            return 0
        # If no alerts found, exit silently with empty stdout (0 token watchdog mode)
        if not has_alert:
            return 0
        if args.json:
            json.dump(report, sys.stdout, ensure_ascii=False, separators=(",", ":"))
            sys.stdout.write("\n")
        else:
            print(render_alert_text(report))
        return 0

    if args.json:
        json.dump(report, sys.stdout, ensure_ascii=False, separators=(",", ":"))
        sys.stdout.write("\n")
    else:
        text = render_text(report)
        if is_quiet_hours:
            text += f"\n\n[夜间勿扰时段生效中 (23:00-08:00)，当前快照已自动归档至夜间日志]"
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
