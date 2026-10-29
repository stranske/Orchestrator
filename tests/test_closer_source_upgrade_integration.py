"""Public source-upgrade composition over a pre-sealed native cohort fixture."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import closer_campaign as campaign
import closer_native_source_upgrade as upgrade
from test_closer_campaign_demand_integration import host, publisher
from test_closer_completed_cohort_reuse import _completed_worker, completed_native_world


@pytest.fixture(autouse=True)
def f858_preseal_recipe(monkeypatch):
    """Set the reviewed physical recipe before qualification/admission, never after terminality."""
    original = campaign.write_manifest
    make_manifest = campaign.make_manifest

    def make(*args, **kwargs):
        # This predecessor identity must enter the manifest before the fixture
        # qualifies and signs its completed native cohort.
        kwargs["candidate_sha"] = upgrade.REVIEWED_OLD_CANDIDATE
        return make_manifest(*args, **kwargs)

    def write(path, manifest):
        demand = manifest.get("mechanism_contract", {}).get("controller_demand")
        if not isinstance(demand, dict) or not isinstance(demand.get("worker_config"), dict):
            return original(path, manifest)
        cfg = demand["worker_config"]
        handoff = Path.home() / ".codex" / "handoff"
        handoff.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("HANDOFF_DIR", str(handoff))
        cfg.update(
            request_timeout_seconds=120,
            execution_policy="finite-frontier/v2",
            coordinator_mode="resource_scoped",
            handoff_dir=str(handoff),
            handoff_agent="codex",
        )
        capacity = manifest["mechanism_contract"].get("capacity_publication")
        if isinstance(capacity, dict):
            capacity["request_timeout_seconds"] = 120
        return original(path, manifest)

    monkeypatch.setattr(campaign, "write_manifest", write)
    monkeypatch.setattr(campaign, "make_manifest", make)


@pytest.mark.parametrize("completed_native_world", ["ci-held"], indirect=True)
def test_presealed_ci_cohort_has_real_terminal_lifecycle(completed_native_world):
    """The old campaign is genuine A/B/C evidence before any upgrade service port."""
    world = completed_native_world
    _campaign, result = _completed_worker(world)
    assert result["outcome"] == "ok"
    assert world.manifest["candidate_sha"] == upgrade.REVIEWED_OLD_CANDIDATE
    assert world.manifest["mechanism_contract"]["controller_demand"]["worker_config"][
        "request_timeout_seconds"
    ] == 120
