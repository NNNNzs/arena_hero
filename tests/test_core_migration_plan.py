import pytest

from arena_tactic.objectives.core_migration import CoreMigrationInput, CoreMigrationPlan, MigrationStage


def facts(**changes):
    baseline = dict(tick=1, destination=(2, 0), cargo_workers_away=0, capacity=10,
                    stored_resources=5, core_moving=False)
    baseline.update(changes)
    return CoreMigrationInput(**baseline)


def test_migration_recall_precedes_start_move():
    state, _, tasks, candidates = CoreMigrationPlan((2, 0)).evaluate(facts(cargo_workers_away=1))
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
