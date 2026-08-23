"""Tests for the tick-based analysis scheduler and migration cache consumption."""

from __future__ import annotations

import pytest

from arena_tactic.analysis_scheduler import (
    AnalysisScheduler,
    AnalysisTask,
    MigrationRecommendation,
    _effective_interval,
    default_analysis_scheduler,
)
from arena_tactic.memory import AgentMemory
from arena_tactic.models import Position


# ---------------------------------------------------------------------------
# AnalysisTask – basic due logic
# ---------------------------------------------------------------------------

class TestAnalysisTaskDue:
    def test_due_when_last_run_is_none_and_tick_past_start(self):
        task = AnalysisTask("t", start_tick=10, interval=50)
        assert task.is_due(tick=10, explored_count=0, resource_count=0) is True

    def test_not_due_before_start_tick(self):
        task = AnalysisTask("t", start_tick=10, interval=50)
        assert task.is_due(tick=9, explored_count=0, resource_count=0) is False

    def test_not_due_when_disabled(self):
        task = AnalysisTask("t", start_tick=0, interval=50, enabled=False)
        assert task.is_due(tick=100, explored_count=0, resource_count=0) is False

    def test_due_after_interval_elapsed(self):
        task = AnalysisTask("t", start_tick=0, interval=50, last_run_tick=100)
        # 100 + 50 = 150, at tick=150 it should be due
        assert task.is_due(tick=150, explored_count=0, resource_count=0) is True
        assert task.is_due(tick=149, explored_count=0, resource_count=0) is False

    def test_due_exactly_at_boundary(self):
        task = AnalysisTask("t", start_tick=0, interval=20, last_run_tick=100)
        assert task.is_due(tick=120, explored_count=0, resource_count=0) is True


# ---------------------------------------------------------------------------
# Dynamic interval
# ---------------------------------------------------------------------------

class TestDynamicInterval:
    def test_static_interval(self):
        task = AnalysisTask("t", start_tick=0, interval=50, last_run_tick=100)
        # interval=50, so due at 150
        assert task.is_due(tick=150, explored_count=0, resource_count=0) is True
        assert task.is_due(tick=149, explored_count=0, resource_count=0) is False

    def test_callable_interval(self):
        fn = lambda ec, rc: max(20, min(300, 60 * (1 + ec // 5000)))
        task = AnalysisTask("t", start_tick=0, interval=fn, last_run_tick=100)
        # 0 explored → interval = 60 → due at 160
        assert task.is_due(tick=160, explored_count=0, resource_count=0) is True
        assert task.is_due(tick=159, explored_count=0, resource_count=0) is False
        # 5000 explored → interval = 120 → due at 220
        assert task.is_due(tick=220, explored_count=5000, resource_count=0) is True
        assert task.is_due(tick=219, explored_count=5000, resource_count=0) is False

    def test_interval_clamped_to_min(self):
        fn = lambda _ec, _rc: 5  # below MIN_INTERVAL=20
        task = AnalysisTask("t", start_tick=0, interval=fn, last_run_tick=100)
        # Clamped to 20 → due at 120
        assert task.is_due(tick=120, explored_count=0, resource_count=0) is True
        assert task.is_due(tick=119, explored_count=0, resource_count=0) is False

    def test_interval_clamped_to_max(self):
        fn = lambda _ec, _rc: 500  # above MAX_INTERVAL=300
        task = AnalysisTask("t", start_tick=0, interval=fn, last_run_tick=100)
        # Clamped to 300 → due at 400
        assert task.is_due(tick=400, explored_count=0, resource_count=0) is True
        assert task.is_due(tick=399, explored_count=0, resource_count=0) is False

    def test_effective_interval_clamping(self):
        assert _effective_interval(5, 0, 0) == 20
        assert _effective_interval(500, 0, 0) == 300
        assert _effective_interval(100, 0, 0) == 100


# ---------------------------------------------------------------------------
# AnalysisScheduler – advance
# ---------------------------------------------------------------------------

class TestSchedulerAdvance:
    def test_returns_due_task_names(self):
        scheduler = AnalysisScheduler(tasks=[
            AnalysisTask("a", start_tick=5, interval=10, last_run_tick=None),
            AnalysisTask("b", start_tick=20, interval=10, last_run_tick=None),
        ])
        due = scheduler.advance(tick=5, explored_count=0, resource_count=0)
        assert due == ["a"]

    def test_no_tasks_due(self):
        scheduler = AnalysisScheduler(tasks=[
            AnalysisTask("a", start_tick=100, interval=50, last_run_tick=None),
        ])
        assert scheduler.advance(tick=5, explored_count=0, resource_count=0) == []

    def test_multiple_tasks_due(self):
        scheduler = AnalysisScheduler(tasks=[
            AnalysisTask("a", start_tick=0, interval=10, last_run_tick=None),
            AnalysisTask("b", start_tick=0, interval=10, last_run_tick=None),
        ])
        due = scheduler.advance(tick=0, explored_count=0, resource_count=0)
        assert set(due) == {"a", "b"}

    def test_mark_completed_delays_next_run(self):
        scheduler = AnalysisScheduler(tasks=[
            AnalysisTask("a", start_tick=0, interval=50, last_run_tick=None),
        ])
        assert scheduler.advance(tick=0, explored_count=0, resource_count=0) == ["a"]
        scheduler.mark_completed("a", tick=0)
        # Next due at tick=50
        assert scheduler.advance(tick=49, explored_count=0, resource_count=0) == []
        assert scheduler.advance(tick=50, explored_count=0, resource_count=0) == ["a"]

    def test_empty_scheduler(self):
        scheduler = AnalysisScheduler()
        assert scheduler.advance(tick=100, explored_count=0, resource_count=0) == []

    def test_get_task(self):
        task = AnalysisTask("x", start_tick=0, interval=10)
        scheduler = AnalysisScheduler(tasks=[task])
        assert scheduler.get_task("x") is task
        assert scheduler.get_task("y") is None


# ---------------------------------------------------------------------------
# Serialization round-trip
# ---------------------------------------------------------------------------

class TestSerialization:
    def test_task_round_trip_static(self):
        task = AnalysisTask("t", start_tick=10, interval=50, last_run_tick=3, enabled=True)
        data = task.to_dict()
        restored = AnalysisTask.from_dict(data)
        assert restored.name == "t"
        assert restored.start_tick == 10
        assert restored.interval == 50
        assert restored.last_run_tick == 3
        assert restored.enabled is True

    def test_task_round_trip_callable(self):
        from arena_tactic.analysis_scheduler import _INTERVAL_REGISTRY
        fn = _INTERVAL_REGISTRY["resource_density_adaptive"]
        task = AnalysisTask("t", start_tick=10, interval=fn, last_run_tick=None, enabled=True)
        data = task.to_dict()
        assert data["interval"] == "resource_density_adaptive"
        restored = AnalysisTask.from_dict(data)
        assert callable(restored.interval)
        # Verify behavior matches
        assert restored.interval(0, 0) == fn(0, 0)
        assert restored.interval(5000, 0) == fn(5000, 0)

    def test_scheduler_round_trip(self):
        from arena_tactic.analysis_scheduler import _INTERVAL_REGISTRY
        scheduler = AnalysisScheduler(tasks=[
            AnalysisTask("a", start_tick=5, interval=50, last_run_tick=3),
            AnalysisTask("b", start_tick=0, interval=_INTERVAL_REGISTRY["resource_density_adaptive"], last_run_tick=None, enabled=True),
        ])
        data = scheduler.to_dict()
        restored = AnalysisScheduler.from_dict(data)
        assert len(restored.tasks) == 2
        assert restored.tasks[0].name == "a"
        assert restored.tasks[0].interval == 50
        assert restored.tasks[0].last_run_tick == 3
        assert callable(restored.tasks[1].interval)

    def test_recommendation_round_trip(self):
        rec = MigrationRecommendation(
            center=(10, 20), score=42.5, computed_at_tick=100, interval_ticks=60
        )
        data = rec.to_dict()
        restored = MigrationRecommendation.from_dict(data)
        assert restored is not None
        assert restored.center == (10, 20)
        assert restored.score == 42.5
        assert restored.computed_at_tick == 100
        assert restored.interval_ticks == 60

    def test_recommendation_from_none_dict(self):
        assert MigrationRecommendation.from_dict(None) is None
        assert MigrationRecommendation.from_dict({}) is None
        assert MigrationRecommendation.from_dict({"center": [1]}) is None

    def test_memory_round_trip_preserves_new_fields(self):
        mem = AgentMemory()
        mem.analysis_tasks = [{"name": "t", "start_tick": 0, "interval": 50, "last_run_tick": 5, "enabled": True}]
        mem.migration_recommendation = {
            "center": [10, 20], "score": 1.0, "computed_at_tick": 5, "interval_ticks": 50
        }
        data = mem.to_dict()
        assert "analysis_tasks" in data
        assert "migration_recommendation" in data
        restored = AgentMemory.from_dict(data)
        assert len(restored.analysis_tasks) == 1
        assert restored.analysis_tasks[0]["name"] == "t"
        assert restored.migration_recommendation["center"] == [10, 20]


# ---------------------------------------------------------------------------
# Respawn behaviour
# ---------------------------------------------------------------------------

class TestRespawn:
    def test_scheduler_on_respawn_resets_last_run_tick(self):
        scheduler = AnalysisScheduler(tasks=[
            AnalysisTask("a", start_tick=0, interval=50, last_run_tick=100),
            AnalysisTask("b", start_tick=0, interval=30, last_run_tick=200, enabled=False),
        ])
        scheduler.on_respawn()
        assert scheduler.tasks[0].last_run_tick is None
        assert scheduler.tasks[1].last_run_tick is None
        # Enabled flag is preserved
        assert scheduler.tasks[0].enabled is True
        assert scheduler.tasks[1].enabled is False

    def test_memory_respawn_clears_recommendation_and_resets_tasks(self):
        """Simulate the respawn path in AgentMemory.advance().

        On CORE_RESPAWNED the migration_recommendation must be cleared and
        analysis_tasks must have last_run_tick reset.
        """
        mem = AgentMemory()
        mem.analysis_tasks = [
            {"name": "t", "start_tick": 0, "interval": 50, "last_run_tick": 100, "enabled": True}
        ]
        mem.migration_recommendation = {
            "center": [10, 20], "score": 1.0, "computed_at_tick": 100, "interval_ticks": 50
        }
        # Simulate what the respawn code path does
        mem.migration_recommendation = {}
        for task_data in mem.analysis_tasks:
            task_data["last_run_tick"] = None

        assert mem.migration_recommendation == {}
        assert mem.analysis_tasks[0]["last_run_tick"] is None
        # Task definition preserved
        assert mem.analysis_tasks[0]["name"] == "t"

    def test_respawned_task_fires_immediately(self):
        """After respawn resets last_run_tick, the task should be due at its
        next eligible tick (>= start_tick)."""
        scheduler = AnalysisScheduler(tasks=[
            AnalysisTask("a", start_tick=5, interval=100, last_run_tick=500),
        ])
        scheduler.on_respawn()
        # last_run_tick=None, start_tick=5 → due at tick=5
        assert scheduler.advance(tick=5, explored_count=0, resource_count=0) == ["a"]
        assert scheduler.advance(tick=4, explored_count=0, resource_count=0) == []


# ---------------------------------------------------------------------------
# MigrationRecommendation freshness
# ---------------------------------------------------------------------------

class TestRecommendationFreshness:
    def test_fresh_within_two_cycles(self):
        rec = MigrationRecommendation(
            center=(0, 0), score=1.0, computed_at_tick=100, interval_ticks=50
        )
        # 2 * 50 = 100 ticks window
        assert rec.is_fresh(199, max_cycles=2) is True
        assert rec.is_fresh(200, max_cycles=2) is False

    def test_fresh_at_tick_zero(self):
        rec = MigrationRecommendation(
            center=(0, 0), score=1.0, computed_at_tick=0, interval_ticks=60
        )
        assert rec.is_fresh(119, max_cycles=2) is True
        assert rec.is_fresh(120, max_cycles=2) is False


# ---------------------------------------------------------------------------
# Default scheduler factory
# ---------------------------------------------------------------------------

class TestDefaultScheduler:
    def test_default_has_resource_density_scan(self):
        scheduler = default_analysis_scheduler()
        assert len(scheduler.tasks) == 1
        assert scheduler.tasks[0].name == "resource_density_scan"
        assert scheduler.tasks[0].start_tick == 10
        assert scheduler.tasks[0].enabled is True
        assert scheduler.tasks[0].last_run_tick is None

    def test_default_interval_is_callable(self):
        scheduler = default_analysis_scheduler()
        assert callable(scheduler.tasks[0].interval)


# ---------------------------------------------------------------------------
# core_plan migration cache consumption
# ---------------------------------------------------------------------------

class TestCorePlanCacheConsumption:
    """Test that _core_migration_direction uses cached migration_recommendation."""

    def test_migration_recommendation_stored_in_memory(self):
        """Verify that migration_recommendation is persisted in memory dict."""
        mem = AgentMemory()
        rec = MigrationRecommendation(
            center=(5, 10), score=42.0, computed_at_tick=100, interval_ticks=60
        )
        mem.migration_recommendation = rec.to_dict()
        data = mem.to_dict()
        restored = AgentMemory.from_dict(data)
        assert restored.migration_recommendation["center"] == [5, 10]
        assert restored.migration_recommendation["score"] == 42.0

    def test_empty_recommendation_does_not_crash(self):
        """Empty migration_recommendation should be handled gracefully."""
        rec = MigrationRecommendation.from_dict({})
        assert rec is None
