"""Small authenticated HTTP facade for :mod:`arena_tactic.command_center`."""

from __future__ import annotations

import hmac
import json
import os
import secrets
import threading
import time
from dataclasses import dataclass
from http import HTTPStatus
from typing import Any, Callable, Mapping
from urllib.parse import parse_qs, urlsplit

from .command_center import CommandError, CommandQueue
from .domain import Command


@dataclass(frozen=True, slots=True)
class ApiResponse:
    status: HTTPStatus
    body: bytes
    headers: tuple[tuple[str, str], ...] = ()


class CommandApi:
    """Command API with local defaults; it never reads a Turn or controller."""

    def __init__(
        self, queue: CommandQueue, *, status_snapshot: Callable[[], Mapping[str, object]],
        admin_password: str | None = None, writes_enabled: bool = False,
        session_seconds: int = 30 * 60, secure_cookie: bool = False,
        allow_lan: bool = False, allowed_origins: frozenset[str] = frozenset(),
    ) -> None:
        self.queue = queue
        self.status_snapshot = status_snapshot
        self.admin_password = admin_password
        self.writes_enabled = writes_enabled
        self.session_seconds = session_seconds
        self.secure_cookie = secure_cookie
        self.allow_lan = allow_lan
        self.allowed_origins = allowed_origins
        self._sessions: dict[str, tuple[float, str]] = {}
        self._rates: dict[tuple[str, str], tuple[float, int]] = {}
        self._lock = threading.Lock()

    @classmethod
    def from_environment(cls, queue: CommandQueue, status_snapshot: Callable[[], Mapping[str, object]]) -> "CommandApi":
        return cls(
            queue, status_snapshot=status_snapshot,
            admin_password=os.environ.get("ARENA_HERO_COMMAND_PASSWORD"),
            writes_enabled=os.environ.get("ARENA_HERO_COMMAND_WRITE") == "1",
            secure_cookie=os.environ.get("ARENA_HERO_COMMAND_SECURE_COOKIE") == "1",
            allow_lan=os.environ.get("ARENA_HERO_COMMAND_LAN") == "1",
            allowed_origins=frozenset(item.strip() for item in os.environ.get("ARENA_HERO_COMMAND_ALLOWED_ORIGINS", "").split(",") if item.strip()),
        )

    def handle(self, method: str, raw_path: str, headers: Mapping[str, str], body: bytes,
               *, remote_host: str = "127.0.0.1") -> ApiResponse | None:
        parsed = urlsplit(raw_path)
        path = parsed.path
        if not path.startswith("/api/v1/"):
            return None
        if method == "POST" and path == "/api/v1/session":
            return self._login(headers, body, remote_host)
        session = self._session(headers)
        if session is None:
            return self._error(HTTPStatus.UNAUTHORIZED, "UNAUTHORIZED", "authentication is required")
        if method == "GET":
            return self._read(path, parsed.query)
        if method not in {"POST", "PATCH", "DELETE"}:
            return self._error(HTTPStatus.METHOD_NOT_ALLOWED, "METHOD_NOT_ALLOWED", "method is not supported")
        if not self._rate_allowed("write", remote_host, limit=60):
            return self._error(HTTPStatus.TOO_MANY_REQUESTS, "RATE_LIMITED", "too many command requests")
        if not self.writes_enabled:
            return self._error(HTTPStatus.FORBIDDEN, "WRITE_DISABLED", "command writes are disabled")
        if not self._origin_allowed(headers, remote_host) or not self._csrf_valid(headers, session):
            return self._error(HTTPStatus.FORBIDDEN, "CSRF_ORIGIN_REJECTED", "origin or csrf token rejected")
        current_tick = _current_tick(self.status_snapshot())
        try:
            if method == "DELETE" and path.startswith("/api/v1/commands/"):
                command = self.queue.cancel(path.rsplit("/", 1)[-1], issuer="operator", current_tick=current_tick,
                                            expected_version=_expected_version(headers))
                return self._json(HTTPStatus.OK, _command_record(command, self.queue.version))
            if method == "DELETE" and path == "/api/v1/core/migrations":
                key = _header(headers, "Idempotency-Key")
                if key is None:
                    raise CommandError("IDEMPOTENCY_REQUIRED", "Idempotency-Key is required")
                command, replayed, version = self.queue.enqueue(
                    {"type": "CANCEL_CORE_MIGRATION", "payload": {}}, issuer="operator", current_tick=current_tick,
                    idempotency_key=key, expected_version=_expected_version(headers),
                )
                return self._json(HTTPStatus.OK if replayed else HTTPStatus.ACCEPTED,
                                  {**_command_record(command, version), "replayed": replayed})
            expected = _expected_version(headers)
            request = _json_object(body)
            if method == "POST" and path == "/api/v1/commands":
                key = _header(headers, "Idempotency-Key")
                if key is None:
                    raise CommandError("IDEMPOTENCY_REQUIRED", "Idempotency-Key is required")
                command, replayed, version = self.queue.enqueue(
                    request, issuer="operator", current_tick=current_tick,
                    idempotency_key=key, expected_version=expected,
                )
                code = HTTPStatus.OK if replayed else HTTPStatus.ACCEPTED
                return self._json(code, {**_command_record(command, version), "replayed": replayed})
            parts = path.split("/")
            if method == "POST" and len(parts) == 6 and parts[:4] == ["", "api", "v1", "entities"] and parts[5] == "tasks":
                key = _header(headers, "Idempotency-Key")
                if key is None:
                    raise CommandError("IDEMPOTENCY_REQUIRED", "Idempotency-Key is required")
                wrapped = {"type": "ASSIGN_TASK", "payload": {"entity_alias": parts[4], **request}}
                command, replayed, version = self.queue.enqueue(
                    wrapped, issuer="operator", current_tick=current_tick,
                    idempotency_key=key, expected_version=expected,
                )
                return self._json(HTTPStatus.OK if replayed else HTTPStatus.ACCEPTED,
                                  {**_command_record(command, version), "replayed": replayed})
            if method == "POST" and len(parts) == 6 and parts[:4] == ["", "api", "v1", "entities"] and parts[5] in {"pause", "resume", "cancel"}:
                key = _header(headers, "Idempotency-Key")
                if key is None:
                    raise CommandError("IDEMPOTENCY_REQUIRED", "Idempotency-Key is required")
                if parts[5] == "pause":
                    wrapped = {"type": "ASSIGN_TASK", "payload": {"entity_alias": parts[4], "task_kind": "HOLD_POSITION",
                                                                        "priority": 800}, **request}
                else:
                    wrapped = {"type": "CANCEL", "payload": {"entity_alias": parts[4]}}
                command, replayed, version = self.queue.enqueue(
                    wrapped, issuer="operator", current_tick=current_tick,
                    idempotency_key=key, expected_version=expected,
                )
                return self._json(HTTPStatus.OK if replayed else HTTPStatus.ACCEPTED,
                                  {**_command_record(command, version), "replayed": replayed})
            if method == "PATCH" and path == "/api/v1/policy":
                key = _header(headers, "Idempotency-Key")
                if key is None:
                    raise CommandError("IDEMPOTENCY_REQUIRED", "Idempotency-Key is required")
                command, replayed, version = self.queue.enqueue(
                    {"type": "UPDATE_POLICY", "payload": request}, issuer="operator", current_tick=current_tick,
                    idempotency_key=key, expected_version=expected,
                )
                return self._json(HTTPStatus.OK if replayed else HTTPStatus.ACCEPTED,
                                  {**_command_record(command, version), "replayed": replayed})
            if method == "POST" and path == "/api/v1/core/migrations":
                key = _header(headers, "Idempotency-Key")
                if key is None:
                    raise CommandError("IDEMPOTENCY_REQUIRED", "Idempotency-Key is required")
                command, replayed, version = self.queue.enqueue(
                    {"type": "START_CORE_MIGRATION", "payload": request}, issuer="operator", current_tick=current_tick,
                    idempotency_key=key, expected_version=expected,
                )
                return self._json(HTTPStatus.OK if replayed else HTTPStatus.ACCEPTED,
                                  {**_command_record(command, version), "replayed": replayed})
            convenience = {
                "/api/v1/control/emergency-stop": "EMERGENCY_STOP",
                "/api/v1/control/resume-auto": "RESUME_AUTO",
            }.get(path)
            if method == "POST" and convenience is not None:
                key = _header(headers, "Idempotency-Key")
                if key is None:
                    raise CommandError("IDEMPOTENCY_REQUIRED", "Idempotency-Key is required")
                wrapped = {"type": convenience, "payload": {}, **request}
                command, replayed, version = self.queue.enqueue(
                    wrapped, issuer="operator", current_tick=current_tick,
                    idempotency_key=key, expected_version=expected,
                )
                return self._json(HTTPStatus.OK if replayed else HTTPStatus.ACCEPTED,
                                  {**_command_record(command, version), "replayed": replayed})
            return self._error(HTTPStatus.NOT_FOUND, "NOT_FOUND", "route does not exist")
        except CommandError as exc:
            payload = {"error": exc.code, "message": str(exc), "command_version": self.queue.version}
            return self._json(HTTPStatus(exc.status), payload)

    def _login(self, headers: Mapping[str, str], body: bytes, remote_host: str) -> ApiResponse:
        if not self._rate_allowed("login", remote_host, limit=10):
            return self._error(HTTPStatus.TOO_MANY_REQUESTS, "RATE_LIMITED", "too many login attempts")
        if not self._origin_allowed(headers, remote_host):
            return self._error(HTTPStatus.FORBIDDEN, "ORIGIN_REJECTED", "login is limited to the local command center")
        try:
            password = _json_object(body).get("password")
        except CommandError:
            password = None
        if not isinstance(password, str) or not self.admin_password or not hmac.compare_digest(password, self.admin_password):
            return self._error(HTTPStatus.UNAUTHORIZED, "UNAUTHORIZED", "invalid credentials")
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
        with self._lock:
            self._sessions[token] = (time.monotonic() + self.session_seconds, csrf)
            self._prune_sessions()
        cookie = f"arena_hero_session={token}; Path=/; HttpOnly; SameSite=Strict" + ("; Secure" if self.secure_cookie else "")
        return self._json(HTTPStatus.OK, {"csrf_token": csrf, "expires_in_seconds": self.session_seconds,
                                          "command_version": self.queue.version}, (("Set-Cookie", cookie),))

    def _read(self, path: str, query: str) -> ApiResponse:
        if path == "/api/v1/snapshot":
            return self._json(HTTPStatus.OK, {"command_version": self.queue.version, "service": dict(self.status_snapshot())})
        if path == "/api/v1/commands":
            return self._json(HTTPStatus.OK, {"command_version": self.queue.version,
                                               "commands": [_command_record(item, self.queue.version) for item in self.queue.snapshot()]})
        if path.startswith("/api/v1/commands/"):
            command_id = path.rsplit("/", 1)[-1]
            command = next((item for item in self.queue.snapshot() if item.command_id == command_id), None)
            return self._json(HTTPStatus.OK, _command_record(command, self.queue.version)) if command else self._error(HTTPStatus.NOT_FOUND, "NOT_FOUND", "command does not exist")
        if path == "/api/v1/audit":
            cursor = parse_qs(query).get("cursor", ["0"])[0]
            try:
                start = max(0, int(cursor))
            except ValueError:
                return self._error(HTTPStatus.BAD_REQUEST, "INVALID_CURSOR", "cursor must be an integer")
            audit = self.queue.audit_snapshot()
            records = [{"event_id": item.event_id, "tick": item.tick, "operation": item.operation,
                        "subject_alias": item.subject_alias, "outcome": item.outcome,
                        "before_version": item.before_version, "after_version": item.after_version}
                       for item in audit[start:]]
            return self._json(HTTPStatus.OK, {"events": records, "next_cursor": len(audit)})
        if path == "/api/v1/policy":
            return self._json(HTTPStatus.OK, self.queue.policy_snapshot())
        if path == "/api/v1/tasks":
            tasks = [item for item in self.queue.snapshot() if item.type.value == "ASSIGN_TASK"]
            return self._json(HTTPStatus.OK, {"command_version": self.queue.version,
                                               "tasks": [_command_record(item, self.queue.version) for item in tasks]})
        if path.startswith("/api/v1/entities/"):
            alias = path.rsplit("/", 1)[-1]
            tasks = [item for item in self.queue.snapshot() if item.payload.get("entity_alias") == alias]
            return self._json(HTTPStatus.OK, {"entity_alias": alias, "commands": [_command_record(item, self.queue.version) for item in tasks]})
        return self._error(HTTPStatus.NOT_FOUND, "NOT_FOUND", "route does not exist")

    def _session(self, headers: Mapping[str, str]) -> str | None:
        raw_cookie = _header(headers, "Cookie") or ""
        token = next((part.split("=", 1)[1] for part in raw_cookie.split(";")
                      if part.strip().startswith("arena_hero_session=") and "=" in part), None)
        if token is None:
            return None
        with self._lock:
            value = self._sessions.get(token)
            if value is None or value[0] < time.monotonic():
                self._sessions.pop(token, None)
                return None
            return value[1]

    def _origin_allowed(self, headers: Mapping[str, str], remote_host: str) -> bool:
        origin = _header(headers, "Origin")
        host = _header(headers, "Host")
        if _loopback(remote_host):
            return origin is None or (host is not None and origin in {f"http://{host}", f"https://{host}"})
        if not self.allow_lan or not self.allowed_origins or origin not in self.allowed_origins:
            return False
        return host is not None and any(origin.endswith(f"://{host}") for origin in self.allowed_origins)

    @staticmethod
    def _csrf_valid(headers: Mapping[str, str], expected: str) -> bool:
        supplied = _header(headers, "X-CSRF-Token")
        return isinstance(supplied, str) and hmac.compare_digest(supplied, expected)

    @staticmethod
    def _json(status: HTTPStatus, payload: Mapping[str, Any], headers: tuple[tuple[str, str], ...] = ()) -> ApiResponse:
        return ApiResponse(status, json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(), headers)

    def _error(self, status: HTTPStatus, code: str, message: str) -> ApiResponse:
        return self._json(status, {"error": code, "message": message, "command_version": self.queue.version})

    def _prune_sessions(self) -> None:
        now = time.monotonic()
        for token, value in tuple(self._sessions.items()):
            if value[0] < now:
                del self._sessions[token]

    def _rate_allowed(self, kind: str, remote_host: str, *, limit: int) -> bool:
        """Fixed one-minute local limiter; it avoids retaining credentials."""
        now = time.monotonic()
        key = kind, remote_host
        with self._lock:
            started, count = self._rates.get(key, (now, 0))
            if now - started >= 60:
                started, count = now, 0
            if count >= limit:
                self._rates[key] = (started, count)
                return False
            self._rates[key] = (started, count + 1)
            return True


def _command_record(command: Command, version: int) -> dict[str, Any]:
    return {"command_id": command.command_id, "status": command.status.value, "type": command.type.value,
            "command_version": version, "effective_not_before_tick": command.not_before_tick,
            "expires_at_tick": command.expires_at_tick, "apply_result": dict(command.apply_result)}


def _header(headers: Mapping[str, str], name: str) -> str | None:
    return next((value for key, value in headers.items() if key.lower() == name.lower()), None)


def _expected_version(headers: Mapping[str, str]) -> int:
    raw = _header(headers, "If-Match")
    if raw is None:
        raise CommandError("VERSION_REQUIRED", "If-Match is required", status=428)
    prefix = '"command-version-'
    if not raw.startswith(prefix) or not raw.endswith('"'):
        raise CommandError("INVALID_VERSION", "If-Match must use command-version-N")
    try:
        return int(raw[len(prefix):-1])
    except ValueError as exc:
        raise CommandError("INVALID_VERSION", "If-Match must use command-version-N") from exc


def _json_object(body: bytes) -> Mapping[str, Any]:
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CommandError("INVALID_JSON", "body must be valid JSON") from exc
    if not isinstance(value, Mapping):
        raise CommandError("INVALID_BODY", "body must be a JSON object")
    return value


def _loopback(host: str) -> bool:
    return host in {"127.0.0.1", "::1", "localhost"}


def _current_tick(snapshot: Mapping[str, object]) -> int | None:
    value = snapshot.get("last_tick")
    return value if type(value) is int and value >= 0 else None
