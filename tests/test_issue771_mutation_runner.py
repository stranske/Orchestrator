"""Failure-safety checks runnable with unittest when pytest is unavailable."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts import verify_issue771_mutations as replay


class MutationReplayTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.checkout = self.root / "checkout"
        self.output = self.root / "evidence"
        self.target = self.checkout / replay.TARGET
        self.target.parent.mkdir(parents=True)
        self.original = "\n".join(anchor for anchor, _ in replay.MUTATIONS.values()).encode()
        self.target.write_bytes(self.original)

    def xml(self, names, outcome=""):
        path = self.root / "result.xml"
        cases = "".join(f'<testcase name="{name}">{outcome}</testcase>' for name in names)
        path.write_text(f"<testsuites><testsuite>{cases}</testsuite></testsuites>")
        return path

    def test_expected_test_failure_is_required_in_junit(self):
        name = next(iter(replay.MUTATIONS))
        replay.verify_result(self.xml([name], "<failure/>"), [name], failed=True)
        for outcome in ("", "<error/>", "<skipped/>"):
            with self.subTest(outcome=outcome), self.assertRaises(ValueError):
                replay.verify_result(self.xml([name], outcome), [name], failed=True)

    def test_collection_errors_and_wrong_or_duplicate_tests_are_rejected(self):
        expected = list(replay.MUTATIONS)
        for names in ([], expected[:-1], expected + [expected[0]], ["unrelated"]):
            with self.subTest(names=names), self.assertRaises(ValueError):
                replay.verify_result(self.xml(names), expected, failed=False)

    def test_restored_run_must_pass_without_skip_or_failure(self):
        names = [next(iter(replay.MUTATIONS))]
        replay.verify_result(self.xml(names), names, failed=False)
        for outcome in ("<failure/>", "<error/>", "<skipped/>"):
            with self.subTest(outcome=outcome), self.assertRaises(ValueError):
                replay.verify_result(self.xml(names, outcome), names, failed=False)

    def test_mutation_anchors_must_be_unique(self):
        for name, (anchor, replacement) in replay.MUTATIONS.items():
            with self.subTest(name=name):
                self.assertIn(replacement, replay.mutate(self.original.decode(), name))
                for invalid in ("unrelated source", anchor + anchor):
                    with self.assertRaises(ValueError):
                        replay.mutate(invalid, name)

    def test_source_restores_and_no_receipt_is_written_after_timeout(self):
        timeout = subprocess.TimeoutExpired(["python", "-m", "pytest"], 120)
        with patch.object(replay, "check_checkout"), patch.object(
            replay, "run_pytest", side_effect=[{}, timeout]
        ), self.assertRaises(subprocess.TimeoutExpired):
            replay.capture(self.checkout, self.output)
        self.assertEqual(self.target.read_bytes(), self.original)
        self.assertFalse((self.output / "receipt.json").exists())

    def test_baseline_failure_prevents_mutation(self):
        with patch.object(replay, "check_checkout"), patch.object(
            replay, "run_pytest", side_effect=ValueError("baseline failure")
        ), self.assertRaises(ValueError):
            replay.capture(self.checkout, self.output)
        self.assertEqual(self.target.read_bytes(), self.original)
        self.assertEqual(list(self.output.iterdir()), [])

    def test_surviving_mutation_or_failed_restoration_cannot_produce_receipt(self):
        for outcomes in (({}, ValueError("surviving mutation")), ({}, {}, ValueError("restore"))):
            with self.subTest(outcomes=outcomes), tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / "evidence"
                with patch.object(replay, "check_checkout"), patch.object(
                    replay, "run_pytest", side_effect=outcomes
                ), self.assertRaises(ValueError):
                    replay.capture(self.checkout, output)
                self.assertEqual(self.target.read_bytes(), self.original)
                self.assertFalse((output / "receipt.json").exists())

    def test_each_mutation_restores_before_the_next_test_and_receipt(self):
        observed = []

        def fake_pytest(checkout, output, label, names, python, failed=False):
            content = self.target.read_bytes()
            if failed:
                self.assertNotEqual(content, self.original)
                self.assertEqual(content.decode(), replay.mutate(self.original.decode(), names[0]))
            else:
                self.assertEqual(content, self.original)
            observed.append((label, names, failed))
            return {"exit_status": 1 if failed else 0}

        with patch.object(replay, "check_checkout"), patch.object(
            replay, "run_pytest", side_effect=fake_pytest
        ):
            replay.capture(self.checkout, self.output)
        self.assertEqual(len(observed), 11)
        receipt = json.loads((self.output / "receipt.json").read_text())
        self.assertEqual(set(receipt["mutations"]), set(replay.MUTATIONS))
        for name in replay.MUTATIONS:
            artifact = self.output / f"{name}.patch"
            self.assertIn("--- a/src/feedback.py", artifact.read_text())

    def test_existing_evidence_and_checkout_directory_cannot_be_overwritten(self):
        self.output.mkdir()
        receipt = self.output / "receipt.json"
        receipt.write_text("prior receipt")
        with patch.object(replay, "check_checkout"), self.assertRaises(ValueError):
            replay.capture(self.checkout, self.output)
        self.assertEqual(receipt.read_text(), "prior receipt")
        with self.assertRaises(ValueError):
            replay.capture(self.checkout, self.checkout / "evidence")

    def test_preflight_rejects_wrong_commit_and_dirty_checkout(self):
        for results in (("wrong-head",), (replay.HEAD, " M src/feedback.py")):
            completed = [SimpleNamespace(stdout=value) for value in results]
            with self.subTest(results=results), patch.object(
                replay.subprocess, "run", side_effect=completed
            ), self.assertRaises(ValueError):
                replay.check_checkout(self.checkout)

    def test_preflight_rejects_changed_blob_even_with_clean_git_status(self):
        with patch.object(
            replay.subprocess,
            "run",
            side_effect=[SimpleNamespace(stdout=replay.HEAD), SimpleNamespace(stdout="")],
        ), self.assertRaisesRegex(ValueError, "blob mismatch"):
            replay.check_checkout(self.checkout)

    def test_pytest_preserves_full_output_and_uses_fresh_bytecode_cache(self):
        self.output.mkdir()
        name = next(iter(replay.MUTATIONS))
        cache_paths = []

        def fake_process(command, **kwargs):
            self.assertEqual(command[:3], ["/chosen/venv/bin/python", "-m", "pytest"])
            self.assertIn("not slow", command)
            cache_paths.append(kwargs["env"]["PYTHONPYCACHEPREFIX"])
            self.assertTrue(Path(cache_paths[-1]).is_dir())
            kwargs["stdout"].write(b"complete output\n" * 10000)
            xml_path = Path(command[-1].removeprefix("--junitxml="))
            xml_path.write_text(self.xml([name], "<failure/>").read_text())
            return SimpleNamespace(returncode=1)

        with patch.object(replay.subprocess, "run", side_effect=fake_process):
            for label in ("first", "second"):
                replay.run_pytest(
                    self.checkout, self.output, label, [name], "/chosen/venv/bin/python", True
                )
        self.assertNotEqual(*cache_paths)
        self.assertEqual((self.output / "first.log").read_bytes(), b"complete output\n" * 10000)

    def test_pytest_collection_exit_is_not_an_expected_mutation_failure(self):
        self.output.mkdir()
        name = next(iter(replay.MUTATIONS))
        with patch.object(
            replay.subprocess, "run", return_value=SimpleNamespace(returncode=2)
        ), self.assertRaisesRegex(ValueError, "exit status"):
            replay.run_pytest(self.checkout, self.output, "collection", [name], "python3", True)
        record = json.loads((self.output / "collection.json").read_text())
        self.assertEqual(record["exit_status"], 2)


if __name__ == "__main__":
    unittest.main()
