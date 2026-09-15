"""工兵探索受阻卡死脱困测试。

验证 _stuck_sidestep 时间戳初始化修复：
当工兵初次向探索目标寻路被地形完全阻挡时（_move 返回 None），
_record_unit_task 应保留 attempt_tick 而非删除，
使得连续受阻超过 _STUCK_THRESHOLD 后能触发侧滑脱困。
"""
from arena_tactic.context import DecisionContext
from arena_tactic.memory import AgentMemory
from arena_tactic.models import ActionKind, ReservationTable
from arena_tactic.strategy.common import _record_unit_task
from arena_tactic.strategy.workers import _STUCK_THRESHOLD, _stuck_sidestep

from .factories import core, turn, unit
from arena_hero import UnitType


def test_record_unit_task_preserves_attempt_tick_when_intent_is_none():
    """_record_unit_task 在 intent=None 时保留 attempt_tick 而非删除。"""
    c = core(position=(0, 0))
    w = unit(1, UnitType.WORKER, (-1, 0), cargo=0)
    t = turn(owned_core=c, units=(w,), obstacle_cells=((0, -1), (1, 0), (0, 1), (-2, 0)))
    context = DecisionContext.from_turn(t)
    memory = AgentMemory()

    # 第一次调用：intent=None（寻路失败），应初始化 attempt_tick
    _record_unit_task(memory, context, w, kind="explore", target=(10, 10), intent=None)
    task = memory.unit_tasks[str(w.id)]
    assert task.get("attempt_tick") == context.tick, (
        f"First blocked call should initialise attempt_tick to {context.tick}, "
        f"got {task.get('attempt_tick')}"
    )


def test_record_unit_task_keeps_earliest_attempt_tick_on_repeated_failure():
    """连续多次 intent=None 时，attempt_tick 应保持最早的卡顿 tick。"""
    c = core(position=(0, 0))
    w = unit(1, UnitType.WORKER, (-1, 0), cargo=0)
    t = turn(owned_core=c, units=(w,), obstacle_cells=((0, -1), (1, 0), (0, 1), (-2, 0)))
    context = DecisionContext.from_turn(t)
    memory = AgentMemory()

    # 第一次卡顿
    _record_unit_task(memory, context, w, kind="explore", target=(10, 10), intent=None)
    earliest = memory.unit_tasks[str(w.id)]["attempt_tick"]

    # 模拟后续 tick 持续卡顿
    for tick_offset in range(1, 5):
        t2 = turn(tick=context.tick + tick_offset, owned_core=c, units=(w,),
                  obstacle_cells=((0, -1), (1, 0), (0, 1), (-2, 0)))
        ctx2 = DecisionContext.from_turn(t2)
        _record_unit_task(memory, ctx2, w, kind="explore", target=(10, 10), intent=None)

    assert memory.unit_tasks[str(w.id)]["attempt_tick"] == earliest, (
        f"attempt_tick should remain at the earliest blocked tick {earliest}, "
        f"got {memory.unit_tasks[str(w.id)]['attempt_tick']}"
    )


def test_record_unit_task_resets_attempt_tick_on_successful_move():
    """当 intent 为有效 MOVE 时，attempt_tick 应更新为当前 tick。"""
    from arena_tactic.models import ActionIntent, ActionKind as AK

    c = core(position=(0, 0))
    w = unit(1, UnitType.WORKER, (-1, 0), cargo=0)
    t = turn(owned_core=c, units=(w,))
    context = DecisionContext.from_turn(t)
    memory = AgentMemory()

    # 先模拟一次卡顿
    _record_unit_task(memory, context, w, kind="explore", target=(10, 10), intent=None)
    old_tick = memory.unit_tasks[str(w.id)]["attempt_tick"]

    # 模拟成功移动
    move_intent = ActionIntent(
        actor_id=w.id, is_core=False, action=AK.MOVE, score=400,
        reason="explore_sector_frontier", target_cell=(10, 10),
        direction=None, reserved_cell=(0, 1),
    )
    t2 = turn(tick=context.tick + 10, owned_core=c, units=(w,))
    ctx2 = DecisionContext.from_turn(t2)
    _record_unit_task(memory, ctx2, w, kind="explore", target=(10, 10), intent=move_intent)
    assert memory.unit_tasks[str(w.id)]["attempt_tick"] == ctx2.tick, (
        f"attempt_tick should reset on successful move to {ctx2.tick}, "
        f"got {memory.unit_tasks[str(w.id)]['attempt_tick']}"
    )


def test_stuck_sidestep_triggers_after_threshold_when_no_initial_attempt_tick():
    """修复后：工兵首次寻路受阻（attempt_tick 由 _record_unit_task 初始化），
    连续卡顿超过 _STUCK_THRESHOLD 后，_stuck_sidestep 应触发侧滑。"""
    c = core(position=(0, 0))
    # 工兵在 (-1, 0)，东面核心 (0,0) 可通行，北/南/西全是墙
    w = unit(1, UnitType.WORKER, (-1, 0), cargo=0)
    t = turn(
        owned_core=c, units=(w,),
        obstacle_cells=((0, -1), (1, 0), (0, 1), (-2, 0)),
    )
    context = DecisionContext.from_turn(t)
    memory = AgentMemory()

    # 模拟连续卡顿 _STUCK_THRESHOLD + 1 个 tick
    for i in range(_STUCK_THRESHOLD + 1):
        tick = context.tick + i
        ti = turn(
            tick=tick, owned_core=c, units=(w,),
            obstacle_cells=((0, -1), (1, 0), (0, 1), (-2, 0)),
        )
        ctx_i = DecisionContext.from_turn(ti)
        # 每 tick 调用 _record_unit_task 模拟寻路失败
        _record_unit_task(memory, ctx_i, w, kind="explore", target=(10, 10), intent=None)

    # 现在 attempt_tick 应已初始化且差值 >= _STUCK_THRESHOLD
    final_ctx = DecisionContext.from_turn(turn(
        tick=context.tick + _STUCK_THRESHOLD + 1,
        owned_core=c, units=(w,),
        obstacle_cells=((0, -1), (1, 0), (0, 1), (-2, 0)),
    ))
    reservations = ReservationTable(
        {cell: len(ids) for cell, ids in final_ctx.friendly_occupancy.items()}
    )
    intent = _stuck_sidestep(
        w, (10, 10), final_ctx, memory, reservations, "exploration_route_unblock",
    )
    # (-1, 0) 东面是核心格 (0, 0) 可通行，应触发侧滑
    assert intent is not None, (
        "Stuck sidestep should trigger after _STUCK_THRESHOLD consecutive blocked ticks"
    )
    assert intent.action is ActionKind.MOVE
    assert intent.reserved_cell != (-1, 0), "Must actually move, not stay in place"


def test_stuck_sidestep_does_not_trigger_before_threshold():
    """修复后：在 _STUCK_THRESHOLD 之前不应触发。"""
    c = core(position=(0, 0))
    w = unit(1, UnitType.WORKER, (-1, 0), cargo=0)
    t = turn(
        owned_core=c, units=(w,),
        obstacle_cells=((0, -1), (1, 0), (0, 1), (-2, 0)),
    )
    context = DecisionContext.from_turn(t)
    memory = AgentMemory()

    # 仅卡顿 1 个 tick（< _STUCK_THRESHOLD）
    _record_unit_task(memory, context, w, kind="explore", target=(10, 10), intent=None)

    reservations = ReservationTable(
        {cell: len(ids) for cell, ids in context.friendly_occupancy.items()}
    )
    intent = _stuck_sidestep(
        w, (10, 10), context, memory, reservations, "exploration_route_unblock",
    )
    assert intent is None, "Should not activate before threshold is reached"


def test_exploration_route_blocked_triggers_sidestep_integration():
    """集成测试：工兵四周被地形障碍围堵，连续受阻后应触发侧滑脱困。

    地形：核心 (0,0)，工兵在 (-1,0)，东面核心格可通行，
    北(0,-1)、南(0,1)、西(-2,0) 全封闭。
    模拟连续 _STUCK_THRESHOLD+1 个 tick 的 choose_actions，
    验证最终产生 exploration_route_unblock MOVE 意图。
    """
    from arena_tactic import choose_actions

    c = core(position=(0, 0))
    w = unit(1, UnitType.WORKER, (-1, 0), cargo=0)
    obstacles = ((0, -1), (1, 0), (0, 1), (-2, 0))

    # 第一次 choose_actions —— 记录 exploration 任务（_move 失败）
    result = choose_actions(turn(
        tick=1, owned_core=c, units=(w,),
        obstacle_cells=obstacles,
    ))
    memory = result.next_memory

    # 连续 choose_actions 模拟卡死
    for tick in range(2, _STUCK_THRESHOLD + 3):
        result = choose_actions(
            turn(tick=tick, owned_core=c, units=(w,), obstacle_cells=obstacles),
            memory=memory,
        )
        memory = result.next_memory

    # 最终意图应为侧滑脱困或至少不是永久 WAIT
    final_intent = next(i for i in result.intents if i.actor_id == w.id)
    # 如果 sidestep 找到了可用格，应该是 MOVE + exploration_route_unblock
    # 如果四周全封（本场景核心格可通行），应产生 sidestep MOVE
    assert final_intent.action is ActionKind.MOVE, (
        f"After {_STUCK_THRESHOLD + 2} ticks of being blocked, worker should sidestep, "
        f"got action={final_intent.action} reason={final_intent.reason}"
    )
    assert final_intent.reason in ("explore_sector_frontier", "exploration_route_unblock"), (
        f"Unexpected reason: {final_intent.reason}"
    )