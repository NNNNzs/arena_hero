"""Dashboard integration fixes for tactical squads and static map memory."""

from __future__ import annotations

import json
import zlib
from typing import Any, Callable

from .dashboard import DashboardDataStore as _BaseDashboardDataStore
from .dashboard import dashboard_static_asset as _base_dashboard_static_asset


_SQUADS = (
    ("squad_base_defense", "基地防御防线", "BASE_DEFENSE"),
    ("squad_expedition_beacon", "信标远征打击群", "EXPEDITION_BEACON"),
    ("squad_mining_escort", "矿区采矿与护航队", "MINING_ESCORT"),
    ("squad_scout_recon", "迷雾探索机动组", "SCOUT_RECON"),
)


def _canonical_alias(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return value if value.startswith("entity_") else f"entity_{value}"


def _cell_list(value: Any) -> list[list[int]]:
    if not isinstance(value, list):
        return []
    return [
        [item[0], item[1]]
        for item in value
        if isinstance(item, list)
        and len(item) == 2
        and all(type(axis) is int for axis in item)
    ]


def _integrated_command_center_js(source: str) -> str:
    """Upgrade the packaged UI without duplicating the large generated asset."""
    source = source.replace(
        "START_CORE_MIGRATION: '开始核心迁移', CANCEL_CORE_MIGRATION: '取消核心迁移', UPDATE_POLICY: '更新策略',",
        "START_CORE_MIGRATION: '开始核心迁移', CANCEL_CORE_MIGRATION: '取消核心迁移', UPDATE_POLICY: '更新策略', ASSIGN_SQUAD: '调整编组',",
    )
    source = source.replace(
        "  intercept_vanguards: '远征编组·先锋编制',\n"
        "  intercept_rangers: '远征编组·游侠编制',\n"
        "  resource_recheck_worker_limit: '矿区护航·工兵编制',\n",
        "  expedition_vanguards: '远征编组·先锋编制',\n"
        "  expedition_rangers: '远征编组·游侠编制',\n"
        "  beacon_secure_radius: '信标撤离·基地安全半径',\n"
        "  mining_escort_vanguards: '矿区护航·先锋编制',\n"
        "  mining_escort_rangers: '矿区护航·游侠编制',\n"
        "  scout_vanguards: '侦察巡逻·先锋编制',\n"
        "  scout_rangers: '侦察巡逻·游侠编制',\n",
    )
    start_marker = "$('squadList').onchange = async event => {"
    end_marker = "\n$('unitFilters').onclick = event => {"
    start = source.find(start_marker)
    end = source.find(end_marker, start + 1) if start >= 0 else -1
    if start >= 0 and end > start:
        replacement = """$('squadList').onchange = async event => {
  const select = event.target.closest('.squad-switch-select');
  if (!select) return;
  const alias = select.dataset.alias;
  const targetSquadId = select.value;
  if (!alias || !targetSquadId) return;
  if (!csrf) {
    alert('请先在右下方“人工任务”面板中输入管理员口令完成认证。');
    if (activeRosterTab === 'squads') renderSquadList();
    return;
  }
  try {
    await api('/api/v1/commands', 'POST', {
      type: 'ASSIGN_SQUAD',
      payload: { entity_alias: alias, squad_id: targetSquadId },
    });
    if (squadAssignmentsState) squadAssignmentsState[alias] = targetSquadId;
    setText('taskState', '编组调整已排队，下一次成功提交后持久生效。');
    if (activeRosterTab === 'squads') renderSquadList();
  } catch (err) {
    alert(`切换编组失败: ${err.message}`);
    if (activeRosterTab === 'squads') renderSquadList();
  }
};"""
        source = source[:start] + replacement + source[end:]
    return source


def dashboard_static_asset(path: str) -> tuple[bytes, str] | None:
    asset = _base_dashboard_static_asset(path)
    if asset is None or path != "/static/command-center.js":
        return asset
    payload, content_type = asset
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        return asset
    return _integrated_command_center_js(text).encode("utf-8"), content_type


class DashboardDataStore(_BaseDashboardDataStore):
    """Expose the same canonical squad and map-memory facts as live strategy."""

    @staticmethod
    def _compute_squads_summary(
        memory_data: dict[str, Any],
        latest: dict[str, Any] | None,
        policy_state: dict[str, Any],
    ) -> dict[str, Any]:
        if not latest or not isinstance(latest.get("map"), dict):
            return {"squads": [], "assignments": {}}

        friendly = latest["map"].get("friendly", [])
        if not isinstance(friendly, list):
            friendly = []
        core = next((item for item in friendly if isinstance(item, dict) and item.get("kind") == "CORE"), None)
        core_pos = core.get("position") if isinstance(core, dict) else None
        beacon = latest.get("map", {}).get("beacon") or {}
        beacon_pos = beacon.get("position") if isinstance(beacon, dict) else None
        mode = str(latest.get("mode") or "ECONOMY")

        squads: dict[str, dict[str, Any]] = {}
        for squad_id, name, squad_type in _SQUADS:
            target = beacon_pos if squad_id == "squad_expedition_beacon" else core_pos
            status = {
                "squad_base_defense": "警戒中",
                "squad_expedition_beacon": "推进中" if mode == "BEACON" else "待命中",
                "squad_mining_escort": "作业中",
                "squad_scout_recon": "侦察巡逻中",
            }[squad_id]
            squads[squad_id] = {
                "id": squad_id,
                "name": name,
                "type": squad_type,
                "target": target,
                "members": [],
                "status": status,
            }

        unit_tasks = memory_data.get("unit_tasks", {})
        if not isinstance(unit_tasks, dict):
            unit_tasks = {}
        manual = memory_data.get("manual_squad_assignments", {})
        if not isinstance(manual, dict):
            manual = {}
        assignments: dict[str, str] = {}

        for item in friendly:
            if not isinstance(item, dict) or item.get("kind") == "CORE":
                continue
            alias = _canonical_alias(item.get("alias"))
            if alias is None:
                continue
            kind = str(item.get("kind") or "")
            task = unit_tasks.get(alias) or {}
            task_kind = str(task.get("kind") or "") if isinstance(task, dict) else ""
            manual_squad = manual.get(alias)
            if isinstance(manual_squad, str) and manual_squad in squads:
                squad_id = manual_squad
            elif "expedition" in task_kind or task_kind == "beacon":
                squad_id = "squad_expedition_beacon"
            elif task_kind in {"core_guard", "defense_search", "mineral_tank", "intercept"}:
                squad_id = "squad_base_defense"
            elif task_kind in {"harvest", "return", "vacate", "assigned_resource", "recon_escort", "resource"} or kind == "WORKER":
                squad_id = "squad_mining_escort"
            else:
                squad_id = "squad_scout_recon"

            assignments[alias] = squad_id
            squads[squad_id]["members"].append({
                "alias": alias,
                "kind": kind,
                "position": item.get("position"),
                "hp": item.get("hp"),
                "cargo": item.get("cargo"),
                "task": task_kind,
            })

        flow_by_mode = {
            "ATTACK": "ATTACK（进攻模式）优先编入基地防御与机动巡逻，远征信标编制暂停。",
            "DEFEND": "DEFEND（防守模式）优先回填核心防线，其余编组保留最低必要成员。",
            "BEACON": "BEACON（信标模式）优先维持信标远征与护卫编制。",
            "ECONOMY": "ECONOMY（经济模式）优先保障采矿与资源护航。",
            "EXPLORE": "EXPLORE（探索模式）将可用战斗单位调往前沿侦察。",
            "RECOVER": "RECOVER（恢复模式）暂停远离核心的高风险编组行动。",
            "RESPAWN": "RESPAWN（重生模式）等待下一份权威状态后重新编组。",
        }
        for squad in squads.values():
            members = squad["members"]
            positions = [member["position"] for member in members if isinstance(member.get("position"), list) and len(member["position"]) == 2]
            squad["causality"] = {
                "flow_reason": flow_by_mode.get(mode, "当前策略模式重新分配编组。"),
                "mode": mode,
                "member_aliases": [member["alias"] for member in members],
                "centroid": [round(sum(cell[0] for cell in positions) / len(positions)), round(sum(cell[1] for cell in positions) / len(positions))] if positions else None,
                "coordination_target": squad["target"],
            }

        return {"squads": list(squads.values()), "assignments": assignments}

    def _integrated_memory_payload(self) -> dict[str, Any]:
        memory = self._memory()
        explored = _cell_list(memory.get("explored", []))
        mined = _cell_list(memory.get("mined_cells", []))
        obstacles = _cell_list(memory.get("obstacles", []))
        known_resources: list[list[int]] = []
        observations = memory.get("resource_observations", {})
        if isinstance(observations, dict):
            for key in observations:
                try:
                    x_text, y_text = str(key).split(",", 1)
                    known_resources.append([int(x_text), int(y_text)])
                except (TypeError, ValueError):
                    continue
        known_resources.sort()
        canonical = json.dumps(
            {
                "explored": explored,
                "mined": mined,
                "obstacles": obstacles,
                "known_resources": known_resources,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        version = zlib.crc32(canonical) & 0xFFFFFFFF
        return {
            "version": version,
            "explored_segments": self._compress_explored_segments(explored),
            "mined": mined,
            "obstacles": obstacles,
            "known_resources": known_resources,
        }

    def map_memory_payload(self) -> dict[str, Any]:
        return self._integrated_memory_payload()

    def payload(
        self,
        status_snapshot: Callable[[], dict[str, object]],
        *,
        event_limit: int = 200,
    ) -> dict[str, Any]:
        payload = super().payload(status_snapshot, event_limit=event_limit)
        version = self._integrated_memory_payload()["version"]
        payload["map_memory_version"] = version
        current = payload.get("current")
        if isinstance(current, dict) and isinstance(current.get("map"), dict):
            current["map"]["memory_version"] = version
        return payload

    def replay_payload(self, status_snapshot, *, limit=32, from_tick=None, to_tick=None):
        payload = super().replay_payload(
            status_snapshot,
            limit=limit,
            from_tick=from_tick,
            to_tick=to_tick,
        )
        version = self._integrated_memory_payload()["version"]
        for frame in payload.get("frames", []):
            snapshot = frame.get("snapshot") if isinstance(frame, dict) else None
            if isinstance(snapshot, dict) and isinstance(snapshot.get("map"), dict):
                snapshot["map"]["memory_version"] = version
        return payload
