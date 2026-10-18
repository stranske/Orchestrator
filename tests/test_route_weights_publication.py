"""Verify capacity publication against a real, isolated Git remote."""

import json
import os
import sqlite3
import subprocess
import tempfile
import time
import unittest
from contextlib import closing
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
    def test_database_uri_characters_preserve_the_exact_evidence_source(self):
        for name in ("weights?.db", "weights#.db", "weights%23.db"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                database = root / name
                export._fixture_db(database)
                # A wrongly parsed URI must not silently read this neighboring DB.
                neighbor = root / "weights"
                export._fixture_db(neighbor)
                with closing(sqlite3.connect(neighbor)) as connection:
                    connection.execute("UPDATE route_weights SET version=99")
                    connection.commit()
                before = database.read_bytes()
                files_before = set(root.iterdir())

                document = export.build_document(
                    database, now=100, capacity_snapshot={"agents": {}, "generated_at": 100}
                )

                self.assertEqual(document["source_version"], 2)
                self.assertTrue(document["task_types"]["implement"]["evidence_ok"])
                self.assertEqual(
                    [row["agent"] for row in document["task_types"]["implement"]["ranking"]],
                    ["codex"],
                )
                self.assertEqual(database.read_bytes(), before)
                self.assertEqual(set(root.iterdir()), files_before)

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

    def _check_publication(self, root: Path, change: str) -> None:
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
            now=int(time.time()),
            capacity_snapshot=snapshot,
            include_capacity=change != "legacy",
        )
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
        document = export.build_document(
            database, now=int(time.time()) + 1, capacity_snapshot=snapshot
        )
        should_publish = change in {"state", "legacy"}
        state_dir = root / "stamps"
        if change == "state":
            # A state change is semantic only after two actual local tick observations.
            local = root / "capacity.json"
            now = int(time.time())
            with patch.object(capacity, "OUT", local):
                for observed in (now - 3600, now):
                    local.write_text(json.dumps({**snapshot, "generated_at": observed}))
                    self.assertTrue(export.record_capacity_tick(state_dir, now=observed))

        with patch.object(export.provision, "ensure_canonical", return_value=canonical):
            self.assertIs(export.publish_document(document, state_dir=state_dir), should_publish)
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

            # An hourly rerun uses the existing local branch and must not create another commit.
            self.assertIs(export.publish_document(document, state_dir=state_dir), False)
            self.assertEqual(_git(remote, "rev-parse", export.EXPORT_BRANCH), published_head)


if __name__ == "__main__":
    unittest.main()
