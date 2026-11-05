"""Component-only synthetic sealed STRONG advice; no native/provider launch."""

import copy
import hashlib
import json

import pytest
from test_closer_controlled_origin import proof as component_proof
from test_closer_controlled_origin import validate
from test_closer_semantic_assessment import answer, setup

import closer_data_only_execution as execution
import closer_dispatcher as closer
import closer_execution_contracts as contracts
import closer_semantic_assessment as semantic
import dispatcher
import transport_saved_result


def disputed(tmp_path, *, native=False):
    case = setup(tmp_path, native=native)
    value = answer(case[4])
    section = "findings" if native else "obligations"
    selected = next(
        row
        for row in case[4]["catalog"]
        if row["purpose"] == ("native_resolution" if native else "ordinary")
    )
    for row in value[section]:
        row.update(
            meaning="understood",
            status="unknown",
            gap="evidence_inconclusive",
            citations=[
                {
                    "ref": selected["ref"],
                    "excerpt": selected["content"],
                    "relevance": "The complete selected receipt leaves this original synthetic question disputed.",
                }
            ],
        )
    return case, value


def candidate(case, value):
    snap, packet, ref, root, *_ = case
    return semantic.adjudication_candidate(
        value, snapshot=snap, packet=packet, packet_ref=ref, retained_dir=root
    )


def scoped_answer(request, selected, *, native=False, outcome=None):
    value = {
        key: copy.deepcopy(request[key])
        for key in ("target", "head", "base", "revision", "packet_sha256", "request_id", "basis")
    }
    value.update(
        schema="closer-criterion-response/v1",
        outcome=outcome or ("resolved_nonissue" if native else "satisfied"),
        reason="The actual scoped answer decides this one synthetic original question.",
        evidence=copy.deepcopy(selected["evidence"]),
        proofs=copy.deepcopy(selected["proofs"]),
    )
    if native:
        value["next_guarded_action"] = "wait"
    if value["outcome"] in {
        "insufficient_evidence",
        "unresolved_missing_evidence",
        "unresolved_mechanical",
    }:
        value["evidence"], value["proofs"] = [], []
    return value


def sealed_strong(
    tmp_path, monkeypatch, value, snap, ref, request, *, original, prompt_override=None
):
    """Only synthetic observed-data ports; all production proof readers stay real."""
    tmp_path.mkdir()
    proof = component_proof.__wrapped__(tmp_path)
    result, expected = proof.result, proof.expected
    origin = result["data_only_origin"]
    descriptor = closer._controlled_model_contract("closer_exception")
    limits = {
        **expected["limits"],
        "role": "strong_criterion_adjudication",
        "model_contract_sha256": descriptor["contract_sha256"],
    }
    cfg, binding = origin["execution"]["config"], origin["execution"]["binding"]
    from pathlib import Path

    prompt = contracts.build_adjudication_prompt(
        original, request, retained_dir=Path(ref["path"]).parent.parent
    )
    if prompt_override is not None:
        prompt = prompt_override
    env = execution.isolated_environment(
        cfg["private_root"],
        cwd=cfg["cwd"],
        existing_oauth_token="synthetic-component-token",
        limits=limits,
    )
    launch = execution.validate_launch(
        agent="claude",
        profile=expected["profile_identity"],
        binary_path=binding["binary_path"],
        binary_sha256=binding["binary_sha256"],
        argv=execution.expected_argv(
            binding["binary_path"], expected["profile_identity"], limits=limits
        ),
        env=env,
        cwd=cfg["cwd"],
        private_root=cfg["private_root"],
        response_protocol=execution.NATIVE_JSON,
        source_sha256={
            name: descriptor["sources"][name]
            for name in ("adapters.py", "dispatcher.py", "closer_data_only_execution.py")
        },
        limits=limits,
        prompt=prompt,
        timeout_seconds=20,
    )
    expected.update(
        task_type="closer_exception",
        production_model_contract=descriptor,
        limits=limits,
        finite_frontier_target=snap["target"],
        prompt=execution.validate_payload(prompt, limits=limits, kind="prompt"),
        basis={
            "target": snap["target"],
            "head": snap["head"],
            "base": snap["base"],
            "revision": snap["revision"],
            "packet_sha256": ref["sha256"],
            "request_id": request["request_id"],
        },
    )
    origin.update(
        task_type="closer_exception", finite_frontier_target=snap["target"], execution=launch
    )
    for observed in [
        result["process_observation"],
        origin["process_observation"],
        *proof.observed.values(),
    ]:
        observed.update(
            task_type="closer_exception",
            finite_frontier_target=snap["target"],
            data_only_execution_binding_sha256=launch["binding_sha256"],
        )
    result["output"] = json.dumps(value)
    native = json.loads(origin["native_json"])
    native["result"] = result["output"]
    origin["native_json"] = json.dumps(native)
    origin["native_stdout_sha256"] = hashlib.sha256(origin["native_json"].encode()).hexdigest()
    origin["native_stdout_bytes"] = len(origin["native_json"].encode())
    origin["final"] = execution.validate_payload(result["output"], limits=limits, kind="final")
    origin["normalized_native_result"] = transport_saved_result.normalize_claude_result(
        origin["native_json"],
        run_id=result["run_id"],
        pool_id="claude-subscription",
        requested_model=expected["profile_identity"]["requested_model"],
        requested_effort=expected["profile_identity"]["reasoning_effort"],
    )
    proof.retain()
    checked = validate(proof)
    monkeypatch.setattr(dispatcher, "DISPATCH_LOG_DIR", tmp_path)
    return {"run_id": result["run_id"], "_closer_controlled_proof": checked}, proof


def reconcile(case, value, request, result, actual):
    snap, packet, ref, root, *_ = case
    return semantic.reconcile_selected(
        value,
        request,
        result,
        snapshot=snap,
        packet=packet,
        packet_ref=ref,
        retained_dir=root,
        strong_actual=actual,
        expected_catalog_sha256=semantic.catalog_digest(case[4]),
    )


def test_one_candidate_is_original_ordered_not_output_order(tmp_path):
    case, value = disputed(tmp_path)
    before = copy.deepcopy(value)
    original, selected = candidate(case, value)
    value["obligations"].reverse()
    reversed_original, reversed_selected = candidate(case, value)
    assert selected == reversed_selected
    assert selected["slot"] == 1 and selected["status"] == "candidate"
    assert original["adjudication_request"] == reversed_original["adjudication_request"]
    assert before["obligations"] == list(reversed(value["obligations"]))
    assert selected["authority"] is False


def test_actual_scoped_receipt_changes_only_selected_row_preserves_low_and_other_gaps(
    tmp_path, monkeypatch
):
    case, value = disputed(tmp_path / "case")
    original_low = copy.deepcopy(value)
    original, selected = candidate(case, value)
    request = selected["request"]
    result = scoped_answer(request, original["decision"]["criteria"][0])
    actual, proof = sealed_strong(
        tmp_path / "receipt", monkeypatch, result, case[0], case[2], request, original=original
    )
    final = reconcile(case, value, request, result, actual)
    assert value == original_low == final["semantic_assessment"]
    assert final["decision"]["criteria"][0]["result"] == "PASS"
    assert final["decision"]["criteria"][1] == original["decision"]["criteria"][1]
    assert "semantic_acceptance_proof_gap" in final["substantive_needs"]
    assert final["semantic_reconciliation"]["selection_consumed"] is True
    assert final["semantic_reconciliation"]["actual"] == actual
    with pytest.raises(ValueError):
        candidate(case, final)  # canonical reconciliation is never original LOW
    proof.result["provider_result_ref"]  # retained actual component proof exists


def test_last_selected_gap_clears_only_derived_marker_not_global_concern(tmp_path, monkeypatch):
    case, value = disputed(tmp_path / "case")
    value["obligations"][1].update(status="satisfied")
    del value["obligations"][1]["gap"]
    value["global_concerns"] = [
        {
            "kind": "ambiguity",
            "explanation": "Separate global concern remains unresolved.",
            "citations": [],
        }
    ]
    original, selected = candidate(case, value)
    result = scoped_answer(selected["request"], original["decision"]["criteria"][0])
    actual, _ = sealed_strong(
        tmp_path / "receipt",
        monkeypatch,
        result,
        case[0],
        case[2],
        selected["request"],
        original=original,
    )
    final = reconcile(case, value, selected["request"], result, actual)
    assert "semantic_acceptance_proof_gap" not in final["substantive_needs"]
    assert "semantic_global_concern" in final["substantive_needs"]
    assert final["semantic_assessment"]["global_concerns"] == value["global_concerns"]
    assert final["semantic_readiness"] == "held"


@pytest.mark.parametrize(
    "fault", ["raw_bytes", "answer_substitution", "request_substitution", "missing_actual"]
)
def test_scoped_chain_cannot_be_forged(tmp_path, monkeypatch, fault):
    case, value = disputed(tmp_path / "case")
    original, selected = candidate(case, value)
    request = selected["request"]
    result = scoped_answer(request, original["decision"]["criteria"][0])
    actual, proof = sealed_strong(
        tmp_path / "receipt", monkeypatch, result, case[0], case[2], request, original=original
    )
    if fault == "raw_bytes":
        from pathlib import Path

        Path(proof.result["provider_result_ref"]["path"]).write_bytes(b"changed")
    elif fault == "answer_substitution":
        result["reason"] = "Caller substituted meaning not in actual original scoped response."
    elif fault == "request_substitution":
        request = {**request, "question": "A different original question?"}
    else:
        actual = {}
    with pytest.raises(ValueError):
        reconcile(case, value, request, result, actual)


@pytest.mark.parametrize(
    "kind", ["no_citations", "duplicate_original", "known_unmet", "owner_risk"]
)
def test_absent_dispute_admission_has_finite_dependency_no_second_selection(tmp_path, kind):
    body = "## Tasks\n- Trim values.\n- Trim values." if kind == "duplicate_original" else None
    if body:
        case = setup(tmp_path, body=body)
        value = answer(case[4])
        value["obligations"] = value["obligations"][1:]
        # Keep BODY known; dispute only duplicated original criterion occurrences.
        first = answer(case[4])["obligations"][0]
        value["obligations"].insert(0, first)
        for row in value["obligations"][1:]:
            row.update(status="unknown", gap="evidence_inconclusive")
    else:
        case, value = disputed(tmp_path)
    if kind == "no_citations":
        value["obligations"][0]["citations"] = []
    elif kind == "known_unmet":
        for row in value["obligations"]:
            row.update(status="unsatisfied")
            del row["gap"]
    elif kind == "owner_risk":
        value["global_concerns"] = [
            {
                "kind": "security",
                "explanation": "Actual security concern needs its qualified owner.",
                "citations": [],
            }
        ]
    _original, selected = candidate(case, value)
    assert selected["status"] in {"pending", "not_needed"}
    assert "request" not in selected


def test_unsupported_mechanical_outcome_never_relabels_semantics(tmp_path, monkeypatch):
    case, value = disputed(tmp_path / "case", native=True)
    original, selected = candidate(case, value)
    assert selected["status"] == "candidate", selected
    result = scoped_answer(
        selected["request"],
        original["resolutions"][0],
        native=True,
        outcome="unresolved_mechanical",
    )
    actual, _ = sealed_strong(
        tmp_path / "receipt",
        monkeypatch,
        result,
        case[0],
        case[2],
        selected["request"],
        original=original,
    )
    before = copy.deepcopy(value)
    with pytest.raises(ValueError, match="registered_plan"):
        reconcile(case, value, selected["request"], result, actual)
    assert value == before
