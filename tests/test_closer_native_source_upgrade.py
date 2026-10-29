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
        upgrade._old_diagnostic(manifest, state, "worker", interpreter=Path("/opt/anaconda3/bin/python3"))


def test_identity_rejects_principal_epoch_and_anchor_changes():
    old = {"policy": {"correction_controller_installation": {"config": {
        "repository": "stranske/Workflows", "repository_id": 1, "epoch": "e",
        "writer_id": 2, "writer_login": "owner", "writer_type": "User", "bootstrap_anchor_sha256": "a",
    }}}}
    for key in old["policy"]["correction_controller_installation"]["config"]:
        new = json.loads(json.dumps(old))
        new["policy"]["correction_controller_installation"]["config"][key] = "changed"
        with pytest.raises(upgrade.SourceUpgradeHeld, match="identity changed"):
            upgrade._same_identity(old, new)


def test_fresh_index_is_get_only_and_rejects_principal_drift(monkeypatch):
    config = {"state_path": "/private/tmp/state", "repository": "stranske/Orchestrator",
              "repository_id": 1, "epoch": "e" * 64, "bootstrap_source_sha": "a" * 40,
              "source_sha": "a" * 40, "writer_id": 7, "writer_login": "owner", "writer_type": "User",
              "bootstrap_anchor_sha256": "b" * 64, "max_bytes": 1024, "max_entries": 4, "max_subjects": 4}
    data = {"schema": controller.SCHEMA, "repository": config["repository"], "repository_id": 1,
            "epoch": config["epoch"], "bootstrap_source_sha": config["bootstrap_source_sha"],
            "revision": 0, "previous_sha256": None, "last_action": None, "entries": []}
    raw = json.dumps(data, separators=(",", ":")).encode()
    inspection = {"highwater": {"anchor": {"commit_sha": "c" * 40, "blob_sha": "blob",
                                                "state_sha256": controller.digest(data)}}}
    calls = []
    replies = [{"object": {"sha": "c" * 40}}, {"sha": "blob", "encoding": "base64",
               "content": base64.b64encode(raw).decode()}, {"id": 7, "login": "owner", "type": "User"},
               {"id": 1, "full_name": "stranske/Orchestrator"}]
    def run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout=json.dumps(replies.pop(0)))
    monkeypatch.setattr(upgrade.subprocess, "run", run)
    assert upgrade.fresh_read_native_index(config, inspection)["commit_sha"] == "c" * 40
    assert all(argv[:2] == ["gh", "api"] for argv in calls)
