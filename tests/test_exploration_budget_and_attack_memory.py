"""Tests for exploration deadline fallback and enemy core visibility memory damping."""

from arena_hero import CoreState, UnitType
from arena_tactic.context import DecisionContext
from arena_tactic.memory import AgentMemory
from arena_tactic.models import AgentConfig, StrategicMode
from arena_tactic.strategy.mode import choose_mode
from arena_tactic.strategy.workers import _frontier_assignments

from .factories import core as make_core, turn as make_turn, unit as make_unit


def test_attack_mode_stays_during_enemy_core_memory_window():
    """ATTACK mode should not drop to BEACON when enemy core briefly flickers out of vision."""
    config = AgentConfig(attack_core_memory_ticks=20, attack_exit_grace_ticks=5)
    c = make_core(value=1, position=(0, 0), hp=5, shield=5, state=CoreState.NORMAL)
    v1 = make_unit(2, UnitType.VANGUARD, (1, 0))
    v2 = make_unit(3, UnitType.VANGUARD, (2, 0))
    r1 = make_unit(4, UnitType.RANGER, (3, 0))
    enemy_c = make_core(value=99, position=(10, 10), hp=5, shield=5, controlled=False)

    # Tick 100: enemy core visible, switches to ATTACK
    t1 = make_turn(tick=100, owned_core=c, units=[v1, v2, r1], enemies=[enemy_c])
    ctx1 = DecisionContext.from_turn(t1)
    mem = AgentMemory().advance(ctx1, config)
    mode1 = choose_mode(ctx1, mem, config)
    assert mode1 is StrategicMode.ATTACK
    assert mem.enemy_core_last_seen_tick == 100

    # Tick 108: 8 ticks later (exceeds attack_exit_grace_ticks=5), enemy core not visible,
    # but within attack_core_memory_ticks=20. Should remain ATTACK instead of oscillating to BEACON.
    t2 = make_turn(tick=108, owned_core=c, units=[v1, v2, r1], enemies=[])
    ctx2 = DecisionContext.from_turn(t2)
    mem = mem.advance(ctx2, config)
    mem.last_mode = StrategicMode.ATTACK
    mem.mode_since_tick = 100
    mode2 = choose_mode(ctx2, mem, config)
    assert mode2 is StrategicMode.ATTACK

    # Tick 130: 30 ticks later (> 20 memory window), should now safely transition out of ATTACK
    t3 = make_turn(tick=130, owned_core=c, units=[v1, v2, r1], enemies=[])
    ctx3 = DecisionContext.from_turn(t3)
    mem = mem.advance(ctx3, config)
    mem.mode_since_tick = 100
    mode3 = choose_mode(ctx3, mem, config)
    assert mode3 is not StrategicMode.ATTACK


def test_frontier_assignment_fallback_on_zero_deadline():
    """When the deadline has already expired, workers should fall back to geometric candidate."""
    config = AgentConfig()
    c = make_core(value=1, position=(0, 0), hp=5, shield=5)
    w = make_unit(2, UnitType.WORKER, (0, 0))
    t = make_turn(tick=1, owned_core=c, units=[w])
    ctx = DecisionContext.from_turn(t)
    mem = AgentMemory()
    mem.explored = {(0, 0)}

    # Pass an already-expired deadline (0.0)
    result = _frontier_assignments(
        units=[w],
        memory=mem,
        context=ctx,
        deadline=0.0,
        config=config,
        task_kind="explore",
    )
    assert str(w.id) in result
    assert result[str(w.id)] is not None
