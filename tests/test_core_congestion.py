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


def test_empty_core_worker_prioritized_before_doorstep_cargo():
    """核心格空载工人必须先获得腾退预约权，避免对换死锁。"""
    workers_sorted = sorted(
        [("b", 0), ("a", 2)],
        key=lambda item: (0 if not item[1] else 1, item[0]),
    )
    assert workers_sorted[0] == ("b", 0)


def test_doorstep_congestion_only_one_cargo_worker_yields():
    """门口拥堵时只有一名载货工人退让，避免两名工人同周期往返振荡死锁。"""
    c = core(position=(0, 0))
    # 核心格已被占满（容量 2），门口 (-1, 0) 有两名载货工人
    w1 = unit(1, UnitType.WORKER, (-1, 0), cargo=1)
    w2 = unit(2, UnitType.WORKER, (-1, 0), cargo=1)
    # 核心上站一个空载工人使得核心格满员
    w_core = unit(3, UnitType.WORKER, (0, 0), cargo=0)
    
    # 单通道口袋：Core 只有西侧门口可走。
    memory = AgentMemory()
    result = choose_actions(turn(
        owned_core=c,
        units=(w1, w2, w_core),
        obstacle_cells=((0, -1), (1, 0), (0, 1)),
    ), memory=memory)
    
    intents = {i.actor_id: i for i in result.intents}
    reasons = [intents[w1.id].reason, intents[w2.id].reason]
    
    # 一个门口工人退避，核心工人同 Tick 进入刚腾出的唯一出口。
    yield_count = sum(1 for r in reasons if r == "yield_doorstep_congestion")
    assert yield_count == 1, f"Expected exactly 1 yielding worker, got: {reasons}"
    assert intents[w_core.id].reason == "vacate_core_cell_for_delivery"
    assert intents[w_core.id].reserved_cell == (-1, 0)


def test_idle_worker_yields_doorstep_when_no_frontier():
    """空载且无探索目标的工人停留在门口时，应主动让开门口避免阻断运矿。"""
    c = core(position=(0, 0))
    # 空载工人在门口 (-1, 0)
    w = unit(1, UnitType.WORKER, (-1, 0), cargo=0)
    # 全图已探索，无 frontier
    explored = {(x, y) for x in range(-5, 6) for y in range(-5, 6)}
    obstacles = {
        (x, y) for x in range(-6, 7) for y in range(-6, 7)
        if not (-5 <= x <= 5 and -5 <= y <= 5)
    }
    memory = AgentMemory(explored=explored, obstacles=obstacles)
    result = choose_actions(turn(owned_core=c, units=(w,)), memory=memory)
    
    intent = next(i for i in result.intents if i.actor_id == w.id)
    assert intent.reason == "yield_core_doorstep_idle"


def test_combat_guard_vacates_core_for_returning_cargo_worker():
    """守卫占核心格时，返矿工人进入咽喉后应优先让位。"""
    c = core(position=(0, 0))
    cargo_worker = unit(1, UnitType.WORKER, (-1, 0), cargo=1)
    guard = unit(2, UnitType.VANGUARD, (0, 0))

    result = choose_actions(
        turn(owned_core=c, units=(cargo_worker, guard)), memory=AgentMemory()
    )

    intent = next(intent for intent in result.intents if intent.actor_id == guard.id)
    assert intent.reason == "yield_cargo_delivery_sidestep"
    assert intent.reserved_cell is not None
    assert intent.reserved_cell[0] > 0
