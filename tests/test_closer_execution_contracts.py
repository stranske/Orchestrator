"""Offline contracts reuse real retained packet/proof/source grammar fixtures."""

import copy
import hashlib
import os

import pytest
from test_closer_disposition import HEAD, fixture

import closer_disposition as disposition
import closer_execution_contracts as contracts


@pytest.fixture
def context(tmp_path):
    snap, packet, ref, _prep, interpretation = fixture(tmp_path / "packet")
    criterion = interpretation["proposal"]["decision"]["criteria"][1]
    snap["verifier"] = {"verdict": "NON_PASS", "run_id": 34, "raw": "report verification failed"}
    finding = disposition.pending_nonpass_findings(snap, packet)[0]
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "report.py").write_text("def ship_report():\n    pass\n")
    (workspace / "test_report.py").write_text("def test_ship():\n    assert True\n")
    tracked = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in workspace.iterdir()
    }
    test = {
        "schema": contracts.TEST_SCHEMA,
        "test_id": "offline-report-regression",
        "target": snap["target"],
        "head": HEAD,
        "finding_id": finding["finding_id"],
        "kind": "pytest",
        "path": "test_report.py",
        "selector": "test_ship",
        "file_sha256": tracked["test_report.py"],
    }
    return {
        "snapshot": snap,
        "packet": packet,
        "packet_ref": ref,
        "retained_dir": tmp_path / "packet" / "evidence",
        "criterion": criterion,
        "finding": finding,
        "original": interpretation["proposal"],
        "workspace": workspace,
        "tracked": tracked,
        "test": test,
    }


def inputs(context):
    return {key: context[key] for key in ("snapshot", "packet", "packet_ref", "retained_dir")}


def request(context, *, finding=False):
    basis = (
        {"finding_id": context["finding"]["finding_id"]}
        if finding
        else {
            "source": context["criterion"]["source"],
            "criterion": context["criterion"]["criterion"],
        }
    )
    return contracts.build_adjudication_request(
        **inputs(context),
        basis=basis,
        question="Does this retained report evidence resolve the named disputed criterion?",
        evidence_refs=context["criterion"]["evidence"],
    )


def answer(context, req):
    value = {
        "schema": contracts.RESPONSE_SCHEMA,
        **{
            key: req[key]
            for key in (
                "request_id",
                "target",
                "head",
                "base",
                "revision",
                "packet_sha256",
                "basis",
            )
        },
        "outcome": "satisfied",
        "reason": "The exact retained report receipt exercises this criterion.",
        "evidence": copy.deepcopy(context["criterion"]["evidence"]),
        "proofs": copy.deepcopy(context["criterion"]["proofs"]),
    }
    if "finding_id" in req["basis"]:
        value.update(outcome="resolved_nonissue", next_guarded_action="wait")
    return value


def mechanical_plan(context):
    value = {
        "schema": contracts.PLAN_SCHEMA,
        "target": context["snapshot"]["target"],
        "head": HEAD,
        "source_owner": context["criterion"]["source"],
        "finding_id": context["finding"]["finding_id"],
        "operation": "format",
        "paths": ["report.py"],
        "required_test": {
            "test_id": context["test"]["test_id"],
            "identity_sha256": contracts._digest(context["test"]),
        },
        "expected_failure": "report verification failed",
        "evidence": context["criterion"]["evidence"],
        "proofs": context["criterion"]["proofs"],
    }
    value["plan_id"] = contracts._digest(value)
    return value


def validate_plan(context, plan, *, tests=None):
    return contracts.validate_mechanical_plan(
        plan,
        **inputs(context),
        workspace=context["workspace"],
        tracked_paths=context["tracked"],
        registered_tests=(
            tests if tests is not None else {context["test"]["test_id"]: context["test"]}
        ),
    )


def redigest(plan):
    plan["plan_id"] = contracts._digest(
        {key: value for key, value in plan.items() if key != "plan_id"}
    )
    return plan


def test_exact_source_criterion_and_finding_requests_are_stable_and_advisory(context):
    for finding in (False, True):
        req = request(context, finding=finding)
        assert req == request(context, finding=finding)
        response = answer(context, req)
        assert (
            contracts.validate_adjudication_response(req, response, **inputs(context)) == response
        )
        assert "acceptance_authority" not in response


@pytest.mark.parametrize(
    "basis",
    [
        None,
        {},
        {"substantive_needs": ["Investigate everything necessary"]},
        {"source": "foreign/repo#1", "criterion": "Ship the report"},
        {"finding_id": "unknown"},
        {"source": "owner/repo#2", "criterion": "invented criterion"},
        {"source": "owner/repo#2", "criterion": "Ship the report", "finding_id": "other"},
    ],
)
def test_missing_generic_wrong_or_multiple_bases_refuse_before_any_worker_contract(context, basis):
    with pytest.raises(contracts.ContractError):
        contracts.build_adjudication_request(
            **inputs(context),
            basis=basis,
            question="Assess this one criterion",
            evidence_refs=context["criterion"]["evidence"],
        )


def test_existing_criterion_hash_grammar_is_reused(context):
    req = request(context)
    hashed = contracts.build_adjudication_request(
        **inputs(context),
        basis=req["basis"],
        question=req["question"],
        evidence_refs=req["evidence"],
    )
    assert hashed == req


@pytest.mark.parametrize("question", [None, "", "generic " * 300, ["first", "second"]])
def test_exactly_one_bounded_question_is_required(context, question):
    with pytest.raises(contracts.ContractError):
        contracts.build_adjudication_request(
            **inputs(context),
            basis=request(context)["basis"],
            question=question,
            evidence_refs=context["criterion"]["evidence"],
        )


@pytest.mark.parametrize(
    "field",
    ["request_id", "target", "head", "base", "revision", "packet_sha256", "basis", "schema"],
)
def test_response_cannot_change_request_scope(context, field):
    req = request(context)
    response = answer(context, req)
    response[field] = "changed"
    with pytest.raises(contracts.ContractError):
        contracts.validate_adjudication_response(req, response, **inputs(context))


def test_broad_full_project_pass_is_not_a_scoped_response(context):
    req = request(context)
    response = answer(context, req)
    response.update(
        classification="relevant",
        decision={"criteria": context["original"]["decision"]["criteria"]},
    )
    with pytest.raises(contracts.ContractError, match="scope"):
        contracts.validate_adjudication_response(req, response, **inputs(context))


def test_proof_and_packet_references_are_real_not_named_metadata(context):
    req = request(context)
    response = answer(context, req)
    response["proofs"][0]["excerpt"] = "this excerpt is absent from the retained receipt"
    with pytest.raises(contracts.ContractError, match="proof"):
        contracts.validate_adjudication_response(req, response, **inputs(context))
    bad = copy.deepcopy(context)
    bad["packet_ref"]["sha256"] = "f" * 64
    with pytest.raises(contracts.ContractError, match="reference"):
        request(bad)


def test_missing_evidence_response_does_not_invent_pass(context):
    req = request(context)
    response = answer(context, req)
    response.update(outcome="insufficient_evidence", evidence=[], proofs=[])
    answer_row = contracts.validate_adjudication_response(req, response, **inputs(context))
    original = copy.deepcopy(context["original"])
    original.update(
        classification="uncertain", substantive_needs=["the named criterion is disputed"]
    )
    merged = contracts.merge_selected_response(original, req, answer_row)
    assert merged["decision"]["criteria"][1]["result"] == "UNKNOWN"
    assert merged["decision"]["criteria"][0] == original["decision"]["criteria"][0]
    assert merged["classification"] == original["classification"]
    assert merged["substantive_needs"] == original["substantive_needs"]
    assert original == context["original"] | {
        "classification": "uncertain",
        "substantive_needs": ["the named criterion is disputed"],
    }


def test_finding_merge_preserves_other_resolution_and_raw_nonpass(context):
    req = request(context, finding=True)
    response = contracts.validate_adjudication_response(
        req, answer(context, req), **inputs(context)
    )
    original = {
        "classification": "uncertain",
        "substantive_needs": ["source owner judgment required"],
        "raw_findings": [context["finding"]],
        "decision": {"criteria": []},
        "resolutions": [
            {"finding_id": req["basis"]["finding_id"], "status": "unresolved_missing_evidence"},
            {"finding_id": "untouched-other-finding", "status": "unresolved_missing_evidence"},
        ],
    }
    merged = contracts.merge_selected_response(original, req, response)
    assert merged["resolutions"][0]["status"] == "resolved_nonissue"
    assert merged["resolutions"][1] == original["resolutions"][1]
    assert merged["raw_findings"] == original["raw_findings"]
    assert merged["decision"]["criteria"] == []
    assert merged["substantive_needs"] == original["substantive_needs"]


@pytest.mark.parametrize("category", ["security", "authority_conflict"])
def test_high_risk_and_conflicting_authority_keep_existing_source_owner_route(context, category):
    context["snapshot"]["verifier"]["category"] = category
    context["finding"] = disposition.pending_nonpass_findings(
        context["snapshot"], context["packet"]
    )[0]
    with pytest.raises(contracts.ContractError) as caught:
        request(context, finding=True)
    refused = contracts.deterministic_refusal(caught.value, context["snapshot"])
    assert refused["source_owners"] == ["owner/repo#2"]
    assert refused["next_guarded_action"] == "wait" and refused["requires_coordinator"] is True
    assert refused["authority"] is False


def test_valid_mechanical_plan_still_does_not_qualify_an_executor(context):
    admitted = validate_plan(context, mechanical_plan(context))
    assert admitted["required_test_identity"] == context["test"]
    assert admitted["runtime_executor_qualified"] is False and admitted["authority"] is False


@pytest.mark.parametrize("field", ["operation", "required_test", "finding_id", "plan_id"])
def test_missing_operation_required_test_or_finding_cannot_admit_writing(context, field):
    plan = mechanical_plan(context)
    del plan[field]
    with pytest.raises(contracts.ContractError):
        validate_plan(context, plan)


@pytest.mark.parametrize("operation", ["replace_algorithm", "publish", "run_shell", "", ["format"]])
def test_substantive_unknown_or_ambiguous_operations_go_to_existing_owner(context, operation):
    plan = redigest(dict(mechanical_plan(context), operation=operation))
    with pytest.raises(contracts.ContractError) as caught:
        validate_plan(context, plan)
    assert caught.value.owner_required is True


def test_currently_accepted_prompt_only_plan_is_now_a_deterministic_owner_refusal(context):
    old = {
        "head": HEAD,
        "source_owner": "owner/repo#2",
        "paths": ["report.py"],
        "prompt": "Replace implementation",
        "expected_failure": "report failed",
    }
    with pytest.raises(contracts.ContractError) as caught:
        validate_plan(context, old)
    assert contracts.deterministic_refusal(caught.value, context["snapshot"])["source_owners"] == [
        "owner/repo#2"
    ]


@pytest.mark.parametrize(
    "paths",
    [["report.py", "report.py"], ["../outside"], ["/outside"], [".git/config"], ["unknown.py"], []],
)
def test_unsafe_duplicate_or_untracked_paths_are_rejected(context, paths):
    with pytest.raises(contracts.ContractError):
        validate_plan(context, redigest(dict(mechanical_plan(context), paths=paths)))


@pytest.mark.parametrize("link", ["symlink", "hardlink"])
def test_even_tracked_paths_cannot_alias_an_outside_file(context, tmp_path, link):
    current = context["workspace"] / "report.py"
    external = tmp_path / "outside.py"
    external.write_bytes(current.read_bytes())
    current.unlink()
    if link == "symlink":
        current.symlink_to(external)
    else:
        os.link(external, current)
    with pytest.raises(contracts.ContractError, match="physically_confined"):
        validate_plan(context, mechanical_plan(context))


def test_current_tracked_content_and_required_test_digest_are_exact(context):
    (context["workspace"] / "report.py").write_text("changed content")
    with pytest.raises(contracts.ContractError, match="physically_confined"):
        validate_plan(context, mechanical_plan(context))


@pytest.mark.parametrize(
    "field,value",
    [
        ("head", "f" * 40),
        ("finding_id", "unrelated-finding"),
        ("kind", "shell"),
        ("selector", "test_ship; curl anything"),
        ("file_sha256", "f" * 64),
    ],
)
def test_registered_test_must_match_current_finding_file_and_supported_identity(
    context, field, value
):
    plan = mechanical_plan(context)
    test = dict(context["test"], **{field: value})
    plan["required_test"]["identity_sha256"] = contracts._digest(test)
    redigest(plan)
    with pytest.raises(contracts.ContractError):
        validate_plan(context, plan, tests={test["test_id"]: test})


def test_no_test_registry_and_freeform_commands_never_enable_execution(context):
    with pytest.raises(contracts.ContractError, match="registered_required_test_missing"):
        validate_plan(context, mechanical_plan(context), tests={})
    plan = dict(mechanical_plan(context), command="black report.py && publish")
    with pytest.raises(contracts.ContractError, match="schema"):
        validate_plan(context, redigest(plan))


@pytest.mark.parametrize("value", [[], {}, True, None])
def test_malformed_test_identity_is_an_owner_refusal_not_a_crash(context, value):
    plan = mechanical_plan(context)
    plan["required_test"]["test_id"] = value
    with pytest.raises(contracts.ContractError):
        validate_plan(context, redigest(plan))


@pytest.mark.parametrize("value", [[], {}, True, None])
def test_malformed_finding_route_is_a_refusal_not_a_crash(context, value):
    req = request(context, finding=True)
    response = answer(context, req)
    response["next_guarded_action"] = value
    with pytest.raises(contracts.ContractError):
        contracts.validate_adjudication_response(req, response, **inputs(context))


def test_raw_ref_objects_and_substituted_directories_are_rejected(context):
    req = request(context)
    req["evidence"][0]["sha256"] = {}
    with pytest.raises(contracts.ContractError):
        contracts.build_adjudication_request(
            **inputs(context),
            basis=req["basis"],
            question=req["question"],
            evidence_refs=req["evidence"],
        )
    current = context["workspace"] / "report.py"
    current.unlink()
    current.mkdir()
    with pytest.raises(contracts.ContractError, match="physically_confined"):
        validate_plan(context, mechanical_plan(context))


def test_changed_path_metadata_without_its_retained_patch_does_not_admit_repair(context):
    raw = context["packet"]["sections"]["diff_files"]["content"]["files"][0]["patch_retained"]
    with open(raw["path"], "wb") as stream:
        stream.write(b"changed retained patch")
    with pytest.raises(contracts.ContractError, match="current_diff"):
        validate_plan(context, mechanical_plan(context))


@pytest.mark.parametrize("kind", ["directory", "symlink", "fifo", "oversize", "bad_hash"])
def test_direct_worker_packet_reads_are_bounded_before_json(context, tmp_path, kind):
    path = tmp_path / "worker-packet"
    ref = dict(context["packet_ref"])
    if kind == "directory":
        path.mkdir()
    elif kind == "symlink":
        path.symlink_to(ref["path"])
    elif kind == "fifo":
        os.mkfifo(path)
    elif kind == "oversize":
        with path.open("wb") as stream:
            stream.truncate(contracts.MAX_BYTES + 1)
    else:
        path.write_text("{}")
    ref["path"] = str(path)
    with pytest.raises(contracts.ContractError, match="retained_packet_reference_changed"):
        contracts.load_retained_packet(ref)


def test_direct_worker_packet_load_matches_exact_retained_object(context):
    packet, root = contracts.load_retained_packet(context["packet_ref"])
    assert packet == context["packet"]
    assert root == context["retained_dir"]


def test_security_finding_refusal_keeps_exact_request_binding_without_admitting_worker(context):
    context["snapshot"]["verifier"]["category"] = "security"
    context["finding"] = disposition.pending_nonpass_findings(
        context["snapshot"], context["packet"]
    )[0]
    with pytest.raises(contracts.ContractError) as caught:
        request(context, finding=True)
    refusal = contracts.deterministic_refusal(caught.value, context["snapshot"])
    identity = refusal["pending_evidence"]["adjudication_request"]
    assert identity["basis"] == {"finding_id": context["finding"]["finding_id"]}
    assert identity["packet_sha256"] == context["packet_ref"]["sha256"]
    assert identity["revision"] == context["snapshot"]["revision"]
    assert identity["request_id"]
    assert refusal["authority"] is False and refusal["requires_coordinator"] is True
