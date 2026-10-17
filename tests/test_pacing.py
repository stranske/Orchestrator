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
    assert row["pacing"] == {**snapshot["agents"]["codex"]["pacing"], "burn_per_day": None}


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


@pytest.mark.parametrize("category,cleared", [("quota", True), (None, False)])
def test_consumed_reset_clears_only_dated_quota_marker(tmp_path, monkeypatch, category, cleared):
    now = START + 2 * pacing.DAY_SECONDS
    monkeypatch.setattr(capacity.time, "time", lambda: now)
    monkeypatch.setattr(capacity, "SHED_DIR", tmp_path / "shed")
    monkeypatch.setattr(pacing_usage, "RESETS_FILE", tmp_path / "resets.json")
    capacity.SHED_DIR.mkdir()
    marker = capacity.SHED_DIR / "codex"
    marker.write_text(
        json.dumps(
            dict(
                created_at=START,
                expires_at=now + 1000,
                category=category,
                incident_id="quota-before-reset",
            )
        )
    )
    pacing_usage.RESETS_FILE.write_text(
        json.dumps({"codex": [dict(expires="2026-11-16", consumed_at=now - 100)], "claude": []})
    )
    assert capacity._shed("codex") is (not cleared)
    assert marker.exists() is (not cleared)
    if cleared:
        monkeypatch.setattr(
            capacity,
            "_auth_health",
            lambda agent: dict(checked=True, authenticated=False, reason="expired credentials"),
        )
        state, _, meta = capacity.compute("codex", capacity.AGENTS["codex"], {})
        assert state == "shed" and meta["policy"] == "auth-failed"


def test_claude_reset_clears_exhaustion_without_moving_T1(tmp_path, monkeypatch):
    now = START + 2 * pacing.DAY_SECONDS
    monkeypatch.setattr(pacing_usage, "RESETS_FILE", tmp_path / "resets.json")
    monkeypatch.setattr(pacing_usage, "CALIBRATION_FILE", tmp_path / "cap.json")
    monkeypatch.setattr(
        pacing_usage, "usage", lambda *a, **k: dict(used_fraction=0.2, ts=now, source=SOURCE)
    )
    pacing_usage.RESETS_FILE.write_text(
        json.dumps({"codex": [], "claude": [dict(expires="2026-11-16", consumed_at=now - 1800)]})
    )
    pacing_usage.append_rows(
        tmp_path / "pacing-usage-samples.ndjson",
        [dict(agent="claude", ts=now - 3600, used_fraction=1, source=SOURCE)],
    )
    agents = {a: dict(state="ok", reason="native") for a in ("codex", "claude")}
    pacing_usage.integrate(agents, now=now, handoff=tmp_path)
    row = agents["claude"]["pacing"]
    assert row["state"] == "ok"
    assert row["period_end"] == pacing.CLAUDE_RESET_ANCHOR


def rollout_file(root, observations):
    path = root / "rollout-fixture.jsonl"
    path.write_text(
        "\n".join(
            json.dumps(
                dict(
                    timestamp=ts,
                    type="event_msg",
                    payload=dict(
                        type="token_count",
                        rate_limits=dict(
                            primary=dict(used_percent=percent, window_minutes=10080, resets_at=end)
                        ),
                    ),
                )
            )
            for ts, percent, end in observations
        )
        + "\n"
    )
    return path


@pytest.mark.parametrize("age,stale", [(0, False), (pacing.STALE_AFTER_SECONDS + 1, True)])
def test_codex_rollout_fresh_stale_and_sanitized(tmp_path, age, stale):
    end = START + pacing.PERIOD_SECONDS
    rollout_file(tmp_path, [(START, 35, end)])
    datum = pacing_usage.usage("codex", START + age, {}, sessions=tmp_path)
    assert datum["used_fraction"] == 0.35
    assert datum["ts"] == START
    assert datum["period_end"] == end
    assert datum["source"] == "codex-rollout:rate_limits.primary"
    assert set(datum) == {
        "used_fraction",
        "ts",
        "period_end",
        "source",
        "reset_events",
        "reset_signals",
        "short_used_fraction",
        "scan",
    }
    row = evaluate(
        now=START + age,
        period_start=START,
        period_end=end,
        source_ts=datum["ts"],
        used_fraction=datum["used_fraction"],
    )
    assert row["stale"] is stale


@pytest.mark.parametrize("percent,expected", [(100, "shed"), (0, "ok")])
def test_rollout_percent_drives_integrated_state(tmp_path, monkeypatch, percent, expected):
    monkeypatch.setattr(pacing_usage, "CODEX_SESSIONS", tmp_path)
    monkeypatch.setattr(pacing_usage, "RESETS_FILE", tmp_path / "resets.json")
    monkeypatch.setattr(pacing_usage, "CALIBRATION_FILE", tmp_path / "calibration.json")
    rollout_file(tmp_path, [(START, percent, START + pacing.PERIOD_SECONDS)])
    agents = {p: dict(state="ok", reason="native") for p in ("codex", "claude")}
    pacing_usage.integrate(agents, now=START, handoff=tmp_path)
    assert agents["codex"]["state"] == expected
    row = agents["codex"]["pacing"]
    assert row["burn_per_day"] == row["burn_rate_per_day"]
    assert {
        "state",
        "used_fraction",
        "allowed_fraction",
        "period_end",
        "resets_available",
    } <= row.keys()


def test_rollout_reset_moves_T0_and_debits_oldest_once(tmp_path, monkeypatch):
    monkeypatch.setattr(pacing_usage, "CODEX_SESSIONS", tmp_path)
    monkeypatch.setattr(pacing_usage, "RESETS_FILE", tmp_path / "resets.json")
    monkeypatch.setattr(pacing_usage, "CALIBRATION_FILE", tmp_path / "calibration.json")
    old_end = START + pacing.PERIOD_SECONDS
    now = START + 2 * pacing.DAY_SECONDS
    rollout_file(tmp_path, [(now - 1, 100, old_end), (now, 0, now + pacing.PERIOD_SECONDS)])
    inventory = dict(
        codex=[dict(id="later", expires="2026-11-16"), dict(id="first", expires="2026-11-01")],
        claude=[],
    )
    (tmp_path / "resets.json").write_text(json.dumps(inventory))
    for tick in (now, now + pacing.TICK_SECONDS):
        agents = {p: dict(state="ok", reason="native") for p in ("codex", "claude")}
        pacing_usage.integrate(agents, now=tick, handoff=tmp_path, record=True)
        assert agents["codex"]["pacing"]["period_start"] == now
        assert agents["codex"]["pacing"]["resets_available"] == 1
    stored = json.loads((tmp_path / "resets.json").read_text())
    assert stored["codex"][1]["consumed_at"] == now
    assert "consumed_at" not in stored["codex"][0]


@pytest.mark.parametrize("change", ["drop", "end"])
def test_rollout_reset_detected_from_each_signal(tmp_path, change):
    end = START + pacing.PERIOD_SECONDS
    rollout_file(
        tmp_path,
        [
            (START, 50, end),
            (START + 1, 0 if change == "drop" else 50, end + 10 if change == "end" else end),
        ],
    )
    datum = pacing_usage.codex_rollout_usage(tmp_path, START + 1)
    assert datum["reset_signals"] == [START + 1]
    assert datum["reset_events"] == []


def test_newer_measured_sample_drains_latched_shed():
    incident = START + pacing.DAY_SECONDS
    assert evaluate(u=0.01, exhausted_at=incident)["state"] == "shed"
    row = evaluate(day=2, u=0.01, exhausted_at=incident, previous_state="shed")
    assert row["state"] == "ok"
    assert "pool codex: ũ=0.04 ≤ 1−r_eff → ok" in row["fyi"]
    stale = evaluate(day=2, u=0.01, exhausted_at=incident, source_ts=incident + 1)
    assert stale["state"] == "shed"


def test_drained_print_without_burn_projection():
    row = evaluate(u=0.01, samples=False, previous_state="shed")
    assert row["state"] == "ok"
    assert "pool codex: shed → ok (reset/rollover; ũ=unmeasured)" in row["fyi"]


def test_integrate_uncalibrated_codex_stale_claude_stay_routable(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(pacing_usage, "RESETS_FILE", tmp_path / "resets.json")
    monkeypatch.setattr(pacing_usage, "CALIBRATION_FILE", tmp_path / "calibration.json")
    monkeypatch.setattr(
        pacing_usage,
        "usage",
        lambda pool, *a, **k: dict(
            used_fraction=None if pool == "codex" else 0.37,
            ts=START - pacing.STALE_AFTER_SECONDS - 1,
            source="fixture",
        ),
    )
    agents = {p: dict(state="ok", reason="native") for p in ("codex", "claude")}
    pacing_usage.integrate(agents, now=START, handoff=tmp_path)
    assert all(a["state"] == "ok" and a["pacing"]["state"] == "unknown" for a in agents.values())
    assert all("pacing.state=unknown" in a["reason"] for a in agents.values())
    monkeypatch.setattr(capacity, "build", lambda: dict(agents=agents))
    monkeypatch.setattr(router, "learned_ranks", lambda: {})
    assert capacity.main(["pick-agent", "--candidates", "codex,claude"]) == 0
    assert capsys.readouterr().out.strip() in agents


@pytest.mark.parametrize(
    "legacy,paced,expected",
    [("warn", "ok", "warn"), ("shed", "ok", "shed"), ("ok", "warn", "warn")],
)
def test_integrate_only_worsens_native_states(tmp_path, monkeypatch, legacy, paced, expected):
    monkeypatch.setattr(pacing_usage, "RESETS_FILE", tmp_path / "absent")
    monkeypatch.setattr(pacing_usage, "CALIBRATION_FILE", tmp_path / "calibration.json")
    monkeypatch.setattr(
        pacing_usage, "usage", lambda *a, **k: dict(used_fraction=0.01, ts=START, source=SOURCE)
    )
    monkeypatch.setattr(
        pacing, "evaluate", lambda *a, **k: dict(state=paced, reason="fixture", fyi=[])
    )
    agents = {p: dict(state=legacy, reason="native") for p in ("codex", "claude")}
    pacing_usage.integrate(agents, now=START, handoff=tmp_path)
    assert all(a["state"] == expected for a in agents.values())


def test_all_unknown_pacing_matches_legacy_including_exploration():
    cap = {"agents": {a: dict(state="ok") for a in ("codex", "claude", "cursor", "vibe", "gemini")}}
    unknown = copy.deepcopy(cap)
    for row in unknown["agents"].values():
        row["pacing"] = dict(state="unknown", routing_weight=None)
    for seed in range(30):
        args = dict(simulate=True, exploration_rate=0.2)
        before = router.select_agent("implement", cap, rng=random.Random(seed), **args)
        after = router.select_agent("implement", unknown, rng=random.Random(seed), **args)
        assert before == after


@pytest.mark.parametrize("field", ["late", "state", "policy", "drain"])
def test_measured_weight_preserves_policy_tiers(monkeypatch, field):
    cap = cap_for(codex="ok", cw=1, aw=0.01)
    if field == "late":
        monkeypatch.setitem(router._ROUTE_TABLE_TYPED["implement"]["agents"][1], "late", True)
    elif field == "state":
        cap["agents"]["codex"]["state"] = "warn"
    elif field == "policy":
        monkeypatch.setattr(
            router, "_capacity_policy_bias", lambda task, agent, cap: int(agent == "codex")
        )
    else:
        monkeypatch.setattr(router, "_drain_urgency", lambda agent, cap: int(agent == "codex"))
    assert pick(cap, exploration_rate=0)["agent"] == "claude"


def test_unmeasured_claude_stays_reserved(monkeypatch):
    monkeypatch.setattr(router, "_exploration_rate", lambda *a: 0)
    cap = cap_for(codex="ok", cw=0.5, aw=None)
    cap["agents"]["claude"]["pacing"]["state"] = "unknown"
    assert router.select_remote_agent("implement", cap)["agent"] == "codex"


@pytest.mark.parametrize("unmeasured", [False, True])
def test_pick_agent_cli_injected_capacity(monkeypatch, capsys, unmeasured):
    cap = cap_for(codex="ok", cw=0.5)
    if unmeasured:
        for a in cap["agents"].values():
            a["pacing"] = dict(state="unknown", routing_weight=None)
    monkeypatch.setattr(capacity, "build", lambda: cap)
    monkeypatch.setattr(router, "learned_ranks", lambda: {})
    assert capacity.main(["pick-agent", "--candidates", "codex,claude"]) == 0
    assert capsys.readouterr().out.strip() in cap["agents"]


def test_calibration_refusal_counts_every_missing_kind(tmp_path):
    a = dict(agent="codex", category="quota", incident_id="a", ts=START)
    ok, text = pacing_usage.calibrate([a, a, {**a, "incident_id": None}], [], tmp_path / "cap.json")
    assert not ok
    for expected in ("seen=3", "unmatched=2", "duplicate=1", "no_weekly_tokens=1"):
        assert expected in text


def test_export_ignores_volatile_pacing_but_publishes_capacity_changes():
    base = dict(capacity=route_weights_export._capacity_block(cap_for()))
    other = copy.deepcopy(base)
    pace = other["capacity"]["codex"]["pacing"]
    pace.update(ts=1, reason="tick", burn_rate_per_day=0.5, burn_per_day=0.5, fyi=["tick"])
    assert route_weights_export._semantic_for_compare(
        base
    ) == route_weights_export._semantic_for_compare(other)
    pace["state"] = "ok"
    assert route_weights_export._semantic_for_compare(
        base
    ) != route_weights_export._semantic_for_compare(other)


@pytest.mark.parametrize("missing,noncodex", [(False, False), (True, True)])
def test_report_zero_share_and_missing_tick_are_unmeasured(tmp_path, missing, noncodex):
    end = START + pacing.PERIOD_SECONDS
    rows = [
        dict(
            ts=t,
            event="pacing_snapshot",
            agents=dict(
                codex=dict(
                    state="ok" if t < START + pacing.DAY_SECONDS else "warn",
                    pacing=dict(period_start=START, period_end=end),
                )
            ),
        )
        for t in range(int(START), int(end), pacing.TICK_SECONDS)
    ]
    if missing:
        rows.pop(100)
    rows += [
        dict(ts=START + 10, event="start", agent="codex", count=1),
        dict(
            ts=START + 2 * pacing.DAY_SECONDS,
            event="start",
            agent="claude" if noncodex else "codex",
            count=1,
        ),
    ]
    pacing_usage.append_rows(tmp_path / "capacity-ledger.ndjson", rows)
    (tmp_path / "rate-limit-incidents.ndjson").write_text("")
    text = pacing_usage.report(tmp_path, now=end)
    assert f"coverage={671 if missing else 672}/672 ticks" in text
    assert "3: unmeasured" in text or "unmeasured — no non-codex dispatches" in text
    assert "counts=" in text


@pytest.mark.parametrize(
    "category,age,used,clears",
    [
        ("quota", 0, 0.37, True),
        ("capacity", 0, 0.37, True),
        ("auth", 0, 0.37, False),
        ("quota", 1801, 0.37, False),
        ("quota", 0, 1, False),
    ],
)
def test_fresh_weekly_usage_releases_only_dated_quota_hold(
    tmp_path, monkeypatch, category, age, used, clears
):
    now = START + 2 * pacing.DAY_SECONDS
    marker = tmp_path / "claude"
    marker.write_text(json.dumps(dict(created_at=START, expires_at=now + 3600, category=category)))
    monkeypatch.setattr(capacity, "SHED_DIR", tmp_path)
    monkeypatch.setattr(capacity.time, "time", lambda: now)
    monkeypatch.setattr(pacing_usage, "RESETS_FILE", tmp_path / "absent")
    monkeypatch.setattr(
        pacing_usage,
        "usage",
        lambda *a, **k: dict(used_fraction=used, short_used_fraction=0.2, ts=now - age),
    )
    assert capacity._shed("claude") is not clears
    assert marker.exists() is not clears


@pytest.mark.parametrize("shift,scheduled", [(3, False), (pacing.PERIOD_SECONDS, True)])
def test_reset_jitter_and_scheduled_rollover_preserve_bank(tmp_path, shift, scheduled):
    end = START + pacing.PERIOD_SECONDS
    now = end if scheduled else START + 100
    rollout_file(tmp_path, [(now - 1, 50, end), (now, 49, end + shift)])
    datum = pacing_usage.codex_rollout_usage(tmp_path, now)
    assert datum["reset_signals"] == [now]
    assert datum["reset_events"] == []
    inventory = dict(codex=resets(1))
    pacing_usage.consume_detected_resets(inventory, datum, now=now, record=False)
    assert len(pacing.active_resets(inventory["codex"], now)) == 1


def test_export_schedule_and_weight_drift_wait_for_regular_cadence():
    base = dict(capacity=route_weights_export._capacity_block(cap_for()))
    other = copy.deepcopy(base)
    other["capacity"]["codex"]["pacing"].update(
        allowed_fraction=0.3, projected_fraction=0.4, routing_weight=0.1
    )
    assert route_weights_export._semantic_for_compare(
        base
    ) == route_weights_export._semantic_for_compare(other)
    other["capacity"]["codex"]["pacing"]["used_fraction"] = 0.2
    assert route_weights_export._semantic_for_compare(
        base
    ) != route_weights_export._semantic_for_compare(other)


@pytest.mark.parametrize("leader", ["claude", "cursor", "vibe", "gemini"])
def test_measured_codex_keeps_nonpacing_legacy_shares(monkeypatch, leader):
    agents = ("claude", "cursor", "codex", "vibe", "gemini")
    posteriors = dict(zip(agents, (0.766, 0.776, 0.612, 0.641, 0.561)))
    learned = {
        a: dict(rank=i, posterior=posteriors[a], n_obs=100)
        for i, a in enumerate([leader] + [a for a in agents if a != leader])
    }
    cap = {"agents": {a: dict(state="ok") for a in agents}}
    measured = copy.deepcopy(cap)
    measured["agents"]["codex"]["pacing"] = dict(state="ok", routing_weight=0.918)
    # Isolate the learned tier from host model and native drain configuration.
    monkeypatch.setattr(router.execution_profiles, "profiles_for_agent", lambda *a, **k: [])
    monkeypatch.setattr(router, "_capacity_policy_bias", lambda *a: 0)
    monkeypatch.setattr(router, "_drain_urgency", lambda *a: 0)
    for entry in router._ROUTE_TABLE_TYPED["implement"]["agents"]:
        monkeypatch.setitem(entry, "late", False)
    before = {a: 0 for a in agents}
    after = {a: 0 for a in agents}
    for seed in range(2000):
        args = dict(
            learned=learned, exploration_rate=0.2, exploration_mode="epsilon-greedy", simulate=True
        )
        old = router.select_agent("implement", cap, rng=random.Random(seed), **args)
        new = router.select_agent("implement", measured, rng=random.Random(seed), **args)
        before[old["agent"]] += 1
        after[new["agent"]] += 1
    for agent in ("cursor", "vibe", "gemini"):
        assert after[agent] == before[agent], (before, after)
    if leader == "claude":
        share = after["claude"] / (after["claude"] + after["codex"])
        assert share == pytest.approx(0.766 / (0.766 + 0.612 * 0.918), abs=0.04)


@pytest.mark.parametrize("pool", ["claude", "codex"])
@pytest.mark.parametrize("short,clears", [(None, False), (1, False), (0.2, True)])
def test_marker_requires_short_window_headroom(tmp_path, monkeypatch, capsys, pool, short, clears):
    now = START + pacing.DAY_SECONDS
    marker = tmp_path / pool
    marker.write_text(
        json.dumps(dict(created_at=now - 100, expires_at=now + 3600, category="quota"))
    )
    monkeypatch.setattr(capacity, "SHED_DIR", tmp_path)
    monkeypatch.setattr(capacity.time, "time", lambda: now)
    monkeypatch.setattr(pacing_usage, "RESETS_FILE", tmp_path / "absent")
    monkeypatch.setattr(
        pacing_usage,
        "usage",
        lambda *a, **k: dict(used_fraction=0.37, short_used_fraction=short, ts=now),
    )
    assert capacity._shed(pool) is not clears
    assert marker.exists() is not clears
    if not clears:
        printed = capsys.readouterr().err
        assert "marker age=100s" in printed and "short-window u=" in printed
        assert "cooldown in 3600s" in printed and "post-incident" in printed


@pytest.mark.parametrize("short", [0, 100, None])
def test_claude_short_window_source(tmp_path, short):
    path = tmp_path / "claude.json"
    path.write_text(json.dumps(dict(samples=[dict(t=START * 1000, u=dict(sd=37, fh=short))])))
    row = pacing_usage.usage("claude", START, {}, claude_path=path)
    assert row["short_used_fraction"] == (short / 100 if short is not None else None)


@pytest.mark.parametrize("window,short", [(300, 0), (300, 100), (10080, 100)])
def test_codex_short_window_source(tmp_path, window, short):
    path = rollout_file(tmp_path, [(START, 37, START + pacing.PERIOD_SECONDS)])
    event = json.loads(path.read_text())
    event["payload"]["rate_limits"]["secondary"] = dict(window_minutes=window, used_percent=short)
    path.write_text(json.dumps(event) + "\n")
    row = pacing_usage.usage("codex", START, {}, sessions=tmp_path)
    assert row["short_used_fraction"] == (short / 100 if window == 300 else None)


def test_rollout_cache_append_partial_and_replacement(tmp_path):
    end = START + pacing.PERIOD_SECONDS
    path = rollout_file(tmp_path, [(START, 100, end)])
    first = pacing_usage.codex_rollout_usage(tmp_path, START)
    warm = pacing_usage.codex_rollout_usage(tmp_path, START)
    assert warm["scan"]["files_read"] == 0
    assert warm["scan"]["files_cached"] == 1
    event = json.loads(path.read_text())
    event.update(timestamp=START + 1)
    event["payload"]["rate_limits"]["primary"].update(used_percent=0, resets_at=end + 3600)
    line = json.dumps(event)
    with path.open("a") as stream:
        stream.write(line[:50])
    partial = pacing_usage.codex_rollout_usage(tmp_path, START + 1)
    assert partial["ts"] == first["ts"]
    with path.open("a") as stream:
        stream.write(line[50:] + "\n")
    appended = pacing_usage.codex_rollout_usage(tmp_path, START + 1)
    assert appended["used_fraction"] == 0
    assert appended["reset_events"] == [START + 1]
    assert appended["scan"]["bytes_read"] == len(line[50:]) + 51
    # A different clock cannot make a future cached event current.
    assert pacing_usage.codex_rollout_usage(tmp_path, START)["ts"] == START
    rollout_file(tmp_path, [(START + 2, 50, end)])
    replaced = pacing_usage.codex_rollout_usage(tmp_path, START + 2)
    assert replaced["used_fraction"] == 0.5 and not replaced["reset_events"]


def test_build_reuses_usage_in_marker_and_pacing(tmp_path, monkeypatch):
    now = START + pacing.DAY_SECONDS
    (tmp_path / "codex").write_text(
        json.dumps(dict(created_at=now - 100, expires_at=now + 3600, category="quota"))
    )
    monkeypatch.setattr(capacity, "SHED_DIR", tmp_path)
    monkeypatch.setattr(capacity, "HANDOFF", tmp_path)
    monkeypatch.setattr(capacity.time, "time", lambda: now)
    monkeypatch.setattr(pacing_usage, "RESETS_FILE", tmp_path / "absent")
    monkeypatch.setattr(pacing_usage, "CALIBRATION_FILE", tmp_path / "calibration.json")
    pacing_usage.CALIBRATION_FILE.write_text(json.dumps(dict(codex_period_start=START)))
    pacing_usage.RESETS_FILE.write_text(
        json.dumps(dict(codex=[dict(expires="2026-11-16", consumed_at=now - 1000)], claude=[]))
    )
    calls = []

    def usage(pool, clock, calibration, **k):
        calls.append(pool)
        if pool == "codex":
            assert calibration["codex_period_start"] == now - 1000
        return dict(used_fraction=0.1, short_used_fraction=1, ts=now, source=SOURCE)

    monkeypatch.setattr(pacing_usage, "usage", usage)

    def compute(agent, *a):
        return ("shed" if capacity._shed(agent) else "ok"), "fixture", {}

    monkeypatch.setattr(capacity, "compute", compute)
    monkeypatch.setattr(capacity, "_tier_models", lambda *a: {})
    monkeypatch.setattr(capacity, "_tier_ceiling", lambda *a: None)
    monkeypatch.setattr(capacity, "_auth_health", lambda *a: {})
    monkeypatch.setattr(capacity, "profile_capacity_snapshot", lambda *a, **k: {})
    monkeypatch.setattr(capacity, "profile_pool_usage_from_ledger", lambda: {})
    assert capacity.build(ccusage_block={})["agents"]["codex"]["state"] == "shed"
    assert calls == ["codex", "claude"]
    assert capacity._BUILD_PACING_USAGE.get() is None


def test_report_zero_constrained_positive_baseline_is_fail(tmp_path):
    end = START + pacing.PERIOD_SECONDS
    pacing_usage.append_rows(
        tmp_path / "capacity-ledger.ndjson",
        [
            dict(
                ts=t,
                event="pacing_snapshot",
                agents=dict(
                    codex=dict(
                        state="ok" if t < START + pacing.DAY_SECONDS else "warn",
                        pacing=dict(period_start=START, period_end=end),
                    )
                ),
            )
            for t in range(int(START), int(end), pacing.TICK_SECONDS)
        ]
        + [
            dict(ts=START + 10, event="start", agent="claude", count=1),
            dict(ts=START + 2 * pacing.DAY_SECONDS, event="start", agent="codex", count=1),
        ],
    )
    line = next(
        line
        for line in pacing_usage.report(tmp_path, now=end).splitlines()
        if line.startswith("3:")
    )
    assert "ok=1.000, warn/shed=0.000" in line and "FAIL" in line
    assert "unmeasured" not in line


def test_shed_to_warn_with_projection_prints_drain():
    row = evaluate(day=6, u=0.8, previous_state="shed")
    assert row["state"] == "warn"
    printed = " ".join(row["fyi"])
    assert "shed → warn" in printed and "ũ=" in printed and "1−r_eff=" in printed
    assert "fresh flat samples or reset" in printed


@pytest.mark.parametrize("weight", [0, 0.918])
def test_pacing_subset_ignores_nonpool_positive_weight(monkeypatch, weight):
    cap = {"agents": {a: dict(state="ok") for a in ("claude", "codex", "cursor", "vibe", "gemini")}}
    for a in ("codex", "claude"):
        cap["agents"][a]["pacing"] = dict(state="ok", routing_weight=weight)
    monkeypatch.setattr(router.execution_profiles, "profiles_for_agent", lambda *a, **k: [])
    row = router.select_agent("implement", cap, exploration_rate=0, simulate=True)
    assert row["agent"] in router.PACING_POOLS
    assert row["pacing_decision"]["policy"] == ("posterior*pacing" if weight else "fallback-warn")
