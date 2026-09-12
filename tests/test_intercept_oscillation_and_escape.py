"""Regression tests for expedition vanguard vision-edge intercept oscillation
and Command API MOVE_TO_CELL safety-preempt bypass.

Covers two defects found in Tick 264165~264308 patrol:

1. UNIT_OSCILLATION: expedition vanguard entity_cb39bf380f0b oscillating between
   [-1088, -638] and [-1087, -638] for 120+ ticks.  The vanguard would see an
   enemy worker at vision edge, move one step toward it, lose sight, fall back
   to expedition regroup, move back, see the enemy again — infinite 2-cell
   ping-pong.

2. Command API escape blocked: when a vanguard at HP>1 tried to execute a
   manual MOVE_TO_CELL command to escape an enemy-adjacent position, the
   _manual_safety_preempts check (distance <= 2 from any enemy) unconditionally
   blocked the command, making operator escape intervention impossible.
"""

from __future__ import annotations

from uuid import UUID

from arena_hero import BeaconStatus, UnitType

from arena_tactic import AgentRuntime
from arena_tactic.identity import entity_alias
from arena_tactic.models import ActionKind, AgentConfig, StrategicMode
from arena_tactic.navigation import distance
from arena_tactic.memory import AgentMemory

from .factories import core, turn, unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _uuid(value: int) -> UUID:
    return UUID(int=value)


def _expedition_config(**overrides) -> AgentConfig:
    defaults = dict(
        core_guard_vanguards=0,
        core_guard_rangers=0,
        expedition_vanguards=4,
        expedition_rangers=4,
        mining_escort_vanguards=0,
        mining_escort_rangers=0,
        scout_vanguards=0,
        scout_rangers=0,
        intercept_distance=8,
        intercept_pursuit_grace_ticks=4,
    )
    defaults.update(overrides)
    return AgentConfig(**defaults)


def _beacon_memory(**overrides):
    defaults = dict(last_mode=StrategicMode.BEACON)
    defaults.update(overrides)
    return AgentMemory(**defaults)


def _alias(uid: int) -> str:
    """Compute the entity_alias for a UUID(int=uid)."""
    return entity_alias(_uuid(uid))


# ---------------------------------------------------------------------------
# 1. Intercept pursuit grace prevents vision-edge oscillation
# ---------------------------------------------------------------------------

def test_expedition_vanguard_pursues_enemy_through_vision_gap():
    """When an enemy goes out of vision, the vanguard must continue pursuing
    for intercept_pursuit_grace_ticks instead of falling back to beacon regroup.
    """
    core_cell = (-898, 1573)
    beacon_cell = (-1134, -1111)
    vanguard_cell = (-1088, -638)
    enemy_cell = (-1086, -638)

    # Tick 1: enemy is visible — vanguard intercepts
    t1 = turn(
        owned_core=core(position=core_cell),
        units=[unit(1, UnitType.VANGUARD, vanguard_cell)],
        enemies=[unit(2, UnitType.WORKER, enemy_cell, hp=2, controlled=False)],
        beacon_position=beacon_cell,
        beacon_status=BeaconStatus.GROUND,
        tick=264165,
    )
    config = _expedition_config()
    memory = _beacon_memory()
    runtime = AgentRuntime(config=config, memory=memory)
    result1 = runtime.decide(t1)
    intent1 = next(i for i in result1.intents if i.actor_id == t1.units[0].id)
    # Should intercept the visible enemy
    assert intent1.reason == "intercept_visible_threat"
    assert intent1.action is ActionKind.MOVE

    # Tick 2: enemy disappears from vision — vanguard should still pursue
    t2 = turn(
        owned_core=core(position=core_cell),
        units=[unit(1, UnitType.VANGUARD, (-1087, -638))],  # moved 1 step east
        enemies=[],  # enemy gone from vision
        beacon_position=beacon_cell,
        beacon_status=BeaconStatus.GROUND,
        tick=264166,
    )
    next_memory = result1.next_memory
    runtime2 = AgentRuntime(config=config, memory=next_memory)
    result2 = runtime2.decide(t2)
    intent2 = next(i for i in result2.intents if i.actor_id == t1.units[0].id)
    # Should use intercept_pursuit_grace, NOT fall back to expedition_vanguard_to_beacon
    assert intent2.reason in ("intercept_pursuit_grace", "intercept_pursuit_grace_blocked"), (
        f"Expected intercept_pursuit_grace but got {intent2.reason} — "
        "vanguard fell back to beacon regroup instead of continuing pursuit."
    )


def test_intercept_pursuit_grace_expires_after_ticks():
    """After intercept_pursuit_grace_ticks elapses, the vanguard should fall
    back to normal behavior (beacon regroup)."""
    core_cell = (-898, 1573)
    beacon_cell = (-1141, -308)
    vanguard_cell = (-1088, -638)

    alias = _alias(1)
    memory = AgentMemory(
        last_mode=StrategicMode.BEACON,
        unit_tasks={
            alias: {
                "kind": "intercept",
                "target": [-1086, -638],
                "intercept_since": 264160,
                "prev_cell": [-1088, -638],
            }
        },
    )
    config = _expedition_config(intercept_pursuit_grace_ticks=4)
    # Tick is 264165 — 5 ticks elapsed, exceeds grace of 4
    t = turn(
        owned_core=core(position=core_cell),
        units=[unit(1, UnitType.VANGUARD, vanguard_cell)],
        enemies=[],  # no visible enemy
        beacon_position=beacon_cell,
        beacon_status=BeaconStatus.GROUND,
        tick=264165,
    )
    runtime = AgentRuntime(config=config, memory=memory)
    result = runtime.decide(t)
    intent = next(i for i in result.intents if i.actor_id == t.units[0].id)
    # Grace expired — should NOT continue pursuit; should fall through to beacon regroup
    assert intent.reason != "intercept_pursuit_grace", (
        "Grace period should have expired but pursuit continued."
    )


def test_intercept_pursuit_grace_within_window():
    """Within the grace window, the vanguard should continue pursuit."""
    core_cell = (-898, 1573)
    beacon_cell = (-1141, -308)
    vanguard_cell = (-1088, -638)

    alias = _alias(1)
    memory = AgentMemory(
        last_mode=StrategicMode.BEACON,
        unit_tasks={
            alias: {
                "kind": "intercept",
                "target": [-1086, -638],
                "intercept_since": 264163,
                "prev_cell": [-1088, -638],
            }
        },
    )
    config = _expedition_config(intercept_pursuit_grace_ticks=4)
    # Tick is 264165 — 2 ticks elapsed, within grace
    t = turn(
        owned_core=core(position=core_cell),
        units=[unit(1, UnitType.VANGUARD, vanguard_cell)],
        enemies=[],  # no visible enemy
        beacon_position=beacon_cell,
        beacon_status=BeaconStatus.GROUND,
        tick=264165,
    )
    runtime = AgentRuntime(config=config, memory=memory)
    result = runtime.decide(t)
    intent = next(i for i in result.intents if i.actor_id == t.units[0].id)
    # Within grace — should continue pursuit
    assert intent.reason in ("intercept_pursuit_grace", "intercept_pursuit_grace_blocked"), (
        f"Expected pursuit within grace window but got {intent.reason}"
    )


def test_intercept_pursuit_grace_preserves_intercept_since():
    """Pursuit grace must preserve intercept_since to maintain continuity across ticks."""
    core_cell = (-898, 1573)
    beacon_cell = (-1141, -308)
    vanguard_cell = (-1087, -638)

    alias = _alias(1)
    memory = AgentMemory(
        last_mode=StrategicMode.BEACON,
        unit_tasks={
            alias: {
                "kind": "intercept",
                "target": [-1086, -638],
                "intercept_since": 264163,
                "prev_cell": [-1088, -638],
            }
        },
    )
    config = _expedition_config(intercept_pursuit_grace_ticks=4)
    t = turn(
        owned_core=core(position=core_cell),
        units=[unit(1, UnitType.VANGUARD, vanguard_cell)],
        enemies=[],
        beacon_position=beacon_cell,
        beacon_status=BeaconStatus.GROUND,
        tick=264165,
    )
    runtime = AgentRuntime(config=config, memory=memory)
    result = runtime.decide(t)
    # Verify intercept_since is preserved in the new memory
    task = result.next_memory.unit_tasks.get(alias, {})
    assert task.get("kind") == "intercept"
    assert task.get("intercept_since") == 264163, (
        "intercept_since should be preserved across grace ticks, "
        f"but got {task.get('intercept_since')}"
    )


def test_non_expedition_vanguard_no_grace_pursuit():
    """Non-expedition vanguards should NOT use the intercept pursuit grace —
    only expedition vanguards benefit from it."""
    core_cell = (0, 0)
    # Use core_guard_vanguards=1 so vanguard is a guard, not expedition
    config = _expedition_config(
        core_guard_vanguards=1,
        expedition_vanguards=0,
    )
    alias = _alias(1)
    memory = AgentMemory(
        last_mode=StrategicMode.BEACON,
        unit_tasks={
            alias: {
                "kind": "intercept",
                "target": [10, 0],
                "intercept_since": 100,
                "prev_cell": [8, 0],
            }
        },
    )
    t = turn(
        owned_core=core(position=core_cell),
        units=[unit(1, UnitType.VANGUARD, (8, 0))],
        enemies=[],  # no visible enemy
        beacon_position=(20, 20),
        beacon_status=BeaconStatus.GROUND,
        tick=103,
    )
    runtime = AgentRuntime(config=config, memory=memory)
    result = runtime.decide(t)
    intent = next(i for i in result.intents if i.actor_id == t.units[0].id)
    # Should NOT use intercept_pursuit_grace for non-expedition vanguard
    assert intent.reason != "intercept_pursuit_grace", (
        "Non-expedition vanguard should not use intercept pursuit grace."
    )


# ---------------------------------------------------------------------------
# 2. Command API MOVE_TO_CELL bypasses enemy proximity preempt
# ---------------------------------------------------------------------------

def test_manual_move_to_cell_allowed_near_enemy():
    """A manual MOVE_TO_CELL command should NOT be preempted when enemies
    are within 2 cells (but HP > 1), allowing the operator to order escapes."""
    from arena_tactic.context import DecisionContext

    core_cell = (0, 0)
    vanguard = unit(1, UnitType.VANGUARD, (5, 5), hp=3)
    # Enemy at distance 1 — would normally preempt
    enemy = unit(900, UnitType.VANGUARD, (5, 6), controlled=False)

    t = turn(
        owned_core=core(position=core_cell),
        units=[vanguard],
        enemies=[enemy],
    )
    context = DecisionContext.from_turn(t)
    runtime = AgentRuntime(config=AgentConfig())

    task = {"kind": "MOVE_TO_CELL", "priority": 900, "target": [5, 4]}
    # Should NOT preempt — MOVE_TO_CELL is an escape command
    assert runtime._manual_safety_preempts(context, vanguard, task) is False


def test_manual_non_move_to_cell_still_preempted_near_enemy():
    """Non-MOVE_TO_CELL manual commands (e.g. HOLD_POSITION) should still be
    preempted when enemies are within 2 cells."""
    from arena_tactic.context import DecisionContext

    core_cell = (0, 0)
    vanguard = unit(1, UnitType.VANGUARD, (5, 5), hp=3)
    enemy = unit(900, UnitType.VANGUARD, (5, 6), controlled=False)

    t = turn(
        owned_core=core(position=core_cell),
        units=[vanguard],
        enemies=[enemy],
    )
    context = DecisionContext.from_turn(t)
    runtime = AgentRuntime(config=AgentConfig())

    task = {"kind": "HOLD_POSITION", "priority": 800}
    # Should preempt — non-escape command near enemy
    assert runtime._manual_safety_preempts(context, vanguard, task) is True


def test_manual_move_to_cell_still_preempted_at_hp_1():
    """Even MOVE_TO_CELL should be preempted when HP <= 1 (unit must heal)."""
    from arena_tactic.context import DecisionContext

    core_cell = (0, 0)
    vanguard = unit(1, UnitType.VANGUARD, (5, 5), hp=1)
    enemy = unit(900, UnitType.VANGUARD, (5, 6), controlled=False)

    t = turn(
        owned_core=core(position=core_cell),
        units=[vanguard],
        enemies=[enemy],
    )
    context = DecisionContext.from_turn(t)
    runtime = AgentRuntime(config=AgentConfig())

    task = {"kind": "MOVE_TO_CELL", "priority": 900, "target": [5, 4]}
    # Should preempt — HP <= 1 unit must heal, not move
    assert runtime._manual_safety_preempts(context, vanguard, task) is True


def test_manual_move_to_cell_no_enemy_no_preempt():
    """MOVE_TO_CELL with no enemies nearby should never preempt."""
    from arena_tactic.context import DecisionContext

    core_cell = (0, 0)
    vanguard = unit(1, UnitType.VANGUARD, (5, 5), hp=3)

    t = turn(
        owned_core=core(position=core_cell),
        units=[vanguard],
        enemies=[],
    )
    context = DecisionContext.from_turn(t)
    runtime = AgentRuntime(config=AgentConfig())

    task = {"kind": "MOVE_TO_CELL", "priority": 900, "target": [5, 4]}
    assert runtime._manual_safety_preempts(context, vanguard, task) is False


def test_manual_safety_preempts_backward_compatible_no_task():
    """Calling _manual_safety_preempts without task (legacy callers) must still
    preempt near enemies."""
    from arena_tactic.context import DecisionContext

    core_cell = (0, 0)
    vanguard = unit(1, UnitType.VANGUARD, (5, 5), hp=3)
    enemy = unit(900, UnitType.VANGUARD, (5, 6), controlled=False)

    t = turn(
        owned_core=core(position=core_cell),
        units=[vanguard],
        enemies=[enemy],
    )
    context = DecisionContext.from_turn(t)
    runtime = AgentRuntime(config=AgentConfig())

    # Legacy call without task parameter — should still preempt near enemies
    assert runtime._manual_safety_preempts(context, vanguard) is True


# ---------------------------------------------------------------------------
# 3. Integration: MOVE_TO_CELL actually executes near enemies
# ---------------------------------------------------------------------------

def test_move_to_cell_intent_generated_near_enemy():
    """Full integration: a MOVE_TO_CELL manual assignment produces a MOVE intent
    even when enemies are within 2 cells."""
    from arena_tactic.command_center import CommandQueue

    core_cell = (0, 0)
    vanguard = unit(1, UnitType.VANGUARD, (5, 5), hp=3)
    # Enemy adjacent — distance 1
    enemy = unit(900, UnitType.VANGUARD, (6, 5), controlled=False, hp=4)

    t = turn(
        owned_core=core(position=core_cell),
        units=[vanguard],
        enemies=[enemy],
        tick=100,
    )

    queue = CommandQueue()
    alias = entity_alias(vanguard.id)
    command, _, _ = queue.enqueue(
        {"type": "ASSIGN_TASK", "payload": {
            "entity_alias": alias,
            "task_kind": "MOVE_TO_CELL",
            "priority": 900,
            "target": [5, 4],
        }},
        issuer="test",
        current_tick=99,
        idempotency_key="escape-test-1",
        expected_version=0,
    )

    runtime = AgentRuntime(config=AgentConfig(), command_queue=queue)
    result = runtime.decide(t)

    # The vanguard should have a manual_task_move intent, NOT be preempted
    vg_intents = [i for i in result.intents if i.actor_id == vanguard.id]
    assert len(vg_intents) == 1
    intent = vg_intents[0]
    assert intent.reason == "manual_task_move", (
        f"Expected manual_task_move but got {intent.reason} — "
        "MOVE_TO_CELL was preempted by enemy proximity."
    )
    assert intent.action is ActionKind.MOVE
