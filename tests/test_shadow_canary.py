from arena_hero import UnitType

from arena_tactic.canary import run_shadow_canary
from arena_tactic.models import AgentConfig
from .factories import core, turn, unit


def _five_hundred_turns():
    return tuple(
        turn(tick=tick, owned_core=core(), units=(
            unit(1, UnitType.WORKER, (0, 0)), unit(2, UnitType.VANGUARD, (1, 0)), unit(3, UnitType.RANGER, (0, 1)),
        ), resource_cells=((2, 0),), beacon_position=(20, 20))
        for tick in range(1, 501)
    )


def test_500_tick_offline_shadow_canary_is_deterministic_and_bounded():
    turns = _five_hundred_turns()
    config = AgentConfig(scheduler_shadow=True, worker_bt_canary=True, vanguard_bt_canary=True,
                         ranger_bt_canary=True, core_bt_canary=True, planner_canary=True)
    first = run_shadow_canary(turns, config=config)
    second = run_shadow_canary(_five_hundred_turns(), config=config)

    assert first.ticks == 500
    assert first.timed_out_ticks == 0
    assert first.rejected_intents == 0
    assert first.p95_decision_ms < 500
    assert first.max_decision_ms < 500
    assert first.action_signatures == second.action_signatures
