"""Vanguard combat and patrol planning."""

from __future__ import annotations

from typing import Iterable
from uuid import UUID

from arena_hero import BeaconStatus, CoreView, UnitType, UnitView

from ..context import DecisionContext
from ..memory import AgentMemory
from ..models import ActionIntent, ActionKind, AgentConfig, Position, ReservationTable, StrategicMode
from ..navigation import DIRECTIONS, destination, distance
from ..squads import SquadPlan, SquadType
from ..tactical_geometry import best_mineral_tank_cell
from .combat import (
    _combat_rosters,
    _combat_target,
    _escort_assignment,
    _enemy_can_attack_core,
    _intercept_target,
    vanguard_cell_score,
)
from .common import (
    _deploy_sidestep,
    _yield_cargo_delivery,
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
    squad_plan: SquadPlan | None = None,
) -> list[ActionIntent]:
    intents: list[ActionIntent] = []
    guard_slots = _guard_slots(context, memory)
    legacy_guards, legacy_guard_rangers, intercept_vanguards, _ = _combat_rosters(
        context, memory, config
    )
    if squad_plan is not None:
        guard_vanguards = squad_plan.ids_for(SquadType.BASE_DEFENSE, UnitType.VANGUARD)
        guard_rangers = squad_plan.ids_for(SquadType.BASE_DEFENSE, UnitType.RANGER)
        expedition_vanguards = squad_plan.ids_for(SquadType.EXPEDITION_BEACON, UnitType.VANGUARD)
        mining_vanguards = squad_plan.ids_for(SquadType.MINING_ESCORT, UnitType.VANGUARD)
        scout_vanguards = squad_plan.ids_for(SquadType.SCOUT_RECON, UnitType.VANGUARD)
        mining_rangers = squad_plan.ids_for(SquadType.MINING_ESCORT, UnitType.RANGER)
        intercept_vanguards = set(intercept_vanguards) - guard_vanguards
    else:
        guard_vanguards = legacy_guards
        guard_rangers = legacy_guard_rangers
        expedition_vanguards = {
            unit.id for unit in context.vanguards if unit.id not in guard_vanguards
        }
        mining_vanguards = set()
        scout_vanguards = set(expedition_vanguards)
        mining_rangers = set()
    intercept_enemy = _intercept_target(context, memory, config)
    patrol_roster = tuple(
        unit for unit in sorted(context.vanguards, key=lambda item: item.id.bytes)
        if unit.id in scout_vanguards
    )
    escort_combat = tuple(
        unit for unit in (*context.vanguards, *context.rangers)
        if unit.id in (mining_vanguards | mining_rangers)
    ) if squad_plan is not None else tuple(
        unit for unit in (*context.vanguards, *context.rangers)
        if unit.id not in guard_vanguards and unit.id not in guard_rangers
    )

    for index, vanguard in enumerate(sorted(context.vanguards, key=lambda unit: str(unit.id))):
        adjacent_by_cell: dict[Position, list[CoreView | UnitView]] = {}
        for enemy in context.enemies:
            if distance(vanguard.position, enemy.position) == 1:
                adjacent_by_cell.setdefault(enemy.position, []).append(enemy)
        best_cell = max(
            adjacent_by_cell,
            key=lambda cell: (vanguard_cell_score(adjacent_by_cell[cell]), cell),
            default=None,
        )
        urgent = best_cell is not None and _vanguard_urgent_cell(
            adjacent_by_cell[best_cell], context, memory
        )

        if vanguard.hp == 1 and not urgent:
            if _at_normal_core(vanguard, context) and heal_allowances.get(vanguard.id, 0) > 0:
                intents.append(_unit_heal_intent(vanguard, heal_allowances[vanguard.id]))
            else:
                intent = _return_to_core(
                    vanguard, context, memory, reservations, deadline, config,
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
            intents.append(_unit_heal_intent(vanguard, heal_cost) if heal_cost > 0 else _wait(vanguard, "healing_waits_for_resources"))
            continue
        if _unit_needs_retreat_heal(vanguard, memory, config) and not urgent:
            if _at_normal_core(vanguard, context):
                heal_cost = heal_allowances.get(vanguard.id, 0)
                intents.append(_unit_heal_intent(vanguard, heal_cost) if heal_cost > 0 else _wait(vanguard, "healing_waits_for_resources"))
            else:
                intent = _unit_retreat_to_core(vanguard, context, memory, reservations, deadline, config)
                intents.append(intent or _wait(vanguard, "unit_retreat_to_core_heal_blocked"))
            continue
        if best_cell is not None:
            direction = next(
                (direction for direction in DIRECTIONS if destination(vanguard.position, direction) == best_cell),
                None,
            )
            intents.append(ActionIntent(
                actor_id=vanguard.id,
                is_core=False,
                action=ActionKind.SWEEP,
                score=850 + vanguard_cell_score(adjacent_by_cell[best_cell]),
                reason="highest_value_adjacent_enemy_cell",
                target_cell=best_cell,
                direction=direction,
            ))
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

        target_enemy = _best_visible_enemy(vanguard, context, memory)
        if target_enemy is not None and mode is StrategicMode.DEFEND:
            intent = _move(
                vanguard, target_enemy.position, "intercept_visible_threat", 740,
                context=context, memory=memory, reservations=reservations,
                deadline=deadline, config=config,
            )
            _record_unit_task(memory, context, vanguard, kind="intercept", target=target_enemy.position, intent=intent)
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

        # Expedition behavior is now membership-driven, not "all non-guards in
        # BEACON mode". Manual squad assignment therefore has real semantics.
        at_beacon = vanguard.position == context.beacon.position and context.beacon.status is BeaconStatus.GROUND
        if vanguard.id in expedition_vanguards:
            if at_beacon:
                intents.append(ActionIntent(
                    actor_id=vanguard.id,
                    is_core=False,
                    action=ActionKind.PICKUP_BEACON,
                    score=780,
                    reason="preferred_vanguard_beacon_pickup",
                ))
            else:
                target_cell = context.beacon.position
                intent = _move(
                    vanguard, target_cell, "expedition_vanguard_to_beacon", 650,
                    context=context, memory=memory, reservations=reservations,
                    deadline=deadline, config=config,
                )
                if intent is None and context.core is not None:
                    intent = _deploy_sidestep(
                        vanguard, target_cell, context, memory,
                        reservations, "expedition_vanguard_sidestep", context.core.position,
                    )
                intents.append(intent or _wait(vanguard, "beacon_route_blocked"))
            memory.unit_tasks[str(vanguard.id)] = {
                "kind": "expedition_beacon",
                "target": list(context.beacon.position),
            }
            continue

        target_enemy = _best_visible_enemy(vanguard, context, memory)
        if target_enemy is not None and mode is StrategicMode.ATTACK:
            intent = _move(
                vanguard, target_enemy.position, "advance_on_high_value_enemy", 600,
                context=context, memory=memory, reservations=reservations,
                deadline=deadline, config=config,
            )
            intents.append(intent or _wait(vanguard, "enemy_approach_blocked"))
            continue

        cargo_yield = _yield_cargo_delivery(vanguard, context, memory, reservations)
        if cargo_yield is not None:
            _record_unit_task(
                memory, context, vanguard, kind="yield_cargo_delivery",
                target=cargo_yield.target_cell or vanguard.position, intent=cargo_yield,
            )
            intents.append(cargo_yield)
            continue

        if vanguard.id in mining_vanguards and context.core is not None:
            exploring_workers = [
                worker for worker in context.workers
                if (worker.cargo or 0) == 0 and distance(worker.position, context.core.position) > 2
            ]
            if exploring_workers:
                assignment = _escort_assignment(vanguard, escort_combat, exploring_workers, context.core)
                if assignment is not None:
                    _, escort_slot = assignment
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

        if vanguard.id in scout_vanguards:
            patrol_target = _combat_target(
                context.core, vanguard, patrol_roster.index(vanguard), len(patrol_roster),
                memory, config, role="patrol",
            ) if context.core is not None and vanguard in patrol_roster else None
            if patrol_target is not None:
                intent = _move(
                    vanguard, patrol_target, "patrol_outer_ring", 360,
                    context=context, memory=memory, reservations=reservations,
                    deadline=deadline, config=config,
                )
                if intent is None and context.core is not None:
                    intent = _deploy_sidestep(
                        vanguard, patrol_target, context, memory,
                        reservations, "patrol_outer_ring", context.core.position,
                    )
                _record_unit_task(memory, context, vanguard, kind="patrol", target=patrol_target, intent=intent)
                intents.append(intent or _wait(vanguard, "patrol_route_blocked"))
                continue

        # Base-defense members and overflow reserve hold the defense ring.
        ordered_guards = tuple(sorted(guard_vanguards, key=lambda unit_id: unit_id.bytes))
        guard_index = ordered_guards.index(vanguard.id) if vanguard.id in guard_vanguards else 0
        guard_target = guard_slots[guard_index % len(guard_slots)] if guard_slots else None
        if guard_target is not None and vanguard.position != guard_target:
            intent = _move(
                vanguard, guard_target, "hold_core_defense_ring", 300,
                context=context, memory=memory, reservations=reservations,
                deadline=deadline, config=config,
            )
            _record_unit_task(memory, context, vanguard, kind="core_guard", target=guard_target, intent=intent)
            intents.append(intent or _wait(vanguard, "guard_route_blocked"))
        else:
            if guard_target is not None:
                _record_unit_task(memory, context, vanguard, kind="core_guard", target=guard_target, intent=None)
            intents.append(_wait(vanguard, "holding_defense_ring"))
    return intents
