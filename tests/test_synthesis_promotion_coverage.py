from __future__ import annotations

import json
import subprocess

import synthesis_promotion as promotion


def _completed(
    returncode: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def test_completion_probe_requires_a_success_marker_and_new_head(tmp_path, monkeypatch) -> None:
    log = tmp_path / "logs" / "synthesis.log"
    marker = log.parent / "done" / "run-1.json"
    marker.parent.mkdir(parents=True)
    marker.write_text(json.dumps({"rc": 0, "receipt": "complete"}))
    state = {
        "synthesis": {
            "current_run_id": "run-1",
            "log": str(log),
            "worktree": str(tmp_path),
            "launch_head": "base",
        }
    }

    monkeypatch.setattr(promotion, "_git", lambda *_: _completed(stdout="new-head\n"))
    assert promotion.default_completion_probe(state) == {
        "status": "complete",
        "commit": "new-head",
        "marker_hash": promotion._hash({"rc": 0, "receipt": "complete"}),
    }

    monkeypatch.setattr(promotion, "_git", lambda *_: _completed(stdout="base\n"))
    assert (
        promotion.default_completion_probe(state)["reason"]
        == "synthesis completed without a new commit"
    )

    marker.write_text("not json")
    invalid_result = promotion.default_completion_probe(state)
    assert invalid_result["status"] == "interrupted"
    assert invalid_result["reason"].startswith("invalid completion marker:")

    marker.write_text(json.dumps({"rc": 9}))
    assert promotion.default_completion_probe(state) == {
        "status": "interrupted",
        "reason": "synthesis marker rc=9",
        "rc": 9,
    }


def test_run_gate_never_runs_non_allowlisted_command(monkeypatch, tmp_path) -> None:
    calls: list[object] = []

    def unexpected_run(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("subprocess must not run an unallowlisted gate")

    monkeypatch.setattr(promotion.subprocess, "run", unexpected_run)
    rejected = promotion._run_gate(["sh", "-c", "echo unsafe"], tmp_path)
    assert rejected == {
        "ok": False,
        "reason": "repo gate command is not allowlisted",
        "argv": ["sh", "-c", "echo unsafe"],
    }
    assert calls == []

    def successful_run(argv, **kwargs):
        calls.append((argv, kwargs["cwd"]))
        return _completed(0, "ok", "")

    monkeypatch.setattr(promotion.subprocess, "run", successful_run)
    accepted = promotion._run_gate(["pytest", "tests/test_unit.py"], tmp_path)
    assert accepted["ok"] is True
    assert accepted["argv"] == ["pytest", "tests/test_unit.py"]
    assert calls == [(["pytest", "tests/test_unit.py"], str(tmp_path))]
    assert promotion._safe_command(["npm", "--prefix", "../escape", "test"]) is False
    assert promotion._safe_command(["npm", "--prefix", "frontend", "run", "test"]) is True


def test_verify_synthesis_records_unallowlisted_local_command_without_spawning(
    tmp_path, monkeypatch
) -> None:
    (tmp_path / "meta.json").write_text(json.dumps({"base_sha": "base"}))
    (tmp_path / "spec.md").write_text("deliberate-break proof required")
    (tmp_path / "promotion-verification.json").write_text(
        json.dumps(
            {
                "local_verify": {"test_cmd": "sh -c unsafe", "test_paths": ["tests/x.py"]},
                "repo_gates": [["git", "diff", "--check"]],
            }
        )
    )
    state = {"synthesis": {"worktree": str(tmp_path)}}
    git_results = iter([_completed(stdout="safe.py\n"), _completed(stdout="")])
    monkeypatch.setattr(promotion, "_git", lambda *_: next(git_results))
    calls: list[object] = []
    monkeypatch.setattr(
        promotion,
        "_run_gate",
        lambda argv, *_: calls.append(argv) or {"ok": True, "argv": argv},
    )

    def fail_if_local_verify_runs(*_args, **_kwargs):
        raise AssertionError("rejected command must not reach local verification")

    monkeypatch.setattr(promotion.local_verify, "verify", fail_if_local_verify_runs)

    result = promotion.verify_synthesis(state, tmp_path)

    assert calls == [["git", "diff", "--check"]]
    assert result["passed"] is False
    assert result["evidence"]["local_verify"] == {
        "ok": False,
        "verdict": "ERROR",
        "node_verdict": None,
        "hollow_nodes": [],
        "result_hash": promotion._hash(
            {
                "verdict": "ERROR",
                "ok": False,
                "reason": "local_verify test command is not allowlisted",
            }
        ),
        "test_paths": ["tests/x.py"],
        "test_cmd": "sh -c unsafe",
    }
    assert result["evidence"]["deliberate_break_status"] == "FAIL"
