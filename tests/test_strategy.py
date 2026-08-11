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

from .factories import core, turn, unit, uuid


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

    nearby_enemy = unit(200, UnitType.WORKER, (4, 0), controlled=False)
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
    assert intent.target_cell is not None


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
    spawn_turn = turn(owned_core=core(), resources=10)
    choose_actions(spawn_turn)
    assert spawn_turn.plan.core_action.type == "SPAWN"
    assert spawn_turn.plan.core_action.unit_type is UnitType.WORKER

    reserved_turn = turn(owned_core=core(), resources=9)
    choose_actions(reserved_turn)
    assert reserved_turn.plan.core_action.type == "WAIT"

    twenty = tuple(
        unit(index + 1, (UnitType.WORKER, UnitType.VANGUARD, UnitType.RANGER)[index % 3], (index + 1, 2))
        for index in range(20)
    )
    capped_turn = turn(owned_core=core(), units=twenty, resources=100)
    choose_actions(capped_turn)
    assert capped_turn.plan.core_action.type != "SPAWN"


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


def test_missing_core_never_invents_an_action():
    unexpected_worker = unit(1, UnitType.WORKER, (0, 0))
    game_turn = turn(owned_core=None, units=(unexpected_worker,))
    result = choose_actions(game_turn)
    assert result.intents == ()
    assert game_turn.plan.core_action is None
    assert game_turn.plan.unit_actions == {}
