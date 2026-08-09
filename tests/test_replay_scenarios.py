from arena_hero import BeaconStatus, UnitType

from arena_tactic import AgentConfig, AgentMemory, StrategicMode, choose_actions
from arena_tactic.models import ActionKind

from .factories import core, event, turn, unit


def test_four_tick_resource_refill_replaces_depleted_observation():
    worker = unit(1, UnitType.WORKER, (0, 0))
    memory = AgentMemory(
        last_tick=3,
        resource_observations={(2, 0): 3},
        unit_tasks={str(worker.id): {"kind": "resource", "target": [2, 0]}},
    )
    depleted = choose_actions(
        turn(
            tick=4,
            owned_core=core(position=(-2, 0)),
            units=(worker,),
            events=(
                event(
                    400,
                    "HARVEST_FAILED",
                    tick=4,
                    reason_code="RESOURCE_DEPLETED",
                    position=(2, 0),
                ),
            ),
        ),
        memory=memory,
    )
    assert (2, 0) not in depleted.next_memory.resource_observations

    refilled = choose_actions(
        turn(
            tick=5,
            owned_core=core(position=(-2, 0)),
            units=(worker,),
            resource_cells=((1, 0),),
        ),
        memory=depleted.next_memory,
    )
    worker_intent = next(
        intent for intent in refilled.intents if intent.actor_id == worker.id
    )
    assert worker_intent.target_cell == (1, 0)
    assert worker_intent.action is ActionKind.MOVE


def test_consecutive_move_failures_are_counted_once_and_replanned():
    worker = unit(1, UnitType.WORKER, (0, 0))
    memory = AgentMemory(
        unit_tasks={str(worker.id): {"kind": "explore", "target": [5, 0]}}
    )
    for tick in (1, 2):
        result = choose_actions(
            turn(
                tick=tick,
                owned_core=core(position=(-2, 0)),
                units=(worker,),
                events=(event(500 + tick, "UNIT_MOVE_FAILED", tick=tick),),
            ),
            memory=memory,
        )
        memory = result.next_memory
    assert memory.event_counts["UNIT_MOVE_FAILED"] == 2
    assert any(intent.action is ActionKind.MOVE for intent in result.intents)


def test_enemy_core_appearing_switches_stable_force_to_attack():
    units = (
        unit(1, UnitType.VANGUARD, (0, 1)),
        unit(2, UnitType.VANGUARD, (1, 0)),
        unit(3, UnitType.RANGER, (0, 2)),
    )
    before = choose_actions(
        turn(
            tick=10,
            owned_core=core(),
            units=units,
            beacon_status=BeaconStatus.CARRIED,
            beacon_carrier_id=core(value=999).id,
        )
    )
    appeared = choose_actions(
        turn(
            tick=11,
            owned_core=core(),
            units=units,
            enemies=(core(value=300, position=(5, 0), controlled=False),),
        ),
        memory=before.next_memory,
    )
    assert appeared.mode is StrategicMode.ATTACK


def test_core_damage_and_beacon_drop_create_recovery_then_pickup_actions():
    recovery = choose_actions(turn(owned_core=core(hp=3), resources=2))
    assert recovery.mode is StrategicMode.RECOVER
    assert next(intent for intent in recovery.intents if intent.is_core).action is ActionKind.HEAL

    roster = (
        unit(1, UnitType.WORKER, (-1, 0)),
        unit(2, UnitType.WORKER, (-2, 0)),
        unit(3, UnitType.WORKER, (-3, 0)),
        unit(4, UnitType.VANGUARD, (2, 0)),
        unit(5, UnitType.VANGUARD, (0, 2)),
        unit(6, UnitType.RANGER, (0, 1)),
    )
    dropped = choose_actions(
        turn(
            tick=2,
            owned_core=core(),
            units=roster,
            beacon_position=(2, 0),
            beacon_status=BeaconStatus.GROUND,
            events=(event(700, "BEACON_DROPPED_ON_DEATH", tick=2, position=(2, 0)),),
        ),
        memory=recovery.next_memory,
    )
    assert dropped.mode is StrategicMode.BEACON
    assert any(intent.action is ActionKind.PICKUP_BEACON for intent in dropped.intents)


def test_expired_budget_uses_legal_deterministic_fallback_plan():
    worker = unit(1, UnitType.WORKER, (0, 0))
    game_turn = turn(
        owned_core=core(position=(-2, 0)),
        units=(worker,),
        resource_cells=((20, 0),),
    )
    result = choose_actions(
        game_turn,
        config=AgentConfig(planning_budget_ms=0.0001),
    )
    assert result.timed_out
    assert game_turn.plan.unit_actions[worker.id].type in {"MOVE", "WAIT"}
