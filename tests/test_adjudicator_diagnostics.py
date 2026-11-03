"""Retained diagnostics must never become native grade or retry authority."""

import copy
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import adjudicator_retro as retro


def substantive_row():
    return {
        "case_id": "original-a",
        "target": "owner/repo#1",
        "decision": "reject_blocker",
        "later_truth": "PASS",
        "backend_run_id": "original-paid",
        "packet": {
            "ground_truth_evidence": {
                "diff_patch": {"text": "+inspected change", "revision": "evaluated"},
                "acceptance_criteria": [{"text": "Inspect evidence", "revision": "issue-rev"}],
            }
        },
        "proposal": {
            "decision": "reject_blocker",
            "confidence": "high",
            "rationale": "The supplied evidence contradicts the blocker.",
            "ground_truth_refs": ["git-path:proof.txt"],
            "recommended_next_step": "Inspect the proof before acting.",
            "evidence_gaps": [],
            "evidence_assessment": [
                {
                    "claim": "proof missing",
                    "status": "contradicted",
                    "evidence_ref": "git-path:proof.txt",
                    "reason": "Proof contents supplied.",
                }
            ],
        },
    }


class OriginalRowDiagnosticsTests(unittest.TestCase):
    def test_retained_first_breaks_follow_evidence_validation_decision_truth_order(self):
        variants = []
        row = substantive_row()
        row.pop("packet")
        variants.append((row, "saved_packet_unavailable"))
        row = substantive_row()
        row["packet"]["ground_truth_evidence"].pop("diff_patch")
        row["errors"] = ["later validator failure"]
        variants.append((row, "saved_substantive_evidence_missing"))
        row = substantive_row()
        row["errors"] = ["retained provider error"]
        variants.append((row, "saved_attempt_error"))
        row = substantive_row()
        row["proposal"]["recommended_next_step"] = "Merge this PR now"
        variants.append((row, "saved_proposal_validator_failure"))
        row = substantive_row()
        row.pop("proposal")
        row["decision"] = "needs_more_evidence"
        row["later_truth"] = None
        variants.append((row, "saved_adjudicator_abstention"))
        row = substantive_row()
        row.pop("proposal")
        row["decision"] = None
        variants.append((row, "saved_binary_decision_unavailable"))
        row = substantive_row()
        row["later_truth"] = None
        variants.append((row, "saved_independent_later_truth_unavailable"))
        variants.append((substantive_row(), "fresh_native_authentication_required"))
        for row, expected in variants:
            with self.subTest(expected=expected):
                before = copy.deepcopy(row)
                result = retro.diagnose_original_rows([row], [])[0]
                self.assertEqual(result["retained_first_break"], expected)
                self.assertEqual(result["source_row"], "rows[0]")
                self.assertFalse(result["dispatch_authorized"])
                self.assertNotIn("graded", result)
                self.assertEqual(row, before)

    def test_incomplete_and_unrevisioned_evidence_stays_missing(self):
        changes = [
            ("diff_patch", {"text": "+patch", "revision": "sha", "truncated": True}),
            ("diff_patch", {"text": "+patch"}),
            ("acceptance_criteria", []),
            ("acceptance_criteria", [{"text": "criterion without revision"}]),
        ]
        for name, value in changes:
            with self.subTest(name=name, value=value):
                row = substantive_row()
                row["packet"]["ground_truth_evidence"][name] = value
                result = retro.diagnose_original_rows([row], [])[0]
                self.assertEqual(
                    result["retained_first_break"], "saved_substantive_evidence_missing"
                )

    def test_unresolved_foreign_attempt_is_not_hidden_by_later_validated_state(self):
        attempts = [
            {
                "case_id": "original-a",
                "attempt_id": "uncertain",
                "state": "in_flight",
                "matches_original_source": False,
            },
            {
                "case_id": "original-a",
                "attempt_id": "completed",
                "state": "validated",
                "matches_original_source": True,
            },
        ]
        before = copy.deepcopy(attempts)
        result = retro.diagnose_original_rows([substantive_row()], attempts)[0]
        self.assertEqual(result["native_first_break"], "unresolved_native_attempt")
        self.assertEqual(
            result["unresolved_native_attempts"],
            [
                {
                    "attempt_id": "uncertain",
                    "state": "in_flight",
                    "matches_original_source": False,
                    "outcome": "UNKNOWN",
                }
            ],
        )
        self.assertEqual(attempts, before)

    def test_all_unresolved_states_have_specific_unknown_locators(self):
        attempts = [
            {
                "case_id": "original-a",
                "attempt_id": state,
                "state": state,
                "matches_original_source": True,
            }
            for state in ("reserved", "in_flight", "raw_persisted")
        ]
        result = retro.diagnose_original_rows([substantive_row()], attempts)[0]
        self.assertEqual(len(result["unresolved_native_attempts"]), 3)
        self.assertTrue(
            all(item["outcome"] == "UNKNOWN" for item in result["unresolved_native_attempts"])
        )

    def test_validated_journal_and_serialized_projection_cannot_supply_a_grade(self):
        row = substantive_row()
        row["native_projection"] = {"trusted": True, "graded": 1, "later_truth": "PASS"}
        attempts = [
            {
                "case_id": row["case_id"],
                "attempt_id": "serialized-state",
                "state": "validated",
                "matches_original_source": True,
            }
        ]
        result = retro.diagnose_original_rows([row], attempts)[0]
        self.assertEqual(
            result["native_first_break"], "fresh_native_receipt_authentication_required"
        )
        self.assertEqual(result["retained_first_break"], "fresh_native_authentication_required")
        self.assertNotIn("graded", result)
        self.assertIn("not fresh collection or truth", result["evidence_scope"])

    def test_malformed_or_mismatched_saved_proposals_have_explicit_breaks(self):
        for value in ([], "unparsed response", {"decision": ["reject_blocker"]}):
            with self.subTest(value=value):
                row = substantive_row()
                row["proposal"] = value
                result = retro.diagnose_original_rows([row], [])[0]
                self.assertEqual(
                    result["retained_first_break"], "saved_proposal_validator_failure"
                )
        row = substantive_row()
        row["decision"] = "uphold_blocker"
        result = retro.diagnose_original_rows([row], [])[0]
        self.assertEqual(result["retained_first_break"], "saved_decision_proposal_mismatch")

    def test_foreign_terminal_attempt_is_not_a_current_source_receipt(self):
        attempts = [
            {
                "case_id": "original-a",
                "attempt_id": "foreign",
                "state": "validated",
                "matches_original_source": False,
            }
        ]
        result = retro.diagnose_original_rows([substantive_row()], attempts)[0]
        self.assertEqual(
            result["native_first_break"], "foreign_source_native_attempt_requires_review"
        )
        self.assertFalse(result["dispatch_authorized"])

    def test_real_inventory_cli_emits_all_originals_without_writes_or_private_payloads(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = root / "original.json"
            canonical = root / "canonical.json"
            row = substantive_row()
            row["retry_attempts"] = [{"retry_attempts": [{"backend_run_id": "earlier-paid"}]}]
            other = {"case_id": "original-b", "decision": "needs_more_evidence"}
            original.write_text(json.dumps({"rows": [row, other]}))
            canonical.write_bytes(original.read_bytes())
            journal = retro.NativeAttemptJournal(root / "native.sqlite")
            digest = hashlib.sha256(original.read_bytes()).hexdigest()
            reserved = journal.reserve("original-b", "rev", "case-digest", digest)
            journal.persist_raw(
                reserved["attempt_id"], {"run_id": "native-paid", "output": "private payload"}
            )
            inputs = {
                "original_report": original,
                "canonical_path": canonical,
                "journal_path": journal.path,
            }
            before = {key: path.read_bytes() for key, path in inputs.items()}

            def forbidden(*_args, **_kwargs):
                self.fail("diagnostic inventory crossed an effect boundary")

            with (
                patch.object(retro.roles, "run_adjudicator_agent", forbidden),
                patch.object(retro, "native_later_truth", forbidden),
                patch.object(retro.feedback, "record_role_run", forbidden),
                patch.object(retro.NativeAttemptJournal, "__init__", forbidden),
            ):
                result = retro.inventory_native_state(**inputs)
            completed = subprocess.run(
                [
                    sys.executable,
                    str(Path(retro.__file__)),
                    "--inventory",
                    "--report",
                    str(original),
                    "--canonical-report",
                    str(canonical),
                    "--native-journal",
                    str(journal.path),
                ],
                capture_output=True,
                text=True,
                check=True,
                timeout=30,
            )
            cli = json.loads(completed.stdout)
            self.assertEqual(cli["row_diagnostics"], result["row_diagnostics"])
            self.assertEqual(
                [item["case_id"] for item in cli["row_diagnostics"]],
                ["original-a", "original-b"],
            )
            self.assertEqual(cli["paid_identity_summary"]["union_unique"], 3)
            self.assertEqual(cli["row_diagnostics"][0]["retained_decision"], "reject_blocker")
            self.assertEqual(
                cli["row_diagnostics"][1]["native_first_break"], "unresolved_native_attempt"
            )
            self.assertNotIn("private payload", completed.stdout)
            self.assertEqual(before, {key: path.read_bytes() for key, path in inputs.items()})


if __name__ == "__main__":
    unittest.main()
