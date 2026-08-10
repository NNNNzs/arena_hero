from dataclasses import FrozenInstanceError, asdict
from types import MappingProxyType

import pytest
from arena_hero import UnitType

from arena_tactic.domain import (
    AuditEvent,
    Command,
    CommandStatus,
    CommandType,
    Goal,
    GoalSource,
    GoalStatus,
    Override,
    OverrideStatus,
    Policy,
    Task,
    TaskAssignment,
    AssignmentStatus,
    TaskStatus,
)


def test_task_lifecycle_rejects_illegal_transition():
    task = Task(
        task_id="task_legacy",
        goal_id="goal_legacy",
        kind="LEGACY_ACTION",
        status=TaskStatus.PENDING,
        priority=0,
    )

    ready = task.transition(TaskStatus.READY)
    assert ready.status is TaskStatus.READY
    assert task.status is TaskStatus.PENDING
    with pytest.raises(ValueError, match="PENDING.*SUCCEEDED"):
        task.transition(TaskStatus.SUCCEEDED)


def test_phase_one_domain_models_are_immutable_and_controller_free():
    goal = Goal(
        goal_id="goal_legacy",
        kind="LEGACY_PLAN",
        source=GoalSource.AUTO,
        status=GoalStatus.ACTIVE,
        priority=0,
        created_tick=7,
    )
    assignment = TaskAssignment(
        assignment_id="assignment_legacy",
        task_id="task_legacy",
        actor_alias="unit_ab12",
        role="WORKER",
        assigned_tick=7,
    )
    command = Command(
        command_id="command_1",
        idempotency_key="test-command",
        type=CommandType.ASSIGN_TASK,
        status=CommandStatus.QUEUED,
        issuer="test",
    )
    policy = Policy(version=1, effective_tick=8)
    override = Override(
        override_id="override_1",
        scope="entity:unit_ab12",
        command_id=command.command_id,
        priority=800,
        mode="FORCE",
        created_tick=7,
        ttl_ticks=3,
        status=OverrideStatus.ACTIVE,
    )
    audit = AuditEvent(
        event_id="audit_1",
        wall_time="2026-08-09T23:12:36Z",
        tick=7,
        actor="test",
        operation="CREATE",
        subject_alias=assignment.actor_alias,
        outcome="ACCEPTED",
    )

    assert goal.status is GoalStatus.ACTIVE
    assert assignment.actor_alias == "unit_ab12"
    assert policy.version == 1
    assert override.status is OverrideStatus.ACTIVE
    assert audit.subject_alias == "unit_ab12"
    assert "controller" not in repr((goal, assignment, command, policy, override, audit)).lower()
    with pytest.raises(FrozenInstanceError):
        goal.priority = 10  # type: ignore[misc]


def test_task_and_assignment_lifecycles_are_exhaustive_and_terminal():
    terminal = {TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.EXPIRED}
    for status in terminal:
        task = Task("t", "g", "K", status, 1)
        for target in TaskStatus:
            with pytest.raises(ValueError):
                task.transition(target)

    assignment = TaskAssignment("a", "t", "unit_x", "WORKER", 1)
    running = assignment.transition(AssignmentStatus.ACCEPTED).transition(AssignmentStatus.RUNNING)
    assert running.transition(AssignmentStatus.COMPLETED).status is AssignmentStatus.COMPLETED
    with pytest.raises(ValueError):
        assignment.transition(AssignmentStatus.COMPLETED)
    with pytest.raises(ValueError):
        running.transition(AssignmentStatus.OFFERED)

    for status in (GoalStatus.SATISFIED, GoalStatus.FAILED, GoalStatus.CANCELLED, GoalStatus.EXPIRED):
        goal = Goal("g", "K", GoalSource.AUTO, status, 1, 1)
        for target in GoalStatus:
            with pytest.raises(ValueError):
                goal.transition(target)


def test_command_and_override_include_phase_one_application_fields():
    command = Command("c", "key", CommandType.ASSIGN_TASK, CommandStatus.QUEUED, "test", issued_at="2026-08-09T00:00:00Z", apply_result={"accepted": True})
    override = Override("o", "entity:x", "c", 1, "FORCE", 1, 2, OverrideStatus.ACTIVE, task_spec={"kind": "SCOUT"}, policy_patch={"risk": 1})
    assert command.issued_at and command.apply_result["accepted"]
    assert override.task_spec["kind"] == "SCOUT" and override.policy_patch["risk"] == 1


def test_goal_task_and_assignment_include_strict_phase_one_plan_fields():
    goal = Goal("g", "SCOUT", GoalSource.AUTO, GoalStatus.ACTIVE, 3, 4,
                parent_goal_id="parent", dependency_goal_ids=("dep",), policy_version=2,
                progress=0.25)
    task = Task("t", "g", "SCOUT", TaskStatus.READY, 3,
                required_roles=("RANGER",), min_assignees=1, max_assignees=2,
                preconditions=("VISIBLE",), success_conditions=("SEEN",),
                failure_conditions=("EXPIRED",), retry_policy={"max_attempts": 2},
                formation={"shape": "LINE"}, formation_id="formation_alpha")
    assignment = TaskAssignment("a", "t", "entity_x", "RANGER", 4, runtime_id="runtime-unit-1")
    preempted = assignment.transition(AssignmentStatus.ACCEPTED).transition(AssignmentStatus.RUNNING).transition(AssignmentStatus.PREEMPTING).transition(AssignmentStatus.PREEMPTED)
    orphaned = assignment.transition(AssignmentStatus.ORPHANED)
    completed = assignment.transition(AssignmentStatus.ACCEPTED).transition(AssignmentStatus.RUNNING).transition(AssignmentStatus.COMPLETED)
    assert goal.parent_goal_id == "parent" and goal.progress == 0.25
    assert task.required_roles == ("RANGER",) and task.max_assignees == 2
    assert task.formation_id == "formation_alpha"
    assert assignment.runtime_id == "runtime-unit-1"
    assert asdict(assignment)["runtime_id"] == "runtime-unit-1"
    assert preempted.status is AssignmentStatus.PREEMPTED
    assert orphaned.status is AssignmentStatus.ORPHANED
    assert completed.status is AssignmentStatus.COMPLETED


def test_assignment_statuses_match_phase_one_plan_exactly():
    assert {status.value for status in AssignmentStatus} == {
        "OFFERED", "ACCEPTED", "RUNNING", "COMPLETED",
        "PREEMPTING", "PREEMPTED", "ORPHANED",
    }


def test_phase_one_nested_values_are_deeply_copied_and_immutable():
    nested = {"rules": [{"kind": "SCOUT", "cells": [1, 2]}]}
    command = Command("c", "test-key", CommandType.ASSIGN_TASK, CommandStatus.QUEUED, "test", payload=nested)
    nested["rules"][0]["kind"] = "ATTACK"
    nested["rules"].append({"kind": "HARVEST"})

    assert isinstance(command.payload, MappingProxyType)
    assert command.payload["rules"] == (MappingProxyType({"kind": "SCOUT", "cells": (1, 2)}),)
    with pytest.raises(TypeError):
        command.payload["new"] = True  # type: ignore[index]
    with pytest.raises(TypeError):
        command.payload["rules"][0]["kind"] = "ATTACK"  # type: ignore[index]


def test_phase_one_sequence_fields_are_deeply_copied_and_immutable():
    reason_codes = ["VISIBLE", ["NESTED", {"state": ["READY"]}]]
    dependencies = ["goal_parent", ["goal_child"]]
    roles = ["RANGER", ["SCOUT", {"rules": ["SAFE"]}]]
    preconditions = ["VISIBLE", ["ROUTE", {"cells": [1, 2]}]]
    goal = Goal(
        "g", "SCOUT", GoalSource.AUTO, GoalStatus.ACTIVE, 1, 1,
        reason_codes=reason_codes, dependency_goal_ids=dependencies,  # type: ignore[arg-type]
    )
    task = Task(
        "t", "g", "SCOUT", TaskStatus.READY, 1,
        required_roles=roles, preconditions=preconditions,  # type: ignore[arg-type]
        success_conditions=[["SEEN", {"count": [1]}]],  # type: ignore[list-item]
        failure_conditions=[["EXPIRED", {"ticks": [4]}]],  # type: ignore[list-item]
    )

    reason_codes[1][1]["state"].append("CHANGED")
    dependencies[1].append("goal_changed")
    roles[1][1]["rules"].append("UNSAFE")
    preconditions[1][1]["cells"].append(3)

    assert goal.reason_codes == ("VISIBLE", ("NESTED", MappingProxyType({"state": ("READY",)})))
    assert goal.dependency_goal_ids == ("goal_parent", ("goal_child",))
    assert task.required_roles == ("RANGER", ("SCOUT", MappingProxyType({"rules": ("SAFE",)})))
    assert task.preconditions == ("VISIBLE", ("ROUTE", MappingProxyType({"cells": (1, 2)})))
    assert task.success_conditions == (("SEEN", MappingProxyType({"count": (1,)})),)
    assert task.failure_conditions == (("EXPIRED", MappingProxyType({"ticks": (4,)})),)


@pytest.mark.parametrize("malicious", [
    object(),
    UnitType.WORKER,
    {"controller": object()},
    {"Authorization": "Bearer secret"},
    {"unit_id": "00000000-0000-0000-0000-000000000001"},
])
def test_phase_one_sequence_fields_reject_unsafe_nested_values(malicious):
    constructors = (
        lambda: Goal("g", "K", GoalSource.AUTO, GoalStatus.ACTIVE, 1, 1,
                     reason_codes=[malicious]),  # type: ignore[list-item]
        lambda: Goal("g", "K", GoalSource.AUTO, GoalStatus.ACTIVE, 1, 1,
                     dependency_goal_ids=[malicious]),  # type: ignore[list-item]
        lambda: Task("t", "g", "K", TaskStatus.READY, 1,
                     required_roles=[malicious]),  # type: ignore[list-item]
        lambda: Task("t", "g", "K", TaskStatus.READY, 1,
                     preconditions=[[malicious]]),  # type: ignore[list-item]
        lambda: Task("t", "g", "K", TaskStatus.READY, 1,
                     success_conditions=[[malicious]]),  # type: ignore[list-item]
        lambda: Task("t", "g", "K", TaskStatus.READY, 1,
                     failure_conditions=[[malicious]]),  # type: ignore[list-item]
    )
    for construct in constructors:
        with pytest.raises((TypeError, ValueError)):
            construct()


def test_all_phase_one_mapping_fields_use_the_same_safe_frozen_value_rules():
    policy = Policy(1, 1, weights={"economy": 1.0}, thresholds={"nested": {"values": [1, 2]}})  # type: ignore[arg-type]
    task = Task("t", "g", "SCOUT", TaskStatus.READY, 1,
                target={"cell": [1, 2]}, retry_policy={"delays": [1, 2]}, formation={"shape": {"offsets": [[0, 1]]}})
    override = Override("o", "entity:x", "c", 1, "FORCE", 1, 2, OverrideStatus.ACTIVE,
                        task_spec={"target": [1, 2]}, policy_patch={"flags": ["safe"]})

    for value in (policy.weights, policy.thresholds, task.target, task.retry_policy,
                  task.formation, override.task_spec, override.policy_patch):
        assert isinstance(value, MappingProxyType)


@pytest.mark.parametrize("malicious", [
    object(),
    UnitType.WORKER,
    {"controller": object()},
    {"Authorization": "Bearer secret"},
    {"unit_id": "00000000-0000-0000-0000-000000000001"},
    {"client_secret": "hidden"},
    {"note": "actor=00000000-0000-0000-0000-000000000001"},
])
def test_phase_one_payloads_reject_controllers_arbitrary_objects_sensitive_keys_and_full_uuids(malicious):
    with pytest.raises((TypeError, ValueError)):
        Command("c", "test-key", CommandType.ASSIGN_TASK, CommandStatus.QUEUED, "test", payload={"value": malicious})


@pytest.mark.parametrize("key", ["controller", "Controller", "con-troller", "con_troller"])
def test_controller_key_variants_are_rejected_even_when_value_is_a_string(key):
    with pytest.raises(ValueError, match="sensitive key"):
        Command(
            "c", "test-key", CommandType.ASSIGN_TASK, CommandStatus.QUEUED,
            "test", payload={"nested": [{key: "controller reference"}]},
        )


def test_explicit_controller_and_sdk_instances_are_rejected():
    ControllerReference = type("TurnController", (), {})
    SdkReference = type("SdkReference", (), {"__module__": "arena_hero.testing"})
    for value in (ControllerReference(), SdkReference()):
        with pytest.raises(TypeError, match="SDK object"):
            Command(
                "c", "test-key", CommandType.ASSIGN_TASK,
                CommandStatus.QUEUED, "test", payload={"value": value},
            )


def test_direct_persisted_text_fields_reject_complete_uuids():
    full_uuid = "00000000-0000-0000-0000-000000000001"
    constructors = (
        lambda: Goal(full_uuid, "K", GoalSource.AUTO, GoalStatus.ACTIVE, 1, 1),
        lambda: Task("t", "g", "K", TaskStatus.READY, 1, formation_id=full_uuid),
        lambda: TaskAssignment("a", "t", "entity_x", "RANGER", 1, runtime_id=full_uuid),
        lambda: Command("c", "key", CommandType.ASSIGN_TASK, CommandStatus.QUEUED, full_uuid),
        lambda: Override("o", full_uuid, "c", 1, "FORCE", 1, 2, OverrideStatus.ACTIVE),
        lambda: AuditEvent("a", "2026-01-01T00:00:00Z", 1, "actor", "CREATE", full_uuid, "OK"),
        lambda: Policy(1, 1, posture=full_uuid),
    )
    for construct in constructors:
        with pytest.raises(ValueError, match="full UUID"):
            construct()
