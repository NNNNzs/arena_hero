"""Current-Turn legality, uniqueness, and cell-capacity validation."""

from __future__ import annotations

from typing import Iterable

from arena_hero import BeaconStatus, CoreState, CoreView, UnitType, UnitView

from .context import DecisionContext
from .models import (
    ActionIntent,
    ActionKind,
    AgentConfig,
    RejectedIntent,
    ReservationTable,
)
from .navigation import destination, shot_range


_UNIT_ACTIONS = {
    ActionKind.WAIT,
    ActionKind.MOVE,
    ActionKind.HEAL,
    ActionKind.PICKUP_BEACON,
}
_CORE_ACTIONS = {
    ActionKind.WAIT,
    ActionKind.HEAL,
    ActionKind.SPAWN,
    ActionKind.REPAIR_SHIELD,
    ActionKind.START_MOVE,
    ActionKind.PICKUP_BEACON,
}


def _basic_rejection(
    intent: ActionIntent,
    context: DecisionContext,
    config: AgentConfig,
) -> str | None:
    actor = context.current_objects.get(intent.actor_id)
    if actor is None:
        return "actor_not_in_current_turn"
    if intent.is_core != isinstance(actor, CoreView):
        return "actor_kind_mismatch"

    if isinstance(actor, CoreView):
        if intent.action not in _CORE_ACTIONS:
            return "action_not_supported_by_core"
        if intent.action is not ActionKind.WAIT and actor.state is CoreState.MOVING:
            return "moving_core_must_wait"
        if intent.action is ActionKind.SPAWN and (
            intent.unit_type is None
            or context.population >= config.max_population
        ):
            return "core_spawn_not_currently_valid"
        if intent.action is ActionKind.START_MOVE:
            if intent.direction is None:
                return "core_move_missing_direction"
            cell = destination(actor.position, intent.direction)
            if (
                cell in context.obstacle_cells
                or cell in context.enemy_occupancy
                or cell in context.friendly_occupancy
            ):
                return "core_move_destination_blocked"
    else:
        allowed = set(_UNIT_ACTIONS)
        if actor.unit_type is UnitType.WORKER:
            allowed.update((ActionKind.HARVEST, ActionKind.DEPOSIT))
        elif actor.unit_type is UnitType.VANGUARD:
            allowed.add(ActionKind.SWEEP)
        elif actor.unit_type is UnitType.RANGER:
            allowed.add(ActionKind.SHOOT)
        if intent.action not in allowed:
            return "action_not_supported_by_unit"
        if intent.action is ActionKind.MOVE:
            if intent.direction is None:
                return "unit_move_missing_direction"
            cell = destination(actor.position, intent.direction)
            if cell in context.obstacle_cells or cell in context.enemy_occupancy:
                return "unit_move_destination_blocked"
        if intent.action is ActionKind.HARVEST and (
            actor.position not in context.resource_cells or (actor.cargo or 0) > 0
        ):
            return "harvest_requires_current_visible_resource"
        if intent.action is ActionKind.DEPOSIT and (
            context.core is None
            or context.core.state is not CoreState.NORMAL
            or actor.position != context.core.position
            or not (actor.cargo or 0)
            or context.resource_space <= 0
        ):
            return "deposit_requires_stationary_core_and_space"
        if intent.action is ActionKind.HEAL and (
            context.core is None
            or context.core.state is not CoreState.NORMAL
            or actor.position != context.core.position
        ):
            return "unit_heal_not_currently_legal"
        if intent.action is ActionKind.SWEEP and intent.direction is None:
            return "vanguard_sweep_missing_direction"
        if intent.action is ActionKind.SHOOT:
            target = (
                context.current_enemies.get(intent.target_id)
                if intent.target_id is not None
                else None
            )
            if (
                target is None
                or intent.target_cell != target.position
                or shot_range(actor.position, target.position, context.obstacle_cells)
                is None
            ):
                return "ranger_target_not_current_and_legal"

    if intent.action is ActionKind.PICKUP_BEACON and (
        context.beacon.status is not BeaconStatus.GROUND
        or actor.position != context.beacon.position
    ):
        return "beacon_not_grounded_on_actor_cell"
    return None


def validate_intents(
    intents: Iterable[ActionIntent],
    context: DecisionContext,
    config: AgentConfig,
) -> tuple[tuple[ActionIntent, ...], tuple[RejectedIntent, ...]]:
    """Select one legal action per current object and enforce cell capacity."""
    rejected: list[RejectedIntent] = []
    selected: dict[object, ActionIntent] = {}
    for intent in sorted(
        intents,
        key=lambda candidate: (
            -candidate.score,
            candidate.actor_id.bytes,
            candidate.action.value,
        ),
    ):
        if intent.actor_id in selected:
            rejected.append(RejectedIntent(intent, "lower_scoring_duplicate_actor_intent"))
            continue
        rejection = _basic_rejection(intent, context, config)
        if rejection is not None:
            rejected.append(RejectedIntent(intent, rejection))
            continue
        selected[intent.actor_id] = intent

    reservations = ReservationTable(
        occupancy={cell: len(ids) for cell, ids in context.friendly_occupancy.items()}
    )
    for actor_id, intent in list(selected.items()):
        if intent.action is not ActionKind.MOVE:
            continue
        actor = context.current_objects[actor_id]
        assert isinstance(actor, UnitView)
        cell = destination(actor.position, intent.direction)  # type: ignore[arg-type]
        if not reservations.reserve(cell):
            rejected.append(RejectedIntent(intent, "friendly_cell_capacity_exceeded"))
            del selected[actor_id]

    # Every current controlled object gets an explicit action. A missing Core
    # remains actionless by design during RESPAWN.
    for actor_id, actor in context.current_objects.items():
        if actor_id in selected:
            continue
        selected[actor_id] = ActionIntent(
            actor_id=actor_id,
            is_core=isinstance(actor, CoreView),
            action=ActionKind.WAIT,
            score=0,
            reason="validator_safe_fallback",
        )

    return (
        tuple(
            sorted(
                selected.values(),
                key=lambda intent: (intent.is_core, intent.actor_id.bytes),
            )
        ),
        tuple(rejected),
    )
