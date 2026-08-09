from __future__ import annotations

import json
from pathlib import Path

from arena_tactic.dashboard import DashboardDataStore
from tactic import ServiceStatus, _http_response


def _response(path: str, status: ServiceStatus, replay: Path) -> tuple[int, str, bytes]:
    result = _http_response(path, status, DashboardDataStore(replay, cache_seconds=0))
    assert result is not None
    code, body, content_type = result
    return code, content_type, body


def test_health_endpoints_distinguish_process_liveness_from_sdk_connection(tmp_path: Path):
    status = ServiceStatus()
    replay = tmp_path / "missing.jsonl"
    live_code, _, live_body = _response("/livez", status, replay)
    health_code, _, health_body = _response("/healthz", status, replay)
    live_payload, health_payload = json.loads(live_body), json.loads(health_body)
    assert live_code == 200
    assert live_payload["running"] is True
    assert health_code == 503
    assert health_payload["connected"] is False

    status.update(connected=True, last_tick=123)
    health_code, _, health_body = _response("/healthz", status, replay)
    assert health_code == 200
    assert json.loads(health_body)["last_tick"] == 123


def test_root_serves_chinese_dashboard_and_json_status_remains_compatible(tmp_path: Path):
    status = ServiceStatus(connected=True, last_tick=42, accepted=7, rejected=2)
    replay = tmp_path / "missing.jsonl"
    code, content_type, body = _response("/", status, replay)
    assert code == 200
    assert content_type.startswith("text/html")
    assert "作战控制台" in body.decode()
    assert "setInterval(refresh,3000)" in body.decode()

    status_code, _, body = _response("/status", status, replay)
    payload = json.loads(body)
    assert status_code == 200
    assert payload["last_tick"] == 42
    assert payload["accepted"] == 7


def test_dashboard_api_tolerates_missing_and_truncated_replay(tmp_path: Path):
    replay = tmp_path / "replay.jsonl"
    status = ServiceStatus(connected=True, last_tick=9)
    code, _, body = _response("/api/dashboard", status, replay)
    empty = json.loads(body)
    assert code == 200
    assert empty["current"] is None
    assert empty["recent"] == []

    replay.write_text(
            json.dumps({
                "tick": 9,
                "mode": "ECONOMY",
                "state": {"resources": 8, "resource_capacity": 15, "population": 3},
                "intents": [{"action": "HARVEST"}],
                "events": [{"type": "HARVEST_SUCCEEDED", "reason": None}],
                "decision_ms": 12.5,
                "accepted": True,
            }) + "\n{\"tick\":10,",
            encoding="utf-8",
        )
    code, _, body = _response("/api/dashboard", status, replay)
    payload = json.loads(body)
    assert code == 200
    assert payload["current"]["tick"] == 9
    assert payload["current"]["mode_label"] == "发展经济"
    assert payload["current"]["actions"] == [{"type": "HARVEST", "label": "采集", "count": 1}]


def test_dashboard_api_does_not_expose_sensitive_or_unapproved_fields(tmp_path: Path):
    secret = "super-secret-token"
    replay = tmp_path / "replay.jsonl"
    replay.write_text(json.dumps({
        "tick": 3,
        "mode": "DEFEND",
        "api_key": secret,
        "Authorization": f"Bearer {secret}",
        "state": {"resources": 1, "resource_capacity": 10, "population": 1, "cookie": secret},
        "intents": [{"action": "WAIT", "target": "12345678-1234-1234-1234-123456789012"}],
        "events": [{"type": "WAITED", "actor": "12345678-1234-1234-1234-123456789012"}],
    }) + "\n", encoding="utf-8")
    status = ServiceStatus(last_error=f"Authorization: Bearer {secret}; object=12345678-1234-1234-9234-123456789012")
    _, _, body = _response("/api/dashboard", status, replay)
    text = body.decode()
    assert secret not in text
    assert "12345678-1234-1234-1234-123456789012" not in text
    assert "api_key" not in text.lower()
    assert "[已脱敏]" in text
    _, _, status_body = _response("/status", status, replay)
    assert secret not in status_body.decode()
    assert "12345678-1234-1234-9234-123456789012" not in status_body.decode()
