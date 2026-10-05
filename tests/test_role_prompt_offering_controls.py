"""A deliberate-break receipt must come from the current named pytest execution."""

import importlib.util
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "verify_role_prompt_offering",
    Path(__file__).resolve().parents[1] / "scripts" / "verify_role_prompt_offering.py",
)
assert SPEC and SPEC.loader
controls = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(controls)


class TestControlReceipts(unittest.TestCase):
    def test_retries_require_fresh_named_assertion_receipts(self):
        test_name = controls.CONTROLS[0][1]
        passing = f'<testsuite><testcase name="{test_name}" /></testsuite>'
        failing = (
            f'<testsuite><testcase name="{test_name}">'
            '<failure message="AssertionError">AssertionError: missing surface</failure>'
            "</testcase></testsuite>"
        )
        # Exercise the real receipt parser. Mock only pytest, which may fail before
        # writing XML; an earlier successful attempt cannot stand in for that run.
        with tempfile.TemporaryDirectory() as scratch:
            directory = Path(scratch)
            for phase, report_text, exit_code in (
                ("baseline", passing, 0),
                ("broken", failing, 1),
                ("restored", passing, 0),
            ):
                report = directory / f"{phase}.xml"
                report.write_text(report_text, encoding="utf-8")
                with self.subTest(phase=phase, receipt="stale"):
                    result = subprocess.CompletedProcess([], exit_code, "child wrote no report", "")
                    with patch.object(controls.subprocess, "run", return_value=result):
                        with self.assertRaisesRegex(RuntimeError, "no pytest report"):
                            controls.run_named_test(directory, test_name, phase)

                def current_run(command, **kwargs):
                    self.assertFalse(report.exists())
                    self.assertIn(f"--junitxml={report}", command)
                    report.write_text(report_text, encoding="utf-8")
                    return subprocess.CompletedProcess(command, exit_code, "current run", "")

                with self.subTest(phase=phase, receipt="fresh"):
                    with patch.object(controls.subprocess, "run", side_effect=current_run):
                        receipt = controls.run_named_test(directory, test_name, phase)
                    self.assertEqual(receipt["phase"], phase)
                    self.assertEqual(receipt["exit"], exit_code)

            with patch.object(controls.subprocess, "run") as child:
                with self.assertRaisesRegex(ValueError, "Unknown control phase"):
                    controls.run_named_test(directory, test_name, "unknown")
                child.assert_not_called()
