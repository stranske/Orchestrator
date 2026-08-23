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

import capabilities
import capability_activation_audit as audit
import capability_admission as admission
import env_prereq


def test_new_capabilities_carry_all_required_parts():
    """A capability added since the gate must bring its caller, heartbeat, fixture and outcome path.

    The eight requirements are not a wish list; each one is a defect that actually happened. Adding
    a capability without them is how six subsystems went dormant, how `issue-readiness` shipped with
    no heartbeat, and how `reference-sync-hygiene` accrued 367 events its own gate could not read.
    """
    # Enforcement is scoped to capabilities registered from 2026-08-21, and registration happens
    # on the running instance. A ledger with no pre-gate rows has no enforced population either,
    # so there is nothing for this to be a gate ON. Name that, do not pass on an empty set.
    env_prereq.require(env_prereq.ledger_legacy_rows_absent())
    rep = admission.report()
    failing = rep["enforced_failing"]
    rows = {r["capability_id"]: r for r in rep["rows"]}
    detail = {cid: rows[cid]["missing"] for cid in failing}
    # A row whose MODULE is not in this checkout cannot have its caller, heartbeat or fixture here
    # either, and `['caller_exists','heartbeat','fixture']` on its own reads as "registered with no
    # implementation — retire it". `evidence-acquisition` was exactly that on 2026-08-22 and
    # retiring it would have discarded merged-ready work. Same helper as the two sibling checks, so
    # all three tell one story.
    assert not failing, (
        f"{len(failing)} capability(ies) added since the admission gate are missing required "
        f"parts: {detail}. Declare them in the ledger, or add an expiring WAIVERS entry with a "
        f"reason — never leave it undeclared." + audit.absent_entrypoint_note(failing)
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
        except Exception:  # noqa: BLE001
            ok = False
        assert not ok, f"requirement {name!r} passes a capability that declares nothing"


def test_legacy_debt_is_reported_not_forgiven():
    """Scoping must not become amnesty.

    Legacy capabilities are excluded from FAILING, never from REPORTING. If the exemption were
    pushed into the predicates instead of the row, the debt would read as compliance — which is
    precisely how "all findings resolved; nothing dropped" coexisted with 13 blocked capabilities.
    """
    env_prereq.require(env_prereq.ledger_legacy_rows_absent())
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
    ledger = capabilities.load_declared(capabilities.REG)
    for cap_id in admission.WAIVERS:
        assert cap_id in ledger, f"WAIVERS names unknown capability {cap_id!r}"
    problems = admission.waiver_problems()
    assert not problems, f"illegitimate waivers: {problems}"


def test_safety_guard_exemption_is_declared_and_narrow():
    """The kill-switch exemption for a CONFINEMENT must stay opt-in, justified, and small.

    A kill switch turns a capability OFF; for a confinement the off state is more dangerous than the
    on state, so demanding one asks for an anti-feature. `agy-runtime-isolation` forced this -- its
    `--add-dir` guard keeps gemini's writes inside the target worktree, and "disabling" it means
    letting an agent write outside its worktree. The risk of any exemption is that it becomes the
    cheap way to clear a red, so this pins the DECLARED SET.

    ASSERTS THE CODE TABLE, NOT THE LEDGER. These declarations used to be applied straight to the
    running instance's `capabilities.json`, which made them machine-local -- green here, absent on a
    fresh checkout -- so this test could only SKIP on CI and the exemption set was never actually
    reviewed anywhere. `capabilities.KNOWN_DECLARATIONS` is the source now, so the set is pinned on
    every machine and widening it shows up in a diff.
    """
    import capabilities

    declared = {
        cid
        for cid, d in capabilities.KNOWN_DECLARATIONS.items()
        if d.get("kill_switch_category") == "safety_guard"
    }
    assert declared == {"agy-runtime-isolation"}, declared
    for cap_id in declared:
        assert str(
            capabilities.KNOWN_DECLARATIONS[cap_id].get("kill_switch_rationale") or ""
        ).strip(), cap_id
    # BOTH parts are required -- the category alone must not satisfy the gate.
    probe = dict(capabilities.KNOWN_DECLARATIONS["agy-runtime-isolation"])
    probe.pop("kill_switch_rationale", None)
    probe.pop("kill_switch", None)
    ok, _ = admission.req_kill_switch(probe, {})
    assert not ok, "category without a rationale must not satisfy the kill-switch requirement"
    # NARROWNESS, control case: `offload` had the identical complaint on the same day and got a REAL
    # switch instead of an exemption. It must stay out of the table, and its switch must still exist
    # in the tree -- `known_controls()` reads the source, so this fails if the guard is deleted.
    assert "offload" not in capabilities.KNOWN_DECLARATIONS
    assert "ORCH_OFFLOAD_DISABLED" in admission.known_controls()
    # DRIFT GUARD: nothing may carry the exemption that the code table does not declare. This is what
    # catches a category typed into a live ledger instead of into the diff.
    ledger = capabilities.load_declared(capabilities.REG)
    stray = {
        cid for cid, cap in ledger.items() if cap.get("kill_switch_category") == "safety_guard"
    } - declared
    assert not stray, f"ledger declares safety_guard for undeclared capabilities: {stray}"


def test_compute_only_category_must_name_a_control_that_exists():
    """The second exemption, and the condition that stops it becoming a rubber stamp.

    A `compute_only` capability takes no action -- stopping it blinds a consumer rather than halting
    anything -- so the real control lives at the consumer that ACTS. "Read-only" is exactly what a
    capability would self-certify to clear a red, so the declaration alone is deliberately NOT
    enough: it must NAME a `control_point`, and that switch must actually be found in the tree.
    """
    import capabilities

    declared = {
        cid
        for cid, d in capabilities.KNOWN_DECLARATIONS.items()
        if d.get("kill_switch_category") == "compute_only"
    }
    assert declared == {"windowed-capacity-policy", "redirect-policy", "feedback-store"}, declared

    controls = admission.known_controls()
    ctx = {"known_controls": controls}
    for cap_id in declared:
        cap = capabilities.KNOWN_DECLARATIONS[cap_id]
        assert str(cap.get("kill_switch_rationale") or "").strip(), cap_id
        assert cap["control_point"] in controls, (cap_id, cap.get("control_point"))
        ok, _ = admission.req_kill_switch(cap, ctx)
        assert ok, cap_id

    # THE ANTI-ABUSE CONDITION: naming a switch that does not exist must FAIL, not pass.
    fake = {
        **capabilities.KNOWN_DECLARATIONS["windowed-capacity-policy"],
        "control_point": "ORCH_NOT_A_REAL_FLAG",
    }
    fake.pop("kill_switch", None)
    ok, detail = admission.req_kill_switch(fake, ctx)
    assert not ok and "not a known switch" in detail, detail
    # ...and the category with no control_point at all must also fail.
    bare = {**capabilities.KNOWN_DECLARATIONS["windowed-capacity-policy"]}
    bare.pop("kill_switch", None)
    bare.pop("control_point", None)
    ok2, _ = admission.req_kill_switch(bare, ctx)
    assert not ok2, "compute_only without a named control must not satisfy the requirement"

    # NARROWNESS: capabilities that DO something are not in this category.
    for acting in ("repo-playbook", "offload", "agy-runtime-isolation"):
        assert (capabilities.KNOWN_DECLARATIONS.get(acting) or {}).get(
            "kill_switch_category"
        ) != "compute_only", acting
    # DRIFT GUARD: same as the safety_guard case -- the ledger may not out-declare the code.
    ledger = capabilities.load_declared(capabilities.REG)
    stray = {
        cid for cid, cap in ledger.items() if cap.get("kill_switch_category") == "compute_only"
    } - declared
    assert not stray, f"ledger declares compute_only for undeclared capabilities: {stray}"


def test_the_gate_admits_itself():
    """Dogfooding, and not for style: a gate exempt from its own rule is the rule being optional."""
    # `admit()` raises ValueError on a capability the ledger has never heard of, so the gate can
    # only be asked about itself where it is registered.
    env_prereq.require(env_prereq.ledger_rows_absent("capability-admission-gate"))
    own = admission.admit("capability-admission-gate")
    assert own["admitted"], f"the admission gate fails its own requirements: {own['missing']}"


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failures, skipped = [], []
    for fn in tests:
        try:
            fn()
            print(f"  OK   {fn.__name__}")
        # A skip is not a pass and not a failure. Catching it BEFORE AssertionError matters:
        # MissingPrerequisite is a SkipTest, not an AssertionError, so an uncaught one would
        # crash this runner — and printing it as OK would be worse, because the count would
        # then claim coverage this machine cannot provide.
        except env_prereq.MissingPrerequisite as exc:
            skipped.append((fn.__name__, str(exc)))
            print(f"  SKIP {fn.__name__}")
            print(f"       {env_prereq.PREREQ_ABSENT_MARK} {str(exc)[:400]}")
        except AssertionError as exc:
            failures.append(fn.__name__)
            print(f"  FAIL {fn.__name__}")
            # 400 chars cut the absent-module diagnostic in half — a truncated explanation of WHY
            # a row looks unimplemented is the same defect as no explanation, so the cap is set
            # above the longest message any check here produces rather than at a round number.
            print(f"       {str(exc)[:2000]}")
    if failures:
        print(f"\n{len(failures)} of {len(tests)} admission checks FAILED")
        return 1
    if skipped:
        # Green, and saying exactly what did not run. verify.py greps the mark and counts it
        # against a ceiling, so this can never quietly become the whole file.
        print(
            f"\n{len(tests) - len(skipped)} of {len(tests)} admission checks passed, "
            f"{len(skipped)} skipped: " + "; ".join(f"{n} ({r[:80]})" for n, r in skipped)
        )
        return 0
    rep = admission.report()
    print(
        f"\nall {len(tests)} admission checks passed — "
        f"{rep['enforced_total']} enforced, {len(rep['legacy_debt'])} legacy debt, "
        f"commitments clean"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
