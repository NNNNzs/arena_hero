from arena_hero import UnitType

from arena_tactic import AgentConfig, AgentMemory, choose_actions
from arena_tactic.strategy.combat import _combat_target, _escort_assignment

from .factories import core, turn, unit


def test_ring_patrol_arc_assignment_is_deterministic():
    owned_core = core()
    patrol = tuple(
        unit(value, UnitType.VANGUARD, (0, value)) for value in range(1, 5)
    )
    config = AgentConfig()

    first_memory = AgentMemory()
    first = [
        _combat_target(
            owned_core, actor, index, len(patrol), first_memory, config,
            role="patrol",
        )
        for index, actor in enumerate(patrol)
    ]
    second_memory = AgentMemory()
    second = [
        _combat_target(
            owned_core, actor, index, len(patrol), second_memory, config,
            role="patrol",
        )
        for index, actor in enumerate(patrol)
    ]

    assert first == second
    assert first == [(9, 0), (0, 9), (-9, 0), (0, -9)]


def test_default_outer_rings_are_not_limited_by_core_visibility():
    owned_core = core()
    actor = unit(1, UnitType.RANGER, (0, 0))
    config = AgentConfig()

    assert (
        config.patrol_radius_min,
        config.patrol_radius_max,
        config.hunter_radius_min,
        config.hunter_radius_max,
        config.patrol_radius_units_per_step,
    ) == (8, 14, 10, 16, 3)
    # A sufficiently large hunter roster reaches the configured maximum (16),
    # rather than the retired Core-vision coverage ceiling.
    assert _combat_target(
        owned_core, actor, 0, 19, AgentMemory(), config, role="hunter"
    ) == (16, 0)


def test_ring_patrol_target_lock_does_not_rotate_with_tick():
    actor = unit(1, UnitType.VANGUARD, (0, 0))
    owned_core = core()
    memory = AgentMemory()
    config = AgentConfig()
    target = _combat_target(
        owned_core, actor, 0, 1, memory, config, role="patrol"
    )
    memory.unit_tasks[str(actor.id)].update({"kind": "patrol", "target": list(target)})

    # Tick is deliberately absent from target selection: a later decision keeps
    # the lock until arrival or invalidation.
    assert _combat_target(
        owned_core, actor, 0, 1, memory, config, role="patrol"
    ) == target


def test_multiple_rangers_round_robin_workers_and_use_distinct_slots():
    owned_core = core()
    workers = (
        unit(1, UnitType.WORKER, (5, 0)),
        unit(2, UnitType.WORKER, (0, 5)),
    )
    rangers = tuple(
        unit(value, UnitType.RANGER, (1, value)) for value in range(10, 14)
    )

    assignments = [
        _escort_assignment(ranger, rangers, workers, owned_core)
        for ranger in rangers
    ]

    assert [assignment[0].id for assignment in assignments if assignment] == [
        workers[0].id, workers[1].id, workers[0].id, workers[1].id
    ]
    slots = [assignment[1] for assignment in assignments if assignment]
    assert len(slots) == len(set(slots))


def test_patrol_unit_switches_to_intercept_when_enemy_approaches():
    combat = tuple(
        unit(value, UnitType.VANGUARD, (0, value)) for value in range(1, 5)
    )
    first_enemy = unit(200, UnitType.WORKER, (8, 0), controlled=False)
    first = choose_actions(
        turn(tick=20, owned_core=core(), units=combat, enemies=(first_enemy,))
    )
    nearer_enemy = unit(200, UnitType.WORKER, (7, 0), controlled=False)
    second = choose_actions(
        turn(tick=21, owned_core=core(), units=combat, enemies=(nearer_enemy,)),
        memory=first.next_memory,
    )

    intercepts = [
        task for task in second.next_memory.unit_tasks.values()
        if task["kind"] == "intercept"
    ]
    assert intercepts
    assert all(task["target"] == [7, 0] for task in intercepts)


def test_excess_escorts_have_no_slot_and_fall_back_to_patrol():
    owned_core = core()
    worker = unit(1, UnitType.WORKER, (5, 0))
    combat = tuple(
        unit(value, UnitType.RANGER, (0, value)) for value in range(10, 14)
    )

    assert _escort_assignment(combat[2], combat, (worker,), owned_core) is not None
    assert _escort_assignment(combat[3], combat, (worker,), owned_core) is None
