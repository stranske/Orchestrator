import hashlib
import io
import json
import tempfile
import time
import zipfile
from pathlib import Path

import closer_dispatcher as dispatcher
import closer_disposition as d
import closer_evidence as e

TARGET = "owner/repo#12"
HEAD = "a" * 40
BASE = "b" * 40
BODY = "## Tasks\n- [ ] Ship the report"
ABSENT = object()


def fixture(
    tmp_path,
    *,
    dispositions=None,
    native=False,
    native_report=True,
    native_manifest_pr=12,
    pr_target=ABSENT,
):
    snap = {
        "target": TARGET,
        "kind": "pr",
        "head": HEAD,
        "base": BASE,
        "revision": "revision-1",
        "review_revision": "review-1",
        "checks": "SUCCESS",
        "threads": [],
        "sources": [{"target": "owner/repo#2", "body": BODY, "state": "open", "labels": []}],
    }
    if pr_target is not ABSENT:
        snap["pr_target"] = pr_target
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr(
            "junit.xml", '<testsuite tests="1"><testcase name="Ship the report"/></testsuite>'
        )
    native_stream = io.BytesIO()
    if native:
        snap["merge"] = "d" * 40
        snap["verifier"] = {
            "schema": "verifier-corpus-decision/v1",
            "verdict": "NON_PASS",
            "repo": "owner/repo",
            "pr": 12,
            "head_sha": HEAD,
            "evaluated_sha": snap["merge"],
            "run_id": 34,
            "run_attempt": 1,
            "provider_verdicts": ["FAIL"],
            "ci_failed": True,
        }
        native_manifest = {
            "schema": "workflows-verifier-input-snapshot/v1",
            "repository": "owner/repo",
            "pr": native_manifest_pr,
            "merge_sha": snap["merge"],
            "source_run_id": 34,
            "verified_input": "Ship the report",
        }
        with zipfile.ZipFile(native_stream, "w") as archive:
            archive.writestr("input.json", json.dumps(native_manifest))
            if native_report:
                archive.writestr(
                    "comparison.json",
                    json.dumps(
                        {
                            "results": [
                                {
                                    "verdict": "CONCERNS",
                                    "provider_used": "test-provider",
                                    "model": "test-model",
                                    "concerns": [
                                        "Ship the report output differs",
                                        "Ship the report attachment differs",
                                    ],
                                }
                            ],
                            "report": "Ship the report output differs",
                        }
                    ),
                )
                archive.writestr(
                    "comparison-comment.md",
                    "Finding report-mismatch: Ship the report output differs",
                )
                archive.writestr("verifier-context.md", "current merge report context")
                archive.writestr("verifier-pr-diff.patch", "+def ship_report(): pass")

    def api(endpoint, **_):
        if "/compare/" in endpoint:
            return {
                "status": "ahead",
                "files": [
                    {
                        "filename": "report.py",
                        "status": "added",
                        "patch": "+def ship_report(): pass",
                        "sha": "c" * 40,
                    }
                ],
            }
        if "/actions/runs?" in endpoint:
            return {"workflow_runs": [{"id": 7, "head_sha": HEAD}]}
        if endpoint.endswith("/runs/7"):
            return {
                "id": 7,
                "run_attempt": 2,
                "head_sha": HEAD,
                "status": "completed",
                "conclusion": "success",
                "pull_requests": [{"number": 12}],
            }
        if endpoint.endswith("/runs/34"):
            return {
                "id": 34,
                "run_attempt": 1,
                "event": "pull_request_target",
                "path": ".github/workflows/agents-verifier.yml",
                "status": "completed",
                "conclusion": "success",
                "repository": {"full_name": "owner/repo"},
                "head_sha": HEAD,
                "pull_requests": [],
            }
        if endpoint.endswith("/runs/34/artifacts?per_page=100"):
            return {
                "artifacts": [
                    {"id": 35, "name": "native-input", "size_in_bytes": 100, "expired": False}
                ]
            }
        if endpoint.endswith("/runs/7/artifacts?per_page=100"):
            return {
                "artifacts": [{"id": 9, "name": "tests", "size_in_bytes": 100, "expired": False}]
            }
        raise AssertionError(endpoint)

    packet = e.collect_evidence(
        snap,
        api,
        tmp_path / "evidence",
        dispositions=dispositions,
        artifact_fetch=lambda artifact, **_kw: (
            native_stream.getvalue() if artifact.get("id") == 35 else stream.getvalue()
        ),
    )
    manifest = tmp_path / "evidence" / "manifests" / (packet["manifest"]["packet_sha256"] + ".json")
    ref = {"path": str(manifest), "sha256": hashlib.sha256(manifest.read_bytes()).hexdigest()}
    raw = packet["sections"]["ci_artifacts"]["content"]["artifacts"][0]["receipts"][0]["raw"]
    proof = {
        "evidence_sha256": raw["sha256"],
        "artifact_id": 9,
        "run_id": 7,
        "run_attempt": 2,
        "head_sha": HEAD,
        "kind": "junit",
        "receipt_path": "junit.xml",
        "excerpt": "Ship the report",
        "relevance": "This test exercises shipping the report.",
    }
    criteria = [
        {
            "source": "owner/repo#2",
            "criterion": value,
            "result": "PASS",
            "evidence": [raw],
            "proofs": [proof],
        }
        for value in (BODY, "Ship the report")
    ]
    proposal = {
        "target": TARGET,
        "head": HEAD,
        "base": BASE,
        "classification": "relevant",
        "substantive_needs": [],
        "findings": [],
        "decision": {"criteria": criteria},
    }
    interpretation = {"actual": {"exit": 0, "run_id": "actual-run-7"}, "proposal": proposal}
    prep = {
        "revision": snap["revision"],
        "producer_sha256": packet["producer_sha256"],
        "evidence_summary": {"section_digests": packet["section_digests"]},
    }
    return snap, packet, ref, prep, interpretation


def check(fx):
    snap, _packet, ref, prep, interpretation = fx
    return d.integrate_ordinary(TARGET, snap, ref, prep, interpretation)


def test_valid_current_head_raw_receipt_mapping_needs_no_strong_review(tmp_path):
    result = check(fixture(tmp_path))
    assert result["status"] == "accepted"
    assert result["disposition"]["reviewer"] == "pipeline:actual-run-7"
    assert len(result["disposition"]["criteria"]) == 2


def test_real_collector_disposition_reaches_existing_acceptance_and_merge_guard(tmp_path):
    snap, packet, ref, prep, interpretation = fixture(tmp_path)
    decision = d.integrate_ordinary(TARGET, snap, ref, prep, interpretation)
    assert decision["status"] == "accepted"
    current = {
        **snap,
        "state": "OPEN",
        "draft": False,
        "active_threads": 0,
        "mergeable": "MERGEABLE",
        "labels": [],
        "verifier": None,
        "sources": [dict(snap["sources"][0], state="open")],
    }
    calls = []

    class FakeGitHub:
        deadline = time.time() + 60

        def snapshot(self, _target):
            return current

    def guard(command, *, timeout):
        calls.append((command, timeout))
        return SimpleNamespace(returncode=0, stdout='{"merge_executed":true,"blocked":false}')

    from types import SimpleNamespace

    backend = dispatcher.Closer(
        FakeGitHub(),
        state_path=tmp_path / "queue.json",
        apply=True,
        dispositions={TARGET: decision["disposition"]},
        workers=False,
        run_fn=guard,
    )
    result = backend.execute(TARGET, "merge", current, {"deadline": time.time() + 60})
    assert result["status"] == "completed"
    assert calls and calls[0][0][1].endswith("merge_guard.py")
    assert calls[0][0][calls[0][0].index("--expected-head") + 1] == HEAD


def test_strong_exception_schema_uses_actual_run_and_same_retained_proof(tmp_path):
    snap, _packet, ref, prep, interpretation = fixture(tmp_path)
    strong = {**interpretation, "actual": {"exit": 0, "run_id": "actual-strong-run-22"}}
    result = d.integrate_ordinary(TARGET, snap, ref, prep, strong)
    assert result["status"] == "accepted"
    assert result["disposition"]["reviewer"] == "pipeline:actual-strong-run-22"


def test_platform_tmp_alias_is_allowed_while_internal_link_is_not():
    with tempfile.TemporaryDirectory(dir="/tmp") as directory:
        assert check(fixture(Path(directory)))["status"] == "accepted"


def test_source_body_or_section_wrapper_cannot_prove_implementation(tmp_path):
    for label in ("body_retained", "retained"):
        fx = fixture(tmp_path / label)
        source = fx[1]["sections"]["source"]
        bogus = (
            source["content"][0]["body_retained"]
            if label == "body_retained"
            else source["retained"]
        )
        for row in fx[4]["proposal"]["decision"]["criteria"]:
            row["evidence"] = [bogus]
            row["proofs"][0]["evidence_sha256"] = bogus["sha256"]
        assert check(fx)["status"] == "wait"


def test_retained_current_revision_code_patch_can_support_low_semantic_mapping(tmp_path):
    fx = fixture(tmp_path)
    file = fx[1]["sections"]["diff_files"]["content"]["files"][0]
    ref = file["patch_retained"]
    assert e.validate_patch_reference(file, base=BASE, head=HEAD)
    proof = {
        "evidence_sha256": ref["sha256"],
        "kind": "codepatch",
        "filename": "report.py",
        "blob_sha": "c" * 40,
        "base": BASE,
        "head": HEAD,
        "excerpt": "ship_report",
        "relevance": "The patch implements the report shipping path.",
    }
    for row in fx[4]["proposal"]["decision"]["criteria"]:
        row.update(evidence=[ref], proofs=[proof])
    assert check(fx)["status"] == "accepted"


def test_unrelated_receipt_claim_without_raw_excerpt_refused(tmp_path):
    fx = fixture(tmp_path)
    for row in fx[4]["proposal"]["decision"]["criteria"]:
        row["proofs"][0]["excerpt"] = "unrelated database migration"
    assert check(fx)["reason"] == "criterion_proof_unbound_or_unrelated"


def test_stale_identity_producer_and_evidence_bindings_wait(tmp_path):
    for change in ("head", "base", "source", "review", "producer", "digest"):
        fx = fixture(tmp_path / change)
        snap, _packet, _ref, prep, _interpretation = fx
        if change == "head":
            snap["head"] = "c" * 40
        elif change == "base":
            snap["base"] = "c" * 40
        elif change == "source":
            snap["sources"][0]["body"] += "\n- [ ] Extra"
        elif change == "review":
            snap["review_revision"] = "changed"
        elif change == "producer":
            prep["producer_sha256"] = "0" * 64
        else:
            prep["evidence_summary"]["section_digests"] = {}
        assert check(fx)["status"] == "wait"


def test_subject_pr_alias_and_impossible_numeric_receipts_are_rejected(tmp_path):
    for label, alias in (("missing", ABSENT), ("null", None), ("empty", "")):
        fx = fixture(tmp_path / label, pr_target=alias)
        assert fx[1]["subject_pr"] == TARGET
        assert check(fx)["status"] == "accepted"
    fx = fixture(tmp_path)
    fx[0]["pr_target"] = "owner/repo#13"
    assert check(fx)["reason"] == "target_or_revision_changed"
    fx = fixture(tmp_path / "numeric")
    packet = fx[1]
    receipt = packet["sections"]["ci_artifacts"]["content"]["artifacts"][0]["receipts"][0]
    ref = receipt["raw"]
    receipt.update(kind="test-summary", total=0, passed=1, failed=0)
    index = d._receipt_index(packet, Path(fx[2]["path"]).parent.parent)
    assert (ref["path"], ref["sha256"]) not in index
    receipt.update(total=True, passed=True, failed=False)
    index = d._receipt_index(packet, Path(fx[2]["path"]).parent.parent)
    assert (ref["path"], ref["sha256"]) not in index
    receipt.update(total=100, passed=1, failed=0, skipped=0)
    index = d._receipt_index(packet, Path(fx[2]["path"]).parent.parent)
    assert (ref["path"], ref["sha256"]) not in index
    receipt.update(total=2, passed=1, failed=0, skipped=1)
    index = d._receipt_index(packet, Path(fx[2]["path"]).parent.parent)
    assert (ref["path"], ref["sha256"]) in index
    receipt.update(kind="junit", tests=2, failures=0, errors=0, skipped=2)
    index = d._receipt_index(packet, Path(fx[2]["path"]).parent.parent)
    assert (ref["path"], ref["sha256"]) not in index


def test_missing_or_malformed_reviewer_finding_fields_escalate(tmp_path):
    for field, bad_value in (
        ("substantive_needs", None),
        ("substantive_needs", ""),
        ("findings", None),
        ("findings", ""),
        ("findings", ["unstructured finding"]),
    ):
        fx = fixture(tmp_path / f"{field}-{type(bad_value).__name__}")
        if bad_value is None:
            del fx[4]["proposal"][field]
        else:
            fx[4]["proposal"][field] = bad_value
        result = check(fx)
        assert result["status"] == "exception"
        assert result["reason"] == "worker_json_invalid"


def test_malicious_ref_symlink_changed_bytes_and_failed_worker_refused(tmp_path):
    fx = fixture(tmp_path / "hash")
    row = fx[4]["proposal"]["decision"]["criteria"][0]
    row["evidence"][0] = dict(row["evidence"][0], sha256="0" * 64)
    assert check(fx)["status"] == "wait"
    fx = fixture(tmp_path / "symlink")
    row = fx[4]["proposal"]["decision"]["criteria"][0]
    true_path = Path(row["evidence"][0]["path"])
    link = true_path.parent / "fake.xml"
    link.symlink_to(true_path)
    row["evidence"][0] = dict(row["evidence"][0], path=str(link))
    assert check(fx)["status"] == "wait"
    fx = fixture(tmp_path / "failed")
    fx[4]["actual"]["exit"] = 1
    assert check(fx)["reason"] == "worker_execution_unverified"


def test_ambiguity_and_prior_nonpass_cannot_be_rewritten_to_pass(tmp_path):
    fx = fixture(tmp_path / "ambiguous")
    fx[4]["proposal"].update(classification="uncertain", substantive_needs=[])
    assert check(fx)["status"] == "exception"
    fx = fixture(tmp_path / "nonpass")
    fx[4]["proposal"]["decision"]["criteria"][0]["result"] = "FAIL"
    assert check(fx)["reason"] == "nonpass_finding"
    fx = fixture(tmp_path / "verifier")
    fx[0]["verifier"] = {"verdict": "NON_PASS", "run_id": 34, "raw": "failed source verification"}
    result = check(fx)
    assert result["reason"] == "nonpass_findings_not_accounted"
    assert result["raw_findings"][0]["raw"]["verdict"] == "NON_PASS"
    fx = fixture(
        tmp_path / "prior", dispositions={"criteria": [{"result": "FAIL", "criterion": BODY}]}
    )
    assert check(fx)["reason"] == "nonpass_findings_not_accounted"


def test_ordinary_nonpass_resolution_preserves_raw_verdict_without_pass_authority(tmp_path):
    fx = fixture(tmp_path)
    verifier = {"verdict": "NON_PASS", "run_id": 34, "raw": "failed source verification"}
    fx[0]["verifier"] = verifier
    finding = {"source": "native_verifier", "verdict": "NON_PASS", "raw": verifier}
    fx[4]["proposal"]["resolutions"] = [
        {
            "finding_id": d._finding_id(finding),
            "status": "unresolved_missing_evidence",
            "next_guarded_action": "collect_evidence",
            "reason": "the source verifier report has no complete raw receipt yet",
        }
    ]
    result = check(fx)
    assert result["status"] == "ordinary_nonpass", result
    assert result["disposition"]["acceptance_authority"] is False
    assert result["disposition"]["raw_verdicts"][0]["raw"]["verdict"] == "NON_PASS"
    resolution = fx[4]["proposal"]["resolutions"][0]
    resolution.update(
        status="resolved_duplicate",
        evidence=fx[4]["proposal"]["decision"]["criteria"][0]["evidence"],
        proofs=fx[4]["proposal"]["decision"]["criteria"][0]["proofs"],
    )
    assert check(fx)["status"] == "ordinary_nonpass"
    resolution["proofs"][0]["excerpt"] = "not in the raw test receipt"
    assert check(fx)["reason"] == "nonpass_resolution_proof_missing"
    resolution.update(
        status="unresolved_mechanical",
        next_guarded_action="repair",
        repair_plan={
            "head": HEAD,
            "source_owner": "owner/repo#2",
            "paths": ["../escape.py"],
            "prompt": "fix it",
            "expected_failure": "report test failed",
        },
    )
    assert check(fx)["reason"] == "mechanical_repair_plan_unbound"


def test_native_merge_input_is_resolution_only_and_exact_run_bound(tmp_path):
    fx = fixture(tmp_path, native=True)
    snap, packet, _ref, _prep, interpretation = fx
    section = packet["sections"]["native_merge_verifier"]
    assert section["status"] == "complete"
    comparison = section["content"]["artifacts"][0]["receipts"][1]
    assert comparison["path"] == "comparison.json"
    assert comparison["findings"]
    assert comparison["findings"][0]["verdict"] == "CONCERNS"
    raw_ref = section["content"]["artifacts"][0]["receipts"][1]["raw"]
    marker = snap["verifier"]
    pending = d.pending_nonpass_findings(snap, packet)
    assert sum(row["raw"]["source"] == "native_provider_concern" for row in pending) == 2
    assert sum(row["raw"]["source"] == "native_provider_verdict" for row in pending) == 1
    resolutions = [
        {
            "finding_id": row["finding_id"],
            "status": "resolved_nonissue",
            "next_guarded_action": "wait",
            "evidence": [raw_ref],
            "proofs": [
                {
                    "evidence_sha256": raw_ref["sha256"],
                    "kind": "native-comparison",
                    "artifact_id": 35,
                    "run_id": 34,
                    "run_attempt": 1,
                    "evaluated_sha": snap["merge"],
                    "head_sha": HEAD,
                    "receipt_path": "comparison.json",
                    "excerpt": "Ship the report",
                    "relevance": "The native comparison finding concerns this report.",
                }
            ],
        }
        for row in pending
    ]
    interpretation["proposal"]["resolutions"] = resolutions[:-1]
    assert check(fx)["reason"] == "nonpass_findings_not_accounted"
    interpretation["proposal"]["resolutions"] = resolutions
    result = check(fx)
    assert result["status"] == "ordinary_nonpass", result
    assert result["disposition"]["acceptance_authority"] is False
    assert len(result["disposition"]["raw_verdicts"]) == len(pending)
    assert any(
        row["raw"]["source"] == "native_provider_verdict"
        and row["raw"]["raw"]["verdict"] == "CONCERNS"
        for row in result["disposition"]["raw_verdicts"]
    )
    run = section["content"]["run"]
    root = Path(_ref["path"]).parent.parent
    run["event"] = "workflow_dispatch"
    section["provenance"]["trigger_event"] = "workflow_dispatch"
    assert (raw_ref["path"], raw_ref["sha256"]) in d._native_resolution_index(packet, root, marker)
    run["event"] = "push"
    assert not d._native_resolution_index(packet, root, marker)
    run["event"] = "pull_request_target"
    section["provenance"]["trigger_event"] = "pull_request_target"
    run["path"] = ".github/workflows/unrelated.yml"
    assert not d._native_resolution_index(packet, root, marker)
    run["path"] = ".github/workflows/agents-verifier.yml"
    comparison["path"] = "unrelated.json"
    assert (raw_ref["path"], raw_ref["sha256"]) not in d._native_resolution_index(
        packet, root, marker
    )
    comparison["path"] = "comparison.json"
    interpretation["proposal"]["resolutions"][0]["proofs"][0]["run_attempt"] = 2
    assert check(fx)["reason"] == "nonpass_resolution_proof_missing"
    bare = fixture(tmp_path / "bare", native=True, native_report=False)
    bare_ref = bare[1]["sections"]["native_merge_verifier"]["content"]["input_manifest"]["raw"]
    bare_finding = {"source": "native_verifier", "verdict": "NON_PASS", "raw": bare[0]["verifier"]}
    bare[4]["proposal"]["resolutions"] = [
        {
            "finding_id": d._finding_id(bare_finding),
            "status": "resolved_nonissue",
            "next_guarded_action": "wait",
            "evidence": [bare_ref],
            "proofs": [
                {
                    "evidence_sha256": bare_ref["sha256"],
                    "kind": "native-input-manifest",
                    "artifact_id": 35,
                    "run_id": 34,
                    "run_attempt": 1,
                    "evaluated_sha": bare[0]["merge"],
                    "head_sha": HEAD,
                    "receipt_path": "input.json",
                    "excerpt": "Ship the report",
                    "relevance": "The input manifest contains the report title.",
                }
            ],
        }
    ]
    assert check(bare)["reason"] == "nonpass_resolution_proof_missing"


def test_unknown_native_manifest_cannot_attribute_provider_findings(tmp_path):
    snap, packet, _ref, _prep, _interpretation = fixture(
        tmp_path, native=True, native_manifest_pr=13
    )
    section = packet["sections"]["native_merge_verifier"]
    assert section["status"] == "unknown"
    assert section["content"]["artifacts"][0]["receipts"][1]["findings"]
    pending = d.pending_nonpass_findings(snap, packet)
    assert len(pending) == 1
    assert pending[0]["raw"]["source"] == "native_verifier"
