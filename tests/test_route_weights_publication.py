"""Verify capacity publication against a real, isolated Git remote."""

import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import capacity
import route_weights_export as export


def _git(repository: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repository), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


class CapacityPublicationTests(unittest.TestCase):
    def test_capacity_change_detection_against_published_export(self):
        for change in ("unchanged", "observed_at", "state", "window", "legacy"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory:
                # Every fetch/push stays local; commit identity is independent of the runner.
                identity = {
                    "GIT_AUTHOR_NAME": "Export Test",
                    "GIT_AUTHOR_EMAIL": "export-test@example.invalid",
                    "GIT_COMMITTER_NAME": "Export Test",
                    "GIT_COMMITTER_EMAIL": "export-test@example.invalid",
                }
                with patch.dict(os.environ, identity):
                    self._check_publication(Path(directory), change)

    def test_evidence_regressions_preserve_remote_unless_deliberately_allowed(self):
        for change in ("empty", "all_false", "lost_task"):
            for allowed in (False, True):
                with (
                    self.subTest(change=change, allowed=allowed),
                    tempfile.TemporaryDirectory() as directory,
                    patch.dict(
                        os.environ,
                        {
                            "GIT_AUTHOR_NAME": "Export Test",
                            "GIT_AUTHOR_EMAIL": "export-test@example.invalid",
                            "GIT_COMMITTER_NAME": "Export Test",
                            "GIT_COMMITTER_EMAIL": "export-test@example.invalid",
                        },
                    ),
                ):
                    self._check_publication(Path(directory), change, allowed=allowed)

    def _check_publication(self, root: Path, change: str, *, allowed: bool = False) -> None:
        remote = root / "remote.git"
        remote.mkdir()
        _git(remote, "init", "--bare", "--initial-branch=main")
        seed = root / "seed"
        _git(root, "clone", str(remote), str(seed))
        (seed / "README.md").write_text("fixture\n", encoding="utf-8")
        _git(seed, "add", "README.md")
        _git(seed, "commit", "-m", "fixture main")
        _git(seed, "push", "origin", "main")

        snapshot = {
            "generated_at": 1_700_000_000,
            "agents": {
                agent: {"state": capacity.OK, "window": cfg["window"]}
                for agent, cfg in capacity.AGENTS.items()
            },
        }
        database = root / "weights.db"
        export._fixture_db(database)
        previous = export.build_document(
            database,
            now=100,
            capacity_snapshot=snapshot,
            include_capacity=change != "legacy",
        )
        if change == "lost_task":
            previous["task_types"]["review"] = {
                "evidence_ok": True,
                "ranking": [{"agent": "codex"}],
            }
        _git(seed, "switch", "-c", export.EXPORT_BRANCH)
        export.write_document(seed / export.EXPORT_PATH, previous)
        _git(seed, "add", str(export.EXPORT_PATH))
        _git(seed, "commit", "-m", "fixture published export")
        _git(seed, "push", "origin", export.EXPORT_BRANCH)
        previous_head = _git(remote, "rev-parse", export.EXPORT_BRANCH)

        # Compare against a remote artifact without an existing local export branch.
        canonical = root / "canonical"
        _git(root, "clone", str(remote), str(canonical))
        if change == "observed_at":
            snapshot["generated_at"] += 3600
        elif change == "state":
            snapshot["agents"]["codex"]["state"] = capacity.SHED
        elif change == "window":
            snapshot["agents"]["codex"]["window"] = "1h"
        document = export.build_document(database, now=101, capacity_snapshot=snapshot)
        if change == "empty":
            document = export.build_document(
                root / "absent.db", now=101, capacity_snapshot=snapshot
            )
        elif change == "all_false":
            for entry in document["task_types"].values():
                entry.update(evidence_ok=False, ranking=[])
        should_publish = change in {"state", "window", "legacy"}

        with patch.object(export.provision, "ensure_canonical", return_value=canonical):
            if change in {"empty", "all_false", "lost_task"}:
                if allowed:
                    with contextlib.redirect_stderr(io.StringIO()) as errors:
                        self.assertTrue(
                            export.publish_document(document, allow_evidence_regression=True)
                        )
                    self.assertIn("deliberate evidence regression allowed", errors.getvalue())
                    self.assertEqual(
                        json.loads(
                            _git(remote, "show", f"{export.EXPORT_BRANCH}:{export.EXPORT_PATH}")
                        ),
                        document,
                    )
                else:
                    with self.assertRaisesRegex(ValueError, "publish_guard refused"):
                        export.publish_document(document)
                    self.assertEqual(_git(remote, "rev-parse", export.EXPORT_BRANCH), previous_head)
                    self.assertEqual(_git(canonical, "branch", "--show-current"), "main")
                    self.assertFalse((canonical / export.EXPORT_PATH).exists())
                return
            self.assertIs(export.publish_document(document), should_publish)
            published_head = _git(remote, "rev-parse", export.EXPORT_BRANCH)
            self.assertIs(published_head != previous_head, should_publish)
            published = json.loads(
                _git(remote, "show", f"{export.EXPORT_BRANCH}:{export.EXPORT_PATH}")
            )
            self.assertEqual(published, document if should_publish else previous)
            if should_publish:
                self.assertEqual(_git(remote, "rev-parse", f"{published_head}^"), previous_head)
                self.assertEqual(
                    _git(
                        remote, "diff-tree", "--no-commit-id", "--name-only", "-r", published_head
                    ),
                    str(export.EXPORT_PATH),
                )

            # A daily rerun uses the existing local branch and must not create another commit.
            self.assertIs(export.publish_document(document), False)
            self.assertEqual(_git(remote, "rev-parse", export.EXPORT_BRANCH), published_head)


if __name__ == "__main__":
    unittest.main()
