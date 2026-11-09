"""Offline response replays for fleet measures; also runnable with unittest.

Fixtures project the REST and GraphQL fields used by the readers. The canary cohort is
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
        if path == "graphql":
            self.assertIn("closingIssuesReferences(first:100)", " ".join(args))
            self.assertIn("pageInfo{hasNextPage}", " ".join(args))
            self.assertIn("owner=stranske", args)
            self.assertIn("name=Orchestrator", args)
            number = next(arg.removeprefix("number=") for arg in args if arg.startswith("number="))
            payload = self.data["pr_details"][number]
        elif "/actions/" in path:
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
            ("graphql", "merge_to_close"),
            ("graphql", "released_flow"),
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

        # A failed detail read is shared by both PR measures, never retried into
        # inconsistent snapshots or treated as an empty connection.
        self.failed = {"graphql"}
        self.calls.clear()
        rows = self.measure()["rows"]
        self.assertEqual(sum(args[1] == "graphql" for args in self.calls), 1)
        for key in ("merge_to_close", "released_flow"):
            self.assertIsNone(rows[key]["value"])
        self.assertIsNotNone(rows["oldest_canary"]["value"])

        self.failed.clear()
        original = copy.deepcopy(self.data["pr_details"]["10"])
        invalid_details = [None, [], {}, {"data": None}, {"errors": [{"message": "refused"}]}]
        for connection in (
            None,
            {},
            {"nodes": [], "pageInfo": {"hasNextPage": True}},
            {"nodes": [], "pageInfo": {"hasNextPage": None}},
            {"nodes": [], "pageInfo": {"hasNextPage": 0}},
            {"nodes": None, "pageInfo": {"hasNextPage": False}},
            {"nodes": [None], "pageInfo": {"hasNextPage": False}},
        ):
            detail = copy.deepcopy(original)
            detail["data"]["repository"]["pullRequest"]["closingIssuesReferences"] = connection
            invalid_details.append(detail)
        for node in (
            {},
            {"number": True, "repository": {"nameWithOwner": "stranske/Orchestrator"}},
            {"number": 0, "repository": {"nameWithOwner": "stranske/Orchestrator"}},
            {"number": "1", "repository": {"nameWithOwner": "stranske/Orchestrator"}},
            {"number": 1, "repository": None},
            {"number": 1, "repository": {"nameWithOwner": ""}},
        ):
            detail = copy.deepcopy(original)
            detail["data"]["repository"]["pullRequest"]["closingIssuesReferences"]["nodes"] = [node]
            invalid_details.append(detail)
        for detail in invalid_details:
            with self.subTest(detail=detail):
                self.data["pr_details"]["10"] = detail
                rows = self.measure()["rows"]
                self.assertEqual(len(rows), 5)
                for key in ("merge_to_close", "released_flow"):
                    self.assertIsNone(rows[key]["value"])
                    self.assertEqual(rows[key]["evidence_class"], "unmeasured")
                    self.assertRegex(rows[key]["reason"], "closingIssuesReferences|gh API")
                self.assertIsNotNone(rows["oldest_canary"]["value"])
                self.assertIsNotNone(rows["opener_cap"]["value"])

        self.data["pr_details"]["10"] = original
        gh = self.gh

        def malformed_json(args, **kwargs):
            if args[1] == "graphql":
                return True, "{broken", ""
            return gh(args, **kwargs)

        with patch.object(self, "gh", side_effect=malformed_json):
            rows = self.measure()["rows"]
        for key in ("merge_to_close", "released_flow"):
            self.assertIsNone(rows[key]["value"])
            self.assertEqual(rows[key]["reason"], "gh invalid JSON response")

        # Unmerged #12 is read only by flow. Its failed structured-link read must
        # invalidate flow even when the body/branch independently names an issue,
        # while preserving the complete merge-to-closure population.
        self.data["prs"][2]["body"] = "Closes #3"
        self.data["prs"][2]["head"]["ref"] = "codex/issue-3"
        failures = {
            "refused": (False, "", "unmeasured: HTTP 403 fixture read refused"),
            "invalid_json": (True, "{broken", ""),
            **{
                name: (True, json.dumps(payload), "")
                for name, payload in self.data["malformed_pr_details"].items()
            },
        }
        for name, response in failures.items():
            with self.subTest(flow_only_detail_failure=name):
                self.calls.clear()

                def fail_flow_detail(args, **kwargs):
                    if args[1] == "graphql" and "number=12" in args:
                        self.calls.append(args)
                        return response
                    return gh(args, **kwargs)

                with patch.object(self, "gh", side_effect=fail_flow_detail):
                    rows = self.measure()["rows"]
                self.assertEqual(len(rows), 5)
                self.assertIsNone(rows["released_flow"]["value"])
                self.assertEqual(rows["released_flow"]["evidence_class"], "unmeasured")
                self.assertRegex(
                    rows["released_flow"]["reason"],
                    "gh HTTP 403|gh invalid JSON|gh API|closingIssuesReferences",
                )
                self.assertIn("median 15 min (2 PR/issue pairs", rows["merge_to_close"]["value"])
                for key in ("oldest_canary", "opener_cap"):
                    self.assertIsNotNone(rows[key]["value"])
                self.assertEqual(sum(args[1] == "graphql" for args in self.calls), 4)
                self.assertFalse(any(args[1].endswith("/issues/3") for args in self.calls))

        # A complete empty connection still permits the existing text attribution.
        detail = self.data["pr_details"]["12"]["data"]["repository"]["pullRequest"]
        detail["closingIssuesReferences"]["nodes"] = []
        self.calls.clear()
        rows = self.measure()["rows"]
        self.assertIn("opened 2/day, merged 1.5/day", rows["released_flow"]["value"])
        self.assertEqual(sum(args[1].endswith("/issues/3") for args in self.calls), 1)

    def test_failed_or_malformed_canary_repo_does_not_print_a_partial_oldest(self):
        self.failed = {"repos/stranske/trip-planner/pulls"}
        rows = self.measure()["rows"]
        self.assertIsNone(rows["oldest_canary"]["value"])
        self.assertIsNotNone(rows["released_flow"]["value"])
        self.failed.clear()
        repo = "stranske/trip-planner"
        for payload in ([], {}, [None], [{}], [[], {}]):
            with self.subTest(payload=payload):
                self.data["canary_prs"][repo] = payload
                section = self.measure()
                row = section["rows"]["oldest_canary"]
                self.assertIsNone(row["value"])
                self.assertEqual(row["evidence_class"], "unmeasured")
                self.assertIn("complete paginated canary PR arrays", row["reason"])
                report = {
                    "generated_at": self.now,
                    "review_days": 7,
                    "held_off": [],
                    "on_but_idle": [],
                    "unconditioned": [],
                    "raise_count": 0,
                    "fleet_delivery_measures": section,
                }
                text = review.format_report(report)
                for name in review.FLEET_DELIVERY_NAMES.values():
                    self.assertEqual(text.count(name + ":"), 1)
                self.assertIn("unmeasured — would need complete paginated canary PR arrays", text)
                self.assertIn("opened 2/day, merged 1.5/day", text)

        for head in ({"ref": ""}, {"ref": "  "}, {"ref": None}, {}, None, "sync/workflows-x"):
            with self.subTest(head=head):
                self.data["canary_prs"][repo] = [[{"number": 44, "head": head}]]
                rows = self.measure()["rows"]
                self.assertIsNone(rows["oldest_canary"]["value"])
                self.assertIn("canary PR head branch", rows["oldest_canary"]["reason"])
                self.assertIsNotNone(rows["released_flow"]["value"])

    def test_canary_drafts_and_later_pages_are_included(self):
        row = self.measure()["rows"]["oldest_canary"]
        self.assertIn("stranske/trip-planner#42", row["value"])

        # A duplicate can indicate that PRs shifted between pages, leaving an
        # older sync PR out of the response. Deduplicating cannot prove its age.
        repo = "stranske/trip-planner"
        pr = copy.deepcopy(self.data["canary_prs"][repo][1][0])
        non_sync = {**pr, "head": {"ref": "feature/other"}}
        for pages in ([[pr, pr]], [[pr], [pr]], [[non_sync], [non_sync]]):
            with self.subTest(overlapping_pages=len(pages)):
                self.data["canary_prs"][repo] = pages
                rows = self.measure()["rows"]
                self.assertIsNone(rows["oldest_canary"]["value"])
                self.assertIn("non-overlapping canary PR pages", rows["oldest_canary"]["reason"])
                self.assertEqual(len(rows), 5)
                for key in ("merge_to_close", "released_flow", "opener_cap"):
                    self.assertIsNotNone(rows[key]["value"])

        for number in (None, False, 0, -1, "42"):
            with self.subTest(invalid_number=number):
                self.data["canary_prs"][repo] = [[{**pr, "number": number}]]
                row = self.measure()["rows"]["oldest_canary"]
                self.assertIsNone(row["value"])
                self.assertIn("complete GitHub canary PR numbers", row["reason"])

        # PR numbers are unique within a repository, rather than across canaries.
        self.data["canary_prs"][repo] = [[pr]]
        other_repo = "stranske/Portable-Alpha-Extension-Model"
        self.data["canary_prs"][other_repo][0][0]["number"] = pr["number"]
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

        # REST supplies head.ref; backlog's branch reference rule expects headRefName.
        # A source issue on the branch still counts when the PR has no body reference.
        pr = self.data["prs"][0]
        for body in (None, "", "No closing reference in this body"):
            with self.subTest(body=body):
                pr["body"] = body
                pr["head"] = {"ref": "codex/issue-1"}
                rows = self.measure()["rows"]
                self.assertIn("opened 2/day, merged 1.5/day", rows["released_flow"]["value"])
                self.assertIn("median 20 min", rows["merge_to_close"]["value"])

        # Combining body and branch references must still count the PR once.
        pr["body"] = "Closes #2"
        row = self.measure()["rows"]["released_flow"]
        self.assertIn("opened 2/day, merged 1.5/day", row["value"])

        original = json.loads(FIXTURE.read_text())
        for repo in ("stranske/Orchestrator", "STRANSKE/orchestrator"):
            with self.subTest(structured_only_repo=repo):
                self.data = copy.deepcopy(original)
                pr = self.data["prs"][0]
                pr["body"] = None
                refs = self.data["pr_details"]["10"]["data"]["repository"]["pullRequest"]
                refs["closingIssuesReferences"]["nodes"] = [
                    {"number": 1, "repository": {"nameWithOwner": repo}}
                ]
                self.calls.clear()
                rows = self.measure()["rows"]
                self.assertIn("median 15 min (2 PR/issue pairs", rows["merge_to_close"]["value"])
                self.assertIn("opened 2/day, merged 1.5/day", rows["released_flow"]["value"])
                details = [args for args in self.calls if args[1] == "graphql"]
                # Recently opened unmerged #12 is eligible only for flow. The
                # unlabeled #13 and old #15 must not cause detail reads.
                self.assertEqual(len(details), 4)
                self.assertEqual(
                    {arg for args in details for arg in args if arg.startswith("number=")},
                    {"number=10", "number=11", "number=12", "number=14"},
                )

        # Same-number links in another repository never become local issue joins.
        refs["closingIssuesReferences"]["nodes"] = [
            {"number": 1, "repository": {"nameWithOwner": "stranske/Workflows"}},
            {"number": 99, "repository": {"nameWithOwner": "other/Orchestrator"}},
        ]
        self.calls.clear()
        rows = self.measure()["rows"]
        self.assertIn("median 20 min (1 PR/issue pairs", rows["merge_to_close"]["value"])
        self.assertIn("opened 1.5/day, merged 1/day", rows["released_flow"]["value"])
        self.assertFalse(any(args[1].endswith(("/issues/1", "/issues/99")) for args in self.calls))

        # Deduplicate structured links and combine them with body and branch
        # sources. Flow counts each PR once; latency counts each distinct pair.
        pr["body"] = "Closes #1\nCloses #2\nCloses #2"
        pr["head"] = {"ref": "codex/issue-3"}
        refs["closingIssuesReferences"]["nodes"] = [
            {"number": n, "repository": {"nameWithOwner": "stranske/Orchestrator"}}
            for n in (1, 1, 2)
        ]
        self.calls.clear()
        rows = self.measure()["rows"]
        self.assertIn("median 20 min (3 PR/issue pairs", rows["merge_to_close"]["value"])
        self.assertIn("opened 2/day, merged 1.5/day", rows["released_flow"]["value"])
        for number in (1, 2, 3, 4):
            self.assertEqual(sum(args[1].endswith(f"/issues/{number}") for args in self.calls), 1)

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

    def test_opener_response_failures_preserve_independent_rows(self):
        for returncode, stdout, reason_part in (
            (0, "not valid json", "valid JSON"),
            (0, "[1, 2, 3]", "a JSON object"),
            (0, '"just a string"', "a JSON object"),
            (1, "", "successful"),
        ):
            with self.subTest(returncode=returncode, stdout=stdout[:30]):

                def opener(args, **kwargs):
                    return SimpleNamespace(returncode=returncode, stdout=stdout)

                rows = self.measure(opener_runner=opener)["rows"]
                # opener_cap has no numeric value
                self.assertIsNone(rows["opener_cap"]["value"])
                # explicit unmeasured reason
                self.assertIn(reason_part, rows["opener_cap"]["reason"])
                self.assertEqual(rows["opener_cap"]["evidence_class"], "unmeasured")
                # all five text rows appear
                self.assertEqual(len(rows), 5)
                for name in review.FLEET_DELIVERY_NAMES:
                    self.assertIn(name, rows)
                # independent measure retains its value
                self.assertIsNotNone(rows["merge_to_close"]["value"])

    def test_opener_response_failures_preserve_independent_text_rows(self):
        for returncode, stdout, reason_part in (
            (0, "not valid json", "valid JSON"),
            (0, "[1, 2, 3]", "a JSON object"),
            (1, "", "successful"),
        ):
            with self.subTest(returncode=returncode, stdout=stdout[:30]):

                def opener(args, **kwargs):
                    return SimpleNamespace(returncode=returncode, stdout=stdout)

                section = self.measure(opener_runner=opener)
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
                # explicit unmeasured reason appears
                self.assertIn("unmeasured — would need", text)
                self.assertIn(reason_part, text)
                # all five text rows appear once
                for name in review.FLEET_DELIVERY_NAMES.values():
                    self.assertEqual(text.count(name + ":"), 1)
                # independent measure retains its value
                self.assertIn("median 15 min", text)

    def test_excluded_repository_is_never_queried(self):
        backlog.SUPPORTED_REPOS.append("stranske/Collab-Admin")
        self.measure()
        self.assertNotIn("Collab-Admin", json.dumps(self.calls))

    def test_invalid_json_preserves_other_rows(self):
        def gh(args, **kwargs):
            if "/actions/" in args[1]:
                return True, "not JSON", ""
            return self.gh(args, **kwargs)

        report = review.fleet_delivery_measures(now=self.now, gh_fn=gh, opener_runner=self.opener)
        self.assertEqual(report["rows"]["gate_cancellations"]["reason"], "gh invalid JSON response")
        self.assertIsNotNone(report["rows"]["released_flow"]["value"])

    def test_unexpected_reader_exception_preserves_other_rows(self):
        with patch.object(review, "_fleet_oldest_canary", side_effect=RuntimeError("broken")):
            rows = self.measure()["rows"]
        self.assertIsNone(rows["oldest_canary"]["value"])
        self.assertIn("RuntimeError", rows["oldest_canary"]["reason"])
        self.assertIsNotNone(rows["released_flow"]["value"])

    def test_recent_pr_pagination_reaches_second_page(self):
        first = [{**self.data["prs"][0], "number": n} for n in range(100, 200)]
        responses = iter([first, [self.data["prs"][1]]])
        calls = []

        def gh(args):
            calls.append(args)
            return True, json.dumps(next(responses)), ""

        rows = review._fleet_recent_prs(gh, "stranske/Orchestrator", self.now)
        self.assertEqual(len(rows), 101)
        self.assertIn("page=2", calls[1][1])

        old = copy.deepcopy(self.data["prs"][0])
        old["number"] = 99
        old["updated_at"] = "2026-11-01T00:00:00Z"
        recent = self.data["prs"][0]
        for pages in (
            [[old, recent]],
            [[recent, old, recent]],
            [
                [{**pr, "updated_at": "2026-11-04T11:00:00Z"} for pr in first],
                [recent],
            ],
        ):
            with self.subTest(pages=len(pages), first=pages[0][0]["updated_at"]):
                responses = iter(pages)
                with self.assertRaisesRegex(review._FleetReadError, "descending updated PR order"):
                    review._fleet_recent_prs(
                        lambda args: (True, json.dumps(next(responses)), ""),
                        "stranske/Orchestrator",
                        self.now,
                    )

        # An old row cannot hide a newer row and turn the report into a numeric undercount.
        self.data["prs"] = [old, recent]
        rows = self.measure()["rows"]
        for key in ("merge_to_close", "released_flow"):
            self.assertIsNone(rows[key]["value"])
            self.assertIn("descending updated PR order", rows[key]["reason"])
        self.assertEqual(len(rows), 5)
        self.assertIsNotNone(rows["oldest_canary"]["value"])
        self.assertIsNotNone(rows["opener_cap"]["value"])

        # Equal timestamps and the exact 48-hour boundary remain valid. Stop without
        # another API read once a fully validated page reaches older PRs.
        boundary = {**recent, "updated_at": "2026-11-03T00:00:00Z"}
        batch = first[:98] + [{**boundary, "number": 200}, {**old, "number": 201}]
        calls = []

        def gh_at_boundary(args):
            calls.append(args)
            self.assertEqual(len(calls), 1)
            return True, json.dumps(batch), ""

        rows = review._fleet_recent_prs(gh_at_boundary, "stranske/Orchestrator", self.now)
        self.assertEqual(len(rows), 99)
        self.assertEqual(rows[-1], {**boundary, "number": 200})

        # Updated-order pagination can overlap when a PR moves during the read.
        # Neither a duplicate on one page nor at a page boundary proves a full population.
        for pages in ([[recent, recent]], [first, [first[-1]]]):
            with self.subTest(overlapping_pages=len(pages)):
                responses = iter(pages)
                with self.assertRaisesRegex(review._FleetReadError, "non-overlapping PR pages"):
                    review._fleet_recent_prs(
                        lambda args: (True, json.dumps(next(responses)), ""),
                        "stranske/Orchestrator",
                        self.now,
                    )

        self.data["prs"] = [recent, recent]
        section = self.measure()
        for key in ("merge_to_close", "released_flow"):
            self.assertIsNone(section["rows"][key]["value"])
            self.assertIn("non-overlapping PR pages", section["rows"][key]["reason"])
        self.assertEqual(len(section["rows"]), 5)
        self.assertIsNotNone(section["rows"]["oldest_canary"]["value"])
        self.assertIsNotNone(section["rows"]["opener_cap"]["value"])

        for number in (None, False, 0, -1, "10"):
            with self.subTest(invalid_pr_number=number):
                with self.assertRaisesRegex(review._FleetReadError, "complete GitHub PR numbers"):
                    review._fleet_recent_prs(
                        lambda args: (True, json.dumps([{**recent, "number": number}]), ""),
                        "stranske/Orchestrator",
                        self.now,
                    )

    def test_pr_pagination_budget_is_not_reported_as_complete(self):
        def gh(args):
            page = int(args[1].rsplit("page=", 1)[1])
            batch = [
                {**self.data["prs"][0], "number": n} for n in range(page * 100, (page + 1) * 100)
            ]
            return True, json.dumps(batch), ""

        with self.assertRaisesRegex(review._FleetReadError, "complete updated PR pagination"):
            review._fleet_recent_prs(gh, "stranske/Orchestrator", self.now)

    def test_missing_pr_evidence_does_not_become_measured_zero(self):
        self.data["prs"][0]["created_at"] = None
        row = self.measure()["rows"]["released_flow"]
        self.assertIsNone(row["value"])
        self.assertIn("UTC GitHub timestamp", row["reason"])

        original = json.loads(FIXTURE.read_text())
        # Only JSON null establishes that a PR is unmerged. Other false values
        # cannot silently drop a merge from the median or the released flow.
        for merged_at in ("", False, 0, [], {}):
            with self.subTest(merged_at=merged_at):
                self.data = copy.deepcopy(original)
                self.data["prs"][0]["merged_at"] = merged_at
                section = self.measure()
                for key in ("merge_to_close", "released_flow"):
                    row = section["rows"][key]
                    self.assertIsNone(row["value"])
                    self.assertEqual(row["evidence_class"], "unmeasured")
                    self.assertIn("UTC GitHub timestamp", row["reason"])
                text = review.format_report(
                    {
                        "generated_at": self.now,
                        "review_days": 7,
                        "held_off": [],
                        "on_but_idle": [],
                        "unconditioned": [],
                        "raise_count": 0,
                        "fleet_delivery_measures": section,
                    }
                )
                for name in review.FLEET_DELIVERY_NAMES.values():
                    self.assertEqual(text.count(name + ":"), 1)
                self.assertIsNotNone(section["rows"]["oldest_canary"]["value"])
                self.assertIsNotNone(section["rows"]["opener_cap"]["value"])
        self.data = copy.deepcopy(original)
        self.data["prs"][0]["merged_at"] = None
        rows = self.measure()["rows"]
        self.assertIn("median 20 min", rows["merge_to_close"]["value"])
        self.assertIn("opened 2/day, merged 1/day", rows["released_flow"]["value"])

        missing = object()
        for body in (missing, False, 0, {}, []):
            with self.subTest(body=body):
                self.data = copy.deepcopy(original)
                if body is missing:
                    del self.data["prs"][0]["body"]
                else:
                    self.data["prs"][0]["body"] = body
                rows = self.measure()["rows"]
                for key in ("merge_to_close", "released_flow"):
                    self.assertIsNone(rows[key]["value"])
                    self.assertIn("complete GitHub PR body", rows[key]["reason"])
                self.assertEqual(len(rows), 5)
                self.assertIsNotNone(rows["oldest_canary"]["value"])
                self.assertIsNotNone(rows["opener_cap"]["value"])
        # GitHub represents a known empty PR body as null or an empty string.
        for body in (None, ""):
            with self.subTest(empty_body=body):
                self.data = copy.deepcopy(original)
                self.data["prs"][0]["body"] = body
                rows = self.measure()["rows"]
                self.assertIn("median 20 min", rows["merge_to_close"]["value"])
                self.assertIn("opened 1.5/day, merged 1/day", rows["released_flow"]["value"])
        self.data = copy.deepcopy(original)
        for head in (missing, None, {}, {"ref": ""}, {"ref": "  "}, {"ref": None}, "main"):
            with self.subTest(head=head):
                self.data = copy.deepcopy(original)
                if head is missing:
                    del self.data["prs"][0]["head"]
                else:
                    self.data["prs"][0]["head"] = head
                rows = self.measure()["rows"]
                self.assertIsNone(rows["released_flow"]["value"])
                self.assertIn("PR head branch", rows["released_flow"]["reason"])
                self.assertIn("median 15 min", rows["merge_to_close"]["value"])
                self.assertIsNotNone(rows["oldest_canary"]["value"])
        for target in ("PR", "issue"):
            for labels in (
                missing,
                None,
                {},
                "agent:codex",
                [None],
                [{}],
                [{"name": ""}],
                [{"name": 1}],
                [False],
            ):
                with self.subTest(target=target, labels=labels):
                    self.data = copy.deepcopy(original)
                    record = self.data["prs"][0] if target == "PR" else self.data["issues"]["1"]
                    if labels is missing:
                        del record["labels"]
                    else:
                        record["labels"] = labels
                    rows = self.measure()["rows"]
                    self.assertEqual(len(rows), 5)
                    row = rows["released_flow"]
                    self.assertIsNone(row["value"])
                    self.assertEqual(row["evidence_class"], "unmeasured")
                    self.assertIn(f"complete GitHub {target} labels", row["reason"])
                    self.assertIsNotNone(rows["oldest_canary"]["value"])
                    self.assertIsNotNone(rows["opener_cap"]["value"])
                    if target == "PR":
                        self.assertIsNone(rows["merge_to_close"]["value"])
                    else:
                        self.assertIn("median 15 min", rows["merge_to_close"]["value"])
            for labels in ([], [{"name": "other"}], ["other"]):
                with self.subTest(target=target, valid_labels=labels):
                    self.data = copy.deepcopy(original)
                    record = self.data["prs"][0] if target == "PR" else self.data["issues"]["1"]
                    record["labels"] = labels
                    row = self.measure()["rows"]["released_flow"]
                    self.assertIn("opened 1.5/day, merged 1/day", row["value"])

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
