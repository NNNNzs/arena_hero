"""Bounded, redacted dashboard projection for the Arena Hero service."""

from __future__ import annotations

import json
import re
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Any, Callable

from .context import _visible_cells


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
            "obstacles": [item for cell in raw_obstacles[:200] if (item := _cell(cell))],
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
    ) -> None:
        self.replay_path = replay_path
        self.trace_path = trace_path or replay_path.with_name("decision-trace.jsonl")
        self.memory_path = memory_path or replay_path.parent / "agent-state.json"
        self.max_bytes = max_bytes
        self.recent_limit = recent_limit
        self.cache_seconds = cache_seconds
        self._cached_at = 0.0
        self._cached_records: list[dict[str, Any]] = []
        self._cached_traces: list[dict[str, Any]] = []
        self._cached_memory: dict[str, Any] = {}
        self._memory_cached_at = 0.0
        self._lock = threading.Lock()

    def _records(self) -> list[dict[str, Any]]:
        now = time.monotonic()
        with self._lock:
            if now - self._cached_at >= self.cache_seconds:
                self._cached_records = _bounded_jsonl_tail(
                    self.replay_path,
                    max_bytes=self.max_bytes,
                    limit=self.recent_limit,
                )
                self._cached_traces = _bounded_jsonl_tail(
                    self.trace_path, max_bytes=self.max_bytes, limit=self.recent_limit
                )
                self._cached_at = now
            return list(self._cached_records)

    def _traces(self) -> list[dict[str, Any]]:
        self._records()  # refresh both bounded caches under the same TTL/lock.
        with self._lock:
            return list(self._cached_traces)

    def _memory(self) -> dict[str, Any]:
        now = time.monotonic()
        with self._lock:
            if now - self._memory_cached_at >= self.cache_seconds * 3:
                try:
                    raw = self.memory_path.read_text(encoding="utf-8")
                    self._cached_memory = json.loads(raw) if raw.strip() else {}
                except (FileNotFoundError, OSError, json.JSONDecodeError):
                    self._cached_memory = {}
                self._memory_cached_at = now
            return dict(self._cached_memory)

    @staticmethod
    def _compute_chunk_saturation(
        memory_data: dict[str, Any], latest: dict[str, Any] | None,
    ) -> dict[str, dict[str, Any]]:
        """从 memory 数据计算 chunk 饱和度，用于 dashboard 展示。"""
        from .chunk_quota import compute_chunk_saturation
        # 解析当前可见资源格
        resource_cells: set[tuple[int, int]] = set()
        if latest and isinstance(latest.get("map"), dict):
            for cell in latest["map"].get("resources", []):
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

    def payload(self, status_snapshot: Callable[[], dict[str, object]]) -> dict[str, Any]:
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
            if latest and isinstance(latest.get("map"), dict):
                latest["map"]["explored"] = explored_cells
                latest["map"]["mined"] = mined_cells
                latest["map"]["known_resources"] = known_resources

            # 注入迁移分析数据（推荐中心 + top-3 候选）
            migration_rec = memory_data.get("migration_recommendation", {})

            # 注入 chunk 饱和度数据
            chunk_saturation = self._compute_chunk_saturation(memory_data, latest)

            # 注入策略配置覆盖
            policy_state = memory_data.get("policy_state", {})

            return {
                "schema_version": 1,
                "generated_at": int(time.time()),
                "service": status,
                "current": latest,
                "command_center": command_center,
                "migration_recommendation": migration_rec,
                "chunk_saturation": chunk_saturation,
                "policy_config": {
                    "posture": policy_state.get("posture", "BALANCED"),
                    "effective_tick": policy_state.get("effective_tick", 0),
                    "overrides": {
                        k: v for k, v in policy_state.items()
                        if k not in ("version", "posture", "effective_tick") and type(v) is int
                    },
                },
            }
        return {
            "schema_version": 1,
            "generated_at": int(time.time()),
            "service": status,
            "current": latest,
            "command_center": command_center,
            "migration_recommendation": {},
            "chunk_saturation": {},
            "policy_config": {"posture": "BALANCED", "effective_tick": 0, "overrides": {}},
        }

    def replay_payload(
        self,
        status_snapshot: Callable[[], dict[str, object]],
        *,
        limit: int = 32,
        from_tick: int | None = None,
    ) -> dict[str, Any]:
        """Return replay frames only, for the /api/replay endpoint."""
        limit = max(1, min(limit, 200))
        # Read more data to satisfy larger limits.
        scale = max(1, limit // self.recent_limit + 1)
        raw_records = _bounded_jsonl_tail(
            self.replay_path,
            max_bytes=self.max_bytes * scale,
            limit=limit * 2 if from_tick is not None else limit,
        )
        try:
            recent = [_project_record(record) for record in raw_records]
        except Exception:
            recent = []
        latest = recent[-1] if recent else None

        # Build trace lookup for this batch.
        try:
            raw_traces = _bounded_jsonl_tail(
                self.trace_path,
                max_bytes=self.max_bytes * scale,
                limit=limit * 2 if from_tick is not None else limit,
            )
            traces = [item for record in raw_traces if (item := _project_trace_record(record))]
        except Exception:
            traces = []
        trace_by_tick = {item["tick"]: item for item in traces}

        # Inject memory into latest snapshot.
        memory_data = self._memory()
        if memory_data and latest and isinstance(latest.get("map"), dict):
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
            latest["map"]["explored"] = explored_cells
            latest["map"]["mined"] = mined_cells
            latest["map"]["known_resources"] = known_resources

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


# Kept below for backwards-compatible source history; Phase 8 serves the
# maintained command center assets defined after this legacy shell.
DASHBOARD_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Arena Hero · 作战控制台</title>
<style>
:root{color-scheme:dark;--bg:#090d12;--panel:#111821;--panel2:#151e29;--line:#263342;--text:#e8eef5;--muted:#8fa0b3;--cyan:#49d7c4;--blue:#58a6ff;--red:#ff6b7a;--amber:#f4bd61}*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 15% -10%,#183044 0,transparent 35%),var(--bg);color:var(--text);font:14px/1.5 system-ui,-apple-system,"Segoe UI","Microsoft YaHei",sans-serif}main{width:min(1180px,calc(100% - 32px));margin:auto;padding:32px 0 56px}header{display:flex;align-items:flex-start;justify-content:space-between;gap:20px;margin-bottom:24px}h1{font-size:24px;letter-spacing:.04em;margin:0 0 5px}.sub,.muted{color:var(--muted)}.status{display:flex;align-items:center;gap:9px;padding:8px 12px;border:1px solid var(--line);border-radius:99px;background:#0c1219}.dot{width:9px;height:9px;border-radius:50%;background:var(--red);box-shadow:0 0 12px currentColor}.ok .dot{background:var(--cyan)}.grid{display:grid;grid-template-columns:repeat(12,1fr);gap:14px}.card{background:linear-gradient(145deg,var(--panel2),var(--panel));border:1px solid var(--line);border-radius:12px;padding:17px;min-width:0}.metric{grid-column:span 3}.wide{grid-column:span 8}.side{grid-column:span 4}.label{font-size:12px;color:var(--muted);letter-spacing:.08em}.value{font-size:27px;font-weight:650;margin-top:5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.value small{font-size:13px;color:var(--muted);font-weight:400}.section-title{font-size:15px;margin:0 0 13px}.bar{height:7px;border-radius:8px;background:#25303d;overflow:hidden;margin-top:10px}.bar i{display:block;height:100%;background:linear-gradient(90deg,var(--blue),var(--cyan));width:0}.error{color:#ffc1c7;word-break:break-word}.turn{display:grid;grid-template-columns:70px 110px 1fr 90px;gap:12px;align-items:center;padding:11px 0;border-top:1px solid var(--line)}.turn:first-of-type{border-top:0}.tag{display:inline-block;padding:3px 8px;border-radius:5px;background:#203349;color:#b9d9ff;font-size:12px}.chips{display:flex;gap:5px;flex-wrap:wrap}.chip{color:#b9c7d6;background:#1b2632;padding:2px 7px;border-radius:4px;font-size:12px}.empty{padding:30px 10px;text-align:center;color:var(--muted)}footer{margin-top:18px;color:var(--muted);font-size:12px}@media(max-width:820px){.metric{grid-column:span 6}.wide,.side{grid-column:span 12}.turn{grid-template-columns:58px 1fr}.turn .chips,.turn .latency{grid-column:2}}@media(max-width:480px){main{width:min(100% - 20px,1180px);padding-top:20px}header{display:block}.status{margin-top:14px;width:max-content}.metric{grid-column:span 12}.value{font-size:24px}}
</style></head><body><main><header><div><h1>ARENA HERO 作战控制台</h1><div class="sub">24/7 自主战术 Agent · 实时态势</div></div><div id="status" class="status"><span class="dot"></span><span>正在获取状态</span></div></header>
<section class="grid"><div class="card metric"><div class="label">运行时间</div><div class="value" id="uptime">—</div></div><div class="card metric"><div class="label">最近 TICK</div><div class="value" id="tick">—</div></div><div class="card metric"><div class="label">提交成功 / 失败</div><div class="value" id="submits">—</div></div><div class="card metric"><div class="label">重连次数</div><div class="value" id="reconnects">—</div></div>
<div class="card side"><h2 class="section-title">当前态势</h2><div class="label">策略模式</div><div class="value" id="mode">等待数据</div><div style="margin-top:17px" class="label">资源 / 容量</div><div class="value" id="resources">—</div><div class="bar"><i id="resourceBar"></i></div><div style="margin-top:17px" class="label">人口</div><div class="value" id="population">—</div></div>
<div class="card wide"><h2 class="section-title">最近回合</h2><div id="turns" class="empty">尚无回放记录</div></div><div class="card wide"><h2 class="section-title">最近错误</h2><div id="error" class="muted">无</div></div><div class="card side"><h2 class="section-title">数据状态</h2><div id="dataState" class="muted">正在同步…</div></div></section><footer>每 3 秒刷新 · 开发挂载模式 · 数据来自本机脱敏回放 · 页面不会展示凭据或完整对象标识</footer></main>
<script>
const $=id=>document.getElementById(id), esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function duration(s){s=Math.max(0,Math.floor(Number(s)||0));const d=Math.floor(s/86400),h=Math.floor(s%86400/3600),m=Math.floor(s%3600/60);return[d&&d+'天',h&&h+'时',m+'分'].filter(Boolean).join(' ')}
function render(d){const s=d.service||{},c=d.current,online=!!s.running,connected=!!s.connected;$('status').className='status '+(online&&connected?'ok':'');$('status').lastElementChild.textContent=!online?'服务已停止':connected?'已连接 · 对战中':'服务在线 · 等待连接';$('uptime').textContent=duration(s.uptime_seconds);$('tick').textContent=s.last_tick??c?.tick??'—';$('submits').innerHTML=`${s.accepted??0} <small>/ ${s.rejected??0}</small>`;$('reconnects').textContent=s.reconnects??0;$('mode').textContent=c?.mode_label||'等待数据';$('resources').innerHTML=c?`${c.resources??'—'} <small>/ ${c.resource_capacity??'—'}</small>`:'—';$('population').textContent=c?.population??'—';const pct=c&&c.resource_capacity?Math.min(100,Math.max(0,c.resources/c.resource_capacity*100)):0;$('resourceBar').style.width=pct+'%';$('error').className=s.last_error?'error':'muted';$('error').textContent=s.last_error||'无';$('dataState').textContent=d.replay?.available?`已载入最近 ${d.replay.records} 个有效回合；最后同步 ${new Date(d.generated_at*1000).toLocaleTimeString('zh-CN')}`:'回放尚不可用；服务会继续等待首个成功提交。';const rows=(d.recent||[]).map(r=>`<div class="turn"><b>#${esc(r.tick??'—')}</b><span class="tag">${esc(r.mode_label)}</span><div class="chips">${(r.actions||[]).map(a=>`<span class="chip">${esc(a.label)} × ${esc(a.count)}</span>`).join('')||'<span class="muted">无动作</span>'}${(r.events||[]).map(e=>`<span class="chip">事件 ${esc(e.type)}</span>`).join('')}</div><span class="latency muted">${r.decision_ms==null?'—':esc(r.decision_ms.toFixed(1))+' ms'}</span></div>`).join('');$('turns').className=rows?'':'empty';$('turns').innerHTML=rows||'尚无有效回放记录';}
async function refresh(){try{const r=await fetch('/api/dashboard',{cache:'no-store'});if(!r.ok)throw Error('HTTP '+r.status);render(await r.json())}catch(e){$('status').className='status';$('status').lastElementChild.textContent='状态获取失败';$('dataState').textContent='Dashboard API 暂时不可用，将自动重试。'}}refresh();setInterval(refresh,3000);
</script></body></html>"""


_STATIC_ROOT = Path(__file__).with_name("web") / "static"
_STATIC_TYPES = {
    "command-center.css": "text/css; charset=utf-8",
    "command-center.js": "application/javascript; charset=utf-8",
    "tactical-map/layers.js": "application/javascript; charset=utf-8",
    "tactical-map/camera.js": "application/javascript; charset=utf-8",
    "tactical-map/radar.js": "application/javascript; charset=utf-8",
    "tactical-map/renderers.js": "application/javascript; charset=utf-8",
    "tactical-map/input.js": "application/javascript; charset=utf-8",
    "tactical-map/main.js": "application/javascript; charset=utf-8",
    "pixi.min.js": "application/javascript; charset=utf-8",
}


def dashboard_static_asset(path: str) -> tuple[bytes, str] | None:
    """Return only allowlisted packaged dashboard assets; no directory traversal."""
    name = path.removeprefix("/static/")
    content_type = _STATIC_TYPES.get(name)
    if content_type is None:
        return None
    try:
        return (_STATIC_ROOT / name).read_bytes(), content_type
    except OSError:
        return None


DASHBOARD_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Arena Hero · 作战指挥中心</title><link rel="stylesheet" href="/static/command-center.css"><script src="/static/pixi.min.js"></script></head>
<body><main class="command-center">
<header class="command-bar">
  <div class="brand"><span class="eyebrow">ARENA HERO / TACTICAL OPERATIONS</span><h1>作战指挥中心</h1><p>权威回合态势 · 地图下令 · 脱敏数据</p></div>
  <div class="top-metrics" aria-label="实时作战状态">
    <div class="metric"><span>TICK</span><strong id="tick">—</strong></div>
    <div class="metric"><span>资源 / 容量</span><strong id="resources">—</strong></div>
    <div class="metric"><span>策略模式</span><strong id="mode">—</strong></div>
    <div class="metric"><span>单位在线</span><strong id="unitCount">—</strong></div>
  </div>
  <div id="status" class="status">正在获取状态</div>
</header>

<section class="operations-shell">
  <aside class="panel unit-panel">
    <div class="section-head"><div><span class="eyebrow">ORDER OF BATTLE</span><h2>单位编组</h2></div><span id="unitFilterCount" class="tick">—</span></div>
    <input id="unitSearch" class="unit-search" type="search" placeholder="搜索脱敏别名…" aria-label="搜索单位">
    <div id="unitFilters" class="unit-filters"><button class="filter-btn is-active" data-kind="ALL">全部</button><button class="filter-btn" data-kind="CORE">核心</button><button class="filter-btn" data-kind="WORKER">工人</button><button class="filter-btn" data-kind="VANGUARD">先锋</button><button class="filter-btn" data-kind="RANGER">游侠</button></div>
    <div id="unitList" class="unit-list muted">尚无单位</div>
  </aside>

  <section class="map-panel" aria-label="战术地图工作区">
    <div class="map-heading"><div><span class="eyebrow">LIVE BATTLESPACE / GRID CONTROL</span><h2>战术地图</h2></div><div class="map-status"><span id="mapModeBadge" class="tag">实时态势</span><span id="mapSummary">—</span><span id="rendererStatus">渲染器待命</span></div></div>
    <div class="map-toolbar" aria-label="地图控件">
      <div class="tool-group"><button id="mapZoomIn" class="map-tool" title="放大">＋</button><button id="mapZoomOut" class="map-tool" title="缩小">－</button><button id="mapReset" class="map-tool" title="重置视口">归中</button></div>
      <label class="layer-toggle"><input id="layerFog" type="checkbox" checked> 迷雾</label>
      <label class="layer-toggle">视野 <select id="visionMode" aria-label="视野显示模式"><option value="selected">选中 + 核心</option><option value="all">全部单位</option><option value="off">关闭</option></select></label>
      <label class="layer-toggle"><input id="layerCoordinates" type="checkbox" checked> 边尺</label>
      <label class="layer-toggle"><input id="layerLabels" type="checkbox" checked> 标注</label>
      <span class="map-legend"><i class="legend-unit worker"></i>工人 <i class="legend-unit vanguard"></i>先锋 <i class="legend-unit ranger"></i>游侠 <i class="legend-resource"></i>资源</span>
      <button id="mapPickTarget" class="neutral map-order">地图选点下令</button>
    </div>
    <div id="map-viewport" class="map" role="application" tabindex="0" aria-label="战术地图；拖拽平移，滚轮缩放，方向键移动视口">
      <div id="map-stage" class="map-stage"><div class="map-loading">正在初始化战术态势渲染器…</div></div>
      <div id="map-axis-x" class="map-axis map-axis-x" aria-hidden="true"></div><div id="map-axis-y" class="map-axis map-axis-y" aria-hidden="true"></div>
      <div class="map-reticle" aria-hidden="true"></div>
      <div class="map-coordinate-strip"><span id="mapCenterCoordinate">中心 —</span><span id="mapCursor">光标 —</span><span id="mapSelectionCoordinate">选中 —</span><span id="mapTargetCoordinate">目标 —</span></div>
      <div id="mapTargetMode" class="map-target-mode" hidden>选点下单 · 移动光标预览路径 · 单击锁定目标</div>
      <div id="mapRendererState" class="map-renderer-state" hidden></div><pre id="mapDebugHud" class="map-debug-hud" hidden></pre>
    </div>
  </section>

  <aside class="panel situation-panel">
    <section class="situation-section"><div class="section-head"><div><span class="eyebrow">SELECTED ELEMENT</span><h2>单位状态与决策链</h2></div></div><div id="unitDetail" class="unit-detail"><div class="empty-detail">从左侧选择单位</div></div></section>
    <section class="situation-section"><div class="section-head"><h3>附近资源点</h3><span class="resource-note">余量由协议隐藏</span></div><div id="resourceInfo" class="resource-list muted">当前无可见资源点</div></section>
    <section class="situation-section"><h3>目标与任务概览</h3><div id="goals" class="compact-list muted">尚无决策记录</div><div id="tasks" class="compact-list muted"></div></section>
    <details class="order-drawer" open><summary>人工任务 · 地图下令</summary><div class="detail-actions"><p class="muted">只对选中对象生效，下一权威 Tick 会重新校验。</p><div class="auth-row"><input id="password" type="password" autocomplete="current-password" placeholder="管理员口令"><button id="login" class="neutral">认证</button></div><span id="loginState" class="muted" aria-live="polite"></span><div class="task-form"><label for="taskAlias">对象</label><select id="taskAlias"><option value="">选择当前实体…</option></select><label for="taskKind">任务</label><select id="taskKind"><option value="HOLD_POSITION">原地待命</option><option value="RETREAT_TO_CORE">撤回核心</option><option value="HARVEST_VISIBLE">采集可见资源</option><option value="MOVE_TO_CELL">移动到目标</option></select><label for="taskPriority">优先级</label><select id="taskPriority"><option value="500">普通 · 500</option><option value="800" selected>高 · 800</option><option value="950">紧急 · 950</option></select><label for="taskTarget">目标坐标</label><input id="taskTarget" inputmode="numeric" placeholder="x,y（仅移动）"><button id="assign" class="primary-action">排队任务</button></div><p id="taskState" class="muted" aria-live="polite"></p><div id="taskCommands" class="command-list muted">认证后显示人工任务。</div></div></details>
    <details class="advanced-drawer"><summary>策略与指挥设置</summary><div class="advanced-grid"><section><h3>核心迁移</h3><input id="migrationTarget" placeholder="目标 x,y"><div class="button-row"><button id="migrate" class="neutral">排队迁移</button><button id="cancelMigration" class="neutral">取消</button></div></section><section><h3>策略姿态</h3><p class="muted">生效：<span id="policyCurrent">均衡</span></p><select id="policyPosture"><option value="BALANCED">均衡</option><option value="DEFENSIVE">防御</option><option value="ECONOMY">经济</option><option value="AGGRESSIVE">进攻</option></select><button id="setPolicy" class="neutral">排队策略</button><p id="policyState" class="muted">认证后可更新。</p></section><section><h3>策略参数热更新</h3><div id="policyConfig" class="config-list muted">加载中…</div></section></div><h3>迁移分析</h3><div id="migrationAnalysis" class="compact-list muted">尚无迁移分析数据</div><div class="button-row"><button id="triggerAnalysis" class="neutral">立即扫描</button></div><p id="migrationState" class="muted"></p><h3>矿区饱和度</h3><div id="chunkSaturation" class="compact-list muted">尚无已知矿区</div><h3>命令审计</h3><div id="commands" class="compact-list muted">尚无命令</div><h3>任务切换</h3><div id="timeline" class="compact-list muted">尚无任务切换记录</div></details>
  </aside>
</section>

<section class="replay-panel" aria-label="32 Tick 作战时间轴">
  <div class="replay-heading"><div><span class="eyebrow">REPLAY WINDOW / 32 TICKS</span><h2>作战时间轴</h2></div><div><span id="replayState" class="muted">等待回放快照</span><strong id="replayTick" class="tick">—</strong></div></div>
  <div class="replay-track"><input id="replaySlider" type="range" min="0" max="0" value="0" step="1" aria-label="回放 Tick"><div id="replayMarkers" class="replay-markers" aria-label="关键战局事件"></div></div>
  <div class="replay-controls"><button id="replayStart" class="neutral" title="回到起点">⏮ 首帧</button><button id="replayPrev" class="neutral" title="上一帧">⏪</button><button id="replayPlay" class="neutral" title="播放或暂停">▶ 播放</button><button id="replayNext" class="neutral" title="下一帧">⏩</button><button id="replayLive" class="neutral" title="跟随实时">⏭ 实时</button></div>
</section>
</main><script src="/static/command-center.js"></script><script src="/static/tactical-map/layers.js"></script><script src="/static/tactical-map/camera.js"></script><script src="/static/tactical-map/radar.js"></script><script src="/static/tactical-map/renderers.js"></script><script src="/static/tactical-map/input.js"></script><script src="/static/tactical-map/main.js"></script></body></html>"""
