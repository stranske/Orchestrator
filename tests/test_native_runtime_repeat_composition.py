"""Real Core request fences distinguish new dispatch from a retained old label.

Initial requests use the public native/scoped CLI fixture. Repeats use the real
queue, Closer actor, pinned RuntimeBinding and shared Core CAS; only GitHub HTTP
responses are inert. No accepted authority, queue, or runtime callback stub.
"""

import base64
import copy
import json
import re
import time
from types import SimpleNamespace

import pytest
from test_closer_merge_successor_integration import public_merge_world as public_merge_world
from test_closer_native_capacity import capacity_host as capacity_host
from test_closer_native_observer import host as host
from test_closer_reasoning_boundary import native_world as native_world
from test_native_runtime_request_composition import runtime_request as runtime_request
from test_repeat_verification_events import event
from test_repeat_verification_reentry import marker, request

import closer_campaign as campaign
import closer_dispatcher as dispatcher
import closer_queue as queue
import closer_runtime_binding as binding

TARGET = "owner/repo#12"


def records(contents):
    return [json.loads(base64.b64decode(row["content"])) for row in contents.rows.values()]


@pytest.fixture
def repeat(runtime_request, monkeypatch, capsys):
    world, manifest, path, context, contents, core_requests = runtime_request
    assert dispatcher.main(world.argv) == 78
    capsys.readouterr()
    assert len(world.verifies) == 1 and not world.merges
    initial = records(contents)[0]
    assert initial["status"] == "succeeded"
    mutation = initial["mutation_intent"]["native_binding"]["verification_mutation"]
    assert mutation["mutation_kind"] == "workflow_dispatch"
    assert mutation["correlation_input"] == mutation["request_basis"]
    posted = world.verifies[0]
    fields = dict(
        posted[index + 1].split("=", 1) for index, value in enumerate(posted) if value == "-f"
    )
    assert fields == {
        "ref": "main",
        "inputs[mode]": "compare",
        "inputs[pr_number]": "12",
        "inputs[verification_request_basis]": mutation["request_basis"],
    }
    # Retain the fixture's old label as ambient prior evidence. It cannot
    # acknowledge a later dispatch or settle an unknown Core request.
    # A completed exact-subject verifier returned concerns; real retained source
    # selectors now request one bounded question about recovered material.
    prior = marker()
    prior.update(head_sha=world.snapshot["head"], evaluated_sha=world.snapshot["merge"])
    prior["verification_event"] = event(
        repo="owner/repo",
        pr="12",
        head_sha=world.snapshot["head"],
        evaluated_sha=world.snapshot["merge"],
    )
    world.snapshot.update(
        verifier=prior,
        verifier_runs=[],
        verifier_history_complete=True,
        revision="same-head-recovered-material",
    )
    backend = dispatcher.Closer(
        dispatcher.GitHub(deadline=time.time() + 30),
        state_path=world.root / "repeat-queue.json",
        apply=True,
        workers=True,
        runtime_binding=binding.binding_from_manifest(path),
        require_runtime_binding=True,
    )
    assert type(backend.runtime_binding) is binding.RuntimeBinding
    backend.dispositions[TARGET] = {
        "ordinary_nonpass": {
            "head": world.snapshot["head"],
            "verification_request": request(world.snapshot),
        }
    }
    assert backend.select(world.snapshot)[0] == "post_merge_verify", (
        backend.select(world.snapshot),
        world.snapshot["verifier"],
        backend.dispositions[TARGET],
    )
    state = SimpleNamespace(
        mode="accepted",
        dispatches=0,
        dispatch_fields=[],
        initial=copy.deepcopy(contents.rows),
        initial_mutation=copy.deepcopy(mutation),
    )
    transport = dispatcher.gh_capacity.gh_run

    def github(argv, **kwargs):
        endpoint = argv[3]
        method = argv[2]
        if endpoint == "repos/owner/repo" and method == "GET":
            if state.mode == "read_wait":
                raise TimeoutError("metadata read unavailable before Core intent")
            if state.mode == "head_drift":
                world.snapshot["head"] = "c" * 40
            if state.mode == "hold_drift":
                world.snapshot["labels"].append("agents:paused")
        if endpoint.endswith("/agents-verifier.yml/dispatches") and method == "POST":
            state.dispatches += 1
            fields = dict(
                argv[index + 1].split("=", 1) for index, value in enumerate(argv) if value == "-f"
            )
            assert set(fields) == {
                "ref",
                "inputs[mode]",
                "inputs[pr_number]",
                "inputs[verification_request_basis]",
                "inputs[verification_material]",
                "inputs[verification_predecessor]",
            }
            selected = backend.dispositions[TARGET]["ordinary_nonpass"]["verification_request"]
            assert json.loads(fields["inputs[verification_material]"]) == selected["material"]
            assert fields["inputs[verification_predecessor]"] == selected["predecessor_event_id"]
            assert fields["ref"] == "main" and fields["inputs[mode]"] == "compare"
            assert fields["inputs[pr_number]"] == "12"
            assert re.fullmatch(r"[0-9a-f]{64}", fields["inputs[verification_request_basis]"])
            state.dispatch_fields.append(fields)
            if state.mode == "uncertain":
                raise TimeoutError("dispatch POST result unavailable")
            if state.mode == "rejected":
                return 1, {"status": "422", "message": "Unexpected inputs"}
        return transport(argv, **kwargs)

    monkeypatch.setattr(dispatcher.gh_capacity, "gh_run", github)
    # GitHub captures its transport at construction; install all inert failure
    # modes before building the adapter actually used by the repeat actor.
    backend.gh = dispatcher.GitHub(deadline=time.time() + 30)
    state.backend, state.world, state.contents, state.path = backend, world, contents, path
    return state


def run(state):
    state.backend.gh.deadline = time.time() + 30
    return queue.run_round(
        state.backend.state_path,
        [TARGET],
        state.backend.observe,
        state.backend.execute,
        reconcile=state.backend.reconcile,
        owner="repeat-owned-native-actor",
        deadline=time.time() + 30,
        receipt_reserve_seconds=0,
        min_action_seconds=0,
        max_workers=1,
        max_passes=1,
    )


def test_initial_dispatch_then_new_dispatch_is_acknowledged_and_restart_idempotent(repeat):
    result = run(repeat)
    assert repeat.dispatches == 1, result
    current = records(repeat.contents)[0]
    assert current["status"] == "succeeded", current
    mutation = current["mutation_intent"]["native_binding"]["verification_mutation"]
    assert mutation["mutation_kind"] == "workflow_dispatch"
    assert mutation["correlation_input"] == mutation["request_basis"]
    assert mutation["request_basis"] != repeat.initial_mutation["request_basis"]
    assert (
        repeat.dispatch_fields[0]["inputs[verification_request_basis]"] == mutation["request_basis"]
    )
    retained = queue._read_state(repeat.backend.state_path)["items"][TARGET]["operation"]
    restarted = binding.binding_from_manifest(repeat.path)
    outcome = restarted.reconcile(TARGET, retained)
    assert outcome["status"] == "completed" and outcome["already_completed"] is True
    assert repeat.dispatches == 1 and repeat.world.events["provider"] == 0


@pytest.mark.parametrize("mode", ["read_wait", "head_drift", "hold_drift"])
def test_pre_post_read_or_binding_failure_leaves_previous_core_record_unchanged(repeat, mode):
    repeat.mode = mode
    result = run(repeat)
    assert repeat.dispatches == 0, result
    assert repeat.contents.rows == repeat.initial
    assert repeat.world.events["provider"] == 0


def test_dispatch_timeout_and_restart_cannot_credit_preexisting_label(repeat):
    repeat.mode = "uncertain"
    result = run(repeat)
    assert repeat.dispatches == 1, result
    current = records(repeat.contents)[0]
    assert current["status"] in {"submitted", "uncertain", "intent_recorded"}
    assert "verify:compare" in repeat.world.snapshot["labels"]
    retained = queue._read_state(repeat.backend.state_path)["items"][TARGET]["operation"]
    restarted = binding.binding_from_manifest(repeat.path)
    outcome = restarted.reconcile(TARGET, retained)
    assert outcome["status"] == "unknown", outcome
    repeat.backend.runtime_binding = restarted
    run(repeat)
    assert repeat.dispatches == 1 and repeat.world.events["provider"] == 0


def test_legacy_intent_without_mutation_kind_is_not_completed_from_old_label(repeat):
    runtime = repeat.backend.runtime_binding
    adapter = runtime._adapter("owner/repo")
    prior = records(repeat.contents)[0]
    native = copy.deepcopy(prior["mutation_intent"]["native_binding"])
    native.pop("verification_mutation")
    native["queue_operation_id"] = "legacy-owned-request"
    spec = campaign.ActionSpec(
        "owner/repo",
        12,
        "verify_request",
        native["head"],
        native["criteria_fingerprint"],
        native["review_revision"],
        prior.get("base_sha"),
        native_binding=native,
    )
    claimed = adapter.claim(spec, "legacy-owned-native-actor")
    assert claimed["state"] == "go", claimed
    adapter.record_intent(
        claimed["handle"],
        {
            "action": "verify_request",
            "head_sha": native["head"],
            "native_binding": native,
        },
    )
    operation = {
        "owner": "legacy-owned-native-actor",
        "action": "post_merge_verify",
        "id": native["queue_operation_id"],
        "revision": native["revision"],
        "binding": {
            key: native[key]
            for key in ("head", "merge", "pr_target", "source_targets", "review_revision")
        },
    }
    outcome = binding.binding_from_manifest(repeat.path).reconcile(TARGET, operation)
    assert outcome["status"] == "unknown" and "mutation kind" in outcome["reason"]
    assert repeat.dispatches == 0 and repeat.world.events["provider"] == 0


def test_exact_422_is_known_not_accepted_then_same_queue_can_retry_compatible_consumer(repeat):
    repeat.mode = "rejected"
    first = run(repeat)
    assert repeat.dispatches == 1, first
    current = records(repeat.contents)[0]
    assert current["status"] == "unavailable", current
    assert repeat.backend.runtime_binding.bundle.runtime.verification_request_not_accepted(current)
    retained = queue._read_state(repeat.backend.state_path)["items"][TARGET]["operation"]
    assert retained["status"] == "confirmed_not_started", retained
    resumed = binding.binding_from_manifest(repeat.path)
    assert resumed.reconcile(TARGET, retained)["status"] == "not_started"
    repeat.mode = "accepted"  # The inert consumer now supports these exact inputs.
    repeat.backend.runtime_binding = resumed
    second = run(repeat)
    assert repeat.dispatches == 2, second
    assert repeat.dispatch_fields[0] == repeat.dispatch_fields[1]
    assert records(repeat.contents)[0]["status"] == "succeeded"
    assert repeat.world.events["provider"] == 0
