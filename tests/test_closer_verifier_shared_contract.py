"""Shared Workflows #4252 contract bytes consumed by the native verifier parser."""

import json
from pathlib import Path

import verification_events
import verifier_evidence

FIXTURE = Path(__file__).parent / "fixtures/verifier_request_correlation.json"
# Copied unchanged from Workflows 5472f91a94fedfb04545b4db2558e6dc49c2e0ad,
# tests/fixtures/verification_event/request_correlation.json.


def test_actual_workflows_fixture_roundtrips_without_replacing_selected_verdict():
    fixture = json.loads(FIXTURE.read_text())
    decision, alias = fixture["decision"], fixture["correlation"]
    source = fixture["source_comment"]
    comments = [
        {**source, "author": {"login": source["author"]}},
        {
            "author": {"login": "github-actions[bot]"},
            "url": "https://github.com/stranske/example/pull/7#issuecomment-44",
            "body": "<!-- verifier-request-correlation/v1 " + json.dumps(alias) + " -->",
        },
    ]
    selected = verifier_evidence.decision_from_pr(
        "stranske/example",
        {
            "number": 7,
            "headRefOid": decision["head_sha"],
            "mergeCommit": {"oid": decision["evaluated_sha"]},
            "comments": {"nodes": comments},
        },
    )
    assert selected["verdict"] == "NON_PASS"
    assert selected["run_id"] == "100" and selected["run_attempt"] == "2"
    assert "verification_request_basis" not in selected
    assert selected["_request_correlations"][0]["record"] == alias
    assert verification_events.content_hash(decision) == alias["result_decision_sha256"]
    assert (
        json.dumps(decision, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        == fixture["canonical_decision"]
    )
    unicode = fixture["unicode_canonical_example"]
    assert verification_events.content_hash(unicode["value"]) == unicode["sha256"]
