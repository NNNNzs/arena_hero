from arena_hero import UnitType

from arena_tactic import AgentRuntime
from arena_tactic.context import DecisionContext
from arena_tactic.identity import entity_alias
from arena_tactic.models import AgentConfig

from .factories import core, event, turn, unit


def test_worker_canary_replaces_only_worker_legacy_proposal():
    worker = unit(1, UnitType.WORKER, (0, 0), cargo=1)
    vanguard = unit(2, UnitType.VANGUARD, (2, 0))
    enemy = unit(3, UnitType.WORKER, (3, 0), controlled=False)
    game_turn = turn(owned_core=core(), units=(worker, vanguard), enemies=(enemy,))

    result = AgentRuntime(config=AgentConfig(worker_bt_canary=True)).decide(game_turn)

    actions = game_turn.plan.model_dump(mode="json")["unit_actions"]
    assert actions[str(worker.id)]["type"] == "DEPOSIT"
    assert actions[str(vanguard.id)]["type"] == "SWEEP"
    assert next(intent for intent in result.intents if intent.actor_id == worker.id).reason == "bt_worker_deposit_cargo"
    worker_trace = next(item for item in result.trace.entity_traces if item.actor_alias == entity_alias(worker.id))
    assert worker_trace.current_task == "BT_WORKER_CANARY"
    assert any(node["node_id"] == "worker.cargo.deposit_action" for node in worker_trace.node_path)


def test_worker_canary_keeps_a_route_cursor_across_authoritative_ticks():
    runtime = AgentRuntime(config=AgentConfig(worker_bt_canary=True))
    first = turn(tick=1, owned_core=core(), units=(unit(1, UnitType.WORKER, (0, 0)),), resource_cells=((2, 0),))
    second = turn(tick=2, owned_core=core(), units=(unit(1, UnitType.WORKER, (2, 0)),), resource_cells=((2, 0),))

    runtime.decide(first)
    result = runtime.decide(second)

    assert first.plan.model_dump(mode="json")["unit_actions"][str(unit(1, UnitType.WORKER, (0, 0)).id)]["type"] == "MOVE"
    assert next(intent for intent in result.intents if intent.actor_id == unit(1, UnitType.WORKER, (1, 0)).id).action.value == "HARVEST"
    board = runtime.worker_canary.boards[unit(1, UnitType.WORKER, (0, 0)).id]
    assert "worker.root" not in board.cursors


def test_worker_canary_times_out_a_stalled_resource_route_then_requests_replan():
    runtime = AgentRuntime(config=AgentConfig(worker_bt_canary=True))
    worker = unit(1, UnitType.WORKER, (0, 0))
    results = []
    for tick in range(1, 6):
        game_turn = turn(tick=tick, owned_core=core(), units=(worker,), resource_cells=((2, 0),))
        results.append(runtime.decide(game_turn))

    intent = next(item for item in results[-1].intents if item.actor_id == worker.id)
    assert intent.action.value == "WAIT"
    assert intent.reason == "bt_worker_replan_resource_route"
    trace = next(item for item in results[-1].trace.entity_traces if item.actor_alias == entity_alias(worker.id))
    assert any(node["reason"] == "TIMEOUT" for node in trace.node_path)


def test_worker_canary_fault_falls_back_to_the_already_built_legacy_plan(monkeypatch):
    runtime = AgentRuntime(config=AgentConfig(worker_bt_canary=True))
    monkeypatch.setattr(
        type(runtime.worker_canary),
        "propose",
        lambda *args: (_ for _ in ()).throw(RuntimeError("canary fault")),
    )
    worker = unit(1, UnitType.WORKER, (0, 0))
    game_turn = turn(owned_core=core(), units=(worker,), resource_cells=((1, 0),))

    result = runtime.decide(game_turn)

    assert game_turn.plan.model_dump(mode="json")["unit_actions"][str(worker.id)]["type"] == "MOVE"
    assert any(
        item["goal"] == "WORKER_BT_CANARY" and item["status"] == "FALLBACK_LEGACY"
        for item in result.trace.goal_summaries
    )


def test_worker_canary_persists_its_next_step_for_move_failure_recovery():
    runtime = AgentRuntime(config=AgentConfig(worker_bt_canary=True))
    worker = unit(1, UnitType.WORKER, (0, 0))
    result = runtime.decide(turn(tick=1, owned_core=core(), units=(worker,), resource_cells=((2, 0),)))

    task = result.next_memory.unit_tasks[str(worker.id)]
    assert task["kind"] == "resource"
    assert task["step"] == [1, 0]
    failed = turn(
        tick=2,
        owned_core=core(),
        units=(worker,),
        events=(event(11, "UNIT_MOVE_FAILED", reason_code="MOVE_BLOCKED_TERRAIN", actor_id=worker.id),),
    )
    recovered = result.next_memory.advance(DecisionContext.from_turn(failed), AgentConfig())
    assert (1, 0) in recovered.obstacles


def test_worker_canary_makes_a_route_blocked_frontier_explicit(monkeypatch):
    runtime = AgentRuntime(config=AgentConfig(worker_bt_canary=True))
    worker = unit(1, UnitType.WORKER, (0, 0))
    monkeypatch.setattr("arena_tactic.behaviors.worker.plan_step", lambda **_: None)

    result = runtime.decide(turn(owned_core=core(), units=(worker,)))

    intent = next(item for item in result.intents if item.actor_id == worker.id)
    assert intent.action.value == "WAIT"
    assert intent.reason == "bt_worker_frontier_route_blocked"
    trace = next(item for item in result.trace.entity_traces if item.actor_alias == entity_alias(worker.id))
    assert "bt_worker_frontier_route_blocked" in trace.reason_codes
    assert any(node["reason"] == "BT_WORKER_FRONTIER_ROUTE_BLOCKED" for node in trace.node_path)


def test_worker_canary_advances_when_a_persisted_frontier_target_is_reached():
    runtime = AgentRuntime(config=AgentConfig(worker_bt_canary=True))
    worker = unit(1, UnitType.WORKER, (0, 0))
    runtime.memory.unit_tasks[str(worker.id)] = {
        "kind": "explore",
        "sector": 0,
        "target": [0, 0],
    }

    result = runtime.decide(turn(owned_core=core(), units=(worker,)))

    intent = next(item for item in result.intents if item.actor_id == worker.id)
    assert intent.action.value == "MOVE"
    assert intent.reason == "bt_worker_advance_frontier"
    assert intent.target_cell != worker.position


def test_vanguard_canary_sweeps_a_current_adjacent_enemy_core():
    vanguard = unit(2, UnitType.VANGUARD, (1, 0))
    enemy_core = core(value=200, position=(2, 0), controlled=False)
    game_turn = turn(owned_core=core(), units=(vanguard,), enemies=(enemy_core,))

    result = AgentRuntime(config=AgentConfig(vanguard_bt_canary=True)).decide(game_turn)

    intent = next(item for item in result.intents if item.actor_id == vanguard.id)
    assert intent.action.value == "SWEEP"
    assert intent.reason == "bt_vanguard_adjacent_sweep"
    trace = next(item for item in result.trace.entity_traces if item.actor_alias == entity_alias(vanguard.id))
    assert trace.current_task == "BT_VANGUARD_CANARY"


def test_ranger_canary_shoots_only_a_current_legal_target():
    ranger = unit(3, UnitType.RANGER, (0, 0))
    enemy_core = core(value=200, position=(3, 0), controlled=False)
    game_turn = turn(owned_core=core(), units=(ranger,), enemies=(enemy_core,))

    result = AgentRuntime(config=AgentConfig(ranger_bt_canary=True)).decide(game_turn)

    intent = next(item for item in result.intents if item.actor_id == ranger.id)
    assert intent.action.value == "SHOOT"
    assert intent.target_id == enemy_core.id and intent.target_cell == enemy_core.position
    trace = next(item for item in result.trace.entity_traces if item.actor_alias == entity_alias(ranger.id))
    assert any(node["node_id"] == "ranger.shoot_action" for node in trace.node_path)


def test_core_canary_prioritizes_recovery_without_self_destruct():
    game_turn = turn(owned_core=core(hp=3), resources=2)

    result = AgentRuntime(config=AgentConfig(core_bt_canary=True)).decide(game_turn)

    core_intent = next(item for item in result.intents if item.is_core)
    assert core_intent.action.value == "HEAL"
    assert core_intent.reason == "bt_core_heal"


def test_core_canary_spawns_a_worker_only_with_reserve_after_current_cost_preview():
    game_turn = turn(owned_core=core(), resources=10)

    result = AgentRuntime(config=AgentConfig(core_bt_canary=True)).decide(game_turn)

    core_intent = next(item for item in result.intents if item.is_core)
    assert core_intent.action.value == "SPAWN"
    assert core_intent.unit_type is UnitType.WORKER


def test_planner_canary_generates_a_complete_plan_without_calling_legacy_strategy(monkeypatch):
    worker = unit(1, UnitType.WORKER, (0, 0))
    vanguard = unit(2, UnitType.VANGUARD, (1, 0))
    ranger = unit(3, UnitType.RANGER, (0, 1))
    runtime = AgentRuntime(config=AgentConfig(planner_canary=True))
    monkeypatch.setattr("arena_tactic.runtime.propose_intents", lambda *_: (_ for _ in ()).throw(AssertionError("legacy strategy called")))

    result = runtime.decide(turn(owned_core=core(), units=(worker, vanguard, ranger), resource_cells=((2, 0),)))

    assert len(result.intents) == 4
    assert result.trace.planner_version.startswith("bt-planner-canary")
