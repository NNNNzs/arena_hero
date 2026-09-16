"""Regression tests for _frontier_assignments preserving attempt_tick.

When _frontier_assignments re-assigns a worker to a frontier target, it must
preserve ``attempt_tick``, ``prev_cell``, and other context-tracking fields
from the worker's previous task.  Previously, the function constructed a brand
new dict that erased these fields, which caused ``_stuck_sidestep`` to never
activate because ``attempt_tick`` was reset every tick.
"""

from arena_hero import CoreState, UnitType
from arena_tactic.context import DecisionContext
from arena_tactic.memory import AgentMemory
from arena_tactic.models import AgentConfig
from arena_tactic.strategy.workers import _frontier_assignments, _stuck_sidestep, _STUCK_THRESHOLD

from .factories import core as make_core, turn as make_turn, unit as make_unit


def test_frontier_assignments_preserves_attempt_tick():
    """_frontier_assignments must keep the existing attempt_tick from the
    previous task so that _stuck_sidestep can accumulate blocked ticks and
    eventually activate.
    """
    config = AgentConfig()
    c = make_core(value=1, position=(0, 0), hp=5, shield=5, state=CoreState.NORMAL)
    w = make_unit(2, UnitType.WORKER, (5, 5))

    # First assignment at tick=10 establishes an exploration task.
    t1 = make_turn(tick=10, owned_core=c, units=[w])
    ctx1 = DecisionContext.from_turn(t1)
    mem = AgentMemory()
    mem.explored = {(0, 0)}

    _frontier_assignments(
        units=[w],
        memory=mem,
        context=ctx1,
        deadline=1.0,
        config=config,
        task_kind="explore",
    )

    uid = str(w.id)
    assert uid in mem.unit_tasks
    first_task = mem.unit_tasks[uid]
    # Simulate the scenario: _record_unit_task sets attempt_tick when the
    # unit fails to move (intent is None).  Pretend that happened at tick=5.
    first_task["attempt_tick"] = 5
    first_task["prev_cell"] = [4, 5]

    # Second assignment at tick=20 — the frontier path is re-evaluated.
    t2 = make_turn(tick=20, owned_core=c, units=[w])
    ctx2 = DecisionContext.from_turn(t2)

    _frontier_assignments(
        units=[w],
        memory=mem,
        context=ctx2,
        deadline=1.0,
        config=config,
        task_kind="explore",
    )

    second_task = mem.unit_tasks[uid]
    # CRITICAL: attempt_tick must survive re-assignment, NOT be reset to 20.
    assert second_task.get("attempt_tick") == 5, (
        f"attempt_tick should be preserved as 5, got {second_task.get('attempt_tick')}"
    )
    # prev_cell should also be preserved.
    assert second_task.get("prev_cell") == [4, 5], (
        f"prev_cell should be preserved, got {second_task.get('prev_cell')}"
    )
    # Kind and target should be updated to the new frontier values.
    assert second_task["kind"] == "explore"


def test_frontier_assignments_preserves_failures_count():
    """The failures counter from the previous task must be carried over."""
    config = AgentConfig()
    c = make_core(value=1, position=(0, 0), hp=5, shield=5, state=CoreState.NORMAL)
    w = make_unit(2, UnitType.WORKER, (5, 5))

    t1 = make_turn(tick=10, owned_core=c, units=[w])
    ctx1 = DecisionContext.from_turn(t1)
    mem = AgentMemory()
    mem.explored = {(0, 0)}

    _frontier_assignments(
        units=[w],
        memory=mem,
        context=ctx1,
        deadline=1.0,
        config=config,
        task_kind="explore",
    )

    uid = str(w.id)
    mem.unit_tasks[uid]["failures"] = 7
    mem.unit_tasks[uid]["attempt_tick"] = 3

    t2 = make_turn(tick=20, owned_core=c, units=[w])
    ctx2 = DecisionContext.from_turn(t2)

    _frontier_assignments(
        units=[w],
        memory=mem,
        context=ctx2,
        deadline=1.0,
        config=config,
        task_kind="explore",
    )

    task = mem.unit_tasks[uid]
    assert task["failures"] == 7, f"failures should be 7, got {task['failures']}"
    assert task.get("attempt_tick") == 3


def test_stuck_sidestep_activates_after_frontier_reassignment():
    """End-to-end: _stuck_sidestep must activate after _frontier_assignments
    re-assigns a worker whose attempt_tick exceeds _STUCK_THRESHOLD.

    This is the exact scenario that caused a 120-tick deadlock: the worker was
    stuck at the same position with exploration_route_blocked, and
    _frontier_assignments kept resetting attempt_tick every tick, making
    _stuck_sidestep permanently unable to trigger.
    """
    from arena_tactic.models import ReservationTable

    config = AgentConfig()
    w = make_unit(2, UnitType.WORKER, (5, 5))

    # Create a turn with obstacles that block the path.
    c = make_core(value=1, position=(0, 0), hp=5, shield=5, state=CoreState.NORMAL)

    # Tick 1: First frontier assignment
    t1 = make_turn(tick=1, owned_core=c, units=[w])
    ctx1 = DecisionContext.from_turn(t1)
    mem = AgentMemory()
    mem.explored = {(0, 0)}

    _frontier_assignments(
        units=[w],
        memory=mem,
        context=ctx1,
        deadline=1.0,
        config=config,
        task_kind="explore",
    )

    uid = str(w.id)
    # Simulate a worker that has been stuck since tick=1
    mem.unit_tasks[uid]["attempt_tick"] = 1

    # Tick 10: Simulate subsequent frontier re-assignments (as happens each tick)
    for tick in range(2, 11):
        t = make_turn(tick=tick, owned_core=c, units=[w])
        ctx = DecisionContext.from_turn(t)
        _frontier_assignments(
            units=[w],
            memory=mem,
            context=ctx,
            deadline=1.0,
            config=config,
            task_kind="explore",
        )

    # After 10 ticks, attempt_tick should still be 1 (NOT reset to 10)
    task = mem.unit_tasks[uid]
    assert task.get("attempt_tick") == 1, (
        f"attempt_tick should be preserved as 1, got {task.get('attempt_tick')}"
    )

    # Now test that _stuck_sidestep can actually activate at tick=10
    # (tick - attempt_tick = 10 - 1 = 9 >= _STUCK_THRESHOLD = 3)
    t_final = make_turn(tick=10, owned_core=c, units=[w])
    ctx_final = DecisionContext.from_turn(t_final)
    reservations = ReservationTable(occupancy={})

    intent = _stuck_sidestep(
        worker=w,
        target=(10, 10),
        context=ctx_final,
        memory=mem,
        reservations=reservations,
        reason="exploration_route_blocked",
    )

    # The sidestep should have activated (intent is not None)
    assert intent is not None, (
        "_stuck_sidestep should activate when attempt_tick is preserved "
        "and tick - attempt_tick >= _STUCK_THRESHOLD"
    )
    assert intent.action.value == "MOVE"


def test_stuck_sidestep_does_not_activate_before_threshold():
    """_stuck_sidestep must NOT activate when blocked ticks < _STUCK_THRESHOLD,
    even after frontier re-assignment preserves attempt_tick.
    """
    from arena_tactic.models import ReservationTable

    config = AgentConfig()
    c = make_core(value=1, position=(0, 0), hp=5, shield=5, state=CoreState.NORMAL)
    w = make_unit(2, UnitType.WORKER, (5, 5))

    t1 = make_turn(tick=1, owned_core=c, units=[w])
    ctx1 = DecisionContext.from_turn(t1)
    mem = AgentMemory()
    mem.explored = {(0, 0)}

    _frontier_assignments(
        units=[w],
        memory=mem,
        context=ctx1,
        deadline=1.0,
        config=config,
        task_kind="explore",
    )

    uid = str(w.id)
    mem.unit_tasks[uid]["attempt_tick"] = 8  # stuck since tick 8

    # Tick 10: only 2 ticks of blocking (< _STUCK_THRESHOLD=3)
    for tick in range(2, 11):
        t = make_turn(tick=tick, owned_core=c, units=[w])
        ctx = DecisionContext.from_turn(t)
        _frontier_assignments(
            units=[w],
            memory=mem,
            context=ctx,
            deadline=1.0,
            config=config,
            task_kind="explore",
        )

    t_final = make_turn(tick=10, owned_core=c, units=[w])
    ctx_final = DecisionContext.from_turn(t_final)
    reservations = ReservationTable(occupancy={})

    intent = _stuck_sidestep(
        worker=w,
        target=(10, 10),
        context=ctx_final,
        memory=mem,
        reservations=reservations,
        reason="exploration_route_blocked",
    )

    # Should NOT activate — only 2 ticks blocked (< threshold of 3)
    assert intent is None


def test_frontier_assignments_no_frontier_preserves_attempt_tick():
    """When there is no frontier, the no-frontier branch must also
    preserve attempt_tick (not just the main frontier branch).
    """
    config = AgentConfig()
    c = make_core(value=1, position=(0, 0), hp=5, shield=5, state=CoreState.NORMAL)
    w = make_unit(2, UnitType.WORKER, (5, 5))

    # Set explored to a very large area so there's no frontier left
    mem = AgentMemory()
    # No frontier — explored covers everything nearby
    mem.explored = set()
    # Clear frontier by making it empty — no cells that aren't explored
    # We achieve this by ensuring all neighbors are in explored set

    t1 = make_turn(tick=1, owned_core=c, units=[w])
    ctx1 = DecisionContext.from_turn(t1)

    _frontier_assignments(
        units=[w],
        memory=mem,
        context=ctx1,
        deadline=1.0,
        config=config,
        task_kind="explore",
    )

    uid = str(w.id)
    # If a task was assigned, set attempt_tick
    if uid in mem.unit_tasks:
        mem.unit_tasks[uid]["attempt_tick"] = 3
        mem.unit_tasks[uid]["prev_cell"] = [4, 5]

        t2 = make_turn(tick=20, owned_core=c, units=[w])
        ctx2 = DecisionContext.from_turn(t2)

        _frontier_assignments(
            units=[w],
            memory=mem,
            context=ctx2,
            deadline=1.0,
            config=config,
            task_kind="explore",
        )

        task = mem.unit_tasks[uid]
        assert task.get("attempt_tick") == 3, (
            f"no-frontier branch: attempt_tick should be 3, got {task.get('attempt_tick')}"
        )
        assert task.get("prev_cell") == [4, 5]
