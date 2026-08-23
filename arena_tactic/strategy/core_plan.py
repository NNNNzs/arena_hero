"""Core healing, production, migration, and reroll planning."""

from __future__ import annotations

from collections import Counter
from typing import Iterable

from arena_hero import BeaconStatus, CoreState, Direction, UnitType, unit_cost

from ..context import DecisionContext
from ..memory import AgentMemory
from ..models import ActionIntent, ActionKind, AgentConfig, ReservationTable, StrategicMode
from ..navigation import DIRECTIONS, destination, enemy_threat_cells, distance
from ..tactical_geometry import migration_site_score, rich_resource_center
from ..analysis_scheduler import MigrationRecommendation
from .common import CORE_MAX_HP, _anticipated_resources, _wait
from .combat import _enemy_can_attack_core
from .mode import (
    _beacon_owned,
    _hidden_attack_pressure,
    _pressure_distance,
)


def _past_early_roster(context: DecisionContext, config: AgentConfig) -> bool:
    """Return *True* when the early-wave roster targets are all met."""
    counts = Counter(unit.unit_type for unit in context.units)
    return (
        counts[UnitType.WORKER] >= config.early_workers
        and counts[UnitType.VANGUARD] >= config.early_vanguards
        and counts[UnitType.RANGER] >= config.early_rangers
    )


def _spawn_target(
    context: DecisionContext,
    config: AgentConfig,
    mode: StrategicMode,
) -> UnitType | None:
    """Return the best unit type to spawn next, or *None* when satisfied.

    In **wartime** (DEFEND / ATTACK) the production bias shifts
    toward combat units: combat units are prioritized first even in early wave.
    """
    counts = Counter(unit.unit_type for unit in context.units)
    wartime = mode in (StrategicMode.DEFEND, StrategicMode.ATTACK)
    if wartime:
        early_combat = (
            (UnitType.VANGUARD, config.early_vanguards),
            (UnitType.RANGER, config.early_rangers),
        )
        for unit_type, target in early_combat:
            if counts[unit_type] < target:
                return unit_type
    else:
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
    if not deficits:
        return None

    wartime = mode in (StrategicMode.DEFEND, StrategicMode.ATTACK)
    if wartime:
        # In wartime, prefer combat units.  Only produce a Worker if
        # there are *no* combat-unit deficits at all.
        combat_deficits = [
            d for d in deficits if d[2] is not UnitType.WORKER
        ]
        if combat_deficits:
            return max(combat_deficits)[2]
    return max(deficits)[2]


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
    dangerous = enemy_threat_cells(context)
    occupied.update(
        intent.reserved_cell
        for intent in unit_intents
        if intent.action is ActionKind.MOVE and intent.reserved_cell is not None
    )
    candidates = [
        direction
        for direction in DIRECTIONS
        if destination(context.core.position, direction) not in memory.obstacles
        and destination(context.core.position, direction) not in context.resource_cells
        and destination(context.core.position, direction) not in occupied
        and destination(context.core.position, direction) not in dangerous
    ]
    forward_candidates = [
        direction for direction in candidates
        if destination(context.core.position, direction)
        != memory.previous_migration_position
    ]
    if forward_candidates:
        candidates = forward_candidates
    frontier = memory.frontier()
    if not candidates:
        return None

    core_pos = context.core.position
    returning_workers = [w for w in context.workers if (w.cargo or 0) > 0]
    if returning_workers:
        target = min(returning_workers, key=lambda w: distance(w.position, core_pos)).position
    elif memory.migration_recommendation:
        # Prefer cached analysis result when within 2 scan cycles.
        cached = MigrationRecommendation.from_dict(memory.migration_recommendation)
        if cached is not None and cached.is_fresh(context.tick, max_cycles=2):
            target = cached.center
        elif memory.resource_observations:
            resource_candidates = rich_resource_center(
                memory.resource_observations, current_tick=context.tick
            )
            target = resource_candidates[0]["center"] if resource_candidates else core_pos
        else:
            return None
    elif memory.resource_observations:
        # rich_resource_center returns top-N candidates; use the best center.
        resource_candidates = rich_resource_center(
            memory.resource_observations, current_tick=context.tick
        )
        target = resource_candidates[0]["center"] if resource_candidates else core_pos
    elif frontier:
        target = max(
            frontier,
            key=lambda cell: (
                min((distance(cell, resource) for resource in memory.resource_observations), default=0),
                distance(core_pos, cell),
                cell,
            ),
        )
    else:
        return None

    return min(
        candidates,
        key=lambda direction: (
            -migration_site_score(
                destination(context.core.position, direction),
                resource_observations=memory.resource_observations,
                obstacles=memory.obstacles,
                explored=memory.explored,
                current_tick=context.tick,
            ),
            distance(destination(context.core.position, direction), target),
            direction.value,
        ),
    )


def _hidden_attack_escape_direction(
    context: DecisionContext,
    memory: AgentMemory,
    unit_intents: Iterable[ActionIntent],
) -> Direction | None:
    """Choose an observed empty neighbor without inferring the fire origin."""
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
        if (cell := destination(context.core.position, direction)) in context.observed_cells
        and cell not in memory.obstacles
        and cell not in context.resource_cells
        and cell not in occupied
    ]
    return min(candidates, key=lambda item: item.value, default=None)


def _spawn_evaluation_active(
    context: DecisionContext, memory: AgentMemory, config: AgentConfig
) -> bool:
    """Return whether this Core generation is still scouting its spawn."""
    return (
        config.enable_spawn_reroll
        and context.core is not None
        and memory.spawn_eval_status == "PENDING"
        and memory.spawn_eval_core_id == str(context.core.id)
        and len(context.workers) <= config.spawn_eval_worker_max
        and not context.vanguards
        and not context.rangers
    )


def _barren_spawn_reroll_due(
    context: DecisionContext, memory: AgentMemory, config: AgentConfig
) -> bool:
    return _spawn_evaluation_active(context, memory, config) and (
        context.tick - memory.spawn_eval_started_tick + 1
        >= max(1, config.spawn_eval_max_ticks)
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
    # This is intentionally ahead of normal recovery/production/migration:
    # a failed early spawn must reroll on its configured final scout Tick.
    # _spawn_evaluation_active hard-gates it to <=2 Workers and no combat.
    if _barren_spawn_reroll_due(context, memory, config):
        return ActionIntent(
            actor_id=core.id,
            is_core=True,
            action=ActionKind.SELF_DESTRUCT,
            score=1_100,
            reason="barren_spawn_fast_reroll",
        )
    if core.state is CoreState.MOVING:
        return _wait(core, "core_migration_progresses_naturally", is_core=True)
    available_resources = _anticipated_resources(context)
    if (
        _hidden_attack_pressure(context, memory)
        and memory.core_damage_streak >= config.hidden_attack_migration_streak
        and core.hp >= 3
    ):
        direction = _hidden_attack_escape_direction(context, memory, unit_intents)
        if direction is not None:
            return ActionIntent(
                actor_id=core.id, is_core=True, action=ActionKind.START_MOVE,
                score=1_050, reason="escape_sustained_hidden_fire",
                direction=direction,
                reserved_cell=destination(core.position, direction),
            )
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

    immediate_threat = any(
        _enemy_can_attack_core(enemy, core, memory.obstacles)
        for enemy in context.enemies
    )
    combat_ready = len(context.vanguards) >= 1 and len(context.rangers) >= 1
    no_combat_units = len(context.vanguards) == 0 and len(context.rangers) == 0

    pressure = _pressure_distance(context)
    if (
        no_combat_units
        and (immediate_threat or (pressure is not None and pressure <= config.defense_enter_distance))
        and available_resources < unit_cost(UnitType.VANGUARD, context.population)
    ):
        direction = _hidden_attack_escape_direction(context, memory, unit_intents)
        if direction is not None:
            return ActionIntent(
                actor_id=core.id,
                is_core=True,
                action=ActionKind.START_MOVE,
                score=1_020,
                reason="evade_threat_without_combat_roster",
                direction=direction,
                reserved_cell=destination(core.position, direction),
            )

    base_reserve = config.minimum_resource_reserve if combat_ready else 0
    reserve = max(
        base_reserve,
        CORE_MAX_HP - core.hp + planned_unit_heals,
    )
    shield_cap = 10 if _beacon_owned(context) else 5
    spawn_type = _spawn_target(context, config, mode)
    # Keep exactly the two-scout opening team while the spawn is being
    # evaluated.  Further production would establish the roster and disable
    # the barren-start safety gate before the full survey window completes.
    if (
        _spawn_evaluation_active(context, memory, config)
        and len(context.workers) >= config.spawn_eval_worker_max
    ):
        spawn_type = None
    allow_spawn_mode = (
        mode is not StrategicMode.DEFEND
        or not combat_ready
        or spawn_type in (UnitType.VANGUARD, UnitType.RANGER)
    )
    if (
        spawn_type is not None
        and allow_spawn_mode
        and context.population < config.max_population
        and _spawn_cell_is_free(context, unit_intents)
    ):
        price = unit_cost(spawn_type, context.population)
        # Peacetime reserves must never make production mathematically
        # impossible under the Core storage cap. Keep the configured buffer,
        # but clamp it to the amount that can coexist with this spawn + reserve.
        peacetime_conserve = (
            config.peacetime_resource_buffer > 0
            and mode not in (StrategicMode.DEFEND, StrategicMode.ATTACK)
            and combat_ready
            and _past_early_roster(context, config)
        )
        if peacetime_conserve:
            storage_capacity = context.resources + context.resource_space
            buffer_room = max(0, storage_capacity - price - reserve)
            effective_reserve = reserve + min(
                config.peacetime_resource_buffer, buffer_room
            )
        else:
            effective_reserve = reserve
        if (
            not combat_ready
            and core.hp == CORE_MAX_HP
            and core.shield >= 3
        ):
            effective_reserve = max(
                0, CORE_MAX_HP - core.hp + planned_unit_heals
            )
        elif (
            spawn_type in (UnitType.VANGUARD, UnitType.RANGER)
            and core.hp == CORE_MAX_HP
            and core.shield >= 3
            and not _beacon_owned(context)
            and not _past_early_roster(context, config)
        ):
            # Do not let the general safety reserve freeze construction of the
            # opening combat roster. Planned Unit heals remain funded because
            # they resolve before this Core action.
            effective_reserve = max(
                0, CORE_MAX_HP - core.hp + planned_unit_heals
            )
        if available_resources >= price + effective_reserve:
            return ActionIntent(
                actor_id=core.id,
                is_core=True,
                action=ActionKind.SPAWN,
                score=650,
                reason="fill_dynamic_roster_deficit",
                unit_type=spawn_type,
                estimated_cost=price,
            )

    if core.shield < shield_cap and available_resources > reserve:
        return ActionIntent(
            actor_id=core.id,
            is_core=True,
            action=ActionKind.REPAIR_SHIELD,
            score=700,
            reason="restore_available_core_shield_capacity",
            estimated_cost=1,
        )

    if (
        mode in (StrategicMode.ECONOMY, StrategicMode.EXPLORE)
        and not _spawn_evaluation_active(context, memory, config)
        and memory.no_resource_ticks >= config.migration_idle_ticks
        and context.tick > memory.migration_cooldown_until_tick
        and not context.resource_cells
        and not any((worker.cargo or 0) > 0 for worker in context.workers)
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
