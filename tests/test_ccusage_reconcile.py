"""Hermetic coverage for autopilot round #1182 (exploration_collection, observability_dashboard, ccusage_reconcile)."""

from __future__ import annotations

import base64
import json
import socket
import subprocess
import tempfile
from datetime import datetime, timezone

import advisor_hermetic
import pytest

import ccusage_reconcile as ccusage
import feedback


@pytest.fixture(autouse=True)
def isolated_runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(advisor_hermetic, "root", lambda: tmp_path)
    with advisor_hermetic.activated(monkeypatch):
        monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path / "state"))
        monkeypatch.setenv("ORCH_LOCAL_RUNTIME", str(tmp_path / "runtime"))
        monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "runtime" / "feedback.db")
        # HANDOFF is captured outside ORCH_LOCAL_RUNTIME at import time.
        monkeypatch.setattr(ccusage.adapters, "HANDOFF", tmp_path / "handoff")
        monkeypatch.setattr(ccusage.adapters, "LEDGER", tmp_path / "handoff" / "ledger.ndjson")
        (tmp_path / "runtime").mkdir(parents=True, exist_ok=True)

        attempted_external = []

        def guarded_subprocess_run(cmd, **kwargs):
            if (
                cmd
                and cmd[0] == "gh"
                and len(cmd) >= 3
                and cmd[1] == "api"
                and "langsmith_fleet_registry.json" in cmd[2]
            ):
                raw = json.dumps({"repos": []}).encode()
                body = json.dumps({"content": base64.b64encode(raw).decode("ascii")})
                return type("Proc", (), {"returncode": 0, "stdout": body, "stderr": ""})()
            if cmd and cmd[0] == "agy" and "models" in cmd:
                # Offline catalog probe shape (tab-separated id + label).
                body = "gemini-3.8-flash-high\tGemini 3.8 Flash (High)\n"
                return type("Proc", (), {"returncode": 0, "stdout": body, "stderr": ""})()
            if cmd and cmd[0] == "cursor-agent" and "--list-models" in cmd:
                body = "composer-2.5 - Composer 2.5 (current)\n"
                return type("Proc", (), {"returncode": 0, "stdout": body, "stderr": ""})()
            if cmd:
                joined = " ".join(str(c) for c in cmd)
                if len(cmd) >= 3 and cmd[0] == "codex" and cmd[1] == "login" and cmd[2] == "status":
                    return type("Proc", (), {"returncode": 0, "stdout": "ok\n", "stderr": ""})()
                if len(cmd) >= 3 and cmd[0] == "claude" and cmd[1] == "auth" and cmd[2] == "status":
                    return type("Proc", (), {"returncode": 0, "stdout": "ok\n", "stderr": ""})()
                if "ccusage" in joined and ("session" in joined or "blocks" in joined):
                    if "blocks" in joined:
                        body = json.dumps({"blocks": []})
                    else:
                        body = "{}"
                    return type("Proc", (), {"returncode": 0, "stdout": body, "stderr": ""})()
            attempted_external.append((cmd, kwargs))
            pytest.fail("tests/test_coverage_round_1182.py attempted unstubbed external I/O")

        def forbid_external(*args, **kwargs):
            attempted_external.append((args, kwargs))
            pytest.fail("tests/test_coverage_round_1182.py attempted external I/O")

        monkeypatch.setattr(subprocess, "run", guarded_subprocess_run)
        monkeypatch.setattr(subprocess, "Popen", forbid_external)
        monkeypatch.setattr(socket, "socket", forbid_external)
        monkeypatch.setattr(socket, "create_connection", forbid_external)

        real_mkdtemp = tempfile.mkdtemp

        def mkdtemp_under_tmp(suffix="", prefix=None, dir=None):
            root = tmp_path / "_tempfile_root"
            root.mkdir(parents=True, exist_ok=True)
            # Always parent beneath tmp_path so selftests never allocate outside the test sandbox.
            return real_mkdtemp(suffix=suffix, prefix=prefix or "tmp", dir=str(root))

        monkeypatch.setattr(tempfile, "mkdtemp", mkdtemp_under_tmp)

        yield

        assert attempted_external == []


def test_ccusage_read_ledger_and_parsers(tmp_path):
    missing, errors = ccusage._read_ledger(tmp_path / "absent.ndjson")
    assert missing == [] and errors == []

    ledger = tmp_path / "ledger.ndjson"
    ledger.write_text(
        "\n".join(
            [
                "",
                "not-json",
                json.dumps([]),
                json.dumps({"run_id": "r1", "event": "start", "ts": 1}),
            ]
        )
    )
    rows, errors = ccusage._read_ledger(ledger)
    assert len(rows) == 1
    assert len(errors) == 2

    assert ccusage._parse_iso_ts(10) == 10
    assert ccusage._parse_iso_ts("2026-01-01T00:00:00Z") is not None
    assert ccusage._parse_iso_ts("") is None
    assert ccusage._parse_iso_ts("not-a-date") is None

    assert ccusage._parse_codex_period_ts(42) is None
    assert ccusage._parse_codex_period_ts("rollout-2026-06-22T06-00-00-id") is not None
    assert ccusage._parse_codex_period_ts("rollout-not-a-ts") is None


def test_ccusage_run_windows_skips(tmp_path, monkeypatch):
    feedback.record_run("known", "o/r#1", "implement", "codex", ts=1)
    feedback.record_run("run-vibe", "o/r#2", "implement", "vibe", ts=1)
    ledger = tmp_path / "ledger.ndjson"
    ledger.write_text(
        "\n".join(
            [
                json.dumps({"run_id": "", "event": "start", "ts": 1}),
                json.dumps({"run_id": "unknown", "event": "start", "ts": 1}),
                json.dumps({"run_id": "run-vibe", "event": "start", "ts": 1, "agent": "vibe"}),
                json.dumps({"run_id": "run-vibe", "event": "complete", "ts": 10, "agent": "vibe"}),
                json.dumps({"run_id": "known", "event": "start", "ts": 10}),
                json.dumps({"run_id": "bad-window", "event": "start", "ts": 100, "agent": "codex"}),
                json.dumps(
                    {"run_id": "bad-window", "event": "complete", "ts": 50, "agent": "codex"}
                ),
            ]
        )
    )
    windows, skipped = ccusage._run_windows(
        [json.loads(line) for line in ledger.read_text().splitlines() if line.strip()],
        {"known": "codex", "run-vibe": "vibe", "bad-window": "codex"},
    )
    assert not windows
    assert skipped["missing_run_id"] == 1
    assert skipped["unknown_run_id"] == 1
    assert skipped["unsupported_agent"] == 1
    assert skipped["incomplete_run_window"] == 1
    assert skipped["invalid_run_window"] == 1


def test_ccusage_load_sessions_and_cli(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(ccusage.shutil, "which", lambda _: None)

    def fail_run(cmd, **kwargs):
        if cmd[0] == "npx":
            return type("P", (), {"returncode": 1, "stdout": "", "stderr": "nope"})()
        raise OSError("boom")

    monkeypatch.setattr(ccusage.subprocess, "run", fail_run)
    sessions, errors = ccusage._load_ccusage_sessions(since_days=7)
    assert sessions == [] and errors

    def ok_run(cmd, **kwargs):
        payload = json.dumps({"sessions": [{"agent": "codex", "period": "p"}]})
        return type("P", (), {"returncode": 0, "stdout": payload, "stderr": ""})()

    monkeypatch.setattr(ccusage.subprocess, "run", ok_run)
    sessions, errors = ccusage._load_ccusage_sessions(since_days=7)
    assert len(sessions) == 1 and not errors

    monkeypatch.setattr(
        ccusage.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(TimeoutError())
    )
    _, errors = ccusage._load_ccusage_sessions()
    assert errors and "ccusage failed" in errors[0]

    summary = {
        "dry_run": True,
        "sessions_read": 0,
        "completed_windows": 0,
        "prepared": 0,
        "written_cost_rows": 0,
        "skipped": {"x": 1},
        "errors": ["e"],
    }
    ccusage._print_summary(summary, as_json=False)
    text = capsys.readouterr().out
    assert "ccusage_reconcile:" in text and "skipped:" in text

    monkeypatch.setattr(ccusage, "_selftest", lambda: None)
    assert ccusage.main(["--selftest"]) == 0
    with pytest.raises(SystemExit) as exc:
        ccusage.main([])
    assert exc.value.code == 2

    monkeypatch.setattr(
        ccusage,
        "reconcile",
        lambda *a, **k: {
            "dry_run": True,
            "sessions_read": 0,
            "completed_windows": 0,
            "prepared": 0,
            "written_cost_rows": 0,
            "skipped": {},
            "errors": [],
            "strict_failed": True,
        },
    )
    assert ccusage.main(["reconcile", "--strict"]) == 2


def test_ccusage_reconcile_uses_loaded_sessions(monkeypatch, tmp_path):
    calls = []

    def load_sessions(**kwargs):
        calls.append(kwargs)
        return [{"agent": "vibe", "period": "loaded-session"}], ["load err"]

    monkeypatch.setattr(ccusage, "_load_ccusage_sessions", load_sessions)
    monkeypatch.setattr(ccusage.adapters, "LEDGER", tmp_path / "ledger.ndjson")
    (tmp_path / "ledger.ndjson").write_text("")
    out = ccusage.reconcile(dry_run=True, since_days=3, strict=True)
    assert "load err" in out["errors"]
    assert calls == [{"since_days": 3}]
    assert out["sessions_read"] == 1
    assert out["skipped"] == {"unsupported_agent": 1}
    assert out["strict_failed"] is True
    assert out["written_cost_rows"] == 0


def test_ccusage_measurement_and_match_helpers():
    assert ccusage._number(-1) == 0.0
    assert ccusage.RunWindow("r", "codex", 1, 10).latency_s == 9.0
    window = ccusage.RunWindow("run-a", "codex", 100, 200)
    session = {
        "agent": "vibe",
        "period": "p",
        "metadata": {"lastActivity": "1970-01-01T00:01:50Z"},
    }
    assert ccusage._match_window(session, [window], slack_seconds=0) == (None, "unsupported_agent")
    session["agent"] = "codex"
    session["metadata"] = {}
    assert ccusage._match_window(session, [window], slack_seconds=0) == (
        None,
        "missing_last_activity",
    )
    session["metadata"] = {"lastActivity": "1970-01-01T00:05:00Z"}
    assert ccusage._match_window(session, [window], slack_seconds=0)[1] == "no_matching_run_window"
    session["metadata"] = {"lastActivity": "1970-01-01T00:01:50Z"}
    assert (
        ccusage._match_window(session, [window, window], slack_seconds=0)[1]
        == "ambiguous_run_window"
    )


def test_ccusage_reconcile_write_path(tmp_path, monkeypatch):
    monkeypatch.setattr(ccusage.adapters, "LEDGER", tmp_path / "ledger.ndjson")
    base = 1_700_000_000
    feedback.record_run("run-a", "o/r#1", "implement", "codex", ts=base)
    ccusage.adapters.record_ledger("codex", event="start", run_id="run-a", ts=base)
    ccusage.adapters.record_ledger("codex", event="complete", run_id="run-a", ts=base + 60)
    sessions = [
        {
            "agent": "codex",
            "period": "p1",
            "metadata": {
                "lastActivity": datetime.fromtimestamp(base + 30, tz=timezone.utc)
                .isoformat()
                .replace("+00:00", "Z")
            },
            "inputTokens": 2,
            "outputTokens": 1,
            "totalCost": 0.1,
        }
    ]
    dry = ccusage.reconcile(
        ccusage.adapters.LEDGER, sessions=sessions, dry_run=True, slack_seconds=0
    )
    assert dry["prepared"] == 1
    with feedback._conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM costs").fetchone()[0] == 0
    live = ccusage.reconcile(
        ccusage.adapters.LEDGER, sessions=sessions, dry_run=False, slack_seconds=0
    )
    assert live["written_cost_rows"] == 1
    with feedback._conn() as conn:
        row = conn.execute(
            "SELECT run_id, tokens_in, tokens_out, cost_usd, source FROM costs"
        ).fetchone()
    assert tuple(row) == ("run-a", 2, 1, 0.1, "ccusage")
    ccusage._print_summary(live, as_json=True)


def test_ccusage_selftest_runs(monkeypatch, tmp_path):
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "fb.db")
    monkeypatch.setattr(ccusage.adapters, "LEDGER", tmp_path / "ledger.ndjson")
    ccusage._selftest()
