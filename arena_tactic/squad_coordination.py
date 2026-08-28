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
from .navigation import DIRECTIONS, destination, distance, plan_step, shot_range
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
    detached_unit_ids: Iterable[UUID] = (),
    maximum_lead: int = 1,
    cohesion_radius: int = 4,
    pickup_radius: int = 2,
    escort_quorum: int = 2,
) -> SquadCohesion:
    """Measure progress from active members, excluding detached recovery units."""
    detached = frozenset(detached_unit_ids)
    live = tuple(sorted(
        (
            unit for unit in units
            if unit.id in squad.member_ids and unit.id not in detached
        ),
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


def _intent_is_detached_for_recovery(intent: ActionIntent | None) -> bool:
    """Identify a member temporarily unavailable for Beacon formation pacing."""
    if intent is None:
        return False
    if intent.action is ActionKind.HEAL:
        return True
    return any(token in intent.reason for token in (
        "retreat", "heal", "critical", "emergency",
    ))


def squad_has_combat_contact(
    context: DecisionContext,
    members: Iterable[UnitView],
    intents: Iterable[ActionIntent],
) -> bool:
    """Return whether a squad is fighting or faces an immediate visible attack."""
    member_list = tuple(members)
    member_ids = {unit.id for unit in member_list}
    combat_reason_tokens = (
        "intercept_",
        "visible_threat",
        "enemy_approach",
    )
    if any(
        intent.actor_id in member_ids
        and (
            intent.action in {ActionKind.SHOOT, ActionKind.SWEEP}
            or any(token in intent.reason for token in combat_reason_tokens)
        )
        for intent in intents
    ):
        return True

    for enemy in context.enemies:
        if not isinstance(enemy, UnitView):
            continue
        for member in member_list:
            if enemy.unit_type is UnitType.VANGUARD:
                if distance(enemy.position, member.position) == 1:
                    return True
            elif (
                enemy.unit_type is UnitType.RANGER
                and shot_range(enemy.position, member.position, context.obstacle_cells) is not None
            ):
                return True
    return False


def _safe_slots(
    center: Position,
    units: tuple[UnitView, ...],
    context: DecisionContext,
    memory: AgentMemory,
    *,
    regroup: bool,
    pace_unit_id: UUID | None,
    anchor_unit_id: UUID | None,
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
            0 if unit.id == anchor_unit_id else 1,
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
    contact_holds: bool = True,
    reason_prefix: str = "expedition",
    allow_single_maneuver: bool = False,
) -> tuple[ActionIntent, ...]:
    """Replace independent Beacon movement with one complete squad order."""
    members = tuple(sorted(
        (unit for unit in context.units if unit.id in squad.member_ids),
        key=lambda unit: unit.id.bytes,
    ))
    if not members:
        return proposals

    by_actor = {intent.actor_id: intent for intent in proposals}
    if len(members) < 2 and not allow_single_maneuver:
        member = members[0]
        existing = by_actor.get(member.id)
        if existing is None or existing.action is not ActionKind.PICKUP_BEACON:
            return proposals
        replacement = ActionIntent(
            member.id,
            False,
            ActionKind.WAIT,
            690,
            f"{reason_prefix}_pickup_waits_for_escort",
            target_cell=squad.target,
        )
        return tuple(
            intent for intent in proposals if intent.actor_id != member.id
        ) + (replacement,)
    protected = {
        unit.id for unit in members if intent_is_squad_protected(by_actor.get(unit.id))
    }
    detached = {
        unit.id for unit in members
        if str(unit.id) in memory.retreating_unit_ids
        or _intent_is_detached_for_recovery(by_actor.get(unit.id))
    }
    contact = squad_has_combat_contact(context, members, proposals)
    cohesion = evaluate_squad_cohesion(
        squad, members, detached_unit_ids=detached,
    )
    active_members = tuple(unit for unit in members if unit.id not in detached)
    reservations = _movement_reservations(context, proposals, squad.member_ids)
    center = (
        next(unit.position for unit in members if unit.id == cohesion.pace_unit_id)
        if cohesion.regroup and cohesion.pace_unit_id is not None
        else squad.target
    )
    slots = _safe_slots(
        center, active_members, context, memory,
        regroup=cohesion.regroup,
        pace_unit_id=cohesion.pace_unit_id,
        anchor_unit_id=squad.anchor_unit_id,
    )
    planning_deadline = deadline or (perf_counter() + config.planning_budget_ms / 1_000)
    replacements: list[ActionIntent] = []

    for unit in members:
        existing = by_actor.get(unit.id)
        if unit.id in protected or unit.id in detached:
            continue
        if contact and contact_holds:
            replacements.append(ActionIntent(
                unit.id, False, ActionKind.WAIT, 690, f"{reason_prefix}_contact_hold",
            ))
            continue
        pickup_is_waiting = (
            existing is not None
            and "pickup_waits_for_escort" in existing.reason
        )
        if (
            existing is not None
            and existing.action is ActionKind.PICKUP_BEACON
        ) or pickup_is_waiting:
            if cohesion.pickup_ready:
                continue
            replacements.append(ActionIntent(
                unit.id, False, ActionKind.WAIT, 690, f"{reason_prefix}_pickup_waits_for_escort",
                target_cell=squad.target,
            ))
            continue
        if cohesion.regroup and unit.id == cohesion.pace_unit_id:
            replacements.append(ActionIntent(
                unit.id, False, ActionKind.WAIT, 690, f"{reason_prefix}_regroup_pace_hold",
            ))
            continue
        if unit.id in cohesion.hold_unit_ids:
            replacements.append(ActionIntent(
                unit.id, False, ActionKind.WAIT, 690, f"{reason_prefix}_cohesion_hold",
            ))
            continue

        target = slots[unit.id]
        if unit.position == target:
            replacements.append(ActionIntent(
                unit.id, False, ActionKind.WAIT, 680,
                f"{reason_prefix}_regroup_slot_hold"
                if cohesion.regroup else f"{reason_prefix}_formation_hold",
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
                unit.id, False, ActionKind.WAIT, 680, f"{reason_prefix}_formation_route_blocked",
                target_cell=target,
            ))
            continue
        replacements.append(ActionIntent(
            unit.id, False, ActionKind.MOVE, 680,
            f"{reason_prefix}_regroup" if cohesion.regroup else f"{reason_prefix}_formation_move",
            target_cell=target,
            direction=direction,
            reserved_cell=destination(unit.position, direction),
        ))

    if not replacements:
        return proposals
    replaced = {intent.actor_id for intent in replacements}
    return tuple(intent for intent in proposals if intent.actor_id not in replaced) + tuple(replacements)
