"""Post-merge rechecks consume real local_verify verdicts through the shared runner."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import runtime_ac
import runtime_ac_gate as gate


class PostMergeBreakTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        root = Path(scratch.name)
        self.checkout = root / "checkout"
        self.checkout.mkdir()
        env = os.environ.copy()
        for key in (
            "GIT_DIR",
            "GIT_WORK_TREE",
            "GIT_COMMON_DIR",
            "GIT_INDEX_FILE",
            "GIT_OBJECT_DIRECTORY",
            "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        ):
            env.pop(key, None)
        env.update(
            ORCH_STATE_DIR=str(root / "state"),
            ORCH_LOCAL_RUNTIME=str(root / "runtime"),
            ORCH_FEEDBACK_DB=str(root / "brain.db"),
            ORCH_CAPABILITIES_PATH=str(root / "ledger.jsonl"),
            ORCH_RUNTIME_AC_ALLOW_COMMANDS="1",
            ORCH_WORKTREE_GIT_REFUSAL_LOG=str(root / "refusals.jsonl"),
        )
        self.enterContext(patch.dict(os.environ, env, clear=True))
        self.git("init", "-q")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.invalid")
        (self.checkout / "behavior.py").write_text("DELIVERED = False\n")
        self.git("add", "behavior.py")
        self.git("commit", "-qm", "before delivery")

    def git(self, *args):
        return subprocess.check_output(
            ["git", "-C", str(self.checkout), *args], text=True, stderr=subprocess.PIPE
        ).strip()

    def recheck(self, *, delivered, hollow=False):
        (self.checkout / "behavior.py").write_text(f"DELIVERED = {delivered!r}\n")
        tests = self.checkout / "tests"
        tests.mkdir()
        assertion = "True" if hollow else "DELIVERED"
        (tests / "test_delivered.py").write_text(
            "import unittest\nfrom behavior import DELIVERED\n\n"
            "class Delivery(unittest.TestCase):\n"
            f"    def test_delivered(self):\n        self.assertTrue({assertion})\n"
        )
        self.git("add", "behavior.py", "tests")
        self.git("commit", "-qm", "merged delivery")
        head = self.git("rev-parse", "HEAD")
        spec = runtime_ac.author_issue_spec(
            "owner/repo#1",
            "## Acceptance Criteria\n- Named test: `tests/test_delivered.py`.\n",
        )
        # An operator-authored break check can carry its own named test command.
        criterion = spec["acceptance_criteria"][0]
        criterion["evidence_required"] = ["test_output", "deliberate_break"]
        criterion["checks"] = [
            {
                "id": "AC1-BREAK",
                "name": "Delivered behavior distinguishes the merge from its parent",
                "type": "deliberate_break",
                "test_cmd": f"{shlex.quote(sys.executable)} -m unittest discover -s tests",
                "test_paths": ["tests/test_delivered.py"],
                "base_ref": "unavailable-authored-base",
                "worktree": "/unavailable-authored-checkout",
            }
        ]
        self.assertEqual(runtime_ac.validate_spec(spec), [])
        original = json.dumps(spec, sort_keys=True)
        result = gate.recheck_merged(spec, self.checkout)
        self.assertEqual(json.dumps(spec, sort_keys=True), original)
        self.assertEqual(self.git("rev-parse", "HEAD"), head)
        run = json.loads(result["transcript"])
        self.assertEqual(len(run["check_results"]), 1)
        proof = json.loads(run["check_results"][0]["stdout_tail"])
        self.assertEqual(proof["base_ref"], "HEAD^")
        self.assertEqual(proof["worktree"], str(self.checkout))
        return result, proof

    def test_broken_merge_is_fail_from_real_deliberate_break(self):
        result, proof = self.recheck(delivered=False)
        self.assertEqual(proof["verdict"], "FAIL_BROKEN")
        self.assertIn("AssertionError", proof["green"]["stderr_tail"])
        self.assertEqual(result["result"], "FAIL")

    def test_delivered_merge_passes_real_parent_comparison(self):
        result, proof = self.recheck(delivered=True)
        self.assertEqual(proof["verdict"], "PASS")
        self.assertEqual(proof["green"]["returncode"], 0)
        self.assertEqual(proof["red"]["returncode"], 1)
        self.assertEqual(result["result"], "PASS")

    def test_hollow_break_stays_unrunnable(self):
        result, proof = self.recheck(delivered=True, hollow=True)
        self.assertEqual(proof["verdict"], "FAIL_HOLLOW")
        self.assertEqual(proof["green"]["returncode"], 0)
        self.assertEqual(proof["red"]["returncode"], 0)
        self.assertEqual(result["result"], "UNRUNNABLE")

    def test_break_tool_failures_never_count_as_assertion_failures(self):
        spec = runtime_ac.author_issue_spec(
            "owner/repo#1", "## Acceptance Criteria\n- Inspect the delivered behavior.\n"
        )
        for verdict, error, code, output in (
            ("ERROR", "base extraction failed", 0, "1 passed"),
            ("FAIL_BROKEN", None, 2, "ERROR collecting tests: ImportError"),
            ("FAIL_BROKEN", None, 4, "ERROR: not found: tests/missing.py"),
            ("FAIL_BROKEN", None, 5, "no tests ran"),
            ("FAIL_BROKEN", None, 1, "No module named pytest"),
            ("FAIL_BROKEN", None, 1, "FAILED (errors=1)"),
        ):
            with self.subTest(verdict=verdict, code=code, output=output):
                run = {
                    "gate": {"verdict": "FAIL"},
                    "check_results": [
                        {
                            "type": "deliberate_break",
                            "status": "FAIL",
                            "stdout_tail": json.dumps(
                                {
                                    "verdict": verdict,
                                    "error": error,
                                    "green": {"returncode": code, "stdout_tail": output},
                                }
                            ),
                        }
                    ],
                }
                with patch.object(runtime_ac, "run_verification", return_value=run):
                    result = gate.recheck_merged(spec, self.checkout)
                self.assertEqual(result["result"], "UNRUNNABLE")
