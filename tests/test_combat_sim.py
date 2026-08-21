import pytest
from uuid import UUID
from arena_hero import CoreView, UnitType, UnitView, CoreState
from arena_tactic.combat_sim import (
    calculate_effective_power,
    estimate_reinforcement_eta,
    assess_local_engagement,
)
from arena_tactic.context import DecisionContext
from .factories import core, turn, unit


def test_combat_power_calculation():
    v1 = unit(1, UnitType.VANGUARD, (0, 0), hp=4)
    r1 = unit(2, UnitType.RANGER, (0, 1), hp=2)
    w1 = unit(3, UnitType.WORKER, (0, 2), hp=2)
    
    # Vanguard: hp 4 * (1 + 1) = 8
    # Ranger: hp 2 * (1 + 1) = 4
    # Worker: hp 2 * (1 + 0) = 2
    power = calculate_effective_power([v1, r1, w1])
    assert power == 14.0


def test_reinforcement_eta_estimation():
    r1 = unit(10, UnitType.RANGER, (10, 10))
    w1 = unit(11, UnitType.WORKER, (2, 2))  # Workers are ignored for combat reinforcement
    
    battle_pos = (0, 0)
    eta = estimate_reinforcement_eta(battle_pos, [r1, w1])
    assert eta == 20  # Manhattan distance (10 + 10)


def test_local_engagement_assessment():
    v1 = unit(1, UnitType.VANGUARD, (0, 0), hp=4)
    e1 = unit(100, UnitType.VANGUARD, (2, 0), hp=1, controlled=False)
    
    ctx = DecisionContext.from_turn(turn(owned_core=core(), units=(v1,), enemies=(e1,)))
    assessment = assess_local_engagement(ctx, [v1], [e1], set())
    
    assert assessment.win_probability > 0.60
    assert assessment.recommended_stance == "ENGAGE"
