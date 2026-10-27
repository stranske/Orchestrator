"""Offline batch diagnostics: providers and remote APIs are simulated explicitly."""

from __future__ import annotations

import copy
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_closer_delivery_lifecycle import OfflineDelivery as _OfflineDelivery
from test_closer_disposition import TARGET

import closer_dispatcher as adapter
import closer_pipeline
import dispatcher
import execution_profiles

_REAL_LOW = adapter._pipeline_low_route
_REAL_EXCEPTION = adapter._exception_route
_REAL_CAPACITY = adapter._fresh_capacity_report


def bound_adjudication(proposal, *, disputed=False):
    """Name one real original criterion using its actual retained raw evidence."""
    row = proposal["decision"]["criteria"][1]
    proposal["adjudication_request"] = {
        "basis": {"source": row["source"], "criterion": row["criterion"]},
        "question": "Does this retained receipt prove the named report criterion?",
        "evidence_refs": row["evidence"],
    }
    if disputed:
        row["result"] = "UNKNOWN"
    return proposal


def scoped_response(proposal, request):
    import closer_execution_contracts as contracts

    row = next(
        row
        for row in proposal["decision"]["criteria"]
        if row["source"] == request["basis"]["source"] and row["criterion"] == request["criterion"]
    )
    return {
        "schema": contracts.RESPONSE_SCHEMA,
        **{
            key: request[key]
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
        "reason": "The exact retained current-head receipt exercises this named criterion.",
        "evidence": row["evidence"],
        "proofs": row["proofs"],
    }


def scoped_context_response(low, context):

    import closer_execution_contracts as contracts

    prep = context["prepare"]
    snap = json.loads(Path(prep["snapshot_ref"]["path"]).read_bytes())
    packet = json.loads(Path(prep["packet_ref"]["path"]).read_bytes())
    snap["base"] = packet["base"]
    proposal = low["proposal"]
    explicit = proposal["adjudication_request"]
    request = contracts.build_adjudication_request(
        snap,
        packet,
        prep["packet_ref"],
        basis=explicit["basis"],
        question=explicit["question"],
        evidence_refs=explicit["evidence_refs"],
        retained_dir=Path(prep["packet_ref"]["path"]).parent.parent,
    )
    return scoped_response(proposal, request)


class OfflineDelivery(_OfflineDelivery):
    """These parser/transport units assess post-verifier acceptance, not premerge."""

    def __init__(self, tmp_path, monkeypatch):
        super().__init__(tmp_path, monkeypatch)
        self.current.update(state="MERGED", merge="d" * 40, merged_at="2026-10-18T00:00:00Z")
        self.refresh()
        # Parse a trusted bot marker through the native verifier validator; the
        # retained original criteria still require real current-head proof below.
        self.supply_native_pass(monkeypatch)
        assert self.current["verifier"]["evaluated_sha"] == self.current["merge"]
        assert self.backend.dispositions == {}


def install_strong_capacity(monkeypatch):
    monkeypatch.setattr(adapter, "_pipeline_capacity", lambda: {"io": 2, "low": 1, "exception": 1})
    monkeypatch.setattr(
        execution_profiles, "default_codex_profile", lambda *_a, **_k: "offline-strong"
    )
    report = {
        "generated_at": time.time(),
        "agents": {
            agent: {"state": "ok"}
            for agent in ("codex", "claude", "gemini", "cursor", "vibe", "muse")
        },
        "profiles": {"offline-strong": {"state": "ok", "capacity_pool_ids": ["offline-pool"]}},
        "pools": {"offline-pool": {"state": "ok"}},
    }
    monkeypatch.setattr(adapter, "_fresh_capacity_report", lambda: report)

    def exception_route(**kwargs):
        load = kwargs.get("load") or {}
        pools = ["offline-pool"]
        if any(load.get("pool:" + pool, 0) for pool in pools):
            return (None, "busy")
        load["codex"] = load.get("codex", 0) + 1
        for pool in pools:
            load["pool:" + pool] = load.get("pool:" + pool, 0) + 1
        return (
            {
                "agent": "codex",
                "profile_id": "offline-strong",
                "pools": pools,
                "mode": "assess",
            },
            "available",
        )

    monkeypatch.setattr(adapter, "_exception_route", exception_route)


def callbacks_for(delivery, monkeypatch):
    callbacks = {}

    def capture(path, targets, **kwargs):
        callbacks.update(kwargs)
        return {"targets": {}}

    monkeypatch.setattr(closer_pipeline, "run_pipeline", capture)
    args = SimpleNamespace(state=str(delivery.root / "queue.json"), max_workers=1, shadow=False)
    adapter._run_staged_pipeline(args, [TARGET], delivery.backend, time.time() + 600)
    prep = callbacks["prepare"](TARGET, {"deadline": time.time() + 180})
    return callbacks, {
        "prepare": prep,
        "deadline": time.time() + 180,
        "revision": prep["revision"],
        "identity": prep["identity"],
        "work_key": "offline-batch",
    }


def native_delivery(tmp_path, monkeypatch):
    """Actual CLI ownership; only the external provider/collector data ports vary.

    The exported OfflineDelivery and callbacks_for stay unchanged for their other
    importers. No test-global caller or production callback override is installed.
    """
    from closer_inert_dispatcher_support import InertCaller
    from test_dispatcher_controlled_data_only import world

    import closer_evidence

    delivery = OfflineDelivery(tmp_path, monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path / "private-home"))
    caller = InertCaller(world.__wrapped__(tmp_path, monkeypatch), monkeypatch)
    caller.world.args["data_only_limits"].update(max_final_bytes=48 * 1024)
    caller.activate()
    caller.seed_capacity()
    monkeypatch.setattr(adapter, "_pipeline_low_route", _REAL_LOW)
    monkeypatch.setattr(adapter, "_exception_route", _REAL_EXCEPTION)
    monkeypatch.setattr(adapter, "_fresh_capacity_report", _REAL_CAPACITY)
    monkeypatch.setattr(adapter, "_pipeline_capacity", lambda: {"io": 2, "low": 1, "exception": 1})
    delivery.backend.workspaces = {"owner/repo": str(tmp_path)}
    packet, _, proposal = caller.packet(delivery.current, tmp_path / "queue.json.evidence")
    # Generate the synthetic candidate before native emission and its immutable
    # receipt, rather than substitute an inline completed proposal afterward.
    bound_adjudication(proposal)
    delivery.proposal = proposal
    monkeypatch.setattr(closer_evidence, "collect_evidence", lambda *_a, **_kw: packet)
    delivery.caller = caller
    delivery.low_response = delivery.offload
    delivery.strong_response = lambda *_a, **_kw: pytest.fail("unexpected strong invocation")

    def broker(agent, prompt, **options):
        if options["task_type"] == "closer_interpret":
            # Reuse only candidate text/exit from the old generator, never its
            # synthetic transport identity or model/log metadata.
            candidate = delivery.low_response("gemini", prompt, **options)
            control = {"exit": candidate.get("exit", 0), "output": candidate.get("output", "")}
        else:
            envelope = json.loads(prompt.split("\n", 1)[1])
            control = delivery.strong_response(envelope, prompt, options)
        result = caller.response(control, agent, prompt, **options)
        delivery.native_outputs[result["run_id"]] = result.get("output", "")
        return result

    delivery.native_outputs = {}
    monkeypatch.setattr(dispatcher, "offload", broker)

    def run(*, blocker_fn=None):
        assert blocker_fn is None, "native callback fixture uses its actual CLI blocker reader"
        args = SimpleNamespace(state=str(delivery.root / "queue.json"), max_workers=1, shadow=False)
        return caller.staged(args, [TARGET], delivery.backend, time.time() + 330, coordinator=True)

    delivery.run = run

    def state():
        shards = list(delivery.root.glob("queue.json.pipeline.json.scoped/targets/*/state.json"))
        assert len(shards) == 1
        return json.loads(shards[0].read_bytes())

    delivery.state = state
    return delivery


def native_callbacks_for(delivery, monkeypatch):
    """Invoke each callback while actual CLI ownership and scoped leases live."""
    saved = {}
    args = SimpleNamespace(state=str(delivery.root / "queue.json"), max_workers=1, shadow=False)

    def invoke(stage, target, context):
        def exercise(path, targets, **callbacks):
            saved["result"] = callbacks[stage](target, context)
            return {"targets": {target: saved["result"]}}

        with monkeypatch.context() as local:
            local.setattr(closer_pipeline, "run_pipeline", exercise)
            delivery.caller.staged(args, [target], delivery.backend, time.time() + 180)
        return saved["result"]

    prep = invoke("prepare", TARGET, {"deadline": time.time() + 180})
    return {
        stage: (lambda target, context, stage=stage: invoke(stage, target, context))
        for stage in ("interpret", "exception", "act")
    }, {
        "prepare": prep,
        "deadline": time.time() + 180,
        "revision": prep["revision"],
        "identity": prep["identity"],
        "work_key": "native-batch",
    }


@pytest.mark.parametrize("tampered", [False, True])
def test_native_coordinator_preserves_exact_callback_registration(tmp_path, monkeypatch, tampered):
    delivery = native_delivery(tmp_path, monkeypatch)
    delivery.proposal.update(classification="uncertain", substantive_needs=["Unresolved evidence"])
    delivery.strong_response = lambda *_a: {"exit": 124, "outcome_uncertain": True}
    actual_runner = closer_pipeline.run_pipeline

    def inspect_registration(*args, **callbacks):
        original = callbacks["interpret"]
        assert original in closer_pipeline._FINITE_DISPATCHER_CALLBACKS
        if tampered:
            callbacks["interpret"] = lambda *a, **kw: original(*a, **kw)
        return actual_runner(*args, **callbacks)

    monkeypatch.setattr(closer_pipeline, "run_pipeline", inspect_registration)
    if tampered:
        with pytest.raises(ValueError, match="exact registered production dispatcher callbacks"):
            delivery.run()
        assert delivery.caller.results == []
    else:
        result = delivery.run()
        assert result["targets"][TARGET]["results"]["interpret"]["actual"]["run_id"]
        assert len(delivery.caller.results) == 2
    assert not delivery.guard_calls


@pytest.mark.parametrize("style", ["json", "fenced", "codex_jsonl"])
def test_batch_low_and_strong_parser_matrix(tmp_path, monkeypatch, style):
    delivery = native_delivery(tmp_path, monkeypatch)
    cb, context = native_callbacks_for(delivery, monkeypatch)
    real_offload = delivery.offload

    def output(agent, prompt, **kw):
        row = real_offload(agent, prompt, **kw)
        if style == "fenced":
            row["output"] = "```json\n" + row["output"] + "\n```"
        if style == "codex_jsonl":
            row["output"] = "\n".join(
                json.dumps(event)
                for event in [
                    {"type": "thread.started", "thread_id": "offline-thread"},
                    {
                        "type": "item.completed",
                        "item": {
                            "type": "command_execution",
                            "aggregated_output": '{"classification":"unrelated"}',
                        },
                    },
                    {
                        "type": "item.completed",
                        "item": {"type": "agent_message", "text": row["output"]},
                    },
                    {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}},
                ]
            )
        if style == "codex_jsonl":
            # This is a parser unit projection only. Codex transport remains
            # unqualified; the projected candidate gets a genuine Claude run.
            parsed = adapter._worker_proposal(row, agent="codex")
            assert parsed
            row["output"] = json.dumps(parsed)
        return row

    delivery.low_response = output
    low = cb["interpret"](TARGET, context)
    assert low["integration_status"] == "accepted", low
    bound_adjudication(low["proposal"])
    context["interpret"] = low
    strong_row = {
        "exit": 0,
        "output": json.dumps(scoped_context_response(low, context)),
    }
    if style == "fenced":
        strong_row["output"] = "```json\n" + strong_row["output"] + "\n```"
    if style == "codex_jsonl":
        strong_row["output"] = "\n".join(
            json.dumps(event)
            for event in [
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": strong_row["output"]},
                },
                {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}},
            ]
        )
    if style == "codex_jsonl":
        parsed = adapter._worker_proposal(strong_row, agent="codex")
        assert parsed
        strong_row["output"] = json.dumps(parsed)
    delivery.strong_response = lambda *_a: strong_row
    context["interpret"] = low
    strong = cb["exception"](TARGET, context)
    # This is the required equivalent behavior; currently reproduces the defect.
    assert strong["validated"]["status"] == "accepted", strong


@pytest.mark.parametrize("outcome", ["inflight", "persistence", "malformed"])
def test_batch_strong_uncertain_restarts_without_duplicate(tmp_path, monkeypatch, outcome):
    delivery = native_delivery(tmp_path, monkeypatch)
    original = delivery.offload

    def needs_senior(agent, prompt, **kw):
        result = original(agent, prompt, **kw)
        proposal = json.loads(result["output"])
        proposal.update(
            classification="uncertain", substantive_needs=["offline evidence ambiguity"]
        )
        bound_adjudication(proposal)
        result["output"] = json.dumps(proposal)
        return result

    delivery.low_response = needs_senior
    calls = []

    def response(envelope, prompt, options):
        calls.append("offline:strong:unknown")
        row = {
            "exit": 124,
            "outcome_uncertain": True,
            "outcome_status": "uncertain",
            "retry_safe": False,
        }
        if outcome == "persistence":
            row.update(
                exit=70,
                raw_exit=0,
                provider_completed=True,
                completion_persistence={"status": "pending"},
            )
        elif outcome == "malformed":
            row["_native_mode"] = "partial"
        return row

    delivery.strong_response = response
    # Cross actual coordinator deadlines, rather than only revisiting the same run.
    real_pipeline = closer_pipeline.run_pipeline
    clock = [time.time()]

    def pipeline_with_clock(*args, **kwargs):
        kwargs.update(now=lambda: clock[0])
        return real_pipeline(*args, **kwargs)

    monkeypatch.setattr(closer_pipeline, "run_pipeline", pipeline_with_clock)
    first = delivery.run()
    assert calls == ["offline:strong:unknown"], first
    assert not delivery.guard_calls
    # Original uncertain invocation must remain held across a fresh process/cadence.
    clock[0] += 61
    second = delivery.run()
    assert calls == ["offline:strong:unknown"], second
    clock[0] += 61
    third = delivery.run()
    assert calls == ["offline:strong:unknown"], third
    assert "provider_outcome_uncertain" in json.dumps(delivery.state())
    assert delivery.caller.results[-1]["run_id"]
    assert delivery.caller.results[-1]["outcome_uncertain"] is True
    if outcome == "malformed":
        # Pure validator unit: malformed native bytes still have a genuine run;
        # separately prove that removing its identity cannot authorize retry.
        item = first["targets"][TARGET]
        uncertain = item["results"]["exception"]
        assert closer_pipeline._valid_uncertain_provider_outcome(uncertain, TARGET, item)
        unbound = copy.deepcopy(uncertain)
        unbound["provider_outcome"]["actual"].pop("run_id")
        assert not closer_pipeline._valid_uncertain_provider_outcome(unbound, TARGET, item)


def test_batch_low_preserves_requested_and_served_identity():
    """Pure post-transport metadata projection; no qualification or invocation."""
    actual = adapter._worker_actual(
        {
            "requested_model": "offline-requested",
            "served_model": None,
            "served_model_status": "unknown",
        }
    )
    assert actual["requested_model"] == "offline-requested"
    assert actual["served_model"] is None
    assert actual["served_model_status"] == "unknown"


@pytest.mark.parametrize(
    "style", ["incomplete", "failed", "malformed", "tool_only", "prose", "foreign_agent"]
)
def test_batch_native_jsonl_rejects_unsafe_or_unsupported_deliverables(style):
    proposal = {"target": TARGET, "classification": "relevant"}
    events = [
        {
            "type": "item.completed",
            "item": {"type": "command_execution", "aggregated_output": json.dumps(proposal)},
        },
        {"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(proposal)}},
        {"type": "turn.completed"},
    ]
    if style == "incomplete":
        events.pop()
    elif style == "failed":
        events[-1] = {"type": "turn.failed"}
    elif style == "tool_only":
        events.pop(1)
    elif style == "prose":
        events[1]["item"]["text"] = "Here is the result: " + json.dumps(proposal)
    output = "\n".join(map(json.dumps, events))
    if style == "malformed":
        output += "\ninvalid trailing text"
    assert (
        adapter._worker_proposal(
            {"exit": 0, "output": output}, agent="claude" if style == "foreign_agent" else "codex"
        )
        == {}
    )


def test_batch_strong_large_native_response_is_retained_once(tmp_path, monkeypatch):
    delivery = native_delivery(tmp_path, monkeypatch)
    cb, context = native_callbacks_for(delivery, monkeypatch)
    low = cb["interpret"](TARGET, context)
    bound_adjudication(low["proposal"])
    context["interpret"] = low
    row = {
        "exit": 0,
        "output": json.dumps(scoped_context_response(low, context)),
    }
    # Native event streams retain large tool transcripts which must not duplicate
    # accepted proof inside the bounded scheduling ledger.
    row["output"] = "\n".join(
        map(
            json.dumps,
            [
                {
                    "type": "item.completed",
                    "item": {"type": "command_execution", "aggregated_output": "x" * 100000},
                },
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": row["output"]},
                },
                {"type": "turn.completed"},
            ],
        )
    )
    # The 100KB legacy Codex stream is explicitly a parser/retention unit input,
    # above the production native cap. It is never qualified or sent to native.
    import closer_evidence

    parsed = adapter._worker_proposal(row, agent="codex")
    assert parsed
    legacy_ref = closer_evidence._retain_bytes(
        tmp_path / "legacy-parser-unit", row["output"].encode(), "worker-output.txt"
    )
    delivery.strong_response = lambda *_a: {"exit": 0, "output": json.dumps(parsed)}
    strong = cb["exception"](TARGET, context)
    assert strong["validated"]["status"] == "accepted"
    closer_pipeline._bounded(
        {"prepare": context["prepare"], "interpret": low, "exception": strong}, limit=64 * 1024
    )
    import hashlib

    ref = legacy_ref
    assert Path(ref["path"]).read_bytes() == row["output"].encode()
    assert ref["sha256"] == hashlib.sha256(row["output"].encode()).hexdigest()
    assert "output" not in strong["result"]["worker"]


@pytest.mark.parametrize(
    "worker_result,expected_reason",
    [
        (
            {
                "status": "failed",
                "reason": "transport failed",
                "worker": {"exit": 1},
            },
            "worker_execution_unverified",
        ),
        ({"status": "deferred", "reason": "no capacity"}, "worker_execution_unverified"),
        ({"status": "blocked", "reason": "no authorized workspace"}, "worker_execution_unverified"),
        (
            {"status": "completed", "worker": {"exit": 0, "output": ""}},
            "worker_execution_unverified",
        ),
        (
            {
                "status": "completed",
                "worker": {
                    "exit": 0,
                    "output": '{"proposal_file":"/tmp/never-open-this","sha256":"untrusted"}',
                },
            },
            "adjudication_response_scope_changed",
        ),
    ],
)
def test_batch_strong_nondecisions_never_gain_authority(
    tmp_path, monkeypatch, worker_result, expected_reason
):
    if expected_reason == "worker_execution_unverified":
        # Pure post-transport refusal unit: these legacy raw statuses are not
        # admitted native callback results. Exercise the actual consumer's
        # execution-verification guard directly, without inventing a run ID or
        # bypassing any origin gate to reach a production callback.
        from test_closer_disposition import fixture

        import closer_disposition

        snap, _, packet_ref, prep, _ = fixture(tmp_path)
        row = worker_result.get("worker") or {}
        proposal = adapter._worker_proposal(row, agent="codex")
        validated = closer_disposition.integrate_ordinary(
            TARGET,
            snap,
            packet_ref,
            prep,
            {"actual": adapter._worker_actual(row), "proposal": proposal},
        )
        assert validated["status"] in {"wait", "exception", "pending"}
        assert validated["reason"] == expected_reason
        assert validated["disposition"] is None  # No acceptance or action authority.
        assert proposal == {}
        return

    delivery = native_delivery(tmp_path, monkeypatch)
    cb, context = native_callbacks_for(delivery, monkeypatch)
    low = cb["interpret"](TARGET, context)
    bound_adjudication(low["proposal"])
    context["interpret"] = low
    delivery.strong_response = lambda *_a: worker_result["worker"]
    strong = cb["exception"](TARGET, context)
    assert strong["validated"]["status"] in {"wait", "exception", "pending"}
    assert strong["validated"]["reason"] == expected_reason
    assert strong["authority"] is False
    assert not delivery.guard_calls


def test_batch_large_proofs_cross_native_stages_and_refuse_unstarted_effect(tmp_path, monkeypatch):
    """Real finite stages and successor selection; no effect-child completion claim."""
    import copy

    import closer_disposition

    delivery = native_delivery(tmp_path, monkeypatch)
    original = delivery.offload

    def low(agent, prompt, **kw):
        result = original(agent, prompt, **kw)
        proposal = json.loads(result["output"])
        for row in proposal["decision"]["criteria"]:
            for proof in row["proofs"]:
                proof["relevance"] += (
                    " Ship the report evidence is retained in this test receipt." * 300
                )
        proposal.update(classification="relevant", substantive_needs=[])
        bound_adjudication(proposal, disputed=True)
        result["output"] = json.dumps(proposal)
        assert len(result["output"]) > 32768
        delivery.proposal_for_strong = proposal
        return result

    delivery.low_response = low
    strong_calls = []

    def response(envelope, prompt, options):
        strong_calls.append(1)
        proposal = copy.deepcopy(delivery.proposal_for_strong)
        response = scoped_response(proposal, envelope["adjudication_request"])
        return {"exit": 0, "output": json.dumps(response)}

    delivery.strong_response = response
    validated = []
    accepted_dispositions = []
    integrate = closer_disposition.integrate_ordinary

    def track(*args, **kwargs):
        result = integrate(*args, **kwargs)
        actual = args[-1].get("actual") or {}
        if (
            len(delivery.caller.results) == 2
            and actual.get("run_id") == delivery.caller.results[-1]["run_id"]
        ):
            validated.append(result["status"])
            if result["status"] == "accepted":
                accepted_dispositions.append(copy.deepcopy(result["disposition"]))
        return result

    monkeypatch.setattr(closer_disposition, "integrate_ordinary", track)
    captured = {"interpret": [], "exception": []}
    real_pipeline = closer_pipeline.run_pipeline

    def pipeline_with_capture(*args, **kwargs):
        # Observe actual coordinator results; never replace registered callbacks.
        receipt = real_pipeline(*args, **kwargs)
        results = receipt["targets"][TARGET].get("results", {})
        for stage in captured:
            if stage in results:
                captured[stage].append(copy.deepcopy(results[stage]))
        return receipt

    monkeypatch.setattr(closer_pipeline, "run_pipeline", pipeline_with_capture)
    receipt = delivery.run()
    assert strong_calls == [1], json.dumps(receipt)
    assert validated == ["accepted", "accepted"]  # Callback and action independently validate.
    assert delivery.guard_calls == [], receipt
    first_item = receipt["targets"][TARGET]
    assert first_item["deferred_reason"] == "finite_frontier_identity_mismatch"
    assert first_item["deferred_next_trigger"] == "new_frontier_required"
    assert delivery.closure_calls == []
    # scoped_backend deliberately keeps its disposition local to that action.
    # Persist the exact actual ACT-consumer result as the successor's input;
    # never author a replacement decision or grant it model-summary authority.
    assert accepted_dispositions[-1] == accepted_dispositions[0]
    delivery.backend.dispositions[TARGET] = accepted_dispositions[-1]
    first_frontier = receipt["finite_frontier"]
    native_runs = len(delivery.caller.results)
    frontier_from_observation = delivery.caller.frontier

    def successor_frontier(snapshots, backend=None):
        import closer_frontier_policy

        observed = frontier_from_observation(snapshots, backend)
        return closer_frontier_policy.make_frontier(
            list(snapshots),
            owner=observed["owner"],
            frontier_id="inert-fixture-validated-successor",
            bindings=observed["bindings"],
        )

    # Acceptance changed the selected guarded action. Admit its genuinely new
    # observed action in a new frontier, retaining the first frontier untouched.
    delivery.caller.frontier = successor_frontier
    receipt = delivery.run()
    assert receipt["finite_frontier"]["frontier_id"] != first_frontier["frontier_id"]
    assert (
        receipt["finite_frontier"]["bindings"][TARGET]["action_id"]
        != first_frontier["bindings"][TARGET]["action_id"]
    )
    assert len(delivery.caller.results) == native_runs
    assert strong_calls == [1]
    assert delivery.closure_calls == []
    item = receipt["targets"][TARGET]
    assert item["results"]["interpret"]["classification"] == "deterministic_close_source"
    assert item["status"] == "unknown"
    assert (
        item["error"]
        == "RuntimeError: inner closer queue did not prove a valid continuation boundary"
    )
    # The exact source-generated effect subprocess was refused before start.
    # Native emitter children are genuine, but no closure OSchild was fabricated.
    assert item["stage"] == "act" and item["stage_status"] == "unknown"
    assert not delivery.guard_calls
    assert captured["interpret"][0]["proposal_ref"]
    assert captured["exception"][0]["validated_ref"]
    for stage in captured.values():
        for result in stage:
            closer_pipeline._bounded(result, limit=32768)
    for observed_item in (first_item, item):
        for result in observed_item["results"].values():
            closer_pipeline._bounded(result, limit=32768)
        closer_pipeline._bounded(observed_item["results"], limit=64 * 1024)


def test_batch_supervisor_closure_effect_unit_keeps_native_source_checks(tmp_path, monkeypatch):
    """In-process native effect unit with fixture GitHub; no finite OSchild claim.

    The actual finite coordinator's unstarted effect is covered separately above.
    This unit preserves the original native supervisor closure/source assertion
    without authorizing a process-observation double or a live GitHub request.
    """
    import keepalive_supervisor

    delivery = OfflineDelivery(tmp_path, monkeypatch)
    result = keepalive_supervisor.close_verified_issues(**delivery.closure_options)
    delivery.closure_calls.append(result)
    assert len(delivery.closure_calls) == 1 and delivery.closure_calls[0]["closed"] == 1
    assert delivery.closure_calls[0]["errors"] == 0
    assert delivery.source_issue["state"] == "closed"
    assert not delivery.guard_calls and not delivery.provider_calls
    repeated = keepalive_supervisor.close_verified_issues(**delivery.closure_options)
    assert repeated["closed"] == 0
    assert not delivery.provider_calls


def test_batch_retained_low_proposal_hash_is_checked_before_escalation(tmp_path, monkeypatch):

    delivery = native_delivery(tmp_path, monkeypatch)
    cb, context = native_callbacks_for(delivery, monkeypatch)
    # Produce a valid normal packet-bound response, with a large reviewer note.
    original = delivery.offload

    def large(agent, prompt, **kw):
        result = original(agent, prompt, **kw)
        proposal = json.loads(result["output"])
        proposal["reviewer_note"] = "x" * 18000
        result["output"] = json.dumps(proposal)
        return result

    delivery.low_response = large
    low = cb["interpret"](TARGET, context)
    Path(low["proposal_ref"]["path"]).write_text('{"classification":"relevant"}')
    delivery.strong_response = lambda *a, **kw: pytest.fail("tampered proposal must not dispatch")
    hold = cb["exception"](TARGET, {**context, "interpret": low})
    assert hold["classification"] == "completed_response_retention_failed"
    assert "hash or size differs" in hold["provider_outcome"]["retention_error"]


@pytest.mark.parametrize("failed_suffix", ["proposal.json", "worker-output.txt", "validation.json"])
def test_batch_completed_response_retention_fault_never_repeats_provider(
    tmp_path, monkeypatch, failed_suffix
):
    import copy
    import hashlib

    import closer_evidence

    delivery = native_delivery(tmp_path, monkeypatch)
    original = delivery.offload
    native_outputs = delivery.native_outputs
    strong_calls = []

    def low(agent, prompt, **kw):
        result = original(agent, prompt, **kw)
        proposal = json.loads(result["output"])
        for row in proposal["decision"]["criteria"]:
            for proof in row["proofs"]:
                proof["relevance"] += (
                    " Ship the report evidence is retained in this test receipt." * 300
                )
        proposal.update(classification="relevant", substantive_needs=[])
        bound_adjudication(proposal, disputed=True)
        result.update(
            output=json.dumps(proposal),
        )
        delivery.proposal_for_strong = proposal
        return result

    delivery.low_response = low

    def response(envelope, prompt, options):
        strong_calls.append(1)
        proposal = copy.deepcopy(delivery.proposal_for_strong)
        response = scoped_response(proposal, envelope["adjudication_request"])
        row = {
            "exit": 0,
            "output": json.dumps(response),
        }
        return row

    delivery.strong_response = response
    retain = closer_evidence._retain_bytes

    def failed_retention(root, raw, suffix):
        if suffix == failed_suffix:
            raise OSError("offline injected disk write failure after provider completion")
        return retain(root, raw, suffix)

    monkeypatch.setattr(closer_evidence, "_retain_bytes", failed_retention)
    real_pipeline = closer_pipeline.run_pipeline
    clock = [time.time()]

    def pipeline_with_clock(*args, **kwargs):
        kwargs.update(now=lambda: clock[0])
        return real_pipeline(*args, **kwargs)

    monkeypatch.setattr(closer_pipeline, "run_pipeline", pipeline_with_clock)
    first = delivery.run()
    item = first["targets"][TARGET]
    stage = "interpret" if failed_suffix == "proposal.json" else "exception"
    assert item["error"] == (
        "interpretation outcome is unresolved"
        if stage == "interpret"
        else "exception outcome is unresolved"
    )
    assert item["status"] == "unknown"
    hold = item["results"][stage]["provider_outcome"]
    actual = hold["actual"]
    assert hold["schema"] == "closer-completed-response-retention/v1"
    assert actual["exit"] == 0 and actual["provider_completed"] is True
    emitted = next(row for row in delivery.caller.results if row["run_id"] == actual["run_id"])
    assert actual["log"] == emitted["log"]
    assert actual["served_model_status"] == emitted["served_model_status"] == "observed"
    assert (
        actual["output_sha256"]
        == hashlib.sha256(native_outputs[actual["run_id"]].encode()).hexdigest()
    )
    assert actual["output_bytes"] == len(native_outputs[actual["run_id"]].encode())
    for _ in range(2):
        clock[0] += 61
        delivery.run()
    assert len(delivery.provider_calls) == 1
    assert strong_calls == ([] if stage == "interpret" else [1])
    assert not delivery.guard_calls


def test_batch_oversized_actual_retained_file_is_rejected_before_read(tmp_path, monkeypatch):

    delivery = native_delivery(tmp_path, monkeypatch)
    cb, context = native_callbacks_for(delivery, monkeypatch)
    original = delivery.offload

    def large(agent, prompt, **kw):
        result = original(agent, prompt, **kw)
        proposal = json.loads(result["output"])
        proposal["reviewer_note"] = "x" * 18000
        result["output"] = json.dumps(proposal)
        return result

    delivery.low_response = large
    low = cb["interpret"](TARGET, context)
    path = Path(low["proposal_ref"]["path"])
    path.write_bytes(b"x" * (4 * 1024 * 1024 + 1))
    actual_fdopen = adapter.os.fdopen
    actual_read_bytes = Path.read_bytes
    reads = []

    class NoReadStream:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.stream.close()

        def fileno(self):
            return self.stream.fileno()

        def read(self, size=-1):
            reads.append(size)
            pytest.fail("oversized actual response must be refused before any read")

    def no_unbounded_read(candidate):
        if candidate == path:
            pytest.fail("Path.read_bytes is unbounded for the mismatched retained response")
        return actual_read_bytes(candidate)

    monkeypatch.setattr(Path, "read_bytes", no_unbounded_read)
    monkeypatch.setattr(
        adapter.os,
        "fdopen",
        lambda fd, *a, **kw: (
            NoReadStream(actual_fdopen(fd, *a, **kw))
            if adapter.os.fstat(fd).st_ino == path.stat().st_ino
            else actual_fdopen(fd, *a, **kw)
        ),
    )
    delivery.strong_response = lambda *a, **kw: pytest.fail("oversized proposal must not dispatch")
    hold = cb["exception"](TARGET, {**context, "interpret": low})
    assert hold["classification"] == "completed_response_retention_failed"
    assert "hash or size differs" in hold["provider_outcome"]["retention_error"]
    assert reads == []


@pytest.mark.parametrize("tamper", ["summary", "output_hash"])
def test_batch_strong_validation_summary_cannot_replace_original_proofs(
    tmp_path, monkeypatch, tamper
):

    delivery = native_delivery(tmp_path, monkeypatch)
    cb, context = native_callbacks_for(delivery, monkeypatch)
    original = delivery.offload

    def uncertain(agent, prompt, **kw):
        result = original(agent, prompt, **kw)
        proposal = json.loads(result["output"])
        proposal.update(classification="uncertain", substantive_needs=["offline missing proof"])
        if tamper == "output_hash":
            proposal.update(classification="relevant", substantive_needs=[])
        bound_adjudication(proposal, disputed=True)
        result["output"] = json.dumps(proposal)
        return result

    delivery.low_response = uncertain
    low = cb["interpret"](TARGET, context)
    row = {
        "exit": 0,
        "output": json.dumps(scoped_context_response(low, context)),
    }
    delivery.strong_response = lambda *_a, **_kw: row
    strong = cb["exception"](TARGET, {**context, "interpret": low})
    if tamper == "summary":
        assert strong["validated"]["status"] == "exception"
        strong["validated"] = {
            "status": "accepted",
            "reason": "untrusted changed summary",
            "disposition": delivery.proposal["decision"],
        }
        strong["validated_ref"] = {
            "path": "/tmp/must-not-be-opened",
            "sha256": "untrusted",
            "bytes": 0,
        }
    else:
        assert strong["validated"]["status"] == "accepted"
        Path(strong["result"]["worker"]["output_ref"]["path"]).write_text("{}")
    action_context = {
        **context,
        "interpret": low,
        "exception": strong,
        "state_path": str(tmp_path / "queue.json.pipeline.json"),
        "action_state_path": str(tmp_path / "action.json"),
    }
    if tamper == "output_hash":
        result = cb["act"](TARGET, action_context)
        assert result["classification"] == "completed_response_retention_failed"
        assert "hash or size differs" in result["provider_outcome"]["retention_error"]
    else:
        import closer_disposition

        checked = []
        integrate = closer_disposition.integrate_ordinary

        def retain_actual_validation(*args, **kwargs):
            result = integrate(*args, **kwargs)
            checked.append(result)
            return result

        monkeypatch.setattr(closer_disposition, "integrate_ordinary", retain_actual_validation)
        action = cb["act"](TARGET, action_context)
        # Assert the actual consumer revalidation of the sealed native result.
        # The separate finite action guard also refuses this changed selection.
        assert action["status"] == "blocked"
        result = checked[-1]
        assert result["reason"] == "substantive_or_ambiguous_finding"
        assert "substantive_or_ambiguous_finding" in json.dumps(result)
    assert not delivery.guard_calls


def test_batch_retained_fifo_refuses_without_waiting_for_writer(tmp_path):
    import os
    import subprocess
    import sys

    fifo = tmp_path / "worker-response.fifo"
    os.mkfifo(fifo)
    # Execute the production nested loader in a child: a missing O_NONBLOCK
    # would hang before fstat, so the timeout safely fails rather than stalls CI.
    child = """
import ast,hashlib,os,stat,sys
from pathlib import Path
source=Path(sys.argv[1]).read_text()
function=next(node for node in ast.walk(ast.parse(source))
              if isinstance(node,ast.FunctionDef) and node.name=="retained_worker_text")
retained_dir=Path(sys.argv[2])
exec(compile(ast.Module(body=[function],type_ignores=[]),sys.argv[1],"exec"),globals())
try:
 retained_worker_text({"path":str(retained_dir/"worker-response.fifo"),"bytes":0,"sha256":hashlib.sha256(b"").hexdigest()})
except RuntimeError as error:
 assert "hash or size differs" in str(error),error
 print("nonregular-refused")
else:
 raise AssertionError("FIFO must never be consumed as worker evidence")
"""
    result = subprocess.run(
        [sys.executable, "-c", child, adapter.__file__, str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "nonregular-refused"


@pytest.mark.parametrize(
    "stage,damage",
    [("exception", "missing"), ("exception", "hash"), ("act", "hash"), ("strong_act", "hash")],
)
def test_batch_completed_retained_read_fault_never_repeats_provider(
    tmp_path, monkeypatch, stage, damage
):
    import copy

    import closer_evidence

    delivery = native_delivery(tmp_path, monkeypatch)
    original = delivery.offload
    strong_calls = []
    original_refs = []

    def low(agent, prompt, **kw):
        result = original(agent, prompt, **kw)
        proposal = json.loads(result["output"])
        proposal["reviewer_note"] = "x" * 18000
        if stage != "act":
            proposal.update(classification="relevant", substantive_needs=[])
            bound_adjudication(proposal, disputed=True)
        result.update(
            output=json.dumps(proposal),
        )
        delivery.proposal_for_strong = proposal
        return result

    delivery.low_response = low

    def response(envelope, prompt, options):
        strong_calls.append(1)
        assert (
            stage == "strong_act"
        ), "corrupted completed low evidence must not dispatch strong work"
        proposal = copy.deepcopy(delivery.proposal_for_strong)
        response = scoped_response(proposal, envelope["adjudication_request"])
        return {"exit": 0, "output": json.dumps(response)}

    delivery.strong_response = response
    retain = closer_evidence._retain_bytes

    def corrupt_completed(root, raw, suffix):
        ref = retain(root, raw, suffix)
        if suffix == ("worker-output.txt" if stage == "strong_act" else "proposal.json"):
            original_refs.append(copy.deepcopy(ref))
            if damage == "missing":
                Path(ref["path"]).unlink()
            else:
                Path(ref["path"]).write_bytes(b'{"corrupted":"after native completion"}')
        return ref

    monkeypatch.setattr(closer_evidence, "_retain_bytes", corrupt_completed)
    real_pipeline = closer_pipeline.run_pipeline
    clock = [time.time()]

    def pipeline_with_clock(*args, **kwargs):
        kwargs.update(now=lambda: clock[0])
        return real_pipeline(*args, **kwargs)

    monkeypatch.setattr(closer_pipeline, "run_pipeline", pipeline_with_clock)
    first = delivery.run()
    item = first["targets"][TARGET]
    assert item["status"] == "unknown", first
    held_stage = "exception" if stage == "exception" else "act"
    outcome = item["results"][held_stage]["provider_outcome"]
    assert outcome["schema"] == "closer-completed-response-retention/v1"
    assert outcome["actual"]["exit"] == 0 and outcome["actual"]["provider_completed"] is True
    emitted = next(
        row for row in delivery.caller.results if row["run_id"] == outcome["actual"]["run_id"]
    )
    assert outcome["actual"]["served_model_status"] == emitted["served_model_status"] == "observed"
    assert outcome["actual"]["log"] == emitted["log"]
    assert outcome["retained_evidence"]["ref"] == original_refs[-1]
    assert (
        "output_sha256" not in outcome["actual"]
    )  # A proposal-ref hash is not a raw-native-output hash.
    for _ in range(2):
        clock[0] += 61
        delivery.run()
    assert len(delivery.provider_calls) == 1
    assert strong_calls == ([1] if stage == "strong_act" else [])
    assert not delivery.guard_calls
