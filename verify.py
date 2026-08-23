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
    identical to all tests passing.
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


def load_floor() -> dict:
    if FLOOR.exists():
        try:
            return json.loads(FLOOR.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return {}
    return {}


# Ceiling keys, and what each bounds. Named once so the check below and `--update-floor` cannot
# disagree about which number they mean.
CEILINGS = (
    ("skipped_max", "skipped test(s)"),
    ("selftest_skipped_max", "skipped selftest(s)"),
    ("gate_skipped_max", "skipped gate(s)"),
)


def _floor_problems(floor: dict, py: dict) -> list[str]:
    """Did the amount of CHECKING drop? Pure, so the selftest exercises the real rule.

    Two independent drops, both of which look like passing:
      * fewer tests COLLECTED — an import error, a rename, a deletion;
      * fewer tests passed-or-consciously-skipped — a test that stopped running without becoming
        a named skip. `passed` alone cannot be the floor once skipping is legitimate, or the
        machine missing a prerequisite fails for being honest; `passed + skipped` can be, and the
        ceiling is what stops the skipped side swallowing everything.
    """
    problems = []
    fc, fp = int(floor.get("collected", 0)), int(floor.get("passed", 0))
    if fc and py["collected"] < fc:
        problems.append(
            f"collection DROPPED: {py['collected']} < floor {fc} — tests stopped "
            f"running, which looks identical to tests passing"
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

    lines = ["# verify.py", ""]
    lines.append(
        f"  pytest:     {py['passed']} passed, {py['failed']} failed, "
        f"{_cap('skipped_max')} skipped "
        f"({py['collected']} collected; floor {fc or 'unset'})"
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

    if update_floor and not problems:
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
        blob["note"] = (
            "floor recorded by verify.py --update-floor; a later run collecting fewer "
            "tests FAILS, because silently running fewer tests looks exactly like "
            "passing. `passed` is compared against passed+skipped. The *_max ceilings "
            "bound skipping and are NOT re-measured here — edit them by hand."
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

    print(
        "verify.py selftest: OK (count parsing, selftest discovery, silent-zero-exit is a "
        "FAILURE, a loud skip is not a pass, skip ceiling fails when exceeded and holds when "
        "not, floor counts passed+skipped, absent-module line is silent when clean and is "
        "never counted as a skip)"
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
