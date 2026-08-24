"""脱敏的终端决策日志和离线回放记录。"""

from __future__ import annotations

import json
import os
import threading
from collections import Counter
from pathlib import Path
from typing import Any
from uuid import UUID

from arena_hero import CoreView, UnitView

from .context import DecisionContext
from .domain.trace import DecisionTrace, trace_record
from .identity import entity_alias
from .models import DecisionResult


_MODE_LABELS = {
    "RESPAWN": "等待重生",
    "RECOVER": "恢复",
    "DEFEND": "防守",
    "ECONOMY": "发展经济",
    "EXPLORE": "探索",
    "BEACON": "争夺信标",
    "ATTACK": "进攻",
}
_KIND_LABELS = {"CORE": "核心", "WORKER": "工人", "VANGUARD": "先锋", "RANGER": "游侠"}
_ACTION_LABELS = {
    "WAIT": "等待",
    "MOVE": "移动",
    "HARVEST": "采集资源",
    "DEPOSIT": "存入资源",
    "SWEEP": "横扫攻击",
    "SHOOT": "远程射击",
    "HEAL": "治疗",
    "SPAWN": "生产单位",
    "REPAIR_SHIELD": "修复护盾",
    "START_MOVE": "开始迁移",
    "PICKUP_BEACON": "拾取信标",
    "SELF_DESTRUCT": "自毁重掷",
}
_DIRECTION_LABELS = {"UP": "向上", "DOWN": "向下", "LEFT": "向左", "RIGHT": "向右"}
_REASON_LABELS = {
    "move_to_unique_resource": "前往可见资源",
    "continue_locked_resource_route": "锁定延续前往资源",
    "resources_reserved_or_no_legal_core_action": "资源需要保留，或当前没有合适的核心动作",
    "reobserve_remembered_resource": "复查记忆中的资源位置",
    "no_visible_resource": "当前没有可见资源",
    "barren_spawn_fast_reroll": "出生点贫瘠，快速重掷",
    "unit_retreat_to_core_heal": "撤退治疗",
}


def summary_line(
    context: DecisionContext,
    result: DecisionResult,
    submission: Any | None = None,
) -> str:
    actor_kinds = {
        unit.id: unit.unit_type.value
        for unit in context.units
    }
    if context.core is not None:
        actor_kinds[context.core.id] = "CORE"
    accepted = getattr(submission, "accepted", None) if submission is not None else None
    submission_text = "提交成功" if accepted else "未提交或提交失败"
    timeout_text = "，已触及决策时限" if result.timed_out else ""
    lines = [
        (
            f"[第 {context.tick} 回合] {submission_text}｜策略："
            f"{_MODE_LABELS.get(result.mode.value, result.mode.value)}｜"
            f"资源：{context.resources}/{context.resource_capacity}｜"
            f"人口：{context.population}｜决策耗时：{result.decision_ms:.1f}ms{timeout_text}"
        ),
        "  本回合决策：",
    ]
    for intent in result.intents:
        kind = _KIND_LABELS.get(actor_kinds.get(intent.actor_id, "UNKNOWN"), "未知对象")
        action = _ACTION_LABELS.get(intent.action.value, intent.action.value)
        detail: list[str] = []
        if intent.direction:
            detail.append(_DIRECTION_LABELS.get(intent.direction.value, intent.direction.value))
        if intent.target_cell:
            detail.append(f"目标坐标 {intent.target_cell}")
        if intent.target_id:
            detail.append(f"目标 #{_alias(intent.target_id)}")
        if intent.unit_type:
            detail.append(f"生产 {_KIND_LABELS.get(intent.unit_type.value, intent.unit_type.value)}")
        reason = _REASON_LABELS.get(intent.reason, intent.reason)
        suffix = f"（{reason}）" if reason else ""
        details = f"，{'；'.join(detail)}" if detail else ""
        lines.append(f"  - {kind} #{_alias(intent.actor_id)}：{action}{details}{suffix}")
    if result.rejected_intents:
        lines.append("  已放弃的提案：")
        for item in result.rejected_intents:
            lines.append(
                f"  - {_ACTION_LABELS.get(item.intent.action.value, item.intent.action.value)}："
                f"{item.rejection_reason}"
            )
    if context.events:
        lines.append("  上回合结算：")
        for event in context.events:
            position = f"，坐标 {event.position}" if event.position else ""
            reason = f"，原因：{event.reason_code}" if event.reason_code else ""
            lines.append(f"  - {event.event_type}{reason}{position}")
    return "\n".join(lines)


def _alias(value: UUID | None) -> str | None:
    alias = entity_alias(value)
    return alias.removeprefix("entity_") if alias else None


def decision_trace_record(trace: DecisionTrace) -> dict[str, Any]:
    """Serialize only the explicit, credential-free trace allowlist."""
    return trace_record(trace)


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
    """Synchronous v1 replay append with bounded, same-directory rotation."""

    def __init__(
        self,
        path: Path,
        max_file_bytes: int = 64 * 1024 * 1024,
        history_files: int = 3,
    ) -> None:
        if max_file_bytes <= 0:
            raise ValueError("max_file_bytes must be positive")
        if history_files < 0:
            raise ValueError("history_files must be nonnegative")
        self.path = path
        self.max_file_bytes = max_file_bytes
        self.history_files = history_files
        self._lock = threading.Lock()

    def append(
        self,
        context: DecisionContext,
        result: DecisionResult,
        submission: Any,
    ) -> None:
        line = json.dumps(
            replay_record(context, result, submission),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        encoded = (line + "\n").encode("utf-8")
        if len(encoded) > self.max_file_bytes:
            # Preserve v1 JSONL rather than writing a partial record. Normal
            # records are far below the production 64 MiB limit.
            return
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            size = self.path.stat().st_size if self.path.exists() else 0
            if size and size + len(encoded) > self.max_file_bytes:
                self._rotate_files()
            with self.path.open("ab") as stream:
                stream.write(encoded)
                stream.flush()

    def _rotate_files(self) -> None:
        if self.history_files == 0:
            self.path.unlink(missing_ok=True)
            return
        oldest = self.path.with_name(f"{self.path.name}.{self.history_files}")
        oldest.unlink(missing_ok=True)
        for index in range(self.history_files - 1, 0, -1):
            source = self.path.with_name(f"{self.path.name}.{index}")
            if source.exists():
                os.replace(source, self.path.with_name(f"{self.path.name}.{index + 1}"))
        if self.path.exists():
            os.replace(self.path, self.path.with_name(f"{self.path.name}.1"))


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
