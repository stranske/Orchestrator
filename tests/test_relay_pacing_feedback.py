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
WINDOW_DAYS_SECONDS = monitor.WINDOW_DAYS * 86400
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


@pytest.fixture
def quiet_switch_review(monkeypatch):
    """Keep switch_review.review offline and free of live side effects for #891 text-out pin."""
    import adjudicator_live
    import adjudicator_retro
    import runtime_ac_gate
    import switch_review

    monkeypatch.setattr(switch_review, "_capability_heartbeat", lambda *a, **kw: None)
    monkeypatch.setattr(switch_review, "stale_runners", lambda **kw: [])
    monkeypatch.setattr(switch_review, "adversarial_shape_population", lambda **kw: {})
    monkeypatch.setattr(switch_review, "mirror_drift", lambda **kw: {"status": "ok"})
    monkeypatch.setattr(switch_review, "fleet_gates", lambda **kw: {})
    monkeypatch.setattr(
        switch_review,
        "firing_regressions",
        lambda **kw: {"status": "ok", "generated_at": NOW, "rows": []},
    )
    monkeypatch.setattr(switch_review, "_exploration_gate", lambda: {})
    monkeypatch.setattr(
        switch_review,
        "gate_expiry",
        lambda **kw: {
            "status": "ok",
            "window_days": 7,
            "expiring": [],
            "lapsed": [],
            "renewable": 0,
            "rows_with_expiry": 0,
            "next_expiry": None,
            "ttl_days": 30,
        },
    )
    monkeypatch.setattr(
        switch_review,
        "capacity_shed",
        lambda **kw: {"status": "ok", "markers": [], "suspect": 0, "dir": "/tmp/orch-shed-fixture"},
    )
    monkeypatch.setattr(
        switch_review,
        "worktree_git_refusals",
        lambda **kw: {
            "refusals": {"status": "never_recorded", "count": 0, "path": "/tmp/refusals"},
            "gc_skips": {"status": "ok", "count": 0, "path": "/tmp/gc.log"},
        },
    )
    monkeypatch.setattr(switch_review, "weekly_pacing_check", lambda **kw: "weekly pacing: fixture")
    monkeypatch.setattr(
        switch_review, "switch_states", lambda **kw: {"held_off": [], "on_but_idle": []}
    )
    monkeypatch.setattr(runtime_ac_gate, "shadow_summary", lambda **kw: None)
    monkeypatch.setattr(runtime_ac_gate, "post_merge_summary", lambda: None)
    monkeypatch.setattr(adjudicator_live, "weekly_line", lambda: "")
    monkeypatch.setattr(adjudicator_retro, "weekly_line", lambda: "")


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
    assert result["relay_pacing"] == {
        "status": "recorded",
        "decisions": 1,
        "joined": 1,
        "unjoined": 0,
    }
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
    # Accepted decision events count as usable (#891 Oct 27 pass; no ledger success heartbeat).
    assert row["usable"] >= 1
    text = "\n".join(monitor.format_lines(rep))
    assert "acted_on 1" in text
    assert re.search(r"usable [1-9]", text)


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


def test_neutral_cursor_log_fields_emit_numeric_weights(capsys, monkeypatch):
    """#891: Cursor with pacing absent still yields numeric weights= for the relay log."""
    decision = {
        "policy": "posterior*pacing",
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
                "routing_weight": 0.8,
                "pacing": {"state": "warn", "routing_weight": 0.8},
            },
        ],
    }
    fields = rate_incidents.relay_log_fields_from_decision(decision, candidates="codex,cursor")
    assert fields["weights"] == "codex:0.8,cursor:1"
    assert "unknown" not in fields["weights"]
    assert fields["states"] == "codex:warn,cursor:ok"
    assert fields["policy"] == "posterior*pacing"

    import io

    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(decision)))
    assert rate_incidents.main(["format-relay-pacing-fields", "--candidates", "codex,cursor"]) == 0
    printed = json.loads(capsys.readouterr().out.strip())
    assert printed == fields


def test_pick_agent_emits_log_fields_for_neutral_cursor(monkeypatch, capsys):
    """pick-agent stderr carries log_fields so the host relay need not dig nested pacing."""
    import capacity
    import router

    snapshot = {
        "agents": {
            "codex": {
                "state": "warn",
                "pacing": {"state": "warn", "routing_weight": 0.8},
            },
            "cursor": {"state": "ok"},
        }
    }
    monkeypatch.setattr(capacity, "build", lambda: snapshot)
    monkeypatch.setattr(router, "learned_ranks", lambda: {})
    assert (
        capacity.main(["pick-agent", "--candidates", "codex,cursor", "--task-type", "implement"])
        == 0
    )
    captured = capsys.readouterr()
    assert captured.out.strip() in {"codex", "cursor"}
    decision = json.loads(captured.err.strip())
    assert decision["policy"] == "posterior*pacing"
    assert decision["log_fields"]["weights"] == "codex:0.8,cursor:1"
    assert "unknown" not in decision["log_fields"]["weights"]


def test_neutral_cursor_pacing_feeds_monitor_usable(tmp_path, monkeypatch, capsys):
    """#891: posterior*pacing with neutral Cursor → numeric weights → join → usable N>0."""
    import capacity
    import router

    log = _setup(tmp_path, monkeypatch)
    snapshot = {
        "agents": {
            "codex": {
                "state": "warn",
                "pacing": {"state": "warn", "routing_weight": 0.8},
            },
            "cursor": {"state": "ok"},
        }
    }
    monkeypatch.setattr(capacity, "build", lambda: snapshot)
    monkeypatch.setattr(router, "learned_ranks", lambda: {})
    assert (
        capacity.main(["pick-agent", "--candidates", "codex,cursor", "--task-type", "implement"])
        == 0
    )
    captured = capsys.readouterr()
    chosen = captured.out.strip()
    assert chosen in {"codex", "cursor"}
    decision = json.loads(captured.err.strip())
    assert decision["policy"] == "posterior*pacing"
    fields = decision["log_fields"]
    assert fields["weights"] == "codex:0.8,cursor:1"
    assert "unknown" not in fields["weights"]

    when = datetime.fromtimestamp(NOW - 30, timezone.utc)
    token = rate_incidents.append_relay_pacing_decision(
        source="codex",
        lane="opener",
        candidates="codex,cursor",
        chosen=chosen,
        weights=fields["weights"],
        states=fields["states"],
        fallback="no",
        when=when,
        log=log,
    )
    line = log.read_text(encoding="utf-8")
    assert "weights=codex:0.8,cursor:1" in line
    assert "unknown" not in line
    tail = tmp_path / "tail"
    tail.write_text("all checks passed")
    result = rate_incidents.record_lane_round(
        agent=chosen,
        lane="opener",
        receiver_reason=None,
        exit_status=0,
        output_file=tail,
        ts=NOW,
        relay_decision_id=token,
    )
    assert result["relay_pacing"]["joined"] == 1
    assert (_counts()["acted_on"], _counts()["outcome"]) == (1, 1)

    ledger = tmp_path / "capabilities.json"
    capabilities.register(feedback.RELAY_PACING_CAPABILITY, {"status": "wired"}, ledger)
    inputs = monitor._brain(feedback.DB_PATH, NOW - WINDOW_DAYS_SECONDS, NOW)
    assert inputs["completion_events"] is not None
    row = monitor.report(now=NOW, path=ledger, env={}, inputs=inputs)["rows"][0]
    assert row["capability"] == feedback.RELAY_PACING_CAPABILITY
    assert row["usable"] >= 1
    assert row["acted_on"] >= 1
    text = "\n".join(monitor.format_lines({"rows": [row], "errors": []}))
    assert re.search(r"usable [1-9]", text)


def test_legacy_cursor_log_fields_emit_neutral_weight(monkeypatch, capsys):
    """#891: unmeasured-only tier stays legacy, but Cursor still logs weight 1 not unknown."""
    import capacity
    import router

    snapshot = {
        "agents": {
            "cursor": {"state": "ok"},
            "muse": {"state": "ok"},
        }
    }
    monkeypatch.setattr(capacity, "build", lambda: snapshot)
    monkeypatch.setattr(router, "learned_ranks", lambda: {})
    assert (
        capacity.main(["pick-agent", "--candidates", "cursor,muse", "--task-type", "implement"])
        == 0
    )
    captured = capsys.readouterr()
    assert captured.out.strip() in {"cursor", "muse"}
    decision = json.loads(captured.err.strip())
    assert decision["policy"].startswith("legacy")
    assert decision["log_fields"]["weights"] == "cursor:1,muse:1"
    assert "unknown" not in decision["log_fields"]["weights"]


def test_switch_review_text_out_usable_after_neutral_cursor_pacing(
    tmp_path, monkeypatch, capsys, quiet_switch_review
):
    """#891 AC surface: switch_review --text-out shows windowed-capacity-policy usable N>0."""
    import capacity
    import router
    import switch_review

    log = _setup(tmp_path, monkeypatch)
    snapshot = {
        "agents": {
            "codex": {
                "state": "warn",
                "pacing": {"state": "warn", "routing_weight": 0.8},
            },
            "cursor": {"state": "ok"},
        }
    }
    monkeypatch.setattr(capacity, "build", lambda: snapshot)
    monkeypatch.setattr(router, "learned_ranks", lambda: {})
    assert (
        capacity.main(["pick-agent", "--candidates", "codex,cursor", "--task-type", "implement"])
        == 0
    )
    captured = capsys.readouterr()
    chosen = captured.out.strip()
    decision = json.loads(captured.err.strip())
    assert decision["policy"] == "posterior*pacing"
    fields = decision["log_fields"]
    assert fields["weights"] == "codex:0.8,cursor:1"
    assert "unknown" not in fields["weights"]

    when = datetime.fromtimestamp(NOW - 30, timezone.utc)
    token = rate_incidents.append_relay_pacing_decision(
        source="codex",
        lane="opener",
        candidates="codex,cursor",
        chosen=chosen,
        weights=fields["weights"],
        states=fields["states"],
        fallback="no",
        when=when,
        log=log,
    )
    assert "weights=codex:0.8,cursor:1" in log.read_text(encoding="utf-8")
    tail = tmp_path / "tail"
    tail.write_text("all checks passed")
    assert (
        rate_incidents.record_lane_round(
            agent=chosen,
            lane="opener",
            receiver_reason=None,
            exit_status=0,
            output_file=tail,
            ts=NOW,
            relay_decision_id=token,
        )["relay_pacing"]["joined"]
        == 1
    )

    ledger = tmp_path / "capabilities.json"
    capabilities.register(feedback.RELAY_PACING_CAPABILITY, {"status": "wired"}, ledger)
    inputs = monitor._brain(feedback.DB_PATH, NOW - WINDOW_DAYS_SECONDS, NOW)
    text_out = tmp_path / "switch-review.md"
    rep = switch_review.review(
        now=NOW,
        path=ledger,
        env={},
        value_chain_inputs=inputs,
    )
    switch_review.write_text_report(rep, text_out)
    body = text_out.read_text(encoding="utf-8")
    match = re.search(r"windowed-capacity-policy:.*usable (\d+)", body)
    assert match, body
    assert int(match.group(1)) > 0, body


def test_pick_agent_non_relay_candidate_keeps_success(monkeypatch, capsys):
    """Relay-only log formatting cannot fail a valid general router pick."""
    import capacity
    import router

    monkeypatch.setattr(capacity, "build", lambda: {"agents": {"gemini": {"state": "ok"}}})
    monkeypatch.setattr(router, "learned_ranks", lambda: {})
    monkeypatch.setattr(router, "select_agent", lambda *args, **kwargs: {"agent": "gemini"})
    assert capacity.main(["pick-agent", "--candidates", "gemini"]) == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == "gemini"
    diagnostic, raw = captured.err.splitlines()
    assert "Relay log fields unavailable" in diagnostic
    assert "supported agents" in diagnostic
    decision = json.loads(raw)
    assert "log_fields" not in decision
    assert decision["candidates"][0]["agent"] == "gemini"


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
