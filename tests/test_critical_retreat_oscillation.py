"""Regression tests for critical-ranger retreat oscillation fix and task tracking.

Covers:
1. Critical ranger retreat records unit_task with prev_cell so oscillation tracking works.
2. _return_to_core with avoid_threats=True detects 2-cell oscillation loop at threat
   boundaries and falls back to unsafe path to break oscillation.
3. Oscillation counter increments on repeated visits to prev_cell and resets on normal steps.
"""

from __future__ import annotations

from time import perf_counter

from arena_hero import CoreState, CoreView, Direction, UnitType, UnitView
from arena_tactic import AgentConfig, AgentMemory, choose_actions
from arena_tactic.context import DecisionContext
from arena_tactic.identity import entity_alias
from arena_tactic.models import ActionKind, ActionIntent, ReservationTable
from arena_tactic.strategy.common import _return_to_core

from .factories import core, turn, unit, uuid


def _deadline() -> float:
    return perf_counter() + 5.0


# ---------------------------------------------------------------------------
# 1. Verification of task recording and anti-oscillation for hp=1 retreat
# ---------------------------------------------------------------------------

def test_hp1_ranger_without_manual_still_retreats():
    """Without a manual assignment, hp=1 ranger must still auto-retreat."""
    ranger = unit(10, UnitType.RANGER, (5, 5), hp=1)
    result = choose_actions(
        turn(tick=10, owned_core=core(), units=(ranger,)),
    )
    ranger_intents = [i for i in result.intents if i.actor_id == ranger.id]
    assert len(ranger_intents) == 1
    intent = ranger_intents[0]
    assert "critical" in intent.reason or "retreat" in intent.reason


def test_hp1_ranger_records_unit_task_and_prev_cell():
    """Critical retreat must record unit_tasks including prev_cell so oscillation tracking works."""
    ranger = unit(10, UnitType.RANGER, (5, 5), hp=1)
    memory = AgentMemory()
    result = choose_actions(
        turn(tick=10, owned_core=core(), units=(ranger,)),
        memory=memory,
    )
    ranger_intents = [i for i in result.intents if i.actor_id == ranger.id]
    assert len(ranger_intents) == 1
    recorded = result.next_memory.unit_tasks.get(str(ranger.id))
    assert recorded is not None
    assert recorded.get("kind") == "critical_ranger_retreat"
    assert recorded.get("prev_cell") == [5, 5]


# ---------------------------------------------------------------------------
# 2. _return_to_core oscillation detection
# ---------------------------------------------------------------------------

def _make_retreat_context(
    ranger_pos: tuple[int, int],
    threat_cells: tuple[tuple[int, int], ...] = (),
    core_pos: tuple[int, int] = (0, 0),
):
    """Build a minimal DecisionContext for testing _return_to_core."""
    ranger = unit(10, UnitType.RANGER, ranger_pos, hp=1)
    t = turn(
        tick=10,
        owned_core=core(position=core_pos),
        units=(ranger,),
        obstacle_cells=threat_cells,
    )
    return DecisionContext.from_turn(t), ranger


def test_return_to_core_oscillation_detection_skips_safe_path():
    """When _return_to_core detects 2+ consecutive oscillation ticks, it
    should skip the safe (avoid_threats=True) path and try the unsafe
    fallback instead."""
    memory = AgentMemory()
    config = AgentConfig()
    reservations = ReservationTable({})
    context, ranger = _make_retreat_context(ranger_pos=(3, 0))
    uid = str(ranger.id)

    # Pre-populate unit_tasks to simulate 2 consecutive oscillation detections.
    memory.unit_tasks[uid] = {
        "kind": "critical_ranger_retreat",
        "prev_cell": [2, 0],
        "oscillation_count": 2,
        "target": [0, 0],
    }

    intent = _return_to_core(
        ranger, context, memory, reservations, _deadline(), config,
        "critical_ranger_retreat",
    )
    # Should get a move intent (unsafe fallback), not None.
    assert intent is not None
    assert intent.action is ActionKind.MOVE
    assert "unsafe_fallback" in intent.reason


def test_return_to_core_normal_path_used_without_oscillation():
    """Without oscillation history, _return_to_core should use the safe path."""
    memory = AgentMemory()
    config = AgentConfig()
    reservations = ReservationTable({})
    context, ranger = _make_retreat_context(ranger_pos=(3, 0))

    intent = _return_to_core(
        ranger, context, memory, reservations, _deadline(), config,
        "critical_ranger_retreat",
    )
    assert intent is not None
    assert intent.action is ActionKind.MOVE
    assert "unsafe_fallback" not in intent.reason


def test_return_to_core_oscillation_counter_increments_on_repeat():
    """When the safe path returns the prev_cell, oscillation_count should
    increment so that subsequent calls skip the safe path."""
    memory = AgentMemory()
    config = AgentConfig()
    reservations = ReservationTable({})
    context, ranger = _make_retreat_context(ranger_pos=(3, 0))

    uid = str(ranger.id)
    # From (3,0) toward (0,0), the safe path returns (2,0).
    # prev_cell=(2,0) matches, so oscillation_count should increment.
    memory.unit_tasks[uid] = {
        "kind": "critical_ranger_retreat",
        "prev_cell": [2, 0],
        "oscillation_count": 0,
        "target": [0, 0],
    }

    intent = _return_to_core(
        ranger, context, memory, reservations, _deadline(), config,
        "critical_ranger_retreat",
    )
    assert intent is not None
    updated = memory.unit_tasks.get(uid, {})
    assert updated.get("oscillation_count", 0) == 1


def test_return_to_core_resets_counter_on_different_step():
    """oscillation_count resets when the safe-path step differs from prev_cell."""
    memory = AgentMemory()
    config = AgentConfig()
    reservations = ReservationTable({})
    # Place ranger at (3,1) with prev_cell=(3,0).  The safe path toward (0,0)
    # goes to (2,1), which is NOT (3,0), so counter should reset.
    context, ranger = _make_retreat_context(ranger_pos=(3, 1))

    uid = str(ranger.id)
    memory.unit_tasks[uid] = {
        "kind": "critical_ranger_retreat",
        "prev_cell": [3, 0],
        "oscillation_count": 1,
        "target": [0, 0],
    }

    intent = _return_to_core(
        ranger, context, memory, reservations, _deadline(), config,
        "critical_ranger_retreat",
    )
    assert intent is not None
    updated = memory.unit_tasks.get(uid, {})
    assert updated.get("oscillation_count", 0) == 0


def test_full_integration_oscillation_breakout_to_unsafe_fallback():
    """End-to-end: hp=1 ranger oscillating between cells automatically breaks
    out to unsafe fallback once oscillation_count reaches 2."""
    ranger = unit(10, UnitType.RANGER, (3, 0), hp=1)
    memory = AgentMemory()
    alias = entity_alias(ranger.id)
    assert alias is not None
    # Simulate oscillation history: ranger was at (2,0) and stepped to (3,0).
    memory.unit_tasks[str(ranger.id)] = {
        "kind": "critical_ranger_retreat",
        "prev_cell": [2, 0],
        "oscillation_count": 2,
        "target": [0, 0],
    }
    result = choose_actions(
        turn(tick=268560, owned_core=core(position=(0, 0)), units=(ranger,)),
        memory=memory,
    )
    ranger_intents = [i for i in result.intents if i.actor_id == ranger.id]
    assert len(ranger_intents) == 1
    intent = ranger_intents[0]
    assert intent.action is ActionKind.MOVE
    assert "unsafe_fallback" in intent.reason
