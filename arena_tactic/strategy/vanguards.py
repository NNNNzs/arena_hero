"""Vanguard combat and patrol planning."""

from __future__ import annotations

from typing import Iterable
from uuid import UUID

from arena_hero import BeaconStatus, CoreView, UnitType, UnitView

from ..context import DecisionContext
from ..memory import AgentMemory
from ..models import ActionIntent, ActionKind, AgentConfig, Position, ReservationTable, StrategicMode
from ..navigation import DIRECTIONS, destination, distance
from ..tactical_geometry import best_mineral_tank_cell
from .combat import (
    _combat_rosters,
    _combat_target,
    _enemy_can_attack_core,
    _intercept_target,
    vanguard_cell_score,
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
    _wait,
)
from .mode import (
    _hidden_attack_pressure,
    _hidden_attack_search_cell,
)
from .workers import _frontier_assignments

def _vanguard_urgent_cell(
    enemies: Iterable[CoreView | UnitView],
    context: DecisionContext,
    memory: AgentMemory,
) -> bool:
    return any(
        isinstance(enemy, CoreView)
        or (
            context.core is not None
            and _enemy_can_attack_core(enemy, context.core, memory.obstacles)
        )
        for enemy in enemies
    )


def _plan_vanguards(
    context: DecisionContext,
    memory: AgentMemory,
    mode: StrategicMode,
    reservations: ReservationTable,
    deadline: float,
    config: AgentConfig,
    heal_allowances: dict[UUID, int],
) -> list[ActionIntent]:
    intents: list[ActionIntent] = []
    beacon_vanguard = min(
        context.vanguards,
        key=lambda unit: (distance(unit.position, context.beacon.position), str(unit.id)),
        default=None,
    )
    guard_slots = _guard_slots(context, memory)
    guard_vanguards, _, intercept_vanguards, _ = _combat_rosters(
        context, memory, config
    )
    intercept_enemy = _intercept_target(context, memory, config)
    patrol_index = 0

    for index, vanguard in enumerate(sorted(context.vanguards, key=lambda unit: str(unit.id))):
        adjacent_by_cell: dict[Position, list[CoreView | UnitView]] = {}
        for enemy in context.enemies:
            if distance(vanguard.position, enemy.position) == 1:
                adjacent_by_cell.setdefault(enemy.position, []).append(enemy)
        best_cell = max(
            adjacent_by_cell,
            key=lambda cell: (
                vanguard_cell_score(adjacent_by_cell[cell]),
                cell,
            ),
            default=None,
        )
        urgent = (
            best_cell is not None
            and _vanguard_urgent_cell(adjacent_by_cell[best_cell], context, memory)
        )

        if vanguard.hp == 1 and not urgent:
            if _at_normal_core(vanguard, context) and heal_allowances.get(
                vanguard.id, 0
            ) > 0:
                heal_cost = heal_allowances[vanguard.id]
                intents.append(_unit_heal_intent(vanguard, heal_cost))
            else:
                intent = _return_to_core(
                    vanguard,
                    context,
                    memory,
                    reservations,
                    deadline,
                    config,
                    "critical_vanguard_retreat",
                )
                intents.append(intent or _wait(vanguard, "critical_retreat_blocked"))
            continue
        if (
            mode is StrategicMode.RECOVER
            and vanguard.hp < UNIT_MAX_HP[UnitType.VANGUARD]
            and _at_normal_core(vanguard, context)
            and not urgent
        ):
            heal_cost = heal_allowances.get(vanguard.id, 0)
            intents.append(
                _unit_heal_intent(vanguard, heal_cost)
                if heal_cost > 0
                else _wait(vanguard, "healing_waits_for_resources")
            )
            continue
        if best_cell is not None:
            direction = next(
                (
                    direction
                    for direction in DIRECTIONS
                    if destination(vanguard.position, direction) == best_cell
                ),
                None,
            )
            intents.append(
                ActionIntent(
                    actor_id=vanguard.id,
                    is_core=False,
                    action=ActionKind.SWEEP,
                    score=850 + vanguard_cell_score(adjacent_by_cell[best_cell]),
                    reason="highest_value_adjacent_enemy_cell",
                    target_cell=best_cell,
                    direction=direction,
                )
            )
            continue

        if _hidden_attack_pressure(context, memory) and context.core is not None:
            search_cell = _hidden_attack_search_cell(context.core, index, context.tick, config)
            intent = _move(
                vanguard, search_cell, "search_hidden_core_attacker", 720,
                context=context, memory=memory, reservations=reservations,
                deadline=deadline, config=config,
            )
            _record_unit_task(memory, context, vanguard, kind="defense_search", target=search_cell, intent=intent)
            intents.append(intent or _wait(vanguard, "hidden_attacker_search_blocked"))
            continue

        # Visible attackers take precedence over static DEFEND assignments.
        # This includes core guards: a melee unit holding the defense ring is
        # not useful against a Ranger that can keep firing from range.
        target_enemy = _best_visible_enemy(vanguard, context, memory)
        if target_enemy is not None and mode is StrategicMode.DEFEND:
            intent = _move(
                vanguard,
                target_enemy.position,
                "intercept_visible_threat",
                740,
                context=context,
                memory=memory,
                reservations=reservations,
                deadline=deadline,
                config=config,
            )
            _record_unit_task(
                memory,
                context,
                vanguard,
                kind="intercept",
                target=target_enemy.position,
                intent=intent,
            )
            intents.append(intent or _wait(vanguard, "visible_threat_route_blocked"))
            continue

        if mode is StrategicMode.DEFEND and context.core is not None:
            occupied = set(context.friendly_occupancy) | set(context.enemy_occupancy)
            occupied.discard(vanguard.position)
            tank_cell = best_mineral_tank_cell(
                resource_cells=context.resource_cells,
                enemy_cells=(enemy.position for enemy in context.enemies),
                core_cell=context.core.position,
                obstacles=memory.obstacles,
                occupied=occupied,
            )
            if tank_cell is not None:
                if vanguard.position == tank_cell:
                    _record_unit_task(memory, context, vanguard, kind="mineral_tank", target=tank_cell, intent=None)
                    intents.append(_wait(vanguard, "hold_vanguard_mineral_tank"))
                else:
                    intent = _move(
                        vanguard, tank_cell, "vanguard_mineral_tank", 710,
                        context=context, memory=memory, reservations=reservations,
                        deadline=deadline, config=config,
                    )
                    _record_unit_task(memory, context, vanguard, kind="mineral_tank", target=tank_cell, intent=intent)
                    intents.append(intent or _wait(vanguard, "mineral_tank_route_blocked"))
                continue

        if vanguard.id in intercept_vanguards and intercept_enemy is not None:
            intent = _move(
                vanguard, intercept_enemy.position, "intercept_approaching_core_threat", 680,
                context=context, memory=memory, reservations=reservations,
                deadline=deadline, config=config,
            )
            _record_unit_task(memory, context, vanguard, kind="intercept", target=intercept_enemy.position, intent=intent)
            intents.append(intent or _wait(vanguard, "intercept_route_blocked"))
            continue

        if mode is StrategicMode.BEACON and beacon_vanguard is vanguard:
            if (
                vanguard.position == context.beacon.position
                and context.beacon.status is BeaconStatus.GROUND
            ):
                intents.append(
                    ActionIntent(
                        actor_id=vanguard.id,
                        is_core=False,
                        action=ActionKind.PICKUP_BEACON,
                        score=780,
                        reason="preferred_vanguard_beacon_pickup",
                    )
                )
            else:
                intent = _move(
                    vanguard,
                    context.beacon.position,
                    "preferred_vanguard_to_beacon",
                    650,
                    context=context,
                    memory=memory,
                    reservations=reservations,
                    deadline=deadline,
                    config=config,
                )
                intents.append(intent or _wait(vanguard, "beacon_route_blocked"))
            memory.unit_tasks[str(vanguard.id)] = {
                "kind": "beacon",
                "target": list(context.beacon.position),
            }
            continue

        target_enemy = _best_visible_enemy(vanguard, context, memory)
        if target_enemy is not None and mode is StrategicMode.ATTACK:
            intent = _move(
                vanguard,
                target_enemy.position,
                "advance_on_high_value_enemy",
                600,
                context=context,
                memory=memory,
                reservations=reservations,
                deadline=deadline,
                config=config,
            )
            intents.append(intent or _wait(vanguard, "enemy_approach_blocked"))
            continue

        if vanguard.id not in guard_vanguards:
            # 伴随式护航优先：若有工兵在外探索，先锋伴随在前线前方 1~2 格开路
            if context.core is not None:
                core_pos = context.core.position
                exploring_workers = [w for w in context.workers if (w.cargo or 0) == 0 and distance(w.position, core_pos) > 2]
                if exploring_workers:
                    assigned_worker = min(exploring_workers, key=lambda w: distance(w.position, vanguard.position))
                    dx = 1 if assigned_worker.position[0] >= core_pos[0] else -1
                    dy = 1 if assigned_worker.position[1] >= core_pos[1] else -1
                    escort_slot = (assigned_worker.position[0] + dx, assigned_worker.position[1] + dy)
                    if escort_slot not in memory.obstacles:
                        intent = _move(
                            vanguard, escort_slot, "recon_squad_vanguard_screen", 450,
                            context=context, memory=memory, reservations=reservations,
                            deadline=deadline, config=config,
                        )
                        if intent is not None:
                            _record_unit_task(memory, context, vanguard, kind="recon_escort", target=escort_slot, intent=intent)
                            intents.append(intent)
                            continue

            patrol_target = _combat_target(
                context.core, patrol_index, context.tick, config.patrol_radius_min,
                config.patrol_radius_max, config.patrol_rotation_ticks,
            ) if context.core is not None else None
            patrol_index += 1
            if patrol_target is not None:
                intent = _move(
                    vanguard, patrol_target, "patrol_outer_ring", 360,
                    context=context, memory=memory, reservations=reservations,
                    deadline=deadline, config=config,
                )
                _record_unit_task(memory, context, vanguard, kind="patrol", target=patrol_target, intent=intent)
                intents.append(intent or _wait(vanguard, "patrol_route_blocked"))
                continue

        guard_index = tuple(sorted(guard_vanguards, key=lambda unit_id: unit_id.bytes)).index(vanguard.id)
        guard_target = guard_slots[guard_index % len(guard_slots)] if guard_slots else None
        if guard_target is not None and vanguard.position != guard_target:
            intent = _move(
                vanguard,
                guard_target,
                "hold_core_defense_ring",
                300,
                context=context,
                memory=memory,
                reservations=reservations,
                deadline=deadline,
                config=config,
            )
            _record_unit_task(memory, context, vanguard, kind="core_guard", target=guard_target, intent=intent)
            intents.append(intent or _wait(vanguard, "guard_route_blocked"))
        else:
            if guard_target is not None:
                _record_unit_task(memory, context, vanguard, kind="core_guard", target=guard_target, intent=None)
            intents.append(_wait(vanguard, "holding_defense_ring"))
    return intents
