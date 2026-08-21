#!/usr/bin/env python3
"""Offline unit tests for ux_review pure helpers (no network, no live agents)."""
from __future__ import annotations

import unittest

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


if __name__ == "__main__":
    raise SystemExit(unittest.main())
