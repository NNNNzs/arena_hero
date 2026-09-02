"""Incremental, presentation-safe event log derived from redacted replay JSONL."""

from __future__ import annotations

import json
import os
from collections import Counter
from pathlib import Path
from typing import Any

from .chunk_quota import chunk_coords, chunk_quota, next_refresh_tick


EVENT_LOG_LIMIT = 5_000
ENEMY_REAPPEAR_TICKS = 3


def _cell(value: Any) -> list[int] | None:
    if isinstance(value, (list, tuple)) and len(value) == 2 and all(type(v) is int for v in value):
        return [value[0], value[1]]
    return None


def _event_key(event: dict[str, Any]) -> tuple[Any, ...]:
    return event["type"], event.get("target"), tuple(event["position"]) if event.get("position") else None


def _description(event_type: str, position: list[int] | None, target: str | None, values: dict[str, Any]) -> str:
    where = f" @ {position[0]},{position[1]}" if position else ""
    suffix = f"（目标 {target}）" if target else ""
    if event_type == "ENEMY_SPOTTED": return f"发现敌方单位{suffix}{where}"
    if event_type == "HARVEST_SUCCEEDED": return f"采集成功（{values.get('source', 'RESOURCE_NODE')}）{where}"
    if event_type == "RESOURCE_DEPLETED": return f"资源点枯竭，预计 Tick {values.get('refresh_tick', '—')} 补货{where}"
    if event_type == "SHOT_MISSED": return f"射击未命中{suffix}{where}"
    if event_type in {"SHOOT", "HIT", "SHOT_HIT"}: return f"射击命中{suffix}{where}"
    if event_type in {"UNIT_KILLED", "UNIT_DESTROYED"}: return f"单位阵亡{suffix}{where}"
    if event_type == "CORE_DAMAGED": return f"核心受损{suffix}{where}"
    if event_type == "CORE_DESTROYED": return f"核心被摧毁{suffix}{where}"
    if event_type == "CORE_RESPAWNED": return f"核心已重生{where}"
    if event_type == "UNIT_SPAWNED": return f"单位生产完成{where}"
    if event_type.startswith("CORE_MOVE"): return f"核心迁移：{event_type.replace('CORE_MOVE_', '')}{where}"
    if event_type == "STRATEGY_MODE_CHANGED": return f"战略模式切换：{values.get('from', '—')} → {values.get('to', '—')}"
    if event_type == "DEPOSIT_FAILED": return f"存入失败：{values.get('reason', '未知原因')}{where}"
    if event_type == "RETREAT_HEAL_TRIGGERED": return f"撤退治疗触发：{values.get('reason', 'retreat')}{where}"
    return f"事件 {event_type}{where}"


def _category(event_type: str) -> str | None:
    if event_type in {"ENEMY_SPOTTED", "SHOOT", "HIT", "SHOT_HIT", "SHOT_MISSED", "UNIT_KILLED", "UNIT_DESTROYED", "CORE_DAMAGED", "CORE_DESTROYED", "CORE_RESPAWNED"}:
        return "combat"
    if event_type in {"HARVEST_SUCCEEDED", "RESOURCE_DEPLETED"}: return "harvest"
    if event_type == "UNIT_SPAWNED" or event_type.startswith("CORE_MOVE") or event_type == "STRATEGY_MODE_CHANGED": return "ops"
    if event_type in {"DEPOSIT_FAILED", "RETREAT_HEAL_TRIGGERED"}: return "anomaly"
    return None


class EventLogCollector:
    """Consume new replay lines once, including files moved to ``.1`` / ``.2``."""

    def __init__(self, replay_path: Path, *, event_path: Path | None = None, state_path: Path | None = None,
                 max_events: int = EVENT_LOG_LIMIT, enemy_reappear_ticks: int = ENEMY_REAPPEAR_TICKS,
                 supabase: Any | None = None, writer: Any | None = None) -> None:
        self.replay_path = replay_path
        self.event_path = event_path or replay_path.with_name("events.jsonl")
        self.state_path = state_path or replay_path.with_name("event-log-state.json")
        self.max_events, self.enemy_reappear_ticks = max_events, enemy_reappear_ticks
        self.supabase, self.writer = supabase, writer

    def _load_state(self) -> dict[str, Any]:
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
            return state if isinstance(state, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_state(self, state: dict[str, Any]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        temporary.write_text(json.dumps(state, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        os.replace(temporary, self.state_path)

    def _new_records(self, state: dict[str, Any]) -> list[dict[str, Any]]:
        offsets = state.setdefault("offsets", {})
        records: list[dict[str, Any]] = []
        paths = [self.replay_path.with_name(self.replay_path.name + f".{n}") for n in range(8, 0, -1)] + [self.replay_path]
        for path in paths:
            try:
                stat = path.stat()
            except OSError:
                continue
            inode = str(getattr(stat, "st_ino", 0))
            start = int(offsets.get(inode, 0))
            if start > stat.st_size: start = 0
            with path.open("rb") as stream:
                stream.seek(start)
                while True:
                    line_start = stream.tell()
                    line = stream.readline()
                    if not line: break
                    if not line.endswith(b"\n"):
                        offsets[inode] = line_start
                        break
                    offsets[inode] = stream.tell()
                    try:
                        item = json.loads(line)
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        continue
                    if isinstance(item, dict): records.append(item)
        # Keep only inode offsets relevant to the current small rotation window.
        state["offsets"] = {key: value for key, value in offsets.items() if isinstance(value, int)}
        return records

    def _record_events(self, record: dict[str, Any], state: dict[str, Any]) -> list[dict[str, Any]]:
        tick = record.get("tick") if type(record.get("tick")) is int else 0
        result: list[dict[str, Any]] = []
        enemies = (record.get("state") or {}).get("visible_enemies", []) if isinstance(record.get("state"), dict) else []
        seen = state.setdefault("enemies", {})
        for enemy in enemies if isinstance(enemies, list) else []:
            if not isinstance(enemy, dict) or not isinstance(enemy.get("id"), str): continue
            enemy_id, last = enemy["id"], seen.get(enemy["id"])
            if last is None or tick - int(last) > self.enemy_reappear_ticks:
                result.append({"tick": tick, "type": "ENEMY_SPOTTED", "target": enemy_id,
                               "position": _cell(enemy.get("position")), "values": {}})
            seen[enemy_id] = tick
        mode = record.get("mode")
        if isinstance(mode, str):
            previous = state.get("mode")
            if isinstance(previous, str) and previous != mode:
                result.append({"tick": tick, "type": "STRATEGY_MODE_CHANGED", "target": None, "position": None,
                               "values": {"from": previous, "to": mode}})
            state["mode"] = mode
        for raw in record.get("events", []) if isinstance(record.get("events"), list) else []:
            if not isinstance(raw, dict): continue
            event_type = str(raw.get("type") or "").upper()
            if event_type == "CORE_SPAWN_SUCCEEDED": event_type = "UNIT_SPAWNED"
            raw_values = raw.get("values") if isinstance(raw.get("values"), dict) else {}
            if event_type == "UNIT_DAMAGED":
                event_type = "UNIT_KILLED" if raw_values.get("hp") == 0 else "HIT"
            if event_type == "RESOURCE_DEPLETED" or event_type == "HARVEST_SUCCEEDED":
                # A successful natural-node harvest consumes the node immediately.
                source = raw.get("values", {}).get("source") if isinstance(raw.get("values"), dict) else None
                if event_type == "HARVEST_SUCCEEDED" and source not in {None, "RESOURCE_NODE"}: pass
                elif event_type == "HARVEST_SUCCEEDED":
                    position = _cell(raw.get("position"))
                    if position:
                        cx, cy = chunk_coords(*position)
                        result.append({"tick": tick, "type": "RESOURCE_DEPLETED", "target": raw.get("target"), "position": position,
                                       "values": {"refresh_tick": next_refresh_tick(tick), "quota": chunk_quota(cx, cy)}})
            category = _category(event_type)
            reason = str(raw.get("reason") or "")
            if category is None and ("retreat" in reason.lower() or "shelter" in reason.lower()):
                event_type, category = "RETREAT_HEAL_TRIGGERED", "anomaly"
            if category is None: continue
            values = raw_values
            if event_type == "RESOURCE_DEPLETED":
                position = _cell(raw.get("position"))
                if position:
                    cx, cy = chunk_coords(*position)
                    values = {**values, "refresh_tick": next_refresh_tick(tick), "quota": chunk_quota(cx, cy)}
            if event_type == "DEPOSIT_FAILED": values = {**values, "reason": reason or "未知原因"}
            result.append({"tick": tick, "type": event_type, "target": raw.get("target") if isinstance(raw.get("target"), str) else None,
                           "position": _cell(raw.get("position")), "values": values})
        merged: dict[tuple[Any, ...], dict[str, Any]] = {}
        for item in result:
            key = _event_key(item)
            if key in merged: merged[key]["count"] += 1
            else: merged[key] = {**item, "category": _category(item["type"]), "count": 1}
        return list(merged.values())

    def collect(self) -> list[dict[str, Any]]:
        state, generated = self._load_state(), []
        for record in self._new_records(state): generated.extend(self._record_events(record, state))
        if generated:
            existing = self.read(limit=self.max_events)
            combined = (existing + generated)[-self.max_events:]
            self.event_path.parent.mkdir(parents=True, exist_ok=True)
            self.event_path.write_text("".join(json.dumps({**event, "description": _description(event["type"], event.get("position"), event.get("target"), event.get("values", {}))}, ensure_ascii=False, separators=(",", ":")) + "\n" for event in combined), encoding="utf-8")
            if self.writer is not None:
                for event in generated:
                    self.writer.submit("event", {**event, "description": _description(event["type"], event.get("position"), event.get("target"), event.get("values", {}))})
        self._save_state(state)
        return generated

    def read(self, *, limit: int = 200) -> list[dict[str, Any]]:
        try:
            rows = [json.loads(line) for line in self.event_path.read_text(encoding="utf-8").splitlines()]
        except (OSError, json.JSONDecodeError):
            rows = []
        if rows:
            return [row for row in rows if isinstance(row, dict)][-max(0, limit):]
        if self.supabase is not None:
            rows = self.supabase.select("arena_events", params={"select": "*", "order": "tick.desc", "limit": str(max(0, limit))})
            if rows is not None:
                return list(reversed(rows))
        return []

    def summary(self) -> dict[str, Any]:
        self.collect()
        try:
            rows = [json.loads(line) for line in self.event_path.read_text(encoding="utf-8").splitlines()]
        except (OSError, json.JSONDecodeError):
            rows = []
        rows = [row for row in rows if isinstance(row, dict)][-self.max_events:]
        return {
            "total": len(rows),
            "counts": dict(Counter(item.get("type") for item in rows if item.get("type"))),
            "category_counts": dict(Counter(item.get("category") for item in rows if item.get("category"))),
        }

    def payload(
        self,
        *,
        limit: int = 50,
        from_tick: int | None = None,
        to_tick: int | None = None,
        category: str | None = None,
    ) -> dict[str, Any]:
        self.collect()
        rows = self.read(limit=min(limit, self.max_events) if (from_tick is None and to_tick is None) else self.max_events)
        total = len(rows)
        all_category_counts = dict(Counter(item.get("category") for item in rows if item.get("category")))
        all_type_counts = dict(Counter(item.get("type") for item in rows if item.get("type")))

        filtered = rows
        if category and category.upper() != "ALL":
            cat_norm = category.strip().lower()
            filtered = [item for item in filtered if (item.get("category") or "").lower() == cat_norm]
        if from_tick is not None:
            filtered = [item for item in filtered if (item.get("tick") or 0) >= from_tick]
        if to_tick is not None:
            filtered = [item for item in filtered if (item.get("tick") or 0) <= to_tick]

        effective_limit = max(0, min(limit, self.max_events))
        result_rows = list(reversed(filtered[-effective_limit:])) if effective_limit > 0 else []

        return {
            "events": result_rows,
            "counts": dict(Counter(item.get("type") for item in result_rows if item.get("type"))),
            "category_counts": dict(Counter(item.get("category") for item in result_rows if item.get("category"))),
            "total": total,
            "matched": len(filtered),
        }
