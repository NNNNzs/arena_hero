"""Safe, deferred command queue for the local command center.

HTTP handlers only authenticate and append immutable commands.  The tactic
thread evaluates a snapshot at the next authoritative Tick and commits its
outcome only after that Tick's SDK submission was accepted.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Mapping

from .domain import AuditEvent, Command, CommandStatus, CommandType

if TYPE_CHECKING:
    from .domain import BoundedAuditSink


_IDEMPOTENCY = re.compile(r"^[\x21-\x7e]{8,128}$")
_ALIAS = re.compile(r"^entity_[0-9a-f]{12}$")
_TASK_KIND = re.compile(r"^[A-Z][A-Z_]{1,47}$")
_POSTURES = frozenset({"BALANCED", "DEFENSIVE", "ECONOMY", "AGGRESSIVE"})
_MANUAL_TASKS = frozenset({"RETREAT_TO_CORE", "HOLD_POSITION", "HARVEST_VISIBLE", "MOVE_TO_CELL"})
# 策略热更新白名单：允许通过 UPDATE_POLICY 覆盖的 AgentConfig 数值字段
# 格式: {字段名: (最小值, 最大值)}
_POLICY_NUMERIC_FIELDS: dict[str, tuple[float, float]] = {
    "core_guard_vanguards": (0, 8),
    "core_guard_rangers": (0, 8),
    "early_workers": (0, 12),
    "early_vanguards": (0, 8),
    "early_rangers": (0, 8),
    "patrol_radius_min": (1, 20),
    "patrol_radius_max": (2, 30),
    "patrol_rotation_ticks": (1, 30),
    "minimum_resource_reserve": (0, 200),
    "peacetime_resource_buffer": (0, 500),
    "unit_retreat_heal_ratio": (0, 1),
}


class CommandError(ValueError):
    """A client-safe command error with a stable machine code."""

    def __init__(self, code: str, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


@dataclass(frozen=True, slots=True)
class PreparedCommands:
    tick: int
    commands: tuple[Command, ...]
    transitions: tuple[tuple[str, CommandStatus, Mapping[str, Any]], ...]
    emergency_halt: bool

    def trace_results(self, *, outcome: str) -> tuple[dict[str, Any], ...]:
        return tuple({"command_id": command.command_id, "type": command.type.value,
                      "status": outcome, "tick": self.tick} for command in self.commands)


class CommandQueue:
    """Thread-safe queue with version, idempotency, TTL and deferred results."""

    def __init__(self, *, audit_sink: BoundedAuditSink | None = None) -> None:
        self._lock = threading.RLock()
        self._version = 0
        self._commands: dict[str, Command] = {}
        self._idempotency: dict[str, tuple[str, str]] = {}
        self._emergency_halt = False
        self._policy = {"version": 0, "posture": "BALANCED", "effective_tick": 0}
        self._audit: list[AuditEvent] = []
        self._audit_sink = audit_sink

    @property
    def version(self) -> int:
        with self._lock:
            return self._version

    def snapshot(self) -> tuple[Command, ...]:
        with self._lock:
            return tuple(self._commands.values())

    def audit_snapshot(self) -> tuple[AuditEvent, ...]:
        with self._lock:
            return tuple(self._audit)

    def policy_snapshot(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._policy)

    def restore_policy(self, policy: Mapping[str, Any]) -> None:
        """Seed an empty post-restart read model from accepted runtime memory only."""
        version, posture, effective_tick = policy.get("version"), policy.get("posture"), policy.get("effective_tick")
        if type(version) is not int or version < 0 or posture not in _POSTURES or type(effective_tick) is not int or effective_tick < 0:
            return
        with self._lock:
            if self._version == 0 and not self._commands:
                restored: dict[str, Any] = {"version": version, "posture": posture, "effective_tick": effective_tick}
                # 恢复数值字段覆盖
                for field_name in _POLICY_NUMERIC_FIELDS:
                    if field_name in policy:
                        restored[field_name] = policy[field_name]
                self._policy = restored

    def enqueue(
        self, body: Mapping[str, Any], *, issuer: str, current_tick: int | None,
        idempotency_key: str, expected_version: int,
    ) -> tuple[Command, bool, int]:
        """Validate and append; identical idempotent retries return the original."""
        if not _IDEMPOTENCY.fullmatch(idempotency_key):
            raise CommandError("INVALID_IDEMPOTENCY_KEY", "Idempotency-Key must be 8-128 visible ASCII characters")
        canonical = _canonical_body(body)
        request_hash = hashlib.sha256(canonical).hexdigest()
        with self._lock:
            existing = self._idempotency.get(idempotency_key)
            if existing is not None:
                command_id, prior_hash = existing
                if prior_hash != request_hash:
                    raise CommandError("IDEMPOTENCY_CONFLICT", "idempotency key was used with a different request", status=409)
                return self._commands[command_id], True, self._version
            if expected_version != self._version:
                raise CommandError("VERSION_CONFLICT", "command version does not match", status=409)
            command_type, payload, not_before, expires = _validate_body(body, current_tick)
            self._version += 1
            command_id = f"cmd_{self._version:08x}_{secrets.token_hex(4)}"
            command = Command(
                command_id=command_id, idempotency_key=idempotency_key, type=command_type,
                status=CommandStatus.QUEUED, issuer=issuer, payload=payload,
                expected_version=expected_version, not_before_tick=not_before,
                expires_at_tick=expires, issued_at=datetime.now(UTC).isoformat(),
            )
            self._commands[command_id] = command
            self._idempotency[idempotency_key] = (command_id, request_hash)
            self._audit_event(current_tick or 0, issuer, "COMMAND_QUEUED", command_id, "QUEUED", self._version - 1, self._version, request_hash)
            return command, False, self._version

    def cancel(self, command_id: str, *, issuer: str, current_tick: int | None, expected_version: int) -> Command:
        with self._lock:
            if expected_version != self._version:
                raise CommandError("VERSION_CONFLICT", "command version does not match", status=409)
            command = self._commands.get(command_id)
            if command is None:
                raise CommandError("NOT_FOUND", "command does not exist", status=404)
            if command.status is not CommandStatus.QUEUED:
                raise CommandError("NOT_CANCELLABLE", "only queued commands can be cancelled", status=409)
            before = self._version
            self._version += 1
            changed = command.transition(CommandStatus.CANCELLED, apply_result={"reason": "cancelled_by_operator"})
            self._commands[command_id] = changed
            self._audit_event(current_tick or 0, issuer, "COMMAND_CANCELLED", command_id, "CANCELLED", before, self._version, None)
            return changed

    def prepare_for_tick(self, tick: int, current_aliases: set[str]) -> PreparedCommands:
        """Read without changing command outcomes; safe to repeat after a rejected submit."""
        with self._lock:
            pending = tuple(command for command in self._commands.values() if command.status is CommandStatus.QUEUED)
            halt = self._emergency_halt
        commands: list[Command] = []
        transitions: list[tuple[str, CommandStatus, Mapping[str, Any]]] = []
        for command in pending:
            if command.expires_at_tick is not None and tick > command.expires_at_tick:
                commands.append(command)
                transitions.append((command.command_id, CommandStatus.EXPIRED, {"reason": "ttl_expired"}))
                continue
            if command.not_before_tick is not None and tick < command.not_before_tick:
                continue
            if command.type is CommandType.ASSIGN_TASK:
                alias = command.payload.get("entity_alias")
                if alias not in current_aliases:
                    commands.append(command)
                    transitions.append((command.command_id, CommandStatus.REJECTED, {"reason": "ENTITY_NOT_CURRENT"}))
                    continue
            if command.type is CommandType.EMERGENCY_STOP:
                halt = True
            elif command.type is CommandType.RESUME_AUTO:
                halt = False
            commands.append(command)
            transitions.append((command.command_id, CommandStatus.APPLIED, {"applied_at_tick": tick}))
        return PreparedCommands(tick, tuple(commands), tuple(transitions), halt)

    def finalize(self, prepared: PreparedCommands, *, accepted: bool) -> tuple[Command, ...]:
        """Make staged outcomes durable only after the SDK accepted the plan."""
        if not accepted:
            return ()
        changed: list[Command] = []
        with self._lock:
            for command_id, status, result in prepared.transitions:
                command = self._commands.get(command_id)
                if command is None or command.status is not CommandStatus.QUEUED:
                    continue
                updated = command.transition(status, apply_result=result)
                self._commands[command_id] = updated
                changed.append(updated)
                if updated.type is CommandType.EMERGENCY_STOP and status is CommandStatus.APPLIED:
                    self._emergency_halt = True
                elif updated.type is CommandType.RESUME_AUTO and status is CommandStatus.APPLIED:
                    self._emergency_halt = False
                elif updated.type is CommandType.UPDATE_POLICY and status is CommandStatus.APPLIED:
                    # 策略更新：保留 posture + 所有白名单数值字段覆盖
                    policy_update: dict[str, Any] = {
                        "version": (updated.expected_version or 0) + 1,
                        "posture": updated.payload["posture"],
                        "effective_tick": prepared.tick,
                    }
                    # 将数值字段覆盖合并到策略快照
                    for field_name in _POLICY_NUMERIC_FIELDS:
                        if field_name in updated.payload:
                            policy_update[field_name] = updated.payload[field_name]
                    self._policy = policy_update
                self._audit_event(prepared.tick, updated.issuer, "COMMAND_APPLIED", updated.command_id,
                                  updated.status.value, self._version, self._version, None)
        return tuple(changed)

    def _audit_event(self, tick: int, actor: str, operation: str, subject: str, outcome: str,
                     before: int | None, after: int | None, request_hash: str | None) -> None:
        event = AuditEvent(
            event_id=f"audit_{secrets.token_hex(8)}", wall_time=datetime.now(UTC).isoformat(), tick=tick,
            actor=actor, operation=operation, subject_alias=subject, outcome=outcome,
            before_version=before, after_version=after, request_hash=request_hash,
        )
        self._audit.append(event)
        del self._audit[:-256]
        if self._audit_sink is not None:
            self._audit_sink.emit(event)


def _canonical_body(body: Mapping[str, Any]) -> bytes:
    try:
        raw = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    except (TypeError, ValueError) as exc:
        raise CommandError("INVALID_JSON", "request must contain JSON values only") from exc
    if len(raw) > 16 * 1024:
        raise CommandError("REQUEST_TOO_LARGE", "command request exceeds 16 KiB", status=413)
    return raw


def _integer(value: Any, name: str, *, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise CommandError("INVALID_FIELD", f"{name} must be an integer from {minimum} to {maximum}")
    return value


def _validate_body(body: Mapping[str, Any], current_tick: int | None) -> tuple[CommandType, dict[str, Any], int | None, int | None]:
    if not isinstance(body, Mapping):
        raise CommandError("INVALID_BODY", "request body must be an object")
    try:
        command_type = CommandType(body.get("type"))
    except ValueError as exc:
        raise CommandError("INVALID_COMMAND_TYPE", "unknown command type") from exc
    raw_payload = body.get("payload", {})
    if not isinstance(raw_payload, Mapping):
        raise CommandError("INVALID_PAYLOAD", "payload must be an object")
    not_before = body.get("not_before_tick")
    if not_before is not None:
        not_before = _integer(not_before, "not_before_tick", minimum=0, maximum=2_147_483_647)
    ttl = body.get("ttl_ticks", 12)
    ttl = _integer(ttl, "ttl_ticks", minimum=1, maximum=120)
    effective_tick = not_before if not_before is not None else ((current_tick + 1) if current_tick is not None else None)
    expires = effective_tick + ttl - 1 if effective_tick is not None else None
    payload: dict[str, Any]
    if command_type is CommandType.ASSIGN_TASK:
        alias, kind = raw_payload.get("entity_alias"), raw_payload.get("task_kind")
        if not isinstance(alias, str) or not _ALIAS.fullmatch(alias):
            raise CommandError("INVALID_ALIAS", "entity_alias must be a redacted current entity alias")
        if not isinstance(kind, str) or not _TASK_KIND.fullmatch(kind) or kind not in _MANUAL_TASKS:
            raise CommandError("INVALID_TASK", "task_kind is not supported by the current manual executor")
        priority = _integer(raw_payload.get("priority", 800), "payload.priority", minimum=0, maximum=1000)
        payload = {"entity_alias": alias, "task_kind": kind, "priority": priority}
        target = raw_payload.get("target")
        if target is not None:
            if not isinstance(target, (list, tuple)) or len(target) != 2 or not all(type(axis) is int for axis in target):
                raise CommandError("INVALID_TARGET", "target must be a two-integer cell")
            payload["target"] = [target[0], target[1]]
        if kind == "MOVE_TO_CELL" and "target" not in payload:
            raise CommandError("INVALID_TARGET", "MOVE_TO_CELL requires a target cell")
    elif command_type is CommandType.CANCEL:
        alias = raw_payload.get("entity_alias")
        if not isinstance(alias, str) or not _ALIAS.fullmatch(alias):
            raise CommandError("INVALID_ALIAS", "entity_alias must be a redacted entity alias")
        payload = {"entity_alias": alias}
    elif command_type is CommandType.START_CORE_MIGRATION:
        target = raw_payload.get("target")
        if not isinstance(target, (list, tuple)) or len(target) != 2 or not all(type(axis) is int for axis in target):
            raise CommandError("INVALID_TARGET", "core migration requires a two-integer target cell")
        payload = {"target": [target[0], target[1]]}
    elif command_type is CommandType.CANCEL_CORE_MIGRATION:
        if raw_payload:
            raise CommandError("INVALID_PAYLOAD", "this command does not accept payload fields")
        payload = {}
    elif command_type is CommandType.UPDATE_POLICY:
        # 支持 posture 字段 + 白名单内数值字段覆盖
        unknown_fields = set(raw_payload) - {"posture", *_POLICY_NUMERIC_FIELDS}
        if unknown_fields:
            raise CommandError("INVALID_POLICY", "policy contains a field outside the update whitelist")
        posture = raw_payload.get("posture")
        if posture not in _POSTURES:
            raise CommandError("INVALID_POLICY", "posture is not allowed")
        payload: dict[str, Any] = {"posture": posture}
        # 遍历白名单数值字段，逐个校验并加入 payload
        for field_name, (minimum, maximum) in _POLICY_NUMERIC_FIELDS.items():
            if field_name in raw_payload:
                value = raw_payload[field_name]
                if (
                    not isinstance(value, (int, float))
                    or isinstance(value, bool)
                    or not minimum <= value <= maximum
                ):
                    raise CommandError(
                        "INVALID_POLICY",
                        f"{field_name} must be a number from {minimum} to {maximum}",
                    )
                payload[field_name] = value
    elif command_type is CommandType.TRIGGER_ANALYSIS:
        # 手动触发分析扫描：payload 可指定 task_name，默认 resource_density_scan
        task_name = raw_payload.get("task_name", "resource_density_scan")
        if not isinstance(task_name, str) or not re.fullmatch(r"[a-z_]{1,64}", task_name):
            raise CommandError("INVALID_PAYLOAD", "task_name must be lowercase snake_case")
        payload = {"task_name": task_name}
    else:
        if raw_payload:
            raise CommandError("INVALID_PAYLOAD", "this command does not accept payload fields")
        payload = {}
    return command_type, payload, effective_tick, expires
