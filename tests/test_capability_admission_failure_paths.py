"""Admission failure paths use disposable state and never invoke an external process."""

from __future__ import annotations

import os
import socket
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import capability_admission as admission
import feedback
import value_chain_monitor as monitor


class TestAdmissionFailurePaths(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="admission-tests-")))
        self.enterContext(
            patch.dict(
                os.environ,
                {
                    "ORCH_STATE_DIR": str(self.root),
                    "ORCH_LOCAL_RUNTIME": str(self.root),
                    "ORCH_AUDITS_DIR": str(self.root / "audits"),
                    "ORCH_FLEET_ROOT": str(self.root / "fleet"),
                },
            )
        )
        self.audits = self.root / "audits"
        self.audits.mkdir()
        self.modules = self.root / "src"
        self.modules.mkdir()
        for target, value in (
            ("AUDITS", self.audits),
            ("HERE", self.modules),
            ("_ACTED_ON_CACHE", {}),
        ):
            self.enterContext(patch.object(admission, target, value))
        self.enterContext(patch.object(admission.paths, "MODULE_DIR", self.modules))
        self.enterContext(patch.object(admission.paths, "REPO_ROOT", self.root))
        self.enterContext(patch.object(admission.paths, "FLEET_ROOT", self.root / "sibling-fleet"))
        self.enterContext(patch.object(admission.capabilities, "REG", self.root / "ledger.json"))
        self.enterContext(patch.object(feedback, "DB_PATH", self.root / "feedback.db"))
        self.enterContext(patch.object(Path, "home", return_value=self.root / "home"))
        self.external_calls = []

        def forbid_external(*args, **kwargs):
            self.external_calls.append((args, kwargs))
            raise AssertionError("admission test attempted an external process or network call")

        for owner, name in (
            (subprocess, "run"),
            (subprocess, "Popen"),
            (socket, "socket"),
            (socket, "create_connection"),
        ):
            self.enterContext(patch.object(owner, name, side_effect=forbid_external))

    def tearDown(self):
        self.assertEqual(self.external_calls, [])
        self.assertFalse((self.root / "ledger.json").exists())
        self.assertFalse((self.root / "feedback.db").exists())

    def test_audit_directory_precedence_and_absent_fleet_fallback(self):
        os.environ["ORCH_AUDITS_DIR"] = str(self.root / "explicit-audits")
        self.assertEqual(admission._audits_dir(), self.root / "explicit-audits")
        del os.environ["ORCH_AUDITS_DIR"]
        explicit = self.root / "fleet" / "Audits" / "Orchestrator"
        sibling = self.root / "sibling-fleet" / "Audits" / "Orchestrator"
        for directory in (explicit, sibling):
            directory.mkdir(parents=True)
        self.assertEqual(admission._audits_dir(), explicit)
        del os.environ["ORCH_FLEET_ROOT"]
        self.assertEqual(admission._audits_dir(), sibling)
        sibling.rmdir()
        fallback = self.root / "home/Library/CloudStorage/Dropbox/Learning/Code/Audits/Orchestrator"
        self.assertEqual(admission._audits_dir(), fallback)
        fallback.mkdir(parents=True)
        self.assertEqual(admission._audits_dir(), fallback)

    def test_known_controls_tolerates_missing_files_and_uses_both_roots(self):
        (self.root / "orchestrate.sh").write_text("ORCH_SHELL_CONTROL=1\n")
        (self.modules / "dispatcher.py").write_text('control = "ORCH_MODULE_2"\n')
        (self.root / "dispatcher.py").write_text('control = "ORCH_WRONG_ROOT"\n')
        (self.modules / "router.py").mkdir()
        self.assertEqual(admission.known_controls(), {"ORCH_SHELL_CONTROL", "ORCH_MODULE_2"})

    def test_consumer_resolution_rejects_escape_and_resolves_test_and_shell_symbols(self):
        (self.root / "outside.py").write_text("def consume(): pass\n")
        (self.modules / "alias.py").symlink_to(self.root / "outside.py")
        (self.root / "tests").mkdir()
        (self.root / "tests/consumer.py").write_text(
            "class Consumer:\n    alias = object()\n    typed: int = 1\n"
        )
        (self.root / "entry.sh").write_text("launch=ready\n")
        declaration = (
            "../outside.py:consume alias.py:consume consumer.py:Consumer.alias "
            "consumer.py:Consumer.typed consumer.py:Consumer.alias.child entry.sh:launch"
        )
        result = admission.consumer_resolution({"downstream_consumer": declaration})
        self.assertEqual(
            result["resolved"],
            ["consumer.py:Consumer.alias", "consumer.py:Consumer.typed", "entry.sh:launch"],
        )
        self.assertEqual(
            result["unresolved"],
            ["../outside.py:consume", "alias.py:consume", "consumer.py:Consumer.alias.child"],
        )
        self.assertEqual(len(result["tokens"]), 6)

    def test_unreadable_act_index_is_cached_until_the_hour_or_input_changes(self):
        failure = RuntimeError("unavailable " + "x" * 100)
        reader = self.enterContext(patch.object(monitor, "acted_on_index", side_effect=failure))
        ledger = {"b": {}, "a": {}}
        first = admission.acted_on_context(ledger, now=7200)
        self.assertEqual(
            first, {"acted_on": None, "acted_on_reason": f"RuntimeError: {str(failure)[:80]}"}
        )
        self.assertIs(admission.acted_on_context({"a": {}, "b": {}}, now=10799), first)
        reader.assert_called_once_with(feedback.DB_PATH, 7200, ["a", "b"])
        admission.acted_on_context(ledger, now=10800)
        admission.acted_on_context({"a": {}}, now=10800)
        with patch.object(feedback, "DB_PATH", self.root / "other.db"):
            admission.acted_on_context(ledger, now=10800)
        self.assertEqual(reader.call_count, 4)

    def test_missing_act_counts_are_zero_and_negative_results_are_cached(self):
        reader = self.enterContext(
            patch.object(monitor, "acted_on_index", return_value=({"a": 3, "unrelated": 8}, ""))
        )
        ledger = {"a": {}, "b": {}}
        self.assertEqual(
            admission.acted_on_context(ledger, now=3600),
            {"acted_on": {"a": 3, "b": 0}, "acted_on_reason": ""},
        )
        reader.return_value = (None, "Brain missing")
        missing = admission.acted_on_context(ledger, now=7200)
        self.assertEqual(missing, {"acted_on": None, "acted_on_reason": "Brain missing"})
        self.assertIs(admission.acted_on_context(ledger, now=7201), missing)
        self.assertEqual(reader.call_count, 2)

    def test_admit_and_preflight_report_predicate_errors_as_missing(self):
        predicate = Mock(side_effect=OSError("cannot inspect"))
        self.enterContext(patch.object(admission, "REQUIREMENTS", (("rollback", predicate),)))
        self.enterContext(patch.object(admission, "_findability_context", return_value={}))
        ledger = {"probe": {"capability_id": "probe", "status": "wired"}}
        with self.assertRaisesRegex(ValueError, "unknown capability: absent"):
            admission.admit("absent", ledger=ledger, ctx={"isolated": True})
        admitted = admission.admit("probe", ledger=ledger, ctx={"isolated": True})
        self.assertEqual(admitted["missing"], ["rollback"])
        self.assertEqual(admitted["blocking"], ["rollback"])
        self.assertFalse(admitted["admitted"])
        self.assertTrue(admitted["enforced"])
        expected = {"ok": False, "detail": "check errored: cannot inspect"}
        self.assertEqual(admitted["checks"]["rollback"], expected)
        preflight = admission.preflight({"capability_id": "probe"})
        self.assertEqual(preflight["declarable_missing"], ["rollback"])
        self.assertFalse(preflight["ready_to_build"])
        self.assertEqual(preflight["checks"]["rollback"], expected)
        self.assertEqual(predicate.call_count, 2)

    def test_decisions_ignore_old_plans_and_unreadable_records(self):
        variable = "ORCH_RANGE_LANE_TRIAL_UNTIL"
        (self.audits / "2026-01-01-range-lane.md").write_text("decision before deadline")
        (self.audits / "2026-02-01-range-lane-PLAN.md").write_text("proposed decision")
        (self.audits / "2026-02-02-unreadable.md").mkdir()
        self.assertFalse(admission._decision_recorded_after(variable, "2026-02-01"))
        record = self.audits / "2026-02-03-decision.md"
        record.write_text("Reviewed range-lane and ended the trial.")
        self.assertTrue(admission._decision_recorded_after(variable, "2026-02-01"))
        record.write_text("Reviewed a thing; no named trial decision.")
        self.assertFalse(admission._decision_recorded_after("ORCH_THING_UNTIL", "2026-02-01"))
        record.write_text("Reviewed ORCH_THING_UNTIL and ended the trial.")
        self.assertTrue(admission._decision_recorded_after("ORCH_THING_UNTIL", "2026-02-01"))
        record.unlink()
        (self.audits / "2026-02-04-range-lane.md").write_text("decision")
        self.assertTrue(admission._decision_recorded_after(variable, "2026-02-01"))
        with patch.object(admission, "AUDITS", self.root / "absent"):
            self.assertTrue(admission._decision_recorded_after(variable, "2026-02-01"))

    def test_commitments_continue_after_read_errors_and_resolve_nested_records(self):
        cited = "2026-02-01-nested-review.md"
        (self.modules / "records").mkdir()
        (self.modules / "records" / cited).write_text("finished review")
        (self.modules / "consumer.py").write_text(
            f"# See {cited}\n# See 2026-02-01-missing-review.md\n"
        )
        unreadable = self.modules / "broken.md"
        unreadable.write_text("# See 2026-02-01-unreadable-review.md\n")
        original = Path.read_text

        def read_text(path, *args, **kwargs):
            if path == unreadable:
                raise PermissionError("synthetic unreadable source")
            return original(path, *args, **kwargs)

        with patch.object(Path, "read_text", read_text):
            result = admission.commitments(root=self.modules)
        self.assertFalse(result["clean"])
        self.assertEqual(
            result["dangling_citations"],
            [
                {
                    "file": "consumer.py",
                    "line": 2,
                    "record": "2026-02-01-missing-review.md",
                    "text": "# See 2026-02-01-missing-review.md",
                }
            ],
        )
        self.assertEqual(result["overdue_without_record"], [])
