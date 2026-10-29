"""Regression tests for the completed=False, no-native-ACT upgrade boundary."""

import base64
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import closer_correction_controller as controller
import closer_native_source_upgrade as upgrade


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
    ):
        changed = json.loads(json.dumps(fixture))
        changed[path[0]][path[1]] = value
        with pytest.raises(upgrade.SourceUpgradeHeld):
            upgrade._no_act(changed["correction"], changed["queue"])
    upgrade._incomplete_cohort({"completed": False}, {"accepted": False})
    with pytest.raises(upgrade.SourceUpgradeHeld):
        upgrade._incomplete_cohort({"completed": True}, {"accepted": False})


def test_old_diagnostic_refuses_a_non_source_module_closure(tmp_path):
    module = tmp_path / "closer_correction_bridge.py"
    module.write_text("# exact old source\n")
    raw = module.read_bytes()
    manifest = tmp_path / "host-manifest.json"
    manifest.write_text(json.dumps({"modules": {module.name: hashlib.sha256(raw).hexdigest()}}))
    state = tmp_path / "old.campaign.json"
    state.write_text('{"workers": []}')
    with pytest.raises(upgrade.SourceUpgradeHeld, match="diagnostic"):
        upgrade._old_diagnostic(
            manifest, state, "worker", interpreter=Path("/opt/anaconda3/bin/python3")
        )


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


def test_fresh_index_is_get_only_and_rejects_principal_drift(monkeypatch):
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
        {"id": 7, "login": "owner", "type": "User"},
        {"id": 1, "full_name": "stranske/Orchestrator"},
    ]

    def run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout=json.dumps(replies.pop(0)))

    monkeypatch.setattr(upgrade.subprocess, "run", run)
    assert upgrade.fresh_read_native_index(config, inspection)["commit_sha"] == "c" * 40
    assert all(argv[:2] == ["gh", "api"] for argv in calls)
