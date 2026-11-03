"""Issue #839: hermetic regression coverage for redirect sweep and lane watching."""

from __future__ import annotations

import json
import os
import socket
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import redirect_plan
import redirect_shadow
import redirect_sweep
import watch


@pytest.fixture(autouse=True)
def isolated_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Tests must neither inspect live claims nor start a process."""
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("ORCH_LOCAL_RUNTIME", str(tmp_path))
    monkeypatch.setenv("HANDOFF_DIR", str(tmp_path / "handoff"))
    monkeypatch.setattr(redirect_sweep, "HANDOFF", tmp_path / "handoff")
    attempted: list[object] = []

    def forbidden(*args, **kwargs):
        attempted.append((args, kwargs))
        raise AssertionError("issue #839 coverage test started an external process or network call")

    for name in ("run", "Popen", "call", "check_call", "check_output"):
        monkeypatch.setattr(subprocess, name, forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    yield
    assert attempted == []


def test_redirect_helpers_keep_invalid_corpus_rows_nonfatal(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus.jsonl"
    corpus.write_text(
        "\n".join(
            [
                "",
                "not json",
                json.dumps({"kind": "other"}),
                json.dumps(
                    {"kind": "redirect_proposal", "ts": "not-a-time", "sweep_fingerprint": "bad"}
                ),
                json.dumps({"kind": "redirect_proposal", "ts": 10, "sweep_fingerprint": "old"}),
                json.dumps(
                    {
                        "kind": "redirect_proposal",
                        "ts": 100,
                        "report": {"sweep_fingerprint": "from-report"},
                    }
                ),
                json.dumps({"kind": "redirect_proposal", "ts": 100, "sweep_fingerprint": "good"}),
            ]
        )
        + "\n"
    )
    assert redirect_sweep._existing_sweep_fingerprints(corpus, since_ts=50) == {
        "good",
        "from-report",
    }
    assert redirect_sweep._parse_action_set(" collect,invalid,redirect ") == {"collect", "redirect"}
    assert redirect_sweep._parse_action_set(["inspect", "nope"]) == {"inspect"}
    assert redirect_sweep._parse_action_set(None) == set(redirect_sweep.DEFAULT_SHADOW_ACTIONS)
    assert redirect_sweep._env_flag({"X": "YES"}, "X")
    assert not redirect_sweep._env_flag({}, "X")
    assert redirect_sweep._selected_backend(" auto ") is None
    assert redirect_sweep._selected_backend(" codex ") == "codex"
    assert redirect_sweep._int_or_none("") is None
    assert redirect_sweep._int_or_none("9") == 9
    assert redirect_sweep._int_or_none("x") is None
    assert redirect_sweep._list_value(None) == []
    assert redirect_sweep._list_value([" a ", "", 2]) == [" a ", "2"]
    assert redirect_sweep._list_value("a, b") == ["a", "b"]
    assert redirect_sweep._list_value(3) == ["3"]
    assert redirect_sweep._sha_text(None) is None
    assert redirect_sweep._sha_text("x")

    criteria = redirect_sweep._acceptance_criteria_for_report(
        {
            "target": "o/r#1",
            "lane": "opener",
            "task_type": "test",
            "agent": "codex",
            "expected_paths": ["tests/"],
            "policy_decision": {"action": "inspect", "confidence": "high", "reason": "evidence"},
            "hints": [{"kind": "auth", "detail": "expired"}],
            "drift": {"findings": [{"kind": "unexpected_paths", "detail": "outside"}]},
        }
    )
    assert "Baseline policy action: inspect" in criteria
    assert "Root-cause hints:" in criteria
    assert "Drift findings:" in criteria


def test_claim_inputs_and_experiment_recording_cover_skip_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handoff = tmp_path / "handoff"
    log = handoff / "dispatch-logs" / "o__r_1.codex.log"
    log.parent.mkdir(parents=True)
    log.write_text("stalled\n")
    monkeypatch.setattr(redirect_sweep, "HANDOFF", handoff)

    wt = tmp_path / "wt"
    wt.mkdir()
    monkeypatch.setattr(redirect_sweep.provision, "worktree_path", lambda *_a, **_k: wt)
    inputs = redirect_sweep._claim_watch_inputs(
        "o/r#1", {"agent": "codex", "lane": "opener", "expected_paths": "tests/,src/"}
    )
    assert inputs["log"].endswith("o__r_1.codex.log")
    assert inputs["worktree"] == str(wt)
    assert inputs["expected_paths"] == ["tests/", "src/"]

    monkeypatch.setattr(
        redirect_sweep.provision,
        "worktree_path",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    broken = redirect_sweep._claim_watch_inputs("o/r#2", {"agent": "codex", "lane": "opener"})
    assert broken["worktree"] == ""

    exp = tmp_path / "exp"
    (exp / "done").mkdir(parents=True)
    (exp / "done" / "e1:ok.json").write_text(json.dumps({"rc": 0}))
    (exp / "diff-ok.patch").write_text("diff\n")
    (exp / "done" / "e1:fail.json").write_text(json.dumps({"rc": 2}))
    (exp / "diff-fail.patch").write_text("")
    # failed arm without a log is skipped; failed arm with a log is classified
    (exp / "fail.log").write_text("auth failed\n")
    (exp / "done" / "e1:nolog.json").write_text("{bad")
    calls: list[dict] = []

    def fake_classify(**kwargs):
        return {
            "target": kwargs["target"],
            "agent": kwargs["agent"],
            "state": "exited",
            "recommended_action": "redirect",
            "policy_decision": {"action": "redirect"},
        }

    monkeypatch.setattr(
        redirect_shadow,
        "record_redirect",
        lambda *a, **kwargs: calls.append(kwargs)
        or {"entry": {"entry_id": "e", "source": kwargs["source"], "valid_proposal": True}},
    )
    result = redirect_sweep.record_experiment_candidates(
        "e1",
        {"repo": "o/r", "task_type": "implement", "agents": ["ok", "fail", "nolog"]},
        exp,
        corpus_path=tmp_path / "corpus.jsonl",
        classify_fn=fake_classify,
        max_records=2,
        backend="auto",
    )
    assert result["experiment_actionable"] == 1
    assert result["recorded_count"] == 1
    assert any(
        row["agent"] == "ok" and not row["failed"] for row in result["experiment_arms_checked"]
    )


def test_sweep_and_recording_are_advisory_and_count_every_skip(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(redirect_sweep, "_capability_heartbeat", lambda: None)
    monkeypatch.setattr(
        redirect_sweep.claims,
        "active_claims",
        lambda **_: {
            "o/r#0": {"agent": "missing"},
            "o/r#1": {"agent": "codex", "pid": 7, "lane": "opener", "task_type": "test"},
            "o/r#2": {"agent": "extra", "pid": 8},
        },
    )
    monkeypatch.setattr(
        redirect_sweep.watch,
        "classify_lane",
        lambda **kwargs: {
            "state": "stalled",
            "recommended_action": "redirect",
            "policy_decision": {"action": "redirect"},
            "target": kwargs["target"],
            "agent": kwargs["agent"],
        },
    )
    sweep = redirect_sweep.sweep(max_reports=2, now=100)
    assert sweep["unwatchable_count"] == 1
    assert sweep["watched_count"] == sweep["actionable_count"] == 1
    assert sweep["truncated_targets"] == ["o/r#2"]
    assert sweep["mutates_state"] is False

    calls: list[dict] = []

    def record_redirect(report, *args, **kwargs):
        if report.get("target") == "o/r#boom":
            raise RuntimeError("record failed")
        calls.append(kwargs)
        return {"entry": {"entry_id": "one", "source": kwargs["source"], "valid_proposal": True}}

    monkeypatch.setattr(redirect_shadow, "record_redirect", record_redirect)
    report = {
        "actionable": [
            {"target": "o/r#1", "agent": "codex", "policy_decision": {"action": "redirect"}},
            {"target": "o/r#1", "agent": "codex", "policy_decision": {"action": "redirect"}},
            {"target": "o/r#boom", "agent": "x", "policy_decision": {"action": "redirect"}},
            {"target": "o/r#3", "agent": "x", "policy_decision": {"action": "collect"}},
            {"target": "o/r#4", "agent": "y", "policy_decision": {"action": "redirect"}},
            {"target": "o/r#5", "agent": "z", "policy_decision": {"action": "redirect"}},
        ]
    }
    with pytest.raises(ValueError, match="requires dispatch or proposal_json"):
        redirect_sweep.record_shadow_candidates(report, corpus_path=tmp_path / "shadow.jsonl")

    corpus = tmp_path / "shadow.jsonl"
    # Pre-seed a fingerprint so the second identical redirect is skipped as a duplicate.
    first_fp = redirect_sweep._stable_sweep_fingerprint(report["actionable"][0])
    corpus.write_text(
        json.dumps({"kind": "redirect_proposal", "ts": 10**12, "sweep_fingerprint": first_fp})
        + "\n"
    )
    recorded = redirect_sweep.record_shadow_candidates(
        report,
        corpus_path=corpus,
        actions={"redirect"},
        max_records=1,
        proposal_json={"action": "redirect"},
        dedupe_hours=0,
    )
    reasons = {row["reason"] for row in recorded["skipped"]}
    assert "duplicate-within-window" in reasons
    assert any("o/r#boom" in json.dumps(err) for err in recorded["errors"])
    assert recorded["recorded_count"] == 1
    assert any(row.get("reason") == "action-not-selected" for row in recorded["skipped"])
    assert any(row.get("reason") == "max-records-reached" for row in recorded["skipped"])
    written = redirect_sweep.write_report({"ok": True}, tmp_path / "out" / "report.json")
    assert Path(written["path"]).is_file() and written["bytes"] > 0


def test_doctor_and_watch_helpers_handle_bad_inputs_without_live_access(
    tmp_path: Path, monkeypatch
) -> None:
    assert redirect_sweep._read_report_status(tmp_path / "missing.json", now=100)["exists"] is False
    report_path = tmp_path / "redirect-sweep.json"
    report_path.write_text("{bad")
    assert redirect_sweep._read_report_status(report_path, now=100)["load_error"]
    report_path.write_text(json.dumps({"generated_at": "unknown"}))
    assert redirect_sweep._read_report_status(report_path, now=100)["age_seconds"] is None

    monkeypatch.setattr(redirect_shadow, "summarize", lambda _: {"corpus": "x", "n": 2})
    monkeypatch.setattr(
        redirect_sweep,
        "sweep",
        lambda **_: {
            "active_claim_count": 1,
            "watched_count": 1,
            "actionable_count": 1,
            "action_counts": {},
            "state_counts": {},
        },
    )
    missing_orch = redirect_sweep.doctor(
        env={},
        state_dir=tmp_path,
        corpus_path=tmp_path / "corpus.jsonl",
        orchestrate_path=tmp_path / "no-such-orchestrate.sh",
        now=100,
    )
    assert "not wired" in missing_orch["recommendation"]

    orchestrate = tmp_path / "orchestrate.sh"
    orchestrate.write_text("python3 redirect_sweep.py\nORCH_REDIRECT_SWEEP_RECORD_CORPUS\n")
    advisory = redirect_sweep.doctor(
        env={},
        state_dir=tmp_path,
        corpus_path=tmp_path / "corpus.jsonl",
        orchestrate_path=orchestrate,
        now=100,
    )
    assert "advisory mode" in advisory["recommendation"]

    enabled = redirect_sweep.doctor(
        env={"ORCH_REDIRECT_SWEEP_RECORD_CORPUS": "1"},
        state_dir=tmp_path,
        corpus_path=tmp_path / "corpus.jsonl",
        orchestrate_path=orchestrate,
        now=100,
    )
    assert "next cadence run can record" in enabled["recommendation"]

    monkeypatch.setattr(
        redirect_sweep,
        "sweep",
        lambda **_: {
            "active_claim_count": 1,
            "watched_count": 1,
            "actionable_count": 0,
            "action_counts": {},
            "state_counts": {},
        },
    )
    doctor = redirect_sweep.doctor(
        env={"ORCH_REDIRECT_SWEEP_RECORD_CORPUS": "1"},
        state_dir=tmp_path,
        corpus_path=tmp_path / "corpus.jsonl",
        orchestrate_path=orchestrate,
        now=100,
    )
    assert "no actionable" in doctor["recommendation"]
    assert "cadence_wired=True" in redirect_sweep.format_doctor(doctor)

    assert watch.tail_text("abcdef", 3) == "def"
    assert watch.tail_text("", 3) == ""
    assert watch.read_log_tail(None) == ""
    assert watch.read_log_tail(tmp_path / "missing") == ""
    blocked = tmp_path / "blocked.log"
    blocked.write_text("secret\n")
    real_path = watch.Path

    class ReadBoomPath(real_path):
        def read_bytes(self):
            raise OSError("no")

    monkeypatch.setattr(watch, "Path", ReadBoomPath)
    assert watch.read_log_tail(blocked) == ""
    monkeypatch.setattr(watch, "Path", real_path)

    assert watch.pid_alive(None) is None and watch.pid_alive(0) is False

    def kill_perm(pid, sig):
        raise PermissionError("denied")

    monkeypatch.setattr(os, "kill", kill_perm)
    assert watch.pid_alive(123) is True

    def kill_missing(pid, sig):
        raise ProcessLookupError("gone")

    monkeypatch.setattr(os, "kill", kill_missing)
    assert watch.pid_alive(123) is False
    assert watch.file_age_seconds(None, now=100) is None
    assert watch.file_age_seconds(tmp_path / "missing", now=100) is None
    aged = tmp_path / "aged.txt"
    aged.write_text("x")

    class StatBoomPath(real_path):
        def exists(self):
            return True

        def stat(self):
            raise OSError("stat")

    monkeypatch.setattr(watch, "Path", StatBoomPath)
    assert watch.file_age_seconds(aged, now=100) is None
    monkeypatch.setattr(watch, "Path", real_path)

    assert watch._status_paths("xx\n M one.py\nR  old.py -> new.py\n?? two.py") == [
        "one.py",
        "new.py",
        "two.py",
    ]
    assert watch._under_expected("src/a.py", ["src"])
    assert watch._under_expected("anything", ["."])
    assert not watch._under_expected("src/a.py", ["", "tests"])
    assert watch.recommended_action("stalled", [{"kind": "auth"}]) == "redirect"
    assert watch.recommended_action("mystery", []) == "inspect"
    assert watch.hints_from_log("") == []
    assert watch.hints_from_log("HTTP 401 Unauthorized")[0]["kind"] == "auth"
    assert (
        watch._action_for_signals("stalled", [], {"pid_alive": None, "has_worktree_changes": True})
        == "collect"
    )
    assert watch.a2a_state("stalled", "Proceed? [y/N]") == "input-required"
    assert watch.a2a_state("stalled", "401 unauthorized") == "auth-required"
    assert watch.a2a_state("exited", "") == "completed"
    assert watch.a2a_state("weird", "") == "unknown"

    def fake_git(_worktree, args):
        if args[0] == "status":
            return SimpleNamespace(returncode=0, stdout=" M src/a.py\n", stderr="")
        if args[:2] == ["diff", "--stat"] or args[:2] == ["diff", "--name-only"]:
            if "bad" in (args[2:] or []):
                return SimpleNamespace(returncode=1, stdout="", stderr="diff failed")
            return SimpleNamespace(returncode=0, stdout="src/a.py\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="src/a.py\n", stderr="")

    monkeypatch.setattr(watch, "_is_git_worktree", lambda _: True)
    monkeypatch.setattr(watch, "_git", fake_git)
    errors: list[str] = []
    assert watch.changed_paths(None, "main", errors) == []
    assert watch.changed_paths(tmp_path, "main", errors) == ["src/a.py"]
    assert watch.changed_paths(tmp_path, "bad", errors)
    assert errors
    git = watch.git_signals(tmp_path, "bad", errors=[])
    assert git["git_available"] is True
    drift = watch.drift_signals(tmp_path, "main", expected_paths=["tests"], task_type="testgen")
    assert drift["severity"] == "medium" and drift["findings"][0]["kind"] == "unexpected_paths"
    assert watch.worktree_activity_age(None) is None
    assert watch.worktree_activity_age(tmp_path / "no-dir") is None
    (tmp_path / "src").mkdir(exist_ok=True)
    (tmp_path / "src" / "a.py").write_text("x")
    assert watch.worktree_activity_age(tmp_path, now=10**12) is not None


def test_classify_format_and_cli_paths_stay_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.setattr(watch, "_capability_heartbeat", lambda *a, **k: None)
    missing = watch.classify_lane(credit=False)
    assert missing["state"] == "missing"

    log = tmp_path / "agent.log"
    log.write_text("HTTP 401 Unauthorized\n")
    monkeypatch.setattr(watch, "pid_alive", lambda pid: False)
    monkeypatch.setattr(
        watch,
        "git_signals",
        lambda *a, **k: {
            "status_short": " M src/a.py",
            "uncommitted_diff_stat": "1 file",
            "committed_diff_stat": "(none)",
            "has_worktree_changes": True,
            "git_available": True,
        },
    )
    monkeypatch.setattr(
        watch,
        "drift_signals",
        lambda *a, **k: {
            "severity": "medium",
            "changed_path_count": 1,
            "findings": [{"kind": "unexpected_paths", "detail": "outside"}],
        },
    )
    exited = watch.classify_lane(
        agent="codex",
        target="o/r#1",
        pid=9,
        log=log,
        worktree=tmp_path,
        base_ref="main",
        expected_paths=["tests"],
        credit=False,
    )
    assert exited["state"] == "exited"
    human = watch.format_human(exited)
    assert "state=exited" in human and "hint[auth]" in human and "drift[" in human

    monkeypatch.setattr(watch, "pid_alive", lambda pid: True)
    monkeypatch.setattr(watch, "file_age_seconds", lambda *a, **k: 5.0)
    monkeypatch.setattr(watch, "worktree_activity_age", lambda *a, **k: 5.0)
    progress = watch.classify_lane(
        pid=1, log=log, worktree=tmp_path, stale_seconds=600, credit=False
    )
    assert progress["state"] == "progress"

    monkeypatch.setattr(watch, "file_age_seconds", lambda *a, **k: 900.0)
    monkeypatch.setattr(watch, "worktree_activity_age", lambda *a, **k: 900.0)
    monkeypatch.setattr(
        watch,
        "git_signals",
        lambda *a, **k: {
            "status_short": "",
            "uncommitted_diff_stat": "(none)",
            "committed_diff_stat": "(none)",
            "has_worktree_changes": False,
            "git_available": True,
        },
    )
    stalled = watch.classify_lane(
        pid=1, log=log, worktree=tmp_path, stale_seconds=600, credit=False
    )
    assert stalled["state"] == "stalled"

    monkeypatch.setattr(watch, "pid_alive", lambda pid: None)
    monkeypatch.setattr(watch, "file_age_seconds", lambda *a, **k: None)
    monkeypatch.setattr(watch, "worktree_activity_age", lambda *a, **k: None)
    running = watch.classify_lane(log=log, worktree=tmp_path, stale_seconds=600, credit=False)
    assert running["state"] == "running"

    state = {"n": 0}

    def plan_fn(report):
        state["n"] += 1
        if state["n"] == 1:
            raise redirect_plan.MissingRedirectAgent("no agent")
        return {"action": "inspect", "steps": [], "requires_confirmation": True}

    monkeypatch.setattr(watch.redirect_plan, "plan", plan_fn)
    monkeypatch.setattr(
        watch.redirect_policy,
        "decide",
        lambda report, history=None: {"action": "redirect", "confidence": "high", "reason": "x"},
    )
    finished = watch._finish_report(
        {
            "state": "stalled",
            "log_tail": "",
            "recommended_action": "redirect",
            "policy_decision": {"action": "redirect"},
        },
        None,
    )
    assert finished["redirect_plan"]["blocked_action"] == "redirect"
    assert finished["redirect_plan"]["blocker"] == "no agent"

    history = tmp_path / "history.json"
    history.write_text("[]")
    monkeypatch.setattr(
        watch,
        "classify_lane",
        lambda **kwargs: {
            "agent": kwargs.get("agent"),
            "state": "missing",
            "recommended_action": "inspect",
            "signals": {"pid_alive": None, "log_age_seconds": 1, "worktree_age_seconds": 2},
            "drift": {"severity": "none"},
            "policy_decision": {"action": "inspect", "confidence": "low", "reason": "x"},
            "redirect_plan": {"action": "inspect", "steps": [1], "requires_confirmation": False},
            "hints": [{"kind": "network", "detail": "down"}],
            "errors": ["e"],
            "log_tail": "tail",
        },
    )
    assert watch.main(["--agent", "codex", "--json", "--attempt-history-json", str(history)]) == 0
    assert '"state": "missing"' in capsys.readouterr().out
    assert watch.main(["--attempt-history-json", str(tmp_path / "nope.json")]) == 1
    assert watch.main(["--agent", "codex"]) == 0
    assert "state=missing" in capsys.readouterr().out


def test_redirect_sweep_main_cli_paths_are_advisory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.setattr(redirect_sweep, "_capability_heartbeat", lambda *a, **k: None)
    monkeypatch.setattr(
        redirect_sweep,
        "doctor",
        lambda **_: {
            "cadence": {
                "redirect_sweep_step_present": True,
                "record_corpus_env_enabled": False,
            },
            "autonomous_redirect_enabled": False,
            "live_sweep": {"active_claim_count": 0, "watched_count": 0, "actionable_count": 0},
            "shadow_corpus": {
                "proposals": 0,
                "valid_proposals": 0,
                "linked_proposals": 0,
                "ready_for_supervised_apply": False,
            },
            "recommendation": "ok",
        },
    )
    assert redirect_sweep.main(["--doctor"]) == 0
    assert "redirect_sweep doctor:" in capsys.readouterr().out

    with pytest.raises(SystemExit):
        redirect_sweep.main(["--doctor", "--record-corpus"])
    with pytest.raises(SystemExit):
        redirect_sweep.main(["--record-corpus"])
    with pytest.raises(SystemExit):
        redirect_sweep.main(
            ["--record-corpus", "--dispatch-redirect-agent", "--proposal-json", "{}"]
        )

    proposal = tmp_path / "proposal.json"
    proposal.write_text(json.dumps({"action": "redirect"}))
    ac = tmp_path / "ac.txt"
    ac.write_text("accept\n")
    out = tmp_path / "report.json"
    monkeypatch.setattr(
        redirect_sweep,
        "sweep",
        lambda **_: {
            "active_claim_count": 1,
            "watched_count": 1,
            "actionable_count": 1,
            "unwatchable_count": 0,
            "actionable": [
                {
                    "target": "o/r#1",
                    "agent": "codex",
                    "state": "stalled",
                    "policy_decision": {"action": "redirect"},
                }
            ],
        },
    )
    monkeypatch.setattr(
        redirect_sweep,
        "record_shadow_candidates",
        lambda report, **kwargs: {
            "recorded_count": 1,
            "attempted_count": 1,
            "corpus": str(kwargs["corpus_path"]),
        },
    )
    rc = redirect_sweep.main(
        [
            "--record-corpus",
            "--proposal-json",
            str(proposal),
            "--corpus",
            str(tmp_path / "corpus.jsonl"),
            "--ac-file",
            str(ac),
            "--write",
            str(out),
            "--json",
        ]
    )
    assert rc == 0
    assert out.is_file()
    assert "shadow_recording" in capsys.readouterr().out

    assert redirect_sweep.main([]) == 0
    human = capsys.readouterr().out
    assert "redirect_sweep: active=1" in human and "o/r#1" in human


@pytest.mark.parametrize("timestamp", [float("inf"), float("-inf")])
def test_nonfinite_corpus_timestamp_is_skipped(tmp_path: Path, timestamp: float) -> None:
    corpus = tmp_path / "nonfinite.jsonl"
    corpus.write_text(
        json.dumps({"kind": "redirect_proposal", "ts": timestamp, "sweep_fingerprint": "bad"})
        + "\n"
        + json.dumps({"kind": "redirect_proposal", "ts": 100, "sweep_fingerprint": "valid"})
        + "\n"
    )
    assert redirect_sweep._existing_sweep_fingerprints(corpus, since_ts=50) == {"valid"}


@pytest.mark.parametrize("timestamp", [float("inf"), float("-inf")])
def test_nonfinite_report_timestamp_has_unknown_age(tmp_path: Path, timestamp: float) -> None:
    report = tmp_path / "report.json"
    report.write_text(json.dumps({"generated_at": timestamp, "watched_count": 3}))
    status = redirect_sweep._read_report_status(report, now=100)
    assert status["exists"] is True
    assert status["age_seconds"] is None
    assert status["watched_count"] == 3
