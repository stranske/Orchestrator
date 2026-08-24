from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest
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
