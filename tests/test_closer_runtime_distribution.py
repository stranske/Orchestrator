"""Public Runtime packages remain pinned and preserve an incumbent installation."""

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
