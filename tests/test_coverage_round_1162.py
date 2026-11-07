"""Hermetic regression coverage for the coverage-autopilot round #1162 targets.

The line lists in the issue are the statements and branches CI's combined pytest-plus-selftest
run left uncovered on these four modules. Tests below execute those paths with filesystem
state and every subprocess boundary redirected into pytest's temporary directory.
"""

from __future__ import annotations

import json
import math
import subprocess
import sys
import types
from dataclasses import replace

import pytest
from tests.test_pattern_miner import completion_episode

import backlog
import capacity
import issue_readiness as readiness
import pattern_miner
from completion_event_adapter import (
    CompletionEvent,
    CompletionIdentity,
    adapt_completion_event_envelope,
)


@pytest.fixture(autouse=True)
def isolated_runtime(tmp_path, monkeypatch):
    """Keep every target's file and subprocess boundary inside pytest's temp directory."""
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ORCH_LOCAL_RUNTIME", str(tmp_path / "runtime"))
    monkeypatch.setattr(capacity, "LEDGER", tmp_path / "capacity-ledger.ndjson")
    monkeypatch.setattr(capacity, "SHED_DIR", tmp_path / "shed")
    monkeypatch.setattr(backlog, "SENTINEL", tmp_path / "lane-handoff.json")


def test_capacity_parsers_and_profile_ledger_ignore_malformed_rows(tmp_path, monkeypatch):
    monkeypatch.setenv("COUNT", "7")
    monkeypatch.setenv("FLOAT", "1.25")
    monkeypatch.setenv("BAD", "nope")
    assert capacity._env_int_or_none("COUNT") == 7
    assert capacity._env_int_or_none("BAD") is None
    assert capacity._env_int_or_none("MISSING") is None
    assert capacity._env_float_or_none("FLOAT") == 1.25
    assert capacity._env_float_or_none("BAD") is None

    registry = {"p": {"capacity_pool_ids": ["pool"]}}
    ledger = tmp_path / "ledger.ndjson"
    ledger.write_text(
        "\n".join(
            [
                "not json",
                json.dumps({"selected_profile_id": "p", "event": "complete", "ts": 10}),
                json.dumps({"selected_profile_id": "p", "ts": "bad"}),
                json.dumps({"selected_profile_id": "p", "ts": 99}),
                json.dumps({"selected_profile_id": "p", "ts": 10, "units": "2"}),
            ]
        )
    )
    monkeypatch.setattr(capacity.time, "time", lambda: 20)
    assert capacity.profile_pool_usage_from_ledger(ledger, now=20, registry=registry) == {
        "pool": 2.0
    }
    assert capacity.debit_profile_pools(
        [
            {"selected_profile_id": "p", "units": "bad"},
            {"selected_profile_id": "p", "event": "complete", "units": 9},
        ],
        registry,
    ) == {"pool": 0.0}


def test_capacity_local_readers_fail_closed_without_starting_commands(tmp_path, monkeypatch):
    calls = []

    def fake_run(*args, **kwargs):
        calls.append((args, kwargs))
        raise OSError("offline")

    monkeypatch.setattr(capacity.shutil, "which", lambda _: None)
    monkeypatch.setattr(capacity.subprocess, "run", fake_run)
    assert capacity._ccusage_active_block() is None
    assert calls and calls[0][0][0][:2] == ["npx", "-y"]

    capacity.LEDGER.write_text(
        "\n".join(
            [
                "",
                "not json",
                json.dumps({"agent": "other", "ts": 10}),
                json.dumps({"agent": "codex", "ts": 99}),
                json.dumps({"agent": "codex", "ts": 10, "cost_usd": "2.5", "count": "3"}),
            ]
        )
    )
    monkeypatch.setattr(capacity.time, "time", lambda: 20)
    assert capacity._ledger_usage("codex", "5h") == (2.5, 3)
    assert capacity._estimate_gemini_units({"task_type": "implement", "count": "bad"}) == 4.0
    assert capacity._estimate_gemini_units({"event": "complete"}) == 0.0
    assert capacity._finite_number(True) is None
    assert capacity._finite_number(math.inf) is None
    assert capacity._finite_number(4) == 4


def test_capacity_marker_explanations_cover_missing_and_reset_metadata(monkeypatch):
    monkeypatch.setattr(capacity.time, "gmtime", lambda _: (_ for _ in ()).throw(OverflowError()))
    assert capacity.utc_stamp(10) == "epoch 10"
    assert (
        capacity._shed_cause({"incident_id": None, "category": None})
        == "a marker that names no incident"
    )
    assert (
        capacity._shed_cause({"incident_id": "i", "category": None})
        == "incident i (no category recorded)"
    )
    assert "cooldown" in capacity._shed_basis({"reset_at": None, "expires_at": 10})
    assert "before" in capacity._shed_basis({"reset_at": 20, "expires_at": 10})


def test_issue_readiness_defensive_github_and_cli_paths(monkeypatch, tmp_path, capsys):
    def bad_json(*args, **kwargs):
        return subprocess.CompletedProcess([], 0, "not json", "")

    monkeypatch.setattr(readiness.subprocess, "run", bad_json)
    assert readiness._gh_json(["issue", "list"]) == []
    monkeypatch.setattr(readiness.backlog, "SUPPORTED_REPOS", ("stranske/One",))
    assert readiness.fetch_open_issues() == []

    source = tmp_path / "broken.json"
    source.write_text("{")
    assert readiness.main(["--input", str(source)]) == 2
    assert "refusing input" in capsys.readouterr().err
    assert readiness.format_census(readiness.census([])).count("0") >= 3


def test_issue_readiness_write_failures_are_reported_without_network(monkeypatch):
    failures = []

    def denied(*args, **kwargs):
        failures.append(args[0])
        return subprocess.CompletedProcess([], 1, "", "denied")

    monkeypatch.setattr(readiness.subprocess, "run", denied)
    assert readiness.reconcile_durable([{"target": "One#1"}], dry_run=False)["errors"] == [
        "One#1: denied"
    ]
    assert readiness.apply_task_labels([{"target": "One#2", "label": "refactor"}], dry_run=False)[
        "errors"
    ] == ["One#2: denied"]
    assert len(failures) == 2


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"min_positive_subjects": 0}, "min_positive_subjects"),
        ({"max_negative_ratio": 2}, "max_negative_ratio"),
        ({"candidate_ttl_days": 0}, "candidate_ttl_days"),
    ],
)
def test_pattern_miner_rejects_invalid_policy_bounds(kwargs, message):
    with pytest.raises(ValueError, match=message):
        pattern_miner.PatternMiner(**kwargs)


def test_pattern_miner_state_and_atomic_writer(tmp_path):
    miner = pattern_miner.PatternMiner()
    missing = tmp_path / "missing.json"
    assert miner.load_state(missing) is False
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"schema": "wrong", "version": 1}))
    with pytest.raises(ValueError, match="unsupported"):
        miner.load_state(bad)
    destination = tmp_path / "nested" / "artifact.json"
    pattern_miner._write_json_atomic(destination, {"ok": True})
    assert json.loads(destination.read_text()) == {"ok": True}
    assert not list(destination.parent.glob("*.tmp"))


def test_backlog_parses_expiry_and_preserves_unknown_sentinel_states(tmp_path, monkeypatch):
    assert backlog._blocker_expiry_ts(None) is None
    assert backlog._blocker_expiry_ts({"expires_at": "bad"}) is None
    assert backlog._blocker_expiry_ts({"expires_at": 10.9}) == 10
    assert backlog._blocker_expiry_ts({"expires_at": "1970-01-01T00:00:10Z"}) == 10
    assert backlog._closer_preference_key({"number": "bad", "isDraft": True}) == (True, 0, 0)
    assert backlog.scoped_blocker_source() == "absent"
    backlog.SENTINEL.write_text("[")
    assert backlog.scoped_blocker_source() == "unparseable"
    backlog.SENTINEL.write_text("[]")
    assert backlog.scoped_blocker_source() == "wrong_shape"
    backlog.SENTINEL.write_text(
        json.dumps({"stop": {"scoped_blockers": {"old": {"expires_at": 1}, "live": {}}}})
    )
    assert backlog.expired_scoped_blockers(now=2) == {"old": {"expires_at": 1}}
    assert backlog.load_scoped_blockers(now=2) == {"live"}


def _forbid_subprocess(monkeypatch, module):
    """Replace subprocess entry points and record that no real command was launched."""

    def forbidden(*args, **kwargs):
        raise AssertionError(f"subprocess must stay stubbed, got {args!r}")

    monkeypatch.setattr(module.subprocess, "run", forbidden)
    monkeypatch.setattr(module.subprocess, "Popen", forbidden)


def _quiet_capacity(monkeypatch):
    """Hold the host shed marker, auth probe, and model catalog out of compute()."""
    monkeypatch.setattr(capacity, "_shed", lambda agent: False)
    monkeypatch.setattr(capacity, "_auth_health", lambda agent: None)
    monkeypatch.setattr(capacity, "_unresolvable_tier", lambda agent: None)
    monkeypatch.setattr(
        capacity,
        "_model_health",
        lambda agent, tier="full": {"model": "stub-model", "resolvable": True},
    )


def _completed(args, code=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args, code, stdout, stderr)


def test_capacity_active_block_count_dollar_and_plan_thresholds(monkeypatch):
    started = []

    def fake_run(cmd, **kwargs):
        started.append(list(cmd))
        payload = {"blocks": [{"isActive": False}, {"isActive": True, "id": "live"}]}
        return _completed(cmd, stdout=json.dumps(payload))

    monkeypatch.setattr(
        capacity.shutil, "which", lambda name: "/bin/ccusage" if name == "ccusage" else None
    )
    monkeypatch.setattr(capacity.subprocess, "run", fake_run)
    monkeypatch.setattr(capacity.subprocess, "Popen", fake_run)
    assert capacity._ccusage_active_block()["id"] == "live"
    assert started == [["/bin/ccusage", "blocks", "--active", "-j"]]

    _quiet_capacity(monkeypatch)
    now = 1_000_000.0
    monkeypatch.setattr(capacity.time, "time", lambda: now)

    def write(**fields):
        capacity.LEDGER.write_text(json.dumps({"agent": "meter", "ts": now - 10, **fields}) + "\n")

    write(count=5, cost_usd=10)
    state, reason, _meta = capacity.compute(
        "meter", {"model": "count", "window": "daily", "limit": 5}, None
    )
    assert (state, reason) == (capacity.SHED, "5/5 requests this daily")
    write(count=8, cost_usd=10)
    state, reason, _meta = capacity.compute(
        "meter", {"model": "count", "window": "daily", "limit": 10}, None
    )
    assert (state, reason) == (capacity.WARN, "8/10 requests this daily")
    write(count=1, cost_usd=1)
    state, reason, _meta = capacity.compute(
        "meter", {"model": "count", "window": "daily", "limit": 10}, None
    )
    assert (state, reason) == (capacity.OK, "1/10 requests this daily")

    write(count=1, cost_usd=10)
    state, reason, _meta = capacity.compute(
        "meter", {"model": "dollar", "window": "daily", "limit_usd": 10}, None
    )
    assert (state, reason) == (capacity.SHED, "$10.00/$10 this daily")
    state, reason, _meta = capacity.compute(
        "meter", {"model": "dollar", "window": "daily", "limit_usd": 12.5}, None
    )
    assert (state, reason) == (capacity.WARN, "$10.00/$12 this daily")
    state, reason, _meta = capacity.compute(
        "meter", {"model": "dollar", "window": "daily", "limit_usd": 100}, None
    )
    assert (state, reason) == (capacity.OK, "$10.00/$100 this daily")

    write(count=9, cost_usd=0)
    state, reason, _meta = capacity.compute(
        "meter",
        {
            "model": "plan_then_credit",
            "plan_limit": 10,
            "plan_window": "daily",
            "credit_usd": 25.0,
            "credit_window": "monthly",
        },
        None,
    )
    assert state == capacity.WARN and "near plan cap" in reason
    state, reason, meta = capacity.compute("meter", {"model": "mystery"}, None)
    assert (state, reason, meta) == (capacity.UNKNOWN, "no capacity model for agent", {})
    assert capacity._finite_number(10**10000) is None


def test_capacity_windowed_ledger_skips_and_soft_budget_warnings(monkeypatch):
    _quiet_capacity(monkeypatch)
    _forbid_subprocess(monkeypatch, capacity)
    now = 1_800_000.0
    monkeypatch.setattr(capacity.time, "time", lambda: now)
    outside = now - 6 * 3600
    weekly_rows = [
        {"agent": "gemini", "ts": outside - i, "task_type": "implement", "count": 1}
        for i in range(4)
    ]
    capacity.LEDGER.write_text("\n".join(json.dumps(row) for row in weekly_rows) + "\n")
    cfg = {
        "model": "windowed_prepaid",
        "window_soft_cap": 10.0,
        "weekly_soft_cap": 20.0,
        "reserve_fraction": 0.0,
        "drain_minutes": 0,
    }
    state, reason, meta = capacity.compute("gemini", cfg, None)
    assert state == capacity.WARN and meta["used_weekly"] == 16.0 and meta["used_5h"] == 0.0
    assert "near weekly" in reason

    junk = [
        "",
        "not json",
        json.dumps({"agent": "gemini", "ts": "yesterday", "task_type": "implement"}),
        json.dumps({"agent": "gemini", "ts": now + 10_000, "task_type": "implement", "count": 1}),
        json.dumps(
            {"agent": "gemini", "ts": now - 10, "event": "complete", "task_type": "implement"}
        ),
        json.dumps({"agent": "gemini", "ts": now - 10, "task_type": "implement", "count": 1}),
        json.dumps({"agent": "gemini", "ts": now - 20, "task_type": "implement", "count": 1}),
    ]
    capacity.LEDGER.write_text("\n".join(junk) + "\n")
    real_estimate = capacity._estimate_gemini_units

    def estimate(row):
        # A complete row must be ignored before this estimator runs. Returning a huge
        # unit count makes a deleted `continue` change the measured window.
        if row.get("event") == "complete":
            return 100.0
        return real_estimate(row)

    monkeypatch.setattr(capacity, "_estimate_gemini_units", estimate)
    state, reason, meta = capacity.compute("gemini", cfg, None)
    assert state == capacity.WARN and meta["used_5h"] == 8.0 and meta["used_weekly"] == 8.0
    assert "near 5h" in reason and "min to window refresh" in reason


def test_capacity_preflight_failures_and_pacing_projection(monkeypatch):
    monkeypatch.setitem(sys.modules, "adapters", None)
    assert capacity._model_health("gemini") is None
    assert capacity._gemini_model_health() is None
    assert capacity._unresolvable_tier("gemini") is None
    assert capacity._auth_health("gemini") is None
    assert capacity._tier_models("gemini") == {}
    assert capacity._tier_ceiling("gemini") is None

    projected = capacity.experiment_pacing(
        {
            "agents": {
                "ghost": {"pacing_state": {"state": "hold"}, "state": "ok"},
                "codex": {"pacing_state": "steady", "state": "ok"},
            },
            "pools": {"codex-subscription": {"state": "shed"}},
        }
    )
    assert projected["ghost"] == "hold"
    assert projected["codex"] == "codex-subscription=shed"


def test_capacity_cli_calibrate_and_unmeasured_pick(monkeypatch, tmp_path, capsys):
    import pacing_usage
    import router

    handoff = tmp_path / "handoff"
    handoff.mkdir()
    monkeypatch.setattr(capacity, "HANDOFF", handoff)
    monkeypatch.setattr(pacing_usage, "CALIBRATION_FILE", tmp_path / "calibration.json")
    _forbid_subprocess(monkeypatch, capacity)
    assert capacity.main(["calibrate-codex"]) == 1
    assert "refuses to write" in capsys.readouterr().out
    assert not (tmp_path / "calibration.json").exists()

    monkeypatch.setattr(pacing_usage, "calibrate", lambda *args, **kwargs: (True, "calibrated"))
    assert capacity.main(["calibrate-codex"]) == 0
    assert capsys.readouterr().out.strip() == "calibrated"

    monkeypatch.setattr(capacity, "build", lambda *args, **kwargs: {"agents": {}})
    monkeypatch.setattr(router, "select_agent", lambda *args, **kwargs: None)
    monkeypatch.setattr(router, "learned_ranks", lambda: {})
    assert capacity.main(["pick-agent", "--candidates", "codex"]) == 1
    assert "unmeasured" in capsys.readouterr().err


def test_issue_readiness_heartbeat_and_defensive_reads(monkeypatch):
    import capabilities

    def explode(*args, **kwargs):
        raise RuntimeError("ledger down")

    monkeypatch.setattr(capabilities, "production_heartbeat", explode)
    verdict = readiness.classify_issue({"title": "t", "body": "x = 1\n", "labels": []})
    assert verdict["verdict"]

    monkeypatch.setattr(readiness.backlog, "SUPPORTED_REPOS", ("stranske/One",))
    monkeypatch.setattr(readiness, "_gh_json", lambda args: None)
    _forbid_subprocess(monkeypatch, readiness)
    assert readiness.fetch_open_issues() == []

    def bad_labels(args, **kwargs):
        return _completed(args, stdout="not json")

    monkeypatch.setattr(readiness.subprocess, "run", bad_labels)
    assert readiness.repo_ready_label("FreshRepo", {}) is None


def test_issue_readiness_apply_errors_and_durable_reconcile(monkeypatch):
    cache = readiness.repo_ready_label.__defaults__[0]
    saved = dict(cache)
    cache.clear()
    cache["One"] = "status: ready"
    calls = []

    def denied(args, **kwargs):
        calls.append(list(args))
        return _completed(args, code=1, stderr="denied by stub")

    monkeypatch.setattr(readiness.subprocess, "run", denied)
    try:
        failed = readiness.apply_ready(
            [{"verdict": "auto_ready", "target": "One#4", "title": "t"}], dry_run=False
        )
    finally:
        cache.clear()
        cache.update(saved)
    assert failed["errors"] == ["One#4: denied by stub"]
    assert calls and calls[0][0] == "gh"

    fake = types.ModuleType("feedback")

    def record_owner_question(*args, **kwargs):
        raise RuntimeError("store down")

    fake.record_owner_question = record_owner_question
    monkeypatch.setitem(sys.modules, "feedback", fake)
    questioned = readiness.apply_ready(
        [{"verdict": "owner_review", "target": "One#5", "title": "risk"}], dry_run=False
    )
    assert questioned["errors"] == ["One#5: store down"]
    assert questioned["questions_raised"] == []

    census = readiness.census(
        [
            {
                "number": 3,
                "title": "fix the widget",
                "labels": [],
                "repository": {"name": "One"},
                "user": {"login": "ada"},
            }
        ]
    )
    assert census["true_open"] == 1 and census["labelled_but_counted"] == []

    def accepted(args, **kwargs):
        calls.append(list(args))
        return _completed(args)

    monkeypatch.setattr(readiness.subprocess, "run", accepted)
    applied = readiness.reconcile_durable([{"target": "One#8"}], dry_run=False)
    assert applied["applied"] == ["One#8"] and applied["errors"] == []


def test_issue_readiness_cli_reports_without_network(monkeypatch, tmp_path, capsys):
    issue = {
        "number": 9,
        "title": "refactor the config surface",
        "labels": [],
        "repository": {"name": "One"},
        "body": "move it",
    }
    monkeypatch.setattr(readiness, "fetch_open_issues", lambda limit=200: [issue])
    monkeypatch.setattr(readiness, "fetch_failures", lambda limit=200: [])
    _forbid_subprocess(monkeypatch, readiness)
    assert readiness.main(["--input", str(tmp_path / "missing.json"), "--apply"]) == 2
    assert "label-writing" in capsys.readouterr().err

    assert readiness.main(["--task-labels"]) == 0
    text = capsys.readouterr().out
    assert "One#9" in text and "refactor" in text
    assert readiness.main(["--task-labels", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["candidates"] == 1 and payload["rows"][0]["label"] == "refactor"

    monkeypatch.setattr(readiness, "APPLY_ENABLED", False)
    assert readiness.main(["--apply-task-labels"]) == 2
    assert "ORCH_ISSUE_AUTOREADY" in capsys.readouterr().err
    monkeypatch.setattr(readiness, "APPLY_ENABLED", True)
    monkeypatch.setattr(readiness, "fetch_open_issues", lambda limit=200: [])
    assert readiness.main(["--apply-task-labels", "--json"]) == 0
    applied = json.loads(capsys.readouterr().out)
    assert applied["apply"]["applied"] == [] and applied["apply"]["errors"] == []

    monkeypatch.setattr(readiness, "fetch_open_issues", lambda limit=200: [])
    monkeypatch.setattr(readiness, "APPLY_ENABLED", False)
    assert readiness.main(["--reconcile-durable"]) == 2
    assert "ORCH_ISSUE_AUTOREADY" in capsys.readouterr().err
    assert readiness.main(["--apply"]) == 2

    monkeypatch.setattr(readiness, "APPLY_ENABLED", True)
    assert readiness.main(["--census"]) == 0
    assert "raw open issues" in capsys.readouterr().out
    assert readiness.main(["--census", "--reconcile-durable", "--json"]) == 0
    census = json.loads(capsys.readouterr().out)
    assert census["reconcile"]["applied"] == []
    assert readiness.main(["--apply", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["apply"]["dry_run"] is False and report["apply"]["applied"] == []


def _identity(subject: str) -> CompletionIdentity:
    return CompletionIdentity(
        subject_id=subject,
        observation_id=f"obs-{subject}",
        family_id="family",
        attempt_id="attempt",
        canonical_target="owner/repo#1",
        repository="owner/repo",
        task_type="testgen",
        normalized_spec_hash="sha256:" + "a" * 64,
        base_sha="abc123",
        profile_id="codex:gpt",
        arm_id="worker",
        resolved_provider="openai",
        resolved_model="gpt",
    )


def _event(
    phase: str, identity: CompletionIdentity, event_id: str, occurred_at: int
) -> CompletionEvent:
    return CompletionEvent(
        event_id=event_id,
        run_id="run",
        attempt_id="attempt",
        event_type="completion",
        phase=phase,
        producer="dispatcher",
        status="success",
        validation_status="accepted",
        payload={},
        content_hash="sha256:" + "b" * 64,
        redaction_count=0,
        occurred_at=occurred_at,
        created_at=occurred_at,
        identity=identity,
    )


def test_pattern_miner_kind_owner_and_evidence_reasons():
    assert (
        pattern_miner._derive_kind(types.SimpleNamespace(payload={"role_ids": ["role:review"]}))
        == "role"
    )
    assert (
        pattern_miner._derive_kind(
            types.SimpleNamespace(payload={"workflow_ids": ["workflow:testgen"]})
        )
        == "workflow"
    )
    assert (
        pattern_miner._derive_kind(types.SimpleNamespace(payload={"skill_ids": ["skill:review"]}))
        == "skill"
    )
    assert (
        pattern_miner._derive_kind(
            types.SimpleNamespace(payload={"acceptance_gate_ids": ["gate:testgen"]})
        )
        == "acceptance_gate"
    )
    assert pattern_miner._derive_kind(types.SimpleNamespace(payload={})) == "unknown"
    assert (
        pattern_miner._derive_owner(types.SimpleNamespace(producer="workflow.runner"))
        == "workflows"
    )
    assert pattern_miner._derive_owner(types.SimpleNamespace(producer="")) == "unknown"

    def by_phase(**kwargs):
        rows = completion_episode(1, **kwargs)
        events = [adapt_completion_event_envelope(row) for row in rows]
        return {event.phase: event for event in events}

    missing = by_phase()
    missing["artifact"] = replace(missing["artifact"], payload={"result": {"status": "rejected"}})
    assert pattern_miner.normalize_episode(missing).evidence_reason == "missing_output_contract"

    failed = by_phase(verification="PASS", durability="durable")
    failed["outcome"] = replace(
        failed["outcome"],
        status="failure",
        payload={"delivery": {"merged": False}, "result": {"outcome_verdict": "failure"}},
    )
    assert pattern_miner.normalize_episode(failed).evidence_reason == "outcome_not_successful"

    regressed = by_phase(verification="PASS", durability="broke_later")
    assert pattern_miner.normalize_episode(regressed).evidence_reason == "durability_regressed"
    pending = by_phase(verification="PASS", durability="pending")
    assert pattern_miner.normalize_episode(pending).evidence_reason == "no_durable_result"


def test_pattern_miner_episode_dedupe_and_preserved_candidates(monkeypatch):
    identity = _identity("subject-a")
    other = _identity("subject-b")
    queue = [
        _event("trigger", identity, "e1", 1),
        _event("trigger", other, "e2", 2),
    ]

    def adapt(raw):
        return queue.pop(0)

    original_adapt = pattern_miner.adapt_completion_event_envelope
    monkeypatch.setattr(pattern_miner, "adapt_completion_event_envelope", adapt)
    miner = pattern_miner.PatternMiner()
    episodes, count = miner._assemble([{}, {}])
    assert count == 2 and episodes == []
    assert miner.rejections[0].reasons == ("episode_identity_mismatch",)

    queue[:] = [
        _event("trigger", identity, "e1", 1),
        _event("trigger", identity, "e2", 2),
    ]
    episodes, count = miner._assemble([{}, {}])
    assert count == 2 and episodes == []
    assert any(
        reason.startswith("duplicate_phase:trigger") for reason in miner.rejections[-1].reasons
    )
    monkeypatch.setattr(pattern_miner, "adapt_completion_event_envelope", original_adapt)

    def bare(fingerprint, successors=()):
        return pattern_miner.NormalizedEpisode(
            episode_id=fingerprint,
            events=(),
            graph={},
            selector={},
            kind_proposal="unknown",
            owner_proposal="unknown",
            fingerprint=fingerprint,
            semantic_fingerprint="sem-" + fingerprint,
            output_contract_fingerprint="out-" + fingerprint,
            successor_of=tuple(successors),
            positive=True,
            evidence_reason="eligible_durable_positive",
            artifact_refs=(),
            verification_ref=None,
            outcome_ref=None,
            durability_ref=None,
        )

    groups = pattern_miner.PatternMiner._dedupe_groups(
        [bare("fa", ("fb",)), bare("fb", ("fa", "fb"))]
    )
    assert len(groups) == 1 and {item.fingerprint for item in groups[0]} == {"fa", "fb"}

    preserved = pattern_miner.PatternMiner(min_positive_subjects=1)
    events = completion_episode(3)
    preserved.mine(events, now=200)
    assert preserved.candidates
    previous = preserved.candidates[0]
    preserved.candidates = [
        replace(
            previous,
            lifecycle=replace(previous.lifecycle, state="promoted"),
            telemetry={**previous.telemetry, "last_evidence_at": 1},
        )
    ]
    preserved.mine(events, now=200, preserve_existing=True)
    assert preserved.candidates[0].telemetry["last_evidence_at"] > 1

    preserved.candidates = [replace(previous, fingerprint="sha256:" + "cd" * 32)]
    preserved.mine(events, now=200, preserve_existing=True)
    fingerprints = {item.fingerprint for item in preserved.candidates}
    assert "sha256:" + "cd" * 32 in fingerprints
    assert previous.fingerprint in fingerprints

    kept = replace(
        previous,
        lifecycle=replace(previous.lifecycle, state="promoted"),
        telemetry={**previous.telemetry, "last_evidence_at": 10**12},
    )
    preserved.candidates = [kept]
    preserved.mine(events, now=200, preserve_existing=True)
    stored = {item.fingerprint: item for item in preserved.candidates}
    assert stored[previous.fingerprint].telemetry["last_evidence_at"] == 10**12


def test_pattern_miner_atomic_replace_failure_and_tick_line(monkeypatch, tmp_path, capsys):
    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(pattern_miner.os, "replace", boom)
    with pytest.raises(OSError, match="disk full"):
        pattern_miner._write_json_atomic(tmp_path / "nested" / "out.json", {"ok": False})
    assert not (tmp_path / "nested" / "out.json").exists()
    assert not list((tmp_path / "nested").glob("*.tmp"))

    status = tmp_path / "status.json"
    status.write_text(
        json.dumps({"mining_health": {"state": "rejecting", "actionable": True, "detail": "fix"}})
    )
    assert pattern_miner.main(["tick-line", "--status", str(status)]) == 0
    printed = capsys.readouterr().out
    assert "MINING: rejecting" in printed and "MINING-ACTIONABLE: fix" in printed


def test_backlog_unreadable_sentinel_and_failed_owner_question(monkeypatch):
    class Unreadable:
        def read_text(self, *args, **kwargs):
            raise PermissionError("denied")

    monkeypatch.setattr(backlog, "SENTINEL", Unreadable())
    assert backlog.scoped_blocker_source() == "unreadable"

    monkeypatch.setitem(sys.modules, "feedback", None)
    assert backlog.raise_expired_blocker_questions(now=10) == []


def test_backlog_question_failure_research_block_and_gh_readers(monkeypatch, tmp_path):
    sentinel = tmp_path / "lane-handoff.json"
    monkeypatch.setattr(backlog, "SENTINEL", sentinel)
    sentinel.write_text(
        json.dumps(
            {
                "stop": {
                    "scoped_blockers": {
                        "o/A#1": {"expires_at": 1, "await_human": True, "reason": "waiting"}
                    }
                }
            }
        )
    )
    fake = types.ModuleType("feedback")

    def record_owner_question(*args, **kwargs):
        raise RuntimeError("store down")

    fake.record_owner_question = record_owner_question
    with monkeypatch.context() as patch:
        patch.setitem(sys.modules, "feedback", fake)
        assert backlog.raise_expired_blocker_questions(now=10) == []

    feed = backlog.build_research_items(
        ["o/A"],
        lambda repo: [
            {
                "number": 7,
                "labels": [{"name": "status: ready"}],
                "title": "study this",
                "body": "body",
            }
        ],
        lambda repo: [],
        scoped={"o/A#7"},
    )
    assert feed["counts"]["scoped_blocker"] == 1 and feed["items"] == []

    calls = []

    def not_a_list(args, **kwargs):
        calls.append(list(args))
        return _completed(args, stdout="null")

    monkeypatch.setattr(backlog.subprocess, "run", not_a_list)
    monkeypatch.setattr(backlog.subprocess, "Popen", not_a_list)
    with pytest.raises(RuntimeError, match="no list"):
        backlog._gh_strict(["pr", "list"])
    assert calls and calls[0][0] == "gh"

    def failed(args, **kwargs):
        return _completed(args, code=1, stderr="nope")

    monkeypatch.setattr(backlog.subprocess, "run", failed)
    assert backlog._gh_json(["issue", "list"]) == []

    def exploded(*args, **kwargs):
        raise OSError("absent")

    monkeypatch.setattr(backlog.subprocess, "run", exploded)
    assert backlog._gh_json(["issue", "list"]) == []

    def malformed(args, **kwargs):
        return _completed(args, stdout="{")

    monkeypatch.setattr(backlog.subprocess, "run", malformed)
    assert backlog._gh_json(["issue", "list"]) == []

    monkeypatch.setattr(
        backlog,
        "_gh_json",
        lambda args: [{"number": 3}] if args[0] == "issue" else [{"number": 4}],
    )
    _forbid_subprocess(monkeypatch, backlog)
    assert backlog.live_fetch_issues("o/A") == [{"number": 3}]
    assert backlog.live_fetch_prs("o/A") == [{"number": 4}]


def test_backlog_cli_selftest_research_and_live_stay_local(monkeypatch, tmp_path, capsys):
    _forbid_subprocess(monkeypatch, backlog)
    assert backlog.main(["--selftest"]) == 0
    assert "backlog.py selftest: OK" in capsys.readouterr().out

    handoff = tmp_path / "handoff"
    monkeypatch.setattr(backlog, "HANDOFF", handoff)
    monkeypatch.setattr(backlog, "RESEARCH_BACKLOG_JSON", handoff / "research-backlog.json")
    monkeypatch.setattr(backlog, "BACKLOG_JSON", handoff / "backlog.json")
    monkeypatch.setattr(backlog, "SENTINEL", handoff / "lane-handoff.json")
    monkeypatch.setattr(backlog, "SUPPORTED_REPOS", ("o/A",))
    monkeypatch.setattr(backlog, "strict_fetch_issues", lambda repo: [])
    monkeypatch.setattr(backlog, "strict_fetch_prs", lambda repo: [])
    monkeypatch.setattr(backlog, "live_fetch_issues", lambda repo: [])
    monkeypatch.setattr(backlog, "live_fetch_prs", lambda repo: [])

    assert backlog.main(["--research"]) == 0
    research = json.loads((handoff / "research-backlog.json").read_text())
    assert research["counts"]["eligible"] == 0
    assert "research feed" in capsys.readouterr().out

    assert backlog.main([]) == 2
    assert "usage:" in capsys.readouterr().err

    assert backlog.main(["--dry-run"]) == 0
    assert not (handoff / "backlog.json").exists()
    dry = json.loads(capsys.readouterr().out)
    assert dry["items"] == []

    assert backlog.main(["--live"]) == 0
    stored = json.loads((handoff / "backlog.json").read_text())
    assert stored["items"] == [] and "orchestrator-local" in stored["scope"]
