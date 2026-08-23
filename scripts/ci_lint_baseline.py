#!/usr/bin/env python3
"""Re-measure the Gate's python-ci checks, and report BOTH quantities for each.

Why this exists rather than a paragraph in a doc. The Orchestrator's recurring defect is a gate
whose recorded reason outlives its evidence: a number is written down once, the situation changes,
and the stale prose keeps the gate shut. So the numbers in ``docs/CI_LINT_BASELINE.md`` are not
authoritative -- this script is. It runs the EXACT commands
``stranske/Workflows/.github/workflows/reusable-10-ci-python.yml`` runs, and prints, for every
check, the blocking quantity next to the drainable one.

The pair is the whole point. "588 mypy errors" reads as be-patient for months; "588 errors,
drainable 0 -- no mechanical fixer exists" reads as the deadlock it is, immediately.

``drainable`` here means one specific, checkable thing: **how much of the blocking quantity a
deterministic tool can remove**. It is derived, never asserted --

* ruff  -- what ``ruff check --fix`` actually rewrites, measured by running it in a scratch copy;
* black -- every file it reports, since ``black`` rewrites all of them;
* mypy  -- 0, because no ``mypy --fix`` exists. That is a property of the toolchain, not an opinion
           about this repo's difficulty;
* coverage -- measured by actually probing collection with the arguments the reusable workflow
           builds today. Until 2026-08-23 this was a standing 1/0: the workflow appended
           ``--cov-config=pyproject.toml`` unconditionally and no change inside this repo could
           satisfy both that and the editable install. stranske/Workflows#3202 drained it upstream,
           so the number is probed rather than asserted -- an upstream regression comes back as a
           count here instead of as silence (see ``--explain``).

Usage::

    python3 scripts/ci_lint_baseline.py            # table
    python3 scripts/ci_lint_baseline.py --json     # machine-readable
    python3 scripts/ci_lint_baseline.py --explain  # what drains each check

Version-sensitivity is enforced, not warned about: the counts depend on the exact tool versions,
so a mismatch against ``.github/workflows/autofix-versions.env`` exits non-zero rather than
printing numbers that describe a toolchain nobody runs. Ruff 0.16 widening its own default set
from 79 findings here to 915 is exactly the event this guard is for.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PIN_FILE = REPO / ".github" / "workflows" / "autofix-versions.env"

# The line length both the Gate's format check and Autofix hardcode on their command lines. One
# constant, read by this script and asserted against ruff.toml by test_ci_gate_config.py, so the
# three cannot drift apart. See ruff.toml for why it is 100 and not Black's default 88.
GATE_LINE_LENGTH = 100

# What would take each check to zero. Hoisted to module level so `--explain` can state a drain
# without running the tools: a check that cannot say what would clear it is already defective,
# so the answer must be cheap to ask for.
DRAINS = {
    "lint-ruff": "`ruff check --fix` for the mechanical share; hand edits for the rest",
    "lint-format": "one `black --line-length 100 .` run",
    "typecheck-mypy": "typed modules landing incrementally; there is no mechanical fixer",
    "coverage": (
        "nothing to drain while this reads 0. It was 1 until stranske/Workflows#3202 (merged "
        "2026-08-23) made `--cov-config` conditional on the file existing and gated the editable "
        "install on real packaging metadata; if it goes back to 1, the drain is upstream again"
    ),
}

# Exactly what reusable-10-ci-python.yml excludes, so the counts here are the counts there.
GATE_EXCLUDE_RUFF = ".workflows-lib"
GATE_EXCLUDE_BLACK = r"(\.venv|\.workflows-lib|node_modules)"


def read_pins() -> dict[str, str]:
    """Parse the pin file the Gate refuses to install without."""
    if not PIN_FILE.is_file():
        raise SystemExit(
            f"missing {PIN_FILE.relative_to(REPO)} -- the Gate's install step exits 1 without it, "
            "so none of these checks can run in CI at all. That absence, not any finding below, "
            "is what failed every python-ci job here until 2026-08-23."
        )
    pins: dict[str, str] = {}
    for line in PIN_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        pins[key.strip()] = value.strip()
    return pins


def installed_version(tool: str) -> str | None:
    """`tool --version`, reduced to the first dotted number it prints."""
    if shutil.which(tool) is None:
        return None
    out = subprocess.run([tool, "--version"], capture_output=True, text=True).stdout
    match = re.search(r"(\d+\.\d+(?:\.\d+)?)", out)
    return match.group(1) if match else None


def _run(cmd: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, cwd=cwd or REPO)


def _scratch_copy() -> tempfile.TemporaryDirectory:
    """A throwaway copy of the tracked tree, so `--fix` can be measured without touching it."""
    tmp = tempfile.TemporaryDirectory(prefix="ci-lint-baseline-")
    archive = _run(["git", "archive", "HEAD"])
    if archive.returncode != 0:
        raise SystemExit(f"git archive HEAD failed: {archive.stderr.strip()}")
    subprocess.run(
        ["tar", "-x", "-C", tmp.name],
        input=archive.stdout.encode("utf-8", "surrogateescape"),
        check=True,
    )
    return tmp


def measure_ruff() -> dict:
    """`ruff check` under this repo's ruff.toml -- the command the Gate runs once a config exists."""
    cmd = [
        "ruff",
        "check",
        "--extend-exclude",
        GATE_EXCLUDE_RUFF,
        "--output-format",
        "concise",
        ".",
    ]
    out = _run(cmd).stdout
    codes: dict[str, int] = {}
    for match in re.finditer(r"^.+?:\d+:\d+: ([A-Z]+\d+)", out, re.M):
        codes[match.group(1)] = codes.get(match.group(1), 0) + 1
    blocking = sum(codes.values())

    # Drainable = what `--fix` actually rewrites. Measured in a scratch copy, never in place.
    with _scratch_copy() as scratch:
        for extra in ([], ["--unsafe-fixes"]):
            _run(["ruff", "check", "--fix", "--exit-zero", *extra, "."], cwd=Path(scratch))
        after = _run(
            [
                "ruff",
                "check",
                "--extend-exclude",
                GATE_EXCLUDE_RUFF,
                "--output-format",
                "concise",
                ".",
            ],
            cwd=Path(scratch),
        ).stdout
        remaining = len(re.findall(r"^.+?:\d+:\d+: [A-Z]+\d+", after, re.M))
    return {
        "check": "lint-ruff",
        "command": " ".join(cmd),
        "blocking": blocking,
        "blocking_unit": "findings",
        "drainable": max(0, blocking - remaining),
        "drain": DRAINS["lint-ruff"],
        "by_code": dict(sorted(codes.items(), key=lambda kv: (-kv[1], kv[0]))),
    }


def measure_black() -> dict:
    """`black --check --line-length 100 ...` -- verbatim from the Gate's format job."""
    cmd = [
        "black",
        "--check",
        "--line-length",
        str(GATE_LINE_LENGTH),
        "--exclude",
        GATE_EXCLUDE_BLACK,
        ".",
    ]
    proc = _run(cmd)
    match = re.search(r"(\d+) files? would be reformatted", proc.stderr + proc.stdout)
    blocking = int(match.group(1)) if match else 0
    return {
        "check": "lint-format",
        "command": " ".join(cmd),
        "blocking": blocking,
        "blocking_unit": "files",
        # Black rewrites every file it reports, so the drain reaches the whole blocking set.
        "drainable": blocking,
        "drain": DRAINS["lint-format"],
        "by_code": {},
    }


def measure_mypy() -> dict:
    """`mypy --exclude .workflows-lib .` -- verbatim from the Gate's typecheck job."""
    cmd = ["mypy", "--exclude", GATE_EXCLUDE_RUFF, "."]
    out = _run(cmd).stdout
    codes: dict[str, int] = {}
    for match in re.finditer(r"\[([a-z][a-z-]+)\]\s*$", out, re.M):
        codes[match.group(1)] = codes.get(match.group(1), 0) + 1
    match = re.search(r"Found (\d+) errors? in (\d+) files?", out)
    blocking = int(match.group(1)) if match else len(codes)
    setup_abort = "errors prevented further checking" in out
    return {
        "check": "typecheck-mypy",
        "command": " ".join(cmd),
        "blocking": blocking,
        "blocking_unit": "errors",
        # No `mypy --fix` exists. This 0 is a fact about the toolchain, not a judgement about
        # how hard the work is.
        "drainable": 0,
        "drain": DRAINS["typecheck-mypy"],
        "files": int(match.group(2)) if match else None,
        "setup_abort": setup_abort,
        "by_code": dict(sorted(codes.items(), key=lambda kv: (-kv[1], kv[0]))),
    }


def measure_coverage() -> dict:
    """Probe collection with the arguments the reusable workflow builds, and count startup errors.

    Deliberately not a static verdict. The old form returned ``1`` whenever this repo had no
    ``pyproject.toml``, which encoded an upstream behaviour as a local fact -- so when
    stranske/Workflows#3202 changed that behaviour, the recorded reason would have outlived its
    evidence and kept the gate shut. Running the probe means an upstream regression reappears here
    as a number instead of as silence.
    """
    args = ["--cov"]
    if (REPO / "pyproject.toml").is_file():
        args.append("--cov-config=pyproject.toml")
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", *args, "--collect-only", "-q", "-p", "no:cacheprovider"],
        cwd=REPO,
        capture_output=True,
        text=True,
    )
    combined = proc.stdout + proc.stderr
    # A coverage misconfiguration aborts before collection; a collection error in the suite itself
    # is a different defect and is not this check's business.
    blocking = 1 if "coverage.exceptions" in combined or "CoverageException" in combined else 0
    return {
        "check": "coverage",
        "command": "pytest " + " ".join(args) + " (arguments built by the reusable workflow)",
        "blocking": blocking,
        "blocking_unit": "startup errors",
        "drainable": 0,
        "drain": DRAINS["coverage"],
        "by_code": {},
    }


MEASURERS = {
    "lint-ruff": ("ruff", "RUFF_VERSION", measure_ruff),
    "lint-format": ("black", "BLACK_VERSION", measure_black),
    "typecheck-mypy": ("mypy", "MYPY_VERSION", measure_mypy),
    "coverage": (None, None, measure_coverage),
}


def collect(*, strict: bool = True) -> dict:
    pins = read_pins()
    results, problems = [], []
    for name, (tool, pin_key, fn) in MEASURERS.items():
        if tool is not None:
            want = pins.get(pin_key or "")
            have = installed_version(tool)
            if have is None:
                problems.append(f"{tool} is not installed; {name} cannot be measured")
                continue
            if want and have != want:
                problems.append(
                    f"{tool} {have} installed but {want} pinned in "
                    f"{PIN_FILE.relative_to(REPO)} -- the counts are version-specific, so "
                    f"measuring with a different {tool} would record a baseline for a toolchain "
                    f"CI never runs"
                )
                continue
        results.append(fn())
    if problems and strict:
        for problem in problems:
            print(f"BLOCKED: {problem}", file=sys.stderr)
        raise SystemExit(2)
    return {"pins": pins, "checks": results, "problems": problems}


def render(report: dict) -> str:
    lines = ["", "Gate python-ci baseline -- blocking vs drainable", ""]
    lines.append(f"  {'check':<16}{'blocking':>22}{'drainable':>12}   drain")
    lines.append("  " + "-" * 16 + "-" * 22 + "-" * 12 + "   " + "-" * 40)
    for entry in report["checks"]:
        blocking = f"{entry['blocking']} {entry['blocking_unit']}"
        verdict = "DEADLOCK" if entry["blocking"] and not entry["drainable"] else ""
        lines.append(
            f"  {entry['check']:<16}{blocking:>22}{entry['drainable']:>12}   "
            f"{entry['drain'][:60]}{'  <-- ' + verdict if verdict else ''}"
        )
    lines.append("")
    for entry in report["checks"]:
        if entry.get("by_code"):
            top = ", ".join(f"{code} {count}" for code, count in list(entry["by_code"].items())[:8])
            lines.append(f"  {entry['check']}: {top}")
    if any(e.get("setup_abort") for e in report["checks"]):
        lines.append("")
        lines.append(
            "  NOTE: mypy aborted before checking everything -- a setup error is masking "
            "the real count. Fix module resolution (mypy.ini) before trusting it."
        )
    lines.append("")
    lines.append("  Recorded baseline and per-rule drains: docs/CI_LINT_BASELINE.md")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true", help="emit the report as JSON")
    parser.add_argument("--explain", action="store_true", help="print each check's drain and exit")
    parser.add_argument(
        "--lenient",
        action="store_true",
        help="report what is measurable instead of exiting on a version mismatch",
    )
    args = parser.parse_args(argv)

    if args.explain:
        for name in MEASURERS:
            print(f"{name}: {DRAINS[name]}")
        return 0

    report = collect(strict=not args.lenient)
    print(json.dumps(report, indent=2) if args.json else render(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
