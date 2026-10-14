from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

import adversarial
import feedback
import keepalive_outcomes
import merge_guard
import provision
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
- [ ] Deliberate break: remove X; `tests/test_punct.py::test_punct` fails; revert it.
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
    assert breaks[2]["test_cmd"].endswith("tests/test_punct.py::test_punct")
    manual = [check["instructions"] for check in checks if check["type"] == "manual"]
    assert "Named test: `::test_orphan` requires an explicit file." in manual
    assert "Inspect the saved receipt." in manual
    assert all(
        ac["statement"] != "Complete these in order." for ac in reordered["acceptance_criteria"]
    )
    assert all(0.0 <= check["confidence"] <= 1.0 for check in checks)

    # Parameterized names must select the exact case, including IDs with spaces,
    # through both command and deliberate-break authoring.
    parameter_node = "tests/test_example.py::TestExample::test_one[case with spaces]"
    parameter_spec = runtime_ac.author_issue_spec(
        "owner/repo#1",
        f"## Tasks\n- [ ] Named test: `{parameter_node}`, `::test_two[other]`.\n"
        "## Acceptance Criteria\n- Deliberate-break → revert: remove reader; tests FAIL; revert.\n",
    )
    assert runtime_ac.validate_spec(parameter_spec) == []
    parameter_checks = parameter_spec["acceptance_criteria"][1]["checks"]
    expected_nodes = [parameter_node, "tests/test_example.py::test_two[other]"]
    assert [shlex.split(check["command"])[-1] for check in parameter_checks] == expected_nodes
    break_check = parameter_spec["acceptance_criteria"][0]["checks"][0]
    assert break_check["type"] == "deliberate_break"
    assert shlex.split(break_check["test_cmd"])[-2:] == expected_nodes
    assert break_check["test_paths"] == ["tests/test_example.py"]
    for suffix in ("[unterminated", "[bad;case]"):
        line = f"Named test: `tests/test_example.py::test_one{suffix}`."
        manual_spec = runtime_ac.author_issue_spec(
            "owner/repo#1", f"## Acceptance Criteria\n- {line}\n"
        )
        manual = manual_spec["acceptance_criteria"][0]["checks"][0]
        assert manual["type"] == "manual"
        assert manual["instructions"] == line
        assert manual["confidence"] == 0.0


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


@pytest.mark.parametrize(
    "pr_body,issue_repo",
    [
        ("Closes #1", "owner/repo"),
        ("Fixes owner/repo#1", "owner/repo"),
        ("Resolves other/project#1", "other/project"),
        ("Closes https://github.com/owner/repo/issues/1", "owner/repo"),
        ("Fixes https://github.com/other/project/issues/1", "other/project"),
    ],
)
def test_ingest_authors_a_spec_once_per_new_pr(private_brain, monkeypatch, pr_body, issue_repo):
    monkeypatch.setattr(keepalive_outcomes, "_gh_throttle", lambda _: None)
    monkeypatch.setenv("ORCH_RUN_RUNTIME_AC", "0")
    calls = []
    pr = {
        "number": 2,
        "state": "OPEN",
        "title": "Local delivery",
        "body": pr_body,
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
    preview = keepalive_outcomes.ingest_keepalive_outcomes(["owner/repo"], dry_run=True, **kwargs)
    path = gate.spec_path("owner/repo#2", spec_dir=kwargs["_spec_dir"])
    assert preview["runtime_ac_specs_authored"] == 0
    assert calls == []
    assert not path.exists()
    assert feedback.runtime_ac_gate_events() == []
    first = keepalive_outcomes.ingest_keepalive_outcomes(["owner/repo"], **kwargs)
    before = path.read_bytes()
    spec = json.loads(before)
    assert spec["verification"]["target"] == "owner/repo#2"
    assert spec["verification"]["repo"] == "owner/repo"
    assert spec["verification"]["source_issue"] == f"{issue_repo}#1"
    assert spec["verification"]["shadow_only"] is True
    assert spec["verification"]["observed_head_sha"] == pr["headRefOid"]
    second = keepalive_outcomes.ingest_keepalive_outcomes(["owner/repo"], **kwargs)
    assert first["runtime_ac_specs_authored"] == 1, first
    assert first["runtime_ac_shadow_errors"] == [], first
    assert second["runtime_ac_specs_authored"] == 0
    replay = gate.author_keepalive_spec(
        "owner/repo", pr, "fixture-replay", issue_fetch_fn=issue, spec_dir=kwargs["_spec_dir"]
    )
    assert replay["spec_authored"] is False
    assert calls == [(issue_repo, 1)]
    assert path.read_bytes() == before
    events = feedback.runtime_ac_gate_events()
    assert sum(event["spec_authored"] for event in events) == 1
    assert all(event["blocking"] is False for event in events)
    assert all(event["shadow_only"] is True for event in events)
    assert os.environ["ORCH_RUN_RUNTIME_AC"] == "0"
    with feedback._conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM outcomes").fetchone()[0] == 0

    # Exercise the unattended defaults too: ingest fetches the linked issue itself,
    # rather than requiring the caller to supply an issue-body callback.
    next_pr = {**pr, "number": 3}
    issue_commands = []

    def fetch_json(args, **_kwargs):
        issue_commands.append(args)
        assert args[-8:] == [
            "gh",
            "issue",
            "view",
            "1",
            "--repo",
            issue_repo,
            "--json",
            "body",
        ]
        return {"body": BODY}

    monkeypatch.setattr(keepalive_outcomes, "_fetch_prs", lambda *_: [next_pr])
    monkeypatch.setattr(keepalive_outcomes, "_fetch_dispatch_times", lambda *_: None)
    monkeypatch.setattr(keepalive_outcomes, "_run_json", fetch_json)
    live = keepalive_outcomes.ingest_keepalive_outcomes(
        ["owner/repo"], _spec_dir=kwargs["_spec_dir"]
    )
    repeated = keepalive_outcomes.ingest_keepalive_outcomes(
        ["owner/repo"], _spec_dir=kwargs["_spec_dir"]
    )
    assert live["runtime_ac_specs_authored"] == 1, live
    assert live["runtime_ac_shadow_errors"] == [], live
    assert repeated["runtime_ac_specs_authored"] == 0
    assert len(issue_commands) == 1
    next_path = gate.spec_path("owner/repo#3", spec_dir=kwargs["_spec_dir"])
    assert json.loads(next_path.read_text())["verification"]["source_issue"] == f"{issue_repo}#1"
    authored_events = [
        event for event in feedback.runtime_ac_gate_events() if event["spec_authored"]
    ]
    assert {event["target"] for event in authored_events} == {"owner/repo#2", "owner/repo#3"}
    assert all(event["blocking"] is False and event["shadow_only"] for event in authored_events)
    assert os.environ["ORCH_RUN_RUNTIME_AC"] == "0"


def test_shadow_spec_cannot_create_an_implicit_merge_gate(private_brain, monkeypatch):
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
    monkeypatch.setattr(
        runtime_ac,
        "run_verification",
        lambda *_a, **_k: pytest.fail("shadow spec executed by merge"),
    )
    monkeypatch.setattr(
        runtime_ac, "record_gate_verdict", lambda *_a, **_k: pytest.fail("shadow verdict consumed")
    )
    for execution_flag in ("0", "1"):
        result = merge_guard.evaluate_merge_gate(
            "owner/repo#2",
            dry_run=False,
            env={"ORCH_RUN_RUNTIME_AC": execution_flag},
            spec_dir=private_brain,
            metadata_fn=lambda _: {"state": "OPEN", "is_draft": False, "labels": []},
        )
        assert result["gate"] is None
        assert result["blocked"] is False
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


def test_competing_spec_is_never_overwritten(private_brain, monkeypatch):
    path = gate.spec_path("owner/repo#2", spec_dir=private_brain)
    publish = gate.os.link

    def competing_publish(staged, destination):
        path.write_bytes(b"competing operator contract")
        publish(staged, destination)

    monkeypatch.setattr(gate.os, "link", competing_publish)
    result = gate.author_keepalive_spec(
        "owner/repo",
        {"number": 2, "body": "Closes #1"},
        "fixture",
        issue_fetch_fn=lambda *_: BODY,
        spec_dir=private_brain,
    )
    assert result["status"] == "existing"
    assert result["spec_authored"] is False
    assert path.read_bytes() == b"competing operator contract"
    assert set(private_brain.iterdir()) == {private_brain / "brain.db", path}
    assert feedback.runtime_ac_gate_events() == []

    # A failure before publication must also clean its staged file. The next attempt
    # can author the spec, and only that successful attempt records an authored event.
    failed_path = gate.spec_path("owner/repo#3", spec_dir=private_brain)
    with monkeypatch.context() as failure:

        def fail_flush(_fd):
            raise OSError("staged spec flush failed")

        failure.setattr(gate.os, "fsync", fail_flush)
        result = gate.author_keepalive_spec(
            "owner/repo",
            {"number": 3, "body": "Closes #1"},
            "fixture",
            issue_fetch_fn=lambda *_: BODY,
            spec_dir=private_brain,
        )
    assert result["status"] == "error"
    assert result["spec_authored"] is False
    assert not failed_path.exists()
    assert set(private_brain.iterdir()) == {private_brain / "brain.db", path}
    assert feedback.runtime_ac_gate_events() == []
    monkeypatch.setattr(gate.os, "link", publish)
    retry = gate.author_keepalive_spec(
        "owner/repo",
        {"number": 3, "body": "Closes #1"},
        "fixture-retry",
        issue_fetch_fn=lambda *_: BODY,
        spec_dir=private_brain,
    )
    assert retry["spec_authored"] is True
    assert runtime_ac.validate_spec(json.loads(failed_path.read_text())) == []
    assert len(feedback.runtime_ac_gate_events()) == 1


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
    # Reasons describe recorded observations, not event volume or the last
    # ingest's budget. A PR that executed remains measured after a planned retry.
    planned = [
        {
            "target": target,
            "shadow_only": True,
            "gate_status": "planned",
            "terminal_reason": reason,
        }
        for target, reason in [
            ("owner/repo#2", "shadow_execution_budget"),
            ("owner/repo#3", "shadow_checkout_failed"),
            ("owner/repo#3", "shadow_execution_budget"),
            ("owner/repo#5", "shadow_execution_disabled"),
            ("owner/repo#6", None),
        ]
    ]
    reasons = gate.shadow_summary(now=now, events=rows + planned + planned)
    assert reasons["executed"] == 1
    assert reasons["execution_unmeasured"] == 3
    assert reasons["execution_unmeasured_reasons"] == {
        "shadow_checkout_failed": 1,
        "shadow_execution_budget": 1,
        "shadow_execution_disabled": 1,
        "unrecorded": 1,
    }
    reason_line = gate.format_shadow_summary(reasons)
    assert "recorded reasons (PRs per reason): shadow_checkout_failed 1" in reason_line
    assert "shadow_execution_budget 1, shadow_execution_disabled 1, unrecorded 1" in reason_line
    assert reason_line in switch_review.format_report({**report, "runtime_ac_shadow": reasons})
    assert "recorded reasons" not in line
    assert "recorded reasons" not in gate.format_shadow_summary(
        {key: value for key, value in summary.items() if key != "execution_unmeasured_reasons"}
    )


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


@pytest.mark.parametrize("without_runtime_context", [False, True])
def test_shadow_execution_records_checks_without_outcome_write(
    private_brain, monkeypatch, without_runtime_context
):
    gate.author_keepalive_spec(
        "owner/repo",
        {"number": 2, "body": "Closes #1", "headRefOid": "a" * 40},
        "fixture",
        issue_fetch_fn=lambda *_: BODY,
        spec_dir=private_brain,
    )
    if without_runtime_context:
        path = gate.spec_path("owner/repo#2", spec_dir=private_brain)
        spec = json.loads(path.read_text())
        spec.pop("runtime_context")
        path.write_text(json.dumps(spec))
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


def test_existing_ingested_run_refreshes_shadow_observation(private_brain, monkeypatch):
    monkeypatch.setattr(keepalive_outcomes, "_gh_throttle", lambda _: None)
    monkeypatch.setenv("ORCH_RUN_RUNTIME_AC", "0")
    pr = {
        "number": 20,
        "state": "OPEN",
        "title": "Delivery",
        "body": "Closes #1",
        "headRefName": "codex/issue-1",
        "headRefOid": "a" * 40,
        "labels": [{"name": "agent:codex"}],
        "author": {"login": "stranske"},
        "createdAt": "2026-10-05T10:00:00Z",
        "updatedAt": "2026-10-05T10:00:00Z",
    }
    calls = []
    original = gate.observe_shadow_spec

    def observe(*args, **kwargs):
        calls.append(kwargs["head_sha"])
        return original(*args, **kwargs)

    monkeypatch.setattr(gate, "observe_shadow_spec", observe)
    kwargs = {
        "_pr_fetch_fn": lambda *_: [pr],
        "_issue_fetch_fn": lambda *_: BODY,
        "_spec_dir": private_brain / "specs",
    }
    first = keepalive_outcomes.ingest_keepalive_outcomes(["owner/repo"], **kwargs)
    path = gate.spec_path("owner/repo#20", spec_dir=kwargs["_spec_dir"])
    before = path.read_bytes()
    second = keepalive_outcomes.ingest_keepalive_outcomes(["owner/repo"], **kwargs)
    assert first["runtime_ac_specs_authored"] == 1
    assert second["runtime_ac_specs_authored"] == 0
    assert calls == ["a" * 40, "a" * 40]
    assert path.read_bytes() == before
    keepalive_outcomes.ingest_keepalive_outcomes(["owner/repo"], dry_run=True, **kwargs)
    assert len(calls) == 2


def test_existing_shadow_spec_observes_new_exact_head(private_brain, monkeypatch):
    authored = gate.author_keepalive_spec(
        "owner/repo",
        {"number": 21, "body": "Closes #1", "headRefOid": "a" * 40},
        "fixture",
        issue_fetch_fn=lambda *_: BODY,
        spec_dir=private_brain,
    )
    path = Path(authored["spec_path"])
    before = path.read_bytes()
    current = "b" * 40
    monkeypatch.setattr(
        gate.subprocess,
        "run",
        lambda *_a, **_k: subprocess.CompletedProcess([], 0, stdout=current + "\n", stderr=""),
    )
    monkeypatch.setattr(
        runtime_ac,
        "run_verification",
        lambda *_a, **_k: {"gate": {"verdict": "PASS"}, "check_results": []},
    )
    result = gate.observe_shadow_spec(
        "owner/repo#21",
        "fixture",
        spec_dir=private_brain,
        env={"ORCH_RUN_RUNTIME_AC": "1"},
        worktree=private_brain,
        head_sha=current,
    )
    assert result["status"] == "executed"
    assert result["blocks"] is False
    assert path.read_bytes() == before
    # A caller-supplied SHA still cannot substitute for the actual checkout.
    current = "c" * 40
    stale = gate.observe_shadow_spec(
        "owner/repo#21",
        "fixture",
        spec_dir=private_brain,
        env={"ORCH_RUN_RUNTIME_AC": "1"},
        worktree=private_brain,
        head_sha="b" * 40,
    )
    assert stale["status"] != "executed"


def test_mixed_named_nodes_preserve_rejected_obligation_as_manual():
    body = (
        "## Acceptance Criteria\n"
        "- Named tests: `tests/test_safe.py::test_ok` and "
        "`tests/test_safe.py::test_case[bad;marker]`.\n"
    )
    spec = runtime_ac.author_issue_spec("owner/repo#1", body)
    criterion = spec["acceptance_criteria"][0]
    checks = criterion["checks"]
    commands = [check for check in checks if check["type"] == "command"]
    assert len(commands) == 1
    assert "test_ok" in commands[0]["command"]
    manual = [check for check in checks if check["type"] == "manual"]
    assert len(manual) == 1
    assert "test_case[bad;marker]" in manual[0]["instructions"]
    assert "manual_review" in criterion["evidence_required"]
    results = [{"id": commands[0]["id"], "status": "PASS", "exit_code": 0}]
    assert runtime_ac.evaluate_results(spec, {"check_results": results})["verdict"] != "PASS"


def test_class_qualified_shorthand_preserves_both_obligations():
    body = "## Acceptance Criteria\n- Named tests: `tests/test_example.py::TestExample::test_one[one]`, `::TestExample::test_two[two]`.\n"
    spec = runtime_ac.author_issue_spec("owner/repo#1", body)
    commands = [
        c["command"] for c in spec["acceptance_criteria"][0]["checks"] if c["type"] == "command"
    ]
    assert len(commands) == 2
    assert "TestExample::test_one[one]" in commands[0]
    assert "TestExample::test_two[two]" in commands[1]


def test_unterminated_named_node_cannot_disappear_into_pass():
    body = "## Acceptance Criteria\n- Named tests: `tests/test_safe.py::test_ok` and `tests/test_safe.py::test_case[unterminated`.\n"
    spec = runtime_ac.author_issue_spec("owner/repo#1", body)
    criterion = spec["acceptance_criteria"][0]
    commands = [c for c in criterion["checks"] if c["type"] == "command"]
    assert len(commands) == 1
    assert any(c["type"] == "manual" for c in criterion["checks"])
    results = [{"id": commands[0]["id"], "status": "PASS", "exit_code": 0}]
    assert runtime_ac.evaluate_results(spec, {"check_results": results})["verdict"] != "PASS"


def _shadow_git_env() -> dict[str, str]:
    env = os.environ.copy()
    for key in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_COMMON_DIR",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    ):
        env.pop(key, None)
    return env


def _shadow_git_repo(path):
    path.mkdir()
    env = _shadow_git_env()
    for args in (
        ["init", "-q"],
        [
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "--allow-empty",
            "-qm",
            "initial",
        ],
    ):
        subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True, env=env)
    return subprocess.check_output(
        ["git", "-C", str(path), "rev-parse", "HEAD"], text=True, env=env
    ).strip()


@pytest.mark.parametrize(
    "storage_override",
    ["GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES"],
)
def test_shadow_git_repo_isolates_caller_storage(tmp_path, monkeypatch, storage_override):
    # Establish caller storage without any runner-supplied repository overrides.
    for key in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_COMMON_DIR",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    ):
        monkeypatch.delenv(key, raising=False)
    clean_env = os.environ.copy()
    caller = tmp_path / "caller"
    _shadow_git_repo(caller)
    (caller / "caller-only.txt").write_text("staged caller contents\n")
    subprocess.run(
        ["git", "-C", str(caller), "add", "caller-only.txt"],
        check=True,
        capture_output=True,
        env=clean_env,
    )
    caller_git = caller / ".git"
    before = {
        path.relative_to(caller_git): path.read_bytes()
        for path in caller_git.rglob("*")
        if path.is_file()
    }
    storage = caller_git / ("index" if storage_override == "GIT_INDEX_FILE" else "objects")
    monkeypatch.setenv(storage_override, str(storage))
    for key, value in (
        ("GIT_DIR", str(caller_git)),
        ("GIT_WORK_TREE", str(caller)),
        ("GIT_COMMON_DIR", str(caller_git)),
    ):
        monkeypatch.setenv(key, value)

    checkout = tmp_path / "checkout"
    head = _shadow_git_repo(checkout)

    assert os.environ[storage_override] == str(storage)
    assert os.environ["GIT_DIR"] == str(caller_git)
    assert os.environ["GIT_WORK_TREE"] == str(caller)
    assert os.environ["GIT_COMMON_DIR"] == str(caller_git)
    after = {
        path.relative_to(caller_git): path.read_bytes()
        for path in caller_git.rglob("*")
        if path.is_file()
    }
    assert after == before
    # Use the independent clean environment so a borrowed object cannot hide a failure.
    assert (
        subprocess.check_output(
            ["git", "-C", str(checkout), "rev-parse", "HEAD"], text=True, env=clean_env
        ).strip()
        == head
    )
    assert (
        subprocess.check_output(
            ["git", "-C", str(checkout), "ls-tree", "-r", "--name-only", "HEAD"],
            text=True,
            env=clean_env,
        )
        == ""
    )
    subprocess.run(
        ["git", "-C", str(checkout), "fsck", "--full"],
        check=True,
        capture_output=True,
        env=clean_env,
    )


def _shadow_ingest_pr(number, head):
    return {
        "number": number,
        "state": "OPEN",
        "title": "Runtime acceptance delivery",
        "body": "Closes #1",
        "headRefName": "codex/issue-1-shadow",
        "headRefOid": head,
        "labels": [{"name": "agent:codex"}],
        "author": {"login": "stranske"},
        "createdAt": "2026-10-05T10:00:00Z",
        "updatedAt": "2026-10-05T10:00:00Z",
    }


def test_ingest_executes_shadow_spec_on_exact_checkout(private_brain, monkeypatch):
    checkout = private_brain / "checkout"
    head = _shadow_git_repo(checkout)
    calls = []
    cleaned = []

    @contextmanager
    def provider(target, requested_head):
        calls.append((target, requested_head))
        try:
            yield str(checkout)
        finally:
            cleaned.append(target)

    def verify(spec, **kwargs):
        assert spec["runtime_context"]["worktree"] == str(checkout)
        assert kwargs["confirm_run"] is True
        return {"gate": {"verdict": "PASS"}, "check_results": []}

    monkeypatch.setattr(provision, "temporary_exact_head_worktree", provider, raising=False)
    monkeypatch.setattr(keepalive_outcomes, "_gh_throttle", lambda _: None)
    monkeypatch.setattr(runtime_ac, "run_verification", verify)
    monkeypatch.setenv("ORCH_RUN_RUNTIME_AC", "1")
    kwargs = {
        "_pr_fetch_fn": lambda *_: [_shadow_ingest_pr(20, head)],
        "_issue_fetch_fn": lambda *_: BODY,
        "_spec_dir": private_brain / "specs",
    }
    summary = keepalive_outcomes.ingest_keepalive_outcomes(["owner/repo"], **kwargs)
    observed = [
        row for row in feedback.runtime_ac_gate_events() if row["gate_status"] == "executed"
    ]
    assert len(observed) == 1, feedback.runtime_ac_gate_events()
    assert observed[0]["verifier_verdict"] == "PASS"
    assert observed[0]["observed_head_sha"] == head
    assert observed[0]["blocking"] is False and observed[0]["shadow_only"] is True
    assert calls == [("owner/repo#20", head)] and cleaned == ["owner/repo#20"]
    assert summary["runtime_ac_checkout_failures"] == 0
    assert summary["runtime_ac_shadow_errors"] == []
    # Existing runs/specs execute too; authoring stays once-only and outcomes stay untouched.
    before = gate.spec_path("owner/repo#20", spec_dir=kwargs["_spec_dir"]).read_bytes()
    repeated = keepalive_outcomes.ingest_keepalive_outcomes(["owner/repo"], **kwargs)
    assert repeated["runtime_ac_specs_authored"] == 0
    assert len(calls) == 2
    assert gate.spec_path("owner/repo#20", spec_dir=kwargs["_spec_dir"]).read_bytes() == before
    with feedback._conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM outcomes").fetchone()[0] == 0


def test_ingest_bounds_shadow_execution_across_repos(private_brain, monkeypatch):
    checkout = private_brain / "checkout"
    head = _shadow_git_repo(checkout)
    calls = []

    @contextmanager
    def provider(target, requested_head):
        assert requested_head == head
        calls.append(target)
        yield str(checkout)

    monkeypatch.setattr(provision, "temporary_exact_head_worktree", provider, raising=False)
    monkeypatch.setattr(keepalive_outcomes, "_gh_throttle", lambda _: None)
    monkeypatch.setattr(
        runtime_ac,
        "run_verification",
        lambda *_a, **_k: {"gate": {"verdict": "PASS"}, "check_results": []},
    )
    monkeypatch.setenv("ORCH_RUN_RUNTIME_AC", "1")
    keepalive_outcomes.ingest_keepalive_outcomes(
        ["owner/first", "owner/second"],
        _pr_fetch_fn=lambda *_: [_shadow_ingest_pr(n, head) for n in (20, 21)],
        _issue_fetch_fn=lambda *_: BODY,
        _spec_dir=private_brain / "specs",
    )
    assert len(calls) == keepalive_outcomes.MAX_RUNTIME_AC_SHADOW_EXECUTIONS == 3
    events = feedback.runtime_ac_gate_events()
    assert sum(row["gate_status"] == "executed" for row in events) == 3
    budget = [row for row in events if row["terminal_reason"] == "shadow_execution_budget"]
    assert len(budget) == 1 and budget[0]["gate_status"] == "planned"
    assert budget[0]["target"] == "owner/second#21"
    assert all(row["terminal_reason"] != "shadow_exact_checkout_missing" for row in events)


def test_ingest_counts_checkout_failures_and_bounds_failed_attempts(private_brain, monkeypatch):
    calls = []

    @contextmanager
    def provider(target, head):
        calls.append(target)
        raise RuntimeError("cannot fetch exact checkout commit")
        yield  # pragma: no cover

    monkeypatch.setattr(provision, "temporary_exact_head_worktree", provider, raising=False)
    monkeypatch.setattr(keepalive_outcomes, "_gh_throttle", lambda _: None)
    monkeypatch.setattr(
        runtime_ac, "run_verification", lambda *_a, **_k: pytest.fail("no checkout")
    )
    monkeypatch.setenv("ORCH_RUN_RUNTIME_AC", "1")
    summary = keepalive_outcomes.ingest_keepalive_outcomes(
        ["owner/repo"],
        _pr_fetch_fn=lambda *_: [_shadow_ingest_pr(n, "a" * 40) for n in range(20, 24)],
        _issue_fetch_fn=lambda *_: BODY,
        _spec_dir=private_brain / "specs",
    )
    assert len(calls) == 3
    assert summary["runtime_ac_checkout_failures"] == 3
    assert len(summary["runtime_ac_checkout_errors"]) == 3
    assert all(
        target in reason for target, reason in zip(calls, summary["runtime_ac_checkout_errors"])
    )
    events = feedback.runtime_ac_gate_events()
    assert sum(row["terminal_reason"] == "shadow_checkout_failed" for row in events) == 3
    assert sum(row["terminal_reason"] == "shadow_execution_budget" for row in events) == 1
    assert not any(row["terminal_reason"] == "shadow_exact_checkout_missing" for row in events)
    weekly = gate.shadow_summary(now=int(time.time()))
    assert weekly["executed"] == 0
    assert weekly["execution_unmeasured_reasons"] == {
        "shadow_checkout_failed": 3,
        "shadow_execution_budget": 1,
    }
    line = gate.format_shadow_summary(weekly)
    assert "executed 0" in line
    assert "shadow_checkout_failed 3, shadow_execution_budget 1" in line


@pytest.mark.parametrize("disabled,dry_run", [(True, False), (False, True)])
def test_ingest_never_provisions_when_disabled_or_dry(
    private_brain, monkeypatch, disabled, dry_run
):
    monkeypatch.setenv("ORCH_RUN_RUNTIME_AC", "0" if disabled else "1")
    monkeypatch.setattr(keepalive_outcomes, "_gh_throttle", lambda _: None)
    monkeypatch.setattr(
        provision,
        "temporary_exact_head_worktree",
        lambda *_a, **_k: pytest.fail("must not provision"),
        raising=False,
    )
    summary = keepalive_outcomes.ingest_keepalive_outcomes(
        ["owner/repo"],
        dry_run=dry_run,
        _pr_fetch_fn=lambda *_: [_shadow_ingest_pr(20, "a" * 40)],
        _issue_fetch_fn=lambda *_: BODY,
        _spec_dir=private_brain / "specs",
    )
    assert summary["runtime_ac_checkout_failures"] == 0


def test_shared_exact_head_checkout_reuse_and_temporary_cleanup(tmp_path, monkeypatch):
    source = tmp_path / "source"
    head = _shadow_git_repo(source)
    subprocess.run(["git", "-C", str(source), "remote", "add", "origin", str(source)], check=True)
    monkeypatch.setattr(provision, "ensure_canonical", lambda _repo: source)
    monkeypatch.setattr(provision, "canonical_path", lambda _repo: source)
    monkeypatch.setattr(provision, "WORKTREES_DIR", tmp_path / "worktrees")
    panel = provision.exact_head_worktree("owner/repo#20", head)
    assert provision.exact_head_worktree("owner/repo#20", head) == panel
    assert adversarial.panel_worktree("owner/repo#20", head) == panel
    with pytest.raises(RuntimeError, match="full commit SHA"):
        provision.exact_head_worktree("owner/repo#20", "not-a-commit")
    with pytest.raises(RuntimeError, match="cannot fetch"):
        provision.exact_head_worktree("owner/repo#20", "f" * 40)
    assert (
        subprocess.check_output(["git", "-C", str(panel), "rev-parse", "HEAD"], text=True).strip()
        == head
    )
    (Path(panel) / "dirty.txt").write_text("dirty")
    with pytest.raises(RuntimeError, match="dirty"):
        provision.exact_head_worktree("owner/repo#20", head)
    with pytest.raises(RuntimeError, match="verifier failed"):
        with provision.temporary_exact_head_worktree("owner/repo#20", head) as runtime_tree:
            assert Path(runtime_tree) != Path(panel)
            (Path(runtime_tree) / "dirty-cache").write_text("generated")

            # Cleanup must remain local even if a canonical fetch would stall.
            def unexpected_fetch(_repo):
                raise AssertionError("cleanup attempted a canonical fetch")

            monkeypatch.setattr(provision, "ensure_canonical", unexpected_fetch)
            raise RuntimeError("verifier failed")
    assert not Path(runtime_tree).exists()
    trees = subprocess.check_output(
        ["git", "-C", str(source), "worktree", "list", "--porcelain"], text=True
    )
    assert runtime_tree not in trees
    assert (Path(panel) / "dirty.txt").read_text() == "dirty"


def test_ingest_runs_real_named_test_and_removes_its_checkout(private_brain, monkeypatch, capsys):
    remote = private_brain / "remote"
    initial_head = _shadow_git_repo(remote)
    source = private_brain / "source"
    subprocess.run(
        ["git", "clone", "-q", "--no-local", str(remote), str(source)],
        check=True,
        capture_output=True,
    )
    # The PR commit is created after cloning: only an actual fetch can supply it.
    (remote / "tests").mkdir()
    (remote / "delivered.txt").write_text("exact PR contents\n")
    (remote / "tests/test_acceptance.py").write_text(
        "from pathlib import Path\n\n"
        "def test_delivered():\n"
        "    assert Path('delivered.txt').read_text() == 'exact PR contents\\n'\n"
    )
    subprocess.run(
        ["git", "-C", str(remote), "add", "tests/test_acceptance.py", "delivered.txt"],
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(remote),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "acceptance",
        ],
        check=True,
        capture_output=True,
    )
    head = subprocess.check_output(
        ["git", "-C", str(remote), "rev-parse", "HEAD"], text=True
    ).strip()
    assert head != initial_head
    missing_commit = subprocess.run(
        ["git", "-C", str(source), "cat-file", "-e", head], capture_output=True
    )
    assert missing_commit.returncode != 0
    initial_trees = subprocess.check_output(
        ["git", "-C", str(source), "worktree", "list", "--porcelain"], text=True
    )
    trees = private_brain / "runtime-trees"
    monkeypatch.setattr(provision, "ensure_canonical", lambda _repo: source)
    monkeypatch.setattr(provision, "canonical_path", lambda _repo: source)
    monkeypatch.setattr(provision, "WORKTREES_DIR", trees)
    monkeypatch.setattr(keepalive_outcomes, "_gh_throttle", lambda _: None)
    monkeypatch.setenv("ORCH_RUN_RUNTIME_AC", "1")
    monkeypatch.setenv("ORCH_RUNTIME_AC_ALLOW_COMMANDS", "1")
    monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)
    # The weekly reader must consume persisted ingest events, not a hand-built
    # summary or a verifier result supplied directly to observe_shadow_spec.
    assert gate.shadow_summary(now=int(time.time()))["executed"] == 0
    summary = keepalive_outcomes.ingest_keepalive_outcomes(
        ["owner/repo"],
        _pr_fetch_fn=lambda *_: [_shadow_ingest_pr(20, head)],
        _issue_fetch_fn=lambda *_: (
            "## Acceptance Criteria\n- Named test: `tests/test_acceptance.py::test_delivered`.\n"
        ),
        _spec_dir=private_brain / "specs",
    )
    executed = [
        row for row in feedback.runtime_ac_gate_events() if row["gate_status"] == "executed"
    ]
    assert len(executed) == 1 and executed[0]["verifier_verdict"] == "PASS", summary
    event = executed[0]
    assert event["observed_head_sha"] == head
    assert event["blocking"] is False and event["shadow_only"] is True
    assert event["false_fail_audit"] is False
    assert len(event["audit_evidence"]) == 1
    audit = event["audit_evidence"][0]
    command = shlex.split(audit["command"])
    assert command[:3] == ["python3", "-m", "pytest"]
    assert command[-1] == "tests/test_acceptance.py::test_delivered"
    assert audit["status"] == "PASS" and audit["returncode"] == 0
    assert audit["passing_named_test"] is True
    assert summary["runtime_ac_checkout_failures"] == 0
    assert summary["runtime_ac_shadow_errors"] == []
    weekly = gate.shadow_summary(now=int(time.time()))
    assert weekly["prs"] == 1
    assert weekly["specs_authored"] == 1
    assert weekly["executed"] == 1
    assert weekly["execution_unmeasured"] == 0
    # Exercise the weekly CLI's real review -> database reader -> formatter path.
    # Only unrelated fleet readers/writers are isolated; no shadow result or
    # report is injected, and a removed review() field must fail this assertion.
    import adjudicator_retro

    monkeypatch.setenv("ORCH_VALUE_CHAIN_MONITOR", "0")
    monkeypatch.setenv("ORCH_DISABLE_STEPS", "issue-size-quality")
    monkeypatch.setattr(switch_review, "_capability_heartbeat", lambda: None)
    monkeypatch.setattr(
        switch_review, "switch_states", lambda **_: {"held_off": [], "on_but_idle": []}
    )
    monkeypatch.setattr(switch_review, "stale_runners", lambda: [])
    monkeypatch.setattr(switch_review, "mirror_drift", lambda: {"status": "ok"})
    monkeypatch.setattr(switch_review, "fleet_gates", lambda **_: {})
    monkeypatch.setattr(switch_review, "firing_regressions", lambda **_: {})
    monkeypatch.setattr(switch_review, "_exploration_gate", lambda: {})
    monkeypatch.setattr(switch_review, "gate_expiry", lambda **_: None)
    monkeypatch.setattr(switch_review, "capacity_shed", lambda: None)
    monkeypatch.setattr(switch_review, "worktree_git_refusals", lambda: None)
    monkeypatch.setattr(switch_review, "credential_exposure", lambda: None)
    monkeypatch.setattr(switch_review, "adversarial_shape_population", lambda **_: {})
    monkeypatch.setattr(adjudicator_retro, "weekly_line", lambda: "unmeasured")
    monkeypatch.setattr(adversarial, "record_shape_measurement", lambda *_, **__: None)

    def unexpected_github(*_, **__):
        raise AssertionError("shadow report attempted an unrelated GitHub request")

    monkeypatch.setattr(switch_review, "_GH_CALL_RUNNER", unexpected_github)
    capsys.readouterr()
    assert switch_review.main(["--env", "process"]) == 0
    printed = capsys.readouterr().out
    shadow_lines = [line for line in printed.splitlines() if line.startswith("runtime-AC shadow:")]
    assert len(shadow_lines) == 1
    assert "PRs 1, specs authored 1, executed 1, would-FAIL 0" in shadow_lines[0]
    assert "false-FAIL audit 0 of 1 (owner/repo#20)" in shadow_lines[0]
    assert "execution unmeasured 0" in shadow_lines[0]
    # The weekly machine-readable artifact and its readable companion must
    # agree with stdout, without creating any new runtime-AC observations.
    text_out = private_brain / "switch-review.txt"
    events_before_report = feedback.runtime_ac_gate_events()
    assert switch_review.main(["--env", "process", "--json", "--text-out", str(text_out)]) == 0
    cli_report = json.loads(capsys.readouterr().out)
    assert cli_report["runtime_ac_shadow"] == weekly
    assert shadow_lines[0] in text_out.read_text()
    assert feedback.runtime_ac_gate_events() == events_before_report
    fetched_head = subprocess.check_output(
        ["git", "-C", str(source), "rev-parse", "FETCH_HEAD"], text=True
    ).strip()
    canonical_head = subprocess.check_output(
        ["git", "-C", str(source), "rev-parse", "HEAD"], text=True
    ).strip()
    assert fetched_head == head and canonical_head == initial_head
    assert list(trees.iterdir()) == []
    listed = subprocess.check_output(
        ["git", "-C", str(source), "worktree", "list", "--porcelain"], text=True
    )
    assert listed == initial_trees


def test_ingest_cleans_checkout_when_verifier_raises(private_brain, monkeypatch):
    cleaned = []

    @contextmanager
    def provider(target, head):
        try:
            yield str(private_brain)
        finally:
            cleaned.append(target)

    monkeypatch.setattr(provision, "temporary_exact_head_worktree", provider)
    monkeypatch.setattr(keepalive_outcomes, "_gh_throttle", lambda _: None)
    monkeypatch.setenv("ORCH_RUN_RUNTIME_AC", "1")

    def fail(*args, **kwargs):
        raise RuntimeError("verifier error")

    monkeypatch.setattr(gate, "observe_shadow_spec", fail)
    summary = keepalive_outcomes.ingest_keepalive_outcomes(
        ["owner/repo"],
        _pr_fetch_fn=lambda *_: [_shadow_ingest_pr(20, "a" * 40)],
        _issue_fetch_fn=lambda *_: BODY,
        _spec_dir=private_brain / "specs",
    )
    assert cleaned == ["owner/repo#20"]
    assert summary["runtime_ac_checkout_failures"] == 0
    assert summary["runtime_ac_shadow_errors"] == ["owner/repo#20: verifier error"]
