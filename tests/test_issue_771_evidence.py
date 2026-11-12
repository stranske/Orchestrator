"""Guard the preserved issue-#771 repair evidence against unsupported claims."""

from __future__ import annotations

import hashlib
import json
import tomllib
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


def test_raw_coverage_figures_and_provenance_match_documentation():
    directory = EVIDENCE_DIR / "coverage"
    raw = (directory / "run-39180639406-artifact-12156286098-coverage.json").read_bytes()
    report = json.loads(raw)
    metadata = json.loads((directory / "artifact-12156286098-metadata.json").read_text())
    summary = (directory / "SUMMARY.md").read_text()
    evidence = (EVIDENCE_DIR / "EVIDENCE.md").read_text()
    assert hashlib.sha256(raw).hexdigest() in summary
    assert metadata["id"] == 12156286098
    assert metadata["name"] == "gate-coverage"
    assert metadata["workflow_run"]["id"] == 39180639406
    assert metadata["digest"].removeprefix("sha256:") in summary
    assert report["meta"]["branch_coverage"] is False
    target = report["files"]["src/feedback.py"]
    counts = target["summary"]
    assert counts["missing_lines"] == len(target["missing_lines"])
    assert counts["covered_lines"] == len(target["executed_lines"])
    assert counts["num_statements"] == counts["covered_lines"] + counts["missing_lines"]
    assert "num_branches" not in counts and "missing_branches" not in counts
    assert f'{counts["missing_lines"]} of {counts["num_statements"]} statements' in summary
    assert f'{counts["covered_lines"]} covered, {counts["percent_covered_display"]}%' in summary
    for label, key in (
        ("Statements (`num_statements`)", "num_statements"),
        ("Missing statements (`missing_lines`)", "missing_lines"),
        ("Covered statements", "covered_lines"),
    ):
        row = next(line for line in evidence.splitlines() if line.startswith(f"| {label} |"))
        assert row.split("|")[2].strip().strip("*") == str(counts[key])
    assert (
        f'| Statement coverage (report, rounded) | {counts["percent_covered_display"]}% |'
        in evidence
    )
    for doc in (summary, evidence):
        assert "shortfall" in doc and "unknown" in doc
        assert "thousands of lines" not in doc
        assert "by itself exceeds" not in doc


def test_event_head_is_distinct_from_executed_checkout():
    metadata = json.loads(
        (EVIDENCE_DIR / "coverage" / "artifact-12156286098-metadata.json").read_text()
    )
    event_head = metadata["workflow_run"]["head_sha"]
    checkout = "12c5c58517863f747972538c9bb17c568bc0bf49"
    assert event_head == "18ad97a054dab7f6039dd42c8131bee04267748b"
    assert checkout != event_head
    for name, line_number in zip(CI_LOGS, (128, 185, 185)):
        lines = (EVIDENCE_DIR / "ci" / name).read_text().splitlines()
        assert f"HEAD is now at {checkout[:7]} Merge {event_head}" in lines[line_number - 1]
        assert checkout in "\n".join(lines)
    evidence = (EVIDENCE_DIR / "EVIDENCE.md").read_text()
    assert f"**Event headSha (not executed checkout):** `{event_head}`" in evidence
    assert f"**Executed synthetic merge checkout:** `{checkout}`" in evidence


def test_controlled_comparison_uses_verified_configs_and_separate_units():
    directory = EVIDENCE_DIR / "coverage"
    comparison = json.loads((directory / "comparison.json").read_text())
    raw = (directory / comparison["raw_report"]["file"]).read_bytes()
    report = json.loads(raw)
    assert comparison["raw_report"]["sha256"] == hashlib.sha256(raw).hexdigest()
    assert comparison["target"] == "src/feedback.py"
    assert comparison["baseline_commit"] == "3b379f8b60046b1b6b21b360a931472d0b6db9c3"
    assert comparison["event_head"] == "18ad97a054dab7f6039dd42c8131bee04267748b"
    assert comparison["report_checkout"] == "12c5c58517863f747972538c9bb17c568bc0bf49"
    assert set(comparison["configurations"]) == {
        comparison["baseline_commit"],
        comparison["event_head"],
    }
    for records in comparison["configurations"].values():
        assert {record["historical_path"] for record in records} == {
            ".github/workflows/ci.yml",
            "pyproject.toml",
            "src/verify.py",
        }
        for record in records:
            content = (directory / record["preserved_file"]).read_bytes()
            assert hashlib.sha256(content).hexdigest() == record["sha256"]
            blob = hashlib.sha1(f"blob {len(content)}\0".encode() + content).hexdigest()
            assert blob == record["git_blob"]
            if record["historical_path"] == "pyproject.toml":
                config = tomllib.loads(content.decode())["tool"]["coverage"]["run"]
                assert config["branch"] is False
                assert config["parallel"] is True and config["source"] == ["src"]
            elif record["historical_path"] == ".github/workflows/ci.yml":
                assert b"python3 src/verify.py --coverage --floor-may-lag" in content
    counts = report["files"][comparison["target"]]["summary"]
    after = comparison["statements"]["after"]
    assert after == {key: counts[key] for key in after}
    assert set(after) == {"num_statements", "covered_lines", "missing_lines"}
    assert after["covered_lines"] + after["missing_lines"] == after["num_statements"]
    assert comparison["statements"]["baseline"] is None
    assert comparison["statements"]["missing_reduction"] is None
    for metric in ("baseline", "after", "missing_reduction"):
        assert comparison["branches"][metric] is None
    assert comparison["combined_reduction"] is None
    assert comparison["goal_shortfall"] is None
    assert comparison["historical_ranking_provenance"] == "unknown"
    assert comparison["fresh_coverage_executed"] is False


def test_isolated_original_five_transcript_preserves_command_and_result():
    transcript = (EVIDENCE_DIR / "ci" / "original-five-18ad97a.log").read_text()
    assert "HEAD: 18ad97a054dab7f6039dd42c8131bee04267748b" in transcript
    assert "Checkout: /private/tmp/issue771-original-five-" in transcript
    assert (
        "Command: /opt/anaconda3/bin/python3 -m pytest -q "
        "tests/test_feedback_registry_queries.py\n"
    ) in transcript
    assert "5 passed" in transcript
    assert "Exit status: 0" in transcript
