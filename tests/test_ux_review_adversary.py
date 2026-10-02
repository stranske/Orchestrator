import hashlib
import json

import ux_review as ur

# The adversary output this module tests against, kept IN the module so the test runs in every tree.
#
# Provenance: PR #349 (issue #325) added it as tests/fixtures/ux_review_adversarial_2026_09_22.txt,
# a fixture copy (`run_id=fixture`) of the gemini critic's output in the trip-planner UX review of
# 2026-09-22: six findings, two at severity 4 with stuck_probability 1.0 and 0.9, all of which the
# old extractor dropped because it required a "scores" key. It moved here byte-for-byte on
# 2026-10-02 because the exec mirror never had it: orch-sync-mirror.sh then shipped tests/*.py and
# tests/rail_exercises and nothing else under tests/, so this test was the one failure of a mirror
# verify while passing in every checkout and in CI. (The sync now ships all of tests/ from
# `git archive HEAD`, and tests/conftest.py fails a test that reads an input git would leave out.)
# The header line and the ```json fence are part of the capture, not decoration: they are what the
# parser has to see through.
ADVERSARY_OUTPUT_2026_09_22 = """\
=== 2026-09-22T18:20:00Z UX-ADVERSARY gemini/full run_id=fixture ===
```json
{
  "findings": [
    {"dimension":"adversarial","severity":4,"screen":"Plan","element":"Generate plan","failure_mode":"false_success","click_path":["open Plan","click Generate plan"],"stuck_probability":1.0},
    {"dimension":"adversarial","severity":4,"screen":"Budget","element":"Estimated total","failure_mode":"fabricated_output","click_path":["open Budget","change destination"],"stuck_probability":0.9},
    {"dimension":"adversarial","severity":3,"screen":"Itinerary","element":"Empty day","failure_mode":"recovery_failure","click_path":["open Itinerary","remove final item"],"stuck_probability":0.8},
    {"dimension":"adversarial","severity":3,"screen":"Search","element":"No results","failure_mode":"missing_help","click_path":["open Search","enter unknown place"],"stuck_probability":0.7},
    {"dimension":"adversarial","severity":2,"screen":"Compare","element":"Option cards","failure_mode":"confusion","click_path":["open Compare","select two options"],"stuck_probability":0.6},
    {"dimension":"adversarial","severity":2,"screen":"Checkout","element":"Back button","failure_mode":"efficiency_trap","click_path":["open Checkout","click Back"],"stuck_probability":0.5}
  ],
  "worst_case": "Generate plan claims success while no usable itinerary exists",
  "evidence_gaps": ["No offline recovery path was exercised"]
}
```
"""
# The git blob id of the file #349 committed (`git cat-file -p` it to compare), so editing the
# capture fails here instead of quietly changing what the test below parses.
ADVERSARY_OUTPUT_2026_09_22_BLOB = "6d4859d9de04ed0f0dbc12142ebfbad96a0cd8b4"


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
    raw = ADVERSARY_OUTPUT_2026_09_22.encode()
    blob = hashlib.sha1(b"blob %d\0" % len(raw) + raw, usedforsecurity=False).hexdigest()
    assert (
        blob == ADVERSARY_OUTPUT_2026_09_22_BLOB
    ), "the 2026-09-22 capture was edited; a new capture belongs in a new test"
    parsed = ur.parse_adversarial_output(ADVERSARY_OUTPUT_2026_09_22)
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
    for invalid_probability in (
        None,
        "bad",
        float("nan"),
        float("inf"),
        -0.1,
        1.1,
        10**309,
        True,
    ):
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
