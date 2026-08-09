"""Map validated intents onto controllers from the current Turn."""

from __future__ import annotations

from collections import Counter
from typing import Iterable

from arena_hero import Turn

from .models import ActionIntent, ActionKind

# Compatibility re-export for callers that used the pre-split module.
from .validation import validate_intents


def apply_intents(turn: Turn, intents: Iterable[ActionIntent]) -> None:
    """Map validated intents only to controllers from this exact Turn."""
    units = {unit.id: unit for unit in turn.units}
    for intent in intents:
        controller = turn.core if intent.is_core else units.get(intent.actor_id)
        if controller is None:
            continue
        if intent.action is ActionKind.WAIT:
            controller.wait()
        elif intent.action is ActionKind.MOVE:
            controller.move(intent.direction)
        elif intent.action is ActionKind.HARVEST:
            controller.harvest()
        elif intent.action is ActionKind.DEPOSIT:
            controller.deposit()
        elif intent.action is ActionKind.SWEEP:
            controller.sweep(intent.direction)
        elif intent.action is ActionKind.SHOOT:
            controller.shoot(intent.target_id, expected_cell=intent.target_cell)
        elif intent.action is ActionKind.HEAL:
            controller.heal()
        elif intent.action is ActionKind.SPAWN:
            controller.spawn(intent.unit_type)
        elif intent.action is ActionKind.REPAIR_SHIELD:
            controller.repair_shield()
        elif intent.action is ActionKind.START_MOVE:
            controller.start_move(intent.direction)
        elif intent.action is ActionKind.PICKUP_BEACON:
            controller.pickup_beacon()


def action_counts(intents: Iterable[ActionIntent]) -> dict[str, int]:
    return dict(Counter(intent.action.value for intent in intents))
