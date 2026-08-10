"""Pure enemy-Core attack lifecycle constrained to current authoritative facts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from ..domain import Goal, GoalSource, GoalStatus, Task, TaskStatus


class CoreAttackStage(StrEnum):
    RALLY = "RALLY"
    ENGAGE = "ENGAGE"
    REACQUIRE = "REACQUIRE"
    RETREAT = "RETREAT"
    CONFIRMED = "CONFIRMED"


@dataclass(frozen=True, slots=True)
class CoreAttackInput:
    tick: int
    target_cell: tuple[int, int] | None
    target_visible: bool
    rally_ready: int
    rally_quorum: int
    ranger_has_legal_slot: bool
    vanguard_adjacent: bool
    force_retreat: bool = False
    core_destroyed_event: bool = False


@dataclass(frozen=True, slots=True)
class CoreAttackCampaign:
    stage: CoreAttackStage = CoreAttackStage.RALLY

    def evaluate(self, facts: CoreAttackInput) -> tuple["CoreAttackCampaign", Goal, tuple[Task, ...], tuple[str, ...]]:
        if facts.core_destroyed_event:
            stage = CoreAttackStage.CONFIRMED
        elif facts.force_retreat:
            stage = CoreAttackStage.RETREAT
        elif not facts.target_visible:
            stage = CoreAttackStage.REACQUIRE
        elif facts.rally_ready < facts.rally_quorum:
            stage = CoreAttackStage.RALLY
        else:
            stage = CoreAttackStage.ENGAGE
        goal = Goal("enemy_core_attack", "ATTACK_ENEMY_CORE", GoalSource.AUTO,
                    GoalStatus.SATISFIED if stage is CoreAttackStage.CONFIRMED else GoalStatus.ACTIVE,
                    780, facts.tick, target=facts.target_cell)
        if stage is CoreAttackStage.RALLY:
            tasks, candidates = (Task("rally_attack", goal.goal_id, "RALLY_ATTACK", TaskStatus.READY, 780,
                                      min_assignees=max(1, facts.rally_quorum), max_assignees=max(1, facts.rally_quorum)),), ("RALLY",)
        elif stage is CoreAttackStage.REACQUIRE:
            tasks, candidates = (Task("reacquire_enemy_core", goal.goal_id, "REACQUIRE_TARGET", TaskStatus.READY, 780),), ("REACQUIRE",)
        elif stage is CoreAttackStage.RETREAT:
            tasks, candidates = (Task("retreat_attackers", goal.goal_id, "RETREAT", TaskStatus.READY, 900),), ("FORCE_RETREAT",)
        elif stage is CoreAttackStage.ENGAGE:
            task_list, candidate_list = [], []
            if facts.ranger_has_legal_slot:
                task_list.append(Task("ranger_fire", goal.goal_id, "RANGER_FIRE", TaskStatus.READY, 780, target=facts.target_cell, required_roles=("RANGER",)))
                candidate_list.append("SHOOT_CELL")
            if facts.vanguard_adjacent:
                task_list.append(Task("vanguard_sweep", goal.goal_id, "VANGUARD_SWEEP", TaskStatus.READY, 780, target=facts.target_cell, required_roles=("VANGUARD",)))
                candidate_list.append("SWEEP")
            tasks, candidates = tuple(task_list), tuple(candidate_list)
        else:
            tasks, candidates = (), ()
        return CoreAttackCampaign(stage), goal, tasks, candidates
