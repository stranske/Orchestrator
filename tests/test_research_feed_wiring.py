"""Run the research discovery shell boundary without running a live orchestration tick."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import paths


class ResearchFeedWiringTests(unittest.TestCase):
    def run_discovery(
        self,
        *,
        deferred=False,
        fail=False,
        arm=None,
        dispatch=False,
    ):
        script = (paths.REPO_ROOT / "orchestrate.sh").read_text()
        start = script.index("# DISCOVERY FEEDS ONLY THIS TOOL'S OWN DISPATCH LANE")
        end = script.index("# ORCH-ANCHOR: frontend-verify-doctor", start)
        fragment = script[start:end]
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            calls = root / "calls"
            fake_python = root / "python3"
            fake_python.write_text(
                '#!/usr/bin/env bash\n'
                'printf "%s\\n" "$*" >> "$RESEARCH_TEST_CALLS"\n'
                'exit "$RESEARCH_TEST_EXIT"\n'
            )
            fake_python.chmod(0o755)
            env = {
                **os.environ,
                "PATH": f"{root}{os.pathsep}{os.environ['PATH']}",
                "ORCH": str(root),
                "ORCH_DISPATCH_LANE": "1" if dispatch else "0",
                "RESEARCH_TEST_CALLS": str(calls),
                "RESEARCH_TEST_EXIT": "1" if fail else "0",
            }
            env.pop("ORCH_RESEARCH_ARM", None)
            if arm is not None:
                env["ORCH_RESEARCH_ARM"] = arm
            replay = root / "discovery.sh"
            replay.write_text(
                "set -euo pipefail\n"
                + "_gh_deferred() { "
                + ("true" if deferred else "false")
                + "; }\n"
                + fragment
                + '\nprintf "discovery completed\\n"\n'
            )
            result = subprocess.run(
                ["bash", str(replay)],
                capture_output=True,
                text=True,
                env=env,
                timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("discovery completed", result.stdout)
            return calls.read_text().splitlines() if calls.exists() else [], result.stdout

    def test_shadow_dispatch_builds_research_feed_with_launching_off(self):
        for arm in (None, "0", "1"):
            with self.subTest(arm=arm):
                calls, _ = self.run_discovery(arm=arm)
                self.assertEqual(len(calls), 1, calls)
                self.assertTrue(calls[0].endswith("/backlog.py --research"), calls)

    def test_deferred_github_skips_both_discovery_reads(self):
        for dispatch in (False, True):
            with self.subTest(dispatch=dispatch):
                calls, output = self.run_discovery(deferred=True, dispatch=dispatch)
                self.assertEqual(calls, [])
                self.assertIn("research feed: SKIPPED", output)

    def test_research_read_failure_warns_and_continues(self):
        calls, output = self.run_discovery(fail=True)
        self.assertEqual(len(calls), 1, calls)
        self.assertIn("warn: research feed failed (continuing)", output)

    def test_live_dispatch_keeps_its_feed_and_builds_research_feed(self):
        calls, _ = self.run_discovery(dispatch=True)
        self.assertEqual(len(calls), 2, calls)
        self.assertTrue(calls[0].endswith("/backlog.py --live"), calls)
        self.assertTrue(calls[1].endswith("/backlog.py --research"), calls)


if __name__ == "__main__":
    unittest.main()
