"""Relay decisions earn capacity consumption only through observed, matching lane exits."""

import json
import re
from datetime import datetime, timezone

import pytest

import capabilities
import feedback
import rate_incidents
import value_chain_monitor as monitor

NOW = 1792476000
_DECISION_ID_RE = re.compile(r"^\d{8}T\d{6}Z-\d+-[0-9a-f]+$")


def _line(ts=NOW - 60, *, chosen="claude_code", fallback="no", lane="opener", decision_id=None):
    stamp = datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return (
        f"{stamp} "
        + (f"decision_id={decision_id} " if decision_id is not None else "")
        + f"source=codex lane={lane} candidates=claude_code,cursor,muse "
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
        (75, "lane already busy", "failed"),
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
    assert result["relay_pacing"] == {
        "status": "recorded",
        "decisions": 1,
        "joined": 1,
        "unjoined": 0,
    }
    counts = _counts()
    assert (counts["acted_on"], counts["outcome"], counts["outcome_success"]) == (
        1,
        1,
        int(exit_status == 0),
    )
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
    assert row["usable"] == 1
    assert row["outcome_success"] == int(exit_status == 0)
    inputs = monitor.collect_inputs(
        now=NOW, gh_fn=lambda _: (True, "{}", ""), repos=[], db=feedback.DB_PATH
    )
    measured = monitor.report(now=NOW, path=ledger, env={}, inputs=inputs)["rows"][0]
    assert measured["situation_count"] == 1
    assert measured["usable"] >= measured["acted_on"]
    assert measured["first_break"] != "unusable"
    assert measured["success_scope"] == (
        "completed lane rounds (exit=0) and durable-delivery successes"
    )
    assert measured["relay_acted_on"] == 1
    assert measured["relay_pass"]["status"] == "pending"
    text = "\n".join(monitor.format_lines({"rows": [measured]}))
    assert "completed lane rounds (exit=0)" in text
    assert "relay pass (2026-10-27): pending" in text
    assert "acted_on 1" in "\n".join(monitor.format_lines(rep))


def test_relay_pass_number_grades_joins_against_demand_within_tolerance():
    """2026-10-27 criterion: relay joins cover log demand ±5%; durable edges stay aside."""
    check = monitor.RELAY_PASS_CHECK_TS
    base = {
        "situation_count": 20,
        "relay_acted_on": 19,
        "usable": 20,
        "outcome": 19,
        "outcome_success": 12,
    }
    pending = monitor.relay_pass_number(base, now=check - 1)
    assert pending["status"] == "pending"
    assert pending["acted_on_floor"] is None

    passed = monitor.relay_pass_number(base, now=check)
    assert passed["status"] == "pass"
    assert passed["acted_on_floor"] == 19  # Exactly 95% of 20 decisions.

    # Whole-round credit must reach 95%, including populations below 20 where
    # rounding down would allow a false pass (even zero joins for one decision).
    for demand, required in ((1, 1), (7, 7), (19, 19), (21, 20), (39, 38), (40, 38)):
        row = {**base, "situation_count": demand, "usable": demand, "outcome": demand}
        below = monitor.relay_pass_number({**row, "relay_acted_on": required - 1}, now=check)
        assert below["status"] == "fail", (demand, below)
        assert below["acted_on_floor"] == required
        enough = monitor.relay_pass_number({**row, "relay_acted_on": required}, now=check)
        assert enough["status"] == "pass", (demand, enough)
        assert enough["acted_on_floor"] == required

    short = monitor.relay_pass_number({**base, "relay_acted_on": 18}, now=check)
    assert short["status"] == "fail"
    assert "relay_acted_on 18 < floor 19" in short["reason"]

    unusable = monitor.relay_pass_number({**base, "usable": 10}, now=check)
    assert unusable["status"] == "fail"
    assert "usable 10 < relay_acted_on 19" in unusable["reason"]

    unread = monitor.relay_pass_number(
        {"situation_count": None, "relay_acted_on": 1, "usable": 1, "outcome": 1},
        now=check,
    )
    assert unread["status"] == "unmeasured"


def test_mixed_relay_and_durable_edges_share_combined_success_scope(tmp_path, monkeypatch):
    """Exit-75 relay + durable delivery edge → success 1; scope names both contributors.

    The capacity row's outcome_success sums operational exit=0 rounds and durable-delivery
    edges. A label that claims exit=0 alone would mis-grade the 2026-10-27 weekly row.
    """
    import time

    log = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(time, "time", lambda: float(NOW))
    log.write_text(_line(chosen="claude_code"))
    feedback.record_lane_round("claude", "opener", None, 75, "lane_busy", NOW)
    assert feedback.ingest_relay_pacing(log, now=NOW)["joined"] == 1
    assert (_counts()["acted_on"], _counts()["outcome"], _counts()["outcome_success"]) == (
        1,
        1,
        0,
    )

    run = "remote:o/r#812:gemini"
    feedback.record_run(run, "o/r#812", "implement", "gemini", mode="remote", pr_number=812)
    feedback.record_capability_consumption(
        capability_id=feedback.RELAY_PACING_CAPABILITY,
        capability_version_id="cv:capacity-delivery",
        source_run_id="capability-producer:windowed-capacity-policy:812",
        target_run_id=run,
        accepted=True,
        receipt_ref="fixture:mixed-edge",
    )
    feedback.record_outcome(run, adjudicated_verdict="PASS", merged=1, durability="durable")
    assert (_counts()["acted_on"], _counts()["outcome"], _counts()["outcome_success"]) == (
        2,
        2,
        1,
    )

    ledger = tmp_path / "capabilities.json"
    capabilities.register(feedback.RELAY_PACING_CAPABILITY, {"status": "wired"}, ledger)
    row = next(
        r
        for r in monitor.report(
            now=NOW,
            path=ledger,
            env={},
            inputs=monitor._brain(feedback.DB_PATH, NOW - 120, NOW),
        )["rows"]
        if r["capability"] == feedback.RELAY_PACING_CAPABILITY
    )
    assert row["outcome_success"] == 1
    assert row["acted_on"] == 2
    # Weekly ±5% pass uses version-exact joins only — durable edges must not inflate it.
    assert row["relay_acted_on"] == 1
    assert row["relay_pass"]["status"] == "pending"
    assert row["success_scope"] == ("completed lane rounds (exit=0) and durable-delivery successes")
    text = "\n".join(monitor.format_lines({"rows": [row]}))
    assert "durable-delivery successes" in text
    assert "relay_acted_on 1" in text

    # An exit=0 relay round adds a second success beside the durable edge.
    log.write_text(log.read_text() + _line(ts=NOW - 30, chosen="muse", decision_id="ok-round"))
    feedback.record_lane_round("muse", "opener", None, 0, "none", NOW, relay_decision_id="ok-round")
    assert feedback.ingest_relay_pacing(log, now=NOW)["joined"] == 2
    assert _counts()["outcome_success"] == 2
    after = next(
        r
        for r in monitor.report(
            now=NOW,
            path=ledger,
            env={},
            inputs=monitor._brain(feedback.DB_PATH, NOW - 120, NOW),
        )["rows"]
        if r["capability"] == feedback.RELAY_PACING_CAPABILITY
    )
    assert after["acted_on"] == 3
    assert after["relay_acted_on"] == 2


def test_relay_decision_acted_on_fields_and_single_owner(tmp_path, monkeypatch):
    """#812 recording contract: one owner row, joined fields, fallback flag, no double-count."""
    # Ownership is the existing capacity row — never a second relay-pacing capability.
    assert feedback.RELAY_PACING_CAPABILITY == "windowed-capacity-policy"
    assert feedback.RELAY_PACING_VERSION == "relay-pacing-v1"
    log = _setup(tmp_path, monkeypatch)
    log.write_text(_line(chosen="muse", fallback="yes"))
    feedback.record_lane_round("muse", "opener", None, 0, "none", NOW)
    assert feedback.ingest_relay_pacing(log, now=NOW)["joined"] == 1
    assert (_counts()["acted_on"], _counts()["outcome"]) == (1, 1)
    with feedback._conn() as c:
        decision = json.loads(
            c.execute(
                "SELECT payload_json FROM completion_events WHERE phase='decision'"
            ).fetchone()[0]
        )["relay_pacing"]
        outcome = json.loads(
            c.execute(
                "SELECT payload_json FROM completion_events WHERE phase='outcome'"
            ).fetchone()[0]
        )["relay_pacing"]
        edge = c.execute(
            "SELECT capability_id, capability_version_id, accepted FROM influence_edges"
        ).fetchone()
        assert decision == {
            "source": "codex",
            "lane": "opener",
            "candidates": ["claude_code", "cursor", "muse"],
            "chosen": "muse",
            "weights": "claude_code:1,cursor:1,muse:1",
            "states": "codex:shed,claude_code:ok,cursor:ok,muse:ok",
            "fallback": True,
        }
        assert outcome["status"] == "completed"
        assert outcome["chosen"] == "muse"
        assert outcome["fallback"] is True
        assert edge == (feedback.RELAY_PACING_CAPABILITY, feedback.RELAY_PACING_VERSION, 1)
        assert c.execute("SELECT COUNT(*) FROM influence_edges").fetchone()[0] == 1
    # Re-read must not invent a second acted_on edge for the same decision line.
    assert feedback.ingest_relay_pacing(log, now=NOW)["joined"] == 1
    assert (_counts()["acted_on"], _counts()["outcome"]) == (1, 1)


def test_fallback_and_reread_preserve_identity_and_window(tmp_path, monkeypatch):
    log = _setup(tmp_path, monkeypatch)
    log.write_text(_line(fallback="yes") * 2)
    feedback.record_lane_round("claude", "opener", None, 0, "none", NOW)
    for end in (NOW, NOW + 86400):
        assert feedback.ingest_relay_pacing(log, now=end)["joined"] == 1
    assert (_counts()["acted_on"], _counts()["outcome"]) == (1, 1)
    assert monitor._relay_decision_population(log, NOW - 120, NOW) == 1
    log.write_text(log.read_text() + _line(ts=NOW - 121) + _line(ts=NOW + 1))
    assert monitor._relay_decision_population(log, NOW - 120, NOW) == 1
    # The log is the independent denominator: an unjoined eligible decision still counts.
    log.write_text(log.read_text() + _line(ts=NOW, chosen="cursor", decision_id="not-joined"))
    assert monitor._relay_decision_population(log, NOW - 120, NOW) == 2
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
        assert payload["chosen"] == "claude_code"
        assert payload["weights"] == "claude_code:1,cursor:1,muse:1"
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
    assert monitor._relay_decision_population(log, NOW - 120, NOW) is None
    log.write_text(
        "unrelated prose\n2026-10-20T05:00:00Z source=codex\n"
        + _line().replace("fallback=no", "fallback=maybe")
    )
    assert feedback.ingest_relay_pacing(log, now=NOW)["decisions"] == 0
    assert monitor._relay_decision_population(log, NOW - 120, NOW) == 0
    log.unlink()
    log.mkdir()  # A genuine IO error must stay visible while the incident is still written.
    tail = tmp_path / "tail"
    tail.write_text("HTTP 429 Too Many Requests")
    result = rate_incidents.record_lane_round(
        agent="claude", lane="opener", receiver_reason=None, exit_status=1, output_file=tail, ts=NOW
    )
    assert result["relay_pacing"]["status"] == "unavailable"
    assert monitor._relay_decision_population(log, NOW - 120, NOW) is None
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


def test_generate_relay_decision_id_matches_documented_shape():
    when = datetime(2026, 10, 20, 13, 22, 56, tzinfo=timezone.utc)
    token = rate_incidents.generate_relay_decision_id(when=when, pid=4242, random_hex="a1b2c3d4")
    assert token == "20261020T132256Z-4242-a1b2c3d4"
    assert _DECISION_ID_RE.fullmatch(token)
    assert _DECISION_ID_RE.fullmatch(rate_incidents.generate_relay_decision_id())
    assert len(token) <= feedback.RELAY_DECISION_ID_MAX_CHARS
    with pytest.raises(ValueError, match="random_hex"):
        rate_incidents.generate_relay_decision_id(random_hex="has space")


def test_cli_generate_relay_decision_id_prints_one_token(capsys):
    assert rate_incidents.main(["generate-relay-decision-id"]) == 0
    token = capsys.readouterr().out.strip()
    assert _DECISION_ID_RE.fullmatch(token)
    assert len(token) <= feedback.RELAY_DECISION_ID_MAX_CHARS


def test_cursor_top_log_fields_feed_monitor_usable_and_acted_on(tmp_path, monkeypatch, capsys):
    """#812 post-merge path: unmeasured-preferred fields → log → join → usable/acted_on > 0.

    Nested pacing is absent on Cursor; formatting must still emit numeric weights so the
    weekly pass can see consumption after mirror sync (owner 2026-10-23).
    """
    log = _setup(tmp_path, monkeypatch)
    decision = {
        "policy": "unmeasured-preferred",
        "candidates": [
            {
                "agent": "cursor",
                "state": "ok",
                "routing_weight": 1.0,
                "pacing": None,
                "reason": "subscription ok",
            },
            {
                "agent": "codex",
                "state": "warn",
                "routing_weight": 0.2,
                "pacing": {"state": "warn", "routing_weight": 0.2},
            },
        ],
    }
    fields = rate_incidents.relay_log_fields_from_decision(decision, candidates="codex,cursor")
    assert fields["weights"] == "codex:0.2,cursor:1"
    assert "unknown" not in fields["weights"]
    # CLI path used by the host relay after pick-agent.
    import io

    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(decision)))
    assert rate_incidents.main(["format-relay-pacing-fields", "--candidates", "codex,cursor"]) == 0
    printed = json.loads(capsys.readouterr().out.strip())
    assert printed["weights"] == fields["weights"]
    assert printed["states"] == fields["states"]

    when = datetime.fromtimestamp(NOW - 30, timezone.utc)
    token = rate_incidents.append_relay_pacing_decision(
        source="codex",
        lane="opener",
        candidates="codex,cursor",
        chosen="cursor",
        weights=fields["weights"],
        states=fields["states"],
        fallback="no",
        when=when,
        log=log,
    )
    line = log.read_text(encoding="utf-8")
    assert "weights=codex:0.2,cursor:1" in line
    assert "unknown" not in line
    tail = tmp_path / "tail"
    tail.write_text("all checks passed")
    result = rate_incidents.record_lane_round(
        agent="cursor",
        lane="opener",
        receiver_reason=None,
        exit_status=0,
        output_file=tail,
        ts=NOW,
        relay_decision_id=token,
    )
    assert result["relay_pacing"]["joined"] == 1
    assert (_counts()["acted_on"], _counts()["outcome"], _counts()["outcome_success"]) == (1, 1, 1)
    ledger = tmp_path / "capabilities.json"
    capabilities.register(feedback.RELAY_PACING_CAPABILITY, {"status": "wired"}, ledger)
    inputs = monitor.collect_inputs(
        now=NOW, gh_fn=lambda _: (True, "{}", ""), repos=[], db=feedback.DB_PATH
    )
    row = monitor.report(now=NOW, path=ledger, env={}, inputs=inputs)["rows"][0]
    assert row["situation_count"] == 1
    assert row["usable"] >= 1
    assert row["acted_on"] >= 1
    assert row["relay_acted_on"] == 1
    assert row["relay_pass"]["status"] == "pending"


def test_append_relay_pacing_decision_writes_joinable_line(tmp_path, monkeypatch, capsys):
    log = _setup(tmp_path, monkeypatch)
    when = datetime.fromtimestamp(NOW - 30, timezone.utc)
    token = rate_incidents.append_relay_pacing_decision(
        source="codex",
        lane="opener",
        candidates="claude_code,cursor,muse",
        chosen="claude_code",
        weights="claude_code:1,cursor:1,muse:1",
        states="codex:shed,claude_code:ok,cursor:ok,muse:ok",
        fallback="no",
        when=when,
        log=log,
    )
    assert _DECISION_ID_RE.fullmatch(token)
    assert f"decision_id={token}" in log.read_text(encoding="utf-8")
    tail = tmp_path / "tail"
    tail.write_text("all checks passed")
    assert (
        rate_incidents.main(
            [
                "append-relay-pacing-decision",
                "--source",
                "codex",
                "--lane",
                "closer",
                "--candidates",
                "claude_code,cursor",
                "--chosen",
                "cursor",
                "--weights",
                "claude_code:1,cursor:1",
                "--states",
                "codex:ok,claude_code:ok,cursor:ok",
                "--fallback",
                "yes",
                "--log",
                str(log),
                "--ts",
                str(NOW - 20),
            ]
        )
        == 0
    )
    closer_id = capsys.readouterr().out.strip()
    assert _DECISION_ID_RE.fullmatch(closer_id)
    result = rate_incidents.record_lane_round(
        agent="claude",
        lane="opener",
        receiver_reason=None,
        exit_status=0,
        output_file=tail,
        ts=NOW,
        relay_decision_id=token,
    )
    assert result["relay_pacing"]["joined"] == 1
    assert (_counts()["acted_on"], _counts()["outcome"]) == (1, 1)
    with pytest.raises(ValueError, match="candidates"):
        rate_incidents.append_relay_pacing_decision(
            source="codex",
            lane="opener",
            candidates="claude_code",
            chosen="cursor",
            weights="claude_code:1",
            states="codex:ok",
            fallback="no",
            log=log,
        )


@pytest.fixture
def pacing_decision_args():
    return {
        "source": "codex",
        "lane": "opener",
        "candidates": "claude_code,cursor,muse",
        "chosen": "claude_code",
        "weights": "claude_code:1,cursor:1,muse:1",
        "states": "codex:shed,claude_code:ok,cursor:ok,muse:ok",
        "fallback": "no",
    }


@pytest.mark.parametrize(
    "field,value,message",
    [
        ("source", "unsupported", "source must be a supported relay agent"),
        ("lane", "invalid", "lane must be opener or closer"),
        ("fallback", "maybe", "fallback must be yes or no"),
        ("candidates", "", "candidates must include chosen"),
        ("candidates", "claude_code,unsupported", "candidates must include chosen"),
        ("chosen", "codex", "candidates must include chosen"),
        ("weights", "", "weights must be a nonempty no-whitespace token"),
        ("weights", "claude_code:1 cursor:1", "weights must be a nonempty no-whitespace token"),
        ("states", "", "states must be a nonempty no-whitespace token"),
        ("states", "codex:shed\ncursor:ok", "states must be a nonempty no-whitespace token"),
        ("decision_id", "", "decision_id must be a nonempty token"),
        ("decision_id", "has space", "decision_id must be a nonempty token"),
        ("decision_id", "has\nnewline", "decision_id must be a nonempty token"),
        (
            "decision_id",
            "x" * (feedback.RELAY_DECISION_ID_MAX_CHARS + 1),
            "decision_id must be a nonempty token",
        ),
    ],
)
def test_append_rejects_invalid_metadata_before_writing(
    tmp_path, pacing_decision_args, field, value, message
):
    log = tmp_path / "pacing" / "relay-pacing.log"
    with pytest.raises(ValueError, match=message):
        rate_incidents.append_relay_pacing_decision(
            **{**pacing_decision_args, field: value}, log=log
        )
    assert not log.parent.exists()


@pytest.mark.parametrize(
    "field,value,message",
    [
        ("source", "unsupported", "source must be a supported relay agent"),
        ("decision_id", "", "decision_id must be a nonempty token"),
        ("decision_id", "has space", "decision_id must be a nonempty token"),
        ("lane", "invalid", "invalid choice"),
        ("fallback", "maybe", "invalid choice"),
    ],
)
def test_cli_append_invalid_input_reports_error_without_writing(
    tmp_path, pacing_decision_args, capsys, field, value, message
):
    log = tmp_path / "pacing" / "relay-pacing.log"
    args = ["append-relay-pacing-decision", "--log", str(log)]
    for name, argument in {**pacing_decision_args, field: value}.items():
        args.extend(["--" + name.replace("_", "-"), argument])
    with pytest.raises(SystemExit) as exc:
        rate_incidents.main(args)
    assert exc.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert message in captured.err
    assert not log.parent.exists()


@pytest.mark.parametrize("explicit", [True, False])
def test_cli_id_selects_one_interleaved_decision(tmp_path, monkeypatch, capsys, explicit):
    log = _setup(tmp_path, monkeypatch)
    opener_one = rate_incidents.generate_relay_decision_id(
        when=datetime.fromtimestamp(NOW - 90, timezone.utc), pid=1, random_hex="aa01"
    )
    closer_one = rate_incidents.generate_relay_decision_id(
        when=datetime.fromtimestamp(NOW - 80, timezone.utc), pid=2, random_hex="bb02"
    )
    opener_two = rate_incidents.generate_relay_decision_id(
        when=datetime.fromtimestamp(NOW - 70, timezone.utc), pid=3, random_hex="cc03"
    )
    log.write_text(
        _line(NOW - 90, decision_id=opener_one if explicit else None)
        + _line(NOW - 80, chosen="cursor", lane="closer", decision_id=closer_one)
        + (_line(NOW - 70, decision_id=opener_two) if explicit else "")
    )
    tail = tmp_path / "tail"
    tail.write_text("finished")
    argv = [
        "record-lane-round",
        "--agent",
        "claude",
        "--surface",
        "handoff-relay",
        "--lane",
        "opener",
        "--exit",
        "0",
        "--output-file",
        str(tail),
        "--ts",
        str(NOW),
    ]
    if explicit:
        argv += ["--relay-decision-id", opener_two]
    assert rate_incidents.main(argv) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["relay_pacing"]["joined"] == 1
    assert (_counts()["acted_on"], _counts()["outcome"]) == (1, 1)
    with feedback._conn() as c:
        outcome = json.loads(
            c.execute(
                "SELECT payload_json FROM completion_events WHERE phase='outcome'"
            ).fetchone()[0]
        )["relay_pacing"]
        assert outcome.get("decision_id") == (opener_two if explicit else None)
    if explicit:
        # Same-second exits are distinct with IDs; no timing inference is needed.
        first = feedback.record_lane_round(
            "claude", "opener", None, 0, "none", NOW, relay_decision_id=opener_one
        )
        assert first != result["decision_id"]
        feedback.record_lane_round(
            "cursor", "closer", None, 0, "none", NOW, relay_decision_id=closer_one
        )
        assert feedback.ingest_relay_pacing(log, now=NOW)["joined"] == 3
    # Retrying the CLI does not create a second round or rewrite settled evidence.
    assert rate_incidents.main(argv) == 0
    capsys.readouterr()
    log.unlink()
    assert feedback.ingest_relay_pacing(log, now=NOW + 86400)["joined"] == (3 if explicit else 1)
    with feedback._conn() as c:
        assert c.execute("SELECT COUNT(*) FROM influence_edges").fetchone()[0] == (
            3 if explicit else 1
        )


@pytest.mark.parametrize("kind", ["unknown", "no_round_id", "lane", "agent", "future", "duplicate"])
def test_explicit_id_never_falls_back_or_guesses(tmp_path, monkeypatch, kind):
    log = _setup(tmp_path, monkeypatch)
    log.write_text(_line(decision_id="one"))
    feedback.record_lane_round(
        "cursor" if kind == "agent" else "claude",
        "closer" if kind == "lane" else "opener",
        None,
        0,
        "none",
        NOW - 70 if kind == "future" else NOW,
        relay_decision_id=(
            None if kind == "no_round_id" else "other" if kind == "unknown" else "one"
        ),
    )
    if kind == "duplicate":
        feedback.record_lane_round(
            "claude", "opener", None, 0, "none", NOW - 1, relay_decision_id="one"
        )
    assert feedback.ingest_relay_pacing(log, now=NOW)["joined"] == 0
    assert _counts()["acted_on"] == 0


def test_legacy_decision_cannot_bind_explicit_round(tmp_path, monkeypatch):
    log = _setup(tmp_path, monkeypatch)
    log.write_text(_line())
    feedback.record_lane_round(
        "claude", "opener", None, 0, "none", NOW, relay_decision_id="missing-from-log"
    )
    assert feedback.ingest_relay_pacing(log, now=NOW)["joined"] == 0


def test_pre_fix_orphans_retire_once_without_credit_or_rescan(tmp_path, monkeypatch):
    log = _setup(tmp_path, monkeypatch)
    cutoff = feedback.RELAY_PACING_BACKLOG_CUTOFF
    log.write_text(
        _line(cutoff - 120)
        + _line(cutoff - 60)
        + _line(cutoff + 1)
        + _line(cutoff + 2, decision_id="unfinished")
        + _line(cutoff + 40)
    )
    assert feedback.ingest_relay_pacing(log, now=cutoff - 1)["unjoined"] == 0
    feedback.record_lane_round("claude", "opener", None, 0, "none", cutoff + 30)
    result = feedback.ingest_relay_pacing(log, now=cutoff + 50)
    assert result == {"status": "recorded", "decisions": 5, "joined": 1, "unjoined": 2}
    with feedback._conn() as c:
        rows = c.execute(
            "SELECT status,payload_json,updated_ts FROM completion_events ORDER BY event_id"
        ).fetchall()
        retired = [json.loads(row[1])["relay_pacing"] for row in rows if row[0] == "skipped"]
        assert len(retired) == 2
        assert all(f["status"] == "unjoined" for f in retired)
        assert all(f["unjoined_reason"] == feedback.RELAY_PACING_UNJOINED_REASON for f in retired)
        assert c.execute("SELECT COUNT(*) FROM influence_edges").fetchone()[0] == 1
        assert (
            c.execute("SELECT COUNT(*) FROM completion_events WHERE phase='outcome'").fetchone()[0]
            == 1
        )

    original_conn = feedback._conn
    scans = []

    def traced_conn():
        c = original_conn()
        c.set_trace_callback(scans.append)
        return c

    def unexpected_write(*args, **kwargs):
        pytest.fail("reread rewrote settled backlog")

    monkeypatch.setattr(feedback, "_conn", traced_conn)
    monkeypatch.setattr(feedback, "_record_completion_event_in_conn", unexpected_write)
    # Retirement survives both rereading the old log and losing it to rotation.
    assert feedback.ingest_relay_pacing(log, now=cutoff + 60) == result
    log.unlink()
    assert feedback.ingest_relay_pacing(log, now=cutoff + 120) == result
    assert any(f"ts>={cutoff + 40}" in sql for sql in scans)
    assert not any(f"ts>={cutoff - 120}" in sql for sql in scans)
    with original_conn() as c:
        assert (
            c.execute(
                "SELECT status,payload_json,updated_ts FROM completion_events ORDER BY event_id"
            ).fetchall()
            == rows
        )


def test_backlog_cutoff_retires_unmatched_pre_cutoff_keeps_cutoff_and_joined(tmp_path, monkeypatch):
    log = _setup(tmp_path, monkeypatch)
    cutoff = feedback.RELAY_PACING_BACKLOG_CUTOFF
    log.write_text(_line(cutoff - 90) + _line(cutoff - 30) + _line(cutoff))
    feedback.record_lane_round("claude", "opener", None, 0, "none", cutoff - 60)
    # First decision joins the observed exit. The second is still unbound after that
    # join and is pre-cutoff, so it retires even though it follows the first exit.
    # The third is at the cutoff and stays pending.
    result = feedback.ingest_relay_pacing(log, now=cutoff + 60)
    assert (result["joined"], result["unjoined"]) == (1, 1)


def test_pre_cutoff_orphan_after_earlier_exit_retires_and_unblocks_later_legacy(
    tmp_path, monkeypatch
):
    log = _setup(tmp_path, monkeypatch)
    cutoff = feedback.RELAY_PACING_BACKLOG_CUTOFF
    # Earlier exit, then an unmatched pre-cutoff decision, then a later legacy
    # decision/exit pair that adjacency can join only once the orphan retires.
    log.write_text(_line(cutoff - 80) + _line(cutoff + 10))
    feedback.record_lane_round("claude", "opener", None, 0, "none", cutoff - 100)
    feedback.record_lane_round("claude", "opener", None, 0, "none", cutoff + 20)
    result = feedback.ingest_relay_pacing(log, now=cutoff + 30)
    assert result == {"status": "recorded", "decisions": 2, "joined": 1, "unjoined": 1}
    with feedback._conn() as c:
        retired = c.execute(
            "SELECT payload_json FROM completion_events WHERE status='skipped'"
        ).fetchall()
        assert len(retired) == 1
        payload = json.loads(retired[0][0])["relay_pacing"]
        assert payload["status"] == "unjoined"
        assert payload["unjoined_reason"] == feedback.RELAY_PACING_UNJOINED_REASON
        assert c.execute("SELECT COUNT(*) FROM influence_edges").fetchone()[0] == 1
    assert (_counts(now=cutoff + 30)["acted_on"], _counts(now=cutoff + 30)["outcome"]) == (
        1,
        1,
    )


def test_pre_fix_id_decision_survives_rotation_until_exact_exit(tmp_path, monkeypatch):
    log = _setup(tmp_path, monkeypatch)
    cutoff = feedback.RELAY_PACING_BACKLOG_CUTOFF
    log.write_text(_line(cutoff - 60, decision_id="explicit"))
    assert feedback.ingest_relay_pacing(log, now=cutoff)["unjoined"] == 0
    log.unlink()
    feedback.record_lane_round(
        "claude", "opener", None, 0, "none", cutoff + 1, relay_decision_id="explicit"
    )
    result = feedback.ingest_relay_pacing(log, now=cutoff + 1)
    assert (result["joined"], result["unjoined"]) == (1, 0)


@pytest.mark.parametrize(
    "bad_id",
    ["", "with space", "x" * (feedback.RELAY_DECISION_ID_MAX_CHARS + 1)],
)
def test_invalid_explicit_id_is_rejected(tmp_path, monkeypatch, bad_id):
    _setup(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="relay_decision_id"):
        feedback.record_lane_round(
            "claude", "opener", None, 0, "none", NOW, relay_decision_id=bad_id
        )


def test_max_length_explicit_id_is_accepted(tmp_path, monkeypatch):
    log = _setup(tmp_path, monkeypatch)
    token = "x" * feedback.RELAY_DECISION_ID_MAX_CHARS
    run_id = feedback.RELAY_PACING_ID_RUN_PREFIX + token
    assert len(run_id) == 192
    log.write_text(_line(decision_id=token))
    feedback.record_lane_round("claude", "opener", None, 0, "none", NOW, relay_decision_id=token)
    assert feedback.ingest_relay_pacing(log, now=NOW)["joined"] == 1
    with feedback._conn() as c:
        row = c.execute(
            "SELECT run_id,validation_status FROM completion_events WHERE phase='decision'"
        ).fetchone()
        assert row == (run_id, "accepted")


@pytest.mark.parametrize("bad_id", ["", "x" * (feedback.RELAY_DECISION_ID_MAX_CHARS + 1)])
def test_importer_skips_invalid_decision_id_in_log(tmp_path, monkeypatch, bad_id):
    log = _setup(tmp_path, monkeypatch)
    log.write_text(_line(decision_id=bad_id))
    feedback.record_lane_round("claude", "opener", None, 0, "none", NOW)
    assert feedback.ingest_relay_pacing(log, now=NOW)["decisions"] == 0
    assert _counts()["acted_on"] == 0


def test_backlog_without_any_exit_is_terminal_without_outcome(tmp_path, monkeypatch):
    log = _setup(tmp_path, monkeypatch)
    log.write_text(_line())
    end = feedback.RELAY_PACING_BACKLOG_CUTOFF
    result = feedback.ingest_relay_pacing(log, now=end)
    assert (result["joined"], result["unjoined"]) == (0, 1)
    log.unlink()
    feedback.record_lane_round("claude", "opener", None, 0, "none", end + 60)
    assert feedback.ingest_relay_pacing(log, now=end + 60) == result
    assert (_counts(now=end + 60)["acted_on"], _counts(now=end + 60)["outcome"]) == (0, 0)


def test_issue825_interleaved_backlog_explicit_join_credits_monitor(tmp_path, monkeypatch):
    """#825 production shape: 24 interleaved legacy decisions stay unjoined; one ID joins."""
    log = _setup(tmp_path, monkeypatch)
    cutoff = feedback.RELAY_PACING_BACKLOG_CUTOFF
    parts: list[str] = []
    stamp = cutoff + 60
    for _ in range(12):
        parts.append(_line(stamp, lane="opener"))
        stamp += 30
        parts.append(_line(stamp, chosen="cursor", lane="closer"))
        stamp += 30
    token = rate_incidents.generate_relay_decision_id(
        when=datetime.fromtimestamp(stamp, timezone.utc), pid=825, random_hex="ac01"
    )
    parts.append(_line(stamp, decision_id=token))
    exit_ts = stamp + 60
    log.write_text("".join(parts))
    # Concurrent-lane exits cannot join the legacy backlog by adjacency.
    feedback.record_lane_round("claude", "opener", None, 0, "none", cutoff + 200)
    feedback.record_lane_round("cursor", "closer", None, 0, "none", cutoff + 400)
    assert feedback.ingest_relay_pacing(log, now=exit_ts - 1) == {
        "status": "recorded",
        "decisions": 25,
        "joined": 0,
        "unjoined": 0,
    }
    tail = tmp_path / "tail"
    tail.write_text("all checks passed")
    result = rate_incidents.record_lane_round(
        agent="claude",
        lane="opener",
        receiver_reason=None,
        exit_status=0,
        output_file=tail,
        ts=exit_ts,
        relay_decision_id=token,
    )
    assert result["relay_pacing"] == {
        "status": "recorded",
        "decisions": 25,
        "joined": 1,
        "unjoined": 0,
    }
    counts = _counts(now=exit_ts)
    assert (counts["acted_on"], counts["outcome"]) == (1, 1)
    ledger = tmp_path / "capabilities.json"
    capabilities.register(feedback.RELAY_PACING_CAPABILITY, {"status": "wired"}, ledger)
    rep = monitor.report(
        now=exit_ts,
        path=ledger,
        env={},
        inputs=monitor._brain(feedback.DB_PATH, cutoff, exit_ts),
    )
    row = next(r for r in rep["rows"] if r["capability"] == feedback.RELAY_PACING_CAPABILITY)
    assert (row["acted_on"], row["outcome"]) == (1, 1)
    text = "\n".join(monitor.format_lines(rep))
    assert "windowed-capacity-policy" in text
    assert "acted_on 1" in text
    assert re.search(r"outcome 1\b", text)
