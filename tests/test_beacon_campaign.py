from arena_hero import BeaconStatus, UnitType
from arena_tactic.models import AgentConfig
from arena_tactic.objectives.beacon import BeaconCampaign, BeaconInput, BeaconStage

from arena_tactic import AgentRuntime
from arena_tactic.squad_coordination import evaluate_squad_cohesion
from arena_tactic.squads import Squad, SquadMember, SquadRole, SquadType

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
    assert reasons[vanguard.id] == "expedition_formation_move"
    assert reasons[ranger.id] == "expedition_formation_move"


def test_planner_canary_uses_scheduler_escort_roster_and_distinct_formation_slots():
    vanguard = unit(1, UnitType.VANGUARD, (0, 0))
    ranger = unit(2, UnitType.RANGER, (0, 1))
    extra_vanguard = unit(3, UnitType.VANGUARD, (0, -1))

    result = AgentRuntime(config=AgentConfig(planner_canary=True, beacon_campaign_v1=True)).decide(
        turn(owned_core=core(), units=(vanguard, ranger, extra_vanguard), beacon_position=(5, 0))
    )

    escort_intents = [
        item for item in result.intents
        if item.reason in {"expedition_formation_move", "expedition_formation_hold"}
    ]
    assert {item.actor_id for item in escort_intents} == {vanguard.id, ranger.id}
    assert len({item.reserved_cell for item in escort_intents}) == 2
    assert not result.rejected_intents


def expedition(*members, target=(10, 0)):
    return Squad(
        squad_id="squad_expedition_beacon",
        squad_type=SquadType.EXPEDITION_BEACON,
        target=target,
        members=tuple(SquadMember(
            member.id,
            member.unit_type,
            SquadRole.POINT_GUARD
            if member.unit_type is UnitType.VANGUARD
            else SquadRole.FIRE_SUPPORT,
        ) for member in members),
        anchor_unit_id=members[0].id if members else None,
    )


def test_cohesion_uses_slowest_member_to_limit_front_runner_progress():
    front = unit(10, UnitType.VANGUARD, (8, 0))
    pace = unit(11, UnitType.RANGER, (2, 0))

    cohesion = evaluate_squad_cohesion(expedition(front, pace), (front, pace))

    assert cohesion.pace_unit_id == pace.id
    assert cohesion.hold_unit_ids == frozenset({front.id})
    assert cohesion.regroup
    assert not cohesion.pickup_ready


def test_pickup_readiness_requires_quorum_and_close_formation():
    carrier = unit(10, UnitType.VANGUARD, (10, 0))
    close_escort = unit(11, UnitType.RANGER, (9, 0))
    distant_escort = unit(12, UnitType.RANGER, (4, 0))

    ready = evaluate_squad_cohesion(
        expedition(carrier, close_escort), (carrier, close_escort)
    )
    scattered = evaluate_squad_cohesion(
        expedition(carrier, distant_escort), (carrier, distant_escort)
    )

    assert ready.pickup_ready
    assert not scattered.pickup_ready


def test_campaign_front_runner_waits_for_lagging_escort():
    front = unit(10, UnitType.VANGUARD, (8, 0))
    pace = unit(11, UnitType.RANGER, (1, 0))
    result = AgentRuntime(config=AgentConfig(beacon_campaign_v1=True)).decide(
        turn(
            owned_core=core(position=(0, 0)),
            units=(front, pace),
            beacon_position=(10, 0),
        )
    )

    front_intent = next(item for item in result.intents if item.actor_id == front.id)
    assert front_intent.action.value == "WAIT"
    assert front_intent.reason == "expedition_cohesion_hold"


def test_campaign_contact_holds_non_engaged_members_without_replacing_fire():
    ranger = unit(10, UnitType.RANGER, (0, 0))
    vanguard = unit(11, UnitType.VANGUARD, (0, 1))
    enemy = unit(90, UnitType.RANGER, (3, 0), controlled=False)
    result = AgentRuntime(config=AgentConfig(beacon_campaign_v1=True)).decide(
        turn(
            owned_core=core(position=(-1, 0)),
            units=(ranger, vanguard),
            enemies=(enemy,),
            beacon_position=(10, 0),
        )
    )

    intents = {item.actor_id: item for item in result.intents}
    assert intents[ranger.id].action.value == "SHOOT"
    assert intents[vanguard.id].action.value == "WAIT"
    assert intents[vanguard.id].reason == "expedition_contact_hold"


def test_default_planner_arbitrates_every_expedition_squad_member():
    core_guard = unit(1, UnitType.VANGUARD, (0, 1))
    front = unit(2, UnitType.VANGUARD, (8, 0))
    pace = unit(3, UnitType.VANGUARD, (2, 0))
    fire_guard = unit(4, UnitType.RANGER, (0, -1))
    fire_one = unit(5, UnitType.RANGER, (3, 0))
    fire_two = unit(6, UnitType.RANGER, (4, 0))

    result = AgentRuntime().decide(turn(
        owned_core=core(),
        units=(core_guard, front, pace, fire_guard, fire_one, fire_two),
        beacon_position=(10, 0),
    ))

    intents = {item.actor_id: item for item in result.intents}
    expedition_ids = {front.id, pace.id, fire_one.id, fire_two.id}
    assert all(intents[actor_id].reason.startswith("expedition_") for actor_id in expedition_ids)
    assert intents[front.id].reason == "expedition_cohesion_hold"
    assert not intents[core_guard.id].reason.startswith("expedition_")
    assert not intents[fire_guard.id].reason.startswith("expedition_")
