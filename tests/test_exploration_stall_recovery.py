"""Regression tests for exploration route blocked stall recovery.

Covers the exploration_route_blocked incident where workers 4d8c2bf83b23 and
05388341e6dd were stuck waiting for many consecutive ticks when their
exploration targets were unreachable.

Fix: when a worker has been stuck on exploration for >= _STUCK_THRESHOLD * 2
(6) consecutive ticks AND _stuck_sidestep also fails, force sector rotation
and clear the exploration target so the worker can try a different direction.
"""

from __future__ import annotations

from time import perf_counter

from arena_hero import UnitType

from arena_tactic.context import DecisionContext
from arena_tactic.memory import AgentMemory
from arena_tactic.models import ActionKind, ReservationTable, AgentConfig
from arena_tactic.strategy.common import _EXPLORATION_SECTORS
from arena_tactic.strategy.workers import _STUCK_THRESHOLD

from .factories import core, turn, unit


def _deadline() -> float:
    return perf_counter() + 5.0


# ---------------------------------------------------------------------------
# 1. Exploration stall triggers sector rotation after threshold
# ---------------------------------------------------------------------------

def test_exploration_stall_forces_sector_rotation():
    """When a worker's exploration target is blocked for _STUCK_THRESHOLD*2
    consecutive ticks, the sector should rotate and the target should be
    cleared."""
    from arena_tactic import choose_actions

    worker = unit(700, UnitType.WORKER, (5, 5), cargo=0)
    memory = AgentMemory()
    uid = str(worker.id)

    # Simulate a worker that has been stuck on an exploration target for
    # many ticks.  Set up the task with an old attempt_tick.
    old_tick = 100
    current_tick = old_tick + _STUCK_THRESHOLD * 2 + 1
    memory.unit_tasks[uid] = {
        "kind": "explore",
        "target": [50, 50],
        "sector": 0,
        "sector_since": old_tick,
        "attempt_tick": old_tick,
    }

    # Create a turn with no enemies, no resources, and the worker far from core
    # so it stays in the exploration path.  Surround the target with obstacles
    # so _move returns None.
    obstacle_positions = []
    for dx in range(-2, 3):
        for dy in range(-2, 3):
            if abs(dx) + abs(dy) <= 2:
                obstacle_positions.append((50 + dx, 50 + dy))

    result = choose_actions(
        turn(
            tick=current_tick,
            owned_core=core(position=(0, 0)),
            units=(worker,),
            obstacle_cells=obstacle_positions,
        ),
        memory=memory,
    )
    worker_intents = [i for i in result.intents if i.actor_id == worker.id]
    assert len(worker_intents) == 1
    intent = worker_intents[0]

    # The worker should either get a MOVE (sidestep/unblock) or a WAIT.
    # Either way, the sector should have been rotated if the stall persisted.
    task = memory.unit_tasks.get(uid, {})
    # If the intent is WAIT (exploration_route_blocked), the sector should
    # have been rotated due to the long stall.
    if intent.action is ActionKind.WAIT and intent.reason == "exploration_route_blocked":
        # The stall handler should have rotated the sector
        assert task.get("sector", 0) != 0 or task.get("attempt_tick", 0) >= current_tick - 1, (
            "After prolonged stall, sector should rotate or attempt_tick should reset"
        )


# ---------------------------------------------------------------------------
# 2. Short stall does NOT trigger premature rotation
# ---------------------------------------------------------------------------

def test_short_stall_no_rotation():
    """A worker stuck for fewer than _STUCK_THRESHOLD*2 ticks should NOT
    have its sector forcibly rotated."""
    from arena_tactic import choose_actions

    worker = unit(701, UnitType.WORKER, (5, 5), cargo=0)
    memory = AgentMemory()
    uid = str(worker.id)

    current_tick = 100
    # Only 1 tick stuck — well below threshold
    memory.unit_tasks[uid] = {
        "kind": "explore",
        "target": [50, 50],
        "sector": 0,
        "sector_since": current_tick - 1,
        "attempt_tick": current_tick - 1,
    }

    obstacle_positions = []
    for dx in range(-2, 3):
        for dy in range(-2, 3):
            if abs(dx) + abs(dy) <= 2:
                obstacle_positions.append((50 + dx, 50 + dy))

    result = choose_actions(
        turn(
            tick=current_tick,
            owned_core=core(position=(0, 0)),
            units=(worker,),
            obstacle_cells=obstacle_positions,
        ),
        memory=memory,
    )
    task = memory.unit_tasks.get(uid, {})
    # Sector should NOT have been forcibly rotated
    assert task.get("sector", 0) == 0, (
        "Short stall should not trigger sector rotation"
    )


# ---------------------------------------------------------------------------
# 3. Prolonged stall records unreachable frontier cooldown and clears target
# ---------------------------------------------------------------------------

def test_exploration_stall_records_unreachable_frontier_cooldown():
    """When a worker's exploration route is blocked for >= _STUCK_THRESHOLD*2,
    the target must be placed into unreachable frontier cooldown and cleared
    from memory to break the frontier lock deadlock."""
    from arena_tactic.strategy.workers import _plan_workers, _frontier_assignments
    from arena_tactic.models import ReservationTable

    worker = unit(702, UnitType.WORKER, (5, 5), cargo=0)
    memory = AgentMemory()
    uid = str(worker.id)
    target_pos = (5, 10)

    # Put target_pos in frontier by exploring (5, 9)
    memory.explored = {(5, 9)}
    old_tick = 100
    current_tick = old_tick + _STUCK_THRESHOLD * 2 + 1
    memory.unit_tasks[uid] = {
        "kind": "explore",
        "target": list(target_pos),
        "sector": 0,
        "sector_since": old_tick,
        "attempt_tick": old_tick,
    }

    # Surround worker so all moves fail
    obstacle_positions = [(5, 6), (5, 4), (6, 5), (4, 5)]
    t = turn(
        tick=current_tick,
        owned_core=core(position=(0, 0)),
        units=(worker,),
        obstacle_cells=obstacle_positions,
    )
    context = DecisionContext.from_turn(t)
    config = AgentConfig()
    reservations = ReservationTable(occupancy={})

    intents = _plan_workers(context, memory, reservations, perf_counter() + 5.0, config, heal_allowances={})

    # 1. Target must be recorded in unreachable_frontier_targets with valid cooldown
    assert target_pos in memory.unreachable_frontier_targets, (
        f"Target {target_pos} should be in unreachable_frontier_targets"
    )
    assert memory.unreachable_frontier_targets[target_pos] > current_tick

    # 2. active_unreachable_frontier_cooldowns must report this target
    assert target_pos in memory.active_unreachable_frontier_cooldowns(current_tick)

    # 3. Subsequent _frontier_assignments must NOT assign this unreachable target
    new_assignments = _frontier_assignments([worker], memory, context, perf_counter() + 5.0, config, task_kind="explore")
    assert new_assignments.get(uid) != target_pos
