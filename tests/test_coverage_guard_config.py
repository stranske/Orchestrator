"""Regression checks for the Gate-to-coverage-guard artifact contract."""

from __future__ import annotations

import re

import paths


WORKFLOW = paths.REPO_ROOT / ".github" / "workflows" / "maint-coverage-guard.yml"


def test_coverage_guard_requires_only_the_gate_payload():
    """Optional trend downloads must not disqualify an otherwise usable Gate payload."""
    workflow = WORKFLOW.read_text(encoding="utf-8")
    match = re.search(
        r"const requiredCoverageArtifactNames = new Set\((.*?)\);",
        workflow,
        re.S,
    )
    assert match, "coverage-guard artifact discovery set is missing"

    required = set(re.findall(r"'([^']+)'", match.group(1)))
    assert required == {"gate-coverage"}, (
        "the coverage payload is mandatory; trend/history artifacts are best-effort inputs "
        "handled by the guard runner when present"
    )

    for step_name, artifact in (
        ("Download coverage trend artifact", "gate-coverage-trend"),
        ("Download coverage trend history artifact", "gate-coverage-trend-history"),
    ):
        block = workflow.split(f"- name: {step_name}", 1)[1].split("\n      - name:", 1)[0]
        assert f"name: {artifact}" in block, f"{step_name} no longer requests {artifact}"
        assert "continue-on-error: true" in block, f"{artifact} must remain optional"
