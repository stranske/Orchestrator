#!/usr/bin/env python3
"""testgen_gate.py - assured-acceptance gate for generated tests.

BRIEF_expand_range.md option #2. This widens the fleet from "implement the
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


def read_coverage_json(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    """Read a coverage JSON report and return (totals, error)."""
    try:
        return coverage_totals(json.loads(path.read_text())), None
    except Exception as exc:
        return None, str(exc)


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
    error = None
    if run["ok"]:
        json_run = command_report(
            f"{name}_coverage_json",
            coverage_json_cmd(data_file, json_file),
            repo,
            timeout,
        )
        if json_run["ok"]:
            totals, error = read_coverage_json(json_file)
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


def verdict_checks(
    collect: dict[str, Any],
    baseline: dict[str, Any],
    candidate: dict[str, Any],
    reliability: dict[str, Any],
    covered_delta: int,
    min_covered_lines_delta: int,
) -> list[dict[str, Any]]:
    """Pure final verdict checks."""
    return [
        {
            "name": "collect_import_ok",
            "ok": bool(collect.get("ok")),
            "detail": "candidate tests collect and import under pytest --collect-only",
        },
        {
            "name": "baseline_non_regression",
            "ok": bool(baseline.get("ok")),
            "detail": "baseline pytest command passes at least once under coverage",
        },
        {
            "name": "candidate_coverage_run",
            "ok": bool(candidate.get("ok")),
            "detail": "candidate pytest command passes once under coverage",
        },
        {
            "name": "candidate_reliability",
            "ok": bool(reliability.get("all_passed")),
            "detail": f"candidate pytest command passes {reliability.get('runs_requested', 0)} repeated runs",
        },
        {
            "name": "coverage_delta",
            "ok": covered_delta >= min_covered_lines_delta,
            "detail": f"covered-lines delta {covered_delta} >= required {min_covered_lines_delta}",
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
    checks = verdict_checks(
        collect,
        baseline,
        candidate,
        reliability,
        covered_delta,
        min_covered_lines_delta,
    )
    ok = all(c["ok"] for c in checks)
    failed = [c["name"] for c in checks if not c["ok"]]
    return {
        "ok": ok,
        "checks": checks,
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
        "error": None if ok else "failed checks: " + ", ".join(failed),
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
    print(
        "testgen_gate.py selftest: OK (argv builders, coverage totals, verdict checks, live input guards)"
    )


def main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Assured-acceptance gate for generated pytest tests."
    )
    parser.add_argument("--selftest", action="store_true")
    parser.add_argument("--repo", default=".", help="repository root to run pytest in")
    parser.add_argument(
        "--source", action="append", default=[], help="coverage source path/package; repeatable"
    )
    parser.add_argument("--baseline-pytest-args", help="pytest args excluding generated tests")
    parser.add_argument("--candidate-pytest-args", help="pytest args including generated tests")
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
