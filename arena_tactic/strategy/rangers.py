"""Ranger targeting, firing, and patrol planning."""

from __future__ import annotations

from uuid import UUID

from arena_hero import CoreView, UnitType, UnitView

from ..context import DecisionContext
from ..memory import AgentMemory
from ..models import ActionIntent, ActionKind, AgentConfig, Position, ReservationTable
from ..navigation import distance, shot_range
from ..squads import SquadPlan, SquadType
from ..tactical_geometry import shadow_fire_advantage
from .combat import (
    _combat_rosters,
    _combat_target,
    _escort_assignment,
    _enemy_can_attack_core,
    _intercept_target,
    ranger_target_score,
)
from .common import (
    _deploy_sidestep,
    _distant_retreat_fallback_intent,
    _yield_cargo_delivery,
    _at_normal_core,
    _best_visible_enemy,
    _critical_retreat_sidestep,
    _evacuate_doorstep_intent,
    _firing_line_sidestep,
    _guard_slots,
    _move,
    _record_unit_task,
    _return_to_core,
    _unit_heal_intent,
    _unit_needs_retreat_heal,
    _unit_retreat_to_core,
    _wait,
)
from .mode import _hidden_attack_pressure, _hidden_attack_search_cell


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
    reservations: ReservationTable,
    deadline: float,
    config: AgentConfig,
    heal_allowances: dict[UUID, int],
    squad_plan: SquadPlan | None = None,
) -> list[ActionIntent]:
    intents: list[ActionIntent] = []
    guard_slots = _guard_slots(context, memory)
    legacy_guard_vanguards, legacy_guard_rangers, _, intercept_rangers = _combat_rosters(
        context, memory, config
    )
    if squad_plan is not None:
        guard_vanguards = squad_plan.ids_for(SquadType.BASE_DEFENSE, UnitType.VANGUARD)
        guard_rangers = squad_plan.ids_for(SquadType.BASE_DEFENSE, UnitType.RANGER)
        expedition_rangers = squad_plan.ids_for(SquadType.EXPEDITION_BEACON, UnitType.RANGER)
        mining_vanguards = squad_plan.ids_for(SquadType.MINING_ESCORT, UnitType.VANGUARD)
        mining_rangers = squad_plan.ids_for(SquadType.MINING_ESCORT, UnitType.RANGER)
        scout_rangers = squad_plan.ids_for(SquadType.SCOUT_RECON, UnitType.RANGER)
        intercept_rangers = set(intercept_rangers) - guard_rangers
    else:
        guard_vanguards = legacy_guard_vanguards
        guard_rangers = legacy_guard_rangers
        expedition_rangers = {unit.id for unit in context.rangers if unit.id not in guard_rangers}
        mining_vanguards = set()
        mining_rangers = set()
        scout_rangers = set(expedition_rangers)

    intercept_enemy = _intercept_target(context, memory, config)
    hunter_roster = tuple(
        unit for unit in sorted(context.rangers, key=lambda item: item.id.bytes)
        if unit.id in scout_rangers
    )
    escort_combat = tuple(
        unit for unit in (*context.vanguards, *context.rangers)
        if unit.id in (mining_vanguards | mining_rangers)
    ) if squad_plan is not None else tuple(
        unit for unit in (*context.vanguards, *context.rangers)
        if unit.id not in guard_vanguards and unit.id not in guard_rangers
    )

    targeted_damage: dict[UUID, int] = {}

    for index, ranger in enumerate(sorted(context.rangers, key=lambda unit: str(unit.id))):
        shootable = [
            enemy
            for enemy in context.enemies
            if shot_range(ranger.position, enemy.position, memory.obstacles) is not None
        ]
        effective_shootable = []
        for enemy in shootable:
            enemy_hp = enemy.hp + (enemy.shield if isinstance(enemy, CoreView) else 0)
            if targeted_damage.get(enemy.id, 0) < enemy_hp:
                effective_shootable.append(enemy)
        if not effective_shootable and shootable:
            effective_shootable = shootable

        target = min(
            effective_shootable,
            key=lambda enemy: (
                -ranger_target_score(ranger, enemy, context, memory, config),
                enemy.id.bytes,
            ),
            default=None,
        )
        urgent = target is not None and _ranger_attack_is_urgent(target, context, memory)

        if ranger.hp == 1 and not urgent:
            if _at_normal_core(ranger, context) and heal_allowances.get(ranger.id, 0) > 0:
                intents.append(_unit_heal_intent(ranger, heal_allowances[ranger.id]))
            else:
                intent = _return_to_core(
                    ranger, context, memory, reservations, deadline, config,
                    "critical_ranger_retreat",
                )
                if intent is None:
                    intent = _evacuate_doorstep_intent(
                        ranger, context, memory, reservations, "yield_doorstep_critical_retreat",
                        max_radius=max(3, config.cargo_delivery_yield_radius),
                    )
                if intent is None:
                    intent = _critical_retreat_sidestep(
                        ranger, context, memory, reservations,
                        "critical_retreat_sidestep",
                    )
                chosen_intent = intent or _wait(ranger, "critical_retreat_blocked")
                intents.append(chosen_intent)
                core_pos = context.core.position if context.core else ranger.position
                _record_unit_task(memory, context, ranger, kind="critical_ranger_retreat", target=core_pos, intent=chosen_intent)
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
                if intent is None:
                    intent = _evacuate_doorstep_intent(
                        ranger, context, memory, reservations, "yield_doorstep_retreat_heal",
                        max_radius=max(3, config.cargo_delivery_yield_radius),
                    )
                if intent is None:
                    intent = _critical_retreat_sidestep(
                        ranger, context, memory, reservations,
                        "retreat_heal_sidestep",
                    )
                chosen_intent = intent or _wait(ranger, "unit_retreat_to_core_heal_blocked")
                intents.append(chosen_intent)
                core_pos = context.core.position if context.core else ranger.position
                _record_unit_task(memory, context, ranger, kind="retreat_heal", target=core_pos, intent=chosen_intent)
            continue
        if target is not None:
            targeted_damage[target.id] = targeted_damage.get(target.id, 0) + 1
            intents.append(ActionIntent(
                actor_id=ranger.id,
                is_core=False,
                action=ActionKind.SHOOT,
                score=850 + ranger_target_score(ranger, target, context, memory, config),
                reason="highest_scoring_legal_ranger_target",
                target_id=target.id,
                target_cell=target.position,
            ))
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
                ranger, staging, "intercept_ranger_firing_line", 620,
                context=context, memory=memory, reservations=reservations,
                deadline=deadline, config=config,
            )
            if intent is None:
                intent = _firing_line_sidestep(
                    ranger, intercept_enemy, staging, context, memory,
                    reservations, "intercept_firing_sidestep",
                )
            _record_unit_task(memory, context, ranger, kind="intercept", target=staging, intent=intent)
            intents.append(intent or _wait(ranger, "intercept_firing_route_blocked"))
            continue

        target_enemy = _best_visible_enemy(ranger, context, memory)
        if target_enemy is not None:
            dist_to_enemy = distance(ranger.position, target_enemy.position)
            in_tactical_range = dist_to_enemy <= max(config.intercept_distance * 2, 16)

            # Expedition ranger only engages enemies within its immediate tactical radius,
            # maintaining priority on advancing with the expedition beacon squad.
            is_expedition = ranger.id in expedition_rangers
            expedition_engage = is_expedition and dist_to_enemy <= max(config.intercept_distance, 8)

            # A base defender may adjust within the defense perimeter even
            # when the intruder has not yet crossed the Core intercept radius.
            # Do not turn a fixed guard into a long-range pursuer.
            local_base_reposition = (
                ranger.id in guard_rangers
                and context.core is not None
                and distance(ranger.position, context.core.position) <= config.intercept_distance
                and in_tactical_range
            )

            # Near core defense is strictly for rangers stationed within core defensive reach
            near_core_defender = (
                context.core is not None
                and distance(target_enemy.position, context.core.position) <= config.intercept_distance
                and distance(ranger.position, context.core.position) <= config.defense_exit_distance
                and in_tactical_range
            )

            # Non-expedition mobile squad (mining/scout) can engage within tactical range
            mobile_engage = (
                not is_expedition
                and ranger.id in (mining_rangers | scout_rangers)
                and in_tactical_range
            )

            if expedition_engage or local_base_reposition or near_core_defender or mobile_engage:
                staging = _ranger_staging_cell(ranger, target_enemy, context, memory)
                intent = _move(
                    ranger, staging, "ranger_seek_legal_firing_line", 580,
                    context=context, memory=memory, reservations=reservations,
                    deadline=deadline, config=config,
                )
                if intent is None:
                    defensive_contact = (
                        context.core is not None
                        and (
                            _enemy_can_attack_core(target_enemy, context.core, memory.obstacles)
                            or distance(target_enemy.position, context.core.position) <= config.intercept_distance
                        )
                    )
                    intent = _firing_line_sidestep(
                        ranger, target_enemy, staging, context, memory,
                        reservations,
                        "defensive_firing_line_sidestep" if defensive_contact else "firing_line_sidestep",
                    )
                if mobile_engage:
                    _record_unit_task(
                        memory, context, ranger,
                        kind="engage_firing_line",
                        target=target_enemy.position,
                        intent=intent,
                    )
                intents.append(intent or _wait(
                    ranger,
                    "defensive_firing_route_blocked" if context.core is not None
                    and (_enemy_can_attack_core(target_enemy, context.core, memory.obstacles)
                         or distance(target_enemy.position, context.core.position) <= config.intercept_distance)
                    else "firing_route_blocked",
                ))
                continue

        cargo_yield = _yield_cargo_delivery(
            ranger, context, memory, reservations, config
        )
        if cargo_yield is not None:
            _record_unit_task(
                memory,
                context,
                ranger,
                kind="yield_cargo_delivery",
                target=cargo_yield.target_cell or ranger.position,
                intent=cargo_yield,
            )
            intents.append(cargo_yield)
            continue

        if ranger.id in expedition_rangers:
            target_cell = context.beacon.position
            intent = _move(
                ranger, target_cell, "expedition_ranger_support", 480,
                context=context, memory=memory, reservations=reservations,
                deadline=deadline, config=config,
            )
            if intent is None and context.core is not None:
                intent = _deploy_sidestep(
                    ranger, target_cell, context, memory,
                    reservations, "expedition_ranger_sidestep", context.core.position,
                )
            _record_unit_task(memory, context, ranger, kind="expedition_support", target=target_cell, intent=intent)
            intents.append(intent or _wait(ranger, "hunter_route_blocked"))
            continue

        if ranger.id in mining_rangers and context.core is not None:
            exploring_workers = [
                worker for worker in context.workers
                if (worker.cargo or 0) == 0 and distance(worker.position, context.core.position) > 2
            ]
            if exploring_workers:
                assignment = _escort_assignment(ranger, escort_combat, exploring_workers, context.core)
                if assignment is not None:
                    _, escort_slot = assignment
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

        if ranger.id in scout_rangers:
            # SCOUT_ENGAGE_HYSTERESIS: when the ranger was recently engaging an
            # enemy via ranger_seek_legal_firing_line, maintain engagement
            # posture for a grace period before falling back to hunter_forward_recon.
            # This prevents the 2-tick back-and-forth oscillation where the
            # ranger alternates between seeking a firing line (toward the enemy)
            # and hunter recon patrol (toward the core).
            _engage_task = memory.unit_tasks.get(str(ranger.id), {})
            _engage_since = _engage_task.get("engage_since")
            _recently_engaged = (
                _engage_task.get("kind") == "engage_firing_line"
                and _engage_since is not None
                and context.tick - _engage_since < config.scout_engage_grace_ticks
            )
            if _recently_engaged:
                # During grace period: if enemy still visible, continue engaging
                # with extended tactical range; otherwise wait in place.
                _grace_enemy = _best_visible_enemy(ranger, context, memory)
                if _grace_enemy is not None and context.core is not None:
                    _staging = _ranger_staging_cell(ranger, _grace_enemy, context, memory)
                    intent = _move(
                        ranger, _staging, "ranger_seek_legal_firing_line", 580,
                        context=context, memory=memory, reservations=reservations,
                        deadline=deadline, config=config,
                    )
                    if intent is None:
                        intent = _firing_line_sidestep(
                            ranger, _grace_enemy, _staging, context, memory,
                            reservations, "engage_grace_firing_sidestep",
                        )
                    _record_unit_task(
                        memory, context, ranger,
                        kind="engage_firing_line",
                        target=_grace_enemy.position,
                        intent=intent,
                    )
                    intents.append(intent or _wait(ranger, "engage_grace_firing_blocked"))
                else:
                    _engage_target = tuple(_engage_task["target"]) if isinstance(_engage_task.get("target"), list) and len(_engage_task["target"]) == 2 else ranger.position
                    intent = _move(
                        ranger, _engage_target, "engage_grace_pursue", 580,
                        context=context, memory=memory, reservations=reservations,
                        deadline=deadline, config=config,
                    )
                    _record_unit_task(
                        memory, context, ranger,
                        kind="engage_firing_line",
                        target=_engage_target,
                        intent=intent,
                    )
                    intents.append(intent or _wait(ranger, "engage_grace_hold"))
                continue

            hunter_target = _combat_target(
                context.core, ranger, hunter_roster.index(ranger), len(hunter_roster),
                memory, config, role="hunter",
            ) if context.core is not None and ranger in hunter_roster else None
            if hunter_target is not None:
                intent = _move(
                    ranger, hunter_target, "hunter_forward_recon", 420,
                    context=context, memory=memory, reservations=reservations,
                    deadline=deadline, config=config,
                )
                if intent is None and context.core is not None:
                    intent = _deploy_sidestep(
                        ranger, hunter_target, context, memory,
                        reservations, "hunter_forward_recon", context.core.position,
                    )
                _record_unit_task(memory, context, ranger, kind="hunter", target=hunter_target, intent=intent)
                intents.append(intent or _wait(ranger, "hunter_route_blocked"))
                continue

        ordered_guards = tuple(sorted(guard_rangers, key=lambda unit_id: unit_id.bytes))
        guard_index = ordered_guards.index(ranger.id) if ranger.id in guard_rangers else 0
        guard_target = guard_slots[(guard_index + len(context.vanguards)) % len(guard_slots)] if guard_slots else None
        if guard_target is not None and ranger.position != guard_target:
            # DECISION_LATENCY_SPIKE fix: rangers far from their guard slot
            # must not attempt expensive A* across 1000+ fogged cells.
            # Skip A* entirely and use lightweight greedy fallback steps.
            if distance(ranger.position, guard_target) > config.long_distance_retreat_threshold:
                intent = _distant_retreat_fallback_intent(
                    ranger, guard_target, context, memory, reservations,
                    "ranger_hold_defense_ring",
                )
            else:
                intent = _move(
                    ranger, guard_target, "ranger_hold_defense_ring", 280,
                    context=context, memory=memory, reservations=reservations,
                    deadline=deadline, config=config,
                )
                if intent is None:
                    intent = _evacuate_doorstep_intent(
                        ranger, context, memory, reservations, "yield_doorstep_guard_route"
                    )
                if intent is None and context.core is not None:
                    intent = _deploy_sidestep(
                        ranger, guard_target, context, memory,
                        reservations, "ranger_hold_defense_ring", context.core.position,
                    )
            _record_unit_task(memory, context, ranger, kind="core_guard", target=guard_target, intent=intent)
            intents.append(intent or _wait(ranger, "guard_route_blocked"))
        else:
            if guard_target is not None:
                _record_unit_task(memory, context, ranger, kind="core_guard", target=guard_target, intent=None)
            # When the unit IS at its assigned guard target, do NOT evacuate
            # the doorstep — the guard-slot allocation is the authoritative
            # placement decision.  Evacuating would create a 2-Tick ping-pong
            # oscillation with the guard-return logic above.  (GUARD_OSCILLATION fix)
            if guard_target is not None and ranger.position == guard_target:
                intents.append(_wait(ranger, "holding_defense_ring"))
            else:
                intent = _evacuate_doorstep_intent(
                    ranger, context, memory, reservations, "yield_doorstep_holding_defense"
                )
                intents.append(intent or _wait(ranger, "holding_defense_ring"))
    return intents
