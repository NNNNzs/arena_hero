"""Minimal synchronous, controller-free behavior tree engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Callable


class BehaviorStatus(StrEnum):
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"


@dataclass(frozen=True, slots=True)
class NodeResult:
    status: BehaviorStatus
    reason: str = ""
    intent: Any = None


@dataclass(frozen=True, slots=True)
class NodeEvent:
    node_id: str
    status: BehaviorStatus
    reason: str


@dataclass(frozen=True, slots=True)
class Checkpoint:
    tick: int
    reason: str
    running_node_ids: tuple[str, ...]


@dataclass(slots=True)
class Blackboard:
    cursors: dict[str, int] = field(default_factory=dict)
    entered_ticks: dict[str, int] = field(default_factory=dict)
    retries: dict[str, int] = field(default_factory=dict)
    events: list[NodeEvent] = field(default_factory=list)

    def clear_tick_trace(self) -> None:
        self.events.clear()


@dataclass(frozen=True, slots=True)
class TickContext:
    tick: int


class Node:
    def __init__(self, node_id: str) -> None:
        if not node_id:
            raise ValueError("node_id is required")
        self.node_id = node_id

    def tick(self, context: TickContext, board: Blackboard) -> NodeResult:
        raise NotImplementedError

    def halt(self, context: TickContext, board: Blackboard) -> None:
        board.cursors.pop(self.node_id, None)
        board.entered_ticks.pop(self.node_id, None)
        board.retries.pop(self.node_id, None)

    def _record(self, board: Blackboard, result: NodeResult) -> NodeResult:
        board.events.append(NodeEvent(self.node_id, result.status, result.reason))
        return result


class Condition(Node):
    def __init__(self, node_id: str, predicate: Callable[[TickContext, Blackboard], bool]) -> None:
        super().__init__(node_id)
        self.predicate = predicate

    def tick(self, context: TickContext, board: Blackboard) -> NodeResult:
        return self._record(board, NodeResult(BehaviorStatus.SUCCESS if self.predicate(context, board) else BehaviorStatus.FAILURE))


class Action(Node):
    def __init__(self, node_id: str, action: Callable[[TickContext, Blackboard], BehaviorStatus | NodeResult], *, on_halt: Callable[[TickContext, Blackboard], None] | None = None) -> None:
        super().__init__(node_id)
        self.action, self.on_halt = action, on_halt

    def tick(self, context: TickContext, board: Blackboard) -> NodeResult:
        value = self.action(context, board)
        result = value if isinstance(value, NodeResult) else NodeResult(value)
        return self._record(board, result)

    def halt(self, context: TickContext, board: Blackboard) -> None:
        if self.on_halt is not None:
            self.on_halt(context, board)
        super().halt(context, board)


class Composite(Node):
    def __init__(self, node_id: str, children: tuple[Node, ...]) -> None:
        super().__init__(node_id)
        self.children = children

    def halt(self, context: TickContext, board: Blackboard) -> None:
        index = board.cursors.get(self.node_id)
        if index is not None and index < len(self.children):
            self.children[index].halt(context, board)
        super().halt(context, board)


class Sequence(Composite):
    def tick(self, context: TickContext, board: Blackboard) -> NodeResult:
        start = board.cursors.get(self.node_id, 0)
        for index in range(start, len(self.children)):
            result = self.children[index].tick(context, board)
            if result.status is BehaviorStatus.RUNNING:
                board.cursors[self.node_id] = index
                return self._record(board, result)
            if result.status is BehaviorStatus.FAILURE:
                board.cursors.pop(self.node_id, None)
                return self._record(board, result)
        board.cursors.pop(self.node_id, None)
        return self._record(board, NodeResult(BehaviorStatus.SUCCESS))


class Selector(Composite):
    """Reactive priority selector: earlier children may interrupt a runner."""

    def tick(self, context: TickContext, board: Blackboard) -> NodeResult:
        previous = board.cursors.get(self.node_id)
        for index, child in enumerate(self.children):
            result = child.tick(context, board)
            if result.status is BehaviorStatus.FAILURE:
                continue
            if previous is not None and previous != index:
                self.children[previous].halt(context, board)
            if result.status is BehaviorStatus.RUNNING:
                board.cursors[self.node_id] = index
            else:
                board.cursors.pop(self.node_id, None)
            return self._record(board, result)
        board.cursors.pop(self.node_id, None)
        return self._record(board, NodeResult(BehaviorStatus.FAILURE))


class Decorator(Node):
    def __init__(self, node_id: str, child: Node) -> None:
        super().__init__(node_id)
        self.child = child

    def halt(self, context: TickContext, board: Blackboard) -> None:
        self.child.halt(context, board)
        super().halt(context, board)


class Timeout(Decorator):
    def __init__(self, node_id: str, child: Node, *, ticks: int) -> None:
        super().__init__(node_id, child)
        if ticks <= 0:
            raise ValueError("ticks must be positive")
        self.ticks = ticks

    def tick(self, context: TickContext, board: Blackboard) -> NodeResult:
        entered = board.entered_ticks.setdefault(self.node_id, context.tick)
        if context.tick - entered >= self.ticks:
            self.child.halt(context, board)
            board.entered_ticks.pop(self.node_id, None)
            return self._record(board, NodeResult(BehaviorStatus.FAILURE, "TIMEOUT"))
        result = self.child.tick(context, board)
        if result.status is not BehaviorStatus.RUNNING:
            board.entered_ticks.pop(self.node_id, None)
        return self._record(board, result)


class Retry(Decorator):
    def __init__(self, node_id: str, child: Node, *, attempts: int) -> None:
        super().__init__(node_id, child)
        if attempts <= 0:
            raise ValueError("attempts must be positive")
        self.attempts = attempts

    def tick(self, context: TickContext, board: Blackboard) -> NodeResult:
        for _ in range(self.attempts):
            result = self.child.tick(context, board)
            if result.status is not BehaviorStatus.FAILURE:
                return self._record(board, result)
        return self._record(board, NodeResult(BehaviorStatus.FAILURE, "RETRY_EXHAUSTED"))


class AbortIf(Decorator):
    def __init__(self, node_id: str, child: Node, predicate: Callable[[TickContext, Blackboard], bool]) -> None:
        super().__init__(node_id, child)
        self.predicate = predicate

    def tick(self, context: TickContext, board: Blackboard) -> NodeResult:
        if self.predicate(context, board):
            self.child.halt(context, board)
            return self._record(board, NodeResult(BehaviorStatus.FAILURE, "ABORTED"))
        return self._record(board, self.child.tick(context, board))


class Tree:
    def __init__(self, tree_id: str, root: Node) -> None:
        self.tree_id, self.root = tree_id, root

    def tick(self, tick: int, board: Blackboard) -> NodeResult:
        board.clear_tick_trace()
        return self.root.tick(TickContext(tick), board)

    def halt(self, tick: int, board: Blackboard, reason: str) -> Checkpoint:
        running = tuple(sorted(board.cursors))
        self.root.halt(TickContext(tick), board)
        return Checkpoint(tick, reason, running)
