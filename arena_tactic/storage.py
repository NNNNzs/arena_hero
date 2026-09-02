"""Supabase/PostgREST persistence with bounded asynchronous writes.

The match loop must never wait for a database round trip.  This module uses
only the standard library and intentionally treats Supabase as an optional
replica: callers keep their local JSON/JSONL fallback authoritative when the
network is unavailable.
"""
from __future__ import annotations

import json
import os
import queue
import threading
from pathlib import Path
from typing import Any
from urllib.error import URLError, HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


def _env_file_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return values
    for line in lines:
        key, sep, value = line.partition("=")
        if sep and key.strip() in {"SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY"}:
            values[key.strip()] = value.strip().strip("\"'")
    return values


class SupabaseStorage:
    """Small PostgREST client. Failures return ``None``/``False`` and are never logged."""

    def __init__(self, url: str, service_role_key: str, *, timeout: float = 1.5) -> None:
        self.url = url.rstrip("/")
        self.service_role_key = service_role_key
        self.timeout = timeout

    @classmethod
    def from_environment(cls, env_file: Path | None = None) -> "SupabaseStorage | None":
        file_values = _env_file_values(env_file) if env_file else {}
        url = os.environ.get("SUPABASE_URL") or file_values.get("SUPABASE_URL")
        key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or file_values.get("SUPABASE_SERVICE_ROLE_KEY")
        return cls(url, key) if url and key else None

    def _request(self, method: str, table: str, *, params: dict[str, str] | None = None,
                 payload: Any = None, prefer: str | None = None) -> Any | None:
        query = f"?{urlencode(params)}" if params else ""
        data = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode() if payload is not None else None
        headers = {"apikey": self.service_role_key, "Authorization": f"Bearer {self.service_role_key}"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if prefer:
            headers["Prefer"] = prefer
        try:
            with urlopen(Request(f"{self.url}/rest/v1/{table}{query}", data=data, headers=headers, method=method), timeout=self.timeout) as response:
                body = response.read()
        except (HTTPError, URLError, OSError, ValueError):
            return None
        if not body:
            return True
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return True

    def select(self, table: str, *, params: dict[str, str]) -> list[dict[str, Any]] | None:
        value = self._request("GET", table, params=params)
        return value if isinstance(value, list) else None

    def upsert(self, table: str, row: dict[str, Any], *, conflict: str | None = None) -> bool:
        params = {"on_conflict": conflict} if conflict else None
        return self._request("POST", table, params=params, payload=row, prefer="resolution=merge-duplicates,return=minimal") is not None

    def insert(self, table: str, row: dict[str, Any]) -> bool:
        return self._request("POST", table, payload=row, prefer="return=minimal") is not None

    def load_memory(self) -> dict[str, Any] | None:
        rows = self.select("arena_agent_state", params={"id": "eq.current", "select": "scheduler_state", "limit": "1"})
        if not rows or not isinstance(rows[0].get("scheduler_state"), dict):
            return None
        return rows[0]["scheduler_state"]

    def save_memory(self, memory: dict[str, Any]) -> bool:
        cells = memory.get("resource_observations", {})
        remembered = [[int(x), int(y)] for key in cells if isinstance(key, str) and "," in key
                      for x, y in [key.split(",", 1)] if x.lstrip("-").isdigit() and y.lstrip("-").isdigit()]
        return self.upsert("arena_agent_state", {
            "id": "current", "tick": int(memory.get("last_tick", 0)), "mode": str(memory.get("last_mode", "RESPAWN")),
            "core": {"position": memory.get("last_core_position")}, "population": 0, "resources": 0,
            "resource_capacity": 0, "explored_cells": memory.get("explored", []), "obstacles": memory.get("obstacles", []),
            "remembered_resources": remembered, "enemy_tracks": memory.get("enemy_tracks", {}),
            "squads": memory.get("manual_squad_assignments", {}), "scheduler_state": memory,
        }, conflict="id")

    def save_replay(self, record: dict[str, Any]) -> bool:
        state = record.get("state", {}) if isinstance(record.get("state"), dict) else {}
        return self.upsert("arena_replays", {"tick": record.get("tick"), "mode": record.get("mode"), "core": state.get("core"),
            "population": state.get("population", 0), "resources": state.get("resources", 0), "resource_capacity": state.get("resource_capacity", 0),
            "units": state.get("units", []), "visible_enemies": state.get("visible_enemies", []), "obstacle_cells": state.get("obstacle_cells", []),
            "resource_cells": state.get("resource_cells", []), "beacon": state.get("beacon"), "events": record.get("events", []),
            "intents": record.get("intents", []), "decision_ms": record.get("decision_ms", 0.0), "accepted": record.get("accepted", False)}, conflict="tick")

    def read_replays(self, limit: int) -> list[dict[str, Any]] | None:
        rows = self.select("arena_replays", params={"select": "*", "order": "tick.desc", "limit": str(limit)})
        if rows is None:
            return None
        return [{"schema_version": 1, "tick": row.get("tick"), "mode": row.get("mode"),
                 "state": {key: row.get(key) for key in ("core", "population", "resources", "resource_capacity", "units", "visible_enemies", "obstacle_cells", "resource_cells", "beacon")},
                 "events": row.get("events") or [], "intents": row.get("intents") or [],
                 "decision_ms": row.get("decision_ms", 0.0), "accepted": bool(row.get("accepted"))}
                for row in reversed(rows)]

    def save_event(self, event: dict[str, Any]) -> bool:
        return self.insert("arena_events", {key: event.get(key) for key in ("tick", "type", "category", "position", "target", "values", "count", "description")})

    def save_trace(self, trace: dict[str, Any]) -> bool:
        # The deployed table stores the redacted envelope as JSONB ``trace``.
        return self.upsert("arena_decision_traces", {"tick": trace.get("tick"), "planner_version": trace.get("planner_version"), "trace": trace}, conflict="tick")

    def save_report(self, report: dict[str, Any]) -> bool:
        return self.insert("arena_reports", {"report_type": report.get("report_type", "runtime"), "tick_start": report.get("tick_start", 0),
            "tick_end": report.get("tick_end", 0), "summary": report.get("summary", ""), "metrics": report.get("metrics", {}),
            "raw_content": report.get("raw_content", "")})


class AsyncSupabaseWriter:
    """Best-effort Tick writer plus durable, asynchronous JSONL backfill.

    Tick records may still be dropped when the small live queue is full, but a
    rotated local volume is never discarded until a separate file job has
    successfully replayed every record in that volume.  All guard operations
    are in-memory and return immediately.
    """
    def __init__(self, storage: SupabaseStorage, *, max_records: int = 256) -> None:
        self.storage, self.queue = storage, queue.Queue(maxsize=max_records)
        self.closed = False
        self._history_lock = threading.Lock()
        self._history_pending: set[tuple[str, int, int, int]] = set()
        self._history_synced: set[tuple[str, int, int, int]] = set()
        self.thread = threading.Thread(target=self._run, name="supabase-writer", daemon=True)
        self.thread.start()

    def submit(self, operation: str, record: dict[str, Any]) -> bool:
        if self.closed:
            return False
        try:
            self.queue.put_nowait((operation, record))
            return True
        except queue.Full:
            return False

    @staticmethod
    def _file_key(operation: str, path: Path) -> tuple[str, int, int, int] | None:
        try:
            stat = path.stat()
        except OSError:
            return None
        return operation, stat.st_ino, stat.st_size, stat.st_mtime_ns

    def can_discard_history_file(self, operation: str, path: Path) -> bool:
        """Return whether *path* was fully backfilled; schedule it otherwise."""
        key = self._file_key(operation, path)
        if key is None:
            return True
        with self._history_lock:
            if key in self._history_synced:
                return True
            if key in self._history_pending or self.closed:
                return False
            self._history_pending.add(key)
        try:
            self.queue.put_nowait(("history_file", (operation, path, key)))
        except queue.Full:
            with self._history_lock:
                self._history_pending.discard(key)
        return False

    def _run(self) -> None:
        while True:
            item = self.queue.get()
            if item is None:
                return
            operation, record = item
            if operation == "history_file":
                self._sync_history_file(*record)
            else:
                getattr(self.storage, f"save_{operation}")(record)

    def _sync_history_file(self, operation: str, path: Path, key: tuple[str, int, int, int]) -> None:
        complete = True
        try:
            with path.open(encoding="utf-8") as stream:
                for line in stream:
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue  # tolerate only a torn final crash line
                    if not isinstance(record, dict) or not isinstance(record.get("tick"), int):
                        continue
                    if operation == "trace" and record.get("record_type") in {
                        "trace_tick_summary", "trace_drop_summary",
                    }:
                        # These are local loss-accounting markers, not a
                        # complete DecisionTrace and must never overwrite one.
                        continue
                    if not getattr(self.storage, f"save_{operation}")(record):
                        complete = False
                        break
        except OSError:
            complete = False
        with self._history_lock:
            self._history_pending.discard(key)
            if complete:
                self._history_synced.add(key)

    def close(self, timeout: float = 2.0) -> None:
        self.closed = True
        try: self.queue.put_nowait(None)
        except queue.Full: pass
        self.thread.join(timeout)
