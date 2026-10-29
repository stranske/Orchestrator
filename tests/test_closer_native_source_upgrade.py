"""Regression tests for the completed=False, no-native-ACT upgrade boundary."""

import hashlib
import json
from pathlib import Path

import pytest

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
        "act_queue": {"events": [], "deferred": {"operation_id": None, "status": None}},
    }


def test_completed_false_no_act_fixture_is_the_only_accepted_disposition():
    fixture = no_act()
    upgrade._no_act(fixture)
    for path, value in (
        (("correction", "act_authorized"), True),
        (("correction", "reason"), "different scope"),
        (("act_queue", "events"), [{"operation_id": "native"}]),
    ):
        changed = json.loads(json.dumps(fixture))
        changed[path[0]][path[1]] = value
        with pytest.raises(upgrade.SourceUpgradeHeld):
            upgrade._no_act(changed)
    upgrade._incomplete_cohort({"completed": False, "accepted": False})
    with pytest.raises(upgrade.SourceUpgradeHeld):
        upgrade._incomplete_cohort({"completed": True, "accepted": False})


def test_old_diagnostic_uses_old_physical_interpreter_and_detects_changed_module(tmp_path):
    module = tmp_path / "closer_correction_bridge.py"
    module.write_text("# exact old source\n")
    raw = module.read_bytes()
    manifest = tmp_path / "host-manifest.json"
    manifest.write_text(json.dumps({"modules": {module.name: hashlib.sha256(raw).hexdigest()}}))
    answer = upgrade._old_diagnostic(manifest, interpreter=Path("/opt/anaconda3/bin/python3"))
    assert answer["modules"] == {module.name: hashlib.sha256(raw).hexdigest()}
    module.write_text("# tampered source\n")
    with pytest.raises(upgrade.SourceUpgradeHeld, match="diagnostic"):
        upgrade._old_diagnostic(manifest, interpreter=Path("/opt/anaconda3/bin/python3"))


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
