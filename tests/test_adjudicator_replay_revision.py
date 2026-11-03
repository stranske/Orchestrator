"""Native CLI replay binds the evaluated commit independently of the PR head."""

import contextlib
import hashlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import adjudicator_replay as replay
import adjudicator_retro as retro
import roles


class NativeReplayRevisionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.invalid")
        (self.repo / "base.txt").write_text("unchanged base")
        self.git("add", ".")
        self.git("commit", "-qm", "base")
        base = self.git("rev-parse", "HEAD")
        runner_path = "replay.py"
        runner_text = """import json
print(json.dumps([{"test": "control", "runs": [
    {"phase": "baseline", "exit": 0, "output": "pass"},
    {"phase": "broken", "exit": 1, "output": "AssertionError"},
    {"phase": "restored", "exit": 0, "output": "pass"}]}]))
"""
        (self.repo / runner_path).write_text(runner_text)
        (self.repo / "proof.txt").write_text("complete proof")
        self.git("add", ".")
        # A squash and the PR head can have the same tree but distinct identities.
        tree = self.git("write-tree")
        self.head = self.git("commit-tree", tree, "-p", base, "-m", "PR head")
        self.git("commit", "-qm", "evaluated squash")
        self.evaluated = self.git("rev-parse", "HEAD")
        self.assertNotEqual(self.head, self.evaluated)
        body = "## Acceptance Criteria\n- Inspect the proof.\n- Replay the control.\n"
        self.row = {
            "case_id": "original-case",
            "target": "owner/repo#1",
            "run_id": "original-run",
            "decision": "needs_more_evidence",
            "retry_attempts": [{"backend_run_id": "original-paid"}],
            "packet": {
                "target": "owner/repo#1",
                "disputed_finding": {
                    "body": "Proof was unavailable",
                    "decision": {"evaluated_sha": self.evaluated},
                },
                "ground_truth_evidence": {
                    "head_sha": self.head,
                    "merge_sha": self.evaluated,
                    "diff_summary": [{"path": "proof.txt"}, {"path": runner_path}],
                    "gate_runs": ["gate-run"],
                },
            },
            "collection_requirements": {
                "source_paths": ["proof.txt", runner_path],
                "acceptance": [{"criterion": "proof", "location": "git-path:proof.txt"}],
            },
            "inventory_provenance": {
                "source_criterion_tasks": body,
                "pr_body_sha256": hashlib.sha256(body.encode()).hexdigest(),
            },
        }
        self.qualification = {
            "comparison_base": base,
            "snapshot": {"number": 1, "headRefOid": self.head, "body": body},
            "criterion_mapping": [
                {"criterion": text, "evidence_locations": ["git-path:proof.txt"], "unknowns": []}
                for text in ("Inspect the proof.", "Replay the control.")
            ],
        }
        self.runner = replay.ReviewedRunner(
            runner_path,
            hashlib.sha256(runner_text.encode()).hexdigest(),
            ("control",),
            self.row["case_id"],
            self.evaluated,
            (),
        )
        self.original = self.root / "original.json"
        self.canonical = self.root / "canonical.json"
        self.journal = retro.NativeAttemptJournal(self.root / "native.sqlite")
        self.outcomes = self.root / "outcomes.sqlite"
        with sqlite3.connect(self.outcomes) as conn:
            conn.execute(
                "CREATE TABLE outcomes (run_id TEXT, durability TEXT, durability_checked_ts INT)"
            )
        self.enterContext(patch.object(replay, "REVIEWED_RUNNERS", {runner_path: self.runner}))
        self.enterContext(patch.object(retro.feedback, "DB_PATH", self.outcomes))
        self.enterContext(patch.object(roles.feedback, "record_role_run", return_value=None))
        self.enterContext(
            patch.object(
                retro.feedback,
                "role_cost_report",
                return_value={"status": "unmeasured", "cost_usd": None},
            )
        )
        self.dispatch = self.enterContext(patch.object(roles.dispatcher, "offload"))
        self.dispatch.side_effect = AssertionError("refresh must never dispatch")

    def git(self, *args):
        environment = {
            key: value for key, value in os.environ.items() if not key.startswith("GIT_")
        }
        return subprocess.check_output(
            ["git", "-C", str(self.repo), *args], text=True, env=environment
        ).strip()

    def prepare(self):
        self.original.write_text(
            json.dumps({"rows": [self.row], "summary": {"graded": 0}, "population": 299})
        )
        self.canonical.write_bytes(self.original.read_bytes())
        (self.root / "qualification.json").write_text(json.dumps(self.qualification))
        self.manifest = self.root / "manifest.json"
        self.manifest.write_text(
            json.dumps(
                [
                    {
                        "case_id": self.row["case_id"],
                        "input_report": self.original.name,
                        "repository": str(self.repo),
                        "qualification": "qualification.json",
                        "replay_runner": self.runner.path,
                    }
                ]
            )
        )
        receipt = replay.collect_replay(
            self.repo,
            self.row["case_id"],
            self.evaluated,
            self.runner.path,
            self.runner.sha256,
            self.runner.tests,
        )
        case = retro.qualify_collected_case(
            self.row, self.repo, self.qualification, replay_receipt=receipt
        )
        proposal = {
            "decision": "reject_blocker",
            "confidence": "high",
            "rationale": "The supplied proof contradicts the blocker.",
            "ground_truth_refs": ["git-path:proof.txt"],
            "recommended_next_step": "Inspect the historical UNKNOWN separately.",
            "evidence_gaps": ["Historical execution remains UNKNOWN."],
            "evidence_assessment": [
                {
                    "claim": "proof unavailable",
                    "status": "contradicted",
                    "evidence_ref": "git-path:proof.txt",
                    "reason": "Complete evaluated proof supplied.",
                }
            ],
        }
        raw = json.dumps(proposal)
        reserved = self.journal.reserve(
            self.row["case_id"],
            retro._native_substantive_revision(case),
            case._digest,
            hashlib.sha256(self.original.read_bytes()).hexdigest(),
        )
        self.journal.persist_raw(reserved["attempt_id"], {"run_id": "native-paid", "output": raw})
        result = roles.run_adjudicator_agent(case=case, proposal_json=proposal)
        self.assertFalse(result["errors"])
        result.update(
            backend_run_id="native-paid", raw_output=raw, case=roles._collected_snapshot(case)
        )
        self.journal.validate(reserved["attempt_id"], result)

    def invoke(self, mode, output):
        argv = [
            "retro",
            "--report",
            str(self.original),
            "--native-journal",
            str(self.journal.path),
            "--expected-source-hash",
            hashlib.sha256(self.original.read_bytes()).hexdigest(),
            "--cohort-manifest",
            str(self.manifest),
        ]
        if mode == "publish":
            argv += [
                "--native-publish",
                str(self.canonical),
                "--expected-canonical-hash",
                hashlib.sha256(self.canonical.read_bytes()).hexdigest(),
            ]
        else:
            argv += ["--native-cohort", "--output", str(output)]
        with patch.object(sys, "argv", argv), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(retro.main(), 0)
        return json.loads((self.canonical if mode == "publish" else output).read_text())

    def check_refresh(self, mode, merge_fallback=False):
        if merge_fallback:
            self.row["packet"]["disputed_finding"]["decision"].pop("evaluated_sha")
        self.prepare()
        source = self.original.read_bytes()
        journal = self.journal.path.read_bytes()
        qualification = (self.root / "qualification.json").read_bytes()
        for expected in (0, 1):
            truth = self.outcomes.read_bytes()
            report = self.invoke(mode, self.root / f"candidate-{expected}.json")
            self.assertEqual(report["native_summary"]["graded"], expected)
            projected = report["rows"][0].pop("native_projection")
            self.assertTrue(projected["trusted"])
            self.assertEqual(report["rows"], [self.row])
            self.assertEqual(self.original.read_bytes(), source)
            self.assertEqual(self.journal.path.read_bytes(), journal)
            self.assertEqual(self.outcomes.read_bytes(), truth)
            self.assertEqual((self.root / "qualification.json").read_bytes(), qualification)
            if mode == "publish":
                self.assertEqual(report["summary"]["graded"], expected)
                self.assertEqual(
                    report["native_publication"]["paid_backend_ids"],
                    ["native-paid", "original-paid"],
                )
            if expected == 0:
                with sqlite3.connect(self.outcomes) as conn:
                    conn.execute(
                        "INSERT INTO outcomes (run_id, durability, durability_checked_ts) "
                        "VALUES (?, ?, ?)",
                        ("original-run", "durable", retro.feedback.DURABILITY_DETECTION_SINCE),
                    )
        self.dispatch.assert_not_called()

    def test_publish_replays_evaluated_merge_with_distinct_pr_head(self):
        self.check_refresh("publish")

    def test_cohort_replays_evaluated_merge_with_distinct_pr_head(self):
        self.check_refresh("cohort")

    def test_publish_uses_merge_anchor_when_finding_has_no_evaluated_sha(self):
        self.check_refresh("publish", merge_fallback=True)

    def test_cohort_uses_merge_anchor_when_finding_has_no_evaluated_sha(self):
        self.check_refresh("cohort", merge_fallback=True)

    def check_refusal(self, message, replay_called):
        inputs = [self.original, self.canonical, self.journal.path]
        before = [path.read_bytes() for path in inputs]
        for mode in ("cohort", "publish"):
            with self.subTest(mode=mode):
                output = self.root / "refused.json"
                error = io.StringIO()
                with (
                    patch.object(replay, "collect_replay", wraps=replay.collect_replay) as collect,
                    contextlib.redirect_stderr(error),
                    self.assertRaises(SystemExit) as raised,
                ):
                    self.invoke(mode, output)
                self.assertEqual(raised.exception.code, 2)
                self.assertIn(message, error.getvalue())
                self.assertEqual(collect.called, replay_called)
                self.assertFalse(output.exists())
                self.assertEqual([path.read_bytes() for path in inputs], before)
        self.dispatch.assert_not_called()

    def test_missing_evaluated_anchor_refuses_before_replay(self):
        self.prepare()
        self.row["packet"]["disputed_finding"]["decision"].pop("evaluated_sha")
        self.row["packet"]["ground_truth_evidence"].pop("merge_sha")
        self.original.write_text(json.dumps({"rows": [self.row]}))
        self.check_refusal("cohort replay requires an exact evaluated commit", replay_called=False)

    def test_invalid_evaluated_anchor_cannot_fall_back_to_snapshot_head(self):
        self.prepare()
        self.row["packet"]["disputed_finding"]["decision"]["evaluated_sha"] = "not-a-sha"
        self.original.write_text(json.dumps({"rows": [self.row]}))
        self.check_refusal("cohort replay requires an exact evaluated commit", replay_called=False)

    def test_reviewed_runner_subject_binding_remains_required(self):
        self.prepare()
        runner = replace(self.runner, evaluated_sha=self.head)
        with patch.object(replay, "REVIEWED_RUNNERS", {runner.path: runner}):
            self.check_refusal("caller subject does not match", replay_called=True)
