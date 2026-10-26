"""Offline scheduled hold consumers and native controller admission boundaries."""

import pytest
from test_closer_native_capacity import capacity_host as capacity_host
from test_closer_native_observer import host as host

import closer_campaign as campaign
import closer_mechanisms as mechanisms


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


@pytest.mark.parametrize(
    "changed", [None, "unrelated-basis", "legacy-input", "pending", "lifecycle"]
)
def test_original_sent_correlation_accepts_only_current_authenticated_terminal(
    tmp_path, monkeypatch, changed
):
    from test_closer_verifier_request_correlation import terminal_world

    import closer_verifier_followup as followup

    path, target, accepted, observation = terminal_world(tmp_path)
    if changed == "unrelated-basis":
        observation["verifier"]["verification_request_basis"] = "f" * 64
    elif changed == "legacy-input":
        import json

        document = json.loads(path.read_text())
        document["frontiers"][accepted["operation_frontier"]]["items"][target]["operation"][
            "result"
        ]["verification_post_accepted"].pop("correlation_input")
        path.write_text(json.dumps(document))
    elif changed == "pending":
        observation["verifier"]["verdict"] = "PENDING"
    elif changed == "lifecycle":
        observation["verifier_lifecycle"]["run_attempt"] = "2"
    result = followup.terminal_proof(path, target, accepted, observation, now=1000)
    assert (result is not None) == (changed is None)
    if result:
        assert result["operation_id"] == accepted["operation_id"]
        assert result["request_basis"] == accepted["event_id"]
        assert result["authority"] is False


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


@pytest.mark.parametrize("stage", ["prepare", "act"])
@pytest.mark.parametrize(
    "state",
    [
        "free",
        "occupied",
        "uncertain",
        "legacy",
        "wrong_root",
        "wrong_bound",
        "bool_bound",
        "zero_bound",
        "negative_bound",
        "oversized_bound",
        "nonfinite_bound",
        "expired",
    ],
)
def test_actual_io_release_observes_original_shared_pool(
    capacity_host, tmp_path, monkeypatch, capsys, stage, state
):
    import copy
    import hashlib
    import json
    from contextlib import ExitStack
    from pathlib import Path

    import closer_hold_followup as holds
    import closer_operational_capacity as scoped
    import closer_pipeline as pipeline
    import closer_resources as resources
    from closer_frontier_policy import make_frontier

    _manifest, manifest_path, _obj, _doc, assignment, clock, _invocation = capacity_host
    import time

    real_clock = resources.run_scoped.__kwdefaults__["now"]
    monkeypatch.setattr(time, "time", real_clock)
    capacity_home = Path.home()
    monkeypatch.setattr(campaign, "qualify", lambda *_: [])
    from test_closer_reasoning_boundary import native_world

    import closer_dispatcher as dispatcher
    from closer_frontier_policy import action_identity

    work = tmp_path / "actual-native"
    work.mkdir()
    native = native_world.__wrapped__(work, monkeypatch)
    native.ad.unlink()
    target = "owner/repo#12"
    native.snapshot.update(
        state="OPEN",
        merge=None,
        verifier=None,
        checks="PENDING",
        pending_check_run_ids=[123],
        pending_check_head=native.snapshot["head"],
    )
    native.snapshot["revision"] = "rev"

    def stage_capacity():
        # Pending-CI INTERPRET is now real native IO. Deny only the requested
        # durable stage rather than denying all IO after PREPARE.
        source_path = (
            resources.shard_dir(resources.scoped_root(native.ledger), target) / "state.json"
        )
        retained = pipeline._load(source_path)
        current = (
            (retained.get("frontiers") or {}).get(native.frontier["frontier_id"], {}).get("items")
            or {}
        ).get(target, {})
        return {
            "io": int(stage == "act" and current.get("stage", "prepare") != "act"),
            "low": 1,
            "exception": 1,
        }

    monkeypatch.setattr(dispatcher, "_pipeline_capacity", stage_capacity)
    observer = dispatcher.Closer(
        dispatcher.GitHub(deadline=clock[0] + 30), state_path=native.raw, apply=True, workers=True
    )
    observed = observer.observe(target)
    frontier = make_frontier(
        [target],
        owner=native.frontier["owner"],
        frontier_id=native.frontier["frontier_id"],
        bindings={
            target: {
                "revision": "rev",
                "action_id": action_identity(target, "rev", "wait", observed["operation_binding"]),
            }
        },
    )
    native.argv[native.argv.index("--finite-frontier-json") + 1] = json.dumps(frontier)
    native.events["prepare"] = 0
    dispatcher.main(native.argv)
    result = json.loads(capsys.readouterr().out)
    assert result["targets"][target]["status"] == "capacity_blocked", result["targets"][target]
    assert result["targets"][target]["stage"] == stage
    if stage == "act":
        assert (
            result["targets"][target]["results"]["interpret"]["classification"]
            == "deterministic_wait"
        )
        assert not (
            resources.scoped_root(native.ledger) / "pools" / "low"
        ).exists(), "deterministic pending CI must use IO, not acquire a model lane"
    assert native.events["provider"] == native.events["effect"] == 0
    clock[0] = real_clock()
    monkeypatch.setattr(time, "time", lambda: clock[0])
    monkeypatch.setenv("HOME", str(capacity_home))
    assignment.parent.mkdir(parents=True, exist_ok=True)
    assignment.write_text(json.dumps({"targets": [{"target": target}]}))
    pipeline_path = native.ledger
    root = resources.scoped_root(pipeline_path)
    path = resources.shard_dir(root, target) / "state.json"
    document = pipeline._load(path)
    row = document["frontiers"][frontier["frontier_id"]]["items"][target]
    assert row["shared_lane_context"]["bounds"]["io"] == 4
    bound = row["shared_lane_context"]["bounds"]["io"]
    original_context = copy.deepcopy(row["shared_lane_context"])
    if state == "legacy":
        row.pop("shared_lane_context")
    elif state == "wrong_root":
        row["shared_lane_context"]["root"] = str(tmp_path / "other.scoped")
    elif state in {
        "wrong_bound",
        "bool_bound",
        "zero_bound",
        "negative_bound",
        "oversized_bound",
        "nonfinite_bound",
    }:
        row["shared_lane_context"]["bounds"]["io"] = {
            "wrong_bound": 3,
            "bool_bound": True,
            "zero_bound": 0,
            "negative_bound": -1,
            "oversized_bound": 999,
            "nonfinite_bound": float("inf"),
        }[state]
    if state in {"wrong_root", "bool_bound", "zero_bound", "negative_bound", "oversized_bound"}:
        context = row["shared_lane_context"]
        context["sha256"] = holds._digest(
            {key: value for key, value in context.items() if key != "sha256"}
        )
    path.write_text(json.dumps(document))
    receipts = {
        "followup_manifest": {
            "path": str(manifest_path),
            "sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        },
        "capacity_observation_window": {"started_at": clock[0], "expires_at": clock[0] + 120},
    }
    if state == "expired":
        clock[0] += 120
    calls = []
    evaluate = scoped.evaluate
    monkeypatch.setattr(scoped, "evaluate", lambda cfg: calls.append(cfg) or evaluate(cfg))
    before = path.read_bytes()
    with ExitStack() as held:
        if state == "occupied":
            for _ in range(bound):
                held.enter_context(resources.pool_slot(root, "io", bound, deadline=clock[0]))
        elif state == "uncertain":
            (root / "pools/io").mkdir(parents=True, exist_ok=True)
            for index in range(bound):
                (root / f"pools/io/slot-{index}.json").write_text(
                    json.dumps(
                        {
                            "schema": "finite-pool-uncertainty/v1",
                            "status": "uncertain",
                            "lane": "io",
                            "process_group": None,
                        }
                    )
                )
        if state == "free":
            proof = holds.release_probe(path, frontier, target, row, assignment, receipts)
            assert proof["kind"] == "available_capacity_followup"
            assert proof["capacity_inputs"] == {}  # Fresh explicit control-plane scope.
            assert proof["io_observation"]["bound"] == bound and proof["io_observation"][
                "root"
            ] == str(root)
            assert proof["io_observation"]["original_context_sha256"] == original_context["sha256"]
            assert proof["acceptance_authority"] is False
            assert holds.release_probe(path, frontier, target, row, assignment, receipts) == proof
        else:
            with pytest.raises((ValueError, resources.ResourceBlocked)):
                holds.release_probe(path, frontier, target, row, assignment, receipts)
    assert calls and all(
        cfg["control_plane_only"] is True and cfg["profile_ids"] == [] for cfg in calls
    )
    assert path.read_bytes() == before
    if state == "free":
        # Only original native terminal attestation is a synthetic oracle here;
        # capacity observation, plan/apply/audit, generation rearm and fresh
        # registered dispatcher callbacks all remain real.
        obj = campaign.Campaign(tmp_path / "actual-io-followup.campaign.json")
        obj.begin(
            "actual-io-followup",
            [target],
            owner=frontier["owner"],
            release_pin=_manifest["package_digest"],
            worker_config={
                "finite_frontier": frontier,
                "dispatch_state": str(native.raw),
                "assignment": str(assignment),
                "hold_followup_policy": {
                    "check_interval_seconds": 1,
                    "deadline_seconds": 120,
                    "attempt_budget": 1,
                },
            },
        )
        obj.update(
            lambda doc: doc.update(
                workers=[
                    {
                        "worker_id": "original-terminal",
                        "target": campaign.ROUND_UNIT,
                        "state": "terminal",
                        "outcome": "ok",
                        "pid": None,
                    }
                ]
            )
        )
        original = next(iter(holds.capture(obj, now=clock[0])["hold_followups"].values()))
        boundary = {"offline_original_terminal": True}

        def validate(_campaign, doc, _manifest, record, proof):
            holds._assert_bound(doc, record)
            if proof != boundary or not holds._probe_allowed(doc):
                raise ValueError("terminal oracle refused changed context")

        def terminal(_campaign, doc, _manifest, record):
            validate(_campaign, doc, _manifest, record, boundary)
            holds._source_row(record)
            return copy.deepcopy(boundary)

        monkeypatch.setattr(holds, "_native_terminal_boundary", terminal)
        monkeypatch.setattr(holds, "_validate_terminal_boundary", validate)
        released, _ = holds.consume(obj, _manifest, str(manifest_path), now=clock[0])
        assert released["hold_followups"][original["hold_id"]]["status"] == "released"
        rearmed = holds.rearm(obj, released, _manifest, now=clock[0])
        assert rearmed["generation"] == 2
        monkeypatch.setattr(time, "time", real_clock)
        native.events["prepare"] = 0
        dispatcher.main(native.argv)
        reblocked = json.loads(capsys.readouterr().out)["targets"][target]
        assert reblocked["status"] == "capacity_blocked" and reblocked["stage"] == stage, reblocked
        clock[0] = real_clock()
        monkeypatch.setattr(time, "time", lambda: clock[0])
        after = holds.capture(campaign.Campaign(obj.path), now=clock[0])
        following = [
            record for record in after["hold_followups"].values() if record["generation"] == 2
        ]
        assert len(following) == 1
        assert following[0]["deadline"] == original["deadline"]
        assert following[0]["attempts"] == following[0]["attempt_budget"] == 1
        assert following[0]["lineage_id"] == original["hold_id"]
        assert following[0]["status"] == "escalated"
        assert native.events["provider"] == native.events["effect"] == 0


@pytest.mark.parametrize(
    "source", [None, {}, {"state_path": None}, {"state_path": "relative/state.json"}]
)
def test_malformed_ci_source_waits_before_backend_observation(monkeypatch, source):
    import closer_ci_followup as ci
    import closer_hold_followup as holds

    monkeypatch.setattr(
        holds,
        "_backend",
        lambda *_: pytest.fail("malformed CI source must not construct or observe a backend"),
    )
    monkeypatch.setattr(
        ci,
        "read_terminal",
        lambda *_a, **_kw: pytest.fail("malformed CI source must not read retained input"),
    )
    record = {"target": "offline/repo#1", "release_proof": {"kind": "ci_terminal"}}
    if source is not None:
        record["source"] = source
    result = holds._probe_ci(record, "unused", {}, {})
    assert result == {
        "status": "waiting",
        "reason": "CI original source binding is missing or invalid",
    }


@pytest.mark.parametrize(
    "damage", ["kind", "relative", "shard", "hash", "run", "revision", "unknown"]
)
def test_invalid_ci_source_join_never_reaches_backend_or_input_reads(tmp_path, monkeypatch, damage):
    import copy

    import closer_ci_followup as ci
    import closer_hold_followup as holds
    import closer_resources as resources
    from closer_frontier_policy import make_frontier

    target = "offline/repo#1"
    frontier = make_frontier(
        [target],
        owner="owner",
        frontier_id="source-join",
        bindings={target: {"revision": "rev", "action_id": "a" * 64}},
    )
    row = {
        "revision": "rev",
        "status": "held",
        "stage": "act",
        "stage_status": "completed",
        "results": {
            "act": {
                "status": "pending",
                "pending_kind": "ci",
                "pending_evidence": {"check_run_id": 123},
            }
        },
    }
    source = resources.shard_dir(tmp_path / "source.pipeline.json.scoped", target) / "state.json"
    record = {
        "target": target,
        "revision": "rev",
        "run_id": holds._digest(["finite-frontier/v2", frontier["frontier_id"], frontier["owner"]])[
            :32
        ],
        "execution_state": "confirmed_not_started",
        "frontier": frontier,
        "release_proof": {"kind": "ci_terminal"},
        "source": {"kind": "pipeline", "state_path": str(source), "row_sha256": holds._digest(row)},
    }
    if damage == "kind":
        record["source"]["kind"] = "campaign_worker"
    elif damage == "relative":
        record["source"]["state_path"] = "relative/" + source.name
    elif damage == "shard":
        record["source"]["state_path"] = str(source.with_name("unbound.json"))
    elif damage == "hash":
        record["source"]["row_sha256"] = "f" * 64
    elif damage == "run":
        record["run_id"] = "foreign-run"
    elif damage == "revision":
        record["revision"] = "foreign-revision"
    else:
        record["execution_state"] = "action_outcome_unknown"
    before = copy.deepcopy(record)
    monkeypatch.setattr(
        holds, "_backend", lambda *_: pytest.fail("invalid CI join reached backend")
    )
    monkeypatch.setattr(
        ci, "read_terminal", lambda *_a, **_kw: pytest.fail("invalid CI join read retained inputs")
    )
    assert holds._probe_ci(record, "unused", {}, row) == {
        "status": "waiting",
        "reason": "CI original source binding is missing or invalid",
    }
    assert record == before
