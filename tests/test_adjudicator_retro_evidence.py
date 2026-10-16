"""Retrospective packet selection and resumable evidence failures."""

import json
import sqlite3
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import adjudicator_retro as retro
import capabilities
import feedback
import roles
import verifier_evidence


class RetrospectiveEvidenceTests(unittest.TestCase):
    def test_retry_repairs_brain_record_without_repeating_paid_verdict(self):
        for partial_write in (False, True):
            with self.subTest(partial_write=partial_write), tempfile.TemporaryDirectory() as root:
                self._exercise_record_recovery(Path(root), partial_write)

    def _exercise_record_recovery(self, root, partial_write):
        db = root / "brain.db"
        proposal = {
            "decision": "uphold_blocker",
            "confidence": "high",
            "rationale": "The finding needs inspection.",
            "evidence_assessment": [
                {
                    "claim": "Missing test",
                    "status": "supported",
                    "evidence_ref": "gate-run",
                    "reason": "Inspect the gate evidence",
                }
            ],
            "ground_truth_refs": ["gate-run"],
            "recommended_next_step": "Inspect the regression evidence",
            "evidence_gaps": [],
        }
        reader = Mock(
            return_value={
                "disputed_finding": {"body": "Missing acceptance test"},
                "ground_truth_evidence": {"diff_summary": "Added test", "gate_runs": ["gate-run"]},
            }
        )
        offload = Mock(
            return_value={
                "run_id": "backend-one",
                "model": "gemini-test-model",
                "output": json.dumps(proposal),
                "exit": 0,
            }
        )
        recorder = feedback.record_role_run
        attempts = []

        def unavailable(run_id, role_name, target, agent, **kwargs):
            kwargs.update(run_id=run_id, role_name=role_name, target=target, agent=agent)
            attempts.append(kwargs)
            if partial_write:
                recorder(**kwargs)
            raise sqlite3.OperationalError("Brain temporarily unavailable")

        with (
            patch.object(feedback, "DB_PATH", db),
            patch.object(capabilities, "REG", root / "capabilities.json"),
            patch.dict("os.environ", {"ORCH_CAPABILITIES_PATH": str(root / "capabilities.json")}),
            patch.object(roles, "route_role", return_value={"agent": "gemini"}),
            patch.object(roles, "_role_capability_event"),
            patch.object(roles.dispatcher, "offload", offload),
        ):
            with feedback._conn() as conn:
                conn.execute(
                    "INSERT INTO runs(run_id,ts,target) VALUES ('original',?,'owner/repo#1')",
                    (int(time.time()),),
                )
                conn.execute(
                    "INSERT INTO outcomes(run_id,verifier_verdict,adjudicated_verdict,merged) "
                    "VALUES ('original','NON_PASS','PASS',1)"
                )
                before = conn.execute("SELECT * FROM outcomes ORDER BY run_id").fetchall()
            path = root / "retro.json"
            with patch.object(feedback, "record_role_run", side_effect=unavailable):
                first = retro.run(dispatch=True, path=path, db=db, evidence_reader=reader)
                row = first["rows"][0]
                assert row["proposal"] == proposal
                assert row["decision"] == "uphold_blocker"
                assert row["role_run_id"] is None
                assert row["role_record_error"] == "Brain temporarily unavailable"
                assert row["role_record"] == attempts[0]
                assert json.loads(path.read_text()) == first

                # A pending record can outlive the replay window. Dry runs,
                # ordinary resumes and a zero retry limit must leave it pending.
                with patch.object(retro, "disputes", return_value=[]):
                    for options in (
                        {"dispatch": False, "retry": True},
                        {"dispatch": True},
                        {"dispatch": True, "retry": True, "limit": 0},
                    ):
                        pending = retro.run(path=path, db=db, **options)
                        assert pending["rows"][0]["role_record_error"]
                    assert len(attempts) == 1
                    failed = retro.run(dispatch=True, retry=True, path=path, db=db)
                    assert failed["rows"][0]["role_record_error"]
                    assert attempts[1] == attempts[0]

            with patch.object(retro, "disputes", return_value=[]):
                repaired = retro.run(dispatch=True, retry=True, path=path, db=db)
                saved = repaired["rows"][0]
                assert saved["role_run_id"] == attempts[0]["run_id"]
                assert saved["role_record_error"] is None
                assert saved["proposal"] == proposal
                assert saved["disposition"] == "needs_more_evidence"
                assert saved["shadow_verdict"] is None
                assert json.loads(path.read_text()) == repaired
                assert (
                    retro.run(dispatch=True, retry=True, path=path, db=db)["rows"]
                    == repaired["rows"]
                )
            with sqlite3.connect(db) as conn:
                assert conn.execute("SELECT * FROM outcomes ORDER BY run_id").fetchall() == before
                records = conn.execute(
                    "SELECT run_id,ts,source,model,decomposition FROM runs "
                    "WHERE role_name='adjudicator'"
                ).fetchall()
            assert len(records) == 1
            run_id, ts, source, model, decomposition = records[0]
            assert run_id == attempts[0]["run_id"]
            assert ts == attempts[0]["ts"]
            assert source == "retrospective"
            assert model == "gemini-test-model"
            assert json.loads(decomposition)["proposal"] == proposal
            offload.assert_called_once()
            reader.assert_called_once()

    def test_saved_verdicts_refresh_after_leaving_the_dispute_window(self):
        now = feedback.DURABILITY_DETECTION_SINCE + 100 * 86400
        with tempfile.TemporaryDirectory() as temporary, patch.dict(
            "os.environ", {"ORCH_STATE_DIR": temporary}
        ):
            db = Path(temporary) / "brain.db"
            with patch.object(feedback, "DB_PATH", db), feedback._conn() as conn:
                conn.execute(
                    "INSERT INTO runs(run_id,ts,target) VALUES ('original',?,'owner/repo#1')",
                    (now - 89 * 86400,),
                )
                conn.execute(
                    "INSERT INTO outcomes(run_id,verifier_verdict,adjudicated_verdict,merged) "
                    "VALUES ('original','NON_PASS','PASS',1)"
                )
                conn.execute(
                    "INSERT INTO costs(run_id,cost_usd,source) VALUES ('backend-one',0,'ledger')"
                )
            reader = Mock(
                return_value={
                    "disputed_finding": {"body": "Missing acceptance test"},
                    "ground_truth_evidence": {
                        "diff_summary": "Added test",
                        "gate_runs": ["gate-run"],
                    },
                }
            )
            runner = Mock(
                return_value={
                    "proposal": {"decision": "uphold_blocker"},
                    "role_run_id": "shadow-role",
                    "backend_run_id": "backend-one",
                }
            )
            with patch.object(retro.time, "time", return_value=now):
                initial = retro.run(dispatch=True, db=db, evidence_reader=reader, runner=runner)
            assert initial["summary"]["graded"] == 0
            assert initial["summary"]["cost_per_case"] is None
            path = Path(temporary) / "capability-program/adjudicator-retro.json"
            assert path.exists()

            # The original dispute ages out before its later failure and complete cost arrive.
            with sqlite3.connect(db) as conn:
                conn.execute(
                    "UPDATE costs SET cost_usd=2,source='ccusage' WHERE run_id='backend-one'"
                )
            for durability, truth in (
                ("reverted", "FAIL"),
                ("broke_later", "FAIL"),
                ("reopened", "FAIL"),
                ("abandoned", "FAIL"),
                ("reworked", "FAIL"),
                ("durable", "PASS"),
                ("pending", None),
            ):
                with self.subTest(durability=durability):
                    with sqlite3.connect(db) as conn:
                        conn.execute(
                            "UPDATE outcomes SET durability=?,durability_checked_ts=? "
                            "WHERE run_id='original'",
                            (durability, now + 2 * 86400),
                        )
                        before = conn.execute("SELECT * FROM outcomes ORDER BY run_id").fetchall()
                    with patch.object(retro.time, "time", return_value=now + 2 * 86400):
                        refreshed = retro.run(
                            dispatch=True, db=db, evidence_reader=reader, runner=runner
                        )
                    assert json.loads(path.read_text()) == refreshed
                    assert refreshed["population"] == 0
                    assert refreshed["rows"][0]["role_run_id"] == "shadow-role"
                    assert refreshed["rows"][0]["later_truth"] == truth
                    summary = refreshed["summary"]
                    assert summary["cases"] == summary["proposed_decisions"] == 1
                    assert summary["adjudicated"] == summary["graded"] == 0
                    assert summary["agree"] == summary["disagree"] == 0
                    assert summary["agreement_rate"] is None
                    assert summary["merge_rule_agreement_rate"] is None
                    assert refreshed["rows"][0]["proposal"]["decision"] == "uphold_blocker"
                    assert refreshed["rows"][0]["disposition"] == "needs_more_evidence"
                    assert refreshed["rows"][0]["shadow_verdict"] is None
                    assert summary["cost_measured_cases"] == 1
                    assert summary["cost_usd"] == summary["cost_per_case"] == 2
                    comparison = refreshed["proposal_comparison"]
                    assert comparison["evidence_basis"] == "raw_metadata_proposals"
                    assert comparison["cases"] == comparison["proposed_decisions"] == 1
                    assert comparison["compared"] == int(truth is not None)
                    assert comparison["pending_truth"] == int(truth is None)
                    assert comparison["agree"] == int(truth == "FAIL")
                    assert comparison["disagree"] == int(truth == "PASS")
                    assert comparison["agreement_rate"] == (
                        float(truth == "FAIL") if truth else None
                    )
                    assert comparison["merge_rule_agreement_rate"] == (
                        float(truth == "PASS") if truth else None
                    )
                    assert comparison["cost_measured_cases"] == 1
                    assert comparison["cost_usd"] == comparison["cost_per_case"] == 2
                    assert refreshed["rows"][0]["proposal_comparison"] == {
                        "verdict": "FAIL",
                        "agrees": truth == "FAIL" if truth else None,
                        "merge_rule_agrees": truth == "PASS" if truth else None,
                    }
                    with sqlite3.connect(db) as conn:
                        after = conn.execute("SELECT * FROM outcomes ORDER BY run_id").fetchall()
                        assert after == before
            reader.assert_called_once()
            runner.assert_called_once()

    def test_incomplete_packet_evidence_never_dispatches(self):
        row = {
            "run_id": "original",
            "target": "owner/repo#1",
            "verifier_verdict": "NON_PASS",
            "adjudicated_verdict": "PASS",
            "merged": 1,
        }
        valid = {
            "disputed_finding": {"body": "Missing acceptance test"},
            "ground_truth_evidence": {"diff_summary": "Added test", "gate_runs": ["gate-run"]},
        }
        invalid = [
            {**valid, "disputed_finding": finding}
            for finding in (
                "verifier-comment",
                {"ref": "verifier-comment"},
                {"body": "  "},
                {"body": ["Missing test"]},
            )
        ]
        invalid.append({**valid, "ground_truth_evidence": "merge=PASS"})
        for key, values in (
            ("diff_summary", (None, "", "  ", [], {"ref": "diff"})),
            ("gate_runs", (None, [], "gate-run", {"ref": "gate-run"})),
        ):
            for value in values:
                invalid.append(
                    {
                        **valid,
                        "ground_truth_evidence": {**valid["ground_truth_evidence"], key: value},
                    }
                )
        with tempfile.TemporaryDirectory() as temporary, patch.object(
            retro, "disputes", return_value=[row]
        ):
            runner = Mock(side_effect=AssertionError("incomplete packet dispatched"))
            for packet in invalid:
                with self.subTest(packet=packet):
                    with self.assertRaises(ValueError):
                        retro.build_packet(row, packet)
                    result = retro.run(
                        dispatch=True,
                        retry=True,
                        path=Path(temporary) / "report.json",
                        evidence_reader=lambda _row: packet,
                        runner=runner,
                    )
                    self.assertIn("error", result["rows"][0])
                    self.assertEqual(result["summary"]["graded"], 0)
                    self.assertIsNone(result["summary"]["agreement_rate"])
            runner.assert_not_called()

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
        patch_text = "diff --git a/tests/test_acceptance.py b/tests/test_acceptance.py\n"
        read_patch = patch.object(
            retro.subprocess,
            "run",
            return_value=subprocess.CompletedProcess([], 0, patch_text, ""),
        )
        read_patch.start()
        self.addCleanup(read_patch.stop)
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
        assert packet["ground_truth_evidence"]["diff_patch"] == {
            "revision": decision["head_sha"],
            "text": patch_text,
        }
        assert packet["ground_truth_evidence"]["gate_runs"] == [gate]

        # Every linked source retains its own criteria revision, including duplicate text.
        pr["closingIssuesReferences"] = {
            "nodes": [
                {
                    "number": 2,
                    "body": "## Acceptance Criteria\n- [ ] first requirement",
                    "updatedAt": "2026-10-01T01:00:00Z",
                },
                {
                    "number": 3,
                    "body": "## Acceptance Criteria\n- [ ] second requirement",
                    "updatedAt": "2026-10-01T02:00:00Z",
                },
            ]
        }
        ground = retro.fetch_evidence({"target": "owner/repo#1", "verifier_verdict": "NON_PASS"})[
            "ground_truth_evidence"
        ]
        assert [c["text"] for c in ground["acceptance_criteria"]] == [
            "first requirement",
            "second requirement",
        ]
        assert [c["source"] for c in ground["acceptance_criteria"]] == [
            "issue#2@2026-10-01T01:00:00Z",
            "issue#3@2026-10-01T02:00:00Z",
        ]
        assert retro._packet_metadata_only(ground) is False
        with patch.object(retro, "MAX_DIFF_PATCH_BYTES", 5):
            clipped = retro.fetch_evidence(
                {"target": "owner/repo#1", "verifier_verdict": "NON_PASS"}
            )["ground_truth_evidence"]
        assert clipped["diff_patch"]["truncated"] is True
        assert len(clipped["diff_patch"]["text"].encode("utf-8")) <= 5
        assert retro._packet_metadata_only(clipped) is True

        # Even without malformed JSON, a matching marker on another PR is not the finding.
        pr["comments"]["nodes"] = [finding, wrong_pr]
        assert (
            retro.fetch_evidence({"target": "owner/repo#1", "verifier_verdict": "NON_PASS"})[
                "disputed_finding"
            ]["ref"]
            == finding["url"]
        )
        pr["mergeCommit"] = None
        with self.assertRaisesRegex(ValueError, "merge commit unavailable"):
            retro.fetch_evidence({"target": "owner/repo#1", "verifier_verdict": "NON_PASS"})

    def _criteria_evidence(self, issues, merged_at="2026-10-01T00:00:00Z"):
        """Read a merged PR linked to ``issues``; return its ground truth and the gh calls made."""
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
        gate = {"name": "Gate", "conclusion": "SUCCESS", "detailsUrl": "gate-run"}
        diff = {"path": "tests/test_acceptance.py", "additions": 10, "deletions": 0}
        contexts = {"pageInfo": {"hasNextPage": False}, "nodes": [gate]}
        pr = {
            "number": 1,
            "state": "MERGED",
            "headRefOid": decision["head_sha"],
            "mergedAt": merged_at,
            "mergeCommit": {"oid": decision["evaluated_sha"]},
            "comments": {"pageInfo": {"hasPreviousPage": False}, "nodes": [finding]},
            "files": {"pageInfo": {"hasNextPage": False}, "nodes": [diff]},
            "closingIssuesReferences": {"nodes": issues},
            "commits": {"nodes": [{"commit": {"statusCheckRollup": {"contexts": contexts}}}]},
        }
        patch_text = "diff --git a/tests/test_acceptance.py b/tests/test_acceptance.py\n"
        # No parenthesized ``with ... as``: black reads it as a Python 3.9+ marker for the whole
        # file and rewrites every older multi-item ``with`` above, which is not this change's diff.
        gh = patch.object(
            retro, "_gh_json", return_value={"data": {"repository": {"pullRequest": pr}}}
        )
        diff_read = patch.object(
            retro.subprocess,
            "run",
            return_value=subprocess.CompletedProcess([], 0, patch_text, ""),
        )
        with gh as read, diff_read:
            evidence = retro.fetch_evidence(
                {"target": "owner/repo#1", "verifier_verdict": "NON_PASS"}
            )
        return evidence["ground_truth_evidence"], read.call_args_list

    def test_post_merge_activity_alone_does_not_refuse_the_criteria(self):
        merged = "2026-10-01T00:00:00Z"
        after = "2026-10-02T00:00:00Z"
        body = "## Acceptance Criteria\n- [ ] {}"
        ground, _ = self._criteria_evidence(
            [
                # A comment, label or reopen moved updatedAt; the body was never edited.
                {
                    "number": 2,
                    "body": body.format("never edited"),
                    "updatedAt": after,
                    "lastEditedAt": None,
                },
                # Last edited before the merge, then touched again after it.
                {
                    "number": 3,
                    "body": body.format("edited before merge"),
                    "updatedAt": after,
                    "lastEditedAt": "2026-09-30T00:00:00Z",
                },
                # An edit in the very second of the merge is not after it.
                {
                    "number": 4,
                    "body": body.format("edited at merge"),
                    "updatedAt": after,
                    "lastEditedAt": merged,
                },
            ],
            merged_at=merged,
        )
        assert ground["acceptance_criteria_errors"] == []
        assert [c["text"] for c in ground["acceptance_criteria"]] == [
            "never edited",
            "edited before merge",
            "edited at merge",
        ]
        assert retro._packet_metadata_only(ground) is False

    def test_a_body_edit_after_merge_still_refuses_the_criteria(self):
        merged = "2026-10-01T00:00:00Z"
        edited = "2026-10-01T00:00:01Z"
        refusal = (
            "linked issue body was edited after merge; "
            "merge-time acceptance criteria are unavailable"
        )
        rewritten = {
            "number": 2,
            "body": "## Acceptance Criteria\n- [ ] rewritten after merge",
            "updatedAt": edited,
            "lastEditedAt": edited,
        }
        ground, _ = self._criteria_evidence([rewritten], merged_at=merged)
        assert ground["acceptance_criteria"] == []
        assert ground["acceptance_criteria_errors"] == [{"issue": 2, "error": refusal}]
        # With no criteria left the packet stays metadata-only, so the evidence floor still
        # withholds a grade.
        assert retro._packet_metadata_only(ground) is True

        # The refusal is per issue: a sibling whose body never changed keeps its criteria.
        sibling = {
            "number": 3,
            "body": "## Acceptance Criteria\n- [ ] the merge-time text",
            "updatedAt": edited,
            "lastEditedAt": None,
        }
        ground, _ = self._criteria_evidence([rewritten, sibling], merged_at=merged)
        assert [c["text"] for c in ground["acceptance_criteria"]] == ["the merge-time text"]
        assert ground["acceptance_criteria_errors"] == [{"issue": 2, "error": refusal}]

    def test_the_pr_query_requests_the_linked_issues_last_edit_time(self):
        # Fixtures hand fetch_evidence ``lastEditedAt`` whether or not the query asks for it, so
        # the comparison above could pass while production never receives the field. Pin the
        # request itself: one mention, inside the closing-issues selection.
        _, calls = self._criteria_evidence([])
        query = next(arg for arg in calls[0].args[0] if arg.startswith("query="))
        selection = query.split("closingIssuesReferences", 1)[1].split("}", 1)[0]
        assert selection.split().count("lastEditedAt") == 1
        assert query.count("lastEditedAt") == 1

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
