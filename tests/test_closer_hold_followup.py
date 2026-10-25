"""Offline scheduled hold consumers and native controller admission boundaries."""

import pytest
from test_closer_native_capacity import capacity_host as capacity_host
from test_closer_native_observer import host as host

import closer_campaign as campaign
import closer_mechanisms as mechanisms
import closer_native_observer as native


def tick_without_model(fixture, launches):
    _manifest, path, obj, *_ = fixture
    return campaign.tick(
        obj.path.parent,
        path,
        mechanism=mechanisms.NATIVE,
        qualifier=lambda *_: [],
        route_fn=lambda *_: {"hold": "no qualified model roles"},
        native_probe=lambda *_: None,
        launcher=lambda argv, _log: launches.append(argv) or 60001,
        pid_alive=lambda *_: True,
        max_steps=1,
    )


def test_actual_tick_launches_native_round_without_a_model_route(capacity_host, monkeypatch):
    import closer_role_qualification as roles

    monkeypatch.setattr(roles, "ROLE_QUALIFICATIONS", {})
    monkeypatch.setattr(roles, "is_runtime_qualified", lambda *_: False)
    launches = []
    result = tick_without_model(capacity_host, launches)
    assert result[0]["kind"] == "launched", result
    assert len(launches) == 1
    assert "--agent" not in launches[0] and "--profile-id" not in launches[0]
    assert "worker" in launches[0]
    manifest = capacity_host[0]
    context = native._CURRENT_ADMISSION[str(native.contract(manifest)[3])]["context"]
    from pathlib import Path

    report = native._read(Path(context["report"]["path"]))
    assert report["control_plane_only"] is True and report["profiles"] == {}


def test_stale_signed_native_admission_cannot_launch_zero_model_round(capacity_host, monkeypatch):
    manifest, path, obj, doc, _source, clock, _invocation = capacity_host
    admission = native.publish_admission(manifest, path, obj.path, doc)
    clock[0] += 601
    monkeypatch.setattr(native, "publish_admission", lambda *_: admission)
    launches = []
    result = tick_without_model(capacity_host, launches)
    assert result[0]["kind"] == "held", result
    assert "expired" in result[0]["reason"]
    assert launches == []


def test_absent_native_admission_does_not_grant_zero_model_launch(capacity_host, monkeypatch):
    monkeypatch.setattr(native, "publish_admission", lambda *_: None)
    launches = []
    result = tick_without_model(capacity_host, launches)
    assert result[0]["kind"] == "wait", result
    assert result[0]["reason"] == "capacity_exhausted"
    assert launches == []


def test_existing_tick_owns_unknown_wrapper_without_pipeline_row_and_escalates(capacity_host):
    import closer_hold_followup as holds
    from closer_frontier_policy import make_frontier

    _manifest, path, obj, _doc, _source, clock, _invocation = capacity_host
    frontier = make_frontier(
        ["offline/repo#1"],
        owner="owner",
        frontier_id="unknown-native",
        bindings={"offline/repo#1": {"revision": "revision-1", "action_id": "a" * 64}},
    )

    def unknown(doc):
        doc["worker_config"]["finite_frontier"] = frontier
        doc["workers"] = [
            {
                "worker_id": "original-native-worker",
                "target": campaign.ROUND_UNIT,
                "state": "unknown",
                "pid": None,
                "claim_target": "original-claim",
            }
        ]
        doc["targets"][campaign.ROUND_UNIT] = "reconcile"

    obj.update(unknown)
    launches = []
    first = tick_without_model(capacity_host, launches)
    journal = obj.read()["hold_followups"]
    assert len(journal) == 1
    record = next(iter(journal.values()))
    assert record["run_id"] == "original-native-worker"
    assert record["source"]["kind"] == "campaign_worker"
    assert record["status"] == "waiting" and record["attempts"] == 1
    assert any(item["kind"] == "hold_checked" for item in first)
    assert any(item["kind"] == "hold_followup_summary" for item in first)
    clock[0] = record["deadline"]
    second = tick_without_model(capacity_host, launches)
    assert any(item["kind"] == "hold_escalated" and item["authority"] is False for item in second)
    assert next(iter(obj.read()["hold_followups"].values()))["status"] == "escalated"
    assert launches == []
    assert holds.summarize_holds(obj.read(), clock[0])["escalated"] == 1


@pytest.mark.parametrize(
    "stage,status", [("interpret", "held"), ("exception", "held"), ("prepare", "capacity_blocked")]
)
def test_audited_role_or_capacity_release_runs_actual_fresh_prepare(
    tmp_path, monkeypatch, stage, status
):
    import json

    from test_closer_finite_reentry import TARGET, fixture

    import closer_finite_reentry as reentry
    import closer_hold_followup as holds
    import closer_pipeline as pipeline
    import closer_resources as resources

    path, frontier, document = fixture(tmp_path, stage=stage, status=status)
    row = document["frontiers"]["frozen"]["items"][TARGET]
    if stage != "prepare":
        row["stage_status"] = "completed"
        row["results"][stage] = {
            "status": "pending",
            "classification": "role_qualification_unavailable",
            "pending_evidence": {"role": "synthetic-qualified-role"},
        }
    path.write_text(json.dumps(document))
    before = json.loads(path.read_text())["frontiers"]["frozen"]["items"][TARGET]
    calls = []

    def fresh_probe(*args):
        calls.append(args)
        return {
            "kind": (
                "qualified_role_followup" if stage != "prepare" else "available_capacity_followup"
            ),
            "same_bound_sources": "synthetic-current-admission",
            "acceptance_authority": False,
        }

    monkeypatch.setattr(holds, "release_probe", fresh_probe)
    args = {
        "frontier": frontier,
        "target": TARGET,
        "event": "same-exact-release",
        "assignment_path": str(tmp_path / "assignment"),
        "source_receipts": {"fixture": True},
    }
    proposal = reentry.plan(path, **args)
    applied = reentry.apply(path, **args, expected_plan_sha256=proposal["plan_sha256"])
    assert applied["status"] == "applied"
    released = json.loads(path.read_text())["frontiers"]["frozen"]["items"][TARGET]
    assert released["stage"] == "prepare" and released["results"] == {}
    assert released["reentry_history"][-1]["previous"] == before
    seen = []
    lease = resources.acquire(
        path.parent.parent.parent, TARGET, owner="new-native-admission", finite_frontier=frontier
    )
    try:
        with resources.fenced(lease):
            receipt = pipeline._run_pipeline_finite(
                path,
                [TARGET],
                frontier_target=TARGET,
                finite_frontier=frontier,
                prepare=lambda *_: seen.append("fresh-prepare")
                or {"revision": "rev", "new_evidence": True},
                interpret=lambda *_: {"substantive_needs": []},
                exception=lambda *_: {"route": "normal"},
                act=lambda *_: {"status": "completed", "target_complete": True},
                capacity_fn=lambda: {"io": 1, "low": 1, "exception": 1},
            )
    finally:
        lease.release()
    assert seen == ["fresh-prepare"]
    assert receipt["targets"][TARGET]["status"] == "completed"
    assert len(calls) >= 3  # Plan, apply verification, and live fenced readiness.


def test_no_start_hold_without_original_native_terminal_proof_cannot_mutate(tmp_path, monkeypatch):
    from test_closer_hold_safety_contracts import NOW, TARGET, only_hold, world

    import closer_finite_reentry as reentry
    import closer_hold_followup as holds

    obj, paths, manifest = world(tmp_path, kind="role")
    before = paths[TARGET].read_bytes()
    monkeypatch.setattr(
        reentry,
        "plan",
        lambda *a, **k: pytest.fail("cannot plan before original worker terminal proof"),
    )
    doc, _ = holds.consume(obj, manifest, "offline-manifest.json", now=NOW)
    assert paths[TARGET].read_bytes() == before
    assert only_hold(obj)["status"] == "waiting"
    assert doc["hold_followups"]


def test_role_registry_event_wakes_owned_hold_before_next_schedule(tmp_path, monkeypatch):
    from test_closer_hold_safety_contracts import NOW, only_hold, world

    import closer_hold_followup as holds
    import closer_role_qualification as roles

    obj, _paths, manifest = world(tmp_path, kind="role")
    calls = []
    monkeypatch.setattr(holds, "_probe", lambda *a, **k: calls.append(1) or {"status": "waiting"})
    holds.consume(obj, manifest, "offline", now=NOW)
    holds.consume(obj, manifest, "offline", now=NOW + 1)
    assert len(calls) == 1
    monkeypatch.setattr(roles, "ROLE_QUALIFICATIONS", {"synthetic-observed-change": {}})
    holds.consume(obj, manifest, "offline", now=NOW + 2)
    assert len(calls) == 2
    assert only_hold(obj)["attempts"] == 2


@pytest.mark.parametrize("changed", [None, "unrelated-event", "pending", "run", "attempt"])
def test_explicit_original_request_terminal_bridge_routes_only_exact_terminal_to_fresh_frontier(
    tmp_path, monkeypatch, changed
):
    import json
    from types import SimpleNamespace

    from test_closer_hold_safety_contracts import TARGET, only_hold, world

    import closer_hold_followup as holds
    import closer_queue as queue
    import verification_events
    from closer_frontier_policy import action_identity, make_frontier

    obj, paths, _manifest = world(tmp_path, kind="accepted")
    record = only_hold(obj)
    row = holds._source_row(record)[1]
    repo, number = TARGET.split("#")
    event = {
        "schema": verification_events.SCHEMA,
        "repo": repo,
        "pr": number,
        "head_sha": "a" * 40,
        "evaluated_sha": "b" * 40,
        "request_sha256": "c" * 64,
        "material_sha256": "d" * 64,
    }
    event["event_id"] = verification_events.event_id(event)
    bridge = {
        "schema": "closer-verifier-terminal-binding/v1",
        "operation_id": "operation-original",
        "target": TARGET,
        "head": "a" * 40,
        "merge": "b" * 40,
        "request_basis": "e" * 64,
        "event_id": event["event_id"],
        "run_id": 50001,
        "run_attempt": 1,
    }
    binding = {"head": "a" * 40, "merge": "b" * 40, "verification_terminal_binding": bridge}
    frontier = make_frontier(
        [TARGET],
        owner=record["owner"],
        frontier_id=record["frontier"]["frontier_id"],
        bindings={
            TARGET: {
                "revision": "revision-1",
                "action_id": action_identity(TARGET, "revision-1", "post_merge_verify", binding),
            }
        },
    )
    record["frontier"] = frontier
    action_path = paths[TARGET].parent / "state.actions" / (holds._digest(TARGET) + ".json")
    original = json.loads(action_path.read_text())
    bucket = original["frontiers"][frontier["frontier_id"]]
    bucket["policy"] = frontier
    operation = bucket["items"][TARGET]["operation"]
    operation.update(
        binding=binding,
        finite_frontier=frontier,
        status="completed",
        result={"verification_request_basis": "e" * 64},
    )
    action_path.write_text(json.dumps(original))
    proof = {
        "operation_frontier": frontier["frontier_id"],
        "operation_id": "operation-original",
        "head": "a" * 40,
        "merge": "b" * 40,
        "event_id": "e" * 64,
    }
    assert holds._terminal_bridge(action_path, TARGET, proof) == bridge
    marker = {
        "schema": "verifier-corpus-decision/v1",
        "repo": repo,
        "pr": number,
        "head_sha": "a" * 40,
        "evaluated_sha": "b" * 40,
        "run_id": "50001",
        "run_attempt": 1,
        "provider_verdicts": ["PASS"],
        "ci_failed": False,
        "verdict": "PASS",
        "verification_event": event,
    }
    if changed == "unrelated-event":
        event["material_sha256"] = "f" * 64
        event["event_id"] = verification_events.event_id(event)
    elif changed == "pending":
        marker["verdict"] = "PENDING"
    elif changed == "run":
        marker["run_id"] = "50002"
    elif changed == "attempt":
        marker["run_attempt"] = 2
    snapshot = {"target": TARGET, "head": "a" * 40, "merge": "b" * 40, "verifier": marker}
    monkeypatch.setattr(holds, "_backend", lambda *a: SimpleNamespace(observe=lambda *a: snapshot))
    monkeypatch.setattr(queue, "accepted_verifier_wait_proof", lambda *a, **k: proof)
    result = holds._probe_ci(record, "offline", obj.read(), row)
    if changed is None:
        assert result["status"] == "escalated"
        assert result["escalation_reason"] == "terminal_observed_requires_fresh_frontier"
        assert result["authority"] is False
        assert result["proof"]["terminal_result"]["event_id"] == bridge["event_id"]
    else:
        assert result["status"] == "waiting"


@pytest.mark.parametrize("resolution", ["unknown", "confirmed_not_started", "completed"])
def test_queue_reconciles_only_original_effect_and_never_creates_outer_receipt(
    tmp_path, resolution
):
    import copy
    import json

    from test_closer_hold_safety_contracts import TARGET, only_hold, world

    import closer_queue as queue
    from closer_frontier_policy import action_identity, make_frontier

    obj, paths, _manifest = world(tmp_path, kind="action")
    record = only_hold(obj)
    path = paths[TARGET].parent / "state.actions" / (queue._digest(TARGET) + ".json")
    before = json.loads(path.read_text())
    bucket = before["frontiers"][record["frontier"]["frontier_id"]]
    operation = bucket["items"][TARGET]["operation"]
    frontier = make_frontier(
        [TARGET],
        owner=record["owner"],
        frontier_id=record["frontier"]["frontier_id"],
        bindings={
            TARGET: {
                "revision": operation["revision"],
                "action_id": action_identity(
                    TARGET, operation["revision"], operation["action"], operation["binding"]
                ),
            }
        },
    )
    bucket["policy"] = frontier
    operation.update(finite_frontier=frontier, deadline=50.0)
    before["frontiers"]["other-namespace"] = {"untouched": True}
    path.write_text(json.dumps(before))
    pipeline_before = paths[TARGET].read_bytes()
    calls = []

    def reconcile(target, original):
        calls.append(copy.deepcopy(original))
        assert target == TARGET and original["id"] == "operation-original"
        assert original["effect_deadline"] == 50.0
        assert original["deadline"] == 1120.0
        return {"status": resolution, "result": {"original_fence": "settled"}}

    result = queue.reconcile_only(
        path,
        TARGET,
        "operation-original",
        "revision-1",
        frontier_id=frontier["frontier_id"],
        reconcile=reconcile,
        now=lambda: 1000.0,
    )
    assert result["status"] == resolution and len(calls) == 1
    after = json.loads(path.read_text())
    assert after["frontiers"]["other-namespace"] == {"untouched": True}
    assert after["frontiers"][frontier["frontier_id"]]["round"] == {}
    assert paths[TARGET].read_bytes() == pipeline_before
    saved = after["frontiers"][frontier["frontier_id"]]["items"][TARGET]["operation"]
    assert saved["id"] == "operation-original"
    assert saved["status"] == ("uncertain" if resolution == "unknown" else resolution)
    if resolution == "completed":
        queue.reconcile_only(
            path,
            TARGET,
            "operation-original",
            "revision-1",
            frontier_id=frontier["frontier_id"],
            reconcile=lambda *a: pytest.fail("no duplicate reconcile"),
            now=lambda: 1000.0,
        )
    with pytest.raises(queue.QueueStateError, match="identity changed"):
        queue.reconcile_only(
            path,
            TARGET,
            "another-operation",
            "revision-1",
            frontier_id=frontier["frontier_id"],
            reconcile=lambda *a: pytest.fail("cannot adopt another operation"),
            now=lambda: 1000.0,
        )


@pytest.mark.parametrize(
    "role,stage",
    [
        ("bounded_classification", "interpret"),
        ("strong_criterion_adjudication", "exception"),
        ("constrained_repair", "exception"),
    ],
)
def test_release_rechecks_exact_named_role_and_current_capacity(
    capacity_host, monkeypatch, role, stage
):
    import hashlib

    import closer_dispatcher as dispatcher
    import closer_hold_followup as holds
    import closer_operational_capacity as scoped
    from closer_frontier_policy import make_frontier

    manifest, path, _obj, _doc, assignment, _clock, _invocation = capacity_host
    target = "offline/repo#1"
    frontier = make_frontier(
        [target],
        owner="owner",
        frontier_id="release-role",
        bindings={target: {"revision": "rev", "action_id": "a" * 64}},
    )
    pending = dispatcher.role_qualification_pending(
        target,
        "rev",
        repair=role == "constrained_repair",
        substantive=role == "strong_criterion_adjudication",
    )
    row = {"status": "held", "stage": stage, "revision": "rev", "results": {stage: pending}}
    receipts = {
        "followup_manifest": {
            "path": str(path),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
    }
    monkeypatch.setattr(campaign, "qualify", lambda *a: [])
    monkeypatch.setattr(
        scoped, "evaluate", lambda *a: {"input_sha256": {"bound-telemetry": "current"}}
    )
    monkeypatch.setattr(scoped, "validate_report", lambda *a, **k: True)
    called = []
    monkeypatch.setattr(
        dispatcher,
        "_profile_exception_qualified",
        lambda report, profile, **flags: called.append(flags) or True,
    )
    proof = holds.release_probe(path, frontier, target, row, assignment, receipts)
    assert proof["role"] == role and proof["eligible_profiles"]
    assert called == [
        {
            "repair": role == "constrained_repair",
            "substantive": role == "strong_criterion_adjudication",
        }
    ]
    assert proof["capacity_inputs"] == {"bound-telemetry": "current"}
    row["results"][stage]["pending_evidence"]["revision"] = "stale-revision"
    with pytest.raises(ValueError, match="role event differs"):
        holds.release_probe(path, frontier, target, row, assignment, receipts)


def test_campaign_metrics_use_only_exact_authenticated_native_round_receipt(capacity_host):
    import json

    from test_closer_native_capacity import completed_standing_round

    import closer_hold_followup as holds

    manifest, _path, obj, *_ = capacity_host
    _cfg, _context, proof, _aggregate, _result = completed_standing_round(capacity_host)
    receipts = holds.validated_receipts(obj, obj.read(), manifest)
    assert len(receipts) == 1
    assert receipts[0]["round_id"] == proof["round_id"]
    assert receipts[0]["run_ids"] == proof["run_ids"]
    report = tick_without_model(capacity_host, [])
    assert any(row["kind"] == "hold_followup_summary" and "efficiency" in row for row in report)
    from pathlib import Path

    receipt = Path(proof["receipt_path"])
    changed = json.loads(receipt.read_text())
    changed["targets"]["unrelated/repo#2"] = {"status": "completed"}
    receipt.write_text(json.dumps(changed))
    assert holds.validated_receipts(obj, obj.read(), manifest) == []
