"""Deterministic bounded pathfinding and safe fallback movement."""

from __future__ import annotations

from heapq import heappop, heappush
from time import perf_counter
from typing import Iterable
from uuid import UUID

from arena_hero import Direction, UnitType, UnitView

from .context import DecisionContext
from .models import AgentConfig, Position, ReservationTable


DIRECTIONS = (Direction.UP, Direction.DOWN, Direction.LEFT, Direction.RIGHT)
DIRECTION_OFFSETS = ((0, -1), (0, 1), (-1, 0), (1, 0))
DELTA_TO_DIRECTION = {
    (0, -1): Direction.UP,
    (0, 1): Direction.DOWN,
    (-1, 0): Direction.LEFT,
    (1, 0): Direction.RIGHT,
}


def distance(a: Position, b: Position) -> int:
    return abs(a[0] - b[0]) + abs(a[1] - b[1])


def destination(origin: Position, direction: Direction) -> Position:
    dx, dy = direction.delta
    return origin[0] + dx, origin[1] + dy


def adjacent_direction(origin: Position, target: Position) -> Direction | None:
    return DELTA_TO_DIRECTION.get((target[0] - origin[0], target[1] - origin[1]))


def shot_range(
    origin: Position, target: Position, obstacles: Iterable[Position]
) -> int | None:
    dx, dy = target[0] - origin[0], target[1] - origin[1]
    range_value = max(abs(dx), abs(dy))
    if range_value not in range(1, 4):
        return None
    if dx and dy and abs(dx) != abs(dy):
        return None
    step_x = 0 if dx == 0 else (1 if dx > 0 else -1)
    step_y = 0 if dy == 0 else (1 if dy > 0 else -1)
    blocked = set(obstacles)
    if any(
        (origin[0] + step_x * step, origin[1] + step_y * step) in blocked
        for step in range(1, range_value)
    ):
        return None
    return range_value


def enemy_threat_cells(context: DecisionContext) -> set[Position]:
    cells = set(context.enemy_occupancy)
    for enemy in context.enemies:
        if isinstance(enemy, UnitView) and enemy.unit_type is UnitType.VANGUARD:
            cells.update(destination(enemy.position, direction) for direction in DIRECTIONS)
        elif isinstance(enemy, UnitView) and enemy.unit_type is UnitType.RANGER:
            for step_x, step_y in (
                (0, -1),
                (0, 1),
                (-1, 0),
                (1, 0),
                (-1, -1),
                (-1, 1),
                (1, -1),
                (1, 1),
            ):
                for range_value in range(1, 4):
                    cell = (
                        enemy.position[0] + step_x * range_value,
                        enemy.position[1] + step_y * range_value,
                    )
                    if shot_range(
                        enemy.position, cell, context.obstacle_cells
                    ) is not None:
                        cells.add(cell)
    return cells


def bounded_astar(
    start: Position,
    goal: Position,
    *,
    blocked: set[Position],
    deadline: float,
    node_limit: int,
) -> Direction | None:
    """Return only the current safe first step; unknown distant cells may replan."""
    if start == goal:
        return None
    gx, gy = goal
    margin = min(12, max(4, (abs(start[0] - gx) + abs(start[1] - gy)) // 3 + 2))
    min_x, max_x = min(start[0], gx) - margin, max(start[0], gx) + margin
    min_y, max_y = min(start[1], gy) - margin, max(start[1], gy) + margin

    queue: list[tuple[int, int, Position]] = []
    heappush(queue, (abs(start[0] - gx) + abs(start[1] - gy), 0, start))
    came_from: dict[Position, Position] = {}
    cost = {start: 0}
    visited = 0

    while queue and visited < node_limit and perf_counter() < deadline:
        _, current_cost, current = heappop(queue)
        if current == goal:
            step = current
            while came_from.get(step) != start:
                previous = came_from.get(step)
                if previous is None:
                    return None
                step = previous
            return DELTA_TO_DIRECTION.get((step[0] - start[0], step[1] - start[1]))
        if current_cost != cost.get(current):
            continue
        visited += 1
        cx, cy = current
        for dx, dy in DIRECTION_OFFSETS:
            nx = cx + dx
            ny = cy + dy
            if not (min_x <= nx <= max_x and min_y <= ny <= max_y):
                continue
            neighbor = (nx, ny)
            if neighbor in blocked and neighbor != goal:
                continue
            new_cost = current_cost + 1
            if new_cost >= cost.get(neighbor, 1 << 60):
                continue
            cost[neighbor] = new_cost
            came_from[neighbor] = current
            heappush(
                queue,
                (new_cost + abs(nx - gx) + abs(ny - gy), new_cost, neighbor),
            )
    return None


def bounded_path_cost(
    start: Position,
    goal: Position,
    *,
    blocked: set[Position],
    deadline: float,
    node_limit: int,
) -> int | None:
    """Return an obstacle-aware shortest-path cost within a bounded search."""
    if start == goal:
        return 0
    gx, gy = goal
    margin = min(12, max(4, (abs(start[0] - gx) + abs(start[1] - gy)) // 3 + 2))
    min_x, max_x = min(start[0], gx) - margin, max(start[0], gx) + margin
    min_y, max_y = min(start[1], gy) - margin, max(start[1], gy) + margin
    queue: list[tuple[int, int, Position]] = [
        (abs(start[0] - gx) + abs(start[1] - gy), 0, start)
    ]
    costs = {start: 0}
    visited = 0
    while queue and visited < node_limit and perf_counter() < deadline:
        _, current_cost, current = heappop(queue)
        if current == goal:
            return current_cost
        if current_cost != costs.get(current):
            continue
        visited += 1
        cx, cy = current
        for dx, dy in DIRECTION_OFFSETS:
            nx = cx + dx
            ny = cy + dy
            if not (min_x <= nx <= max_x and min_y <= ny <= max_y):
                continue
            neighbor = (nx, ny)
            if neighbor in blocked and neighbor != goal:
                continue
            candidate_cost = current_cost + 1
            if candidate_cost >= costs.get(neighbor, 1 << 60):
                continue
            costs[neighbor] = candidate_cost
            heappush(
                queue,
                (
                    candidate_cost + abs(nx - gx) + abs(ny - gy),
                    candidate_cost,
                    neighbor,
                ),
            )
    return None


def deterministic_fallback(
    actor_id: UUID,
    start: Position,
    goal: Position,
    blocked: set[Position],
) -> Direction | None:
    ranked = sorted(
        DIRECTIONS,
        key=lambda direction: (
            distance(destination(start, direction), goal),
            (list(DIRECTIONS).index(direction) - actor_id.int) % len(DIRECTIONS),
            direction.value,
        ),
    )
    return next(
        (
            direction
            for direction in ranked
            if destination(start, direction) not in blocked
        ),
        None,
    )


def plan_step(
    *,
    actor_id: UUID,
    start: Position,
    goal: Position,
    context: DecisionContext,
    persistent_obstacles: set[Position],
    reservations: ReservationTable,
    deadline: float,
    config: AgentConfig,
    avoid_threats: bool = False,
) -> Direction | None:
    if start == goal:
        return None
    blocked = set(persistent_obstacles)
    blocked.update(context.enemy_occupancy)
    blocked.update(context.obstacle_cells)
    blocked.discard(goal)
    if avoid_threats:
        blocked.update(enemy_threat_cells(context))
    direction = bounded_astar(
        start,
        goal,
        blocked=blocked,
        deadline=deadline,
        # Long-haul routes need more than the default node budget: the
        # bounding box is capped at +-12 cells while the corridor inside it
        # may be dense with remembered obstacles, so a fixed limit starves
        # the search before it can escape a pocket (seen as an endless
        # no_safe_route_with_cargo wait on far workers).
        node_limit=max(config.astar_node_limit, min(40 * distance(start, goal), 4000)),
    )
    # For distant goals (e.g. Beacon or far exploration across the map),
    # direct bounded A* across hundreds/thousands of tiles with a ±12 margin corridor
    # can exhaust node limits or hit corridor dead-ends.
    # Project an intermediate waypoint along the vector to find a safe local step.
    if direction is None and distance(start, goal) > 30:
        dist = distance(start, goal)
        dx = goal[0] - start[0]
        dy = goal[1] - start[1]
        step_dist = min(25, max(10, dist // 4))
        subgoal = (
            start[0] + int(round(dx * step_dist / dist)),
            start[1] + int(round(dy * step_dist / dist)),
        )
        direction = bounded_astar(
            start,
            subgoal,
            blocked=blocked,
            deadline=deadline,
            node_limit=max(config.astar_node_limit, 2000),
        )
    # A bounded search that cannot prove a route must not fall back to a
    # locally greedy step.  That step can move toward the same unreachable
    # target from alternating sides and create a left/right oscillation.
    if direction is None:
        return None
    cell = destination(start, direction)
    if cell in blocked or not reservations.reserve(cell, source=start):
        # Do not sidestep an occupied/contested first step.  The next
        # authoritative Turn will replan with fresh occupancy and memory.
        return None
    return direction
