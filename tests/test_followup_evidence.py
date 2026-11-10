"""Delayed-evidence collection must preserve genuine failures and causal acknowledgments."""

import copy
import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import followup_evidence as evidence

NOW = datetime(2026, 11, 10, 12, tzinfo=timezone.utc)


def event(**changes):
    return {
        "ts": NOW.isoformat(),
        "target": "example/repo#1",
        "lane": "opener",
        "outcome": "failed",
        "cause": "branch-exists",
        **changes,
    }


def test_intact_active_archive_and_continuity_are_distinct_from_zero(tmp_path):
    instrument = tmp_path / "provision.py"
    instrument.write_text("instrument-v1")
    active = tmp_path / "provision-events.ndjson"
    active.write_text(json.dumps(event()) + "\n")
    archive = tmp_path / "provision-events.ndjson.old"
    archive.write_text(json.dumps(event(cause="branch-held")) + "\n")
    report, state = evidence.provision_report(tmp_path, {}, NOW, instrument)
    assert report["branch_refusals"] == 2
    assert report["coverage"] == "UNKNOWN"
    # Actual hourly observation receipts are required, not just old event timestamps.
    for hour in range(1, 169):
        report, state = evidence.provision_report(
            tmp_path, state, NOW + timedelta(hours=hour), instrument
        )
    assert report["coverage"] == "continuous"
    assert report["branch_refusals"] == 2  # boundary events retained
    assert not report["evidence_ready"]
    report, _ = evidence.provision_report(tmp_path, state, NOW + timedelta(hours=171), instrument)
    assert report["coverage"] == "UNKNOWN"
    assert "gap" in report["continuity_failures"][0]
    active.write_text("")
    report, _ = evidence.provision_report(tmp_path, state, NOW + timedelta(hours=169), instrument)
    assert "previous ledger prefix missing or rewritten" in report["continuity_failures"]


def test_rotation_keeps_prefixes_and_malformed_data_cannot_make_zero(tmp_path):
    instrument = tmp_path / "provision.py"
    instrument.write_text("instrument")
    active = tmp_path / "provision-events.ndjson"
    active.write_text(json.dumps(event(outcome="ok", cause="new-worktree")) + "\n")
    _, state = evidence.provision_report(tmp_path, {}, NOW, instrument)
    active.rename(tmp_path / "provision-events.ndjson.rotated")
    active.write_text("{")
    report, state = evidence.provision_report(tmp_path, state, NOW + timedelta(hours=1), instrument)
    assert report["coverage"] == "UNKNOWN"
    assert report["continuity_failures"]
    assert not report["evidence_ready"]
    active.write_text(json.dumps(event(target=evidence.EXCLUDED + "#1")) + "\n")
    report, _ = evidence.provision_report(tmp_path, state, NOW + timedelta(hours=2), instrument)
    assert report["branch_refusals"] == 0
    assert evidence.EXCLUDED not in json.dumps(report)


def receipt(tmp_path, index=1):
    run = f"{evidence.EXPERIMENT}:member:arm-{index:02}-single-cursor--member-01-cursor"
    expected = {
        "run_id": run,
        "session_id": f"original-session-{index}",
        "provider": "cursor",
        "started_at": NOW.isoformat(),
        "completed_at": (NOW + timedelta(minutes=1)).isoformat(),
    }
    segment = {
        "model": "original-served-model",
        "request_id": "original-request",
        "price_source_url": "https://provider.example/original-price-export",
        "price_valid_from": (NOW - timedelta(days=40)).isoformat(),
        "price_valid_until": (NOW + timedelta(days=1)).isoformat(),
    }
    for name in ("input", "output", "cache_read", "cache_write"):
        segment[name + "_tokens"] = 100
        segment[name + "_usd_per_million"] = 2
    raw = {
        "schema": "original-attempt-usage/v1",
        "experiment_id": evidence.EXPERIMENT,
        "attempt_run_id": run,
        "session_id": expected["session_id"],
        "provider": "cursor",
        "terminal": True,
        "coverage": "whole_attempt",
        "source_kind": "provider_export",
        "started_at": expected["started_at"],
        "completed_at": expected["completed_at"],
        "segments": [segment],
    }
    source = tmp_path / f"raw-{index}.json"
    source.write_text(json.dumps(raw))
    raw.update(raw_source_path=str(source), raw_source_sha256=evidence.digest(source.read_bytes()))
    export = tmp_path / f"run-{index}.receipt.json"
    export.write_text(json.dumps(raw))
    return expected, raw, export


def test_original_export_requires_cache_prices_terminal_binding_and_raw_equality(tmp_path):
    expected, raw, export = receipt(tmp_path)
    result = evidence.usage_receipt(raw, expected, export)
    assert result["cost_usd"] == pytest.approx(0.0008)
    assert result["brain_admitted"] is False
    for field, value in [
        ("terminal", False),
        ("session_id", "replacement-session"),
        ("coverage", "one_turn"),
    ]:
        broken = {**raw, field: value}
        with pytest.raises(ValueError, match="binding"):
            evidence.usage_receipt(broken, expected, export)
    broken = copy.deepcopy(raw)
    del broken["segments"][0]["cache_read_tokens"]
    with pytest.raises(ValueError):
        evidence.usage_receipt(broken, expected, export)
    Path(raw["raw_source_path"]).write_text("[]")
    with pytest.raises(ValueError, match="hash mismatch"):
        evidence.usage_receipt(raw, expected, export)


def test_conflicting_or_changed_originals_do_not_revive_issue(tmp_path):
    expected, raw, export = receipt(tmp_path)
    done = tmp_path / "done.json"
    done.write_text(json.dumps({"run_id": expected["run_id"], "rc": 0, "rc_of": "agent"}))
    binding = tmp_path / "native-meta.json"
    binding.write_text("original")
    expected.update(
        done_path=str(done),
        done_sha256=evidence.digest(done.read_bytes()),
        binding_path=str(binding),
        binding_sha256=evidence.digest(binding.read_bytes()),
    )
    config = {"attempts": [expected], "watch_paths": [str(done)]}
    assert evidence.strategy_report(config, tmp_path)["qualified_original_attempts"] == 1
    (tmp_path / "conflict.receipt.json").write_bytes(export.read_bytes())
    assert evidence.strategy_report(config, tmp_path)["qualified_original_attempts"] == 0
    binding.write_text("replacement")
    report = evidence.strategy_report(config, tmp_path)
    assert not report["evidence_ready"]
    assert not report["attempts"][0]["terminal_bound"]
    assert not report["original_acceptance_complete"]


def test_unknown_publication_resumes_frozen_packet_once_before_native_handoff(
    tmp_path, monkeypatch
):
    original = {"issue": 1098, "evidence_ready": True, "branch_refusals": 0}
    comments, calls = [], []
    labels = [{"name": "agents:paused"}]
    uncertain = True

    def api(method, path, payload=None):
        nonlocal uncertain
        calls.append((method, path))
        if method == "POST":
            comments.append({"body": payload["body"], "id": 123})
            if uncertain:
                uncertain = False
                raise subprocess.TimeoutExpired("gh", 45)
        if "/comments?" in path:
            return comments
        if "/issues/comments/" in path:
            return comments[0]
        if method == "DELETE":
            labels.clear()
            return None
        return {"state": "open", "labels": labels}

    monkeypatch.setattr(evidence, "gh", api)
    with pytest.raises(subprocess.TimeoutExpired):
        evidence.reconcile_ready(original, tmp_path)
    pending = evidence.load(tmp_path / "ack-1098.json")
    assert pending["state"] == "pending"
    changed = {**original, "new_collection": "different report"}
    result = evidence.reconcile_ready(changed, tmp_path)
    assert result["state"] == "acknowledged"
    assert result["evidence_sha256"] == evidence.digest(evidence.document(original))
    assert sum(method == "POST" for method, _ in calls) == 1
    calls.clear()
    evidence.reconcile_ready(changed, tmp_path)
    evidence.reconcile_ready({"issue": 434, "evidence_ready": False}, tmp_path)
    assert not calls
