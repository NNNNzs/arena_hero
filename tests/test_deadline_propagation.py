"""Tests for deadline propagation and vanguard intercept fallback."""

from __future__ import annotations

from unittest.mock import patch

from arena_hero import UnitType
from arena_tactic.context import DecisionContext
from arena_tactic.memory import AgentMemory
from arena_tactic.models import ActionKind, AgentConfig, ReservationTable
from arena_tactic.runtime import AgentRuntime
from arena_tactic.squads import build_squad_plan
from arena_tactic.strategy.vanguards import _plan_vanguards
from tests.factories import core, turn, unit


def test_deadline_passed_to_beacon_campaign():
    """Verify that AgentRuntime passes deadline to _apply_beacon_campaign."""
    runtime = AgentRuntime()
    test_turn = turn(tick=10, owned_core=core(position=(0, 0)))

    with patch.object(runtime, "_apply_beacon_campaign", wraps=runtime._apply_beacon_campaign) as mock_beacon:
        runtime.decide(test_turn)
        assert mock_beacon.called
        _, kwargs = mock_beacon.call_args
        assert "deadline" in kwargs
        assert kwargs["deadline"] is not None


def test_vanguard_intercept_sidestep_fallback():
    """When direct path to threat is blocked, vanguard should sidestep towards core instead of waiting."""
    v = unit(1, UnitType.VANGUARD, (10, 10))
    e = unit(2, UnitType.WORKER, (10, 12))
    c = core(position=(0, 0))

    t = turn(tick=10, owned_core=c, units=(v,), enemies=(e,))
    ctx = DecisionContext.from_turn(t)
    mem = AgentMemory()
    config = AgentConfig(intercept_distance=5)
    reservations = ReservationTable(occupancy={})
    squad_plan = build_squad_plan(ctx, mem, config)

    # Mock _move to fail (simulate obstacle-blocked corridor)
    with patch("arena_tactic.strategy.vanguards._move", return_value=None):
        intents = _plan_vanguards(
            ctx, mem, reservations, deadline=999999.0,
            config=config, heal_allowances={}, squad_plan=squad_plan,
        )

    assert len(intents) == 1
    intent = intents[0]
    assert intent.actor_id == v.id
    # Intent should be a MOVE sidestep, not a WAIT (guard_route_blocked or visible_threat_route_blocked)
    assert intent.action == ActionKind.MOVE
    assert intent.reason.endswith("_sidestep")
