#!/usr/bin/env python3
"""test_capability_set_coverage.py — the capability set is a SET, and this test enforces it.

WHY THIS EXISTS. Repeatedly, instructions that applied to "the capabilities" were answered for a
convenient subset: four analysed in depth and thirty-one summarised in a sentence; a recurrence
check built for sixteen and reported as if it covered all; twelve gated capabilities dismissed with
one line. Every time, the omission was invisible in the output — the numbers quoted (18 of 21) were
about FIXTURES, and nothing in the system objected that nineteen capabilities had no fixture at all.

So this is not a promise to do better. It is a gate that FAILS when the set is treated as optional:

  1. Every capability in the ledger MUST have a recurrence fixture. Adding a capability without one
     breaks this test, which is the point — coverage becomes a precondition of adding, not a
     follow-up someone remembers.
  2. Every capability MUST appear in the activation audit, and its audit row must be complete.
  3. Every fixture MUST name a real capability — a typo silently covering nothing is also a failure.
  4. A capability that cannot fire MUST have a stated reason. "Blocked" is acceptable; unexplained
     is not, because unexplained is how nineteen of them stayed invisible.

Run it directly (`python3 test_capability_set_coverage.py`) or under pytest. It is deliberately part
of the standard suite so a normal verification run cannot pass while the set is under-covered.
"""

from __future__ import annotations

import sys

import capabilities
import capability_activation_audit as audit
import capability_recurrence_check as recurrence
import env_prereq

# Capabilities exempt from needing a recurrence fixture, each with a REASON. This list exists so an
# exemption is a deliberate, reviewable act rather than a silent omission. Keep it empty if possible.
FIXTURE_EXEMPT: dict[str, str] = {}


def _fixture_capabilities() -> set[str]:
    """Every capability named by a fixture (guards and flag probes excluded)."""
    named = [f.get("capability") for f in recurrence.FIXTURES]
    named += [f.get("capability") for f in recurrence.PREDICATE_FIXTURES]
    return {n for n in named if n and not str(n).endswith("-flag")}


def test_every_capability_has_a_recurrence_fixture():
    """No capability may sit outside the recurrence check.

    This is the specific failure this file exists to prevent: 19 of 35 capabilities had no fixture
    while the reported score ("18 of 21") looked comprehensive.
    """
    ledger = set(capabilities.load_declared(capabilities.REG))
    covered = _fixture_capabilities()
    missing = sorted(ledger - covered - set(FIXTURE_EXEMPT))
    # A row whose MODULE is not in this checkout has no fixture here because it has no CODE here,
    # and the two fixes are opposite: wait-or-merge versus write the fixture. Same helper as the
    # admission and heartbeat-call-site checks, so all three name the same rows the same way.
    assert not missing, (
        f"{len(missing)} capability(ies) have NO recurrence fixture. Add one that replays a real "
        f"historical condition, or add an explicit FIXTURE_EXEMPT entry with a reason: {missing}"
        + audit.absent_entrypoint_note(missing)
    )


def test_an_absent_entrypoint_diagnoses_itself_differently_from_a_real_defect():
    """The two reds this file's own message could not tell apart.

    Salvaged from PR #43, which built this diagnostic independently and in parallel; its pytest
    coverage was the half #46 lacked, because #46 put the equivalent checks in the module
    `--selftest` and only a COLLECTED test is guarded by `.verify-floor.json`.

    The capability ledger is SHARED machine-local state (`$ORCH_LOCAL_RUNTIME`) while code is
    branch-isolated per worktree, so a sibling branch that registers a capability makes every
    other branch's `verify.py` red with a bare capability id — the same text a row registered with
    no implementation produces. On 2026-08-22 that ambiguity was read the wrong way, and the
    remedies proposed for a LIVE capability were to retire its ledger row or mask it with a
    waiver; either would have discarded merged-ready work.

    This is not a skip. Both cases still FAIL; the text now says which one it is. The ledger is
    INJECTED, so the test pins the distinction on any machine rather than on whichever rows this
    instance happens to have registered.
    """
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory(prefix="cap-set-entrypoint-") as td:
        saved = audit.HERE
        try:
            audit.HERE = Path(td)
            (Path(td) / "here_lane.py").write_text("# in the tree\n")
            led = {"here-cap": {"capability_id": "here-cap", "entrypoint": "here_lane.py:run"},
                   "gone-cap": {"capability_id": "gone-cap", "entrypoint": "gone_lane.py:run"}}

            gone = audit.absent_entrypoint_note(["gone-cap"], ledger=led)
            assert "MODULE ABSENT FROM THIS TREE" in gone, gone
            assert "gone_lane.py is not in this tree" in gone, gone
            assert "WAIT-OR-MERGE" in gone, gone
            # Retiring or waiving must be named as the WRONG move, not left to inference.
            assert "WAIVERS" in gone and "discards finished work" in gone, gone
            # The pointer, and the caveat that makes it honest: the wrong verdict rested on
            # `git log --all` coming back empty for a branch whose ref was never fetched.
            assert "git log --all --oneline -- gone_lane.py" in gone, gone
            assert "FETCH FIRST" in gone, gone

            # THE OPPOSITE CASE. A row whose module is right here must produce NO diagnosis at
            # all, or the text sends a reader off to wait for a merge of code already in front of
            # them. Silence is the assertion: the ordinary declaration failure reads unchanged.
            here = audit.absent_entrypoint_note(["here-cap"], ledger=led)
            assert here == "", here
            assert audit.entrypoint_presence(led["here-cap"])["state"] == audit.ENTRYPOINT_PRESENT
            assert audit.entrypoint_presence(led["gone-cap"])["state"] == audit.ENTRYPOINT_ABSENT
        finally:
            audit.HERE = saved


def test_the_capability_gates_all_consult_the_entrypoint_diagnosis():
    """One shared helper, three call sites — checked, because three copies is how they drift.

    Also salvaged from PR #43, and the sharper of its two ideas: nothing else in the tree notices
    if one gate quietly stops calling the helper and goes back to reporting a bare capability id.

    Matches the CALL (`audit.absent_entrypoint_note(`), never the bare name: every one of these
    files also MENTIONS the helper in a comment, so a name-only grep would keep passing after
    someone deleted the call and left the comment behind.
    """
    from pathlib import Path

    here = Path(__file__).resolve().parent
    for name in ("test_capability_admission.py", "test_capability_set_coverage.py",
                 "test_model_tier_resolution.py"):
        text = (here / name).read_text(encoding="utf-8")
        assert "audit.absent_entrypoint_note(" in text, (
            f"{name} no longer calls audit.absent_entrypoint_note(), so its capability gate is "
            f"back to reporting a bare capability id — indistinguishable from the defect it "
            f"guards"
        )


def test_no_fixture_names_an_unknown_capability():
    """A fixture pointing at a nonexistent capability covers nothing while looking like coverage."""
    # This check compares fixtures against the LIVE ledger, so it can only distinguish a typo
    # from an unregistered capability where the whole registered set is present. On a machine
    # that has never run the system the ledger holds only the rows the code declares, and every
    # fixture beyond those would read as a typo. Name the absent rows instead of asserting.
    env_prereq.require(env_prereq.ledger_rows_absent(*sorted(_fixture_capabilities())))
    ledger = set(capabilities.load_declared(capabilities.REG))
    unknown = sorted(_fixture_capabilities() - ledger)
    assert not unknown, f"fixtures name capabilities absent from the ledger: {unknown}"


def test_every_capability_appears_in_the_activation_audit():
    """The audit must see the whole set, with a complete row for each."""
    ledger = capabilities.load_declared(capabilities.REG)
    rep = audit.audit(use_cache=True)
    rows = {r["capability_id"]: r for r in rep["rows"]}
    missing = sorted(set(ledger) - set(rows))
    assert not missing, f"capabilities absent from the activation audit: {missing}"
    for cap_id, row in rows.items():
        for field in ("entry_class", "defects", "reachable"):
            assert field in row, f"{cap_id} audit row missing {field!r}"
        assert row["entry_class"] in (
            audit.ENTRY_TASK_ROUTED,
            audit.ENTRY_DIRECT,
            audit.ENTRY_GATED,
            audit.ENTRY_UNKNOWN,
        ), (cap_id, row["entry_class"])


def test_unreachable_capabilities_state_a_reason():
    """ "Cannot fire" is allowed. "Cannot fire, unexplained" is not."""
    rep = audit.audit(use_cache=True)
    silent = [r["capability_id"] for r in rep["rows"] if not r["reachable"] and not r["defects"]]
    assert not silent, f"capabilities blocked with no named defect: {silent}"


def test_every_defect_is_a_known_class():
    """A defect string with no entry in DEFECT_CLASSES cannot be aimed at or counted."""
    rep = audit.audit(use_cache=True)
    unknown = sorted(
        {d for r in rep["rows"] for d in r["defects"] if d not in audit.DEFECT_CLASSES}
    )
    assert not unknown, f"defects with no DEFECT_CLASSES description: {unknown}"


def test_exemptions_carry_reasons_and_exist():
    """An exemption must name a real capability and say why — never a bare skip."""
    ledger = set(capabilities.load_declared(capabilities.REG))
    for cap_id, reason in FIXTURE_EXEMPT.items():
        assert cap_id in ledger, f"FIXTURE_EXEMPT names unknown capability {cap_id!r}"
        assert reason and len(reason) > 20, f"FIXTURE_EXEMPT[{cap_id!r}] needs a real reason"


def roster() -> str:
    """EVERY capability with its coverage and firing status — never just the failures.

    A gate that reports only what FAILS still permits reporting on a subset: "all checks passed"
    says nothing about how many capabilities exist. That is how 19 uncovered capabilities sat behind
    a headline of "18 of 21". So the roster prints the whole set, every run, and the count is the
    ledger count by construction.
    """
    import capability_recurrence_check as rc

    ledger = capabilities.load_declared(capabilities.REG)
    covered = _fixture_capabilities()
    rep = audit.audit(use_cache=True)
    rows = {r["capability_id"]: r for r in rep["rows"]}
    # A MISSING PREREQUISITE is the one expected failure and it names itself; anything else is a
    # real recurrence-check defect and must be printed. Catching bare Exception into `fired = {}`
    # made the roster show a dash for every capability and still report success, so a broken
    # replay was indistinguishable from a replay that found nothing -- the founding defect again.
    replay_error: str | None = None
    fired: dict[str, bool] = {}
    try:
        rec = rc.replay(offline=True)
        for row in rec["rows"]:
            cap = row.get("capability")
            if cap and not str(cap).endswith("-flag"):
                fired[cap] = fired.get(cap, True) and bool(row["fires"])
    except env_prereq.MissingPrerequisite as exc:
        replay_error = f"prerequisite absent: {exc}"
    except Exception as exc:  # noqa: BLE001 -- reported, never swallowed
        replay_error = f"{type(exc).__name__}: {exc}"

    out = (
        [
            f"# Capability set roster — all {len(ledger)} capabilities",
            "",
        ]
        + (
            [
                f"> RECURRENCE REPLAY DID NOT RUN — {replay_error}. Every 'Recurrence' cell below is "
                f"UNKNOWN, not empty.",
                "",
            ]
            if replay_error
            else []
        )
        + [
            "| Capability | Fixture | Can fire | Recurrence | Blocker |",
            "|---|---|---|---|---|",
        ]
    )
    for cap_id in sorted(ledger):
        row = rows.get(cap_id) or {}
        fx = "yes" if cap_id in covered else ("EXEMPT" if cap_id in FIXTURE_EXEMPT else "**NO**")
        can = "yes" if row.get("reachable") else "NO"
        fire = {True: "fires", False: "miss"}.get(fired.get(cap_id), "—")
        out.append(
            f"| {cap_id} | {fx} | {can} | {fire} | "
            f"{', '.join(row.get('defects') or []) or '—'} |"
        )
    uncovered = sorted(set(ledger) - covered - set(FIXTURE_EXEMPT))
    blocked = sorted(c for c in ledger if not (rows.get(c) or {}).get("reachable"))
    out += [
        "",
        f"  fixtures: {len(covered)}/{len(ledger)}   "
        f"uncovered: {len(uncovered)}   blocked: {len(blocked)}",
    ]
    return "\n".join(out) + "\n"


def main() -> int:
    if "--roster" in sys.argv:
        print(roster(), end="")
        return 0
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failures, skipped = [], []
    for fn in tests:
        try:
            fn()
            print(f"  OK   {fn.__name__}")
        # MissingPrerequisite is a SkipTest, not an AssertionError — caught first so it neither
        # crashes this runner nor gets counted as a pass. "5 of 6 passed, 1 skipped because X"
        # is the honest line; "all 6 passed" over a set the machine cannot see is the lie this
        # whole file exists to prevent.
        except env_prereq.MissingPrerequisite as exc:
            skipped.append((fn.__name__, str(exc)))
            print(f"  SKIP {fn.__name__}")
            print(f"       {env_prereq.PREREQ_ABSENT_MARK} {str(exc)[:400]}")
        except AssertionError as exc:
            failures.append((fn.__name__, str(exc)))
            print(f"  FAIL {fn.__name__}")
            # 400 chars cut the absent-module diagnostic in half, and a half-explanation of why a
            # row looks uncovered is as misleading as none. Capped above the longest message any
            # check here produces rather than at a round number.
            print(f"       {str(exc)[:2000]}")
    if failures:
        print(f"\n{len(failures)} of {len(tests)} capability-set coverage checks FAILED")
        return 1
    ledger = capabilities.load_declared(capabilities.REG)
    if skipped:
        print(
            f"\n{len(tests) - len(skipped)} of {len(tests)} capability-set coverage checks "
            f"passed over {len(ledger)} ledger capabilities, {len(skipped)} skipped: "
            + "; ".join(f"{n} ({r[:80]})" for n, r in skipped)
        )
        return 0
    print(
        f"\nall {len(tests)} capability-set coverage checks passed "
        f"over ALL {len(ledger)} ledger capabilities "
        f"(--roster for the per-capability table)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
