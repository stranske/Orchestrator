"""Offline pacing and consumption contracts with injected clocks/evidence."""

import copy
import json
import random

import pytest

import capacity
import pacing
import pacing_usage
import route_weights_export
import router

START = pacing.CLAUDE_RESET_ANCHOR - pacing.PERIOD_SECONDS
SOURCE = "fixture-percent"


def resets(n=0):
    return [{"id": f"bank-{i}", "expires": "2026-11-16"} for i in range(n)]


def evaluate(day=1, u=0.6, n=0, samples=True, **kwargs):
    now = START + day * pacing.DAY_SECONDS
    data = (
        [
            dict(ts=START, used_fraction=0, source=SOURCE),
            dict(ts=now, used_fraction=u, source=SOURCE),
        ]
        if samples
        else []
    )
    if isinstance(samples, list):
        data = samples
    args = dict(
        now=now,
        used_fraction=u,
        period_start=START,
        period_end=START + pacing.PERIOD_SECONDS,
        resets=resets(n),
        samples=data,
        source=SOURCE,
        source_ts=now,
    )
    args.update(kwargs)
    return pacing.evaluate("codex", **args)


def test_early_heavy_spend_sheds_without_resets():
    row = evaluate()
    assert row["projected_fraction"] == pytest.approx(4.2)
    assert row["state"] == "shed"
    assert "ũ=4.20" in row["reason"] and "clears in" in row["reason"]


def test_banked_resets_relieve_reserve_RESET_BOOST():
    row = evaluate(n=2)
    assert (
        row["effective_reserve"] == 0
    ), "RESET_BOOST must consume resets_available in reserve math"
    assert row["state"] == "warn", "RESET_BOOST banked resets must keep projected exhaustion warn"
    assert "bank-0" in row["fyi"][0]
    # Also exercise an actual state difference induced by the reserve formula.
    assert evaluate(day=6, u=0.8)["state"] == "warn"
    assert evaluate(day=6, u=0.8, n=2)["state"] == "ok", "RESET_BOOST changes pacing state"


def test_late_heavy_spend_is_ok():
    row = evaluate(day=6)
    assert row["projected_fraction"] == pytest.approx(0.7)
    assert row["state"] == "ok"
    assert 1 - row["effective_reserve"] == 0.85


@pytest.mark.parametrize("u,state", [(0.2, "ok"), (0.3, "warn"), (0, "ok")])
def test_undefined_burn_schedule(u, state):
    row = evaluate(u=u, samples=False)
    assert row["burn_rate_per_day"] is None
    assert row["projected_fraction"] is None
    assert row["state"] == state


@pytest.mark.parametrize("n,expected", [(0, 0.15), (1, 0.075), (2, 0)])
def test_literal_reserve_formula(n, expected):
    assert pacing.effective_reserve(n) == pytest.approx(
        expected
    ), "RESET_BOOST literal revised formula"


def test_expired_and_consumed_resets_drop_out():
    entries = resets(2) + [
        {"expires": "2026-10-01"},
        {"expires": "2026-11-16", "consumed_at": START},
    ]
    assert len(pacing.active_resets(entries, START)) == 2
    assert len(pacing.active_resets(entries, pacing.timestamp("2026-11-17"))) == 0


def test_codex_reset_moves_period_claude_stays_fixed():
    now = START + 2 * pacing.DAY_SECONDS
    assert pacing.period("codex", now, start=START, reset_at=now) == (
        now,
        now + pacing.PERIOD_SECONDS,
    )
    assert pacing.period("claude", now, start=START, reset_at=now) == (
        START,
        pacing.CLAUDE_RESET_ANCHOR,
    )
    assert pacing.period("codex", now) is None


def test_weight_literal_endpoints_and_monotonicity():
    values = [-1, 0, 0.1, 0.5, 0.85, 1, 4]
    weights = [pacing.routing_weight(v, 0.15) for v in values]
    assert weights == sorted(weights, reverse=True)
    assert weights[:2] == [1, 1]
    assert weights[-3:] == [0, 0, 0]
    assert weights[3] == pytest.approx((0.85 - 0.5) / 0.85)


@pytest.mark.parametrize("u", [1, 1.2])
def test_exhaustion_overrides_banked_resets(u):
    assert evaluate(u=u, n=2)["state"] == "shed"


def test_429_latches_until_period_reset():
    now = START + pacing.DAY_SECONDS
    assert evaluate(u=0.01, exhausted_at=now)["state"] == "shed"
    assert evaluate(u=0.01, exhausted_at=START - 1)["state"] == "ok"
    assert evaluate(u=0.01, exhausted_at=now, used_fraction=None)["state"] == "shed"
    assert pacing.calibration_miss(1000, 0.8) == 800


def test_shed_drains_to_exact_print():
    first = evaluate()
    assert first["state"] == "shed"
    # The pool receives no dispatch. Fresh flat readings show measured zero burn.
    now = START + 2 * pacing.DAY_SECONDS
    row = evaluate(
        day=2,
        previous_state="shed",
        samples=[
            dict(ts=now - 3600, used_fraction=0.6, source=SOURCE),
            dict(ts=now, used_fraction=0.6, source=SOURCE),
        ],
    )
    assert row["burn_rate_per_day"] == 0
    assert row["state"] == "ok"
    assert "pool codex: ũ=0.60 ≤ 1−r_eff → ok" in row["fyi"]


def test_stale_and_missing_usage_are_visible():
    now = START + pacing.DAY_SECONDS
    stale = evaluate(source_ts=now - pacing.STALE_AFTER_SECONDS - 1)
    assert stale["stale"] is True and stale["state"] == "unknown"
    assert "stale" in stale["reason"]
    missing = evaluate(used_fraction=None, source="none")
    assert missing["used_fraction"] is None and missing["state"] == "unknown"
    unknown_resets = evaluate(resets=None)
    assert unknown_resets["resets_available"] is None
    assert "resets=unknown" in unknown_resets["fyi"][0]


def test_calibration_refuses_less_than_two_and_writes_evidence(tmp_path):
    output = tmp_path / "cap.json"
    incident = dict(
        agent="codex",
        category="quota",
        incident_id="a",
        ts=START + 100,
        extra=dict(weekly_tokens=1000, period_start=START),
    )
    ok, text = pacing_usage.calibrate([incident], [], output)
    assert ok is False and "at least 2" in text
    assert output.exists() is False
    second = {
        **incident,
        "incident_id": "b",
        "ts": START + 200,
        "extra": dict(weekly_tokens=1200, period_start=START),
    }
    ok, _ = pacing_usage.calibrate([incident, second], [], output)
    assert ok is True
    assert json.loads(output.read_text())["codex_weekly_token_cap"] == 1100
    assert len(json.loads(output.read_text())["evidence"]) == 2


def test_calibration_refuses_duplicate_or_shared_projection(tmp_path):
    r = dict(
        agent="codex",
        category="quota",
        incident_id="a",
        ts=START,
        extra=dict(projected_tokens=1000),
    )
    assert pacing_usage.calibrate([r, r], [], tmp_path / "cap.json")[0] is False


def cap_for(codex="shed", claude="ok", cw=0, aw=0.5):
    return {
        "agents": {
            "codex": dict(
                state=codex,
                pacing=dict(
                    state=codex, routing_weight=cw, projected_fraction=4.2, effective_reserve=0.15
                ),
            ),
            "claude": dict(
                state=claude,
                pacing=dict(
                    state=claude, routing_weight=aw, projected_fraction=0.4, effective_reserve=0.15
                ),
            ),
        }
    }


def pick(cap, **kwargs):
    return router.select_agent(
        "implement", cap, only={"codex", "claude"}, rng=random.Random(42), simulate=True, **kwargs
    )


def test_router_consumes_pacing_shed_loses_to_ok():
    result = pick(cap_for())
    assert result["agent"] == "claude"
    decision = result["pacing_decision"]
    assert decision["policy"] == "posterior*pacing"
    assert {r["agent"]: r["routing_weight"] for r in decision["candidates"]} == {
        "codex": 0,
        "claude": 0.5,
    }
    codex = next(r for r in decision["candidates"] if r["agent"] == "codex")
    assert codex["pacing"]["projected_fraction"] == 4.2


def test_all_shed_fails_toward_motion_best_posterior():
    result = pick(
        cap_for(claude="shed", aw=0),
        learned={
            "codex": {"posterior": 0.8, "score": 0.8},
            "claude": {"posterior": 0.9, "score": 0.9},
        },
    )
    assert result["agent"] == "claude"
    assert result["pacing_decision"]["policy"] == "fail-toward-motion"


def test_warn_zero_is_not_shed():
    result = pick(cap_for(claude="warn", aw=0))
    assert result["agent"] == "claude"
    assert result["pacing_decision"]["policy"] == "fallback-warn"


def test_router_weighted_probabilities_without_changing_posteriors():
    cap = cap_for(codex="ok", cw=0.25, aw=0.75)
    learned = {a: {"posterior": 0.8, "score": 0.8} for a in ("codex", "claude")}
    original = copy.deepcopy(learned)
    rng = random.Random(2)
    results = [
        router.select_agent(
            "implement", cap, only={"codex", "claude"}, learned=learned, rng=rng, simulate=True
        )["agent"]
        for _ in range(1000)
    ]
    assert 700 < results.count("claude") < 800
    assert learned == original


def test_pacing_cannot_override_auth_failure():
    cap = cap_for(claude="shed", aw=0)
    cap["agents"]["claude"]["policy"] = "auth-failed"
    assert pick(cap)["agent"] == "codex"


def test_usage_claude_timestamp_and_zero(tmp_path):
    path = tmp_path / "claude.json"
    path.write_text(json.dumps({"samples": [{"t": START * 1000, "u": {"sd": 0}}]}))
    value = pacing_usage.usage("claude", START, {}, claude_path=path)
    assert value["used_fraction"] == 0
    assert value["ts"] == START
    assert value["source"].startswith("claude-desktop")


def test_weekly_shared_totals_not_attributed(tmp_path):
    path = tmp_path / "weekly-fixture.json"
    path.write_text(
        json.dumps(
            {
                "weekly": [
                    dict(
                        agent="all",
                        period="2026-10-12",
                        totalTokens=100,
                        metadata={"agents": ["claude", "codex"]},
                    )
                ]
            }
        )
    )
    assert pacing_usage.weekly_tokens(path, "codex") == []


def test_reset_loader_absence_expiry_and_unknown(tmp_path):
    path = tmp_path / "resets.json"
    data, lines = pacing_usage.reset_inventory(path, START)
    assert data == {} and "resets=unknown" in lines[0]
    path.write_text(json.dumps({"codex": [{"expires": "2026-01-01"}], "claude": []}))
    data, lines = pacing_usage.reset_inventory(path, START)
    assert len(pacing.active_resets(data["codex"], START)) == 0
    assert "expired" in lines[0]


def test_export_keeps_existing_keys_and_pacing():
    snapshot = cap_for()
    snapshot["generated_at"] = 123
    row = route_weights_export._capacity_block(snapshot)["codex"]
    assert row["observed_at"] == 123 and row["window"] == capacity.AGENTS["codex"]["window"]
    assert row["pacing"] == snapshot["agents"]["codex"]["pacing"]


def test_report_three_numbers_unmeasured(tmp_path):
    text = pacing_usage.report(tmp_path, now=START)
    for number in ("1 codex:", "1 claude:", "2:", "3:"):
        assert number in text
    assert "unmeasured — would need" in text
    assert "usage store + rate-limit ledger" in text


def test_integrate_samples_per_tick_and_source_staleness(tmp_path, monkeypatch):
    monkeypatch.setattr(pacing_usage, "RESETS_FILE", tmp_path / "resets.json")
    monkeypatch.setattr(pacing_usage, "CALIBRATION_FILE", tmp_path / "cap.json")
    monkeypatch.setattr(
        pacing_usage, "usage", lambda *a, **k: dict(used_fraction=0.1, ts=START, source=SOURCE)
    )
    agents = {a: dict(state="ok", reason="native") for a in ("codex", "claude")}
    pacing_usage.integrate(agents, now=START, handoff=tmp_path, record=True)
    samples = pacing_usage.read_rows(tmp_path / "pacing-usage-samples.ndjson")
    assert len(samples) == 2
    assert all("source" in r and "ts" in r for r in samples)
    assert len(pacing_usage.read_rows(tmp_path / "capacity-ledger.ndjson")) == 1
    pacing_usage.integrate(agents, now=START + pacing.STALE_AFTER_SECONDS + 1, handoff=tmp_path)
    assert agents["claude"]["pacing"]["stale"] is True


def test_429_calibration_miss_consumed_and_cap_lowered(tmp_path):
    path = tmp_path / "calibration.json"
    path.write_text(json.dumps({"codex_weekly_token_cap": 1000}))
    snapshots = [
        dict(
            ts=START,
            agents={
                "codex": {
                    "pacing": dict(state="ok", used_fraction=0.8, stale=False, source="fixture")
                }
            },
        )
    ]
    incidents = [dict(incident_id="miss-1", ts=START + 30, agent="codex", category="quota")]
    lines = pacing_usage.calibration_misses(
        incidents, snapshots, handoff=tmp_path, calibration_path=path
    )
    assert "calibration miss" in lines[0]
    assert json.loads(path.read_text())["codex_weekly_token_cap"] == 800
    assert len(pacing_usage.read_rows(tmp_path / "pacing-calibration-misses.ndjson")) == 1
    assert (
        pacing_usage.calibration_misses(
            incidents, snapshots, handoff=tmp_path, calibration_path=path
        )
        == []
    )


def test_report_measured_zero_is_not_unmeasured(tmp_path):
    # Complete snapshot coverage, a real empty incident ledger, and populations in
    # both states allow a measured zero to be represented and assessed.
    end = START + pacing.PERIOD_SECONDS
    rows = []
    for ts in range(int(START), int(end), pacing.TICK_SECONDS):
        state = "ok" if ts < START + 3 * pacing.DAY_SECONDS else "warn"
        row = dict(
            state=state,
            used_fraction=0.8,
            projected_fraction=0.8,
            effective_reserve=0.15,
            period_start=START,
            period_end=end,
            source="fixture",
            ts=ts,
            stale=False,
        )
        rows.append(
            dict(
                ts=ts,
                event="pacing_snapshot",
                agents={
                    "codex": dict(state=state, pacing=row),
                    "claude": dict(state="ok", pacing=row),
                },
            )
        )
    rows += [
        dict(ts=START + 100, event="start", agent="codex", count=1),
        dict(ts=START + 4 * pacing.DAY_SECONDS, event="start", agent="claude", count=1),
    ]
    pacing_usage.append_rows(tmp_path / "capacity-ledger.ndjson", rows)
    (tmp_path / "rate-limit-incidents.ndjson").write_text("")
    text = pacing_usage.report(tmp_path, now=end)
    assert "1 codex: u(T1)=0.800" in text
    assert "2: 0 incidents" in text
    assert "3: non-codex share ok=0.000, warn/shed=1.000" in text


def test_calibration_can_match_token_samples_without_existing_cap(tmp_path):
    incidents = [
        dict(agent="codex", incident_id=str(i), category="quota", ts=START + i) for i in (1, 2)
    ]
    samples = [
        dict(
            agent="codex",
            ts=START,
            source="ccusage-unscaled:weekly-agent-tokens",
            weekly_tokens=1000,
            period_start=START,
        )
    ]
    ok, _ = pacing_usage.calibrate(incidents, samples, tmp_path / "cap.json")
    assert ok is True


def test_unknown_samples_do_not_crash_next_tick(tmp_path, monkeypatch):
    monkeypatch.setattr(pacing_usage, "RESETS_FILE", tmp_path / "resets.json")
    monkeypatch.setattr(pacing_usage, "CALIBRATION_FILE", tmp_path / "cap.json")
    monkeypatch.setattr(
        pacing_usage, "usage", lambda *a, **k: dict(used_fraction=None, ts=None, source="none")
    )
    (tmp_path / "cap.json").write_text(json.dumps({"codex_period_start": START}))
    agents = {a: dict(state="ok", reason="native") for a in ("codex", "claude")}
    for tick in (START, START + pacing.TICK_SECONDS):
        pacing_usage.integrate(agents, now=tick, handoff=tmp_path, record=True)
    assert len(pacing_usage.read_rows(tmp_path / "pacing-usage-samples.ndjson")) == 4


def test_plan_all_shed_reaches_dispatch_assignment(monkeypatch):
    monkeypatch.setattr(router.claims, "active_claims", lambda: {})
    cap = cap_for(claude="shed", aw=0)
    cap.update(capacity.profile_capacity_snapshot(cap))
    result = router.plan(
        [dict(target="fixture/repo#668", task_type="implement", lane="opener")], cap, dry_run=True
    )
    assert len(result["assignments"]) == 1
    assert result["assignments"][0]["pacing_decision"]["policy"] == "fail-toward-motion"


def test_remote_reserved_warn_beats_shed():
    result = router.select_remote_agent("implement", cap_for(claude="warn", aw=0))
    assert result["agent"] == "claude"
    assert result["pacing_decision"]["policy"] == "fallback-warn"


def test_weighted_routing_respects_tick_load():
    cap = cap_for(codex="ok", cw=0.1, aw=1)
    result = pick(cap, load={"claude": router.PER_AGENT_TICK_CAP})
    assert result["agent"] == "codex"
