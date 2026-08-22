from arena_hero import UnitType

from arena_tactic import AgentConfig, AgentMemory, choose_actions
from arena_tactic.models import ActionKind

from .factories import core, turn, unit


def _core_intent(result):
    return next(intent for intent in result.intents if intent.is_core)


def test_nearby_visible_mine_passes_spawn_evaluation_and_harvests():
    workers = (
        unit(1, UnitType.WORKER, (12, 0)),
        unit(2, UnitType.WORKER, (0, 1)),
    )
    result = choose_actions(
        turn(
            tick=5,
            owned_core=core(position=(0, 0)),
            units=workers,
            resource_cells=((12, 0),),
        )
    )

    assert result.next_memory.spawn_eval_status == "PASSED"
    assert _core_intent(result).action is not ActionKind.SELF_DESTRUCT
    assert next(intent for intent in result.intents if intent.actor_id == workers[0].id).action is ActionKind.HARVEST


def test_barren_spawn_self_destructs_on_final_evaluation_tick():
    workers = (
        unit(1, UnitType.WORKER, (1, 0)),
        unit(2, UnitType.WORKER, (0, 1)),
    )
    memory = AgentMemory()
    for tick in range(1, 21):
        game_turn = turn(tick=tick, owned_core=core(value=100), units=workers)
        result = choose_actions(
            game_turn,
            memory=memory,
        )
        memory = result.next_memory

    core_intent = _core_intent(result)
    assert core_intent.action is ActionKind.SELF_DESTRUCT
    assert core_intent.reason == "barren_spawn_fast_reroll"
    assert result.action_counts["SELF_DESTRUCT"] == 1
    assert game_turn.plan.core_action.type == "SELF_DESTRUCT"


def test_spawn_evaluation_resets_for_a_new_core_generation():
    workers = (
        unit(1, UnitType.WORKER, (1, 0)),
        unit(2, UnitType.WORKER, (0, 1)),
    )
    stale_memory = AgentMemory(
        last_core_id=str(core(value=100).id),
        spawn_eval_core_id=str(core(value=100).id),
        spawn_eval_started_tick=1,
        spawn_eval_status="PENDING",
    )

    missing = choose_actions(turn(tick=19, owned_core=None), memory=stale_memory)
    assert missing.next_memory.spawn_eval_core_id is None

    game_turn = turn(tick=20, owned_core=core(value=101), units=workers)
    result = choose_actions(
        game_turn,
        memory=missing.next_memory,
    )

    assert result.next_memory.spawn_eval_core_id == str(core(value=101).id)
    assert result.next_memory.spawn_eval_started_tick == 20
    assert result.next_memory.spawn_eval_status == "PENDING"
    assert _core_intent(result).action is not ActionKind.SELF_DESTRUCT
    assert game_turn.plan.core_action.type != "SELF_DESTRUCT"


def test_established_or_combat_rosters_never_trigger_spawn_reroll():
    pending = AgentMemory(
        spawn_eval_core_id=str(core().id),
        spawn_eval_started_tick=1,
        spawn_eval_status="PENDING",
    )
    established = choose_actions(
        turn(
            tick=20,
            owned_core=core(),
            units=tuple(unit(index, UnitType.WORKER, (index, 0)) for index in range(1, 4)),
        ),
        memory=pending,
    )
    combat = choose_actions(
        turn(
            tick=20,
            owned_core=core(),
            units=(
                unit(1, UnitType.WORKER, (1, 0)),
                unit(2, UnitType.VANGUARD, (0, 1)),
            ),
        ),
        memory=pending,
    )

    assert _core_intent(established).action is not ActionKind.SELF_DESTRUCT
    assert _core_intent(combat).action is not ActionKind.SELF_DESTRUCT


def test_spawn_reroll_can_be_disabled():
    memory = AgentMemory(
        spawn_eval_core_id=str(core().id),
        spawn_eval_started_tick=1,
        spawn_eval_status="PENDING",
    )
    result = choose_actions(
        turn(tick=20, owned_core=core(), units=(unit(1, UnitType.WORKER, (1, 0)),)),
        memory=memory,
        config=AgentConfig(enable_spawn_reroll=False),
    )

    assert _core_intent(result).action is not ActionKind.SELF_DESTRUCT
