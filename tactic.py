"""Arena Hero SDK loop for the adaptive local tactic."""

from __future__ import annotations

import os
from getpass import getpass
from pathlib import Path

from arena_hero import ArenaHeroClient, Turn

from arena_tactic import AgentRuntime, DecisionResult, MemoryStore
from arena_tactic.context import DecisionContext
from arena_tactic.observability import ReplayWriter, summary_line
from arena_tactic.runtime import choose_actions

__all__ = ["choose_actions", "play"]


def play(api_key: str) -> None:
    """Run synchronously until the SDK stream closes or the user interrupts."""
    state_file = Path(__file__).with_name("runtime") / "agent-state.json"
    replay = ReplayWriter(Path(__file__).with_name("runtime") / "replay.jsonl")
    runtime = AgentRuntime(memory_store=MemoryStore(state_file))
    with ArenaHeroClient(api_key=api_key) as game:
        for turn in game.turns():
            context = DecisionContext.from_turn(turn)
            result: DecisionResult = runtime.decide(turn)
            accepted = turn.submit()
            if accepted.accepted:
                runtime.commit(result)
                replay.append(context, result, accepted)
            print(summary_line(context, result, accepted), flush=True)


def _api_key_from_environment() -> str | None:
    """Read the key without logging it, preferring the process environment."""
    if value := os.environ.get("ARENA_HERO_API_KEY"):
        return value
    env_file = Path(__file__).with_name(".env")
    if not env_file.is_file():
        return None
    for line in env_file.read_text(encoding="utf-8").splitlines():
        name, separator, value = line.partition("=")
        if separator and name.strip() == "ARENA_HERO_API_KEY":
            return value.strip().strip("\"'") or None
    return None


if __name__ == "__main__":
    try:
        play(_api_key_from_environment() or getpass("Arena Hero API key: "))
    except KeyboardInterrupt:
        print("Arena Hero tactic stopped by user.")
