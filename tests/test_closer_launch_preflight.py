"""The launch preflight is diagnostic-only and never probes providers itself."""

import json
from pathlib import Path

import pytest

import closer_launch_preflight as preflight


@pytest.mark.parametrize("alias_kind", ["relative", "symlink"])
def test_noncanonical_manifest_is_held_before_runtime_or_capacity_reads(tmp_path, alias_kind):
    target = tmp_path / "manifest.json"
    target.write_text("{}")
    if alias_kind == "relative":
        supplied = Path("relative-manifest.json")
    else:
        supplied = tmp_path / "manifest-alias.json"
        supplied.symlink_to(target)
    result = preflight.inspect(supplied)
    assert result["stage"] == "held" and result["authority"] is False
    assert result["reasons"] == ["closure: manifest path is not canonical"]


def _patch_closed_scope(monkeypatch, tmp_path, *, report, strict_error=None, validate=True):
    import closer_campaign as campaign
    import closer_native_observer as native
    import closer_operational_capacity as operational
    import execution_profiles

    manifest_path = (tmp_path / "manifest.json").resolve()
    manifest_path.write_text("{}")
    manifest = {
        "package_digest": "package",
        "modules": {
            "closer_launch_preflight.py": preflight._sha256(Path(preflight.__file__).resolve())
        },
    }
    policy = {
        "owner": "native-owner",
        "profile_ids": ["profile"],
        "bounded_trial_agents": ["claude"],
        "freshness_seconds": 30,
    }
    monkeypatch.setattr(campaign, "load_manifest", lambda _path: manifest)
    monkeypatch.setattr(
        native, "contract", lambda _manifest: ({}, Path("/wrapper"), Path("/py"), tmp_path)
    )

    def contract(_manifest, *, control_plane_only=False):
        if not control_plane_only and strict_error is not None:
            raise strict_error
        return policy

    monkeypatch.setattr(native, "capacity_contract", contract)
    monkeypatch.setattr(execution_profiles, "get_profile", lambda _profile: {"agent": "claude"})
    monkeypatch.setattr(operational, "evaluate", lambda _config, *, now: report)
    monkeypatch.setattr(operational, "validate_report", lambda _report, *, now: validate)
    return manifest_path


def _ready_report(state="ok"):
    return {
        "generated_at": 1000,
        "expires_at": 1030,
        "input_sha256": {"input": "hash"},
        "profiles": {"profile": {"state": state, "capacity_pool_ids": ["pool"]}},
        "pools": {"pool": {"state": state, "agent": "claude"}},
        "agents": {"claude": {"state": state}},
    }


def test_report_reasons_require_profile_agent_and_pool_usable():
    report = {
        "profiles": {"p": {"state": "ok", "capacity_pool_ids": ["pool"]}},
        "pools": {"pool": {"state": "warn", "agent": "claude"}},
        "agents": {"claude": {"state": "ok"}},
    }
    assert preflight._report_reasons(report, ["p"]) == []
    report["agents"]["claude"]["state"] = "shed"
    assert "agent 'claude'" in preflight._report_reasons(report, ["p"])[0]


def test_selected_owner_recovery_is_scoped_and_records_before_after(monkeypatch):
    calls = []

    class Capacity:
        def shed_marker(self, agent, *, now):
            return {"agent": agent, "state": "active" if not calls else "expired"}

        def _shed(self, agent):
            calls.append(agent)
            return False

    rows = preflight._refresh_selected_sheds(Capacity(), {"claude"}, now=1000)
    assert calls == ["claude"]
    assert rows[0]["before"]["state"] == "active"
    assert rows[0]["after"]["state"] == "expired"


def test_stale_or_changed_inputs_hold_without_owner_recovery(monkeypatch, tmp_path):
    import capacity

    path = _patch_closed_scope(monkeypatch, tmp_path, report=_ready_report(), validate=False)
    monkeypatch.setattr(
        capacity, "_shed", lambda _agent: (_ for _ in ()).throw(AssertionError("mutated"))
    )
    result = preflight.inspect(path)
    assert result["stage"] == "held" and result["authority"] is False
    assert result["reasons"] == ["capacity: report is stale or inputs changed during read"]


def test_control_plane_scope_never_becomes_business_ready(monkeypatch, tmp_path):
    path = _patch_closed_scope(
        monkeypatch,
        tmp_path,
        report=_ready_report(),
        strict_error=RuntimeError("role qualification unavailable"),
    )
    result = preflight.inspect(path)
    assert result["stage"] == "held" and result["authority"] is False
    assert result["reasons"] == ["qualification: RuntimeError: role qualification unavailable"]


def test_unpinned_evaluator_cannot_report_ready(monkeypatch, tmp_path):
    import closer_campaign as campaign

    path = _patch_closed_scope(monkeypatch, tmp_path, report=_ready_report())
    monkeypatch.setattr(campaign, "load_manifest", lambda _path: {"modules": {}})
    result = preflight.inspect(path)
    assert result["stage"] == "held" and result["authority"] is False
    assert "evaluator is outside the pinned Runtime closure" in result["reasons"][0]


def test_held_cli_returns_nonzero_without_launching(tmp_path, capsys):
    assert preflight.main(["--manifest", str((tmp_path / "missing.json").resolve())]) == 2
    report = json.loads(capsys.readouterr().out)
    assert report["stage"] == "held" and report["authority"] is False


def test_conditional_selected_recovery_can_make_a_new_read_ready(monkeypatch, tmp_path):
    import capacity
    import closer_operational_capacity as operational

    recovered = {"done": False}
    path = _patch_closed_scope(monkeypatch, tmp_path, report=_ready_report("shed"))
    monkeypatch.setattr(
        operational,
        "evaluate",
        lambda _config, *, now: _ready_report("ok" if recovered["done"] else "shed"),
    )
    monkeypatch.setattr(
        capacity,
        "shed_marker",
        lambda agent, *, now: {
            "agent": agent,
            "state": "expired" if recovered["done"] else "active",
        },
    )

    def conditional_owner_recovery(agent):
        assert agent == "claude"
        recovered["done"] = True
        return False

    monkeypatch.setattr(capacity, "_shed", conditional_owner_recovery)
    result = preflight.inspect(path, refresh_capacity_owner=True)
    assert result["stage"] == "ready" and result["authority"] is False
    assert result["shed_recovery"][0]["agent"] == "claude"
