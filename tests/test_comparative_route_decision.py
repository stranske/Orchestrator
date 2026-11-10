"""Tests for the route_decision function in comparative routing."""

import datetime as dt
import pytest
from unittest.mock import patch

from comparative import ROUTE_GO_LIVE, ROUTE_INCIDENT_WINDOW_DAYS, route_decision
from pacing_usage import read_rows


class TestRouteDecision:
    """Test suite for route_decision function."""

    def test_route_enabled_only_after_go_live_and_optimal(self):
        """Route is disabled before go-live or when status is not optimal."""
        # Before go-live
        go_live_ts = int(dt.datetime.fromisoformat(ROUTE_GO_LIVE).timestamp())
        before_go_live = go_live_ts - 86400  # One day before
        
        result = {"status": "optimal", "reasons": []}
        decision = route_decision(result, incidents_rows=[], now=before_go_live)
        assert decision["route_enabled"] is False
        assert "before go-live" in decision["route_reason"]
        
        # At go-live but status not optimal
        at_go_live = go_live_ts
        for non_optimal_status in ["inputs_unmeasured", "partial", "not_testable_slack"]:
            result = {"status": non_optimal_status, "reasons": []}
            decision = route_decision(result, incidents_rows=[], now=at_go_live)
            assert decision["route_enabled"] is False
            assert "status not optimal" in decision["route_reason"]

    def test_incident_rise_reverts_to_absolute_routing(self):
        """Route is disabled when recent incidents exceed prior incidents."""
        go_live_ts = int(dt.datetime.fromisoformat(ROUTE_GO_LIVE).timestamp())
        now = go_live_ts + 86400  # One day after go-live
        
        result = {"status": "optimal", "reasons": []}
        
        # Create incidents: more recent than prior
        window_seconds = ROUTE_INCIDENT_WINDOW_DAYS * 86400
        prior_start = now - 2 * window_seconds
        recent_start = now - window_seconds
        
        # 5 incidents in prior period, 10 in recent period (should revert)
        incidents_rows = [
            {"ts": prior_start + 86400 * i} for i in range(5)  # 5 in prior
        ] + [
            {"ts": recent_start + 86400 * i} for i in range(10)  # 10 in recent
        ]
        
        decision = route_decision(result, incidents_rows=incidents_rows, now=now)
        assert decision["route_enabled"] is False
        assert "revert: incidents rose" in decision["route_reason"]
        assert decision["incidents_prior"] == 5
        assert decision["incidents_recent"] == 10

    def test_unreadable_ledger_fails_to_absolute(self):
        """Route is disabled when incident ledger is unreadable."""
        go_live_ts = int(dt.datetime.fromisoformat(ROUTE_GO_LIVE).timestamp())
        now = go_live_ts + 86400  # One day after go-live
        
        result = {"status": "optimal", "reasons": []}
        
        # Mock read_rows to return None (simulating unreadable ledger)
        with patch("pacing_usage.read_rows") as mock_read_rows:
            mock_read_rows.return_value = None
            decision = route_decision(result, incidents_rows=None, now=now)
            assert decision["route_enabled"] is False
            assert "incident ledger unreadable" in decision["route_reason"]
            assert decision["incidents_recent"] is None
            assert decision["incidents_prior"] is None

    def test_route_enabled_when_conditions_met(self):
        """Route is enabled when all conditions are met."""
        go_live_ts = int(dt.datetime.fromisoformat(ROUTE_GO_LIVE).timestamp())
        now = go_live_ts + 86400  # One day after go-live
        
        result = {"status": "optimal", "reasons": []}
        
        # Create incidents: fewer recent than prior (or equal)
        window_seconds = ROUTE_INCIDENT_WINDOW_DAYS * 86400
        prior_start = now - 2 * window_seconds
        recent_start = now - window_seconds
        
        # 10 incidents in prior period, 5 in recent period (should enable)
        incidents_rows = [
            {"ts": prior_start + 86400 * i} for i in range(10)  # 10 in prior
        ] + [
            {"ts": recent_start + 86400 * i} for i in range(5)  # 5 in recent
        ]
        
        decision = route_decision(result, incidents_rows=incidents_rows, now=now)
        assert decision["route_enabled"] is True
        assert "enabled; incidents" in decision["route_reason"]
        assert decision["incidents_prior"] == 10
        assert decision["incidents_recent"] == 5

    def test_route_enabled_with_equal_incidents(self):
        """Route is enabled when recent incidents equal prior incidents."""
        go_live_ts = int(dt.datetime.fromisoformat(ROUTE_GO_LIVE).timestamp())
        now = go_live_ts + 86400
        
        result = {"status": "optimal", "reasons": []}
        
        window_seconds = ROUTE_INCIDENT_WINDOW_DAYS * 86400
        prior_start = now - 2 * window_seconds
        recent_start = now - window_seconds
        
        # Equal incidents in both periods
        incidents_rows = [
            {"ts": prior_start + 86400 * i} for i in range(5)
        ] + [
            {"ts": recent_start + 86400 * i} for i in range(5)
        ]
        
        decision = route_decision(result, incidents_rows=incidents_rows, now=now)
        assert decision["route_enabled"] is True
        assert decision["incidents_prior"] == 5
        assert decision["incidents_recent"] == 5

    def test_route_enabled_with_no_incidents(self):
        """Route is enabled when there are no incidents."""
        go_live_ts = int(dt.datetime.fromisoformat(ROUTE_GO_LIVE).timestamp())
        now = go_live_ts + 86400
        
        result = {"status": "optimal", "reasons": []}
        
        decision = route_decision(result, incidents_rows=[], now=now)
        assert decision["route_enabled"] is True
        assert decision["incidents_prior"] == 0
        assert decision["incidents_recent"] == 0

    def test_route_decision_handles_iso_timestamps(self):
        """Route decision handles ISO format timestamps in incident rows."""
        go_live_ts = int(dt.datetime.fromisoformat(ROUTE_GO_LIVE).timestamp())
        now = go_live_ts + 86400
        
        result = {"status": "optimal", "reasons": []}
        
        # Use ISO timestamps
        now_dt = dt.datetime.fromtimestamp(now, dt.timezone.utc)
        window_seconds = ROUTE_INCIDENT_WINDOW_DAYS * 86400
        prior_start_dt = now_dt - dt.timedelta(seconds=2 * window_seconds)
        recent_start_dt = now_dt - dt.timedelta(seconds=window_seconds)
        
        incidents_rows = [
            {"ts": prior_start_dt.isoformat()} for _ in range(3)
        ] + [
            {"ts": recent_start_dt.isoformat()} for _ in range(2)
        ]
        
        decision = route_decision(result, incidents_rows=incidents_rows, now=now)
        assert decision["route_enabled"] is True
        assert decision["incidents_prior"] == 3
        assert decision["incidents_recent"] == 2

    def test_route_decision_ignores_malformed_timestamps(self):
        """Route decision ignores malformed timestamps in incident rows."""
        go_live_ts = int(dt.datetime.fromisoformat(ROUTE_GO_LIVE).timestamp())
        now = go_live_ts + 86400
        
        result = {"status": "optimal", "reasons": []}
        
        window_seconds = ROUTE_INCIDENT_WINDOW_DAYS * 86400
        prior_start = now - 2 * window_seconds
        
        # Mix of valid and invalid timestamps
        incidents_rows = [
            {"ts": prior_start + 86400 * i} for i in range(3)
        ] + [
            {"ts": "invalid-timestamp"},
            {"ts": None},
            {"no_ts_field": "value"},
        ]
        
        decision = route_decision(result, incidents_rows=incidents_rows, now=now)
        assert decision["route_enabled"] is True
        assert decision["incidents_prior"] == 3
        assert decision["incidents_recent"] == 0
