"""Offline successive-cadence regressions for retained native wait read deferrals."""

from __future__ import annotations

import copy
import hashlib

import pytest
from test_closer_pipeline import (
    FakeRoundCapacityDeferred,
    callbacks,
    compact_paused_wait_fixture,
    interrupted_round_fixture,
    run,
)

import claims
import closer_pipeline as pipeline


def recovered_wait(path, *, valid_stop=False):
    """Create the audit/recovery anchors via the real coordinator and native queue."""
    at, target, inner_path = compact_paused_wait_fixture(path)
    state = pipeline._load(path)
    original = state["runs"]["interrupted"]
    row = original["targets"][target]
    audited = original["invalid_stop_history"][0]["targets"][target]
    row["results"] = {
        "prepare": {
            "revision": row["revision"],
            "snapshot": {"target": target, "revision": row["revision"], "head": "b" * 40},
        },
        "act": copy.deepcopy(audited["act_result"]),
    }
    original["invalid_stop_history"] = []
    if valid_stop:
        # An expired read-capacity receipt plus the native wait creates a real
        # recovery audit. It is absent from the next assignment, so the new stop
        # is valid without introducing or clearing any uncertainty/quarantine.
        read_capacity = "owner/repo#capacity-0"
        original["targets"] = {target: row, read_capacity: original["targets"][read_capacity]}
        original["assignment"] = [read_capacity]
        original["carry_targets"] = [target]
    state["carry"] = {}
    pipeline._save(path, state)
    initial = run(path, ["fresh"], callbacks(), seconds=10, now=lambda: at)
    assert initial.get("status") != "blocked"
    old = next(iter(pipeline._load(path)["runs"].values()))
    return at, target, inner_path, old


@pytest.mark.parametrize("reason", ["worker_capacity", "provider_failure", "github_capacity"])
@pytest.mark.parametrize(
    ("valid_stop", "tamper"),
    [
        (False, None),
        (False, "resource"),
        (False, "trigger"),
        (False, "head"),
        (False, "audit"),
        (True, None),
    ],
)
def test_audited_wait_survives_three_natural_read_deferral_cadences(
    tmp_path, monkeypatch, reason, tamper, valid_stop
):
    # Isolate every external claim boundary; queue/pipeline/validators stay real.
    monkeypatch.setenv("HANDOFF_DIR", str(tmp_path / "handoff"))
    monkeypatch.setattr(claims, "holder", lambda *_: None)
    monkeypatch.setattr(claims, "claim", lambda *a, **kw: True)
    monkeypatch.setattr(claims, "release", lambda *a, **kw: True)
    monkeypatch.setattr(claims, "update_metadata", lambda *a, **kw: True)
    path = tmp_path / "pipeline.json"
    at, target, inner, original = recovered_wait(path, valid_stop=valid_stop)
    original_act = copy.deepcopy(original["targets"][target]["results"]["act"])
    original_audit = copy.deepcopy(original["invalid_stop_history"])
    original_anchor = copy.deepcopy(original["recovered_invalid_stop"])
    original_inner_sha = hashlib.sha256(inner.read_bytes()).hexdigest()
    quarantines = {
        name: copy.deepcopy(row["recovery_barrier"])
        for name, row in original["targets"].items()
        if row.get("recovery_barrier")
    }
    uncertain = {
        name for name, row in original["targets"].items() if row.get("status") == "uncertain"
    }
    assert len(quarantines) == (0 if valid_stop else 3)
    assert len(uncertain) == (0 if valid_stop else 2)
    prepared_calls = []
    interpreted_calls = []

    def prepare(name, _context):
        assert name == target
        prepared_calls.append(name)
        if reason == "github_capacity":
            raise FakeRoundCapacityDeferred(
                resource="core",
                throttle={
                    "resource": "core",
                    "state": "shed",
                    "action": "defer",
                    "remaining": 50,
                    "limit": 5000,
                    "reset_in_s": 4200,
                    "source": "call",
                    "reason": "offline measured SHED",
                },
                observed_at=now,
                reset_at_epoch=now + 4200,
            )
        return {
            "revision": original["targets"][target]["revision"],
            "identity": original["targets"][target]["identity"],
            "snapshot": copy.deepcopy(
                original["targets"][target]["results"]["prepare"]["snapshot"]
            ),
        }

    def interpret(name, _context):
        assert reason == "provider_failure", "zero low capacity must not dispatch a worker"
        interpreted_calls.append(name)
        # This is the external transport's typed failure contract, not a provider
        # invocation. The production stage handler validates and retains it.
        return {
            "status": "deferred",
            "reason": "provider_failure",
            "next_trigger": "retry_other_low_profile",
            "resource_evidence": {
                "provider_failure": {
                    "profile_id": "offline-low",
                    "agent": "gemini",
                    "run_id": f"offline-native-attempt-{len(interpreted_calls)}",
                    "category": "transport",
                    "evidence_hash": "f" * 64,
                    "exit": 70,
                }
            },
        }

    for cadence in range(1, 4):
        now = at + cadence * 20
        result = run(
            path,
            [target],
            {**callbacks(interpret=interpret), "prepare": prepare},
            capacity={"io": 1, "low": 0 if reason == "worker_capacity" else 1, "exception": 1},
            seconds=10,
            now=lambda: now,
            round_capacity_exception=FakeRoundCapacityDeferred,
        )
        assert result.get("status") != "blocked", result
        current = next(iter(pipeline._load(path)["runs"].values()))
        row = current["targets"][target]
        assert row["status"] == "deferred"
        assert row["stage"] == ("prepare" if reason == "github_capacity" else "interpret")
        assert row["deferred_reason"] == reason
        assert row["results"]["act"] == original_act
        assert row["work_key"] != original["targets"][target]["work_key"]
        if reason == "worker_capacity":
            assert pipeline._valid_worker_capacity(row["resource_evidence"], now)
        elif reason == "provider_failure":
            assert pipeline._valid_provider_failure(row["resource_evidence"])
        else:
            assert pipeline._valid_github_capacity_evidence(row["resource_evidence"], now)
        assert current["invalid_stop_history"][: len(original_audit)] == original_audit
        # Global recovery metadata can advance for unrelated barrier rows. The
        # original target anchor and its immutable source audit must survive.
        current_anchor = current["recovered_invalid_stop"]["targets"][target]
        for key, value in original_anchor["targets"][target].items():
            assert current_anchor[key] == value
        assert any(
            entry.get("targets", {}).get(target, {}).get("item_sha256")
            == current_anchor["previous_item_sha256"]
            and entry["targets"][target]["act_result"] == original_act
            for entry in current["invalid_stop_history"]
        )
        assert current["prior_invalid_stop_evidence"]["valid"] is False
        assert hashlib.sha256(inner.read_bytes()).hexdigest() == original_inner_sha
        assert {
            name: row["recovery_barrier"]
            for name, row in current["targets"].items()
            if row.get("recovery_barrier")
        } == quarantines
        assert {
            name for name, row in current["targets"].items() if row.get("status") == "uncertain"
        } == uncertain
        # The mixed round remains honestly invalid because its original barriers
        # persist, but the next cadence may recover the proved read-only rows.
        prior = pipeline.validate_stop(path, current["assignment"], now=lambda: now + 20)
        assert prior["valid"] is valid_stop
        expected = {f"unresolved_pipeline_work:{name}:failed" for name in quarantines}
        assert set(prior["reasons"]) == expected
        if not valid_stop:
            recovery = pipeline._expired_invalid_round_recovery(
                prior, current, path, result["run_id"], now + 20
            )
            assert recovery[0] and target in recovery[1]
            assert recovery[3] == set(quarantines)
            assert recovery[4] == uncertain
    assert len(prepared_calls) == 3
    assert len(interpreted_calls) == (3 if reason == "provider_failure" else 0)

    if tamper and not valid_stop:
        changed = pipeline._load(path)
        current = next(iter(changed["runs"].values()))
        row = current["targets"][target]
        if tamper == "resource":
            row["resource_evidence"] = {}
        elif tamper == "trigger":
            row["deferred_next_trigger"] = ""
        elif tamper == "head":
            row["results"]["prepare"]["snapshot"]["head"] = "c" * 40
        else:
            anchor = current["recovered_invalid_stop"]["targets"][target]
            origin = next(
                entry["targets"][target]
                for entry in current["invalid_stop_history"]
                if entry.get("targets", {}).get(target, {}).get("item_sha256")
                == anchor["previous_item_sha256"]
            )
            origin["act_result"] = {"status": "completed", "target_complete": True}
        pipeline._save(path, changed)
        prior = pipeline.validate_stop(path, current["assignment"], now=lambda: now + 20)
        assert not pipeline._expired_invalid_round_recovery(
            prior, current, path, result["run_id"], now + 20
        )[0]
        assert hashlib.sha256(inner.read_bytes()).hexdigest() == original_inner_sha


def test_unstarted_read_keeps_measured_github_capacity_across_recovery(tmp_path, monkeypatch):
    monkeypatch.setenv("HANDOFF_DIR", str(tmp_path / "handoff"))
    monkeypatch.setattr(claims, "holder", lambda *_: None)
    path = tmp_path / "pipeline.json"
    at = 1_800_000_000.0
    interrupted_round_fixture(path, at)
    warm = run(path, ["fresh"], callbacks(), seconds=10, now=lambda: at)
    assert warm.get("status") != "blocked"
    original = next(iter(pipeline._load(path)["runs"].values()))
    original_audit = copy.deepcopy(original["invalid_stop_history"])
    target = "owner/repo#new-read"

    def prepare(*_):
        raise FakeRoundCapacityDeferred(
            resource="core",
            throttle={
                "resource": "core",
                "state": "shed",
                "action": "defer",
                "remaining": 50,
                "limit": 5000,
                "reset_in_s": 4200,
                "source": "call",
                "reason": "offline measured SHED",
            },
            observed_at=now,
            reset_at_epoch=now + 4200,
        )

    for cadence in range(1, 4):
        now = at + cadence * 20
        result = run(
            path,
            [target],
            {**callbacks(), "prepare": prepare},
            seconds=10,
            now=lambda: now,
            round_capacity_exception=FakeRoundCapacityDeferred,
        )
        assert result.get("status") != "blocked", result
        current = next(iter(pipeline._load(path)["runs"].values()))
        row = current["targets"][target]
        assert row["status"] == "deferred" and row["stage"] == "prepare"
        assert row["deferred_reason"] == "github_capacity"
        assert "act" not in row["results"]
        assert pipeline._valid_github_capacity_evidence(row["resource_evidence"], now)
        assert row["deferred_next_trigger"] == f"retry_after_github_reset:{now + 4200:.3f}"
        assert current["invalid_stop_history"][: len(original_audit)] == original_audit
        prior = pipeline.validate_stop(path, current["assignment"], now=lambda: now + 20)
        recovery = pipeline._expired_invalid_round_recovery(
            prior, current, path, result["run_id"], now + 20
        )
        assert recovery[0] and target in recovery[1]
        assert len(recovery[3]) == 3 and len(recovery[4]) == 2
