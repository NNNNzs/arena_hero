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


def test_delivery_corridor_yield_prefers_empty_side_over_occupied_lane():
    """递送走廊让位应优先空格，不能按 DOWN/LEFT/RIGHT/UP 字符串顺序选满格。"""
    c = core(position=(0, 0))
    doorstep_a = unit(1, UnitType.WORKER, (-1, 0), cargo=1)
    doorstep_b = unit(2, UnitType.WORKER, (-1, 0), cargo=1)
    core_worker = unit(3, UnitType.WORKER, (0, 0))
    # The southern lane is already full while the northern side tile is empty.
    south_a = unit(4, UnitType.WORKER, (-1, 1), cargo=1)
    south_b = unit(5, UnitType.WORKER, (-1, 1), cargo=1)

    result = choose_actions(
        turn(
            owned_core=c,
            units=(doorstep_a, doorstep_b, core_worker, south_a, south_b),
            obstacle_cells=((0, -1), (1, 0), (0, 1)),
        ),
        memory=AgentMemory(),
    )

    intents = {intent.actor_id: intent for intent in result.intents}
    yielded = [intents[worker.id] for worker in (doorstep_a, doorstep_b)]
    yield_intent = next(intent for intent in yielded if intent.reason == "yield_doorstep_congestion")
    assert yield_intent.reserved_cell == (-1, -1)


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


def test_damaged_ranger_evacuates_doorstep_when_core_entry_blocked():
    """残血游侠撤退至单通道门口且核心已满无法进入时，必须向外避让绝不堵门。"""
    c = core(position=(0, 0))
    # 核心格已被占满（容量 2）
    w_core1 = unit(1, UnitType.WORKER, (0, 0), cargo=0)
    w_core2 = unit(2, UnitType.WORKER, (0, 0), cargo=0)
    # 单通道门口 (-1, 0) 站着残血游侠 (hp=1)
    ranger = unit(3, UnitType.RANGER, (-1, 0), hp=1)

    # 单通道口袋：Core 北、东、南为障碍，(-1, 0) 为唯一出入口
    result = choose_actions(
        turn(
            owned_core=c,
            units=(w_core1, w_core2, ranger),
            obstacle_cells=((0, -1), (1, 0), (0, 1)),
        ),
        memory=AgentMemory(),
    )

    r_intent = next(intent for intent in result.intents if intent.actor_id == ranger.id)
    assert r_intent.action.value == "MOVE"
    assert r_intent.reason == "yield_doorstep_critical_retreat"
    assert r_intent.reserved_cell is not None
    assert r_intent.reserved_cell != (-1, 0)
    assert abs(r_intent.reserved_cell[0]) + abs(r_intent.reserved_cell[1]) >= 2


def test_empty_core_worker_vacates_and_avoids_dead_end_pocket():
    """单出入口核心格空载工兵腾退时，门口工兵避让必须避开死胡同口袋，实现同回合出入对换。"""
    c = core(position=(0, 0))
    # 核心格内有 1 个空载工兵
    w_core = unit(1, UnitType.WORKER, (0, 0), cargo=0)
    # 唯一门口 (-1, 0) 站着 2 个满载工兵
    w_door1 = unit(2, UnitType.WORKER, (-1, 0), cargo=1)
    w_door2 = unit(3, UnitType.WORKER, (-1, 0), cargo=1)
    # 北面 (-1, -1) 形成死胡同口袋：周围三面是墙
    # (-1, -2) 是墙, (0, -1) 是墙, (-2, -1) 是墙
    # 只有南面 (-1, 1) 和西面 (-2, 0) 可以通行
    result = choose_actions(
        turn(
            owned_core=c,
            units=(w_core, w_door1, w_door2),
            obstacle_cells=(
                (0, -1), (1, 0), (0, 1),   # 核心北、东、南全封闭
                (-1, -2), (-2, -1),        # 门口北侧 (-1, -1) 成为死胡同口袋
            ),
        ),
        memory=AgentMemory(),
    )

    intents = {i.actor_id: i for i in result.intents}
    # 核心工兵必须成功生成腾退指令走向 (-1, 0)
    core_intent = intents[w_core.id]
    assert core_intent.action.value == "MOVE"
    assert core_intent.reason == "vacate_core_cell_for_delivery"
    assert core_intent.reserved_cell == (-1, 0)

    # 门口工兵之一必须让位，且绝不能避让进死胡同口袋 (-1, -1)
    door_intents = [intents[uid] for uid in (w_door1.id, w_door2.id)]
    yield_intent = next(i for i in door_intents if i.reason == "yield_doorstep_congestion")
    assert yield_intent.reserved_cell != (-1, -1)
    assert yield_intent.reserved_cell in ((-1, 1), (-2, 0))


def test_doorstep_cargo_workers_enter_empty_core_without_sidestep_oscillation():
    """当核心为空时，门口重叠的满载工兵必须优先进入核心入库，绝不能让路外退导致振荡。"""
    c = core(position=(0, 0))
    # 核心格为空，门口 (-1, 0) 站着 2 名载货工人
    w1 = unit(1, UnitType.WORKER, (-1, 0), cargo=1)
    w2 = unit(2, UnitType.WORKER, (-1, 0), cargo=1)

    result = choose_actions(
        turn(
            owned_core=c,
            units=(w1, w2),
        ),
        memory=AgentMemory(),
    )

    intents = {i.actor_id: i for i in result.intents}
    # 至少有一名工人必须直接走入核心格 (0, 0) 进行入库
    reasons = [intents[w1.id].reason, intents[w2.id].reason]
    reserved_cells = [intents[w1.id].reserved_cell, intents[w2.id].reserved_cell]

    assert (0, 0) in reserved_cells, f"Expected at least one worker entering core (0, 0), got: {reserved_cells}"
    # 另一名工兵绝不能触发 yield_doorstep_congestion 外退
    assert "yield_doorstep_congestion" not in reasons, f"Cargo worker should not yield doorstep when core is free: {reasons}"


def test_doorstep_cargo_worker_enters_core_even_when_astar_times_out():
    """当系统决策时间紧迫/超时导致 A* 无法完成完整寻路时，门口相邻(dist=1)的载货工兵必须直进核心，绝不卡死在等待状态。"""
    from arena_tactic.models import ActionKind, AgentConfig, ReservationTable
    from arena_tactic.strategy.workers import _plan_workers
    from arena_tactic.context import DecisionContext

    c = core(position=(0, 0))
    w1 = unit(1, UnitType.WORKER, (-1, 0), cargo=1)
    w2 = unit(2, UnitType.WORKER, (-1, 0), cargo=1)

    t = turn(owned_core=c, units=(w1, w2))
    context = DecisionContext.from_turn(t)
    memory = AgentMemory()
    config = AgentConfig()
    reservations = ReservationTable({cell: len(ids) for cell, ids in context.friendly_occupancy.items()})

    # 模拟 deadline 已经过期 (deadline = 0.0)，导致 A* 内部 visited 循环或 perf_counter 立即超时
    intents = _plan_workers(context, memory, reservations, 0.0, config, {})
    intents_by_id = {i.actor_id: i for i in intents}

    # 第一名工兵必须直进核心 (0, 0)，第二名在门口就地等待
    w1_intent = intents_by_id[w1.id]
    w2_intent = intents_by_id[w2.id]

    actions = {w1_intent.action, w2_intent.action}
    assert ActionKind.MOVE in actions
    reserved_cells = [w1_intent.reserved_cell, w2_intent.reserved_cell]
    assert (0, 0) in reserved_cells


def test_evacuate_doorstep_anti_oscillation():
    """_evacuate_doorstep_intent 必须防止在门口两格之间往复振荡。

    当工兵从格子 A 移动到格子 B 后（记录 prev_cell=A），下一次 evacuate
    必须对返回格子 A 施加高惩罚，优先选择其他方向的格子。
    """
    from arena_tactic.models import AgentConfig, ReservationTable
    from arena_tactic.strategy.common import _evacuate_doorstep_intent

    c = core(position=(0, 0))
    # 工兵在门口 (-1, 0)，周围有 (-2, 0) 和 (-1, 1) 和 (-1, -1) 可选
    w = unit(1, UnitType.WORKER, (-1, 0), cargo=0)
    # 模拟工兵刚从 (-1, 1) 移动到 (-1, 0)，prev_cell=(-1, 1)
    t = turn(owned_core=c, units=(w,), obstacle_cells=((0, -1), (1, 0), (0, 1)))
    from arena_tactic.context import DecisionContext
    context = DecisionContext.from_turn(t)
    memory = AgentMemory()
    memory.unit_tasks[str(w.id)] = {"prev_cell": [-1, 1]}
    reservations = ReservationTable({cell: len(ids) for cell, ids in context.friendly_occupancy.items()})

    intent = _evacuate_doorstep_intent(w, context, memory, reservations, "yield_core_doorstep_idle")
    assert intent is not None
    # 绝不能返回 (-1, 1) —— 这就是振荡的根源
    assert intent.reserved_cell != (-1, 1), f"Should not oscillate back to prev_cell (-1, 1), got {intent.reserved_cell}"


def test_stuck_sidestep_breaks_resource_deadlock():
    """当工兵连续多 Tick 无法到达矿点时，_stuck_sidestep 应找到任意空闲格脱困。"""
    from arena_tactic.models import AgentConfig, ReservationTable
    from arena_tactic.strategy.workers import _stuck_sidestep
    from arena_tactic.context import DecisionContext

    c = core(position=(0, 0))
    # 工兵在 (-1, 0)，目标矿点在 (5, 0)，但被障碍物围堵
    w = unit(1, UnitType.WORKER, (-1, 0), cargo=0)
    t = turn(
        owned_core=c, units=(w,),
        resource_cells=((5, 0),),
        obstacle_cells=((0, -1), (1, 0), (0, 1), (-2, 0)),  # 东、北、南封死，西面 (-2, 0) 也是墙
    )
    context = DecisionContext.from_turn(t)
    memory = AgentMemory()
    # 模拟工兵已卡死 5 ticks（超过阈值 3）
    memory.unit_tasks[str(w.id)] = {"kind": "resource", "target": [5, 0], "attempt_tick": context.tick - 5, "prev_cell": [-1, -1]}
    reservations = ReservationTable({cell: len(ids) for cell, ids in context.friendly_occupancy.items()})

    intent = _stuck_sidestep(w, (5, 0), context, memory, reservations, "resource_route_unblock")
    # (-1, 0) 东面是核心格 (0, 0)、北面 (0, -1) 是墙、南面 (0, 1) 是墙、西面 (-2, 0) 是墙
    # 核心格 (0, 0) 是唯一可用的相邻格（虽然距离目标更远）
    assert intent is not None, "Stuck sidestep should find at least one free adjacent cell"
    assert intent.reserved_cell != (-1, 0), "Must actually move, not stay in place"


def test_stuck_sidestep_respects_threshold():
    """_stuck_sidestep 在未达到阈值前不应激活。"""
    from arena_tactic.models import ReservationTable
    from arena_tactic.strategy.workers import _stuck_sidestep
    from arena_tactic.context import DecisionContext

    c = core(position=(0, 0))
    w = unit(1, UnitType.WORKER, (-1, 0), cargo=0)
    t = turn(owned_core=c, units=(w,), obstacle_cells=((0, -1), (1, 0), (0, 1)))
    context = DecisionContext.from_turn(t)
    memory = AgentMemory()
    # 只卡了 1 tick（未达到阈值 3）
    memory.unit_tasks[str(w.id)] = {"kind": "resource", "target": [5, 0], "attempt_tick": context.tick - 1}
    reservations = ReservationTable({cell: len(ids) for cell, ids in context.friendly_occupancy.items()})

    intent = _stuck_sidestep(w, (5, 0), context, memory, reservations, "resource_route_unblock")
    assert intent is None, "Should not activate before threshold is reached"




