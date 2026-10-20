"""Issue #839: hermetic regression coverage for redirect sweep and lane watching."""

from __future__ import annotations

import json
import socket
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import redirect_shadow
import redirect_sweep
import watch


@pytest.fixture(autouse=True)
def isolated_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Tests must neither inspect live claims nor start a process."""
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("ORCH_LOCAL_RUNTIME", str(tmp_path))
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
                "not json",
                json.dumps({"kind": "other"}),
                json.dumps(
                    {"kind": "redirect_proposal", "ts": "not-a-time", "sweep_fingerprint": "bad"}
                ),
                json.dumps({"kind": "redirect_proposal", "ts": 100, "sweep_fingerprint": "good"}),
            ]
        )
        + "\n"
    )
    assert redirect_sweep._existing_sweep_fingerprints(corpus, since_ts=50) == {"good"}
    assert redirect_sweep._parse_action_set(" collect,invalid,redirect ") == {"collect", "redirect"}
    assert redirect_sweep._parse_action_set(None) == set(redirect_sweep.DEFAULT_SHADOW_ACTIONS)
    assert redirect_sweep._env_flag({"X": "YES"}, "X")
    assert not redirect_sweep._env_flag({}, "X")
    assert redirect_sweep._selected_backend(" auto ") is None
    assert redirect_sweep._selected_backend(" codex ") == "codex"

    criteria = redirect_sweep._acceptance_criteria_for_report(
        {
            "target": "o/r#1",
            "lane": "opener",
            "task_type": "test",
            "agent": "codex",
            "expected_paths": ["tests/"],
            "policy_decision": {"action": "inspect", "confidence": "high", "reason": "evidence"},
            "hints": [{"kind": "auth", "detail": "expired"}],
        }
    )
    assert "Baseline policy action: inspect" in criteria and "Root-cause hints:" in criteria


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
    monkeypatch.setattr(
        redirect_shadow,
        "record_redirect",
        lambda *args, **kwargs: calls.append(kwargs)
        or {"entry": {"entry_id": "one", "source": kwargs["source"], "valid_proposal": True}},
    )
    report = {
        "actionable": sweep["actionable"]
        + [{"target": "o/r#3", "agent": "x", "policy_decision": {"action": "collect"}}]
    }
    recorded = redirect_sweep.record_shadow_candidates(
        report,
        corpus_path=tmp_path / "shadow.jsonl",
        actions={"redirect"},
        max_records=1,
        proposal_json={"action": "redirect"},
    )
    assert recorded["recorded_count"] == recorded["attempted_count"] == 1
    assert recorded["skipped"][-1]["reason"] == "action-not-selected"
    assert calls[0]["dispatch"] is False and calls[0]["source"] == "redirect-sweep-replay"


def test_doctor_and_watch_helpers_handle_bad_inputs_without_live_access(
    tmp_path: Path, monkeypatch
) -> None:
    report_path = tmp_path / "redirect-sweep.json"
    report_path.write_text("{bad")
    assert redirect_sweep._read_report_status(report_path, now=100)["load_error"]
    report_path.write_text(json.dumps({"generated_at": "unknown"}))
    assert redirect_sweep._read_report_status(report_path, now=100)["age_seconds"] is None

    orchestrate = tmp_path / "orchestrate.sh"
    orchestrate.write_text("python3 redirect_sweep.py\nORCH_REDIRECT_SWEEP_RECORD_CORPUS\n")
    monkeypatch.setattr(redirect_shadow, "summarize", lambda _: {"corpus": "x", "n": 2})
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
    assert watch.read_log_tail(tmp_path / "missing") == ""
    assert watch.pid_alive(None) is None and watch.pid_alive(0) is False
    assert watch.file_age_seconds(tmp_path / "missing", now=100) is None
    assert watch._status_paths(" M one.py\nR  old.py -> new.py\n?? two.py") == [
        "one.py",
        "new.py",
        "two.py",
    ]
    assert watch._under_expected("src/a.py", ["src"])
    assert not watch._under_expected("src/a.py", ["tests"])
    assert watch.recommended_action("stalled", [{"kind": "auth"}]) == "redirect"
    assert (
        watch._action_for_signals("stalled", [], {"pid_alive": None, "has_worktree_changes": True})
        == "collect"
    )
    assert watch.a2a_state("stalled", "Proceed? [y/N]") == "input-required"

    def fake_git(_worktree, args):
        text = " M src/a.py\n" if args[0] == "status" else "src/a.py\n"
        return SimpleNamespace(returncode=0, stdout=text, stderr="")

    monkeypatch.setattr(watch, "_is_git_worktree", lambda _: True)
    monkeypatch.setattr(watch, "_git", fake_git)
    errors: list[str] = []
    assert watch.changed_paths(tmp_path, "main", errors) == ["src/a.py"]
    drift = watch.drift_signals(tmp_path, "main", expected_paths=["tests"], task_type="testgen")
    assert drift["severity"] == "medium" and drift["findings"][0]["kind"] == "unexpected_paths"
