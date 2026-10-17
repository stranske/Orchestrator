"""Assess network fallback uses a real command builder and private accounting."""

from __future__ import annotations

import json
import shlex
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import dispatcher
import switch_review


@pytest.fixture
def sandbox_offload(tmp_path, monkeypatch):
    monkeypatch.delenv("CODEX_SANDBOX", raising=False)
    monkeypatch.delenv("ORCH_OFFLOAD_DISABLED", raising=False)
    monkeypatch.setenv("ORCH_OFFLOAD_NETWORK_RETRIES", "0")
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ORCH_LOCAL_RUNTIME", str(tmp_path / "runtime"))
    monkeypatch.setenv(dispatcher.credential_redaction.SECRET_FILES_ENV, "")
    monkeypatch.setattr(dispatcher, "DISPATCH_LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(dispatcher.adapters, "LEDGER", tmp_path / "ledger.ndjson")
    monkeypatch.setattr(dispatcher, "_select_offload_profile", lambda *_: None)
    monkeypatch.setattr(dispatcher, "_offload_prompt", lambda prompt, *_: prompt)
    monkeypatch.setattr(dispatcher, "_capability_heartbeat", lambda *_args, **_kw: None)
    monkeypatch.setattr(dispatcher, "_auth_prelude", lambda *_: "")
    monkeypatch.setattr(dispatcher, "_agent_runtime_prelude", lambda *_: "")
    monkeypatch.setattr(dispatcher.feedback, "record_run", lambda *_args, **_kw: None)
    monkeypatch.setattr(dispatcher.feedback, "record_cost", lambda *_args, **_kw: None)
    monkeypatch.setattr(
        dispatcher.adapters, "can_report_cli_identity", lambda *_: (False, "fixture")
    )
    (tmp_path / "workspace").mkdir()
    calls = []
    network = {"returncode": 1}

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        if "sandbox" in argv:
            return SimpleNamespace(returncode=network["returncode"], stdout="", stderr="")
        assert argv[:2] == ["bash", "-lc"], argv
        if "--json" not in shlex.split(argv[2]):
            return SimpleNamespace(
                returncode=0, stderr="", stdout="Assessment complete: GitHub is reachable."
            )
        return SimpleNamespace(
            returncode=0,
            stderr="",
            stdout=json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "type": "agent_message",
                        "text": "Assessment complete: GitHub is reachable.",
                    },
                }
            ),
        )

    monkeypatch.setattr(dispatcher.subprocess, "run", run)
    return tmp_path, calls, network


def test_sandbox_probe_failure_falls_back_with_preamble_and_ledger(sandbox_offload, capsys):
    root, calls, _ = sandbox_offload
    result = dispatcher.offload(
        "codex", "Read GitHub; do not write files.", mode="assess", cwd=str(root / "workspace")
    )
    assert result["exit"] == 0, result
    assert len(calls) == 2
    assert calls[0][0][1:3] == ["sandbox", "-c"]
    assert 'sandbox_mode="read-only"' in calls[0][0]
    assert calls[0][1]["timeout"] == 5
    assert calls[0][1]["cwd"] == root / "workspace"
    assert result["output"] == "Assessment complete: GitHub is reachable."
    argv = shlex.split(calls[1][0][2])
    assert argv[argv.index("--sandbox") + 1] == "workspace-write"
    assert "--json" not in argv
    assert argv[-1].startswith(dispatcher.ASSESS_NO_WRITE_PREAMBLE)
    assert "Read GitHub; do not write files." in argv[-1]
    assert dispatcher.ASSESS_SANDBOX_FALLBACK_LOG in capsys.readouterr().err
    assert dispatcher.ASSESS_SANDBOX_FALLBACK_LOG in Path(result["log"]).read_text()
    rows = [json.loads(line) for line in (root / "ledger.ndjson").read_text().splitlines()]
    assert [row["event"] for row in rows] == ["start", "complete"]
    assert all(row["sandbox_fallback"] is True and row["mode"] == "assess" for row in rows)


def test_sandbox_probe_success_leaves_assess_unchanged(sandbox_offload, capsys):
    root, calls, network = sandbox_offload
    network["returncode"] = 0
    result = dispatcher.offload("codex", "Read GitHub.", mode="assess", cwd=str(root / "workspace"))
    assert result["exit"] == 0, result
    argv = shlex.split(calls[-1][0][2])
    assert argv[argv.index("--sandbox") + 1] == "read-only"
    assert argv[-1] == "Read GitHub."
    assert dispatcher.ASSESS_SANDBOX_FALLBACK_LOG not in capsys.readouterr().err
    rows = [json.loads(line) for line in (root / "ledger.ndjson").read_text().splitlines()]
    assert all(row["sandbox_fallback"] is False for row in rows)


def test_sandbox_non_assess_does_not_probe(sandbox_offload):
    root, calls, _ = sandbox_offload
    result = dispatcher.offload("codex", "Read GitHub.", mode="mid", cwd=str(root / "workspace"))
    assert result["exit"] == 0, result
    assert len(calls) == 1
    assert dispatcher.ASSESS_NO_WRITE_PREAMBLE not in calls[0][0][2]


@pytest.mark.parametrize("failure", [subprocess.TimeoutExpired("probe", 5), OSError("missing CLI")])
def test_sandbox_probe_unavailable_is_false(monkeypatch, failure):
    def run(*_args, **_kwargs):
        raise failure

    monkeypatch.setattr(dispatcher.subprocess, "run", run)
    assert dispatcher._sandbox_has_network("assess") is False


def test_sandbox_report_counts_each_run_once_and_network_incomplete(tmp_path):
    ledger = tmp_path / "ledger.ndjson"
    log = tmp_path / "dispatch.log"
    log.write_text("curl: (6) Could not resolve host: api.github.com\n")
    common = {"ts": 1000000, "agent": "codex", "mode": "assess"}
    rows = [
        {**common, "run_id": "offload:1", "event": "start", "sandbox_fallback": True},
        {**common, "run_id": "offload:1", "event": "complete", "sandbox_fallback": True, "exit": 0},
        {
            **common,
            "run_id": "offload:2",
            "event": "complete",
            "error": "OFFLOAD_INCOMPLETE",
            "log_file": str(log),
        },
        {**common, "run_id": "offload:3", "event": "start"},
        {**common, "run_id": "offload:old", "event": "start", "ts": 1},
        {**common, "run_id": "offload:other", "event": "start", "mode": "mid"},
    ]
    ledger.write_text("\n".join(map(json.dumps, rows)))
    report = switch_review.offload_sandbox_network(now=1000001, ledger_path=ledger)
    assert report == {
        "window_days": 7,
        "assess_runs": 3,
        "sandbox_fallbacks": 1,
        "network_incomplete": 1,
        "unknown_runs": 1,
        "status": "measured",
    }
    rendered = "\n".join(switch_review.format_offload_sandbox_network(report))
    assert "sandbox_fallback=true=1" in rendered
    assert "network-caused OFFLOAD_INCOMPLETE=1" in rendered


def test_sandbox_report_missing_ledger_is_unmeasured(tmp_path):
    report = switch_review.offload_sandbox_network(ledger_path=tmp_path / "missing")
    assert report["status"] == "unmeasured"
    assert report["network_incomplete"] is None
    assert "unmeasured" in "\n".join(switch_review.format_offload_sandbox_network(report))


def test_sandbox_profile_model_is_preserved_during_fallback(sandbox_offload, monkeypatch):
    root, calls, _ = sandbox_offload
    monkeypatch.setattr(dispatcher.adapters, "profile_codex_binary", lambda: "/fixture/codex")
    result = dispatcher.offload(
        "codex",
        "Read GitHub.",
        mode="assess",
        cwd=str(root / "workspace"),
        profile_id="codex-6.1-sol-medium",
    )
    assert result["exit"] == 0, result
    assert calls[0][0][0] == "/fixture/codex"
    argv = shlex.split(calls[-1][0][2])
    assert "/fixture/codex" in argv
    profile = dispatcher.execution_profiles.get_profile("codex-6.1-sol-medium")
    assert argv[argv.index("--model") + 1] == profile["requested_model"]
    assert argv[argv.index("--sandbox") + 1] == "workspace-write"


def test_sandbox_report_unknown_and_malformed_evidence_stays_visible(tmp_path):
    ledger = tmp_path / "ledger.ndjson"
    ledger.write_text(
        json.dumps(
            {
                "ts": 100,
                "agent": "codex",
                "mode": "assess",
                "run_id": "offload:1",
                "event": "complete",
                "error": "agent reported OFFLOAD_INCOMPLETE",
            }
        )
        + "\nnot json\n"
    )
    report = switch_review.offload_sandbox_network(now=101, ledger_path=ledger)
    assert report["status"] == "partial"
    assert report["assess_runs"] == 1
    assert report["unknown_runs"] == 1
    assert report["network_incomplete"] == 0
