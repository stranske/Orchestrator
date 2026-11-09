"""The shared agent model table: local copy, derivations, and drift against Workflows."""

from __future__ import annotations

import io
import urllib.error
import urllib.request

import pytest

import adapters
import execution_profiles
import model_table


def test_copy_matches_workflows_source() -> None:
    """Fails when this copy and stranske/Workflows main disagree.

    Only this (copy) side compares across repositories: if Workflows CI also compared against
    Orchestrator, a table change could merge on neither side. Remedy on failure:
    `python3 src/model_table.py --sync <Workflows>/.github/agents/model-table.json`.
    """
    try:
        with urllib.request.urlopen(model_table.WORKFLOWS_SOURCE, timeout=30) as response:
            source_text = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        # A transient rate-limit or server response has no table body to compare.
        # Unexpected HTTP failures still fail; successful bodies must match exactly.
        if exc.code == 404:
            pytest.skip(
                "CANNOT COMPARE: Workflows main has no model table yet (paired PR not merged)"
            )
        if exc.code in {429, 500, 502, 503, 504}:
            pytest.skip(f"CANNOT COMPARE: Workflows source returned HTTP {exc.code}; not a pass")
        raise
    except (urllib.error.URLError, TimeoutError) as exc:
        pytest.skip(f"CANNOT COMPARE: Workflows source unreachable ({exc}); not a pass")
    assert model_table.TABLE_JSON == source_text, (
        "model_table.TABLE_JSON differs from stranske/Workflows main .github/agents/model-table.json; "
        "run `python3 src/model_table.py --sync <Workflows>/.github/agents/model-table.json`"
    )


def test_copy_comparison_distinguishes_unavailable_from_real_failure(monkeypatch) -> None:
    for code in (404, 429, 500, 502, 503, 504):

        def unavailable(*args, **kwargs):
            raise urllib.error.HTTPError(
                model_table.WORKFLOWS_SOURCE, code, "controlled", None, None
            )

        monkeypatch.setattr(urllib.request, "urlopen", unavailable)
        with pytest.raises(pytest.skip.Exception, match="CANNOT COMPARE"):
            test_copy_matches_workflows_source()
    for code in (401, 403, 418):

        def unexpected(*args, **kwargs):
            raise urllib.error.HTTPError(
                model_table.WORKFLOWS_SOURCE, code, "controlled", None, None
            )

        monkeypatch.setattr(urllib.request, "urlopen", unexpected)
        with pytest.raises(urllib.error.HTTPError) as error:
            test_copy_matches_workflows_source()
        assert error.value.code == code
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **kw: io.BytesIO(b"different body"))
    with pytest.raises(AssertionError, match="TABLE_JSON differs"):
        test_copy_matches_workflows_source()
    monkeypatch.setattr(
        urllib.request, "urlopen", lambda *a, **kw: io.BytesIO(model_table.TABLE_JSON.encode())
    )
    test_copy_matches_workflows_source()


def test_sync_round_trips_byte_for_byte(tmp_path, monkeypatch) -> None:
    module = tmp_path / "model_table.py"
    module.write_text(open(model_table.__file__, encoding="utf-8").read(), encoding="utf-8")
    edited = model_table.TABLE_JSON.replace('"as_of": "', '"as_of": "x', 1)
    source = tmp_path / "model-table.json"
    source.write_text(edited, encoding="utf-8")
    monkeypatch.setattr(model_table, "__file__", str(module))
    assert model_table._sync(source) == 0
    text = module.read_text(encoding="utf-8")
    start = text.index("\n" + model_table.BEGIN_MARK) + 1 + len(model_table.BEGIN_MARK)
    end = text.index("\n" + model_table.END_MARK, start) + 1
    assert text[start:end] == edited
    assert text.count(model_table.BEGIN_MARK) == 2  # the constant and the block


def test_model_tiers_are_derived_from_the_table() -> None:
    table = model_table.TABLE
    for agent in model_table.TIERED_AGENTS:
        models = (
            table["providers"][agent].get("surface_models", {}).get("local")
            or table["providers"][agent]["models"]
        )
        assert adapters.MODEL_TIERS[agent] == {t: models[t] for t in model_table.LOCAL_TIERS}
    for agent in ("cursor", "vibe", "aider", "muse"):
        assert adapters.MODEL_TIERS[agent] == {}
    assert adapters.VIBE_MODEL == table["providers"]["vibe"]["models"]["full"]
    assert adapters.MUSE_MODEL == table["providers"]["muse"]["models"]["full"]
    assert adapters.AIDER_MODEL == table["providers"]["aider"]["models"]["full"]
    assert adapters.CURSOR_COMPOSER_MODEL == table["providers"]["cursor"]["models"]["full"]


@pytest.mark.parametrize("task_type", sorted(model_table.TASK_WORK_KINDS))
def test_codex_task_profiles_carry_the_table_pair(task_type: str) -> None:
    kind = model_table.TASK_WORK_KINDS[task_type]
    profile = execution_profiles.get_profile(execution_profiles.CODEX_TASK_PROFILES[task_type])
    assert profile["requested_model"] == model_table.model_for("codex", kind)
    assert profile["reasoning_effort"] == model_table.effort_for("codex", kind)


@pytest.mark.parametrize("mode", sorted(model_table.MODE_WORK_KINDS))
def test_codex_offload_profiles_carry_the_table_pair(mode: str) -> None:
    kind = model_table.MODE_WORK_KINDS[mode]
    profile = execution_profiles.get_profile(execution_profiles.CODEX_OFFLOAD_PROFILES[mode])
    assert profile["requested_model"] == model_table.model_for("codex", kind)
    assert profile["reasoning_effort"] == model_table.effort_for("codex", kind)


def test_tier_only_codex_run_passes_the_table_level() -> None:
    for tier, kind in (("cheap", "extract"), ("mid", "routine"), ("full", "implement")):
        cmd = adapters.build_command("codex", "x", mode=tier)
        effort = model_table.effort_for("codex", kind)
        assert f'model_reasoning_effort="{effort}"' in cmd, (tier, cmd)
    assess = adapters.build_command("codex", "x", mode="assess")
    assert not any("model_reasoning_effort" in part for part in assess)


def test_clamped_tier_run_passes_the_clamped_tier_level(monkeypatch) -> None:
    # A seat ceiling lowers the model; the level must follow it, or the run gets a (model, level)
    # pair the table never defines and the ceiling no longer bounds cost.
    monkeypatch.setenv("ORCH_CODEX_MAX_TIER", "mid")
    cmd = adapters.build_command("codex", "x", mode="full")
    assert cmd[cmd.index("--model") + 1] == model_table.model_for("codex", "routine")
    effort = model_table.effort_for("codex", "routine")
    assert cmd.count(f'model_reasoning_effort="{effort}"') == 1, cmd


def test_unknown_task_type_uses_the_table_default_profile() -> None:
    profile_id = execution_profiles.default_codex_profile("no-such-task")
    assert profile_id == execution_profiles.resolve_production_profile_id(
        execution_profiles.CODEX_DEFAULT_TASK_PROFILE
    )
    profile = execution_profiles.get_profile(profile_id)
    kind = model_table.DEFAULT_TASK_WORK_KIND
    assert profile["requested_model"] == model_table.model_for("codex", kind)
    assert profile["reasoning_effort"] == model_table.effort_for("codex", kind)


def test_profile_run_keeps_its_immutable_level(monkeypatch) -> None:
    # Only the argv is checked, so the exact-profile binary lookup is stubbed: the test must run
    # on hosts without the Codex app bundle (CI), not skip there.
    monkeypatch.setattr(adapters, "profile_codex_binary", lambda: "codex")
    profile = execution_profiles.CODEX_TASK_PROFILES["review"]
    effort = execution_profiles.get_profile(profile)["reasoning_effort"]
    cmd = adapters.build_command("codex", "x", mode="full", profile=profile, transport="local")
    assert cmd.count(f'model_reasoning_effort="{effort}"') == 1
    assert (
        cmd[cmd.index("--model") + 1] == execution_profiles.get_profile(profile)["requested_model"]
    )


def test_missing_profile_for_a_table_pair_refuses() -> None:
    registry = {
        pid: p
        for pid, p in execution_profiles.PROFILE_REGISTRY.items()
        if p["requested_model"] != model_table.model_for("codex", "extract")
    }
    with pytest.raises(ValueError, match="must match exactly one active profile"):
        model_table.codex_profile_for("extract", registry, execution_profiles.PROFILE_RETIREMENTS)
