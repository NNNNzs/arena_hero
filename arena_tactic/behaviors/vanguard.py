"""Conservative Vanguard behavior-tree canary using only current Turn facts."""

from __future__ import annotations

from dataclasses import dataclass, field
from time import perf_counter
from uuid import UUID

from arena_hero import CoreState, CoreView, UnitType, UnitView

from ..behavior_tree import Action, BehaviorStatus, Blackboard, Condition, NodeResult, Selector, Sequence, Tree
from ..context import DecisionContext
from ..identity import entity_alias
from ..memory import AgentMemory
from ..models import ActionIntent, ActionKind, AgentConfig, ReservationTable
from ..navigation import adjacent_direction, destination, distance, plan_step


@dataclass(slots=True)
class VanguardCanaryPlanner:
    boards: dict[UUID, Blackboard] = field(default_factory=dict)
    trees: dict[UUID, Tree] = field(default_factory=dict)
    node_events: dict[UUID, tuple[object, ...]] = field(default_factory=dict)

    def propose(self, context: DecisionContext, memory: AgentMemory, config: AgentConfig, deadline: float) -> tuple[ActionIntent, ...]:
        current = {unit.id for unit in context.vanguards}
        self.boards = {key: value for key, value in self.boards.items() if key in current}
        self.trees = {key: value for key, value in self.trees.items() if key in current}
        self.node_events = {key: value for key, value in self.node_events.items() if key in current}
        reservations = ReservationTable({cell: len(ids) for cell, ids in context.friendly_occupancy.items()})
        intents: list[ActionIntent] = []
        for unit in sorted(context.vanguards, key=lambda item: item.id.bytes):
            board = self.boards.setdefault(unit.id, Blackboard())
            tree = self.trees.setdefault(unit.id, self._tree())
            result = tree.tick(context.tick, board, data={"context": context, "memory": memory, "config": config, "deadline": deadline, "unit": unit, "reservations": reservations})
            if isinstance(result.intent, ActionIntent):
                intents.append(result.intent)
            self.node_events[unit.id] = tuple(board.events)
        return tuple(intents)

    @staticmethod
    def _tree() -> Tree:
        return Tree("vanguard-canary-v1", Selector("vanguard.root", (
            Sequence("vanguard.retreat", (Condition("vanguard.critical", VanguardCanaryPlanner._critical), Action("vanguard.return", VanguardCanaryPlanner._return))),
            Sequence("vanguard.sweep", (Condition("vanguard.adjacent_enemy", VanguardCanaryPlanner._adjacent), Action("vanguard.sweep_action", VanguardCanaryPlanner._sweep))),
            Sequence("vanguard.assignment", (Condition("vanguard.assignment_move", VanguardCanaryPlanner._has_assignment_move), Action("vanguard.assignment_step", VanguardCanaryPlanner._assignment_move))),
            Action("vanguard.guard", VanguardCanaryPlanner._guard),
        )))

    @staticmethod
    def _data(tick):
        return tick.data

    @classmethod
    def _critical(cls, tick, board):
        data = cls._data(tick); return data["unit"].hp <= 1 and data["context"].core is not None

    @classmethod
    def _adjacent(cls, tick, board):
        data = cls._data(tick); unit = data["unit"]
        return any(distance(unit.position, enemy.position) == 1 for enemy in data["context"].enemies)

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
        data = cls._data(tick); unit = data["unit"]; context = data["context"]; core = context.core
        assert core is not None
        if unit.position == core.position and core.state is CoreState.NORMAL:
            return NodeResult(BehaviorStatus.SUCCESS, "BT_VANGUARD_HEAL", ActionIntent(unit.id, False, ActionKind.HEAL, 920, "bt_vanguard_heal"))
        direction = plan_step(actor_id=unit.id, start=unit.position, goal=core.position, context=context, persistent_obstacles=data["memory"].obstacles, reservations=data["reservations"], deadline=data["deadline"], config=data["config"], avoid_threats=True)
        if direction is None:
            return cls._wait(unit, "bt_vanguard_retreat_blocked")
        return NodeResult(BehaviorStatus.RUNNING, "BT_VANGUARD_RETREAT", ActionIntent(unit.id, False, ActionKind.MOVE, 900, "bt_vanguard_retreat", direction=direction, target_cell=core.position, reserved_cell=destination(unit.position, direction)))

    @classmethod
    def _sweep(cls, tick, board):
        data = cls._data(tick); unit = data["unit"]
        enemies = [enemy for enemy in data["context"].enemies if distance(unit.position, enemy.position) == 1]
        target = min(enemies, key=lambda enemy: (0 if isinstance(enemy, CoreView) else 1, enemy.hp, enemy.id.bytes))
        direction = adjacent_direction(unit.position, target.position)
        assert direction is not None
        return NodeResult(BehaviorStatus.SUCCESS, "BT_VANGUARD_SWEEP", ActionIntent(unit.id, False, ActionKind.SWEEP, 850, "bt_vanguard_adjacent_sweep", direction=direction, target_cell=target.position))

    @classmethod
    def _assignment_move(cls, tick, board):
        data = cls._data(tick); unit = data["unit"]; target = cls._assignment_target(tick)
        assert target is not None
        direction = plan_step(actor_id=unit.id, start=unit.position, goal=target, context=data["context"],
                              persistent_obstacles=data["memory"].obstacles, reservations=data["reservations"],
                              deadline=data["deadline"], config=data["config"], avoid_threats=True)
        if direction is None:
            return cls._wait(unit, "bt_vanguard_assignment_blocked")
        task = data["memory"].scheduler_assignments.get(entity_alias(unit.id) or "", {})
        kind = str(task.get("kind", "ASSIGNMENT")).lower()
        return NodeResult(BehaviorStatus.RUNNING, "BT_VANGUARD_ASSIGNMENT", ActionIntent(unit.id, False, ActionKind.MOVE, float(task.get("priority", 850)), f"bt_vanguard_{kind}", direction=direction, target_cell=target, reserved_cell=destination(unit.position, direction)))

    @classmethod
    def _guard(cls, tick, board):
        return cls._wait(cls._data(tick)["unit"], "bt_vanguard_guard")

    @staticmethod
    def _wait(unit: UnitView, reason: str) -> NodeResult:
        return NodeResult(BehaviorStatus.SUCCESS, reason.upper(), ActionIntent(unit.id, False, ActionKind.WAIT, 0, reason))
