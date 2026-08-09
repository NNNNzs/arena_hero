"""Small, versioned, controller-free Agent memory with atomic persistence."""

from __future__ import annotations

import json
import os
from collections import Counter
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .context import DecisionContext
from .models import AgentConfig, Position, StrategicMode


MEMORY_VERSION = 2
_CARDINAL = ((0, -1), (0, 1), (-1, 0), (1, 0))


def _cell_key(cell: Position) -> str:
    return f"{cell[0]},{cell[1]}"


def _parse_cell(value: str) -> Position:
    x, y = value.split(",", 1)
    return int(x), int(y)


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
        next_memory.obstacles.update(context.obstacle_cells)
        next_memory.explored.update(context.observed_cells - context.obstacle_cells)
        next_memory.temporary_blocks = {
            cell: blocked_until
            for cell, blocked_until in next_memory.temporary_blocks.items()
            if blocked_until >= context.tick
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
            event_id = str(event.event_id)
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
            "unit_tasks": self.unit_tasks,
            "processed_event_ids": self.processed_event_ids,
            "event_counts": self.event_counts,
            "submitted_ticks": self.submitted_ticks,
            "accepted_ticks": self.accepted_ticks,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AgentMemory":
        if data.get("version") not in (1, MEMORY_VERSION):
            return cls()
        try:
            return cls(
                version=MEMORY_VERSION,
                last_tick=int(data.get("last_tick", 0)),
                last_mode=StrategicMode(
                    data.get("last_mode", StrategicMode.RESPAWN.value)
                ),
                mode_since_tick=int(data.get("mode_since_tick", 0)),
                no_resource_ticks=int(data.get("no_resource_ticks", 0)),
                obstacles={tuple(cell) for cell in data.get("obstacles", [])},
                explored={tuple(cell) for cell in data.get("explored", [])},
                resource_observations={
                    _parse_cell(cell): int(tick)
                    for cell, tick in data.get(
                        "resource_observations", {}
                    ).items()
                },
                temporary_blocks={
                    _parse_cell(cell): int(blocked_until)
                    for cell, blocked_until in data.get(
                        "temporary_blocks", {}
                    ).items()
                },
                unit_tasks=dict(data.get("unit_tasks", {})),
                processed_event_ids=list(data.get("processed_event_ids", [])),
                event_counts=dict(data.get("event_counts", {})),
                submitted_ticks=int(data.get("submitted_ticks", 0)),
                accepted_ticks=int(data.get("accepted_ticks", 0)),
            )
        except (KeyError, TypeError, ValueError):
            return cls()


class MemoryStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> AgentMemory:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return AgentMemory()
        return AgentMemory.from_dict(data)

    def save(self, memory: AgentMemory) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        payload = json.dumps(
            memory.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        temporary.write_text(payload + "\n", encoding="utf-8")
        os.replace(temporary, self.path)
