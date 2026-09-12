"""Regression coverage for the Tick 260320-260481 Core-loss chain."""

from arena_hero import UnitType

from arena_tactic import AgentConfig, choose_actions
from arena_tactic.models import ActionKind

from .factories import core, turn, unit


def test_attack_replacement_keeps_high_population_core_survival_reserve():
    """ATTACK may replace a Ranger, but never spends the last 30 resources."""
    roster = (
        *(unit(i, UnitType.WORKER, (i + 1, 4)) for i in range(12)),
        *(unit(i, UnitType.VANGUARD, (i - 11, 5)) for i in range(12, 24)),
        *(unit(i, UnitType.RANGER, (i - 23, 6)) for i in range(24, 38)),
    )
    result = choose_actions(
        turn(
            owned_core=core(), units=roster, resources=63,
            enemies=(core(value=900, position=(30, 30), controlled=False),),
        ),
        config=AgentConfig(max_population=40),
    )
    assert result.mode.value == "ATTACK"
    core_intent = next(intent for intent in result.intents if intent.is_core)
    assert core_intent.action is not ActionKind.SPAWN


def test_high_population_opening_exception_cannot_bypass_survival_reserve():
    """The early-roster exception is not a back door around the hard floor."""
    workers = tuple(
        unit(index, UnitType.WORKER, (index + 1, 4)) for index in range(30)
    )
    result = choose_actions(
        turn(owned_core=core(), units=workers, resources=40),
        config=AgentConfig(max_population=40),
    )
    core_intent = next(intent for intent in result.intents if intent.is_core)
    assert core_intent.action is not ActionKind.SPAWN


def test_empty_emergency_workers_clear_core_defense_corridor():
    worker = unit(1, UnitType.WORKER, (-1, 0))
    vanguard = unit(2, UnitType.VANGUARD, (0, -1))
    ranger = unit(3, UnitType.RANGER, (0, 1))
    enemy = unit(200, UnitType.VANGUARD, (3, 0), controlled=False)

    result = choose_actions(
        turn(owned_core=core(), units=(worker, vanguard, ranger), enemies=(enemy,))
    )
    intent = next(intent for intent in result.intents if intent.actor_id == worker.id)
    assert intent.action is ActionKind.MOVE
    assert intent.reason == "emergency_worker_defensive_clearance"
    assert intent.reserved_cell is not None
    assert abs(intent.reserved_cell[0]) + abs(intent.reserved_cell[1]) >= 2


def test_defensive_ranger_sidesteps_when_core_threat_route_is_blocked(monkeypatch):
    import arena_tactic.strategy.rangers as rangers_module

    ranger = unit(1, UnitType.RANGER, (0, 1))
    vanguard = unit(2, UnitType.VANGUARD, (-1, 0))
    enemy = unit(200, UnitType.RANGER, (3, 0), controlled=False)
    monkeypatch.setattr(rangers_module, "_move", lambda *args, **kwargs: None)

    result = choose_actions(
        turn(owned_core=core(), units=(ranger, vanguard), enemies=(enemy,))
    )
    intent = next(intent for intent in result.intents if intent.actor_id == ranger.id)
    assert intent.action is ActionKind.MOVE
    assert intent.reason == "defensive_firing_line_sidestep"


def test_critical_doorstep_vanguard_holds_when_no_healthy_tank_remains():
    guard = unit(1, UnitType.VANGUARD, (-1, 0), hp=1)
    enemy = unit(200, UnitType.VANGUARD, (1, 0), controlled=False)

    result = choose_actions(turn(owned_core=core(), units=(guard,), enemies=(enemy,)))
    intent = next(intent for intent in result.intents if intent.actor_id == guard.id)
    assert intent.action is ActionKind.WAIT
    assert intent.reason == "doorstep_last_stand_hold"
