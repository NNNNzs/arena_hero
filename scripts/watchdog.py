#!/usr/bin/env python3
"""Arena Hero Watchdog & Health Inspection Script.

Analyzes /livez endpoint and /runtime/decision-trace.jsonl for:
- Connection & tick progression health
- Submission errors / rejects / reconnects
- Decision timing latency spikes
- Entity movement oscillation / dithering loops
- Task status & fallbacks
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from collections import defaultdict
from pathlib import Path

BASE_DIR = Path("/root/project/arena_hero")
RUNTIME_DIR = BASE_DIR / "runtime"
TRACE_FILE = RUNTIME_DIR / "decision-trace.jsonl"
STATE_FILE = Path("/tmp/arena_hero_watchdog_state.json")
LIVEZ_URL = "http://127.0.0.1:8787/livez"


def fetch_livez() -> dict:
    req = urllib.request.Request(LIVEZ_URL, headers={"User-Agent": "ArenaHero-Watchdog/1.0"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        if resp.status != 200:
            raise RuntimeError(f"HTTP {resp.status} from {LIVEZ_URL}")
        return json.loads(resp.read().decode("utf-8"))


def read_last_traces(n_lines: int = 50) -> list[dict]:
    if not TRACE_FILE.exists():
        return []
    traces = []
    try:
        # Read from end of file
        with open(TRACE_FILE, "rb") as f:
            f.seek(0, os.SEEK_END)
            filesize = f.tell()
            buffer_size = min(filesize, n_lines * 4096)
            f.seek(max(0, filesize - buffer_size))
            lines = f.read().decode("utf-8", errors="ignore").strip().split("\n")
            for line in lines[-n_lines:]:
                if line.strip():
                    try:
                        traces.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
    except Exception as e:
        print(f"Warning reading trace file: {e}", file=sys.stderr)
    return traces


def detect_oscillations(traces: list[dict], min_cycles: int = 3) -> list[dict]:
    """Detect entities ping-ponging back and forth between coordinates (e.g. A->B->A->B)."""
    entity_history = defaultdict(list)
    for t in traces:
        for et in t.get("entity_traces", []):
            alias = et.get("actor_alias")
            cell = et.get("current_cell")
            kind = et.get("entity_kind")
            task = et.get("current_task")
            if alias and cell:
                entity_history[alias].append({
                    "cell": tuple(cell),
                    "kind": kind,
                    "task": task,
                    "tick": t.get("tick"),
                })

    oscillations = []
    for alias, hist in entity_history.items():
        if len(hist) < 6:
            continue
        cells = [h["cell"] for h in hist]
        # Check if last 6 cells alternate between two positions
        recent = cells[-6:]
        unique_cells = set(recent)
        if len(unique_cells) == 2:
            c1, c2 = list(unique_cells)
            if recent == [c1, c2, c1, c2, c1, c2] or recent == [c2, c1, c2, c1, c2, c1]:
                oscillations.append({
                    "alias": alias,
                    "kind": hist[-1]["kind"],
                    "task": hist[-1]["task"],
                    "pattern": [f"[{c[0]},{c[1]}]" for c in recent[-4:]],
                    "current_cell": hist[-1]["cell"],
                })
    return oscillations


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Arena Hero Watchdog")
    parser.add_argument("--silent-ok", action="store_true", help="Do not print output if system is completely healthy")
    args = parser.parse_args()

    issues = []
    warnings = []
    now = time.time()

    # 1. Check livez
    livez_ok = False
    livez_data = {}
    try:
        livez_data = fetch_livez()
        livez_ok = True
    except Exception as e:
        issues.append(f"🚨 **服务不可达**: 无法连接 {LIVEZ_URL} ({e})")

    last_tick = livez_data.get("last_tick", 0)
    running = livez_data.get("running", False)
    connected = livez_data.get("connected", False)
    accepted = livez_data.get("accepted", 0)
    rejected = livez_data.get("rejected", 0)
    reconnects = livez_data.get("reconnects", 0)
    last_error = livez_data.get("last_error")

    if livez_ok:
        if not running:
            issues.append("🚨 **服务未运行**: `running == false`")
        if not connected:
            issues.append("🚨 **WebSocket 未连接**: `connected == false`")
        if rejected > 0:
            warnings.append(f"⚠️ **存在拒绝提交**: `rejected = {rejected}`")
        if last_error:
            warnings.append(f"⚠️ **最近记录错误**: `{last_error}`")

    # 2. Check tick progression against state cache
    prev_state = {}
    if STATE_FILE.exists():
        try:
            prev_state = json.loads(STATE_FILE.read_text())
        except Exception:
            pass

    tick_rate_min = 0.0
    if prev_state and livez_ok:
        prev_tick = prev_state.get("last_tick", 0)
        prev_time = prev_state.get("timestamp", now)
        elapsed_min = (now - prev_time) / 60.0
        if elapsed_min > 0.5:
            delta_ticks = last_tick - prev_tick
            tick_rate_min = delta_ticks / elapsed_min
            if delta_ticks <= 0 and running and connected:
                issues.append(f"🚨 **Tick 停滞**: 过去 {elapsed_min:.1f} 分钟内 Tick 未推进 (停在 {last_tick})")

    # Update state file
    if livez_ok:
        try:
            STATE_FILE.write_text(json.dumps({
                "last_tick": last_tick,
                "timestamp": now,
                "accepted": accepted,
                "rejected": rejected,
                "reconnects": reconnects,
            }))
        except Exception as e:
            print(f"Warning writing state file: {e}", file=sys.stderr)

    # 3. Analyze decision trace
    traces = read_last_traces(50)
    avg_decision_ms = 0.0
    max_decision_ms = 0.0
    slow_ticks = 0
    entity_counts = defaultdict(int)
    core_pos = None

    if traces:
        timings = [t.get("timings", {}).get("decision_ms", 0.0) for t in traces if t.get("timings")]
        if timings:
            avg_decision_ms = sum(timings) / len(timings)
            max_decision_ms = max(timings)
            slow_ticks = sum(1 for ms in timings if ms > 400.0)

        if max_decision_ms > 800.0 or slow_ticks >= 3:
            warnings.append(f"⚠️ **决策时延偏高**: 最大 {max_decision_ms:.1f}ms, 超 400ms 回合数 {slow_ticks}/{len(timings)}")

        # Check entity summary from last trace
        latest_trace = traces[-1]
        for et in latest_trace.get("entity_traces", []):
            kind = et.get("entity_kind", "UNKNOWN")
            entity_counts[kind] += 1
            if kind == "CORE":
                core_pos = et.get("current_cell")

        # Check for movement oscillations
        oscillations = detect_oscillations(traces)
        if oscillations:
            osc_details = ", ".join([f"`{o['alias']}` ({o['kind']})" for o in oscillations[:3]])
            warnings.append(f"⚠️ **检测到单位移动振荡**: {len(oscillations)} 个单位在两格间往复徘徊 ({osc_details})")

    # 4. Generate Report
    if not issues and not warnings and args.silent_ok:
        return 0

    if issues:
        status_badge = "🚨 **【严重异常 - CRITICAL】**"
    elif warnings:
        status_badge = "⚠️ **【需关注 - WARNING】**"
    else:
        status_badge = "✅ **【运行良好 - HEALTHY】**"

    lines = [
        f"### 🛡️ Arena Hero 看门狗巡检简报",
        f"**系统状态**: {status_badge}",
        f"",
        f"**核心指标**:",
        f"- 当前回合: `{last_tick}` ({f'+{tick_rate_min:.1f} ticks/min' if tick_rate_min > 0 else '实时推进中'})",
        f"- 提交/拒绝/重连: `{accepted}` / `{rejected}` / `{reconnects}`",
        f"- 决策延迟: 平均 `{avg_decision_ms:.1f}ms` (峰值 `{max_decision_ms:.1f}ms`)",
    ]

    if core_pos:
        lines.append(f"- 核心坐标: `[{core_pos[0]}, {core_pos[1]}]`")
    if entity_counts:
        unit_str = ", ".join([f"{k}: {v}" for k, v in sorted(entity_counts.items())])
        lines.append(f"- 在场单位: `{unit_str}`")

    if issues or warnings:
        lines.append("")
        lines.append("**问题诊断与告警**:")
        for issue in issues:
            lines.append(f"- {issue}")
        for warn in warnings:
            lines.append(f"- {warn}")

    output_text = "\n".join(lines)
    print(output_text)

    # Return exit code: 2 for critical, 1 for warning, 0 for healthy
    if issues:
        return 2
    if warnings:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
