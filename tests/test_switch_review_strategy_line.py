"""The switch-review line for a completed strategy experiment never prints a cost nobody measured.

``strategy_experiment.write_scored_result`` completes the verdict on scores alone, so a completed
receipt may carry ``cost_status: unmeasured`` with every ``cost_usd`` None. The consumer used to sum
those None values as zero and print ``total_cost=$0.0000``: a measured zero for an unmeasured cost,
which is the latched-gate question 4 failure moved from the gate into its report.
"""

from __future__ import annotations

import json

import strategy_experiment
import switch_review

MINIMAL_REPORT = {
    "review_days": 7,
    "raise_count": 0,
    "held_off": [],
    "on_but_idle": [],
    "unconditioned": [],
    "mirror_drift": {"status": "ok"},
}


def _completed_result(cost_usd: dict[str, float | None]) -> dict:
    costs = {arm: {"cost_usd": value, "latency_s": 10.0} for arm, value in cost_usd.items()}
    unmeasured = sorted(arm for arm, value in cost_usd.items() if value is None)
    return {
        "exp_id": "strategy-line-test",
        "status": "completed",
        "acceptance_status": "completed",
        "cost_status": "unmeasured" if unmeasured else "measured",
        "unmeasured_arm_ids": unmeasured,
        "costs": costs,
        "comparison": {
            "pair": {"score_mean": 6.2222, "cost_status": "unmeasured"},
            "single": {"score_mean": 4.2222, "cost_status": "unmeasured"},
        },
    }


def test_unmeasured_cost_is_named_and_never_summed_to_zero():
    result = _completed_result({"arm-01": None, "arm-02": None})
    line = switch_review.strategy_completed_line(result, result["costs"])
    assert "completed strategy-line-test" in line
    assert "cost unmeasured (2 of 2 arm(s)" in line
    assert "total_cost" not in line
    assert "$0.0000" not in line
    assert "scores pair 6.22/single 4.22" in line


def test_measured_cost_still_prints_the_total():
    result = _completed_result({"arm-01": 0.75, "arm-02": 0.5})
    line = switch_review.strategy_completed_line(result, result["costs"])
    assert "total_cost=$1.2500" in line
    assert "unmeasured" not in line


def test_report_carries_the_unmeasured_line_once(tmp_path, monkeypatch):
    receipt = tmp_path / "strategy-receipt.json"
    receipt.write_text(json.dumps(_completed_result({"arm-01": None, "arm-02": 0.5})))
    monkeypatch.setattr(strategy_experiment, "strategy_result_path", lambda: receipt)
    rendered = switch_review.format_report(dict(MINIMAL_REPORT))
    strategy_lines = [line for line in rendered.splitlines() if "strategy experiment:" in line]
    assert len(strategy_lines) == 1, rendered
    assert "cost unmeasured (1 of 2 arm(s)" in strategy_lines[0]
    assert "$0.0000" not in rendered
    assert "UNKNOWN" not in strategy_lines[0]
