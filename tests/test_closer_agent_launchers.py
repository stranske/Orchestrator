"""Focused tests for concrete closer agent CLI bindings."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import closer_agent_launchers as cal
import closer_campaign as campaign
import closer_distribution_package as package
import execution_profiles

MODULE_DIR = Path(campaign.__file__).resolve().parent


def _trusted_gates():
    return {
        "qualify_fn": lambda _m, _r: [],
        "policy_fn": lambda _p: None,
        "native_probe": lambda _d: None,
    }


def host_runtime_fixture(tmp_path: Path):
    core_digest = "a" * 64
    core_root = tmp_path / "core"
    core_root.mkdir()
    core_descriptor = {
        "schema_version": 1,
        "package_kind": "closer-core",
        "owner": "Workflows",
        "contract": campaign.CONTRACT,
        "core_version": "1",
        "package_digest": core_digest,
        "entrypoints": {"runtime": "scripts/closer_runtime.py"},
    }
    (core_root / "closer-core-package.json").write_text(json.dumps(core_descriptor))
    supported_core = {
        "owner": "Workflows",
        "contract": campaign.CONTRACT,
        "core_version": "1",
        "package_digest": core_digest,
    }
    public = package.build_public_runtime_descriptor(
        MODULE_DIR,
        candidate_sha="c" * 40,
        state_versions={"campaign": 1, "release": 1},
        supported_core=supported_core,
    )
    install_root = tmp_path / "runtime"
    package.materialize_runtime_tree(MODULE_DIR, install_root, public)
    campaign_root = tmp_path / "campaigns"
    campaign_root.mkdir()
    host = package.host_binding_from_public(
        public,
        install_root=install_root,
        campaign_root=campaign_root,
        python=sys.executable,
        core_binding={**supported_core, "package_root": str(core_root)},
    )
    manifest_path = install_root / "host-manifest.json"
    package.write_host_manifest(manifest_path, host)
    return host, manifest_path, campaign_root, install_root


@pytest.mark.parametrize("agent", cal.APPROVED_AGENTS)
def test_each_approved_agent_maps_to_cli_offload(agent):
    binding = cal.binding_for(agent)
    assert binding is not None
    assert binding.cli_mechanism == cal.MECHANISM_CLI_OFFLOAD
    assert binding.registered_profile_ids
    assert binding.desktop_scheduler_supported is False
    assert binding.desktop_native_supported is False
    assert binding.desktop_native_installed is False


def test_aider_is_refused():
    assert cal.refuse_agent("aider") is not None
    assert cal.binding_for("aider") is None


def test_unknown_agent_refused():
    assert cal.refuse_agent("not-a-seat") is not None


def test_profile_agent_mismatch_fails_closed(tmp_path):
    _, path, root, _ = host_runtime_fixture(tmp_path)
    plan, problems = cal.plan_cli_run(
        "codex",
        "claude-sonnet-5-5-high",
        "hello",
        manifest_path=path,
        root=root,
    )
    assert plan is None
    assert problems


def test_retired_profile_refused():
    problems = cal.validate_profile("gemini", "gemini-3.7-flash-high")
    assert any("retired" in p for p in problems)


def test_missing_manifest_blocks_before_offload(tmp_path):
    profile_id = execution_profiles.default_codex_profile("offload", "mid")
    called = False

    def _fake_offload(*_a, **_k):
        nonlocal called
        called = True
        return {"exit": 0}

    out = cal.run_cli(
        "codex",
        profile_id,
        "x",
        manifest_path=tmp_path / "missing.manifest.json",
        root=tmp_path,
        offload_fn=_fake_offload,
    )
    assert out["ran"] is False
    assert out["provider_started"] is False
    assert out["prelaunch_refused"] is True
    assert out["outcome"] == "denied"
    assert not called


def test_manifest_tamper_blocks_before_offload(tmp_path):
    host, path, root, install = host_runtime_fixture(tmp_path)
    bad = json.loads(json.dumps(host))
    bad["command_digest"] = "0" * 64
    called = False

    def _fake_offload(*_a, **_k):
        nonlocal called
        called = True
        return {"exit": 0}

    out = cal.run_cli(
        "codex",
        execution_profiles.default_codex_profile("offload", "mid"),
        "x",
        manifest=bad,
        manifest_path=path,
        root=root,
        own_launcher_path=install / "closer_launcher.py",
        offload_fn=_fake_offload,
        **_trusted_gates(),
    )
    assert out["ran"] is False
    assert not called


def test_pin_mismatch_on_launcher_argv(tmp_path):
    host, path, root, _ = host_runtime_fixture(tmp_path)
    foreign = [sys.executable, str(tmp_path / "missing.py"), "run"]
    problems = cal.verify_runtime_manifest(
        host,
        manifest_path=path,
        root=root,
        launcher_argv=foreign,
        own_launcher_path=host["launcher"]["path"],
    )
    assert problems


def test_valid_host_command_passes_pin_checks(tmp_path):
    host, path, root, install = host_runtime_fixture(tmp_path)
    argv = cal.expected_host_launcher_command(host, manifest_path=path, root=root)
    problems = cal.verify_runtime_manifest(
        host,
        manifest_path=path,
        root=root,
        launcher_argv=argv,
        own_launcher_path=install / "closer_launcher.py",
    )
    assert problems == []


def test_run_cli_preserves_profile_bound_prompt_and_stub(tmp_path):
    host, path, root, install = host_runtime_fixture(tmp_path)
    profile_id = execution_profiles.default_codex_profile("offload", "mid")
    seen: dict[str, object] = {}

    def stub(agent, prompt, **kwargs):
        seen.update(kwargs)
        seen["prompt"] = prompt
        seen["agent"] = agent
        return {
            "agent": agent,
            "exit": 0,
            "output": "ok",
            "run_id": "offload:codex:1",
            "requested_model": "m",
            "provider_completed": True,
        }

    out = cal.run_cli(
        "codex",
        profile_id,
        "summarize",
        manifest_path=path,
        root=root,
        cwd=tmp_path,
        timeout=30,
        manifest=host,
        own_launcher_path=install / "closer_launcher.py",
        offload_fn=stub,
        **_trusted_gates(),
    )
    assert out["provider_started"] is True
    assert out["outcome"] == "ok"
    assert out["profile_id"] == profile_id
    assert seen["profile_id"] == profile_id
    assert seen["task_type"] == "closer_interpret"
    assert "CLOSER RUNTIME BINDING" in str(seen["prompt"])
    assert str(path) in str(seen["prompt"])
    assert out["offload"].get("requested_model") == "m"


def test_exit_zero_uncertain_not_ok(tmp_path):
    host, path, root, install = host_runtime_fixture(tmp_path)
    profile_id = execution_profiles.default_codex_profile("offload", "mid")

    def stub(*_a, **_k):
        return {"exit": 0, "outcome_uncertain": True, "retry_safe": False}

    out = cal.run_cli(
        "codex",
        profile_id,
        "x",
        manifest_path=path,
        root=root,
        manifest=host,
        own_launcher_path=install / "closer_launcher.py",
        offload_fn=stub,
        **_trusted_gates(),
    )
    assert out["outcome"] == "unknown"


def test_denied_offload_classified(tmp_path):
    host, path, root, install = host_runtime_fixture(tmp_path)

    def stub(*_a, **_k):
        return {"exit": 2, "disabled": True, "error": "refused"}

    out = cal.run_cli(
        "codex",
        execution_profiles.default_codex_profile("offload", "mid"),
        "x",
        manifest_path=path,
        root=root,
        manifest=host,
        own_launcher_path=install / "closer_launcher.py",
        offload_fn=stub,
        **_trusted_gates(),
    )
    assert out["outcome"] == "denied"
    assert out["provider_started"] is False


def test_vibe_native_handoff_unsupported():
    binding = cal.binding_for("vibe")
    assert binding is not None
    assert binding.native_handoff_actor is None
    assert binding.native_handoff_actor_known is False
    assert binding.desktop_native_supported is False
    assert any("allowlist" in n for n in binding.notes)


def test_argv_preview_uses_explicit_profile(tmp_path):
    profile_id = "cursor-composer-2.5"
    host, path, root, install = host_runtime_fixture(tmp_path)
    plan, problems = cal.plan_cli_run(
        "cursor",
        profile_id,
        "hi",
        cwd=".",
        manifest_path=path,
        root=root,
        manifest=host,
        own_launcher_path=install / "closer_launcher.py",
        **_trusted_gates(),
    )
    assert not problems
    assert plan is not None
    assert plan.profile_id == profile_id
    assert "--model" in plan.argv_preview


def test_inventory_lists_profiles_without_default():
    inv = cal.inventory()
    for row in inv["bindings"]:
        assert "default_profile_id" not in row
        assert row["registered_profile_ids"]


def test_readiness_cli_distinct_from_desktop_qualified(monkeypatch):
    monkeypatch.setattr(cal.shutil, "which", lambda _binary: "/fixture/codex")
    ready = cal.readiness(
        "codex",
        execution_profiles.default_codex_profile("offload", "mid"),
        runner=lambda *_a, **_k: type("P", (), {"returncode": 0, "stdout": "", "stderr": ""})(),
    )
    assert ready.cli_installed is True
    assert ready.desktop_native_qualified is False


def test_materialized_launcher_agent_cli_inventory_subprocess(tmp_path):
    _host, path, _root, install = host_runtime_fixture(tmp_path)
    proc = subprocess.run(
        [
            sys.executable,
            str(install / "closer_launcher.py"),
            "agent-cli",
            "--manifest",
            str(path),
            "inventory",
        ],
        capture_output=True,
        text=True,
        cwd=str(install),
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert len(payload["bindings"]) == len(cal.APPROVED_AGENTS)


def test_cli_probe_uses_injected_runner(monkeypatch):
    monkeypatch.setattr(cal.shutil, "which", lambda _binary: "/fixture/codex")
    class Proc:
        returncode = 0
        stdout = stderr = ""

    def runner(cmd, **kwargs):
        assert cmd[1] == "--version"
        return Proc()

    ready = cal.readiness(
        "codex", execution_profiles.default_codex_profile("offload", "mid"), runner=runner
    )
    assert ready.cli_installed is True


def test_module_selftest():
    assert cal._selftest() == 0


PROFILE = "codex-6.1-sol-medium"


def test_started_exit_two_never_claims_prelaunch_refusal(tmp_path):
    manifest, path, root, install = host_runtime_fixture(tmp_path)
    result = cal.run_cli(
        "codex",
        PROFILE,
        "bounded review",
        manifest_path=path,
        root=root,
        manifest=manifest,
        own_launcher_path=install / "closer_launcher.py",
        offload_fn=lambda *a, **kw: {
            "exit": 2,
            "raw_exit": 2,
            "run_id": "actual-native-run",
            "output": "provider failed after start",
        },
        **_trusted_gates(),
    )
    assert result["provider_started"] is not False, result
    assert "before provider start" not in result["detail"], result


def test_unknown_retry_safety_nonzero_is_never_unavailable():
    assert (
        cal.classify_offload_result(
            {"exit": 70, "run_id": "actual-native-run", "retry_safe": False}
        )[0]
        == "unknown"
    )


def test_exception_after_submission_never_claims_not_started(tmp_path):
    manifest, path, root, install = host_runtime_fixture(tmp_path)

    def fail(*a, **kw):
        raise OSError("receipt persistence failed after provider submission")

    result = cal.run_cli(
        "codex",
        PROFILE,
        "bounded review",
        manifest_path=path,
        root=root,
        manifest=manifest,
        own_launcher_path=install / "closer_launcher.py",
        offload_fn=fail,
        **_trusted_gates(),
    )
    assert result["outcome"] == "unknown"
    assert result["provider_started"] is not False, result


def test_cli_readiness_does_not_require_nonexistent_manifest_argument(monkeypatch, capsys):
    monkeypatch.setattr(
        cal,
        "readiness",
        lambda *a, **kw: cal.Readiness("codex", PROFILE, True, True, None, True, False, []),
    )
    assert cal.main(["readiness", "--agent", "codex", "--profile", PROFILE]) == 0


def test_policy_change_after_preparation_is_checked_before_dispatch(tmp_path, monkeypatch):
    manifest, path, root, install = host_runtime_fixture(tmp_path)
    policy_calls = []
    called = []

    def policy(p):
        policy_calls.append(p.copy())
        return "worker paused" if p.get("paused") else None

    original = cal.adapters.build_command

    def build(*a, **kw):
        argv = original(*a, **kw)
        changed = json.loads(path.read_text())
        changed["policy"] = {"paused": True}
        path.write_text(json.dumps(changed))
        return argv

    monkeypatch.setattr(cal.adapters, "build_command", build)
    result = cal.run_cli(
        "codex",
        PROFILE,
        "bounded review",
        manifest_path=path,
        root=root,
        manifest=manifest,
        own_launcher_path=install / "closer_launcher.py",
        policy_fn=policy,
        qualify_fn=lambda *a: [],
        native_probe=lambda *a: None,
        offload_fn=lambda *a, **kw: called.append(True)
        or {"exit": 0, "run_id": "mock-run", "output": "mock"},
    )
    assert not called, (result, policy_calls)


def test_malformed_argv_fails_closed_instead_of_indexerror(tmp_path):
    manifest, path, root, install = host_runtime_fixture(tmp_path)
    problems = cal.validate_launcher_argv(
        ["python", "--manifest"], manifest, manifest_path=path, root=root
    )
    assert problems


def test_foreign_manifest_does_not_qualify_current_execution_package(tmp_path):
    import closer_launcher

    manifest, path, root, install = host_runtime_fixture(tmp_path)
    assert Path(closer_launcher.__file__).resolve() != Path(manifest["launcher"]["path"]).resolve()
    # The actual executing launcher is A; the supplied validated manifest pins B.
    # Default verification must not override A's own identity with B's self-declaration.
    problems = cal.verify_runtime_manifest(manifest, manifest_path=path, root=root)
    assert problems, {
        "executing_launcher": closer_launcher.__file__,
        "manifest_launcher": manifest["launcher"]["path"],
        "problems": problems,
    }


@pytest.mark.parametrize("timeout", [None, 0, -1, True, 901, float("inf")])
def test_unbounded_timeout_never_dispatches(timeout):
    calls = []
    result = cal.run_cli(
        "codex",
        PROFILE,
        "x",
        manifest_path="missing",
        root=".",
        timeout=timeout,
        offload_fn=lambda *a, **k: calls.append(True),
    )
    assert result["prelaunch_refused"] is True
    assert not calls


def test_packaged_helper_tamper_refused_before_import(tmp_path):
    _, path, _, install = host_runtime_fixture(tmp_path)
    marker = tmp_path / "unverified-import"
    (install / "closer_agent_launchers.py").write_text(
        "from pathlib import Path\nPath(" + repr(str(marker)) + ").touch()\n"
    )
    proc = subprocess.run(
        [
            sys.executable,
            str(install / "closer_launcher.py"),
            "agent-cli",
            "--manifest",
            str(path),
            "inventory",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 78, proc.stderr
    assert not marker.exists()


def test_shared_cli_rejects_different_inner_manifest(tmp_path):
    _, path, root, install = host_runtime_fixture(tmp_path)
    foreign = tmp_path / "foreign.json"
    foreign.write_bytes(path.read_bytes())
    proc = subprocess.run(
        [
            sys.executable,
            str(install / "closer_launcher.py"),
            "agent-cli",
            "--manifest",
            str(path),
            "plan",
            "--manifest",
            str(foreign),
            "--root",
            str(root),
            "--agent",
            "codex",
            "--profile",
            PROFILE,
            "--prompt",
            "bounded",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 78, proc.stderr
    assert "inner manifest differs" in proc.stdout


def test_real_policy_gate_blocks_unregistered_reader(tmp_path):
    host, path, root, install = host_runtime_fixture(tmp_path)
    calls = []
    result = cal.run_cli(
        "codex",
        PROFILE,
        "x",
        manifest_path=path,
        root=root,
        own_launcher_path=install / "closer_launcher.py",
        offload_fn=lambda *a, **kw: calls.append(True),
    )
    assert result["prelaunch_refused"] is True
    assert not calls
    assert "policy" in result["detail"]


def test_real_qualification_gate_cannot_be_skipped(tmp_path):
    host, path, root, install = host_runtime_fixture(tmp_path)
    calls = []
    result = cal.run_cli(
        "codex",
        PROFILE,
        "x",
        manifest_path=path,
        root=root,
        own_launcher_path=install / "closer_launcher.py",
        policy_fn=lambda p: None,
        offload_fn=lambda *a, **kw: calls.append(True),
    )
    assert result["prelaunch_refused"] is True
    assert "not qualified" in result["detail"]
    assert not calls


def test_full_native_provenance_is_retained(tmp_path):
    host, path, root, install = host_runtime_fixture(tmp_path)
    native = {
        "exit": 0,
        "provider_completed": True,
        "output_ref": "native-ref",
        "log": "native-log",
        "pool": "original-pool",
        "future_field": {"a": 1},
    }
    result = cal.run_cli(
        "codex",
        PROFILE,
        "x",
        manifest_path=path,
        root=root,
        own_launcher_path=install / "closer_launcher.py",
        offload_fn=lambda *a, **kw: native,
        **_trusted_gates(),
    )
    assert result["offload"] == native
    assert result["queue_action_accepted"] is False
    assert result["desktop_qualified"] is False
