"""Research intake fails closed on unreadable feeds and incomplete ownership discovery."""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import backlog
import tick


class ResearchFeedValidationTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.path = Path(scratch.name) / "research-backlog.json"

    def load(self, data, now=1000):
        self.path.write_text(json.dumps(data))
        return tick.load_research_feed(self.path, now=now)

    def test_array_and_scalar_feeds_are_unreadable(self):
        for data in ([], [1], None, 1, "feed"):
            with self.subTest(data=data):
                result = self.load(data)
                self.assertEqual((result["status"], result["items"]), ("unreadable", []))

    def test_invalid_timestamps_are_unreadable(self):
        for timestamp in ("today", "1000", True, [], {}, -1, 10**1000):
            with self.subTest(timestamp=timestamp):
                result = self.load({"generated_at": timestamp, "items": []})
                self.assertEqual((result["status"], result["items"]), ("unreadable", []))

    def test_nonfinite_timestamps_are_unreadable(self):
        for timestamp in (float("nan"), float("inf"), -float("inf")):
            with self.subTest(timestamp=timestamp):
                result = self.load({"generated_at": timestamp, "items": []})
                self.assertEqual((result["status"], result["items"]), ("unreadable", []))

    def test_future_timestamps_contribute_no_items(self):
        result = self.load({"generated_at": 1001, "items": [{"target": "o/r#1"}]})
        self.assertEqual((result["status"], result["items"]), ("unreadable", []))

    def test_malformed_items_are_unreadable(self):
        for items in (None, {}, "items", [None], [1], ["o/r#1"]):
            with self.subTest(items=items):
                result = self.load({"generated_at": 1000, "items": items})
                self.assertEqual((result["status"], result["items"]), ("unreadable", []))

    def test_fresh_stale_absent_and_invalid_json_statuses(self):
        self.assertEqual(tick.load_research_feed(self.path)["status"], "absent")
        self.path.write_text("{bad JSON")
        self.assertEqual(tick.load_research_feed(self.path)["status"], "unreadable")
        items = [{"target": "o/r#1"}]
        data = {"generated_at": 1000, "items": items}
        self.assertEqual(self.load(data)["items"], items)
        result = self.load(data, now=1000 + tick.RESEARCH_FEED_MAX_AGE_S + 1)
        self.assertEqual((result["status"], result["items"]), ("stale", []))

    def test_pr_discovery_refuses_a_full_page(self):
        for count in (200, 201):
            with self.subTest(count=count):
                response = subprocess.CompletedProcess([], 0, json.dumps([{}] * count), "")
                with patch("subprocess.run", return_value=response):
                    with self.assertRaisesRegex(RuntimeError, "ownership is incomplete"):
                        backlog.strict_fetch_prs("o/r")

    def test_issue_discovery_refuses_a_full_page(self):
        for count in (1000, 1001):
            with self.subTest(count=count):
                response = subprocess.CompletedProcess([], 0, json.dumps([{}] * count), "")
                with patch("subprocess.run", return_value=response):
                    with self.assertRaisesRegex(RuntimeError, "ownership is incomplete"):
                        backlog.strict_fetch_issues("o/r")

    def test_complete_pr_discovery_keeps_open_pr_exclusion(self):
        prs = [{"number": 2, "body": "Closes #1"}, *([{}] * 198)]
        response = subprocess.CompletedProcess([], 0, json.dumps(prs), "")
        issue = {"number": 1, "labels": ["priority:high"]}
        with patch("subprocess.run", return_value=response):
            feed = backlog.build_research_items(
                ["o/r"], lambda repo: [issue], backlog.strict_fetch_prs
            )
        self.assertEqual(feed["repos_failed"], [])
        self.assertEqual(feed["counts"]["open_pr"], 1)
        self.assertEqual(feed["items"], [])

    def test_truncated_repo_is_named_and_contributes_nothing(self):
        issue = {"number": 1, "labels": ["priority:high"]}

        def read(argv, **kwargs):
            count = 200 if argv[argv.index("-R") + 1] == "o/truncated" else 0
            return subprocess.CompletedProcess(argv, 0, json.dumps([{}] * count), "")

        with patch("subprocess.run", side_effect=read):
            feed = backlog.build_research_items(
                ["o/truncated", "o/complete"],
                lambda repo: [issue],
                backlog.strict_fetch_prs,
            )
        self.assertEqual(feed["repos_failed"], ["o/truncated"])
        self.assertEqual(feed["repos_read"], 1)
        self.assertEqual([item["target"] for item in feed["items"]], ["o/complete#1"])


if __name__ == "__main__":
    unittest.main()
