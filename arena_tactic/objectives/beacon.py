"""Pure, persistent Beacon objective lifecycle; no SDK or Turn dependency."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from ..domain import Goal, GoalSource, GoalStatus, Task, TaskStatus


class BeaconStage(StrEnum):
    ASSEMBLE = "ASSEMBLE"
    PICKUP = "PICKUP"
    EXFIL = "EXFIL"
    SECURE = "SECURE"
    # Persisted v1 state may still contain HOLD.  It is accepted on load and
    # immediately normalized to EXFIL or SECURE from current carrier facts.
    HOLD = "HOLD"
    RECOVER = "RECOVER"


@dataclass(frozen=True, slots=True)
class BeaconInput:
    tick: int
    beacon_cell: tuple[int, int]
    ground_visible: bool
    own_carrier_alias: str | None
    carrier_alive: bool
    escort_ready: int
    escort_quorum: int
    holding: bool = False
    carrier_secured: bool = False


@dataclass(frozen=True, slots=True)
class BeaconCampaign:
    stage: BeaconStage = BeaconStage.ASSEMBLE
    carrier_alias: str | None = None
    recovery_cell: tuple[int, int] | None = None

    def evaluate(self, facts: BeaconInput) -> tuple["BeaconCampaign", Goal, tuple[Task, ...], tuple[str, ...]]:
        """Return next lifecycle records and intent *kinds*, never actions."""
        stage = self.stage
        carrier = self.carrier_alias
        recovery = self.recovery_cell
        if carrier is not None and not facts.carrier_alive:
            stage, carrier, recovery = BeaconStage.RECOVER, None, facts.beacon_cell
        elif facts.holding or (facts.own_carrier_alias is not None and facts.carrier_alive):
            stage = BeaconStage.SECURE if facts.carrier_secured else BeaconStage.EXFIL
            carrier = facts.own_carrier_alias or carrier
        elif stage is BeaconStage.RECOVER and facts.ground_visible:
            stage, recovery = BeaconStage.ASSEMBLE, None
        elif facts.escort_ready >= facts.escort_quorum and facts.ground_visible:
            stage = BeaconStage.PICKUP
        else:
            stage = BeaconStage.ASSEMBLE

        next_state = BeaconCampaign(stage, carrier, recovery)
        goal = Goal("beacon_campaign", "CONTROL_BEACON", GoalSource.AUTO, GoalStatus.ACTIVE, 650, facts.tick)
        if stage is BeaconStage.PICKUP:
            tasks = (Task("pickup_beacon", goal.goal_id, "PICKUP_BEACON", TaskStatus.READY, 650,
                          target=facts.beacon_cell, required_roles=("VANGUARD", "RANGER", "WORKER")),)
            candidates = ("PICKUP_BEACON",)
        elif stage is BeaconStage.EXFIL:
            tasks = (
                Task("exfil_beacon", goal.goal_id, "EXFIL_BEACON", TaskStatus.RUNNING, 850),
                Task("escort_carrier", goal.goal_id, "ESCORT_CARRIER", TaskStatus.RUNNING, 800),
            )
            candidates = ("WITHDRAW_BEACON",)
        elif stage in {BeaconStage.SECURE, BeaconStage.HOLD}:
            tasks = (
                Task("secure_beacon", goal.goal_id, "SECURE_BEACON", TaskStatus.RUNNING, 700),
                Task("repair_beacon_core", goal.goal_id, "REPAIR_SHIELD", TaskStatus.READY, 700),
                Task("expand_mining_escort", goal.goal_id, "EXPAND_MINING_ESCORT", TaskStatus.READY, 620),
            )
            candidates = ("REPAIR_SHIELD", "EXPAND_MINING_ESCORT")
        elif stage is BeaconStage.RECOVER:
            tasks = (Task("recover_beacon", goal.goal_id, "RECOVER_BEACON", TaskStatus.READY, 600,
                          target=recovery),)
            candidates = ("REACQUIRE_BEACON",)
        else:
            tasks = (Task("escort_beacon", goal.goal_id, "ESCORT_CARRIER", TaskStatus.READY, 600,
                          min_assignees=max(1, facts.escort_quorum), max_assignees=max(1, facts.escort_quorum)),)
            candidates = ("ASSEMBLE_ESCORT",)
        return next_state, goal, tasks, candidates
