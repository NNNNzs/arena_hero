"""Offline shadow/canary acceptance harness; it never opens a live client."""

from __future__ import annotations

import argparse
from copy import deepcopy
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from arena_hero import Turn

from .models import AgentConfig
from .runtime import AgentRuntime


@dataclass(frozen=True, slots=True)
class ShadowCanaryReport:
    ticks: int
    max_decision_ms: float
    p95_decision_ms: float
    timed_out_ticks: int
    rejected_intents: int
    action_signatures: tuple[tuple[tuple[str, str], ...], ...]


@dataclass(frozen=True, slots=True)
class ReplayCanaryGate:
    """Evidence for the Phase 9 rollout gate, without exposing replay contents."""

    available_ticks: int
    longest_contiguous_ticks: int
    required_ticks: int
    p95_decision_ms: float
    max_decision_ms: float
    timed_out_ticks: int
    rejected_intents: int
    deterministic: bool

    @property
    def accepted(self) -> bool:
        return (
            self.longest_contiguous_ticks >= self.required_ticks
            and self.timed_out_ticks == 0
            and self.rejected_intents == 0
            and self.p95_decision_ms < 500
            and self.max_decision_ms < 900
            and self.deterministic
        )

    def record(self) -> dict[str, object]:
        return {
            "accepted": self.accepted,
            "available_ticks": self.available_ticks,
            "longest_contiguous_ticks": self.longest_contiguous_ticks,
            "required_ticks": self.required_ticks,
            "p95_decision_ms": round(self.p95_decision_ms, 3),
            "max_decision_ms": round(self.max_decision_ms, 3),
            "timed_out_ticks": self.timed_out_ticks,
            "rejected_intents": self.rejected_intents,
            "deterministic": self.deterministic,
        }


def _clone_replay_turn(turn: Turn) -> Turn:
    """Create an isolated offline Turn from one authoritative state snapshot."""
    return Turn(
        tick=turn.tick,
        state=deepcopy(turn.state),
        submitter=lambda _plan, _key: None,
    )


def run_shadow_canary(turns: Iterable[Turn], *, config: AgentConfig | None = None) -> ShadowCanaryReport:
    """Exercise enabled planners against supplied authoritative replay Turns.

    The harness intentionally does not call ``Turn.submit``.  Its commit is a
    local simulation used only to carry controller-free memory to the next
    supplied Turn, so it cannot be mistaken for live validation.
    """
    runtime = AgentRuntime(config=config or AgentConfig(
        scheduler_shadow=True, worker_bt_canary=True, vanguard_bt_canary=True,
        ranger_bt_canary=True, core_bt_canary=True,
        planner_canary=True,
    ))
    ticks = timed_out = rejected = 0
    maximum = 0.0
    durations: list[float] = []
    signatures: list[tuple[tuple[str, str], ...]] = []
    for turn in turns:
        # Controller calls mutate a Turn's in-memory plan.  Rehydrate the
        # controller facade around the same immutable authoritative state so a
        # caller can safely replay one frozen input snapshot more than once.
        replay_turn = _clone_replay_turn(turn)
        result = runtime.decide(replay_turn)
        ticks += 1
        timed_out += int(result.timed_out)
        rejected += len(result.rejected_intents)
        maximum = max(maximum, result.decision_ms)
        durations.append(result.decision_ms)
        signatures.append(tuple(sorted((str(intent.actor_id), intent.action.value) for intent in result.intents)))
        runtime.commit(result)
    p95_index = max(0, (len(durations) * 95 + 99) // 100 - 1)
    p95 = sorted(durations)[p95_index] if durations else 0.0
    return ShadowCanaryReport(ticks, maximum, p95, timed_out, rejected, tuple(signatures))


def verify_redacted_replay(path: Path, *, required_ticks: int = 500) -> ReplayCanaryGate:
    """Run the complete opt-in pipeline twice and return a redacted gate result."""
    if required_ticks <= 0:
        raise ValueError("required_ticks must be positive")
    from .replay_loader import freeze_redacted_replay_history, load_frozen_redacted_replay

    config = AgentConfig(
        scheduler_shadow=True, scheduler_canary=True, worker_bt_canary=True,
        vanguard_bt_canary=True, ranger_bt_canary=True, core_bt_canary=True,
        beacon_campaign_v1=True, core_migration_v1=True,
        core_attack_campaign_v1=True, planner_canary=True,
    )
    # Freeze JSON records before either run.  A live ReplayWriter may append
    # while this gate executes, but it cannot change either deterministic input.
    snapshot = freeze_redacted_replay_history(path)
    all_turns = load_frozen_redacted_replay(snapshot)
    turns = _latest_longest_contiguous_run(all_turns)
    second_turns = _latest_longest_contiguous_run(load_frozen_redacted_replay(snapshot))
    first = run_shadow_canary(turns, config=config)
    second = run_shadow_canary(second_turns, config=config)
    return ReplayCanaryGate(
        available_ticks=len(all_turns),
        longest_contiguous_ticks=first.ticks,
        required_ticks=required_ticks,
        p95_decision_ms=first.p95_decision_ms,
        max_decision_ms=first.max_decision_ms,
        timed_out_ticks=first.timed_out_ticks,
        rejected_intents=first.rejected_intents,
        deterministic=first.action_signatures == second.action_signatures,
    )


def _latest_longest_contiguous_run(turns: Iterable[Turn]) -> tuple[Turn, ...]:
    """Select the latest longest adjacent-Tick run; gaps and duplicate ticks reset it."""
    best: tuple[Turn, ...] = ()
    current: list[Turn] = []
    prior_tick: int | None = None
    for turn in turns:
        if prior_tick is None or turn.tick != prior_tick + 1:
            if len(current) >= len(best):
                best = tuple(current)
            current = [turn]
        else:
            current.append(turn)
        prior_tick = turn.tick
    if len(current) >= len(best):
        best = tuple(current)
    return best


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify a redacted Arena Hero replay against the full canary gate.")
    parser.add_argument("replay", type=Path, help="redacted replay-v1 JSONL path")
    parser.add_argument("--min-ticks", type=int, default=500, help="minimum representative replay ticks (default: 500)")
    args = parser.parse_args(argv)
    try:
        result = verify_redacted_replay(args.replay, required_ticks=args.min_ticks)
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps(result.record(), ensure_ascii=False, sort_keys=True))
    return 0 if result.accepted else 2


if __name__ == "__main__":
    raise SystemExit(main())
