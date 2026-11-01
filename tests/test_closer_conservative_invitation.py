"""One inert native-emitter basis; native acquisition/ownership are fixtures."""

import copy
import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import test_dispatcher_controlled_data_only as controlled_fixture
from closer_inert_dispatcher_support import SECOND, InertCaller
from test_closer_conservative_intake import world as world

import closer_conservative_intake as intake
import closer_correction_bridge as bridge
import closer_correction_enrollment_terminal as enrollment
import closer_correction_maintenance as maintenance
import closer_correction_ownership as ownership
import closer_correction_source_record as source_record
import closer_dispatcher as closer
import closer_execution_contracts as contracts
import closer_pipeline as pipeline
import closer_resources as resources
import dispatcher
from closer_frontier_policy import action_identity, make_frontier

controlled_world = controlled_fixture.world


@pytest.mark.parametrize("include_packet_length", [True, False])
def test_one_actual_inert_strong_and_cold_invitation_readback(
    world, controlled_world, monkeypatch, tmp_path, include_packet_length
):
    snapshot, packet, ref, root, inputs, _selection = world
    monkeypatch.setenv("HANDOFF_DIR", str(tmp_path / "handoff"))
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path / "private-state"))
    monkeypatch.setenv("ORCH_BRAIN_ROOT", str(tmp_path / "private-brain"))
    caller = InertCaller(controlled_world, monkeypatch)
    monkeypatch.setattr(bridge, "collect_original_inputs", lambda *_: copy.deepcopy(inputs))

    def observed(*, seed, **_kw):
        wire = {
            "schema": "closer-correction-ownership/v1",
            "kind": "no_incumbent",
            **bridge.effect_identity(seed),
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "evidence_refs": ["synthetic-native-ownership-fixture"],
        }
        return SimpleNamespace(
            document=lambda: {
                "ownership": wire,
                "incumbent": {"kind": "no_incumbent"},
                "remote_authenticated": False,
            }
        )

    def issue_context(_reader, repo, identity, tests, *, native=False, **_kw):
        value = {
            "repository": repo,
            **identity,
            "body": inputs["source_body"],
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "required_tests": tests,
        }
        return (value, {"synthetic_native_fixture": True}) if native else value

    monkeypatch.setattr(ownership, "collect_ownership", observed)
    monkeypatch.setattr(source_record, "_issue_context", issue_context)
    monkeypatch.setattr(
        closer,
        "_pipeline_low_route",
        lambda **_: pytest.fail("invitation started LOW/classifier model"),
    )
    snapshot_path = root / "invitation-snapshot.json"
    snapshot_path.write_text(json.dumps(snapshot))
    prep = {
        # Exact production compact prepare keys (merge is deliberately absent).
        "snapshot": {
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
        },
        "revision": snapshot["revision"],
        "packet_ref": ref,
        "snapshot_ref": {
            "path": str(snapshot_path),
            "sha256": hashlib.sha256(snapshot_path.read_bytes()).hexdigest(),
        },
    }
    if not include_packet_length:
        prep["packet_ref"] = {k: v for k, v in ref.items() if k != "bytes"}
    target, events = snapshot["target"], []
    frontier = make_frontier(
        [target],
        owner="synthetic-source-owner",
        frontier_id="invitation",
        bindings={
            target: {
                "revision": snapshot["revision"],
                "action_id": action_identity(target, snapshot["revision"], "disposition", {}),
            }
        },
    )

    def prepare(*_):
        return prep

    def interpret(*_):
        pytest.fail("host intake called broad model")

    def exception(_target, context):
        low = context["interpret"]["proposal"]
        request = contracts.build_adjudication_request(
            snapshot, packet, ref, retained_dir=root, **low["adjudication_request"]
        )
        prompt = contracts.build_adjudication_prompt(low, request, retained_dir=root)
        answer = {
            "schema": contracts.RESPONSE_SCHEMA,
            **{
                k: request[k]
                for k in (
                    "request_id",
                    "target",
                    "head",
                    "base",
                    "revision",
                    "packet_sha256",
                    "basis",
                )
            },
            "outcome": "unsatisfied",
            "reason": "The retained function is only pass.",
            "evidence": request["evidence"],
            "proofs": [
                {
                    **request["proof_identities"][0]["alternatives"][0],
                    "excerpt": "def ship_report(): pass",
                    "relevance": "The required operation has no implementation.",
                }
            ],
        }
        with resources.pool_slot(
            tmp_path / "handoff" / "closer-profile-execution",
            hashlib.sha256(b"claude-subscription").hexdigest(),
            1,
            deadline=time.time() + 10,
            finite_frontier=True,
            reservation={
                "reservation_id": "inert-strong-once",
                "frontier_id": frontier["frontier_id"],
                "target": target,
                "agent": "claude",
                "profile_id": SECOND,
            },
        ):
            actual = closer._controlled_offload(
                agent="claude",
                prompt=prompt,
                profile_id=SECOND,
                task_type="closer_exception",
                operation_id=context["work_key"],
                frontier=frontier,
                target=target,
                basis={**intake._basis(prep, snapshot), "request_id": request["request_id"]},
                deadline=context["deadline"],
                mode="assess",
                offload=lambda agent, text, **kw: caller.response(
                    {"exit": 0, "output": json.dumps(answer)}, agent, text, **kw
                ),
            )
        assert actual["exit"] == 0, actual
        stage = {
            "requested_agent": "claude",
            "requested_profile": SECOND,
            "actual": closer._worker_actual(actual),
            "proposal": answer,
        }
        correction = bridge.run_controlled_correction(
            context=context,
            preparation=prep,
            stage_result=stage,
            github=object(),
            retained_dir=root,
            route_load={},
        )
        events.append((copy.deepcopy(context), copy.deepcopy(stage), correction))
        assert correction["status"] == "materialized", correction
        return {
            **stage,
            "correction": correction,
            "status": "pending",
            "pending_kind": "external_event",
            "next_trigger": "native action not part of test",
            "pending_evidence": {"event_id": "synthetic-native-action-required"},
        }

    def act(*_):
        pytest.fail("invitation granted action authority")

    for callback in (prepare, interpret, exception, act):
        pipeline._FINITE_DISPATCHER_CALLBACKS.add(callback)  # Explicit inert registration seam.
    result = resources.run_scoped(
        tmp_path / "invitation-state.json",
        [target],
        finite_frontier=frontier,
        prepare=prepare,
        interpret=interpret,
        exception=exception,
        act=act,
        capacity_fn=lambda: {"io": 2, "low": 0, "exception": 1},
        resource_keys=lambda *_: [],
        request_timeout_seconds=20,
    )
    assert len(events) == len(caller.results) == 1, (result, caller.results)
    old_context, stage, correction = events[0]
    assert correction["provider_generation_performed"] is False
    state = json.loads(Path(old_context["state_path"]).read_text())
    row = state["frontiers"][frontier["frontier_id"]]["items"][target]
    context = {**old_context, **row["results"]}
    # No authenticated native outer B window exists in this inert fixture.
    with pytest.raises(maintenance.MaintenanceUnknown, match="historical maintenance scope"):
        source_record.retained_original_facts(context, correction["source_decision_ref"])
    proof = stage["actual"]["_closer_controlled_proof"]
    receipt = closer._read_controlled_receipt(
        proof["provider_result_ref"], dispatcher.DISPATCH_LOG_DIR
    )
    # Explicit synthetic outer-window seam; no live/native admission is claimed.
    monkeypatch.setattr(
        maintenance,
        "_historical_window",
        lambda: {"started_at": receipt["started_at"], "finished_at": receipt["completed_at"]},
    )
    values = source_record.retained_original_facts(context, correction["source_decision_ref"])
    record, arguments, materialized = values[4], values[7], values[8]
    assert record["schema"] == source_record.INVITATION_SCHEMA
    assert "classifier_run_id" not in record
    assert materialized["kind"] == intake.INVITATION_DECISION
    assert materialized["decision"]["acceptance_authority"] is False
    assert all(
        r["status"] == "unresolved_missing_evidence" for r in materialized["decision"]["findings"]
    )
    proofs = source_record.original_provider_proofs(context, arguments)
    assert len(proofs) == 1 and proofs[0]["expected"]["task_type"] == "closer_exception"
    assert proofs[0]["run_id"] == stage["actual"]["run_id"]

    snapshots = enrollment._issuance_proofs(
        context, {"materializer": arguments}, int(time.time() * 1000) + 1
    )
    facts = {
        "request": arguments["original_request"],
        "raw_findings": values[5]["inputs"]["raw_findings"],
        "seed_selector": arguments["seed_bundle"],
        "materializer": arguments,
        "original_source_context": record["original_source_context"],
        "original_source_native": record["original_source_native"],
        "original_incumbent_context": record["original_incumbent_context"],
        "source_record_ref": correction["source_decision_ref"],
        "decision": materialized["decision"],
    }
    issued = {
        "original_proofs_json": json.dumps(snapshots),
        "issued_at_ms": int(time.time() * 1000) + 1,
        "finite_frontier": frontier,
        "target": target,
        "source_record_ref": correction["source_decision_ref"],
        "original_source_record_sha256": enrollment._hash(record),
        "decision_ref": materialized["decision_ref"],
        "original_facts_sha256": enrollment._hash(facts),
    }
    replay = enrollment._proof_knowledge(
        issued,
        {
            "request": arguments["original_request"],
            "materializer": arguments,
            "seed_selector": arguments["seed_bundle"],
        },
        context,
        row,
    )
    assert replay == facts

    assert all(c["result"] == "UNKNOWN" for c in materialized["decision"]["criteria"])
    # Cold replay must not accept a rewritten proof inventory, task, source row,
    # native packet, selected meaning, or an authority-bearing document.
    doc = closer._read_controlled_receipt(arguments["request_ref"], root)
    for damage in (
        "producer",
        "authority",
        "generation",
        "work_key",
        "deadline",
        "frontier",
        "stage_identity",
        "proof_task",
        "proof_basis",
        "original_target",
        "native_findings",
        "packet",
    ):
        changed, kwargs = copy.deepcopy(doc), copy.deepcopy(arguments)
        if damage == "producer":
            changed["producer_sha256"] = "0" * 64
        elif damage == "authority":
            changed["authority"] = True
        elif damage == "generation":
            changed["provider_generation_performed"] = True
        elif damage == "work_key":
            changed["source_context"]["work_key"] = "foreign"
        elif damage == "deadline":
            changed["source_context"]["deadline"] += 100
        elif damage == "frontier":
            changed["source_context"]["finite_frontier"]["owner"] = "foreign"
        elif damage == "stage_identity":
            changed["strong_stage"]["actual"]["run_id"] = "foreign"
        elif damage == "proof_task":
            kwargs["proof"]["expected"]["task_type"] = "closer_interpret"
        elif damage == "proof_basis":
            kwargs["basis"]["head"] = "0" * 40
        elif damage == "original_target":
            changed["original_request"]["target"]["head_sha"] = "0" * 40
        elif damage == "native_findings":
            kwargs["raw_findings"] = []
        elif damage == "packet":
            kwargs["retained_packet_ref"]["sha256"] = "0" * 64
        with pytest.raises((ValueError, KeyError), match=".+"):
            intake.materialize_invitation(
                document=changed, now=receipt["completed_at"], persist=False, **kwargs
            )

    # A completed named assessment remains evidence after its original
    # assessment window, but only inside a fresh, live ACT frame. The fixture
    # already supplies an authenticated provider receipt and controlled proof;
    # this local frame is deliberately not an effects callback.
    live_context = copy.deepcopy(context)
    live_context["deadline"] = time.time() + 30
    live_row = copy.deepcopy(row)
    live_row.update(stage="act", stage_status="running", status="running")
    live_row["stage_deadline"] = live_context["deadline"]
    installation = object()
    frame_changed = [False]

    def current_frame(actual_context, actual_installation):
        assert actual_installation is installation
        if time.time() >= actual_context["deadline"]:
            raise source_record.SourceRecordUnavailable("current ACT expired")
        assert actual_context in (live_context, source_record._project_context(live_context))
        value = copy.deepcopy(live_row)
        if frame_changed[0]:
            value["identity"] = "foreign"
        return value

    monkeypatch.setattr(source_record, "_installed_current", lambda actual: actual is installation)
    monkeypatch.setattr(source_record, "_current_act_frame", current_frame)
    late_request = copy.deepcopy(arguments["original_request"])
    late_request["original_operation"]["deadline"] = datetime.fromtimestamp(
        receipt["completed_at"] + 1, timezone.utc
    ).isoformat()
    with pytest.raises(source_record.SourceRecordUnavailable, match="reader caller differs"):
        with source_record._invitation_reader_scope(live_context, installation):
            pass
    record_for_candidate, intent_for_candidate = copy.deepcopy(record), copy.deepcopy(values[5])
    record_for_candidate["owner_semantics_sha256"] = "synthetic-digest"

    def local_facts(_context, _ref):
        assert (
            source_record._invitation_completion_before_deadline(proof, late_request)
            == receipt["completed_at"]
        )
        foreign = copy.deepcopy(proof)
        foreign["run_id"] = "foreign"
        with pytest.raises(source_record.SourceRecordUnavailable, match="provider proof differs"):
            source_record._invitation_completion_before_deadline(foreign, late_request)
        frame_changed[0] = True
        with pytest.raises(source_record.SourceRecordUnavailable, match="provider proof differs"):
            source_record._invitation_completion_before_deadline(proof, late_request)
        frame_changed[0] = False
        with pytest.raises(
            source_record.SourceRecordUnavailable, match="exceeded declared deadline"
        ):
            source_record._invitation_completion_before_deadline(
                proof,
                {
                    **late_request,
                    "original_operation": {
                        **late_request["original_operation"],
                        "deadline": datetime.fromtimestamp(
                            receipt["completed_at"] - 1, timezone.utc
                        ).isoformat(),
                    },
                },
            )
        return (
            None,
            snapshot,
            packet,
            root,
            record_for_candidate,
            intent_for_candidate,
            None,
            None,
            None,
        )

    original = intent_for_candidate["original_request"]
    monkeypatch.setattr(source_record, "_local_facts", local_facts)
    monkeypatch.setattr(
        source_record,
        "_qualified_owner_projection",
        lambda *_args: (
            {
                key: original[key]
                for key in ("target", "source_issue", "original_verifier", "finding", "scope")
            },
            original["required_tests"],
            {},
        ),
    )
    monkeypatch.setattr(bridge, "_owner_semantic_digest", lambda *_args: "synthetic-digest")
    candidate = source_record.candidate_for_original_act(
        context=live_context, installation=installation
    )
    assert type(candidate) is source_record.SourceCandidate
    expired_context = {**live_context, "deadline": time.time() - 1}
    with pytest.raises(source_record.SourceRecordUnavailable, match="current ACT expired"):
        source_record.candidate_for_original_act(context=expired_context, installation=installation)
