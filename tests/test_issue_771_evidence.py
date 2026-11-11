"""Guard the preserved issue-#771 repair evidence against unsupported claims."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE_DIR = ROOT / "docs" / "evidence" / "issue-771-repair"

ORIGINAL_FIVE = [
    "test_run_queries_keep_pending_and_mode_scopes_distinct",
    "test_snapshot_json_exports_initialized_tables_to_the_requested_path",
    "test_evidence_registry_normalizes_citations_and_retires_unused_rows",
    "test_evidence_proposals_and_approval_require_recurrence_then_reactivate",
    "test_role_selector_metrics_preserve_no_match_and_rejection_signals",
]

CI_LOGS = [
    "run-39180639406-job-117832439061.log",
    "run-39180639803-job-117832552036.log",
    "run-39180639803-job-117832552676.log",
]


def test_mutation_records_cover_all_five_original_tests():
    for test_id in ORIGINAL_FIVE:
        record = EVIDENCE_DIR / "mutations" / f"{test_id}.md"
        assert record.is_file(), record
        text = record.read_text()
        assert "18ad97a054dab7f6039dd42c8131bee04267748b" in text
        assert "## Patch" in text
        assert "## Failure output" in text and "1 failed" in text
        assert "## Restored pass output" in text and "1 passed" in text


def test_preserved_ci_logs_are_indexed_and_nonempty():
    evidence = (EVIDENCE_DIR / "EVIDENCE.md").read_text()
    for name in CI_LOGS:
        log = EVIDENCE_DIR / "ci" / name
        assert log.is_file() and log.stat().st_size > 0, log
        assert name in evidence
    gate_log = (EVIDENCE_DIR / "ci" / CI_LOGS[1]).read_bytes()
    assert b"5429 passed, 41 skipped" in gate_log
    for test_id in ORIGINAL_FIVE:
        assert test_id.encode() in gate_log


def test_coverage_summary_separates_statements_from_branches():
    summary = (EVIDENCE_DIR / "coverage" / "SUMMARY.md").read_text()
    assert "branch = false" in summary
    assert "Missing statements" in summary
    assert "Missing branches" in summary
    assert "unknown" in summary.lower()
    commits = (
        "18ad97a054dab7f6039dd42c8131bee04267748b",
        "3b379f8b60046b1b6b21b360a931472d0b6db9c3",
    )
    for commit in commits:
        assert commit in summary
        for kind in ("ci", "pyproject", "verify"):
            prefix = "ci" if kind == "ci" else kind
            suffix = "yml" if kind == "ci" else ("toml" if kind == "pyproject" else "py")
            assert (EVIDENCE_DIR / "coverage" / f"{prefix}-{commit[:7]}.{suffix}").is_file()


def test_evidence_makes_no_unsupported_combined_claims():
    evidence = (EVIDENCE_DIR / "EVIDENCE.md").read_text()
    assert "large shortfall" not in evidence
    assert "best available Gate-comparable" not in evidence
    assert "with raw JSON reports" not in evidence
    assert "unknown" in evidence.lower()
    for test_id in ORIGINAL_FIVE:
        assert f"mutations/{test_id}.md" in evidence
