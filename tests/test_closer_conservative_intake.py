"""Inert source intake controls; synthetic native inputs are never live origin."""

import copy
import hashlib
import json
import time
from pathlib import Path

import pytest
from test_closer_disposition import fixture

import closer_conservative_intake as intake
import closer_conservative_intake_policy as policy
import closer_correction_bridge as bridge
import closer_correction_classification as contract
import closer_correction_owner as owner
import closer_correction_producer as producer
import closer_disposition as disposition
import closer_evidence as evidence
import closer_execution_contracts as contracts
import closer_pipeline as pipeline
import closer_resources as resources
from closer_frontier_policy import action_identity, make_frontier


@pytest.fixture
def world(tmp_path, monkeypatch):
    snapshot, packet, ref, _prep, _interpret = fixture(tmp_path / "packet")
    snapshot["sources"][0]["kind"] = "issue"
    # Explicit synthetic current-source fixture; this is not a native origin.
    packet["bindings"]["source"] = evidence._hash(snapshot["sources"])
    packet["manifest"]["packet_sha256"] = evidence._packet_hash(packet)
    packet_path = tmp_path / "packet" / "evidence" / "manifests" / "synthetic-intake.json"
    packet_path.write_bytes(evidence._bytes(packet))
    ref = {"path": str(packet_path), "sha256": hashlib.sha256(packet_path.read_bytes()).hexdigest()}
    snapshot["merge"] = "d" * 40
    event = {
        "schema": "verification-event/v1",
        "repo": "owner/repo",
        "pr": "12",
        "head_sha": snapshot["head"],
        "evaluated_sha": snapshot["merge"],
        "request_sha256": "d" * 64,
        "material_sha256": "e" * 64,
    }
    event["event_id"] = contract.digest(event)
    snapshot["verifier"] = {
        "verdict": "NON_PASS",
        "run_id": 34,
        "verification_event": event,
        "raw": "Native paraphrase, not the verbatim acceptance clause.",
    }
    findings = disposition.pending_nonpass_findings(snapshot, packet)
    native = {
        "id": "raw-one",
        "evidence": {
            "ref": "comparison.json#/results/0/concerns/0",
            "sha256": hashlib.sha256(
                b"Native paraphrase, not the verbatim acceptance clause."
            ).hexdigest(),
        },
        "text": "Native paraphrase, not the verbatim acceptance clause.",
    }
    native["id"] = "raw-" + contract.digest(native["evidence"])
    inputs = {
        "source_body": snapshot["sources"][0]["body"],
        "raw_findings": [native],
        "native_seed_mapping": [
            {"seed_finding_id": native["id"], "native_finding_id": findings[0]["finding_id"]}
        ],
    }
    target_wire = {
        "repository": "owner/repo",
        "pr": "12",
        "head_sha": snapshot["head"],
        "base_sha": snapshot["base"],
        "merge_sha": snapshot["merge"],
    }
    source_wire = {
        "number": "2",
        "node_id": "I_2",
        "body_sha256": hashlib.sha256(inputs["source_body"].encode()).hexdigest(),
    }
    original_verifier = {
        "verification_event": event,
        "run_id": "34",
        "run_attempt": "1",
        "comment_id": "43",
        "comment_sha256": "a" * 64,
        "decision_sha256": "b" * 64,
    }
    seed = {
        "schema": "closer-correction-seed/v1",
        "target": target_wire,
        "source_issue": source_wire,
        "original_verifier": original_verifier,
        "finding": {k: native[k] for k in ("id", "evidence")},
        "scope": "substantive_implementation",
    }
    bundle = {
        "schema": "closer-correction-seed-bundle/v1",
        "verification_event": event,
        "assessment_identity": {
            "mode": "evaluate",
            "provider": "synthetic",
            "model": "",
            "model2": "",
            "canonical": True,
        },
        "result_sha256": "2" * 64,
        "seeds": [seed],
    }
    bundle["bundle_sha256"] = contract.digest(bundle)
    inputs["seed_bundle"] = bundle
    inputs["retained_packet_ref"] = {**ref, "bytes": len(Path(ref["path"]).read_bytes())}
    criterion = disposition._requirements(inputs["source_body"])[0]
    selected = {
        "schema": "closer-source-intake-selection/v1",
        "selection_id": "inert-source",
        "target": snapshot["target"].lower(),
        "head": snapshot["head"],
        "merge": snapshot["merge"],
        "event_id": event["event_id"],
        "source": snapshot["sources"][0]["target"].lower(),
        "source_body_sha256": hashlib.sha256(inputs["source_body"].encode()).hexdigest(),
        "criterion_sha256": hashlib.sha256(criterion.encode()).hexdigest(),
        "native_claim": copy.deepcopy(native["evidence"]),
    }
    # Explicit source-policy fixture; never a public payload/Boolean authority.
    monkeypatch.setattr(policy, "SELECTIONS", (selected,))
    monkeypatch.setattr(
        intake,
        "_DATA_SHA",
        hashlib.sha256(json.dumps(policy.SELECTIONS, sort_keys=True).encode()).hexdigest(),
    )
    return snapshot, packet, ref, tmp_path / "packet" / "evidence", inputs, selected


def test_conservative_inventory_preserves_every_unknown_and_unresolved(world):
    snapshot, packet, ref, root, inputs, selected = world
    low, actual = intake._inventory(snapshot, packet, ref, root, inputs)
    assert actual == selected
    assert [
        (r["source"], r["criterion"]) for r in low["decision"]["criteria"]
    ] == disposition._expected(snapshot)
    assert all(
        r["result"] == "UNKNOWN" and not r["evidence"] and not r["proofs"]
        for r in low["decision"]["criteria"]
    )
    assert all(
        r["status"] == "unresolved_missing_evidence" and r["evidence"] == r["proofs"] == []
        for r in low["resolutions"]
    )
    assert low["findings"] == [] and low["authority"] is False
    assert low["source_intake_selection"]["semantic_relationship_asserted"] is False
    assert not any(
        criterion in inputs["raw_findings"][0]["text"]
        for _source, criterion in disposition._expected(snapshot)
    )
    request = contracts.build_adjudication_request(
        snapshot, packet, ref, retained_dir=root, **low["adjudication_request"]
    )
    assert request["criterion"] == disposition._requirements(inputs["source_body"])[0]


@pytest.mark.parametrize(
    "damage",
    [
        "head",
        "merge",
        "event",
        "source",
        "body",
        "criterion",
        "claim_sha",
        "claim_ref",
        "duplicate_source",
        "duplicate_claim",
        "missing_mapping",
        "duplicate_selection",
    ],
)
def test_source_question_selection_rejects_forgery_or_ambiguity(world, damage):
    snapshot, packet, ref, root, inputs, selected = copy.deepcopy(world)
    selections = [selected]
    if damage in {"head", "merge"}:
        snapshot[damage] = "0" * 40
    elif damage == "event":
        snapshot["verifier"]["verification_event"]["event_id"] = "0" * 64
    elif damage == "source":
        snapshot["sources"][0]["target"] = "foreign/repo#2"
    elif damage == "body":
        snapshot["sources"][0]["body"] += " changed"
    elif damage == "criterion":
        selected["criterion_sha256"] = "0" * 64
    elif damage == "claim_sha":
        selected["native_claim"]["sha256"] = "0" * 64
    elif damage == "claim_ref":
        selected["native_claim"]["ref"] = "comparison.json#/results/1/concerns/0"
    elif damage == "duplicate_source":
        snapshot["sources"].append(copy.deepcopy(snapshot["sources"][0]))
    elif damage == "duplicate_claim":
        inputs["raw_findings"].append(copy.deepcopy(inputs["raw_findings"][0]))
    elif damage == "missing_mapping":
        inputs["native_seed_mapping"] = []
    elif damage == "duplicate_selection":
        selections.append(copy.deepcopy(selected))
    with pytest.raises(intake.IntakeUnavailable):
        intake._select(snapshot, inputs, selections)


def test_untrusted_extra_selector_and_caller_cannot_issue(world):
    snapshot, packet, ref, root, inputs, selected = world
    with pytest.raises(intake.IntakeUnavailable, match="original_pipeline_caller"):
        intake.try_from_pipeline(snapshot["target"], {"selection": selected}, lambda *_: {})
    assert (
        pipeline._source_native_intake(
            snapshot["target"], {}, lambda *_: {}, _scope_callback=lambda *_: {}
        )
        is None
    )
    assert not list(root.rglob("*.host-native-intake.json"))


def test_native_collector_failure_does_not_mint_host_origin(world, monkeypatch):
    snapshot, _packet, ref, _root, _inputs, _selected = world
    monkeypatch.setattr(intake, "_preparation", lambda _: ({"packet_ref": ref}, snapshot, {}, None))
    # Exercise the real collector's native-event/packet admission rejection.
    assert intake.ready(snapshot["target"], {}) is False


@pytest.mark.parametrize("risk", ["security", "authority_conflict", "ownership_conflict"])
def test_protected_native_metadata_still_requires_owner(world, monkeypatch, risk):
    snapshot, packet, ref, root, inputs, _selected = world
    monkeypatch.setattr(
        disposition,
        "pending_nonpass_findings",
        lambda *_: [{"finding_id": "one", "raw": {"category": risk}}],
    )
    with pytest.raises(intake.IntakeUnavailable, match="protected_owner_required"):
        intake._inventory(snapshot, packet, ref, root, inputs)


def test_host_kind_is_not_a_claude_proof_or_another_role():
    fake = {"actual": {"kind": intake.KIND, "host_intake_ref": {}}}
    with pytest.raises(owner.OwnerDecisionUnavailable, match="not a model"):
        owner._stage(
            fake,
            task="closer_exception",
            basis={},
            operation_id="x",
            now=time.time(),
            host_context={},
        )
    with pytest.raises(owner.OwnerDecisionUnavailable, match="not a model"):
        owner._stage(fake, task="closer_interpret", basis={}, operation_id="x", now=time.time())


def test_real_scope_and_lane_wrappers_issue_and_recompute_host_inventory(
    world, monkeypatch, tmp_path
):
    """Actual queue/locks/pools; native acquisition is explicitly a synthetic seam."""
    snapshot, packet, ref, root, inputs, _selected = world
    snapshot_path = root / "synthetic-snapshot.json"
    snapshot_path.write_text(json.dumps(snapshot))
    prep = {
        "snapshot": snapshot,
        "revision": snapshot["revision"],
        "packet_ref": ref,
        "snapshot_ref": {
            "path": str(snapshot_path),
            "sha256": hashlib.sha256(snapshot_path.read_bytes()).hexdigest(),
        },
    }
    monkeypatch.setattr(bridge, "collect_original_inputs", lambda *_: copy.deepcopy(inputs))
    target = snapshot["target"]
    selected = make_frontier(
        [target],
        owner="synthetic-source-owner",
        frontier_id="host-intake",
        bindings={
            target: {
                "revision": snapshot["revision"],
                "action_id": action_identity(target, snapshot["revision"], "disposition", {}),
            }
        },
    )
    events, refuses = [], []
    original_need = intake._need

    def need(ok, reason):
        if not ok:
            refuses.append(reason)
        return original_need(ok, reason)

    monkeypatch.setattr(intake, "_need", need)

    def prepare(_target, _context):
        return prep

    def interpret(*_):
        pytest.fail("source-admitted inventory called a model callback")

    def exception(_target, context):
        row, _, _ = intake._queue(context, stages={"exception"})
        low, proof = intake.revalidate(
            context["interpret"],
            context=context,
            basis=intake._basis(prep, snapshot),
            operation_id=row["stage_results"]["interpret"]["work_key"],
        )
        events.append((copy.deepcopy(context), low, proof))
        return {
            "status": "pending",
            "pending_kind": "external_event",
            "next_trigger": "real named proof still required",
            "pending_evidence": {"event_id": "synthetic-review-required"},
        }

    def act(*_):
        pytest.fail("host inventory authorized an action")

    for callback in (prepare, interpret, exception, act):
        pipeline._FINITE_DISPATCHER_CALLBACKS.add(callback)  # Explicit inert registration seam.
    result = resources.run_scoped(
        tmp_path / "host-state.json",
        [target],
        finite_frontier=selected,
        prepare=prepare,
        interpret=interpret,
        exception=exception,
        act=act,
        capacity_fn=lambda: {"io": 2, "low": 0, "exception": 1},
        resource_keys=lambda *_: [],
        request_timeout_seconds=10,
    )
    assert len(events) == 1, (result, refuses)
    context, low, proof = events[0]
    assert context["interpret"]["actual"]["provider_generation_performed"] is False
    assert context["interpret"]["requested_profile"] is None
    assert "_closer_controlled_proof" not in context["interpret"]["actual"]
    assert proof["authority"] is False and proof["kind"] == intake.KIND
    assert all(row["result"] == "UNKNOWN" for row in low["decision"]["criteria"])


@pytest.mark.parametrize("fault", ["retain", "queue", "native_after_ready"])
def test_selected_host_fault_holds_without_original_model(world, monkeypatch, tmp_path, fault):
    _fault_routing_control(world, monkeypatch, tmp_path, fault=fault, selected=True)


@pytest.mark.parametrize("changed", ["event", "head", "merge"])
def test_unselected_same_pr_event_keeps_ordinary_callback(world, monkeypatch, tmp_path, changed):
    _fault_routing_control(
        world, monkeypatch, tmp_path, fault="retain", selected=False, changed=changed
    )


def _fault_routing_control(world, monkeypatch, tmp_path, *, fault, selected, changed=None):
    snapshot, _packet, ref, root, inputs, _ = world
    if changed == "event":
        snapshot["verifier"]["verification_event"]["event_id"] = "0" * 64
    elif changed == "head":
        snapshot["head"] = "0" * 40
    elif changed == "merge":
        snapshot["merge"] = "0" * 40
    path = root / "fault-snapshot.json"
    path.write_text(json.dumps(snapshot))
    prep = {
        "snapshot": snapshot,
        "revision": snapshot["revision"],
        "packet_ref": ref,
        "snapshot_ref": {
            "path": str(path),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        },
    }
    target = snapshot["target"]
    frontier = make_frontier(
        [target],
        owner="synthetic-fault-review",
        frontier_id="host-fault",
        bindings={
            target: {
                "revision": snapshot["revision"],
                "action_id": action_identity(target, snapshot["revision"], "disposition", {}),
            }
        },
    )
    events, issuing = [], []
    private_error_text = "SYNTHETIC_CREDENTIAL_NOT_FOR_WAIT_DIAGNOSTIC"
    original_queue = intake._queue

    def queue(*args, **kwargs):
        issuing.append(True)
        if fault == "queue":
            raise OSError(private_error_text)
        return original_queue(*args, **kwargs)

    def native_inputs(*_):
        if fault == "native_after_ready" and issuing:
            raise ValueError(private_error_text)
        return copy.deepcopy(inputs)

    def failed_write(*_args, **_kwargs):
        raise OSError(private_error_text)

    monkeypatch.setattr(intake, "_queue", queue)
    monkeypatch.setattr(bridge, "collect_original_inputs", native_inputs)
    monkeypatch.setattr(producer, "retain", failed_write)

    def prepare(*_):
        return prep

    def interpret(*_):
        events.append("ORIGINAL_INTERPRET_CALLED")
        return {
            "status": "pending",
            "pending_kind": "external_event",
            "next_trigger": "no effect",
            "pending_evidence": {
                "target": target,
                "revision": snapshot["revision"],
                "event_id": "synthetic-ordinary-marker",
            },
        }

    def exception(*_):
        pytest.fail("host receipt fault advanced to STRONG")

    def act(*_):
        pytest.fail("host receipt fault granted action")

    for fn in (prepare, interpret, exception, act):
        pipeline._FINITE_DISPATCHER_CALLBACKS.add(fn)
    result = resources.run_scoped(
        tmp_path / "fault-state.json",
        [target],
        finite_frontier=frontier,
        prepare=prepare,
        interpret=interpret,
        exception=exception,
        act=act,
        capacity_fn=lambda: {"io": 2, "low": 0 if selected else 1, "exception": 1},
        resource_keys=lambda *_: [],
        request_timeout_seconds=10,
    )
    row = result["targets"][target]
    assert row["status"] == "held"
    assert events == ([] if selected else ["ORIGINAL_INTERPRET_CALLED"])
    if selected:
        result = row["results"]["interpret"]
        assert result["reason"] == "source_native_intake_unavailable"
        assert result["failure_type"] == (
            "ValueError" if fault == "native_after_ready" else "OSError"
        )
        assert private_error_text not in json.dumps(result)
        assert result["status"] == "pending" and result["pending_kind"] == "external_event"
        assert result["next_trigger"] == "reconcile_source_native_intake_before_readmission"
        assert result["provider_generation_performed"] is False
        assert result["authority"] is False
        assert "actual" not in result and "_closer_controlled_proof" not in result


@pytest.mark.parametrize("damage", [None, "evaluated_sha", "event_head", "inline_merge"])
def test_actual_compact_preparation_event_route_identity(world, damage):
    snapshot, _, _, _, _, _ = world
    compact = {
        k: snapshot.get(k)
        for k in (
            "target",
            "head",
            "base",
            "revision",
            "review_revision",
            "checks",
            "state",
            "kind",
            "active_threads",
            "mergeable",
            "verifier",
            "_local_cached",
        )
    }
    assert "merge" not in compact
    if damage == "evaluated_sha":
        compact["verifier"]["verification_event"]["evaluated_sha"] = "0" * 40
    elif damage == "event_head":
        compact["verifier"]["verification_event"]["head_sha"] = "0" * 40
    elif damage == "inline_merge":
        compact["merge"] = "0" * 40
    assert intake._selected_route_identity(snapshot["target"], {"snapshot": compact}) is (
        damage is None
    )
