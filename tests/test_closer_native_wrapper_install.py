"""Contract tests for the source-owned wrapper installer."""

import base64
import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest
from test_closer_native_capacity import capacity_host as capacity_host
from test_closer_native_observer import host as host

import closer_campaign as campaign
import closer_native_observer as native
import closer_native_readmission as readmission
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
    with pytest.raises(install.InstallHeld, match="canonical preimage changed"):
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
    existing_locks = [
        Path(predecessor_roots[key]) / leaf
        for key in ("old_observation_root", "new_observation_root")
        for leaf in ("native-entry.lock", "native-supervisor.lock")
    ] + [
        Path(predecessor_roots["custody_path"]) / "writer.lock",
        wrapper.parent.parent / "handoff/.lane-closer.lock.coord.lock",
    ]
    for path in existing_locks:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(mode=0o600)
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


def _prospective_v2_world(capacity_host, monkeypatch, tmp_path):
    """Build a real finite-v2 A/B/C no-start result; no provider is invoked."""
    import copy
    import sys

    from test_closer_native_frontier import finite_host

    import closer_mechanisms as mechanisms
    import closer_runtime_binding as binding

    manifest, old_manifest, old_campaign, old_doc, _source, clock, _frontier = finite_host(
        capacity_host
    )
    old_state = old_campaign.path
    # The finite fixture supplies a real v2 contract; the wrapper installer also
    # needs a real source-installation state binding for its render gate.
    manifest["policy"]["correction_controller_installation"] = {
        "config": {
            "state_path": str(old_state),
            "repository": "offline/repo",
            "repository_id": 1,
            "epoch": "e" * 64,
            "bootstrap_source_sha": "a" * 40,
            "source_sha": "b" * 40,
            "writer_id": 1,
            "writer_login": "fixture",
            "writer_type": "User",
            "bootstrap_anchor_sha256": "c" * 64,
            "max_bytes": 1024,
            "max_entries": 1,
            "max_subjects": 1,
            "consumers": {},
            "native_transport_entrypoint": "fixture",
            "native_transport_source_sha256": "d" * 64,
        }
    }
    archived = old_manifest.parent / "archived-original-wrapper.sh"
    current = Path(manifest["mechanism_contract"]["wrapper"]["path"])
    archived_bytes = current.read_bytes().replace(
        native.MARKER.encode(),
        b'if [[ "$LANE" == "closer" ]]; then\n  echo "original window expired" >&2\n  exit 78\nfi\n'
        + native.MARKER.encode(),
        1,
    )
    archived.write_bytes(archived_bytes)
    monkeypatch.setattr(install, "RESTORED_SHA", hashlib.sha256(archived_bytes).hexdigest())
    old_render = install._render(install._guard_source(archived_bytes), manifest, old_manifest)
    current.write_bytes(old_render)
    current.chmod(0o755)
    manifest["mechanism_contract"]["wrapper"]["sha256"] = hashlib.sha256(old_render).hexdigest()
    campaign.write_manifest(old_manifest, manifest)
    native._write(
        Path(manifest["mechanism_contract"]["observation_root"]) / "native-active.json",
        {
            "nonce": native._CURRENT_ADMISSION[str(native.contract(manifest)[3])]["nonce"],
            "status": "submitted",
            "binding": native.binding(manifest),
        },
    )

    context = native.publish_admission(manifest, old_manifest, old_state, old_doc)
    worker_id = "a1b2c3d4e5f6"
    result = campaign.result_path(old_state, worker_id)
    fields = {
        "state": str(old_state),
        "target": campaign.ROUND_UNIT,
        "owner": old_doc["owner"],
        "generation": "1",
        "result": str(result),
        "runtime-manifest": str(old_manifest),
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
            state=str(old_state),
            target=campaign.ROUND_UNIT,
            owner=old_doc["owner"],
            generation=1,
            result=str(result),
            manifest_path=str(old_manifest),
            campaign_id=old_doc["campaign_id"],
        )
        == context
    )

    def launched(doc):
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

    old_campaign.update(launched)
    cfg = native.admission_config(manifest, old_campaign.read()["worker_config"], context)
    cfg["native_worker"] = fields
    token = "fixture token HANDOFF_EXACT_TOKEN_V1:77\n"
    token_sha = hashlib.sha256(token.encode()).hexdigest()
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
            "lane_dir": str(Path(os.environ["HANDOFF_DIR"]) / ".lane-closer.lock"),
            "expected_token_sha256": token_sha,
            "meta_sha256": token_sha,
            "released_lease_inode": 77,
            "released": True,
            "exact_token_matched": True,
            "released_at": clock[0],
        },
    }
    original_validate = native.validate_admission
    monkeypatch.setattr(
        native,
        "validate_admission",
        lambda *a, **kw: (
            (_ for _ in ()).throw(mechanisms.MechanismError("fixture poll changed"))
            if kw.get("for_effects")
            else original_validate(*a, **kw)
        ),
    )
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
        runner=lambda *_a, **_kw: pytest.fail("B must not run"),
        hooks=hooks,
        now=lambda: clock[0],
    )
    monkeypatch.setattr(native, "validate_admission", original_validate)
    assert outcome == "unavailable"
    campaign.write_result(
        result,
        worker_id,
        campaign.ROUND_UNIT,
        outcome,
        detail,
        mechanism=mechanisms.NATIVE,
        native_admission=context,
    )
    campaign.reconcile(old_campaign, pid_alive=lambda _pid: False)
    root = Path(manifest["mechanism_contract"]["observation_root"])
    native._write(
        root / "native-active.json",
        {"nonce": context["nonce"], "status": "terminal", "binding": native.binding(manifest)},
    )
    for name in ("native-entry.lock", "native-supervisor.lock"):
        (root / name).touch()
    Path(str(old_state) + ".lock").touch()
    old_custody = Path(str(old_state) + ".evidence") / ("correction-index-1-" + "e" * 64)
    old_custody.mkdir(mode=0o700, parents=True)
    (old_custody / "writer.lock").touch()
    return manifest, old_manifest, old_state, worker_id, archived_bytes


def _prospective_install_inputs(capacity_host, monkeypatch, tmp_path):
    import closer_correction_controller as controller

    manifest, old_manifest, old_state, worker_id, archived = _prospective_v2_world(
        capacity_host, monkeypatch, tmp_path
    )
    new_root = tmp_path / "new-observation"
    new_root.mkdir(mode=0o700)
    for name in ("native-entry.lock", "native-supervisor.lock"):
        (new_root / name).touch()
    new_campaigns = tmp_path / "new-campaigns"
    new_campaigns.mkdir()
    new_state = new_campaigns / "fresh.campaign.json"
    copy_manifest = json.loads(old_manifest.read_text())
    copy_manifest["mechanism_contract"]["observation_root"] = str(new_root)
    copy_manifest["mechanism_contract"]["campaign_root"] = str(new_campaigns)
    copy_manifest["policy"]["correction_controller_installation"]["config"]["state_path"] = str(
        new_state
    )
    new_custody = Path(str(new_state) + ".evidence") / ("correction-index-1-" + "e" * 64)
    new_custody.mkdir(mode=0o700, parents=True)
    (new_custody / "writer.lock").touch()
    new_config = copy_manifest["policy"]["correction_controller_installation"]["config"]
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
    (new_custody / "highwater-0000000000000000.json").write_bytes(
        controller.canonical({"revision": 0})
    )
    new_manifest_path = tmp_path / "new-host-manifest.json"
    command = copy_manifest["command"]
    command[command.index("--manifest") + 1] = str(new_manifest_path)
    copy_manifest["command_digest"] = campaign.sha256_json(command)
    new_manifest_path.write_text(json.dumps(copy_manifest, sort_keys=True))
    preview = install._render(install._guard_source(archived), copy_manifest, new_manifest_path)
    copy_manifest["mechanism_contract"]["wrapper"]["sha256"] = hashlib.sha256(preview).hexdigest()
    campaign.write_manifest(new_manifest_path, copy_manifest)
    home = Path.home()
    coord = home / ".codex/handoff/.lane-closer.lock.coord.lock"
    coord.parent.mkdir(parents=True, exist_ok=True)
    coord.touch()
    (
        Path(manifest["mechanism_contract"]["wrapper"]["path"]).parent
        / ".closer-public989-install.lock"
    ).touch()
    legacy = {
        "old_observation_root": tmp_path / "legacy-old",
        "new_observation_root": tmp_path / "legacy-new",
        "old_campaign_root": tmp_path / "legacy-old-campaigns",
        "new_campaign_root": tmp_path / "legacy-new-campaigns",
        "custody_path": tmp_path / "legacy-custody",
    }
    for directory in legacy.values():
        directory.mkdir()
    for root in (legacy["old_observation_root"], legacy["new_observation_root"]):
        for name in ("native-entry.lock", "native-supervisor.lock"):
            (root / name).touch()
    (legacy["custody_path"] / "writer.lock").touch()
    backup = tmp_path / "backup.json"
    backup.write_text(
        json.dumps(
            {
                "base64": base64.b64encode(archived).decode(),
                "sha256": hashlib.sha256(archived).hexdigest(),
                "mode": 0o755,
            }
        )
    )
    swap = tmp_path / "swap.json"
    swap.write_text(
        json.dumps(
            {
                "plan": {
                    "before_sha256": install.RESTORED_SHA,
                    "old_manifest_path": str(old_manifest),
                    "new_runtime_root": str(old_manifest.parent),
                    "old_runtime_root": str(tmp_path / "legacy-runtime"),
                    "wrapper": str(manifest["mechanism_contract"]["wrapper"]["path"]),
                    "canary_state": str(tmp_path / "legacy-state.json"),
                    **{key: str(value) for key, value in legacy.items()},
                }
            }
        )
    )
    receipt = tmp_path / "restore-known.json"

    def ref(path):
        return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}

    receipt.write_text(
        json.dumps(
            {
                "status": "host_wrapper_restored",
                "before_sha256": install.RESTORED_SHA,
                "mode": 0o755,
                "witnesses": {"backup": ref(backup), "swap_intent": ref(swap)},
            }
        )
    )
    return old_manifest, old_state, worker_id, new_manifest_path, new_state, receipt


def test_prospective_install_composes_real_v2_proof_and_preserves_old_bytes(
    capacity_host, monkeypatch, tmp_path
):
    args = _prospective_install_inputs(capacity_host, monkeypatch, tmp_path)
    old_result = campaign.result_path(args[1], args[2])
    old_state = args[1].read_bytes()
    old_result_bytes = old_result.read_bytes()
    proposal = install.plan_prospective(*args)
    outcome = install.apply_prospective(*args, expected_plan_sha256=proposal["plan_sha256"])
    assert outcome["status"] == "installed"
    assert (
        hashlib.sha256(Path(proposal["wrapper"]).read_bytes()).hexdigest()
        == proposal["after_sha256"]
    )
    receipt = Path(proposal["receipt"])
    assert json.loads(receipt.read_text())["status"] == "installed"
    assert args[1].read_bytes() == old_state
    assert old_result.read_bytes() == old_result_bytes
    assert not args[4].exists()
    # The old signed manifest pins the pre-install wrapper, so either the
    # no-start reader or the install's explicit receipt guard must refuse a
    # second attempt.  In both cases the prior install is never retried.
    with pytest.raises(
        (install.InstallHeld, readmission.ReadmissionHeld),
        match="(prior prospective install|no-start)",
    ):
        install.apply_prospective(*args, expected_plan_sha256=proposal["plan_sha256"])


def test_prospective_install_refuses_policy_state_mismatch_before_wrapper_change(
    capacity_host, monkeypatch, tmp_path
):
    args = _prospective_install_inputs(capacity_host, monkeypatch, tmp_path)
    wrapper = Path(json.loads(args[0].read_text())["mechanism_contract"]["wrapper"]["path"])
    before = wrapper.read_bytes()
    manifest = json.loads(args[3].read_text())
    manifest["policy"]["correction_controller_installation"]["config"]["state_path"] = str(
        tmp_path / "wrong.campaign.json"
    )
    campaign.write_manifest(args[3], manifest)
    with pytest.raises(install.InstallHeld, match="state path"):
        install.plan_prospective(*args)
    assert wrapper.read_bytes() == before


@pytest.mark.parametrize("existing", ("intent", "receipt"))
def test_prospective_apply_refuses_wrong_hash_or_existing_install_artifact(
    capacity_host, monkeypatch, tmp_path, existing
):
    args = _prospective_install_inputs(capacity_host, monkeypatch, tmp_path)
    proposal = install.plan_prospective(*args)
    wrapper = Path(proposal["wrapper"])
    before = wrapper.read_bytes()
    with pytest.raises(install.InstallHeld, match="plan changed"):
        install.apply_prospective(*args, expected_plan_sha256="0" * 64)
    artifact = (
        Path(proposal["receipt"]).with_name("wrapper-install-prospective-intent.json")
        if existing == "intent"
        else Path(proposal["receipt"])
    )
    artifact.write_text("already consumed")
    with pytest.raises(install.InstallHeld):
        install.apply_prospective(*args, expected_plan_sha256=proposal["plan_sha256"])
    assert wrapper.read_bytes() == before


def test_prospective_apply_preserves_uncertainty_after_postreplace_failure(
    capacity_host, monkeypatch, tmp_path
):
    args = _prospective_install_inputs(capacity_host, monkeypatch, tmp_path)
    proposal = install.plan_prospective(*args)
    wrapper = Path(proposal["wrapper"])
    monkeypatch.setattr(
        native,
        "_check_wrapper_entry",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("postreplace test failure")),
    )
    with pytest.raises(install.InstallHeld, match="postreplace outcome uncertain"):
        install.apply_prospective(*args, expected_plan_sha256=proposal["plan_sha256"])
    assert hashlib.sha256(wrapper.read_bytes()).hexdigest() == proposal["after_sha256"]
    assert Path(proposal["receipt"]).with_name("wrapper-install-prospective-intent.json").exists()
    assert not Path(proposal["receipt"]).exists()


def test_prospective_apply_refuses_foreign_wrapper_after_intent(
    capacity_host, monkeypatch, tmp_path
):
    args = _prospective_install_inputs(capacity_host, monkeypatch, tmp_path)
    proposal = install.plan_prospective(*args)
    wrapper = Path(proposal["wrapper"])
    foreign = b"#!/bin/bash\necho foreign writer\n"
    original_write_once = install.host._write_once

    def write_intent_then_replace(path, value):
        original_write_once(path, value)
        if path.name == "wrapper-install-prospective-intent.json":
            wrapper.write_bytes(foreign)
            wrapper.chmod(0o755)

    monkeypatch.setattr(install.host, "_write_once", write_intent_then_replace)
    with pytest.raises(install.InstallHeld, match="wrapper changed before replacement"):
        install.apply_prospective(*args, expected_plan_sha256=proposal["plan_sha256"])
    assert wrapper.read_bytes() == foreign
    assert Path(proposal["receipt"]).with_name("wrapper-install-prospective-intent.json").exists()
    assert not Path(proposal["receipt"]).exists()


def test_prospective_current_custody_not_replaced_by_unrelated_predecessor(
    capacity_host, monkeypatch, tmp_path
):
    args = _prospective_install_inputs(capacity_host, monkeypatch, tmp_path)
    old_config = json.loads(args[0].read_text())["policy"]["correction_controller_installation"][
        "config"
    ]
    current = Path(str(args[1]) + ".evidence") / (
        f"correction-index-{old_config['repository_id']}-{old_config['epoch']}"
    )
    (current / "writer.lock").unlink()
    # The restore witness still carries an unrelated legacy custody lock, but
    # it is not a substitute for the current old controller writer fence.
    with pytest.raises(install.InstallHeld, match="old controller writer lock"):
        install.plan_prospective(*args)


@pytest.mark.parametrize("which", ("old", "new"))
def test_prospective_refuses_contended_current_controller_writer(
    capacity_host, monkeypatch, tmp_path, which
):
    import fcntl

    args = _prospective_install_inputs(capacity_host, monkeypatch, tmp_path)
    state = args[1] if which == "old" else args[4]
    manifest = args[0] if which == "old" else args[3]
    config = json.loads(manifest.read_text())["policy"]["correction_controller_installation"][
        "config"
    ]
    writer = (
        Path(str(state) + ".evidence")
        / (f"correction-index-{config['repository_id']}-{config['epoch']}")
        / "writer.lock"
    )
    fd = os.open(writer, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(RuntimeError, match="lock_contended"):
            install.apply_prospective(
                *args, expected_plan_sha256=install.plan_prospective(*args)["plan_sha256"]
            )
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def test_prospective_cli_plan_and_apply_need_no_reconciliation(
    capacity_host, monkeypatch, tmp_path, capsys
):
    args = _prospective_install_inputs(capacity_host, monkeypatch, tmp_path)
    cli = [
        "--kind",
        "prospective",
        "plan",
        "--old-manifest",
        str(args[0]),
        "--old-state",
        str(args[1]),
        "--worker",
        args[2],
        "--new-manifest",
        str(args[3]),
        "--new-state",
        str(args[4]),
        "--restore-receipt",
        str(args[5]),
    ]
    assert install.main(cli) == 0
    proposal = json.loads(capsys.readouterr().out)
    assert proposal["schema"] == install.PROSPECTIVE_SCHEMA
    apply_cli = ["apply" if value == "plan" else value for value in cli]
    assert (
        install.main(
            apply_cli
            + [
                "--expected-plan-sha256",
                proposal["plan_sha256"],
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["status"] == "installed"


@pytest.mark.parametrize("change", ["proof", "wrapper", "lock", "namespace"])
def test_prospective_install_refuses_changed_evidence_or_closure(
    capacity_host, monkeypatch, tmp_path, change
):
    args = _prospective_install_inputs(capacity_host, monkeypatch, tmp_path)
    wrapper = Path(json.loads(args[0].read_text())["mechanism_contract"]["wrapper"]["path"])
    before = wrapper.read_bytes()
    if change == "proof":
        result = campaign.result_path(args[1], args[2])
        value = json.loads(result.read_text())
        detail = json.loads(value["detail"])
        detail["native_no_start"]["phases"]["B"]["started"] = True
        value["detail"] = json.dumps(detail)
        result.write_text(json.dumps(value))
    elif change == "wrapper":
        wrapper.write_bytes(before + b"# tampered\n")
    elif change == "lock":
        (
            Path(json.loads(args[0].read_text())["mechanism_contract"]["observation_root"])
            / "native-entry.lock"
        ).unlink()
    else:
        (
            Path(json.loads(args[3].read_text())["mechanism_contract"]["observation_root"])
            / "foreign"
        ).write_text("x")
    with pytest.raises((install.InstallHeld, RuntimeError, ValueError)):
        install.plan_prospective(*args)
    assert wrapper.read_bytes() == (before if change != "wrapper" else before + b"# tampered\n")
