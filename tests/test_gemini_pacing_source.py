"""Regression tests for Gemini's agy /usage poll as a pacing and capacity source (#1434)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

import capacity
import comparative
import pacing
import pacing_usage
import rate_incidents
import route_weights_export as export

NOW = 1_800_000_000


def _write_gemini_poll(
    poll_dir: Path,
    *,
    ts: float = NOW - 60,
    weekly_percent: float = 58,
    five_hour_percent: float = 15,
    resets_at: float = NOW + 3 * 86400,
    weekly_id: str = "gemini-weekly",
) -> Path:
    poll_dir.mkdir(parents=True, exist_ok=True)
    path = poll_dir / "gemini-usage.json"
    payload = {
        "ts": ts,
        "source": "agy:/usage",
        "weekly": {
            "id": weekly_id,
            "used_percent": weekly_percent,
            "resets_at": resets_at,
        },
        "five_hour": {
            "id": "gemini-5h",
            "used_percent": five_hour_percent,
            "resets_at": NOW + 3600,
        },
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _comparative_fixture(tmp_path: Path) -> tuple[Path, dict]:
    db = tmp_path / "brain.db"
    export._fixture_db(db)
    with sqlite3.connect(db) as conn:
        conn.execute("DELETE FROM route_weights")
        for task, qualities in (("implement", (0.9, 0.75)), ("review", (0.8, 0.85))):
            for agent, quality in zip(("codex", "gemini"), qualities):
                conn.execute(
                    "INSERT INTO route_weights VALUES (2,0,?,?,0,?,20,0.5,0,0,'',0,0)",
                    (task, agent, quality),
                )
        conn.execute(
            "CREATE TABLE runs (run_id TEXT, ts INTEGER, agent TEXT, task_type TEXT, source TEXT)"
        )
        conn.execute(
            "CREATE TABLE costs ("
            "run_id TEXT, tokens_in INTEGER, tokens_out INTEGER, cost_usd REAL, source TEXT"
            ")"
        )
        conn.executemany(
            "INSERT INTO runs VALUES (?,?,?,?,?)",
            [
                ("c1", NOW - 30, "codex", "implement", "orchestrator_local"),
                ("c2", NOW - 30, "codex", "review", "keepalive"),
                ("g1", NOW - 30, "gemini", "implement", "orchestrator_local"),
                ("g2", NOW - 30, "gemini", "review", "keepalive"),
            ],
        )
    snapshot = {
        "generated_at": NOW,
        "agents": {
            "codex": {
                "state": "ok",
                "pacing": {
                    "state": "ok",
                    "used_fraction": 0.5,
                    "allowed_fraction": 0.75,
                    "effective_reserve": 0.15,
                    "period_end": NOW + 3 * 86400,
                    "resets_available": 0,
                    "burn_rate_per_day": 0.1,
                    "ts": NOW,
                },
            },
            "gemini": {
                "state": "ok",
            },
        },
    }
    return db, snapshot


def test_fresh_gemini_poll_enters_the_comparative_solve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    poll_dir = tmp_path / "usage-poll"
    monkeypatch.setattr(pacing_usage, "POLL_DIR", poll_dir)
    _write_gemini_poll(poll_dir, ts=NOW - 60, weekly_percent=58, five_hour_percent=15)

    polled = pacing_usage.polled_usage("gemini", NOW)
    assert polled["poll"]["valid"] is True
    assert polled["poll"]["fresh"] is True
    assert polled["used_fraction"] == pytest.approx(0.58)
    assert polled["short_used_fraction"] == pytest.approx(0.15)
    assert polled["source"] == "agy:/usage"

    bad = pacing_usage.polled_usage(
        "gemini",
        NOW,
        data={
            "ts": NOW - 60,
            "weekly": {"id": "other-bucket", "used_percent": 58, "resets_at": NOW + 86400},
        },
    )
    assert bad["poll"]["valid"] is False
    assert bad["poll"]["fresh"] is False

    db, snapshot = _comparative_fixture(tmp_path)
    result = comparative.measure(db, snapshot, now=NOW)
    assert result["status"] == "optimal"
    assert "gemini" not in result["excluded"]
    assert result["lambda"]["gemini"] is not None
    assert isinstance(result["lambda"]["gemini"], float)

    # Historical poll rows in gemini-usage.ndjson feed short-window EWMA burn when present.
    resets_at = NOW + 3 * 86400
    ndjson_rows = [
        {
            "ts": NOW - 7200,
            "source": "agy:/usage",
            "weekly": {"id": "gemini-weekly", "used_percent": 50, "resets_at": resets_at},
            "five_hour": {"id": "gemini-5h", "used_percent": 10, "resets_at": NOW + 3600},
        },
        {
            "ts": NOW - 3600,
            "source": "agy:/usage",
            "weekly": {"id": "gemini-weekly", "used_percent": 54, "resets_at": resets_at},
            "five_hour": {"id": "gemini-5h", "used_percent": 12, "resets_at": NOW + 3600},
        },
    ]
    (poll_dir / "gemini-usage.ndjson").write_text(
        "\n".join(json.dumps(r) for r in ndjson_rows) + "\n", encoding="utf-8"
    )
    with_history = comparative.measure(db, snapshot, now=NOW)
    assert with_history["status"] == "optimal"
    assert "gemini" not in with_history["excluded"]
    assert isinstance(with_history["lambda"]["gemini"], float)


def test_stale_or_missing_poll_keeps_todays_exclusion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    poll_dir = tmp_path / "usage-poll"
    monkeypatch.setattr(pacing_usage, "POLL_DIR", poll_dir)
    db, snapshot = _comparative_fixture(tmp_path)

    missing_result = comparative.measure(db, snapshot, now=NOW)
    assert missing_result["status"] == "optimal"
    assert missing_result["excluded"] == ["gemini"]
    assert missing_result["lambda"]["gemini"] is None
    assert (
        "excluded: no pacing source (capacity model windowed_prepaid)"
        in missing_result["inputs"]["cost_source_by_agent"]["gemini"]["reason"]
    )

    _write_gemini_poll(
        poll_dir,
        ts=NOW - pacing.STALE_AFTER_SECONDS - 1,
        weekly_percent=58,
        five_hour_percent=15,
    )
    stale_polled = pacing_usage.polled_usage("gemini", NOW)
    assert stale_polled["poll"]["valid"] is True
    assert stale_polled["poll"]["fresh"] is False

    stale_result = comparative.measure(db, snapshot, now=NOW)
    assert stale_result["status"] == "optimal"
    assert stale_result["excluded"] == ["gemini"]
    assert stale_result["lambda"]["gemini"] is None
    assert (
        "excluded: no pacing source (capacity model windowed_prepaid)"
        in stale_result["inputs"]["cost_source_by_agent"]["gemini"]["reason"]
    )

    _write_gemini_poll(
        poll_dir,
        ts=NOW - 60,
        weekly_percent=58,
        five_hour_percent=15,
        resets_at=NOW - 1,
    )
    expired_polled = pacing_usage.polled_usage("gemini", NOW)
    assert expired_polled["poll"]["valid"] is True
    assert expired_polled["poll"]["fresh"] is False
    assert expired_polled["used_fraction"] is None
    assert expired_polled["poll"]["rolled_period_end"] == NOW - 1 + pacing.PERIOD_SECONDS

    expired_result = comparative.measure(db, snapshot, now=NOW)
    assert expired_result["status"] == "optimal"
    assert expired_result["excluded"] == ["gemini"]
    assert expired_result["lambda"]["gemini"] is None


def test_polled_usage_overrides_the_soft_estimate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    poll_dir = tmp_path / "usage-poll"
    ledger = tmp_path / "capacity-ledger.ndjson"
    shed_dir = tmp_path / "capacity-shed"
    shed_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(pacing_usage, "POLL_DIR", poll_dir)
    monkeypatch.setattr(capacity, "LEDGER", ledger)
    monkeypatch.setattr(capacity, "SHED_DIR", shed_dir)
    monkeypatch.setattr(rate_incidents, "HANDOFF", tmp_path)
    monkeypatch.setattr(rate_incidents, "INCIDENT_FILE", tmp_path / "rate-limit-incidents.ndjson")
    monkeypatch.setattr(rate_incidents, "LOCK_FILE", tmp_path / "rate-limit-incidents.ndjson.lock")
    monkeypatch.setattr(rate_incidents, "SHED_DIR", shed_dir)
    monkeypatch.setattr(capacity, "_auth_health", lambda agent: None)
    monkeypatch.setattr(capacity, "_unresolvable_tier", lambda agent: None)
    monkeypatch.setattr(
        capacity,
        "_model_health",
        lambda agent, tier="full": {"model": "gemini-4-argon-high", "resolvable": True},
    )
    monkeypatch.setattr(capacity.time, "time", lambda: float(NOW))

    # 176 implement starts * 4.0 units = 704 units (over the 280 weekly and 8 five-hour soft caps).
    rows = [
        {"ts": NOW - 60, "agent": "gemini", "count": 176, "task_type": "implement"},
    ]
    ledger.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    _write_gemini_poll(poll_dir, ts=NOW - 60, weekly_percent=58, five_hour_percent=15)
    state, reason, meta = capacity.compute("gemini", capacity.AGENTS["gemini"], None)
    assert state == capacity.OK, (state, reason, meta)
    assert meta["used_weekly"] == pytest.approx(704.0)
    assert "58% weekly" in reason

    # Same thresholds as Claude: >= 80% warns, >= 100% sheds.
    _write_gemini_poll(poll_dir, ts=NOW - 60, weekly_percent=85, five_hour_percent=15)
    warn_state, warn_reason, warn_meta = capacity.compute("gemini", capacity.AGENTS["gemini"], None)
    assert warn_state == capacity.WARN, (warn_state, warn_reason)
    assert warn_meta["policy"] == "weekly-polled-warn"

    _write_gemini_poll(poll_dir, ts=NOW - 60, weekly_percent=58, five_hour_percent=85)
    window_warn_state, _, window_warn_meta = capacity.compute(
        "gemini", capacity.AGENTS["gemini"], None
    )
    assert window_warn_state == capacity.WARN
    assert window_warn_meta["policy"] == "window-polled-warn"

    _write_gemini_poll(poll_dir, ts=NOW - 60, weekly_percent=58, five_hour_percent=100)
    shed_state, shed_reason, shed_meta = capacity.compute("gemini", capacity.AGENTS["gemini"], None)
    assert shed_state == capacity.SHED, (shed_state, shed_reason)
    assert shed_meta["policy"] == "window-polled-cap"

    _write_gemini_poll(poll_dir, ts=NOW - 60, weekly_percent=100, five_hour_percent=15)
    wk_shed_state, _, wk_shed_meta = capacity.compute("gemini", capacity.AGENTS["gemini"], None)
    assert wk_shed_state == capacity.SHED
    assert wk_shed_meta["policy"] == "weekly-polled-cap"

    # Stale poll falls back to the soft unit estimate (704 / 280 units -> warn).
    _write_gemini_poll(
        poll_dir,
        ts=NOW - pacing.STALE_AFTER_SECONDS - 1,
        weekly_percent=58,
        five_hour_percent=15,
    )
    fallback_state, fallback_reason, fallback_meta = capacity.compute(
        "gemini", capacity.AGENTS["gemini"], None
    )
    assert fallback_state == capacity.WARN, (fallback_state, fallback_reason, fallback_meta)
    assert fallback_meta["policy"] == "weekly-soft-cap"
    assert "704.0/280" in fallback_reason

    # A real 429 shed marker still sheds the seat even when the poll is fresh at 58%.
    _write_gemini_poll(poll_dir, ts=NOW - 60, weekly_percent=58, five_hour_percent=15)
    (shed_dir / "gemini").write_text(
        json.dumps(
            {
                "created_at": NOW - 30,
                "expires_at": NOW + 3600,
                "incident_id": "inc-429",
                "category": "quota",
            }
        ),
        encoding="utf-8",
    )
    incident_state, _, _ = capacity.compute("gemini", capacity.AGENTS["gemini"], None)
    assert incident_state == capacity.SHED
