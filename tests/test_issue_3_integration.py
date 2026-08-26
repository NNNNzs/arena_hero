from __future__ import annotations

import json
from time import perf_counter

import pytest
from arena_hero import Direction, UnitType

from arena_tactic import AgentRuntime
from arena_tactic.command_center import CommandError, CommandQueue, _validate_body
from arena_tactic.context import DecisionContext
from arena_tactic.dashboard_integration import DashboardDataStore, dashboard_static_asset
from arena_tactic.identity import entity_alias
from arena_tactic.memory import AgentMemory
from arena_tactic.models import ActionIntent, ActionKind, AgentConfig, ReservationTable
from arena_tactic.squads import SquadType, build_squad_plan
from arena_tactic.strategy.workers import _plan_workers
from arena_tactic.validation import validate_intents

from .factories import core, turn, unit


def test_assign_squad_requires_canonical_alias_and_accepts_stable_squad_id():
    command_type, payload, _, _ = _validate_body(
        {
            "type": "ASSIGN_SQUAD",
            "payload": {
                "entity_alias": "entity_0123456789ab",
                "squad_id": "squad_scout_recon",
            },
        },
        current_tick=10,
    )
    assert command_type.value == "ASSIGN_SQUAD"
    assert payload == {
        "entity_alias": "entity_0123456789ab",
        "squad_id": "squad_scout_recon",
    }

    with pytest.raises(CommandError):
        _validate_body(
            {
                "type": "ASSIGN_SQUAD",
                "payload": {
                    "entity_alias": "0123456789ab",
                    "squad_id": "squad_scout_recon",
                },
            },
            current_tick=10,
        )


def test_manual_squad_membership_round_trips_through_memory():
    original = AgentMemory(
        manual_squad_assignments={
            "entity_0123456789ab": "squad_expedition_beacon"
        }
    )
    restored = AgentMemory.from_dict(original.to_dict())
    assert restored.manual_squad_assignments == original.manual_squad_assignments


def test_build_squad_plan_uses_dedicated_expedition_sizes():
    vanguards = tuple(unit(index, UnitType.VANGUARD, (index, 0)) for index in range(1, 5))
    rangers = tuple(unit(index + 10, UnitType.RANGER, (index, 1)) for index in range(1, 5))
    context = DecisionContext.from_turn(
        turn(owned_core=core(), units=(*vanguards, *rangers), beacon_position=(20, 0))
    )
    memory = AgentMemory(last_mode="BEACON")
    # record_mode normally supplies the enum before planning.
    from arena_tactic.models import StrategicMode
    memory.last_mode = StrategicMode.BEACON
    config = AgentConfig(
        core_guard_vanguards=1,
        core_guard_rangers=1,
        expedition_vanguards=1,
        expedition_rangers=2,
        mining_escort_vanguards=0,
        mining_escort_rangers=0,
        scout_vanguards=0,
        scout_rangers=0,
    )

    plan = build_squad_plan(context, memory, config)

    assert len(plan.ids_for(SquadType.EXPEDITION_BEACON, UnitType.VANGUARD)) == 1
    assert len(plan.ids_for(SquadType.EXPEDITION_BEACON, UnitType.RANGER)) == 2


def test_assign_squad_is_staged_as_persistent_membership_and_drives_behavior():
    vanguard = unit(1, UnitType.VANGUARD, (1, 0))
    game_turn = turn(
        tick=1,
        owned_core=core(position=(0, 0)),
        units=(vanguard,),
        beacon_position=(8, 0),
    )
    alias = entity_alias(vanguard.id)
    assert alias is not None
    queue = CommandQueue()
    queue.enqueue(
        {
            "type": "ASSIGN_SQUAD",
            "payload": {
                "entity_alias": alias,
                "squad_id": "squad_expedition_beacon",
            },
        },
        issuer="test",
        current_tick=0,
        idempotency_key="issue3-squad-assign",
        expected_version=0,
    )
    runtime = AgentRuntime(command_queue=queue)

    result = runtime.decide(game_turn)

    assert result.next_memory.manual_squad_assignments[alias] == "squad_expedition_beacon"
    intent = next(item for item in result.intents if item.actor_id == vanguard.id)
    assert intent.action is ActionKind.MOVE
    assert intent.target_cell == (8, 0)
    assert intent.reason == "expedition_vanguard_to_beacon"


def test_committed_policy_override_drives_live_guard_roster():
    vanguards = (
        unit(1, UnitType.VANGUARD, (5, 0)),
        unit(2, UnitType.VANGUARD, (6, 0)),
    )
    memory = AgentMemory(
        last_tick=1,
        policy_state={
            "version": 1,
            "posture": "BALANCED",
            "effective_tick": 1,
            "core_guard_vanguards": 2,
        },
    )
    runtime = AgentRuntime(
        memory=memory,
        config=AgentConfig(
            core_guard_vanguards=0,
            core_guard_rangers=0,
            scout_vanguards=2,
        ),
    )

    result = runtime.decide(turn(tick=2, owned_core=core(), units=vanguards))

    vanguard_intents = [item for item in result.intents if item.actor_id in {unit.id for unit in vanguards}]
    assert len(vanguard_intents) == 2
    assert all(item.reason in {"hold_core_defense_ring", "holding_defense_ring"} for item in vanguard_intents)
    assert runtime.config.core_guard_vanguards == 2


def test_map_memory_exposes_persisted_obstacles_and_versions_them(tmp_path):
    replay = tmp_path / "replay.jsonl"
    memory = tmp_path / "agent-state.json"
    memory.write_text(
        json.dumps({
            "version": 7,
            "obstacles": [[1, 2]],
            "explored": [[0, 0]],
            "mined_cells": [],
            "resource_observations": {},
        }),
        encoding="utf-8",
    )
    first = DashboardDataStore(replay, memory_path=memory).map_memory_payload()
    assert first["obstacles"] == [[1, 2]]

    memory.write_text(
        json.dumps({
            "version": 7,
            "obstacles": [[1, 2], [2, 2]],
            "explored": [[0, 0]],
            "mined_cells": [],
            "resource_observations": {},
        }),
        encoding="utf-8",
    )
    second = DashboardDataStore(replay, memory_path=memory).map_memory_payload()
    assert second["obstacles"] == [[1, 2], [2, 2]]
    assert second["version"] != first["version"]


def test_command_center_asset_uses_real_squad_command_and_config_fields():
    asset = dashboard_static_asset("/static/command-center.js")
    assert asset is not None
    text = asset[0].decode("utf-8")
    assert "type: 'ASSIGN_SQUAD'" in text
    assert "payload: { entity_alias: alias, squad_id: targetSquadId }" in text
    assert "expedition_vanguards: '远征编组·先锋编制'" in text
    assert "mining_escort_rangers: '矿区护航·游侠编制'" in text
    assert "resource_recheck_worker_limit: '矿区护航·工兵编制'" not in text


def test_final_capacity_allows_entering_a_cell_that_another_unit_leaves():
    leaving = unit(1, UnitType.WORKER, (0, 0))
    staying = unit(2, UnitType.WORKER, (0, 0))
    entering = unit(3, UnitType.WORKER, (1, 0))
    context = DecisionContext.from_turn(
        turn(owned_core=core(position=(10, 10)), units=(leaving, staying, entering))
    )
    proposals = (
        ActionIntent(
            leaving.id, False, ActionKind.MOVE, 10, "leave_full_cell",
            direction=Direction.UP, reserved_cell=(0, -1),
        ),
        ActionIntent(
            staying.id, False, ActionKind.WAIT, 20, "stay",
        ),
        ActionIntent(
            entering.id, False, ActionKind.MOVE, 100, "enter_after_departure",
            direction=Direction.LEFT, reserved_cell=(0, 0),
        ),
    )

    accepted, rejected = validate_intents(proposals, context, AgentConfig())

    assert not rejected
    assert next(item for item in accepted if item.actor_id == leaving.id).action is ActionKind.MOVE
    assert next(item for item in accepted if item.actor_id == entering.id).action is ActionKind.MOVE


def test_worker_prefers_safe_resource_under_same_threat_model_as_move():
    worker = unit(1, UnitType.WORKER, (0, 0))
    enemy_ranger = unit(50, UnitType.RANGER, (3, 0), controlled=False)
    context = DecisionContext.from_turn(
        turn(
            owned_core=core(position=(-5, 0)),
            units=(worker,),
            enemies=(enemy_ranger,),
            resource_cells=((2, 0), (0, 2)),
        )
    )
    memory = AgentMemory()
    reservations = ReservationTable(
        occupancy={cell: len(ids) for cell, ids in context.friendly_occupancy.items()}
    )

    intents = _plan_workers(
        context,
        memory,
        reservations,
        perf_counter() + 1,
        AgentConfig(),
        {},
    )
    intent = next(item for item in intents if item.actor_id == worker.id)

    assert intent.action is ActionKind.MOVE
    assert intent.target_cell == (0, 2)
