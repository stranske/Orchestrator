"""Reviewed absence is visible debt and never consumer acceptance (source #599)."""

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
