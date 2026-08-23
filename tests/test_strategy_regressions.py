from arena_hero import Direction, UnitType

from arena_tactic.context import DecisionContext
from arena_tactic.memory import AgentMemory
from arena_tactic.models import ActionKind, AgentConfig, StrategicMode
from arena_tactic.strategy.combat import ranger_target_score
from arena_tactic.strategy.core_plan import _core_migration_direction, _plan_core

from .factories import core, turn, unit


def test_peacetime_buffer_cannot_exceed_core_storage_capacity():
    units = (
        unit(1, UnitType.WORKER, (1, 0)),
        unit(2, UnitType.WORKER, (2, 0)),
        unit(3, UnitType.VANGUARD, (0, 1)),
        unit(4, UnitType.VANGUARD, (0, 2)),
        unit(5, UnitType.RANGER, (-1, 0)),
    )
    context = DecisionContext.from_turn(
        turn(owned_core=core(), units=units, resources=25)
    )

    intent = _plan_core(
        context,
        AgentMemory(),
        StrategicMode.ECONOMY,
        [],
        0,
        AgentConfig(),
    )

    assert intent is not None
    assert intent.action is ActionKind.SPAWN
    assert intent.unit_type is UnitType.RANGER


def test_stale_migration_recommendation_falls_back_to_best_resource_center():
    context = DecisionContext.from_turn(turn(tick=200, owned_core=core()))
    memory = AgentMemory(
        last_tick=199,
        resource_observations={(5, 0): 199},
        migration_recommendation={
            "center": [50, 50],
            "score": 100.0,
            "computed_at_tick": 0,
            "interval_ticks": 60,
        },
    )

    direction = _core_migration_direction(context, memory, [])

    assert direction is Direction.RIGHT


def test_ranger_scoring_uses_legal_firing_range_for_diagonal_targets():
    ranger = unit(1, UnitType.RANGER, (0, 0))
    diagonal = unit(2, UnitType.RANGER, (2, 2), controlled=False)
    straight = unit(3, UnitType.RANGER, (3, 0), controlled=False)
    context = DecisionContext.from_turn(
        turn(owned_core=core(position=(-5, 0)), units=(ranger,), enemies=(diagonal, straight))
    )
    memory = AgentMemory()

    diagonal_score = ranger_target_score(ranger, diagonal, context, memory)
    straight_score = ranger_target_score(ranger, straight, context, memory)

    assert diagonal_score > straight_score
