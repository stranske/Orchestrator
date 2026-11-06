"""Focused mode-selection regression for coverage round #1182."""

from __future__ import annotations

import pytest

import exploration_collection as ec


@pytest.fixture(autouse=True)
def isolated_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ORCH_LOCAL_RUNTIME", str(tmp_path / "runtime"))


def test_choose_mode_aliases_and_deficits():
    plan = {
        "direct_mode_deficits": [
            {"mode": "epsilon-greedy", "remaining_outcome_runs": 1},
            {"mode": "thompson-hybrid", "remaining_outcome_runs": 3},
        ]
    }
    assert ec.choose_mode(plan, "thompson") == "thompson-hybrid"
    assert ec.choose_mode(plan, "epsilon") == "epsilon-greedy"
    assert ec.choose_mode(plan, "auto") == "thompson-hybrid"
    tied = {
        "direct_mode_deficits": [
            {"mode": "epsilon-greedy", "remaining_outcome_runs": 2},
            {"mode": "thompson-hybrid", "remaining_outcome_runs": 2},
        ]
    }
    week = 7 * 86400
    assert ec.choose_mode(tied, "auto", now=0) == "epsilon-greedy"
    assert ec.choose_mode(tied, "auto", now=week) == "thompson-hybrid"
