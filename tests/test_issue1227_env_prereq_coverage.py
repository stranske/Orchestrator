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
import capability_compiler
import env_prereq
import paths


def _deny_external_io(monkeypatch):
    denied = Mock(side_effect=AssertionError("unexpected process or network I/O"))
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
        monkeypatch.setattr(module, name, denied)
    return denied


def test_env_prereq_selftest_runs_with_an_isolated_runtime(monkeypatch, tmp_path):
    """Exercise the detector contract without consulting local state or spawning git."""
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("ORCH_LOCAL_RUNTIME", str(tmp_path))
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
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
    denied = _deny_external_io(monkeypatch)

    env_prereq._selftest()
    # Assert the contract _selftest checks, so removing one of its inner checks cannot pass silently.
    assert issubclass(env_prereq.MissingPrerequisite, unittest.SkipTest)
    assert env_prereq.require(None, None) is None
    try:
        env_prereq.require(None, "the named thing is missing", "a later reason")
    except env_prereq.MissingPrerequisite as exc:
        assert str(exc) == "the named thing is missing"
    else:
        raise AssertionError("require() must raise on the first named absence")
    reason = env_prereq.ledger_rows_absent("definitely-not-a-capability")
    assert reason is None or (isinstance(reason, str) and reason.strip())
    assert env_prereq.ledger_legacy_rows_absent().startswith(
        "capability ledger holds no pre-admission-gate (legacy) capability"
    )
    denied.assert_not_called()


def test_ledger_detectors_name_an_unreadable_ledger(monkeypatch, tmp_path):
    """A local-ledger read failure is a named prerequisite, never an uncaught error."""
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("ORCH_LOCAL_RUNTIME", str(tmp_path))
    denied = _deny_external_io(monkeypatch)
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
    denied.assert_not_called()


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

    def test_real_layout_detector_requires_flat_modules_and_git_absence_for_mirror(self):
        for flat in (False, True):
            module_dir = self.root if flat else self.root / "src"
            with patch.object(env_prereq, "__file__", str(module_dir / "env_prereq.py")):
                reason = env_prereq._flat_layout()
                if flat:
                    self.assertEqual(
                        reason, f"{self.root} holds the modules at its root, not under src/"
                    )
                else:
                    self.assertIsNone(reason)
                for git_absent in (False, True):
                    with (
                        self.subTest(flat=flat, git_absent=git_absent),
                        patch.object(
                            env_prereq,
                            "git_repo_absent",
                            return_value="not a repository" if git_absent else None,
                        ) as git_check,
                    ):
                        shape = env_prereq.exec_mirror_shape()
                        git_check.assert_called_once_with()
                        if flat and git_absent:
                            self.assertIn(f"{self.root} is the exec mirror", shape)
                            self.assertIn("git reports no repository here", shape)
                        else:
                            self.assertIsNone(shape)

    def test_populated_rows_satisfy_lineage_and_invocation_prerequisites(self):
        self.ledger.return_value = {
            "ready": {"capability_version_id": "version:1", "last_invocation": 123}
        }
        self.assertIsNone(env_prereq.ledger_version_lineage_absent("ready"))
        self.assertIsNone(env_prereq.ledger_invocation_history_absent("ready"))
        self.assertIn("missing", env_prereq.ledger_version_lineage_absent("ready", "missing"))
        self.assertIn("missing", env_prereq.ledger_invocation_history_absent("ready", "missing"))

    def test_bare_machine_requires_every_local_prerequisite_to_be_absent(self):
        for seat_present in (False, True):
            for skill_present in (False, True):
                for codex_present in (False, True):
                    with (
                        self.subTest(
                            seat=seat_present, skill=skill_present, codex=codex_present
                        ),
                        patch.object(
                            env_prereq, "_seat_signals", return_value={"fixture": seat_present}
                        ),
                        patch.object(
                            env_prereq,
                            "skill_resource_absent",
                            return_value=None if skill_present else "fixture skill absent",
                        ),
                        patch.object(
                            env_prereq,
                            "codex_profile_binary_absent",
                            return_value=None if codex_present else "fixture Codex absent",
                        ),
                    ):
                        reason = env_prereq.bare_machine()
                        if seat_present or skill_present or codex_present:
                            self.assertIsNone(reason)
                        else:
                            self.assertIn("no agent seat", reason)
                            self.assertIn("no reference skill resource", reason)
                            self.assertIn("no version-capable Codex", reason)

    def test_unreadable_machine_marks_cannot_select_the_bare_machine_allowance(self):
        detectors = ("_seat_signals", "skill_resource_absent", "codex_profile_binary_absent")
        for unreadable in detectors:
            with self.subTest(unreadable=unreadable), ExitStack() as stack:
                probes = {}
                for name in detectors:
                    probes[name] = stack.enter_context(
                        patch.object(
                            env_prereq,
                            name,
                            return_value={"fixture": False} if name == "_seat_signals" else "absent",
                        )
                    )
                probes[unreadable].side_effect = OSError("fixture mark cannot be read")
                self.assertIsNone(env_prereq.bare_machine())
                probes[unreadable].assert_called_once_with()

    def test_skill_resource_accepts_installed_file_and_names_missing_fallback(self):
        resource = self.root / "reference.sh"
        resource.touch()
        with patch.object(capability_compiler, "reference_skill_source") as source:
            source.return_value = {"resources": [{"source_path": str(resource)}]}
            self.assertIsNone(env_prereq.skill_resource_absent())
            resource.unlink()
            self.assertIn(str(resource), env_prereq.skill_resource_absent())
            fallback = self.root / ".codex/skills/code-workspace-hygiene/scripts/audit_code_root.sh"
            for error in (OSError("absent"), KeyError("resources"), IndexError("empty")):
                with self.subTest(error=type(error).__name__):
                    source.side_effect = error
                    self.assertIn(str(fallback), env_prereq.skill_resource_absent())
            fallback.parent.mkdir(parents=True)
            fallback.touch()
            self.assertIsNone(env_prereq.skill_resource_absent())

    def test_codex_profile_detector_reports_resolver_verdict_without_launching(self):
        with patch.object(
            adapters, "profile_codex_binary", return_value="/fixture/codex"
        ) as resolve:
            self.assertIsNone(env_prereq.codex_profile_binary_absent())
            resolve.assert_called_once_with()
            resolve.side_effect = RuntimeError("configured Codex binary is unavailable")
            self.assertEqual(
                env_prereq.codex_profile_binary_absent(),
                "configured Codex binary is unavailable",
            )

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

    def test_runnable_records_only_the_first_absent_prerequisite(self):
        gaps = ["earlier section is absent"]
        self.assertTrue(env_prereq.runnable(gaps, None, ""))
        self.assertEqual(gaps, ["earlier section is absent"])
        self.assertFalse(env_prereq.runnable(gaps, None, "ledger row is absent", "CLI is absent"))
        self.assertEqual(gaps, ["earlier section is absent", "ledger row is absent"])

    def test_report_gaps_prints_each_reason_once_in_first_seen_order(self):
        output = io.StringIO()
        with redirect_stdout(output):
            env_prereq.report_gaps("fixture", [])
        self.assertEqual(output.getvalue(), "")
        with redirect_stdout(output):
            env_prereq.report_gaps(
                "fixture", ["ledger is absent", "CLI is absent", "ledger is absent"]
            )
        self.assertEqual(
            output.getvalue().splitlines(),
            [
                f"fixture selftest: {env_prereq.PREREQ_ABSENT_MARK} section skipped — {reason}"
                for reason in ("ledger is absent", "CLI is absent")
            ],
        )

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

    def test_main_selftest_dispatches_once_without_reporting_machine(self):
        inventory = Mock(side_effect=AssertionError("selftest mode must not report machine inventory"))
        with (
            patch.object(env_prereq, "_selftest") as selftest,
            patch.object(env_prereq, "exec_mirror_shape", inventory),
            patch.object(env_prereq, "bare_machine", inventory),
            redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(env_prereq.main(["--selftest"]), 0)
        selftest.assert_called_once_with()
        inventory.assert_not_called()
        self.assertEqual(output.getvalue(), "")
