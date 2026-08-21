"""Lightweight pure-functional tactical combat simulator and reinforcement ETA evaluator."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence
from uuid import UUID

from arena_hero import CoreView, UnitType, UnitView

from .context import DecisionContext
from .models import Position
from .navigation import distance, shot_range

UNIT_BASE_DPS = {
    UnitType.VANGUARD: 1.0,  # SWEEP base damage per turn
    UnitType.RANGER: 1.0,    # SHOOT base damage per turn
    UnitType.WORKER: 0.0,    # No combat capability
}

CORE_DPS = 0.0  # Cores cannot attack directly


@dataclass(frozen=True, slots=True)
class LocalCombatAssessment:
    """Local combat power evaluation result."""
    win_probability: float
    friendly_power: float
    enemy_power: float
    reinforcement_eta: int | None
    recommended_stance: str  # "ENGAGE", "KITE_ENGAGE", "STALL_AND_WAIT", "COVERING_RETREAT"


def calculate_effective_power(
    units: Sequence[UnitView],
    cores: Sequence[CoreView] = (),
) -> float:
    """Calculate combined combat power = sum((HP + Shield) * Effective_DPS)."""
    total_power = 0.0
    for u in units:
        dps = UNIT_BASE_DPS.get(u.unit_type, 0.0)
        total_power += u.hp * (1.0 + dps)
    for c in cores:
        total_power += (c.hp + c.shield) * 0.2  # Cores absorb damage
    return total_power


def estimate_reinforcement_eta(
    battle_center: Position,
    friendly_reinforcements: Iterable[UnitView],
) -> int | None:
    """Estimate minimum ticks for the closest friendly combat unit to reach battle_center."""
    combat_units = [
        u for u in friendly_reinforcements
        if u.unit_type in (UnitType.VANGUARD, UnitType.RANGER)
    ]
    if not combat_units:
        return None
    return min(distance(u.position, battle_center) for u in combat_units)


def assess_local_engagement(
    context: DecisionContext,
    local_friendlies: Sequence[UnitView],
    local_enemies: Sequence[UnitView | CoreView],
    obstacles: set[Position],
) -> LocalCombatAssessment:
    """Perform a pure-functional assessment of the local skirmish."""
    if not local_enemies:
        return LocalCombatAssessment(
            win_probability=1.0,
            friendly_power=100.0,
            enemy_power=0.0,
            reinforcement_eta=None,
            recommended_stance="ENGAGE",
        )

    enemy_units = [e for e in local_enemies if isinstance(e, UnitView)]
    enemy_cores = [e for e in local_enemies if isinstance(e, CoreView)]

    friendly_power = calculate_effective_power(local_friendlies)
    enemy_power = calculate_effective_power(enemy_units, enemy_cores)

    if enemy_power <= 0.0:
        win_prob = 1.0
    elif friendly_power <= 0.0:
        win_prob = 0.0
    else:
        win_prob = friendly_power / (friendly_power + enemy_power)

    # Determine battle center
    if local_enemies:
        cx = sum(e.position[0] for e in local_enemies) // len(local_enemies)
        cy = sum(e.position[1] for e in local_enemies) // len(local_enemies)
        battle_center = (cx, cy)
    else:
        battle_center = context.core.position if context.core else (0, 0)

    # Find reinforcements not in the immediate skirmish
    local_ids = {u.id for u in local_friendlies}
    outside_friendlies = [u for u in context.units if u.id not in local_ids]
    eta = estimate_reinforcement_eta(battle_center, outside_friendlies)

    if win_prob >= 0.60:
        stance = "ENGAGE"
    elif win_prob >= 0.40:
        stance = "KITE_ENGAGE"
    elif eta is not None and eta <= 4:
        stance = "STALL_AND_WAIT"
    else:
        stance = "COVERING_RETREAT"

    return LocalCombatAssessment(
        win_probability=win_prob,
        friendly_power=friendly_power,
        enemy_power=enemy_power,
        reinforcement_eta=eta,
        recommended_stance=stance,
    )
