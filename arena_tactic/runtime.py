"""Synchronous per-Turn runtime that separates decision and persistence."""

from __future__ import annotations

from time import perf_counter

from arena_hero import Turn

from .allocator import action_counts, apply_intents
from .context import DecisionContext
from .memory import AgentMemory, MemoryStore
from .models import AgentConfig, DecisionResult
from .strategy import propose_intents
from .validation import validate_intents


class AgentRuntime:
    def __init__(
        self,
        *,
        memory: AgentMemory | None = None,
        config: AgentConfig | None = None,
        memory_store: MemoryStore | None = None,
    ) -> None:
        self.config = config or AgentConfig()
        self.memory_store = memory_store
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
        return DecisionResult(
            mode=mode,
            intents=intents,
            rejected_intents=rejected,
            decision_ms=elapsed_ms,
            action_counts=action_counts(intents),
            wait_reasons=waits,
            next_memory=next_memory,
            timed_out=timed_out or elapsed_ms >= self.config.planning_budget_ms,
        )

    def commit(self, result: DecisionResult) -> None:
        """Persist only after the caller confirms successful submission."""
        result.next_memory.submitted_ticks += 1
        result.next_memory.accepted_ticks += 1
        self.memory = result.next_memory
        if self.memory_store is not None:
            self.memory_store.save(self.memory)


def choose_actions(
    turn: Turn,
    *,
    memory: AgentMemory | None = None,
    config: AgentConfig | None = None,
) -> DecisionResult:
    """Compatibility entry: queue one plan without performing persistence."""
    return AgentRuntime(memory=memory, config=config).decide(turn)
