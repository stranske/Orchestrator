"""Lock the PR #793 acceptance record so the recorded revisions stay checkable."""

from __future__ import annotations

from pathlib import Path

import synthesis_promotion

REPO = Path(__file__).resolve().parents[1]
EVIDENCE = REPO / "docs" / "verification" / "issue-1109-pr-793-acceptance.md"
PRODUCTION = Path(synthesis_promotion.__file__).resolve()

REQUIRED = (
    "574c1694b2990e50ce0668589472e180f481019c",
    "2f12281b795d749498e5641edcf32ef41420f499",
    "891a157836ac6556bbecd4dc192680cb5b9c3933",
    "39260265878",
    "5791 passed, 0 failed",
    "test_completion_probe_requires_a_success_marker_and_new_head",
    "test_run_gate_never_runs_non_allowlisted_command",
    "test_verify_synthesis_records_unallowlisted_local_command_without_spawning",
    "subprocess must not run an unallowlisted gate",
    "rejected command must not reach local verification",
    "synthesis marker rc=0",
    "branch_coverage: false",
    "26 uncovered statements",
    "154",
    "historical",
    "ORCH_STATE_DIR",
    "ORCH_LOCAL_RUNTIME",
    "No new CI run was executed",
)


def test_acceptance_evidence_records_revisions_ci_and_mutations() -> None:
    text = EVIDENCE.read_text(encoding="utf-8")
    missing = [needle for needle in REQUIRED if needle not in text]
    assert missing == []
    assert "local partial" in text.lower() or "Local partial" in text


def test_deliberate_production_mutations_are_not_in_the_tree() -> None:
    source = PRODUCTION.read_text(encoding="utf-8")
    assert "        if rc != 0:\n" in source
    assert "    if not _safe_command(argv):\n" in source
    assert "        if not _safe_command(local_argv):\n" in source
    assert "        if rc == 0:\n" not in source
