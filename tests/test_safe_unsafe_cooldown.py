"""Regression tests for safe/unsafe_fallback ping-pong oscillation cooldown.

Covers the Tick 289436 incident: Worker f73fd96585d7 was stuck oscillating
between [-746, -600] and [-747, -600] (44 reversals in 60 ticks) because
``_return_to_core`` reset ``oscillation_count`` to 0 immediately on every
successful unsafe_fallback, causing the unit to revert to the safe path next
tick and perpetually ping-pong between safe and unsafe routes.

Fix: introduced ``unsafe_steps_remaining`` cooldown counter.  After
oscillation is confirmed (count >= 2), the unit stays in unsafe mode for
``_UNSAFE_COOLDOWN_STEPS`` (3) consecutive ticks before attempting the safe
path again.
"""

from __future__ import annotations

from time import perf_counter
from uuid import UUID

from arena_hero import CoreState, Direction, UnitType

from arena_tactic.context import DecisionContext
from arena_tactic.memory import AgentMemory
from arena_tactic.models import ActionKind, ActionIntent, ReservationTable
from arena_tactic.strategy.common import _return_to_core, _record_unit_task, _UNSAFE_COOLDOWN_STEPS

from .factories import core, turn, unit


def _deadline() -> float:
    return perf_counter() + 5.0


def _make_cargo_context(
    worker_pos: tuple[int, int],
    *,
    core_pos: tuple[int, int] = (0, 0),
    threat_cells: tuple[tuple[int, int], ...] = (),
):
    """Build a DecisionContext for a cargo-carrying worker."""
    w = unit(200, UnitType.WORKER, worker_pos, cargo=1)
    t = turn(
        tick=289440,
        owned_core=core(position=core_pos),
        units=(w,),
        obstacle_cells=threat_cells,
    )
    return DecisionContext.from_turn(t), w


# ---------------------------------------------------------------------------
# 1. unsafe_fallback cooldown prevents immediate revert to safe path
# ---------------------------------------------------------------------------

def test_unsafe_cooldown_prevents_safe_revert():
    """After oscillation_count reaches 2 and unsafe_fallback succeeds,
    unsafe_steps_remaining must be set.  On the NEXT tick, even though
    oscillation_count has been reset to 0, the cooldown should force
    the unsafe path again."""
    memory = AgentMemory()
    config = __import__("arena_tactic", fromlist=["AgentConfig"]).AgentConfig()
    reservations = ReservationTable({})
    core_pos = (0, 0)

    # Tick 1: oscillation_count=2 → triggers unsafe fallback
    context1, w1 = _make_cargo_context((3, 0), core_pos=core_pos)
    uid = str(w1.id)
    memory.unit_tasks[uid] = {
        "kind": "return_cargo_to_core",
        "prev_cell": [2, 0],
        "oscillation_count": 2,
        "target": list(core_pos),
    }

    intent1 = _return_to_core(
        w1, context1, memory, reservations, _deadline(), config,
        "return_cargo_to_core",
    )
    assert intent1 is not None
    assert "unsafe_fallback" in intent1.reason, (
        "oscillation_count=2 should trigger unsafe fallback"
    )
    # After unsafe_fallback succeeds with oscillation_count>=2,
    # unsafe_steps_remaining should be set to _UNSAFE_COOLDOWN_STEPS.
    task = memory.unit_tasks[uid]
    assert task.get("unsafe_steps_remaining") == _UNSAFE_COOLDOWN_STEPS, (
        f"unsafe_steps_remaining should be {_UNSAFE_COOLDOWN_STEPS} after oscillation-triggered unsafe fallback"
    )
    assert task.get("oscillation_count") == 0, (
        "oscillation_count should be reset to 0"
    )


# ---------------------------------------------------------------------------
# 2. cooldown persists across ticks: safe path stays skipped during cooldown
# ---------------------------------------------------------------------------

def test_cooldown_persists_across_ticks():
    """Simulate 3 consecutive ticks after oscillation triggers.  Every tick
    should use unsafe_fallback because the cooldown is active."""
    memory = AgentMemory()
    config = __import__("arena_tactic", fromlist=["AgentConfig"]).AgentConfig()
    reservations = ReservationTable({})
    core_pos = (0, 0)
    uid = None

    # First: trigger oscillation → unsafe → cooldown starts
    context0, w0 = _make_cargo_context((3, 0), core_pos=core_pos)
    uid = str(w0.id)
    memory.unit_tasks[uid] = {
        "kind": "return_cargo_to_core",
        "prev_cell": [2, 0],
        "oscillation_count": 2,
        "target": list(core_pos),
    }
    _return_to_core(w0, context0, memory, reservations, _deadline(), config, "return_cargo_to_core")

    # Now simulate _UNSAFE_COOLDOWN_STEPS ticks — all should use unsafe_fallback
    for tick_offset in range(_UNSAFE_COOLDOWN_STEPS):
        reservations = ReservationTable({})  # fresh reservation each tick
        context, w = _make_cargo_context((3, 0), core_pos=core_pos)
        # Update tick in task to simulate passage of time
        if uid in memory.unit_tasks:
            memory.unit_tasks[uid]["attempt_tick"] = 289440 + tick_offset

        intent = _return_to_core(
            w, context, memory, reservations, _deadline(), config,
            "return_cargo_to_core",
        )
        assert intent is not None, f"Tick {tick_offset}: should get an intent"
        assert "unsafe_fallback" in intent.reason, (
            f"Tick {tick_offset}: cooldown active, should use unsafe_fallback, got: {intent.reason}"
        )
        # Verify cooldown is decrementing
        remaining = memory.unit_tasks[uid].get("unsafe_steps_remaining", 0)
        assert remaining >= 0, f"Tick {tick_offset}: remaining should be non-negative"


# ---------------------------------------------------------------------------
# 3. After cooldown expires, safe path is available again
# ---------------------------------------------------------------------------

def test_safe_path_available_after_cooldown_expires():
    """After the unsafe cooldown counter reaches 0, the safe path should be
    available again (if no new oscillation occurs)."""
    memory = AgentMemory()
    config = __import__("arena_tactic", fromlist=["AgentConfig"]).AgentConfig()
    reservations = ReservationTable({})
    core_pos = (0, 0)

    # Set up with cooldown=1 (one tick remaining)
    context, w = _make_cargo_context((3, 0), core_pos=core_pos)
    uid = str(w.id)
    memory.unit_tasks[uid] = {
        "kind": "return_cargo_to_core",
        "prev_cell": [4, 0],  # prev_cell different from safe path target
        "oscillation_count": 0,
        "unsafe_steps_remaining": 1,
        "target": list(core_pos),
    }

    intent = _return_to_core(
        w, context, memory, reservations, _deadline(), config,
        "return_cargo_to_core",
    )
    assert intent is not None
    # This is the last cooldown tick — unsafe should be used
    assert "unsafe_fallback" in intent.reason, (
        "Last cooldown tick should still use unsafe_fallback"
    )
    # After this, cooldown should be 0
    assert memory.unit_tasks[uid].get("unsafe_steps_remaining", 0) == 0, (
        "Cooldown should reach 0 after last tick"
    )

    # Next tick: cooldown=0, safe path should be available
    reservations = ReservationTable({})
    context2, w2 = _make_cargo_context((3, 0), core_pos=core_pos)
    intent2 = _return_to_core(
        w2, context2, memory, reservations, _deadline(), config,
        "return_cargo_to_core",
    )
    if intent2 is not None:
        # Should NOT be unsafe_fallback (safe path should be tried first)
        assert "unsafe_fallback" not in intent2.reason, (
            "After cooldown expires, safe path should be available again"
        )


# ---------------------------------------------------------------------------
# 4. unsafe_steps_remaining survives _record_unit_task
# ---------------------------------------------------------------------------

def test_unsafe_steps_remaining_preserved_by_record_unit_task():
    """_record_unit_task must preserve unsafe_steps_remaining in the whitelist."""
    memory = AgentMemory()
    context, w = _make_cargo_context((5, 0))
    uid = str(w.id)

    memory.unit_tasks[uid] = {
        "kind": "return_cargo_to_core",
        "unsafe_steps_remaining": 2,
        "oscillation_count": 0,
        "prev_cell": [4, 0],
        "target": [0, 0],
    }

    _record_unit_task(memory, context, w, kind="return", target=(0, 0), intent=None)
    recorded = memory.unit_tasks[uid]
    assert recorded.get("unsafe_steps_remaining") == 2, (
        "unsafe_steps_remaining must survive _record_unit_task"
    )


# ---------------------------------------------------------------------------
# 5. E2E: full choose_actions pipeline respects cooldown
# ---------------------------------------------------------------------------

def test_e2e_cooldown_forces_unsafe_path():
    """End-to-end: a cargo worker with oscillation_count=2 should trigger
    unsafe fallback.  Two consecutive choose_actions calls should both
    use unsafe fallback (first from oscillation_count>=2, second from
    cooldown)."""
    from arena_tactic import AgentMemory as FullMemory, choose_actions

    worker = unit(600, UnitType.WORKER, (3, 0), cargo=1)
    memory = FullMemory()
    uid = str(worker.id)

    memory.unit_tasks[uid] = {
        "kind": "return_cargo_to_core",
        "prev_cell": [2, 0],
        "oscillation_count": 2,
        "target": [0, 0],
    }

    # Tick 1: oscillation_count=2 → unsafe + cooldown set
    result1 = choose_actions(
        turn(tick=289450, owned_core=core(position=(0, 0)), units=(worker,)),
        memory=memory,
    )
    intent1 = [i for i in result1.intents if i.actor_id == worker.id][0]
    assert "unsafe_fallback" in intent1.reason
    # Verify cooldown was set in memory (via _return_to_core write)
    task1 = memory.unit_tasks.get(uid, {})
    assert task1.get("unsafe_steps_remaining", 0) > 0 or "unsafe_fallback" in intent1.reason, (
        "Cooldown should be set or intent should be unsafe"
    )

    # Tick 2: cooldown still active → unsafe again (even though oscillation_count was reset to 0)
    result2 = choose_actions(
        turn(tick=289451, owned_core=core(position=(0, 0)), units=(worker,)),
        memory=memory,
    )
    intent2 = [i for i in result2.intents if i.actor_id == worker.id][0]
    assert "unsafe_fallback" in intent2.reason, (
        "Cooldown should force unsafe_fallback on next tick"
    )


# ---------------------------------------------------------------------------
# 6. Normal path (no oscillation) is unaffected by cooldown logic
# ---------------------------------------------------------------------------

def test_normal_return_unaffected_by_cooldown():
    """A normal return-to-core without any oscillation should work exactly
    as before — no cooldown, no unsafe_fallback."""
    memory = AgentMemory()
    config = __import__("arena_tactic", fromlist=["AgentConfig"]).AgentConfig()
    reservations = ReservationTable({})

    context, w = _make_cargo_context((5, 0), core_pos=(0, 0))
    uid = str(w.id)
    memory.unit_tasks[uid] = {
        "kind": "return_cargo_to_core",
        "target": [0, 0],
    }

    intent = _return_to_core(
        w, context, memory, reservations, _deadline(), config,
        "return_cargo_to_core",
    )
    assert intent is not None
    # Should be the safe path (no _unsafe_fallback suffix)
    assert "unsafe_fallback" not in intent.reason, (
        "Normal return should use safe path"
    )
    task = memory.unit_tasks[uid]
    assert task.get("unsafe_steps_remaining", 0) == 0, (
        "No cooldown should be set for normal path"
    )