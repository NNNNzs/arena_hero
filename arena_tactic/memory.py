"""Small, versioned, controller-free Agent memory with atomic persistence."""

from __future__ import annotations

import json
import os
from collections import Counter
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID
import re
from datetime import datetime, timezone

from .context import DecisionContext
from .identity import entity_alias
from .models import AgentConfig, Position, StrategicMode


MEMORY_VERSION = 3
_CARDINAL = ((0, -1), (0, 1), (-1, 0), (1, 0))
_SENSITIVE = ("credential", "controller", "authorization", "cookie", "token", "secret")
_UUID_RE = re.compile(r"(?i)\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")
_EVENT_COUNT_RE = re.compile(r"^[A-Z0-9_]+$")
_EVENT_ID_RE = re.compile(r"^(?:entity_[0-9a-f]{4,64}|event_[A-Za-z0-9_-]{1,96}|legacy-[A-Za-z0-9_-]{1,64})$")
_MANUAL_TASKS = frozenset({"RETREAT_TO_CORE", "HOLD_POSITION", "HARVEST_VISIBLE", "MOVE_TO_CELL"})


def _safe_event_id(value: Any) -> str:
    text = str(value)
    alias = _persisted_task_key(text)
    candidate = alias or text
    if any(word in candidate.lower() for word in _SENSITIVE):
        return ""
    return candidate if _EVENT_ID_RE.fullmatch(candidate) else ""


def _safe_event_counts(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    return {str(key): item for key, item in value.items()
            if _EVENT_COUNT_RE.fullmatch(str(key)) and type(item) is int and item >= 0}


def _cell_key(cell: Position) -> str:
    return f"{cell[0]},{cell[1]}"


def _parse_cell(value: str) -> Position:
    x, y = value.split(",", 1)
    return int(x), int(y)


def _persisted_task_key(value: str) -> str:
    if value.startswith("entity_"):
        return value
    try:
        alias = entity_alias(UUID(value))
    except (TypeError, ValueError):
        return ""
    return alias or ""


def _safe_task(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, Any] = {}
    for key in ("kind", "target", "step", "sector", "sector_since", "failures", "attempt_tick", "since_tick"):
        item = value.get(key)
        if any(word in key.lower() for word in _SENSITIVE):
            continue
        if key in {"target", "step"}:
            if isinstance(item, (list, tuple)) and len(item) == 2 and all(type(part) is int for part in item):
                result[key] = list(item)
        elif key == "kind":
            if isinstance(item, str) and len(item) <= 64 and not _UUID_RE.search(item) and not any(word in item.lower() for word in _SENSITIVE):
                result[key] = item
        elif type(item) is int:
            result[key] = item
    return result


def _safe_cells(value: Any) -> set[Position]:
    if not isinstance(value, (list, tuple)):
        return set()
    return {
        (item[0], item[1]) for item in value
        if isinstance(item, (list, tuple)) and len(item) == 2 and all(type(part) is int for part in item)
    }


def _safe_cell_map(value: Any) -> dict[Position, int]:
    if not isinstance(value, dict):
        return {}
    result: dict[Position, int] = {}
    for cell, raw in value.items():
        try:
            parsed = _parse_cell(cell)
            number = int(raw)
        except (AttributeError, TypeError, ValueError):
            continue
        result[parsed] = number
    return result


def _safe_objective_states(value: Any) -> dict[str, dict[str, Any]]:
    """Keep only the small controller-free lifecycle checkpoints we own."""
    if not isinstance(value, dict):
        return {}
    states: dict[str, dict[str, Any]] = {}
    for name in ("beacon", "migration", "attack"):
        raw = value.get(name)
        if not isinstance(raw, dict):
            continue
        state: dict[str, Any] = {}
        stage = raw.get("stage")
        if isinstance(stage, str) and re.fullmatch(r"[A-Z_]{1,32}", stage):
            state["stage"] = stage
        for key in ("carrier_alias", "target_alias"):
            item = raw.get(key)
            if isinstance(item, str) and _EVENT_ID_RE.fullmatch(item):
                state[key] = item
        for key in ("destination", "recovery_cell"):
            item = raw.get(key)
            if isinstance(item, (list, tuple)) and len(item) == 2 and all(type(axis) is int for axis in item):
                state[key] = list(item)
        for key in ("replan_count",):
            item = raw.get(key)
            if type(item) is int and item >= 0:
                state[key] = item
        if type(raw.get("start_attempted")) is bool:
            state["start_attempted"] = raw["start_attempted"]
        if type(raw.get("manual")) is bool:
            state["manual"] = raw["manual"]
        if state:
            states[name] = state
    return states


def _safe_manual_assignments(value: Any) -> dict[str, dict[str, Any]]:
    """Persist only bounded, alias-keyed manual tasks; never raw IDs or payloads."""
    if not isinstance(value, dict):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for alias, raw in value.items():
        if not isinstance(alias, str) or not _EVENT_ID_RE.fullmatch(alias) or not isinstance(raw, dict):
            continue
        kind = raw.get("kind")
        until = raw.get("until_tick")
        priority = raw.get("priority")
        if kind not in _MANUAL_TASKS or type(until) is not int or until < 0 or type(priority) is not int or not 0 <= priority <= 1000:
            continue
        item: dict[str, Any] = {"kind": kind, "until_tick": until, "priority": priority}
        target = raw.get("target")
        if isinstance(target, (list, tuple)) and len(target) == 2 and all(type(axis) is int for axis in target):
            item["target"] = list(target)
        if kind == "MOVE_TO_CELL" and "target" not in item:
            continue
        result[alias] = item
    return result


def _safe_scheduler_assignments(value: Any) -> dict[str, dict[str, Any]]:
    """Persist only alias-keyed scheduler outputs and their bounded lease."""
    if not isinstance(value, dict):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for alias, raw in value.items():
        if not isinstance(alias, str) or not _EVENT_ID_RE.fullmatch(alias) or not isinstance(raw, dict):
            continue
        task_id, kind, role = raw.get("task_id"), raw.get("kind"), raw.get("role")
        priority, lease = raw.get("priority"), raw.get("lease_until_tick")
        if not all(isinstance(value, str) and len(value) <= 96 for value in (task_id, kind, role)):
            continue
        if type(priority) is not int or not 0 <= priority <= 1000 or type(lease) is not int or lease < 0:
            continue
        item: dict[str, Any] = {"task_id": task_id, "kind": kind, "role": role,
                                "priority": priority, "lease_until_tick": lease}
        target = raw.get("target")
        if isinstance(target, (list, tuple)) and len(target) == 2 and all(type(axis) is int for axis in target):
            item["target"] = list(target)
        result[alias] = item
    return result


def _safe_policy_state(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {"version": 0, "posture": "BALANCED", "effective_tick": 0}
    version, effective = value.get("version"), value.get("effective_tick")
    posture = value.get("posture")
    if type(version) is not int or version < 0 or type(effective) is not int or effective < 0:
        return {"version": 0, "posture": "BALANCED", "effective_tick": 0}
    if posture not in {"BALANCED", "DEFENSIVE", "ECONOMY", "AGGRESSIVE"}:
        return {"version": 0, "posture": "BALANCED", "effective_tick": 0}
    return {"version": version, "posture": posture, "effective_tick": effective}


@dataclass(slots=True)
class AgentMemory:
    version: int = MEMORY_VERSION
    last_tick: int = 0
    last_mode: StrategicMode = StrategicMode.RESPAWN
    mode_since_tick: int = 0
    no_resource_ticks: int = 0
    obstacles: set[Position] = field(default_factory=set)
    explored: set[Position] = field(default_factory=set)
    resource_observations: dict[Position, int] = field(default_factory=dict)
    temporary_blocks: dict[Position, int] = field(default_factory=dict)
    unit_tasks: dict[str, dict[str, Any]] = field(default_factory=dict)
    manual_assignments: dict[str, dict[str, Any]] = field(default_factory=dict)
    scheduler_assignments: dict[str, dict[str, Any]] = field(default_factory=dict)
    policy_state: dict[str, Any] = field(default_factory=lambda: {"version": 0, "posture": "BALANCED", "effective_tick": 0})
    objective_states: dict[str, dict[str, Any]] = field(default_factory=dict)
    processed_event_ids: list[str] = field(default_factory=list)
    event_counts: dict[str, int] = field(default_factory=dict)
    submitted_ticks: int = 0
    accepted_ticks: int = 0

    def clone(self) -> "AgentMemory":
        return deepcopy(self)

    def frontier(self) -> set[Position]:
        result: set[Position] = set()
        for x, y in self.explored:
            for dx, dy in _CARDINAL:
                cell = (x + dx, y + dy)
                if cell not in self.explored and cell not in self.obstacles:
                    result.add(cell)
        return result

    def active_temporary_blocks(self, tick: int) -> set[Position]:
        return {
            cell for cell, blocked_until in self.temporary_blocks.items()
            if blocked_until >= tick
        }

    def advance(
        self, context: DecisionContext, config: AgentConfig
    ) -> "AgentMemory":
        next_memory = self.clone()
        next_memory.version = MEMORY_VERSION
        # Persisted v3 aliases are rebound only to objects in this authoritative
        # Turn; no controller or stale runtime identifier is retained.
        for unit in context.units:
            alias = entity_alias(unit.id)
            if alias in next_memory.unit_tasks and str(unit.id) not in next_memory.unit_tasks:
                next_memory.unit_tasks[str(unit.id)] = next_memory.unit_tasks.pop(alias)
        next_memory.obstacles.update(context.obstacle_cells)
        next_memory.explored.update(context.observed_cells - context.obstacle_cells)
        next_memory.temporary_blocks = {
            cell: blocked_until
            for cell, blocked_until in next_memory.temporary_blocks.items()
            if blocked_until >= context.tick
        }
        current_aliases = {entity_alias(item_id) for item_id in context.current_objects}
        next_memory.manual_assignments = {
            alias: task for alias, task in next_memory.manual_assignments.items()
            if alias in current_aliases and int(task.get("until_tick", -1)) >= context.tick
        }
        next_memory.scheduler_assignments = {
            alias: task for alias, task in next_memory.scheduler_assignments.items()
            if alias in current_aliases and int(task.get("lease_until_tick", -1)) >= context.tick
        }

        if len(next_memory.explored) > config.explored_history_limit:
            # Deterministic trimming favors cells closest to current friendly
            # positions while retaining a bounded JSON state file.
            origins = [unit.position for unit in context.units]
            if context.core is not None:
                origins.append(context.core.position)
            if origins:
                next_memory.explored = set(
                    sorted(
                        next_memory.explored,
                        key=lambda cell: (
                            min(
                                abs(cell[0] - origin[0])
                                + abs(cell[1] - origin[1])
                                for origin in origins
                            ),
                            cell,
                        ),
                    )[: config.explored_history_limit]
                )

        # Current visible facts overwrite remembered resource observations.
        for cell in context.resource_cells:
            next_memory.resource_observations[cell] = context.tick
        for cell in context.observed_cells - context.resource_cells:
            next_memory.resource_observations.pop(cell, None)

        processed = set(next_memory.processed_event_ids)
        counts = Counter(next_memory.event_counts)
        for event in context.events:
            event_id = entity_alias(event.event_id) or ""
            if event_id in processed:
                continue
            processed.add(event_id)
            next_memory.processed_event_ids.append(event_id)
            counts[event.event_type] += 1
            if event.actor_id is not None and event.event_type == "UNIT_MOVE_FAILED":
                unit_id = str(event.actor_id)
                task = next_memory.unit_tasks.get(unit_id)
                attempted_cell: Position | None = None
                if task is not None and isinstance(task.get("step"), list):
                    step = task["step"]
                    if len(step) == 2:
                        attempted_cell = int(step[0]), int(step[1])
                if attempted_cell is not None:
                    if event.reason_code == "MOVE_BLOCKED_TERRAIN":
                        next_memory.obstacles.add(attempted_cell)
                        next_memory.explored.discard(attempted_cell)
                    else:
                        next_memory.temporary_blocks[attempted_cell] = (
                            context.tick + config.movement_failure_cooldown_ticks
                        )
                if task is not None and task.get("kind") in {"explore", "scout"}:
                    rotated = dict(task)
                    rotated["sector"] = (int(rotated.get("sector", 0)) + 1) % 4
                    rotated["sector_since"] = context.tick
                    rotated["failures"] = int(rotated.get("failures", 0)) + 1
                    rotated.pop("target", None)
                    rotated.pop("step", None)
                    rotated.pop("attempt_tick", None)
                    next_memory.unit_tasks[unit_id] = rotated
                else:
                    next_memory.unit_tasks.pop(unit_id, None)
            elif event.actor_id is not None and event.event_type == "UNIT_MOVE_SUCCEEDED":
                task = next_memory.unit_tasks.get(str(event.actor_id))
                if task is not None:
                    task.pop("step", None)
                    task.pop("attempt_tick", None)
                    task["failures"] = 0
            elif event.actor_id is not None and event.event_type == "DEPOSIT_FAILED":
                next_memory.unit_tasks.pop(str(event.actor_id), None)
            if event.event_type == "CORE_RESPAWNED":
                next_memory.unit_tasks.clear()
                next_memory.no_resource_ticks = 0
            if event.position is not None and (
                event.event_type == "RESOURCE_DEPLETED"
                or event.reason_code == "RESOURCE_DEPLETED"
                or event.event_type == "HARVEST_SUCCEEDED"
            ):
                next_memory.resource_observations.pop(event.position, None)
        next_memory.event_counts = dict(counts)
        next_memory.processed_event_ids = next_memory.processed_event_ids[
            -config.event_history_limit :
        ]

        current_unit_ids = {str(unit.id) for unit in context.units}
        next_memory.unit_tasks = {
            unit_id: task
            for unit_id, task in next_memory.unit_tasks.items()
            if unit_id in current_unit_ids
        }

        if context.tick > next_memory.last_tick:
            next_memory.no_resource_ticks = (
                next_memory.no_resource_ticks + 1
                if not context.resource_cells
                else 0
            )
            next_memory.last_tick = context.tick
        return next_memory

    def record_mode(self, mode: StrategicMode, tick: int) -> None:
        if mode is not self.last_mode:
            self.last_mode = mode
            self.mode_since_tick = tick

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "last_tick": self.last_tick,
            "last_mode": self.last_mode.value,
            "mode_since_tick": self.mode_since_tick,
            "no_resource_ticks": self.no_resource_ticks,
            "obstacles": [list(cell) for cell in sorted(self.obstacles)],
            "explored": [list(cell) for cell in sorted(self.explored)],
            "resource_observations": {
                _cell_key(cell): tick
                for cell, tick in sorted(self.resource_observations.items())
            },
            "temporary_blocks": {
                _cell_key(cell): blocked_until
                for cell, blocked_until in sorted(self.temporary_blocks.items())
            },
            "unit_tasks": {
                alias: _safe_task(task)
                for unit_id, task in self.unit_tasks.items()
                if (alias := _persisted_task_key(unit_id))
            },
            "manual_assignments": _safe_manual_assignments(self.manual_assignments),
            "scheduler_assignments": _safe_scheduler_assignments(self.scheduler_assignments),
            "policy_state": _safe_policy_state(self.policy_state),
            "objective_states": _safe_objective_states(self.objective_states),
            "processed_event_ids": [
                safe
                for value in self.processed_event_ids
                if (safe := _safe_event_id(value))
            ],
            "event_counts": _safe_event_counts(self.event_counts),
            "submitted_ticks": self.submitted_ticks,
            "accepted_ticks": self.accepted_ticks,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AgentMemory":
        if not isinstance(data, dict):
            return cls()
        if data.get("version") not in (1, 2, MEMORY_VERSION):
            return cls()
        def integer(name: str, default: int = 0) -> int:
            try:
                return int(data.get(name, default))
            except (TypeError, ValueError):
                return default
        try:
            mode = StrategicMode(data.get("last_mode", StrategicMode.RESPAWN.value))
        except (TypeError, ValueError):
            mode = StrategicMode.RESPAWN
        tasks = data.get("unit_tasks", {})
        events = data.get("processed_event_ids", [])
        counts = data.get("event_counts", {})
        return cls(
                version=MEMORY_VERSION,
                last_tick=integer("last_tick"),
                last_mode=mode,
                mode_since_tick=integer("mode_since_tick"),
                no_resource_ticks=integer("no_resource_ticks"),
                obstacles=_safe_cells(data.get("obstacles", [])),
                explored=_safe_cells(data.get("explored", [])),
                resource_observations=_safe_cell_map(data.get("resource_observations", {})),
                temporary_blocks=_safe_cell_map(data.get("temporary_blocks", {})),
                unit_tasks={
                    alias: _safe_task(task)
                    for unit_id, task in (tasks.items() if isinstance(tasks, dict) else ())
                    if (alias := _persisted_task_key(str(unit_id)))
                },
                manual_assignments=_safe_manual_assignments(data.get("manual_assignments", {})),
                scheduler_assignments=_safe_scheduler_assignments(data.get("scheduler_assignments", {})),
                policy_state=_safe_policy_state(data.get("policy_state", {})),
                objective_states=_safe_objective_states(data.get("objective_states", {})),
                processed_event_ids=[
                    safe
                    for value in (events if isinstance(events, list) else [])
                    if (safe := _safe_event_id(value))
                ],
                event_counts=_safe_event_counts(counts),
                submitted_ticks=integer("submitted_ticks"),
                accepted_ticks=integer("accepted_ticks"),
            )


class MemoryStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> AgentMemory:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except (FileNotFoundError, OSError):
            return AgentMemory()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            recovered = AgentMemory(obstacles=self._salvage_obstacles(raw))
            self._quarantine()
            return recovered
        return AgentMemory.from_dict(data)

    def _quarantine(self) -> None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        corrupt = self.path.with_name(f"{self.path.name}.corrupt-{stamp}")
        try:
            os.replace(self.path, corrupt)
        except OSError:
            pass

    @staticmethod
    def _salvage_obstacles(raw: str) -> set[Position]:
        match = re.search(r'"obstacles"\s*:\s*', raw)
        if match is None:
            return set()
        try:
            value, _ = json.JSONDecoder().raw_decode(raw[match.end():])
        except (json.JSONDecodeError, TypeError):
            return set()
        return _safe_cells(value)

    def save(self, memory: AgentMemory) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        payload = json.dumps(
            memory.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        temporary.write_text(payload + "\n", encoding="utf-8")
        os.replace(temporary, self.path)
