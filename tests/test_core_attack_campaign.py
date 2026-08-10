from arena_tactic.objectives.core_attack import CoreAttackCampaign, CoreAttackInput, CoreAttackStage


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
