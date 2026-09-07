"""单通道口袋死锁疏散测试。

核心场景：战斗单位占用核心格，门口被载货工人堵死，形成确定性对换死锁。
修复后载货工人必须向外让道，释放门口格子让战斗单位走出核心格。
"""
from arena_tactic import choose_actions
from arena_tactic.memory import AgentMemory
from arena_tactic.models import ActionKind
from .factories import core, turn, unit
from arena_hero import UnitType


def test_combat_on_core_cargo_workers_yield_to_break_pocket_deadlock():
    """单通道口袋死锁疏散：先锋占核心格 + 门口2名载货工人死锁时，工人必须向外退让。

    地形：核心 (0,0)，北(0,-1)、东(1,0)、南(0,1) 全封闭，
    西(-1,0) 是唯一门口通道。
    核心格：CORE + 先锋 (2/2)
    门口格(-1,0)：2 名载货工人等待入库 (2/2)

    修复前：先锋无法 deploy_sidestep（门口满载），工人等待入库（核心满载）→ 死锁。
    修复后：门口载货工人向外让出门口格子，先锋下回合可走出核心。
    """
    c = core(position=(0, 0))
    # 先锋站在核心格上
    vanguard = unit(1, UnitType.VANGUARD, (0, 0))
    # 2 名载货工人堵在唯一门口
    w1 = unit(2, UnitType.WORKER, (-1, 0), cargo=1)
    w2 = unit(3, UnitType.WORKER, (-1, 0), cargo=1)

    # 单通道口袋：北、东、南封闭
    result = choose_actions(turn(
        owned_core=c,
        units=(vanguard, w1, w2),
        obstacle_cells=((0, -1), (1, 0), (0, 1)),
    ), memory=AgentMemory())

    intents = {i.actor_id: i for i in result.intents}

    # 门口载货工人中必须至少有一人被强制向外让道
    worker_intents = [intents[w1.id], intents[w2.id]]
    yield_count = sum(
        1 for i in worker_intents
        if i.reason == "yield_corridor_for_combat" and i.action is ActionKind.MOVE
    )
    assert yield_count >= 1, (
        f"At least 1 cargo worker must yield outward to break pocket deadlock, "
        f"got reasons: {[i.reason for i in worker_intents]}, "
        f"actions: {[i.action for i in worker_intents]}"
    )
    # 让道工人的 reserved_cell 必须不是核心格也不是原地
    for i in worker_intents:
        if i.reason == "yield_corridor_for_combat":
            assert i.reserved_cell is not None
            assert i.reserved_cell != (0, 0), "Must not step into core"
            assert i.reserved_cell != (-1, 0), "Must actually vacate doorstep"


def test_combat_on_core_no_yield_when_no_cargo_workers_at_doorstep():
    """当门口没有载货工人等待时，不触发走廊退让（正常路径不受影响）。"""
    c = core(position=(0, 0))
    vanguard = unit(1, UnitType.VANGUARD, (0, 0))
    # 门口工人没有货物
    w1 = unit(2, UnitType.WORKER, (-1, 0), cargo=0)

    result = choose_actions(turn(
        owned_core=c,
        units=(vanguard, w1),
        obstacle_cells=((0, -1), (1, 0), (0, 1)),
    ), memory=AgentMemory())

    intents = {i.actor_id: i for i in result.intents}
    worker_intent = intents[w1.id]
    # 空载工人不应触发 yield_corridor_for_combat
    assert worker_intent.reason != "yield_corridor_for_combat", (
        f"Empty worker should not yield for combat, got: {worker_intent.reason}"
    )


def test_combat_on_core_yield_uses_all_available_doorstep_cells():
    """让道工人必须走向门口格之外的安全格，不能走入死胡同。

    地形：核心 (0,0)，北(0,-1)、东(1,0)、南(0,1) 封闭，
    西(-1,0) 是唯一门口。
    (-1,-1) 北侧也是死胡同：(-1,-2) 墙、(0,-1) 墙。
    (-1,1) 南侧和 (-2,0) 西侧是可通行区域。
    """
    c = core(position=(0, 0))
    vanguard = unit(1, UnitType.VANGUARD, (0, 0))
    w1 = unit(2, UnitType.WORKER, (-1, 0), cargo=1)
    w2 = unit(3, UnitType.WORKER, (-1, 0), cargo=1)

    result = choose_actions(turn(
        owned_core=c,
        units=(vanguard, w1, w2),
        obstacle_cells=(
            (0, -1), (1, 0), (0, 1),    # 核心北、东、南封闭
            (-1, -2), (-2, -1),          # 门口北侧死胡同
        ),
    ), memory=AgentMemory())

    intents = {i.actor_id: i for i in result.intents}
    worker_intents = [intents[w1.id], intents[w2.id]]
    yield_intents = [
        i for i in worker_intents
        if i.reason == "yield_corridor_for_combat"
    ]
    assert len(yield_intents) >= 1, (
        f"At least 1 worker must yield, got: {[i.reason for i in worker_intents]}"
    )
    for i in yield_intents:
        # 不能走入死胡同 (-1, -1)
        assert i.reserved_cell != (-1, -1), (
            f"Must not yield into dead-end pocket (-1, -1), got {i.reserved_cell}"
        )


def test_combat_on_core_yield_ranger_also_triggers():
    """游侠同样触发门口工人退让（不限于先锋）。"""
    c = core(position=(0, 0))
    ranger = unit(1, UnitType.RANGER, (0, 0))
    w1 = unit(2, UnitType.WORKER, (-1, 0), cargo=1)
    w2 = unit(3, UnitType.WORKER, (-1, 0), cargo=1)

    result = choose_actions(turn(
        owned_core=c,
        units=(ranger, w1, w2),
        obstacle_cells=((0, -1), (1, 0), (0, 1)),
    ), memory=AgentMemory())

    intents = {i.actor_id: i for i in result.intents}
    worker_intents = [intents[w1.id], intents[w2.id]]
    yield_count = sum(
        1 for i in worker_intents
        if i.reason == "yield_corridor_for_combat" and i.action is ActionKind.MOVE
    )
    assert yield_count >= 1, (
        f"Ranger on core should trigger cargo worker yield, "
        f"got: {[i.reason for i in worker_intents]}"
    )


def test_combat_on_core_yield_anti_oscillation():
    """让道工人不振荡回上一格（prev_cell 防振荡机制）。"""
    from arena_tactic.context import DecisionContext
    from arena_tactic.models import AgentConfig, ReservationTable
    from arena_tactic.strategy.common import _force_doorstep_yield

    c = core(position=(0, 0))
    w = unit(1, UnitType.WORKER, (-1, 0), cargo=1)

    t = turn(
        owned_core=c,
        units=(w,),
        obstacle_cells=((0, -1), (1, 0), (0, 1)),
    )
    context = DecisionContext.from_turn(t)
    memory = AgentMemory()
    # 模拟工人刚从 (-1, 1) 移动到 (-1, 0)
    memory.unit_tasks[str(w.id)] = {"prev_cell": [-1, 1]}
    reservations = ReservationTable(
        {cell: len(ids) for cell, ids in context.friendly_occupancy.items()}
    )
    blocked = memory.obstacles | set(context.enemy_occupancy)

    intent = _force_doorstep_yield(
        w, (0, 0), context, memory, reservations, blocked,
    )
    assert intent is not None
    # 绝不能振荡回 prev_cell
    assert intent.reserved_cell != (-1, 1), (
        f"Must not oscillate back to prev_cell (-1, 1), got {intent.reserved_cell}"
    )


def test_combat_on_core_yield_all_exits_blocked_safe_wait():
    """当门口工人四周全被封死时，安全等待不崩溃。"""
    c = core(position=(0, 0))
    vanguard = unit(1, UnitType.VANGUARD, (0, 0))
    w1 = unit(2, UnitType.WORKER, (-1, 0), cargo=1)
    w2 = unit(3, UnitType.WORKER, (-1, 0), cargo=1)

    # 门口工人周围全是障碍：(-2,0), (-1,-1), (-1,1) 全封闭
    result = choose_actions(turn(
        owned_core=c,
        units=(vanguard, w1, w2),
        obstacle_cells=(
            (0, -1), (1, 0), (0, 1),   # 核心北、东、南封闭
            (-2, 0), (-1, -1), (-1, 1), # 门口工人四周全封闭
        ),
    ), memory=AgentMemory())

    intents = {i.actor_id: i for i in result.intents}
    # 工人如果无法让道，保持 cargo_doorstep_wait_for_entry（安全等待）
    for w in (w1, w2):
        i = intents[w.id]
        assert i.action is ActionKind.WAIT or i.action is ActionKind.MOVE, (
            f"Worker must either WAIT or MOVE, got {i.action}"
        )


def test_pocket_deadlock_full_scenario_with_observe_checks():
    """完整口袋死锁场景：模拟 Tick 239451 的核心 [-898, 1573] 状态。

    核心 [-898, 1573]，西门 [-899, 1573]。
    核心格：CORE + 先锋 entity_6ee037b7b2bc (2/2)
    门口格：2 名载货工人 (2/2)
    东、北、南三面绝壁。

    验证修复后门口至少一名工人让出门口格。
    """
    core_pos = (-898, 1573)
    doorstep = (-899, 1573)
    c = core(position=core_pos)
    vanguard = unit(1, UnitType.VANGUARD, core_pos)
    w1 = unit(2, UnitType.WORKER, doorstep, cargo=1)
    w2 = unit(3, UnitType.WORKER, doorstep, cargo=1)

    # 东(1,0)、北(0,-1)、南(0,1) 相对于核心都是绝壁
    result = choose_actions(turn(
        owned_core=c,
        units=(vanguard, w1, w2),
        obstacle_cells=(
            (core_pos[0] + 1, core_pos[1]),     # 东
            (core_pos[0], core_pos[1] - 1),      # 北
            (core_pos[0], core_pos[1] + 1),      # 南
        ),
    ), memory=AgentMemory())

    intents = {i.actor_id: i for i in result.intents}
    worker_intents = [intents[w1.id], intents[w2.id]]

    # 验证：至少一名工人向外让道（yield_corridor_for_combat）
    yield_count = sum(
        1 for i in worker_intents
        if i.reason == "yield_corridor_for_combat"
    )
    assert yield_count >= 1, (
        f"Pocket deadlock at {core_pos}: at least 1 worker must yield, "
        f"got reasons: {[i.reason for i in worker_intents]}"
    )

    # 让道的工人 reserved_cell 不能是核心格或门口原位
    for i in worker_intents:
        if i.reason == "yield_corridor_for_combat":
            assert i.reserved_cell != core_pos, "Must not step into core"
            assert i.reserved_cell != doorstep, "Must vacate doorstep"
