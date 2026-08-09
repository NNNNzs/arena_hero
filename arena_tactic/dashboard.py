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
    }


class DashboardDataStore:
    """Small TTL cache around bounded replay-tail reads."""

    def __init__(
        self,
        replay_path: Path,
        *,
        max_bytes: int = 256 * 1024,
        recent_limit: int = 12,
        cache_seconds: float = 1.0,
    ) -> None:
        self.replay_path = replay_path
        self.max_bytes = max_bytes
        self.recent_limit = recent_limit
        self.cache_seconds = cache_seconds
        self._cached_at = 0.0
        self._cached_records: list[dict[str, Any]] = []
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
                self._cached_at = now
            return list(self._cached_records)

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
        }


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
