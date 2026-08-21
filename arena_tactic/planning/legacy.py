"""Read-only projection around the unchanged legacy strategy planner."""

from __future__ import annotations

import json
from dataclasses import replace
from time import perf_counter

from arena_hero import CoreView

from ..context import DecisionContext
from ..domain.trace import (
    TRACE_SCHEMA_VERSION,
    DecisionTrace,
    EntityTrace,
    NodeTrace,
    TraceLimits,
    TraceTruncation,
    trace_record,
)
from ..identity import entity_alias
from ..memory import AgentMemory
from ..models import ActionIntent
from ..models import RejectedIntent


_WAIT_REASONS = {
    "deposit_waits_for_core_migration": ("LIFECYCLE", "CORE_MOVEMENT_COMPLETES", None),
    "no_safe_route_with_cargo": ("SAFETY", "SAFE_ROUTE_AVAILABLE", None),
    "healing_waits_for_resources": ("RESOURCE_WAIT", "CORE_RESOURCES_AVAILABLE", None),
    "resource_route_blocked": ("BLOCKED", "ROUTE_AVAILABLE", None),
    "exploration_route_blocked": ("BLOCKED", "ROUTE_AVAILABLE", None),
    "no_resource_or_frontier": ("RESOURCE_WAIT", "RESOURCE_OR_FRONTIER_VISIBLE", None),
    "critical_retreat_blocked": ("SAFETY", "SAFE_ROUTE_AVAILABLE", None),
    "emergency_worker_rally_blocked": ("SAFETY", "SAFE_ROUTE_AVAILABLE", None),
    "beacon_route_blocked": ("BLOCKED", "ROUTE_AVAILABLE", None),
    "enemy_approach_blocked": ("BLOCKED", "ROUTE_AVAILABLE", None),
    "guard_route_blocked": ("BLOCKED", "ROUTE_AVAILABLE", None),
    "patrol_route_blocked": ("BLOCKED", "ROUTE_AVAILABLE", None),
    "hunter_route_blocked": ("BLOCKED", "ROUTE_AVAILABLE", None),
    "intercept_route_blocked": ("BLOCKED", "ROUTE_AVAILABLE", None),
    "mineral_tank_route_blocked": ("BLOCKED", "ROUTE_AVAILABLE", None),
    "intercept_firing_route_blocked": ("BLOCKED", "ROUTE_AVAILABLE", None),
    "hidden_attacker_search_blocked": ("BLOCKED", "ROUTE_AVAILABLE", None),
    "holding_defense_ring": ("ACTIVE", "NEXT_AUTHORITATIVE_TURN", 1),
    "hold_vanguard_mineral_tank": ("ACTIVE", "NEXT_AUTHORITATIVE_TURN", 1),
    "scout_route_blocked": ("BLOCKED", "ROUTE_AVAILABLE", None),
    "firing_route_blocked": ("BLOCKED", "ROUTE_AVAILABLE", None),
    "core_migration_progresses_naturally": ("LIFECYCLE", "CORE_MOVEMENT_COMPLETES", None),
    "resources_reserved_or_no_legal_core_action": ("RESOURCE_WAIT", "CORE_RESOURCES_OR_LEGAL_ACTION", None),
    "validator_safe_fallback": ("SAFETY", "NEXT_AUTHORITATIVE_TURN", 1),
}


def classify_wait_reason(reason: str) -> tuple[str, str, int | None]:
    """Classify only reviewed strategy reasons; unknown values remain explicit."""
    return _WAIT_REASONS.get(reason, ("UNKNOWN_WAIT", "NEXT_AUTHORITATIVE_TURN", None))


def _jsonl_size(trace: DecisionTrace) -> int:
    record = {"record_type": "decision_trace", **trace_record(trace)}
    return len(json.dumps(record, separators=(",", ":"), sort_keys=True).encode()) + 1


class LegacyPlannerAdapter:
    planner_version = "legacy-strategy-v1"

    def trace(
        self,
        context: DecisionContext,
        intents: tuple[ActionIntent, ...],
        rejected_intents: tuple[RejectedIntent, ...],
        memory: AgentMemory,
        limits: TraceLimits,
        decision_ms: float = 0.0,
    ) -> DecisionTrace:
        entities: list[EntityTrace] = []
        dropped_nodes = 0
        winners = {intent.actor_id: intent for intent in intents}
        rejected_by_actor: dict[object, list[RejectedIntent]] = {}
        for rejected in rejected_intents:
            rejected_by_actor.setdefault(rejected.intent.actor_id, []).append(rejected)
        current = (*((context.core,) if context.core is not None else ()), *context.units)
        for actor in current:
            projection_started = perf_counter()
            intent = winners.get(actor.id)
            alias = entity_alias(actor.id)
            assert alias is not None
            persisted = memory.unit_tasks.get(str(actor.id), {})
            task_kind = str(persisted.get("kind", "legacy_action")).upper()
            target = intent.target_cell if intent is not None else None
            if target is None and isinstance(persisted.get("target"), list) and len(persisted["target"]) == 2:
                target = int(persisted["target"][0]), int(persisted["target"][1])
            rejected = rejected_by_actor.get(actor.id, [])
            reason = intent.reason if intent is not None else (
                "LIFECYCLE_BLOCKED" if context.core is None else "NO_LEGAL_USEFUL_INTENT"
            )
            status = "NO_INTENT" if intent is None else ("IDLE" if intent.action.value == "WAIT" else "SUCCESS")
            action = intent.action.value if intent is not None else "NO_INTENT"
            is_wait = action == "WAIT"
            wait_kind = None
            blocker = None
            wake_condition = None
            eta = None
            if is_wait:
                wait_kind, wake_condition, eta = classify_wait_reason(reason)
                blocker = reason if wait_kind in {"BLOCKED", "SAFETY"} else None
            candidate_intents = tuple(
                {"action": item.intent.action.value, "result": "REJECTED", "reason": item.rejection_reason}
                for item in rejected
            )
            if intent is not None:
                candidate_intents += ({"action": action, "result": "ACCEPTED", "reason": reason},)
            duration_ms = round(max(0.0, (perf_counter() - projection_started) * 1_000), 3)
            nodes = (NodeTrace("legacy.projection", status, reason, duration_ms),)
            if len(nodes) > limits.max_nodes_per_entity:
                dropped_nodes += len(nodes) - limits.max_nodes_per_entity
                nodes = nodes[: limits.max_nodes_per_entity]
            entities.append(
                EntityTrace(
                    actor_alias=alias,
                    entity_kind="CORE" if isinstance(actor, CoreView) else actor.unit_type.value,
                    current_task=f"LEGACY_{task_kind}",
                    goal="LEGACY_PLAN",
                    action=action,
                    reason_codes=(reason, *(item.rejection_reason for item in rejected)),
                    status=status,
                    duration_ms=duration_ms,
                    task_status="LEGACY",
                    assignment_status="IDLE",
                    current_cell=actor.position,
                    target_cell=target,
                    blocker=blocker,
                    waited_ticks=max(0, context.tick - int(persisted.get("since_tick", context.tick))),
                    eta_ticks=eta,
                    assignment={"kind": "LEGACY", "task": f"LEGACY_{task_kind}"},
                    next_step=action,
                    candidate_intents=candidate_intents,
                    winning_intent={"action": action, "reason": reason} if intent is not None else None,
                    wait_kind=wait_kind,
                    wake_condition=wake_condition,
                    node_path=nodes,
                )
            )

        detailed_entities = min(len(entities), limits.max_entities)
        dropped_nodes += sum(len(item.node_path) for item in entities[detailed_entities:])
        entities = [item if index < detailed_entities else replace(
            item, node_path=(), candidate_intents=(), winning_intent=None,
            assignment=None, summary_only=True,
        ) for index, item in enumerate(entities)]
        if sum(len(item.node_path) for item in entities) > limits.max_nodes:
            remaining = limits.max_nodes
            bounded: list[EntityTrace] = []
            for item in entities:
                kept = item.node_path[:remaining]
                dropped_nodes += len(item.node_path) - len(kept)
                bounded.append(replace(item, node_path=kept))
                remaining -= len(kept)
            entities = bounded

        trace = DecisionTrace(
            schema_version=TRACE_SCHEMA_VERSION,
            tick=context.tick,
            planner_version=self.planner_version,
            policy_version=0,
            entity_traces=tuple(entities),
            goal_summaries=({"goal": "LEGACY_PLAN", "status": "PROJECTED"},),
            arbitration=tuple(
                {"actor_alias": item.actor_alias, "winner": item.action}
                for item in entities if item.winning_intent is not None
            ),
            validation=tuple(
                {"actor_alias": entity_alias(item.intent.actor_id), "result": "REJECTED", "reason": item.rejection_reason}
                for item in rejected_intents
            ),
            timings={"decision_ms": round(decision_ms, 3)},
            truncation=TraceTruncation(
                truncated=bool(len(entities) > detailed_entities or dropped_nodes),
                dropped_entities=0,
                dropped_nodes=dropped_nodes,
            ),
        )
        encoded_size = _jsonl_size(trace)
        if encoded_size > limits.max_bytes:
            trace = replace(
                trace,
                entity_traces=tuple(replace(item, node_path=(), candidate_intents=()) for item in trace.entity_traces),
                arbitration=(), validation=(),
                truncation=replace(trace.truncation, truncated=True, dropped_nodes=trace.truncation.dropped_nodes + sum(len(item.node_path) for item in trace.entity_traces), byte_limit_reached=True),
            )
            encoded_size = _jsonl_size(trace)
        if encoded_size > limits.max_bytes:
            trace = replace(
                trace,
                entity_traces=tuple(replace(item, summary_only=True) for item in trace.entity_traces),
                goal_summaries=(), task_transitions=(), command_results=(), timings={},
                truncation=replace(trace.truncation, truncated=True, byte_limit_reached=True),
            )
            encoded_size = _jsonl_size(trace)
        if encoded_size > limits.max_bytes:
            trace = replace(
                trace,
                entity_traces=(), goal_summaries=(), task_transitions=(), arbitration=(),
                validation=(), command_results=(), timings={},
                truncation=replace(
                    trace.truncation, truncated=True, dropped_entities=len(entities),
                    byte_limit_reached=True,
                ),
            )
        return trace
