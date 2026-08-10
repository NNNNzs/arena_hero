"""Worker behavior-tree canary executed against the current authoritative Turn.

The executor proposes controller-independent intents only.  Runtime still owns
validation, current-Turn controller allocation, and one complete submission.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping
from uuid import UUID

from arena_hero import CoreState, UnitType, UnitView

from ..behavior_tree import (
    Action,
    BehaviorStatus,
    Blackboard,
    Condition,
    NodeResult,
    Selector,
    Sequence,
    Timeout,
    Tree,
)
from ..context import DecisionContext
from ..identity import entity_alias
from ..memory import AgentMemory
from ..models import ActionIntent, ActionKind, AgentConfig, Position, ReservationTable
from ..navigation import destination, distance, plan_step


_SECTORS: tuple[Position, ...] = ((1, 0), (0, 1), (-1, 0), (0, -1))


@dataclass(slots=True)
class _Frame:
    context: DecisionContext
    memory: AgentMemory
    config: AgentConfig
    deadline: float
    reservations: ReservationTable
    resource_targets: Mapping[UUID, Position]


@dataclass(frozen=True, slots=True)
class WorkerCanaryResult:
    intents: tuple[ActionIntent, ...]
    node_events: Mapping[UUID, tuple[object, ...]]


@dataclass(slots=True)
class WorkerCanaryPlanner:
    """Keep behavior cursors per living Worker without retaining controllers."""

    boards: dict[UUID, Blackboard] = field(default_factory=dict)
    trees: dict[UUID, Tree] = field(default_factory=dict)

    def propose(
        self,
        context: DecisionContext,
        memory: AgentMemory,
        config: AgentConfig,
        deadline: float,
    ) -> WorkerCanaryResult:
        self._discard_missing(context)
        occupancy = {cell: len(ids) for cell, ids in context.friendly_occupancy.items()}
        reservations = ReservationTable(occupancy=occupancy)
        targets = self._resource_targets(context, memory)
        frame = _Frame(context, memory, config, deadline, reservations, targets)
        intents: list[ActionIntent] = []
        events: dict[UUID, tuple[object, ...]] = {}
        for worker in sorted(context.workers, key=lambda item: item.id.bytes):
            board = self.boards.setdefault(worker.id, Blackboard())
            tree = self.trees.setdefault(worker.id, self._build_tree())
            result = tree.tick(context.tick, board, data={"frame": frame, "worker": worker})
            if isinstance(result.intent, ActionIntent):
                intents.append(result.intent)
            else:
                # A planner must never leave a currently controlled Worker to
                # the generic validator fallback.  This is deliberately a
                # defensive last line: normal route failures are translated by
                # their owning behavior below, while an unexpected tree
                # failure remains visible in the decision trace.
                intents.append(
                    ActionIntent(
                        worker.id,
                        False,
                        ActionKind.WAIT,
                        0,
                        "bt_worker_no_intent",
                    )
                )
            events[worker.id] = tuple(board.events)
        return WorkerCanaryResult(tuple(intents), events)

    def _discard_missing(self, context: DecisionContext) -> None:
        current = {worker.id for worker in context.workers}
        for worker_id in tuple(self.boards):
            if worker_id not in current:
                self.boards.pop(worker_id, None)
                self.trees.pop(worker_id, None)

    @staticmethod
    def _resource_targets(
        context: DecisionContext, memory: AgentMemory
    ) -> dict[UUID, Position]:
        """Assign each current visible node to at most one empty Worker."""
        blocked = memory.obstacles | memory.active_temporary_blocks(context.tick)
        available = sorted(cell for cell in context.resource_cells if cell not in blocked)
        targets: dict[UUID, Position] = {}
        scheduler_active = bool(memory.scheduler_assignments)
        for worker in sorted(context.workers, key=lambda item: item.id.bytes):
            if worker.cargo or not available:
                continue
            scheduled = memory.scheduler_assignments.get(entity_alias(worker.id) or "")
            target = scheduled.get("target") if isinstance(scheduled, dict) and scheduled.get("kind") == "HARVEST_RESOURCE" else None
            if isinstance(target, (list, tuple)) and len(target) == 2 and all(type(axis) is int for axis in target):
                cell = target[0], target[1]
                if cell in available:
                    targets[worker.id] = cell
                    available.remove(cell)
                continue
            if scheduler_active:
                continue
            selected = min(available, key=lambda cell: (distance(worker.position, cell), cell))
            targets[worker.id] = selected
            available.remove(selected)
        return targets

    @staticmethod
    def _frame(context) -> _Frame:
        frame = context.data.get("frame")
        if not isinstance(frame, _Frame):
            raise RuntimeError("Worker behavior tree requires a current decision frame")
        return frame

    @staticmethod
    def _worker(context) -> UnitView:
        worker = context.data.get("worker")
        if not isinstance(worker, UnitView) or worker.unit_type is not UnitType.WORKER:
            raise RuntimeError("Worker behavior tree requires a current Worker view")
        return worker

    def _build_tree(self) -> Tree:
        return Tree(
            "worker-canary-v1",
            Selector("worker.root", (
                Sequence("worker.emergency", (
                    Condition("worker.emergency.detect", self._emergency),
                    Selector("worker.emergency.response", (
                        Sequence("worker.emergency.deposit", (
                            Condition("worker.emergency.at_core_with_cargo", self._at_core_with_cargo),
                            Action("worker.emergency.deposit_action", self._deposit),
                        )),
                        Action("worker.emergency.retreat", self._return_to_core),
                    )),
                )),
                Sequence("worker.cargo", (
                    Condition("worker.cargo.present", self._has_cargo),
                    Selector("worker.cargo.response", (
                        Sequence("worker.cargo.deposit", (
                            Condition("worker.cargo.at_core", self._at_core_with_cargo),
                            Action("worker.cargo.deposit_action", self._deposit),
                        )),
                        Action("worker.cargo.return", self._return_to_core),
                    )),
                )),
                Sequence("worker.resource", (
                    Condition("worker.resource.assigned", self._has_resource_target),
                    Selector("worker.resource.response", (
                        Sequence("worker.resource.harvest", (
                            Condition("worker.resource.on_target", self._on_resource_target),
                            Action("worker.resource.harvest_action", self._harvest),
                        )),
                        Timeout(
                            "worker.resource.timeout",
                            Action("worker.resource.move", self._move_to_resource),
                            ticks=4,
                        ),
                        Action("worker.resource.replan", self._replan_resource),
                    )),
                )),
                Action("worker.scout", self._advance_frontier),
            )),
        )

    @classmethod
    def _emergency(cls, context, board: Blackboard) -> bool:
        frame, worker = cls._frame(context), cls._worker(context)
        return any(distance(worker.position, enemy.position) <= 2 for enemy in frame.context.enemies)

    @classmethod
    def _has_cargo(cls, context, board: Blackboard) -> bool:
        return bool(cls._worker(context).cargo)

    @classmethod
    def _at_core_with_cargo(cls, context, board: Blackboard) -> bool:
        frame, worker = cls._frame(context), cls._worker(context)
        return bool(
            worker.cargo
            and frame.context.core is not None
            and frame.context.core.state is CoreState.NORMAL
            and worker.position == frame.context.core.position
            and frame.context.resource_space > 0
        )

    @classmethod
    def _has_resource_target(cls, context, board: Blackboard) -> bool:
        frame, worker = cls._frame(context), cls._worker(context)
        return worker.id in frame.resource_targets

    @classmethod
    def _on_resource_target(cls, context, board: Blackboard) -> bool:
        frame, worker = cls._frame(context), cls._worker(context)
        return frame.resource_targets.get(worker.id) == worker.position and worker.position in frame.context.resource_cells

    @classmethod
    def _deposit(cls, context, board: Blackboard) -> NodeResult:
        worker = cls._worker(context)
        return NodeResult(
            BehaviorStatus.SUCCESS,
            "BT_WORKER_DEPOSIT_CARGO",
            ActionIntent(worker.id, False, ActionKind.DEPOSIT, 950, "bt_worker_deposit_cargo"),
        )

    @classmethod
    def _return_to_core(cls, context, board: Blackboard) -> NodeResult:
        frame, worker = cls._frame(context), cls._worker(context)
        core = frame.context.core
        if core is None:
            return cls._wait(worker, "BT_WORKER_WAITING_FOR_CORE")
        target = core.destination if core.state is CoreState.MOVING and core.destination else core.position
        result = cls._move(worker, target, "bt_worker_return_to_core", 900, frame)
        cls._record_move_task(worker, target, "return", result, frame)
        if result.status is BehaviorStatus.FAILURE:
            return cls._wait(worker, "BT_WORKER_RETURN_ROUTE_BLOCKED")
        return result

    @classmethod
    def _harvest(cls, context, board: Blackboard) -> NodeResult:
        worker = cls._worker(context)
        return NodeResult(
            BehaviorStatus.SUCCESS,
            "BT_WORKER_HARVEST_VISIBLE",
            ActionIntent(worker.id, False, ActionKind.HARVEST, 850, "bt_worker_harvest_visible", target_cell=worker.position),
        )

    @classmethod
    def _move_to_resource(cls, context, board: Blackboard) -> NodeResult:
        frame, worker = cls._frame(context), cls._worker(context)
        target = frame.resource_targets.get(worker.id)
        if target is None:
            return NodeResult(BehaviorStatus.FAILURE, "BT_WORKER_RESOURCE_ASSIGNMENT_LOST")
        if board.entered_ticks.get("worker.resource.timeout") is not None and board.cursors.get("worker.root") is not None:
            previous = board.retries.get("worker.resource.position")
            if previous is not None and previous != worker.position:
                board.entered_ticks.pop("worker.resource.timeout", None)
                board.retries["worker.resource.position"] = worker.position
        result = cls._move(worker, target, "bt_worker_move_to_visible_resource", 750, frame)
        cls._record_move_task(worker, target, "resource", result, frame)
        return result

    @classmethod
    def _replan_resource(cls, context, board: Blackboard) -> NodeResult:
        frame, worker = cls._frame(context), cls._worker(context)
        frame.memory.unit_tasks.pop(str(worker.id), None)
        board.retries.pop("worker.resource.position", None)
        return cls._wait(worker, "BT_WORKER_REPLAN_RESOURCE_ROUTE")

    @classmethod
    def _advance_frontier(cls, context, board: Blackboard) -> NodeResult:
        frame, worker = cls._frame(context), cls._worker(context)
        target = cls._frontier_target(worker, frame)
        if target is None:
            return cls._wait(worker, "BT_WORKER_NO_FRONTIER")
        result = cls._move(worker, target, "bt_worker_advance_frontier", 350, frame)
        if isinstance(result.intent, ActionIntent) and result.intent.action is ActionKind.MOVE:
            frame.memory.unit_tasks[str(worker.id)] = {
                "kind": "explore", "target": list(target), "step": list(result.intent.reserved_cell or target),
                "sector": cls._sector(worker, frame.memory), "sector_since": frame.context.tick,
            }
        if result.status is BehaviorStatus.FAILURE:
            return cls._wait(worker, "BT_WORKER_FRONTIER_ROUTE_BLOCKED")
        return result

    @staticmethod
    def _sector(worker: UnitView, memory: AgentMemory) -> int:
        task = memory.unit_tasks.get(str(worker.id), {})
        return int(task.get("sector", worker.id.int % len(_SECTORS))) % len(_SECTORS)

    @classmethod
    def _frontier_target(cls, worker: UnitView, frame: _Frame) -> Position | None:
        task = frame.memory.unit_tasks.get(str(worker.id), {})
        previous = task.get("target")
        if task.get("kind") in {"explore", "scout"} and isinstance(previous, list) and len(previous) == 2:
            candidate = (previous[0], previous[1])
            if (
                all(type(axis) is int for axis in candidate)
                and candidate != worker.position
                and candidate not in frame.memory.active_temporary_blocks(frame.context.tick)
            ):
                return candidate
        sector = cls._sector(worker, frame.memory)
        direction = _SECTORS[sector]
        frontier = (
            frame.memory.frontier()
            - frame.memory.active_temporary_blocks(frame.context.tick)
            - {worker.position}
        )
        if frontier:
            return min(
                frontier,
                key=lambda cell: (
                    -(cell[0] - worker.position[0]) * direction[0] - (cell[1] - worker.position[1]) * direction[1],
                    distance(worker.position, cell), cell,
                ),
            )
        return worker.position[0] + direction[0] * 3, worker.position[1] + direction[1] * 3

    @classmethod
    def _move(cls, worker: UnitView, target: Position, reason: str, score: float, frame: _Frame) -> NodeResult:
        direction = plan_step(
            actor_id=worker.id,
            start=worker.position,
            goal=target,
            context=frame.context,
            persistent_obstacles=frame.memory.obstacles | frame.memory.active_temporary_blocks(frame.context.tick),
            reservations=frame.reservations,
            deadline=frame.deadline,
            config=frame.config,
            avoid_threats=True,
        )
        if direction is None:
            return NodeResult(BehaviorStatus.FAILURE, "BT_WORKER_ROUTE_BLOCKED")
        step = destination(worker.position, direction)
        return NodeResult(
            BehaviorStatus.RUNNING,
            reason.upper(),
            ActionIntent(worker.id, False, ActionKind.MOVE, score, reason, target_cell=target, direction=direction, reserved_cell=step),
        )

    @staticmethod
    def _record_move_task(
        worker: UnitView,
        target: Position,
        kind: str,
        result: NodeResult,
        frame: _Frame,
    ) -> None:
        """Leave only serializable facts for next-Tick failure recovery."""
        intent = result.intent
        if not isinstance(intent, ActionIntent) or intent.action is not ActionKind.MOVE:
            return
        task = dict(frame.memory.unit_tasks.get(str(worker.id), {}))
        task.update({
            "kind": kind,
            "target": list(target),
            "step": list(intent.reserved_cell or target),
            "attempt_tick": frame.context.tick,
            "since_tick": int(task.get("since_tick", frame.context.tick)),
        })
        frame.memory.unit_tasks[str(worker.id)] = task

    @staticmethod
    def _wait(worker: UnitView, reason: str) -> NodeResult:
        return NodeResult(
            BehaviorStatus.SUCCESS,
            reason,
            ActionIntent(worker.id, False, ActionKind.WAIT, 0, reason.lower()),
        )
