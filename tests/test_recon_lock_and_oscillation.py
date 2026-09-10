"""Regression tests for recon target locking and multi-step anti-oscillation."""

from arena_hero import UnitType
from arena_tactic.context import DecisionContext
from arena_tactic.memory import AgentMemory, _safe_task
from arena_tactic.models import AgentConfig
from arena_tactic.strategy.workers import _locked_recon_targets
from tests.factories import unit, turn


def test_safe_task_preserves_recon_since():
    raw_task = {
        "kind": "recon",
        "target": [-930, 1481],
        "attempt_tick": 252342,
        "recon_since": 252300,
        "failures": 0,
        "recent_cells": [[-929, 1505], [-930, 1505]],
    }
    cleaned = _safe_task(raw_task)
    assert cleaned.get("recon_since") == 252300
    assert cleaned.get("kind") == "recon"
    assert cleaned.get("target") == [-930, 1481]


def test_locked_recon_targets_holds_and_expires():
    w = unit(1, UnitType.WORKER, (-929, 1505))
    worker_id = str(w.id)
    memory = AgentMemory()
    target = (-930, 1481)
    memory.resource_observations[target] = 252000
    memory.unit_tasks[worker_id] = {
        "kind": "recon",
        "target": list(target),
        "recon_since": 100,
    }
    config = AgentConfig(recon_target_grace_ticks=8)

    # Within grace ticks (100 + 5 <= 108)
    ctx_active = DecisionContext.from_turn(turn(tick=105, units=(w,)))
    locked = _locked_recon_targets((w,), memory, ctx_active, config)
    assert locked.get(worker_id) == target

    # Expired grace ticks (100 + 10 > 108)
    ctx_expired = DecisionContext.from_turn(turn(tick=110, units=(w,)))
    locked_expired = _locked_recon_targets((w,), memory, ctx_expired, config)
    assert worker_id not in locked_expired
