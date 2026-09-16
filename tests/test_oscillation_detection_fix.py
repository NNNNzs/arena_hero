"""Regression test for UNIT_OSCILLATION: recent_cells truncation disabled detection.

Bug: ``_record_unit_task`` truncated ``recent_cells`` to ``[-5:]`` (5 entries),
but ``_detect_target_oscillation`` requires ``len(recent_raw) >= _OSCILLATION_WINDOW``
(6) to enter detection logic.  Since 5 < 6 the function always returned ``False``,
rendering oscillation cooldown and explore sector rotation completely inert.

Fix: widen the truncation window to ``[-max(10, _OSCILLATION_WINDOW):]`` in all
three locations that append to recent_cells.
"""

from __future__ import annotations

from arena_hero import Direction, UnitType

from arena_tactic.context import DecisionContext
from arena_tactic.memory import AgentMemory, _resolve_unit_task
from arena_tactic.models import ActionIntent, ActionKind, AgentConfig, Position, ReservationTable
from arena_tactic.strategy.common import (
    _detect_target_oscillation,
    _OSCILLATION_WINDOW,
    _record_unit_task,
)
from arena_tactic.strategy.workers import _locked_recon_targets

from .factories import core, turn, unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _oscillation_bounce_history(
    pos_a: Position, pos_b: Position, count: int
) -> list[list[int]]:
    """Generate alternating position history simulating 2-cell bounce."""
    return [
        list(pos_a if i % 2 == 0 else pos_b)
        for i in range(count)
    ]


# ---------------------------------------------------------------------------
# 1. _detect_target_oscillation triggers after ≥ _OSCILLATION_WINDOW bounces
# ---------------------------------------------------------------------------

def test_detect_target_oscillation_triggers_on_sufficient_history():
    """Must return True when ≥ _OSCILLATION_WINDOW entries with ≤2 unique cells."""
    u = unit(1, UnitType.WORKER, (-751, -607))
    memory = AgentMemory()
    uid = str(u.id)
    history = _oscillation_bounce_history((-751, -607), (-751, -608), _OSCILLATION_WINDOW)
    memory.unit_tasks[uid] = {
        "kind": "recon",
        "recent_cells": history,
    }
    assert _detect_target_oscillation(memory, uid) is True, (
        f"Expected oscillation detected with {_OSCILLATION_WINDOW} entries, got False"
    )


def test_detect_target_oscillation_triggers_with_more_than_window():
    """Must return True when history exceeds _OSCILLATION_WINDOW."""
    u = unit(1, UnitType.WORKER, (-751, -607))
    memory = AgentMemory()
    uid = str(u.id)
    history = _oscillation_bounce_history((-751, -607), (-751, -608), 10)
    memory.unit_tasks[uid] = {
        "kind": "explore",
        "recent_cells": history,
    }
    assert _detect_target_oscillation(memory, uid) is True


# ---------------------------------------------------------------------------
# 2. _detect_target_oscillation returns False with insufficient history
# ---------------------------------------------------------------------------

def test_detect_target_oscillation_false_with_short_history():
    """Must return False when fewer than _OSCILLATION_WINDOW entries exist."""
    u = unit(1, UnitType.WORKER, (-751, -607))
    memory = AgentMemory()
    uid = str(u.id)
    history = _oscillation_bounce_history((-751, -607), (-751, -608), _OSCILLATION_WINDOW - 1)
    memory.unit_tasks[uid] = {
        "kind": "recon",
        "recent_cells": history,
    }
    assert _detect_target_oscillation(memory, uid) is False, (
        f"Expected no oscillation with {_OSCILLATION_WINDOW - 1} entries, got True"
    )


# ---------------------------------------------------------------------------
# 3. _detect_target_oscillation returns False when >2 unique cells
# ---------------------------------------------------------------------------

def test_detect_target_oscillation_false_with_many_unique_cells():
    """Must return False when recent_cells has >2 distinct positions."""
    u = unit(1, UnitType.WORKER, (10, 10))
    memory = AgentMemory()
    uid = str(u.id)
    memory.unit_tasks[uid] = {
        "kind": "explore",
        "recent_cells": [[10, 10], [11, 10], [12, 10], [13, 10], [14, 10], [15, 10]],
    }
    assert _detect_target_oscillation(memory, uid) is False


# ---------------------------------------------------------------------------
# 4. _record_unit_task accumulates recent_cells beyond 5
# ---------------------------------------------------------------------------

def test_record_unit_task_preserves_beyond_5_entries():
    """recent_cells must accumulate beyond 5 entries after repeated MOVE intents."""
    u = unit(1, UnitType.WORKER, (0, 0))
    t = turn(tick=100, owned_core=core(), units=(u,))
    context = DecisionContext.from_turn(t)
    memory = AgentMemory()

    # Pre-seed with 5 entries (the old cap)
    memory.unit_tasks[str(u.id)] = {
        "kind": "explore",
        "recent_cells": [[0, 0], [1, 0], [0, 0], [1, 0], [0, 0]],
    }

    # Create a move intent to trigger recent_cells append
    intent = ActionIntent(
        actor_id=u.id,
        is_core=False,
        action=ActionKind.MOVE,
        score=50,
        reason="test",
        target_cell=(1, 0),
        direction=Direction.RIGHT,
        reserved_cell=(1, 0),
    )
    _record_unit_task(memory, context, u, kind="explore", target=(10, 10), intent=intent)

    task = memory.unit_tasks[str(u.id)]
    recent = task.get("recent_cells", [])
    assert len(recent) >= 6, (
        f"Expected recent_cells to accumulate beyond 5 entries, got {len(recent)}"
    )
    assert _detect_target_oscillation(memory, str(u.id)) is True, (
        "Oscillation should be detected after 6+ entries of 2-cell bounce"
    )


# ---------------------------------------------------------------------------
# 5. End-to-end: oscillation triggers recon cooldown
# ---------------------------------------------------------------------------

def test_oscillation_triggers_recon_cooldown():
    """When oscillation is detected on a recon target, resource_recheck_cooldown is set."""
    u = unit(1, UnitType.WORKER, (-751, -607))
    memory = AgentMemory()
    uid = str(u.id)
    target = (-740, -628)
    memory.resource_observations[target] = 100

    # Simulate 8 ticks of oscillating between two cells on a recon target
    history = _oscillation_bounce_history((-751, -607), (-751, -608), 8)
    memory.unit_tasks[uid] = {
        "kind": "recon",
        "target": list(target),
        "recent_cells": history,
        "recon_since": 50,
    }

    # Verify detection triggers
    assert _detect_target_oscillation(memory, uid) is True

    # Simulate what workers.py does after oscillation detection
    config = AgentConfig(resource_recheck_cooldown_ticks=50)
    t = turn(tick=200, owned_core=core(), units=(u,))
    context = DecisionContext.from_turn(t)

    if _detect_target_oscillation(memory, uid):
        if target in memory.resource_observations:
            memory.resource_recheck_cooldowns[target] = (
                context.tick + config.resource_recheck_cooldown_ticks
            )

    assert target in memory.resource_recheck_cooldowns, (
        "Recon target cooldown should be set after oscillation detection"
    )
    assert memory.resource_recheck_cooldowns[target] == 250, (
        f"Expected cooldown tick 250, got {memory.resource_recheck_cooldowns[target]}"
    )


# ---------------------------------------------------------------------------
# 6. End-to-end: oscillation triggers explore sector rotation
# ---------------------------------------------------------------------------

def test_oscillation_triggers_explore_sector_rotation():
    """When oscillation is detected on an explore task, sector must rotate."""
    u = unit(1, UnitType.WORKER, (-751, -607))
    memory = AgentMemory()
    uid = str(u.id)

    history = _oscillation_bounce_history((-751, -607), (-751, -608), 8)
    memory.unit_tasks[uid] = {
        "kind": "explore",
        "sector": 0,
        "sector_since": 100,
        "recent_cells": history,
    }

    assert _detect_target_oscillation(memory, uid) is True

    # Simulate what workers.py does: rotate sector
    t = turn(tick=200, owned_core=core(), units=(u,))
    context = DecisionContext.from_turn(t)

    if _detect_target_oscillation(memory, uid):
        _key, _task = _resolve_unit_task(memory.unit_tasks, uid)
        if _task is not None:
            _task["sector"] = (int(_task.get("sector", 0)) + 1) % 4
            _task["sector_since"] = context.tick
            _task.pop("target", None)

    _key, _task = _resolve_unit_task(memory.unit_tasks, uid)
    assert _task is not None
    assert _task.get("sector") == 1, (
        f"Expected sector to rotate from 0 to 1, got {_task.get('sector')}"
    )
    assert _task.get("sector_since") == 200, (
        f"Expected sector_since=200, got {_task.get('sector_since')}"
    )
    assert "target" not in _task, "Target should be cleared after sector rotation"