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
    "fractions,totals,expected", [([0.4], [400], 1000), ([0.2, 0.4, 0.6], [200, 600, 600], 1000)]
)
def test_cap_from_desktop_token_pairs(stores, fractions, totals, expected):
    samples = []
    path = pacing_usage.CLAUDE_PROJECTS / "seat.jsonl"
    previous = 0
    for i, (fraction, total) in enumerate(zip(fractions, totals)):
        ts = NOW - 20000 + i * 1000
        samples.append(dict(t=ts * 1000, u=dict(sd=fraction * 100)))
        token_event(path, ts - 1, total - previous, str(i))
        previous = total
    stores[0].write_text(json.dumps(dict(samples=samples)))
    row = read_usage(stores, dict(claude_weekly_token_cap=7))
    assert row["token_cap"] == expected
    assert row["cap_pair_count"] == len(fractions)
    assert row["cap_source"] == "desktop-token-pairs"
    assert row["source"] == "ccusage-fallback"
    assert integrated(stores, row)["claude"]["pacing"]["state"] == "ok"
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
    assert "cap: 1000" in output


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
