"""Claude source freshness, quota calibration, and report contracts (offline)."""

import hashlib
import json
import os
import tempfile

import pytest

import capacity
import pacing
import pacing_usage

START = pacing.CLAUDE_RESET_ANCHOR - pacing.PERIOD_SECONDS
NOW = START + 6 * pacing.DAY_SECONDS


@pytest.fixture
def stores(tmp_path, monkeypatch):
    desktop = tmp_path / "claude.json"
    snapshots = tmp_path / "snapshots"
    snapshots.mkdir()
    monkeypatch.setattr(pacing_usage, "CLAUDE_USAGE", desktop)
    monkeypatch.setattr(pacing_usage, "SNAPSHOTS", snapshots)
    monkeypatch.setattr(pacing_usage, "HANDOFF", tmp_path)
    monkeypatch.setattr(pacing_usage, "RESETS_FILE", tmp_path / "resets.json")
    monkeypatch.setattr(pacing_usage, "CALIBRATION_FILE", tmp_path / "calibration.json")
    monkeypatch.setattr(pacing_usage, "CODEX_SESSIONS", tmp_path / "no-rollouts")
    monkeypatch.setattr(capacity, "HANDOFF", tmp_path)
    monkeypatch.setattr(pacing_usage, "CLAUDE_PROJECTS", tmp_path / "projects")
    # Offline: never let the live poller's files decide selection or boundaries.
    monkeypatch.setattr(pacing_usage, "POLL_DIR", tmp_path / "polls")
    (tmp_path / "resets.json").write_text(json.dumps(dict(claude=[], codex=[])))
    return desktop, snapshots, tmp_path


def desktop_sample(path, age, percent=37):
    path.write_text(json.dumps(dict(samples=[dict(t=(NOW - age) * 1000, u=dict(sd=percent))])))


def weekly_sample(snapshots, *, age=60, pool="claude", start=START, tokens=400):
    path = snapshots / "weekly-fixture.json"
    path.write_text(json.dumps(dict(weekly=[dict(agent=pool, period=start, totalTokens=tokens)])))
    os.utime(path, (NOW - age, NOW - age))
    return path


def read_usage(stores, calibration=None):
    desktop, snapshots, handoff = stores
    return pacing_usage.usage(
        "claude",
        NOW,
        calibration or {},
        snapshots=snapshots,
        claude_path=desktop,
        handoff=handoff,
    )


def integrated(stores, datum):
    agents = {pool: dict(state="ok", reason="native") for pool in ("codex", "claude")}
    pacing_usage.integrate(
        agents,
        now=NOW,
        handoff=stores[2],
        usage_data={"claude": datum, "codex": dict(used_fraction=None, ts=None, source="none")},
    )
    return agents


@pytest.mark.parametrize("age", [0, 60, pacing_usage.CLAUDE_USAGE_MAX_AGE_SECONDS])
def test_fresh_desktop_source_preserves_observation_time(stores, age):
    desktop_sample(stores[0], age)
    weekly_sample(stores[1])
    row = read_usage(stores, dict(claude_weekly_token_cap=1000))
    assert row["source"] == "claude-desktop"
    assert row["used_fraction"] == 0.37
    assert row["ts"] == NOW - age
    assert row["sample_age_s"] == age
    assert integrated(stores, row)["claude"]["pacing"]["state"] != "unknown"


def test_stale_desktop_uses_fallback_with_computed_state(stores):
    desktop_sample(stores[0], pacing_usage.CLAUDE_USAGE_MAX_AGE_SECONDS + 1, percent=99)
    weekly_sample(stores[1])
    row = read_usage(stores, dict(claude_weekly_token_cap=1000))
    assert row["source"] == "ccusage-fallback"
    assert row["used_fraction"] == 0.4
    assert row["sample_age_s"] == 60
    assert row["desktop_sample_age_s"] == 7201
    result = integrated(stores, row)["claude"]["pacing"]
    assert result["state"] == "ok"
    assert result["projected_fraction"] is None  # cold-start schedule test
    assert result["routing_weight"] > 0


def test_missing_desktop_uses_fallback(stores):
    weekly_sample(stores[1])
    row = read_usage(stores, dict(claude_weekly_token_cap=1000))
    assert row["source"] == "ccusage-fallback"
    assert row["desktop_sample_age_s"] is None
    assert integrated(stores, row)["claude"]["pacing"]["state"] == "ok"


@pytest.mark.parametrize("age,expected", [(60, "claude-desktop"), (7201, "ccusage-fallback")])
def test_capacity_report_prints_source_and_selected_sample_age(
    stores, monkeypatch, capsys, age, expected
):
    desktop_sample(stores[0], age)
    weekly_sample(stores[1])
    pacing_usage.CALIBRATION_FILE.write_text(json.dumps(dict(claude_weekly_token_cap=1000)))
    monkeypatch.setattr(capacity.time, "time", lambda: NOW)
    assert capacity.main(["pacing-report"]) == 0
    output = capsys.readouterr().out
    line = next(line for line in output.splitlines() if line.startswith("pool claude:"))
    assert f"source={expected} sample_age_s=60 " in line
    assert f"desktop_sample_age_s={age}" in line
    if expected == "ccusage-fallback":
        assert "cap_source=calibration-file" in line


def test_old_fallback_remains_visibly_stale(stores):
    weekly_sample(stores[1], age=pacing_usage.CLAUDE_USAGE_MAX_AGE_SECONDS + 1)
    row = read_usage(stores, dict(claude_weekly_token_cap=1000))
    agents = integrated(stores, row)
    assert agents["claude"]["pacing"]["state"] == "unknown"
    output = pacing_usage.report(stores[2], now=NOW, current=dict(agents=agents))
    assert "source=ccusage-fallback sample_age_s=7201" in output


@pytest.mark.parametrize("pool,start", [("all", START), ("codex", START), ("claude", START + 2)])
def test_fallback_rejects_mixed_agent_or_wrong_period(stores, pool, start):
    weekly_sample(stores[1], pool=pool, start=start)
    row = read_usage(stores, dict(claude_weekly_token_cap=1000))
    assert row["used_fraction"] is None
    assert row["sample_age_s"] is None
    assert "no Claude-only snapshot" in row["fallback_reason"]


def incident(identity, tokens=None, *, pool="claude", category="quota"):
    return dict(
        agent=pool,
        category=category,
        incident_id=identity,
        ts=NOW - 100,
        extra=dict(weekly_tokens=tokens, period_start=START),
    )


def token_event(path, ts, tokens, identity):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as stream:
        stream.write(
            json.dumps(
                dict(
                    type="assistant",
                    timestamp=ts,
                    requestId=identity,
                    message=dict(id=identity, usage=dict(input_tokens=tokens)),
                )
            )
            + "\n"
        )


@pytest.mark.parametrize(
    "fractions,totals,pair_cap", [([0.4], [400], 1000), ([0.2, 0.4, 0.6], [200, 400, 600], 1000)]
)
def test_cap_from_desktop_token_pairs(stores, fractions, totals, pair_cap):
    samples = []
    path = pacing_usage.CLAUDE_PROJECTS / "seat.jsonl"
    previous = 0
    for i, (fraction, total) in enumerate(zip(fractions, totals)):
        ts = NOW - 20000 + i * 1000
        samples.append(dict(t=ts * 1000, u=dict(sd=fraction * 100)))
        token_event(path, ts - 1, total - previous, str(i))
        previous = total
    stores[0].write_text(json.dumps(dict(samples=samples)))
    # A valid calibrated cap must be authoritative; pair estimates cannot override it.
    # Use cap=500 which is lower than pair-derived cap
    row = read_usage(stores, dict(claude_weekly_token_cap=500))
    assert row["token_cap"] == 500  # calibrated cap=500 is lower and authoritative
    assert row["cap_pair_count"] == len(fractions)
    assert row["cap_source"] == "calibration-file"
    assert row["source"] == "ccusage-fallback"
    # With cap=500 and tokens=400 (first case) or 1200 (second case), fractions are 0.8 and 2.4
    # The first should be ok, the second should be shed
    if sum(totals) <= 500:
        assert integrated(stores, row)["claude"]["pacing"]["state"] == "ok"
    else:
        assert integrated(stores, row)["claude"]["pacing"]["state"] == "shed"
    cache_key = hashlib.sha256(str(pacing_usage.CLAUDE_PROJECTS.resolve()).encode()).hexdigest()
    cached = json.loads(
        (
            pacing_usage.Path(tempfile.gettempdir())
            / f"orch-claude-ccusage-{os.getuid()}-{cache_key}.json"
        ).read_text()
    )
    assert cached["ts"] == NOW
    output = pacing_usage.report(stores[2], now=NOW, current=dict(agents=integrated(stores, row)))
    assert f"pairs={len(fractions)}" in output
    assert "cap: 500" in output


def test_producer_extracts_claude_only_exact_window():
    rows = [
        dict(agent=agent, ts=ts, totalTokens=tokens)
        for agent, ts, tokens in [
            ("claude", START - 1, 900),
            ("codex", START + 1, 900),
            ("claude", START + 1, 100),
            ("claude", NOW + 1, 900),
        ]
    ]
    sample = pacing_usage.claude_window(rows, START, START + pacing.PERIOD_SECONDS, NOW)
    assert sample["agent"] == "claude"
    assert sample["tokens"] == 100
    assert sample["period_start"] == START
    assert sample["ts"] == NOW


def test_producer_deduplicates_and_respects_boundaries(stores):
    path = pacing_usage.CLAUDE_PROJECTS / "seat.jsonl"
    for ts, tokens, identity in [
        (START - 1, 500, "outside"),
        (START + 1, 100, "a"),
        (START + 1, 100, "a"),
        (NOW + 1, 500, "future"),
    ]:
        token_event(path, ts, tokens, identity)
    row = read_usage(stores, dict(claude_weekly_token_cap=1000))
    assert row["weekly_tokens"] == 100
    assert row["used_fraction"] == 0.1


def claude_poll(stores, *, age, resets_at, percent=90):
    directory = stores[2] / "polls"
    directory.mkdir(exist_ok=True)
    weekly = dict(label="All models", used_percent=percent, resets_at=resets_at)
    row = dict(ts=NOW - age, weekly=weekly, session=dict(used_percent=10))
    (directory / "claude-usage.json").write_text(json.dumps(row))


@pytest.mark.parametrize(
    "age,resets_at",
    [
        (pacing_usage.CLAUDE_USAGE_MAX_AGE_SECONDS + 1, START + pacing.PERIOD_SECONDS),
        (60, START),  # Rolled: its weekly usage describes the ended week.
    ],
)
def test_stale_or_rolled_poll_yields_to_usable_fallback(stores, age, resets_at):
    claude_poll(stores, age=age, resets_at=resets_at)
    token_event(pacing_usage.CLAUDE_PROJECTS / "seat.jsonl", NOW - 100, 100, "a")
    row = read_usage(stores, dict(claude_weekly_token_cap=1000))
    assert row["source"] == "ccusage-fallback"
    assert row["used_fraction"] == 0.1
    # The poll's (rolled) reset time survives as the authoritative boundary.
    assert row["period_end"] == START + pacing.PERIOD_SECONDS
    assert row["poll"]["valid"] is True
    assert integrated(stores, row)["claude"]["pacing"]["state"] != "unknown"


def test_fallback_uses_non_default_authoritative_boundary(stores):
    shift = 3 * pacing.DAY_SECONDS
    end = START + pacing.PERIOD_SECONDS + shift
    claude_poll(stores, age=pacing_usage.CLAUDE_USAGE_MAX_AGE_SECONDS + 1, resets_at=end)
    ts = START - 10000
    stores[0].write_text(json.dumps(dict(samples=[dict(t=ts * 1000, u=dict(sd=50))])))
    path = pacing_usage.CLAUDE_PROJECTS / "seat.jsonl"
    token_event(path, START - pacing.PERIOD_SECONDS + pacing.DAY_SECONDS, 300, "old-default")
    token_event(path, ts - 1, 500, "prior")
    token_event(path, START + pacing.DAY_SECONDS, 100, "default-only")
    token_event(path, NOW - 100, 50, "current")
    row = read_usage(stores)
    assert (row["period_start"], row["period_end"]) == (end - pacing.PERIOD_SECONDS, end)
    assert row["weekly_tokens"] == 50
    assert row["token_cap"] == 1000  # 500 / 0.5 within the authoritative week, not 800 / 0.5.
    assert integrated(stores, row)["claude"]["pacing"]["period_end"] == end
    # A cached sample from another boundary is never reused.
    (stores[2] / "polls" / "claude-usage.json").unlink()
    row = read_usage(stores)
    assert row["period_end"] == START + pacing.PERIOD_SECONDS
    assert row["weekly_tokens"] == 150


def test_disappearing_log_cannot_abort_fallback(stores, monkeypatch):
    token_event(pacing_usage.CLAUDE_PROJECTS / "seat.jsonl", NOW - 100, 100, "a")
    token_event(pacing_usage.CLAUDE_PROJECTS / "rotated.jsonl", NOW - 100, 900, "b")
    real = pacing_usage.Path.stat

    def rotating(self, *args, **kwargs):
        if self.name == "rotated.jsonl":
            raise FileNotFoundError(self)
        return real(self, *args, **kwargs)

    monkeypatch.setattr(pacing_usage.Path, "stat", rotating)
    row = read_usage(stores, dict(claude_weekly_token_cap=1000))
    assert row["source"] == "ccusage-fallback"
    assert row["weekly_tokens"] == 100


def test_insufficient_calibration_uses_configured_published_cap_and_says_so(stores):
    weekly_sample(stores[1])
    pacing_usage.append_rows(stores[2] / "rate-limit-incidents.ndjson", [incident("a", 900)])
    row = read_usage(stores, dict(claude_published_weekly_token_cap=1000))
    assert row["used_fraction"] == 0.4
    assert row["cap_source"] == "plan-published"
    output = pacing_usage.report(stores[2], now=NOW, current=dict(agents=integrated(stores, row)))
    assert "token_cap=1000 cap_source=plan-published" in output


def test_no_cap_is_unknown_with_reason_in_report(stores):
    weekly_sample(stores[1])
    row = read_usage(stores)
    agents = integrated(stores, row)
    assert agents["claude"]["pacing"]["state"] == "unknown"
    output = pacing_usage.report(stores[2], now=NOW, current=dict(agents=agents))
    assert "cap: unavailable (0 pairs)" in output


@pytest.mark.parametrize("payload", [[], {"samples": None}, {"samples": "invalid"}])
def test_malformed_desktop_uses_fallback(stores, payload):
    stores[0].write_text(json.dumps(payload))
    weekly_sample(stores[1])
    assert read_usage(stores, dict(claude_weekly_token_cap=1000))["used_fraction"] == 0.4


def test_build_snapshot_reuses_newly_lowered_calibration(stores):
    weekly_sample(stores[1])
    row = read_usage(stores, dict(claude_weekly_token_cap=1000))
    pacing_usage.CALIBRATION_FILE.write_text(json.dumps(dict(claude_weekly_token_cap=800)))
    result = integrated(stores, row)["claude"]["pacing"]
    assert result["token_cap"] == 800
    assert result["used_fraction"] == 0.5


def test_aging_unchanged_observation_does_not_republish_capacity(stores):
    desktop_sample(stores[0], 7201)
    results = []
    for now in (NOW, NOW + 10):
        agents = {pool: dict(state="ok", reason="native") for pool in ("codex", "claude")}
        pacing_usage.integrate(agents, now=now, handoff=stores[2])
        results.append(agents["claude"]["pacing"])
    assert results[0] == results[1], "age must be derived at reporting time, not exported as state"
    output = pacing_usage.report(stores[2], now=NOW + 10, current=dict(agents=agents))
    assert "desktop_sample_age_s=7211" in output


def test_historical_pair_survives_weekly_rollover(stores):
    ts = START - 10000
    stores[0].write_text(json.dumps(dict(samples=[dict(t=ts * 1000, u=dict(sd=50))])))
    path = pacing_usage.CLAUDE_PROJECTS / "seat.jsonl"
    token_event(path, ts - 1, 500, "prior")
    token_event(path, NOW - 1, 100, "current")
    row = read_usage(stores)
    assert row["token_cap"] == 1000
    assert row["cap_pair_count"] == 1
    assert row["weekly_tokens"] == 100
    assert row["used_fraction"] == 0.1


def test_producer_cache_reuses_timestamp_and_invalidates_on_new_usage(stores):
    path = pacing_usage.CLAUDE_PROJECTS / "seat.jsonl"
    token_event(path, NOW - 100, 100, "a")
    first = read_usage(stores, dict(claude_weekly_token_cap=1000))
    second = pacing_usage.usage(
        "claude",
        NOW + 10,
        dict(claude_weekly_token_cap=1000),
        snapshots=stores[1],
        claude_path=stores[0],
        handoff=stores[2],
    )
    assert second["ts"] == first["ts"]
    token_event(path, NOW - 1, 100, "b")
    assert read_usage(stores, dict(claude_weekly_token_cap=1000))["weekly_tokens"] == 200


def test_produced_tokens_without_pair_print_unavailable(stores):
    token_event(pacing_usage.CLAUDE_PROJECTS / "seat.jsonl", NOW - 1, 100, "a")
    row = read_usage(stores)
    assert row["weekly_tokens"] == 100
    assert row["token_cap"] is None
    agents = integrated(stores, row)
    assert agents["claude"]["pacing"]["state"] == "unknown"
    assert "cap: unavailable (0 pairs)" in pacing_usage.report(
        stores[2], now=NOW, current=dict(agents=agents)
    )


def test_desktop_token_pairs_cannot_override_lower_calibrated_cap_at_selection(stores):
    # Use stale desktop so it falls back to ccusage-fallback with pair_cap
    # Desktop samples must all be stale (> CLAUDE_USAGE_MAX_AGE_SECONDS old)
    # but we still need them for pair calculation in produce_claude_sample
    samples = []
    path = pacing_usage.CLAUDE_PROJECTS / "seat.jsonl"
    # Add a stale desktop sample and matching token event at the same time
    # Use a time that's stale but within the same week as other events
    stale_ts = NOW - pacing_usage.CLAUDE_USAGE_MAX_AGE_SECONDS - 1
    samples.append(dict(t=stale_ts * 1000, u=dict(sd=40)))
    token_event(path, stale_ts, 400, "a")
    stores[0].write_text(json.dumps(dict(samples=samples)))
    row = read_usage(stores, dict(claude_weekly_token_cap=1000))
    assert row["token_cap"] == 1000
    assert row["cap_source"] == "calibration-file"
    assert row["cap_pair_count"] >= 1


def test_lower_calibrated_cap_respected_during_integration_with_desktop_pairs(stores):
    # Use stale desktop so it falls back to ccusage-fallback with pair_cap
    # Desktop samples must all be stale (> CLAUDE_USAGE_MAX_AGE_SECONDS old)
    # but we still need them for pair calculation in produce_claude_sample
    samples = []
    path = pacing_usage.CLAUDE_PROJECTS / "seat.jsonl"
    # Add stale desktop samples for pair calculation
    samples.append(dict(t=(NOW - pacing_usage.CLAUDE_USAGE_MAX_AGE_SECONDS - 1) * 1000, u=dict(sd=40)))
    stores[0].write_text(json.dumps(dict(samples=samples)))
    token_event(path, NOW - 100, 400, "a")
    pacing_usage.CALIBRATION_FILE.write_text(json.dumps(dict(claude_weekly_token_cap=1000)))
    row = read_usage(stores, dict(claude_weekly_token_cap=1000))
    assert row["token_cap"] == 1000
    assert row["cap_source"] == "calibration-file"
    pacing_usage.CALIBRATION_FILE.write_text(json.dumps(dict(claude_weekly_token_cap=800)))
    result = integrated(stores, row)["claude"]["pacing"]
    assert result["token_cap"] == 800
    assert result["used_fraction"] == 0.5


def test_event_cache_reuses_history_and_refreshes_changed_files(stores):
    root = pacing_usage.CLAUDE_PROJECTS
    path = root / "seat.jsonl"
    token_event(path, NOW - 10, 100, "a")
    token_event(root / "history.jsonl", NOW - 20, 50, "history")
    cold, warm, changed = {}, {}, {}
    first = pacing_usage.claude_token_events(root, START, NOW, stats=cold)
    assert pacing_usage.claude_token_events(root, START, NOW, stats=warm) == first
    assert cold == dict(files_read=2, files_cached=0)
    assert warm == dict(files_read=0, files_cached=2)
    token_event(path, NOW - 1, 200, "b")
    rows = pacing_usage.claude_token_events(root, START, NOW, stats=changed)
    assert sum(r["totalTokens"] for r in rows) == 350
    assert changed == dict(files_read=1, files_cached=1)


def test_cached_future_events_and_replacement_keep_exact_totals(stores):
    root = pacing_usage.CLAUDE_PROJECTS
    path = root / "seat.jsonl"
    token_event(path, NOW + 10, 100, "a")
    assert pacing_usage.claude_token_events(root, START, NOW) == []
    assert pacing_usage.claude_token_events(root, START, NOW + 10)[0]["totalTokens"] == 100
    replacement = root / "replacement"
    token_event(replacement, NOW - 10, 200, "b")
    replacement.replace(path)
    assert pacing_usage.claude_token_events(root, START, NOW)[0]["totalTokens"] == 200


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
        "w_p_prev→w_p=0.000→0.250",
    ):
        assert field in text


def test_invalid_utf8_project_log_preserves_fallback_totals(stores, monkeypatch, capsys):
    token_event(pacing_usage.CLAUDE_PROJECTS / "seat.jsonl", NOW - 100, 100, "a")
    (pacing_usage.CLAUDE_PROJECTS / "corrupt.jsonl").write_bytes(b"\xff\xfe\n")
    row = read_usage(stores, dict(claude_weekly_token_cap=1000))
    assert row["source"] == "ccusage-fallback"
    assert row["weekly_tokens"] == 100
    pacing_usage.CALIBRATION_FILE.write_text(json.dumps(dict(claude_weekly_token_cap=1000)))
    monkeypatch.setattr(capacity.time, "time", lambda: NOW)
    assert capacity.main(["pacing-report"]) == 0
    assert "pool claude:" in capsys.readouterr().out


def test_rolled_poll_pre_boundary_desktop_uses_current_week_tokens(stores):
    boundary = START + pacing.PERIOD_SECONDS
    now = boundary + 500
    desktop_ts = boundary - 1000
    stores[0].write_text(json.dumps(dict(samples=[dict(t=desktop_ts * 1000, u=dict(sd=50))])))
    directory = stores[2] / "polls"
    directory.mkdir(exist_ok=True)
    weekly = dict(label="All models", used_percent=90, resets_at=boundary)
    (directory / "claude-usage.json").write_text(
        json.dumps(dict(ts=now - 60, weekly=weekly, session=dict(used_percent=10)))
    )
    token_event(pacing_usage.CLAUDE_PROJECTS / "seat.jsonl", now - 100, 150, "current")
    row = pacing_usage.usage(
        "claude",
        now,
        dict(claude_weekly_token_cap=1000),
        snapshots=stores[1],
        claude_path=stores[0],
        handoff=stores[2],
        poll_dir=directory,
    )
    assert row["source"] == "ccusage-fallback"
    assert row["weekly_tokens"] == 150
    assert row["used_fraction"] == 0.15
    assert row["desktop_sample_age_s"] == now - desktop_ts
    agents = {pool: dict(state="ok", reason="native") for pool in ("codex", "claude")}
    pacing_usage.integrate(
        agents,
        now=now,
        handoff=stores[2],
        usage_data={"claude": row, "codex": dict(used_fraction=None, ts=None, source="none")},
    )
    assert agents["claude"]["pacing"]["state"] != "unknown"
