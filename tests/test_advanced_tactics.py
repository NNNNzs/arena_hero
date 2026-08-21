from arena_hero import Direction, UnitType

from arena_tactic import AgentMemory, choose_actions
from arena_tactic.context import DecisionContext
from arena_tactic.models import ActionKind
from arena_tactic.strategy import _core_migration_direction, ranger_target_score
from arena_tactic.tactical_geometry import (
    best_mineral_tank_cell,
    has_line_of_sight,
    migration_site_score,
    shadow_fire_advantage,
)

from .factories import core, turn, unit


def test_shadow_fire_uses_los_supercover_but_shot_intermediate_cells_only():
    ranger_cell = (0, 0)
    enemy = unit(90, UnitType.VANGUARD, (2, 2), controlled=False)
    obstacles = {(1, 0)}  # diagonal corner side: blocks LOS, not Ranger fire

    assert not has_line_of_sight(enemy, ranger_cell, obstacles)
    assert shadow_fire_advantage(ranger_cell, enemy, obstacles) > 0


def test_ranger_target_score_rewards_one_way_shadow_shot():
    ranger = unit(1, UnitType.RANGER, (0, 0))
    enemy = unit(90, UnitType.VANGUARD, (2, 2), controlled=False)
    open_context = DecisionContext.from_turn(
        turn(owned_core=core(position=(-1, 0)), units=(ranger,), enemies=(enemy,))
    )
    shadow_context = DecisionContext.from_turn(
        turn(owned_core=core(position=(2, 1)), units=(ranger,), enemies=(enemy,), obstacle_cells=((1, 0),))
    )

    assert ranger_target_score(ranger, enemy, shadow_context, AgentMemory(obstacles={(1, 0)})) > ranger_target_score(
        ranger, enemy, open_context, AgentMemory()
    )


def test_vanguard_defense_moves_to_current_mineral_denial_cell():
    vanguard = unit(2, UnitType.VANGUARD, (0, 1))
    enemy = unit(90, UnitType.WORKER, (3, 0), controlled=False)
    result = choose_actions(
        turn(owned_core=core(), units=(vanguard,), enemies=(enemy,), resource_cells=((2, 0),))
    )
    intent = next(item for item in result.intents if item.actor_id == vanguard.id)

    assert intent.action is ActionKind.MOVE
    assert intent.reason == "vanguard_mineral_tank"
    assert intent.target_cell == (2, 0)
    assert best_mineral_tank_cell(
        resource_cells={(2, 0)}, enemy_cells={(3, 0)}, core_cell=(0, 0), obstacles=set()
    ) == (2, 0)


def test_migration_site_combines_recent_mineral_heat_and_chokepoint_terrain():
    observations = {(4, -1): 20, (4, 0): 20, (4, 1): 20}
    explored = {(1, -1), (1, 0), (1, 1), (0, 0), (2, 0)}
    obstacles = {(1, -1), (1, 1)}

    rich_choke = migration_site_score(
        (1, 0), resource_observations=observations, obstacles=obstacles,
        explored=explored, current_tick=20,
    )
    empty_open = migration_site_score(
        (-1, 0), resource_observations=observations, obstacles=obstacles,
        explored=explored, current_tick=20,
    )
    assert rich_choke > empty_open

    context = DecisionContext.from_turn(turn(tick=20, owned_core=core()))
    memory = AgentMemory(
        resource_observations=observations,
        obstacles=obstacles,
        explored=explored,
    )
    assert _core_migration_direction(context, memory, ()) is Direction.RIGHT


def test_core_migration_never_selects_a_current_visible_resource_cell():
    context = DecisionContext.from_turn(
        turn(tick=20, owned_core=core(), resource_cells=((1, 0),))
    )
    memory = AgentMemory(resource_observations={(4, 0): 20}, explored={(0, 0), (1, 0)})
    assert _core_migration_direction(context, memory, ()) is not Direction.RIGHT
