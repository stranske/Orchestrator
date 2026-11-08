"""An exhausted Argon quota sheds only gemini's `full` tier; Flash (cheap/mid) keeps routing.

On 2026-11-07 one Argon quota incident shed all of gemini until 2026-11-14 while Flash still
answered, and the Flash-vs-Argon triage trial got zero gemini runs.
"""

from __future__ import annotations

import json
import time

import pytest

import adapters
import capacity
import rate_incidents
import roles

ARGON_FAILURE = (
    '{"event": "init", "init": {"model": "gemini-4-argon-medium", "cwd": "/tmp/x"}}\n'
    '{"event": "result", "result": {"status": "ERROR", "error": "Individual quota reached. '
    'Resets in 124h46m45s."}}\n'
    'AGY_ERROR: {"short_error":"RESOURCE_EXHAUSTED (code 429): Individual quota reached."}'
)
FLASH_FAILURE = ARGON_FAILURE.replace("gemini-4-argon-medium", "gemini-3.8-flash-high")


@pytest.fixture
def shed_dir(tmp_path, monkeypatch):
    shed = tmp_path / "capacity-shed"
    shed.mkdir()
    monkeypatch.setattr(rate_incidents, "SHED_DIR", shed)
    monkeypatch.setattr(capacity, "SHED_DIR", shed)
    monkeypatch.setattr(rate_incidents, "INCIDENT_FILE", tmp_path / "incidents.ndjson")
    if hasattr(rate_incidents, "LOCK_FILE"):
        monkeypatch.setattr(rate_incidents, "LOCK_FILE", tmp_path / "incidents.lock")
    monkeypatch.delenv("ORCH_GEMINI_MAX_TIER", raising=False)
    return shed


def _record(evidence: str, run_id: str, reset_in: int = 3600) -> dict:
    return rate_incidents.record_incident(
        agent="gemini",
        surface="dispatcher.offload",
        category="quota",
        status="recorded",
        run_id=run_id,
        evidence=evidence,
        reset_at=int(time.time()) + reset_in,
    )


def _marker(shed_dir) -> dict:
    return json.loads((shed_dir / "gemini").read_text())


def test_argon_quota_end_to_end_caps_gemini_at_flash_instead_of_shedding_it(shed_dir, monkeypatch):
    _record(ARGON_FAILURE, "run-argon")
    marker = _marker(shed_dir)
    assert marker["model"] == "gemini-4-argon-medium" and marker["tier_scope"] == "full"
    assert capacity._shed("gemini") is False, "the seat stays routable"
    exports = capacity.tier_ceiling_exports(environ={})
    assert "export ORCH_GEMINI_MAX_TIER=mid" in exports
    assert any("capped at mid" in line and "gemini-4-argon-medium" in line for line in exports)
    monkeypatch.setenv("ORCH_GEMINI_MAX_TIER", "mid")
    assert adapters.effective_tier("gemini", "full") == "mid"
    assert (
        adapters.resolve_model("gemini", adapters.effective_tier("gemini", "full"))
        == adapters.MODEL_TIERS["gemini"]["mid"]
    )


def test_a_flash_quota_still_sheds_the_whole_seat(shed_dir):
    _record(FLASH_FAILURE, "run-flash")
    marker = _marker(shed_dir)
    assert "tier_scope" not in marker and marker["model"] == "gemini-3.8-flash-high"
    assert capacity._shed("gemini") is True
    assert capacity.tier_ceilings() == {}


def test_a_failure_without_a_model_still_sheds_the_whole_seat(shed_dir):
    _record("Individual quota reached. RESOURCE_EXHAUSTED", "run-unknown")
    assert "tier_scope" not in _marker(shed_dir) and capacity._shed("gemini") is True


def test_an_active_whole_seat_shed_is_never_narrowed(shed_dir):
    # Resets beyond the quota cooldown, so the later Argon expiry would otherwise win.
    _record(FLASH_FAILURE, "run-flash", reset_in=100_000)
    _record(ARGON_FAILURE, "run-argon", reset_in=200_000)
    assert "tier_scope" not in _marker(shed_dir) and capacity._shed("gemini") is True


def test_a_scoped_shed_widens_when_flash_runs_out_too(shed_dir):
    _record(ARGON_FAILURE, "run-argon", reset_in=200_000)
    _record(FLASH_FAILURE, "run-flash", reset_in=600)
    marker = _marker(shed_dir)
    assert "tier_scope" not in marker and capacity._shed("gemini") is True
    assert marker["expires_at"] >= int(time.time()) + 190_000, "widening keeps the longer expiry"


def test_an_expired_scoped_marker_is_removed_and_caps_nothing(shed_dir):
    (shed_dir / "gemini").write_text(
        json.dumps(
            {
                "category": "quota",
                "created_at": 1,
                "expires_at": int(time.time()) - 5,
                "model": "gemini-4-argon-high",
                "tier_scope": "full",
            }
        )
    )
    assert capacity.tier_ceilings() == {}
    assert capacity._shed("gemini") is False
    assert not (shed_dir / "gemini").exists(), "the drain still removes it on read"


def test_an_unknown_scope_is_a_whole_seat_shed(shed_dir):
    (shed_dir / "gemini").write_text(
        json.dumps(
            {
                "category": "quota",
                "created_at": 1,
                "expires_at": int(time.time()) + 600,
                "tier_scope": "weird",
            }
        )
    )
    assert capacity._shed("gemini") is True and capacity.tier_ceilings() == {}


def test_an_explicit_owner_ceiling_is_kept(shed_dir):
    _record(ARGON_FAILURE, "run-argon")
    exports = capacity.tier_ceiling_exports(environ={"ORCH_GEMINI_MAX_TIER": "cheap"})
    assert not any(line.startswith("export ") for line in exports)
    assert any("keeps explicit ORCH_GEMINI_MAX_TIER" in line for line in exports)


def test_trial_argon_arm_routes_normally_while_argon_is_capped(tmp_path, monkeypatch):
    trial = tmp_path / "trial.json"
    start = 498_332 * 3600
    trial.write_text(json.dumps({"start_ts": start, "end_ts": start + 7 * 86400}))
    flash_hour, argon_hour = start + 60, start + 2 * 3600 + 60
    monkeypatch.delenv("ORCH_GEMINI_MAX_TIER", raising=False)
    assert roles.triage_trial_mode(path=trial, now=argon_hour) == "full"
    monkeypatch.setenv("ORCH_GEMINI_MAX_TIER", "mid")
    assert roles.triage_trial_mode(path=trial, now=argon_hour) is None
    assert (
        roles.triage_trial_mode(path=trial, now=flash_hour) == "mid"
    ), "the Flash arm keeps running"


def test_no_scoped_shed_prints_that_none_is_active(shed_dir):
    assert capacity.tier_ceiling_exports(environ={}) == ["echo '  tier ceilings: none active'"]
