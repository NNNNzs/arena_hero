"""Combat target selection and scoring."""

from __future__ import annotations

from typing import Iterable
from uuid import UUID

from arena_hero import CoreView, UnitType, UnitView

from ..context import DecisionContext
from ..identity import entity_alias
from ..memory import AgentMemory
from ..models import AgentConfig, Position
from ..navigation import distance, shot_range
from ..tactical_geometry import shadow_fire_advantage

_PATROL_ARCS = ((1, 0), (0, 1), (-1, 0), (0, -1))


def _combat_target(
    core: CoreView,
    unit: UnitView,
    index: int,
    roster_size: int,
    memory: AgentMemory,
    config: AgentConfig,
    *,
    role: str,
) -> Position:
    """Return a locked clockwise ring waypoint for one stable patrol member."""
    task = memory.unit_tasks.get(str(unit.id), {})
    minimum_radius = config.patrol_radius_min if role == "patrol" else config.hunter_radius_min
    configured_max = config.patrol_radius_max if role == "patrol" else config.hunter_radius_max
    # Peripheral patrols deliberately operate beyond Core vision.  Mobile
    # intercept rosters react to threats that enter current friendly vision;
    # constraining this ring to overlap Core vision makes guards clog a narrow
    # mining exit.
    maximum_radius = max(minimum_radius, configured_max)
    radius = min(
        maximum_radius,
        minimum_radius + max(0, roster_size - 1) // max(1, config.patrol_radius_units_per_step),
    )
    prior_arc = task.get("patrol_arc") if task.get("patrol_role") == role else None
    arc = int(prior_arc) % len(_PATROL_ARCS) if type(prior_arc) is int else index % len(_PATROL_ARCS)
    dx, dy = _PATROL_ARCS[arc]
    target = (core.position[0] + dx * radius, core.position[1] + dy * radius)
    locked = task.get("target")
    if (
        task.get("kind") == role
        and task.get("patrol_core") == list(core.position)
        and isinstance(locked, list)
        and len(locked) == 2
        and tuple(locked) not in memory.obstacles
        and unit.position != tuple(locked)
    ):
        target = int(locked[0]), int(locked[1])
    elif unit.position == target or target in memory.obstacles:
        for advance in range(1, len(_PATROL_ARCS) + 1):
            candidate_arc = (arc + advance) % len(_PATROL_ARCS)
            next_dx, next_dy = _PATROL_ARCS[candidate_arc]
            candidate = (core.position[0] + next_dx * radius, core.position[1] + next_dy * radius)
            if candidate not in memory.obstacles:
                arc, target = candidate_arc, candidate
                break
    task = dict(task)
    task.update({"patrol_arc": arc, "patrol_role": role, "patrol_core": list(core.position)})
    memory.unit_tasks[str(unit.id)] = task
    return target


def _escort_assignment(
    unit: UnitView,
    combat_units: Iterable[UnitView],
    workers: Iterable[UnitView],
    core: CoreView,
) -> tuple[UnitView, Position] | None:
    """Assign combat escorts across workers and three non-overlapping slots."""
    worker_roster = tuple(sorted(workers, key=lambda item: item.id.bytes))
    combat_roster = tuple(sorted(combat_units, key=lambda item: item.id.bytes))
    if not worker_roster or unit not in combat_roster:
        return None
    rank = combat_roster.index(unit)
    if rank >= len(worker_roster) * 3:
        return None
    worker = worker_roster[rank % len(worker_roster)]
    slot_index = rank // len(worker_roster)
    wx, wy = worker.position
    cx, cy = core.position
    dx, dy = wx - cx, wy - cy
    if abs(dx) >= abs(dy):
        forward = (1 if dx >= 0 else -1, 0)
    else:
        forward = (0, 1 if dy >= 0 else -1)
    left = (forward[1], -forward[0])
    offsets = (forward, left, (-left[0], -left[1]))
    ox, oy = offsets[slot_index]
    return worker, (wx + ox, wy + oy)


def _enemy_can_attack_core(enemy: CoreView | UnitView, core: CoreView, obstacles: Iterable[Position]) -> bool:
    if not isinstance(enemy, UnitView):
        return False
    if enemy.unit_type is UnitType.VANGUARD:
        return distance(enemy.position, core.position) == 1
    return enemy.unit_type is UnitType.RANGER and shot_range(enemy.position, core.position, obstacles) is not None


def _intercept_target(context: DecisionContext, memory: AgentMemory, config: AgentConfig) -> CoreView | UnitView | None:
    if context.core is None:
        return None
    candidates: list[tuple[int, int, bytes, CoreView | UnitView]] = []
    for enemy in context.enemies:
        immediate = _enemy_can_attack_core(enemy, context.core, memory.obstacles)
        track = memory.enemy_tracks.get(entity_alias(enemy.id) or "", {})
        approaching = int(track.get("approach_streak", 0)) >= config.intercept_approach_streak
        enemy_distance = distance(enemy.position, context.core.position)
        intruder = enemy_distance < config.intercept_distance
        if immediate or intruder or (approaching and enemy_distance <= config.intercept_distance):
            priority = 0 if immediate else (1 if intruder else 2)
            candidates.append((priority, enemy_distance, enemy.id.bytes, enemy))
    return min(candidates, default=(0, 0, b"", None))[-1]


def _combat_rosters(context: DecisionContext, memory: AgentMemory, config: AgentConfig) -> tuple[set[UUID], set[UUID], set[UUID], set[UUID]]:
    """Return stable guard, patrol, hunter and temporary intercept rosters."""
    vanguards = tuple(sorted(context.vanguards, key=lambda unit: unit.id.bytes))
    rangers = tuple(sorted(context.rangers, key=lambda unit: unit.id.bytes))
    guards = {unit.id for unit in vanguards[:config.core_guard_vanguards]}
    guard_rangers = {unit.id for unit in rangers[:config.core_guard_rangers]}
    patrol = {unit.id for unit in vanguards if unit.id not in guards}
    hunters = {unit.id for unit in rangers if unit.id not in guard_rangers}
    if _intercept_target(context, memory, config) is None:
        return guards, guard_rangers, patrol, hunters
    return (
        guards, guard_rangers,
        set(sorted(patrol, key=lambda unit_id: unit_id.bytes)[:config.intercept_vanguards]),
        set(sorted(hunters, key=lambda unit_id: unit_id.bytes)[:config.intercept_rangers]),
    )


def ranger_target_score(
    ranger: UnitView,
    enemy: CoreView | UnitView,
    context: DecisionContext,
    memory: AgentMemory,
) -> float:
    score = 100.0 if isinstance(enemy, CoreView) else 0.0
    remaining_health = enemy.hp + (enemy.shield if isinstance(enemy, CoreView) else 0)
    if remaining_health <= 1:
        score += 40.0
    if context.core is not None and _enemy_can_attack_core(
        enemy, context.core, memory.obstacles
    ):
        score += 30.0
    score += shadow_fire_advantage(ranger.position, enemy, memory.obstacles)
    firing_range = shot_range(ranger.position, enemy.position, memory.obstacles)
    score -= (
        firing_range if firing_range is not None
        else distance(ranger.position, enemy.position)
    ) * 5.0
    return score


def vanguard_cell_score(
    enemies: Iterable[CoreView | UnitView],
) -> float:
    return sum(100.0 if isinstance(enemy, CoreView) else 10.0 for enemy in enemies)
