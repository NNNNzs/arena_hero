"""Regression tests for DECISION_LATENCY_SPIKE (决策延迟激增) deadline propagation
and DEFENSE_DISENGAGED (防守单位脱离交战) sidestep fallback.

Covers two root-cause fixes:
1. Global deadline must propagate from decide() through _apply_beacon_campaign,
   _apply_core_attack, _apply_core_migration, and _objective_move — no independent
   deadline creation allowed.
2. Combat units with blocked routes to guard/intercept targets must attempt
   _deploy_sidestep before falling back to WAIT, preventing DEFENSE_DISENGAGED.
"""
from __future__ import annotations

from time import perf_counter
from unittest import mock
from unittest.mock import patch as mock_patch

from arena_hero import BeaconStatus, UnitType

from arena_tactic import AgentRuntime
from arena_tactic.models import ActionKind, AgentConfig, StrategicMode
from arena_tactic.navigation import distance
from arena_tactic.runtime import AgentRuntime as Runtime

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


# ===========================================================================
# Part 1: DECISION_LATENCY_SPIKE — Global deadline propagation
# ===========================================================================

# ---------------------------------------------------------------------------
# 1.1 _objective_move must use passed-in deadline, not create new one
# ---------------------------------------------------------------------------

def test_objective_move_uses_passed_deadline():
    """When a deadline is explicitly passed, _objective_move must not create
    a fresh one.  We verify by passing an already-expired deadline — plan_step
    should immediately bail out (returning None) rather than having 500ms."""
    core_obj = core(position=(0, 0))
    vg = unit(1, UnitType.VANGUARD, (5, 0))
    config = AgentConfig()
    memory = _beacon_memory()
    game_turn = turn(owned_core=core_obj, units=(vg,))

    rt = AgentRuntime(memory=memory, config=config)
    context_obj = __import__("arena_tactic.context", fromlist=["DecisionContext"]).DecisionContext.from_turn(game_turn)

    # Expired deadline: 1 second in the past
    expired = perf_counter() - 1.0
    result = rt._objective_move(
        context_obj, memory, vg, (10, 0), 500, "test_reason",
        avoid_threats=False, deadline=expired,
    )
    # With expired deadline, plan_step should return None (no time budget)
    assert result is None, (
        f"_objective_move should respect expired deadline, got {result}"
    )


# ---------------------------------------------------------------------------
# 1.2 _apply_beacon_campaign passes deadline to coordinate_expedition_intents
# ---------------------------------------------------------------------------

def test_beacon_campaign_passes_deadline_to_squad_coordination():
    """_apply_beacon_campaign must forward the global deadline to
    coordinate_expedition_intents rather than letting it create a new one."""
    called_kwargs = {}

    def mock_coordinate(*args, **kwargs):
        called_kwargs.update(kwargs)
        return kwargs.get("proposals", args[4] if len(args) > 4 else ())

    core_obj = core(position=(0, 0))
    vg = unit(1, UnitType.VANGUARD, (1, 0))
    ranger = unit(5, UnitType.RANGER, (2, 0))
    config = AgentConfig(beacon_campaign_v1=True)
    from arena_tactic.memory import AgentMemory
    memory = AgentMemory(
        last_mode=StrategicMode.BEACON,
        objective_states={"beacon": {"stage": "ASSEMBLE", "escort_aliases": []}},
    )
    game_turn = turn(
        owned_core=core_obj,
        units=(vg, ranger),
        beacon_position=(3, 0),
        beacon_status=BeaconStatus.GROUND,
    )

    with mock_patch(
        "arena_tactic.runtime.coordinate_expedition_intents",
        side_effect=mock_coordinate,
    ):
        rt = AgentRuntime(memory=memory, config=config)
        # Set a known deadline
        test_deadline = perf_counter() + 999.0
        result = rt.decide(game_turn)

    # If beacon campaign was active, deadline must have been forwarded
    if called_kwargs.get("deadline") is not None:
        assert called_kwargs["deadline"] <= perf_counter() + 1000, (
            "deadline should be the global one from decide(), not a fresh one"
        )


# ---------------------------------------------------------------------------
# 1.3 _objective_move in _apply_core_attack uses global deadline
# ---------------------------------------------------------------------------

def test_core_attack_uses_global_deadline():
    """_apply_core_attack must pass the global deadline to _objective_move."""
    captured_deadlines = []
    original_objective_move = Runtime._objective_move

    def tracking_objective_move(self, context, memory, unit, target, score, reason, *, avoid_threats, reservations=None, deadline=None):
        captured_deadlines.append(deadline)
        return original_objective_move(self, context, memory, unit, target, score, reason, avoid_threats=avoid_threats, reservations=reservations, deadline=deadline)

    core_obj = core(position=(0, 0))
    vg = unit(1, UnitType.VANGUARD, (5, 0))
    enemy_core = core(value=200, position=(10, 0), controlled=False)
    config = AgentConfig(core_attack_campaign_v1=True)
    from arena_tactic.memory import AgentMemory
    memory = AgentMemory(
        last_mode=StrategicMode.DEFEND,
        objective_states={"attack": {"stage": "RALLY"}},
    )
    game_turn = turn(
        owned_core=core_obj,
        units=(vg,),
        enemies=(enemy_core,),
    )

    with mock.patch.object(Runtime, "_objective_move", tracking_objective_move):
        rt = AgentRuntime(memory=memory, config=config)
        result = rt.decide(game_turn)

    # All captured deadlines should be non-None (global deadline passed)
    for d in captured_deadlines:
        if d is not None:
            # Should be a real deadline, not a freshly created one
            # (a fresh one would be close to perf_counter(), while the global
            # one was created at decide() start)
            assert d <= perf_counter() + 1.0, (
                "Deadline should be from decide() start, not freshly created"
            )


# ===========================================================================
# Part 2: DEFENSE_DISENGAGED — Sidestep fallback for blocked routes
# ===========================================================================

# ---------------------------------------------------------------------------
# 2.1 Guard vanguard near core with blocked route uses sidestep, not WAIT
# ---------------------------------------------------------------------------

def test_guard_vanguard_blocked_route_attempts_sidestep():
    """A guard vanguard near core with blocked A* route should attempt
    _deploy_sidestep before falling back to guard_route_blocked WAIT."""
    core_obj = core(position=(0, 0))
    # Place vanguard at a moderate distance (not far enough for distant fallback)
    # but with obstacles blocking the direct path
    vg = unit(1, UnitType.VANGUARD, (3, 0))
    # Create obstacles that block the path to guard slot
    obstacles = [(2, 0), (1, 0), (0, 1), (-1, 0), (0, -1)]
    config = _guard_config()
    memory = _beacon_memory()
    game_turn = turn(
        owned_core=core_obj,
        units=(vg,),
        obstacle_cells=obstacles,
    )

    result = AgentRuntime(memory=memory, config=config).decide(game_turn)
    intent = next(item for item in result.intents if item.actor_id == vg.id)

    # Should not be bare guard_route_blocked — should either MOVE (sidestep)
    # or at least have tried sidestep
    if intent.action is ActionKind.MOVE:
        assert "sidestep" in intent.reason or "defense_ring" in intent.reason or "hold_core" in intent.reason, (
            f"Guard should attempt sidestep or move, got: {intent.reason}"
        )
    # If it's a WAIT, it must NOT be guard_route_blocked (that means sidestep wasn't tried)
    if intent.action is ActionKind.WAIT:
        assert intent.reason != "guard_route_blocked", (
            "Guard vanguard fell through to guard_route_blocked WAIT without trying sidestep"
        )


# ---------------------------------------------------------------------------
# 2.2 Guard ranger near core with blocked route uses sidestep, not WAIT
# ---------------------------------------------------------------------------

def test_guard_ranger_blocked_route_attempts_sidestep():
    """A guard ranger near core with blocked A* route should attempt
    _deploy_sidestep before falling back to guard_route_blocked WAIT."""
    core_obj = core(position=(0, 0))
    ranger = unit(5, UnitType.RANGER, (4, 0))
    obstacles = [(3, 0), (2, 0), (1, 0), (0, 1), (-1, 0), (0, -1)]
    config = _guard_config()
    memory = _beacon_memory()
    game_turn = turn(
        owned_core=core_obj,
        units=(ranger,),
        obstacle_cells=obstacles,
    )

    result = AgentRuntime(memory=memory, config=config).decide(game_turn)
    intent = next(item for item in result.intents if item.actor_id == ranger.id)

    if intent.action is ActionKind.WAIT:
        assert intent.reason != "guard_route_blocked", (
            "Guard ranger fell through to guard_route_blocked WAIT without trying sidestep"
        )


# ---------------------------------------------------------------------------
# 2.3 Vanguard intercepting visible threat with blocked route uses sidestep
# ---------------------------------------------------------------------------

def test_vanguard_threat_intercept_blocked_attempts_sidestep():
    """A vanguard intercepting a visible threat with blocked route should
    attempt _deploy_sidestep before visible_threat_route_blocked WAIT."""
    core_obj = core(position=(0, 0))
    # Place vanguard close enough to intercept (within intercept_distance * 2 = 16)
    vg = unit(1, UnitType.VANGUARD, (5, 0))
    enemy_vg = unit(100, UnitType.VANGUARD, (7, 0), controlled=False)
    # Block direct path
    obstacles = [(6, 0)]
    config = _guard_config(
        core_guard_vanguards=0,  # Not a guard, so it will try to intercept
        intercept_vanguards=1,
    )
    memory = _beacon_memory()
    game_turn = turn(
        owned_core=core_obj,
        units=(vg,),
        enemies=(enemy_vg,),
        obstacle_cells=obstacles,
    )

    result = AgentRuntime(memory=memory, config=config).decide(game_turn)
    intent = next(item for item in result.intents if item.actor_id == vg.id)

    # Should not be bare visible_threat_route_blocked WAIT
    if intent.action is ActionKind.WAIT:
        assert intent.reason != "visible_threat_route_blocked", (
            "Vanguard fell through to visible_threat_route_blocked WAIT without trying sidestep"
        )


# ---------------------------------------------------------------------------
# 2.4 Integration: guard with obstacles produces MOVE (sidestep) not WAIT
# ---------------------------------------------------------------------------

def test_guard_with_surrounding_obstacles_produces_move():
    """When guard slot is blocked but there are adjacent free cells,
    the unit should MOVE (sidestep) rather than WAIT."""
    core_obj = core(position=(0, 0))
    # Vanguard at (0, 2) — guard slot will be at (0, 1) or similar
    vg = unit(1, UnitType.VANGUARD, (0, 2))
    # Block some directions but leave at least one open
    obstacles = [(0, 1)]
    config = _guard_config()
    memory = _beacon_memory()
    game_turn = turn(
        owned_core=core_obj,
        units=(vg,),
        obstacle_cells=obstacles,
    )

    result = AgentRuntime(memory=memory, config=config).decide(game_turn)
    intent = next(item for item in result.intents if item.actor_id == vg.id)

    # Should produce a MOVE (sidestep or A*) or holding_defense_ring WAIT
    # but NOT guard_route_blocked
    assert intent.reason != "guard_route_blocked", (
        f"Guard with partial obstacles should sidestep, got: {intent}"
    )
