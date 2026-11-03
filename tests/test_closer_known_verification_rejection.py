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
    pin = op["result"]["verification_rejection_proof"]["core_identity"]
    assert pin["schema"] == "closer-verification-core-rejection/v1"
    assert pin["namespace"] == backend.runtime_binding.bundle.namespace
    assert pin["digest_scope"] == "canonical_core_record_excluding_prior_operations"
    later_readback = backend.runtime_binding.reconcile("owner/repo#12", op)
    assert later_readback["status"] == "not_started"
    assert "verification_rejection_proof" not in later_readback
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


def test_actual_changed_revision_accepted_replacement_preserves_unique_archived_rejection_read(
    repeat, monkeypatch
):
    import closer_runtime_binding as binding

    repeat.mode = "rejected"
    native_run(repeat)
    backend = repeat.backend
    old = copy.deepcopy(
        queue._read_state(backend.state_path)["items"]["owner/repo#12"]["operation"]
    )
    repeat.world.snapshot["revision"] += "-accepted-replacement"
    repeat.mode = "accepted"
    native_run(repeat)
    state = queue._read_state(backend.state_path)
    assert state["items"]["owner/repo#12"]["operation"]["status"] == "completed"
    archived = state["items"]["owner/repo#12"]["operation_history"][-1]
    assert archived["id"] == old["id"] and archived["status"] == "abandoned"
    snapshot = backend.verification_snapshot(backend.gh.snapshot("owner/repo#12"))
    assert repeat.dispatches == 2
    for restart in (False, True):
        if restart:
            backend.runtime_binding = binding.binding_from_manifest(repeat.path)
        before = copy.deepcopy(repeat.contents.rows)
        adapter = backend.runtime_binding._adapter("owner/repo")
        read = adapter.store.read
        calls = []

        def counted(key):
            calls.append(key)
            return read(key)

        with monkeypatch.context() as patch:
            patch.setattr(adapter.store, "read", counted)
            assert backend._verification_rejection_not_accepted(snapshot, archived) is True
            assert len(calls) == 1
        assert backend._unacknowledged_verification_request(snapshot) is None
        assert repeat.contents.rows == before and repeat.dispatches == 2

    core = backend.runtime_binding.bundle.runtime
    baseline = copy.deepcopy(repeat.contents.rows)
    key = next(iter(baseline))
    original_current = json.loads(base64.b64decode(baseline[key]["content"]))
    index = next(
        i
        for i, e in enumerate(original_current["prior_operations"])
        if (e["record"].get("mutation_intent") or {})
        .get("native_binding", {})
        .get("queue_operation_id")
        == old["id"]
    )
    for damage in (
        "foreign",
        "wrong_op",
        "wrong_revision",
        "wrong_source",
        "wrong_review",
        "criteria_hash",
        "fully_rehashed_criteria",
        "duplicate",
        "hash",
        "malformed",
        "nested",
        "overflow",
        "byte_budget",
    ):
        current = copy.deepcopy(original_current)
        entry = current["prior_operations"][index]
        prior = entry["record"]
        native = prior["mutation_intent"]["native_binding"]
        if damage == "foreign":
            prior["repository"] = "foreign/repo"
        elif damage in {
            "wrong_op",
            "wrong_revision",
            "wrong_source",
            "wrong_review",
            "criteria_hash",
            "fully_rehashed_criteria",
        }:
            if damage == "wrong_op":
                native["queue_operation_id"] = "foreign-op"
            elif damage == "wrong_revision":
                native["revision"] = "foreign-revision"
            elif damage == "wrong_source":
                native["source_targets"] = ["foreign/repo#99"]
            elif damage == "wrong_review":
                native["review_revision"] = "foreign-review"
                prior["review_revision"] = "foreign-review"
            elif damage == "fully_rehashed_criteria":
                native["criteria"] = "all-fields-self-consistent-foreign-criteria"
                native["criteria_fingerprint"] = campaigns.sha256_json(native["criteria"])
                prior["criteria_fingerprint"] = native["criteria_fingerprint"]
            else:
                native["criteria"] = "different-from-record-fingerprint"
            prior["guard_versions"]["native_binding"] = campaigns.sha256_json(native)
            prior["operation_key"] = core.canonical_operation_key(core.spec_from_record(prior))
            prior["receipts"][-1]["operation_key"] = prior["operation_key"]
        elif damage == "duplicate":
            current["prior_operations"].append(copy.deepcopy(entry))
        elif damage == "hash":
            entry["sha256"] = "f" * 64
        elif damage == "malformed":
            entry["extra"] = False
        elif damage == "nested":
            prior["prior_operations"] = []
        elif damage == "overflow":
            current["prior_operations"] = [copy.deepcopy(entry) for _ in range(65)]
        else:
            current["padding"] = "x" * (1024 * 1024 + 1)
        if damage in {
            "foreign",
            "wrong_op",
            "wrong_revision",
            "wrong_source",
            "wrong_review",
            "criteria_hash",
            "fully_rehashed_criteria",
            "nested",
        }:
            entry["sha256"] = core._hash_canonical(prior)
        if damage == "fully_rehashed_criteria":
            assert core.validate_operation_record(current) == []
            assert core.verification_request_not_accepted(prior) is True
        row = copy.deepcopy(baseline[key])
        row["content"] = base64.b64encode(json.dumps(current).encode()).decode()
        repeat.contents.rows[key] = row
        assert backend._verification_rejection_not_accepted(snapshot, archived) is False, damage
        assert (
            backend._unacknowledged_verification_request(snapshot)["operation_id"] == old["id"]
        ), damage
        assert repeat.dispatches == 2
        repeat.contents.rows = copy.deepcopy(baseline)


def test_core_unknown_outcome_cannot_mint_original_rejection_pin(repeat):
    repeat.mode = "uncertain"
    native_run(repeat)
    op = queue._read_state(repeat.backend.state_path)["items"]["owner/repo#12"]["operation"]
    assert op["status"] == "uncertain" and repeat.dispatches == 1
    assert '"core_identity"' not in json.dumps(op)
    snapshot = repeat.backend.verification_snapshot(repeat.backend.gh.snapshot("owner/repo#12"))
    assert repeat.backend._unacknowledged_verification_request(snapshot)["operation_id"] == op["id"]


def test_actual_rejection_cannot_retain_or_restamp_caller_supplied_core_pin(repeat, monkeypatch):
    repeat.mode = "rejected"
    original = repeat.backend.submit_verify

    def poisoned(*args, **kwargs):
        result = original(*args, **kwargs)
        result["verification_rejection_proof"]["core_identity"] = {"schema": "caller-supplied"}
        return result

    monkeypatch.setattr(repeat.backend, "submit_verify", poisoned)
    native_run(repeat)
    op = queue._read_state(repeat.backend.state_path)["items"]["owner/repo#12"]["operation"]
    assert op["status"] == "confirmed_not_started" and repeat.dispatches == 1
    assert "core_identity" not in op["result"]["verification_rejection_proof"]
    snapshot = repeat.backend.verification_snapshot(repeat.backend.gh.snapshot("owner/repo#12"))
    assert repeat.backend._unacknowledged_verification_request(snapshot)["operation_id"] == op["id"]
