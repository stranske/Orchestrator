"""Pacing integration contracts across provider-source changes."""

import json

import pytest

import pacing
import pacing_usage


@pytest.mark.parametrize("pool", ["codex", "claude"])
def test_source_change_keeps_same_period_prior_weight(tmp_path, monkeypatch, pool):
    start = pacing.timestamp("2026-10-11T05:00:00Z")
    end = start + pacing.PERIOD_SECONDS
    now = start + 5 * 3600
    monkeypatch.setattr(pacing_usage, "POLL_DIR", tmp_path / "polls")
    monkeypatch.setattr(pacing_usage, "RESETS_FILE", tmp_path / "resets.json")
    monkeypatch.setattr(pacing_usage, "CALIBRATION_FILE", tmp_path / "calibration.json")
    pacing_usage.RESETS_FILE.write_text(json.dumps(dict(codex=[], claude=[])))
    pacing_usage.CALIBRATION_FILE.write_text(
        json.dumps(dict(codex_period_start=start, claude_period_end=end))
    )
    prior = dict(
        period_start=start,
        period_end=end,
        source="old-source",
        ts=now - 900,
        routing_weight=0,
        state="warn",
    )
    (tmp_path / "capacity.json").write_text(json.dumps(dict(agents={pool: dict(pacing=prior)})))
    data = {p: dict(used_fraction=0, ts=now, source="new-source") for p in ("codex", "claude")}
    agents = {p: dict(state="ok", reason="native") for p in data}
    pacing_usage.integrate(agents, now=now, handoff=tmp_path, usage_data=data)
    assert agents[pool]["pacing_diagnostics"]["previous_routing_weight"] == 0
    assert agents[pool]["pacing"]["routing_weight"] == 0.25
    text = pacing_usage.report(tmp_path, now=now, current=dict(agents=agents))
    for field in (
        "C=",
        "n_usable=0",
        "s=",
        "ḃ_short=unmeasured",
        "ḃ_period=0.0",
        "ḃ_eff=0.0",
        "w_p_prev→w_p=0.0→0.25",
    ):
        assert field in text
