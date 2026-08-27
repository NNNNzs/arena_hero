"""Tactical squad orchestration data structures and formation management."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from uuid import UUID

from arena_hero import UnitType, UnitView

from .context import DecisionContext
from .identity import entity_alias
from .memory import AgentMemory
from .models import AgentConfig, Position, StrategicMode
from .navigation import distance


class SquadType(str, Enum):
    EXPEDITION_BEACON = "EXPEDITION_BEACON"
    BASE_DEFENSE = "BASE_DEFENSE"
    MINING_ESCORT = "MINING_ESCORT"
    SCOUT_RECON = "SCOUT_RECON"


class SquadRole(str, Enum):
    POINT_GUARD = "POINT_GUARD"
    FIRE_SUPPORT = "FIRE_SUPPORT"
    MINER = "MINER"
    SCOUT = "SCOUT"
    DEFENDER = "DEFENDER"


SQUAD_ID_BY_TYPE = {
    SquadType.BASE_DEFENSE: "squad_base_defense",
    SquadType.EXPEDITION_BEACON: "squad_expedition_beacon",
    SquadType.MINING_ESCORT: "squad_mining_escort",
    SquadType.SCOUT_RECON: "squad_scout_recon",
}
SQUAD_TYPE_BY_ID = {value: key for key, value in SQUAD_ID_BY_TYPE.items()}


@dataclass(frozen=True, slots=True)
class SquadMember:
    unit_id: UUID
    unit_type: UnitType
    role: SquadRole


@dataclass(frozen=True, slots=True)
class Squad:
    squad_id: str
    squad_type: SquadType
    target: Position
    members: tuple[SquadMember, ...]
    anchor_unit_id: UUID | None = None

    @property
    def member_ids(self) -> set[UUID]:
        return {member.unit_id for member in self.members}

    @property
    def vanguards(self) -> tuple[UUID, ...]:
        return tuple(member.unit_id for member in self.members if member.unit_type is UnitType.VANGUARD)

    @property
    def rangers(self) -> tuple[UUID, ...]:
        return tuple(member.unit_id for member in self.members if member.unit_type is UnitType.RANGER)

    @property
    def workers(self) -> tuple[UUID, ...]:
        return tuple(member.unit_id for member in self.members if member.unit_type is UnitType.WORKER)


@dataclass(slots=True)
class SquadPlan:
    squads: dict[str, Squad] = field(default_factory=dict)
    unit_to_squad: dict[UUID, tuple[str, SquadRole]] = field(default_factory=dict)

    def add_squad(self, squad: Squad) -> None:
        self.squads[squad.squad_id] = squad
        for member in squad.members:
            self.unit_to_squad[member.unit_id] = (squad.squad_id, member.role)

    def get_unit_squad(self, unit_id: UUID) -> tuple[Squad, SquadRole] | None:
        mapping = self.unit_to_squad.get(unit_id)
        if mapping is None:
            return None
        squad_id, role = mapping
        squad = self.squads.get(squad_id)
        return (squad, role) if squad is not None else None

    def unit_is(self, unit_id: UUID, squad_type: SquadType) -> bool:
        assignment = self.get_unit_squad(unit_id)
        return assignment is not None and assignment[0].squad_type is squad_type

    def ids_for(self, squad_type: SquadType, unit_type: UnitType | None = None) -> set[UUID]:
        squad = self.squads.get(SQUAD_ID_BY_TYPE[squad_type])
        if squad is None:
            return set()
        return {
            member.unit_id
            for member in squad.members
            if unit_type is None or member.unit_type is unit_type
        }


def _role(unit: UnitView, squad_type: SquadType) -> SquadRole:
    if unit.unit_type is UnitType.WORKER:
        return SquadRole.SCOUT if squad_type is SquadType.SCOUT_RECON else SquadRole.MINER
    if squad_type is SquadType.BASE_DEFENSE:
        return SquadRole.DEFENDER
    if unit.unit_type is UnitType.RANGER:
        return SquadRole.FIRE_SUPPORT
    return SquadRole.SCOUT if squad_type is SquadType.SCOUT_RECON else SquadRole.POINT_GUARD


def _manual_members(
    units: tuple[UnitView, ...], memory: AgentMemory, squad_type: SquadType
) -> list[UnitView]:
    squad_id = SQUAD_ID_BY_TYPE[squad_type]
    return [
        unit
        for unit in units
        if memory.manual_squad_assignments.get(entity_alias(unit.id) or "") == squad_id
    ]


def build_squad_plan(
    context: DecisionContext,
    memory: AgentMemory,
    config: AgentConfig,
) -> SquadPlan:
    """Build one authoritative, stable four-squad plan for the current Tick.

    Manual membership wins over automatic composition, but it does not bypass
    planner safety rules such as critical-HP retreat or immediate enemy fire.
    Automatic expedition membership is activated only while BEACON is the
    strategic mode; otherwise those mobile units remain available for mining
    escort, scouting, or defensive reserve.
    """
    plan = SquadPlan()
    if context.core is None:
        return plan

    units = tuple(sorted(context.units, key=lambda unit: unit.id.bytes))
    assigned: set[UUID] = set()
    members: dict[SquadType, list[UnitView]] = {kind: [] for kind in SquadType}

    # Manual assignment is the first and strongest membership decision.
    for squad_type in SquadType:
        for unit in _manual_members(units, memory, squad_type):
            if unit.id not in assigned:
                members[squad_type].append(unit)
                assigned.add(unit.id)

    def available(unit_type: UnitType) -> list[UnitView]:
        return [
            unit for unit in units
            if unit.unit_type is unit_type and unit.id not in assigned
        ]

    beacon_state = memory.objective_states.get("beacon", {})
    beacon_stage = beacon_state.get("stage")
    carrier_alias = beacon_state.get("carrier_alias")
    retained_escort_aliases = {
        str(alias) for alias in beacon_state.get("escort_aliases", ())
        if isinstance(alias, str)
    }

    # Core defense gets its configured baseline first.
    for unit_type, count in (
        (UnitType.VANGUARD, config.core_guard_vanguards),
        (UnitType.RANGER, config.core_guard_rangers),
    ):
        chosen = available(unit_type)[:max(0, count)]
        members[SquadType.BASE_DEFENSE].extend(chosen)
        assigned.update(unit.id for unit in chosen)

    # A secured carrier stays in the Core defense envelope.  It is never sent
    # back out with the miners merely because its former escorts change jobs.
    if beacon_stage == "SECURE" and isinstance(carrier_alias, str):
        carrier = next(
            (
                unit for unit in units
                if unit.id not in assigned and entity_alias(unit.id) == carrier_alias
            ),
            None,
        )
        if carrier is not None:
            members[SquadType.BASE_DEFENSE].append(carrier)
            assigned.add(carrier.id)

    # Only a live BEACON campaign automatically consumes expedition strength.
    if memory.last_mode is StrategicMode.BEACON:
        for unit_type, count in (
            (UnitType.VANGUARD, config.expedition_vanguards),
            (UnitType.RANGER, config.expedition_rangers),
        ):
            chosen = sorted(
                available(unit_type),
                key=lambda unit: (
                    0 if entity_alias(unit.id) in retained_escort_aliases else 1,
                    distance(unit.position, context.beacon.position),
                    unit.id.bytes,
                ),
            )[:max(0, count)]
            members[SquadType.EXPEDITION_BEACON].extend(chosen)
            assigned.update(unit.id for unit in chosen)

    # Workers are economic by default unless the operator explicitly moved
    # them to a different squad. Combat escorts are a distinct config surface.
    mining_workers = available(UnitType.WORKER)
    members[SquadType.MINING_ESCORT].extend(mining_workers)
    assigned.update(unit.id for unit in mining_workers)
    mining_anchor = min(
        mining_workers,
        key=lambda worker: (-distance(worker.position, context.core.position), worker.id.bytes),
        default=None,
    )
    escort_target = mining_anchor.position if mining_anchor is not None else context.core.position

    # After exfil, the surviving expedition escorts become the expanded mining
    # security screen.  The carrier remains at home; only combat escorts are
    # transferred into MINING_ESCORT.
    if beacon_stage == "SECURE":
        secured_escorts = [
            unit for unit in units
            if unit.id not in assigned
            and unit.unit_type in (UnitType.VANGUARD, UnitType.RANGER)
            and entity_alias(unit.id) in retained_escort_aliases
            and entity_alias(unit.id) != carrier_alias
        ]
        members[SquadType.MINING_ESCORT].extend(secured_escorts)
        assigned.update(unit.id for unit in secured_escorts)
    for unit_type, count in (
        (UnitType.VANGUARD, config.mining_escort_vanguards),
        (UnitType.RANGER, config.mining_escort_rangers),
    ):
        chosen = sorted(
            available(unit_type),
            key=lambda unit: (distance(unit.position, escort_target), unit.id.bytes),
        )[:max(0, count)]
        members[SquadType.MINING_ESCORT].extend(chosen)
        assigned.update(unit.id for unit in chosen)

    # Scout strength is explicitly configurable. Remaining combat units are
    # defensive reserve instead of silently swelling the expedition roster.
    for unit_type, count in (
        (UnitType.VANGUARD, config.scout_vanguards),
        (UnitType.RANGER, config.scout_rangers),
    ):
        chosen = available(unit_type)[:max(0, count)]
        members[SquadType.SCOUT_RECON].extend(chosen)
        assigned.update(unit.id for unit in chosen)

    reserve = [unit for unit in units if unit.id not in assigned]
    members[SquadType.BASE_DEFENSE].extend(reserve)
    assigned.update(unit.id for unit in reserve)

    targets = {
        SquadType.BASE_DEFENSE: context.core.position,
        SquadType.EXPEDITION_BEACON: context.beacon.position,
        SquadType.MINING_ESCORT: escort_target,
        SquadType.SCOUT_RECON: context.core.position,
    }
    anchors = {
        SquadType.BASE_DEFENSE: context.core.id,
        SquadType.EXPEDITION_BEACON: next(
            (unit.id for unit in members[SquadType.EXPEDITION_BEACON] if unit.unit_type is UnitType.VANGUARD),
            context.core.id,
        ),
        SquadType.MINING_ESCORT: mining_anchor.id if mining_anchor is not None else context.core.id,
        SquadType.SCOUT_RECON: context.core.id,
    }

    # Always emit all four stable squad IDs so Dashboard membership and manual
    # reassignment options never depend on transient roster composition.
    for squad_type in (
        SquadType.BASE_DEFENSE,
        SquadType.EXPEDITION_BEACON,
        SquadType.MINING_ESCORT,
        SquadType.SCOUT_RECON,
    ):
        plan.add_squad(Squad(
            squad_id=SQUAD_ID_BY_TYPE[squad_type],
            squad_type=squad_type,
            target=targets[squad_type],
            members=tuple(
                SquadMember(unit.id, unit.unit_type, _role(unit, squad_type))
                for unit in members[squad_type]
            ),
            anchor_unit_id=anchors[squad_type],
        ))
    return plan
