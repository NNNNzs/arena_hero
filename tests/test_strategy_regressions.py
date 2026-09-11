from arena_hero import Direction, UnitType

from arena_tactic.context import DecisionContext
from arena_tactic.memory import AgentMemory
from arena_tactic.models import ActionKind, AgentConfig, Position, ReservationTable, StrategicMode
from arena_tactic.strategy.combat import ranger_target_score
from arena_tactic.strategy.core_plan import _core_migration_direction, _plan_core

from .factories import core, turn, unit


def test_peacetime_buffer_cannot_exceed_core_storage_capacity():
    units = (
        unit(1, UnitType.WORKER, (1, 0)),
        unit(2, UnitType.WORKER, (2, 0)),
        unit(3, UnitType.VANGUARD, (0, 1)),
        unit(4, UnitType.VANGUARD, (0, 2)),
        unit(5, UnitType.RANGER, (-1, 0)),
    )
    context = DecisionContext.from_turn(
        turn(owned_core=core(), units=units, resources=25)
    )

    intent = _plan_core(
        context,
        AgentMemory(),
        StrategicMode.ECONOMY,
        [],
        0,
        AgentConfig(),
    )

    assert intent is not None
    assert intent.action is ActionKind.SPAWN
    assert intent.unit_type is UnitType.WORKER


def test_stale_migration_recommendation_falls_back_to_best_resource_center():
    context = DecisionContext.from_turn(turn(tick=200, owned_core=core()))
    memory = AgentMemory(
        last_tick=199,
        resource_observations={(5, 0): 199},
        migration_recommendation={
            "center": [50, 50],
            "score": 100.0,
            "computed_at_tick": 0,
            "interval_ticks": 60,
        },
    )

    direction = _core_migration_direction(context, memory, [])

    assert direction is Direction.RIGHT


def test_ranger_scoring_uses_legal_firing_range_for_diagonal_targets():
    ranger = unit(1, UnitType.RANGER, (0, 0))
    diagonal = unit(2, UnitType.RANGER, (2, 2), controlled=False)
    straight = unit(3, UnitType.RANGER, (3, 0), controlled=False)
    context = DecisionContext.from_turn(
        turn(owned_core=core(position=(-5, 0)), units=(ranger,), enemies=(diagonal, straight))
    )
    memory = AgentMemory()

    diagonal_score = ranger_target_score(ranger, diagonal, context, memory)
    straight_score = ranger_target_score(ranger, straight, context, memory)

    assert diagonal_score > straight_score


def test_guard_slots_skips_radius_1_in_tight_bottleneck_terrain():
    from arena_tactic.strategy.common import _guard_slots

    # Core at (0, 0) with obstacles on North, East, South -> only ( -1, 0 ) is passable
    context = DecisionContext.from_turn(turn(owned_core=core(position=(0, 0))))
    memory = AgentMemory(
        obstacles={(0, 1), (1, 0), (0, -1)},
    )
    slots = _guard_slots(context, memory)
    # Radius 1 slot (-1, 0) must NOT be chosen to avoid blocking the sole entrance
    assert (-1, 0) not in slots
    # Guard posts begin beyond the dedicated 1-3 cell mining corridor.
    assert slots
    assert all(abs(x) + abs(y) in (4, 5, 6) for x, y in slots)


def test_guard_slots_excludes_passable_exits():
    """Guard slots must never include core-adjacent passable exits (doorstep cells).

    Regression for GUARD_OSCILLATION: units assigned to a guard slot that is
    also a passable exit get driven off by ``_evacuate_doorstep_intent``, then
    pulled back by the guard-return logic, creating a 2-Tick ping-pong.
    """
    from arena_tactic.strategy.common import _guard_slots

    # Core at (0, 0) with all 4 exits passable (no obstacles).
    context = DecisionContext.from_turn(turn(owned_core=core(position=(0, 0))))
    memory = AgentMemory()
    slots = _guard_slots(context, memory)

    passable_exits = {(1, 0), (-1, 0), (0, 1), (0, -1)}
    for slot in slots:
        assert slot not in passable_exits, (
            f"Guard slot {slot} is a passable exit — will cause doorstep oscillation"
        )


def test_guard_slots_excludes_passable_exits_with_partial_obstacles():
    """Guard slots must exclude all passable exits even when some are blocked."""
    from arena_tactic.strategy.common import _guard_slots

    # Core at (0, 0): North and East blocked, South and West passable.
    context = DecisionContext.from_turn(turn(owned_core=core(position=(0, 0))))
    memory = AgentMemory(obstacles={(0, 1), (1, 0)})
    slots = _guard_slots(context, memory)

    # The two remaining passable exits must not appear in guard slots.
    passable_exits = {(-1, 0), (0, -1)}
    for slot in slots:
        assert slot not in passable_exits, (
            f"Guard slot {slot} is a passable exit — will cause doorstep oscillation"
        )


def test_vanguard_at_guard_target_holds_position_instead_of_evacuating():
    """A vanguard that has reached its guard target must WAIT, not evacuate.

    Regression for GUARD_OSCILLATION: the ``else`` branch in ``_plan_vanguards``
    (vanguard at guard target) previously always called
    ``_evacuate_doorstep_intent``, which moved the unit off a doorstep cell,
    causing the guard-return logic to pull it back next tick.
    """
    from arena_tactic.strategy.common import _guard_slots
    from arena_tactic.strategy.vanguards import _plan_vanguards

    # Core at (0, 0) with all 4 exits open → guard slots exclude passable
    # exits, so first guard slot is at radius 2 (e.g. (-2, 0)).
    context = DecisionContext.from_turn(
        turn(owned_core=core(position=(0, 0)))
    )
    memory = AgentMemory()
    guard_slots = _guard_slots(context, memory)
    assert guard_slots, "Expected non-empty guard slots"
    guard_target = guard_slots[0]

    vanguard = unit(1, UnitType.VANGUARD, guard_target)
    context = DecisionContext.from_turn(
        turn(owned_core=core(position=(0, 0)), units=(vanguard,))
    )
    memory = AgentMemory()
    reservations = ReservationTable(occupancy={guard_target: 1})
    config = AgentConfig(core_guard_vanguards=1)

    intents = _plan_vanguards(
        context, memory, reservations, deadline=999.0, config=config,
        heal_allowances={},
    )
    vanguard_intent = next(i for i in intents if i.actor_id == vanguard.id)

    # Must hold position — reason must be "holding_defense_ring", NOT an
    # evacuate_doorstep reason that would move the unit away.
    assert vanguard_intent.action is ActionKind.WAIT, (
        f"Expected WAIT at guard target {guard_target}, got {vanguard_intent.action}"
    )
    assert vanguard_intent.reason == "holding_defense_ring", (
        f"Expected holding_defense_ring, got {vanguard_intent.reason}"
    )


def test_ranger_at_guard_target_holds_position_instead_of_evacuating():
    """A ranger that has reached its guard target must WAIT, not evacuate.

    Same regression as the vanguard variant — rangers had the identical
    oscillation pattern in ``_plan_rangers``.
    """
    from arena_tactic.strategy.common import _guard_slots
    from arena_tactic.strategy.rangers import _plan_rangers

    context = DecisionContext.from_turn(
        turn(owned_core=core(position=(0, 0)))
    )
    memory = AgentMemory()
    guard_slots = _guard_slots(context, memory)
    assert guard_slots, "Expected non-empty guard slots"
    # Rangers are offset by the number of vanguards (0 in this case).
    guard_target = guard_slots[0]

    ranger = unit(1, UnitType.RANGER, guard_target)
    context = DecisionContext.from_turn(
        turn(owned_core=core(position=(0, 0)), units=(ranger,))
    )
    memory = AgentMemory()
    reservations = ReservationTable(occupancy={guard_target: 1})
    config = AgentConfig(core_guard_rangers=1)

    intents = _plan_rangers(
        context, memory, reservations, deadline=999.0, config=config,
        heal_allowances={},
    )
    ranger_intent = next(i for i in intents if i.actor_id == ranger.id)

    assert ranger_intent.action is ActionKind.WAIT, (
        f"Expected WAIT at guard target {guard_target}, got {ranger_intent.action}"
    )
    assert ranger_intent.reason == "holding_defense_ring", (
        f"Expected holding_defense_ring, got {ranger_intent.reason}"
    )


def test_guard_vanguard_no_oscillation_across_ticks():
    """Multi-tick simulation: guard vanguard must not oscillate.

    Regression for GUARD_OSCILLATION at Tick 261349: a vanguard assigned to a
    guard slot adjacent to the core ping-ponged between the guard target and
    an adjacent cell every 2 ticks because ``_evacuate_doorstep_intent``
    conflicted with the guard-return logic.
    """
    from arena_tactic.navigation import destination, distance
    from arena_tactic.strategy.common import _guard_slots
    from arena_tactic.strategy.vanguards import _plan_vanguards

    config = AgentConfig(core_guard_vanguards=1)
    # Core at (-834, -577). Before the fix, the guard slot was (-835, -577)
    # (a passable exit), causing oscillation. After the fix, guard slots
    # exclude passable exits, so the first guard slot is (-836, -577).
    core_pos = (-834, -577)

    # Compute the actual guard target.
    ctx = DecisionContext.from_turn(turn(owned_core=core(position=core_pos)))
    mem = AgentMemory()
    guard_slots = _guard_slots(ctx, mem)
    assert guard_slots, "Expected non-empty guard slots"
    guard_target = guard_slots[0]

    # Vanguard starts at the old doorstep position (-835, -577).
    # After the fix, this is NOT a guard slot, so the vanguard should move
    # toward the actual guard target and then hold without oscillating.
    start_pos = (-835, -577)
    positions: list[Position] = [start_pos]

    for tick in range(261349, 261359):
        current = positions[-1]
        vanguard = unit(1, UnitType.VANGUARD, current)
        game_turn = turn(
            tick=tick,
            owned_core=core(position=core_pos),
            units=(vanguard,),
        )
        context = DecisionContext.from_turn(game_turn)
        memory = AgentMemory()
        reservations = ReservationTable(occupancy={current: 1})

        intents = _plan_vanguards(
            context, memory, reservations, deadline=999.0, config=config,
            heal_allowances={},
        )
        vanguard_intent = next(i for i in intents if i.actor_id == vanguard.id)

        if vanguard_intent.action is ActionKind.MOVE and vanguard_intent.direction:
            new_pos = destination(current, vanguard_intent.direction)
            positions.append(new_pos)
        else:
            # WAIT → position stays the same
            positions.append(current)

    # No 2-cell oscillation: for any i >= 2, if the unit moved (pos[i] != pos[i-1]),
    # it must not bounce back to the position from 2 ticks ago (pos[i] == pos[i-2]).
    for i in range(2, len(positions)):
        if positions[i] != positions[i - 1]:
            assert positions[i] != positions[i - 2], (
                f"OSCILLATION at tick {i}: {positions[i-2]} → {positions[i-1]} → {positions[i]}. "
                f"Full path: {positions}"
            )
    # The vanguard must converge to the guard target and stay there.
    final = positions[-1]
    assert distance(final, guard_target) <= 1, (
        f"Vanguard at {final} did not converge to guard target {guard_target}"
    )


def test_empty_worker_on_core_vacates_when_resource_route_blocked():
    from arena_tactic.models import ReservationTable
    from arena_tactic.strategy.workers import _plan_workers

    # Core at (0, 0) with empty worker at (0, 0).
    # Passable exit (-1, 0) is blocked by a friend, but (-1, -1) or other open cell is reachable.
    w_empty = unit(1, UnitType.WORKER, (0, 0), cargo=0)
    w_blocker = unit(2, UnitType.WORKER, (-1, 0), cargo=1)
    context = DecisionContext.from_turn(
        turn(
            owned_core=core(position=(0, 0)),
            units=(w_empty, w_blocker),
            resource_cells=((-5, 0),),
        )
    )
    # (-1, 0) has 2 occupancy (fully blocked), North/East/South are obstacles
    memory = AgentMemory(
        obstacles={(0, 1), (1, 0), (0, -1)},
    )
    reservations = ReservationTable(occupancy={(0, 0): 2, (-1, 0): 2})
    intents = _plan_workers(
        context, memory, reservations, 9999999999.0, AgentConfig(), {}
    )
    # The empty worker must attempt to vacate or step aside instead of purely waiting on resource_route_blocked
    w1_intent = next((i for i in intents if i.actor_id == w_empty.id), None)
    assert w1_intent is not None


def test_deploy_sidestep_penalizes_immediate_return_to_previous_cell():
    from arena_tactic.models import ReservationTable
    from arena_tactic.strategy.common import _deploy_sidestep

    ranger = unit(1, UnitType.RANGER, (0, 0))
    context = DecisionContext.from_turn(turn(owned_core=core(position=(0, 0)), units=(ranger,)))
    memory = AgentMemory()
    # Record that ranger was at (0, 1) in previous tick
    memory.unit_tasks[str(ranger.id)] = {"prev_cell": [0, 1]}

    reservations = ReservationTable(occupancy={})
    intent = _deploy_sidestep(
        ranger,
        target=(10, 0),
        context=context,
        memory=memory,
        reservations=reservations,
        reason="test_deploy",
        core_position=(0, 0),
    )
    assert intent is not None
    # Must NOT choose (0, 1) since it was the previous cell
    assert intent.reserved_cell != (0, 1)


def test_combat_unit_yields_outward_for_nearby_cargo_delivery():
    from arena_tactic.models import ReservationTable
    from arena_tactic.strategy.common import _yield_cargo_delivery

    vanguard = unit(1, UnitType.VANGUARD, (-1, 0))
    cargo_worker = unit(2, UnitType.WORKER, (-2, 0), cargo=1)
    context = DecisionContext.from_turn(
        turn(owned_core=core(position=(0, 0)), units=(vanguard, cargo_worker))
    )
    intent = _yield_cargo_delivery(
        vanguard, context, AgentMemory(), ReservationTable(occupancy={}), AgentConfig()
    )

    assert intent is not None
    assert intent.reason == "yield_cargo_delivery_sidestep"
    assert intent.reserved_cell is not None
    assert abs(intent.reserved_cell[0]) + abs(intent.reserved_cell[1]) > 1


def test_combat_unit_vacates_core_cell_for_cargo_delivery():
    from arena_tactic.models import ReservationTable
    from arena_tactic.strategy.common import _yield_cargo_delivery

    ranger = unit(1, UnitType.RANGER, (0, 0))
    cargo_worker = unit(2, UnitType.WORKER, (-1, 0), cargo=1)
    context = DecisionContext.from_turn(
        turn(owned_core=core(position=(0, 0)), units=(ranger, cargo_worker))
    )
    intent = _yield_cargo_delivery(
        ranger,
        context,
        AgentMemory(),
        ReservationTable(occupancy={(0, 0): 2}),
        AgentConfig(),
    )

    assert intent is not None
    assert intent.reserved_cell != (0, 0)
    assert intent.reserved_cell is not None
    assert intent.reserved_cell[0] > 0


def test_combat_unit_yields_for_cargo_waiting_at_the_outer_corridor_relay():
    """A five-cell cargo queue must clear its blocking relay before entry."""
    from arena_tactic.models import ReservationTable
    from arena_tactic.strategy.common import _yield_cargo_delivery

    # This recreates Issue #7's priority inversion: the returning Worker is
    # outside the former three-cell throat, while a stationary Ranger sits in
    # the same one-cell approach corridor.
    ranger = unit(1, UnitType.RANGER, (-4, 0))
    cargo_worker = unit(2, UnitType.WORKER, (-5, 0), cargo=1)
    context = DecisionContext.from_turn(
        turn(owned_core=core(position=(0, 0)), units=(ranger, cargo_worker))
    )

    intent = _yield_cargo_delivery(
        ranger,
        context,
        AgentMemory(),
        ReservationTable(occupancy={(-4, 0): 1, (-5, 0): 1}),
        AgentConfig(cargo_delivery_yield_radius=6),
    )

    assert intent is not None
    assert intent.reason == "yield_cargo_delivery_sidestep"
    assert intent.reserved_cell is not None
    assert intent.reserved_cell != ranger.position
