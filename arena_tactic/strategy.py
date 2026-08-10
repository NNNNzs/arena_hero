"""Pure strategic mode selection and controller-independent intent planning."""

from __future__ import annotations

from collections import Counter
from time import perf_counter
from typing import Iterable
from uuid import UUID

from arena_hero import (
    BeaconStatus,
    CoreState,
    CoreView,
    Direction,
    UnitType,
    UnitView,
    unit_cost,
)

from .context import DecisionContext
from .memory import AgentMemory
from .models import (
    ActionIntent,
    ActionKind,
    AgentConfig,
    Position,
    ReservationTable,
    StrategicMode,
)
from .navigation import (
    DIRECTIONS,
    bounded_path_cost,
    destination,
    distance,
    plan_step,
    shot_range,
)


UNIT_MAX_HP = {
    UnitType.WORKER: 2,
    UnitType.VANGUARD: 4,
    UnitType.RANGER: 2,
}
CORE_MAX_HP = 5
_EXPLORATION_SECTORS = (
    (1, 0),   # east
    (0, 1),   # south
    (-1, 0),  # west
    (0, -1),  # north
)


def _enemy_can_attack_core(
    enemy: CoreView | UnitView,
    core: CoreView,
    obstacles: Iterable[Position],
) -> bool:
    if not isinstance(enemy, UnitView):
        return False
    if enemy.unit_type is UnitType.VANGUARD:
        return distance(enemy.position, core.position) == 1
    if enemy.unit_type is UnitType.RANGER:
        return shot_range(enemy.position, core.position, obstacles) is not None
    return False


def _pressure_distance(context: DecisionContext) -> int | None:
    if context.core is None or not context.enemies:
        return None
    return min(distance(enemy.position, context.core.position) for enemy in context.enemies)


def _beacon_owned(context: DecisionContext) -> bool:
    return context.beacon.carrier_id in context.current_objects


def choose_mode(
    context: DecisionContext,
    memory: AgentMemory,
    config: AgentConfig,
) -> StrategicMode:
    """Select a mode with hard safety overrides and small exit hysteresis."""
    core = context.core
    if core is None:
        return StrategicMode.RESPAWN
    if core.state is CoreState.MOVING:
        return StrategicMode.RECOVER

    pressure = _pressure_distance(context)
    immediate_threat = any(
        _enemy_can_attack_core(enemy, core, memory.obstacles)
        for enemy in context.enemies
    )
    if immediate_threat or (
        pressure is not None and pressure <= config.defense_enter_distance
    ):
        return StrategicMode.DEFEND
    if (
        memory.last_mode is StrategicMode.DEFEND
        and pressure is not None
        and pressure <= config.defense_exit_distance
    ):
        return StrategicMode.DEFEND

    damaged_at_core = any(
        unit.position == core.position and unit.hp < UNIT_MAX_HP[unit.unit_type]
        for unit in context.units
    )
    if core.hp < CORE_MAX_HP or core.shield < 3 or damaged_at_core:
        return StrategicMode.RECOVER

    combat_count = len(context.vanguards) + len(context.rangers)
    enemy_core_visible = any(isinstance(enemy, CoreView) for enemy in context.enemies)
    attack_enter = (
        enemy_core_visible
        and combat_count >= 3
        and core.hp == CORE_MAX_HP
        and core.shield >= 3
    )
    attack_stay = (
        memory.last_mode is StrategicMode.ATTACK
        and enemy_core_visible
        and combat_count >= 2
        and core.hp == CORE_MAX_HP
        and core.shield >= 2
    )
    if attack_enter or attack_stay:
        return StrategicMode.ATTACK

    beacon_available = (
        not _beacon_owned(context)
        and context.beacon.status in (None, BeaconStatus.GROUND)
    )
    beacon_enter = (
        beacon_available
        and core.hp == CORE_MAX_HP
        and core.shield >= 3
        and context.population >= 6
    )
    beacon_stay = (
        memory.last_mode is StrategicMode.BEACON
        and beacon_available
        and core.hp == CORE_MAX_HP
        and core.shield >= 2
    )
    if beacon_enter or beacon_stay:
        return StrategicMode.BEACON

    early_roster_ready = (
        len(context.workers) >= config.early_workers
        and len(context.vanguards) >= config.early_vanguards
        and len(context.rangers) >= config.early_rangers
    )
    if (
        context.resource_cells
        or any((worker.cargo or 0) > 0 for worker in context.workers)
        or not early_roster_ready
    ):
        return StrategicMode.ECONOMY
    return StrategicMode.EXPLORE


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
    score -= distance(ranger.position, enemy.position) * 5.0
    return score


def vanguard_cell_score(
    enemies: Iterable[CoreView | UnitView],
) -> float:
    return sum(100.0 if isinstance(enemy, CoreView) else 10.0 for enemy in enemies)


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


def _assign_unique_targets(
    units: Iterable[UnitView],
    targets: Iterable[Position],
    *,
    blocked: set[Position],
    deadline: float,
    config: AgentConfig,
) -> dict[str, Position]:
    units = tuple(units)
    targets = tuple(set(targets) - blocked)
    candidates: list[tuple[int, str, Position, UnitView]] = []
    for unit in units:
        for target in targets:
            cost = bounded_path_cost(
                unit.position,
                target,
                blocked=blocked,
                deadline=deadline,
                node_limit=config.astar_node_limit,
            )
            if cost is None:
                cost = distance(unit.position, target) + 10_000
            candidates.append((cost, str(unit.id), target, unit))
    assigned_units: set[str] = set()
    assigned_targets: set[Position] = set()
    result: dict[str, Position] = {}
    for _, unit_id, target, _unit in sorted(candidates, key=lambda item: item[:3]):
        if unit_id in assigned_units or target in assigned_targets:
            continue
        assigned_units.add(unit_id)
        assigned_targets.add(target)
        result[unit_id] = target
    return result


def _frontier_assignments(
    units: Iterable[UnitView],
    memory: AgentMemory,
    context: DecisionContext,
    deadline: float,
    config: AgentConfig,
    *,
    task_kind: str,
    sector_offset: int = 0,
) -> dict[str, Position]:
    units = tuple(sorted(units, key=lambda unit: unit.id.bytes))
    blocked = (
        memory.obstacles
        | memory.active_temporary_blocks(context.tick)
        | set(context.enemy_occupancy)
    )
    frontier = memory.frontier() - blocked
    assigned: set[Position] = set()
    result: dict[str, Position] = {}
    if not units:
        return result

    # When the remembered frontier is exhausted, keep reconnaissance moving
    # by projecting a bounded target three cells into each unit's assigned
    # sector. The target is still validated by navigation before submission.
    if not frontier:
        for index, unit in enumerate(units):
            sector = (
                (index * len(_EXPLORATION_SECTORS)) // len(units) + sector_offset
            ) % len(_EXPLORATION_SECTORS)
            dx, dy = _EXPLORATION_SECTORS[sector]
            candidates = (
                (unit.position[0] + dx * 3, unit.position[1] + dy * 3),
                (unit.position[0] + dx * 4, unit.position[1] + dy * 4),
            )
            target = next(
                (cell for cell in candidates if cell not in blocked and cell not in assigned),
                None,
            )
            if target is None:
                continue
            assigned.add(target)
            result[str(unit.id)] = target
            memory.unit_tasks[str(unit.id)] = {
                "kind": task_kind,
                "target": list(target),
                "sector": sector,
                "sector_since": context.tick,
                "failures": 0,
            }
        return result

    for index, unit in enumerate(units):
        unit_id = str(unit.id)
        previous = memory.unit_tasks.get(unit_id, {})
        if previous.get("kind") == task_kind and "sector" in previous:
            sector = int(previous["sector"]) % len(_EXPLORATION_SECTORS)
            sector_since = int(previous.get("sector_since", context.tick))
        else:
            sector = (
                (index * len(_EXPLORATION_SECTORS)) // len(units)
                + sector_offset
            ) % len(_EXPLORATION_SECTORS)
            sector_since = context.tick

        if context.tick - sector_since >= config.exploration_sector_ticks:
            sector = (sector + 1) % len(_EXPLORATION_SECTORS)
            sector_since = context.tick
            previous = {}

        previous_target: Position | None = None
        if previous.get("kind") == task_kind and isinstance(
            previous.get("target"), list
        ):
            raw_target = previous["target"]
            if len(raw_target) == 2:
                previous_target = int(raw_target[0]), int(raw_target[1])
        if previous_target in frontier and previous_target not in assigned:
            target = previous_target
        else:
            target = None
            selected_sector = sector
            for rotation in range(len(_EXPLORATION_SECTORS)):
                candidate_sector = (sector + rotation) % len(_EXPLORATION_SECTORS)
                vector_x, vector_y = _EXPLORATION_SECTORS[candidate_sector]
                geometric_candidates: list[tuple[int, int, int, Position]] = []
                for cell in frontier - assigned:
                    delta_x = cell[0] - unit.position[0]
                    delta_y = cell[1] - unit.position[1]
                    projection = delta_x * vector_x + delta_y * vector_y
                    if projection <= 0:
                        continue
                    lateral = abs(delta_x * vector_y - delta_y * vector_x)
                    geometric_candidates.append(
                        (
                            distance(unit.position, cell) + lateral,
                            lateral,
                            -projection,
                            cell,
                        )
                    )
                candidates: list[tuple[int, int, int, Position]] = []
                for _, lateral, negative_projection, cell in sorted(
                    geometric_candidates
                )[:32]:
                    path_cost = bounded_path_cost(
                        unit.position,
                        cell,
                        blocked=blocked,
                        deadline=deadline,
                        node_limit=config.astar_node_limit,
                    )
                    if path_cost is None:
                        path_cost = distance(unit.position, cell) + 10_000
                    candidates.append(
                        (path_cost + lateral, lateral, negative_projection, cell)
                    )
                if candidates:
                    target = min(candidates)[-1]
                    selected_sector = candidate_sector
                    break
            if target is None:
                continue
            if selected_sector != sector:
                sector = selected_sector
                sector_since = context.tick

        assigned.add(target)
        result[unit_id] = target
        memory.unit_tasks[unit_id] = {
            "kind": task_kind,
            "target": list(target),
            "sector": sector,
            "sector_since": sector_since,
            "failures": int(previous.get("failures", 0)),
        }
    return result


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
    return _move(
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


def _plan_workers(
    context: DecisionContext,
    memory: AgentMemory,
    reservations: ReservationTable,
    deadline: float,
    config: AgentConfig,
    heal_allowances: dict[UUID, int],
) -> list[ActionIntent]:
    intents: list[ActionIntent] = []
    empty_workers = tuple(worker for worker in context.workers if not (worker.cargo or 0))
    worker_blocks = (
        memory.obstacles
        | memory.active_temporary_blocks(context.tick)
        | set(context.enemy_occupancy)
    )
    resource_assignments = _assign_unique_targets(
        empty_workers,
        context.resource_cells,
        blocked=worker_blocks,
        deadline=deadline,
        config=config,
    )

    unassigned = [
        worker for worker in empty_workers if str(worker.id) not in resource_assignments
    ]
    remembered_targets = set(memory.resource_observations) - set(context.resource_cells)
    reconnaissance = _assign_unique_targets(
        unassigned,
        remembered_targets,
        blocked=worker_blocks,
        deadline=deadline,
        config=config,
    )
    still_unassigned = [
        worker for worker in unassigned if str(worker.id) not in reconnaissance
    ]
    exploration = _frontier_assignments(
        still_unassigned,
        memory,
        context,
        deadline,
        config,
        task_kind="explore",
    )

    for worker in sorted(context.workers, key=lambda unit: str(unit.id)):
        cargo = worker.cargo or 0
        if cargo and _at_normal_core(worker, context):
            intents.append(
                ActionIntent(
                    actor_id=worker.id,
                    is_core=False,
                    action=ActionKind.DEPOSIT,
                    score=950,
                    reason="preserve_worker_cargo",
                )
            )
            continue
        if cargo:
            intent = _return_to_core(
                worker,
                context,
                memory,
                reservations,
                deadline,
                config,
                "return_cargo_to_core",
            )
            if context.core is not None:
                return_target = (
                    context.core.destination
                    if context.core.state is CoreState.MOVING
                    and context.core.destination
                    else context.core.position
                )
                _record_unit_task(
                    memory,
                    context,
                    worker,
                    kind="return",
                    target=return_target,
                    intent=intent,
                )
            intents.append(intent or _wait(worker, "no_safe_route_with_cargo"))
            continue

        if worker.hp < UNIT_MAX_HP[UnitType.WORKER] and _at_normal_core(
            worker, context
        ):
            heal_cost = heal_allowances.get(worker.id, 0)
            intents.append(
                _unit_heal_intent(worker, heal_cost)
                if heal_cost > 0
                else _wait(worker, "healing_waits_for_resources")
            )
            continue

        target = resource_assignments.get(str(worker.id))
        if target is not None and worker.position == target:
            intents.append(
                ActionIntent(
                    actor_id=worker.id,
                    is_core=False,
                    action=ActionKind.HARVEST,
                    score=900,
                    reason="assigned_visible_resource",
                    target_cell=target,
                )
            )
            _record_unit_task(
                memory,
                context,
                worker,
                kind="resource",
                target=target,
                intent=None,
            )
            continue
        if target is not None:
            intent = _move(
                worker,
                target,
                "move_to_unique_resource",
                750,
                context=context,
                memory=memory,
                reservations=reservations,
                deadline=deadline,
                config=config,
                avoid_threats=True,
            )
            _record_unit_task(
                memory,
                context,
                worker,
                kind="resource",
                target=target,
                intent=intent,
            )
            intents.append(intent or _wait(worker, "resource_route_blocked"))
            continue

        target = reconnaissance.get(str(worker.id))
        reason = "reobserve_remembered_resource"
        if target is None:
            target = exploration.get(str(worker.id))
            reason = "explore_sector_frontier"
        if target is not None:
            task_kind = (
                "recon" if reconnaissance.get(str(worker.id)) else "explore"
            )
            intent = _move(
                worker,
                target,
                reason,
                400,
                context=context,
                memory=memory,
                reservations=reservations,
                deadline=deadline,
                config=config,
                avoid_threats=True,
            )
            _record_unit_task(
                memory,
                context,
                worker,
                kind=task_kind,
                target=target,
                intent=intent,
            )
            intents.append(intent or _wait(worker, "exploration_route_blocked"))
            continue

        intents.append(_wait(worker, "no_resource_or_frontier"))
    return intents


def _vanguard_urgent_cell(
    enemies: Iterable[CoreView | UnitView],
    context: DecisionContext,
    memory: AgentMemory,
) -> bool:
    return any(
        isinstance(enemy, CoreView)
        or (
            context.core is not None
            and _enemy_can_attack_core(enemy, context.core, memory.obstacles)
        )
        for enemy in enemies
    )


def _plan_vanguards(
    context: DecisionContext,
    memory: AgentMemory,
    mode: StrategicMode,
    reservations: ReservationTable,
    deadline: float,
    config: AgentConfig,
    heal_allowances: dict[UUID, int],
) -> list[ActionIntent]:
    intents: list[ActionIntent] = []
    beacon_vanguard = min(
        context.vanguards,
        key=lambda unit: (distance(unit.position, context.beacon.position), str(unit.id)),
        default=None,
    )
    guard_slots = _guard_slots(context, memory)

    for index, vanguard in enumerate(sorted(context.vanguards, key=lambda unit: str(unit.id))):
        adjacent_by_cell: dict[Position, list[CoreView | UnitView]] = {}
        for enemy in context.enemies:
            if distance(vanguard.position, enemy.position) == 1:
                adjacent_by_cell.setdefault(enemy.position, []).append(enemy)
        best_cell = max(
            adjacent_by_cell,
            key=lambda cell: (
                vanguard_cell_score(adjacent_by_cell[cell]),
                cell,
            ),
            default=None,
        )
        urgent = (
            best_cell is not None
            and _vanguard_urgent_cell(adjacent_by_cell[best_cell], context, memory)
        )

        if vanguard.hp == 1 and not urgent:
            if _at_normal_core(vanguard, context) and heal_allowances.get(
                vanguard.id, 0
            ) > 0:
                heal_cost = heal_allowances[vanguard.id]
                intents.append(_unit_heal_intent(vanguard, heal_cost))
            else:
                intent = _return_to_core(
                    vanguard,
                    context,
                    memory,
                    reservations,
                    deadline,
                    config,
                    "critical_vanguard_retreat",
                )
                intents.append(intent or _wait(vanguard, "critical_retreat_blocked"))
            continue
        if (
            mode is StrategicMode.RECOVER
            and vanguard.hp < UNIT_MAX_HP[UnitType.VANGUARD]
            and _at_normal_core(vanguard, context)
            and not urgent
        ):
            heal_cost = heal_allowances.get(vanguard.id, 0)
            intents.append(
                _unit_heal_intent(vanguard, heal_cost)
                if heal_cost > 0
                else _wait(vanguard, "healing_waits_for_resources")
            )
            continue
        if best_cell is not None:
            direction = next(
                (
                    direction
                    for direction in DIRECTIONS
                    if destination(vanguard.position, direction) == best_cell
                ),
                None,
            )
            intents.append(
                ActionIntent(
                    actor_id=vanguard.id,
                    is_core=False,
                    action=ActionKind.SWEEP,
                    score=850 + vanguard_cell_score(adjacent_by_cell[best_cell]),
                    reason="highest_value_adjacent_enemy_cell",
                    target_cell=best_cell,
                    direction=direction,
                )
            )
            continue

        if mode is StrategicMode.BEACON and beacon_vanguard is vanguard:
            if (
                vanguard.position == context.beacon.position
                and context.beacon.status is BeaconStatus.GROUND
            ):
                intents.append(
                    ActionIntent(
                        actor_id=vanguard.id,
                        is_core=False,
                        action=ActionKind.PICKUP_BEACON,
                        score=780,
                        reason="preferred_vanguard_beacon_pickup",
                    )
                )
            else:
                intent = _move(
                    vanguard,
                    context.beacon.position,
                    "preferred_vanguard_to_beacon",
                    650,
                    context=context,
                    memory=memory,
                    reservations=reservations,
                    deadline=deadline,
                    config=config,
                )
                intents.append(intent or _wait(vanguard, "beacon_route_blocked"))
            memory.unit_tasks[str(vanguard.id)] = {
                "kind": "beacon",
                "target": list(context.beacon.position),
            }
            continue

        target_enemy = _best_visible_enemy(vanguard, context, memory)
        if target_enemy is not None and mode in (
            StrategicMode.DEFEND,
            StrategicMode.ATTACK,
        ):
            intent = _move(
                vanguard,
                target_enemy.position,
                "intercept_visible_threat"
                if mode is StrategicMode.DEFEND
                else "advance_on_high_value_enemy",
                600,
                context=context,
                memory=memory,
                reservations=reservations,
                deadline=deadline,
                config=config,
            )
            intents.append(intent or _wait(vanguard, "enemy_approach_blocked"))
            continue

        guard_target = guard_slots[index % len(guard_slots)] if guard_slots else None
        if guard_target is not None and vanguard.position != guard_target:
            intent = _move(
                vanguard,
                guard_target,
                "hold_core_defense_ring",
                300,
                context=context,
                memory=memory,
                reservations=reservations,
                deadline=deadline,
                config=config,
            )
            intents.append(intent or _wait(vanguard, "guard_route_blocked"))
        else:
            intents.append(_wait(vanguard, "holding_defense_ring"))
    return intents


def _ranger_attack_is_urgent(
    enemy: CoreView | UnitView,
    context: DecisionContext,
    memory: AgentMemory,
) -> bool:
    remaining = enemy.hp + (enemy.shield if isinstance(enemy, CoreView) else 0)
    return (
        remaining <= 1
        or (
            context.core is not None
            and _enemy_can_attack_core(enemy, context.core, memory.obstacles)
        )
    )


def _ranger_staging_cell(
    ranger: UnitView,
    target: CoreView | UnitView,
    context: DecisionContext,
    memory: AgentMemory,
) -> Position:
    tx, ty = target.position
    offsets = (
        (0, -3), (0, -2), (0, 2), (0, 3),
        (-3, 0), (-2, 0), (2, 0), (3, 0),
        (-2, -2), (-2, 2), (2, -2), (2, 2),
        (-3, -3), (-3, 3), (3, -3), (3, 3),
    )
    candidates = [
        (tx + dx, ty + dy)
        for dx, dy in offsets
        if (tx + dx, ty + dy) not in memory.obstacles
        and (tx + dx, ty + dy) not in context.enemy_occupancy
    ]
    return min(
        candidates,
        key=lambda cell: (distance(ranger.position, cell), cell),
        default=target.position,
    )


def _plan_rangers(
    context: DecisionContext,
    memory: AgentMemory,
    mode: StrategicMode,
    reservations: ReservationTable,
    deadline: float,
    config: AgentConfig,
    heal_allowances: dict[UUID, int],
) -> list[ActionIntent]:
    intents: list[ActionIntent] = []
    stable_roster = (
        len(context.workers) >= config.early_workers
        and len(context.vanguards) >= config.early_vanguards
        and len(context.rangers) >= config.early_rangers
    )
    scout = min(context.rangers, key=lambda unit: str(unit.id), default=None)
    scout_assignments = (
        _frontier_assignments(
            (scout,),
            memory,
            context,
            deadline,
            config,
            task_kind="scout",
            sector_offset=3,
        )
        if stable_roster and scout is not None and mode is StrategicMode.EXPLORE
        else {}
    )
    guard_slots = _guard_slots(context, memory)

    for index, ranger in enumerate(sorted(context.rangers, key=lambda unit: str(unit.id))):
        shootable = [
            enemy
            for enemy in context.enemies
            if shot_range(ranger.position, enemy.position, memory.obstacles) is not None
        ]
        target = min(
            shootable,
            key=lambda enemy: (
                -ranger_target_score(ranger, enemy, context, memory),
                enemy.id.bytes,
            ),
            default=None,
        )
        urgent = target is not None and _ranger_attack_is_urgent(
            target, context, memory
        )
        if ranger.hp == 1 and not urgent:
            if _at_normal_core(ranger, context) and heal_allowances.get(
                ranger.id, 0
            ) > 0:
                heal_cost = heal_allowances[ranger.id]
                intents.append(_unit_heal_intent(ranger, heal_cost))
            else:
                intent = _return_to_core(
                    ranger,
                    context,
                    memory,
                    reservations,
                    deadline,
                    config,
                    "critical_ranger_retreat",
                )
                intents.append(intent or _wait(ranger, "critical_retreat_blocked"))
            continue
        if target is not None:
            intents.append(
                ActionIntent(
                    actor_id=ranger.id,
                    is_core=False,
                    action=ActionKind.SHOOT,
                    score=850 + ranger_target_score(ranger, target, context, memory),
                    reason="highest_scoring_legal_ranger_target",
                    target_id=target.id,
                    target_cell=target.position,
                )
            )
            continue

        scout_target = scout_assignments.get(str(ranger.id))
        if scout_target is not None:
            intent = _move(
                ranger,
                scout_target,
                "stable_roster_ranger_scout",
                420,
                context=context,
                memory=memory,
                reservations=reservations,
                deadline=deadline,
                config=config,
            )
            _record_unit_task(
                memory,
                context,
                ranger,
                kind="scout",
                target=scout_target,
                intent=intent,
            )
            intents.append(intent or _wait(ranger, "scout_route_blocked"))
            continue

        target_enemy = _best_visible_enemy(ranger, context, memory)
        if target_enemy is not None and mode in (
            StrategicMode.DEFEND,
            StrategicMode.ATTACK,
        ):
            staging = _ranger_staging_cell(
                ranger, target_enemy, context, memory
            )
            intent = _move(
                ranger,
                staging,
                "ranger_seek_legal_firing_line",
                580,
                context=context,
                memory=memory,
                reservations=reservations,
                deadline=deadline,
                config=config,
            )
            intents.append(intent or _wait(ranger, "firing_route_blocked"))
            continue

        guard_target = (
            guard_slots[(index + len(context.vanguards)) % len(guard_slots)]
            if guard_slots
            else None
        )
        if guard_target is not None and ranger.position != guard_target:
            intent = _move(
                ranger,
                guard_target,
                "ranger_hold_defense_ring",
                280,
                context=context,
                memory=memory,
                reservations=reservations,
                deadline=deadline,
                config=config,
            )
            intents.append(intent or _wait(ranger, "guard_route_blocked"))
        else:
            intents.append(_wait(ranger, "holding_defense_ring"))
    return intents


def _spawn_target(context: DecisionContext, config: AgentConfig) -> UnitType | None:
    counts = Counter(unit.unit_type for unit in context.units)
    early = (
        (UnitType.WORKER, config.early_workers),
        (UnitType.VANGUARD, config.early_vanguards),
        (UnitType.RANGER, config.early_rangers),
    )
    for unit_type, target in early:
        if counts[unit_type] < target:
            return unit_type
    mature = (
        (UnitType.WORKER, config.mature_workers),
        (UnitType.VANGUARD, config.mature_vanguards),
        (UnitType.RANGER, config.mature_rangers),
    )
    deficits = [
        (target - counts[unit_type], -index, unit_type)
        for index, (unit_type, target) in enumerate(mature)
        if counts[unit_type] < target
    ]
    return max(deficits, default=(0, 0, None))[2]


def _spawn_cell_is_free(
    context: DecisionContext, unit_intents: Iterable[ActionIntent]
) -> bool:
    if context.core is None:
        return False
    intents_by_actor = {
        intent.actor_id: intent for intent in unit_intents if not intent.is_core
    }
    final_unit_count = 0
    for unit in context.units:
        intent = intents_by_actor.get(unit.id)
        if unit.position == context.core.position:
            if intent is None or intent.action is not ActionKind.MOVE:
                final_unit_count += 1
        elif (
            intent is not None
            and intent.action is ActionKind.MOVE
            and intent.reserved_cell == context.core.position
        ):
            final_unit_count += 1
    return final_unit_count == 0


def _core_migration_direction(
    context: DecisionContext,
    memory: AgentMemory,
    unit_intents: Iterable[ActionIntent],
) -> Direction | None:
    if context.core is None:
        return None
    occupied = set(context.friendly_occupancy) | set(context.enemy_occupancy)
    occupied.update(
        intent.reserved_cell
        for intent in unit_intents
        if intent.action is ActionKind.MOVE and intent.reserved_cell is not None
    )
    candidates = [
        direction
        for direction in DIRECTIONS
        if destination(context.core.position, direction) not in memory.obstacles
        and destination(context.core.position, direction) not in occupied
    ]
    frontier = memory.frontier()
    if not candidates or not frontier:
        return None
    target = max(
        frontier,
        key=lambda cell: (
            min((distance(cell, resource) for resource in memory.resource_observations), default=0),
            distance(context.core.position, cell),
            cell,
        ),
    )
    return min(
        candidates,
        key=lambda direction: (
            distance(destination(context.core.position, direction), target),
            direction.value,
        ),
    )


def _plan_core(
    context: DecisionContext,
    memory: AgentMemory,
    mode: StrategicMode,
    unit_intents: list[ActionIntent],
    planned_unit_heals: int,
    config: AgentConfig,
) -> ActionIntent | None:
    core = context.core
    if core is None:
        return None
    if core.state is CoreState.MOVING:
        return _wait(core, "core_migration_progresses_naturally", is_core=True)
    available_resources = _anticipated_resources(context)
    if core.hp < CORE_MAX_HP:
        return ActionIntent(
            actor_id=core.id,
            is_core=True,
            action=ActionKind.HEAL,
            score=1_000,
            reason="core_hp_recovery",
            estimated_cost=min(CORE_MAX_HP - core.hp, available_resources),
        )
    if core.shield < 3:
        return ActionIntent(
            actor_id=core.id,
            is_core=True,
            action=ActionKind.REPAIR_SHIELD,
            score=980,
            reason="core_emergency_shield_repair",
            estimated_cost=1 if available_resources else 0,
        )

    if (
        mode is StrategicMode.BEACON
        and not context.vanguards
        and core.position == context.beacon.position
        and context.beacon.status is BeaconStatus.GROUND
    ):
        return ActionIntent(
            actor_id=core.id,
            is_core=True,
            action=ActionKind.PICKUP_BEACON,
            score=760,
            reason="fallback_core_beacon_pickup",
        )

    reserve = max(
        config.minimum_resource_reserve,
        CORE_MAX_HP - core.hp + planned_unit_heals,
    )
    shield_cap = 10 if _beacon_owned(context) else 5
    if core.shield < shield_cap and available_resources > reserve:
        return ActionIntent(
            actor_id=core.id,
            is_core=True,
            action=ActionKind.REPAIR_SHIELD,
            score=700,
            reason="restore_available_core_shield_capacity",
            estimated_cost=1,
        )

    spawn_type = _spawn_target(context, config)
    if (
        spawn_type is not None
        and context.population < config.max_population
        and _spawn_cell_is_free(context, unit_intents)
    ):
        price = unit_cost(spawn_type, context.population)
        if available_resources >= price + reserve:
            return ActionIntent(
                actor_id=core.id,
                is_core=True,
                action=ActionKind.SPAWN,
                score=650,
                reason="fill_dynamic_roster_deficit",
                unit_type=spawn_type,
                estimated_cost=price,
            )

    immediate_threat = any(
        _enemy_can_attack_core(enemy, core, memory.obstacles)
        for enemy in context.enemies
    )
    if (
        mode in (StrategicMode.ECONOMY, StrategicMode.EXPLORE)
        and memory.no_resource_ticks >= config.migration_idle_ticks
        and not context.resource_cells
        and not context.enemies
        and not immediate_threat
        and core.hp == CORE_MAX_HP
        and core.shield >= 3
    ):
        direction = _core_migration_direction(context, memory, unit_intents)
        if direction is not None:
            return ActionIntent(
                actor_id=core.id,
                is_core=True,
                action=ActionKind.START_MOVE,
                score=350,
                reason="eight_tick_safe_exploration_migration",
                direction=direction,
                reserved_cell=destination(core.position, direction),
            )
    return _wait(core, "resources_reserved_or_no_legal_core_action", is_core=True)


def propose_intents(
    context: DecisionContext,
    memory: AgentMemory,
    config: AgentConfig,
    deadline: float,
) -> tuple[StrategicMode, tuple[ActionIntent, ...], bool]:
    mode = choose_mode(context, memory, config)
    memory.record_mode(mode, context.tick)
    if context.core is None:
        return mode, (), perf_counter() >= deadline

    occupancy = {cell: len(ids) for cell, ids in context.friendly_occupancy.items()}
    if context.core.state is CoreState.MOVING and context.core.destination:
        # The migrating Core will occupy one slot at its public destination.
        occupancy[context.core.destination] = occupancy.get(context.core.destination, 0) + 1
    reservations = ReservationTable(occupancy=occupancy)

    anticipated_resources = _anticipated_resources(context)
    core_future_cost = min(
        max(0, CORE_MAX_HP - context.core.hp), anticipated_resources
    )
    remaining_heal_budget = max(0, anticipated_resources - core_future_cost)
    heal_allowances: dict[UUID, int] = {}
    for unit in sorted(context.units, key=lambda candidate: candidate.id.bytes):
        if not _at_normal_core(unit, context):
            continue
        missing_hp = UNIT_MAX_HP[unit.unit_type] - unit.hp
        allowance = min(missing_hp, remaining_heal_budget)
        if allowance > 0:
            heal_allowances[unit.id] = allowance
            remaining_heal_budget -= allowance

    intents = _plan_workers(
        context, memory, reservations, deadline, config, heal_allowances
    )
    intents.extend(
        _plan_vanguards(
            context,
            memory,
            mode,
            reservations,
            deadline,
            config,
            heal_allowances,
        )
    )
    intents.extend(
        _plan_rangers(
            context,
            memory,
            mode,
            reservations,
            deadline,
            config,
            heal_allowances,
        )
    )
    planned_unit_heals = sum(
        intent.estimated_cost
        for intent in intents
        if intent.action is ActionKind.HEAL
    )
    core_intent = _plan_core(
        context, memory, mode, intents, planned_unit_heals, config
    )
    if core_intent is not None:
        intents.append(core_intent)
    return mode, tuple(intents), perf_counter() >= deadline
