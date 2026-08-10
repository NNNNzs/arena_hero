from arena_tactic.objectives.beacon import BeaconCampaign, BeaconInput, BeaconStage


def facts(**changes):
    baseline = dict(tick=1, beacon_cell=(3, 3), ground_visible=True, own_carrier_alias=None,
                    carrier_alive=True, escort_ready=0, escort_quorum=2)
    baseline.update(changes)
    return BeaconInput(**baseline)


def test_beacon_campaign_waits_for_escort_quorum():
    state, _, tasks, candidates = BeaconCampaign().evaluate(facts(escort_ready=1))
    assert state.stage is BeaconStage.ASSEMBLE
    assert tasks[0].kind == "ESCORT_CARRIER" and candidates == ("ASSEMBLE_ESCORT",)


def test_pickup_requires_current_ground_visibility():
    state, _, _, candidates = BeaconCampaign().evaluate(facts(escort_ready=2, ground_visible=False))
    assert state.stage is BeaconStage.ASSEMBLE and "PICKUP_BEACON" not in candidates


def test_carrier_death_rebuilds_recovery_tasks():
    state, _, tasks, candidates = BeaconCampaign(BeaconStage.HOLD, "entity_carrier").evaluate(facts(carrier_alive=False))
    assert state.stage is BeaconStage.RECOVER
    assert state.recovery_cell == (3, 3)
    assert tasks[0].kind == "RECOVER_BEACON" and candidates == ("REACQUIRE_BEACON",)


def test_holding_beacon_raises_repair_policy():
    state, _, tasks, candidates = BeaconCampaign().evaluate(facts(own_carrier_alias="entity_carrier"))
    assert state.stage is BeaconStage.HOLD
    assert {task.kind for task in tasks} == {"HOLD_BEACON", "REPAIR_SHIELD"}
    assert candidates == ("REPAIR_SHIELD",)
