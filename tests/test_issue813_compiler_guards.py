"""Offline rejection tests for issue 813's first ranked coverage target.

These unittest cases are also collected by pytest. Keeping the runner dependency
optional lets a minimal checkout verify the compiler's pure validation paths.
"""

from __future__ import annotations

import copy
import io
import os
import socket
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import call, patch

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

    def test_successful_probe_with_same_reference_is_not_recorded_again(self):
        ledger = self.root / "ledger.json"
        existing = {
            "capability:test": {
                "activation_evidence": {"producer_probe": {"passed": True, "ref": "result-1"}}
            }
        }
        with (
            patch.object(compiler.capabilities, "load", return_value=existing) as load,
            patch.object(compiler.capabilities, "record_probe") as record,
        ):
            compiler._probe_once("capability:test", "producer_probe", "result-1", ledger)
        load.assert_called_once_with(ledger, create=False)
        record.assert_not_called()

    def test_successful_probe_for_an_old_reference_does_not_suppress_new_evidence(self):
        ledger = self.root / "ledger.json"
        existing = {
            "capability:test": {
                "activation_evidence": {"producer_probe": {"passed": True, "ref": "result-1"}}
            }
        }
        with (
            patch.object(compiler.capabilities, "load", return_value=existing) as load,
            patch.object(compiler.capabilities, "record_probe") as record,
        ):
            compiler._probe_once("capability:test", "producer_probe", "result-2", ledger)
        load.assert_called_once_with(ledger, create=False)
        record.assert_called_once_with(
            "capability:test", "producer_probe", passed=True, ref="result-2", path=ledger
        )

    def test_existing_active_capability_with_same_plan_is_not_modified(self):
        ledger = self.root / "ledger.json"
        plan = compiler.compile_workflow_rail(self.source)
        existing = {
            plan["capability_id"]: {
                "status": "active",
                "activation_evidence": {"workflow_plan_id": plan["plan_id"]},
            }
        }
        before = copy.deepcopy(existing)
        with (
            patch.object(compiler.capabilities, "load", return_value=existing) as load,
            patch.object(compiler.capabilities, "save") as save,
            patch.object(compiler.capabilities, "register") as register,
            patch.object(compiler.capabilities, "validate_capability") as validate,
        ):
            compiler._register_shadow(plan, ledger)
        load.assert_called_once_with(ledger, create=False)
        save.assert_not_called()
        register.assert_not_called()
        validate.assert_not_called()
        self.assertEqual(existing, before)

    def test_shadow_plan_replacement_preserves_unique_history_and_updates_contract(self):
        ledger = self.root / "ledger.json"
        plan = compiler.compile_workflow_rail(self.source)
        for prior_id, history, expected_history in (
            ("previous-plan", ["older-plan"], ["older-plan", "previous-plan"]),
            ("previous-plan", ["previous-plan"], ["previous-plan"]),
            (None, ["older-plan"], ["older-plan"]),
        ):
            with self.subTest(prior_id=prior_id, history=history):
                earlier_event = {"type": "registered", "timestamp": 10}
                cap = {
                    "status": "shadow",
                    "activation_evidence": {
                        "workflow_plan_id": prior_id,
                        "workflow_plan_previous_ids": history,
                    },
                    "matcher": {"field": "repository", "operator": "equals", "value": "old/repo"},
                    "rollback": {"steps": []},
                    "event_history": [earlier_event],
                }
                existing = {plan["capability_id"]: cap}
                with (
                    patch.object(compiler.capabilities, "load", return_value=existing) as load,
                    patch.object(compiler.capabilities, "save") as save,
                    patch.object(compiler.capabilities, "register") as register,
                    patch.object(compiler.capabilities, "validate_capability") as validate,
                    patch.object(compiler.time, "time", return_value=1234),
                ):
                    compiler._register_shadow(plan, ledger)
                load.assert_called_once_with(ledger, create=False)
                validate.assert_called_once_with(cap)
                save.assert_called_once_with(existing, ledger)
                register.assert_not_called()
                self.assertEqual(cap["status"], "shadow")
                self.assertEqual(cap["activation_evidence"]["workflow_plan_id"], plan["plan_id"])
                self.assertEqual(
                    cap["activation_evidence"]["workflow_plan_previous_ids"], expected_history
                )
                self.assertEqual(cap["matcher"], plan["selector"])
                self.assertEqual(cap["rollback"], {"steps": plan["rollback_order"]})
                self.assertEqual(
                    cap["event_history"],
                    [
                        earlier_event,
                        {
                            "timestamp": 1234,
                            "type": "workflow_plan_extended",
                            "from": prior_id,
                            "to": plan["plan_id"],
                            "reason": "typed consumer-sync shadow step added",
                        },
                    ],
                )

    def test_reference_cli_without_ledger_rejects_before_running_workflow(self):
        error = io.StringIO()
        with (
            patch.object(compiler, "run_reference_workflow") as run,
            redirect_stderr(error),
        ):
            with self.assertRaises(SystemExit) as caught:
                compiler.main(["reference"])
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("reference requires --ledger", error.getvalue())
        run.assert_not_called()

    def test_reference_workflow_records_receipt_lineage_and_all_four_probes(self):
        ledger = self.root / "ledger.json"
        with (
            patch.object(compiler, "_register_shadow") as register,
            patch.object(compiler.capabilities, "heartbeat") as heartbeat,
            patch.object(compiler, "_probe_once") as probe,
        ):
            output = compiler.run_reference_workflow(ledger_path=ledger)
        plan = output["plan"]
        result = output["result"]
        receipt = output["consumer_receipt"]
        capability_id = plan["capability_id"]
        prefix = plan["plan_id"]
        register.assert_called_once_with(plan, ledger)
        self.assertEqual(result["side_effects"], [])
        self.assertTrue(receipt["consumed"])
        self.assertEqual(receipt["result_id"], result["result_id"])
        expected_events = [
            ("match", prefix),
            ("invocation", prefix),
            ("output", result["result_id"]),
            ("consumer", receipt["receipt_id"]),
            ("success", receipt["receipt_id"]),
            ("outcome", f"shadow:{receipt['receipt_id']}"),
        ]
        self.assertEqual(
            heartbeat.call_args_list,
            [
                call(
                    capability_id,
                    event,
                    ref=ref,
                    path=ledger,
                    idempotency_key=f"{prefix}:{event}",
                )
                for event, ref in expected_events
            ],
        )
        self.assertEqual(
            probe.call_args_list,
            [
                call(capability_id, "producer_probe", result["result_id"], ledger),
                call(capability_id, "consumer_probe", receipt["receipt_id"], ledger),
                call(capability_id, "outcome_probe", f"shadow:{receipt['receipt_id']}", ledger),
                call(
                    capability_id,
                    "rollback_probe",
                    compiler.stable_hash("workflow-rollback", plan["rollback_order"]),
                    ledger,
                ),
            ],
        )

    def test_dry_run_failure_records_no_output_success_or_probes(self):
        ledger = self.root / "ledger.json"
        failure = RuntimeError("bounded dry-run fixture failure")
        plan = compiler.compile_workflow_rail(self.source)
        with (
            patch.object(compiler, "_register_shadow"),
            patch.object(compiler, "dry_run_workflow_rail", side_effect=failure),
            patch.object(compiler.capabilities, "heartbeat") as heartbeat,
            patch.object(compiler, "_probe_once") as probe,
        ):
            with self.assertRaises(RuntimeError) as caught:
                compiler.run_reference_workflow(ledger_path=ledger)
        self.assertIs(caught.exception, failure)
        self.assertEqual(
            [entry.args[1] for entry in heartbeat.call_args_list],
            ["match", "invocation", "failure"],
        )
        error_ref = compiler.stable_hash(
            "workflow-shadow-failure",
            {"plan_id": plan["plan_id"], "error_type": "RuntimeError"},
        )
        self.assertEqual(
            heartbeat.call_args_list[-1],
            call(
                plan["capability_id"],
                "failure",
                ref=error_ref,
                path=ledger,
                idempotency_key=f"{plan['plan_id']}:failure:{error_ref}",
            ),
        )
        probe.assert_not_called()

    def test_consumer_failure_keeps_output_but_records_no_success_or_probes(self):
        ledger = self.root / "ledger.json"
        failure = ValueError("bounded consumer fixture failure")
        with (
            patch.object(compiler, "_register_shadow"),
            patch.object(compiler.capabilities, "heartbeat") as heartbeat,
            patch.object(compiler, "_probe_once") as probe,
            patch.object(compiler, "consume_workflow_output", side_effect=failure) as consumer,
        ):
            with self.assertRaises(ValueError) as caught:
                compiler.run_reference_workflow(ledger_path=ledger, consumer=consumer)
        self.assertIs(caught.exception, failure)
        self.assertEqual(
            [entry.args[1] for entry in heartbeat.call_args_list],
            ["match", "invocation", "output", "failure"],
        )
        consumer.assert_called_once()
        result = consumer.call_args.args[0]
        self.assertEqual(result["status"], "shadow_pass")
        self.assertEqual(result["side_effects"], [])
        self.assertEqual(heartbeat.call_args_list[2].kwargs["ref"], result["result_id"])
        self.assertNotIn(str(failure), str(heartbeat.call_args_list))
        probe.assert_not_called()
