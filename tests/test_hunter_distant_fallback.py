"""Hunter ranger long-distance patrol fallback: anti-oscillation regression tests.

Covers the fix for UNIT_OSCILLATION where a scout ranger assigned to
``hunter_forward_recon`` with a target 400+ cells away would fall through
A* pathfinding into ``_deploy_sidestep``, causing a 2-cell local oscillation
between adjacent tiles instead of making steady greedy progress toward the
distant target.
"""
from __future__ import annotations

from arena_hero import Direction, UnitType

from arena_tactic import AgentConfig, AgentMemory, choose_actions
from arena_tactic.models import ActionKind, Position, ReservationTable
from arena_tactic.navigation import distance
from arena_tactic.strategy.common import _distant_retreat_fallback_intent
from arena_tactic.context import DecisionContext

from .factories import core, turn, unit, uuid


# ---------------------------------------------------------------------------
# Direct helper tests
# ---------------------------------------------------------------------------

class TestHunterDistantFallbackHelper:
    """Direct unit tests for _distant_retreat_fallback_intent in hunter context."""

    def _make_context_and_memory(
        self,
        ranger_pos: Position,
        core_pos: Position = (0, 0),
        obstacles: set[Position] | None = None,
    ):
        ranger = unit(1, UnitType.RANGER, ranger_pos)
        _core = core(position=core_pos)
        obstacles = obstacles or set()
        t = turn(
            owned_core=_core,
            units=(ranger,),
            obstacle_cells=tuple(obstacles),
        )
        memory = AgentMemory()
        context = DecisionContext.from_turn(t)
        return ranger, context, memory

    def test_fallback_produces_move_toward_target(self):
        """Greedy fallback should produce a MOVE intent trending toward target."""
        ranger, context, memory = self._make_context_and_memory((-1239, -691))
        target = (-833, -574)
        reservations = ReservationTable(occupancy={})

        intent = _distant_retreat_fallback_intent(
            ranger, target, context, memory, reservations,
            "hunter_forward_recon",
        )
        assert intent is not None
        assert intent.action is ActionKind.MOVE
        assert intent.reserved_cell is not None
        # Should reduce Manhattan distance to target
        old_dist = distance(ranger.position, target)
        new_dist = distance(intent.reserved_cell, target)
        assert new_dist <= old_dist

    def test_fallback_anti_oscillation_taboo(self):
        """Recent cells should be penalised to prevent 2-cell bounce."""
        ranger, context, memory = self._make_context_and_memory((-1239, -691))
        target = (-833, -574)
        # Seed recent_cells so the unit bounces between two cells
        memory.unit_tasks[str(ranger.id)] = {
            "kind": "hunter",
            "target": list(target),
            "prev_cell": [-1238, -691],
            "recent_cells": [[-1238, -691], [-1239, -691], [-1238, -691], [-1239, -691]],
        }
        reservations = ReservationTable(occupancy={})

        intent = _distant_retreat_fallback_intent(
            ranger, target, context, memory, reservations,
            "hunter_forward_recon",
        )
        assert intent is not None
        assert intent.action is ActionKind.MOVE
        # Should NOT bounce back to the most-recent taboo cell
        assert intent.reserved_cell != (-1238, -691)

    def test_fallback_returns_none_when_fully_surrounded(self):
        """When every adjacent cell is blocked, return None."""
        ranger, context, memory = self._make_context_and_memory(
            (-1239, -691),
            obstacles={(-1238, -691), (-1240, -691), (-1239, -690), (-1239, -692)},
        )
        target = (-833, -574)
        reservations = ReservationTable(occupancy={})

        intent = _distant_retreat_fallback_intent(
            ranger, target, context, memory, reservations,
            "hunter_forward_recon",
        )
        assert intent is None


# ---------------------------------------------------------------------------
# Integration: full strategy pipeline
# ---------------------------------------------------------------------------

class TestHunterDistantFallbackIntegration:
    """Integration tests verifying the full ranger strategy pipeline."""

    def _make_scout_rangers_and_turn(
        self,
        ranger_count: int = 4,
        ranger_positions: list[Position] | None = None,
        core_pos: Position = (0, 0),
        enemies: tuple = (),
        tick: int = 1,
    ):
        """Build a turn with scout rangers and no guard rangers."""
        if ranger_positions is None:
            ranger_positions = [(20 + i, 20) for i in range(ranger_count)]
        rangers = tuple(
            unit(i + 1, UnitType.RANGER, pos)
            for i, pos in enumerate(ranger_positions[:ranger_count])
        )
        _core = core(position=core_pos)
        t = turn(tick=tick, owned_core=_core, units=rangers, enemies=enemies)
        return rangers, _core, t

    def test_distant_hunter_uses_fallback_not_sidestep(self):
        """A scout ranger 400+ cells from its hunter target should use the
        distant fallback (greedy step toward target), NOT _deploy_sidestep.
        
        This directly reproduces the Tick 274846 oscillation scenario:
        ranger at [-1239, -691], hunter target at [-833, -574], distance > 400.
        """
        rangers = [unit(i, UnitType.RANGER, (-1239 + i, -691)) for i in range(4)]
        target_ranger = rangers[0]  # at (-1239, -691)
        _core = core(position=(0, 0))
        t = turn(
            tick=1,
            owned_core=_core,
            units=tuple(rangers),
            enemies=(),
        )
        result = choose_actions(t)
        intent = next(item for item in result.intents if item.actor_id == target_ranger.id)
        # Should be a MOVE (greedy fallback), not WAIT
        assert intent.action is ActionKind.MOVE
        # The reason should indicate distant_fallback, NOT sidestep
        assert "distant_fallback" in intent.reason
        assert "sidestep" not in intent.reason
        # Should make progress: reduce distance to core area (toward patrol ring)
        old_dist = distance(target_ranger.position, (0, 0))
        new_dist = distance(intent.reserved_cell, (0, 0))
        assert new_dist <= old_dist

    def test_nearby_hunter_uses_normal_move(self):
        """A scout ranger within normal range of its hunter target should
        use the regular _move path, not the distant fallback."""
        # Place rangers close to core so their patrol targets are nearby
        rangers = [unit(i, UnitType.RANGER, (10 + i, 10)) for i in range(4)]
        target_ranger = rangers[0]
        _core = core(position=(0, 0))
        t = turn(
            tick=1,
            owned_core=_core,
            units=tuple(rangers),
            enemies=(),
        )
        result = choose_actions(t)
        intent = next(item for item in result.intents if item.actor_id == target_ranger.id)
        assert intent.action is ActionKind.MOVE
        # Should NOT use distant_fallback since ranger is close
        assert "distant_fallback" not in intent.reason

    def test_distant_hunter_anti_oscillation_multi_tick(self):
        """Simulate multiple ticks to verify the taboo anti-oscillation
        prevents the 2-cell bounce pattern observed in production."""
        rangers = [unit(i, UnitType.RANGER, (-1239 + i, -691)) for i in range(4)]
        target_ranger = rangers[0]
        _core = core(position=(0, 0))

        memory = AgentMemory()
        visited_cells: list[Position] = []

        for tick in range(1, 6):
            t = turn(
                tick=tick,
                owned_core=_core,
                units=tuple(rangers),
                enemies=(),
            )
            result = choose_actions(t, memory=memory)
            intent = next(item for item in result.intents if item.actor_id == target_ranger.id)
            assert intent.action is ActionKind.MOVE
            visited_cells.append(intent.reserved_cell)

        # Verify no 2-cell oscillation (no consecutive A-B-A pattern)
        for i in range(2, len(visited_cells)):
            assert not (visited_cells[i] == visited_cells[i - 2] and visited_cells[i] != visited_cells[i - 1]), (
                f"2-cell oscillation detected at tick {i + 1}: "
                f"{visited_cells[i - 2]} -> {visited_cells[i - 1]} -> {visited_cells[i]}"
            )

    def test_distant_hunter_makes_net_progress_over_time(self):
        """Over several ticks, the ranger should make net progress toward
        its hunter target (closer to core patrol ring)."""
        initial_pos: Position = (-1239, -691)
        initial_dist = distance(initial_pos, (0, 0))

        memory = AgentMemory()
        last_pos = initial_pos
        progress_count = 0

        for tick in range(1, 6):
            rangers = tuple(
                unit(i + 1, UnitType.RANGER, (-1239 + i, -691))
                for i in range(4)
            )
            # Override ranger 1 with current position
            all_rangers = list(rangers)
            all_rangers[0] = unit(1, UnitType.RANGER, last_pos)
            _core = core(position=(0, 0))
            t = turn(
                tick=tick,
                owned_core=_core,
                units=tuple(all_rangers),
                enemies=(),
            )
            result = choose_actions(t, memory=memory)
            # Find our target ranger's intent by id
            target_id = uuid(1)
            matching = [item for item in result.intents if item.actor_id == target_id]
            if not matching:
                break
            intent = matching[0]
            if intent.action is ActionKind.MOVE and intent.reserved_cell:
                new_dist = distance(intent.reserved_cell, (0, 0))
                if new_dist < distance(last_pos, (0, 0)):
                    progress_count += 1
                last_pos = intent.reserved_cell

        final_dist = distance(last_pos, (0, 0))
        # After multiple ticks, the ranger should have made some net progress
        assert final_dist < initial_dist or progress_count > 0, (
            f"No net progress after ticks: started at dist {initial_dist}, "
            f"ended at dist {final_dist}, progress steps: {progress_count}"
        )

    def test_distant_hunter_uses_fallback_when_move_blocked(self, monkeypatch):
        """Even when _move returns None, a distant hunter should use
        distant_fallback instead of _deploy_sidestep."""
        import arena_tactic.strategy.rangers as rangers_mod

        rangers = [unit(i, UnitType.RANGER, (-1239 + i, -691)) for i in range(4)]
        target_ranger = rangers[0]
        _core = core(position=(0, 0))

        # Patch _move to always return None
        monkeypatch.setattr(rangers_mod, "_move", lambda *a, **kw: None)

        t = turn(
            tick=1,
            owned_core=_core,
            units=tuple(rangers),
            enemies=(),
        )
        result = choose_actions(t)
        intent = next(item for item in result.intents if item.actor_id == target_ranger.id)
        assert intent.action is ActionKind.MOVE
        # distant_fallback should be used, not sidestep
        assert "distant_fallback" in intent.reason
