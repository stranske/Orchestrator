"""Post-merge rechecks consume real local_verify verdicts through the shared runner."""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import unittest
import venv
import zipfile
from contextlib import contextmanager
from importlib import metadata
from pathlib import Path
from unittest.mock import patch

import backlog
import durability_sweep
import feedback
import provision
import runtime_ac
import runtime_ac_gate as gate


class PostMergeBreakTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        root = Path(scratch.name).resolve()
        self.root = root
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
        self.enterContext(patch.object(feedback, "DB_PATH", root / "brain.db"))
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

    def merged_spec(self, *, delivered, hollow=False):
        # These verdict/delivery fixtures reuse pytest's installed environment;
        # environment provisioning has its own isolated tests below.
        (self.checkout / ".venv").symlink_to(sys.prefix, target_is_directory=True)
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
        return spec, head

    def recheck(self, *, delivered, hollow=False):
        spec, head = self.merged_spec(delivered=delivered, hollow=hollow)
        original = json.dumps(spec, sort_keys=True)
        result = gate.recheck_merged(spec, self.checkout)
        self.assertEqual(json.dumps(spec, sort_keys=True), original)
        self.assertEqual(self.git("rev-parse", "HEAD"), head)
        run = json.loads(result["transcript"])
        self.assertEqual(len(run["check_results"]), 1)
        proof = json.loads(run["check_results"][0]["stdout_tail"])
        self.assertEqual(proof["base_ref"], "HEAD^")
        self.assertEqual(Path(proof["worktree"]).resolve(), self.checkout.resolve())
        self.assertEqual(shlex.split(proof["green"]["cmd"])[0], result["interpreter"])
        return result, proof

    def sweep_case(
        self,
        *,
        delivered=False,
        hollow=False,
        reopen=True,
        fail_label=False,
        fail_comment_after_post=False,
        fail_reopen=False,
        issue_already_open=False,
        merge_ci_green=False,
    ):
        spec, head = self.merged_spec(delivered=delivered, hollow=hollow)
        target = "owner/repo#2"
        spec["verification"]["target"] = target
        root = self.root / "specs"
        root.mkdir()
        gate.spec_path(target, spec_dir=root).write_text(json.dumps(spec))
        self.enterContext(
            patch.dict(
                os.environ,
                ORCH_RUN_RUNTIME_AC="1",
                ORCH_RUNTIME_AC_REOPEN="1" if reopen else "0",
            )
        )
        self.enterContext(patch.object(backlog, "SUPPORTED_REPOS", ["owner/repo"]))
        self.enterContext(
            patch.object(
                durability_sweep,
                "_merged_verifier_candidates",
                return_value={("owner/repo", 2): {"fixture"}},
            )
        )
        provisions, calls, comments = [], [], []

        @contextmanager
        def provider(actual_target, actual_head):
            self.assertEqual((actual_target, actual_head), (target, head))
            provisions.append(actual_head)
            yield self.checkout

        self.enterContext(patch.object(provision, "temporary_exact_head_worktree", provider))

        def gh(args):
            calls.append(args)
            if args[1:3] == ["api", "graphql"]:
                return {
                    "data": {
                        "repository": {
                            "object": {
                                "oid": head,
                                "statusCheckRollup": {
                                    "state": "SUCCESS" if merge_ci_green else "FAILURE"
                                },
                            }
                        }
                    }
                }, None
            if args[1:3] == ["pr", "view"]:
                return {
                    "state": "MERGED",
                    "mergeCommit": {"oid": head},
                    "closingIssuesReferences": [{"number": 1}],
                }, None
            if "--slurp" in args:
                return [comments], None
            if args == ["gh", "api", "repos/owner/repo/issues/1"]:
                return ({"state": "open"} if issue_already_open else {"state": "closed"}), None
            if "POST" in args and args[4].endswith("/comments"):
                comments.append({"body": args[-1].removeprefix("body=")})
                if fail_comment_after_post and len(comments) == 1:
                    return None, "comment response lost after posting"
            if "PATCH" in args and fail_reopen:
                if sum("PATCH" in call for call in calls) == 1:
                    return None, "temporary reopen API failure"
            if "labels[]=runtime-ac:reopened" in args and fail_label:
                if sum("labels[]=runtime-ac:reopened" in call for call in calls) == 1:
                    return None, "temporary label API failure"
            return {}, None

        def run():
            return durability_sweep._post_merge_rechecks(
                dry_run=False, gh=gh, now=1792022400, spec_dir=root
            )

        return run, calls, comments, provisions, head

    def post_merge_payloads(self):
        with feedback._conn() as conn:
            rows = conn.execute(
                "SELECT payload_json FROM completion_events "
                "WHERE producer='runtime_ac_gate' AND phase='post_merge_recheck'"
            ).fetchall()
        return [
            payload["runtime_ac_post_merge"]
            for row in rows
            if isinstance((payload := json.loads(row[0])).get("runtime_ac_post_merge"), dict)
        ]

    def test_invalid_persisted_rows_recover_without_repeating_valid_delivery(self):
        payloads = [
            "{",
            "{}",
            '{"rejection_codes":["invalid"]}',
            '{"runtime_ac_post_merge":true}',
            '{"runtime_ac_post_merge":{}}',
            '{"runtime_ac_post_merge":{"result":"PASS","delivery_complete":true}}',
        ]
        for payload in payloads:
            with self.subTest(payload=payload):
                case = type(self)()
                case.setUp()
                try:
                    with patch.object(
                        gate,
                        "recheck_merged",
                        return_value={
                            "result": "UNRUNNABLE",
                            "transcript": "isolated recovery control",
                        },
                    ):
                        run, calls, comments, provisions, head = case.sweep_case()
                        self.assertEqual(run(), {"rechecked": 1, "errors": []})
                        with feedback._conn() as conn:
                            conn.execute(
                                "UPDATE completion_events SET payload_json=? "
                                "WHERE producer='runtime_ac_gate' AND phase='post_merge_recheck'",
                                (payload,),
                            )
                        self.assertEqual(run(), {"rechecked": 1, "errors": []})
                        self.assertEqual(provisions, [head, head])
                        self.assertTrue(case.post_merge_payloads()[0]["delivery_complete"])
                        self.assertEqual(run(), {"rechecked": 0, "errors": []})
                        self.assertEqual(provisions, [head, head])
                        self.assertEqual(len(comments), 1)
                finally:
                    case.doCleanups()

    def test_real_broken_merge_comments_reopens_and_delivers_once(self):
        run, calls, comments, provisions, head = self.sweep_case()
        self.assertEqual(run(), {"rechecked": 1, "errors": []})
        payloads = self.post_merge_payloads()
        self.assertEqual(len(payloads), 1)
        payload = payloads[0]
        self.assertEqual(payload["result"], "FAIL")
        self.assertTrue(payload["reopened"] and payload["delivery_complete"])
        self.assertEqual(payload["interpreter"], str(self.checkout / ".venv/bin/python"))
        self.assertEqual(len(comments), 1)
        self.assertTrue(comments[0]["body"].startswith(f"runtime-AC post-merge FAIL on {head}"))
        transcript = Path(payload["transcript_path"]).read_text()
        self.assertIn("FAIL_BROKEN", transcript)
        self.assertIn(transcript, comments[0]["body"])
        self.assertEqual(run(), {"rechecked": 0, "errors": []})
        self.assertEqual(self.post_merge_payloads(), payloads)
        self.assertEqual(provisions, [head])
        self.assertEqual(len(comments), 1)
        self.assertEqual(sum("PATCH" in call for call in calls), 1)
        self.assertEqual(sum("labels[]=runtime-ac:reopened" in call for call in calls), 1)

    def test_real_broken_merge_with_reopen_off_only_comments(self):
        run, calls, comments, provisions, head = self.sweep_case(reopen=False)
        self.assertEqual(run(), {"rechecked": 1, "errors": []})
        self.assertEqual(run(), {"rechecked": 0, "errors": []})
        self.assertEqual(len(comments), 1)
        self.assertTrue(comments[0]["body"].startswith(f"runtime-AC post-merge FAIL on {head}"))
        self.assertEqual(provisions, [head])
        self.assertFalse(any("PATCH" in call for call in calls))
        self.assertFalse(any("labels[]=runtime-ac:reopened" in call for call in calls))
        payloads = self.post_merge_payloads()
        self.assertEqual(len(payloads), 1)
        self.assertEqual(payloads[0]["result"], "FAIL")
        self.assertFalse(payloads[0]["reopened"])

    def test_real_hollow_break_comments_once_and_never_reopens(self):
        run, calls, comments, provisions, head = self.sweep_case(delivered=True, hollow=True)
        self.assertEqual(run(), {"rechecked": 1, "errors": []})
        self.assertEqual(run(), {"rechecked": 0, "errors": []})
        self.assertEqual(len(comments), 1)
        self.assertTrue(
            comments[0]["body"].startswith(f"runtime-AC post-merge UNRUNNABLE on {head}")
        )
        self.assertIn("FAIL_HOLLOW", comments[0]["body"])
        self.assertEqual(provisions, [head])
        self.assertFalse(any("PATCH" in call for call in calls))
        self.assertFalse(any("labels[]=runtime-ac:reopened" in call for call in calls))
        payloads = self.post_merge_payloads()
        self.assertEqual(len(payloads), 1)
        self.assertEqual(payloads[0]["result"], "UNRUNNABLE")
        self.assertFalse(payloads[0]["reopened"])

    def test_completed_receipt_rejects_tampered_transcript(self):
        run, calls, comments, provisions, head = self.sweep_case(delivered=True)
        self.assertEqual(run(), {"rechecked": 1, "errors": []})
        payload = self.post_merge_payloads()[-1]
        Path(payload["transcript_path"]).write_text("CORRUPTED transcript")
        writes = [call for call in calls if "POST" in call or "PATCH" in call]
        second = run()
        self.assertEqual(second["rechecked"], 0)
        self.assertIn("post-merge transcript hash mismatch", second["errors"][0])
        self.assertEqual(provisions, [head])
        self.assertEqual([call for call in calls if "POST" in call or "PATCH" in call], writes)

    def test_completed_receipt_rejects_redirected_transcript(self):
        run, calls, comments, provisions, head = self.sweep_case(delivered=True)
        self.assertEqual(run(), {"rechecked": 1, "errors": []})
        with feedback._conn() as conn:
            event_id, raw = conn.execute(
                "SELECT event_id,payload_json FROM completion_events "
                "WHERE producer='runtime_ac_gate' AND phase='post_merge_recheck'"
            ).fetchone()
            payload = json.loads(raw)
            receipt = payload["runtime_ac_post_merge"]
            redirect = self.root / "redirect.txt"
            redirect.write_bytes(Path(receipt["transcript_path"]).read_bytes())
            receipt["transcript_path"] = str(redirect)
            conn.execute(
                "UPDATE completion_events SET payload_json=? WHERE event_id=?",
                (json.dumps(payload), event_id),
            )
        self.assertIn("not the canonical artifact", run()["errors"][0])
        self.assertEqual(provisions, [head])

    def test_partial_receipt_rejects_missing_transcript_before_delivery(self):
        run, calls, comments, provisions, head = self.sweep_case(fail_reopen=True)
        self.assertEqual(run()["rechecked"], 1)
        Path(self.post_merge_payloads()[0]["transcript_path"]).unlink()
        writes = [call for call in calls if "POST" in call or "PATCH" in call]
        self.assertTrue(run()["errors"])
        self.assertFalse(self.post_merge_payloads()[0]["delivery_complete"])
        self.assertEqual(provisions, [head])
        self.assertEqual([call for call in calls if "POST" in call or "PATCH" in call], writes)

    def test_real_break_label_retry_preserves_execution_comment_and_reopen(self):
        run, calls, comments, provisions, head = self.sweep_case(fail_label=True)
        first = run()
        self.assertEqual(first["rechecked"], 1)
        self.assertIn("temporary label API failure", first["errors"][0])
        payloads = self.post_merge_payloads()
        self.assertEqual(len(payloads), 1)
        self.assertTrue(payloads[0]["commented"] and payloads[0]["reopened"])
        self.assertFalse(payloads[0]["delivery_complete"])
        self.assertEqual(run(), {"rechecked": 0, "errors": []})
        self.assertTrue(self.post_merge_payloads()[0]["delivery_complete"])
        self.assertEqual(run(), {"rechecked": 0, "errors": []})
        self.assertEqual(provisions, [head])
        self.assertEqual(len(comments), 1)
        self.assertEqual(sum("PATCH" in call for call in calls), 1)
        self.assertEqual(sum("labels[]=runtime-ac:reopened" in call for call in calls), 2)

    def test_real_break_lost_comment_response_reuses_remote_marker(self):
        run, calls, comments, provisions, head = self.sweep_case(fail_comment_after_post=True)
        first = run()
        self.assertEqual(first["rechecked"], 1)
        self.assertIn("comment response lost after posting", first["errors"][0])
        payloads = self.post_merge_payloads()
        self.assertEqual(len(payloads), 1)
        initial = payloads[0]
        self.assertEqual(initial["result"], "FAIL")
        self.assertFalse(
            initial["commented"] or initial["reopened"] or initial["delivery_complete"]
        )
        self.assertEqual(len(comments), 1)
        self.assertTrue(comments[0]["body"].startswith(f"runtime-AC post-merge FAIL on {head}"))
        self.assertIn(Path(initial["transcript_path"]).read_text(), comments[0]["body"])
        self.assertFalse(any("PATCH" in call for call in calls))

        self.assertEqual(run(), {"rechecked": 0, "errors": []})
        final = self.post_merge_payloads()
        self.assertEqual(len(final), 1)
        self.assertTrue(final[0]["commented"] and final[0]["reopened"])
        self.assertTrue(final[0]["delivery_complete"])
        self.assertEqual(final[0]["transcript_hash"], initial["transcript_hash"])
        self.assertEqual(final[0]["merge_sha"], head)
        self.assertEqual(provisions, [head])
        self.assertEqual(len(comments), 1)
        self.assertEqual(sum("PATCH" in call for call in calls), 1)
        self.assertEqual(sum("labels[]=runtime-ac:reopened" in call for call in calls), 1)
        writes = [call for call in calls if "POST" in call or "PATCH" in call]
        self.assertEqual(run(), {"rechecked": 0, "errors": []})
        self.assertEqual(self.post_merge_payloads(), final)
        self.assertEqual([call for call in calls if "POST" in call or "PATCH" in call], writes)

    def test_real_break_reopen_retry_preserves_execution_and_comment(self):
        run, calls, comments, provisions, head = self.sweep_case(fail_reopen=True)
        first = run()
        self.assertEqual(first["rechecked"], 1)
        self.assertIn("temporary reopen API failure", first["errors"][0])
        payloads = self.post_merge_payloads()
        self.assertEqual(len(payloads), 1)
        initial = payloads[0]
        self.assertEqual(initial["result"], "FAIL")
        self.assertTrue(initial["commented"])
        self.assertFalse(initial["reopened"] or initial["delivery_complete"])
        self.assertFalse(any("labels[]=runtime-ac:reopened" in call for call in calls))

        self.assertEqual(run(), {"rechecked": 0, "errors": []})
        final = self.post_merge_payloads()
        self.assertEqual(len(final), 1)
        self.assertTrue(final[0]["commented"] and final[0]["reopened"])
        self.assertTrue(final[0]["delivery_complete"])
        self.assertEqual(final[0]["transcript_hash"], initial["transcript_hash"])
        self.assertEqual(final[0]["merge_sha"], head)
        self.assertEqual(provisions, [head])
        self.assertEqual(len(comments), 1)
        self.assertEqual(sum("PATCH" in call for call in calls), 2)
        self.assertEqual(sum("labels[]=runtime-ac:reopened" in call for call in calls), 1)
        writes = [call for call in calls if "POST" in call or "PATCH" in call]
        self.assertEqual(run(), {"rechecked": 0, "errors": []})
        self.assertEqual(self.post_merge_payloads(), final)
        self.assertEqual([call for call in calls if "POST" in call or "PATCH" in call], writes)

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
                with (
                    patch.object(runtime_ac, "run_verification", return_value=run),
                    patch.object(gate, "_worktree_interpreter", return_value=sys.executable),
                ):
                    result = gate.recheck_merged(spec, self.checkout)
                self.assertEqual(result["result"], "UNRUNNABLE")

    def test_spawned_interpreter_failure_is_not_a_delivery_fail(self):
        spec = runtime_ac.author_issue_spec(
            "owner/repo#1", "## Acceptance Criteria\n- Inspect the delivered behavior.\n"
        )
        run = {
            "gate": {"verdict": "FAIL"},
            "check_results": [
                {
                    "type": "command",
                    "status": "FAIL",
                    "returncode": 1,
                    "stderr_tail": (
                        "2 failed\nsubprocess.CalledProcessError: Command "
                        "['/opt/anaconda3/bin/python3', '-c', 'import package']' returned non-zero"
                    ),
                }
            ],
        }
        with (
            patch.object(runtime_ac, "run_verification", return_value=run),
            patch.object(gate, "_worktree_interpreter", return_value="/opt/anaconda3/bin/python3"),
        ):
            result = gate.recheck_merged(spec, self.checkout, merge_ci_green=True)
        self.assertEqual(result["result"], "UNRUNNABLE")
        self.assertEqual(result["reason"], "environment_mismatch")
        self.assertEqual(result["interpreter"], "/opt/anaconda3/bin/python3")

    def test_worktree_interpreter_bootstraps_repository_and_test_dependencies(self):
        (self.checkout / "pyproject.toml").write_text(
            '[project]\nname = "delivery"\nversion = "1"\n'
            '[project.optional-dependencies]\ntest = ["pytest"]\ndev = []\n'
        )
        (self.checkout / "requirements.txt").touch()
        interpreter = str(self.checkout / ".venv/bin/python")
        with (
            patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run,
            patch.dict(os.environ, PYTHONPATH="/scheduler/src", PYTHONHOME="/scheduler/python"),
        ):
            self.assertEqual(gate._worktree_interpreter(self.checkout), interpreter)
        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual(commands[0], [sys.executable, "-m", "venv", str(self.checkout / ".venv")])
        self.assertEqual(
            commands[1],
            [
                interpreter,
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "pytest",
                "-r",
                str(self.checkout / "requirements.txt"),
                ".[dev,test]",
            ],
        )
        for call in run.call_args_list:
            self.assertEqual(call.kwargs["cwd"], str(self.checkout))
            self.assertNotIn("PYTHONPATH", call.kwargs["env"])
            self.assertNotIn("PYTHONHOME", call.kwargs["env"])
        install_env = run.call_args_list[1].kwargs["env"]
        self.assertEqual(install_env["PATH"].split(os.pathsep)[0], str(Path(interpreter).parent))

    def test_failed_environment_setup_never_runs_checks_or_leaves_partial_venv(self):
        spec = runtime_ac.author_issue_spec(
            "owner/repo#1", "## Acceptance Criteria\n- Named test: `tests/test_delivery.py`.\n"
        )

        for stage in ("venv", "pip"):
            for failure in ("exit", "timeout", "os_error"):
                with self.subTest(stage=stage, failure=failure):
                    checkout = self.checkout / f"{stage}-{failure}"
                    checkout.mkdir()
                    environment = checkout / ".venv"
                    interpreter = environment / "bin/python"

                    def setup(command, **kwargs):
                        interpreter.parent.mkdir(parents=True, exist_ok=True)
                        interpreter.touch()
                        if stage in command:
                            if failure == "timeout":
                                raise subprocess.TimeoutExpired(command, kwargs["timeout"])
                            if failure == "os_error":
                                raise OSError("environment setup interrupted")
                            return subprocess.CompletedProcess(command, 1)
                        return subprocess.CompletedProcess(command, 0)

                    with (
                        patch.object(subprocess, "run", side_effect=setup),
                        patch.object(runtime_ac, "run_verification") as verify,
                    ):
                        result = gate.recheck_merged(spec, checkout)
                    self.assertEqual(
                        (result["result"], result["reason"]), ("UNRUNNABLE", "tool_error")
                    )
                    self.assertIsNone(result["interpreter"])
                    verify.assert_not_called()
                    self.assertFalse(environment.exists())

                    # Retry must provision from scratch rather than selecting a
                    # partially installed interpreter or falling back to the scheduler.
                    verification = {
                        "gate": {"verdict": "PASS"},
                        "check_results": [{"type": "command", "status": "PASS"}],
                    }
                    with (
                        patch.object(
                            subprocess, "run", return_value=subprocess.CompletedProcess([], 0)
                        ) as retry,
                        patch.object(
                            runtime_ac, "run_verification", return_value=verification
                        ) as verify,
                    ):
                        result = gate.recheck_merged(spec, checkout)
                    self.assertEqual(result["result"], "PASS", result)
                    self.assertEqual(result["interpreter"], str(interpreter))
                    self.assertEqual(
                        [call.args[0][:3] for call in retry.call_args_list],
                        [[sys.executable, "-m", "venv"], [str(interpreter), "-m", "pip"]],
                    )
                    context = verify.call_args.args[0]["runtime_context"]
                    self.assertEqual(context["python_interpreter"], str(interpreter))
                    self.assertEqual(context["worktree"], str(checkout))

    def test_runner_error_preserves_selected_worktree_interpreter(self):
        spec = runtime_ac.author_issue_spec(
            "owner/repo#1", "## Acceptance Criteria\n- Named test: `tests/test_delivery.py`.\n"
        )
        interpreter = str(self.checkout / ".venv/bin/python")
        with (
            patch.object(gate, "_worktree_interpreter", return_value=interpreter),
            patch.object(runtime_ac, "run_verification", side_effect=RuntimeError("runner failed")),
        ):
            result = gate.recheck_merged(spec, self.checkout)
        self.assertEqual((result["result"], result["reason"]), ("UNRUNNABLE", "tool_error"))
        self.assertEqual(result["interpreter"], interpreter)
        self.assertIn("runner failed", result["transcript"])

    def test_fresh_worktree_installs_repository_and_runs_pytest_offline(self):
        # Repackage the test runner's installed pytest and its dependencies so the
        # real provisioning path can run without network access or a shared venv.
        wheelhouse = self.root / "wheels"
        wheelhouse.mkdir()
        pending = ["pytest"]
        copied = set()
        while pending:
            name = pending.pop()
            try:
                distribution = metadata.distribution(name)
            except metadata.PackageNotFoundError:
                continue
            name = distribution.metadata["Name"]
            if name in copied:
                continue
            copied.add(name)
            for requirement in distribution.requires or []:
                # This fixture requests no extras. Do not copy optional test/doc
                # dependencies just because they happen to be installed on the host.
                # pip still selects applicable platform requirements from the wheels.
                if re.search(r"\bextra\b", requirement.partition(";")[2]):
                    continue
                match = re.match(r"[A-Za-z0-9][A-Za-z0-9._-]*", requirement)
                if match:
                    pending.append(match.group())
            tag = next(
                line.removeprefix("Tag: ")
                for line in distribution.read_text("WHEEL").splitlines()
                if line.startswith("Tag: ")
            )
            filename = f"{name.replace('-', '_')}-{distribution.version}-{tag}.whl"
            with zipfile.ZipFile(wheelhouse / filename, "w") as archive:
                for entry in distribution.files:
                    if ".." not in entry.parts and entry.suffix != ".pyc":
                        archive.write(distribution.locate_file(entry), str(entry))

        package = self.checkout / "src/runtime_delivery"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("DELIVERED = True\n")
        (self.checkout / "pyproject.toml").write_text(
            '[project]\nname = "runtime-delivery"\nversion = "1.0"\n'
            '[build-system]\nrequires = []\nbuild-backend = "delivery_backend"\n'
            'backend-path = ["."]\n'
        )
        # A stdlib-only backend installs the actual checkout's source, avoiding a
        # dependency on setuptools being present in the host or the new venv.
        build_child = f"import sys; assert sys.prefix == {str(self.checkout / '.venv')!r}"
        (self.checkout / "delivery_backend.py").write_text(
            "from pathlib import Path\nimport subprocess, sys, zipfile\n\n"
            "def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):\n"
            "    for launcher in (sys.executable, 'python', 'python3'):\n"
            f"        subprocess.run([launcher, '-c', {build_child!r}], check=True)\n"
            "    filename = 'runtime_delivery-1.0-py3-none-any.whl'\n"
            "    with zipfile.ZipFile(Path(wheel_directory) / filename, 'w') as archive:\n"
            "        archive.write('src/runtime_delivery/__init__.py', "
            "'runtime_delivery/__init__.py')\n"
            "        archive.writestr('runtime_delivery-1.0.dist-info/METADATA', "
            "'Metadata-Version: 2.1\\nName: runtime-delivery\\nVersion: 1.0\\n')\n"
            "        archive.writestr('runtime_delivery-1.0.dist-info/WHEEL', "
            "'Wheel-Version: 1.0\\nRoot-Is-Purelib: true\\nTag: py3-none-any\\n')\n"
            "        archive.writestr('runtime_delivery-1.0.dist-info/RECORD', '')\n"
            "    return filename\n"
        )
        interpreter = str(self.checkout / ".venv/bin/python")
        tests = self.checkout / "tests"
        tests.mkdir()
        child = (
            "import sys, runtime_delivery; from pathlib import Path; "
            f"assert str(Path(sys.executable).parent) == {str(Path(interpreter).parent)!r}; "
            f"assert sys.prefix == {str(self.checkout / '.venv')!r}; "
            f"assert runtime_delivery.__file__.startswith({str(self.checkout / '.venv')!r}); "
            "assert runtime_delivery.DELIVERED"
        )
        (tests / "test_delivery.py").write_text(
            "import subprocess, sys\nimport pytest\n\n"
            "@pytest.mark.parametrize('launcher', [sys.executable, 'python3'])\n"
            "def test_installed_delivery(launcher):\n"
            f"    assert sys.executable == {interpreter!r}\n"
            f"    subprocess.run([launcher, '-c', {child!r}], "
            f"cwd={str(self.root)!r}, check=True)\n"
        )
        scheduler = self.root / "scheduler"
        scheduler_bin = scheduler / "bin"
        scheduler_bin.mkdir(parents=True)
        scheduler_python = scheduler_bin / "python3"
        scheduler_python.write_text("#!/bin/sh\nexit 93\n")
        scheduler_python.chmod(0o755)
        scheduler_source = scheduler / "src"
        scheduler_source.mkdir()
        (scheduler_source / "runtime_delivery.py").write_text("DELIVERED = False\n")
        spec = runtime_ac.author_issue_spec(
            "owner/repo#1", "## Acceptance Criteria\n- Named test: `tests/test_delivery.py`.\n"
        )
        self.assertFalse((self.checkout / ".venv").exists())
        with patch.dict(
            os.environ,
            PIP_NO_INDEX="1",
            PIP_FIND_LINKS=str(wheelhouse),
            PATH=str(scheduler_bin) + os.pathsep + os.environ.get("PATH", ""),
            PYTHONPATH=str(scheduler_source),
            PYTHONHOME=str(scheduler / "unavailable-python"),
            VIRTUAL_ENV=str(scheduler),
        ):
            result = gate.recheck_merged(spec, self.checkout)
        self.assertEqual(result["result"], "PASS", result)
        self.assertEqual(result["interpreter"], interpreter)
        row = json.loads(result["transcript"])["check_results"][0]
        self.assertEqual(row["interpreter"], interpreter)
        self.assertEqual(shlex.split(row["command"])[0], interpreter)
        self.assertIn("2 passed", row["stdout_tail"])

    def test_tool_only_pyproject_does_not_require_a_distribution(self):
        (self.checkout / "pyproject.toml").write_text("[tool.black]\nline-length = 100\n")
        with patch.object(
            subprocess, "run", return_value=subprocess.CompletedProcess([], 0)
        ) as run:
            interpreter = gate._worktree_interpreter(self.checkout)
        self.assertEqual(
            run.call_args.args[0],
            [interpreter, "-m", "pip", "install", "--disable-pip-version-check", "pytest"],
        )

    def test_merge_ci_must_belong_to_the_merge_commit(self):
        sha = "a" * 40
        for oid, state, expected in (
            (sha, "SUCCESS", True),
            (sha, "PENDING", False),
            (sha, "FAILURE", False),
            ("b" * 40, "SUCCESS", False),
        ):
            with self.subTest(oid=oid, state=state):
                response = {
                    "data": {
                        "repository": {
                            "object": {
                                "oid": oid,
                                "statusCheckRollup": {"state": state},
                            }
                        }
                    }
                }
                self.assertEqual(
                    durability_sweep._merged_ci_green(
                        "owner/repo", sha, lambda _: (response, None)
                    ),
                    expected,
                )
        self.assertFalse(durability_sweep._merged_ci_green("owner/repo", sha, lambda _: ({}, None)))

    def test_python_and_pytest_launchers_use_worktree_environment_without_coverage(self):
        interpreter = str(self.checkout / ".venv/bin/python")
        for launcher in ("python3", "/scheduler/bin/python3.12", "pytest", "/scheduler/bin/pytest"):
            with self.subTest(launcher=launcher):
                args = [launcher]
                if "python" in launcher:
                    args.extend(["-m", "pytest"])
                args.extend(["tests", "--cov=delivery", "--cov-report", "term"])
                check = {"id": "AC1", "type": "command", "command": shlex.join(args)}
                with (
                    patch.object(
                        subprocess,
                        "run",
                        return_value=subprocess.CompletedProcess([], 0, "1 passed", ""),
                    ) as run,
                    patch.dict(
                        os.environ, PYTHONPATH="/scheduler/src", PYTEST_ADDOPTS="--cov=delivery -q"
                    ),
                ):
                    result = runtime_ac._run_command_check(
                        check, cwd=self.checkout, timeout=10, python_interpreter=interpreter
                    )
                self.assertEqual(result["status"], "PASS")
                self.assertEqual(
                    run.call_args.args[0],
                    [interpreter, "-m", "pytest", "-p", "no:cov", "-o", "addopts=", "tests"],
                )
                env = run.call_args.kwargs["env"]
                self.assertEqual(env["PATH"].split(os.pathsep)[0], str(Path(interpreter).parent))
                self.assertEqual(env["VIRTUAL_ENV"], str(self.checkout / ".venv"))
                self.assertEqual(env["PYTHONPATH"], os.pathsep.join(("src", ".")))
                self.assertEqual(env["PYTEST_ADDOPTS"], "-q")

    def test_recheck_uses_installed_repository_for_child_process_outside_worktree(self):
        environment = self.checkout / ".venv"
        venv.EnvBuilder(with_pip=True, symlinks=True).create(environment)
        interpreter = str(environment / "bin/python")
        # Build a local repository wheel without a build backend or network access.
        wheel = self.root / "runtime_delivery-1.0-py3-none-any.whl"
        metadata = "runtime_delivery-1.0.dist-info"
        with zipfile.ZipFile(wheel, "w") as archive:
            archive.writestr("runtime_delivery/__init__.py", "DELIVERED = True\n")
            archive.writestr(
                f"{metadata}/METADATA",
                "Metadata-Version: 2.1\nName: runtime_delivery\nVersion: 1.0\n",
            )
            archive.writestr(
                f"{metadata}/WHEEL",
                "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
            )
            archive.writestr(f"{metadata}/RECORD", "")
        subprocess.run(
            [interpreter, "-m", "pip", "install", "--no-index", str(wheel)],
            check=True,
            capture_output=True,
            text=True,
        )
        child = (
            "import json, sys, runtime_delivery; "
            "print(json.dumps([sys.executable, sys.prefix, runtime_delivery.__file__, "
            "runtime_delivery.DELIVERED]))"
        )
        spec = runtime_ac.author_issue_spec(
            "owner/repo#1", "## Acceptance Criteria\n- Named test: `tests/test_delivery.py`.\n"
        )
        spec["acceptance_criteria"][0]["checks"][0]["command"] = shlex.join(
            ["python3", "check_child.py"]
        )
        spec["acceptance_criteria"][0]["evidence_required"] = ["command_output"]
        for launcher in ("sys.executable", "python", "python3"):
            with self.subTest(launcher=launcher):
                child_launcher = (
                    "sys.executable" if launcher == "sys.executable" else repr(launcher)
                )
                parent = (
                    "import subprocess, sys; "
                    f"subprocess.run([{child_launcher}, '-c', {child!r}], "
                    f"cwd={str(self.root)!r}, check=True)"
                )
                (self.checkout / "check_child.py").write_text(parent)
                # By-name children must find the repository's interpreter even
                # when the scheduler has supplied its own active environment.
                with patch.dict(
                    os.environ,
                    PYTHONPATH="/scheduler/src",
                    VIRTUAL_ENV=sys.prefix,
                    PATH=str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"],
                ):
                    result = gate.recheck_merged(spec, self.checkout)
                self.assertEqual(result["result"], "PASS", result)
                self.assertEqual(result["interpreter"], interpreter)
                run = json.loads(result["transcript"])
                row = run["check_results"][0]
                self.assertEqual(row["interpreter"], interpreter)
                executable, prefix, package_path, delivered = json.loads(row["stdout_tail"])
                expected = (
                    interpreter
                    if launcher == "sys.executable"
                    else str(environment / "bin" / launcher)
                )
                self.assertEqual(executable, expected)
                self.assertEqual(prefix, str(environment))
                self.assertTrue(Path(package_path).is_relative_to(environment))
                self.assertTrue(delivered)

    def test_worktree_interpreter_preserves_repository_environment(self):
        environment = self.checkout / ".venv"
        (environment / "bin").mkdir(parents=True)
        interpreter = environment / "bin" / "python"
        interpreter.touch()
        self.assertEqual(gate._worktree_interpreter(self.checkout), str(interpreter))
        interpreter.unlink()
        with self.assertRaisesRegex(RuntimeError, "selected repository interpreter is unavailable"):
            gate._worktree_interpreter(self.checkout)

    def test_genuine_assertion_wins_over_spawned_interpreter_error(self):
        spec = runtime_ac.author_issue_spec(
            "owner/repo#1", "## Acceptance Criteria\n- Inspect the delivered behavior.\n"
        )
        run = {
            "gate": {"verdict": "FAIL"},
            "check_results": [
                {
                    "type": "command",
                    "status": "FAIL",
                    "returncode": 1,
                    "stderr_tail": (
                        "2 failed\nAssertionError: delivered behavior is wrong\n"
                        "subprocess.CalledProcessError: Command ['/opt/anaconda3/bin/python3']"
                    ),
                }
            ],
        }
        with (
            patch.object(runtime_ac, "run_verification", return_value=run),
            patch.object(gate, "_worktree_interpreter", return_value=sys.executable),
        ):
            result = gate.recheck_merged(spec, self.checkout, merge_ci_green=True)
        self.assertEqual(result["result"], "FAIL")

    def test_environment_classification_preserves_assertion_failures(self):
        spec = runtime_ac.author_issue_spec(
            "owner/repo#1", "## Acceptance Criteria\n- Inspect the delivered behavior.\n"
        )
        environment = {
            "type": "command",
            "status": "FAIL",
            "returncode": 1,
            "stderr_tail": "2 failed\nsubprocess.CalledProcessError: Command ['/other/python3']",
        }
        assertion = {
            "type": "command",
            "status": "FAIL",
            "returncode": 1,
            "stdout_tail": "1 failed\nAssertionError: delivery is wrong",
        }
        deliberate = {
            "type": "deliberate_break",
            "status": "FAIL",
            "stdout_tail": json.dumps({"verdict": "FAIL_BROKEN", "green": environment}),
        }
        child_import = {
            **environment,
            "stderr_tail": "2 failed\nsubprocess.run([sys.executable, '-c', ...])\nModuleNotFoundError: missing dependency",
        }
        direct_import = {**environment, "stderr_tail": "2 failed\nModuleNotFoundError: product bug"}
        for rows, green, expected in (
            ([environment], False, "FAIL"),
            ([environment, assertion], True, "FAIL"),
            ([deliberate], True, "UNRUNNABLE"),
            ([child_import], True, "UNRUNNABLE"),
            ([direct_import], True, "FAIL"),
        ):
            with (
                self.subTest(rows=rows, green=green),
                patch.object(gate, "_worktree_interpreter", return_value="/repo/.venv/bin/python"),
                patch.object(
                    runtime_ac,
                    "run_verification",
                    return_value={
                        "gate": {"verdict": "FAIL"},
                        "check_results": rows,
                    },
                ),
            ):
                result = gate.recheck_merged(spec, self.checkout, merge_ci_green=green)
            self.assertEqual(result["result"], expected)
            if expected == "UNRUNNABLE":
                self.assertEqual(result["reason"], "environment_mismatch")

    def test_already_open_issue_is_not_counted_as_reopened(self):
        run, calls, _comments, _provisions, _head = self.sweep_case(issue_already_open=True)
        summary = run()
        self.assertEqual(summary["errors"], [])
        self.assertIn(["gh", "api", "repos/owner/repo/issues/1"], calls)
        self.assertFalse(any("PATCH" in call for call in calls))
        self.assertFalse(any("labels[]=runtime-ac:reopened" in call for call in calls))
        payload = self.post_merge_payloads()[0]
        self.assertFalse(payload["reopened"])
        self.assertTrue(payload["already_open"])
        self.assertEqual(gate.post_merge_summary()["reopened"], 0)
        self.assertIn("reopened 0", gate.format_post_merge_summary(gate.post_merge_summary()))

    def test_environment_mismatch_never_reopens_with_green_merge_ci(self):
        run, calls, comments, _provisions, head = self.sweep_case(merge_ci_green=True)
        verification = {
            "gate": {"verdict": "FAIL"},
            "check_results": [
                {
                    "type": "command",
                    "status": "FAIL",
                    "returncode": 1,
                    "stderr_tail": "2 failed\nsubprocess.CalledProcessError: Command ['/other/python3']",
                }
            ],
        }
        with (
            patch.object(runtime_ac, "run_verification", return_value=verification),
            patch.object(gate, "_worktree_interpreter", return_value="/repo/.venv/bin/python"),
        ):
            self.assertEqual(run(), {"rechecked": 1, "errors": []})
        payload = self.post_merge_payloads()[0]
        self.assertEqual(
            (payload["result"], payload["reason"]), ("UNRUNNABLE", "environment_mismatch")
        )
        self.assertFalse(payload["reopened"])
        self.assertTrue(payload["delivery_complete"])
        self.assertEqual(comments, [])
        self.assertFalse(any("PATCH" in call for call in calls))
        query = next(call for call in calls if call[1:3] == ["api", "graphql"])
        self.assertIn("sha=" + head, query)
