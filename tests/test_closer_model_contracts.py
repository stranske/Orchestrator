"""Offline production-prompt and parser parity, with real retained proof fixtures."""

import copy
import hashlib
import json
from pathlib import Path

import pytest
from test_closer_batch_interpretation import OfflineDelivery, callbacks_for
from test_closer_disposition import TARGET, fixture
from test_closer_execution_contracts import answer, inputs, request
from test_closer_execution_contracts import context as _strong_context

import closer_dispatcher as dispatcher
import closer_disposition as disposition
import closer_evidence_recovery as recovery
import closer_execution_contracts as strong
import closer_model_contracts as contracts
import closer_pipeline

strong_context = _strong_context

# Historical source-renderer fixtures for closer-classification-prompt/v3 at
# 98f25636 (required codepatch kind). Keys are the flags
# run_drift/data_only/recovery_enabled/collection_mode in that order. The large
# Unicode optional payload exercises the unchanged 9,000-character projection.
# Captured research prompts/results and response fixtures are separate history.
_INPUTS = {
    "prompt_mandatory": {
        "target": "offline/repo#12",
        "head": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "base": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        "snapshot_ref": {
            "path": "/retained/snapshot.json",
            "sha256": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
        },
        "packet_ref": {
            "path": "/retained/packet.json",
            "sha256": "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
        },
        "snapshot_revision": "r1",
        "semantic_cache_key": "closer-evidence-semantic/v1:fixture",
        "manifest": {"schema": "closer-evidence-packet/v1"},
        "section_refs": {
            "source": {"sha256": "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"}
        },
        "existing_disposition": {},
        "verification_recovery_progress": None,
        "nonpass_findings": [
            {
                "finding_id": "current:nonpass-1",
                "summary": "Retain raw NON_PASS — naïve Unicode",
                "evidence_refs": [
                    "sha256:ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"
                ],
            }
        ],
    },
    "source_rows": [
        {"target": "offline/repo#2", "body": "## Tasks\n- [ ] Preserve exact report output — café"},
        {"target": "offline/repo#2", "body": "## Tasks\n- [ ] Preserve exact report output — café"},
        {"target": "offline/repo#2", "body": "## Tasks\n- [ ] Preserve exact report output — café"},
        {"target": "offline/repo#2", "body": "## Tasks\n- [ ] Preserve exact report output — café"},
        {"target": "offline/repo#2", "body": "## Tasks\n- [ ] Preserve exact report output — café"},
        {"target": "offline/repo#2", "body": "## Tasks\n- [ ] Preserve exact report output — café"},
    ],
    "diff_rows": {"files": [{"filename": "report.py", "patch": '+print("✔")' * 1300}]},
    "review_rows": [
        {"id": 0, "body": "observed finding"},
        {"id": 1, "body": "observed finding"},
        {"id": 2, "body": "observed finding"},
        {"id": 3, "body": "observed finding"},
        {"id": 4, "body": "observed finding"},
        {"id": 5, "body": "observed finding"},
        {"id": 6, "body": "observed finding"},
        {"id": 7, "body": "observed finding"},
        {"id": 8, "body": "observed finding"},
        {"id": 9, "body": "observed finding"},
        {"id": 10, "body": "observed finding"},
        {"id": 11, "body": "observed finding"},
        {"id": 12, "body": "observed finding"},
        {"id": 13, "body": "observed finding"},
        {"id": 14, "body": "observed finding"},
    ],
    "artifact_rows": {
        "receipts": [
            {
                "run_id": 123,
                "run_attempt": 2,
                "head_sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            }
        ]
    },
    "readiness": {"ready": False, "gaps": ["ci_artifacts:pending"], "requirements": []},
}

_GOLDEN = {
    "0000": {
        "sha256": "a2dcaa65a17aa7e6a0e14b6f43f6d78503999d61429dde5d5cd40a9e946ccf45",
        "bytes": 17398,
    },
    "0001": {
        "sha256": "a2dcaa65a17aa7e6a0e14b6f43f6d78503999d61429dde5d5cd40a9e946ccf45",
        "bytes": 17398,
    },
    "0010": {
        "sha256": "afaab2450f9ffaa9691759e72412d636a46e4819b808140926a7674e32ee76f3",
        "bytes": 18158,
    },
    "0011": {
        "sha256": "82bec381bc4d38a4dd9071df76be5cb0713767fd6964fe62ac827360b022aa69",
        "bytes": 19201,
    },
    "0100": {
        "sha256": "46c11367fffe7c26799205c12c5002d9c786079f8de4f272609745ac5f87af00",
        "bytes": 17677,
    },
    "0101": {
        "sha256": "46c11367fffe7c26799205c12c5002d9c786079f8de4f272609745ac5f87af00",
        "bytes": 17677,
    },
    "0110": {
        "sha256": "cc959d093b13ed1aa018a76b1496b71ec403347ca22aac28417f50a9155562f9",
        "bytes": 18437,
    },
    "0111": {
        "sha256": "3e9bc7010568415e9d8997764c20157059354b211631b42df215f6a384e7a058",
        "bytes": 19480,
    },
    "1000": {
        "sha256": "a15344dd6c7fdd5324d72b6797da5e57244d4ec052aeaf94d42518c62d1ff2b5",
        "bytes": 17801,
    },
    "1001": {
        "sha256": "a15344dd6c7fdd5324d72b6797da5e57244d4ec052aeaf94d42518c62d1ff2b5",
        "bytes": 17801,
    },
    "1010": {
        "sha256": "678a95cce12f46eefc839d433d6fab947b736d1c4b4c43995b9130f621e52c51",
        "bytes": 18561,
    },
    "1011": {
        "sha256": "53e8abf11c42ef1a143a3f3ca7ad04fa4ef74dc5b5de1dba2ee54be91483775b",
        "bytes": 19604,
    },
    "1100": {
        "sha256": "2594b92347df4223c6b99d93f1b8f3ca0de424664b1a2a3303472a959c564d22",
        "bytes": 18080,
    },
    "1101": {
        "sha256": "2594b92347df4223c6b99d93f1b8f3ca0de424664b1a2a3303472a959c564d22",
        "bytes": 18080,
    },
    "1110": {
        "sha256": "da77398a17f8340fc19d7995cc9cfe4ff410c2f49593114708c0ccff398d2b74",
        "bytes": 18840,
    },
    "1111": {
        "sha256": "70dfdbf34768262b301ae62a813f3d305ec85f681ee22e2381b1a7d3fd1ef481",
        "bytes": 19883,
    },
}


def render_inputs():
    parameters = {
        **copy.deepcopy(_INPUTS),
        "collection_instructions": recovery.COLLECTION_INSTRUCTIONS,
    }
    expected = disposition._expected({"sources": parameters["source_rows"]})
    assert expected is not None
    # Synthetic renderer input only, with no retained proof or semantic answer.
    parameters["semantic_inventory"] = {
        "mode": "ordinary_acceptance",
        "host_residual_holds": [],
        "required_obligation_slots": list(range(1, len(expected) + 1)),
        "required_finding_slots": [],
        "obligations": [
            {"slot": n, "source": source, "text": text}
            for n, (source, text) in enumerate(expected, 1)
        ],
        "findings": [],
        "catalog": [],
    }
    return parameters


@pytest.mark.parametrize("flags", list(_GOLDEN))
def test_prospective_v2_renderer_branches_preserve_v3_golden_lineage(flags):
    assert contracts.CLASSIFICATION_PROMPT_VERSION == "closer-semantic-assessment-prompt/v2"
    records = json.loads(
        (
            Path(__file__).parent
            / "fixtures/semantic_api_reference/prospective_v2_renderer_golden.json"
        ).read_text()
    )
    record = records[flags]
    assert record["historical_v3"] == _GOLDEN[flags]
    run_drift, data_only, recovery_enabled, collection_mode = map(lambda value: value == "1", flags)
    parameters = {
        **render_inputs(),
        "run_drift": run_drift,
        "data_only": data_only,
        "recovery_enabled": recovery_enabled,
        "collection_mode": collection_mode,
    }
    if collection_mode:
        with pytest.raises(contracts.ClassificationPromptError, match=record["refusal"]):
            contracts.render_classification_prompt(**parameters)
    else:
        prompt = contracts.render_classification_prompt(**parameters)
        assert hashlib.sha256(prompt.encode()).hexdigest() == record["sha256"]
        assert len(prompt.encode()) == record["bytes"]
        assert "closer-semantic-assessment/v2" in prompt
        assert "Your advice grants no ACT" in prompt


@pytest.mark.parametrize(
    "document",
    [
        '{"target":"one","target":"two"}',
        '{"decision":{"criteria":[],"criteria":[{}]}}',
        '{"score":NaN}',
        '{"score":Infinity}',
        '{"score":-Infinity}',
        '{"score":1e999}',
        '{"target":"one"} trailing',
        "{}{}",
        "[{}]",
        '{"target":',
        "prefix {}",
        "```json\n{}\n```\nextra",
    ],
)
def test_shared_parser_matches_actual_production_strict_refusal(document):
    assert contracts.parse_low_proposal(document) == dispatcher._parse_low_proposal(document) == {}


@pytest.mark.parametrize("fence", [False, True])
def test_complete_real_response_shape_is_not_replaced_with_toy_grammar(tmp_path, fence):
    snap, packet, ref, prep, interpreted = fixture(tmp_path)
    output = json.dumps(interpreted["proposal"])
    if fence:
        output = "```json\n" + output + "\n```"
    parsed = contracts.parse_low_proposal(output)
    assert parsed == interpreted["proposal"]
    assert (
        disposition.integrate_ordinary(
            TARGET, snap, ref, prep, {**interpreted, "proposal": parsed}
        )["status"]
        == "accepted"
    )


@pytest.mark.parametrize("field", ["target", "head", "base"])
def test_stale_ids_parse_as_json_but_actual_retained_semantics_refuse(tmp_path, field):
    snap, packet, ref, prep, interpreted = fixture(tmp_path)
    interpreted["proposal"][field] = "stale"
    parsed = contracts.parse_low_proposal(json.dumps(interpreted["proposal"]))
    assert parsed[field] == "stale"  # Syntax-only is deliberately not authority.
    assert (
        disposition.integrate_ordinary(
            TARGET, snap, ref, prep, {**interpreted, "proposal": parsed}
        )["reason"]
        == "target_or_revision_changed"
    )


def test_actual_nonpass_accounting_keeps_every_finding_and_raw_verdict(tmp_path):
    snap, packet, ref, prep, interpreted = fixture(tmp_path)
    snap["verifier"] = {"verdict": "NON_PASS", "run_id": 34, "raw": "failed source verification"}
    findings = disposition.pending_nonpass_findings(snap, packet)
    proposal = interpreted["proposal"]
    proposal["decision"]["criteria"] = []
    proposal["resolutions"] = [
        {
            "finding_id": finding["finding_id"],
            "status": "unresolved_missing_evidence",
            "next_guarded_action": "collect_evidence",
            "reason": "retain missing original raw source evidence",
        }
        for finding in findings
    ]
    parsed = contracts.parse_low_proposal(json.dumps(proposal))
    result = disposition.integrate_ordinary(
        TARGET, snap, ref, prep, {**interpreted, "proposal": parsed}
    )
    assert result["status"] == "ordinary_nonpass"
    assert result["disposition"]["acceptance_authority"] is False
    assert result["disposition"]["raw_verdicts"][0]["raw"]["verdict"] == "NON_PASS"
    parsed["resolutions"][0]["finding_id"] = "stale-finding"
    result = disposition.integrate_ordinary(
        TARGET, snap, ref, prep, {**interpreted, "proposal": parsed}
    )
    assert result["status"] != "ordinary_nonpass"
    assert snap["verifier"]["verdict"] == "NON_PASS"


def test_actual_interpret_renders_complete_prompt_before_unqualified_refusal(tmp_path, monkeypatch):
    delivery = OfflineDelivery(tmp_path, monkeypatch)
    callbacks, context = callbacks_for(delivery, monkeypatch)
    render = contracts.render_classification_prompt
    captured = []

    def observed(mandatory, **kwargs):
        rendered = render(mandatory, **kwargs)
        captured.append((copy.deepcopy(mandatory), copy.deepcopy(kwargs), rendered))
        return rendered

    monkeypatch.setattr(contracts, "render_classification_prompt", observed)
    result = callbacks["interpret"](TARGET, context)
    assert result["classification"] == "assessment_transport_unavailable", result
    assert result["pending_kind"] == "external_event"
    assert len(captured) == 1 and delivery.provider_calls == []
    mandatory, kwargs, rendered = captured[0]
    assert rendered == render(mandatory, **kwargs)
    assert mandatory["target"] == TARGET
    assert mandatory["data_only_evidence"]["sections"]["source"]["content_supplied"] is True
    assert result["pending_evidence"]["target"] == TARGET
    assert result["authority"] is False


@pytest.mark.parametrize(
    "payload", ["x" * 70000, "界" * 21000], ids=["large-mandatory", "utf8-byte-overflow"]
)
def test_whole_prompt_overflow_refuses_before_profile_lease_or_offload(
    tmp_path, monkeypatch, payload
):
    delivery = OfflineDelivery(tmp_path, monkeypatch)
    delivery.backend.dispositions[TARGET] = {"retained_coordinator_note": payload}
    callbacks, context = callbacks_for(delivery, monkeypatch)
    release = dispatcher._release_route_load
    released = []

    def observe_release(route, load):
        release(route, load)
        released.append(dict(load))

    monkeypatch.setattr(dispatcher, "_release_route_load", observe_release)
    monkeypatch.setattr(
        dispatcher,
        "_execute_profile_route",
        lambda *_a, **_kw: pytest.fail("overflow must not allocate a provider profile"),
    )
    result = callbacks["interpret"](TARGET, context)
    assert result["status"] == "pending" and result["pending_kind"] == "external_event"
    assert result["classification"] == "evidence_pending"
    assert result["reason"] == "classification_prompt_exceeds_bound"
    assert result["authority"] is False and result["substantive_needs"] == []
    assert "classification_prompt_exceeds_bound" in result["pending_evidence"]["readiness"]["gaps"]
    assert not delivery.provider_calls and not delivery.guard_calls and not delivery.closure_calls
    assert released and all(value == 0 for value in released[-1].values())
    assert not result.get("producer_audit", {}).get("status") == "complete"


def test_whole_prompt_bound_counts_all_wrappers_and_preserves_mandatory():
    parameters = render_inputs()
    parameters["prompt_mandatory"]["oversize"] = "界" * 22000
    before = copy.deepcopy(parameters)
    with pytest.raises(
        contracts.ClassificationPromptError, match="classification_prompt_exceeds_bound"
    ):
        contracts.render_classification_prompt(**parameters)
    assert parameters == before


def test_descriptor_binds_actual_source_pin_and_separates_syntax_from_semantics():
    source_sha = hashlib.sha256(Path(contracts.__file__).read_bytes()).hexdigest()
    descriptor = contracts.classification_contract_descriptor(source_sha256=source_sha)
    assert descriptor["source_sha256"] == source_sha
    assert descriptor["prompt_version"] == "closer-semantic-assessment-prompt/v2"
    assert descriptor["max_prompt_bytes"] == 65536
    assert descriptor["response_syntax"]["identity_or_evidence_validation"] is False
    assert descriptor["semantic_validator"]["retained_io_required"] is True
    assert descriptor["response_fields"]["schema"] == "closer-semantic-assessment/v2"
    assert {"obligations", "findings", "global_concerns"} <= set(descriptor["response_fields"])
    assert (
        contracts.STRONG_CONTRACT_ENTRYPOINTS["response_validator"]
        == "closer_execution_contracts.validate_adjudication_response"
    )
    body = {key: value for key, value in descriptor.items() if key != "contract_sha256"}
    assert (
        descriptor["contract_sha256"]
        == hashlib.sha256(
            json.dumps(body, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        ).hexdigest()
    )


def test_renderer_parser_descriptor_have_no_ambient_io(monkeypatch):
    import builtins
    import socket
    import subprocess

    def forbidden(*_args, **_kwargs):
        pytest.fail("pure model contract used ambient I/O")

    parameters = render_inputs()
    monkeypatch.setattr(builtins, "open", forbidden)
    monkeypatch.setattr(Path, "read_bytes", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    assert contracts.render_classification_prompt(**parameters)
    assert contracts.parse_low_proposal('{"classification":"uncertain"}') == {
        "classification": "uncertain"
    }
    assert contracts.classification_contract_descriptor(source_sha256="a" * 64)


def test_existing_strong_contract_keeps_scoped_merge_and_global_uncertainty(strong_context):
    context = strong_context
    req = request(context)
    original = copy.deepcopy(context["original"])
    original["classification"] = "uncertain"
    original["substantive_needs"] = ["existing owner ambiguity remains"]
    original["raw_verdict"] = "NON_PASS"
    original["decision"]["criteria"][1]["result"] = "UNKNOWN"
    prompt = strong.build_adjudication_prompt(original, req, retained_dir=context["retained_dir"])
    assert len(prompt.encode()) <= contracts.STRONG_CONTRACT_ENTRYPOINTS["max_prompt_bytes"]
    response = contracts.parse_low_proposal(json.dumps(answer(context, req)))
    validated = strong.validate_adjudication_response(req, response, **inputs(context))
    merged = strong.merge_selected_response(original, req, validated)
    assert merged["decision"]["criteria"][0] == original["decision"]["criteria"][0]
    assert merged["decision"]["criteria"][1]["result"] == "PASS"
    assert (
        merged["classification"] == "uncertain"
        and merged["substantive_needs"] == original["substantive_needs"]
    )
    assert merged["raw_verdict"] == "NON_PASS"
    response["request_id"] = "stale"
    with pytest.raises(strong.ContractError, match="scope_changed"):
        strong.validate_adjudication_response(req, response, **inputs(context))


@pytest.mark.parametrize("payload", ["x" * 70000, "界" * 21000])
def test_overflow_pending_stops_actual_pipeline_before_exception_or_act(
    tmp_path, monkeypatch, payload
):
    runner = closer_pipeline.run_pipeline
    delivery = OfflineDelivery(tmp_path, monkeypatch)
    delivery.backend.dispositions[TARGET] = {"retained_coordinator_note": payload}
    callbacks, _context = callbacks_for(delivery, monkeypatch)
    monkeypatch.setattr(
        dispatcher,
        "_execute_profile_route",
        lambda *_a, **_kw: pytest.fail("overflow pipeline allocated a provider"),
    )
    monkeypatch.setattr(
        dispatcher,
        "_exception_route",
        lambda *_a, **_kw: pytest.fail("overflow pipeline escalated to strong worker"),
    )
    result = runner(tmp_path / "overflow.pipeline.json", [TARGET], **callbacks)
    row = result["targets"][TARGET]
    assert row["status"] == "pending" and row["stage"] == "interpret", row
    assert row["results"]["interpret"]["reason"] == "classification_prompt_exceeds_bound"
    assert "act" not in row["results"] and "exception" not in row["results"]
    assert not delivery.provider_calls and not delivery.guard_calls and not delivery.closure_calls
