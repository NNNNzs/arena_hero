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

_EXPLORATION_SECTORS = ((1, 0), (0, 1), (-1, 0), (0, -1))


def _combat_target(
    core: CoreView, index: int, tick: int, minimum_radius: int,
    maximum_radius: int, rotation_ticks: int,
) -> Position:
    """Return a deterministic, distinct sector slot around the current Core."""
    sector = (index + tick // max(1, rotation_ticks)) % len(_EXPLORATION_SECTORS)
    radius = minimum_radius + (index // len(_EXPLORATION_SECTORS)) % max(1, maximum_radius - minimum_radius + 1)
    dx, dy = _EXPLORATION_SECTORS[sector]
    lateral = ((index // len(_EXPLORATION_SECTORS)) % 2) * 2 - 1
    return (core.position[0] + dx * radius, core.position[1] + lateral) if dx else (core.position[0] + lateral, core.position[1] + dy * radius)


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
        if immediate or (approaching and enemy_distance <= config.intercept_distance):
            candidates.append((0 if immediate else 1, enemy_distance, enemy.id.bytes, enemy))
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
    score -= distance(ranger.position, enemy.position) * 5.0
    return score


def vanguard_cell_score(
    enemies: Iterable[CoreView | UnitView],
) -> float:
    return sum(100.0 if isinstance(enemy, CoreView) else 10.0 for enemy in enemies)



