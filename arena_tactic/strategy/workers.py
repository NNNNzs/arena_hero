"""Worker harvesting, return, and frontier plans."""

from __future__ import annotations

from typing import Iterable
from uuid import UUID

from arena_hero import CoreState, UnitType, UnitView

from ..context import DecisionContext
from ..memory import AgentMemory
from ..models import ActionIntent, ActionKind, AgentConfig, Position, ReservationTable, StrategicMode
from ..navigation import DIRECTIONS, bounded_path_cost, destination, distance
from .common import (
    UNIT_MAX_HP,
    _EXPLORATION_SECTORS,
    _at_normal_core,
    _move,
    _record_unit_task,
    _return_to_core,
    _return_to_core_sidestep,
    _unit_heal_intent,
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
                # 滞留核心格的空载工人：优先把让位格作为探索投影目标，
                # 避免原地等待堵死核心入口（拥堵死锁）。
                (unit.position[0] + dx, unit.position[1] + dy),
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

        previous_target: Position | None = None
        if previous.get("kind") == task_kind and isinstance(
            previous.get("target"), list
        ):
            raw_target = previous["target"]
            if len(raw_target) == 2:
                previous_target = int(raw_target[0]), int(raw_target[1])

        # ── target lock: keep previous target as long as it is still on the
        # frontier and unassigned.  The sector timer no longer forces rotation;
        # the worker only re-picks when the target disappears / is taken.
        if previous_target in frontier and previous_target not in assigned:
            target = previous_target
        else:
            # Target lost → consider sector rotation (minimum-hold semantics).
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
                        continue
                    # Reachability pre-filter: skip targets whose path cost
                    # clearly exceeds the remaining sector hold window so we
                    # never pick a destination the worker cannot reach.
                    if path_cost > remaining_hold:
                        continue
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



def _plan_workers(
    context: DecisionContext,
    memory: AgentMemory,
    reservations: ReservationTable,
    deadline: float,
    config: AgentConfig,
    heal_allowances: dict[UUID, int],
) -> list[ActionIntent]:
    # Resolve via the compatibility package to preserve legacy monkeypatching.
    from . import _return_to_core as return_to_core

    intents: list[ActionIntent] = []
    core = context.core
    if core is None:
        return intents
    combat_ready = len(context.vanguards) >= 1 and len(context.rangers) >= 1
    if memory.last_mode is StrategicMode.DEFEND or _core_emergency_defense(context):
        # When combat ready, workers rally to Core to shelter behind combat units.
        # When NOT combat ready (0 combat units), empty workers MUST keep gathering resources,
        # only cargo workers return to deposit so we can fund combat unit production.
        for worker in sorted(context.workers, key=lambda unit: str(unit.id)):
            if (
                worker.cargo
                and core.state is CoreState.MOVING
                and memory.unit_tasks.get(str(worker.id), {}).get("kind")
                == "await_core_stationary"
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
                target = (
                    core.destination
                    if core.state is CoreState.MOVING and core.destination
                    else core.position
                )
                if intent is None:
                    intent = _return_to_core_sidestep(
                        worker,
                        target,
                        context,
                        memory,
                        reservations,
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
    worker_blocks = (
        memory.obstacles
        | memory.active_temporary_blocks(context.tick)
        | set(context.enemy_occupancy)
    )
    locked_resource_assignments = _locked_resource_targets(
        empty_workers, memory, context, config
    )
    resource_assignments = _assign_unique_targets(
        (
            worker
            for worker in empty_workers
            if str(worker.id) not in locked_resource_assignments
        ),
        set(context.resource_cells) - set(locked_resource_assignments.values()),
        blocked=worker_blocks,
        deadline=deadline,
        config=config,
    )
    resource_assignments = locked_resource_assignments | resource_assignments

    unassigned = [
        worker for worker in empty_workers if str(worker.id) not in resource_assignments
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
        still_unassigned,
        memory,
        context,
        deadline,
        config,
        task_kind="explore",
    )

    # 载货工人优先决策：先为有货的工人预约核心格与路径，
    # 避免空载探索工人占满核心入口导致返航死锁（拥堵互堵）。
    for worker in sorted(
        context.workers,
        key=lambda unit: (0 if (unit.cargo or 0) else 1, str(unit.id)),
    ):
        cargo = worker.cargo or 0
        if (
            cargo
            and core.state is CoreState.MOVING
            and memory.unit_tasks.get(str(worker.id), {}).get("kind")
            == "await_core_stationary"
        ):
            intents.append(_wait(worker, "deposit_waits_for_core_migration"))
            continue
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
            intent = return_to_core(
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
            target_is_visible = target in context.resource_cells
            intent = _move(
                worker,
                target,
                (
                    "move_to_unique_resource"
                    if target_is_visible
                    else "continue_locked_resource_route"
                ),
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
        if (
            context.core is not None
            and _at_normal_core(worker, context)
            and not cargo
        ):
            # 空载工人滞留核心格：核心格容量只有 2（核心+1），
            # 停在这里会堵死载货工人的存矿入口。主动让位到最近的
            # 非核心相邻空格，把入口让给返航的载货同伴。
            # 此分支优先于探索目标：站在核心格上时让位是第一优先级，
            # 让完位后下一 Tick 自然接续正常探索/复查任务。
            # 让位候选格用引擎容量语义（ReservationTable：每格至多 2 人）
            # 判定，而不是"必须完全无人"。此前只要格上有 1 个友军就
            # 被排除，导致核心格唯一的近出口（站了 1 个游侠、还剩 1 个
            # 空位）被白白丢弃，空载工让不出去、门口的载货工进不来，
            # 双方在核心门口互相等死（core_cell_vacate_blocked /
            # no_safe_route_with_cargo 同时持续）。
            occupied = dict(context.enemy_occupancy)
            reservations = ReservationTable(
                {
                    cell: len(ids) - 1  # 扣掉自己：自己即将离开当前格
                    if cell == worker.position
                    else len(ids)
                    for cell, ids in context.friendly_occupancy.items()
                    if cell != context.core.position  # 核心自身不算让位障碍
                }
            )
            vacate = next(
                (
                    cell
                    for cell in sorted(
                        destination(worker.position, d)
                        for d in DIRECTIONS
                    )
                    if cell not in memory.obstacles
                    and cell not in occupied
                    and reservations.can_reserve(cell)
                ),
                None,
            )
            if vacate is not None:
                intent = _move(
                    worker, vacate, "vacate_core_cell_for_delivery", 420,
                    context=context, memory=memory,
                    reservations=reservations, deadline=deadline, config=config,
                )
                if intent is not None:
                    _record_unit_task(
                        memory, context, worker, kind="vacate",
                        target=vacate, intent=intent,
                    )
                    intents.append(intent)
                    continue
            intents.append(_wait(worker, "core_cell_vacate_blocked"))
            continue
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

