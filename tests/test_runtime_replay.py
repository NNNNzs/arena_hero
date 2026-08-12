import json
from time import perf_counter
from types import SimpleNamespace

from arena_hero import BeaconStatus, UnitType

from arena_tactic import (
    AgentConfig,
    AgentMemory,
    AgentRuntime,
    MemoryStore,
    choose_actions,
)
from arena_tactic.context import DecisionContext
from arena_tactic.models import ActionKind
from arena_tactic.observability import ReplayWriter, replay_metrics, summary_line

from .factories import core, event, turn, unit, uuid


def test_event_processing_is_deduplicated_and_resource_depletion_invalidates_memory():
    depletion = event(
        500,
        "HARVEST_FAILED",
        reason_code="RESOURCE_DEPLETED",
        position=(5, 5),
    )
    memory = AgentMemory(resource_observations={(5, 5): 0})
    context = DecisionContext.from_turn(
        turn(owned_core=core(), events=(depletion, depletion))
    )
    next_memory = memory.advance(context, AgentConfig())
    assert next_memory.event_counts["HARVEST_FAILED"] == 1
    assert (5, 5) not in next_memory.resource_observations


def test_remembered_resource_is_cooled_after_two_consecutive_empty_rechecks():
    remembered = (2, 0)
    worker = unit(1, UnitType.WORKER, (0, 0))
    memory = AgentMemory(last_tick=1, resource_observations={remembered: 1})
    config = AgentConfig(
        resource_recheck_failure_threshold=2,
        resource_recheck_cooldown_ticks=8,
    )

    first = memory.advance(
        DecisionContext.from_turn(turn(tick=2, owned_core=core(position=(-5, 0)), units=(worker,))),
        config,
    )
    assert first.resource_observations == {remembered: 1}
    assert first.resource_recheck_failures == {remembered: 1}

    second = first.advance(
        DecisionContext.from_turn(turn(tick=3, owned_core=core(position=(-5, 0)), units=(worker,))),
        config,
    )
    assert remembered not in second.resource_observations
    assert second.resource_recheck_failures == {}
    assert second.resource_recheck_cooldowns[remembered] == 11


def test_rejected_tick_does_not_persist_resource_recheck_failure():
    remembered = (2, 0)
    runtime = AgentRuntime(
        memory=AgentMemory(last_tick=1, resource_observations={remembered: 1}),
        config=AgentConfig(resource_recheck_failure_threshold=2),
    )
    worker = unit(1, UnitType.WORKER, (0, 0))

    result = runtime.decide(
        turn(tick=2, owned_core=core(position=(-5, 0)), units=(worker,))
    )

    assert result.next_memory.resource_recheck_failures == {remembered: 1}
    assert runtime.memory.resource_recheck_failures == {}


def test_memory_store_round_trip_is_versioned_and_controller_free(tmp_path):
    store = MemoryStore(tmp_path / "runtime" / "agent-state.json")
    memory = AgentMemory(
        last_tick=4,
        obstacles={(1, 2)},
        explored={(0, 0)},
        resource_recheck_failures={(3, 4): 1},
        resource_recheck_cooldowns={(5, 6): 12},
        unit_tasks={str(uuid(1)): {"kind": "explore", "target": [3, 4]}},
    )
    store.save(memory)
    loaded = store.load()
    assert loaded.last_tick == 4
    assert loaded.obstacles == {(1, 2)}
    assert loaded.resource_recheck_failures == {(3, 4): 1}
    assert loaded.resource_recheck_cooldowns == {(5, 6): 12}
    payload = json.loads(store.path.read_text(encoding="utf-8"))
    assert payload["version"] == 3
    assert "controller" not in store.path.read_text(encoding="utf-8").lower()
    assert str(uuid(1)) not in store.path.read_text(encoding="utf-8")
    assert not store.path.with_suffix(".json.tmp").exists()


def test_memory_store_migrates_version_one_without_losing_exploration(tmp_path):
    store = MemoryStore(tmp_path / "agent-state.json")
    store.path.write_text(
        json.dumps(
            {
                "version": 1,
                "last_tick": 7,
                "last_mode": "EXPLORE",
                "obstacles": [[1, 0]],
                "explored": [[0, 0], [0, 1]],
                "resource_observations": {},
                "unit_tasks": {},
            }
        ),
        encoding="utf-8",
    )
    loaded = store.load()
    assert loaded.version == 3
    assert loaded.last_tick == 7
    assert loaded.obstacles == {(1, 0)}
    assert loaded.explored == {(0, 0), (0, 1)}


def test_memory_store_loads_v2_and_preserves_operational_state(tmp_path):
    store = MemoryStore(tmp_path / "agent-state.json")
    store.path.write_text(
        json.dumps(
            {
                "version": 2,
                "last_tick": 9,
                "obstacles": [[4, 5]],
                "explored": [[1, 1]],
                "resource_observations": {"3,2": 8},
                "temporary_blocks": {"2,2": 12},
                "processed_event_ids": ["legacy-event"],
                "event_counts": {"UNIT_MOVE_FAILED": 2},
                "unit_tasks": {str(uuid(1)): {"kind": "explore", "target": [9, 9]}},
            }
        ),
        encoding="utf-8",
    )

    loaded = store.load()

    assert loaded.version == 3
    assert loaded.obstacles == {(4, 5)}
    assert loaded.explored == {(1, 1)}
    assert loaded.resource_observations == {(3, 2): 8}
    assert loaded.temporary_blocks == {(2, 2): 12}
    assert loaded.processed_event_ids == ["legacy-event"]
    assert loaded.event_counts == {"UNIT_MOVE_FAILED": 2}
    assert loaded.unit_tasks


def test_memory_unknown_schema_falls_back_safely():
    loaded = AgentMemory.from_dict(
        {
            "version": 999,
            "last_tick": 999,
            "obstacles": [["bad", "cell"]],
            "unit_tasks": {"unsafe": {"controller": "not allowed"}},
        }
    )

    assert loaded.version == 3
    assert loaded.last_tick == 0
    assert loaded.obstacles == set()
    assert loaded.unit_tasks == {}


def test_memory_partial_corruption_preserves_parseable_permanent_obstacles():
    loaded = AgentMemory.from_dict({"version": 3, "obstacles": [[1, 2], ["bad"], [3, 4]], "explored": "corrupt", "event_counts": []})
    assert loaded.obstacles == {(1, 2), (3, 4)}


def test_safe_task_rejects_nested_secrets_and_complete_uuids():
    malicious = {"kind": "explore", "target": [1, 2], "step": {"authorization": "Bearer bad"}, "sector": {"nested": str(uuid(77))}, "cookie": "bad", "token": "bad"}
    memory = AgentMemory(unit_tasks={str(uuid(1)): malicious})
    raw = json.dumps(memory.to_dict())
    assert "explore" in raw and "[1, 2]" in raw
    for forbidden in ("authorization", "cookie", "token", "secret", str(uuid(77))):
        assert forbidden not in raw.lower()


def test_v2_alias_rebind_preserves_task_and_processed_event_dedupes_next_turn():
    event_id = uuid(88)
    loaded = AgentMemory.from_dict({"version": 2, "unit_tasks": {str(uuid(1)): {"kind": "explore", "target": [9, 9]}}, "processed_event_ids": [str(event_id)], "event_counts": {"UNIT_MOVE_FAILED": 1}})
    context = DecisionContext.from_turn(turn(tick=10, owned_core=core(), units=(unit(1, UnitType.WORKER, (0, 0)),), events=(event(88, "UNIT_MOVE_FAILED", actor_id=uuid(1)),)))
    advanced = loaded.advance(context, AgentConfig())
    assert advanced.unit_tasks[str(uuid(1))]["kind"] == "explore"
    assert advanced.event_counts["UNIT_MOVE_FAILED"] == 1


def test_memory_is_only_committed_after_explicit_success(tmp_path):
    store = MemoryStore(tmp_path / "agent-state.json")
    runtime = AgentRuntime(memory_store=store)
    result = runtime.decide(turn(owned_core=core(), resources=10))
    assert not store.path.exists()
    runtime.commit(result)
    assert store.path.exists()
    assert store.load().accepted_ticks == 1


def test_redacted_replay_and_offline_metrics_exclude_credentials(tmp_path):
    game_turn = turn(owned_core=core(), resources=10)
    context = DecisionContext.from_turn(game_turn)
    result = choose_actions(game_turn)
    writer = ReplayWriter(tmp_path / "replay.jsonl")
    writer.append(context, result, SimpleNamespace(accepted=True))
    raw = writer.path.read_text(encoding="utf-8")
    lowered = raw.lower()
    assert "api_key" not in lowered
    assert "authorization" not in lowered
    assert "bot_owner" not in raw
    metrics = replay_metrics(writer.path)
    assert metrics["ticks"] == 1
    assert metrics["accepted_ticks"] == 1
    assert metrics["action_counts"]["SPAWN"] == 1


def test_replay_v1_rotates_by_size_without_changing_record_schema(tmp_path):
    writer = ReplayWriter(tmp_path / "replay.jsonl", max_file_bytes=900, history_files=3)
    for tick in range(1, 20):
        game_turn = turn(tick=tick, owned_core=core(), resources=10)
        writer.append(DecisionContext.from_turn(game_turn), choose_actions(game_turn), SimpleNamespace(accepted=True))

    files = sorted(tmp_path.glob("replay.jsonl*"))
    assert files == [
        writer.path,
        writer.path.with_name("replay.jsonl.1"),
        writer.path.with_name("replay.jsonl.2"),
        writer.path.with_name("replay.jsonl.3"),
    ]
    records = [json.loads(line) for file in files for line in file.read_text().splitlines()]
    assert records and all(record["schema_version"] == 1 for record in records)
    assert all(file.stat().st_size <= 900 for file in files)


def test_terminal_summary_includes_redacted_per_actor_intents_and_events():
    worker = unit(1, UnitType.WORKER, (0, 0))
    resolved_event = event(
        101,
        "HARVEST_FAILED",
        reason_code="RESOURCE_DEPLETED",
        position=(0, 0),
    )
    game_turn = turn(
        owned_core=core(position=(-2, 0)),
        units=(worker,),
        resource_cells=((2, 0),),
        events=(resolved_event,),
    )
    context = DecisionContext.from_turn(game_turn)
    result = choose_actions(game_turn)

    summary = summary_line(context, result, SimpleNamespace(accepted=True))

    assert "[第 1 回合] 提交成功｜策略：发展经济" in summary
    assert "工人 #" in summary
    assert "：移动，向右；目标坐标 (2, 0)（前往可见资源）" in summary
    assert str(worker.id) not in summary
    assert "上回合结算：" in summary
    assert "HARVEST_FAILED，原因：RESOURCE_DEPLETED，坐标 (0, 0)" in summary


def test_same_input_and_memory_produces_same_plan():
    units = (
        unit(1, UnitType.WORKER, (0, 0)),
        unit(2, UnitType.VANGUARD, (0, 1)),
        unit(3, UnitType.RANGER, (0, 2)),
    )
    first = choose_actions(
        turn(owned_core=core(position=(-2, 0)), units=units, resource_cells=((4, 0),))
    )
    second = choose_actions(
        turn(owned_core=core(position=(-2, 0)), units=units, resource_cells=((4, 0),))
    )
    comparable = lambda result: [
        (
            intent.actor_id,
            intent.action,
            intent.target_id,
            intent.target_cell,
            intent.direction,
            intent.reason,
        )
        for intent in result.intents
    ]
    assert comparable(first) == comparable(second)


def test_generated_plans_obey_current_object_and_visible_target_properties():
    for seed in range(1, 25):
        units = tuple(
            unit(
                seed * 100 + index,
                (UnitType.WORKER, UnitType.VANGUARD, UnitType.RANGER)[index % 3],
                (index - 2, seed % 3),
            )
            for index in range(1, 7)
        )
        enemy = unit(seed * 1000, UnitType.WORKER, (3, seed % 3), controlled=False)
        game_turn = turn(owned_core=core(), units=units, enemies=(enemy,), resources=20)
        result = choose_actions(game_turn)
        current_ids = {game_turn.core.id, *(unit.id for unit in game_turn.units)}
        assert {intent.actor_id for intent in result.intents} == current_ids
        assert len(result.intents) == len(current_ids)
        assert all(
            intent.target_id in {visible.id for visible in game_turn.visible_enemies}
            for intent in result.intents
            if intent.action is ActionKind.SHOOT
        )
        assert "api_key" not in repr(result).lower()


def test_twenty_unit_decision_finishes_within_budget():
    units = tuple(
        unit(
            index + 1,
            (UnitType.WORKER, UnitType.VANGUARD, UnitType.RANGER)[index % 3],
            (index % 5, index // 5 + 1),
        )
        for index in range(20)
    )
    game_turn = turn(
        owned_core=core(),
        units=units,
        resources=100,
        resource_cells=((8, 0), (8, 2), (8, 4), (10, 1)),
        obstacle_cells=((3, 0), (3, 1), (3, 2)),
        beacon_status=BeaconStatus.CARRIED,
        beacon_carrier_id=uuid(999),
    )
    started = perf_counter()
    result = choose_actions(game_turn)
    elapsed_ms = (perf_counter() - started) * 1_000
    assert result.decision_ms < 500
    assert elapsed_ms < 500
    assert len(result.intents) == 21


def test_useful_opportunity_prevents_all_wait():
    worker = unit(1, UnitType.WORKER, (0, 0))
    result = choose_actions(
        turn(owned_core=core(position=(-2, 0)), units=(worker,), resource_cells=((2, 0),))
    )
    assert any(intent.action is not ActionKind.WAIT for intent in result.intents)


def test_memory_rejects_untrusted_event_identifiers_and_counts():
    loaded = AgentMemory.from_dict({
        "version": 3,
        "processed_event_ids": ["entity_ab12", "event_ABC-123", "raw secret token", "../../cookie"],
        "event_counts": {"UNIT_MOVE_FAILED": 2, "authorization": 9, "bad-value": 1},
    })
    assert loaded.processed_event_ids == ["entity_ab12", "event_ABC-123"]
    assert loaded.event_counts == {"UNIT_MOVE_FAILED": 2}
    encoded = json.dumps(loaded.to_dict()).lower()
    assert "secret" not in encoded and "cookie" not in encoded and "authorization" not in encoded


def test_corrupt_memory_is_quarantined_and_obstacles_salvaged(tmp_path):
    path = tmp_path / "memory.json"
    path.write_text('{"version":3,"obstacles":[[1,2],[-3,4]],"broken":', encoding="utf-8")
    loaded = MemoryStore(path).load()
    assert loaded.obstacles == {(1, 2), (-3, 4)}
    assert not path.exists()
    assert len(list(tmp_path.glob("memory.json.corrupt-*"))) == 1


def test_corrupt_memory_without_salvage_returns_empty_and_is_quarantined(tmp_path):
    path = tmp_path / "memory.json"
    path.write_text("not-json", encoding="utf-8")
    assert MemoryStore(path).load().to_dict() == AgentMemory().to_dict()
    assert len(list(tmp_path.glob("memory.json.corrupt-*"))) == 1
