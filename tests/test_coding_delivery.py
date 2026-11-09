"""Negative source-inspection/interface mechanics; no native authority or process."""

import sys
from types import SimpleNamespace

import pytest

import coding_delivery as delivery


@pytest.mark.parametrize("name", ["_begin", "_verify", "_revoke", "require_coding_delivery"])
@pytest.mark.parametrize("frame_kind", ["actual", "none"])
def test_unavailable_frame_inspection_refuses_before_registry_effect(monkeypatch, name, frame_kind):
    frame = sys._getframe() if frame_kind == "actual" else None
    before = dict(delivery._ACTIVE)
    # An unavailable inspection primitive is negative DATA, never a fabricated
    # authorized caller frame or successful source validator.
    monkeypatch.setattr(delivery, "inspect", SimpleNamespace(currentframe=lambda: None))
    with pytest.raises(ValueError, match="original.*coding|coding.*required"):
        getattr(delivery, name)(frame)
    assert delivery._ACTIVE == before


@pytest.mark.parametrize("fault", [None, "provider_argv", "transport_argv", "registry"])
def test_original_offload_prompt_custody_refuses_mutation_before_popen(
    tmp_path, monkeypatch, fault
):
    from test_offload_failure_evidence import isolated_offload

    import dispatcher
    import execution_profiles

    isolated_offload(tmp_path, monkeypatch)
    monkeypatch.setattr(dispatcher.adapters, "agent_workspace", lambda *a, **k: (tmp_path, None))
    monkeypatch.setattr(dispatcher, "_agent_runtime_prelude", lambda *a: "")
    monkeypatch.setattr(dispatcher, "_auth_prelude", lambda *a: "")
    monkeypatch.setattr(dispatcher, "_path_prefix", lambda: "true")
    monkeypatch.setattr(dispatcher, "_net_hygiene_prelude", lambda: "")
    monkeypatch.setattr(
        dispatcher.adapters,
        "build_command",
        lambda agent, prompt, *a, **k: [sys.executable, "-c", "print('local fixture')", prompt],
    )
    monkeypatch.setattr(dispatcher.subprocess, "Popen", lambda *a, **k: pytest.fail("no spawn"))
    observations = []

    class InspectedBeforeSpawn(Exception):
        pass

    def before(_event):
        frame = sys._getframe().f_back
        held = delivery.require_coding_delivery(frame)
        observations.append(held)
        assert held.prompt_utf8 == b"private local fixture"
        assert held.profile_json
        assert held.provider_argv[-1] == "private local fixture"
        if fault == "provider_argv":
            frame.f_back.f_back.f_locals["argv"][-1] = "changed"
        elif fault == "transport_argv":
            frame.f_locals["command"][-1] = "changed"
        elif fault == "registry":
            delivery._ACTIVE.clear()
        else:
            raise InspectedBeforeSpawn

    error = ValueError if fault else InspectedBeforeSpawn
    with pytest.raises(error):
        dispatcher.offload(
            "codex",
            "private local fixture",
            cwd=str(tmp_path),
            profile_id=execution_profiles.get_profile(delivery.SELECTED_PROFILE)["profile_id"],
            process_starting_callback=before,
            process_started_callback=lambda _: None,
            max_network_retries=0,
        )
    assert len(observations) == 1
    assert delivery._ACTIVE == {}


@pytest.mark.parametrize("options", [{"data_only": True}, {"trusted_auth_supplier": lambda: None}])
def test_retired_transport_refuses_before_runtime_or_credential_effects(monkeypatch, options):
    import dispatcher

    monkeypatch.setattr(
        dispatcher, "_capability_heartbeat", lambda *a, **k: pytest.fail("no effect")
    )
    result = dispatcher.offload("claude", "local fixture", **options)
    assert result["exit"] == 2 and result["process_started"] is False
    assert result["outcome_uncertain"] is False and result["run_id"] is None


def test_existing_maintenance_restriction_still_refuses_before_dispatch(monkeypatch):
    import dispatcher
    import offload_guard

    monkeypatch.setattr(offload_guard._CURRENT, "scope", object(), raising=False)
    monkeypatch.setattr(
        dispatcher, "_capability_heartbeat", lambda *a, **k: pytest.fail("no effect")
    )
    result = dispatcher.offload("cursor", "local fixture")
    assert result["process_started"] is False and result["retry_safe"] is False
