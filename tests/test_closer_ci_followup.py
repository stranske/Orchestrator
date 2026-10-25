"""Offline original CI waits, actual queues/journals, and fixed native read seams."""

from __future__ import annotations

import copy
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import claims
import closer_ci_followup as ci
import closer_dispatcher as dispatcher
import closer_frontier_policy as policy
import closer_pipeline as pipeline
import closer_queue as queue
import closer_resources as resources

TARGET = "offline/ci#7"
HEAD = "a" * 40


def world(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HANDOFF_DIR", str(tmp_path / "handoff"))
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path / "orchestrator"))
    monkeypatch.setattr(claims, "holder", lambda *_: None)
    original = {
        "target": TARGET,
        "pr_target": TARGET,
        "kind": "pr",
        "state": "OPEN",
        "head": HEAD,
        "base": "b" * 40,
        "merge": None,
        "body": "Original request",
        "title": "Delivery",
        "draft": False,
        "sources": [{"target": "offline/ci#1", "body": "Required behavior", "state": "open"}],
        "source_inventory": {"complete": True, "linked_issue_targets": ["offline/ci#1"]},
        "review_revision": "review-1",
        "active_threads": 0,
        "threads": [],
        "labels": [],
        "checks": "PENDING",
        "pending_check_run_ids": [123],
        "pending_check_head": HEAD,
        "mergeable": "MERGEABLE",
        "verifier": None,
    }
    original["revision"] = dispatcher.digest(original)
    backend = dispatcher.Closer(
        SimpleNamespace(), state_path=tmp_path / "queue", apply=False, workers=False
    )
    binding = backend.operation_binding(TARGET, original)
    frontier = policy.make_frontier(
        [TARGET],
        owner="ci-owner",
        frontier_id="original-ci-wait",
        bindings={
            TARGET: {
                "revision": original["revision"],
                "action_id": policy.action_identity(TARGET, original["revision"], "wait", binding),
            }
        },
    )
    root = resources.scoped_root(tmp_path / "queue.pipeline.json")
    path = resources.shard_dir(root, TARGET) / "state.json"
    path.parent.mkdir(parents=True)
    retained = {}

    def act(_target, context):
        action_path = Path(context["action_state_path"])
        receipt = queue.run_round(
            action_path,
            [TARGET],
            lambda *_: {
                **copy.deepcopy(original),
                "action": "wait",
                "eligible": False,
                "reason": "current-head checks pending or unknown",
                "next_trigger": "original check terminal",
                "operation_binding": binding,
                "evidence": copy.deepcopy(original),
                "claim_targets": [TARGET],
                "repo_keys": ["offline/ci"],
            },
            lambda *_: pytest.fail("CI wait never executes"),
            owner="original-native",
            deadline=None,
            finite_frontier=frontier,
            frontier_target=TARGET,
            request_timeout_seconds=10,
            receipt_reserve_seconds=0,
            max_operation_retries=0,
            claim_fn=lambda *a: True,
            release_fn=lambda *a: True,
        )
        evidence = {
            "check_run_id": 123,
            "head": HEAD,
            "target": TARGET,
            "observed_revision": original["revision"],
        }
        retained.update(action_path=action_path, receipt=receipt, evidence=evidence)
        return {
            "status": "pending",
            "pending_kind": "ci",
            "pending_evidence": evidence,
            "next_trigger": "original check terminal",
            "queue_receipt": receipt,
        }

    lease = resources.acquire(root, TARGET, owner="original-native", finite_frontier=frontier)
    try:
        with resources.fenced(lease):
            receipt = pipeline._run_pipeline_finite(
                path,
                [TARGET],
                frontier_target=TARGET,
                finite_frontier=frontier,
                prepare=lambda *_: {
                    "revision": original["revision"],
                    "snapshot": copy.deepcopy(original),
                },
                interpret=lambda *_: {"substantive_needs": []},
                exception=lambda *_: {"route": "normal"},
                act=act,
                capacity_fn=lambda: {"io": 1, "low": 1, "exception": 1},
            )
    finally:
        lease.release()
    assert receipt["targets"][TARGET]["status"] == "held", receipt
    row = pipeline._load(path)["frontiers"][frontier["frontier_id"]]["items"][TARGET]
    fresh = copy.deepcopy(original)
    fresh.update(checks="SUCCESS", pending_check_run_ids=[], pending_check_head=None)
    fresh["revision"] = dispatcher.digest(fresh)
    gh = dispatcher.GitHub(deadline=time.time() + 120)
    calls = []
    check = {
        "id": 123,
        "head_sha": HEAD,
        "status": "completed",
        "conclusion": "success",
        "name": "required-test",
        "html_url": "https://github.com/offline/ci/runs/123",
    }

    def api(endpoint, **_kwargs):
        calls.append(endpoint)
        assert endpoint == "repos/offline/ci/check-runs/123"
        return copy.deepcopy(check)

    monkeypatch.setattr(gh, "api", api)
    monkeypatch.setattr(gh, "snapshot", lambda *_: copy.deepcopy(fresh))
    backend = dispatcher.Closer(gh, state_path=tmp_path / "queue", apply=False, workers=False)
    return SimpleNamespace(
        path=path,
        frontier=frontier,
        row=row,
        retained=retained,
        original=original,
        fresh=fresh,
        check=check,
        backend=backend,
        calls=calls,
    )


def test_original_ci_wait_has_real_no_effect_queue_proof_and_terminal_read(tmp_path, monkeypatch):
    w = world(tmp_path, monkeypatch)
    before = w.retained["action_path"].read_bytes()
    proof = queue.ci_wait_no_effect_proof(
        w.retained["action_path"],
        TARGET,
        frontier=w.frontier,
        receipt=w.retained["receipt"],
        evidence=w.retained["evidence"],
    )
    assert proof["confirmed_no_effect"] is True and proof["authority"] is False
    read = ci.read_terminal(w.backend, w.retained["action_path"], TARGET, w.frontier, w.row)
    assert read["status"] == "ready", read
    assert read["proof"]["terminal_check"]["id"] == 123
    assert w.calls == ["repos/offline/ci/check-runs/123"]
    assert w.retained["action_path"].read_bytes() == before


@pytest.mark.parametrize(
    "corrupt", ["id", "head", "pending", "failure", "changed-source", "history", "unknown"]
)
def test_ci_terminal_never_infers_no_start_or_readiness(tmp_path, monkeypatch, corrupt):
    w = world(tmp_path, monkeypatch)
    if corrupt == "id":
        w.check["id"] = 124
    elif corrupt == "head":
        w.check["head_sha"] = "f" * 40
    elif corrupt == "pending":
        w.check.update(status="in_progress", conclusion=None)
    elif corrupt == "failure":
        w.check["conclusion"] = "failure"
    elif corrupt == "changed-source":
        w.fresh["body"] += " substantive"
    else:
        document = queue._read_state(w.retained["action_path"])
        item = document["frontiers"][w.frontier["frontier_id"]]["items"][TARGET]
        if corrupt == "history":
            item["operation_history"] = [{"id": "old-operation", "status": "completed"}]
        else:
            item["provider_outcome_uncertain"] = {"run_id": "unknown-provider"}
        w.retained["action_path"].write_text(json.dumps(document))
    before = w.retained["action_path"].read_bytes()
    try:
        result = ci.read_terminal(w.backend, w.retained["action_path"], TARGET, w.frontier, w.row)
    except (ValueError, queue.QueueStateError):
        result = {"status": "waiting"}
    assert result["status"] != "ready"
    if corrupt == "failure":
        assert result["status"] == "escalated" and result["authority"] is False
    if corrupt in {"history", "unknown"}:
        assert w.calls == []
    assert w.retained["action_path"].read_bytes() == before


def create(w, canonical, read):
    import closer_stage_continuation as stages

    return stages.create_ci_successor(
        w.path,
        w.retained["action_path"],
        predecessor=w.frontier,
        target=TARGET,
        terminal_read=read,
        canonical_path=canonical,
        canonical_row={"target": TARGET},
        admission=lambda: {"paused": False, "dependency": None, "live_owner": False},
    )


def test_ci_successor_retains_original_no_effect_bytes_and_restarts_exact_child(
    tmp_path, monkeypatch
):
    w = world(tmp_path, monkeypatch)
    original_queue = w.retained["action_path"].read_bytes()
    original_parent = copy.deepcopy(pipeline._load(w.path)["frontiers"][w.frontier["frontier_id"]])
    canonical = tmp_path / "assignment.json"
    canonical.write_text(json.dumps({"targets": [{"target": TARGET}]}))
    read = ci.read_terminal(
        w.backend, w.retained["action_path"], TARGET, w.frontier, w.row, state_path=w.path
    )
    first = create(w, canonical, read)
    assert first["finite_frontier"]["frontier_id"] not in pipeline._load(w.path)["frontiers"]
    reference = first["predecessor"]["original_queue"]
    assert Path(reference["path"]).read_bytes() == original_queue
    assert first["predecessor"]["original_wait"]["confirmed_no_effect"] is True
    child = first["finite_frontier"]
    calls = []
    # Actual queue creates this exact child's unknown operation. Restart may
    # reuse its namespace; it must never infer this child was not started.
    queue.run_round(
        w.retained["action_path"],
        [TARGET],
        lambda *_: read["observation"],
        lambda *_: calls.append("child-original-effect") or {"status": "unknown"},
        owner="child-writer",
        finite_frontier=child,
        frontier_target=TARGET,
        deadline=None,
        request_timeout_seconds=10,
        max_operation_retries=0,
        receipt_reserve_seconds=0,
        max_passes=1,
        claim_fn=lambda *a: True,
        release_fn=lambda *a: True,
    )
    after_effect = w.retained["action_path"].read_bytes()
    refreshed = ci.read_terminal(
        w.backend, w.retained["action_path"], TARGET, w.frontier, w.row, state_path=w.path
    )
    second = create(w, canonical, refreshed)
    assert second["reused"] and second["finite_frontier"] == child
    assert calls == ["child-original-effect"]
    assert w.retained["action_path"].read_bytes() == after_effect
    assert pipeline._load(w.path)["frontiers"][w.frontier["frontier_id"]] == original_parent
    assert len(pipeline._load(w.path)["stage_successors"]) == 1


@pytest.mark.parametrize("corrupt", ["archive", "parent", "foreign", "child-policy"])
def test_ci_successor_restart_refuses_unbound_history(tmp_path, monkeypatch, corrupt):
    w = world(tmp_path, monkeypatch)
    canonical = tmp_path / "assignment.json"
    canonical.write_text(json.dumps({"targets": [{"target": TARGET}]}))
    read = ci.read_terminal(
        w.backend, w.retained["action_path"], TARGET, w.frontier, w.row, state_path=w.path
    )
    first = create(w, canonical, read)
    if corrupt == "archive":
        Path(first["predecessor"]["original_queue"]["path"]).write_text("{}")
    elif corrupt == "parent":
        state = pipeline._load(w.path)
        state["frontiers"][w.frontier["frontier_id"]]["items"][TARGET][
            "unexpected"
        ] = "source drift"
        pipeline._save(w.path, state)
    else:
        document = queue._read_state(w.retained["action_path"])
        child = first["finite_frontier"]
        key = "foreign-history" if corrupt == "foreign" else child["frontier_id"]
        document["frontiers"][key] = {
            "policy": w.frontier if corrupt == "foreign" else {**child, "owner": "changed"},
            "items": {TARGET: {"operation": {"id": "unknown", "status": "uncertain"}}},
            "round": {},
        }
        w.retained["action_path"].write_text(json.dumps(document))
    with pytest.raises((ValueError, queue.QueueStateError)):
        ci.read_terminal(
            w.backend,
            w.retained["action_path"],
            TARGET,
            w.frontier,
            pipeline._load(w.path)["frontiers"][w.frontier["frontier_id"]]["items"][TARGET],
            state_path=w.path,
        )


@pytest.mark.parametrize("crash", [False, True])
def test_campaign_consumes_ci_terminal_and_rearms_original_cohort_once(
    tmp_path, monkeypatch, crash
):
    import closer_campaign as campaigns
    import closer_hold_followup as holds

    w = world(tmp_path, monkeypatch)
    manifest = {"package_digest": "a" * 64}
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest))
    assignment = tmp_path / "assignment.json"
    assignment.write_text(json.dumps({"targets": [{"target": TARGET}]}))
    obj = campaigns.Campaign(tmp_path / "native.campaign.json")
    obj.begin(
        "ci-owned-native",
        [TARGET],
        owner=w.frontier["owner"],
        release_pin=manifest["package_digest"],
        worker_config={
            "finite_frontier": w.frontier,
            "dispatch_state": str(tmp_path / "queue"),
            "runtime_manifest": str(manifest_path),
            "assignment": str(assignment),
            "granularity": "round",
            "hold_followup_policy": {"attempt_budget": 1},
        },
    )
    obj.update(
        lambda doc: doc.update(
            workers=[
                {
                    "worker_id": "original-ci-native",
                    "target": campaigns.ROUND_UNIT,
                    "state": "terminal",
                    "outcome": "scoped_continuation",
                }
            ],
            targets={campaigns.ROUND_UNIT: "reconcile"},
        )
    )
    monkeypatch.setattr(holds, "_backend", lambda *a: w.backend)
    # Independent host hook tests cover the native boundary; the actual CI
    # queue, HTTP read interface, sidecar/CAS and no-effect proofs remain real.
    monkeypatch.setattr(
        holds, "_native_terminal_boundary", lambda *a: {"fixture": "original-known-native"}
    )
    monkeypatch.setattr(holds, "_validate_terminal_boundary", lambda *a: None)
    before_source, before_queue = w.path.read_bytes(), w.retained["action_path"].read_bytes()
    if crash:
        update = obj.update

        class Interruption(BaseException):
            pass

        def fail_after_intent(callback):
            if callback.__name__ == "finish":
                raise Interruption()
            return update(callback)

        monkeypatch.setattr(obj, "update", fail_after_intent)
        with pytest.raises(Interruption):
            holds.consume(obj, manifest, str(manifest_path))
        row = next(iter(obj.read()["hold_followups"].values()))
        assert row["ci_followup_intent"] and row["status"] == "checking" and row["attempts"] == 1
        obj = campaigns.Campaign(obj.path)
    doc, _ = holds.consume(obj, manifest, str(manifest_path))
    row = next(iter(doc["hold_followups"].values()))
    assert row["status"] == "released" and row["attempts"] == 1
    assert row["execution_state"] == "confirmed_not_started"
    assert (
        w.path.read_bytes() == before_source
        and w.retained["action_path"].read_bytes() == before_queue
    )
    rearmed = holds.rearm(obj, doc, manifest)
    assert rearmed["generation"] == 2 and rearmed["worker_config"]["finite_frontier"] == w.frontier
    assert rearmed["targets"] == {campaigns.ROUND_UNIT: "pending"}
    assert next(iter(rearmed["hold_followups"].values()))["status"] == "consumed"
    assert holds.rearm(obj, obj.read(), manifest)["generation"] == 2
    holds.capture(obj)
    assert len(obj.read()["hold_followups"]) == 1
    assert (
        w.path.read_bytes() == before_source
        and w.retained["action_path"].read_bytes() == before_queue
    )


@pytest.fixture
def public_ci_world(request, monkeypatch):
    from test_closer_merge_successor_integration import public_merge_world
    from test_closer_reasoning_boundary import native_world

    # Reuse only existing synthetic external transports; all CLI registry,
    # resource admission, queues and stage callbacks remain production code.
    w = public_merge_world.__wrapped__(
        native_world.__wrapped__(request.getfixturevalue("tmp_path"), monkeypatch), monkeypatch
    )
    w.snapshot.update(
        checks="PENDING", pending_check_run_ids=[123], pending_check_head=w.snapshot["head"]
    )
    observer = dispatcher.Closer(
        dispatcher.GitHub(deadline=time.time() + 25), state_path=w.raw, apply=True, workers=True
    )
    observation = observer.observe("owner/repo#12")
    w.frontier = policy.make_frontier(
        ["owner/repo#12"],
        owner=w.frontier["owner"],
        frontier_id=w.frontier["frontier_id"],
        bindings={
            "owner/repo#12": {
                "revision": observation["revision"],
                "action_id": policy.action_identity(
                    "owner/repo#12",
                    observation["revision"],
                    observation["action"],
                    observation["operation_binding"],
                ),
            }
        },
    )
    w.argv[w.argv.index("--finite-frontier-json") + 1] = json.dumps(w.frontier)
    w.check = {
        "id": 123,
        "head_sha": w.snapshot["head"],
        "status": "in_progress",
        "conclusion": None,
    }
    prior = dispatcher.gh_capacity.gh_run

    def transport(args, **kwargs):
        if args[3] == "repos/owner/repo/check-runs/123":
            assert args[:3] == ["api", "--method", "GET"]
            return 0, copy.deepcopy(w.check)
        return prior(args, **kwargs)

    monkeypatch.setattr(dispatcher.gh_capacity, "gh_run", transport)
    return w


def test_public_ci_merge_verification_wait_and_restart_without_replaying_effects(
    public_ci_world, capsys
):
    w = public_ci_world
    dispatcher.main(w.argv)
    initial = json.loads(capsys.readouterr().out)
    assert not w.merges and not w.verifies, initial
    w.check.update(status="completed", conclusion="success")
    w.snapshot.update(checks="SUCCESS")
    dispatcher.main(w.argv)
    followed = json.loads(capsys.readouterr().out)
    assert len(w.merges) == 1, (
        followed.get("merge_verification_successors", {}).get("owner/repo#12", {}).get("reason")
    )
    assert len(w.verifies) == 1, followed.get("merge_verification_successors")
    assert followed["round_id"] != followed["descendant_receipts"][0]["round_id"]
    assert len(followed["descendant_receipts"]) == 2, followed
    active = followed["merge_verification_successors"]["owner/repo#12"]["native_result"]["targets"][
        "owner/repo#12"
    ]
    assert active["deferred_reason"] == "accepted_verifier_request"
    assert active["results"]["act"]["pending_kind"] == "external_event"
    assert followed["finite_frontier"] == w.frontier
    dispatcher.main(w.argv)
    restarted = json.loads(capsys.readouterr().out)
    assert len(w.merges) == 1 and len(w.verifies) == 1, restarted
    assert w.events["provider"] == 0


def test_public_ci_unknown_child_is_owned_without_new_child_or_retry(public_ci_world, capsys):
    w = public_ci_world
    dispatcher.main(w.argv)
    capsys.readouterr()
    w.check.update(status="completed", conclusion="success")
    w.snapshot.update(checks="SUCCESS")
    w.guard_mode = "uncertain"
    dispatcher.main(w.argv)
    first = json.loads(capsys.readouterr().out)
    root = resources.scoped_root(dispatcher._pipeline_state_path(w.raw))
    shard = resources.shard_dir(root, "owner/repo#12") / "state.json"
    state = pipeline._load(shard)
    links = copy.deepcopy(state["stage_successors"])
    assert len(links) == 1 and len(w.merges) == 1 and not w.verifies
    assert first["validation"]["valid"] is False
    dispatcher.main(w.argv)
    restarted = json.loads(capsys.readouterr().out)
    assert pipeline._load(shard)["stage_successors"] == links
    assert restarted["validation"]["valid"] is False
    child = next(iter(links))
    assert (
        pipeline._load(shard)["frontiers"][child]["items"]["owner/repo#12"]["status"] == "unknown"
    )
    assert len(w.merges) == 1 and not w.verifies and w.events["provider"] == 0


@pytest.mark.parametrize("change", ["wrong_check", "stale_head", "source"])
def test_public_ci_stale_or_wrong_proof_never_creates_successor(public_ci_world, capsys, change):
    w = public_ci_world
    dispatcher.main(w.argv)
    capsys.readouterr()
    w.check.update(status="completed", conclusion="success")
    w.snapshot.update(checks="SUCCESS")
    if change == "wrong_check":
        w.check["id"] = 999
    elif change == "stale_head":
        w.check["head_sha"] = "e" * 40
    else:
        w.snapshot["sources"][0]["body"] += "\nNew original requirement"
    dispatcher.main(w.argv)
    stopped = json.loads(capsys.readouterr().out)
    assert stopped["merge_verification_successors"]["owner/repo#12"]["status"] == "held"
    assert not w.merges and not w.verifies and w.events["provider"] == 0


def test_capture_cannot_promote_raw_linked_descendant_without_native_coverage(
    tmp_path, monkeypatch
):
    import closer_hold_followup as holds
    import closer_stage_continuation as stages

    w = world(tmp_path, monkeypatch)
    canonical = tmp_path / "assignment.json"
    canonical.write_text(json.dumps({"targets": [{"target": TARGET}]}))
    terminal = ci.read_terminal(
        w.backend, w.retained["action_path"], TARGET, w.frontier, w.row, state_path=w.path
    )
    child = create(w, canonical, terminal)
    state = pipeline._load(w.path)
    state["frontiers"][child["finite_frontier"]["frontier_id"]] = {
        "finite_frontier": child["finite_frontier"],
        "run_id": "uncovered-child-run",
        "items": {
            TARGET: {"status": "capacity_blocked", "stage": "prepare", "stage_status": "pending"}
        },
    }
    pipeline._save(w.path, state)
    assert stages.linked_chain(state, w.frontier, TARGET)[1] == child["finite_frontier"]
    doc = {
        "campaign_id": "actual-owner-campaign",
        "owner": w.frontier["owner"],
        "generation": 1,
        "release_pin": "fixed-pin",
        "assignment_targets": [TARGET],
        "worker_config": {
            "finite_frontier": w.frontier,
            "dispatch_state": str(tmp_path / "queue"),
            "runtime_manifest": str(tmp_path / "missing-runtime.json"),
        },
        "workers": [],
    }
    campaign = SimpleNamespace(read=lambda: doc)

    def update(callback):
        callback(doc)
        return doc

    campaign.update = update
    captured = holds.capture(campaign)
    rows = list(captured["hold_followups"].values())
    assert len(rows) == 1 and rows[0]["frontier"] == w.frontier
    assert "descendant_coverage" not in rows[0]["source"]


def test_same_frontier_guard_readmission_reports_latest_actual_descendant_receipt(
    public_ci_world, capsys
):
    w = public_ci_world
    dispatcher.main(w.argv)
    capsys.readouterr()
    w.check.update(status="completed", conclusion="success")
    w.snapshot.update(checks="SUCCESS")
    w.guard_mode = "blocked"
    dispatcher.main(w.argv)
    result = json.loads(capsys.readouterr().out)
    refs = result["descendant_receipts"]
    identities = [(ref["target"], ref["finite_frontier"]["frontier_id"]) for ref in refs]
    assert len(identities) == len(set(identities)) == 1
    root = resources.scoped_root(dispatcher._pipeline_state_path(w.raw))
    shard = resources.shard_dir(root, "owner/repo#12") / "state.json"
    state = pipeline._load(shard)
    descriptor = refs[0]["finite_frontier"]
    assert refs[0] == state["stage_successors"][descriptor["frontier_id"]]["receipt_refs"][-1]
    receipt, sha = resources.read_round_receipt(Path(refs[0]["receipt_path"]))
    assert sha == refs[0]["receipt_sha256"]
    assert (
        receipt["targets"]["owner/repo#12"]
        == state["frontiers"][descriptor["frontier_id"]]["items"]["owner/repo#12"]
    )
    assert not w.verifies and w.events["provider"] == 0
