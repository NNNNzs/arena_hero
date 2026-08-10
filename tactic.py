"""Arena Hero SDK loop for the adaptive local tactic."""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from getpass import getpass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from arena_hero import ArenaHeroClient

from arena_tactic import AgentConfig, AgentRuntime, DecisionResult, MemoryStore
from arena_tactic.command_api import CommandApi
from arena_tactic.command_center import CommandQueue
from arena_tactic.domain import BoundedAuditSink, BoundedTraceSink
from arena_tactic.context import DecisionContext
from arena_tactic.dashboard import DASHBOARD_HTML, DashboardDataStore, dashboard_static_asset, redact_error
from arena_tactic.observability import ReplayWriter, summary_line
from arena_tactic.runtime import choose_actions

__all__ = ["choose_actions", "play", "serve"]


@dataclass
class ServiceStatus:
    """Thread-safe status snapshot exposed by the health endpoint."""

    started_at: float = field(default_factory=time.time)
    connected: bool = False
    running: bool = True
    last_tick: int | None = None
    accepted: int = 0
    rejected: int = 0
    reconnects: int = 0
    last_error: str | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def update(self, **values: object) -> None:
        with self._lock:
            for key, value in values.items():
                setattr(self, key, value)

    def increment(self, key: str) -> None:
        with self._lock:
            setattr(self, key, int(getattr(self, key)) + 1)

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            connected = self.connected
            return {
                "status": "ok" if self.running and connected else "degraded",
                "running": self.running,
                "connected": connected,
                "last_tick": self.last_tick,
                "accepted": self.accepted,
                "rejected": self.rejected,
                "reconnects": self.reconnects,
                "last_error": redact_error(self.last_error),
                "uptime_seconds": round(time.time() - self.started_at, 1),
            }


class _HealthHandler(BaseHTTPRequestHandler):
    status: ServiceStatus
    dashboard: DashboardDataStore
    command_api: CommandApi

    def do_GET(self) -> None:  # noqa: N802
        command_response = self.command_api.handle("GET", self.path, self.headers, b"", remote_host=self.client_address[0])
        if command_response is not None:
            self._send(command_response.status, command_response.body, "application/json; charset=utf-8", command_response.headers)
            return
        path = urlsplit(self.path).path
        response = _http_response(path, self.status, self.dashboard)
        if response is None:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        self._send(*response)

    def do_POST(self) -> None:  # noqa: N802
        self._command_request("POST")

    def do_PATCH(self) -> None:  # noqa: N802
        self._command_request("PATCH")

    def do_DELETE(self) -> None:  # noqa: N802
        self._command_request("DELETE")

    def _command_request(self, method: str) -> None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length < 0 or length > 16 * 1024:
            self._send(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, b'{"error":"REQUEST_TOO_LARGE"}', "application/json; charset=utf-8")
            return
        body = self.rfile.read(length)
        response = self.command_api.handle(method, self.path, self.headers, body, remote_host=self.client_address[0])
        if response is None:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        self._send(response.status, response.body, "application/json; charset=utf-8", response.headers)

    def _send(self, code: HTTPStatus, payload: bytes, content_type: str, extra_headers: tuple[tuple[str, str], ...] = ()) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for name, value in extra_headers:
            self.send_header(name, value)
        self.end_headers()
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, format: str, *args: object) -> None:
        return


def _http_response(
    path: str,
    status: ServiceStatus,
    dashboard: DashboardDataStore,
) -> tuple[HTTPStatus, bytes, str] | None:
    """Build a route response without a socket, keeping HTTP behavior testable."""
    if path.startswith("/static/"):
        asset = dashboard_static_asset(path)
        return (HTTPStatus.OK, *asset) if asset is not None else None
    if path == "/":
        return HTTPStatus.OK, DASHBOARD_HTML.encode(), "text/html; charset=utf-8"
    if path == "/api/dashboard":
        payload = json.dumps(dashboard.payload(status.snapshot), ensure_ascii=False).encode()
        return HTTPStatus.OK, payload, "application/json; charset=utf-8"
    if path not in {"/livez", "/healthz", "/status"}:
        return None
    snapshot = status.snapshot()
    payload = json.dumps(snapshot, ensure_ascii=False).encode()
    is_live = path in {"/livez", "/status"}
    code = HTTPStatus.OK if (snapshot["running"] and (is_live or snapshot["connected"])) else HTTPStatus.SERVICE_UNAVAILABLE
    return code, payload, "application/json; charset=utf-8"


def _start_health_server(
    host: str,
    port: int,
    status: ServiceStatus,
    replay_path: Path | None = None,
    command_api: CommandApi | None = None,
) -> ThreadingHTTPServer:
    dashboard = DashboardDataStore(replay_path or Path(__file__).with_name("runtime") / "replay.jsonl")
    api = command_api or CommandApi(CommandQueue(), status_snapshot=status.snapshot)
    handler = type("ArenaHeroHealthHandler", (_HealthHandler,), {"status": status, "dashboard": dashboard, "command_api": api})
    server = ThreadingHTTPServer((host, port), handler)
    threading.Thread(target=server.serve_forever, name="health-server", daemon=True).start()
    return server


def play(api_key: str, *, status: ServiceStatus | None = None, stop: threading.Event | None = None,
         command_queue: CommandQueue | None = None, config: AgentConfig | None = None) -> None:
    """Run the tactic once until the SDK stream closes or stop is requested."""
    status = status or ServiceStatus()
    stop = stop or threading.Event()
    state_file = Path(__file__).with_name("runtime") / "agent-state.json"
    replay = ReplayWriter(Path(__file__).with_name("runtime") / "replay.jsonl")
    trace_sink = BoundedTraceSink(Path(__file__).with_name("runtime") / "decision-trace.jsonl")
    runtime = AgentRuntime(memory_store=MemoryStore(state_file), trace_sink=trace_sink,
                           command_queue=command_queue, config=config or _runtime_config_from_environment())
    base_url = os.environ.get("ARENA_HERO_BASE_URL", "https://api.arenahero.io")
    try:
        with ArenaHeroClient(api_key=api_key, base_url=base_url) as game:
            for turn in game.turns():
                if stop.is_set():
                    break
                status.update(connected=True, last_error=None, last_tick=turn.tick)
                context = DecisionContext.from_turn(turn)
                result: DecisionResult = runtime.decide(turn)
                accepted = turn.submit()
                if accepted.accepted:
                    runtime.commit(result)
                    replay.append(context, result, accepted)
                    status.increment("accepted")
                else:
                    status.increment("rejected")
                print(summary_line(context, result, accepted), flush=True)
    finally:
        runtime.close()
    status.update(connected=False)


def serve(api_key: str, *, host: str = "127.0.0.1", port: int = 8787) -> None:
    """Run a self-healing 24/7 worker with a health/status HTTP endpoint."""
    status = ServiceStatus()
    stop = threading.Event()
    audit_sink = BoundedAuditSink(Path(__file__).with_name("runtime") / "audit.jsonl")
    command_queue = CommandQueue(audit_sink=audit_sink)
    command_api = CommandApi.from_environment(command_queue, status.snapshot)
    health = _start_health_server(host, port, status, command_api=command_api)
    reconnect_delay = float(os.environ.get("ARENA_HERO_RECONNECT_DELAY", "10"))
    try:
        while not stop.is_set():
            try:
                play(api_key, status=status, stop=stop, command_queue=command_queue)
            except Exception as exc:  # network/server failures must not stop 24/7 play
                safe_error = redact_error(f"{type(exc).__name__}: {exc}") or type(exc).__name__
                status.update(connected=False, last_error=safe_error)
                status.increment("reconnects")
                print(f"Arena Hero worker error: {safe_error}; retrying", flush=True)
                stop.wait(reconnect_delay)
            else:
                if not stop.is_set():
                    status.update(last_error="SDK stream closed")
                    status.increment("reconnects")
                    stop.wait(reconnect_delay)
    finally:
        status.update(running=False, connected=False)
        health.shutdown()
        health.server_close()
        audit_sink.close()


def _api_key_from_environment() -> str | None:
    """Read the key without logging it, preferring the process environment."""
    if value := os.environ.get("ARENA_HERO_API_KEY"):
        return value
    env_file = Path(__file__).with_name(".env")
    if not env_file.is_file():
        return None
    for line in env_file.read_text(encoding="utf-8").splitlines():
        name, separator, value = line.partition("=")
        if separator and name.strip() == "ARENA_HERO_API_KEY":
            return value.strip().strip("\"'") or None
    return None


def _runtime_config_from_environment() -> AgentConfig:
    """Keep every new action source opt-in for foreground rollout and rollback."""
    enabled = lambda name: os.environ.get(name) == "1"
    full_canary = enabled("ARENA_HERO_FULL_CANARY")
    return AgentConfig(
        scheduler_shadow=enabled("ARENA_HERO_SCHEDULER_SHADOW"),
        scheduler_canary=full_canary or enabled("ARENA_HERO_SCHEDULER_CANARY"),
        worker_bt_canary=full_canary or enabled("ARENA_HERO_WORKER_BT_CANARY"),
        vanguard_bt_canary=full_canary or enabled("ARENA_HERO_VANGUARD_BT_CANARY"),
        ranger_bt_canary=full_canary or enabled("ARENA_HERO_RANGER_BT_CANARY"),
        core_bt_canary=full_canary or enabled("ARENA_HERO_CORE_BT_CANARY"),
        beacon_campaign_v1=full_canary or enabled("ARENA_HERO_BEACON_CAMPAIGN_V1"),
        core_migration_v1=full_canary or enabled("ARENA_HERO_CORE_MIGRATION_V1"),
        core_attack_campaign_v1=full_canary or enabled("ARENA_HERO_CORE_ATTACK_CAMPAIGN_V1"),
        planner_canary=full_canary or enabled("ARENA_HERO_PLANNER_CANARY"),
    )


if __name__ == "__main__":
    try:
        serve(
            _api_key_from_environment() or getpass("Arena Hero API key: "),
            host=os.environ.get("ARENA_HERO_HEALTH_HOST", "127.0.0.1"),
            port=int(os.environ.get("ARENA_HERO_HEALTH_PORT", "8787")),
        )
    except KeyboardInterrupt:
        print("Arena Hero service stopped by user.")
