"""Tick-based analysis task scheduler for periodic heavy computations.

The scheduler is purely functional: ``advance()`` returns the names of due
tasks without executing anything.  Callers are responsible for running the
actual analysis and writing results back to memory.

Design rationale
----------------
* **O(task-count) per tick** – each task stores ``last_run_tick`` so the due
  check is a single subtraction; no global re-sort is needed.
* **Dynamic intervals** – ``interval`` may be a static ``int`` or a callable
  ``(explored_count, resource_count) -> int`` that is clamped to
  ``[min_interval, max_interval]``.  This lets the resource-density scan
  slow down as the explored map grows.
* **Respawn behaviour** – on Core respawn we reset ``last_run_tick`` to
  *None* so the task fires again on its next eligible tick, but we keep the
  task definition and ``enabled`` flag.  The cached
  ``migration_recommendation`` is *also* cleared on respawn (new map → stale
  coordinates).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from .models import Position

# ---------------------------------------------------------------------------
# Interval strategies (serializable by name)
# ---------------------------------------------------------------------------

IntervalFn = Callable[[int, int], int]
"""Signature: ``(explored_count, resource_count) -> interval_ticks``."""

_INTERVAL_REGISTRY: dict[str, IntervalFn] = {
    # Longer interval as explored map grows – base 60, +60 per 5000 explored.
    "resource_density_adaptive": lambda ec, _rc: max(20, min(300, 60 * (1 + ec // 5000))),
}

MIN_INTERVAL = 20
MAX_INTERVAL = 300


def _resolve_interval(raw: int | str | IntervalFn) -> int | IntervalFn:
    """Return a usable interval value from a serialized or live form."""
    if callable(raw):
        return raw
    if isinstance(raw, str):
        fn = _INTERVAL_REGISTRY.get(raw)
        if fn is not None:
            return fn
        raise ValueError(f"Unknown interval strategy: {raw!r}")
    return int(raw)


def _effective_interval(
    interval: int | IntervalFn,
    explored_count: int,
    resource_count: int,
) -> int:
    """Compute and clamp the interval for a given data size."""
    value = interval(explored_count, resource_count) if callable(interval) else int(interval)
    return max(MIN_INTERVAL, min(MAX_INTERVAL, value))


# ---------------------------------------------------------------------------
# AnalysisTask
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class AnalysisTask:
    """A single periodic analysis job."""

    name: str
    start_tick: int
    interval: int | IntervalFn
    last_run_tick: int | None = None
    enabled: bool = True

    # -- helpers --------------------------------------------------------------

    def is_due(self, tick: int, explored_count: int, resource_count: int) -> bool:
        """Return *True* when the task should execute at *tick*.

        O(1) – one subtraction, one callable invocation at most.
        """
        if not self.enabled:
            return False
        if self.last_run_tick is None:
            return tick >= self.start_tick
        eff = _effective_interval(self.interval, explored_count, resource_count)
        return tick - self.last_run_tick >= eff

    def current_interval(self, explored_count: int, resource_count: int) -> int:
        return _effective_interval(self.interval, explored_count, resource_count)

    # -- serialization --------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        interval_raw: int | str
        if callable(self.interval):
            # Reverse-lookup the registry name.
            for name, fn in _INTERVAL_REGISTRY.items():
                if fn is self.interval:
                    interval_raw = name
                    break
            else:
                raise ValueError(
                    "Cannot serialize anonymous interval callable; "
                    "register it in _INTERVAL_REGISTRY"
                )
        else:
            interval_raw = int(self.interval)
        return {
            "name": self.name,
            "start_tick": self.start_tick,
            "interval": interval_raw,
            "last_run_tick": self.last_run_tick,
            "enabled": self.enabled,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AnalysisTask:
        return cls(
            name=str(data.get("name", "")),
            start_tick=int(data.get("start_tick", 0)),
            interval=_resolve_interval(data.get("interval", 60)),
            last_run_tick=(
                int(data["last_run_tick"])
                if data.get("last_run_tick") is not None
                else None
            ),
            enabled=bool(data.get("enabled", True)),
        )


# ---------------------------------------------------------------------------
# Migration recommendation (cached result)
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class MigrationRecommendation:
    """Cached output of the resource-density scan."""

    center: Position
    score: float
    computed_at_tick: int
    interval_ticks: int  # interval active at computation time

    def is_fresh(self, current_tick: int, max_cycles: int = 2) -> bool:
        """Return *True* if the cache has not expired (within *max_cycles*)."""
        return current_tick - self.computed_at_tick < max_cycles * self.interval_ticks

    def to_dict(self) -> dict[str, Any]:
        return {
            "center": list(self.center),
            "score": self.score,
            "computed_at_tick": self.computed_at_tick,
            "interval_ticks": self.interval_ticks,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MigrationRecommendation | None:
        if not isinstance(data, dict):
            return None
        center = data.get("center")
        if not isinstance(center, (list, tuple)) or len(center) != 2:
            return None
        return cls(
            center=(int(center[0]), int(center[1])),
            score=float(data.get("score", 0.0)),
            computed_at_tick=int(data.get("computed_at_tick", 0)),
            interval_ticks=int(data.get("interval_ticks", 60)),
        )


# ---------------------------------------------------------------------------
# AnalysisScheduler
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class AnalysisScheduler:
    """Container for analysis tasks with pure-functional due-date checking.

    ``advance(tick, explored_count, resource_count)`` returns a **list of task
    names** that are due.  It never mutates the tasks; the caller must call
    ``mark_completed(name, tick)`` after executing each due task.
    """

    tasks: list[AnalysisTask] = field(default_factory=list)

    # -- core API -------------------------------------------------------------

    def advance(
        self,
        tick: int,
        explored_count: int,
        resource_count: int,
    ) -> list[str]:
        """Return names of tasks due at *tick*.  O(len(tasks))."""
        return [
            task.name
            for task in self.tasks
            if task.is_due(tick, explored_count, resource_count)
        ]

    def mark_completed(self, name: str, tick: int) -> None:
        """Update ``last_run_tick`` for the named task."""
        for task in self.tasks:
            if task.name == name:
                task.last_run_tick = tick
                return

    def get_task(self, name: str) -> AnalysisTask | None:
        for task in self.tasks:
            if task.name == name:
                return task
        return None

    # -- serialization --------------------------------------------------------

    def to_dict(self) -> list[dict[str, Any]]:
        return [task.to_dict() for task in self.tasks]

    @classmethod
    def from_dict(cls, data: list[dict[str, Any]]) -> AnalysisScheduler:
        tasks = [AnalysisTask.from_dict(item) for item in data] if isinstance(data, list) else []
        return cls(tasks=tasks)

    # -- respawn handling -----------------------------------------------------

    def on_respawn(self) -> None:
        """Reset ``last_run_tick`` on all tasks so they re-fire promptly.

        We intentionally keep the task definitions and ``enabled`` flags –
        only the execution watermark is cleared.  This means a respawned agent
        does not wait the full dynamic interval before its first analysis.
        """
        for task in self.tasks:
            task.last_run_tick = None


# ---------------------------------------------------------------------------
# Default scheduler factory
# ---------------------------------------------------------------------------

def default_analysis_scheduler() -> AnalysisScheduler:
    """Return a scheduler pre-configured with the resource-density scan."""
    return AnalysisScheduler(
        tasks=[
            AnalysisTask(
                name="resource_density_scan",
                start_tick=10,
                interval=_INTERVAL_REGISTRY["resource_density_adaptive"],
                last_run_tick=None,
                enabled=True,
            ),
        ]
    )
