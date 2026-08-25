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


def verify(
    worktree: str | Path,
    *,
    base_ref: str = "HEAD",
    test_cmd: str,
    test_paths: list[str] | None = None,
    timeout: int = 120,
) -> dict:
    wt = Path(worktree).resolve()
    tests = test_paths or _default_test_paths(wt, base_ref)
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
    return {
        "verdict": verdict,
        "ok": ok,
        "reason": reason,
        "worktree": str(wt),
        "base_ref": base_ref,
        "test_paths": tests,
        "copied_test_paths": copied,
        "green": green,
        "red": red,
        "node_verdict": nodes["verdict"],
        "hollow_nodes": nodes["hollow_nodes"],
        "node_analysis": nodes,
    }


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
        "hollow attribution w/ break->revert, named-prerequisite INDETERMINATE, feedback record)"
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
    print(json.dumps(result, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
