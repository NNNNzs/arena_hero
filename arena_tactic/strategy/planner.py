"""Top-level intent planning orchestration."""

from __future__ import annotations

from time import perf_counter
from uuid import UUID

from arena_hero import CoreState

from ..context import DecisionContext
from ..memory import AgentMemory
from ..models import (
    ActionIntent,
    ActionKind,
    AgentConfig,
    ReservationTable,
    StrategicMode,
)
from ..squad_coordination import coordinate_expedition_intents
from ..squads import SQUAD_ID_BY_TYPE, SquadType, build_squad_plan
from .common import CORE_MAX_HP, UNIT_MAX_HP, _anticipated_resources, _at_normal_core, _evict_combat_from_core_for_cargo
from .core_plan import _plan_core
from .mode import choose_mode
from .rangers import _plan_rangers
from .vanguards import _plan_vanguards
from .workers import _plan_workers


def propose_intents(
    context: DecisionContext,
    memory: AgentMemory,
    config: AgentConfig,
    deadline: float,
) -> tuple[StrategicMode, tuple[ActionIntent, ...], bool]:
    mode = choose_mode(context, memory, config)
    memory.record_mode(mode, context.tick)
    if context.core is None:
        return mode, (), perf_counter() >= deadline

    # One authoritative squad plan is shared by all role planners so the
    # Dashboard roster, persistent manual membership and actual behavior agree.
    squad_plan = build_squad_plan(context, memory, config)

    occupancy = {cell: len(ids) for cell, ids in context.friendly_occupancy.items()}
    if context.core.state is CoreState.MOVING and context.core.destination:
        occupancy[context.core.destination] = occupancy.get(context.core.destination, 0) + 1
    reservations = ReservationTable(occupancy=occupancy)

    anticipated_resources = _anticipated_resources(context)
    core_future_cost = min(
        max(0, CORE_MAX_HP - context.core.hp), anticipated_resources
    )
    remaining_heal_budget = max(0, anticipated_resources - core_future_cost)
    heal_allowances: dict[UUID, int] = {}
    for unit in sorted(context.units, key=lambda candidate: candidate.id.bytes):
        if not _at_normal_core(unit, context):
            continue
        missing_hp = UNIT_MAX_HP[unit.unit_type] - unit.hp
        allowance = min(missing_hp, remaining_heal_budget)
        if allowance > 0:
            heal_allowances[unit.id] = allowance
            remaining_heal_budget -= allowance

    intents = _plan_workers(
        context, memory, reservations, deadline, config, heal_allowances, squad_plan
    )
    intents.extend(
        _plan_vanguards(
            context,
            memory,
            mode,
            reservations,
            deadline,
            config,
            heal_allowances,
            squad_plan,
        )
    )
    intents.extend(
        _plan_rangers(
            context,
            memory,
            mode,
            reservations,
            deadline,
            config,
            heal_allowances,
            squad_plan,
        )
    )
    expedition = squad_plan.squads.get(
        SQUAD_ID_BY_TYPE[SquadType.EXPEDITION_BEACON]
    )
    if expedition is not None and expedition.members:
        intents = list(coordinate_expedition_intents(
            context,
            memory,
            config,
            expedition,
            tuple(intents),
            deadline=deadline,
        ))
    # Post-process: evict any combat unit WAIT-ing on the core cell so
    # that cargo workers can enter and deposit.  This catches cases the
    # in-tree _yield_cargo_delivery call cannot reach (expedition contact
    # hold, guard-route-blocked, healing-waits-for-resources, etc.).
    intents = _evict_combat_from_core_for_cargo(
        intents, context, memory, reservations, config,
    )
    planned_unit_heals = sum(
        intent.estimated_cost
        for intent in intents
        if intent.action is ActionKind.HEAL
    )
    core_intent = _plan_core(
        context, memory, mode, intents, planned_unit_heals, config
    )
    if core_intent is not None:
        intents.append(core_intent)
    return mode, tuple(intents), perf_counter() >= deadline
