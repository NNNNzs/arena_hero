"""Regression test for UNIT_OSCILLATION: recent_cells wiped by _plan_vanguards.

Bug: In ``_plan_vanguards`` (vanguards.py ~line 323), the line::

    memory.unit_tasks[str(vanguard.id)] = {
        "kind": "expedition_beacon",
        "target": list(context.beacon.position),
    }

unconditionally replaced the task dict each tick, destroying the
``recent_cells`` history that ``coordinate_expedition_intents`` uses for
anti-oscillation taboo penalties.  This caused expedition vanguards to
ping-pong between 2-3 cells when ``_squad_evasion_step`` was active.

Fix: preserve ``recent_cells`` when overwriting the task dict in
``_plan_vanguards``, and also in ``_record_unit_task`` when kind changes.
"""

from __future__ import annotations

from uuid import UUID

from arena_hero import BeaconStatus, Direction, UnitType

from arena_tactic.context import DecisionContext
from arena_tactic.memory import AgentMemory
from arena_tactic.models import ActionIntent, ActionKind, AgentConfig, Position, ReservationTable
from arena_tactic.navigation import destination
from arena_tactic.squad_coordination import (
    _load_recent_cells,
    coordinate_expedition_intents,
)
from arena_tactic.strategy.common import _record_unit_task
from arena_tactic.strategy.vanguards import _plan_vanguards
from arena_tactic.squads import Squad, SquadMember, SquadRole, SquadPlan, SquadType

from .factories import core, turn, unit


def _uuid(value: int) -> UUID:
    return UUID(int=value)


def _make_squad(
    unit_ids: list[int],
    target: Position = (20, 20),
) -> Squad:
    return Squad(
        squad_id="squad_expedition_beacon",
        squad_type=SquadType.EXPEDITION_BEACON,
        target=target,
        members=tuple(
            SquadMember(_uuid(uid), UnitType.VANGUARD, SquadRole.POINT_GUARD)
            for uid in unit_ids
        ),
    )


def _make_squad_plan(unit_ids: list[int], target: Position = (20, 20)) -> SquadPlan:
    plan = SquadPlan()
    plan.add_squad(_make_squad(unit_ids, target))
    return plan


# ---------------------------------------------------------------------------
# 1. Direct unit_tasks preservation: _plan_vanguards keeps recent_cells
# ---------------------------------------------------------------------------

def test_plan_vanguards_preserves_recent_cells():
    """_plan_vanguards must not destroy recent_cells when setting the task."""
    u = unit(1, UnitType.VANGUARD, (5, 5))
    beacon_pos: Position = (20, 20)
    t = turn(
        tick=200,
        owned_core=core(position=(0, 0)),
        units=(u,),
        beacon_position=beacon_pos,
        beacon_status=BeaconStatus.GROUND,
    )
    context = DecisionContext.from_turn(t)
    memory = AgentMemory()
    # Pre-populate recent_cells as if coordinate_expedition_intents had been
    # recording position history over several ticks.
    memory.unit_tasks[str(u.id)] = {
        "kind": "squad_evasion",
        "recent_cells": [[4, 5], [3, 5], [5, 4]],
    }
    reservations = ReservationTable(occupancy={})
    config = AgentConfig()
    squad_plan = _make_squad_plan([1], target=beacon_pos)

    _plan_vanguards(context, memory, reservations, deadline=999.0, config=config,
                    heal_allowances={}, squad_plan=squad_plan)

    task = memory.unit_tasks.get(str(u.id), {})
    recent = task.get("recent_cells")
    assert recent is not None, (
        "recent_cells was destroyed by _plan_vanguards; UNIT_OSCILLATION root cause."
    )
    assert len(recent) == 3, (
        f"Expected 3 recent_cells preserved, got {len(recent)}"
    )


# ---------------------------------------------------------------------------
# 2. _record_unit_task preserves recent_cells across kind changes
# ---------------------------------------------------------------------------

def test_record_unit_task_preserves_recent_cells_on_kind_change():
    """_record_unit_task must carry recent_cells when kind transitions."""
    u = unit(1, UnitType.VANGUARD, (5, 5))
    t = turn(tick=100, owned_core=core(), units=(u,))
    context = DecisionContext.from_turn(t)
    memory = AgentMemory()
    memory.unit_tasks[str(u.id)] = {
        "kind": "squad_evasion",
        "recent_cells": [[4, 5], [6, 5]],
    }

    _record_unit_task(memory, context, u, kind="expedition_beacon",
                      target=(20, 20), intent=None)

    task = memory.unit_tasks[str(u.id)]
    assert task["kind"] == "expedition_beacon"
    assert task.get("recent_cells") == [[4, 5], [6, 5]], (
        "_record_unit_task dropped recent_cells on kind change."
    )


# ---------------------------------------------------------------------------
# 3. _record_unit_task preserves recent_cells when kind is the same
# ---------------------------------------------------------------------------

def test_record_unit_task_preserves_recent_cells_same_kind():
    """_record_unit_task keeps recent_cells when kind stays the same."""
    u = unit(1, UnitType.VANGUARD, (5, 5))
    t = turn(tick=100, owned_core=core(), units=(u,))
    context = DecisionContext.from_turn(t)
    memory = AgentMemory()
    memory.unit_tasks[str(u.id)] = {
        "kind": "expedition_beacon",
        "target": [20, 20],
        "recent_cells": [[4, 5], [6, 5]],
    }

    _record_unit_task(memory, context, u, kind="expedition_beacon",
                      target=(21, 21), intent=None)

    task = memory.unit_tasks[str(u.id)]
    assert task["kind"] == "expedition_beacon"
    assert task.get("recent_cells") == [[4, 5], [6, 5]], (
        "_record_unit_task dropped recent_cells on same kind update."
    )


# ---------------------------------------------------------------------------
# 4. Multi-tick integration: _plan_vanguards + coordinate_expedition no oscillation
# ---------------------------------------------------------------------------

def test_vanguard_expedition_no_oscillation_across_ticks():
    """Simulate 6 ticks of _plan_vanguards → coordinate_expedition_intents.

    The vanguard starts east of a wall blocking the direct path to the beacon.
    ``_squad_evasion_step`` must drive the unit around the obstacle without
    bouncing back and forth, which requires recent_cells to survive the
    ``_plan_vanguards`` overwrite each tick.
    """
    uid = 1
    beacon_pos: Position = (20, 5)
    positions: list[Position] = [(5, 5)]
    prev_unit_tasks: dict = {}
    config = AgentConfig()

    for tick in range(200, 206):
        current = positions[-1]
        u = unit(uid, UnitType.VANGUARD, current)
        # Wall blocks the direct east path at column 6
        obstacle_cells = ((6, 5), (6, 6))
        t = turn(
            tick=tick,
            owned_core=core(position=(0, 0)),
            units=(u,),
            beacon_position=beacon_pos,
            beacon_status=BeaconStatus.GROUND,
            obstacle_cells=obstacle_cells,
        )
        context = DecisionContext.from_turn(t)
        memory = AgentMemory()
        # Carry over unit_tasks from previous tick (simulates real persistence)
        if prev_unit_tasks:
            memory.unit_tasks = prev_unit_tasks
        memory.obstacles = set(obstacle_cells)

        reservations = ReservationTable(occupancy={current: 1})
        squad_plan = _make_squad_plan([uid], target=beacon_pos)

        # Step 1: _plan_vanguards generates the intent and overwrites unit_tasks
        intents = _plan_vanguards(
            context, memory, reservations, deadline=999.0, config=config,
            heal_allowances={}, squad_plan=squad_plan,
        )

        # Step 2: coordinate_expedition_intents applies squad evasion
        squad = _make_squad([uid], target=beacon_pos)
        result = coordinate_expedition_intents(
            context, memory, config, squad, tuple(intents),
        )

        # Track position
        for intent in result:
            if intent.action is ActionKind.MOVE and intent.direction:
                new_pos = destination(current, intent.direction)
                positions.append(new_pos)

        # Carry over for next tick
        prev_unit_tasks = dict(memory.unit_tasks)

    # Assert no 2-cell ping-pong: for any i >= 2, pos[i] != pos[i-2]
    for i in range(2, len(positions)):
        assert positions[i] != positions[i - 2], (
            f"OSCILLATION at step {i}: {positions[i-2]} → {positions[i-1]} → {positions[i]}. "
            f"Full path: {positions}"
        )


# ---------------------------------------------------------------------------
# 5. recent_cells accumulates across ticks in the full pipeline
# ---------------------------------------------------------------------------

def test_recent_cells_accumulates_across_ticks():
    """Verify that recent_cells is preserved across ticks when _plan_vanguards runs."""
    uid = 1
    beacon_pos: Position = (20, 5)
    config = AgentConfig()

    u = unit(uid, UnitType.VANGUARD, (5, 5))
    t = turn(
        tick=200,
        owned_core=core(position=(0, 0)),
        units=(u,),
        beacon_position=beacon_pos,
        beacon_status=BeaconStatus.GROUND,
    )
    context = DecisionContext.from_turn(t)
    memory = AgentMemory()
    # Pre-populate recent_cells as if past evasion occurred
    memory.unit_tasks[str(u.id)] = {
        "kind": "squad_evasion",
        "target": list(beacon_pos),
        "recent_cells": [[5, 4], [5, 3]],
    }

    reservations = ReservationTable(occupancy={(5, 5): 1})
    squad_plan = _make_squad_plan([uid], target=beacon_pos)
    _plan_vanguards(
        context, memory, reservations, deadline=999.0, config=config,
        heal_allowances={}, squad_plan=squad_plan,
    )

    task = memory.unit_tasks.get(str(u.id), {})
    recent = task.get("recent_cells", [])
    assert recent == [[5, 4], [5, 3]], (
        f"Expected recent_cells to be preserved by _plan_vanguards, got: {recent}"
    )

