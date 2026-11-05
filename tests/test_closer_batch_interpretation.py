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


def native_delivery(tmp_path, monkeypatch, *, large=False):
    """Actual CLI ownership; only the external provider/collector data ports vary.

    The exported OfflineDelivery and callbacks_for stay unchanged for their other
    importers. No test-global caller or production callback override is installed.
    """
    import test_closer_delivery_lifecycle as lifecycle
    import test_closer_disposition as pure_disposition
    from closer_inert_dispatcher_support import InertCaller
    from closer_semantic_fixture_support import physical_product
    from test_dispatcher_controlled_data_only import world

    import closer_evidence

    # Source data is constructed BEFORE OfflineDelivery/InertCaller restrict
    # subprocesses. Existing source validators consume genuine Git/blob bytes.
    product = physical_product(tmp_path / "physical-product")
    monkeypatch.setattr(lifecycle, "HEAD", product.head)
    monkeypatch.setattr(pure_disposition, "HEAD", product.head)
    monkeypatch.setattr(pure_disposition, "BASE", product.base)
    delivery = OfflineDelivery(tmp_path, monkeypatch)
    delivery.current.update(head=product.head, base=product.base, merge=product.head)
    delivery.supply_native_pass(monkeypatch)
    if large:
        body = "## Tasks\n" + "\n".join(
            "- [ ] Ship the report requirement " + str(i) for i in range(1, 10)
        )
        delivery.current["sources"][0]["body"] = body
        delivery.source_issue["body"] = body
        delivery.refresh()
        delivery.semantic_text_length = 1800
        import io
        import zipfile

        archive = io.BytesIO()
        criterion = adapter.requirements(body)[0]
        with zipfile.ZipFile(archive, "w") as output:
            output.writestr(
                "junit.xml", '<testsuite tests="1"><testcase name="' + criterion + '"/></testsuite>'
            )
        delivery.archive = archive.getvalue()
    monkeypatch.setenv("HOME", str(tmp_path / "private-home"))
    caller = InertCaller(world.__wrapped__(tmp_path, monkeypatch), monkeypatch)
    caller.world.args["data_only_limits"].update(max_final_bytes=48 * 1024)
    caller.activate()
    caller.seed_capacity()
    monkeypatch.setattr(adapter, "_pipeline_low_route", _REAL_LOW)
    monkeypatch.setattr(adapter, "_exception_route", _REAL_EXCEPTION)
    monkeypatch.setattr(adapter, "_fresh_capacity_report", _REAL_CAPACITY)
    monkeypatch.setattr(adapter, "_pipeline_capacity", lambda: {"io": 2, "low": 1, "exception": 1})
    delivery.backend.workspaces = {"owner/repo": str(product.root)}
    original_api = delivery.api

    def source_api(endpoint, *, method="GET", **kwargs):
        if endpoint in product.docs:
            assert method == "GET"
            return copy.deepcopy(product.docs[endpoint])
        if "/compare/" in endpoint:
            assert method == "GET" and endpoint.endswith(product.base + "..." + product.head)
            return copy.deepcopy(product.diff)
        return original_api(endpoint, method=method, **kwargs)

    delivery.api = source_api
    packet, _, proposal = caller.packet(delivery.current, tmp_path / "queue.json.evidence")
    # Generate the synthetic candidate before native emission and its immutable
    # receipt, rather than substitute an inline completed proposal afterward.
    bound_adjudication(proposal, disputed=True)
    delivery.proposal = proposal
    original_collect = closer_evidence.collect_evidence

    def local_collect(snapshot, _api, destination, **kwargs):
        kwargs["artifact_fetch"] = lambda *_a, **_kw: delivery.archive
        return original_collect(snapshot, source_api, destination, **kwargs)

    monkeypatch.setattr(closer_evidence, "collect_evidence", local_collect)
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
