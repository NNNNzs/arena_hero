import pytest
from arena_hero import CoreView, UnitType, UnitView, CoreState
from arena_tactic.context import DecisionContext
from arena_tactic import AgentConfig, AgentMemory, choose_actions
from arena_tactic.models import ActionKind, StrategicMode
from arena_tactic.strategy import _core_migration_direction
from .factories import core, turn, unit


def test_core_bidirectional_migration_towards_returning_worker():
    c = core(position=(0, 0))
    # Worker 带着矿在 (5, 0)
    w = unit(1, UnitType.WORKER, (5, 0), cargo=1)
    
    game_turn = turn(owned_core=c, units=(w,))
    ctx = DecisionContext.from_turn(game_turn)
    memory = AgentMemory(last_mode=StrategicMode.ECONOMY, no_resource_ticks=20)
    
    dir_to_move = _core_migration_direction(ctx, memory, [])
    # 核心应该主动向右移动 (朝向 (5, 0) 迎面奔赴)
    assert dir_to_move is not None
    assert dir_to_move.value == "RIGHT"


def test_vanguard_recon_escort_screen():
    c = core(position=(0, 0))
    w = unit(1, UnitType.WORKER, (5, 5), cargo=0)
    v = unit(2, UnitType.VANGUARD, (1, 1))
    
    # 当有工兵在外探索且无敌人时，Vanguard 应该执行伴随护航
    game_turn = turn(owned_core=c, units=(w, v))
    res = choose_actions(game_turn, config=AgentConfig(core_guard_vanguards=0, mining_escort_vanguards=1))
    
    v_intent = next((item for item in res.intents if item.actor_id == v.id), None)
    assert v_intent is not None
    assert "recon_squad_vanguard_screen" in v_intent.reason
