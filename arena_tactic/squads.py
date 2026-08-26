"""Tactical squad orchestration data structures and formation management."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping, Sequence
from uuid import UUID

from arena_hero import CoreView, UnitType, UnitView

from .context import DecisionContext
from .memory import AgentMemory
from .models import AgentConfig, Position
from .navigation import distance, DIRECTIONS, destination


class SquadType(str, Enum):
    EXPEDITION_BEACON = "EXPEDITION_BEACON"   # 远征夺取信标
    BASE_DEFENSE = "BASE_DEFENSE"             # 基地防御圈
    MINING_ESCORT = "MINING_ESCORT"           # 矿区采矿/护航
    SCOUT_RECON = "SCOUT_RECON"               # 迷雾前沿探索


class SquadRole(str, Enum):
    POINT_GUARD = "POINT_GUARD"       # 尖刀先锋（突击、卡位、拾取）
    FIRE_SUPPORT = "FIRE_SUPPORT"     # 火力支援（游侠侧翼或后排掩护）
    MINER = "MINER"                   # 采矿工兵
    SCOUT = "SCOUT"                   # 侦察兵
    DEFENDER = "DEFENDER"             # 守备驻扎


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
        return {m.unit_id for m in self.members}

    @property
    def vanguards(self) -> tuple[UUID, ...]:
        return tuple(m.unit_id for m in self.members if m.unit_type is UnitType.VANGUARD)

    @property
    def rangers(self) -> tuple[UUID, ...]:
        return tuple(m.unit_id for m in self.members if m.unit_type is UnitType.RANGER)

    @property
    def workers(self) -> tuple[UUID, ...]:
        return tuple(m.unit_id for m in self.members if m.unit_type is UnitType.WORKER)


@dataclass(slots=True)
class SquadPlan:
    squads: dict[str, Squad] = field(default_factory=dict)
    unit_to_squad: dict[UUID, tuple[str, SquadRole]] = field(default_factory=dict)

    def add_squad(self, squad: Squad) -> None:
        self.squads[squad.squad_id] = squad
        for m in squad.members:
            self.unit_to_squad[m.unit_id] = (squad.squad_id, m.role)

    def get_unit_squad(self, unit_id: UUID) -> tuple[Squad, SquadRole] | None:
        mapping = self.unit_to_squad.get(unit_id)
        if not mapping:
            return None
        squad_id, role = mapping
        squad = self.squads.get(squad_id)
        if not squad:
            return None
        return squad, role


def build_squad_plan(
    context: DecisionContext,
    memory: AgentMemory,
    config: AgentConfig,
) -> SquadPlan:
    """Organize combat and civilian units into cohesive tactical squads."""
    plan = SquadPlan()
    if context.core is None:
        return plan

    core_pos = context.core.position
    vanguards = sorted(context.vanguards, key=lambda u: u.id.bytes)
    rangers = sorted(context.rangers, key=lambda u: u.id.bytes)
    workers = sorted(context.workers, key=lambda u: u.id.bytes)

    # 1. 基地核心防卫编组（Base Defense Squad）
    # 固定分配核心守备先锋与守备游侠
    base_guard_vanguards = vanguards[:config.core_guard_vanguards]
    base_guard_rangers = rangers[:config.core_guard_rangers]
    
    defense_members: list[SquadMember] = [
        SquadMember(u.id, UnitType.VANGUARD, SquadRole.DEFENDER)
        for u in base_guard_vanguards
    ] + [
        SquadMember(u.id, UnitType.RANGER, SquadRole.DEFENDER)
        for u in base_guard_rangers
    ]
    if defense_members:
        plan.add_squad(
            Squad(
                squad_id="squad_base_defense",
                squad_type=SquadType.BASE_DEFENSE,
                target=core_pos,
                members=tuple(defense_members),
                anchor_unit_id=context.core.id,
            )
        )

    # 机动兵力
    avail_vanguards = [u for u in vanguards if u not in base_guard_vanguards]
    avail_rangers = [u for u in rangers if u not in base_guard_rangers]

    # 2. 远征信标编组（Expedition Beacon Strike Group）
    # 当信标有效且有可用机动兵力时，选拔先锋和游侠组成突击大队
    beacon_target = context.beacon.position
    if beacon_target is not None:
        # 按离信标距离由近及远排序，取先锋与游侠
        expedition_vg = sorted(
            avail_vanguards,
            key=lambda u: distance(u.position, beacon_target),
        )[:max(2, config.intercept_vanguards)]
        
        # 挑选游侠提供伴随火力支援
        expedition_ra = sorted(
            avail_rangers,
            key=lambda u: distance(u.position, beacon_target),
        )[:max(2, config.intercept_rangers)]

        if expedition_vg:
            anchor = expedition_vg[0].id
            exp_members: list[SquadMember] = [
                SquadMember(u.id, UnitType.VANGUARD, SquadRole.POINT_GUARD)
                for u in expedition_vg
            ] + [
                SquadMember(u.id, UnitType.RANGER, SquadRole.FIRE_SUPPORT)
                for u in expedition_ra
            ]
            plan.add_squad(
                Squad(
                    squad_id="squad_expedition_beacon",
                    squad_type=SquadType.EXPEDITION_BEACON,
                    target=beacon_target,
                    members=tuple(exp_members),
                    anchor_unit_id=anchor,
                )
            )
            # 扣除已加入远征队的兵力
            avail_vanguards = [u for u in avail_vanguards if u not in expedition_vg]
            avail_rangers = [u for u in avail_rangers if u not in expedition_ra]

    # 3. 矿区采矿与伴随护航编组（Mining & Escort Squads）
    # 针对距离基地 > 2 格在外采矿的工兵，分配剩余机动兵力就近护航
    remote_workers = [
        w for w in workers
        if distance(w.position, core_pos) > 2
    ]
    for idx, worker in enumerate(remote_workers):
        escort_members = [SquadMember(worker.id, UnitType.WORKER, SquadRole.MINER)]
        # 为该工人分配 1 个最近的游侠或先锋伴随掩护
        if avail_rangers:
            closest_ra = min(avail_rangers, key=lambda r: distance(r.position, worker.position))
            escort_members.append(SquadMember(closest_ra.id, UnitType.RANGER, SquadRole.FIRE_SUPPORT))
            avail_rangers.remove(closest_ra)
        elif avail_vanguards:
            closest_vg = min(avail_vanguards, key=lambda v: distance(v.position, worker.position))
            escort_members.append(SquadMember(closest_vg.id, UnitType.VANGUARD, SquadRole.POINT_GUARD))
            avail_vanguards.remove(closest_vg)

        plan.add_squad(
            Squad(
                squad_id=f"squad_mining_escort_{idx + 1}",
                squad_type=SquadType.MINING_ESCORT,
                target=worker.position,
                members=tuple(escort_members),
                anchor_unit_id=worker.id,
            )
        )

    # 4. 剩余单位归入迷雾探索/自由巡逻侦察（Scout Recon）
    scout_members: list[SquadMember] = [
        SquadMember(u.id, UnitType.VANGUARD, SquadRole.SCOUT)
        for u in avail_vanguards
    ] + [
        SquadMember(u.id, UnitType.RANGER, SquadRole.SCOUT)
        for u in avail_rangers
    ] + [
        SquadMember(w.id, UnitType.WORKER, SquadRole.MINER)
        for w in workers
        if w not in remote_workers
    ]
    if scout_members:
        plan.add_squad(
            Squad(
                squad_id="squad_scout_recon",
                squad_type=SquadType.SCOUT_RECON,
                target=core_pos,
                members=tuple(scout_members),
                anchor_unit_id=context.core.id,
            )
        )

    return plan
