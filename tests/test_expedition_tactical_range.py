"""Tactical range guard: expedition combat units must not target distant base enemies.

Regression tests for:
1. SQUAD_EXPEDITION_STALL: Expedition units thousands of cells away from base
   being drawn into intercepting or seeking firing lines against intruders
   near the core, abandoning their beacon advance and freezing in place.
2. DECISION_LATENCY_SPIKE: Distant combat units attempting full-map A* pathfinding
   and expensive shadow fire advantage evaluations across 2000+ cells against
   enemies near the base, hitting the 2500ms+ timeout limit.
"""
from __future__ import annotations

from unittest.mock import patch as mock_patch

from arena_hero import BeaconStatus, UnitType

from arena_tactic import AgentRuntime
from arena_tactic.models import ActionKind, AgentConfig, StrategicMode
from arena_tactic.navigation import distance

from .factories import core, turn, unit


def _expedition_config(**overrides) -> AgentConfig:
    defaults = dict(
        core_guard_vanguards=1,
        core_guard_rangers=1,
        expedition_vanguards=4,
        expedition_rangers=4,
        mining_escort_vanguards=0,
        mining_escort_rangers=0,
        scout_vanguards=0,
        scout_rangers=0,
        intercept_distance=8,
    )
    defaults.update(overrides)
    return AgentConfig(**defaults)


def _beacon_memory(**overrides):
    from arena_tactic.memory import AgentMemory
    defaults = dict(last_mode=StrategicMode.BEACON)
    defaults.update(overrides)
    return AgentMemory(**defaults)


def test_distant_expedition_ranger_ignores_distant_base_enemy():
    """An expedition ranger 2000+ cells away must NOT seek firing line against base intruders."""
    core_cell = (-898, 1573)
    beacon_cell = (-1134, -1111)
    base_enemy_cell = (-887, 1578)  # distance to core ~16
    far_ranger_cell = (-2797, 946)  # distance to enemy ~2500

    t = turn(
        owned_core=core(position=core_cell),
        units=[
            unit(1, UnitType.RANGER, far_ranger_cell),
        ],
        enemies=[
            unit(2, UnitType.WORKER, base_enemy_cell, hp=2, controlled=False),
        ],
        beacon_position=beacon_cell,
        beacon_status=BeaconStatus.GROUND,
        tick=227464,
    )

    runtime = AgentRuntime(config=_expedition_config(), memory=_beacon_memory())
    u = t.units[0]

    with mock_patch("arena_tactic.strategy.rangers._ranger_staging_cell") as mock_staging:
        result = runtime.decide(t)
        # Should NOT evaluate expensive staging cells for enemies 2500 cells away
        assert mock_staging.call_count == 0

    intent = next(i for i in result.intents if i.actor_id == u.id)
    # Must NOT be seeking firing line against base intruder
    assert intent.reason != "ranger_seek_legal_firing_line"
    assert "firing_route_blocked" not in intent.reason


def test_distant_expedition_vanguard_ignores_distant_base_enemy():
    """An expedition vanguard 2000+ cells away must NOT intercept base intruders."""
    core_cell = (-898, 1573)
    beacon_cell = (-1134, -1111)
    base_enemy_cell = (-887, 1578)  # distance to core ~16
    far_vanguard_cell = (-2733, 1140)  # distance to enemy ~2200

    t = turn(
        owned_core=core(position=core_cell),
        units=[
            unit(1, UnitType.VANGUARD, far_vanguard_cell),
        ],
        enemies=[
            unit(2, UnitType.WORKER, base_enemy_cell, hp=2, controlled=False),
        ],
        beacon_position=beacon_cell,
        beacon_status=BeaconStatus.GROUND,
        tick=227464,
    )

    runtime = AgentRuntime(config=_expedition_config(), memory=_beacon_memory())
    u = t.units[0]
    result = runtime.decide(t)

    intent = next(i for i in result.intents if i.actor_id == u.id)
    # Must NOT attempt to path 2200 cells to intercept visible base threat
    assert intent.reason != "intercept_visible_threat"
    assert "visible_threat_route_blocked" not in intent.reason


def test_nearby_combat_units_still_intercept_and_fire():
    """Combat units within tactical range must still engage local intruders."""
    core_cell = (-898, 1573)
    beacon_cell = (-1134, -1111)
    base_enemy_cell = (-887, 1578)  # distance ~16
    nearby_vanguard_cell = (-894, 1578)  # distance to enemy is 7 (<= intercept_distance)
    nearby_ranger_cell = (-890, 1578)  # distance to enemy is 3 (in firing range)

    t = turn(
        owned_core=core(position=core_cell),
        units=[
            unit(1, UnitType.VANGUARD, nearby_vanguard_cell),
            unit(2, UnitType.RANGER, nearby_ranger_cell),
        ],
        enemies=[
            unit(3, UnitType.WORKER, base_enemy_cell, hp=2, controlled=False),
        ],
        beacon_position=beacon_cell,
        beacon_status=BeaconStatus.GROUND,
        tick=227464,
    )

    runtime = AgentRuntime(config=_expedition_config(core_guard_vanguards=0, core_guard_rangers=0), memory=_beacon_memory())
    result = runtime.decide(t)

    vg_intent = next(i for i in result.intents if i.actor_id == t.units[0].id)
    rg_intent = next(i for i in result.intents if i.actor_id == t.units[1].id)

    # Local vanguard intercepts the nearby enemy
    assert vg_intent.reason in ("intercept_visible_threat", "highest_value_adjacent_enemy_cell")
    # Local ranger shoots or maneuvers to firing line
    assert rg_intent.action in (ActionKind.SHOOT, ActionKind.MOVE)
