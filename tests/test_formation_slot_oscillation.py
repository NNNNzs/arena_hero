"""Regression tests for formation slot stickiness anti-oscillation.

Covers the scenario described in the UNIT_OSCILLATION incident (Tick 229352..229480):
Ranger 5aa67ac70b1c oscillating between [-951, 1568] and [-952, 1568] for 120 ticks
because ``_safe_slots`` used a pure greedy nearest-cell pick that flipped every tick
as the unit drifted one cell closer to a different candidate slot.

The fix introduces slot stickiness: once assigned, a slot is kept unless a clearly
better candidate is more than 1 cell closer.
"""

from __future__ import annotations

from uuid import UUID

from arena_hero import UnitType

from arena_tactic.context import DecisionContext
from arena_tactic.memory import AgentMemory
from arena_tactic.models import Position
from arena_tactic.navigation import distance
from arena_tactic.squad_coordination import (
    _load_previous_formation_slots,
    _safe_slots,
    _save_formation_slots,
    _SLOT_STICKINESS_BONUS,
)

from .factories import core, turn, unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _uuid(value: int) -> UUID:
    return UUID(int=value)


def _make_context_memory(
    unit_objs,
    *,
    obstacles: set[Position] | None = None,
    enemy_occupancy: tuple[Position, ...] = (),
    unit_tasks: dict | None = None,
    tick: int = 100,
    objective_states: dict | None = None,
):
    """Build DecisionContext and AgentMemory for slot assignment tests."""
    enemies = [
        unit(900 + i, UnitType.RANGER, pos, controlled=False)
        for i, pos in enumerate(enemy_occupancy)
    ]
    t = turn(
        tick=tick,
        owned_core=core(position=(0, 0)),
        units=tuple(unit_objs),
        enemies=enemies,
    )
    context = DecisionContext.from_turn(t)
    memory = AgentMemory(obstacles=obstacles or set())
    if unit_tasks:
        memory.unit_tasks = unit_tasks
    if objective_states:
        memory.objective_states = objective_states
    return context, memory


# ---------------------------------------------------------------------------
# 1. _load_previous_formation_slots / _save_formation_slots round-trip
# ---------------------------------------------------------------------------

def test_save_and_load_formation_slots_round_trip():
    """Saved slots survive a load from objective_states."""
    memory = AgentMemory()
    slots = {_uuid(1): (10, 20), _uuid(2): (11, 21)}
    _save_formation_slots(memory, slots)
    loaded = _load_previous_formation_slots(memory)
    assert loaded == slots


def test_load_previous_formation_slots_empty():
    """Loading from empty memory returns empty dict."""
    memory = AgentMemory()
    assert _load_previous_formation_slots(memory) == {}


def test_load_previous_formation_slots_ignores_malformed():
    """Malformed entries in objective_states are silently skipped."""
    memory = AgentMemory()
    memory.objective_states["squad_coordination"] = {
        "formation_slots": {
            "not-a-uuid": [1, 2],
            str(_uuid(1)): "not-a-cell",
            str(_uuid(3)): ["a", "b"],
        }
    }
    loaded = _load_previous_formation_slots(memory)
    assert loaded == {}


# ---------------------------------------------------------------------------
# 2. Slot stickiness: unit keeps assigned slot when distances are similar
# ---------------------------------------------------------------------------

def test_slot_stickiness_keeps_previous_slot_on_small_drift():
    """When a unit drifts 1 cell, it should keep its previously assigned slot."""
    # Unit at (5, 5) with target center at (10, 5).
    # Candidate slots include (8, 5) at distance 3 and (7, 6) at distance 3.
    # After moving to (6, 5), (7, 6) becomes distance 2 while (8, 5) is still 2.
    # Without stickiness, the unit might flip to (7, 6). With stickiness, it keeps (8, 5).
    u = unit(1, UnitType.VANGUARD, (6, 5))
    center: Position = (10, 5)
    context, memory = _make_context_memory([u])

    # Set previous slot assignment to (8, 5)
    _save_formation_slots(memory, {_uuid(1): (8, 5)})
    slots = _safe_slots(
        center, (u,), context, memory,
        regroup=False, extreme_split=False,
        pace_unit_id=None, anchor_unit_id=None,
    )
    assert slots[_uuid(1)] == (8, 5), (
        f"Unit should keep previous slot (8, 5) but got {slots[_uuid(1)]}"
    )


def test_slot_stickiness_allows_switch_when_significantly_better():
    """When a clearly better slot exists (>1 cell closer), stickiness is overridden."""
    u = unit(1, UnitType.VANGUARD, (6, 5))
    center: Position = (10, 5)
    context, memory = _make_context_memory([u])

    # Set previous slot to outer ring (13, 5) — distance 7 from unit
    _save_formation_slots(memory, {_uuid(1): (13, 5)})
    slots = _safe_slots(
        center, (u,), context, memory,
        regroup=False, extreme_split=False,
        pace_unit_id=None, anchor_unit_id=None,
    )
    # The new best slot should be much closer than 7
    assigned = slots[_uuid(1)]
    assert distance(u.position, assigned) < 7, (
        f"Unit should switch to a closer slot, but got {assigned} at distance "
        f"{distance(u.position, assigned)}"
    )


# ---------------------------------------------------------------------------
# 3. Multi-tick simulation: no slot oscillation across ticks
# ---------------------------------------------------------------------------

def test_multi_tick_slot_no_oscillation():
    """Simulate 10 ticks of movement; slot assignment must not flip-flop."""
    center: Position = (20, 5)
    memory = AgentMemory()
    slot_history: list[Position] = []

    for tick in range(100, 110):
        # Unit moves 1 cell right each tick (simulating movement toward slot)
        pos = (5 + (tick - 100), 5)
        u = unit(1, UnitType.RANGER, pos)
        context, memory = _make_context_memory(
            [u], tick=tick,
            objective_states=memory.objective_states,
        )
        slots = _safe_slots(
            center, (u,), context, memory,
            regroup=False, extreme_split=False,
            pace_unit_id=None, anchor_unit_id=None,
        )
        _save_formation_slots(memory, slots)
        slot_history.append(slots[_uuid(1)])

    # Check for oscillation: no A-B-A pattern where A != B
    # (i.e., the slot should not flip to a different value and back)
    for i in range(2, len(slot_history)):
        a, b, c = slot_history[i - 2], slot_history[i - 1], slot_history[i]
        assert not (a != b and c == a), (
            f"A-B-A slot oscillation at tick {i}: {a} -> {b} -> {c}"
        )


def test_multi_tick_slot_stability_with_two_candidates():
    """Exact regression: unit between two equidistant slots must not flip-flop.

    Simulates a unit marching toward the center with two candidate slots at
    similar distances.  The slot assignment must never exhibit an A-B-A
    oscillation pattern (flip to a different slot and back).
    """
    center: Position = (10, 5)
    memory = AgentMemory()
    slot_history: list[Position] = []

    for tick in range(100, 108):
        pos = (8 + (tick - 100), 5)
        u = unit(1, UnitType.RANGER, pos)
        context, memory = _make_context_memory(
            [u], tick=tick,
            objective_states=memory.objective_states,
        )
        slots = _safe_slots(
            center, (u,), context, memory,
            regroup=False, extreme_split=False,
            pace_unit_id=None, anchor_unit_id=None,
        )
        _save_formation_slots(memory, slots)
        slot_history.append(slots[_uuid(1)])

    # Check for A-B-A oscillation pattern
    for i in range(2, len(slot_history)):
        a, b, c = slot_history[i - 2], slot_history[i - 1], slot_history[i]
        assert not (a != b and c == a), (
            f"A-B-A slot oscillation at tick {i}: {a} -> {b} -> {c}"
        )


# ---------------------------------------------------------------------------
# 4. Slot stickiness does not interfere when slot becomes blocked
# ---------------------------------------------------------------------------

def test_slot_stickiness_respects_blocked_slots():
    """If the previous slot becomes blocked, a new slot is assigned."""
    center: Position = (10, 5)
    u = unit(1, UnitType.RANGER, (8, 5))
    prev_slots = {_uuid(1): (12, 5)}
    # (12, 5) is now blocked by an obstacle
    context, memory = _make_context_memory(
        [u], obstacles={(12, 5)}, objective_states={"squad_coordination": {"formation_slots": {str(_uuid(1)): [12, 5]}}},
    )
    slots = _safe_slots(
        center, (u,), context, memory,
        regroup=False, extreme_split=False,
        pace_unit_id=None, anchor_unit_id=None,
    )
    assert slots[_uuid(1)] != (12, 5), (
        "Should not assign a blocked slot"
    )


# ---------------------------------------------------------------------------
# 5. Slot stickiness with multiple units avoids collision
# ---------------------------------------------------------------------------

def test_slot_stickiness_multi_unit_no_collision():
    """Two units with sticky slots must not be assigned the same cell."""
    center: Position = (10, 5)
    u1 = unit(1, UnitType.VANGUARD, (8, 5))
    u2 = unit(2, UnitType.RANGER, (8, 6))
    context, memory = _make_context_memory(
        [u1, u2],
        objective_states={
            "squad_coordination": {
                "formation_slots": {
                    str(_uuid(1)): [12, 5],
                    str(_uuid(2)): [12, 5],  # both want same slot!
                }
            }
        },
    )
    slots = _safe_slots(
        center, (u1, u2), context, memory,
        regroup=False, extreme_split=False,
        pace_unit_id=None, anchor_unit_id=None,
    )
    assert slots[_uuid(1)] != slots[_uuid(2)], (
        f"Two units assigned the same slot: {slots[_uuid(1)]}"
    )


# ---------------------------------------------------------------------------
# 6. _save_formation_slots is idempotent
# ---------------------------------------------------------------------------

def test_save_formation_slots_idempotent():
    """Saving twice with same data produces same result."""
    memory = AgentMemory()
    slots = {_uuid(1): (10, 20)}
    _save_formation_slots(memory, slots)
    _save_formation_slots(memory, slots)
    loaded = _load_previous_formation_slots(memory)
    assert loaded == slots


# ---------------------------------------------------------------------------
# 7. Slot stickiness bonus constant is correct
# ---------------------------------------------------------------------------

def test_slot_stickiness_bonus_value():
    """The stickiness bonus should be exactly 1 cell."""
    assert _SLOT_STICKINESS_BONUS == 1


# ---------------------------------------------------------------------------
# 8. Fresh start: no previous slots means greedy assignment
# ---------------------------------------------------------------------------

def test_fresh_start_uses_greedy_assignment():
    """Without previous slots, the nearest candidate is chosen."""
    center: Position = (10, 5)
    u = unit(1, UnitType.VANGUARD, (8, 5))
    context, memory = _make_context_memory([u])
    slots = _safe_slots(
        center, (u,), context, memory,
        regroup=False, extreme_split=False,
        pace_unit_id=None, anchor_unit_id=None,
    )
    assigned = slots[_uuid(1)]
    # The nearest candidate to (8, 5) among formation slots around (10, 5)
    # should be the closest one
    assert distance(u.position, assigned) <= 3, (
        f"Fresh assignment should pick a nearby slot, got {assigned}"
    )
