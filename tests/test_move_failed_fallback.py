"""Regression tests for MOVE_CONTESTED fallback via last_move_attempt.

Covers the scenario where a dynamically dispatched unit (e.g. expedition
vanguard, squad evasion) receives UNIT_MOVE_FAILED but has no persistent
task in ``unit_tasks`` — or its task lacks ``step``.  The fix records every
unit's last attempted move target in ``last_move_attempt`` so that the
failed destination is still cooled down in ``temporary_blocks``.

Bug: Arena Hero 远征先锋连续 115 Tick 遭遇 MOVE_CONTESTED 无法自愈死锁。
"""
from __future__ import annotations

from uuid import UUID

from arena_hero import Direction, UnitType

from arena_tactic import AgentConfig, AgentMemory, choose_actions
from arena_tactic.models import ActionKind

from .factories import core, event, turn, unit


def _unit_intent(result, actor_id):
    return next(intent for intent in result.intents if intent.actor_id == actor_id)


# ---------------------------------------------------------------------------
# 1. Unit NOT in unit_tasks gets MOVE_CONTESTED → temporary_blocks
# ---------------------------------------------------------------------------

def test_move_contested_without_unit_task_adds_to_temporary_blocks():
    """A unit with no entry in unit_tasks should still cool down the contested
    cell via the last_move_attempt fallback."""
    vanguard = unit(10, UnitType.VANGUARD, (5, 5))
    # Pre-populate last_move_attempt as if the previous tick recorded a move.
    memory = AgentMemory(
        last_tick=1,
        last_move_attempt={str(vanguard.id): (6, 5)},
    )
    result = choose_actions(
        turn(
            tick=2,
            owned_core=core(position=(0, 0)),
            units=(vanguard,),
            events=(
                event(
                    900,
                    "UNIT_MOVE_FAILED",
                    tick=1,
                    reason_code="MOVE_CONTESTED",
                    position=(5, 5),
                    actor_id=vanguard.id,
                ),
            ),
        ),
        memory=memory,
        config=AgentConfig(movement_failure_cooldown_ticks=4),
    )
    assert (6, 5) in result.next_memory.temporary_blocks
    assert result.next_memory.temporary_blocks[(6, 5)] == 6  # tick 2 + 4


def test_move_contested_with_empty_step_adds_to_temporary_blocks():
    """A unit whose task exists but has no ``step`` should still cool down."""
    vanguard = unit(11, UnitType.VANGUARD, (3, 3))
    memory = AgentMemory(
        last_tick=1,
        unit_tasks={str(vanguard.id): {"kind": "explore", "target": [10, 3]}},
        last_move_attempt={str(vanguard.id): (4, 3)},
    )
    result = choose_actions(
        turn(
            tick=2,
            owned_core=core(position=(0, 0)),
            units=(vanguard,),
            events=(
                event(
                    901,
                    "UNIT_MOVE_FAILED",
                    tick=1,
                    reason_code="MOVE_CONTESTED",
                    position=(3, 3),
                    actor_id=vanguard.id,
                ),
            ),
        ),
        memory=memory,
        config=AgentConfig(movement_failure_cooldown_ticks=6),
    )
    # Fallback should have kicked in.
    assert (4, 3) in result.next_memory.temporary_blocks
    assert result.next_memory.temporary_blocks[(4, 3)] == 8  # tick 2 + 6


# ---------------------------------------------------------------------------
# 2. MOVE_BLOCKED_TERRAIN fallback → obstacles (permanent)
# ---------------------------------------------------------------------------

def test_move_blocked_terrain_without_unit_task_adds_to_obstacles():
    """MOVE_BLOCKED_TERRAIN should promote the cell to permanent obstacles
    even when the unit has no task step."""
    vanguard = unit(12, UnitType.VANGUARD, (7, 7))
    memory = AgentMemory(
        last_tick=1,
        last_move_attempt={str(vanguard.id): (8, 7)},
    )
    result = choose_actions(
        turn(
            tick=2,
            owned_core=core(position=(0, 0)),
            units=(vanguard,),
            events=(
                event(
                    902,
                    "UNIT_MOVE_FAILED",
                    tick=1,
                    reason_code="MOVE_BLOCKED_TERRAIN",
                    position=(7, 7),
                    actor_id=vanguard.id,
                ),
            ),
        ),
        memory=memory,
    )
    assert (8, 7) in result.next_memory.obstacles


# ---------------------------------------------------------------------------
# 3. last_move_attempt is recorded from validated MOVE intents
# ---------------------------------------------------------------------------

def test_last_move_attempt_recorded_from_move_intent():
    """After a MOVE intent is validated, the target cell should be stored in
    last_move_attempt for the actor."""
    worker = unit(1, UnitType.WORKER, (0, 0))
    result = choose_actions(
        turn(tick=1, owned_core=core(position=(-10, 0)), units=(worker,)),
    )
    intent = _unit_intent(result, worker.id)
    assert intent.action is ActionKind.MOVE
    assert str(worker.id) in result.next_memory.last_move_attempt
    expected = intent.reserved_cell
    assert expected is not None
    assert result.next_memory.last_move_attempt[str(worker.id)] == expected


# ---------------------------------------------------------------------------
# 4. Pathfinding avoids temporarily blocked cells
# ---------------------------------------------------------------------------

def test_pathfinding_avoids_temporary_blocks_from_fallback():
    """After a MOVE_CONTESTED failure cools down a cell via last_move_attempt
    fallback, subsequent pathfinding should avoid that cell."""
    vanguard = unit(20, UnitType.VANGUARD, (0, 0))
    # The vanguard tried to move RIGHT to (1,0) last tick and it was contested.
    memory = AgentMemory(
        last_tick=1,
        last_move_attempt={str(vanguard.id): (1, 0)},
    )
    # Process the move failure → (1,0) goes into temporary_blocks.
    result1 = choose_actions(
        turn(
            tick=2,
            owned_core=core(position=(-10, 0)),
            units=(vanguard,),
            events=(
                event(
                    910,
                    "UNIT_MOVE_FAILED",
                    tick=1,
                    reason_code="MOVE_CONTESTED",
                    position=(0, 0),
                    actor_id=vanguard.id,
                ),
            ),
        ),
        memory=memory,
        config=AgentConfig(movement_failure_cooldown_ticks=4),
    )
    assert (1, 0) in result1.next_memory.temporary_blocks

    # Next tick: the vanguard should NOT try to move into (1,0).
    result2 = choose_actions(
        turn(
            tick=3,
            owned_core=core(position=(-10, 0)),
            units=(vanguard,),
        ),
        memory=result1.next_memory,
        config=AgentConfig(movement_failure_cooldown_ticks=4),
    )
    intent = _unit_intent(result2, vanguard.id)
    if intent.action is ActionKind.MOVE:
        from arena_tactic.navigation import destination
        dest = destination(vanguard.position, intent.direction)
        assert dest != (1, 0), "Unit should avoid temporarily blocked cell"


# ---------------------------------------------------------------------------
# 5. last_move_attempt is persisted and loaded correctly
# ---------------------------------------------------------------------------

def test_last_move_attempt_roundtrip_serialization():
    """last_move_attempt survives to_dict/from_dict roundtrip."""
    m = AgentMemory(last_tick=5)
    m.last_move_attempt = {"entity_abc123": (42, 99)}
    d = m.to_dict()
    loaded = AgentMemory.from_dict(d)
    assert loaded.last_move_attempt == {"entity_abc123": (42, 99)}


# ---------------------------------------------------------------------------
# 6. last_move_attempt is cleared on core respawn
# ---------------------------------------------------------------------------

def test_last_move_attempt_cleared_on_core_respawn():
    """When the core respawns, last_move_attempt should be cleared."""
    from arena_tactic.context import DecisionContext
    vanguard = unit(30, UnitType.VANGUARD, (5, 5))
    old_core = core(value=100, position=(0, 0))
    new_core = core(value=200, position=(0, 0))  # new core id
    memory = AgentMemory(
        last_tick=1,
        last_core_id=str(old_core.id),
        last_core_position=(0, 0),
        last_move_attempt={str(vanguard.id): (6, 5)},
    )
    ctx = DecisionContext.from_turn(
        turn(
            tick=2,
            owned_core=new_core,
            units=(vanguard,),
            events=(event(999, "CORE_RESPAWNED", tick=2),),
        )
    )
    advanced = memory.advance(ctx, AgentConfig())
    assert advanced.last_move_attempt == {}


# ---------------------------------------------------------------------------
# 7. last_move_attempt pruned for absent units
# ---------------------------------------------------------------------------

def test_last_move_attempt_pruned_for_absent_units():
    """Units no longer on the field should be removed from last_move_attempt."""
    from arena_tactic.context import DecisionContext
    present = unit(31, UnitType.VANGUARD, (2, 2))
    absent_id = "entity_deadbeef"
    memory = AgentMemory(
        last_tick=1,
        last_move_attempt={str(present.id): (3, 2), absent_id: (9, 9)},
    )
    ctx = DecisionContext.from_turn(
        turn(tick=2, owned_core=core(), units=(present,))
    )
    advanced = memory.advance(ctx, AgentConfig())
    assert str(present.id) in advanced.last_move_attempt
    assert absent_id not in advanced.last_move_attempt
