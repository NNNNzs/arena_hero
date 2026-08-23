"""矿区 chunk 配额与饱和度计算。

官方机制：矿每 4 resolved tick 按 chunk 配额原位补货。
- chunk 大小: 32×32
- chunk 坐标: cx = floor(x/32), cy = floor(y/32)
- axis 函数: axis(c) = c if c >= 0 else -c-1
- ring = |axis(cx)| + |axis(cy)|
- 配额 quota = max(2, floor(16 * 8 / (8 + ring)))
- 补货 tick 对齐: 每 4 的倍数 tick (tick % 4 == 0)

数据结构:
- chunk_key: str = "cx,cy" (chunk 坐标字符串)
- 每个 chunk 记录: {visible_count, quota, last_confirmed_tick, resource_cells}
"""

from __future__ import annotations

import math
from typing import Any


def axis(c: int) -> int:
    """官方 axis 函数：非负返回原值，负数返回 -c-1。"""
    return c if c >= 0 else -c - 1


def chunk_key(x: int, y: int) -> str:
    """计算格子 (x,y) 所属 chunk 的字符串键。"""
    return f"{math.floor(x / 32)},{math.floor(y / 32)}"


def chunk_coords(x: int, y: int) -> tuple[int, int]:
    """计算格子 (x,y) 所属 chunk 坐标。"""
    return math.floor(x / 32), math.floor(y / 32)


def chunk_quota(cx: int, cy: int) -> int:
    """计算 chunk (cx,cy) 的补货配额。"""
    ring = abs(axis(cx)) + abs(axis(cy))
    return max(2, math.floor(16 * 8 / (8 + ring)))


def next_refresh_tick(current_tick: int) -> int:
    """返回当前 tick 之后的下一个 4 的倍数 tick。"""
    remainder = current_tick % 4
    if remainder == 0:
        return current_tick + 4
    return current_tick + (4 - remainder)


def compute_chunk_saturation(
    resource_cells: set[tuple[int, int]],
    mined_cells: set[tuple[int, int]],
    resource_observations: dict[tuple[int, int], int],
    current_tick: int,
) -> dict[str, dict[str, Any]]:
    """计算所有已知 chunk 的饱和度信息。

    参数:
        resource_cells: 当前可见的资源格集合
        mined_cells: 已记忆的已挖空格集合
        resource_observations: 资源格最后可见 tick 映射
        current_tick: 当前 tick

    返回:
        chunk_key -> {
            cx, cy: chunk 坐标
            quota: 配额
            visible_count: 当前可见矿数
            saturation: 饱和度 (visible_count / quota, 0~1)
            next_refresh: 下次补货 tick
            resource_cells: chunk 内可见矿点列表
            mined_count: chunk 内已挖空数
        }
    """
    chunks: dict[str, dict[str, Any]] = {}

    # 统计当前可见资源
    for x, y in resource_cells:
        key = chunk_key(x, y)
        cx, cy = chunk_coords(x, y)
        if key not in chunks:
            chunks[key] = {
                "cx": cx, "cy": cy,
                "quota": chunk_quota(cx, cy),
                "visible_count": 0,
                "resource_cells": [],
                "mined_count": 0,
            }
        chunks[key]["visible_count"] += 1
        chunks[key]["resource_cells"].append([x, y])

    # 统计已挖空的格子（可能在无当前可见资源的 chunk 中）
    for x, y in mined_cells:
        key = chunk_key(x, y)
        cx, cy = chunk_coords(x, y)
        if key not in chunks:
            chunks[key] = {
                "cx": cx, "cy": cy,
                "quota": chunk_quota(cx, cy),
                "visible_count": 0,
                "resource_cells": [],
                "mined_count": 0,
            }
        chunks[key]["mined_count"] += 1

    # 也包含记忆中的资源点（不在当前可见但在 resource_observations 中）
    for x, y in resource_observations:
        if (x, y) not in resource_cells and (x, y) not in mined_cells:
            key = chunk_key(x, y)
            cx, cy = chunk_coords(x, y)
            if key not in chunks:
                chunks[key] = {
                    "cx": cx, "cy": cy,
                    "quota": chunk_quota(cx, cy),
                    "visible_count": 0,
                    "resource_cells": [],
                    "mined_count": 0,
                }
            # 记忆中的资源点不计入 visible_count，但记录位置
            chunks[key]["resource_cells"].append([x, y])

    # 计算饱和度和下次补货 tick
    next_refresh = next_refresh_tick(current_tick)
    for key, info in chunks.items():
        quota = info["quota"]
        visible = info["visible_count"]
        info["saturation"] = round(min(1.0, visible / quota), 3) if quota > 0 else 0.0
        info["next_refresh"] = next_refresh
        info["refresh_countdown"] = next_refresh - current_tick

    return chunks
