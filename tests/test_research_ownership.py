"""Production research discovery must respect pause and sole-writer ownership labels."""

import json
import sqlite3
import subprocess
import unittest
from unittest.mock import patch

import backlog
import feedback
import research_scheduler
import tick

PAUSE_LABELS = ("agents:paused", "agents:auto-pilot-pause")
IN_PROGRESS_LABELS = ("status:in-progress", "status: in-progress")
OWNERSHIP_LABELS = (
    "agent:codex",
    "agent:claude",
    "agent:cursor",
    "agent:gemini",
    "agent:aider",
    "agent:auto",
    "agent:retry",
    "agent:rate-limited",
    "agent:needs-attention",
    "agent:codex-invite",
    "agent:future-worker",
)


class ResearchOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript(feedback.SCHEMA)
        feedback._migrate_schema(self.conn)
        self.addCleanup(self.conn.close)

    def feed(self, *labels):
        issue = {
            "number": 1,
            "labels": [{"name": lab} for lab in ("priority:high", *labels)],
            "title": "Unowned research subject",
            "body": "Implement the parser",
        }
        return backlog.build_research_items(["o/r"], lambda repo: [issue], lambda repo: [])

    def candidates(self, items):
        return research_scheduler.research_job_candidates(
            items,
            {"codex": 3, "cursor": 3, "gemini": 3},
            [],
            conn=self.conn,
            weights=[],
        )

    def test_paused_issue_is_excluded(self):
        feed = self.feed("agents:paused")
        self.assertEqual(feed["counts"]["eligible"], 0)
        self.assertEqual(self.candidates(feed["items"]), [])

    def test_assigned_issue_is_excluded(self):
        feed = self.feed("agent:codex")
        self.assertEqual(feed["counts"]["eligible"], 0)
        self.assertEqual(self.candidates(feed["items"]), [])

    def test_auto_pilot_pause_remains_excluded(self):
        feed = self.feed("agents:auto-pilot-pause")
        self.assertEqual(feed["counts"]["eligible"], 0)
        self.assertEqual(self.candidates(feed["items"]), [])

        # Keep the existing pause control beside both reproduced failures and an eligible
        # subject: rejecting ownership must not discard the rest of a repository's feed.
        issues = [
            {
                "number": number,
                "labels": ["priority:high", *labels],
                "title": "Research subject",
                "body": "Implement the parser",
            }
            for number, labels in enumerate(
                (
                    ["agents:auto-pilot-pause"],
                    ["agents:paused"],
                    ["agent:codex"],
                    [],
                ),
                start=1,
            )
        ]
        feed = backlog.build_research_items(["o/r"], lambda repo: issues, lambda repo: [])
        self.assertEqual(feed["counts"]["open_issues"], 4)
        self.assertEqual(feed["counts"]["eligible"], 1)
        self.assertEqual([item["target"] for item in feed["items"]], ["o/r#4"])
        self.assertEqual([job["target"] for job in self.candidates(feed["items"])], ["o/r#4"])

    def test_every_pause_and_agent_family_is_excluded(self):
        for label in (*PAUSE_LABELS, *OWNERSHIP_LABELS, *IN_PROGRESS_LABELS):
            for spelling in (label, f"  {label.upper()}  "):
                with self.subTest(label=spelling):
                    feed = self.feed(spelling)
                    self.assertEqual(feed["counts"]["eligible"], 0)
                    self.assertEqual(
                        feed["counts"].get("owned_or_paused", 0)
                        + feed["counts"].get("excluded_label", 0),
                        1,
                    )
                    self.assertEqual(self.candidates(feed["items"]), [])

    def test_scheduler_rechecks_injected_research_ownership(self):
        for label in (*PAUSE_LABELS, *OWNERSHIP_LABELS, *IN_PROGRESS_LABELS):
            for spelling in (label, f"  {label.upper()}  "):
                with self.subTest(label=spelling):
                    item = {
                        "target": "o/r#1",
                        "task_type": "implement",
                        "lane": "research",
                        "labels": ["priority:high", spelling],
                        "body": "Implement the parser",
                    }
                    with patch.object(research_scheduler, "should_test") as select:
                        self.assertEqual(self.candidates([item]), [])
                        select.assert_not_called()

        # Missing or malformed feed labels cannot establish that the subject is unowned.
        # An explicitly empty list remains a known unowned subject (positive control below).
        for labels in (None, {}, "agent:codex", [None], [{"name": "agent:codex"}]):
            with self.subTest(unknown_labels=labels):
                item = {
                    "target": "o/r#1",
                    "task_type": "implement",
                    "lane": "research",
                    "body": "Implement the parser",
                }
                if labels is not None:
                    item["labels"] = labels
                skipped = []
                with patch.object(research_scheduler, "should_test") as select:
                    jobs = research_scheduler.research_job_candidates(
                        [item],
                        {"codex": 3, "cursor": 3, "gemini": 3},
                        [],
                        conn=self.conn,
                        weights=[],
                        skipped=skipped,
                    )
                    self.assertEqual(jobs, [])
                    self.assertEqual(skipped[0]["reason"], "owned_or_paused")
                    select.assert_not_called()
                jobs = self.candidates([item, *self.feed()["items"]])
                self.assertEqual([job["target"] for job in jobs], ["o/r#1"])

    def test_unowned_prioritised_issue_reaches_scheduler(self):
        for label in (None, "agent-ready", "agents:keepalive"):
            with self.subTest(label=label):
                feed = self.feed(*([label] if label else []))
                self.assertEqual(feed["counts"]["eligible"], 1)
                jobs = self.candidates(feed["items"])
                self.assertEqual([job["target"] for job in jobs], ["o/r#1"])

    def test_ownership_is_rechecked_at_launch(self):
        # The feed was unowned when read. A writer or pause added afterward must stop
        # the production tick before it reserves the subject or prepares any arms.
        items = self.feed()["items"]
        cap = {"agents": {agent: {"state": "ok"} for agent in ("codex", "cursor", "gemini")}}
        for label in (*PAUSE_LABELS, *OWNERSHIP_LABELS, *IN_PROGRESS_LABELS):
            with self.subTest(label=label):
                payload = {
                    "title": "Research subject",
                    "body": "Implement the parser",
                    "state": "OPEN",
                    "labels": [{"name": label}],
                }
                result = subprocess.CompletedProcess([], 0, json.dumps(payload), "")
                with (
                    patch("subprocess.run", return_value=result) as read,
                    patch.object(tick.claims, "active_claims", return_value={}),
                    patch.object(tick.claims, "reap_stale"),
                    patch.object(tick.claims, "claim") as claim,
                    patch.object(tick.exp_abcd, "prepare_arms") as prepare,
                    patch.object(tick.capabilities, "production_heartbeat"),
                ):
                    self.assertIsNone(tick._fetch_issue_body("o/r#1"))
                    self.assertIn("labels", read.call_args.args[0][-1])
                    outcome = tick.research_tick(
                        items,
                        cap,
                        dry_run=False,
                        env={"ORCH_RESEARCH_ARM": "1"},
                        conn=self.conn,
                        hyps=[],
                    )
                    self.assertTrue(outcome["planned"])
                    self.assertEqual(outcome["planned"][0]["active_status"], "no_issue_body")
                    self.assertFalse(outcome["active"])
                    claim.assert_not_called()
                    prepare.assert_not_called()

    def test_launch_refuses_unknown_labels_and_accepts_unowned_open_issue(self):
        payload = {"title": "Subject", "body": "Implement the parser", "state": "OPEN"}
        for labels in (None, {}, [None], [{"name": None}], []):
            with self.subTest(labels=labels):
                result = subprocess.CompletedProcess(
                    [], 0, json.dumps({**payload, "labels": labels}), ""
                )
                with patch("subprocess.run", return_value=result):
                    self.assertEqual(
                        tick._fetch_issue_body("o/r#1"),
                        "# Subject\n\nImplement the parser" if labels == [] else None,
                    )

    def test_launch_stays_off_in_shadow_or_without_arm_flag(self):
        items = self.feed()["items"]
        cap = {"agents": {agent: {"state": "ok"} for agent in ("codex", "cursor", "gemini")}}
        for dry_run, env in (
            (True, {"ORCH_RESEARCH_ARM": "1"}),
            (False, {}),
            (False, {"ORCH_RESEARCH_ARM": "0"}),
        ):
            with self.subTest(dry_run=dry_run, env=env):
                with (
                    patch.object(tick.claims, "active_claims", return_value={}),
                    patch.object(tick.claims, "reap_stale"),
                    patch.object(tick.claims, "claim") as claim,
                    patch.object(tick, "_fetch_issue_body") as read,
                    patch.object(tick.exp_abcd, "prepare_arms") as prepare,
                    patch.object(tick.capabilities, "production_heartbeat"),
                ):
                    result = tick.research_tick(
                        items,
                        cap,
                        dry_run=dry_run,
                        env=env,
                        conn=self.conn,
                        hyps=[],
                    )
                    self.assertTrue(result["planned"])
                    self.assertFalse(result["active"])
                    read.assert_not_called()
                    claim.assert_not_called()
                    prepare.assert_not_called()


if __name__ == "__main__":
    unittest.main()
