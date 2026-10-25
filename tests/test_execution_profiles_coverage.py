"""Hermetic tests for profile validation and replay-safe selection failures."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

import execution_profiles as ep


@pytest.fixture(autouse=True)
def isolated_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state = tmp_path / "state"
    runtime = tmp_path / "runtime"
    state.mkdir()
    runtime.mkdir()
    monkeypatch.setenv("ORCH_STATE_DIR", str(state))
    monkeypatch.setenv("ORCH_LOCAL_RUNTIME", str(runtime))


def test_profile_validation_rejects_missing_bad_permission_and_unknown_pool():
    profile = deepcopy(next(iter(ep.PROFILE_REGISTRY.values())))
    missing = dict(profile)
    del missing["agent"]
    with pytest.raises(ValueError, match="missing fields"):
        ep.validate_profile(missing)
    bad_permission = {**profile, "permission_mode": "unsafe"}
    with pytest.raises(ValueError, match="invalid permission_mode"):
        ep.validate_profile(bad_permission)
    bad_pool = {**profile, "capacity_pool_ids": ["not-a-pool"]}
    with pytest.raises(ValueError, match="unknown capacity pools"):
        ep.validate_profile(bad_pool)


def test_retired_only_selection_is_explicitly_unassigned_and_tampering_is_rejected():
    retired, successor = next(iter(ep.PROFILE_RETIREMENTS.items()))
    envelope = ep.select_profile("implement", "o/r#1", [retired], rng_seed=1)
    assert envelope["selected_profile_id"] is None
    assert envelope["assignment_probability"] == 0.0
    assert envelope["gate_results"][retired]["retirement_reason"] == "superseded_production_profile"
    tampered = dict(envelope, replay_hash="not-the-real-hash")
    with pytest.raises(ValueError, match="not replayable"):
        ep.replay_decision(tampered)
    assert ep.resolve_production_profile_id(retired) == successor
