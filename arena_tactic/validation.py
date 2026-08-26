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
from .navigation import DIRECTIONS, destination, distance, shot_range


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
    ActionKind.SELF_DESTRUCT,
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
        if intent.action not in (ActionKind.WAIT, ActionKind.SELF_DESTRUCT) and actor.state is CoreState.MOVING:
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
                or shot_range(actor.position, target.position, context.obstacle_cells) is None
            ):
                return "ranger_target_not_current_and_legal"

    if intent.action is ActionKind.PICKUP_BEACON and (
        context.beacon.status is not BeaconStatus.GROUND
        or actor.position != context.beacon.position
    ):
        return "beacon_not_grounded_on_actor_cell"
    return None


def _project_final_occupancy(
    selected: dict[object, ActionIntent],
    context: DecisionContext,
) -> dict[tuple[int, int], int]:
    """Project all currently selected Unit moves as one simultaneous Tick."""
    counts = {
        cell: len(ids) for cell, ids in context.friendly_occupancy.items()
    }
    for actor_id, intent in selected.items():
        if intent.action is not ActionKind.MOVE:
            continue
        actor = context.current_objects.get(actor_id)
        if not isinstance(actor, UnitView) or intent.direction is None:
            continue
        target = destination(actor.position, intent.direction)
        counts[actor.position] = max(0, counts.get(actor.position, 0) - 1)
        counts[target] = counts.get(target, 0) + 1
    return counts


def _incoming_to(
    cell: tuple[int, int],
    selected: dict[object, ActionIntent],
    context: DecisionContext,
) -> list[tuple[object, ActionIntent]]:
    result: list[tuple[object, ActionIntent]] = []
    for actor_id, intent in selected.items():
        if intent.action is not ActionKind.MOVE or intent.direction is None:
            continue
        actor = context.current_objects.get(actor_id)
        if isinstance(actor, UnitView) and destination(actor.position, intent.direction) == cell:
            result.append((actor_id, intent))
    return result


def validate_intents(
    intents: Iterable[ActionIntent],
    context: DecisionContext,
    config: AgentConfig,
) -> tuple[tuple[ActionIntent, ...], tuple[RejectedIntent, ...]]:
    """Select one legal action per current object and enforce final cell capacity.

    Movement is arbitrated as a simultaneous end-of-Tick projection. This
    avoids rejecting an entrant merely because the lower-scoring occupant that
    frees its slot would have been visited later in score order.
    """
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

    # Recompute the whole final occupancy after every rejected/rerouted move.
    # This is intentionally iterative: rejecting one departure can make its
    # source full again and invalidate an entrant that depended on that slot.
    repair_budget = max(8, len(context.units) * 4)
    while repair_budget > 0:
        repair_budget -= 1
        final_occupancy = _project_final_occupancy(selected, context)
        overfull = sorted(cell for cell, count in final_occupancy.items() if count > 2)
        if not overfull:
            break
        cell = overfull[0]
        incoming = _incoming_to(cell, selected, context)
        if not incoming:
            # Current authoritative state should never exceed capacity; if it
            # does, do not invent movement to repair server state.
            break
        # Highest score wins capacity. Deterministic UUID/action ordering makes
        # the same lower-priority entrant lose on replay.
        actor_id, loser = min(
            incoming,
            key=lambda item: (
                item[1].score,
                item[1].actor_id.bytes,
                item[1].action.value,
            ),
        )
        actor = context.current_objects[actor_id]
        assert isinstance(actor, UnitView)
        del selected[actor_id]
        if config.planner_canary:
            projected_without = _project_final_occupancy(selected, context)
            repaired = _capacity_repair(
                loser,
                actor,
                context,
                ReservationTable(occupancy=projected_without),
            )
            selected[actor_id] = repaired
        else:
            rejected.append(RejectedIntent(loser, "friendly_cell_capacity_exceeded"))

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


def _capacity_repair(
    intent: ActionIntent,
    actor: UnitView,
    context: DecisionContext,
    reservations: ReservationTable,
) -> ActionIntent:
    """Reroute one canary move against already-projected final occupancy."""
    target = intent.target_cell or destination(actor.position, intent.direction)  # type: ignore[arg-type]
    alternatives = sorted(
        DIRECTIONS,
        key=lambda direction: (
            distance(destination(actor.position, direction), target),
            direction.value,
        ),
    )
    for direction in alternatives:
        cell = destination(actor.position, direction)
        if cell in context.obstacle_cells or cell in context.enemy_occupancy:
            continue
        if reservations.reserve(cell, source=actor.position):
            return ActionIntent(
                actor.id,
                False,
                ActionKind.MOVE,
                intent.score,
                "arbitrator_capacity_reroute",
                direction=direction,
                target_cell=intent.target_cell,
                reserved_cell=cell,
            )
    return ActionIntent(
        actor.id,
        False,
        ActionKind.WAIT,
        intent.score,
        "arbitrator_capacity_wait",
        target_cell=intent.target_cell,
    )
