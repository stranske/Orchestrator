"""Hermetic regression coverage for the coverage-autopilot round #1162 targets."""

from __future__ import annotations

import json
import math
import subprocess

import pytest

import backlog
import capacity
import issue_readiness as readiness
import pattern_miner


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
