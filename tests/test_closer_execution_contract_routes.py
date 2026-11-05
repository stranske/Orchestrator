"""Actual offline callbacks refuse generic work and revalidate scoped advice."""

import copy
import functools
import hashlib
import inspect
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from closer_inert_dispatcher_support import LOW, InertCaller
from test_closer_batch_interpretation import OfflineDelivery
from test_closer_disposition import TARGET
from test_closer_execution_contracts import (  # noqa: F401
    context as _context,
)
from test_closer_execution_contracts import (
    inputs,
    mechanical_plan,
)
from test_dispatcher_controlled_data_only import world as _controlled_world

import closer_dispatcher as adapter
import closer_disposition as disposition
import closer_evidence as evidence
import closer_execution_contracts as contracts
import closer_pipeline
import closer_resources as resources
import dispatcher

context = _context
controlled_world = _controlled_world
_REAL_LOW = adapter._pipeline_low_route
_REAL_EXCEPTION = adapter._exception_route
_REAL_CAPACITY = adapter._fresh_capacity_report
_INERT = None
_PREPARED = None
_REAL_PIPELINE = closer_pipeline.run_pipeline


@pytest.fixture(autouse=True)
def inert_transport(controlled_world, monkeypatch):
    """Only test-local roles/auth and the reviewed inert executable are enabled."""
    monkeypatch.setenv("HANDOFF_DIR", str(controlled_world.root / "handoff"))
    monkeypatch.setenv("HOME", str(controlled_world.root / "private-home"))
    caller = InertCaller(controlled_world, monkeypatch)
    monkeypatch.setattr(sys.modules[__name__], "_INERT", caller)
    monkeypatch.setenv("ORCH_STATE_DIR", str(controlled_world.root / "private-state"))
    monkeypatch.setenv("ORCH_BRAIN_ROOT", str(controlled_world.root / "private-brain"))
    return caller


_DECLARED_LOW = {}


def prepared(tmp_path, monkeypatch):
    assert _PREPARED is not None, "runtime contract must run inside the actual CLI-owned fixture"
    return _PREPARED


def configured_delivery(tmp_path, monkeypatch):
    delivery = OfflineDelivery(tmp_path, monkeypatch)
    caller = _INERT
    caller.activate()
    caller.seed_capacity()
    monkeypatch.setattr(adapter, "_pipeline_low_route", _REAL_LOW)
    monkeypatch.setattr(adapter, "_exception_route", _REAL_EXCEPTION)
    monkeypatch.setattr(adapter, "_fresh_capacity_report", _REAL_CAPACITY)
    monkeypatch.setattr(adapter, "_pipeline_capacity", lambda: {"io": 2, "low": 1, "exception": 1})
    delivery.backend.workspaces = {"owner/repo": str(tmp_path)}
    # Reuse the existing helper's actual collector and physical CI receipts.
    # Only the external collection source is supplied; all validators remain real.
    packet, _packet_ref, proposal = caller.packet(
        delivery.current, tmp_path / "queue.json.evidence"
    )
    delivery.proposal = proposal
    monkeypatch.setattr(evidence, "collect_evidence", lambda *_a, **_kw: packet)
    delivery.inert_caller = caller
    delivery.strong_response = lambda *_a: pytest.fail("unexpected strong invocation")

    def broker(agent, prompt, **options):
        if options["task_type"] == "closer_interpret":
            # Only payload text is reused, never the old fake transport/run.
            payload = delivery.offload("gemini", prompt, **options)["output"]
            control = {"exit": 0, "output": payload}
        else:
            envelope = json.loads(prompt.split("\n", 1)[1])
            control = delivery.strong_response(envelope, prompt, options)
        return caller.response(control, agent, prompt, **options)

    monkeypatch.setattr(dispatcher, "offload", broker)
    return delivery, caller


def native_callback_case(test):
    """Keep the existing actual CLI/backend registration and scope alive for test."""

    @functools.wraps(test)
    def run(*args, **kwargs):
        bound = inspect.signature(test).bind(*args, **kwargs)
        tmp_path, monkeypatch = bound.arguments["tmp_path"], bound.arguments["monkeypatch"]
        delivery, caller = configured_delivery(tmp_path, monkeypatch)
        completed = []

        def exercise(path, targets, **callbacks):
            # Existing helper wrappers own real scope leases. Their source
            # callback still closes over the unmodified CLI-constructed backend.
            source_prepare = callbacks["prepare"].__defaults__[0]
            actual_backend = dict(
                zip(
                    source_prepare.__code__.co_freevars,
                    (cell.cell_contents for cell in source_prepare.__closure__),
                )
            )["backend"]
            delivery.backend = actual_backend
            prep = callbacks["prepare"](TARGET, {"deadline": time.time() + 180})
            ctx = {
                "prepare": prep,
                "deadline": time.time() + 180,
                "revision": prep["revision"],
                "identity": prep["identity"],
                "work_key": "actual-inert-contract-low",
            }
            low = callbacks["interpret"](TARGET, ctx)
            assert low["integration_status"] == "accepted", low
            ctx["interpret"] = low
            # Keep the original pre-response fixture specification separately.
            # Named tests may declare a NEW response, never re-emit this actual.
            _DECLARED_LOW[id(low)] = (delivery, copy.deepcopy(delivery.proposal), ctx)
            delivery.frontier = callbacks["finite_frontier"]
            install_exception_readback(delivery, callbacks)
            monkeypatch.setattr(sys.modules[__name__], "_PREPARED", (delivery, callbacks, ctx))
            completed.append(test(*args, **kwargs))
            return {"targets": {TARGET: {"status": "contract_fixture_finished"}}}

        monkeypatch.setattr(closer_pipeline, "run_pipeline", exercise)
        caller.staged(
            SimpleNamespace(state=str(tmp_path / "queue.json"), max_workers=1, shadow=False),
            [TARGET],
            delivery.backend,
            time.time() + 180,
        )
        assert len(completed) == 1
        return completed[0]

    return run


def install_exception_readback(delivery, callbacks):
    # The production full_interpretation replays the original sealed typed
    # response. Named fixtures declare a new response explicitly in named().
    return None


def actual_worker(delivery, ctx, low, request):
    """Post-transport advisory-input boundary with genuine supervised strong output."""
    prep = ctx["prepare"]
    snap = json.loads(Path(prep["snapshot_ref"]["path"]).read_bytes())
    with resources.pool_slot(
        delivery.root / "strong-pools",
        hashlib.sha256(b"claude-subscription").hexdigest(),
        1,
        deadline=time.time() + 30,
        finite_frontier=True,
        reservation={
            "reservation_id": "actual-strong-contract",
            "target": TARGET,
            "frontier_id": delivery.frontier["frontier_id"],
            "agent": "claude",
            "profile_id": LOW,
        },
    ):
        return delivery.backend.worker(
            TARGET,
            "disposition",
            snap,
            {
                "id": "actual-strong-contract",
                "deadline": time.time() + 30,
                "finite_frontier": delivery.frontier,
            },
            profile_id=LOW,
            advisory_context={
                "adjudication_request": request,
                "packet_ref": prep["packet_ref"],
                "interpretation": low,
            },
        )


def named(low, *, global_explanations=()):
    delivery, declared, context = _DECLARED_LOW[id(low)]
    gold = copy.deepcopy(declared)
    gold["decision"]["criteria"][1]["result"] = "UNKNOWN"
    gold["substantive_needs"] = list(global_explanations)
    prep = context["prepare"]
    packet, _root = contracts.load_retained_packet(prep["packet_ref"])
    snap = json.loads(Path(prep["snapshot_ref"]["path"]).read_bytes())
    actual = delivery.inert_caller.interpretation(snap, packet, prep["packet_ref"], gold)
    low.update(actual)
    low["run_id"] = actual["actual"]["run_id"]
    low["classification"] = actual["proposal"]["classification"]
    low["substantive_needs"] = list(actual["proposal"]["substantive_needs"])
    assert low["proposal"]["semantic_followup"]["status"] == "candidate"
    return low["proposal"]["decision"]["criteria"][1]


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
    assert result["status"] == "uncertain", result
    assert result["authority"] is False
    assert result["reason"] == "original_live_repair_operation_UNMET"
    assert result.get("operation_id") is None
    # The direct worker call has no actual queue authority. Retain the separate
    # fine-grained plan/test-identity oracle without manufacturing that authority.
    pending = contracts.mechanical_execution_pending(plan, context["snapshot"])
    assert pending["reason"] in {
        "mechanical_operation_not_allowlisted",
        "registered_required_test_missing",
        "bounded_mechanical_executor_unqualified",
    }
    assert "operation_id" not in pending
    assert pending["pending_evidence"]["finding_id"] == context["finding"]["finding_id"]
    if kind in {"valid_but_no_executor", "symlink", "hardlink"}:
        assert pending["pending_evidence"]["required_test_identity"] == context["test"]
        assert "physical_confinement_verified" not in result


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
