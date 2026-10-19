"""Offline batch diagnostics: providers and remote APIs are simulated explicitly."""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest
from test_closer_delivery_lifecycle import OfflineDelivery
from test_closer_disposition import TARGET

import closer_dispatcher as adapter
import closer_pipeline
import dispatcher
import execution_profiles


def install_strong_capacity(monkeypatch):
    monkeypatch.setattr(adapter, "_pipeline_capacity", lambda: {"io": 2, "low": 1, "exception": 1})
    monkeypatch.setattr(execution_profiles, "default_codex_profile", lambda *_a, **_k: "offline-strong")
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


@pytest.mark.parametrize("style", ["json", "fenced", "codex_jsonl"])
def test_batch_low_and_strong_parser_matrix(tmp_path, monkeypatch, style):
    delivery = OfflineDelivery(tmp_path, monkeypatch)
    install_strong_capacity(monkeypatch)
    cb, context = callbacks_for(delivery, monkeypatch)
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
        row.update(requested_model="offline-requested", served_model_status="unknown")
        return row

    if style == "codex_jsonl":
        monkeypatch.setattr(
            adapter,
            "_pipeline_low_route",
            lambda **kw: (
                {"agent": "codex", "profile_id": "offline-codex-low", "pools": []},
                "available",
            ),
        )
        original = real_offload

        def real_offload(_agent, prompt, **kw):
            return original("gemini", prompt, **kw)

    monkeypatch.setattr(dispatcher, "offload", output)
    low = cb["interpret"](TARGET, context)
    assert low["integration_status"] == "accepted", low
    strong_row = {
        "exit": 0,
        "run_id": "offline:strong:1",
        "output": json.dumps(low["proposal"]),
        "requested_model": "offline-requested",
        "served_model_status": "unknown",
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
    monkeypatch.setattr(
        delivery.backend, "worker", lambda *a, **kw: {"status": "completed", "worker": strong_row}
    )
    context["interpret"] = low
    strong = cb["exception"](TARGET, context)
    # This is the required equivalent behavior; currently reproduces the defect.
    assert strong["validated"]["status"] == "accepted", strong


@pytest.mark.parametrize("outcome", ["inflight", "persistence", "malformed"])
def test_batch_strong_uncertain_restarts_without_duplicate(tmp_path, monkeypatch, outcome):
    delivery = OfflineDelivery(tmp_path, monkeypatch)
    install_strong_capacity(monkeypatch)
    original = delivery.offload

    def needs_senior(agent, prompt, **kw):
        result = original(agent, prompt, **kw)
        proposal = json.loads(result["output"])
        proposal.update(
            classification="uncertain", substantive_needs=["offline evidence ambiguity"]
        )
        proposal["decision"]["criteria"] = []
        result["output"] = json.dumps(proposal)
        return result

    monkeypatch.setattr(dispatcher, "offload", needs_senior)
    calls = []

    def worker(self, *args, **kw):
        calls.append("offline:strong:unknown")
        row = {
            "exit": 124,
            "run_id": "offline:strong:unknown",
            "outcome_uncertain": True,
            "outcome_status": "uncertain",
            "retry_safe": False,
            "requested_model": "offline-strong",
            "served_model_status": "unknown",
        }
        if outcome == "persistence":
            row.update(
                exit=70,
                raw_exit=0,
                provider_completed=True,
                completion_persistence={"status": "pending"},
            )
        elif outcome == "malformed":
            row.pop("run_id")
        return {"status": "failed", "advisory_only": True, "worker": row}

    monkeypatch.setattr(adapter.Closer, "worker", worker)
    # Cross actual coordinator deadlines, rather than only revisiting the same run.
    real_pipeline = closer_pipeline.run_pipeline
    clock = [time.time()]

    def pipeline_with_clock(*args, **kwargs):
        kwargs.update(now=lambda: clock[0], deadline=clock[0] + 30)
        return real_pipeline(*args, **kwargs)

    monkeypatch.setattr(closer_pipeline, "run_pipeline", pipeline_with_clock)
    first = delivery.run()
    assert calls == ["offline:strong:unknown"], first
    assert not delivery.guard_calls
    # Original uncertain invocation must remain held across a fresh process/cadence.
    clock[0] += 31
    second = delivery.run()
    assert calls == ["offline:strong:unknown"], second
    clock[0] += 31
    third = delivery.run()
    assert calls == ["offline:strong:unknown"], third
    if outcome == "malformed":
        assert closer_pipeline.UNBOUND_UNCERTAIN_PROVIDER_ERROR in json.dumps(delivery.state())
    else:
        assert "provider_outcome_uncertain" in json.dumps(delivery.state())


def test_batch_low_preserves_requested_and_served_identity(tmp_path, monkeypatch):
    delivery = OfflineDelivery(tmp_path, monkeypatch)
    cb, context = callbacks_for(delivery, monkeypatch)
    original = delivery.offload

    def output(agent, prompt, **kw):
        row = original(agent, prompt, **kw)
        row.update(
            requested_model="offline-requested", served_model=None, served_model_status="unknown"
        )
        return row

    monkeypatch.setattr(dispatcher, "offload", output)
    low = cb["interpret"](TARGET, context)
    assert low["actual"]["requested_model"] == "offline-requested"
    assert low["actual"]["served_model"] is None
    assert low["actual"]["served_model_status"] == "unknown"


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
    delivery = OfflineDelivery(tmp_path, monkeypatch)
    install_strong_capacity(monkeypatch)
    cb, context = callbacks_for(delivery, monkeypatch)
    low = cb["interpret"](TARGET, context)
    row = {"exit": 0, "run_id": "offline:strong:large", "output": json.dumps(low["proposal"])}
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
    monkeypatch.setattr(
        delivery.backend, "worker", lambda *a, **kw: {"status": "completed", "worker": row}
    )
    strong = cb["exception"](TARGET, context)
    assert strong["validated"]["status"] == "accepted"
    closer_pipeline._bounded(
        {"prepare": context["prepare"], "interpret": low, "exception": strong}, limit=64 * 1024
    )
    import hashlib
    from pathlib import Path

    ref = strong["result"]["worker"]["output_ref"]
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
                "worker": {"exit": 1, "run_id": "offline:failed"},
            },
            "transport failed",
        ),
        ({"status": "deferred", "reason": "no capacity"}, "no capacity"),
        ({"status": "blocked", "reason": "no authorized workspace"}, "no authorized workspace"),
        (
            {"status": "completed", "worker": {"exit": 0, "run_id": "offline:empty", "output": ""}},
            "strong_worker_json_invalid",
        ),
        (
            {
                "status": "completed",
                "worker": {
                    "exit": 0,
                    "run_id": "offline:pointer",
                    "output": '{"proposal_file":"/tmp/never-open-this","sha256":"untrusted"}',
                },
            },
            "worker_json_invalid",
        ),
    ],
)
def test_batch_strong_nondecisions_never_gain_authority(
    tmp_path, monkeypatch, worker_result, expected_reason
):
    delivery = OfflineDelivery(tmp_path, monkeypatch)
    install_strong_capacity(monkeypatch)
    cb, context = callbacks_for(delivery, monkeypatch)
    monkeypatch.setattr(delivery.backend, "worker", lambda *a, **kw: worker_result)
    strong = cb["exception"](TARGET, context)
    assert strong["validated"]["status"] in {"wait", "exception"}
    assert strong["validated"]["reason"] == expected_reason
    assert strong["authority"] is False
    assert not delivery.guard_calls


def test_batch_large_low_and_strong_cross_full_pipeline_with_proofs_loaded_again(
    tmp_path, monkeypatch
):
    import copy

    import closer_disposition

    delivery = OfflineDelivery(tmp_path, monkeypatch)
    install_strong_capacity(monkeypatch)
    original = delivery.offload

    def low(agent, prompt, **kw):
        result = original(agent, prompt, **kw)
        proposal = json.loads(result["output"])
        for row in proposal["decision"]["criteria"]:
            for proof in row["proofs"]:
                proof["relevance"] += (
                    " Ship the report evidence is retained in this test receipt." * 300
                )
        proposal.update(
            classification="uncertain", substantive_needs=["offline ambiguous evidence"]
        )
        result["output"] = json.dumps(proposal)
        assert len(result["output"]) > 32768
        return result

    monkeypatch.setattr(dispatcher, "offload", low)
    strong_calls = []

    def worker(self, *args, **kw):
        strong_calls.append(1)
        proposal = copy.deepcopy(kw["advisory_context"]["interpretation"])
        proposal.update(classification="relevant", substantive_needs=[])
        return {
            "status": "completed",
            "worker": {
                "exit": 0,
                "run_id": "offline:strong:large-full",
                "output": json.dumps(proposal),
            },
        }

    monkeypatch.setattr(adapter.Closer, "worker", worker)
    validated = []
    integrate = closer_disposition.integrate_ordinary

    def track(*args, **kwargs):
        result = integrate(*args, **kwargs)
        actual = args[-1].get("actual") or {}
        if actual.get("run_id") == "offline:strong:large-full":
            validated.append(result["status"])
        return result

    monkeypatch.setattr(closer_disposition, "integrate_ordinary", track)
    captured = {"interpret": [], "exception": []}
    real_pipeline = closer_pipeline.run_pipeline

    def pipeline_with_capture(*args, **kwargs):
        for stage in captured:
            callback = kwargs[stage]

            def capture(target, context, callback=callback, stage=stage):
                value = callback(target, context)
                captured[stage].append(copy.deepcopy(value))
                return value

            kwargs[stage] = capture
        return real_pipeline(*args, **kwargs)

    monkeypatch.setattr(closer_pipeline, "run_pipeline", pipeline_with_capture)
    receipt = delivery.run()
    assert strong_calls == [1], json.dumps(receipt)
    assert validated == ["accepted", "accepted"]  # Callback and action independently validate.
    assert len(delivery.guard_calls) == 1, receipt
    item = receipt["targets"][TARGET]
    assert not item.get("error"), item
    assert captured["interpret"][0]["proposal_ref"]
    assert captured["exception"][0]["validated_ref"]
    for stage in captured.values():
        for result in stage:
            closer_pipeline._bounded(result, limit=32768)
    for result in item["results"].values():
        closer_pipeline._bounded(result, limit=32768)
    closer_pipeline._bounded(item["results"], limit=64 * 1024)


def test_batch_retained_low_proposal_hash_is_checked_before_escalation(tmp_path, monkeypatch):
    from pathlib import Path

    delivery = OfflineDelivery(tmp_path, monkeypatch)
    install_strong_capacity(monkeypatch)
    cb, context = callbacks_for(delivery, monkeypatch)
    # Produce a valid normal packet-bound response, with a large reviewer note.
    original = delivery.offload

    def large(agent, prompt, **kw):
        result = original(agent, prompt, **kw)
        proposal = json.loads(result["output"])
        proposal["reviewer_note"] = "x" * 18000
        result["output"] = json.dumps(proposal)
        return result

    monkeypatch.setattr(dispatcher, "offload", large)
    low = cb["interpret"](TARGET, context)
    Path(low["proposal_ref"]["path"]).write_text('{"classification":"relevant"}')
    monkeypatch.setattr(
        delivery.backend,
        "worker",
        lambda *a, **kw: pytest.fail("tampered proposal must not dispatch"),
    )
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

    delivery = OfflineDelivery(tmp_path, monkeypatch)
    install_strong_capacity(monkeypatch)
    original = delivery.offload
    native_outputs = {}
    strong_calls = []

    def low(agent, prompt, **kw):
        result = original(agent, prompt, **kw)
        proposal = json.loads(result["output"])
        for row in proposal["decision"]["criteria"]:
            for proof in row["proofs"]:
                proof["relevance"] += (
                    " Ship the report evidence is retained in this test receipt." * 300
                )
        proposal.update(classification="uncertain", substantive_needs=["offline ambiguity"])
        result.update(
            output=json.dumps(proposal),
            log="offline-native-log",
            requested_model="offline-low-requested",
            served_model_status="unknown",
        )
        native_outputs[result["run_id"]] = result["output"]
        return result

    monkeypatch.setattr(dispatcher, "offload", low)

    def worker(self, *args, **kw):
        strong_calls.append(1)
        proposal = copy.deepcopy(kw["advisory_context"]["interpretation"])
        proposal.update(classification="relevant", substantive_needs=[])
        row = {
            "exit": 0,
            "run_id": "offline:completed-strong",
            "output": json.dumps(proposal),
            "log": "offline-native-log",
            "requested_model": "offline-strong-requested",
            "served_model_status": "unknown",
        }
        native_outputs[row["run_id"]] = row["output"]
        return {"status": "completed", "worker": row}

    monkeypatch.setattr(adapter.Closer, "worker", worker)
    retain = closer_evidence._retain_bytes

    def failed_retention(root, raw, suffix):
        if suffix == failed_suffix:
            raise OSError("offline injected disk write failure after provider completion")
        return retain(root, raw, suffix)

    monkeypatch.setattr(closer_evidence, "_retain_bytes", failed_retention)
    real_pipeline = closer_pipeline.run_pipeline
    clock = [time.time()]

    def pipeline_with_clock(*args, **kwargs):
        kwargs.update(now=lambda: clock[0], deadline=clock[0] + 30)
        return real_pipeline(*args, **kwargs)

    monkeypatch.setattr(closer_pipeline, "run_pipeline", pipeline_with_clock)
    first = delivery.run()
    item = first["targets"][TARGET]
    assert item["error"] == closer_pipeline.UNBOUND_UNCERTAIN_PROVIDER_ERROR
    stage = "interpret" if failed_suffix == "proposal.json" else "exception"
    hold = item["results"][stage]["provider_outcome"]
    actual = hold["actual"]
    assert hold["schema"] == "closer-completed-response-retention/v1"
    assert actual["exit"] == 0 and actual["provider_completed"] is True
    assert actual["log"] == "offline-native-log"
    assert actual["served_model_status"] == "unknown"
    assert (
        actual["output_sha256"]
        == hashlib.sha256(native_outputs[actual["run_id"]].encode()).hexdigest()
    )
    assert actual["output_bytes"] == len(native_outputs[actual["run_id"]].encode())
    for _ in range(2):
        clock[0] += 31
        delivery.run()
    assert len(delivery.provider_calls) == 1
    assert strong_calls == ([] if stage == "interpret" else [1])
    assert not delivery.guard_calls


def test_batch_oversized_actual_retained_file_is_rejected_before_read(tmp_path, monkeypatch):
    from pathlib import Path

    delivery = OfflineDelivery(tmp_path, monkeypatch)
    install_strong_capacity(monkeypatch)
    cb, context = callbacks_for(delivery, monkeypatch)
    original = delivery.offload

    def large(agent, prompt, **kw):
        result = original(agent, prompt, **kw)
        proposal = json.loads(result["output"])
        proposal["reviewer_note"] = "x" * 18000
        result["output"] = json.dumps(proposal)
        return result

    monkeypatch.setattr(dispatcher, "offload", large)
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
        adapter.os, "fdopen", lambda *a, **kw: NoReadStream(actual_fdopen(*a, **kw))
    )
    monkeypatch.setattr(
        delivery.backend,
        "worker",
        lambda *a, **kw: pytest.fail("oversized proposal must not dispatch"),
    )
    hold = cb["exception"](TARGET, {**context, "interpret": low})
    assert hold["classification"] == "completed_response_retention_failed"
    assert "hash or size differs" in hold["provider_outcome"]["retention_error"]
    assert reads == []


@pytest.mark.parametrize("tamper", ["summary", "output_hash"])
def test_batch_strong_validation_summary_cannot_replace_original_proofs(
    tmp_path, monkeypatch, tamper
):
    import copy
    from pathlib import Path

    delivery = OfflineDelivery(tmp_path, monkeypatch)
    install_strong_capacity(monkeypatch)
    cb, context = callbacks_for(delivery, monkeypatch)
    original = delivery.offload

    def uncertain(agent, prompt, **kw):
        result = original(agent, prompt, **kw)
        proposal = json.loads(result["output"])
        proposal.update(classification="uncertain", substantive_needs=["offline missing proof"])
        result["output"] = json.dumps(proposal)
        return result

    monkeypatch.setattr(dispatcher, "offload", uncertain)
    low = cb["interpret"](TARGET, context)
    proposal = copy.deepcopy(low["proposal"])
    if tamper == "output_hash":
        proposal.update(classification="relevant", substantive_needs=[])
    row = {"exit": 0, "run_id": "offline:strong:original", "output": json.dumps(proposal)}
    monkeypatch.setattr(
        delivery.backend, "worker", lambda *a, **kw: {"status": "completed", "worker": row}
    )
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
        result = cb["act"](TARGET, action_context)
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
    from pathlib import Path

    import closer_evidence

    delivery = OfflineDelivery(tmp_path, monkeypatch)
    install_strong_capacity(monkeypatch)
    original = delivery.offload
    strong_calls = []
    original_refs = []

    def low(agent, prompt, **kw):
        result = original(agent, prompt, **kw)
        proposal = json.loads(result["output"])
        proposal["reviewer_note"] = "x" * 18000
        if stage != "act":
            proposal.update(classification="uncertain", substantive_needs=["offline ambiguity"])
        result.update(
            output=json.dumps(proposal),
            log="offline-original-low-log",
            requested_model="offline-requested-low",
            served_model_status="unknown",
        )
        return result

    monkeypatch.setattr(dispatcher, "offload", low)

    def worker(self, *args, **kw):
        strong_calls.append(1)
        assert (
            stage == "strong_act"
        ), "corrupted completed low evidence must not dispatch strong work"
        proposal = copy.deepcopy(kw["advisory_context"]["interpretation"])
        proposal.update(classification="relevant", substantive_needs=[])
        return {
            "status": "completed",
            "worker": {
                "exit": 0,
                "run_id": "offline:strong:readback",
                "output": json.dumps(proposal),
                "log": "offline-original-strong-log",
                "requested_model": "offline-requested-strong",
                "served_model_status": "unknown",
            },
        }

    monkeypatch.setattr(adapter.Closer, "worker", worker)
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
        kwargs.update(now=lambda: clock[0], deadline=clock[0] + 30)
        return real_pipeline(*args, **kwargs)

    monkeypatch.setattr(closer_pipeline, "run_pipeline", pipeline_with_clock)
    first = delivery.run()
    item = first["targets"][TARGET]
    assert item["error"] == closer_pipeline.UNBOUND_UNCERTAIN_PROVIDER_ERROR, first
    held_stage = "exception" if stage == "exception" else "act"
    outcome = item["results"][held_stage]["provider_outcome"]
    assert outcome["schema"] == "closer-completed-response-retention/v1"
    assert outcome["actual"]["exit"] == 0 and outcome["actual"]["provider_completed"] is True
    assert outcome["actual"]["served_model_status"] == "unknown"
    assert outcome["actual"]["log"] == (
        "offline-original-strong-log" if stage == "strong_act" else "offline-original-low-log"
    )
    assert outcome["retained_evidence"]["ref"] == original_refs[-1]
    assert (
        "output_sha256" not in outcome["actual"]
    )  # A proposal-ref hash is not a raw-native-output hash.
    for _ in range(2):
        clock[0] += 31
        delivery.run()
    assert len(delivery.provider_calls) == 1
    assert strong_calls == ([1] if stage == "strong_act" else [])
    assert not delivery.guard_calls
