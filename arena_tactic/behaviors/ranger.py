"""Ranger canary: current-visible shots first, then safe recovery."""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

from arena_hero import CoreState, CoreView, UnitView

from ..behavior_tree import Action, BehaviorStatus, Blackboard, Condition, NodeResult, Selector, Sequence, Tree
from ..context import DecisionContext
from ..identity import entity_alias
from ..memory import AgentMemory
from ..models import ActionIntent, ActionKind, AgentConfig, ReservationTable
from ..navigation import destination, distance, plan_step, shot_range


@dataclass(slots=True)
class RangerCanaryPlanner:
    boards: dict[UUID, Blackboard] = field(default_factory=dict)
    trees: dict[UUID, Tree] = field(default_factory=dict)
    node_events: dict[UUID, tuple[object, ...]] = field(default_factory=dict)

    def propose(self, context: DecisionContext, memory: AgentMemory, config: AgentConfig, deadline: float) -> tuple[ActionIntent, ...]:
        current = {unit.id for unit in context.rangers}
        self.boards = {key: value for key, value in self.boards.items() if key in current}
        self.trees = {key: value for key, value in self.trees.items() if key in current}
        self.node_events = {key: value for key, value in self.node_events.items() if key in current}
        reservations = ReservationTable({cell: len(ids) for cell, ids in context.friendly_occupancy.items()})
        intents: list[ActionIntent] = []
        for unit in sorted(context.rangers, key=lambda item: item.id.bytes):
            board = self.boards.setdefault(unit.id, Blackboard())
            tree = self.trees.setdefault(unit.id, self._tree())
            result = tree.tick(context.tick, board, data={"context": context, "memory": memory, "config": config, "deadline": deadline, "unit": unit, "reservations": reservations})
            if isinstance(result.intent, ActionIntent):
                intents.append(result.intent)
            self.node_events[unit.id] = tuple(board.events)
        return tuple(intents)

    @staticmethod
    def _tree() -> Tree:
        return Tree("ranger-canary-v1", Selector("ranger.root", (
            Sequence("ranger.retreat", (Condition("ranger.critical", RangerCanaryPlanner._critical), Action("ranger.return", RangerCanaryPlanner._return))),
            Sequence("ranger.shoot", (Condition("ranger.legal_target", RangerCanaryPlanner._legal_target), Action("ranger.shoot_action", RangerCanaryPlanner._shoot))),
            Sequence("ranger.assignment", (Condition("ranger.assignment_move", RangerCanaryPlanner._has_assignment_move), Action("ranger.assignment_step", RangerCanaryPlanner._assignment_move))),
            Action("ranger.guard", RangerCanaryPlanner._wait),
        )))

    @staticmethod
    def _data(tick): return tick.data

    @classmethod
    def _critical(cls, tick, board):
        data = cls._data(tick); return data["unit"].hp <= 1 and data["context"].core is not None

    @classmethod
    def _targets(cls, tick):
        data = cls._data(tick); unit = data["unit"]
        return [enemy for enemy in data["context"].enemies if shot_range(unit.position, enemy.position, data["context"].obstacle_cells) is not None]

    @classmethod
    def _legal_target(cls, tick, board): return bool(cls._targets(tick))

    @classmethod
    def _assignment_target(cls, tick):
        data = cls._data(tick); unit = data["unit"]
        task = data["memory"].scheduler_assignments.get(entity_alias(unit.id) or "")
        target = task.get("target") if isinstance(task, dict) and task.get("kind") in {"DEFEND_CORE", "BEACON_ESCORT", "ATTACK_RALLY", "RETREAT"} else None
        return (target[0], target[1]) if isinstance(target, (list, tuple)) and len(target) == 2 and all(type(axis) is int for axis in target) else None

    @classmethod
    def _has_assignment_move(cls, tick, board):
        target = cls._assignment_target(tick)
        return target is not None and cls._data(tick)["unit"].position != target

    @classmethod
    def _return(cls, tick, board):
        data = cls._data(tick); unit = data["unit"]; core = data["context"].core
        assert core is not None
        if unit.position == core.position and core.state is CoreState.NORMAL:
            return NodeResult(BehaviorStatus.SUCCESS, "BT_RANGER_HEAL", ActionIntent(unit.id, False, ActionKind.HEAL, 920, "bt_ranger_heal"))
        direction = plan_step(actor_id=unit.id, start=unit.position, goal=core.position, context=data["context"], persistent_obstacles=data["memory"].obstacles, reservations=data["reservations"], deadline=data["deadline"], config=data["config"], avoid_threats=True)
        if direction is None: return cls._wait(tick, board, "bt_ranger_retreat_blocked")
        return NodeResult(BehaviorStatus.RUNNING, "BT_RANGER_RETREAT", ActionIntent(unit.id, False, ActionKind.MOVE, 900, "bt_ranger_retreat", direction=direction, target_cell=core.position, reserved_cell=destination(unit.position, direction)))

    @classmethod
    def _shoot(cls, tick, board):
        data = cls._data(tick); unit = data["unit"]
        target = min(cls._targets(tick), key=lambda enemy: (0 if isinstance(enemy, CoreView) else 1, enemy.hp, enemy.id.bytes))
        return NodeResult(BehaviorStatus.SUCCESS, "BT_RANGER_SHOOT", ActionIntent(unit.id, False, ActionKind.SHOOT, 850, "bt_ranger_current_legal_shot", target_id=target.id, target_cell=target.position))

    @classmethod
    def _assignment_move(cls, tick, board):
        data = cls._data(tick); unit = data["unit"]; target = cls._assignment_target(tick)
        assert target is not None
        direction = plan_step(actor_id=unit.id, start=unit.position, goal=target, context=data["context"],
                              persistent_obstacles=data["memory"].obstacles, reservations=data["reservations"],
                              deadline=data["deadline"], config=data["config"], avoid_threats=True)
        if direction is None: return cls._wait(tick, board, "bt_ranger_assignment_blocked")
        task = data["memory"].scheduler_assignments.get(entity_alias(unit.id) or "", {})
        kind = str(task.get("kind", "ASSIGNMENT")).lower()
        return NodeResult(BehaviorStatus.RUNNING, "BT_RANGER_ASSIGNMENT", ActionIntent(unit.id, False, ActionKind.MOVE, float(task.get("priority", 850)), f"bt_ranger_{kind}", direction=direction, target_cell=target, reserved_cell=destination(unit.position, direction)))

    @classmethod
    def _wait(cls, tick, board, reason: str = "bt_ranger_guard"):
        unit: UnitView = cls._data(tick)["unit"]
        return NodeResult(BehaviorStatus.SUCCESS, reason.upper(), ActionIntent(unit.id, False, ActionKind.WAIT, 0, reason))
