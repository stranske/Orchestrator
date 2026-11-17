"""An unlinked PR's own acceptance claim becomes its runtime-AC spec, so a required gate can run.

stranske/Orchestrator#1415 (2026-11-17): the Gate's acceptance-criteria label armed runtime AC for a PR that
closes no issue; specs were authored only from a closing issue, so merge_guard refused it until a person
wrote the spec by hand. Its marker is the fixture below.
"""

import json

import merge_guard
import runtime_ac_gate

MARKER_1415 = (
    "## Deliberate break\n\n<!-- deliberate-break: "
    "test=tests/test_gate_label_job.py::test_the_label_job_can_write_pull_requests_and_runs_no_pr_code "
    "test-file=tests/test_gate_label_job.py break-file=.github/workflows/pr-00-gate.yml -->\n"
)
TARGET = "stranske/Orchestrator#1415"


def test_a_marker_becomes_a_named_test_and_a_deliberate_break():
    text = runtime_ac_gate.pr_body_acceptance(MARKER_1415)
    assert text.startswith("## Acceptance Criteria\n")
    assert "python -m pytest tests/test_gate_label_job.py::test_the_label_job" in text
    assert "restore the base version of `.github/workflows/pr-00-gate.yml`" in text


def test_an_acceptance_section_in_the_pr_body_wins_and_no_claim_is_none():
    body = (
        "Intro\n\n## Acceptance Criteria\n- [x] `pytest tests/test_x.py` passes.\n\n## Notes\nx\n"
    )
    assert runtime_ac_gate.pr_body_acceptance(body) == (
        "## Acceptance Criteria\n- [x] `pytest tests/test_x.py` passes.\n\n"
    )
    assert runtime_ac_gate.pr_body_acceptance("No claim here.") is None
    assert (
        runtime_ac_gate.pr_body_acceptance("<!-- deliberate-break: revert the branch -->") is None
    )
    assert runtime_ac_gate.pr_body_acceptance("<!-- deliberate-break: test='unclosed -->") is None


def test_the_spec_runs_the_break_and_is_never_overwritten(tmp_path):
    first = runtime_ac_gate.author_pr_body_spec(TARGET, MARKER_1415, "e" * 40, spec_dir=tmp_path)
    assert first["status"] == "authored"
    spec = json.loads((tmp_path / "stranske__Orchestrator__1415.json").read_text())
    kinds = [check["type"] for ac in spec["acceptance_criteria"] for check in ac["checks"]]
    assert "command" in kinds and "deliberate_break" in kinds
    assert spec["verification"]["source"] == "pr_body"
    assert spec["verification"]["observed_head_sha"] == "e" * 40
    again = runtime_ac_gate.author_pr_body_spec(
        TARGET, "## Acceptance Criteria\n- x", "f" * 40, spec_dir=tmp_path
    )
    assert again["status"] == "existing"
    assert json.loads((tmp_path / "stranske__Orchestrator__1415.json").read_text()) == spec


def _meta(closing):
    return lambda target: {
        "target": target,
        "title": "t",
        "labels": ["acceptance-criteria"],
        "state": "OPEN",
        "is_draft": False,
        "merge_state_status": "CLEAN",
        "closing_issues": closing,
        "head_branch": "fix/x",
        "body": MARKER_1415,
    }


def test_merge_gate_authors_the_spec_before_the_gate_only_for_an_unlinked_pr(tmp_path):
    seen = []

    def gate(item, *, dry_run, env, spec_dir):
        seen.append(runtime_ac_gate.spec_path(item["target"], spec_dir=spec_dir).exists())
        return {"status": "planned", "blocks": False}

    result = merge_guard.evaluate_merge_gate(
        TARGET,
        dry_run=False,
        env={},
        spec_dir=tmp_path,
        metadata_fn=_meta([]),
        gate_fn=gate,
        expected_head="e" * 40,
    )
    assert seen == [True]
    assert result["pr_body_spec"]["status"] == "authored"
    assert result["blocked"] is False

    linked_dir = tmp_path / "linked"
    seen.clear()
    result = merge_guard.evaluate_merge_gate(
        "stranske/Orchestrator#1416",
        dry_run=False,
        env={},
        spec_dir=linked_dir,
        metadata_fn=_meta(["stranske/Orchestrator#1414"]),
        gate_fn=gate,
        expected_head="e" * 40,
    )
    assert seen == [False]  # a linked PR keeps the issue-spec path; nothing authored here
    assert "pr_body_spec" not in result
