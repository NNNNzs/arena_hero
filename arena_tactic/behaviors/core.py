"""Conservative Core behavior-tree canary with no self-destruct path."""

from __future__ import annotations

from dataclasses import dataclass, field

from arena_hero import BeaconStatus, CoreState, UnitType, unit_cost

from ..behavior_tree import Action, BehaviorStatus, Blackboard, Condition, NodeResult, Selector, Sequence, Tree
from ..context import DecisionContext
from ..models import ActionIntent, ActionKind, AgentConfig


@dataclass(slots=True)
class CoreCanaryPlanner:
    board: Blackboard = field(default_factory=Blackboard)
    tree: Tree | None = None
    node_events: tuple[object, ...] = ()

    def propose(self, context: DecisionContext, config: AgentConfig) -> ActionIntent | None:
        if context.core is None:
            self.board = Blackboard()
            return None
        if self.tree is None:
            self.tree = self._tree()
        result = self.tree.tick(context.tick, self.board, data={"context": context, "config": config})
        self.node_events = tuple(self.board.events)
        return result.intent if isinstance(result.intent, ActionIntent) else None

    @staticmethod
    def _tree() -> Tree:
        return Tree("core-canary-v1", Selector("core.root", (
            Sequence("core.moving", (Condition("core.is_moving", CoreCanaryPlanner._moving), Action("core.moving_wait", CoreCanaryPlanner._wait))),
            Sequence("core.heal", (Condition("core.damaged", CoreCanaryPlanner._damaged), Action("core.heal_action", CoreCanaryPlanner._heal))),
            Sequence("core.shield", (Condition("core.low_shield", CoreCanaryPlanner._low_shield), Action("core.repair_action", CoreCanaryPlanner._repair))),
            Sequence("core.beacon", (Condition("core.grounded_beacon", CoreCanaryPlanner._grounded_beacon), Action("core.pickup_action", CoreCanaryPlanner._pickup))),
            Sequence("core.production", (Condition("core.needs_worker", CoreCanaryPlanner._needs_worker), Action("core.spawn_worker", CoreCanaryPlanner._spawn_worker))),
            Action("core.wait", CoreCanaryPlanner._wait),
        )))

    @staticmethod
    def _context(tick) -> DecisionContext: return tick.data["context"]
    @classmethod
    def _moving(cls, tick, board): return cls._context(tick).core.state is CoreState.MOVING
    @classmethod
    def _damaged(cls, tick, board):
        context = cls._context(tick); return context.core.hp < 5 and context.resources > 0
    @classmethod
    def _low_shield(cls, tick, board):
        context = cls._context(tick); return context.core.shield < 3 and context.resources > 0
    @classmethod
    def _grounded_beacon(cls, tick, board):
        context = cls._context(tick); return context.beacon.status is BeaconStatus.GROUND and context.core.position == context.beacon.position
    @classmethod
    def _needs_worker(cls, tick, board):
        context = cls._context(tick); config: AgentConfig = tick.data["config"]
        cost = unit_cost(UnitType.WORKER, context.population)
        return context.population < config.early_workers and context.resources - cost >= config.minimum_resource_reserve
    @classmethod
    def _heal(cls, tick, board):
        core = cls._context(tick).core; return NodeResult(BehaviorStatus.SUCCESS, "BT_CORE_HEAL", ActionIntent(core.id, True, ActionKind.HEAL, 900, "bt_core_heal"))
    @classmethod
    def _repair(cls, tick, board):
        core = cls._context(tick).core; return NodeResult(BehaviorStatus.SUCCESS, "BT_CORE_REPAIR", ActionIntent(core.id, True, ActionKind.REPAIR_SHIELD, 800, "bt_core_repair_shield"))
    @classmethod
    def _pickup(cls, tick, board):
        core = cls._context(tick).core; return NodeResult(BehaviorStatus.SUCCESS, "BT_CORE_PICKUP", ActionIntent(core.id, True, ActionKind.PICKUP_BEACON, 700, "bt_core_pickup_ground_beacon"))
    @classmethod
    def _spawn_worker(cls, tick, board):
        core = cls._context(tick).core; return NodeResult(BehaviorStatus.SUCCESS, "BT_CORE_SPAWN_WORKER", ActionIntent(core.id, True, ActionKind.SPAWN, 450, "bt_core_spawn_worker", unit_type=UnitType.WORKER, estimated_cost=unit_cost(UnitType.WORKER, cls._context(tick).population)))
    @classmethod
    def _wait(cls, tick, board):
        core = cls._context(tick).core; return NodeResult(BehaviorStatus.SUCCESS, "BT_CORE_WAIT", ActionIntent(core.id, True, ActionKind.WAIT, 0, "bt_core_wait"))
