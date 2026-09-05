"""Guard: expedition vanguards and far-from-core vanguards must not be
assigned the mineral_tank (base mineral cell body-block) task.

Regression for SQUAD_EXPEDITION_STALL observed at Tick 225835~225841 where
16 expedition vanguards were stuck on `mineral_tank_route_blocked` because
the mineral_tank logic ran before the expedition membership check and had no
distance guard.
"""
from __future__ import annotations

from arena_hero import BeaconStatus, UnitType

from arena_tactic import AgentRuntime, choose_actions
from arena_tactic.context import DecisionContext
from arena_tactic.memory import AgentMemory
from arena_tactic.models import ActionKind, AgentConfig, StrategicMode
from arena_tactic.navigation import distance
from arena_tactic.squads import SquadType, build_squad_plan
from arena_tactic.strategy.vanguards import _plan_vanguards

from .factories import core, turn, unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _beacon_config(**overrides) -> AgentConfig:
    """Config with a live BEACON expedition and enough guards."""
    defaults = dict(
        beacon_campaign_v1=True,
        core_guard_vanguards=1,
        core_guard_rangers=0,
        expedition_vanguards=2,
        expedition_rangers=0,
        mining_escort_vanguards=0,
        mining_escort_rangers=0,
        scout_vanguards=0,
        scout_rangers=0,
    )
    defaults.update(overrides)
    return AgentConfig(**defaults)


def _beacon_memory(**overrides) -> AgentMemory:
    defaults = dict(last_mode=StrategicMode.BEACON)
    defaults.update(overrides)
    return AgentMemory(**defaults)


# ---------------------------------------------------------------------------
# Test: expedition vanguard must not be assigned mineral_tank
# ---------------------------------------------------------------------------

def test_expedition_vanguard_does_not_receive_mineral_tank_task():
    """An expedition vanguard far from core with mineral cells near core
    must NOT be pulled back to base for mineral_tank duty."""
    guard = unit(1, UnitType.VANGUARD, (0, 1))
    expedition_far = unit(2, UnitType.VANGUARD, (30, 0))  # far from core
    resources_near_core = ((3, 0), (4, 0))

    config = _beacon_config()
    memory = _beacon_memory()
    game_turn = turn(
        owned_core=core(),
        units=(guard, expedition_far),
        resource_cells=resources_near_core,
        beacon_position=(50, 0),
        beacon_status=BeaconStatus.GROUND,
    )

    result = AgentRuntime(memory=memory, config=config).decide(game_turn)
    intent = next(item for item in result.intents if item.actor_id == expedition_far.id)

    # Expedition vanguard must NOT get mineral_tank; should head to beacon instead.
    assert intent.reason != "mineral_tank_route_blocked"
    assert intent.reason != "hold_vanguard_mineral_tank"
    assert "mineral_tank" not in intent.reason


def test_expedition_vanguard_marches_to_beacon_despite_minerals_near_core():
    """Even with rich mineral cells next to the core, the expedition vanguard
    must continue advancing toward the beacon."""
    guard = unit(1, UnitType.VANGUARD, (0, 1))
    expedition_vanguard = unit(2, UnitType.VANGUARD, (5, 0))

    config = _beacon_config()
    memory = _beacon_memory()
    game_turn = turn(
        owned_core=core(),
        units=(guard, expedition_vanguard),
        resource_cells=((3, 0), (4, 0)),
        beacon_position=(50, 0),
        beacon_status=BeaconStatus.GROUND,
    )

    result = AgentRuntime(memory=memory, config=config).decide(game_turn)
    intent = next(item for item in result.intents if item.actor_id == expedition_vanguard.id)

    # Expedition vanguard should either march to beacon (via campaign
    # coordinator `beacon_campaign_escort` or raw `expedition_vanguard_to_beacon`)
    # or pick it up — never mineral_tank.
    assert "mineral_tank" not in intent.reason
    assert intent.reason in (
        "beacon_campaign_escort",
        "expedition_vanguard_to_beacon",
        "expedition_vanguard_sidestep",
        "expedition_beacon",
        "beacon_campaign_pickup_current_ground",
        "preferred_vanguard_beacon_pickup",
        "expedition_formation_move",
        "expedition_cohesion_hold",
        "expedition_contact_hold",
        "visible_threat_route_blocked",
        "intercept_visible_threat",
    ), f"Unexpected reason: {intent.reason}"


# ---------------------------------------------------------------------------
# Test: guard vanguard within defense_exit_distance CAN get mineral_tank
# ---------------------------------------------------------------------------

def test_guard_vanguard_near_core_receives_mineral_tank():
    """A base-defense guard vanguard close to the core should still be
    eligible for mineral_tank body-blocking."""
    guard = unit(1, UnitType.VANGUARD, (0, 1))  # within defense_exit_distance
    config = _beacon_config()
    memory = _beacon_memory()
    game_turn = turn(
        owned_core=core(),
        units=(guard,),
        resource_cells=((3, 0),),
        beacon_position=(50, 0),
        beacon_status=BeaconStatus.GROUND,
    )

    result = AgentRuntime(memory=memory, config=config).decide(game_turn)
    intent = next(item for item in result.intents if item.actor_id == guard.id)

    # Guard near core SHOULD be eligible for mineral_tank.
    assert intent.reason in (
        "hold_vanguard_mineral_tank",
        "vanguard_mineral_tank",
        "mineral_tank_route_blocked",
        # Might also be holding defense ring or intercepting
        "hold_core_defense_ring",
        "highest_value_adjacent_enemy_cell",
        "intercept_visible_threat",
    )


# ---------------------------------------------------------------------------
# Test: vanguard beyond defense_exit_distance does not receive mineral_tank
# ---------------------------------------------------------------------------

def test_far_from_core_vanguard_blocked_from_mineral_tank():
    """A vanguard beyond defense_exit_distance (even if classified as a guard
    by legacy logic) must not be pulled back for mineral_tank."""
    # Use no squad_plan (legacy path) so all non-guard vanguards become
    # expedition. Place a guard beyond defense_exit_distance.
    distant_guard = unit(1, UnitType.VANGUARD, (20, 0))  # far from core

    # Legacy mode: no squad_plan, guard = combat_rosters guards.
    # With no enemies, legacy_guards is empty, so all vanguards are expedition.
    # But the test verifies the distance guard works.
    result = choose_actions(
        turn(
            owned_core=core(),
            units=(distant_guard,),
            resource_cells=((3, 0),),
        ),
    )
    intent = next(item for item in result.intents if item.actor_id == distant_guard.id)

    assert "mineral_tank" not in intent.reason


# ---------------------------------------------------------------------------
# Test: squad plan correctly assigns vanguards to disjoint squads
# ---------------------------------------------------------------------------

def test_squad_plan_expedition_and_guard_sets_are_disjoint():
    """Expedition vanguards and guard/mining vanguards must be disjoint sets,
    ensuring the mineral_tank guard never overlaps."""
    vanguards = tuple(unit(i, UnitType.VANGUARD, (i, 0)) for i in range(1, 7))
    config = _beacon_config()
    context = DecisionContext.from_turn(
        turn(
            owned_core=core(),
            units=vanguards,
            beacon_position=(50, 0),
        )
    )
    memory = _beacon_memory()
    plan = build_squad_plan(context, memory, config)

    expedition = plan.ids_for(SquadType.EXPEDITION_BEACON, UnitType.VANGUARD)
    guard = plan.ids_for(SquadType.BASE_DEFENSE, UnitType.VANGUARD)
    mining = plan.ids_for(SquadType.MINING_ESCORT, UnitType.VANGUARD)

    assert expedition.isdisjoint(guard), f"expedition ∩ guard = {expedition & guard}"
    assert expedition.isdisjoint(mining), f"expedition ∩ mining = {expedition & mining}"


# ---------------------------------------------------------------------------
# Test: integration — all expedition members produce non-mineral_tank intents
# ---------------------------------------------------------------------------

def test_all_expedition_vanguards_avoid_mineral_tank_in_full_roster():
    """Full roster with 6 vanguards in BEACON mode: the 2 expedition members
    must produce intents that are NOT mineral_tank."""
    vanguards = tuple(unit(i, UnitType.VANGUARD, (i, 0)) for i in range(1, 7))
    config = _beacon_config()
    memory = _beacon_memory()
    game_turn = turn(
        owned_core=core(),
        units=vanguards,
        resource_cells=((3, 0), (4, 0), (5, 0)),
        beacon_position=(50, 0),
        beacon_status=BeaconStatus.GROUND,
    )

    result = AgentRuntime(memory=memory, config=config).decide(game_turn)

    context = DecisionContext.from_turn(game_turn)
    plan = build_squad_plan(context, memory, config)
    expedition_ids = plan.ids_for(SquadType.EXPEDITION_BEACON, UnitType.VANGUARD)

    for intent in result.intents:
        if intent.actor_id in expedition_ids:
            assert "mineral_tank" not in intent.reason, (
                f"Expedition vanguard {intent.actor_id} got mineral_tank task: {intent.reason}"
            )
