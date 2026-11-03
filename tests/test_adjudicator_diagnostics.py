"""Retained diagnostics must never become native grade or retry authority."""

import copy
import hashlib
import json
import os
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
                self.assertEqual(result["retained_first_break"], "saved_proposal_validator_failure")
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


class InventoryCodeStabilityTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        original = self.root / "original.json"
        canonical = self.root / "canonical.json"
        original.write_text(json.dumps({"rows": [substantive_row()]}))
        canonical.write_bytes(original.read_bytes())
        journal = retro.NativeAttemptJournal(self.root / "native.sqlite")
        self.inputs = {
            "original_report": original,
            "canonical_path": canonical,
            "journal_path": journal.path,
        }
        self.before = {key: path.read_bytes() for key, path in self.inputs.items()}
        self.deployed = self.root / "deployed"
        self.mirror = self.root / "mirror"
        for location in (self.deployed, self.mirror):
            location.mkdir()
            (location / "adjudicator_retro.py").write_text("original code")

    def inventory(self):
        return retro.inventory_native_state(
            **self.inputs, deployed_root=self.deployed, mirror_root=self.mirror
        )

    def assert_rejected_change(self, change):
        observe = retro._inventory_code
        changed = False

        def observe_then_change(root):
            nonlocal changed
            result = observe(root)
            if root == self.mirror and not changed:
                changed = True
                change()
            return result

        with patch.object(retro, "_inventory_code", observe_then_change):
            with self.assertRaisesRegex(ValueError, "runtime code.*changed"):
                self.inventory()
        self.assertTrue(changed)
        self.assertEqual(self.before, {key: path.read_bytes() for key, path in self.inputs.items()})

    def test_inventory_rejects_deployed_or_mirror_script_change(self):
        for location in (self.deployed, self.mirror):
            with self.subTest(location=location.name):
                code = location / "adjudicator_retro.py"
                self.assert_rejected_change(lambda: code.write_text("replacement code"))
                code.write_text("original code")

    def test_inventory_rejects_flat_mirror_dependency_change(self):
        dependency = self.mirror / "roles.py"
        dependency.write_text("original validator")
        self.assert_rejected_change(lambda: dependency.write_text("changed validator"))

    def test_inventory_rejects_dependency_change_in_already_dirty_checkout(self):
        self.init_checkout()
        dependency = self.deployed / "roles.py"
        dependency.write_text("committed validator")
        self.git("add", "roles.py")
        self.git("commit", "-qm", "validator deployment")
        dependency.write_text("first local validator")
        self.assertFalse(retro._inventory_code(self.deployed)["tracked_files_clean"])
        self.assert_rejected_change(lambda: dependency.write_text("second local validator"))
        self.assertFalse(retro._inventory_code(self.deployed)["tracked_files_clean"])

    def test_inventory_rejects_flat_mirror_module_addition_or_removal(self):
        dependency = self.mirror / "roles.py"
        self.assert_rejected_change(lambda: dependency.write_text("new validator"))
        self.assert_rejected_change(dependency.unlink)

    def test_runtime_module_hashes_pin_bytes_and_names_in_both_layouts(self):
        for layout in ("flat", "src"):
            with self.subTest(layout=layout):
                root = self.root / layout
                modules = root if layout == "flat" else root / "src"
                modules.mkdir(parents=True)
                expected = {}
                for name, content in (
                    ("roles.py", b"validator bytes"),
                    ("adjudicator_retro.py", b"retrospective bytes"),
                    ("feedback.py", b"feedback bytes"),
                ):
                    (modules / name).write_bytes(content)
                    expected[name] = hashlib.sha256(content).hexdigest()
                (modules / "observations.json").write_text("not runtime code")
                observed = retro._inventory_code(root)
                self.assertEqual(observed["runtime_module_sha256"], expected)
                self.assertEqual(list(observed["runtime_module_sha256"]), sorted(expected))
                self.assertEqual(observed["adjudicator_sha256"], expected["adjudicator_retro.py"])
                self.assertEqual(observed["live_reader_binding"], "unverified")

    def test_inventory_rejects_mirror_generation_switch_with_identical_code(self):
        generation = self.root / "generation-a"
        self.mirror.rename(generation)
        self.mirror.symlink_to(generation, target_is_directory=True)
        replacement = self.root / "generation-b"
        replacement.mkdir()
        (replacement / "adjudicator_retro.py").write_text("original code")

        def switch_generation():
            self.mirror.unlink()
            self.mirror.symlink_to(replacement, target_is_directory=True)

        self.assert_rejected_change(switch_generation)

    def git(self, *args):
        return subprocess.run(
            [
                "git",
                "-C",
                str(self.deployed),
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.invalid",
                *args,
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )

    def init_checkout(self):
        self.git("init", "-q")
        self.git("add", "adjudicator_retro.py")
        self.git("commit", "-qm", "initial deployment")

    def test_inventory_rejects_revision_change_with_identical_script(self):
        self.init_checkout()
        self.assert_rejected_change(
            lambda: self.git("commit", "--allow-empty", "-qm", "new deployment")
        )

    def test_inventory_rejects_tracked_dirty_status_change(self):
        self.init_checkout()
        config = self.deployed / "deployment.json"
        config.write_text("{}")
        self.git("add", "deployment.json")
        self.git("commit", "-qm", "deployment configuration")
        self.assert_rejected_change(lambda: config.write_text('{"changed": true}'))

    def test_inventory_rejects_disappearing_code_location(self):
        self.assert_rejected_change(lambda: self.mirror.rename(self.root / "removed-mirror"))

    def test_stable_code_inventory_preserves_inputs_and_remains_observational(self):
        self.init_checkout()
        result = self.inventory()
        self.assertEqual(
            result["deployed_code"]["git_revision"], self.git("rev-parse", "HEAD").stdout.strip()
        )
        self.assertTrue(result["deployed_code"]["tracked_files_clean"])
        for key in ("deployed_code", "mirror_code"):
            self.assertEqual(
                result[key]["adjudicator_sha256"], hashlib.sha256(b"original code").hexdigest()
            )
            self.assertEqual(result[key]["live_reader_binding"], "unverified")
        self.assertFalse(result["dispatch_authorized"])
        self.assertEqual(self.before, {key: path.read_bytes() for key, path in self.inputs.items()})

    def test_inherited_git_locations_cannot_redirect_revision_or_dirty_status(self):
        self.init_checkout()
        foreign = self.root / "foreign"
        self.git("clone", "-q", str(self.deployed), str(foreign))
        (self.deployed / "adjudicator_retro.py").write_text("current deployment")
        self.git("commit", "-qam", "current deployment")
        expected_head = self.git("rev-parse", "HEAD").stdout.strip()
        foreign_head = self.git("-C", str(foreign), "rev-parse", "HEAD").stdout.strip()
        self.assertNotEqual(expected_head, foreign_head)
        environments = [
            {"GIT_DIR": str(foreign / ".git"), "GIT_WORK_TREE": str(foreign)},
            {"GIT_WORK_TREE": str(foreign)},
            {"GIT_INDEX_FILE": str(foreign / ".git/index")},
            {"GIT_COMMON_DIR": str(foreign / ".git")},
        ]
        for environment in environments:
            with self.subTest(environment=environment):
                before = dict(os.environ)
                with patch.dict(os.environ, environment):
                    result = self.inventory()["deployed_code"]
                    self.assertEqual(result["git_revision"], expected_head)
                    self.assertTrue(result["tracked_files_clean"])
                    self.assertEqual(
                        result["adjudicator_sha256"],
                        hashlib.sha256(b"current deployment").hexdigest(),
                    )
                    for name, value in environment.items():
                        self.assertEqual(os.environ[name], value)
                self.assertEqual(dict(os.environ), before)
        self.assertEqual(self.before, {key: path.read_bytes() for key, path in self.inputs.items()})

    def test_git_worktree_inventory_uses_its_own_metadata_under_foreign_environment(self):
        self.init_checkout()
        worktree = self.root / "deployed-worktree"
        self.git("worktree", "add", "-q", "-b", "deployment", str(worktree))
        self.git("commit", "--allow-empty", "-qm", "checkout only")
        expected_head = self.git("-C", str(worktree), "rev-parse", "HEAD").stdout.strip()
        (worktree / "adjudicator_retro.py").write_text("dirty worktree code")
        self.assertTrue((worktree / ".git").is_file())
        self.mirror = worktree
        with patch.dict(
            os.environ,
            {"GIT_DIR": str(self.deployed / ".git"), "GIT_WORK_TREE": str(self.deployed)},
        ):
            result = self.inventory()["mirror_code"]
        self.assertEqual(result["git_revision"], expected_head)
        self.assertFalse(result["tracked_files_clean"])
        self.assertEqual(
            result["adjudicator_sha256"], hashlib.sha256(b"dirty worktree code").hexdigest()
        )


class InventoryReportStabilityTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        original = self.root / "original.json"
        canonical = self.root / "canonical.json"
        original.write_text(json.dumps({"rows": [substantive_row()]}))
        canonical.write_bytes(original.read_bytes())
        journal = retro.NativeAttemptJournal(self.root / "native.sqlite")
        self.inputs = {
            "original_report": original,
            "canonical_path": canonical,
            "journal_path": journal.path,
        }

    def assert_rejected_change(self, change):
        observe = retro._inventory_code
        changed = False

        def observe_then_change(root):
            nonlocal changed
            result = observe(root)
            if not changed:
                changed = True
                change()
            return result

        with patch.object(retro, "_inventory_code", observe_then_change):
            with self.assertRaisesRegex(ValueError, "report location changed"):
                retro.inventory_native_state(**self.inputs)
        self.assertTrue(changed)

    def test_inventory_rejects_identical_report_replacement(self):
        for name in ("original_report", "canonical_path"):
            with self.subTest(report=name):
                report = self.inputs[name]
                replacement = self.root / "replacement.json"
                replacement.write_bytes(report.read_bytes())
                self.assert_rejected_change(lambda: replacement.replace(report))

    def test_inventory_rejects_report_symlink_switch_with_identical_bytes(self):
        for name in ("original_report", "canonical_path"):
            with self.subTest(report=name):
                report = self.inputs[name]
                link = self.root / (name + "-link.json")
                link.symlink_to(report)
                self.inputs[name] = link
                replacement = self.root / (name + "-replacement.json")
                replacement.write_bytes(report.read_bytes())

                def switch():
                    link.unlink()
                    link.symlink_to(replacement)

                self.assert_rejected_change(switch)

    def test_inventory_rejects_disappearing_report(self):
        for name in ("original_report", "canonical_path"):
            with self.subTest(report=name):
                report = self.inputs[name]
                contents = report.read_bytes()
                try:
                    self.assert_rejected_change(report.unlink)
                finally:
                    report.write_bytes(contents)

    def test_stable_report_symlinks_pin_resolved_locations_without_writes(self):
        before = {name: path.read_bytes() for name, path in self.inputs.items()}
        targets = {}
        for name in ("original_report", "canonical_path"):
            targets[name] = self.inputs[name]
            link = self.root / (name + "-link.json")
            link.symlink_to(targets[name])
            self.inputs[name] = link
        result = retro.inventory_native_state(**self.inputs)
        self.assertEqual(result["original_report"]["path"], str(targets["original_report"]))
        self.assertEqual(result["canonical_report"]["path"], str(targets["canonical_path"]))
        self.assertEqual(before, {name: path.read_bytes() for name, path in self.inputs.items()})
        self.assertFalse(result["dispatch_authorized"])


class InventoryJournalStabilityTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        original = self.root / "original.json"
        canonical = self.root / "canonical.json"
        original.write_text(json.dumps({"rows": [substantive_row()]}))
        canonical.write_bytes(original.read_bytes())
        journal = retro.NativeAttemptJournal(self.root / "native.sqlite")
        journal.reserve("original-a", "revision", "digest", "source")
        self.inputs = {
            "original_report": original,
            "canonical_path": canonical,
            "journal_path": journal.path,
        }
        self.replacement = self.root / "replacement.sqlite"
        self.replacement.write_bytes(journal.path.read_bytes())

    def assert_rejected_change(self, change):
        observe = retro._inventory_code
        changed = False

        def observe_then_change(root):
            nonlocal changed
            result = observe(root)
            if not changed:
                changed = True
                change()
            return result

        with patch.object(retro, "_inventory_code", observe_then_change):
            with self.assertRaisesRegex(ValueError, "journal.*changed"):
                retro.inventory_native_state(**self.inputs)
        self.assertTrue(changed)

    def test_inventory_rejects_atomic_journal_replacement_with_identical_receipts(self):
        self.assert_rejected_change(lambda: self.replacement.replace(self.inputs["journal_path"]))

    def test_inventory_rejects_atomic_journal_replacement_with_new_unknown_attempt(self):
        replacement = retro.NativeAttemptJournal.open_existing(self.replacement)
        replacement.reserve("original-b", "revision", "digest", "source")
        self.assert_rejected_change(lambda: self.replacement.replace(self.inputs["journal_path"]))

    def test_inventory_rejects_journal_symlink_switch_with_identical_receipts(self):
        link = self.root / "journal-link.sqlite"
        link.symlink_to(self.inputs["journal_path"])
        self.inputs["journal_path"] = link

        def switch():
            link.unlink()
            link.symlink_to(self.replacement)

        self.assert_rejected_change(switch)

    def test_inventory_rejects_disappearing_journal(self):
        self.assert_rejected_change(lambda: self.inputs["journal_path"].unlink())


if __name__ == "__main__":
    unittest.main()
