"""Worker harvesting, return, and frontier plans."""

from __future__ import annotations

from collections import defaultdict
import heapq
from typing import Iterable
from time import perf_counter
from uuid import UUID

from arena_hero import CoreState, Direction, UnitType, UnitView

from ..context import DecisionContext
from ..memory import AgentMemory
from ..models import ActionIntent, ActionKind, AgentConfig, Position, ReservationTable
from ..navigation import DIRECTIONS, adjacent_direction, bounded_path_cost, destination, distance, enemy_threat_cells
from ..squads import SquadPlan, SquadType
from .common import (
    UNIT_MAX_HP,
    _EXPLORATION_SECTORS,
    _at_normal_core,
    _critical_retreat_sidestep,
    _deploy_sidestep,
    _evacuate_doorstep_intent,
    _move,
    _record_unit_task,
    _return_to_core,
    _return_to_core_sidestep,
    _unit_heal_intent,
    _unit_needs_retreat_heal,
    _unit_retreat_to_core,
    _wait,
)
from .combat import _enemy_can_attack_core
from .mode import _core_emergency_defense


_STUCK_THRESHOLD = 3  # consecutive blocked ticks before sidestep activates


def _stuck_sidestep(
    worker: UnitView,
    target: Position,
    context: DecisionContext,
    memory: AgentMemory,
    reservations: ReservationTable,
    reason: str,
) -> ActionIntent | None:
    """Break a stationary deadlock by stepping to any adjacent free cell.

    When a worker has been stuck for multiple consecutive ticks trying to
    reach *target*, this sidestep picks any legal adjacent cell — preferring
    outward movement and avoiding the previous cell — to free up the current
    position for other units in a congested corridor.
    """
    task = memory.unit_tasks.get(str(worker.id), {})
    prev_cell_raw = task.get("prev_cell")
    prev_cell = tuple(prev_cell_raw) if isinstance(prev_cell_raw, (list, tuple)) and len(prev_cell_raw) == 2 else None
    # Only activate after the worker has been stuck for a few ticks.
    attempt_tick = task.get("attempt_tick")
    if attempt_tick is None or context.tick - attempt_tick < _STUCK_THRESHOLD:
        return None
    blocked = (
        memory.obstacles
        | memory.active_temporary_blocks(context.tick)
        | set(context.enemy_occupancy)
        | enemy_threat_cells(context)
    )
    candidates = []
    for direction in DIRECTIONS:
        cand = destination(worker.position, direction)
        if cand in blocked:
            continue
        dist_to_target = distance(cand, target)
        backtrack_penalty = 5000 if prev_cell is not None and cand == prev_cell else 0
        candidates.append((backtrack_penalty + dist_to_target, direction, cand))
    candidates.sort(key=lambda x: (x[0], x[1].value))
    for _, direction, cand in candidates:
        if reservations.reserve(cand, source=worker.position):
            return ActionIntent(
                worker.id,
                False,
                ActionKind.MOVE,
                350,
                reason,
                target_cell=target,
                direction=direction,
                reserved_cell=cand,
            )
    return None


def _workers_need_local_shelter(context: DecisionContext, memory: AgentMemory, config: AgentConfig) -> bool:
    """Keep Workers safe from current local facts, not a remembered macro mode."""
    core = context.core
    return core is not None and (
        _core_emergency_defense(context)
        or any(
            _enemy_can_attack_core(enemy, core, memory.obstacles)
            or distance(enemy.position, core.position) <= config.intercept_distance
            for enemy in context.enemies
        )
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
    target_set = set(targets) - blocked
    if len(target_set) > 8 and units:
        candidate_pool: set[Position] = set()
        for unit in units:
            for t in sorted(target_set, key=lambda p: distance(unit.position, p))[:6]:
                candidate_pool.add(t)
        targets = tuple(candidate_pool)
    else:
        targets = tuple(target_set)
    candidates: list[tuple[int, str, Position, UnitView]] = []
    for unit in units:
        for target in targets:
            if perf_counter() >= deadline:
                break
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

    # 空间网格索引：加速大规模迷雾地图下的候选点筛选（64x64 空间分桶）
    _BUCKET_SIZE = 64
    frontier_buckets: dict[tuple[int, int], list[Position]] = defaultdict(list)
    for c in frontier:
        frontier_buckets[(c[0] // _BUCKET_SIZE, c[1] // _BUCKET_SIZE)].append(c)

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
            ux, uy = unit.position
            ucx, ucy = ux // _BUCKET_SIZE, uy // _BUCKET_SIZE

            # 优先在单位周围 5x5 chunks 检索未分配的局部候选点
            local_cells: list[Position] = []
            for dcx in range(-2, 3):
                for dcy in range(-2, 3):
                    local_cells.extend(frontier_buckets.get((ucx + dcx, ucy + dcy), ()))
            use_cells = [c for c in local_cells if c not in assigned]
            if not use_cells:
                use_cells = [c for c in frontier if c not in assigned]

            for rotation in range(len(_EXPLORATION_SECTORS)):
                candidate_sector = (sector + rotation) % len(_EXPLORATION_SECTORS)
                vector_x, vector_y = _EXPLORATION_SECTORS[candidate_sector]
                geometric_candidates: list[tuple[int, int, int, Position]] = []
                for cell in use_cells:
                    delta_x = cell[0] - ux
                    delta_y = cell[1] - uy
                    projection = delta_x * vector_x + delta_y * vector_y
                    if projection <= 0:
                        continue
                    lateral = abs(delta_x * vector_y - delta_y * vector_x)
                    d = abs(delta_x) + abs(delta_y)
                    geometric_candidates.append((
                        d + lateral,
                        lateral,
                        -projection,
                        cell,
                    ))

                if not geometric_candidates and len(use_cells) != len(frontier):
                    # 若局部区域在该扇区方向无候选，兜底在全量 frontier 中检索
                    for cell in frontier:
                        if cell in assigned:
                            continue
                        delta_x = cell[0] - ux
                        delta_y = cell[1] - uy
                        projection = delta_x * vector_x + delta_y * vector_y
                        if projection <= 0:
                            continue
                        lateral = abs(delta_x * vector_y - delta_y * vector_x)
                        d = abs(delta_x) + abs(delta_y)
                        geometric_candidates.append((
                            d + lateral,
                            lateral,
                            -projection,
                            cell,
                        ))

                candidates: list[tuple[int, int, int, Position]] = []
                fallback_candidates: list[tuple[int, int, int, Position]] = []
                for _, lateral, negative_projection, cell in heapq.nsmallest(6, geometric_candidates):
                    if perf_counter() >= deadline:
                        break
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
    if _workers_need_local_shelter(context, memory, config):
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
    recon_deadline = min(deadline, perf_counter() + 0.05)
    reconnaissance = _assign_unique_targets(
        recheck_workers,
        remembered_targets,
        blocked=worker_blocks,
        deadline=recon_deadline,
        config=config,
    )
    still_unassigned = [
        worker for worker in unassigned if str(worker.id) not in reconnaissance
    ]
    explore_deadline = min(deadline, perf_counter() + 0.05)
    exploration = _frontier_assignments(
        tuple(still_unassigned) + scout_workers,
        memory,
        context,
        explore_deadline,
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
            occupants,
            key=lambda worker: (0 if not worker.cargo else 1, worker.id.bytes),
        ))
        if not candidate_workers:
            return False
        threats = enemy_threat_cells(context)
        for worker in candidate_workers:
            choices: list[tuple[int, int, int, int, Position, Direction]] = []
            for direction_index, direction in enumerate(DIRECTIONS):
                target = destination(cell, direction)
                if (
                    target == core_pos
                    or target in memory.obstacles
                    or target in memory.active_temporary_blocks(context.tick)
                    or target in context.enemy_occupancy
                    or target in threats
                ):
                    continue
                # Avoid yielding into a dead-end pocket where unit will just bounce back next tick
                other_exits = sum(
                    1 for d in DIRECTIONS
                    if destination(target, d) != cell
                    and destination(target, d) != core_pos
                    and destination(target, d) not in memory.obstacles
                    and destination(target, d) not in memory.active_temporary_blocks(context.tick)
                    and destination(target, d) not in context.enemy_occupancy
                    and destination(target, d) not in threats
                )
                if other_exits == 0:
                    continue
                # Prefer outward travel, then side lanes.  A recursive yield
                # permits a packed westbound queue to shed one unit per cell.
                occupancy = len(context.friendly_occupancy.get(target, ()))
                outward_penalty = 0 if distance(target, core_pos) > distance(cell, core_pos) else 1
                choices.append((
                    occupancy,
                    outward_penalty,
                    distance(target, core_pos),
                    direction_index,
                    target,
                    direction,
                ))
            for _, _, _, _, target, direction in sorted(choices):
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
        if worker.hp < UNIT_MAX_HP[UnitType.WORKER] and _at_normal_core(worker, context):
            heal_cost = heal_allowances.get(worker.id, 0)
            intents.append(_unit_heal_intent(worker, heal_cost) if heal_cost > 0 else _wait(worker, "healing_waits_for_resources"))
            continue
        if not cargo and context.core is not None and _at_normal_core(worker, context):
            occupied = dict(context.enemy_occupancy)
            exit_cells = tuple(sorted(
                (
                    destination(worker.position, direction)
                    for direction in DIRECTIONS
                    if destination(worker.position, direction) not in memory.obstacles
                    and destination(worker.position, direction) not in occupied
                ),
                key=lambda cell: (len(context.friendly_occupancy.get(cell, ())), cell),
            ))
            vacate = next((cell for cell in exit_cells if reservations.can_reserve(cell)), None)
            if vacate is None:
                # A full (2/2) unique exit needs an explicit outward yield chain.
                for exit_cell in exit_cells:
                    if _relieve_delivery_corridor(exit_cell):
                        vacate = exit_cell
                        break
            if vacate is not None:
                if reservations.reserve(vacate, source=worker.position):
                    direction = next(d for d in DIRECTIONS if destination(worker.position, d) == vacate)
                    intent = ActionIntent(
                        actor_id=worker.id,
                        is_core=False,
                        action=ActionKind.MOVE,
                        score=720,
                        reason="vacate_core_cell_for_delivery",
                        direction=direction,
                        target_cell=vacate,
                        reserved_cell=vacate,
                    )
                    _record_unit_task(memory, context, worker, kind="vacate", target=vacate, intent=intent)
                    intents.append(intent)
                    continue
            intents.append(_wait(worker, "core_cell_vacate_blocked"))
            continue
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
            return_target = core.destination if core.state is CoreState.MOVING and core.destination else core.position
            intent = None
            if (
                distance(worker.position, return_target) == 1
                and return_target not in memory.obstacles
                and return_target not in context.enemy_occupancy
                and reservations.reserve(return_target, source=worker.position)
            ):
                direction = adjacent_direction(worker.position, return_target)
                if direction is not None:
                    intent = ActionIntent(
                        actor_id=worker.id,
                        is_core=False,
                        action=ActionKind.MOVE,
                        score=700,
                        reason="return_cargo_to_core",
                        target_cell=return_target,
                        direction=direction,
                        reserved_cell=return_target,
                    )
            if intent is None:
                intent = return_to_core(
                    worker, context, memory, reservations, deadline, config,
                    "return_cargo_to_core",
                )
            if intent is None:
                if distance(worker.position, return_target) > 1:
                    intent = _return_to_core_sidestep(
                        worker, return_target, context, memory, reservations,
                        "return_cargo_to_core", minimum_distance=1,
                    )
                # Cargo worker at distance == 1: hold position and wait for
                # the core cell to become available.  Yielding away (to
                # distance 2) caused a ping-pong oscillation between
                # yield_doorstep_congestion and _return_to_core_sidestep,
                # permanently blocking cargo delivery.
            _record_unit_task(memory, context, worker, kind="return", target=return_target, intent=intent)
            if intent is None and distance(worker.position, return_target) <= 1:
                wait_reason = "cargo_doorstep_wait_for_entry"
            else:
                wait_reason = "no_safe_route_with_cargo"
            intents.append(intent or _wait(worker, wait_reason))
            continue

        if (
            _unit_needs_retreat_heal(worker, memory, config)
            and not (_core_emergency_defense(context) and not combat_ready and not cargo)
        ):
            intent = _unit_retreat_to_core(worker, context, memory, reservations, deadline, config)
            if intent is None:
                intent = _critical_retreat_sidestep(
                    worker, context, memory, reservations,
                    "retreat_heal_sidestep",
                )
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
            # When stuck for multiple consecutive ticks, attempt a local
            # sidestep to any adjacent free cell to break deadlock.
            intent = _stuck_sidestep(worker, target, context, memory, reservations, "resource_route_unblock")
            if intent is not None:
                _record_unit_task(memory, context, worker, kind="resource", target=target, intent=intent)
                intents.append(intent)
                continue
            # Stuck on a resource route — try an alternative visible resource
            # cell that is not already locked by another worker, to avoid all
            # 12 workers stalling on the same blocked path.
            if not cargo:
                assigned_targets = set(resource_assignments.values())
                alt_target = min(
                    (
                        cell for cell in context.resource_cells
                        if cell != target and cell not in assigned_targets
                    ),
                    key=lambda cell: distance(worker.position, cell),
                    default=None,
                )
                if alt_target is not None:
                    intent = _move(
                        worker, alt_target, "resource_route_alt_cell", 750,
                        context=context, memory=memory, reservations=reservations,
                        deadline=deadline, config=config, avoid_threats=True,
                    )
                    if intent is not None:
                        _record_unit_task(memory, context, worker, kind="resource", target=alt_target, intent=intent)
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
                and distance(worker.position, context.core.position) <= max(3, config.cargo_delivery_yield_radius)
                and distance(worker.position, target) > 1
            ):
                intent = _deploy_sidestep(
                    worker, target, context, memory, reservations,
                    reason + "_sidestep", context.core.position,
                )
            if intent is None and context.core is not None and distance(worker.position, context.core.position) <= max(3, config.cargo_delivery_yield_radius):
                intent = _evacuate_doorstep_intent(
                    worker, context, memory, reservations, "yield_core_doorstep_blocked", max_radius=max(3, config.cargo_delivery_yield_radius)
                )
            # When stuck for multiple consecutive ticks, attempt a local
            # sidestep to any adjacent free cell to break deadlock.
            if intent is None:
                intent = _stuck_sidestep(worker, target, context, memory, reservations, "exploration_route_unblock")
            _record_unit_task(memory, context, worker, kind=task_kind, target=target, intent=intent)
            intents.append(intent or _wait(worker, "exploration_route_blocked"))
            continue

        if context.core is not None and distance(worker.position, context.core.position) <= max(3, config.cargo_delivery_yield_radius):
            intent = _evacuate_doorstep_intent(
                worker, context, memory, reservations, "yield_core_doorstep_idle", max_radius=max(3, config.cargo_delivery_yield_radius)
            )
            if intent is not None:
                intents.append(intent)
                continue

        intents.append(_wait(worker, "no_resource_or_frontier"))
    return intents
