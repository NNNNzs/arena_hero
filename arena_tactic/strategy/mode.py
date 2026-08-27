"""Strategic mode selection and mode predicates."""

from __future__ import annotations

from arena_hero import BeaconStatus, CoreState, CoreView, UnitType

from ..context import DecisionContext
from ..memory import AgentMemory
from ..models import AgentConfig, Position, StrategicMode
from ..navigation import distance
from .combat import _enemy_can_attack_core

UNIT_MAX_HP = {
    UnitType.WORKER: 2,
    UnitType.VANGUARD: 4,
    UnitType.RANGER: 2,
}
CORE_MAX_HP = 5
_EXPLORATION_SECTORS = ((1, 0), (0, 1), (-1, 0), (0, -1))

def _pressure_distance(context: DecisionContext) -> int | None:
    if context.core is None or not context.enemies:
        return None
    return min(distance(enemy.position, context.core.position) for enemy in context.enemies)


def _core_emergency_defense(context: DecisionContext) -> bool:
    """Return True when normal patrol/economy behavior is too risky."""
    core = context.core
    if core is None:
        return False
    recent_damage = sum(
        event.event_type == "CORE_DAMAGED"
        for event in context.events
    )
    return core.hp <= 3 or core.shield == 0 or recent_damage >= 2


def _hidden_attack_pressure(context: DecisionContext, memory: AgentMemory) -> bool:
    """Use damage timing as pressure only; it never supplies an attacker cell."""
    return (
        context.core is not None
        and not context.enemies
        and memory.core_damage_streak >= 2
        and memory.last_core_damage_tick == context.tick
    )


def _hidden_attack_search_cell(
    core: CoreView, index: int, tick: int, config: AgentConfig
) -> Position:
    """Return a deterministic reconnaissance slot, not an inferred target."""
    sector = (index + tick) % len(_EXPLORATION_SECTORS)
    dx, dy = _EXPLORATION_SECTORS[sector]
    radius = max(2, config.hidden_attack_search_radius)
    return core.position[0] + dx * radius, core.position[1] + dy * radius


def _beacon_owned(context: DecisionContext) -> bool:
    return context.beacon.carrier_id in context.current_objects


def _beacon_needs_exfil(
    context: DecisionContext,
    config: AgentConfig,
) -> bool:
    if context.core is None or context.beacon.carrier_id is None:
        return False
    carrier = context.current_objects.get(context.beacon.carrier_id)
    return (
        carrier is not None
        and distance(carrier.position, context.core.position)
        > max(0, config.beacon_secure_radius)
    )


def choose_mode(
    context: DecisionContext,
    memory: AgentMemory,
    config: AgentConfig,
) -> StrategicMode:
    """Select a mode with hard safety overrides and small exit hysteresis."""
    core = context.core
    if core is None:
        return StrategicMode.RESPAWN
    if core.state is CoreState.MOVING:
        return StrategicMode.RECOVER

    pressure = _pressure_distance(context)
    emergency_defense = _core_emergency_defense(context)
    immediate_threat = any(
        _enemy_can_attack_core(enemy, core, memory.obstacles)
        for enemy in context.enemies
    )
    if _hidden_attack_pressure(context, memory):
        return StrategicMode.DEFEND
    if emergency_defense and context.enemies:
        return StrategicMode.DEFEND
    if immediate_threat or (
        pressure is not None and pressure <= config.defense_enter_distance
    ):
        return StrategicMode.DEFEND
    # C: 防守超时退出——处于 DEFEND 且超过 defense_stale_ticks 回合没有实际伤害，
    # 跳过所有滞后 DEFEND 返回，强制解除防守
    defend_stale = (
        memory.last_mode is StrategicMode.DEFEND
        and context.tick - memory.last_core_damage_tick > config.defense_stale_ticks
    )
    if not defend_stale and (
        memory.last_mode is StrategicMode.DEFEND
        and pressure is not None
        and pressure <= config.defense_exit_distance
    ):
        return StrategicMode.DEFEND

    damaged_at_core = any(
        unit.position == core.position and unit.hp < UNIT_MAX_HP[unit.unit_type]
        for unit in context.units
    )
    if core.hp < CORE_MAX_HP or core.shield < 3 or damaged_at_core:
        return StrategicMode.RECOVER

    # Once acquired, bringing the publicly tracked carrier home outranks a new
    # offensive campaign. Immediate defense and recovery still preempt exfil.
    if _beacon_needs_exfil(context, config):
        return StrategicMode.BEACON

    combat_count = len(context.vanguards) + len(context.rangers)
    enemy_core_visible = any(isinstance(enemy, CoreView) for enemy in context.enemies)
    attack_enter = (
        enemy_core_visible
        and combat_count >= 3
        and core.hp == CORE_MAX_HP
        and core.shield >= 3
    )
    attack_stay = (
        memory.last_mode is StrategicMode.ATTACK
        and combat_count >= 2
        and core.hp == CORE_MAX_HP
        and core.shield >= 2
        and (
            enemy_core_visible
            or context.tick - memory.mode_since_tick <= config.attack_exit_grace_ticks
        )
    )
    if attack_enter or attack_stay:
        return StrategicMode.ATTACK

    beacon_available = (
        not _beacon_owned(context)
        and context.beacon.status in (None, BeaconStatus.GROUND)
    )
    beacon_enter = (
        beacon_available
        and core.hp == CORE_MAX_HP
        and core.shield >= 3
        and context.population >= 6
    )
    beacon_stay = (
        memory.last_mode is StrategicMode.BEACON
        and beacon_available
        and core.hp == CORE_MAX_HP
        and core.shield >= 2
    )
    if beacon_enter or beacon_stay:
        return StrategicMode.BEACON

    early_roster_ready = (
        len(context.workers) >= config.early_workers
        and len(context.vanguards) >= config.early_vanguards
        and len(context.rangers) >= config.early_rangers
    )
    if (
        context.resource_cells
        or any((worker.cargo or 0) > 0 for worker in context.workers)
        or not early_roster_ready
    ):
        return StrategicMode.ECONOMY
    return StrategicMode.EXPLORE

