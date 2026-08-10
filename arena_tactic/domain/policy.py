"""Versioned policy values reserved for the scheduler migration."""

from dataclasses import dataclass, field
from typing import Mapping

from .values import freeze_mapping, freeze_text


@dataclass(frozen=True, slots=True)
class Policy:
    version: int
    effective_tick: int
    posture: str = "BALANCED"
    weights: Mapping[str, float] = field(default_factory=dict)
    thresholds: Mapping[str, float] = field(default_factory=dict)
    safety_limits: Mapping[str, int] = field(default_factory=dict)
    planner_feature_flags: Mapping[str, bool] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "posture", freeze_text(self.posture, field_name="Policy.posture"))
        for name in ("weights", "thresholds", "safety_limits", "planner_feature_flags"):
            object.__setattr__(self, name, freeze_mapping(getattr(self, name), field_name=f"Policy.{name}"))
