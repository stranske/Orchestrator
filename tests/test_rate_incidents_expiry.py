"""Expiry behavior for provider-capacity shed markers."""

import json
import time

import capacity
import rate_incidents


def test_reset_at_sets_the_marker_expiry(tmp_path, monkeypatch):
    monkeypatch.setattr(rate_incidents, "HANDOFF", tmp_path)
    monkeypatch.setattr(rate_incidents, "INCIDENT_FILE", tmp_path / "incidents.ndjson")
    monkeypatch.setattr(rate_incidents, "LOCK_FILE", tmp_path / "incidents.lock")
    monkeypatch.setattr(rate_incidents, "SHED_DIR", tmp_path / "shed")
    monkeypatch.setattr(capacity, "SHED_DIR", rate_incidents.SHED_DIR)
    now = int(time.time())
    reset_at = now + 30 * 60 * 60

    rate_incidents.record_incident(
        agent="codex",
        surface="handoff-relay",
        category="rate_limit",
        run_id="round-1",
        timestamp=now,
        reset_at=reset_at,
    )

    marker = json.loads((rate_incidents.SHED_DIR / "codex").read_text())
    assert marker["expires_at"] == reset_at
    assert marker["reset_at"] == reset_at
    monkeypatch.setattr(capacity.time, "time", lambda: now + 7 * 60 * 60)
    assert capacity._shed("codex") is True


def test_ensure_shed_extends_but_never_shortens(tmp_path, monkeypatch):
    monkeypatch.setattr(rate_incidents, "HANDOFF", tmp_path)
    monkeypatch.setattr(rate_incidents, "LOCK_FILE", tmp_path / "incidents.lock")
    monkeypatch.setattr(rate_incidents, "SHED_DIR", tmp_path / "shed")
    now = int(time.time())
    marker = rate_incidents.SHED_DIR / "codex"

    assert rate_incidents.ensure_shed(
        "codex", category="quota", incident_id="first", expires_at=now + 6 * 60 * 60
    )
    assert rate_incidents.ensure_shed(
        "codex", category="quota", incident_id="second", expires_at=now + 30 * 60 * 60
    )
    assert json.loads(marker.read_text())["expires_at"] == now + 30 * 60 * 60
    assert rate_incidents.ensure_shed(
        "codex", category="quota", incident_id="third", expires_at=now + 60 * 60
    )
    assert json.loads(marker.read_text())["expires_at"] == now + 30 * 60 * 60
