#!/usr/bin/env python3
"""local_verify.py - local deliberate-break verifier for delegated work.

The gate is intentionally simple:
1. Run the target test command in the live worktree. It must pass.
2. Build a temporary copy of the base ref, overlay only the candidate test files,
   and run the same command. It must fail. If it still passes, the tests are
   likely hollow or not coupled to the implementation.

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

    if red["ok"]:
        verdict = "FAIL_HOLLOW"
        ok = False
        reason = "candidate tests still pass against the base implementation"
    else:
        verdict = "PASS"
        ok = True
        reason = "candidate tests pass live and fail against the base implementation"
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
    }


def record_verdict(run_id: str, result: dict) -> dict:
    """Patch the feedback outcome row with this verifier verdict."""
    verdict = result.get("verdict") or "ERROR"
    reason = result.get("reason") or result.get("error") or "local verifier completed"
    feedback.record_outcome(
        run_id,
        verifier_verdict=verdict,
        notes=f"local_verify: {verdict} - {reason}",
    )
    feedback.record_completion_event(
        run_id,
        event_type="verification",
        phase="verification",
        producer="local_verify",
        status=verdict,
        payload={
            "test_ids": result.get("test_paths") or [],
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
    return {"run_id": run_id, "verifier_verdict": verdict, "recorded": True}


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

        hollow = _init_repo(Path(tmp) / "hollow", correct_impl=True, test_body=hollow_test)
        out = verify(
            hollow,
            base_ref="HEAD",
            test_cmd=cmd,
            test_paths=["tests/test_math_utils.py"],
            timeout=30,
        )
        assert out["verdict"] == "FAIL_HOLLOW", out

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
            ver = feedback.relearn({"verifytest": {"cursor": 0.5}})
            learned = feedback.current_weights("verifytest", ver)[0]
            assert learned["posterior"] < 0.5, learned
        finally:
            feedback.DB_PATH = old_db

    print(
        "local_verify.py selftest: OK (green/red deliberate-break PASS, hollow, broken, feedback record)"
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
