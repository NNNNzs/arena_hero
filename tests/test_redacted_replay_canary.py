import json
from pathlib import Path

import arena_tactic.canary as canary_module
from arena_tactic.canary import _latest_longest_contiguous_run, verify_redacted_replay, run_shadow_canary
from arena_tactic.models import AgentConfig
from arena_tactic.replay_loader import (
    freeze_redacted_replay, load_frozen_redacted_replay, load_redacted_replay,
    load_redacted_replay_history,
)
from arena_tactic.runtime import AgentRuntime


def test_project_redacted_replay_runs_as_a_deterministic_offline_canary():
    path = Path(__file__).parents[1] / "runtime" / "replay.jsonl"
    snapshot = freeze_redacted_replay(path)
    turns = load_frozen_redacted_replay(snapshot)

    assert len(turns) >= 100
    config = AgentConfig(scheduler_shadow=True, worker_bt_canary=True, vanguard_bt_canary=True,
                         ranger_bt_canary=True, core_bt_canary=True, beacon_campaign_v1=True,
                         core_migration_v1=True, core_attack_campaign_v1=True, planner_canary=True)
    first = run_shadow_canary(turns, config=config)
    second_turns = load_frozen_redacted_replay(snapshot)
    second = run_shadow_canary(second_turns, config=config)

    assert first.ticks == len(turns)
    assert len(second_turns) == len(turns)
    assert all(first_turn is not second_turn for first_turn, second_turn in zip(turns, second_turns))
    assert all(first_turn.state is not second_turn.state for first_turn, second_turn in zip(turns, second_turns))
    assert first.timed_out_ticks == 0
    assert first.rejected_intents == 0
    assert first.p95_decision_ms < 500
    assert first.action_signatures == second.action_signatures


def test_recent_replay_canary_explains_every_current_entity_without_legacy_actions():
    path = Path(__file__).parents[1] / "runtime" / "replay.jsonl"
    config = AgentConfig(scheduler_shadow=True, worker_bt_canary=True, vanguard_bt_canary=True,
                         ranger_bt_canary=True, core_bt_canary=True, beacon_campaign_v1=True,
                         core_migration_v1=True, core_attack_campaign_v1=True, planner_canary=True)
    runtime = AgentRuntime(config=config)

    for turn in load_redacted_replay(path, limit=100):
        result = runtime.decide(turn)

        assert result.trace is not None
        assert result.trace.planner_version == "bt-planner-canary-v1"
        for entity in result.trace.entity_traces:
            assert entity.current_task
            assert entity.action
            assert entity.reason_codes
        runtime.commit(result)


def test_replay_gate_keeps_its_threshold_and_accepts_only_enough_representative_ticks():
    path = Path(__file__).parents[1] / "runtime" / "replay.jsonl"

    gate = verify_redacted_replay(path, required_ticks=500)

    assert gate.available_ticks >= 100
    assert gate.required_ticks == 500
    assert gate.timed_out_ticks == 0
    assert gate.rejected_intents == 0
    assert gate.deterministic
    assert gate.p95_decision_ms < 500
    assert gate.max_decision_ms < 900
    assert gate.accepted is (gate.longest_contiguous_ticks >= gate.required_ticks)


def test_replay_gate_uses_the_latest_longest_contiguous_tick_run():
    turns = load_redacted_replay(Path(__file__).parents[1] / "runtime" / "replay.jsonl")
    selected = _latest_longest_contiguous_run(turns)

    assert selected
    assert all(current.tick == prior.tick + 1 for prior, current in zip(selected, selected[1:]))
    assert selected[-1].tick >= selected[0].tick


def test_replay_history_loader_reads_rotations_oldest_to_newest(tmp_path: Path):
    source = Path(__file__).parents[1] / "runtime" / "replay.jsonl"
    template = json.loads(next(line for line in source.read_text(encoding="utf-8").splitlines() if line.strip()))
    path = tmp_path / "replay.jsonl"
    for destination, ticks in ((path.with_name("replay.jsonl.2"), (1, 2)),
                               (path.with_name("replay.jsonl.1"), (3, 4)),
                               (path, (5, 6))):
        records = []
        for tick in ticks:
            record = {**template, "tick": tick}
            records.append(json.dumps(record))
        destination.write_text("\n".join(records) + "\n", encoding="utf-8")

    turns = load_redacted_replay_history(path)

    assert [turn.tick for turn in turns] == [1, 2, 3, 4, 5, 6]


def test_replay_gate_uses_one_frozen_snapshot_when_the_live_file_grows(tmp_path: Path, monkeypatch):
    source = Path(__file__).parents[1] / "runtime" / "replay.jsonl"
    records = [line for line in source.read_text(encoding="utf-8").splitlines() if line.strip()][:3]
    path = tmp_path / "replay.jsonl"
    path.write_text("\n".join(records) + "\n", encoding="utf-8")
    appended = json.loads(records[-1])
    appended["tick"] += 1
    original_run = canary_module.run_shadow_canary
    runs = []

    def append_after_first_run(turns, *, config):
        report = original_run(turns, config=config)
        runs.append(report)
        if len(runs) == 1:
            with path.open("a", encoding="utf-8") as replay:
                replay.write(json.dumps(appended) + "\n")
        return report

    monkeypatch.setattr(canary_module, "run_shadow_canary", append_after_first_run)

    gate = verify_redacted_replay(path, required_ticks=1)

    assert len(runs) == 2
    assert runs[0].ticks == runs[1].ticks == 3
    assert gate.available_ticks == 3
    assert gate.longest_contiguous_ticks == 3
    assert gate.deterministic
