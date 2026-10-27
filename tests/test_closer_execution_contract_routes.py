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
from closer_inert_dispatcher_support import LOW, REAL_GITHUB, InertCaller
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
    caller = delivery.inert_caller
    original = callbacks["exception"]

    def exception(target, context):
        current = context["interpret"]
        packet, root = contracts.load_retained_packet(context["prepare"]["packet_ref"])
        snap = json.loads(Path(context["prepare"]["snapshot_ref"]["path"]).read_bytes())
        # A changed proposed response gets its OWN real invocation and immutable
        # receipt. Never relabel or overwrite the original completed low run.
        actual = caller.interpretation(
            snap, packet, context["prepare"]["packet_ref"], copy.deepcopy(current["proposal"])
        )
        stage_needs = list(current.get("substantive_needs", []))
        current.update({key: value for key, value in actual.items() if key != "substantive_needs"})
        current["run_id"] = actual["actual"]["run_id"]
        current["classification"] = actual["proposal"].get("classification")
        current["substantive_needs"] = list(
            dict.fromkeys([*stage_needs, *actual["proposal"].get("substantive_needs", [])])
        )
        checked = disposition.integrate_ordinary(
            target, snap, context["prepare"]["packet_ref"], context["prepare"], actual
        )
        current["integration_status"] = checked["status"]
        return original(target, context)

    callbacks["exception"] = exception


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

    def strong(envelope, _prompt, _kwargs):
        req = envelope["adjudication_request"]
        calls.append(req)
        return {"exit": 0, "output": json.dumps(scoped_answer(req, row))}

    delivery.strong_response = strong
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

    def strong(envelope, _prompt, _kwargs):
        req = envelope["adjudication_request"]
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
        return {"exit": 0, "output": output}

    delivery.strong_response = strong
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

    def strong(envelope, prompt, kwargs):
        seen.append((prompt, kwargs))
        req = envelope["adjudication_request"]
        assert envelope["supplied_evidence"][0]["content"]
        return {"exit": 0, "output": json.dumps(scoped_answer(req, row))}

    delivery.strong_response = strong
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


def test_actual_pipeline_persists_generic_request_hold_before_action(tmp_path, monkeypatch):
    from test_closer_dispatcher_finite_frontier import command

    # Run the real scoped scheduler, not a nested coordinator under the helper's
    # existing live lease. Registration, ownership and journal guards stay real.
    delivery, caller = configured_delivery(tmp_path, monkeypatch)
    delivery.proposal["substantive_needs"] = ["Investigate everything necessary"]
    caller.temporary_campaign_guard()  # checked private pytest home only
    frontier = caller.frontier({TARGET: delivery.current}, delivery.backend)
    argv, _initial, _assignment = command(tmp_path, (TARGET,))
    argv[argv.index("--state") + 1] = str(tmp_path / "queue.json")
    argv[argv.index("--finite-frontier-json") + 1] = json.dumps(frontier)
    monkeypatch.setattr(adapter, "GitHub", REAL_GITHUB)
    monkeypatch.setattr(
        REAL_GITHUB, "snapshot", lambda self, target: copy.deepcopy(delivery.current)
    )
    monkeypatch.setattr(adapter, "known_workspaces", lambda targets: delivery.backend.workspaces)

    def read_only_github(args, **kwargs):
        assert args[:3] == ["api", "--method", "GET"], "no GitHub mutation may start"
        return 0, delivery.api(args[3])

    monkeypatch.setattr(adapter.gh_capacity, "gh_run", read_only_github)
    monkeypatch.setattr(adapter, "_exception_route", forbidden)
    monkeypatch.setattr(closer_pipeline, "run_pipeline", _REAL_PIPELINE)
    adapter.main(argv)
    ledger = adapter._pipeline_state_path(tmp_path / "queue.json")
    shard = resources.shard_dir(resources.scoped_root(ledger), TARGET) / "state.json"
    saved = json.loads(shard.read_text())
    row = saved["frontiers"][frontier["frontier_id"]]["items"][TARGET]
    assert row["status"] == "held" and row["stage"] == "exception", row
    pending = row["results"]["exception"]
    assert pending["status"] == "pending" and pending["pending_kind"] == "external_event"
    assert (
        pending["pending_evidence"]["contract_reason"] == "one_bound_adjudication_request_required"
    )
    assert pending["pending_evidence"]["target"] == TARGET
    assert pending["pending_evidence"]["revision"] == row["revision"]
    assert "act" not in row["results"]
    assert len(caller.results) == 1  # only the original genuine low invocation
    assert not delivery.guard_calls and not delivery.closure_calls


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

    if kind == "missing_run":
        # Pure negative at the untrusted worker-return seam: a real broker always
        # supplies run_id, so no actual completed invocation is fabricated here.
        def missing_run(*_a, **kwargs):
            req = kwargs["advisory_context"]["adjudication_request"]
            return {
                "status": "completed",
                "worker": {"exit": 0, "output": json.dumps(scoped_answer(req, row))},
            }

        monkeypatch.setattr(delivery.backend, "worker", missing_run)
    else:

        def strong(envelope, _prompt, _kwargs):
            return {
                "exit": 130 if kind == "cancelled" else 124,
                "outcome_uncertain": kind == "unknown",
                "output": json.dumps(scoped_answer(envelope["adjudication_request"], row)),
            }

        delivery.strong_response = strong
    result = cb["exception"](TARGET, ctx)
    assert result["authority"] is False
    assert result.get("validated", {}).get("status") != "accepted"
    if kind == "missing_run":
        assert result["validated"]["reason"] == "worker_execution_unverified"
    elif kind == "cancelled":
        assert delivery.inert_caller.results[-1]["raw_exit"] == 130
        assert result["status"] == "uncertain"
    elif kind == "unknown":
        assert result["status"] == "uncertain"
        assert result["provider_outcome"]["actual"]["retry_safe"] is False
        assert (
            result["provider_outcome"]["actual"]["run_id"]
            == delivery.inert_caller.results[-1]["run_id"]
        )


@pytest.mark.parametrize(
    "padding", ["x" * 100000, "😀" * 20000, "\x01" * 12000], ids=["ascii", "unicode", "escaped"]
)
def test_post_transport_combined_prompt_bound_precedes_any_strong_allocation(
    tmp_path, monkeypatch, padding
):
    delivery, cb, ctx = prepared(tmp_path, monkeypatch)
    row = named(ctx["interpret"])
    row["proofs"][0]["relevance"] = padding
    # Deliberately oversized coordinator input cannot be claimed as an admitted
    # bounded native final. Test the real post-transport worker contract gate.
    prep = ctx["prepare"]
    packet, root = contracts.load_retained_packet(prep["packet_ref"])
    snap = json.loads(Path(prep["snapshot_ref"]["path"]).read_bytes())
    explicit = ctx["interpret"]["proposal"]["adjudication_request"]
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
    result = delivery.backend.worker(
        TARGET,
        "disposition",
        snap,
        {"deadline": time.time() + 30},
        profile_id=LOW,
        advisory_context={
            "adjudication_request": req,
            "packet_ref": prep["packet_ref"],
            "interpretation": ctx["interpret"]["proposal"],
        },
    )
    assert result["status"] == "pending"
    assert result["reason"] == "adjudication_prompt_exceeds_bound"
    identity = result["pending_evidence"]["adjudication_request"]
    assert identity["target"] == TARGET and identity["revision"] == ctx["revision"]
    assert identity["basis"]["source"] == row["source"]
    assert identity["request_id"]
    delivery.proposal = copy.deepcopy(ctx["interpret"]["proposal"])
    with monkeypatch.context() as no_strong:
        no_strong.setattr(delivery.backend, "worker", forbidden)
        held = cb["interpret"](TARGET, {**ctx, "work_key": "oversized-native-low"})
    assert held["status"] == "uncertain" and held["authority"] is False
    assert held["provider_outcome"]["actual"]["retry_safe"] is False


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


def test_post_transport_unknown_fields_never_reach_strong_prompt(tmp_path, monkeypatch):
    delivery, cb, ctx = prepared(tmp_path, monkeypatch)
    row = named(ctx["interpret"])
    row["unexpected_project_context"] = "forbidden-padding" * 10000
    row["proofs"][0]["unknown_tool_context"] = "forbidden-padding" * 10000
    row["evidence"][0]["unknown_hook_context"] = "forbidden-padding" * 10000
    seen = []

    def strong(envelope, prompt, _kwargs):
        seen.append(prompt)
        assert "forbidden-padding" not in prompt
        assert len(prompt.encode("utf-8")) <= contracts.MAX_ADJUDICATION_PROMPT_BYTES
        selected = envelope["selected_row"]
        assert set(selected) == {"source", "criterion", "result", "evidence", "proofs"}
        assert set(selected["evidence"][0]) == {"path", "sha256", "bytes"}
        return {
            "exit": 0,
            "output": json.dumps(scoped_answer(envelope["adjudication_request"], selected)),
        }

    delivery.strong_response = strong
    prep = ctx["prepare"]
    packet, root = contracts.load_retained_packet(prep["packet_ref"])
    snap = json.loads(Path(prep["snapshot_ref"]["path"]).read_bytes())
    explicit = ctx["interpret"]["proposal"]["adjudication_request"]
    req = contracts.build_adjudication_request(
        snap,
        packet,
        prep["packet_ref"],
        basis=explicit["basis"],
        question=explicit["question"],
        evidence_refs=explicit["evidence_refs"],
        retained_dir=root,
    )
    result = actual_worker(delivery, ctx, ctx["interpret"]["proposal"], req)
    assert len(seen) == 1
    assert result["worker"]["exit"] == 0, result
    response = adapter._worker_proposal(result["worker"], agent="claude")
    answer = contracts.validate_adjudication_response(
        req,
        response,
        snapshot=snap,
        packet=packet,
        packet_ref=prep["packet_ref"],
        retained_dir=root,
    )
    merged = contracts.merge_selected_response(ctx["interpret"]["proposal"], req, answer)
    checked = disposition.integrate_ordinary(
        TARGET,
        snap,
        prep["packet_ref"],
        prep,
        {"actual": adapter._worker_actual(result["worker"]), "proposal": merged},
    )
    assert checked["status"] == "accepted", checked
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


# All runtime cases execute within InertCaller.staged's actual CLI-owned backend
# frame and held scope. Pure disposition/mechanical tests above need no wrapper.
for _name in (
    "test_actual_exception_admission_refuses_before_route_or_worker",
    "test_actual_named_advice_merges_one_row_then_full_guard",
    "test_actual_strong_response_rejects_rebinding_and_broad_rewrites",
    "test_actual_worker_strong_prompt_is_one_data_only_question",
    "test_scoped_worker_interruption_never_gains_completion_authority",
    "test_post_transport_combined_prompt_bound_precedes_any_strong_allocation",
    "test_actual_worker_rechecks_combined_prompt_bound_before_its_route",
    "test_post_transport_unknown_fields_never_reach_strong_prompt",
    "test_owner_required_hold_retains_only_actually_bound_named_identity",
):
    globals()[_name] = native_callback_case(globals()[_name])
