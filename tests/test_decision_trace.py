import json
import ast
import threading
from time import monotonic
from pathlib import Path

from arena_hero import UnitType

from arena_tactic import AgentMemory, AgentRuntime
from arena_tactic.models import ActionIntent, ActionKind, StrategicMode
import pytest

from arena_tactic.domain import (
    AuditEvent,
    BoundedAuditSink,
    BoundedTraceSink,
    DecisionTrace,
    EntityTrace,
    NodeTrace,
    TraceLimits,
)
from arena_tactic.observability import decision_trace_record
from arena_tactic.planning.legacy import classify_wait_reason

from .factories import core, turn, unit


def _plan_payload(game_turn):
    return game_turn.plan.model_dump(mode="json")


def test_trace_explains_each_current_entity():
    game_turn = turn(
        tick=11,
        owned_core=core(),
        units=(
            unit(1, UnitType.WORKER, (0, 0)),
            unit(2, UnitType.RANGER, (0, 1)),
        ),
        resource_cells=((2, 0),),
    )

    result = AgentRuntime().decide(game_turn)

    assert isinstance(result.trace, DecisionTrace)
    assert result.trace.schema_version == 1
    assert len(result.trace.entity_traces) == 3
    for entity in result.trace.entity_traces:
        assert entity.actor_alias
        assert entity.current_task
        assert entity.goal
        assert entity.action
        assert entity.reason_codes
        assert entity.status in {"SUCCESS", "RUNNING", "IDLE", "NO_INTENT"}
        assert entity.duration_ms >= 0
        assert entity.waited_ticks >= 0
        if entity.action == "WAIT":
            assert entity.wait_kind in {"ACTIVE", "BLOCKED", "RESOURCE_WAIT", "LIFECYCLE", "SAFETY", "UNKNOWN_WAIT"}
            assert bool(entity.blocker) is (entity.wait_kind in {"BLOCKED", "SAFETY"})
    raw = json.dumps(decision_trace_record(result.trace))
    assert str(game_turn.core.id) not in raw
    assert all(str(item.id) not in raw for item in game_turn.units)
    assert "authorization" not in raw.lower()


def test_trace_truncates_without_blocking():
    game_turn = turn(
        owned_core=core(),
        units=tuple(unit(index, UnitType.WORKER, (index, 1)) for index in range(1, 8)),
    )
    result = AgentRuntime(trace_limits=TraceLimits(max_bytes=3_000, max_entities=2, max_nodes=1)).decide(
        game_turn
    )
    sink = BoundedTraceSink(max_records=1)

    assert result.trace.truncation.truncated
    assert len(result.trace.entity_traces) == 8
    assert sink.emit(result.trace)
    assert not sink.emit(result.trace)
    assert sink.dropped == 1


def test_default_64k_trace_budget_covers_every_current_entity():
    game_turn = turn(
        owned_core=core(),
        units=tuple(unit(index, UnitType.WORKER, (index, 1)) for index in range(1, 31)),
    )

    trace = AgentRuntime().decide(game_turn).trace

    assert trace is not None
    assert len(trace.entity_traces) == 31
    assert trace.truncation.dropped_entities == 0


def test_legacy_planner_sdk_plan_is_unchanged_by_trace_adapter():
    baseline_turn = turn(
        owned_core=core(position=(-2, 0)),
        units=(unit(1, UnitType.WORKER, (0, 0)),),
        resource_cells=((2, 0),),
        resources=10,
    )
    adapted_turn = turn(
        owned_core=core(position=(-2, 0)),
        units=(unit(1, UnitType.WORKER, (0, 0)),),
        resource_cells=((2, 0),),
        resources=10,
    )

    AgentRuntime(enable_trace=False).decide(baseline_turn)
    AgentRuntime(enable_trace=True).decide(adapted_turn)

    assert _plan_payload(adapted_turn) == _plan_payload(baseline_turn)


def test_trace_failure_does_not_block_current_plan(monkeypatch):
    game_turn = turn(owned_core=core(), resources=10)
    runtime = AgentRuntime()

    def fail_trace(*args, **kwargs):
        raise RuntimeError("trace unavailable")

    monkeypatch.setattr(runtime.legacy_planner, "trace", fail_trace)
    result = runtime.decide(game_turn)

    assert result.trace is None
    assert game_turn.plan.core_action is not None


def test_core_missing_still_traces_every_current_unit():
    game_turn = turn(units=(unit(1, UnitType.WORKER, (1, 1)), unit(2, UnitType.RANGER, (2, 2))))
    result = AgentRuntime().decide(game_turn)

    assert len(result.trace.entity_traces) == 2
    assert {item.action for item in result.trace.entity_traces} == {"NO_INTENT"}
    assert all("LIFECYCLE_BLOCKED" in item.reason_codes for item in result.trace.entity_traces)


def test_trace_schema_has_stable_phase_one_sections_and_wait_semantics():
    game_turn = turn(owned_core=core(), units=(unit(1, UnitType.WORKER, (0, 0)),))
    record = decision_trace_record(AgentRuntime().decide(game_turn).trace)

    for field in ("goal_summaries", "task_transitions", "arbitration", "validation", "command_results", "timings"):
        assert field in record
    entity = record["entity_traces"][0]
    for field in ("task_status", "assignment_status", "assignment", "next_step", "candidate_intents", "winning_intent", "wait_kind", "wake_condition", "eta_ticks"):
        assert field in entity
    assert entity["task_status"] == "LEGACY"
    assert entity["assignment_status"] == "IDLE"
    assert {"status", "duration_ms"} <= record["entity_traces"][0]["node_path"][0].keys()
    assert {"status", "duration_ms"} <= record["entity_traces"][0].keys()
    assert record["entity_traces"][0]["duration_ms"] >= 0
    for item in record["entity_traces"]:
        if item["action"] == "WAIT":
            assert item["wait_kind"] in {"ACTIVE", "BLOCKED", "RESOURCE_WAIT", "LIFECYCLE", "SAFETY", "UNKNOWN_WAIT"}
            assert bool(item["blocker"]) is (item["wait_kind"] in {"BLOCKED", "SAFETY"})


def test_trace_sequence_fields_use_shared_deep_freeze_and_reject_unsafe_values():
    candidates = [{"steps": [["MOVE", {"cells": [1, 2]}]]}]
    transitions = [{"changes": [["READY", {"reasons": ["VISIBLE"]}]]}]
    entity = EntityTrace(
        "unit_ab12", "RANGER", "SCOUT", "EXPLORE", "WAIT", ["SAFE"],
        candidate_intents=candidates,
        node_path=[NodeTrace("node", "RUNNING", "safe")],
    )
    trace = DecisionTrace(1, 7, "test", 1, [entity], task_transitions=transitions)
    candidates[0]["steps"][0][1]["cells"].append(3)
    transitions[0]["changes"][0][1]["reasons"].append("CHANGED")

    record = decision_trace_record(trace)
    assert record["entity_traces"][0]["candidate_intents"] == [
        {"steps": [["MOVE", {"cells": [1, 2]}]]}
    ]
    assert record["task_transitions"] == [
        {"changes": [["READY", {"reasons": ["VISIBLE"]}]]}
    ]

    for malicious in (object(), UnitType.WORKER, {"Authorization": "secret"},
                      "00000000-0000-0000-0000-000000000001"):
        with pytest.raises((TypeError, ValueError)):
            EntityTrace("u", "RANGER", "T", "G", "WAIT", [malicious])  # type: ignore[list-item]
        with pytest.raises((TypeError, ValueError)):
            DecisionTrace(1, 1, "test", 1, [entity], task_transitions=[[malicious]])  # type: ignore[list-item]


def test_node_trace_nested_fields_are_deeply_frozen_and_sanitized():
    children = [["child", {"states": ["READY"]}]]
    parameters = {"route": {"cells": [[1, 2]]}}
    metadata = {"labels": ["safe"]}
    node = NodeTrace(
        "node", "RUNNING", "safe", children=children,
        parameters=parameters, metadata=metadata,  # type: ignore[arg-type]
    )
    children[0][1]["states"].append("CHANGED")
    parameters["route"]["cells"].append([3, 4])
    metadata["labels"].append("changed")
    entity = EntityTrace("u", "RANGER", "T", "G", "WAIT", ["SAFE"], node_path=[node])
    record = decision_trace_record(DecisionTrace(1, 1, "test", 1, [entity]))
    serialized = record["entity_traces"][0]["node_path"][0]
    assert serialized["children"] == [["child", {"states": ["READY"]}]]
    assert serialized["parameters"] == {"route": {"cells": [[1, 2]]}}
    assert serialized["metadata"] == {"labels": ["safe"]}

    for kwargs in (
        {"children": [[{"con-troller": "string"}]]},
        {"parameters": {"Controller": "string"}},
        {"metadata": {"id": "00000000-0000-0000-0000-000000000001"}},
        {"metadata": {"kind": UnitType.WORKER}},
    ):
        with pytest.raises((TypeError, ValueError)):
            NodeTrace("node", "RUNNING", "safe", **kwargs)


def test_trace_direct_text_fields_reject_complete_uuids():
    full_uuid = "00000000-0000-0000-0000-000000000001"
    with pytest.raises(ValueError, match="full UUID"):
        NodeTrace(full_uuid, "RUNNING", "safe")
    with pytest.raises(ValueError, match="full UUID"):
        EntityTrace("entity_x", "RANGER", "TASK", full_uuid, "WAIT", ["SAFE"])
    with pytest.raises(ValueError, match="full UUID"):
        DecisionTrace(1, 1, full_uuid, 1, [])


def test_trace_limits_reject_nonpositive_and_impossible_byte_budget():
    for kwargs in ({"max_bytes": 0}, {"max_entities": 0}, {"max_nodes": 0}, {"max_nodes_per_entity": 0}):
        with pytest.raises(ValueError):
            TraceLimits(**kwargs)
    with pytest.raises(ValueError, match="minimum"):
        TraceLimits(max_bytes=8)
    with pytest.raises(ValueError, match="minimum"):
        BoundedTraceSink(max_bytes=8)


def test_hard_byte_limit_is_never_exceeded():
    game_turn = turn(owned_core=core(), units=tuple(unit(i, UnitType.WORKER, (i, 1)) for i in range(1, 10)))
    limits = TraceLimits(max_bytes=3_000, max_entities=20, max_nodes=20)
    trace = AgentRuntime(trace_limits=limits).decide(game_turn).trace
    encoded = json.dumps({"record_type": "decision_trace", **decision_trace_record(trace)}, separators=(",", ":"), sort_keys=True).encode() + b"\n"
    assert len(encoded) <= limits.max_bytes


def test_actual_jsonl_line_including_wrapper_and_newline_respects_limit(tmp_path):
    limits = TraceLimits(max_bytes=3_000, max_entities=2, max_nodes=2)
    sink = BoundedTraceSink(tmp_path / "trace.jsonl")
    runtime = AgentRuntime(trace_limits=limits, trace_sink=sink)
    result = runtime.decide(turn(tick=7, owned_core=core(), units=tuple(unit(i, UnitType.WORKER, (i, 1)) for i in range(1, 10))))
    runtime.commit(result)
    assert sink.close(timeout=2)
    for line in sink.path.read_bytes().splitlines(keepends=True):
        assert len(line) <= limits.max_bytes


def test_impossible_runtime_entity_budget_does_not_block_sdk_plan():
    game_turn = turn(owned_core=core(), units=tuple(unit(i, UnitType.WORKER, (i, 1)) for i in range(1, 10)))
    runtime = AgentRuntime(trace_limits=TraceLimits(max_bytes=512))
    result = runtime.decide(game_turn)

    assert game_turn.plan.core_action is not None
    assert result.intents
    assert result.trace is not None
    assert result.trace.entity_traces == ()
    assert result.trace.truncation.dropped_entities == 10
    assert runtime.trace_drops == 0


def test_tiny_trace_budget_emits_tick_only_drop_summary_without_changing_plan():
    baseline_turn = turn(
        tick=29,
        owned_core=core(),
        units=tuple(unit(i, UnitType.WORKER, (i, 1)) for i in range(1, 10)),
    )
    traced_turn = turn(
        tick=29,
        owned_core=core(),
        units=tuple(unit(i, UnitType.WORKER, (i, 1)) for i in range(1, 10)),
    )
    AgentRuntime(enable_trace=False).decide(baseline_turn)
    sink = BoundedTraceSink(max_bytes=512)
    runtime = AgentRuntime(trace_limits=TraceLimits(max_bytes=512), trace_sink=sink)

    result = runtime.decide(traced_turn)
    runtime.commit(result)
    records = [json.loads(payload) for payload in sink.drain()]

    assert _plan_payload(traced_turn) == _plan_payload(baseline_turn)
    assert records == [{
        "dropped_entities": 10,
        "planner_version": "legacy-strategy-v1",
        "record_type": "trace_drop_summary",
        "tick": 29,
    }]


def test_large_drop_tick_summary_actual_line_respects_byte_limit(tmp_path):
    max_bytes = 512
    sink = BoundedTraceSink(tmp_path / "drops.jsonl", max_records=10_000, max_bytes=max_bytes)
    with sink._lock:
        sink.dropped = 10_000
        sink._drop_ticks.extend(range(1, 20_001, 2))
    assert sink.close(timeout=2)

    lines = sink.path.read_bytes().splitlines(keepends=True)
    records = [json.loads(line) for line in lines]
    assert all(len(line) <= max_bytes for line in lines)
    assert all(record["record_type"] == "trace_drop_summary" for record in records)
    assert all(record["truncated"] is False for record in records)
    accounted = set()
    for record in records:
        for start, end in record["tick_ranges"]:
            accounted.update(range(start, end + 1))
    assert accounted == set(range(1, 20_001, 2))


@pytest.mark.parametrize(
    ("reason", "kind", "wake", "eta"),
    [
        ("resource_route_blocked", "BLOCKED", "ROUTE_AVAILABLE", None),
        ("holding_defense_ring", "ACTIVE", "NEXT_AUTHORITATIVE_TURN", 1),
        ("healing_waits_for_resources", "RESOURCE_WAIT", "CORE_RESOURCES_AVAILABLE", None),
        ("emergency_deposit_queue_wait", "DEPOSIT_QUEUE", "CORE_DEPOSIT_SLOT_AVAILABLE", 1),
        ("emergency_worker_sheltered_near_core", "SHELTER", "NEXT_AUTHORITATIVE_TURN", 1),
        ("core_migration_progresses_naturally", "LIFECYCLE", "CORE_MOVEMENT_COMPLETES", None),
        ("validator_safe_fallback", "SAFETY", "NEXT_AUTHORITATIVE_TURN", 1),
        ("totally_new_reason", "UNKNOWN_WAIT", "NEXT_AUTHORITATIVE_TURN", None),
    ],
)
def test_wait_reason_classification_is_explicit(reason, kind, wake, eta):
    assert classify_wait_reason(reason) == (kind, wake, eta)


def test_every_literal_strategy_wait_reason_has_an_explicit_classification():
    strategy_dir = Path("arena_tactic/strategy")
    trees = [
        ast.parse(path.read_text(encoding="utf-8"))
        for path in sorted(strategy_dir.glob("*.py"))
    ] or [ast.parse(Path("arena_tactic/strategy.py").read_text(encoding="utf-8"))]
    reasons = {
        node.args[1].value
        for tree in trees
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        and node.func.id == "_wait" and len(node.args) > 1
        and isinstance(node.args[1], ast.Constant) and isinstance(node.args[1].value, str)
    }
    reasons.add("validator_safe_fallback")
    assert reasons
    assert {reason for reason in reasons if classify_wait_reason(reason)[0] == "UNKNOWN_WAIT"} == set()


def test_runtime_writes_only_committed_trace_and_closes_writer(tmp_path):
    before = {thread.ident for thread in threading.enumerate()}
    sink = BoundedTraceSink(tmp_path / "runtime" / "decision-trace.jsonl", max_records=2)
    runtime = AgentRuntime(trace_sink=sink)
    rejected = runtime.decide(turn(tick=1, owned_core=core()))
    accepted = runtime.decide(turn(tick=2, owned_core=core()))
    runtime.commit(accepted)
    assert sink.flush(timeout=2)
    sink.close(timeout=2)

    records = [json.loads(line) for line in sink.path.read_text().splitlines()]
    assert [record["tick"] for record in records if record.get("record_type") == "decision_trace"] == [2]
    assert rejected.trace is not None
    assert not any(thread.name == sink.thread_name and thread.is_alive() for thread in threading.enumerate())
    assert before <= {thread.ident for thread in threading.enumerate()}


def test_writer_rotates_by_size_and_close_preserves_current_plus_three_history_files(tmp_path):
    path = tmp_path / "decision-trace.jsonl"
    sink = BoundedTraceSink(
        path,
        max_records=32,
        max_bytes=512,
        max_file_bytes=512,
        history_files=3,
    )
    runtime = AgentRuntime(trace_limits=TraceLimits(max_bytes=512), trace_sink=sink)
    for tick in range(1, 25):
        runtime.commit(runtime.decide(turn(tick=tick, owned_core=core())))

    assert sink.close(timeout=2)
    files = sorted(tmp_path.glob("decision-trace.jsonl*"))

    assert files == [path, path.with_name(path.name + ".1"), path.with_name(path.name + ".2"), path.with_name(path.name + ".3")]
    assert all(0 < item.stat().st_size <= 512 for item in files)
    assert not any(thread.name == sink.thread_name and thread.is_alive() for thread in threading.enumerate())


def test_audit_writer_is_async_redacted_and_rotates_with_eight_file_budget(tmp_path):
    path = tmp_path / "audit.jsonl"
    sink = BoundedAuditSink(path, max_records=32, max_bytes=512, max_file_bytes=512)
    for tick in range(1, 30):
        assert sink.emit(AuditEvent(
            f"audit_{tick}", "2026-08-10T00:00:00Z", tick, "worker",
            "TRACE_COMMITTED", "entity_ab12", "ACCEPTED", reason="safe",
        ))

    assert sink.close(timeout=2)
    files = sorted(tmp_path.glob("audit.jsonl*"))
    assert len(files) <= 8
    assert files[0] == path
    records = [json.loads(line) for file in files for line in file.read_text().splitlines()]
    assert records
    assert all(record["record_type"] == "audit_event" for record in records)
    assert all(len((json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n").encode()) <= 512 for record in records)
    assert "controller" not in json.dumps(records).lower()


def test_full_writer_queue_preserves_compact_tick_summary(tmp_path):
    sink = BoundedTraceSink(tmp_path / "decision-trace.jsonl", max_records=1)
    runtime = AgentRuntime(trace_sink=sink)
    for tick in range(1, 200):
        runtime.commit(runtime.decide(turn(tick=tick, owned_core=core())))
    sink.close(timeout=2)
    records = [json.loads(line) for line in sink.path.read_text().splitlines()]
    assert sink.dropped > 0
    assert any(item["record_type"] in {"trace_tick_summary", "trace_drop_summary"} for item in records)


def test_full_queue_summary_names_evicted_old_tick():
    sink = BoundedTraceSink(max_records=1)
    runtime = AgentRuntime()
    first = runtime.decide(turn(tick=10, owned_core=core())).trace
    second = runtime.decide(turn(tick=11, owned_core=core())).trace
    assert sink.emit(first)
    assert not sink.emit(second)
    records = [json.loads(item) for item in sink.drain()]
    assert records == [{"dropped_tick": 10, "dropped_total": 1, "planner_version": "legacy-strategy-v1", "record_type": "trace_tick_summary", "tick": 11}]


def test_concurrent_emit_never_silently_loses_a_tick(tmp_path):
    path = tmp_path / "concurrent.jsonl"
    sink = BoundedTraceSink(path, max_records=3, max_file_bytes=8 * 1024 * 1024)
    runtime = AgentRuntime()
    traces = [runtime.decide(turn(tick=tick, owned_core=core())).trace for tick in range(1, 401)]
    barrier = threading.Barrier(5)

    def produce(items):
        barrier.wait()
        for item in items:
            sink.emit(item)

    producers = [
        threading.Thread(target=produce, args=(traces[offset::4],))
        for offset in range(4)
    ]
    for producer in producers:
        producer.start()
    barrier.wait()
    for producer in producers:
        producer.join()
    assert sink.close(timeout=5)

    records = [json.loads(line) for line in path.read_text().splitlines()]
    accounted = {record["tick"] for record in records if "tick" in record}
    for record in records:
        if "dropped_tick" in record:
            accounted.add(record["dropped_tick"])
        for start, end in record.get("tick_ranges", ()):
            accounted.update(range(start, end + 1))
    assert accounted == set(range(1, 401))


def test_queue_eviction_while_writer_is_busy_keeps_current_tick_summary(tmp_path, monkeypatch):
    path = tmp_path / "busy-writer.jsonl"
    sink = BoundedTraceSink(path, max_records=1)
    runtime = AgentRuntime()
    entered_write = threading.Event()
    release_write = threading.Event()
    original = sink._write_line

    def blocked_write(stream, payload):
        if not entered_write.is_set():
            entered_write.set()
            assert release_write.wait(2)
        return original(stream, payload)

    monkeypatch.setattr(sink, "_write_line", blocked_write)
    assert sink.emit(runtime.decide(turn(tick=1, owned_core=core())).trace)
    assert entered_write.wait(2)
    assert sink.emit(runtime.decide(turn(tick=2, owned_core=core())).trace)
    assert not sink.emit(runtime.decide(turn(tick=3, owned_core=core())).trace)
    release_write.set()
    assert sink.close(timeout=5)

    records = [json.loads(line) for line in path.read_text().splitlines()]
    accounted = {record["tick"] for record in records if "tick" in record}
    for record in records:
        if "dropped_tick" in record:
            accounted.add(record["dropped_tick"])
        for start, end in record.get("tick_ranges", ()):
            accounted.update(range(start, end + 1))
    assert accounted == {1, 2, 3}
    assert any(record.get("tick") == 3 for record in records)


def test_writer_failure_retains_tick_summary_and_counter(tmp_path):
    bad_path = tmp_path / "is-a-directory"
    bad_path.mkdir()
    sink = BoundedTraceSink(bad_path, max_records=2)
    deadline = monotonic() + 2
    while not sink.write_failures and monotonic() < deadline:
        threading.Event().wait(0.005)
    assert sink.write_failures >= 1
    runtime = AgentRuntime(trace_sink=sink)
    runtime.commit(runtime.decide(turn(tick=41, owned_core=core())))
    assert sink.close(timeout=2)
    assert sink.write_failures >= 1
    assert sink.dropped >= 1
    assert 41 in sink.dropped_tick_summaries


def test_legacy_equivalence_across_lifecycle_wait_core_and_main_unit_actions():
    scenarios = (
        turn(tick=2, units=(unit(1, UnitType.WORKER, (0, 0)),)),
        turn(tick=2, owned_core=core(), resources=10),
        turn(tick=2, owned_core=core(), units=(unit(1, UnitType.WORKER, (0, 0)),), resource_cells=((0, 0),)),
        turn(tick=2, owned_core=core(), units=(unit(1, UnitType.WORKER, (0, 0), cargo=1),), resources=0),
        turn(tick=2, owned_core=core(), units=(unit(2, UnitType.RANGER, (0, 0)),), enemies=(unit(9, UnitType.WORKER, (0, 2), controlled=False),)),
        turn(tick=2, owned_core=core(), units=(unit(3, UnitType.VANGUARD, (0, 0)),), enemies=(unit(9, UnitType.WORKER, (0, 1), controlled=False),)),
    )
    for original in scenarios:
        clone = turn(tick=original.tick, owned_core=original.core.view if original.core else None, units=tuple(item.view for item in original.units), enemies=original.visible_enemies, resources=original.resources, resource_cells=original.resource_cells)
        memory = AgentMemory()
        without = AgentRuntime(memory=memory.clone(), enable_trace=False).decide(original)
        with_trace = AgentRuntime(memory=memory.clone(), enable_trace=True).decide(clone)
        assert _plan_payload(original) == _plan_payload(clone)
        assert without.next_memory.to_dict() == with_trace.next_memory.to_dict()


def test_rejected_legacy_intent_is_traced_without_changing_empty_plan(monkeypatch):
    import arena_tactic.runtime as runtime_module

    game_turn = turn(owned_core=core())
    invalid = ActionIntent(game_turn.core.id, True, ActionKind.HARVEST, 1, "invalid_core_harvest")
    monkeypatch.setattr(runtime_module, "propose_intents", lambda *args: (StrategicMode.ECONOMY, (invalid,), False))
    result = AgentRuntime().decide(game_turn)
    assert game_turn.plan.core_action.type == "WAIT"
    assert result.rejected_intents
    assert result.trace.validation[0]["result"] == "REJECTED"
