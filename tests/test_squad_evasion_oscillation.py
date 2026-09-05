"""Regression tests for squad evasion anti-oscillation in coordinate_expedition_intents.

Covers the scenario described in the UNIT_OSCILLATION / SQUAD_EXPEDITION_STALL
incident (Tick 227887): four expedition units (2aada0b86a43, 972040cb9dc0,
b7d5a0ddd165, 5aa67ac70b1c) oscillating between two adjacent cells 58 times in
60 samples because ``_squad_evasion_step`` had no anti-oscillation mechanism.
"""

from __future__ import annotations

from uuid import UUID

from arena_hero import Direction, UnitType

from arena_tactic.context import DecisionContext
from arena_tactic.memory import AgentMemory
from arena_tactic.models import ActionIntent, ActionKind, Position, ReservationTable
from arena_tactic.navigation import distance, destination
from arena_tactic.squad_coordination import (
    _load_recent_cells,
    _squad_evasion_step,
    coordinate_expedition_intents,
)
from arena_tactic.squads import Squad, SquadMember, SquadRole, SquadType

from .factories import core, turn, unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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


def _make_context_memory(
    unit_objs,
    *,
    obstacles: set[Position] | None = None,
    enemy_occupancy: tuple[Position, ...] = (),
    unit_tasks: dict | None = None,
    tick: int = 100,
    target: Position = (20, 20),
    obstacle_cells: tuple[Position, ...] = (),
):
    """Build DecisionContext and AgentMemory for squad coordination tests."""
    enemies = [
        unit(900 + i, UnitType.RANGER, pos, controlled=False)
        for i, pos in enumerate(enemy_occupancy)
    ]
    t = turn(
        tick=tick,
        owned_core=core(position=(0, 0)),
        units=tuple(unit_objs),
        enemies=enemies,
        obstacle_cells=obstacle_cells,
    )
    context = DecisionContext.from_turn(t)
    memory = AgentMemory(obstacles=obstacles or set())
    if unit_tasks:
        memory.unit_tasks = unit_tasks
    return context, memory


def _reservations_for(unit_objs) -> ReservationTable:
    occupancy: dict[Position, int] = {}
    for u in unit_objs:
        occupancy[u.position] = occupancy.get(u.position, 0) + 1
    return ReservationTable(occupancy=occupancy)


# ---------------------------------------------------------------------------
# 1. _squad_evasion_step avoids recently visited cells
# ---------------------------------------------------------------------------

def test_squad_evasion_step_avoids_recent_cell():
    """The evasion step must NOT return to the cell the unit just came from."""
    u = unit(1, UnitType.VANGUARD, (5, 5))
    target: Position = (10, 5)  # east — RIGHT is closest
    context, memory = _make_context_memory([u], target=target)
    # Record that the unit just came from (6, 5) — the RIGHT direction.
    memory.unit_tasks = {
        str(u.id): {
            "kind": "squad_evasion",
            "recent_cells": [[6, 5]],
        }
    }
    reservations = _reservations_for([u])
    direction = _squad_evasion_step(
        u, target, context, memory, reservations, avoid_threats=False,
    )
    assert direction is not None
    chosen = destination(u.position, direction)
    assert chosen != (6, 5), (
        f"Evasion chose {chosen} which was recently visited; "
        "anti-oscillation should have prevented this."
    )


# ---------------------------------------------------------------------------
# 2. Multi-tick simulation: evasion never revisits recent cells
# ---------------------------------------------------------------------------

def test_squad_evasion_multi_tick_no_oscillation():
    """Simulate 6 consecutive evasion ticks; unit must never revisit recent."""
    target: Position = (10, 5)
    recent: list[Position] = []
    current: Position = (5, 5)

    for tick in range(100, 106):
        u = unit(1, UnitType.VANGUARD, current)
        context, memory = _make_context_memory(
            [u], target=target, tick=tick,
        )
        memory.unit_tasks = {
            str(u.id): {
                "kind": "squad_evasion",
                "recent_cells": [list(c) for c in recent],
            }
        }
        reservations = _reservations_for([u])
        direction = _squad_evasion_step(
            u, target, context, memory, reservations, avoid_threats=False,
        )
        if direction is None:
            break
        chosen = destination(current, direction)
        assert chosen not in set(recent[-5:]), (
            f"Tick {tick}: evasion chose {chosen} which is in recent {recent[-5:]}"
        )
        recent.append(current)
        current = chosen


# ---------------------------------------------------------------------------
# 3. _load_recent_cells helper
# ---------------------------------------------------------------------------

def test_load_recent_cells_basic():
    m = AgentMemory()
    m.unit_tasks["test_unit"] = {"recent_cells": [[1, 2], [3, 4]]}
    cells = _load_recent_cells(m, "test_unit")
    assert cells == [(1, 2), (3, 4)]


def test_load_recent_cells_empty():
    m = AgentMemory()
    cells = _load_recent_cells(m, "nonexistent")
    assert cells == []


def test_load_recent_cells_rejects_malformed():
    m = AgentMemory()
    m.unit_tasks["bad"] = {"recent_cells": [["a", "b"], [1, 2, 3]]}
    cells = _load_recent_cells(m, "bad")
    assert cells == []


# ---------------------------------------------------------------------------
# 4. coordinate_expedition_intents records recent_cells on evasion
# ---------------------------------------------------------------------------

def test_coordinate_expedition_records_recent_cells():
    """When evasion is used, memory.unit_tasks should be updated with recent_cells."""
    u1 = unit(1, UnitType.VANGUARD, (5, 5))
    u2 = unit(2, UnitType.VANGUARD, (5, 6))
    squad = _make_squad([1, 2], target=(20, 5))
    # Block the direct path so plan_step fails and evasion is used.
    obstacles = {(6, 5), (6, 6)}
    context, memory = _make_context_memory(
        [u1, u2],
        obstacles=obstacles,
        target=(20, 5),
    )
    proposals = (
        ActionIntent(u1.id, False, ActionKind.MOVE, 680, "expedition_formation_move",
                     target_cell=(20, 5), direction=Direction.RIGHT, reserved_cell=(6, 5)),
        ActionIntent(u2.id, False, ActionKind.MOVE, 680, "expedition_formation_move",
                     target_cell=(20, 5), direction=Direction.RIGHT, reserved_cell=(6, 6)),
    )
    from arena_tactic.models import AgentConfig
    config = AgentConfig()
    result = coordinate_expedition_intents(
        context, memory, config, squad, proposals,
    )
    # Check that evasion intents were generated for units that could move.
    evasion_intents = [
        i for i in result
        if "evasion" in i.reason and i.action is ActionKind.MOVE
    ]
    if evasion_intents:
        # At least one unit used evasion; its recent_cells should be recorded.
        for intent in evasion_intents:
            task = memory.unit_tasks.get(str(intent.actor_id), {})
            assert "recent_cells" in task, (
                f"Unit {intent.actor_id} used evasion but recent_cells was not recorded."
            )
            assert task["kind"] == "squad_evasion"


# ---------------------------------------------------------------------------
# 5. _squad_evasion_step with all 4 neighbors recently visited returns None
# ---------------------------------------------------------------------------

def test_squad_evasion_step_returns_none_when_all_recently_visited():
    """When all neighbors are recently visited AND blocked, returns None."""
    u = unit(1, UnitType.VANGUARD, (5, 5))
    target: Position = (10, 5)
    # All 4 adjacent cells are recently visited
    recent = [[6, 5], [4, 5], (5, 6), (5, 4)]
    context, memory = _make_context_memory([u], target=target)
    memory.unit_tasks = {
        str(u.id): {"kind": "squad_evasion", "recent_cells": recent}
    }
    # Also block all 4 neighbors
    obstacles = {(6, 5), (4, 5), (5, 6), (5, 4)}
    memory.obstacles = obstacles
    reservations = _reservations_for([u])
    direction = _squad_evasion_step(
        u, target, context, memory, reservations, avoid_threats=False,
    )
    assert direction is None


# ---------------------------------------------------------------------------
# 6. Taboo penalty correctly decays with recency
# ---------------------------------------------------------------------------

def test_taboo_penalty_decays():
    """Most recent cell gets the heaviest penalty; older cells get less."""
    u = unit(1, UnitType.VANGUARD, (5, 5))
    target: Position = (10, 5)
    # Oldest → newest: (2,5) is oldest, (6,5) is newest
    recent = [[2, 5], (3, 5), (4, 5), (5, 4), (6, 5)]
    context, memory = _make_context_memory([u], target=target)
    memory.unit_tasks = {
        str(u.id): {"kind": "squad_evasion", "recent_cells": recent}
    }
    reservations = _reservations_for([u])
    direction = _squad_evasion_step(
        u, target, context, memory, reservations, avoid_threats=False,
    )
    assert direction is not None
    chosen = destination(u.position, direction)
    # Most recent cell (6, 5 = RIGHT) should be heavily penalized
    assert chosen != (6, 5), "Most recent cell should be avoided."
    # But older cells might still be chosen if no other option exists.
    # With 4 directions and 5 recent cells, at least one direction should
    # not be in the most recent 4.
    # UP = (5,4) is also in recent, DOWN = (5,6) is not in recent.
    assert chosen == (5, 6) or chosen not in set(tuple(c) for c in recent[-5:])


# ---------------------------------------------------------------------------
# 7. Evasion with threats still respects taboo
# ---------------------------------------------------------------------------

def test_squad_evasion_respects_taboo_with_threats():
    """Taboo penalties work even when avoid_threats=True."""
    u = unit(1, UnitType.VANGUARD, (5, 5))
    target: Position = (10, 5)
    # Enemy to the south creates a threat on (5, 6)
    enemy = unit(999, UnitType.RANGER, (5, 7), controlled=False)
    context = DecisionContext.from_turn(
        turn(tick=100, owned_core=core(), units=(u,), enemies=(enemy,))
    )
    memory = AgentMemory()
    # The unit recently came from RIGHT (6, 5) — the best direction toward target
    memory.unit_tasks = {
        str(u.id): {"kind": "squad_evasion", "recent_cells": [[6, 5]]}
    }
    reservations = _reservations_for([u])
    direction = _squad_evasion_step(
        u, target, context, memory, reservations, avoid_threats=True,
    )
    if direction is not None:
        chosen = destination(u.position, direction)
        # Should avoid both the recent cell and the threatened cell
        assert chosen != (6, 5), "Should avoid recently visited cell."
        # (5, 6) is a threat cell from the ranger at (5, 7), but with threats
        # blocked, it should not be chosen.
        assert chosen != (5, 6), "Should avoid threatened cell."


# ---------------------------------------------------------------------------
# 8. Integration: coordinate_expedition_intents breaks 2-cell ping-pong
# ---------------------------------------------------------------------------

def test_coordinate_expedition_breaks_ping_pong():
    """Full integration: simulate 5 ticks and verify no unit oscillates."""
    u1 = unit(1, UnitType.VANGUARD, (5, 5))
    u2 = unit(2, UnitType.VANGUARD, (5, 6))
    squad = _make_squad([1, 2], target=(20, 5))
    from arena_tactic.models import AgentConfig
    config = AgentConfig()
    positions: dict[int, list[Position]] = {1: [], 2: []}
    prev_unit_tasks: dict = {}

    for tick in range(100, 105):
        # Recreate units at their current positions
        u1 = unit(1, UnitType.VANGUARD, positions[1][-1] if positions[1] else (5, 5))
        u2 = unit(2, UnitType.VANGUARD, positions[2][-1] if positions[2] else (5, 6))
        # Obstacle blocks direct path east
        context, memory = _make_context_memory(
            [u1, u2], obstacles={(6, 5), (6, 6)}, target=(20, 5), tick=tick,
        )
        # Carry over unit_tasks from previous tick for position history
        if prev_unit_tasks:
            memory.unit_tasks = prev_unit_tasks
        proposals = (
            ActionIntent(u1.id, False, ActionKind.MOVE, 680,
                         "expedition_formation_move",
                         target_cell=(20, 5), direction=Direction.RIGHT,
                         reserved_cell=(6, 5)),
            ActionIntent(u2.id, False, ActionKind.MOVE, 680,
                         "expedition_formation_move",
                         target_cell=(20, 5), direction=Direction.RIGHT,
                         reserved_cell=(6, 6)),
        )
        result = coordinate_expedition_intents(
            context, memory, config, squad, proposals,
        )
        for intent in result:
            if intent.action is ActionKind.MOVE and intent.direction:
                new_pos = destination(
                    u1.position if intent.actor_id == u1.id else u2.position,
                    intent.direction,
                )
                uid = 1 if intent.actor_id == u1.id else 2
                positions[uid].append(new_pos)
        prev_unit_tasks = dict(memory.unit_tasks)

    # Check for oscillation: no unit should visit the same cell twice in a row
    for uid, pos_list in positions.items():
        for i in range(1, len(pos_list)):
            assert pos_list[i] != pos_list[i - 2] if i >= 2 else True, (
                f"Unit {uid} oscillated: {pos_list[i-2]} -> {pos_list[i-1]} -> {pos_list[i]}"
            )
