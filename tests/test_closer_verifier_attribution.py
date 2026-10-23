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
    duplicate_manifest=False,
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
    monkeypatch.setenv("ORCH_STORAGE_STATE_DIR", str(tmp_path / "private-storage-ledger"))
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
        capture_family="comparison",
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
                        "name": f"{w.capture_family}-results-99",
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


def diagnose(world):
    """Optional historical attribution keeps strict integrity without granting effects.

    Admission no longer depends on this historical enrichment. Its existing
    archive/provenance regressions still exercise the production read boundary.
    """
    return world.backend._verification_preflight(
        world.snapshot["target"], world.snapshot, check_current=False
    ) or {"status": "completed"}


def test_later_foreign_manual_run_does_not_block_current_verification(world):
    result = diagnose(world)
    assert result["status"] == "completed" and not world.posts
    assert len(world.downloads) == 1
    # Retained immutable capture avoids downloading it again on another read.
    diagnose(world)
    assert len(world.downloads) == 1


@pytest.mark.parametrize(
    "pr,merge,cause",
    [(12, MERGE, "existing_delivery"), (12, "e" * 40, "delivery_revision_mismatch")],
)
def test_optional_diagnostics_bind_same_pr_and_reject_wrong_merge(world, pr, merge, cause):
    world.data = archive(pr, merge)
    result = diagnose(world)
    assert not world.posts and result["verifier_resource_wait"]["cause"] == cause
    validate(result, world.snapshot)


@pytest.mark.parametrize("change", ["corrupt", "missing", "incomplete", "read", "merge_time"])
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
    result = diagnose(world)
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
    result = diagnose(world)
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
    result = diagnose(world)
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
    if case not in {"capacity", "deadline"}:
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
    assert len(reads) >= count and len(w.verifies) == int(case not in {"capacity", "deadline"})
    assert all(p.read_bytes() == raw for p, raw in w.originals.items())


@pytest.mark.parametrize("event", ["workflow_dispatch", "pull_request_target"])
def test_unlinked_supported_native_event_can_prove_foreign(world, event):
    world.run["event"] = event
    result = diagnose(world)
    assert result["status"] == "completed" and not world.posts


@pytest.mark.parametrize("change", ["linked_current", "repo", "invalid_link"])
def test_contradictory_identity_never_authorizes_post(world, change):
    if change == "linked_current":
        world.run["pull_requests"] = [{"number": 12}]
    elif change == "repo":
        world.run["repository"]["full_name"] = "other/repo"
    else:
        world.run["pull_requests"] = [{"number": "12"}]
    result = diagnose(world)
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
    result = diagnose(world)
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


def test_progress_trigger_ignores_transport_urls_and_retained_ref_locations(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCH_STORAGE_STATE_DIR", str(tmp_path / "private-storage-ledger"))
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
    result = diagnose(world)
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
    result = diagnose(world)
    assert result["status"] == "completed" and not world.posts


@pytest.mark.parametrize("kind", ["duplicate_key", "duplicate_manifest"])
def test_ambiguous_manifest_identity_never_authorizes_foreign_skip(world, kind):
    world.data = archive(**{kind: True})
    result = diagnose(world)
    assert result["status"] == "resource_wait" and not world.posts
    validate(result, world.snapshot)


def test_server_expiry_does_not_discard_verified_retained_identity(world):
    first = diagnose(world)
    assert first["status"] == "completed" and len(world.downloads) == 1
    world.expired = True
    world.current["labels"] = []
    second = diagnose(world)
    assert second["status"] == "completed" and len(world.downloads) == 1 and not world.posts


def test_expired_uncached_capture_is_pending_without_redownload(world):
    world.expired = True
    result = diagnose(world)
    assert result["status"] == "resource_wait" and not world.posts and not world.downloads
    validate(result, world.snapshot)


@pytest.mark.parametrize("capture_family", ["comparison", "evaluation"])
@pytest.mark.parametrize("linked", [False, True])
def test_targeted_optional_capture_seals_diagnostic_packet_without_admission_dependency(
    world, tmp_path, linked, capture_family
):
    import closer_evidence as evidence

    world.data = (
        evaluation_archive(12, MERGE) if capture_family == "evaluation" else archive(12, MERGE)
    )
    world.capture_family = capture_family
    if linked:
        world.run["event"] = "pull_request_target"
        world.run["pull_requests"] = [{"number": 12}]
    world.snapshot.update(
        kind="pr", base="c" * 40, sources=[], review_revision="r", checks="SUCCESS", threads=[]
    )
    world.current = copy.deepcopy(world.snapshot)
    # Ordinary observation does not fetch any completed history or capture.
    assert world.backend.verification_snapshot(world.snapshot) is world.snapshot
    assert not world.reads and not world.downloads
    enriched = world.backend.verification_snapshot(world.snapshot, include_history=True)
    assert enriched["native_diagnostic_reference"]["run"]["id"] == 99
    packet = native.collect_native_evidence(
        enriched,
        world.backend.gh,
        tmp_path / "diagnostic",
        deadline=time.monotonic() + 10,
        verifier_archive_root=world.backend.verifier_archive_root,
    )
    assert evidence.validate_packet(packet)
    section = packet["sections"]["native_merge_verifier"]
    assert section["content"]["acceptance_authority"] is False
    assert section["content"]["resolution_authority"] is False
    assert section["content"]["artifacts"][0]["identity_bound"] is True
    assert len(world.downloads) == 1 and not [
        endpoint for endpoint in world.posts if endpoint != "graphql"
    ]
    # A valid optional diagnostic is neither a trusted verdict nor an action gate.
    assert world.backend.select(enriched)[0] == "post_merge_verify"


def test_preparation_supplied_adapter_governs_attribution_inventory(world):
    world.data = archive(12, MERGE)
    supplied = native.GitHub(deadline=time.time() + 10)
    supplied.api = world.backend.gh.api

    def forbidden(*_a, **_k):
        pytest.fail("preflight bypassed supplied evidence transport")

    world.backend.gh.api = forbidden
    result = world.backend._verification_preflight(
        TARGET, world.snapshot, gh=supplied, check_current=False
    )
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
    observed = world.backend.verification_snapshot(world.snapshot, include_history=True)
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


@pytest.fixture
def history_world(world, monkeypatch):
    w = world
    original = w.backend.gh.api
    w.runs = []
    w.archives = {}
    w.pages = []
    w.fail_page = None
    w.downloads = []
    w.on_fetch = None
    w.on_page = None

    def api(endpoint, **kwargs):
        if kwargs.get("method") == "POST":
            return original(endpoint, **kwargs)
        if "/actions/workflows/agents-verifier.yml/runs?" in endpoint:
            if "&status=" in endpoint:
                return {"total_count": 0, "workflow_runs": []}
            page = int(endpoint.rsplit("&page=", 1)[-1]) if "&page=" in endpoint else 1
            w.pages.append(page)
            if w.on_page is not None:
                w.on_page(page)
            if w.fail_page == page:
                raise native.RoundCapacityDeferred(
                    resource="core",
                    throttle={"action": "defer", "reset_in_s": 60},
                    observed_at=1792640000,
                )
            return {
                "total_count": len(w.runs),
                "workflow_runs": w.runs[(page - 1) * 100 : page * 100],
            }
        if endpoint.endswith("/artifacts?per_page=100"):
            rid = int(endpoint.split("/runs/")[1].split("/")[0])
            data = w.archives[rid]
            run = next(r for r in w.runs if r["id"] == rid)
            return {
                "total_count": 1,
                "artifacts": [
                    {
                        "id": rid * 10,
                        "name": f"{w.capture_family}-results-{rid}",
                        "expired": False,
                        "digest": "sha256:" + hashlib.sha256(data).hexdigest(),
                        "size_in_bytes": len(data),
                        "workflow_run": {"id": rid, "head_sha": run["head_sha"]},
                    }
                ],
            }
        return original(endpoint, **kwargs)

    def fetch(a, **_k):
        rid = a["workflow_run"]["id"]
        w.downloads.append(rid)
        if w.on_fetch is not None:
            w.on_fetch(rid)
        return w.archives[rid]

    w.backend.gh.api = api
    monkeypatch.setattr(native, "native_artifact_fetcher", lambda _: fetch)

    def populate(count, *, linked=False):
        for i in range(count):
            run = copy.deepcopy(w.run)
            run["id"] = 1000 + i
            if linked:
                run.update(event="pull_request_target", pull_requests=[{"number": 858}])
            w.runs.append(run)
            w.archives[run["id"]] = archive(runid=run["id"])

    w.populate = populate
    return w


def test_nine_cold_foreign_runs_progress_eight_then_one_without_permanent_window(history_world):
    w = history_world
    w.populate(9)
    first = diagnose(w)
    assert first["status"] == "resource_wait" and len(w.downloads) == 8 and not w.posts
    assert first["verifier_resource_wait"]["requirements"] == [
        "continue verifier attribution after retained acquisition progress"
    ]
    validate(first, w.snapshot)
    second = diagnose(w)
    assert second["status"] == "completed" and len(w.downloads) == 9 and not w.posts
    assert len(set(w.downloads)) == 9


def test_history_pagination_finds_same_delivery_after_first_hundred_foreign_runs(history_world):
    w = history_world
    w.populate(125, linked=True)
    w.runs[-1].update(event="workflow_dispatch", pull_requests=[])
    rid = w.runs[-1]["id"]
    w.archives[rid] = archive(12, MERGE, runid=rid)
    result = diagnose(w)
    assert result["verifier_resource_wait"]["cause"] == "existing_delivery"
    assert w.pages == [1, 2] and w.downloads == [rid] and not w.posts


@pytest.mark.parametrize("change", ["attempt", "digest"])
def test_retained_foreign_archive_never_hides_changed_server_identity(history_world, change):
    w = history_world
    w.populate(9)
    assert diagnose(w)["status"] == "resource_wait"
    run = w.runs[0]
    if change == "attempt":
        run["run_attempt"] = 2
    else:
        w.archives[run["id"]] = archive(12, MERGE, runid=run["id"])
    result = diagnose(w)
    assert result["status"] == "resource_wait" and not w.posts
    assert result["verifier_resource_wait"]["cause"] == (
        "attribution_pending" if change == "attempt" else "existing_delivery"
    )


def test_history_page_capacity_preserves_reset_and_history_progress_without_acquisition(
    history_world,
):
    w = history_world
    w.populate(101, linked=True)
    # Page-one coverage is retained before the page-two refusal; no archive starts.
    w.runs[0].update(event="workflow_dispatch", pull_requests=[])
    w.fail_page = 2
    result = diagnose(w)
    assert result["verifier_resource_wait"]["cause"] == "capacity_deferred" and not w.posts
    assert not w.downloads
    assert result["verifier_resource_wait"]["control"]["reset_at_epoch"] == 1792640060
    assert any(
        r["endpoint"].endswith("runs?per_page=100")
        for r in result["verifier_resource_wait"]["observations"]
    )
    validate(result, w.snapshot)


def test_enrichment_omits_duplicate_core_snapshot_and_retains_no_poll_json(world):
    world.run["created_at"] = "2026-10-20T00:00:00Z"
    snapshots = []

    def snapshot(_):
        snapshots.append(1)
        return copy.deepcopy(world.current)

    world.backend.gh.snapshot = snapshot
    before = set(world.backend.verifier_archive_root.rglob("*"))
    for _ in range(3):
        assert (
            world.backend.verification_snapshot(world.snapshot, include_history=True)
            == world.snapshot
        )
    assert not snapshots
    assert set(world.backend.verifier_archive_root.rglob("*")) == before
    world.backend.initiate_verify(TARGET, world.snapshot)
    assert len(snapshots) == 1 and len(world.posts) == 1


def test_history_pages_finish_before_archive_fetch_can_shift_busy_run_inventory(history_world):
    w = history_world
    w.populate(101, linked=True)
    w.runs[0].update(event="workflow_dispatch", pull_requests=[])
    pages_at_fetch = []

    def concurrent_foreign_run(_rid):
        pages_at_fetch.append(list(w.pages))
        incoming = copy.deepcopy(w.runs[1])
        incoming.update(id=9000, created_at="2026-10-22T01:00:00Z")
        w.runs.insert(0, incoming)

    w.on_fetch = concurrent_foreign_run
    result = diagnose(w)
    assert result["status"] == "completed" and not w.posts, result
    assert pages_at_fetch == [[1, 2]]
    assert w.pages == [1, 2] and len(w.downloads) == 1


def test_history_change_during_page_reads_remains_incomplete_and_starts_no_archive(history_world):
    w = history_world
    w.populate(101, linked=True)

    def changed_page(page):
        if page == 2:
            incoming = copy.deepcopy(w.runs[0])
            incoming.update(id=9000, created_at="2026-10-22T01:00:00Z")
            w.runs.insert(0, incoming)

    w.on_page = changed_page
    result = diagnose(w)
    assert result["verifier_resource_wait"]["cause"] == "history_incomplete"
    assert w.pages == [1, 2] and not w.downloads and not w.posts
    validate(result, w.snapshot)


def evaluation_archive(pr=867, merge="e" * 40, *, runid=99, omit=False, **identity):
    """Small production-shaped input capture; no retained operational payloads."""
    with zipfile.ZipFile(io.BytesIO(archive(pr, merge, runid=runid, omit=omit))) as source:
        files = {name: source.read(name) for name in source.namelist()}
    manifest = json.loads(files["verifier-input-manifest.json"])
    manifest.update(mode="evaluate", **identity)
    files["verifier-input-manifest.json"] = json.dumps(manifest).encode()
    files.pop("comparison.json")
    files["evaluation.json"] = json.dumps(
        {
            "verdict": "PASS",
            "summary": "Single-provider untrusted diagnostic",
            "concerns": ["One retained diagnostic concern"],
            "provider_used": "anthropic/example",
            "model": "example-model",
            "used_llm": True,
            "error": None,
        }
    ).encode()
    files["evaluation-execution.json"] = json.dumps(
        {
            "schema": "workflows-verifier-evaluation-execution/v1",
            "mode": "evaluate",
            "status": "completed",
            "decision_available": True,
            "acceptance_authority": False,
        }
    ).encode()
    files["evaluation-comment.md"] = b"One-provider retained diagnostic; no acceptance authority."
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as capture:
        for name, data in files.items():
            capture.writestr(name, data)
    return output.getvalue()


def test_foreign_evaluation_capture_does_not_become_target_acceptance(world):
    world.capture_family = "evaluation"
    world.data = evaluation_archive()
    target = "owner/repo#656"
    world.snapshot["target"] = target
    world.current["target"] = target
    relation, requirement = boundary.attribute(
        world.run,
        world.snapshot,
        world.backend.gh.api,
        native.native_artifact_fetcher(world.snapshot),
        world.backend.verifier_archive_root,
        deadline=time.monotonic() + 30,
        reads=[],
    )
    assert (relation, requirement) == ("foreign", None)
    assert not world.posts
    result = world.backend.initiate_verify(target, world.snapshot)
    assert result["status"] == "completed"
    assert len(world.posts) == 1 and world.posts == ["repos/owner/repo/issues/656/labels"]
    assert "verdict" not in result and "native_diagnostic_reference" not in result
    assert world.snapshot.get("verifier") is None
    assert len(world.downloads) == 1


@pytest.mark.parametrize(
    "merge,cause", [(MERGE, "existing_delivery"), ("e" * 40, "delivery_revision_mismatch")]
)
def test_evaluation_same_delivery_and_wrong_merge_never_dispatch(world, merge, cause):
    world.capture_family = "evaluation"
    world.data = evaluation_archive(12, merge)
    result = diagnose(world)
    assert result["status"] == "resource_wait" and not world.posts
    assert result["verifier_resource_wait"]["cause"] == cause
    assert len(world.downloads) == 1
    validate(result, world.snapshot)


@pytest.mark.parametrize("second_family", ["comparison", "evaluation"])
def test_dual_or_duplicate_capture_families_remain_ambiguous(world, second_family):
    world.capture_family = "evaluation"
    world.data = evaluation_archive()
    api = world.backend.gh.api

    def ambiguous(endpoint, **kwargs):
        response = api(endpoint, **kwargs)
        if endpoint.endswith("/artifacts?per_page=100"):
            extra = dict(response["artifacts"][0], id=8, name=f"{second_family}-results-99")
            response["artifacts"].append(extra)
            response["total_count"] = 2
        return response

    world.backend.gh.api = ambiguous
    result = diagnose(world)
    assert result["status"] == "resource_wait" and not world.posts and not world.downloads
    assert "unambiguous" in result["verifier_resource_wait"]["requirements"][0]
    validate(result, world.snapshot)


@pytest.mark.parametrize(
    "identity",
    [
        {"repository": "other/repo"},
        {"source_run_id": "100"},
        {"source_run_attempt": 2},
        {"pr": None},
        {"merge_sha": "not-a-merge"},
        {"context_sha256": "0" * 64},
    ],
)
def test_evaluation_bad_core_identity_never_authorizes_foreign_skip(world, identity):
    world.capture_family = "evaluation"
    world.data = evaluation_archive(**identity)
    result = diagnose(world)
    assert result["status"] == "resource_wait" and not world.posts
    assert result["verifier_resource_wait"]["cause"] == "attribution_pending"
    assert len(world.downloads) == 1
    validate(result, world.snapshot)


def test_evaluation_optional_capture_omission_does_not_prevent_identity(world):
    world.capture_family = "evaluation"
    world.data = evaluation_archive(omit=True)
    result = diagnose(world)
    assert result["status"] == "completed" and not world.posts
    assert world.snapshot.get("verifier") is None


def test_evaluation_expired_capture_requires_retained_verified_bytes(world):
    world.capture_family = "evaluation"
    world.data = evaluation_archive()
    world.expired = True
    first = diagnose(world)
    assert first["status"] == "resource_wait" and not world.posts and not world.downloads
    world.expired = False
    assert diagnose(world)["status"] == "completed"
    assert len(world.downloads) == 1
    world.expired = True
    world.current["labels"] = []
    assert diagnose(world)["status"] == "completed"
    assert len(world.downloads) == 1


def test_evaluation_archive_digest_mismatch_never_authorizes_foreign_skip(world):
    world.capture_family = "evaluation"
    world.data = evaluation_archive()
    api = world.backend.gh.api

    def mismatch(endpoint, **kwargs):
        response = api(endpoint, **kwargs)
        if endpoint.endswith("/artifacts?per_page=100"):
            response["artifacts"][0]["digest"] = "sha256:" + "0" * 64
        return response

    world.backend.gh.api = mismatch
    result = diagnose(world)
    assert result["status"] == "resource_wait" and not world.posts
    assert result["verifier_resource_wait"]["requirements"] == [
        "archive digest verification for run 99"
    ]
    validate(result, world.snapshot)


@pytest.mark.parametrize("field", ["id", "head_sha"])
def test_evaluation_archive_requires_exact_server_run_owner(world, field):
    world.capture_family = "evaluation"
    world.data = evaluation_archive()
    api = world.backend.gh.api

    def mismatch(endpoint, **kwargs):
        response = api(endpoint, **kwargs)
        if endpoint.endswith("/artifacts?per_page=100"):
            response["artifacts"][0]["workflow_run"][field] = 100 if field == "id" else "f" * 40
        return response

    world.backend.gh.api = mismatch
    result = diagnose(world)
    assert result["status"] == "resource_wait" and not world.posts and not world.downloads
    assert result["verifier_resource_wait"]["requirements"] == [
        "exact server-bound verifier archive for run 99"
    ]
    validate(result, world.snapshot)


def test_evaluation_capture_reuses_progress_under_existing_acquisition_budget(history_world):
    world = history_world
    world.capture_family = "evaluation"
    world.populate(9)
    for run in world.runs:
        world.archives[run["id"]] = evaluation_archive(runid=run["id"])
    first = diagnose(world)
    assert first["status"] == "resource_wait" and len(world.downloads) == 8 and not world.posts
    assert first["verifier_resource_wait"]["requirements"] == [
        "continue verifier attribution after retained acquisition progress"
    ]
    second = diagnose(world)
    assert second["status"] == "completed" and len(world.downloads) == 9
    assert len(set(world.downloads)) == 9 and not world.posts
