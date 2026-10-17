import io
import json
import zipfile
from pathlib import Path

import pytest

import closer_evidence as evidence

HEAD = "a" * 40
BASE = "b" * 40
TARGET = "acme/repo#12"


def snapshot(**overrides):
    data = {
        "target": TARGET,
        "kind": "pr",
        "head": HEAD,
        "base": BASE,
        "sources": [
            {
                "target": "acme/repo#3",
                "body": "## Tasks\n- [ ] Ship it",
                "state": "open",
                "labels": [],
            }
        ],
        "threads": [
            {
                "id": "thread-1",
                "isResolved": False,
                "isOutdated": False,
                "comments": {
                    "nodes": [
                        {"author": {"login": "reviewer"}, "body": "Please fix", "createdAt": "now"}
                    ]
                },
            }
        ],
        "review_revision": "r1",
        "checks": None,
    }
    data.update(overrides)
    return data


def base_api(endpoint, **kwargs):
    if "/compare/" in endpoint:
        return {
            "status": "ahead",
            "ahead_by": 1,
            "files": [
                {
                    "filename": "src/a.py",
                    "status": "modified",
                    "additions": 2,
                    "deletions": 1,
                    "changes": 3,
                    "sha": "c" * 40,
                    "patch": "@@ -1 +1 @@\n-old\n+new",
                }
            ],
        }
    if "/actions/runs?" in endpoint:
        return {"workflow_runs": []}
    raise AssertionError(endpoint)


def collect(tmp_path, snap=None, api=base_api, **kwargs):
    return evidence.collect_evidence(snap or snapshot(), api, tmp_path, **kwargs)


def test_pending_ci_has_first_admission_packet_and_schema(tmp_path):
    packet = collect(tmp_path)
    assert evidence.validate_packet(packet)
    assert packet["schema"] == "closer-evidence/v1"
    assert packet["sections"]["checks"]["status"] == "pending"
    assert packet["sections"]["source"]["content"][0]["requirements"] == ["Ship it"]
    file_row = packet["sections"]["diff_files"]["content"]["files"][0]
    assert file_row["patch"]
    assert evidence.validate_patch_reference(file_row, base=BASE, head=HEAD)
    assert Path(file_row["patch_retained"]["path"]).read_bytes() == file_row["patch"].encode()
    altered_ref = dict(file_row, patch_retained={**file_row["patch_retained"], "sha256": "0" * 64})
    assert not evidence.validate_patch_reference(altered_ref, base=BASE, head=HEAD)
    altered_binding = dict(file_row, patch_binding={**file_row["patch_binding"], "head": "c" * 40})
    assert not evidence.validate_patch_reference(altered_binding, base=BASE, head=HEAD)
    assert packet["sections"]["review_conversations"]["status"] == "truncated"
    assert packet["sections"]["ci_artifacts"]["status"] == "missing"
    assert Path(packet["manifest"]["section_files"]["source"]["path"]).exists()


def test_source_body_truncation_and_credential_flag_are_explicit(tmp_path):
    body = "## Tasks\n" + "x" * (evidence.MAX_TEXT_BYTES + 100)
    packet = collect(tmp_path, snapshot(sources=[{"target": "acme/repo#3", "body": body}]))
    assert packet["sections"]["source"]["status"] == "truncated"
    assert packet["sections"]["source"]["content"][0]["truncated"] is True
    ordinary = collect(
        tmp_path / "ordinary",
        snapshot(
            sources=[
                {"target": "acme/repo#3", "body": "## Acceptance Criteria\n- api_key must be set"}
            ]
        ),
    )
    assert ordinary["sections"]["source"]["content"][0]["body"].endswith("api_key must be set")
    assert ordinary["sections"]["source"]["content"][0]["credential_pattern_present"] is False
    secret = collect(
        tmp_path / "secret",
        snapshot(sources=[{"target": "acme/repo#3", "body": "token=supersecretvalue"}]),
    )
    row = secret["sections"]["source"]["content"][0]
    assert row["redaction_applied"] is True and "supersecretvalue" not in row["body"]
    assert "supersecretvalue" not in Path(row["body_retained"]["path"]).read_text()
    secret_snapshot = snapshot(
        sources=[{"target": "acme/repo#3", "body": "token=supersecretvalue"}]
    )
    refreshed_secret = collect(tmp_path / "secret", secret_snapshot, previous=secret)
    assert refreshed_secret["sections"]["source"] is secret["sections"]["source"]

    reviewed = collect(
        tmp_path / "review-secret",
        snapshot(
            threads=[
                {
                    "id": "thread-secret",
                    "comments": {
                        "nodes": [
                            {"author": {"login": "reviewer"}, "body": "token=reviewsecretvalue"}
                        ]
                    },
                }
            ]
        ),
        review_threads_fetch=lambda snap, **_kw: {
            "threads": snap["threads"],
            "complete": True,
            "has_next_page": False,
            "end_cursor": None,
        },
    )
    review = reviewed["sections"]["review_conversations"]
    serialized = json.dumps(review)
    assert "reviewsecretvalue" not in serialized
    assert review["content"][0]["comments"][0]["redaction_applied"] is True
    assert review["status"] == "partial"


def test_current_head_ci_runs_collected_before_verifier_and_progress_refreshes(tmp_path):
    runs_state = {"conclusion": None}

    def api(endpoint, **kwargs):
        if "/compare/" in endpoint:
            return base_api(endpoint)
        if "/actions/runs?" in endpoint:
            return {"workflow_runs": [{"id": 7, "head_sha": HEAD}]}
        if endpoint.endswith("/runs/7"):
            return {
                "id": 7,
                "run_attempt": 1,
                "head_sha": HEAD,
                "pull_requests": [{"number": 12}],
                "status": "completed" if runs_state["conclusion"] else "in_progress",
                "conclusion": runs_state["conclusion"],
                "event": "pull_request",
            }
        if endpoint.endswith("/runs/7/artifacts?per_page=100"):
            return {"artifacts": []}
        raise AssertionError(endpoint)

    first = collect(tmp_path, api=api)
    assert first["sections"]["ci_artifacts"]["status"] == "partial"
    assert first["sections"]["ci_artifacts"]["content"]["runs"][0]["status"] == "in_progress"
    runs_state["conclusion"] = "success"
    second = collect(tmp_path, api=api, previous=first)
    assert second["sections"]["ci_artifacts"]["status"] == "missing"
    assert second["sections"]["ci_artifacts"]["content"]["runs"][0]["conclusion"] == "success"


def test_ci_run_and_artifact_identity_provenance_and_junit_raw_retention(tmp_path):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("junit.xml", '<testsuite tests="1"><testcase name="one"/></testsuite>')
        archive.writestr("arbitrary.json", '{"hello":"world"}')

    def api(endpoint, **kwargs):
        if "/compare/" in endpoint:
            return base_api(endpoint)
        if "/actions/runs?" in endpoint:
            return {"workflow_runs": [{"id": 23, "head_sha": HEAD}]}
        if endpoint.endswith("/runs/23"):
            return {
                "id": 23,
                "run_attempt": 2,
                "head_sha": HEAD,
                "pull_requests": [{"number": 12}],
                "status": "completed",
                "conclusion": "success",
                "event": "pull_request",
            }
        if endpoint.endswith("/runs/23/artifacts?per_page=100"):
            return {
                "artifacts": [
                    {"id": 2, "name": "test-output", "size_in_bytes": 100, "expired": False},
                    {"id": 3, "name": "coverage-output", "size_in_bytes": 50, "expired": True},
                ]
            }
        raise AssertionError(endpoint)

    packet = collect(tmp_path, api=api, artifact_fetch=lambda artifact, **kw: stream.getvalue())
    ci = packet["sections"]["ci_artifacts"]
    assert ci["status"] == "partial"
    assert ci["content"]["retrieval_status"] == "partial"
    assert ci["content"]["proof_status"] == "partial"
    assert len(ci["content"]["unavailable"]) == 2
    assert any(item.get("expired") for item in ci["content"]["unavailable"])
    artifact = ci["content"]["artifacts"][0]
    assert (
        artifact["run_id"] == 23 and artifact["run_attempt"] == 2 and artifact["head_sha"] == HEAD
    )
    assert Path(artifact["archive"]["path"]).exists()
    tc = packet["sections"]["test_coverage_artifacts"]
    assert tc["status"] == "partial"
    assert tc["content"]["proof_status"] == "partial"
    assert tc["content"]["tests"][0]["tests"] == 1
    assert len(list((tmp_path / "raw").rglob("*.xml"))) == 1
    assert any("not a recognized" in x["reason"] for x in artifact["omissions"])


def test_unrelated_json_does_not_claim_tests_and_malicious_zip_is_omitted(tmp_path):
    assert evidence._recognized_json({"foo": 1}) is None
    for bad in (
        {"total": 0, "passed": 1, "failed": 0},
        {"total": True, "passed": 1, "failed": 0},
        {"total": 2, "passed": -1, "failed": 0},
        {"total": 1, "passed": 1, "failed": 1},
        {"total": 100, "passed": 1, "failed": 0},
        {"total": 2, "passed": 0, "failed": 0, "skipped": 2},
        {"total": 2, "passed": 1, "failed": float("inf"), "skipped": 0},
        {"totals": {"lines": {"count": 2, "covered": 3, "percent_covered": 150}}},
        {"totals": {"lines": {"count": 2, "covered": 1, "percent_covered": float("nan")}}},
    ):
        assert evidence._recognized_json(bad) is None
    assert evidence._recognized_json({"total": 2, "passed": 1, "failed": 0, "skipped": 1}) == {
        "kind": "test-summary",
        "total": 2,
        "passed": 1,
        "failed": 0,
        "skipped": 1,
    }
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("../escape.xml", "<testsuite><testcase/></testsuite>")
    receipts, omissions, status = evidence._parse_archive(stream.getvalue(), tmp_path)
    assert receipts == [] and omissions and status == "truncated"
    assert not (tmp_path.parent / "escape.xml").exists()


def test_incomplete_run_pagination_and_review_pagination_are_not_complete(tmp_path):
    def api(endpoint, **kwargs):
        if "/compare/" in endpoint:
            return base_api(endpoint)
        if "/actions/runs?" in endpoint:
            return {"workflow_runs": [{"id": i, "head_sha": HEAD} for i in range(50)]}
        if "/actions/runs/" in endpoint and "/artifacts" not in endpoint:
            rid = int(endpoint.rsplit("/", 1)[1])
            return {"id": rid, "head_sha": HEAD, "run_attempt": 1, "status": "in_progress"}
        if "/artifacts" in endpoint:
            return {"artifacts": []}
        raise AssertionError(endpoint)

    packet = collect(tmp_path, api=api)
    assert packet["sections"]["ci_artifacts"]["status"] in {"partial", "truncated"}
    assert packet["sections"]["review_conversations"]["status"] == "truncated"
    full = collect(
        tmp_path / "full",
        review_threads_fetch=lambda *a, **kw: {
            "threads": snapshot()["threads"],
            "complete": True,
            "has_next_page": False,
            "end_cursor": None,
        },
    )
    assert full["sections"]["review_conversations"]["status"] == "complete"


def test_incremental_refreshes_volatile_sections_and_invalidates_identity(tmp_path):
    first = collect(tmp_path)

    def api(endpoint, **kwargs):
        if "/compare/" in endpoint:
            return base_api(endpoint)
        if "/actions/runs?" in endpoint:
            return {"workflow_runs": [{"id": 9, "head_sha": HEAD}]}
        if endpoint.endswith("/runs/9"):
            return {
                "id": 9,
                "head_sha": HEAD,
                "pull_requests": [{"number": 12}],
                "run_attempt": 1,
                "status": "completed",
                "conclusion": "success",
            }
        if endpoint.endswith("/runs/9/artifacts?per_page=100"):
            return {"artifacts": []}
        raise AssertionError(endpoint)

    second = collect(tmp_path, api=api, previous=first)
    assert second["sections"]["ci_artifacts"]["status"] == "missing"
    assert second["sections"]["ci_artifacts"]["content"]["runs"][0]["id"] == 9
    assert second["sections"]["source"] is first["sections"]["source"]
    changed = collect(tmp_path, snapshot(head="c" * 40), previous=second)
    assert changed["sections"]["diff_files"]["provenance"]["head"] == "c" * 40


def test_incremental_cache_skips_diff_and_artifact_download_but_refreshes_inventory(tmp_path):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("junit.xml", "<testsuite><testcase/></testsuite>")
    calls = {"compare": 0, "artifact_list": 0, "download": 0}

    def api(endpoint, **kwargs):
        if "/compare/" in endpoint:
            calls["compare"] += 1
            return base_api(endpoint)
        if "/actions/runs?" in endpoint:
            return {"workflow_runs": [{"id": 32, "head_sha": HEAD}]}
        if endpoint.endswith("/runs/32"):
            return {
                "id": 32,
                "head_sha": HEAD,
                "pull_requests": [{"number": 12}],
                "run_attempt": 1,
                "status": "completed",
                "conclusion": "success",
                "event": "pull_request",
                "updated_at": "same",
            }
        if endpoint.endswith("/runs/32/artifacts?per_page=100"):
            calls["artifact_list"] += 1
            return {"artifacts": [{"id": 44, "name": "junit", "expired": False}]}
        raise AssertionError(endpoint)

    def fetch(artifact, **kwargs):
        calls["download"] += 1
        return stream.getvalue()

    first = collect(tmp_path, api=api, artifact_fetch=fetch)
    assert calls == {"compare": 1, "artifact_list": 1, "download": 1}
    second = collect(tmp_path, api=api, artifact_fetch=fetch, previous=first)
    assert calls == {"compare": 1, "artifact_list": 2, "download": 1}
    assert second["sections"]["diff_files"]["sha256"] == first["sections"]["diff_files"]["sha256"]
    assert second["sections"]["ci_artifacts"]["content"]["artifacts"][0]["receipts"][0]["raw"]

    # A new attempt is fresh inventory but not the old attempt's archived content.
    changed_attempt = dict(snapshot())

    def attempt_api(endpoint, **kwargs):
        if "/compare/" in endpoint:
            calls["compare"] += 1
            return base_api(endpoint)
        if "/actions/runs?" in endpoint:
            return {"workflow_runs": [{"id": 32, "head_sha": HEAD}]}
        if endpoint.endswith("/runs/32"):
            return {
                "id": 32,
                "head_sha": HEAD,
                "pull_requests": [{"number": 12}],
                "run_attempt": 2,
                "status": "completed",
                "conclusion": "success",
                "event": "pull_request",
                "updated_at": "new-attempt",
            }
        if endpoint.endswith("/runs/32/artifacts?per_page=100"):
            calls["artifact_list"] += 1
            return {"artifacts": [{"id": 45, "name": "junit-new", "expired": False}]}
        raise AssertionError(endpoint)

    def fetch_new(artifact, **kwargs):
        calls["download"] += 1
        return stream.getvalue()

    third = collect(
        tmp_path, changed_attempt, api=attempt_api, artifact_fetch=fetch_new, previous=second
    )
    assert calls["compare"] == 1  # same base/head diff reused
    assert calls["artifact_list"] == 3 and calls["download"] == 2
    assert third["sections"]["ci_artifacts"]["content"]["artifacts"][0]["run_attempt"] == 2


def test_invalid_cached_archive_is_refetched_and_changed_source_only_invalidates_source(tmp_path):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("junit.xml", "<testsuite><testcase/></testsuite>")
    calls = {"compare": 0, "list": 0, "fetch": 0}

    def api(endpoint, **kwargs):
        if "/compare/" in endpoint:
            calls["compare"] += 1
            return base_api(endpoint)
        if "/actions/runs?" in endpoint:
            return {"workflow_runs": [{"id": 51, "head_sha": HEAD}]}
        if endpoint.endswith("/runs/51"):
            return {
                "id": 51,
                "head_sha": HEAD,
                "pull_requests": [{"number": 12}],
                "run_attempt": 1,
                "status": "completed",
                "conclusion": "success",
                "event": "pull_request",
            }
        if endpoint.endswith("/runs/51/artifacts?per_page=100"):
            calls["list"] += 1
            return {"artifacts": [{"id": 52, "name": "junit", "expired": False}]}
        raise AssertionError(endpoint)

    def fetch(artifact, **kwargs):
        calls["fetch"] += 1
        return stream.getvalue()

    first = collect(tmp_path, api=api, artifact_fetch=fetch)
    cached = collect(tmp_path, api=api, artifact_fetch=fetch, previous=first)
    assert cached["sections"]["source"] is first["sections"]["source"]
    changed_source = snapshot(sources=[{"target": "acme/repo#3", "body": "## Tasks\n- New"}])
    changed = collect(tmp_path, changed_source, api=api, artifact_fetch=fetch, previous=cached)
    assert changed["sections"]["source"]["content"][0]["body"].endswith("New")
    assert calls == {"compare": 1, "list": 3, "fetch": 1}

    archive_ref = changed["sections"]["ci_artifacts"]["content"]["artifacts"][0]["archive"]
    Path(archive_ref["path"]).unlink()
    retried = collect(tmp_path, changed_source, api=api, artifact_fetch=fetch, previous=changed)
    assert calls["list"] == 4 and calls["fetch"] == 2
    assert retried["sections"]["ci_artifacts"]["content"]["artifacts"][0]["status"] == "complete"


def test_schema_validation_and_drift_sampling(tmp_path):
    packet = collect(tmp_path)
    sample = evidence.drift_sample(packet, recurring=True, count=4)
    assert len(sample) == 4 and all(x["authority"] is False for x in sample)
    assert all(x.get("expected_assertions") and x.get("section_refs") for x in sample)
    assert sample == evidence.drift_sample(packet, recurring=True, count=4)
    for mutation in ({"sections": {}}, {"producer_sha256": "bad"}, {"head": "not-a-sha"}):
        bad = dict(packet)
        bad.update(mutation)
        assert not evidence.validate_packet(bad)
    tampered = dict(packet)
    tampered["sections"] = dict(packet["sections"])
    tampered["sections"]["source"] = dict(packet["sections"]["source"], content="tampered")
    assert not evidence.validate_packet(tampered)
    with pytest.raises(ValueError):
        evidence.drift_sample(tampered)


def test_native_manual_nonpass_requires_exact_manifest_and_source_run(tmp_path):
    merge = "d" * 40
    marker = {
        "schema": "verifier-corpus-decision/v1",
        "repo": "acme/repo",
        "pr": "12",
        "head_sha": HEAD,
        "evaluated_sha": merge,
        "run_id": "900",
        "run_attempt": "2",
        "verdict": "NON_PASS",
        "provider_verdicts": ["FAIL"],
        "ci_failed": False,
    }
    manifest = {
        "schema": "workflows-verifier-input-snapshot/v1",
        "repository": "acme/repo",
        "pr": 12,
        "merge_sha": merge,
        "source_run_id": "900",
    }
    archive_stream = io.BytesIO()
    with zipfile.ZipFile(archive_stream, "w") as archive:
        archive.writestr("input-snapshot.json", json.dumps(manifest))
        archive.writestr("comparison.json", json.dumps({"findings": []}))
        archive.writestr("comparison-comment.md", "No findings")
        archive.writestr("verifier-context.md", "Bound verifier context")
        archive.writestr("verifier-pr-diff.patch", "diff --git a/a b/a\n")

    def api(endpoint, **kwargs):
        if "/compare/" in endpoint:
            return base_api(endpoint)
        if "/actions/runs?" in endpoint:
            return {"workflow_runs": []}
        if endpoint.endswith("/runs/900"):
            return {
                "id": 900,
                "run_attempt": 2,
                "event": "workflow_dispatch",
                "status": "completed",
                "conclusion": "success",
                "head_sha": "e" * 40,
                "repository": {"full_name": "acme/repo"},
            }
        if endpoint.endswith("/runs/900/artifacts?per_page=100"):
            return {"artifacts": [{"id": 9, "name": "verifier-input", "expired": False}]}
        raise AssertionError(endpoint)

    snap = snapshot(merge=merge, verifier=marker)
    packet = collect(
        tmp_path, snap, api=api, artifact_fetch=lambda _artifact, **_kw: archive_stream.getvalue()
    )
    native = packet["sections"]["native_merge_verifier"]
    assert native["status"] == "complete"
    assert native["content"]["evidence_type"] == "evaluated-merge-verifier-non-pass"
    assert native["content"]["proof_status"] == "resolution-only"
    assert native["content"]["input_manifest"]["raw"]["sha256"]
    assert native["provenance"]["acceptance_proof"] is False
    assert native["content"]["run"]["head_sha"] == "e" * 40

    wrong_source_stream = io.BytesIO()
    with zipfile.ZipFile(wrong_source_stream, "w") as archive:
        archive.writestr("input-snapshot.json", json.dumps({**manifest, "source_run_id": "700"}))
    wrong_source = collect(
        tmp_path / "source-run",
        snap,
        api=api,
        artifact_fetch=lambda _artifact, **_kw: wrong_source_stream.getvalue(),
    )
    assert wrong_source["sections"]["native_merge_verifier"]["status"] == "unknown"

    for index, wrong_marker in enumerate(
        ({**marker, "pr": "13"}, {**marker, "evaluated_sha": "f" * 40})
    ):
        bad = collect(
            tmp_path / f"wrong-{index}",
            snapshot(merge=merge, verifier=wrong_marker),
            api=api,
            artifact_fetch=lambda _artifact, **_kw: archive_stream.getvalue(),
        )
        assert bad["sections"]["native_merge_verifier"]["status"] == "missing"

    def wrong_attempt(endpoint, **kwargs):
        result = api(endpoint, **kwargs)
        if endpoint.endswith("/runs/900"):
            result["run_attempt"] = 3
        return result

    bad = collect(
        tmp_path / "attempt",
        snap,
        api=wrong_attempt,
        artifact_fetch=lambda _artifact, **_kw: archive_stream.getvalue(),
    )
    assert bad["sections"]["native_merge_verifier"]["status"] == "unknown"


def test_assigned_issue_alias_uses_delivery_pr_for_diff_and_ci_identity(tmp_path):
    requests = []

    def api(endpoint, **kwargs):
        requests.append(endpoint)
        if "/compare/" in endpoint:
            return {"status": "ahead", "files": []}
        if "/actions/runs?" in endpoint:
            return {"workflow_runs": [{"id": 6990, "head_sha": HEAD}]}
        if endpoint.endswith("/runs/6990"):
            return {
                "id": 6990,
                "head_sha": HEAD,
                "pull_requests": [{"number": 699}],
                "run_attempt": 1,
                "status": "completed",
                "conclusion": "success",
            }
        if endpoint.endswith("/runs/6990/artifacts?per_page=100"):
            return {"artifacts": []}
        raise AssertionError(endpoint)

    alias = snapshot(target="acme/repo#671", pr_target="acme/repo#699", kind="pr")
    packet = collect(tmp_path, alias, api=api)
    assert packet["target"] == "acme/repo#671"
    assert packet["subject_pr"] == "acme/repo#699"
    assert packet["sections"]["ci_artifacts"]["content"]["runs"][0]["id"] == 6990
    assert any(
        "pull/" not in call and "repos/acme/repo/actions/runs/6990" in call for call in requests
    )

    def wrong_pr_api(endpoint, **kwargs):
        result = api(endpoint, **kwargs)
        if endpoint.endswith("/runs/6990"):
            result["pull_requests"] = [{"number": 671}]
        return result

    rejected = collect(tmp_path / "wrong", alias, api=wrong_pr_api)
    assert rejected["sections"]["ci_artifacts"]["status"] == "missing"
