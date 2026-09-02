"""Bounded, credential-free decision explanation records."""

from __future__ import annotations

import json
import os
import threading
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic
from typing import Any, Mapping, Callable

from .values import FrozenJson, freeze_mapping, freeze_optional_text, freeze_sequence, freeze_text, thaw_json


TRACE_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class NodeTrace:
    node_id: str
    status: str
    reason: str
    duration_ms: float = 0.0
    children: tuple[FrozenJson, ...] = ()
    parameters: Mapping[str, FrozenJson] = field(default_factory=dict)
    metadata: Mapping[str, FrozenJson] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("node_id", "status", "reason"):
            object.__setattr__(self, name, freeze_text(getattr(self, name), field_name=f"NodeTrace.{name}"))
        object.__setattr__(self, "children", freeze_sequence(
            self.children, field_name="NodeTrace.children"
        ))
        object.__setattr__(self, "parameters", freeze_mapping(
            self.parameters, field_name="NodeTrace.parameters"
        ))
        object.__setattr__(self, "metadata", freeze_mapping(
            self.metadata, field_name="NodeTrace.metadata"
        ))


@dataclass(frozen=True, slots=True)
class EntityTrace:
    actor_alias: str
    entity_kind: str
    current_task: str
    goal: str
    action: str
    reason_codes: tuple[FrozenJson, ...]
    status: str = "IDLE"
    duration_ms: float = 0.0
    task_status: str = "UNKNOWN"
    assignment_status: str = "IDLE"
    current_cell: tuple[int, int] | None = None
    target_cell: tuple[int, int] | None = None
    blocker: str | None = None
    waited_ticks: int = 0
    eta_ticks: int | None = None
    assignment: Mapping[str, Any] | None = None
    next_step: str | None = None
    candidate_intents: tuple[Mapping[str, FrozenJson], ...] = ()
    winning_intent: Mapping[str, Any] | None = None
    wait_kind: str | None = None
    wake_condition: str | None = None
    node_path: tuple[Mapping[str, FrozenJson], ...] = ()
    summary_only: bool = False

    def __post_init__(self) -> None:
        for name in ("actor_alias", "entity_kind", "current_task", "goal", "action", "status", "task_status", "assignment_status"):
            object.__setattr__(self, name, freeze_text(getattr(self, name), field_name=f"EntityTrace.{name}"))
        for name in ("blocker", "next_step", "wait_kind", "wake_condition"):
            object.__setattr__(self, name, freeze_optional_text(getattr(self, name), field_name=f"EntityTrace.{name}"))
        object.__setattr__(self, "reason_codes", freeze_sequence(
            self.reason_codes, field_name="EntityTrace.reason_codes"
        ))
        for name in ("current_cell", "target_cell"):
            value = getattr(self, name)
            if value is not None:
                frozen_cell = freeze_sequence(value, field_name=f"EntityTrace.{name}")
                if len(frozen_cell) != 2 or not all(type(axis) is int for axis in frozen_cell):
                    raise TypeError(f"EntityTrace.{name} must contain exactly two integers")
                object.__setattr__(self, name, frozen_cell)
        node_values = [
            {
                "node_id": node.node_id,
                "status": node.status,
                "reason": node.reason,
                "duration_ms": node.duration_ms,
                **({"children": node.children} if node.children else {}),
                **({"parameters": node.parameters} if node.parameters else {}),
                **({"metadata": node.metadata} if node.metadata else {}),
            } if isinstance(node, NodeTrace) else node
            for node in self.node_path
        ]
        frozen_nodes = freeze_sequence(node_values, field_name="EntityTrace.node_path")
        if not all(isinstance(node, Mapping) for node in frozen_nodes):
            raise TypeError("EntityTrace.node_path entries must be mappings or NodeTrace values")
        object.__setattr__(self, "node_path", frozen_nodes)
        if self.assignment is not None:
            object.__setattr__(self, "assignment", freeze_mapping(self.assignment, field_name="EntityTrace.assignment"))
        frozen_candidates = freeze_sequence(self.candidate_intents, field_name="EntityTrace.candidate_intents")
        if not all(isinstance(item, Mapping) for item in frozen_candidates):
            raise TypeError("EntityTrace.candidate_intents entries must be mappings")
        object.__setattr__(self, "candidate_intents", frozen_candidates)
        if self.winning_intent is not None:
            object.__setattr__(self, "winning_intent", freeze_mapping(self.winning_intent, field_name="EntityTrace.winning_intent"))


@dataclass(frozen=True, slots=True)
class TraceTruncation:
    truncated: bool = False
    dropped_entities: int = 0
    dropped_nodes: int = 0
    byte_limit_reached: bool = False


@dataclass(frozen=True, slots=True)
class DecisionTrace:
    schema_version: int
    tick: int
    planner_version: str
    policy_version: int
    entity_traces: tuple[EntityTrace, ...]
    goal_summaries: tuple[Mapping[str, Any], ...] = ()
    task_transitions: tuple[Mapping[str, Any], ...] = ()
    arbitration: tuple[Mapping[str, Any], ...] = ()
    validation: tuple[Mapping[str, Any], ...] = ()
    command_results: tuple[Mapping[str, Any], ...] = ()
    timings: Mapping[str, float] = field(default_factory=dict)
    causality: Mapping[str, Any] = field(default_factory=dict)
    truncation: TraceTruncation = TraceTruncation()

    def __post_init__(self) -> None:
        object.__setattr__(self, "planner_version", freeze_text(
            self.planner_version, field_name="DecisionTrace.planner_version"
        ))
        entity_traces = tuple(self.entity_traces)
        if not all(isinstance(item, EntityTrace) for item in entity_traces):
            raise TypeError("DecisionTrace.entity_traces entries must be EntityTrace values")
        object.__setattr__(self, "entity_traces", entity_traces)
        for name in ("goal_summaries", "task_transitions", "arbitration", "validation", "command_results"):
            frozen = freeze_sequence(getattr(self, name), field_name=f"DecisionTrace.{name}")
            if not all(isinstance(item, Mapping) for item in frozen):
                raise TypeError(f"DecisionTrace.{name} entries must be mappings")
            object.__setattr__(self, name, frozen)
        object.__setattr__(self, "timings", freeze_mapping(self.timings, field_name="DecisionTrace.timings"))
        object.__setattr__(self, "causality", freeze_mapping(self.causality, field_name="DecisionTrace.causality"))


@dataclass(frozen=True, slots=True)
class TraceLimits:
    max_bytes: int = 64 * 1024
    max_entities: int = 200
    max_nodes: int = 2_048
    max_nodes_per_entity: int = 64

    def __post_init__(self) -> None:
        for name in ("max_bytes", "max_entities", "max_nodes", "max_nodes_per_entity"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        if self.max_bytes < 512:
            raise ValueError("max_bytes is below the 512-byte minimum trace summary")


def trace_record(trace: DecisionTrace) -> dict[str, Any]:
    def entity_record(item: EntityTrace) -> dict[str, Any]:
        minimal = {
            "actor_alias": item.actor_alias, "entity_kind": item.entity_kind,
            "current_task": item.current_task, "goal": item.goal,
            "action": item.action, "reason_codes": list(item.reason_codes[:1]),
            "status": item.status, "duration_ms": item.duration_ms,
            "task_status": item.task_status,
            "assignment_status": item.assignment_status,
        }
        if item.summary_only:
            return minimal
        return {**minimal,
            "current_cell": list(item.current_cell) if item.current_cell else None,
            "target_cell": list(item.target_cell) if item.target_cell else None,
            "blocker": item.blocker, "waited_ticks": item.waited_ticks,
            "eta_ticks": item.eta_ticks, "assignment": thaw_json(item.assignment) if item.assignment else None,
            "next_step": item.next_step, "candidate_intents": [thaw_json(value) for value in item.candidate_intents],
            "winning_intent": thaw_json(item.winning_intent) if item.winning_intent else None, "wait_kind": item.wait_kind,
            "wake_condition": item.wake_condition,
            "node_path": [thaw_json(node) for node in item.node_path],
        }
    return {
        "schema_version": trace.schema_version,
        "tick": trace.tick,
        "planner_version": trace.planner_version,
        "policy_version": trace.policy_version,
        "entity_traces": [entity_record(item) for item in trace.entity_traces],
        "goal_summaries": [thaw_json(value) for value in trace.goal_summaries],
        "task_transitions": [thaw_json(value) for value in trace.task_transitions],
        "arbitration": [thaw_json(value) for value in trace.arbitration],
        "validation": [thaw_json(value) for value in trace.validation],
        "command_results": [thaw_json(value) for value in trace.command_results],
        "timings": thaw_json(trace.timings),
        "causality": thaw_json(trace.causality),
        "truncation": {
            "truncated": trace.truncation.truncated,
            "dropped_entities": trace.truncation.dropped_entities,
            "dropped_nodes": trace.truncation.dropped_nodes,
            "byte_limit_reached": trace.truncation.byte_limit_reached,
        },
    }


def audit_record(event: "AuditEvent") -> dict[str, Any]:
    """Serialize the Phase-one audit allowlist without command payloads."""
    return {
        "schema_version": TRACE_SCHEMA_VERSION,
        "event_id": event.event_id,
        "wall_time": event.wall_time,
        "tick": event.tick,
        "actor": event.actor,
        "operation": event.operation,
        "subject_alias": event.subject_alias,
        "outcome": event.outcome,
        "before_version": event.before_version,
        "after_version": event.after_version,
        "reason": event.reason,
        "request_hash": event.request_hash,
    }


class BoundedTraceSink:
    """Bounded non-blocking producer with an optional dedicated JSONL writer."""

    def __init__(
        self,
        path: Path | None = None,
        max_records: int = 256,
        max_bytes: int = 64 * 1024,
        max_file_bytes: int = 32 * 1024 * 1024,
        history_files: int = 11,
        on_record: Callable[[dict[str, Any]], None] | None = None,
        can_discard: Callable[[Path], bool] | None = None,
    ) -> None:
        if max_records <= 0:
            raise ValueError("max_records must be positive")
        if max_bytes < 128:
            raise ValueError("max_bytes is below the 128-byte minimum trace sink summary")
        if max_file_bytes < max_bytes:
            raise ValueError("max_file_bytes must be at least max_bytes")
        if history_files < 0:
            raise ValueError("history_files must be nonnegative")
        self.path = path
        self.max_bytes = max_bytes
        self.max_file_bytes = max_file_bytes
        self.history_files = history_files
        self.on_record = on_record
        self.can_discard = can_discard
        self._max_records = max_records
        self._records: deque[tuple[int, bytes]] = deque()
        self.dropped = 0
        self.write_failures = 0
        self._drop_ticks: deque[int] = deque()
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._active_writes = 0
        self._closed = False
        self._writer_failed = False
        self.thread_name = f"decision-trace-writer-{id(self):x}"
        self._thread: threading.Thread | None = None
        if path is not None:
            self._thread = threading.Thread(target=self._write_loop, name=self.thread_name, daemon=True)
            self._thread.start()

    def emit(self, trace: DecisionTrace) -> bool:
        if not trace.entity_traces and trace.truncation.dropped_entities:
            record = {
                "record_type": "trace_drop_summary", "tick": trace.tick,
                "planner_version": trace.planner_version,
                "dropped_entities": trace.truncation.dropped_entities,
            }
        else:
            record = {"record_type": "decision_trace", **trace_record(trace)}
        if self.on_record is not None:
            try:
                self.on_record(record)
            except Exception:
                # Observability exports are explicitly best-effort.
                pass
        payload = json.dumps(record, separators=(",", ":"), sort_keys=True).encode()
        return self._enqueue(trace.tick, payload, trace.planner_version)

    def _enqueue(self, tick: int, payload: bytes, planner_version: str) -> bool:
        """Atomically enqueue one already-redacted JSON record or a compact summary."""
        if len(payload) + 1 > self.max_bytes:
            payload = self._tick_summary(tick, planner_version)
        with self._condition:
            if self._closed or self._writer_failed:
                if self._writer_failed:
                    self.dropped += 1
                    self._drop_ticks.append(tick)
                return False
            if len(self._records) < self._max_records:
                self._records.append((tick, payload))
                self._condition.notify()
                return True

            dropped_tick, _ = self._records.popleft()
            self.dropped += 1
            dropped_total = self.dropped
            self._drop_ticks.append(dropped_tick)
            summary = self._tick_summary(
                tick,
                planner_version,
                dropped_tick=dropped_tick,
                dropped_total=dropped_total,
            )
            self._records.append((tick, summary))
            self._condition.notify()
            return False

    def drain(self) -> tuple[bytes, ...]:
        with self._condition:
            records = tuple(payload for _, payload in self._records)
            self._records.clear()
            self._condition.notify_all()
            return records

    def _drop_summaries(self) -> tuple[tuple[bytes, tuple[int, ...]], ...]:
        with self._lock:
            if not self._drop_ticks:
                return ()
            ticks = sorted(set(self._drop_ticks))
            self._drop_ticks.clear()
            dropped_total = self.dropped
        ranges: list[list[int]] = []
        for tick in ticks:
            if ranges and tick == ranges[-1][1] + 1:
                ranges[-1][1] = tick
            else:
                ranges.append([tick, tick])
        batches: list[tuple[bytes, tuple[int, ...]]] = []
        current: list[list[int]] = []
        for item in ranges:
            candidate = [*current, item]
            record = {
                "record_type": "trace_drop_summary",
                "dropped_total": dropped_total,
                "tick_ranges": candidate,
                "truncated": False,
            }
            encoded = json.dumps(record, separators=(",", ":"), sort_keys=True).encode()
            if len(encoded) + 1 <= self.max_bytes:
                current = candidate
                continue
            if not current:
                raise ValueError("max_bytes cannot contain one dropped Tick range")
            batches.append((json.dumps({
                "record_type": "trace_drop_summary",
                "dropped_total": dropped_total,
                "tick_ranges": current,
                "truncated": False,
            }, separators=(",", ":"), sort_keys=True).encode(), tuple(
                tick for start, end in current for tick in range(start, end + 1)
            )))
            current = [item]
        if current:
            batches.append((json.dumps({
                "record_type": "trace_drop_summary",
                "dropped_total": dropped_total,
                "tick_ranges": current,
                "truncated": False,
            }, separators=(",", ":"), sort_keys=True).encode(), tuple(
                tick for start, end in current for tick in range(start, end + 1)
            )))
        return tuple(batches)

    def _write_drop_summaries(self, stream: Any) -> Any:
        """Write extracted drop batches, restoring any batch that did not persist."""
        batches = self._drop_summaries()
        for index, (summary, ticks) in enumerate(batches):
            try:
                stream = self._write_line(stream, summary)
            except OSError:
                remaining = (*ticks, *(
                    tick for _, batch_ticks in batches[index + 1:] for tick in batch_ticks
                ))
                with self._lock:
                    self._drop_ticks.extendleft(reversed(remaining))
                raise
        return stream

    def _tick_summary(
        self,
        tick: int,
        planner_version: str,
        *,
        dropped_tick: int | None = None,
        dropped_total: int | None = None,
    ) -> bytes:
        record: dict[str, Any] = {
            "record_type": "trace_tick_summary", "tick": tick,
            "planner_version": planner_version,
        }
        if dropped_tick is not None:
            record["dropped_tick"] = dropped_tick
        if dropped_total is not None:
            record["dropped_total"] = dropped_total
        encoded = json.dumps(record, separators=(",", ":"), sort_keys=True).encode()
        if len(encoded) + 1 <= self.max_bytes:
            return encoded
        compact = {"record_type": "trace_tick_summary", "tick": tick, "truncated": True}
        encoded = json.dumps(compact, separators=(",", ":"), sort_keys=True).encode()
        if len(encoded) + 1 > self.max_bytes:
            raise ValueError("max_bytes cannot contain a trace tick summary")
        return encoded

    @property
    def dropped_tick_summaries(self) -> tuple[int, ...]:
        with self._lock:
            return tuple(self._drop_ticks)

    def _write_loop(self) -> None:
        assert self.path is not None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            stream = self.path.open("ab", buffering=0)
            try:
                while True:
                    with self._condition:
                        self._condition.wait_for(lambda: bool(self._records) or self._closed)
                        if not self._records:
                            break
                        _, payload = self._records.popleft()
                        self._active_writes += 1
                    try:
                        stream = self._write_line(stream, payload)
                        stream = self._write_drop_summaries(stream)
                    except OSError:
                        with self._lock:
                            self.write_failures += 1
                        self._remember_failed_payload(payload)
                    finally:
                        with self._condition:
                            self._active_writes -= 1
                            self._condition.notify_all()
                stream = self._write_drop_summaries(stream)
                stream.flush()
                os.fsync(stream.fileno())
            finally:
                stream.close()
        except OSError:
            with self._condition:
                self.write_failures += 1
                self._writer_failed = True
                queued = tuple(self._records)
                self._records.clear()
                self._condition.notify_all()
            for _, payload in queued:
                self._remember_failed_payload(payload)

    def _write_line(self, stream: Any, payload: bytes) -> Any:
        line = payload + b"\n"
        if stream.tell() and stream.tell() + len(line) > self.max_file_bytes:
            stream.flush()
            os.fsync(stream.fileno())
            stream.close()
            self._rotate_files()
            assert self.path is not None
            stream = self.path.open("ab", buffering=0)
        stream.write(line)
        return stream

    def _rotate_files(self) -> bool:
        assert self.path is not None
        discard_target = self.path if self.history_files == 0 else self.path.with_name(
            f"{self.path.name}.{self.history_files}"
        )
        if discard_target.exists() and self.can_discard is not None and not self.can_discard(discard_target):
            # The callback only enqueues/checks work; never wait for the
            # network on this dedicated writer thread.
            return False
        if self.history_files == 0:
            self.path.unlink(missing_ok=True)
            return True
        oldest = self.path.with_name(f"{self.path.name}.{self.history_files}")
        oldest.unlink(missing_ok=True)
        for index in range(self.history_files - 1, 0, -1):
            source = self.path.with_name(f"{self.path.name}.{index}")
            if source.exists():
                os.rename(source, self.path.with_name(f"{self.path.name}.{index + 1}"))
        if self.path.exists():
            os.rename(self.path, self.path.with_name(f"{self.path.name}.1"))
        return True

    def _remember_failed_payload(self, payload: bytes) -> None:
        try:
            tick = int(json.loads(payload).get("tick"))
        except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
            return
        with self._lock:
            self.dropped += 1
            self._drop_ticks.append(tick)

    def flush(self, timeout: float = 5.0) -> bool:
        deadline = monotonic() + timeout
        with self._condition:
            while (self._records or self._active_writes) and monotonic() < deadline:
                self._condition.wait(timeout=max(0.0, deadline - monotonic()))
            return not self._records and self._active_writes == 0

    def close(self, timeout: float = 5.0) -> bool:
        with self._condition:
            self._closed = True
            self._condition.notify_all()
        self.flush(timeout)
        if self._thread is not None:
            self._thread.join(max(0.0, timeout))
            return not self._thread.is_alive()
        return True

    def __enter__(self) -> "BoundedTraceSink":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


class BoundedAuditSink(BoundedTraceSink):
    """The same bounded asynchronous writer for credential-free audit events."""

    def __init__(
        self,
        path: Path | None = None,
        max_records: int = 256,
        max_bytes: int = 64 * 1024,
        max_file_bytes: int = 16 * 1024 * 1024,
        history_files: int = 7,
    ) -> None:
        super().__init__(path, max_records, max_bytes, max_file_bytes, history_files)

    def emit(self, event: "AuditEvent") -> bool:  # type: ignore[override]
        payload = json.dumps(
            {"record_type": "audit_event", **audit_record(event)},
            separators=(",", ":"), sort_keys=True,
        ).encode()
        return self._enqueue(event.tick, payload, "audit-v1")
