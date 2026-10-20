"""Hermetic coverage for issue 807's second ranked target: capability_compiler skill paths.

Existing skill-compiler tests skip on bare CI runners because they hash a live
`~/.codex/skills` resource. These tests synthesize every resource under a temp
dir so the same code runs in CI without installed skills.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import capabilities
import capability_compiler as compiler
import feedback
from capability_ir import stable_hash


@pytest.fixture(autouse=True)
def isolated_runtime(tmp_path, monkeypatch):
    state = tmp_path / "state"
    runtime = tmp_path / "runtime"
    state.mkdir()
    runtime.mkdir()
    monkeypatch.setenv("ORCH_STATE_DIR", str(state))
    monkeypatch.setenv("ORCH_LOCAL_RUNTIME", str(runtime))
    monkeypatch.setenv("ORCH_FEEDBACK_DB", str(runtime / "feedback" / "orchestrator.db"))
    monkeypatch.setattr(feedback, "DB_PATH", runtime / "feedback" / "orchestrator.db")


def _script(tmp_path: Path, name: str = "helper.sh", body: str = "#!/bin/bash\necho ok\n") -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


def _skill_source(tmp_path: Path, **overrides) -> dict:
    script = _script(tmp_path)
    source = {
        "schema": compiler.SKILL_SOURCE_SCHEMA,
        "version": compiler.SKILL_VERSION,
        "capability_id": "capability:issue807-audit-handoff",
        "source_ir_ref": "capability-ir:issue807-audit-handoff",
        "source_fingerprint": stable_hash("issue807-skill", "v1"),
        "procedure_class": "reusable_procedure",
        "reuse_scope": "shared",
        "name": "issue807-audit-handoff",
        "description": "Prepare bounded workspace-audit evidence for issue 807 coverage.",
        "triggers": [
            "audit workspace hygiene after completing delegated work",
            "prepare evidence for an audit or workflow handoff",
        ],
        "instructions": [
            "Run the bundled workspace audit script in report-only mode.",
            "Classify reported entries without deleting anything.",
            "Summarize the audit result for the completion handoff.",
        ],
        "resources": [
            {
                "kind": "scripts",
                "source_path": str(script),
                "target": "scripts/helper.sh",
                "content_hash": compiler._sha256_file(script),
            }
        ],
        "safety_boundaries": [
            "Keep the bundled audit script in report-only mode.",
            "Do not delete, archive, or modify workspace contents.",
        ],
        "expected_artifacts": ["workspace-audit-report", "audit-handoff-summary"],
        "validation_command": "bash scripts/helper.sh --help",
        "expires_at": 1893456000,
        "predecessor": "capability:completion-event-lineage",
        "rollback": {
            "action": "retire_shadow_package",
            "reason": "shadow evidence expires or duplicates an installed skill",
        },
    }
    source.update(overrides)
    return source


def test_bounded_lists_and_skill_directory_helpers(tmp_path):
    empty, errors = compiler._bounded_string_list([], "triggers", limit=2)
    assert empty == [] and "non-empty list" in errors[0]

    too_many, errors = compiler._bounded_string_list(["a", "b", "c"], "triggers", limit=2)
    assert too_many == ["a", "b"]
    assert "exceeds item limit" in errors[0]

    bad_items, errors = compiler._bounded_string_list(["ok", "  ", "x" * 600], "triggers", limit=4)
    assert bad_items == ["ok"]
    assert any("non-empty string" in e for e in errors)
    assert any("length limit" in e for e in errors)

    skill = tmp_path / "installed" / "demo"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: demo\ndescription: Existing.\n---\n\n# Demo\n", encoding="utf-8"
    )
    orphan = tmp_path / "installed" / "broken"
    orphan.mkdir()
    (orphan / "SKILL.md").write_text("no frontmatter\n", encoding="utf-8")
    found = compiler._skill_directories([tmp_path / "installed", skill])
    assert skill.resolve() in found
    assert compiler._skill_frontmatter_name(skill / "SKILL.md") == "demo"
    assert compiler._skill_frontmatter_name(orphan / "SKILL.md") is None
    assert compiler._skill_frontmatter_name(tmp_path / "missing.md") is None

    manifest_dir = skill / ".orchestrator"
    manifest_dir.mkdir()
    (manifest_dir / "manifest.json").write_text(
        json.dumps({"source_fingerprint": "sha256:" + ("ab" * 32)}), encoding="utf-8"
    )
    (tmp_path / "installed" / "bad-manifest").mkdir()
    bad = tmp_path / "installed" / "bad-manifest"
    (bad / "SKILL.md").write_text("---\nname: bad\ndescription: x\n---\n", encoding="utf-8")
    (bad / ".orchestrator").mkdir()
    (bad / ".orchestrator" / "manifest.json").write_text("{bad", encoding="utf-8")
    names, fingerprints = compiler._installed_skill_evidence([tmp_path / "installed"])
    assert "demo" in names
    assert ("sha256:" + ("ab" * 32)) in fingerprints


def test_safe_resource_rejects_malformed_and_accepts_valid(tmp_path):
    none, errors = compiler._safe_resource("not-a-dict", 0)
    assert none is None and "must contain" in errors[0]

    script = _script(tmp_path)
    good, errors = compiler._safe_resource(
        {
            "kind": "scripts",
            "source_path": str(script),
            "target": "scripts/helper.sh",
            "content_hash": compiler._sha256_file(script),
        },
        0,
    )
    assert errors == []
    assert good["target"] == "scripts/helper.sh"

    secret = _script(tmp_path, "secret.py", "TOKEN=ghp_supersecret123456789\n")
    none, errors = compiler._safe_resource(
        {
            "kind": "scripts",
            "source_path": str(secret),
            "target": "scripts/secret.py",
            "content_hash": compiler._sha256_file(secret),
        },
        1,
    )
    assert none is None
    assert any("secret-bearing resource" in e for e in errors)

    none, errors = compiler._safe_resource(
        {
            "kind": "scripts",
            "source_path": str(tmp_path / "missing.sh"),
            "target": "assets/wrong.ext",
            "content_hash": "sha256:" + ("00" * 32),
        },
        2,
    )
    assert none is None
    assert "resources[2].source_path does not exist" in errors
    assert "resources[2].target must stay under scripts/" in errors


def test_compile_skill_package_and_candidate_success(tmp_path):
    source = _skill_source(tmp_path)
    package = compiler.compile_skill_package(source, output_root=tmp_path / "candidates")
    skill_dir = Path(package["package_path"])
    manifest = compiler.validate_skill_package(skill_dir)
    assert manifest["name"] == "issue807-audit-handoff"
    assert manifest["lifecycle"]["state"] == "shadow"
    assert (skill_dir / "scripts" / "helper.sh").is_file()
    assert "scripts/helper.sh" in (skill_dir / "SKILL.md").read_text(encoding="utf-8")

    decision = compiler.compile_skill_candidate(
        _skill_source(tmp_path, name="issue807-second", capability_id="capability:issue807-second"),
        output_root=tmp_path / "second",
    )
    assert decision["status"] == "compiled_shadow"
    assert decision["target"] == "skill"
    assert decision["executable"] is False


def test_compile_skill_rejects_invalid_inputs_and_duplicates(tmp_path):
    with pytest.raises(compiler.SkillCompileError, match="must be an object"):
        compiler.compile_skill_package("bad", output_root=tmp_path / "out")  # type: ignore[arg-type]

    source = _skill_source(tmp_path, extra_field="nope")
    with pytest.raises(compiler.SkillCompileError) as exc:
        compiler.compile_skill_package(source, output_root=tmp_path / "out")
    assert any("unsupported fields" in r for r in exc.value.reasons)

    routed = compiler.compile_skill_candidate(
        _skill_source(tmp_path, procedure_class="deterministic_gate"),
        output_root=tmp_path / "gate",
    )
    assert routed["target"] == "acceptance_gate"

    playbook = compiler.compile_skill_candidate(
        _skill_source(tmp_path, reuse_scope="repo_only"),
        output_root=tmp_path / "playbook",
    )
    assert playbook["target"] == "playbook"

    installed = tmp_path / "installed" / "issue807-audit-handoff"
    installed.mkdir(parents=True)
    (installed / "SKILL.md").write_text(
        "---\nname: issue807-audit-handoff\ndescription: Existing.\n---\n",
        encoding="utf-8",
    )
    dup = compiler.compile_skill_candidate(
        _skill_source(tmp_path),
        output_root=tmp_path / "dup",
        installed_skill_roots=[tmp_path / "installed"],
    )
    assert dup["target"] == "existing_skill"
    assert any("duplicate installed skill" in r for r in dup["rejection_reasons"])

    fingerprint = compiler.compile_skill_candidate(
        _skill_source(tmp_path, name="issue807-fp", capability_id="capability:issue807-fp"),
        output_root=tmp_path / "fp",
        existing_capability_fingerprints=[stable_hash("issue807-skill", "v1")],
    )
    assert fingerprint["target"] == "existing_skill"

    # Overwrite refusal after a successful first compile.
    first = compiler.compile_skill_package(
        _skill_source(tmp_path, name="issue807-once", capability_id="capability:issue807-once"),
        output_root=tmp_path / "once",
    )
    assert Path(first["package_path"]).is_dir()
    with pytest.raises(compiler.SkillCompileError, match="refusing to overwrite"):
        compiler.compile_skill_package(
            _skill_source(tmp_path, name="issue807-once", capability_id="capability:issue807-once"),
            output_root=tmp_path / "once",
        )


def test_validate_skill_package_catches_tampering(tmp_path):
    package = compiler.compile_skill_package(
        _skill_source(tmp_path), output_root=tmp_path / "candidates"
    )
    skill_dir = Path(package["package_path"])
    manifest_path = skill_dir / ".orchestrator" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["version"] = 99
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(AssertionError, match="invalid skill manifest"):
        compiler.validate_skill_package(skill_dir)

    with pytest.raises(AssertionError, match="does not exist"):
        compiler.validate_skill_package(tmp_path / "missing-skill")

    # Rebuild a clean package and break SKILL.md / resource linkage.
    clean = compiler.compile_skill_package(
        _skill_source(tmp_path, name="issue807-tamper", capability_id="capability:issue807-tamper"),
        output_root=tmp_path / "tamper",
    )
    clean_dir = Path(clean["package_path"])
    (clean_dir / "SKILL.md").write_text("no frontmatter\n", encoding="utf-8")
    with pytest.raises(AssertionError, match="frontmatter"):
        compiler.validate_skill_package(clean_dir)

    clean2 = compiler.compile_skill_package(
        _skill_source(tmp_path, name="issue807-hash", capability_id="capability:issue807-hash"),
        output_root=tmp_path / "hash",
    )
    clean2_dir = Path(clean2["package_path"])
    (clean2_dir / "scripts" / "helper.sh").write_text(
        "#!/bin/bash\necho changed\n", encoding="utf-8"
    )
    with pytest.raises(AssertionError, match="hash mismatch"):
        compiler.validate_skill_package(clean2_dir)


def test_shadow_invoke_records_and_registers(tmp_path):
    package = compiler.compile_skill_package(
        _skill_source(tmp_path), output_root=tmp_path / "candidates"
    )
    ledger = tmp_path / "capabilities.json"
    target_run = "issue807-shadow-run"
    feedback.record_run(target_run, "owner/repo#807", "review", "codex", pr_number=807)
    artifact = {
        "artifact_id": "audit-handoff:owner/repo#807",
        "kind": "audit-handoff",
        "content_hash": feedback._completion_hash("bounded handoff"),
        "ref_class": "shadow",
    }
    result = compiler.shadow_invoke_skill_package(
        Path(package["package_path"]),
        task_ref="owner/repo#807:audit",
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
    assert result["accepted"] is True
    assert result["baseline_changed"] is False
    assert result["outcome_ref"]
    cap = capabilities.load(ledger, create=False)[package["manifest"]["capability_id"]]
    assert cap["status"] == "shadow"
    assert cap["activation_evidence"]["skill_content_hash"] == package["manifest"]["content_hash"]

    # Second register with the same package is a no-op; different hash raises.
    compiler._register_skill_shadow(package["manifest"], ledger)
    other = dict(package["manifest"])
    other["content_hash"] = "sha256:" + ("cd" * 32)
    with pytest.raises(ValueError, match="different skill package"):
        compiler._register_skill_shadow(other, ledger)

    rejected = compiler.shadow_invoke_skill_package(
        Path(package["package_path"]),
        task_ref="owner/repo#807:rejected",
        influenced_run_ids=[target_run],
        artifact_refs=[],
        ledger_path=ledger,
        accepted=False,
    )
    assert rejected["accepted"] is False
    assert rejected["outcome_ref"] is None


def test_workflow_typed_helpers_and_dependency_path():
    typed, errors = compiler._typed_values("nope", {"a": "string"}, "inputs")
    assert typed == {} and "must be an object" in errors[0]

    typed, errors = compiler._typed_values(
        {"a": 1, "extra": "x"}, {"a": "string", "b": "integer"}, "inputs"
    )
    assert any("unsupported inputs" in e for e in errors)
    assert any("missing inputs" in e for e in errors)
    assert any("must be a string" in e for e in errors)

    typed, errors = compiler._typed_values(
        {"flag": "yes", "count": "three", "name": "  "},
        {"flag": "boolean", "count": "integer", "name": "string"},
        "inputs",
    )
    assert any("must be a boolean" in e for e in errors)
    assert any("must be an integer" in e for e in errors)
    assert any("non-empty" in e for e in errors)

    conds, errors = compiler._conditions("nope", "pre")
    assert conds == [] and "must be a list" in errors[0]
    conds, errors = compiler._conditions(
        [
            {"check": "kill_switch_off", "expected": True},
            {"check": "judge_this", "expected": True},
            {"check": "unknown_check", "expected": True},
            {"check": "not_expired", "expected": {"nested": 1}},
            "bad",
        ],
        "pre",
    )
    assert len(conds) == 1
    assert any("ambiguous judgment" in e for e in errors)
    assert any("unsupported condition" in e for e in errors)
    assert any("must be a scalar" in e for e in errors)

    by_id = {
        "a": {"depends_on": ["b"]},
        "b": {"depends_on": ["c"]},
        "c": {"depends_on": []},
        "loop": {"depends_on": ["loop"]},
    }
    assert compiler._has_dependency_path(by_id, "a", "c") is True
    assert compiler._has_dependency_path(by_id, "c", "a") is False
    assert compiler._has_dependency_path(by_id, "missing", "a") is False
    assert compiler._has_dependency_path(by_id, "loop", "c") is False


def test_reference_role_candidate_and_main_cli(tmp_path, monkeypatch, capsys):
    role = compiler.reference_role_candidate(now=1_700_000_000)
    assert role.kind_proposal == "role"
    assert role.capability_id.endswith("evidence-gap-prioritizer-reference")
    assert len(role.source_occurrences) == 3

    with pytest.raises(SystemExit) as exc:
        compiler.main([])
    assert exc.value.code == 2
    with pytest.raises(SystemExit) as exc:
        compiler.main(["reference"])
    assert exc.value.code == 2

    ledger = tmp_path / "ledger.json"
    # Stub the workflow to test argument parsing and JSON output hermetically.
    monkeypatch.setattr(
        compiler,
        "run_reference_workflow",
        lambda ledger_path: {"ok": True, "ledger": str(ledger_path)},
    )
    assert compiler.main(["reference", "--ledger", str(ledger)]) == 0
    assert json.loads(capsys.readouterr().out) == {"ok": True, "ledger": str(ledger)}
