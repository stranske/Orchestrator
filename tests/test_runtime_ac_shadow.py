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
    assert gate.spec_path(
        "owner/repo#2", env={"ORCH_STATE_DIR": "/private/fixture"}
    ).parent == Path("/private/fixture/runtime-ac")
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
    assert "would-FAIL 1 (unmet ACs: owner/repo#2)" in line
    assert summary["would_fail_targets"] == summary["merged_with_unmet_targets"] == ["owner/repo#2"]
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
    # The latest observation per PR stands beside every cause: repo#3 was refused for budget
    # after its checkout failed, so "what stands now" is one budget refusal, not two causes.
    assert reasons["latest_reason_per_pr"] == {
        "shadow_execution_budget": 1,
        "shadow_execution_disabled": 1,
        "unrecorded": 1,
    }
    reason_line = gate.format_shadow_summary(reasons)
    assert "recorded reasons (PRs per reason): shadow_checkout_failed 1" in reason_line
    assert "latest unmeasured state (PRs): shadow_execution_budget 1" in reason_line
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
    monkeypatch.delenv(gate.SHADOW_BUDGET_FLAG, raising=False)
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
    monkeypatch.delenv(gate.SHADOW_BUDGET_FLAG, raising=False)
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


@pytest.fixture
def post_merge_case(private_brain, monkeypatch):
    import backlog
    import durability_sweep as sweep

    checkout = private_brain / "merged-checkout"
    _shadow_git_repo(checkout)
    # Recheck environment setup is tested separately; these sweep fixtures stay offline.
    (checkout / ".venv").symlink_to(sys.prefix, target_is_directory=True)
    (checkout / "tests").mkdir()
    (checkout / "tests/test_merged.py").write_text(
        "def test_delivered():\n    assert False, 'delivery missing on merged tip'\n"
    )
    clean_env = _shadow_git_env()
    subprocess.run(["git", "-C", str(checkout), "add", "tests"], check=True, env=clean_env)
    subprocess.run(
        [
            "git",
            "-C",
            str(checkout),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-m",
            "merged fixture",
        ],
        check=True,
        capture_output=True,
        env=clean_env,
    )
    sha = subprocess.check_output(
        ["git", "-C", str(checkout), "rev-parse", "HEAD"], text=True, env=clean_env
    ).strip()
    root = private_brain / "specs"
    root.mkdir()
    spec = runtime_ac.author_issue_spec(
        "owner/repo#1",
        "## Acceptance Criteria\n- Named test: `tests/test_merged.py::test_delivered`.\n",
        target="owner/repo#2",
    )
    spec["acceptance_criteria"][0]["checks"][0][
        "command"
    ] = f"{shlex.quote(sys.executable)} -m pytest -p no:cov tests/test_merged.py::test_delivered -q"
    path = gate.spec_path("owner/repo#2", spec_dir=root)
    path.write_text(json.dumps(spec))
    monkeypatch.setenv("ORCH_STATE_DIR", str(private_brain))
    monkeypatch.setenv("ORCH_RUN_RUNTIME_AC", "1")
    monkeypatch.setenv("ORCH_RUNTIME_AC_ALLOW_COMMANDS", "1")
    # The recheck's own switch defaults ON; an inherited `=0` would turn the default-on tests
    # into UNRUNNABLE, so the fixture starts from the default.
    monkeypatch.delenv(gate.POST_MERGE_COMMANDS_FLAG, raising=False)
    monkeypatch.setenv("ORCH_RUNTIME_AC_REOPEN", "1")
    monkeypatch.setattr(backlog, "SUPPORTED_REPOS", ["owner/repo"])
    monkeypatch.setattr(
        sweep, "_merged_verifier_candidates", lambda: {("owner/repo", 2): {"fixture"}}
    )
    for name in ["_pending_merged_runs", "_truncated_durable_runs", "_bare_reference_runs"]:
        monkeypatch.setattr(sweep, name, lambda: [])
    provisions = []

    @contextmanager
    def provider(target, head):
        assert target == "owner/repo#2" and head == sha
        provisions.append(head)
        yield str(checkout)

    monkeypatch.setattr(provision, "temporary_exact_head_worktree", provider)
    calls, comments = [], []
    fail_comment = [False]

    def gh(args):
        calls.append(args)
        if args == ["gh", "api", "repos/owner/repo/issues/1"]:
            return {"state": "closed"}, None
        if args[1:3] == ["pr", "view"]:
            return {
                "state": "MERGED",
                "mergeCommit": {"oid": sha},
                "closingIssuesReferences": [{"number": 1}],
            }, None
        if "--slurp" in args:
            return [comments], None
        if "POST" in args and args[4].endswith("/comments"):
            if fail_comment[0]:
                return None, "temporary comment API failure"
            comments.append({"body": args[-1].removeprefix("body=")})
        return {}, None

    def run(**kwargs):
        return sweep.sweep_durability(
            _gh=gh, _state_fn=lambda _: None, _spec_dir=root, _now=1792022400, **kwargs
        )

    return {
        "run": run,
        "calls": calls,
        "comments": comments,
        "provisions": provisions,
        "sha": sha,
        "spec": spec,
        "path": path,
        "checkout": checkout,
        "fail_comment": fail_comment,
    }


def _post_merge_events():
    with feedback._conn() as conn:
        return conn.execute(
            "SELECT phase,validation_status,payload_json FROM completion_events WHERE producer='runtime_ac_gate' AND phase='post_merge_recheck'"
        ).fetchall()


def test_post_merge_recheck_reopens_on_fail(post_merge_case):
    case = post_merge_case
    summary = case["run"]()
    assert summary["runtime_ac_post_merge"] == {"rechecked": 1, "errors": []}
    reopens = [a for a in case["calls"] if "PATCH" in a]
    assert len(reopens) == 1 and reopens[0][-1] == "state=open"
    assert any("labels[]=runtime-ac:reopened" in a for a in case["calls"])
    assert len(case["comments"]) == 1
    assert case["comments"][0]["body"].startswith(f"runtime-AC post-merge FAIL on {case['sha']}")
    assert "delivery missing on merged tip" in case["comments"][0]["body"]
    rows = _post_merge_events()
    assert len(rows) == 1 and rows[0][:2] == ("post_merge_recheck", "accepted")
    payload = json.loads(rows[0][2])["runtime_ac_post_merge"]
    assert payload["result"] == "FAIL" and payload["reopened"] is True
    assert gate.post_merge_summary() == {
        "rechecked": 1,
        "passed": 0,
        "failed": 1,
        "reopened": 1,
        "unrunnable": 0,
        "unrunnable_reasons": {},
    }
    line = gate.format_post_merge_summary(gate.post_merge_summary())
    assert (
        line == "runtime-AC post-merge: rechecked 1, passed 0, failed 1, reopened 1, unrunnable 0"
        ", reopen forced_on (executed 1 of 10)"
    )
    assert line in switch_review.format_report(
        {
            "generated_at": 1792022400,
            "review_days": 7,
            "raise_count": 0,
            "held_off": [],
            "on_but_idle": [],
            "unconditioned": [],
            "runtime_ac_post_merge": gate.post_merge_summary(),
        }
    )


def test_post_merge_recheck_is_idempotent(post_merge_case):
    case = post_merge_case
    case["run"]()
    events = _post_merge_events()
    case["run"]()
    assert _post_merge_events() == events
    assert len(case["provisions"]) == len(case["comments"]) == 1
    assert len([a for a in case["calls"] if "PATCH" in a]) == 1


def test_post_merge_recheck_never_reopens_below_ten_executed_rechecks(post_merge_case, monkeypatch):
    monkeypatch.delenv("ORCH_RUNTIME_AC_REOPEN")
    case = post_merge_case
    case["run"]()
    assert len(case["comments"]) == 1
    assert not any("PATCH" in a or "labels[]=runtime-ac:reopened" in a for a in case["calls"])
    summary = gate.post_merge_summary()
    assert summary["reopened"] == 0
    assert gate.reopen_policy({}, summary) == {
        "mode": "auto",
        "allowed": False,
        "executed": 1,
        "floor": 10,
    }
    assert "reopen auto (executed 1 of 10)" in gate.format_post_merge_summary(summary)


def test_post_merge_recheck_commands_disabled_is_unrunnable_and_silent(
    post_merge_case, monkeypatch
):
    """The runner's own switch is not the delivery's state: an event, a reason, no comment."""
    case = post_merge_case
    monkeypatch.setenv(gate.POST_MERGE_COMMANDS_FLAG, "0")
    case["run"]()
    case["run"]()
    assert case["comments"] == []
    assert not any("PATCH" in a for a in case["calls"])
    summary = gate.post_merge_summary()
    assert summary["unrunnable"] == 1 and summary["passed"] == 0
    assert summary["unrunnable_reasons"] == {"commands_disabled": 1}
    assert "unrunnable 1 (commands_disabled 1)" in gate.format_post_merge_summary(summary)


@pytest.mark.parametrize(
    "value,allowed",
    [
        (None, True),
        ("1", True),
        ("yes", True),
        ("", True),
        ("0", False),
        ("false", False),
        ("no", False),
        ("off", False),
        (" OFF ", False),
    ],
)
def test_post_merge_commands_allowed_reads_every_documented_value(value, allowed):
    """The post-merge policy is its own switch: every documented disabled word, and absence."""
    env = {} if value is None else {gate.POST_MERGE_COMMANDS_FLAG: value}
    assert gate.post_merge_commands_allowed(env) is allowed
    # The ingest opt-in never reaches this policy in either direction.
    assert (
        gate.post_merge_commands_allowed({"ORCH_RUNTIME_AC_ALLOW_COMMANDS": "0", **env}) is allowed
    )


def test_post_merge_recheck_runs_the_named_test_without_the_ingest_opt_in(
    post_merge_case, monkeypatch
):
    """Command checks are on by default here; the ingest's opt-in no longer gates the recheck."""
    case = post_merge_case
    monkeypatch.delenv("ORCH_RUNTIME_AC_ALLOW_COMMANDS", raising=False)
    case["run"]()
    summary = gate.post_merge_summary()
    assert summary["failed"] == 1 and summary["unrunnable"] == 0
    assert len(case["comments"]) == 1
    assert case["comments"][0]["body"].startswith("runtime-AC post-merge FAIL")


def test_post_merge_recheck_passes_when_every_named_test_passes_despite_manual_criteria(
    post_merge_case, monkeypatch
):
    """A spec mixing named tests with manual criteria reads NEEDS_REVIEW at the gate; the recheck's
    verdict is about the named tests, so green tests are PASS. A gate FAIL is not."""
    case = post_merge_case

    def mixed(*_a, **_kw):
        return {
            "gate": {"verdict": "NEEDS_REVIEW"},
            "check_results": [
                {"type": "command", "status": "PASS", "returncode": 0},
                {"type": "manual", "status": "NEEDS_REVIEW"},
                {"type": "manual", "status": "NEEDS_REVIEW"},
            ],
        }

    monkeypatch.setattr(runtime_ac, "run_verification", mixed)
    result = gate.recheck_merged(case["spec"], case["checkout"])
    assert result["result"] == "PASS" and result["reason"] is None
    # A non-PASS executable result under the same manual NEEDS_REVIEW gate is not a pass.
    for status in ("SKIP", "ERROR"):
        monkeypatch.setattr(
            runtime_ac,
            "run_verification",
            lambda *_a, _status=status, **_kw: {
                "gate": {"verdict": "NEEDS_REVIEW"},
                "check_results": [
                    {"type": "command", "status": "PASS", "returncode": 0},
                    {"type": "command", "status": _status},
                    {"type": "manual", "status": "NEEDS_REVIEW"},
                ],
            },
        )
        result = gate.recheck_merged(case["spec"], case["checkout"])
        assert result["result"] == "UNRUNNABLE" and result["reason"] == "incomplete", status
    monkeypatch.setattr(
        runtime_ac,
        "run_verification",
        lambda *_a, **_kw: {
            "gate": {"verdict": "FAIL"},
            "check_results": [{"type": "command", "status": "PASS", "returncode": 0}],
        },
    )
    result = gate.recheck_merged(case["spec"], case["checkout"])
    assert result["result"] == "UNRUNNABLE" and result["reason"] == "incomplete"


def test_post_merge_recheck_manual_only_spec_comments_one_line_once(post_merge_case, monkeypatch):
    """A spec with no executable check is about the delivery, so it is said once, in one line."""
    case = post_merge_case
    monkeypatch.setattr(
        runtime_ac,
        "run_verification",
        lambda *_a, **_kw: {
            "gate": {"verdict": "NEEDS_REVIEW"},
            "check_results": [{"type": "manual", "status": "NEEDS_REVIEW"}],
        },
    )
    case["run"]()
    case["run"]()
    assert len(case["comments"]) == 1
    body = case["comments"][0]["body"]
    assert body.startswith("runtime-AC post-merge UNRUNNABLE") and "```" not in body
    assert "no executable test" in body
    assert not any("PATCH" in a for a in case["calls"])
    assert gate.post_merge_summary()["unrunnable_reasons"] == {"manual_only": 1}


def test_post_merge_recheck_retries_delivery_without_reexecution(post_merge_case):
    case = post_merge_case
    case["fail_comment"][0] = True
    assert case["run"]()["runtime_ac_post_merge"]["errors"]
    case["fail_comment"][0] = False
    case["run"]()
    assert len(case["provisions"]) == len(case["comments"]) == 1
    assert len(_post_merge_events()) == 1
    assert gate.post_merge_summary()["reopened"] == 1


def test_post_merge_recheck_dry_run_and_execution_off_write_nothing(post_merge_case, monkeypatch):
    case = post_merge_case
    case["run"](dry_run=True)
    monkeypatch.setenv("ORCH_RUN_RUNTIME_AC", "0")
    case["run"]()
    assert case["calls"] == case["provisions"] == []
    assert _post_merge_events() == []


def test_post_merge_reopen_declaration_is_evidence_gated(post_merge_case, monkeypatch):
    declaration = gate.capabilities.KNOWN_GATES["runtime-ac-checks"]
    assert declaration["flags_defaults"]["post_merge_reopen_default"] == {
        "ORCH_RUNTIME_AC_REOPEN": "auto"
    }
    assert declaration["flags_defaults"]["shadow_budget_default"] == {gate.SHADOW_BUDGET_FLAG: "12"}
    assert "runtime_ac_gate.reopen_policy" in declaration["gate_evidence"]
    assert "rollback: ORCH_RUNTIME_AC_REOPEN=0" in declaration["gate_evidence"]
    assert gate.capabilities.artifact_lifecycle(declaration) is None
    # Under the default, one executed recheck is below the floor: the FAIL is commented, not
    # reopened, and the line says how far the pool is from opening the path.
    monkeypatch.setenv("ORCH_RUNTIME_AC_REOPEN", "auto")
    case = post_merge_case
    case["run"]()
    assert len(case["comments"]) == 1
    assert not any("PATCH" in a for a in case["calls"])
    assert "reopen auto (executed 1 of 10)" in gate.format_post_merge_summary(
        gate.post_merge_summary()
    )


def test_post_merge_recheck_reuses_named_and_deliberate_break_runner(post_merge_case, monkeypatch):
    case = post_merge_case
    original = json.dumps(case["spec"], sort_keys=True)
    calls = []

    def verify(spec, **kwargs):
        calls.append((spec, kwargs))
        return {
            "gate": {"verdict": "PASS"},
            "check_results": [
                {"type": "command", "status": "PASS"},
                {
                    "type": "deliberate_break",
                    "status": "PASS",
                    "stdout_tail": json.dumps(
                        {"verdict": "PASS", "error": None, "green": {"returncode": 0}}
                    ),
                },
            ],
        }

    monkeypatch.setattr(runtime_ac, "run_verification", verify)
    result = gate.recheck_merged(case["spec"], case["checkout"])
    assert result["result"] == "PASS" and "deliberate_break" in result["transcript"]
    assert calls[0][0]["runtime_context"]["worktree"] == str(case["checkout"])
    assert calls[0][1] == {"confirm_run": True, "allow_command_checks": True, "timeout": 120}
    assert json.dumps(case["spec"], sort_keys=True) == original
    case["run"]()
    assert case["comments"] == [] and not any("PATCH" in a for a in case["calls"])
    assert gate.post_merge_summary() == {
        "rechecked": 1,
        "passed": 1,
        "failed": 0,
        "reopened": 0,
        "unrunnable": 0,
        "unrunnable_reasons": {},
    }


@pytest.mark.parametrize(
    "returncode,output",
    [
        (2, "ERROR collecting tests: ImportError"),
        (4, "ERROR: not found: tests/missing.py"),
        (5, "no tests ran"),
        (1, "No module named pytest"),
    ],
)
def test_post_merge_recheck_tool_failures_never_reopen(
    post_merge_case, monkeypatch, returncode, output
):
    case = post_merge_case
    monkeypatch.setattr(
        runtime_ac,
        "run_verification",
        lambda *_a, **_kw: {
            "gate": {"verdict": "FAIL"},
            "check_results": [
                {
                    "type": "command",
                    "status": "FAIL",
                    "returncode": returncode,
                    "stdout_tail": output,
                }
            ],
        },
    )
    case["run"]()
    summary = gate.post_merge_summary()
    assert summary["unrunnable"] == 1 and summary["unrunnable_reasons"] == {"incomplete": 1}
    assert not any("PATCH" in a for a in case["calls"])
    # A launcher or collection failure is not the delivery's state either: no comment.
    assert case["comments"] == []


def test_post_merge_recheck_refuses_wrong_merge_checkout(post_merge_case, monkeypatch):
    case = post_merge_case

    @contextmanager
    def wrong_checkout(*_):
        other = case["checkout"].parent / "other"
        _shadow_git_repo(other)
        yield str(other)

    monkeypatch.setattr(provision, "temporary_exact_head_worktree", wrong_checkout)
    case["run"]()
    summary = gate.post_merge_summary()
    assert summary["unrunnable"] == 1 and summary["unrunnable_reasons"] == {"tool_error": 1}
    # The runner's own checkout failure is not the delivery's state: event and transcript only.
    assert case["comments"] == []
    assert not any("PATCH" in a for a in case["calls"])


def test_post_merge_recheck_refuses_unbound_issue(post_merge_case):
    case = post_merge_case
    spec = case["spec"]
    spec["verification"]["source_issue"] = "owner/repo#9"
    case["path"].write_text(json.dumps(spec))
    summary = case["run"]()
    assert "closing references" in summary["runtime_ac_post_merge"]["errors"][0]
    assert case["provisions"] == case["comments"] == []
    assert _post_merge_events() == []


def test_post_merge_recheck_honors_configured_timeout(post_merge_case, monkeypatch):
    monkeypatch.setenv("ORCH_RUNTIME_AC_TIMEOUT", "37")
    calls = []

    def verify(spec, **kwargs):
        calls.append(kwargs)
        return {
            "gate": {"verdict": "PASS"},
            "check_results": [{"type": "command", "status": "PASS"}],
        }

    monkeypatch.setattr(runtime_ac, "run_verification", verify)
    case = post_merge_case
    assert gate.recheck_merged(case["spec"], case["checkout"])["result"] == "PASS"
    assert calls == [{"confirm_run": True, "allow_command_checks": True, "timeout": 37}]


@pytest.mark.parametrize(
    "payload",
    [
        "not JSON",
        "[]",
        '{"runtime_ac_post_merge": []}',
        '{"runtime_ac_post_merge":{"result":"UNKNOWN"}}',
        '{"runtime_ac_post_merge":{"result":{}}}',
    ],
)
def test_post_merge_summary_preserves_valid_rows_when_one_is_malformed(
    post_merge_case, monkeypatch, payload
):
    class Connection:
        def execute(self, *_args):
            return self

        def fetchall(self):
            return [
                (payload,),
                ('{"runtime_ac_post_merge":{"result":"FAIL","reopened":true}}',),
                ('{"rejection_codes":["invalid"]}',),
            ]

    @contextmanager
    def connection():
        yield Connection()

    monkeypatch.setattr(feedback, "_conn", connection)
    summary = gate.post_merge_summary()
    assert summary == {
        "rechecked": 1,
        "passed": 0,
        "failed": 1,
        "reopened": 1,
        "unrunnable": 0,
        "unrunnable_reasons": {},
        "malformed": 2,
    }
    assert "rechecked 1" in gate.format_post_merge_summary(summary)
    assert "malformed 2" in gate.format_post_merge_summary(summary)


@pytest.mark.parametrize("body_fails", [False, True])
def test_temporary_checkout_retains_prune_failure_even_after_path_removal(
    tmp_path, monkeypatch, body_fails
):
    checkout = tmp_path / "private-checkout"
    checkout.mkdir()
    monkeypatch.setattr(provision, "exact_head_worktree", lambda *a, **kw: str(checkout))
    monkeypatch.setattr(provision, "canonical_path", lambda _repo: tmp_path)

    def failed_cleanup(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 1, "", "cleanup denied")

    monkeypatch.setattr(provision, "_run", failed_cleanup)
    with pytest.raises(RuntimeError, match="cleanup denied") as caught:
        with provision.temporary_exact_head_worktree("owner/repo#1", "a" * 40):
            if body_fails:
                raise ValueError("original verifier failure")
    assert not checkout.exists()
    if body_fails:
        assert isinstance(caught.value.__cause__, ValueError)
        assert str(caught.value.__cause__) == "original verifier failure"


def _seed_executed_rechecks(count: int) -> None:
    for i in range(count):
        feedback.record_completion_event(
            f"runtime-ac-post-merge:seed{i}",
            event_type="verification",
            phase="post_merge_recheck",
            producer="runtime_ac_gate",
            event_id=f"event:post-merge-recheck:seed{i}",
            status="completed",
            payload={
                "runtime_ac_post_merge": {
                    "result": "PASS",
                    "reason": None,
                    "issue": f"owner/repo#{100 + i}",
                    "merge_sha": "f" * 40,
                    "reopened": False,
                    "commented": False,
                    "delivery_complete": True,
                }
            },
        )


def test_execution_budget_reads_one_flag(monkeypatch):
    monkeypatch.delenv(gate.SHADOW_BUDGET_FLAG, raising=False)
    assert gate.execution_budget() == gate.DEFAULT_EXECUTION_BUDGET == 3
    assert gate.execution_budget({gate.SHADOW_BUDGET_FLAG: "12"}) == 12
    assert gate.execution_budget({gate.SHADOW_BUDGET_FLAG: "x"}) == 3
    assert gate.execution_budget({gate.SHADOW_BUDGET_FLAG: "-4"}) == 0
    assert gate.execution_budget({}, default=5) == 5


def test_reopen_policy_is_evidence_not_a_date():
    below = {"passed": 6, "failed": 3}
    at = {"passed": 9, "failed": 1}
    assert gate.reopen_policy({}, below) == {
        "mode": "auto",
        "allowed": False,
        "executed": 9,
        "floor": 10,
    }
    assert gate.reopen_policy({}, at)["allowed"] is True
    forced = gate.reopen_policy({gate.REOPEN_FLAG: "1"}, below)
    assert (forced["mode"], forced["allowed"]) == ("forced_on", True)
    assert gate.reopen_policy({gate.REOPEN_FLAG: "0"}, at) == {
        "mode": "off",
        "allowed": False,
        "executed": 10,
        "floor": 10,
    }
    assert gate.reopen_policy({gate.REOPEN_FLAG: "auto"}, at)["allowed"] is True
    assert not hasattr(gate.capabilities, "RUNTIME_AC_REOPEN_EXPIRY"), "the calendar expiry is gone"


def test_ingest_budget_comes_from_the_flag_or_the_argument(private_brain, monkeypatch):
    checkout = private_brain / "checkout"
    head = _shadow_git_repo(checkout)
    calls = []

    @contextmanager
    def provider(target, requested_head):
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
    monkeypatch.setenv(gate.SHADOW_BUDGET_FLAG, "1")
    keepalive_outcomes.ingest_keepalive_outcomes(
        ["owner/first"],
        _pr_fetch_fn=lambda *_: [_shadow_ingest_pr(n, head) for n in (20, 21)],
        _issue_fetch_fn=lambda *_: BODY,
        _spec_dir=private_brain / "specs",
    )
    assert len(calls) == 1, "the flag bounds the ingest"
    keepalive_outcomes.ingest_keepalive_outcomes(
        ["owner/second"],
        _pr_fetch_fn=lambda *_: [_shadow_ingest_pr(n, head) for n in (30, 31, 32)],
        _issue_fetch_fn=lambda *_: BODY,
        _spec_dir=private_brain / "specs",
        shadow_budget=3,
    )
    assert len(calls) == 4, "an explicit budget (the one-off backfill) wins over the flag"


def test_post_merge_reopen_opens_on_ten_executed_rechecks(post_merge_case, monkeypatch):
    monkeypatch.delenv("ORCH_RUNTIME_AC_REOPEN", raising=False)
    _seed_executed_rechecks(gate.REOPEN_EXECUTED_FLOOR)
    case = post_merge_case
    case["run"]()
    assert any("PATCH" in a for a in case["calls"]), "ten executed rechecks open the reopen path"
    summary = gate.post_merge_summary()
    assert summary["reopened"] == 1
    assert gate.reopen_policy({}, summary) == {
        "mode": "auto",
        "allowed": True,
        "executed": 11,
        "floor": 10,
    }
    assert "reopen auto (executed 11 of 10)" in gate.format_post_merge_summary(summary)
    # Deliberate break, in place: the kill switch still wins over the evidence.
    monkeypatch.setenv("ORCH_RUNTIME_AC_REOPEN", "0")
    assert "reopen off (executed 11 of 10)" in gate.format_post_merge_summary(summary)


def test_post_merge_legacy_row_without_a_reason_is_rechecked_once(
    post_merge_case, private_brain, monkeypatch
):
    import hashlib

    case = post_merge_case
    root = private_brain / "specs"
    identity = hashlib.sha256(f"owner/repo#1@{case['sha']}".encode()).hexdigest()
    transcript = root / f"post-merge-{identity}.txt"
    transcript.write_text("legacy recheck transcript\n", encoding="utf-8")
    legacy = {
        "result": "UNRUNNABLE",
        "issue": "owner/repo#1",
        "merge_sha": case["sha"],
        "reopened": False,
        "commented": True,
        "delivery_complete": True,
        "transcript_path": str(transcript),
        "transcript_hash": hashlib.sha256(transcript.read_bytes()).hexdigest(),
    }
    feedback.record_completion_event(
        f"runtime-ac-post-merge:{identity}",
        event_type="verification",
        phase="post_merge_recheck",
        producer="runtime_ac_gate",
        event_id=f"event:post-merge-recheck:{identity}",
        status="completed",
        payload={"runtime_ac_post_merge": legacy},
    )
    assert gate.post_merge_summary()["unrunnable_reasons"] == {"unclassified": 1}
    case["run"]()
    assert len(case["provisions"]) == 1, "a legacy row without a cause is re-run once"
    summary = gate.post_merge_summary()
    assert summary["rechecked"] == 1 and summary["unrunnable"] == 0 and summary["failed"] == 1
    case["run"]()
    assert len(case["provisions"]) == 1, "and only once"
