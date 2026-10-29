"""The second reviewed predecessor is a capacity stop, never a no-B retry."""

import copy
import hashlib
import json

import pytest

import closer_native_source_upgrade as upgrade


def capacity_stop():
    return {
        "row_status": "capacity_blocked",
        "row_error": "worker_capacity",
        "row_next_trigger": "fresh shared-pool capacity snapshot",
        "result_stages": ["exception", "interpret", "prepare"],
        "native_effect_contract": None,
        "exception": {
            "status": "deferred",
            "reason": "worker_capacity",
            "next_trigger": "fresh shared-pool capacity snapshot",
            "resource_evidence": {
                "capacity": {
                    "lane": "exception",
                    "available": 0,
                    "required": 1,
                    "sampled_at": 1793303747.4790149,
                }
            },
        },
        "interpretation": {
            "classification": "source_native_conservative_inventory",
            "authority": False,
            "actual": {
                "kind": "source_native_conservative_inventory",
                "provider_generation_performed": False,
                "host_intake_ref": {
                    "path": "/private/tmp/source-owned-host-intake.json",
                    "sha256": "a" * 64,
                    "bytes": 9329,
                },
            },
            "requested_agent": None,
            "requested_profile": None,
        },
    }


def test_reviewed_capacity_stop_is_a_separate_non_authoritative_disposition():
    upgrade._diagnostic_no_act(
        upgrade.REVIEWED_CAPACITY_CANDIDATE, {"capacity_disposition": capacity_stop()}
    )
    with pytest.raises(upgrade.SourceUpgradeHeld):
        upgrade._diagnostic_no_act(
            upgrade.REVIEWED_OLD_CANDIDATE, {"capacity_disposition": capacity_stop()}
        )
    with pytest.raises(upgrade.SourceUpgradeHeld):
        upgrade._diagnostic_no_act("a" * 40, {"capacity_disposition": capacity_stop()})


@pytest.mark.parametrize(
    "path,value",
    [
        (("row_status",), "unknown"),
        (("row_error",), "execution_failed"),
        (("row_next_trigger",), "reconcile_before_retry"),
        (("result_stages",), ["act", "exception", "interpret", "prepare"]),
        (("native_effect_contract",), {"kind": "native_remote_only"}),
        (("exception", "status"), "completed"),
        (("exception", "reason"), "worker_capacity_after_execution"),
        (("exception", "next_trigger"), "capacity_recheck"),
        (("exception", "actual"), {"run_id": "provider-ran"}),
        (("exception", "resource_evidence", "capacity", "lane"), "low"),
        (("exception", "resource_evidence", "capacity", "available"), 1),
        (("exception", "resource_evidence", "capacity", "available"), False),
        (("exception", "resource_evidence", "capacity", "required"), 2),
        (("exception", "resource_evidence", "capacity", "sampled_at"), float("nan")),
        (("exception", "resource_evidence", "capacity", "sampled_at"), float("inf")),
        (("exception", "resource_evidence", "capacity", "sampled_at"), -1),
        (("interpretation", "actual", "provider_generation_performed"), True),
        (("interpretation", "actual", "provider_run_id"), "provider-ran"),
        (("interpretation", "actual", "provider_result"), {"output": "provider-ran"}),
        (("interpretation", "actual", "kind"), "model_advice"),
        (("interpretation", "actual", "host_intake_ref", "sha256"), "not-a-digest"),
        (("interpretation", "authority"), True),
        (("interpretation", "requested_agent"), "claude"),
        (("interpretation", "requested_profile"), "profile"),
    ],
)
def test_capacity_stop_refuses_effects_unknowns_and_other_deferred_branches(path, value):
    changed = copy.deepcopy(capacity_stop())
    parent = changed
    for key in path[:-1]:
        parent = parent[key]
    parent[path[-1]] = value
    with pytest.raises(upgrade.SourceUpgradeHeld):
        upgrade._capacity_no_act(changed)


def test_second_predecessor_requires_its_independently_reviewed_module_and_descriptor_hashes(
    monkeypatch,
):
    modules = {"closer_campaign.py": "a" * 64}
    descriptor = {
        "candidate_sha": upgrade.REVIEWED_CAPACITY_CANDIDATE,
        "modules": modules,
        "package_digest": "b" * 64,
        "state_versions": {"campaign": 1},
    }
    raw = json.dumps(descriptor, sort_keys=True).encode()
    manifest = {**descriptor, "public_runtime_descriptor": "closer-runtime-package.json"}
    monkeypatch.setattr(
        upgrade,
        "REVIEWED_CAPACITY_MODULES_SHA256",
        hashlib.sha256(
            json.dumps(modules, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    )
    monkeypatch.setattr(
        upgrade, "REVIEWED_CAPACITY_DESCRIPTOR_SHA256", hashlib.sha256(raw).hexdigest()
    )
    assert upgrade._reviewed_old_metadata(manifest, raw) == modules
    for changed_manifest, changed_raw in (
        ({**manifest, "candidate_sha": "a" * 40}, raw),
        ({**manifest, "modules": {"closer_campaign.py": "c" * 64}}, raw),
        (manifest, raw + b" "),
        ({**manifest, "candidate_sha": upgrade.REVIEWED_OLD_CANDIDATE}, raw),
    ):
        with pytest.raises(upgrade.SourceUpgradeHeld):
            upgrade._reviewed_old_metadata(changed_manifest, changed_raw)
