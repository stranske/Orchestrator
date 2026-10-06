"""PR-fact preconditions must withhold offers (fact_missing), not flood wrong-moment declines."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

import capabilities
import capability_advisor as ca
import capability_propensity as cp


def test_missing_fact_events_deduplicate_per_surface_without_cross_counting(tmp_path):
    path = _ledger(tmp_path)
    exp = ca.experiment_id("same bounded assessment")
    assert cp.record_fact_missing(
        "runtime-ac-checks", exp, fact="PR size unknown", surface="one", path=path
    )
    assert cp.record_fact_missing(
        "runtime-ac-checks", exp, fact="PR size unknown", surface="two", path=path
    )
    assert not cp.record_fact_missing(
        "runtime-ac-checks", exp, fact="PR size unknown", surface="two", path=path
    )
    assert cp.record_fact_missing(
        "redirect-policy", exp, fact="history unknown", surface="one", path=path
    )
    assert cp.surface_fact_missing_total("one", path=path) == 2
    assert cp.surface_fact_missing_total("two", path=path) == 1
    assert cp.surface_fact_missing_total("unseen", path=path) == 0
    assert all(not t["candidates"] and not t["declined"] for t in cp.experiments(path=path))


def test_missing_fact_write_failures_are_visible_without_exception_payload(monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("secret=private")

    monkeypatch.setattr(cp, "record_fact_missing", fail)
    advice = {"experiment_id": ca.experiment_id("bounded assessment")}
    assert (
        ca._record_fact_missing(
            advice, [{"capability_id": "runtime-ac-checks"}], surface="closer-lane"
        )
        == 0
    )
    assert advice["fact_missing_record_errors"] == [
        {"capability_id": "runtime-ac-checks", "surface": "closer-lane", "error_type": "OSError"}
    ]
    assert "secret=private" not in str(advice)


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
    missing = {e["capability_id"]: e for e in result["fact_missing"]}
    assert missing["runtime-ac-checks"]["fact"] == "PR size unknown"
    assert missing["runtime-ac-checks"]["surface"] == "closer-lane"
    assert not (offered & missing.keys())
    output = ca.format_advice(result)
    assert "fact_missing on closer-lane: runtime-ac-checks — PR size unknown" in output
    assert "fact_missing on closer-lane: redirect-policy — PR labels unknown" in output
    row = capabilities.load(ledger)["runtime-ac-checks"]
    events = [
        ev
        for ev in row["event_history"]
        if (ev.get("metadata") or {}).get("source") == cp.FACT_MISSING_SOURCE
    ]
    assert events
    assert events[0]["type"] == "match"
    assert events[0]["metadata"]["surface"] == "closer-lane"
    assert events[0]["metadata"]["capability"] == "runtime-ac-checks"
    assert events[0]["metadata"]["fact"] == "PR size unknown"
    assert not any(
        (ev.get("metadata") or {}).get("source") == cp.DECLINE_SOURCE for ev in row["event_history"]
    )
    again = ca.advise(
        "closer: sweep the merge queue",
        surface="closer-lane",
        repository="stranske/Repo",
        record=True,
        path=ledger,
    )
    assert again["recorded_fact_missing"] == 0

    # A match against one task type can miss another. Withholding that match for missing facts
    # must not also report it as not applicable to this consult.
    mixed_ledger = tmp_path / "mixed" / "capabilities.json"
    row = capabilities._blank_capability("runtime-ac-checks")
    row["status"] = "generated"
    row["matcher"] = {"field": "task_type", "operator": "eq", "value": "runtime_ac"}
    capabilities.save({"runtime-ac-checks": row}, mixed_ledger)
    mixed = ca.advise("review acceptance criteria", record=False, path=mixed_ledger)
    assert set(mixed["task_types"]) == {"review", "runtime_ac"}
    assert "runtime-ac-checks" in {row["capability_id"] for row in mixed["fact_missing"]}
    assert "runtime-ac-checks" not in {row["capability_id"] for row in mixed["not_applicable"]}

    # A capability-specific eligibility filter must not swallow an unknown verdict before
    # the surface can report the missing fact, on either the bound-only or classified path.
    adjudicator_ledger = tmp_path / "adjudicator.json"
    row = capabilities._blank_capability("role-adjudicator")
    row["status"] = "generated"
    capabilities.save({"role-adjudicator": row}, adjudicator_ledger)
    for text in ("xyzzy plugh", "review disputed verifier results"):
        result = ca.advise(
            text,
            surface="closer-lane",
            repository="stranske/Repo",
            record=True,
            path=adjudicator_ledger,
        )
        assert result["capabilities"] == []
        assert result["fact_missing"] == [
            {
                "capability_id": "role-adjudicator",
                "surface": "closer-lane",
                "fact": "verifier verdict or merge disposition unknown",
            }
        ]
        assert result["recorded_fact_missing"] == 1
    events = [
        ev
        for ev in capabilities.load_declared(adjudicator_ledger)["role-adjudicator"][
            "event_history"
        ]
        if ev["type"] == "match"
    ]
    assert len(events) == 2
    assert all(ev["metadata"]["source"] == cp.FACT_MISSING_SOURCE for ev in events)
    counts = cp.surface_decline_counts("closer-lane", path=adjudicator_ledger)
    assert counts["offered"] == counts["declined"] == {}
    assert cp.surface_fact_missing_total("closer-lane", path=adjudicator_ledger) == 2


def test_known_precondition_true_offers_and_false_declines_as_precondition_unmet(
    tmp_path, monkeypatch
):
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
    assert by_id["redirect-policy"]["precondition_met"] is False
    assert by_id["redirect-policy"]["auto_declined"]["kind"] == "precondition_unmet"
    assert {row["capability_id"] for row in result["fact_missing"]} == {"adversarial-review"}

    result = ca.advise(
        "closer: stranske/Repo#1234 merge and verify",
        surface="closer-lane",
        repository="stranske/Repo",
        context={"changedFiles": 5, "labels": ["agent:retry"]},
        record=False,
        path=ledger,
    )
    by_id = {e["capability_id"]: e for e in result["capabilities"]}
    for name in ("runtime-ac-checks", "redirect-policy"):
        assert by_id[name]["precondition_met"] is True
        assert "auto_declined" not in by_id[name]
    assert {row["capability_id"] for row in result["fact_missing"]} == {"adversarial-review"}


def test_detect_reports_fact_missing_per_surface_and_never_as_a_decline(
    tmp_path, monkeypatch, capsys
):
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
    withheld = set(cp.experiments(path=ledger)[0]["fact_missing"])
    assert not (withheld & counts["offered"].keys())
    assert not (withheld & counts["declined"].keys())
    assert not (withheld & surf["declines"].keys())
    assert not (withheld & counts["declined_demotable"].keys())
    assert not any(d["capability_id"] in withheld for d in det["demotions"])

    monkeypatch.setattr(cp, "detect", lambda **kwargs: det)
    monkeypatch.setattr(cp, "_capability_heartbeat", lambda *args: None)
    assert cp.main(["detect"]) == 0
    output = capsys.readouterr().out
    assert "closer-lane" in output
    assert f"fact_missing {surf['fact_missing']} — the lane passed no PR facts" in output


def test_classification_miss_withholds_all_unknown_offers(tmp_path, monkeypatch):
    ledger = _ledger(tmp_path)
    monkeypatch.setitem(ca.SURFACE_BINDINGS, "test-missing-facts", {"runtime-ac-checks": "test"})
    result = ca.advise(
        "xyzzy plugh frobnicate",
        skill="test-missing-facts",
        record=True,
        path=ledger,
    )
    assert result["task_types"] == []
    assert result["capabilities"] == []
    assert result["useful"] is False
    assert result["fact_missing"] == [
        {
            "capability_id": "runtime-ac-checks",
            "surface": "test-missing-facts",
            "fact": "PR size unknown",
        }
    ]
    assert result["recorded_fact_missing"] == 1
    assert cp.surface_fact_missing_total("test-missing-facts", path=ledger) == 1
    output = ca.format_advice(result)
    assert "fact_missing on test-missing-facts: runtime-ac-checks — PR size unknown" in output


def test_context_facts_evaluate_without_a_pr_number(tmp_path, monkeypatch):
    ledger = _ledger(tmp_path)

    def unexpected_fetch(*args):
        raise AssertionError("a consult without a PR number must use its context facts")

    monkeypatch.setattr(ca, "PR_FACTS_FETCH", unexpected_fetch)
    # Empty lists and zero counts are known facts; they must not become unknown values.
    context = {
        "changedFiles": 0,
        "additions": 0,
        "deletions": 0,
        "paths": [],
        "labels": [],
        "title": "tiny",
        "task_text": "coordinate stranske/Repo#1 and stranske/Other#2",
    }
    result = ca.advise(
        "closer: merge and verify",
        surface="closer-lane",
        repository="stranske/Repo",
        context=context,
        record=False,
        path=ledger,
    )
    by_id = {e["capability_id"]: e for e in result["capabilities"]}
    assert {row["capability_id"] for row in result["fact_missing"]} == {"adversarial-review"}
    assert by_id["runtime-ac-checks"]["auto_declined"]["kind"] == "scope_too_small"
    assert by_id["redirect-policy"]["auto_declined"]["kind"] == "precondition_unmet"
    assert by_id["cross-repo-coordination"]["precondition_met"] is True


def test_help_documents_the_pr_fact_context_fields():
    proc = subprocess.run(
        [sys.executable, str(Path(ca.__file__)), "--help"],
        capture_output=True,
        text=True,
        check=True,
    )
    for field in (
        "changedFiles",
        "additions",
        "deletions",
        "paths",
        "labels",
        "title",
        "task_text",
    ):
        assert field in proc.stdout


def test_empty_and_suppressed_consults_return_an_empty_fact_missing_list(tmp_path):
    ledger = _ledger(tmp_path)
    for text, surface in (("xyzzy plugh", ""), ("add unit tests", "repo-audit:phase-1")):
        result = ca.advise(text, surface=surface, record=False, path=ledger)
        assert result["capabilities"] == []
        assert result["fact_missing"] == []


@pytest.mark.parametrize("last_match,last_invocation", [(None, None), (10, 20)])
def test_fact_missing_keeps_event_without_advancing_offer_liveness(
    tmp_path, last_match, last_invocation
):
    ledger = tmp_path / "liveness-ledger.json"
    cap = capabilities._blank_capability("missing-fact-fixture")
    rows = {"missing-fact-fixture": cap}
    cap.update(status="wired", last_match=last_match, last_invocation=last_invocation)
    capabilities.save(rows, ledger)
    before = capabilities.classify_liveness(cap, now=100)
    assert capabilities.heartbeat(
        "missing-fact-fixture",
        "match",
        ref="advice:missing-fact",
        timestamp=100,
        metadata={
            "source": cp.FACT_MISSING_SOURCE,
            "surface": "closer-lane",
            "fact": "PR size unknown",
        },
        path=ledger,
    )
    after = capabilities.load(ledger)["missing-fact-fixture"]
    assert after["last_match"] == last_match
    assert capabilities.classify_liveness(after, now=100) == before
    assert after["event_history"][-1]["metadata"]["source"] == cp.FACT_MISSING_SOURCE
    assert capabilities.heartbeat(
        "missing-fact-fixture", "match", ref="advice:actual-offer", timestamp=101, path=ledger
    )
    assert capabilities.load(ledger)["missing-fact-fixture"]["last_match"] == 101
