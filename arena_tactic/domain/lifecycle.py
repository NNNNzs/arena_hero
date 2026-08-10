"""Persistent domain records and their explicit lifecycle boundaries."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any, Mapping

from .values import FrozenJson, freeze_json, freeze_mapping, freeze_optional_text, freeze_sequence, freeze_text


class GoalSource(StrEnum):
    AUTO = "AUTO"
    MANUAL = "MANUAL"


class GoalStatus(StrEnum):
    PROPOSED = "PROPOSED"
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"
    SATISFIED = "SATISFIED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


class TaskStatus(StrEnum):
    PENDING = "PENDING"
    READY = "READY"
    ASSIGNED = "ASSIGNED"
    RUNNING = "RUNNING"
    BLOCKED = "BLOCKED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


class AssignmentStatus(StrEnum):
    OFFERED = "OFFERED"
    ACCEPTED = "ACCEPTED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    PREEMPTING = "PREEMPTING"
    PREEMPTED = "PREEMPTED"
    ORPHANED = "ORPHANED"


_TASK_TRANSITIONS = {
    TaskStatus.PENDING: {TaskStatus.READY, TaskStatus.CANCELLED, TaskStatus.EXPIRED},
    TaskStatus.READY: {TaskStatus.ASSIGNED, TaskStatus.BLOCKED, TaskStatus.CANCELLED, TaskStatus.EXPIRED},
    TaskStatus.ASSIGNED: {TaskStatus.RUNNING, TaskStatus.BLOCKED, TaskStatus.CANCELLED, TaskStatus.EXPIRED},
    TaskStatus.RUNNING: {TaskStatus.BLOCKED, TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.EXPIRED},
    TaskStatus.BLOCKED: {TaskStatus.READY, TaskStatus.RUNNING, TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.EXPIRED},
}

_GOAL_TRANSITIONS = {
    GoalStatus.PROPOSED: {GoalStatus.ACTIVE, GoalStatus.CANCELLED, GoalStatus.EXPIRED},
    GoalStatus.ACTIVE: {GoalStatus.SUSPENDED, GoalStatus.SATISFIED, GoalStatus.FAILED, GoalStatus.CANCELLED, GoalStatus.EXPIRED},
    GoalStatus.SUSPENDED: {GoalStatus.ACTIVE, GoalStatus.FAILED, GoalStatus.CANCELLED, GoalStatus.EXPIRED},
}


@dataclass(frozen=True, slots=True)
class Goal:
    goal_id: str
    kind: str
    source: GoalSource
    status: GoalStatus
    priority: int
    created_tick: int
    utility: float = 0.0
    target: Any = None
    deadline_tick: int | None = None
    ttl_ticks: int | None = None
    reason_codes: tuple[FrozenJson, ...] = ()
    parent_goal_id: str | None = None
    dependency_goal_ids: tuple[FrozenJson, ...] = ()
    policy_version: int = 0
    progress: float = 0.0

    def __post_init__(self) -> None:
        if not 0.0 <= self.progress <= 1.0:
            raise ValueError("Goal progress must be between 0 and 1")
        if self.policy_version < 0:
            raise ValueError("Goal policy_version must be nonnegative")
        for name in ("goal_id", "kind"):
            object.__setattr__(self, name, freeze_text(getattr(self, name), field_name=f"Goal.{name}"))
        object.__setattr__(self, "parent_goal_id", freeze_optional_text(
            self.parent_goal_id, field_name="Goal.parent_goal_id"
        ))
        object.__setattr__(self, "target", freeze_json(self.target, field_name="Goal.target"))
        object.__setattr__(self, "reason_codes", freeze_sequence(self.reason_codes, field_name="Goal.reason_codes"))
        object.__setattr__(self, "dependency_goal_ids", freeze_sequence(
            self.dependency_goal_ids, field_name="Goal.dependency_goal_ids"
        ))

    def transition(self, status: GoalStatus) -> "Goal":
        if status not in _GOAL_TRANSITIONS.get(self.status, set()):
            raise ValueError(f"illegal Goal transition {self.status.value} -> {status.value}")
        return replace(self, status=status)


@dataclass(frozen=True, slots=True)
class Task:
    task_id: str
    goal_id: str
    kind: str
    status: TaskStatus
    priority: int
    target: Any = None
    wait_budget: int = 4
    ttl_ticks: int | None = None
    required_roles: tuple[FrozenJson, ...] = ()
    min_assignees: int = 1
    max_assignees: int = 1
    preconditions: tuple[FrozenJson, ...] = ()
    success_conditions: tuple[FrozenJson, ...] = ()
    failure_conditions: tuple[FrozenJson, ...] = ()
    retry_policy: Mapping[str, Any] | None = None
    formation: Mapping[str, Any] | None = None
    formation_id: str | None = None

    def __post_init__(self) -> None:
        if self.min_assignees <= 0 or self.max_assignees < self.min_assignees:
            raise ValueError("Task assignee bounds are invalid")
        for name in ("task_id", "goal_id", "kind"):
            object.__setattr__(self, name, freeze_text(getattr(self, name), field_name=f"Task.{name}"))
        object.__setattr__(self, "formation_id", freeze_optional_text(
            self.formation_id, field_name="Task.formation_id"
        ))
        object.__setattr__(self, "target", freeze_json(self.target, field_name="Task.target"))
        for name in ("required_roles", "preconditions", "success_conditions", "failure_conditions"):
            object.__setattr__(self, name, freeze_sequence(getattr(self, name), field_name=f"Task.{name}"))
        if self.retry_policy is not None:
            object.__setattr__(self, "retry_policy", freeze_mapping(self.retry_policy, field_name="Task.retry_policy"))
        if self.formation is not None:
            object.__setattr__(self, "formation", freeze_mapping(self.formation, field_name="Task.formation"))

    def transition(self, status: TaskStatus) -> "Task":
        if status not in _TASK_TRANSITIONS.get(self.status, set()):
            raise ValueError(f"illegal Task transition {self.status.value} -> {status.value}")
        return replace(self, status=status)


@dataclass(frozen=True, slots=True)
class TaskAssignment:
    assignment_id: str
    task_id: str
    actor_alias: str
    role: str
    assigned_tick: int
    status: AssignmentStatus = AssignmentStatus.OFFERED
    lease_until_tick: int | None = None
    started_tick: int | None = None
    waiting_since: int | None = None
    preemptible: bool = True
    checkpoint: str | None = None
    last_blocker: str | None = None
    runtime_id: str | None = None

    def __post_init__(self) -> None:
        for name in ("assignment_id", "task_id", "actor_alias", "role"):
            object.__setattr__(self, name, freeze_text(
                getattr(self, name), field_name=f"TaskAssignment.{name}"
            ))
        for name in ("checkpoint", "last_blocker", "runtime_id"):
            object.__setattr__(self, name, freeze_optional_text(
                getattr(self, name), field_name=f"TaskAssignment.{name}"
            ))

    def transition(self, status: AssignmentStatus) -> "TaskAssignment":
        transitions = {
            AssignmentStatus.OFFERED: {AssignmentStatus.ACCEPTED, AssignmentStatus.ORPHANED},
            AssignmentStatus.ACCEPTED: {AssignmentStatus.RUNNING, AssignmentStatus.ORPHANED},
            AssignmentStatus.RUNNING: {AssignmentStatus.COMPLETED, AssignmentStatus.PREEMPTING, AssignmentStatus.ORPHANED},
            AssignmentStatus.PREEMPTING: {AssignmentStatus.PREEMPTED, AssignmentStatus.RUNNING, AssignmentStatus.ORPHANED},
        }
        if status not in transitions.get(self.status, set()):
            raise ValueError(f"illegal Assignment transition {self.status.value} -> {status.value}")
        return replace(self, status=status)
