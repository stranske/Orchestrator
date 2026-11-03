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


def test_policy_source_counts_sum_equals_policy_switches(tmp_path):
    import json

    entries = [
        {
            "previous_agent": "codex",
            "chosen_agent": "claude",
            "timestamp": f"2026-10-17T00:00:0{i}Z",
            "delegation_source": source,
            "iteration": i,
        }
        for i, source in enumerate(agent_switches.DELEGATION_SOURCES)
    ]
    fact = {
        "label_events": [],
        "commit_ts": [],
        "keepalive_state": {"read": "marker", "delegation_log": entries},
    }
    pr = {"ref": "example/repo#1", "repo": "example/repo", "number": 1, "merged": True}
    payload, _ = agent_switches.aggregate([pr], {pr["ref"]: fact}, now=200, window_days=1)
    counts = payload["counts"]
    assert (
        sum(
            counts.get(key, 0)
            for key in (
                "policy_route_weights",
                "policy_route_weights_exploration",
                "policy_static",
                "policy_source_unknown",
            )
        )
        == counts["policy_switches"]
        == 4
    )
    assert counts["policy_route_weights_exploration"] == 1
    (tmp_path / "agent-switches.json").write_text(json.dumps(payload))
    summary = agent_switches.summary_for_report(tmp_path)
    assert summary["policy_transitions"]["codex->claude"]["route_weights_exploration"] == 1
    assert "1 exploration" in agent_switches.policy_phrase(summary)
