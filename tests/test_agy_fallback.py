import copy
import time

import adapters
import agy_fallback
import execution_profiles
import router


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
    monkeypatch.setattr(agy_fallback, "select", lambda: "agy-claude-sonnet-5-5-low")
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
