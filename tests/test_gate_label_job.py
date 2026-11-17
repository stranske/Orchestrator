"""Gate labels a deliberate-break PR from a job that can write PR labels and runs no PR-head code.

stranske/Orchestrator#1354 (2026-11-12) and #1382 (2026-11-15): with pull-requests read, "Apply runtime
acceptance-criteria label" inside test-quality failed "Resource not accessible by integration". Workflows#4688
moved it into its own job in the consumer template, but pr-00-gate.yml syncs create_only, so this repo's own
Gate kept the old step until it was ported here.
"""

from pathlib import Path

import yaml

GATE = Path(__file__).resolve().parents[1] / ".github/workflows/pr-00-gate.yml"
LABEL_STEP = "Apply runtime acceptance-criteria label"


def _jobs():
    return yaml.safe_load(GATE.read_text(encoding="utf-8"))["jobs"]


def test_the_label_job_can_write_pull_requests_and_runs_no_pr_code():
    jobs = _jobs()
    holders = [
        name
        for name, job in jobs.items()
        if any(step.get("name") == LABEL_STEP for step in job.get("steps") or [])
    ]
    assert holders == ["acceptance-criteria-label"], holders
    job = jobs["acceptance-criteria-label"]
    assert job["permissions"] == {"issues": "write", "pull-requests": "write"}
    assert not any("checkout" in str(step.get("uses", "")) for step in job["steps"])
    assert "test-quality" in job["needs"]
    assert "deliberate_break_marker" in job["if"]
    assert "acceptance-criteria-label" not in jobs["summary"]["needs"]


def test_test_quality_keeps_pull_requests_read_and_exports_the_marker():
    job = _jobs()["test-quality"]
    assert job["permissions"]["pull-requests"] == "read"
    assert job["outputs"]["deliberate_break_marker"] == "${{ steps.deliberate_break.outputs.has_marker }}"
