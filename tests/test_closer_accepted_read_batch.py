"""Static-work/fresh-policy tests. Synthetic leaf facts never issue native authority.

Actual _qualified_policy, qualify, verify_modules and the borrowing helper run.
Native call-path shells are inert frame/argument fixtures; they stop immediately
following policy reads, before protected records, lifecycle or effects.
"""

import copy
import hashlib
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import closer_campaign as campaign
import closer_correction_controller as ctl
import closer_native_observer as native
import closer_runtime_binding as runtime
import closer_source_installation as source


class PolicyReadFinished(Exception):
    pass


@pytest.fixture
def world(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    modules = root / "modules"
    modules.mkdir()
    names = [
        "closer_source_installation.py",
        "closer_runtime_binding.py",
        "closer_correction_controller.py",
    ]
    for name in names:
        (modules / name).write_text("# synthetic immutable source fixture\n")

    def sha(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    state = root / "genuine.campaign.json"
    anchor = {
        "revision": 0,
        "commit_sha": "a" * 40,
        "blob_sha": "b" * 40,
        "state_sha256": "c" * 64,
        "previous_anchor_sha256": None,
        "result_name": None,
        "result_sha256": None,
    }
    config = {
        "state_path": str(state),
        "repository": "o/r",
        "repository_id": 1,
        "epoch": "synthetic",
        "bootstrap_anchor_sha256": source._digest(anchor),
    }
    custody = Path(str(state) + ".evidence") / "correction-index-1-synthetic"
    custody.mkdir(parents=True, mode=0o700)
    (custody / "namespace.json").write_text(
        json.dumps(
            {
                "schema": "local-correction-controller-namespace/v1",
                "config_sha256": source._digest(config),
            }
        )
    )
    (custody / "highwater-0000000000000000.json").write_text(json.dumps(anchor))
    core = root / "core"
    core.mkdir()
    core_files = {}
    for name in runtime.CORE_FILES:
        path = core / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("synthetic immutable Core leaf\n")
        core_files[name] = sha(path)
    (core / "closer-core-package.json").write_text(json.dumps({"files": core_files}))
    wrapper, interpreter = root / "wrapper.sh", root / "python-leaf"
    wrapper.write_text("synthetic wrapper leaf\n")
    interpreter.write_text("synthetic interpreter leaf\n")
    native_root = root / "native"
    native_root.mkdir()
    phase = native_root / ("a" * 32)
    phase.mkdir()
    (phase / "phase.json").write_text('{"synthetic":true}')
    (native_root / "native-qualification.json").write_text(
        json.dumps(
            {
                "record": {
                    "nonce": phase.name,
                    "phases": {
                        "startup": {"file": "phase.json", "sha256": sha(phase / "phase.json")}
                    },
                }
            }
        )
    )
    manifest_path = root / "manifest.json"
    manifest = {
        "module_path": str(modules / "closer_campaign.py"),
        "module_dir": str(modules),
        "modules": {n: sha(modules / n) for n in names},
        "policy": {
            "correction_controller_installation": {"schema": source.SCHEMA, "config": config}
        },
        "mechanism_contract": {"campaign_root": str(root)},
        "portable": {"package_root": str(core)},
        "state_versions": {"campaign": campaign.STATE_SCHEMA},
        "candidate_sha": "d" * 40,
        "runtime": {},
        "command": [],
    }
    for name in ["runtime", "command", "state_versions"]:
        manifest["state_digest" if name == "state_versions" else name + "_digest"] = (
            campaign.sha256_json(manifest[name])
        )
    manifest["package_digest"] = campaign.sha256_json(
        [
            manifest["candidate_sha"],
            sorted(manifest["modules"].items()),
            manifest["runtime_digest"],
            manifest["state_digest"],
            manifest["portable"],
        ]
    )
    manifest_path.write_text(json.dumps(manifest))
    state.write_text(
        json.dumps(
            {
                "campaign_id": "synthetic",
                "owner": "owner",
                "generation": 1,
                "release_pin": manifest["package_digest"],
                "worker_config": {"runtime_manifest": str(manifest_path)},
                "correction_controller": {"config": config},
            }
        )
    )
    binding = SimpleNamespace(mechanism="native-handoff/v1", policy=manifest["policy"])
    origin = SimpleNamespace(path=str(manifest_path), sha256=sha(manifest_path))
    monkeypatch.setattr(
        runtime, "_factory_manifest_origin", lambda value: origin if value is binding else None
    )
    monkeypatch.setattr(source, "__file__", str(modules / names[0]))
    monkeypatch.setattr(source, "_SOURCE_SHA256", manifest["modules"][names[0]])
    monkeypatch.setattr(source, "_REQUIRED", set(names))
    monkeypatch.setattr(runtime, "_BINDING_SOURCE_SHA256", manifest["modules"][names[1]])
    monkeypatch.setattr(ctl, "__file__", str(modules / names[2]))
    monkeypatch.setattr(ctl, "_LOADED_SOURCE_SHA256", manifest["modules"][names[2]])
    monkeypatch.setattr(source, "_config", lambda value, *_: copy.deepcopy(value))
    monkeypatch.setattr(campaign, "validate_manifest", lambda _: [])  # Synthetic schema leaf.
    monkeypatch.setattr(runtime, "CoreBundle", lambda _: object())
    monkeypatch.setattr(runtime, "dependency_problems", lambda: [])
    import closer_mechanisms

    monkeypatch.setattr(closer_mechanisms, "mechanism", lambda *_a, **_k: "native-handoff/v1")
    control = {
        "active_unknown": False,
        "after": None,
        "captures": [],
        "batched": True,
        "bad_alias": None,
    }
    monkeypatch.setattr(
        native,
        "qualification_problems",
        lambda *_a, **_k: ["native active UNKNOWN"] if control["active_unknown"] else [],
    )
    monkeypatch.setattr(
        native,
        "contract",
        lambda _: (
            {"wrapper": {"sha256": sha(wrapper)}, "interpreter": {"sha256": sha(interpreter)}},
            wrapper,
            interpreter,
            native_root,
        ),
    )
    monkeypatch.setattr(campaign, "load_manifest", lambda _: copy.deepcopy(manifest))
    stable = {
        "custody_path": str(custody),
        "namespace": custody.name,
        "config_sha256": source._digest(config),
        "bootstrap_anchor_sha256": config["bootstrap_anchor_sha256"],
        "device": str(custody.stat().st_dev),
        "inode": str(custody.stat().st_ino),
    }
    monkeypatch.setattr(ctl, "inspect_provisioned_custody", lambda _: {"identity": stable})
    qualified, _ = source._qualified_policy(binding)
    context = {
        "synthetic": True,
        "coding_continuation": {},
        "runtime_manifest": str(manifest_path),
        "runtime_manifest_sha256": sha(manifest_path),
    }
    installation = object.__new__(source.SourceInstallation)
    installation._seal, installation._binding = source._SEAL, binding
    installation._value = {**qualified, "admission": context}

    def full_validation(value):
        current, _ = source._qualified_policy(binding)
        return value is installation and all(current[k] == value._value[k] for k in current)

    monkeypatch.setattr(source, "validate_source_installation", full_validation)
    monkeypatch.setattr(runtime, "binding_from_manifest", lambda _: binding)
    # All paths stop before any protected record/index read or effect.
    monkeypatch.setattr(
        ctl, "ControllerConfig", lambda **_: (_ for _ in ()).throw(PolicyReadFinished())
    )
    shell = """
def validate_admission(manifest, context):
    return _coding_record_from_context(manifest, context)
def _lifecycle_authority(manifest, cfg):
    return validate_admission(manifest, dict(cfg['native_admission']) if _batch_control['bad_alias'] == 'lifecycle_context' else cfg['native_admission'])
def admission_config(manifest, cfg, context):
    return validate_admission(dict(manifest) if _batch_control['bad_alias'] == 'admission_manifest' else manifest, context)
def result_admission_config(cfg, context, use_admission=False):
    manifest = _batch_manifest()
    if use_admission:
        return admission_config(manifest, dict(cfg) if _batch_control['bad_alias'] == 'configuration' else cfg, context)
    return validate_admission(manifest, context)
def source_b_start(installation, path):
    context, manifest = installation.admission, _batch_manifest()
    if path == 'start':
        return validate_admission(manifest, context)
    if path == 'lifecycle':
        return _lifecycle_authority(manifest, {'native_admission': context})
    return result_admission_config({}, context, path == 'admission')
def accepted_coding_worker_purpose(binding_value):
    installation, manifest = _batch_installation, _batch_manifest()
    if not _batch_control['batched']:
        _batch_full_validation(installation)
        for path in ['direct', 'start', 'lifecycle', 'result', 'admission']:
            try:
                if path == 'direct': validate_admission(manifest, installation.admission)
                else: source_b_start(installation, path)
            except _batch_stop: pass
        return
    with _batch_source.accepted_coding_validation_read(installation, inspect.currentframe()) as qualified_read:
        _batch_control['captures'].append(qualified_read)
        for path in ['direct', 'start', 'lifecycle', 'result', 'admission']:
            try:
                if path == 'direct': validate_admission(manifest, installation.admission)
                else: source_b_start(installation, path)
            except _batch_stop: pass
            if _batch_control['after'] is not None:
                _batch_control['after'](path)
"""
    exports = {
        "_batch_manifest": lambda: copy.deepcopy(manifest),
        "_batch_installation": installation,
        "_batch_control": control,
        "_batch_full_validation": full_validation,
        "_batch_source": source,
        "_batch_stop": PolicyReadFinished,
    }
    for name, value in exports.items():
        monkeypatch.setitem(native.__dict__, name, value)
    for name in [
        "validate_admission",
        "_lifecycle_authority",
        "admission_config",
        "result_admission_config",
        "source_b_start",
        "accepted_coding_worker_purpose",
    ]:
        namespace = dict(native.__dict__)
        exec(compile(shell, "<inert-native-read-path-shells>", "exec"), namespace)
        # Functions need the actual native namespace to exercise fixed global joins.
        import types

        value = types.FunctionType(
            namespace[name].__code__, native.__dict__, name, namespace[name].__defaults__
        )
        monkeypatch.setattr(native, name, value)
    return SimpleNamespace(
        binding=binding,
        installation=installation,
        manifest=manifest,
        control=control,
        state=state,
        modules=modules,
        custody=custody,
        phase=phase,
        native_root=native_root,
        manifest_path=manifest_path,
    )


def counts(world, batched):
    world.control["batched"] = batched
    functions = {
        "qualified_policy": source._qualified_policy.__code__,
        "full_qualification": campaign.qualify.__code__,
        "immutable_module_verification": campaign.verify_modules.__code__,
        "fresh_current_barrier": campaign._current_qualification_problems.__code__,
    }
    result = {name: 0 for name in functions}

    def profile(frame, event, _arg):
        if event == "call":
            for name, code in functions.items():
                if frame.f_code is code:
                    result[name] += 1

    previous = sys.getprofile()
    sys.setprofile(profile)
    try:
        native.accepted_coding_worker_purpose(world.binding)
    finally:
        sys.setprofile(previous)
    return result


def test_actual_qualification_work_reduces_and_fresh_getters_remain(world):
    old, new = counts(world, False), counts(world, True)
    assert old == {
        "qualified_policy": 6,
        "full_qualification": 6,
        "immutable_module_verification": 6,
        "fresh_current_barrier": 6,
    }
    assert new == {
        "qualified_policy": 6,
        "full_qualification": 1,
        "immutable_module_verification": 1,
        "fresh_current_barrier": 6,
    }


@pytest.mark.parametrize("field", ["release_pin", "runtime_manifest", "controller_config"])
def test_mutable_campaign_checks_are_not_cached(world, field):
    def change(path):
        if path != "direct":
            return
        value = json.loads(world.state.read_text())
        if field == "release_pin":
            value["release_pin"] = "0" * 64
        elif field == "runtime_manifest":
            value["worker_config"]["runtime_manifest"] = "/wrong"
        else:
            value["correction_controller"]["config"] = {}
        world.state.write_text(json.dumps(value))

    world.control["after"] = change
    with pytest.raises(ValueError, match="shared qualified campaign"):
        counts(world, True)


def test_native_unknown_barrier_remains_fresh(world):
    world.control["after"] = lambda _: world.control.update(active_unknown=True)
    with pytest.raises(ValueError, match="current manifest qualification"):
        counts(world, True)


@pytest.mark.parametrize("kind", ["module", "qualification", "namespace", "anchor", "phase"])
def test_immutable_dependency_mutation_is_refused(world, kind):
    path = {
        "module": world.modules / "closer_runtime_binding.py",
        "qualification": world.native_root / "native-qualification.json",
        "namespace": world.custody / "namespace.json",
        "anchor": world.custody / "highwater-0000000000000000.json",
        "phase": world.phase / "phase.json",
    }[kind]

    def change(stage):
        if stage == "direct":
            path.write_bytes(path.read_bytes())  # same bytes, changed ctime.

    world.control["after"] = change
    with pytest.raises(ValueError):
        counts(world, True)


def test_closed_batch_cannot_escape_accepted_frame(world):
    counts(world, True)
    read = world.control["captures"][0]
    assert not read.active and read.frame is None and read.pins.closed
    with pytest.raises(ValueError):
        read.check_inputs(world.binding, world.manifest)


@pytest.mark.parametrize("alias", ["lifecycle_context", "admission_manifest", "configuration"])
def test_recognized_native_chain_with_wrong_alias_refuses(world, alias):
    world.control["bad_alias"] = alias
    with pytest.raises(native.mechanisms.MechanismError, match="aliases differ"):
        counts(world, True)


def test_live_queue_deadline_still_checked_after_static_batch(world, monkeypatch):
    import threading

    import closer_queue as queue

    counts(world, True)
    operation = {"status": "submitted", "deadline": 120.0}
    clock = [119.999]
    invocation = {
        "pid": os.getpid(),
        "thread": threading.get_ident(),
        "target": "synthetic",
        "operation": operation,
        "now": lambda: clock[0],
    }
    monkeypatch.setattr(queue._callback_threads, "invocation", invocation, raising=False)
    assert queue._currently_invoking("synthetic", operation)
    clock[0] = 120.0
    assert not queue._currently_invoking("synthetic", operation)
    assert invocation["operation"] == operation  # expiry alone; no rewritten operation.


def test_unrecognized_caller_cannot_borrow(world):
    import inspect

    assert (
        native._accepted_frame_qualified_value(world.manifest, {}, inspect.currentframe()) is None
    )
