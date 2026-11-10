"""Hermetic exploration collection regressions for coverage round #1182."""

from __future__ import annotations

import io
import json
import os
import random
import socket
import subprocess
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from unittest.mock import call, patch

import exploration_collection as ec


class TestExplorationCollection(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(
            patch.dict(
                os.environ,
                ORCH_STATE_DIR=str(self.root),
                ORCH_LOCAL_RUNTIME=str(self.root),
                ORCH_EXPLORATION_EVIDENCE="0",
            )
        )
        self.stack.enter_context(patch.object(ec.feedback, "DB_PATH", self.root / "feedback.db"))
        self.stack.enter_context(patch.object(ec.router, "HANDOFF", self.root / "handoff"))
        self.stack.enter_context(
            patch.object(ec.router, "DECISION_JSON", self.root / "handoff" / "decision.json")
        )
        self.forbidden = []
        for owner, name in (
            (subprocess, "run"),
            (subprocess, "Popen"),
            (subprocess, "call"),
            (subprocess, "check_call"),
            (subprocess, "check_output"),
            (socket, "create_connection"),
            (socket.socket, "connect"),
            (os, "system"),
            (ec.dispatcher, "run"),
            (ec.router, "plan"),
            (ec.router, "learned_ranks"),
            (ec.claims, "release"),
            (ec.feedback, "_conn"),
            (ec.exploration_evidence_plan, "build_plan"),
            (ec.exploration_evidence_plan, "_capacity_snapshot"),
            (ec.exploration_evidence_plan, "_backlog_items"),
        ):
            guard = self.stack.enter_context(
                patch.object(owner, name, side_effect=AssertionError(f"unexpected call: {name}"))
            )
            self.forbidden.append(guard)

    def tearDown(self):
        for guard in self.forbidden:
            guard.assert_not_called()
        self.assertFalse(ec.feedback.DB_PATH.exists())

    def test_choose_mode_aliases_and_deficits(self):
        plan = {
            "direct_mode_deficits": [
                {"mode": "epsilon-greedy", "remaining_outcome_runs": 1},
                {"mode": "thompson-hybrid", "remaining_outcome_runs": 3},
            ]
        }
        self.assertEqual(ec.choose_mode(plan, "thompson"), "thompson-hybrid")
        self.assertEqual(ec.choose_mode(plan, "epsilon"), "epsilon-greedy")
        self.assertEqual(ec.choose_mode(plan, "auto"), "thompson-hybrid")
        plan["direct_mode_deficits"][0]["remaining_outcome_runs"] = 4
        self.assertEqual(ec.choose_mode(plan, "auto"), "epsilon-greedy")
        week = 7 * 86400
        self.assertEqual(ec.choose_mode({}, "auto", now=0), "epsilon-greedy")
        self.assertEqual(ec.choose_mode({}, "auto", now=week), "thompson-hybrid")

    def test_filter_rejects_recommended_high_risk_tasks(self):
        plan = {"candidate_task_types": [{"task_type": "epic", "recommended": True}]}
        item = {"target": "o/r#1", "task_type": "epic", "lane": "opener"}
        with patch.object(ec.exploration_evidence_plan, "_backlog_items", return_value=[item]):
            selected, skipped = ec._filter_backlog(plan, {}, 1)
        self.assertEqual(selected, [])
        self.assertEqual(
            skipped[0]["reason"], "task type is not low-risk for supervised collection"
        )

    def test_sanitize_rejects_each_unsafe_route_and_preserves_safe_assignment(self):
        safe = self.assignment()
        assignments = [
            {**safe, "lane": "closer"},
            {**safe, "task_type": "epic"},
            {**safe, "agent": "paygo"},
            {**safe, "agent": "late"},
            {**safe, "exploration": False},
            safe,
        ]
        table = {"implement": {"agents": [None, {"agent": "late", "mode": "local", "late": True}]}}
        with patch.object(ec.router, "BACKUP_AGENTS", {"paygo"}), patch.object(
            ec.router, "ROUTE_TABLE", table
        ):
            clean, rejected = ec._sanitize_decision(
                {"assignments": assignments, "notes": ["existing"]}, require_exploration=True
            )
        self.assertEqual(clean["assignments"], [safe])
        self.assertEqual(
            [row["reason"] for row in rejected],
            [
                "not opener lane",
                "not a low-risk supervised-collection task type",
                "backup/paygo agent is not allowed in supervised exploration windows",
                "late/paygo route entry is not allowed in supervised exploration windows",
                "non-exploratory assignment does not collect direct exploration evidence",
            ],
        )
        self.assertEqual(
            clean["notes"], ["existing", "rejected 5 unsafe supervised-window assignments"]
        )
        self.assertEqual(len(assignments), 6)

    @staticmethod
    def assignment(**kwargs):
        return {
            "target": "o/r#1",
            "task_type": "implement",
            "agent": "codex",
            "mode": "local",
            "lane": "opener",
            "exploration": True,
            **kwargs,
        }

    def test_claim_cleanup_skips_incomplete_rows_and_continues_after_failure(self):
        assignments = [
            {},
            {"target": "missing-agent"},
            self.assignment(),
            self.assignment(target="o/r#2"),
        ]
        for release, rows in (
            (ec._release_assignments, assignments),
            (ec._release_rejected_claims, [{"assignment": row} for row in assignments]),
        ):
            with self.subTest(release=release.__name__), patch.object(
                ec.claims, "release", side_effect=[RuntimeError("lost claim"), None]
            ) as mocked:
                release(rows)
                self.assertEqual(
                    mocked.call_args_list, [call("o/r#1", "codex"), call("o/r#2", "codex")]
                )

    def test_router_environment_restored_on_exception(self):
        with patch.dict(
            os.environ, ORCH_EXPLORATION_MODE="old-mode", ORCH_EXPLORATION_RATE="0.123"
        ):
            with self.assertRaisesRegex(RuntimeError, "probe failed"):
                with ec._temporary_router_env(mode="epsilon-greedy", rate=0.2):
                    self.assertEqual(os.environ["ORCH_EXPLORATION_MODE"], "epsilon-greedy")
                    self.assertEqual(os.environ["ORCH_EXPLORATION_RATE"], "0.200")
                    raise RuntimeError("probe failed")
            self.assertEqual(os.environ["ORCH_EXPLORATION_MODE"], "old-mode")
            self.assertEqual(os.environ["ORCH_EXPLORATION_RATE"], "0.123")

    def test_unseeded_probe_does_not_change_random_state(self):
        before = random.getstate()
        with ec._temporary_random_seed(None):
            self.assertEqual(random.getstate(), before)
        self.assertEqual(random.getstate(), before)
        self.assertEqual(ec._seed_candidates(17, 0), [17])
        self.assertEqual(ec._seed_candidates(None, 0), [None])

    def window(self, **kwargs):
        plan = {
            "candidate_task_types": [{"task_type": "implement", "recommended": True}],
            "supervised_collection": {
                "windows": [
                    {
                        "mode": "epsilon-greedy",
                        "eligible": True,
                        "max_exploratory_dispatches_per_day": 2,
                    }
                ]
            },
        }
        with (
            patch.object(ec.exploration_evidence_plan, "build_plan", return_value=plan),
            patch.object(ec.exploration_evidence_plan, "_capacity_snapshot", return_value={}),
            patch.object(
                ec.exploration_evidence_plan, "_backlog_items", return_value=[self.assignment()]
            ),
        ):
            return ec.build_window(requested_mode="epsilon-greedy", **kwargs)

    def test_window_rejects_zero_cap_and_zero_minimum_before_planning(self):
        result = self.window(max_dispatches=-1, min_exploratory_dispatches=0)
        self.assertFalse(result["eligible"])
        self.assertEqual(
            result["blocked_reasons"],
            ["max dispatch cap is zero", "min exploratory dispatches must be at least one"],
        )
        self.assertEqual(result["seed_search"]["attempted"], 0)

    def test_failed_seed_search_keeps_best_probe_for_diagnostics(self):
        rejected = [{"assignment": self.assignment(target="o/r#2"), "reason": "fixture"}]
        decisions = [({"assignments": []}, []), ({"assignments": [self.assignment()]}, rejected)]
        with patch.object(ec, "_plan_for_seed", side_effect=decisions) as probe:
            result = self.window(min_exploratory_dispatches=2, seed_search_limit=2)
        self.assertFalse(result["eligible"])
        self.assertEqual(result["seed_search"]["attempted"], 2)
        self.assertEqual(result["seed_search"]["selected_seed"], 1)
        self.assertEqual(result["direct_exploration_assignments"], 1)
        self.assertEqual(result["rejected_assignments"], rejected)
        self.assertIn("no router seed produced 2", result["blocked_reasons"][0])
        self.assertTrue(all(c.kwargs["dry_run"] for c in probe.call_args_list))

    def test_active_claim_race_releases_kept_and_rejected_claims(self):
        kept = self.assignment()
        rejected = [{"assignment": self.assignment(target="o/r#2")}]
        with (
            patch.object(
                ec,
                "_plan_for_seed",
                side_effect=[({"assignments": [kept, kept]}, []), ({"assignments": [kept]}, rejected)],
            ) as probe,
            patch.object(ec.claims, "release") as release,
        ):
            result = self.window(dry_run=False, rng_seed=9, min_exploratory_dispatches=2)
        self.assertEqual([c.kwargs["dry_run"] for c in probe.call_args_list], [True, False])
        self.assertEqual([c.kwargs["seed"] for c in probe.call_args_list], [9, 9])
        self.assertEqual(release.call_args_list, [call("o/r#2", "codex"), call("o/r#1", "codex")])
        self.assertFalse(result["eligible"])
        self.assertEqual(result["decision"]["assignments"], [])
        self.assertIn("active claim race", result["blocked_reasons"][0])

    def test_format_human_lists_blockers_and_assignments(self):
        window = {
            "mode": "epsilon-greedy",
            "exploration_rate": 0.2,
            "eligible": False,
            "read_only": True,
            "blocked_reasons": ["capacity unavailable"],
            "decision": {"assignments": [self.assignment()]},
        }
        text = ec.format_human(window)
        self.assertIn("blocked:\n  - capacity unavailable", text)
        self.assertIn("o/r#1: implement -> codex/local exploration", text)

    def cli(self, args):
        output = io.StringIO()
        with redirect_stdout(output):
            status = ec.main(args)
        return status, output.getvalue()

    def test_apply_requires_both_confirmation_and_environment(self):
        for enabled, confirmed in ((False, False), (False, True), (True, False)):
            with (
                self.subTest(enabled=enabled, confirmed=confirmed),
                patch.dict(os.environ, ORCH_EXPLORATION_EVIDENCE="1" if enabled else "0"),
                patch.object(ec, "build_window") as build,
            ):
                args = ["--apply", "--json"] + (["--confirm-window"] if confirmed else [])
                status, text = self.cli(args)
                self.assertEqual(status, 2)
                self.assertTrue(json.loads(text)["read_only"])
                self.assertIn("--confirm-window", json.loads(text)["error"])
                build.assert_not_called()
        self.assertFalse(ec.router.HANDOFF.exists())

    def test_dry_cli_forwards_window_options_without_dispatch(self):
        with patch.object(ec, "build_window", return_value={"read_only": True}) as build:
            status, text = self.cli(
                [
                    "--json",
                    "--mode",
                    "thompson-hybrid",
                    "--rate",
                    "0.1",
                    "--max-dispatches",
                    "2",
                    "--rng-seed",
                    "17",
                    "--seed-search-limit",
                    "5",
                    "--min-exploratory-dispatches",
                    "2",
                    "--exclude-target",
                    "o/r#3",
                ]
            )
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(text), {"read_only": True})
        build.assert_called_once_with(
            requested_mode="thompson-hybrid",
            rate=0.1,
            max_dispatches=2,
            dry_run=True,
            rng_seed=17,
            min_exploratory_dispatches=2,
            seed_search_limit=5,
            exclude_targets=["o/r#3"],
        )
        self.assertFalse(ec.router.HANDOFF.exists())

    def test_active_cli_blocks_ineligible_or_empty_window(self):
        for eligible, assignments in ((False, [self.assignment()]), (True, [])):
            window = {"eligible": eligible, "decision": {"assignments": assignments}}
            with (
                self.subTest(eligible=eligible),
                patch.dict(os.environ, ORCH_EXPLORATION_EVIDENCE="1"),
                patch.object(ec, "build_window", return_value=window),
            ):
                status, text = self.cli(["--apply", "--confirm-window", "--json"])
                self.assertEqual(status, 0)
                self.assertEqual(json.loads(text)["dispatch_result"], {"count": 0, "blocked": True})
        self.assertFalse(ec.router.HANDOFF.exists())

    def test_active_cli_writes_decision_before_stubbed_dispatch(self):
        decision = {"assignments": [self.assignment()]}

        def dispatch(actual, *, dry_run):
            self.assertFalse(dry_run)
            self.assertEqual(actual, decision)
            self.assertEqual(json.loads(ec.router.DECISION_JSON.read_text()), decision)
            return {"count": 1}

        with (
            patch.dict(os.environ, ORCH_EXPLORATION_EVIDENCE="1"),
            patch.object(ec, "build_window", return_value={"eligible": True, "decision": decision}),
            patch.object(ec.dispatcher, "run", side_effect=dispatch) as dispatched,
        ):
            status, text = self.cli(["--apply", "--confirm-window", "--json"])
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(text)["dispatch_result"], {"count": 1})
        dispatched.assert_called_once_with(decision, dry_run=False)


if __name__ == "__main__":
    unittest.main()
