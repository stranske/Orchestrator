#!/usr/bin/env python3
"""test_capability_admission.py — the admission gate, wired into the suite so it cannot be skipped.

`capability_admission.py` can compute everything it needs on demand, which is exactly why it needs
this file: a module nobody calls is the defect this project keeps re-committing (`watch.py` and
`features.py` each shipped complete, selftested, and uninvoked). The gate has to be a CALLER of
itself before it is entitled to lecture anything else about callers.

Scope is deliberate. Enforcement binds on capabilities registered from 2026-08-21 onward; the 36
pre-gate capabilities are reported as legacy debt on every run and do not fail the suite. A gate
that is red on arrival gets switched off, and then it protects nothing — so the trade is: block the
next mistake, keep the old debt visible.
"""
from __future__ import annotations

import sys

import capabilities
import capability_admission as admission


def test_new_capabilities_carry_all_required_parts():
    """A capability added since the gate must bring its caller, heartbeat, fixture and outcome path.

    The eight requirements are not a wish list; each one is a defect that actually happened. Adding
    a capability without them is how six subsystems went dormant, how `issue-readiness` shipped with
    no heartbeat, and how `reference-sync-hygiene` accrued 367 events its own gate could not read.
    """
    rep = admission.report()
    failing = rep["enforced_failing"]
    rows = {r["capability_id"]: r for r in rep["rows"]}
    detail = {cid: rows[cid]["missing"] for cid in failing}
    assert not failing, (
        f"{len(failing)} capability(ies) added since the admission gate are missing required "
        f"parts: {detail}. Declare them in the ledger, or add an expiring WAIVERS entry with a "
        f"reason — never leave it undeclared."
    )


def test_dated_promises_left_an_artifact():
    """A cited decision record must exist, and a passed deadline must have produced one.

    This is the 2026-07-15 range-lane failure expressed as a test: the review fired
    (`lastRunAt` 2026-07-15T18:00:04Z), wrote nothing, the flag auto-reverted the next day and
    stayed off 36 days, and `orchestrate.sh:95` cited the record nobody wrote. Nothing in the
    system objected. Now something does.
    """
    com = admission.commitments()
    assert not com["dangling_citations"], (
        "code or docs cite a dated record that does not exist — either write the record or drop "
        f"the citation: {com['dangling_citations']}"
    )
    assert not com["overdue_without_record"], (
        "a deadline passed with no record naming its subject. A bounded trial must end by a "
        f"DECISION, not by timeout: {com['overdue_without_record']}"
    )


def test_every_requirement_can_fail():
    """A predicate that cannot fail is decoration, and decoration is what prose rules turned out to be."""
    ctx = {"audit_rows": {}, "fixtures": set()}
    empty = capabilities._blank_capability("capability:nothing-declared")
    empty["event_history"] = [{"timestamp": admission._now(), "type": "migrated"}]
    for name, fn in admission.REQUIREMENTS:
        try:
            ok, _ = fn(empty, ctx)
        except Exception:                                          # noqa: BLE001
            ok = False
        assert not ok, f"requirement {name!r} passes a capability that declares nothing"


def test_legacy_debt_is_reported_not_forgiven():
    """Scoping must not become amnesty.

    Legacy capabilities are excluded from FAILING, never from REPORTING. If the exemption were
    pushed into the predicates instead of the row, the debt would read as compliance — which is
    precisely how "all findings resolved; nothing dropped" coexisted with 13 blocked capabilities.
    """
    rep = admission.report()
    legacy = [r for r in rep["rows"] if r["legacy"]]
    assert legacy, "expected pre-gate capabilities to be marked legacy"
    assert any(r["missing"] for r in legacy), "legacy rows must still name what they are missing"
    assert all(not r["enforced"] for r in legacy)
    assert isinstance(rep["legacy_debt"], list)


def test_waivers_are_bounded_not_just_dated():
    """An exception with no end date — or a fake one — is how "temporary" lasted a month at a time.

    A break-test set a waiver expiry to 9999999999 and every check still passed: the waiver had "an
    expiry", so the rule was satisfied while the exemption was permanent in practice. Requiring the
    expiry to be WITHIN a horizon is what makes it a real deadline.
    """
    ledger = capabilities.load(capabilities.REG)
    for cap_id in admission.WAIVERS:
        assert cap_id in ledger, f"WAIVERS names unknown capability {cap_id!r}"
    problems = admission.waiver_problems()
    assert not problems, f"illegitimate waivers: {problems}"


def test_the_gate_admits_itself():
    """Dogfooding, and not for style: a gate exempt from its own rule is the rule being optional."""
    own = admission.admit("capability-admission-gate")
    assert own["admitted"], f"the admission gate fails its own requirements: {own['missing']}"


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failures = []
    for fn in tests:
        try:
            fn()
            print(f"  OK   {fn.__name__}")
        except AssertionError as exc:
            failures.append(fn.__name__)
            print(f"  FAIL {fn.__name__}")
            print(f"       {str(exc)[:400]}")
    if failures:
        print(f"\n{len(failures)} of {len(tests)} admission checks FAILED")
        return 1
    rep = admission.report()
    print(f"\nall {len(tests)} admission checks passed — "
          f"{rep['enforced_total']} enforced, {len(rep['legacy_debt'])} legacy debt, "
          f"commitments clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
