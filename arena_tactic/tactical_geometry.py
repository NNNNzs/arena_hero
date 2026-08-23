"""Pure geometry and terrain scoring for advanced combat and settlement tactics."""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from arena_hero import CoreView, UnitType, UnitView

from .context import _supercover_line
from .models import Position
from .navigation import DIRECTIONS, destination, distance, shot_range


def vision_radius(observer: CoreView | UnitView) -> int:
    if isinstance(observer, CoreView):
        return 5
    return {
        UnitType.WORKER: 3,
        UnitType.VANGUARD: 4,
        UnitType.RANGER: 5,
    }[observer.unit_type]


def has_line_of_sight(
    observer: CoreView | UnitView,
    target: Position,
    obstacles: Iterable[Position],
) -> bool:
    """Apply the authoritative Manhattan-radius, supercover vision rule."""
    if distance(observer.position, target) > vision_radius(observer):
        return False
    blocked = set(obstacles)
    return not any(
        cell in blocked
        for cell in _supercover_line(observer.position, target)[1:-1]
    )


def shadow_fire_advantage(
    ranger_cell: Position,
    enemy: CoreView | UnitView,
    obstacles: Iterable[Position],
) -> float:
    """Score legal Ranger fire which the target's own LOS cannot answer.

    This deliberately models only the target object's vision. Other enemy
    observers may still reveal the Ranger in the authoritative global state.
    """
    blocked = set(obstacles)
    if shot_range(ranger_cell, enemy.position, blocked) is None:
        return 0.0
    if has_line_of_sight(enemy, ranger_cell, blocked):
        return 0.0
    # A closer firing line is more stable if the target moves one cell.
    firing_range = shot_range(ranger_cell, enemy.position, blocked) or 3
    return 75.0 + (3 - firing_range) * 5.0


def mineral_tank_score(
    cell: Position,
    *,
    resource_cells: Iterable[Position],
    enemy_cells: Iterable[Position],
    core_cell: Position,
    obstacles: Iterable[Position],
) -> float:
    """Score a Vanguard mineral denial/defense position from current facts."""
    resources, enemies, blocked = set(resource_cells), tuple(enemy_cells), set(obstacles)
    if cell in blocked or not resources or not enemies:
        return float("-inf")
    nearest_resource = min(distance(cell, resource) for resource in resources)
    nearest_enemy = min(distance(cell, enemy) for enemy in enemies)
    if nearest_resource > 1 or min(distance(resource, enemy) for resource in resources for enemy in enemies) > 5:
        return float("-inf")
    score = 70.0 if cell in resources else 45.0
    score += max(0, 5 - nearest_enemy) * 8.0
    score += max(0, 6 - distance(cell, core_cell)) * 3.0
    # Adjacent enemies can all be hit in the best occupied target cell.
    adjacent_counts: dict[Position, int] = {}
    for enemy in enemies:
        if distance(cell, enemy) == 1:
            adjacent_counts[enemy] = adjacent_counts.get(enemy, 0) + 1
    score += max(adjacent_counts.values(), default=0) * 20.0
    return score


def best_mineral_tank_cell(
    *,
    resource_cells: Iterable[Position],
    enemy_cells: Iterable[Position],
    core_cell: Position,
    obstacles: Iterable[Position],
    occupied: Iterable[Position] = (),
) -> Position | None:
    resources = set(resource_cells)
    enemies = tuple(enemy_cells)
    blocked, occupied_cells = set(obstacles), set(occupied)
    candidates = set(resources)
    for resource in resources:
        candidates.update(destination(resource, direction) for direction in DIRECTIONS)
    candidates -= blocked | occupied_cells
    ranked = [
        (mineral_tank_score(cell, resource_cells=resources, enemy_cells=enemies,
                            core_cell=core_cell, obstacles=blocked), cell)
        for cell in candidates
    ]
    valid = [item for item in ranked if item[0] != float("-inf")]
    return max(valid, key=lambda item: (item[0], -distance(item[1], core_cell), item[1]), default=(0, None))[1]


def migration_site_score(
    cell: Position,
    *,
    resource_observations: Mapping[Position, int],
    obstacles: Iterable[Position],
    explored: Iterable[Position],
    current_tick: int,
    resource_radius: int = 6,
) -> float:
    """Combine remembered mineral heat with observed defensive terrain.

    Resource sightings are intentionally soft, age-decayed hints. Terrain
    bonuses are awarded only for explored cells, so fog is never invented as
    a wall or chokepoint.
    """
    blocked, known = set(obstacles), set(explored)
    if cell in blocked:
        return float("-inf")
    mineral_heat = 0.0
    for resource, seen_tick in resource_observations.items():
        separation = distance(cell, resource)
        if separation <= resource_radius:
            age_factor = 1.0 / (1.0 + max(0, current_tick - seen_tick) / 8.0)
            mineral_heat += (resource_radius + 1 - separation) * age_factor * 4.0
    neighbors = [destination(cell, direction) for direction in DIRECTIONS]
    known_neighbors = [neighbor for neighbor in neighbors if neighbor in known or neighbor in blocked]
    wall_count = sum(neighbor in blocked for neighbor in neighbors)
    passable_count = sum(neighbor in known and neighbor not in blocked for neighbor in neighbors)
    terrain_score = wall_count * 14.0
    if len(known_neighbors) == 4 and passable_count == 2:
        terrain_score += 24.0
    elif len(known_neighbors) == 4 and passable_count == 1:
        terrain_score += 12.0  # defensible, but avoid overvaluing a dead end
    return mineral_heat + terrain_score


def rich_resource_center(
    resource_observations: Mapping[Position, int], *, current_tick: int, bucket_size: int = 6,
    top_n: int = 1,
) -> list[dict[str, object]] | None:
    """返回加权资源桶的 top-N 候选中心。

    每个候选包含:
      - center: 候选中心坐标 (Position)
      - score: 加权总分 (float)
      - resource_count: 桶内矿点数量 (int)
      - resources: 桶内矿点列表 (list[Position])

    当 top_n=1 时兼容旧行为，返回单元素列表。
    """
    if not resource_observations:
        return None
    buckets: dict[Position, list[tuple[Position, float]]] = {}
    for cell, seen_tick in resource_observations.items():
        weight = 1.0 / (1.0 + max(0, current_tick - seen_tick) / 8.0)
        bucket = cell[0] // bucket_size, cell[1] // bucket_size
        buckets.setdefault(bucket, []).append((cell, weight))
    # 按加权总分排序，取 top-N
    ranked_buckets = sorted(
        buckets.values(),
        key=lambda items: (sum(w for _, w in items), len(items), min(c for c, _ in items)),
        reverse=True,
    )[:max(1, top_n)]
    results: list[dict[str, object]] = []
    for richest in ranked_buckets:
        total = sum(weight for _, weight in richest)
        center: Position = (
            round(sum(cell[0] * weight for cell, weight in richest) / total),
            round(sum(cell[1] * weight for cell, weight in richest) / total),
        )
        nearest = min((cell for cell, _ in richest), key=lambda cell: (distance(cell, center), cell))
        results.append({
            "center": nearest,
            "score": total,
            "resource_count": len(richest),
            "resources": sorted(cell for cell, _ in richest),
        })
    return results if results else None
