from __future__ import annotations

import json

from arena_hero import UnitType

from arena_tactic import AgentRuntime
from arena_tactic.command_api import CommandApi
from arena_tactic.command_center import CommandQueue
from arena_tactic.domain import CommandStatus
from arena_tactic.identity import entity_alias
from arena_tactic.models import AgentConfig
from arena_tactic.memory import AgentMemory
from .factories import core, turn, unit


def _body(value: object) -> bytes:
    return json.dumps(value).encode()


def _login(api: CommandApi) -> tuple[str, str]:
    response = api.handle("POST", "/api/v1/session", {"Host": "localhost:8787"}, _body({"password": "test-password"}))
    assert response is not None and response.status == 200
    csrf = json.loads(response.body)["csrf_token"]
    cookie = next(value for name, value in response.headers if name == "Set-Cookie").split(";", 1)[0]
    return cookie, csrf


def _write_headers(cookie: str, csrf: str, version: int, key: str) -> dict[str, str]:
    return {"Host": "localhost:8787", "Origin": "http://localhost:8787", "Cookie": cookie,
            "X-CSRF-Token": csrf, "If-Match": f'"command-version-{version}"', "Idempotency-Key": key}


def test_queue_does_not_apply_emergency_until_accepted_submit():
    queue = CommandQueue()
    command, _, _ = queue.enqueue({"type": "EMERGENCY_STOP", "payload": {}}, issuer="test", current_tick=7,
                                  idempotency_key="emergency-queue-1", expected_version=0)
    prepared = queue.prepare_for_tick(8, set())

    assert prepared.emergency_halt
    assert queue.snapshot()[0].status is CommandStatus.QUEUED
    assert queue.finalize(prepared, accepted=False) == ()
    assert queue.snapshot()[0].status is CommandStatus.QUEUED

    changed = queue.finalize(prepared, accepted=True)
    assert changed == (command.transition(CommandStatus.APPLIED, apply_result={"applied_at_tick": 8}),)


def test_emergency_stop_produces_only_current_turn_waits_and_commits_after_submit():
    queue = CommandQueue()
    queue.enqueue({"type": "EMERGENCY_STOP", "payload": {}}, issuer="test", current_tick=1,
                  idempotency_key="emergency-runtime-1", expected_version=0)
    worker = unit(1, UnitType.WORKER, (0, 0))
    runtime = AgentRuntime(command_queue=queue)

    owned_core = core()
    result = runtime.decide(turn(tick=2, owned_core=owned_core, units=(worker,)))

    assert {intent.action.value for intent in result.intents} == {"WAIT"}
    assert {intent.actor_id for intent in result.intents} == {worker.id, owned_core.id}
    assert queue.snapshot()[0].status is CommandStatus.QUEUED
    runtime.commit(result)
    assert queue.snapshot()[0].status is CommandStatus.APPLIED


def test_command_api_requires_session_csrf_version_and_is_idempotent():
    queue = CommandQueue()
    api = CommandApi(queue, status_snapshot=lambda: {"last_tick": 10}, admin_password="test-password", writes_enabled=True)
    forbidden = api.handle("POST", "/api/v1/commands", {}, _body({"type": "EMERGENCY_STOP", "payload": {}}))
    assert forbidden is not None and forbidden.status == 401
    cookie, csrf = _login(api)
    headers = _write_headers(cookie, csrf, 0, "emergency-api-key-1")
    first = api.handle("POST", "/api/v1/commands", headers, _body({"type": "EMERGENCY_STOP", "payload": {}}))
    assert first is not None and first.status == 202
    payload = json.loads(first.body)
    retry = api.handle("POST", "/api/v1/commands", headers, _body({"type": "EMERGENCY_STOP", "payload": {}}))
    assert retry is not None and retry.status == 200 and json.loads(retry.body)["command_id"] == payload["command_id"]
    conflict = api.handle("POST", "/api/v1/commands", headers, _body({"type": "RESUME_AUTO", "payload": {}}))
    assert conflict is not None and conflict.status == 409


def test_command_api_write_switch_defaults_to_disabled():
    queue = CommandQueue()
    api = CommandApi(queue, status_snapshot=lambda: {"last_tick": 10}, admin_password="test-password")
    cookie, csrf = _login(api)
    response = api.handle("POST", "/api/v1/commands", _write_headers(cookie, csrf, 0, "write-disabled-1"),
                          _body({"type": "EMERGENCY_STOP", "payload": {}}))
    assert response is not None and response.status == 403


def test_command_api_convenience_task_route_becomes_deferred_assign_command():
    queue = CommandQueue()
    api = CommandApi(queue, status_snapshot=lambda: {"last_tick": 10}, admin_password="test-password", writes_enabled=True)
    cookie, csrf = _login(api)
    alias = "entity_0123456789ab"
    response = api.handle("POST", f"/api/v1/entities/{alias}/tasks", _write_headers(cookie, csrf, 0, "assign-task-key-1"),
                          _body({"task_kind": "RETREAT_TO_CORE", "priority": 820}))
    assert response is not None and response.status == 202
    assert queue.snapshot()[0].type.value == "ASSIGN_TASK"
    assert queue.snapshot()[0].payload["entity_alias"] == alias


def test_command_api_pause_and_resume_routes_enqueue_bounded_commands():
    queue = CommandQueue()
    api = CommandApi(queue, status_snapshot=lambda: {"last_tick": 10}, admin_password="test-password", writes_enabled=True)
    cookie, csrf = _login(api)
    alias = "entity_0123456789ab"
    paused = api.handle("POST", f"/api/v1/entities/{alias}/pause", _write_headers(cookie, csrf, 0, "pause-task-key-1"), _body({"ttl_ticks": 5}))
    assert paused is not None and paused.status == 202
    assert queue.snapshot()[0].payload["task_kind"] == "HOLD_POSITION"
    resumed = api.handle("POST", f"/api/v1/entities/{alias}/resume", _write_headers(cookie, csrf, 1, "resume-task-key-1"), _body({}))
    assert resumed is not None and resumed.status == 202
    assert queue.snapshot()[1].type.value == "CANCEL"


def test_lan_command_access_requires_explicit_origin_allowlist():
    queue = CommandQueue()
    blocked = CommandApi(queue, status_snapshot=lambda: {}, admin_password="test-password", writes_enabled=True, allow_lan=True)
    denied = blocked.handle("POST", "/api/v1/session", {"Host": "tactic.lan", "Origin": "http://tactic.lan"},
                            _body({"password": "test-password"}), remote_host="192.168.1.20")
    assert denied is not None and denied.status == 403
    allowed = CommandApi(queue, status_snapshot=lambda: {}, admin_password="test-password", writes_enabled=True,
                         allow_lan=True, allowed_origins=frozenset({"http://tactic.lan"}))
    accepted = allowed.handle("POST", "/api/v1/session", {"Host": "tactic.lan", "Origin": "http://tactic.lan"},
                              _body({"password": "test-password"}), remote_host="192.168.1.20")
    assert accepted is not None and accepted.status == 200


def test_accepted_manual_assignment_persists_and_replaces_only_its_current_actor():
    queue = CommandQueue()
    worker = unit(1, UnitType.WORKER, (1, 0))
    alias = entity_alias(worker.id)
    assert alias is not None
    queue.enqueue({"type": "ASSIGN_TASK", "payload": {"entity_alias": alias, "task_kind": "MOVE_TO_CELL",
                                                           "priority": 800, "target": [3, 0]}},
                  issuer="test", current_tick=1, idempotency_key="manual-move-task-1", expected_version=0)
    runtime = AgentRuntime(command_queue=queue)
    result = runtime.decide(turn(tick=2, owned_core=core(), units=(worker,)))

    intent = next(item for item in result.intents if item.actor_id == worker.id)
    assert intent.reason == "manual_task_move"
    runtime.commit(result)
    assert runtime.memory.manual_assignments[alias]["kind"] == "MOVE_TO_CELL"
    assert queue.snapshot()[0].status is CommandStatus.APPLIED


def test_manual_assignment_cancel_is_applied_only_after_accepted_tick():
    queue = CommandQueue()
    worker = unit(1, UnitType.WORKER, (1, 0))
    alias = entity_alias(worker.id)
    assert alias is not None
    runtime = AgentRuntime(command_queue=queue)
    queue.enqueue({"type": "ASSIGN_TASK", "payload": {"entity_alias": alias, "task_kind": "HOLD_POSITION", "priority": 800}},
                  issuer="test", current_tick=1, idempotency_key="manual-hold-task-1", expected_version=0)
    runtime.commit(runtime.decide(turn(tick=2, owned_core=core(), units=(worker,))))
    queue.enqueue({"type": "CANCEL", "payload": {"entity_alias": alias}}, issuer="test", current_tick=2,
                  idempotency_key="manual-cancel-task-1", expected_version=1)

    result = runtime.decide(turn(tick=3, owned_core=core(), units=(worker,)))
    assert alias not in result.next_memory.manual_assignments
    assert alias in runtime.memory.manual_assignments
    runtime.commit(result)
    assert alias not in runtime.memory.manual_assignments


def test_core_migration_command_activates_only_after_accepted_tick():
    queue = CommandQueue()
    runtime = AgentRuntime(command_queue=queue)
    queue.enqueue({"type": "START_CORE_MIGRATION", "payload": {"target": [2, 0]}}, issuer="test", current_tick=1,
                  idempotency_key="manual-core-move-1", expected_version=0)

    result = runtime.decide(turn(tick=2, owned_core=core(position=(0, 0))))

    core_intent = next(item for item in result.intents if item.is_core)
    assert core_intent.action.value == "START_MOVE"
    assert runtime.memory.objective_states.get("migration") is None
    runtime.commit(result)
    assert runtime.memory.objective_states["migration"]["manual"] is True


def test_command_api_core_migration_route_queues_a_migration_command():
    queue = CommandQueue()
    api = CommandApi(queue, status_snapshot=lambda: {"last_tick": 10}, admin_password="test-password", writes_enabled=True)
    cookie, csrf = _login(api)
    response = api.handle("POST", "/api/v1/core/migrations", _write_headers(cookie, csrf, 0, "core-migrate-key-1"),
                          _body({"target": [8, 2]}))
    assert response is not None and response.status == 202
    assert queue.snapshot()[0].type.value == "START_CORE_MIGRATION"


def test_command_api_core_migration_cancel_route_is_deferred():
    queue = CommandQueue()
    api = CommandApi(queue, status_snapshot=lambda: {"last_tick": 10}, admin_password="test-password", writes_enabled=True)
    cookie, csrf = _login(api)
    response = api.handle("DELETE", "/api/v1/core/migrations", _write_headers(cookie, csrf, 0, "core-cancel-key-1"), b"")
    assert response is not None and response.status == 202
    assert queue.snapshot()[0].type.value == "CANCEL_CORE_MIGRATION"


def test_policy_command_is_deferred_then_persisted_and_used_by_scheduler():
    queue = CommandQueue()
    worker = unit(1, UnitType.WORKER, (0, 0))
    queue.enqueue({"type": "UPDATE_POLICY", "payload": {"posture": "ECONOMY"}}, issuer="test", current_tick=1,
                  idempotency_key="policy-economy-key-1", expected_version=0)
    runtime = AgentRuntime(config=AgentConfig(planner_canary=True), command_queue=queue)

    result = runtime.decide(turn(tick=2, owned_core=core(), units=(worker,), resource_cells=((3, 0),)))

    assert runtime.memory.policy_state["posture"] == "BALANCED"
    assert result.next_memory.policy_state == {"version": 1, "posture": "ECONOMY", "effective_tick": 2}
    scheduled = result.next_memory.scheduler_assignments[entity_alias(worker.id) or ""]
    assert scheduled["kind"] == "HARVEST_RESOURCE"
    runtime.commit(result)
    assert runtime.memory.policy_state == {"version": 1, "posture": "ECONOMY", "effective_tick": 2}
    assert queue.policy_snapshot() == {"version": 1, "posture": "ECONOMY", "effective_tick": 2}


def test_command_api_policy_route_returns_only_applied_policy():
    queue = CommandQueue()
    api = CommandApi(queue, status_snapshot=lambda: {"last_tick": 10}, admin_password="test-password", writes_enabled=True)
    cookie, csrf = _login(api)
    queued = api.handle("PATCH", "/api/v1/policy", _write_headers(cookie, csrf, 0, "policy-api-key-1"),
                        _body({"posture": "DEFENSIVE"}))
    assert queued is not None and queued.status == 202
    before = api.handle("GET", "/api/v1/policy", {"Cookie": cookie}, b"")
    assert before is not None and json.loads(before.body)["posture"] == "BALANCED"
    queue.finalize(queue.prepare_for_tick(11, set()), accepted=True)
    after = api.handle("GET", "/api/v1/policy", {"Cookie": cookie}, b"")
    assert after is not None and json.loads(after.body) == {"version": 1, "posture": "DEFENSIVE", "effective_tick": 11}


def test_empty_queue_restores_accepted_policy_from_runtime_memory_after_restart():
    queue = CommandQueue()
    AgentRuntime(memory=AgentMemory(policy_state={"version": 3, "posture": "AGGRESSIVE", "effective_tick": 17}),
                 command_queue=queue)

    assert queue.policy_snapshot() == {"version": 3, "posture": "AGGRESSIVE", "effective_tick": 17}
