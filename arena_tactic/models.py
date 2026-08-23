"""Immutable decision-domain models for the Arena Hero tactic."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Mapping
from uuid import UUID

from arena_hero import Direction, UnitType

if TYPE_CHECKING:
    from .command_center import PreparedCommands
    from .domain import DecisionTrace
    from .memory import AgentMemory


Position = tuple[int, int]


class StrategicMode(StrEnum):
    RESPAWN = "RESPAWN"
    RECOVER = "RECOVER"
    DEFEND = "DEFEND"
    ECONOMY = "ECONOMY"
    EXPLORE = "EXPLORE"
    BEACON = "BEACON"
    ATTACK = "ATTACK"


class ActionKind(StrEnum):
    WAIT = "WAIT"
    MOVE = "MOVE"
    HARVEST = "HARVEST"
    DEPOSIT = "DEPOSIT"
    SWEEP = "SWEEP"
    SHOOT = "SHOOT"
    HEAL = "HEAL"
    SPAWN = "SPAWN"
    REPAIR_SHIELD = "REPAIR_SHIELD"
    START_MOVE = "START_MOVE"
    PICKUP_BEACON = "PICKUP_BEACON"
    SELF_DESTRUCT = "SELF_DESTRUCT"


@dataclass(frozen=True, slots=True)
class AgentConfig:
    """Centralized behavior settings intended for replay-based tuning."""

    planning_budget_ms: float = 500.0
    max_population: int = 40
    minimum_resource_reserve: int = 5
    defense_enter_distance: int = 3
    defense_exit_distance: int = 5
    # C: DEFEND 超时：连续 N 回合没有实际伤害事件则强制退出防守
    defense_stale_ticks: int = 30
    # Enemy cores can disappear from one authoritative Turn when they leave
    # vision. Keep an already-started attack alive briefly instead of letting
    # the strategic mode fall through to BEACON on the very next Turn.
    attack_exit_grace_ticks: int = 4
    migration_idle_ticks: int = 8
    migration_cooldown_ticks: int = 8
    early_workers: int = 2
    enable_spawn_reroll: bool = True
    spawn_eval_max_ticks: int = 20
    spawn_eval_mine_max_dist: int = 12
    spawn_eval_worker_max: int = 2
    early_vanguards: int = 2
    early_rangers: int = 1
    mature_workers: int = 12
    mature_vanguards: int = 12
    mature_rangers: int = 16
    # Extra resource buffer kept in peacetime so wartime production can
    # afford expensive late-game units.  Only applies when the mature
    # roster is already filled; 0 disables the peacetime conservation.
    peacetime_resource_buffer: int = 40
    astar_node_limit: int = 1_500
    explored_history_limit: int = 20_000
    event_history_limit: int = 512
    exploration_sector_ticks: int = 60
    movement_failure_cooldown_ticks: int = 4
    resource_recheck_failure_threshold: int = 2
    resource_recheck_cooldown_ticks: int = 8
    resource_recheck_worker_limit: int = 1
    # Keep a Worker's already-selected resource destination briefly when it
    # leaves the current visibility set between path steps.  The resource
    # observation remains the authority: depletion or an authoritative empty
    # recheck still removes the lock immediately.
    resource_target_grace_ticks: int = 4
    core_guard_vanguards: int = 2
    core_guard_rangers: int = 1
    patrol_radius_min: int = 5
    patrol_radius_max: int = 8
    patrol_rotation_ticks: int = 6
    hunter_radius_min: int = 7
    hunter_radius_max: int = 10
    intercept_vanguards: int = 2
    intercept_rangers: int = 1
    intercept_distance: int = 8
    intercept_approach_streak: int = 1
    enemy_track_ttl_ticks: int = 3
    scheduler_shadow: bool = False
    scheduler_canary: bool = False
    worker_bt_canary: bool = False
    vanguard_bt_canary: bool = False
    ranger_bt_canary: bool = False
    core_bt_canary: bool = False
    beacon_campaign_v1: bool = False
    core_migration_v1: bool = False
    hidden_attack_search_radius: int = 3
    hidden_attack_migration_streak: int = 3
    core_attack_campaign_v1: bool = False
    planner_canary: bool = False


@dataclass(frozen=True, slots=True)
class ActionIntent:
    """A controller-independent action proposal for a current-Turn object."""

    actor_id: UUID
    is_core: bool
    action: ActionKind
    score: float
    reason: str
    target_id: UUID | None = None
    target_cell: Position | None = None
    direction: Direction | None = None
    unit_type: UnitType | None = None
    estimated_cost: int = 0
    reserved_cell: Position | None = None


@dataclass(frozen=True, slots=True)
class RejectedIntent:
    intent: ActionIntent
    rejection_reason: str


@dataclass(frozen=True, slots=True)
class DecisionResult:
    mode: StrategicMode
    intents: tuple[ActionIntent, ...]
    rejected_intents: tuple[RejectedIntent, ...]
    decision_ms: float
    action_counts: Mapping[str, int]
    wait_reasons: tuple[str, ...]
    next_memory: AgentMemory
    timed_out: bool = False
    trace: DecisionTrace | None = None
    prepared_commands: PreparedCommands | None = None


@dataclass(slots=True)
class ReservationTable:
    """Conservative end-of-Tick friendly-cell capacity tracking."""

    occupancy: dict[Position, int]
    incoming: dict[Position, int] = field(default_factory=dict)

    def can_reserve(self, destination: Position) -> bool:
        return self.occupancy.get(destination, 0) + self.incoming.get(
            destination, 0
        ) < 2

    def reserve(self, destination: Position) -> bool:
        if not self.can_reserve(destination):
            return False
        self.incoming[destination] = self.incoming.get(destination, 0) + 1
        return True
