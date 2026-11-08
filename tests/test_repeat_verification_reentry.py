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


@pytest.mark.parametrize("large_snapshot", [False, True])
def test_changed_finite_action_holds_before_collection_or_model(
    public_merge_world, monkeypatch, capsys, large_snapshot
):
    import hashlib
    import json
    from pathlib import Path

    w = public_merge_world
    if large_snapshot:
        # The real #984 snapshot had 18,583B of excerpts plus two 11,311B
        # lifecycle views. A changed frontier must still produce its precise
        # no-effect hold through the unchanged 32 KiB callback boundary.
        w.snapshot["verifier_comment_excerpts"] = ["excerpt" * 2700]
        w.snapshot["verifier_lifecycle"] = {"retained": "lifecycle" * 1500}
        w.snapshot["body"] = "original acceptance criterion\n" * 800
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
    if large_snapshot:
        prep = row["results"]["prepare"]
        ref = prep["snapshot_ref"]
        raw = Path(ref["path"]).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == ref["sha256"]
        retained = json.loads(raw)
        for field in ("verifier_comment_excerpts", "verifier_lifecycle", "body"):
            assert retained[field] == w.snapshot[field]
        assert len(raw) > 32768
        assert len(json.dumps(prep, separators=(",", ":")).encode()) < 32768
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
