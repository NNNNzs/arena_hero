from arena_tactic.objectives.core_attack import CoreAttackCampaign, CoreAttackInput, CoreAttackStage
from arena_hero import UnitType
from arena_tactic import AgentRuntime
from arena_tactic.models import AgentConfig
from .factories import core, event, turn, unit


def facts(**changes):
    baseline = dict(tick=1, target_cell=(3, 0), target_visible=True, rally_ready=3, rally_quorum=3,
                    ranger_has_legal_slot=False, vanguard_adjacent=False)
    baseline.update(changes)
    return CoreAttackInput(**baseline)


def test_attack_waits_for_rally_quorum():
    state, _, tasks, candidates = CoreAttackCampaign().evaluate(facts(rally_ready=2))
    assert state.stage is CoreAttackStage.RALLY
    assert tasks[0].kind == "RALLY_ATTACK" and candidates == ("RALLY",)


def test_ranger_uses_legal_firing_slot():
    _, _, tasks, candidates = CoreAttackCampaign().evaluate(facts(ranger_has_legal_slot=True))
    assert any(task.kind == "RANGER_FIRE" for task in tasks) and candidates == ("SHOOT_CELL",)


def test_vanguard_sweeps_enemy_core_cell():
    _, _, tasks, candidates = CoreAttackCampaign().evaluate(facts(vanguard_adjacent=True))
    assert tasks[0].kind == "VANGUARD_SWEEP" and candidates == ("SWEEP",)


def test_lost_visibility_creates_reacquire_not_stale_attack():
    state, _, tasks, candidates = CoreAttackCampaign().evaluate(facts(target_visible=False, target_cell=None))
    assert state.stage is CoreAttackStage.REACQUIRE
    assert tasks[0].kind == "REACQUIRE_TARGET" and candidates == ("REACQUIRE",)


def test_force_retreat_preempts_engagement():
    state, _, tasks, candidates = CoreAttackCampaign(CoreAttackStage.ENGAGE).evaluate(facts(force_retreat=True, ranger_has_legal_slot=True))
    assert state.stage is CoreAttackStage.RETREAT
    assert tasks[0].priority == 900 and candidates == ("FORCE_RETREAT",)


def test_kill_confirmed_only_from_authoritative_event():
    state, goal, _, _ = CoreAttackCampaign().evaluate(facts(core_destroyed_event=True))
    assert state.stage is CoreAttackStage.CONFIRMED and goal.status.value == "SATISFIED"
    state, goal, _, _ = CoreAttackCampaign().evaluate(facts(target_visible=False))
    assert state.stage is CoreAttackStage.REACQUIRE and goal.status.value == "ACTIVE"


def test_enabled_attack_uses_only_current_visible_enemy_core_after_quorum():
    target = core(value=200, position=(3, 0), controlled=False)
    ranger = unit(1, UnitType.RANGER, (0, 0))
    first = unit(2, UnitType.VANGUARD, (2, 0))
    second = unit(3, UnitType.VANGUARD, (3, 1))
    game_turn = turn(owned_core=core(), units=(ranger, first, second), enemies=(target,))

    result = AgentRuntime(config=AgentConfig(core_attack_campaign_v1=True)).decide(game_turn)

    intents = {item.actor_id: item for item in result.intents}
    assert intents[ranger.id].action.value == "SHOOT" and intents[ranger.id].target_id == target.id
    assert intents[first.id].action.value == "SWEEP" and intents[second.id].action.value == "SWEEP"


def test_runtime_confirms_attack_only_for_the_tracked_enemy_core_destroyed_event():
    target = core(value=200, position=(3, 0), controlled=False)
    units = (unit(1, UnitType.RANGER, (0, 0)), unit(2, UnitType.VANGUARD, (2, 0)), unit(3, UnitType.VANGUARD, (3, 1)))
    runtime = AgentRuntime(config=AgentConfig(core_attack_campaign_v1=True))
    runtime.commit(runtime.decide(turn(tick=1, owned_core=core(), units=units, enemies=(target,))))

    result = runtime.decide(turn(tick=2, owned_core=core(), units=units,
                                 events=(event(300, "CORE_DESTROYED", tick=2, reason_code="ATTACK", target_id=target.id),)))

    assert result.next_memory.objective_states["attack"]["stage"] == "CONFIRMED"


def test_attack_rally_and_retreat_move_only_current_combat_units():
    target = core(value=200, position=(8, 0), controlled=False)
    ranger = unit(1, UnitType.RANGER, (0, 0))
    runtime = AgentRuntime(config=AgentConfig(core_attack_campaign_v1=True))
    rally = runtime.decide(turn(tick=1, owned_core=core(), units=(ranger,), enemies=(target,)))
    assert next(item for item in rally.intents if item.actor_id == ranger.id).reason == "core_attack_rally"
    runtime.commit(rally)

    retreater = unit(1, UnitType.RANGER, (3, 0))
    retreat = runtime.decide(turn(tick=2, owned_core=core(hp=1), units=(retreater,), enemies=(target,)))
    assert next(item for item in retreat.intents if item.actor_id == retreater.id).reason == "core_attack_retreat"
