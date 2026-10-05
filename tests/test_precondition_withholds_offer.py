"""PR-fact preconditions must withhold offers (fact_missing), not flood wrong-moment declines."""

from __future__ import annotations

from pathlib import Path

import capabilities
import capability_advisor as ca
import capability_propensity as cp

CLOSER_BOUND = (
    "adversarial-review",
    "runtime-ac-checks",
    "cross-repo-coordination",
    "offload",
    "redirect-policy",
    "redirect-plan",
)


def _ledger(tmp_path: Path) -> Path:
    path = tmp_path / "capabilities.json"
    rows = {}
    for name in CLOSER_BOUND:
        row = capabilities._blank_capability(name)
        row["status"] = "generated"
        rows[name] = row
    capabilities.save(rows, path)
    return path


def test_unknown_precondition_withholds_the_offer_and_records_fact_missing_on_the_surface(
    tmp_path, monkeypatch
):
    ledger = _ledger(tmp_path)
    monkeypatch.setattr(ca, "PR_FACTS_FETCH", lambda *a, **k: None)

    result = ca.advise(
        "closer: sweep the merge queue",
        surface="closer-lane",
        repository="stranske/Repo",
        record=True,
        path=ledger,
    )
    offered = {e["capability_id"] for e in result["capabilities"]}
    assert "runtime-ac-checks" not in offered
    assert "redirect-policy" not in offered
    assert result["fact_missing"]
    assert result["recorded_fact_missing"] >= 1
    row = capabilities.load(ledger)["runtime-ac-checks"]
    events = [
        ev
        for ev in row["event_history"]
        if (ev.get("metadata") or {}).get("source") == cp.FACT_MISSING_SOURCE
    ]
    assert events
    assert events[0]["metadata"]["surface"] == "closer-lane"


def test_known_precondition_true_offers_and_false_declines_as_precondition_unmet(tmp_path, monkeypatch):
    ledger = _ledger(tmp_path)

    def fetch(repository, pr):
        return {
            "number": pr,
            "labels": [],
            "changedFiles": 1,
            "additions": 2,
            "deletions": 1,
            "title": "tiny",
            "paths": ["a.py"],
        }

    monkeypatch.setattr(ca, "PR_FACTS_FETCH", fetch)
    result = ca.advise(
        "closer: stranske/Repo#1234 merge and verify",
        surface="closer-lane",
        repository="stranske/Repo",
        record=False,
        path=ledger,
    )
    by_id = {e["capability_id"]: e for e in result["capabilities"]}
    assert "runtime-ac-checks" in by_id
    assert by_id["runtime-ac-checks"]["auto_declined"]["kind"] == "scope_too_small"
    assert not result["fact_missing"]


def test_detect_reports_fact_missing_per_surface_and_never_as_a_decline(tmp_path, monkeypatch):
    ledger = _ledger(tmp_path)
    monkeypatch.setattr(ca, "PR_FACTS_FETCH", lambda *a, **k: None)
    ca.advise(
        "closer: sweep the merge queue",
        surface="closer-lane",
        repository="stranske/Repo",
        record=True,
        path=ledger,
    )
    det = cp.detect(path=ledger)
    surf = det["surfaces"]["closer-lane"]
    assert surf["fact_missing"] >= 1
    counts = cp.surface_decline_counts("closer-lane", path=ledger)
    assert counts["offered"].get("runtime-ac-checks", 0) == 0
