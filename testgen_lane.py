#!/usr/bin/env python3
"""testgen_lane.py - build gate-backed prompts for generated-test work.

BRIEF_expand_range.md option #2 already supplied `testgen_gate.py`. This helper
wires that gate into an actual orchestrator lane: the seat can generate one
prompt file for a delegated agent, and the prompt includes the exact acceptance
gate command that must pass before commit/PR.
"""
from __future__ import annotations

import argparse
import json
import shlex
import sys
from pathlib import Path
from typing import Sequence


ORCH_DIR = Path(__file__).resolve().parent
TESTGEN_GATE = ORCH_DIR / "testgen_gate.py"
READ_ONLY_GATE_GUARD = (
    "Do not edit `testgen_gate.py`, `testgen_lane.py`, or other Orchestrator "
    "gate/helper files. Treat them as acceptance infrastructure. If the gate "
    "appears wrong, stop and report the failing check instead of changing the gate."
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
    parts.extend([
        "--baseline-pytest-args", _quote(baseline_pytest_args),
        "--candidate-pytest-args", _quote(candidate_pytest_args),
    ])
    if reliability_pytest_args is not None:
        parts.extend(["--reliability-pytest-args", _quote(reliability_pytest_args)])
    parts.extend([
        "--runs", str(runs),
        "--min-covered-lines-delta", str(min_covered_lines_delta),
        "--timeout", str(timeout),
    ])
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
        if reliability_pytest_args is not None else
        "- Repeated reliability args: same as candidate pytest args"
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
    parser.add_argument("--source", action="append", default=[], help="coverage source path/package; repeatable")
    parser.add_argument("--baseline-pytest-args", help="pytest args excluding generated tests")
    parser.add_argument("--candidate-pytest-args", help="pytest args including generated tests")
    parser.add_argument("--reliability-pytest-args", help="optional narrower args for repeated flake runs")
    parser.add_argument("--runs", type=_positive_int, default=5)
    parser.add_argument("--min-covered-lines-delta", type=_non_negative_int, default=1)
    parser.add_argument("--timeout", type=_positive_int, default=120)
    parser.add_argument("--target", default="", help="issue/PR target to include in the prompt")
    parser.add_argument("--context", default="", help="short inline task context")
    parser.add_argument("--context-file", help="file containing additional task context")
    parser.add_argument("--json", action="store_true", help="print JSON with prompt and gate command")
    args = parser.parse_args(list(argv))

    if args.selftest:
        _selftest()
        return 0
    if not args.source:
        parser.error("--source is required")
    if args.baseline_pytest_args is None:
        parser.error("--baseline-pytest-args is required")
    if args.candidate_pytest_args is None:
        parser.error("--candidate-pytest-args is required")

    context = args.context
    if args.context_file:
        context = (context + "\n" + Path(args.context_file).read_text()).strip()
    prompt = build_prompt(
        repo=args.repo,
        sources=args.source,
        baseline_pytest_args=args.baseline_pytest_args,
        candidate_pytest_args=args.candidate_pytest_args,
        reliability_pytest_args=args.reliability_pytest_args,
        runs=args.runs,
        min_covered_lines_delta=args.min_covered_lines_delta,
        timeout=args.timeout,
        target=args.target,
        context=context,
    )
    if args.json:
        print(json.dumps({
            "prompt": prompt,
            "gate_command": gate_command(
                args.repo,
                args.source,
                args.baseline_pytest_args,
                args.candidate_pytest_args,
                reliability_pytest_args=args.reliability_pytest_args,
                runs=args.runs,
                min_covered_lines_delta=args.min_covered_lines_delta,
                timeout=args.timeout,
            ),
        }, indent=2))
    else:
        print(prompt)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
