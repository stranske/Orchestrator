"""Public transition mechanics with explicit synthetic old-validator responses.

These old state/result files are service simulation inputs, not signed business
proof. The real old diagnostic is validated separately against the live cohort.
"""

# ruff: noqa: F811
from __future__ import annotations

import base64
import copy
import hashlib
import json
import shutil
import sys
from pathlib import Path

import pytest
from test_closer_native_observer import host as host  # noqa: F401
from test_closer_native_production_composition import _core

import closer_campaign as campaign
import closer_correction_controller as controller
import closer_frontier_policy as policy
import closer_native_observer as native
import closer_native_readmission as readmission
import closer_native_source_upgrade as upgrade
import closer_native_wrapper_install as install
import closer_pipeline as pipeline


def test_source_upgrade_refuses_nonprivate_observation_root_before_key_creation(tmp_path):
    root = tmp_path / "observations"
    root.mkdir(mode=0o755)
    root.chmod(0o755)
    manifest = {"mechanism_contract": {"observation_root": str(root)}}

    with pytest.raises(upgrade.SourceUpgradeHeld, match="observation root unavailable"):
        upgrade._provision_upgrade_key(manifest)
    assert not (root / "supervisor.key").exists()


@pytest.mark.parametrize("value", [None, [], 7, ""])
def test_prepare_held_rejects_malformed_untrusted_manifest_reference(tmp_path, value):
    seal = tmp_path / "source-upgrade-current-disposition.json"
    seal.write_text(json.dumps({"record": {"new": {"manifest": value}}}))

    with pytest.raises(upgrade.SourceUpgradeHeld, match="upgrade seal invalid"):
        upgrade.prepare_held(seal, confirm_plan_sha256="a" * 64)


@pytest.mark.parametrize(
    "interference",
    [
        "none",
        "old_result",
        "old_manifest",
        "seal_recipe",
        "unsigned_seal",
        "forged_known",
        "old_lock",
        "custody_lock",
        "pre_cas_retry",
        "missing_key_recovery",
        "tampered_recovery",
        "post_cas_unknown",
    ],
)
def test_public_source_upgrade_plan_apply_and_known_held(host, monkeypatch, tmp_path, interference):
    old, old_path, wrapper = host
    modules = Path(old["module_dir"])
    shutil.copy2(Path(upgrade.__file__), modules / Path(upgrade.__file__).name)
    rebuilt = campaign.make_manifest(
        modules,
        candidate_sha=upgrade.REVIEWED_OLD_CANDIDATE,
        state_versions={"campaign": 1, "release": 1},
        python=str(Path(sys.executable).resolve()),
        portable=_core(tmp_path),
        modules=(*old["modules"], Path(upgrade.__file__).name),
        policy=old["policy"],
        campaign_root=old["mechanism_contract"]["campaign_root"],
        manifest_path=old_path,
    )
    rebuilt["mechanism_contract"] = old["mechanism_contract"]
    old = rebuilt
    old_state = Path(old["mechanism_contract"]["campaign_root"]) / "old.campaign.json"
    new_state = tmp_path / "new/campaigns/source-upgrade.campaign.json"
    new_state.parent.mkdir(parents=True)
    new_observation = tmp_path / "new/observations"
    new_observation.mkdir(mode=0o700)
    old["mechanism_contract"]["controller_demand"] = {
        "schema": "closer-controller-demand/v1",
        "campaign_state": str(old_state),
        "campaign_id": "old",
        "owner": "owner",
    }
    handoff = Path.home() / ".codex/handoff"
    monkeypatch.setenv("HANDOFF_DIR", str(handoff))
    frontier = policy.make_frontier(
        ["offline/repo#1"],
        owner="owner",
        frontier_id="old",
        bindings={"offline/repo#1": {"revision": "a" * 64, "action_id": "b" * 64}},
    )
    raw = b'{"targets":[{"target":"offline/repo#1"}]}'
    digest = hashlib.sha256(raw).hexdigest()
    assignment = Path(str(old_state) + ".demand") / (digest + ".json")
    assignment.parent.mkdir()
    assignment.write_bytes(raw)
    hooks = {"bounded_no_new_relay": True}
    for phase in ("prerun", "postrun"):
        path = tmp_path / "old-hooks" / ("handoff-" + phase + ".sh")
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(b"#!/bin/sh\nexit 0\n")
        new_hook = tmp_path / "new/native-hooks" / path.name
        new_hook.parent.mkdir(parents=True, exist_ok=True)
        new_hook.write_bytes(path.read_bytes())
        hooks[phase] = {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    mirror = Path.home() / ".codex/orchestrator-mirror"
    mirror.mkdir()
    shutil.copy2(modules / "closer_dispatcher.py", mirror / "closer_dispatcher.py")
    cfg = {
        "assignment": str(assignment),
        "dispatch_state": str(old_state.parent / "dispatch.json"),
        "runtime_manifest": str(old_path),
        "policy": old["policy"],
        "native_hooks": hooks,
        "handoff_agent": "codex",
        "handoff_dir": str(handoff),
        "mechanism": "native-handoff/v1",
        "granularity": "round",
        "apply": True,
        "execution_policy": "finite-frontier/v2",
        "coordinator_mode": "resource_scoped",
        "coordinator_contract_pin": pipeline.scoped_capability(),
        "dispatch_workers": 1,
        "request_timeout_seconds": 120,
        "finite_frontier": frontier,
    }
    joins = {
        "campaign_id": "old",
        "owner": "owner",
        "campaign_generation": 1,
        "assignment_generation": 1,
        "request_id": "q" * 64,
    }
    old_document = {
        "schema_version": 1,
        "campaign_id": "old",
        "owner": "owner",
        "generation": 1,
        "phase": "work",
        "hold": None,
        "targets": {"*round*": "done"},
        "assignment_targets": ["offline/repo#1"],
        "worker_config": cfg,
        "workers": [
            {
                "worker_id": "012345abcdef",
                "target": "*round*",
                "state": "terminal",
                "outcome": "ok",
                "pid": None,
            }
        ],
        "demand_assignment": {"path": str(assignment), "sha256": digest, **joins},
        "demand_admission": {"assignment_sha256": digest, **joins},
    }
    result = {"worker_id": "012345abcdef", "target": "*round*", "outcome": "ok"}
    result_path = campaign.result_path(old_state, result["worker_id"])
    result_path.parent.mkdir(parents=True)
    result_path.write_text(json.dumps(result))
    (Path(old["mechanism_contract"]["observation_root"]) / "native-active.json").write_text(
        '{"status":"terminal"}'
    )
    # Provision the controller's real read-only custody shape.  No signer or
    # cleanup seam is substituted: inspection validates these exact bytes.
    index = {
        "schema": controller.SCHEMA,
        "repository": "offline/repo",
        "repository_id": 7,
        "epoch": "e" * 64,
        "bootstrap_source_sha": "a" * 40,
        "revision": 0,
        "previous_sha256": None,
        "last_action": None,
        "entries": [],
    }
    anchor = {
        "revision": 0,
        "commit_sha": "b" * 40,
        "blob_sha": "c" * 40,
        "state_sha256": controller.digest(index),
        "previous_anchor_sha256": None,
        "result_name": None,
        "result_sha256": None,
    }
    config = {
        "state_path": str(old_state),
        "repository": "offline/repo",
        "repository_id": 7,
        "epoch": "e" * 64,
        "bootstrap_source_sha": "a" * 40,
        "source_sha": old["candidate_sha"],
        "writer_id": 9,
        "writer_login": "offline",
        "writer_type": "User",
        "bootstrap_anchor_sha256": controller.digest(anchor),
        "max_bytes": 1048576,
        "max_entries": 4,
        "max_subjects": 4,
        "native_transport_source_sha256": old["modules"]["closer_correction_native_effects.py"],
        "consumers": {
            "native": {
                "native_bootstrap": {
                    "private_root": str(old_state.parent / "controller-private"),
                    "confinement": {
                        "helper_path": str(
                            Path(old["module_dir"]) / "closer_codex_confinement_probe.py"
                        ),
                        "helper_sha256": old["modules"]["closer_codex_confinement_probe.py"],
                    },
                }
            }
        },
    }
    custody = Path(str(old_state) + ".evidence") / controller.ControllerConfig(**config).namespace
    custody.mkdir(parents=True)
    (custody / "writer.lock").touch()
    (custody / "namespace.json").write_bytes(
        controller.canonical(
            {
                "schema": "local-correction-controller-namespace/v1",
                "config_sha256": controller.digest(
                    controller.ControllerConfig(**config).source_binding()
                ),
            }
        )
    )
    (custody / "highwater-0000000000000000.json").write_bytes(controller.canonical(anchor))
    custody.chmod(0o700)
    for item in custody.iterdir():
        item.chmod(0o600)
    old["policy"]["correction_controller_installation"] = {"config": config}
    cfg["policy"] = old["policy"]
    old_document["worker_config"] = cfg
    old_state.write_text(json.dumps(old_document, sort_keys=True))
    hook_root = new_state.parent.parent / "native-hooks"
    hook_root.mkdir(exist_ok=True)
    for phase in ("prerun", "postrun"):
        source = Path(cfg["native_hooks"][phase]["path"])
        (hook_root / ("handoff-" + phase + ".sh")).write_bytes(source.read_bytes())

    # Recreate the reviewed restored base and require the production renderer
    # for both old and new wrapper bytes.
    base = b'#!/bin/bash\nLANE="${1:-closer}"\nif [[ "$LANE" == "closer" ]]; then\n  echo "original window expired" >&2\n  exit 78\nfi\n# BEGIN CLOSER PINNED NATIVE ENTRY v1\nold\n# END CLOSER PINNED NATIVE ENTRY v1\nexport PATH="$HOME/.local/bin:$HOME/.cursor/bin:$PATH"\ncase "$LANE" in\ncloser|opener) ;;\nesac\n'
    monkeypatch.setattr(install, "RESTORED_SHA", hashlib.sha256(base).hexdigest())
    wrapper = Path.home() / ".codex/bin/cursor-lane.sh"
    wrapper.parent.mkdir(parents=True, exist_ok=True)
    old["mechanism_contract"]["wrapper"]["sha256"] = hashlib.sha256(
        install._render(install._guard_source(base), old, old_path)
    ).hexdigest()
    wrapper.write_bytes(install._render(install._guard_source(base), old, old_path))
    wrapper.chmod(0o755)
    campaign.write_manifest(old_path, old)

    new = copy.deepcopy(old)
    new["mechanism_contract"]["campaign_root"] = str(new_state.parent)
    new["mechanism_contract"]["observation_root"] = str(new_observation)
    new["mechanism_contract"]["controller_demand"].update(
        campaign_state=str(new_state), campaign_id="new"
    )
    new["command"][new["command"].index("--root") + 1] = str(new_state.parent)
    new["candidate_sha"] = "d" * 40
    new_config = new["policy"]["correction_controller_installation"]["config"]
    new_config["source_sha"] = new["candidate_sha"]
    new_config["state_path"] = str(new_state)
    new_config["native_transport_source_sha256"] = new["modules"][
        "closer_correction_native_effects.py"
    ]
    new_custody = (
        Path(str(new_state) + ".evidence") / controller.ControllerConfig(**new_config).namespace
    )
    new_custody.mkdir(parents=True, mode=0o700)
    (new_custody / "writer.lock").touch(mode=0o600)
    bootstrap = new_config["consumers"]["native"]["native_bootstrap"]
    bootstrap["private_root"] = str(new_state.parent.parent / "controller-private")
    bootstrap["confinement"] = {
        "helper_path": str(Path(new["module_dir"]) / "closer_codex_confinement_probe.py"),
        "helper_sha256": new["modules"]["closer_codex_confinement_probe.py"],
    }
    (new_custody / "namespace.json").write_bytes(
        controller.canonical(
            {
                "schema": "local-correction-controller-namespace/v1",
                "config_sha256": controller.digest(
                    controller.ControllerConfig(**new_config).source_binding()
                ),
            }
        )
    )
    (new_custody / "highwater-0000000000000000.json").write_bytes(controller.canonical(anchor))
    for item in new_custody.iterdir():
        item.chmod(0o600)
    new_path = new_state.parent / "source-upgrade.manifest.json"
    new["command"][new["command"].index("--manifest") + 1] = str(new_path)
    new["command_digest"] = campaign.sha256_json(new["command"])
    new["package_digest"] = campaign.sha256_json(
        [
            new["candidate_sha"],
            sorted(new["modules"].items()),
            new["runtime_digest"],
            new["state_digest"],
            new["portable"],
        ]
    )
    new["mechanism_contract"]["wrapper"]["sha256"] = hashlib.sha256(
        install._render(install._guard_source(base), new, new_path)
    ).hexdigest()
    campaign.write_manifest(new_path, new)

    for root in (Path(old["mechanism_contract"]["observation_root"]), new_observation):
        for name in ("native-entry.lock", "native-supervisor.lock"):
            (root / name).touch()
    Path(str(old_state) + ".lock").touch()
    (wrapper.parent / ".closer-public989-install.lock").touch()
    (Path.home() / ".codex/handoff/.lane-closer.lock.coord.lock").touch()
    swap = old_state.parent / "restored-swap.json"
    historical_custody = tmp_path / "historical-custody"
    historical_custody.mkdir(mode=0o700)
    (historical_custody / "writer.lock").touch(mode=0o600)
    predecessor = {
        "before_sha256": install.RESTORED_SHA,
        "old_observation_root": old["mechanism_contract"]["observation_root"],
        "new_observation_root": new["mechanism_contract"]["observation_root"],
        "custody_path": str(historical_custody),
        "old_campaign_root": str(old_state.parent),
        "new_campaign_root": str(new_state.parent),
        "old_manifest_path": str(old_path),
        "new_runtime_root": old["module_dir"],
        "old_runtime_root": old["module_dir"],
        "wrapper": str(wrapper),
        "canary_state": str(old_state),
    }
    swap.write_text(json.dumps({"plan": predecessor}))
    backup = old_state.parent / "restored-backup.json"
    backup.write_text(
        json.dumps(
            {
                "base64": base64.b64encode(base).decode(),
                "sha256": install.RESTORED_SHA,
                "mode": install.RESTORED_MODE,
            }
        )
    )
    receipt = old_state.parent / "restore-known.json"
    receipt.write_text(
        json.dumps(
            {
                "status": "host_wrapper_restored",
                "before_sha256": install.RESTORED_SHA,
                "mode": install.RESTORED_MODE,
                "witnesses": {
                    "swap_intent": {
                        "path": str(swap),
                        "sha256": hashlib.sha256(swap.read_bytes()).hexdigest(),
                    },
                    "backup": {
                        "path": str(backup),
                        "sha256": hashlib.sha256(backup.read_bytes()).hexdigest(),
                    },
                },
            }
        )
    )

    diagnostic = {
        "worker_target": result["target"],
        "source_target": cfg["finite_frontier"]["targets"][0],
        "correction": {
            "status": "owned_unknown",
            "reason": "owner source criterion differs from seed",
            "remote_authenticated": False,
            "act_authorized": False,
            "source_closure_authorized": False,
        },
        "queue_receipt": {
            "events": [],
            "accounting": [{"operation_id": None, "operation_status": None}],
        },
        "cohort": {"completed": False, "worker_id": result["worker_id"]},
        "completed_round": {"accepted": False},
    }
    monkeypatch.setattr(upgrade, "_old_diagnostic", lambda *_a, **_k: diagnostic)
    monkeypatch.setattr(upgrade, "fresh_read_native_index", lambda *_a: {"offline": "fixed"})
    monkeypatch.setattr(
        upgrade,
        "_installed_release",
        lambda *_a: {"path": str(old_state.parent / "release.json"), "offline_generation": "fixed"},
    )
    frontier = copy.deepcopy(cfg["finite_frontier"])
    frontier["frontier_id"] = "fresh-source-upgrade"
    worker_config = upgrade._physical_recipe(
        cfg, old_document, old["policy"], old_state, new, new_path, new_state, frontier
    )
    plan = upgrade.plan(
        old_path,
        old_state,
        result["worker_id"],
        new_path,
        new_state,
        receipt,
        worker_config,
        frontier,
        old_state.parent / "release.json",
    )
    lock_paths = {row["path"] for row in plan["locks"]}
    assert str(custody / "writer.lock") in lock_paths
    assert str(new_custody / "writer.lock") in lock_paths
    assert plan["authority"] is False and not new_state.exists()
    inputs = (
        old_path,
        old_state,
        result["worker_id"],
        new_path,
        new_state,
        receipt,
        worker_config,
        frontier,
        old_state.parent / "release.json",
    )

    def apply_bound():
        return upgrade.apply(*inputs, expected_plan_sha256=plan["plan_sha256"])

    assert not (new_observation / "supervisor.key").exists()
    original_write = upgrade.host._write_once
    if interference in {
        "pre_cas_retry",
        "missing_key_recovery",
        "tampered_recovery",
        "post_cas_unknown",
    }:
        failed_name = (
            "source-upgrade-known.json"
            if interference == "post_cas_unknown"
            else "source-upgrade-intent.json"
        )

        def fail_write(path, value):
            if Path(path).name == failed_name:
                raise RuntimeError("private-injected-payload")
            return original_write(path, value)

        monkeypatch.setattr(upgrade.host, "_write_once", fail_write)
        if interference == "post_cas_unknown":
            with pytest.raises(
                upgrade.SourceUpgradeHeld, match="known_receipt_write:RuntimeError"
            ) as caught:
                apply_bound()
            assert "private-injected-payload" not in str(caught.value)
            assert (
                hashlib.sha256(wrapper.read_bytes()).hexdigest() == plan["wrapper"]["after_sha256"]
            )
            with pytest.raises(upgrade.SourceUpgradeHeld, match="CAS is not known"):
                upgrade.prepare_held(plan["seal"], confirm_plan_sha256=plan["plan_sha256"])
            with pytest.raises(upgrade.SourceUpgradeHeld):
                apply_bound()
            assert not new_state.exists()
            return
        with pytest.raises(RuntimeError, match="private-injected-payload"):
            apply_bound()
        seal = Path(plan["seal"])
        assert seal.exists() and not seal.with_name("source-upgrade-intent.json").exists()
        assert hashlib.sha256(wrapper.read_bytes()).hexdigest() == plan["wrapper"]["before_sha256"]
        monkeypatch.setattr(upgrade.host, "_write_once", original_write)
        if interference == "missing_key_recovery":
            key = new_observation / "supervisor.key"
            key.unlink()
            with pytest.raises(upgrade.SourceUpgradeHeld, match="signature differs"):
                apply_bound()
            assert not key.exists()
            assert not new_state.exists()
            assert (
                hashlib.sha256(wrapper.read_bytes()).hexdigest() == plan["wrapper"]["before_sha256"]
            )
            return
        if interference == "tampered_recovery":
            changed = json.loads(seal.read_bytes())
            changed["record"]["held_recipe"]["worker_config"]["apply"] = False
            seal.chmod(0o600)
            seal.write_text(json.dumps(changed))
            with pytest.raises(upgrade.SourceUpgradeHeld, match="signature differs"):
                apply_bound()
            assert not new_state.exists()
            assert (
                hashlib.sha256(wrapper.read_bytes()).hexdigest() == plan["wrapper"]["before_sha256"]
            )
            return
    applied = apply_bound()
    assert applied["status"] == "installed"
    assert (new_observation / "supervisor.key").is_file()
    assert wrapper.read_bytes() == install._render(install._guard_source(base), new, new_path)
    assert not new_state.exists()

    before = {p: p.read_bytes() for p in (old_path, old_state, result_path)}
    qualification = native.supervise_native(new, new_path, budget_seconds=45)
    assert qualification["passed"]
    assert campaign.qualify(new, new_path.parent) == []
    if interference == "old_result":
        result_path.write_bytes(result_path.read_bytes() + b" ")
        with pytest.raises(upgrade.SourceUpgradeHeld, match="old result changed"):
            upgrade.prepare_held(plan["seal"], confirm_plan_sha256=plan["plan_sha256"])
        assert not new_state.exists()
        return
    if interference == "old_manifest":
        old_path.write_bytes(old_path.read_bytes() + b" ")
        with pytest.raises(upgrade.SourceUpgradeHeld, match="old manifest changed"):
            upgrade.prepare_held(plan["seal"], confirm_plan_sha256=plan["plan_sha256"])
        assert not new_state.exists()
        return
    if interference == "seal_recipe":
        seal = Path(plan["seal"])
        changed = json.loads(seal.read_bytes())
        changed["record"]["held_recipe"]["worker_config"]["apply"] = False
        seal.chmod(0o600)
        seal.write_text(json.dumps(changed))
        with pytest.raises(upgrade.SourceUpgradeHeld, match="signature differs"):
            upgrade.prepare_held(seal, confirm_plan_sha256=plan["plan_sha256"])
        assert not new_state.exists()
        return
    if interference == "unsigned_seal":
        seal = Path(plan["seal"])
        envelope = json.loads(seal.read_bytes())
        seal.chmod(0o600)
        seal.write_text(json.dumps(envelope["record"], sort_keys=True))
        with pytest.raises(upgrade.SourceUpgradeHeld, match="upgrade seal invalid"):
            upgrade.prepare_held(seal, confirm_plan_sha256=plan["plan_sha256"])
        assert not new_state.exists()
        return
    if interference == "forged_known":
        known = Path(plan["seal"]).with_name("source-upgrade-known.json")
        changed = json.loads(known.read_bytes())
        changed["record"]["status"] = "forged"
        known.chmod(0o600)
        known.write_text(json.dumps(changed, sort_keys=True))
        with pytest.raises(upgrade.SourceUpgradeHeld, match="known receipt signature differs"):
            upgrade.prepare_held(plan["seal"], confirm_plan_sha256=plan["plan_sha256"])
        assert not new_state.exists()
        return
    if interference in {"old_lock", "custody_lock"}:
        import fcntl

        locked_path = (
            Path(str(old_state) + ".lock")
            if interference == "old_lock"
            else custody / "writer.lock"
        )
        with locked_path.open("r+b") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            with pytest.raises(RuntimeError, match="lock_contended"):
                upgrade.prepare_held(plan["seal"], confirm_plan_sha256=plan["plan_sha256"])
        assert not new_state.exists()
        return
    prepared = upgrade.prepare_held(plan["seal"], confirm_plan_sha256=plan["plan_sha256"])
    assert prepared["status"] == "fresh_held_prepared"
    document = campaign.Campaign(new_state).read()
    assert document["hold"]["kind"] == "source_initialization"
    assert document["workers"] == [] and "demand_admission" not in document
    readmission.require_preparation_known(new_state, document, initial=True)
    assert all(p.read_bytes() == raw for p, raw in before.items())
    with pytest.raises(upgrade.SourceUpgradeHeld, match="already exists"):
        upgrade.prepare_held(plan["seal"], confirm_plan_sha256=plan["plan_sha256"])
