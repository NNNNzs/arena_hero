"""Bounded, redacted dashboard projection for the Arena Hero service."""

from __future__ import annotations

import json
import mimetypes
import os
import re
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Any, Callable

from .context import _visible_cells
from .event_log import EventLogCollector


MODE_LABELS = {
    "RESPAWN": "等待重生",
    "RECOVER": "恢复",
    "DEFEND": "防守",
    "ECONOMY": "发展经济",
    "EXPLORE": "探索",
    "BEACON": "争夺信标",
    "ATTACK": "进攻",
}
ACTION_LABELS = {
    "WAIT": "等待",
    "MOVE": "移动",
    "HARVEST": "采集",
    "DEPOSIT": "存储",
    "SWEEP": "横扫",
    "SHOOT": "射击",
    "HEAL": "治疗",
    "SPAWN": "生产",
    "REPAIR_SHIELD": "修复护盾",
    "START_MOVE": "核心迁移",
    "PICKUP_BEACON": "拾取信标",
    "SELF_DESTRUCT": "自毁重掷",
}
VISION_RADIUS = {"CORE": 5, "WORKER": 3, "VANGUARD": 4, "RANGER": 5}


def _replay_markers(record: dict[str, Any]) -> list[dict[str, str]]:
    """Return small, presentation-safe event markers for one replay frame."""
    markers: list[dict[str, str]] = []
    seen: set[str] = set()

    def add(kind: str, label: str) -> None:
        if kind not in seen:
            markers.append({"kind": kind, "label": label})
            seen.add(kind)

    for event in record.get("events", ()):
        if not isinstance(event, dict):
            continue
        event_type = str(event.get("type") or "").upper()
        if "DAMAGE" in event_type or "DESTROY" in event_type or "SHOT" in event_type:
            add("DAMAGE", "受损/交火")
        if "SPAWN" in event_type:
            add("SPAWN", "生产")
        if "MOVE" in event_type or "MIGRAT" in event_type:
            add("MOVE", "迁移/移动")
    for action in record.get("actions", ()):
        if not isinstance(action, dict):
            continue
        action_type = str(action.get("type") or "").upper()
        if action_type == "SPAWN":
            add("SPAWN", "生产")
        elif action_type in {"MOVE", "START_MOVE"}:
            add("MOVE", "迁移/移动")
        elif action_type == "SELF_DESTRUCT":
            add("REROLL", "贫瘠出生点重掷")
    return markers


def _bounded_jsonl_tail(path: Path, *, max_bytes: int, limit: int) -> list[dict[str, Any]]:
    """Read only a bounded file tail and ignore partial or malformed lines."""
    if max_bytes <= 0 or limit <= 0:
        return []
    try:
        with path.open("rb") as stream:
            stream.seek(0, 2)
            size = stream.tell()
            start = max(0, size - max_bytes)
            stream.seek(start)
            raw = stream.read(max_bytes)
    except (FileNotFoundError, OSError):
        return []
    lines = raw.splitlines()
    if start and lines:
        lines = lines[1:]
    records: list[dict[str, Any]] = []
    for line in lines:
        try:
            value = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if isinstance(value, dict):
            records.append(value)
    return records[-limit:]


def _integer(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _safe_text(value: Any, *, maximum: int = 80) -> str | None:
    if not isinstance(value, str):
        return None
    return value[:maximum]


def redact_error(value: Any) -> str | None:
    text = _safe_text(value, maximum=240)
    if not text:
        return None
    patterns = (
        r"(?i)(api[-_ ]?key\s*[:=]\s*)[^,;\r\n]+",
        r"(?i)(authorization\s*[:=]\s*)[^,;\r\n]+",
        r"(?i)(cookie\s*[:=]\s*)[^,;\r\n]+",
        r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+",
    )
    for pattern in patterns:
        text = re.sub(pattern, lambda match: match.group(1) + "[已脱敏]" if match.lastindex else "[已脱敏]", text)
    text = re.sub(
        r"(?i)\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b",
        "[对象标识已脱敏]",
        text,
    )
    return text


def _project_record(record: dict[str, Any]) -> dict[str, Any]:
    """Allowlist fields displayed by the browser; never pass records through."""
    state = record.get("state") if isinstance(record.get("state"), dict) else {}
    raw_intents = record.get("intents") if isinstance(record.get("intents"), list) else []
    raw_events = record.get("events") if isinstance(record.get("events"), list) else []
    actions = Counter(
        action
        for item in raw_intents
        if isinstance(item, dict)
        and (action := _safe_text(item.get("action"), maximum=32))
    )
    events = []
    for item in raw_events[-8:]:
        if not isinstance(item, dict):
            continue
        event_type = _safe_text(item.get("type"), maximum=48)
        if not event_type:
            continue
        events.append({
            "type": event_type,
            "reason": _safe_text(item.get("reason"), maximum=64),
        })
    mode = _safe_text(record.get("mode"), maximum=32)
    def _map_object(value: Any, *, enemy: bool = False) -> dict[str, Any] | None:
        if not isinstance(value, dict):
            return None
        position = value.get("position")
        identity = _safe_text(value.get("id"), maximum=24)
        kind = _safe_text(value.get("unit_type") or value.get("kind"), maximum=24)
        if not isinstance(position, list) or len(position) != 2 or not all(type(axis) is int for axis in position) or not identity or not kind:
            return None
        return {"alias": identity, "kind": kind, "position": position,
                "hp": _integer(value.get("hp")), "shield": _integer(value.get("shield")),
                "cargo": _integer(value.get("cargo")), "state": _safe_text(value.get("state"), maximum=24),
                "vision_radius": _integer(value.get("vision_radius")) or VISION_RADIUS.get(kind),
                "destination": list(value.get("destination")) if isinstance(value.get("destination"), list)
                and len(value["destination"]) == 2 and all(type(axis) is int for axis in value["destination"]) else None,
                "enemy": enemy}
    beacon = state.get("beacon") if isinstance(state.get("beacon"), dict) else {}
    raw_units = state.get("units") if isinstance(state.get("units"), list) else []
    raw_enemies = state.get("visible_enemies") if isinstance(state.get("visible_enemies"), list) else []
    raw_resources = state.get("resource_cells") if isinstance(state.get("resource_cells"), list) else []
    raw_obstacles = state.get("obstacle_cells") if isinstance(state.get("obstacle_cells"), list) else []
    raw_observed = state.get("observed_cells") if isinstance(state.get("observed_cells"), list) else []
    def _cell(value: Any) -> list[int] | None:
        return list(value) if isinstance(value, list) and len(value) == 2 and all(type(axis) is int for axis in value) else None
    observed = [item for cell in raw_observed[:2000] if (item := _cell(cell))]
    if not observed:
        origins = []
        for value in [state.get("core")] + raw_units[:100]:
            if not isinstance(value, dict):
                continue
            position = value.get("position")
            kind = str(value.get("unit_type") or value.get("kind") or "")
            radius = _integer(value.get("vision_radius")) or VISION_RADIUS.get(kind)
            if isinstance(position, list) and len(position) == 2 and all(type(axis) is int for axis in position) and radius is not None:
                origins.append((tuple(position), radius))
        obstacles = {tuple(item) for cell in raw_obstacles[:200] if (item := _cell(cell))}
        observed = [list(cell) for cell in sorted(_visible_cells(tuple(origins), obstacles))[:2000]]
    return {
        "tick": _integer(record.get("tick")),
        "mode": mode,
        "mode_label": MODE_LABELS.get(mode, mode or "未知"),
        "resources": _integer(state.get("resources")),
        "resource_capacity": _integer(state.get("resource_capacity")),
        "population": _integer(state.get("population")),
        "accepted": bool(record.get("accepted")),
        "decision_ms": _number(record.get("decision_ms")),
        "timed_out": bool(record.get("timed_out")),
        "actions": [
            {"type": name, "label": ACTION_LABELS.get(name, name), "count": count}
            for name, count in actions.most_common()
        ],
        "events": events,
        "map": {
            "friendly": [item for value in ([state.get("core")] + raw_units[:100]) if (item := _map_object(value))],
            "enemies": [item for value in raw_enemies[:100] if (item := _map_object(value, enemy=True))],
            "resources": [item for cell in raw_resources[:200] if (item := _cell(cell))],
            "observed": observed,
            "beacon": {"position": list(beacon.get("position")) if isinstance(beacon.get("position"), list) and len(beacon["position"]) == 2 else None,
                       "status": _safe_text(beacon.get("status"), maximum=24)},
        },
    }


def _project_trace_record(record: dict[str, Any]) -> dict[str, Any] | None:
    """Expose a bounded allowlist from the decision trace, never raw state."""
    if record.get("record_type") not in {None, "decision_trace"}:
        return None
    tick = _integer(record.get("tick"))
    if tick is None:
        return None
    entities = []
    for item in record.get("entity_traces", ())[:200]:
        if not isinstance(item, dict):
            continue
        alias = _safe_text(item.get("actor_alias"), maximum=32)
        if not alias or not re.fullmatch(r"entity_[0-9a-f]{12}", alias):
            continue
        raw_assignment = item.get("assignment") if isinstance(item.get("assignment"), dict) else {}
        entities.append({
            "alias": alias, "kind": _safe_text(item.get("entity_kind"), maximum=24),
            "task": _safe_text(item.get("current_task"), maximum=48),
            "goal": _safe_text(item.get("goal"), maximum=64),
            "action": _safe_text(item.get("action"), maximum=24),
            "status": _safe_text(item.get("status"), maximum=24),
            "task_status": _safe_text(item.get("task_status"), maximum=24),
            "assignment_status": _safe_text(item.get("assignment_status"), maximum=24),
            "current_cell": list(item.get("current_cell")) if isinstance(item.get("current_cell"), (list, tuple)) and len(item["current_cell"]) == 2 and all(type(axis) is int for axis in item["current_cell"]) else None,
            "target_cell": list(item.get("target_cell")) if isinstance(item.get("target_cell"), (list, tuple)) and len(item["target_cell"]) == 2 and all(type(axis) is int for axis in item["target_cell"]) else None,
            "next_step": _safe_text(item.get("next_step"), maximum=80),
            "wait_kind": _safe_text(item.get("wait_kind"), maximum=32),
            "wake_condition": _safe_text(item.get("wake_condition"), maximum=100),
            "eta_ticks": _integer(item.get("eta_ticks")),
            "reason": _safe_text((item.get("reason_codes") or [None])[0], maximum=80),
            "blocker": _safe_text(item.get("blocker"), maximum=80),
            "waited_ticks": _integer(item.get("waited_ticks")) or 0,
            "node_path": [
                {"node_id": _safe_text(node.get("node_id"), maximum=48),
                 "status": _safe_text(node.get("status"), maximum=24),
                 "reason": _safe_text(node.get("reason"), maximum=80)}
                for node in item.get("node_path", ())[:12] if isinstance(node, dict)
            ],
            "candidate_intents": [
                {key: value for key, value in {
                    "action": _safe_text(candidate.get("action"), maximum=24),
                    "direction": _safe_text(candidate.get("direction"), maximum=16),
                    "target_cell": list(candidate.get("target_cell")) if isinstance(candidate.get("target_cell"), (list, tuple)) and len(candidate["target_cell"]) == 2 and all(type(axis) is int for axis in candidate["target_cell"]) else None,
                    "score": _number(candidate.get("score")),
                    "reason": _safe_text(candidate.get("reason"), maximum=80),
                }.items() if value is not None}
                for candidate in item.get("candidate_intents", ())[:8] if isinstance(candidate, dict)
            ],
            "assignment": {
                "task_id": _safe_text(raw_assignment.get("task_id"), maximum=96),
                "goal": _safe_text(raw_assignment.get("goal"), maximum=64),
                "role": _safe_text(raw_assignment.get("role"), maximum=24),
                "lock": _safe_text(raw_assignment.get("lock"), maximum=96),
                "assigned_tick": _integer(raw_assignment.get("assigned_tick")),
                "lease_until_tick": _integer(raw_assignment.get("lease_until_tick")),
            } if raw_assignment else None,
        })
    def _summary(value: Any) -> dict[str, Any] | None:
        if not isinstance(value, dict):
            return None
        goal = _safe_text(value.get("goal"), maximum=48)
        status = _safe_text(value.get("status"), maximum=24)
        return {"goal": goal, "status": status, "stage": _safe_text(value.get("stage"), maximum=32)} if goal else None
    raw_causality = record.get("causality") if isinstance(record.get("causality"), dict) else {}
    raw_mode_causality = raw_causality.get("mode") if isinstance(raw_causality.get("mode"), dict) else {}
    mode_causality = {
        "mode": _safe_text(raw_mode_causality.get("mode"), maximum=24),
        "previous_mode": _safe_text(raw_mode_causality.get("previous_mode"), maximum=24),
        "changed": bool(raw_mode_causality.get("changed")),
        "duration_ticks": _integer(raw_mode_causality.get("duration_ticks")),
        "rule_id": _safe_text(raw_mode_causality.get("rule_id"), maximum=48),
        "summary": _safe_text(raw_mode_causality.get("summary"), maximum=120),
        "exit_condition": _safe_text(raw_mode_causality.get("exit_condition"), maximum=80),
        "source_cell": list(raw_mode_causality.get("source_cell")) if isinstance(raw_mode_causality.get("source_cell"), (list, tuple)) and len(raw_mode_causality["source_cell"]) == 2 and all(type(axis) is int for axis in raw_mode_causality["source_cell"]) else None,
        "preconditions": {key: value for key, value in raw_mode_causality.get("preconditions", {}).items() if isinstance(key, str) and isinstance(value, (str, int, bool, type(None)))} if isinstance(raw_mode_causality.get("preconditions"), dict) else {},
    }
    return {
        "tick": tick,
        "planner_version": _safe_text(record.get("planner_version"), maximum=80),
        "entities": entities,
        "goals": [item for value in record.get("goal_summaries", ())[:32] if (item := _summary(value))],
        "commands": [
            {"command_id": _safe_text(value.get("command_id"), maximum=48),
             "type": _safe_text(value.get("type"), maximum=32),
             "status": _safe_text(value.get("status"), maximum=32)}
            for value in record.get("command_results", ())[:32] if isinstance(value, dict)
        ],
        "tasks": [
            {"task_id": _safe_text(value.get("task_id"), maximum=96),
             "goal": _safe_text(value.get("goal"), maximum=64),
             "kind": _safe_text(value.get("kind"), maximum=48),
             "status": _safe_text(value.get("status"), maximum=24),
             "actor_alias": _safe_text(value.get("actor_alias"), maximum=32),
             "role": _safe_text(value.get("role"), maximum=24),
             "lock": _safe_text(value.get("lock"), maximum=96),
             "assigned_tick": _integer(value.get("assigned_tick")),
             "lease_until_tick": _integer(value.get("lease_until_tick")),
             "target": list(value.get("target")) if isinstance(value.get("target"), (list, tuple))
                       and len(value["target"]) == 2 and all(type(axis) is int for axis in value["target"]) else None,
             "waited_ticks": _integer(value.get("waited_ticks")) or 0,
             "reason": _safe_text(value.get("reason"), maximum=64)}
            for value in record.get("task_transitions", ())[:100] if isinstance(value, dict)
        ],
        "causality": {"mode": mode_causality} if mode_causality["mode"] else {},
    }


def _merge_entity_state(command_center: dict[str, Any] | None, current: dict[str, Any] | None) -> dict[str, Any] | None:
    """Join trace decisions to the same-Tick redacted replay snapshot."""
    if command_center is None:
        return None
    state_tick = current.get("tick") if isinstance(current, dict) else None
    synced = state_tick == command_center.get("tick")
    state_by_alias = {
        (item.get("alias") if str(item.get("alias", "")).startswith("entity_") else f"entity_{item.get('alias')}"): item
        for item in (current or {}).get("map", {}).get("friendly", ())
        if isinstance(item, dict) and item.get("alias")
    }
    entities = []
    for entity in command_center.get("entities", ()):
        if not isinstance(entity, dict):
            continue
        state = state_by_alias.get(entity.get("alias")) if synced else None
        entities.append({**entity, "trace_tick": command_center.get("tick"), "state_synced": synced,
                         "state_sync_label": "已同步" if synced else "等待下一份权威状态",
                         "position": state.get("position") if state else None,
                         "hp": state.get("hp") if state else None,
                         "shield": state.get("shield") if state else None,
                         "cargo": state.get("cargo") if state else None,
                         "object_state": state.get("state") if state else None,
                         "destination": state.get("destination") if state else None})
    return {**command_center, "state_tick": state_tick, "state_synced": synced, "entities": entities}


class DashboardDataStore:
    """Small TTL cache around bounded replay-tail reads."""

    def __init__(
        self,
        replay_path: Path,
        *,
        trace_path: Path | None = None,
        memory_path: Path | None = None,
        max_bytes: int = 256 * 1024,
        recent_limit: int = 32,
        cache_seconds: float = 1.0,
        supabase: Any | None = None,
        writer: Any | None = None,
    ) -> None:
        self.replay_path = replay_path
        self.trace_path = trace_path or replay_path.with_name("decision-trace.jsonl")
        self.memory_path = memory_path or replay_path.parent / "agent-state.json"
        self.max_bytes = max_bytes
        self.recent_limit = recent_limit
        self.cache_seconds = cache_seconds
        self.supabase = supabase
        self._cached_at = 0.0
        self._cached_records: list[dict[str, Any]] = []
        self._cached_traces: list[dict[str, Any]] = []
        self._cached_memory: dict[str, Any] = {}
        self._memory_cached_at = 0.0
        self.event_log = EventLogCollector(replay_path, supabase=supabase, writer=writer)
        self._lock = threading.Lock()

    def _records(self) -> list[dict[str, Any]]:
        now = time.monotonic()
        with self._lock:
            if now - self._cached_at >= self.cache_seconds:
                # Local authoritative files first for sub-millisecond local reads.
                # Supabase is an asynchronous replica / long-term archive.
                local_records = _bounded_jsonl_tail(
                    self.replay_path,
                    max_bytes=self.max_bytes,
                    limit=self.recent_limit,
                )
                if local_records:
                    self._cached_records = local_records
                elif self.supabase is not None:
                    remote = self.supabase.read_replays(self.recent_limit)
                    self._cached_records = remote if remote is not None else []
                else:
                    self._cached_records = []
                self._cached_traces = _bounded_jsonl_tail(
                    self.trace_path, max_bytes=self.max_bytes, limit=self.recent_limit
                )
                self._cached_at = now
            return list(self._cached_records)

    def _traces(self) -> list[dict[str, Any]]:
        self._records()  # refresh both bounded caches under the same TTL/lock.
        with self._lock:
            if self._cached_traces:
                return list(self._cached_traces)
            if self.supabase is not None:
                remote = self.supabase.select("arena_decision_traces", params={"select": "trace", "order": "tick.desc", "limit": str(self.recent_limit)})
                if remote is not None:
                    return [row["trace"] for row in reversed(remote) if isinstance(row.get("trace"), dict)]
            return list(self._cached_traces)

    def _memory(self) -> dict[str, Any]:
        now = time.monotonic()
        with self._lock:
            if now - self._memory_cached_at >= self.cache_seconds * 3:
                try:
                    raw = self.memory_path.read_text(encoding="utf-8")
                    if raw.strip():
                        self._cached_memory = json.loads(raw)
                        self._memory_cached_at = now
                        return dict(self._cached_memory)
                except (OSError, json.JSONDecodeError):
                    pass
                if self.supabase is not None:
                    remote = self.supabase.load_memory()
                    if isinstance(remote, dict):
                        self._cached_memory, self._memory_cached_at = remote, now
                        return dict(self._cached_memory)
            return dict(self._cached_memory)

    @staticmethod
    def _compute_chunk_saturation(
        memory_data: dict[str, Any], latest: dict[str, Any] | None,
    ) -> dict[str, dict[str, Any]]:
        """从 memory 数据计算 chunk 饱和度，用于 dashboard 展示。"""
        from .chunk_quota import compute_chunk_saturation
        # 解析当前可见资源格：优先当前帧 resources，回退到记忆中的 known_resources
        resource_cells: set[tuple[int, int]] = set()
        if latest and isinstance(latest.get("map"), dict):
            for cell in latest["map"].get("resources", []):
                if isinstance(cell, list) and len(cell) == 2 and all(type(v) is int for v in cell):
                    resource_cells.add((cell[0], cell[1]))
            if not resource_cells:
                for cell in latest["map"].get("known_resources", []):
                    if isinstance(cell, list) and len(cell) == 2 and all(type(v) is int for v in cell):
                        resource_cells.add((cell[0], cell[1]))
        # 解析已挖空格
        mined_cells: set[tuple[int, int]] = set()
        raw_mined = memory_data.get("mined_cells", [])
        if isinstance(raw_mined, list):
            for cell in raw_mined:
                if isinstance(cell, list) and len(cell) == 2 and all(type(v) is int for v in cell):
                    mined_cells.add((cell[0], cell[1]))
        # 解析资源观察
        resource_observations: dict[tuple[int, int], int] = {}
        raw_obs = memory_data.get("resource_observations", {})
        if isinstance(raw_obs, dict):
            for key, tick in raw_obs.items():
                try:
                    x_str, y_str = str(key).split(",", 1)
                    resource_observations[(int(x_str), int(y_str))] = int(tick)
                except (ValueError, AttributeError, TypeError):
                    continue
        current_tick = int(latest.get("tick", 0)) if latest else 0
        return compute_chunk_saturation(resource_cells, mined_cells, resource_observations, current_tick)

    @staticmethod
    def _compute_squads_summary(
        memory_data: dict[str, Any], latest: dict[str, Any] | None, policy_state: dict[str, Any],
    ) -> dict[str, Any]:
        """聚合计算当前的战术编组与成员归属。"""
        if not latest or not isinstance(latest.get("map"), dict):
            return {"squads": [], "assignments": {}}
        
        friendly_units = latest["map"].get("friendly", [])
        core_obj = next((u for u in friendly_units if u.get("kind") == "CORE"), None)
        core_pos = core_obj.get("position") if core_obj else None
        mode = str(latest.get("mode") or "ECONOMY")
        beacon = latest.get("map", {}).get("beacon") or {}
        beacon_pos = beacon.get("position")

        unit_tasks = memory_data.get("unit_tasks", {})
        unit_manual_squads = memory_data.get("manual_squad_assignments", {})

        squads: dict[str, dict[str, Any]] = {
            "squad_base_defense": {
                "id": "squad_base_defense",
                "name": "基地防御防线",
                "type": "BASE_DEFENSE",
                "target": core_pos,
                "members": [],
                "status": "警戒中",
            },
            "squad_expedition_beacon": {
                "id": "squad_expedition_beacon",
                "name": "信标远征打击群",
                "type": "EXPEDITION_BEACON",
                "target": beacon_pos,
                "members": [],
                "status": "推进中" if mode == "BEACON" else "待命中",
            },
            "squad_mining_escort": {
                "id": "squad_mining_escort",
                "name": "矿区采矿与护航队",
                "type": "MINING_ESCORT",
                "target": core_pos,
                "members": [],
                "status": "作业中",
            },
            "squad_scout_recon": {
                "id": "squad_scout_recon",
                "name": "迷雾探索机动组",
                "type": "SCOUT_RECON",
                "target": core_pos,
                "members": [],
                "status": "侦察巡逻中",
            },
        }

        assignments: dict[str, str] = {}

        for u in friendly_units:
            kind = u.get("kind")
            alias = u.get("alias")
            if not alias or kind == "CORE":
                continue
            
            # 手动指派优先
            manual_squad = unit_manual_squads.get(alias)
            task = unit_tasks.get(alias) or {}
            task_kind = task.get("kind", "") if isinstance(task, dict) else ""
            if manual_squad and manual_squad in squads:
                assigned_squad = manual_squad
            else:
                # 自动根据任务分配
                if "expedition" in task_kind or task_kind == "beacon":
                    assigned_squad = "squad_expedition_beacon"
                elif task_kind in ("core_guard", "defense_search", "mineral_tank", "intercept"):
                    assigned_squad = "squad_base_defense"
                elif task_kind in ("harvest", "return", "vacate", "assigned_resource", "recon_escort") or kind == "WORKER":
                    assigned_squad = "squad_mining_escort"
                else:
                    assigned_squad = "squad_scout_recon"

            assignments[alias] = assigned_squad
            squads[assigned_squad]["members"].append({
                "alias": alias,
                "kind": kind,
                "position": u.get("position"),
                "hp": u.get("hp"),
                "cargo": u.get("cargo"),
                "task": task_kind,
            })

        flow_by_mode = {
            "ATTACK": "ATTACK（进攻模式）优先编入基地防御与机动巡逻，远征信标编制暂停。",
            "DEFEND": "DEFEND（防守模式）优先回填核心防线，其余编组保留最低必要成员。",
            "BEACON": "BEACON（信标模式）优先维持信标远征与护卫编制。",
            "ECONOMY": "ECONOMY（经济模式）优先保障采矿与资源护航。",
            "EXPLORE": "EXPLORE（探索模式）将可用战斗单位调往前沿侦察。",
            "RECOVER": "RECOVER（恢复模式）暂停远离核心的高风险编组行动。",
            "RESPAWN": "RESPAWN（重生模式）等待下一份权威状态后重新编组。",
        }
        for squad in squads.values():
            members = squad["members"]
            positions = [member["position"] for member in members if isinstance(member.get("position"), list) and len(member["position"]) == 2]
            squad["causality"] = {
                "flow_reason": flow_by_mode.get(mode, "当前策略模式重新分配编组。"),
                "mode": mode,
                "member_aliases": [member["alias"] for member in members],
                "centroid": [round(sum(cell[0] for cell in positions) / len(positions)), round(sum(cell[1] for cell in positions) / len(positions))] if positions else None,
                "coordination_target": squad["target"],
            }

        return {
            "squads": list(squads.values()),
            "assignments": assignments,
        }

    @staticmethod
    def _compress_explored_segments(explored: list[list[int]]) -> list[list[int]]:
        """Scanline-compress explored cells into horizontal segments.

        Converts a list of [x, y] cells into [startX, endX, y] segments
        where consecutive cells on the same row are merged.  This typically
        reduces payload size by ~95 % for large explored maps.
        """
        if not explored:
            return []
        rows: dict[int, list[int]] = {}
        for cell in explored:
            if not isinstance(cell, list) or len(cell) != 2:
                continue
            if not all(type(v) is int for v in cell):
                continue
            rows.setdefault(cell[1], []).append(cell[0])
        segments: list[list[int]] = []
        for y in sorted(rows):
            xs = sorted(set(rows[y]))
            if not xs:
                continue
            start = xs[0]
            end = xs[0]
            for x in xs[1:]:
                if x == end + 1:
                    end = x
                else:
                    segments.append([start, end, y])
                    start = x
                    end = x
            segments.append([start, end, y])
        return segments

    def map_memory_payload(self) -> dict[str, Any]:
        """Return the static memory layer for /api/map/memory.

        Payload contains explored (scanline-compressed), mined, obstacles
        and known_resources, plus a version fingerprint for change detection.
        """
        memory_data = self._memory()

        def _parse_cell_set(raw: Any) -> list[list[int]]:
            if not isinstance(raw, list):
                return []
            return [list(item) for item in raw
                    if isinstance(item, list) and len(item) == 2
                    and all(type(v) is int for v in item)]

        explored_cells = _parse_cell_set(memory_data.get("explored", []))
        mined_cells = _parse_cell_set(memory_data.get("mined_cells", []))
        obstacles = _parse_cell_set(memory_data.get("obstacle_cells", []))
        known_resources = _parse_cell_set(memory_data.get("known_resources", []))
        # Fall back to extracting known_resources from resource_observations
        if not known_resources:
            raw_resource_obs = memory_data.get("resource_observations", {})
            if isinstance(raw_resource_obs, dict):
                for key in raw_resource_obs:
                    try:
                        x_str, y_str = str(key).split(",", 1)
                        known_resources.append([int(x_str), int(y_str)])
                    except (ValueError, AttributeError):
                        continue
        explored_segments = self._compress_explored_segments(explored_cells)
        version = hash((
            len(explored_cells), len(mined_cells), len(known_resources),
            explored_cells[0][0] if explored_cells else 0,
            explored_cells[-1][0] if explored_cells else 0,
            mined_cells[0][0] if mined_cells else 0,
            known_resources[0][0] if known_resources else 0,
        )) & 0xFFFFFFFF
        return {
            "version": version,
            "explored_segments": explored_segments,
            "mined": mined_cells,
            "obstacles": obstacles,
            "known_resources": known_resources,
        }

    def payload(self, status_snapshot: Callable[[], dict[str, object]], *, event_limit: int = 200) -> dict[str, Any]:
        raw_status = status_snapshot()
        status = {
            "status": _safe_text(raw_status.get("status"), maximum=16),
            "running": bool(raw_status.get("running")),
            "connected": bool(raw_status.get("connected")),
            "last_tick": _integer(raw_status.get("last_tick")),
            "accepted": _integer(raw_status.get("accepted")) or 0,
            "rejected": _integer(raw_status.get("rejected")) or 0,
            "reconnects": _integer(raw_status.get("reconnects")) or 0,
            "last_error": redact_error(raw_status.get("last_error")),
            "uptime_seconds": _number(raw_status.get("uptime_seconds")) or 0.0,
        }
        try:
            recent = [_project_record(record) for record in self._records()]
        except Exception:  # dashboard failure must never affect the match worker
            recent = []
        latest = recent[-1] if recent else None
        try:
            traces = [item for record in self._traces() if (item := _project_trace_record(record))]
        except Exception:
            traces = []
        command_center = traces[-1] if traces else None
        timeline = [
            {"tick": item["tick"], **task}
            for item in traces[-self.recent_limit:]
            for task in item["tasks"]
        ][-100:]
        if command_center is not None:
            command_center = _merge_entity_state({**command_center, "timeline": timeline}, latest)
        # Inject persistent memory layers (explored fog, mined resource markers)
        # from the agent-state file into the latest map projection.
        memory_data = self._memory()
        if memory_data:
            def _parse_cell_set(raw: Any) -> list[list[int]]:
                if not isinstance(raw, list):
                    return []
                return [list(item) for item in raw
                        if isinstance(item, list) and len(item) == 2
                        and all(type(v) is int for v in item)]
            explored_cells = _parse_cell_set(memory_data.get("explored", []))
            mined_cells = _parse_cell_set(memory_data.get("mined_cells", []))
            raw_resource_obs = memory_data.get("resource_observations", {})
            known_resources: list[list[int]] = []
            if isinstance(raw_resource_obs, dict):
                for key in raw_resource_obs:
                    try:
                        x_str, y_str = str(key).split(",", 1)
                        known_resources.append([int(x_str), int(y_str)])
                    except (ValueError, AttributeError):
                        continue
            # Compute a simple hash-based memory version so the frontend
            # can detect when the static memory layer has changed.
            memory_version = hash((
                len(explored_cells), len(mined_cells), len(known_resources),
                explored_cells[0][0] if explored_cells else 0,
                explored_cells[-1][0] if explored_cells else 0,
                mined_cells[0][0] if mined_cells else 0,
                known_resources[0][0] if known_resources else 0,
            ))
            # Use a positive 32-bit fingerprint to avoid JSON negatives.
            memory_version = memory_version & 0xFFFFFFFF
            if latest and isinstance(latest.get("map"), dict):
                latest["map"]["memory_version"] = memory_version

            # 注入迁移分析数据（推荐中心 + top-3 候选）
            migration_rec = memory_data.get("migration_recommendation", {})

            # 注入 chunk 饱和度数据
            chunk_saturation = self._compute_chunk_saturation(memory_data, latest)

            # 注入策略配置覆盖
            policy_state = memory_data.get("policy_state", {})

            # 注入战术编组数据
            squads_data = self._compute_squads_summary(memory_data, latest, policy_state)
            if command_center is not None:
                causality = dict(command_center.get("causality") or {})
                causality["squads"] = [
                    {"id": item["id"], "flow_reason": item.get("causality", {}).get("flow_reason"),
                     "member_aliases": item.get("causality", {}).get("member_aliases", []),
                     "centroid": item.get("causality", {}).get("centroid"),
                     "coordination_target": item.get("causality", {}).get("coordination_target")}
                    for item in squads_data["squads"]
                ]
                command_center["causality"] = causality

            return {
                "schema_version": 1,
                "generated_at": int(time.time()),
                "service": status,
                "current": latest,
                "command_center": command_center,
                "map_memory_version": memory_version,
                "migration_recommendation": migration_rec,
                "chunk_saturation": chunk_saturation,
                "squads": squads_data,
                "policy_config": {
                    "posture": policy_state.get("posture", "BALANCED"),
                    "effective_tick": policy_state.get("effective_tick", 0),
                    "overrides": {
                        k: v for k, v in policy_state.items()
                        if k not in ("version", "posture", "effective_tick") and type(v) is int
                    },
                },
                "event_log": (
                    self.event_log.payload(limit=event_limit)
                    if event_limit > 0 and event_limit != 200
                    else {"events": [], **self.event_log.summary()}
                ),
            }
        return {
            "schema_version": 1,
            "generated_at": int(time.time()),
            "service": status,
            "current": latest,
            "command_center": command_center,
            "map_memory_version": 0,
            "migration_recommendation": {},
            "chunk_saturation": {},
            "squads": {},
            "policy_config": {"posture": "BALANCED", "effective_tick": 0, "overrides": {}},
            "event_log": (
                self.event_log.payload(limit=event_limit)
                if event_limit > 0 and event_limit != 200
                else {"events": [], **self.event_log.summary()}
            ),
        }

    def event_log_payload(
        self,
        *,
        limit: int = 50,
        from_tick: int | None = None,
        to_tick: int | None = None,
        category: str | None = None,
    ) -> dict[str, Any]:
        """Return dedicated event log data for the /api/events endpoint."""
        return self.event_log.payload(
            limit=limit,
            from_tick=from_tick,
            to_tick=to_tick,
            category=category,
        )

    def replay_timeline_payload(
        self,
        status_snapshot: Callable[[], dict[str, object]],
        *,
        limit: int = 64,
    ) -> dict[str, Any]:
        """Return light-weight timeline ticks and markers without bulky frames."""
        limit = max(1, min(limit, 200))
        scale = max(1, limit // self.recent_limit + 1)
        local_records = _bounded_jsonl_tail(self.replay_path, max_bytes=self.max_bytes * scale, limit=limit)
        raw_records = local_records if local_records else (self.supabase.read_replays(limit) if self.supabase else [])
        timeline_items: list[dict[str, Any]] = []
        ticks: list[int] = []
        for record in raw_records:
            tick = record.get("tick")
            if type(tick) is not int:
                continue
            ticks.append(tick)
            markers = _replay_markers(record)
            timeline_items.append({"tick": tick, "markers": markers})
        return {
            "ticks": ticks,
            "latest_tick": ticks[-1] if ticks else None,
            "timeline": timeline_items,
        }

    def replay_payload(
        self,
        status_snapshot: Callable[[], dict[str, object]],
        *,
        limit: int = 32,
        from_tick: int | None = None,
        to_tick: int | None = None,
    ) -> dict[str, Any]:
        """Return replay frames only, for the /api/replay and /api/replay/frames endpoints."""
        limit = max(1, min(limit, 200))
        # Read more data to satisfy larger limits.
        scale = max(1, limit // self.recent_limit + 1)
        requested = limit * 2 if (from_tick is not None or to_tick is not None) else limit
        local_records = _bounded_jsonl_tail(self.replay_path, max_bytes=self.max_bytes * scale, limit=requested)
        raw_records = local_records if local_records else (self.supabase.read_replays(requested) if self.supabase else [])
        try:
            recent = [_project_record(record) for record in raw_records]
        except Exception:
            recent = []
        latest = recent[-1] if recent else None

        # Build trace lookup for this batch.
        try:
            local_traces = _bounded_jsonl_tail(self.trace_path, max_bytes=self.max_bytes * scale, limit=requested)
            if local_traces:
                raw_traces = local_traces
            elif self.supabase is not None:
                remote_traces = self.supabase.select("arena_decision_traces", params={"select": "trace", "order": "tick.desc", "limit": str(requested)})
                raw_traces = [row["trace"] for row in reversed(remote_traces) if isinstance(row.get("trace"), dict)] if remote_traces is not None else []
            else:
                raw_traces = []
            traces = [item for record in raw_traces if (item := _project_trace_record(record))]
        except Exception:
            traces = []
        trace_by_tick = {item["tick"]: item for item in traces}

        # Inject memory version into latest snapshot (heavy data lives
        # in /api/map/memory; the replay frame only carries the version).
        memory_data = self._memory()
        if memory_data and latest and isinstance(latest.get("map"), dict):
            explored_cells = []
            raw_explored = memory_data.get("explored", [])
            if isinstance(raw_explored, list):
                for item in raw_explored:
                    if isinstance(item, list) and len(item) == 2 and all(type(v) is int for v in item):
                        explored_cells.append(item)
            mined_cells_count = 0
            raw_mined = memory_data.get("mined_cells", [])
            if isinstance(raw_mined, list):
                mined_cells_count = sum(1 for item in raw_mined
                    if isinstance(item, list) and len(item) == 2 and all(type(v) is int for v in item))
            known_count = 0
            raw_resource_obs = memory_data.get("resource_observations", {})
            if isinstance(raw_resource_obs, dict):
                known_count = len(raw_resource_obs)
            memory_version = hash((
                len(explored_cells), mined_cells_count, known_count,
                explored_cells[0][0] if explored_cells else 0,
                explored_cells[-1][0] if explored_cells else 0,
            )) & 0xFFFFFFFF
            latest["map"]["memory_version"] = memory_version

        # Compute timeline from traces.
        timeline = [
            {"tick": item["tick"], **task}
            for item in traces[-self.recent_limit:]
            for task in item["tasks"]
        ][-100:]

        frames: list[dict[str, Any]] = []
        for snapshot in recent:
            tick = snapshot.get("tick")
            if from_tick is not None and tick is not None and tick <= from_tick:
                continue
            if to_tick is not None and tick is not None and tick > to_tick:
                continue
            trace = trace_by_tick.get(tick)
            center = _merge_entity_state({**trace, "timeline": timeline}, snapshot) if trace else None
            frames.append({
                "tick": tick, "snapshot": snapshot,
                "command_center": center, "markers": _replay_markers(snapshot),
            })
        frames = frames[-limit:]
        return {
            "frames": frames,
            "ticks": [frame["tick"] for frame in frames],
        }


_STATIC_ROOT = Path(__file__).with_name("web") / "static"
_APP_ROOT = Path(os.environ.get("ARENA_HERO_FRONTEND_APP_ROOT") or str(_STATIC_ROOT / "app"))
_STATIC_TYPES = {
    "pixi.min.js": "application/javascript; charset=utf-8",
}


def dashboard_static_asset(path: str) -> tuple[bytes, str] | None:
    """Return only allowlisted packaged dashboard assets; no directory traversal."""
    if path.startswith("/static/app/"):
        return dashboard_app_asset(path)
    name = path.removeprefix("/static/")
    content_type = _STATIC_TYPES.get(name)
    if content_type is None:
        return None
    try:
        return (_STATIC_ROOT / name).read_bytes(), content_type
    except OSError:
        return None


def dashboard_app_asset(path: str) -> tuple[bytes, str] | None:
    """Serve a built Vue asset below the isolated frontend app directory."""
    name = path.removeprefix("/static/app/")
    if not name or name.startswith(("/", "\\")):
        return None
    root = _APP_ROOT.resolve()
    candidate = (root / name).resolve()
    if candidate != root and root not in candidate.parents:
        return None
    try:
        if not candidate.is_file():
            return None
        content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        if content_type == "text/javascript":
            content_type = "application/javascript"
        return candidate.read_bytes(), f"{content_type}; charset=utf-8" if content_type.startswith("text/") or content_type in {"application/javascript", "application/json"} else content_type
    except OSError:
        return None
