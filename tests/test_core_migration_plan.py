import pytest

from arena_tactic.objectives.core_migration import CoreMigrationInput, CoreMigrationPlan, MigrationStage
from arena_tactic import AgentMemory, AgentRuntime
from arena_tactic.models import AgentConfig
from arena_hero import UnitType
from .factories import core, turn, unit


def facts(**changes):
    baseline = dict(tick=1, destination=(2, 0), cargo_workers_pending=0, capacity=10,
                    stored_resources=5, core_moving=False)
    baseline.update(changes)
    return CoreMigrationInput(**baseline)


def test_migration_recall_precedes_start_move():
    state, _, tasks, candidates = CoreMigrationPlan((2, 0)).evaluate(facts(cargo_workers_pending=1))
    assert state.stage is MigrationStage.RECALL
    assert tasks[0].kind == "RECALL_CARGO_WORKERS" and candidates == ("RECALL_CARGO",)


def test_moving_core_continues_without_restart():
    state, _, tasks, candidates = CoreMigrationPlan((2, 0), MigrationStage.MOVING, True).evaluate(facts(core_moving=True, move_progress=2))
    assert state.stage is MigrationStage.MOVING
    assert tasks[0].kind == "CONTINUE_CORE_MOVE" and "START_MOVE" not in candidates


def test_start_move_is_not_repeated_before_a_fresh_observation():
    state, _, tasks, candidates = CoreMigrationPlan((2, 0), MigrationStage.START, True).evaluate(facts())
    assert state.stage is MigrationStage.START
    assert tasks[0].kind == "AWAIT_CORE_MOVE" and "START_MOVE" not in candidates


def test_failed_fourth_tick_replans_leg():
    state, _, tasks, candidates = CoreMigrationPlan((2, 0), MigrationStage.MOVING, True).evaluate(facts(core_moving=True, move_progress=4, move_failed=True))
    assert state.stage is MigrationStage.REPLAN and state.replan_count == 1
    assert tasks[0].kind == "REPLAN_CORE_LEG" and candidates == ("REPLAN_LEG",)


def test_migration_preserves_capacity_constraints():
    with pytest.raises(ValueError, match="over-capacity"):
        CoreMigrationPlan((2, 0)).evaluate(facts(capacity=10, stored_resources=11))


def test_enabled_migration_starts_one_safe_current_leg_only():
    game_turn = turn(owned_core=core(position=(0, 0)), beacon_position=(2, 0))

    result = AgentRuntime(config=AgentConfig(core_migration_v1=True)).decide(game_turn)

    core_intent = next(item for item in result.intents if item.is_core)
    assert core_intent.action.value == "START_MOVE"
    assert core_intent.reason == "core_migration_start_safe_leg"


def test_completed_observed_leg_rearms_exactly_one_next_current_leg():
    plan = CoreMigrationPlan((3, 0), MigrationStage.START, start_attempted=True)
    moving, _, _, _ = plan.evaluate(facts(core_moving=True, move_progress=1))
    landed, _, tasks, candidates = moving.evaluate(facts(core_moving=False, arrived=False))
    awaiting, _, _, next_candidates = landed.evaluate(facts(core_moving=False, arrived=False))

    assert moving.stage is MigrationStage.MOVING
    assert landed.stage is MigrationStage.START and landed.start_attempted
    assert tasks[0].kind == "START_CORE_MOVE" and candidates == ("START_MOVE",)
    assert awaiting.start_attempted and next_candidates == ("AWAIT_MOVE_STATE",)


def test_enabled_migration_recalls_current_cargo_workers_before_starting_core_leg():
    worker = unit(1, UnitType.WORKER, (3, 0), cargo=1)
    result = AgentRuntime(config=AgentConfig(core_migration_v1=True)).decide(
        turn(owned_core=core(position=(0, 0)), units=(worker,), beacon_position=(5, 0))
    )

    intent = next(item for item in result.intents if item.actor_id == worker.id)
    assert intent.reason == "core_migration_recall_cargo"


def test_enabled_migration_deposits_colocated_cargo_before_starting_core_leg():
    owned_core = core(position=(0, 0))
    worker = unit(1, UnitType.WORKER, (0, 0), cargo=1)

    result = AgentRuntime(config=AgentConfig(core_migration_v1=True)).decide(
        turn(owned_core=owned_core, units=(worker,), beacon_position=(5, 0))
    )

    worker_intent = next(item for item in result.intents if item.actor_id == worker.id)
    core_intent = next(item for item in result.intents if item.is_core)
    assert worker_intent.action.value == "DEPOSIT"
    assert worker_intent.reason == "core_migration_deposit_cargo"
    assert core_intent.action.value != "START_MOVE"


def test_enabled_migration_respects_cooldown_and_recent_origin():
    state = {"migration": {"stage": "START", "destination": [0, 0],
                           "start_attempted": False, "replan_count": 0}}
    cooling = AgentRuntime(
        memory=AgentMemory(last_tick=9, migration_cooldown_until_tick=12,
                           objective_states=state),
        config=AgentConfig(core_migration_v1=True),
    ).decide(turn(tick=10, owned_core=core(position=(1, 0))))
    cooling_core = next(item for item in cooling.intents if item.is_core)
    assert cooling_core.action.value != "START_MOVE"

    active = AgentRuntime(
        memory=AgentMemory(last_tick=12, previous_migration_position=(0, 0),
                           objective_states=state),
        config=AgentConfig(core_migration_v1=True),
    ).decide(turn(tick=13, owned_core=core(position=(1, 0))))
    active_core = next(item for item in active.intents if item.is_core)
    assert active_core.action.value == "START_MOVE"
    assert active_core.reserved_cell != (0, 0)
