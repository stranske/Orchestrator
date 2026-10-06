from __future__ import annotations

import base64
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import codemod_lane as lane
import paths


class CampaignLineEndingTests(unittest.TestCase):
    def setUp(self):
        temporary = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(temporary)
        self.enterContext(patch.dict(os.environ, {"ORCH_STATE_DIR": temporary}))
        self.campaign = json.loads(
            (paths.REPO_ROOT / "campaigns/gitignore-caches-2026-10.json").read_text()
        )
        subprocess.run(["git", "init", "-q", temporary], check=True)

    def assert_effective_append(self, original):
        repaired = lane.append_missing_ignores(original)
        self.assertTrue(repaired.startswith(original))
        self.assertEqual(lane.append_missing_ignores(repaired), repaired)
        (self.root / ".gitignore").write_bytes(repaired.encode())
        probes = [
            f"{entry}probe" if entry.endswith("/") else entry for entry in lane.IGNORE_ENTRIES
        ]
        checked = subprocess.run(
            ["git", "-C", str(self.root), "check-ignore", "--no-index", *probes],
            capture_output=True,
            text=True,
        )
        self.assertEqual(checked.returncode, 0)
        self.assertEqual(checked.stdout.splitlines(), probes)

    def test_only_git_line_endings_separate_ignore_entries(self):
        for separator in ["\r", "\v", "\f", "\x1c", "\x85", "\u2028", "\u2029"]:
            with self.subTest(separator=repr(separator)):
                self.assert_effective_append(f"# existing comment{separator}.coverage\n")

    def test_lone_trailing_carriage_return_does_not_join_appended_entry(self):
        for original in ["existing/\r", "existing/\r\nlast/\r"]:
            with self.subTest(original=repr(original)):
                self.assert_effective_append(original)

    def test_crlf_entries_are_preserved_without_duplicates(self):
        original = "existing/\r\n.mypy_cache/\r\n"
        repaired = lane.append_missing_ignores(original)
        self.assert_effective_append(original)
        self.assertEqual(repaired.count(".mypy_cache/"), 1)
        self.assertNotIn("\n", repaired.replace("\r\n", ""))
        complete = "\r\n".join(lane.IGNORE_ENTRIES) + "\r\n"
        self.assertEqual(lane.append_missing_ignores(complete), complete)

    def test_issue_plan_and_receipts_include_entries_after_non_git_line_breaks(self):
        original = "# existing comment\r.coverage\n"
        body = lane.target_issue_body(self.campaign, "stranske/Ready", original)
        scope = body.split("## Scope\n")[1].split("## Non-Goals")[0]
        self.assertIn("`.coverage`", scope)

        def gh(args):
            if args[0] == "api" and args[1].endswith("/contents/.gitignore"):
                return {
                    "encoding": "base64",
                    "content": base64.b64encode(original.encode()).decode(),
                }
            if args[:2] == ["issue", "list"]:
                repo = args[args.index("--repo") + 1]
                return [
                    {
                        "number": 10,
                        "state": "OPEN",
                        "url": f"https://github.com/{repo}/issues/10",
                        "body": f"<!-- codemod-campaign:{self.campaign['campaign']['id']} -->",
                    }
                ]
            if args[0] == "api" and args[1].endswith("/comments"):
                return {"id": 1}
            self.fail(f"unexpected GitHub call: {args}")

        program = lane.file_targets(self.campaign, gh=gh)
        for row in program["repos"].values():
            self.assertEqual(row["missing"], list(lane.IGNORE_ENTRIES))
