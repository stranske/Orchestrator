from __future__ import annotations

import contextlib
import copy
import io
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import codemod_lane as lane
import feedback
import paths
import range_lane_rollout as rollout


class CampaignOutcomeTests(unittest.TestCase):
    def setUp(self):
        temporary = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(temporary)
        self.enterContext(patch.dict(os.environ, {"ORCH_STATE_DIR": temporary}))
        self.db = self.root / "brain.db"
        self.enterContext(patch.object(feedback, "DB_PATH", self.db))
        self.campaign = json.loads(
            (paths.REPO_ROOT / "campaigns/gitignore-caches-2026-10.json").read_text()
        )
        with contextlib.closing(sqlite3.connect(self.db)) as conn, conn:
            conn.executescript(feedback.SCHEMA)

    def record_run(
        self, run_id, target, number, durability="durable", cost=2.5, source="ccusage", merged=1
    ):
        with contextlib.closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute(
                "INSERT INTO runs(run_id,target,pr_number) VALUES (?,?,?)", (run_id, target, number)
            )
            if durability is not None:
                conn.execute(
                    "INSERT INTO outcomes(run_id,merged,durability) VALUES (?,?,?)",
                    (run_id, merged, durability),
                )
            conn.execute(
                "INSERT INTO costs(run_id,cost_usd,source) VALUES (?,?,?)", (run_id, cost, source)
            )

    def test_each_merged_pr_requires_brain_attribution(self):
        self.record_run("one", "owner/repo#7", 91)
        result = lane.campaign_measure("owner/repo#7", [{"number": 91}, {"number": 92}])
        self.assertIsNone(result["durable"])
        self.assertIsNone(result["cost_usd"])
        self.record_run("two", "owner/repo#7", 92, durability="reverted", cost=1.5)
        result = lane.campaign_measure("owner/repo#7", [{"number": 91}, {"number": 92}])
        self.assertIs(result["durable"], False)
        self.assertEqual(result["cost_usd"], 4)

    def test_missing_run_outcome_keeps_measurements_unknown(self):
        self.record_run("one", "owner/repo#7", 91)
        self.record_run("two", "owner/repo#7", 91, durability=None)
        result = lane.campaign_measure("owner/repo#7", [{"number": 91}])
        self.assertIsNone(result["durable"])
        self.assertIsNone(result["cost_usd"])

    def test_unrecognized_durability_is_unknown(self):
        self.record_run("one", "owner/repo#7", 91, durability="unknown")
        result = lane.campaign_measure("owner/repo#7", [{"number": 91}])
        self.assertIsNone(result["durable"])
        self.assertEqual(result["cost_usd"], 2.5)

    def test_cost_includes_failed_attempts_with_and_without_prs(self):
        self.record_run("delivery", "owner/repo#7", 91)
        self.record_run("closed", "owner/repo#7", 92, merged=0, cost=1.5)
        self.record_run("no-pr", "owner/repo#7", None, merged=0, cost=2)
        self.record_run("other-target", "owner/repo#8", 91, cost=100)
        result = lane.campaign_measure("owner/repo#7", [{"number": 91}])
        self.assertIs(result["durable"], True)
        self.assertEqual(result["cost_usd"], 6)

    def test_cost_is_measured_before_any_delivery_merges(self):
        self.record_run("failed", "owner/repo#7", None, merged=0, cost=1.5)
        result = lane.campaign_measure("owner/repo#7", [])
        self.assertIsNone(result["durable"])
        self.assertEqual(result["cost_usd"], 1.5)

    def test_incomplete_failed_attempt_cost_keeps_total_unknown(self):
        self.record_run("delivery", "owner/repo#7", 91)
        for cost, source in [(0, "ccusage"), (1.5, "ledger")]:
            with self.subTest(cost=cost, source=source):
                with contextlib.closing(sqlite3.connect(self.db)) as conn, conn:
                    conn.execute("DELETE FROM runs WHERE run_id='failed'")
                    conn.execute("DELETE FROM outcomes WHERE run_id='failed'")
                    conn.execute("DELETE FROM costs WHERE run_id='failed'")
                self.record_run("failed", "owner/repo#7", 92, merged=0, cost=cost, source=source)
                result = lane.campaign_measure("owner/repo#7", [{"number": 91}])
                self.assertIs(result["durable"], True)
                self.assertIsNone(result["cost_usd"])

    def test_unfinished_attempt_keeps_total_cost_unknown(self):
        self.record_run("delivery", "owner/repo#7", 91)
        self.record_run("unfinished", "owner/repo#7", None, durability=None, cost=1.5)
        result = lane.campaign_measure("owner/repo#7", [{"number": 91}])
        self.assertIs(result["durable"], True)
        self.assertIsNone(result["cost_usd"])

    def test_complete_repositories_have_explicit_unknown_outcomes(self):
        campaign = copy.deepcopy(self.campaign)
        campaign.pop("source_target")

        def gh(args):
            # All targets already contain every entry; no issue needs filing.
            self.assertEqual(args[0], "api")
            self.assertTrue(args[1].endswith("/contents/.gitignore"))
            import base64

            return {
                "encoding": "base64",
                "content": base64.b64encode("\n".join(lane.IGNORE_ENTRIES).encode()).decode(),
            }

        program = lane.file_targets(campaign, gh=gh)
        saved = json.loads(lane.campaign_program_path(campaign).read_text())
        self.assertEqual(saved, program)
        for row in saved["repos"].values():
            self.assertEqual(row["state"], "already-complete")
            self.assertNotIn("target", row)
            self.assertEqual(
                [row[field] for field in ("merged", "durable", "cost_usd")],
                [None] * 3,
            )

    def test_record_campaign_persists_outcomes_without_dispatch(self):
        complete, durable, pending, unattributed, unmerged, partial = self.campaign["campaign"][
            "repos"
        ]
        program = {
            "campaign_id": self.campaign["campaign"]["id"],
            "repos": {
                repo: {"target": f"{repo}#10", "state": "filed"}
                for repo in self.campaign["campaign"]["repos"]
            },
        }
        # Exercise an older receipt that has no outcome fields.
        program["repos"][complete] = {"state": "already-complete", "missing": []}
        program["repos"][durable]["dispatches"] = [{"target": f"{durable}#10"}]
        lane._write_program(lane.campaign_program_path(self.campaign), program)
        self.record_run("durable", f"{durable}#10", 91)
        self.record_run("failed", f"{durable}#10", None, merged=0, cost=1.5)
        self.record_run("pending", f"{pending}#10", 91, "pending", 0, "ledger")
        self.record_run("partial", f"{partial}#10", 91)

        def gh(args):
            repo = args[args.index("--repo") + 1]
            if args[:2] == ["issue", "view"]:
                return {
                    "state": "CLOSED",
                    "body": f"<!-- codemod-campaign:{program['campaign_id']} -->",
                    "labels": [],
                }
            self.assertEqual(args[:2], ["pr", "list"])
            return [
                {
                    "number": number,
                    "state": "CLOSED" if repo == unmerged else "MERGED",
                    "mergedAt": None if repo == unmerged else "2026-10-06T00:00:00Z",
                    "headRefOid": f"head-{number}",
                    "url": f"https://github.com/{repo}/pull/{number}",
                    "closingIssuesReferences": [{"number": 10}],
                }
                for number in ([91, 92] if repo == partial else [91])
            ]

        self.enterContext(patch.object(lane, "_gh", gh))
        self.enterContext(patch.object(rollout.backlog, "load_scoped_blockers", return_value={}))
        self.enterContext(patch.object(rollout, "_capacity_snapshot", return_value={}))
        self.enterContext(patch.object(rollout.router, "learned_ranks", return_value={}))
        dispatch = self.enterContext(
            patch.object(rollout.dispatcher, "run", side_effect=AssertionError("report dispatched"))
        )
        os.environ.pop(rollout.ENV_FLAG, None)
        campaign_file = self.root / "campaign.json"
        campaign_file.write_text(json.dumps(self.campaign))
        argv = ["--campaign", str(campaign_file), "--json"]
        before = lane.campaign_program_path(self.campaign).read_bytes()
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(rollout.main(argv), 0)
        preview = json.loads(output.getvalue())
        self.assertEqual(lane.campaign_program_path(self.campaign).read_bytes(), before)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(rollout.main([*argv, "--record-campaign"]), 0)
        result = json.loads(output.getvalue())
        saved = json.loads(lane.campaign_program_path(self.campaign).read_text())
        self.assertEqual(saved, preview["campaign"])
        self.assertEqual(saved, result["campaign"])
        self.assertEqual(saved["campaign_id"], program["campaign_id"])
        self.assertEqual(set(saved["repos"]), set(self.campaign["campaign"]["repos"]))
        for repo, expected in [
            (complete, (None, None, None)),
            (durable, (True, True, 4)),
            (pending, (True, None, None)),
            (unattributed, (True, None, None)),
            (unmerged, (False, None, None)),
            (partial, (True, None, None)),
        ]:
            row = saved["repos"][repo]
            self.assertEqual((row["merged"], row["durable"], row["cost_usd"]), expected)
            if repo != complete:
                self.assertEqual(row["target"], program["repos"][repo]["target"])
                self.assertEqual(row["delivery_prs"][0]["headRefOid"], "head-91")
        self.assertEqual(
            saved["repos"][durable]["dispatches"],
            program["repos"][durable]["dispatches"],
        )
        self.assertIn("campaign: repos 6, dispatched 0, merged 4", rollout.format_human(result))
        dispatch.assert_not_called()
