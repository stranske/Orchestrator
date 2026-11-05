"""Verify deployment evidence against real export history without production state."""

import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import route_weights_export as export


class ChallengerDeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.state = self.root / "state"
        self.remote = self.root / "remote.git"
        self.checkout = self.root / "checkout"
        environment = {
            f"GIT_{role}_{key}": value
            for role in ("AUTHOR", "COMMITTER")
            for key, value in (("NAME", "Deployment Test"), ("EMAIL", "deploy@example.invalid"))
        }
        environment["ORCH_FEEDBACK_DB"] = str(self.root / "feedback.db")
        environment["ORCH_STATE_DIR"] = str(self.state)
        environment["ORCH_LOCAL_RUNTIME"] = str(self.root / "runtime")
        self.enterContext(patch.dict(os.environ, environment))
        self.enterContext(patch.object(Path, "home", return_value=self.root))
        self.git(self.root, "init", "--bare", "--initial-branch=main", str(self.remote))
        self.git(self.root, "clone", str(self.remote), str(self.checkout))
        self.enterContext(
            patch.object(export.provision, "ensure_canonical", return_value=self.checkout)
        )
        self.enterContext(patch.object(export, "_now", return_value="2026-11-05T12:00:00Z"))
        self.document = {
            "generated_at": "2026-11-05T10:00:00Z",
            "source_version": 1,
            "task_types": {"implement": {"evidence_ok": True}},
            "exploration": {
                "challengers": {task: "codex" for task in export.CONSUMER_TASK_TYPES},
            },
        }
        self.seed(self.document)

    def git(self, repository, *args):
        return subprocess.run(
            ["git", "-C", str(repository), *args],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def seed(self, document):
        target = self.checkout / export.EXPORT_PATH
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(document), encoding="utf-8")
        self.git(self.checkout, "add", ".")
        self.git(self.checkout, "commit", "-m", "publication fixture")
        self.git(self.checkout, "push", "origin", f"HEAD:{export.EXPORT_BRANCH}")
        return self.git(self.remote, "rev-parse", export.EXPORT_BRANCH)

    def publish_filter(self):
        self.document["exploration"]["challenger_filter"] = export.CHALLENGER_FILTER
        self.assertTrue(export.publish_document(self.document, state_dir=self.state))
        return self.git(self.remote, "rev-parse", export.EXPORT_BRANCH)

    def provenance(self, commit):
        return {
            "published_commit": commit,
            "deployed_at": "2026-11-05T11:00:00Z",
            "deployment_evidence": "publication-audit/run-123/successful-push",
        }

    def record(self, provenance):
        return export.record_challenger_filter_deployment(provenance, state_dir=self.state)

    def test_real_publication_records_explicit_deployment_time_and_commit(self):
        commit = self.publish_filter()
        receipt = self.record(self.provenance(commit))
        self.assertEqual(receipt["published_commit"], commit)
        self.assertEqual(receipt["deployed_epoch"], 1793876400)
        self.assertEqual(receipt["generated_at"], self.document["generated_at"])
        self.assertNotEqual(receipt["deployed_at"], export._now())
        self.assertEqual(
            json.loads((self.state / export.CHALLENGER_DEPLOY_FILE).read_text()), receipt
        )

    def test_later_and_unchanged_publications_preserve_first_receipt(self):
        first = self.publish_filter()
        provenance = self.provenance(first)
        receipt = self.record(provenance)
        path = self.state / export.CHALLENGER_DEPLOY_FILE
        before = path.read_bytes(), path.stat().st_mtime_ns
        self.assertFalse(export.publish_document(self.document, state_dir=self.state))
        self.document["source_version"] += 1
        self.assertTrue(export.publish_document(self.document, state_dir=self.state))
        self.assertEqual(self.record(provenance), receipt)
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)
        later = self.git(self.remote, "rev-parse", export.EXPORT_BRANCH)
        with self.assertRaisesRegex(ValueError, "first verified"):
            self.record(self.provenance(later))

    def test_missing_and_malformed_provenance_never_create_receipt(self):
        good = self.provenance(self.publish_filter())
        for field in ("published_commit", "deployed_at", "deployment_evidence"):
            for value in (None, "", [], True):
                with self.subTest(field=field, value=value):
                    bad = {**good, field: value}
                    with self.assertRaises(ValueError):
                        self.record(bad)
        for value in (None, [], "invalid"):
            with self.subTest(provenance=value), self.assertRaises(ValueError):
                self.record(value)
        self.assertFalse((self.state / export.CHALLENGER_DEPLOY_FILE).exists())

    def test_invalid_deployment_instants_are_rejected(self):
        good = self.provenance(self.publish_filter())
        for timestamp in (
            "2026-11-05",
            "2026-11-05T11:00:00",
            "2026-11-05T11:00:00+01:00",
            "2026-11-05T11:00:00.123Z",
            "2026-11-05T09:59:59Z",
            "2026-11-06T11:00:00Z",
            "invalid",
        ):
            with self.subTest(timestamp=timestamp), self.assertRaises(ValueError):
                self.record({**good, "deployed_at": timestamp})
        self.assertFalse((self.state / export.CHALLENGER_DEPLOY_FILE).exists())

    def test_absent_contract_cannot_prove_the_filter_from_challenger_values(self):
        commit = self.git(self.remote, "rev-parse", export.EXPORT_BRANCH)
        with self.assertRaisesRegex(ValueError, "first verified"):
            self.record(self.provenance(commit))
        del self.document["exploration"]
        commit = self.seed(self.document)
        with self.assertRaisesRegex(ValueError, "first verified"):
            self.record(self.provenance(commit))

    def test_malformed_challengers_are_rejected(self):
        self.document["exploration"]["challenger_filter"] = export.CHALLENGER_FILTER
        for challengers in (None, [], {}, {task: "gemini" for task in export.CONSUMER_TASK_TYPES}):
            with self.subTest(challengers=challengers):
                self.document["exploration"]["challengers"] = challengers
                commit = self.seed(self.document)
                with self.assertRaisesRegex(ValueError, "contract is malformed"):
                    self.record(self.provenance(commit))
        self.assertFalse((self.state / export.CHALLENGER_DEPLOY_FILE).exists())

    def test_malformed_first_generated_at_is_not_replaced_by_clock(self):
        self.document["exploration"]["challenger_filter"] = export.CHALLENGER_FILTER
        self.document["generated_at"] = "malformed"
        first = self.seed(self.document)
        with self.assertRaisesRegex(ValueError, "generated_at"):
            self.record(self.provenance(first))
        # Repairing a later artifact cannot manufacture provenance for the first one.
        self.document["generated_at"] = "2026-11-05T10:00:00Z"
        later = self.seed(self.document)
        with self.assertRaisesRegex(ValueError, "generated_at"):
            self.record(self.provenance(later))
        self.assertFalse((self.state / export.CHALLENGER_DEPLOY_FILE).exists())

    def test_missing_generated_at_is_not_replaced_by_clock(self):
        self.document["exploration"]["challenger_filter"] = export.CHALLENGER_FILTER
        del self.document["generated_at"]
        commit = self.seed(self.document)
        with self.assertRaisesRegex(ValueError, "generated_at"):
            self.record(self.provenance(commit))
        self.assertFalse((self.state / export.CHALLENGER_DEPLOY_FILE).exists())

    def test_shallow_history_cannot_establish_the_first_publication(self):
        provenance = self.provenance(self.publish_filter())
        shallow = self.root / "shallow"
        self.git(
            self.root,
            "clone",
            "--depth=1",
            "--branch",
            export.EXPORT_BRANCH,
            self.remote.as_uri(),
            str(shallow),
        )
        with patch.object(export.provision, "ensure_canonical", return_value=shallow):
            with self.assertRaisesRegex(ValueError, "complete export history"):
                self.record(provenance)

    def test_stamp_write_failure_recovers_from_original_audit_without_republishing(self):
        provenance = self.provenance(self.publish_filter())
        with patch.object(export, "write_document", side_effect=OSError("state unavailable")):
            with self.assertRaisesRegex(OSError, "state unavailable"):
                self.record(provenance)
        self.assertFalse((self.state / export.CHALLENGER_DEPLOY_FILE).exists())
        self.assertFalse(export.publish_document(self.document, state_dir=self.state))
        with patch.object(export, "_now", return_value="2026-11-05T13:00:00Z"):
            receipt = self.record(provenance)
        self.assertEqual(receipt["deployed_at"], provenance["deployed_at"])

    def test_exported_filter_contract_can_be_verified(self):
        tasks = export.CONSUMER_TASK_TYPES
        self.document["exploration"] = export._exploration_block(
            1,
            {task: [] for task in tasks},
            {task: {"ranking": []} for task in tasks},
            1793872800,
        )
        self.assertTrue(export.publish_document(self.document, state_dir=self.state))
        commit = self.git(self.remote, "rev-parse", export.EXPORT_BRANCH)
        self.assertEqual(self.record(self.provenance(commit))["published_commit"], commit)

    def test_local_unpublished_commit_is_rejected(self):
        self.publish_filter()
        target = self.checkout / export.EXPORT_PATH
        changed = copy.deepcopy(self.document)
        changed["source_version"] += 1
        target.write_text(json.dumps(changed), encoding="utf-8")
        self.git(self.checkout, "add", ".")
        self.git(self.checkout, "commit", "-m", "not pushed")
        local = self.git(self.checkout, "rev-parse", "HEAD")
        with self.assertRaisesRegex(ValueError, "first verified"):
            self.record(self.provenance(local))

    def test_recorded_provenance_cannot_be_replaced(self):
        provenance = self.provenance(self.publish_filter())
        self.record(provenance)
        with self.assertRaisesRegex(ValueError, "refusing to replace"):
            self.record({**provenance, "deployed_at": "2026-11-05T11:30:00Z"})

    def test_fetch_failure_creates_no_provenance(self):
        provenance = self.provenance(self.publish_filter())
        failure = subprocess.CalledProcessError(1, "git")
        with patch.object(export, "_fetch_export", side_effect=failure):
            with self.assertRaises(subprocess.CalledProcessError):
                self.record(provenance)
        self.assertFalse((self.state / export.CHALLENGER_DEPLOY_FILE).exists())

    def test_cli_records_without_building_or_publishing(self):
        provenance = self.provenance(self.publish_filter())
        path = self.root / "audit.json"
        path.write_text(json.dumps(provenance), encoding="utf-8")
        with (
            patch.object(
                export.sys,
                "argv",
                [
                    "export",
                    "--record-challenger-deployment",
                    str(path),
                    "--state-dir",
                    str(self.state),
                ],
            ),
            patch.object(export, "build_document", side_effect=AssertionError("unexpected build")),
            patch.object(
                export, "publish_document", side_effect=AssertionError("unexpected publish")
            ),
        ):
            self.assertEqual(export.main(), 0)
        path.write_text("{}", encoding="utf-8")
        with patch.object(
            export.sys, "argv", ["export", "--record-challenger-deployment", str(path)]
        ):
            self.assertEqual(export.main(), 1)


if __name__ == "__main__":
    unittest.main()
