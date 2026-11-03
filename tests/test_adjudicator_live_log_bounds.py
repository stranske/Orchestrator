"""Strict byte bounds for execution evidence, including the certified packet path."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import adjudicator_live as live


class ExecutionLogBoundsTests(unittest.TestCase):
    def test_tail_includes_marker_and_valid_utf8_in_byte_budget(self):
        for text in ("", "short", "x" * 8_000, "界🙂" * 2_000 + "failure"):
            for limit in (0, 1, 3, 20, 21, 22, 4_096):
                with self.subTest(text=text[:10], limit=limit):
                    tail = live._tail(text, limit)
                    self.assertLessEqual(len(tail.encode("utf-8")), limit)
                    self.assertNotIn("\ufffd", tail)
                    if len(text.encode("utf-8")) <= limit:
                        self.assertEqual(tail, text)
                    elif limit >= 22:
                        self.assertTrue(tail.startswith("[... truncated ...]\n"))
                        self.assertTrue(text.endswith(tail.split("\n", 1)[1]))

    def test_failure_logs_share_a_strict_byte_budget(self):
        sha = "a" * 40

        def reader(args):
            if "/commits/" in args[1]:
                return {
                    "total_count": 3,
                    "check_runs": [
                        {"id": i, "head_sha": sha, "name": f"job-{i}", "conclusion": "failure"}
                        for i in range(3)
                    ],
                }
            job = args[1].rsplit("/", 1)[1]
            return {
                "head_sha": sha,
                "details_url": f"https://github.com/owner/repo/actions/runs/9/job/{job}",
            }

        with (
            patch.object(live, "CHECK_RUN_LOG_TAIL_BYTES", 64),
            patch.object(live, "EXECUTION_EVIDENCE_BYTES", 70),
        ):
            runs = live._check_run_evidence("owner/repo#1", sha, reader, lambda _url: "x" * 300)
        tails = [run["log_tail"] for run in runs if "log_tail" in run]
        self.assertEqual(len(tails), 2)
        self.assertTrue(all(len(tail.encode("utf-8")) <= 64 for tail in tails))
        self.assertLessEqual(sum(len(tail.encode("utf-8")) for tail in tails), 70)
        self.assertEqual(runs[2]["log_unavailable"], "execution evidence budget exhausted")

    def test_certification_retains_paginated_checks_and_bounded_failure_log(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sha = live._toy_repository(root)
            row = {
                "target": "owner/repo#1",
                "packet": live.retro.build_packet(
                    {"target": "owner/repo#1"}, live._evidence_for(sha)
                ),
            }
            calls = []

            def reader(args):
                endpoint = args[1]
                calls.append(endpoint)
                if "/commits/" in endpoint:
                    page = int(endpoint.rsplit("=", 1)[1])
                    self.assertIn(f"/commits/{sha}/check-runs", endpoint)
                    self.assertIn("per_page=100", endpoint)
                    return {
                        "total_count": 2,
                        "check_runs": [
                            {
                                "id": page,
                                "head_sha": sha,
                                "name": f"job-{page}",
                                "conclusion": "failure" if page == 2 else "success",
                            }
                        ],
                    }
                return {
                    "head_sha": sha,
                    "details_url": "https://github.com/owner/repo/actions/runs/9/job/2",
                }

            case = live.certify(
                row,
                root / "repo",
                {"number": 1, "headRefOid": sha, "body": live.AC_BODY},
                github_reader=reader,
                runtime_events_reader=lambda: [],
                log_reader=lambda _url: "界🙂" * 2_000 + "last failure",
            )
            packet = live.roles._collected_snapshot(case)
            execution = packet["ground_truth_evidence"]["live_execution_evidence"]
            runs = execution["check_runs"]
            self.assertEqual([run["name"] for run in runs], ["job-1", "job-2"])
            self.assertEqual([run["conclusion"] for run in runs], ["success", "failure"])
            for index, run in enumerate(runs, 1):
                self.assertEqual(run["sha"], sha)
                self.assertEqual(
                    run["api_url"], f"https://api.github.com/repos/owner/repo/check-runs/{index}"
                )
            self.assertEqual(
                runs[1]["log_provenance"],
                {
                    "api_url": "https://api.github.com/repos/owner/repo/actions/jobs/2/logs",
                    "sha": sha,
                },
            )
            self.assertTrue(runs[1]["log_tail"].endswith("last failure"))
            self.assertLessEqual(len(runs[1]["log_tail"].encode()), live.CHECK_RUN_LOG_TAIL_BYTES)
            self.assertTrue(any(endpoint.endswith("page=2") for endpoint in calls))


if __name__ == "__main__":
    unittest.main()
