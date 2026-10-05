"""Exercise durability recovery through persisted outcomes and both routing learners.

Role aggregation counts one observation per role: an attributable failure dominates
passes and excluded acting outcomes, independent of update order. Unknown attribution
contributes no observation. These tests use only stdlib fixtures and no GitHub API.

Run without pytest: PYTHONPATH=src python3 -m unittest discover -s tests
    -p test_durability_feedback_recovery.py -v
"""

from __future__ import annotations

import datetime as dt
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import durability_sweep
import feedback

DAY = 86400
NOW = int(time.time())
EXHAUSTED_REVERT = "revert PR search limit; revert commit search limit"


def _pr(number, *, merged_at, files=("src/app.py",)):
    return {
        "number": number,
        "state": "MERGED",
        "mergedAt": dt.datetime.fromtimestamp(merged_at, dt.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        "mergeCommit": {"oid": f"sha{number}"},
        "baseRefName": "main",
        "files": [{"path": path} for path in files],
    }


class DurabilityFeedbackRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.enterContext(patch.object(feedback, "DB_PATH", Path(self.tmp.name) / "brain.db"))
        self.enterContext(
            patch.dict(
                "os.environ",
                {"ORCH_STATE_DIR": self.tmp.name, "ORCH_RELEARN_HALF_LIFE_DAYS": "0"},
            )
        )
        self.enterContext(patch.object(durability_sweep, "_gh_throttle", lambda *_a: None))
        self.enterContext(patch.object(durability_sweep, "_run_json", lambda *_a, **_kw: []))

    def merged_run(self, run_id, *, target="o/r#42", mode="remote", recorded=NOW):
        feedback.record_run(run_id, target, "implement", "codex", mode=mode, ts=NOW - 40 * DAY)
        feedback.record_outcome(
            run_id, adjudicated_verdict="PASS", merged=True, durability="pending"
        )
        with feedback._conn() as conn:
            conn.execute(
                "UPDATE outcomes SET durability_checked_ts=? WHERE run_id=?", (recorded, run_id)
            )

    def row(self, run_id):
        with feedback._conn() as conn:
            return conn.execute(
                "SELECT durability,failure_class,notes FROM outcomes WHERE run_id=?", (run_id,)
            ).fetchone()

    def assert_learning(self, *, task_type="implement", agent="codex", observations):
        # Test the consumers, not merely _has_outcome_evidence: both must count the same failure.
        for learner in (feedback.relearn, feedback.relearn_quality):
            with self.subTest(learner=learner.__name__):
                version = learner({task_type: {agent: 0.5}}, window_days=90)
                with feedback._conn() as conn:
                    posterior, n_obs = conn.execute(
                        "SELECT posterior,n_obs FROM route_weights "
                        "WHERE version=? AND task_type=? AND agent=?",
                        (version, task_type, agent),
                    ).fetchone()
                self.assertEqual(n_obs, observations)
                self.assertAlmostEqual(
                    posterior,
                    feedback.PRIOR_STRENGTH * 0.5 / (feedback.PRIOR_STRENGTH + observations),
                )

    def sweep(self, **kwargs):
        return durability_sweep.sweep_durability(
            _now=NOW, _verifier_fetch_fn=lambda _repo, _nums: {}, **kwargs
        )

    def branch_lookup(self, prs):
        def gh(args, **_kwargs):
            if args[1:3] == ["pr", "view"]:
                self.assertEqual(args[3], "42", "ambiguous attribution fetched PR details")
                return None, "GraphQL: Could not resolve to a PullRequest with the number of 42"
            self.assertEqual(args[1:3], ["pr", "list"])
            self.assertEqual(args[args.index("--head") + 1], "orchestrator/issue-42")
            return prs, None

        return gh

    def test_missing_recording_time_is_persisted_as_excluded_not_provisional_success(self):
        self.merged_run("missing-time", mode="local", recorded=None)
        summary = self.sweep(_gh=self.branch_lookup([_pr(43, merged_at=NOW - DAY)]))
        self.assertEqual(
            (summary["unjudgeable"], summary["durable"], summary["skipped"]), (1, 0, 0)
        )
        durability, failure_class, notes = self.row("missing-time")
        self.assertEqual((durability, failure_class), ("unjudgeable", feedback.UNJUDGEABLE_MERGE))
        self.assertIn("missing outcome recording time", notes)
        self.assert_learning(observations=0)
        self.assertEqual(self.sweep(_gh=self.branch_lookup([]))["checked"], 0)

    def test_clock_skew_window_with_two_merges_remains_ambiguous_and_excluded(self):
        recorded = NOW - 39 * DAY
        self.merged_run("ambiguous-time", mode="local", recorded=recorded)
        prs = [
            _pr(43, merged_at=recorded - 1),
            _pr(44, merged_at=recorded + durability_sweep.INGEST_CLOCK_SKEW_S),
        ]
        summary = self.sweep(_gh=self.branch_lookup(prs))
        self.assertEqual((summary["unjudgeable"], summary["durable"]), (1, 0))
        self.assertIn("2 merges", self.row("ambiguous-time")[2])
        self.assert_learning(observations=0)

    def test_bookkeeping_failure_is_persisted_despite_exhausted_revert_search(self):
        self.merged_run("bookkeeping")
        summary = self.sweep(
            _state_fn=lambda _target: _pr(
                42, merged_at=NOW - 30 * DAY, files=(".agents/issue-42-ledger.yml",)
            ),
            _revert_fn=lambda _pr: (None, EXHAUSTED_REVERT),
        )
        self.assertEqual(
            (summary["abandoned"], summary["unjudgeable"], summary["judged"]), (1, 0, 1)
        )
        durability, failure_class, notes = self.row("bookkeeping")
        self.assertEqual((durability, failure_class), ("abandoned", None))
        self.assertIn("delivered nothing", notes)
        self.assert_learning(observations=1)
        self.assertEqual(self.sweep(_state_fn=lambda _target: None)["checked"], 0)

    def test_identified_repair_is_persisted_despite_exhausted_revert_search(self):
        self.merged_run("repaired")
        repair = {
            **_pr(43, merged_at=NOW - DAY),
            "title": "fix regression from PR #42",
            "body": "Fixes #42",
        }
        # Exercise the sweep's real cached repair matcher, rather than injecting its verdict.
        with patch.object(durability_sweep, "_fetch_repo_fix_prs", return_value=([repair], False)):
            summary = self.sweep(
                _state_fn=lambda _target: _pr(42, merged_at=NOW - 30 * DAY),
                _revert_fn=lambda _pr: (None, EXHAUSTED_REVERT),
            )
        self.assertEqual(
            (summary["broke_later"], summary["unjudgeable"], summary["judged"]), (1, 0, 1)
        )
        durability, failure_class, notes = self.row("repaired")
        self.assertEqual((durability, failure_class), ("broke_later", None))
        self.assertIn("#43", notes)
        self.assertIn(EXHAUSTED_REVERT, notes)
        self.assert_learning(observations=1)
        self.assertEqual(self.sweep(_state_fn=lambda _target: None)["checked"], 0)

    def test_ci_only_pending_outcome_cannot_displace_durable_role_evidence(self):
        for order in (("durable", "ci-only"), ("ci-only", "durable")):
            with self.subTest(order=order):
                role = "role:triage:gemini:" + "-".join(order)
                feedback.record_role_run(role, "triage", "triage:two-items", "gemini")
                for kind in order:
                    run = role + ":" + kind
                    feedback.record_run(
                        run,
                        "o/r#42",
                        "implement",
                        "codex",
                        mode="remote",
                        influenced_by_role_run_ids=[role],
                    )
                    feedback.record_outcome(
                        run,
                        merged=kind == "durable",
                        durability="durable" if kind == "durable" else "pending",
                        ci_status="SUCCESS" if kind == "durable" else "FAILURE",
                    )
                self.assertEqual(self.row(role)[0], "durable")
                for learner in (feedback.relearn, feedback.relearn_quality):
                    version = learner({"role:triage": {"gemini": 0.5}}, window_days=90)
                    with feedback._conn() as conn:
                        posterior, observations = conn.execute(
                            "SELECT posterior,n_obs FROM route_weights WHERE version=? "
                            "AND task_type=? AND agent=?",
                            (version, "role:triage", "gemini"),
                        ).fetchone()
                    self.assertGreater(observations, 0)
                    self.assertGreater(posterior, 0.5)

    def assert_role_failure_survives(self, order):
        role = "role:triage:gemini:multi"
        feedback.record_role_run(role, "triage", "triage:two-items", "gemini")
        for run in order:
            feedback.record_run(
                run,
                "o/r#42",
                "implement",
                "codex",
                mode="remote",
                influenced_by_role_run_ids=[role],
            )
        seen_failure = False
        for run in order:
            excluded = run == "unknown"
            feedback.record_outcome(
                run,
                adjudicated_verdict="PASS",
                merged=True,
                durability="unjudgeable" if excluded else "broke_later",
                failure_class=feedback.UNJUDGEABLE_MERGE if excluded else None,
                notes=f"evidence from {run}",
            )
            # Before the attributable failure arrives, the role contributes no evidence.
            seen_failure |= not excluded
            self.assert_learning(
                task_type="role:triage", agent="gemini", observations=int(seen_failure)
            )
        for run in order:
            feedback.record_outcome(run, notes=f"late evidence from {run}")
            durability, failure_class, notes = self.row(role)
            self.assertEqual((durability, failure_class), ("broke_later", None))
            self.assertIn("automatically influenced failed", notes)
            self.assertIn("evidence from failed", notes)
            # Repeated observations and two acting edges still count as ONE role observation.
            self.assert_learning(task_type="role:triage", agent="gemini", observations=1)

    def test_role_failure_after_excluded_outcome_reaches_both_learners(self):
        self.assert_role_failure_survives(("unknown", "failed"))

    def test_role_exclusion_after_attributable_failure_cannot_erase_learning(self):
        self.assert_role_failure_survives(("failed", "unknown"))

    def linked_role(self):
        """Create a role and its accepted acting edge for exclusion provenance tests."""
        role = "role:triage:gemini:own-exclusion"
        feedback.record_role_run(role, "triage", "triage:one-item", "gemini")
        feedback.record_run(
            "acting", "o/r#42", "implement", "codex", influenced_by_role_run_ids=[role]
        )
        return role

    def test_role_owned_exclusion_without_notes_survives_repeated_propagation(self):
        """A direct outcome owns its class even when earlier propagation notes remain."""
        role = self.linked_role()
        feedback.record_outcome("acting", adjudicated_verdict="PASS", durability="pending")
        self.assertTrue(self.row(role)[2].startswith("automatically influenced"))
        feedback.record_outcome(role, failure_class="transient_infra")
        for update in ({"durability": "broke_later"}, {"notes": "late acting evidence"}):
            feedback.record_outcome("acting", **update)
            self.assertEqual(self.row(role)[1], "transient_infra")
            self.assert_learning(task_type="role:triage", agent="gemini", observations=0)

    def test_role_owned_class_survives_an_inherited_exclusion_and_its_recovery(self):
        """An excluded acting run must not replace the role's independent exclusion."""
        role = self.linked_role()
        feedback.record_outcome(role, failure_class="transient_infra")
        feedback.record_outcome(
            "acting", durability="unjudgeable", failure_class=feedback.UNJUDGEABLE_MERGE
        )
        self.assertEqual(self.row(role)[1], "transient_infra")
        feedback.record_run(
            "repair", "o/r#43", "implement", "codex", influenced_by_role_run_ids=[role]
        )
        feedback.record_outcome("repair", adjudicated_verdict="FAIL", durability="broke_later")
        self.assertEqual(self.row(role)[1], "transient_infra")
        self.assert_learning(task_type="role:triage", agent="gemini", observations=0)

    def test_infra_marker_claims_an_inherited_class_as_role_owned(self):
        """The independent infra writer must persist ownership even for the same class."""
        role = self.linked_role()
        feedback.record_outcome("acting", durability="abandoned", failure_class="transient_infra")
        self.assertTrue(feedback.mark_transient_infra(role))
        self.assertFalse(feedback.mark_transient_infra(role))
        feedback.record_run(
            "repair", "o/r#43", "implement", "codex", influenced_by_role_run_ids=[role]
        )
        feedback.record_outcome("repair", adjudicated_verdict="FAIL", durability="broke_later")
        self.assertEqual(self.row(role)[1], "transient_infra")
        self.assert_learning(task_type="role:triage", agent="gemini", observations=0)

    def test_migration_preserves_legacy_exclusions_without_guessing_from_notes(self):
        """Legacy notes cannot prove inheritance; reopening preserves unknown ownership."""
        role = "role:triage:gemini:legacy"
        with sqlite3.connect(feedback.DB_PATH) as conn:
            conn.executescript(feedback.SCHEMA)
            conn.execute("ALTER TABLE outcomes ADD COLUMN failure_class TEXT")
            conn.execute(
                "INSERT INTO runs (run_id,role_name,task_type,agent,ts) VALUES (?,?,?,?,?)",
                (role, "triage", "role:triage", "gemini", NOW),
            )
            conn.execute(
                "INSERT INTO outcomes (run_id,failure_class,notes) VALUES (?,?,?)",
                (role, "transient_infra", "automatically influenced legacy-acting"),
            )
        for _ in range(2):
            with feedback._conn() as conn:
                self.assertEqual(
                    conn.execute(
                        "SELECT failure_class_origin FROM outcomes WHERE run_id=?", (role,)
                    ).fetchone(),
                    ("own",),
                )
        feedback.record_run(
            "acting", "o/r#42", "implement", "codex", influenced_by_role_run_ids=[role]
        )
        feedback.record_outcome("acting", adjudicated_verdict="FAIL", durability="broke_later")
        self.assertEqual(self.row(role)[1], "transient_infra")
        self.assert_learning(task_type="role:triage", agent="gemini", observations=0)


if __name__ == "__main__":
    unittest.main()
