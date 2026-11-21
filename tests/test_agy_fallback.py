import copy
import time

import pytest

import adapters
import agy_fallback
import dispatcher
import execution_profiles
import router
import storage_lifecycle


def sample(now):
    def row(key, value):
        return {"id": key, "remaining_fraction": value}

    return {
        "ts": now,
        "groups": [
            {"name": "Gemini Models", "buckets": [row("gemini-weekly", 0), row("gemini-5h", 1)]},
            {
                "name": "Claude and GPT models",
                "buckets": [row("3p-weekly", 0.8), row("3p-5h", 0.7)],
            },
        ],
    }


def test_independent_quota_and_fail_closed(monkeypatch):
    now = time.time()
    data = sample(now)
    ids = ["claude-sonnet-5-5-low", "gpt-oss-120b-medium"]

    def choose(d, t=now):
        return agy_fallback.select(data=d, now=t, advertised=ids)

    assert choose(data) == "agy-claude-sonnet-5-5-low"
    for group, bucket, value in [(0, 0, 0.1), (1, 0, 0), (1, 1, 0)]:
        other = copy.deepcopy(data)
        other["groups"][group]["buckets"][bucket]["remaining_fraction"] = value
        assert choose(other) is None
    assert choose(data, now + 100000) is None
    assert agy_fallback.select(data=data, now=now, advertised=[]) is None
    malformed = copy.deepcopy(data)
    malformed["groups"][1]["buckets"][0]["remaining_fraction"] = True
    assert choose(malformed) is None
    malformed = copy.deepcopy(data)
    malformed["groups"][1]["buckets"][0]["id"] = []
    assert choose(malformed) is None
    duplicate = copy.deepcopy(data)
    duplicate["groups"].append(copy.deepcopy(duplicate["groups"][0]))
    assert choose(duplicate) is None
    duplicate = copy.deepcopy(data)
    duplicate["groups"][1]["buckets"].append(copy.deepcopy(duplicate["groups"][1]["buckets"][0]))
    assert choose(duplicate) is None
    expired = copy.deepcopy(data)
    expired["groups"][1]["buckets"][0]["reset_time"] = "2020-01-01T00:00:00Z"
    assert choose(expired) is None
    assert (
        agy_fallback.select(data=data, now=now, advertised=["gpt-oss-120b-medium"])
        == "agy-gpt-oss-120b-medium"
    )
    assert (
        agy_fallback.select(data=data, now=now, advertised=["claude-opus-5-5-low"])
        == "agy-claude-opus-5-5-low"
    )
    assert agy_fallback.select(now=now) is None
    monkeypatch.setenv("ORCH_GEMINI_MAX_TIER", "cheap")
    assert choose(data) is None


def test_router_preserves_nonquota_shed_and_selects_independent_profile(monkeypatch):
    monkeypatch.setattr(
        agy_fallback, "select", lambda **kwargs: "agy-claude-sonnet-5-5-low" if not kwargs else None
    )
    cap = {"agents": {"gemini": {"state": "shed", "policy": "weekly-polled-cap"}}}
    picked = router.select_agent(
        "implement", cap, only={"gemini"}, exploration_rate=0, simulate=True
    )
    assert picked["selected_profile_id"] == "agy-claude-sonnet-5-5-low"
    picked = router.select_agent(
        "mechanical", cap, only={"gemini"}, exploration_rate=0, simulate=True
    )
    assert picked["selected_profile_id"] == "agy-claude-sonnet-5-5-low"
    assert picked["late"]
    cap["agents"]["gemini"]["policy"] = "auth-failed"
    assert (
        router.select_agent("implement", cap, only={"gemini"}, exploration_rate=0, simulate=True)
        is None
    )


def test_fallback_never_enables_paid_overages(tmp_path):
    profile = execution_profiles.get_profile("agy-claude-sonnet-5-5-low")
    cmd = adapters.build_command("gemini", "hello", "full", cwd=tmp_path, profile=profile)
    assert cmd[cmd.index("--model") + 1] == "claude-sonnet-5-5-low"
    assert cmd[cmd.index("--settings") + 1] == '{"useG1Credits":false}'
    assert (
        adapters.build_command(
            "gemini", "hello", "full", cwd=tmp_path, profile=profile["profile_id"]
        )
        == cmd
    )


def test_partner_backup_and_weekly_drain_are_independent_from_gemini(monkeypatch):
    now = time.time()
    data = sample(now)
    data["groups"][0]["buckets"][0]["remaining_fraction"] = 0.8
    data["groups"][1]["buckets"][0]["reset_time"] = time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(now + 2 * 86400)
    )
    claude = {
        "ts": now,
        "weekly": {"label": "all models", "used_percent": 100, "resets_at": now + 86400},
        "session": {"used_percent": 0, "resets_at": now + 3600},
    }

    def choose(d, c):
        return agy_fallback.select(
            data=d, claude_data=c, now=now, advertised=["claude-sonnet-5-5-low"], purpose="partner"
        )

    assert choose(data, claude) == "agy-claude-sonnet-5-5-low"
    monkeypatch.setattr(
        adapters, "tier_ceiling", lambda agent: "cheap" if agent == "claude" else None
    )
    assert choose(data, claude) is None
    monkeypatch.setattr(adapters, "tier_ceiling", lambda agent: None)
    assert agy_fallback.select(data=data, now=now, advertised=["claude-sonnet-5-5-low"]) is None
    claude["weekly"]["used_percent"] = 0
    assert choose(data, claude) is None
    claude["session"]["used_percent"] = 100
    assert choose(data, claude) == "agy-claude-sonnet-5-5-low"
    claude["session"]["used_percent"] = 0
    data["groups"][1]["buckets"][0]["reset_time"] = time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(now + 3600)
    )
    assert choose(data, claude) == "agy-claude-sonnet-5-5-low"
    data["groups"][1]["buckets"][0]["remaining_fraction"] = 0
    assert choose(data, claude) is None


def test_partner_is_a_distinct_lane_and_delegate_rechecks_it(monkeypatch):
    monkeypatch.setattr(
        agy_fallback,
        "select",
        lambda **kwargs: (
            "agy-claude-sonnet-5-5-low" if kwargs.get("purpose") == "partner" else None
        ),
    )
    monkeypatch.setattr(agy_fallback, "weekly_drain", lambda: False)
    monkeypatch.setattr(router, "_local_comparative_enabled", lambda: False)
    cap = {"agents": {"gemini": {"state": "ok", "policy": "steady"}, "claude": {"state": "shed"}}}
    partner = router.select_agent("implement", cap, only={"agy"}, exploration_rate=0, simulate=True)
    assert partner["agent"] == "agy"
    assert partner["selected_profile_id"] == "agy-claude-sonnet-5-5-low"
    gemini = router.select_agent(
        "implement", cap, only={"gemini"}, exploration_rate=0, simulate=True
    )
    assert gemini["agent"] == "gemini"
    assert gemini["selected_profile_id"].startswith("gemini-")
    monkeypatch.setattr(agy_fallback, "weekly_drain", lambda: True)
    drain = router.select_agent(
        "implement", cap, only={"agy", "gemini"}, exploration_rate=0, simulate=True
    )
    assert drain["agent"] == "agy"
    assert drain["capacity_policy"] == "drain"
    assert (
        router._capacity_policy_bias("testgen", "agy", {"agents": {"agy": {"policy": "drain"}}})
        == -1
    )
    monkeypatch.setattr(
        agy_fallback, "claude_backup_for_dispatch", lambda: "agy-claude-sonnet-5-5-low"
    )
    monkeypatch.setattr(dispatcher, "linked_open_prs", lambda target: [])
    monkeypatch.setattr(dispatcher.claims, "reap_stale", lambda: None)
    monkeypatch.setattr(dispatcher.claims, "claim", lambda target, agent: True)
    monkeypatch.setattr(dispatcher.claims, "release", lambda target, agent: True)
    seen = {}

    def stop_after_plan(assignment, **kwargs):
        seen.update(assignment)
        return {"error": "test stops before spawn"}

    monkeypatch.setattr(dispatcher, "plan_dispatch", stop_after_plan)
    dispatcher.delegate("agy", "stranske/Orchestrator#1", "opener", "test")
    assert seen["agent"] == "gemini"
    assert seen["selected_profile_id"] == "agy-claude-sonnet-5-5-low"
    cap["agents"]["gemini"] = {"state": "shed", "policy": "weekly-polled-cap"}
    monkeypatch.setattr(agy_fallback, "select", lambda **kwargs: "agy-claude-sonnet-5-5-low")
    assert (
        router.select_agent("implement", cap, only={"agy"}, exploration_rate=0, simulate=True)
        is None
    )
    assert (
        router.select_agent("implement", cap, only={"gemini"}, exploration_rate=0, simulate=True)[
            "selected_profile_id"
        ]
        == "agy-claude-sonnet-5-5-low"
    )

    def stop_storage(agent):
        raise RuntimeError("selected:" + agent)

    monkeypatch.setattr(storage_lifecycle, "owned_agent_tmpdir", stop_storage)
    with pytest.raises(RuntimeError, match="selected:gemini"):
        dispatcher.offload.__wrapped__("claude", "test")
    for opts in (
        {"mode": "mid"},
        {"profile_id": "claude-sonnet-5-5-high-v2"},
        {"research_round": "r"},
    ):
        with pytest.raises(RuntimeError, match="selected:claude"):
            dispatcher.offload.__wrapped__("claude", "test", **opts)
    monkeypatch.setattr(
        agy_fallback, "claude_backup_for_dispatch", lambda: "agy-gpt-oss-120b-medium"
    )
    with pytest.raises(RuntimeError, match="selected:claude"):
        dispatcher.offload.__wrapped__("claude", "test")

    seen_load = []

    def pick_queued(task_type, cap, **kwargs):
        seen_load.append(dict(kwargs["load"]))
        return {
            "agent": "agy",
            "mode": "mid",
            "state": "ok",
            "selected_profile_id": "agy-claude-sonnet-5-5-low",
        }

    monkeypatch.setattr(router, "select_agent", pick_queued)
    monkeypatch.setattr(router.claims, "active_claims", lambda: {})
    queued_cap = {"agents": {"gemini": {"state": "ok"}, "codex": {"state": "ok"}}}
    tasks = [
        {"target": f"stranske/Orchestrator#{n}", "task_type": "implement", "lane": "opener"}
        for n in (1, 2)
    ]
    result = router.plan(tasks, queued_cap, dry_run=True, capability_records={})
    assert len(result["assignments"]) == 2
    assert seen_load[1].get("agy") == 1 and "gemini" not in seen_load[1]
