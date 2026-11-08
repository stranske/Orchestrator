"""Offline restart contracts for persisted supervisor closure operations."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

import dispatcher
import keepalive_supervisor as supervisor


class SupervisorRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(
            patch.dict(
                os.environ,
                ORCH_STATE_DIR=str(self.root),
                ORCH_LOCAL_RUNTIME=str(self.root),
            )
        )
        denied = Mock(side_effect=AssertionError("unexpected process, network, or agent call"))
        for module, name in (
            (subprocess, "run"),
            (subprocess, "Popen"),
            (socket, "socket"),
            (socket, "create_connection"),
            (os, "system"),
            (dispatcher, "offload"),
        ):
            self.stack.enter_context(patch.object(module, name, denied))
        self.addCleanup(denied.assert_not_called)
        self.sha = "a" * 40
        self.issue_target = "o/r#7"
        run_id = supervisor._closure_run_id(self.issue_target, self.sha)
        self.operation = {
            "issue": self.issue_target,
            "pr": "o/r#21",
            "merge_sha": self.sha,
            "run_id": run_id,
            "marker": f"<!-- supervisor-close/v1 {self.issue_target}@{self.sha} -->",
            "phase": "confirmed",
            "rule": "verifier PASS",
        }
        self.path = self.root / "supervisor-close-operations" / (run_id.split(":")[1] + ".json")
        self.path.parent.mkdir()
        self.issue = {"number": 7, "updated_at": "2026-11-08T08:00:00Z", "body": ""}
        self.gh = Mock(side_effect=self.read_github)
        self.record = Mock()
        self.lineage = Mock()
        self.unused = Mock(side_effect=AssertionError("recovery must not reevaluate closure"))
        self.addCleanup(self.unused.assert_not_called)

    def read_github(self, args):
        if args == ["api", "repos/o/r/issues/7"]:
            return dict(self.issue)
        if args == ["api", "repos/o/r/issues/7/comments", "--paginate", "--slurp"]:
            return [[{"body": self.operation["marker"]}]]
        self.fail(f"unexpected GitHub mutation or read: {args}")

    def close_issue(self):
        return supervisor._close_verified_issue(
            "o/r#21",
            "o/r",
            21,
            {"comments": {"nodes": []}},
            self.sha,
            "PASS",
            {"number": 7},
            gh=self.gh,
            pr_read=self.unused,
            ci_green=self.unused,
            gate=self.unused,
            record=self.record,
            holds=self.unused,
            root=self.root,
            lineage=self.lineage,
        )

    def test_mismatched_operation_identity_refuses_replay(self):
        self.path.write_text(json.dumps({**self.operation, "pr": "o/r#99"}))
        before = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, "closure operation identity mismatch"):
            self.close_issue()
        self.assertEqual(self.path.read_bytes(), before)
        self.gh.assert_called_once_with(["api", "repos/o/r/issues/7"])
        self.lineage.assert_not_called()
        self.record.assert_not_called()

    def test_confirmed_receipt_retries_without_reclosing_issue(self):
        self.path.write_text(json.dumps(self.operation))
        before = self.path.read_bytes()
        self.lineage.side_effect = RuntimeError("lineage unavailable")
        result = self.close_issue()
        self.assertEqual(
            result,
            {
                "target": self.issue_target,
                "pr": "o/r#21",
                "merge_sha": self.sha,
                "closed": False,
                "reason": "confirmed closure receipt pending",
                "outcome_error": "lineage unavailable",
            },
        )
        self.assertEqual(self.path.read_bytes(), before)
        self.record.assert_not_called()
        self.gh.assert_called_once_with(["api", "repos/o/r/issues/7"])

        self.lineage.side_effect = None
        result = self.close_issue()
        self.assertEqual(result["reason"], "confirmed closure receipt reconciled")
        self.assertFalse(result["closed"])
        self.assertNotIn("outcome_error", result)
        self.assertEqual(self.lineage.call_count, 2)
        self.record.assert_called_once_with(self.issue_target, self.sha, "verifier PASS")
        self.assertEqual(json.loads(self.path.read_text()), {**self.operation, "phase": "settled"})
        self.assertEqual(self.gh.call_count, 3)


if __name__ == "__main__":
    unittest.main()
