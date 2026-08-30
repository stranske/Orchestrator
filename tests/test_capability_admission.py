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

import pathlib
import tempfile

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


def _findability_ledger(tmp_dir):
    """A SYNTHETIC ledger covering every findability verdict, with controlled registration dates.

    Never the live ledger: it holds 43 rows on this machine and far fewer on a clean runner, so an
    assertion about counts or membership there passes locally and fails in CI. Dates are explicit
    because the whole point of the requirement is that it binds from a cutoff, and `_created_ts`
    reads the earliest `event_history` timestamp.
    """
    import capabilities as caps

    after = admission.FINDABILITY_ENFORCED_FROM + 3600
    before = admission.FINDABILITY_ENFORCED_FROM - 86400
    rows = {
        # Everything except findability is declared, so the only verdict under test is the ninth.
        "t-consulted": dict(bound="asked", when=after),
        "t-stranded": dict(bound="silent", when=after),
        "t-nowhere": dict(bound=None, when=after),
        "t-declared": dict(bound=None, when=after, category=True, rationale=True),
        "t-category-only": dict(bound=None, when=after, category=True),
        "t-old-nowhere": dict(bound=None, when=before),
    }
    ledger = {}
    for cap_id, spec in rows.items():
        cap = caps._blank_capability(cap_id)
        cap.update(
            {
                "notes": "dedup: checked A/B/C; not present; building new",
                "downstream_consumer": "x.py:consume",
                "learning_sink": "feedback.outcomes",
                "kill_switch": "ORCH_X=0",
                "rollback": "revert PR",
                "trigger_cadence": "daily",
                "event_history": [{"timestamp": spec["when"], "type": "migrated"}],
            }
        )
        if spec.get("category"):
            cap["findability_category"] = admission.FINDABILITY_CATEGORY
        if spec.get("rationale"):
            cap["findability_rationale"] = "a rail invokes it unconditionally; never offered"
        ledger[cap_id] = cap
    path = pathlib.Path(tmp_dir) / "capabilities.json"
    caps.save(ledger, path)
    ctx = {
        "audit_rows": {cap_id: {"reachable": True, "defects": []} for cap_id in rows},
        "fixtures": set(rows),
        "known_controls": set(),
        "bound_surfaces": {
            cap_id: ([spec["bound"]] if spec.get("bound") else []) for cap_id, spec in rows.items()
        },
        "reached_surfaces": {"asked"},
        "consult_reach": {"reached": ["asked"], "bound_unconsulted": ["silent"]},
    }
    return path, ctx


def test_findability_distinguishes_its_three_sub_causes():
    """The verdicts a CALLER receives, per sub-cause — because the fixes are different.

    `bound_nowhere` is fixed by declaring a surface; `bound_to_unconsulted_surface` is fixed by
    binding a surface someone asks at, or by making that surface ask. Collapsing them into one
    "unfindable" licenses the wrong repair, which is why the causes are asserted to DIFFER rather
    than merely to be failures. Asserted through `admit()` and `report()`, the two entry points
    anything outside this module calls — never through `findability_cause` directly.
    """
    with tempfile.TemporaryDirectory(prefix="find-cause-") as td:
        path, ctx = _findability_ledger(td)
        verdicts = {
            cap_id: admission.admit(cap_id, path=path, ctx=ctx)
            for cap_id in (
                "t-consulted",
                "t-stranded",
                "t-nowhere",
                "t-declared",
                "t-category-only",
            )
        }
        ok = {cap_id: v["checks"]["findable"]["ok"] for cap_id, v in verdicts.items()}
        assert ok == {
            "t-consulted": True,
            "t-stranded": False,
            "t-nowhere": False,
            "t-declared": True,
            "t-category-only": False,
        }, ok
        # The two failures must be DISTINGUISHABLE, not just both red.
        causes = {
            cap_id: v["checks"]["findable"]["detail"].split(":")[0]
            for cap_id, v in verdicts.items()
            if not v["checks"]["findable"]["ok"]
        }
        assert causes["t-nowhere"] == "bound_nowhere", causes
        assert causes["t-stranded"] == "bound_to_unconsulted_surface", causes
        assert causes["t-nowhere"] != causes["t-stranded"]
        # The stranded verdict must NAME the surface, or the reader cannot act on it.
        assert "silent" in verdicts["t-stranded"]["checks"]["findable"]["detail"]
        # A category with no rationale must not clear the requirement — both halves are required,
        # the same rule as the two kill-switch categories.
        assert "bound_nowhere" in verdicts["t-category-only"]["checks"]["findable"]["detail"]
        # ...and the aggregate a report reader sees carries the causes AND the drain, so a debt
        # count can never read as "be patient indefinitely".
        #
        # SCOPED TO THE `t-` ROWS ON PURPOSE. `capabilities.load()` seeds every `KNOWN_GATES` row
        # into any ledger it opens, synthetic ones included (verified: a one-row file comes back
        # with 15). Asserting on the whole population would therefore be asserting on the committed
        # gate table, which is a different test and would move whenever that table does.
        rep = admission.report(path=path, ctx=ctx)
        find = rep["findability"]
        mine = {cap_id for cap_id in find["failing"] if cap_id.startswith("t-")}
        assert mine == {"t-category-only", "t-nowhere", "t-old-nowhere", "t-stranded"}, mine
        by_cause = {
            cause: [c for c in ids if c.startswith("t-")] for cause, ids in find["by_cause"].items()
        }
        assert by_cause["bound_nowhere"] == [
            "t-category-only",
            "t-nowhere",
            "t-old-nowhere",
        ], by_cause
        assert by_cause["bound_to_unconsulted_surface"] == ["t-stranded"], by_cause
        # EVERY FAILURE MUST BE DRAINABLE. A gate reporting a backlog with no stated way to clear it
        # is a latched gate; `drainable` falls below `failing` the moment a cause has no declared
        # fix, which is the alarm this equality arms.
        assert len(find["drainable"]) == len(find["failing"]), find
        assert set(find["by_cause"]) <= set(find["drain"]), (
            "a failing cause with no declared fix in FINDABILITY_DRAIN — the report would then "
            f"name a backlog it cannot say how to clear: {set(find['by_cause']) - set(find['drain'])}"
        )
        assert find["bound_unconsulted_surfaces"] == ["silent"], find
        assert find["not_checked"], "the requirement must say what it does NOT check"
        assert "heartbeat_reachable" in find["not_checked"], find["not_checked"]


def test_findability_blocks_new_capabilities_and_reports_older_ones_as_debt():
    """Scoped like the gate it joins: block the next mistake, keep the old debt visible.

    A requirement added later is red on arrival for everything that predates it, and this module
    already recorded what happens then — the gate gets switched off and protects nothing. So a row
    registered before the requirement's own cutoff still REPORTS `findable` as missing and does not
    BLOCK, and a row registered after it does both.
    """
    with tempfile.TemporaryDirectory(prefix="find-scope-") as td:
        path, ctx = _findability_ledger(td)
        new = admission.admit("t-nowhere", path=path, ctx=ctx)
        assert "findable" in new["missing"] and "findable" in new["blocking"], new
        assert new["deferred"] == [], new

        old = admission.admit("t-old-nowhere", path=path, ctx=ctx)
        assert "findable" in old["missing"], "pre-cutoff debt must still be REPORTED"
        assert "findable" in old["deferred"], old
        assert "findable" not in old["blocking"], "a pre-cutoff row must not block the suite"

        # AND THE SAME SPLIT IN THE AGGREGATE. Scoped to the `t-` rows because
        # `capabilities.load()` seeds the committed `KNOWN_GATES` rows into any ledger; and read
        # from `findability`, not from `enforced_failing`, because these synthetic rows have no
        # module so `req_heartbeat` fails them too — a row can be blocking for another reason
        # entirely, and this test is about the ninth requirement's scoping, not about all nine.
        rep = admission.report(path=path, ctx=ctx)
        find = rep["findability"]
        assert {c for c in find["blocking"] if c.startswith("t-")} == {
            "t-nowhere",
            "t-stranded",
            "t-category-only",
        }, find["blocking"]
        assert "t-old-nowhere" in find["deferred"], find["deferred"]
        assert "t-old-nowhere" not in find["blocking"], find["blocking"]
        # ...and a row blocking on findability must be in `enforced_failing`, or the gate reports a
        # violation it does not act on.
        assert "t-nowhere" in rep["enforced_failing"], rep["enforced_failing"]
    # THE CUTOFF MUST BE IN THE PAST. `GRANDFATHERED_BEFORE` was first written four days ahead,
    # which grandfathered brand-new capabilities and made the gate check nothing.
    assert admission.FINDABILITY_ENFORCED_FROM <= admission._now()
    assert set(admission.REQUIREMENT_ENFORCED_FROM) <= {n for n, _ in admission.REQUIREMENTS}


def test_unreadable_reach_is_not_evaluated_and_never_a_failure():
    """Three-valued, like every other verdict here — and every failure must name its own fix.

    If `capability_advisor` cannot be read at all, the consult-reach question is UNANSWERABLE.
    Collapsing that into False would fail every bound capability in the catalogue on an ImportError,
    which is a gate red on arrival; collapsing it into True would be a silent pass. So a bound
    capability passes with the reason stated, while `bound_nowhere` still fails, because that
    question needs no reach at all.

    The same test carries the DRAIN-COVERAGE invariant, because the two break together: dropping
    `reach_not_evaluated` from `FINDABLE_OK` both fails a bound capability for an unreadable module
    AND makes the report name a cause `FINDABILITY_DRAIN` has no fix for — a backlog it cannot say
    how to clear, which is this workspace's definition of an already-defective gate.
    """
    with tempfile.TemporaryDirectory(prefix="find-blind-") as td:
        path, ctx = _findability_ledger(td)
        blind = {
            **ctx,
            "reached_surfaces": set(),
            "consult_reach": {"unreadable": "ImportError: no capability_advisor"},
        }
        for cap_id in ("t-consulted", "t-stranded"):
            v = admission.admit(cap_id, path=path, ctx=blind)["checks"]["findable"]
            assert v["ok"], f"{cap_id}: an unreadable advisor must not fail a bound capability: {v}"
            assert "NOT EVALUATED" in v["detail"], v
        nowhere = admission.admit("t-nowhere", path=path, ctx=blind)["checks"]["findable"]
        assert not nowhere["ok"], "bound_nowhere needs no reach and must still fail"
        assert "bound_nowhere" in nowhere["detail"], nowhere

        rep = admission.report(path=path, ctx=blind)
        find = rep["findability"]
        assert set(find["by_cause"]) <= set(find["drain"]), (
            "the report names a failing cause that FINDABILITY_DRAIN has no fix for, so it reports "
            "a backlog it cannot say how to clear: "
            f"{sorted(set(find['by_cause']) - set(find['drain']))}"
        )
        assert len(find["drainable"]) == len(find["failing"]), find


def test_findability_exemption_is_declared_in_code_not_in_a_live_ledger():
    """The `no_surface` category, pinned like the two kill-switch categories.

    A declaration applied straight to the running instance's `capabilities.json` is green where it
    was typed, absent on a fresh checkout, and invisible in a diff — the exact defect that moved
    `kill_switch_category` into `KNOWN_DECLARATIONS`. So the exempt set is asserted from the CODE
    table, and the ledger may not out-declare it.
    """
    # BOTH code tables: reconciliation seeds from {**KNOWN_GATES, **KNOWN_DECLARATIONS}, and a
    # capability may not sit in both, so the union IS the code-side truth. Gate-registered rails
    # (completion-event-lineage, the keepalive/redirect/synthesis gates, the quarantined trial)
    # carry the declaration on their gate entry.
    declared = {
        cid
        for table in (capabilities.KNOWN_DECLARATIONS, capabilities.KNOWN_GATES)
        for cid, d in table.items()
        if d.get("findability_category") == admission.FINDABILITY_CATEGORY
    }
    assert declared == {
        "capability-admission-gate",
        "docs-drift-fix-agent",
        # The 2026-08-29 hard-tail set: eleven internal rails plus the quarantined trial transport,
        # each rationale naming the rail (or policy) that makes an agent surface wrong for it.
        "agy-runtime-isolation",
        "capability:reference-sync-hygiene-test-gate",
        "completion-event-lineage",
        "evidence-acquisition",
        "feature-reflection-cli",
        "feedback-store",
        "issue-readiness",
        "live-keepalive-supervisor",
        "local-model-profile-trial",
        "redirect-apply-bootstrap",
        "research-scheduler",
        # 2026-08-30: exp_abcd's followup admission control — admission control selected by the
        # admitted is no control. Declared on its gate entry.
        "research-usage-guard",
        "synthesis-promotion",
    }, declared
    for cap_id in declared:
        row = capabilities.KNOWN_DECLARATIONS.get(cap_id) or capabilities.KNOWN_GATES.get(cap_id)
        assert str((row or {}).get("findability_rationale") or "").strip(), cap_id
    # Reconciliation can only seed these if the fields are declaration-owned.
    for field in ("findability_category", "findability_rationale"):
        assert field in capabilities.DECLARATION_FIELDS, field
    # DRIFT GUARD: nothing may carry the exemption that the code table does not declare.
    ledger = capabilities.load_declared(capabilities.REG)
    stray = {
        cid
        for cid, cap in ledger.items()
        if cap.get("findability_category") == admission.FINDABILITY_CATEGORY
    } - declared
    assert (
        not stray
    ), f"ledger declares the findability exemption for undeclared capabilities: {stray}"


def test_consult_sites_are_falsifiable_claims_about_real_callers():
    """A declared consult site names a file; this opens it.

    Without this, `CONSULT_SITES` would be prose in a dict — and the requirement it feeds would
    inherit the failure mode this whole module exists to stop. Three states, and only one is a
    failure: verified, unverified (the caller is not on this machine, so no verdict), and DRIFTED
    (the caller is here and no longer names its surface), which is the real regression.
    """
    import capability_advisor as advisor

    reach = advisor.consulting_surfaces()
    assert not reach["drifted"], (
        "a consult site's caller no longer names its surface — update CONSULT_SITES or restore the "
        f"consult: {reach['drifted']}"
    )
    # The in-tree site must verify on EVERY machine, CI included, so this check can never degrade
    # into "everything unverified, nothing checked".
    assert "tick" in reach["verified"], reach["verified"]
    assert advisor.CONSULT_SITES["tick"]["caller"].endswith(
        ".py"
    ), "the in-tree site must be in-tree"
    # Every declared instance must be accounted for in exactly one of the three states.
    accounted = (
        set(reach["reached"])
        | {u["surface"] for u in reach["unverified"]}
        | {d["surface"] for d in reach["drifted"]}
    )
    for key, site in advisor.CONSULT_SITES.items():
        assert str(site.get("how") or "").strip(), f"{key} declares no reason"
        assert set(site.get("instances") or [key]) <= accounted or key in accounted, key
    # A family key is a label for the declaration, not a surface anyone passes.
    keys = advisor.consult_keys()
    assert "repo-audit:phase-1" in keys and "repo-audit:phase-N" not in keys, sorted(keys)


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
