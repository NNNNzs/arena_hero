"""Regression tests for _distant_retreat_fallback_intent multi-cell oscillation fix.

Covers the scenario described in the UNIT_OSCILLATION incident (Tick 225150~225160):
Ranger 5c604f8a2a33 at [-2598, 1176] was stuck in a 4-tick cycle oscillation
([-2597,1176] -> LEFT -> [-2598,1176] -> DOWN -> [-2598,1177] -> UP ->
 [-2598,1176] -> RIGHT -> [-2597,1176]) because only a single prev_cell was
tracked. The fix introduces multi-tier taboo penalties via recent_cells.
"""

from __future__ import annotations

from uuid import UUID

from arena_hero import Direction, UnitType

from arena_tactic.context import DecisionContext
from arena_tactic.memory import AgentMemory
from arena_tactic.models import ActionKind, Position, ReservationTable
from arena_tactic.navigation import distance
from arena_tactic.strategy.common import _distant_retreat_fallback_intent

from .factories import core, turn, unit


def _make_context_and_memory(
    unit_obj,
    *,
    obstacles: set[Position] | None = None,
    enemy_occupancy: tuple[Position, ...] = (),
    unit_tasks: dict | list | None = None,
    tick: int = 100,
    core_pos: Position = (0, 0),
):
    """Build a DecisionContext and AgentMemory suitable for testing fallback."""
    enemies = [
        unit(900 + i, UnitType.RANGER, pos, controlled=False)
        for i, pos in enumerate(enemy_occupancy)
    ]
    context = DecisionContext.from_turn(
        turn(tick=tick, owned_core=core(position=core_pos), units=(unit_obj,), enemies=enemies)
    )
    memory = AgentMemory(obstacles=obstacles or set())
    if isinstance(unit_tasks, list):
        memory.unit_tasks = {str(unit_obj.id): unit_tasks[0]} if unit_tasks else {}
    elif isinstance(unit_tasks, dict):
        memory.unit_tasks = {str(unit_obj.id): task for task in unit_tasks}
    occupancy: dict[Position, int] = {}
    for u in context.units:
        occupancy[u.position] = occupancy.get(u.position, 0) + 1
    reservations = ReservationTable(occupancy=occupancy)
    return context, memory, reservations


# ---------------------------------------------------------------------------
# 1. Classic 4-cell loop: the unit must NOT return to any of the 4 recently
#    visited cells when a fresh direction exists.
# ---------------------------------------------------------------------------

def test_distant_retreat_breaks_4_cell_loop():
    """Simulate the exact 4-cell oscillation pattern from the incident.

    Layout (obstacles form an L-shaped pocket):
        B = [-2600, 1176] (blocked, west of unit)
        Unit at [-2598, 1176], target at [0, 0]
        recent_cells simulates 4-tick cycle history.

    Without the fix, the unit would pick [-2597, 1176] (RIGHT) because
    [-2598, 1177] (DOWN) has the same distance and prev_cell only blocked
    the immediately previous cell.  With the fix, both [-2597, 1176] and
    [-2598, 1177] carry taboo penalties; the unit is forced to pick a
    genuinely fresh direction if one exists.
    """
    ranger = unit(1, UnitType.RANGER, (-2598, 1176))
    obstacles = {(-2600, 1176)}  # block west
    # Recent cells: the 4-cell loop history
    recent = [[-2598, 1176], [-2597, 1176], [-2598, 1176], [-2598, 1177]]
    tasks = [{
        "kind": "distant_retreat_fallback",
        "target": [0, 0],
        "recent_cells": recent,
        "prev_cell": [-2598, 1177],
    }]
    context, memory, reservations = _make_context_and_memory(
        ranger, obstacles=obstacles, unit_tasks=tasks, tick=225160,
    )
    target: Position = (0, 0)
    intent = _distant_retreat_fallback_intent(
        ranger, target, context, memory, reservations,
        "unit_retreat_to_core_heal",
    )
    assert intent is not None
    # The chosen cell must NOT be any of the recently visited cells.
    chosen = intent.reserved_cell
    recent_set = {tuple(c) for c in recent}
    assert chosen not in recent_set, (
        f"Unit chose {chosen} which is in recent_cells {recent_set}; "
        "multi-tier taboo should have prevented this."
    )


# ---------------------------------------------------------------------------
# 2. Simulate multiple ticks to verify the loop is fully broken.
# ---------------------------------------------------------------------------

def test_distant_retreat_multi_tick_no_oscillation():
    """Simulate 6 consecutive ticks; the unit must never revisit a recent cell."""
    target: Position = (0, 0)
    recent: list[Position] = []
    current: Position = (-2598, 1176)

    for tick in range(225150, 225156):
        ranger = unit(1, UnitType.RANGER, current)
        obstacles = {(-2600, 1176)}
        task_entry = {
            "kind": "distant_retreat_fallback",
            "target": [0, 0],
            "recent_cells": [list(c) for c in recent],
            "prev_cell": list(recent[-1]) if recent else list(current),
        }
        context, memory, reservations = _make_context_and_memory(
            ranger, obstacles=obstacles, unit_tasks=[task_entry], tick=tick,
        )
        intent = _distant_retreat_fallback_intent(
            ranger, target, context, memory, reservations,
            "unit_retreat_to_core_heal",
        )
        if intent is None:
            break  # blocked — acceptable
        chosen = intent.reserved_cell
        # Must not revisit any of the last 5 cells
        assert chosen not in set(recent[-5:]), (
            f"Tick {tick}: chose {chosen} but it's in recent {recent[-5:]}"
        )
        recent.append(current)
        current = chosen


# ---------------------------------------------------------------------------
# 3. Backward compatibility: legacy prev_cell (no recent_cells) still works.
# ---------------------------------------------------------------------------

def test_distant_retreat_backward_compat_prev_cell_only():
    """When unit_tasks has only prev_cell (legacy format), the function
    should still work and apply taboo to that single cell."""
    ranger = unit(1, UnitType.RANGER, (-2598, 1176))
    task_entry = {
        "kind": "distant_retreat_fallback",
        "target": [0, 0],
        "prev_cell": [-2597, 1176],
    }
    context, memory, reservations = _make_context_and_memory(
        ranger, unit_tasks=[task_entry], tick=100,
    )
    target: Position = (0, 0)
    intent = _distant_retreat_fallback_intent(
        ranger, target, context, memory, reservations,
        "unit_retreat_to_core_heal",
    )
    assert intent is not None
    # The prev_cell should be penalised (taboo of 600 from migration)
    assert intent.reserved_cell != (-2597, 1176), (
        "Legacy prev_cell should still be penalised."
    )
    # After the call, recent_cells should be written for future ticks.
    task = memory.unit_tasks[str(ranger.id)]
    assert "recent_cells" in task
    assert len(task["recent_cells"]) >= 1


# ---------------------------------------------------------------------------
# 4. No candidates → returns None (unchanged behavior).
# ---------------------------------------------------------------------------

def test_distant_retreat_returns_none_when_all_blocked():
    ranger = unit(1, UnitType.RANGER, (-2598, 1176))
    # Block all 4 neighbors
    obstacles = {(-2597, 1176), (-2599, 1176), (-2598, 1175), (-2598, 1177)}
    context, memory, reservations = _make_context_and_memory(
        ranger, obstacles=obstacles, tick=100,
    )
    target: Position = (0, 0)
    intent = _distant_retreat_fallback_intent(
        ranger, target, context, memory, reservations,
        "unit_retreat_to_core_heal",
    )
    assert intent is None


# ---------------------------------------------------------------------------
# 5. Threatened cell gets higher priority in sorting than taboo cells.
# ---------------------------------------------------------------------------

def test_distant_retreat_prefers_threat_avoidance_over_taboo():
    """Even if the only non-taboo cell is threatened, the function still
    prefers it only when no safe+non-taboo option exists — but if all
    non-threat cells are taboo, it should pick the least-penalised taboo
    cell rather than WAIT forever."""
    ranger = unit(1, UnitType.RANGER, (-2598, 1176))
    # enemy at (-2598, 1177) makes DOWN a threatened cell
    enemy = unit(999, UnitType.RANGER, (-2598, 1177), controlled=False)
    # recent cells include RIGHT
    recent = [[-2597, 1176], [-2598, 1176], [-2597, 1176]]
    task_entry = {
        "kind": "distant_retreat_fallback",
        "target": [0, 0],
        "recent_cells": recent,
        "prev_cell": [-2597, 1176],
    }
    context = DecisionContext.from_turn(
        turn(
            tick=100,
            owned_core=core(),
            units=(ranger,),
            enemies=(enemy,),
        )
    )
    memory = AgentMemory()
    memory.unit_tasks = {str(ranger.id): task_entry}
    occupancy: dict[Position, int] = {ranger.position: 1}
    reservations = ReservationTable(occupancy=occupancy)
    target: Position = (0, 0)

    intent = _distant_retreat_fallback_intent(
        ranger, target, context, memory, reservations,
        "unit_retreat_to_core_heal",
    )
    assert intent is not None
    # Should not pick the threatened cell if a non-threat option exists
    # (UP or LEFT should be preferred over threatened DOWN).
    assert intent.reserved_cell != (-2598, 1177), (
        "Should avoid threatened cell when non-threat options exist."
    )


# ---------------------------------------------------------------------------
# 6. recent_cells is correctly accumulated and capped at 5.
# ---------------------------------------------------------------------------

def test_recent_cells_capped_at_five():
    ranger = unit(1, UnitType.RANGER, (-2598, 1176))
    # Start with 5 recent cells (the max)
    recent = [
        [-2598, 1176], [-2597, 1176], [-2598, 1177],
        [-2599, 1176], [-2598, 1175],
    ]
    task_entry = {
        "kind": "distant_retreat_fallback",
        "target": [0, 0],
        "recent_cells": recent,
        "prev_cell": [-2598, 1175],
    }
    context, memory, reservations = _make_context_and_memory(
        ranger, unit_tasks=[task_entry], tick=100,
    )
    target: Position = (0, 0)
    intent = _distant_retreat_fallback_intent(
        ranger, target, context, memory, reservations,
        "unit_retreat_to_core_heal",
    )
    assert intent is not None
    task = memory.unit_tasks[str(ranger.id)]
    stored = task["recent_cells"]
    # Should be capped at 5: old entries dropped
    assert len(stored) <= 5, f"recent_cells has {len(stored)} entries, expected <= 5"
    # The last entry should be the unit's current position before the move
    assert stored[-1] == list(ranger.position)


# ---------------------------------------------------------------------------
# 7. Memory round-trip: recent_cells survives _safe_task → from_dict.
# ---------------------------------------------------------------------------

def test_recent_cells_survives_memory_serialisation():
    from arena_tactic.memory import _safe_task

    task = {
        "kind": "distant_retreat_fallback",
        "target": [0, 0],
        "prev_cell": [-2598, 1176],
        "recent_cells": [[-2598, 1176], [-2597, 1176], [-2598, 1177]],
        "step": [-2597, 1176],
        "attempt_tick": 100,
    }
    safe = _safe_task(task)
    assert "recent_cells" in safe
    assert safe["recent_cells"] == [[-2598, 1176], [-2597, 1176], [-2598, 1177]]

    # Round-trip through AgentMemory.to_dict / from_dict
    memory = AgentMemory()
    unit_id = "entity_test12345678"
    memory.unit_tasks[unit_id] = task
    data = memory.to_dict()
    restored = AgentMemory.from_dict(data)
    restored_task = restored.unit_tasks.get(unit_id, {})
    assert "recent_cells" in restored_task, (
        "recent_cells should survive full memory round-trip"
    )


# ---------------------------------------------------------------------------
# 8. _safe_task rejects malformed recent_cells.
# ---------------------------------------------------------------------------

def test_safe_task_rejects_malformed_recent_cells():
    from arena_tactic.memory import _safe_task

    # Too many entries (>10)
    task = {"kind": "test", "recent_cells": [[0, 0]] * 11}
    safe = _safe_task(task)
    assert "recent_cells" not in safe

    # Non-integer coordinates
    task = {"kind": "test", "recent_cells": [["a", "b"]]}
    safe = _safe_task(task)
    assert "recent_cells" not in safe

    # Wrong length sub-items
    task = {"kind": "test", "recent_cells": [[1, 2, 3]]}
    safe = _safe_task(task)
    assert "recent_cells" not in safe

    # Not a list
    task = {"kind": "test", "recent_cells": "bad"}
    safe = _safe_task(task)
    assert "recent_cells" not in safe


# ---------------------------------------------------------------------------
# 9. Direct 2-cell oscillation (legacy failure mode) also covered.
# ---------------------------------------------------------------------------

def test_distant_retreat_breaks_2_cell_ping_pong():
    """Even a simple 2-cell back-and-forth should be broken."""
    ranger = unit(1, UnitType.RANGER, (5, 5))
    recent = [[6, 5], [5, 5]]  # bounced between (6,5) and (5,5)
    task_entry = {
        "kind": "distant_retreat_fallback",
        "target": [0, 0],
        "recent_cells": recent,
        "prev_cell": [6, 5],
    }
    context, memory, reservations = _make_context_and_memory(
        ranger, unit_tasks=[task_entry], tick=100,
    )
    target: Position = (0, 0)
    intent = _distant_retreat_fallback_intent(
        ranger, target, context, memory, reservations,
        "unit_retreat_to_core_heal",
    )
    assert intent is not None
    assert intent.reserved_cell != (6, 5), (
        "Should not return to the cell it just came from."
    )
