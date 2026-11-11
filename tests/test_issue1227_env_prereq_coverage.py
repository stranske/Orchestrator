"""Hermetic pytest coverage for the first-ranked #1227 target."""

from __future__ import annotations

import io
import json
import os
import socket
import subprocess
import tempfile
import unittest
import urllib.request
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import adapters
import agent_auth_check
import capabilities
import capability_activation_audit
import capability_admission
import env_prereq
import paths


def test_env_prereq_selftest_runs_with_an_isolated_runtime(monkeypatch, tmp_path):
    """Exercise the detector contract without consulting local state or spawning git."""
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("ORCH_LOCAL_RUNTIME", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(env_prereq.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(agent_auth_check, "HOME", tmp_path)
    monkeypatch.setattr(
        agent_auth_check,
        "CREDENTIAL_FILES",
        {
            agent: (tmp_path / agent / path.name, key)
            for agent, (path, key) in agent_auth_check.CREDENTIAL_FILES.items()
        },
    )
    monkeypatch.setattr(capability_activation_audit, "STATE_DIR", tmp_path)
    monkeypatch.setattr(env_prereq.shutil, "which", lambda _binary: None)
    monkeypatch.setattr(capabilities, "REG", tmp_path / "capabilities.json")
    monkeypatch.setattr(
        env_prereq.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout="[]", stderr=""),
    )

    env_prereq._selftest()
    assert env_prereq.ledger_legacy_rows_absent().startswith(
        "capability ledger holds no pre-admission-gate (legacy) capability"
    )


def test_ledger_detectors_name_an_unreadable_ledger(monkeypatch):
    """A local-ledger read failure is a named prerequisite, never an uncaught error."""
    monkeypatch.setattr(
        env_prereq,
        "_ledger",
        lambda: (_ for _ in ()).throw(OSError("fixture ledger unavailable")),
    )

    for detector in (
        env_prereq.ledger_rows_absent,
        env_prereq.ledger_version_lineage_absent,
        env_prereq.ledger_invocation_history_absent,
        env_prereq.ledger_rows_not_live,
    ):
        reason = detector("fixture-capability")
        assert reason == "capability ledger unreadable (OSError: fixture ledger unavailable)"


class PrerequisiteBoundaryTests(unittest.TestCase):
    """Exercise exceptional paths without requiring the host's runtime or installed CLIs."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="issue1227-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.stack = self.enterContext(ExitStack())
        self.stack.enter_context(patch.object(tempfile, "tempdir", str(self.root)))
        self.stack.enter_context(
            patch.dict(
                os.environ,
                {"ORCH_STATE_DIR": str(self.root), "ORCH_LOCAL_RUNTIME": str(self.root)},
            )
        )
        self.stack.enter_context(patch.object(Path, "home", return_value=self.root))
        self.stack.enter_context(patch.object(capabilities, "REG", self.root / "ledger.json"))
        self.ledger = self.stack.enter_context(patch.object(env_prereq, "_ledger", return_value={}))
        self.stack.enter_context(patch.object(paths, "checkout_root", return_value=self.root))
        self.denied = Mock(side_effect=AssertionError("unexpected process or network I/O"))
        self.addCleanup(self.denied.assert_not_called)
        for module, name in (
            (subprocess, "run"),
            (subprocess, "Popen"),
            (subprocess, "call"),
            (subprocess, "check_call"),
            (subprocess, "check_output"),
            (os, "system"),
            (socket, "socket"),
            (socket, "create_connection"),
            (urllib.request, "urlopen"),
        ):
            self.stack.enter_context(patch.object(module, name, self.denied))

    def test_core_sibling_override_presence_and_checkout_default(self):
        sibling = self.root / "independent"
        with patch.dict(os.environ, {"CLOSER_CORE_SOURCE": str(sibling)}):
            self.assertEqual(env_prereq.closer_core_source_path(), sibling)
            reason = env_prereq.core_sibling_absent()
            self.assertIn(env_prereq.CORE_SIBLING_ABSENT_MARK, reason)
            self.assertIn(str(sibling), reason)
            runtime = sibling / "scripts/closer_runtime.py"
            runtime.parent.mkdir(parents=True)
            runtime.touch()
            self.assertIsNone(env_prereq.core_sibling_absent())
        with patch.dict(os.environ):
            os.environ.pop("CLOSER_CORE_SOURCE", None)
            self.assertEqual(env_prereq.closer_core_source_path(), self.root.parent / "portable")

    def test_populated_rows_satisfy_lineage_and_invocation_prerequisites(self):
        self.ledger.return_value = {
            "ready": {"capability_version_id": "version:1", "last_invocation": 123}
        }
        self.assertIsNone(env_prereq.ledger_version_lineage_absent("ready"))
        self.assertIsNone(env_prereq.ledger_invocation_history_absent("ready"))
        self.assertIn("missing", env_prereq.ledger_version_lineage_absent("ready", "missing"))
        self.assertIn("missing", env_prereq.ledger_invocation_history_absent("ready", "missing"))

    def test_legacy_detector_names_report_errors_and_accepts_legacy_rows(self):
        with patch.object(capability_admission, "report") as report:
            report.side_effect = OSError("fixture report unavailable")
            self.assertEqual(
                env_prereq.ledger_legacy_rows_absent(),
                "admission report unavailable (OSError: fixture report unavailable)",
            )
            report.side_effect = None
            report.return_value = {"rows": [{"legacy": True}]}
            self.assertIsNone(env_prereq.ledger_legacy_rows_absent())

    def test_git_detector_checks_exit_status_and_names_launch_errors(self):
        with (
            patch.object(env_prereq.shutil, "which", return_value="/fixture/git"),
            patch.object(subprocess, "run", return_value=SimpleNamespace(returncode=0)) as run,
        ):
            self.assertIsNone(env_prereq.git_repo_absent())
            run.assert_called_once_with(
                ["git", "rev-parse", "--git-dir"],
                cwd=self.root,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            run.return_value.returncode = 128
            self.assertIn("not a git repository", env_prereq.git_repo_absent())
            for error in (OSError("fixture launch failure"), subprocess.TimeoutExpired("git", 30)):
                with self.subTest(error=type(error).__name__):
                    run.side_effect = error
                    reason = env_prereq.git_repo_absent()
                    self.assertIn(f"git could not be run in {self.root}", reason)
                    self.assertIn(str(error), reason)

    def test_seats_accept_cli_or_file_and_name_missing_signals(self):
        present = self.root / "seat.json"
        present.touch()
        with (
            patch.object(agent_auth_check, "AGENTS", ("cli", "file", "blind")),
            patch.object(adapters, "AUTH_PROBES", {"cli": {"cmd": ["fixture-cli"]}}),
            patch.object(agent_auth_check, "CREDENTIAL_FILES", {"file": (present, "unused")}),
            patch.object(env_prereq.shutil, "which") as which,
        ):
            which.side_effect = lambda binary: "/fixture/cli" if binary == "fixture-cli" else None
            self.assertEqual(
                env_prereq._seat_signals(), {"cli": True, "file": True, "blind": False}
            )
            self.assertIsNone(env_prereq.agent_cli_absent("cli"))
            self.assertIn("blind (blind)", env_prereq.agent_cli_absent("blind"))
            self.assertIsNone(env_prereq.credential_file_absent("file"))
            self.assertIn(
                "blind (no credential file registered)", env_prereq.credential_file_absent("blind")
            )
            self.assertIn("for blind:", env_prereq.seat_has_no_free_signal())
            present.unlink()
            self.assertIn(str(present), env_prereq.credential_file_absent("file"))
        with patch.object(env_prereq, "_seat_signals", return_value={"ready": True}):
            self.assertIsNone(env_prereq.seat_has_no_free_signal())

    def test_recorded_verdicts_reject_unusable_reports_and_filter_unknown_outcomes(self):
        flag = env_prereq.PYTEST_RESULTS_FLAG
        record = self.root / "verdicts.json"
        for argv in ([], [flag], [flag, str(record)]):
            with self.subTest(argv=argv):
                self.assertEqual(env_prereq.recorded_verdicts(argv, "tests/test_gate.py"), {})
        for blob in ("{broken", "[]", '{"test_gate": []}', '{"another_gate": {}}'):
            with self.subTest(blob=blob):
                record.write_text(blob, encoding="utf-8")
                self.assertEqual(
                    env_prereq.recorded_verdicts([flag, str(record)], "tests/test_gate.py"), {}
                )
        record.write_text(
            json.dumps(
                {
                    "test_gate": {
                        "unknown": {"outcome": "xfailed"},
                        "bad": None,
                        "ok": {"outcome": "passed"},
                    }
                }
            ),
            encoding="utf-8",
        )
        self.assertEqual(
            env_prereq.recorded_verdicts([flag, str(record)], "tests/test_gate.py"),
            {"ok": {"outcome": "passed"}},
        )

    def test_replayed_failure_preserves_message_and_supplies_fallback(self):
        check = Mock(__name__="test_recorded")
        for message, expected in (
            ("AssertionError: differing values", "differing values"),
            ("plain failure", "plain failure"),
            ("AssertionError: ", "failed under pytest"),
            (None, "failed under pytest"),
        ):
            with self.subTest(message=message), self.assertRaises(AssertionError) as raised:
                env_prereq.run_or_replay(
                    check, {"test_recorded": {"outcome": "failed", "message": message}}
                )
            self.assertEqual(str(raised.exception), expected)
        check.assert_not_called()

    def test_main_reports_both_shapes_and_named_prerequisites(self):
        detectors = (
            "exec_mirror_shape",
            "bare_machine",
            "skill_resource_absent",
            "codex_profile_binary_absent",
            "seat_has_no_free_signal",
            "credential_file_absent",
            "ledger_legacy_rows_absent",
        )
        for missing in (False, True):
            with self.subTest(missing=missing), ExitStack() as stack:
                for detector in detectors:
                    stack.enter_context(
                        patch.object(
                            env_prereq, detector, return_value="fixture absent" if missing else None
                        )
                    )
                output = io.StringIO()
                with redirect_stdout(output):
                    self.assertEqual(env_prereq.main([]), 0)
                text = output.getvalue()
                self.assertIn(
                    "tree shape: EXEC MIRROR" if missing else "tree shape: checkout", text
                )
                self.assertIn("machine:    BARE" if missing else "machine:    provisioned", text)
                self.assertIn(
                    (
                        "ABSENT  reference skill resource"
                        if missing
                        else "present reference skill resource"
                    ),
                    text,
                )
                self.assertEqual(text.count("fixture absent"), 7 if missing else 0)
