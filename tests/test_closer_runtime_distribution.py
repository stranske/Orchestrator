"""Public Runtime packages remain pinned and preserve an incumbent installation."""

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import closer_campaign as campaign
import closer_distribution_package as package
import closer_launcher as launcher


def descriptor():
    return package.build_public_runtime_descriptor(
        Path(campaign.__file__).parent,
        candidate_sha="b" * 40,
        state_versions={"campaign": 1, "release": 1},
        supported_core={
            "owner": "Workflows",
            "contract": campaign.CONTRACT,
            "core_version": "1",
            "package_digest": "a" * 64,
        },
    )


def test_existing_runtime_is_never_removed_by_materialization(tmp_path):
    incumbent = tmp_path / "active"
    incumbent.mkdir()
    sentinel = incumbent / "worker-generation"
    sentinel.write_text("still active")
    with pytest.raises(package.DistributionPackageError, match="destination exists"):
        package.materialize_runtime_tree(Path(campaign.__file__).parent, incumbent, descriptor())
    assert sentinel.read_text() == "still active"


def test_source_upgrade_boundary_is_pinned_in_public_runtime_descriptor():
    public = descriptor()
    assert "closer_native_source_upgrade.py" in public["modules"]


@pytest.mark.parametrize(
    "field,value", [("module", "../other.py"), ("sha256", "f" * 64), ("compatible_runtime", "99")]
)
def test_public_launcher_fields_cannot_redirect_execution(field, value):
    public = descriptor()
    public["launcher"][field] = value
    assert package.validate_public_runtime_descriptor(public)


def test_public_core_requires_an_independent_digest_pin():
    public = descriptor()
    public["supported_core"].pop("package_digest")
    assert (
        "supported_core package digest must be pinned"
        in package.validate_public_runtime_descriptor(public)
    )


def test_host_binding_command_parses_with_explicit_root_and_digest_binds_argv(
    tmp_path, monkeypatch, capsys
):
    public = descriptor()
    install_root = tmp_path / "runtime"
    package.materialize_runtime_tree(Path(campaign.__file__).parent, install_root, public)
    campaign_root = tmp_path / "state" / "actual-campaigns"
    host = package.host_binding_from_public(
        public, install_root=install_root, campaign_root=campaign_root, python=sys.executable
    )
    manifest_path = install_root / "host-manifest.json"
    package.write_host_manifest(manifest_path, host)

    assert host["command"][-2:] == ["--root", str(campaign_root.resolve())]
    assert host["command_digest"] == campaign.sha256_json(host["command"])
    assert host["command"][host["command"].index("--manifest") + 1] == str(manifest_path)

    invoked = {}
    fake_runtime = SimpleNamespace(
        tick=lambda root, manifest: invoked.update(root=root, manifest=manifest) or {"ok": True}
    )
    monkeypatch.setattr(launcher, "__file__", str(install_root / "closer_launcher.py"))
    monkeypatch.setattr(launcher, "load_runtime", lambda _manifest: fake_runtime)
    assert launcher.main(host["command"][2:]) == 0
    capsys.readouterr()
    assert invoked == {"root": str(campaign_root.resolve()), "manifest": str(manifest_path)}


def test_host_binding_defaults_to_actual_campaign_state_not_install_root(tmp_path, monkeypatch):
    public = descriptor()
    install_root = tmp_path / "installed-runtime"
    state_root = tmp_path / "state"
    monkeypatch.setenv("ORCH_STATE_DIR", str(state_root))
    monkeypatch.delenv("ORCH_CLOSER_CAMPAIGN_DIR", raising=False)

    host = package.host_binding_from_public(public, install_root=install_root)
    assert host["command"][-2:] == ["--root", str((state_root / "closer-campaigns").resolve())]

    configured = tmp_path / "configured-campaigns"
    monkeypatch.setenv("ORCH_CLOSER_CAMPAIGN_DIR", str(configured))
    configured_host = package.host_binding_from_public(public, install_root=install_root)
    assert configured_host["command"][-2:] == ["--root", str(configured.resolve())]


@pytest.mark.parametrize("extra", ["unowned.py", "__pycache__/claims.pyc"])
def test_launcher_refuses_unsealed_code_before_loading_runtime(tmp_path, extra):
    public = descriptor()
    root = tmp_path / "pkg"
    package.materialize_runtime_tree(Path(campaign.__file__).parent, root, public)
    manifest = package.host_binding_from_public(public, install_root=root)
    added = root / extra
    added.parent.mkdir(parents=True, exist_ok=True)
    added.write_bytes(b"unsealed code must never load")
    problems = launcher.pin_problems(manifest, own_path=str(root / "closer_launcher.py"))
    assert "Runtime tree contains undeclared executable code" in problems


def test_physical_dispatcher_entry_does_not_create_bytecode_in_sealed_runtime(tmp_path):
    """The native B child starts this exact physical entrypoint without ``-B``."""
    public = descriptor()
    runtime = tmp_path / "sealed-runtime"
    package.materialize_runtime_tree(Path(campaign.__file__).parent, runtime, public)
    core = tmp_path / "core"
    core.mkdir()
    core_descriptor = {
        "owner": "Workflows",
        "contract": campaign.CONTRACT,
        "core_version": "1",
        "package_digest": "a" * 64,
    }
    (core / "closer-core-package.json").write_text(json.dumps(core_descriptor))
    manifest = package.host_binding_from_public(
        public,
        install_root=runtime,
        core_binding={**core_descriptor, "package_root": str(core)},
        python=sys.executable,
    )
    manifest_path = runtime / "host-manifest.json"
    package.write_host_manifest(manifest_path, manifest)
    env = {**os.environ, "PYTHONPATH": ""}
    env.pop("PYTHONDONTWRITEBYTECODE", None)
    completed = subprocess.run(
        [sys.executable, str(runtime / "closer_dispatcher.py"), "--help"],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert not list(runtime.rglob("*.pyc"))
    assert campaign.load_manifest(manifest_path) == manifest


def test_actual_runtime_export_carries_source_bound_flat_selection_policy(tmp_path):
    import json
    import subprocess

    import closer_role_selection as selection

    public = descriptor()
    root = tmp_path / "exported-runtime"
    package.materialize_runtime_tree(Path(campaign.__file__).parent, root, public)
    policy = root / selection.POLICY_FILENAME
    assert policy.read_bytes() == selection.POLICY_PATH.read_bytes()
    assert not (root / "config").exists()
    assert all(not path.is_dir() and not path.is_symlink() for path in root.iterdir())
    assert "closer_role_selection.py" in public["modules"]
    code = (
        "import sys,json;sys.path.insert(0,sys.argv[1]);import closer_role_selection as s;"
        "print(json.dumps([str(s.POLICY_PATH),s.policy_sha256(),s.load_policy()['schema']]))"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", code, str(root)],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert json.loads(result.stdout) == [
        str(policy.resolve()),
        selection.EXPECTED_POLICY_SHA256,
        selection.SCHEMA,
    ]
    host = package.host_binding_from_public(public, install_root=root)
    assert not launcher.pin_problems(host, own_path=str(root / "closer_launcher.py"))


@pytest.mark.parametrize("damage", ["missing", "tamper", "symlink"])
def test_exported_policy_data_cannot_be_missing_changed_or_aliased(tmp_path, damage):
    import closer_role_selection as selection

    root = tmp_path / "exported-runtime"
    package.materialize_runtime_tree(Path(campaign.__file__).parent, root, descriptor())
    policy = root / selection.POLICY_FILENAME
    if damage == "missing":
        policy.unlink()
    elif damage == "tamper":
        policy.write_bytes(policy.read_bytes() + b"\n")
    else:
        raw = policy.read_bytes()
        policy.unlink()
        actual = tmp_path / "same-but-unowned.json"
        actual.write_bytes(raw)
        policy.symlink_to(actual)
    with pytest.raises((OSError, ValueError)):
        selection._policy_bytes(policy)
    # The real producer must refuse re-export before creating a destination.
    again = tmp_path / "second-runtime"
    with pytest.raises((OSError, ValueError)):
        package.materialize_runtime_tree(root, again, descriptor())
    assert not again.exists()
