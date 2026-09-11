"""Ranger firing-route sidestep: disengage defence regression tests.

Covers the fix for DEFENSE_DISENGAGED (防守单位脱离交战) where rangers at
a defence or intercept post would fall back to WAIT when their primary
staging cell was occupied / line-of-fire blocked, and then remain
stationary for multiple ticks instead of adjusting to an adjacent cell.
"""
from __future__ import annotations

from arena_hero import Direction, UnitType

from arena_tactic import AgentConfig, AgentMemory, choose_actions
from arena_tactic.models import ActionKind, Position, ReservationTable
from arena_tactic.navigation import shot_range
from arena_tactic.strategy.common import _firing_line_sidestep
from arena_tactic.context import DecisionContext

from .factories import core, turn, unit, uuid


# ---------------------------------------------------------------------------
# _firing_line_sidestep helper tests
# ---------------------------------------------------------------------------

class TestFiringLineSidestepHelper:
    """Direct unit tests for the _firing_line_sidestep helper."""

    def _make_context_and_memory(
        self,
        ranger_pos: Position,
        enemy_pos: Position,
        obstacles: set[Position] | None = None,
    ):
        """Build a minimal DecisionContext + AgentMemory for sidestep tests."""
        ranger = unit(1, UnitType.RANGER, ranger_pos)
        enemy = unit(100, UnitType.RANGER, enemy_pos, controlled=False)
        _core = core(position=(0, 0))
        obstacles = obstacles or set()
        obstacle_cells = tuple(obstacles)
        t = turn(
            owned_core=_core,
            units=(ranger,),
            enemies=(enemy,),
            obstacle_cells=obstacle_cells,
        )
        memory = AgentMemory()
        context = DecisionContext.from_turn(t)
        return ranger, enemy, context, memory

    def test_sidestep_prefers_cell_with_firing_line(self):
        """When the staging cell is reachable but blocked, sidestep should
        prefer an adjacent cell that has a clear shot to the enemy."""
        ranger_pos = (5, 0)
        enemy_pos = (8, 0)
        ranger, enemy, context, memory = self._make_context_and_memory(
            ranger_pos, enemy_pos,
        )
        reservations = ReservationTable(occupancy={})
        staging = (5, 3)  # staging cell to the south

        # (6,0) to (8,0): dx=2, dy=0 → range 2, clear → has shot.
        # Other cells either have no shot or are farther from staging.
        intent = _firing_line_sidestep(
            ranger, enemy, staging, context, memory, reservations,
            "test_sidestep",
        )
        assert intent is not None
        assert intent.action is ActionKind.MOVE
        assert intent.reason == "test_sidestep"
        # (6,0) has a firing line at range 2 → tier 0 (has shot + closer to staging)
        assert intent.reserved_cell == (6, 0)

    def test_sidestep_moves_toward_staging_when_no_shot_available(self):
        """When no adjacent cell has a firing line, sidestep should move
        closer to the staging cell."""
        ranger_pos = (5, 5)
        enemy_pos = (5, 50)
        ranger, enemy, context, memory = self._make_context_and_memory(
            ranger_pos, enemy_pos,
        )
        reservations = ReservationTable(occupancy={})
        staging = (5, 8)  # 3 cells south

        intent = _firing_line_sidestep(
            ranger, enemy, staging, context, memory, reservations,
            "test_no_shot",
        )
        assert intent is not None
        assert intent.action is ActionKind.MOVE
        # Should move toward staging: (5,6) is 1 step closer
        assert intent.reserved_cell == (5, 6)

    def test_sidestep_returns_none_when_all_blocked(self):
        """When every adjacent cell is blocked, return None."""
        ranger_pos = (5, 5)
        enemy_pos = (5, 50)
        all_adjacent = {(5, 4), (5, 6), (4, 5), (6, 5)}
        ranger, enemy, context, memory = self._make_context_and_memory(
            ranger_pos, enemy_pos, obstacles=all_adjacent,
        )
        reservations = ReservationTable(occupancy={})
        staging = (5, 8)

        intent = _firing_line_sidestep(
            ranger, enemy, staging, context, memory, reservations,
            "test_all_blocked",
        )
        assert intent is None

    def test_sidestep_anti_oscillation(self):
        """Prev-cell penalty should discourage bouncing back."""
        ranger_pos = (5, 5)
        enemy_pos = (5, 50)
        ranger, enemy, context, memory = self._make_context_and_memory(
            ranger_pos, enemy_pos,
        )
        # Record that ranger came from (5,4)
        memory.unit_tasks[str(ranger.id)] = {
            "kind": "firing_sidestep",
            "prev_cell": [5, 4],
            "target": [5, 50],
        }
        reservations = ReservationTable(occupancy={})
        staging = (5, 8)

        intent = _firing_line_sidestep(
            ranger, enemy, staging, context, memory, reservations,
            "test_anti_osc",
        )
        assert intent is not None
        # Should NOT go back to (5,4)
        assert intent.reserved_cell != (5, 4)

    def test_sidestep_with_clear_firing_line_at_range_2(self):
        """Verify a cell at range 2 with clear line-of-sight gets priority."""
        ranger_pos = (0, 0)
        enemy_pos = (3, 0)
        ranger, enemy, context, memory = self._make_context_and_memory(
            ranger_pos, enemy_pos,
        )
        reservations = ReservationTable(occupancy={})
        staging = (0, 0)  # same position, just need to sidestep

        intent = _firing_line_sidestep(
            ranger, enemy, staging, context, memory, reservations,
            "test_range2",
        )
        assert intent is not None
        # (1,0) has shot_range 2 to (3,0) — clear line, tier 0
        assert intent.reserved_cell == (1, 0)

    def test_sidestep_respects_friendly_occupancy_limit(self):
        """Should not sidestep into a cell already at 2/2 capacity."""
        ranger_pos = (5, 5)
        enemy_pos = (5, 50)
        ranger, enemy, context, memory = self._make_context_and_memory(
            ranger_pos, enemy_pos,
        )
        # (5,6) is full at 2/2 capacity
        reservations = ReservationTable(occupancy={(5, 6): 2})
        staging = (5, 8)

        intent = _firing_line_sidestep(
            ranger, enemy, staging, context, memory, reservations,
            "test_capacity",
        )
        assert intent is not None
        # Should not pick (5,6) because it's at capacity
        assert intent.reserved_cell != (5, 6)


# ---------------------------------------------------------------------------
# Integration: _move blocked → sidestep instead of WAIT
# ---------------------------------------------------------------------------

class TestRangerFiringSidestepIntegration:
    """Integration tests verifying the full strategy pipeline uses sidestep."""

    def test_ranger_sidesteps_when_staging_move_fails(self, monkeypatch):
        """When _move to staging returns None, ranger should sidestep rather
        than immediately WAIT with firing_route_blocked."""
        import arena_tactic.strategy.rangers as rangers_mod

        # Enemy at range 5 (not directly shootable, within engage distance)
        ranger = unit(11, UnitType.RANGER, (0, 1))
        enemy = unit(200, UnitType.RANGER, (0, 6), controlled=False)

        # Patch _move in rangers module to always return None
        monkeypatch.setattr(rangers_mod, "_move", lambda *a, **kw: None)

        result = choose_actions(
            turn(
                owned_core=core(),
                units=(ranger,),
                enemies=(enemy,),
            )
        )
        intent = next(item for item in result.intents if item.actor_id == ranger.id)
        # Should be a MOVE (sidestep), not WAIT
        assert intent.action is ActionKind.MOVE
        assert "sidestep" in intent.reason

    def test_intercept_ranger_sidesteps_when_staging_blocked(self, monkeypatch):
        """Intercept ranger with blocked staging should sidestep, not wait."""
        import arena_tactic.strategy.rangers as rangers_mod

        guard = unit(10, UnitType.VANGUARD, (0, 1))
        ranger = unit(11, UnitType.RANGER, (0, 2))
        # Enemy at range 6 — not shootable, but within intercept distance
        enemy = unit(200, UnitType.RANGER, (0, 8), controlled=False)

        # Patch _move to always return None
        monkeypatch.setattr(rangers_mod, "_move", lambda *a, **kw: None)

        result = choose_actions(
            turn(
                owned_core=core(),
                units=(guard, ranger),
                enemies=(enemy,),
            )
        )
        intent = next(item for item in result.intents if item.actor_id == ranger.id)
        # Should be a MOVE (sidestep), not WAIT
        assert intent.action is ActionKind.MOVE
        assert "sidestep" in intent.reason

    def test_ranger_still_waits_when_sidestep_also_fails(self, monkeypatch):
        """If both _move and _firing_line_sidestep fail (all cells blocked),
        ranger should fall back to WAIT as last resort."""
        import arena_tactic.strategy.rangers as rangers_mod

        ranger = unit(11, UnitType.RANGER, (0, 1))
        enemy = unit(200, UnitType.RANGER, (0, 6), controlled=False)

        # Patch both _move and _firing_line_sidestep to return None
        monkeypatch.setattr(rangers_mod, "_move", lambda *a, **kw: None)
        monkeypatch.setattr(rangers_mod, "_firing_line_sidestep", lambda *a, **kw: None)

        result = choose_actions(
            turn(
                owned_core=core(),
                units=(ranger,),
                enemies=(enemy,),
            )
        )
        intent = next(item for item in result.intents if item.actor_id == ranger.id)
        assert intent.action is ActionKind.WAIT
        assert "blocked" in intent.reason

    def test_ranger_still_shoots_when_target_in_range(self):
        """When ranger already has a target in range, shoot — no sidestep needed."""
        ranger = unit(11, UnitType.RANGER, (0, 0))
        enemy = unit(200, UnitType.RANGER, (3, 0), controlled=False)

        result = choose_actions(
            turn(
                owned_core=core(),
                units=(ranger,),
                enemies=(enemy,),
            )
        )
        intent = next(item for item in result.intents if item.actor_id == ranger.id)
        # Should shoot, not move
        assert intent.action is ActionKind.SHOOT

    def test_ranger_engages_enemy_at_range_5_when_move_blocked(self, monkeypatch):
        """With _move blocked, ranger should sidestep toward an enemy at
        range 5 (within engagement range but not shoot range)."""
        import arena_tactic.strategy.rangers as rangers_mod

        # Enemy at range 5 along the x-axis — not directly shootable
        ranger = unit(11, UnitType.RANGER, (5, 5))
        enemy = unit(200, UnitType.RANGER, (10, 5), controlled=False)
        _core = core(position=(0, 0))

        monkeypatch.setattr(rangers_mod, "_move", lambda *a, **kw: None)

        result = choose_actions(
            turn(
                owned_core=_core,
                units=(ranger,),
                enemies=(enemy,),
            )
        )
        intent = next(item for item in result.intents if item.actor_id == ranger.id)
        # Should be a sidestep MOVE, not SHOOT (out of range) or WAIT
        assert intent.action is ActionKind.MOVE
        assert "sidestep" in intent.reason
