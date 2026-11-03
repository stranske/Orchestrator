"""A failed propensity read must not erase otherwise usable capability advice."""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import capabilities
import capability_advisor as advisor
import capability_propensity
import repo_knowledge


class AdvisorRankingFailureTests(unittest.TestCase):
    def setUp(self):
        scratch = self.enterContext(tempfile.TemporaryDirectory(prefix="advisor-ranking-"))
        self.root = Path(scratch)
        self.ledger = self.root / "capabilities.json"
        self.enterContext(
            patch.dict(
                os.environ,
                {"ORCH_STATE_DIR": scratch, "ORCH_LOCAL_RUNTIME": scratch},
            )
        )
        self.external_calls = []

        def forbidden(*args, **kwargs):
            self.external_calls.append((args, kwargs))
            raise AssertionError("advisor test attempted an external call or ledger write")

        for target, name in (
            (subprocess, "run"),
            (subprocess, "Popen"),
            (socket, "socket"),
            (socket, "create_connection"),
            (capabilities, "heartbeat"),
            (advisor, "PR_FACTS_FETCH"),
        ):
            self.enterContext(patch.object(target, name, side_effect=forbidden))
        self.addCleanup(lambda: self.assertEqual(self.external_calls, []))

        self.rows = {}
        for name in ("a-warning", "z-usable"):
            row = capabilities._blank_capability(name)
            row.update(
                status="generated",
                matcher={"kind": "task_type", "name": "review"},
                entrypoint=f"{name}.py",
            )
            self.rows[name] = row

        def load_declared(path):
            self.assertEqual(path, self.ledger)
            return self.rows

        self.enterContext(patch.object(capabilities, "load_declared", side_effect=load_declared))
        self.enterContext(patch.object(advisor, "direct_entry", return_value={}))
        self.enterContext(patch.object(advisor, "CONSULT_SITES", {}))
        self.enterContext(patch.object(advisor, "KNOWN_UNCONSULTED", set()))
        self.enterContext(
            patch.object(
                advisor,
                "SURFACE_BINDINGS",
                {"offline": {name: "declared for this surface" for name in self.rows}},
            )
        )
        self.enterContext(
            patch.object(
                repo_knowledge,
                "contraindications_for",
                return_value={
                    "a-warning": {
                        "reason": "unreliable in this repository",
                        "instead": "z-usable",
                    }
                },
            )
        )
        self.rank = self.enterContext(
            patch.object(capability_propensity, "rank", side_effect=OSError("store unavailable"))
        )

    def assert_advice_survives(self, task, confidence):
        result = advisor.advise(
            task,
            surface="offline",
            repository="owner/project",
            record=False,
            path=self.ledger,
        )
        self.rank.assert_called_once()
        self.assertEqual(self.rank.call_args.kwargs, {"path": self.ledger})
        self.assertTrue(result["useful"])
        self.assertEqual(result["confidence"], confidence)
        entries = result["capabilities"]
        self.assertEqual([entry["capability_id"] for entry in entries], ["z-usable", "a-warning"])
        self.assertTrue(all(entry["bound"] for entry in entries))
        self.assertEqual(result["bound_count"], 2)
        self.assertEqual(result["coverage"]["matched"], 2)
        self.assertEqual(result["not_applicable"], [])
        self.assertEqual(result["contraindicated"], ["a-warning"])
        self.assertEqual(entries[1]["use_instead"], "z-usable")
        self.assertIn("unreliable in this repository", advisor.format_advice(result))
        self.assertNotIn("recorded_matches", result)
        self.assertFalse(self.ledger.exists())
        return result

    def test_binding_only_advice_survives_ranking_failure(self):
        result = self.assert_advice_survives("xylophone", "binding_only")
        self.assertEqual(result["task_types"], [])
        self.assertTrue(all(entry["bound_only"] for entry in result["capabilities"]))

    def test_classified_advice_survives_ranking_failure(self):
        result = self.assert_advice_survives("review and critique", "high")
        self.assertEqual(result["task_types"], ["review"])
        self.assertTrue(
            all(entry["matched_task_type"] == "review" for entry in result["capabilities"])
        )

    def test_timeout_is_reported_and_previous_alarm_handler_is_restored(self):
        previous = object()
        registrations = self.enterContext(patch.object(signal, "signal", return_value=previous))
        alarms = self.enterContext(patch.object(signal, "alarm"))

        def expire(**kwargs):
            self.assertEqual(kwargs, {"day": "2026-10-17", "path": self.ledger})
            advisor._consult_expired(None, None)

        consult = self.enterContext(patch.object(advisor, "consult_phases", side_effect=expire))
        report = advisor.consult_phases_guarded(day="2026-10-17", path=self.ledger)
        consult.assert_called_once()
        self.assertEqual(report["day"], "2026-10-17")
        self.assertEqual(report["phases"], 0)
        self.assertEqual(report["rows"], [])
        self.assertEqual(report["offered"], 0)
        self.assertEqual(report["recorded"], 0)
        self.assertEqual(report["errors"], ["*"])
        self.assertEqual(
            report["error"],
            f"TimeoutError: capability phase consult exceeded {advisor.CONSULT_BUDGET_S}s",
        )
        self.assertEqual(
            [call.args for call in alarms.call_args_list], [(advisor.CONSULT_BUDGET_S + 5,), (0,)]
        )
        self.assertEqual(
            [call.args for call in registrations.call_args_list],
            [(signal.SIGALRM, advisor._consult_expired), (signal.SIGALRM, previous)],
        )

    def test_phase_failure_and_exhausted_budget_preserve_successful_phase_counts(self):
        phases = ["tick:broken", "tick:healthy", "tick:late"]
        clock = self.enterContext(
            patch.object(
                advisor.time,
                "monotonic",
                side_effect=[100, 100, 101, 101 + advisor.CONSULT_BUDGET_S],
            )
        )
        consult = self.enterContext(
            patch.object(
                advisor,
                "advise",
                side_effect=[
                    OSError("unreadable phase"),
                    {
                        "experiment_id": "healthy-trial",
                        "capabilities": [{"capability_id": "one"}, {"capability_id": "two"}],
                        "bound_capabilities": ["two", "one"],
                        "recorded_matches": 1,
                    },
                ],
            )
        )
        suppressed = self.enterContext(patch.object(advisor, "binding_suppressed", return_value=""))
        report = advisor.consult_phases(
            day="2026-10-17", surfaces=phases, record=False, path=self.ledger
        )
        self.assertEqual(clock.call_count, 4)
        self.assertEqual(consult.call_count, 2)
        for call, surface in zip(consult.call_args_list, phases):
            self.assertEqual(call.args, (advisor.consult_text(surface, "2026-10-17"),))
            self.assertEqual(
                call.kwargs,
                dict(surface=surface, skill=surface, lane="tick", record=False, path=self.ledger),
            )
        suppressed.assert_called_once_with("tick:healthy")
        self.assertEqual(report["day"], "2026-10-17")
        self.assertEqual(report["phases"], 3)
        self.assertEqual(report["offered"], 2)
        self.assertEqual(report["recorded"], 1)
        self.assertEqual(report["errors"], ["tick:broken", "tick:late"])
        self.assertEqual(
            report["rows"],
            [
                {
                    "surface": "tick:broken",
                    "error": "OSError: unreadable phase",
                    "offered": 0,
                    "recorded": 0,
                },
                {
                    "surface": "tick:healthy",
                    "experiment_id": "healthy-trial",
                    "offered": 2,
                    "bound": ["one", "two"],
                    "recorded": 1,
                    "suppressed": False,
                },
                {
                    "surface": "tick:late",
                    "error": f"consult budget of {advisor.CONSULT_BUDGET_S}s exhausted before this phase",
                    "offered": 0,
                    "recorded": 0,
                },
            ],
        )

    def test_guard_runs_without_alarm_support(self):
        report = {"day": "offline", "phases": 1}
        for error in (ValueError, AttributeError, OSError):
            with self.subTest(error=error.__name__), patch.object(
                signal, "signal", side_effect=error("no alarm support")
            ), patch.object(signal, "alarm") as alarms, patch.object(
                advisor, "consult_phases", return_value=report
            ) as consult:
                self.assertIs(
                    advisor.consult_phases_guarded(day="offline", record=False, path=self.ledger),
                    report,
                )
                consult.assert_called_once_with(day="offline", record=False, path=self.ledger)
                alarms.assert_not_called()

    def test_guard_reports_base_exception_when_alarm_is_unavailable(self):
        self.enterContext(patch.object(signal, "signal", side_effect=ValueError("worker thread")))
        alarms = self.enterContext(patch.object(signal, "alarm"))
        consult = self.enterContext(
            patch.object(advisor, "consult_phases", side_effect=KeyboardInterrupt("interrupted"))
        )
        self.assertEqual(
            advisor.consult_phases_guarded(day="offline", path=self.ledger),
            {
                "day": "offline",
                "phases": 0,
                "rows": [],
                "offered": 0,
                "recorded": 0,
                "errors": ["*"],
                "error": "KeyboardInterrupt: interrupted",
            },
        )
        consult.assert_called_once_with(day="offline", path=self.ledger)
        alarms.assert_not_called()

    def test_guard_cleanup_failure_cannot_replace_successful_report(self):
        previous = object()
        report = {"phases": 2, "offered": 3, "recorded": 1}
        for failing in ("cancel", "restore"):
            with self.subTest(failing=failing), patch.object(
                signal,
                "signal",
                side_effect=[previous, OSError("cannot restore")] if failing == "restore" else None,
                return_value=previous,
            ) as registrations, patch.object(
                signal,
                "alarm",
                side_effect=[None, OSError("cannot cancel")] if failing == "cancel" else None,
            ) as alarms, patch.object(
                advisor, "consult_phases", return_value=report
            ) as consult:
                self.assertIs(advisor.consult_phases_guarded(path=self.ledger), report)
                consult.assert_called_once_with(path=self.ledger)
                self.assertEqual(
                    [call.args for call in alarms.call_args_list],
                    [(advisor.CONSULT_BUDGET_S + 5,), (0,)],
                )
                self.assertEqual(
                    registrations.call_args_list[0].args, (signal.SIGALRM, advisor._consult_expired)
                )
                self.assertEqual(registrations.call_count, 2 if failing == "restore" else 1)
                if failing == "restore":
                    self.assertEqual(registrations.call_args.args, (signal.SIGALRM, previous))

    def test_unreadable_contraindications_do_not_change_candidates(self):
        candidates = [{"capability_id": "one"}, {"capability_id": "two"}]
        notes = self.enterContext(
            patch.object(repo_knowledge, "contraindications_for", side_effect=OSError("unreadable"))
        )
        self.assertEqual(advisor._annotate_contraindications(candidates, "owner/project"), [])
        notes.assert_called_once_with("owner/project")
        self.assertEqual(candidates, [{"capability_id": "one"}, {"capability_id": "two"}])
