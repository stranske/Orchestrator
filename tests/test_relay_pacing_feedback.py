"""Relay decisions earn capacity consumption only through observed, matching lane exits."""

import json
from datetime import datetime, timezone

import pytest

import capabilities
import feedback
import rate_incidents
import value_chain_monitor as monitor

NOW = 1792476000


def _line(ts=NOW - 60, *, chosen="claude_code", fallback="no", lane="opener"):
    stamp = datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return (
        f"{stamp} source=codex lane={lane} candidates=claude_code,cursor,muse "
        f"chosen={chosen} weights=claude_code:1,cursor:1,muse:1 "
        f"states=codex:shed,claude_code:ok,cursor:ok,muse:ok fallback={fallback}\n"
    )


def _setup(tmp_path, monkeypatch):
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "brain.db")
    monkeypatch.setattr(rate_incidents, "HANDOFF", tmp_path)
    monkeypatch.setattr(rate_incidents, "INCIDENT_FILE", tmp_path / "incidents.ndjson")
    monkeypatch.setattr(rate_incidents, "LOCK_FILE", tmp_path / "incidents.lock")
    monkeypatch.setattr(rate_incidents, "SHED_DIR", tmp_path / "shed")
    log = tmp_path / "relay-pacing.log"
    monkeypatch.setenv("HANDOFF_PACING_LOG", str(log))
    return log


def _counts(now=NOW):
    brain = monitor._brain(feedback.DB_PATH, NOW - 120, now)
    assert not brain["errors"], brain
    return monitor.outcome_counts(
        feedback.RELAY_PACING_CAPABILITY,
        monitor.own_edges_of(brain["edges"], feedback.RELAY_PACING_CAPABILITY),
    )


@pytest.mark.parametrize(
    "agent,chosen", [("claude", "claude_code"), ("cursor", "cursor"), ("muse", "muse")]
)
@pytest.mark.parametrize(
    "exit_status,evidence,status",
    [
        (0, "all checks passed", "completed"),
        (2, "checkout failed", "failed"),
        (1, "HTTP 429 Too Many Requests", "rate-limited"),
    ],
)
def test_existing_exit_hook_credits_capacity_row(
    tmp_path, monkeypatch, agent, chosen, exit_status, evidence, status
):
    log = _setup(tmp_path, monkeypatch)
    log.write_text(_line(chosen=chosen))
    tail = tmp_path / "tail"
    tail.write_text(evidence)
    result = rate_incidents.record_lane_round(
        agent=agent,
        lane="opener",
        receiver_reason=None,
        exit_status=exit_status,
        output_file=tail,
        ts=NOW,
    )
    assert result["relay_pacing"] == {"status": "recorded", "decisions": 1, "joined": 1}
    counts = _counts()
    assert (counts["acted_on"], counts["outcome"], counts["outcome_success"]) == (1, 1, 0)
    with feedback._conn() as c:
        row = c.execute(
            "SELECT run_id,payload_json,validation_status FROM completion_events "
            "WHERE phase='outcome'"
        ).fetchone()
        assert row[0] == result["decision_id"]
        assert row[2] == "accepted"
        payload = json.loads(row[1])["relay_pacing"]
        assert (payload["chosen"], payload["status"]) == (chosen, status)
        assert c.execute("SELECT COUNT(*) FROM outcomes").fetchone()[0] == 0
    # Exercise the actual report consumed by switch_review as well as the shared admission index.
    ledger = tmp_path / "capabilities.json"
    capabilities.register(feedback.RELAY_PACING_CAPABILITY, {"status": "wired"}, ledger)
    rep = monitor.report(
        now=NOW, path=ledger, env={}, inputs=monitor._brain(feedback.DB_PATH, NOW - 120, NOW)
    )
    row = next(r for r in rep["rows"] if r["capability"] == feedback.RELAY_PACING_CAPABILITY)
    assert (row["acted_on"], row["outcome"]) == (1, 1)
    assert "acted_on 1" in "\n".join(monitor.format_lines(rep))


def test_fallback_and_reread_preserve_identity_and_window(tmp_path, monkeypatch):
    log = _setup(tmp_path, monkeypatch)
    log.write_text(_line(fallback="yes") * 2)
    feedback.record_lane_round("claude", "opener", None, 0, "none", NOW)
    for end in (NOW, NOW + 86400):
        assert feedback.ingest_relay_pacing(log, now=end)["joined"] == 1
    assert (_counts()["acted_on"], _counts()["outcome"]) == (1, 1)
    with feedback._conn() as c:
        assert c.execute("SELECT COUNT(*) FROM influence_edges").fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM completion_events").fetchone()[0] == 2
        assert c.execute("SELECT created_ts FROM influence_edges").fetchone()[0] == NOW - 60
        payload = json.loads(
            c.execute(
                "SELECT payload_json FROM completion_events " "WHERE phase='decision'"
            ).fetchone()[0]
        )["relay_pacing"]
        assert payload["fallback"] is True
        assert payload["source"] == "codex"
        assert payload["states"].startswith("codex:shed")
        assert payload["candidates"] == ["claude_code", "cursor", "muse"]
    index, _ = monitor.acted_on_window_index(
        feedback.DB_PATH, {feedback.RELAY_PACING_CAPABILITY: (NOW + 1, NOW + 86400)}
    )
    assert index[feedback.RELAY_PACING_CAPABILITY] == 0


@pytest.mark.parametrize(
    "kind", ["unfinished", "mismatch", "multiple", "tied", "future", "same_second"]
)
def test_uncertain_decisions_never_get_consumption_credit(tmp_path, monkeypatch, kind):
    log = _setup(tmp_path, monkeypatch)
    log.write_text(
        _line(NOW + 1 if kind == "future" else NOW if kind == "same_second" else NOW - 60)
    )
    if kind != "unfinished":
        feedback.record_lane_round(
            "codex" if kind == "mismatch" else "claude", "opener", None, 0, "none", NOW
        )
    if kind == "multiple":
        with log.open("a") as f:
            f.write(_line(NOW - 30))
    if kind == "tied":
        feedback.record_lane_round("claude", "opener", "second", 0, "none", NOW)
    feedback.ingest_relay_pacing(log, now=NOW)
    assert _counts()["acted_on"] == 0
    assert _counts()["outcome"] == 0


def test_late_exit_cannot_double_bind_decision(tmp_path, monkeypatch):
    log = _setup(tmp_path, monkeypatch)
    log.write_text(_line())
    original = feedback.record_lane_round("claude", "opener", None, 0, "none", NOW)
    feedback.ingest_relay_pacing(log, now=NOW)
    feedback.record_lane_round("claude", "opener", None, 0, "none", NOW - 10)
    feedback.ingest_relay_pacing(log, now=NOW)
    assert _counts()["acted_on"] == 1
    with feedback._conn() as c:
        assert c.execute("SELECT target_run_id FROM influence_edges").fetchone()[0] == original


def test_serial_rounds_and_lanes_do_not_cross_join(tmp_path, monkeypatch):
    log = _setup(tmp_path, monkeypatch)
    log.write_text(
        _line(NOW - 100)
        + _line(NOW - 50, chosen="muse")
        + _line(NOW - 90, chosen="cursor", lane="closer")
    )
    feedback.record_lane_round("claude", "opener", None, 0, "none", NOW - 80)
    feedback.record_lane_round("muse", "opener", None, 1, "auth_failed", NOW)
    feedback.record_lane_round("cursor", "closer", None, 0, "none", NOW - 70)
    assert feedback.ingest_relay_pacing(log, now=NOW)["joined"] == 3
    assert (_counts()["acted_on"], _counts()["outcome"]) == (3, 3)
    with feedback._conn() as c:
        assert (
            c.execute(
                "SELECT json_extract(payload_json,'$.relay_pacing.status') FROM completion_events "
                "WHERE phase='outcome' AND json_extract(payload_json,'$.relay_pacing.chosen')='muse'"
            ).fetchone()[0]
            == "failed"
        )


def test_malformed_and_missing_log_and_import_failure(tmp_path, monkeypatch):
    log = _setup(tmp_path, monkeypatch)
    assert feedback.ingest_relay_pacing(log, now=NOW)["status"] == "absent"
    log.write_text(
        "unrelated prose\n2026-10-20T05:00:00Z source=codex\n"
        + _line().replace("fallback=no", "fallback=maybe")
    )
    assert feedback.ingest_relay_pacing(log, now=NOW)["decisions"] == 0
    log.unlink()
    log.mkdir()  # A genuine IO error must stay visible while the incident is still written.
    tail = tmp_path / "tail"
    tail.write_text("HTTP 429 Too Many Requests")
    result = rate_incidents.record_lane_round(
        agent="claude", lane="opener", receiver_reason=None, exit_status=1, output_file=tail, ts=NOW
    )
    assert result["relay_pacing"]["status"] == "unavailable"
    assert result["incident_id"] is not None


def test_round_outcome_is_not_visible_before_its_end(tmp_path, monkeypatch):
    log = _setup(tmp_path, monkeypatch)
    log.write_text(_line())
    feedback.record_lane_round("claude", "opener", None, 0, "none", NOW)
    feedback.ingest_relay_pacing(log, now=NOW)
    before = _counts(now=NOW - 30)
    assert (before["acted_on"], before["outcome"]) == (1, 0)
    assert _counts()["outcome"] == 1


def test_saved_pending_decision_can_join_after_log_rotation(tmp_path, monkeypatch):
    log = _setup(tmp_path, monkeypatch)
    log.write_text(_line())
    assert feedback.ingest_relay_pacing(log, now=NOW)["joined"] == 0
    log.unlink()
    feedback.record_lane_round("claude", "opener", None, 0, "none", NOW)
    assert feedback.ingest_relay_pacing(log, now=NOW)["joined"] == 1
    assert (_counts()["acted_on"], _counts()["outcome"]) == (1, 1)


def test_settled_history_is_not_rewritten_on_reread(tmp_path, monkeypatch):
    log = _setup(tmp_path, monkeypatch)
    log.write_text(_line())
    feedback.record_lane_round("claude", "opener", None, 0, "none", NOW)
    feedback.ingest_relay_pacing(log, now=NOW)

    def unexpected_write(*args, **kwargs):
        pytest.fail("reread rewrote an already settled decision or outcome")

    monkeypatch.setattr(feedback, "_record_completion_event_in_conn", unexpected_write)
    assert feedback.ingest_relay_pacing(log, now=NOW + 60)["joined"] == 1
    assert _counts()["acted_on"] == 1


def test_incremental_join_keeps_preceding_exit_boundary(tmp_path, monkeypatch):
    log = _setup(tmp_path, monkeypatch)
    log.write_text(_line(NOW - 100))
    feedback.record_lane_round("claude", "opener", None, 0, "none", NOW - 80)
    assert feedback.ingest_relay_pacing(log, now=NOW)["joined"] == 1
    # A new pending decision does not have to share a file with the settled predecessor.
    log.write_text(_line(NOW - 50, chosen="muse"))
    assert feedback.ingest_relay_pacing(log, now=NOW)["joined"] == 1
    feedback.record_lane_round("muse", "opener", None, 0, "none", NOW)
    assert feedback.ingest_relay_pacing(log, now=NOW)["joined"] == 2
    assert (_counts()["acted_on"], _counts()["outcome"]) == (2, 2)
