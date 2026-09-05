"""Tests for AgentMemory.clone() — correctness & performance regression."""

from __future__ import annotations

import time

from arena_tactic.memory import AgentMemory
from arena_tactic.models import StrategicMode


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_populated_memory(
    *, n_explored: int = 1000, n_tasks: int = 50, n_tracks: int = 20,
) -> AgentMemory:
    """Return a non-trivial AgentMemory with populated fields."""
    m = AgentMemory()
    m.last_tick = 42
    m.last_mode = StrategicMode.BEACON
    m.mode_since_tick = 30
    m.no_resource_ticks = 5
    m.migration_cooldown_until_tick = 100
    m.last_core_id = "abc-123"
    m.last_core_position = (10, 20)
    m.spawn_eval_core_id = "abc-123"
    m.spawn_eval_started_tick = 1
    m.spawn_eval_status = "PASSED"
    m.previous_migration_position = (5, 15)
    m.core_damage_streak = 2
    m.last_core_damage_tick = 40
    m.submitted_ticks = 10
    m.accepted_ticks = 8

    # Sets
    m.obstacles = {(i, i + 1) for i in range(500)}
    m.explored = {(i * 2, i * 3) for i in range(n_explored)}
    m.mined_cells = {(i, -i) for i in range(200)}
    m.retreating_unit_ids = {f"entity_{i:04x}" for i in range(30)}

    # Flat dicts
    m.resource_observations = {(i, i): i * 10 for i in range(300)}
    m.resource_recheck_failures = {(i, i + 5): 1 for i in range(100)}
    m.resource_recheck_cooldowns = {(i, i + 3): 50 + i for i in range(80)}
    m.temporary_blocks = {(i, i * 2): 60 for i in range(150)}
    m.event_counts = {"UNIT_MOVE_SUCCEEDED": 99, "CORE_DAMAGED": 3}
    m.manual_squad_assignments = {
        f"entity_{i:04x}": "squad_base_defense" for i in range(10)
    }

    # Dicts of dicts
    m.enemy_tracks = {
        f"entity_{i:04x}": {
            "last_position": [100 + i, 200 + i],
            "last_seen_tick": 40 - i,
            "previous_distance_to_core": 50 + i,
            "approach_streak": i % 3,
        }
        for i in range(n_tracks)
    }
    m.unit_tasks = {
        f"entity_{i:04x}": {
            "kind": "explore",
            "sector": i % 4,
            "sector_since": 30,
            "failures": 0,
            "recent_cells": [[i, i + 1], [i + 1, i + 2]],
        }
        for i in range(n_tasks)
    }
    m.manual_assignments = {
        f"entity_{i:04x}": {
            "kind": "MOVE_TO_CELL",
            "target": [i, i],
            "until_tick": 100,
            "priority": 900,
        }
        for i in range(5)
    }
    m.scheduler_assignments = {
        f"entity_{i:04x}": {
            "task_id": f"task_{i}",
            "kind": "harvest",
            "role": "worker",
            "priority": 500,
            "lease_until_tick": 100,
            "target": [i * 10, i * 20],
        }
        for i in range(5)
    }
    m.objective_states = {
        "beacon": {"stage": "EN_ROUTE", "destination": [100, 200]},
        "attack": {"stage": "ENGAGING", "replan_count": 2},
    }
    m.policy_state = {
        "version": 3,
        "posture": "DEFENSIVE",
        "effective_tick": 40,
        "core_guard_vanguards": 4,
    }
    m.processed_event_ids = [f"entity_{i:04x}" for i in range(200)]
    m.analysis_tasks = [
        {"name": "economy_review", "interval_ticks": 300, "last_run_tick": 10},
        {"name": "beacon_check", "interval_ticks": 500, "last_run_tick": None},
    ]
    m.migration_recommendation = {"target": [50, 60], "score": 0.85}

    return m


# ---------------------------------------------------------------------------
# Correctness tests
# ---------------------------------------------------------------------------


def test_clone_returns_equal_content():
    """All fields of the clone must match the original."""
    original = _make_populated_memory()
    cloned = original.clone()

    # Scalars
    assert cloned.last_tick == original.last_tick
    assert cloned.last_mode is original.last_mode
    assert cloned.last_core_id == original.last_core_id
    assert cloned.last_core_position == original.last_core_position
    assert cloned.core_damage_streak == original.core_damage_streak

    # Sets
    assert cloned.explored == original.explored
    assert cloned.obstacles == original.obstacles
    assert cloned.mined_cells == original.mined_cells
    assert cloned.retreating_unit_ids == original.retreating_unit_ids

    # Flat dicts
    assert cloned.resource_observations == original.resource_observations
    assert cloned.resource_recheck_failures == original.resource_recheck_failures
    assert cloned.resource_recheck_cooldowns == original.resource_recheck_cooldowns
    assert cloned.temporary_blocks == original.temporary_blocks
    assert cloned.event_counts == original.event_counts
    assert cloned.manual_squad_assignments == original.manual_squad_assignments

    # Nested dicts
    assert cloned.enemy_tracks == original.enemy_tracks
    assert cloned.unit_tasks == original.unit_tasks
    assert cloned.manual_assignments == original.manual_assignments
    assert cloned.scheduler_assignments == original.scheduler_assignments
    assert cloned.objective_states == original.objective_states
    assert cloned.policy_state == original.policy_state

    # Lists
    assert cloned.processed_event_ids == original.processed_event_ids
    assert cloned.analysis_tasks == original.analysis_tasks
    assert cloned.migration_recommendation == original.migration_recommendation


def test_clone_is_independent_mutation_of_sets():
    """Mutating cloned set must not affect original."""
    original = _make_populated_memory()
    cloned = original.clone()

    cloned.explored.add((9999, 9999))
    cloned.obstacles.add((8888, 8888))
    cloned.mined_cells.add((7777, 7777))
    cloned.retreating_unit_ids.add("entity_ffff")

    assert (9999, 9999) not in original.explored
    assert (8888, 8888) not in original.obstacles
    assert (7777, 7777) not in original.mined_cells
    assert "entity_ffff" not in original.retreating_unit_ids


def test_clone_is_independent_mutation_of_dicts():
    """Mutating cloned dicts must not affect original."""
    original = _make_populated_memory()
    cloned = original.clone()

    new_cell = (99999, 88888)  # not in any original dict
    cloned.resource_observations[new_cell] = 9999
    cloned.temporary_blocks[new_cell] = 9999
    cloned.event_counts["NEW_EVENT"] = 1
    cloned.enemy_tracks["new_enemy"] = {
        "last_position": [0, 0],
        "last_seen_tick": 0,
        "previous_distance_to_core": 0,
        "approach_streak": 0,
    }
    cloned.unit_tasks["new_unit"] = {"kind": "hold"}
    cloned.policy_state["posture"] = "AGGRESSIVE"

    assert new_cell not in original.resource_observations
    assert new_cell not in original.temporary_blocks
    assert "NEW_EVENT" not in original.event_counts
    assert "new_enemy" not in original.enemy_tracks
    assert "new_unit" not in original.unit_tasks
    assert original.policy_state["posture"] == "DEFENSIVE"


def test_clone_is_independent_mutation_of_nested_dict_values():
    """Mutating values inside cloned nested dicts must not affect original."""
    original = _make_populated_memory()
    cloned = original.clone()

    # Mutate a value dict inside enemy_tracks
    first_key = next(iter(cloned.enemy_tracks))
    cloned.enemy_tracks[first_key]["last_seen_tick"] = 99999
    assert original.enemy_tracks[first_key]["last_seen_tick"] != 99999

    # Mutate a value dict inside unit_tasks
    task_key = next(iter(cloned.unit_tasks))
    cloned.unit_tasks[task_key]["kind"] = "mutated"
    assert original.unit_tasks[task_key]["kind"] != "mutated"


def test_clone_is_independent_mutation_of_lists():
    """Mutating cloned lists must not affect original."""
    original = _make_populated_memory()
    cloned = original.clone()

    cloned.processed_event_ids.append("entity_new")
    cloned.processed_event_ids[0] = "OVERWRITTEN"
    cloned.analysis_tasks.append({"name": "new_task"})
    cloned.analysis_tasks[0]["name"] = "mutated"
    cloned.migration_recommendation["score"] = 0.0

    assert "entity_new" not in original.processed_event_ids
    assert original.processed_event_ids[0] != "OVERWRITTEN"
    assert len(original.analysis_tasks) == 2
    assert original.analysis_tasks[0]["name"] == "economy_review"
    assert original.migration_recommendation["score"] == 0.85


def test_clone_empty_memory():
    """Cloning a fresh default memory must work."""
    original = AgentMemory()
    cloned = original.clone()
    assert cloned.explored == set()
    assert cloned.unit_tasks == {}
    assert cloned.last_tick == 0
    # Independence
    cloned.last_tick = 999
    assert original.last_tick == 0


def test_clone_preserves_all_fields():
    """Every dataclass field must be present on the clone (no omission)."""
    original = _make_populated_memory()
    cloned = original.clone()
    for field_name in original.__dataclass_fields__:
        assert hasattr(cloned, field_name), f"Missing field: {field_name}"


# ---------------------------------------------------------------------------
# Performance regression test
# ---------------------------------------------------------------------------


def test_clone_performance_with_large_explored_set():
    """
    DECISION_LATENCY_SPIKE (决策延迟激增) regression guard.

    With 100k explored coordinates, clone() must complete within 100 ms.
    Previous deepcopy implementation took 700–1200 ms and caused every tick
    to exceed the 500 ms planning budget.
    """
    m = AgentMemory()
    m.explored = {(i // 1000, i % 1000) for i in range(100_000)}
    m.obstacles = {(i // 500, i % 500) for i in range(20_000)}
    m.mined_cells = {(i, -i) for i in range(10_000)}
    m.resource_observations = {(i, i): i for i in range(10_000)}
    m.temporary_blocks = {(i, i * 2): 60 for i in range(5_000)}
    m.unit_tasks = {f"entity_{i:04x}": {"kind": "explore", "sector": i % 4} for i in range(100)}
    m.enemy_tracks = {
        f"entity_{i:04x}": {"last_position": [i, i], "last_seen_tick": 40, "previous_distance_to_core": 50, "approach_streak": 0}
        for i in range(50)
    }
    m.processed_event_ids = [f"entity_{i:04x}" for i in range(500)]

    start = time.perf_counter()
    cloned = m.clone()
    elapsed_ms = (time.perf_counter() - start) * 1000

    assert cloned.explored == m.explored
    assert cloned.obstacles == m.obstacles
    assert cloned.unit_tasks == m.unit_tasks
    assert elapsed_ms < 100, f"clone() took {elapsed_ms:.1f} ms — exceeds 100 ms budget"
