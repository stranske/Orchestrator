"""Retrospective packet selection and resumable evidence failures."""

import json
import sqlite3
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import adjudicator_retro as retro
import feedback
import verifier_evidence


class RetrospectiveEvidenceTests(unittest.TestCase):
    def test_fetch_evidence_selects_the_validated_verifier_comment(self):
        decision = {
            "schema": verifier_evidence.MARKER,
            "repo": "owner/repo",
            "pr": 1,
            "head_sha": "a" * 40,
            "evaluated_sha": "b" * 40,
            "run_id": "123",
            "run_attempt": "1",
            "provider_verdicts": ["FAIL"],
            "ci_failed": False,
            "verdict": "NON_PASS",
        }
        finding = {
            "body": "Missing acceptance test\n"
            f"<!-- {verifier_evidence.MARKER} {json.dumps(decision)} -->",
            "url": "https://github.com/owner/repo/pull/1#issuecomment-1",
            "author": {"login": "github-actions[bot]"},
        }
        malformed = {
            **finding,
            "body": f"<!-- {verifier_evidence.MARKER} {{invalid}} -->",
            "url": "https://github.com/owner/repo/pull/1#issuecomment-2",
        }
        wrong_pr = {**finding, "url": "https://github.com/owner/repo/pull/2#issuecomment-3"}
        gate = {"name": "Gate", "conclusion": "SUCCESS", "detailsUrl": "gate-run"}
        diff = {"path": "tests/test_acceptance.py", "additions": 10, "deletions": 0}
        pr = {
            "number": 1,
            "state": "MERGED",
            "headRefOid": decision["head_sha"],
            "mergeCommit": {"oid": decision["evaluated_sha"]},
            "comments": {
                "pageInfo": {"hasPreviousPage": False},
                "nodes": [finding, wrong_pr, malformed],
            },
            "files": {"pageInfo": {"hasNextPage": False}, "nodes": [diff]},
            "commits": {
                "nodes": [
                    {
                        "commit": {
                            "statusCheckRollup": {
                                "contexts": {
                                    "pageInfo": {"hasNextPage": False},
                                    "nodes": [gate],
                                }
                            }
                        }
                    }
                ]
            },
        }
        read = patch.object(
            retro, "_gh_json", return_value={"data": {"repository": {"pullRequest": pr}}}
        )
        read.start()
        self.addCleanup(read.stop)
        packet = retro.build_packet(
            {"target": "owner/repo#1"},
            retro.fetch_evidence({"target": "owner/repo#1", "verifier_verdict": "NON_PASS"}),
        )
        assert packet["disputed_finding"] == {
            "ref": finding["url"],
            "body": finding["body"],
            "decision": decision,
        }
        assert packet["ground_truth_evidence"]["head_sha"] == decision["head_sha"]
        assert packet["ground_truth_evidence"]["merge_sha"] == decision["evaluated_sha"]
        assert packet["ground_truth_evidence"]["diff_summary"] == [diff]
        assert packet["ground_truth_evidence"]["gate_runs"] == [gate]

        # Even without malformed JSON, a matching marker on another PR is not the finding.
        pr["comments"]["nodes"] = [finding, wrong_pr]
        assert (
            retro.fetch_evidence({"target": "owner/repo#1", "verifier_verdict": "NON_PASS"})[
                "disputed_finding"
            ]["ref"]
            == finding["url"]
        )

    def test_evidence_timeout_is_saved_and_the_batch_continues(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        tmp_path = Path(temporary.name)
        private_brain = tmp_path / "brain.db"
        database = patch.object(feedback, "DB_PATH", private_brain)
        database.start()
        self.addCleanup(database.stop)
        with feedback._conn() as conn:
            conn.execute(
                "INSERT INTO runs(run_id,ts,target) VALUES ('original',?,'owner/repo#1')",
                (int(time.time()),),
            )
            conn.execute(
                "INSERT INTO outcomes(run_id,verifier_verdict,adjudicated_verdict,merged) "
                "VALUES ('original','NON_PASS','PASS',1)"
            )
        with sqlite3.connect(private_brain) as conn:
            conn.execute(
                "INSERT INTO runs(run_id,ts,target) VALUES ('second',?,'owner/repo#2')",
                (int(time.time()),),
            )
            conn.execute(
                "INSERT INTO outcomes(run_id,verifier_verdict,adjudicated_verdict,merged) "
                "VALUES ('second','NON_PASS','PASS',1)"
            )
            before = conn.execute("SELECT * FROM outcomes ORDER BY run_id").fetchall()
        reads = []

        def reader(row):
            reads.append(row["target"])
            if len(reads) == 1:
                raise subprocess.TimeoutExpired(["gh", "api", "graphql"], 120)
            return {
                "disputed_finding": {"body": "Missing test"},
                "ground_truth_evidence": {"diff_summary": "Added test", "gate_runs": ["gate-run"]},
            }

        def runner(**kwargs):
            return {"proposal": {"decision": "uphold_blocker"}, "role_run_id": "shadow-role"}

        path = tmp_path / "report.json"
        result = retro.run(
            dispatch=True,
            limit=2,
            path=path,
            db=private_brain,
            evidence_reader=reader,
            runner=runner,
        )
        assert len(reads) == 2
        assert "timed out" in result["rows"][0]["error"]
        assert "decision" not in result["rows"][0]
        assert result["rows"][1]["decision"] == "uphold_blocker"
        assert json.loads(path.read_text())["rows"] == result["rows"]
        with sqlite3.connect(private_brain) as conn:
            assert conn.execute("SELECT * FROM outcomes ORDER BY run_id").fetchall() == before

        retro.run(dispatch=True, path=path, db=private_brain, evidence_reader=reader, runner=runner)
        assert len(reads) == 2  # A failed attempt is not retried unless requested.
        retried = retro.run(
            dispatch=True,
            retry=True,
            limit=1,
            path=path,
            db=private_brain,
            evidence_reader=reader,
            runner=runner,
        )
        assert len(reads) == 3
        assert retried["rows"][0]["decision"] == "uphold_blocker"
