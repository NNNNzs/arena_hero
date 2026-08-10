"""Pure multi-Tick Core migration plan that is driven by fresh observations."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from ..domain import Goal, GoalSource, GoalStatus, Task, TaskStatus


class MigrationStage(StrEnum):
    RECALL = "RECALL"
    START = "START"
    MOVING = "MOVING"
    REPLAN = "REPLAN"
    COMPLETE = "COMPLETE"


@dataclass(frozen=True, slots=True)
class CoreMigrationInput:
    tick: int
    destination: tuple[int, int]
    cargo_workers_pending: int
    capacity: int
    stored_resources: int
    core_moving: bool
    move_progress: int | None = None
    move_failed: bool = False
    arrived: bool = False


@dataclass(frozen=True, slots=True)
class CoreMigrationPlan:
    destination: tuple[int, int]
    stage: MigrationStage = MigrationStage.RECALL
    start_attempted: bool = False
    replan_count: int = 0

    def evaluate(self, facts: CoreMigrationInput) -> tuple["CoreMigrationPlan", Goal, tuple[Task, ...], tuple[str, ...]]:
        if facts.stored_resources > facts.capacity:
            raise ValueError("migration plan cannot accept an over-capacity snapshot")
        stage, started, replans = self.stage, self.start_attempted, self.replan_count
        if facts.arrived:
            stage = MigrationStage.COMPLETE
        elif facts.move_failed or (facts.core_moving and facts.move_progress is not None and facts.move_progress >= 4):
            stage, started, replans = MigrationStage.REPLAN, False, replans + 1
        elif facts.core_moving:
            stage, started = MigrationStage.MOVING, True
        elif facts.cargo_workers_pending:
            stage = MigrationStage.RECALL
        elif stage is MigrationStage.MOVING:
            # The server has resolved the prior leg and supplied a fresh
            # stationary Core.  Re-arm exactly one next leg from this current
            # position; do not reuse an old controller or destination cell.
            stage, started = MigrationStage.START, False
        elif stage in {MigrationStage.RECALL, MigrationStage.REPLAN}:
            stage = MigrationStage.START

        goal_status = GoalStatus.SATISFIED if stage is MigrationStage.COMPLETE else GoalStatus.ACTIVE
        goal = Goal("core_migration", "MIGRATE_CORE", GoalSource.AUTO, goal_status, 650, facts.tick,
                    target=self.destination)
        if stage is MigrationStage.RECALL:
            tasks, candidates = (Task("recall_cargo_workers", goal.goal_id, "RECALL_CARGO_WORKERS", TaskStatus.READY, 650),), ("RECALL_CARGO",)
        elif stage is MigrationStage.START and not started:
            tasks, candidates, started = (Task("start_core_move", goal.goal_id, "START_CORE_MOVE", TaskStatus.READY, 650,
                                                target=self.destination),), ("START_MOVE",), True
        elif stage is MigrationStage.START:
            # A caller must wait for the next authoritative moving/failed
            # observation; never emit a second START_MOVE for one leg.
            tasks, candidates = (Task("await_core_move", goal.goal_id, "AWAIT_CORE_MOVE", TaskStatus.RUNNING, 650),), ("AWAIT_MOVE_STATE",)
        elif stage is MigrationStage.MOVING:
            tasks, candidates = (Task("continue_core_move", goal.goal_id, "CONTINUE_CORE_MOVE", TaskStatus.RUNNING, 650),), ("WAIT_FOR_MOVE",)
        elif stage is MigrationStage.REPLAN:
            tasks, candidates = (Task("replan_core_leg", goal.goal_id, "REPLAN_CORE_LEG", TaskStatus.READY, 650,
                                      target=self.destination),), ("REPLAN_LEG",)
        else:
            tasks, candidates = (), ()
        return CoreMigrationPlan(self.destination, stage, started, replans), goal, tasks, candidates
