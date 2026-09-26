import json
from pathlib import Path

import ux_review as ur

FIXTURE = Path(__file__).parent / "fixtures" / "ux_review_adversarial_2026_09_22.txt"


def _clean_panel() -> dict[str, dict]:
    return {
        evaluator: {
            "scores": {dimension: 9 for dimension in ur.DIMENSIONS},
            "overall": 9,
            "findings": [],
            "evidence_gaps": [],
        }
        for evaluator in ("claude", "codex", "cursor", "gemini")
    }


def test_2026_09_22_adversary_fixture_survives_aggregation() -> None:
    parsed = ur.parse_adversarial_output(FIXTURE.read_text())
    report = ur.aggregate_panel(_clean_panel(), parsed, 4)

    assert len(report["adversarial"]["findings"]) == 6
    assert len(report["findings"]) == 6
    assert len(report["blockers"]) == 2
    assert {finding["failure_mode"] for finding in report["findings"]} == {
        "confusion",
        "efficiency_trap",
        "fabricated_output",
        "false_success",
        "missing_help",
        "recovery_failure",
    }
    assert {finding["failure_mode"] for finding in report["blockers"]} == {
        "fabricated_output",
        "false_success",
    }
    assert report["adversarial"]["parse_error"] is None
    assert report["adversarial"]["worst_case"].startswith("Generate plan")


def test_nonempty_unparseable_adversary_output_blocks_gate() -> None:
    parsed = ur.parse_adversarial_output("critic failed before returning JSON")
    report = ur.aggregate_panel(_clean_panel(), parsed, 4)
    decision = ur.gate_decision({"ok": True}, report)

    assert report["adversarial"]["parse_error"] == "nonempty_unparseable_output"
    assert decision["done"] is False
    assert "adversarial_parse_error" in decision["reasons"]


def test_empty_adversary_output_remains_distinct_from_parse_error() -> None:
    report = ur.aggregate_panel(_clean_panel(), ur.parse_adversarial_output(""), 4)

    assert report["adversarial"]["parse_error"] is None


def test_adversary_findings_must_be_a_list_of_objects() -> None:
    for output in ('{"findings": null}', '{"findings": ["not an object"]}'):
        parsed = ur.parse_adversarial_output(output)
        report = ur.aggregate_panel(_clean_panel(), parsed, 4)
        decision = ur.gate_decision({"ok": True}, report)

        assert report["adversarial"]["findings"] == []
        assert report["adversarial"]["parse_error"] == "invalid_findings_schema"
        assert decision["done"] is False
        assert "adversarial_parse_error" in decision["reasons"]


def test_adversary_findings_require_bounded_finite_probability() -> None:
    for invalid_probability in (None, "bad", float("nan"), float("inf"), -0.1, 1.1, True):
        parsed = ur.parse_adversarial_output(
            json.dumps({"findings": [{"stuck_probability": invalid_probability, "severity": 2}]})
        )

        assert parsed == {"parse_error": "invalid_findings_schema", "findings": []}


def test_adversary_findings_require_integer_severity_in_documented_range() -> None:
    for invalid_severity in (None, "bad", 2.5, -1, 5, True):
        parsed = ur.parse_adversarial_output(
            json.dumps({"findings": [{"stuck_probability": 0.5, "severity": invalid_severity}]})
        )

        assert parsed == {"parse_error": "invalid_findings_schema", "findings": []}
