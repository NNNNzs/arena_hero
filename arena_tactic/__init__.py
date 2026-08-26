"""Adaptive Arena Hero tactic package."""

from .context import DecisionContext
from .memory import AgentMemory, MemoryStore
from .models import (
    ActionIntent,
    ActionKind,
    AgentConfig,
    DecisionResult,
    RejectedIntent,
    StrategicMode,
)
from .navigation import adjacent_direction, distance, shot_range
from .integrated_runtime import AgentRuntime, choose_actions
from .strategy import choose_mode, ranger_target_score, vanguard_cell_score

# Keep compatibility imports on the same integrated implementations used by
# the service/public package without rewriting the mature base modules.
from . import runtime as _runtime_module
_runtime_module.AgentRuntime = AgentRuntime
_runtime_module.choose_actions = choose_actions

from . import dashboard as _dashboard_module
from .dashboard_integration import (
    DashboardDataStore as _IntegratedDashboardDataStore,
    dashboard_static_asset as _integrated_dashboard_static_asset,
)
_dashboard_module.DashboardDataStore = _IntegratedDashboardDataStore
_dashboard_module.dashboard_static_asset = _integrated_dashboard_static_asset

__all__ = [
    "ActionIntent",
    "ActionKind",
    "AgentConfig",
    "AgentMemory",
    "AgentRuntime",
    "DecisionContext",
    "DecisionResult",
    "MemoryStore",
    "RejectedIntent",
    "StrategicMode",
    "adjacent_direction",
    "choose_actions",
    "choose_mode",
    "distance",
    "ranger_target_score",
    "shot_range",
    "vanguard_cell_score",
]
