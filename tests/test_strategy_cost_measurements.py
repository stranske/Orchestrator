"""A verified promotion still needs whole-run measured costs for every strategy attempt."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import exp_abcd
import feedback
import strategy_experiment
import synthesis_promotion


class StrategyCostMeasurementTests(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.object(feedback, "DB_PATH", root / "feedback.db"))
        self.enterContext(patch.object(exp_abcd, "EXP_DIR", root / "experiments"))
        self.enterContext(patch.dict("os.environ", {"ORCH_STATE_DIR": str(root / "state")}))
        self.exp_id = "cost-measurement-test"
        self.edir = exp_abcd.EXP_DIR / self.exp_id
        self.edir.mkdir(parents=True)
        spec = self.edir / "spec.md"
        spec.write_text("## Acceptance Criteria\nDeliver the requested behavior.")
        self.plan = strategy_experiment.build_strategy_plan(
            "o/r", str(spec), self.exp_id, strategy_experiment.subject_arms("cursor", "vibe")
        )
        (self.edir / "strategy.json").write_text(
            json.dumps(strategy_experiment.strategy_metadata(self.plan))
        )
        (self.edir / "meta.json").write_text(
            json.dumps({"exp_id": self.exp_id, "repo": "o/r", "arms": self.plan["arms"]})
        )
        (self.edir / "eval-maps.json").write_text(json.dumps({"vibe": {}}))
        self.run_ids = [run_id for arm in self.plan["arms"] for run_id in arm["attempt_run_ids"]]
        for arm in self.plan["arms"]:
            for run_id in arm["attempt_run_ids"]:
                feedback.record_cost(run_id, cost_usd=0.25, source="ccusage")
            feedback.record_evaluation_v2(
                experiment_id=self.exp_id,
                implementer_arm_id=arm["arm_id"],
                implementer_member_id=arm["final_artifact_id"],
                implementation_agent="cursor",
                evaluator_id="vibe",
                evaluator_agent="vibe",
                score=8 if arm["strategy"] == "pair" else 6,
            )
        state = synthesis_promotion.ensure_evaluated_state(self.edir)
        state["synthesis"] = {"commit": "fixture-commit", "run_ids": [f"{self.exp_id}:synth"]}
        state["verification"] = {"passed": True, "evidence_hash": "fixture-hash", "evidence": {}}
        for phase in ("synth_running", "synth_complete", "synth_verified"):
            state, _ = synthesis_promotion.transition(state, phase, reason="test")
        state, _ = synthesis_promotion.compile_candidate(state, self.edir)
        synthesis_promotion._atomic_json(synthesis_promotion.state_path(self.edir), state)

    def refresh(self):
        path = strategy_experiment.refresh_evaluation_result(self.exp_id)
        return json.loads(path.read_text())

    def test_partial_sources_block_acceptance_until_whole_run_cost_arrives(self):
        arm = self.plan["arms"][-1]
        run_id = arm["attempt_run_ids"][-1]
        for source in (None, "", "ledger", "langsmith"):
            with self.subTest(source=source):
                with feedback._conn() as conn:
                    conn.execute("UPDATE costs SET source=? WHERE run_id=?", (source, run_id))
                receipt = self.refresh()
                self.assertEqual(receipt["status"], "UNKNOWN")
                self.assertEqual(receipt["acceptance_status"], "UNKNOWN")
                self.assertIsNone(receipt["comparison"])
                self.assertTrue(receipt["promotion"]["verified_candidate"])
                cost = receipt["costs"][arm["arm_id"]]
                self.assertIsNone(cost["cost_usd"])
                self.assertEqual(cost["unmeasured_attempt_run_ids"], [run_id])
                self.assertEqual(cost["sources"][run_id], source)
                feedback.record_cost(run_id, cost_usd=0.25, source="ccusage")
                receipt = self.refresh()
                self.assertEqual(receipt["acceptance_status"], "completed")
                self.assertEqual(receipt["comparison"]["pair"]["cost_usd"], 1.5)
                self.assertEqual(receipt["cost_scale"], feedback.COST_SCALE)

    def test_invalid_cost_measurements_stay_unknown(self):
        for value in (None, True, -0.25, float("inf"), float("nan"), "0.25"):
            with self.subTest(cost_usd=value):
                rows = [
                    {"run_id": run_id, "cost_usd": 0.25, "source": "ccusage"}
                    for run_id in self.run_ids
                ]
                rows[-1]["cost_usd"] = value
                path = strategy_experiment.write_scored_result(
                    self.plan,
                    scores={arm["arm_id"]: 8 for arm in self.plan["arms"]},
                    cost_rows=rows,
                    result_path=self.edir / "invalid-cost.json",
                )
                receipt = json.loads(path.read_text())
                self.assertEqual(receipt["status"], "UNKNOWN")
                self.assertIsNone(receipt["costs"][self.plan["arms"][-1]["arm_id"]]["cost_usd"])

    def test_measured_zero_cost_is_preserved(self):
        for run_id in self.run_ids:
            feedback.record_cost(run_id, cost_usd=0.0, source="ccusage")
        receipt = self.refresh()
        self.assertEqual(receipt["acceptance_status"], "completed")
        for row in receipt["comparison"].values():
            self.assertEqual(row["cost_usd"], 0)

    def test_missing_costs_name_all_nine_exact_attempts(self):
        with feedback._conn() as conn:
            conn.execute("DELETE FROM costs")
        receipt = self.refresh()
        self.assertEqual(receipt["acceptance_status"], "UNKNOWN")
        gaps = []
        for row in receipt["costs"].values():
            self.assertEqual(row["missing_attempt_run_ids"], row["unmeasured_attempt_run_ids"])
            self.assertIsNone(row["cost_usd"])
            gaps.extend(row["unmeasured_attempt_run_ids"])
        self.assertCountEqual(gaps, self.run_ids)
        self.assertEqual(len(gaps), 9)

    def test_earlier_duplicate_measurement_cannot_hide_partial_replacement(self):
        rows = [
            {"run_id": run_id, "cost_usd": 0.25, "source": "ccusage"}
            for run_id in self.run_ids
        ]
        rows.append({"run_id": self.run_ids[-1], "cost_usd": 0.01, "source": "langsmith"})
        costs = strategy_experiment.strategy_arm_costs(self.plan, rows)
        cost = costs[self.plan["arms"][-1]["arm_id"]]
        self.assertIsNone(cost["cost_usd"])
        self.assertEqual(cost["unmeasured_attempt_run_ids"], [self.run_ids[-1]])


if __name__ == "__main__":
    unittest.main()
