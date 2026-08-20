#!/usr/bin/env python3
"""Arena Hero Nightly Tactical Aggregator and Email Dispatcher.

Handles:
1. Appending tactical snapshot during quiet hours (23:00 - 08:00).
2. Aggregating nightly reports and dispatching via agently-cli to email at 08:00.
"""

from __future__ import annotations

import datetime
import json
import os
import subprocess
import sys
from pathlib import Path

BASE_DIR = Path("/root/project/arena_hero")
NIGHTLY_LOG = BASE_DIR / "runtime" / "nightly_tactical_reports.jsonl"
DEFAULT_EMAIL = "709934831@qq.com"


def is_night_time() -> bool:
    """Check if current time is between 23:00 and 08:00."""
    now = datetime.datetime.now()
    hour = now.hour
    return hour >= 23 or hour < 8


def append_nightly_snapshot(snapshot: dict) -> None:
    NIGHTLY_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(NIGHTLY_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(snapshot, ensure_ascii=False) + "\n")


def aggregate_and_send_email(target_email: str = DEFAULT_EMAIL) -> tuple[bool, str]:
    if not NIGHTLY_LOG.exists():
        return False, "没有找到夜间战况记录文件。"

    records = []
    with open(NIGHTLY_LOG, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except Exception:
                    continue

    if not records:
        return False, "夜间战况记录为空。"

    total_ticks = len(records)
    first_tick = records[0].get("tick_start", records[0].get("last_tick", "N/A"))
    last_tick = records[-1].get("tick_end", records[-1].get("last_tick", "N/A"))
    
    # Analyze anomalies across the night
    all_anomalies = []
    oscillations_count = 0
    drought_count = 0
    damage_count = 0
    destroyed_count = 0

    for r in records:
        for finding in r.get("findings", []):
            code = finding.get("code")
            all_anomalies.append(finding)
            if code == "UNIT_OSCILLATION":
                oscillations_count += 1
            elif code == "RESOURCE_DROUGHT":
                drought_count += 1
            elif code == "CORE_DAMAGED":
                damage_count += 1
            elif code == "CORE_DESTROYED":
                destroyed_count += 1

    date_str = datetime.date.today().strftime("%Y-%m-%d")
    subject = f"【Arena Hero】夜间战况与战术健康整合晨报 ({date_str})"

    lines = [
        f"<h2>🛡️ Arena Hero 夜间战术巡检整合报告</h2>",
        f"<p><b>统计周期</b>：昨夜 23:00 ～ 今晨 08:00（共计采样 {total_ticks} 次巡检）</p>",
        f"<p><b>回合跨度</b>：Tick <code>{first_tick}</code> → <code>{last_tick}</code></p>",
        f"<hr>",
        f"<h3>📊 夜间关键战术指标统计</h3>",
        f"<ul>",
        f"<li><b>核心受损/摧毁事件</b>：受损 <code>{damage_count}</code> 次，摧毁/重生 <code>{destroyed_count}</code> 次</li>",
        f"<li><b>单位往复振荡/徘徊告警</b>：共触发 <code>{oscillations_count}</code> 次巡检周期</li>",
        f"<li><b>资源枯竭/空视野告警</b>：共触发 <code>{drought_count}</code> 次巡检周期</li>",
        f"</ul>",
        f"<hr>",
        f"<h3>🔍 战情细节与异常摘要</h3>",
    ]

    if damage_count > 0 or destroyed_count > 0:
        lines.append(f"<p style='color: red;'><b>🚨 严重事件</b>：夜间曾发生核心受袭或毁灭，请重点排查防御机制与回放！</p>")
    else:
        lines.append(f"<p style='color: green;'><b>✅ 防御稳固</b>：夜间核心未受到敌方攻击，整体生存状态良好。</p>")

    if all_anomalies:
        lines.append("<h4>主要战术线索：</h4><ul>")
        seen_messages = set()
        for a in all_anomalies:
            msg = a.get("message", "")
            if msg and msg not in seen_messages:
                seen_messages.add(msg)
                lines.append(f"<li>[{a.get('severity', 'INFO')}] {msg}</li>")
        lines.append("</ul>")

    lines.append("<hr><p style='color: #666;'><i>此邮件由 Hermes AI 战术参谋自动整合生成并发送。</i></p>")
    html_body = "\n".join(lines)

    # Dispatch via agently-cli
    cmd = [
        "agently-cli", "message", "+send",
        "--to", target_email,
        "--subject", subject,
        "--body", html_body,
        "--confirmed"
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if res.returncode == 0:
            # Backup & Clear the nightly log
            backup_file = BASE_DIR / "runtime" / f"nightly_reports_{date_str}.jsonl"
            NIGHTLY_LOG.rename(backup_file)
            return True, f"邮件已成功发送至 {target_email}，夜间记录已备份至 {backup_file.name}"
        else:
            return False, f"agently-cli 发送失败: {res.stderr or res.stdout}"
    except Exception as e:
        return False, f"发送异常: {e}"


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--dispatch-morning-email":
        target = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_EMAIL
        success, msg = aggregate_and_send_email(target)
        print(json.dumps({"success": success, "message": msg}, ensure_ascii=False))
    elif len(sys.argv) > 1 and sys.argv[1] == "--is-night":
        print("true" if is_night_time() else "false")
    else:
        print("Usage: python3 nightly_tactical_manager.py [--dispatch-morning-email [email] | --is-night]")
