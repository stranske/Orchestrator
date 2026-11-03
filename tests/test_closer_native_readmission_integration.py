# ruff: noqa: F811
"""Offline integration coverage for a prospective finite native readmission."""

from __future__ import annotations

import copy
import hashlib
import json
import sys
from pathlib import Path

import pytest
from test_closer_native_capacity import capacity_host  # noqa: F401
from test_closer_native_frontier import finite_host
from test_closer_native_observer import host  # noqa: F401

import closer_campaign as campaign
import closer_frontier_policy as policy
import closer_mechanisms as mechanisms
import closer_native_observer as native
import closer_native_readmission as readmission
import closer_runtime_binding as binding


def test_v2_no_start_reconcile_readmission_then_fresh_admission(  # noqa: F811
    capacity_host, monkeypatch
):
    """A real sealed v2 refusal permits exactly one new finite campaign record."""
    manifest, manifest_path, old_campaign, old_doc, _source, clock, _frontier = finite_host(
        capacity_host
    )
    context = native.publish_admission(manifest, manifest_path, old_campaign.path, old_doc)
    worker_id = "abcdef012345"
    result = campaign.result_path(old_campaign.path, worker_id)
    fields = {
        "state": str(old_campaign.path),
        "target": campaign.ROUND_UNIT,
        "owner": old_doc["owner"],
        "generation": "1",
        "result": str(result),
        "runtime-manifest": str(manifest_path),
        "mechanism": mechanisms.NATIVE,
    }
    argv = [
        manifest["mechanism_contract"]["interpreter"]["path"],
        manifest["module_path"],
        "worker",
    ]
    for key, value in fields.items():
        argv.extend(["--" + key, value])
    native.authorize_worker(manifest, argv)
    assert (
        native.accept_worker(
            manifest,
            state=str(old_campaign.path),
            target=campaign.ROUND_UNIT,
            owner=old_doc["owner"],
            generation=1,
            result=str(result),
            manifest_path=str(manifest_path),
            campaign_id=old_doc["campaign_id"],
        )
        == context
    )

    def launch(doc):
        doc["worker_config"] = copy.deepcopy(old_doc["worker_config"])
        doc["workers"].append(
            {
                "worker_id": worker_id,
                "target": campaign.ROUND_UNIT,
                "pid": None,
                "claim_target": "offline",
                "state": "launched",
            }
        )

    old_campaign.update(launch)
    old_doc = old_campaign.read()
    cfg = native.admission_config(manifest, old_doc["worker_config"], context)
    cfg["native_worker"] = fields
    token = "fixture token HANDOFF_EXACT_TOKEN_V1:77\n"
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    handoff = Path.home() / ".codex/handoff"
    monkeypatch.setenv("HANDOFF_DIR", str(handoff))
    hooks = object.__new__(binding.PinnedNativeHooks)
    hooks.bounded = True
    hooks.interpreter_anchor = Path(sys.executable)
    hooks.dependencies = {"interpreter": (hooks.interpreter_anchor, "fixture")}
    hooks.env = {}
    hooks.prerun = lambda _deadline: (0, token)
    hooks.owns = lambda candidate: candidate == token
    hooks._dependencies_match = lambda: True
    hooks.postrun = lambda _token, _deadline: 65
    hooks.postrun_result = {
        "code": 65,
        "dependencies_valid": True,
        "refusal": "terminal_named_receipt_unavailable",
        "cleanup": {
            "schema": "closer-native-lease-cleanup/v1",
            "lane": "closer",
            "lane_dir": str(handoff / ".lane-closer.lock"),
            "expected_token_sha256": token_hash,
            "meta_sha256": token_hash,
            "released_lease_inode": 77,
            "released": True,
            "exact_token_matched": True,
            "released_at": clock[0],
        },
    }
    original_validate = native.validate_admission

    def refusal(*args, **kwargs):
        if kwargs.get("for_effects"):
            raise mechanisms.MechanismError("fixture poll changed")
        return original_validate(*args, **kwargs)

    monkeypatch.setattr(native, "validate_admission", refusal)
    outcome, detail = binding.run_native_round(
        [
            str(hooks.interpreter_anchor),
            "fixture.py",
            "--assignment",
            cfg["assignment"],
            "--finite-frontier-json",
            json.dumps(cfg["finite_frontier"]),
            "--request-timeout-seconds",
            "30",
        ],
        cfg,
        runner=lambda *_a, **_kw: pytest.fail("B must not run after pre-dispatch refusal"),
        hooks=hooks,
        now=lambda: clock[0],
    )
    assert outcome == "unavailable"
    campaign.write_result(
        result,
        worker_id,
        campaign.ROUND_UNIT,
        outcome,
        detail,
        mechanism="native-handoff/v1",
        native_admission=context,
    )
    reconciled = campaign.reconcile(old_campaign, pid_alive=lambda _pid: False)
    assert reconciled["workers"][0]["outcome"] == "unavailable"
    assert reconciled["targets"][campaign.ROUND_UNIT] == "reconcile"
    proof = json.loads(detail)["native_no_start"]
    assert native.pre_dispatch_no_start_proof(manifest, cfg, proof) == proof

    monkeypatch.setattr(native, "validate_admission", original_validate)
    fresh_frontier = copy.deepcopy(old_doc["worker_config"]["finite_frontier"])
    fresh_frontier["frontier_id"] = "new-finite-authority"
    fresh_frontier = policy.make_frontier(
        fresh_frontier["targets"],
        owner=fresh_frontier["owner"],
        frontier_id=fresh_frontier["frontier_id"],
        bindings=fresh_frontier["bindings"],
    )
    new_cfg = copy.deepcopy(old_doc["worker_config"])
    new_cfg["runtime_manifest"] = str(manifest_path)
    new_state = (
        Path(manifest["mechanism_contract"]["campaign_root"]) / "fresh-readmission.campaign.json"
    )
    root = native.contract(manifest)[3]
    for name in ("native-entry.lock", "native-supervisor.lock"):
        (root / name).touch()
    lane_coord = Path.home() / ".codex/handoff/.lane-closer.lock.coord.lock"
    lane_coord.parent.mkdir(parents=True, exist_ok=True)
    lane_coord.touch()
    Path(str(old_campaign.path) + ".lock").touch()
    planned = readmission.plan_prospective(
        manifest_path,
        old_campaign.path,
        worker_id,
        manifest_path,
        new_cfg,
        new_state,
        fresh_frontier,
    )
    readmission.apply_prospective(
        manifest_path,
        old_campaign.path,
        worker_id,
        manifest_path,
        new_cfg,
        new_state,
        fresh_frontier,
        confirm_plan_sha256=planned["plan_sha256"],
    )
    created = campaign.Campaign(new_state).read()
    assert created["targets"] == {campaign.ROUND_UNIT: "pending"}
    assert created["assignment_targets"] == fresh_frontier["targets"]

    capacity_host[6](2)
    invocation = native._CURRENT_ADMISSION[str(native.contract(manifest)[3])]
    invocation.pop("original_deadline")
    invocation["execution_policy"] = policy.POLICY
    fresh_context = native.publish_admission(manifest, manifest_path, new_state, created)
    assert fresh_context["nonce"] != context["nonce"]
    assert native.validate_admission(manifest, fresh_context, for_effects=True) == fresh_context
