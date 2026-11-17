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


def _isolate_incident_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(rate_incidents, "HANDOFF", tmp_path)
    monkeypatch.setattr(rate_incidents, "INCIDENT_FILE", tmp_path / "incidents.ndjson")
    monkeypatch.setattr(rate_incidents, "LOCK_FILE", tmp_path / "incidents.lock")
    monkeypatch.setattr(rate_incidents, "SHED_DIR", tmp_path / "shed")
    monkeypatch.setattr(capacity, "SHED_DIR", rate_incidents.SHED_DIR)
    monkeypatch.setattr(capacity, "_auth_health", lambda agent: None)
    monkeypatch.setattr(capacity, "_model_health", lambda agent, tier="full": None)
    return rate_incidents.SHED_DIR / "codex"


def test_shed_cleanup_preserves_marker_renewed_during_read(tmp_path, monkeypatch):
    marker = _isolate_incident_paths(tmp_path, monkeypatch)
    now = int(time.time())
    renewed_until = now + 3600
    assert rate_incidents.ensure_shed(
        "codex", category="quota", incident_id="stale", expires_at=now - 1
    )

    original_shed_marker = capacity.shed_marker
    renewed = False

    def renew_before_lock(agent, *args, **kwargs):
        nonlocal renewed
        snapshot = original_shed_marker(agent, *args, **kwargs)
        if agent == "codex" and not renewed:
            renewed = True
            assert rate_incidents.ensure_shed(
                "codex",
                category="quota",
                incident_id="renewed",
                expires_at=renewed_until,
            )
        return snapshot

    monkeypatch.setattr(capacity, "shed_marker", renew_before_lock)

    assert (capacity._shed("codex"), marker.exists()) == (True, True)
    assert renewed is True
    saved = json.loads(marker.read_text())
    assert saved["incident_id"] == "renewed"
    assert saved["expires_at"] == renewed_until
    assert capacity._shed("claude") is False
    assert capacity.compute("claude", capacity.AGENTS["claude"], None)[0] == capacity.OK


def test_shed_cleanup_preserves_manual_marker_replacement(tmp_path, monkeypatch):
    marker = _isolate_incident_paths(tmp_path, monkeypatch)
    now = int(time.time())
    assert rate_incidents.ensure_shed(
        "codex", category="quota", incident_id="stale", expires_at=now - 1
    )

    original_shed_marker = capacity.shed_marker
    replaced = False

    def replace_with_manual(agent, *args, **kwargs):
        nonlocal replaced
        snapshot = original_shed_marker(agent, *args, **kwargs)
        if agent == "codex" and not replaced:
            replaced = True
            marker.write_text("", encoding="utf-8")
        return snapshot

    monkeypatch.setattr(capacity, "shed_marker", replace_with_manual)

    assert capacity._shed("codex") is True
    assert replaced is True
    assert marker.exists()
    assert original_shed_marker("codex")["state"] == "manual"


def test_shed_cleanup_revalidates_recovery_with_build_snapshot(tmp_path, monkeypatch):
    import pacing_usage

    marker = _isolate_incident_paths(tmp_path, monkeypatch)
    now = 1_750_000_000
    monkeypatch.setattr(capacity.time, "time", lambda: now)
    monkeypatch.setattr(rate_incidents.time, "time", lambda: now)
    monkeypatch.setattr(pacing_usage, "RESETS_FILE", tmp_path / "absent-resets.json")
    monkeypatch.setattr(pacing_usage, "CALIBRATION_FILE", tmp_path / "absent-calibration.json")
    monkeypatch.setattr(
        pacing_usage,
        "polled_usage",
        lambda *args, **kwargs: {"poll": {"fresh": False}},
    )

    recoverable = {
        "used_fraction": 0.2,
        "short_used_fraction": 0.2,
        "ts": now - 10,
        "short_ts": now - 10,
        "poll": {"fresh": False},
    }
    exhausted = {
        "used_fraction": 1.0,
        "short_used_fraction": 0.2,
        "ts": now - 5,
        "short_ts": now - 5,
    }
    monkeypatch.setattr(pacing_usage, "usage", lambda *args, **kwargs: exhausted)

    same_expiry = now + 3600
    rate_incidents.SHED_DIR.mkdir(parents=True, exist_ok=True)
    marker.write_text(
        json.dumps(
            {
                "created_at": now - 100,
                "expires_at": same_expiry,
                "category": "quota",
                "incident_id": "before-build-snapshot",
            }
        ),
        encoding="utf-8",
    )

    token = capacity._BUILD_PACING_USAGE.set({"codex": recoverable})
    try:
        assert capacity._shed("codex") is True
        assert marker.exists()
        saved = json.loads(marker.read_text())
        assert saved["incident_id"] == "before-build-snapshot"
    finally:
        capacity._BUILD_PACING_USAGE.reset(token)


def test_shed_cleanup_revalidates_sample_recovery_and_unchanged_expiry_replacement(
    tmp_path, monkeypatch
):
    import pacing_usage

    marker = _isolate_incident_paths(tmp_path, monkeypatch)
    now = 1_750_000_000
    monkeypatch.setattr(capacity.time, "time", lambda: now)
    monkeypatch.setattr(rate_incidents.time, "time", lambda: now)
    monkeypatch.setattr(pacing_usage, "RESETS_FILE", tmp_path / "absent-resets.json")
    monkeypatch.setattr(pacing_usage, "CALIBRATION_FILE", tmp_path / "absent-calibration.json")
    monkeypatch.setattr(
        pacing_usage,
        "usage",
        lambda *args, **kwargs: {
            "used_fraction": 0.2,
            "short_used_fraction": 0.2,
            "ts": now - 10,
            "short_ts": now - 10,
        },
    )

    # Replacement with the same expires_at ("unchanged expiry") and newer created_at/incident_id
    # must survive cleanup even though the initial snapshot qualified for sample recovery.
    rate_incidents.SHED_DIR.mkdir(parents=True, exist_ok=True)
    same_expiry = now + 3600
    marker.write_text(
        json.dumps(
            {
                "created_at": now - 100,
                "expires_at": same_expiry,
                "category": "quota",
                "incident_id": "before-sample",
            }
        ),
        encoding="utf-8",
    )
    original_shed_marker = capacity.shed_marker
    replaced = False

    def replace_with_same_expiry(agent, *args, **kwargs):
        nonlocal replaced
        snapshot = original_shed_marker(agent, *args, **kwargs)
        if agent == "codex" and not replaced:
            replaced = True
            marker.write_text(
                json.dumps(
                    {
                        "created_at": now,
                        "expires_at": same_expiry,
                        "category": "quota",
                        "incident_id": "after-sample-same-expiry",
                    }
                ),
                encoding="utf-8",
            )
        return snapshot

    monkeypatch.setattr(capacity, "shed_marker", replace_with_same_expiry)
    assert capacity._shed("codex") is True
    assert marker.exists()
    assert json.loads(marker.read_text())["incident_id"] == "after-sample-same-expiry"

    # When recovery evidence turns exhausted before the locked check, keep the marker.
    monkeypatch.setattr(capacity, "shed_marker", original_shed_marker)
    marker.write_text(
        json.dumps(
            {
                "created_at": now - 100,
                "expires_at": same_expiry,
                "category": "quota",
                "incident_id": "before-sample",
            }
        ),
        encoding="utf-8",
    )
    samples = iter(
        [
            {
                "used_fraction": 0.2,
                "short_used_fraction": 0.2,
                "ts": now - 10,
                "short_ts": now - 10,
            },
            {
                "used_fraction": 1.0,
                "short_used_fraction": 0.2,
                "ts": now - 5,
                "short_ts": now - 5,
            },
        ]
    )
    monkeypatch.setattr(pacing_usage, "usage", lambda *args, **kwargs: next(samples))
    assert capacity._shed("codex") is True
    assert marker.exists()

    # Positive control: when marker and valid sample recovery remain unchanged, cleanup unlinks it.
    monkeypatch.setattr(
        pacing_usage,
        "usage",
        lambda *args, **kwargs: {
            "used_fraction": 0.2,
            "short_used_fraction": 0.2,
            "ts": now - 10,
            "short_ts": now - 10,
        },
    )
    assert capacity._shed("codex") is False
    assert not marker.exists()


def test_shed_cleanup_lock_failure_and_unchanged_expiry_controls(tmp_path, monkeypatch):
    import fcntl
    import os

    import pytest

    marker = _isolate_incident_paths(tmp_path, monkeypatch)
    now = int(time.time())
    expired_at = now - 5
    assert rate_incidents.ensure_shed(
        "codex", category="quota", incident_id="expired-first", expires_at=expired_at
    )

    # Replacement with an unchanged expired timestamp but a new incident identity must not be
    # unlinked by cleanup of the earlier snapshot.
    original_shed_marker = capacity.shed_marker
    swapped = False

    def swap_same_expired_timestamp(agent, *args, **kwargs):
        nonlocal swapped
        snapshot = original_shed_marker(agent, *args, **kwargs)
        if agent == "codex" and not swapped:
            swapped = True
            marker.write_text(
                json.dumps(
                    {
                        "created_at": now,
                        "expires_at": expired_at,
                        "category": "quota",
                        "incident_id": "expired-second",
                    }
                ),
                encoding="utf-8",
            )
        return snapshot

    monkeypatch.setattr(capacity, "shed_marker", swap_same_expired_timestamp)
    assert capacity._shed("codex") is True
    assert marker.exists()
    assert json.loads(marker.read_text())["incident_id"] == "expired-second"
    monkeypatch.setattr(capacity, "shed_marker", original_shed_marker)

    # Lock-failure control: while the shared incident lock is held by a writer, nonblocking
    # acquisition fails, descriptors are cleaned up, the marker stays on disk, and _shed is True.
    competing_fd = os.open(rate_incidents.LOCK_FILE, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(competing_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        closed_fds = []
        original_close = os.close

        def track_close(fd):
            closed_fds.append(fd)
            return original_close(fd)

        monkeypatch.setattr(rate_incidents.os, "close", track_close)
        with pytest.raises(BlockingIOError):
            with rate_incidents.incident_lock(blocking=False):
                pass
        assert len(closed_fds) == 1
        assert capacity._shed("codex") is True
        assert len(closed_fds) == 2
        assert marker.exists()
    finally:
        fcntl.flock(competing_fd, fcntl.LOCK_UN)
        os.close(competing_fd)

    # Unchanged-expiry positive control: once the lock is available and the expired marker is
    # unchanged across the reread, cleanup deletes it and returns False.
    assert capacity._shed("codex") is False
    assert not marker.exists()
