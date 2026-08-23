from __future__ import annotations

import copy
import json
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

import capabilities
import capability_compiler as compiler
import env_prereq
import feedback

QUICK_VALIDATE = (
    Path.home()
    / ".codex"
    / "skills"
    / ".system"
    / "skill-creator"
    / "scripts"
    / "quick_validate.py"
)

# EVERY test here builds from `compiler.reference_skill_source()`, which hashes a real installed
# skill resource under ~/.codex/skills — on purpose: the skill compiler is exercised against a
# genuine installed skill, not a synthetic fixture, because a fixture could not catch a manifest
# that fails the real validator. So the resource is this file's prerequisite in full.
#
# `skipif` rather than a module-level raise: skipif leaves all 7 items COLLECTED and skips them
# individually with the reason attached, while a raise at import time would drop the collection
# count by 7 — and a dropped collection count is exactly what verify.py's floor exists to catch.
_SKILL_RESOURCE_ABSENT = env_prereq.skill_resource_absent()
pytestmark = pytest.mark.skipif(bool(_SKILL_RESOURCE_ABSENT), reason=_SKILL_RESOURCE_ABSENT or "")


@pytest.fixture
def generated_skill(tmp_path: Path) -> dict:
    package = compiler.compile_skill_package(
        compiler.reference_skill_source(), output_root=tmp_path / "candidates"
    )
    return package


def test_reusable_procedure_compiles_to_valid_skill_package(generated_skill: dict) -> None:
    skill_dir = Path(generated_skill["package_path"])
    manifest = compiler.validate_skill_package(skill_dir)
    assert manifest == generated_skill["manifest"]
    assert manifest["schema"] == compiler.SKILL_MANIFEST_SCHEMA
    assert manifest["version"] == 1
    assert manifest["name"] == "audit-handoff-evidence"
    assert manifest["content_hash"].startswith("sha256:")
    assert manifest["lifecycle"]["state"] == "shadow"
    assert manifest["lifecycle"]["globally_installed"] is False
    assert not (skill_dir / "agents" / "openai.yaml").exists()

    skill_md = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    assert skill_md.startswith("---\nname: audit-handoff-evidence\ndescription:")
    assert "## Workflow" in skill_md
    assert "## Safety boundaries" in skill_md
    assert "## Expected artifacts" in skill_md
    assert "## Validation" in skill_md
    assert "## Candidate lifecycle" in skill_md
    for resource in manifest["resources"]:
        assert (skill_dir / resource["target"]).is_file(), "skill resource does not exist"
        assert f"({resource['target']})" in skill_md
        assert compiler._sha256_file(skill_dir / resource["target"]) == resource["content_hash"]

    validated = subprocess.run(
        [sys.executable, str(QUICK_VALIDATE), str(skill_dir)],
        text=True,
        capture_output=True,
        check=False,
    )
    assert validated.returncode == 0, validated.stdout + validated.stderr
    command = subprocess.run(
        shlex.split(manifest["validation_command"]),
        cwd=skill_dir,
        text=True,
        capture_output=True,
        check=False,
    )
    assert command.returncode == 0
    assert "Usage: audit_code_root.sh" in command.stdout


def test_duplicate_installed_skill_and_fingerprint_are_rejected(tmp_path: Path) -> None:
    source = compiler.reference_skill_source()
    installed = tmp_path / "installed" / source["name"]
    installed.mkdir(parents=True)
    (installed / "SKILL.md").write_text(
        f"---\nname: {source['name']}\ndescription: Existing equivalent.\n---\n\n# Existing\n",
        encoding="utf-8",
    )
    decision = compiler.compile_skill_candidate(
        source,
        output_root=tmp_path / "candidate-one",
        installed_skill_roots=[tmp_path / "installed"],
    )
    assert decision["status"] == "routed" and decision["target"] == "existing_skill"
    assert f"duplicate installed skill: {source['name']}" in decision["rejection_reasons"]

    fingerprint_decision = compiler.compile_skill_candidate(
        source,
        output_root=tmp_path / "candidate-two",
        existing_capability_fingerprints=[source["source_fingerprint"]],
    )
    assert fingerprint_decision["target"] == "existing_skill"
    assert "duplicate capability fingerprint" in fingerprint_decision["rejection_reasons"]


def test_secret_bearing_resource_is_rejected_without_package(tmp_path: Path) -> None:
    secret_script = tmp_path / "unsafe.py"
    secret_script.write_text("TOKEN=ghp_supersecret123456789\n", encoding="utf-8")
    source = compiler.reference_skill_source()
    source["name"] = "unsafe-audit-handoff"
    source["capability_id"] = "capability:unsafe-audit-handoff"
    source["source_fingerprint"] = compiler.stable_hash("unsafe-source", "one")
    source["resources"] = [
        {
            "kind": "scripts",
            "source_path": str(secret_script),
            "target": "scripts/unsafe.py",
            "content_hash": compiler._sha256_file(secret_script),
        }
    ]
    decision = compiler.compile_skill_candidate(source, output_root=tmp_path / "candidates")
    assert decision["status"] == "rejected" and decision["target"] is None
    assert "secret-bearing resource: scripts/unsafe.py" in decision["rejection_reasons"]
    assert not (tmp_path / "candidates" / source["name"]).exists()


@pytest.mark.parametrize(
    ("field", "value", "reason", "target"),
    [
        ("reuse_scope", "repo_only", "repo-only procedure: route to playbook", "playbook"),
        (
            "procedure_class",
            "deterministic_gate",
            "deterministic gate: route to acceptance_gate",
            "acceptance_gate",
        ),
    ],
)
def test_non_skill_procedures_route_to_correct_target(
    tmp_path: Path, field: str, value: str, reason: str, target: str
) -> None:
    source = compiler.reference_skill_source()
    source[field] = value
    decision = compiler.compile_skill_candidate(source, output_root=tmp_path / field)
    assert decision["status"] == "routed"
    assert decision["target"] == target
    assert reason in decision["rejection_reasons"]
    assert decision["executable"] is False


def test_shadow_invocation_records_version_artifacts_influence_and_durability(
    generated_skill: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "feedback" / "orchestrator.db")
    target_run = "shadow-skill-target"
    feedback.record_run(
        target_run,
        "owner/repo#20",
        "review",
        "codex",
        pr_number=20,
    )
    artifact = {
        "artifact_id": "audit-handoff:owner/repo#20",
        "kind": "audit-handoff",
        "content_hash": feedback._completion_hash("bounded handoff result"),
        "ref_class": "shadow",
    }
    ledger = tmp_path / "capabilities.json"
    result = compiler.shadow_invoke_skill_package(
        Path(generated_skill["package_path"]),
        task_ref="owner/repo#20:audit-handoff",
        influenced_run_ids=[target_run],
        artifact_refs=[artifact],
        ledger_path=ledger,
        accepted=True,
        downstream_outcome={
            "verifier_verdict": "PASS",
            "adjudicated_verdict": "PASS",
            "merged": True,
            "durability": "durable",
        },
    )
    assert result["accepted"] and result["baseline_changed"] is False
    manifest = generated_skill["manifest"]
    with feedback._conn() as conn:
        event_row = conn.execute(
            "SELECT status,validation_status,payload_json FROM completion_events WHERE event_id=?",
            (result["invocation"]["event_id"],),
        ).fetchone()
        edge = conn.execute(
            "SELECT accepted,counterfactual,outcome_verdict,merged,durability FROM influence_edges "
            "WHERE target_run_id=? AND influence_type='skill'",
            (target_run,),
        ).fetchone()
    payload = json.loads(event_row[2])
    assert event_row[:2] == ("succeeded", "accepted")
    assert payload["skill"] == {
        "accepted": True,
        "phase": "execution",
        "result": "succeeded",
        "skill_id": manifest["name"],
        "version_hash": manifest["content_hash"],
    }
    assert artifact["artifact_id"] in {row["artifact_id"] for row in payload["artifact_refs"]}
    assert edge == (1, 0, "PASS", 1, "durable")

    cap = capabilities.load(ledger, create=False)[manifest["capability_id"]]
    assert cap["status"] == "shadow"
    assert cap["expiry"] == manifest["lifecycle"]["expires_at"]
    assert cap["rollback"] == manifest["lifecycle"]["rollback"]
    assert result["outcome_ref"] in cap["outcome_links"]
    assert all(
        (cap["activation_evidence"].get(probe) or {}).get("passed")
        for probe in capabilities.ACTIVE_PROBES
    )


def test_rejected_shadow_use_remains_counterfactual(
    generated_skill: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "rejected" / "orchestrator.db")
    target_run = "rejected-skill-target"
    feedback.record_run(target_run, "owner/repo#21", "review", "codex")
    result = compiler.shadow_invoke_skill_package(
        Path(generated_skill["package_path"]),
        task_ref="owner/repo#21:audit-handoff",
        influenced_run_ids=[target_run],
        artifact_refs=[],
        ledger_path=tmp_path / "rejected-capabilities.json",
        accepted=False,
        downstream_outcome={
            "adjudicated_verdict": "PASS",
            "merged": True,
            "durability": "durable",
        },
    )
    with feedback._conn() as conn:
        edge = conn.execute(
            "SELECT accepted,counterfactual,outcome_verdict,merged,durability FROM influence_edges"
        ).fetchone()
        payload = json.loads(
            conn.execute(
                "SELECT payload_json FROM completion_events WHERE event_id=?",
                (result["invocation"]["event_id"],),
            ).fetchone()[0]
        )
    assert edge == (0, 1, None, None, None)
    assert payload["skill"]["accepted"] is False
    assert payload["skill"]["result"] == "failed"
