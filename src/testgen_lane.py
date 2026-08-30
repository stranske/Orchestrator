#!/usr/bin/env python3
"""testgen_lane.py - build gate-backed prompts for generated-test work.

docs/briefs/BRIEF_expand_range.md option #2 already supplied `testgen_gate.py`. This helper
wires that gate into an actual orchestrator lane: the seat can generate one
prompt file for a delegated agent, and the prompt includes the exact acceptance
gate command that must pass before commit/PR.
"""

from __future__ import annotations

import argparse
import json
import shlex
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

ORCH_DIR = Path(__file__).resolve().parent
TESTGEN_GATE = ORCH_DIR / "testgen_gate.py"
READ_ONLY_GATE_GUARD = (
    "Do not edit `testgen_gate.py`, `testgen_lane.py`, or other Orchestrator "
    "gate/helper files. Treat them as acceptance infrastructure. If the gate "
    "appears wrong, stop and report the failing check instead of changing the gate."
)


RANKING_SWITCH = "ORCH_ESCAPED_DEFECT_PRIORITY"


def _importable_source(repo: Path, path: str) -> str:
    """Turn a ranked FILE path into a coverage `--source` value that measures something.

    `coverage run --source=src/mod.py` measures NOTHING and exits 0 — verified, and already
    documented at the top of `testgen_gate.py` as one of two directions this goes wrong. A ranker
    that emitted file paths would therefore hand the gate a source with no measured files, so the
    conversion happens here rather than being left to the caller.

    The leading component is dropped when it is a source ROOT rather than a package, detected by
    the absence of `__init__.py` — which is why `src/testgen_lane.py` in this repo is importable as
    `testgen_lane` and not as `src.testgen_lane`. A wrong guess is not silent: `testgen_gate`'s own
    `unmeasured_sources` fails the gate on a source no measured file belongs to.
    """
    parts = Path(path).with_suffix("").as_posix().split("/")
    while len(parts) > 1 and not (repo / parts[0] / "__init__.py").exists():
        parts.pop(0)
    return ".".join(parts)


def ranked_sources(
    *,
    repo: str | Path,
    limit: int,
    coverage_json_path: str | None = None,
    lookback_days: int = 180,
    explicit_sources: Sequence[str] = (),
) -> tuple[list[str], list[dict[str, Any]], str]:
    """Choose sources by measured priority, and NEVER by silently choosing none.

    Returns `(sources, ranking, note)`. The note is always non-empty and always printed: a lane
    that quietly fell back to hand-named sources would look identical to one that ranked them, and
    the whole value of the ordering is knowing which one you got.

    Every failure path FAILS TOWARD MOTION. If the switch is off, the ranker will not import, the
    repo is unreadable or nothing scores, sources the caller named by hand are used instead and the
    reason is stated. Only when there is nothing to fall back on does this return no sources — and
    then the note names what was missing, so the caller's exit is a diagnosis rather than a shrug.
    """
    repo_path = Path(repo).expanduser().resolve()

    def _fallback(reason: str) -> tuple[list[str], list[dict[str, Any]], str]:
        if explicit_sources:
            return (
                list(explicit_sources),
                [],
                f"testgen_lane: ranking not applied ({reason}); "
                f"using the {len(explicit_sources)} source(s) named on the command line",
            )
        return [], [], f"testgen_lane: no sources — ranking not applied ({reason}), and none named"

    import os

    if os.environ.get(RANKING_SWITCH, "") != "1":
        return _fallback(f"{RANKING_SWITCH} is not set to 1")

    try:
        import escaped_defect_priority
    except Exception as exc:  # pragma: no cover - import failure is environment-specific
        return _fallback(f"escaped_defect_priority did not import: {exc}")

    coverage_json: dict[str, Any] = {}
    coverage_note = "no coverage report supplied, so tier 3 is unread"
    if coverage_json_path:
        report = Path(coverage_json_path).expanduser()
        if not report.exists():
            coverage_note = f"coverage report {report} does not exist, so tier 3 is UNREAD"
        else:
            try:
                coverage_json = json.loads(report.read_text())
                coverage_note = f"tier 3 read from {report}"
            except Exception as exc:
                coverage_note = (
                    f"coverage report {report} is unreadable ({exc}), so tier 3 is UNREAD"
                )

    ranking, status = escaped_defect_priority.rank_status(
        repo_path, coverage_json, lookback_days=lookback_days, limit=limit
    )
    if status["status"] != "ok":
        return _fallback(f"{status['status']}: {status['reason']}")

    seen: dict[str, None] = {}
    for row in ranking:
        seen.setdefault(_importable_source(repo_path, str(row["path"])), None)
    sources = list(seen)[:limit]
    if not sources:
        return _fallback("ranking produced rows but no importable source names")
    return (
        sources,
        ranking[:limit],
        f"testgen_lane: ranked {len(ranking)} candidate(s), taking {len(sources)} "
        f"({coverage_note})",
    )


def _quote(value: str | Path) -> str:
    return shlex.quote(str(value))


def gate_command(
    repo: str | Path,
    sources: Sequence[str],
    baseline_pytest_args: str,
    candidate_pytest_args: str,
    *,
    reliability_pytest_args: str | None = None,
    runs: int = 5,
    min_covered_lines_delta: int = 1,
    timeout: int = 120,
    base_ref: str | None = None,
    test_path: str | None = None,
) -> str:
    """Return the exact shell command an agent should pass before commit/PR."""
    if not sources:
        raise ValueError("at least one source is required")
    if runs < 1:
        raise ValueError("runs must be >= 1")
    if min_covered_lines_delta < 0:
        raise ValueError("min_covered_lines_delta must be >= 0")
    if timeout < 1:
        raise ValueError("timeout must be >= 1")

    parts = ["python3", _quote(TESTGEN_GATE), "--repo", _quote(repo)]
    for source in sources:
        parts.extend(["--source", _quote(source)])
    parts.extend(
        [
            "--baseline-pytest-args",
            _quote(baseline_pytest_args),
            "--candidate-pytest-args",
            _quote(candidate_pytest_args),
        ]
    )
    if reliability_pytest_args is not None:
        parts.extend(["--reliability-pytest-args", _quote(reliability_pytest_args)])
    parts.extend(
        [
            "--runs",
            str(runs),
            "--min-covered-lines-delta",
            str(min_covered_lines_delta),
            "--timeout",
            str(timeout),
        ]
    )
    # WITHOUT --base-ref THE GATE'S STRONGEST CHECK CANNOT RUN. no_hollow_nodes needs a ref
    # holding the code before the change, so local_verify.py can revert to it and see which
    # candidate nodes still pass -- those are hollow. The gate reports COULD NOT MEASURE and
    # FAILS when it is absent, deliberately, so omitting it cannot quietly buy a green run.
    if base_ref:
        parts.extend(["--base-ref", _quote(base_ref)])
    if test_path:
        parts.extend(["--test-path", _quote(test_path)])
    return " ".join(parts)


def build_prompt(
    *,
    repo: str | Path,
    sources: Sequence[str],
    baseline_pytest_args: str,
    candidate_pytest_args: str,
    reliability_pytest_args: str | None = None,
    runs: int = 5,
    min_covered_lines_delta: int = 1,
    timeout: int = 120,
    target: str = "",
    context: str = "",
    ranking: Sequence[dict[str, Any]] | None = None,
) -> str:
    """Build the reusable prompt handed to a local or remote test-generation agent."""
    cmd = gate_command(
        repo,
        sources,
        baseline_pytest_args,
        candidate_pytest_args,
        reliability_pytest_args=reliability_pytest_args,
        runs=runs,
        min_covered_lines_delta=min_covered_lines_delta,
        timeout=timeout,
    )
    source_list = ", ".join(sources)
    reliability_line = (
        f"- Repeated reliability args: `{reliability_pytest_args}`"
        if reliability_pytest_args is not None
        else "- Repeated reliability args: same as candidate pytest args"
    )
    lines = [
        "You are in the Orchestrator test-generation lane.",
        "",
    ]
    if target:
        lines += [f"Target: {target}", ""]
    lines += [
        f"Goal: add meaningful pytest coverage for `{source_list}` without changing production behavior.",
        "Generate tests only unless a tiny production-code testability fix is unavoidable; if that happens,",
        "explain it clearly and keep it minimal.",
    ]
    if context.strip():
        lines += ["", "Context:", context.strip()]
    lines += [
        "",
        "Read-only guardrails:",
        f"- {READ_ONLY_GATE_GUARD}",
        "- Before committing, run `git diff --name-only` and confirm no Orchestrator gate/helper file changed.",
        "",
        "Acceptance gate:",
        "```bash",
        cmd,
        "```",
        "",
        "Gate contract:",
        f"- Baseline pytest args exclude the generated tests: `{baseline_pytest_args}`",
        f"- Candidate pytest args include the generated tests: `{candidate_pytest_args}`",
        reliability_line,
        f"- Minimum covered-line delta: `{min_covered_lines_delta}`",
        f"- Reliability runs: `{runs}`",
    ]
    if ranking:
        lines += ["", "Why these files, in this order:"]
        for position, row in enumerate(ranking, start=1):
            evidence = "; ".join(row.get("evidence") or []) or "no per-file evidence recorded"

            # `?` and never `0` for an absent key. The first draft defaulted these to 0 while
            # reading names the ranker does not emit, so every file's stated reason was "escaped 0,
            # churn 0, uncovered 0" — correct ordering under a rationale no input could falsify.
            # A rendered `?` is a visible defect; a rendered 0 is a lie that reads as good news.
            def _tier(key: str) -> str:
                return "?" if key not in row else str(row[key])

            lines.append(
                f"{position}. `{row.get('path', '?')}` — "
                f"escaped-defect weight {_tier('tier1_escaped_defects')}, "
                f"churn {_tier('tier2_churn')}, "
                f"uncovered {_tier('tier3_uncovered_statements')}, "
                f"hollow rate {_tier('hollow_rate')} ({evidence})"
            )
        lines += [
            "",
            "That order is a PRIORITY, not a mandate. It ranks by where testing has already been",
            "observed to fail, then by churn, then by uncovered mass — never by uncovered mass",
            "first, because that ordering points at the largest glue modules and is the one most",
            "likely to produce tests that pass against a broken base. If a file higher in the list",
            "genuinely cannot be tested meaningfully, SAY SO and take the next one; do not write a",
            "smoke test to clear it.",
        ]
    lines += [
        "",
        "Workflow:",
        "1. Inspect the target source and nearby tests.",
        "2. Add focused tests that assert real behavior, edge cases, or regression risks.",
        "3. Run the acceptance gate above.",
        "4. If the gate fails, iterate on the tests until it passes or stop with the failing check names.",
        "5. Commit/push/open a PR only after the gate passes. Include the gate command and result in the PR body.",
    ]
    return "\n".join(lines)


def _non_negative_int(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not an integer: {raw}") from exc
    if value < 0:
        raise argparse.ArgumentTypeError("must be >= 0")
    return value


def _positive_int(raw: str) -> int:
    value = _non_negative_int(raw)
    if value == 0:
        raise argparse.ArgumentTypeError("must be positive")
    return value


def _selftest() -> None:
    cmd = gate_command(
        "/tmp/repo with space",
        ["pkg", "lib/core.py"],
        "tests/unit -k 'not slow'",
        "tests/unit tests/generated/test_core.py",
        reliability_pytest_args="tests/generated/test_core.py",
        runs=3,
        min_covered_lines_delta=2,
        timeout=45,
    )
    assert "testgen_gate.py" in cmd, cmd
    # The lane never GUESSES a base ref: the gate fails closed without one, so an omitted ref
    # surfaces as a failed check rather than as a silently weaker gate.
    assert "--base-ref" not in cmd, cmd
    with_ref = gate_command(
        ".", ["pkg"], "tests", "tests", base_ref="abc123", test_path="tests/test_new.py"
    )
    assert "--base-ref abc123" in with_ref, with_ref
    assert "--test-path tests/test_new.py" in with_ref, with_ref
    assert "--source pkg --source lib/core.py" in cmd, cmd
    assert shlex.quote("/tmp/repo with space") in cmd, cmd
    assert "--reliability-pytest-args tests/generated/test_core.py" in cmd, cmd
    assert "--runs 3" in cmd and "--min-covered-lines-delta 2" in cmd, cmd

    prompt = build_prompt(
        repo="/tmp/repo",
        sources=["pkg"],
        baseline_pytest_args="tests/unit",
        candidate_pytest_args="tests/unit tests/generated",
        target="owner/repo#123",
        context="Cover parser fallback behavior.",
    )
    assert "Target: owner/repo#123" in prompt, prompt
    assert "Acceptance gate:" in prompt and "python3" in prompt, prompt
    assert "Baseline pytest args exclude" in prompt, prompt
    assert "same as candidate pytest args" in prompt, prompt
    assert "Do not edit `testgen_gate.py`" in prompt, prompt
    assert "git diff --name-only" in prompt, prompt
    assert "Commit/push/open a PR only after the gate passes" in prompt, prompt

    try:
        gate_command(".", [], "tests", "tests")
    except ValueError as exc:
        assert "source" in str(exc), exc
    else:
        raise AssertionError("empty sources should fail")

    print("testgen_lane.py selftest: OK (gate command quoting, prompt contract, input guards)")


def _capability_heartbeat(event_type: str = "invocation") -> None:
    """Record that this capability ran, at its own code path.

    Infrastructure and lane capabilities are not always ROUTED to — they are entered directly — so
    each records use where it actually executes. Lazy import (capabilities imports feedback, and
    several of these are imported BY capabilities' dependencies), never raises (recording use must
    not be able to prevent the work), and inert outside an active tick via
    ORCH_CAPABILITY_HEARTBEATS. (2026-08-09)
    """
    try:
        import capabilities

        capabilities.production_heartbeat("testgen-lane", event_type, ref="testgen_lane.main")
    except Exception:
        pass


def main(argv: Sequence[str]) -> int:
    _capability_heartbeat()
    parser = argparse.ArgumentParser(description="Build a gate-backed test-generation lane prompt.")
    parser.add_argument("--selftest", action="store_true")
    parser.add_argument("--repo", default=".", help="repository root the agent will work in")
    parser.add_argument(
        "--source", action="append", default=[], help="coverage source path/package; repeatable"
    )
    parser.add_argument("--baseline-pytest-args", help="pytest args excluding generated tests")
    parser.add_argument("--candidate-pytest-args", help="pytest args including generated tests")
    parser.add_argument(
        "--reliability-pytest-args", help="optional narrower args for repeated flake runs"
    )
    parser.add_argument("--runs", type=_positive_int, default=5)
    parser.add_argument("--min-covered-lines-delta", type=_non_negative_int, default=1)
    parser.add_argument("--timeout", type=_positive_int, default=120)
    parser.add_argument("--target", default="", help="issue/PR target to include in the prompt")
    parser.add_argument("--context", default="", help="short inline task context")
    parser.add_argument("--context-file", help="file containing additional task context")
    parser.add_argument(
        "--rank-sources",
        type=_positive_int,
        metavar="N",
        help=(
            "choose the N highest-priority sources with escaped_defect_priority instead of "
            "naming them by hand; requires " + RANKING_SWITCH + "=1"
        ),
    )
    parser.add_argument(
        "--coverage-json", help="coverage.py JSON report, feeding --rank-sources' third tier"
    )
    parser.add_argument(
        "--lookback-days",
        type=_positive_int,
        default=180,
        help="history window --rank-sources reads for fix commits and churn",
    )
    parser.add_argument(
        "--json", action="store_true", help="print JSON with prompt and gate command"
    )
    args = parser.parse_args(list(argv))

    if args.selftest:
        _selftest()
        return 0
    sources = list(args.source)
    ranking: list[dict[str, Any]] = []
    if args.rank_sources:
        sources, ranking, note = ranked_sources(
            repo=args.repo,
            limit=args.rank_sources,
            coverage_json_path=args.coverage_json,
            lookback_days=args.lookback_days,
            explicit_sources=list(args.source),
        )
        print(note, file=sys.stderr)
        if not sources:
            return 2
    if not sources:
        parser.error("--source is required (or --rank-sources N to choose them by priority)")
    if args.baseline_pytest_args is None:
        parser.error("--baseline-pytest-args is required")
    if args.candidate_pytest_args is None:
        parser.error("--candidate-pytest-args is required")

    context = args.context
    if args.context_file:
        context = (context + "\n" + Path(args.context_file).read_text()).strip()
    prompt = build_prompt(
        repo=args.repo,
        sources=sources,
        baseline_pytest_args=args.baseline_pytest_args,
        candidate_pytest_args=args.candidate_pytest_args,
        reliability_pytest_args=args.reliability_pytest_args,
        runs=args.runs,
        min_covered_lines_delta=args.min_covered_lines_delta,
        timeout=args.timeout,
        target=args.target,
        context=context,
        ranking=ranking,
    )
    if args.json:
        print(
            json.dumps(
                {
                    "prompt": prompt,
                    "gate_command": gate_command(
                        args.repo,
                        sources,
                        args.baseline_pytest_args,
                        args.candidate_pytest_args,
                        reliability_pytest_args=args.reliability_pytest_args,
                        runs=args.runs,
                        min_covered_lines_delta=args.min_covered_lines_delta,
                        timeout=args.timeout,
                    ),
                },
                indent=2,
            )
        )
    else:
        print(prompt)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
