from arena_tactic.behavior_tree import AbortIf, Action, BehaviorStatus, Blackboard, Condition, Retry, Selector, Sequence, Timeout, Tree


def test_running_node_resumes_next_tick():
    calls: list[int] = []

    def progress(context, board):
        calls.append(context.tick)
        return BehaviorStatus.RUNNING if len(calls) == 1 else BehaviorStatus.SUCCESS

    tree = Tree("worker", Sequence("root", (Condition("ready", lambda c, b: True), Action("move", progress))))
    board = Blackboard()
    assert tree.tick(1, board).status is BehaviorStatus.RUNNING
    assert tree.tick(2, board).status is BehaviorStatus.SUCCESS
    assert calls == [1, 2]


def test_halt_clears_running_child_on_preemption():
    halted: list[str] = []
    action = Action("move", lambda c, b: BehaviorStatus.RUNNING, on_halt=lambda c, b: halted.append("move"))
    tree = Tree("worker", Sequence("root", (action,)))
    board = Blackboard()
    tree.tick(1, board)
    checkpoint = tree.halt(2, board, "PREEMPTED")

    assert halted == ["move"]
    assert board.cursors == {}
    assert checkpoint.reason == "PREEMPTED"


def test_worker_block_timeout_requests_replan():
    tree = Tree("worker", Timeout("blocked_timeout", Action("move", lambda c, b: BehaviorStatus.RUNNING), ticks=2))
    board = Blackboard()
    assert tree.tick(1, board).status is BehaviorStatus.RUNNING
    assert tree.tick(2, board).status is BehaviorStatus.RUNNING
    result = tree.tick(3, board)

    assert result.status is BehaviorStatus.FAILURE
    assert result.reason == "TIMEOUT"
    assert "blocked_timeout" not in board.entered_ticks


def test_vanguard_rescue_interrupts_running_guard():
    rescue = {"active": False}
    guard_halts: list[bool] = []
    tree = Tree(
        "vanguard",
        Selector("root", (
            Sequence("rescue", (Condition("rescue_needed", lambda c, b: rescue["active"]), Action("rescue_move", lambda c, b: BehaviorStatus.SUCCESS))),
            Action("guard", lambda c, b: BehaviorStatus.RUNNING, on_halt=lambda c, b: guard_halts.append(True)),
        )),
    )
    board = Blackboard()
    assert tree.tick(1, board).status is BehaviorStatus.RUNNING
    rescue["active"] = True
    assert tree.tick(2, board).status is BehaviorStatus.SUCCESS
    assert guard_halts == [True]


def test_retry_and_abort_if_are_controller_free_contracts():
    attempts = {"count": 0}
    retry = Retry("retry", Action("try", lambda c, b: attempts.__setitem__("count", attempts["count"] + 1) or BehaviorStatus.FAILURE), attempts=2)
    tree = Tree("unit", AbortIf("abort", retry, lambda c, b: c.tick == 2))
    board = Blackboard()
    assert tree.tick(1, board).status is BehaviorStatus.FAILURE
    assert attempts["count"] == 2
    assert tree.tick(2, board).reason == "ABORTED"
