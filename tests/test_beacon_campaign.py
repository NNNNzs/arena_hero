from arena_tactic.objectives.beacon import BeaconCampaign, BeaconInput, BeaconStage
from arena_hero import BeaconStatus, UnitType
from arena_tactic import AgentRuntime
from arena_tactic.models import AgentConfig
from .factories import core, turn, unit


def facts(**changes):
    baseline = dict(tick=1, beacon_cell=(3, 3), ground_visible=True, own_carrier_alias=None,
                    carrier_alive=True, escort_ready=0, escort_quorum=2)
    baseline.update(changes)
    return BeaconInput(**baseline)


def test_beacon_campaign_waits_for_escort_quorum():
    state, _, tasks, candidates = BeaconCampaign().evaluate(facts(escort_ready=1))
    assert state.stage is BeaconStage.ASSEMBLE
    assert tasks[0].kind == "ESCORT_CARRIER" and candidates == ("ASSEMBLE_ESCORT",)


def test_pickup_requires_current_ground_visibility():
    state, _, _, candidates = BeaconCampaign().evaluate(facts(escort_ready=2, ground_visible=False))
    assert state.stage is BeaconStage.ASSEMBLE and "PICKUP_BEACON" not in candidates


def test_carrier_death_rebuilds_recovery_tasks():
    state, _, tasks, candidates = BeaconCampaign(BeaconStage.HOLD, "entity_carrier").evaluate(facts(carrier_alive=False))
    assert state.stage is BeaconStage.RECOVER
    assert state.recovery_cell == (3, 3)
    assert tasks[0].kind == "RECOVER_BEACON" and candidates == ("REACQUIRE_BEACON",)


def test_holding_beacon_raises_repair_policy():
    state, _, tasks, candidates = BeaconCampaign().evaluate(facts(own_carrier_alias="entity_carrier"))
    assert state.stage is BeaconStage.HOLD
    assert {task.kind for task in tasks} == {"HOLD_BEACON", "REPAIR_SHIELD"}
    assert candidates == ("REPAIR_SHIELD",)


def test_enabled_campaign_picks_up_only_current_ground_beacon_after_quorum():
    carrier = unit(1, UnitType.VANGUARD, (1, 0))
    escort = unit(2, UnitType.RANGER, (2, 0))
    game_turn = turn(owned_core=core(), units=(carrier, escort), beacon_position=(1, 0), beacon_status=BeaconStatus.GROUND)

    result = AgentRuntime(config=AgentConfig(beacon_campaign_v1=True)).decide(game_turn)

    intent = next(item for item in result.intents if item.actor_id == carrier.id)
    assert intent.action.value == "PICKUP_BEACON"
    assert intent.reason == "beacon_campaign_pickup_current_ground"


def test_enabled_campaign_assembles_a_bounded_combat_escort_from_current_positions():
    vanguard = unit(1, UnitType.VANGUARD, (0, 0))
    ranger = unit(2, UnitType.RANGER, (0, 1))
    result = AgentRuntime(config=AgentConfig(beacon_campaign_v1=True)).decide(
        turn(owned_core=core(), units=(vanguard, ranger), beacon_position=(5, 0))
    )

    reasons = {item.actor_id: item.reason for item in result.intents}
    assert reasons[vanguard.id] == "beacon_campaign_escort"
    assert reasons[ranger.id] == "beacon_campaign_escort"


def test_planner_canary_uses_scheduler_escort_roster_and_distinct_formation_slots():
    vanguard = unit(1, UnitType.VANGUARD, (0, 0))
    ranger = unit(2, UnitType.RANGER, (0, 1))
    extra_vanguard = unit(3, UnitType.VANGUARD, (0, -1))

    result = AgentRuntime(config=AgentConfig(planner_canary=True, beacon_campaign_v1=True)).decide(
        turn(owned_core=core(), units=(vanguard, ranger, extra_vanguard), beacon_position=(5, 0))
    )

    escort_intents = [item for item in result.intents if item.reason == "beacon_campaign_escort"]
    assert {item.actor_id for item in escort_intents} == {vanguard.id, ranger.id}
    assert len({item.reserved_cell for item in escort_intents}) == 2
    assert not result.rejected_intents
