from arena_hero import UnitType

from arena_tactic.domain import Goal, GoalSource, GoalStatus, Task, TaskStatus
from arena_tactic.scheduler import Actor, DeterministicScheduler, ScheduledTask
from arena_tactic import AgentRuntime
from arena_tactic.models import AgentConfig
from arena_tactic.identity import entity_alias
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


def test_new_objective_flags_are_disabled_by_default():
    config = AgentConfig()
    assert not config.scheduler_shadow and not config.worker_bt_canary and not config.vanguard_bt_canary and not config.ranger_bt_canary and not config.core_bt_canary
    assert not config.beacon_campaign_v1 and not config.core_migration_v1 and not config.core_attack_campaign_v1



def test_scheduler_shadow_marks_an_enabled_worker_canary_as_active():
    game_turn = turn(owned_core=core(), units=(unit(1, UnitType.WORKER, (0, 0)),), resource_cells=((1, 0),))

    result = AgentRuntime(config=AgentConfig(scheduler_shadow=True, worker_bt_canary=True)).decide(game_turn)

    shadow = next(item for item in result.trace.goal_summaries if item["goal"] == "SCHEDULER_SHADOW")
    assert shadow["worker_canary"] == "ACTIVE"


def test_enabled_phase_4_to_6_objectives_persist_and_only_replace_allowed_actions():
    target = core(value=200, position=(3, 0), controlled=False)
    baseline = turn(owned_core=core(), units=(unit(1, UnitType.WORKER, (0, 0)),), enemies=(target,))
    observed = turn(owned_core=core(), units=(unit(1, UnitType.WORKER, (0, 0)),), enemies=(target,))
    runtime = AgentRuntime(config=AgentConfig(
        beacon_campaign_v1=True, core_migration_v1=True, core_attack_campaign_v1=True,
    ))

    result = runtime.decide(observed)

    core_action = observed.plan.model_dump(mode="json")["core_action"]
    assert core_action["type"] == "START_MOVE"
    shadow_goals = {item["goal"] for item in result.trace.goal_summaries if item["status"] == "SHADOW"}
    assert shadow_goals == {"CONTROL_BEACON", "MIGRATE_CORE", "ATTACK_ENEMY_CORE"}
    assert result.next_memory.objective_states["migration"]["stage"] == "START"
    runtime.commit(result)
    assert runtime.memory.objective_states == result.next_memory.objective_states


def test_planner_canary_persists_scheduler_assignments_and_behavior_trees_consume_them():
    first_worker = unit(1, UnitType.WORKER, (0, 0))
    second_worker = unit(2, UnitType.WORKER, (0, 1))
    vanguard = unit(3, UnitType.VANGUARD, (4, 0))
    runtime = AgentRuntime(config=AgentConfig(planner_canary=True))
    first = runtime.decide(turn(tick=1, owned_core=core(), units=(first_worker, second_worker, vanguard), resource_cells=((2, 0),)))

    first_alias = entity_alias(first_worker.id)
    assert first_alias is not None
    assert first.next_memory.scheduler_assignments[first_alias]["kind"] == "HARVEST_RESOURCE"
    assert next(item for item in first.intents if item.actor_id == first_worker.id).reason == "bt_worker_move_to_visible_resource"
    assert next(item for item in first.intents if item.actor_id == vanguard.id).reason == "bt_vanguard_defend_core"
    worker_trace = next(item for item in first.trace.entity_traces if item.actor_alias == first_alias)
    assert worker_trace.current_task == "HARVEST_RESOURCE" and worker_trace.assignment_status == "SCHEDULED"
    transition = next(item for item in first.trace.task_transitions if item["actor_alias"] == first_alias)
    assert transition["goal"] == "economy_goal"
    assert transition["lock"] == "resource:2,0"
    assert transition["lease_until_tick"] == 3 and transition["target"] == (2, 0)
    runtime.commit(first)
    second = runtime.decide(turn(tick=2, owned_core=core(), units=(first_worker, second_worker, vanguard), resource_cells=((2, 0),)))

    assert second.next_memory.scheduler_assignments[first_alias]["task_id"] == first.next_memory.scheduler_assignments[first_alias]["task_id"]


def test_scheduler_canary_projects_active_beacon_and_attack_objectives_into_assignments():
    vanguard = unit(1, UnitType.VANGUARD, (0, 0))
    ranger = unit(2, UnitType.RANGER, (0, 1))
    beacon = AgentRuntime(config=AgentConfig(planner_canary=True, beacon_campaign_v1=True)).decide(
        turn(owned_core=core(), units=(vanguard, ranger), beacon_position=(5, 0))
    )
    assert {item["kind"] for item in beacon.next_memory.scheduler_assignments.values()} == {"BEACON_ESCORT"}

    enemy = core(value=200, position=(8, 0), controlled=False)
    attack = AgentRuntime(config=AgentConfig(planner_canary=True, core_attack_campaign_v1=True)).decide(
        turn(owned_core=core(), units=(vanguard, ranger), enemies=(enemy,))
    )
    assert {item["kind"] for item in attack.next_memory.scheduler_assignments.values()} == {"ATTACK_RALLY"}
