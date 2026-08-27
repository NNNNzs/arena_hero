"""Squad-level movement arbitration for coordinated tactical maneuver."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from time import perf_counter
from uuid import UUID

from arena_hero import UnitType, UnitView

from .context import DecisionContext
from .memory import AgentMemory
from .models import ActionIntent, ActionKind, AgentConfig, Position, ReservationTable
from .navigation import DIRECTIONS, destination, distance, plan_step
from .squads import Squad


@dataclass(frozen=True, slots=True)
class SquadCohesion:
    """Pure current-Turn cohesion facts for one squad objective."""

    pace_unit_id: UUID | None
    hold_unit_ids: frozenset[UUID]
    regroup: bool
    pickup_ready: bool
    maximum_separation: int


def evaluate_squad_cohesion(
    squad: Squad,
    units: Iterable[UnitView],
    *,
    maximum_lead: int = 1,
    cohesion_radius: int = 4,
    pickup_radius: int = 2,
    escort_quorum: int = 2,
) -> SquadCohesion:
    """Measure progress from the slowest member instead of each unit's route."""
    live = tuple(sorted(
        (unit for unit in units if unit.id in squad.member_ids),
        key=lambda unit: unit.id.bytes,
    ))
    if not live:
        return SquadCohesion(None, frozenset(), False, False, 0)

    remaining = {unit.id: distance(unit.position, squad.target) for unit in live}
    pace = max(live, key=lambda unit: (remaining[unit.id], unit.id.bytes))
    pace_remaining = remaining[pace.id]
    hold = frozenset(
        unit.id for unit in live
        if remaining[unit.id] + max(0, maximum_lead) < pace_remaining
    )
    maximum_separation = max(
        (distance(left.position, right.position) for left in live for right in live),
        default=0,
    )
    regroup = maximum_separation > max(1, cohesion_radius)
    pickup_ready = (
        len(live) >= max(1, escort_quorum)
        and maximum_separation <= max(1, cohesion_radius)
        and all(remaining[unit.id] <= max(0, pickup_radius) for unit in live)
    )
    return SquadCohesion(pace.id, hold, regroup, pickup_ready, maximum_separation)


def intent_is_squad_protected(intent: ActionIntent | None) -> bool:
    """Keep combat and survival actions above ordinary squad movement."""
    if intent is None:
        return False
    if intent.action in {ActionKind.SHOOT, ActionKind.SWEEP, ActionKind.HEAL}:
        return True
    return any(token in intent.reason for token in (
        "retreat", "heal", "critical", "emergency", "intercept_visible_threat",
    ))


def _safe_slots(
    center: Position,
    units: tuple[UnitView, ...],
    context: DecisionContext,
    memory: AgentMemory,
    *,
    regroup: bool,
    pace_unit_id: UUID | None,
) -> dict[UUID, Position]:
    """Assign deterministic, unique loose-formation cells around an anchor."""
    cx, cy = center
    near = tuple(destination(center, direction) for direction in DIRECTIONS)
    ranger_ring = (
        (cx + 2, cy), (cx, cy + 2), (cx - 2, cy), (cx, cy - 2),
        (cx + 1, cy + 1), (cx - 1, cy + 1),
        (cx - 1, cy - 1), (cx + 1, cy - 1),
    )
    outer = (
        (cx + 3, cy), (cx, cy + 3), (cx - 3, cy), (cx, cy - 3),
    )
    blocked = memory.obstacles | set(context.obstacle_cells) | set(context.enemy_occupancy)
    used: set[Position] = set()
    slots: dict[UUID, Position] = {}
    ordered = tuple(sorted(
        units,
        key=lambda unit: (
            0 if unit.id == pace_unit_id else 1,
            0 if unit.unit_type is UnitType.VANGUARD else 1,
            unit.id.bytes,
        ),
    ))
    for unit in ordered:
        if regroup and unit.id == pace_unit_id:
            slots[unit.id] = unit.position
            used.add(unit.position)
            continue
        candidates = (
            near + ranger_ring + outer
            if regroup
            else ((center,) + near + ranger_ring + outer
                  if unit.unit_type is UnitType.VANGUARD
                  else ranger_ring + near + outer)
        )
        available = tuple(cell for cell in candidates if cell not in blocked and cell not in used)
        if not available:
            available = tuple(cell for cell in candidates if cell not in used) or (unit.position,)
        slot = min(available, key=lambda cell: (distance(unit.position, cell), cell))
        slots[unit.id] = slot
        used.add(slot)
    return slots


def _movement_reservations(
    context: DecisionContext,
    proposals: tuple[ActionIntent, ...],
    member_ids: set[UUID],
) -> ReservationTable:
    reservations = ReservationTable({
        cell: len(ids) for cell, ids in context.friendly_occupancy.items()
    })
    for intent in proposals:
        if intent.actor_id in member_ids or intent.reserved_cell is None:
            continue
        actor = context.current_objects.get(intent.actor_id)
        source = actor.position if actor is not None else None
        reservations.reserve(intent.reserved_cell, source=source)
    return reservations


def coordinate_expedition_intents(
    context: DecisionContext,
    memory: AgentMemory,
    config: AgentConfig,
    squad: Squad,
    proposals: tuple[ActionIntent, ...],
    *,
    deadline: float | None = None,
) -> tuple[ActionIntent, ...]:
    """Replace independent Beacon movement with one complete squad order."""
    members = tuple(sorted(
        (unit for unit in context.units if unit.id in squad.member_ids),
        key=lambda unit: unit.id.bytes,
    ))
    if len(members) < 2:
        return proposals

    by_actor = {intent.actor_id: intent for intent in proposals}
    protected = {
        unit.id for unit in members if intent_is_squad_protected(by_actor.get(unit.id))
    }
    contact = bool(protected)
    cohesion = evaluate_squad_cohesion(squad, members)
    reservations = _movement_reservations(context, proposals, squad.member_ids)
    center = (
        next(unit.position for unit in members if unit.id == cohesion.pace_unit_id)
        if cohesion.regroup and cohesion.pace_unit_id is not None
        else squad.target
    )
    slots = _safe_slots(
        center, members, context, memory,
        regroup=cohesion.regroup, pace_unit_id=cohesion.pace_unit_id,
    )
    planning_deadline = deadline or (perf_counter() + config.planning_budget_ms / 1_000)
    replacements: list[ActionIntent] = []

    for unit in members:
        existing = by_actor.get(unit.id)
        if unit.id in protected:
            continue
        if contact:
            replacements.append(ActionIntent(
                unit.id, False, ActionKind.WAIT, 690, "expedition_contact_hold",
            ))
            continue
        if existing is not None and existing.action is ActionKind.PICKUP_BEACON:
            if cohesion.pickup_ready:
                continue
            replacements.append(ActionIntent(
                unit.id, False, ActionKind.WAIT, 690, "expedition_pickup_waits_for_escort",
                target_cell=squad.target,
            ))
            continue
        if cohesion.regroup and unit.id == cohesion.pace_unit_id:
            replacements.append(ActionIntent(
                unit.id, False, ActionKind.WAIT, 690, "expedition_regroup_pace_hold",
            ))
            continue
        if unit.id in cohesion.hold_unit_ids:
            replacements.append(ActionIntent(
                unit.id, False, ActionKind.WAIT, 690, "expedition_cohesion_hold",
            ))
            continue

        target = slots[unit.id]
        if unit.position == target:
            replacements.append(ActionIntent(
                unit.id, False, ActionKind.WAIT, 680,
                "expedition_regroup_slot_hold" if cohesion.regroup else "expedition_formation_hold",
                target_cell=target,
            ))
            continue
        direction = plan_step(
            actor_id=unit.id,
            start=unit.position,
            goal=target,
            context=context,
            persistent_obstacles=memory.obstacles | memory.active_temporary_blocks(context.tick),
            reservations=reservations,
            deadline=planning_deadline,
            config=config,
            avoid_threats=True,
        )
        if direction is None:
            replacements.append(ActionIntent(
                unit.id, False, ActionKind.WAIT, 680, "expedition_formation_route_blocked",
                target_cell=target,
            ))
            continue
        replacements.append(ActionIntent(
            unit.id, False, ActionKind.MOVE, 680,
            "expedition_regroup" if cohesion.regroup else "expedition_formation_move",
            target_cell=target,
            direction=direction,
            reserved_cell=destination(unit.position, direction),
        ))

    if not replacements:
        return proposals
    replaced = {intent.actor_id for intent in replacements}
    return tuple(intent for intent in proposals if intent.actor_id not in replaced) + tuple(replacements)
