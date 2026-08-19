from time import perf_counter

from arena_hero import Direction, UnitType

from arena_tactic.context import DecisionContext
from arena_tactic.models import ActionIntent, ActionKind, AgentConfig
from arena_tactic.navigation import (
    bounded_astar,
    deterministic_fallback,
    shot_range,
)
from arena_tactic.validation import validate_intents

from .factories import core, turn, unit, uuid


def test_bounded_astar_routes_around_a_permanent_obstacle():
    direction = bounded_astar(
        (0, 0),
        (2, 0),
        blocked={(1, 0)},
        deadline=perf_counter() + 1,
        node_limit=100,
    )
    assert direction in (Direction.UP, Direction.DOWN)


def test_ranger_shot_geometry_accepts_exact_diagonal_only():
    assert shot_range((0, 0), (3, 3), set()) == 3
    assert shot_range((0, 0), (2, 1), set()) is None
    assert shot_range((0, 0), (3, 0), {(1, 0)}) is None
    assert shot_range((0, 0), (1, 1), {(1, 0)}) == 1


def test_timeout_fallback_is_deterministic_for_uuid():
    first = deterministic_fallback(uuid(7), (0, 0), (3, 1), {(1, 0)})
    second = deterministic_fallback(uuid(7), (0, 0), (3, 1), {(1, 0)})
    assert first == second
    assert first is not None


def test_plan_step_waits_when_no_proven_route_exists():
    context = DecisionContext.from_turn(
        turn(owned_core=core(), units=(unit(1, UnitType.WORKER, (0, 0)),), obstacle_cells=((1, 0), (0, 1), (-1, 0), (0, -1)))
    )
    from arena_tactic.models import ReservationTable
    from arena_tactic.navigation import plan_step

    assert plan_step(
        actor_id=uuid(1),
        start=(0, 0),
        goal=(3, 0),
        context=context,
        persistent_obstacles=set(context.obstacle_cells),
        reservations=ReservationTable({}),
        deadline=perf_counter() + 1,
        config=AgentConfig(),
    ) is None


def test_validator_limits_two_entities_per_destination():
    workers = (
        unit(1, UnitType.WORKER, (-1, 0)),
        unit(2, UnitType.WORKER, (1, 0)),
        unit(3, UnitType.WORKER, (0, -1)),
    )
    game_turn = turn(owned_core=core(position=(10, 10)), units=workers)
    context = DecisionContext.from_turn(game_turn)
    directions = (Direction.RIGHT, Direction.LEFT, Direction.DOWN)
    intents = tuple(
        ActionIntent(
            actor_id=worker.id,
            is_core=False,
            action=ActionKind.MOVE,
            score=10,
            reason="capacity_test",
            direction=direction,
            reserved_cell=(0, 0),
        )
        for worker, direction in zip(workers, directions, strict=True)
    )
    accepted, rejected = validate_intents(intents, context, AgentConfig())
    assert sum(intent.action is ActionKind.MOVE for intent in accepted) == 2
    assert any(item.rejection_reason == "friendly_cell_capacity_exceeded" for item in rejected)


def test_planner_canary_capacity_conflict_is_rerouted_without_a_rejected_intent():
    workers = (
        unit(1, UnitType.WORKER, (-1, 0)),
        unit(2, UnitType.WORKER, (1, 0)),
        unit(3, UnitType.WORKER, (0, -1)),
    )
    context = DecisionContext.from_turn(turn(owned_core=core(position=(10, 10)), units=workers))
    intents = tuple(
        ActionIntent(worker.id, False, ActionKind.MOVE, 10, "capacity_test", direction=direction,
                     target_cell=(0, 0), reserved_cell=(0, 0))
        for worker, direction in zip(workers, (Direction.RIGHT, Direction.LEFT, Direction.DOWN), strict=True)
    )

    accepted, rejected = validate_intents(intents, context, AgentConfig(planner_canary=True))

    assert not rejected
    assert len(accepted) == len(context.current_objects)
    assert sum(intent.action is ActionKind.MOVE for intent in accepted) == 3
    rerouted = next(intent for intent in accepted if intent.actor_id == workers[2].id)
    assert rerouted.reason == "arbitrator_capacity_reroute"
    assert rerouted.reserved_cell != (0, 0)


def test_validator_rejects_stale_ranger_target():
    ranger = unit(1, UnitType.RANGER, (0, 0))
    game_turn = turn(owned_core=core(), units=(ranger,))
    context = DecisionContext.from_turn(game_turn)
    stale = ActionIntent(
        actor_id=ranger.id,
        is_core=False,
        action=ActionKind.SHOOT,
        score=10,
        reason="stale",
        target_id=uuid(999),
        target_cell=(1, 0),
    )
    accepted, rejected = validate_intents((stale,), context, AgentConfig())
    assert next(intent for intent in accepted if intent.actor_id == ranger.id).action is ActionKind.WAIT
    assert rejected[0].rejection_reason == "ranger_target_not_current_and_legal"
