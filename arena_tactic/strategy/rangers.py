"""Ranger targeting, firing, and patrol planning."""

from __future__ import annotations

from uuid import UUID

from arena_hero import BeaconStatus, CoreView, UnitType, UnitView

from ..context import DecisionContext
from ..memory import AgentMemory
from ..models import ActionIntent, ActionKind, AgentConfig, Position, ReservationTable, StrategicMode
from ..navigation import DIRECTIONS, destination, distance, shot_range
from ..tactical_geometry import shadow_fire_advantage
from .combat import (
    _combat_rosters,
    _combat_target,
    _enemy_can_attack_core,
    _intercept_target,
    ranger_target_score,
)
from .common import (
    UNIT_MAX_HP,
    _at_normal_core,
    _best_visible_enemy,
    _guard_slots,
    _move,
    _record_unit_task,
    _return_to_core,
    _unit_heal_intent,
    _unit_needs_retreat_heal,
    _unit_retreat_to_core,
    _wait,
)
from .mode import (
    _hidden_attack_pressure,
    _hidden_attack_search_cell,
)
from .workers import _frontier_assignments

def _ranger_attack_is_urgent(
    enemy: CoreView | UnitView,
    context: DecisionContext,
    memory: AgentMemory,
) -> bool:
    remaining = enemy.hp + (enemy.shield if isinstance(enemy, CoreView) else 0)
    return (
        remaining <= 1
        or (
            context.core is not None
            and _enemy_can_attack_core(enemy, context.core, memory.obstacles)
        )
    )


def _ranger_staging_cell(
    ranger: UnitView,
    target: CoreView | UnitView,
    context: DecisionContext,
    memory: AgentMemory,
) -> Position:
    tx, ty = target.position
    offsets = (
        (0, -3), (0, -2), (0, 2), (0, 3),
        (-3, 0), (-2, 0), (2, 0), (3, 0),
        (-2, -2), (-2, 2), (2, -2), (2, 2),
        (-3, -3), (-3, 3), (3, -3), (3, 3),
    )
    candidates = [
        (tx + dx, ty + dy)
        for dx, dy in offsets
        if (tx + dx, ty + dy) not in memory.obstacles
        and (tx + dx, ty + dy) not in context.enemy_occupancy
    ]
    return min(
        candidates,
        key=lambda cell: (
            -shadow_fire_advantage(cell, target, memory.obstacles),
            distance(ranger.position, cell),
            cell,
        ),
        default=target.position,
    )


def _plan_rangers(
    context: DecisionContext,
    memory: AgentMemory,
    mode: StrategicMode,
    reservations: ReservationTable,
    deadline: float,
    config: AgentConfig,
    heal_allowances: dict[UUID, int],
) -> list[ActionIntent]:
    intents: list[ActionIntent] = []
    guard_slots = _guard_slots(context, memory)
    _, guard_rangers, _, intercept_rangers = _combat_rosters(context, memory, config)
    intercept_enemy = _intercept_target(context, memory, config)
    hunter_index = 0
    
    # 局部集火与过杀保护统计: 记录本回合敌人已分配受击伤害
    targeted_damage: dict[UUID, int] = {}

    for index, ranger in enumerate(sorted(context.rangers, key=lambda unit: str(unit.id))):
        shootable = [
            enemy
            for enemy in context.enemies
            if shot_range(ranger.position, enemy.position, memory.obstacles) is not None
        ]
        
        # 过滤掉已经受到足够致命伤害的目标（过杀保护）
        effective_shootable = []
        for enemy in shootable:
            enemy_hp = enemy.hp + (enemy.shield if isinstance(enemy, CoreView) else 0)
            if targeted_damage.get(enemy.id, 0) < enemy_hp:
                effective_shootable.append(enemy)
        if not effective_shootable and shootable:
            effective_shootable = shootable  # 若全都被预定，则允许补刀

        target = min(
            effective_shootable,
            key=lambda enemy: (
                -ranger_target_score(ranger, enemy, context, memory),
                enemy.id.bytes,
            ),
            default=None,
        )
        urgent = target is not None and _ranger_attack_is_urgent(
            target, context, memory
        )
        if ranger.hp == 1 and not urgent:
            if _at_normal_core(ranger, context) and heal_allowances.get(
                ranger.id, 0
            ) > 0:
                heal_cost = heal_allowances[ranger.id]
                intents.append(_unit_heal_intent(ranger, heal_cost))
            else:
                intent = _return_to_core(
                    ranger,
                    context,
                    memory,
                    reservations,
                    deadline,
                    config,
                    "critical_ranger_retreat",
                )
                intents.append(intent or _wait(ranger, "critical_retreat_blocked"))
            continue
        if _unit_needs_retreat_heal(ranger, memory, config) and not urgent:
            if _at_normal_core(ranger, context):
                heal_cost = heal_allowances.get(ranger.id, 0)
                intents.append(
                    _unit_heal_intent(ranger, heal_cost)
                    if heal_cost > 0
                    else _wait(ranger, "healing_waits_for_resources")
                )
            else:
                intent = _unit_retreat_to_core(
                    ranger, context, memory, reservations, deadline, config
                )
                intents.append(intent or _wait(ranger, "unit_retreat_to_core_heal_blocked"))
            continue
        if target is not None:
            # 记录集火伤害 (Ranger 单次伤害为 1)
            targeted_damage[target.id] = targeted_damage.get(target.id, 0) + 1
            intents.append(
                ActionIntent(
                    actor_id=ranger.id,
                    is_core=False,
                    action=ActionKind.SHOOT,
                    score=850 + ranger_target_score(ranger, target, context, memory),
                    reason="highest_scoring_legal_ranger_target",
                    target_id=target.id,
                    target_cell=target.position,
                )
            )
            continue

        if _hidden_attack_pressure(context, memory) and context.core is not None:
            search_cell = _hidden_attack_search_cell(context.core, index, context.tick, config)
            intent = _move(
                ranger, search_cell, "search_hidden_core_attacker", 720,
                context=context, memory=memory, reservations=reservations,
                deadline=deadline, config=config,
            )
            _record_unit_task(memory, context, ranger, kind="defense_search", target=search_cell, intent=intent)
            intents.append(intent or _wait(ranger, "hidden_attacker_search_blocked"))
            continue

        if ranger.id in intercept_rangers and intercept_enemy is not None:
            staging = _ranger_staging_cell(ranger, intercept_enemy, context, memory)
            intent = _move(
                ranger,
                staging,
                "intercept_ranger_firing_line",
                620,
                context=context,
                memory=memory,
                reservations=reservations,
                deadline=deadline,
                config=config,
            )
            _record_unit_task(
                memory,
                context,
                ranger,
                kind="intercept",
                target=staging,
                intent=intent,
            )
            intents.append(intent or _wait(ranger, "intercept_firing_route_blocked"))
            continue

        target_enemy = _best_visible_enemy(ranger, context, memory)
        if target_enemy is not None and mode in (
            StrategicMode.DEFEND,
            StrategicMode.ATTACK,
        ):
            staging = _ranger_staging_cell(
                ranger, target_enemy, context, memory
            )
            intent = _move(
                ranger,
                staging,
                "ranger_seek_legal_firing_line",
                580,
                context=context,
                memory=memory,
                reservations=reservations,
                deadline=deadline,
                config=config,
            )
            intents.append(intent or _wait(ranger, "firing_route_blocked"))
            continue

        if ranger.id not in guard_rangers:
            # 伴随式火力掩护优先：若有工兵在外探索，游侠伴随在工兵侧翼 2 格射程位
            if context.core is not None:
                core_pos = context.core.position
                exploring_workers = [w for w in context.workers if (w.cargo or 0) == 0 and distance(w.position, core_pos) > 2]
                if exploring_workers:
                    assigned_worker = min(exploring_workers, key=lambda w: distance(w.position, ranger.position))
                    # 站在工兵侧翼 2 格处提供火力掩护
                    escort_slot = (assigned_worker.position[0] + 1, assigned_worker.position[1] - 1)
                    if escort_slot not in memory.obstacles:
                        intent = _move(
                            ranger, escort_slot, "recon_squad_ranger_flank", 450,
                            context=context, memory=memory, reservations=reservations,
                            deadline=deadline, config=config,
                        )
                        if intent is not None:
                            _record_unit_task(memory, context, ranger, kind="recon_escort", target=escort_slot, intent=intent)
                            intents.append(intent)
                            continue

            hunter_target = _combat_target(
                context.core, hunter_index, context.tick, config.hunter_radius_min,
                config.hunter_radius_max, config.patrol_rotation_ticks,
            ) if context.core is not None else None
            hunter_index += 1
            if hunter_target is not None:
                intent = _move(
                    ranger, hunter_target, "hunter_forward_recon", 420,
                    context=context, memory=memory, reservations=reservations,
                    deadline=deadline, config=config,
                )
                _record_unit_task(memory, context, ranger, kind="hunter", target=hunter_target, intent=intent)
                intents.append(intent or _wait(ranger, "hunter_route_blocked"))
                continue

        guard_index = tuple(sorted(guard_rangers, key=lambda unit_id: unit_id.bytes)).index(ranger.id)
        guard_target = guard_slots[(guard_index + len(context.vanguards)) % len(guard_slots)] if guard_slots else None
        if guard_target is not None and ranger.position != guard_target:
            intent = _move(
                ranger,
                guard_target,
                "ranger_hold_defense_ring",
                280,
                context=context,
                memory=memory,
                reservations=reservations,
                deadline=deadline,
                config=config,
            )
            _record_unit_task(memory, context, ranger, kind="core_guard", target=guard_target, intent=intent)
            intents.append(intent or _wait(ranger, "guard_route_blocked"))
        else:
            if guard_target is not None:
                _record_unit_task(memory, context, ranger, kind="core_guard", target=guard_target, intent=None)
            intents.append(_wait(ranger, "holding_defense_ring"))
    return intents
