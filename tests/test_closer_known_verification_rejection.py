"""Owned actual HTTP/Core rejection provenance, never a remote/provider effect."""

import base64
import copy
import json

import pytest
from test_closer_accepted_request_wait import run as accepted_run
from test_closer_accepted_request_wait import submit
from test_closer_accepted_request_wait import world as world
from test_native_runtime_repeat_composition import (
    capacity_host as capacity_host,
)
from test_native_runtime_repeat_composition import (
    host as host,
)
from test_native_runtime_repeat_composition import (
    native_world as native_world,
)
from test_native_runtime_repeat_composition import (
    public_merge_world as public_merge_world,
)
from test_native_runtime_repeat_composition import (
    repeat as repeat,
)
from test_native_runtime_repeat_composition import (
    run as native_run,
)
from test_native_runtime_repeat_composition import (
    runtime_request as runtime_request,
)

import closer_campaign as campaigns
import closer_dispatcher as dispatcher
import closer_queue as queue


def rejected(world):
    backend, remote, path, frontier = world
    remote.reject = True
    accepted_run(backend, path, frontier, lambda *_: submit(backend, remote))
    state = queue._read_state(path)
    op = state["frontiers"][frontier["frontier_id"]]["items"][remote.current["target"]]["operation"]
    assert op["status"] == "confirmed_not_started" and len(remote.posts) == 1
    return backend, remote, path, frontier, state, op


def test_actual_http422_live_invocation_proof_is_bound_and_no_preparation_effect(world):
    backend, remote, _path, _frontier, _state, op = rejected(world)
    proof = op["result"]["verification_rejection_proof"]
    assert proof["operation_id"] == op["id"] and proof["revision"] == op["revision"]
    assert proof["binding_sha256"] == dispatcher.digest(op["binding"])
    assert proof["post_attempted"] is True and proof["request_accepted"] is False
    assert backend._verification_rejection_not_accepted(remote.current, op) is True
    assert backend._unacknowledged_verification_request(remote.current) is None
    assert len(remote.posts) == 1


@pytest.mark.parametrize(
    "damage",
    [
        "missing",
        "booleans_only",
        "foreign_id",
        "foreign_revision",
        "foreign_target",
        "wrong_binding",
        "wrong_namespace",
        "wrong_status",
        "float_status",
        "wrong_basis",
        "changed_valid_basis",
        "extra_rejection_field",
        "nominal_false",
        "nominal_true",
        "native_unknown",
        "accepted",
    ],
)
def test_rejection_markers_without_exact_producer_identity_cannot_clear_fence(world, damage):
    backend, remote, path, _frontier, state, op = rejected(world)
    result = op["result"]
    proof = result["verification_rejection_proof"]
    marker = result["verification_rejected"]
    if damage == "missing":
        result.pop("verification_rejection_proof")
    elif damage == "booleans_only":
        result.clear()
        result.update(core_rejected_not_accepted=True, no_effect=True)
    elif damage == "foreign_id":
        proof["operation_id"] = "foreign"
    elif damage == "foreign_revision":
        proof["revision"] = "foreign"
    elif damage == "foreign_target":
        proof["target"] = "foreign/repo#1"
    elif damage == "wrong_binding":
        proof["binding_sha256"] = "f" * 64
    elif damage == "wrong_namespace":
        proof["core_namespace"] = "foreign-core"
    elif damage == "wrong_status":
        marker["http_status"] = 403
    elif damage == "float_status":
        marker["http_status"] = 422.0
    elif damage == "wrong_basis":
        marker["request_basis"] = "wrong"
    elif damage == "changed_valid_basis":
        marker["request_basis"] = "f" * 64
    elif damage == "extra_rejection_field":
        marker["request_accepted"] = True
    elif damage == "nominal_false":
        proof["request_accepted"] = 0
    elif damage == "nominal_true":
        proof["post_attempted"] = 1
    elif damage == "native_unknown":
        result["native_result"] = {"status": "unknown"}
    else:
        result["verification_post_accepted"] = {"request_basis": marker["request_basis"]}
    if "verification_rejection_proof" in result:
        proof["rejection_sha256"] = dispatcher.digest(
            marker
        )  # Recomputed hash cannot repair provenance.
    path.write_text(json.dumps(state))
    assert backend._unacknowledged_verification_request(remote.current)["operation_id"] == op["id"]
    assert len(remote.posts) == 1


def test_outside_live_queue_invocation_cannot_mint_original_operation_proof(world):
    backend, remote, _path, _frontier = world
    remote.reject = True
    result = submit(backend, remote)
    assert result["status"] == "not_started" and "verification_rejected" in result
    assert "verification_rejection_proof" not in result


def test_actual_core_receipt_read_is_pure_and_rebound_guard_consistent_record_is_rejected(
    repeat, monkeypatch
):
    repeat.mode = "rejected"
    native_run(repeat)
    backend = repeat.backend
    state = queue._read_state(backend.state_path)
    op = state["items"]["owner/repo#12"]["operation"]
    snapshot = backend.verification_snapshot(backend.gh.snapshot("owner/repo#12"))
    adapter = backend.runtime_binding._adapter("owner/repo")
    for name in ("claim", "record_intent", "reject_not_accepted", "finalize"):
        if hasattr(adapter, name):
            monkeypatch.setattr(adapter, name, lambda *a, **k: pytest.fail("readback wrote Core"))
    before = copy.deepcopy(repeat.contents.rows)
    assert backend._verification_rejection_not_accepted(snapshot, op) is True
    assert repeat.contents.rows == before and repeat.dispatches == 1
    core = backend.runtime_binding.bundle.runtime
    for field in (
        "queue_operation_id",
        "revision",
        "record_head_sha",
        "record_criteria_fingerprint",
        "record_review_revision",
    ):
        key = next(iter(before))
        row = copy.deepcopy(before[key])
        record = json.loads(base64.b64decode(row["content"]))
        native = record["mutation_intent"]["native_binding"]
        if field.startswith("record_"):
            name = field.removeprefix("record_")
            record[name] = "c" * 40 if name == "head_sha" else "f" * 64
            if name == "head_sha":
                record["mutation_intent"]["head_sha"] = record[name]
                record["receipts"][-1]["head_sha"] = record[name]
        else:
            native[field] = "foreign-original-operation"
        record["guard_versions"]["native_binding"] = campaigns.sha256_json(native)
        context = record.get("verification_context") or {}
        spec = core.OperationSpec(
            record["repository"],
            record["target"]["number"],
            record["action"],
            record["head_sha"],
            record.get("base_sha"),
            record["criteria_fingerprint"],
            record["review_revision"],
            record["guard_versions"],
            required_capabilities=record.get("required_capabilities", []),
            blocking_labels=record.get("blocking_labels", []),
            verification_event=context.get("event"),
            verification_event_action=context.get("action"),
        )
        record["operation_key"] = core.canonical_operation_key(spec)
        record["receipts"][-1]["operation_key"] = record["operation_key"]
        assert core.verification_request_not_accepted(record) is True
        row["content"] = base64.b64encode(json.dumps(record).encode()).decode()
        repeat.contents.rows[key] = row
        assert backend._verification_rejection_not_accepted(snapshot, op) is False
        assert backend._unacknowledged_verification_request(snapshot)["operation_id"] == op["id"]
        repeat.contents.rows = copy.deepcopy(before)
    key = next(iter(before))
    row = copy.deepcopy(before[key])
    record = json.loads(base64.b64decode(row["content"]))
    record["denial"] = {"no_fallback": True}
    row["content"] = base64.b64encode(json.dumps(record).encode()).decode()
    repeat.contents.rows[key] = row
    assert core.verification_request_not_accepted(record) is True
    assert backend._verification_rejection_not_accepted(snapshot, op) is False
    repeat.contents.rows = copy.deepcopy(before)
    assert repeat.dispatches == 1


def test_real_rejected_legacy_operation_abandoned_by_revision_retains_bound_no_effect_proof(
    tmp_path, monkeypatch
):
    from test_closer_queue import _run
    from test_closer_verification_ack_barrier import world as legacy_world

    backend, snapshot = legacy_world(tmp_path, monkeypatch, "post")
    posts = []

    def api(endpoint, **kwargs):
        if kwargs.get("method") == "POST":
            posts.append(endpoint)
            raise dispatcher.GitHubNoEffectRejected("synthetic exact 422")
        return {"default_branch": "main"}

    backend.gh.api = api
    monkeypatch.setattr(
        backend, "prepare_verify", dispatcher.Closer.prepare_verify.__get__(backend)
    )
    monkeypatch.setattr(backend, "_verification_preflight", lambda *a: None)
    target = snapshot["target"]
    _run(
        backend.state_path,
        [target],
        lambda _: copy.deepcopy(snapshot),
        backend.execute,
        max_passes=1,
    )
    original = queue._read_state(backend.state_path)["items"][target]["operation"]
    assert (
        original["status"] == "confirmed_not_started"
        and "verification_rejection_proof" in original["result"]
    )
    changed = {**snapshot, "revision": "r2", "action": "wait", "eligible": False}
    _run(
        backend.state_path,
        [target],
        lambda _: copy.deepcopy(changed),
        backend.execute,
        max_passes=1,
    )
    archived = queue._read_state(backend.state_path)["items"][target]["operation_history"][-1]
    assert archived["status"] == "abandoned"
    assert archived["result"] == original["result"]
    assert backend._unacknowledged_verification_request(snapshot) is None
    assert backend.prepare_verify(target, snapshot)["mutation_kind"] == "workflow_dispatch"
    assert len(posts) == 1
