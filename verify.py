#!/usr/bin/env python3
"""verify.py — the ONE honest way to verify this tree, because ad-hoc loops lie.

WHY THIS EXISTS, precisely. For most of 2026-08-21 the verification command in use was:

    for t in test_*.py; do python3 "$t" && echo OK; done

**25 of the 28 test files are pytest-only.** Run directly they import their module, define their
test functions, execute nothing, and exit 0. So "all 28 test files pass" meant three real runs and
twenty-five vacuous zero-exits — reported repeatedly, in the same session that produced
`ADDING_CAPABILITIES.md` and its first failure mode, "completion measured as paperwork instead of
behaviour". The instrument was wrong in exactly the way the document warned about.

Exit codes are not evidence on their own. `pytest --timeout=0` with no timeout plugin installed
prints a usage error and **also exits 0**. So this module never trusts a status code alone: it reads
the collected/passed COUNTS out of pytest and compares them against a recorded floor.

What it runs:
  1. pytest over the whole directory — the only thing that executes the pytest-only files.
  2. every module exposing `--selftest`, discovered rather than hardcoded, so a new module with a
     selftest is picked up without anyone remembering to add it here.
  3. the capability gates: activation audit, recurrence replay, set coverage, admission.

What makes it honest:
  * a **floor** (`.verify-floor.json`) on tests collected and passed. A silent collection drop — an
    import error making a file uncollectable, a renamed file, a deleted test — fails instead of
    reading as green. This is the same trap in a different costume: fewer tests running looks
    identical to all tests passing. `collected` is an EQUALITY, not a minimum (2026-08-23): a
    floor that has fallen BEHIND reality is permissive by exactly the gap, and that direction
    was silent for as long as it existed. See `_floor_problems` for why only `collected` can be
    strict, and for the merge-conflict property the equality buys.
  * **zero collected is always a failure**, whatever the exit status.
  * the summary states what actually executed, never "the suite passed".
  * a **SKIP CEILING** (added 2026-08-21, with the first CI run). Skipping is the other way to
    run less while reading green, so it is bounded exactly like collection: the floor file
    records the maximum number of skipped tests, skipped selftests and skipped gates, and
    exceeding any of them FAILS. Every skip must also NAME its missing prerequisite — this
    module prints all of them, so "green" always states what did not run.

    The floor is now `passed + skipped >= floor.passed`, not `passed >= floor.passed`: a check
    may move between passing and consciously-skipped, but the two together may never shrink.
    Turning a failure into a pass by skipping it is what the ceiling forbids; letting a machine
    without the prerequisite report honestly is what the floor change allows. One constant per
    ceiling, defined once in the floor file and read once here, so the measuring and draining
    windows cannot drift apart.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import pathlib
import re
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
FLOOR = HERE / ".verify-floor.json"

# The token a selftest or gate prints to say "I did not run this, and here is what is missing".
# Imported from env_prereq rather than duplicated: a shared literal in two files is a pair that
# drifts, and a mark that drifts turns a skip back into a silent pass.
try:
    from env_prereq import PREREQ_ABSENT_MARK
except Exception:  # noqa: BLE001
    PREREQ_ABSENT_MARK = "PREREQUISITE ABSENT:"

# pytest's terse summary line, e.g. "182 passed, 3 skipped in 41.20s"
COUNT_RE = re.compile(r"(\d+) (passed|failed|error|errors|skipped|xfailed|xpassed)")


def run_pytest(*, extra: list[str] | None = None) -> dict:
    """Execute the suite and read the COUNTS, not the exit code."""
    # `-rfEs`: failures, errors AND skip reasons in the short summary. Skip reasons are why this
    # flag is here at all — a skip count with no story is the shape a silent narrowing hides in.
    # But `f` and `E` are NOT optional additions: pytest's default is `-rfE`, so passing a bare
    # `-rs` REPLACES it and silently stops printing FAILED lines. That is exactly what happened —
    # a run with 1 real failure printed "pytest failures (0)", from the very code added here to
    # stop failures going unlisted. An instrument that reports less, introduced by the change that
    # was meant to make it report more.
    cmd = [sys.executable, "-m", "pytest", "-q", "-rfEs", "-p", "no:cacheprovider", "--no-header"]
    cmd += extra or []
    proc = subprocess.run(cmd, cwd=HERE, capture_output=True, text=True)
    tail = (proc.stdout or "") + (proc.stderr or "")
    counts: dict[str, int] = {}
    for n, kind in COUNT_RE.findall(tail):
        kind = "error" if kind.startswith("error") else kind
        counts[kind] = counts.get(kind, 0) + int(n)
    collected = sum(
        counts.get(k, 0) for k in ("passed", "failed", "error", "skipped", "xfailed", "xpassed")
    )
    # A usage error prints to stderr and exits 0. Absence of counts is therefore a failure, never
    # an empty success.
    usage_error = "usage: pytest" in tail or "unrecognized arguments" in tail
    lines = tail.strip().splitlines()
    # EVERY failure, not the last 12 lines of output. The first CI run reported 21 failures and
    # the log named 7 of them, because this was `lines[-12:]` — a truncated red costs a whole
    # round trip to diagnose.
    failures = [
        ln.strip()
        for ln in lines
        if ln.startswith(("FAILED ", "ERROR ")) or ln.lstrip().startswith(("FAILED ", "ERROR "))
    ]
    skips = [ln.strip() for ln in lines if ln.lstrip().startswith("SKIPPED ")]
    return {
        "counts": counts,
        "collected": collected,
        "passed": counts.get("passed", 0),
        "skipped": counts.get("skipped", 0),
        "failed": counts.get("failed", 0) + counts.get("error", 0),
        "returncode": proc.returncode,
        "usage_error": usage_error,
        "failures": failures,
        "skips": skips,
        "tail": lines[-12:],
    }


def selftest_modules() -> list[str]:
    """Discover modules exposing --selftest instead of hardcoding a list that goes stale."""
    found = []
    for path in sorted(HERE.glob("*.py")):
        if path.name.startswith("test_") or path.name == "verify.py":
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:  # noqa: BLE001
            continue
        if '"--selftest"' in text or "'--selftest'" in text:
            found.append(path.stem)
    return found


def run_selftests(modules: list[str]) -> dict:
    """Run each `--selftest` and sort it into ran / skipped-with-a-reason / failed.

    THREE outcomes, not two. A selftest that exits 0 having executed nothing was already caught
    (the silent zero-exit rule). The matching hole is a selftest that exits 0, SPEAKS, and still
    executed nothing — indistinguishable from a pass by the old two-way split. So a selftest that
    cannot run here prints the shared `PREREQUISITE ABSENT:` mark with the missing thing named,
    and lands in `skipped`, which is counted, printed, and ceilinged. `ok` therefore means "ran",
    and the number after it is trustworthy again.
    """
    ok, bad, skipped = [], {}, {}
    for mod in modules:
        proc = subprocess.run(
            [sys.executable, f"{mod}.py", "--selftest"], cwd=HERE, capture_output=True, text=True
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        # A selftest must both exit 0 AND say something. A silent zero-exit is the very failure
        # this module exists to catch.
        spoke = bool(out.strip())
        if proc.returncode != 0 or not spoke:
            bad[mod] = (
                "silent zero-exit — did it run?" if proc.returncode == 0 else out.strip()[-200:]
            )
            continue
        reasons = [
            ln.split(PREREQ_ABSENT_MARK, 1)[1].strip()
            for ln in out.splitlines()
            if PREREQ_ABSENT_MARK in ln
        ]
        if reasons:
            skipped[mod] = reasons
        else:
            ok.append(mod)
    return {"ok": ok, "failed": bad, "skipped": skipped}


GATES = (
    ("activation audit", ["capability_activation_audit.py", "--no-cache"]),
    ("recurrence replay", ["capability_recurrence_check.py"]),
    ("set coverage", ["test_capability_set_coverage.py"]),
    ("admission", ["test_capability_admission.py"]),
    ("ledger validate", ["capabilities.py", "--json", "validate"]),
)


def run_gates() -> dict:
    out = {}
    for name, argv in GATES:
        proc = subprocess.run([sys.executable, *argv], cwd=HERE, capture_output=True, text=True)
        text = (proc.stdout or "") + (proc.stderr or "")
        # Same three-way split as the selftests: a gate that could not judge here says so with
        # the shared mark, and is reported as SKIP rather than folded into `ok`.
        reasons = [
            ln.split(PREREQ_ABSENT_MARK, 1)[1].strip()
            for ln in text.splitlines()
            if PREREQ_ABSENT_MARK in ln
        ]
        out[name] = {"ok": proc.returncode == 0, "skipped": reasons, "line": _headline(name, text)}
    return out


def _headline(name: str, text: str) -> str:
    wanted = {
        "activation audit": r"CAN FIRE:.*",
        "recurrence replay": r"WOULD FIRE:.*",
        "set coverage": r"all \d+ capability-set.*",
        "admission": r"all \d+ admission.*",
        "ledger validate": r'"valid": \w+',
    }
    pat = wanted.get(name)
    if pat:
        m = re.search(pat, text)
        if m:
            return m.group(0).strip()
    return (text.strip().splitlines() or ["(no output)"])[-1][:100]


def absent_entrypoint_line() -> str | None:
    """One line naming ledger rows whose declared module is NOT in this tree, or None.

    WHY IT IS IN THE SUMMARY. The capability ledger is shared per MACHINE while code is
    branch-isolated per WORKTREE, so a row another session registered makes three checks go red
    here with messages that read "registered with no implementation — retire it". On 2026-08-22 that
    row was `evidence-acquisition`, its module sat on an unmerged branch, and retiring or waiving it
    would have discarded finished work. Naming the condition once, at the top level, means the
    reader learns it from the SUMMARY rather than from three separate failures.

    DIAGNOSTIC, NOT A VERDICT. It never enters `problems`, never suppresses a failure, and never
    counts as a skip — the three checks still fail exactly as loudly. An import failure is REPORTED
    rather than swallowed, because a diagnostic that silently stops appearing is indistinguishable
    from one that has nothing to say.
    """
    try:
        import capabilities
        import capability_activation_audit as audit

        # load_declared: read-only. verify.py must not mutate the ledger it is reporting on.
        rep = audit.absent_entrypoint_report(sorted(capabilities.load_declared(capabilities.REG)))
    except Exception as exc:  # noqa: BLE001
        return f"  entrypoints: NOT CHECKED ({type(exc).__name__}: {exc})"
    return _format_absent_line(rep)


def _format_absent_line(rep: dict) -> str | None:
    """Render the report as one summary line, or None when nothing is absent. PURE.

    Split out so the selftest exercises the real rule on synthetic reports, the same reason
    `_floor_problems` and `_ceiling_problems` are pure: the interesting cases here are a machine
    with an absent row and a machine without one, and no single machine is both.

    It deliberately does NOT carry `PREREQ_ABSENT_MARK`. That token is how a skip is counted
    against the ceiling, and nothing was skipped — miscounting this line as a skip would consume
    ceiling headroom that belongs to a real missing prerequisite.
    """
    if not rep.get("absent"):
        return None
    where = []
    for row in rep["absent"]:
        found = row.get("found_in") or []
        seen = (
            f"found in {found[0]['checkout']}"
            + (f" +{len(found) - 1} more" if len(found) > 1 else "")
            if found
            else "not found in any sibling checkout"
        )
        where.append(f"{row['capability_id']} -> {row['entrypoint']} ({seen})")
    # Both numbers in the same place, per the house rule: "1" reads as an emergency, "1 of 43"
    # reads as one session's in-flight branch, which is what it is.
    return (
        f"  entrypoints: {len(rep['absent'])} of {rep.get('total', '?')} ledger row(s) declare "
        f"code ABSENT from this tree — {'; '.join(where)}. "
        f"WAIT-OR-MERGE, not retire/waive."
    )


# ---------------------------------------------------------------------------
# THE `ci` SURFACE CONSULT. `capability_advisor.SURFACE_BINDINGS` declares a `ci` surface, and
# nothing consulted it: `capability-admission-gate`, `docs-drift-fix-agent` and
# `capability:reference-sync-hygiene-test-gate` were bound to a surface with no caller, which is the
# same defect as no binding at all -- a capability nothing can offer can never earn the evidence
# that would rank it. verify.py IS that surface: it runs on every PR and already EXECUTES the
# admission gate as one of its five gates.
#
# NO NEW MACHINERY, and three hard constraints because this file is the project's verdict and its
# output is read by CI:
#   1. IT CANNOT CHANGE THE VERDICT. The line never enters `problems`, so exit semantics are
#      untouched, exactly like `absent_entrypoint_line` above.
#   2. IT CANNOT CHANGE THE COUNTS, and it is NOT a skip: it deliberately does not carry
#      `PREREQ_ABSENT_MARK`, because that token is how a skip is counted against the ceiling and
#      the ceilings have zero headroom (26/26 tests, 7/7 selftests on a bare runner).
#   3. IT NEVER MUTATES THE LEDGER IT REPORTS ON. `record=False`, for the same reason
#      `absent_entrypoint_line` uses `load_declared`: this run's own gates read that ledger, and a
#      verifier that writes to its subject is not a verifier. A CI runner's ledger is thrown away
#      anyway, so a write there would buy nothing and cost the flock.
# It also asserts NOTHING about which capability comes back -- the ledger is machine-local (43 rows
# here, ~14 on a clean runner) and that exact mistake has shipped twice.
# ---------------------------------------------------------------------------

CI_SURFACE = "ci"


def _format_ci_consult_line(declared: int, offered: int, bound_rows: int, total_rows: int) -> str:
    """Render the consult as one summary line. PURE, so the selftest exercises the real rule.

    BOTH QUANTITIES, ALWAYS. `declared` comes from the committed table and is the same on every
    machine; `offered` comes from the machine-local ledger and is not. Printing only the second
    makes "this runner has no rows for them" indistinguishable from "nothing is bound to CI", and
    printing only the first hides that the offer was empty. The findability pair is the same rule one
    level up: a bound count with no unbound count reads as "fine" however many rows nothing can
    offer.
    """
    unbound = max(0, total_rows - bound_rows)
    tail = (
        f"; findability {bound_rows}/{total_rows} ledger row(s) bound to some surface, "
        f"{unbound} bound to none"
    )
    if declared and not offered:
        return (
            f"  ci consult:  surface {CI_SURFACE!r} declares {declared} capability(ies), "
            f"0 present in this machine's ledger (nothing offered here){tail}"
        )
    return (
        f"  ci consult:  surface {CI_SURFACE!r} declares {declared} capability(ies), "
        f"{offered} offered (bound first; an unbound keyword match is ranked after, never "
        f"dropped){tail}"
    )


def ci_consult_line() -> str:
    """Consult the advisor as the CI surface and report it in one line. Never a verdict.

    An import or advisor failure is REPORTED rather than swallowed, for the same reason as
    `absent_entrypoint_line`: a diagnostic that silently stops appearing is indistinguishable from
    one that has nothing to say.
    """
    try:
        import capabilities
        import capability_advisor as advisor

        declared = len(
            [k for k in advisor.SURFACE_BINDINGS.get(CI_SURFACE, {}) if k != advisor.NO_BINDING]
        )
        # Free text describing THIS run, not the surface's own name: a fixed phrase would make any
        # learned association an artifact of this string rather than of the work.
        advice = advisor.advise(
            "verify this tree before merging: real pytest counts, module selftests and the "
            "capability gates",
            surface=CI_SURFACE,
            skill=CI_SURFACE,
            record=False,
        )
        offered = len(advice.get("capabilities") or [])
        rows = sorted(capabilities.load_declared(capabilities.REG))
        bound = sum(
            1
            for cap_id in rows
            if any(
                cap_id in advisor.binding_for(surface)
                for surface in advisor.SURFACE_BINDINGS
            )
        )
        return _format_ci_consult_line(declared, offered, bound, len(rows))
    except Exception as exc:  # noqa: BLE001
        return f"  ci consult:  NOT CHECKED ({type(exc).__name__}: {exc})"


def load_floor() -> dict:
    if FLOOR.exists():
        try:
            return json.loads(FLOOR.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return {}
    return {}


# The drift message's opening words, named ONCE. The message is built from it and the
# `--update-floor` unblock predicate matches on it; a matching pair of literals would drift, and a
# drifted pair here would silently re-latch the gate closed. (House rule: one constant, defined
# once, consumed by both the measuring and the draining side.)
DRIFT_PREFIX = "floor is BEHIND reality"


def _blocks_floor_update(problems: list[str]) -> list[str]:
    """Which problems must stop `--update-floor` from writing? NOT the drift one.

    LATCHED-GATE FIX. `--update-floor` used to require a fully green run. The moment `collected`
    became an equality that stopped being safe: a floor behind reality is now a PROBLEM, so the
    one command that fixes it would have been refused for the existence of the very condition it
    exists to clear — the clear path blocked by the thing the gate measures. Real failures still
    block the write, because recording a floor from a broken run would bake the breakage in.
    """
    return [p for p in problems if not p.startswith(DRIFT_PREFIX)]


def _appended_note(prior: str | None, collected: int, passed: int, today: str) -> str:
    """Append the recorded counts to the EXISTING note. Never replace it. Pure, so the selftest
    can hold the preservation property rather than trusting it.

    Until 2026-08-23 `--update-floor` overwrote the note with a generic sentence. The note is the
    only record of WHY each ceiling is the number it is — which missing prerequisite justifies
    each agreed skip — so overwriting it destroyed the rationale on every use. The file had to
    carry a warning about its own tool ("must be restored by hand after every use"), and a tool
    whose correct use requires undoing part of what it just did is a footgun, not a tool. It also
    made the honest path — hand-editing two integers — the only safe one, which is how the floor
    came to be updated rarely enough to fall behind in the first place.
    """
    stamp = (
        f"FLOOR RECORDED by verify.py --update-floor on {today}: collected={collected}, "
        f"passed={passed}. Ceilings preserved, never re-measured — they are edited by hand."
    )
    prior = (prior or "").strip()
    return f"{prior} {stamp}" if prior else stamp


# Ceiling keys, and what each bounds. Named once so the check below and `--update-floor` cannot
# disagree about which number they mean.
CEILINGS = (
    ("skipped_max", "skipped test(s)"),
    ("selftest_skipped_max", "skipped selftest(s)"),
    ("gate_skipped_max", "skipped gate(s)"),
)


def _floor_problems(floor: dict, py: dict) -> list[str]:
    """Is the amount of CHECKING wrong in either direction? Pure, so the selftest exercises the
    real rule.

    Three problems, all of which look like passing:
      * fewer tests COLLECTED — an import error, a rename, a deletion;
      * MORE tests collected than the floor records — the floor has fallen behind reality;
      * fewer tests passed-or-consciously-skipped — a test that stopped running without becoming
        a named skip. `passed` alone cannot be the floor once skipping is legitimate, or the
        machine missing a prerequisite fails for being honest; `passed + skipped` can be, and the
        ceiling is what stops the skipped side swallowing everything.
    """
    # WHY `collected` IS AN EQUALITY AND `passed` IS NOT.
    #
    # Until 2026-08-23 this fired only downward, so a branch could add tests and never touch the
    # floor: silently green, with the floor left permissive by exactly the number added. That is
    # not hypothetical — #34 and #37 each added a test and left the file alone, and every one of
    # the recorded drifts (21 low at the worst, then 8, then 1, then 2) was caught only because
    # somebody happened to look. A floor below reality is the hole this file exists to close, so
    # falling BEHIND it has to be exactly as loud as dropping below it.
    #
    # The equality also buys what no amount of discipline could. Once every test-adding branch
    # must edit these same two lines, two concurrent branches CONFLICT IN GIT. The second cannot
    # merge without rebasing onto the first, and the rebased run reports the true merge-result
    # count. Git's own conflict detection is what enforces "measure on the merge result, not on
    # the branch" — the rule the note in .verify-floor.json had to repeat three times precisely
    # because nothing enforced it. (This very change was rebased that way: #42 landed underneath
    # it and moved the floor 368 -> 387.)
    #
    # Only `collected` can be strict, and the asymmetry is load-bearing. Collection is
    # machine-invariant: a skipped test is still a collected test, so a runner with none of this
    # instance's prerequisites collects exactly what the owner's machine collects — measured on
    # CI and locally on 2026-08-23, both 368, with pass/skip splits of 344/24 against 368/0.
    # `passed` is NOT invariant — it trades against `skipped` machine by machine — so it stays a
    # MINIMUM on `passed + skipped`. Making that one strict too would fail every machine for
    # being honest about a named skip.
    problems = []
    fc, fp = int(floor.get("collected", 0)), int(floor.get("passed", 0))
    if fc and py["collected"] < fc:
        problems.append(
            f"collection DROPPED: {py['collected']} < floor {fc} — tests stopped "
            f"running, which looks identical to tests passing"
        )
    elif fc and py["collected"] > fc:
        # Both numbers AND the remedy, per the house rule that a gate must say what would clear
        # it. "387 > 386" alone invites a shrug; naming the exact integer to write does not.
        problems.append(
            f"{DRIFT_PREFIX}: {py['collected']} collected > floor {fc} — the floor is "
            f"{py['collected'] - fc} test(s) permissive, so that many could silently stop being "
            f"collected and still read as green. Set \"collected\": {py['collected']} and "
            f"\"passed\": {py['passed'] + py.get('skipped', 0)} in .verify-floor.json, keeping "
            f"the existing note, or run `python3 verify.py --update-floor`. If a branch merged "
            f"under you, REBASE FIRST: the number must be measured on the merge result."
        )
    if fp and py["passed"] + py.get("skipped", 0) < fp:
        problems.append(
            f"executed-or-skipped count dropped: {py['passed']} passed + "
            f"{py.get('skipped', 0)} skipped < floor {fp} — a test stopped being run "
            f"without becoming a named skip"
        )
    return problems


def _ceiling_problems(floor: dict, actual: dict) -> list[str]:
    """Is anything skipping MORE than the agreed maximum? Pure, for the same reason.

    An UNSET ceiling means "nothing agreed yet", not "zero" — reading a missing key as 0 would
    condemn every machine that legitimately lacks a prerequisite.
    """
    problems = []
    for key, label in CEILINGS:
        limit = floor.get(key)
        if limit is None:
            continue
        if actual.get(key, 0) > int(limit):
            problems.append(
                f"SKIP CEILING exceeded: {actual[key]} {label} > agreed maximum {limit}. "
                f"Skipping is bounded on purpose — either the new skip is wrong, or raise "
                f"`{key}` in .verify-floor.json deliberately and say why."
            )
    return problems


def verify(*, update_floor: bool = False) -> tuple[int, str]:
    py = run_pytest()
    floor = load_floor()
    mods = selftest_modules()
    st = run_selftests(mods)
    gates = run_gates()

    problems = []
    if py["usage_error"]:
        problems.append("pytest reported a usage error (and still exited 0)")
    if py["collected"] == 0:
        problems.append("pytest collected ZERO tests — the suite did not run")
    if py["failed"]:
        problems.append(f"{py['failed']} pytest failure(s)/error(s)")
    fc = int(floor.get("collected", 0))
    problems += _floor_problems(floor, py)
    if st["failed"]:
        problems.append(
            f"{len(st['failed'])} module selftest(s) failed: " f"{', '.join(sorted(st['failed']))}"
        )
    for name, res in gates.items():
        if not res["ok"]:
            problems.append(f"gate failed: {name}")

    # THE SKIP CEILING. Skipping is bounded, not open-ended: exceeding an agreed maximum fails,
    # so a future change cannot quietly convert a red into a skip. Each ceiling reports its own
    # value against the limit in the same breath, per the house rule that a gate must always say
    # both numbers — `24/24` alone reads as "fine", `24/24 (ceiling)` reads as "at the limit".
    actual = {
        "skipped_max": py["skipped"],
        "selftest_skipped_max": len(st["skipped"]),
        "gate_skipped_max": sum(1 for r in gates.values() if r["skipped"]),
    }
    problems += _ceiling_problems(floor, actual)

    def _cap(key: str) -> str:
        """Both numbers, always in the same place: the count AND what bounds it."""
        limit = floor.get(key)
        return f"{actual[key]}" + (f"/{limit} max" if limit is not None else " (no ceiling set)")

    # The floor reports its RELATIONSHIP to reality, not just its value. "floor 386" reads as
    # fine at a glance; "floor 386 — 1 BEHIND" cannot be misread, which is the same house rule
    # that makes each ceiling print its count against its limit.
    floor_state = (
        "unset"
        if not fc
        else (
            f"{fc}"
            if py["collected"] == fc
            else (
                f"{fc} — {py['collected'] - fc} BEHIND"
                if py["collected"] > fc
                else f"{fc} — NOT MET"
            )
        )
    )
    lines = ["# verify.py", ""]
    lines.append(
        f"  pytest:     {py['passed']} passed, {py['failed']} failed, "
        f"{_cap('skipped_max')} skipped "
        f"({py['collected']} collected; floor {floor_state})"
    )
    lines.append(
        f"  selftests:  {len(st['ok'])} of {len(mods)} modules ran, "
        f"{_cap('selftest_skipped_max')} skipped"
    )
    for name, res in gates.items():
        state = "SKIP" if res["skipped"] else ("ok " if res["ok"] else "FAIL")
        lines.append(f"  {name:<18} {state} {res['line']}")
    if actual["gate_skipped_max"]:
        lines.append(f"  gates:      {_cap('gate_skipped_max')} skipped")

    # Printed under a GREEN verdict as much as a red one: a row registered by another checkout can
    # be present while every check still passes, and the reader wants to know before the next merge.
    entrypoints = absent_entrypoint_line()
    if entrypoints:
        lines.append(entrypoints)

    # THE `ci` SURFACE'S CONSULT, printed under a green verdict as much as a red one. Computed
    # AFTER the gates above have already run, so even a future recording variant could not change
    # what this run reported on. Never appended to `problems`.
    lines.append(ci_consult_line())

    # WHAT DID NOT RUN, always — under a green verdict as much as a red one. A number of skips
    # with no reasons beside it is how "green" quietly stops meaning "checked".
    if py["skips"] or st["skipped"] or actual["gate_skipped_max"]:
        lines.append("")
        lines.append("  SKIPPED (prerequisite absent on this machine — nothing here was checked):")
        for ln in py["skips"]:
            lines.append(f"    pytest   {ln}")
        for mod, reasons in sorted(st["skipped"].items()):
            for reason in reasons:
                lines.append(f"    selftest {mod}: {reason}")
        for name, res in gates.items():
            for reason in res["skipped"]:
                lines.append(f"    gate     {name}: {reason}")

    lines.append("")
    if problems:
        lines.append("  PROBLEMS:")
        lines += [f"    - {p}" for p in problems]
        for mod, why in sorted(st["failed"].items()):
            lines.append(f"    selftest {mod}: {why[:120]}")
        if py["failed"] or py["usage_error"]:
            # EVERY failure by name, then the tail for context. A truncated failure list costs a
            # whole CI round trip, which is what happened on the first run.
            lines += ["", f"  pytest failures ({len(py['failures'])}):"]
            lines += [f"    {ln}" for ln in py["failures"]]
            lines += ["", "  pytest tail:"] + [f"    {ln}" for ln in py["tail"]]
    else:
        lines.append(
            f"  VERIFIED — {py['passed']} tests actually executed and passed, "
            f"{len(st['ok'])} selftests spoke, "
            f"{len(gates) - actual['gate_skipped_max']} of {len(gates)} gates green"
            + (
                f"; {py['skipped']} test(s), {actual['selftest_skipped_max']} selftest(s) "
                f"and {actual['gate_skipped_max']} gate(s) SKIPPED for a named missing "
                f"prerequisite, listed above"
                if (py["skipped"] or actual["selftest_skipped_max"] or actual["gate_skipped_max"])
                else ""
            )
        )

    # Drift does NOT block the write — see `_blocks_floor_update`. A real failure still does.
    if update_floor and not _blocks_floor_update(problems):
        # `collected` and `passed` are re-measured; the CEILINGS are NOT. A ceiling re-recorded
        # from whatever the last run happened to skip is not a ceiling, it is a ratchet that
        # follows the leak — and on the machine that has every prerequisite it would record 0 and
        # fail every other machine. So the agreed maxima are preserved from the existing file and
        # only ever changed by hand, deliberately.
        # `passed + skipped`, not `passed`: the floor means "checks that ran or were named", so
        # it records the same number on a machine with every prerequisite and on one without.
        # Recording bare `passed` from a skipping machine would RATCHET THE FLOOR DOWN by exactly
        # the amount that was skipped — the floor following the leak instead of catching it.
        blob = {"collected": py["collected"], "passed": py["passed"] + py["skipped"]}
        for key, _label in CEILINGS:
            if floor.get(key) is not None:
                blob[key] = int(floor[key])
        blob["note"] = _appended_note(
            str(floor.get("note", "")),
            py["collected"],
            blob["passed"],
            _dt.datetime.now(_dt.timezone.utc).date().isoformat(),
        )
        FLOOR.write_text(json.dumps(blob, indent=1) + "\n", encoding="utf-8")
        lines.append(
            f"  floor updated: collected={py['collected']} passed={py['passed']} "
            f"(ceilings preserved)"
        )

    return (1 if problems else 0), "\n".join(lines) + "\n"


def _selftest() -> None:
    # Count parsing must survive the real shapes pytest emits.
    for text, expect_collected, expect_passed in (
        ("182 passed, 3 skipped in 41.20s", 185, 182),
        ("1 failed, 181 passed in 40s", 182, 181),
        ("5 passed in 1s", 5, 5),
        ("no tests ran in 0.01s", 0, 0),
    ):
        counts = {}
        for n, kind in COUNT_RE.findall(text):
            kind = "error" if kind.startswith("error") else kind
            counts[kind] = counts.get(kind, 0) + int(n)
        collected = sum(
            counts.get(k, 0) for k in ("passed", "failed", "error", "skipped", "xfailed", "xpassed")
        )
        assert collected == expect_collected, (text, collected)
        assert counts.get("passed", 0) == expect_passed, (text, counts)

    # Discovery must find real modules, and must not include test files or itself.
    mods = selftest_modules()
    assert mods, "no modules with --selftest discovered — discovery is broken"
    assert "verify" not in mods and not any(m.startswith("test_") for m in mods), mods
    assert "capability_admission" in mods, mods

    # A SILENT ZERO-EXIT MUST BE A FAILURE. This is the exact hole that let 25 pytest-only files
    # read as passing: they exited 0 having executed nothing. Point run_selftests at a module that
    # does precisely that and confirm it is classified as failed, not ok.
    import tempfile

    saved = globals()["HERE"]
    with tempfile.TemporaryDirectory(prefix="verify-") as td:
        (pathlib.Path(td) / "silent_mod.py").write_text(
            'import sys\nif "--selftest" in sys.argv:\n    sys.exit(0)\n'
        )
        (pathlib.Path(td) / "loud_mod.py").write_text(
            'import sys\nif "--selftest" in sys.argv:\n    print("loud selftest: OK")\n'
        )
        # A LOUD ZERO-EXIT THAT EXECUTED NOTHING MUST NOT READ AS A PASS. This is the silent
        # zero-exit's twin, and the reason `ok` had to stop meaning "exited 0 and spoke": a
        # skipped selftest speaks. It must land in `skipped`, with its reason carried out.
        (pathlib.Path(td) / "skipping_mod.py").write_text(
            "import sys\n"
            'if "--selftest" in sys.argv:\n'
            f'    print("skipping_mod selftest: {PREREQ_ABSENT_MARK} the widget is not installed")\n'
        )
        try:
            globals()["HERE"] = pathlib.Path(td)
            got = run_selftests(["silent_mod", "loud_mod", "skipping_mod"])
        finally:
            globals()["HERE"] = saved
    assert "silent_mod" in got["failed"], f"a silent zero-exit must FAIL: {got}"
    assert "did it run?" in got["failed"]["silent_mod"], got
    assert got["ok"] == ["loud_mod"], f"a skipped selftest must not be counted as ok: {got}"
    assert got["skipped"] == {"skipping_mod": ["the widget is not installed"]}, got

    # THE -r FLAG MUST NOT SUPPRESS FAILURES. Passing `-rs` replaces pytest's default `-rfE`, so
    # skip reasons arrive and FAILED lines silently stop — which is how a run with 1 real failure
    # printed "pytest failures (0)" from the code added to stop exactly that. Assert on the flag,
    # because the symptom only shows on a red run and a green suite would never reveal it.
    import inspect

    src = inspect.getsource(run_pytest)
    flag = [tok for tok in src.split() if tok.strip("\",'").startswith("-r")]
    assert flag, "run_pytest no longer passes an -r flag; skip reasons would vanish"
    got_flag = flag[0].strip("\",'")
    for letter, what in (("f", "failures"), ("E", "errors"), ("s", "skip reasons")):
        assert letter in got_flag[2:], (
            f"{got_flag} omits {letter!r}: {what} would not be listed. pytest's default is -rfE, "
            f"so any -r you pass must re-include f and E as well as s"
        )

    # ---- THE SKIP CEILING, in both directions -------------------------------------------------
    # Bounding skips is the whole reason skipping was allowed at all, so the bound is tested the
    # way a gate must be: it has to FAIL when exceeded and PASS when not, and the failure has to
    # name the key to raise. `_ceiling_problems` is the same code path `verify()` uses.
    at_limit = _ceiling_problems(
        {"skipped_max": 24}, {"skipped_max": 24, "selftest_skipped_max": 0, "gate_skipped_max": 0}
    )
    assert at_limit == [], f"exactly at the ceiling must pass: {at_limit}"
    over = _ceiling_problems(
        {"skipped_max": 24}, {"skipped_max": 25, "selftest_skipped_max": 0, "gate_skipped_max": 0}
    )
    assert len(over) == 1 and "SKIP CEILING exceeded" in over[0], over
    assert "skipped_max" in over[0], "the failure must name the key to raise deliberately"
    # Each ceiling is independent — one slipping must not be masked by the others holding.
    for key in ("selftest_skipped_max", "gate_skipped_max"):
        counts = {"skipped_max": 0, "selftest_skipped_max": 0, "gate_skipped_max": 0}
        counts[key] = 3
        got_c = _ceiling_problems({key: 2}, counts)
        assert len(got_c) == 1 and key in got_c[0], (key, got_c)
    # An UNSET ceiling is not a ceiling of zero — it means nothing has been agreed yet. Reading
    # `None` as 0 would fail every machine that legitimately skips anything.
    assert (
        _ceiling_problems({}, {"skipped_max": 99, "selftest_skipped_max": 9, "gate_skipped_max": 9})
        == []
    )

    # ---- the floor counts what RAN OR WAS NAMED, and the ceiling stops that being a loophole --
    # 330 -> 300 passed with 30 named skips is fine; 300 passed with 0 skips is a test that
    # vanished. Both directions, because only having one is how a floor becomes decoration.
    assert (
        _floor_problems(
            {"collected": 330, "passed": 330}, {"collected": 330, "passed": 306, "skipped": 24}
        )
        == []
    )
    dropped = _floor_problems(
        {"collected": 330, "passed": 330}, {"collected": 330, "passed": 306, "skipped": 0}
    )
    assert len(dropped) == 1 and "dropped" in dropped[0], dropped
    shrank = _floor_problems(
        {"collected": 330, "passed": 330}, {"collected": 320, "passed": 320, "skipped": 0}
    )
    assert any("collection DROPPED" in p for p in shrank), shrank

    # ---- and the floor may not fall BEHIND reality either (2026-08-23) ------------------------
    # The permissive direction, silent until this was added: a branch adds tests, leaves the file
    # alone, and the floor is now slack by exactly the number added. DELIBERATE-BREAK DEMO: revert
    # the `elif` in `_floor_problems` and this assert fails while every other check here still
    # passes — which is precisely the shape of the bug, a real hole that reads as green.
    behind = _floor_problems(
        {"collected": 366, "passed": 366}, {"collected": 368, "passed": 368, "skipped": 0}
    )
    assert len(behind) == 1 and "BEHIND reality" in behind[0], behind
    # It must name the exact integers to write; "too low" that does not say the number is how a
    # gate becomes something people shrug at rather than clear.
    assert '"collected": 368' in behind[0] and '"passed": 368' in behind[0], behind
    assert "REBASE FIRST" in behind[0], behind
    # Exact agreement is the only clean state, and a machine that SKIPS is still exact: skipped
    # tests are collected, so the runner and the owner's machine hit the same equality.
    assert (
        _floor_problems(
            {"collected": 368, "passed": 368}, {"collected": 368, "passed": 344, "skipped": 24}
        )
        == []
    )
    # An UNSET floor still means "nothing agreed yet" — it must not suddenly demand equality.
    assert _floor_problems({}, {"collected": 368, "passed": 368, "skipped": 0}) == []

    # ---- --update-floor can run while the floor gate is CLOSED (latched-gate fix) -------------
    # DELIBERATE-BREAK DEMO: change the guard back to `not problems` and the remedy the drift
    # message names becomes unreachable — the gate forbidding its own drain, which is the defect
    # this repo hits most. The predicate is what keeps the two windows the same size.
    assert _blocks_floor_update(behind) == [], behind
    assert _blocks_floor_update(behind + ["3 pytest failure(s)/error(s)"]) == [
        "3 pytest failure(s)/error(s)"
    ]
    # A collection DROP is not drift and must keep blocking: recording a floor from a run that
    # lost tests would bake the loss in as the new normal.
    assert _blocks_floor_update(shrank) != []
    # ...and the predicate must be WIRED, not merely present. The three asserts above all pass
    # while the call site still reads `not problems` -- the helper exists, nothing calls it,
    # built-but-not-wired, which is this repo's founding defect wearing yet another hat. It got
    # through the first draft of this very change and was caught only by the deliberate-break
    # demo, so the guard line itself is now the assertion.
    _src = pathlib.Path(__file__).read_text(encoding="utf-8")
    # The needle is BUILT FROM TWO PIECES on purpose. Written as one literal it would appear in
    # this very line, so `_src` would contain it no matter what the call site said and the assert
    # could never fail -- a test that cannot fail, guarding against a defect that already
    # happened once. Split, the joined form exists only at the real guard.
    _guard = "if update_floor and not " + "_blocks_floor_update(problems):"
    assert _guard in _src, (
        "the --update-floor guard no longer calls _blocks_floor_update — a floor behind reality "
        "would once again block the one command that fixes it"
    )

    # ---- --update-floor APPENDS to the note, so the ceiling rationale survives the tool -------
    # DELIBERATE-BREAK DEMO: restore the old `blob["note"] = (...)` literal and the first assert
    # fails — the prior text, which is the only record of why each ceiling is what it is, is gone.
    kept = _appended_note(
        "26/7/2 is what a bare runner skips, measured 2026-08-21.", 387, 387, "2026-08-23"
    )
    assert kept.startswith("26/7/2 is what a bare runner skips, measured 2026-08-21."), kept
    assert "collected=387" in kept and "2026-08-23" in kept, kept
    assert _appended_note("", 387, 387, "2026-08-23").startswith("FLOOR RECORDED")
    assert _appended_note(None, 1, 1, "2026-08-23")  # a missing note is not a crash

    # ---- the absent-module summary line -------------------------------------------------------
    # A row registered by another checkout makes three checks fail with messages that read
    # "registered with no implementation — retire it", and retiring it discards finished work. The
    # line exists so the reader meets that fact in the SUMMARY. Two properties are load-bearing:
    # it stays silent when there is nothing to say, and it is NOT a skip.
    assert _format_absent_line({"absent": [], "checked": 43, "total": 43}) is None
    assert _format_absent_line({}) is None
    one = _format_absent_line(
        {
            "total": 43,
            "checked": 43,
            "absent": [
                {
                    "capability_id": "evidence-acquisition",
                    "entrypoint": "evidence_acquisition.py:run",
                    "found_in": [
                        {"checkout": ".claude/worktrees/other", "modules": ["x.py"]},
                        {"checkout": "repo root", "modules": ["x.py"]},
                    ],
                }
            ],
        }
    )
    for phrase in (
        "1 of 43",
        "evidence-acquisition",
        ".claude/worktrees/other",
        "+1 more",
        "WAIT-OR-MERGE",
    ):
        assert phrase in one, (phrase, one)
    # `PREREQ_ABSENT_MARK` is how verify.py counts a SKIP against the ceiling. Nothing was
    # skipped here, so carrying the mark would spend ceiling headroom that belongs to a real
    # missing prerequisite — and would make a diagnostic look like a narrowing of the suite.
    assert PREREQ_ABSENT_MARK not in one, one
    # A row whose module is nowhere at all must say so rather than implying a sibling has it.
    nowhere = _format_absent_line(
        {
            "total": 43,
            "checked": 1,
            "absent": [{"capability_id": "ghost", "entrypoint": "ghost.py", "found_in": []}],
        }
    )
    assert "not found in any sibling checkout" in nowhere, nowhere

    # ---- the `ci` surface consult ---------------------------------------------------------------
    # verify.py IS the CI surface, and the capabilities bound to it had no caller. The consult must
    # be a REPORT and nothing else. Asserted on synthetic counts, never on which capability came
    # back: the ledger holds 43 rows here and ~14 on a runner, and a machine-local assertion in
    # exactly this position has shipped green-locally/red-on-CI twice.
    empty_ledger = _format_ci_consult_line(3, 0, 0, 0)
    populated = _format_ci_consult_line(3, 3, 42, 43)
    # BOTH NUMBERS IN BOTH CASES. A runner with no rows for the bound set must not read the same as
    # a surface that binds nothing, so the DECLARED count (identical on every machine) is always
    # printed beside the offered one.
    assert "declares 3" in empty_ledger and "0 present" in empty_ledger, empty_ledger
    assert "declares 3" in populated and "3 offered" in populated, populated
    # ...and the findability pair: bound count with the unbound count beside it, always. A bound
    # count alone reads as "fine" however many rows nothing can offer.
    assert "42/43" in populated and "1 bound to none" in populated, populated
    assert "0 bound to none" in _format_ci_consult_line(2, 2, 14, 14)
    # NOT A SKIP, in every branch. The mark is how a skip is counted against a ceiling that has
    # zero headroom on a bare runner, so carrying it would turn a report into an automatic red.
    for line in (empty_ledger, populated, ci_consult_line()):
        assert PREREQ_ABSENT_MARK not in line, line
        assert line.startswith("  ci consult:"), line
    # A BROKEN CONSULT IS REPORTED AND STILL NOT A VERDICT. `problems` is built only from pytest,
    # selftests, gates, the floor and the ceilings; this line is appended to `lines` afterwards and
    # can never reach it. Pinned by construction: the renderer returns a string, never a problem.
    assert isinstance(ci_consult_line(), str)

    print(
        "verify.py selftest: OK (count parsing, selftest discovery, silent-zero-exit is a "
        "FAILURE, a loud skip is not a pass, skip ceiling fails when exceeded and holds when "
        "not, floor counts passed+skipped, floor fails BEHIND reality as loudly as below "
        "it and names the integers to write, --update-floor appends to the note instead of "
        "clobbering the ceiling rationale, absent-module line is silent when clean and is "
        "never counted as a skip, ci consult reports both numbers and is never a skip)"
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument(
        "--update-floor",
        action="store_true",
        help="record the current counts as the floor (only when everything passes)",
    )
    args = ap.parse_args()
    if args.selftest:
        _selftest()
        return 0
    code, text = verify(update_floor=args.update_floor)
    print(text, end="")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
