"""Guard: long-distance guard units must not deadlock on guard_route_blocked.

Regression for Tick 226521 where ~10 LEGACY_CORE_GUARD vanguards and rangers
at coordinates like [-2800, 700] (2000+ cells from core at [-898, 1573]) had
their guard_target set to a slot near the core.  Direct A* pathfinding across
such a vast distance with fog and obstacles returns None, and
_evacuate_doorstep_intent also returns None, causing every tick to fall
through to _wait("guard_route_blocked") — a permanent deadlock with
decision latency spikes up to 1220ms.

Fix: when guard route is blocked and the unit is far from its guard slot
(distance > long_distance_retreat_threshold), use
_distant_retreat_fallback_intent for incremental greedy progress toward the
target.
"""
from __future__ import annotations

from arena_hero import BeaconStatus, UnitType

from arena_tactic import AgentRuntime, choose_actions
from arena_tactic.models import ActionKind, AgentConfig, StrategicMode
from arena_tactic.navigation import distance

from .factories import core, turn, unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _guard_config(**overrides) -> AgentConfig:
    """Config that assigns exactly 1 vanguard guard + 1 ranger guard."""
    defaults = dict(
        core_guard_vanguards=1,
        core_guard_rangers=1,
        expedition_vanguards=0,
        expedition_rangers=0,
        mining_escort_vanguards=0,
        mining_escort_rangers=0,
        scout_vanguards=0,
        scout_rangers=0,
    )
    defaults.update(overrides)
    return AgentConfig(**defaults)


def _beacon_memory(**overrides):
    from arena_tactic.memory import AgentMemory
    defaults = dict(last_mode=StrategicMode.BEACON)
    defaults.update(overrides)
    return AgentMemory(**defaults)


# ---------------------------------------------------------------------------
# Test: far-from-core guard vanguard does not deadlock with guard_route_blocked
# ---------------------------------------------------------------------------

def test_far_guard_vanguard_uses_distant_fallback_instead_of_deadlock():
    """A guard vanguard 2000+ cells from core must use distant_fallback
    to make incremental progress, not permanently WAIT."""
    core_obj = core(position=(-898, 1573))
    # Place vanguard very far from core — simulating the real scenario
    far_guard = unit(1, UnitType.VANGUARD, (-2800, 700))
    config = _guard_config()
    memory = _beacon_memory()
    game_turn = turn(
        owned_core=core_obj,
        units=(far_guard,),
        beacon_position=(-1141, -308),
        beacon_status=BeaconStatus.GROUND,
    )

    result = AgentRuntime(memory=memory, config=config).decide(game_turn)
    intent = next(item for item in result.intents if item.actor_id == far_guard.id)

    # Must NOT be a permanent guard_route_blocked WAIT
    assert intent.reason != "guard_route_blocked", (
        f"Far guard vanguard deadlocked on guard_route_blocked: {intent}"
    )
    # Should be a MOVE action (distant fallback or regular)
    assert intent.action is ActionKind.MOVE, (
        f"Far guard vanguard should MOVE, got {intent.action} ({intent.reason})"
    )
    # Reason should indicate the fallback kicked in
    assert "hold_core_defense_ring" in intent.reason, (
        f"Unexpected reason for far guard: {intent.reason}"
    )
    # The move should make progress toward the guard target (closer to core)
    if intent.reserved_cell:
        new_dist = distance(intent.reserved_cell, core_obj.position)
        old_dist = distance(far_guard.position, core_obj.position)
        assert new_dist <= old_dist, (
            f"Fallback move should not increase distance to core: "
            f"{old_dist} -> {new_dist}"
        )


# ---------------------------------------------------------------------------
# Test: far-from-core guard ranger does not deadlock
# ---------------------------------------------------------------------------

def test_far_guard_ranger_uses_distant_fallback_instead_of_deadlock():
    """A guard ranger 2000+ cells from core must use distant_fallback
    to make incremental progress, not permanently WAIT."""
    core_obj = core(position=(-898, 1573))
    far_ranger = unit(5, UnitType.RANGER, (-2800, 700))
    config = _guard_config()
    memory = _beacon_memory()
    game_turn = turn(
        owned_core=core_obj,
        units=(far_ranger,),
        beacon_position=(-1141, -308),
        beacon_status=BeaconStatus.GROUND,
    )

    result = AgentRuntime(memory=memory, config=config).decide(game_turn)
    intent = next(item for item in result.intents if item.actor_id == far_ranger.id)

    assert intent.reason != "guard_route_blocked", (
        f"Far guard ranger deadlocked on guard_route_blocked: {intent}"
    )
    assert intent.action is ActionKind.MOVE, (
        f"Far guard ranger should MOVE, got {intent.action} ({intent.reason})"
    )
    assert "ranger_hold_defense_ring" in intent.reason, (
        f"Unexpected reason for far ranger: {intent.reason}"
    )
    if intent.reserved_cell:
        new_dist = distance(intent.reserved_cell, core_obj.position)
        old_dist = distance(far_ranger.position, core_obj.position)
        assert new_dist <= old_dist, (
            f"Fallback move should not increase distance: {old_dist} -> {new_dist}"
        )


# ---------------------------------------------------------------------------
# Test: guard unit at moderate distance (>50 but A* may still work) uses fallback
# ---------------------------------------------------------------------------

def test_guard_vanguard_at_moderate_distance_attempts_fallback():
    """A guard vanguard at distance > long_distance_retreat_threshold (50)
    where A* fails should trigger the distant fallback."""
    core_obj = core(position=(0, 0))
    # Place vanguard at distance 60 — beyond threshold
    far_guard = unit(1, UnitType.VANGUARD, (60, 0))
    config = _guard_config()
    memory = _beacon_memory()
    game_turn = turn(
        owned_core=core_obj,
        units=(far_guard,),
    )

    result = AgentRuntime(memory=memory, config=config).decide(game_turn)
    intent = next(item for item in result.intents if item.actor_id == far_guard.id)

    # At distance 60, the unit should either:
    # - successfully path via A* (hold_core_defense_ring), OR
    # - use distant fallback (hold_core_defense_ring_distant_fallback)
    # In no case should it be a bare guard_route_blocked WAIT
    assert intent.reason != "guard_route_blocked", (
        f"Guard at moderate distance deadlocked: {intent}"
    )


# ---------------------------------------------------------------------------
# Test: guard unit CLOSE to core still uses normal guard route (no change)
# ---------------------------------------------------------------------------

def test_near_core_guard_vanguard_uses_normal_defense_ring():
    """A guard vanguard close to core should still use the normal
    hold_core_defense_ring path — the fallback must NOT activate
    for short distances."""
    core_obj = core(position=(0, 0))
    near_guard = unit(1, UnitType.VANGUARD, (0, 1))  # distance 1
    config = _guard_config()
    memory = _beacon_memory()
    game_turn = turn(
        owned_core=core_obj,
        units=(near_guard,),
    )

    result = AgentRuntime(memory=memory, config=config).decide(game_turn)
    intent = next(item for item in result.intents if item.actor_id == near_guard.id)

    # Close guard should get normal defense ring behavior
    assert intent.reason in (
        "hold_core_defense_ring",
        "holding_defense_ring",
        "yield_doorstep_holding_defense",
    ), f"Near guard got unexpected reason: {intent.reason}"


# ---------------------------------------------------------------------------
# Test: multi-tick simulation — far guard makes progress over several ticks
# ---------------------------------------------------------------------------

def test_far_guard_makes_progress_over_multiple_ticks():
    """Simulate several ticks: the far guard should move closer to core
    each tick (or at least not stay permanently stuck)."""
    core_obj = core(position=(0, 0))
    far_guard = unit(1, UnitType.VANGUARD, (100, 0))
    config = _guard_config()

    from arena_tactic.memory import AgentMemory
    memory = AgentMemory(last_mode=StrategicMode.BEACON)
    initial_dist = distance(far_guard.position, core_obj.position)

    # Simulate 5 ticks
    positions = [far_guard.position]
    for tick in range(1, 6):
        game_turn = turn(
            tick=tick,
            owned_core=core_obj,
            units=(far_guard,),
        )
        result = AgentRuntime(memory=memory, config=config).decide(game_turn)
        intent = next(item for item in result.intents if item.actor_id == far_guard.id)

        # Should never be a permanent guard_route_blocked
        assert intent.reason != "guard_route_blocked", (
            f"Tick {tick}: guard deadlocked at {far_guard.position}"
        )

        # Move the unit to simulate the next tick
        if intent.action is ActionKind.MOVE and intent.reserved_cell:
            far_guard = unit(1, UnitType.VANGUARD, intent.reserved_cell)
            positions.append(far_guard.position)
            memory = result.next_memory

    # After several ticks, the unit should have moved closer
    final_dist = distance(far_guard.position, core_obj.position)
    assert final_dist < initial_dist, (
        f"Guard did not make progress: started at dist {initial_dist}, "
        f"ended at dist {final_dist}, positions: {positions}"
    )


# ---------------------------------------------------------------------------
# Test: both vanguard and ranger guards far from core in same turn
# ---------------------------------------------------------------------------

def test_both_guard_types_far_from_core_no_deadlock():
    """Both a guard vanguard and guard ranger far from core should produce
    MOVE intents, not guard_route_blocked WAIT."""
    core_obj = core(position=(-898, 1573))
    far_vanguard = unit(1, UnitType.VANGUARD, (-2800, 700))
    far_ranger = unit(5, UnitType.RANGER, (-2600, 900))
    config = _guard_config()
    memory = _beacon_memory()
    game_turn = turn(
        owned_core=core_obj,
        units=(far_vanguard, far_ranger),
        beacon_position=(-1141, -308),
        beacon_status=BeaconStatus.GROUND,
    )

    result = AgentRuntime(memory=memory, config=config).decide(game_turn)

    for intent in result.intents:
        if intent.actor_id in (far_vanguard.id, far_ranger.id):
            assert intent.reason != "guard_route_blocked", (
                f"Unit {intent.actor_id} deadlocked: {intent}"
            )
            assert intent.action is ActionKind.MOVE, (
                f"Unit {intent.actor_id} should MOVE: {intent}"
            )
