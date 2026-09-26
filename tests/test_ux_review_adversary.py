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
