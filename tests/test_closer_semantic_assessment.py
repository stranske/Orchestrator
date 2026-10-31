"""Pure synthetic advice/retained-proof controls; no real CI, provider or native run."""

import hashlib
from pathlib import Path

import pytest
from closer_semantic_audit_support import (
    leaves,
    source_path,
    source_unchanged,
    unreserved_pipeline,
)
from closer_semantic_native_integration_support import (
    assert_reviewed_role_registry_successor,
)
from closer_semantic_native_integration_support import (
    integrated_reference as reference,
)
from test_closer_disposition import fixture

import closer_disposition as disposition
import closer_evidence as evidence
import closer_model_contracts as contracts
import closer_semantic_assessment as semantic


def setup(tmp_path, *, native=False, body=None, dispositions=None, native_manifest_pr=12):
    if body is None:
        snap, packet, ref, prep, _old = fixture(
            tmp_path,
            native=native,
            dispositions=dispositions,
            native_manifest_pr=native_manifest_pr,
        )
    else:
        snap = {
            "target": "tutorial/static#12",
            "kind": "pr",
            "head": "a" * 40,
            "base": "b" * 40,
            "revision": "r1",
            "review_revision": "reviews1",
            "checks": "SUCCESS",
            "threads": [],
            "sources": [
                {"target": "tutorial/static#2", "body": body, "state": "open", "labels": []}
            ],
        }

        def api(endpoint, **_):
            if "/compare/" in endpoint:
                return {
                    "status": "ahead",
                    "files": [
                        {
                            "filename": "trim.py",
                            "status": "added",
                            "sha": "c" * 40,
                            "patch": "+def trim(value):\n+    return value.strip()\n",
                        }
                    ],
                }
            if "/actions/runs?" in endpoint:
                return {"workflow_runs": []}
            raise AssertionError(endpoint)

        root = tmp_path / "evidence"
        packet = evidence.collect_evidence(snap, api, root)
        path = root / "manifests" / (packet["manifest"]["packet_sha256"] + ".json")
        ref = {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        prep = {
            "revision": snap["revision"],
            "producer_sha256": packet["producer_sha256"],
            "evidence_summary": {"section_digests": packet["section_digests"]},
        }
    root = Path(ref["path"]).parent.parent
    view = semantic.catalog_input(snap, packet, ref, root)

    def bridge(answer):
        return semantic.materialize(
            answer, snapshot=snap, packet=packet, packet_ref=ref, retained_dir=root
        )

    def integrate(answer):
        return disposition.integrate_ordinary(
            snap["target"],
            snap,
            ref,
            prep,
            {"actual": {"exit": 0, "run_id": "synthetic-pure-only"}, "proposal": bridge(answer)},
        )

    return snap, packet, ref, root, view, bridge, integrate


def answer(view, *, status="satisfied"):
    selected = next(row for row in view["catalog"] if row["purpose"] == "ordinary")
    return {
        "schema": semantic.SCHEMA,
        "obligations": [
            {
                "slot": slot,
                "meaning": "understood",
                "status": status,
                "citations": [
                    {
                        "ref": selected["ref"],
                        "excerpt": selected["content"],
                        "relevance": "The supplied complete code or receipt supports this exact original obligation.",
                    }
                ],
                "explanation": "This is a synthetic semantic claim, not execution or authority.",
            }
            for slot in view["required_obligation_slots"]
        ],
        "findings": [
            {
                "slot": slot,
                "meaning": "understood",
                "status": "unknown",
                "citations": [],
                "explanation": "The original finding is understood; complete resolution evidence is absent.",
                "gap": "evidence_absent",
            }
            for slot in view["required_finding_slots"]
        ],
        "global_concerns": [],
    }


def prompt(snap, view):
    return contracts.render_classification_prompt(
        {},
        source_rows=snap["sources"],
        diff_rows=[],
        review_rows=[],
        artifact_rows=[],
        semantic_inventory=view,
    )


def block(rendered, name):
    return contracts.parse_low_proposal(
        rendered.split(name + "_JSON_BEGIN\n", 1)[1].split("\n" + name + "_JSON_END", 1)[0]
    )


def test_static_positive_actual_collector_bridge_and_unchanged_host(tmp_path):
    snap, packet, ref, root, view, bridge, integrate = setup(
        tmp_path,
        body="## Tasks\n- Strip surrounding whitespace.\n## Acceptance Criteria\n- Return the trimmed string.",
    )
    result = answer(view)
    canonical = bridge(result)
    assert integrate(result)["status"] == "accepted"
    assert len(canonical["decision"]["criteria"]) == 3
    for row in canonical["decision"]["criteria"]:
        assert disposition._proofs_valid(
            row["evidence"], row["proofs"], disposition._receipt_index(packet, root), root
        )
    assert canonical["authority"] is False
    assert "static obligation" in prompt(snap, view)
    assert "explicitly depends" in prompt(snap, view)


def test_exact_rendered_example_roundtrips_real_bridge_and_full_host(tmp_path):
    snap, _, _, _, view, bridge, integrate = setup(
        tmp_path, body="## Tasks\n- Observe a successful current-head test run."
    )
    exact = block(prompt(snap, view), "SEMANTIC_EXECUTION_GAP_RESPONSE")
    canonical = bridge(exact)
    assert canonical["classification"] == "relevant"
    assert canonical["semantic_readiness"] == "knowledge_incomplete"
    assert integrate(exact)["status"] == "exception"
    assert all(row["gap"] == "observed_execution_required" for row in exact["obligations"])


def test_required_sets_allow_shuffled_rows_and_paraphrased_explanations(tmp_path):
    *_, view, bridge, integrate = setup(tmp_path)
    result = answer(view)
    result["obligations"].reverse()
    result["obligations"][0][
        "explanation"
    ] = "A different explanation is fine; identity comes from its explicit slot."
    assert integrate(result)["status"] == "accepted"
    assert bridge(result)["semantic_readiness"] == "acceptance_candidate_only"


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_BODY",
        "missing_AC",
        "duplicate_slot",
        "foreign_slot",
        "bool_slot",
        "missing_meaning",
        "meaning_list",
        "wrong_status",
        "missing_excerpt",
        "excerpt_alias",
        "missing_relevance",
        "wrong_excerpt",
        "foreign_ref",
        "bool_ref",
        "extra_action",
        "opaque_identity",
        "unknown_without_gap",
        "known_with_gap",
        "unknown_meaning_known_status",
    ],
)
def test_necessary_refusals(tmp_path, mutation):
    *_, view, bridge, _integrate = setup(tmp_path)
    value = answer(view)
    row = value["obligations"][0]
    citation = row["citations"][0]
    if mutation == "missing_BODY":
        value["obligations"].pop(0)
    elif mutation == "missing_AC":
        value["obligations"].pop()
    elif mutation == "duplicate_slot":
        value["obligations"][1]["slot"] = row["slot"]
    elif mutation == "foreign_slot":
        row["slot"] = 900
    elif mutation == "bool_slot":
        row["slot"] = True
    elif mutation == "missing_meaning":
        del row["meaning"]
    elif mutation == "meaning_list":
        row["meaning"] = []
    elif mutation == "wrong_status":
        row["status"] = "PASS"
    elif mutation == "missing_excerpt":
        del citation["excerpt"]
    elif mutation == "excerpt_alias":
        citation["raw_excerpt"] = citation.pop("excerpt")
    elif mutation == "missing_relevance":
        del citation["relevance"]
    elif mutation == "wrong_excerpt":
        citation["excerpt"] = "not in the retained original proof"
    elif mutation == "foreign_ref":
        citation["ref"] = 900
    elif mutation == "bool_ref":
        citation["ref"] = True
    elif mutation == "extra_action":
        value["action"] = "merge"
    elif mutation == "opaque_identity":
        citation["sha256"] = "0" * 64
    elif mutation == "unknown_without_gap":
        row["status"] = "unknown"
    elif mutation == "known_with_gap":
        row["gap"] = "evidence_absent"
    elif mutation == "unknown_meaning_known_status":
        row["meaning"] = "unknown"
    with pytest.raises((ValueError, TypeError)):
        bridge(value)


def test_explicit_meaning_is_distinct_from_understood_proof_gap_and_unmet(tmp_path):
    *_, view, bridge, integrate = setup(tmp_path)
    value = answer(view, status="unsatisfied")
    assert bridge(value)["classification"] == "relevant"
    assert bridge(value)["semantic_readiness"] == "known_unmet"
    assert integrate(value)["reason"] == "nonpass_finding"
    row = value["obligations"][0]
    row.update(status="unknown", citations=[], gap="evidence_absent")
    assert bridge(value)["classification"] == "relevant"
    row.update(meaning="unknown", gap="obligation_ambiguous")
    assert bridge(value)["classification"] == "uncertain"


def test_verified_nonpass_mode_can_leave_obligations_unassessed_without_extra_work(tmp_path):
    *_, view, bridge, integrate = setup(tmp_path, native=True)
    assert view["mode"] == "known_NONPASS_accounting"
    assert view["required_obligation_slots"] == []
    value = answer(view)
    canonical = bridge(value)
    assert canonical["decision"]["criteria"] == []
    assert len(canonical["unassessed_obligation_slots"]) == len(view["obligations"])
    assert canonical["classification"] == "uncertain"
    assert canonical["substantive_needs"] == [
        "host_OTHER:" + hold for hold in view["host_residual_holds"]
    ]
    result = integrate(value)
    assert result["status"] == "exception", result
    assert result["disposition"] is None
    assert len(canonical["semantic_original_findings"]) == len(view["findings"])
    assert all(row["status"] == "unknown" for row in value["findings"])


def test_nonpass_resolution_matches_existing_ordinary_plus_native_proof_union(tmp_path):
    *_, view, bridge, integrate = setup(tmp_path, native=True)
    value = answer(view)
    selected = next(
        row
        for row in view["catalog"]
        if row["purpose"] == "native_resolution" and row["kind"] == "native-comparison"
    )
    for row in value["findings"]:
        row.update(
            status="nonissue",
            citations=[
                {
                    "ref": selected["ref"],
                    "excerpt": "Ship the report",
                    "relevance": "The original native comparison explains this exact synthetic source finding.",
                }
            ],
        )
        del row["gap"]
    assert integrate(value)["status"] == "exception"
    assert all(row["status"] == "resolved_nonissue" for row in bridge(value)["resolutions"])
    ci = next(row for row in view["catalog"] if row["purpose"] == "ordinary")
    value["findings"][0]["citations"] = [
        {
            "ref": ci["ref"],
            "excerpt": ci["content"],
            "relevance": "This exact ordinary proof is admitted for the named finding; semantic sufficiency is reviewer advice.",
        }
    ]
    assert bridge(value)["authority"] is False
    assert integrate(value)["status"] == "exception"
    assert all(row["status"] == "resolved_nonissue" for row in bridge(value)["resolutions"])
    assert integrate(value)["disposition"] is None
    assert bridge(value)["host_residual_holds"] == view["host_residual_holds"]


def test_physical_change_foreign_snapshot_and_missing_host_inventory_refused(tmp_path):
    snap, packet, ref, root, view, bridge, _ = setup(tmp_path)
    value = answer(view)
    snap["head"] = "e" * 40
    with pytest.raises(ValueError):
        bridge(value)
    snap["head"] = packet["head"]
    path = Path(disposition._receipt_index(packet, root).popitem()[0][0])
    path.write_bytes(b"changed proof")
    with pytest.raises(ValueError):
        bridge(value)
    with pytest.raises(contracts.ClassificationPromptError):
        contracts.render_classification_prompt(
            {}, source_rows=snap["sources"], diff_rows=[], review_rows=[], artifact_rows=[]
        )


def test_unmodified_host_security_strong_and_parser_ast_and_only_reserved_dispatcher_hunks():
    base = "78f6244c"
    for filename in (
        "closer_disposition.py",
        "closer_execution_contracts.py",
    ):
        assert source_unchanged(base, filename), filename
    # Reviewed native record insertion changes registry data only. All validator
    # ASTs remain exact; pin this explicit successor instead of rewriting history.
    assert_reviewed_role_registry_successor()
    for filename, allowed in (
        (
            "closer_model_contracts.py",
            {"render_classification_prompt", "classification_contract_descriptor"},
        ),
        (
            "closer_dispatcher.py",
            {
                "_controlled_model_contract",
                "_run_staged_pipeline",
                "_initial_dispatch_held",
                "main",
            },
        ),
    ):
        old = reference(base)["files"][filename]["top_level_ast"]
        new = leaves(source_path(filename).read_text())
        assert {k: v for k, v in new.items() if k not in allowed} == {
            k: v for k, v in old.items() if k not in allowed
        }


def test_no_unreserved_nested_dispatcher_edits():
    current = source_path("closer_dispatcher.py").read_text()
    expected = reference("78f6244c")["files"]["closer_dispatcher.py"][
        "unreserved_pipeline_ast_sha256"
    ]
    assert unreserved_pipeline(current) == expected


def test_confirmed_native_defect_stays_relevant_known_unmet_without_fake_resolution(tmp_path):
    *_, view, bridge, _integrate = setup(tmp_path, native=True)
    value = answer(view)
    native = next(row for row in view["catalog"] if row["kind"] == "native-comparison")
    row = value["findings"][0]
    row.update(
        status="confirmed",
        citations=[
            {
                "ref": native["ref"],
                "excerpt": "Ship the report",
                "relevance": "This exact native comparison describes the original synthetic defect.",
            }
        ],
    )
    del row["gap"]
    canonical = bridge(value)
    assert canonical["classification"] == "uncertain"
    assert canonical["semantic_summary"]["known_unmet"] is True
    assert canonical["semantic_readiness"] == "held"  # remaining findings still lack proof
    assert (
        "known_unmet_finding_requires_independently_admitted_source_route"
        in canonical["substantive_needs"]
    )
    assert all(
        resolution["status"] != "unresolved_mechanical" for resolution in canonical["resolutions"]
    )
    assert len(canonical["semantic_original_findings"]) == len(view["findings"])


def test_global_concern_does_not_clear_host_or_grant_action(tmp_path):
    *_, view, bridge, integrate = setup(tmp_path)
    value = answer(view)
    value["global_concerns"] = [
        {
            "kind": "authority_conflict",
            "explanation": "The original task authority is explicitly disputed.",
            "citations": [],
        }
    ]
    canonical = bridge(value)
    assert canonical["semantic_readiness"] == "held"
    assert canonical["authority"] is False
    assert integrate(value)["status"] == "exception"


def test_obligation_multiplicity_is_not_deduplicated(tmp_path):
    *_, view, bridge, _integrate = setup(tmp_path, body="## Tasks\n- Trim values.\n- Trim values.")
    assert len(view["obligations"]) == 3
    assert view["obligations"][1]["text"] == view["obligations"][2]["text"]
    value = answer(view)
    assert len(bridge(value)["decision"]["criteria"]) == 3
    value["obligations"].pop()
    with pytest.raises(ValueError, match="coverage"):
        bridge(value)


def test_exact_rendered_static_input_and_response_pass_actual_full_host(tmp_path):
    # Bootstrap rendering is data-only; the teaching input is then extracted
    # literally, collected into NEW retained files, and its exact output used.
    bootstrap = setup(tmp_path / "bootstrap")
    teaching = block(prompt(bootstrap[0], bootstrap[4]), "SEMANTIC_TUTORIAL_INPUT")
    snap, packet, ref, root, view, bridge, integrate = setup(
        tmp_path / "teaching", body=teaching["body"]
    )
    assert (snap["target"], snap["head"], snap["base"], snap["sources"][0]["target"]) == (
        teaching["target"],
        teaching["head"],
        teaching["base"],
        teaching["source"],
    )
    assert view["catalog"][0]["ref"] == teaching["catalog_ref"]
    assert view["catalog"][0]["content"] == teaching["raw_patch"]
    output = block(prompt(snap, view), "SEMANTIC_TUTORIAL_RESPONSE")
    canonical = bridge(output)
    assert integrate(output)["status"] == "accepted"
    assert len(canonical["decision"]["criteria"]) == 3
    for row in canonical["decision"]["criteria"]:
        assert disposition._proofs_valid(
            row["evidence"], row["proofs"], disposition._receipt_index(packet, root), root
        )
    # Teaching proof copied unchanged to the different receipt fixture fails,
    # even though slots happen to be small integers there too.
    with pytest.raises(ValueError):
        bootstrap[5](output)


@pytest.mark.parametrize("verdict", ["NON_PASS", "FAIL", "FAILED"])
def test_actual_prior_raw_inventory_matches_host_and_can_remain_nonaccepting(tmp_path, verdict):
    original = {"verdict": verdict, "summary": "Original source-owned retained failure"}
    prior = {
        "criteria": [{"result": "FAIL", "criterion": "Original prior criterion"}],
        "raw_findings": [original, dict(original)],
    }
    snap, packet, ref, root, view, bridge, integrate = setup(
        tmp_path, native=True, dispositions=prior
    )
    expected = disposition.pending_nonpass_findings(snap, packet)
    expected += [{"finding_id": disposition._finding_id(original), "raw": original}] * 2
    assert len(view["findings"]) == len(expected)
    canonical = bridge(answer(view))
    assert canonical["semantic_original_findings"] == expected
    assert [row["finding_id"] for row in canonical["resolutions"]] == [
        row["finding_id"] for row in expected
    ]
    assert view["required_obligation_slots"] == []
    result = integrate(answer(view))
    assert result["status"] == "exception", result
    assert len(canonical["semantic_original_findings"]) == len(expected)
    assert result["disposition"] is None
    assert canonical["host_residual_holds"] == view["host_residual_holds"]
    value = answer(view)
    value["findings"].pop()
    with pytest.raises(ValueError, match="coverage"):
        bridge(value)
    value = answer(view)
    value["findings"][-1]["slot"] = value["findings"][0]["slot"]
    with pytest.raises(ValueError, match="slot"):
        bridge(value)


def test_source_known_nonpass_missing_native_input_is_proofgap_not_extra_assessment(tmp_path):
    *_, view, bridge, integrate = setup(tmp_path, native=True, native_manifest_pr=99)
    assert view["mode"] == "known_NONPASS_accounting"
    assert view["required_obligation_slots"] == []
    assert all(row["purpose"] != "native_resolution" for row in view["catalog"])
    canonical = bridge(answer(view))
    assert canonical["classification"] == "relevant"
    assert canonical["decision"]["criteria"] == []
    assert canonical["unassessed_obligation_slots"] == [1, 2]
    assert integrate(answer(view))["status"] == "ordinary_nonpass"


def test_prior_ordinary_finding_resolution_uses_its_existing_ordinary_proof_scope(tmp_path):
    prior = {"raw_findings": [{"verdict": "FAIL", "summary": "Original ordinary static finding"}]}
    *_, view, bridge, integrate = setup(tmp_path, dispositions=prior)
    assert [row["proof_scope"] for row in view["findings"]] == ["ordinary_or_native_resolution"]
    value = answer(view)
    selected = next(row for row in view["catalog"] if row["purpose"] == "ordinary")
    row = value["findings"][0]
    row.update(
        status="nonissue",
        citations=[
            {
                "ref": selected["ref"],
                "excerpt": selected["content"],
                "relevance": "The actual retained ordinary receipt explains this exact original ordinary finding.",
            }
        ],
    )
    del row["gap"]
    assert integrate(value)["status"] == "ordinary_nonpass"
    assert bridge(value)["authority"] is False
