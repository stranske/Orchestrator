"""Hermetic regression tests for recurrence caching and propensity recording boundaries."""

from __future__ import annotations

import contextlib
import io
import json
import signal
import socket
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import call, patch

import capabilities
import capability_propensity as propensity
import capability_recurrence_check as recurrence
import dispatcher
import feedback


class TickBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.scoped = self.enterContext(contextlib.ExitStack())
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.scoped.enter_context(
            patch.dict(
                "os.environ",
                {"ORCH_STATE_DIR": str(self.root), "ORCH_LOCAL_RUNTIME": str(self.root)},
            )
        )
        self.scoped.enter_context(
            patch.object(capabilities, "REG", self.root / "capabilities.json")
        )
        self.scoped.enter_context(patch.object(feedback, "DB_PATH", self.root / "brain.db"))
        self.scoped.enter_context(patch.object(recurrence, "_TICK_ENV", None))
        self.scoped.enter_context(patch.object(recurrence, "_TICK_ENV_DIAG", None))
        self.external = [
            self.scoped.enter_context(
                patch.object(owner, name, side_effect=AssertionError("external work forbidden"))
            )
            for owner, name in (
                (subprocess, "run"),
                (subprocess, "Popen"),
                (socket, "socket"),
                (socket, "create_connection"),
                (dispatcher, "offload"),
                (capabilities, "production_heartbeat"),
            )
        ]

    def tearDown(self):
        for external in self.external:
            external.assert_not_called()

    def test_exhausted_resolution_is_cached_until_explicit_refresh(self):
        """A cached empty environment must not restart shell retries on every flag lookup."""
        failed = ({}, {"reason": "timeout", "detail": "busy"})
        recovered = ({"ORCH_RECOVERED": "1"}, {"reason": "ok", "keys": 1})
        with (
            patch.object(recurrence, "TICK_ENV_ATTEMPTS", 3),
            patch.object(recurrence, "TICK_ENV_BACKOFF", 0.25),
            patch.object(recurrence, "_tick_env_attempt", side_effect=[failed] * 3 + [recovered])
            as attempt,
            patch.object(recurrence.time, "sleep") as sleep,
            contextlib.redirect_stderr(io.StringIO()) as stderr,
        ):
            first = recurrence.tick_env()
            self.assertEqual(first, {})
            self.assertIs(recurrence.tick_env(), first)
            status = recurrence.tick_env_status()
            self.assertEqual(attempt.call_count, 3)
            self.assertEqual(sleep.call_args_list, [call(0.25), call(0.5)])
            self.assertEqual(status["outcome"], "environment")
            self.assertEqual(status["reason"], "timeout")
            self.assertEqual(status["retried"], 2)
            self.assertEqual([row["attempt"] for row in status["attempts"]], [1, 2, 3])
            self.assertEqual(stderr.getvalue().count("[tick_env]"), 1)
            status["reason"] = "caller modification"
            self.assertEqual(recurrence.tick_env_status()["reason"], "timeout")
            self.assertEqual(recurrence.tick_env(refresh=True), {"ORCH_RECOVERED": "1"})
            self.assertEqual(attempt.call_count, 4)
            self.assertEqual(recurrence.tick_env_status()["retried"], 0)
            self.assertEqual(recurrence.tick_env_status()["outcome"], "ok")

    def test_incomplete_preview_is_reported_without_starting_bash(self):
        script = self.root / "orchestrate.sh"
        script.write_text(
            "export ORCH_BASE=1\n"
            "_gh_gate() { :; }\n"
            "# ORCH-ANCHOR: exploration-window-start\n"
            "export ORCH_PREVIEW=1\n",
            encoding="utf-8",
        )
        with patch.object(recurrence, "ORCHESTRATE", script):
            self.assertEqual(
                recurrence._tick_env_attempt(),
                (
                    {},
                    {
                        "reason": "script_unreadable",
                        "detail": "exploration preview block is incomplete",
                    },
                ),
            )

    def test_preview_inspection_uses_only_prologue_and_marked_block(self):
        script = self.root / "orchestrate.sh"
        prologue = "export ORCH_BASE=1\n"
        preview = (
            "# ORCH-ANCHOR: exploration-window-start\n"
            "export ORCH_PREVIEW=1\n"
            "# ORCH-ANCHOR: exploration-window-end\n"
        )
        script.write_text(
            prologue + "_gh_gate() { :; }\nauth_and_dispatch\n" + preview + "start_agents\n",
            encoding="utf-8",
        )
        with (
            patch.object(recurrence, "ORCHESTRATE", script),
            patch.object(
                recurrence.subprocess,
                "run",
                return_value=SimpleNamespace(
                    returncode=0, stdout="ORCH_BASE=1\nORCH_PREVIEW=1\n", stderr=""
                ),
            ) as run,
        ):
            env, diagnostic = recurrence._tick_env_attempt()
            self.assertEqual(env, {"ORCH_BASE": "1", "ORCH_PREVIEW": "1"})
            self.assertEqual(diagnostic["reason"], "ok")
            run.assert_called_once_with(
                [
                    "bash",
                    "-c",
                    prologue + preview + "env | grep '^ORCH_' || true\n",
                    str(script.resolve()),
                ],
                capture_output=True,
                text=True,
                timeout=recurrence.TICK_ENV_TIMEOUT,
            )

    def test_tick_evidence_interrupt_is_reported_and_previous_alarm_handler_restored(self):
        """An evidence-step interrupt cannot escape into the dispatching tick."""

        def previous_handler(_signum, _frame):
            pass

        with (
            patch.object(signal, "signal", return_value=previous_handler) as handler,
            patch.object(signal, "alarm") as alarm,
            patch.object(propensity, "tick_evidence", side_effect=KeyboardInterrupt("interrupted"))
            as evidence,
        ):
            result = propensity.tick_evidence_guarded(budget_s=0, record=False)
            evidence.assert_called_once_with(budget_s=0, record=False)
            self.assertEqual(result["error"], "KeyboardInterrupt: interrupted")
            self.assertEqual(
                result["reason"], "tick capability evidence failed; the tick is unaffected"
            )
            for field in ("verdicts_recorded", "triggers_recorded", "matches_recorded"):
                self.assertEqual(result[field], 0)
            self.assertEqual(alarm.call_args_list, [call(6), call(0)])
            self.assertEqual(handler.call_count, 2)
            self.assertEqual(handler.call_args_list[0].args[0], signal.SIGALRM)
            self.assertEqual(handler.call_args_list[1], call(signal.SIGALRM, previous_handler))

    def test_consult_cli_duplicate_refuses_to_overwrite_recorded_direction(self):
        """A retry must signal refusal and keep the first externally observed outcome."""
        ledger = self.root / "capabilities.json"
        args = [
            "consult-outcome",
            "--experiment",
            "advice:boundary",
            "--direction",
            "landed",
            "--evidence",
            "owner/repo#7 merged",
            "--corroboration",
            "merge event",
            "--provenance",
            "machine_observed",
            "--deliverable",
            " Batch-One ",
            "--ledger",
            str(ledger),
        ]
        with contextlib.redirect_stdout(io.StringIO()) as stdout:
            self.assertEqual(propensity.main(args), 0)
        first = json.loads(stdout.getvalue())
        self.assertEqual(first["deliverable"], "batch-one")
        self.assertEqual(first["command"], "consult-outcome")
        self.assertTrue(first["recorded"])
        store = propensity.consult_outcomes_path(ledger)
        original = store.read_bytes()
        args[args.index("landed")] = "failed"
        with contextlib.redirect_stdout(io.StringIO()) as stdout:
            self.assertEqual(propensity.main(args), 3)
        duplicate = json.loads(stdout.getvalue())
        self.assertFalse(duplicate["recorded"])
        self.assertEqual(duplicate["reason"], "already recorded")
        self.assertEqual(duplicate["existing"]["direction"], "landed")
        self.assertEqual(
            duplicate["existing"]["idempotency_key"], "consult:advice:boundary:batch-one"
        )
        self.assertEqual(store.read_bytes(), original)
        self.assertFalse(ledger.exists())
        self.assertEqual(list(self.root.iterdir()), [store])
