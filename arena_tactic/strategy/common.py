"""Shared strategy planning primitives."""

from __future__ import annotations

from time import perf_counter
from typing import Iterable
from uuid import UUID

from arena_hero import CoreState, CoreView, Direction, UnitType, UnitView

from ..context import DecisionContext
from ..identity import entity_alias
from ..memory import AgentMemory
from ..memory import _resolve_unit_task
from ..models import ActionIntent, ActionKind, AgentConfig, Position, ReservationTable
from ..navigation import DIRECTIONS, bounded_path_cost, destination, distance, enemy_threat_cells, plan_step, shot_range
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
    _existing_key, _existing_task = _resolve_unit_task(memory.unit_tasks, str(unit.id))
    existing = _existing_task or {}
    task = dict(existing) if existing.get("kind") == kind else {
        key: existing[key]
        for key in ("patrol_arc", "patrol_role", "patrol_core", "recent_cells", "prev_cell", "recon_since", "intercept_since", "engage_since")
        if key in existing
    }
    task.update({"kind": kind, "target": list(target)})
    if kind == "recon":
        if existing.get("kind") == "recon" and existing.get("target") == list(target) and "recon_since" in existing:
            task["recon_since"] = existing["recon_since"]
        else:
            task["recon_since"] = context.tick
    if kind == "intercept":
        if existing.get("kind") == "intercept" and "intercept_since" in existing:
            task["intercept_since"] = existing["intercept_since"]
        else:
            task["intercept_since"] = context.tick
    if kind == "engage_firing_line":
        if existing.get("kind") == "engage_firing_line" and "engage_since" in existing:
            task["engage_since"] = existing["engage_since"]
        else:
            task["engage_since"] = context.tick
    task["prev_cell"] = list(unit.position)
    if intent is not None and intent.action is ActionKind.MOVE:
        task["step"] = list(intent.reserved_cell) if intent.reserved_cell else None
        task["attempt_tick"] = context.tick
    else:
        task.pop("step", None)
        # Preserve attempt_tick so _stuck_sidestep can activate after
        # _STUCK_THRESHOLD consecutive blocked ticks.  When a move fails
        # (intent is None), keep the earliest blocked tick; if this is the
        # first failure, initialise to the current tick.
        if "attempt_tick" not in task:
            task["attempt_tick"] = context.tick
    memory.unit_tasks[str(unit.id)] = task
    alias = entity_alias(unit.id)
    if alias:
        memory.unit_tasks[alias] = task


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
    # Collect all core-adjacent passable exits (doorstep cells) so that guard
    # slots never overlap them.  Units stationed on a doorstep conflict with
    # ``_evacuate_doorstep_intent`` which drives them off — causing a 2-Tick
    # ping-pong oscillation between guard-holding and doorstep evacuation.
    passable_exits: set[Position] = set(passable_r1)
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
        if cell not in memory.obstacles
        and cell not in passable_exits
        and cell not in context.enemy_occupancy
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
    """Make safe local progress when a fog-distance route home cannot be planned.

    Uses a multi-tier taboo penalty on recently visited cells (``recent_cells``)
    to break 3–4 cell oscillation loops that the legacy single ``prev_cell``
    heuristic could not prevent.  Cells visited within the last 4 ticks receive
    progressively heavier penalties (most-recent first); the unit is forced to
    explore a direction it has not recently traversed.
    """
    blocked = (
        memory.obstacles
        | memory.active_temporary_blocks(context.tick)
        | set(context.obstacle_cells)
        | set(context.enemy_occupancy)
    )
    threats = enemy_threat_cells(context)

    # Load recent-cell history for multi-tier anti-oscillation.
    existing_task = memory.unit_tasks.get(str(unit.id), {})
    recent_raw = existing_task.get("recent_cells")
    recent_cells: list[Position] = []
    if isinstance(recent_raw, list):
        for item in recent_raw:
            if isinstance(item, (list, tuple)) and len(item) == 2 and all(type(p) is int for p in item):
                recent_cells.append((int(item[0]), int(item[1])))
    # Backward compatibility: migrate legacy single-step prev_cell.
    if not recent_cells:
        prev_raw = existing_task.get("prev_cell")
        if isinstance(prev_raw, (list, tuple)) and len(prev_raw) == 2 and all(type(p) is int for p in prev_raw):
            recent_cells.append((int(prev_raw[0]), int(prev_raw[1])))

    # Build a recency penalty map: most recent visit → highest penalty.
    # Reversed so index 0 = most recent visit → penalty 600; older steps halve the weight.
    _TABOO_BASE = 600
    taboo: dict[Position, int] = {}
    for idx, cell in enumerate(reversed(recent_cells)):
        penalty = _TABOO_BASE >> idx  # 600, 300, 150, 75, …
        if penalty > 0:
            taboo[cell] = max(taboo.get(cell, 0), penalty)

    candidates = [
        (destination(unit.position, direction), direction)
        for direction in DIRECTIONS
        if destination(unit.position, direction) not in blocked
    ]
    # Prefer an unthreatened cell, then one that still trends home.  The strong
    # multi-tier anti-backtrack penalty prevents blocked long-haul routes from
    # becoming 3–4 cell oscillation loops while new terrain is revealed.
    candidates.sort(key=lambda item: (
        item[0] in threats,
        taboo.get(item[0], 0),
        distance(item[0], target),
        item[1].value,
    ))
    for cell, direction in candidates:
        if reservations.reserve(cell, source=unit.position):
            # Append current position and cap at 5 recent entries.
            new_recent = [*recent_cells, unit.position][-5:]
            memory.unit_tasks[str(unit.id)] = {
                "kind": "distant_retreat_fallback",
                "target": list(target),
                "prev_cell": list(unit.position),
                "recent_cells": [list(c) for c in new_recent],
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
    # Anti-oscillation: read previous cell from task history.  When the unit
    # has been bouncing between two cells (prev_cell == current position's
    # neighbour that was just visited), the safe path with avoid_threats=True
    # creates a local-minimum loop at threat boundaries.  Detect the 2-cell
    # cycle and skip directly to the unsafe fallback path.
    existing_task = memory.unit_tasks.get(str(unit.id), {})
    prev_cell_raw = existing_task.get("prev_cell")
    prev_cell = tuple(prev_cell_raw) if isinstance(prev_cell_raw, (list, tuple)) and len(prev_cell_raw) == 2 else None
    oscillation_count = existing_task.get("oscillation_count", 0)

    # B: 优先走安全路径；若被威胁格封死则降级为普通路径强行回核心
    # Skip safe path when oscillation is detected (count >= 2 means we've
    # been bouncing for at least 2 consecutive ticks).
    if oscillation_count < 2:
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
            # Check if this step would return to the previous cell (2-cell cycle).
            if prev_cell is not None and intent.reserved_cell == prev_cell:
                # Record oscillation detection but still try the safe path
                # once more — only break out after 2 consecutive detections.
                new_task = dict(existing_task)
                new_task["oscillation_count"] = oscillation_count + 1
                new_task["kind"] = reason
                memory.unit_tasks[str(unit.id)] = new_task
                alias = entity_alias(unit.id)
                if alias:
                    memory.unit_tasks[alias] = new_task
            else:
                # Normal movement — reset oscillation counter.
                if oscillation_count > 0:
                    new_task = dict(existing_task)
                    new_task["oscillation_count"] = 0
                    memory.unit_tasks[str(unit.id)] = new_task
                    alias = entity_alias(unit.id)
                    if alias:
                        memory.unit_tasks[alias] = new_task
            return intent
    # 安全路径不通或振荡检测触发，降级为忽略威胁的普通路径
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
        # Reset oscillation counter on successful unsafe fallback.
        if oscillation_count > 0:
            new_task = dict(existing_task)
            new_task["oscillation_count"] = 0
            new_task["kind"] = reason
            memory.unit_tasks[str(unit.id)] = new_task
            alias = entity_alias(unit.id)
            if alias:
                memory.unit_tasks[alias] = new_task
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

    scored_candidates: list[tuple[int, Direction, Position]] = []
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

    # Fallback: when all forward/lateral cells are blocked, allow stepping
    # backward (distance +1) to break deadlock in concave/chokepoint terrain
    # where idle workers block all cells closer to the target.
    if not scored_candidates:
        for direction in DIRECTIONS:
            cand = destination(unit.position, direction)
            if cand in blocked:
                continue
            dist = distance(cand, target)
            if dist > current_distance + 1:
                continue
            score = dist * 10 + 10000  # heavy penalty for backward movement
            if prev_cell is not None and cand == prev_cell:
                score += 5000  # anti-oscillation: penalise returning to previous cell
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


def _critical_retreat_sidestep(
    unit: UnitView,
    context: DecisionContext,
    memory: AgentMemory,
    reservations: ReservationTable,
    reason: str,
) -> ActionIntent | None:
    """Emergency sidestep for a critically wounded unit whose direct retreat
    and doorstep evacuation have both failed.

    Picks any adjacent free cell — preferring toward the Core but accepting
    any passable cell — to unblock the core entrance.  Anti-oscillation
    penalises returning to the previous cell.
    """
    if context.core is None:
        return None
    target = (
        context.core.destination
        if context.core.state is CoreState.MOVING and context.core.destination
        else context.core.position
    )
    blocked = (
        memory.obstacles
        | memory.active_temporary_blocks(context.tick)
        | set(context.enemy_occupancy)
        | enemy_threat_cells(context)
    )
    existing_task = memory.unit_tasks.get(str(unit.id), {})
    prev_cell_raw = existing_task.get("prev_cell")
    prev_cell = tuple(prev_cell_raw) if isinstance(prev_cell_raw, (list, tuple)) and len(prev_cell_raw) == 2 else None

    candidates: list[tuple[int, int, Position, Direction]] = []
    for direction_idx, direction in enumerate(DIRECTIONS):
        cand = destination(unit.position, direction)
        if cand in blocked:
            continue
        dist_to_core = distance(cand, target)
        dist_now = distance(unit.position, target)
        # Prefer toward-core cells, then lateral, then outward.
        toward_penalty = 0 if dist_to_core < dist_now else (1 if dist_to_core == dist_now else 2)
        backtrack_penalty = 5000 if prev_cell is not None and cand == prev_cell else 0
        candidates.append((toward_penalty + backtrack_penalty, direction_idx, cand, direction))

    candidates.sort()
    # If every candidate carries the backtrack penalty, refuse to oscillate.
    if candidates and candidates[0][0] >= 5000:
        return None
    for _, _, cand, direction in candidates:
        if reservations.reserve(cand, source=unit.position):
            _record_unit_task(memory, context, unit, kind="critical_retreat_sidestep", target=cand, intent=ActionIntent(
                actor_id=unit.id,
                is_core=False,
                action=ActionKind.MOVE,
                score=755,
                reason=reason,
                direction=direction,
                target_cell=cand,
                reserved_cell=cand,
            ))
            return ActionIntent(
                actor_id=unit.id,
                is_core=False,
                action=ActionKind.MOVE,
                score=755,
                reason=reason,
                direction=direction,
                target_cell=cand,
                reserved_cell=cand,
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


def _firing_line_sidestep(
    ranger: UnitView,
    enemy: CoreView | UnitView,
    staging: Position,
    context: DecisionContext,
    memory: AgentMemory,
    reservations: ReservationTable,
    reason: str,
) -> ActionIntent | None:
    """Try an adjacent cell when the primary staging route is blocked.

    When ``_move(ranger, staging, ...)`` returns *None* the ranger is about to
    stand still and wait indefinitely.  This helper inspects the four adjacent
    cells and picks one that:

    1. is not blocked by obstacles, enemies, or temporary blocks,
    2. **preferably** has a clear firing line (``shot_range``) to *enemy*,
    3. otherwise moves closer to *staging* so the ranger can try again next
       tick.

    Anti-oscillation: penalises returning to ``prev_cell``.
    """
    blocked = (
        memory.obstacles
        | memory.active_temporary_blocks(context.tick)
        | set(context.obstacle_cells)
        | set(context.enemy_occupancy)
    )
    enemy_pos = enemy.position
    existing_task = memory.unit_tasks.get(str(ranger.id), {})
    prev_cell_raw = existing_task.get("prev_cell")
    prev_cell = (
        tuple(prev_cell_raw)
        if isinstance(prev_cell_raw, (list, tuple)) and len(prev_cell_raw) == 2
        else None
    )
    current_dist_to_staging = distance(ranger.position, staging)

    candidates: list[tuple[int, int, int, Direction, Position]] = []
    for direction_idx, direction in enumerate(DIRECTIONS):
        cand = destination(ranger.position, direction)
        if cand in blocked:
            continue
        has_shot = shot_range(cand, enemy_pos, memory.obstacles) is not None
        dist_to_staging = distance(cand, staging)
        # Tier 0: has firing line AND closer/equal to staging
        # Tier 1: has firing line but further from staging
        # Tier 2: no firing line but closer/equal to staging
        # Tier 3: no firing line and further from staging
        if has_shot:
            tier = 0 if dist_to_staging <= current_dist_to_staging else 1
        else:
            tier = 2 if dist_to_staging <= current_dist_to_staging else 3
        is_backtrack = 1 if prev_cell is not None and cand == prev_cell else 0
        score = tier * 10000 + dist_to_staging * 10 + is_backtrack * 5000
        candidates.append((score, direction_idx, is_backtrack, direction, cand))

    # If *every* non-blocked candidate would bounce back to prev_cell,
    # refuse to oscillate — drop those and keep only non-backtrack cells.
    if candidates and all(c[2] for c in candidates):
        candidates = [c for c in candidates if not c[2]]

    candidates.sort()
    for _, _, _, direction, cand in candidates:
        if reservations.reserve(cand, source=ranger.position):
            _record_unit_task(
                memory, context, ranger, kind="firing_sidestep",
                target=enemy_pos, intent=ActionIntent(
                    actor_id=ranger.id, is_core=False, action=ActionKind.MOVE,
                    score=625, reason=reason, direction=direction,
                    target_cell=staging, reserved_cell=cand,
                ),
            )
            return ActionIntent(
                actor_id=ranger.id, is_core=False, action=ActionKind.MOVE,
                score=625, reason=reason, direction=direction,
                target_cell=staging, reserved_cell=cand,
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

    Additionally, when the combat unit's yield fails because the only exit
    corridor is blocked by cargo workers waiting at the doorstep
    (``cargo_doorstep_wait_for_entry``), this function forces those cargo
    workers to yield outward, breaking the deterministic swap deadlock in
    single-exit pocket terrain.
    """
    if context.core is None:
        return intents
    core_position = context.core.position
    combat_ids = {unit.id for unit in (*context.rangers, *context.vanguards)}
    changed = False
    combat_stuck_on_core = False
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
                # Combat unit on core could not yield — flag for doorstep
                # corridor relief below.
                combat_stuck_on_core = True
        result.append(intent)

    # Pocket deadlock relief: when a combat unit is stuck on the core and
    # cargo workers are blocking the only exit by waiting at the doorstep,
    # force those cargo workers to yield outward so the combat unit can
    # leave on the next tick.
    if combat_stuck_on_core:
        result = _yield_cargo_doorstep_for_combat(
            result, context, memory, reservations, config,
        )
    return result


def _yield_cargo_doorstep_for_combat(
    intents: list[ActionIntent],
    context: DecisionContext,
    memory: AgentMemory,
    reservations: ReservationTable,
    config: AgentConfig,
) -> list[ActionIntent]:
    """Force cargo workers at the doorstep to yield outward for a stuck combat unit.

    In a single-exit pocket terrain, the following deadlock can occur:
    - Core cell is full (CORE + combat unit, 2/2)
    - The only exit is blocked by cargo workers waiting to enter (2/2)
    - The combat unit cannot ``_deploy_sidestep`` because all exits are full
    - Cargo workers won't move because they're waiting for core space

    This function detects the pattern and replaces ``cargo_doorstep_wait_for_entry``
    WAIT intents for cargo workers adjacent to the core with outward MOVE intents,
    breaking the deterministic swap deadlock.

    **Cascade evacuation**: When distance-1 doorstep workers cannot yield because
    all adjacent cells are saturated (2/2 each, as in high-density corridor
    congestion), outer-ring workers (distance 2–3) are evacuated further outward
    first to create space.  This is essential for pocket terrain with a long
    packed corridor where 10+ cargo workers queue up at distance 1–3.
    """
    if context.core is None:
        return intents
    core_position = context.core.position
    threats = enemy_threat_cells(context)
    blocked = (
        memory.obstacles
        | memory.active_temporary_blocks(context.tick)
        | set(context.enemy_occupancy)
        | threats
    )

    # Pre-pass: cascade evacuation of outer-ring cargo workers (distance 2-3)
    # to free cells for distance-1 doorstep workers.
    result = _cascade_corridor_evacuation(
        intents, context, memory, reservations, blocked, core_position,
    )

    final: list[ActionIntent] = []
    for intent in result:
        # Match WAIT intents from doorstep cargo workers (original behavior).
        is_wait_doorstep = (
            intent.action is ActionKind.WAIT
            and intent.reason == "cargo_doorstep_wait_for_entry"
        )
        # Match MOVE intents from doorstep cargo workers already oscillating
        # with yield_corridor_for_combat — re-evaluate their yield direction
        # now that the cascade may have freed outer cells.
        is_move_yield = (
            intent.action is ActionKind.MOVE
            and intent.reason == "yield_corridor_for_combat"
        )
        if is_wait_doorstep or is_move_yield:
            unit = context.current_objects.get(intent.actor_id)
            if (
                isinstance(unit, UnitView)
                and unit.cargo
                and distance(unit.position, core_position) == 1
            ):
                yield_intent = _force_doorstep_yield(
                    unit, core_position, context, memory, reservations, blocked,
                )
                if yield_intent is not None:
                    _record_unit_task(
                        memory, context, unit,
                        kind="yield_corridor_for_combat",
                        target=yield_intent.target_cell or unit.position,
                        intent=yield_intent,
                    )
                    final.append(yield_intent)
                    continue
        final.append(intent)
    return final


def _cascade_corridor_evacuation(
    intents: list[ActionIntent],
    context: DecisionContext,
    memory: AgentMemory,
    reservations: ReservationTable,
    blocked: set[Position],
    core_position: Position,
) -> list[ActionIntent]:
    """Evacuate outer-ring cargo workers outward to break high-density corridor congestion.

    In pocket terrain with a long packed corridor, all cells adjacent to the
    doorstep (distance 1) may be saturated at 2/2 capacity with cargo workers
    (``no_safe_route_with_cargo`` or ``cargo_doorstep_wait_for_entry``).  The
    existing ``_force_doorstep_yield`` cannot reserve any cell because every
    candidate is full.

    This function scans for cargo workers at distance 2-3 whose WAIT reason
    indicates corridor congestion and forces them outward (to distance 3-4+),
    creating free cells for distance-1 workers to yield into.  Workers already
    at distance >= 4 are not affected (they are beyond the congestion zone).

    The evacuation processes distance 3 first (outermost congestion ring),
    then distance 2, ensuring space cascades inward.
    """
    result = list(intents)
    existing_tasks = memory.unit_tasks

    # Build lookup: actor_id -> index in result list for intent replacement.
    intent_index: dict = {}
    for idx, intent in enumerate(result):
        intent_index[intent.actor_id] = idx

    # Process distance 3 first (outermost ring), then distance 2.
    # This ensures space flows inward: distance 3 yields → distance 2 gets
    # space → distance 1 gets space.
    for ring_distance in (3, 2):
        for idx, intent in enumerate(result):
            # Match WAIT intents from blocked cargo workers (original behavior).
            is_wait_blocked = (
                intent.action is ActionKind.WAIT
                and intent.reason in (
                    "no_safe_route_with_cargo",
                    "cargo_doorstep_wait_for_entry",
                )
            )
            # Match MOVE intents from cargo workers oscillating in the
            # corridor between yield_corridor_for_combat and
            # return_cargo_to_core_sidestep — these are stuck in a
            # MOVE-based oscillation that the WAIT-only filter cannot break.
            is_move_oscillating = (
                intent.action is ActionKind.MOVE
                and intent.reason in (
                    "yield_corridor_for_combat",
                    "return_cargo_to_core_sidestep",
                )
            )
            if not (is_wait_blocked or is_move_oscillating):
                continue
            unit = context.current_objects.get(intent.actor_id)
            if (
                not isinstance(unit, UnitView)
                or not unit.cargo
                or distance(unit.position, core_position) != ring_distance
            ):
                continue
            existing_task = existing_tasks.get(str(unit.id), {})
            prev_cell_raw = existing_task.get("prev_cell")
            prev_cell = (
                tuple(prev_cell_raw)
                if isinstance(prev_cell_raw, (list, tuple))
                and len(prev_cell_raw) == 2
                else None
            )

            cascade_intent = _cascade_yield_outward(
                unit, core_position, context, memory, reservations,
                blocked, prev_cell,
            )
            if cascade_intent is not None:
                _record_unit_task(
                    memory, context, unit,
                    kind="yield_corridor_for_combat",
                    target=cascade_intent.target_cell or unit.position,
                    intent=cascade_intent,
                )
                result[idx] = cascade_intent

    return result


def _cascade_yield_outward(
    worker: UnitView,
    core_position: Position,
    context: DecisionContext,
    memory: AgentMemory,
    reservations: ReservationTable,
    blocked: set[Position],
    prev_cell: Position | None,
) -> ActionIntent | None:
    """Move a cargo worker outward from a congested corridor ring.

    Selects the best adjacent cell that is:
    - Not the core position
    - Not blocked (obstacles, enemy, threats)
    - At the same or greater distance from core (outward or lateral)
    - Not the previous cell (anti-oscillation)
    - Has at least one usable exit besides the worker's current cell (dead-end guard)
    """
    current_dist = distance(worker.position, core_position)

    candidates: list[tuple[int, int, int, Position, Direction]] = []
    for direction_idx, direction in enumerate(DIRECTIONS):
        cand = destination(worker.position, direction)
        if cand == core_position or cand in blocked:
            continue
        cand_dist = distance(cand, core_position)
        if cand_dist < current_dist:
            continue  # Don't move closer to core (would worsen congestion)
        # Dead-end guard: cell must have at least one non-blocked exit besides
        # the cell we came from and the core.
        other_exits = sum(
            1 for d in DIRECTIONS
            if destination(cand, d) != worker.position
            and destination(cand, d) != core_position
            and destination(cand, d) not in blocked
        )
        if other_exits == 0:
            continue
        outward_bonus = 1 if cand_dist > current_dist else 0
        backtrack_penalty = 5000 if prev_cell is not None and cand == prev_cell else 0
        occupancy = len(context.friendly_occupancy.get(cand, ()))
        candidates.append((
            backtrack_penalty + (1 - outward_bonus) + occupancy,
            cand_dist,
            direction_idx,
            cand,
            direction,
        ))

    candidates.sort()
    # If every candidate carries the backtrack penalty, avoid oscillation.
    if candidates and candidates[0][0] >= 5000:
        return None
    for _, _, _, cand, direction in candidates:
        if reservations.reserve(cand, source=worker.position):
            return ActionIntent(
                actor_id=worker.id,
                is_core=False,
                action=ActionKind.MOVE,
                score=420,
                reason="yield_corridor_for_combat",
                direction=direction,
                target_cell=cand,
                reserved_cell=cand,
            )
    return None


def _force_doorstep_yield(
    worker: UnitView,
    core_position: Position,
    context: DecisionContext,
    memory: AgentMemory,
    reservations: ReservationTable,
    blocked: set[Position],
) -> ActionIntent | None:
    """Move a cargo worker outward from the core doorstep to break a pocket deadlock.

    Selects the best adjacent free cell preferring outward movement (away from
    core), avoiding dead-end pockets, and penalising the previous cell to
    prevent oscillation.
    """
    existing_task = memory.unit_tasks.get(str(worker.id), {})
    prev_cell_raw = existing_task.get("prev_cell")
    prev_cell = tuple(prev_cell_raw) if isinstance(prev_cell_raw, (list, tuple)) and len(prev_cell_raw) == 2 else None

    candidates: list[tuple[int, int, int, Position, Direction]] = []
    for direction_idx, direction in enumerate(DIRECTIONS):
        cand = destination(worker.position, direction)
        if (
            cand == core_position
            or cand in blocked
            or cand in context.enemy_occupancy
        ):
            continue
        # Avoid dead-end pockets: cell must have at least one non-blocked
        # exit besides the cell we came from and the core.
        other_exits = sum(
            1 for d in DIRECTIONS
            if destination(cand, d) != worker.position
            and destination(cand, d) != core_position
            and destination(cand, d) not in blocked
            and destination(cand, d) not in context.enemy_occupancy
        )
        if other_exits == 0:
            continue
        dist_from_core = distance(cand, core_position)
        outward_bonus = 1 if dist_from_core > distance(worker.position, core_position) else 0
        backtrack_penalty = 5000 if prev_cell is not None and cand == prev_cell else 0
        occupancy = len(context.friendly_occupancy.get(cand, ()))
        candidates.append((
            backtrack_penalty + (1 - outward_bonus) + occupancy,
            distance(cand, core_position),
            direction_idx,
            cand,
            direction,
        ))

    candidates.sort()
    # If every candidate carries the backtrack penalty, avoid oscillation.
    if candidates and candidates[0][0] >= 5000:
        return None
    for _, _, _, cand, direction in candidates:
        if reservations.reserve(cand, source=worker.position):
            return ActionIntent(
                actor_id=worker.id,
                is_core=False,
                action=ActionKind.MOVE,
                score=420,
                reason="yield_corridor_for_combat",
                direction=direction,
                target_cell=cand,
                reserved_cell=cand,
            )
    return None


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

    # Anti-oscillation: read previous cell from memory to penalise returning
    # to it, preventing 2-cell ping-pong on the core doorstep.
    existing_task = memory.unit_tasks.get(str(unit.id), {})
    prev_cell_raw = existing_task.get("prev_cell")
    prev_cell = tuple(prev_cell_raw) if isinstance(prev_cell_raw, (list, tuple)) and len(prev_cell_raw) == 2 else None

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
        # Heavy penalty to prevent oscillating back to the cell we just came from
        backtrack_penalty = 5000 if prev_cell is not None and cand == prev_cell else 0
        candidates.append((outward_penalty + backtrack_penalty, occupancy, direction_idx, cand, direction))

    candidates.sort()
    # If every remaining candidate carries the backtrack penalty, the unit
    # would oscillate between two cells every other tick.  Return None so
    # the caller falls through to WAIT, which breaks the cycle.
    if candidates and candidates[0][0] >= 5000:
        return None
    for _, _, _, cand, direction in candidates:
        if reservations.reserve(cand, source=unit.position):
            _record_unit_task(memory, context, unit, kind="evacuate_doorstep", target=cand, intent=ActionIntent(
                actor_id=unit.id,
                is_core=False,
                action=ActionKind.MOVE,
                score=410,
                reason=reason,
                direction=direction,
                target_cell=cand,
                reserved_cell=cand,
            ))
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


