"""Synchronous per-Turn runtime that separates decision and persistence."""

from __future__ import annotations

from time import perf_counter
from dataclasses import replace

from arena_hero import Turn

from .allocator import action_counts, apply_intents
from .context import DecisionContext
from .domain import BoundedTraceSink, Task, TaskStatus, TraceLimits
from .identity import entity_alias
from .memory import AgentMemory, MemoryStore
from .models import AgentConfig, DecisionResult
from .planning import LegacyPlannerAdapter
from .scheduler import Actor, DeterministicScheduler, ScheduledTask, ScheduleResult
from .strategy import propose_intents
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
    ) -> None:
        self.config = config or AgentConfig()
        self.memory_store = memory_store
        self.enable_trace = enable_trace
        self.trace_limits = trace_limits or TraceLimits()
        self.trace_sink = trace_sink
        self.trace_drops = 0
        self.legacy_planner = LegacyPlannerAdapter()
        self.scheduler = DeterministicScheduler()
        self.last_shadow: ScheduleResult | None = None
        self.memory = memory or (
            memory_store.load() if memory_store is not None else AgentMemory()
        )

    def decide(self, turn: Turn) -> DecisionResult:
        started = perf_counter()
        deadline = started + self.config.planning_budget_ms / 1_000
        context = DecisionContext.from_turn(turn)
        next_memory = self.memory.advance(context, self.config)
        mode, proposals, timed_out = propose_intents(
            context, next_memory, self.config, deadline
        )
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
                if shadow is not None:
                    trace = replace(
                        trace,
                        planner_version="legacy-strategy-v1+scheduler-shadow",
                        goal_summaries=trace.goal_summaries + ({
                            "goal": "SCHEDULER_SHADOW", "status": "OBSERVED",
                            "assignments": len(shadow.assignments), "blocked": len(shadow.blocked),
                            "worker_canary": "DISABLED" if not self.config.worker_bt_canary else "NOT_IMPLEMENTED",
                        },),
                        task_transitions=tuple(
                            {"task_id": entry.task_id, "status": "ASSIGNED", "actor_alias": entry.actor_alias}
                            for entry in shadow.assignments
                        ) + tuple(
                            {"task_id": entry.task_id, "status": "BLOCKED", "waited_ticks": entry.waited_ticks,
                             "reason": entry.reason}
                            for entry in shadow.blocked
                        ),
                    )
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
        )

    def commit(self, result: DecisionResult) -> None:
        """Persist only after the caller confirms successful submission."""
        result.next_memory.submitted_ticks += 1
        result.next_memory.accepted_ticks += 1
        self.memory = result.next_memory
        if self.memory_store is not None:
            self.memory_store.save(self.memory)
        if self.trace_sink is not None and result.trace is not None:
            self.trace_sink.emit(result.trace)

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
