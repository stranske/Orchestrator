"""Offline parser fixtures, source selection, rollover, health and scan-race contracts."""

import copy
import datetime as dt
import importlib.util
import json
import subprocess
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import capacity
import pacing
import pacing_usage
import route_weights_export
import router

NOW = pacing.timestamp("2026-10-17T19:00:00Z")
END = pacing.timestamp("2026-10-18T05:00:00Z")
CLAUDE_TEXT = """Current session: 61% used · resets Oct 17 at 4:40pm (America/Chicago)
Current week (all models): 85% used · resets Oct 18 at 12am (America/Chicago)
Current week (Fable): 91% used · resets Oct 18 at 12am (America/Chicago)
"""
CODEX_RESPONSE = dict(
    rateLimits=dict(
        primary=dict(usedPercent=14, windowDurationMins=10080, resetsAt=NOW + 600000),
        secondary=dict(usedPercent=20, windowDurationMins=300, resetsAt=NOW + 3600),
    ),
    ordinaryUsageAllowed=True,
    rateLimitResetCredits=dict(
        availableCount=2,
        credits=[
            dict(expiresAt=NOW + 1000000, status="available"),
            dict(expiresAt=NOW + 2000000, status="available"),
        ],
    ),
)


def load_poller(name):
    path = Path(__file__).resolve().parents[1] / "scripts/usage_poll" / f"{name}-usage-poll.py"
    spec = importlib.util.spec_from_file_location(f"{name}_usage_poll", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def pollers(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts/usage_poll"))
    return {p: load_poller(p) for p in ("codex", "claude")}


@pytest.fixture
def evidence(tmp_path, monkeypatch, pollers):
    monkeypatch.setattr(pacing_usage, "POLL_DIR", tmp_path / "polls")
    monkeypatch.setattr(pacing_usage, "RESETS_FILE", tmp_path / "resets.json")
    monkeypatch.setattr(pacing_usage, "CALIBRATION_FILE", tmp_path / "calibration.json")
    monkeypatch.setattr(pacing_usage, "CODEX_SESSIONS", tmp_path / "sessions")
    monkeypatch.setattr(pacing_usage, "SNAPSHOTS", tmp_path / "snapshots")
    monkeypatch.setattr(pacing_usage, "CLAUDE_USAGE", tmp_path / "desktop.json")
    pacing_usage.POLL_DIR.mkdir()
    for pool, module in pollers.items():
        row = (
            module.parse_response(copy.deepcopy(CODEX_RESPONSE), NOW)
            if pool == "codex"
            else module.parse_usage(CLAUDE_TEXT, dt.datetime.fromtimestamp(NOW, dt.timezone.utc))
        )
        write_poll(pool, row)
    pacing_usage.RESETS_FILE.write_text(
        json.dumps(dict(codex=[dict(expires=NOW + 1000000)], claude=[]))
    )
    return tmp_path


def write_poll(pool, row):
    path = pacing_usage.POLL_DIR / f"{pool}-usage.json"
    path.write_text(json.dumps(row))
    return row


def integrate(tmp_path, now=NOW, record=False):
    agents = {p: dict(state="ok", reason="fixture") for p in ("codex", "claude")}
    messages = pacing_usage.integrate(agents, now=now, handoff=tmp_path, record=record)
    return agents, "\n".join(messages)


def test_claude_usage_fixture(pollers):
    row = pollers["claude"].parse_usage(
        CLAUDE_TEXT, dt.datetime.fromtimestamp(NOW, dt.timezone.utc)
    )
    assert row["weekly"]["resets_at"] == END
    assert row["weekly"]["used_percent"] == 85
    assert row["session"]["used_percent"] == 61
    assert row["session"]["resets_at"] == pacing.timestamp("2026-10-17T21:40:00Z")
    assert row["buckets"][2]["label"] == "Fable"


@pytest.mark.parametrize(
    "text,clock,expected",
    [
        ("Oct 17 at 4:40pm", "2026-10-17T19:00Z", "2026-10-17T21:40Z"),
        ("Oct 18 at 12am", "2026-10-17T19:00Z", "2026-10-18T05:00Z"),
        ("Oct 18 at 12pm", "2026-10-17T19:00Z", "2026-10-18T17:00Z"),
        ("12am", "2026-10-17T19:00Z", "2026-10-18T05:00Z"),
        ("12pm", "2026-10-17T16:00Z", "2026-10-17T17:00Z"),
        ("Jan 1 at 12am", "2026-12-31T19:00Z", "2027-01-01T06:00Z"),
        ("Dec 31 at 11:59pm", "2027-01-01T06:00Z", "2027-01-01T05:59Z"),
    ],
)
def test_claude_reset_edges(pollers, text, clock, expected):
    now = dt.datetime.fromisoformat(clock)
    assert pollers["claude"].parse_reset(
        text, ZoneInfo("America/Chicago"), now
    ) == pacing.timestamp(expected)


@pytest.mark.parametrize("text", ["", "0am", "13pm", "12:60am", "Oct 32 at 12am"])
def test_bad_reset_rejected(pollers, text):
    with pytest.raises(ValueError):
        pollers["claude"].parse_reset(
            text, ZoneInfo("America/Chicago"), dt.datetime.now(dt.timezone.utc)
        )


@pytest.mark.parametrize("credits", [False, True])
def test_codex_response_fixture_sanitized(pollers, credits):
    response = copy.deepcopy(CODEX_RESPONSE)
    if not credits:
        response.pop("rateLimitResetCredits")
    response["accountId"] = "DO_NOT_RETAIN"
    response["rateLimits"]["credits"] = dict(balance="DO_NOT_RETAIN")
    if credits:
        response["rateLimitResetCredits"]["credits"][0]["id"] = "DO_NOT_RETAIN"
    row = pollers["codex"].parse_response(response, NOW)
    assert row["primary"]["used_percent"] == 14
    assert row["secondary"]["used_percent"] == 20
    assert row["reset_credits_available"] == (2 if credits else None)
    assert (row["reset_credits"] is not None) == credits
    assert "DO_NOT_RETAIN" not in json.dumps(row)


@pytest.mark.parametrize(
    "pool,failure",
    [
        ("claude", "command"),
        ("claude", "parse"),
        ("claude", "timeout"),
        ("codex", "command"),
        ("codex", "parse"),
    ],
)
def test_failure_leaves_latest_untouched(tmp_path, pollers, monkeypatch, pool, failure):
    module = pollers[pool]
    path = tmp_path / f"{pool}-usage.json"
    path.write_text('{"ts":1}')
    before = path.stat().st_mtime_ns
    if pool == "claude":
        monkeypatch.setattr(module.shutil, "which", lambda *a, **k: "/fixture/claude")

        monkeypatch.setattr(module.time, "sleep", lambda _: None)

        def run_usage(executable):
            if failure == "timeout":
                return "", "timeout 40s"
            return ("", "exit 1") if failure == "command" else ("invalid", "")

        monkeypatch.setattr(module, "run_usage", run_usage)
    else:

        def read():
            if failure == "command":
                raise RuntimeError("fixture")
            return {}

        monkeypatch.setattr(module, "read_response", read)
    with pytest.raises((RuntimeError, ValueError, subprocess.TimeoutExpired)):
        module.main(tmp_path)
    assert path.read_text() == '{"ts":1}' and path.stat().st_mtime_ns == before
    assert not (tmp_path / f"{pool}-usage.ndjson").exists()


@pytest.mark.parametrize("pool", ["codex", "claude"])
def test_publish_roundtrip(tmp_path, pollers, pool):
    pollers[pool].publish(dict(ts=1), tmp_path)
    assert json.loads((tmp_path / f"{pool}-usage.json").read_text()) == dict(ts=1)
    assert pacing_usage.read_rows(tmp_path / f"{pool}-usage.ndjson") == [dict(ts=1)]


def test_both_polled_measured_inventory_discrepancy_and_buckets(evidence):
    first, _ = integrate(evidence)
    later, _ = integrate(evidence, now=NOW + 1)
    before = dict(capacity=route_weights_export._capacity_block(dict(agents=first)))
    after = dict(capacity=route_weights_export._capacity_block(dict(agents=later)))
    assert route_weights_export._semantic_for_compare(
        before
    ) == route_weights_export._semantic_for_compare(after)
    agents, text = integrate(evidence, record=True)
    assert agents["claude"]["pacing"]["period_end"] == END
    assert agents["claude"]["pacing"]["used_fraction"] == 0.85
    assert agents["codex"]["pacing"]["resets_available"] == 2
    assert len(agents["codex"]["pacing"]["reset_expiries"]) == 2
    assert all(a["pacing"]["measurement"] == "measured" for a in agents.values())
    assert "poll=2 hand file=1" in text and "Fable weekly usage higher" in text
    assert "weekly bucket Fable: u=0.91" in text
    for pool in ("codex", "claude"):
        assert f"POLLER {pool}: last ok ts=" in text
    assert "age=0.0" in text and "allowance=3 ticks/1800s" in text
    assert json.loads(pacing_usage.RESETS_FILE.read_text())["codex"] == [
        dict(expires=NOW + 1000000)
    ]
    rows = pacing_usage.read_rows(evidence / "pacing-usage-samples.ndjson")
    assert len(rows) == 2
    required = {
        "state",
        "used_fraction",
        "allowed_fraction",
        "period_end",
        "resets_available",
        "burn_per_day",
    }
    assert all(required <= a["pacing"].keys() for a in agents.values())


@pytest.mark.parametrize("pool,delta", [("codex", -1), ("codex", 1), ("claude", -1), ("claude", 1)])
def test_freshest_first_class_source(evidence, pool, delta):
    if pool == "codex":
        pacing_usage.CODEX_SESSIONS.mkdir()
        rollout(pacing_usage.CODEX_SESSIONS / "rollout-fixture.jsonl", NOW + delta)
    else:
        pacing_usage.CLAUDE_USAGE.write_text(
            json.dumps(dict(samples=[dict(t=(NOW + delta) * 1000, u=dict(sd=40, fh=30))]))
        )
    row = pacing_usage.usage(
        pool, NOW + 1, {}, snapshots=pacing_usage.SNAPSHOTS, claude_path=pacing_usage.CLAUDE_USAGE
    )
    assert row["used_fraction"] == (0.4 if delta > 0 else 0.14 if pool == "codex" else 0.85)
    assert ("rollout" in row["source"] or "desktop" in row["source"]) == (delta > 0)
    assert row["short_ts"] == NOW  # Independent poll short-window timestamp.


@pytest.mark.parametrize("age,fresh", [(1800, True), (1801, False)])
def test_three_tick_staleness_and_fallback_inventory(evidence, age, fresh):
    agents, text = integrate(evidence, now=NOW + age)
    assert agents["codex"]["pacing"]["resets_available"] == (2 if fresh else 1)
    assert agents["claude"]["pacing"]["measurement"] == ("measured" if fresh else "unknown")
    assert ("POLLER STALE: claude" in text) is not fresh
    if not fresh:
        assert "age=1801.0s" in text and "3 ticks/1800s" in text
        assert agents["claude"]["state"] == "ok"  # Native state retained.


@pytest.mark.parametrize("poll_state", ["absent", "stale", "invalid"])
def test_unknown_inventory_when_no_fallback(evidence, poll_state):
    pacing_usage.RESETS_FILE.unlink()
    path = pacing_usage.POLL_DIR / "codex-usage.json"
    if poll_state == "absent":
        path.unlink()
    elif poll_state == "invalid":
        path.write_text("{")
    agents, text = integrate(evidence, NOW + 1801 if poll_state == "stale" else NOW)
    assert agents["codex"]["pacing"]["resets_available"] is None
    assert "resets=unknown" in text


def test_credit_status_expiry_count_and_missing_inventory(evidence):
    row = json.loads((pacing_usage.POLL_DIR / "codex-usage.json").read_text())
    row["reset_credits"] += [
        dict(expires_at=NOW - 1, status="available"),
        dict(expires_at=NOW + 999, status="used"),
    ]
    row["reset_credits_available"] = 99  # Entries, not a summary field, define inventory.
    write_poll("codex", row)
    agents, _ = integrate(evidence)
    assert agents["codex"]["pacing"]["resets_available"] == 2
    row["reset_credits"] = None
    write_poll("codex", row)
    agents, _ = integrate(evidence)
    assert agents["codex"]["pacing"]["resets_available"] is None


def test_reset_epoch_jump_unchanged_credits_never_debits(evidence):
    path = pacing_usage.POLL_DIR / "codex-usage.json"
    old = json.loads(path.read_text())
    old["ts"] = NOW - 600
    old["primary"]["used_percent"] = 80
    old["primary"]["resets_at"] -= 3600
    pacing_usage.append_rows(pacing_usage.POLL_DIR / "codex-usage.ndjson", [old])
    before = pacing_usage.RESETS_FILE.read_bytes()
    datum = pacing_usage.usage("codex", NOW, {})
    assert datum["reset_signals"] == [NOW] and datum["reset_events"] == [NOW]
    agents, text = integrate(evidence, record=True)
    assert "auto-debit disabled" in text
    assert agents["codex"]["pacing"]["resets_available"] == 2
    assert pacing_usage.RESETS_FILE.read_bytes() == before


def test_no_guessed_claude_boundary(evidence):
    (pacing_usage.POLL_DIR / "claude-usage.json").unlink()
    pacing_usage.CLAUDE_USAGE.write_text(
        json.dumps(dict(samples=[dict(t=NOW * 1000, u=dict(sd=85))]))
    )
    agents, _ = integrate(evidence)
    assert agents["claude"]["pacing"]["period_end"] is None
    assert agents["claude"]["pacing"]["state"] == "unknown"
    assert pacing.period("claude", NOW) is None


@pytest.mark.parametrize("pool", ["codex", "claude"])
def test_poll_short_windows_clear_only_post_incident(evidence, monkeypatch, pool):
    monkeypatch.setattr(capacity.time, "time", lambda: NOW)
    monkeypatch.setattr(capacity, "SHED_DIR", evidence)
    marker = evidence / pool
    marker.write_text(json.dumps(dict(created_at=NOW - 1, expires_at=NOW + 3600, category="quota")))
    assert capacity._shed(pool) is False
    # A newer desktop/rollout weekly reading must not freshen older short-window data.
    marker.write_text(json.dumps(dict(created_at=NOW + 1, expires_at=NOW + 3600, category="quota")))
    monkeypatch.setattr(capacity.time, "time", lambda: NOW + 2)
    if pool == "claude":
        pacing_usage.CLAUDE_USAGE.write_text(
            json.dumps(dict(samples=[dict(t=(NOW + 2) * 1000, u=dict(sd=85, fh=1))]))
        )
    else:
        pacing_usage.CODEX_SESSIONS.mkdir()
        rollout(pacing_usage.CODEX_SESSIONS / "rollout-fixture.jsonl", NOW + 2)
    assert capacity._shed(pool) is True


def rollout(path, ts):
    path.write_text(
        json.dumps(
            dict(
                timestamp=ts,
                type="event_msg",
                payload=dict(
                    type="token_count",
                    rate_limits=dict(
                        primary=dict(used_percent=40, window_minutes=10080, resets_at=NOW + 600000)
                    ),
                ),
            )
        )
        + "\n"
    )


@pytest.mark.parametrize(
    "phase,error", [("stat", "vanished"), ("stat", "unreadable"), ("open", "vanished")]
)
def test_rollout_scan_race_survives(evidence, monkeypatch, phase, error):
    root = pacing_usage.CODEX_SESSIONS
    root.mkdir()
    keep, gone = root / "rollout-keep.jsonl", root / "rollout-gone.jsonl"
    rollout(keep, NOW)
    rollout(gone, NOW - 1)
    original_stat, original_open = Path.stat, Path.open

    def fail(path, *a, **k):
        if path == gone:
            if error == "vanished":
                gone.unlink(missing_ok=True)
                raise FileNotFoundError("injected rotation")
            raise PermissionError("injected unreadable")
        return (original_stat if phase == "stat" else original_open)(path, *a, **k)

    monkeypatch.setattr(Path, phase, fail)
    stats = {}
    rows = pacing_usage.rollout_observations(root, NOW, stats=stats)
    assert rows[-1]["ts"] == NOW
    assert stats["unreadable_files"] == 1
    assert stats["vanished_files"] == (error == "vanished")
    assert stats["total_files"] == 2


def test_pick_agent_real_build_survives_glob_stat_rotation(evidence, monkeypatch, capsys):
    root = pacing_usage.CODEX_SESSIONS
    root.mkdir()
    gone = root / "rollout-gone.jsonl"
    rollout(gone, NOW)
    original = Path.stat

    def stat(path, *a, **k):
        if path == gone:
            gone.unlink(missing_ok=True)
            raise FileNotFoundError("injected rotation")
        return original(path, *a, **k)

    monkeypatch.setattr(Path, "stat", stat)
    monkeypatch.setattr(capacity.time, "time", lambda: NOW)
    monkeypatch.setattr(capacity, "HANDOFF", evidence)
    monkeypatch.setattr(capacity, "compute", lambda *a: ("ok", "fixture", {}))
    monkeypatch.setattr(capacity, "_ccusage_active_block", lambda: {})
    monkeypatch.setattr(capacity, "_tier_models", lambda *a: {})
    monkeypatch.setattr(capacity, "_tier_ceiling", lambda *a: None)
    monkeypatch.setattr(capacity, "_auth_health", lambda *a: {})
    monkeypatch.setattr(capacity, "profile_capacity_snapshot", lambda *a, **k: {})
    monkeypatch.setattr(capacity, "profile_pool_usage_from_ledger", lambda: {})
    monkeypatch.setattr(router, "learned_ranks", lambda: {})
    assert capacity.main(["pick-agent", "--candidates", "codex,claude"]) == 0
    output = capsys.readouterr()
    assert output.out.strip() in ("codex", "claude")
    assert "vanished_files=1" in output.err


def test_coverage_counts_actual_poll_slots_once_for_both_passes(evidence):
    start = NOW - 1800
    for pool in ("codex", "claude"):
        row = json.loads((pacing_usage.POLL_DIR / f"{pool}-usage.json").read_text())
        for i in (0, 0, 1):  # Poll in force covers slot 2; duplicates add no coverage.
            pacing_usage.append_rows(
                pacing_usage.POLL_DIR / f"{pool}-usage.ndjson", [dict(row, ts=start + i * 600)]
            )
    assert pacing_usage.poll_coverage(start, NOW, now=NOW) == (3, 3)
    snapshots = [
        dict(
            ts=start + i * 600,
            event="pacing_snapshot",
            agents=dict(
                claude=dict(state="ok"),
                codex=dict(state="ok", pacing=dict(period_start=start, period_end=NOW)),
            ),
        )
        for i in range(3)
    ]
    pacing_usage.append_rows(evidence / "capacity-ledger.ndjson", snapshots)
    text = pacing_usage.report(evidence, now=NOW)
    for n in ("2:", "3:"):
        line = next(item for item in text.splitlines() if item.startswith(n))
        assert "coverage=3/3 ticks" in line
        assert "longest poll gap=1200s/1800s allowance" in line


def test_fresh_poll_blocks_false_hand_reset_marker_release(evidence, monkeypatch):
    monkeypatch.setattr(capacity.time, "time", lambda: NOW)
    monkeypatch.setattr(capacity, "SHED_DIR", evidence)
    pacing_usage.RESETS_FILE.write_text(
        json.dumps(dict(codex=[dict(expires=NOW + 10000, consumed_at=NOW - 10)]))
    )
    marker = evidence / "codex"
    marker.write_text(
        json.dumps(dict(created_at=NOW - 100, expires_at=NOW + 3600, category="quota"))
    )
    poll = json.loads((pacing_usage.POLL_DIR / "codex-usage.json").read_text())
    poll["secondary"] = None
    write_poll("codex", poll)
    assert capacity._shed("codex") is True
    assert marker.exists()


def test_stale_poll_prevents_redebiting_before_inventory_observation(evidence):
    bank = dict(codex=[dict(expires=NOW + 10000)])
    datum = dict(ts=NOW, reset_events=[NOW - 100], poll=dict(ts=NOW, resets=[], fresh=False))
    pacing_usage.consume_detected_resets(bank, datum, now=NOW + 1801, record=False)
    assert bank["codex"] == [dict(expires=NOW + 10000)]


def test_full_poll_coverage_without_capacity_baseline_cannot_pass(evidence):
    start = NOW - 600
    for pool in ("codex", "claude"):
        row = json.loads((pacing_usage.POLL_DIR / f"{pool}-usage.json").read_text())
        pacing_usage.append_rows(
            pacing_usage.POLL_DIR / f"{pool}-usage.ndjson", [dict(row, ts=start)]
        )
    snapshots = [
        dict(
            ts=NOW,
            event="pacing_snapshot",
            agents=dict(
                claude=dict(state="ok"),
                codex=dict(state="ok", pacing=dict(period_start=start, period_end=NOW)),
            ),
        )
    ]
    pacing_usage.append_rows(evidence / "capacity-ledger.ndjson", snapshots)
    (evidence / "rate-limit-incidents.ndjson").write_text("")
    text = pacing_usage.report(evidence, now=NOW)
    line = next(item for item in text.splitlines() if item.startswith("2:"))
    assert "unmeasured" in line and "coverage=0/1 ticks" in line


def test_fallback_snapshot_rotation_cannot_crash_usage(evidence, monkeypatch, capsys):
    for path in pacing_usage.POLL_DIR.glob("*"):
        path.unlink()
    pacing_usage.SNAPSHOTS.mkdir()
    gone = pacing_usage.SNAPSHOTS / "weekly-vanished.json"
    gone.write_text("{}")
    original = Path.stat

    def stat(path, *a, **k):
        if path == gone:
            gone.unlink(missing_ok=True)
            raise FileNotFoundError("injected rotation")
        return original(path, *a, **k)

    monkeypatch.setattr(Path, "stat", stat)
    row = pacing_usage.usage("codex", NOW, {}, snapshots=pacing_usage.SNAPSHOTS)
    assert row["used_fraction"] is None
    assert "unreadable_files=1" in capsys.readouterr().err


def test_coverage_650_second_cadence_with_jitter_is_full(evidence):
    import random

    rng = random.Random(703)
    start = NOW - pacing.PERIOD_SECONDS
    for pool in ("codex", "claude"):
        row = json.loads((pacing_usage.POLL_DIR / f"{pool}-usage.json").read_text())
        times = [start - 100]
        while times[-1] < NOW - 660:
            times.append(times[-1] + 650 + rng.randint(-15, 15))
        pacing_usage.append_rows(
            pacing_usage.POLL_DIR / f"{pool}-usage.ndjson", [dict(row, ts=ts) for ts in times]
        )
    stats = {}
    assert pacing_usage.poll_coverage(start, NOW, now=NOW, stats=stats) == (1008, 1008)
    assert 635 <= stats["longest_gap"] <= 665


def test_coverage_1900_second_gap_misses_slot(evidence):
    start = NOW - 2400
    for pool in ("codex", "claude"):
        row = json.loads((pacing_usage.POLL_DIR / f"{pool}-usage.json").read_text())
        pacing_usage.append_rows(
            pacing_usage.POLL_DIR / f"{pool}-usage.ndjson",
            [dict(row, ts=start - 50), dict(row, ts=start + 1850)],
        )
    stats = {}
    assert pacing_usage.poll_coverage(start, NOW, now=NOW, stats=stats) == (3, 4)
    assert stats["longest_gap"] == 1900


@pytest.mark.parametrize("phase", [0, 50, 300, 550])
def test_coverage_gap_between_slot_ticks_is_uncovered(evidence, phase):
    start = NOW - 3000
    for pool in ("codex", "claude"):
        row = json.loads((pacing_usage.POLL_DIR / f"{pool}-usage.json").read_text())
        pacing_usage.append_rows(
            pacing_usage.POLL_DIR / f"{pool}-usage.ndjson",
            [dict(row, ts=ts) for ts in (start, start + phase, start + phase + 1900, NOW)],
        )
    snapshots = [
        dict(
            ts=start + i * 600,
            event="pacing_snapshot",
            agents=dict(
                claude=dict(state="ok"),
                codex=dict(state="ok", pacing=dict(period_start=start, period_end=NOW)),
            ),
        )
        for i in range(5)
    ]
    stats = {}
    assert pacing_usage.poll_coverage(start, NOW, now=NOW, snapshots=snapshots, stats=stats) == (
        4,
        5,
    )
    assert stats["longest_gap"] == 1900
    pacing_usage.append_rows(evidence / "capacity-ledger.ndjson", snapshots)
    (evidence / "rate-limit-incidents.ndjson").write_text("")
    for line in pacing_usage.report(evidence, now=NOW).splitlines():
        if line.startswith(("2:", "3:")):
            assert "unmeasured" in line and "PASS" not in line
            assert "coverage=4/5 ticks" in line
            assert "longest poll gap=1900s/1800s allowance; uncovered=1 slots" in line


@pytest.mark.parametrize("first", ["hang", "cost"])
def test_claude_retries_with_process_group_cleanup(tmp_path, pollers, monkeypatch, first):
    module = pollers["claude"]
    calls, killed, sleeps = [], [], []

    class Process:
        pid = 703
        returncode = 0

        def __init__(self, *args, **kwargs):
            calls.append((args, kwargs))
            self.attempt = len(calls)
            self.communications = 0

        def communicate(self, timeout=None):
            self.communications += 1
            if self.attempt == 1:
                if first == "hang" and self.communications == 1:
                    assert timeout == 40
                    raise subprocess.TimeoutExpired("fixture", timeout)
                return "Total cost: fixture", ""
            return CLAUDE_TEXT, ""

    monkeypatch.delenv("CLAUDE_USAGE_POLL_TIMEOUT", raising=False)
    monkeypatch.setattr(module.shutil, "which", lambda *a, **k: "/fixture/claude")
    monkeypatch.setattr(module.subprocess, "Popen", Process)
    monkeypatch.setattr(module.os, "killpg", lambda *args: killed.append(args))
    monkeypatch.setattr(module.time, "sleep", sleeps.append)
    module.main(tmp_path)
    assert len(calls) == 2 and sleeps == [3]
    assert killed == ([(703, module.signal.SIGKILL)] if first == "hang" else [])
    for args, kwargs in calls:
        assert args == (["/fixture/claude", "-p", "/usage"],)
        assert kwargs["start_new_session"] is True
        assert kwargs["stdin"] == subprocess.DEVNULL and kwargs["cwd"] == "/tmp"
    assert json.loads((tmp_path / "claude-usage.json").read_text())["weekly"]["used_percent"] == 85


def test_claude_all_retries_fail_preserves_latest_and_lists_every_try(
    tmp_path, pollers, monkeypatch
):
    module = pollers["claude"]
    latest = tmp_path / "claude-usage.json"
    latest.write_text('{"ts":1}')
    before = latest.stat().st_mtime_ns
    responses = iter(["hang", "cost", "exit"])
    sleeps = []
    killed = []

    class Process:
        pid = 703

        def __init__(self, *args, **kwargs):
            self.mode = next(responses)
            self.returncode = 1 if self.mode == "exit" else 0

        def communicate(self, timeout=None):
            if self.mode == "hang" and timeout is not None:
                raise subprocess.TimeoutExpired("fixture", timeout)
            return "Total cost: DO_NOT_LOG", "DO_NOT_LOG"

    monkeypatch.setattr(module.shutil, "which", lambda *a, **k: "/fixture/claude")
    monkeypatch.setattr(module.subprocess, "Popen", Process)
    monkeypatch.setattr(module.os, "killpg", lambda *a: killed.append(a))
    monkeypatch.setattr(module.time, "sleep", sleeps.append)
    with pytest.raises(RuntimeError) as error:
        module.main(tmp_path)
    assert str(error.value) == "try1: timeout 40s; try2: no weekly line; try3: exit 1"
    assert sleeps == [3, 3] and killed == [(703, module.signal.SIGKILL)]
    assert latest.read_text() == '{"ts":1}' and latest.stat().st_mtime_ns == before
    assert not (tmp_path / "claude-usage.ndjson").exists()


@pytest.mark.parametrize("field", ["secondary", "session", "model"])
def test_informational_fields_are_best_effort(pollers, field):
    if field == "secondary":
        response = copy.deepcopy(CODEX_RESPONSE)
        response["rateLimits"]["secondary"] = {"usedPercent": "invalid"}
        row = pollers["codex"].parse_response(response, NOW)
        assert row["secondary"] is None and row["primary"]["used_percent"] == 14
        response["rateLimits"]["primary"] = {"usedPercent": "invalid"}
        with pytest.raises(ValueError):
            pollers["codex"].parse_response(response, NOW)
    else:
        text = CLAUDE_TEXT.replace(
            (
                "Oct 17 at 4:40pm"
                if field == "session"
                else "(Fable): 91% used · resets Oct 18 at 12am"
            ),
            "not a time" if field == "session" else "(Fable): 91% used · resets not a time",
        )
        row = pollers["claude"].parse_usage(text, dt.datetime.fromtimestamp(NOW, dt.timezone.utc))
        assert row["weekly"]["used_percent"] == 85
        assert (row["session"] is None) if field == "session" else (row["buckets"][2] is None)
        with pytest.raises(ValueError):
            pollers["claude"].parse_usage(
                text.replace(
                    "(all models): 85% used · resets Oct 18 at 12am",
                    "(all models): 85% used · resets invalid",
                ),
                dt.datetime.fromtimestamp(NOW, dt.timezone.utc),
            )
    assert len(row["warnings"]) == 1


@pytest.mark.parametrize("weeks", [0, 2])
@pytest.mark.parametrize("desktop", ["absent", "old", "fresh"])
def test_claude_boundary_rolls_without_reusing_old_percentage(evidence, weeks, desktop):
    now = END + weeks * pacing.PERIOD_SECONDS + 1
    if desktop != "absent":
        ts = now if desktop == "fresh" else END - 1
        pacing_usage.CLAUDE_USAGE.write_text(
            json.dumps(dict(samples=[dict(t=ts * 1000, u=dict(sd=4, fh=2))]))
        )
    datum = pacing_usage.usage("claude", now, {}, claude_path=pacing_usage.CLAUDE_USAGE)
    assert datum["period_end"] == END + (weeks + 1) * pacing.PERIOD_SECONDS
    assert datum["used_fraction"] == (0.04 if desktop == "fresh" else None)
    agents, text = integrate(evidence, now=now)
    row = agents["claude"]["pacing"]
    assert row["period_end"] == datum["period_end"]
    assert row["measurement"] == ("measured" if desktop == "fresh" else "unknown")
    assert f"old T1={END}" in text and f"rolled T1={datum['period_end']}" in text
    assert f"poll age={now - NOW}s" in text


def test_hot_usage_reads_bounded_history_tail(evidence, monkeypatch, capsys):
    path = pacing_usage.POLL_DIR / "codex-usage.ndjson"
    row = json.loads((pacing_usage.POLL_DIR / "codex-usage.json").read_text())
    old = copy.deepcopy(row)
    old["ts"] = NOW - 600
    old["primary"].update(used_percent=80, resets_at=row["primary"]["resets_at"] - 3600)
    path.write_text((json.dumps(dict(ts=1)) + "\n") * 1000 + json.dumps(old) + "\n")
    monkeypatch.setattr(pacing_usage, "POLL_HISTORY_TAIL_BYTES", 1024)
    original_open = Path.open
    reads = []

    class BoundedFile:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.stream.close()

        def __getattr__(self, name):
            return getattr(self.stream, name)

        def read(self, size=-1):
            assert 0 <= size <= 1024
            reads.append(size)
            return self.stream.read(size)

    def opened(p, *args, **kwargs):
        stream = original_open(p, *args, **kwargs)
        return BoundedFile(stream) if p == path else stream

    monkeypatch.setattr(Path, "open", opened)
    datum = pacing_usage.usage("codex", NOW, {})
    assert reads == [1024] and datum["reset_events"] == [NOW]
    assert "older_bytes_omitted=" in capsys.readouterr().err


@pytest.mark.parametrize("pool", ["codex", "claude"])
def test_history_age_and_size_rotation_is_reported(tmp_path, pollers, pool, monkeypatch, capsys):
    monkeypatch.setitem(pollers[pool].append_history.__globals__, "HISTORY_MAX_LINES", 3)
    path = tmp_path / f"{pool}-usage.ndjson"
    path.write_text(
        "\n".join(json.dumps(dict(ts=ts)) for ts in [1, NOW - 4, NOW - 3, NOW - 2, NOW - 1]) + "\n"
    )
    pollers[pool].publish(dict(ts=NOW), tmp_path)
    assert pacing_usage.read_rows(path) == [dict(ts=NOW - 2), dict(ts=NOW - 1), dict(ts=NOW)]
    assert "rotated_out=3 retained=3 lines" in capsys.readouterr().err
    assert json.loads((tmp_path / f"{pool}-usage.json").read_text()) == dict(ts=NOW)


def test_launchd_template_keeps_unthrottled_process_type():
    import plistlib

    template = (
        Path(__file__).resolve().parents[1] / "scripts/usage_poll/com.stranske.usage-poll.plist"
    )
    data = plistlib.loads(template.read_bytes())
    assert "ProcessType" not in data and "LowPriorityIO" not in data


def test_post_boundary_poll_with_expired_window_cannot_reuse_percentage(evidence):
    row = json.loads((pacing_usage.POLL_DIR / "claude-usage.json").read_text())
    row["ts"] = END + 1
    write_poll("claude", row)
    datum = pacing_usage.usage("claude", END + 1, {})
    assert datum["period_end"] == END + pacing.PERIOD_SECONDS
    assert datum["used_fraction"] is None and datum["poll"]["fresh"] is False


def test_history_byte_cap_and_two_period_anchor(tmp_path, pollers, monkeypatch, capsys):
    path = tmp_path / "codex-usage.ndjson"
    anchor = dict(ts=NOW - 2 * pacing.PERIOD_SECONDS - pacing.STALE_AFTER_SECONDS)
    path.write_text(json.dumps(anchor) + "\n")
    pollers["codex"].publish(dict(ts=NOW), tmp_path)
    assert pacing_usage.read_rows(path) == [anchor, dict(ts=NOW)]
    capsys.readouterr()
    monkeypatch.setitem(
        pollers["codex"].append_history.__globals__,
        "HISTORY_MAX_BYTES",
        len(json.dumps(dict(ts=NOW))) + 1,
    )
    pollers["codex"].publish(dict(ts=NOW + 1), tmp_path)
    assert pacing_usage.read_rows(path) == [dict(ts=NOW + 1)]
    assert "rotated_out=2 retained=1 lines" in capsys.readouterr().err


def test_history_retains_two_periods_plus_stale_allowance(pollers):
    history = pollers["codex"].append_history.__globals__
    assert history["HISTORY_SECONDS"] >= 2 * pacing.PERIOD_SECONDS + pacing.STALE_AFTER_SECONDS
    assert history["HISTORY_MAX_LINES"] == 4096
    assert history["HISTORY_MAX_BYTES"] == 8 * 1024 * 1024


def test_appended_history_covers_period_ended_one_day_earlier(evidence, pollers):
    end = NOW - 86400
    start = end - pacing.PERIOD_SECONDS
    for pool in ("codex", "claude"):
        row = json.loads((pacing_usage.POLL_DIR / f"{pool}-usage.json").read_text())
        path = pacing_usage.POLL_DIR / f"{pool}-usage.ndjson"
        for ts in range(int(NOW - 2 * pacing.PERIOD_SECONDS), int(NOW) + 1, 600):
            pollers[pool].append_history(path, dict(row, ts=ts))
    stats = {}
    assert pacing_usage.poll_coverage(start, end, now=NOW, stats=stats) == (1008, 1008)
    assert stats["longest_gap"] == 600
