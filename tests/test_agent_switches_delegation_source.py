"""Hosted exploration draws must survive switch ingestion without widening unknown sources."""

import agent_switches


def test_exploration_source_is_recognised():
    for reason, expected in [
        ("delegation_source: route_weights_exploration", "route_weights_exploration"),
        ("delegation_source: route_weights", "route_weights"),
        ("delegation_source: static (x)", "static"),
        ("garbage", "unknown"),
        ("delegation_source: route_weights_exploration_extra", "unknown"),
        ("delegation_source: route_weights_extra", "unknown"),
    ]:
        assert agent_switches.delegation_source_of(reason) == expected
        assert expected in agent_switches.DELEGATION_SOURCES
