"""Phase-one command and audit schemas; execution belongs to later phases."""

from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any, Mapping

from .values import freeze_mapping, freeze_optional_text, freeze_text


class CommandType(StrEnum):
    ASSIGN_TASK = "ASSIGN_TASK"
    UPDATE_POLICY = "UPDATE_POLICY"
    CANCEL = "CANCEL"
    EMERGENCY_STOP = "EMERGENCY_STOP"
    RESUME_AUTO = "RESUME_AUTO"
    START_CORE_MIGRATION = "START_CORE_MIGRATION"
    CANCEL_CORE_MIGRATION = "CANCEL_CORE_MIGRATION"
    TRIGGER_ANALYSIS = "TRIGGER_ANALYSIS"


class CommandStatus(StrEnum):
    QUEUED = "QUEUED"
    APPLIED = "APPLIED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    SUPERSEDED = "SUPERSEDED"
    CANCELLED = "CANCELLED"


class OverrideStatus(StrEnum):
    ACTIVE = "ACTIVE"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"


@dataclass(frozen=True, slots=True)
class Command:
    command_id: str
    idempotency_key: str
    type: CommandType
    status: CommandStatus
    issuer: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    expected_version: int | None = None
    not_before_tick: int | None = None
    expires_at_tick: int | None = None
    issued_at: str | None = None
    apply_result: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("command_id", "idempotency_key", "issuer"):
            object.__setattr__(self, name, freeze_text(getattr(self, name), field_name=f"Command.{name}"))
        object.__setattr__(self, "issued_at", freeze_optional_text(self.issued_at, field_name="Command.issued_at"))
        object.__setattr__(self, "payload", freeze_mapping(self.payload, field_name="Command.payload"))
        object.__setattr__(self, "apply_result", freeze_mapping(self.apply_result, field_name="Command.apply_result"))

    def transition(self, status: CommandStatus, *, apply_result: Mapping[str, Any] | None = None) -> "Command":
        allowed = {
            CommandStatus.QUEUED: {CommandStatus.APPLIED, CommandStatus.REJECTED, CommandStatus.EXPIRED, CommandStatus.SUPERSEDED, CommandStatus.CANCELLED}
        }
        if status not in allowed.get(self.status, set()):
            raise ValueError(f"illegal Command transition {self.status.value} -> {status.value}")
        return replace(self, status=status, apply_result=self.apply_result if apply_result is None else apply_result)


@dataclass(frozen=True, slots=True)
class Override:
    override_id: str
    scope: str
    command_id: str
    priority: int
    mode: str
    created_tick: int
    ttl_ticks: int
    status: OverrideStatus
    task_spec: Mapping[str, Any] = field(default_factory=dict)
    policy_patch: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("override_id", "scope", "command_id", "mode"):
            object.__setattr__(self, name, freeze_text(getattr(self, name), field_name=f"Override.{name}"))
        object.__setattr__(self, "task_spec", freeze_mapping(self.task_spec, field_name="Override.task_spec"))
        object.__setattr__(self, "policy_patch", freeze_mapping(self.policy_patch, field_name="Override.policy_patch"))

    def transition(self, status: OverrideStatus) -> "Override":
        if self.status is not OverrideStatus.ACTIVE or status not in {OverrideStatus.EXPIRED, OverrideStatus.CANCELLED}:
            raise ValueError(f"illegal Override transition {self.status.value} -> {status.value}")
        return replace(self, status=status)


@dataclass(frozen=True, slots=True)
class AuditEvent:
    event_id: str
    wall_time: str
    tick: int
    actor: str
    operation: str
    subject_alias: str
    outcome: str
    before_version: int | None = None
    after_version: int | None = None
    reason: str | None = None
    request_hash: str | None = None

    def __post_init__(self) -> None:
        for name in ("event_id", "wall_time", "actor", "operation", "subject_alias", "outcome"):
            object.__setattr__(self, name, freeze_text(getattr(self, name), field_name=f"AuditEvent.{name}"))
        for name in ("reason", "request_hash"):
            object.__setattr__(self, name, freeze_optional_text(getattr(self, name), field_name=f"AuditEvent.{name}"))
