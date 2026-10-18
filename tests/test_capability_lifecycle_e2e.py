from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest
from capability_dispatch_fixture import local_dispatch
from test_evidence_contract_compiler import _plan as evidence_contract_plan
from test_evidence_contract_compiler import gap_rows as evidence_gap_rows
from test_playbook_compiler import REPO as PLAYBOOK_REPO
from test_playbook_compiler import _candidate as playbook_candidate
from test_playbook_compiler import _write_registry as write_playbook_registry
from test_role_compiler import _candidate as role_candidate

import capabilities
import capability_compiler as compiler
import capability_lifecycle
import capability_targets
import env_prereq
import feedback
import roles


def _active_predecessor(capability_id: str, now: int) -> dict:
    cap = capabilities._blank_capability(capability_id)
    cap.update(
        {
            "status": "active",
            "owner": "orchestrator",
            "matcher": {"repository": "owner/repo", "task_types": ["implement"]},
            "entrypoint": "baseline.py:run",
            "trigger_cadence": "per matching task",
            "output_artifact": "baseline-result.json",
            "downstream_consumer": "dispatcher.py",
            "learning_sink": "feedback outcomes",
            "activation_evidence": {
                probe: {"passed": True, "checked_at": now, "ref": f"baseline:{probe}"}
                for probe in capabilities.ACTIVE_PROBES
            },
            "last_match": now,
            "last_invocation": now,
            "last_success": now,
            "outcome_links": ["outcome:baseline"],
            "expiry": now + 365 * 86400,
            "kill_switch": "ORCH_BASELINE=0",
            "rollback": {"transition": "retired", "predecessor": "manual"},
        }
    )
    return cap


def _gate_plan(now: int) -> dict:
    del now
    return evidence_contract_plan(evidence_gap_rows.__wrapped__())


def _target_fixture(kind: str, root: Path, now: int) -> dict:
    if kind == "workflow":
        artifact = compiler.compile_workflow_rail(compiler.reference_workflow_source())
        return {
            "artifact": artifact,
            "matcher": {"repository": "owner/repo", "task_types": ["implement"]},
            "context": {},
            "trigger": {"task_type": artifact["selector"]["value"][0], "target": "owner/repo#23"},
            "inputs": {},
        }
    if kind == "skill":
        package = compiler.compile_skill_package(
            compiler.reference_skill_source(), output_root=root / "skill-candidates"
        )
        manifest = package["manifest"]
        return {
            "artifact": Path(package["package_path"]),
            "matcher": {"repository": "owner/repo", "task_types": ["implement"]},
            "context": {},
            "trigger": {"skill_name": manifest["name"], "target": "owner/repo#23"},
            "inputs": {"artifact_refs": []},
        }
    if kind == "role":
        manifest = compiler.compile_role_capability(role_candidate(now=now))["manifest"]
        return {
            "artifact": manifest,
            "matcher": {"repository": "owner/repo", "task_types": ["review"]},
            "context": {},
            "trigger": {"task_type": "review", "target": "owner/repo#23"},
            "inputs": {
                "context": {
                    "task_type": "review",
                    "evidence_refs": ["artifact:verification-1"],
                    "question": "Which evidence resolves this dispute?",
                },
                "proposal": {
                    "decision": "inspect",
                    "evidence_refs": ["artifact:verification-1"],
                    "rationale": "Inspect the named verifier artifact before changing the baseline.",
                },
                "backend_agent": "codex",
                "env": {},
            },
        }
    if kind == "playbook":
        repo_root = root / "playbook-repo"
        (repo_root / "docs" / "ci").mkdir(parents=True)
        (repo_root / "docs" / "ci" / "WORKFLOWS.md").write_text(
            "workflow_name is the canonical registry symbol.\n", encoding="utf-8"
        )
        (repo_root / "AGENTS.md").write_text(
            "# User-authored instructions\n\nPreserve this.\n", encoding="utf-8"
        )
        repo_registry = root / "repo-knowledge.json"
        write_playbook_registry(repo_registry)
        artifact = compiler.compile_playbook_capability(
            playbook_candidate(repo_root, now=now),
            repo_root=repo_root,
            registry_path=repo_registry,
            now=now,
        )
        return {
            "artifact": artifact,
            "matcher": {"repository": PLAYBOOK_REPO, "task_types": ["implement"]},
            "context": {
                "repo_root": str(repo_root),
                "repo_registry_path": str(repo_registry),
                "capability_ledger_path": str(root / "playbook-target-ledger.json"),
            },
            "trigger": {
                "repository": PLAYBOOK_REPO,
                "task_type": "implement",
                "lane": "opener",
                "target": f"{PLAYBOOK_REPO}#23",
            },
            "inputs": {},
        }
    artifact = _gate_plan(now)
    return {
        "artifact": artifact,
        "matcher": {"repository": "owner/repo", "task_types": ["verify"]},
        "context": {},
        "trigger": {
            "kind": "acceptance_gate",
            "named_test_id": artifact["named_test_id"],
            "target": "owner/repo#23",
        },
        "inputs": {
            "result": {
                "named_test_id": artifact["named_test_id"],
                "status": "PASS",
                "result_hash": "sha256:" + "0" * 64,
                "deliberate_break_status": "PASS",
                "duration_ms": 17,
            }
        },
    }


def _record_target_run(run_id: str, subject: str) -> None:
    feedback.record_run(
        run_id,
        subject,
        "implement",
        "codex",
        routing_metadata={"subject_id": subject},
    )


def test_all_target_kinds_complete_shadow_canary_lifecycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The `skill` kind compiles the reference skill package, which hashes a REAL installed skill
    # resource under ~/.codex/skills — deliberately, so the compiler is exercised against a
    # genuine file rather than a fixture. Without it installed there is no skill to take through
    # the lifecycle, and the other four kinds are covered by their own tests.
    env_prereq.require(env_prereq.skill_resource_absent())
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "brain.db")
    now = int(time.time())
    for kind in ("role", "workflow", "skill", "playbook", "gate"):
        root = tmp_path / kind
        root.mkdir()
        ledger = root / "capabilities.json"
        target_registry = root / "targets.json"
        predecessor = f"baseline:{kind}"
        capabilities.save({predecessor: _active_predecessor(predecessor, now)}, ledger)
        fixture = _target_fixture(kind, root, now)
        registered = capability_lifecycle.register_compiled_target(
            kind,
            fixture["artifact"],
            lifecycle_policy={
                "min_independent_durable_reuse": 2,
                "max_failures": 0,
                "max_rework": 0,
                "tasks_per_day": 1,
                "risk_level": "low_reversible",
                "side_effect_policy": "read_only",
            },
            matcher=fixture["matcher"],
            predecessor=predecessor,
            ledger_path=ledger,
            target_registry_path=target_registry,
            target_context=fixture["context"],
            expiry=now + 86400,
        )
        capability_id = registered["capability"]["capability_id"]
        assert registered["status"] == "wired"
        # Every compiled kind owns its expiry through its manifest lifecycle, `gate` included
        # (it records no artifact key), so a ledger renewal must refuse each one.
        blocker = capabilities.renewal_blocker(registered["capability"], now=now)
        assert blocker and "compile and register" in blocker, (kind, blocker)
        assert (
            registered["binding"]["capability_version_id"]
            == registered["capability"]["capability_version_id"]
        )
        no_match = capability_lifecycle.invoke_compiled_target(
            capability_id,
            trigger={},
            target_run_id=f"{kind}:no-match",
            ledger_path=ledger,
            target_registry_path=target_registry,
            inputs=fixture["inputs"],
            timestamp=now,
        )
        assert no_match == {"matched": False, "invoked": False, "reason": "selector_mismatch"}

        first_run = f"{kind}:shadow:1"
        _record_target_run(first_run, f"{kind}:subject:1")
        if kind == "role":
            roles.reset_role_invocation_counts()
        first = capability_lifecycle.invoke_compiled_target(
            capability_id,
            trigger=fixture["trigger"],
            target_run_id=first_run,
            ledger_path=ledger,
            target_registry_path=target_registry,
            inputs=fixture["inputs"],
            timestamp=now,
        )
        assert first["invoked"] and first["accepted"] and first["consumer_ref"], (kind, first)
        assert first["causal_edge"]["edge_id"].startswith("edge:")
        feedback.record_outcome(
            first_run, adjudicated_verdict="PASS", merged=True, durability="durable"
        )
        shadow = capability_lifecycle.reconcile_capability(
            capability_id,
            ledger_path=ledger,
            target_registry_path=target_registry,
            timestamp=now + 1,
        )
        assert shadow["status"] == "exercised"
        capability_lifecycle.start_canary(
            capability_id, ledger_path=ledger, evidence_ref=first["causal_edge"]["edge_id"]
        )

        second_run = f"{kind}:canary:2"
        _record_target_run(second_run, f"{kind}:subject:2")
        if kind == "role":
            roles.reset_role_invocation_counts()
        second = capability_lifecycle.invoke_compiled_target(
            capability_id,
            trigger={**fixture["trigger"], "target": f"owner/repo#{kind}-2"},
            target_run_id=second_run,
            ledger_path=ledger,
            target_registry_path=target_registry,
            inputs=fixture["inputs"],
            timestamp=now + 2,
        )
        assert second["invoked"] and second["accepted"]
        before_outcome = feedback.capability_causal_evidence(
            capability_id, registered["capability"]["capability_version_id"]
        )
        assert (
            next(row for row in before_outcome if row["target_run_id"] == second_run)[
                "terminal_outcome"
            ]
            is False
        )
        quota = capability_lifecycle.invoke_compiled_target(
            capability_id,
            trigger={**fixture["trigger"], "target": f"owner/repo#{kind}-quota"},
            target_run_id=f"{kind}:canary:quota",
            ledger_path=ledger,
            target_registry_path=target_registry,
            inputs=fixture["inputs"],
            timestamp=now + 2,
        )
        assert quota == {
            "matched": True,
            "invoked": False,
            "reason": "canary_quota_exhausted",
        }
        feedback.record_outcome(
            second_run, adjudicated_verdict="PASS", merged=True, durability="durable"
        )
        promoted = capability_lifecycle.reconcile_capability(
            capability_id,
            ledger_path=ledger,
            target_registry_path=target_registry,
            timestamp=now + 3,
        )
        assert promoted["status"] == "active"

        regression_run = f"{kind}:active:3"
        _record_target_run(regression_run, f"{kind}:subject:3")
        if kind == "role":
            roles.reset_role_invocation_counts()
        third = capability_lifecycle.invoke_compiled_target(
            capability_id,
            trigger={**fixture["trigger"], "target": f"owner/repo#{kind}-3"},
            target_run_id=regression_run,
            ledger_path=ledger,
            target_registry_path=target_registry,
            inputs=fixture["inputs"],
            timestamp=now + 4,
        )
        assert third["invoked"] and third["accepted"]
        feedback.record_outcome(
            regression_run, adjudicated_verdict="PASS", merged=True, durability="durable"
        )
        feedback.record_outcome(regression_run, durability="reworked")
        rolled_back = capability_lifecycle.reconcile_capability(
            capability_id,
            ledger_path=ledger,
            target_registry_path=target_registry,
            timestamp=now + 5,
        )
        assert rolled_back["status"] == "retired"
        assert rolled_back["rollback_status"] == "verified_retired"
        retired = capabilities.load(ledger, create=False)[capability_id]
        assert retired["rollback_pending"] is None
        assert retired["rollback_result"]["rollback_proof"].startswith("sha256:")
        assert capabilities.load(ledger, create=False)[predecessor]["status"] == "active"

        if kind == "workflow":
            preserved = list(retired["outcome_links"])
            source = compiler.reference_workflow_source()
            source["capability_id"] = "capability:reference-sync-hygiene-successor"
            for step in source["steps"]:
                step["idempotency_key"] = compiler.workflow_step_idempotency_key(
                    source["capability_id"],
                    step["id"],
                    step["version"],
                    step["entrypoint"],
                    step["inputs"],
                )
            successor_plan = compiler.compile_workflow_rail(source)
            successor = capability_lifecycle.register_compiled_target(
                "workflow",
                successor_plan,
                lifecycle_policy={"risk_level": "low", "side_effect_policy": "read_only"},
                matcher=fixture["matcher"],
                predecessor=predecessor,
                ledger_path=ledger,
                target_registry_path=target_registry,
                expiry=now + 86400,
            )["capability"]
            link = capabilities.link_successor(
                capability_id, successor["capability_id"], path=ledger, timestamp=now + 6
            )
            assert link["preserved_outcome_links"] == preserved
            old = capabilities.load(ledger, create=False)[capability_id]
            assert old["status"] == "retired" and old["outcome_links"] == preserved
            assert old["successor"] == successor["capability_id"]

        if kind == "role":
            roles.unregister_generated_role("evidence-gap-prioritizer")
            roles.reset_role_invocation_counts()


def _registered_workflow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "brain.db")
    now = int(time.time())
    ledger = tmp_path / "capabilities.json"
    registry = tmp_path / "targets.json"
    predecessor = "baseline:workflow"
    capabilities.save({predecessor: _active_predecessor(predecessor, now)}, ledger)
    fixture = _target_fixture("workflow", tmp_path, now)
    registered = capability_lifecycle.register_compiled_target(
        "workflow",
        fixture["artifact"],
        lifecycle_policy={
            "min_independent_durable_reuse": 2,
            "max_failures": 0,
            "max_rework": 0,
            "tasks_per_day": 5,
            "risk_level": "low_reversible",
            "side_effect_policy": "read_only",
        },
        matcher=fixture["matcher"],
        predecessor=predecessor,
        ledger_path=ledger,
        target_registry_path=registry,
        target_context=fixture["context"],
        expiry=now + 86400,
    )
    return {
        "now": now,
        "ledger": ledger,
        "registry": registry,
        "fixture": fixture,
        "capability_id": registered["capability"]["capability_id"],
        "version_id": registered["capability"]["capability_version_id"],
        "binding": registered["binding"],
    }


def _mutate_json(path: Path, *keys: str, **fields) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    row = payload
    for key in keys:
        row = row[key]
    row.update(fields)
    path.write_text(json.dumps(payload), encoding="utf-8")


@pytest.mark.parametrize("kind", ["role", "workflow", "skill", "playbook", "gate"])
def test_ineligible_kill_switch_declared_per_kind(kind: str) -> None:
    env_name = f"ORCH_CAPABILITY_{kind.upper()}_ENABLED"
    cap = {"expiry": 9999999999, "kill_switch": f"{env_name}=0"}
    binding = {"artifact": {}, "lifecycle_policy": {}}
    assert capability_lifecycle._invocation_ineligible(cap, binding, 100, {env_name: "0"}) == (
        "kill_switch"
    )
    assert capability_lifecycle._invocation_ineligible(cap, binding, 100, {}) is None
    assert capability_lifecycle._invocation_ineligible(cap, binding, 100, {env_name: "1"}) is None


@pytest.mark.parametrize(
    ("case", "reason"),
    [
        ("expired_ledger", "expired"),
        ("expired_artifact", "expired"),
        ("kill_switch", "kill_switch"),
        ("ledger_rollback_pending", "rollback_pending"),
        ("target_rollback_pending", "rollback_pending"),
    ],
)
def test_invocation_ineligible_refusals_write_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str, reason: str
) -> None:
    ctx = _registered_workflow(tmp_path, monkeypatch)
    now, ledger, registry = ctx["now"], ctx["ledger"], ctx["registry"]
    cap_id, version_id = ctx["capability_id"], ctx["version_id"]
    if case == "expired_ledger":
        _mutate_json(ledger, "capabilities", cap_id, expiry=now - 1)
    elif case == "expired_artifact":
        payload = json.loads(registry.read_text(encoding="utf-8"))
        payload["targets"][version_id]["artifact"]["lifecycle"]["expires_at"] = now - 1
        registry.write_text(json.dumps(payload), encoding="utf-8")
    elif case == "kill_switch":
        monkeypatch.setenv("ORCH_CAPABILITY_WORKFLOW_ENABLED", "0")
    elif case == "ledger_rollback_pending":
        _mutate_json(ledger, "capabilities", cap_id, rollback_pending={"reason": "regressed"})
    else:
        binding = capability_targets.get_binding(version_id, registry_path=registry)
        capability_targets.prepare_rollback(binding, registry_path=registry, reason="regressed")
    before = capability_targets.get_binding(version_id, registry_path=registry)
    result = capability_lifecycle.invoke_compiled_target(
        cap_id,
        trigger=ctx["fixture"]["trigger"],
        target_run_id=f"workflow:{case}",
        ledger_path=ledger,
        target_registry_path=registry,
        inputs=ctx["fixture"]["inputs"],
        timestamp=now,
    )
    assert result == {"matched": True, "invoked": False, "reason": reason}, result
    after = capability_targets.get_binding(version_id, registry_path=registry)
    assert after["invocations"] == before["invocations"] == []
    assert "causal_edge" not in result
    assert feedback.capability_causal_evidence(cap_id, version_id) == []


@pytest.mark.parametrize("kind", ["role", "workflow", "skill", "playbook", "gate"])
def test_invocation_kill_switch_refuses_each_kind_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    # Each kind's ledger row declares its own switch (`_record` writes
    # ``ORCH_CAPABILITY_<KIND>_ENABLED=0``); the unit-level helper test cannot
    # prove a registered row of that kind refuses through the real entrypoint.
    if kind == "skill":
        env_prereq.require(env_prereq.skill_resource_absent())
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "brain.db")
    now = int(time.time())
    root = tmp_path / kind
    root.mkdir()
    ledger = root / "capabilities.json"
    registry = root / "targets.json"
    predecessor = f"baseline:{kind}"
    capabilities.save({predecessor: _active_predecessor(predecessor, now)}, ledger)
    fixture = _target_fixture(kind, root, now)
    registered = capability_lifecycle.register_compiled_target(
        kind,
        fixture["artifact"],
        lifecycle_policy={
            "min_independent_durable_reuse": 2,
            "max_failures": 0,
            "max_rework": 0,
            "tasks_per_day": 5,
            "risk_level": "low_reversible",
            "side_effect_policy": "read_only",
        },
        matcher=fixture["matcher"],
        predecessor=predecessor,
        ledger_path=ledger,
        target_registry_path=registry,
        target_context=fixture["context"],
        expiry=now + 86400,
    )
    capability_id = registered["capability"]["capability_id"]
    version_id = registered["capability"]["capability_version_id"]
    declared = capabilities.load(ledger, create=False)[capability_id]["kill_switch"]
    env_name, _, env_value = declared.partition("=")
    assert env_name == f"ORCH_CAPABILITY_{kind.upper()}_ENABLED"
    monkeypatch.setenv(env_name, env_value)
    before_ledger = ledger.read_bytes()
    result = capability_lifecycle.invoke_compiled_target(
        capability_id,
        trigger=fixture["trigger"],
        target_run_id=f"{kind}:kill-switch:1",
        ledger_path=ledger,
        target_registry_path=registry,
        inputs=fixture["inputs"],
        timestamp=now,
    )
    assert result == {"matched": True, "invoked": False, "reason": "kill_switch"}, (kind, result)
    assert capability_targets.get_binding(version_id, registry_path=registry)["invocations"] == []
    assert "causal_edge" not in result
    assert feedback.capability_causal_evidence(capability_id, version_id) == []
    assert ledger.read_bytes() == before_ledger


def test_invocation_ineligible_eligible_control_still_invokes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _registered_workflow(tmp_path, monkeypatch)
    _record_target_run("workflow:eligible:1", "workflow:subject:eligible")
    result = capability_lifecycle.invoke_compiled_target(
        ctx["capability_id"],
        trigger=ctx["fixture"]["trigger"],
        target_run_id="workflow:eligible:1",
        ledger_path=ctx["ledger"],
        target_registry_path=ctx["registry"],
        inputs=ctx["fixture"]["inputs"],
        timestamp=ctx["now"],
    )
    assert result["invoked"] and result["accepted"] and result["causal_edge"], result


def test_compiled_invocation_expiry_refuses_after_expiry_and_invokes_before(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _registered_workflow(tmp_path, monkeypatch)
    _record_target_run("workflow:expiry:1", "workflow:subject:expiry")
    before_expiry = capability_lifecycle.invoke_compiled_target(
        ctx["capability_id"],
        trigger=ctx["fixture"]["trigger"],
        target_run_id="workflow:expiry:1",
        ledger_path=ctx["ledger"],
        target_registry_path=ctx["registry"],
        inputs=ctx["fixture"]["inputs"],
        timestamp=ctx["now"],
    )
    assert before_expiry["invoked"] and before_expiry["accepted"], before_expiry
    invocations = capability_targets.get_binding(ctx["version_id"], registry_path=ctx["registry"])[
        "invocations"
    ]
    assert len(invocations) == 1
    _mutate_json(ctx["ledger"], "capabilities", ctx["capability_id"], expiry=ctx["now"] - 1)
    refused = capability_lifecycle.invoke_compiled_target(
        ctx["capability_id"],
        trigger=ctx["fixture"]["trigger"],
        target_run_id="workflow:expiry:2",
        ledger_path=ctx["ledger"],
        target_registry_path=ctx["registry"],
        inputs=ctx["fixture"]["inputs"],
        timestamp=ctx["now"],
    )
    assert refused == {"matched": True, "invoked": False, "reason": "expired"}, refused
    assert (
        capability_targets.get_binding(ctx["version_id"], registry_path=ctx["registry"])[
            "invocations"
        ]
        == invocations
    )


def test_expired_row_never_reaches_workflow_producer_or_consumer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _registered_workflow(tmp_path, monkeypatch)
    _mutate_json(ctx["ledger"], "capabilities", ctx["capability_id"], expiry=ctx["now"] - 1)
    calls: list[str] = []

    def _spy(name: str):
        def _record(*args, **kwargs):
            calls.append(name)
            raise AssertionError(f"{name} must be unreachable for an expired row")

        return _record

    monkeypatch.setattr(compiler, "dry_run_workflow_rail", _spy("dry_run_workflow_rail"))
    monkeypatch.setattr(compiler, "consume_workflow_output", _spy("consume_workflow_output"))
    refused = capability_lifecycle.invoke_compiled_target(
        ctx["capability_id"],
        trigger=ctx["fixture"]["trigger"],
        target_run_id="workflow:expiry:producer",
        ledger_path=ctx["ledger"],
        target_registry_path=ctx["registry"],
        inputs=ctx["fixture"]["inputs"],
        timestamp=ctx["now"],
    )
    assert refused == {"matched": True, "invoked": False, "reason": "expired"}, refused
    binding = capability_targets.get_binding(ctx["version_id"], registry_path=ctx["registry"])
    direct = capability_targets.invoke_target(
        binding,
        trigger=ctx["fixture"]["trigger"],
        registry_path=ctx["registry"],
        ledger_path=ctx["ledger"],
        target_run_id="workflow:expiry:direct",
        inputs=ctx["fixture"]["inputs"],
        timestamp=ctx["now"],
    )
    assert direct == {"matched": True, "invoked": False, "reason": "expired"}, direct
    assert calls == []
    assert (
        capability_targets.get_binding(ctx["version_id"], registry_path=ctx["registry"])[
            "invocations"
        ]
        == []
    )


def test_compiled_invocation_ineligible_consults_shared_capability_expired_before_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _registered_workflow(tmp_path, monkeypatch)
    seen: list[tuple[object, object]] = []
    real_predicate = capabilities.capability_expired

    def _forcing_predicate(cap, now=None):
        seen.append((cap.get("expiry"), now))
        return True

    monkeypatch.setattr(capabilities, "capability_expired", _forcing_predicate)
    binding_lookups: list[str] = []
    real_get_binding = capability_targets.get_binding

    def _spy_get_binding(*args, **kwargs):
        binding_lookups.append("get_binding")
        return real_get_binding(*args, **kwargs)

    monkeypatch.setattr(capability_targets, "get_binding", _spy_get_binding)
    assert real_predicate({"expiry": ctx["now"] + 86400}, ctx["now"]) is False
    refused = capability_lifecycle.invoke_compiled_target(
        ctx["capability_id"],
        trigger=ctx["fixture"]["trigger"],
        target_run_id="workflow:expiry:shared-predicate",
        ledger_path=ctx["ledger"],
        target_registry_path=ctx["registry"],
        inputs=ctx["fixture"]["inputs"],
        timestamp=ctx["now"],
    )
    assert refused == {"matched": True, "invoked": False, "reason": "expired"}, refused
    assert seen and seen[0][1] == ctx["now"]
    assert binding_lookups == []


@pytest.mark.parametrize(
    ("case", "reason"),
    [
        ("expired_artifact", "expired"),
        ("kill_switch", "kill_switch"),
        ("rollback_pending", "rollback_pending"),
    ],
)
def test_direct_invoke_target_ineligible_refuses_without_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str, reason: str
) -> None:
    ctx = _registered_workflow(tmp_path, monkeypatch)
    registry, version_id = ctx["registry"], ctx["version_id"]
    if case == "expired_artifact":
        payload = json.loads(registry.read_text(encoding="utf-8"))
        payload["targets"][version_id]["artifact"]["lifecycle"]["expires_at"] = ctx["now"] - 1
        registry.write_text(json.dumps(payload), encoding="utf-8")
    elif case == "kill_switch":
        monkeypatch.setenv("ORCH_REFERENCE_WORKFLOW_DISABLED", "1")
    else:
        binding = capability_targets.get_binding(version_id, registry_path=registry)
        capability_targets.prepare_rollback(binding, registry_path=registry, reason="regressed")
    binding = capability_targets.get_binding(version_id, registry_path=registry)
    result = capability_targets.invoke_target(
        binding,
        trigger=ctx["fixture"]["trigger"],
        registry_path=registry,
        ledger_path=ctx["ledger"],
        target_run_id=f"direct:{case}",
        inputs=ctx["fixture"]["inputs"],
        timestamp=ctx["now"],
    )
    assert result == {"matched": True, "invoked": False, "reason": reason}, result
    assert capability_targets.get_binding(version_id, registry_path=registry)["invocations"] == []


@pytest.mark.parametrize(
    ("case", "reason"),
    [
        ("expired_ledger", "expired"),
        ("ledger_kill_switch", "kill_switch"),
        ("ledger_rollback_pending", "rollback_pending"),
    ],
)
def test_direct_invoke_target_ledger_ineligible_refuses_without_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str, reason: str
) -> None:
    ctx = _registered_workflow(tmp_path, monkeypatch)
    registry, version_id, ledger = ctx["registry"], ctx["version_id"], ctx["ledger"]
    if case == "expired_ledger":
        _mutate_json(ledger, "capabilities", ctx["capability_id"], expiry=ctx["now"] - 1)
    elif case == "ledger_kill_switch":
        monkeypatch.setenv("ORCH_CAPABILITY_WORKFLOW_ENABLED", "0")
    else:
        _mutate_json(
            ledger, "capabilities", ctx["capability_id"], rollback_pending={"reason": "regressed"}
        )
    before = ledger.read_bytes()
    binding = capability_targets.get_binding(version_id, registry_path=registry)
    result = capability_targets.invoke_target(
        binding,
        trigger=ctx["fixture"]["trigger"],
        registry_path=registry,
        ledger_path=ledger,
        target_run_id=f"direct:{case}",
        inputs=ctx["fixture"]["inputs"],
        timestamp=ctx["now"],
    )
    assert result == {"matched": True, "invoked": False, "reason": reason}, result
    assert capability_targets.get_binding(version_id, registry_path=registry)["invocations"] == []
    assert "causal_edge" not in result
    assert ledger.read_bytes() == before


def test_invocation_disabled_binding_control_still_returns_binding_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _registered_workflow(tmp_path, monkeypatch)
    payload = json.loads(ctx["registry"].read_text(encoding="utf-8"))
    payload["targets"][ctx["version_id"]]["enabled"] = False
    ctx["registry"].write_text(json.dumps(payload), encoding="utf-8")
    result = capability_lifecycle.invoke_compiled_target(
        ctx["capability_id"],
        trigger=ctx["fixture"]["trigger"],
        target_run_id="workflow:disabled:1",
        ledger_path=ctx["ledger"],
        target_registry_path=ctx["registry"],
        inputs=ctx["fixture"]["inputs"],
        timestamp=ctx["now"],
    )
    assert result == {"matched": False, "invoked": False, "reason": "binding_disabled"}, result
    assert (
        capability_targets.get_binding(ctx["version_id"], registry_path=ctx["registry"])[
            "invocations"
        ]
        == []
    )
    assert "causal_edge" not in result


@pytest.mark.parametrize(
    ("case", "reason"),
    [
        ("malformed_artifact_expiry", "expired"),
        ("binding_kill_switch", "kill_switch"),
        ("policy_kill_switch", "kill_switch"),
    ],
)
def test_direct_invoke_target_fail_closed_declarations_refuse_without_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str, reason: str
) -> None:
    ctx = _registered_workflow(tmp_path, monkeypatch)
    registry, version_id = ctx["registry"], ctx["version_id"]
    payload = json.loads(registry.read_text(encoding="utf-8"))
    target = payload["targets"][version_id]
    if case == "malformed_artifact_expiry":
        target["artifact"]["lifecycle"]["expires_at"] = "not-a-timestamp"
    elif case == "binding_kill_switch":
        target["kill_switch"] = "ORCH_DIRECT_BINDING_DISABLED=1"
        monkeypatch.setenv("ORCH_DIRECT_BINDING_DISABLED", "1")
    else:
        target["lifecycle_policy"]["kill_switch"] = "ORCH_DIRECT_POLICY_DISABLED=1"
        monkeypatch.setenv("ORCH_DIRECT_POLICY_DISABLED", "1")
    registry.write_text(json.dumps(payload), encoding="utf-8")
    binding = capability_targets.get_binding(version_id, registry_path=registry)
    result = capability_targets.invoke_target(
        binding,
        trigger=ctx["fixture"]["trigger"],
        registry_path=registry,
        ledger_path=ctx["ledger"],
        target_run_id=f"direct:{case}",
        inputs=ctx["fixture"]["inputs"],
        timestamp=ctx["now"],
    )
    assert result == {"matched": True, "invoked": False, "reason": reason}, result
    assert capability_targets.get_binding(version_id, registry_path=registry)["invocations"] == []


def test_binding_ineligible_validates_skill_package_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def _counting_validate(path: Path) -> dict:
        calls.append(str(path))
        return {"name": "counted-skill", "expires_at": 9999999999}

    monkeypatch.setattr(compiler, "validate_skill_package", _counting_validate)
    binding = {
        "kind": "skill",
        "artifact": "/tmp/counted-skill-package",
        "lifecycle_policy": {},
        "rollback": None,
    }
    assert capability_targets._binding_ineligible(binding, 100, {}) is None
    assert len(calls) == 1, calls

    def _failing_validate(path: Path) -> dict:
        calls.append(str(path))
        raise ValueError("broken package")

    monkeypatch.setattr(compiler, "validate_skill_package", _failing_validate)
    calls.clear()
    assert capability_targets._binding_ineligible(binding, 100, {}) == "artifact_invalid"
    assert len(calls) == 1, calls

    # Malformed declarations refuse before Path or package validation. They must
    # not appear eligible and then raise in the selector's skill match.
    calls.clear()
    for artifact in (None, 17, {}, []):
        malformed = {**binding, "artifact": artifact}
        assert capability_targets._binding_artifact(malformed) == ({}, True)
        assert capability_targets._binding_ineligible(malformed, 100, {}) == "artifact_invalid"
    missing = {k: v for k, v in binding.items() if k != "artifact"}
    assert capability_targets._binding_ineligible(missing, 100, {}) == "artifact_invalid"
    assert calls == []
    for deadline in (float("inf"), float("-inf"), float("nan")):
        invalid_deadline = {"kind": "workflow", "artifact": {"expires_at": deadline}}
        assert capability_targets._binding_ineligible(invalid_deadline, 100, {}) == "expired"


def test_direct_invoke_target_broken_skill_package_refuses_without_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _registered_workflow(tmp_path, monkeypatch)
    registry, version_id = ctx["registry"], ctx["version_id"]
    payload = json.loads(registry.read_text(encoding="utf-8"))
    target = payload["targets"][version_id]
    target["kind"] = "skill"
    target["artifact"] = str(tmp_path / "missing-skill-package")
    registry.write_text(json.dumps(payload), encoding="utf-8")
    binding = capability_targets.get_binding(version_id, registry_path=registry)
    cap = {"expiry": ctx["now"] + 86400, "kill_switch": "ORCH_CAPABILITY_SKILL_ENABLED=0"}
    assert capability_lifecycle._invocation_ineligible(cap, binding, ctx["now"], {}) == (
        "artifact_invalid"
    )
    result = capability_targets.invoke_target(
        binding,
        trigger={"skill_name": "missing", "target": "owner/repo#23"},
        registry_path=registry,
        ledger_path=ctx["ledger"],
        target_run_id="direct:broken-skill",
        inputs={"artifact_refs": []},
        timestamp=ctx["now"],
    )
    assert result == {"matched": True, "invoked": False, "reason": "artifact_invalid"}, result
    assert capability_targets.get_binding(version_id, registry_path=registry)["invocations"] == []
    assert "causal_edge" not in result
    for artifact in (None, 17, {}, []):
        target["artifact"] = artifact
        registry.write_text(json.dumps(payload), encoding="utf-8")
        malformed = capability_targets.get_binding(version_id, registry_path=registry)
        refused = capability_targets.invoke_target(
            malformed,
            trigger={"skill_name": "missing", "target": "owner/repo#23"},
            registry_path=registry,
            ledger_path=ctx["ledger"],
            target_run_id="direct:malformed-skill",
            inputs={"artifact_refs": []},
            timestamp=ctx["now"],
        )
        assert refused == {"matched": True, "invoked": False, "reason": "artifact_invalid"}
        assert (
            capability_targets.get_binding(version_id, registry_path=registry)["invocations"] == []
        )


def test_compiled_invocation_malformed_ledger_expiry_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _registered_workflow(tmp_path, monkeypatch)
    _mutate_json(ctx["ledger"], "capabilities", ctx["capability_id"], expiry="not-a-timestamp")
    result = capability_lifecycle.invoke_compiled_target(
        ctx["capability_id"],
        trigger=ctx["fixture"]["trigger"],
        target_run_id="workflow:malformed-expiry:1",
        ledger_path=ctx["ledger"],
        target_registry_path=ctx["registry"],
        inputs=ctx["fixture"]["inputs"],
        timestamp=ctx["now"],
    )
    assert result == {"matched": True, "invoked": False, "reason": "expired"}, result
    assert (
        capability_targets.get_binding(ctx["version_id"], registry_path=ctx["registry"])[
            "invocations"
        ]
        == []
    )
    assert "causal_edge" not in result


def test_compiled_invocation_unreadable_ledger_refuses_without_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _registered_workflow(tmp_path, monkeypatch)
    ctx["ledger"].write_text("{", encoding="utf-8")
    registry_before = ctx["registry"].read_bytes()
    result = capability_lifecycle.invoke_compiled_target(
        ctx["capability_id"],
        trigger=ctx["fixture"]["trigger"],
        target_run_id="workflow:unreadable-ledger:1",
        ledger_path=ctx["ledger"],
        target_registry_path=ctx["registry"],
        inputs=ctx["fixture"]["inputs"],
        timestamp=ctx["now"],
    )
    assert result == {"matched": False, "invoked": False, "reason": "ledger_unreadable"}, result
    assert "causal_edge" not in result
    assert ctx["ledger"].read_bytes() == b"{"
    assert ctx["registry"].read_bytes() == registry_before
    assert (
        capability_targets.get_binding(ctx["version_id"], registry_path=ctx["registry"])[
            "invocations"
        ]
        == []
    )


def test_direct_invoke_target_unreadable_ledger_refuses_without_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _registered_workflow(tmp_path, monkeypatch)
    binding = capability_targets.get_binding(ctx["version_id"], registry_path=ctx["registry"])
    ctx["ledger"].write_text("{", encoding="utf-8")
    result = capability_targets.invoke_target(
        binding,
        trigger=ctx["fixture"]["trigger"],
        registry_path=ctx["registry"],
        ledger_path=ctx["ledger"],
        target_run_id="direct:unreadable-ledger",
        inputs=ctx["fixture"]["inputs"],
        timestamp=ctx["now"],
    )
    assert result == {"matched": True, "invoked": False, "reason": "ledger_unreadable"}, result
    assert "causal_edge" not in result
    assert (
        capability_targets.get_binding(ctx["version_id"], registry_path=ctx["registry"])[
            "invocations"
        ]
        == []
    )


@pytest.mark.parametrize("rollback", ["pending", 123, ["pending"]])
def test_binding_ineligible_malformed_rollback_refuses_as_rollback_pending(rollback) -> None:
    # A rollback declaration that is not a mapping cannot prove no rollback
    # is pending; it must refuse as pending instead of raising AttributeError.
    binding = {"kind": "workflow", "artifact": {}, "lifecycle_policy": {}, "rollback": rollback}
    assert capability_targets._binding_ineligible(binding, 100, {}) == "rollback_pending"
    cap = {"expiry": 9999999999, "kill_switch": "ORCH_CAPABILITY_WORKFLOW_ENABLED=0"}
    assert capability_lifecycle._invocation_ineligible(cap, binding, 100, {}) == (
        "rollback_pending"
    )


def test_direct_invoke_target_malformed_rollback_refuses_without_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _registered_workflow(tmp_path, monkeypatch)
    registry, version_id = ctx["registry"], ctx["version_id"]
    payload = json.loads(registry.read_text(encoding="utf-8"))
    payload["targets"][version_id]["rollback"] = "pending"
    registry.write_text(json.dumps(payload), encoding="utf-8")
    registry_before = registry.read_bytes()
    ledger_before = ctx["ledger"].read_bytes()
    binding = capability_targets.get_binding(version_id, registry_path=registry)
    result = capability_targets.invoke_target(
        binding,
        trigger=ctx["fixture"]["trigger"],
        registry_path=registry,
        ledger_path=ctx["ledger"],
        target_run_id="direct:malformed-rollback",
        inputs=ctx["fixture"]["inputs"],
        timestamp=ctx["now"],
    )
    assert result == {"matched": True, "invoked": False, "reason": "rollback_pending"}, result
    assert capability_targets.get_binding(version_id, registry_path=registry)["invocations"] == []
    assert "causal_edge" not in result
    assert registry.read_bytes() == registry_before
    assert ctx["ledger"].read_bytes() == ledger_before


def test_ledger_ineligible_skips_unrelated_non_mapping_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _registered_workflow(tmp_path, monkeypatch)
    ledger, registry = ctx["ledger"], ctx["registry"]
    payload = json.loads(ledger.read_text(encoding="utf-8"))
    # Place the corrupt row first so skipping it is required to find our row.
    payload["capabilities"] = {"unrelated-corrupt": "corrupt", **payload["capabilities"]}
    ledger.write_text(json.dumps(payload), encoding="utf-8")
    before = ledger.read_bytes()
    registry_before = registry.read_bytes()
    binding = capability_targets.get_binding(ctx["version_id"], registry_path=registry)
    assert capability_targets._ledger_ineligible(binding, ledger, ctx["now"], {}) is None
    assert ledger.read_bytes() == before
    assert registry.read_bytes() == registry_before


def test_direct_invoke_target_non_mapping_own_row_refuses_without_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _registered_workflow(tmp_path, monkeypatch)
    registry, version_id, ledger = ctx["registry"], ctx["version_id"], ctx["ledger"]
    payload = json.loads(ledger.read_text(encoding="utf-8"))
    payload["capabilities"][ctx["capability_id"]] = "corrupt-row"
    ledger.write_text(json.dumps(payload), encoding="utf-8")
    before = ledger.read_bytes()
    binding = capability_targets.get_binding(version_id, registry_path=registry)
    result = capability_targets.invoke_target(
        binding,
        trigger=ctx["fixture"]["trigger"],
        registry_path=registry,
        ledger_path=ledger,
        target_run_id="direct:corrupt-row",
        inputs=ctx["fixture"]["inputs"],
        timestamp=ctx["now"],
    )
    assert result == {"matched": True, "invoked": False, "reason": "ledger_unreadable"}, result
    assert capability_targets.get_binding(version_id, registry_path=registry)["invocations"] == []
    assert "causal_edge" not in result
    assert ledger.read_bytes() == before


@pytest.mark.parametrize("row", ["corrupt-row", 0, "", []])
def test_compiled_invocation_non_mapping_row_refuses_without_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, row
) -> None:
    ctx = _registered_workflow(tmp_path, monkeypatch)
    payload = json.loads(ctx["ledger"].read_text(encoding="utf-8"))
    payload["capabilities"][ctx["capability_id"]] = row
    ctx["ledger"].write_text(json.dumps(payload), encoding="utf-8")
    registry_before = ctx["registry"].read_bytes()
    result = capability_lifecycle.invoke_compiled_target(
        ctx["capability_id"],
        trigger=ctx["fixture"]["trigger"],
        target_run_id="workflow:corrupt-row:1",
        ledger_path=ctx["ledger"],
        target_registry_path=ctx["registry"],
        inputs=ctx["fixture"]["inputs"],
        timestamp=ctx["now"],
    )
    assert result == {"matched": False, "invoked": False, "reason": "ledger_unreadable"}, result
    assert "causal_edge" not in result
    assert ctx["registry"].read_bytes() == registry_before


@pytest.mark.parametrize("status", [["canary"], {"status": "canary"}, 123])
def test_compiled_invocation_malformed_status_refuses_without_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status
) -> None:
    # An unhashable or non-string status is a corrupt row: it must refuse
    # with its status reason instead of raising TypeError on the set check.
    ctx = _registered_workflow(tmp_path, monkeypatch)
    _mutate_json(ctx["ledger"], "capabilities", ctx["capability_id"], status=status)
    ledger_before = ctx["ledger"].read_bytes()
    registry_before = ctx["registry"].read_bytes()
    result = capability_lifecycle.invoke_compiled_target(
        ctx["capability_id"],
        trigger=ctx["fixture"]["trigger"],
        target_run_id="workflow:corrupt-status:1",
        ledger_path=ctx["ledger"],
        target_registry_path=ctx["registry"],
        inputs=ctx["fixture"]["inputs"],
        timestamp=ctx["now"],
    )
    assert result == {
        "matched": False,
        "invoked": False,
        "reason": f"status:{status}",
    }, result
    assert "causal_edge" not in result
    assert ctx["ledger"].read_bytes() == ledger_before
    assert ctx["registry"].read_bytes() == registry_before


@pytest.mark.parametrize("version_id", [None, 123, "", "bogus"])
def test_compiled_invocation_missing_version_refuses_without_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, version_id
) -> None:
    # A row without a usable capability_version_id cannot be matched to a
    # binding; it refuses as unreadable instead of raising KeyError.
    ctx = _registered_workflow(tmp_path, monkeypatch)
    payload = json.loads(ctx["ledger"].read_text(encoding="utf-8"))
    if version_id is None:
        del payload["capabilities"][ctx["capability_id"]]["capability_version_id"]
    else:
        payload["capabilities"][ctx["capability_id"]]["capability_version_id"] = version_id
    ctx["ledger"].write_text(json.dumps(payload), encoding="utf-8")
    ledger_before = ctx["ledger"].read_bytes()
    registry_before = ctx["registry"].read_bytes()
    result = capability_lifecycle.invoke_compiled_target(
        ctx["capability_id"],
        trigger=ctx["fixture"]["trigger"],
        target_run_id="workflow:corrupt-version:1",
        ledger_path=ctx["ledger"],
        target_registry_path=ctx["registry"],
        inputs=ctx["fixture"]["inputs"],
        timestamp=ctx["now"],
    )
    assert result == {"matched": False, "invoked": False, "reason": "ledger_unreadable"}, result
    assert "causal_edge" not in result
    assert ctx["ledger"].read_bytes() == ledger_before
    assert ctx["registry"].read_bytes() == registry_before


@pytest.mark.parametrize(
    "kill_switch",
    [["ORCH_CAPABILITY_WORKFLOW_ENABLED=0"], 123, "invalid-switch", {"env": ""}],
)
def test_compiled_invocation_malformed_kill_switch_refuses_without_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kill_switch
) -> None:
    # A kill-switch declaration that is neither a string nor a mapping cannot
    # prove the switch is not triggered, so it refuses as kill_switch.
    ctx = _registered_workflow(tmp_path, monkeypatch)
    _mutate_json(ctx["ledger"], "capabilities", ctx["capability_id"], kill_switch=kill_switch)
    ledger_before = ctx["ledger"].read_bytes()
    registry_before = ctx["registry"].read_bytes()
    result = capability_lifecycle.invoke_compiled_target(
        ctx["capability_id"],
        trigger=ctx["fixture"]["trigger"],
        target_run_id="workflow:corrupt-kill-switch:1",
        ledger_path=ctx["ledger"],
        target_registry_path=ctx["registry"],
        inputs=ctx["fixture"]["inputs"],
        timestamp=ctx["now"],
    )
    assert result == {"matched": True, "invoked": False, "reason": "kill_switch"}, result
    assert "causal_edge" not in result
    assert (
        capability_targets.get_binding(ctx["version_id"], registry_path=ctx["registry"])[
            "invocations"
        ]
        == []
    )
    assert ctx["ledger"].read_bytes() == ledger_before
    assert ctx["registry"].read_bytes() == registry_before


def test_direct_invoke_target_malformed_ledger_kill_switch_refuses_without_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _registered_workflow(tmp_path, monkeypatch)
    _mutate_json(ctx["ledger"], "capabilities", ctx["capability_id"], kill_switch={"env": ""})
    ledger_before = ctx["ledger"].read_bytes()
    registry_before = ctx["registry"].read_bytes()
    binding = capability_targets.get_binding(ctx["version_id"], registry_path=ctx["registry"])
    result = capability_targets.invoke_target(
        binding,
        trigger=ctx["fixture"]["trigger"],
        registry_path=ctx["registry"],
        ledger_path=ctx["ledger"],
        target_run_id="direct:corrupt-kill-switch",
        inputs=ctx["fixture"]["inputs"],
        timestamp=ctx["now"],
    )
    assert result == {"matched": True, "invoked": False, "reason": "kill_switch"}, result
    assert "causal_edge" not in result
    assert ctx["ledger"].read_bytes() == ledger_before
    assert ctx["registry"].read_bytes() == registry_before


@pytest.mark.parametrize(
    "policy", [["canary"], {"tasks_per_day": "not-a-count"}, {"tasks_per_day": {"x": 1}}]
)
def test_compiled_canary_malformed_policy_refuses_without_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, policy
) -> None:
    # A canary policy that cannot be parsed cannot prove quota remains; it
    # refuses as exhausted (quota logic for well-formed policies unchanged).
    ctx = _registered_workflow(tmp_path, monkeypatch)
    _mutate_json(
        ctx["ledger"],
        "capabilities",
        ctx["capability_id"],
        status="canary",
        lifecycle_policy=policy,
    )
    ledger_before = ctx["ledger"].read_bytes()
    registry_before = ctx["registry"].read_bytes()
    result = capability_lifecycle.invoke_compiled_target(
        ctx["capability_id"],
        trigger=ctx["fixture"]["trigger"],
        target_run_id="workflow:corrupt-canary-policy:1",
        ledger_path=ctx["ledger"],
        target_registry_path=ctx["registry"],
        inputs=ctx["fixture"]["inputs"],
        timestamp=ctx["now"],
    )
    assert result == {"matched": True, "invoked": False, "reason": "canary_quota_exhausted"}, result
    assert "causal_edge" not in result
    assert ctx["ledger"].read_bytes() == ledger_before
    assert ctx["registry"].read_bytes() == registry_before


def test_compiled_canary_corrupt_invocation_history_refuses_without_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _registered_workflow(tmp_path, monkeypatch)
    _mutate_json(ctx["ledger"], "capabilities", ctx["capability_id"], status="canary")
    payload = json.loads(ctx["registry"].read_text(encoding="utf-8"))
    payload["targets"][ctx["version_id"]]["invocations"] = {"not": "a-list"}
    ctx["registry"].write_text(json.dumps(payload), encoding="utf-8")
    ledger_before = ctx["ledger"].read_bytes()
    registry_before = ctx["registry"].read_bytes()
    result = capability_lifecycle.invoke_compiled_target(
        ctx["capability_id"],
        trigger=ctx["fixture"]["trigger"],
        target_run_id="workflow:corrupt-canary-history:1",
        ledger_path=ctx["ledger"],
        target_registry_path=ctx["registry"],
        inputs=ctx["fixture"]["inputs"],
        timestamp=ctx["now"],
    )
    assert result == {"matched": True, "invoked": False, "reason": "canary_quota_exhausted"}, result
    assert "causal_edge" not in result
    assert ctx["ledger"].read_bytes() == ledger_before
    assert ctx["registry"].read_bytes() == registry_before


@pytest.mark.parametrize("invoked_at", [None, float("inf")])
def test_compiled_canary_unusable_timestamp_refuses_without_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, invoked_at
) -> None:
    ctx = _registered_workflow(tmp_path, monkeypatch)
    _mutate_json(ctx["ledger"], "capabilities", ctx["capability_id"], status="canary")
    payload = json.loads(ctx["registry"].read_text(encoding="utf-8"))
    row = {"lifecycle_stage": "canary"}
    if invoked_at is not None:
        row["invoked_at"] = invoked_at
    payload["targets"][ctx["version_id"]]["invocations"] = [row]
    ctx["registry"].write_text(json.dumps(payload), encoding="utf-8")
    ledger_before = ctx["ledger"].read_bytes()
    registry_before = ctx["registry"].read_bytes()
    result = capability_lifecycle.invoke_compiled_target(
        ctx["capability_id"],
        trigger=ctx["fixture"]["trigger"],
        target_run_id="workflow:bad-canary-timestamp:1",
        ledger_path=ctx["ledger"],
        target_registry_path=ctx["registry"],
        inputs=ctx["fixture"]["inputs"],
        timestamp=ctx["now"],
    )
    assert result == {"matched": True, "invoked": False, "reason": "canary_quota_exhausted"}
    assert ctx["ledger"].read_bytes() == ledger_before
    assert ctx["registry"].read_bytes() == registry_before


@pytest.mark.parametrize("field", ["kind", "artifact"])
def test_direct_invoke_target_missing_binding_shape_refuses_without_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    # A registry binding missing its kind or artifact is corrupt; direct
    # invocation refuses as artifact_invalid instead of raising KeyError.
    ctx = _registered_workflow(tmp_path, monkeypatch)
    payload = json.loads(ctx["registry"].read_text(encoding="utf-8"))
    del payload["targets"][ctx["version_id"]][field]
    ctx["registry"].write_text(json.dumps(payload), encoding="utf-8")
    ledger_before = ctx["ledger"].read_bytes()
    registry_before = ctx["registry"].read_bytes()
    binding = capability_targets.get_binding(ctx["version_id"], registry_path=ctx["registry"])
    result = capability_targets.invoke_target(
        binding,
        trigger=ctx["fixture"]["trigger"],
        registry_path=ctx["registry"],
        ledger_path=ctx["ledger"],
        target_run_id="direct:corrupt-binding-shape",
        inputs=ctx["fixture"]["inputs"],
        timestamp=ctx["now"],
    )
    assert result == {"matched": True, "invoked": False, "reason": "artifact_invalid"}, result
    assert "causal_edge" not in result
    assert ctx["ledger"].read_bytes() == ledger_before
    assert ctx["registry"].read_bytes() == registry_before


@pytest.mark.parametrize(
    "field, value", [("artifact", []), ("invocations", None), ("invocations", {})]
)
def test_direct_invoke_target_corrupt_binding_values_refuse_without_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str, value
) -> None:
    ctx = _registered_workflow(tmp_path, monkeypatch)
    payload = json.loads(ctx["registry"].read_text(encoding="utf-8"))
    payload["targets"][ctx["version_id"]][field] = value
    ctx["registry"].write_text(json.dumps(payload), encoding="utf-8")
    ledger_before = ctx["ledger"].read_bytes()
    registry_before = ctx["registry"].read_bytes()
    binding = capability_targets.get_binding(ctx["version_id"], registry_path=ctx["registry"])
    result = capability_targets.invoke_target(
        binding,
        trigger=ctx["fixture"]["trigger"],
        registry_path=ctx["registry"],
        ledger_path=ctx["ledger"],
        target_run_id="direct:corrupt-binding-values",
        inputs=ctx["fixture"]["inputs"],
        timestamp=ctx["now"],
    )
    assert result == {"matched": True, "invoked": False, "reason": "artifact_invalid"}
    assert ctx["ledger"].read_bytes() == ledger_before
    assert ctx["registry"].read_bytes() == registry_before


@pytest.mark.parametrize(
    "binding", [None, "corrupt", [], {}, {"kind": "workflow"}, {"capability_version_id": 17}]
)
def test_direct_invoke_target_malformed_binding_argument_refuses_without_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, binding
) -> None:
    # A direct caller handing invoke_target a non-mapping or version-less
    # binding gets a refusal, not a KeyError/TypeError, and nothing is written.
    ctx = _registered_workflow(tmp_path, monkeypatch)
    ledger_before = ctx["ledger"].read_bytes()
    registry_before = ctx["registry"].read_bytes()
    result = capability_targets.invoke_target(
        binding,
        trigger=ctx["fixture"]["trigger"],
        registry_path=ctx["registry"],
        ledger_path=ctx["ledger"],
        target_run_id="direct:malformed-binding-argument",
        inputs=ctx["fixture"]["inputs"],
        timestamp=ctx["now"],
    )
    assert result == {"matched": True, "invoked": False, "reason": "artifact_invalid"}, result
    assert "causal_edge" not in result
    assert ctx["ledger"].read_bytes() == ledger_before
    assert ctx["registry"].read_bytes() == registry_before


@pytest.mark.parametrize("row", [None, "corrupt", 17, ["corrupt"]])
def test_corrupt_registry_row_refuses_without_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, row
) -> None:
    # A registry row that is not a mapping cannot prove eligibility: both the
    # direct and the compiled entrypoints refuse instead of raising.
    ctx = _registered_workflow(tmp_path, monkeypatch)
    payload = json.loads(ctx["registry"].read_text(encoding="utf-8"))
    payload["targets"][ctx["version_id"]] = row
    ctx["registry"].write_text(json.dumps(payload), encoding="utf-8")
    ledger_before = ctx["ledger"].read_bytes()
    registry_before = ctx["registry"].read_bytes()
    binding = capability_targets.get_binding(ctx["version_id"], registry_path=ctx["registry"])
    direct = capability_targets.invoke_target(
        binding,
        trigger=ctx["fixture"]["trigger"],
        registry_path=ctx["registry"],
        ledger_path=ctx["ledger"],
        target_run_id="direct:corrupt-registry-row",
        inputs=ctx["fixture"]["inputs"],
        timestamp=ctx["now"],
    )
    assert direct == {"matched": True, "invoked": False, "reason": "artifact_invalid"}, direct
    compiled = capability_lifecycle.invoke_compiled_target(
        ctx["capability_id"],
        trigger=ctx["fixture"]["trigger"],
        target_run_id="compiled:corrupt-registry-row",
        ledger_path=ctx["ledger"],
        target_registry_path=ctx["registry"],
        inputs=ctx["fixture"]["inputs"],
        timestamp=ctx["now"],
    )
    assert compiled == {"matched": True, "invoked": False, "reason": "artifact_invalid"}, compiled
    assert "causal_edge" not in compiled
    assert ctx["ledger"].read_bytes() == ledger_before
    assert ctx["registry"].read_bytes() == registry_before


@pytest.mark.parametrize("rollback", [{}, {"phase": "unknown"}, {"phase": []}])
def test_unknown_rollback_phase_refuses_both_entrypoints_without_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rollback
) -> None:
    ctx = _registered_workflow(tmp_path, monkeypatch)
    binding = capability_targets.get_binding(ctx["version_id"], registry_path=ctx["registry"])
    payload = json.loads(ctx["registry"].read_text())
    payload["targets"][ctx["version_id"]]["rollback"] = rollback
    ctx["registry"].write_text(json.dumps(payload))
    before = (ctx["registry"].read_bytes(), ctx["ledger"].read_bytes())

    def forbidden(*args, **kwargs):
        raise AssertionError("invalid rollback must refuse before the producer")

    monkeypatch.setattr(compiler, "dry_run_workflow_rail", forbidden)
    direct = capability_targets.invoke_target(
        binding,
        trigger=ctx["fixture"]["trigger"],
        registry_path=ctx["registry"],
        ledger_path=ctx["ledger"],
        inputs=ctx["fixture"]["inputs"],
        timestamp=ctx["now"],
    )
    compiled = capability_lifecycle.invoke_compiled_target(
        ctx["capability_id"],
        trigger=ctx["fixture"]["trigger"],
        target_run_id="compiled:unknown-rollback",
        ledger_path=ctx["ledger"],
        target_registry_path=ctx["registry"],
        inputs=ctx["fixture"]["inputs"],
        timestamp=ctx["now"],
    )
    assert direct == compiled == {"matched": True, "invoked": False, "reason": "rollback_pending"}
    assert (ctx["registry"].read_bytes(), ctx["ledger"].read_bytes()) == before


@pytest.mark.parametrize("identity", [None, "different-version"])
def test_loaded_binding_identity_refuses_before_producer_without_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, identity
) -> None:
    ctx = _registered_workflow(tmp_path, monkeypatch)
    binding = capability_targets.get_binding(ctx["version_id"], registry_path=ctx["registry"])
    payload = json.loads(ctx["registry"].read_text())
    row = payload["targets"][ctx["version_id"]]
    row.pop("capability_version_id")
    if identity is not None:
        row["capability_version_id"] = identity
    ctx["registry"].write_text(json.dumps(payload))
    before = (ctx["registry"].read_bytes(), ctx["ledger"].read_bytes())

    def forbidden(*args, **kwargs):
        raise AssertionError("invalid loaded identity must refuse before the producer")

    monkeypatch.setattr(compiler, "dry_run_workflow_rail", forbidden)
    result = capability_targets.invoke_target(
        binding,
        trigger=ctx["fixture"]["trigger"],
        registry_path=ctx["registry"],
        ledger_path=ctx["ledger"],
        inputs=ctx["fixture"]["inputs"],
        timestamp=ctx["now"],
    )
    assert result == {"matched": True, "invoked": False, "reason": "artifact_invalid"}
    assert (ctx["registry"].read_bytes(), ctx["ledger"].read_bytes()) == before


def test_absent_binding_still_reports_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _registered_workflow(tmp_path, monkeypatch)
    payload = json.loads(ctx["registry"].read_text())
    del payload["targets"][ctx["version_id"]]
    ctx["registry"].write_text(json.dumps(payload))
    assert capability_targets.get_binding(ctx["version_id"], registry_path=ctx["registry"]) is None
    with pytest.raises(ValueError, match="lifecycle version has no target binding"):
        capability_lifecycle.invoke_compiled_target(
            ctx["capability_id"],
            trigger=ctx["fixture"]["trigger"],
            target_run_id="missing",
            ledger_path=ctx["ledger"],
            target_registry_path=ctx["registry"],
            timestamp=ctx["now"],
        )


def test_high_risk_candidate_uses_bounded_brain_owner_question(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "brain.db")
    now = int(time.time())
    ledger = tmp_path / "capabilities.json"
    predecessor = "baseline:workflow"
    capabilities.save({predecessor: _active_predecessor(predecessor, now)}, ledger)
    plan = compiler.compile_workflow_rail(compiler.reference_workflow_source())
    result = capability_lifecycle.register_compiled_target(
        "workflow",
        plan,
        lifecycle_policy={
            "risk_level": "destructive",
            "side_effect_policy": "irreversible_write",
            "owner_question_expires_days": 0.001,
        },
        matcher={"repository": "owner/repo", "task_types": ["implement"]},
        predecessor=predecessor,
        ledger_path=ledger,
        target_registry_path=tmp_path / "targets.json",
        expiry=now + 86400,
    )
    assert result["status"] == "owner_question" and result["binding"] is None
    question = result["capability"]["owner_question"]
    assert question["question_id"].startswith("q-") and question["status"] == "open"
    assert feedback.open_owner_questions()[0]["default_action"] == "keep_shadow_unexported"
    assert feedback.expire_owner_questions(now=now + 1000) == 1


def test_lifecycle_dry_states_remain_distinct() -> None:
    now = int(time.time())
    no_work = capabilities._blank_capability("no-work")
    no_work["status"] = "shadow"
    matched = capabilities._blank_capability("matched")
    matched.update({"status": "shadow", "last_match": now})
    invoked = capabilities._blank_capability("invoked")
    invoked.update({"status": "shadow", "last_match": now, "last_invocation": now})
    gated = capabilities._blank_capability("gated")
    gated.update({"status": "generated", "gate_reason": "bounded owner question"})
    stale = _active_predecessor("stale", now - 40 * 86400)
    stale["event_history"].append(
        {"type": "outcome", "timestamp": now - 40 * 86400, "ref": "old-outcome"}
    )
    assert capabilities.classify_liveness(no_work, now=now) == "no_matching_work"
    assert capabilities.classify_liveness(matched, now=now) == "matched_not_invoked"
    assert capabilities.classify_liveness(invoked, now=now) == "invoked_without_outcomes"
    assert capabilities.classify_liveness(gated, now=now) == "deliberately_gated"
    assert capabilities.classify_liveness(stale, now=now) == "stale_active"


@pytest.fixture
def regressing_canary(tmp_path: Path) -> tuple[dict, Path]:
    plan = compiler.compile_workflow_rail(compiler.reference_workflow_source())
    registry = tmp_path / "targets.json"
    binding = capability_targets.register_target(
        "workflow",
        plan,
        registry_path=registry,
        predecessor="baseline:workflow",
        lifecycle_policy={"risk_level": "low", "side_effect_policy": "read_only"},
    )
    if os.environ.get("ORCH_TEST_REMOVE_ROLLBACK_TARGET") == "1":
        payload = json.loads(registry.read_text(encoding="utf-8"))
        payload["targets"][binding["capability_version_id"]]["predecessor"] = None
        registry.write_text(json.dumps(payload), encoding="utf-8")
    return binding, registry


def test_regression_requires_automatic_rollback(regressing_canary: tuple[dict, Path]) -> None:
    binding, registry = regressing_canary
    current = capability_targets.get_binding(
        binding["capability_version_id"], registry_path=registry
    )
    capability_targets.prepare_rollback(
        current, registry_path=registry, reason="joined outcome regressed"
    )


@pytest.mark.parametrize("dry_run", [False, True])
def test_dispatch_unexecuted_selection_records_baseline_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dry_run: bool
) -> None:
    import dispatcher

    ctx = _registered_workflow(tmp_path, monkeypatch)

    def unexpected_invocation(*args, **kwargs):
        pytest.fail("planning must not execute the selected target")

    monkeypatch.setattr(capability_lifecycle, "invoke_compiled_target", unexpected_invocation)
    assignment = {
        "target": ctx["fixture"]["trigger"]["target"],
        "task_type": ctx["fixture"]["trigger"]["task_type"],
        "agent": "codex",
        "mode": "full",
        "selected_capability_id": ctx["capability_id"],
        "selected_capability_version_id": ctx["version_id"],
        "capability_ids": [],
        "capability_version_ids": [],
    }
    with local_dispatch(tmp_path) as worker:
        dispatch = dispatcher.plan_dispatch(assignment, dry_run=dry_run)
        assert worker.call_count == 0
    assert dispatch["capability_ids"] == []
    assert dispatch["capability_version_ids"] == []
    metadata = dispatch["routing_metadata"]
    assert metadata["capability_dispatch_result"] == {
        "invoked": False,
        "accepted": False,
        "reason": "dry_run" if dry_run else "not_executed",
    }
    assert metadata["capability_fallback"] == "baseline"
    with feedback._conn() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM influence_edges WHERE influence_type='capability' AND accepted=1"
        ).fetchone()[0]
    assert count == 0
    binding = capability_targets.get_binding(ctx["version_id"], registry_path=ctx["registry"])
    assert len(binding["invocations"]) == 0


def test_dispatcher_dispatch_workflow_produces_one_accepted_edge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import dispatcher

    ctx = _registered_workflow(tmp_path, monkeypatch)
    monkeypatch.setattr(capabilities, "REG", ctx["ledger"])
    monkeypatch.setattr(capabilities.load, "__defaults__", (ctx["ledger"],))
    monkeypatch.setattr(capability_lifecycle, "DEFAULT_TARGET_REGISTRY", ctx["registry"])
    infra_id = "thompson-hybrid-routing"
    infra_version = "capability-version:infrastructure"
    infra = capabilities._blank_capability(infra_id)
    infra["capability_version_id"] = infra_version
    _mutate_json(ctx["ledger"], "capabilities", **{infra_id: infra})
    run_id = "dispatch:workflow:1"
    trigger = ctx["fixture"]["trigger"]
    assignment = {
        "target": trigger["target"],
        "task_type": trigger["task_type"],
        "agent": "codex",
        "mode": "full",
        "selected_capability_id": ctx["capability_id"],
        "selected_capability_version_id": ctx["version_id"],
        "exploration": True,
        "exploration_mode": "thompson-hybrid",
        "capability_ids": [],
        "capability_version_ids": [],
    }
    with local_dispatch(tmp_path) as worker:
        preview = dispatcher.plan_dispatch(assignment, dry_run=True)
        assert preview["capability_ids"] == [infra_id]
        assert preview["routing_metadata"]["capability_dispatch_result"]["reason"] == "dry_run"
        assert (
            capability_targets.get_binding(ctx["version_id"], registry_path=ctx["registry"])[
                "invocations"
            ]
            == []
        )
        d = dispatcher.plan_dispatch(assignment)
        d["run_id"] = run_id
        assert dispatcher._spawn(d) == 1234
        assert worker.call_count == 1
    assert d["capability_ids"] == [infra_id, ctx["capability_id"]]
    assert d["capability_version_ids"] == [infra_version, ctx["version_id"]]
    receipt = d["routing_metadata"]["capability_dispatch_result"]
    assert receipt["invoked"] is True and receipt["accepted"] is True
    with feedback._conn() as conn:
        edges = conn.execute(
            "SELECT capability_id,capability_version_id,source_event_id,target_event_id "
            "FROM influence_edges WHERE target_run_id=? "
            "AND influence_type='capability' AND accepted=1",
            (run_id,),
        ).fetchall()
        source = json.loads(
            conn.execute(
                "SELECT payload_json FROM completion_events WHERE event_id=?",
                (receipt["source_event_id"],),
            ).fetchone()[0]
        )
        producer = conn.execute(
            "SELECT run_id,capability_id,capability_version_id FROM completion_events "
            "WHERE event_id=?",
            (receipt["source_event_id"],),
        ).fetchone()
        consumer = conn.execute(
            "SELECT run_id FROM completion_events WHERE event_id=?",
            (receipt["target_event_id"],),
        ).fetchone()
        stored = json.loads(
            conn.execute("SELECT routing_metadata FROM runs WHERE run_id=?", (run_id,)).fetchone()[
                0
            ]
        )
    infrastructure_edges = [row for row in edges if row[0] == infra_id]
    compiled_edges = [row for row in edges if row[0] == ctx["capability_id"]]
    assert len(infrastructure_edges) == 1
    assert tuple(infrastructure_edges[0][:2]) == (infra_id, infra_version)
    assert len(compiled_edges) == 1
    assert len(edges) == 2
    assert tuple(compiled_edges[0]) == (
        ctx["capability_id"],
        ctx["version_id"],
        receipt["source_event_id"],
        receipt["target_event_id"],
    )
    assert source["artifact_refs"] == [
        {"artifact_id": receipt["consumer_ref"], "kind": "consumer_receipt"}
    ]
    assert tuple(producer) == (
        f"capability-producer:{receipt['invocation_id']}",
        ctx["capability_id"],
        ctx["version_id"],
    )
    assert tuple(consumer) == (run_id,)
    assert source["result"]["accepted"] is True
    assert stored["capability_dispatch_result"] == receipt
    assert "capability_fallback" not in stored
    with feedback._conn() as conn:
        events = conn.execute(
            "SELECT phase,payload_json FROM completion_events WHERE run_id=? AND event_type='decision'",
            (run_id,),
        ).fetchall()
    assert {
        row[0] for row in events if ctx["capability_id"] in json.loads(row[1])["capability_ids"]
    } == {"trigger", "decision"}
    binding = capability_targets.get_binding(ctx["version_id"], registry_path=ctx["registry"])
    assert len(binding["invocations"]) == 1
    assert binding["invocations"][0]["consumer_ref"] == receipt["consumer_ref"]
    assert binding["invocations"][0]["invocation_id"] == receipt["invocation_id"]

    # Updating run metadata changes completion-event identity, but must preserve the
    # existing infrastructure edge and the independently proven compiled receipt.
    feedback.record_run(
        run_id,
        trigger["target"],
        trigger["task_type"],
        "codex",
        capability_ids=d["capability_ids"],
        capability_version_ids=d["capability_version_ids"],
        routing_metadata=d["routing_metadata"],
    )
    with feedback._conn() as conn:
        repeated_edges = conn.execute(
            "SELECT capability_id,capability_version_id,source_event_id,target_event_id "
            "FROM influence_edges WHERE target_run_id=? "
            "AND influence_type='capability' AND accepted=1",
            (run_id,),
        ).fetchall()
    assert sorted(map(tuple, repeated_edges)) == sorted(map(tuple, edges))


@pytest.mark.parametrize("role_failure", [False, True])
@pytest.mark.parametrize(
    ("case", "reason"),
    [
        ("refused", "kill_switch"),
        ("failed", "invoke_failed:ValueError"),
        ("expired", "expired"),
        ("unsupported", "unsupported_target_kind"),
        ("version_changed", "selected_version_mismatch"),
        ("not_consumed", "output_not_consumed"),
        ("receipt_missing", "consumption_receipt_missing"),
    ],
)
def test_dispatch_fallback_has_no_compiled_credit_and_keeps_infrastructure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    reason: str,
    role_failure: bool,
) -> None:
    import dispatcher

    ctx = _registered_workflow(tmp_path, monkeypatch)
    monkeypatch.setattr(capabilities, "REG", ctx["ledger"])
    monkeypatch.setattr(capability_lifecycle, "DEFAULT_TARGET_REGISTRY", ctx["registry"])
    infra_id = "thompson-hybrid-routing"
    infra = capabilities._blank_capability(infra_id)
    infra["capability_version_id"] = "capability-version:infrastructure"
    _mutate_json(ctx["ledger"], "capabilities", **{infra_id: infra})
    if role_failure:

        def fail_role_activation(*args, **kwargs):
            raise RuntimeError("optional role activation failed")

        monkeypatch.setattr(roles, "activate_dispatch_roles", fail_role_activation)
    if case == "refused":
        monkeypatch.setenv("ORCH_CAPABILITY_WORKFLOW_ENABLED", "0")
    elif case == "expired":
        _mutate_json(ctx["ledger"], "capabilities", ctx["capability_id"], expiry=ctx["now"] - 1)
    elif case == "unsupported":
        _mutate_json(ctx["registry"], "targets", ctx["version_id"], kind="role")
    elif case == "failed":

        def fail(_artifact):
            raise ValueError("fixture producer failed")

        monkeypatch.setattr(compiler, "dry_run_workflow_rail", fail)
    elif case in {"not_consumed", "receipt_missing"}:
        monkeypatch.setattr(
            compiler,
            "consume_workflow_output",
            lambda output: {
                "consumed": case == "receipt_missing",
                "receipt_id": None if case == "receipt_missing" else "receipt:unused",
            },
        )
    assignment = {
        "agent": "codex",
        "mode": "full",
        "target": ctx["fixture"]["trigger"]["target"],
        "task_type": ctx["fixture"]["trigger"]["task_type"],
        "selected_capability_id": ctx["capability_id"],
        "selected_capability_version_id": (
            "capability-version:old" if case == "version_changed" else ctx["version_id"]
        ),
        "exploration": True,
        "exploration_mode": "thompson-hybrid",
    }
    with local_dispatch(tmp_path) as worker:
        d = dispatcher.plan_dispatch(assignment)
        if role_failure:
            assert d["role_activation"]["error"] == "optional role activation failed"
        else:
            assert len(d["role_activation"]["selectors"]) == 2
            assert all(not selector["invoked"] for selector in d["role_activation"]["selectors"])
        d["run_id"] = f"dispatch:fallback:{case}"
        assert dispatcher._spawn(d) == 1234
        assert worker.call_count == 1
    assert d["capability_ids"] == [infra_id]
    assert ctx["version_id"] not in d["capability_version_ids"]
    with feedback._conn() as conn:
        selected_count = conn.execute(
            "SELECT COUNT(*) FROM influence_edges WHERE target_run_id=? "
            "AND capability_id=? AND accepted=1",
            (d["run_id"], ctx["capability_id"]),
        ).fetchone()[0]
        accepted_edges = conn.execute(
            "SELECT capability_id,capability_version_id FROM influence_edges "
            "WHERE target_run_id=? AND influence_type='capability' AND accepted=1",
            (d["run_id"],),
        ).fetchall()
        metadata = json.loads(
            conn.execute(
                "SELECT routing_metadata FROM runs WHERE run_id=?", (d["run_id"],)
            ).fetchone()[0]
        )
    assert selected_count == 0
    assert len(accepted_edges) == 1
    assert tuple(accepted_edges[0]) == (infra_id, infra["capability_version_id"])
    assert metadata["capability_fallback"] == "baseline"
    assert metadata["capability_dispatch_result"]["accepted"] is False
    assert metadata["capability_dispatch_result"]["reason"] == reason
