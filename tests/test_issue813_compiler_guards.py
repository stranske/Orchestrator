"""Offline rejection tests for issue 813's first ranked coverage target.

These unittest cases are also collected by pytest. Keeping the runner dependency
optional lets a minimal checkout verify the compiler's pure validation paths.
"""

from __future__ import annotations

import os
import socket
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import adapters
import capability_compiler as compiler


class TestWorkflowCompilerGuards(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="issue813-")))
        self.enterContext(
            patch.dict(
                os.environ,
                {"ORCH_STATE_DIR": str(self.root), "ORCH_LOCAL_RUNTIME": str(self.root)},
            )
        )
        for owner, name in (
            (subprocess, "run"),
            (subprocess, "Popen"),
            (socket, "create_connection"),
            (socket, "socket"),
            (adapters, "dispatch"),
        ):
            boundary = self.enterContext(
                patch.object(owner, name, side_effect=AssertionError("unexpected live boundary"))
            )
            self.addCleanup(boundary.assert_not_called)
        self.source = compiler.reference_workflow_source()

    def assert_rejected(self, reason):
        with self.assertRaises(compiler.WorkflowCompileError) as caught:
            compiler.compile_workflow_rail(self.source)
        self.assertIn(reason, caught.exception.reasons)

    def test_selector_requires_exact_fields(self):
        self.source["selector"]["fallback"] = "all"
        self.assert_rejected("selector must contain field, operator, and value")

    def test_selector_rejects_judgment_operator(self):
        self.source["selector"]["operator"] = "review"
        self.assert_rejected("ambiguous judgment: selector")

    def test_workflow_requires_at_least_one_step(self):
        self.source["steps"] = []
        self.assert_rejected("workflow requires steps")

    def test_steps_must_be_objects(self):
        self.source["steps"].append("sync")
        self.assert_rejected("steps[4] must be an object")

    def test_steps_reject_unknown_fields(self):
        self.source["steps"][0]["fallback"] = "all"
        self.assert_rejected("steps[0] has unsupported fields: ['fallback']")

    def test_step_version_must_be_positive(self):
        self.source["steps"][0]["version"] = 0
        self.assert_rejected("steps[0].version must be a positive integer")

    def test_step_cannot_override_entrypoint_side_effect_policy(self):
        self.source["steps"][0]["side_effect_policy"] = "read_only"
        self.assert_rejected("side-effect policy mismatch: sync")

    def test_retry_requires_exact_fields(self):
        self.source["steps"][0]["retry"] = {"max_attempts": 1}
        self.assert_rejected("steps[0].retry must contain max_attempts and backoff_seconds")

    def test_retry_backoff_is_bounded_and_typed(self):
        for backoff in (-1, 301, "0"):
            with self.subTest(backoff=backoff):
                self.source["steps"][0]["retry"] = {
                    "max_attempts": 1,
                    "backoff_seconds": backoff,
                }
                self.assert_rejected("steps[0].retry.backoff_seconds must be between 0 and 300")

    def test_rollback_must_be_allowed_for_the_forward_entrypoint(self):
        self.source["steps"][0]["rollback"] = {"entrypoint": "workflow.noop", "inputs": {}}
        self.assert_rejected("invalid rollback: sync")

    def test_step_keys_cannot_be_reused(self):
        self.source["steps"][1]["idempotency_key"] = self.source["steps"][0]["idempotency_key"]
        self.assert_rejected("duplicate idempotency key")

    def test_barrier_condition_must_be_allowlisted(self):
        self.source["barriers"][0]["condition_id"] = "human_decision"
        self.assert_rejected("unsupported barrier condition: human_decision")

    def test_existing_active_capability_cannot_adopt_a_different_plan(self):
        plan = compiler.compile_workflow_rail(self.source)
        existing = {
            plan["capability_id"]: {
                "status": "active",
                "activation_evidence": {"workflow_plan_id": "previous-plan"},
            }
        }
        with (
            patch.object(compiler.capabilities, "load", return_value=existing) as load,
            patch.object(compiler.capabilities, "save") as save,
        ):
            with self.assertRaisesRegex(ValueError, "non-shadow capability cannot adopt"):
                compiler._register_shadow(plan, self.root / "ledger.json")
        load.assert_called_once_with(self.root / "ledger.json", create=False)
        save.assert_not_called()
        self.assertEqual(
            existing[plan["capability_id"]]["activation_evidence"]["workflow_plan_id"],
            "previous-plan",
        )

    def test_failed_probe_is_retried_even_when_reference_matches(self):
        ledger = self.root / "ledger.json"
        existing = {
            "capability:test": {
                "activation_evidence": {"producer_probe": {"passed": False, "ref": "result-1"}}
            }
        }
        with (
            patch.object(compiler.capabilities, "load", return_value=existing) as load,
            patch.object(compiler.capabilities, "record_probe") as record,
        ):
            compiler._probe_once("capability:test", "producer_probe", "result-1", ledger)
        load.assert_called_once_with(ledger, create=False)
        record.assert_called_once_with(
            "capability:test", "producer_probe", passed=True, ref="result-1", path=ledger
        )
