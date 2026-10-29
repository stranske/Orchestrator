"""Regression tests for the completed=False, no-native-ACT upgrade boundary."""

import base64
import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import closer_correction_controller as controller
import closer_native_source_upgrade as upgrade
import closer_release


def no_act():
    return {
        "correction": {
            "status": "owned_unknown",
            "reason": "owner source criterion differs from seed",
            "remote_authenticated": False,
            "act_authorized": False,
            "source_closure_authorized": False,
        },
        "queue": {"events": [], "accounting": [{"operation_id": None, "operation_status": None}]},
    }


def test_completed_false_no_act_fixture_is_the_only_accepted_disposition():
    fixture = no_act()
    upgrade._no_act(fixture["correction"], fixture["queue"])
    for path, value in (
        (("correction", "act_authorized"), True),
        (("correction", "reason"), "different scope"),
        (("queue", "events"), [{"operation_id": "native"}]),
        (("queue", "accounting"), ["not-an-accounting-record"]),
    ):
        changed = json.loads(json.dumps(fixture))
        changed[path[0]][path[1]] = value
        with pytest.raises(upgrade.SourceUpgradeHeld):
            upgrade._no_act(changed["correction"], changed["queue"])
    upgrade._incomplete_cohort({"completed": False}, {"accepted": False})
    with pytest.raises(upgrade.SourceUpgradeHeld):
        upgrade._incomplete_cohort({"completed": True}, {"accepted": False})


def test_old_diagnostic_refuses_a_non_source_module_closure(tmp_path, monkeypatch):
    module = tmp_path / "closer_correction_bridge.py"
    module.write_text("# exact old source\n")
    manifest = tmp_path / "host-manifest.json"
    manifest.write_text(
        json.dumps({"module_dir": str(tmp_path), "modules": {module.name: "0" * 64}})
    )
    state = tmp_path / "old.campaign.json"
    state.write_text('{"workers": []}')
    actual_run = upgrade.subprocess.run
    returncodes = []

    def capture_run(*args, **kwargs):
        result = actual_run(*args, **kwargs)
        returncodes.append(result.returncode)
        return result

    monkeypatch.setattr(upgrade.subprocess, "run", capture_run)
    with pytest.raises(upgrade.SourceUpgradeHeld, match="diagnostic"):
        upgrade._old_diagnostic(
            manifest, state, "worker", interpreter=Path(sys.executable).resolve()
        )
    assert returncodes == [31]


def test_installed_release_refuses_a_modified_inspector_before_execution(tmp_path, monkeypatch):
    """The global readback helper itself is a pinned pre-execution witness."""
    home = tmp_path / "home"
    mirror = home / ".codex/orchestrator-mirror"
    inspector = mirror / "scripts/install_verified_snapshot.py"
    inspector.parent.mkdir(parents=True)
    inspector.write_text("# substituted inspector\n")
    journal = tmp_path / "release.json"
    journal.write_text("{}")
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()

    class Record:
        def __init__(self, _path):
            pass

        def read(self):
            return {
                "stage": "installed",
                "candidate_sha": "a" * 40,
                "installed_generation": "generation",
                "verification": {
                    "verified": True,
                    "snapshot_digest": "b" * 64,
                    "snapshot_path": str(snapshot),
                },
            }

    calls = []
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(closer_release, "ReleaseRecord", Record)
    monkeypatch.setattr(upgrade.subprocess, "run", lambda *a, **k: calls.append(a))
    with pytest.raises(upgrade.SourceUpgradeHeld, match="inspector differs"):
        upgrade._installed_release(journal, "a" * 40)
    assert calls == []


def _installed_release_fixture(tmp_path, monkeypatch, *, mutate=False):
    home = tmp_path / "home"
    mirror = home / ".codex/orchestrator-mirror"
    inspector = mirror / "scripts/install_verified_snapshot.py"
    inspector.parent.mkdir(parents=True)
    observed = tmp_path / "inspector-observed.json"
    inspector.write_text(
        "import json, os, pathlib, sys\n"
        "pathlib.Path(os.environ['INSPECTOR_OBSERVED']).write_text(json.dumps({'file': __file__, 'argv0': sys.argv[0], 'argv': sys.argv[1:]}))\n"
        "if os.environ.get('MUTATE_INSPECTOR') == '1' and '--generation-token' in sys.argv:\n"
        " pathlib.Path(__file__).write_text('# changed during inspector execution\\n')\n"
        "print('generation-v1:fixture' if '--generation-token' in sys.argv else 'd' * 64)\n"
    )
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    journal = tmp_path / "release.json"
    journal.write_text("{}")

    class Record:
        def __init__(self, _path):
            pass

        def read(self):
            return {
                "stage": "installed",
                "candidate_sha": "a" * 40,
                "installed_generation": "generation-v1:fixture",
                "verification": {
                    "verified": True,
                    "snapshot_digest": "d" * 64,
                    "snapshot_path": str(snapshot),
                },
            }

    calls = []
    actual_run = upgrade.subprocess.run

    def capture_run(*args, **kwargs):
        calls.append((args, kwargs))
        return actual_run(*args, **kwargs)

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("INSPECTOR_OBSERVED", str(observed))
    if mutate:
        monkeypatch.setenv("MUTATE_INSPECTOR", "1")
    monkeypatch.setattr(closer_release, "ReleaseRecord", Record)
    monkeypatch.setattr(
        upgrade,
        "REVIEWED_INSTALL_VERIFIED_SNAPSHOT_SHA256",
        hashlib.sha256(inspector.read_bytes()).hexdigest(),
    )
    monkeypatch.setattr(upgrade.subprocess, "run", capture_run)
    return journal, inspector, observed, calls


def test_installed_release_executes_the_pinned_inspector_with_physical_identity(
    tmp_path, monkeypatch
):
    journal, inspector, observed, calls = _installed_release_fixture(tmp_path, monkeypatch)

    release = upgrade._installed_release(journal, "a" * 40)

    assert release["installed_generation"] == "generation-v1:fixture"
    assert release["snapshot_digest"] == "d" * 64
    assert len(calls) == 2
    for args, kwargs in calls:
        command = args[0]
        assert command[:3] == [str(Path(sys.executable).resolve()), "-I", "-B"]
        assert kwargs["input"] == inspector.read_text()
    observed_call = json.loads(observed.read_text())
    assert observed_call["file"] == observed_call["argv0"] == str(inspector)
    assert observed_call["argv"][-1] == "--digest"


def test_installed_release_refuses_inspector_mutation_during_execution(tmp_path, monkeypatch):
    journal, _inspector, _observed, calls = _installed_release_fixture(
        tmp_path, monkeypatch, mutate=True
    )

    with pytest.raises(upgrade.SourceUpgradeHeld, match="inspector changed"):
        upgrade._installed_release(journal, "a" * 40)
    assert len(calls) == 2


def test_identity_rejects_principal_epoch_and_anchor_changes():
    old = {
        "policy": {
            "correction_controller_installation": {
                "config": {
                    "repository": "stranske/Workflows",
                    "repository_id": 1,
                    "epoch": "e",
                    "writer_id": 2,
                    "writer_login": "owner",
                    "writer_type": "User",
                    "bootstrap_anchor_sha256": "a",
                }
            }
        }
    }
    for key in old["policy"]["correction_controller_installation"]["config"]:
        new = json.loads(json.dumps(old))
        new["policy"]["correction_controller_installation"]["config"][key] = "changed"
        with pytest.raises(upgrade.SourceUpgradeHeld, match="identity changed"):
            upgrade._same_identity(old, new)


def test_lawful_policy_rebase_refuses_unreviewed_controller_policy_changes(tmp_path):
    """Only the reviewed source/root/transport/helper substitutions may change."""
    module_dir = tmp_path / "runtime"
    module_dir.mkdir()
    helper = module_dir / "closer_codex_confinement_probe.py"
    helper.write_text("# confinement probe\n")
    effects = "e" * 64
    old_policy = {
        "correction_controller_installation": {
            "config": {
                "source_sha": "a" * 40,
                "state_path": "/private/tmp/old.campaign.json",
                "native_transport_source_sha256": "b" * 64,
                "max_entries": 4,
                "consumers": {
                    "native": {
                        "allowed_paths": ["/private/tmp/old"],
                        "native_bootstrap": {
                            "private_root": "/private/tmp/old-private",
                            "confinement": {
                                "helper_path": "/private/tmp/old-probe.py",
                                "helper_sha256": "c" * 64,
                            },
                        },
                    }
                },
            }
        },
        "publication": {"target": "fixed", "permissions": ["read"]},
    }
    new_state = tmp_path / "campaigns" / "fresh.campaign.json"
    new_state.parent.mkdir()
    new = {
        "candidate_sha": "d" * 40,
        "module_dir": str(module_dir),
        "modules": {
            "closer_correction_native_effects.py": effects,
            helper.name: hashlib.sha256(helper.read_bytes()).hexdigest(),
        },
        "policy": json.loads(json.dumps(old_policy)),
    }
    config = new["policy"]["correction_controller_installation"]["config"]
    config["source_sha"] = new["candidate_sha"]
    config["state_path"] = str(new_state)
    config["native_transport_source_sha256"] = effects
    bootstrap = config["consumers"]["native"]["native_bootstrap"]
    bootstrap["private_root"] = str(new_state.parent.parent / "controller-private")
    bootstrap["confinement"] = {
        "helper_path": str(helper),
        "helper_sha256": new["modules"][helper.name],
    }

    assert upgrade._rebased_policy({"policy": old_policy}, new, new_state) == new["policy"]
    for mutate in (
        lambda value: value["correction_controller_installation"]["config"].__setitem__(
            "max_entries", 99
        ),
        lambda value: value["correction_controller_installation"]["config"]["consumers"][
            "native"
        ].__setitem__("allowed_paths", ["/elsewhere"]),
        lambda value: value["correction_controller_installation"]["config"].__setitem__(
            "native_transport_source_sha256", "f" * 64
        ),
        lambda value: value["publication"].__setitem__("permissions", ["write"]),
    ):
        changed = json.loads(json.dumps(new))
        mutate(changed["policy"])
        with pytest.raises(upgrade.SourceUpgradeHeld, match="outside lawful rebase"):
            upgrade._rebased_policy({"policy": old_policy}, changed, new_state)


@pytest.mark.parametrize(
    ("principal", "message"),
    [
        ({"id": 7, "login": "owner", "type": "User"}, None),
        ({"id": 8, "login": "different", "type": "User"}, "native index principal changed"),
    ],
)
def test_fresh_index_is_get_only_and_rejects_principal_drift(monkeypatch, principal, message):
    config = {
        "state_path": "/private/tmp/state",
        "repository": "stranske/Orchestrator",
        "repository_id": 1,
        "epoch": "e" * 64,
        "bootstrap_source_sha": "a" * 40,
        "source_sha": "a" * 40,
        "writer_id": 7,
        "writer_login": "owner",
        "writer_type": "User",
        "bootstrap_anchor_sha256": "b" * 64,
        "max_bytes": 1024,
        "max_entries": 4,
        "max_subjects": 4,
    }
    data = {
        "schema": controller.SCHEMA,
        "repository": config["repository"],
        "repository_id": 1,
        "epoch": config["epoch"],
        "bootstrap_source_sha": config["bootstrap_source_sha"],
        "revision": 0,
        "previous_sha256": None,
        "last_action": None,
        "entries": [],
    }
    raw = json.dumps(data, separators=(",", ":")).encode()
    inspection = {
        "highwater": {
            "anchor": {
                "commit_sha": "c" * 40,
                "blob_sha": "blob",
                "state_sha256": controller.digest(data),
            }
        }
    }
    calls = []
    replies = [
        {"object": {"sha": "c" * 40}},
        {"sha": "blob", "encoding": "base64", "content": base64.b64encode(raw).decode()},
        principal,
        {"id": 1, "full_name": "stranske/Orchestrator"},
    ]

    def run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout=json.dumps(replies.pop(0)))

    monkeypatch.setattr(upgrade.subprocess, "run", run)
    if message:
        with pytest.raises(upgrade.SourceUpgradeHeld, match=message):
            upgrade.fresh_read_native_index(config, inspection)
    else:
        assert upgrade.fresh_read_native_index(config, inspection)["commit_sha"] == "c" * 40
    assert all(argv[:2] == ["gh", "api"] for argv in calls)
