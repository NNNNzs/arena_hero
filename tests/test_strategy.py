from arena_hero import BeaconStatus, CoreState, CoreView, Direction, UnitType, unit_cost

from arena_tactic import (
    AgentConfig,
    AgentMemory,
    StrategicMode,
    choose_actions,
    ranger_target_score,
)
from arena_tactic.context import DecisionContext
from arena_tactic.models import ActionKind
from arena_tactic.strategy import choose_mode

from .factories import core, event, turn, unit, uuid


def _early_roster():
    return (
        unit(1, UnitType.WORKER, (-1, 0)),
        unit(2, UnitType.WORKER, (-2, 0)),
        unit(3, UnitType.WORKER, (-3, 0)),
        unit(4, UnitType.VANGUARD, (0, 1)),
        unit(5, UnitType.VANGUARD, (0, 2)),
        unit(6, UnitType.RANGER, (1, 0)),
    )


def test_modes_cover_respawn_recover_defend_attack_beacon_economy_and_explore():
    memory = AgentMemory()
    assert choose_actions(turn(owned_core=None)).mode is StrategicMode.RESPAWN
    assert choose_actions(turn(owned_core=core(hp=4), resources=2)).mode is StrategicMode.RECOVER

    nearby_enemy = unit(200, UnitType.WORKER, (3, 0), controlled=False)
    assert choose_actions(turn(owned_core=core(), enemies=(nearby_enemy,))).mode is StrategicMode.DEFEND

    enemy_core = core(value=300, position=(5, 0), controlled=False)
    combat = (
        unit(10, UnitType.VANGUARD, (0, 1)),
        unit(11, UnitType.VANGUARD, (1, 0)),
        unit(12, UnitType.RANGER, (0, 2)),
    )
    assert choose_actions(turn(owned_core=core(), units=combat, enemies=(enemy_core,))).mode is StrategicMode.ATTACK

    roster = _early_roster()
    assert choose_actions(turn(owned_core=core(), units=roster)).mode is StrategicMode.BEACON
    assert choose_actions(turn(owned_core=core(), resource_cells=((5, 0),))).mode is StrategicMode.ECONOMY
    carried = uuid(999)
    assert choose_actions(
        turn(
            owned_core=core(),
            units=roster,
            beacon_status=BeaconStatus.CARRIED,
            beacon_carrier_id=carried,
        )
    ).mode is StrategicMode.EXPLORE


def test_defense_mode_uses_exit_hysteresis():
    memory = AgentMemory(last_mode=StrategicMode.DEFEND)
    enemy = unit(200, UnitType.WORKER, (5, 0), controlled=False)
    context = DecisionContext.from_turn(turn(owned_core=core(), enemies=(enemy,)))
    assert choose_mode(context, memory, AgentConfig()) is StrategicMode.DEFEND


def test_hidden_repeated_core_damage_searches_instead_of_holding():
    fighters = (
        unit(10, UnitType.VANGUARD, (-1, 0)),
        unit(11, UnitType.RANGER, (1, 0)),
    )
    memory = AgentMemory(
        last_tick=9, core_damage_streak=1, last_core_damage_tick=9
    )
    result = choose_actions(
        turn(
            tick=10,
            owned_core=core(),
            units=fighters,
            events=(event(500, "CORE_DAMAGED", tick=9),),
        ),
        memory=memory,
    )
    assert result.mode is StrategicMode.DEFEND
    combat = [intent for intent in result.intents if intent.actor_id in {item.id for item in fighters}]
    assert all(intent.reason == "search_hidden_core_attacker" for intent in combat)
    assert all(intent.target_id is None for intent in combat)


def test_sustained_hidden_damage_can_migrate_but_visible_threat_cannot():
    pressure = AgentMemory(last_tick=19, core_damage_streak=2, last_core_damage_tick=19)
    hidden = choose_actions(
        turn(tick=20, owned_core=core(hp=3), events=(event(501, "CORE_DAMAGED", tick=19),)),
        memory=pressure,
    )
    hidden_core = next(intent for intent in hidden.intents if intent.is_core)
    assert hidden_core.action is ActionKind.START_MOVE
    assert hidden_core.reason == "escape_sustained_hidden_fire"

    attacker = unit(200, UnitType.VANGUARD, (1, 0), controlled=False)
    visible = choose_actions(
        turn(tick=20, owned_core=core(hp=3), enemies=(attacker,), events=(event(502, "CORE_DAMAGED", tick=19),)),
        memory=pressure,
    )
    assert next(intent for intent in visible.intents if intent.is_core).action is not ActionKind.START_MOVE


def test_defend_rallies_workers_when_combat_ready():
    combat_roster = (
        unit(1, UnitType.WORKER, (4, 0)),
        unit(2, UnitType.VANGUARD, (0, 1)),
        unit(3, UnitType.RANGER, (0, 2)),
    )
    attacker = unit(200, UnitType.VANGUARD, (1, 0), controlled=False)
    result = choose_actions(
        turn(owned_core=core(), units=combat_roster, enemies=(attacker,), resources=200)
    )
    worker_intent = next(intent for intent in result.intents if intent.actor_id == uuid(1))
    core_intent = next(intent for intent in result.intents if intent.is_core)
    assert worker_intent.reason == "emergency_worker_rally_to_core"
    assert core_intent.action is not ActionKind.SPAWN


def test_defend_empty_workers_keep_harvesting_and_core_spawns_vanguard_when_not_combat_ready():
    worker = unit(1, UnitType.WORKER, (4, 0))
    attacker = unit(200, UnitType.VANGUARD, (1, 0), controlled=False)
    result = choose_actions(
        turn(owned_core=core(), units=(worker,), enemies=(attacker,), resources=200)
    )
    worker_intent = next(intent for intent in result.intents if intent.actor_id == uuid(1))
    core_intent = next(intent for intent in result.intents if intent.is_core)
    assert worker_intent.reason == "explore_sector_frontier"
    assert core_intent.action is ActionKind.SPAWN
    assert core_intent.unit_type is UnitType.VANGUARD


def test_defend_vanguard_intercepts_ranger_threatening_core():
    """A core guard must close with a Ranger instead of holding its ring."""
    vanguard = unit(10, UnitType.VANGUARD, (0, 1))
    attacker = unit(200, UnitType.RANGER, (3, 0), controlled=False)

    result = choose_actions(
        turn(owned_core=core(), units=(vanguard,), enemies=(attacker,))
    )

    assert result.mode is StrategicMode.DEFEND
    intent = next(item for item in result.intents if item.actor_id == vanguard.id)
    assert intent.action is ActionKind.MOVE
    assert intent.reason == "intercept_visible_threat"
    assert intent.reason != "holding_defense_ring"


def test_defend_ranger_moves_into_firing_position_against_visible_threat():
    """DEFEND must not prevent a Ranger from seeking a legal firing line."""
    ranger = unit(11, UnitType.RANGER, (0, 1))
    attacker = unit(201, UnitType.RANGER, (3, 0), controlled=False)

    result = choose_actions(
        turn(
            owned_core=core(),
            units=(ranger,),
            enemies=(attacker,),
        )
    )

    assert result.mode is StrategicMode.DEFEND
    intent = next(item for item in result.intents if item.actor_id == ranger.id)
    assert intent.action is ActionKind.MOVE
    assert intent.reason in {
        "intercept_ranger_firing_line",
        "ranger_seek_legal_firing_line",
    }
    assert intent.reason != "holding_defense_ring"


def test_insufficient_resources_and_respawn_clear_hidden_pressure():
    pressure = AgentMemory(last_tick=29, core_damage_streak=2, last_core_damage_tick=29)
    damaged = choose_actions(
        turn(tick=30, owned_core=core(hp=2), resources=0, events=(event(503, "CORE_DAMAGED", tick=29),)),
        memory=pressure,
    )
    core_intent = next(intent for intent in damaged.intents if intent.is_core)
    assert core_intent.action is ActionKind.HEAL
    assert core_intent.estimated_cost == 0

    respawned = choose_actions(
        turn(tick=31, owned_core=core(value=101), events=(event(504, "CORE_RESPAWNED", tick=30),)),
        memory=damaged.next_memory,
    )
    assert respawned.next_memory.core_damage_streak == 0
    assert respawned.next_memory.last_core_damage_tick == 0
    assert all(intent.reason != "search_hidden_core_attacker" for intent in respawned.intents)


def test_attack_mode_survives_one_tick_of_lost_enemy_core_visibility():
    enemy_core = core(value=300, position=(5, 0), controlled=False)
    combat = (
        unit(10, UnitType.VANGUARD, (0, 1)),
        unit(11, UnitType.VANGUARD, (1, 0)),
        unit(12, UnitType.RANGER, (0, 2)),
    )
    config = AgentConfig(attack_exit_grace_ticks=4)
    attack_context = DecisionContext.from_turn(
        turn(tick=10, owned_core=core(), units=combat, enemies=(enemy_core,))
    )
    assert choose_mode(attack_context, AgentMemory(), config) is StrategicMode.ATTACK

    lost_visibility = DecisionContext.from_turn(
        turn(tick=11, owned_core=core(), units=combat, enemies=())
    )
    memory = AgentMemory(last_mode=StrategicMode.ATTACK, mode_since_tick=10)
    assert choose_mode(lost_visibility, memory, config) is StrategicMode.ATTACK


def test_worker_resource_assignments_are_unique():
    workers = (
        unit(1, UnitType.WORKER, (0, 1)),
        unit(2, UnitType.WORKER, (0, 2)),
    )
    result = choose_actions(
        turn(
            owned_core=core(position=(-3, 0)),
            units=workers,
            resource_cells=((3, 1), (3, 2)),
        )
    )
    targets = [
        intent.target_cell
        for intent in result.intents
        if intent.actor_id in {worker.id for worker in workers}
        and intent.action is ActionKind.MOVE
    ]
    assert len(targets) == 2
    assert len(set(targets)) == 2


def test_remembered_invisible_resource_is_only_a_reconnaissance_target():
    worker = unit(1, UnitType.WORKER, (0, 0))
    memory = AgentMemory(resource_observations={(10, 0): 1})
    result = choose_actions(
        turn(owned_core=core(position=(-2, 0)), units=(worker,), tick=2),
        memory=memory,
    )
    intent = next(intent for intent in result.intents if intent.actor_id == worker.id)
    assert intent.action is ActionKind.MOVE
    assert intent.target_cell == (10, 0)
    assert intent.reason == "reobserve_remembered_resource"


def test_workers_keep_exploring_when_frontier_is_exhausted_and_no_resource_is_visible():
    worker = unit(1, UnitType.WORKER, (0, 0))
    memory = AgentMemory(explored={(x, y) for x in range(-2, 3) for y in range(-2, 3)})
    result = choose_actions(
        turn(owned_core=core(position=(-3, 0)), units=(worker,)),
        memory=memory,
    )
    intent = next(intent for intent in result.intents if intent.actor_id == worker.id)
    assert intent.action is ActionKind.MOVE
    assert intent.reason == "explore_sector_frontier"


def test_no_visible_resource_keeps_only_one_worker_on_remembered_recheck():
    workers = tuple(
        unit(index, UnitType.WORKER, (0, index)) for index in range(1, 4)
    )
    memory = AgentMemory(
        last_tick=1,
        resource_observations={(10, 0): 1, (10, 1): 1, (10, 2): 1},
    )

    result = choose_actions(
        turn(tick=2, owned_core=core(position=(-5, 0)), units=workers),
        memory=memory,
    )
    worker_intents = [
        intent for intent in result.intents if intent.actor_id in {worker.id for worker in workers}
    ]

    assert sum(intent.reason == "reobserve_remembered_resource" for intent in worker_intents) == 1
    assert sum(intent.reason == "explore_sector_frontier" for intent in worker_intents) == 2


def test_visible_resource_clears_recheck_cooldown_and_is_harvestable_again():
    resource = (0, 0)
    worker = unit(1, UnitType.WORKER, resource)
    memory = AgentMemory(
        last_tick=10,
        resource_recheck_cooldowns={resource: 20},
    )

    result = choose_actions(
        turn(
            tick=11,
            owned_core=core(position=(-5, 0)),
            units=(worker,),
            resource_cells=(resource,),
        ),
        memory=memory,
    )
    intent = next(item for item in result.intents if item.actor_id == worker.id)

    assert intent.action is ActionKind.HARVEST
    assert result.next_memory.resource_observations[resource] == 11
    assert resource not in result.next_memory.resource_recheck_cooldowns
    assert intent.target_cell is not None


def test_default_combat_roster_keeps_guards_but_patrols_and_hunts_elsewhere():
    combat = (
        *(unit(index, UnitType.VANGUARD, (0, index)) for index in range(1, 5)),
        *(unit(index, UnitType.RANGER, (1, index)) for index in range(5, 8)),
    )
    result = choose_actions(turn(owned_core=core(), units=combat, beacon_status=BeaconStatus.CARRIED, beacon_carrier_id=uuid(999)))
    tasks = result.next_memory.unit_tasks

    kinds = [tasks[str(actor.id)]["kind"] for actor in combat]
    assert kinds.count("core_guard") == 3
    assert kinds.count("patrol") == 2
    assert kinds.count("hunter") == 2
    assert any(
        intent.reason == "patrol_outer_ring"
        for intent in result.intents
    )
    assert any(
        intent.reason == "hunter_forward_recon"
        for intent in result.intents
    )


def test_patrol_and_hunter_rosters_receive_distinct_sector_targets():
    combat = (
        *(unit(index, UnitType.VANGUARD, (0, index)) for index in range(1, 6)),
        *(unit(index, UnitType.RANGER, (1, index)) for index in range(6, 10)),
    )
    result = choose_actions(turn(tick=8, owned_core=core(), units=combat, beacon_status=BeaconStatus.CARRIED, beacon_carrier_id=uuid(999)))
    tasks = result.next_memory.unit_tasks
    patrol_targets = [tasks[str(actor.id)]["target"] for actor in combat if tasks[str(actor.id)]["kind"] == "patrol"]
    hunter_targets = [tasks[str(actor.id)]["target"] for actor in combat if tasks[str(actor.id)]["kind"] == "hunter"]

    assert len(patrol_targets) >= 2 and len({tuple(target) for target in patrol_targets}) == len(patrol_targets)
    assert len(hunter_targets) >= 2 and len({tuple(target) for target in hunter_targets}) == len(hunter_targets)


def test_approaching_enemy_forms_intercept_without_emptying_core_guards():
    combat = (
        *(unit(index, UnitType.VANGUARD, (0, index)) for index in range(1, 6)),
        *(unit(index, UnitType.RANGER, (1, index)) for index in range(6, 9)),
    )
    enemy_first = unit(200, UnitType.WORKER, (8, 0), controlled=False)
    first = choose_actions(turn(tick=10, owned_core=core(), units=combat, enemies=(enemy_first,)))
    enemy_nearer = unit(200, UnitType.WORKER, (7, 0), controlled=False)
    second = choose_actions(
        turn(tick=11, owned_core=core(), units=combat, enemies=(enemy_nearer,)),
        memory=first.next_memory,
    )
    tasks = second.next_memory.unit_tasks

    assert sum(task["kind"] == "intercept" for task in tasks.values()) == 3
    assert sum(task["kind"] == "core_guard" for task in tasks.values()) == 3
    assert second.mode is not StrategicMode.DEFEND


def test_enemy_departure_or_lost_visibility_does_not_leave_stale_intercept():
    combat = (
        *(unit(index, UnitType.VANGUARD, (0, index)) for index in range(1, 5)),
        *(unit(index, UnitType.RANGER, (1, index)) for index in range(5, 7)),
    )
    near = unit(200, UnitType.WORKER, (6, 0), controlled=False)
    first = choose_actions(turn(tick=10, owned_core=core(), units=combat, enemies=(near,)))
    farther = unit(200, UnitType.WORKER, (8, 0), controlled=False)
    departed = choose_actions(turn(tick=11, owned_core=core(), units=combat, enemies=(farther,)), memory=first.next_memory)
    lost = choose_actions(turn(tick=12, owned_core=core(), units=combat), memory=departed.next_memory)

    assert all(task["kind"] != "intercept" for task in departed.next_memory.unit_tasks.values())
    assert all(task["kind"] != "intercept" for task in lost.next_memory.unit_tasks.values())
    assert lost.mode is not StrategicMode.ATTACK


def test_respawn_discards_stale_resource_recheck_targets():
    worker = unit(1, UnitType.WORKER, (-412, 165))
    memory = AgentMemory(
        last_tick=100,
        resource_observations={(41, 2): 99},
        resource_recheck_failures={(41, 2): 1},
        resource_recheck_cooldowns={(80, 3): 120},
    )
    result = choose_actions(
        turn(
            tick=101,
            owned_core=core(value=101, position=(-412, 165)),
            units=(worker,),
            events=(event(900, "CORE_RESPAWNED", tick=100),),
        ),
        memory=memory,
    )
    intent = next(item for item in result.intents if item.actor_id == worker.id)
    assert result.next_memory.resource_observations == {}
    assert result.next_memory.resource_recheck_failures == {}
    assert result.next_memory.resource_recheck_cooldowns == {}
    assert intent.reason != "reobserve_remembered_resource"


    fighter = unit(1, UnitType.VANGUARD, (1, 0))
    enemy = unit(200, UnitType.WORKER, (6, 0), controlled=False)
    observed = choose_actions(turn(tick=10, owned_core=core(), units=(fighter,), enemies=(enemy,)))
    assert observed.next_memory.enemy_tracks

    missing = choose_actions(turn(tick=11, owned_core=None), memory=observed.next_memory)
    assert missing.next_memory.enemy_tracks == {}
    assert missing.next_memory.unit_tasks == {}

    reborn = choose_actions(turn(tick=12, owned_core=core(value=101), units=(fighter,)), memory=missing.next_memory)
    assert reborn.next_memory.enemy_tracks == {}
    assert all(task["kind"] != "intercept" for task in reborn.next_memory.unit_tasks.values())

    event_reborn = choose_actions(
        turn(tick=13, owned_core=core(value=102), units=(fighter,), events=(event(301, "CORE_RESPAWNED", tick=12),)),
        memory=observed.next_memory,
    )
    assert event_reborn.next_memory.enemy_tracks == {}
    assert all(task["kind"] != "intercept" for task in event_reborn.next_memory.unit_tasks.values())


def test_worker_waits_instead_of_stepping_away_from_blocked_adjacent_resource():
    worker = unit(1, UnitType.WORKER, (0, 0))
    guards = (
        unit(2, UnitType.VANGUARD, (0, 1)),
        unit(3, UnitType.VANGUARD, (0, 1)),
    )
    result = choose_actions(
        turn(
            owned_core=core(position=(-2, 0)),
            units=(worker, *guards),
            resource_cells=((0, 1),),
        )
    )
    intent = next(intent for intent in result.intents if intent.actor_id == worker.id)
    assert intent.action is ActionKind.WAIT
    assert intent.reason == "resource_route_blocked"


def test_cargo_worker_waits_at_moving_core_destination():
    moving_core = core(position=(0, 0), state=CoreState.MOVING)
    worker = unit(1, UnitType.WORKER, (1, 0), cargo=1)
    result = choose_actions(turn(owned_core=moving_core, units=(worker,)))
    intent = next(intent for intent in result.intents if intent.actor_id == worker.id)
    assert intent.action is ActionKind.WAIT
    assert result.mode is StrategicMode.RECOVER


def test_damaged_worker_heals_before_leaving_core():
    worker = unit(1, UnitType.WORKER, (0, 0), hp=1)
    result = choose_actions(
        turn(owned_core=core(), units=(worker,), resources=1, resource_cells=((2, 0),))
    )
    intent = next(intent for intent in result.intents if intent.actor_id == worker.id)
    assert intent.action is ActionKind.HEAL


def test_ranger_scores_and_shoots_visible_enemy_core_first():
    ranger = unit(1, UnitType.RANGER, (0, 0))
    enemy_unit = unit(201, UnitType.WORKER, (1, 0), controlled=False)
    enemy_core = core(value=202, position=(3, 0), shield=0, controlled=False)
    game_turn = turn(
        owned_core=core(position=(-5, 0)),
        units=(ranger,),
        enemies=(enemy_unit, enemy_core),
    )
    context = DecisionContext.from_turn(game_turn)
    memory = AgentMemory()
    assert ranger_target_score(ranger, enemy_core, context, memory) > ranger_target_score(
        ranger, enemy_unit, context, memory
    )
    choose_actions(game_turn)
    assert game_turn.plan.unit_actions[ranger.id].type == "SHOOT"
    assert game_turn.plan.unit_actions[ranger.id].target_id == enemy_core.id


def test_ranger_equal_score_uses_lowest_raw_uuid():
    ranger = unit(1, UnitType.RANGER, (0, 0))
    lower = unit(201, UnitType.WORKER, (2, 0), controlled=False)
    higher = unit(202, UnitType.WORKER, (0, 2), controlled=False)
    game_turn = turn(
        owned_core=core(position=(-5, 0)),
        units=(ranger,),
        enemies=(higher, lower),
    )
    choose_actions(game_turn)
    assert game_turn.plan.unit_actions[ranger.id].target_id == lower.id


def test_ranger_ignores_non_ray_target_and_hp_one_retreats():
    ranger = unit(1, UnitType.RANGER, (2, 0), hp=1)
    enemy = unit(201, UnitType.WORKER, (4, 1), controlled=False)
    game_turn = turn(owned_core=core(), units=(ranger,), enemies=(enemy,), resources=1)
    choose_actions(game_turn)
    assert game_turn.plan.unit_actions[ranger.id].type == "MOVE"
    assert game_turn.plan.unit_actions[ranger.id].direction is Direction.LEFT


def test_adjacent_vanguard_attacks_core_cell_before_healing():
    vanguard = unit(1, UnitType.VANGUARD, (0, 0), hp=1)
    enemy_core = core(value=201, position=(1, 0), controlled=False)
    enemy_unit = unit(202, UnitType.WORKER, (1, 0), controlled=False)
    game_turn = turn(
        owned_core=core(position=(-3, 0)),
        units=(vanguard,),
        enemies=(enemy_core, enemy_unit),
        resources=1,
    )
    choose_actions(game_turn)
    action = game_turn.plan.unit_actions[vanguard.id]
    assert action.type == "SWEEP"
    assert action.direction is Direction.RIGHT


def test_beacon_prefers_vanguard_and_never_drops_it():
    roster = _early_roster()
    beacon_vanguard = roster[3]
    game_turn = turn(
        owned_core=core(),
        units=roster,
        beacon_position=beacon_vanguard.position,
        beacon_status=BeaconStatus.GROUND,
    )
    result = choose_actions(game_turn)
    intent = next(
        intent for intent in result.intents if intent.actor_id == beacon_vanguard.id
    )
    assert intent.action is ActionKind.PICKUP_BEACON
    assert all(intent.action.value != "DROP_BEACON" for intent in result.intents)


def test_beacon_ownership_enables_repair_toward_ten_shield_cap():
    carrier = unit(1, UnitType.VANGUARD, (1, 0))
    game_turn = turn(
        owned_core=core(shield=5),
        units=(carrier,),
        resources=6,
        beacon_status=BeaconStatus.CARRIED,
        beacon_carrier_id=carrier.id,
    )
    choose_actions(game_turn)
    assert game_turn.plan.core_action.type == "REPAIR_SHIELD"


def test_core_spawn_order_reserve_and_population_cap():
    # Before combat units (1 Vanguard + 1 Ranger), 0 reserve: 5 resources can spawn worker
    spawn_turn = turn(owned_core=core(), resources=5)
    choose_actions(spawn_turn)
    assert spawn_turn.plan.core_action.type == "SPAWN"
    assert spawn_turn.plan.core_action.unit_type is UnitType.WORKER

    # 4 resources cannot afford 5-resource worker
    underfunded_turn = turn(owned_core=core(), resources=4)
    choose_actions(underfunded_turn)
    assert underfunded_turn.plan.core_action.type == "WAIT"

    # After 1 Vanguard + 1 Ranger are present, 5-resource reserve kicks in
    combat_units = (
        unit(1, UnitType.WORKER, (1, 0)),
        unit(2, UnitType.VANGUARD, (0, 1)),
        unit(3, UnitType.RANGER, (0, 2)),
    )
    # Worker cost is 5, reserve is 5 -> total 10 needed. 9 resources will WAIT
    combat_ready_reserved_turn = turn(owned_core=core(), units=combat_units, resources=9)
    choose_actions(combat_ready_reserved_turn)
    assert combat_ready_reserved_turn.plan.core_action.type == "WAIT"

    forty = tuple(
        unit(index + 1, (UnitType.WORKER, UnitType.VANGUARD, UnitType.RANGER)[index % 3], (index + 1, 2))
        for index in range(40)
    )
    capped_turn = turn(owned_core=core(), units=forty, resources=200)
    choose_actions(capped_turn)
    assert capped_turn.plan.core_action.type != "SPAWN"


def test_healthy_core_spends_exact_vanguard_cost_to_finish_early_roster():
    early_workers = tuple(
        unit(index, UnitType.WORKER, (index, 1)) for index in range(1, 4)
    )
    game_turn = turn(
        owned_core=core(hp=5, shield=5),
        units=early_workers,
        resources=10,
    )

    choose_actions(game_turn)

    assert game_turn.plan.core_action.type == "SPAWN"
    assert game_turn.plan.core_action.unit_type is UnitType.VANGUARD


def test_production_uses_dynamic_sdk_price_when_cap_is_explicitly_raised():
    twenty_vanguards = tuple(
        unit(index + 1, UnitType.VANGUARD, (index + 1, 3))
        for index in range(20)
    )
    price = unit_cost(UnitType.WORKER, 20)
    game_turn = turn(
        owned_core=core(),
        units=twenty_vanguards,
        resources=price + 5,
    )
    result = choose_actions(
        game_turn,
        config=AgentConfig(max_population=21),
    )
    core_intent = next(intent for intent in result.intents if intent.is_core)
    assert core_intent.action is ActionKind.SPAWN
    assert core_intent.estimated_cost == price


def test_vanguard_heal_budget_accounts_for_all_missing_hp():
    vanguard = unit(1, UnitType.VANGUARD, (0, 0), hp=1)
    result = choose_actions(
        turn(owned_core=core(), units=(vanguard,), resources=3)
    )
    intent = next(intent for intent in result.intents if intent.actor_id == vanguard.id)
    assert intent.action is ActionKind.HEAL
    assert intent.estimated_cost == 3


def test_unit_heal_budget_follows_raw_uuid_resolution_order():
    lower_vanguard = unit(1, UnitType.VANGUARD, (0, 0), hp=1)
    higher_worker = unit(2, UnitType.WORKER, (0, 0), hp=1)
    result = choose_actions(
        turn(
            owned_core=core(),
            units=(higher_worker, lower_vanguard),
            resources=3,
        )
    )
    by_actor = {intent.actor_id: intent for intent in result.intents}
    assert by_actor[lower_vanguard.id].action is ActionKind.HEAL
    assert by_actor[lower_vanguard.id].estimated_cost == 3
    assert by_actor[higher_worker.id].action is ActionKind.WAIT


def test_same_tick_worker_deposit_funds_later_core_healing():
    worker = unit(1, UnitType.WORKER, (0, 0), cargo=2)
    result = choose_actions(
        turn(owned_core=core(hp=3), units=(worker,), resources=0)
    )
    by_actor = {intent.actor_id: intent for intent in result.intents}
    assert by_actor[worker.id].action is ActionKind.DEPOSIT
    core_intent = next(intent for intent in result.intents if intent.is_core)
    assert core_intent.action is ActionKind.HEAL
    assert core_intent.estimated_cost == 2


def test_core_migrates_only_after_eight_safe_resource_empty_ticks():
    memory = AgentMemory(
        last_tick=8,
        no_resource_ticks=8,
        explored={(0, 0), (1, 0), (-1, 0), (0, 1), (0, -1)},
    )
    game_turn = turn(tick=9, owned_core=core(), resources=0)
    result = choose_actions(game_turn, memory=memory)
    core_intent = next(intent for intent in result.intents if intent.is_core)
    assert core_intent.action is ActionKind.START_MOVE


def test_distant_enemy_core_does_not_block_safe_resource_drought_migration():
    memory = AgentMemory(
        last_tick=8,
        no_resource_ticks=8,
        explored={(0, 0), (1, 0), (2, 0), (0, 1), (0, -1), (-1, 0)},
    )
    distant_enemy_core = core(value=300, position=(4, 0), controlled=False)
    result = choose_actions(
        turn(tick=9, owned_core=core(), enemies=(distant_enemy_core,)),
        memory=memory,
    )

    core_intent = next(intent for intent in result.intents if intent.is_core)
    assert core_intent.action is ActionKind.START_MOVE
    assert core_intent.reserved_cell != distant_enemy_core.position


def test_successful_core_migration_resets_exploration_and_starts_cooldown():
    memory = AgentMemory(
        last_tick=8,
        no_resource_ticks=40,
        last_core_position=(0, 0),
        explored={(0, 0), (1, 0), (2, 0), (1, 1), (1, -1)},
    )
    game_turn = turn(
        tick=9,
        owned_core=core(position=(1, 0)),
        events=(event(901, "CORE_MOVE_SUCCEEDED", tick=8, position=(1, 0)),),
    )

    result = choose_actions(game_turn, memory=memory)

    core_intent = next(intent for intent in result.intents if intent.is_core)
    assert core_intent.action is not ActionKind.START_MOVE
    assert result.next_memory.no_resource_ticks == 0
    assert result.next_memory.migration_cooldown_until_tick == 17
    assert result.next_memory.previous_migration_position == (0, 0)


def test_exploration_migration_avoids_immediate_reverse_when_other_safe_leg_exists():
    memory = AgentMemory(
        last_tick=20,
        no_resource_ticks=8,
        previous_migration_position=(0, 0),
        explored={(0, 0), (1, 0), (2, 0), (1, 1), (1, -1), (3, 0)},
    )
    result = choose_actions(
        turn(tick=21, owned_core=core(position=(1, 0))), memory=memory
    )
    core_intent = next(intent for intent in result.intents if intent.is_core)
    assert core_intent.action is ActionKind.START_MOVE
    assert core_intent.reserved_cell != (0, 0)


def test_worker_cargo_defers_ordinary_exploration_migration():
    worker = unit(1, UnitType.WORKER, (2, 0), cargo=1)
    memory = AgentMemory(
        last_tick=8,
        no_resource_ticks=8,
        explored={(0, 0), (1, 0), (-1, 0), (0, 1), (0, -1)},
    )
    result = choose_actions(
        turn(tick=9, owned_core=core(), units=(worker,)), memory=memory
    )
    core_intent = next(intent for intent in result.intents if intent.is_core)
    assert core_intent.action is not ActionKind.START_MOVE


def test_deposit_core_moving_failure_waits_until_core_is_stationary():
    worker = unit(1, UnitType.WORKER, (0, 0), cargo=1)
    result = choose_actions(
        turn(
            tick=2,
            owned_core=core(state=CoreState.MOVING),
            units=(worker,),
            events=(event(902, "DEPOSIT_FAILED", tick=1,
                          reason_code="CORE_MOVING", actor_id=worker.id),),
        )
    )
    worker_intent = next(intent for intent in result.intents if intent.actor_id == worker.id)
    assert worker_intent.action is ActionKind.WAIT
    assert worker_intent.reason == "deposit_waits_for_core_migration"


def test_missing_core_never_invents_an_action():
    unexpected_worker = unit(1, UnitType.WORKER, (0, 0))
    game_turn = turn(owned_core=None, units=(unexpected_worker,))
    result = choose_actions(game_turn)
    assert result.intents == ()
    assert game_turn.plan.core_action is None
    assert game_turn.plan.unit_actions == {}


# --- Population-40 / resource-capacity-200 / wartime-production tests ---


def _make_mature_roster():
    """12 Workers + 12 Vanguards + 16 Rangers = 40 units."""
    units = []
    idx = 1
    for _ in range(12):
        units.append(unit(idx, UnitType.WORKER, (idx, 0)))
        idx += 1
    for _ in range(12):
        units.append(unit(idx, UnitType.VANGUARD, (idx, 1)))
        idx += 1
    for _ in range(16):
        units.append(unit(idx, UnitType.RANGER, (idx, 2)))
        idx += 1
    return tuple(units)


def test_population_40_stops_production_when_mature_roster_filled():
    """At pop=40 (12W/12V/16R), resource_capacity=200 — no spawn."""
    roster = _make_mature_roster()
    assert len(roster) == 40
    game_turn = turn(owned_core=core(), units=roster, resources=200)
    result = choose_actions(game_turn)
    core_intent = next(intent for intent in result.intents if intent.is_core)
    assert core_intent.action is not ActionKind.SPAWN
    # Verify resource_capacity comes from Turn, not hardcoded
    assert game_turn.resource_capacity == 200


def test_resource_capacity_200_derives_from_population_40():
    """resource_capacity = max(10, population*5) → 200 at pop 40."""
    roster = _make_mature_roster()
    game_turn = turn(owned_core=core(), units=roster, resources=100)
    assert game_turn.resource_capacity == 200
    assert game_turn.resource_space == 100  # capacity 200 - resources 100


def test_mature_roster_gap_fills_largest_deficit():
    """When 2 Vanguards and 4 Rangers are missing, Ranger is produced."""
    roster = (
        *(unit(i, UnitType.WORKER, (i, 0)) for i in range(1, 13)),      # 12 Workers (full)
        *(unit(i, UnitType.VANGUARD, (i, 1)) for i in range(13, 23)),    # 10 Vanguards (gap=2)
        *(unit(i, UnitType.RANGER, (i, 2)) for i in range(23, 33)),     # 12 Rangers (gap=4)
    )
    # 34 units total, pop < 40
    game_turn = turn(owned_core=core(), units=roster, resources=200)
    result = choose_actions(game_turn, config=AgentConfig(peacetime_resource_buffer=0))
    core_intent = next(intent for intent in result.intents if intent.is_core)
    assert core_intent.action is ActionKind.SPAWN
    assert core_intent.unit_type is UnitType.RANGER  # largest deficit


def test_peacetime_resource_conservation_after_mature_roster_filled():
    """In ECONOMY/EXPLORE mode, peacetime buffer blocks production."""
    # 12W/12V/14R → gap = 2 Rangers, but mature roster not filled yet
    roster = (
        *(unit(i, UnitType.WORKER, (i, 0)) for i in range(1, 13)),
        *(unit(i, UnitType.VANGUARD, (i, 1)) for i in range(13, 25)),
        *(unit(i, UnitType.RANGER, (i, 2)) for i in range(25, 39)),
    )
    # 38 units, not mature yet (rangers=14 < 16) — should still produce
    game_turn = turn(owned_core=core(), units=roster, resources=200, resource_cells=((5, 0),))
    result = choose_actions(game_turn, config=AgentConfig(peacetime_resource_buffer=40))
    core_intent = next(intent for intent in result.intents if intent.is_core)
    assert core_intent.action is ActionKind.SPAWN  # roster not filled, produce freely


def test_peacetime_conserve_blocks_production_when_roster_filled_and_buffer_insufficient():
    """Once mature roster is filled, peacetime buffer blocks production."""
    roster = _make_mature_roster()
    # resources=80: price for ranger at pop 39 = 34; reserve=5; buffer=40 → need 34+5+40=79.  Just enough.
    # But at pop=40 we can't spawn (cap hit).  Use 39 instead:
    roster39 = roster[:39]  # 12W/12V/15R — one Ranger short of mature
    game_turn = turn(owned_core=core(), units=roster39, resources=80, resource_cells=((5, 0),))
    # With buffer=40 and price=34+reserve=5: need 79. 80 >= 79 → should produce
    result = choose_actions(game_turn, config=AgentConfig(peacetime_resource_buffer=40))
    core_intent = next(intent for intent in result.intents if intent.is_core)
    assert core_intent.action is ActionKind.SPAWN  # 80 >= 34+5+40 = 79

    # Now with buffer=40 and resources=78 → 78 < 79 → should NOT produce
    game_turn2 = turn(owned_core=core(), units=roster39, resources=78, resource_cells=((5, 0),))
    result2 = choose_actions(game_turn2, config=AgentConfig(peacetime_resource_buffer=40))
    core_intent2 = next(intent for intent in result2.intents if intent.is_core)
    assert core_intent2.action is not ActionKind.SPAWN


def test_defend_stops_production_during_visible_breach_when_combat_ready():
    """DEFEND preserves resources when combat roster is established."""
    # 3 Workers, 1 Vanguard, 1 Ranger (combat ready established)
    roster = (
        *(unit(i, UnitType.WORKER, (i, 0)) for i in range(1, 4)),
        unit(4, UnitType.VANGUARD, (0, 1)),
        unit(5, UnitType.RANGER, (0, 2)),
    )
    nearby_enemy = unit(200, UnitType.VANGUARD, (1, 0), controlled=False)
    game_turn = turn(
        owned_core=core(),
        units=roster,
        enemies=(nearby_enemy,),
        resources=200,
    )
    result = choose_actions(game_turn)
    assert result.mode is StrategicMode.DEFEND
    core_intent = next(intent for intent in result.intents if intent.is_core)
    assert core_intent.action is ActionKind.WAIT


def test_defend_stops_production_even_with_combat_gaps():
    """Roster deficits do not override DEFEND resource preservation."""
    # 3 Workers (early met), 10 Vanguards (gap=2), 10 Rangers (gap=6)
    roster = (
        *(unit(i, UnitType.WORKER, (0, i)) for i in range(1, 4)),
        *(unit(i, UnitType.VANGUARD, (i, 1)) for i in range(4, 14)),
        *(unit(i, UnitType.RANGER, (i, 2)) for i in range(14, 24)),
    )
    nearby_enemy = unit(200, UnitType.VANGUARD, (1, 0), controlled=False)
    game_turn = turn(
        owned_core=core(),
        units=roster,
        enemies=(nearby_enemy,),
        resources=200,
    )
    result = choose_actions(game_turn)
    assert result.mode is StrategicMode.DEFEND
    core_intent = next(intent for intent in result.intents if intent.is_core)
    assert core_intent.action is ActionKind.WAIT


def test_peacetime_respects_normal_deficit_order():
    """In ECONOMY mode without buffer, standard deficit order applies."""
    # 8 Workers (early met), 10 Vanguards (early met), 10 Rangers (early met)
    # → mature phase: Worker gap=4, Vanguard gap=2, Ranger gap=6
    roster = (
        *(unit(i, UnitType.WORKER, (10, i)) for i in range(1, 9)),
        *(unit(i, UnitType.VANGUARD, (i + 10, 1)) for i in range(4, 14)),
        *(unit(i, UnitType.RANGER, (i + 10, 2)) for i in range(14, 24)),
    )
    game_turn = turn(
        owned_core=core(),
        units=roster,
        resources=200,
        resource_cells=((5, 0),),
        beacon_status=BeaconStatus.CARRIED,
        beacon_carrier_id=uuid(999),
    )
    result = choose_actions(
        game_turn,
        config=AgentConfig(peacetime_resource_buffer=0),
    )
    assert result.mode is StrategicMode.ECONOMY
    core_intent = next(intent for intent in result.intents if intent.is_core)
    assert core_intent.action is ActionKind.SPAWN
    # mature phase: Worker=2(-0), Vanguard=2(-1), Ranger=6(-2)
    # max by (deficit, -index) → (6, -2) → RANGER
    assert core_intent.unit_type is UnitType.RANGER


def test_early_respawn_zero_reserve_spawns_worker_with_five_resources():
    """Respawn with 1 worker and 5 resources immediately spawns 2nd worker."""
    worker = unit(1, UnitType.WORKER, (1, 0))
    result = choose_actions(turn(owned_core=core(), units=(worker,), resources=5))
    core_intent = next(intent for intent in result.intents if intent.is_core)
    assert core_intent.action is ActionKind.SPAWN
    assert core_intent.unit_type is UnitType.WORKER


def test_zero_combat_units_evades_threat_when_resources_insufficient():
    """With 0 combat units and near enemy threat, Core evades when resources < 10."""
    worker = unit(1, UnitType.WORKER, (0, 1))
    enemy = unit(200, UnitType.VANGUARD, (1, 0), controlled=False)
    result = choose_actions(
        turn(owned_core=core(position=(0, 0)), units=(worker,), enemies=(enemy,), resources=6)
    )
    core_intent = next(intent for intent in result.intents if intent.is_core)
    assert core_intent.action is ActionKind.START_MOVE
    assert core_intent.reason == "evade_threat_without_combat_roster"
