"""Synchronous per-Turn runtime that separates decision and persistence."""

from __future__ import annotations

from time import perf_counter
from dataclasses import replace

from arena_hero import BeaconStatus, CoreState, CoreView, Turn, UnitType, UnitView

from .allocator import action_counts, apply_intents
from .behaviors import CoreCanaryPlanner, RangerCanaryPlanner, VanguardCanaryPlanner, WorkerCanaryPlanner, WorkerCanaryResult
from .command_center import CommandQueue, PreparedCommands
from .context import DecisionContext
from .domain import BoundedTraceSink, EntityTrace, Goal, GoalSource, GoalStatus, NodeTrace, Task, TaskStatus, TraceLimits
from .identity import entity_alias
from .memory import AgentMemory, MemoryStore
from .models import ActionIntent, ActionKind, AgentConfig, DecisionResult, ReservationTable, StrategicMode
from .navigation import DIRECTIONS, adjacent_direction, destination, distance, plan_step, shot_range
from .objectives import (
    BeaconCampaign,
    BeaconInput,
    BeaconStage,
    CoreAttackCampaign,
    CoreAttackInput,
    CoreAttackStage,
    CoreMigrationInput,
    CoreMigrationPlan,
    MigrationStage,
)
from .planning import LegacyPlannerAdapter
from .scheduler import Actor, DeterministicScheduler, ScheduledAssignment, ScheduledTask, ScheduleResult
from .squad_coordination import (
    coordinate_expedition_intents,
    intent_is_squad_protected,
)
from .squads import (
    SQUAD_ID_BY_TYPE,
    Squad,
    SquadMember,
    SquadRole,
    SquadType,
    build_squad_plan,
)
from .analysis_scheduler import AnalysisScheduler, MigrationRecommendation, default_analysis_scheduler
from .tactical_geometry import migration_site_score, rich_resource_center
from .strategy import choose_mode, explain_mode, propose_intents
from .validation import validate_intents


class AgentRuntime:
    def __init__(
        self,
        *,
        memory: AgentMemory | None = None,
        config: AgentConfig | None = None,
        memory_store: MemoryStore | None = None,
        enable_trace: bool = True,
        trace_limits: TraceLimits | None = None,
        trace_sink: BoundedTraceSink | None = None,
        command_queue: CommandQueue | None = None,
    ) -> None:
        self.config = config or AgentConfig()
        self.memory_store = memory_store
        self.enable_trace = enable_trace
        self.trace_limits = trace_limits or TraceLimits()
        self.trace_sink = trace_sink
        self.command_queue = command_queue
        self.trace_drops = 0
        self.legacy_planner = LegacyPlannerAdapter()
        self.scheduler = DeterministicScheduler()
        self.worker_canary = WorkerCanaryPlanner()
        self.vanguard_canary = VanguardCanaryPlanner()
        self.ranger_canary = RangerCanaryPlanner()
        self.core_canary = CoreCanaryPlanner()
        self.last_shadow: ScheduleResult | None = None
        self.last_schedule: ScheduleResult | None = None
        self._last_schedule_tasks: dict[str, dict[str, object]] = {}
        self._last_shadow_tasks: dict[str, dict[str, object]] = {}
        self.memory = memory or (
            memory_store.load() if memory_store is not None else AgentMemory()
        )
        # Initialize analysis scheduler from persisted memory or defaults.
        if self.memory.analysis_tasks:
            self.analysis_scheduler = AnalysisScheduler.from_dict(
                self.memory.analysis_tasks
            )
        else:
            self.analysis_scheduler = default_analysis_scheduler()
            self.memory.analysis_tasks = self.analysis_scheduler.to_dict()
        if self.command_queue is not None:
            self.command_queue.restore_policy(self.memory.policy_state)

    # 策略热更新白名单：与 command_center._POLICY_NUMERIC_FIELDS 对齐
    _CONFIG_OVERRIDE_FIELDS = frozenset({
        "core_guard_vanguards", "core_guard_rangers", "cargo_delivery_yield_radius",
        "beacon_secure_radius",
        "early_workers", "early_vanguards", "early_rangers",
        "patrol_radius_min", "patrol_radius_max", "patrol_arc_segments",
        "patrol_radius_units_per_step",
        "minimum_resource_reserve", "peacetime_resource_buffer", "unit_retreat_heal_ratio",
        "unit_retreat_heal_return_ratio",
    })

    def _apply_config_overrides(self, memory: AgentMemory) -> AgentConfig:
        """从 policy_state 读取数值覆盖，生成每 tick 有效的配置副本。"""
        overrides: dict[str, int | float] = {}
        for field_name in self._CONFIG_OVERRIDE_FIELDS:
            value = memory.policy_state.get(field_name)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and hasattr(self.config, field_name):
                overrides[field_name] = value
        if overrides:
            return replace(self.config, **overrides)
        return self.config

    def decide(self, turn: Turn) -> DecisionResult:
        started = perf_counter()
        deadline = started + self.config.planning_budget_ms / 1_000
        context = DecisionContext.from_turn(turn)
        mode_before = self.memory.last_mode
        mode_since_before = self.memory.mode_since_tick
        prepared_commands = self._prepare_commands(context)
        next_memory = self.memory.advance(context, self.config)
        # 应用策略覆盖到运行时配置（每 tick 从 policy_state 读取数值覆盖）
        effective_config = self._apply_config_overrides(next_memory)
        self._effective_config = effective_config
        # Run due analysis tasks – pure functional check, then execute.
        self._run_due_analyses(context, next_memory)
        command_assignments = self._stage_manual_commands(context, next_memory, prepared_commands)
        # TRIGGER_ANALYSIS 命令会重置 last_run_tick，需再次检查以立即执行
        if "analysis" in command_assignments:
            self._run_due_analyses(context, next_memory)
        objective_shadow = self._objective_shadow(context, next_memory)
        schedule = self._schedule_canary(context, next_memory) if (self.config.scheduler_canary or self.config.planner_canary) else None
        if self.config.planner_canary:
            mode = choose_mode(context, next_memory, self.config)
            next_memory.record_mode(mode, context.tick)
            proposals, timed_out = (), False
        else:
            mode, proposals, timed_out = propose_intents(
                context, next_memory, self.config, deadline
            )
        worker_result: WorkerCanaryResult | None = None
        worker_canary_failed = False
        vanguard_canary_failed = False
        ranger_canary_failed = False
        core_canary_failed = False
        use_worker_tree = self.config.worker_bt_canary or self.config.planner_canary
        use_vanguard_tree = self.config.vanguard_bt_canary or self.config.planner_canary
        use_ranger_tree = self.config.ranger_bt_canary or self.config.planner_canary
        use_core_tree = self.config.core_bt_canary or self.config.planner_canary
        if context.core is not None and use_worker_tree:
            try:
                worker_result = self.worker_canary.propose(
                    context, next_memory, self.config, deadline
                )
                worker_ids = {worker.id for worker in context.workers}
                # One planner owns one entity.  The Worker tree replaces only
                # Worker proposals; Vanguard, Ranger, and Core remain legacy.
                proposals = tuple(
                    proposal for proposal in proposals if proposal.actor_id not in worker_ids
                ) + worker_result.intents
            except Exception:
                # A canary must not turn a local planning fault into a missed
                # command window.  Retain the already-built legacy proposal.
                worker_canary_failed = True
        if context.core is not None and use_vanguard_tree:
            try:
                vanguard_intents = self.vanguard_canary.propose(context, next_memory, self.config, deadline)
                vanguard_ids = {unit.id for unit in context.vanguards}
                proposals = tuple(item for item in proposals if item.actor_id not in vanguard_ids) + vanguard_intents
            except Exception:
                vanguard_canary_failed = True
        if context.core is not None and use_ranger_tree:
            try:
                ranger_intents = self.ranger_canary.propose(context, next_memory, self.config, deadline)
                ranger_ids = {unit.id for unit in context.rangers}
                proposals = tuple(item for item in proposals if item.actor_id not in ranger_ids) + ranger_intents
            except Exception:
                ranger_canary_failed = True
        if context.core is not None and use_core_tree:
            try:
                core_intent = self.core_canary.propose(context, self.config)
                if core_intent is not None:
                    proposals = tuple(item for item in proposals if not item.is_core) + (core_intent,)
            except Exception:
                core_canary_failed = True
        proposals = self._apply_beacon_campaign(
            context, next_memory, proposals, mode,
            deadline=deadline,
        )
        proposals = self._apply_core_migration(context, next_memory, proposals, deadline=deadline)
        proposals = self._apply_core_attack(context, next_memory, proposals, deadline=deadline)
        proposals = self._apply_manual_assignments(context, next_memory, proposals, deadline)
        if prepared_commands is not None and prepared_commands.emergency_halt:
            proposals = self._emergency_waits(context)
        if context.core is None:
            intents, rejected = (), ()
        else:
            intents, rejected = validate_intents(proposals, context, self.config)
            apply_intents(turn, intents)
        elapsed_ms = (perf_counter() - started) * 1_000
        waits = tuple(
            sorted({intent.reason for intent in intents if intent.action.value == "WAIT"})
        )
        trace = None
        shadow = self._scheduler_shadow(context) if self.config.scheduler_shadow else None
        if self.enable_trace:
            try:
                trace = self.legacy_planner.trace(
                    context, intents, rejected, next_memory, self.trace_limits, elapsed_ms
                )
                if not trace.truncation.byte_limit_reached:
                    trace = replace(trace, causality={"mode": explain_mode(
                        context, next_memory, effective_config, mode,
                        previous_mode=mode_before, previous_since_tick=mode_since_before,
                    )})
                if self.config.planner_canary:
                    trace = replace(trace, planner_version="bt-planner-canary-v1")
                if worker_result is not None:
                    trace = self._with_worker_canary_trace(trace, worker_result)
                if use_vanguard_tree and not vanguard_canary_failed:
                    trace = self._with_entity_canary_trace(trace, self.vanguard_canary.node_events, "BT_VANGUARD_CANARY")
                if use_ranger_tree and not ranger_canary_failed:
                    trace = self._with_entity_canary_trace(trace, self.ranger_canary.node_events, "BT_RANGER_CANARY")
                if use_core_tree and not core_canary_failed and context.core is not None:
                    trace = self._with_entity_canary_trace(trace, {context.core.id: self.core_canary.node_events}, "BT_CORE_CANARY")
                elif worker_canary_failed:
                    trace = replace(
                        trace,
                        goal_summaries=trace.goal_summaries + ({
                            "goal": "WORKER_BT_CANARY", "status": "FALLBACK_LEGACY",
                        },),
                    )
                if vanguard_canary_failed:
                    trace = replace(trace, goal_summaries=trace.goal_summaries + ({
                        "goal": "VANGUARD_BT_CANARY", "status": "FALLBACK_LEGACY",
                    },))
                if ranger_canary_failed:
                    trace = replace(trace, goal_summaries=trace.goal_summaries + ({
                        "goal": "RANGER_BT_CANARY", "status": "FALLBACK_LEGACY",
                    },))
                if core_canary_failed:
                    trace = replace(trace, goal_summaries=trace.goal_summaries + ({
                        "goal": "CORE_BT_CANARY", "status": "FALLBACK_LEGACY",
                    },))
                if objective_shadow:
                    trace = replace(
                        trace,
                        goal_summaries=trace.goal_summaries + tuple(objective_shadow),
                    )
                if prepared_commands is not None and prepared_commands.commands:
                    trace = replace(
                        trace,
                        command_results=prepared_commands.trace_results(outcome="STAGED"),
                        goal_summaries=trace.goal_summaries + (({
                            "goal": "EMERGENCY_STOP", "status": "ACTIVE",
                        },) if prepared_commands.emergency_halt else ()),
                    )
                if command_assignments:
                    trace = replace(
                        trace,
                        goal_summaries=trace.goal_summaries + ({
                            "goal": "MANUAL_ASSIGNMENT", "status": "ACTIVE", "entities": len(command_assignments),
                        },),
                    )
                if schedule is not None:
                    trace = replace(
                        trace,
                        goal_summaries=trace.goal_summaries + ({
                            "goal": "SCHEDULER_CANARY", "status": "ACTIVE",
                            "assignments": len(schedule.assignments), "blocked": len(schedule.blocked),
                        },),
                        task_transitions=trace.task_transitions + self._schedule_task_transitions(
                            schedule, self._last_schedule_tasks
                        ),
                    )
                    trace = self._with_scheduler_assignment_trace(trace, next_memory.scheduler_assignments)
                if shadow is not None:
                    trace = replace(
                        trace,
                        planner_version=f"{trace.planner_version}+scheduler-shadow",
                        goal_summaries=trace.goal_summaries + ({
                            "goal": "SCHEDULER_SHADOW", "status": "OBSERVED",
                            "assignments": len(shadow.assignments), "blocked": len(shadow.blocked),
                            "worker_canary": "DISABLED" if not self.config.worker_bt_canary else "ACTIVE",
                        },),
                        task_transitions=self._schedule_task_transitions(
                            shadow, self._last_shadow_tasks
                        ),
                    )
                if self.config.planner_canary:
                    trace = replace(trace, planner_version="bt-planner-canary-v1")
            except Exception:
                # Observability is best-effort and must never consume the
                # current command window or prevent the already-built plan.
                self.trace_drops += 1
                trace = None
        return DecisionResult(
            mode=mode,
            intents=intents,
            rejected_intents=rejected,
            decision_ms=elapsed_ms,
            action_counts=action_counts(intents),
            wait_reasons=waits,
            next_memory=next_memory,
            timed_out=timed_out or elapsed_ms >= self.config.planning_budget_ms,
            trace=trace,
            prepared_commands=prepared_commands,
        )

    @staticmethod
    def _canary_plan_seed(context: DecisionContext) -> StrategicMode:
        """A controller-free posture label used only when legacy planning is off."""
        if context.core is None:
            return StrategicMode.RESPAWN
        if context.enemies:
            return StrategicMode.DEFEND
        if context.resource_cells:
            return StrategicMode.ECONOMY
        return StrategicMode.EXPLORE

    def _prepare_commands(self, context: DecisionContext) -> PreparedCommands | None:
        if self.command_queue is None:
            return None
        return self.command_queue.prepare_for_tick(
            context.tick, {alias for item in context.current_objects if (alias := entity_alias(item))}
        )

    def _schedule_canary(self, context: DecisionContext, memory: AgentMemory) -> ScheduleResult:
        """Persist a deterministic, controller-free assignment for current actors."""
        actors: list[Actor] = []
        if context.core is not None:
            alias = entity_alias(context.core.id)
            assert alias is not None
            actors.append(Actor(alias, "CORE"))
        for unit in context.units:
            alias = entity_alias(unit.id)
            assert alias is not None
            actors.append(Actor(alias, unit.unit_type.value))
        tasks = self._scheduled_tasks(context, memory, tuple(actors))
        previous = tuple(
            ScheduledAssignment(task.get("task_id", ""), alias, task.get("role", "ANY"),
                                max(0, int(task.get("lease_until_tick", context.tick)) - self.scheduler.lease_ticks),
                                int(task.get("lease_until_tick", context.tick)))
            for alias, task in memory.scheduler_assignments.items()
            if isinstance(task.get("task_id"), str) and isinstance(task.get("role"), str)
        )
        result = self.scheduler.schedule(context.tick, tasks, actors, previous)
        task_by_id = {item.task.task_id: item.task for item in tasks}
        self._last_schedule_tasks = {
            item.task.task_id: self._task_trace_metadata(item) for item in tasks
        }
        memory.scheduler_assignments = {
            assignment.actor_alias: {
                "task_id": assignment.task_id,
                "kind": task_by_id[assignment.task_id].kind,
                "goal": task_by_id[assignment.task_id].goal_id,
                "role": assignment.role,
                "priority": task_by_id[assignment.task_id].priority,
                "assigned_tick": assignment.assigned_tick,
                "lease_until_tick": assignment.lease_until_tick,
                "lock": self._last_schedule_tasks[assignment.task_id]["lock"],
                **({"target": list(task_by_id[assignment.task_id].target)}
                   if isinstance(task_by_id[assignment.task_id].target, (tuple, list))
                   and len(task_by_id[assignment.task_id].target) == 2
                   and all(type(axis) is int for axis in task_by_id[assignment.task_id].target) else {}),
            }
            for assignment in result.assignments if assignment.task_id in task_by_id
        }
        self.last_schedule = result
        return result

    @staticmethod
    def _task_trace_metadata(item: ScheduledTask) -> dict[str, object]:
        """Produce bounded, credential-free task facts for trace and UI."""
        target = item.task.target
        metadata: dict[str, object] = {
            "goal": item.task.goal_id,
            "kind": item.task.kind,
            "lock": item.target_key or f"task:{item.task.task_id}",
        }
        if isinstance(target, (tuple, list)) and len(target) == 2 and all(type(axis) is int for axis in target):
            metadata["target"] = [target[0], target[1]]
        return metadata

    @staticmethod
    def _schedule_task_transitions(
        schedule: ScheduleResult,
        metadata_by_task: dict[str, dict[str, object]],
    ) -> tuple[dict[str, object], ...]:
        """Keep task-to-goal, lock, and lease facts in the redacted trace."""
        transitions: list[dict[str, object]] = []
        for entry in schedule.assignments:
            transitions.append({
                "task_id": entry.task_id,
                "status": "ASSIGNED",
                "actor_alias": entry.actor_alias,
                "role": entry.role,
                "assigned_tick": entry.assigned_tick,
                "lease_until_tick": entry.lease_until_tick,
                **metadata_by_task.get(entry.task_id, {}),
            })
        for entry in schedule.blocked:
            transitions.append({
                "task_id": entry.task_id,
                "status": "BLOCKED",
                "waited_ticks": entry.waited_ticks,
                "reason": entry.reason,
                **metadata_by_task.get(entry.task_id, {}),
            })
        return tuple(transitions)

    def _scheduled_tasks(self, context: DecisionContext, memory: AgentMemory, actors: tuple[Actor, ...]) -> tuple[ScheduledTask, ...]:
        """Make a bounded task set from current resources and accepted overrides."""
        tasks: list[ScheduledTask] = []
        posture = str(memory.policy_state.get("posture", "BALANCED"))
        actor_by_alias = {actor.alias: actor for actor in actors}
        for alias, override in memory.manual_assignments.items():
            actor = actor_by_alias.get(alias)
            if actor is None:
                continue
            target = override.get("target")
            task = Task(f"manual_{alias}", "manual_goal", str(override.get("kind")), TaskStatus.READY,
                        int(override.get("priority", 800)), target=target, required_roles=(actor.role,))
            tasks.append(ScheduledTask(task, utility=1_000.0, target_key=f"manual:{alias}", eligible_aliases=(alias,)))
        workers = sorted(context.workers, key=lambda item: item.id.bytes)
        resources = sorted(context.resource_cells)
        for worker, resource in zip(workers, resources):
            alias = entity_alias(worker.id)
            assert alias is not None
            task = Task(f"harvest_{resource[0]}_{resource[1]}", "economy_goal", "HARVEST_RESOURCE", TaskStatus.READY,
                        700 if posture == "ECONOMY" else 500, target=resource, required_roles=(UnitType.WORKER.value,))
            tasks.append(ScheduledTask(task, utility=(200.0 if posture == "ECONOMY" else 100.0) - distance(worker.position, resource),
                                       target_key=f"resource:{resource[0]},{resource[1]}", eligible_aliases=(alias,)))
        if not resources:
            for worker in workers:
                alias = entity_alias(worker.id)
                assert alias is not None
                task = Task(f"scout_{alias}", "scout_goal", "SCOUT", TaskStatus.READY, 250,
                            required_roles=(UnitType.WORKER.value,))
                tasks.append(ScheduledTask(task, utility=1.0, target_key=f"scout:{alias}", eligible_aliases=(alias,)))
        attack_stage = memory.objective_states.get("attack", {}).get("stage")
        beacon_stage = memory.objective_states.get("beacon", {}).get("stage")
        beacon_active_stages = {
            BeaconStage.ASSEMBLE.value,
            BeaconStage.PICKUP.value,
            BeaconStage.RECOVER.value,
            BeaconStage.EXFIL.value,
            BeaconStage.HOLD.value,
        }
        active_objective = attack_stage in {CoreAttackStage.RALLY.value, CoreAttackStage.ENGAGE.value, CoreAttackStage.RETREAT.value} or beacon_stage in beacon_active_stages
        combat = tuple(sorted((*context.vanguards, *context.rangers), key=lambda item: item.id.bytes))
        if beacon_stage in beacon_active_stages:
            for unit in combat[:2]:
                alias = entity_alias(unit.id)
                assert alias is not None
                task = Task(f"beacon_{alias}", "beacon_goal", "BEACON_ESCORT", TaskStatus.READY, 650,
                            target=context.beacon.position, required_roles=(unit.unit_type.value,))
                tasks.append(ScheduledTask(task, utility=60.0, target_key=f"beacon:{alias}", eligible_aliases=(alias,)))
        target = min((enemy for enemy in context.enemies if isinstance(enemy, CoreView)), key=lambda item: item.id.bytes, default=None)
        if target is not None and attack_stage in {CoreAttackStage.RALLY.value, CoreAttackStage.ENGAGE.value}:
            for unit in combat:
                alias = entity_alias(unit.id)
                assert alias is not None
                task = Task(f"attack_{alias}", "attack_goal", "ATTACK_RALLY", TaskStatus.READY, 880 if posture == "AGGRESSIVE" else 780,
                            target=target.position, required_roles=(unit.unit_type.value,))
                tasks.append(ScheduledTask(task, utility=180.0 if posture == "AGGRESSIVE" else 80.0, target_key=f"attack:{alias}", eligible_aliases=(alias,)))
        if context.core is not None and attack_stage == CoreAttackStage.RETREAT.value:
            for unit in combat:
                alias = entity_alias(unit.id)
                assert alias is not None
                task = Task(f"retreat_{alias}", "attack_goal", "RETREAT", TaskStatus.READY, 900,
                            target=context.core.position, required_roles=(unit.unit_type.value,))
                tasks.append(ScheduledTask(task, utility=200.0, target_key=f"retreat:{alias}", eligible_aliases=(alias,)))
        if context.core is not None and not active_objective:
            slots = ((1, 0), (0, 1), (-1, 0), (0, -1))
            for index, unit in enumerate(combat):
                alias = entity_alias(unit.id)
                assert alias is not None
                slot = context.core.position[0] + slots[index % len(slots)][0], context.core.position[1] + slots[index % len(slots)][1]
                task = Task(f"defend_{alias}", "defense_goal", "DEFEND_CORE", TaskStatus.READY, 950 if posture == "DEFENSIVE" else 850,
                            target=slot, required_roles=(unit.unit_type.value,))
                tasks.append(ScheduledTask(task, utility=110.0 if posture == "DEFENSIVE" else 10.0, target_key=f"defend:{alias}", eligible_aliases=(alias,)))
        return tuple(tasks)

    def _stage_manual_commands(
        self, context: DecisionContext, memory: AgentMemory, prepared: PreparedCommands | None,
    ) -> tuple[str, ...]:
        """Stage bounded manual assignments in next memory, never in HTTP state."""
        if prepared is None:
            return ()
        staged: list[str] = []
        for command in prepared.commands:
            if command.type.value == "UPDATE_POLICY":
                posture = command.payload.get("posture")
                if isinstance(posture, str):
                    # 策略状态更新：posture + 数值字段覆盖
                    policy_update: dict[str, object] = {
                        "version": (command.expected_version or 0) + 1,
                        "posture": posture,
                        "effective_tick": context.tick,
                    }
                    # 合并白名单内的数值字段覆盖
                    for field_name, value in command.payload.items():
                        if (
                            field_name != "posture"
                            and isinstance(value, (int, float))
                            and not isinstance(value, bool)
                        ):
                            policy_update[field_name] = value
                    memory.policy_state = policy_update
                    staged.append("policy")
                continue
            if command.type.value == "TRIGGER_ANALYSIS":
                # 手动触发分析扫描：重置调度器的 last_run_tick 使下次检查立即触发
                task_name = command.payload.get("task_name", "resource_density_scan")
                task = self.analysis_scheduler.get_task(task_name)
                if task is not None:
                    task.last_run_tick = None
                staged.append("analysis")
                continue
            if command.type.value == "START_CORE_MIGRATION":
                target = command.payload.get("target")
                if isinstance(target, (list, tuple)) and len(target) == 2 and all(type(axis) is int for axis in target):
                    memory.objective_states["migration"] = {
                        "stage": "START", "destination": list(target), "start_attempted": False,
                        "replan_count": 0, "manual": True,
                    }
                    staged.append("core")
                continue
            if command.type.value == "CANCEL_CORE_MIGRATION":
                memory.objective_states.pop("migration", None)
                staged.append("core")
                continue
            alias = command.payload.get("entity_alias")
            if not isinstance(alias, str):
                continue
            if command.type.value == "CANCEL":
                memory.manual_assignments.pop(alias, None)
                staged.append(alias)
                continue
            if command.type.value != "ASSIGN_TASK":
                continue
            kind = command.payload.get("task_kind")
            priority = command.payload.get("priority")
            if not isinstance(kind, str) or type(priority) is not int:
                continue
            entry: dict[str, object] = {
                "kind": kind, "priority": priority,
                "until_tick": command.expires_at_tick if command.expires_at_tick is not None else context.tick,
            }
            target = command.payload.get("target")
            if isinstance(target, (list, tuple)) and len(target) == 2 and all(type(axis) is int for axis in target):
                entry["target"] = list(target)
            memory.manual_assignments[alias] = entry
            staged.append(alias)
        return tuple(staged)

    def _apply_manual_assignments(self, context: DecisionContext, memory: AgentMemory, proposals, deadline: float):
        """Give accepted manual tasks priority 800 without overriding safety 900+."""
        replacements: list[ActionIntent] = []
        occupancy = {cell: len(ids) for cell, ids in context.friendly_occupancy.items()}
        reservations = ReservationTable(occupancy)
        for actor in context.current_objects.values():
            alias = entity_alias(actor.id)
            task = memory.manual_assignments.get(alias or "")
            if task is None or self._manual_safety_preempts(context, actor):
                continue
            intent = self._manual_intent(context, memory, actor, task, reservations, deadline)
            if intent is not None:
                replacements.append(intent)
        if not replacements:
            return proposals
        ids = {item.actor_id for item in replacements}
        return tuple(item for item in proposals if item.actor_id not in ids) + tuple(replacements)

    @staticmethod
    def _manual_safety_preempts(context: DecisionContext, actor) -> bool:
        if isinstance(actor, CoreView):
            return actor.state is CoreState.MOVING or actor.hp <= 1
        return actor.hp <= 1 or any(distance(actor.position, enemy.position) <= 2 for enemy in context.enemies)

    def _manual_intent(self, context: DecisionContext, memory: AgentMemory, actor, task, reservations: ReservationTable, deadline: float) -> ActionIntent | None:
        kind = task.get("kind")
        score = float(task.get("priority", 800))
        if kind == "HOLD_POSITION":
            return ActionIntent(actor.id, isinstance(actor, CoreView), ActionKind.WAIT, score, "manual_hold_position")
        if context.core is None or isinstance(actor, CoreView):
            return None
        if kind == "RETREAT_TO_CORE":
            if actor.position == context.core.position and actor.unit_type is UnitType.WORKER and (actor.cargo or 0) and context.resource_space > 0:
                return ActionIntent(actor.id, False, ActionKind.DEPOSIT, score, "manual_retreat_deposit")
            target = context.core.position
        elif kind == "HARVEST_VISIBLE":
            if actor.unit_type is not UnitType.WORKER:
                return None
            if actor.position in context.resource_cells and not (actor.cargo or 0):
                return ActionIntent(actor.id, False, ActionKind.HARVEST, score, "manual_harvest_visible")
            if not context.resource_cells or (actor.cargo or 0):
                return None
            target = min(context.resource_cells, key=lambda cell: (distance(actor.position, cell), cell))
        elif kind == "MOVE_TO_CELL":
            raw_target = task.get("target")
            if not isinstance(raw_target, (list, tuple)) or len(raw_target) != 2 or not all(type(axis) is int for axis in raw_target):
                return None
            target = raw_target[0], raw_target[1]
            if actor.position == target:
                return ActionIntent(actor.id, False, ActionKind.WAIT, score, "manual_target_reached")
        else:
            return None
        direction = plan_step(
            actor_id=actor.id, start=actor.position, goal=target, context=context,
            persistent_obstacles=memory.obstacles, reservations=reservations,
            deadline=deadline, config=self.config, avoid_threats=True,
        )
        if direction is None:
            return ActionIntent(actor.id, False, ActionKind.WAIT, score, "manual_route_blocked")
        return ActionIntent(actor.id, False, ActionKind.MOVE, score, "manual_task_move", direction=direction,
                            target_cell=target, reserved_cell=destination(actor.position, direction))

    def _run_due_analyses(
        self, context: DecisionContext, memory: AgentMemory
    ) -> None:
        """Check for due analysis tasks and execute them.

        Called every tick after ``memory.advance()``.  Results are written
        directly into *memory* so that subsequent planning can consume them.
        """
        explored_count = len(memory.explored)
        resource_count = len(memory.resource_observations)
        due = self.analysis_scheduler.advance(
            context.tick, explored_count, resource_count
        )
        if not due:
            return
        for task_name in due:
            if task_name == "resource_density_scan":
                self._execute_resource_density_scan(context, memory)
            self.analysis_scheduler.mark_completed(task_name, context.tick)
        # Persist scheduler state back to memory.
        memory.analysis_tasks = self.analysis_scheduler.to_dict()

    def _execute_resource_density_scan(
        self, context: DecisionContext, memory: AgentMemory
    ) -> None:
        """Run rich_resource_center (top-3) and cache the result with candidates."""
        if not memory.resource_observations:
            return
        # 获取 top-3 候选桶
        candidates = rich_resource_center(
            memory.resource_observations, current_tick=context.tick, top_n=3,
        )
        if not candidates:
            return
        # 以最佳候选作为推荐中心
        best = candidates[0]
        center: Position = best["center"]  # type: ignore[assignment]
        # 用 migration_site_score 对中心评分
        score = migration_site_score(
            center,
            resource_observations=memory.resource_observations,
            obstacles=memory.obstacles,
            explored=memory.explored,
            current_tick=context.tick,
        )
        task = self.analysis_scheduler.get_task("resource_density_scan")
        interval = task.current_interval(
            len(memory.explored), len(memory.resource_observations)
        ) if task else 60
        rec = MigrationRecommendation(
            center=center,  # type: ignore[arg-type]
            score=score,
            computed_at_tick=context.tick,
            interval_ticks=interval,
        )
        # 缓存推荐 + top-3 候选
        rec_dict = rec.to_dict()
        rec_dict["candidates"] = [
            {
                "center": list(c["center"]),  # type: ignore[arg-type]
                "score": round(float(c["score"]), 2),  # type: ignore[arg-type]
                "resource_count": c["resource_count"],
                "resources": [list(r) for r in c["resources"][:20]],  # type: ignore[arg-type]
            }
            for c in candidates
        ]
        memory.migration_recommendation = rec_dict

    @staticmethod
    def _emergency_waits(context: DecisionContext) -> tuple[ActionIntent, ...]:
        """A queued emergency override owns all current objects for this Tick."""
        intents: list[ActionIntent] = []
        if context.core is not None:
            intents.append(ActionIntent(context.core.id, True, ActionKind.WAIT, 950, "emergency_stop_next_tick"))
        intents.extend(
            ActionIntent(unit.id, False, ActionKind.WAIT, 950, "emergency_stop_next_tick")
            for unit in context.units
        )
        return tuple(intents)

    def _apply_beacon_campaign(
        self,
        context: DecisionContext,
        memory: AgentMemory,
        proposals,
        mode: StrategicMode,
        *,
        deadline: float | None = None,
    ):
        """Acquire only in BEACON mode, then exfil the public carrier home."""
        if not self.config.beacon_campaign_v1 or context.core is None:
            return proposals
        state = memory.objective_states.get("beacon", {})
        stage = state.get("stage")
        acquisition_stages = {
            BeaconStage.ASSEMBLE.value,
            BeaconStage.RECOVER.value,
            BeaconStage.PICKUP.value,
        }
        exfil_stages = {BeaconStage.EXFIL.value, BeaconStage.HOLD.value}
        carrier = context.current_objects.get(context.beacon.carrier_id)

        # Global posture owns campaign activation.  A shadow lifecycle must not
        # commandeer arbitrary combat units during DEFEND/RECOVER/ATTACK.
        if stage in acquisition_stages and mode is not StrategicMode.BEACON:
            return proposals
        if stage in exfil_stages and (
            mode is not StrategicMode.BEACON or carrier is None
        ):
            return proposals

        # Once secured, role planners own the former escorts through the
        # MINING_ESCORT squad.  The campaign only spends surplus resources on
        # the raised Core shield cap and never generates DROP_BEACON.
        if stage == BeaconStage.SECURE.value:
            if (
                mode in {StrategicMode.ECONOMY, StrategicMode.EXPLORE}
                and context.core.shield < 10
                and context.resources > self.config.minimum_resource_reserve
            ):
                intent = ActionIntent(
                    context.core.id,
                    True,
                    ActionKind.REPAIR_SHIELD,
                    700,
                    "beacon_secure_core_repair",
                )
                return self._replace_objective_proposals(proposals, [intent])
            return proposals

        replacements: list[ActionIntent] = []
        proposal_by_actor = {item.actor_id: item for item in proposals}
        planned_expedition = build_squad_plan(
            context, memory, self.config
        ).squads.get(SQUAD_ID_BY_TYPE[SquadType.EXPEDITION_BEACON])
        retained_aliases = {
            str(alias) for alias in state.get("escort_aliases", ())
            if isinstance(alias, str)
        }
        if retained_aliases:
            escort_units = tuple(sorted(
                (
                    unit for unit in context.units
                    if entity_alias(unit.id) in retained_aliases
                    and unit.unit_type in (UnitType.VANGUARD, UnitType.RANGER)
                ),
                key=lambda unit: unit.id.bytes,
            ))
        else:
            planned_ids = (
                planned_expedition.member_ids
                if planned_expedition is not None else set()
            )
            escort_units = tuple(sorted(
                (
                    unit for unit in context.units
                    if unit.id in planned_ids
                    and unit.unit_type in (UnitType.VANGUARD, UnitType.RANGER)
                ),
                key=lambda unit: unit.id.bytes,
            ))
            if escort_units:
                state["escort_aliases"] = [
                    alias for unit in escort_units
                    if (alias := entity_alias(unit.id)) is not None
                ]

        if isinstance(carrier, UnitView) and carrier.id not in {
            unit.id for unit in escort_units
        }:
            escort_units = tuple(sorted((*escort_units, carrier), key=lambda unit: unit.id.bytes))
            alias = entity_alias(carrier.id)
            if alias is not None:
                state["escort_aliases"] = list(dict.fromkeys((
                    *state.get("escort_aliases", ()), alias,
                )))

        if not escort_units:
            return proposals
        mobile_escort_units = tuple(
            unit for unit in escort_units
            if not self._manual_safety_preempts(context, unit)
            and not intent_is_squad_protected(proposal_by_actor.get(unit.id))
        )
        campaign_squad = Squad(
            squad_id=SQUAD_ID_BY_TYPE[SquadType.EXPEDITION_BEACON],
            squad_type=SquadType.EXPEDITION_BEACON,
            target=(
                context.core.position
                if stage in exfil_stages else context.beacon.position
            ),
            members=tuple(SquadMember(
                unit.id,
                unit.unit_type,
                SquadRole.POINT_GUARD
                if unit.unit_type is UnitType.VANGUARD
                else SquadRole.FIRE_SUPPORT,
            ) for unit in escort_units),
            anchor_unit_id=(
                carrier.id if isinstance(carrier, UnitView)
                else next(
                    (
                        unit.id for unit in escort_units
                        if unit.unit_type is UnitType.VANGUARD
                    ),
                    escort_units[0].id,
                )
            ),
        )
        reservations = ReservationTable({cell: len(ids) for cell, ids in context.friendly_occupancy.items()})
        if stage in acquisition_stages:
            # Keep the escort roster stable and bounded.  A carrier may only
            # pick up after the lifecycle has observed its current-cell facts.
            for index, unit in enumerate(mobile_escort_units):
                target = context.beacon.position if index == 0 else self._beacon_escort_slot(
                    context, index, actor_cell=unit.position
                )
                if unit.position == target:
                    continue
                if intent := self._objective_move(context, memory, unit, target, 620,
                                                  "beacon_campaign_escort", avoid_threats=True,
                                                  reservations=reservations, deadline=deadline):
                    replacements.append(intent)
        if stage == BeaconStage.PICKUP.value and context.beacon.status is BeaconStatus.GROUND:
            candidates = sorted(
                (unit for unit in mobile_escort_units if unit.position == context.beacon.position),
                key=lambda unit: ({UnitType.VANGUARD: 0, UnitType.RANGER: 1, UnitType.WORKER: 2}[unit.unit_type], unit.id.bytes),
            )
            if candidates:
                carrier = candidates[0]
                intent = ActionIntent(carrier.id, False, ActionKind.PICKUP_BEACON, 700, "beacon_campaign_pickup_current_ground", target_cell=context.beacon.position)
                replacements = [item for item in replacements if item.actor_id != carrier.id] + [intent]
        if (
            stage in exfil_stages
            and context.core.shield < 10
            and context.resources > self.config.minimum_resource_reserve
        ):
            intent = ActionIntent(
                context.core.id, True, ActionKind.REPAIR_SHIELD, 700,
                "beacon_exfil_core_repair",
            )
            replacements.append(intent)
        combined = self._replace_objective_proposals(proposals, replacements)
        return coordinate_expedition_intents(
            context,
            memory,
            self.config,
            campaign_squad,
            tuple(combined),
            deadline=deadline,
            contact_holds=stage not in exfil_stages,
            reason_prefix=(
                "beacon_exfil" if stage in exfil_stages else "expedition"
            ),
            allow_single_maneuver=stage in exfil_stages,
            override_intercept_moves=True,
        )

    def _apply_core_migration(self, context: DecisionContext, memory: AgentMemory, proposals, *, deadline: float | None = None):
        """Start one safe observed leg only; later Turns drive all progress."""
        if (not self.config.core_migration_v1 and not memory.objective_states.get("migration", {}).get("manual")) or context.core is None:
            return proposals
        state = memory.objective_states.get("migration", {})
        if state.get("stage") == MigrationStage.RECALL.value:
            replacements: list[ActionIntent] = []
            for worker in context.workers:
                if not (worker.cargo or 0):
                    continue
                if worker.position == context.core.position:
                    if context.resource_space > 0:
                        replacements.append(ActionIntent(worker.id, False, ActionKind.DEPOSIT, 650,
                                                         "core_migration_deposit_cargo"))
                    continue
                if intent := self._objective_move(context, memory, worker, context.core.position, 650,
                                                  "core_migration_recall_cargo", avoid_threats=True, deadline=deadline):
                    replacements.append(intent)
            return self._replace_objective_proposals(proposals, replacements)
        if (
            not state.get("manual")
            and context.tick <= memory.migration_cooldown_until_tick
        ):
            return proposals
        if any((worker.cargo or 0) > 0 for worker in context.workers):
            # The lifecycle evaluator will enter RECALL from this fresh Turn.
            # Never allow an ordinary leg to race a cargo deposit meanwhile.
            return proposals
        if state.get("stage") != MigrationStage.START.value or context.core.state is not CoreState.NORMAL:
            return proposals
        raw_destination = state.get("destination")
        if not isinstance(raw_destination, list) or len(raw_destination) != 2:
            return proposals
        target = raw_destination[0], raw_destination[1]
        choices = sorted(DIRECTIONS, key=lambda direction: (distance(destination(context.core.position, direction), target), direction.value))
        safe_choices = [item for item in choices if destination(context.core.position, item) not in context.obstacle_cells | context.resource_cells | set(context.enemy_occupancy) | set(context.friendly_occupancy)]
        forward_choices = [item for item in safe_choices if destination(context.core.position, item) != memory.previous_migration_position]
        direction = next(iter(forward_choices or safe_choices), None)
        if direction is None:
            state["start_attempted"] = False
            return proposals
        intent = ActionIntent(context.core.id, True, ActionKind.START_MOVE, 650, "core_migration_start_safe_leg", direction=direction, reserved_cell=destination(context.core.position, direction))
        return tuple(item for item in proposals if not item.is_core) + (intent,)

    def _apply_core_attack(self, context: DecisionContext, memory: AgentMemory, proposals, *, deadline: float | None = None):
        """Rally, engage and retreat only from this authoritative Turn."""
        if not self.config.core_attack_campaign_v1:
            return proposals
        stage = memory.objective_states.get("attack", {}).get("stage")
        if stage == CoreAttackStage.RETREAT.value:
            if context.core is None:
                return proposals
            replacements = [
                intent for unit in (*context.vanguards, *context.rangers)
                if not self._manual_safety_preempts(context, unit)
                if (intent := self._objective_move(context, memory, unit, context.core.position, 900, "core_attack_retreat", avoid_threats=True, deadline=deadline))
            ]
            return self._replace_objective_proposals(proposals, replacements)
        if stage not in {CoreAttackStage.RALLY.value, CoreAttackStage.ENGAGE.value}:
            return proposals
        target = min((enemy for enemy in context.enemies if isinstance(enemy, CoreView)), key=lambda item: item.id.bytes, default=None)
        if target is None:
            return proposals
        replacements: list[ActionIntent] = []
        for ranger in context.rangers:
            if ranger.hp > 1 and shot_range(ranger.position, target.position, context.obstacle_cells) is not None:
                replacements.append(ActionIntent(ranger.id, False, ActionKind.SHOOT, 780, "core_attack_current_visible_fire", target_id=target.id, target_cell=target.position))
        for vanguard in context.vanguards:
            direction = adjacent_direction(vanguard.position, target.position)
            if vanguard.hp > 1 and direction is not None:
                replacements.append(ActionIntent(vanguard.id, False, ActionKind.SWEEP, 780, "core_attack_current_visible_sweep", direction=direction, target_cell=target.position))
        if stage == CoreAttackStage.RALLY.value:
            for unit in (*context.vanguards, *context.rangers):
                if unit.id in {intent.actor_id for intent in replacements} or self._manual_safety_preempts(context, unit):
                    continue
                if intent := self._objective_move(context, memory, unit, target.position, 760, "core_attack_rally", avoid_threats=True, deadline=deadline):
                    replacements.append(intent)
        return self._replace_objective_proposals(proposals, replacements)

    @staticmethod
    def _beacon_escort_slot(
        context: DecisionContext, index: int, *, center=None, actor_cell=None
    ):
        """Give non-carrier escorts distinct, currently non-hostile formation slots."""
        anchor = center or context.beacon.position
        candidates = tuple(destination(anchor, direction) for direction in DIRECTIONS)
        safe = tuple(cell for cell in candidates if cell not in context.obstacle_cells and cell not in context.enemy_occupancy)
        available = safe or candidates
        if actor_cell is not None:
            available = tuple(sorted(available, key=lambda cell: (
                0 if cell[0] == actor_cell[0] or cell[1] == actor_cell[1] else 1,
                distance(actor_cell, cell),
                cell,
            )))
        return available[(index - 1) % len(available)]

    def _objective_move(self, context: DecisionContext, memory: AgentMemory, unit, target, score: float, reason: str, *, avoid_threats: bool, reservations: ReservationTable | None = None, deadline: float | None = None) -> ActionIntent | None:
        if unit.position == target:
            return None
        reservations = reservations or ReservationTable({cell: len(ids) for cell, ids in context.friendly_occupancy.items()})
        if deadline is None:
            deadline = perf_counter() + self.config.planning_budget_ms / 1_000
        direction = plan_step(actor_id=unit.id, start=unit.position, goal=target, context=context,
                              persistent_obstacles=memory.obstacles, reservations=reservations,
                              deadline=deadline, config=self.config, avoid_threats=avoid_threats)
        if direction is None:
            return None
        return ActionIntent(unit.id, False, ActionKind.MOVE, score, reason, direction=direction,
                            target_cell=target, reserved_cell=destination(unit.position, direction))

    @staticmethod
    def _replace_objective_proposals(proposals, replacements: list[ActionIntent]):
        if not replacements:
            return proposals
        actor_ids = {intent.actor_id for intent in replacements}
        return tuple(item for item in proposals if item.actor_id not in actor_ids) + tuple(replacements)

    def _objective_shadow(
        self, context: DecisionContext, memory: AgentMemory
    ) -> tuple[dict[str, object], ...]:
        """Evaluate enabled Phase 4–6 lifecycles without proposing SDK actions."""
        summaries: list[dict[str, object]] = []
        if self.config.beacon_campaign_v1:
            prior = memory.objective_states.get("beacon", {})
            try:
                campaign = BeaconCampaign(
                    BeaconStage(prior.get("stage", BeaconStage.ASSEMBLE)),
                    prior.get("carrier_alias"),
                    tuple(prior["recovery_cell"]) if "recovery_cell" in prior else None,
                )
            except (TypeError, ValueError):
                campaign = BeaconCampaign()
            carrier = context.beacon.carrier_id
            own_carrier = entity_alias(carrier) if carrier in context.current_objects else None
            carrier_alive = campaign.carrier_alias is None or campaign.carrier_alias == own_carrier
            carrier_object = context.current_objects.get(carrier)
            carrier_secured = (
                own_carrier is not None
                and carrier_object is not None
                and context.core is not None
                and context.core.state is CoreState.NORMAL
                and distance(carrier_object.position, context.core.position)
                <= max(0, self.config.beacon_secure_radius)
            )
            escort_ready = sum(
                unit.unit_type in (UnitType.VANGUARD, UnitType.RANGER)
                and distance(unit.position, context.beacon.position) <= 4
                for unit in context.units
            )
            next_campaign, goal, tasks, candidates = campaign.evaluate(BeaconInput(
                context.tick, context.beacon.position,
                context.beacon.status is BeaconStatus.GROUND, own_carrier, carrier_alive,
                escort_ready, 2, holding=own_carrier is not None,
                carrier_secured=carrier_secured,
            ))
            memory.objective_states["beacon"] = {
                "stage": next_campaign.stage.value,
                **({"carrier_alias": next_campaign.carrier_alias} if next_campaign.carrier_alias else {}),
                **({"recovery_cell": list(next_campaign.recovery_cell)} if next_campaign.recovery_cell else {}),
                **({"escort_aliases": list(prior["escort_aliases"])}
                   if isinstance(prior.get("escort_aliases"), (list, tuple)) else {}),
            }
            summaries.append({"goal": goal.kind, "status": "SHADOW", "stage": next_campaign.stage.value,
                              "tasks": tuple(task.kind for task in tasks), "candidates": candidates})

        core = context.core
        if (self.config.core_migration_v1 or memory.objective_states.get("migration", {}).get("manual")) and core is not None:
            prior = memory.objective_states.get("migration", {})
            raw_destination = prior.get("destination", context.beacon.position)
            destination = tuple(raw_destination) if isinstance(raw_destination, (list, tuple)) else context.beacon.position
            try:
                plan = CoreMigrationPlan(destination, MigrationStage(prior.get("stage", MigrationStage.RECALL)),
                                         bool(prior.get("start_attempted", False)), int(prior.get("replan_count", 0)))
            except (TypeError, ValueError):
                plan = CoreMigrationPlan(context.beacon.position)
            cargo_pending = sum((worker.cargo or 0) > 0 for worker in context.workers)
            move_failed = any(event.event_type == "CORE_MOVE_FAILED" for event in context.events)
            next_plan, goal, tasks, candidates = plan.evaluate(CoreMigrationInput(
                context.tick, plan.destination, cargo_pending, context.resource_capacity, context.resources,
                core.state is CoreState.MOVING, core.move_progress, move_failed,
                arrived=core.position == plan.destination,
            ))
            memory.objective_states["migration"] = {"stage": next_plan.stage.value,
                "destination": list(next_plan.destination), "start_attempted": next_plan.start_attempted,
                "replan_count": next_plan.replan_count, "manual": bool(prior.get("manual"))}
            summaries.append({"goal": goal.kind, "status": "SHADOW", "stage": next_plan.stage.value,
                              "tasks": tuple(task.kind for task in tasks), "candidates": candidates})

        if self.config.core_attack_campaign_v1:
            prior = memory.objective_states.get("attack", {})
            try:
                campaign = CoreAttackCampaign(CoreAttackStage(prior.get("stage", CoreAttackStage.RALLY)))
            except ValueError:
                campaign = CoreAttackCampaign()
            target = min((enemy for enemy in context.enemies if isinstance(enemy, CoreView)), key=lambda item: item.id.bytes, default=None)
            target_alias = entity_alias(target.id) if target is not None else prior.get("target_alias")
            core_destroyed = bool(target_alias) and any(
                event.event_type == "CORE_DESTROYED"
                and event.reason_code == "ATTACK"
                and entity_alias(event.target_id) == target_alias
                for event in context.events
            )
            core_destroyed = core_destroyed or (
                campaign.stage is CoreAttackStage.CONFIRMED and target is None
            )
            if target is not None or campaign.stage is not CoreAttackStage.RALLY:
                rally_ready = sum(unit.unit_type in (UnitType.VANGUARD, UnitType.RANGER)
                                  and target is not None and distance(unit.position, target.position) <= 5 for unit in context.units)
                ranger_slot = target is not None and any(shot_range(unit.position, target.position, context.obstacle_cells) is not None for unit in context.rangers)
                vanguard_adjacent = target is not None and any(distance(unit.position, target.position) == 1 for unit in context.vanguards)
                force_retreat = core is None or core.hp <= 1
                next_campaign, goal, tasks, candidates = campaign.evaluate(CoreAttackInput(
                    context.tick, target.position if target else None, target is not None, rally_ready, 3,
                    ranger_slot, vanguard_adjacent, force_retreat, core_destroyed,
                ))
                memory.objective_states["attack"] = {
                    "stage": next_campaign.stage.value,
                    **({"target_alias": target_alias} if target_alias else {}),
                }
                summaries.append({"goal": goal.kind, "status": "SHADOW", "stage": next_campaign.stage.value,
                                  "tasks": tuple(task.kind for task in tasks), "candidates": candidates})
        return tuple(summaries)

    @staticmethod
    def _with_worker_canary_trace(trace, result: WorkerCanaryResult):
        """Overlay controller-free BT events onto the legacy compatibility trace."""
        events_by_alias = {
            entity_alias(worker_id): events
            for worker_id, events in result.node_events.items()
        }
        entities: list[EntityTrace] = []
        for entity in trace.entity_traces:
            events = events_by_alias.get(entity.actor_alias)
            if events is None:
                entities.append(entity)
                continue
            nodes = tuple(
                NodeTrace(event.node_id, event.status.value, event.reason)
                for event in events
            )
            status = events[-1].status.value if events else entity.status
            entities.append(replace(
                entity,
                current_task="BT_WORKER_CANARY",
                status=status,
                task_status="RUNNING",
                assignment_status="CANARY",
                node_path=nodes,
            ))
        return replace(
            trace,
            planner_version="legacy-strategy-v1+worker-bt-canary",
            entity_traces=tuple(entities),
            goal_summaries=trace.goal_summaries + ({
                "goal": "WORKER_BT_CANARY", "status": "ACTIVE",
                "entities": len(result.node_events),
            },),
        )

    @staticmethod
    def _with_entity_canary_trace(trace, events_by_id, task_name: str):
        aliases = {entity_alias(actor_id): events for actor_id, events in events_by_id.items()}
        entities = []
        for entity in trace.entity_traces:
            events = aliases.get(entity.actor_alias)
            if events is None:
                entities.append(entity)
                continue
            nodes = tuple(NodeTrace(event.node_id, event.status.value, event.reason) for event in events)
            entities.append(replace(entity, current_task=task_name, task_status="RUNNING", assignment_status="CANARY", node_path=nodes))
        return replace(trace, entity_traces=tuple(entities))

    @staticmethod
    def _with_scheduler_assignment_trace(trace, assignments):
        entities: list[EntityTrace] = []
        for entity in trace.entity_traces:
            task = assignments.get(entity.actor_alias) if isinstance(assignments, dict) else None
            if not isinstance(task, dict):
                entities.append(entity)
                continue
            target = task.get("target")
            target_cell = tuple(target) if isinstance(target, (list, tuple)) and len(target) == 2 and all(type(axis) is int for axis in target) else entity.target_cell
            entities.append(replace(
                entity, current_task=str(task.get("kind", entity.current_task)), task_status="RUNNING",
                assignment_status="SCHEDULED", target_cell=target_cell,
                assignment={"task_id": task.get("task_id"), "role": task.get("role"),
                            "goal": task.get("goal"), "lock": task.get("lock"),
                            "assigned_tick": task.get("assigned_tick"),
                            "lease_until_tick": task.get("lease_until_tick")},
            ))
        return replace(trace, entity_traces=tuple(entities))

    def commit(self, result: DecisionResult) -> None:
        """Persist only after the caller confirms successful submission."""
        result.next_memory.submitted_ticks += 1
        result.next_memory.accepted_ticks += 1
        self.memory = result.next_memory
        if self.memory_store is not None:
            self.memory_store.save(self.memory)
        trace = result.trace
        if self.command_queue is not None and result.prepared_commands is not None:
            changed = self.command_queue.finalize(result.prepared_commands, accepted=True)
            if trace is not None and changed:
                trace = replace(trace, command_results=tuple({
                    "command_id": command.command_id, "type": command.type.value,
                    "status": command.status.value, "tick": result.prepared_commands.tick,
                } for command in changed))
        if self.trace_sink is not None and trace is not None:
            self.trace_sink.emit(trace)

    def close(self) -> None:
        if self.trace_sink is not None:
            self.trace_sink.close()

    def _scheduler_shadow(self, context: DecisionContext) -> ScheduleResult:
        """Project only current entities into scheduler inputs; never queue actions."""
        tasks = []
        actors = []
        for unit in context.units:
            alias = entity_alias(unit.id)
            assert alias is not None
            role = unit.unit_type.value
            goal_id = f"shadow_goal_{alias}"
            task_id = f"shadow_task_{alias}"
            tasks.append(ScheduledTask(Task(task_id, goal_id, "LEGACY_SHADOW", TaskStatus.READY, 250,
                                            required_roles=(role,)), utility=0.0, target_key=f"entity:{alias}"))
            actors.append(Actor(alias, role))
        self._last_shadow_tasks = {
            item.task.task_id: self._task_trace_metadata(item) for item in tasks
        }
        self.last_shadow = self.scheduler.schedule(context.tick, tasks, actors)
        return self.last_shadow


def choose_actions(
    turn: Turn,
    *,
    memory: AgentMemory | None = None,
    config: AgentConfig | None = None,
) -> DecisionResult:
    """Compatibility entry: queue one plan without performing persistence."""
    return AgentRuntime(memory=memory, config=config).decide(turn)
