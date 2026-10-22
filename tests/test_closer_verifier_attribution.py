"""Semantic native verifier attribution and source-typed pre-POST waits."""

import copy
import hashlib
import io
import json
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_closer_merge_successor_integration import public_merge_world as public_merge_world
from test_closer_reasoning_boundary import native_world as native_world

import closer_dispatcher as native
import closer_queue as queue
import closer_verifier_boundary as boundary

TARGET = "owner/repo#12"
HEAD = "a" * 40
MERGE = "b" * 40


def archive(
    pr=858,
    merge="e" * 40,
    *,
    corrupt=False,
    omit=False,
    runid=99,
    manifest_only=False,
    duplicate_key=False,
    duplicate_manifest=False
):
    files = {
        "verifier-context.md": b"full retained original requirements",
        "verifier-pr-diff.patch": b"full exact changed code",
        "verifier-diff-summary.md": b"exact diff summary",
        "comparison.json": b'{"results":[],"report":"diagnostic only"}',
    }
    manifest = {
        "schema": "workflows-verifier-input-snapshot/v1",
        "capture_kind": "production",
        "input_limits": {"workflow_runs": 200, "artifacts": 400},
        "repository": "owner/repo",
        "pr": pr,
        "merge_sha": merge,
        "source_run_id": str(runid),
    }
    for name, key in [
        ("verifier-context.md", "context_sha256"),
        ("verifier-pr-diff.patch", "diff_sha256"),
        ("verifier-diff-summary.md", "diff_summary_sha256"),
    ]:
        manifest[key] = hashlib.sha256(files[name]).hexdigest()
    if corrupt:
        manifest["context_sha256"] = "0" * 64
    if omit:
        files.pop("verifier-pr-diff.patch")
    if manifest_only:
        files = {}
        for key in ("context_sha256", "diff_sha256", "diff_summary_sha256"):
            manifest.pop(key)
    raw = json.dumps(manifest)
    if duplicate_key:
        raw = raw[:-1] + ', "pr": 858}'
    files["verifier-input-manifest.json"] = raw.encode()
    if duplicate_manifest:
        files["other/verifier-input-manifest.json"] = raw.encode()
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        for name, value in files.items():
            z.writestr(name, value)
    return out.getvalue()


@pytest.fixture
def world(tmp_path, monkeypatch):
    snapshot = {
        "target": TARGET,
        "state": "MERGED",
        "revision": "revision",
        "head": HEAD,
        "merge": MERGE,
        "merged_at": "2026-10-21T00:00:00Z",
        "labels": [],
    }
    run = {
        "id": 99,
        "run_attempt": 1,
        "head_sha": "d" * 40,
        "repository": {"full_name": "owner/repo"},
        "path": ".github/workflows/agents-verifier.yml",
        "event": "workflow_dispatch",
        "status": "completed",
        "conclusion": "success",
        "pull_requests": [],
        "created_at": "2026-10-22T00:00:00Z",
    }
    w = SimpleNamespace(
        snapshot=snapshot,
        run=run,
        data=archive(),
        posts=[],
        reads=[],
        downloads=[],
        history_extra=0,
        fail=None,
        current=copy.deepcopy(snapshot),
        post_timeout=False,
        expired=False,
    )

    def api(endpoint, **kwargs):
        w.reads.append(endpoint)
        if kwargs.get("method") == "POST":
            w.posts.append(endpoint)
            if w.post_timeout:
                raise TimeoutError("POST uncertain")
            w.current["labels"] = ["verify:compare"]
            return []
        if w.fail:
            raise w.fail
        if endpoint.endswith("/actions/runs/99"):
            return copy.deepcopy(w.run)
        if endpoint.endswith("/artifacts?per_page=100"):
            return {
                "total_count": 1,
                "artifacts": [
                    {
                        "id": 7,
                        "name": "comparison-results-99",
                        "expired": w.expired,
                        "digest": "sha256:" + hashlib.sha256(w.data).hexdigest(),
                        "size_in_bytes": len(w.data),
                        "workflow_run": {"id": 99, "head_sha": w.run["head_sha"]},
                    }
                ],
            }
        if "&status=" in endpoint:
            return {"total_count": 0, "workflow_runs": []}
        return {"total_count": 1 + w.history_extra, "workflow_runs": [w.run]}

    def fetch(*args, **kwargs):
        w.downloads.append(1)
        return w.data

    gh = native.GitHub(deadline=time.time() + 120)
    gh.api = api
    gh.snapshot = lambda _: copy.deepcopy(w.current)
    w.backend = native.Closer(gh, state_path=tmp_path / "queue.json", workers=True, apply=True)
    monkeypatch.setattr(native, "native_artifact_fetcher", lambda _: fetch)
    return w


def validate(result, snapshot):
    proof = result["verifier_resource_wait"]
    op = {
        "status": "resource_wait",
        "action": "post_merge_verify",
        "revision": snapshot["revision"],
        "binding": {
            "pr_target": snapshot["target"],
            "head": snapshot["head"],
            "merge": snapshot["merge"],
        },
        "invoked_at": proof["observed_at"] - 1,
        "completed_at": proof["observed_at"] + 1,
        "result": result,
    }
    assert queue.verifier_resource_wait_proof(op, snapshot["target"]) == proof
    return op


def test_later_foreign_manual_run_does_not_block_current_verification(world):
    result = world.backend.initiate_verify(TARGET, world.snapshot)
    assert result["status"] == "completed" and len(world.posts) == 1
    assert len(world.downloads) == 1
    # Retained immutable capture avoids downloading it again on another read.
    world.backend.initiate_verify(TARGET, world.snapshot)
    assert len(world.downloads) == 1


@pytest.mark.parametrize(
    "pr,merge,cause",
    [(12, MERGE, "existing_delivery"), (12, "e" * 40, "delivery_revision_mismatch")],
)
def test_same_pr_never_reruns_and_wrong_merge_is_not_foreign(world, pr, merge, cause):
    world.data = archive(pr, merge)
    result = world.backend.initiate_verify(TARGET, world.snapshot)
    assert not world.posts and result["verifier_resource_wait"]["cause"] == cause
    validate(result, world.snapshot)


@pytest.mark.parametrize(
    "change", ["corrupt", "missing", "incomplete", "read", "merge_time", "changed", "label"]
)
def test_all_known_prewrite_failures_are_typed_no_effect(world, change):
    if change == "corrupt":
        world.data = archive(corrupt=True)
    elif change == "omit":
        world.data = archive(omit=True)
    elif change == "missing":
        world.run["run_attempt"] = 2
    elif change == "incomplete":
        world.history_extra = 1
    elif change == "read":
        world.fail = TimeoutError("GET failed")
    elif change == "merge_time":
        world.snapshot["merged_at"] = None
    elif change == "changed":
        world.run["created_at"] = "2026-10-20T00:00:00Z"
        world.current["revision"] = "new"
    else:
        world.run["created_at"] = "2026-10-20T00:00:00Z"
        world.current["labels"] = ["verify:compare"]
    result = world.backend.initiate_verify(TARGET, world.snapshot)
    assert result["status"] == "resource_wait" and not world.posts
    validate(result, world.snapshot)


def test_timeout_after_post_is_never_a_known_no_effect(world):
    world.run["created_at"] = "2026-10-20T00:00:00Z"
    world.post_timeout = True
    with pytest.raises(TimeoutError, match="POST uncertain"):
        world.backend.initiate_verify(TARGET, world.snapshot)
    assert len(world.posts) == 1


def test_rehashed_binding_or_cause_mutation_still_refused(world):
    world.data = archive(12, MERGE)
    result = world.backend.initiate_verify(TARGET, world.snapshot)
    op = validate(result, world.snapshot)
    for field, value in [("head", "c" * 40), ("cause", "generic_no_effect"), ("requirements", [])]:
        changed = copy.deepcopy(op)
        proof = changed["result"]["verifier_resource_wait"]
        proof[field] = value
        proof["sha256"] = boundary.digest({k: v for k, v in proof.items() if k != "sha256"})
        assert queue.verifier_resource_wait_proof(changed, TARGET) is None


def test_explicit_other_pr_terminal_run_needs_no_optional_capture(world):
    world.run.update(event="pull_request_target", pull_requests=[{"number": 858}])
    world.data = b"optional capture unavailable"
    result = world.backend.initiate_verify(TARGET, world.snapshot)
    assert result["status"] == "completed" and len(world.posts) == 1 and not world.downloads
    assert not any("/artifacts?" in x for x in world.reads)


@pytest.mark.parametrize("created", ["", None, "not-a-timestamp", "2026-10-22T00:00:00"])
def test_missing_or_ambiguous_time_cannot_exclude_candidate(world, created):
    world.run["created_at"] = created
    result = world.backend.initiate_verify(TARGET, world.snapshot)
    assert result["status"] == "resource_wait" and not world.posts
    validate(result, world.snapshot)


@pytest.mark.parametrize(
    "case", ["foreign", "wrong_merge", "corrupt", "history", "capacity", "deadline"]
)
def test_public_cli_real_queue_typed_attribution_boundary(
    public_merge_world, monkeypatch, capsys, case
):
    w = public_merge_world
    transport = native.gh_capacity.gh_run
    run = {
        "id": 99,
        "run_attempt": 1,
        "head_sha": "e" * 40,
        "status": "completed",
        "conclusion": "success",
        "path": ".github/workflows/agents-verifier.yml",
        "event": "workflow_dispatch",
        "pull_requests": [],
        "repository": {"full_name": "owner/repo"},
        "created_at": "2026-10-22T00:00:01Z",
    }
    data = archive(
        12 if case in {"same", "wrong_merge"} else 858,
        "d" * 40 if case == "same" else "e" * 40,
        corrupt=case == "corrupt",
    )
    reads = []

    def api(args, **kwargs):
        endpoint = args[3]
        if "/actions/workflows/agents-verifier.yml/runs?" in endpoint:
            reads.append(endpoint)
            if "&status=" in endpoint:
                if case == "capacity":
                    raise native.RoundCapacityDeferred(
                        resource="core",
                        throttle={"action": "defer", "reset_in_s": 90},
                        observed_at=1792640000,
                    )
                if case == "deadline":
                    raise native.RoundDeadlineExhausted(deadline=1792640000, remaining_seconds=-1)
                return 0, {"total_count": 0, "workflow_runs": []}
            return 0, {"total_count": 2 if case == "history" else 1, "workflow_runs": [run]}
        if endpoint.endswith("/actions/runs/99/artifacts?per_page=100"):
            return 0, {
                "total_count": 1,
                "artifacts": [
                    {
                        "id": 7,
                        "name": "comparison-results-99",
                        "expired": False,
                        "size_in_bytes": len(data),
                        "digest": "sha256:" + hashlib.sha256(data).hexdigest(),
                        "workflow_run": {"id": 99, "head_sha": run["head_sha"]},
                    }
                ],
            }
        return transport(args, **kwargs)

    monkeypatch.setattr(native.gh_capacity, "gh_run", api)
    monkeypatch.setattr(native, "native_artifact_fetcher", lambda _: lambda *_a, **_k: data)
    assert native.main(w.argv) == 78
    result = json.loads(capsys.readouterr().out)
    assert len(w.merges) == 1 and w.events["provider"] == 0
    child = result["merge_verification_successors"]["owner/repo#12"]["native_result"]
    row = child["targets"]["owner/repo#12"]
    if case == "foreign":
        assert len(w.verifies) == 1
    else:
        assert not w.verifies and row["status"] == "held" and "error" not in row, row
        assert child["validation"]["scoped_completion_valid"] is True
        pending = row["results"]["act"]["pending_evidence"]
        assert pending["collection_requirements"] and pending["collection_cause"]
        if case in {"capacity", "deadline"}:
            assert pending["read_control"]["kind"] == (
                "capacity" if case == "capacity" else "request_deadline"
            )
    count = len(reads)
    assert native.main(w.argv) == 78
    capsys.readouterr()
    # The completed-merge helper performs a fresh read-only next-stage screen.
    # The unchanged native child remains terminal and repeats no effect.
    assert len(reads) >= count and len(w.verifies) == int(case == "foreign")
    assert all(p.read_bytes() == raw for p, raw in w.originals.items())


@pytest.mark.parametrize("event", ["workflow_dispatch", "pull_request_target"])
def test_unlinked_supported_native_event_can_prove_foreign(world, event):
    world.run["event"] = event
    result = world.backend.initiate_verify(TARGET, world.snapshot)
    assert result["status"] == "completed" and len(world.posts) == 1


@pytest.mark.parametrize("change", ["linked_current", "repo", "invalid_link"])
def test_contradictory_identity_never_authorizes_post(world, change):
    if change == "linked_current":
        world.run["pull_requests"] = [{"number": 12}]
    elif change == "repo":
        world.run["repository"]["full_name"] = "other/repo"
    else:
        world.run["pull_requests"] = [{"number": "12"}]
    result = world.backend.initiate_verify(TARGET, world.snapshot)
    assert result["status"] == "resource_wait" and not world.posts
    validate(result, world.snapshot)


@pytest.mark.parametrize("control", ["capacity", "deadline"])
def test_prewrite_round_control_retains_authoritative_scheduler_fields(world, control):
    if control == "capacity":
        world.fail = native.RoundCapacityDeferred(
            resource="core",
            throttle={"action": "defer", "reset_in_s": 93, "remaining": 0},
            observed_at=1792640000,
        )
    else:
        world.fail = native.RoundDeadlineExhausted(deadline=1792640000, remaining_seconds=-1)
    result = world.backend.initiate_verify(TARGET, world.snapshot)
    assert result["status"] == "resource_wait" and not world.posts
    op = validate(result, world.snapshot)
    proof = op["result"]["verifier_resource_wait"]
    if control == "capacity":
        assert proof["control"] == {
            "kind": "capacity",
            "resource": "core",
            "throttle": world.fail.throttle,
            "observed_at": 1792640000,
            "reset_at_epoch": 1792640093,
        }
        assert result["next_trigger"].endswith(":core:1792640093")
    else:
        assert proof["control"] == {
            "kind": "request_deadline",
            "deadline": 1792640000,
            "remaining_seconds": -1,
        }
        assert result["next_trigger"].endswith(":fresh-owned-request")
    changed = copy.deepcopy(op)
    p = changed["result"]["verifier_resource_wait"]
    p["control"]["kind"] = "generic_retry"
    p["sha256"] = boundary.digest({k: v for k, v in p.items() if k != "sha256"})
    assert queue.verifier_resource_wait_proof(changed, TARGET) is None


def test_progress_trigger_ignores_transport_urls_and_retained_ref_locations(tmp_path):
    first = {
        "total_count": 1,
        "artifacts": [
            {
                "id": 7,
                "name": "comparison-results-99",
                "digest": "sha256:" + "c" * 64,
                "expired": False,
                "size_in_bytes": 88,
                "workflow_run": {"id": 99},
                "archive_download_url": "https://old",
                "updated_at": "old",
            }
        ],
    }
    second = copy.deepcopy(first)
    second["artifacts"][0].update(archive_download_url="https://new", updated_at="new")
    r1 = boundary.observation("inventory", first, root=tmp_path / "first")
    r2 = boundary.observation("inventory", second, root=tmp_path / "second")
    snapshot = {"target": TARGET, "revision": "r", "head": HEAD, "merge": MERGE}
    p1 = boundary.pending(snapshot, TARGET, "attribution_pending", [r1], ["exact capture"])
    p2 = boundary.pending(snapshot, TARGET, "attribution_pending", [r2], ["exact capture"])
    assert r1["raw"] != r2["raw"] and r1["sha256"] == r2["sha256"]
    assert p1["next_trigger"] == p2["next_trigger"]
    second["artifacts"][0]["digest"] = "sha256:" + "d" * 64
    r3 = boundary.observation("inventory", second)
    assert (
        boundary.pending(snapshot, TARGET, "attribution_pending", [r3], ["exact capture"])[
            "next_trigger"
        ]
        != p1["next_trigger"]
    )


@pytest.mark.parametrize("kind", ["oversized", "symlink"])
def test_unsafe_retained_archive_is_not_read_or_refetched(world, kind, tmp_path):
    root = world.backend.state_path
    root = Path(str(root) + ".evidence") / "verifier-attribution"
    sha = hashlib.sha256(world.data).hexdigest()
    cached = root / "raw" / sha[:2] / (sha + ".zip")
    cached.parent.mkdir(parents=True)
    if kind == "oversized":
        with cached.open("wb") as stream:
            stream.truncate(boundary.evidence.MAX_ARCHIVE_BYTES + 1)
    else:
        outside = tmp_path / "outside.zip"
        outside.write_bytes(world.data)
        cached.symlink_to(outside)
    result = world.backend.initiate_verify(TARGET, world.snapshot)
    assert result["status"] == "resource_wait" and not world.posts and not world.downloads
    assert result["verifier_resource_wait"]["requirements"] == [
        "bounded regular retained archive for run 99"
    ]
    validate(result, world.snapshot)


@pytest.mark.parametrize("kind", ["missing_optional_file", "manifest_only"])
def test_authenticated_identity_does_not_require_optional_capture_completeness(world, kind):
    world.data = archive(
        omit=kind == "missing_optional_file", manifest_only=kind == "manifest_only"
    )
    result = world.backend.initiate_verify(TARGET, world.snapshot)
    assert result["status"] == "completed" and len(world.posts) == 1


@pytest.mark.parametrize("kind", ["duplicate_key", "duplicate_manifest"])
def test_ambiguous_manifest_identity_never_authorizes_foreign_skip(world, kind):
    world.data = archive(**{kind: True})
    result = world.backend.initiate_verify(TARGET, world.snapshot)
    assert result["status"] == "resource_wait" and not world.posts
    validate(result, world.snapshot)


def test_server_expiry_does_not_discard_verified_retained_identity(world):
    first = world.backend.initiate_verify(TARGET, world.snapshot)
    assert first["status"] == "completed" and len(world.downloads) == 1
    world.expired = True
    world.current["labels"] = []
    second = world.backend.initiate_verify(TARGET, world.snapshot)
    assert second["status"] == "completed" and len(world.downloads) == 1 and len(world.posts) == 2


def test_expired_uncached_capture_is_pending_without_redownload(world):
    world.expired = True
    result = world.backend.initiate_verify(TARGET, world.snapshot)
    assert result["status"] == "resource_wait" and not world.posts and not world.downloads
    validate(result, world.snapshot)


@pytest.mark.parametrize("completed_merge", [False, True])
@pytest.mark.parametrize("linked", [False, True])
def test_public_cli_unlabelled_same_manual_delivery_collects_sealed_diagnostic_packet(
    public_merge_world, monkeypatch, capsys, completed_merge, linked
):
    import closer_evidence as evidence
    import closer_frontier_policy as policy
    import closer_pipeline as pipeline
    import closer_resources as resources

    w = public_merge_world
    if not completed_merge:
        w.snapshot.update(
            state="MERGED", merge="d" * 40, merged_at="2026-10-22T00:00:00Z", verifier=None
        )
    run = {
        "id": 99,
        "run_attempt": 1,
        "head_sha": "e" * 40,
        "status": "completed",
        "conclusion": "success",
        "path": ".github/workflows/agents-verifier.yml",
        "event": "workflow_dispatch",
        "pull_requests": [],
        "repository": {"full_name": "owner/repo"},
        "created_at": "2026-10-22T00:00:01Z",
    }
    if linked:
        run["event"] = "pull_request_target"
        run["pull_requests"] = [
            {
                "number": 12,
                "id": 12345,
                "url": "https://api.github.com/repos/owner/repo/pulls/12",
                "head": {"sha": "a" * 40, "ref": "feature"},
                "base": {"sha": "b" * 40, "ref": "main"},
            }
        ]
    data = archive(12, "d" * 40)
    downloads = []
    reads = []
    transport = native.gh_capacity.gh_run

    def api(args, **kwargs):
        endpoint = args[3]
        if "/actions/workflows/agents-verifier.yml/runs?" in endpoint:
            reads.append(endpoint)
            return 0, (
                {"total_count": 0, "workflow_runs": []}
                if "&status=" in endpoint
                else {"total_count": 1, "workflow_runs": [run]}
            )
        if endpoint.endswith("/actions/runs/99"):
            return 0, copy.deepcopy(run)
        if endpoint.endswith("/actions/runs/99/artifacts?per_page=100"):
            return 0, {
                "total_count": 1,
                "artifacts": [
                    {
                        "id": 7,
                        "name": "comparison-results-99",
                        "expired": False,
                        "size_in_bytes": len(data),
                        "digest": "sha256:" + hashlib.sha256(data).hexdigest(),
                        "workflow_run": {"id": 99, "head_sha": run["head_sha"]},
                    }
                ],
            }
        return transport(args, **kwargs)

    def fetch(*_a, **_k):
        downloads.append(1)
        return data

    monkeypatch.setattr(native.gh_capacity, "gh_run", api)
    monkeypatch.setattr(native, "native_artifact_fetcher", lambda _: fetch)
    observer = native.Closer(
        native.GitHub(deadline=time.time() + 25), state_path=w.raw, apply=True, workers=True
    )
    if completed_merge:
        assert native.main(w.argv) == 78
        merged = json.loads(capsys.readouterr().out)
        next_stage = merged["merge_verification_successors"][TARGET]
        assert next_stage["status"] == "observed" and next_stage["action"] == "collect_evidence"
        assert len(w.merges) == 1 and not w.verifies
    initial = observer.observe(TARGET)
    assert initial["action"] == "collect_evidence" and initial["eligible"] is True
    assert initial["verifier"] is None and initial["labels"] == []
    if completed_merge:
        import closer_stage_continuation as continuation

        shard = resources.shard_dir(resources.scoped_root(w.ledger), TARGET) / "state.json"
        assignment = Path(w.argv[w.argv.index("--assignment") + 1])
        initial["observed_at"] = time.time()
        canonical = native.assignment_rows_from_file(assignment)[TARGET]
        child = continuation.create_successor(
            shard,
            shard.parent / "state.actions" / (pipeline._digest(TARGET) + ".json"),
            predecessor=w.frontier,
            target=TARGET,
            observation=initial,
            canonical_path=assignment,
            canonical_row=canonical,
            admission=lambda: {"paused": False, "dependency": None, "live_owner": False},
        )
        w.frontier = child["finite_frontier"]
    else:
        w.frontier = policy.make_frontier_from_observations(
            {TARGET: initial}, owner=w.frontier["owner"], frontier_id=w.frontier["frontier_id"]
        )
    w.argv[w.argv.index("--finite-frontier-json") + 1] = json.dumps(w.frontier)
    assert native.main(w.argv) == 78
    result = json.loads(capsys.readouterr().out)
    row = result["targets"][TARGET]
    assert (
        len(w.merges) == int(completed_merge) and not w.verifies and len(downloads) == 1
    ), downloads
    assert w.events["provider"] >= 1, row
    shard = resources.shard_dir(resources.scoped_root(w.ledger), TARGET) / "state.json"
    stored = pipeline._load(shard)["frontiers"][w.frontier["frontier_id"]]["items"][TARGET]
    prep = stored["results"]["prepare"]
    actual_screen = prep["collection_result"]["readiness"]
    assert actual_screen["mode"] == "native_diagnostic" and actual_screen["ready"] is False
    assert actual_screen["evaluation_ready"] is True
    assert actual_screen["gaps"] == ["native_merge_verifier:trusted_bound_verdict_missing"]
    assert (
        next(r for r in actual_screen["requirements"] if r["section"] == "native_merge_verifier")[
            "necessity"
        ]
        == "useful"
    )
    packet = json.loads(Path(prep["packet_ref"]["path"]).read_text())
    assert evidence.validate_packet(packet)
    section = packet["sections"]["native_merge_verifier"]
    assert (
        section["content"]["acceptance_authority"] is False
        and section["content"]["resolution_authority"] is False
    )
    assert section["content"]["artifacts"][0]["identity_bound"] is True, section
    receipts = section["content"]["artifacts"][0]["input_snapshots"][0]["receipts"]
    assert {r["path"] for r in receipts} >= {
        "comparison.json",
        "verifier-context.md",
        "verifier-pr-diff.patch",
    }
    # An intentionally rootless screen cannot use retained implementation references.
    screen = evidence.action_evidence_readiness(w.snapshot, packet, "collect_evidence")
    assert screen["mode"] == "native_diagnostic" and screen["ready"] is False
    assert "native_merge_verifier:trusted_bound_verdict_missing" in screen["gaps"]
    assert section["content"]["runs"][0]["head_sha"] == "e" * 40
    assert stored["results"]["interpret"]["authority"] is False
    count = len(reads)
    assert native.main(w.argv) == 78
    capsys.readouterr()
    assert len(reads) == count and len(downloads) == 1
    assert all(p.read_bytes() == raw for p, raw in w.originals.items())


def test_preparation_supplied_adapter_governs_attribution_inventory(world):
    world.data = archive(12, MERGE)
    supplied = native.GitHub(deadline=time.time() + 10)
    supplied.api = world.backend.gh.api

    def forbidden(*_a, **_k):
        pytest.fail("preflight bypassed supplied evidence transport")

    world.backend.gh.api = forbidden
    result = world.backend._verification_preflight(TARGET, world.snapshot, gh=supplied)
    assert result["native_diagnostic_reference"]["run"]["id"] == 99
    assert any("/artifacts?" in endpoint for endpoint in world.reads)


@pytest.mark.parametrize(
    "change",
    [
        "foreign",
        "different_attempt",
        "different_head",
        "wrong_subject",
        "wrong_digest",
        "association",
    ],
)
def test_normal_diagnostic_collector_revalidates_reference_not_boolean_permission(
    world, tmp_path, change
):
    import closer_evidence as evidence

    world.data = archive(12, MERGE)
    observed = world.backend.verification_snapshot(world.snapshot)
    reference = observed["native_diagnostic_reference"]
    if change == "foreign":
        world.data = archive(858)
    elif change == "different_attempt":
        world.run["run_attempt"] = 2
    elif change == "different_head":
        world.run["head_sha"] = "f" * 40
    elif change == "wrong_subject":
        reference["subject_pr"] = "owner/repo#999"
    elif change == "association":
        world.run["pull_requests"] = [{"number": 858}]
    else:
        reference["archive_digest"] = "0" * 64
    section = evidence._native_verifier_section(
        observed,
        world.backend.gh.api,
        time.monotonic() + 10,
        tmp_path / "packet",
        lambda *_a, **_k: world.data,
        native_archive_root=world.backend.verifier_archive_root,
    )
    assert section["status"] == "unknown"
    assert not (section.get("content") or {}).get("artifacts")
    assert not world.posts
