"""三大看板功能补充测试：top-N 候选、chunk 配额、策略热更新白名单。"""
import pytest

from arena_tactic.chunk_quota import chunk_quota, compute_chunk_saturation
from arena_tactic.command_center import _validate_body, CommandError
from arena_tactic.tactical_geometry import rich_resource_center


def _payload(raw):
    return _validate_body({"type": "UPDATE_POLICY", "payload": raw}, current_tick=100)


# ---------- chunk 配额公式（官方规则 v0.14） ----------

def test_chunk_quota_center_ring_is_16():
    # 中心 2x2 chunks (cx,cy in {-1,0}) ring=0 → quota=16
    assert chunk_quota(0, 0) == 16
    assert chunk_quota(-1, -1) == 16


def test_chunk_quota_outer_rings_decay_and_floor_at_2():
    assert chunk_quota(1, 0) == max(2, int(16 * 8 / (8 + 1)))
    assert chunk_quota(5, 5) >= 2
    # 极远处配额触底为 2
    assert chunk_quota(50, 50) == 2


def test_negative_axis_convention():
    # axis(c) = c if c>=0 else -c-1 → cx=-2 → axis=1 → ring=1
    assert chunk_quota(-2, 0) == chunk_quota(1, 0)


# ---------- top-N 候选 ----------

def test_rich_resource_center_returns_top_n_with_metadata():
    observations = {(4, 0): 10, (4, 1): 11, (40, 40): 12}
    result = rich_resource_center(observations, current_tick=20, top_n=3)
    assert isinstance(result, list) and len(result) == 2
    first = result[0]
    assert set(first) >= {"center", "score", "resource_count", "resources"}
    assert first["score"] >= result[1]["score"]
    # 最富桶应包含两个相邻矿点
    assert first["resource_count"] == 2


def test_rich_resource_center_empty_returns_none():
    assert rich_resource_center({}, current_tick=10, top_n=3) is None


# ---------- 策略热更新白名单 ----------

def test_update_policy_accepts_numeric_override():
    _, payload, _, _ = _payload({"posture": "ECONOMY", "core_guard_vanguards": 1})
    assert payload["posture"] == "ECONOMY"
    assert payload["core_guard_vanguards"] == 1


def test_update_policy_accepts_retreat_heal_ratio_override():
    _, payload, _, _ = _payload({"posture": "ECONOMY", "unit_retreat_heal_ratio": 0.75})
    assert payload["unit_retreat_heal_ratio"] == 0.75


def test_update_policy_rejects_out_of_range_retreat_heal_ratio():
    with pytest.raises(CommandError):
        _payload({"posture": "ECONOMY", "unit_retreat_heal_ratio": 1.1})


def test_update_policy_accepts_retreat_heal_return_ratio_override():
    _, payload, _, _ = _payload(
        {"posture": "ECONOMY", "unit_retreat_heal_return_ratio": 0.7}
    )
    assert payload["unit_retreat_heal_return_ratio"] == 0.7


def test_update_policy_rejects_out_of_range_retreat_heal_return_ratio():
    with pytest.raises(CommandError):
        _payload({"posture": "ECONOMY", "unit_retreat_heal_return_ratio": -0.1})


def test_update_policy_rejects_out_of_range():
    with pytest.raises(CommandError):
        _payload({"core_guard_vanguards": 99})


def test_update_policy_rejects_unknown_field_and_canary_switch():
    with pytest.raises(CommandError):
        _payload({"unknown_field": 1})
    with pytest.raises(CommandError):
        _payload({"planner_canary": 1})
