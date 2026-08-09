"""Build an immutable tactical snapshot from one authoritative Turn."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping
from uuid import UUID

from arena_hero import (
    ChampionBeacon,
    CoreView,
    ResolutionEvent,
    Turn,
    UnitType,
    UnitView,
)

from .models import Position


_VISION_RADIUS = {
    "CORE": 5,
    UnitType.WORKER: 3,
    UnitType.VANGUARD: 4,
    UnitType.RANGER: 5,
}


def _supercover_line(start: Position, end: Position) -> tuple[Position, ...]:
    """Enumerate every grid cell touched by the center-to-center line."""
    x, y = start
    dx, dy = end[0] - x, end[1] - y
    nx, ny = abs(dx), abs(dy)
    sign_x = 0 if dx == 0 else (1 if dx > 0 else -1)
    sign_y = 0 if dy == 0 else (1 if dy > 0 else -1)
    ix = iy = 0
    cells = [start]
    while ix < nx or iy < ny:
        decision = (1 + 2 * ix) * ny - (1 + 2 * iy) * nx
        if decision == 0:
            # A corner crossing touches both side cells before the diagonal.
            side_x = (x + sign_x, y)
            side_y = (x, y + sign_y)
            cells.extend((side_x, side_y))
            x += sign_x
            y += sign_y
            ix += 1
            iy += 1
        elif decision < 0:
            x += sign_x
            ix += 1
        else:
            y += sign_y
            iy += 1
        cell = (x, y)
        if cells[-1] != cell:
            cells.append(cell)
    return tuple(dict.fromkeys(cells))


def _visible_cells(
    origins: tuple[tuple[Position, int], ...], obstacles: set[Position]
) -> set[Position]:
    visible: set[Position] = set()
    for origin, radius in origins:
        for dx in range(-radius, radius + 1):
            for dy in range(-radius, radius + 1):
                if abs(dx) + abs(dy) > radius:
                    continue
                target = origin[0] + dx, origin[1] + dy
                line = _supercover_line(origin, target)
                if any(cell in obstacles for cell in line[1:-1]):
                    continue
                visible.add(target)
    return visible


@dataclass(frozen=True, slots=True)
class DecisionContext:
    tick: int
    resources: int
    resource_capacity: int
    resource_space: int
    population: int
    core: CoreView | None
    units: tuple[UnitView, ...]
    workers: tuple[UnitView, ...]
    vanguards: tuple[UnitView, ...]
    rangers: tuple[UnitView, ...]
    enemies: tuple[CoreView | UnitView, ...]
    resource_cells: frozenset[Position]
    obstacle_cells: frozenset[Position]
    beacon: ChampionBeacon
    friendly_occupancy: Mapping[Position, tuple[UUID, ...]]
    enemy_occupancy: Mapping[Position, tuple[UUID, ...]]
    current_objects: Mapping[UUID, CoreView | UnitView]
    current_enemies: Mapping[UUID, CoreView | UnitView]
    observed_cells: frozenset[Position]
    events: tuple[ResolutionEvent, ...]

    @classmethod
    def from_turn(cls, turn: Turn) -> "DecisionContext":
        core = turn.core.view if turn.core is not None else None
        units = tuple(unit.view for unit in turn.units)
        workers = tuple(
            unit for unit in units if unit.unit_type is UnitType.WORKER
        )
        vanguards = tuple(
            unit for unit in units if unit.unit_type is UnitType.VANGUARD
        )
        rangers = tuple(
            unit for unit in units if unit.unit_type is UnitType.RANGER
        )
        enemies = tuple(turn.visible_enemies)

        friendly: dict[Position, list[UUID]] = {}
        objects: dict[UUID, CoreView | UnitView] = {}
        if core is not None:
            objects[core.id] = core
            friendly.setdefault(core.position, []).append(core.id)
        for unit in units:
            objects[unit.id] = unit
            friendly.setdefault(unit.position, []).append(unit.id)

        enemy_occupancy: dict[Position, list[UUID]] = {}
        current_enemies: dict[UUID, CoreView | UnitView] = {}
        for enemy in enemies:
            current_enemies[enemy.id] = enemy
            enemy_occupancy.setdefault(enemy.position, []).append(enemy.id)

        # Empty terrain cells are omitted by the protocol, so reconstruct the
        # exact Manhattan-radius union with integer-supercover obstacle blocking.
        vision_origins = [
            (unit.position, _VISION_RADIUS[unit.unit_type]) for unit in units
        ]
        if core is not None:
            vision_origins.append((core.position, _VISION_RADIUS["CORE"]))
        observed = _visible_cells(tuple(vision_origins), set(turn.obstacle_cells))

        return cls(
            tick=turn.tick,
            resources=turn.resources,
            resource_capacity=turn.resource_capacity,
            resource_space=turn.resource_space,
            population=turn.state.population,
            core=core,
            units=units,
            workers=workers,
            vanguards=vanguards,
            rangers=rangers,
            enemies=enemies,
            resource_cells=frozenset(turn.resource_cells),
            obstacle_cells=frozenset(turn.obstacle_cells),
            beacon=turn.beacon,
            friendly_occupancy=MappingProxyType(
                {cell: tuple(ids) for cell, ids in friendly.items()}
            ),
            enemy_occupancy=MappingProxyType(
                {cell: tuple(ids) for cell, ids in enemy_occupancy.items()}
            ),
            current_objects=MappingProxyType(objects),
            current_enemies=MappingProxyType(current_enemies),
            observed_cells=frozenset(observed),
            events=tuple(turn.events),
        )
