#!/usr/bin/env python3
"""testgen_gate.py - assured-acceptance gate for generated tests.

docs/briefs/BRIEF_expand_range.md option #2. This widens the fleet from "implement the
given issue" to "propose coverage-raising tests, then accept only the tests that
survive a gate." The gate follows the transferable TestGen-LLM pattern:
collect/import -> non-regression -> repeated reliability -> coverage delta.

Live mode assumes a Python repo with pytest and coverage.py available. Coverage
artifacts are written under a temporary directory so the target repo is not
dirtied. Coverage JSON generation forces `--fail-under=0` so repo-wide coverage
thresholds cannot mask the gate's own covered-lines delta verdict. `--selftest`
is offline and checks only pure command/verdict helpers.
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
import sys
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

TAIL_STDOUT = 1200
TAIL_STDERR = 800

# ---------------------------------------------------------------------------
# MISUSE IS NOT A BAD-TEST VERDICT (2026-08-25).
#
# Both of this gate's argument-shaped failures reported as FAILED CHECKS rather than as misuse,
# which is the worst available failure mode for a gate: an agent that trusts the verdict concludes
# its TESTS are bad when in fact its INVOCATION was. Measured on two independent implementation runs:
#
#   * `--baseline-pytest-args "-k not (a or b)"` — the inner expression is unquoted, so the shell-
#     style split hands pytest `-k`, `not`, `(a`, `or`, `b)`. pytest collects 0 items and exits 5,
#     `baseline_non_regression` went False, and that is indistinguishable from a real regression in
#     the pre-existing tests. The gate accused the baseline of breaking.
#   * `--source src/pkg/mod.py` — normalised to the dotted name `src.pkg.mod`, which is not
#     importable when `src` is a source root rather than a package. Coverage measured NOTHING and
#     `coverage_delta` reported 0, which reads as "the new tests cover nothing". Same shape from a
#     second direction: a repo whose own `[tool.coverage.run] source` or `addopts = --cov=src` wins
#     over the requested source measures the WRONG tree and reports `0 / 11398`.
#
# Both are "could not measure" wearing the mask of "measured zero" — the same class #121 drained out
# of three other gates in this tree. The verdict still fails (an unmeasurable gate certifies
# nothing); what changes is that it now NAMES the misuse and the remedy instead of blaming the tests.
#
# ONE TABLE, so the classification cannot drift between the four checks that consume it.
PYTEST_EXIT_MEANINGS: dict[int, dict[str, Any]] = {
    0: {"meaning": "all selected tests passed", "measured": True},
    1: {"meaning": "tests ran and some FAILED", "measured": True},
    2: {"meaning": "pytest was interrupted", "measured": False},
    3: {"meaning": "internal pytest error", "measured": False},
    4: {
        "meaning": "pytest USAGE ERROR — it rejected these arguments, so nothing ran",
        "measured": False,
    },
    5: {
        "meaning": "NO TESTS WERE COLLECTED — the arguments selected nothing, so nothing ran",
        "measured": False,
    },
    124: {"meaning": "timed out before finishing", "measured": False},
}
# The remedy, attached to the two codes an argument mistake actually produces. A diagnosis without
# the fix is what sent one run hunting a test defect that did not exist.
PYTEST_EXIT_REMEDY: dict[int, str] = {
    4: (
        "check the pytest arguments passed to this gate; they are split shell-style, so an option "
        "value containing spaces must be quoted INSIDE the string "
        "(--baseline-pytest-args \"tests -k 'not slow'\")"
    ),
    5: (
        "the arguments matched no test. A `-k` expression containing spaces that was not quoted "
        "INSIDE the string is shredded into separate tokens by the shell-style split and silently "
        "selects nothing — quote it (\"tests -k 'not slow'\"), or use repeated --deselect <nodeid>, "
        "which contains no spaces"
    ),
}


def pytest_exit_meaning(code: int | None) -> dict[str, Any]:
    """What a pytest exit code MEANS, and whether anything was measured. One lookup, no drift."""
    row = PYTEST_EXIT_MEANINGS.get(code) if code is not None else None
    # `code is None` is repeated rather than implied by `row is None`: it is what narrows the type
    # for the `PYTEST_EXIT_REMEDY` lookup below, and mypy is right that the implication is not one
    # a reader should have to reconstruct either.
    if code is None or row is None:
        return {
            "exit_code": code,
            "meaning": "the command did not run to completion" if code is None else "unknown",
            "measured": False,
            "remedy": "",
        }
    return {
        "exit_code": code,
        "meaning": row["meaning"],
        "measured": bool(row["measured"]),
        "remedy": PYTEST_EXIT_REMEDY.get(code, ""),
    }


def tail(text: str, limit: int) -> str:
    """Return a bounded tail for JSON reports."""
    return text[-limit:] if text and len(text) > limit else (text or "")


def split_args(raw: str | None) -> list[str]:
    """Split a shell-style pytest argument string into argv tokens."""
    return shlex.split(raw or "")


def as_text(value: str | bytes | None) -> str:
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value or ""


def pytest_cmd(
    pytest_args: Sequence[str], *, collect_only: bool = False, cache_dir: Path | None = None
) -> list[str]:
    """Build the pytest command used by collection and reliability checks."""
    cmd = [sys.executable, "-m", "pytest"]
    if collect_only:
        cmd.append("--collect-only")
    if cache_dir is not None:
        cmd += ["-o", f"cache_dir={cache_dir}"]
    return cmd + list(pytest_args)


def coverage_sources(sources: Sequence[str]) -> list[str]:
    """Normalize coverage --source values to importable module names."""
    normalized: list[str] = []
    for source in sources:
        if source.endswith(".py"):
            normalized.append(Path(source).with_suffix("").as_posix().replace("/", "."))
        else:
            normalized.append(source)
    return normalized


def coverage_run_cmd(
    sources: Sequence[str],
    data_file: Path,
    pytest_args: Sequence[str],
    *,
    cache_dir: Path | None = None,
) -> list[str]:
    """Build the coverage run command for a pytest invocation."""
    if not sources:
        raise ValueError("at least one source is required for coverage measurement")
    cmd = [
        sys.executable,
        "-m",
        "coverage",
        "run",
        f"--data-file={data_file}",
        f"--source={','.join(coverage_sources(sources))}",
        "-m",
        "pytest",
    ]
    if cache_dir is not None:
        cmd += ["-o", f"cache_dir={cache_dir}"]
    return cmd + list(pytest_args)


def coverage_json_cmd(data_file: Path, json_file: Path) -> list[str]:
    """Build the command that converts coverage data into JSON."""
    return [
        sys.executable,
        "-m",
        "coverage",
        "json",
        f"--data-file={data_file}",
        "-o",
        str(json_file),
        "--fail-under=0",
        "-q",
    ]


def command_report(name: str, argv: Sequence[str], cwd: Path, timeout: int) -> dict[str, Any]:
    """Run a command and return a JSON-safe bounded report."""
    started = time.monotonic()
    try:
        proc = subprocess.run(
            list(argv),
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return {
            "name": name,
            "argv": list(argv),
            "ok": proc.returncode == 0,
            "exit_code": proc.returncode,
            "duration_sec": round(time.monotonic() - started, 3),
            "stdout_tail": tail(proc.stdout, TAIL_STDOUT),
            "stderr_tail": tail(proc.stderr, TAIL_STDERR),
        }
    except subprocess.TimeoutExpired as exc:
        stderr_tail = tail(as_text(exc.stderr), TAIL_STDERR)
        if stderr_tail:
            stderr_tail = f"{stderr_tail}\n[timed out after {timeout}s]"
        else:
            stderr_tail = f"timed out after {timeout}s"
        return {
            "name": name,
            "argv": list(argv),
            "ok": False,
            "exit_code": 124,
            "duration_sec": round(time.monotonic() - started, 3),
            "stdout_tail": tail(as_text(exc.stdout), TAIL_STDOUT),
            "stderr_tail": stderr_tail,
        }
    except Exception as exc:
        return {
            "name": name,
            "argv": list(argv),
            "ok": False,
            "exit_code": None,
            "duration_sec": round(time.monotonic() - started, 3),
            "stdout_tail": "",
            "stderr_tail": str(exc),
        }


def coverage_totals(report: dict[str, Any]) -> dict[str, Any]:
    """Extract covered-line totals from coverage.py JSON.

    coverage.py normally writes top-level `totals`. Some older or reduced JSON
    reports may only have per-file summaries, so this falls back to summing
    `files[*].summary`.
    """
    totals = report.get("totals")
    if isinstance(totals, dict):
        return {
            "covered_lines": int(totals.get("covered_lines", 0)),
            "num_statements": int(totals.get("num_statements", 0)),
            "percent_covered": float(totals.get("percent_covered", 0.0)),
        }

    covered = 0
    statements = 0
    for file_report in (report.get("files") or {}).values():
        summary = file_report.get("summary") or {}
        covered += int(summary.get("covered_lines", 0))
        statements += int(summary.get("num_statements", 0))
    percent = (covered / statements * 100.0) if statements else 0.0
    return {
        "covered_lines": covered,
        "num_statements": statements,
        "percent_covered": percent,
    }


def measured_files(report: dict[str, Any]) -> list[str]:
    """The files coverage actually measured, as posix paths. Empty means it measured nothing."""
    return sorted(str(name).replace("\\", "/").lstrip("./") for name in (report.get("files") or {}))


def _source_matches_file(source: str, file_path: str) -> bool:
    """Does one measured file belong to this requested `--source`?

    Deliberately LENIENT: a false "unmeasured" would relabel a genuine measured-zero as misuse,
    which is the very confusion this detection exists to remove, only pointing the other way. Both
    the path form (`scripts`, `src/pkg/mod.py`) and the dotted form (`src.pkg.mod`) are accepted,
    because `coverage_sources` normalises one into the other and the caller may pass either.
    """
    src = source.replace("\\", "/").lstrip("./").rstrip("/")
    if not src:
        return False
    candidates = {src, src.replace(".", "/")}
    if src.endswith(".py"):
        candidates.add(src[: -len(".py")].replace(".", "/") + ".py")
    haystack = "/" + file_path
    for cand in candidates:
        stem = cand[: -len(".py")] if cand.endswith(".py") else cand
        if file_path == cand or haystack.endswith("/" + cand):
            return True
        if file_path.startswith(stem + "/") or (("/" + stem + "/") in haystack):
            return True
        if haystack.endswith("/" + stem + ".py"):
            return True
    return False


def unmeasured_sources(sources: Sequence[str], files: Sequence[str]) -> list[str]:
    """Requested `--source` values that no measured file belongs to.

    THE EXACT FORM OF "COULD NOT MEASURE". It catches all three ways this gate has been silently
    aimed at nothing: a file path normalised into an unimportable dotted name; a repo whose own
    `[tool.coverage.run] source` or `addopts = --cov=...` wins and measures a different tree; and a
    `--no-cov` that disables the measurement the gate was asked to make. Every one of them used to
    surface as `coverage_delta 0`.
    """
    return sorted(
        source for source in sources if not any(_source_matches_file(str(source), f) for f in files)
    )


_NEVER_IMPORTED = re.compile(r"Module ([\w./-]+) was never imported")


def never_imported_modules(*outputs: str | None) -> list[str]:
    """Modules coverage.py itself reported as never imported. Names the source, when the tail has it.

    Best effort by construction — the console tails are bounded — so it NAMES a cause and is never
    the sole evidence: `unmeasured_sources` is the exact check.
    """
    found: set[str] = set()
    for text in outputs:
        for match in _NEVER_IMPORTED.finditer(text or ""):
            found.add(match.group(1))
    return sorted(found)


def read_coverage_json(path: Path) -> tuple[dict[str, Any] | None, list[str], str | None]:
    """Read a coverage JSON report and return (totals, measured files, error).

    The file list is returned BESIDE the totals rather than inside them: `coverage` travels into the
    result as the covered-lines record every existing consumer reads, and the two fallback literals
    that stand in for it when there is no report would have had to grow a matching key.
    """
    try:
        report = json.loads(path.read_text())
    except Exception as exc:
        return None, [], str(exc)
    return coverage_totals(report), measured_files(report), None


def coverage_check(
    name: str,
    repo: Path,
    sources: Sequence[str],
    pytest_args: Sequence[str],
    temp_dir: Path,
    timeout: int,
) -> dict[str, Any]:
    """Run pytest under coverage and parse the coverage JSON result."""
    data_file = temp_dir / f"{name}.coverage"
    json_file = temp_dir / f"{name}.json"
    run = command_report(
        f"{name}_coverage_run",
        coverage_run_cmd(
            sources, data_file, pytest_args, cache_dir=temp_dir / f"{name}-pytest-cache"
        ),
        repo,
        timeout,
    )
    json_run = None
    totals = None
    files: list[str] = []
    error = None
    if run["ok"]:
        json_run = command_report(
            f"{name}_coverage_json",
            coverage_json_cmd(data_file, json_file),
            repo,
            timeout,
        )
        if json_run["ok"]:
            totals, files, error = read_coverage_json(json_file)
        else:
            output = "\n".join(
                part for part in (json_run.get("stdout_tail"), json_run.get("stderr_tail")) if part
            )
            if run["ok"] and "No data to report" in output:
                totals = {"covered_lines": 0, "num_statements": 0, "percent_covered": 0.0}
                error = None
            else:
                error = json_run["stderr_tail"] or json_run["stdout_tail"] or "coverage json failed"
    else:
        error = run["stderr_tail"] or "coverage run failed"
    return {
        "ok": bool(run["ok"] and totals is not None and error is None),
        "run": run,
        "json": json_run,
        "coverage": totals or {"covered_lines": 0, "num_statements": 0, "percent_covered": 0.0},
        # WHY A FAILING RUN FAILED, in pytest's own vocabulary. `ok: False` alone cannot tell a real
        # regression from arguments pytest rejected or matched nothing with, and those demand
        # opposite responses: fix the tests, versus fix the command.
        "exit": pytest_exit_meaning(run.get("exit_code")),
        "measured_files": files,
        "never_imported": never_imported_modules(run.get("stderr_tail"), run.get("stdout_tail")),
        "error": error,
    }


def reliability_check(
    repo: Path, pytest_args: Sequence[str], runs: int, timeout: int, temp_dir: Path
) -> dict[str, Any]:
    """Run candidate tests repeatedly and report pass/fail per run."""
    reports = []
    for idx in range(runs):
        reports.append(
            command_report(
                f"candidate_reliability_{idx + 1}",
                pytest_cmd(pytest_args, cache_dir=temp_dir / f"reliability-pytest-cache-{idx + 1}"),
                repo,
                timeout,
            )
        )
    return {
        "runs_requested": runs,
        "pytest_args": list(pytest_args),
        "all_passed": all(r["ok"] for r in reports),
        "runs": reports,
    }


def coverage_measurement(
    sources: Sequence[str], baseline: dict[str, Any], candidate: dict[str, Any]
) -> dict[str, Any]:
    """Was the requested coverage measured AT ALL, and if not, what stopped it.

    Answered only from runs that completed: a run pytest rejected or collected nothing from measures
    nothing either, and its own check already names that cause — blaming `--source` there would be a
    second wrong answer. `unmeasured` is the INTERSECTION over both runs, because a source one run
    legitimately never touches is not a misuse.
    """
    ran = [side for side in (baseline, candidate) if side.get("ok")]
    if len(ran) < 2:
        return {
            "measured": None,
            "reasons": [],
            "unmeasured_sources": [],
            "never_imported": [],
            "statements": None,
            "unevaluated_because": "a coverage run did not complete, so its own check names the cause",
        }
    statements = max(int(side["coverage"].get("num_statements", 0)) for side in ran)
    unmeasured = sorted(
        set(unmeasured_sources(sources, baseline.get("measured_files") or []))
        & set(unmeasured_sources(sources, candidate.get("measured_files") or []))
    )
    never = sorted(
        set(baseline.get("never_imported") or []) | set(candidate.get("never_imported") or [])
    )
    reasons: list[str] = []
    if statements == 0:
        reasons.append(
            "coverage measured 0 statements on both runs, so the delta is not a fact about the "
            "tests"
        )
    if unmeasured:
        reasons.append(
            "no measured file belongs to --source " + ", ".join(repr(s) for s in unmeasured)
        )
    if never:
        reasons.append("coverage.py reported never imported: " + ", ".join(never))
    if reasons:
        reasons.append(
            "--source takes an importable MODULE or package path resolved from --repo; a file path "
            "is normalised to a dotted name, so 'src/pkg/mod.py' becomes 'src.pkg.mod' and imports "
            "only if 'src' is itself a package. A repo that pins [tool.coverage.run] source or "
            "addopts = --cov=... overrides the request and measures its own tree instead: pass "
            "-c <minimal ini> in both baseline and candidate args"
        )
    return {
        "measured": not reasons,
        "reasons": reasons,
        "unmeasured_sources": unmeasured,
        "never_imported": never,
        "statements": statements,
        "unevaluated_because": "",
    }


def verdict_checks(
    collect: dict[str, Any],
    baseline: dict[str, Any],
    candidate: dict[str, Any],
    reliability: dict[str, Any],
    covered_delta: int,
    min_covered_lines_delta: int,
    measurement: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Pure final verdict checks.

    A FAILED CHECK NOW SAYS WHICH KIND OF FAILURE IT IS. `ok` keeps its exact meaning for every
    existing consumer — an unmeasurable gate certifies nothing, so it still fails — but a check that
    failed because the INVOCATION was wrong carries `could_not_measure: True` and says so in
    `detail`, instead of asserting a defect in the tests that nobody has evidence for.
    """
    measurement = measurement or {}

    def _run_check(name: str, side: dict[str, Any], detail: str) -> dict[str, Any]:
        ok = bool(side.get("ok"))
        exit_info = side.get("exit") or pytest_exit_meaning(
            (side.get("run") or side).get("exit_code")
        )
        blind = not ok and not exit_info.get("measured", True)
        if blind:
            remedy = exit_info.get("remedy") or ""
            detail = (
                f"COULD NOT MEASURE — exit {exit_info.get('exit_code')}: {exit_info.get('meaning')}. "
                f"This is a misuse of the gate, NOT a verdict on the tests"
                + (f". {remedy}" if remedy else "")
            )
        return {"name": name, "ok": ok, "detail": detail, "could_not_measure": blind}

    delta_ok = covered_delta >= min_covered_lines_delta
    delta_blind = measurement.get("measured") is False
    delta_detail = f"covered-lines delta {covered_delta} >= required {min_covered_lines_delta}"
    if delta_blind:
        delta_detail = "COULD NOT MEASURE COVERAGE — " + "; ".join(measurement.get("reasons") or [])
    return [
        _run_check(
            "collect_import_ok",
            collect,
            "candidate tests collect and import under pytest --collect-only",
        ),
        _run_check(
            "baseline_non_regression",
            baseline,
            "baseline pytest command passes at least once under coverage",
        ),
        _run_check(
            "candidate_coverage_run",
            candidate,
            "candidate pytest command passes once under coverage",
        ),
        {
            "name": "candidate_reliability",
            "ok": bool(reliability.get("all_passed")),
            "detail": f"candidate pytest command passes {reliability.get('runs_requested', 0)} repeated runs",
            "could_not_measure": False,
        },
        {
            # A BLIND MEASUREMENT NEVER PASSES. `delta_ok and not delta_blind` matters when
            # `min_covered_lines_delta` is 0 or negative: 0 >= 0 is True, and a gate that measured
            # nothing must not certify a threshold it never observed.
            "name": "coverage_delta",
            "ok": bool(delta_ok and not delta_blind),
            "detail": delta_detail,
            "could_not_measure": bool(delta_blind),
        },
    ]


def run_gate(
    repo: str | Path,
    sources: Sequence[str],
    baseline_pytest_args: Sequence[str],
    candidate_pytest_args: Sequence[str],
    *,
    reliability_pytest_args: Sequence[str] | None = None,
    runs: int = 5,
    min_covered_lines_delta: int = 1,
    timeout: int = 120,
) -> dict[str, Any]:
    """Run the assured-acceptance gate and return a JSON-safe result."""
    repo_path = Path(repo).expanduser().resolve()
    if runs < 1:
        return {"ok": False, "error": "--runs must be >= 1"}
    if not repo_path.exists() or not repo_path.is_dir():
        return {"ok": False, "error": f"repo not found: {repo_path}"}
    if not sources:
        return {"ok": False, "error": "at least one --source is required"}
    reliability_args = list(
        reliability_pytest_args if reliability_pytest_args is not None else candidate_pytest_args
    )

    with tempfile.TemporaryDirectory(prefix="orch-testgen-gate-") as td:
        temp_dir = Path(td)
        collect = command_report(
            "candidate_collect",
            pytest_cmd(
                candidate_pytest_args,
                collect_only=True,
                cache_dir=temp_dir / "collect-pytest-cache",
            ),
            repo_path,
            timeout,
        )
        baseline = coverage_check(
            "baseline", repo_path, sources, baseline_pytest_args, temp_dir, timeout
        )
        candidate = coverage_check(
            "candidate", repo_path, sources, candidate_pytest_args, temp_dir, timeout
        )
        reliability = reliability_check(repo_path, reliability_args, runs, timeout, temp_dir)

    baseline_covered = int(baseline["coverage"]["covered_lines"])
    candidate_covered = int(candidate["coverage"]["covered_lines"])
    covered_delta = candidate_covered - baseline_covered
    measurement = coverage_measurement(sources, baseline, candidate)
    checks = verdict_checks(
        collect,
        baseline,
        candidate,
        reliability,
        covered_delta,
        min_covered_lines_delta,
        measurement,
    )
    ok = all(c["ok"] for c in checks)
    failed = [c["name"] for c in checks if not c["ok"]]
    # THE HEADLINE NAMES THE KIND. "failed checks: baseline_non_regression" sent one run looking for
    # a regression in tests that were fine; the cause was a shredded `-k` expression.
    blind = [c["name"] for c in checks if c.get("could_not_measure")]
    return {
        "ok": ok,
        "checks": checks,
        "coverage_measurement": measurement,
        "could_not_measure": blind,
        "baseline": {
            "pytest_args": list(baseline_pytest_args),
            "coverage": baseline["coverage"],
            "run": baseline["run"],
            "json": baseline["json"],
            "error": baseline["error"],
        },
        "candidate": {
            "pytest_args": list(candidate_pytest_args),
            "collect": collect,
            "coverage": candidate["coverage"],
            "run": candidate["run"],
            "json": candidate["json"],
            "error": candidate["error"],
        },
        "coverage_delta": {
            "covered_lines": covered_delta,
            "min_required": min_covered_lines_delta,
            "baseline_covered_lines": baseline_covered,
            "candidate_covered_lines": candidate_covered,
        },
        "reliability": reliability,
        "error": (
            None
            if ok
            else (
                "failed checks: "
                + ", ".join(failed)
                + (
                    "; "
                    + ", ".join(blind)
                    + " COULD NOT MEASURE — this is a misuse of the gate, not a verdict on the "
                    "tests: " + "; ".join(c["detail"] for c in checks if c.get("could_not_measure"))
                    if blind
                    else ""
                )
            )
        ),
    }


def _selftest() -> None:
    assert split_args('tests -k "not slow"') == ["tests", "-k", "not slow"]
    assert pytest_cmd(["tests/a.py"], collect_only=True) == [
        sys.executable,
        "-m",
        "pytest",
        "--collect-only",
        "tests/a.py",
    ]
    cached = pytest_cmd(["tests/a.py"], cache_dir=Path("/tmp/pytest-cache"))
    assert "-o" in cached and "cache_dir=/tmp/pytest-cache" in cached, cached
    cov = coverage_run_cmd(["pkg", "lib"], Path("/tmp/.cov"), ["tests"])
    assert cov[:4] == [sys.executable, "-m", "coverage", "run"], cov
    assert "--source=pkg,lib" in cov and "-m" in cov and "pytest" in cov and "tests" in cov, cov
    file_source = coverage_run_cmd(["tools/resolve_mypy_pin.py"], Path("/tmp/.cov"), ["tests"])
    assert "--source=tools.resolve_mypy_pin" in file_source, file_source
    cached_cov = coverage_run_cmd(
        ["pkg"], Path("/tmp/.cov"), ["tests"], cache_dir=Path("/tmp/cov-cache")
    )
    assert "cache_dir=/tmp/cov-cache" in cached_cov, cached_cov
    cj = coverage_json_cmd(Path("/tmp/.cov"), Path("/tmp/cov.json"))
    assert cj[:4] == [sys.executable, "-m", "coverage", "json"] and "-o" in cj, cj
    assert "--fail-under=0" in cj, cj

    totals = coverage_totals(
        {"totals": {"covered_lines": 7, "num_statements": 10, "percent_covered": 70}}
    )
    assert totals == {"covered_lines": 7, "num_statements": 10, "percent_covered": 70.0}, totals
    fallback = coverage_totals(
        {
            "files": {
                "a.py": {"summary": {"covered_lines": 3, "num_statements": 4}},
                "b.py": {"summary": {"covered_lines": 2, "num_statements": 6}},
            }
        }
    )
    assert fallback["covered_lines"] == 5 and fallback["num_statements"] == 10, fallback

    collect = {"ok": True}
    baseline = {"ok": True}
    candidate = {"ok": True}
    reliable = {"all_passed": True, "runs_requested": 5}
    checks = verdict_checks(collect, baseline, candidate, reliable, 2, 1)
    assert all(c["ok"] for c in checks), checks
    checks = verdict_checks(collect, baseline, candidate, reliable, 0, 1)
    assert not [c for c in checks if c["name"] == "coverage_delta"][0]["ok"], checks
    checks = verdict_checks({"ok": False}, baseline, candidate, reliable, 2, 1)
    assert not checks[0]["ok"], checks

    missing = run_gate("/path/that/does/not/exist", ["pkg"], [], [], runs=1)
    assert missing["ok"] is False and "repo not found" in missing["error"], missing

    # ---- MISUSE IS NOT A BAD-TEST VERDICT (2026-08-25). Asserted on the CHECK LIST the caller
    # receives as `result["checks"]`, never on the classification tables themselves — a test that
    # reads back its own table passes with the table restored.
    #
    # 1. A SHREDDED PYTEST ARGUMENT. pytest exit 5 = nothing was collected, so nothing ran; the old
    #    verdict was `baseline_non_regression: False`, which accuses the pre-existing tests.
    def _side(exit_code: int, *, files=(), statements=10, covered=5):
        run = {"exit_code": exit_code, "ok": exit_code == 0, "stderr_tail": "", "stdout_tail": ""}
        return {
            "ok": exit_code == 0,
            "run": run,
            "exit": pytest_exit_meaning(exit_code),
            "coverage": {
                "covered_lines": covered,
                "num_statements": statements,
                "percent_covered": 50.0,
            },
            "measured_files": list(files),
            "never_imported": [],
        }

    shredded = verdict_checks(
        {"ok": True, "exit_code": 0}, _side(5), _side(0, files=["pkg/a.py"]), reliable, 2, 1
    )
    base_check = next(c for c in shredded if c["name"] == "baseline_non_regression")
    assert base_check["ok"] is False, base_check
    assert base_check["could_not_measure"] is True, base_check
    assert "COULD NOT MEASURE" in base_check["detail"], base_check
    assert "not a verdict on the tests" in base_check["detail"].lower(), base_check
    # THE REMEDY TRAVELS WITH THE DIAGNOSIS, in the same string a reader is already looking at.
    assert "-k" in base_check["detail"] and "quote" in base_check["detail"], base_check
    # ...and a REAL regression is still a real regression. Exit 1 means tests ran and failed.
    genuine = verdict_checks(
        {"ok": True, "exit_code": 0}, _side(1), _side(0, files=["pkg/a.py"]), reliable, 2, 1
    )
    genuine_base = next(c for c in genuine if c["name"] == "baseline_non_regression")
    assert genuine_base["ok"] is False and genuine_base["could_not_measure"] is False, genuine_base
    assert "COULD NOT MEASURE" not in genuine_base["detail"], genuine_base

    # 2. A `--source` THAT MEASURED NOTHING. All three live shapes reduce to the same exact fact:
    #    no measured file belongs to the requested source.
    assert unmeasured_sources(["src/pkg/mod.py"], []) == ["src/pkg/mod.py"]
    assert unmeasured_sources(["src/pkg/mod.py"], ["src/pkg/mod.py"]) == []
    assert unmeasured_sources(["src.pkg.mod"], ["src/pkg/mod.py"]) == []
    # The repo-pins-its-own-source shape: `scripts` requested, `src` measured, 11398 statements.
    assert unmeasured_sources(["scripts"], ["src/a.py", "src/b.py"]) == ["scripts"]
    assert unmeasured_sources(["scripts"], ["scripts/build.py"]) == []
    # A source ONE side legitimately never touches is not a misuse: the verdict is the intersection.
    half = coverage_measurement(
        ["pkg"], _side(0, files=[]), _side(0, files=["pkg/a.py"], statements=12, covered=9)
    )
    assert half["measured"] is True, half

    blind_measurement = coverage_measurement(
        ["scripts"],
        _side(0, files=["src/a.py"], statements=11398, covered=0),
        _side(0, files=["src/a.py"], statements=11398, covered=0),
    )
    assert blind_measurement["measured"] is False, blind_measurement
    assert blind_measurement["unmeasured_sources"] == ["scripts"], blind_measurement
    assert any("--source" in r for r in blind_measurement["reasons"]), blind_measurement
    blind_checks = verdict_checks(
        {"ok": True, "exit_code": 0},
        _side(0, files=["src/a.py"]),
        _side(0, files=["src/a.py"]),
        reliable,
        0,
        0,
        blind_measurement,
    )
    delta_check = next(c for c in blind_checks if c["name"] == "coverage_delta")
    # A BLIND MEASUREMENT NEVER PASSES, and `0 >= 0` is exactly the case that would have let it.
    assert delta_check["ok"] is False, delta_check
    assert delta_check["could_not_measure"] is True, delta_check
    assert "COULD NOT MEASURE COVERAGE" in delta_check["detail"], delta_check
    # ...while a genuinely measured zero still reads as a measured zero, not as misuse.
    measured_zero = coverage_measurement(
        ["pkg"],
        _side(0, files=["pkg/a.py"], statements=10, covered=5),
        _side(0, files=["pkg/a.py"], statements=10, covered=5),
    )
    assert measured_zero["measured"] is True, measured_zero
    zero_checks = verdict_checks(
        {"ok": True, "exit_code": 0},
        _side(0, files=["pkg/a.py"]),
        _side(0, files=["pkg/a.py"]),
        reliable,
        0,
        1,
        measured_zero,
    )
    zero_delta = next(c for c in zero_checks if c["name"] == "coverage_delta")
    assert zero_delta["ok"] is False and zero_delta["could_not_measure"] is False, zero_delta
    assert "covered-lines delta 0" in zero_delta["detail"], zero_delta
    # A run that never completed leaves the coverage question UNEVALUATED, with the reason named --
    # blaming `--source` for a command pytest rejected would be a second wrong answer.
    unevaluated = coverage_measurement(["pkg"], _side(5), _side(0, files=["pkg/a.py"]))
    assert unevaluated["measured"] is None and unevaluated["unevaluated_because"], unevaluated

    # 3. COVERAGE.PY'S OWN WARNING NAMES THE SOURCE, when the bounded tail carries it.
    assert never_imported_modules(
        "CoverageWarning: Module src.pkg.mod was never imported. (module-not-imported)"
    ) == ["src.pkg.mod"]
    assert never_imported_modules(None, "") == []

    print(
        "testgen_gate.py selftest: OK (argv builders, coverage totals, verdict checks, live input "
        "guards, and misuse reported as misuse: a shredded pytest arg is not a baseline regression, "
        "an unmeasured --source is not a zero delta, and a measured zero still is)"
    )


def main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Assured-acceptance gate for generated pytest tests."
    )
    parser.add_argument("--selftest", action="store_true")
    parser.add_argument("--repo", default=".", help="repository root to run pytest in")
    parser.add_argument(
        "--source",
        action="append",
        default=[],
        help="coverage source: an IMPORTABLE module or package, resolved from --repo; repeatable. "
        "A file path is normalised to a dotted name ('src/pkg/mod.py' -> 'src.pkg.mod'), which "
        "imports only if 'src' is itself a package. When nothing the run measures belongs to it, "
        "the gate now says COULD NOT MEASURE instead of reporting a zero coverage delta",
    )
    parser.add_argument(
        "--baseline-pytest-args",
        help="pytest args excluding generated tests. Split shell-style, so an option value "
        "containing spaces must be quoted INSIDE the string (\"tests -k 'not slow'\"); an unquoted "
        "-k expression is shredded into separate tokens and selects nothing",
    )
    parser.add_argument(
        "--candidate-pytest-args",
        help="pytest args including generated tests; same shell-style quoting rule as "
        "--baseline-pytest-args",
    )
    parser.add_argument(
        "--reliability-pytest-args",
        help="optional narrower pytest args for repeated flakiness runs",
    )
    parser.add_argument("--runs", type=int, default=5, help="candidate reliability runs")
    parser.add_argument("--min-covered-lines-delta", type=int, default=1)
    parser.add_argument("--timeout", type=int, default=120, help="per-command timeout in seconds")
    ns = parser.parse_args(list(argv))

    if ns.selftest:
        _selftest()
        return 0
    if not ns.source:
        parser.error("--source is required in live mode")
    if ns.baseline_pytest_args is None:
        parser.error("--baseline-pytest-args is required in live mode")
    if ns.candidate_pytest_args is None:
        parser.error("--candidate-pytest-args is required in live mode")

    result = run_gate(
        ns.repo,
        ns.source,
        split_args(ns.baseline_pytest_args),
        split_args(ns.candidate_pytest_args),
        reliability_pytest_args=(
            split_args(ns.reliability_pytest_args)
            if ns.reliability_pytest_args is not None
            else None
        ),
        runs=ns.runs,
        min_covered_lines_delta=ns.min_covered_lines_delta,
        timeout=ns.timeout,
    )
    print(json.dumps(result, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
