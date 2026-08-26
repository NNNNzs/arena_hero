"""Issue-3 runtime integration layer.

This keeps the legacy runtime implementation stable while making committed
policy overrides and persistent squad membership authoritative for live play.
"""

from __future__ import annotations

from dataclasses import replace

from arena_hero import Turn

from .command_center import PreparedCommands
from .context import DecisionContext
from .memory import AgentMemory
from .models import AgentConfig, DecisionResult
from .runtime import AgentRuntime as _BaseAgentRuntime


class AgentRuntime(_BaseAgentRuntime):
    """Runtime using one committed effective config for each authoritative Tick."""

    _CONFIG_OVERRIDE_FIELDS = _BaseAgentRuntime._CONFIG_OVERRIDE_FIELDS | frozenset({
        "expedition_vanguards",
        "expedition_rangers",
        "mining_escort_vanguards",
        "mining_escort_rangers",
        "scout_vanguards",
        "scout_rangers",
    })

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        # Keep an immutable startup baseline. Rebuilding from this baseline is
        # important when an operator later removes/changes an override; using
        # the previous Tick's effective config would make stale values sticky.
        self._base_config = self.config

    def _apply_config_overrides(self, memory: AgentMemory) -> AgentConfig:
        overrides: dict[str, int | float] = {}
        for field_name in self._CONFIG_OVERRIDE_FIELDS:
            value = memory.policy_state.get(field_name)
            if (
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and hasattr(self._base_config, field_name)
            ):
                overrides[field_name] = value
        return replace(self._base_config, **overrides) if overrides else self._base_config

    def decide(self, turn: Turn) -> DecisionResult:
        # Only already-committed memory may affect this Tick. UPDATE_POLICY is
        # staged later by the base runtime and therefore takes effect on the
        # following Tick after a successful submit/commit, matching the UI.
        self.config = self._apply_config_overrides(self.memory)
        self._effective_config = self.config
        return super().decide(turn)

    def _stage_manual_commands(
        self,
        context: DecisionContext,
        memory: AgentMemory,
        prepared: PreparedCommands | None,
    ) -> tuple[str, ...]:
        if prepared is None:
            return ()

        # Let the mature manual-task/policy/objective staging implementation
        # handle every command it already understands. ASSIGN_SQUAD is handled
        # separately because squad membership is persistent, not a TTL task.
        ordinary_commands = tuple(
            command for command in prepared.commands
            if command.type.value != "ASSIGN_SQUAD"
        )
        ordinary = PreparedCommands(
            tick=prepared.tick,
            commands=ordinary_commands,
            transitions=prepared.transitions,
            emergency_halt=prepared.emergency_halt,
        )
        staged = list(super()._stage_manual_commands(context, memory, ordinary))

        for command in prepared.commands:
            alias = command.payload.get("entity_alias")
            if command.type.value == "ASSIGN_SQUAD":
                squad_id = command.payload.get("squad_id")
                if isinstance(alias, str) and isinstance(squad_id, str):
                    memory.manual_squad_assignments[alias] = squad_id
                    # A previously queued manual action must not hide the newly
                    # selected squad's normal planner behavior.
                    memory.manual_assignments.pop(alias, None)
                    staged.append(alias)
            elif command.type.value == "CANCEL" and isinstance(alias, str):
                # CANCEL restores full automatic control for both temporary
                # manual tasks and persistent squad overrides.
                memory.manual_squad_assignments.pop(alias, None)

        return tuple(dict.fromkeys(staged))


def choose_actions(
    turn: Turn,
    *,
    memory: AgentMemory | None = None,
    config: AgentConfig | None = None,
) -> DecisionResult:
    """Compatibility entry using the integrated runtime."""
    return AgentRuntime(memory=memory, config=config).decide(turn)
