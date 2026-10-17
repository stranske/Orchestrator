"""Review seams and the suite guard keep test reports out of persistent goal history."""

import os
from pathlib import Path

import pytest

import feature_goals
import switch_review

NOW = 1_800_000_000


@pytest.fixture
def quiet_review(monkeypatch):
    import adjudicator_live
    import adjudicator_retro
    import runtime_ac_gate

    for name in (
        "_capability_heartbeat",
        "stale_runners",
        "adversarial_shape_population",
        "mirror_drift",
        "fleet_gates",
        "firing_regressions",
        "_exploration_gate",
        "gate_expiry",
        "capacity_shed",
        "worktree_git_refusals",
    ):
        monkeypatch.setattr(switch_review, name, lambda *a, **kw: {})
    monkeypatch.setattr(
        switch_review, "switch_states", lambda **kw: {"held_off": [], "on_but_idle": []}
    )
    monkeypatch.setattr(runtime_ac_gate, "shadow_summary", lambda **kw: {})
    monkeypatch.setattr(runtime_ac_gate, "post_merge_summary", lambda: {})
    monkeypatch.setattr(adjudicator_live, "weekly_line", lambda: "")
    monkeypatch.setattr(adjudicator_retro, "weekly_line", lambda: "")


@pytest.mark.feature_goals_real
@pytest.mark.parametrize("injected", ["sources", "path"])
def test_injected_review_writes_no_history(tmp_path, monkeypatch, quiet_review, injected):
    def forbidden(*args, **kw):
        pytest.fail("injected review called the real feature goal report")

    monkeypatch.setattr(feature_goals, "report", forbidden)
    switch_review.review(
        now=NOW,
        env={"ORCH_VALUE_CHAIN_MONITOR": "0"},
        **{injected: {} if injected == "sources" else tmp_path / "capabilities.json"},
    )
    assert not feature_goals.history_path().exists()


@pytest.mark.feature_goals_real
def test_explicit_feature_goals_fn_overrides_injected_source_default(quiet_review):
    calls = []
    sentinel = {"fixture": True}
    rep = switch_review.review(
        now=NOW,
        sources={},
        env={"ORCH_VALUE_CHAIN_MONITOR": "0"},
        feature_goals_fn=lambda stamp: calls.append(stamp) or sentinel,
    )
    assert calls == [NOW]
    assert rep["feature_goals"] is sentinel


def test_suite_feature_goal_report_writes_no_history(tmp_path, monkeypatch, quiet_review):
    # In the RED demo the real report can persist, but only empty goals at this private path.
    # This exercises review's uninjected path without live measurements or GitHub calls.
    path = tmp_path / "review-history.jsonl"
    monkeypatch.setattr(feature_goals, "history_path", lambda: path)
    persist = feature_goals._persist_report

    def empty_goals(*args, **kwargs):
        return persist(*args, **{**kwargs, "goals": []})

    monkeypatch.setattr(feature_goals, "_persist_report", empty_goals)
    switch_review.review(now=NOW, env={"ORCH_VALUE_CHAIN_MONITOR": "0"})
    assert not path.exists(), "suite isolation removed: review wrote history"
    assert Path(os.environ["ORCH_STATE_DIR"]) == tmp_path
