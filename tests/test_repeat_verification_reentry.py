"""Existing accounting and native request actors compose for fresh evidence."""

import copy
import time

import pytest
from test_closer_disposition import HEAD, TARGET, fixture
from test_closer_merge_successor_integration import public_merge_world as public_merge_world
from test_closer_reasoning_boundary import native_world as native_world
from test_closer_verifier_attribution import MERGE, archive
from test_closer_verifier_attribution import world as world

import closer_dispatcher as dispatcher
import closer_disposition as disposition
import verification_events


def request(snapshot):
    return dict(
        schema="verification-request-reentry/v1",
        head=snapshot["head"],
        merge=snapshot["merge"],
        review_revision=snapshot.get("review_revision"),
        source_digest=dispatcher.source_digest(snapshot["sources"]),
        predecessor_event_id=(snapshot["verifier"].get("verification_event") or {}).get("event_id")
        or verification_events.legacy_id(snapshot["verifier"]),
        material=[dict(kind="comment", url="https://github.com/owner/repo/pull/12#issuecomment-9")],
        proof_hashes=["a" * 64],
        question="Does the recovered output satisfy the original task?",
    )


def marker():
    return dict(
        schema="verifier-corpus-decision/v1",
        repo="owner/repo",
        pr="12",
        head_sha=HEAD,
        evaluated_sha=MERGE,
        run_id="99",
        run_attempt="1",
        verdict="NON_PASS",
        ci_failed=False,
        provider_verdicts=["CONCERNS"],
    )


def configure(world):
    prior = marker()
    from test_repeat_verification_events import event

    prior["verification_event"] = event(repo="owner/repo", pr="12")
    world.snapshot.update(kind="pr", sources=[], review_revision="reviews", verifier=prior)
    world.current = copy.deepcopy(world.snapshot)
    world.backend.dispositions[TARGET] = {
        "ordinary_nonpass": {"head": HEAD, "verification_request": request(world.snapshot)}
    }
    world.data = archive(12, MERGE)
    original_api = world.backend.gh.api

    def api(endpoint, **kwargs):
        if endpoint == "repos/owner/repo":
            return {"default_branch": "main"}
        return original_api(endpoint, **kwargs)

    world.backend.gh.api = api


def test_completed_prior_delivery_does_not_block_fresh_evidence_request(world):
    configure(world)
    assert world.backend.select(world.snapshot)[0] == "post_merge_verify"
    result = world.backend.initiate_verify(TARGET, world.snapshot)
    assert result["status"] == "completed"
    assert world.posts == ["repos/owner/repo/actions/workflows/agents-verifier.yml/dispatches"]
    assert (
        result["verification_predecessor"]
        == world.snapshot["verifier"]["verification_event"]["event_id"]
    )


def test_uncertain_repeat_post_remains_uncertain_and_does_not_relabel(world):
    configure(world)
    world.post_timeout = True
    with pytest.raises(TimeoutError, match="POST uncertain"):
        world.backend.initiate_verify(TARGET, world.snapshot)
    assert world.posts == ["repos/owner/repo/actions/workflows/agents-verifier.yml/dispatches"]


def test_same_sha_material_question_does_not_require_local_acceptance_pass(tmp_path):
    snap, packet, ref, prep, interpretation = fixture(tmp_path)
    previous = marker()
    from test_repeat_verification_events import event

    previous["verification_event"] = event(repo="owner/repo", pr="12")
    snap.update(state="MERGED", labels=[], merge=MERGE, verifier=previous)
    pending = disposition.pending_nonpass_findings(snap, packet)
    proof = interpretation["proposal"]["decision"]["criteria"][0]
    interpretation["proposal"]["resolutions"] = [
        dict(
            finding_id=row["finding_id"],
            status="unresolved_missing_evidence",
            next_guarded_action="post_merge_verify",
            reason="Recovered evidence makes the question evaluable",
            evidence=proof["evidence"],
            proofs=proof["proofs"],
        )
        for row in pending
    ]
    interpretation["proposal"]["verification_request"] = dict(
        material=request(snap)["material"],
        question="Evaluate recovered output against the original task",
    )
    result = disposition.integrate_ordinary(TARGET, snap, ref, prep, interpretation)
    assert result["status"] == "ordinary_nonpass", result
    assert result["disposition"]["acceptance_authority"] is False
    assert verification_events.request_matches(snap, result["disposition"])
    changed = copy.deepcopy(snap)
    changed["sources"][0]["body"] += "\nNew requirement"
    assert verification_events.request_matches(changed, result["disposition"]) is None


def test_dispatch_204_and_known_422_are_distinct_from_uncertain_transport(monkeypatch):
    monkeypatch.setattr(dispatcher.gh_capacity, "throttle", lambda *a, **k: {"action": "allow"})
    endpoint = "repos/o/r/actions/workflows/agents-verifier.yml/dispatches"
    gh = dispatcher.GitHub(deadline=time.time() + 60, api_fn=lambda *a, **k: (0, None))
    assert gh.api(endpoint, method="POST") == {"accepted": True}
    gh.api_fn = lambda *a, **k: (1, {"status": "422", "message": "Unexpected inputs"})
    with pytest.raises(dispatcher.GitHubNoEffectRejected):
        gh.api(endpoint, method="POST")
    gh.api_fn = lambda *a, **k: (1, None)
    with pytest.raises(RuntimeError, match="outcome unknown"):
        gh.api(endpoint, method="POST")


def test_metadata_get_failure_is_known_not_started(world):
    configure(world)
    original = world.backend.gh.api

    def failed(endpoint, **kwargs):
        if endpoint == "repos/owner/repo":
            raise TimeoutError("read only metadata unavailable")
        return original(endpoint, **kwargs)

    world.backend.gh.api = failed
    result = world.backend.initiate_verify(TARGET, world.snapshot)
    assert result["status"] == "not_started"
    assert not world.posts


@pytest.mark.parametrize(
    "subject",
    ["stranske/Manager-Database#1817", "stranske/Orchestrator#667", "stranske/Orchestrator#683"],
)
def test_skipped_unattributed_repository_run_never_blocks_initial_verification(world, subject):
    # Compact reproduction of retained native receipts: all five jobs skipped,
    # no evaluator/capture, and no immutable subject association. Orch667/683
    # were both refused for the same unrelated repository run39960803170.
    repo, number = subject.split("#")
    world.snapshot.update(
        target=subject,
        kind="pr",
        sources=[],
        verifier=None,
        labels=["verify:compare"],
        verifier_history_complete=True,
        verifier_runs=[],
    )
    world.current = copy.deepcopy(world.snapshot)
    world.run.update(
        id=39874908943 if "Manager" in repo else 39960803170,
        status="completed",
        conclusion="skipped",
        pull_requests=[],
    )
    requests = []

    def api(endpoint, **kwargs):
        requests.append((endpoint, kwargs))
        if "&status=" in endpoint:
            return {"total_count": 0, "workflow_runs": []}
        if endpoint == f"repos/{repo}":
            return {"default_branch": "main"}
        if kwargs.get("method") == "POST":
            return {"accepted": True}
        raise AssertionError("Admission must not acquire historical jobs or capture artifacts")

    world.backend.gh.api = api
    assert world.backend.select(world.snapshot)[0] == "post_merge_verify"
    basis = world.backend.verification_request_basis(world.snapshot)
    changed_head = dict(world.snapshot, head="e" * 40)
    assert world.backend.verification_request_basis(changed_head) != basis
    result = world.backend.initiate_verify(subject, world.snapshot)
    assert result["status"] == "completed"
    writes = [(endpoint, kwargs) for endpoint, kwargs in requests if kwargs.get("method") == "POST"]
    assert len(writes) == 1
    assert writes[0][1]["fields"] == {
        "ref": "main",
        "inputs[pr_number]": number,
        "inputs[mode]": "compare",
        "inputs[verification_request_basis]": basis,
    }
    assert result["verification_request_basis"] == basis
    assert result["verification_post_accepted"] == {
        "mutation_kind": "workflow_dispatch",
        "request_basis": basis,
        "correlation_input": basis,
    }
    assert not world.downloads


def test_legacy_material_selection_alone_does_not_invent_recovery_progress(world):
    configure(world)
    world.snapshot["verifier"] = marker()
    proposed = {"head": HEAD, "verification_request": request(world.snapshot)}
    assert verification_events.request_matches(world.snapshot, proposed) is None


@pytest.mark.parametrize("recover_abandoned", [False, True])
def test_concerns_recovered_material_same_sha_reenters_existing_verifier_then_closure(
    monkeypatch, tmp_path, recover_abandoned
):
    """Real collector/accounting/selection/ingestion/closure guard; fake external reports/actor transport."""
    import json
    from types import SimpleNamespace

    from test_repeat_verification_events import event

    import feedback
    import verifier_evidence

    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "brain.db")
    monkeypatch.setenv("ORCH_SUPERVISOR_CLOSE", "1")
    snap, packet, ref, prep, interpretation = fixture(tmp_path / "capture")
    old = marker()
    old["verification_event"] = event(repo="owner/repo", pr="12")
    snap.update(state="MERGED", merge=MERGE, labels=["verify:compare"], verifier=old)
    backend = dispatcher.Closer(
        SimpleNamespace(deadline=time.time() + 60),
        state_path=tmp_path / "queue.json",
        workers=True,
        apply=True,
    )
    assert backend.select(snap)[0] == "disposition"
    original_interpretation = copy.deepcopy(interpretation)
    retained_proof = interpretation["proposal"]["decision"]["criteria"][0]
    interpretation["proposal"].update(
        resolutions=[
            dict(
                finding_id=row["finding_id"],
                status="unresolved_missing_evidence",
                next_guarded_action="post_merge_verify",
                reason="Recovered exact output can now be evaluated",
                evidence=retained_proof["evidence"],
                proofs=retained_proof["proofs"],
            )
            for row in disposition.pending_nonpass_findings(snap, packet)
        ],
        verification_request=dict(
            material=request(snap)["material"],
            question="Evaluate the recovered report output against the unchanged original task",
        ),
    )
    accounted = disposition.integrate_ordinary(TARGET, snap, ref, prep, interpretation)
    assert (
        accounted["status"] == "ordinary_nonpass"
        and not accounted["disposition"]["acceptance_authority"]
    )
    backend.dispositions[TARGET] = {"ordinary_nonpass": accounted["disposition"]}
    assert backend.select(snap)[0] == "post_merge_verify"

    current_event = event("e", repo="owner/repo", pr="12")
    pending = dict(
        schema=verifier_evidence.STATE_MARKER,
        repo="owner/repo",
        pr="12",
        head_sha=HEAD,
        evaluated_sha=MERGE,
        run_id="100",
        run_attempt="1",
        status="pending",
        verification_event=current_event,
        assessment_identity={
            "mode": "compare",
            "provider": "",
            "model": "",
            "model2": "",
            "canonical": True,
        },
        predecessor_event_id=old["verification_event"]["event_id"],
    )
    fresh = {
        **old,
        "run_id": "100",
        "verdict": "PASS",
        "ci_failed": False,
        "provider_verdicts": ["PASS"],
        "verification_event": current_event,
        "predecessor_event_id": old["verification_event"]["event_id"],
    }

    def observed(rows, include_pending=False):
        pr = dict(
            number=12,
            headRefOid=HEAD,
            mergeCommit={"oid": MERGE},
            comments={
                "nodes": [
                    dict(
                        author={"login": "github-actions[bot]"},
                        url=f"https://github.com/owner/repo/pull/12#issuecomment-{i+1}",
                        body=f"<!-- {row['schema']} {json.dumps(row)} -->",
                    )
                    for i, row in enumerate(rows)
                ]
            },
        )
        return verifier_evidence.decision_from_pr("owner/repo", pr, include_pending=include_pending)

    assert observed([old, pending]) is None
    awaiting = {**snap, "verifier": None, "verifier_lifecycle": observed([old, pending], True)}
    assert backend.select(awaiting)[0] == "wait"
    if recover_abandoned:
        pending["execution_binding"] = {
            "schema": "verifier-execution-binding/v1",
            "workflow_path": ".github/workflows/agents-verifier.yml",
            "job_name": "Run post-merge verifier",
            "pending_step": "Record verification event pending",
            "evaluator_step": "Run multi-provider comparison",
        }
        awaiting["verifier_lifecycle"] = observed([old, pending], True)
        run = dict(
            id=100,
            run_attempt=1,
            repository={"full_name": "owner/repo"},
            head_repository={"full_name": "owner/repo"},
            path=".github/workflows/agents-verifier.yml",
            event="workflow_dispatch",
            status="completed",
            conclusion="cancelled",
            head_sha="f" * 40,
        )
        jobs = {
            "total_count": 1,
            "jobs": [
                {
                    "id": 1,
                    "name": "Run post-merge verifier",
                    "run_id": 100,
                    "run_attempt": 1,
                    "head_sha": "f" * 40,
                    "status": "completed",
                    "steps": [
                        {
                            "name": "Record verification event pending",
                            "number": 2,
                            "status": "completed",
                            "conclusion": "success",
                        },
                        {
                            "name": "Run multi-provider comparison",
                            "number": 3,
                            "status": "completed",
                            "conclusion": "skipped",
                        },
                    ],
                }
            ],
        }
        posts = []

        def api(endpoint, *, method="GET", **kwargs):
            if method == "POST":
                posts.append((endpoint, kwargs))
                return None
            if endpoint.endswith(("/runs/100", "/runs/100/attempts/1")):
                return run
            if "/attempts/1/jobs?" in endpoint:
                return jobs
            if endpoint == "repos/owner/repo":
                return {"default_branch": "main"}
            if "/runs?" in endpoint:
                return {"total_count": 0, "workflow_runs": []}
            raise AssertionError(endpoint)

        monkeypatch.setenv("ORCH_STORAGE_STATE_DIR", str(tmp_path / "storage-control"))
        backend.gh = SimpleNamespace(
            api=api, snapshot=lambda _target: awaiting, deadline=time.time() + 60
        )
        prepared = backend.verification_snapshot(awaiting)
        assert backend.select(prepared)[0] == "post_merge_verify"
        assert backend.initiate_verify(TARGET, prepared)["status"] == "completed"
        assert len(posts) == 1 and posts[0][0].endswith("agents-verifier.yml/dispatches")
        assert prepared["verifier_lifecycle"]["status"] == "pending"
        fresh["run_id"] = "101"  # Same material event, fresh authenticated evaluation attempt.
    terminal = observed([old, pending, fresh])
    assert terminal and terminal["verdict"] == "PASS" and terminal["evaluated_sha"] == MERGE
    feedback.record_run("r", TARGET, "implement", "codex", source="keepalive")
    feedback.record_outcome("r", merged=True, verifier_verdict="NON_PASS")
    assert verifier_evidence.record_decision("r", "owner/repo", 12, terminal)
    snap.update(verifier=terminal, verifier_lifecycle=terminal)
    accepted = disposition.integrate_ordinary(TARGET, snap, ref, prep, original_interpretation)
    assert accepted["status"] == "accepted", accepted
    backend.dispositions[TARGET] = accepted["disposition"]
    assert backend.select(snap)[0] == "close_source"
    calls = []

    def closure_transport(command, **_kwargs):
        calls.append(command)
        assert "close_verified_issues" in command[2]
        return SimpleNamespace(stdout=json.dumps({"closed": 1, "errors": 0}))

    backend.run_fn = closure_transport
    assert backend.close_sources(TARGET, snap)["status"] == "completed"
    assert len(calls) == 1
    with feedback._conn() as conn:
        assert conn.execute(
            "SELECT verifier_verdict FROM outcomes WHERE run_id='r'"
        ).fetchone() == ("PASS",)
        assert conn.execute(
            "SELECT COUNT(*) FROM completion_events WHERE producer='verifier_evidence'"
        ).fetchone() == (3,)
    assert old["verdict"] == "NON_PASS"


def test_ready_native_verification_needs_no_model_capacity(public_merge_world, monkeypatch, capsys):
    import json

    w = public_merge_world
    monkeypatch.setattr(
        dispatcher, "_pipeline_capacity", lambda: {"io": 2, "low": 0, "exception": 0}
    )
    assert dispatcher.main(w.argv) == 78
    result = json.loads(capsys.readouterr().out)
    assert len(w.merges) == len(w.verifies) == 1, result
    assert w.events["provider"] == 0
    child = result["merge_verification_successors"][TARGET]["native_result"]
    assert child["validation"]["scoped_completion_valid"] is True
    prep = child["targets"][TARGET]["results"]["prepare"]
    assert dispatcher.native_interpretation_ready(prep, target=TARGET)
    for change in (
        "head",
        "revision",
        "target",
        "packet",
        "collect",
        "disposition",
        "ineligible",
        "gap",
    ):
        bad = copy.deepcopy(prep)
        if change == "head":
            bad["native_effect_contract"]["head"] = "f" * 40
        elif change == "revision":
            bad["snapshot"]["revision"] = "stale"
        elif change == "target":
            bad["snapshot"]["target"] = "owner/repo#99"
        elif change == "packet":
            bad["packet_ref"]["sha256"] = "0" * 64
        elif change in {"collect", "disposition"}:
            bad["proposal"]["action"] = "collect_evidence" if change == "collect" else "disposition"
        elif change == "ineligible":
            bad["proposal"]["eligible"] = False
        else:
            bad["collection_result"]["readiness"]["gaps"] = ["missing core"]
        assert not dispatcher.native_interpretation_ready(bad, target=TARGET), change


def test_native_dispatch_packet_collects_bound_core_without_optional_api_reads(world, tmp_path):
    import closer_evidence

    world.snapshot.update(kind="pr", sources=[], review_revision="reviews")

    def forbidden(*_args, **_kwargs):
        raise AssertionError("optional enrichment requested before native dispatch")

    packet = dispatcher.collect_native_evidence(
        world.snapshot,
        type("ReadOnly", (), {"api": staticmethod(forbidden)})(),
        tmp_path / "core",
        action="post_merge_verify",
        deadline=time.monotonic() + 2,
    )
    assert closer_evidence.validate_packet(packet)
    assert packet["head"] == world.snapshot["head"]
    assert packet["capture_revision"] == world.snapshot["revision"]
    assert packet["collection_result"]["readiness"]["ready"] is True
    for name in ("diff_files", "review_conversations", "ci_artifacts", "native_merge_verifier"):
        assert packet["sections"][name]["provenance"]["request_status"] == "not_requested"
        assert packet["sections"][name]["status"] != "complete"


def test_remote_discovery_preserves_pin_without_requiring_live_checkout(tmp_path):
    import hashlib
    import json

    import closer_quarantine

    path = tmp_path / "workspaces.json"
    missing = tmp_path / "not-materialized"
    path.write_text(
        json.dumps(
            {
                "schema": closer_quarantine.WORKSPACE_SCHEMA,
                "historical": {},
                "execution": {TARGET: str(missing)},
            }
        )
    )
    pin = hashlib.sha256(path.read_bytes()).hexdigest()
    mapping, _ = dispatcher.load_workspace_config(
        path, pin, require_pin=True, targets=[TARGET], validate_execution=False
    )
    assert mapping[TARGET.lower()] == str(missing)
    with pytest.raises(ValueError, match="unavailable"):
        dispatcher.load_workspace_config(path, pin, require_pin=True, targets=[TARGET])
    with pytest.raises(ValueError, match="SHA256"):
        dispatcher.load_workspace_config(
            path, "0" * 64, require_pin=True, targets=[TARGET], validate_execution=False
        )


def test_changed_finite_action_holds_before_collection_or_model(
    public_merge_world, monkeypatch, capsys
):
    import json

    w = public_merge_world
    monkeypatch.setattr(
        dispatcher.Closer, "select", lambda *_: ("collect_evidence", "changed action", True, None)
    )

    def forbidden(*_args, **_kwargs):
        raise AssertionError("changed frozen action reached expensive collection")

    monkeypatch.setattr(dispatcher, "collect_native_evidence", forbidden)
    assert dispatcher.main(w.argv) == 78
    result = json.loads(capsys.readouterr().out)
    row = result["targets"][TARGET]
    assert row["status"] == "held"
    assert row["error"] == "frontier action binding differs"
    assert row["results"]["prepare"]["finite_binding_changed"] is True
    assert w.events["provider"] == 0
    assert w.merges == w.verifies == []


def test_minimum_dispatch_retains_bound_progress_without_optional_fetch(tmp_path):
    import closer_evidence

    snap, prior, _, _, _ = fixture(tmp_path / "retained")

    def forbidden(*_args, **_kwargs):
        raise AssertionError("dispatch attempted optional retrieval")

    transport = type("ReadOnly", (), {"api": staticmethod(forbidden)})()
    fresh = dispatcher.collect_native_evidence(
        snap,
        transport,
        tmp_path / "retained",
        previous=prior,
        action="post_merge_verify",
        deadline=time.monotonic() + 2,
    )
    old = prior["sections"]["ci_artifacts"]
    assert fresh["sections"]["ci_artifacts"]["sha256"] == old["sha256"]
    assert closer_evidence.validate_packet(fresh)
    from pathlib import Path

    Path(old["retained"]["path"]).write_bytes(b"corrupt retained section")
    corrupt = dispatcher.collect_native_evidence(
        snap,
        transport,
        tmp_path / "corrupt",
        previous=prior,
        action="post_merge_verify",
        deadline=time.monotonic() + 2,
    )
    assert corrupt["sections"]["ci_artifacts"]["provenance"]["request_status"] == "not_requested"
    stale = dispatcher.collect_native_evidence(
        {**snap, "revision": "new-current-revision"},
        transport,
        tmp_path / "stale",
        previous=fresh,
        action="post_merge_verify",
        deadline=time.monotonic() + 2,
    )
    assert stale["sections"]["diff_files"]["provenance"]["request_status"] == "not_requested"


def test_missing_product_checkout_blocks_model_consuming_public_path(native_world, capsys):
    import hashlib
    import json

    import closer_quarantine

    world = native_world
    mapping = world.root / "workspace-map.json"
    mapping.write_text(
        json.dumps(
            {
                "schema": closer_quarantine.WORKSPACE_SCHEMA,
                "historical": {},
                "execution": {TARGET: str(world.root / "missing-product")},
            }
        )
    )
    world.argv.extend(
        [
            "--workspaces",
            str(mapping),
            "--workspaces-sha256",
            hashlib.sha256(mapping.read_bytes()).hexdigest(),
        ]
    )
    assert dispatcher.main(world.argv) == 78
    result = json.loads(capsys.readouterr().out)
    assert world.events["provider"] == world.events["effect"] == 0
    row = result["targets"][TARGET]
    assert row["status"] != "completed"
    interpretation = row.get("results", {}).get("interpret", {})
    assert interpretation.get("classification") == "workspace_unavailable", row
    assert interpretation.get("status") == "deferred"


def pending_execution(world):
    from test_repeat_verification_events import event

    import verifier_evidence

    pending = {
        key: value
        for key, value in marker().items()
        if key not in {"verdict", "ci_failed", "provider_verdicts"}
    }
    pending.update(
        schema=verifier_evidence.STATE_MARKER,
        status="pending",
        verification_event=event(repo="owner/repo", pr="12"),
        assessment_identity={
            "mode": "compare",
            "provider": "",
            "model": "",
            "model2": "",
            "canonical": True,
        },
        execution_binding={
            "schema": "verifier-execution-binding/v1",
            "workflow_path": ".github/workflows/agents-verifier.yml",
            "job_name": "Run post-merge verifier",
            "pending_step": "Record verification event pending",
            "evaluator_step": "Run multi-provider comparison",
        },
    )
    world.snapshot.update(
        kind="pr",
        sources=[],
        review_revision="reviews",
        verifier=None,
        verifier_lifecycle=pending,
        labels=["verify:compare"],
    )
    world.current = copy.deepcopy(world.snapshot)
    run = dict(
        id=99,
        run_attempt=1,
        repository={"full_name": "owner/repo"},
        head_repository={"full_name": "owner/repo"},
        path=".github/workflows/agents-verifier.yml",
        event="workflow_dispatch",
        status="completed",
        conclusion="cancelled",
        head_sha="f" * 40,
    )
    job = dict(
        id=17,
        name="Verify / Run post-merge verifier",
        status="completed",
        run_id=99,
        run_attempt=1,
        head_sha="f" * 40,
        steps=[
            dict(
                name="Record verification event pending",
                number=2,
                status="completed",
                conclusion="success",
            ),
            dict(
                name="Run multi-provider comparison",
                number=3,
                status="completed",
                conclusion="skipped",
            ),
        ],
    )
    inventory = dict(total_count=1, jobs=[job])
    original = world.backend.gh.api

    def api(endpoint, **kwargs):
        if endpoint in {
            "repos/owner/repo/actions/runs/99",
            "repos/owner/repo/actions/runs/99/attempts/1",
        }:
            return run
        if endpoint == "repos/owner/repo/actions/runs/99/attempts/1/jobs?per_page=100":
            return inventory
        if endpoint == "repos/owner/repo":
            return {"default_branch": "main"}
        return original(endpoint, **kwargs)

    world.backend.gh.api = api
    return pending, run, inventory


def test_native_pending_no_start_reenters_same_request_without_rewriting_marker(world):
    pending, _, inventory = pending_execution(world)
    original = copy.deepcopy(pending)
    assert world.backend.select(world.snapshot)[0] == "wait"
    observed = world.backend.verification_snapshot(world.snapshot)
    assert world.backend.select(observed)[0] == "post_merge_verify"
    assert observed["verifier_lifecycle"] == original
    assert observed["pending_no_evaluation"]["jobs_sha256"] == verification_events.content_hash(
        inventory["jobs"]
    )
    result = world.backend.execute(
        TARGET, "post_merge_verify", observed, {"deadline": time.time() + 10}
    )
    assert result["status"] == "completed"
    assert world.posts == ["repos/owner/repo/actions/workflows/agents-verifier.yml/dispatches"]
    assert observed["verifier_lifecycle"] == original


@pytest.mark.parametrize(
    "damage",
    [
        "started",
        "active",
        "foreign",
        "incomplete",
        "old_binding",
        "other_evaluator",
        "mode_mismatch",
        "malformed_mode",
        "execution_step_mismatch",
    ],
)
def test_native_pending_no_start_proof_never_infers_missing_or_uncertain_effect(world, damage):
    _, run, inventory = pending_execution(world)
    observed = world.backend.verification_snapshot(world.snapshot)
    assert "pending_no_evaluation" in observed
    if damage == "started":
        inventory["jobs"][0]["steps"][1]["conclusion"] = "failure"
    elif damage == "active":
        run["status"] = "in_progress"
    elif damage == "foreign":
        run["repository"]["full_name"] = "owner/other"
    elif damage == "incomplete":
        inventory["total_count"] = 2
    elif damage in {"mode_mismatch", "malformed_mode"}:
        world.current["verifier_lifecycle"]["assessment_identity"]["mode"] = (
            "evaluate" if damage == "mode_mismatch" else "unknown-future-mode"
        )
        assert (
            verification_events.pending_no_evaluation_proof(
                world.current["verifier_lifecycle"], run, inventory
            )
            is None
        )
    elif damage == "execution_step_mismatch":
        world.current["verifier_lifecycle"]["execution_binding"][
            "evaluator_step"
        ] = "Run LLM evaluation"
        inventory["jobs"][0]["steps"][1]["name"] = "Run LLM evaluation"
        assert (
            verification_events.pending_no_evaluation_proof(
                world.current["verifier_lifecycle"], run, inventory
            )
            is None
        )
    elif damage == "other_evaluator":
        inventory["jobs"][0]["steps"].append(
            dict(name="Run LLM evaluation", number=4, status="completed", conclusion="failure")
        )
    else:
        world.current["verifier_lifecycle"].pop("execution_binding")
    result = world.backend.execute(
        TARGET, "post_merge_verify", observed, {"deadline": time.time() + 10}
    )
    assert result["status"] == "blocked"
    assert world.posts == []
    # A failed refreshed proof never inherits a previous transient permission.
    fresh = world.backend.verification_snapshot(world.current)
    assert "pending_no_evaluation" not in fresh


@pytest.mark.parametrize("cross_frontier", [False, True])
def test_accepted_pending_retry_receipt_prevents_second_dispatch_before_new_marker(
    world, cross_frontier
):
    import json
    from pathlib import Path

    import closer_queue

    pending_execution(world)
    observed = world.backend.verification_snapshot(world.snapshot)
    first = world.backend.execute(
        TARGET, "post_merge_verify", observed, {"deadline": time.time() + 10}
    )
    assert first["status"] == "completed" and len(world.posts) == 1
    state = closer_queue._new_state()
    state["items"][TARGET] = {
        "operation": {
            "id": "accepted-retry",
            "action": "post_merge_verify",
            "status": "completed",
            "revision": observed["revision"],
            "binding": {"pr_target": TARGET, "head": HEAD, "merge": MERGE},
            "result": first,
        }
    }
    operation = state["items"][TARGET]["operation"]
    if cross_frontier:
        import closer_frontier_policy

        frozen = {
            TARGET: {
                "target": TARGET,
                "revision": observed["revision"],
                "action": "post_merge_verify",
                "eligible": True,
                "operation_binding": operation["binding"],
            }
        }
        prior = closer_frontier_policy.make_frontier_from_observations(
            frozen, owner="owner", frontier_id="accepted-old"
        )
        current = closer_frontier_policy.make_frontier_from_observations(
            frozen, owner="owner", frontier_id="fresh-new"
        )
        operation["finite_frontier"] = prior
        state["frontiers"] = {
            "accepted-old": {
                "finite_frontier": prior,
                "items": {TARGET: state["items"][TARGET]},
                "round": {},
            }
        }
        state["items"] = {}
        world.backend.finite_frontier = current
    Path(world.backend.state_path).write_text(json.dumps(state))
    assert world.backend.select(observed)[0] == "wait"
    second = world.backend.execute(
        TARGET, "post_merge_verify", observed, {"deadline": time.time() + 10}
    )
    assert second["status"] == "blocked" and len(world.posts) == 1
    # Unrelated operation identity cannot turn this request into a hold.
    operation["binding"]["pr_target"] = "owner/repo#999"
    Path(world.backend.state_path).write_text(json.dumps(state))
    assert world.backend.select(observed)[0] == "post_merge_verify"


@pytest.mark.parametrize(
    "newer_state",
    [
        "skipped",
        "active",
        "started",
        "missing",
        "drift",
        "old_changed",
        "foreign_attempt",
        "wrong_head",
        "incomplete",
        "intermediate_started",
        "excess_attempts",
    ],
)
def test_native_pending_attempt_recovery_binds_all_newer_attempts(world, newer_state):
    pending, first_run, first_inventory = pending_execution(world)
    original_marker = copy.deepcopy(pending)
    second_run = {**copy.deepcopy(first_run), "run_attempt": 2}
    second_inventory = copy.deepcopy(first_inventory)
    second_job = second_inventory["jobs"][0]
    second_job.update(id=18, run_attempt=2)
    # A later terminal skipped evaluator does not require a new successful pending
    # publication. The original pending marker retains its original attempt.
    second_job["steps"] = second_job["steps"][1:]
    if newer_state == "active":
        second_run["status"] = "in_progress"
    elif newer_state == "started":
        second_job["steps"][0]["conclusion"] = "failure"
    elif newer_state == "missing":
        second_job["steps"] = []
    elif newer_state == "foreign_attempt":
        second_run["repository"] = {"full_name": "owner/foreign"}
    elif newer_state == "wrong_head":
        second_run["head_sha"] = second_job["head_sha"] = "e" * 40
    elif newer_state == "incomplete":
        second_inventory["total_count"] = 2
    elif newer_state == "intermediate_started":
        second_job["steps"][0]["conclusion"] = "failure"
    latest_run = {**second_run}
    if newer_state in {"intermediate_started", "excess_attempts"}:
        latest_run["run_attempt"] = 3 if newer_state == "intermediate_started" else 12
    third_inventory = copy.deepcopy(first_inventory)
    third_inventory["jobs"][0].update(id=19, run_attempt=3)
    original_api = world.backend.gh.api
    latest_reads = 0
    old_reads = 0

    def api(endpoint, **kwargs):
        nonlocal latest_reads, old_reads
        if endpoint == "repos/owner/repo/actions/runs/99":
            latest_reads += 1
            if newer_state == "drift" and latest_reads > 1:
                return {**second_run, "run_attempt": 3}
            return copy.deepcopy(latest_run)
        if endpoint == "repos/owner/repo/actions/runs/99/attempts/3":
            return copy.deepcopy(latest_run)
        if endpoint == "repos/owner/repo/actions/runs/99/attempts/3/jobs?per_page=100":
            return copy.deepcopy(third_inventory)
        if endpoint == "repos/owner/repo/actions/runs/99/attempts/1":
            old_reads += 1
            if newer_state == "old_changed" and old_reads > 1:
                return {**first_run, "conclusion": "success"}
            return copy.deepcopy(first_run)
        if endpoint == "repos/owner/repo/actions/runs/99/attempts/2":
            return copy.deepcopy(second_run)
        if endpoint == "repos/owner/repo/actions/runs/99/attempts/2/jobs?per_page=100":
            return copy.deepcopy(second_inventory)
        return original_api(endpoint, **kwargs)

    world.backend.gh.api = api
    observed = world.backend.verification_snapshot(world.snapshot)
    assert observed["verifier_lifecycle"] == original_marker
    assert world.posts == []
    if newer_state == "skipped":
        proof = observed["pending_no_evaluation"]
        assert proof["run_attempt"] == "1"
        assert proof["latest_run_attempt"] == "2"
        assert proof["subsequent_attempts"][0]["run_attempt"] == "2"
        assert proof["subsequent_attempts"][0]["jobs_sha256"] == verification_events.content_hash(
            second_inventory["jobs"]
        )
        assert world.backend.select(observed)[0] == "post_merge_verify"
    else:
        assert "pending_no_evaluation" not in observed
        assert world.backend.select(observed)[0] == "wait"


def test_newer_old_run_attempt_cannot_supersede_an_accepted_new_dispatch(world):
    import json
    from pathlib import Path

    import closer_queue

    pending, first_run, first_inventory = pending_execution(world)
    original = world.backend.gh.api
    latest = {**first_run, "run_attempt": 2}
    inventory = copy.deepcopy(first_inventory)
    inventory["jobs"][0].update(id=18, run_attempt=2)

    def api(endpoint, **kwargs):
        if endpoint == "repos/owner/repo/actions/runs/99":
            return copy.deepcopy(latest)
        if endpoint == "repos/owner/repo/actions/runs/99/attempts/2":
            return copy.deepcopy(latest)
        if endpoint == "repos/owner/repo/actions/runs/99/attempts/2/jobs?per_page=100":
            return copy.deepcopy(inventory)
        if "/runs?" in endpoint:
            return {
                "total_count": 1,
                "workflow_runs": [
                    {
                        "id": 100,
                        "status": "in_progress",
                        "pull_requests": [],
                    }
                ],
            }
        return original(endpoint, **kwargs)

    original_basis = world.backend.verification_request_basis(world.snapshot)
    world.backend.gh.api = api
    observed = world.backend.verification_snapshot(world.snapshot)
    new_basis = world.backend.verification_request_basis(observed)
    assert new_basis == original_basis
    unchanged = copy.deepcopy(observed)
    unchanged["pending_no_evaluation"]["jobs_sha256"] = "0" * 64
    assert world.backend.verification_request_basis(unchanged) == new_basis
    state = closer_queue._new_state()
    state["items"][TARGET] = {
        "operation": {
            "id": "previous-request",
            "action": "post_merge_verify",
            "status": "completed",
            "revision": observed["revision"],
            "binding": {"pr_target": TARGET, "head": HEAD, "merge": MERGE},
            "result": {
                "verification_request_basis": original_basis,
                "verification_post_accepted": {
                    "mutation_kind": "workflow_dispatch",
                    "request_basis": original_basis,
                },
            },
        }
    }
    Path(world.backend.state_path).write_text(json.dumps(state))
    assert world.backend.select(observed)[0] == "wait"
    result = world.backend.execute(
        TARGET, "post_merge_verify", observed, {"deadline": time.time() + 10}
    )
    assert result["status"] == "blocked"
    assert world.posts == []
    assert observed["verifier_lifecycle"] == pending
