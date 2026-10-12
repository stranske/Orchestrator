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

    def test_every_pause_and_agent_family_is_excluded(self):
        for label in (*PAUSE_LABELS, *OWNERSHIP_LABELS):
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
        for label in (*PAUSE_LABELS, *OWNERSHIP_LABELS):
            with self.subTest(label=label):
                item = {
                    "target": "o/r#1",
                    "task_type": "implement",
                    "lane": "research",
                    "labels": ["priority:high", label],
                    "body": "Implement the parser",
                }
                with patch.object(research_scheduler, "should_test") as select:
                    self.assertEqual(self.candidates([item]), [])
                    select.assert_not_called()

    def test_unowned_prioritised_issue_reaches_scheduler(self):
        for label in (None, "agent-ready", "agents:keepalive"):
            with self.subTest(label=label):
                feed = self.feed(*([label] if label else []))
                self.assertEqual(feed["counts"]["eligible"], 1)
                jobs = self.candidates(feed["items"])
                self.assertEqual([job["target"] for job in jobs], ["o/r#1"])

    def test_ownership_is_rechecked_at_launch(self):
        for label in (*PAUSE_LABELS, *OWNERSHIP_LABELS, "status:in-progress"):
            with self.subTest(label=label):
                payload = {
                    "title": "Research subject",
                    "body": "Implement the parser",
                    "state": "OPEN",
                    "labels": [{"name": label}],
                }
                result = subprocess.CompletedProcess([], 0, json.dumps(payload), "")
                with patch("subprocess.run", return_value=result) as read:
                    self.assertIsNone(tick._fetch_issue_body("o/r#1"))
                    self.assertIn("labels", read.call_args.args[0][-1])

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
        for dry_run, env in ((True, {"ORCH_RESEARCH_ARM": "1"}), (False, {})):
            with self.subTest(dry_run=dry_run, env=env):
                with (
                    patch.object(tick.claims, "active_claims", return_value={}),
                    patch.object(tick.claims, "reap_stale"),
                    patch.object(tick, "_fetch_issue_body") as read,
                    patch.object(tick.exp_abcd, "prepare") as prepare,
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
                    prepare.assert_not_called()


if __name__ == "__main__":
    unittest.main()
