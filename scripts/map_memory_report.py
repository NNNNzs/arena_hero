#!/usr/bin/env python3
"""战术地图记忆分析：已探索区域 / 已采矿点 / 视野统计与交叉诊断。

用法:
    python3 scripts/map_memory_report.py            # 完整报告
    python3 scripts/map_memory_report.py --json     # 结构化输出
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

RUNTIME = Path(__file__).resolve().parent.parent / "runtime"
STATE_FILE = RUNTIME / "agent-state.json"


def _cells(raw) -> list[tuple[int, int]]:
    out = []
    for c in raw or []:
        try:
            out.append((int(c[0]), int(c[1])))
        except (TypeError, ValueError, IndexError):
            continue
    return out


def load_state() -> dict:
    if not STATE_FILE.exists():
        sys.exit(f"未找到 {STATE_FILE}")
    return json.loads(STATE_FILE.read_text())


def memory_view(state: dict) -> dict:
    """兼容 agent-state.json 的多种布局，尽量取出 memory 字段。"""
    mem = state.get("memory") or state.get("agent", {}).get("memory") or {}
    if not mem:  # 兜底：顶层直接找键
        mem = state
    return {
        "tick": state.get("tick") or state.get("current_tick"),
        "core_id": state.get("core_id") or mem.get("last_core_id"),
        "explored": set(_cells(mem.get("explored"))),
        "mined": set(_cells(mem.get("mined_cells") or mem.get("mined"))),
        "obstacles": set(_cells(mem.get("obstacles"))),
    }


def bbox(cells):
    xs = [c[0] for c in cells]
    ys = [c[1] for c in cells]
    return min(xs), min(ys), max(xs), max(ys)


def dist(a, b) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def report(view: dict) -> dict:
    explored, mined = view["explored"], view["mined"]
    core = None
    # 核心坐标若在顶层实体里可补充；此处以探索区质心作参照
    ref = (
        sum(c[0] for c in explored) / len(explored),
        sum(c[1] for c in explored) / len(explored),
    ) if explored else None

    out = {
        "tick": view["tick"],
        "core_id": str(view["core_id"])[:8] if view["core_id"] else None,
        "explored_cells": len(explored),
        "mined_cells": len(mined),
        "obstacles_known": len(view["obstacles"]),
        "mined_in_explored": len(mined & explored),
        "mined_outside_explored": sorted(mined - explored),
    }
    if explored:
        x0, y0, x1, y1 = bbox(explored)
        out["explored_bbox"] = [x0, y0, x1, y1]
        out["explored_area"] = f"{x1 - x0 + 1} x {y1 - y0 + 1}"
    if ref and mined:
        nearest = min(mined, key=lambda m: dist(m, ref))
        out["nearest_mined_to_center"] = {"cell": list(nearest), "dist": round(dist(nearest, ref), 1)}
    if ref and not mined:
        # 探索了却没矿记录：列出离中心最远的探索边缘，辅助判断是否该扩探/迁移
        far = max(explored, key=lambda c: dist(c, ref))
        out["diagnosis"] = {
            "issue": "已探索但无任何采集矿点记录",
            "possible_causes": [
                "本局尚未有 HARVEST_SUCCEEDED / RESOURCE_DEPLETED 事件（矿点只在采集成功或枯竭时才记入 mined_cells）",
                "重生后 explored 已清零、当前探索区是新出生点周边（正常）",
                "前端渲染层未收到 mined 数据（检查 /api/dashboard 的 map.mined 字段）",
            ],
            "farthest_explored_cell": list(far),
        }
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    result = report(memory_view(load_state()))
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    print("== Arena Hero 地图记忆报告 ==")
    print(f"Tick: {result['tick']}  Core: {result['core_id']}")
    print(f"已探索格子: {result['explored_cells']}  范围: {result.get('explored_area', '-')}")
    print(f"已采矿点:   {result['mined_cells']}  （其中在探索区内: {result['mined_in_explored']}）")
    print(f"已知障碍:   {result['obstacles_known']}")
    if "nearest_mined_to_center" in result:
        n = result["nearest_mined_to_center"]
        print(f"最近矿点距探索中心: {n['dist']} 格 @ {n['cell']}")
    if "diagnosis" in result:
        d = result["diagnosis"]
        print(f"\n⚠️  {d['issue']}")
        for cause in d["possible_causes"]:
            print(f"   - {cause}")


if __name__ == "__main__":
    main()
