"""Synthetic local proof/refusal contracts; no provider, auth reader, or qualification."""

import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_closer_reasoning_boundary import native_world as native_world
from test_dispatcher_controlled_data_only import world as _controlled_world

import closer_data_only_execution as execution
import closer_dispatcher as closer
import closer_role_qualification as roles
import execution_profiles
import transport_saved_result

controlled_world = _controlled_world


@pytest.fixture
def proof(tmp_path):
    profile = execution_profiles.get_profile("claude-sonnet-5-5-low")
    identity = roles.profile_identity(profile)
    descriptor = closer._controlled_model_contract("closer_interpret")
    limits = {
        "schema": "closer-data-only-limits/v1",
        "role": "bounded_classification",
        "model_contract_sha256": descriptor["contract_sha256"],
        "max_prompt_bytes": 65536,
        "max_final_bytes": 8192,
        "max_native_result_bytes": 65536,
        "max_output_tokens": 2048,
        "max_turns": 1,
        "timeout_seconds": 45,
    }  # Explicit synthetic component limits; never a production default.
    prompt = "Actual bounded request supplied by this offline fixture."
    root = str(tmp_path / "runtime")
    cwd = root + "/cwd"
    env = execution.isolated_environment(
        root, cwd=cwd, existing_oauth_token="synthetic-component-token", limits=limits
    )
    launch = execution.validate_launch(
        agent="claude",
        profile=identity,
        binary_path="/tmp/inert-cli",
        binary_sha256=execution.CLAUDE_BINARY_SHA256,
        argv=execution.expected_argv("/tmp/inert-cli", identity, limits=limits),
        env=env,
        cwd=cwd,
        private_root=root,
        response_protocol=execution.NATIVE_JSON,
        source_sha256={
            name: descriptor["sources"][name]
            for name in ("adapters.py", "dispatcher.py", "closer_data_only_execution.py")
        },
        limits=limits,
        prompt=prompt,
        timeout_seconds=20,
    )
    output = json.dumps(
        {
            "target": "offline/repo#1",
            "head": "a" * 40,
            "base": "b" * 40,
            "classification": "uncertain",
            "substantive_needs": ["one criterion"],
            "findings": [],
            "decision": {"criteria": []},
        }
    )
    native = json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "num_turns": 1,
            "terminal_reason": "completed",
            "stop_reason": "end_turn",
            "session_id": "offline-session",
            "result": output,
            "total_cost_usd": 0.01,
            "usage": {
                "input_tokens": 10,
                "output_tokens": 20,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
            },
            "modelUsage": {
                identity["requested_model"]: {
                    "inputTokens": 10,
                    "outputTokens": 20,
                    "cacheReadInputTokens": 0,
                    "cacheCreationInputTokens": 0,
                    "costUSD": 0.01,
                }
            },
        }
    )
    observed = {
        "source": "trusted-popen-transport/v1",
        "pid": 12345,
        "process_group": 12345,
        "os_started": "offline-process-start",
        "run_id": "original-run",
        "cleanup_terminal": True,
        "agent": "claude",
        "operation_id": "original-op",
        "profile_id": profile["profile_id"],
        "task_type": "closer_interpret",
        "finite_frontier_id": "original-frontier",
        "finite_frontier_target": "offline/repo#1",
        "data_only_execution_binding_sha256": launch["binding_sha256"],
    }
    reservation = {
        "reservation_id": "original-reservation",
        "lane": hashlib.sha256(b"claude-subscription").hexdigest(),
        "frontier_id": "original-frontier",
        "target": "offline/repo#1",
        "agent": "claude",
        "profile_id": profile["profile_id"],
    }
    expected = {
        "schema": "closer-controlled-response-expectation/v1",
        "operation_id": "original-op",
        "task_type": "closer_interpret",
        "profile_id": profile["profile_id"],
        "profile_identity": identity,
        "finite_frontier_id": "original-frontier",
        "finite_frontier_target": "offline/repo#1",
        "pool_reservation": reservation,
        "production_model_contract": descriptor,
        "limits": limits,
        "prompt": execution.validate_payload(prompt, limits=limits, kind="prompt"),
        "qualification_id": "synthetic-component-not-production",
        "qualification_record_sha256": "e" * 64,
    }
    origin = {
        "schema": "closer-data-only-runtime-origin/v1",
        "source": "existing-dispatcher-observed-supervisor",
        "authority": False,
        "classification_validated": False,
        "pool_id": "claude-subscription",
        **{
            key: expected[key]
            for key in (
                "operation_id",
                "task_type",
                "profile_id",
                "finite_frontier_id",
                "finite_frontier_target",
                "pool_reservation",
            )
        },
        "run_id": "original-run",
        "process_observation": observed,
        "execution": launch,
        "collector_sources": {
            name: descriptor["sources"][name]
            for name in ("transport_saved_result.py", "transport_qualification.py")
        },
        "native_json": native,
        "native_stdout_sha256": hashlib.sha256(native.encode()).hexdigest(),
        "native_stdout_bytes": len(native.encode()),
        "final": execution.validate_payload(output, limits=limits, kind="final"),
        "normalized_native_result": transport_saved_result.normalize_claude_result(
            native,
            run_id="original-run",
            pool_id="claude-subscription",
            requested_model=identity["requested_model"],
            requested_effort=identity["reasoning_effort"],
        ),
    }
    result = {
        "exit": 0,
        "output": output,
        "run_id": "original-run",
        "process_started": True,
        "data_only_private_runtime": {"path": root, "cleanup": "removed_owned_private_runtime"},
        "data_only_origin": origin,
        "process_observation": observed,
    }

    def retain():
        retained = {key: value for key, value in result.items() if key != "provider_result_ref"}
        receipt = {
            "schema": "offload-provider-result/v1",
            "full_result_retained": True,
            "outcome_uncertain": False,
            "run_id": result["run_id"],
            "profile_id": profile["profile_id"],
            "result": retained,
            "retained_result_sha256": hashlib.sha256(
                json.dumps(retained, sort_keys=True).encode()
            ).hexdigest(),
        }
        raw = json.dumps(receipt).encode()
        path = tmp_path / "original.provider-result-1.json"
        path.write_bytes(raw)
        result["provider_result_ref"] = {
            "path": str(path),
            "bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
        }

    retain()
    return SimpleNamespace(
        result=result,
        expected=expected,
        observed={
            "starting": dict(observed),
            "started": dict(observed),
            "finished": dict(observed),
        },
        root=tmp_path,
        retain=retain,
    )


def validate(proof):
    return closer._validate_controlled_result(
        proof.result, proof.expected, proof.observed, receipt_root=proof.root
    )


def test_physical_original_proof_can_be_rechecked_without_offload(proof):
    first = validate(proof)
    restarted = copy.deepcopy(proof)
    assert validate(restarted) == first
    assert first["authority"] is False


@pytest.mark.parametrize(
    "field",
    [
        "operation_id",
        "profile_id",
        "task_type",
        "finite_frontier_id",
        "finite_frontier_target",
        "pool_reservation",
        "process_observation",
        "run_id",
    ],
)
def test_wrong_or_replayed_origin_refused_even_in_self_consistent_receipt(proof, field):
    proof.result["data_only_origin"][field] = "different"
    proof.retain()
    with pytest.raises(closer.ControlledOriginUnavailable):
        validate(proof)


@pytest.mark.parametrize(
    "damage",
    [
        "missing",
        "hash",
        "bytes",
        "outside",
        "symlink",
        "changed",
        "duplicate",
        "unknown",
        "callback",
        "cleanup",
        "source",
        "prompt",
        "limits",
        "native",
        "final",
    ],
)
def test_unbound_or_partial_proof_never_gets_semantic_credit(proof, damage, tmp_path):
    result = proof.result
    if damage == "missing":
        result.pop("provider_result_ref")
    elif damage == "hash":
        result["provider_result_ref"]["sha256"] = "0" * 64
    elif damage == "bytes":
        result["provider_result_ref"]["bytes"] = 1
    elif damage == "outside":
        proof.root = tmp_path / "other"
    elif damage == "symlink":
        original = Path(result["provider_result_ref"]["path"])
        link = tmp_path / "alias.json"
        link.symlink_to(original)
        result["provider_result_ref"]["path"] = str(link)
    elif damage == "changed":
        Path(result["provider_result_ref"]["path"]).write_text("{}")
    elif damage == "duplicate":
        raw = b'{"schema":"a","schema":"b"}'
        Path(result["provider_result_ref"]["path"]).write_bytes(raw)
        result["provider_result_ref"].update(bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
    elif damage == "unknown":
        result["outcome_uncertain"] = True
    elif damage == "callback":
        proof.observed["started"]["pid"] = 999
    elif damage == "cleanup":
        proof.observed["finished"]["cleanup_terminal"] = False
    elif damage == "source":
        proof.expected["production_model_contract"]["sources"]["closer_evidence.py"] = "0" * 64
    elif damage == "prompt":
        proof.expected["prompt"]["sha256"] = "0" * 64
    elif damage == "limits":
        proof.expected["limits"]["max_output_tokens"] = 2000
    elif damage == "native":
        result["data_only_origin"]["native_json"] = "{}"
    else:
        result["output"] = "{}"
    with pytest.raises(closer.ControlledOriginUnavailable):
        validate(proof)


def test_no_reviewed_production_limits_never_borrows_auth_or_spawns(monkeypatch):
    import closer_claude_auth

    monkeypatch.setattr(
        closer_claude_auth,
        "borrow_claude_oauth_token",
        lambda **_kw: pytest.fail("unqualified role borrowed auth"),
    )
    with pytest.raises(closer.ControlledOriginUnavailable):
        closer._controlled_launch_options(
            profile_id="claude-sonnet-5-5-low",
            task_type="closer_interpret",
            operation_id="original",
            frontier={"frontier_id": "one"},
            target="offline/repo#1",
            prompt="bounded",
        )
    assert roles.ROLE_QUALIFICATIONS == {}


def test_strong_descriptor_pins_real_named_evidence_policy_and_dependency_sources():
    import closer_execution_contracts as contracts

    descriptor = closer._controlled_model_contract("closer_exception")
    assert descriptor["role"] == roles.STRONG_CRITERION_ADJUDICATION
    assert (
        descriptor["model_contract"]["evidence_policy"] == contracts.ADJUDICATION_EVIDENCE_VERSION
    )
    assert (
        descriptor["model_contract"]["response_validator"]
        == "closer_execution_contracts.validate_adjudication_response"
    )
    assert (
        "closer_role_qualification.py" not in descriptor["sources"]
    )  # No registry hash fixed point.
    assert (
        descriptor["sources"]["closer_execution_contracts.py"]
        == hashlib.sha256(Path(contracts.__file__).read_bytes()).hexdigest()
    )


@pytest.mark.parametrize("field", ["semantic_promotion_blocked", "data_only_private_runtime"])
def test_unverified_private_cleanup_cannot_be_promoted(proof, field):
    proof.result[field] = (
        True
        if field == "semantic_promotion_blocked"
        else {"cleanup": "held_owned_cleanup_unverified"}
    )
    proof.retain()
    with pytest.raises(closer.ControlledOriginUnavailable):
        validate(proof)


def test_default_registry_controlled_caller_refuses_before_broker_and_auth(monkeypatch):
    import closer_resources
    import dispatcher

    monkeypatch.setattr(
        dispatcher, "offload", lambda *a, **kw: pytest.fail("unqualified broker call")
    )
    monkeypatch.setattr(closer_resources, "finite_pool_no_start", lambda: None)
    result = closer._controlled_offload(
        agent="claude",
        prompt="bounded",
        profile_id="claude-sonnet-5-5-low",
        task_type="closer_interpret",
        operation_id="original-op",
        frontier={"frontier_id": "one"},
        target="offline/repo#1",
        basis={},
        deadline=9999999999,
    )
    assert result["process_started"] is False and result["outcome_uncertain"] is False
    assert result["unsupported_assessment_tool_boundary"] is True


def test_restart_uses_original_physical_proof_without_invocation(proof, monkeypatch):
    import dispatcher

    monkeypatch.setattr(
        dispatcher, "offload", lambda *a, **kw: pytest.fail("restart invoked broker")
    )
    import closer_claude_auth

    monkeypatch.setattr(
        closer_claude_auth,
        "borrow_claude_oauth_token",
        lambda **kw: pytest.fail("restart borrowed auth"),
    )
    basis = {"target": "offline/repo#1", "revision": "original-revision"}
    proof.expected["basis"] = basis
    checked = validate(proof)
    assert (
        closer._revalidate_controlled_proof(
            checked, run_id="original-run", basis=basis, receipt_root=proof.root
        )["output"]
        == proof.result["output"]
    )
    with pytest.raises(closer.ControlledOriginUnavailable):
        closer._revalidate_controlled_proof(
            checked, run_id="new-run", basis=basis, receipt_root=proof.root
        )
    with pytest.raises(closer.ControlledOriginUnavailable):
        closer._revalidate_controlled_proof(
            checked,
            run_id="original-run",
            basis={**basis, "revision": "new"},
            receipt_root=proof.root,
        )


@pytest.mark.parametrize(
    "damage", ["argv", "ambient_env", "tool_control", "text_protocol", "retry", "timeout", "binary"]
)
def test_self_consistent_config_tampering_still_fails_actual_contract(proof, damage):
    origin = proof.result["data_only_origin"]
    launch = origin["execution"]
    config, binding = launch["config"], launch["binding"]
    if damage == "argv":
        config["argv"].append("--dangerously-skip-permissions")
        binding["argv_sha256"] = closer._contract_digest(config["argv"])
    elif damage == "ambient_env":
        config["environment"]["MCP_CONFIG"] = "ambient"
        binding["environment_sha256"] = closer._contract_digest(config["environment"])
    elif damage == "tool_control":
        config["control_presence"] = {}
    elif damage == "text_protocol":
        config["response_protocol"] = binding["response_protocol"] = "text"
    elif damage == "retry":
        config["automatic_retries"] = 1
    elif damage == "timeout":
        config["timeout_seconds"] = float("inf")
    else:
        binding["binary_sha256"] = "0" * 64
    try:
        binding["config_sha256"] = closer._contract_digest(config)
        launch["binding_sha256"] = closer._contract_digest(binding)
    except ValueError:  # Nonfinite JSON may never manufacture a new valid binding.
        pass
    proof.retain()
    with pytest.raises(closer.ControlledOriginUnavailable):
        validate(proof)


def test_completed_broker_without_origin_is_owned_uncertainty_not_retry(proof, monkeypatch):
    import time

    import closer_resources
    import dispatcher

    called = []
    monkeypatch.setattr(
        closer, "_controlled_launch_options", lambda **kw: ({}, proof.expected, proof.observed)
    )
    monkeypatch.setattr(
        closer_resources,
        "finite_pool_no_start",
        lambda: pytest.fail("completed call called no-start"),
    )
    result = copy.deepcopy(proof.result)
    result.pop("data_only_origin")

    def broker(*args, **kwargs):
        called.append(kwargs)
        return result

    monkeypatch.setattr(dispatcher, "offload", broker)
    basis = {
        "target": "offline/repo#1",
        "revision": "original",
        "head": "a" * 40,
        "base": "b" * 40,
        "packet_sha256": "c" * 64,
    }
    held = closer._controlled_offload(
        agent="claude",
        prompt="bounded",
        profile_id="claude-sonnet-5-5-low",
        task_type="closer_interpret",
        operation_id="original-op",
        frontier={"frontier_id": "one", "bindings": {"offline/repo#1": {"revision": "original"}}},
        target="offline/repo#1",
        basis=basis,
        deadline=time.time() + 30,
    )
    assert len(called) == 1 and held["outcome_uncertain"] is True
    assert held["retry_safe"] is False and held["provider_completed"] is True
    assert (
        held["run_id"] == "original-run"
        and held["provider_result_ref"] == result["provider_result_ref"]
    )


@pytest.mark.parametrize("task_type", ["closer_interpret", "closer_exception"])
def test_actual_inert_supervisor_caller_receipt_semantics_and_restart(
    tmp_path, monkeypatch, controlled_world, task_type
):
    """Actual native collector/broker/lease; synthetic qualification+auth only here."""
    import time

    from test_closer_disposition import TARGET, fixture
    from test_closer_role_qualification import record

    import adapters
    import closer_claude_auth
    import closer_disposition as disposition
    import closer_evidence as evidence
    import closer_execution_contracts as contracts
    import closer_model_contracts as model
    import closer_resources as resources
    import dispatcher

    world = controlled_world
    snapshot, packet, packet_ref, prep, low = fixture(tmp_path / "actual-evidence")
    proposal = copy.deepcopy(low["proposal"])
    request = None
    if task_type == "closer_interpret":
        readiness = evidence.action_evidence_readiness(
            snapshot,
            packet,
            "collect_evidence",
            retained_dir=Path(packet_ref["path"]).parent.parent,
        )
        import closer_semantic_assessment as semantic

        prompt = model.render_classification_prompt(
            {
                "target": TARGET,
                "head": snapshot["head"],
                "base": snapshot["base"],
                "packet_ref": packet_ref,
                "snapshot_revision": snapshot["revision"],
                "manifest": packet["manifest"],
                "section_refs": packet["sections"],
                "data_only_evidence": closer.data_only_evidence_context(
                    packet, readiness=readiness, snapshot=snapshot
                ),
            },
            source_rows=packet["sections"]["source"]["content"],
            diff_rows={},
            review_rows=[],
            artifact_rows={},
            data_only=True,
            readiness=readiness,
            semantic_inventory=semantic.catalog_input(
                snapshot, packet, packet_ref, Path(packet_ref["path"]).parent.parent
            ),
        )
        response = proposal
    else:
        selected = proposal["decision"]["criteria"][1]
        selected["result"] = "UNKNOWN"
        request = contracts.build_adjudication_request(
            snapshot,
            packet,
            packet_ref,
            basis={"source": selected["source"], "criterion": selected["criterion"]},
            question="Does this retained receipt prove the named report criterion?",
            evidence_refs=selected["evidence"],
            retained_dir=Path(packet_ref["path"]).parent.parent,
        )
        prompt = contracts.build_adjudication_prompt(
            proposal, request, retained_dir=Path(packet_ref["path"]).parent.parent
        )
        response = {
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
            "reason": "Exact retained test exercises the named report criterion.",
            "evidence": selected["evidence"],
            "proofs": selected["proofs"],
        }
    script = world.cli.read_text()
    script = (
        "\n".join(
            "final=" + repr(json.dumps(response)) if line.startswith("final=json.dumps(") else line
            for line in script.splitlines()
        )
        + "\n"
    )
    world.cli.write_text(script)
    binary_sha = hashlib.sha256(world.cli.read_bytes()).hexdigest()
    monkeypatch.setattr(adapters, "DATA_ONLY_CLAUDE_SHA256", binary_sha)
    monkeypatch.setattr(execution, "CLAUDE_BINARY_SHA256", binary_sha)
    profile = execution_profiles.get_profile(world.args["profile_id"])
    descriptor = closer._controlled_model_contract(task_type)
    limits = {
        **world.args["data_only_limits"],
        "role": roles.TASK_ROLES[task_type],
        "model_contract_sha256": descriptor["contract_sha256"],
    }
    qualified = record(profile, roles.TASK_ROLES[task_type])
    qualified.update(
        qualification_id="synthetic-inert-component",
        qualified_at=time.time() - 10,
        expires_at=time.time() + 60,
        execution_limits=limits,
        production_model_contract=descriptor,
    )
    qualified["confinement"]["adapter_build_sha256"] = binary_sha
    monkeypatch.setattr(roles, "ROLE_QUALIFICATIONS", {"synthetic-inert-component": qualified})
    monkeypatch.setattr(
        closer_claude_auth,
        "borrow_claude_oauth_token",
        lambda **kw: world.args["trusted_auth_supplier"](),
    )
    basis = {
        "target": TARGET,
        "revision": snapshot["revision"],
        "head": snapshot["head"],
        "base": snapshot["base"],
        "packet_sha256": packet_ref["sha256"],
    }
    if request:
        basis["request_id"] = request["request_id"]
    frontier = {
        "frontier_id": "original-frontier",
        "bindings": {TARGET: {"revision": snapshot["revision"]}},
    }
    with resources.pool_slot(
        world.root / "pools",
        hashlib.sha256(b"claude-subscription").hexdigest(),
        1,
        deadline=time.time() + 10,
        finite_frontier=True,
        reservation={
            "reservation_id": "original-reservation",
            "target": TARGET,
            "frontier_id": "original-frontier",
            "agent": "claude",
            "profile_id": profile["profile_id"],
        },
    ):
        result = closer._controlled_offload(
            agent="claude",
            prompt=prompt,
            profile_id=profile["profile_id"],
            task_type=task_type,
            operation_id="original-component-operation",
            frontier=frontier,
            target=TARGET,
            basis=basis,
            deadline=time.time() + 20,
            mode="assess",
        )
    assert result["exit"] == 0, result
    checked = result["_closer_controlled_proof"]
    assert (
        checked["observed"]["starting"]["data_only_execution_binding_sha256"]
        == result["data_only_origin"]["execution"]["binding_sha256"]
    )
    assert (
        checked["expected"]["prompt"]["sha256"]
        == result["data_only_origin"]["execution"]["config"]["prompt"]["sha256"]
    )
    assert checked["expected"]["operation_id"] == "original-component-operation"
    assert world.events["auth"] == world.events["auth_closed"] == 1
    original_capture = world.capture.read_bytes()
    monkeypatch.setattr(
        dispatcher, "offload", lambda *a, **kw: pytest.fail("restart resubmitted original call")
    )
    restored = closer._revalidate_controlled_proof(
        checked, run_id=result["run_id"], basis=basis, receipt_root=dispatcher.DISPATCH_LOG_DIR
    )
    assert restored["output"] == result["output"] and world.capture.read_bytes() == original_capture
    parsed = closer._worker_proposal(restored, agent="claude")
    if request:
        answer = contracts.validate_adjudication_response(
            request,
            parsed,
            snapshot=snapshot,
            packet=packet,
            packet_ref=packet_ref,
            retained_dir=Path(packet_ref["path"]).parent.parent,
        )
        parsed = contracts.merge_selected_response(proposal, request, answer)
    integrated = disposition.integrate_ordinary(
        TARGET,
        snapshot,
        packet_ref,
        prep,
        {"actual": {"exit": 0, "run_id": result["run_id"]}, "proposal": parsed},
    )
    assert integrated["status"] == "accepted", integrated
    assert result["data_only_origin"]["authority"] is False
    assert result["data_only_origin"]["classification_validated"] is False


def test_actual_registered_pipeline_missing_contract_stops_before_broker(
    native_world, monkeypatch, capsys
):
    """Original synthetic role oracle confers no new execution-contract authority."""
    import dispatcher

    world = native_world
    # native_world now installs complete, hash-bound inert launch records.
    # Deliberately remove only execution-contract authority for this negative;
    # the real role qualification must still pass independently.
    records = copy.deepcopy(roles.ROLE_QUALIFICATIONS)
    for record in records.values():
        record.pop("execution_limits", None)
        record.pop("production_model_contract", None)
    monkeypatch.setattr(roles, "ROLE_QUALIFICATIONS", records)
    profile = execution_profiles.get_profile("claude-sonnet-5-5-low")
    assert roles.is_runtime_qualified(profile, roles.BOUNDED_CLASSIFICATION)
    monkeypatch.setattr(
        dispatcher,
        "offload",
        lambda *a, **kw: pytest.fail("missing controlled contract called broker"),
    )
    closer.main(world.argv)
    row = json.loads(capsys.readouterr().out)["targets"][world.snapshot["target"]]
    interpreted = row["results"]["interpret"]
    assert interpreted["classification"] == "role_qualification_unavailable", row
    assert interpreted["pending_kind"] == "external_event"
    assert interpreted["pending_evidence"]["target"] == world.snapshot["target"]
    assert interpreted["pending_evidence"]["revision"] == world.snapshot["revision"]
    assert world.events["provider"] == world.events["effect"] == 0


def test_recomputed_launch_must_match_original_callback_binding(proof):
    validate(proof)
    launch = proof.result["data_only_origin"]["execution"]
    launch["config"]["timeout_seconds"] = 19
    launch["binding"]["config_sha256"] = closer._contract_digest(launch["config"])
    launch["binding_sha256"] = closer._contract_digest(launch["binding"])
    proof.retain()
    with pytest.raises(closer.ControlledOriginUnavailable):
        validate(proof)


def test_rebound_expected_and_origin_operation_cannot_replace_actual_callback(proof):
    proof.expected["operation_id"] = "foreign-operation"
    proof.result["data_only_origin"]["operation_id"] = "foreign-operation"
    proof.retain()
    with pytest.raises(closer.ControlledOriginUnavailable):
        validate(proof)


@pytest.mark.parametrize(
    "field",
    [
        "operation_id",
        "profile_id",
        "task_type",
        "finite_frontier_id",
        "finite_frontier_target",
        "data_only_execution_binding_sha256",
        "agent",
    ],
)
def test_started_finished_callback_contract_must_remain_identical(proof, field):
    proof.observed["started"][field] = "foreign-original-callback"
    with pytest.raises(closer.ControlledOriginUnavailable):
        validate(proof)


@pytest.mark.parametrize("path", [None, "/foreign/unrelated-runtime", "/tmp/../foreign-runtime"])
def test_private_cleanup_proof_must_name_original_launch_root(proof, path):
    if path is None:
        proof.result["data_only_private_runtime"].pop("path")
    else:
        proof.result["data_only_private_runtime"]["path"] = path
    proof.retain()
    with pytest.raises(closer.ControlledOriginUnavailable):
        validate(proof)


def test_token_equal_prompt_refuses_without_hash_or_start(controlled_world, monkeypatch):
    """Synthetic reviewed-role/pool component; no actual credential reader or child."""
    import time

    from test_closer_role_qualification import record

    import closer_claude_auth
    import dispatcher

    world = controlled_world
    profile = execution_profiles.get_profile(world.args["profile_id"])
    descriptor = closer._controlled_model_contract("closer_interpret")
    limits = {
        **world.args["data_only_limits"],
        "model_contract_sha256": descriptor["contract_sha256"],
    }
    qualified = record(profile, roles.BOUNDED_CLASSIFICATION)
    qualified.update(
        qualification_id="synthetic-token-exclusion",
        qualified_at=time.time() - 10,
        expires_at=time.time() + 60,
        execution_limits=limits,
        production_model_contract=descriptor,
    )
    qualified["confinement"]["adapter_build_sha256"] = hashlib.sha256(
        world.cli.read_bytes()
    ).hexdigest()
    monkeypatch.setattr(roles, "ROLE_QUALIFICATIONS", {"synthetic-token-exclusion": qualified})
    monkeypatch.setattr(
        closer_claude_auth,
        "borrow_claude_oauth_token",
        lambda **kw: world.args["trusted_auth_supplier"](),
    )
    real_sha = hashlib.sha256
    secret_hashes = []

    def guarded_sha(data=b"", *args, **kwargs):
        if world.token.encode() in data:
            secret_hashes.append(True)
            pytest.fail("known-not-started caller hashed borrowed synthetic token")
        return real_sha(data, *args, **kwargs)

    monkeypatch.setattr(hashlib, "sha256", guarded_sha)
    with world.lease():
        result = closer._controlled_offload(
            agent="claude",
            prompt=world.token,
            profile_id=profile["profile_id"],
            task_type="closer_interpret",
            operation_id="original-op",
            frontier={
                "frontier_id": "original-frontier",
                "bindings": {"offline/repo#1": {"revision": "original"}},
            },
            target="offline/repo#1",
            basis={
                "target": "offline/repo#1",
                "revision": "original",
                "head": "a" * 40,
                "base": "b" * 40,
                "packet_sha256": "c" * 64,
            },
            deadline=time.time() + 20,
            mode="assess",
        )
    assert result["process_started"] is False and result["outcome_uncertain"] is False
    assert world.events["auth"] == world.events["auth_closed"] == 1
    assert not secret_hashes and not world.capture.exists() and world.events["ledger"] == []
    assert result["run_id"] is None and not list(
        dispatcher.DISPATCH_LOG_DIR.glob("*.provider-result-*.json")
    )


@pytest.mark.parametrize("prompt", [None, "", False, "\ud800", "界" * 20])
def test_prompt_preflight_is_hash_free_and_strict(prompt, monkeypatch):
    monkeypatch.setattr(hashlib, "sha256", lambda *a, **kw: pytest.fail("preflight hashed prompt"))
    with pytest.raises(closer.ControlledOriginUnavailable):
        closer._controlled_prompt_size(prompt, {"max_prompt_bytes": 10})


def test_restart_checks_sealed_source_context_before_reading_provider_text(proof, monkeypatch):
    basis = {"target": "offline/repo#1", "revision": "original"}
    proof.expected["basis"] = basis
    checked = validate(proof)
    checked["expected"]["production_model_contract"]["sources"]["closer_model_contracts.py"] = (
        "0" * 64
    )
    monkeypatch.setattr(
        closer,
        "_read_controlled_receipt",
        lambda *a, **kw: pytest.fail("foreign source read retained provider text"),
    )
    with pytest.raises(closer.ControlledOriginUnavailable):
        closer._revalidate_controlled_proof(
            checked, run_id="original-run", basis=basis, receipt_root=proof.root
        )


def test_exported_native_fixture_reuses_explicit_controlled_world(native_world, controlled_world):
    assert native_world.inert_caller.world is controlled_world
    assert controlled_world.events["auth"] == 0
    assert controlled_world.events["started"] == []
    assert native_world.events["provider"] == native_world.events["effect"] == 0
