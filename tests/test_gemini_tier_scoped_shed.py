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


def test_marker_text_cannot_execute_when_the_tick_evals_the_exports(shed_dir, tmp_path):
    import subprocess

    pwned = tmp_path / "pwned"
    (shed_dir / "gemini").write_text(
        json.dumps(
            {
                "category": "quota",
                "created_at": 1,
                "expires_at": int(time.time()) + 600,
                "tier_scope": "full",
                "model": f"x'; touch {pwned}; echo '$(touch {pwned})`touch {pwned}`",
            }
        )
    )
    (shed_dir / "bad;name").write_text(
        json.dumps(
            {
                "category": "quota",
                "created_at": 1,
                "expires_at": int(time.time()) + 600,
                "tier_scope": "full",
            }
        )
    )
    lines = capacity.tier_ceiling_exports(environ={})
    assert not any(
        "bad;name" in line for line in lines
    ), "an agent name that is not a variable is skipped"
    out = subprocess.run(
        ["bash", "-c", 'eval "$1"; echo "cap=$ORCH_GEMINI_MAX_TIER"', "_", "\n".join(lines)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert not pwned.exists(), "marker text executed"
    assert "cap=mid" in out and "capped at mid" in out


LANE_REFUSAL = (
    "error: Individual quota reached. Please upgrade your subscription to increase your limits. "
    "Resets in 119h11m23s.\n"
    'AGY_ERROR: {"short_error":"RESOURCE_EXHAUSTED (code 429): Individual quota reached."}'
)


def _scoped_marker(shed_dir, scope="full"):
    payload = {"category": "quota", "created_at": time.time(), "expires_at": time.time() + 3600}
    if scope:
        payload |= {"tier_scope": scope, "model": "gemini-4-argon-high"}
    (shed_dir / "gemini").write_text(json.dumps(payload))


def test_lane_dispatch_without_the_tick_env_is_capped_by_the_marker(shed_dir):
    """2026-11-09: closer/opener dispatches never ran the tick's export and went to Argon."""
    _scoped_marker(shed_dir)
    assert adapters.tier_ceiling("gemini") == "mid"
    assert adapters.effective_tier("gemini", "full") == "mid"
    model = adapters.resolve_model("gemini", adapters.effective_tier("gemini", "full"))
    assert "argon" not in model.lower()
    # What a lane dispatch actually sends: the command build_command produces for mode=full.
    argv = " ".join(map(str, adapters.build_command("gemini", "x", mode="full")))
    assert "argon" not in argv.lower() and "flash" in argv.lower()


def test_whole_seat_or_absent_marker_sets_no_ceiling(shed_dir):
    assert adapters.tier_ceiling("gemini") is None
    _scoped_marker(shed_dir, scope=None)
    assert adapters.tier_ceiling("gemini") is None


def test_explicit_owner_ceiling_still_wins_over_the_marker(shed_dir, monkeypatch):
    _scoped_marker(shed_dir)
    monkeypatch.setenv("ORCH_GEMINI_MAX_TIER", "cheap")
    assert adapters.tier_ceiling("gemini") == "cheap"


def _runs_db(tmp_path, monkeypatch, run_id, model):
    import sqlite3

    import feedback

    db = tmp_path / "feedback.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE runs (run_id TEXT, ts REAL, model TEXT)")
        conn.execute("INSERT INTO runs VALUES (?, ?, ?)", (run_id, time.time(), model))
    monkeypatch.setattr(feedback, "DB_PATH", db)


def test_refusal_without_init_event_is_scoped_from_the_recorded_run_model(
    shed_dir, tmp_path, monkeypatch
):
    """Workflows#4515: the 429 came before agy's init event, so the evidence named no model."""
    _runs_db(tmp_path, monkeypatch, "run-lane", "agy:gemini-4-argon-high")
    _record(LANE_REFUSAL, "run-lane")
    marker = _marker(shed_dir)
    assert marker["tier_scope"] == "full" and marker["model"] == "gemini-4-argon-high"
    assert capacity._shed("gemini") is False


def test_refusal_without_init_event_on_a_flash_run_still_sheds_the_seat(
    shed_dir, tmp_path, monkeypatch
):
    _runs_db(tmp_path, monkeypatch, "run-flash", "agy:gemini-3.8-flash-high")
    _record(LANE_REFUSAL, "run-flash")
    assert "tier_scope" not in _marker(shed_dir) or _marker(shed_dir)["tier_scope"] is None
