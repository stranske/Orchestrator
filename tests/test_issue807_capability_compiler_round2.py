"""Hermetic coverage for remaining issue-807 capability_compiler edge arms.

Round 1 covered the skill happy path and helpers that skip on bare CI runners.
This round targets the rejection / validation arms that still show as Miss/BrPart
under the focused combined measure: resource safety, skill-source validation,
package integrity, role/playbook schema guards, and workflow dry-run consumers.
"""

from __future__ import annotations

import copy
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
        "capability_id": "capability:issue807-round2",
        "source_ir_ref": "capability-ir:issue807-round2",
        "source_fingerprint": stable_hash("issue807-round2", "v1"),
        "procedure_class": "reusable_procedure",
        "reuse_scope": "shared",
        "name": "issue807-round2",
        "description": "Bounded audit handoff procedure for round-two coverage.",
        "triggers": ["prepare evidence for an audit or workflow handoff"],
        "instructions": [
            "Run the bundled workspace audit script in report-only mode.",
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
        "safety_boundaries": ["Keep the bundled audit script in report-only mode."],
        "expected_artifacts": ["workspace-audit-report"],
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


def test_safe_resource_rejects_kind_target_suffix_size_and_hash(tmp_path, monkeypatch):
    none, errors = compiler._safe_resource(
        {
            "kind": "binaries",
            "source_path": str(_script(tmp_path)),
            "target": "binaries/helper.bin",
            "content_hash": "sha256:" + ("00" * 32),
        },
        0,
    )
    assert none is None
    assert any("kind is unsupported" in e for e in errors)

    none, errors = compiler._safe_resource(
        {
            "kind": "scripts",
            "source_path": str(_script(tmp_path)),
            "target": "scripts/token=supersecretvalue",
            "content_hash": "sha256:" + ("00" * 32),
        },
        1,
    )
    assert none is None
    assert any("secret-bearing resource target" in e for e in errors)

    bad_suffix = _script(tmp_path, "notes.txt", "plain text\n")
    none, errors = compiler._safe_resource(
        {
            "kind": "scripts",
            "source_path": str(bad_suffix),
            "target": "scripts/notes.txt",
            "content_hash": compiler._sha256_file(bad_suffix),
        },
        2,
    )
    assert none is None
    assert any("unsupported resource type" in e for e in errors)

    huge = _script(tmp_path, "huge.sh", "#!/bin/bash\n")
    monkeypatch.setattr(compiler, "MAX_SKILL_RESOURCE_BYTES", 4)
    none, errors = compiler._safe_resource(
        {
            "kind": "scripts",
            "source_path": str(huge),
            "target": "scripts/huge.sh",
            "content_hash": compiler._sha256_file(huge),
        },
        3,
    )
    assert none is None
    assert any("exceeds size limit" in e for e in errors)
    monkeypatch.setattr(compiler, "MAX_SKILL_RESOURCE_BYTES", 1024 * 1024)

    good = _script(tmp_path, "ok.sh")
    none, errors = compiler._safe_resource(
        {
            "kind": "scripts",
            "source_path": str(good),
            "target": "scripts/ok.sh",
            "content_hash": "sha256:" + ("ab" * 32),
        },
        4,
    )
    assert none is None
    assert any("content hash mismatch" in e for e in errors)


def test_compile_skill_rejects_schema_identity_and_command_edges(tmp_path):
    with pytest.raises(compiler.SkillCompileError) as exc:
        compiler.compile_skill_package(
            _skill_source(tmp_path, schema="wrong", version=99),
            output_root=tmp_path / "out",
        )
    assert any("unsupported skill source schema" in r for r in exc.value.reasons)

    with pytest.raises(compiler.SkillCompileError) as exc:
        compiler.compile_skill_package(
            _skill_source(
                tmp_path,
                capability_id="not-a-capability",
                source_ir_ref="missing-prefix",
                source_fingerprint="not-a-hash",
            ),
            output_root=tmp_path / "out2",
        )
    reasons = exc.value.reasons
    assert any("invalid capability_id" in r for r in reasons)
    assert any("capability IR reference" in r for r in reasons)
    assert any("invalid source fingerprint" in r for r in reasons)

    with pytest.raises(compiler.SkillCompileError) as exc:
        compiler.compile_skill_package(
            _skill_source(tmp_path, name="Bad_Name", description="<script>nope</script>"),
            output_root=tmp_path / "out3",
        )
    assert any("invalid skill name" in r for r in exc.value.reasons)
    assert any("invalid skill description" in r for r in exc.value.reasons)

    with pytest.raises(compiler.SkillCompileError) as exc:
        compiler.compile_skill_package(
            _skill_source(
                tmp_path,
                validation_command="curl https://example.com",
                expires_at=-1,
                rollback={"action": "delete_everything", "reason": "nope"},
            ),
            output_root=tmp_path / "out4",
        )
    reasons = exc.value.reasons
    assert any("validation command is not allowlisted" in r for r in reasons)
    assert any("expires_at must be a positive integer" in r for r in reasons)
    assert any("unsupported skill rollback action" in r for r in reasons)

    with pytest.raises(compiler.SkillCompileError) as exc:
        compiler.compile_skill_package(
            _skill_source(tmp_path, resources=[], rollback="not-a-dict"),
            output_root=tmp_path / "out5",
        )
    reasons = exc.value.reasons
    assert any("skill requires at least one reused resource" in r for r in reasons)
    assert any("skill rollback must contain action and reason" in r for r in reasons)


def test_compile_skill_rejects_duplicate_targets_and_unbundled_validation(tmp_path):
    script = _script(tmp_path)
    other = _script(tmp_path, "other.sh")
    digest = compiler._sha256_file(script)
    source = _skill_source(
        tmp_path,
        resources=[
            {
                "kind": "scripts",
                "source_path": str(script),
                "target": "scripts/helper.sh",
                "content_hash": digest,
            },
            {
                "kind": "scripts",
                "source_path": str(other),
                "target": "scripts/helper.sh",
                "content_hash": compiler._sha256_file(other),
            },
        ],
        validation_command="bash scripts/missing.sh --help",
    )
    with pytest.raises(compiler.SkillCompileError) as exc:
        compiler.compile_skill_package(source, output_root=tmp_path / "dup-target")
    reasons = exc.value.reasons
    assert any("duplicate skill resource target" in r for r in reasons)
    assert any("validation command does not reference a bundled resource" in r for r in reasons)


def test_compile_skill_rejects_secret_bearing_source_and_long_description(tmp_path):
    with pytest.raises(compiler.SkillCompileError) as exc:
        compiler.compile_skill_package(
            _skill_source(tmp_path, description="token=supersecretvalue"),
            output_root=tmp_path / "secret",
        )
    assert any("secret-bearing skill source" in r for r in exc.value.reasons)

    long_trigger = "x" * 200
    with pytest.raises(compiler.SkillCompileError) as exc:
        compiler.compile_skill_package(
            _skill_source(
                tmp_path,
                description="A" * 600,
                triggers=[long_trigger, long_trigger, long_trigger],
            ),
            output_root=tmp_path / "long-desc",
        )
    assert any(
        "rendered skill description exceeds length limit" in r or "length limit" in r
        for r in exc.value.reasons
    )


def test_validate_skill_package_integrity_edges(tmp_path):
    package = compiler.compile_skill_package(_skill_source(tmp_path), output_root=tmp_path / "pkg")
    skill_dir = Path(package["package_path"])

    skill_md = skill_dir / "SKILL.md"
    original = skill_md.read_text(encoding="utf-8")
    skill_md.unlink()
    with pytest.raises(AssertionError, match="SKILL.md does not exist"):
        compiler.validate_skill_package(skill_dir)
    skill_md.write_text(original, encoding="utf-8")

    skill_md.write_text(
        "---\nname: issue807-round2\ndescription: x\nextra: y\n---\n\n# Body\n",
        encoding="utf-8",
    )
    with pytest.raises(AssertionError, match="frontmatter must contain only"):
        compiler.validate_skill_package(skill_dir)

    skill_md.write_text(
        '---\nname: wrong-name\ndescription: "x"\n---\n\n# Body\n',
        encoding="utf-8",
    )
    with pytest.raises(AssertionError, match="skill name does not match"):
        compiler.validate_skill_package(skill_dir)

    # Restore a valid package and break resource linkage / package hash / secrets.
    clean = compiler.compile_skill_package(
        _skill_source(tmp_path, name="issue807-integrity", capability_id="capability:issue807-int"),
        output_root=tmp_path / "integrity",
    )
    clean_dir = Path(clean["package_path"])
    (clean_dir / "scripts" / "helper.sh").unlink()
    with pytest.raises(AssertionError, match="skill resource does not exist"):
        compiler.validate_skill_package(clean_dir)

    clean2 = compiler.compile_skill_package(
        _skill_source(tmp_path, name="issue807-ref", capability_id="capability:issue807-ref"),
        output_root=tmp_path / "ref",
    )
    clean2_dir = Path(clean2["package_path"])
    md = clean2_dir / "SKILL.md"
    text = md.read_text(encoding="utf-8")
    md.write_text(text.replace("(scripts/helper.sh)", "(scripts/gone.sh)"), encoding="utf-8")
    with pytest.raises(AssertionError, match="^skill resource is not referenced by SKILL.md$"):
        compiler.validate_skill_package(clean2_dir)

    clean3 = compiler.compile_skill_package(
        _skill_source(tmp_path, name="issue807-secret", capability_id="capability:issue807-sec"),
        output_root=tmp_path / "sec",
    )
    clean3_dir = Path(clean3["package_path"])
    md = clean3_dir / "SKILL.md"
    md.write_text(md.read_text(encoding="utf-8") + "\ntoken=supersecretvalue\n", encoding="utf-8")
    manifest_path = clean3_dir / ".orchestrator" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["content_hash"] = compiler.stable_hash(
        "skill-package-content", compiler._package_file_hashes(clean3_dir)
    )
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(AssertionError, match="^secret detected in generated skill$"):
        compiler.validate_skill_package(clean3_dir)


def test_reference_skill_source_uses_home_skill_resource(tmp_path, monkeypatch):
    skill_script = (
        tmp_path
        / "home"
        / ".codex"
        / "skills"
        / "code-workspace-hygiene"
        / "scripts"
        / "audit_code_root.sh"
    )
    skill_script.parent.mkdir(parents=True)
    skill_script.write_text("#!/bin/bash\necho audit\n", encoding="utf-8")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    source = compiler.reference_skill_source()
    assert source["name"] == "audit-handoff-evidence"
    assert Path(source["resources"][0]["source_path"]) == skill_script
    assert source["resources"][0]["content_hash"] == compiler._sha256_file(skill_script)


def test_role_schema_and_identity_guards():
    empty, errors = compiler._validate_role_schema({}, label="input schema")
    assert empty == {} and "lacks input schema" in errors[0]

    schema, errors = compiler._validate_role_schema(
        {
            "Bad-Name": {"type": "string", "required": True},
            "ok": "not-object",
            "flag": {"type": "string", "required": True, "extra": 1},
            "mode": {"type": "widget", "required": True},
            "optional": {"type": "string", "required": False},
            "choice": {"type": "string", "required": True, "enum": []},
            "lenny": {"type": "string", "required": True, "max_length": 0},
            "items": {"type": "string_list", "required": True, "max_items": 99},
        },
        label="input schema",
    )
    joined = " ".join(errors)
    assert "invalid input schema field" in joined
    assert "must be an object" in joined
    assert "unsupported fields" in joined
    assert "unsupported type" in joined
    assert "must be required" in joined
    assert "invalid enum" in joined
    assert "invalid max_length" in joined
    assert "invalid max_items" in joined
    # Invalid fields are still normalized with the declared type; errors carry the rejection.
    assert "Bad-Name" not in schema
    assert schema["flag"]["type"] == "string"

    good, errors = compiler._validate_role_schema(
        {
            "decision": {
                "type": "string",
                "required": True,
                "enum": ["inspect", "defer"],
                "max_length": 32,
            },
            "refs": {"type": "string_list", "required": True, "max_items": 4},
        },
        label="output schema",
    )
    assert errors == []
    assert good["decision"]["enum"] == ["inspect", "defer"]
    assert good["refs"]["max_items"] == 4

    identity_errors = compiler._forbidden_role_identity(
        {"nested": [{"provider": "openai"}, {"safe": True}], "model_id": "x"}
    )
    assert any("execution identity" in e for e in identity_errors)

    with pytest.raises(compiler.RoleCompileError, match="typed role contract"):
        compiler._role_contract(
            type("C", (), {"graph": {}})()  # minimal stand-in without role_contract
        )


def test_validate_generated_role_manifest_assertion_arms():
    with pytest.raises(AssertionError, match="invalid generated role manifest"):
        compiler._validate_generated_role_manifest("nope")  # type: ignore[arg-type]
    with pytest.raises(AssertionError, match="lacks output schema"):
        compiler._validate_generated_role_manifest({"schema": "x"})

    now = 1_800_000_000
    compiled = compiler.compile_role_capability(compiler.reference_role_candidate(now=now), now=now)
    manifest = copy.deepcopy(compiled["manifest"])
    manifest["shadow_only"] = False
    with pytest.raises(AssertionError, match="not shadow-only"):
        compiler._validate_generated_role_manifest(manifest)

    manifest = copy.deepcopy(compiled["manifest"])
    manifest["version"] = 99
    with pytest.raises(AssertionError, match="unsupported generated role manifest"):
        compiler._validate_generated_role_manifest(manifest)

    manifest = copy.deepcopy(compiled["manifest"])
    manifest["prompt_hash"] = "sha256:" + ("00" * 32)
    with pytest.raises(AssertionError, match="prompt hash mismatch"):
        compiler._validate_generated_role_manifest(manifest)

    manifest = copy.deepcopy(compiled["manifest"])
    manifest["manifest_id"] = "sha256:" + ("11" * 32)
    with pytest.raises(AssertionError, match="manifest hash mismatch"):
        compiler._validate_generated_role_manifest(manifest)


def test_playbook_manifest_and_contract_guards(tmp_path):
    with pytest.raises(compiler.PlaybookCompileError, match="typed playbook contract"):
        compiler._playbook_contract(type("C", (), {"graph": {}})())

    with pytest.raises(AssertionError, match="invalid playbook manifest shape"):
        compiler.validate_playbook_manifest({"schema": "x"})

    # Build a minimal shape that fails successive guards.
    bad = {
        "schema": "wrong",
        "version": 1,
        "manifest_id": "x",
        "rule_id": "r",
        "capability_id": "capability:x",
        "source_ir_ref": "capability-ir:x",
        "source_fingerprint": "sha256:" + ("ab" * 32),
        "repo": "owner/repo",
        "section": "validation",
        "text": "text",
        "content_hash": "sha256:" + ("cd" * 32),
        "selector": {},
        "current_refs": [],
        "negative_examples": [],
        "evidence_refs": [],
        "risk_level": "low_reversible",
        "optional_workflows_bundle": False,
        "lifecycle": {"state": "canary", "predecessor": "p"},
        "generated_at": 1,
    }
    with pytest.raises(AssertionError, match="unsupported playbook manifest"):
        compiler.validate_playbook_manifest(bad)

    bad["schema"] = compiler.PLAYBOOK_MANIFEST_SCHEMA
    bad["section"] = "not-a-section"
    with pytest.raises(AssertionError, match="invalid playbook manifest section"):
        compiler.validate_playbook_manifest(bad)

    bad["section"] = "validation"
    bad["risk_level"] = "high"
    with pytest.raises(AssertionError, match="not low risk"):
        compiler.validate_playbook_manifest(bad)

    bad["risk_level"] = "low_reversible"
    bad["lifecycle"] = {"state": "shadow"}
    with pytest.raises(AssertionError, match="invalid playbook canary lifecycle"):
        compiler.validate_playbook_manifest(bad)

    bad["lifecycle"] = {"state": "canary", "predecessor": "p"}
    with pytest.raises(AssertionError, match="content hash mismatch"):
        compiler.validate_playbook_manifest(bad)


def test_workflow_dry_run_consume_and_compile_rejection_edges():
    with pytest.raises(ValueError, match="unsupported workflow rail plan"):
        compiler.dry_run_workflow_rail({"schema": "nope", "version": 1})

    with pytest.raises(ValueError, match="invalid shadow workflow output"):
        compiler.consume_workflow_output(
            {"schema": "nope", "side_effects": [], "steps": [], "result_id": "r"}
        )

    plan = compiler.compile_workflow_rail(compiler.reference_workflow_source())
    result = compiler.dry_run_workflow_rail(plan)
    broken = copy.deepcopy(result)
    broken["steps"][0]["status"] = "failed"
    with pytest.raises(ValueError, match="incomplete shadow workflow output"):
        compiler.consume_workflow_output(broken)

    with pytest.raises(compiler.WorkflowCompileError, match="must be an object"):
        compiler.compile_workflow_rail("bad")  # type: ignore[arg-type]

    source = compiler.reference_workflow_source()
    source["extra_field"] = True
    source["capability_id"] = "bad"
    source["source_ir_ref"] = "bad"
    source["schema"] = "wrong"
    with pytest.raises(compiler.WorkflowCompileError) as exc:
        compiler.compile_workflow_rail(source)
    reasons = " ".join(exc.value.reasons)
    assert "unsupported fields" in reasons
    assert "unsupported workflow source schema" in reasons
    assert "invalid capability_id" in reasons

    source = compiler.reference_workflow_source()
    source["selector"] = {"field": "task_type", "operator": "equals", "value": 12}
    with pytest.raises(compiler.WorkflowCompileError) as exc:
        compiler.compile_workflow_rail(source)
    assert any("equals value must be a string" in r for r in exc.value.reasons)

    source = compiler.reference_workflow_source()
    source["selector"] = {"field": "task_type", "operator": "in", "value": "not-list"}
    with pytest.raises(compiler.WorkflowCompileError) as exc:
        compiler.compile_workflow_rail(source)
    assert any("in value must be a list" in r for r in exc.value.reasons)

    source = compiler.reference_workflow_source()
    source["steps"][0]["retry"] = {"max_attempts": 9, "backoff_seconds": 1}
    with pytest.raises(compiler.WorkflowCompileError) as exc:
        compiler.compile_workflow_rail(source)
    assert any("max_attempts must be between 1 and 5" in r for r in exc.value.reasons)

    source = compiler.reference_workflow_source()
    source["steps"][0]["timeout_seconds"] = 0
    with pytest.raises(compiler.WorkflowCompileError) as exc:
        compiler.compile_workflow_rail(source)
    assert any("timeout_seconds must be between 1 and 3600" in r for r in exc.value.reasons)

    source = compiler.reference_workflow_source()
    source["barriers"] = "nope"
    source["expires_at"] = -1
    source["kill_switch"] = ""
    with pytest.raises(compiler.WorkflowCompileError) as exc:
        compiler.compile_workflow_rail(source)
    reasons = " ".join(exc.value.reasons)
    assert "barriers must be a list" in reasons
    assert "expires_at must be a positive integer" in reasons
    assert "kill_switch is required" in reasons

    # Successful candidate path after a rejected one already covered; hit compiled_shadow return.
    ok = compiler.compile_workflow_candidate(compiler.reference_workflow_source())
    assert ok["status"] == "compiled_shadow"
    assert ok["executable"] is False
    assert ok["plan"]["plan_id"]


def test_shadow_invoke_records_outcome_without_success_when_rejected(tmp_path):
    package = compiler.compile_skill_package(
        _skill_source(tmp_path), output_root=tmp_path / "candidates"
    )
    ledger = tmp_path / "capabilities.json"
    run_id = "issue807-round2-run"
    feedback.record_run(run_id, "owner/repo#807", "review", "codex", pr_number=807)
    result = compiler.shadow_invoke_skill_package(
        Path(package["package_path"]),
        task_ref="owner/repo#807:rejected-with-outcome",
        influenced_run_ids=[run_id],
        artifact_refs=[],
        ledger_path=ledger,
        accepted=False,
        downstream_outcome={
            "verifier_verdict": "FAIL",
            "adjudicated_verdict": "FAIL",
            "merged": False,
            "durability": "broke_later",
        },
    )
    assert result["accepted"] is False
    assert result["outcome_ref"]
    # Rejected-with-outcome must not emit the success heartbeat path (branch 1739->1754).
    cap = capabilities.load(ledger, create=False)[package["manifest"]["capability_id"]]
    assert not any(event.get("type") == "success" for event in cap["event_history"])
