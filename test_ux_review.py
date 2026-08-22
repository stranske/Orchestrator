#!/usr/bin/env python3
"""Offline unit tests for ux_review pure helpers (no network, no live agents)."""
from __future__ import annotations

import json
import sqlite3
import unittest

import research_subjects
import ux_review as ur


SAMPLE_BUNDLE = {
    "app": "stranske/Trend_Model_Project",
    "review_id": "stranske/Trend_Model_Project:uxreview:2026-06-22",
    "url": "http://localhost:8600/",
    "wired": {"ok": True, "findings": []},
    "screens": [{"name": "Home", "a11y": "button Run Demo", "notes": ""}],
    "scenarios": [{
        "name": "Run the demo",
        "steps": [
            {"action": "open Home", "observed": "Home screen visible"},
            {"action": "click Run Demo", "observed": "results table appears"},
        ],
        "goal": "see results",
    }],
}


class TestPrompts(unittest.TestCase):
    def test_rubric_prompt_contains_dimensions_schema_and_rules(self):
        p = ur.build_rubric_prompt(SAMPLE_BUNDLE)
        for dim in ur.DIMENSIONS:
            self.assertIn(dim, p)
        self.assertIn("workflow_productivity", p)
        self.assertIn('"scores"', p)
        self.assertIn("HARD RULES", p)
        self.assertIn("feels clunky", p)
        self.assertIn("severity scale 0=none", p)
        self.assertIn("STRICT JSON only", p)
        self.assertIn("confidence", p)
        self.assertIn("failure_mode", p)
        self.assertIn("Never infer behavior not present in observed", p)
        self.assertIn(SAMPLE_BUNDLE["app"], p)

    def test_adversarial_prompt_framing(self):
        p = ur.build_adversarial_prompt(SAMPLE_BUNDLE)
        self.assertIn("hostile, novice first-time user who WANTS to fail", p)
        self.assertIn("stuck_probability", p)
        self.assertIn("worst_case", p)
        self.assertIn("adversarial", p)


class TestMedian(unittest.TestCase):
    def test_median_odd_even_empty(self):
        self.assertEqual(ur.median([1, 2, 3, 4, 100]), 3.0)
        self.assertEqual(ur.median([1, 2, 3, 4]), 2.5)
        self.assertEqual(ur.median([]), 0.0)


class TestConsensusFlag(unittest.TestCase):
    def test_spread_threshold(self):
        self.assertFalse(ur.consensus_flag([5, 6, 7]))
        self.assertTrue(ur.consensus_flag([5, 6, 10]))


class TestDedupeFindings(unittest.TestCase):
    def test_max_severity_and_confidence_wins(self):
        f1 = {"screen": "Home", "element": "Run", "failure_mode": "confusion",
              "severity": 2, "confidence": 0.6, "click_path": ["a"]}
        f2 = {"screen": "Home", "element": "Run", "failure_mode": "confusion",
              "severity": 4, "confidence": 0.8, "click_path": ["a"]}
        f3 = {"screen": "Home", "element": "Help", "failure_mode": "missing_help",
              "severity": 3, "confidence": 0.5, "click_path": ["b"]}
        out = ur.dedupe_findings([f1, f2, f3])
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["severity"], 4)
        self.assertEqual(out[0]["confidence"], 0.8)

    def test_sorted_by_severity_desc(self):
        low = {"screen": "A", "element": "x", "failure_mode": "confusion",
               "severity": 1, "click_path": ["a"]}
        high = {"screen": "B", "element": "y", "failure_mode": "confusion",
                "severity": 3, "click_path": ["b"]}
        out = ur.dedupe_findings([low, high])
        self.assertEqual(out[0]["severity"], 3)


class TestAggregateAcceptedFindings(unittest.TestCase):
    def test_majority_accepts(self):
        # majority_needed for n=4 is (4//2)+1 = 3. 2 of 4 is NOT a majority -> reject.
        ev_findings = {
            "claude": [{"screen": "H", "element": "E", "failure_mode": "confusion", "severity": 2,
                        "click_path": ["x"], "confidence": 0.5}],
            "codex": [{"screen": "H", "element": "E", "failure_mode": "confusion", "severity": 3,
                       "click_path": ["x"], "confidence": 0.6}],
        }
        accepted, non = ur.aggregate_accepted_findings(ev_findings, [], n_evaluators=4)
        self.assertEqual(len(accepted), 0)  # 2 of 4 — not a majority
        # add a 3rd evaluator -> 3 of 4 IS a majority -> accept, keeping max severity in the group.
        ev_findings["cursor"] = [{"screen": "H", "element": "E", "failure_mode": "confusion",
                                  "severity": 2, "click_path": ["x"], "confidence": 0.4}]
        accepted, _ = ur.aggregate_accepted_findings(ev_findings, [], n_evaluators=4)
        self.assertEqual(len(accepted), 1)
        self.assertEqual(accepted[0]["severity"], 3)

    def test_adversarial_stuck_probability_accepts(self):
        accepted, _ = ur.aggregate_accepted_findings(
            {"claude": []},
            [{"screen": "H", "element": "E", "failure_mode": "confusion", "severity": 3,
              "click_path": ["x"], "stuck_probability": 0.6, "dimension": "adversarial"}],
            n_evaluators=4,
        )
        self.assertEqual(len(accepted), 1)

    def test_missing_click_path_becomes_non_finding(self):
        accepted, non = ur.aggregate_accepted_findings(
            {"claude": [{"screen": "H", "element": "E", "failure_mode": "confusion", "severity": 2}]},
            [],
            n_evaluators=4,
        )
        self.assertEqual(accepted, [])
        self.assertEqual(len(non), 1)
        self.assertEqual(non[0]["reject_reason"], "missing_click_path")


class TestOverallMedian(unittest.TestCase):
    def test_blocker_caps_overall(self):
        self.assertEqual(ur.compute_overall_median([8.0, 8.5, 9.0], []), 8.5)
        self.assertEqual(ur.compute_overall_median([8.0, 8.5, 9.0], [{"severity": 4}]), 3.0)


class TestGateDecision(unittest.TestCase):
    def test_truth_table(self):
        g1_ok = {"ok": True}
        g1_bad = {"ok": False}
        rep_pass = {"overall_median": 8.0, "findings": [{"severity": 2}]}
        rep_low = {"overall_median": 6.0, "findings": []}
        rep_block = {"overall_median": 8.0, "findings": [{"severity": 4}]}

        self.assertTrue(ur.gate_decision(g1_ok, rep_pass)["done"])
        self.assertFalse(ur.gate_decision(g1_bad, rep_pass)["done"])
        self.assertFalse(ur.gate_decision(g1_ok, rep_low)["done"])
        self.assertFalse(ur.gate_decision(g1_ok, rep_block)["done"])
        self.assertIn("blockers_present", ur.gate_decision(g1_ok, rep_block)["reasons"])


class TestAggregatePanel(unittest.TestCase):
    def test_dimension_medians_and_evidence_gaps(self):
        agg = ur.aggregate_panel({
            "claude": {"scores": {"wired": 9, "usability": 7, "help_clarity": 8,
                                  "workflow_productivity": 8},
                       "overall": 8, "findings": [], "evidence_gaps": ["missing tooltip"]},
            "codex": {"scores": {"wired": 9, "usability": 4, "help_clarity": 8,
                                 "workflow_productivity": 8},
                      "overall": 7, "findings": [], "evidence_gaps": []},
            "cursor": {"scores": {"wired": 9, "usability": 7, "help_clarity": 8,
                                  "workflow_productivity": 8},
                       "overall": 8, "findings": [], "evidence_gaps": []},
            "gemini": {"scores": {"wired": 9, "usability": 7, "help_clarity": 8,
                                  "workflow_productivity": 8},
                       "overall": 8, "findings": [], "evidence_gaps": []},
        }, {"worst_case": "Run Demo", "findings": []}, n_evaluators=4)
        self.assertEqual(agg["dimension_medians"]["usability"], 7.0)
        self.assertTrue(agg["consensus_flags"]["usability"])
        self.assertIn("missing tooltip", agg["evidence_gaps"])



class TestPanelSubjectRegistration(unittest.TestCase):
    """The panel's evidence is only minable if the panel registers a research subject."""

    def _conn(self):
        conn = sqlite3.connect(":memory:")
        research_subjects.ensure_schema(conn)
        return conn

    def test_panel_registers_one_subject_with_the_real_rubric_spec(self):
        conn = self._conn()
        evaluators = ["claude", "codex", "cursor", "vibe"]
        identity = ur.register_panel_subject(SAMPLE_BUNDLE, evaluators, conn=conn)
        self.assertIsNotNone(identity, "panel must register a subject")
        # The subject row exists and is linked to the panel's experiment id -- the join key
        # the completion-event exporter resolves identity through.
        row = conn.execute(
            "SELECT s.canonical_target, s.task_type, s.spec_hash, s.arms_json "
            "FROM research_subject_experiments x "
            "JOIN research_subjects s ON s.subject_id = x.subject_id WHERE x.exp_id=?",
            (SAMPLE_BUNDLE["review_id"],),
        ).fetchone()
        self.assertIsNotNone(row, "exp_id must join to a research_subjects row")
        self.assertEqual(row[0], SAMPLE_BUNDLE["app"].lower())
        self.assertEqual(row[1], "ux_review")
        # The spec hash is the hash of the REAL rubric prompt, not an invented value.
        expected = research_subjects.subject_identity(
            SAMPLE_BUNDLE["app"], "ux_review", ur.build_rubric_prompt(SAMPLE_BUNDLE),
            ur.resolve_panel_base_sha(SAMPLE_BUNDLE), evaluators,
        )
        self.assertEqual(row[2], expected["spec_hash"])
        self.assertEqual(identity["subject_id"], expected["subject_id"])
        # Every arm is retained: collapsing the panel to one arm would forge independence.
        self.assertEqual(sorted(json.loads(row[3])), sorted(evaluators))

    def test_distinct_rubrics_are_distinct_subjects(self):
        """Two panels on the same app with different rubrics are NOT one subject."""
        conn = self._conn()
        evaluators = ["claude", "codex"]
        a = ur.register_panel_subject(SAMPLE_BUNDLE, evaluators, spec="rubric A", conn=conn)
        other = dict(SAMPLE_BUNDLE, review_id=SAMPLE_BUNDLE["review_id"] + "b")
        b = ur.register_panel_subject(other, evaluators, spec="rubric B", conn=conn)
        self.assertNotEqual(a["subject_id"], b["subject_id"])
        n = conn.execute("SELECT COUNT(*) FROM research_subjects").fetchone()[0]
        self.assertEqual(n, 2)

    def test_registration_failure_is_reported_not_swallowed(self):
        """A swallowed failure is indistinguishable from 'never meant to be mined'."""
        broken = {"review_id": "x:uxreview:1"}  # no "app" -> KeyError inside
        self.assertIsNone(ur.register_panel_subject(broken, ["claude"], spec="s"))




class TestPanelArmOutcome(unittest.TestCase):
    """Route weights learn from outcomes, so each arm needs an un-gameable label."""

    def test_unparseable_or_unscored_arm_fails(self):
        self.assertEqual(ur.panel_arm_outcome(None, [], set(), True)[0], "FAIL")
        self.assertEqual(ur.panel_arm_outcome({}, [], set(), True)[0], "FAIL")
        # produced prose but no rubric scores -> did not do the task
        v, d, _ = ur.panel_arm_outcome({"overall": 7}, [], set(), True)
        self.assertEqual((v, d), ("FAIL", "reverted"))

    def test_corroborated_finding_is_durable(self):
        f = {"screen": "Home", "element": "Run", "failure_mode": "dead"}
        v, d, note = ur.panel_arm_outcome(
            {"scores": {"wired": 5}}, [f], {ur.finding_key(f)}, True
        )
        self.assertEqual((v, d), ("PASS", "durable"))
        self.assertIn("corroborated", note)

    def test_uncorroborated_findings_are_not_durable(self):
        mine = {"screen": "A", "element": "b", "failure_mode": "c"}
        other = {"screen": "X", "element": "y", "failure_mode": "z"}
        v, d, _ = ur.panel_arm_outcome(
            {"scores": {"wired": 5}}, [mine], {ur.finding_key(other)}, True
        )
        self.assertEqual((v, d), ("PASS", "reverted"))

    def test_clean_app_does_not_penalise_a_silent_arm(self):
        """Marking arms down for finding nothing on a sound app would train them to invent findings."""
        v, d, note = ur.panel_arm_outcome({"scores": {"wired": 9}}, [], set(), False)
        self.assertEqual((v, d), ("PASS", "held"))
        self.assertIn("clean-app", note)
        self.assertIn(d, ("durable", "held", "survived"))  # counts as durable to the learner

    def test_durability_values_are_learner_recognised(self):
        """A label the learner does not recognise is the same as no label at all."""
        from pattern_miner import DURABLE_STATUSES, TERMINAL_FAILURE_DURABILITY
        known = set(DURABLE_STATUSES) | set(TERMINAL_FAILURE_DURABILITY)
        for args in [(None, [], set(), True),
                     ({"scores": {"wired": 1}}, [], set(), False),
                     ({"scores": {"wired": 1}}, [], {("a", "b", "c")}, True)]:
            self.assertIn(ur.panel_arm_outcome(*args)[1], known)




class TestPanelBaseSha(unittest.TestCase):
    def test_supplied_base_sha_wins(self):
        self.assertEqual(
            ur.resolve_panel_base_sha({"app": "o/r", "base_sha": "deadbeef"}), "deadbeef")

    def test_non_repo_app_gets_no_placeholder(self):
        """An invented base commit makes two app states look like one subject."""
        self.assertIsNone(ur.resolve_panel_base_sha({"app": "local-Reader"}))
        self.assertIsNone(ur.resolve_panel_base_sha({}))

    def test_panel_subject_uses_the_resolved_base_sha(self):
        conn = sqlite3.connect(":memory:")
        research_subjects.ensure_schema(conn)
        bundle = dict(SAMPLE_BUNDLE, base_sha="abc123def")
        identity = ur.register_panel_subject(bundle, ["claude", "codex"], conn=conn)
        self.assertEqual(identity["base_sha"], "abc123def")
        row = conn.execute("SELECT base_sha FROM research_subjects").fetchone()
        self.assertEqual(row[0], "abc123def")



if __name__ == "__main__":
    raise SystemExit(unittest.main())
