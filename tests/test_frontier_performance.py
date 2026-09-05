"""Regression test for AgentMemory.frontier caching and _frontier_assignments performance."""

import time
from arena_hero.models import UnitType
from arena_tactic.memory import AgentMemory
from arena_tactic.models import AgentConfig
from arena_tactic.strategy.workers import _frontier_assignments
from tests.factories import turn, core, unit


def test_frontier_cache_returns_same_set_without_recomputing():
    memory = AgentMemory()
    memory.explored = {(x, y) for x in range(-50, 50) for y in range(-50, 50)}
    f1 = memory.frontier()
    assert len(f1) == 400
    assert memory._frontier_cache is f1

    # Second call returns the exact cached object
    f2 = memory.frontier()
    assert f2 is f1

    # After clone, cache is invalidated
    cloned = memory.clone()
    assert cloned._frontier_cache is None
    f3 = cloned.frontier()
    assert f3 == f1
    assert cloned._frontier_cache is f3


def test_frontier_assignments_performance_with_large_fog_map():
    memory = AgentMemory()
    # 200x200 = 40,000 explored cells
    memory.explored = {(x, y) for x in range(-100, 100) for y in range(-100, 100)}
    workers = [unit(i, UnitType.WORKER, (0, 0)) for i in range(1, 13)]
    t = turn(owned_core=core(position=(0, 0)), units=workers)
    from arena_tactic.context import DecisionContext
    ctx = DecisionContext.from_turn(t)
    config = AgentConfig()

    t0 = time.perf_counter()
    assignments = _frontier_assignments(
        workers,
        memory,
        ctx,
        deadline=t0 + 2.0,
        config=config,
        task_kind="frontier_recon",
    )
    elapsed = time.perf_counter() - t0
    assert len(assignments) == 12
    # Must complete well within 200ms
    assert elapsed < 0.200, f"_frontier_assignments took {elapsed:.3f}s, expected < 0.200s"
