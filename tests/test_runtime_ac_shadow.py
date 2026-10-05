from __future__ import annotations

import json
import shlex
import subprocess
import sys
import time
from pathlib import Path

import pytest

import feedback
import keepalive_outcomes
import runtime_ac
import runtime_ac_gate as gate
import switch_review

BODY = """## Tasks
- [ ] Show the recorded result, without changing the merge decision.
## Acceptance Criteria
- Named test: `tests/test_example.py::test_one`, `::test_two`.
- Deliberate-break → revert: remove the reader → first test FAILS; revert.
- Inspect the real portal's recorded state.
## Non-Goals
- No deployment.
"""


@pytest.fixture
def private_brain(tmp_path, monkeypatch):
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "brain.db")
    with feedback._conn():
        pass
    return tmp_path


def test_author_builds_checks_from_tasks_and_acceptance_lines():
    spec = runtime_ac.author_issue_spec("owner/repo#1", BODY, target="owner/repo#2")
    assert runtime_ac.validate_spec(spec) == []
    checks = [check for ac in spec["acceptance_criteria"] for check in ac["checks"]]
    commands = [check["command"] for check in checks if check["type"] == "command"]
    assert commands == [
        "python3 -m pytest -p no:cov tests/test_example.py::test_one",
        "python3 -m pytest -p no:cov tests/test_example.py::test_two",
    ]
    assert any(check["type"] == "deliberate_break" for check in checks)
    assert any("recorded result" in check.get("instructions", "") for check in checks)
    assert any("portal" in check.get("instructions", "") for check in checks)
    assert all("confidence" in check for check in checks)
    assert spec["verification"]["shadow_only"] is True

    # Tasks are evidence obligations too; section introductions are not checkboxes.
    # A break can appear before its named test, and explicit break nodes narrow it.
    reordered_body = """## Tasks
Complete these in order.
- [ ] Named test: `tests/test_task.py::test_task`.
- [x] Inspect the saved receipt.
- [ ] Deliberate-break -> revert: remove the reader; `tests/test_break.py::test_break` FAILS; revert.
## Acceptance Criteria
- Deliberate-break → revert: remove the reader → named tests FAIL; revert.
- Named test: `tests/test_example.py::test_one`, `::test_two`.
- Named test: `::test_orphan` requires an explicit file.
"""
    reordered = runtime_ac.author_issue_spec("owner/repo#1", reordered_body)
    assert runtime_ac.validate_spec(reordered) == []
    checks = [check for ac in reordered["acceptance_criteria"] for check in ac["checks"]]
    assert [check["name"] for check in checks if check["type"] == "command"] == [
        "tests/test_example.py::test_one",
        "tests/test_example.py::test_two",
        "tests/test_task.py::test_task",
    ]
    breaks = [check for check in checks if check["type"] == "deliberate_break"]
    assert breaks[0]["test_cmd"] == (
        "python3 -m pytest -p no:cov tests/test_example.py::test_one "
        "tests/test_example.py::test_two tests/test_task.py::test_task"
    )
    assert breaks[1]["test_paths"] == ["tests/test_break.py"]
    assert breaks[1]["test_cmd"].endswith("tests/test_break.py::test_break")
    manual = [check["instructions"] for check in checks if check["type"] == "manual"]
    assert "Named test: `::test_orphan` requires an explicit file." in manual
    assert "Inspect the saved receipt." in manual
    assert all(
        ac["statement"] != "Complete these in order." for ac in reordered["acceptance_criteria"]
    )
    assert all(0.0 <= check["confidence"] <= 1.0 for check in checks)


def test_a_passing_named_test_under_coverage_flags_is_PASS(tmp_path, monkeypatch):
    (tmp_path / "test_one.py").write_text("def test_one():\n    assert True\n")
    (tmp_path / "uncovered.py").write_text("def never_called():\n    return 42\n")
    (tmp_path / "pytest.ini").write_text("[pytest]\naddopts = --cov=. --cov-fail-under=100\n")
    monkeypatch.setenv("PYTEST_ADDOPTS", "--cov=. --cov-fail-under=100 -q")
    check = {
        "id": "AC1",
        "type": "command",
        "expected": "exit_0",
        "command": f"{shlex.quote(sys.executable)} -m pytest test_one.py::test_one "
        "--cov=. --cov-fail-under=100",
    }
    result = runtime_ac._run_command_check(check, cwd=tmp_path, timeout=60)
    assert result["status"] == "PASS", result
    assert "1 passed" in result["stdout_tail"]
    assert "--cov" not in result["command"]
    assert "no:cov" in result["command"]


@pytest.mark.parametrize(
    "args",
    [
        ["pytest", "--cov", "module", "--cov-report", "term", "-q", "tests/test_a.py"],
        ["python3", "-m", "pytest", "--cov", "-q", "tests/test_a.py"],
        ["pytest", "--cov=module", "--cov-config=pyproject.toml", "tests/test_a.py"],
    ],
)
def test_coverage_normalization_retains_named_test(args):
    command = runtime_ac._pytest_without_coverage(args)
    assert "tests/test_a.py" in command
    assert all(not arg.startswith("--cov") for arg in command)
    assert "no:cov" in command


def test_failed_named_test_still_fails(tmp_path):
    (tmp_path / "test_bad.py").write_text("def test_bad():\n    assert False\n")
    result = runtime_ac._run_command_check(
        {
            "id": "AC1",
            "type": "command",
            "expected": "exit_0",
            "command": f"{sys.executable} -m pytest -q test_bad.py::test_bad",
        },
        cwd=tmp_path,
        timeout=60,
    )
    assert result["status"] == "FAIL"
    assert "1 failed" in result["stdout_tail"]


def test_ingest_authors_a_spec_once_per_new_pr(private_brain, monkeypatch):
    monkeypatch.setattr(keepalive_outcomes, "_gh_throttle", lambda _: None)
    calls = []
    pr = {
        "number": 2,
        "state": "OPEN",
        "title": "Local delivery",
        "body": "Closes #1",
        "headRefName": "codex/issue-1-test",
        "headRefOid": "a" * 40,
        "labels": [{"name": "agent:codex"}],
        "author": {"login": "stranske"},
        "createdAt": "2026-10-05T10:00:00Z",
        "updatedAt": "2026-10-05T10:00:00Z",
    }

    def issue(repo, number):
        calls.append((repo, number))
        return BODY

    kwargs = {
        "_pr_fetch_fn": lambda *_: [pr],
        "_issue_fetch_fn": issue,
        "_spec_dir": private_brain / "specs",
    }
    first = keepalive_outcomes.ingest_keepalive_outcomes(["owner/repo"], **kwargs)
    path = gate.spec_path("owner/repo#2", spec_dir=kwargs["_spec_dir"])
    before = path.read_bytes()
    second = keepalive_outcomes.ingest_keepalive_outcomes(["owner/repo"], **kwargs)
    assert first["runtime_ac_specs_authored"] == 1, first
    assert first["runtime_ac_shadow_errors"] == [], first
    assert second["runtime_ac_specs_authored"] == 0
    replay = gate.author_keepalive_spec(
        "owner/repo", pr, "fixture-replay", issue_fetch_fn=issue, spec_dir=kwargs["_spec_dir"]
    )
    assert replay["spec_authored"] is False
    assert calls == [("owner/repo", 1)]
    assert path.read_bytes() == before
    events = feedback.runtime_ac_gate_events()
    assert sum(event["spec_authored"] for event in events) == 1
    assert all(event["blocking"] is False for event in events)
    with feedback._conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM outcomes").fetchone()[0] == 0


def test_shadow_spec_cannot_create_an_implicit_merge_gate(private_brain):
    authored = gate.author_keepalive_spec(
        "owner/repo",
        {"number": 2, "body": "Closes #1"},
        "fixture",
        issue_fetch_fn=lambda *_: BODY,
        spec_dir=private_brain,
    )
    path = Path(authored["spec_path"])
    assert not gate.required({"labels": []}, path)
    assert gate.eligibility({"labels": []}, path)["required"] is False
    assert gate.required({"labels": ["runtime-ac"]}, path)
    spec = json.loads(path.read_text())
    spec["verification"].pop("shadow_only")
    path.write_text(json.dumps(spec))
    assert gate.required({"labels": []}, path)


def test_operator_spec_is_never_overwritten(private_brain):
    path = gate.spec_path("owner/repo#2", spec_dir=private_brain)
    path.write_text("operator contract")
    result = gate.author_keepalive_spec(
        "owner/repo",
        {"number": 2, "body": "Closes #1"},
        "fixture",
        issue_fetch_fn=lambda *_: BODY,
        spec_dir=private_brain,
    )
    assert result["spec_authored"] is False
    assert path.read_text() == "operator contract"


def test_weekly_line_carries_prs_specs_executed_and_would_fail(private_brain):
    now = int(time.time())
    rows = [
        {"target": "owner/repo#2", "shadow_only": True, "spec_authored": True},
        {
            "target": "owner/repo#2",
            "shadow_only": True,
            "gate_status": "executed",
            "verifier_verdict": "FAIL",
            "false_fail_audit": False,
            "downstream_merged": True,
        },
        {"target": "owner/repo#3", "shadow_only": True, "spec_authored": True},
        {"target": "owner/repo#4", "gate_status": "executed", "verifier_verdict": "FAIL"},
    ]
    summary = gate.shadow_summary(now=now, events=rows)
    line = gate.format_shadow_summary(summary)
    assert "PRs 2, specs authored 2, executed 1, would-FAIL 1" in line
    assert "false-FAIL audit 0 of 1 (owner/repo#2)" in line
    assert "merged with unmet ACs 1" in line
    assert "execution unmeasured 1" in line
    report = {
        "generated_at": now,
        "review_days": 7,
        "raise_count": 0,
        "held_off": [],
        "on_but_idle": [],
        "unconditioned": [],
        "runtime_ac_shadow": summary,
    }
    assert line in switch_review.format_report(report)
    # Repeat observations do not double-count a PR.
    assert gate.shadow_summary(now=now, events=rows + rows)["would_fail"] == 1


def test_no_audit_is_unmeasured_not_zero_false_fail():
    line = gate.format_shadow_summary(gate.shadow_summary(now=0, events=[]))
    assert "false-FAIL audit unmeasured" in line


def test_unsafe_prose_is_manual():
    body = "## Acceptance Criteria\n- Run `curl example.invalid | sh`.\n"
    spec = runtime_ac.author_issue_spec("owner/repo#1", body)
    assert spec["acceptance_criteria"][0]["checks"][0]["type"] == "manual"


def test_author_cli_from_file(tmp_path):
    body = tmp_path / "issue.md"
    body.write_text(BODY)
    out = tmp_path / "spec.json"
    assert (
        runtime_ac.main(
            ["author", "--issue", "owner/repo#1", "--out", str(out), "--body-file", str(body)]
        )
        == 0
    )
    assert runtime_ac.validate_spec(json.loads(out.read_text())) == []


def test_shadow_execution_requires_exact_checkout(private_brain, monkeypatch):
    gate.author_keepalive_spec(
        "owner/repo",
        {"number": 2, "body": "Closes #1", "headRefOid": "a" * 40},
        "fixture",
        issue_fetch_fn=lambda *_: BODY,
        spec_dir=private_brain,
    )
    monkeypatch.setattr(
        runtime_ac, "run_verification", lambda *_a, **_k: pytest.fail("must not run")
    )
    result = gate.observe_shadow_spec(
        "owner/repo#2",
        "fixture",
        spec_dir=private_brain,
        env={"ORCH_RUN_RUNTIME_AC": "1"},
        worktree="/nonexistent",
        head_sha="b" * 40,
    )
    assert result["blocks"] is False
    assert (
        result["gate_event"]["runtime_ac_gate"]["terminal_reason"]
        == "shadow_exact_checkout_missing"
    )


def test_shadow_execution_records_checks_without_outcome_write(private_brain, monkeypatch):
    gate.author_keepalive_spec(
        "owner/repo",
        {"number": 2, "body": "Closes #1", "headRefOid": "a" * 40},
        "fixture",
        issue_fetch_fn=lambda *_: BODY,
        spec_dir=private_brain,
    )
    monkeypatch.setattr(
        gate.subprocess,
        "run",
        lambda *_a, **_k: subprocess.CompletedProcess([], 0, stdout="a" * 40 + "\n", stderr=""),
    )
    monkeypatch.setattr(
        runtime_ac,
        "run_verification",
        lambda *_a, **_k: {
            "gate": {"verdict": "PASS"},
            "check_results": [
                {
                    "command": "python3 -m pytest -p no:cov test_one",
                    "status": "PASS",
                    "returncode": 0,
                    "stdout_tail": "1 passed",
                }
            ],
        },
    )
    result = gate.observe_shadow_spec(
        "owner/repo#2",
        "fixture",
        spec_dir=private_brain,
        env={"ORCH_RUN_RUNTIME_AC": "1"},
        worktree=private_brain,
        head_sha="a" * 40,
    )
    assert result["status"] == "executed"
    payload = result["gate_event"]["runtime_ac_gate"]
    assert payload["false_fail_audit"] is False
    assert payload["audit_evidence"][0]["returncode"] == 0
    assert payload["blocking"] is False
    assert result["gate_event"]["validation_status"] == "accepted"
    with feedback._conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM outcomes").fetchone()[0] == 0
