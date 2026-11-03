"""Hermetic regression coverage for issue 813's ranked coverage targets.

The command entry points below are deliberately exercised with every live boundary
replaced.  These paths are easy to miss in unit suites, but a malformed CLI result
must not turn into a network call or a write to the shared runtime.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import capability_compiler
import experiment_recovery
import exploration_evidence_plan
import gh_capacity
import keepalive_shadow


@pytest.fixture(autouse=True)
def isolated_runtime_without_processes(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("ORCH_LOCAL_RUNTIME", str(tmp_path))
    started = []

    def forbid_process(*args, **kwargs):
        started.append((args, kwargs))
        pytest.fail("ranked CLI coverage must never start a subprocess or agent")

    monkeypatch.setattr(subprocess, "run", forbid_process)
    monkeypatch.setattr(subprocess, "Popen", forbid_process)
    yield
    assert started == []


def test_capability_compiler_reference_cli_requires_only_the_injected_workflow(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    ledger = tmp_path / "ledger.json"
    monkeypatch.setattr(
        capability_compiler,
        "run_reference_workflow",
        lambda *, ledger_path: {"ledger": str(ledger_path), "status": "shadow"},
    )

    assert capability_compiler.main(["reference", "--ledger", str(ledger)]) == 0
    assert json.loads(capsys.readouterr().out) == {"ledger": str(ledger), "status": "shadow"}


def test_gh_capacity_cli_writes_only_the_supplied_temp_snapshot(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    out = tmp_path / "capacity.json"
    monkeypatch.setattr(gh_capacity, "HANDOFF", tmp_path / "handoff")
    monkeypatch.setattr(gh_capacity, "OUT", out)
    monkeypatch.setattr(gh_capacity, "build", lambda: {"resources": {"core": {"state": "ok"}}})

    assert gh_capacity.main([]) == 0
    assert json.loads(out.read_text()) == {"resources": {"core": {"state": "ok"}}}
    assert json.loads(capsys.readouterr().out) == {"resources": {"core": {"state": "ok"}}}


def test_keepalive_shadow_cli_uses_temp_corpus_and_no_live_github(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    corpus = tmp_path / "corpus.jsonl"
    decision = {"target": "o/r#7", "shadow_action": "wait"}
    monkeypatch.setattr(keepalive_shadow, "gather_signals", lambda target: {"target": target})
    monkeypatch.setattr(keepalive_shadow, "shadow_decide", lambda signals: decision)
    recorded = []

    def fake_record(entry, path):
        recorded.append((entry, path))
        return {"recorded": True, "corpus": str(path)}

    monkeypatch.setattr(keepalive_shadow, "record", fake_record)

    assert keepalive_shadow.main(["--shadow", "o/r#7", "--corpus", str(corpus)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result == {**decision, "recorded": True, "corpus": str(corpus)}
    assert recorded == [(decision, corpus)]


def test_experiment_recovery_cli_apply_uses_only_injected_rows(monkeypatch, capsys) -> None:
    row = {"exp_id": "tick-1-o-r-7", "repo": "o/r"}
    monkeypatch.setattr(experiment_recovery, "plan", lambda **_: {"experiments": 1, "counts": {}})
    second_row = {"exp_id": "tick-2-o-r-8", "repo": "o/r"}
    monkeypatch.setattr(experiment_recovery, "unevaluated_experiments", lambda: [row, second_row])
    restored = []

    def fake_restore(received, **kwargs):
        restored.append((received, kwargs))
        return {"exp_id": received["exp_id"], "applied": kwargs["apply"]}

    monkeypatch.setattr(experiment_recovery, "restore_experiment", fake_restore)

    assert experiment_recovery.main(["--apply", "--limit", "1", "--json"]) == 0
    assert len(restored) == 1
    assert restored[0][0] == row
    assert restored[0][1]["apply"] is True
    assert json.loads(capsys.readouterr().out) == {
        "applied": 1,
        "counts": {},
        "experiments": 1,
        "results": [{"applied": True, "exp_id": "tick-1-o-r-7"}],
    }


def test_exploration_plan_cli_clamps_inputs_and_never_reads_live_state(monkeypatch, capsys) -> None:
    seen = {}

    def fake_build_plan(**kwargs):
        seen.update(kwargs)
        return {"stage": "stage_1", "read_only": True}

    monkeypatch.setattr(exploration_evidence_plan, "build_plan", fake_build_plan)
    assert exploration_evidence_plan.main(["--json", "--trials", "0", "--window-days", "0"]) == 0
    assert seen["trials"] == seen["window_days"] == 1
    assert json.loads(capsys.readouterr().out) == {"read_only": True, "stage": "stage_1"}
