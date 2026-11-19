import json

import capacity
import pacing
import pacing_usage

NOW = 1_800_000_000.0


def _snapshot(ts, pool, **pacing_fields):
    return {"event": "pacing_snapshot", "ts": ts, "agents": {pool: {"pacing": pacing_fields}}}


def _write(handoff, rows):
    handoff.mkdir(parents=True, exist_ok=True)
    (handoff / "capacity-ledger.ndjson").write_text("\n".join(json.dumps(r) for r in rows) + "\n")


def test_usage_in_band_is_the_share_of_measured_pools(tmp_path):
    reserve = 0.15
    lo, hi = pacing.pass_bounds(reserve)
    end = NOW - 60
    rows = [
        _snapshot(
            end - 30,
            "claude",
            period_end=end,
            used_fraction=(lo + hi) / 2,
            effective_reserve=reserve,
            stale=False,
        ),
        _snapshot(
            end - 30,
            "codex",
            period_end=end,
            used_fraction=0.99,
            effective_reserve=reserve,
            stale=False,
        ),
    ]
    _write(tmp_path, rows)
    usage = pacing_usage.pass_numbers(tmp_path, NOW)["usage_in_target_band"]
    assert usage["value"] == 0.5
    assert "claude" in usage["reason"] and "codex" in usage["reason"]


def test_unmeasured_pass_numbers_are_none_never_zero(tmp_path):
    _write(tmp_path, [])
    result = pacing_usage.pass_numbers(tmp_path, NOW)
    assert result["usage_in_target_band"]["value"] is None
    assert result["quota_incidents_while_other_ok"]["value"] is None
    assert result["non_codex_share_at_least_double"]["value"] is None
    assert result["quota_incidents_while_other_ok"]["reason"].startswith("2: unmeasured")


def test_pacing_report_is_read_only(tmp_path, monkeypatch):
    monkeypatch.setattr(capacity, "HANDOFF", tmp_path)
    _write(tmp_path, [])
    before = sorted(p.name for p in tmp_path.iterdir())
    data = capacity.pacing_report(NOW)
    assert set(data) == {
        "usage_in_target_band",
        "quota_incidents_while_other_ok",
        "non_codex_share_at_least_double",
    }
    assert sorted(p.name for p in tmp_path.iterdir()) == before


def test_text_report_lines_come_from_the_same_computation(tmp_path):
    _write(tmp_path, [])
    text = pacing_usage.report(tmp_path, now=NOW, current={}, previous={})
    for line in pacing_usage.period_results(tmp_path, [], NOW)["lines"]:
        assert line in text


def test_one_pool_in_band_and_one_unmeasured_reads_half(tmp_path):
    reserve = 0.15
    lo, hi = pacing.pass_bounds(reserve)
    end = NOW - 60
    _write(
        tmp_path,
        [
            _snapshot(
                end - 30,
                "claude",
                period_end=end,
                used_fraction=(lo + hi) / 2,
                effective_reserve=reserve,
                stale=False,
            )
        ],
    )
    usage = pacing_usage.pass_numbers(tmp_path, NOW)["usage_in_target_band"]
    assert usage["value"] == 0.5
    assert "codex unmeasured" in usage["reason"]


def _poll_row(pool, ts, used_percent, resets_at):
    window = {"used_percent": used_percent, "resets_at": resets_at}
    if pool == "codex":
        return {"ts": ts, "primary": {**window, "window_minutes": 10080}}
    return {"ts": ts, "weekly": {**window, "label": "All models"}}


def _judging_world(tmp_path, monkeypatch, codex_polls, incidents, codex_state="shed"):
    """A completed 4-hour period; claude polled every 10 min; codex polls as given; one snapshot per tick."""
    polls = tmp_path / "polls"
    polls.mkdir()
    monkeypatch.setattr(pacing_usage, "POLL_DIR", polls)
    start, end = NOW - 5 * 3600, NOW - 3600
    tick = pacing.POLL_TICK_SECONDS
    claude = [_poll_row("claude", t, 40, NOW + 86400) for t in range(int(start), int(end), tick)]
    (polls / "claude-usage.ndjson").write_text("\n".join(json.dumps(r) for r in claude) + "\n")
    (polls / "codex-usage.ndjson").write_text(
        "\n".join(json.dumps(_poll_row("codex", *p)) for p in codex_polls) + "\n"
    )
    snaps = [
        {
            "event": "pacing_snapshot",
            "ts": t,
            "agents": {
                "claude": {
                    "state": "warn",
                    "pacing": {"period_start": start, "period_end": end},
                },
                "codex": {"state": codex_state},
            },
        }
        for t in range(int(start), int(end), tick)
    ]
    _write(tmp_path, snaps)
    (tmp_path / "rate-limit-incidents.ndjson").write_text(
        "".join(json.dumps({"ts": t, "agent": "claude"}) + "\n" for t in incidents)
    )
    return start, end


def test_an_incident_while_the_other_pool_is_exhausted_is_judged_through_its_poll_gap(
    tmp_path, monkeypatch
):
    """Owner 2026-11-18: an exhausted account is often closed until it resets, so its poll stops.
    The last poll said 'at capacity until reset', so the gap hides nothing."""
    start = NOW - 5 * 3600
    codex = [(start + 60, 100, NOW + 86400)]  # exhausted, then no poll for the rest of the period
    _judging_world(tmp_path, monkeypatch, codex, incidents=[start + 2 * 3600])
    result = pacing_usage.pass_numbers(tmp_path, NOW)["quota_incidents_while_other_ok"]
    assert result["value"] == 0
    assert (
        result["reason"].count("incidents judged 1/1 (other pool carried as exhausted in 1)") == 1
    )


def test_an_incident_in_a_gap_while_the_other_pool_was_not_exhausted_stays_unmeasured(
    tmp_path, monkeypatch
):
    """11-09: the codex poll failed for 22 h at 63% used; an incident there cannot be judged."""
    start = NOW - 5 * 3600
    codex = [(start + 60, 63, NOW + 86400)]
    _judging_world(tmp_path, monkeypatch, codex, incidents=[start + 2 * 3600])
    result = pacing_usage.pass_numbers(tmp_path, NOW)["quota_incidents_while_other_ok"]
    assert result["value"] is None
    assert result["reason"].count("1 incident(s) fell where another pool's state is unknown") == 1


def test_an_incident_while_the_other_pool_is_measured_ok_is_a_violation(tmp_path, monkeypatch):
    start, end = NOW - 5 * 3600, NOW - 3600
    tick = pacing.POLL_TICK_SECONDS
    codex = [(t, 30, NOW + 86400) for t in range(int(start), int(end), tick)]
    _judging_world(
        tmp_path, monkeypatch, codex, incidents=[start + 2 * 3600 + 30], codex_state="ok"
    )
    result = pacing_usage.pass_numbers(tmp_path, NOW)["quota_incidents_while_other_ok"]
    assert result["value"] == 1


def test_a_period_with_gaps_but_no_incidents_is_measured_zero(tmp_path, monkeypatch):
    start = NOW - 5 * 3600
    _judging_world(tmp_path, monkeypatch, [(start + 60, 63, NOW + 86400)], incidents=[])
    result = pacing_usage.pass_numbers(tmp_path, NOW)["quota_incidents_while_other_ok"]
    assert result["value"] == 0


def test_judged_violations_fail_the_zero_target_even_with_unjudged_incidents(tmp_path, monkeypatch):
    """A zero target fails on the first judged violation; unjudged incidents cannot rescue it."""
    start = NOW - 5 * 3600
    tick = pacing.POLL_TICK_SECONDS
    codex = [(t, 30, NOW + 86400) for t in range(int(start), int(start + 7200), tick)]
    _judging_world(
        tmp_path, monkeypatch, codex, incidents=[start + 1830, start + 3 * 3600], codex_state="ok"
    )
    result = pacing_usage.pass_numbers(tmp_path, NOW)["quota_incidents_while_other_ok"]
    assert result["value"] == 1
    assert (
        result["reason"].count("2: at least 1 incidents while another pool ok (1 more could not")
        == 1
    )
