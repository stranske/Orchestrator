"""Inert source admission controls: no native process, auth reader or enrollment."""

import copy
import hashlib
import json
import os
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import closer_claude_auth
import closer_claude_role_admission as admission
import closer_data_only_execution as execution
import closer_dispatcher as closer
import closer_evidence as evidence
import closer_execution_contracts as contracts
import closer_resources as resources
import closer_role_qualification as roles
import closer_role_selection as selection
import dispatcher
import execution_profiles
import transport_saved_result

_ACTUAL_QUALITY = admission._quality


@pytest.fixture
def world(tmp_path, monkeypatch, request):
    def forbidden(*_a, **_kw):
        raise AssertionError("No subprocess/auth/native call in source admission control")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(closer_claude_auth, "borrow_claude_oauth_token", forbidden)
    logs = tmp_path / "handoff" / "dispatch-logs"
    logs.mkdir(parents=True)
    monkeypatch.setattr(dispatcher, "DISPATCH_LOG_DIR", logs)
    monkeypatch.setenv("HANDOFF_DIR", str(tmp_path / "handoff"))
    profile = execution_profiles.get_profile(admission.PROFILE_ID)
    scope = {
        "subject": "sonnet_high_v2",
        "runtime_enrollment_granted": False,
        "action_authority": False,
    }
    monkeypatch.setattr(admission, "_quality", lambda _ref: (profile, scope))
    monkeypatch.setattr(
        closer,
        "_fresh_capacity_report",
        lambda: {
            "agents": {"claude": {"state": "ok"}},
            "profiles": {admission.PROFILE_ID: {"state": "ok"}},
            "pools": {"claude-subscription": {"state": "ok"}},
        },
    )
    monkeypatch.setattr("adapters.automatic_profile_within_cap", lambda _p: True)

    def absent(pgid, signal):
        assert pgid == 12345 and signal == 0
        raise ProcessLookupError

    monkeypatch.setattr(os, "killpg", absent)
    body = getattr(request, "param", "## Tasks\n- [ ] " + admission.PUBLIC_CRITERION)
    monkeypatch.setattr(
        admission, "PUBLIC_SOURCE_SHA256", hashlib.sha256(body.encode()).hexdigest()
    )
    snapshot = {
        "target": "stranske/Orchestrator#984",
        "kind": "pr",
        "head": sorted(admission.PUBLIC_HEADS)[0],
        "base": "b" * 40,
        "revision": "inert-current-revision",
        "review_revision": "no-reviews",
        "sources": [
            {"target": "stranske/Orchestrator#983", "body": body, "state": "open", "labels": []}
        ],
    }
    patch = '+    if format == "csv":\n+        raise NotImplementedError("Declared incomplete CSV canary fixture")'

    def api(endpoint, **_kw):
        if "/compare/" in endpoint:
            return {
                "status": "ahead",
                "files": [
                    {
                        "filename": admission.PUBLIC_FILE,
                        "status": "added",
                        "patch": patch,
                        "sha": "c" * 40,
                    }
                ],
            }
        if "/actions/runs?" in endpoint:
            return {"workflow_runs": []}
        raise AssertionError(endpoint)

    packet = evidence.collect_evidence(snapshot, api, tmp_path / "evidence")
    path = tmp_path / "evidence" / "manifests" / (packet["manifest"]["packet_sha256"] + ".json")
    packet_ref = {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    ref = packet["sections"]["diff_files"]["content"]["files"][0]["patch_retained"]
    original = {
        "target": snapshot["target"],
        "head": snapshot["head"],
        "base": snapshot["base"],
        "classification": "uncertain",
        "substantive_needs": [],
        "findings": [],
        "decision": {
            "criteria": [
                {
                    "source": snapshot["sources"][0]["target"],
                    "criterion": text,
                    "result": "UNKNOWN",
                    "evidence": [],
                    "proofs": [],
                }
                for text in (body, *closer.requirements(body))
            ]
        },
    }
    proposal = {
        "schema": admission.PROPOSAL_SCHEMA,
        "snapshot": snapshot,
        "packet_ref": packet_ref,
        "original": original,
        "basis": {
            "source": snapshot["sources"][0]["target"],
            "criterion": admission.PUBLIC_CRITERION,
        },
        "evidence_refs": [ref],
    }
    calls = []
    control = {"duplicate": False, "exit": 0, "outcome": "unsatisfied", "omit_finished": False}

    def offload(agent, prompt, **kw):
        calls.append(kw)
        control["captured_permit"] = admission._CURRENT.get()
        assert agent == "claude" and kw["profile_id"] == admission.PROFILE_ID
        assert kw["mode"] == "assess" and kw["max_network_retries"] == 0
        assert resources.finite_pool_current_binding() == {
            "frontier_id": kw["finite_frontier_id"],
            "target": snapshot["target"],
        }
        packet, root, request, expected_prompt, descriptor = admission._prepare(proposal)
        assert prompt == expected_prompt
        limits = kw["data_only_limits"]
        private = str(tmp_path / "inert-private-runtime")
        launch = execution.validate_launch(
            agent="claude",
            profile=profile,
            binary_path="/tmp/inert-never-executed-claude",
            binary_sha256=execution.CLAUDE_BINARY_SHA256,
            argv=execution.expected_argv(
                "/tmp/inert-never-executed-claude", profile, limits=limits
            ),
            env=execution.isolated_environment(
                private,
                cwd=private + "/cwd",
                existing_oauth_token="inert-placeholder",
                limits=limits,
            ),
            cwd=private + "/cwd",
            private_root=private,
            response_protocol=execution.NATIVE_JSON,
            source_sha256={
                n: descriptor["sources"][n]
                for n in ("adapters.py", "dispatcher.py", "closer_data_only_execution.py")
            },
            limits=limits,
            prompt=prompt,
            timeout_seconds=kw["timeout"],
        )
        observation = {
            "source": "trusted-popen-transport/v1",
            "pid": 12345,
            "process_group": 12345,
            "os_started": "inert-no-process",
            "run_id": "inert-original-canary",
            "cleanup_terminal": True,
            "agent": "claude",
            "operation_id": kw["data_only_operation_id"],
            "profile_id": admission.PROFILE_ID,
            "task_type": admission.TASK,
            "finite_frontier_id": kw["finite_frontier_id"],
            "finite_frontier_target": kw["finite_frontier_target"],
            "data_only_execution_binding_sha256": launch["binding_sha256"],
        }
        kw["process_starting_callback"](observation)
        kw["process_started_callback"](observation)
        if not control["omit_finished"]:
            kw["process_finished_callback"](observation)
        answer = {
            "schema": contracts.RESPONSE_SCHEMA,
            **{
                k: request[k]
                for k in (
                    "request_id",
                    "target",
                    "head",
                    "base",
                    "revision",
                    "packet_sha256",
                    "basis",
                )
            },
            "outcome": control["outcome"],
            "reason": "The exact retained CSV branch explicitly raises rather than exporting rows.",
            "evidence": request["evidence"],
            "proofs": [
                {
                    **request["proof_identities"][0]["alternatives"][0],
                    "excerpt": "raise NotImplementedError",
                    "relevance": "The named CSV implementation explicitly refuses this format.",
                }
            ],
        }
        output = json.dumps(answer)
        if control["duplicate"]:
            output = output[:-1] + ',"outcome":"unsatisfied"}'
        native = json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "num_turns": 1,
                "terminal_reason": "completed",
                "stop_reason": "end_turn",
                "session_id": "inert-session",
                "result": output,
                "total_cost_usd": 0.0,
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 20,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                },
                "modelUsage": {
                    profile["requested_model"]: {
                        "inputTokens": 10,
                        "outputTokens": 20,
                        "cacheReadInputTokens": 0,
                        "cacheCreationInputTokens": 0,
                        "costUSD": 0.0,
                    }
                },
            }
        )
        reservation = {
            k: resources._pool_slots()[0]["record"][k]
            for k in ("reservation_id", "lane", "frontier_id", "target", "agent", "profile_id")
        }
        origin = {
            "schema": "closer-data-only-runtime-origin/v1",
            "source": "existing-dispatcher-observed-supervisor",
            "authority": False,
            "classification_validated": False,
            "pool_id": "claude-subscription",
            "operation_id": kw["data_only_operation_id"],
            "task_type": admission.TASK,
            "profile_id": admission.PROFILE_ID,
            "finite_frontier_id": kw["finite_frontier_id"],
            "finite_frontier_target": kw["finite_frontier_target"],
            "pool_reservation": reservation,
            "run_id": observation["run_id"],
            "process_observation": observation,
            "execution": launch,
            "collector_sources": {
                n: descriptor["sources"][n]
                for n in ("transport_saved_result.py", "transport_qualification.py")
            },
            "native_json": native,
            "native_stdout_sha256": hashlib.sha256(native.encode()).hexdigest(),
            "native_stdout_bytes": len(native.encode()),
            "final": execution.validate_payload(output, limits=limits, kind="final"),
            "normalized_native_result": transport_saved_result.normalize_claude_result(
                native,
                run_id=observation["run_id"],
                pool_id="claude-subscription",
                requested_model=profile["requested_model"],
                requested_effort=profile["reasoning_effort"],
            ),
        }
        result = {
            "exit": control["exit"],
            "output": output,
            "run_id": observation["run_id"],
            "process_started": True,
            "data_only_private_runtime": {
                "path": private,
                "cleanup": "removed_owned_private_runtime",
            },
            "data_only_origin": origin,
            "process_observation": observation,
        }
        receipt = {
            "schema": "offload-provider-result/v1",
            "full_result_retained": True,
            "outcome_uncertain": False,
            "run_id": result["run_id"],
            "profile_id": admission.PROFILE_ID,
            "result": copy.deepcopy(result),
            "retained_result_sha256": hashlib.sha256(
                json.dumps(result, sort_keys=True).encode()
            ).hexdigest(),
        }
        result["provider_result_ref"] = admission._write_once(
            logs / "inert.provider-result-1.json", receipt
        )
        return result

    monkeypatch.setattr(dispatcher, "offload", offload)
    return SimpleNamespace(
        proposal=proposal,
        quality_ref={"path": "inert-quality-only"},
        calls=calls,
        control=control,
        logs=logs,
        profile=profile,
    )


def run(world):
    return admission.run_current_source_public_qualification(
        world.proposal, quality_ref=world.quality_ref, deadline=time.time() + 80
    )


def test_actual_shared_callback_native_proof_and_inactive_row(world):
    before = copy.deepcopy(roles.ROLE_QUALIFICATIONS)
    value = run(world)
    assert value["capability_pass"] and value["qualification_only"]
    report = admission._read(value["report_ref"])
    assert report["callback_proof"]["expected"]["qualification_only"] is True
    assert len(world.calls) == 1
    proposal = admission.reviewed_record_proposal(value["report_ref"])
    assert (
        proposal["record"]["status"] == "pending_review"
        and proposal["record"]["expires_at"] is None
    )
    assert not proposal["enrollment_granted"] and not proposal["activation"]
    assert roles.ROLE_QUALIFICATIONS == before == {}
    assert roles.qualification_problems(
        world.profile,
        admission.ROLE,
        records={proposal["record"]["qualification_id"]: proposal["record"]},
    )


def test_same_immutable_intent_cannot_resubmit(world):
    run(world)
    with pytest.raises(FileExistsError):
        run(world)
    assert len(world.calls) == 1


def test_production_always_rejects_canary_proof_even_without_marker(world):
    value = run(world)
    report = admission._read(value["report_ref"])
    proof = report["callback_proof"]
    for remove in (False, True):
        expected = copy.deepcopy(proof["expected"])
        if remove:
            expected.pop("qualification_only")
        with pytest.raises(closer.ControlledOriginUnavailable):
            closer._validate_controlled_result(
                report["result"], expected, proof["observed"], receipt_root=world.logs
            )
        with pytest.raises(closer.ControlledOriginUnavailable):
            closer._validate_controlled_result(
                report["result"],
                expected,
                proof["observed"],
                receipt_root=world.logs,
                _qualification=True,
            )


@pytest.mark.parametrize("mutation", ["target", "head", "source", "question", "evidence", "schema"])
def test_arbitrary_public_input_or_scope_change_refused_before_broker(world, mutation):
    p = world.proposal
    if mutation == "target":
        p["snapshot"]["target"] = "foreign/private#1"
    elif mutation == "head":
        p["snapshot"]["head"] = "0" * 40
    elif mutation == "source":
        p["snapshot"]["sources"][0]["body"] += "\nnew instruction"
    elif mutation == "question":
        p["prompt"] = "arbitrary prompt"
    elif mutation == "evidence":
        p["evidence_refs"] = []
    else:
        p["schema"] = "foreign"
    with pytest.raises((ValueError, OSError)):
        run(world)
    assert not world.calls


@pytest.mark.parametrize("change", ["duplicate", "omit_finished", "exit", "outcome"])
def test_bad_native_or_answer_never_proposes_enrollment(world, change):
    world.control[change] = 3 if change == "exit" else "satisfied" if change == "outcome" else True
    value = run(world)
    assert not value["capability_pass"] and len(world.calls) == 1
    with pytest.raises(ValueError):
        admission.reviewed_record_proposal(value["report_ref"])
    assert roles.ROLE_QUALIFICATIONS == {}


def test_fake_or_foreign_source_permits_cannot_open_launch(world):
    record = {
        "execution_limits": admission._limits(closer._controlled_model_contract(admission.TASK))
    }
    with pytest.raises(ValueError):
        admission._issue_permit("launch", admission.PREFIX + "fake", record, time.time() + 60)
    with pytest.raises(ValueError):
        closer._controlled_launch_inputs(
            profile=world.profile,
            record=record,
            task_type=admission.TASK,
            operation_id="plain-production",
            frontier={},
            target="x/y#1",
            prompt="x",
        )
    with pytest.raises(ValueError):
        closer._controlled_launch_inputs(
            profile=world.profile,
            record=record,
            task_type=admission.TASK,
            operation_id=admission.PREFIX + "fake",
            frontier={},
            target="x/y#1",
            prompt="x",
            qualification=True,
        )
    assert not world.calls


def test_current_registry_remains_required_for_production(world):
    with pytest.raises(closer.ControlledOriginUnavailable):
        closer._controlled_launch_options(
            profile_id=admission.PROFILE_ID,
            task_type=admission.TASK,
            operation_id="production",
            frontier={},
            target="x/y#1",
            prompt="x",
        )
    assert not world.calls


def test_deadline_is_bounded_before_any_callback(world):
    for deadline in (time.time() - 1, time.time() + 91, float("inf"), True):
        with pytest.raises(ValueError):
            admission.run_current_source_public_qualification(
                world.proposal, quality_ref=world.quality_ref, deadline=deadline
            )
    assert not world.calls


def test_expired_opaque_permit_cannot_be_reused_or_exported(world):
    value = run(world)
    report = admission._read(value["report_ref"])
    permit = world.control["captured_permit"]
    assert permit.active is False and admission._CURRENT.get() is None
    assert "permit" not in report
    with pytest.raises(ValueError):
        closer._validate_controlled_result(
            report["result"],
            report["callback_proof"]["expected"],
            report["callback_proof"]["observed"],
            receipt_root=world.logs,
            _qualification=permit,
        )


def test_registry_data_is_excluded_but_driver_source_is_in_contract(world, monkeypatch):
    before = closer._controlled_model_contract(admission.TASK)
    assert "closer_claude_role_admission.py" in before["sources"]
    assert "closer_role_qualification.py" not in before["sources"]
    assert "execution_profiles.py" not in before["sources"]
    monkeypatch.setattr(roles, "ROLE_QUALIFICATIONS", {"inert": {"status": "pending_review"}})
    assert closer._controlled_model_contract(admission.TASK) == before


def test_report_modification_or_current_contract_change_holds_receiver(world, monkeypatch):
    value = run(world)
    actual = closer._controlled_model_contract

    def changed(task):
        descriptor = actual(task)
        descriptor["contract_sha256"] = "0" * 64
        return descriptor

    monkeypatch.setattr(closer, "_controlled_model_contract", changed)
    with pytest.raises(ValueError):
        admission.reviewed_record_proposal(value["report_ref"])
    monkeypatch.setattr(closer, "_controlled_model_contract", actual)
    path = Path(value["report_ref"]["path"])
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(ValueError):
        admission.reviewed_record_proposal(value["report_ref"])


def test_receiver_has_no_caller_expiry_or_enrollment_argument(world):
    value = run(world)
    with pytest.raises(TypeError):
        admission.reviewed_record_proposal(value["report_ref"], expires_at=time.time() + 900)
    assert roles.ROLE_QUALIFICATIONS == {}


def test_huge_original_input_is_refused_before_native_callback(world):
    world.proposal["original"]["unused"] = "x" * admission.MAX_PROPOSAL_BYTES
    with pytest.raises(ValueError):
        run(world)
    assert not world.calls


def test_real_quality_reader_requires_physical_exact_scoped_record(tmp_path, monkeypatch):
    profile = execution_profiles.get_profile(admission.PROFILE_ID)
    record = {
        "schema": "closer-scoped-role-evidence-record/v1",
        "status": "scoped_evidence_eligible_not_runtime_enrolled",
        "scope": {
            "profile_identity": roles.profile_identity(profile),
            "role": admission.ROLE,
            "named_question_only": True,
        },
    }
    path = tmp_path / "inert-quality.json"
    raw = json.dumps(record).encode()
    path.write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    monkeypatch.setattr(admission, "QUALITY_SHA256", digest)
    scope = {
        "subject": "sonnet_high_v2",
        "quality_evidence": {"sha256": digest},
        "runtime_enrollment_granted": False,
        "action_authority": False,
    }
    monkeypatch.setattr(selection, "quality_scope", lambda *_a: scope)
    ref = {"path": str(path.resolve()), "bytes": len(raw), "sha256": digest}
    assert _ACTUAL_QUALITY(ref) == (profile, scope)
    path.write_bytes(raw + b" ")
    with pytest.raises(ValueError):
        _ACTUAL_QUALITY(ref)
    with pytest.raises(ValueError):
        _ACTUAL_QUALITY(None)


def test_known_prelaunch_refusal_resolves_only_original_held_pool(world, monkeypatch):
    def held(*_a, **_kw):
        raise closer.ControlledOriginUnavailable("inert prelaunch refusal")

    monkeypatch.setattr(closer, "_controlled_launch_inputs", held)
    value = run(world)
    assert not value["capability_pass"] and not world.calls
    root = world.logs.parent / "closer-profile-execution"
    rows = [json.loads(p.read_text()) for p in root.glob("pools/*/*.json")]
    assert len(rows) == 1 and rows[0]["status"] == "resolved"
    assert rows[0]["resolution"] == "trusted_no_spawn"


def test_existing_outer_pool_cannot_be_reused_as_qualification_authority(world):
    reservation = {
        "reservation_id": "outer-inert",
        "frontier_id": "outer",
        "target": "another/repo#1",
        "agent": "claude",
        "profile_id": admission.PROFILE_ID,
    }
    with resources.pool_slot(
        world.logs.parent / "separate-inert-pool",
        "outer-lane",
        1,
        deadline=time.time() + 5,
        finite_frontier=True,
        reservation=reservation,
    ):
        with pytest.raises(ValueError):
            run(world)
        assert resources._pool_slots()[0]["record"]["status"] == "starting"
        resources.finite_pool_no_start(
            expected={k: reservation[k] for k in ("agent", "profile_id", "frontier_id", "target")}
        )
    assert not world.calls


@pytest.mark.parametrize(
    "world",
    [(Path(__file__).parent / "fixtures/sonnet_role_admission/original-983-body.txt").read_text()],
    indirect=True,
)
def test_unchanged_real983_body_reaches_current_request_and_protected_receiver(world):
    original = copy.deepcopy(world.proposal["original"])
    value = run(world)
    assert value["capability_pass"]
    report = admission._read(value["report_ref"])
    assert report["request"]["criterion"] == admission.PUBLIC_CRITERION
    assert len(original["decision"]["criteria"]) == 7  # whole BODY plus all six clauses
    assert world.proposal["original"] == original
    proposed = admission.reviewed_record_proposal(value["report_ref"])
    assert proposed["record"]["status"] == "pending_review"
    assert proposed["record"]["expires_at"] is None
    assert roles.ROLE_QUALIFICATIONS == {}
