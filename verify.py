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

# pytest's terse summary line, e.g. "182 passed, 3 skipped in 41.20s"
COUNT_RE = re.compile(r"(\d+) (passed|failed|error|errors|skipped|xfailed|xpassed)")


def run_pytest(*, extra: list[str] | None = None) -> dict:
    """Execute the suite and read the COUNTS, not the exit code."""
    cmd = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--no-header"]
    cmd += extra or []
    proc = subprocess.run(cmd, cwd=HERE, capture_output=True, text=True)
    tail = (proc.stdout or "") + (proc.stderr or "")
    counts: dict[str, int] = {}
    for n, kind in COUNT_RE.findall(tail):
        kind = "error" if kind.startswith("error") else kind
        counts[kind] = counts.get(kind, 0) + int(n)
    collected = sum(counts.get(k, 0) for k in
                    ("passed", "failed", "error", "skipped", "xfailed", "xpassed"))
    # A usage error prints to stderr and exits 0. Absence of counts is therefore a failure, never
    # an empty success.
    usage_error = "usage: pytest" in tail or "unrecognized arguments" in tail
    return {"counts": counts, "collected": collected, "passed": counts.get("passed", 0),
            "failed": counts.get("failed", 0) + counts.get("error", 0),
            "returncode": proc.returncode, "usage_error": usage_error,
            "tail": tail.strip().splitlines()[-12:]}


def selftest_modules() -> list[str]:
    """Discover modules exposing --selftest instead of hardcoding a list that goes stale."""
    found = []
    for path in sorted(HERE.glob("*.py")):
        if path.name.startswith("test_") or path.name == "verify.py":
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:                                          # noqa: BLE001
            continue
        if '"--selftest"' in text or "'--selftest'" in text:
            found.append(path.stem)
    return found


def run_selftests(modules: list[str]) -> dict:
    ok, bad = [], {}
    for mod in modules:
        proc = subprocess.run([sys.executable, f"{mod}.py", "--selftest"],
                              cwd=HERE, capture_output=True, text=True)
        # A selftest must both exit 0 AND say something. A silent zero-exit is the very failure
        # this module exists to catch.
        spoke = bool((proc.stdout or "").strip() or (proc.stderr or "").strip())
        if proc.returncode == 0 and spoke:
            ok.append(mod)
        else:
            bad[mod] = ("silent zero-exit — did it run?" if proc.returncode == 0
                        else ((proc.stdout or "") + (proc.stderr or "")).strip()[-200:])
    return {"ok": ok, "failed": bad}


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
        proc = subprocess.run([sys.executable, *argv], cwd=HERE,
                              capture_output=True, text=True)
        text = (proc.stdout or "") + (proc.stderr or "")
        out[name] = {"ok": proc.returncode == 0, "line": _headline(name, text)}
    return out


def _headline(name: str, text: str) -> str:
    wanted = {"activation audit": r"CAN FIRE:.*", "recurrence replay": r"WOULD FIRE:.*",
              "set coverage": r"all \d+ capability-set.*", "admission": r"all \d+ admission.*",
              "ledger validate": r'"valid": \w+'}
    pat = wanted.get(name)
    if pat:
        m = re.search(pat, text)
        if m:
            return m.group(0).strip()
    return (text.strip().splitlines() or ["(no output)"])[-1][:100]


def load_floor() -> dict:
    if FLOOR.exists():
        try:
            return json.loads(FLOOR.read_text(encoding="utf-8"))
        except Exception:                                          # noqa: BLE001
            return {}
    return {}


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
    fc, fp = int(floor.get("collected", 0)), int(floor.get("passed", 0))
    if fc and py["collected"] < fc:
        problems.append(f"collection DROPPED: {py['collected']} < floor {fc} — tests stopped "
                        f"running, which looks identical to tests passing")
    if fp and py["passed"] < fp:
        problems.append(f"passing count dropped: {py['passed']} < floor {fp}")
    if st["failed"]:
        problems.append(f"{len(st['failed'])} module selftest(s) failed: "
                        f"{', '.join(sorted(st['failed']))}")
    for name, res in gates.items():
        if not res["ok"]:
            problems.append(f"gate failed: {name}")

    lines = ["# verify.py", ""]
    lines.append(f"  pytest:     {py['passed']} passed, {py['failed']} failed, "
                 f"{py['counts'].get('skipped', 0)} skipped "
                 f"({py['collected']} collected; floor {fc or 'unset'})")
    lines.append(f"  selftests:  {len(st['ok'])} of {len(mods)} modules exposing --selftest")
    for name, res in gates.items():
        lines.append(f"  {name:<18} {'ok ' if res['ok'] else 'FAIL'} {res['line']}")
    lines.append("")
    if problems:
        lines.append("  PROBLEMS:")
        lines += [f"    - {p}" for p in problems]
        for mod, why in sorted(st["failed"].items()):
            lines.append(f"    selftest {mod}: {why[:120]}")
        if py["failed"] or py["usage_error"]:
            lines += ["", "  pytest tail:"] + [f"    {ln}" for ln in py["tail"]]
    else:
        lines.append(f"  VERIFIED — {py['passed']} tests actually executed and passed, "
                     f"{len(st['ok'])} selftests spoke, {len(gates)} gates green")

    if update_floor and not problems:
        FLOOR.write_text(json.dumps({"collected": py["collected"], "passed": py["passed"],
                                     "note": "floor recorded by verify.py --update-floor; a later "
                                             "run collecting fewer tests FAILS, because silently "
                                             "running fewer tests looks exactly like passing"},
                                    indent=1) + "\n", encoding="utf-8")
        lines.append(f"  floor updated: collected={py['collected']} passed={py['passed']}")

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
        collected = sum(counts.get(k, 0) for k in
                        ("passed", "failed", "error", "skipped", "xfailed", "xpassed"))
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
            'import sys\nif "--selftest" in sys.argv:\n    sys.exit(0)\n')
        (pathlib.Path(td) / "loud_mod.py").write_text(
            'import sys\nif "--selftest" in sys.argv:\n    print("loud selftest: OK")\n')
        try:
            globals()["HERE"] = pathlib.Path(td)
            got = run_selftests(["silent_mod", "loud_mod"])
        finally:
            globals()["HERE"] = saved
    assert "silent_mod" in got["failed"], f"a silent zero-exit must FAIL: {got}"
    assert "did it run?" in got["failed"]["silent_mod"], got
    assert got["ok"] == ["loud_mod"], got

    print("verify.py selftest: OK (count parsing, selftest discovery, silent-zero-exit is a FAILURE)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--update-floor", action="store_true",
                    help="record the current counts as the floor (only when everything passes)")
    args = ap.parse_args()
    if args.selftest:
        _selftest()
        return 0
    code, text = verify(update_floor=args.update_floor)
    print(text, end="")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
