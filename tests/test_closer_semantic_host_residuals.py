"""Fresh current-producer synthetic data only; no model or native execution."""

import copy
import hashlib
import io
import json
import zipfile
from pathlib import Path

import pytest
from test_closer_semantic_assessment import answer, block, prompt

import closer_disposition as disposition
import closer_evidence as evidence
import closer_model_contracts as contracts
import closer_semantic_assessment as semantic

MISMATCH = "native_provider_FAIL_CONCERNS_mismatch"
CI_GAP = "native_ci_failed_without_same_boundary_failed_receipt"


def collected(
    tmp_path,
    *,
    reported="FAIL",
    actual="CONCERNS",
    ci_failed=True,
    same_boundary=False,
    failed=False,
    manifest_pr=12,
    attempt=1,
    report_text="",
    native=True,
    native_head=None,
):
    head = "a" * 40
    merge = head if same_boundary else "d" * 40
    snap = {
        "target": "owner/repo#12",
        "kind": "pr",
        "head": head,
        "base": "b" * 40,
        "revision": "synthetic-r1",
        "review_revision": "reviews1",
        "checks": "SUCCESS",
        "threads": [],
        "sources": [
            {
                "target": "owner/repo#2",
                "body": "## Tasks\n- Preserve the input.",
                "state": "open",
                "labels": [],
            }
        ],
    }
    if native:
        snap.update(
            merge=merge,
            verifier={
                "schema": "verifier-corpus-decision/v1",
                "verdict": "NON_PASS",
                "repo": "owner/repo",
                "pr": 12,
                "head_sha": head,
                "evaluated_sha": merge,
                "run_id": 34,
                "run_attempt": 1,
                "provider_verdicts": [reported],
                "ci_failed": ci_failed,
            },
        )
    native_zip = io.BytesIO()
    with zipfile.ZipFile(native_zip, "w") as archive:
        archive.writestr(
            "input.json",
            json.dumps(
                {
                    "schema": "workflows-verifier-input-snapshot/v1",
                    "repository": "owner/repo",
                    "pr": manifest_pr,
                    "merge_sha": merge,
                    "source_run_id": 34,
                }
            ),
        )
        archive.writestr(
            "comparison.json",
            json.dumps(
                {
                    "results": [
                        {
                            "verdict": actual,
                            "provider_used": "synthetic-provider",
                            "model": "synthetic-model",
                            "summary": report_text,
                            "concerns": ["The input handling has a substantive defect."],
                        }
                    ],
                    "report": report_text,
                }
            ),
        )
    ci_zip = io.BytesIO()
    with zipfile.ZipFile(ci_zip, "w") as archive:
        archive.writestr(
            "junit.xml",
            '<testsuite><testcase name="input">'
            + ("<failure>synthetic failure</failure>" if failed else "")
            + "</testcase></testsuite>",
        )
    ci_run, ci_attempt = (34, 1) if same_boundary else (7, 2)

    def api(endpoint, **_):
        if "/compare/" in endpoint:
            return {
                "status": "ahead",
                "files": [
                    {
                        "filename": "input.py",
                        "status": "added",
                        "sha": "c" * 40,
                        "patch": "+def preserve(value): return value",
                    }
                ],
            }
        if "/actions/runs?" in endpoint:
            return {"workflow_runs": [{"id": ci_run, "head_sha": head}]}
        if endpoint.endswith("/runs/34"):
            return {
                "id": 34,
                "run_attempt": attempt,
                "head_sha": native_head or head,
                "event": "pull_request_target",
                "path": ".github/workflows/agents-verifier.yml",
                "status": "completed",
                "conclusion": "failure" if same_boundary and failed else "success",
                "repository": {"full_name": "owner/repo"},
                "pull_requests": [{"number": 12}],
            }
        if endpoint.endswith("/runs/7"):
            return {
                "id": 7,
                "run_attempt": ci_attempt,
                "head_sha": head,
                "status": "completed",
                "conclusion": "failure" if failed else "success",
                "pull_requests": [{"number": 12}],
            }
        if endpoint.endswith("/runs/34/artifacts?per_page=100"):
            return {
                "artifacts": [
                    {"id": 35, "name": "native-input", "size_in_bytes": 100, "expired": False}
                ]
                + (
                    [{"id": 9, "name": "tests", "size_in_bytes": 100, "expired": False}]
                    if same_boundary
                    else []
                )
            }
        if endpoint.endswith("/runs/7/artifacts?per_page=100"):
            return {
                "artifacts": [{"id": 9, "name": "tests", "size_in_bytes": 100, "expired": False}]
            }
        raise AssertionError(endpoint)

    root = tmp_path / "evidence"
    packet = evidence.collect_evidence(
        snap,
        api,
        root,
        artifact_fetch=lambda artifact, **_: (
            native_zip.getvalue() if artifact["id"] == 35 else ci_zip.getvalue()
        ),
    )
    path = root / "manifests" / (packet["manifest"]["packet_sha256"] + ".json")
    ref = {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    return snap, packet, ref, root


def holds(fixture):
    return semantic.host_residual_holds(*fixture)


def test_exact_compound_fields_become_independent_host_holds_without_model(tmp_path):
    fx = collected(tmp_path)
    assert holds(fx) == [MISMATCH, CI_GAP]
    assert fx[1]["sections"]["native_merge_verifier"]["content"]["run"]["conclusion"] == "success"
    # This executes only collector data ports and the host helper, no role callback.


@pytest.mark.parametrize(
    "options,expected",
    [
        ({"reported": "CONCERNS", "ci_failed": False}, []),
        ({"actual": "FAIL", "ci_failed": False}, []),
        ({"ci_failed": False}, [MISMATCH]),
        ({"reported": "CONCERNS"}, [CI_GAP]),
        ({"failed": True, "reported": "CONCERNS"}, [CI_GAP]),
        ({"manifest_pr": 13}, []),
        ({"attempt": 2}, []),
        ({"native_head": "e" * 40}, []),
        ({"native": False, "report_text": "provider_verdicts FAIL CONCERNS ci_failed=true"}, []),
    ],
)
def test_presence_absence_wrong_boundaries_and_untrusted_lookalikes(tmp_path, options, expected):
    assert holds(collected(tmp_path, **options)) == expected


def test_real_collector_exact_boundary_failed_receipt_explains_only_ci_flag(tmp_path):
    fx = collected(tmp_path, same_boundary=True, failed=True)
    assert holds(fx) == [MISMATCH]
    assert not disposition._receipt_index(fx[1], fx[3]).get(
        ("made-up", "f" * 64)
    )  # No invented acceptance or receipt registration.


def test_model_omission_and_semantic_concerns_cannot_erase_host_residuals(tmp_path):
    snap, packet, ref, root = collected(tmp_path)
    view = semantic.catalog_input(snap, packet, ref, root)
    response = answer(view)
    assert response["global_concerns"] == []
    original = copy.deepcopy(response)
    result = semantic.materialize(
        response,
        snapshot=snap,
        packet=packet,
        packet_ref=ref,
        retained_dir=root,
        expected_catalog_sha256=semantic.catalog_digest(view),
    )
    assert response == original
    assert result["semantic_assessment"] == original
    assert result["host_residual_holds"] == [MISMATCH, CI_GAP]
    assert result["classification"] == "uncertain"
    assert result["semantic_readiness"] == "held"
    assert set(result["substantive_needs"]) >= {"host_OTHER:" + MISMATCH, "host_OTHER:" + CI_GAP}
    response["global_concerns"] = [
        {
            "kind": "security",
            "explanation": "A separate synthetic security concern.",
            "citations": [],
        }
    ]
    preserved = semantic.materialize(
        response,
        snapshot=snap,
        packet=packet,
        packet_ref=ref,
        retained_dir=root,
        expected_catalog_sha256=semantic.catalog_digest(view),
    )
    assert "semantic_global_concern" in preserved["substantive_needs"]
    assert "host_OTHER:" + CI_GAP in preserved["substantive_needs"]
    assert len(preserved["semantic_original_findings"]) == len(view["findings"])


def test_rendered_v2_inventory_and_schema_bind_host_responsibility(tmp_path):
    snap, packet, ref, root = collected(tmp_path)
    view = semantic.catalog_input(snap, packet, ref, root)
    rendered = prompt(snap, view)
    assert block(rendered, "CURRENT_HOST_INVENTORY")["host_residual_holds"] == [MISMATCH, CI_GAP]
    assert block(rendered, "SEMANTIC_RESPONSE_SCHEMA")["schema"] == semantic.SCHEMA
    assert contracts.classification_contract_descriptor(source_sha256="f" * 64)[
        "prompt_version"
    ].endswith("/v2")
    assert "No residual flag, equal values, or absence of a flag proves compatibility" in rendered
    forged = answer(view)
    forged["host_residual_holds"] = []
    with pytest.raises(semantic.AssessmentError, match="assessment_schema_invalid"):
        semantic.materialize(
            forged, snapshot=snap, packet=packet, packet_ref=ref, retained_dir=root
        )


def test_prose_lookalikes_in_actual_report_do_not_manufacture_flags(tmp_path):
    fx = collected(
        tmp_path,
        reported="CONCERNS",
        actual="CONCERNS",
        ci_failed=False,
        report_text="provider_verdicts=['FAIL']; ci_failed=true; CONCERNS",
    )
    assert holds(fx) == []
    view = semantic.catalog_input(*fx)
    result = semantic.materialize(
        answer(view), snapshot=fx[0], packet=fx[1], packet_ref=fx[2], retained_dir=fx[3]
    )
    assert result["semantic_original_findings"]  # Absence is not acceptance or erasure.
    assert result["semantic_readiness"] != "acceptance_candidate_only"


def test_old_schema_cannot_silently_enter_prospective_contract(tmp_path):
    snap, packet, ref, root = collected(tmp_path)
    view = semantic.catalog_input(snap, packet, ref, root)
    old = answer(view)
    old["schema"] = "closer-semantic-assessment/v1"
    with pytest.raises(semantic.AssessmentError, match="assessment_schema_invalid"):
        semantic.materialize(old, snapshot=snap, packet=packet, packet_ref=ref, retained_dir=root)


def test_physical_comparison_mutation_cannot_mint_a_trusted_mismatch(tmp_path):
    snap, packet, ref, root = collected(
        tmp_path, reported="CONCERNS", actual="CONCERNS", ci_failed=False
    )
    comparison = next(
        row
        for row in packet["sections"]["native_merge_verifier"]["content"]["artifacts"][0][
            "receipts"
        ]
        if row["kind"] == "native-comparison"
    )
    path = Path(comparison["raw"]["path"])
    path.write_bytes(path.read_bytes().replace(b"CONCERNS", b"FAIL"))
    assert semantic.host_residual_holds(snap, packet, ref, root) == []
    with pytest.raises(semantic.AssessmentError, match="catalog_raw_changed"):
        semantic.catalog_input(snap, packet, ref, root)


def test_actual_sealed_low_replay_rebuilds_holds_ignoring_inline_erasure(tmp_path, monkeypatch):
    from test_closer_semantic_catalog_replay import actual_replay, sealed_input

    snap, packet, ref, root = collected(tmp_path / "case")
    view = semantic.catalog_input(snap, packet, ref, root)
    output = answer(view)
    state = sealed_input(tmp_path / "receipt", monkeypatch, snap, ref, view, output)
    state["proposal"] = {
        "classification": "relevant",
        "substantive_needs": [],
        "host_residual_holds": [],
    }
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps(snap))
    prep = {
        "snapshot": snap,
        "snapshot_ref": {
            "path": str(path),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        },
        "revision": snap["revision"],
        "packet_ref": ref,
    }
    rebuilt = actual_replay(root)(state, snap["target"], prep)["proposal"]
    assert rebuilt["semantic_assessment"] == output
    assert rebuilt["host_residual_holds"] == [MISMATCH, CI_GAP]
    assert rebuilt["classification"] == "uncertain"
    assert "host_OTHER:" + CI_GAP in rebuilt["substantive_needs"]
    assert rebuilt["authority"] is False


def test_actual_scoped_strong_reconciliation_cannot_clear_independent_host_holds(
    tmp_path, monkeypatch
):
    from test_closer_semantic_host_glue import scoped_answer, sealed_strong

    snap, packet, ref, root = collected(tmp_path / "case")
    view = semantic.catalog_input(snap, packet, ref, root)
    output = answer(view)
    selected = next(row for row in view["catalog"] if row["purpose"] == "native_resolution")
    output["findings"][0].update(
        gap="evidence_inconclusive",
        citations=[
            {
                "ref": selected["ref"],
                "excerpt": selected["content"],
                "relevance": "The exact supplied native input leaves this finding disputed.",
            }
        ],
    )
    original, candidate = semantic.adjudication_candidate(
        output,
        snapshot=snap,
        packet=packet,
        packet_ref=ref,
        retained_dir=root,
        expected_catalog_sha256=semantic.catalog_digest(view),
    )
    assert candidate["status"] == "candidate"
    request = candidate["request"]
    resolution = next(
        row
        for row in original["resolutions"]
        if row["finding_id"] == request["basis"]["finding_id"]
    )
    response = scoped_answer(request, resolution, native=True)
    actual, _ = sealed_strong(
        tmp_path / "strong", monkeypatch, response, snap, ref, request, original=original
    )
    rebuilt = semantic.reconcile_selected(
        output,
        request,
        response,
        snapshot=snap,
        packet=packet,
        packet_ref=ref,
        retained_dir=root,
        strong_actual=actual,
        expected_catalog_sha256=semantic.catalog_digest(view),
    )
    assert rebuilt["semantic_assessment"] == output
    assert rebuilt["host_residual_holds"] == [MISMATCH, CI_GAP]
    assert rebuilt["classification"] == "uncertain"
    assert rebuilt["semantic_readiness"] == "held"
    assert "host_OTHER:" + MISMATCH in rebuilt["substantive_needs"]
    assert "host_OTHER:" + CI_GAP in rebuilt["substantive_needs"]


def test_current_snapshot_subject_selector_must_match_original_packet(tmp_path):
    snap, packet, ref, root = collected(tmp_path)
    snap = {**snap, "pr_target": "foreign/repo#13"}
    assert semantic.host_residual_holds(snap, packet, ref, root) == []


@pytest.mark.parametrize(
    "lookalike", [True, "ci_failed", ["model_says_ci_failed"], [CI_GAP, CI_GAP]]
)
def test_renderer_rejects_noncanonical_host_inventory_shape(tmp_path, lookalike):
    snap, packet, ref, root = collected(tmp_path)
    view = semantic.catalog_input(snap, packet, ref, root)
    view["host_residual_holds"] = lookalike
    with pytest.raises(
        contracts.ClassificationPromptError, match="semantic_host_residual_inventory_invalid"
    ):
        prompt(snap, view)
