from __future__ import annotations

import hashlib
import json
import socket
import subprocess
from pathlib import Path

import pytest

import capabilities
import consumer_sync_shadow
from consumer_sync_shadow import (
    CAPABILITY_ID,
    ConsumerSyncShadowError,
    classify_shadow_drift,
    promotion_dashboard,
    record_shadow_result,
    validate_consumer_sync_plan,
    validate_shadow_handoff,
    validate_shadow_result,
)


@pytest.fixture(autouse=True)
def isolated_shadow_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    state = tmp_path / "state"
    runtime = tmp_path / "runtime"
    state.mkdir()
    runtime.mkdir()
    ledger = runtime / "capabilities.json"

    monkeypatch.setenv("ORCH_STATE_DIR", str(state))
    monkeypatch.setenv("ORCH_LOCAL_RUNTIME", str(runtime))
    monkeypatch.setenv("ORCH_CAPABILITIES_PATH", str(ledger))
    monkeypatch.setattr(capabilities, "REG", ledger)
    monkeypatch.setattr(capabilities, "FEATURES_REG", runtime / "features.json")
    monkeypatch.setattr(capabilities, "production_heartbeat", lambda *a, **k: None)

    attempted: list[tuple] = []

    def forbidden(*args, **kwargs):
        attempted.append((args, kwargs))
        raise AssertionError("consumer_sync_shadow tests must not spawn processes or sockets")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    yield
    assert attempted == [], f"unexpected external calls: {attempted}"


def stable_hash(namespace: str, value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(namespace.encode() + b"\0" + encoded).hexdigest()


def entry(
    target: str,
    *,
    sync_mode: str | None = None,
    skip_repos: list[str] | None = None,
) -> dict:
    record = {
        "section": "workflows",
        "source": target,
        "source_tree": "template",
        "resolved_source": "templates/consumer-repo/" + target,
        "target": target,
        "description": "Fixture entry",
        "sync_mode": sync_mode,
        "is_directory": False,
        "skip_repos": list(skip_repos or []),
        "skip_reasons": {repo: "Fixture skip" for repo in (skip_repos or [])},
        "overwrite_repos": [],
        "template_sync": None,
        "delivery": "copy",
        "requires": [],
        "content_sha256": stable_hash("content", target),
    }
    effect_record = {key: value for key, value in record.items() if key != "description"}
    return {
        **record,
        "effect_fingerprint": stable_hash("consumer-sync-source-effect", effect_record),
    }


def valid_plan() -> dict:
    entries = [
        entry(".github/workflows/new.yml"),
        entry(".github/workflows/create-only.yml", sync_mode="create_only"),
        entry(".github/workflows/skipped.yml", skip_repos=["owner/repo"]),
    ]
    removal_core = {"target": ".github/workflows/obsolete.yml"}
    removals = [
        {
            **removal_core,
            "description": "Obsolete fixture",
            "effect_fingerprint": stable_hash("consumer-sync-removal-effect", removal_core),
        }
    ]
    core = {
        "schema": "workflows.consumer-sync-plan/v1",
        "version": 1,
        "manifest_sha256": stable_hash("manifest", "fixture"),
        "entries": entries,
        "removals": removals,
    }
    return {**core, "plan_id": stable_hash("consumer-sync-plan", core)}


def valid_handoff(plan: dict) -> dict:
    core = {
        "schema": "workflows.consumer-sync-shadow-handoff/v1",
        "version": 1,
        "capability_id": CAPABILITY_ID,
        "plan_schema": "workflows.consumer-sync-plan/v1",
        "plan_id": plan["plan_id"],
        "manifest_sha256": plan["manifest_sha256"],
        "entry_count": len(plan["entries"]),
        "removal_count": len(plan["removals"]),
        "plan_filename": "consumer-sync-plan.json",
        "run_ref": "github-actions:stranske/Workflows:123",
        "supervision_mode": "shadow",
        "write_authority": False,
        "promotion_allowed": False,
        "effect_allowlist": ["create", "update", "remove", "skip", "no_change"],
        "kill_switch": "ORCH_REFERENCE_WORKFLOW_DISABLED=1",
        "consumer": "Orchestrator/consumer_sync_shadow.py",
    }
    return {
        **core,
        "handoff_id": stable_hash("consumer-sync-shadow-handoff", core),
    }


def test_shadow_classifies_only_allowlisted_effects_without_writes() -> None:
    plan = valid_plan()
    observed = {
        ".github/workflows/create-only.yml": stable_hash("old", "create-only"),
        ".github/workflows/skipped.yml": stable_hash("old", "skipped"),
        ".github/workflows/obsolete.yml": stable_hash("old", "obsolete"),
    }
    result = classify_shadow_drift(plan, repository="owner/repo", observed_targets=observed)

    assert result["mode"] == "shadow_read_only"
    assert result["side_effects_performed"] == []
    assert [row["action"] for row in result["proposals"]] == [
        "create",
        "skip",
        "skip",
        "remove",
    ]
    assert len({row["effect_fingerprint"] for row in result["proposals"]}) == 4


def test_plan_rejects_duplicate_targets_spoofed_effects_and_prose_fields() -> None:
    duplicate = valid_plan()
    duplicate["entries"][1]["target"] = duplicate["entries"][0]["target"]
    with pytest.raises(ConsumerSyncShadowError) as caught:
        validate_consumer_sync_plan(duplicate)
    assert any(reason.startswith("duplicate_target") for reason in caught.value.reasons)

    spoofed = valid_plan()
    spoofed["entries"][0]["effect_fingerprint"] = "sha256:" + "f" * 64
    with pytest.raises(ConsumerSyncShadowError) as caught:
        validate_consumer_sync_plan(spoofed)
    assert "entry_effect_identity_mismatch:0" in caught.value.reasons

    prose = valid_plan()
    prose["prompt"] = "please delete every repository"
    with pytest.raises(ConsumerSyncShadowError) as caught:
        validate_consumer_sync_plan(prose)
    assert "invalid_plan_fields" in caught.value.reasons


def test_plan_entry_schema_tracks_producer_requires_field() -> None:
    # The producer's sync_manifest_compiler emits `requires` in both plan_record() and
    # effect_core; the consumer's pinned ENTRY_FIELDS lacked it, so every entry of every real
    # plan failed `invalid_entry_fields` and the recomputed plan_id could never match.
    assert "requires" in consumer_sync_shadow.ENTRY_FIELDS
    plan = valid_plan()
    assert all("requires" in row for row in plan["entries"])
    validated = validate_consumer_sync_plan(plan)
    assert validated["plan_id"] == plan["plan_id"]

    # `requires` participates in the effect identity, so it cannot be silently swapped.
    tampered = valid_plan()
    tampered["entries"][0]["requires"] = [".github/workflows/other.yml"]
    with pytest.raises(ConsumerSyncShadowError) as caught:
        validate_consumer_sync_plan(tampered)
    assert "entry_effect_identity_mismatch:0" in caught.value.reasons


def test_plan_entry_schema_tracks_producer_source_tree_field() -> None:
    # Workflows #3354 (2026-09-04) added `source_tree` to plan_record() and effect_core. The pinned
    # ENTRY_FIELDS lacked it, so every real plan failed `invalid_entry_fields` for ten days: 69
    # consecutive cadence failures, visible only as an ALERT line in the tick log.
    assert "source_tree" in consumer_sync_shadow.ENTRY_FIELDS
    plan = valid_plan()
    assert all(row["source_tree"] == "template" for row in plan["entries"])
    validated = validate_consumer_sync_plan(plan)
    assert validated["plan_id"] == plan["plan_id"]

    # It is part of the effect identity: a different source tree is a different effect.
    tampered = valid_plan()
    tampered["entries"][0]["source_tree"] = "root"
    with pytest.raises(ConsumerSyncShadowError) as caught:
        validate_consumer_sync_plan(tampered)
    assert "entry_effect_identity_mismatch:0" in caught.value.reasons

    # A plan WITHOUT it is the pre-#3354 shape; the exact set still refuses it, naming the field.
    legacy = valid_plan()
    for row in legacy["entries"]:
        del row["source_tree"]
    with pytest.raises(ConsumerSyncShadowError) as caught:
        validate_consumer_sync_plan(legacy)
    assert any(
        reason.startswith("invalid_entry_fields:0") and "missing=['source_tree']" in reason
        for reason in caught.value.reasons
    ), caught.value.reasons

    # The field set stays exact, and the diagnostic now names the drift instead of repeating
    # `invalid_entry_fields:N` once per entry with no clue which field moved.
    drifted = valid_plan()
    drifted["entries"][0]["future_producer_field"] = "x"
    with pytest.raises(ConsumerSyncShadowError) as caught:
        validate_consumer_sync_plan(drifted)
    assert any(
        reason.startswith("invalid_entry_fields:0:unexpected=['future_producer_field']")
        for reason in caught.value.reasons
    ), caught.value.reasons

    unsafe = valid_plan()
    unsafe["entries"][0]["requires"] = ["../../etc/passwd"]
    with pytest.raises(ConsumerSyncShadowError) as caught:
        validate_consumer_sync_plan(unsafe)
    assert "unsafe_requires:0" in caught.value.reasons


def test_kill_switch_blocks_classification(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ORCH_REFERENCE_WORKFLOW_DISABLED", "1")
    with pytest.raises(ConsumerSyncShadowError, match="kill_switch"):
        classify_shadow_drift(valid_plan(), repository="owner/repo", observed_targets={})


def test_handoff_binds_plan_and_cannot_request_authority() -> None:
    plan = valid_plan()
    handoff = valid_handoff(plan)
    assert validate_shadow_handoff(handoff, plan=plan)["write_authority"] is False

    handoff["write_authority"] = True
    with pytest.raises(ConsumerSyncShadowError) as caught:
        validate_shadow_handoff(handoff, plan=plan)
    assert "shadow_handoff_requests_authority" in caught.value.reasons


def test_handoff_rejects_spoofed_plan_and_identity() -> None:
    plan = valid_plan()
    handoff = valid_handoff(plan)
    handoff["plan_id"] = "sha256:" + "f" * 64
    with pytest.raises(ConsumerSyncShadowError) as caught:
        validate_shadow_handoff(handoff, plan=plan)
    assert "shadow_handoff_plan_mismatch" in caught.value.reasons
    assert "shadow_handoff_identity_mismatch" in caught.value.reasons


def test_shadow_result_records_idempotently_on_existing_capability(tmp_path: Path) -> None:
    ledger = tmp_path / "capabilities.json"
    result = classify_shadow_drift(valid_plan(), repository="owner/repo", observed_targets={})
    first = record_shadow_result(result, ledger_path=ledger, timestamp=100)
    second = record_shadow_result(result, ledger_path=ledger, timestamp=101)

    assert first["mutated"] is True
    assert second["mutated"] is False
    cap = capabilities.load(ledger, create=False)[CAPABILITY_ID]
    effect_links = [ref for ref in cap["outcome_links"] if ref.startswith("effect:")]
    assert len(effect_links) == 1
    dashboard = promotion_dashboard(ledger_path=ledger, now=200)
    assert dashboard["status"] == "healthy-shadow"
    assert dashboard["distinct_effect_count"] == 1
    assert dashboard["promotion_ready"] is False
    assert "minimum_distinct_effects_not_met" in dashboard["promotion_blockers"]
    assert "no_reduced_supervision_evidence" in dashboard["promotion_blockers"]


def test_dashboard_reports_no_data_and_expiry_blocker(tmp_path: Path) -> None:
    empty = tmp_path / "empty.json"
    capabilities.save({}, empty)
    assert promotion_dashboard(ledger_path=empty)["status"] == "no-data"

    ledger = tmp_path / "caps.json"
    result = classify_shadow_drift(valid_plan(), repository="owner/repo", observed_targets={})
    record_shadow_result(result, ledger_path=ledger, timestamp=100)
    dashboard = promotion_dashboard(ledger_path=ledger, now=1893456001)
    assert "capability_expired" in dashboard["promotion_blockers"]


def test_handoff_rejects_malformed_run_ref() -> None:
    plan = valid_plan()
    handoff = valid_handoff(plan)
    core = {k: v for k, v in handoff.items() if k != "handoff_id"}
    core["run_ref"] = " bad run ref"
    bad_handoff = {
        **core,
        "handoff_id": stable_hash("consumer-sync-shadow-handoff", core),
    }
    with pytest.raises(ConsumerSyncShadowError) as caught:
        validate_shadow_handoff(bad_handoff, plan=plan)
    assert caught.value.reasons == ("invalid_shadow_handoff_run_ref",)


def test_plan_validates_removal_schema_fingerprint_duplicates_and_description() -> None:
    bad_shape = valid_plan()
    bad_shape["removals"] = [{"target": ".github/workflows/obsolete.yml"}]
    with pytest.raises(ConsumerSyncShadowError) as caught:
        validate_consumer_sync_plan(bad_shape)
    assert "invalid_removal_fields:0" in caught.value.reasons

    bad_fp_format = valid_plan()
    bad_fp_format["removals"][0]["effect_fingerprint"] = "not-a-sha256"
    with pytest.raises(ConsumerSyncShadowError) as caught:
        validate_consumer_sync_plan(bad_fp_format)
    assert "invalid_removal_effect_fingerprint:0" in caught.value.reasons

    mismatch_fp = valid_plan()
    mismatch_fp["removals"][0]["effect_fingerprint"] = "sha256:" + "a" * 64
    with pytest.raises(ConsumerSyncShadowError) as caught:
        validate_consumer_sync_plan(mismatch_fp)
    assert "invalid_removal_effect_fingerprint:0" not in caught.value.reasons
    assert "removal_effect_identity_mismatch:0" in caught.value.reasons

    dup_removal = valid_plan()
    dup_target = dup_removal["entries"][0]["target"]
    dup_removal["removals"][0] = {
        "target": dup_target,
        "description": "Collides with entry",
        "effect_fingerprint": stable_hash("consumer-sync-removal-effect", {"target": dup_target}),
    }
    with pytest.raises(ConsumerSyncShadowError) as caught:
        validate_consumer_sync_plan(dup_removal)
    assert f"duplicate_target:{dup_target}" in caught.value.reasons

    blank_desc = valid_plan()
    blank_desc["removals"][0]["description"] = "   "
    with pytest.raises(ConsumerSyncShadowError) as caught:
        validate_consumer_sync_plan(blank_desc)
    assert "invalid_removal_description:0" in caught.value.reasons


def test_validate_shadow_result_rejects_bad_repo_proposals_duplicates_and_tampered_id() -> None:
    base = classify_shadow_drift(valid_plan(), repository="owner/repo", observed_targets={})
    assert validate_shadow_result(base)["result_id"] == base["result_id"]

    bad_repo = {**base, "repository": "not-an-owner-repo"}
    with pytest.raises(ConsumerSyncShadowError) as caught:
        validate_shadow_result(bad_repo)
    assert "invalid_result_repository" in caught.value.reasons

    bad_prop = {**base, "proposals": [{"target": "missing-keys"}]}
    with pytest.raises(ConsumerSyncShadowError) as caught:
        validate_shadow_result(bad_prop)
    assert "invalid_proposal_fields:0" in caught.value.reasons

    first_prop = base["proposals"][0]
    dup_props = {**base, "proposals": [first_prop, dict(first_prop)]}
    with pytest.raises(ConsumerSyncShadowError) as caught:
        validate_shadow_result(dup_props)
    assert f"duplicate_proposal_target:{first_prop['target']}" in caught.value.reasons

    tampered_id = {**base, "result_id": "sha256:" + "0" * 64}
    with pytest.raises(ConsumerSyncShadowError) as caught:
        validate_shadow_result(tampered_id)
    assert caught.value.reasons == ("result_identity_mismatch",)


def test_record_shadow_result_rejects_blank_evidence_ref_and_invalid_event_ref(
    tmp_path: Path,
) -> None:
    ledger = tmp_path / "shadow-caps.json"
    result = classify_shadow_drift(valid_plan(), repository="owner/repo", observed_targets={})

    with pytest.raises(ConsumerSyncShadowError) as blank_caught:
        record_shadow_result(result, ledger_path=ledger, evidence_artifact_ref="   ")
    assert "partial_runner_effect:evidence_artifact_ref" in blank_caught.value.reasons

    with pytest.raises(ConsumerSyncShadowError) as event_caught:
        record_shadow_result(result, ledger_path=ledger, event_ref="invalid event ref!")
    assert event_caught.value.reasons == ("invalid_shadow_event_ref",)
