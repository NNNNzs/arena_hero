from __future__ import annotations

from dataclasses import replace

from arena_hero import Direction, UnitType

from arena_tactic import AgentConfig, AgentMemory, choose_actions
from arena_tactic.models import ActionKind
from arena_tactic.navigation import destination

from .factories import core, event, turn, unit


def _unit_intent(result, actor_id):
    return next(intent for intent in result.intents if intent.actor_id == actor_id)


def test_four_workers_cover_all_cardinal_exploration_sectors():
    workers = (
        unit(1, UnitType.WORKER, (0, 1)),
        unit(2, UnitType.WORKER, (1, 0)),
        unit(3, UnitType.WORKER, (0, -1)),
        unit(4, UnitType.WORKER, (-1, 0)),
    )
    result = choose_actions(turn(owned_core=core(), units=workers))
    directions = {
        _unit_intent(result, worker.id).direction for worker in workers
    }
    assert directions == {
        Direction.RIGHT,
        Direction.DOWN,
        Direction.LEFT,
        Direction.UP,
    }


def test_sector_rotation_after_ticks_when_target_lost():
    """Sector rotates when the target is no longer in the frontier."""
    cfg = AgentConfig(exploration_sector_ticks=6)
    worker = unit(1, UnitType.WORKER, (0, 0))
    first = choose_actions(
        turn(tick=1, owned_core=core(position=(-10, 0)), units=(worker,)),
        config=cfg,
    )
    first_dir = _unit_intent(first, worker.id).direction
    assert first_dir is Direction.RIGHT

    # Mark the current target as explored so it leaves the frontier.
    target = tuple(first.next_memory.unit_tasks[str(worker.id)]["target"])
    explored = first.next_memory.explored | {target}
    memory = replace(first.next_memory, explored=explored)

    rotated = choose_actions(
        turn(tick=7, owned_core=core(position=(-10, 0)), units=(worker,)),
        memory=memory,
        config=cfg,
    )
    assert _unit_intent(rotated, worker.id).direction is Direction.DOWN


def test_target_lock_preserved_within_sector_window():
    """Target is kept across sector timer when still valid (default 60-tick hold)."""
    worker = unit(1, UnitType.WORKER, (0, 0))
    first = choose_actions(
        turn(tick=1, owned_core=core(position=(-10, 0)), units=(worker,)),
    )
    # At tick=7 the 60-tick window has NOT expired – target must be preserved.
    second = choose_actions(
        turn(tick=7, owned_core=core(position=(-10, 0)), units=(worker,)),
        memory=first.next_memory,
    )
    first_dir = _unit_intent(first, worker.id).direction
    second_dir = _unit_intent(second, worker.id).direction
    assert first_dir is Direction.RIGHT
    assert second_dir is Direction.RIGHT  # same target, no forced rotation


def test_target_lock_held_even_after_sector_timer_when_still_valid():
    """Even after 60 ticks, if the target is still in the frontier, keep it."""
    worker = unit(1, UnitType.WORKER, (0, 0))
    first = choose_actions(
        turn(tick=1, owned_core=core(position=(-10, 0)), units=(worker,)),
    )
    # tick=62 > 60, but frontier target is still valid → no rotation
    late = choose_actions(
        turn(tick=62, owned_core=core(position=(-10, 0)), units=(worker,)),
        memory=first.next_memory,
    )
    first_target = first.next_memory.unit_tasks[str(worker.id)]["target"]
    late_target = late.next_memory.unit_tasks[str(worker.id)]["target"]
    assert first_target == late_target  # target unchanged


def test_explorer_keeps_sector_and_routes_around_visible_obstacle_without_reversing():
    worker = unit(1, UnitType.WORKER, (0, 0))
    first = choose_actions(
        turn(
            tick=1,
            owned_core=core(position=(-10, 0)),
            units=(worker,),
            obstacle_cells=((1, 0),),
        )
    )
    first_intent = _unit_intent(first, worker.id)
    assert first_intent.action is ActionKind.MOVE
    assert first_intent.direction in (Direction.UP, Direction.DOWN)

    next_position = destination(worker.position, first_intent.direction)
    moved_worker = unit(1, UnitType.WORKER, next_position)
    second = choose_actions(
        turn(
            tick=2,
            owned_core=core(position=(-10, 0)),
            units=(moved_worker,),
            obstacle_cells=((1, 0),),
        ),
        memory=first.next_memory,
    )
    second_intent = _unit_intent(second, moved_worker.id)
    opposite = {
        Direction.UP: Direction.DOWN,
        Direction.DOWN: Direction.UP,
    }[first_intent.direction]
    assert second_intent.action is ActionKind.MOVE
    assert second_intent.direction is not opposite
    assert second.next_memory.unit_tasks[str(worker.id)]["sector"] == 0


def test_failed_destination_is_cooled_down_and_exploration_sector_rotates():
    worker = unit(1, UnitType.WORKER, (0, 0))
    memory = AgentMemory(
        last_tick=1,
        explored={(0, 0)},
        unit_tasks={
            str(worker.id): {
                "kind": "explore",
                "target": [5, 0],
                "step": [1, 0],
                "sector": 0,
                "sector_since": 1,
            }
        },
    )
    result = choose_actions(
        turn(
            tick=2,
            owned_core=core(position=(-10, 0)),
            units=(worker,),
            events=(
                event(
                    900,
                    "UNIT_MOVE_FAILED",
                    tick=1,
                    reason_code="MOVE_CONTESTED",
                    position=(0, 0),
                    actor_id=worker.id,
                ),
            ),
        ),
        memory=memory,
        config=AgentConfig(movement_failure_cooldown_ticks=4),
    )
    intent = _unit_intent(result, worker.id)
    assert intent.direction is not Direction.RIGHT
    assert result.next_memory.temporary_blocks[(1, 0)] == 6
    assert result.next_memory.unit_tasks[str(worker.id)]["sector"] == 1


def test_blocked_terrain_failure_promotes_attempted_cell_to_permanent_obstacle():
    worker = unit(1, UnitType.WORKER, (0, 0))
    memory = AgentMemory(
        last_tick=1,
        unit_tasks={
            str(worker.id): {
                "kind": "explore",
                "target": [5, 0],
                "step": [1, 0],
                "sector": 0,
                "sector_since": 1,
            }
        },
    )
    result = choose_actions(
        turn(
            tick=2,
            owned_core=core(position=(-10, 0)),
            units=(worker,),
            events=(
                event(
                    901,
                    "UNIT_MOVE_FAILED",
                    tick=1,
                    reason_code="MOVE_BLOCKED_TERRAIN",
                    position=(0, 0),
                    actor_id=worker.id,
                ),
            ),
        ),
        memory=memory,
    )
    assert (1, 0) in result.next_memory.obstacles
    assert _unit_intent(result, worker.id).direction is not Direction.RIGHT
