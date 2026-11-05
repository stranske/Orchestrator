"""Offline response replays for fleet measures; also runnable with unittest.

Fixtures project the REST response fields used by the readers. The canary cohort is
recorded from Workflows/config/consumer_sync_canaries.json; measurement examples
are synthetic, with fixed UTC times, rather than claims about live campaign results.
"""

from __future__ import annotations

import copy
import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import backlog
import switch_review as review

FIXTURE = Path(__file__).parent / "fixtures" / "switch_review_fleet.json"


class FleetDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.data = json.loads(FIXTURE.read_text())
        self.now = self.data["now"]
        self.calls = []
        self.failed = set()
        self.patcher = patch.object(backlog, "SUPPORTED_REPOS", ["stranske/Orchestrator"])
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def gh(self, args, **kwargs):
        self.assertEqual(args[0], "api")
        path = args[1]
        self.calls.append(args)
        if any(part in path for part in self.failed):
            return False, "", "unmeasured: HTTP 403 fixture read refused"
        if "/actions/" in path:
            self.assertIn("--paginate", args)
            payload = self.data["gate"]
        elif "/contents/" in path:
            self.assertIn("Accept: application/vnd.github.raw+json", args)
            payload = self.data["canary_config"]
        elif "state=open" in path:
            self.assertIn("--paginate", args)
            repo = path.split("/pulls?")[0].removeprefix("repos/")
            payload = self.data["canary_prs"][repo]
        elif "state=all" in path:
            self.assertIn("sort=updated&direction=desc", path)
            self.assertIn("page=1", path)
            payload = self.data["prs"]
        elif "/issues/" in path:
            payload = self.data["issues"][path.rsplit("/", 1)[1]]
        else:
            self.fail(f"unexpected gh call: {args}")
        return True, json.dumps(payload), ""

    def opener(self, args, **kwargs):
        self.assertEqual(args[-1], "--json")
        self.assertTrue(args[-2].endswith("/.codex/bin/opener-cap-health.py"))
        self.assertEqual(kwargs["timeout"], review.GH_TIMEOUT_S)
        self.assertFalse(kwargs["check"])
        return SimpleNamespace(returncode=0, stdout=json.dumps(self.data["opener"]))

    def measure(self, **kwargs):
        return review.fleet_delivery_measures(
            now=self.now, gh_fn=self.gh, opener_runner=kwargs.get("opener_runner", self.opener)
        )

    def test_all_five_rows_and_values(self):
        report = self.measure()
        self.assertEqual(report["as_of"], self.now)
        rows = report["rows"]
        self.assertEqual(list(rows), list(review.FLEET_DELIVERY_NAMES))
        self.assertIn(
            "median 15 min (2 PR/issue pairs; 0 still open", rows["merge_to_close"]["value"]
        )
        self.assertIn("2.0 days", rows["oldest_canary"]["value"])
        self.assertIn("opened 2/day, merged 1.5/day", rows["released_flow"]["value"])
        self.assertIn("human-held exempt 2; coverage exempt 1", rows["opener_cap"]["value"])
        for key in ("merge_to_close", "oldest_canary", "released_flow", "opener_cap"):
            self.assertTrue(rows[key]["evidence_class"])

    def test_pull_request_event_never_infers_trigger_action(self):
        row = self.measure()["rows"]["gate_cancellations"]
        self.assertIsNone(row["value"])
        self.assertIn("run-ID-keyed github.event.action", row["reason"])
        self.assertIn("labeled/synchronize", row["reason"])

    def test_failed_gate_read_is_not_a_number(self):
        self.failed.add("/actions/")
        row = self.measure()["rows"]["gate_cancellations"]
        self.assertIsNone(row["value"])
        self.assertEqual(row["reason"], "gh HTTP 403 fixture read refused")

    def test_recorded_gh_failure_stays_unmeasured(self):
        recorded = json.loads(FIXTURE.with_name("switch_review_fleet_recorded.json").read_text())
        rows = review.fleet_delivery_measures(
            now=self.now,
            gh_fn=lambda args: (False, "", recorded["gh_failure"]),
            opener_runner=self.opener,
        )["rows"]
        for key in ("gate_cancellations", "merge_to_close", "oldest_canary", "released_flow"):
            self.assertIsNone(rows[key]["value"])
            self.assertIn("gh error connecting to api.github.com", rows[key]["reason"])

    def test_recorded_rest_pr_projection(self):
        recorded = json.loads(FIXTURE.with_name("switch_review_fleet_recorded.json").read_text())
        now = review._fleet_stamp(recorded["pr"]["updated_at"])
        rows = review._fleet_recent_prs(
            lambda args: (True, json.dumps([recorded["pr"]]), ""),
            "stranske/Orchestrator",
            now,
        )
        self.assertEqual(rows[0]["number"], 1130)
        self.assertIsNone(rows[0]["merged_at"])

    def test_each_gh_failure_preserves_every_row(self):
        for endpoint, key in (
            ("/actions/", "gate_cancellations"),
            ("state=all", "merge_to_close"),
            ("/issues/", "released_flow"),
            ("/contents/", "oldest_canary"),
            ("state=open", "oldest_canary"),
        ):
            with self.subTest(endpoint=endpoint):
                self.failed = {endpoint}
                rows = self.measure()["rows"]
                self.assertEqual(len(rows), 5)
                self.assertIsNone(rows[key]["value"])
                self.assertTrue(rows[key]["reason"].startswith("gh HTTP 403"))
                self.assertIsNotNone(rows["opener_cap"]["value"])

    def test_failed_canary_repo_does_not_print_a_partial_oldest(self):
        self.failed = {"repos/stranske/trip-planner/pulls"}
        rows = self.measure()["rows"]
        self.assertIsNone(rows["oldest_canary"]["value"])
        self.assertIsNotNone(rows["released_flow"]["value"])

    def test_canary_drafts_and_later_pages_are_included(self):
        row = self.measure()["rows"]["oldest_canary"]
        self.assertIn("stranske/trip-planner#42", row["value"])

    def test_no_open_sync_prs_is_measured_absence(self):
        for repo in self.data["canary_prs"]:
            self.data["canary_prs"][repo] = [[]]
        row = self.measure()["rows"]["oldest_canary"]
        self.assertIn("none open", row["value"])
        self.assertNotEqual(row["evidence_class"], "unmeasured")

    def test_no_closed_pairs_is_not_zero_latency(self):
        self.data["prs"] = []
        rows = self.measure()["rows"]
        self.assertIsNone(rows["merge_to_close"]["value"])
        self.assertIn("0 observed pairs", rows["merge_to_close"]["reason"])
        self.assertIn("opened 0/day, merged 0/day", rows["released_flow"]["value"])

    def test_still_open_issues_are_named_beside_observed_median(self):
        self.data["prs"][2]["merged_at"] = "2026-11-04T01:00:00Z"
        row = self.measure()["rows"]["merge_to_close"]
        self.assertIn("median 15 min", row["value"])
        self.assertIn("1 still open", row["value"])

    def test_prs_are_counted_once_with_multiple_released_issues(self):
        self.data["prs"][0]["body"] += "\nCloses #2\nCloses #2"
        row = self.measure()["rows"]["released_flow"]
        self.assertIn("opened 2/day, merged 1.5/day", row["value"])

    def test_closure_before_merge_does_not_produce_negative_latency(self):
        self.data["issues"]["1"]["closed_at"] = "2026-11-03T00:00:00Z"
        row = self.measure()["rows"]["merge_to_close"]
        self.assertIn("median 20 min", row["value"])
        self.assertIn("1 closed before merge", row["value"])

    def test_recent_merges_of_old_prs_count_toward_merge_flow(self):
        self.data["prs"][0]["created_at"] = "2026-10-01T00:00:00Z"
        row = self.measure()["rows"]["released_flow"]
        self.assertIn("opened 1.5/day, merged 1.5/day", row["value"])

    def test_missing_exemption_count_is_not_zero(self):
        del self.data["opener"]["coverage_exempt_count"]
        row = self.measure()["rows"]["opener_cap"]
        self.assertIsNone(row["value"])
        self.assertIn("both exempt counts", row["reason"])

    def test_opener_counts_never_invent_a_cap(self):
        row = self.measure()["rows"]["opener_cap"]
        self.assertIn("7 owned", row["value"])
        self.assertIn("cap unmeasured — would need", row["value"])
        self.assertIn("counts only", row["evidence_class"])

    def test_opener_failure_does_not_stop_other_rows(self):
        for failure in (FileNotFoundError(), subprocess.TimeoutExpired("opener", 30)):
            with self.subTest(failure=type(failure).__name__):
                def fail(*args, **kwargs):
                    raise failure

                rows = self.measure(opener_runner=fail)["rows"]
                self.assertEqual(len(rows), 5)
                self.assertIsNone(rows["opener_cap"]["value"])
                self.assertIsNotNone(rows["merge_to_close"]["value"])

    def test_excluded_repository_is_never_queried(self):
        backlog.SUPPORTED_REPOS.append("stranske/Collab-Admin")
        self.measure()
        self.assertNotIn("Collab-Admin", json.dumps(self.calls))

    def test_invalid_json_preserves_other_rows(self):
        def gh(args, **kwargs):
            if "/actions/" in args[1]:
                return True, "not JSON", ""
            return self.gh(args, **kwargs)

        report = review.fleet_delivery_measures(
            now=self.now, gh_fn=gh, opener_runner=self.opener
        )
        self.assertEqual(report["rows"]["gate_cancellations"]["reason"], "gh invalid JSON response")
        self.assertIsNotNone(report["rows"]["released_flow"]["value"])

    def test_unexpected_reader_exception_preserves_other_rows(self):
        with patch.object(review, "_fleet_oldest_canary", side_effect=RuntimeError("broken")):
            rows = self.measure()["rows"]
        self.assertIsNone(rows["oldest_canary"]["value"])
        self.assertIn("RuntimeError", rows["oldest_canary"]["reason"])
        self.assertIsNotNone(rows["released_flow"]["value"])

    def test_recent_pr_pagination_reaches_second_page(self):
        first = [copy.deepcopy(self.data["prs"][0]) for _ in range(100)]
        responses = iter([first, [self.data["prs"][1]]])
        calls = []

        def gh(args):
            calls.append(args)
            return True, json.dumps(next(responses)), ""

        rows = review._fleet_recent_prs(gh, "stranske/Orchestrator", self.now)
        self.assertEqual(len(rows), 101)
        self.assertIn("page=2", calls[1][1])

    def test_pr_pagination_budget_is_not_reported_as_complete(self):
        batch = [self.data["prs"][0]] * 100
        with self.assertRaisesRegex(review._FleetReadError, "complete updated PR pagination"):
            review._fleet_recent_prs(
                lambda args: (True, json.dumps(batch), ""), "stranske/Orchestrator", self.now
            )

    def test_missing_timestamps_do_not_become_measured_zero(self):
        self.data["prs"][0]["created_at"] = None
        row = self.measure()["rows"]["released_flow"]
        self.assertIsNone(row["value"])
        self.assertIn("UTC GitHub timestamp", row["reason"])

    def test_text_output_retains_five_rows_with_partial_failure(self):
        self.failed.add("/actions/")
        section = self.measure()
        report = {
            "generated_at": self.now,
            "review_days": 7,
            "held_off": [],
            "on_but_idle": [],
            "unconditioned": [],
            "raise_count": 0,
            "fleet_delivery_measures": section,
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "review.txt"
            review.write_text_report(report, path)
            text = path.read_text()
        self.assertIn("## Fleet delivery measures", text)
        self.assertIn("unmeasured — gh HTTP 403 fixture read refused", text)
        for name in review.FLEET_DELIVERY_NAMES.values():
            self.assertEqual(text.count(name + ":"), 1)
        self.assertIn("median 15 min", text)
        self.assertIn("[GitHub REST", text)

    def test_cli_collects_rows_for_json_and_text(self):
        import adversarial

        section = self.measure()
        report = {
            "generated_at": self.now,
            "review_days": 7,
            "held_off": [],
            "on_but_idle": [],
            "unconditioned": [],
            "raise_count": 0,
        }
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(
                os.environ,
                {
                    "ORCH_VALUE_CHAIN_MONITOR": "0",
                    "ORCH_DISABLE_STEPS": "issue-size-quality",
                },
            ),
            patch.object(review, "review", return_value=report),
            patch.object(review, "fleet_delivery_measures", return_value=section) as collect,
            patch.object(review, "credential_exposure", return_value={}),
            patch.object(review.python_standard_audit, "snapshot", return_value={}),
            patch.object(adversarial, "record_shape_measurement", return_value=False),
            redirect_stdout(io.StringIO()) as stdout,
        ):
            path = Path(tmp) / "report.txt"
            status = review.main(["--env", "process", "--json", "--text-out", str(path)])
            self.assertEqual(status, 0)
            collect.assert_called_once_with(now=self.now)
            self.assertEqual(json.loads(stdout.getvalue())["fleet_delivery_measures"], section)
            for name in review.FLEET_DELIVERY_NAMES.values():
                self.assertIn(name + ":", path.read_text())


if __name__ == "__main__":
    unittest.main()
