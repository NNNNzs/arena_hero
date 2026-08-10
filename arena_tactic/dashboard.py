"""Bounded, redacted dashboard projection for the Arena Hero service."""

from __future__ import annotations

import json
import re
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Any, Callable


MODE_LABELS = {
    "RESPAWN": "等待重生",
    "RECOVER": "恢复",
    "DEFEND": "防守",
    "ECONOMY": "发展经济",
    "EXPLORE": "探索",
    "BEACON": "争夺信标",
    "ATTACK": "进攻",
}
ACTION_LABELS = {
    "WAIT": "等待",
    "MOVE": "移动",
    "HARVEST": "采集",
    "DEPOSIT": "存储",
    "SWEEP": "横扫",
    "SHOOT": "射击",
    "HEAL": "治疗",
    "SPAWN": "生产",
    "REPAIR_SHIELD": "修复护盾",
    "START_MOVE": "核心迁移",
    "PICKUP_BEACON": "拾取信标",
}


def _bounded_jsonl_tail(path: Path, *, max_bytes: int, limit: int) -> list[dict[str, Any]]:
    """Read only a bounded file tail and ignore partial or malformed lines."""
    if max_bytes <= 0 or limit <= 0:
        return []
    try:
        with path.open("rb") as stream:
            stream.seek(0, 2)
            size = stream.tell()
            start = max(0, size - max_bytes)
            stream.seek(start)
            raw = stream.read(max_bytes)
    except (FileNotFoundError, OSError):
        return []
    lines = raw.splitlines()
    if start and lines:
        lines = lines[1:]
    records: list[dict[str, Any]] = []
    for line in lines:
        try:
            value = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if isinstance(value, dict):
            records.append(value)
    return records[-limit:]


def _integer(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _safe_text(value: Any, *, maximum: int = 80) -> str | None:
    if not isinstance(value, str):
        return None
    return value[:maximum]


def redact_error(value: Any) -> str | None:
    text = _safe_text(value, maximum=240)
    if not text:
        return None
    patterns = (
        r"(?i)(api[-_ ]?key\s*[:=]\s*)[^,;\r\n]+",
        r"(?i)(authorization\s*[:=]\s*)[^,;\r\n]+",
        r"(?i)(cookie\s*[:=]\s*)[^,;\r\n]+",
        r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+",
    )
    for pattern in patterns:
        text = re.sub(pattern, lambda match: match.group(1) + "[已脱敏]" if match.lastindex else "[已脱敏]", text)
    text = re.sub(
        r"(?i)\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b",
        "[对象标识已脱敏]",
        text,
    )
    return text


def _project_record(record: dict[str, Any]) -> dict[str, Any]:
    """Allowlist fields displayed by the browser; never pass records through."""
    state = record.get("state") if isinstance(record.get("state"), dict) else {}
    raw_intents = record.get("intents") if isinstance(record.get("intents"), list) else []
    raw_events = record.get("events") if isinstance(record.get("events"), list) else []
    actions = Counter(
        action
        for item in raw_intents
        if isinstance(item, dict)
        and (action := _safe_text(item.get("action"), maximum=32))
    )
    events = []
    for item in raw_events[-8:]:
        if not isinstance(item, dict):
            continue
        event_type = _safe_text(item.get("type"), maximum=48)
        if not event_type:
            continue
        events.append({
            "type": event_type,
            "reason": _safe_text(item.get("reason"), maximum=64),
        })
    mode = _safe_text(record.get("mode"), maximum=32)
    def _map_object(value: Any, *, enemy: bool = False) -> dict[str, Any] | None:
        if not isinstance(value, dict):
            return None
        position = value.get("position")
        identity = _safe_text(value.get("id"), maximum=24)
        kind = _safe_text(value.get("unit_type") or value.get("kind"), maximum=24)
        if not isinstance(position, list) or len(position) != 2 or not all(type(axis) is int for axis in position) or not identity or not kind:
            return None
        return {"alias": identity, "kind": kind, "position": position,
                "hp": _integer(value.get("hp")), "cargo": _integer(value.get("cargo")), "enemy": enemy}
    beacon = state.get("beacon") if isinstance(state.get("beacon"), dict) else {}
    raw_units = state.get("units") if isinstance(state.get("units"), list) else []
    raw_enemies = state.get("visible_enemies") if isinstance(state.get("visible_enemies"), list) else []
    raw_resources = state.get("resource_cells") if isinstance(state.get("resource_cells"), list) else []
    raw_obstacles = state.get("obstacle_cells") if isinstance(state.get("obstacle_cells"), list) else []
    return {
        "tick": _integer(record.get("tick")),
        "mode": mode,
        "mode_label": MODE_LABELS.get(mode, mode or "未知"),
        "resources": _integer(state.get("resources")),
        "resource_capacity": _integer(state.get("resource_capacity")),
        "population": _integer(state.get("population")),
        "accepted": bool(record.get("accepted")),
        "decision_ms": _number(record.get("decision_ms")),
        "timed_out": bool(record.get("timed_out")),
        "actions": [
            {"type": name, "label": ACTION_LABELS.get(name, name), "count": count}
            for name, count in actions.most_common()
        ],
        "events": events,
        "map": {
            "friendly": [item for value in ([state.get("core")] + raw_units[:100]) if (item := _map_object(value))],
            "enemies": [item for value in raw_enemies[:100] if (item := _map_object(value, enemy=True))],
            "resources": [list(cell) for cell in raw_resources[:200] if isinstance(cell, list) and len(cell) == 2 and all(type(axis) is int for axis in cell)],
            "obstacles": [list(cell) for cell in raw_obstacles[:200] if isinstance(cell, list) and len(cell) == 2 and all(type(axis) is int for axis in cell)],
            "beacon": {"position": list(beacon.get("position")) if isinstance(beacon.get("position"), list) and len(beacon["position"]) == 2 else None,
                       "status": _safe_text(beacon.get("status"), maximum=24)},
        },
    }


def _project_trace_record(record: dict[str, Any]) -> dict[str, Any] | None:
    """Expose a bounded allowlist from the decision trace, never raw state."""
    if record.get("record_type") not in {None, "decision_trace"}:
        return None
    tick = _integer(record.get("tick"))
    if tick is None:
        return None
    entities = []
    for item in record.get("entity_traces", ())[:200]:
        if not isinstance(item, dict):
            continue
        alias = _safe_text(item.get("actor_alias"), maximum=32)
        if not alias or not re.fullmatch(r"entity_[0-9a-f]{12}", alias):
            continue
        raw_assignment = item.get("assignment") if isinstance(item.get("assignment"), dict) else {}
        entities.append({
            "alias": alias, "kind": _safe_text(item.get("entity_kind"), maximum=24),
            "task": _safe_text(item.get("current_task"), maximum=48),
            "action": _safe_text(item.get("action"), maximum=24),
            "status": _safe_text(item.get("status"), maximum=24),
            "reason": _safe_text((item.get("reason_codes") or [None])[0], maximum=80),
            "blocker": _safe_text(item.get("blocker"), maximum=80),
            "waited_ticks": _integer(item.get("waited_ticks")) or 0,
            "node_path": [
                {"node_id": _safe_text(node.get("node_id"), maximum=48),
                 "status": _safe_text(node.get("status"), maximum=24),
                 "reason": _safe_text(node.get("reason"), maximum=80)}
                for node in item.get("node_path", ())[:12] if isinstance(node, dict)
            ],
            "assignment": {
                "task_id": _safe_text(raw_assignment.get("task_id"), maximum=96),
                "goal": _safe_text(raw_assignment.get("goal"), maximum=64),
                "role": _safe_text(raw_assignment.get("role"), maximum=24),
                "lock": _safe_text(raw_assignment.get("lock"), maximum=96),
                "assigned_tick": _integer(raw_assignment.get("assigned_tick")),
                "lease_until_tick": _integer(raw_assignment.get("lease_until_tick")),
            } if raw_assignment else None,
        })
    def _summary(value: Any) -> dict[str, Any] | None:
        if not isinstance(value, dict):
            return None
        goal = _safe_text(value.get("goal"), maximum=48)
        status = _safe_text(value.get("status"), maximum=24)
        return {"goal": goal, "status": status, "stage": _safe_text(value.get("stage"), maximum=32)} if goal else None
    return {
        "tick": tick,
        "planner_version": _safe_text(record.get("planner_version"), maximum=80),
        "entities": entities,
        "goals": [item for value in record.get("goal_summaries", ())[:32] if (item := _summary(value))],
        "commands": [
            {"command_id": _safe_text(value.get("command_id"), maximum=48),
             "type": _safe_text(value.get("type"), maximum=32),
             "status": _safe_text(value.get("status"), maximum=32)}
            for value in record.get("command_results", ())[:32] if isinstance(value, dict)
        ],
        "tasks": [
            {"task_id": _safe_text(value.get("task_id"), maximum=96),
             "goal": _safe_text(value.get("goal"), maximum=64),
             "kind": _safe_text(value.get("kind"), maximum=48),
             "status": _safe_text(value.get("status"), maximum=24),
             "actor_alias": _safe_text(value.get("actor_alias"), maximum=32),
             "role": _safe_text(value.get("role"), maximum=24),
             "lock": _safe_text(value.get("lock"), maximum=96),
             "assigned_tick": _integer(value.get("assigned_tick")),
             "lease_until_tick": _integer(value.get("lease_until_tick")),
             "target": list(value.get("target")) if isinstance(value.get("target"), (list, tuple))
                       and len(value["target"]) == 2 and all(type(axis) is int for axis in value["target"]) else None,
             "waited_ticks": _integer(value.get("waited_ticks")) or 0,
             "reason": _safe_text(value.get("reason"), maximum=64)}
            for value in record.get("task_transitions", ())[:100] if isinstance(value, dict)
        ],
    }


class DashboardDataStore:
    """Small TTL cache around bounded replay-tail reads."""

    def __init__(
        self,
        replay_path: Path,
        *,
        trace_path: Path | None = None,
        max_bytes: int = 256 * 1024,
        recent_limit: int = 12,
        cache_seconds: float = 1.0,
    ) -> None:
        self.replay_path = replay_path
        self.trace_path = trace_path or replay_path.with_name("decision-trace.jsonl")
        self.max_bytes = max_bytes
        self.recent_limit = recent_limit
        self.cache_seconds = cache_seconds
        self._cached_at = 0.0
        self._cached_records: list[dict[str, Any]] = []
        self._cached_traces: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def _records(self) -> list[dict[str, Any]]:
        now = time.monotonic()
        with self._lock:
            if now - self._cached_at >= self.cache_seconds:
                self._cached_records = _bounded_jsonl_tail(
                    self.replay_path,
                    max_bytes=self.max_bytes,
                    limit=self.recent_limit,
                )
                self._cached_traces = _bounded_jsonl_tail(
                    self.trace_path, max_bytes=self.max_bytes, limit=self.recent_limit
                )
                self._cached_at = now
            return list(self._cached_records)

    def _traces(self) -> list[dict[str, Any]]:
        self._records()  # refresh both bounded caches under the same TTL/lock.
        with self._lock:
            return list(self._cached_traces)

    def payload(self, status_snapshot: Callable[[], dict[str, object]]) -> dict[str, Any]:
        raw_status = status_snapshot()
        status = {
            "status": _safe_text(raw_status.get("status"), maximum=16),
            "running": bool(raw_status.get("running")),
            "connected": bool(raw_status.get("connected")),
            "last_tick": _integer(raw_status.get("last_tick")),
            "accepted": _integer(raw_status.get("accepted")) or 0,
            "rejected": _integer(raw_status.get("rejected")) or 0,
            "reconnects": _integer(raw_status.get("reconnects")) or 0,
            "last_error": redact_error(raw_status.get("last_error")),
            "uptime_seconds": _number(raw_status.get("uptime_seconds")) or 0.0,
        }
        try:
            recent = [_project_record(record) for record in self._records()]
        except Exception:  # dashboard failure must never affect the match worker
            recent = []
        latest = recent[-1] if recent else None
        try:
            traces = [item for record in self._traces() if (item := _project_trace_record(record))]
        except Exception:
            traces = []
        command_center = traces[-1] if traces else None
        if command_center is not None:
            timeline = [
                {"tick": item["tick"], **task}
                for item in traces[-self.recent_limit:]
                for task in item["tasks"]
            ][-100:]
            command_center = {**command_center, "timeline": timeline}
        return {
            "schema_version": 1,
            "generated_at": int(time.time()),
            "service": status,
            "current": latest,
            "recent": list(reversed(recent)),
            "replay": {
                "available": bool(recent),
                "records": len(recent),
                "window_bytes": self.max_bytes,
            },
            "command_center": command_center,
        }


# Kept below for backwards-compatible source history; Phase 8 serves the
# maintained command center assets defined after this legacy shell.
DASHBOARD_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Arena Hero · 作战控制台</title>
<style>
:root{color-scheme:dark;--bg:#090d12;--panel:#111821;--panel2:#151e29;--line:#263342;--text:#e8eef5;--muted:#8fa0b3;--cyan:#49d7c4;--blue:#58a6ff;--red:#ff6b7a;--amber:#f4bd61}*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 15% -10%,#183044 0,transparent 35%),var(--bg);color:var(--text);font:14px/1.5 system-ui,-apple-system,"Segoe UI","Microsoft YaHei",sans-serif}main{width:min(1180px,calc(100% - 32px));margin:auto;padding:32px 0 56px}header{display:flex;align-items:flex-start;justify-content:space-between;gap:20px;margin-bottom:24px}h1{font-size:24px;letter-spacing:.04em;margin:0 0 5px}.sub,.muted{color:var(--muted)}.status{display:flex;align-items:center;gap:9px;padding:8px 12px;border:1px solid var(--line);border-radius:99px;background:#0c1219}.dot{width:9px;height:9px;border-radius:50%;background:var(--red);box-shadow:0 0 12px currentColor}.ok .dot{background:var(--cyan)}.grid{display:grid;grid-template-columns:repeat(12,1fr);gap:14px}.card{background:linear-gradient(145deg,var(--panel2),var(--panel));border:1px solid var(--line);border-radius:12px;padding:17px;min-width:0}.metric{grid-column:span 3}.wide{grid-column:span 8}.side{grid-column:span 4}.label{font-size:12px;color:var(--muted);letter-spacing:.08em}.value{font-size:27px;font-weight:650;margin-top:5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.value small{font-size:13px;color:var(--muted);font-weight:400}.section-title{font-size:15px;margin:0 0 13px}.bar{height:7px;border-radius:8px;background:#25303d;overflow:hidden;margin-top:10px}.bar i{display:block;height:100%;background:linear-gradient(90deg,var(--blue),var(--cyan));width:0}.error{color:#ffc1c7;word-break:break-word}.turn{display:grid;grid-template-columns:70px 110px 1fr 90px;gap:12px;align-items:center;padding:11px 0;border-top:1px solid var(--line)}.turn:first-of-type{border-top:0}.tag{display:inline-block;padding:3px 8px;border-radius:5px;background:#203349;color:#b9d9ff;font-size:12px}.chips{display:flex;gap:5px;flex-wrap:wrap}.chip{color:#b9c7d6;background:#1b2632;padding:2px 7px;border-radius:4px;font-size:12px}.empty{padding:30px 10px;text-align:center;color:var(--muted)}footer{margin-top:18px;color:var(--muted);font-size:12px}@media(max-width:820px){.metric{grid-column:span 6}.wide,.side{grid-column:span 12}.turn{grid-template-columns:58px 1fr}.turn .chips,.turn .latency{grid-column:2}}@media(max-width:480px){main{width:min(100% - 20px,1180px);padding-top:20px}header{display:block}.status{margin-top:14px;width:max-content}.metric{grid-column:span 12}.value{font-size:24px}}
</style></head><body><main><header><div><h1>ARENA HERO 作战控制台</h1><div class="sub">24/7 自主战术 Agent · 实时态势</div></div><div id="status" class="status"><span class="dot"></span><span>正在获取状态</span></div></header>
<section class="grid"><div class="card metric"><div class="label">运行时间</div><div class="value" id="uptime">—</div></div><div class="card metric"><div class="label">最近 TICK</div><div class="value" id="tick">—</div></div><div class="card metric"><div class="label">提交成功 / 失败</div><div class="value" id="submits">—</div></div><div class="card metric"><div class="label">重连次数</div><div class="value" id="reconnects">—</div></div>
<div class="card side"><h2 class="section-title">当前态势</h2><div class="label">策略模式</div><div class="value" id="mode">等待数据</div><div style="margin-top:17px" class="label">资源 / 容量</div><div class="value" id="resources">—</div><div class="bar"><i id="resourceBar"></i></div><div style="margin-top:17px" class="label">人口</div><div class="value" id="population">—</div></div>
<div class="card wide"><h2 class="section-title">最近回合</h2><div id="turns" class="empty">尚无回放记录</div></div><div class="card wide"><h2 class="section-title">最近错误</h2><div id="error" class="muted">无</div></div><div class="card side"><h2 class="section-title">数据状态</h2><div id="dataState" class="muted">正在同步…</div></div></section><footer>每 3 秒刷新 · 开发挂载模式 · 数据来自本机脱敏回放 · 页面不会展示凭据或完整对象标识</footer></main>
<script>
const $=id=>document.getElementById(id), esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function duration(s){s=Math.max(0,Math.floor(Number(s)||0));const d=Math.floor(s/86400),h=Math.floor(s%86400/3600),m=Math.floor(s%3600/60);return[d&&d+'天',h&&h+'时',m+'分'].filter(Boolean).join(' ')}
function render(d){const s=d.service||{},c=d.current,online=!!s.running,connected=!!s.connected;$('status').className='status '+(online&&connected?'ok':'');$('status').lastElementChild.textContent=!online?'服务已停止':connected?'已连接 · 对战中':'服务在线 · 等待连接';$('uptime').textContent=duration(s.uptime_seconds);$('tick').textContent=s.last_tick??c?.tick??'—';$('submits').innerHTML=`${s.accepted??0} <small>/ ${s.rejected??0}</small>`;$('reconnects').textContent=s.reconnects??0;$('mode').textContent=c?.mode_label||'等待数据';$('resources').innerHTML=c?`${c.resources??'—'} <small>/ ${c.resource_capacity??'—'}</small>`:'—';$('population').textContent=c?.population??'—';const pct=c&&c.resource_capacity?Math.min(100,Math.max(0,c.resources/c.resource_capacity*100)):0;$('resourceBar').style.width=pct+'%';$('error').className=s.last_error?'error':'muted';$('error').textContent=s.last_error||'无';$('dataState').textContent=d.replay?.available?`已载入最近 ${d.replay.records} 个有效回合；最后同步 ${new Date(d.generated_at*1000).toLocaleTimeString('zh-CN')}`:'回放尚不可用；服务会继续等待首个成功提交。';const rows=(d.recent||[]).map(r=>`<div class="turn"><b>#${esc(r.tick??'—')}</b><span class="tag">${esc(r.mode_label)}</span><div class="chips">${(r.actions||[]).map(a=>`<span class="chip">${esc(a.label)} × ${esc(a.count)}</span>`).join('')||'<span class="muted">无动作</span>'}${(r.events||[]).map(e=>`<span class="chip">事件 ${esc(e.type)}</span>`).join('')}</div><span class="latency muted">${r.decision_ms==null?'—':esc(r.decision_ms.toFixed(1))+' ms'}</span></div>`).join('');$('turns').className=rows?'':'empty';$('turns').innerHTML=rows||'尚无有效回放记录';}
async function refresh(){try{const r=await fetch('/api/dashboard',{cache:'no-store'});if(!r.ok)throw Error('HTTP '+r.status);render(await r.json())}catch(e){$('status').className='status';$('status').lastElementChild.textContent='状态获取失败';$('dataState').textContent='Dashboard API 暂时不可用，将自动重试。'}}refresh();setInterval(refresh,3000);
</script></body></html>"""


_STATIC_ROOT = Path(__file__).with_name("web") / "static"
_STATIC_TYPES = {
    "command-center.css": "text/css; charset=utf-8",
    "command-center.js": "application/javascript; charset=utf-8",
    "tactical-map.js": "application/javascript; charset=utf-8",
}


def dashboard_static_asset(path: str) -> tuple[bytes, str] | None:
    """Return only the two packaged dashboard assets; no directory traversal."""
    name = path.removeprefix("/static/")
    content_type = _STATIC_TYPES.get(name)
    if content_type is None:
        return None
    try:
        return (_STATIC_ROOT / name).read_bytes(), content_type
    except OSError:
        return None


DASHBOARD_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Arena Hero · 作战指挥中心</title><link rel="stylesheet" href="/static/command-center.css"></head><body><main>
<header><div><h1>ARENA HERO 作战指挥中心</h1><div class="muted">仅展示当前脱敏事实；写命令在下一次成功提交后生效</div></div><div id="status" class="status">正在获取状态</div></header>
<section class="grid"><div class="card metric"><div class="muted">最近 Tick</div><div id="tick" class="value">—</div></div><div class="card metric"><div class="muted">资源 / 容量</div><div id="resources" class="value">—</div></div><div class="card metric"><div class="muted">策略模式</div><div id="mode" class="value">—</div></div><div class="card metric"><div class="muted">命令语义</div><div class="value">Next Tick</div></div>
<div class="card wide"><h2>战术地图（当前可见）</h2><svg id="map" class="map" viewBox="0 0 400 260" role="img" aria-label="当前可见战术地图"></svg></div><div class="card side"><h2>紧急控制</h2><p class="muted">默认关闭写操作；仅限本机认证会话。</p><input id="password" type="password" autocomplete="current-password" placeholder="管理员口令"><button id="login" class="neutral">认证</button><p id="loginState" class="muted"></p><button id="stop" class="danger">紧急停机</button> <button id="resume" class="neutral">恢复自动</button></div>
<div class="card wide"><h2>Goal / 任务</h2><div id="goals" class="muted">尚无 trace</div></div>
<div class="card wide"><h2>实体与行为树路径</h2><div id="entities" class="muted">尚无实体 trace</div></div><div class="card side"><h2>下达任务</h2><input id="taskAlias" placeholder="entity_…"><select id="taskKind"><option>HOLD_POSITION</option><option>RETREAT_TO_CORE</option><option>HARVEST_VISIBLE</option><option>MOVE_TO_CELL</option></select><input id="taskTarget" placeholder="目标 x,y（仅移动）"><button id="assign" class="neutral">排队任务</button><p id="taskState" class="muted"></p></div>
<div class="card wide"><h2>任务、租约与阻塞</h2><div id="tasks" class="muted">尚无 scheduler trace</div></div><div class="card side"><h2>命令审计状态</h2><div id="commands" class="muted">尚无命令</div></div>
<div class="card wide"><h2>任务依赖、目标锁与租约时间线</h2><div id="timeline" class="muted">尚无 scheduler trace</div></div>
<div class="card wide"><h2>Core 迁移</h2><p class="muted">只会在下一权威 Tick 对当前正常 Core 选择一条安全相邻腿。</p><input id="migrationTarget" placeholder="目标 x,y"><button id="migrate" class="neutral">排队迁移</button> <button id="cancelMigration" class="neutral">取消迁移</button></div><div class="card side"><h2>策略姿态</h2><p class="muted">当前生效：<span id="policyCurrent">BALANCED</span></p><select id="policyPosture"><option>BALANCED</option><option>DEFENSIVE</option><option>ECONOMY</option><option>AGGRESSIVE</option></select><button id="setPolicy" class="neutral">排队策略</button><p id="policyState" class="muted">认证后可读取和更新。</p></div></section>
</main><script src="/static/command-center.js"></script><script src="/static/tactical-map.js"></script></body></html>"""
