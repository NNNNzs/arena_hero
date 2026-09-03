"""Shared strategy planning primitives."""

from __future__ import annotations

from time import perf_counter
from typing import Iterable
from uuid import UUID

from arena_hero import CoreState, CoreView, Direction, UnitType, UnitView

from ..context import DecisionContext
from ..memory import AgentMemory
from ..models import ActionIntent, ActionKind, AgentConfig, Position, ReservationTable
from ..navigation import DIRECTIONS, bounded_path_cost, destination, distance, enemy_threat_cells, plan_step
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
    task = dict(existing) if existing.get("kind") == kind else {
        key: existing[key]
        for key in ("patrol_arc", "patrol_role", "patrol_core")
        if key in existing
    }
    task.update({"kind": kind, "target": list(target)})
    task["prev_cell"] = list(unit.position)
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
    passable_r1 = [
        (x + dx, y + dy)
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1))
        if (x + dx, y + dy) not in memory.obstacles
        and (x + dx, y + dy) not in context.enemy_occupancy
    ]
    # A Core with at most two exits is a chokepoint.  Reserve its first three
    # rings exclusively for Workers entering with cargo and leaving to mine;
    # guard posts begin on the outer defensive ring instead.
    radii = (4, 5, 6) if len(passable_r1) <= 2 else (1, 2, 3)
    candidates = [
        (x + dx, y + dy)
        for radius in radii
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


def _unit_needs_retreat_heal(
    unit: UnitView, memory: AgentMemory, config: AgentConfig
) -> bool:
    """Apply low-HP entry and higher-HP exit thresholds for healing retreat."""
    unit_id = str(unit.id)
    maximum = UNIT_MAX_HP[unit.unit_type]
    if unit_id in memory.retreating_unit_ids:
        if unit.hp >= maximum * config.unit_retreat_heal_return_ratio:
            memory.retreating_unit_ids.discard(unit_id)
            return False
        return True
    if unit.hp < maximum * config.unit_retreat_heal_ratio:
        memory.retreating_unit_ids.add(unit_id)
        return True
    return False


def _retreat_threats_are_dense(
    unit: UnitView, target: Position, context: DecisionContext
) -> bool:
    """Use only visible current-Turn enemies to reject a suicidal route home."""
    toward_core = [
        enemy
        for enemy in context.enemies
        if distance(enemy.position, unit.position) <= 3
        and distance(enemy.position, target) < distance(unit.position, target)
    ]
    # One nearby shooter can make a route inconvenient; two converging visible
    # enemies (or a point-blank attacker) make an unsafe path a likely kill.
    return len(toward_core) >= 2 or any(
        distance(enemy.position, unit.position) <= 2 for enemy in toward_core
    )


def _retreat_shelter_intent(
    unit: UnitView,
    target: Position,
    context: DecisionContext,
    memory: AgentMemory,
    reservations: ReservationTable,
) -> ActionIntent:
    """Take one visible, unthreatened step away from concentrated attackers."""
    threats = enemy_threat_cells(context)
    blocked = (
        memory.obstacles
        | memory.active_temporary_blocks(context.tick)
        | set(context.obstacle_cells)
        | set(context.enemy_occupancy)
        | threats
    )
    candidates = [
        (destination(unit.position, direction), direction)
        for direction in DIRECTIONS
        if destination(unit.position, direction) not in blocked
    ]
    if not candidates:
        return _wait(unit, "unit_retreat_to_core_heal_shelter")
    cell, direction = min(
        candidates,
        key=lambda item: (
            -min(distance(item[0], enemy.position) for enemy in context.enemies),
            distance(item[0], target),
            item[1].value,
        ),
    )
    if not reservations.reserve(cell, source=unit.position):
        return _wait(unit, "unit_retreat_to_core_heal_shelter")
    return ActionIntent(
        actor_id=unit.id,
        is_core=False,
        action=ActionKind.MOVE,
        score=760,
        reason="unit_retreat_to_core_heal_shelter",
        target_cell=cell,
        direction=direction,
        reserved_cell=cell,
    )


def _distant_retreat_fallback_intent(
    unit: UnitView,
    target: Position,
    context: DecisionContext,
    memory: AgentMemory,
    reservations: ReservationTable,
    reason: str,
) -> ActionIntent | None:
    """Make safe local progress when a fog-distance route home cannot be planned."""
    blocked = (
        memory.obstacles
        | memory.active_temporary_blocks(context.tick)
        | set(context.obstacle_cells)
        | set(context.enemy_occupancy)
    )
    threats = enemy_threat_cells(context)
    previous = memory.unit_tasks.get(str(unit.id), {}).get("prev_cell")
    previous_cell = tuple(previous) if isinstance(previous, (list, tuple)) and len(previous) == 2 else None
    candidates = [
        (destination(unit.position, direction), direction)
        for direction in DIRECTIONS
        if destination(unit.position, direction) not in blocked
    ]
    # Prefer an unthreatened cell, then one that still trends home.  The strong
    # anti-backtrack penalty prevents a blocked long-haul route becoming a
    # two-cell oscillation while new terrain is revealed.
    candidates.sort(key=lambda item: (
        item[0] in threats,
        1 if previous_cell is not None and item[0] == previous_cell else 0,
        distance(item[0], target),
        item[1].value,
    ))
    for cell, direction in candidates:
        if reservations.reserve(cell, source=unit.position):
            memory.unit_tasks[str(unit.id)] = {
                "kind": "distant_retreat_fallback",
                "target": list(target),
                "prev_cell": list(unit.position),
                "step": list(cell),
                "attempt_tick": context.tick,
            }
            return ActionIntent(
                actor_id=unit.id,
                is_core=False,
                action=ActionKind.MOVE,
                score=755,
                reason=reason + "_distant_fallback",
                target_cell=cell,
                direction=direction,
                reserved_cell=cell,
            )
    return None


def _unit_retreat_to_core(
    unit: UnitView,
    context: DecisionContext,
    memory: AgentMemory,
    reservations: ReservationTable,
    deadline: float,
    config: AgentConfig,
) -> ActionIntent | None:
    """Plan a safe current-Turn step home for a low-HP Unit."""
    if context.core is None:
        return None
    target = (
        context.core.destination
        if context.core.state is CoreState.MOVING and context.core.destination
        else context.core.position
    )
    intent = _move(
        unit,
        target,
        "unit_retreat_to_core_heal",
        790,
        context=context,
        memory=memory,
        reservations=reservations,
        deadline=deadline,
        config=config,
        avoid_threats=True,
    )
    if intent is not None:
        return intent
    if _retreat_threats_are_dense(unit, target, context):
        return _retreat_shelter_intent(
            unit, target, context, memory, reservations
        )
    intent = _move(
        unit,
        target,
        "unit_retreat_to_core_heal_unsafe_fallback",
        760,
        context=context,
        memory=memory,
        reservations=reservations,
        deadline=deadline,
        config=config,
        avoid_threats=False,
    )
    if intent is not None:
        return intent
    if distance(unit.position, target) >= config.long_distance_retreat_threshold:
        return _distant_retreat_fallback_intent(
            unit, target, context, memory, reservations,
            "unit_retreat_to_core_heal",
        )
    return None


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
    intent = _move(
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
    if intent is not None:
        return intent
    if distance(unit.position, target) >= config.long_distance_retreat_threshold:
        return _distant_retreat_fallback_intent(
            unit, target, context, memory, reservations, reason,
        )
    return None


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
    """Reserve a local approach cell after the direct return step is contested."""
    current_distance = distance(unit.position, target)
    if current_distance <= minimum_distance:
        return None
    blocked = (
        memory.obstacles
        | memory.active_temporary_blocks(context.tick)
        | set(context.enemy_occupancy)
    )
    existing_task = memory.unit_tasks.get(str(unit.id), {})
    prev_cell_raw = existing_task.get("prev_cell")
    prev_cell = tuple(prev_cell_raw) if isinstance(prev_cell_raw, (list, tuple)) and len(prev_cell_raw) == 2 else None

    scored_candidates = []
    for direction in DIRECTIONS:
        cand = destination(unit.position, direction)
        if cand in blocked:
            continue
        dist = distance(cand, target)
        if dist > current_distance:
            continue
        score = dist * 10
        if prev_cell is not None and cand == prev_cell:
            score += 5000  # heavy penalty to prevent 2-cell ping-pong oscillation
        scored_candidates.append((score, direction, cand))

    scored_candidates.sort(key=lambda x: (x[0], x[1].value))
    for _, direction, cell in scored_candidates:
        if reservations.reserve(cell, source=unit.position):
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


def _deploy_sidestep(
    unit: UnitView,
    target: Position,
    context: DecisionContext,
    memory: AgentMemory,
    reservations: ReservationTable,
    reason: str,
    core_position: Position | None,
) -> ActionIntent | None:
    """Reserve a lateral or outward step when direct deployment route is congested near Core."""
    blocked = (
        memory.obstacles
        | memory.active_temporary_blocks(context.tick)
        | set(context.obstacle_cells)
        | set(context.enemy_occupancy)
    )
    current_dist_to_target = distance(unit.position, target)
    current_dist_to_core = distance(unit.position, core_position) if core_position is not None else 0

    existing_task = memory.unit_tasks.get(str(unit.id), {})
    prev_cell_raw = existing_task.get("prev_cell")
    prev_cell = tuple(prev_cell_raw) if isinstance(prev_cell_raw, (list, tuple)) and len(prev_cell_raw) == 2 else None

    has_carrying_workers = any(w.cargo for w in context.workers)

    candidates = []
    for direction in DIRECTIONS:
        cand = destination(unit.position, direction)
        if cand in blocked:
            continue
        # Never step INTO the core cell during deploy sidestep
        if core_position is not None and cand == core_position:
            continue

        dist_to_target = distance(cand, target)
        dist_to_core = distance(cand, core_position) if core_position is not None else 0
        if dist_to_target <= current_dist_to_target:
            score = dist_to_target * 10 - dist_to_core
        elif core_position is not None and dist_to_core >= current_dist_to_core:
            score = 1000 + dist_to_target * 10 - dist_to_core
        else:
            score = 2000 + dist_to_target * 10

        # Prioritize outward movement to clear doorstep for workers with cargo
        if has_carrying_workers and current_dist_to_core <= 2 and dist_to_core > current_dist_to_core:
            score -= 500

        # Prevent immediate 2-cell ping-pong oscillation
        if prev_cell is not None and cand == prev_cell:
            score += 5000

        candidates.append((score, direction, cand))

    candidates.sort(key=lambda x: (x[0], x[1].value))
    for _, direction, cand in candidates:
        if reservations.reserve(cand, source=unit.position):
            return ActionIntent(
                unit.id,
                False,
                ActionKind.MOVE,
                380,
                reason + "_sidestep",
                target_cell=target,
                direction=direction,
                reserved_cell=cand,
            )
    return None


def _evict_combat_from_core_for_cargo(
    intents: list[ActionIntent],
    context: DecisionContext,
    memory: AgentMemory,
    reservations: ReservationTable,
    config: AgentConfig,
) -> list[ActionIntent]:
    """Replace WAIT intents for combat units on the core cell with yield moves.

    When a combat unit (RANGER/VANGUARD) stands on the core position and its
    planned action is WAIT (from any branch: expedition contact hold, guard
    route blocked, healing-waits-for-resources, holding defense ring, etc.),
    it blocks cargo workers from entering the core to deposit resources.

    This post-processing step catches ALL such WAIT-on-core cases that the
    in-tree ``_yield_cargo_delivery`` call cannot reach.
    """
    if context.core is None:
        return intents
    core_position = context.core.position
    combat_ids = {unit.id for unit in (*context.rangers, *context.vanguards)}
    changed = False
    result: list[ActionIntent] = []
    for intent in intents:
        if (
            intent.action is ActionKind.WAIT
            and intent.actor_id in combat_ids
        ):
            unit = context.current_objects.get(intent.actor_id)
            if (
                isinstance(unit, UnitView)
                and unit.position == core_position
            ):
                yield_intent = _yield_cargo_delivery(
                    unit, context, memory, reservations, config
                )
                if yield_intent is not None:
                    _record_unit_task(
                        memory, context, unit,
                        kind="yield_cargo_delivery",
                        target=yield_intent.target_cell or unit.position,
                        intent=yield_intent,
                    )
                    result.append(yield_intent)
                    changed = True
                    continue
        result.append(intent)
    return result


def _yield_cargo_delivery(
    unit: UnitView,
    context: DecisionContext,
    memory: AgentMemory,
    reservations: ReservationTable,
    config: AgentConfig,
) -> ActionIntent | None:
    """Move a nearby combat Unit outward before a cargo route is blocked.

    Workers are planned first, so their reservations expose the immediate
    delivery pressure.  This deliberately applies only outside an urgent
    combat/intercept branch and across the configurable Core approach ring.
    The wider ring prevents a priority inversion where a cargo Worker waits
    outside the old three-cell throat while a stationary relay Unit still
    occupies the next cell in a one-cell corridor.
    """
    if context.core is None:
        return None
    core_position = context.core.position
    unit_distance = distance(unit.position, core_position)
    if not 0 <= unit_distance <= config.cargo_delivery_yield_radius:
        return None
    cargo_workers = tuple(
        worker for worker in context.workers
        if (
            worker.cargo
            and distance(worker.position, core_position)
            <= config.cargo_delivery_yield_radius
        )
    )
    if not cargo_workers:
        return None

    x, y = unit.position
    core_x, core_y = core_position
    if unit_distance == 0:
        # A combat Unit sharing the Core occupies its only extra slot.  Move in
        # the direction opposite the nearest returning Worker so that worker
        # can enter and deposit on the following Tick.
        nearest_cargo = min(
            cargo_workers,
            key=lambda worker: (distance(worker.position, core_position), worker.id.bytes),
        )
        cargo_x, cargo_y = nearest_cargo.position
        x, y = core_x * 2 - cargo_x, core_y * 2 - cargo_y
    outward_target = (
        core_x + (1 if x > core_x else -1 if x < core_x else 0) * 6,
        core_y + (1 if y > core_y else -1 if y < core_y else 0) * 6,
    )
    return _deploy_sidestep(
        unit,
        outward_target,
        context,
        memory,
        reservations,
        "yield_cargo_delivery",
        core_position,
    )


def _evacuate_doorstep_intent(
    unit: UnitView,
    context: DecisionContext,
    memory: AgentMemory,
    reservations: ReservationTable,
    reason: str = "evacuate_doorstep_for_delivery",
    *,
    max_radius: int = 1,
) -> ActionIntent | None:
    """If a Unit is about to idle on or near a Core doorstep/chokepoint exit, step outward."""
    if context.core is None:
        return None
    core_pos = context.core.position
    passable_exits = [
        (core_pos[0] + dx, core_pos[1] + dy)
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1))
        if (core_pos[0] + dx, core_pos[1] + dy) not in memory.obstacles
        and (core_pos[0] + dx, core_pos[1] + dy) not in context.enemy_occupancy
    ]
    dist = distance(unit.position, core_pos)
    if dist > max_radius:
        return None
    if max_radius <= 1 and unit.position not in passable_exits:
        return None

    threats = enemy_threat_cells(context)
    candidates: list[tuple[int, int, int, Position, Direction]] = []
    for direction_idx, direction in enumerate(DIRECTIONS):
        cand = destination(unit.position, direction)
        if (
            cand == core_pos
            or cand in memory.obstacles
            or cand in memory.active_temporary_blocks(context.tick)
            or cand in context.enemy_occupancy
            or cand in threats
        ):
            continue
        outward_penalty = 0 if distance(cand, core_pos) > distance(unit.position, core_pos) else 1
        occupancy = len(context.friendly_occupancy.get(cand, ()))
        candidates.append((outward_penalty, occupancy, direction_idx, cand, direction))

    candidates.sort()
    for _, _, _, cand, direction in candidates:
        if reservations.reserve(cand, source=unit.position):
            return ActionIntent(
                actor_id=unit.id,
                is_core=False,
                action=ActionKind.MOVE,
                score=410,
                reason=reason,
                direction=direction,
                target_cell=cand,
                reserved_cell=cand,
            )
    return None

