"""Offline inherited-cutoff admission: scheduling proof never repairs an old stop."""

from __future__ import annotations

import copy
import math

import pytest
from test_closer_pipeline import callbacks, retained_read_wait_fixture, run

import claims
import closer_pipeline as pipeline
import closer_queue as queue


@pytest.fixture(autouse=True)
def isolated_claims(monkeypatch, tmp_path):
    monkeypatch.setenv("HANDOFF_DIR", str(tmp_path / "handoff"))
    monkeypatch.setattr(claims, "holder", lambda *_a, **_k: None)


def inherited_fixture(path):
    at, target, inner = retained_read_wait_fixture(path, stage="prepare")
    doc = pipeline._load(path)
    source_id, previous = next(iter(doc["runs"].items()))
    source_deadline = previous["deadline"]
    row = previous["targets"][target]
    row["last_scheduled"] = previous["created"] - 1
    row["fresh_inner_wait_admission"] = True
    pipeline._save(path, doc)
    prior = pipeline.validate_stop(path, previous["assignment"], now=lambda: at)
    recovery = pipeline._expired_invalid_round_recovery(prior, previous, path, source_id, at)
    assert recovery[0] and target in recovery[1]
    audit = recovery[-1]["history_entry"]
    assert audit["targets"][target]["recovery"] == "retry_fresh"
    previous["invalid_stop_history"].append(audit)
    # Historical-format input: the next cadence inherited the exact resource and
    # audited wait, never scheduled its prepare, then the old normalizer changed
    # queued/prepare into deferred without supplying a new cutoff observation.
    # No historical deadline or resource is manufactured for that later cadence.
    previous["created"] = source_deadline + 10
    previous["deadline"] = source_deadline + 20
    previous["receipt_summary"]["finished_at"] = previous["deadline"] + 1
    row["last_scheduled"] = source_deadline - 1
    pipeline._save(path, doc)
    return previous["deadline"] + 2, target, inner


def test_inherited_unstarted_prepare_reenters_only_fresh_admission_and_preserves_audit(tmp_path):
    path = tmp_path / "pipeline.json"
    at, target, inner = inherited_fixture(path)
    doc = pipeline._load(path)
    run_id, previous = next(iter(doc["runs"].items()))
    old_row = copy.deepcopy(previous["targets"][target])
    history = copy.deepcopy(previous["invalid_stop_history"])
    assert pipeline._inherited_unstarted_wait_deferral(previous, target, old_row, history)
    prior = pipeline.validate_stop(path, previous["assignment"], now=lambda: at)
    old_reason = f"unverified_deadline_boundary:{target}"
    assert old_reason in prior["reasons"] and not prior["valid"]
    recovered = pipeline._expired_invalid_round_recovery(prior, previous, path, run_id, at)
    assert recovered[0] and target in recovered[1]
    assert previous["targets"][target] == old_row
    assert previous["invalid_stop_history"] == history
    assert old_reason in recovered[-1]["prior_stop"]["reasons"]
    assert recovered[-1]["targets"][target]["resource_evidence"] == old_row["resource_evidence"]
    original_native = inner.read_bytes()
    prepared = []

    def prepare(name, _context):
        prepared.append(name)
        return {
            "revision": old_row["revision"],
            "identity": old_row["identity"],
            "snapshot": old_row["results"]["prepare"]["snapshot"],
        }

    result = run(
        path,
        [target],
        {**callbacks(act=lambda *_: old_row["results"]["act"]), "prepare": prepare},
        seconds=10,
        now=lambda: at,
    )
    assert result.get("status") != "blocked" and prepared == [target]
    assert result["targets"][target]["deferred_reason"] == "validated_inner_wait"
    current = next(iter(pipeline._load(path)["runs"].values()))
    assert all(entry in current["invalid_stop_history"] for entry in history)
    assert old_reason in current["prior_invalid_stop_evidence"]["reasons"]
    assert inner.read_bytes() == original_native


@pytest.mark.parametrize(
    "mutation",
    [
        "scheduled_current",
        "scheduled_bool",
        "scheduled_nan",
        "created_bool",
        "created_nan",
        "interpret_stage",
        "running",
        "uncertain_provider",
        "known_action",
        "fresh_false",
        "fresh_missing",
        "resource_changed",
        "later_deadline",
        "deadline_bool",
        "observed_nan",
        "observed_current",
        "trigger",
        "missing_audit",
        "conflicting_audit",
        "audit_status",
        "audit_stage",
        "audit_recovery",
        "audit_resource",
        "audit_revision",
        "audit_identity",
        "audit_work_key",
        "audit_act",
        "audit_source_missing",
    ],
)
def test_inherited_prepare_admission_requires_exact_old_unstarted_audit(tmp_path, mutation):
    path = tmp_path / "pipeline.json"
    _at, target, _inner = inherited_fixture(path)
    previous = next(iter(pipeline._load(path)["runs"].values()))
    item = previous["targets"][target]
    history = previous["invalid_stop_history"]
    audit = history[-1]["targets"][target]
    if mutation == "scheduled_current":
        item["last_scheduled"] = previous["created"]
    elif mutation == "scheduled_bool":
        item["last_scheduled"] = True
    elif mutation == "scheduled_nan":
        item["last_scheduled"] = math.nan
    elif mutation == "created_bool":
        previous["created"] = True
    elif mutation == "created_nan":
        previous["created"] = math.nan
    elif mutation == "interpret_stage":
        item["stage"] = "interpret"
    elif mutation == "running":
        item["status"] = "running"
    elif mutation == "uncertain_provider":
        item["provider_outcome_uncertain"] = {"actual": {"run_id": "unknown-callback"}}
    elif mutation == "known_action":
        item["known_completed_action"] = True
    elif mutation == "fresh_false":
        item["fresh_inner_wait_admission"] = False
    elif mutation == "fresh_missing":
        item.pop("fresh_inner_wait_admission")
    elif mutation == "resource_changed":
        item["resource_evidence"]["observed_at"] += 0.5
    elif mutation == "later_deadline":
        item["resource_evidence"].update(
            deadline=previous["created"], observed_at=previous["created"]
        )
    elif mutation == "deadline_bool":
        item["resource_evidence"]["deadline"] = True
    elif mutation == "observed_nan":
        item["resource_evidence"]["observed_at"] = math.nan
    elif mutation == "observed_current":
        item["resource_evidence"]["observed_at"] = previous["created"]
    elif mutation == "trigger":
        item["deferred_next_trigger"] = "retry_provider"
    elif mutation == "missing_audit":
        history = []
    elif mutation == "conflicting_audit":
        conflicting = copy.deepcopy(history[-1])
        conflicting["targets"][target]["work_key"] = "different-cadence"
        history.append(conflicting)
    elif mutation == "audit_source_missing":
        history[-1].pop("source_run_id")
    else:
        field = mutation.removeprefix("audit_")
        if field == "act":
            audit["act_result"]["status"] = "completed"
        elif field == "resource":
            audit["resource_evidence"]["observed_at"] += 1
        else:
            audit[field] = "unrelated"
    assert not pipeline._inherited_unstarted_wait_deferral(previous, target, item, history)


@pytest.mark.parametrize("tamper", ["head", "receipt", "operation"])
def test_inherited_resource_cannot_bypass_invalid_native_wait_proof(tmp_path, tamper):
    path = tmp_path / "pipeline.json"
    at, target, inner = inherited_fixture(path)
    previous = next(iter(pipeline._load(path)["runs"].values()))
    # The scheduling helper is deliberately insufficient without the independent
    # native proof checked by the full recovery path.
    assert pipeline._inherited_unstarted_wait_deferral(
        previous, target, previous["targets"][target], previous["invalid_stop_history"]
    )
    native = queue._read_state(inner)
    if tamper == "head":
        native["items"][target]["evidence"]["head"] = "c" * 40
    elif tamper == "receipt":
        native["round"]["receipt"]["valid"] = False
    else:
        native["items"][target]["operation"] = {"id": "unresolved", "status": "uncertain"}
    queue._atomic_write(inner, native)
    before = pipeline._load(path)
    old_id, previous = next(iter(before["runs"].items()))
    prior = pipeline.validate_stop(path, previous["assignment"], now=lambda: at)
    assert not pipeline._expired_invalid_round_recovery(prior, previous, path, old_id, at)[0]
    assert pipeline._load(path) == before
