import base64
import hashlib
import json
import os
from pathlib import Path

import pytest

import closer_host_restore_contract as restore
import closer_mechanisms as mechanisms


def _sha(value):
    return hashlib.sha256(Path(value).read_bytes()).hexdigest()


def _write(path, value):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_bytes(value if isinstance(value, bytes) else json.dumps(value).encode())


def _ref(path):
    return {"path": str(path), "sha256": _sha(path)}


def _world(tmp_path, monkeypatch):
    home = tmp_path / "home"
    wrapper = home / ".codex/bin/cursor-lane.sh"
    _write(wrapper, b"temporary\n")
    wrapper.chmod(0o755)
    monkeypatch.setattr(restore.Path, "home", classmethod(lambda _cls: home))
    root = tmp_path / "observation"
    root.mkdir(mode=0o700)
    key = root / "supervisor.key"
    key.write_bytes(b"k" * 32)
    key.chmod(0o600)
    old_observation = tmp_path / "old-observation"
    old_observation.mkdir()
    new_runtime = tmp_path / "runtime"
    new_runtime.mkdir()
    module = new_runtime / "closer.py"
    _write(module, b"module\n")
    manifest = {
        "candidate_sha": "a" * 40,
        "modules": {"closer.py": _sha(module)},
        "policy": {},
        "mechanism_contract": {"observation_root": str(root)},
    }
    _write(new_runtime / "host-manifest.json", manifest)
    old_campaign = tmp_path / "old-campaign"
    new_campaign = tmp_path / "new-campaign"
    old_campaign.mkdir()
    new_campaign.mkdir()
    nonce = "nonce"
    nonce_root = root / nonce
    nonce_root.mkdir(mode=0o700)
    admission_context = {
        "nonce": nonce,
        "schema": "native-invocation-admission/v2",
        "runtime_manifest": str(new_runtime / "host-manifest.json"),
    }
    worker_fields = {
        "state": str(new_campaign / "attempt.campaign.json"),
        "result": str(new_campaign / "worker1.json"),
    }
    result = {
        "worker_id": "worker1",
        "outcome": "unknown",
        "native_admission": admission_context,
        "detail": {
            "native_execution": {
                "schema": "native-round-execution/v1",
                "admission_nonce": nonce,
                "worker": worker_fields,
                "phases": {
                    "A": {
                        "admitted": True,
                        "exit_code": 0,
                        "token_sha256": "a" * 64,
                        "lease_inode": 7,
                    },
                    "B": {"started": False},
                    "C": {
                        "attempted": True,
                        "exit_code": 65,
                        "started_at": 1,
                        "finished_at": 3,
                        "cleanup": {
                            "schema": "closer-native-lease-cleanup/v1",
                            "lane": "closer",
                            "lane_dir": str(home / ".codex/handoff/.lane-closer.lock"),
                            "expected_token_sha256": "a" * 64,
                            "meta_sha256": "a" * 64,
                            "released_lease_inode": 7,
                            "exact_token_matched": True,
                            "released": True,
                            "released_at": 2,
                        },
                    },
                },
            }
        },
    }
    result_path = new_campaign / "worker1.json"
    _write(result_path, result)
    state_path = new_campaign / "attempt.campaign.json"
    _write(
        state_path,
        {
            "worker_config": {
                "runtime_manifest": str(new_runtime / "host-manifest.json"),
                "dispatch_state": str(new_campaign / "dispatch"),
            }
        },
    )
    _write(new_campaign / "dispatch.pipeline.json.runlock", b"")
    custody = tmp_path / "custody"
    custody.mkdir()
    _write(custody / "writer.lock", b"")
    for directory in (old_observation, root, home / ".codex/handoff"):
        for leaf in (
            ("native-supervisor.lock", "native-entry.lock")
            if directory != home / ".codex/handoff"
            else (".lane-closer.lock.coord.lock",)
        ):
            _write(directory / leaf, b"")
    _write(wrapper.parent / ".closer-public989-install.lock", b"")
    backup = b"original\n"
    binding = {
        "mechanism": mechanisms.NATIVE,
        "mechanism_contract_digest": mechanisms.digest(manifest["mechanism_contract"]),
        "policy_digest": mechanisms.digest(manifest["policy"]),
        **{key: manifest.get(key) for key in mechanisms.PIN_KEYS},
    }
    records = [
        ("admission.json", {"binding": binding, "context": admission_context}),
        (
            "worker.json",
            {
                "schema": "native-worker-handoff/v1",
                "binding": binding,
                "nonce": nonce,
                "admission": admission_context,
                "fields": worker_fields,
            },
        ),
        (
            "terminal.json",
            {
                "schema": "native-wrapper-lifecycle/v1",
                "source": "owned-native-wrapper-process",
                "passed": True,
                "binding": binding,
                "nonce": nonce,
                "wrapper": {"pid": 1001},
                "entry": {"pid": 1002},
                "tick_result": [{"kind": "launched", "worker_id": "worker1", "pid": 1003}],
            },
        ),
    ]
    signed = {}
    for name, record in records:
        path = nonce_root / ("worker-worker1.json" if name == "worker.json" else name)
        _write(
            path, {"record": record, "signature": mechanisms._signature(record, key.read_bytes())}
        )
        signed[name] = path
    receipts = tmp_path / "receipts"
    receipts.mkdir()
    plan = {
        "schema": "closer-temporary-canonical-wrapper-window/v1",
        "wrapper": str(wrapper),
        "source_sha": "a" * 40,
        "before_sha256": hashlib.sha256(backup).hexdigest(),
        "after_sha256": hashlib.sha256(wrapper.read_bytes()).hexdigest(),
        "before_mode": 0o755,
        "new_runtime_root": str(new_runtime),
        "old_runtime_root": str(tmp_path / "old-runtime"),
        "manifest_sha256": _sha(new_runtime / "host-manifest.json"),
        "modules": manifest["modules"],
        "new_observation_root": str(root),
        "old_observation_root": str(old_observation),
        "old_campaign_root": str(old_campaign),
        "new_campaign_root": str(new_campaign),
        "old_manifest_path": str(tmp_path / "old-manifest.json"),
        "custody_path": str(custody),
        "effect_targets": ["one"],
        "canary_state": str(state_path),
        "receipts_root": str(receipts),
        "original_deadline": 1,
    }
    _write(
        tmp_path / "intent.json",
        {
            "before_sha256": plan["before_sha256"],
            "after_sha256": plan["after_sha256"],
            "plan": plan,
        },
    )
    _write(
        tmp_path / "known.json",
        {
            "before_sha256": plan["before_sha256"],
            "after_sha256": plan["after_sha256"],
            "original_deadline": 1,
        },
    )
    _write(
        tmp_path / "backup.json",
        {
            "base64": base64.b64encode(backup).decode(),
            "sha256": plan["before_sha256"],
            "mode": 0o755,
        },
    )
    request = {
        "schema": restore.SCHEMA,
        "swap_intent": _ref(tmp_path / "intent.json"),
        "swap_known": _ref(tmp_path / "known.json"),
        "backup": _ref(tmp_path / "backup.json"),
        "admission": _ref(signed["admission.json"]),
        "worker_handoff": _ref(signed["worker.json"]),
        "terminal": _ref(signed["terminal.json"]),
        "result": _ref(result_path),
    }
    request_path = tmp_path / "request.json"
    _write(request_path, request)
    monkeypatch.setattr(restore, "_dead", lambda pid: None)
    return request_path, wrapper, backup, result_path, signed["terminal.json"]


def test_cas_restores_only_wrapper_and_preserves_unknown(tmp_path, monkeypatch):
    request, wrapper, backup, result, _ = _world(tmp_path, monkeypatch)
    before = result.read_bytes()
    proposal = restore.plan(request)
    assert proposal["eligible"] is True
    outcome = restore.apply(request, proposal["plan_sha256"])
    assert (
        outcome["status"] == "host_wrapper_restored"
        and wrapper.read_bytes() == backup
        and result.read_bytes() == before
    )
    assert outcome["unknown_reclassification_authority"] is False
    assert any(
        row["path"].endswith("dispatch.pipeline.json.runlock") and row["must_exist"]
        for row in proposal["locks"]
    )


def test_static_dynamic_lock_collision_keeps_existing_requirement(tmp_path, monkeypatch):
    request, wrapper, *_ = _world(tmp_path, monkeypatch)
    collision = wrapper.parent / ".closer-public989-install.lock"
    monkeypatch.setattr(restore, "_dynamic_locks", lambda _plan: [collision])
    proposal = restore.plan(request)
    rows = [row for row in proposal["locks"] if row["path"] == str(collision)]
    assert rows == [{"path": str(collision), "must_exist": True}]
    collision.unlink()
    with pytest.raises(restore.Refused, match="mandatory_lock_missing"):
        with restore._hold(rows):
            pass
    assert not collision.exists()


def test_release_lock_resources_are_derived_from_real_config(tmp_path, monkeypatch):
    request, wrapper, *_ = _world(tmp_path, monkeypatch)
    intent = json.loads(Path(json.loads(request.read_text())["swap_intent"]["path"]).read_text())
    retained = intent["plan"]
    campaign = Path(retained["canary_state"])
    document = json.loads(campaign.read_text())
    record = tmp_path / "release-record.json"
    config = tmp_path / "release-config.json"
    _write(
        config,
        {
            "record": str(record),
            "args": {"mirror": str(wrapper.parent), "installer": str(tmp_path / "installer.py")},
        },
    )
    document["release_attempt"] = {"id": "attempt"}
    document["release_record"] = str(record)
    document["worker_config"]["release"] = {"config_file": str(config)}
    _write(campaign, document)
    for suffix in (".lock", ".publisher.lock"):
        _write(Path(str(record) + suffix), b"")
    paths = {row["path"] for row in restore.plan(request)["locks"] if row["must_exist"]}
    assert {str(record) + ".lock", str(record) + ".publisher.lock"} <= paths


@pytest.mark.parametrize("mutate", ["backup", "nonce", "result", "lease", "race"])
def test_refuses_changed_witnesses_and_lease(tmp_path, monkeypatch, mutate):
    request, wrapper, backup, result, terminal = _world(tmp_path, monkeypatch)
    if mutate == "backup":
        Path(json.loads(request.read_text())["backup"]["path"]).write_text("{}")
    elif mutate == "nonce":
        item = json.loads(terminal.read_text())
        item["record"]["nonce"] = "foreign"
        terminal.write_text(json.dumps(item))
    elif mutate == "result":
        result.write_text(json.dumps({"worker_id": "worker1", "outcome": "accepted"}))
    elif mutate == "lease":
        monkeypatch.setattr(
            restore,
            "_dead",
            lambda pid: (_ for _ in ()).throw(restore.Refused("signed_process_or_group_is_live")),
        )
    else:
        wrapper.write_bytes(b"raced")
    assert restore.plan(request)["eligible"] is False


def test_lock_contention_and_foreign_request_fields_refuse(tmp_path, monkeypatch):
    request, _, _, _, _ = _world(tmp_path / "successor", monkeypatch)
    proposal = restore.plan(request)
    lock = proposal["locks"][0]["path"]
    fd = os.open(lock, os.O_RDWR)
    import fcntl

    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    with pytest.raises(restore.Refused, match="lock_contended"):
        restore.apply(request, proposal["plan_sha256"])
    os.close(fd)
    item = json.loads(request.read_text())
    item["locks"] = []
    request.write_text(json.dumps(item))
    assert restore.plan(request)["eligible"] is False


def test_resigned_crossjoin_cleanup_and_successor_lane_refuse(tmp_path, monkeypatch):
    request, _, _, result, _ = _world(tmp_path, monkeypatch)
    item = json.loads(result.read_text())
    item["detail"]["native_execution"]["phases"]["C"]["cleanup"]["released_lease_inode"] = 99
    result.write_text(json.dumps(item))
    request_item = json.loads(request.read_text())
    request_item["result"] = _ref(result)  # bypasses only the outer digest pin
    request.write_text(json.dumps(request_item))
    assert restore.plan(request)["eligible"] is False
    # A current successor mutex is independently decisive.
    request, _, _, _, _ = _world(tmp_path / "successor", monkeypatch)
    (Path.home() / ".codex/handoff/.lane-closer.lock").mkdir()
    assert restore.plan(request)["eligible"] is False


def test_selftest_has_no_request_or_host_effect(capsys):
    assert restore.main(["--selftest"]) == 0
    assert json.loads(capsys.readouterr().out)["selftest"] == "ok"


def test_foreign_result_symlink_and_missing_mandatory_lock_refuse(tmp_path, monkeypatch):
    request, _, _, result, _ = _world(tmp_path, monkeypatch)
    foreign = tmp_path / "foreign-result.json"
    foreign.write_bytes(result.read_bytes())
    item = json.loads(request.read_text())
    item["result"] = _ref(foreign)
    request.write_text(json.dumps(item))
    assert restore.plan(request)["eligible"] is False
    with pytest.raises(restore.Refused, match="mandatory_lock_missing"):
        with restore._hold([{"path": str(tmp_path / "gone.lock"), "must_exist": True}]):
            pass
    request, _, _, _, _ = _world(tmp_path / "symlink", monkeypatch)
    backup = Path(json.loads(request.read_text())["backup"]["path"])
    target = backup.with_name("backup-target.json")
    backup.rename(target)
    backup.symlink_to(target)
    assert restore.plan(request)["eligible"] is False


def test_postreplace_failure_is_uncertain_not_retryable(tmp_path, monkeypatch):
    request, _, _, _, _ = _world(tmp_path, monkeypatch)
    proposal = restore.plan(request)
    original = restore._write_once
    calls = 0

    def fail_known(path, item):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("receipt device failed")
        original(path, item)

    monkeypatch.setattr(restore, "_write_once", fail_known)
    with pytest.raises(restore.Refused, match="postreplace_outcome_uncertain"):
        restore.apply(request, proposal["plan_sha256"])
