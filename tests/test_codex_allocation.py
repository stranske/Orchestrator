"""Production Codex allocation and explicit escalation without model dispatch."""

import adapters
import dispatcher
import execution_profiles
import router


def test_task_routes_use_the_requested_effort_and_preserve_escalation():
    cap = {"agents": {"codex": {"state": "ok"}}}
    expected = {
        "implement": ("codex-5.6-sol-high", "high"),
        "testgen": ("codex-5.6-terra-medium", "medium"),
        "review": ("codex-5.6-terra-medium", "medium"),
        "epic": ("codex-6-astra-medium", "medium"),
        "cross_repo": ("codex-6-astra-medium", "medium"),
    }
    for task_type, (profile_id, effort) in expected.items():
        selected = router.select_agent(task_type, cap, only={"codex"})
        assert selected["selected_profile_id"] == profile_id
        assert selected["reasoning_effort"] == effort
        profile = execution_profiles.get_profile(profile_id)
        argv = adapters.build_command("codex", "x", profile=profile, transport="local")
        assert argv[argv.index("--model") + 1] == profile["requested_model"]
        assert argv[argv.index("-c") + 1] == f'model_reasoning_effort="{effort}"'
    assert execution_profiles.get_profile("codex-6-astra-high")["reasoning_effort"] == "high"


def test_explicit_astra_assessment_pins_model_effort_and_read_only_sandbox():
    profile = execution_profiles.get_profile("codex-6-astra-medium")
    argv = adapters.build_command(
        "codex", "diagnose", mode="assess", profile=profile, transport="offload"
    )
    assert argv[argv.index("--model") + 1] == "gpt-6-astra"
    assert argv[argv.index("-c") + 1] == 'model_reasoning_effort="medium"'
    assert argv[argv.index("--sandbox") + 1] == "read-only"


def test_offload_modes_select_bounded_profiles():
    for mode, profile_id in {
        None: "codex-5.6-terra-medium",
        "cheap": "codex-5.6-luna-low",
        "mid": "codex-5.6-terra-medium",
        "full": "codex-5.6-sol-high",
        "assess": "codex-5.6-sol-medium",
    }.items():
        assert dispatcher._select_offload_profile("codex", mode)["profile_id"] == profile_id


def test_direct_delegate_honors_closer_lane_and_explicit_mode():
    choose = execution_profiles.default_codex_delegate_profile
    assert choose("implement", "opener") == "codex-5.6-sol-high"
    assert choose("implement", "closer") == "codex-5.6-sol-medium"
    assert choose("implement", "closer", "mid") == "codex-5.6-terra-medium"
    assert choose("implement", "opener", "cheap") == "codex-5.6-luna-low"


def test_invalid_explicit_delegate_profile_fails_before_claim(monkeypatch):
    def unexpected_claim(*_args, **_kwargs):
        raise AssertionError("invalid profile must not claim a target")

    monkeypatch.setattr(dispatcher.claims, "claim", unexpected_claim)
    result = dispatcher.delegate(
        "codex", "owner/repo#1", "closer", "diagnose", profile_id="missing-profile"
    )
    assert "unknown execution profile" in result["error"]


def test_cli_forwards_explicit_astra_profile_without_running_it(monkeypatch, capsys):
    observed = {}

    def fake_offload(agent, prompt, **kwargs):
        observed.update(agent=agent, prompt=prompt, **kwargs)
        return {"exit": 0, "output": "planned"}

    monkeypatch.setattr(dispatcher, "offload", fake_offload)
    assert (
        dispatcher.main(
            [
                "offload",
                "--agent",
                "codex",
                "--mode",
                "assess",
                "--profile-id",
                "codex-6-astra-medium",
                "--prompt",
                "Diagnose the blocker",
            ]
        )
        == 0
    )
    assert observed["profile_id"] == "codex-6-astra-medium"
    assert observed["mode"] == "assess"
    assert capsys.readouterr().out.strip() == "planned"
