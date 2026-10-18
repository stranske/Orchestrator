"""Regression evidence for real-clock reachability and stale-history continuity."""

import json
import math
from pathlib import Path

import pytest

import pacing
import pacing_usage

SOURCE = "fixture-percent"
START = 1792269641.0
END = START + pacing.PERIOD_SECONDS


def assess(samples, **kwargs):
    last = samples[-1]
    args = dict(
        now=last["ts"],
        source_ts=last["ts"],
        used_fraction=last["used_fraction"],
        period_start=START,
        period_end=END,
        resets=[],
        samples=samples,
        source=last["source"],
    )
    args.update(kwargs)
    return pacing.evaluate("codex", **args)


@pytest.mark.parametrize("in_window_span", [5.75, 5.99])
def test_jittered_window_reaches_shed_and_prints_recovery(in_window_span):
    now = START + pacing.DAY_SECONDS
    times = [now - 6.1 * 3600, now - in_window_span * 3600, now - 3600, now]
    rows = [
        dict(ts=t, used_fraction=0.6 * (t - START) / pacing.DAY_SECONDS, source=SOURCE)
        for t in times
    ]
    shed = assess(rows)
    assert shed["span_h"] == 6 and shed["confidence"] == 1
    assert shed["burn_rate_per_day"] == pytest.approx(0.6)
    assert shed["state"] == "shed"
    assert "ũ=4.20" in shed["reason"] and "clears in" in shed["reason"]
    flat = [dict(r, used_fraction=0.6) for r in rows]
    recovered = assess(flat, previous_state=shed["state"])
    assert recovered["state"] == "ok"
    assert "pool codex: ũ=0.60 ≤ 1−r_eff → ok" in recovered["fyi"]
    print(shed["reason"])
    print(*recovered["fyi"], sep="\n")


def test_anchor_interval_mass_is_clipped_and_only_latest_anchor_counts():
    now = START + pacing.DAY_SECONDS
    rows = [
        dict(ts=now + h * 3600, used_fraction=u, source=SOURCE)
        for h, u in [(-9, 0), (-7, 0.1), (-5, 0.12), (0, 0.22)]
    ]
    rate, span = pacing.burn_estimate(rows, now, START, source=SOURCE)
    w1 = math.exp(-5 / 6) - math.exp(-1)
    w2 = 1 - math.exp(-5 / 6)
    assert span == 6
    assert rate == pytest.approx((0.24 * w1 + 0.48 * w2) / (w1 + w2))
    # An old anchor alone cannot establish a three-observation estimate.
    assert pacing.burn_estimate([rows[0], rows[-1]], now, START, source=SOURCE)[0] is None
    sparse = assess([rows[0], rows[-1]], previous_state="warn")
    assert sparse["state"] == "ok" and sparse["projected_fraction"] is None
    assert sparse["fyi"] == []  # A full span still needs a qualified projection.


def test_reset_after_anchor_restarts_confidence():
    now = START + pacing.DAY_SECONDS
    rows = [
        dict(ts=now + h * 3600, used_fraction=u, source=SOURCE)
        for h, u in [(-7, 0.8), (-5.9, 0.01), (-3, 0.02), (0, 0.03)]
    ]
    row = assess(rows)
    assert row["span_h"] == pytest.approx(5.9)
    assert row["confidence"] < 1 and row["state"] == "ok"


def test_real_111_tick_replay_sheds_and_prints_drained_recovery():
    data = json.loads((Path(__file__).parent / "fixtures/pacing-real-111.json").read_text())
    replay = {}
    for pool, samples in data.items():
        assert len(samples) == 111
        assert all(set(r) == {"ts", "used_fraction", "source"} for r in samples)
        states = []
        prior = None
        for i, sample in enumerate(samples):
            if pool == "codex":
                start = START if sample["ts"] >= START else 1792248898.0
            else:
                # Observed weekly rollover; no reset credits or account data.
                start = 1792299540.0 if sample["ts"] >= 1792299540.0 else 1791694740.0
            row = pacing.evaluate(
                pool,
                now=sample["ts"],
                source_ts=sample["ts"],
                used_fraction=sample["used_fraction"],
                period_start=start,
                period_end=start + pacing.PERIOD_SECONDS,
                resets=[],
                samples=samples[: i + 1],
                source=sample["source"],
                previous_state=prior["state"] if prior else None,
                previous_weight=prior["routing_weight"] if prior else None,
            )
            states.append(row)
            prior = row
        assert any(r["confidence"] == 1 for r in states), pool
        replay[pool] = states
        print(
            f"real replay {pool}: ticks={len(states)} full-confidence="
            f"{sum(r['confidence'] == 1 for r in states)} shed="
            f"{sum(r['state'] == 'shed' for r in states)}"
        )
    shed = replay["codex"][-1]
    assert shed["state"] == "shed" and shed["used_fraction"] < 1
    assert shed["effective_projected_fraction"] > 1
    print(shed["reason"])
    # Counterfactual no-dispatch recovery, explicitly separate from recorded data.
    # Evaluate EVERY jittered tick: the real path goes shed -> warn -> ok. A test
    # that jumps straight to the final flat window misses the drained-print gap.
    last = data["codex"][-1]
    history = list(data["codex"])
    prior = shed
    drained = []
    transitions = []
    for i in range(1, 38):
        history.append(dict(last, ts=last["ts"] + i * 607.1))
        recovered = assess(
            history, previous_state=prior["state"], previous_weight=prior["routing_weight"]
        )
        if recovered["state"] != prior["state"]:
            transitions.append((prior["state"], recovered["state"]))
            print(recovered["reason"])
        drained.extend(line for line in recovered["fyi"] if "≤ 1−r_eff → ok" in line)
        prior = recovered
    assert transitions == [("shed", "warn"), ("warn", "ok")]
    assert recovered["confidence"] == 1 and recovered["state"] == "ok"
    assert drained == ["pool codex: ũ=0.78 ≤ 1−r_eff → ok"]
    print(*drained, sep="\n")


def test_weight_literal_endpoints_and_monotonicity():
    for reserve in (0, 0.075, 0.15):
        target = 1 - reserve
        projections = [-0.1, 0, target / 4, target / 2, target, 1, 2]
        weights = [pacing.spec_routing_weight(u, reserve) for u in projections]
        assert weights == pytest.approx([1, 1, 0.75, 0.5, 0, 0, 0])
        assert weights == sorted(weights, reverse=True)
    # Pure comparison function only: routing retains its merged rate-ratio semantics.
    assert pacing.routing_weight(0.1, 0.2) == 0.5


@pytest.mark.parametrize("gap", ["stale", "missing", "no_period"])
def test_low_weight_survives_repeated_gaps_until_real_boundary(tmp_path, monkeypatch, gap):
    monkeypatch.setattr(pacing_usage, "RESETS_FILE", tmp_path / "resets.json")
    monkeypatch.setattr(pacing_usage, "CALIBRATION_FILE", tmp_path / "calibration.json")
    monkeypatch.setattr(pacing_usage, "POLL_DIR", tmp_path / "polls")
    pacing_usage.RESETS_FILE.write_text(json.dumps(dict(codex=[], claude=[])))
    now = START + pacing.DAY_SECONDS
    prior = dict(period_start=START, period_end=END, ts=now - 3600, routing_weight=0.07)
    previous = dict(agents={p: dict(pacing=prior) for p in ("codex", "claude")})
    for step in range(4):
        tick = now + step * pacing.TICK_SECONDS
        fresh = step == 3
        calibration = (
            {}
            if gap == "no_period" and not fresh
            else dict(codex_period_start=START, claude_period_end=END)
        )
        pacing_usage.CALIBRATION_FILE.write_text(json.dumps(calibration))
        (tmp_path / "capacity.json").write_text(json.dumps(previous))
        datum = dict(
            used_fraction=None if gap == "missing" and not fresh else 0.01,
            ts=now - 3600 if gap == "stale" and not fresh else tick,
            source=SOURCE,
        )
        agents = {p: dict(state="ok", reason="native") for p in ("codex", "claude")}
        pacing_usage.integrate(
            agents, now=tick, handoff=tmp_path, usage_data={p: dict(datum) for p in agents}
        )
        for agent in agents.values():
            if fresh:
                assert agent["pacing"]["routing_weight"] <= 0.07 + pacing.MAX_WEIGHT_CHANGE
            else:
                assert agent["pacing"]["routing_weight"] is None
                assert agent["pacing_diagnostics"]["previous_routing_weight"] == 0.07
        previous = dict(agents=agents)
    # A confirmed period change expires the carry, rather than importing old throttling.
    pacing_usage.CALIBRATION_FILE.write_text(json.dumps(dict(codex_period_start=tick)))
    (tmp_path / "capacity.json").write_text(json.dumps(previous))
    agents = {
        "codex": dict(state="ok", reason="native"),
        "claude": dict(state="ok", reason="native"),
    }
    pacing_usage.integrate(
        agents, now=tick, handoff=tmp_path, usage_data={p: dict(datum) for p in agents}
    )
    assert agents["codex"]["pacing_diagnostics"]["previous_routing_weight"] == 1
