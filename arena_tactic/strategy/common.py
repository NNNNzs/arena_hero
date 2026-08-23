"""Shared strategy planning primitives."""

from __future__ import annotations

from time import perf_counter
from typing import Iterable
from uuid import UUID

from arena_hero import CoreState, CoreView, UnitType, UnitView

from ..context import DecisionContext
from ..memory import AgentMemory
from ..models import ActionIntent, ActionKind, AgentConfig, Position, ReservationTable
from ..navigation import DIRECTIONS, bounded_path_cost, destination, distance, plan_step
from .combat import _enemy_can_attack_core

UNIT_MAX_HP = {
    UnitType.WORKER: 2,
    UnitType.VANGUARD: 4,
    UnitType.RANGER: 2,
}
CORE_MAX_HP = 5
_EXPLORATION_SECTORS = (
    (1, 0),
    (0, 1),
    (-1, 0),
    (0, -1),
)

def _wait(actor: CoreView | UnitView, reason: str, *, is_core: bool = False) -> ActionIntent:
    return ActionIntent(
        actor_id=actor.id,
        is_core=is_core,
        action=ActionKind.WAIT,
        score=0,
        reason=reason,
    )


def _move(
    unit: UnitView,
    target: Position,
    reason: str,
    score: float,
    *,
    context: DecisionContext,
    memory: AgentMemory,
    reservations: ReservationTable,
    deadline: float,
    config: AgentConfig,
    avoid_threats: bool = False,
) -> ActionIntent | None:
    direction = plan_step(
        actor_id=unit.id,
        start=unit.position,
        goal=target,
        context=context,
        persistent_obstacles=(
            memory.obstacles | memory.active_temporary_blocks(context.tick)
        ),
        reservations=reservations,
        deadline=deadline,
        config=config,
        avoid_threats=avoid_threats,
    )
    if direction is None:
        return None
    reserved_cell = destination(unit.position, direction)
    return ActionIntent(
        actor_id=unit.id,
        is_core=False,
        action=ActionKind.MOVE,
        score=score,
        reason=reason,
        target_cell=target,
        direction=direction,
        reserved_cell=reserved_cell,
    )



def _record_unit_task(
    memory: AgentMemory,
    context: DecisionContext,
    unit: UnitView,
    *,
    kind: str,
    target: Position,
    intent: ActionIntent | None,
) -> None:
    existing = memory.unit_tasks.get(str(unit.id), {})
    task = dict(existing) if existing.get("kind") == kind else {}
    task.update({"kind": kind, "target": list(target)})
    if intent is not None and intent.action is ActionKind.MOVE:
        task["step"] = list(intent.reserved_cell) if intent.reserved_cell else None
        task["attempt_tick"] = context.tick
    else:
        task.pop("step", None)
        task.pop("attempt_tick", None)
    memory.unit_tasks[str(unit.id)] = task


def _guard_slots(context: DecisionContext, memory: AgentMemory) -> list[Position]:
    if context.core is None:
        return []
    x, y = context.core.position
    candidates = [
        (x + dx, y + dy)
        for radius in (1, 2, 3)
        for dx in range(-radius, radius + 1)
        for dy in range(-radius, radius + 1)
        if abs(dx) + abs(dy) == radius
    ]
    return [
        cell
        for cell in candidates
        if cell not in memory.obstacles and cell not in context.enemy_occupancy
    ]


def _best_visible_enemy(
    unit: UnitView,
    context: DecisionContext,
    memory: AgentMemory,
) -> CoreView | UnitView | None:
    if not context.enemies:
        return None
    return min(
        context.enemies,
        key=lambda enemy: (
            -(100 if isinstance(enemy, CoreView) else 0),
            -(30
            if context.core is not None
            and _enemy_can_attack_core(enemy, context.core, memory.obstacles)
            else 0),
            distance(unit.position, enemy.position),
            enemy.id.bytes,
        ),
    )


def _unit_heal_intent(unit: UnitView, planned_cost: int) -> ActionIntent:
    return ActionIntent(
        actor_id=unit.id,
        is_core=False,
        action=ActionKind.HEAL,
        score=800,
        reason="damaged_at_stationary_core",
        estimated_cost=planned_cost,
    )


def _at_normal_core(unit: UnitView, context: DecisionContext) -> bool:
    return (
        context.core is not None
        and context.core.state is CoreState.NORMAL
        and unit.position == context.core.position
    )


def _anticipated_resources(context: DecisionContext) -> int:
    """Include cargo that can be deposited before healing and the Core action."""
    core = context.core
    if core is None or core.state is not CoreState.NORMAL:
        return context.resources
    depositable = sum(
        worker.cargo or 0
        for worker in context.workers
        if worker.position == core.position
    )
    return context.resources + min(depositable, context.resource_space)


def _return_to_core(
    unit: UnitView,
    context: DecisionContext,
    memory: AgentMemory,
    reservations: ReservationTable,
    deadline: float,
    config: AgentConfig,
    reason: str,
) -> ActionIntent | None:
    if context.core is None:
        return None
    target = (
        context.core.destination
        if context.core.state is CoreState.MOVING and context.core.destination
        else context.core.position
    )
    # B: 优先走安全路径；若被威胁格封死则降级为普通路径强行回核心
    intent = _move(
        unit,
        target,
        reason,
        700,
        context=context,
        memory=memory,
        reservations=reservations,
        deadline=deadline,
        config=config,
        avoid_threats=True,
    )
    if intent is not None:
        return intent
    # 安全路径不通，降级为忽略威胁的普通路径
    return _move(
        unit,
        target,
        reason + "_unsafe_fallback",
        650,
        context=context,
        memory=memory,
        reservations=reservations,
        deadline=deadline,
        config=config,
        avoid_threats=False,
    )


def _return_to_core_sidestep(
    unit: UnitView,
    target: Position,
    context: DecisionContext,
    memory: AgentMemory,
    reservations: ReservationTable,
    reason: str,
    *,
    minimum_distance: int,
) -> ActionIntent | None:
    """Reserve a local approach cell after the direct return step is contested.

    This is deliberately local: a failed bounded route still waits for the next
    authoritative Turn, while a congested first step can yield to an equally
    useful neighboring approach cell without creating a new long-lived route.
    """
    current_distance = distance(unit.position, target)
    if current_distance <= minimum_distance:
        return None
    blocked = (
        memory.obstacles
        | memory.active_temporary_blocks(context.tick)
        | set(context.enemy_occupancy)
    )
    candidates = sorted(
        (
            (distance(destination(unit.position, direction), target), direction)
            for direction in DIRECTIONS
            if destination(unit.position, direction) not in blocked
            and distance(destination(unit.position, direction), target)
            <= current_distance
        ),
        key=lambda candidate: (candidate[0], candidate[1].value),
    )
    for _, direction in candidates:
        cell = destination(unit.position, direction)
        if reservations.reserve(cell):
            return ActionIntent(
                unit.id,
                False,
                ActionKind.MOVE,
                625,
                reason + "_sidestep",
                target_cell=target,
                direction=direction,
                reserved_cell=cell,
            )
    return None



