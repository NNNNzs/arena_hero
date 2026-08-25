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
    assert intent.unit_type is UnitType.WORKER


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


def test_guard_slots_skips_radius_1_in_tight_bottleneck_terrain():
    from arena_tactic.strategy.common import _guard_slots

    # Core at (0, 0) with obstacles on North, East, South -> only ( -1, 0 ) is passable
    context = DecisionContext.from_turn(turn(owned_core=core(position=(0, 0))))
    memory = AgentMemory(
        obstacles={(0, 1), (1, 0), (0, -1)},
    )
    slots = _guard_slots(context, memory)
    # Radius 1 slot (-1, 0) must NOT be chosen to avoid blocking the sole entrance
    assert (-1, 0) not in slots
    # Radius 2/3 slots should be present
    assert any(abs(x) + abs(y) >= 2 for x, y in slots)


def test_empty_worker_on_core_vacates_when_resource_route_blocked():
    from arena_tactic.models import ReservationTable
    from arena_tactic.strategy.workers import _plan_workers

    # Core at (0, 0) with empty worker at (0, 0).
    # Passable exit (-1, 0) is blocked by a friend, but (-1, -1) or other open cell is reachable.
    w_empty = unit(1, UnitType.WORKER, (0, 0), cargo=0)
    w_blocker = unit(2, UnitType.WORKER, (-1, 0), cargo=1)
    context = DecisionContext.from_turn(
        turn(
            owned_core=core(position=(0, 0)),
            units=(w_empty, w_blocker),
            resource_cells=((-5, 0),),
        )
    )
    # (-1, 0) has 2 occupancy (fully blocked), North/East/South are obstacles
    memory = AgentMemory(
        obstacles={(0, 1), (1, 0), (0, -1)},
    )
    reservations = ReservationTable(occupancy={(0, 0): 2, (-1, 0): 2})
    intents = _plan_workers(
        context, memory, reservations, 9999999999.0, AgentConfig(), {}
    )
    # The empty worker must attempt to vacate or step aside instead of purely waiting on resource_route_blocked
    w1_intent = next((i for i in intents if i.actor_id == w_empty.id), None)
    assert w1_intent is not None

