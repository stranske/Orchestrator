"""Actual offline callbacks refuse generic work and revalidate scoped advice."""

import copy
import json
import os
import time

import pytest
from test_closer_batch_interpretation import OfflineDelivery, callbacks_for, install_strong_capacity
from test_closer_disposition import TARGET
from test_closer_execution_contracts import (  # noqa: F401
    context as _context,
)
from test_closer_execution_contracts import (
    inputs,
    mechanical_plan,
)

import closer_dispatcher as adapter
import closer_disposition as disposition
import closer_execution_contracts as contracts

context = _context


def prepared(tmp_path, monkeypatch):
    delivery = OfflineDelivery(tmp_path, monkeypatch)
    install_strong_capacity(monkeypatch)
    callbacks, ctx = callbacks_for(delivery, monkeypatch)
    low = callbacks["interpret"](TARGET, ctx)
    assert low["integration_status"] == "accepted", low
    ctx["interpret"] = low
    return delivery, callbacks, ctx


def named(low):
    row = low["proposal"]["decision"]["criteria"][1]
    low["proposal"]["adjudication_request"] = {
        "basis": {"source": row["source"], "criterion": row["criterion"]},
        "question": "Does this retained receipt prove the named report criterion?",
        "evidence_refs": row["evidence"],
    }
    return row


def scoped_answer(req, row):
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
        "outcome": "satisfied",
        "reason": "The supplied current-head receipt exercises the report.",
        "evidence": row["evidence"],
        "proofs": row["proofs"],
    }


def forbidden(*_a, **_kw):
    pytest.fail("no route, worker or writing call is allowed")


@pytest.mark.parametrize(
    "kind", ["missing", "generic", "multiple", "unknown", "missing_row", "security", "authority"]
)
def test_actual_exception_admission_refuses_before_route_or_worker(tmp_path, monkeypatch, kind):
    delivery, cb, ctx = prepared(tmp_path, monkeypatch)
    low = ctx["interpret"]
    row = named(low)
    explicit = low["proposal"]["adjudication_request"]
    if kind == "missing":
        low["proposal"].pop("adjudication_request")
    elif kind == "generic":
        explicit["basis"] = {"substantive_needs": ["Investigate everything necessary"]}
    elif kind == "multiple":
        explicit["question"] = "Does this work? Should we rewrite everything?"
    elif kind == "unknown":
        explicit["basis"] = {"source": row["source"], "criterion": "invented requirement"}
    elif kind == "missing_row":
        low["proposal"]["decision"]["criteria"] = []
    elif kind == "security":
        low["proposal"]["findings"] = [
            {"category": "security", "summary": "needs qualified security owner"}
        ]
    else:
        low["proposal"]["findings"] = [{"authority_conflict": True}]
    low["substantive_needs"] = ["a disputed finding"]
    monkeypatch.setattr(adapter, "_exception_route", forbidden)
    monkeypatch.setattr(delivery.backend, "worker", forbidden)
    result = cb["exception"](TARGET, ctx)
    assert result["status"] == "pending", result
    assert result["pending_kind"] == "external_event"
    assert result["pending_evidence"]["target"] == TARGET
    assert result["pending_evidence"]["revision"] == ctx["revision"]
    assert result["pending_evidence"]["event_id"]
    assert result["next_trigger"]
    assert result["authority"] is False
    assert result["requires_coordinator"] is (kind in {"security", "authority"})


@pytest.mark.parametrize("uncertain", [False, True])
def test_actual_named_advice_merges_one_row_then_full_guard(tmp_path, monkeypatch, uncertain):
    delivery, cb, ctx = prepared(tmp_path, monkeypatch)
    low = ctx["interpret"]
    row = named(low)
    row["result"] = "UNKNOWN"
    if uncertain:
        low["proposal"].update(
            classification="uncertain", substantive_needs=["original global ambiguity"]
        )
    original = copy.deepcopy(low["proposal"])
    calls, integrations = [], []
    real_integrate = disposition.integrate_ordinary

    def integrate(*args):
        integrations.append(copy.deepcopy(args[-1]["proposal"]))
        return real_integrate(*args)

    monkeypatch.setattr(disposition, "integrate_ordinary", integrate)

    def worker(*_a, **kwargs):
        req = kwargs["advisory_context"]["adjudication_request"]
        calls.append(req)
        return {
            "status": "completed",
            "worker": {
                "exit": 0,
                "run_id": "offline-scoped-answer",
                "output": json.dumps(scoped_answer(req, row)),
            },
        }

    monkeypatch.setattr(delivery.backend, "worker", worker)
    result = cb["exception"](TARGET, ctx)
    assert len(calls) == 1
    assert result["validated"]["scoped_adjudication"]["outcome"] == "satisfied"
    assert result["validated"]["status"] == ("exception" if uncertain else "accepted"), result
    merged = integrations[-1]
    assert merged["decision"]["criteria"][1]["result"] == "PASS"
    assert merged["decision"]["criteria"][0] == original["decision"]["criteria"][0]
    assert merged["classification"] == original["classification"]
    assert merged["substantive_needs"] == original["substantive_needs"]
    assert merged["findings"] == original["findings"]
    assert low["proposal"] == original
    # Re-entry re-reads the exact native scoped response and repeats full integration.
    if not uncertain:
        ctx["exception"] = result
        ctx["action_state_path"] = str(tmp_path / "action-queue.json")
        # Action backend is reached through actual act callback. Remote effect
        # endpoints remain fixture doubles and must not contact a provider.
        before = len(integrations)
        cb["act"](TARGET, ctx)
        assert len(integrations) >= before + 2
        assert integrations[-1]["decision"]["criteria"][1]["result"] == "PASS"


@pytest.mark.parametrize(
    "mutation", ["head", "request_id", "packet", "basis", "rewrite", "broad_pass", "duplicate_json"]
)
def test_actual_strong_response_rejects_rebinding_and_broad_rewrites(
    tmp_path, monkeypatch, mutation
):
    delivery, cb, ctx = prepared(tmp_path, monkeypatch)
    row = named(ctx["interpret"])

    def worker(*_a, **kwargs):
        req = kwargs["advisory_context"]["adjudication_request"]
        answer = scoped_answer(req, row)
        if mutation == "head":
            answer["head"] = "f" * 40
        elif mutation == "request_id":
            answer["request_id"] = "f" * 64
        elif mutation == "packet":
            answer["packet_sha256"] = "f" * 64
        elif mutation == "basis":
            answer["basis"] = {"finding_id": "invented"}
        elif mutation == "rewrite":
            answer["decision"] = {"criteria": []}
        elif mutation == "broad_pass":
            answer["classification"] = "relevant"
            answer["substantive_needs"] = []
        output = json.dumps(answer)
        if mutation == "duplicate_json":
            output = output[:-1] + ',"outcome":"satisfied"}'
        return {
            "status": "completed",
            "worker": {"exit": 0, "run_id": "offline-refused-answer", "output": output},
        }

    monkeypatch.setattr(delivery.backend, "worker", worker)
    result = cb["exception"](TARGET, ctx)
    assert result["validated"]["status"] in {"pending", "wait"}, result
    if mutation != "duplicate_json":
        assert result["status"] == "pending"  # actual queue admission, not nested metadata
        assert result["pending_evidence"]["event_id"]
    assert result["authority"] is False


def test_actual_worker_strong_prompt_is_one_data_only_question(tmp_path, monkeypatch):
    delivery, cb, ctx = prepared(tmp_path, monkeypatch)
    row = named(ctx["interpret"])
    seen = []
    monkeypatch.setattr(
        adapter,
        "_validated_pipeline_exception_route",
        lambda *_a: {"agent": "codex", "profile_id": "offline-strong", "mode": "assess"},
    )
    delivery.backend.workspaces = {"owner/repo": str(tmp_path)}

    def offload(agent, prompt, **kwargs):
        seen.append((prompt, kwargs))
        envelope = json.loads(prompt.split("\n", 1)[1])
        req = envelope["adjudication_request"]
        assert envelope["supplied_evidence"][0]["content"]
        return {
            "exit": 0,
            "run_id": "offline-real-worker",
            "output": json.dumps(scoped_answer(req, row)),
        }

    delivery.backend.offload_fn = offload
    result = cb["exception"](TARGET, ctx)
    assert result["validated"]["status"] == "accepted", result
    assert len(seen) == 1
    prompt, kwargs = seen[0]
    assert "Answer ONLY the one named disputed question" in prompt
    assert "complete source and Tasks/AC coverage" not in prompt
    assert "Report workspace git HEAD" not in prompt
    assert kwargs["data_only"] is True
    assert kwargs["isolate"] is True
    assert kwargs["max_network_retries"] == 0


def test_nonpass_mechanical_disposition_collects_missing_test_without_rewriting_native(context):
    snap = context["snapshot"]
    original_verifier = copy.deepcopy(snap["verifier"])
    proposal = copy.deepcopy(context["original"])
    proposal["resolutions"] = [
        {
            "finding_id": context["finding"]["finding_id"],
            "status": "unresolved_mechanical",
            "next_guarded_action": "repair",
            "reason": "format only",
            "evidence": [],
            "proofs": [],
            "repair_plan": mechanical_plan(context),
        }
    ]
    prep = {
        "revision": snap["revision"],
        "producer_sha256": context["packet"]["producer_sha256"],
        "evidence_summary": {"section_digests": context["packet"]["section_digests"]},
    }
    result = disposition.integrate_ordinary(
        snap["target"],
        snap,
        context["packet_ref"],
        prep,
        {"actual": {"exit": 0, "run_id": "offline-disposition"}, "proposal": proposal},
    )
    assert result["status"] == "pending", result
    assert result["reason"] == "registered_required_test_missing"
    assert result["pending_evidence"]["finding_id"] == context["finding"]["finding_id"]
    assert result["next_guarded_action"] == "collect_evidence"
    assert snap["verifier"] == original_verifier
    assert result["raw_findings"][0]["raw"]["verdict"] == "NON_PASS"


@pytest.mark.parametrize(
    "kind",
    ["substantive", "no_operation", "no_test", "valid_but_no_executor", "symlink", "hardlink"],
)
def test_actual_worker_never_allocates_generic_repair_writer(context, tmp_path, monkeypatch, kind):
    plan = mechanical_plan(context)
    if kind == "substantive":
        plan["operation"] = "rewrite_algorithm"
    elif kind == "no_operation":
        plan.pop("operation")
    elif kind == "no_test":
        plan.pop("required_test")
    elif kind in {"symlink", "hardlink"}:
        path = context["workspace"] / "report.py"
        saved = context["workspace"] / "retained.py"
        path.rename(saved)
        if kind == "symlink":
            path.symlink_to(saved)
        else:
            os.link(saved, path)
    if kind not in {"no_test", "substantive", "no_operation"}:
        monkeypatch.setitem(
            contracts.REGISTERED_REQUIRED_TESTS, context["test"]["test_id"], context["test"]
        )
    monkeypatch.setattr(adapter, "_exception_route", forbidden)
    backend = adapter.Closer(
        object(),
        state_path=tmp_path / "queue.json",
        workers=True,
        workspaces={"owner/repo": str(context["workspace"])},
        dispositions={TARGET: {"repair": plan}},
        offload_fn=forbidden,
    )
    result = backend.worker(TARGET, "repair", context["snapshot"], {"deadline": time.time() + 60})
    assert result["status"] == "pending", result
    assert result["authority"] is False
    assert result["reason"] in {
        "mechanical_operation_not_allowlisted",
        "registered_required_test_missing",
        "bounded_mechanical_executor_unqualified",
    }
    assert "operation_id" not in result
    assert result["pending_evidence"]["finding_id"] == context["finding"]["finding_id"]
    if kind in {"valid_but_no_executor", "symlink", "hardlink"}:
        assert result["pending_evidence"]["required_test_identity"] == context["test"]
        assert "physical_confinement_verified" not in result


def test_actual_pipeline_persists_generic_request_hold_before_action(tmp_path, monkeypatch):
    import closer_pipeline

    real_pipeline = closer_pipeline.run_pipeline
    delivery, cb, _ctx = prepared(tmp_path, monkeypatch)
    original_interpret = cb["interpret"]

    def ambiguous(target, ctx):
        low = original_interpret(target, ctx)
        low["substantive_needs"] = ["Investigate everything necessary"]
        low["proposal"]["substantive_needs"] = low["substantive_needs"]
        return low

    cb["interpret"] = ambiguous
    cb["act"] = forbidden
    cb["deadline"] = time.time() + 30
    monkeypatch.setattr(adapter, "_exception_route", forbidden)
    monkeypatch.setattr(delivery.backend, "worker", forbidden)
    path = tmp_path / "bounded-contract-queue.json"
    receipt = real_pipeline(path, [TARGET], **cb)
    item = receipt["targets"][TARGET]
    assert item["status"] == "pending", receipt
    assert item["pending_kind"] == "external_event"
    saved = json.loads(path.read_text())
    run = next(iter(saved["runs"].values()))
    row = run["targets"][TARGET]
    assert row["pending_evidence"]["contract_reason"] == "one_bound_adjudication_request_required"
    assert row["pending_evidence"]["target"] == TARGET
    assert row["pending_evidence"]["revision"] == row["revision"]
    assert "act" not in row["results"]
    assert "exception" in row["results"]


def test_scoped_finding_advice_full_guard_preserves_native_nonpass(context):
    from test_closer_execution_contracts import answer, request

    req = request(context, finding=True)
    snap = context["snapshot"]
    verifier = copy.deepcopy(snap["verifier"])
    original = copy.deepcopy(context["original"])
    original["decision"]["criteria"] = []
    original["resolutions"] = [
        {
            "finding_id": req["basis"]["finding_id"],
            "status": "unresolved_missing_evidence",
            "reason": "needs supplied proof",
            "next_guarded_action": "collect_evidence",
            "evidence": [],
            "proofs": [],
        }
    ]
    validated = contracts.validate_adjudication_response(
        req, answer(context, req), **inputs(context)
    )
    merged = contracts.merge_selected_response(original, req, validated)
    prep = {
        "revision": snap["revision"],
        "producer_sha256": context["packet"]["producer_sha256"],
        "evidence_summary": {"section_digests": context["packet"]["section_digests"]},
    }
    integration = disposition.integrate_ordinary(
        TARGET,
        snap,
        context["packet_ref"],
        prep,
        {"actual": {"exit": 0, "run_id": "offline-scoped-finding"}, "proposal": merged},
    )
    assert integration["status"] == "ordinary_nonpass", integration
    assert integration["disposition"]["acceptance_authority"] is False
    assert integration["disposition"]["raw_verdicts"][0]["raw"]["verdict"] == "NON_PASS"
    assert snap["verifier"] == verifier
    assert original["resolutions"][0]["status"] == "unresolved_missing_evidence"


@pytest.mark.parametrize("kind", ["missing_run", "cancelled", "unknown"])
def test_scoped_worker_interruption_never_gains_completion_authority(tmp_path, monkeypatch, kind):
    delivery, cb, ctx = prepared(tmp_path, monkeypatch)
    row = named(ctx["interpret"])

    def worker(*_a, **kwargs):
        req = kwargs["advisory_context"]["adjudication_request"]
        native = {
            "exit": 0,
            "run_id": "offline-scoped-uncertain",
            "output": json.dumps(scoped_answer(req, row)),
        }
        if kind == "missing_run":
            native.pop("run_id")
        elif kind == "cancelled":
            native.update(exit=130, process_started=True)
        else:
            native.update(
                exit=124, outcome_uncertain=True, retry_safe=False, outcome_status="uncertain"
            )
        return {"status": "failed" if kind != "missing_run" else "completed", "worker": native}

    monkeypatch.setattr(delivery.backend, "worker", worker)
    result = cb["exception"](TARGET, ctx)
    assert result["authority"] is False
    assert result.get("validated", {}).get("status") != "accepted"
    if kind == "missing_run":
        assert result["validated"]["reason"] == "worker_execution_unverified"
    elif kind == "unknown":
        assert result["status"] == "uncertain"
        assert result["provider_outcome"]["actual"]["retry_safe"] is False
        assert result["provider_outcome"]["actual"]["run_id"] == "offline-scoped-uncertain"


@pytest.mark.parametrize("padding", ["x" * 100000, "😀" * 20000, "\x01" * 12000])
def test_combined_serialized_prompt_bound_precedes_any_strong_allocation(
    tmp_path, monkeypatch, padding
):
    delivery, cb, ctx = prepared(tmp_path, monkeypatch)
    row = named(ctx["interpret"])
    row["proofs"][0]["relevance"] = padding
    monkeypatch.setattr(adapter, "_exception_route", forbidden)
    monkeypatch.setattr(delivery.backend, "worker", forbidden)
    result = cb["exception"](TARGET, ctx)
    assert result["status"] == "pending"
    assert result["reason"] == "adjudication_prompt_exceeds_bound"
    identity = result["pending_evidence"]["adjudication_request"]
    assert identity["target"] == TARGET and identity["revision"] == ctx["revision"]
    assert identity["basis"]["source"] == row["source"]
    assert identity["request_id"]


def test_actual_worker_rechecks_combined_prompt_bound_before_its_route(tmp_path, monkeypatch):
    from pathlib import Path

    delivery, _cb, ctx = prepared(tmp_path, monkeypatch)
    low = ctx["interpret"]["proposal"]
    row = named(ctx["interpret"])
    row["proofs"][0]["relevance"] = "x" * 100000
    prep = ctx["prepare"]
    packet, root = contracts.load_retained_packet(prep["packet_ref"])
    snap = json.loads(Path(prep["snapshot_ref"]["path"]).read_bytes())
    snap["base"] = packet["base"]
    explicit = low["adjudication_request"]
    req = contracts.build_adjudication_request(
        snap,
        packet,
        prep["packet_ref"],
        basis=explicit["basis"],
        question=explicit["question"],
        evidence_refs=explicit["evidence_refs"],
        retained_dir=root,
    )
    monkeypatch.setattr(adapter, "_exception_route", forbidden)
    monkeypatch.setattr(adapter, "_validated_pipeline_exception_route", forbidden)
    delivery.backend.offload_fn = forbidden
    result = delivery.backend.worker(
        TARGET,
        "disposition",
        snap,
        {"deadline": time.time() + 60},
        profile_id="offline-strong",
        advisory_context={
            "adjudication_request": req,
            "packet_ref": prep["packet_ref"],
            "interpretation": low,
        },
    )
    assert result["status"] == "pending"
    assert result["reason"] == "adjudication_prompt_exceeds_bound"


def test_unknown_selected_row_and_proof_fields_never_reach_strong_prompt(tmp_path, monkeypatch):
    delivery, cb, ctx = prepared(tmp_path, monkeypatch)
    row = named(ctx["interpret"])
    row["unexpected_project_context"] = "forbidden-padding" * 10000
    row["proofs"][0]["unknown_tool_context"] = "forbidden-padding" * 10000
    row["evidence"][0]["unknown_hook_context"] = "forbidden-padding" * 10000
    seen = []
    monkeypatch.setattr(
        adapter,
        "_validated_pipeline_exception_route",
        lambda *_a: {"agent": "codex", "profile_id": "offline-strong", "mode": "assess"},
    )
    delivery.backend.workspaces = {"owner/repo": str(tmp_path)}

    def offload(_agent, prompt, **_kwargs):
        seen.append(prompt)
        assert "forbidden-padding" not in prompt
        assert len(prompt.encode("utf-8")) <= contracts.MAX_ADJUDICATION_PROMPT_BYTES
        envelope = json.loads(prompt.split("\n", 1)[1])
        selected = envelope["selected_row"]
        assert set(selected) == {"source", "criterion", "result", "evidence", "proofs"}
        assert set(selected["evidence"][0]) == {"path", "sha256", "bytes"}
        return {
            "exit": 0,
            "run_id": "offline-projected-answer",
            "output": json.dumps(scoped_answer(envelope["adjudication_request"], selected)),
        }

    delivery.backend.offload_fn = offload
    result = cb["exception"](TARGET, ctx)
    assert len(seen) == 1
    assert result["validated"]["status"] == "accepted", result
    assert "unexpected_project_context" in row  # Original inventory was not rewritten.


@pytest.mark.parametrize("bound", [True, False])
def test_owner_required_hold_retains_only_actually_bound_named_identity(
    tmp_path, monkeypatch, bound
):
    delivery, cb, ctx = prepared(tmp_path, monkeypatch)
    row = named(ctx["interpret"])
    ctx["interpret"]["proposal"]["findings"] = [
        {"category": "security", "summary": "qualified owner required"}
    ]
    if not bound:
        ctx["interpret"]["proposal"]["adjudication_request"]["basis"] = {"finding_id": "invented"}
    monkeypatch.setattr(adapter, "_exception_route", forbidden)
    monkeypatch.setattr(delivery.backend, "worker", forbidden)
    result = cb["exception"](TARGET, ctx)
    assert result["status"] == "pending" and result["requires_coordinator"] is True
    assert result["source_owners"] == [row["source"]]
    assert result["authority"] is False
    identity = result["pending_evidence"].get("adjudication_request")
    if bound:
        assert (
            identity["target"] == TARGET and identity["head"] == ctx["prepare"]["snapshot"]["head"]
        )
        assert identity["basis"]["source"] == row["source"]
        assert (
            identity["basis"]["criterion_sha256"]
            == contracts.hashlib.sha256(row["criterion"].encode()).hexdigest()
        )
        assert (
            identity["request_id"]
            and identity["packet_sha256"] == ctx["prepare"]["packet_ref"]["sha256"]
        )
    else:
        assert identity is None
