"""Redacted one-line decision summaries."""

from __future__ import annotations

import json
from collections import Counter
from hashlib import blake2s
from pathlib import Path
from typing import Any
from uuid import UUID

from arena_hero import CoreView, UnitView

from .context import DecisionContext
from .models import DecisionResult


def summary_line(
    context: DecisionContext,
    result: DecisionResult,
    submission: Any | None = None,
) -> str:
    accepted = getattr(submission, "accepted", None) if submission is not None else None
    payload = {
        "tick": context.tick,
        "mode": result.mode.value,
        "resources": context.resources,
        "population": context.population,
        "actions": dict(result.action_counts),
        "wait_reasons": list(result.wait_reasons),
        "decision_ms": round(result.decision_ms, 3),
        "timed_out": result.timed_out,
        "submitted": submission is not None,
        "accepted": accepted,
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _alias(value: UUID | None) -> str | None:
    return blake2s(value.bytes, digest_size=6).hexdigest() if value else None


def _object_record(value: CoreView | UnitView) -> dict[str, Any]:
    if isinstance(value, CoreView):
        return {
            "id": _alias(value.id),
            "kind": "CORE",
            "position": list(value.position),
            "hp": value.hp,
            "shield": value.shield,
            "state": value.state.value,
            "destination": list(value.destination) if value.destination else None,
        }
    return {
        "id": _alias(value.id),
        "kind": "UNIT",
        "unit_type": value.unit_type.value,
        "position": list(value.position),
        "hp": value.hp,
        "cargo": value.cargo,
    }


def _safe_event_values(values: dict[str, Any] | None) -> dict[str, Any]:
    allowed = {
        "amount",
        "available",
        "capacity",
        "cost",
        "damage",
        "destroyed",
        "hp",
        "progress",
        "remaining",
        "required",
        "resources",
        "shield",
        "unit_type",
        "workers",
    }
    return {
        key: value
        for key, value in (values or {}).items()
        if key in allowed and isinstance(value, (bool, int, float, str))
    }


def replay_record(
    context: DecisionContext,
    result: DecisionResult,
    submission: Any,
) -> dict[str, Any]:
    """Create a credential-free snapshot suitable for offline replay metrics."""
    return {
        "schema_version": 1,
        "tick": context.tick,
        "mode": result.mode.value,
        "state": {
            "resources": context.resources,
            "resource_capacity": context.resource_capacity,
            "population": context.population,
            "core": _object_record(context.core) if context.core else None,
            "units": [_object_record(unit) for unit in context.units],
            "visible_enemies": [_object_record(enemy) for enemy in context.enemies],
            "resource_cells": [list(cell) for cell in sorted(context.resource_cells)],
            "obstacle_cells": [list(cell) for cell in sorted(context.obstacle_cells)],
            "beacon": {
                "position": list(context.beacon.position),
                "status": context.beacon.status.value if context.beacon.status else None,
                "carrier": _alias(context.beacon.carrier_id),
            },
        },
        "events": [
            {
                "id": _alias(event.event_id),
                "type": event.event_type,
                "reason": event.reason_code,
                "actor": _alias(event.actor_id),
                "target": _alias(event.target_id),
                "position": list(event.position) if event.position else None,
                "values": _safe_event_values(event.values),
            }
            for event in context.events
        ],
        "intents": [
            {
                "actor": _alias(intent.actor_id),
                "is_core": intent.is_core,
                "action": intent.action.value,
                "target": _alias(intent.target_id),
                "target_cell": list(intent.target_cell) if intent.target_cell else None,
                "direction": intent.direction.value if intent.direction else None,
                "unit_type": intent.unit_type.value if intent.unit_type else None,
                "score": intent.score,
                "reason": intent.reason,
                "estimated_cost": intent.estimated_cost,
            }
            for intent in result.intents
        ],
        "rejected": [
            {
                "actor": _alias(item.intent.actor_id),
                "action": item.intent.action.value,
                "reason": item.rejection_reason,
            }
            for item in result.rejected_intents
        ],
        "decision_ms": round(result.decision_ms, 3),
        "timed_out": result.timed_out,
        "accepted": bool(getattr(submission, "accepted", False)),
    }


class ReplayWriter:
    def __init__(self, path: Path) -> None:
        self.path = path

    def append(
        self,
        context: DecisionContext,
        result: DecisionResult,
        submission: Any,
    ) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(
            replay_record(context, result, submission),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(line + "\n")
            stream.flush()


def replay_metrics(path: Path) -> dict[str, Any]:
    """Summarize valid JSONL records; tolerate a truncated final crash line."""
    records: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        lines = []
    for line in lines:
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    modes = Counter(record.get("mode") for record in records)
    actions = Counter(
        intent.get("action")
        for record in records
        for intent in record.get("intents", [])
    )
    durations = [float(record.get("decision_ms", 0)) for record in records]
    resources = [
        int(record.get("state", {}).get("resources", 0)) for record in records
    ]
    populations = [
        int(record.get("state", {}).get("population", 0)) for record in records
    ]
    missing_core_ticks = sum(
        record.get("state", {}).get("core") is None for record in records
    )
    damaged_core_ticks = sum(
        (core := record.get("state", {}).get("core")) is not None
        and int(core.get("hp", 0)) < 5
        for record in records
    )
    beacon_owned_ticks = 0
    for record in records:
        state = record.get("state", {})
        owned_ids = {
            item.get("id")
            for item in [state.get("core"), *state.get("units", [])]
            if item is not None
        }
        if state.get("beacon", {}).get("carrier") in owned_ids:
            beacon_owned_ticks += 1
    return {
        "ticks": len(records),
        "accepted_ticks": sum(bool(record.get("accepted")) for record in records),
        "timed_out_ticks": sum(bool(record.get("timed_out")) for record in records),
        "mode_counts": dict(modes),
        "action_counts": dict(actions),
        "average_decision_ms": sum(durations) / len(durations) if durations else 0.0,
        "max_decision_ms": max(durations, default=0.0),
        "resource_delta": resources[-1] - resources[0] if resources else 0,
        "max_population": max(populations, default=0),
        "missing_core_ticks": missing_core_ticks,
        "damaged_core_ticks": damaged_core_ticks,
        "beacon_owned_ticks": beacon_owned_ticks,
    }
