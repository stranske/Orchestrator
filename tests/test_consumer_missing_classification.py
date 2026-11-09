"""Reviewed absence is visible debt and never consumer acceptance (source #599)."""

from unittest.mock import patch

import capabilities
import capability_admission as admission


def test_reviewed_absence_stays_missing_even_with_a_resolvable_token():
    for consumer in ("the external repair issues", "merge_guard.py:evaluate_merge_gate"):
        cap = {"downstream_consumer": consumer, "consumer_missing": True}
        cls, ok, detail = admission.consumer_class(cap, {})
        assert cls == "consumer_missing", detail
        assert not ok, detail
        assert not admission.req_consumer_acts(cap, {})[0]


def test_consumer_report_counts_reviewed_absence_without_granting_acceptance():
    ledger = {
        "absent": {"downstream_consumer": "external repair issues", "consumer_missing": True},
        "broken": {"downstream_consumer": "missing_module.py:missing_consumer"},
    }
    rows = [
        {"capability_id": cid, "blocking": ["consumer_acts"], "deferred": [], "enforced": True}
        for cid in ledger
    ]
    consumers = admission.consumer_report(ledger, {}, rows)
    assert consumers["by_class"]["prose"] == []
    assert consumers["by_class"]["consumer_missing"] == ["absent"]
    assert consumers["by_class"]["unresolved"] == ["broken"]
    assert consumers["drainable"] == ["absent", "broken"]
    assert consumers["blocking"] == ["absent", "broken"]
    assert not consumers["drained"]
    report = {
        "total": 2,
        "admitted": 0,
        "enforced_total": 2,
        "enforced_failing": [],
        "legacy_debt": [],
        "rows": [],
        "consumers": consumers,
        "commitments": {"clean": True, "dangling_citations": [], "overdue_without_record": []},
    }
    rendered = admission.format_report(report)
    assert "0 of 2 declare a consumer this tree can find" in rendered
    assert "prose 0, missing 2" in rendered
    assert "reviewed absent 1, unresolved 1" in rendered
    assert "DRAINABLE 2" in rendered


def test_unmarked_prose_and_undeclared_consumer_are_still_rejected():
    cls, ok, _ = admission.consumer_class({"downstream_consumer": "some reader"}, {})
    assert cls == "prose" and not ok
    cls, ok, _ = admission.consumer_class({"consumer_missing": True}, {})
    assert cls == "undeclared" and not ok


def test_reconciliation_clears_reviewed_absence_when_consumer_is_replaced():
    cid = "issue599-consumer-transition"
    cap = capabilities._blank_capability(cid)
    cap.update(status="active", downstream_consumer="external repair issues", consumer_missing=True)
    cap["event_history"] = [{"type": "invocation", "timestamp": 1}]
    ledger = {cid: cap}
    with patch.dict(
        capabilities.KNOWN_EXISTING_DECLARATIONS,
        {cid: {"downstream_consumer": "merge_guard.py:evaluate_merge_gate"}},
    ):
        assert capabilities._reconcile_known_declarations(ledger, 2)
        assert "consumer_missing" not in cap
        assert admission.consumer_resolution(cap)["resolved"]
        assert admission.consumer_class(cap, {})[0] != "consumer_missing"
        assert cap["status"] == "active" and cap["outcome_links"] == []
        assert cap["event_history"][0] == {"type": "invocation", "timestamp": 1}
        assert cap["event_history"][-1]["changed_fields"] == [
            "consumer_missing",
            "downstream_consumer",
        ]
        assert not capabilities._reconcile_known_declarations(ledger, 3)


def test_reconciliation_preserves_explicit_missing_consumer_values():
    cid = "issue599-explicit-missing"
    for explicit in (True, False):
        cap = capabilities._blank_capability(cid)
        cap.update(downstream_consumer="old prose", consumer_missing=not explicit)
        with patch.dict(
            capabilities.KNOWN_EXISTING_DECLARATIONS,
            {
                cid: {
                    "downstream_consumer": "merge_guard.py:evaluate_merge_gate",
                    "consumer_missing": explicit,
                }
            },
        ):
            assert capabilities._reconcile_known_declarations({cid: cap}, 2)
            assert cap["consumer_missing"] is explicit


def test_declaration_without_consumer_ownership_does_not_clear_reviewed_absence():
    cid = "issue599-unowned-consumer"
    cap = capabilities._blank_capability(cid)
    cap.update(downstream_consumer="old prose", consumer_missing=True)
    with patch.dict(capabilities.KNOWN_EXISTING_DECLARATIONS, {cid: {"notes": "updated note"}}):
        assert capabilities._reconcile_known_declarations({cid: cap}, 2)
        assert cap["consumer_missing"] is True
