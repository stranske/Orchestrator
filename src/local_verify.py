#!/usr/bin/env python3
"""local_verify.py - local deliberate-break verifier for delegated work.

The gate is intentionally simple:
1. Run the target test command in the live worktree. It must pass.
2. Build a temporary copy of the base ref, overlay only the candidate test files,
   and run the same command. It must fail. If it still passes, the tests are
   likely hollow or not coupled to the implementation.
3. Attribute step 2 to individual test NODES, and name every node that PASSED there.

Step 3 exists because steps 1-2 grade the whole COMMAND. `red["ok"]` goes False as soon as ANY
test in the command fails against the base, so ONE genuinely discriminating test earned a PASS for
every tautology sitting beside it in the same file, and named none of them. Observed live: a
three-test file with two tautologies returned a bare PASS (Fine-Art-Archive audit finding F3).
Step 3 re-runs the candidate paths ALONE against the same extracted base and reads pytest's
per-node outcomes: a node that fails there is part of the proof, a node that passes there proves
nothing about this change and is reported by node id.

Step 3 is ADVISORY and cannot move `verdict`. That is deliberate -- `verdict`, `ok` and the CLI
exit code keep the exact meaning every existing consumer already reads (`runtime_ac`,
`synthesis_promotion`, `record_verdict`) -- but the hollow node ids ride into the evidence stream
through `reason`, which is what `record_verdict` writes to `outcomes.notes`. When step 3 cannot
attribute (no pytest, a collection error, a non-Python command) it says so with the missing thing
NAMED: an empty hollow list and "could not look" read identically otherwise, which is the same
masking this step exists to remove.

The live worktree is never mutated.

PRECONDITION: THE FIX MUST ALREADY BE IN THE WORKTREE. This is a phase-4 tool — it proves a test
gate is coupled to an implementation, so it needs both to be present. Pointing it at a bare finding
(the fix not yet applied) makes step 1 fail, and a step-1 failure means "your test command does not
pass here", NOT "the finding is unreal" — the two read identically if you were expecting a verdict
on the finding. An auditor holding a candidate defect must apply the patch first, then run this;
without a patch there is nothing for step 2 to remove and the run cannot say anything about the
finding. Recorded because a real audit run reached for it one phase early.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import shlex
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path
from shutil import copy2, copytree

import feedback

TAIL_CHARS = 4000


def capture_evidence_contract(plan: dict, result: dict) -> dict:
    """Typed capture hook; it deliberately accepts no command or raw output."""
    if plan.get("capture_hook") != "local_verify.named_test_capture":
        raise ValueError("plan does not target the local_verify capture hook")
    from capability_compiler import capture_named_test_evidence

    return capture_named_test_evidence(plan, result)


def _tail(text: str, limit: int = TAIL_CHARS) -> str:
    return text[-limit:] if len(text) > limit else text


def _run(cmd: str, cwd: Path, timeout: int) -> dict:
    started = time.time()
    try:
        res = subprocess.run(
            shlex.split(cmd),
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return {
            "cmd": cmd,
            "cwd": str(cwd),
            "returncode": res.returncode,
            "ok": res.returncode == 0,
            "duration_s": round(time.time() - started, 3),
            "stdout_tail": _tail(res.stdout),
            "stderr_tail": _tail(res.stderr),
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "cmd": cmd,
            "cwd": str(cwd),
            "returncode": None,
            "ok": False,
            "duration_s": round(time.time() - started, 3),
            "stdout_tail": _tail((exc.stdout or "") if isinstance(exc.stdout, str) else ""),
            "stderr_tail": f"timed out after {timeout}s",
        }


# --- per-node attribution ---------------------------------------------------------------------
# The disposition table IS the fix. `PASSED` against the base means the node did not detect the
# missing implementation, so it is no part of the deliberate-break proof -- which is exactly what
# command-granularity grading could not say. A legitimate regression guard over PRE-EXISTING
# behaviour also lands in `hollow`, and that is correct: it may be a good test, but it is still not
# part of THIS change's proof.
NODE_OUTCOME_DISPOSITIONS = {
    "PASSED": "hollow",
    "FAILED": "discriminates",
    "ERROR": "discriminates",
    "SKIPPED": "inconclusive",
    "XFAIL": "inconclusive",
    "XPASS": "inconclusive",
}
NODE_DISPOSITIONS = ("discriminates", "hollow", "inconclusive")
_SUMMARY_MARKER = "short test summary info"
# `pytest -v` progress lines: "<nodeid> <OUTCOME>[ (reason)] [ nn%]". Anchored node-then-outcome on
# purpose: the short-summary section says it the other way round ("FAILED <nodeid> - <error>"), and
# parsing both would double-count. The summary is cut off before parsing anyway, because an error
# message there can contain an outcome word.
_NODE_LINE_RE = re.compile(
    r"^(?P<node>\S.*?::.+?)\s+(?P<outcome>PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\b"
)
_EMPTY_NODE_COUNTS = {"nodes": 0, "discriminates": 0, "hollow": 0, "inconclusive": 0}


def _probe_env() -> dict:
    """Environment for the per-node probe: the caller's, minus `PYTEST_ADDOPTS`.

    An inherited `PYTEST_ADDOPTS` changes what the probe can see, in the direction that hurts:
    `-x` stops at the first failing node and leaves every node after it unreported, and `-n auto`
    activates xdist, whose verbose lines put the outcome BEFORE the node id so none of them parse.
    Both would UNDER-report hollow nodes, which is the masking this pass exists to remove. The
    probe's reading has to be a function of the code under test, not of the harness that launched
    it.
    """
    env = dict(os.environ)
    env["PYTEST_ADDOPTS"] = ""
    return env


def _run_argv(
    argv: list[str], cwd: Path, timeout: int, env: dict | None = None
) -> tuple[dict, str]:
    """Like `_run`, but takes argv and ALSO returns the untruncated stdout.

    The bounded report is what gets stored; the full text is what gets parsed. Parsing the
    4000-char tail instead would silently drop the earliest nodes of any sizeable file -- the same
    masking the per-node pass exists to remove, one layer down.
    """
    started = time.time()
    try:
        res = subprocess.run(
            argv,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=env,
        )
        report = {
            "cmd": shlex.join(argv),
            "cwd": str(cwd),
            "returncode": res.returncode,
            "ok": res.returncode == 0,
            "duration_s": round(time.time() - started, 3),
            "stdout_tail": _tail(res.stdout),
            "stderr_tail": _tail(res.stderr),
        }
        return report, res.stdout or ""
    except subprocess.TimeoutExpired as exc:
        partial = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
        report = {
            "cmd": shlex.join(argv),
            "cwd": str(cwd),
            "returncode": None,
            "ok": False,
            "duration_s": round(time.time() - started, 3),
            "stdout_tail": _tail(partial),
            "stderr_tail": f"timed out after {timeout}s",
        }
        return report, partial


def _analysis_python(test_cmd: str) -> str:
    """Pick the interpreter for the per-node probe.

    Reuses the interpreter the test command itself names -- a repo with its own venv has to be
    probed with that venv's pytest -- but ONLY when the token is recognisably a Python executable.
    Deliberately not "whatever the first token is": `test_cmd` is agent-authored, and running an
    arbitrary token with `-m pytest` would widen what this module executes beyond the one command
    it was asked to run.
    """
    try:
        tokens = shlex.split(test_cmd)
    except ValueError:
        tokens = []
    if tokens:
        stem = Path(tokens[0]).name.lower()
        if stem == "python" or stem.startswith(("python3", "python.")):
            return tokens[0]
    return sys.executable


def _parse_node_outcomes(stdout: str) -> list[tuple[str, str]]:
    """Extract (node_id, pytest outcome) pairs from a `pytest -v` run, in report order."""
    head = stdout.split(_SUMMARY_MARKER, 1)[0]
    seen: dict[str, str] = {}
    for line in head.splitlines():
        match = _NODE_LINE_RE.match(line.strip())
        if match:
            # Last outcome wins: a rerun plugin can report the same node twice, and the final
            # word on whether it passed against the base is the one that counts.
            seen[match.group("node")] = match.group("outcome")
    return list(seen.items())


def _node_probe_reason(report: dict, python: str, timeout: int) -> str:
    """Name the missing prerequisite when no node could be attributed.

    Every branch here NAMES what stopped the attribution. A bare "0 hollow nodes" would read as a
    clean per-node proof, so this string is the difference between a gate that reports what it did
    not check and one that goes quiet.
    """
    returncode = report.get("returncode")
    stderr = (report.get("stderr_tail") or "").strip()
    if returncode is None:
        return f"the per-node probe timed out after {timeout}s"
    if "No module named pytest" in stderr:
        return f"pytest is not importable with {python}"
    if returncode == 5:
        return "pytest collected no tests from the candidate paths against the base"
    if returncode == 2:
        return (
            "the candidate paths do not import against the base (pytest reported a collection "
            "error), so the file as a whole fails there but no single node is attributable"
        )
    if returncode == 4:
        return f"pytest rejected the per-node probe arguments: {_tail(stderr, 200)}"
    return f"pytest emitted no parseable per-node outcome lines (returncode={returncode})"


def _analyse_nodes(base_root: Path, test_cmd: str, test_paths: list[str], timeout: int) -> dict:
    """Grade the deliberate break per test NODE against the already-extracted base.

    Scoped to `test_paths` rather than to the whole `test_cmd`, which tightens the evidence twice
    over: it names the tautologies inside a passing file, and it stops a failure in some unrelated
    test the command happens to run from being read as this change's proof.

    Costs ONE extra subprocess, not one per node, and shares the caller's `timeout`. Advisory: it
    returns its own `verdict` and never touches the rolled-up one.
    """
    candidates = [
        path for path in test_paths if path.endswith(".py") or (base_root / path).is_dir()
    ]
    if not candidates:
        named = ", ".join(test_paths[:5]) or "none supplied"
        return {
            "supported": False,
            "verdict": "INDETERMINATE",
            "reason": (
                "no candidate path is a Python test file or directory, so pytest cannot attribute "
                f"per-node outcomes: {named}"
            ),
            "python": None,
            "probe": None,
            "nodes": [],
            "hollow_nodes": [],
            "counts": dict(_EMPTY_NODE_COUNTS),
        }
    python = _analysis_python(test_cmd)
    argv = [python, "-m", "pytest", *candidates, "-v", "--tb=no", "-p", "no:cacheprovider"]
    probe, stdout = _run_argv(argv, base_root, timeout, env=_probe_env())
    nodes = [
        {
            "node_id": node_id,
            "outcome": outcome,
            "disposition": NODE_OUTCOME_DISPOSITIONS.get(outcome, "inconclusive"),
        }
        for node_id, outcome in _parse_node_outcomes(stdout)
    ]
    counts = {"nodes": len(nodes)}
    for disposition in NODE_DISPOSITIONS:
        counts[disposition] = sum(1 for node in nodes if node["disposition"] == disposition)
    hollow = [node["node_id"] for node in nodes if node["disposition"] == "hollow"]
    if not nodes:
        verdict = "INDETERMINATE"
        reason = _node_probe_reason(probe, python, timeout)
    elif hollow:
        verdict = "FAIL_HOLLOW_NODES"
        reason = f"{len(hollow)} of {len(nodes)} candidate nodes pass against the base"
    else:
        verdict = "PASS"
        reason = f"all {len(nodes)} candidate nodes fail against the base"
    return {
        "supported": True,
        "verdict": verdict,
        "reason": reason,
        "python": python,
        "probe": probe,
        "nodes": nodes,
        "hollow_nodes": hollow,
        "counts": counts,
    }


def name_nodes(node_ids: list[str], limit: int = 5) -> str:
    """Name node ids for a bounded reason string, keeping the count when the list is long.

    Public because `synthesis_promotion` names the same nodes in its candidate body, and one
    bounded formatter beats two that drift.
    """
    extra = len(node_ids) - limit
    named = ", ".join(node_ids[:limit])
    return f"{named} (+{extra} more)" if extra > 0 else named


# --- the quotable artifact ----------------------------------------------------------------------
# WHY THIS EXISTS. The Counter_Risk audit of 2026-08-24 reached for this capability at
# `repo-audit:phase-4`, judged it "a genuinely close match to what I did by hand", and ran the
# break-then-revert itself anyway. The stated reason was not a capability mismatch: AGENT_ISSUE_FORMAT
# requires a named test gate with the raw before/after console output QUOTED into the issue body, and
# `verify()` returns a structured verdict whose console output is JSON-escaped inside it. Every audit
# on record has re-run the same proof by hand for the same reason.
#
# So this adds no measurement and captures nothing new -- `green` and `red` already hold both halves.
# It is a pure formatter over the existing result, which is why it cannot change a verdict, an exit
# code or a consumer.
_FENCE_RUN = re.compile(r"`+")


def _fence(*bodies: str) -> str:
    """A fence longer than the longest backtick run in the content it must survive.

    Test output really does contain backticks (a pytest assertion repr of a string with one), and a
    three-backtick fence around it renders as two broken blocks in the issue body this exists to
    produce.
    """
    longest = max(
        (len(m.group(0)) for body in bodies for m in _FENCE_RUN.finditer(body)), default=0
    )
    return "`" * max(3, longest + 1)


def _console(run: dict | None) -> str:
    if not run:
        return "(the run did not happen)"
    parts = [str(run.get("stdout_tail") or "").rstrip(), str(run.get("stderr_tail") or "").rstrip()]
    body = "\n".join(p for p in parts if p)
    return body or "(no output)"


def _rc(run: dict | None) -> str:
    if not run:
        return "n/a"
    code = run.get("returncode")
    return "killed/timed out" if code is None else str(code)


def uncopied_test_paths(result: dict) -> list[str]:
    """Declared `--test-path`s that never reached the base tree, so RED proves nothing about them.

    `_copy_candidate_paths` copies a path only when it resolves to a real file or directory, and it
    reports what it copied. A pytest NODE ID (`tests/test_x.py::test_y`) resolves to neither, so it
    is silently skipped -- the base tree then has no candidate test at all, the command fails with
    "file or directory not found", and `red["ok"]` is False for the wrong reason. The rolled-up
    verdict is PASS and it means "the test is ABSENT from the base", not "the test FAILS against the
    base". Those read identically in a JSON verdict and differently in a transcript somebody quotes
    into a permanent record, which is why the transcript names them and the verdict is left alone.
    """
    if not result.get("red"):
        return []
    copied = {str(p) for p in result.get("copied_test_paths") or []}
    return [str(p) for p in result.get("test_paths") or [] if str(p) not in copied]


def _path_covered(path: str, copied: list[str]) -> bool:
    """Did the overlay carry this changed path into the base tree? Directories cover their files."""
    target = str(path).strip("/")
    for entry in copied:
        item = str(entry).strip("/")
        if not item:
            continue
        if target == item or target.startswith(item + "/"):
            return True
    return False


def overlay_covers_every_change(result: dict) -> bool:
    """Did the overlay carry EVERY difference between the worktree and the base into the base?

    If it did, the base tree after the overlay is identical to the worktree in every file that
    differs -- so RED and GREEN ran the same code and the comparison is degenerate whichever way it
    lands. This is the WHOLE test, not a heuristic about which files look like tests: it is the
    exact condition under which the run can prove nothing.

    MEASURED (Counter_Risk #964, 2026-08-25): when the fix itself lives in TEST files, the default
    `--test-path` scope is "every changed test file", which is every changed file -- so the overlay
    carried the fix into the base, the red step came back GREEN, and the run reported FAIL_HOLLOW.
    That verdict is a statement about the TESTS and it was false; the truth was a misconfigured run.
    Scoping `--test-path` to only the new module was load-bearing and had to be known in advance.

    CONSERVATIVE ON PURPOSE. It fires only when NOTHING is left uncovered, so the correct usage --
    scoping the overlay to the new test while the fix stays behind in the base -- can never trip it.
    A deletion cannot be copied, so a worktree that removes a file is never flagged.
    """
    if not result.get("red"):
        return False
    changed = [str(p) for p in result.get("changed_paths") or []]
    copied = [str(p) for p in result.get("copied_test_paths") or []]
    if result.get("changed_paths") is None:
        return False
    return all(_path_covered(p, copied) for p in changed)


# ONE PREDICATE, TWO CAUSES, so a consumer has a single question to ask: "is this a valid
# demonstration?". They were separate before and only one of them reached the transcript, so a JSON
# consumer -- which is every automated one -- could not see either.
def invalid_demonstration(result: dict) -> list[str]:
    """Why this run proves nothing, in words. Empty means the demonstration stands.

    REPORTED, NEVER GATED. `verdict`, `ok` and the CLI exit code keep the exact meaning every
    existing consumer already reads (`runtime_ac`, `synthesis_promotion`, `record_verdict`), and a
    selftest pins that they do not move. The finding rides on `reason`, which `record_verdict`
    writes to `outcomes.notes`, and leads the quotable transcript -- because a transcript that
    travels into a PR body leaves its caveats behind if they live anywhere else.
    """
    reasons: list[str] = []
    stale = uncopied_test_paths(result)
    if stale:
        reasons.append(
            ", ".join(stale) + " was not copied into the base tree — `--test-path` takes files and "
            "directories, not pytest node ids. The base therefore ran without that test at all, so "
            "the red below means the test was ABSENT, not that it FAILED. Re-run with the "
            "containing file as `--test-path` (the node id may stay in `--test-cmd`, which runs "
            "verbatim)."
        )
    if overlay_covers_every_change(result):
        changed = ", ".join(str(p) for p in result.get("changed_paths") or []) or "(nothing)"
        reasons.append(
            "the overlay carried EVERY file that differs from the base into the base tree "
            f"({changed}), so RED and GREEN ran the same code and neither verdict means anything. "
            "This is what happens when the fix itself lives in test files and `--test-path` "
            "defaults to every changed test file. Re-run with `--test-path` scoped to the NEW test "
            "only, leaving the fix behind in the base."
        )
    return reasons


def break_transcript(result: dict, *, worktree_label: str = "") -> str:
    """The red/green console transcript, in the shape an issue or PR body can quote verbatim.

    RED FIRST, because that is the reading order of the claim being made: the gate fails without the
    implementation, then passes with it. Every caveat the structured result carries is stated INSIDE
    the block rather than beside it -- a transcript that travels into an issue body leaves its
    caveats behind if they live anywhere else, and an overstated proof in a permanent record is worse
    than no proof at all.
    """
    verdict = str(result.get("verdict") or "ERROR")
    green, red = result.get("green"), result.get("red")
    cmd = str((green or red or {}).get("cmd") or "")
    lines = [f"### Deliberate-break demonstration — {verdict}", ""]
    if result.get("error"):
        lines += [f"**The run could not complete:** {result['error']}", ""]
    lines += [
        f"- gate: `{cmd}`" if cmd else "- gate: (none recorded)",
        f"- base ref: `{result.get('base_ref')}`",
        f"- worktree: `{worktree_label or result.get('worktree')}`",
    ]
    paths = [str(p) for p in result.get("test_paths") or []]
    if paths:
        lines.append(
            "- candidate tests overlaid onto the base: " + ", ".join(f"`{p}`" for p in paths)
        )
    lines.append("")

    # LOUD, and at the top, because these are the shapes of a FALSE proof: RED is red (or green) for
    # a reason that has nothing to do with the implementation, and the verdict reads normal either
    # way. Both causes now come from ONE predicate, so a cause added later cannot reach the JSON and
    # miss the transcript.
    for why in invalid_demonstration(result):
        lines += ["> **THIS IS NOT A VALID DEMONSTRATION.** " + why, ""]

    red_body, green_body = _console(red), _console(green)
    fence = _fence(red_body, green_body)
    red_section = [
        f"**RED — the gate against the base implementation** (`{result.get('base_ref')}` with only "
        f"the candidate tests overlaid). Exit code `{_rc(red)}`.",
        "",
        fence,
        red_body,
        fence,
        "",
    ]
    green_section = [
        f"**GREEN — the same gate in the worktree, with the implementation present.** Exit code "
        f"`{_rc(green)}`.",
        "",
        fence,
        green_body,
        fence,
        "",
    ]
    # RED THEN GREEN, as one expression, because that ORDER is the claim: the gate fails without the
    # implementation and passes with it. Reversed, the same two blocks read as a regression.
    lines += red_section + green_section

    node_verdict = result.get("node_verdict")
    hollow = [str(n) for n in result.get("hollow_nodes") or []]
    counts = (result.get("node_analysis") or {}).get("counts") or {}
    if hollow:
        lines.append(
            f"**Per-node attribution: {node_verdict}.** {len(hollow)} of {counts.get('nodes')} "
            f"candidate nodes PASS against the base and are therefore no part of this proof: "
            f"{name_nodes(hollow)}. The command-level red above was earned by the others."
        )
    elif node_verdict == "INDETERMINATE":
        lines.append(
            "**Per-node attribution unavailable**, so the red above is graded per COMMAND and one "
            "discriminating test would earn it for every tautology beside it: "
            f"{(result.get('node_analysis') or {}).get('reason')}"
        )
    elif node_verdict == "PASS":
        lines.append(
            f"**Per-node attribution: PASS.** All {counts.get('nodes')} candidate nodes fail "
            "against the base, so no tautology is riding along on another test's red."
        )
    lines.append("")
    lines.append(
        "_Produced by `local_verify.py --transcript`; the live worktree was never mutated._"
    )
    return "\n".join(lines) + "\n"


def _git(worktree: Path, args: list[str]) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["git", "-C", str(worktree), *args], capture_output=True, check=False)


def _changed_paths(worktree: Path, base_ref: str) -> list[str]:
    paths: set[str] = set()
    diff = _git(worktree, ["diff", "--name-only", base_ref])
    if diff.returncode == 0:
        paths.update(p.decode().strip() for p in diff.stdout.splitlines() if p.strip())
    status = subprocess.run(
        ["git", "-C", str(worktree), "status", "--short", "--untracked-files=all"],
        capture_output=True,
        text=True,
        check=False,
    )
    if status.returncode == 0:
        for line in status.stdout.splitlines():
            path = (line[3:] if len(line) >= 4 and line[2] == " " else line[2:]).strip()
            if " -> " in path:
                path = path.split(" -> ", 1)[1].strip()
            if path:
                paths.add(path)
    return sorted(paths)


def _looks_like_test(path: str) -> bool:
    p = Path(path)
    name = p.name.lower()
    parts = {part.lower() for part in p.parts}
    return "tests" in parts or name.startswith("test_") or name.endswith("_test.py")


def _default_test_paths(worktree: Path, base_ref: str) -> list[str]:
    """The default overlay: every CHANGED path that looks like a test.

    `verify()` no longer calls this — it reads `_changed_paths` once and filters, because the whole
    changed list is needed to answer whether the overlay covered all of it. Kept because the default
    IS this rule and the rule deserves a name; the two must not drift, so both filter the same way.
    """
    return [path for path in _changed_paths(worktree, base_ref) if _looks_like_test(path)]


def _extract_base(worktree: Path, base_ref: str, dest: Path) -> str | None:
    archive = _git(worktree, ["archive", "--format=tar", base_ref])
    if archive.returncode != 0:
        return (
            archive.stderr.decode(errors="replace").strip() or f"git archive failed for {base_ref}"
        )
    with tarfile.open(fileobj=io.BytesIO(archive.stdout), mode="r:") as tf:
        tf.extractall(dest)
    return None


def _copy_candidate_paths(worktree: Path, dest: Path, paths: list[str]) -> list[str]:
    copied = []
    for rel in paths:
        src = worktree / rel
        dst = dest / rel
        if src.is_dir():
            copytree(src, dst, dirs_exist_ok=True)
            copied.append(rel)
        elif src.is_file():
            dst.parent.mkdir(parents=True, exist_ok=True)
            copy2(src, dst)
            copied.append(rel)
    return copied


def _pytest_failure_cause(run: dict) -> dict:
    """Was this non-zero run a failing TEST, or the runner failing for another reason?

    Delegates to `testgen_gate.pytest_failure_cause`, which owns this vocabulary already — the exit
    table lives there and a second copy here would drift. Fails toward "a test failed" if that
    module cannot be imported: excusing a real red is far worse than reporting one.
    """
    text = f"{run.get('stdout_tail') or ''}\n{run.get('stderr_tail') or ''}"
    try:
        import testgen_gate

        return testgen_gate.pytest_failure_cause(run.get("returncode"), text)
    except Exception:  # noqa: BLE001
        return {"test_failure": True, "cause": "a test failed", "remedy": ""}


def verify(
    worktree: str | Path,
    *,
    base_ref: str = "HEAD",
    test_cmd: str,
    test_paths: list[str] | None = None,
    timeout: int = 120,
) -> dict:
    wt = Path(worktree).resolve()
    # Read ONCE and carried into the result: the default test paths are a SUBSET of this list, and
    # `overlay_covers_every_change` needs the whole of it. Deriving the two from separate git calls
    # would let them disagree about what changed.
    changed = _changed_paths(wt, base_ref)
    tests = test_paths or [path for path in changed if _looks_like_test(path)]
    if not tests:
        return {
            "verdict": "ERROR",
            "ok": False,
            "error": "no test paths supplied or detected",
            "worktree": str(wt),
            "base_ref": base_ref,
        }

    green = _run(test_cmd, wt, timeout)
    if not green["ok"]:
        # A NON-ZERO EXIT IS NOT PROOF THE TESTS FAILED. `pytest-cov --cov-fail-under` exits 1 — the
        # same code as a real red — while every selected test passed, and narrowing with `-k` is
        # what collapses the coverage total in the first place. Returning FAIL_BROKEN there tells
        # the caller to fix tests that are fine, and the two readings demand opposite actions.
        # Found 2026-08-26 by running this against stranske/Fine-Art-Archive: verdict FAIL_BROKEN on
        # a test measured PASSING (exit 0 with --no-cov, exit 1 with the gate, "1 passed, 92
        # deselected" both times).
        cause = _pytest_failure_cause(green)
        if not cause["test_failure"]:
            # ERROR, not FAIL_*: this is "could not perform the comparison", which is what ERROR
            # already means in this module (no test paths, base extraction failed). Claiming a
            # verdict about the tests here would be the one-sentinel-two-meanings defect.
            return {
                "verdict": "ERROR",
                "ok": False,
                "error": f"the test command failed for a reason other than a failing test: {cause['cause']}",
                "reason": cause["cause"],
                "remedy": cause["remedy"],
                "non_test_failure": cause,
                "worktree": str(wt),
                "base_ref": base_ref,
                "test_paths": tests,
                "green": green,
                "red": None,
            }
        return {
            "verdict": "FAIL_BROKEN",
            "ok": False,
            "reason": "candidate tests fail in the live worktree",
            "worktree": str(wt),
            "base_ref": base_ref,
            "test_paths": tests,
            "green": green,
            "red": None,
        }

    with tempfile.TemporaryDirectory(prefix="orch-local-verify-") as tmp:
        red_root = Path(tmp) / "base"
        red_root.mkdir()
        err = _extract_base(wt, base_ref, red_root)
        if err:
            return {
                "verdict": "ERROR",
                "ok": False,
                "error": err,
                "worktree": str(wt),
                "base_ref": base_ref,
                "test_paths": tests,
                "green": green,
                "red": None,
            }
        copied = _copy_candidate_paths(wt, red_root, tests)
        red = _run(test_cmd, red_root, timeout)
        # Inside the temporary directory on purpose: the probe needs the SAME extracted base the
        # red run used, and that base stops existing when this block exits.
        nodes = _analyse_nodes(red_root, test_cmd, tests, timeout)

    if red["ok"]:
        verdict = "FAIL_HOLLOW"
        ok = False
        reason = "candidate tests still pass against the base implementation"
    else:
        verdict = "PASS"
        ok = True
        reason = "candidate tests pass live and fail against the base implementation"
    # The per-node finding rides on `reason`, which is what record_verdict() writes to
    # outcomes.notes -- so a PASS carrying tautologies stops being indistinguishable from a clean
    # one, and a PASS whose per-node pass could not run SAYS so rather than implying precision it
    # does not have.
    if nodes["hollow_nodes"]:
        reason += (
            f"; {len(nodes['hollow_nodes'])} of {nodes['counts']['nodes']} candidate nodes do not "
            f"discriminate: {name_nodes(nodes['hollow_nodes'])}"
        )
    elif nodes["verdict"] == "INDETERMINATE":
        reason += f"; per-node attribution unavailable: {nodes['reason']}"
    result = {
        "verdict": verdict,
        "ok": ok,
        "reason": reason,
        "worktree": str(wt),
        "base_ref": base_ref,
        "test_paths": tests,
        "copied_test_paths": copied,
        "changed_paths": changed,
        "green": green,
        "red": red,
        "node_verdict": nodes["verdict"],
        "hollow_nodes": nodes["hollow_nodes"],
        "node_analysis": nodes,
    }
    # THE JSON CONSUMER IS THE ONE BEING MISLED, so the finding goes in the result and not only in
    # the transcript. `verdict` and `ok` are computed ABOVE this and are deliberately untouched: a
    # field that could move a gate would make the artifact and the gate two answers to one question,
    # the same rule `--transcript` already follows. It rides `reason` because that is what
    # `record_verdict` writes to `outcomes.notes`, so the evidence stream sees it too.
    invalid = invalid_demonstration(result)
    result["demonstration_valid"] = not invalid
    result["invalid_demonstration_reasons"] = invalid
    if invalid:
        result["reason"] = (
            "THIS IS NOT A VALID DEMONSTRATION — " + " ".join(invalid) + " (verdict as computed: "
            f"{verdict}; {reason})"
        )
    return result


def record_verdict(run_id: str, result: dict) -> dict:
    """Patch the feedback outcome row with this verifier verdict."""
    verdict = result.get("verdict") or "ERROR"
    reason = result.get("reason") or result.get("error") or "local verifier completed"
    # Node ids where the per-node pass produced them, file paths otherwise: `test_ids` is the only
    # field in the completion-event schema that carries test identity, and a node id is the finer
    # one. Which of them are hollow is named in `reason`, hence in outcomes.notes -- resist adding
    # a key for it, feedback._sanitize_completion_payload REJECTS unknown payload fields.
    node_ids = [
        str(node["node_id"])
        for node in (result.get("node_analysis") or {}).get("nodes") or []
        if node.get("node_id")
    ]
    feedback.record_outcome(
        run_id,
        verifier_verdict=verdict,
        notes=f"local_verify: {verdict} - {reason}",
    )
    event = feedback.record_completion_event(
        run_id,
        event_type="verification",
        phase="verification",
        producer="local_verify",
        status=verdict,
        payload={
            "test_ids": node_ids or result.get("test_paths") or [],
            "acceptance_gate_ids": ["local-deliberate-break"],
            "result_hashes": [
                feedback._completion_hash(result.get("green")),
                feedback._completion_hash(result.get("red")),
            ],
            "verification": {
                "verifier_verdict": verdict,
                "verifier_ids": ["local_verify"],
                "result_hashes": {
                    "result": feedback._completion_hash(result),
                },
            },
        },
    )
    return {
        "run_id": run_id,
        "verifier_verdict": verdict,
        "recorded": True,
        # Reported so a payload the sanitizer REJECTED cannot look like one it stored.
        "validation_status": event.get("validation_status"),
    }


def _init_repo(root: Path, *, correct_impl: bool, test_body: str) -> Path:
    wt = root / "repo"
    wt.mkdir(parents=True)
    subprocess.run(["git", "init", str(wt)], check=True, capture_output=True, text=True)
    subprocess.run(
        ["git", "-C", str(wt), "config", "user.email", "verify@example.test"], check=True
    )
    subprocess.run(["git", "-C", str(wt), "config", "user.name", "Verifier"], check=True)
    (wt / "math_utils.py").write_text("def add(a, b):\n    return 0\n")
    subprocess.run(["git", "-C", str(wt), "add", "math_utils.py"], check=True)
    subprocess.run(
        ["git", "-C", str(wt), "commit", "-m", "base"], check=True, capture_output=True, text=True
    )
    if correct_impl:
        (wt / "math_utils.py").write_text("def add(a, b):\n    return a + b\n")
    (wt / "tests").mkdir()
    (wt / "tests" / "test_math_utils.py").write_text(test_body)
    return wt


def _selftest() -> None:
    sound_test = """import unittest
from math_utils import add

class TestAdd(unittest.TestCase):
    def test_add(self):
        self.assertEqual(add(2, 3), 5)
"""
    hollow_test = """import unittest

class TestHollow(unittest.TestCase):
    def test_hollow(self):
        self.assertTrue(True)
"""
    # One real test and one tautology in the SAME file: the case command-granularity grading
    # cannot see, because the real test alone makes the whole command fail against the base.
    mixed_test = """import unittest
from math_utils import add

class TestMixed(unittest.TestCase):
    def test_real(self):
        self.assertEqual(add(2, 3), 5)

    def test_tautology(self):
        self.assertTrue(True)
"""
    cmd = f"{sys.executable} -m unittest discover -s tests"
    with tempfile.TemporaryDirectory(prefix="local-verify-selftest-") as tmp:
        sound = _init_repo(Path(tmp) / "sound", correct_impl=True, test_body=sound_test)
        out = verify(
            sound,
            base_ref="HEAD",
            test_cmd=cmd,
            test_paths=["tests/test_math_utils.py"],
            timeout=30,
        )
        sound_out = out
        assert out["verdict"] == "PASS", out
        assert out["node_verdict"] == "PASS", out["node_analysis"]
        assert out["hollow_nodes"] == [], out["hollow_nodes"]
        assert out["node_analysis"]["counts"] == {
            "nodes": 1,
            "discriminates": 1,
            "hollow": 0,
            "inconclusive": 0,
        }, out["node_analysis"]

        hollow = _init_repo(Path(tmp) / "hollow", correct_impl=True, test_body=hollow_test)
        out = verify(
            hollow,
            base_ref="HEAD",
            test_cmd=cmd,
            test_paths=["tests/test_math_utils.py"],
            timeout=30,
        )
        assert out["verdict"] == "FAIL_HOLLOW", out
        assert out["hollow_nodes"] == ["tests/test_math_utils.py::TestHollow::test_hollow"], out[
            "hollow_nodes"
        ]

        # PER-NODE GRANULARITY. The rolled-up verdict is PASS either way here -- red["ok"] goes
        # False on test_real alone -- so the whole deliverable is that test_tautology is NAMED.
        # (Observed live as a bare PASS: Fine-Art-Archive audit finding F3, 2 of 3 tautologies.)
        mixed = _init_repo(Path(tmp) / "mixed", correct_impl=True, test_body=mixed_test)

        def verify_mixed() -> dict:
            return verify(
                mixed,
                base_ref="HEAD",
                test_cmd=cmd,
                test_paths=["tests/test_math_utils.py"],
                timeout=30,
            )

        taut = "tests/test_math_utils.py::TestMixed::test_tautology"
        mixed_out = verify_mixed()
        assert mixed_out["verdict"] == "PASS", mixed_out
        assert mixed_out["node_verdict"] == "FAIL_HOLLOW_NODES", mixed_out["node_analysis"]
        assert mixed_out["hollow_nodes"] == [taut], mixed_out["hollow_nodes"]
        assert taut in mixed_out["reason"], mixed_out["reason"]
        assert mixed_out["node_analysis"]["counts"] == {
            "nodes": 2,
            "discriminates": 1,
            "hollow": 1,
            "inconclusive": 0,
        }, mixed_out["node_analysis"]

        # DELIBERATE BREAK -> REVERT on the disposition table, which IS the fix: count a node that
        # PASSES against the base as part of the proof and the run reverts to exactly the
        # command-granularity PASS that masked the tautology.
        saved_dispositions = dict(NODE_OUTCOME_DISPOSITIONS)
        try:
            NODE_OUTCOME_DISPOSITIONS["PASSED"] = "discriminates"
            broken_nodes = verify_mixed()
            assert broken_nodes["verdict"] == "PASS", broken_nodes
            assert (
                broken_nodes["hollow_nodes"] == []
            ), "break did not change behaviour -- test is vacuous"
            assert broken_nodes["node_verdict"] == "PASS", broken_nodes["node_analysis"]
            assert taut not in broken_nodes["reason"], broken_nodes["reason"]
        finally:
            NODE_OUTCOME_DISPOSITIONS.clear()
            NODE_OUTCOME_DISPOSITIONS.update(saved_dispositions)
        reverted = verify_mixed()
        assert reverted["hollow_nodes"] == [taut], "revert did not restore per-node attribution"

        # ---- THE QUOTABLE ARTIFACT. Phase 4 of every audit on record re-ran this proof by hand
        # because the artifact it is graded on is the raw before/after console output in an issue
        # body, and the verdict dict escapes it inside JSON. So the assertion that matters is
        # VERBATIM CONTAINMENT, not the presence of a heading.
        mixed_text = break_transcript(mixed_out)
        # ASSERTED AGAINST THE RAW RESULT FIELDS, never through `_console`. Going through the
        # formatter's own helper made the first version of this assertion use the very thing it
        # guards: escaping the output escaped both sides equally and the break stayed green.
        for half in ("red", "green"):
            for stream in ("stdout_tail", "stderr_tail"):
                raw = str(mixed_out[half].get(stream) or "").rstrip()
                assert not raw or raw in mixed_text, (
                    f"the {half} run's {stream} is not quotable verbatim out of the transcript, "
                    "which is the entire artifact an issue body is graded on",
                    half,
                    stream,
                    mixed_text,
                )
        assert mixed_text.index("**RED —") < mixed_text.index("**GREEN —"), (
            "the break-then-revert claim reads red-before-green; the transcript must too",
            mixed_text,
        )
        # ...AND IT MAY NOT OVERSTATE. `mixed` is a command-level PASS carrying a tautology, which
        # is exactly the body that would claim a gate it did not earn.
        assert taut in mixed_text and "no part of this proof" in mixed_text, mixed_text
        sound_text = break_transcript(sound_out)
        assert "no tautology is riding along" in sound_text, sound_text
        assert "no part of this proof" not in sound_text, sound_text

        # ---- THE COPY-SCOPE GUARD. `--test-path` takes files and directories; a pytest NODE ID is
        # silently not copied, so the base runs without the test, red is red because it is ABSENT,
        # and the rolled-up verdict is PASS. REPORTED, never gated: the verdict below is asserted
        # UNCHANGED, because a rendering flag that could move a gate would make the artifact and
        # the gate two different answers.
        node_id_path = "tests/test_math_utils.py::TestAdd::test_add"
        node_scoped = verify(
            sound, base_ref="HEAD", test_cmd=cmd, test_paths=[node_id_path], timeout=30
        )
        assert node_scoped["verdict"] == "PASS", node_scoped
        assert node_scoped["copied_test_paths"] == [], node_scoped["copied_test_paths"]
        assert uncopied_test_paths(node_scoped) == [node_id_path], node_scoped
        node_text = break_transcript(node_scoped)
        assert "THIS IS NOT A VALID DEMONSTRATION" in node_text, node_text
        assert node_id_path in node_text, node_text
        # A real file path is copied, so the guard must stay silent there -- a warning on every run
        # is a warning nobody reads.
        assert uncopied_test_paths(sound_out) == [], sound_out
        assert "THIS IS NOT A VALID DEMONSTRATION" not in sound_text, sound_text

        # ---- THE OVERLAY-SCOPE GUARD (2026-08-25). When the FIX ITSELF LIVES IN TEST FILES, the
        # default `--test-path` scope is "every changed test file", which is every changed file --
        # so the overlay carries the fix into the base, the base is not broken, and the red step
        # comes back GREEN. The run then reports FAIL_HOLLOW: a statement about the TESTS, and a
        # false one. Measured on Counter_Risk #964, where scoping `--test-path` to only the new
        # module was load-bearing and had to be known in advance.
        carried = Path(tmp) / "carried" / "repo"
        carried.mkdir(parents=True)
        subprocess.run(["git", "init", str(carried)], check=True, capture_output=True, text=True)
        subprocess.run(
            ["git", "-C", str(carried), "config", "user.email", "verify@example.test"], check=True
        )
        subprocess.run(["git", "-C", str(carried), "config", "user.name", "Verifier"], check=True)
        (carried / "math_utils.py").write_text("def add(a, b):\n    return a + b\n")
        (carried / "tests").mkdir()
        # The DEFECT is in the guard: it inspects source text instead of executing anything, so it
        # passes whatever `add` does. That is the thing the change fixes, and it lives in a test.
        (carried / "tests" / "test_guard.py").write_text(
            "import unittest\n\n"
            "class TestGuard(unittest.TestCase):\n"
            "    def test_guard(self):\n"
            "        self.assertIn('def add', open('math_utils.py').read())\n"
        )
        subprocess.run(["git", "-C", str(carried), "add", "-A"], check=True)
        subprocess.run(
            ["git", "-C", str(carried), "commit", "-m", "base with a grep-shaped guard"],
            check=True,
            capture_output=True,
            text=True,
        )
        # THE FIX, entirely inside test files: the guard now executes, and a regression test is
        # added beside it. No production file changes at all.
        (carried / "tests" / "test_guard.py").write_text(
            "import unittest\nfrom math_utils import add\n\n"
            "class TestGuard(unittest.TestCase):\n"
            "    def test_guard(self):\n"
            "        self.assertEqual(add(2, 3), 5)\n"
        )
        (carried / "tests" / "test_regression.py").write_text(
            "import unittest\nfrom math_utils import add\n\n"
            "class TestRegression(unittest.TestCase):\n"
            "    def test_regression(self):\n"
            "        self.assertEqual(add(0, 0), 0)\n"
        )
        degenerate = verify(carried, base_ref="HEAD", test_cmd=cmd, timeout=30)
        # The default scope picked up EVERY changed file, so base + overlay == worktree.
        assert sorted(degenerate["changed_paths"]) == [
            "tests/test_guard.py",
            "tests/test_regression.py",
        ], degenerate["changed_paths"]
        assert sorted(degenerate["copied_test_paths"]) == sorted(
            degenerate["changed_paths"]
        ), degenerate
        assert overlay_covers_every_change(degenerate), degenerate
        assert degenerate["demonstration_valid"] is False, degenerate
        # THE VERDICT IS UNCHANGED — reported, never gated, exactly as the copy-scope guard above.
        # And this is the false verdict the finding is about: it accuses the tests of being hollow.
        assert degenerate["verdict"] == "FAIL_HOLLOW", degenerate["verdict"]
        assert degenerate["reason"].startswith("THIS IS NOT A VALID DEMONSTRATION"), degenerate[
            "reason"
        ]
        assert "FAIL_HOLLOW" in degenerate["reason"], (
            "the computed verdict must survive INSIDE the reason; a consumer that reads only "
            "`reason` must still learn what the run concluded",
            degenerate["reason"],
        )
        degenerate_text = break_transcript(degenerate)
        assert "THIS IS NOT A VALID DEMONSTRATION" in degenerate_text, degenerate_text
        assert "carried EVERY file that differs" in degenerate_text, degenerate_text
        # ...AND THE CORRECT USAGE IS SILENT. Same repo, same fix, `--test-path` scoped to the new
        # module alone: the guard's fix stays behind in the base, so the comparison is real. This is
        # the assertion that makes the guard usable rather than a warning on every run.
        scoped = verify(
            carried,
            base_ref="HEAD",
            test_cmd=cmd,
            test_paths=["tests/test_regression.py"],
            timeout=30,
        )
        assert scoped["demonstration_valid"] is True, scoped
        assert "THIS IS NOT A VALID DEMONSTRATION" not in break_transcript(scoped), scoped
        # A DIRECTORY OVERLAY COVERS THE FILES UNDER IT, and an absent `changed_paths` is never
        # read as "nothing changed" — synthetic, because both are about the predicate's inputs.
        assert overlay_covers_every_change(
            {
                "red": {"ok": False},
                "changed_paths": ["tests/a.py", "tests/b.py"],
                "copied_test_paths": ["tests"],
            }
        )
        assert not overlay_covers_every_change(
            {
                "red": {"ok": False},
                "changed_paths": ["src/x.py", "tests/a.py"],
                "copied_test_paths": ["tests"],
            }
        )
        assert not overlay_covers_every_change(
            {"red": {"ok": False}, "copied_test_paths": ["tests"]}
        )

        # ---- THE FENCE MUST SURVIVE ITS CONTENT. Pytest really does print backticks (an assertion
        # repr of a string containing one), and a 3-backtick fence around them renders as two broken
        # blocks in the issue body this exists to produce. Pure function, synthetic input.
        fenced = break_transcript(
            {
                "verdict": "PASS",
                "base_ref": "HEAD",
                "worktree": "/w",
                "test_paths": ["tests/t.py"],
                "copied_test_paths": ["tests/t.py"],
                "green": {"cmd": "pytest", "returncode": 0, "stdout_tail": "ok"},
                "red": {"cmd": "pytest", "returncode": 1, "stdout_tail": "E   assert '```x' == ''"},
            }
        )
        assert "\n````\n" in fenced, fenced
        assert "```x" in fenced, fenced

        # The two parse shapes that would silently UNDER-report. An outcome word inside a summary
        # error message must not be read as a node (hence the summary cut), and xdist's
        # outcome-before-node line must yield NOTHING so the run reports INDETERMINATE rather than
        # a confidently short hollow list.
        assert _parse_node_outcomes(
            "t.py::a PASSED [ 50%]\n"
            f"=== {_SUMMARY_MARKER} ===\n"
            "FAILED t.py::b - AssertionError: PASSED was expected\n"
        ) == [("t.py::a", "PASSED")]
        assert _parse_node_outcomes("[gw0] [ 50%] PASSED t.py::a\n") == []
        assert _probe_env()["PYTEST_ADDOPTS"] == ""

        # An absent prerequisite must NAME itself. "no hollow nodes" and "could not look" read
        # identically otherwise, which is the masking this pass exists to remove.
        unsupported = _analyse_nodes(Path(tmp), cmd, ["src/app.js"], 5)
        assert unsupported["verdict"] == "INDETERMINATE", unsupported
        assert unsupported["supported"] is False, unsupported
        assert "src/app.js" in unsupported["reason"], unsupported["reason"]
        assert unsupported["hollow_nodes"] == [], unsupported

        broken = _init_repo(Path(tmp) / "broken", correct_impl=False, test_body=sound_test)
        out = verify(
            broken,
            base_ref="HEAD",
            test_cmd=cmd,
            test_paths=["tests/test_math_utils.py"],
            timeout=30,
        )
        assert out["verdict"] == "FAIL_BROKEN", out

        old_db = feedback.DB_PATH
        feedback.DB_PATH = Path(tmp) / "feedback.db"
        try:
            feedback.record_run("lv-hollow", "o/r#1", "verifytest", "cursor")
            rec = record_verdict("lv-hollow", out)
            assert rec["verifier_verdict"] == "FAIL_BROKEN", rec
            # The node ids reach the completion event through `test_ids`, and the payload must be
            # ACCEPTED -- an unknown field would be silently rejected by the sanitizer.
            feedback.record_run("lv-mixed", "o/r#2", "verifytest", "cursor")
            mixed_rec = record_verdict("lv-mixed", mixed_out)
            assert mixed_rec["validation_status"] != "rejected", mixed_rec
            with feedback._conn() as conn:
                stored = conn.execute(
                    "SELECT notes FROM outcomes WHERE run_id=?", ("lv-mixed",)
                ).fetchone()
            assert stored and taut in stored[0], stored
            ver = feedback.relearn({"verifytest": {"cursor": 0.5}})
            learned = feedback.current_weights("verifytest", ver)[0]
            assert learned["posterior"] < 0.5, learned
        finally:
            feedback.DB_PATH = old_db

    print(
        "local_verify.py selftest: OK (green/red deliberate-break PASS, hollow, broken, per-node "
        "hollow attribution w/ break->revert, named-prerequisite INDETERMINATE, feedback record, "
        "quotable transcript w/ verbatim console + copy-scope guard + overlay-scope guard "
        "+ fence escape)"
    )


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

        capabilities.production_heartbeat(
            "deliberate-break-verifier", event_type, ref="local_verify.main"
        )
    except Exception:
        pass


def main(argv: list[str]) -> int:
    _capability_heartbeat()
    if "--selftest" in argv:
        _selftest()
        return 0
    parser = argparse.ArgumentParser(description="Run a local deliberate-break verifier.")
    parser.add_argument("--worktree", required=True)
    parser.add_argument("--base-ref", default="HEAD")
    parser.add_argument("--test-cmd", required=True)
    parser.add_argument("--test-path", action="append", default=[])
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument(
        "--record-run-id",
        default="",
        help="optional feedback.run_id to patch with verifier_verdict",
    )
    parser.add_argument(
        "--transcript",
        action="store_true",
        help="print the quotable red/green console transcript instead of the JSON verdict — the "
        "artifact AGENT_ISSUE_FORMAT asks for in an issue or PR body. Same run, same exit code; "
        "only the rendering differs",
    )
    args = parser.parse_args(argv)
    result = verify(
        args.worktree,
        base_ref=args.base_ref,
        test_cmd=args.test_cmd,
        test_paths=args.test_path or None,
        timeout=args.timeout,
    )
    if args.record_run_id:
        result["feedback"] = record_verdict(args.record_run_id, result)
    # THE EXIT CODE IS COMPUTED ONCE, ABOVE THE RENDERING CHOICE. `--transcript` changes what is
    # printed and nothing else: a flag that could move a gate's verdict would make the artifact and
    # the gate two different answers to the same question.
    if args.transcript:
        print(break_transcript(result), end="")  # already newline-terminated
    else:
        print(json.dumps(result, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
