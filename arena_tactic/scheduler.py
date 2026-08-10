"""Deterministic, controller-free scheduler used in shadow mode first."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .domain import AssignmentStatus, Task, TaskAssignment, TaskStatus


@dataclass(frozen=True, slots=True)
class Actor:
    alias: str
    role: str


@dataclass(frozen=True, slots=True)
class ScheduledTask:
    task: Task
    utility: float = 0.0
    age: int = 0
    target_key: str | None = None
    target_capacity: int = 1
    eligible_aliases: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.target_capacity <= 0:
            raise ValueError("target_capacity must be positive")


@dataclass(frozen=True, slots=True)
class ScheduledAssignment:
    task_id: str
    actor_alias: str
    role: str
    assigned_tick: int
    lease_until_tick: int
    status: AssignmentStatus = AssignmentStatus.RUNNING
    waiting_since: int | None = None
    checkpoint: str | None = None

    def domain_record(self) -> TaskAssignment:
        return TaskAssignment(
            assignment_id=f"assignment_{self.task_id}_{self.actor_alias}",
            task_id=self.task_id,
            actor_alias=self.actor_alias,
            role=self.role,
            assigned_tick=self.assigned_tick,
            status=self.status,
            lease_until_tick=self.lease_until_tick,
            waiting_since=self.waiting_since,
            checkpoint=self.checkpoint,
        )


@dataclass(frozen=True, slots=True)
class BlockedTask:
    task_id: str
    waited_ticks: int
    reason: str


@dataclass(frozen=True, slots=True)
class ScheduleResult:
    assignments: tuple[ScheduledAssignment, ...]
    blocked: tuple[BlockedTask, ...] = ()
    blocked_assignments: tuple[ScheduledAssignment, ...] = ()
    preempted: tuple[ScheduledAssignment, ...] = ()
    reassigned_task_ids: tuple[str, ...] = ()


class DeterministicScheduler:
    """Small two-phase allocator with entity and target capacity locks.

    It contains no Turn or controller reference.  A caller can therefore run it
    as an observational shadow without risking a queued SDK action.
    """

    def __init__(self, *, lease_ticks: int = 2, blocked_reassign_ticks: int = 4) -> None:
        if lease_ticks <= 0 or blocked_reassign_ticks <= 0:
            raise ValueError("lease_ticks and blocked_reassign_ticks must be positive")
        self.lease_ticks = lease_ticks
        self.blocked_reassign_ticks = blocked_reassign_ticks

    @staticmethod
    def _rank(item: ScheduledTask) -> tuple[float | int | str, ...]:
        # Negation makes normal ascending tuple ordering choose higher values.
        return (-item.task.priority, -item.utility, -item.age, item.task.task_id)

    @staticmethod
    def _roles(task: Task) -> tuple[str, ...]:
        roles = tuple(str(item) for item in task.required_roles if isinstance(item, str))
        return roles or ("ANY",)

    def schedule(
        self,
        tick: int,
        tasks: Iterable[ScheduledTask],
        actors: Iterable[Actor],
        previous_assignments: Iterable[ScheduledAssignment] = (),
    ) -> ScheduleResult:
        ordered_tasks = tuple(sorted(tasks, key=self._rank))
        actors_by_alias = {actor.alias: actor for actor in actors}
        prior_by_task: dict[str, list[ScheduledAssignment]] = {}
        for assignment in previous_assignments:
            prior_by_task.setdefault(assignment.task_id, []).append(assignment)

        assigned: list[ScheduledAssignment] = []
        blocked: list[BlockedTask] = []
        blocked_assignments: list[ScheduledAssignment] = []
        used_actors: set[str] = set()
        target_uses: dict[str, int] = {}
        assigned_task_ids: set[str] = set()

        for item in ordered_tasks:
            task = item.task
            if task.status not in {TaskStatus.READY, TaskStatus.ASSIGNED, TaskStatus.RUNNING, TaskStatus.BLOCKED}:
                continue
            target = item.target_key or f"task:{task.task_id}"
            if target_uses.get(target, 0) >= item.target_capacity:
                prior = prior_by_task.get(task.task_id, ())
                waiting_since = min((entry.waiting_since or entry.assigned_tick for entry in prior), default=tick)
                blocked.append(BlockedTask(task.task_id, max(0, tick - waiting_since), "TARGET_LOCKED"))
                blocked_assignments.extend(
                    ScheduledAssignment(entry.task_id, entry.actor_alias, entry.role, entry.assigned_tick,
                                        entry.lease_until_tick, entry.status, waiting_since, entry.checkpoint)
                    for entry in prior
                )
                continue
            roles = self._roles(task)
            candidates = tuple(sorted(
                (actor for actor in actors_by_alias.values()
                 if actor.alias not in used_actors
                 and (not item.eligible_aliases or actor.alias in item.eligible_aliases)
                 and ("ANY" in roles or actor.role in roles)),
                key=lambda actor: actor.alias,
            ))
            needed = task.min_assignees
            if len(candidates) < needed:
                prior = prior_by_task.get(task.task_id, ())
                waiting_since = min((entry.waiting_since or entry.assigned_tick for entry in prior), default=tick)
                blocked.append(BlockedTask(task.task_id, max(0, tick - waiting_since), "NO_ELIGIBLE_ACTOR"))
                blocked_assignments.extend(
                    ScheduledAssignment(entry.task_id, entry.actor_alias, entry.role, entry.assigned_tick,
                                        entry.lease_until_tick, entry.status, waiting_since, entry.checkpoint)
                    for entry in prior
                )
                continue
            for actor in candidates[: min(task.max_assignees, len(candidates))]:
                old = next((entry for entry in prior_by_task.get(task.task_id, ()) if entry.actor_alias == actor.alias), None)
                assigned.append(ScheduledAssignment(
                    task.task_id, actor.alias, actor.role,
                    old.assigned_tick if old else tick,
                    tick + self.lease_ticks,
                    checkpoint=old.checkpoint if old else None,
                ))
                used_actors.add(actor.alias)
            target_uses[target] = target_uses.get(target, 0) + 1
            assigned_task_ids.add(task.task_id)

        active_pairs = {(entry.task_id, entry.actor_alias) for entry in assigned}
        preempted = tuple(sorted(
            (entry for entries in prior_by_task.values() for entry in entries
             if (entry.task_id, entry.actor_alias) not in active_pairs and entry.lease_until_tick >= tick),
            key=lambda entry: (entry.actor_alias, entry.task_id),
        ))
        reassigned = tuple(sorted({
            entry.task_id for entry in assigned
            if any(old.task_id == entry.task_id and old.actor_alias != entry.actor_alias
                   and tick - (old.waiting_since or old.lease_until_tick) >= self.blocked_reassign_ticks
                   for old in prior_by_task.get(entry.task_id, ()))
        }))
        return ScheduleResult(tuple(assigned), tuple(blocked), tuple(blocked_assignments), preempted, reassigned)
