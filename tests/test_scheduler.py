from arena_hero import UnitType

from arena_tactic.domain import Goal, GoalSource, GoalStatus, Task, TaskStatus
from arena_tactic.scheduler import Actor, DeterministicScheduler, ScheduledTask
from arena_tactic import AgentRuntime
from arena_tactic.models import AgentConfig
from .factories import core, turn, unit


def _task(task_id: str, priority: int, utility: float, *, target: str = "cell:1,1", status: TaskStatus = TaskStatus.READY) -> ScheduledTask:
    goal = Goal(f"goal_{task_id}", "TEST", GoalSource.AUTO, GoalStatus.ACTIVE, priority, 1)
    return ScheduledTask(Task(task_id, goal.goal_id, "TEST", status, priority, required_roles=("WORKER",)), utility, target_key=target)


def test_high_priority_rescue_preempts_guard():
    scheduler = DeterministicScheduler()
    prior = scheduler.schedule(1, (_task("guard", 400, 2),), (Actor("entity_worker", "WORKER"),))
    result = scheduler.schedule(2, (_task("guard", 400, 2), _task("rescue", 900, 1)), (Actor("entity_worker", "WORKER"),), prior.assignments)

    assert [item.task_id for item in result.assignments] == ["rescue"]
    assert result.preempted[0].task_id == "guard"


def test_lock_prevents_double_resource_assignment():
    result = DeterministicScheduler().schedule(
        1,
        (_task("harvest_a", 500, 2, target="resource:2,2"), _task("harvest_b", 500, 1, target="resource:2,2")),
        (Actor("entity_a", "WORKER"), Actor("entity_b", "WORKER")),
    )

    assert len(result.assignments) == 1
    assert result.assignments[0].task_id == "harvest_a"
    assert result.blocked[0].task_id == "harvest_b"


def test_blocked_task_ages_then_reassigns_after_lease_expiry():
    scheduler = DeterministicScheduler(blocked_reassign_ticks=2, lease_ticks=1)
    task = _task("blocked", 500, 1)
    held = scheduler.schedule(1, (task,), (Actor("entity_a", "WORKER"),))
    blocked = scheduler.schedule(2, (ScheduledTask(task.task, 1, target_key="resource:2,2"),), (), held.assignments)
    recovered = scheduler.schedule(4, (task,), (Actor("entity_b", "WORKER"),), blocked.blocked_assignments)

    assert blocked.blocked[0].waited_ticks == 1
    assert recovered.assignments[0].actor_alias == "entity_b"
    assert recovered.reassigned_task_ids == ("blocked",)


def test_scheduler_is_deterministic():
    scheduler = DeterministicScheduler()
    tasks = (_task("z", 500, 1), _task("a", 500, 1, target="cell:2,2"))
    actors = (Actor("entity_z", "WORKER"), Actor("entity_a", "WORKER"))

    first = scheduler.schedule(8, tasks, actors)
    second = scheduler.schedule(8, tuple(reversed(tasks)), tuple(reversed(actors)))

    assert first == second


def test_shadow_scheduler_records_trace_without_changing_legacy_sdk_plan():
    baseline = turn(owned_core=core(), units=(unit(1, UnitType.WORKER, (0, 0)),), resource_cells=((1, 0),))
    shadowed = turn(owned_core=core(), units=(unit(1, UnitType.WORKER, (0, 0)),), resource_cells=((1, 0),))

    AgentRuntime().decide(baseline)
    result = AgentRuntime(config=AgentConfig(scheduler_shadow=True, worker_bt_canary=False)).decide(shadowed)

    assert shadowed.plan.model_dump(mode="json") == baseline.plan.model_dump(mode="json")
    assert any(item["goal"] == "SCHEDULER_SHADOW" for item in result.trace.goal_summaries)


def test_new_objective_flags_are_disabled_by_default_and_do_not_change_legacy_plan():
    config = AgentConfig()
    assert not config.scheduler_shadow and not config.worker_bt_canary
    assert not config.beacon_campaign_v1 and not config.core_migration_v1 and not config.core_attack_campaign_v1

    baseline = turn(owned_core=core(), units=(unit(1, UnitType.WORKER, (0, 0)),), resource_cells=((1, 0),))
    flagged = turn(owned_core=core(), units=(unit(1, UnitType.WORKER, (0, 0)),), resource_cells=((1, 0),))
    AgentRuntime().decide(baseline)
    AgentRuntime(config=AgentConfig(beacon_campaign_v1=True, core_migration_v1=True, core_attack_campaign_v1=True)).decide(flagged)
    assert flagged.plan.model_dump(mode="json") == baseline.plan.model_dump(mode="json")
