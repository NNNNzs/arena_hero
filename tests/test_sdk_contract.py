from arena_hero import BeaconStatus, CoreState, UnitType

from arena_tactic import AgentMemory, choose_actions

from .factories import core, turn, unit


def test_worker_current_resource_and_cargo_actions_map_to_sdk():
    harvester = unit(1, UnitType.WORKER, (2, 0))
    harvest_turn = turn(
        owned_core=core(),
        units=(harvester,),
        resource_cells=((2, 0),),
    )
    choose_actions(harvest_turn)
    assert harvest_turn.plan.unit_actions[harvester.id].type == "HARVEST"

    depositor = unit(2, UnitType.WORKER, (0, 0), cargo=2)
    deposit_turn = turn(owned_core=core(), units=(depositor,))
    choose_actions(deposit_turn)
    assert deposit_turn.plan.unit_actions[depositor.id].type == "DEPOSIT"


def test_core_heal_repair_spawn_wait_move_and_pickup_map_to_sdk():
    heal_turn = turn(owned_core=core(hp=3), resources=2)
    choose_actions(heal_turn)
    assert heal_turn.plan.core_action.type == "HEAL"

    repair_turn = turn(owned_core=core(shield=2), resources=1)
    choose_actions(repair_turn)
    assert repair_turn.plan.core_action.type == "REPAIR_SHIELD"

    spawn_turn = turn(owned_core=core(), resources=10)
    choose_actions(spawn_turn)
    assert spawn_turn.plan.core_action.type == "SPAWN"

    wait_turn = turn(owned_core=core(), resources=4)
    choose_actions(wait_turn)
    assert wait_turn.plan.core_action.type == "WAIT"

    migration_memory = AgentMemory(
        last_tick=8,
        no_resource_ticks=8,
        explored={(0, 0), (1, 0), (-1, 0), (0, 1), (0, -1)},
    )
    move_turn = turn(tick=9, owned_core=core())
    choose_actions(move_turn, memory=migration_memory)
    assert move_turn.plan.core_action.type == "START_MOVE"

    units = tuple(
        unit(index + 1, UnitType.WORKER, (index + 1, 0))
        for index in range(6)
    )
    pickup_turn = turn(
        owned_core=core(),
        units=units,
        beacon_position=(0, 0),
        beacon_status=BeaconStatus.GROUND,
    )
    choose_actions(pickup_turn)
    assert pickup_turn.plan.core_action.type == "PICKUP_BEACON"


def test_moving_core_maps_only_wait_and_exposes_destination_to_cargo_worker():
    moving = core(state=CoreState.MOVING)
    worker = unit(1, UnitType.WORKER, (0, 1), cargo=1)
    game_turn = turn(owned_core=moving, units=(worker,))
    choose_actions(game_turn)
    assert game_turn.plan.core_action.type == "WAIT"
    assert game_turn.plan.unit_actions[worker.id].type == "MOVE"


def test_core_does_not_spawn_while_a_unit_moves_into_its_cell():
    cargo_worker = unit(1, UnitType.WORKER, (1, 0), cargo=1)
    game_turn = turn(owned_core=core(), units=(cargo_worker,), resources=20)
    choose_actions(game_turn)
    assert game_turn.plan.unit_actions[cargo_worker.id].type == "MOVE"
    assert game_turn.plan.core_action.type != "SPAWN"
