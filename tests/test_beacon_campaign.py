from arena_hero import BeaconStatus, UnitType

from arena_tactic import AgentRuntime
from arena_tactic.context import DecisionContext
from arena_tactic.identity import entity_alias
from arena_tactic.memory import AgentMemory
from arena_tactic.models import AgentConfig, StrategicMode
from arena_tactic.objectives.beacon import BeaconCampaign, BeaconInput, BeaconStage

from arena_tactic.squad_coordination import (
    coordinate_expedition_intents,
    evaluate_squad_cohesion,
)
from arena_tactic.squads import (
    Squad,
    SquadMember,
    SquadRole,
    SquadType,
    build_squad_plan,
)

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
    assert state.stage is BeaconStage.EXFIL
    assert {task.kind for task in tasks} == {"EXFIL_BEACON", "ESCORT_CARRIER"}
    assert candidates == ("WITHDRAW_BEACON",)


def test_secured_beacon_expands_mining_escort_policy():
    state, _, tasks, candidates = BeaconCampaign().evaluate(facts(
        own_carrier_alias="entity_carrier",
        carrier_secured=True,
    ))

    assert state.stage is BeaconStage.SECURE
    assert {task.kind for task in tasks} == {
        "SECURE_BEACON",
        "REPAIR_SHIELD",
        "EXPAND_MINING_ESCORT",
    }
    assert candidates == ("REPAIR_SHIELD", "EXPAND_MINING_ESCORT")


def campaign_runtime(**changes):
    config = dict(
        beacon_campaign_v1=True,
        core_guard_vanguards=0,
        core_guard_rangers=0,
        expedition_vanguards=1,
        expedition_rangers=1,
        mining_escort_vanguards=0,
        mining_escort_rangers=0,
        scout_vanguards=0,
        scout_rangers=0,
    )
    config.update(changes)
    return AgentRuntime(
        memory=AgentMemory(last_mode=StrategicMode.BEACON),
        config=AgentConfig(**config),
    )


def test_enabled_campaign_picks_up_only_current_ground_beacon_after_quorum():
    carrier = unit(1, UnitType.VANGUARD, (1, 0))
    escort = unit(2, UnitType.RANGER, (2, 0))
    game_turn = turn(owned_core=core(), units=(carrier, escort), beacon_position=(1, 0), beacon_status=BeaconStatus.GROUND)

    result = campaign_runtime().decide(game_turn)

    intent = next(item for item in result.intents if item.actor_id == carrier.id)
    assert intent.action.value == "PICKUP_BEACON"
    assert intent.reason == "beacon_campaign_pickup_current_ground"


def test_lone_expedition_member_waits_for_pickup_quorum():
    carrier = unit(3, UnitType.VANGUARD, (1, 0))
    runtime = campaign_runtime(expedition_rangers=0)
    result = runtime.decide(turn(
        owned_core=core(),
        units=(carrier,),
        beacon_position=carrier.position,
        beacon_status=BeaconStatus.GROUND,
    ))

    intent = next(item for item in result.intents if item.actor_id == carrier.id)
    assert intent.action.value == "WAIT"
    assert intent.reason == "expedition_pickup_waits_for_escort"


def test_enabled_campaign_assembles_a_bounded_combat_escort_from_current_positions():
    vanguard = unit(1, UnitType.VANGUARD, (0, 0))
    ranger = unit(2, UnitType.RANGER, (0, 1))
    result = campaign_runtime().decide(
        turn(owned_core=core(), units=(vanguard, ranger), beacon_position=(5, 0))
    )

    reasons = {item.actor_id: item.reason for item in result.intents}
    assert reasons[vanguard.id] == "expedition_formation_move"
    assert reasons[ranger.id] == "expedition_formation_move"


def test_planner_canary_uses_scheduler_escort_roster_and_distinct_formation_slots():
    vanguard = unit(1, UnitType.VANGUARD, (0, 0))
    ranger = unit(2, UnitType.RANGER, (0, 1))
    extra_vanguard = unit(3, UnitType.VANGUARD, (0, -1))

    result = campaign_runtime(planner_canary=True).decide(
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


def test_cohesion_allows_members_within_formation_radius_to_march_together():
    front = unit(10, UnitType.VANGUARD, (4, 0))
    middle = unit(11, UnitType.RANGER, (2, 0))
    pace = unit(12, UnitType.RANGER, (0, 0))

    cohesion = evaluate_squad_cohesion(
        expedition(front, middle, pace),
        (front, middle, pace),
    )

    assert not cohesion.regroup
    assert cohesion.hold_unit_ids == frozenset()


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
    result = campaign_runtime().decide(
        turn(
            owned_core=core(position=(0, 0)),
            units=(front, pace),
            beacon_position=(10, 0),
        )
    )

    front_intent = next(item for item in result.intents if item.actor_id == front.id)
    assert front_intent.action.value == "WAIT"
    assert front_intent.reason == "expedition_cohesion_hold"


def test_campaign_extreme_split_advances_trailing_escort_without_mutual_hold():
    front = unit(10, UnitType.VANGUARD, (200, 0))
    trailing = unit(11, UnitType.RANGER, (0, 0))

    result = campaign_runtime().decide(
        turn(
            owned_core=core(position=(0, 0)),
            units=(front, trailing),
            beacon_position=(400, 0),
        )
    )

    intents = {item.actor_id: item for item in result.intents}
    assert intents[front.id].reason == "expedition_cohesion_hold"
    assert intents[trailing.id].action.value == "MOVE"
    assert intents[trailing.id].reason == "expedition_regroup"
    assert intents[trailing.id].reserved_cell == (1, 0)


def test_campaign_extreme_split_releases_the_entire_trailing_group_to_march():
    front = unit(10, UnitType.VANGUARD, (200, 0))
    trailing_vanguard = unit(11, UnitType.VANGUARD, (0, 0))
    trailing_ranger_one = unit(12, UnitType.RANGER, (0, 1))
    trailing_ranger_two = unit(13, UnitType.RANGER, (1, 0))

    result = campaign_runtime(
        expedition_vanguards=2,
        expedition_rangers=2,
    ).decide(turn(
        owned_core=core(position=(0, -1)),
        units=(front, trailing_vanguard, trailing_ranger_one, trailing_ranger_two),
        beacon_position=(400, 0),
    ))

    intents = {item.actor_id: item for item in result.intents}
    assert intents[front.id].reason == "expedition_cohesion_hold"
    for member in (trailing_vanguard, trailing_ranger_one, trailing_ranger_two):
        assert intents[member.id].action.value == "MOVE"
        assert intents[member.id].reason.startswith("expedition_regroup")


def test_expedition_crowded_departure_uses_local_evasion_for_all_members():
    vanguard = unit(10, UnitType.VANGUARD, (0, 0))
    ranger = unit(11, UnitType.RANGER, (0, 0))
    # A current-Turn full cell occupies the direct departure lane.  These are
    # not squad members, so the formation must use a side lane this Tick.
    traffic_one = unit(20, UnitType.WORKER, (1, 0))
    traffic_two = unit(21, UnitType.WORKER, (1, 0))
    context = DecisionContext.from_turn(turn(
        owned_core=core(position=(-2, 0)),
        units=(vanguard, ranger, traffic_one, traffic_two),
        beacon_position=(10, 0),
    ))

    intents = coordinate_expedition_intents(
        context,
        AgentMemory(),
        AgentConfig(),
        expedition(vanguard, ranger),
        (),
    )
    by_actor = {item.actor_id: item for item in intents}

    for member in (vanguard, ranger):
        assert by_actor[member.id].action.value == "MOVE"
        assert by_actor[member.id].reason == "expedition_formation_evasion"
        assert by_actor[member.id].reserved_cell != (1, 0)


def test_campaign_contact_holds_non_engaged_members_without_replacing_fire():
    ranger = unit(10, UnitType.RANGER, (0, 0))
    vanguard = unit(11, UnitType.VANGUARD, (0, 1))
    enemy = unit(90, UnitType.RANGER, (3, 0), controlled=False)
    result = campaign_runtime().decide(
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


def test_campaign_detaches_retreating_ranger_without_holding_healthy_escort():
    ranger = unit(10, UnitType.RANGER, (2, 0), hp=1)
    vanguard = unit(11, UnitType.VANGUARD, (6, 0))
    result = campaign_runtime().decide(
        turn(
            owned_core=core(position=(0, 0)),
            units=(ranger, vanguard),
            beacon_position=(10, 0),
        )
    )

    intents = {item.actor_id: item for item in result.intents}
    assert intents[ranger.id].reason == "critical_ranger_retreat"
    assert intents[vanguard.id].action.value == "MOVE"
    assert intents[vanguard.id].reason == "expedition_formation_move"


def test_campaign_detaches_retreat_blocked_ranger_without_holding_healthy_escort():
    ranger = unit(10, UnitType.RANGER, (2, 0), hp=1)
    vanguard = unit(11, UnitType.VANGUARD, (6, 0))
    result = campaign_runtime().decide(
        turn(
            owned_core=core(position=(0, 0)),
            units=(ranger, vanguard),
            obstacle_cells=((1, 0), (2, -1), (2, 1), (3, 0)),
            beacon_position=(10, 0),
        )
    )

    intents = {item.actor_id: item for item in result.intents}
    assert intents[ranger.id].reason == "critical_retreat_blocked"
    assert intents[vanguard.id].action.value == "MOVE"
    assert intents[vanguard.id].reason == "expedition_formation_move"


def test_campaign_distant_critical_ranger_uses_local_fallback_when_core_route_is_blocked():
    ranger = unit(10, UnitType.RANGER, (51, 0), hp=1)
    vanguard = unit(11, UnitType.VANGUARD, (101, 0))
    wall = tuple((50, y) for y in range(-12, 13))

    result = campaign_runtime().decide(
        turn(
            owned_core=core(position=(0, 0)),
            units=(ranger, vanguard),
            obstacle_cells=wall,
            beacon_position=(200, 0),
        )
    )

    intents = {item.actor_id: item for item in result.intents}
    assert intents[ranger.id].action.value == "MOVE"
    assert intents[ranger.id].reason == "critical_ranger_retreat_distant_fallback"
    assert intents[ranger.id].reserved_cell not in wall
    assert intents[vanguard.id].action.value == "MOVE"


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


def test_campaign_does_not_acquire_beacon_outside_global_beacon_mode():
    vanguard = unit(20, UnitType.VANGUARD, (0, 0))
    ranger = unit(21, UnitType.RANGER, (0, 1))
    runtime = AgentRuntime(config=AgentConfig(
        beacon_campaign_v1=True,
        core_guard_vanguards=0,
        core_guard_rangers=0,
        expedition_vanguards=1,
        expedition_rangers=1,
    ))

    result = runtime.decide(turn(
        owned_core=core(),
        units=(vanguard, ranger),
        beacon_position=(8, 0),
    ))

    assert result.mode is StrategicMode.ECONOMY
    assert all(
        not intent.reason.startswith("expedition_")
        for intent in result.intents
    )


def test_carried_beacon_exfils_with_escort_toward_core():
    carrier = unit(30, UnitType.VANGUARD, (8, 0))
    escort = unit(31, UnitType.RANGER, (7, 0))
    runtime = campaign_runtime()
    runtime.memory.objective_states["beacon"] = {
        "stage": BeaconStage.PICKUP.value,
        "escort_aliases": [entity_alias(carrier.id), entity_alias(escort.id)],
    }

    result = runtime.decide(turn(
        owned_core=core(position=(0, 0)),
        units=(carrier, escort),
        beacon_position=carrier.position,
        beacon_status=BeaconStatus.CARRIED,
        beacon_carrier_id=carrier.id,
    ))
    intents = {item.actor_id: item for item in result.intents}

    assert result.mode is StrategicMode.BEACON
    assert intents[carrier.id].action.value == "MOVE"
    assert intents[carrier.id].reason == "beacon_exfil_formation_move"
    assert intents[carrier.id].reserved_cell == (7, 0)
    assert intents[escort.id].reason.startswith("beacon_exfil_")


def test_exfil_carrier_keeps_withdrawing_while_ranger_covers_contact():
    carrier = unit(40, UnitType.VANGUARD, (8, 0))
    escort = unit(41, UnitType.RANGER, (7, 1))
    enemy = unit(90, UnitType.RANGER, (10, 1), controlled=False)
    runtime = campaign_runtime()
    runtime.memory.objective_states["beacon"] = {
        "stage": BeaconStage.EXFIL.value,
        "carrier_alias": entity_alias(carrier.id),
        "escort_aliases": [entity_alias(carrier.id), entity_alias(escort.id)],
    }

    result = runtime.decide(turn(
        owned_core=core(position=(0, 0)),
        units=(carrier, escort),
        enemies=(enemy,),
        beacon_position=carrier.position,
        beacon_status=BeaconStatus.CARRIED,
        beacon_carrier_id=carrier.id,
    ))
    intents = {item.actor_id: item for item in result.intents}

    assert intents[escort.id].action.value == "SHOOT"
    assert intents[carrier.id].action.value == "MOVE"
    assert intents[carrier.id].reason.startswith("beacon_exfil_")


def test_secured_carrier_stays_home_and_former_escort_joins_miners():
    carrier = unit(50, UnitType.VANGUARD, (1, 0))
    escort = unit(51, UnitType.RANGER, (2, 0))
    worker = unit(52, UnitType.WORKER, (5, 0))
    memory = AgentMemory(
        last_mode=StrategicMode.ECONOMY,
        objective_states={"beacon": {
            "stage": BeaconStage.SECURE.value,
            "carrier_alias": entity_alias(carrier.id),
            "escort_aliases": [entity_alias(carrier.id), entity_alias(escort.id)],
        }},
    )
    context = DecisionContext.from_turn(turn(
        owned_core=core(),
        units=(carrier, escort, worker),
        beacon_position=carrier.position,
        beacon_status=BeaconStatus.CARRIED,
        beacon_carrier_id=carrier.id,
    ))
    plan = build_squad_plan(context, memory, AgentConfig(
        core_guard_vanguards=0,
        core_guard_rangers=0,
        mining_escort_vanguards=0,
        mining_escort_rangers=0,
        scout_vanguards=0,
        scout_rangers=0,
    ))

    assert plan.unit_is(carrier.id, SquadType.BASE_DEFENSE)
    assert plan.unit_is(worker.id, SquadType.MINING_ESCORT)
    assert plan.unit_is(escort.id, SquadType.MINING_ESCORT)


def test_secured_beacon_repairs_core_only_from_surplus_resources():
    carrier = unit(60, UnitType.VANGUARD, (1, 0))
    escort = unit(61, UnitType.RANGER, (2, 0))
    runtime = campaign_runtime(minimum_resource_reserve=5)
    runtime.memory.objective_states["beacon"] = {
        "stage": BeaconStage.SECURE.value,
        "carrier_alias": entity_alias(carrier.id),
        "escort_aliases": [entity_alias(carrier.id), entity_alias(escort.id)],
    }

    result = runtime.decide(turn(
        owned_core=core(shield=5),
        units=(carrier, escort),
        resources=6,
        resource_cells=((4, 0),),
        beacon_position=carrier.position,
        beacon_status=BeaconStatus.CARRIED,
        beacon_carrier_id=carrier.id,
    ))
    core_intent = next(item for item in result.intents if item.is_core)

    assert core_intent.action.value == "REPAIR_SHIELD"
    assert core_intent.reason == "beacon_secure_core_repair"


def test_global_defense_preempts_beacon_exfil_overlay():
    carrier = unit(70, UnitType.VANGUARD, (8, 0))
    escort = unit(71, UnitType.RANGER, (7, 0))
    enemy = unit(99, UnitType.VANGUARD, (1, 0), controlled=False)
    runtime = campaign_runtime()
    runtime.memory.objective_states["beacon"] = {
        "stage": BeaconStage.EXFIL.value,
        "carrier_alias": entity_alias(carrier.id),
        "escort_aliases": [entity_alias(carrier.id), entity_alias(escort.id)],
    }

    result = runtime.decide(turn(
        owned_core=core(),
        units=(carrier, escort),
        enemies=(enemy,),
        beacon_position=carrier.position,
        beacon_status=BeaconStatus.CARRIED,
        beacon_carrier_id=carrier.id,
    ))

    assert result.mode is StrategicMode.DEFEND
    assert all(
        not intent.reason.startswith("beacon_exfil_")
        for intent in result.intents
    )


def test_beacon_escort_roster_survives_memory_round_trip():
    first = unit(80, UnitType.VANGUARD, (3, 0))
    second = unit(81, UnitType.RANGER, (4, 0))
    aliases = [entity_alias(first.id), entity_alias(second.id)]
    memory = AgentMemory(objective_states={"beacon": {
        "stage": BeaconStage.EXFIL.value,
        "carrier_alias": aliases[0],
        "escort_aliases": aliases,
    }})

    restored = AgentMemory.from_dict(memory.to_dict())
    assert restored.objective_states["beacon"]["carrier_alias"] == aliases[0]
    assert restored.objective_states["beacon"]["escort_aliases"] == aliases


def test_campaign_cohesion_hold_timeout_breaks_deadlock_and_advances():
    front = unit(10, UnitType.VANGUARD, (8, 0))
    pace = unit(11, UnitType.RANGER, (0, 0))
    runtime = campaign_runtime()

    # 前 10 个 tick 应该持续 hold
    for i in range(10):
        game_turn = turn(
            owned_core=core(position=(0, 0)),
            units=(front, pace),
            beacon_position=(20, 0),
            tick=i + 1,
        )
        result = runtime.decide(game_turn)
        runtime.commit(result)
        front_intent = next(item for item in result.intents if item.actor_id == front.id)
        assert front_intent.action.value == "WAIT"
        assert front_intent.reason == "expedition_cohesion_hold"

    # 第 11 个 tick 超时触发，front 强制打破死锁向前/向编队槽位移动
    game_turn = turn(
        owned_core=core(position=(0, 0)),
        units=(front, pace),
        beacon_position=(20, 0),
        tick=11,
    )
    result = runtime.decide(game_turn)
    front_intent = next(item for item in result.intents if item.actor_id == front.id)
    assert front_intent.action.value == "MOVE"
    assert front_intent.reason.startswith("expedition_")
