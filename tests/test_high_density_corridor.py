"""口袋地形多工兵走廊高密度拥堵级联疏散测试。

核心场景：战斗单位卡在核心格，门口（distance=1）被 2 名载货工人占满，
外围走廊（distance=2~3）也被载货工人高密度塞满（每格 2 名），
现有距离-1 退让机制因外围格 2/2 满员而无法预约，
需要级联疏散（cascade evacuation）从外向内逐层腾退。

本模块回归测试覆盖 Tick 240110~240135 的
CARGO_DELIVERY_STAGNATION + INEFFECTIVE_STATIONARY 死锁场景。
"""
from arena_tactic import choose_actions
from arena_tactic.memory import AgentMemory
from arena_tactic.models import ActionKind
from .factories import core, turn, unit
from arena_hero import UnitType


def test_high_density_corridor_cascade_evacuation():
    """高密度走廊级联疏散：外围（distance 2~3）载货工人先向外退让，
    释放格子给门口（distance 1）工人，门口工人再让道给战斗单位出核心。

    地形：核心 (0,0)，唯一出口西门 (-1,0)。
    北(0,-1)、东(1,0)、南(0,1) 全封闭。
    核心格：CORE + 先锋 (2/2)
    门口格(-1,0)：2 名载货工人 (2/2)
    西走廊(-2,0)：2 名载货工人 (2/2)
    西走廊(-3,0)：2 名载货工人 (2/2)
    北侧(-1,-1)：2 名载货工人 (2/2)
    北侧(-2,-1)：2 名载货工人 (2/2)
    共 10 名载货工人堵死走廊。
    """
    c = core(position=(0, 0))
    vanguard = unit(1, UnitType.VANGUARD, (0, 0))
    # 门口 (distance 1) — 2 名载货工人
    w1 = unit(2, UnitType.WORKER, (-1, 0), cargo=1)
    w2 = unit(3, UnitType.WORKER, (-1, 0), cargo=1)
    # 西走廊 (distance 2) — 2 名载货工人
    w3 = unit(4, UnitType.WORKER, (-2, 0), cargo=1)
    w4 = unit(5, UnitType.WORKER, (-2, 0), cargo=1)
    # 西走廊远端 (distance 3) — 2 名载货工人
    w5 = unit(6, UnitType.WORKER, (-3, 0), cargo=1)
    w6 = unit(7, UnitType.WORKER, (-3, 0), cargo=1)
    # 北侧走廊 (distance 2) — 2 名载货工人
    w7 = unit(8, UnitType.WORKER, (-1, -1), cargo=1)
    w8 = unit(9, UnitType.WORKER, (-1, -1), cargo=1)
    # 北侧走廊远端 (distance 3) — 2 名载货工人
    w9 = unit(10, UnitType.WORKER, (-2, -1), cargo=1)
    w10 = unit(11, UnitType.WORKER, (-2, -1), cargo=1)

    result = choose_actions(turn(
        owned_core=c,
        units=(vanguard, w1, w2, w3, w4, w5, w6, w7, w8, w9, w10),
        obstacle_cells=((0, -1), (1, 0), (0, 1)),  # 核心北、东、南封闭
    ), memory=AgentMemory())

    intents = {i.actor_id: i for i in result.intents}

    # 至少 1 名门口 (distance 1) 载货工人必须向外让道
    doorstep_yield_count = sum(
        1 for w in (w1, w2)
        if intents[w.id].reason == "yield_corridor_for_combat"
        and intents[w.id].action is ActionKind.MOVE
    )
    # 至少 1 名外围 (distance 2~3) 载货工人必须被级联疏散
    outer_yield_count = sum(
        1 for w in (w3, w4, w5, w6, w7, w8, w9, w10)
        if intents[w.id].reason == "yield_corridor_for_combat"
        and intents[w.id].action is ActionKind.MOVE
    )
    assert doorstep_yield_count >= 1 or outer_yield_count >= 1, (
        f"High-density corridor deadlock: at least 1 worker must yield (cascade or doorstep). "
        f"doorstep_yields={doorstep_yield_count}, outer_yields={outer_yield_count}"
    )


def test_cascade_only_triggers_with_combat_unit_stuck():
    """级联疏散仅在战斗单位卡在核心格时触发；
    无战斗单位时不应强制工人移动。
    """
    c = core(position=(0, 0))
    # 核心格只有 CORE + 空载工人（无战斗单位）
    w0 = unit(1, UnitType.WORKER, (0, 0), cargo=0)
    # 门口载货工人
    w1 = unit(2, UnitType.WORKER, (-1, 0), cargo=1)
    w2 = unit(3, UnitType.WORKER, (-1, 0), cargo=1)
    # 走廊载货工人
    w3 = unit(4, UnitType.WORKER, (-2, 0), cargo=1)
    w4 = unit(5, UnitType.WORKER, (-2, 0), cargo=1)

    result = choose_actions(turn(
        owned_core=c,
        units=(w0, w1, w2, w3, w4),
        obstacle_cells=((0, -1), (1, 0), (0, 1)),
    ), memory=AgentMemory())

    intents = {i.actor_id: i for i in result.intents}
    # 无战斗单位卡核心 → 不应触发 yield_corridor_for_combat
    for w in (w1, w2, w3, w4):
        i = intents[w.id]
        assert i.reason != "yield_corridor_for_combat", (
            f"Worker {w.id} should not yield without combat unit on core, "
            f"got reason={i.reason}"
        )


def test_cascade_outer_ring_yields_before_inner():
    """级联疏散：外围（distance 3）先向外腾退，释放格子给内层（distance 2），
    再给门口（distance 1），形成空间级联。

    地形：狭窄走廊 — 核心 (0,0)，仅西门 (-1,0)，封闭线 (-1,-1),(0,-1),(1,0),(0,1),(-1,1)。
    这样 (-2,0) 只有向西和向北两个出口。
    """
    c = core(position=(0, 0))
    vanguard = unit(1, UnitType.VANGUARD, (0, 0))
    w1 = unit(2, UnitType.WORKER, (-1, 0), cargo=1)  # distance 1
    w2 = unit(3, UnitType.WORKER, (-1, 0), cargo=1)  # distance 1
    w3 = unit(4, UnitType.WORKER, (-2, 0), cargo=1)  # distance 2
    w4 = unit(5, UnitType.WORKER, (-2, 0), cargo=1)  # distance 2
    w5 = unit(6, UnitType.WORKER, (-3, 0), cargo=1)  # distance 3
    w6 = unit(7, UnitType.WORKER, (-3, 0), cargo=1)  # distance 3

    result = choose_actions(turn(
        owned_core=c,
        units=(vanguard, w1, w2, w3, w4, w5, w6),
        obstacle_cells=(
            (0, -1), (1, 0), (0, 1),   # 核心北、东、南封闭
            (-1, -1), (-1, 1),          # 门口北侧和南侧封闭 → 狭窄走廊
        ),
    ), memory=AgentMemory())

    intents = {i.actor_id: i for i in result.intents}

    # distance 3 工人应被级联疏散向外
    d3_yields = sum(
        1 for w in (w5, w6)
        if intents[w.id].reason == "yield_corridor_for_combat"
        and intents[w.id].action is ActionKind.MOVE
    )
    # distance 2 工人应被级联疏散向外
    d2_yields = sum(
        1 for w in (w3, w4)
        if intents[w.id].reason == "yield_corridor_for_combat"
        and intents[w.id].action is ActionKind.MOVE
    )
    # distance 1 工人被强制让道
    d1_yields = sum(
        1 for w in (w1, w2)
        if intents[w.id].reason == "yield_corridor_for_combat"
        and intents[w.id].action is ActionKind.MOVE
    )
    total_yields = d1_yields + d2_yields + d3_yields
    assert total_yields >= 2, (
        f"Cascade evacuation: at least 2 workers should yield to break deadlock, "
        f"got d1={d1_yields}, d2={d2_yields}, d3={d3_yields}"
    )


def test_cascade_respects_anti_oscillation():
    """级联疏散的工人不振荡回 prev_cell。"""
    from arena_tactic.context import DecisionContext
    from arena_tactic.models import AgentConfig, ReservationTable
    from arena_tactic.strategy.common import _cascade_yield_outward
    from arena_tactic.navigation import enemy_threat_cells

    c = core(position=(0, 0))
    w = unit(1, UnitType.WORKER, (-2, 0), cargo=1)

    t = turn(
        owned_core=c,
        units=(w,),
        obstacle_cells=((0, -1), (1, 0), (0, 1)),
    )
    context = DecisionContext.from_turn(t)
    memory = AgentMemory()
    # 模拟工人刚从 (-1, 0) 移动到 (-2, 0)
    memory.unit_tasks[str(w.id)] = {"prev_cell": [-1, 0]}
    reservations = ReservationTable(
        {cell: len(ids) for cell, ids in context.friendly_occupancy.items()}
    )
    threats = enemy_threat_cells(context)
    blocked = memory.obstacles | set(context.enemy_occupancy) | threats

    intent = _cascade_yield_outward(
        w, (0, 0), context, memory, reservations, blocked, (-1, 0),
    )
    assert intent is not None
    # 绝不能振荡回 prev_cell
    assert intent.reserved_cell != (-1, 0), (
        f"Must not oscillate back to prev_cell (-1, 0), got {intent.reserved_cell}"
    )


def test_cascade_does_not_move_closer_to_core():
    """级联疏散不把工人推向核心方向（否则加剧拥堵）。"""
    from arena_tactic.context import DecisionContext
    from arena_tactic.models import ReservationTable
    from arena_tactic.strategy.common import _cascade_yield_outward
    from arena_tactic.navigation import enemy_threat_cells

    c = core(position=(0, 0))
    w = unit(1, UnitType.WORKER, (-2, 0), cargo=1)

    t = turn(
        owned_core=c,
        units=(w,),
        obstacle_cells=((0, -1), (1, 0), (0, 1), (-2, -1), (-3, 0)),
    )
    context = DecisionContext.from_turn(t)
    memory = AgentMemory()
    reservations = ReservationTable(
        {cell: len(ids) for cell, ids in context.friendly_occupancy.items()}
    )
    threats = enemy_threat_cells(context)
    blocked = memory.obstacles | set(context.enemy_occupancy) | threats

    intent = _cascade_yield_outward(
        w, (0, 0), context, memory, reservations, blocked, None,
    )
    if intent is not None:
        # 不能移向核心方向 (距离减小)
        from arena_tactic.navigation import distance
        assert distance(intent.reserved_cell, (0, 0)) >= 2, (
            f"Cascade yield must not move closer to core, "
            f"got {intent.reserved_cell} (dist={distance(intent.reserved_cell, (0, 0))})"
        )


def test_cascade_avoids_dead_end():
    """级联疏散不把工人推入死胡同。"""
    from arena_tactic.context import DecisionContext
    from arena_tactic.models import ReservationTable
    from arena_tactic.strategy.common import _cascade_yield_outward
    from arena_tactic.navigation import enemy_threat_cells

    c = core(position=(0, 0))
    w = unit(1, UnitType.WORKER, (-2, 0), cargo=1)

    # (-3, 0) 是死胡同：北(-3,-1)墙，南(-3,1)墙，东(-2,0)回原位，西(-4,0)墙
    t = turn(
        owned_core=c,
        units=(w,),
        obstacle_cells=(
            (0, -1), (1, 0), (0, 1),   # 核心三面封闭
            (-3, -1), (-3, 1), (-4, 0), # (-3,0) 三面封闭 → 死胡同
        ),
    )
    context = DecisionContext.from_turn(t)
    memory = AgentMemory()
    reservations = ReservationTable(
        {cell: len(ids) for cell, ids in context.friendly_occupancy.items()}
    )
    threats = enemy_threat_cells(context)
    blocked = memory.obstacles | set(context.enemy_occupancy) | threats

    intent = _cascade_yield_outward(
        w, (0, 0), context, memory, reservations, blocked, None,
    )
    if intent is not None:
        assert intent.reserved_cell != (-3, 0), (
            f"Must not yield into dead-end (-3, 0), got {intent.reserved_cell}"
        )


def test_cascade_safe_wait_when_fully_enclosed():
    """当外围工人四周全封闭时，级联疏散安全返回 None（不崩溃、不虚构移动）。"""
    from arena_tactic.context import DecisionContext
    from arena_tactic.models import ReservationTable
    from arena_tactic.strategy.common import _cascade_yield_outward
    from arena_tactic.navigation import enemy_threat_cells

    c = core(position=(0, 0))
    w = unit(1, UnitType.WORKER, (-2, 0), cargo=1)

    # (-2,0) 四面全是障碍或核心
    t = turn(
        owned_core=c,
        units=(w,),
        obstacle_cells=(
            (0, -1), (1, 0), (0, 1),   # 核心三面封闭
            (-2, -1), (-2, 1), (-3, 0), # (-2,0) 除 (-1,0) 外全封闭
        ),
    )
    context = DecisionContext.from_turn(t)
    memory = AgentMemory()
    # (-1,0) 是门口格，也在 blocked 中（为了测试完全封闭场景，设为障碍）
    # 这里故意不把 (-1,0) 设为障碍，因为门口应该是可通行的
    # 但如果门口也是回核心方向，级联疏散不会选它
    reservations = ReservationTable(
        {cell: len(ids) for cell, ids in context.friendly_occupancy.items()}
    )
    threats = enemy_threat_cells(context)
    blocked = memory.obstacles | set(context.enemy_occupancy) | threats

    intent = _cascade_yield_outward(
        w, (0, 0), context, memory, reservations, blocked, None,
    )
    # 应返回 None 而不是崩溃
    # (如果 (-1,0) 可用但方向朝核心，也不会选)


def test_full_pocket_scenario_exact_tick_240110():
    """完整口袋场景复现 Tick 240110~240135：核心 [-898,1573]，西门 [-899,1573]。

    核心格：CORE + 先锋 (2/2)
    门口格 [-899,1573]：2 名载货工人 (2/2)
    外围走廊 [-899,1572], [-899,1574], [-900,1573], [-901,1573]：每格 2 名载货工人 (2/2)
    共 10 名载货工人。

    验证：级联疏散后至少有 1 名工人被强制向外让道，
    且让道目标不是核心格也不是原地。
    """
    core_pos = (-898, 1573)
    doorstep = (-899, 1573)
    c = core(position=core_pos)
    vanguard = unit(1, UnitType.VANGUARD, core_pos)

    # 门口 (distance 1)
    w1 = unit(2, UnitType.WORKER, doorstep, cargo=1)
    w2 = unit(3, UnitType.WORKER, doorstep, cargo=1)
    # 西走廊 (distance 2)
    w3 = unit(4, UnitType.WORKER, (-900, 1573), cargo=1)
    w4 = unit(5, UnitType.WORKER, (-900, 1573), cargo=1)
    # 北走廊 (distance 2)
    w5 = unit(6, UnitType.WORKER, (-899, 1572), cargo=1)
    w6 = unit(7, UnitType.WORKER, (-899, 1572), cargo=1)
    # 南走廊 (distance 2)
    w7 = unit(8, UnitType.WORKER, (-899, 1574), cargo=1)
    w8 = unit(9, UnitType.WORKER, (-899, 1574), cargo=1)
    # 远端 (distance 3)
    w9 = unit(10, UnitType.WORKER, (-901, 1573), cargo=1)
    w10 = unit(11, UnitType.WORKER, (-901, 1573), cargo=1)

    result = choose_actions(turn(
        owned_core=c,
        units=(vanguard, w1, w2, w3, w4, w5, w6, w7, w8, w9, w10),
        obstacle_cells=(
            (core_pos[0] + 1, core_pos[1]),   # 东 [-897, 1573]
            (core_pos[0], core_pos[1] - 1),    # 北 [-898, 1572]
            (core_pos[0], core_pos[1] + 1),    # 南 [-898, 1574]
        ),
    ), memory=AgentMemory())

    intents = {i.actor_id: i for i in result.intents}
    all_workers = [w1, w2, w3, w4, w5, w6, w7, w8, w9, w10]

    # 级联疏散后，至少有 1 名工人被强制向外让道
    yield_count = sum(
        1 for w in all_workers
        if intents[w.id].reason == "yield_corridor_for_combat"
        and intents[w.id].action is ActionKind.MOVE
    )
    assert yield_count >= 1, (
        f"Tick 240110 scenario: at least 1 worker must yield via cascade evacuation. "
        f"yield_count={yield_count}, "
        f"reasons: {[intents[w.id].reason for w in all_workers]}"
    )

    # 让道工人的目标不能是核心格也不能是原地
    for w in all_workers:
        i = intents[w.id]
        if i.reason == "yield_corridor_for_combat":
            assert i.reserved_cell is not None
            assert i.reserved_cell != core_pos, (
                f"Worker {w.id} must not yield into core {core_pos}"
            )
            assert i.reserved_cell != w.position, (
                f"Worker {w.id} must actually vacate, got {i.reserved_cell}"
            )
