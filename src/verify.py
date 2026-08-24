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
import os
import pathlib
import re
import subprocess
import sys

# TWO ROOTS, and verify.py is the module that most needs them separated: it DISCOVERS modules
# (beside itself) and it READS repo files — the floor, the coverage artifacts — and RUNS pytest,
# all of which belong to the checkout. They are the same directory only on a flat tree.
HERE = pathlib.Path(__file__).resolve().parent
try:
    import paths

    MODULES = paths.MODULE_DIR
    ROOT = paths.checkout_root(HERE)
except Exception:  # noqa: BLE001 — verify.py must run even if a sibling module is broken
    MODULES = ROOT = HERE
FLOOR = ROOT / ".verify-floor.json"

# --- coverage instrumentation, OFF unless asked for -------------------------------------------
# Every runner below spawns a SUBPROCESS. That is deliberate (a selftest must be exercised the way
# it actually ships, and one module's crash must not take the harness with it), but it means a
# coverage run wrapped around verify.py itself sees none of the work. Since a per-module
# `--selftest` is this project's primary test mechanism -- 78 modules have no test_*.py at all,
# ~79.6% of non-test root Python -- "coverage" measured over pytest alone reports a blind spot and
# calls it a score. The measured gap on 2026-08-23: outcomes.py reported 9.0% and is 62% under its
# own selftest; adversarial.py 14.1% vs 88%; gh_capacity.py 14.6% vs 86%; keepalive_outcomes.py
# 14.0% vs 78%. All four selftests exit 0. Twelve of the twelve modules the report named as worst
# were selftest-only.
#
# So `--coverage` prefixes each child with `-m coverage run --parallel-mode` and combines the data
# files afterwards. It is OFF by default and MUST stay off: instrumenting ~90 subprocesses is much
# slower, and the default path is the one that produces the verdict. Turning it on changes what is
# MEASURED, never what is asserted -- every count, floor check and gate behaves identically.
COVERAGE = False


def child_argv(argv: list[str]) -> list[str]:
    """Wrap a child command in `coverage run --parallel-mode`, or return it unchanged.

    argv always starts with sys.executable. Coverage is inserted after it, so `-m pytest ...` and
    `module.py --selftest` are both handled without the caller knowing which form it passed.
    """
    if not COVERAGE:
        return argv
    return [argv[0], "-m", "coverage", "run", "--parallel-mode", *argv[1:]]


def coverage_reset() -> None:
    """Delete stale parallel data files so a combine cannot mix runs."""
    for stale in list(ROOT.glob(".coverage.*")) + [ROOT / ".coverage"]:
        try:
            stale.unlink()
        except OSError:
            pass


def coverage_combine_and_report() -> str:
    """Combine the per-process data files and return the report, or say why there is none.

    An absent or empty data set is reported as such rather than as 0% or as silence: a coverage
    number nobody produced must not be mistaken for a coverage number that is bad, and neither may
    look like success. That is the same rule the selftest three-way split follows.
    """
    files = sorted(ROOT.glob(".coverage.*"))
    if not files:
        return "coverage: NO DATA — no instrumented child wrote a data file (did any test run?)\n"
    subprocess.run(
        [sys.executable, "-m", "coverage", "combine", "--quiet"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    proc = subprocess.run(
        [sys.executable, "-m", "coverage", "report"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    body = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0 and not body.strip():
        return "coverage: FAILED to report and said nothing — treat as no measurement\n"
    return f"coverage: combined {len(files)} instrumented process(es)\n{body}"


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
    cmd = child_argv(
        [sys.executable, "-m", "pytest", "-q", "-rfEs", "-p", "no:cacheprovider", "--no-header"]
    )
    cmd += extra or []
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
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
    for path in sorted(MODULES.glob("*.py")):
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
            child_argv([sys.executable, str(MODULES / f"{mod}.py"), "--selftest"]),
            cwd=ROOT,
            capture_output=True,
            text=True,
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


# Two of the five gates are TEST files and three are modules, so they no longer live in one
# directory. Named by bare filename and resolved below — a hardcoded prefix per entry would be a
# second place for the layout to be recorded, and those drift.
GATES = (
    ("activation audit", ["capability_activation_audit.py", "--no-cache"]),
    ("recurrence replay", ["capability_recurrence_check.py"]),
    ("set coverage", ["test_capability_set_coverage.py"]),
    ("admission", ["test_capability_admission.py"]),
    ("ledger validate", ["capabilities.py", "--json", "validate"]),
)


def _gate_script(name: str) -> str:
    """Absolute path to a gate's script, whether it is a module or a test file."""
    for base in (MODULES, ROOT / "tests"):
        candidate = base / name
        if candidate.is_file():
            return str(candidate)
    # Absent is reported by the runner as a failure, never silently skipped — so return the
    # module-dir guess and let the "cannot open file" surface with the name in it.
    return str(MODULES / name)


def _child_env() -> dict:
    """`src` on PYTHONPATH, because two of the gates are TEST files.

    Running `python3 tests/test_capability_admission.py` puts `tests/` on `sys.path`, not `src/`, so
    its `import capabilities` fails — the gate reported `ModuleNotFoundError` rather than a verdict.
    pytest gets this from `pythonpath = ["src"]` in pyproject.toml; a bare subprocess has to be told.
    Prepended rather than replacing any inherited PYTHONPATH.
    """
    env = dict(os.environ)
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = f"{MODULES}{os.pathsep}{existing}" if existing else str(MODULES)
    return env


def run_gates() -> dict:
    out = {}
    for name, argv in GATES:
        proc = subprocess.run(
            child_argv([sys.executable, _gate_script(argv[0]), *argv[1:]]),
            cwd=ROOT,
            capture_output=True,
            text=True,
            env=_child_env(),
        )
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


def _failing_problems(
    problems: list[str], *, forgive_healed_drift: bool, wrote_floor: bool
) -> list[str]:
    """Which problems make the run RED? Normally all of them. Pure, so the selftest holds the rule.

    WHY A NARROWER ANSWER EXISTS AT ALL. `collected` is an equality, so a floor left behind
    reality is a problem — correctly, it is permissive by exactly the gap. But the number is only
    knowable on the MERGE RESULT, so on a busy day every concurrent branch records a value that
    the next merge invalidates. The drain (write the two integers) is one command, but nothing
    ran it automatically, which is how the floor came to be found behind reality four separate
    times in this file's own history.

    Under `--reconcile-floor` a drift that THIS RUN JUST HEALED is not a failure: the file now
    records what was measured in this same process, in this same tree, so there is nothing left
    for anyone to do about it. The measuring window and the draining window are the same run —
    the house rule about one constant serving both sides, taken to its limit.

    TWO THINGS KEEP THIS FROM BECOMING A GATE THAT CLEARS ITSELF:

    * It reuses `_blocks_floor_update`, the SAME predicate that decided the write. A second
      literal listing "which problems are drift" would be a matching pair, and a drifted pair
      would either re-latch the gate or forgive a real failure.
    * It is gated on `wrote_floor`, NOT on the flag. If the write did not happen — because a real
      problem blocked it — every problem still fails, drift included. Forgiving an UNHEALED drift
      would be exactly the defect this repo names most often: a gate that opens because it was
      asked nicely rather than because the condition changed.

    What it can never do is LOWER a floor. `_blocks_floor_update` keeps blocking on a collection
    DROP, so the write never happens in that direction and there is nothing to forgive. Raising a
    floor to observed reality makes the gate STRICTER, never weaker.
    """
    if forgive_healed_drift and wrote_floor:
        return _blocks_floor_update(problems)
    return problems


def _floor_needs_writing(floor: dict, collected: int, passed: int) -> bool:
    """Would writing actually change the recorded numbers? Pure, so the selftest holds the rule.

    THE DEFECT THIS CLOSES, and it was introduced by automating the write. `--update-floor` wrote
    on every fully-green run, drift or not: with no problems at all, `_blocks_floor_update([])` is
    empty and the guard passes. Harmless while a human ran it by hand — the redundant write
    recorded the same integers and appended one dated stamp. But ci.yml's reconcile job runs it on
    EVERY push to main, so each push produced a bot commit that changed nothing but the note, and
    the note grew +150 chars a time: 22217 -> 22367 -> 22517 over three pushes, measured. A commit
    that records nothing is noise on main, and unbounded growth in the one file whose note is
    load-bearing documentation is worse than noise.

    A no-op write also costs the audit trail nothing: a "FLOOR RECORDED" stamp for a write that
    changed no number is a record of nothing having happened.

    DRIFT ALWAYS STILL WRITES, which is what keeps the drain working. Drift means
    `collected != floor["collected"]` by definition, so this predicate is True in exactly the case
    the reconcile job exists for. An UNSET floor also writes: `{}` reads as 0, which differs from
    any real count, so a fresh checkout still records its first floor.
    """
    return (
        int(floor.get("collected", 0) or 0) != collected
        or int(floor.get("passed", 0) or 0) != passed
    )


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
    # The mypy exempt list is bounded exactly like skipping, and for the same reason: a list of
    # type-check exemptions that can only GROW is an amnesty with a deadline nobody set. ONE
    # constant, defined once in the floor file and consumed by both the count and the bound — a
    # matching pair of literals would drift, a shared name cannot.
    ("mypy_exempt_max", "module(s) exempt from mypy"),
)


def mypy_exempt_modules() -> list[str] | None:
    """Modules on `[[tool.mypy.overrides]] ignore_errors` — the ratchet's blocking quantity.

    None means the question could not be answered here (no pyproject.toml, unreadable, no override).
    REPORTED, never treated as zero: a ratchet that stops being counted is indistinguishable from
    one that emptied, and only one of those is good news.
    """
    try:
        import tomllib

        data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    for override in data.get("tool", {}).get("mypy", {}).get("overrides") or []:
        if override.get("ignore_errors"):
            mods = override.get("module")
            return sorted(mods) if isinstance(mods, list) else [str(mods)]
    return None


def _format_mypy_exempt_line(mods: list[str] | None, limit: int | None, total: int = 99) -> str:
    """One line carrying BOTH numbers, per the runtime rule in CLAUDE.md. PURE.

    `typecheck: on` alone reads as "typed". `66/66 max of 99 modules exempt` reads as "typed where
    it is checked, and here is exactly how much is not" — the difference between a ratchet and an
    amnesty.
    """
    if mods is None:
        return "  mypy ratchet: NOT COUNTED (no readable ignore_errors override in pyproject.toml)"
    bound = f"/{limit} max" if limit is not None else " (no ceiling set)"
    tail = " — type a module, delete its line" if mods else " — fully drained"
    return (
        f"  mypy ratchet: {len(mods)}{bound} of {total} module(s) exempt, "
        f"{total - len(mods)} checked{tail}"
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
                # `CEILING`, not `SKIP CEILING`: the mypy exempt list is bounded by this same
                # machinery and is not a skip, so the old wording sent a reader hunting for a skip
                # that does not exist. The label already names WHICH population overflowed.
                f"CEILING exceeded: {actual[key]} {label} > agreed maximum {limit}. "
                f"This is bounded on purpose — either the new one is wrong, or raise "
                f"`{key}` in .verify-floor.json deliberately and say why."
            )
    return problems


def verify(*, update_floor: bool = False, forgive_healed_drift: bool = False) -> tuple[int, str]:
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
    exempt = mypy_exempt_modules()
    actual = {
        "skipped_max": py["skipped"],
        "selftest_skipped_max": len(st["skipped"]),
        "gate_skipped_max": sum(1 for r in gates.values() if r["skipped"]),
        # None (uncountable) must not read as 0 — that would let the ceiling pass by being blind.
        "mypy_exempt_max": len(exempt) if exempt is not None else 0,
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
    # ALWAYS printed: a drained ratchet is news, and a ratchet that stopped being counted is
    # exactly what this line exists to expose.
    lines.append(_format_mypy_exempt_line(exempt, floor.get("mypy_exempt_max")))

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
    wrote_floor = False
    if (
        update_floor
        and not _blocks_floor_update(problems)
        # Third conjunct, and it is what stops the reconcile job committing on every push. See
        # `_floor_needs_writing`: drift is always still written, a no-op never is.
        and _floor_needs_writing(floor, py["collected"], py["passed"] + py["skipped"])
    ):
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
        wrote_floor = True
        # Report the number WRITTEN, not `py["passed"]`. The blob records `passed + skipped`
        # (machine-invariant, see above), so printing the bare pass count made the line disagree
        # with the file it had just written: the first production run logged `passed=416` while
        # recording 442. A log that contradicts the artifact is how a correct write comes to look
        # like a bug -- and how a real one could hide.
        lines.append(
            f"  floor updated: collected={blob['collected']} passed={blob['passed']} "
            f"(ceilings preserved)"
        )

    failing = _failing_problems(
        problems, forgive_healed_drift=forgive_healed_drift, wrote_floor=wrote_floor
    )
    return (1 if failing else 0), "\n".join(lines) + "\n"


def _selftest() -> None:
    # Count parsing must survive the real shapes pytest emits.
    for text, expect_collected, expect_passed in (
        ("182 passed, 3 skipped in 41.20s", 185, 182),
        ("1 failed, 181 passed in 40s", 182, 181),
        ("5 passed in 1s", 5, 5),
        ("no tests ran in 0.01s", 0, 0),
    ):
        counts: dict[str, int] = {}
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

    saved, saved_mods = globals()["HERE"], globals()["MODULES"]
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
            globals()["HERE"] = globals()["MODULES"] = pathlib.Path(td)
            got = run_selftests(["silent_mod", "loud_mod", "skipping_mod"])
        finally:
            globals()["HERE"], globals()["MODULES"] = saved, saved_mods
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
    assert len(over) == 1 and "CEILING exceeded" in over[0], over
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
    # Narrowed 2026-08-24 from the whole `if` line to just this conjunct. The guard legitimately
    # became a multi-line `if (...)` when `_floor_needs_writing` was ANDed in, which broke a
    # needle that pinned the entire single-line form — the same brittleness that made a reformat
    # look like a coverage regression in test_verify_coverage_mode.py. The conjunct is the part
    # that carries the meaning: pin that, not the formatting around it.
    _guard = "and not " + "_blocks_floor_update(problems)"
    assert _guard in _src, (
        "the --update-floor guard no longer calls _blocks_floor_update — a floor behind reality "
        "would once again block the one command that fixes it"
    )

    # ---- --reconcile-floor forgives a drift it HEALED, and nothing else ------------------------
    # DELIBERATE-BREAK DEMO: drop the `wrote_floor` conjunct from `_failing_problems` and the
    # third assert below fails — an UNHEALED drift starts passing, which is the gate opening
    # because it was asked rather than because the condition changed.
    _drift = [f"{DRIFT_PREFIX}: 417 collected > floor 416 — ..."]
    _real = ["3 pytest failure(s)/error(s)"]
    # Healed drift, reconciling: green. The file now says what this run measured.
    assert _failing_problems(_drift, forgive_healed_drift=True, wrote_floor=True) == []
    # Same drift, NOT reconciling: still red. The default path is unchanged, which is what keeps
    # a PR honest — its author still has to record the number on the merge result.
    assert _failing_problems(_drift, forgive_healed_drift=False, wrote_floor=True) == _drift
    # Drift that was NOT written (a real problem blocked the write): still red, drift included.
    assert _failing_problems(_drift, forgive_healed_drift=True, wrote_floor=False) == _drift
    # A real failure is never forgiven, even alongside a healed drift.
    assert _failing_problems(_drift + _real, forgive_healed_drift=True, wrote_floor=True) == _real
    # A collection DROP cannot reach forgiveness at all: `_blocks_floor_update` refuses the write,
    # so `wrote_floor` is False by construction. Asserted so the two guards stay coupled.
    _dropped = ["collection DROPPED: 400 < floor 416 — tests stopped running"]
    assert _blocks_floor_update(_dropped) == _dropped
    assert _failing_problems(_dropped, forgive_healed_drift=True, wrote_floor=False) == _dropped
    # ...and WIRED, not merely present — the same lesson as the guard above. Split literal so the
    # needle cannot match this line itself.
    _recon = "failing = _failing_problems(" + "\n"
    assert _recon in _src, (
        "verify() no longer routes its exit code through _failing_problems — --reconcile-floor "
        "would silently stop forgiving, or worse, forgive unconditionally"
    )

    # ---- a no-op floor write is not performed at all -----------------------------------------
    # DELIBERATE-BREAK DEMO: drop the `_floor_needs_writing` conjunct from the write guard and the
    # first assert below still passes (the predicate is pure and unaffected) but the WIRING assert
    # at the end fails. That ordering is the point: this defect shipped once already as a helper
    # that existed and a guard that ignored it.
    #
    # WHY IT MATTERS: --update-floor wrote on every green run, so ci.yml's reconcile job committed
    # to main on every push, changing nothing but appending a dated stamp — the note grew
    # 22217 -> 22367 -> 22517 over three pushes before this was caught.
    assert not _floor_needs_writing({"collected": 442, "passed": 442}, 442, 442)
    # Drift ALWAYS still writes — this is the case the reconcile job exists for, so the guard must
    # never suppress it.
    assert _floor_needs_writing({"collected": 441, "passed": 441}, 442, 442)
    # `passed` moving alone is still a real change worth recording.
    assert _floor_needs_writing({"collected": 442, "passed": 440}, 442, 442)
    # An UNSET floor must record its first value rather than read as "already correct".
    assert _floor_needs_writing({}, 442, 442)
    # A collection DROP would also "need writing", which is exactly why this conjunct is ANDed
    # with `_blocks_floor_update` and never replaces it: that one refuses the downward write.
    assert _floor_needs_writing({"collected": 442, "passed": 442}, 400, 400)
    assert _blocks_floor_update(["collection DROPPED: 400 < floor 442"]) != []
    # ...and WIRED. Split literal so the needle cannot match this line.
    _noop_guard = (
        "and _floor_needs_writing(floor, " + 'py["collected"], py["passed"] + py["skipped"])'
    )
    assert _noop_guard in _src, (
        "the write guard no longer consults _floor_needs_writing — the reconcile job will commit "
        "to main on every push again, appending a stamp that records nothing"
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
    assert one is not None, "the absent-module line must render for a non-empty report"
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
    assert nowhere is not None and "not found in any sibling checkout" in nowhere, nowhere

    # THE TWO ROOTS. On a flat tree they coincide, which is exactly why an assertion is needed:
    # without one, code that re-merged them would pass here and only fail after the layout moved.
    assert ROOT == paths.checkout_root(HERE), (ROOT, HERE)
    assert MODULES == HERE or MODULES.name == "src", (MODULES, HERE)
    assert FLOOR.parent == ROOT, "the floor belongs to the checkout, not the module dir"

    # ---- the mypy ratchet ---------------------------------------------------------------------
    # An exempt list that can only GROW is an amnesty with a deadline nobody set. Two properties
    # make it a ratchet: the count is always PRINTED with its bound, and it is CEILINGED by the
    # same generic machinery as the skips.
    assert "64/64 max of 99" in _format_mypy_exempt_line(["m"] * 64, 64)
    assert "35 checked" in _format_mypy_exempt_line(["m"] * 64, 64)
    assert "type a module, delete its line" in _format_mypy_exempt_line(["m"], 1)
    # A DRAINED ratchet must say so, not print a bare 0 — the drain finishing is news.
    assert "fully drained" in _format_mypy_exempt_line([], 64)
    # UNCOUNTABLE must never render as zero: a ratchet that stopped being counted looks identical
    # to one that emptied, and only one of those is good news.
    nc = _format_mypy_exempt_line(None, 64)
    assert "NOT COUNTED" in nc and " 0" not in nc, nc
    # It is not a skip, so it must not carry the mark that spends skip-ceiling headroom.
    assert PREREQ_ABSENT_MARK not in _format_mypy_exempt_line(["m"], 64)
    # Bounded by the SAME `_ceiling_problems` used for skips — one mechanism, so the measuring and
    # draining windows cannot drift apart.
    assert ("mypy_exempt_max", "module(s) exempt from mypy") in CEILINGS
    _zero = {k: 0 for k, _ in CEILINGS}
    assert _ceiling_problems({"mypy_exempt_max": 64}, {**_zero, "mypy_exempt_max": 64}) == []
    _over = _ceiling_problems({"mypy_exempt_max": 64}, {**_zero, "mypy_exempt_max": 65})
    assert len(_over) == 1 and "mypy_exempt_max" in _over[0], _over
    # And the real file must be readable, or the line would silently report NOT COUNTED forever.
    assert mypy_exempt_modules(), "pyproject.toml's ignore_errors override is unreadable"

    print(
        "verify.py selftest: OK (count parsing, selftest discovery, silent-zero-exit is a "
        "FAILURE, a loud skip is not a pass, skip ceiling fails when exceeded and holds when "
        "not, floor counts passed+skipped, floor fails BEHIND reality as loudly as below "
        "it and names the integers to write, --update-floor appends to the note instead of "
        "clobbering the ceiling rationale, a no-op floor write is skipped entirely, "
        "--reconcile-floor forgives ONLY a drift it "
        "healed and never an unwritten one, absent-module line is silent when clean and is "
        "never counted as a skip, mypy ratchet prints both numbers and its ceiling can fail)"
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument(
        "--update-floor",
        action="store_true",
        help="record the current counts as the floor (only when everything passes)",
    )
    ap.add_argument(
        "--reconcile-floor",
        action="store_true",
        help=(
            "like --update-floor, but a drift this run HEALED does not fail the run. For CI on "
            "main: heals a floor left behind reality, and still fails on anything real."
        ),
    )
    ap.add_argument(
        "--coverage",
        action="store_true",
        help=(
            "also measure coverage, by running each child under `coverage run --parallel-mode` "
            "and combining. Much slower; OFF by default. Needed because the per-module "
            "--selftest is a SUBPROCESS, so a pytest-only coverage run cannot see ~80%% of this "
            "codebase and reports the blind spot as a score."
        ),
    )
    args = ap.parse_args()
    if args.selftest:
        _selftest()
        return 0
    if args.coverage:
        globals()["COVERAGE"] = True
        coverage_reset()
    code, text = verify(
        update_floor=args.update_floor or args.reconcile_floor,
        forgive_healed_drift=args.reconcile_floor,
    )
    print(text, end="")
    if args.coverage:
        # Printed AFTER the verdict, and it never alters the exit code. Coverage is a measurement,
        # not a gate: making it one here would be a threshold nobody agreed to, against a number
        # that has been wrong until this run.
        print("\n" + coverage_combine_and_report(), end="")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
