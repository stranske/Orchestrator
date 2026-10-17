"""Claude source freshness, quota calibration, and report contracts (offline)."""

import json
import os

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
    weekly_sample(stores[1], age=pacing.STALE_AFTER_SECONDS + 1)
    row = read_usage(stores, dict(claude_weekly_token_cap=1000))
    agents = integrated(stores, row)
    assert agents["claude"]["pacing"]["state"] == "unknown"
    output = pacing_usage.report(stores[2], now=NOW, current=dict(agents=agents))
    assert "source=ccusage-fallback sample_age_s=1801" in output


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


def test_fallback_reuses_distinct_agent_specific_quota_calibration(stores):
    weekly_sample(stores[1])
    incidents = [
        incident("a", 900),
        incident("b"),
        incident("a", 999999),  # duplicate
        incident("codex", 999999, pool="codex"),
        incident("throttle", 999999, category="rate_limit"),
    ]
    samples = [
        dict(
            agent="claude",
            source="ccusage-fallback",
            ts=NOW - 100,
            weekly_tokens=1100,
            period_start=START,
        )
    ]
    pacing_usage.append_rows(stores[2] / "rate-limit-incidents.ndjson", incidents)
    pacing_usage.append_rows(stores[2] / "pacing-usage-samples.ndjson", samples)
    row = read_usage(stores)
    assert row["token_cap"] == 1000
    assert row["cap_source"] == "rate-limit-ledger"
    assert row["used_fraction"] == 0.4


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
    assert "need 2 matched Claude quota incidents or a published cap" in output


def test_calibration_miss_reduction_is_not_overwritten_by_ledger(stores):
    weekly_sample(stores[1])
    pacing_usage.append_rows(
        stores[2] / "rate-limit-incidents.ndjson", [incident("a", 900), incident("b", 1100)]
    )
    row = read_usage(stores, dict(claude_weekly_token_cap=800))
    assert row["token_cap"] == 800
    assert row["used_fraction"] == 0.5


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
