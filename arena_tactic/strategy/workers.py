"""Worker harvesting, return, and frontier plans."""

from __future__ import annotations

from typing import Iterable
from uuid import UUID

from arena_hero import CoreState, Direction, UnitType, UnitView

from ..context import DecisionContext
from ..memory import AgentMemory
from ..models import ActionIntent, ActionKind, AgentConfig, Position, ReservationTable, StrategicMode
from ..navigation import DIRECTIONS, bounded_path_cost, destination, distance, enemy_threat_cells
from ..squads import SquadPlan, SquadType
from .common import (
    UNIT_MAX_HP,
    _EXPLORATION_SECTORS,
    _at_normal_core,
    _deploy_sidestep,
    _move,
    _record_unit_task,
    _return_to_core,
    _return_to_core_sidestep,
    _unit_heal_intent,
    _unit_needs_retreat_heal,
    _unit_retreat_to_core,
    _wait,
)
from .mode import _core_emergency_defense


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
            path_cost = bounded_path_cost(
                unit.position,
                target,
                blocked=blocked,
                deadline=deadline,
                node_limit=config.astar_node_limit,
            )
            if path_cost is None:
                continue
            candidates.append((path_cost, str(unit.id), target, unit))
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


def _locked_resource_targets(
    workers: Iterable[UnitView],
    memory: AgentMemory,
    context: DecisionContext,
    config: AgentConfig,
) -> dict[str, Position]:
    """Keep an in-progress resource route stable through short fog gaps."""
    locked: dict[str, Position] = {}
    claimed: set[Position] = set()
    for worker in sorted(workers, key=lambda unit: str(unit.id)):
        task = memory.unit_tasks.get(str(worker.id), {})
        raw_target = task.get("target")
        if (
            task.get("kind") != "resource"
            or not isinstance(raw_target, list)
            or len(raw_target) != 2
            or not all(type(axis) is int for axis in raw_target)
        ):
            continue
        target = raw_target[0], raw_target[1]
        observed_tick = memory.resource_observations.get(target)
        if (
            observed_tick is None
            or context.tick - observed_tick > config.resource_target_grace_ticks
            or target in claimed
        ):
            continue
        locked[str(worker.id)] = target
        claimed.add(target)
    return locked


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
        | enemy_threat_cells(context)
    )
    frontier = memory.frontier() - blocked
    assigned: set[Position] = set()
    result: dict[str, Position] = {}
    if not units:
        return result

    if not frontier:
        for index, unit in enumerate(units):
            sector = (
                (index * len(_EXPLORATION_SECTORS)) // len(units) + sector_offset
            ) % len(_EXPLORATION_SECTORS)
            dx, dy = _EXPLORATION_SECTORS[sector]
            candidates = (
                (unit.position[0] + dx * 3, unit.position[1] + dy * 3),
                (unit.position[0] + dx * 4, unit.position[1] + dy * 4),
                (unit.position[0] + dx, unit.position[1] + dy),
            )
            target = next(
                (
                    cell for cell in candidates
                    if cell not in blocked
                    and cell not in assigned
                    and bounded_path_cost(
                        unit.position,
                        cell,
                        blocked=blocked,
                        deadline=deadline,
                        node_limit=config.astar_node_limit,
                    ) is not None
                ),
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
                (index * len(_EXPLORATION_SECTORS)) // len(units) + sector_offset
            ) % len(_EXPLORATION_SECTORS)
            sector_since = context.tick

        previous_target: Position | None = None
        if previous.get("kind") == task_kind and isinstance(previous.get("target"), list):
            raw_target = previous["target"]
            if len(raw_target) == 2:
                previous_target = int(raw_target[0]), int(raw_target[1])

        if previous_target in frontier and previous_target not in assigned:
            target = previous_target
        else:
            if context.tick - sector_since >= config.exploration_sector_ticks:
                sector = (sector + 1) % len(_EXPLORATION_SECTORS)
                sector_since = context.tick
            remaining_hold = max(
                1, config.exploration_sector_ticks - (context.tick - sector_since)
            )
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
                    geometric_candidates.append((
                        distance(unit.position, cell) + lateral,
                        lateral,
                        -projection,
                        cell,
                    ))
                candidates: list[tuple[int, int, int, Position]] = []
                fallback_candidates: list[tuple[int, int, int, Position]] = []
                for _, lateral, negative_projection, cell in sorted(geometric_candidates)[:32]:
                    path_cost = bounded_path_cost(
                        unit.position,
                        cell,
                        blocked=blocked,
                        deadline=deadline,
                        node_limit=config.astar_node_limit,
                    )
                    if path_cost is None:
                        continue
                    item = (path_cost + lateral, lateral, negative_projection, cell)
                    fallback_candidates.append(item)
                    if path_cost <= remaining_hold:
                        candidates.append(item)
                if candidates:
                    target = min(candidates)[-1]
                    selected_sector = candidate_sector
                    break
                if fallback_candidates and target is None:
                    target = min(fallback_candidates)[-1]
                    selected_sector = candidate_sector
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


def _plan_workers(
    context: DecisionContext,
    memory: AgentMemory,
    reservations: ReservationTable,
    deadline: float,
    config: AgentConfig,
    heal_allowances: dict[UUID, int],
    squad_plan: SquadPlan | None = None,
) -> list[ActionIntent]:
    from . import _return_to_core as return_to_core

    intents: list[ActionIntent] = []
    core = context.core
    if core is None:
        return intents
    combat_ready = len(context.vanguards) >= 1 and len(context.rangers) >= 1
    if memory.last_mode is StrategicMode.DEFEND or _core_emergency_defense(context):
        for worker in sorted(context.workers, key=lambda unit: str(unit.id)):
            if (
                worker.cargo
                and core.state is CoreState.MOVING
                and memory.unit_tasks.get(str(worker.id), {}).get("kind") == "await_core_stationary"
            ):
                intents.append(_wait(worker, "deposit_waits_for_core_migration"))
                continue
            if _at_normal_core(worker, context) and worker.cargo and context.resource_space > 0:
                intents.append(ActionIntent(worker.id, False, ActionKind.DEPOSIT, 980, "emergency_deposit_at_core"))
                continue
            if worker.cargo or combat_ready:
                intent = return_to_core(
                    worker, context, memory, reservations, deadline, config,
                    "emergency_worker_rally_to_core",
                )
                target = core.destination if core.state is CoreState.MOVING and core.destination else core.position
                if intent is None:
                    intent = _return_to_core_sidestep(
                        worker, target, context, memory, reservations,
                        "emergency_worker_rally_to_core",
                        minimum_distance=1 if worker.cargo else 2,
                    )
                if intent is not None:
                    intents.append(intent)
                elif worker.cargo and distance(worker.position, target) <= 1:
                    intents.append(_wait(worker, "emergency_deposit_queue_wait"))
                elif combat_ready and distance(worker.position, target) <= 2:
                    intents.append(_wait(worker, "emergency_worker_sheltered_near_core"))
                else:
                    intents.append(_wait(worker, "emergency_worker_rally_blocked"))
                continue
        if combat_ready:
            return intents

    empty_workers = tuple(worker for worker in context.workers if not (worker.cargo or 0))
    if squad_plan is not None:
        mining_worker_ids = squad_plan.ids_for(SquadType.MINING_ESCORT, UnitType.WORKER)
        scout_worker_ids = squad_plan.ids_for(SquadType.SCOUT_RECON, UnitType.WORKER)
        base_worker_ids = squad_plan.ids_for(SquadType.BASE_DEFENSE, UnitType.WORKER)
        expedition_worker_ids = squad_plan.ids_for(SquadType.EXPEDITION_BEACON, UnitType.WORKER)
    else:
        mining_worker_ids = {worker.id for worker in empty_workers}
        scout_worker_ids = set()
        base_worker_ids = set()
        expedition_worker_ids = set()

    economic_workers = tuple(worker for worker in empty_workers if worker.id in mining_worker_ids)
    scout_workers = tuple(worker for worker in empty_workers if worker.id in scout_worker_ids)
    worker_blocks = (
        memory.obstacles
        | memory.active_temporary_blocks(context.tick)
        | set(context.enemy_occupancy)
        | enemy_threat_cells(context)
    )
    worker_by_id = {str(worker.id): worker for worker in economic_workers}
    locked_resource_assignments = _locked_resource_targets(
        economic_workers, memory, context, config
    )
    locked_resource_assignments = {
        unit_id: target
        for unit_id, target in locked_resource_assignments.items()
        if target not in worker_blocks
        and (worker := worker_by_id.get(unit_id)) is not None
        and bounded_path_cost(
            worker.position,
            target,
            blocked=worker_blocks,
            deadline=deadline,
            node_limit=config.astar_node_limit,
        ) is not None
    }
    resource_assignments = _assign_unique_targets(
        (
            worker for worker in economic_workers
            if str(worker.id) not in locked_resource_assignments
        ),
        set(context.resource_cells) - set(locked_resource_assignments.values()),
        blocked=worker_blocks,
        deadline=deadline,
        config=config,
    )
    resource_assignments = locked_resource_assignments | resource_assignments

    unassigned = [
        worker for worker in economic_workers if str(worker.id) not in resource_assignments
    ]
    remembered_targets = (
        set(memory.resource_observations)
        - set(context.resource_cells)
        - memory.active_resource_recheck_cooldowns(context.tick)
    )
    recheck_workers = tuple(sorted(unassigned, key=lambda unit: str(unit.id)))[
        : config.resource_recheck_worker_limit
    ]
    reconnaissance = _assign_unique_targets(
        recheck_workers,
        remembered_targets,
        blocked=worker_blocks,
        deadline=deadline,
        config=config,
    )
    still_unassigned = [
        worker for worker in unassigned if str(worker.id) not in reconnaissance
    ]
    exploration = _frontier_assignments(
        tuple(still_unassigned) + scout_workers,
        memory,
        context,
        deadline,
        config,
        task_kind="explore",
    )

    core_pos = context.core.position
    preempted_doorstep_ids: set[UUID] = set()

    def _relieve_delivery_corridor(
        cell: Position,
        *,
        depth: int = 0,
        visited: frozenset[Position] = frozenset(),
    ) -> bool:
        """Create a bounded outward yield chain from a full delivery throat.

        A Core in a one-cell pocket cannot trade places with a full doorstep:
        the Worker on the Core must leave while at least one cargo Worker on
        the doorstep leaves in the same movement graph.  Start at the far end
        of a saturated corridor so each reservation records its departure
        before the next Worker enters that cell.
        """
        if depth >= 8 or cell in visited:
            return False
        occupants = tuple(
            worker for worker in context.workers
            if worker.position == cell and worker.id not in preempted_doorstep_ids
        )
        if not occupants:
            return reservations.can_reserve(cell)
        candidate_workers = tuple(sorted(
            (worker for worker in occupants if worker.cargo), key=lambda worker: worker.id.bytes
        ))
        if not candidate_workers:
            return False
        threats = enemy_threat_cells(context)
        for worker in candidate_workers:
            choices: list[tuple[int, int, str, Direction, Position]] = []
            for direction in DIRECTIONS:
                target = destination(cell, direction)
                if (
                    target == core_pos
                    or target in memory.obstacles
                    or target in memory.active_temporary_blocks(context.tick)
                    or target in context.enemy_occupancy
                    or target in threats
                ):
                    continue
                # Prefer outward travel, then side lanes.  A recursive yield
                # permits a packed westbound queue to shed one unit per cell.
                outward_penalty = 0 if distance(target, core_pos) > distance(cell, core_pos) else 1
                choices.append((outward_penalty, distance(target, core_pos), direction.value, direction, target))
            for _, _, _, direction, target in sorted(choices):
                if not reservations.can_reserve(target) and not _relieve_delivery_corridor(
                    target, depth=depth + 1, visited=visited | {cell}
                ):
                    continue
                if not reservations.reserve(target, source=cell):
                    continue
                preempted_doorstep_ids.add(worker.id)
                intents.append(ActionIntent(
                    actor_id=worker.id,
                    is_core=False,
                    action=ActionKind.MOVE,
                    score=430 if depth == 0 else 410,
                    reason="yield_doorstep_congestion" if depth == 0 else "yield_delivery_corridor_congestion",
                    direction=direction,
                    target_cell=target,
                    reserved_cell=target,
                ))
                _record_unit_task(memory, context, worker, kind="yield_delivery_corridor", target=target, intent=intents[-1])
                return True
        return False

    def _worker_order(unit: UnitView) -> tuple[int, int, str]:
        u_cargo = unit.cargo or 0
        at_core = _at_normal_core(unit, context)
        dist = distance(unit.position, core_pos)
        # The only spare Core slot is a delivery-critical resource.  An empty
        # Worker on the Core must reserve its evacuation before doorstep cargo
        # attempts to enter, otherwise a one-cell pocket becomes a swap wait.
        if not u_cargo and at_core:
            return (0, 0, str(unit.id))
        if u_cargo and dist <= 1:
            return (1, 0, str(unit.id))
        if u_cargo:
            return (2, dist, str(unit.id))
        return (3, dist, str(unit.id))

    for worker in sorted(context.workers, key=_worker_order):
        if worker.id in preempted_doorstep_ids:
            continue
        cargo = worker.cargo or 0
        if (
            cargo
            and core.state is CoreState.MOVING
            and memory.unit_tasks.get(str(worker.id), {}).get("kind") == "await_core_stationary"
        ):
            intents.append(_wait(worker, "deposit_waits_for_core_migration"))
            continue
        if cargo and _at_normal_core(worker, context):
            intents.append(ActionIntent(
                actor_id=worker.id,
                is_core=False,
                action=ActionKind.DEPOSIT,
                score=950,
                reason="preserve_worker_cargo",
            ))
            continue
        if cargo:
            intent = return_to_core(
                worker, context, memory, reservations, deadline, config,
                "return_cargo_to_core",
            )
            return_target = core.destination if core.state is CoreState.MOVING and core.destination else core.position
            if intent is None:
                if distance(worker.position, return_target) > 1:
                    intent = _return_to_core_sidestep(
                        worker, return_target, context, memory, reservations,
                        "return_cargo_to_core", minimum_distance=1,
                    )
                elif (
                    distance(worker.position, return_target) == 1
                    and len(context.friendly_occupancy.get(worker.position, ())) >= 2
                    and worker.id == sorted(
                        context.friendly_occupancy.get(worker.position, ()), key=lambda unit_id: str(unit_id)
                    )[0]
                ):
                    existing_task = memory.unit_tasks.get(str(worker.id), {})
                    prev_cell_raw = existing_task.get("prev_cell")
                    prev_cell = tuple(prev_cell_raw) if isinstance(prev_cell_raw, (list, tuple)) and len(prev_cell_raw) == 2 else None
                    candidates = []
                    for direction in DIRECTIONS:
                        side_cell = destination(worker.position, direction)
                        if (
                            side_cell != return_target
                            and side_cell not in memory.obstacles
                            and side_cell not in memory.active_temporary_blocks(context.tick)
                            and side_cell not in context.enemy_occupancy
                        ):
                            penalty = 5000 if prev_cell is not None and side_cell == prev_cell else 0
                            candidates.append((penalty, direction, side_cell))
                    candidates.sort(key=lambda item: (item[0], item[1].value))
                    for _, direction, side_cell in candidates:
                        if reservations.reserve(side_cell, source=worker.position):
                            intent = ActionIntent(
                                actor_id=worker.id,
                                is_core=False,
                                action=ActionKind.MOVE,
                                score=400,
                                reason="yield_doorstep_congestion",
                                direction=direction,
                                target_cell=side_cell,
                                reserved_cell=side_cell,
                            )
                            break
            _record_unit_task(memory, context, worker, kind="return", target=return_target, intent=intent)
            intents.append(intent or _wait(worker, "no_safe_route_with_cargo"))
            continue

        if worker.hp < UNIT_MAX_HP[UnitType.WORKER] and _at_normal_core(worker, context):
            heal_cost = heal_allowances.get(worker.id, 0)
            intents.append(_unit_heal_intent(worker, heal_cost) if heal_cost > 0 else _wait(worker, "healing_waits_for_resources"))
            continue

        if (
            _unit_needs_retreat_heal(worker, memory, config)
            and not (_core_emergency_defense(context) and not combat_ready and not cargo)
        ):
            intent = _unit_retreat_to_core(worker, context, memory, reservations, deadline, config)
            intents.append(intent or _wait(worker, "unit_retreat_to_core_heal_blocked"))
            continue

        if worker.id in base_worker_ids:
            if worker.position == core.position:
                _record_unit_task(memory, context, worker, kind="squad_base_defense", target=core.position, intent=None)
                intents.append(_wait(worker, "squad_base_defense_worker_hold"))
            else:
                intent = _move(
                    worker, core.position, "squad_base_defense_worker_return", 520,
                    context=context, memory=memory, reservations=reservations,
                    deadline=deadline, config=config, avoid_threats=True,
                )
                _record_unit_task(memory, context, worker, kind="squad_base_defense", target=core.position, intent=intent)
                intents.append(intent or _wait(worker, "squad_base_defense_worker_blocked"))
            continue
        if worker.id in expedition_worker_ids:
            intent = _move(
                worker, context.beacon.position, "squad_expedition_worker_advance", 500,
                context=context, memory=memory, reservations=reservations,
                deadline=deadline, config=config, avoid_threats=True,
            )
            _record_unit_task(memory, context, worker, kind="expedition_beacon", target=context.beacon.position, intent=intent)
            intents.append(intent or _wait(worker, "squad_expedition_worker_blocked"))
            continue

        target = resource_assignments.get(str(worker.id))
        if target is not None and worker.position == target:
            intents.append(ActionIntent(
                actor_id=worker.id,
                is_core=False,
                action=ActionKind.HARVEST,
                score=900,
                reason="assigned_visible_resource",
                target_cell=target,
            ))
            _record_unit_task(memory, context, worker, kind="resource", target=target, intent=None)
            continue
        if target is not None:
            target_is_visible = target in context.resource_cells
            intent = _move(
                worker,
                target,
                "move_to_unique_resource" if target_is_visible else "continue_locked_resource_route",
                750,
                context=context,
                memory=memory,
                reservations=reservations,
                deadline=deadline,
                config=config,
                avoid_threats=True,
            )
            _record_unit_task(memory, context, worker, kind="resource", target=target, intent=intent)
            if intent is not None:
                intents.append(intent)
                continue
            if (
                context.core is not None
                and distance(worker.position, context.core.position) <= 1
                and distance(worker.position, target) > 1
            ):
                intent = _deploy_sidestep(
                    worker, target, context, memory, reservations,
                    "resource_route_sidestep", context.core.position,
                )
                if intent is not None:
                    _record_unit_task(memory, context, worker, kind="resource", target=target, intent=intent)
                    intents.append(intent)
                    continue
            if not (_at_normal_core(worker, context) and not cargo):
                intents.append(_wait(worker, "resource_route_blocked"))
                continue

        target = reconnaissance.get(str(worker.id))
        reason = "reobserve_remembered_resource"
        if target is None:
            target = exploration.get(str(worker.id))
            reason = "explore_sector_frontier"
        if context.core is not None and _at_normal_core(worker, context) and not cargo:
            occupied = dict(context.enemy_occupancy)
            exit_cells = tuple(sorted(
                cell
                for cell in (destination(worker.position, direction) for direction in DIRECTIONS)
                if cell not in memory.obstacles and cell not in occupied
            ))
            vacate = next((cell for cell in exit_cells if reservations.can_reserve(cell)), None)
            if vacate is None:
                # A full (2/2) unique exit needs an explicit outward yield
                # chain.  With one occupant, ``can_reserve`` above already
                # permits the legal two-unit destination capacity.
                for exit_cell in exit_cells:
                    if _relieve_delivery_corridor(exit_cell):
                        vacate = exit_cell
                        break
            if vacate is not None:
                intent = _move(
                    worker, vacate, "vacate_core_cell_for_delivery", 420,
                    context=context, memory=memory, reservations=reservations,
                    deadline=deadline, config=config,
                )
                if intent is not None:
                    _record_unit_task(memory, context, worker, kind="vacate", target=vacate, intent=intent)
                    intents.append(intent)
                    continue
            intents.append(_wait(worker, "core_cell_vacate_blocked"))
            continue

        if (
            not cargo
            and target is not None
            and reason == "explore_sector_frontier"
            and context.core is not None
            and distance(worker.position, context.core.position) <= 1
        ):
            frontier_cells = memory.frontier() - worker_blocks
            if not frontier_cells:
                target = None
        if target is not None:
            task_kind = "recon" if reconnaissance.get(str(worker.id)) else "explore"
            intent = _move(
                worker, target, reason, 400,
                context=context, memory=memory, reservations=reservations,
                deadline=deadline, config=config, avoid_threats=True,
            )
            if (
                intent is None
                and context.core is not None
                and distance(worker.position, context.core.position) <= 1
                and distance(worker.position, target) > 1
            ):
                intent = _deploy_sidestep(
                    worker, target, context, memory, reservations,
                    reason + "_sidestep", context.core.position,
                )
            _record_unit_task(memory, context, worker, kind=task_kind, target=target, intent=intent)
            intents.append(intent or _wait(worker, "exploration_route_blocked"))
            continue

        if context.core is not None and distance(worker.position, context.core.position) <= 1:
            yielded = False
            threats = enemy_threat_cells(context)
            for direction in DIRECTIONS:
                step_cell = destination(worker.position, direction)
                if (
                    step_cell != context.core.position
                    and step_cell not in memory.obstacles
                    and step_cell not in memory.active_temporary_blocks(context.tick)
                    and step_cell not in context.enemy_occupancy
                    and step_cell not in threats
                    and reservations.reserve(step_cell, source=worker.position)
                ):
                    intents.append(ActionIntent(
                        actor_id=worker.id,
                        is_core=False,
                        action=ActionKind.MOVE,
                        score=350,
                        reason="yield_core_doorstep_idle",
                        direction=direction,
                        target_cell=step_cell,
                        reserved_cell=step_cell,
                    ))
                    yielded = True
                    break
            if yielded:
                continue

        intents.append(_wait(worker, "no_resource_or_frontier"))
    return intents
