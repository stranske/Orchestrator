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

# Recorded from unmodified ccc6b951 before extraction. Keys are the flags
# run_drift/data_only/recovery_enabled/collection_mode in that order. The large
# Unicode optional payload exercises the unchanged 9,000-character projection.
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
        "sha256": "995cdf67b53299785c6e4ff6ce0b057085699be95903dcef1ce9f2e6920746bb",
        "bytes": 17380,
    },
    "0001": {
        "sha256": "995cdf67b53299785c6e4ff6ce0b057085699be95903dcef1ce9f2e6920746bb",
        "bytes": 17380,
    },
    "0010": {
        "sha256": "ff33bf7bed40818232c850c4a25ae9330364c54d1a3f16d482fb227204eea859",
        "bytes": 18140,
    },
    "0011": {
        "sha256": "93d3e8febec1090b38a59f0e6c6febdf9e71ea34fb957753feb4a11136856a8d",
        "bytes": 19183,
    },
    "0100": {
        "sha256": "85eedf556512b0760a7437142c839661f515faac2bbe671f4413ef57870e6ab2",
        "bytes": 17659,
    },
    "0101": {
        "sha256": "85eedf556512b0760a7437142c839661f515faac2bbe671f4413ef57870e6ab2",
        "bytes": 17659,
    },
    "0110": {
        "sha256": "0aeca7b96111549dca8da910357b8b4f888baa0fdc63183519bb5c556aaae18d",
        "bytes": 18419,
    },
    "0111": {
        "sha256": "0f9e8a21bf137d9fbe686d7e2eb0f42e7950db8960e42346bd24b4f3cf8ea9f0",
        "bytes": 19462,
    },
    "1000": {
        "sha256": "95659bcbada273e2d765bc7c9429136a7680498fdc147fb82d70eb4ffbd27148",
        "bytes": 17783,
    },
    "1001": {
        "sha256": "95659bcbada273e2d765bc7c9429136a7680498fdc147fb82d70eb4ffbd27148",
        "bytes": 17783,
    },
    "1010": {
        "sha256": "702daf220654dce893c7b1393396976c4ee838e9350a999d07cb7531c5552b03",
        "bytes": 18543,
    },
    "1011": {
        "sha256": "7baf33b81d2bed31c28b3ed480d992426993a2b1d7322498bdc046ce83a17754",
        "bytes": 19586,
    },
    "1100": {
        "sha256": "0018d58ff8301d10031fe1e5249e369486f3eb431e8b2b0b415b708affc28ef2",
        "bytes": 18062,
    },
    "1101": {
        "sha256": "0018d58ff8301d10031fe1e5249e369486f3eb431e8b2b0b415b708affc28ef2",
        "bytes": 18062,
    },
    "1110": {
        "sha256": "f3e6b683eea2e67699349f0206f88d7dc41be672b87b1c901bf40313fb9436d4",
        "bytes": 18822,
    },
    "1111": {
        "sha256": "fd1986897c0785ea93f8f67ce1e11f96ad06dfc5fd1ba8b9fb7e8992e29b8524",
        "bytes": 19865,
    },
}


def render_inputs():
    return {**copy.deepcopy(_INPUTS), "collection_instructions": recovery.COLLECTION_INSTRUCTIONS}


@pytest.mark.parametrize("flags", list(_GOLDEN))
def test_all_original_prompt_branches_are_byte_identical(flags):
    run_drift, data_only, recovery_enabled, collection_mode = map(lambda value: value == "1", flags)
    prompt = contracts.render_classification_prompt(
        **render_inputs(),
        run_drift=run_drift,
        data_only=data_only,
        recovery_enabled=recovery_enabled,
        collection_mode=collection_mode,
    )
    assert hashlib.sha256(prompt.encode()).hexdigest() == _GOLDEN[flags]["sha256"]
    assert len(prompt.encode()) == _GOLDEN[flags]["bytes"]
    assert contracts.NONPASS_ACCOUNTING_CONTRACT in prompt


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
    assert descriptor["prompt_version"] == "closer-classification-prompt/v2"
    assert descriptor["max_prompt_bytes"] == 65536
    assert descriptor["response_syntax"]["identity_or_evidence_validation"] is False
    assert descriptor["semantic_validator"]["retained_io_required"] is True
    assert "decision.criteria" in descriptor["response_fields"]
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
