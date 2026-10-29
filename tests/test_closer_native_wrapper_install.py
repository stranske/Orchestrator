"""Contract tests for the source-owned wrapper installer."""

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest
from test_closer_native_observer import host as host

import closer_campaign as campaign
import closer_native_observer as native
import closer_native_wrapper_install as install
import closer_retained_reconciliation as retained


def _old_wrapper():
    return b"""#!/bin/bash
LANE="${1:-closer}"
if [[ "$LANE" == "closer" ]]; then
  echo "original window expired" >&2
  exit 78
fi
# BEGIN CLOSER PINNED NATIVE ENTRY v1
old native entry
# END CLOSER PINNED NATIVE ENTRY v1
export PATH="$HOME/.local/bin:$HOME/.cursor/bin:$PATH"
case "$LANE" in
  closer) ;;
esac
"""


def test_guard_requires_exact_reviewed_predecessor(monkeypatch):
    raw = _old_wrapper()
    monkeypatch.setattr(install, "RESTORED_SHA", hashlib.sha256(raw).hexdigest())
    assert b"original window expired" not in install._guard_source(raw)
    with pytest.raises(install.InstallHeld):
        install._guard_source(raw.replace(b"expired", b"changed"))


def test_render_keeps_shebang_and_declares_lane_before_guard(monkeypatch, tmp_path):
    raw = _old_wrapper()
    monkeypatch.setattr(install, "RESTORED_SHA", hashlib.sha256(raw).hexdigest())
    manifest = {
        "mechanism_contract": {
            "campaign_root": str(tmp_path / "campaigns"),
            "interpreter": {"path": "/bin/echo"},
        },
        "launcher": {"path": "/bin/echo"},
        "policy": {
            "correction_controller_installation": {
                "config": {"state_path": str(tmp_path / "campaigns/new.campaign.json")}
            }
        },
    }
    rendered = install._render(
        install._guard_source(raw), manifest, tmp_path / "host-manifest.json"
    )
    assert rendered.startswith(b"#!/bin/bash\n")
    assert (
        rendered.index(b"LANE=")
        < rendered.index(b"CLOSER_NATIVE_QUALIFICATION_REQUEST")
        < rendered.index(b"# BEGIN CLOSER PINNED NATIVE ENTRY v1")
    )


def test_old_unknown_fixture_is_never_a_write_target(tmp_path):
    result = tmp_path / "old.results/worker.json"
    result.parent.mkdir()
    result.write_bytes(b'{"outcome":"unknown"}')
    before = result.read_bytes()
    (tmp_path / "new").mkdir()
    assert result.read_bytes() == before


def test_readmission_guard_exits_before_dummy_native_entry(monkeypatch, tmp_path):
    raw = _old_wrapper()
    monkeypatch.setattr(install, "RESTORED_SHA", hashlib.sha256(raw).hexdigest())
    home, wrapper = tmp_path / "home", tmp_path / "wrapper.sh"
    manifest = {
        "mechanism_contract": {
            "campaign_root": str(tmp_path / "campaigns"),
            "interpreter": {"path": "/bin/echo"},
        },
        "launcher": {"path": "/bin/echo"},
        "policy": {
            "correction_controller_installation": {
                "config": {"state_path": str(tmp_path / "campaigns/new.campaign.json")}
            }
        },
    }
    wrapper.write_bytes(
        install._render(install._guard_source(raw), manifest, tmp_path / "host-manifest.json")
    )
    wrapper.chmod(0o755)
    (home / ".codex/handoff").mkdir(parents=True)
    (home / ".codex/handoff/closer-native-handoff.enabled").write_text("1")
    env = dict(os.environ, HOME=str(home))
    assert subprocess.run([str(wrapper), "closer"], env=env, capture_output=True).returncode == 78
    qualified = subprocess.run(
        [str(wrapper), "closer"],
        env=dict(env, CLOSER_NATIVE_QUALIFICATION_REQUEST="1"),
        capture_output=True,
    )
    assert qualified.returncode == 0 and b"native-entry" in qualified.stdout


def test_cli_selftest_is_effect_free(capsys):
    assert install.main(["--selftest"]) == 0
    assert '"authority": false' in capsys.readouterr().out


@pytest.fixture
def install_world(host, tmp_path, monkeypatch):
    manifest, manifest_path, wrapper = host
    source = (
        wrapper.read_text()
        .replace(
            native.MARKER,
            'if [[ "$LANE" == "closer" ]]; then\n  echo "original window expired" >&2\n  exit 78\nfi\n'
            + native.MARKER,
            1,
        )
        .encode()
    )
    wrapper.write_bytes(source)
    wrapper.chmod(0o755)
    monkeypatch.setattr(install, "RESTORED_SHA", hashlib.sha256(source).hexdigest())
    root = Path(manifest["mechanism_contract"]["observation_root"])
    for name in ("native-entry.lock", "native-supervisor.lock"):
        (root / name).write_bytes(b"")
    global_lock = wrapper.parent / ".closer-public989-install.lock"
    global_lock.write_bytes(b"")
    old_lock = tmp_path / "old-campaign.lock"
    old_lock.write_bytes(b"")
    monkeypatch.setattr(
        retained, "lock_rows", lambda *_: [{"path": str(old_lock), "must_exist": True}]
    )
    old_result = tmp_path / "old-result.json"
    old_result.write_bytes(b'{"outcome":"unknown"}')
    old_ref = {
        "path": str(old_result),
        "sha256": hashlib.sha256(old_result.read_bytes()).hexdigest(),
    }
    monkeypatch.setattr(retained, "validate", lambda *_: {"original": {"result": old_ref}})
    receipt = tmp_path / "host-restore-known.json"
    predecessor_roots = {}
    for name in (
        "old_observation_root",
        "new_observation_root",
        "old_campaign_root",
        "new_campaign_root",
        "custody_path",
    ):
        directory = tmp_path / ("predecessor-" + name)
        directory.mkdir()
        predecessor_roots[name] = str(directory)
    predecessor = {
        **predecessor_roots,
        "wrapper": str(wrapper),
        "old_manifest_path": str(tmp_path / "initial.json"),
        "new_runtime_root": str(tmp_path),
        "old_runtime_root": str(tmp_path / "old-runtime"),
        "before_sha256": install.RESTORED_SHA,
        "canary_state": str(tmp_path / "old-state.json"),
    }
    swap = tmp_path / "swap-intent.json"
    swap.write_text(json.dumps({"plan": predecessor}))
    swap_ref = {"path": str(swap), "sha256": hashlib.sha256(swap.read_bytes()).hexdigest()}
    receipt.write_text(
        json.dumps(
            {
                "status": "host_wrapper_restored",
                "before_sha256": install.RESTORED_SHA,
                "mode": 0o755,
                "witnesses": {"result": old_ref, "swap_intent": swap_ref},
            }
        )
    )
    state = Path(manifest["mechanism_contract"]["campaign_root"]) / "new.campaign.json"
    manifest["policy"]["correction_controller_installation"] = {
        "config": {
            "state_path": str(state),
            "repository": "stranske/example",
            "repository_id": 1,
            "epoch": "e" * 64,
            "bootstrap_source_sha": "a" * 40,
            "source_sha": "a" * 40,
            "writer_id": 1,
            "writer_login": "test",
            "writer_type": "User",
            "bootstrap_anchor_sha256": "b" * 64,
            "max_bytes": 1048576,
            "max_entries": 1,
            "max_subjects": 4,
        }
    }
    preview = install._render(install._guard_source(source), manifest, manifest_path)
    manifest["mechanism_contract"]["wrapper"]["sha256"] = hashlib.sha256(preview).hexdigest()
    campaign.write_manifest(manifest_path, manifest)
    args = (
        {"path": "typed-reference", "sha256": "a" * 64},
        tmp_path / "host-manifest.json",
        tmp_path / "old-state.json",
        "worker",
        manifest_path,
        state,
        receipt,
    )
    return args, wrapper, source, old_result, root


def test_install_real_plan_cas_and_native_contract_preserve_unknown(install_world):
    args, wrapper, before, old_result, root = install_world
    raw = old_result.read_bytes()
    proposal = install.plan(*args)
    outcome = install.apply(*args, expected_plan_sha256=proposal["plan_sha256"])
    assert outcome["status"] == "installed" and wrapper.read_bytes() != before
    assert hashlib.sha256(wrapper.read_bytes()).hexdigest() == proposal["after_sha256"]
    assert old_result.read_bytes() == raw and not args[5].exists()
    assert json.loads((root / "wrapper-install-known.json").read_text())["status"] == "installed"
    native._check_wrapper_entry(campaign.load_manifest(args[4]), args[4])


def test_install_changed_manifest_and_contended_lock_do_not_replace(install_world):
    import fcntl

    args, wrapper, before, _, _ = install_world
    proposal = install.plan(*args)
    path = proposal["locks"][0]["path"]
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with pytest.raises(RuntimeError, match="lock_contended"):
            install.apply(*args, expected_plan_sha256=proposal["plan_sha256"])
    finally:
        os.close(fd)
    assert wrapper.read_bytes() == before
    args[4].write_bytes(args[4].read_bytes() + b" ")
    with pytest.raises(install.InstallHeld, match="plan changed"):
        install.apply(*args, expected_plan_sha256=proposal["plan_sha256"])
    assert wrapper.read_bytes() == before


def test_postreplace_failure_retains_intent_and_forbids_blind_retry(install_world, monkeypatch):
    args, wrapper, before, old_result, root = install_world
    raw = old_result.read_bytes()
    proposal = install.plan(*args)
    original = install.host._write_once

    def fail_known(path, value):
        if path.name == "wrapper-install-known.json":
            raise OSError("receipt device failure")
        return original(path, value)

    monkeypatch.setattr(install.host, "_write_once", fail_known)
    with pytest.raises(install.InstallHeld, match="postreplace outcome uncertain"):
        install.apply(*args, expected_plan_sha256=proposal["plan_sha256"])
    assert wrapper.read_bytes() != before and old_result.read_bytes() == raw
    assert (root / "wrapper-install-intent.json").exists()
    with pytest.raises(install.InstallHeld):
        install.apply(*args, expected_plan_sha256=proposal["plan_sha256"])
