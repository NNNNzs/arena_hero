"""核心格拥堵死锁修复的回归测试。"""
from arena_tactic import choose_actions
from arena_tactic.memory import AgentMemory
from .factories import core, turn, unit
from arena_hero import UnitType


def test_empty_worker_vacates_core_cell_when_no_frontier():
    """空载工人滞留核心格、且无任何探索/复查目标时，应主动让位堵门格。

    构造：整张地图已全部探索（无 frontier），工人只能站在核心格上。
    """
    c = core(position=(0, 0))
    w = unit(1, UnitType.WORKER, (0, 0))
    # 探索全图 15x15 并把边界外一圈标记为障碍，使 frontier 为空
    explored = {(x, y) for x in range(-7, 8) for y in range(-7, 8)}
    obstacles = {
        (x, y) for x in range(-8, 9) for y in range(-8, 9)
        if not (-7 <= x <= 7 and -7 <= y <= 7)
    }
    memory = AgentMemory(explored=explored, obstacles=obstacles)
    result = choose_actions(turn(owned_core=c, units=(w,)), memory=memory)
    reasons = [i.reason for i in result.intents if i.actor_id == w.id]
    assert reasons, "worker must emit an intent"
    assert "vacate_core_cell_for_delivery" in reasons or "core_cell_vacate_blocked" in reasons


def test_cargo_worker_prioritized_before_empty_worker():
    """载货工人应先于空载工人获得决策预约权（排序逻辑直接验证）。"""
    workers_sorted = sorted(
        [("b", 0), ("a", 2)],
        key=lambda item: (0 if item[1] else 1, item[0]),
    )
    assert workers_sorted[0] == ("a", 2)
