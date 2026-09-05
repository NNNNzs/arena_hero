"""Guard latency: distant guard units must not trigger expensive A*.

Regression for Tick 227209 DECISION_LATENCY_SPIKE: ~10 combat units at
positions 2000+ cells from the core (e.g. [-2862, 900]) were assigned
LEGACY_CORE_GUARD.  Each unit attempted full A* pathfinding (up to 1500–4000
nodes) toward the core guard slot, exhausting the node limit and only then
falling back to _distant_retreat_fallback_intent.  With 10+ units doing this
every tick, cumulative A* search consumed 2.5+ seconds, exceeding the
decision deadline.

Fix: when distance(unit, guard_target) > long_distance_retreat_threshold,
skip A* entirely and use lightweight greedy fallback
(_distant_retreat_fallback_intent) which costs O(1) per unit.
"""
from __future__ import annotations

from unittest.mock import patch as mock_patch

from arena_hero import BeaconStatus, UnitType

from arena_tactic import AgentRuntime
from arena_tactic.models import ActionKind, AgentConfig, StrategicMode
from arena_tactic.navigation import distance

from .factories import core, turn, unit


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
# 1. Distant guard vanguard must NOT invoke A* (latency-critical)
# ---------------------------------------------------------------------------

def test_distant_guard_vanguard_skips_astar():
    """A guard vanguard 2000+ cells from core must bypass A* entirely
    and use the lightweight _distant_retreat_fallback_intent instead.

    This prevents DECISION_LATENCY_SPIKE from accumulated A* node searches.
    """
    core_obj = core(position=(-898, 1573))
    far_guard = unit(1, UnitType.VANGUARD, (-2862, 900))
    config = _guard_config()
    memory = _beacon_memory()
    game_turn = turn(
        owned_core=core_obj,
        units=(far_guard,),
        beacon_position=(-1141, -308),
        beacon_status=BeaconStatus.GROUND,
    )

    with mock_patch(
        "arena_tactic.navigation.bounded_astar",
        side_effect=AssertionError("A* should not be called for distant guard"),
    ):
        result = AgentRuntime(memory=memory, config=config).decide(game_turn)

    intent = next(item for item in result.intents if item.actor_id == far_guard.id)
    # Must be a MOVE with fallback reason (not guard_route_blocked WAIT)
    assert intent.action is ActionKind.MOVE, (
        f"Far guard should MOVE via fallback, got {intent.action}"
    )
    assert "hold_core_defense_ring" in intent.reason, (
        f"Expected hold_core_defense_ring reason, got {intent.reason}"
    )
    assert "distant_fallback" in intent.reason, (
        f"Expected distant_fallback in reason (no A*), got {intent.reason}"
    )


# ---------------------------------------------------------------------------
# 2. Distant guard ranger must NOT invoke A* (latency-critical)
# ---------------------------------------------------------------------------

def test_distant_guard_ranger_skips_astar():
    """A guard ranger 2000+ cells from core must bypass A* entirely."""
    core_obj = core(position=(-898, 1573))
    far_ranger = unit(5, UnitType.RANGER, (-2798, 946))
    config = _guard_config()
    memory = _beacon_memory()
    game_turn = turn(
        owned_core=core_obj,
        units=(far_ranger,),
        beacon_position=(-1141, -308),
        beacon_status=BeaconStatus.GROUND,
    )

    with mock_patch(
        "arena_tactic.navigation.bounded_astar",
        side_effect=AssertionError("A* should not be called for distant guard"),
    ):
        result = AgentRuntime(memory=memory, config=config).decide(game_turn)

    intent = next(item for item in result.intents if item.actor_id == far_ranger.id)
    assert intent.action is ActionKind.MOVE
    assert "ranger_hold_defense_ring" in intent.reason
    assert "distant_fallback" in intent.reason


# ---------------------------------------------------------------------------
# 3. Near-core guard vanguard still uses normal A* (no regression)
# ---------------------------------------------------------------------------

def test_near_guard_vanguard_still_uses_astar():
    """A guard vanguard close to core should still use normal A* pathfinding.
    The latency fix must NOT activate for short distances."""
    core_obj = core(position=(0, 0))
    near_guard = unit(1, UnitType.VANGUARD, (2, 0))
    config = _guard_config()
    memory = _beacon_memory()
    game_turn = turn(
        owned_core=core_obj,
        units=(near_guard,),
    )

    result = AgentRuntime(memory=memory, config=config).decide(game_turn)
    intent = next(item for item in result.intents if item.actor_id == near_guard.id)

    # Close guard should get normal defense ring behavior, not distant_fallback
    assert "distant_fallback" not in intent.reason, (
        f"Near guard should not use distant fallback: {intent.reason}"
    )


# ---------------------------------------------------------------------------
# 4. Near-core guard ranger still uses normal A* (no regression)
# ---------------------------------------------------------------------------

def test_near_guard_ranger_still_uses_astar():
    """A guard ranger close to core should still use normal A* pathfinding."""
    core_obj = core(position=(0, 0))
    near_ranger = unit(5, UnitType.RANGER, (3, 0))
    config = _guard_config()
    memory = _beacon_memory()
    game_turn = turn(
        owned_core=core_obj,
        units=(near_ranger,),
    )

    result = AgentRuntime(memory=memory, config=config).decide(game_turn)
    intent = next(item for item in result.intents if item.actor_id == near_ranger.id)

    assert "distant_fallback" not in intent.reason, (
        f"Near guard ranger should not use distant fallback: {intent.reason}"
    )


# ---------------------------------------------------------------------------
# 5. Multi-unit scenario: 10 distant guards must all use fallback
# ---------------------------------------------------------------------------

def test_many_distant_guards_all_use_fallback():
    """Simulate the Tick 227209 scenario: 10 units at 2000+ cells from core.
    ALL must use distant_fallback (no A*), preventing latency spike."""
    core_obj = core(position=(-898, 1573))
    # Real coordinates from the incident log
    distant_positions = [
        (-2862, 900), (-2798, 946), (-2621, 1166),
        (-2750, 800), (-2900, 1050), (-2680, 1200),
        (-2550, 950), (-2800, 750), (-2650, 1100), (-2720, 880),
    ]
    vanguards = [
        unit(i + 1, UnitType.VANGUARD, pos)
        for i, pos in enumerate(distant_positions[:5])
    ]
    rangers = [
        unit(i + 10, UnitType.RANGER, pos)
        for i, pos in enumerate(distant_positions[5:])
    ]
    all_units = (*vanguards, *rangers)
    config = _guard_config(
        core_guard_vanguards=5,
        core_guard_rangers=5,
    )
    memory = _beacon_memory()
    game_turn = turn(
        owned_core=core_obj,
        units=all_units,
        beacon_position=(-1141, -308),
        beacon_status=BeaconStatus.GROUND,
    )

    with mock_patch(
        "arena_tactic.navigation.bounded_astar",
        side_effect=AssertionError("A* must not run for any distant guard"),
    ):
        result = AgentRuntime(memory=memory, config=config).decide(game_turn)

    # Every guard unit should produce a MOVE with distant_fallback
    for u in all_units:
        intent = next(item for item in result.intents if item.actor_id == u.id)
        assert intent.action is ActionKind.MOVE, (
            f"Unit {u.id} at {u.position} should MOVE, got {intent.action}"
        )
        assert "distant_fallback" in intent.reason, (
            f"Unit {u.id} should use distant_fallback, got {intent.reason}"
        )


# ---------------------------------------------------------------------------
# 6. Fallback move makes progress toward core
# ---------------------------------------------------------------------------

def test_distant_guard_fallback_makes_progress():
    """The distant fallback step should reduce Manhattan distance to core."""
    core_obj = core(position=(-898, 1573))
    far_guard = unit(1, UnitType.VANGUARD, (-2862, 900))
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

    assert intent.action is ActionKind.MOVE
    assert intent.reserved_cell is not None
    old_dist = distance(far_guard.position, core_obj.position)
    new_dist = distance(intent.reserved_cell, core_obj.position)
    assert new_dist <= old_dist, (
        f"Fallback should not increase distance: {old_dist} -> {new_dist}"
    )


# ---------------------------------------------------------------------------
# 7. Moderate distance (> threshold, within 200) also skips A*
# ---------------------------------------------------------------------------

def test_moderate_distance_guard_skips_astar():
    """A guard unit at distance 100 (above threshold of 50) must still
    skip A* and use the lightweight fallback."""
    core_obj = core(position=(0, 0))
    guard = unit(1, UnitType.VANGUARD, (100, 0))
    config = _guard_config()
    memory = _beacon_memory()
    game_turn = turn(owned_core=core_obj, units=(guard,))

    with mock_patch(
        "arena_tactic.navigation.bounded_astar",
        side_effect=AssertionError("A* should not run for distance > threshold"),
    ):
        result = AgentRuntime(memory=memory, config=config).decide(game_turn)

    intent = next(item for item in result.intents if item.actor_id == guard.id)
    assert intent.action is ActionKind.MOVE
    assert "distant_fallback" in intent.reason


# ---------------------------------------------------------------------------
# 8. Unit exactly at threshold boundary uses normal A* (boundary check)
# ---------------------------------------------------------------------------

def test_threshold_boundary_guard_uses_normal_astar():
    """A guard unit at exactly distance 50 (= long_distance_retreat_threshold)
    should use normal A*, not the distant fallback. The check is strict >."""
    core_obj = core(position=(0, 0))
    # Guard target will be at ~distance 1-6 from core. Place unit so
    # distance to guard slot is exactly 50 or just under.
    # Guard slots start at radius 1-3, so put unit at distance 48 from core.
    guard = unit(1, UnitType.VANGUARD, (48, 0))
    config = _guard_config()
    memory = _beacon_memory()
    game_turn = turn(owned_core=core_obj, units=(guard,))

    result = AgentRuntime(memory=memory, config=config).decide(game_turn)
    intent = next(item for item in result.intents if item.actor_id == guard.id)

    # Distance from (48,0) to guard slot at ~(3,0) = 45, which is < 50
    # So normal A* should be used
    assert "distant_fallback" not in intent.reason, (
        f"At boundary, should use normal A*: {intent.reason}"
    )
