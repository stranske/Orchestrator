"""Tests for the route_decision function in comparative routing.

Owner 2026-11-11: the incident test is a ROLLBACK after go-live (the 2026-10-17 rule "revert on a 429
rise"), not a precondition. The baseline is the 14 days before go-live; incidents since go-live are
compared with baseline rate x elapsed days (at least one day).
"""

import datetime as dt
import json

import rate_incidents
from comparative import (
    OPTIMAL_HOLD_SECONDS,
    ROUTE_GO_LIVE,
    ROUTE_INCIDENT_WINDOW_DAYS,
    hold_last_optimal,
    route_decision,
)

GO_LIVE = int(dt.datetime.fromisoformat(ROUTE_GO_LIVE).timestamp())
DAY = 86400
OPTIMAL = {"status": "optimal", "reasons": []}


def _baseline(n):
    """n incidents spread over the 14 days before go-live."""
    step = ROUTE_INCIDENT_WINDOW_DAYS * DAY // max(n, 1)
    return [{"ts": GO_LIVE - ROUTE_INCIDENT_WINDOW_DAYS * DAY + i * step} for i in range(n)]


def test_route_enabled_only_after_go_live_and_optimal():
    decision = route_decision(OPTIMAL, incidents_rows=[], now=GO_LIVE - DAY)
    assert decision["route_enabled"] is False
    assert "before go-live" in decision["route_reason"]
    for status in ["inputs_unmeasured", "partial", "not_testable_slack"]:
        decision = route_decision({"status": status, "reasons": []}, incidents_rows=[], now=GO_LIVE)
        assert decision["route_enabled"] is False
        assert "status not optimal" in decision["route_reason"]


def test_fleet_incidents_rising_before_go_live_do_not_block_it():
    """The live 2026-11-11 shape: 67 in the 14 days before go-live vs 46 in the 14 before that.

    The old precondition read that as "incidents rose" and held the flag off although routing had
    not changed; nothing routing did could clear it.
    """
    older = [{"ts": GO_LIVE - 2 * ROUTE_INCIDENT_WINDOW_DAYS * DAY + i * 3600} for i in range(46)]
    decision = route_decision(OPTIMAL, incidents_rows=older + _baseline(67), now=GO_LIVE + 60)
    assert decision["route_enabled"] is True, decision
    assert decision["incidents_prior"] == 67
    assert decision["incidents_recent"] == 0


def test_rise_since_go_live_reverts():
    since = [{"ts": GO_LIVE + 3600 * i} for i in range(3)]
    decision = route_decision(OPTIMAL, incidents_rows=_baseline(14) + since, now=GO_LIVE + DAY)
    assert decision["route_enabled"] is False
    assert decision["route_reason"].startswith("revert: incidents since go-live 3 vs expected 1.0")
    assert (decision["incidents_recent"], decision["incidents_prior"]) == (3, 14)
    assert decision["incidents_expected"] == 1.0


def test_first_hour_is_measured_against_a_full_day():
    since = [{"ts": GO_LIVE + 600}]
    decision = route_decision(OPTIMAL, incidents_rows=_baseline(14) + since, now=GO_LIVE + 3600)
    assert decision["route_enabled"] is True, decision


def test_a_revert_clears_while_closed_as_the_rate_falls_back():
    """Latched-gate check: the drain (elapsed time at a normal rate) runs while the flag is off."""
    rows = _baseline(14) + [{"ts": GO_LIVE + 3600 * i} for i in range(4)]
    assert route_decision(OPTIMAL, incidents_rows=rows, now=GO_LIVE + DAY)["route_enabled"] is False
    later = route_decision(OPTIMAL, incidents_rows=rows, now=GO_LIVE + 5 * DAY)
    assert later["route_enabled"] is True
    assert "incidents since go-live 4 vs expected 5.0" in later["route_reason"]


def test_after_the_window_the_comparison_slides_but_the_baseline_stays_before_go_live():
    now = GO_LIVE + 20 * DAY
    early = [{"ts": GO_LIVE + DAY + i} for i in range(50)]  # older than the last 14 days
    recent = [{"ts": now - DAY + i} for i in range(10)]
    decision = route_decision(OPTIMAL, incidents_rows=_baseline(14) + early + recent, now=now)
    assert decision["incidents_recent"] == 10
    assert decision["incidents_expected"] == 14.0
    assert decision["route_enabled"] is True


def test_no_incidents_anywhere_is_enabled_and_says_so():
    decision = route_decision(OPTIMAL, incidents_rows=[], now=GO_LIVE + DAY)
    assert decision["route_enabled"] is True
    assert decision["route_reason"] == (
        "enabled; incidents since go-live 0 vs expected 0.0 (baseline 0 over the 14d before go-live)"
    )


def test_iso_and_malformed_timestamps():
    iso = dt.datetime.fromtimestamp(GO_LIVE + 600, dt.timezone.utc).isoformat()
    rows = _baseline(28) + [{"ts": iso}, {"ts": "not a time"}, {"ts": None}, {}]
    decision = route_decision(OPTIMAL, incidents_rows=rows, now=GO_LIVE + DAY)
    assert decision["incidents_recent"] == 1
    assert decision["incidents_prior"] == 28
    assert decision["route_enabled"] is True


def test_unreadable_ledger_fails_to_absolute(tmp_path):
    """Route is disabled when incident ledger is unreadable."""
    go_live_ts = int(dt.datetime.fromisoformat(ROUTE_GO_LIVE).timestamp())
    now = go_live_ts + 86400  # One day after go-live

    result = {"status": "optimal", "reasons": []}

    # Save original INCIDENT_FILE
    original_incident_file = rate_incidents.INCIDENT_FILE

    try:
        # Test case 1: missing file
        missing_path = tmp_path / "nonexistent_incidents.ndjson"
        rate_incidents.INCIDENT_FILE = missing_path
        decision = route_decision(result, incidents_rows=None, now=now)
        assert decision["route_enabled"] is False
        assert "incident ledger unreadable" in decision["route_reason"]
        assert decision["incidents_recent"] is None
        assert decision["incidents_prior"] is None

        # Test case 2: unreadable path (a directory)
        dir_path = tmp_path / "incidents_dir"
        dir_path.mkdir()
        rate_incidents.INCIDENT_FILE = dir_path
        decision = route_decision(result, incidents_rows=None, now=now)
        assert decision["route_enabled"] is False
        assert "incident ledger unreadable" in decision["route_reason"]
        assert decision["incidents_recent"] is None
        assert decision["incidents_prior"] is None
    finally:
        # Restore original INCIDENT_FILE
        rate_incidents.INCIDENT_FILE = original_incident_file


def test_hold_saves_optimal_and_holds_partial_for_a_day(tmp_path):
    path = tmp_path / "comparative-last-optimal.json"
    optimal = {"status": "optimal", "reasons": [], "lambda": {"claude": 1.0}, "ranks": {"x": []}}
    assert hold_last_optimal(dict(optimal), path, now=1000) == optimal
    assert json.loads(path.read_text())["saved_at"] == 1000

    partial = {
        "status": "partial",
        "reasons": ["codex: burn unmeasured"],
        "lambda": {"claude": 2.0},
    }
    held = hold_last_optimal(partial, path, now=1000 + 3600)
    assert held["status"] == "optimal"
    assert held["lambda"] == {"claude": 1.0}
    assert (held["held_from"], held["held_age_seconds"]) == (1000, 3600)
    assert held["held_because"] == ["codex: burn unmeasured"]
    assert route_decision(held, incidents_rows=[], now=GO_LIVE + DAY)["route_enabled"] is True


def test_hold_expires_and_never_covers_real_data_loss(tmp_path):
    path = tmp_path / "comparative-last-optimal.json"
    hold_last_optimal({"status": "optimal", "reasons": []}, path, now=1000)
    stale = hold_last_optimal(
        {"status": "partial", "reasons": []}, path, now=1000 + OPTIMAL_HOLD_SECONDS + 1
    )
    assert stale["status"] == "partial"
    assert "hold limit" in stale["hold_reason"]
    lost = hold_last_optimal({"status": "inputs_unmeasured", "reasons": []}, path, now=1001)
    assert lost["status"] == "inputs_unmeasured" and "held_from" not in lost
    nothing = hold_last_optimal(
        {"status": "partial", "reasons": []}, tmp_path / "absent.json", now=1
    )
    assert nothing["hold_reason"] == "no saved optimal result to hold"
