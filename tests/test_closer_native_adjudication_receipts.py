"""Native NON_PASS receipt parity without expanding acceptance or failed-CI trust."""

import copy
import hashlib
import io
import json
import zipfile
from pathlib import Path

import pytest
from test_closer_disposition import HEAD, TARGET, fixture

import closer_disposition as disposition
import closer_evidence as evidence
import closer_execution_contracts as contracts
import closer_model_contracts as model


def native_context(tmp_path, receipt_index=1):
    snapshot, packet, packet_ref, prep, interpreted = fixture(tmp_path / "packet", native=True)
    root = Path(packet_ref["path"]).parent.parent
    raw = packet["sections"]["native_merge_verifier"]["content"]["artifacts"][0]["receipts"][
        receipt_index
    ]["raw"]
    proof = {
        "evidence_sha256": raw["sha256"],
        "kind": "native-comparison" if receipt_index == 1 else "native-comparison-comment",
        "artifact_id": 35,
        "run_id": 34,
        "run_attempt": 1,
        "evaluated_sha": snapshot["merge"],
        "head_sha": HEAD,
        "receipt_path": "comparison.json" if receipt_index == 1 else "comparison-comment.md",
        "excerpt": "Ship the report",
        "relevance": "The native comparison finding concerns this report.",
    }
    proposal = interpreted["proposal"]
    proposal["decision"]["criteria"] = []
    proposal["resolutions"] = [
        {
            "finding_id": finding["finding_id"],
            "status": "unresolved_missing_evidence",
            "next_guarded_action": "collect_evidence",
            "reason": "Named comparison still disputed",
        }
        for finding in disposition.pending_nonpass_findings(snapshot, packet)
    ]
    assert len(proposal["resolutions"]) == 4
    return {
        "snapshot": snapshot,
        "packet": packet,
        "packet_ref": packet_ref,
        "prep": prep,
        "interpreted": interpreted,
        "root": root,
        "ref": raw,
        "proof": proof,
    }


def request(context, *, basis=None, ref=None):
    return contracts.build_adjudication_request(
        context["snapshot"],
        context["packet"],
        context["packet_ref"],
        basis=basis
        or {
            "finding_id": disposition.pending_nonpass_findings(
                context["snapshot"], context["packet"]
            )[0]["finding_id"]
        },
        question="Does this retained native comparison resolve this named finding?",
        evidence_refs=[ref or context["ref"]],
        retained_dir=context["root"],
    )


def answer(context, req):
    return {
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
        "outcome": "resolved_nonissue",
        "reason": "The supplied comparison addresses this exact finding.",
        "evidence": [context["ref"]],
        "proofs": [copy.deepcopy(context["proof"])],
        "next_guarded_action": "wait",
    }


def validate(context, req, response):
    return contracts.validate_adjudication_response(
        req,
        response,
        snapshot=context["snapshot"],
        packet=context["packet"],
        packet_ref=context["packet_ref"],
        retained_dir=context["root"],
    )


def reseal(context):
    """Valid container hashes cannot substitute for the native producer joins."""
    packet = context["packet"]
    for name, section in packet["sections"].items():
        section["sha256"] = evidence._section_hash(section)
        section["retained"] = evidence._retain_bytes(
            context["root"],
            evidence._bytes({key: value for key, value in section.items() if key != "retained"}),
            "json",
        )
        packet["section_digests"][name] = section["sha256"]
    packet["semantic_cache_key"] = evidence._semantic_cache_key(packet)
    packet["manifest"] = {
        "section_files": {
            name: section["retained"] for name, section in packet["sections"].items()
        },
        "packet_sha256": evidence._packet_hash(packet),
    }
    raw = evidence._bytes(packet)
    path = context["root"] / "manifests" / (packet["manifest"]["packet_sha256"] + ".json")
    path.write_bytes(raw)
    context["packet_ref"] = {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest()}
    assert evidence.validate_packet(packet)
    contracts._context(context["snapshot"], packet, context["packet_ref"], context["root"])


@pytest.mark.parametrize("receipt_index", [1, 2], ids=["comparison", "comparison-comment"])
def test_native_comparison_can_adjudicate_one_finding_without_acceptance(tmp_path, receipt_index):
    ctx = native_context(tmp_path, receipt_index)
    original = copy.deepcopy(ctx["interpreted"]["proposal"])
    key = (ctx["ref"]["path"], ctx["ref"]["sha256"])
    assert key not in disposition._receipt_index(ctx["packet"], ctx["root"])
    req = request(ctx)
    prompt = contracts.build_adjudication_prompt(original, req, retained_dir=ctx["root"])
    assert (
        "Ship the report" in prompt
        and len(prompt.encode()) <= contracts.MAX_ADJUDICATION_PROMPT_BYTES
    )
    response = model.parse_low_proposal(json.dumps(answer(ctx, req)))
    merged = contracts.merge_selected_response(original, req, validate(ctx, req, response))
    assert merged["resolutions"][1:] == original["resolutions"][1:]
    for field in ("classification", "substantive_needs", "findings", "decision"):
        assert merged.get(field) == original.get(field)
    result = disposition.integrate_ordinary(
        TARGET,
        ctx["snapshot"],
        ctx["packet_ref"],
        ctx["prep"],
        {**ctx["interpreted"], "proposal": merged},
    )
    assert result["status"] == "ordinary_nonpass"
    assert result["disposition"]["acceptance_authority"] is False
    assert len(result["disposition"]["raw_verdicts"]) == 4
    assert ctx["snapshot"]["verifier"]["verdict"] == "NON_PASS"
    assert key not in disposition._receipt_index(ctx["packet"], ctx["root"])


def test_native_comparison_is_not_original_criterion_or_mechanical_proof(tmp_path):
    ctx = native_context(tmp_path)
    source = ctx["snapshot"]["sources"][0]
    with pytest.raises(contracts.ContractError, match="reference_changed_or_unbound"):
        request(ctx, basis={"source": source["target"], "criterion": "Ship the report"})
    assert all(
        (member if isinstance(member, list) else [member])[0].get("kind")
        not in {"native-comparison", "native-comparison-comment"}
        for member in disposition._receipt_index(ctx["packet"], ctx["root"]).values()
    )


@pytest.mark.parametrize(
    "damage",
    [
        "head",
        "repository",
        "attempt",
        "event",
        "workflow",
        "artifact_run",
        "artifact_merge",
        "receipt_path",
        "manifest_raw",
        "raw_bytes",
        "symlink",
        "arbitrary_content",
    ],
)
def test_native_reference_requires_actual_producer_joins_even_with_valid_packet(tmp_path, damage):
    ctx = native_context(tmp_path)
    section = ctx["packet"]["sections"]["native_merge_verifier"]
    content, provenance = section["content"], section["provenance"]
    artifact = content["artifacts"][0]
    if damage == "head":
        provenance["head_sha"] = "f" * 40
    elif damage == "repository":
        provenance["repository"] = "foreign/repository"
    elif damage == "attempt":
        provenance["run_attempt"] = 99
    elif damage == "event":
        content["run"]["event"] = provenance["trigger_event"] = "push"
    elif damage == "workflow":
        content["run"]["path"] = ".github/workflows/unrelated.yml"
    elif damage == "artifact_run":
        artifact["run_id"] = 999
    elif damage == "artifact_merge":
        artifact["evaluated_sha"] = "f" * 40
    elif damage == "receipt_path":
        artifact["receipts"][1]["path"] = "unapproved.json"
    elif damage == "manifest_raw":
        content["input_manifest"]["raw"] = copy.deepcopy(ctx["ref"])
    elif damage == "raw_bytes":
        Path(ctx["ref"]["path"]).write_bytes(b"changed comparison")
    elif damage == "symlink":
        path = Path(ctx["ref"]["path"])
        foreign = tmp_path / "foreign.json"
        foreign.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(foreign)
    else:
        content["artifacts"] = []
        ctx["packet"]["sections"]["prior_dispositions"]["content"] = {
            "untrusted_nested_ref": {"raw": ctx["ref"], "proof": ctx["proof"]}
        }
    reseal(ctx)
    with pytest.raises(contracts.ContractError, match="reference_changed_or_unbound"):
        request(ctx)


@pytest.mark.parametrize("field", ["merge", "head_sha"])
def test_native_adjudication_requires_current_snapshot_merge_and_head(tmp_path, field):
    ctx = native_context(tmp_path)
    if field == "merge":
        ctx["snapshot"]["merge"] = "f" * 40
    else:
        ctx["snapshot"]["verifier"]["head_sha"] = "f" * 40
    with pytest.raises(contracts.ContractError):
        request(ctx)


@pytest.mark.parametrize("field", ["run_attempt", "head_sha", "evaluated_sha", "receipt_path"])
def test_native_response_proof_stays_exactly_bound(tmp_path, field):
    ctx = native_context(tmp_path)
    req = request(ctx)
    response = answer(ctx, req)
    response["proofs"][0][field] = 999 if field == "run_attempt" else "stale"
    with pytest.raises(contracts.ContractError, match="proof_unbound"):
        validate(ctx, req, response)


def test_native_security_finding_still_requires_existing_owner(tmp_path):
    ctx = native_context(tmp_path)
    ctx["snapshot"]["verifier"]["category"] = "security"
    ctx["packet"]["sections"]["native_merge_verifier"]["content"]["verifier"] = copy.deepcopy(
        ctx["snapshot"]["verifier"]
    )
    reseal(ctx)
    with pytest.raises(contracts.ContractError) as raised:
        request(ctx)
    assert raised.value.owner_required is True
    assert (
        raised.value.bound_request["basis"]["finding_id"]
        == disposition.pending_nonpass_findings(ctx["snapshot"], ctx["packet"])[0]["finding_id"]
    )


def test_genuine_failed_ci_receipt_remains_untrusted_for_adjudication(tmp_path, monkeypatch):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "junit.xml",
            '<testsuite tests="1" failures="1"><testcase name="Ship the report"><failure message="bad output"/></testcase></testsuite>',
        )
    collect = evidence.collect_evidence

    def failed_collect(snapshot, api, root, **kwargs):
        snapshot["checks"] = "FAILURE"

        def failed_api(endpoint, **options):
            value = api(endpoint, **options)
            return {**value, "conclusion": "failure"} if endpoint.endswith("/runs/7") else value

        kwargs["artifact_fetch"] = lambda *_a, **_kw: buffer.getvalue()
        return collect(snapshot, failed_api, root, **kwargs)

    monkeypatch.setattr(evidence, "collect_evidence", failed_collect)
    snapshot, packet, ref, prep, interpreted = fixture(tmp_path / "failed")
    root = Path(ref["path"]).parent.parent
    receipt = packet["sections"]["ci_artifacts"]["content"]["artifacts"][0]["receipts"][0]
    assert receipt["failures"] == 1 and snapshot["checks"] == "FAILURE"
    assert (receipt["raw"]["path"], receipt["raw"]["sha256"]) not in disposition._receipt_index(
        packet, root
    )
    with pytest.raises(contracts.ContractError, match="reference_changed_or_unbound"):
        contracts.build_adjudication_request(
            snapshot,
            packet,
            ref,
            basis={"source": snapshot["sources"][0]["target"], "criterion": "Ship the report"},
            question="Does this retained receipt establish this criterion?",
            evidence_refs=[receipt["raw"]],
            retained_dir=root,
        )
    from types import SimpleNamespace

    import closer_dispatcher as dispatcher

    current = {
        **snapshot,
        "state": "OPEN",
        "draft": False,
        "active_threads": 0,
        "mergeable": "MERGEABLE",
        "labels": [],
    }
    backend = dispatcher.Closer(
        SimpleNamespace(), state_path=tmp_path / "failed-state", apply=False, workers=False
    )
    assert backend.select(current)[0] == "repair"  # Never merge a failed head.


def test_native_pass_does_not_enable_the_resolution_only_index(tmp_path):
    ctx = native_context(tmp_path)
    ctx["snapshot"]["verifier"]["verdict"] = "PASS"
    ctx["packet"]["sections"]["native_merge_verifier"]["content"]["verifier"] = copy.deepcopy(
        ctx["snapshot"]["verifier"]
    )
    reseal(ctx)
    findings = disposition.pending_nonpass_findings(ctx["snapshot"], ctx["packet"])
    if findings:
        with pytest.raises(contracts.ContractError, match="reference_changed_or_unbound"):
            request(ctx, basis={"finding_id": findings[0]["finding_id"]})
    assert (ctx["ref"]["path"], ctx["ref"]["sha256"]) not in contracts._adjudication_receipt_index(
        ctx["snapshot"], ctx["packet"], ctx["root"], {"finding_id": "not-an-authority"}
    )
