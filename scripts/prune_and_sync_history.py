#!/usr/bin/env python3
"""Safely reconcile rotated Hot Tier JSONL volumes with Supabase Cold Tier.

The live process never blocks a Tick for this work.  Run this utility from a
cron job or manually to repair a backlog and remove only *rotated* volumes
whose every Tick is present in the corresponding Supabase table.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from arena_tactic.storage import SupabaseStorage  # noqa: E402


SOURCES = (
    ("replay.jsonl", "arena_replays", "replay"),
    ("decision-trace.jsonl", "arena_decision_traces", "trace"),
)


def records(path: Path, operation: str) -> list[dict[str, object]]:
    """Read valid Tick-bearing records, tolerating one crash-torn final line."""
    result: list[dict[str, object]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return result
    for line in lines:
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if (
            isinstance(value, dict)
            and isinstance(value.get("tick"), int)
            and not (operation == "trace" and value.get("record_type") in {"trace_tick_summary", "trace_drop_summary"})
        ):
            result.append(value)
    return result


def remote_ticks(storage: SupabaseStorage, table: str, ticks: set[int]) -> set[int] | None:
    """Fetch exact existing Tick IDs in small PostgREST requests, including gaps."""
    found: set[int] = set()
    ordered_ticks = sorted(ticks)
    for start in range(0, len(ordered_ticks), 200):
        batch = ordered_ticks[start:start + 200]
        rows = storage.select(table, params={"select": "tick", "tick": "in.(" + ",".join(map(str, batch)) + ")"})
        if rows is None:
            return None
        found.update(int(row["tick"]) for row in rows if isinstance(row.get("tick"), int))
    return found


def watermark(storage: SupabaseStorage, table: str, order: str) -> int | None:
    rows = storage.select(table, params={"select": "tick", "order": f"tick.{order}", "limit": "1"})
    if not rows or not isinstance(rows[0].get("tick"), int):
        return None
    return int(rows[0]["tick"])


def reconcile_file(storage: SupabaseStorage, path: Path, table: str, operation: str, dry_run: bool) -> tuple[int, int, bool]:
    values = records(path, operation)
    ticks = {int(record["tick"]) for record in values}
    present = remote_ticks(storage, table, ticks)
    if present is None:
        return 0, len(ticks), False
    missing = [record for record in values if int(record["tick"]) not in present]
    uploaded = 0
    if not dry_run:
        saver = getattr(storage, f"save_{operation}")
        for record in missing:
            if not saver(record):
                return uploaded, len(missing) - uploaded, False
            uploaded += 1
        # Verify the upsert acknowledgement with an exact Tick check before
        # allowing lifecycle cleanup.
        present = remote_ticks(storage, table, ticks)
        if present is None:
            return uploaded, len(ticks), False
    return uploaded, len(ticks - present), present == ticks


def main() -> int:
    parser = argparse.ArgumentParser(description="sync and safely prune Arena Hero JSONL history")
    parser.add_argument("--dry-run", action="store_true", help="report only; make no Supabase or file changes")
    parser.add_argument("--days", type=float, default=7, help="retain rotated volumes newer than this many days (default: 7)")
    parser.add_argument("--runtime-dir", type=Path, default=ROOT / "runtime", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.days < 0:
        parser.error("--days must be nonnegative")
    storage = SupabaseStorage.from_environment(ROOT / ".env")
    if storage is None:
        print("Supabase is not configured; refusing history cleanup / 未配置 Supabase，拒绝清理历史卷。")
        return 2

    cutoff = time.time() - args.days * 86400
    mode = "DRY_RUN (演练)" if args.dry_run else "APPLY (执行)"
    print(f"{mode}: retention={args.days:g} days / 保留 {args.days:g} 天")
    failures = 0
    for filename, table, operation in SOURCES:
        low, high = watermark(storage, table, "asc"), watermark(storage, table, "desc")
        print(f"{table}: remote watermark / 云端水位 tick={low}..{high}")
        paths = sorted(args.runtime_dir.glob(filename + "*"), key=lambda item: item.name)
        for path in paths:
            uploaded, unsynced, safe = reconcile_file(storage, path, table, operation, args.dry_run)
            age_expired = path.name != filename and path.stat().st_mtime < cutoff
            action = "kept"
            if age_expired and safe and not args.dry_run:
                path.unlink()
                action = "pruned (已安全清理)"
            elif age_expired and safe:
                action = "would-prune (将安全清理)"
            elif unsynced:
                action = "protected-unsynced (未同步受保护)"
                if not args.dry_run:
                    failures += 1
            print(f"{path.name}: upload={uploaded} unsynced={unsynced} {action}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
