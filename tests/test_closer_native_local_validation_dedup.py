"""Count real nested callers with inert native/history leaves; no issued authority."""

import copy
import inspect
import os
import signal
import sys
import threading
from types import SimpleNamespace

import pytest

import closer_coding_publication as publication
import closer_coding_worker as worker
import closer_correction_controller as ctl
import closer_mechanisms as mechanisms
import closer_native_observer as native
import closer_queue as queue

pytest_plugins = ("test_closer_native_observer",)


@pytest.fixture
def qualified(host):
    manifest, _, wrapper = host
    root = native.contract(manifest)[3]
    mechanisms._key(root, create=True)
    nonce = "a" * 32
    directory = root / nonce
    directory.mkdir()
    names = {
        "startup": "startup.json",
        "handoff": "child-journal.json",
        "crash_restart": "crash.json",
        "result_recording": "child-result.json",
    }
    bound = native.binding(manifest)
    for name in names.values():
        native._write(
            directory / name,
            (
                {"exit": -signal.SIGKILL}
                if name == "crash.json"
                else {"binding": bound, "nonce": nonce, "no_effect": True}
            ),
        )
    record = {
        "schema": native.OBSERVATION,
        "kind": "native-wrapper-qualification",
        "binding": bound,
        "passed": True,
        "desktop_qualified": False,
        "source": "owned-native-wrapper-process",
        "actor": "cursor",
        "scheduler_origin": "unobserved",
        "nonce": nonce,
        "phases": {
            phase: {"file": name, "sha256": native._hash(directory / name)}
            for phase, name in names.items()
        },
    }
    native._signed(root / "native-qualification.json", record, root)
    native._write(root / "native-active.json", {"status": "terminal"})
    return manifest, wrapper, root, directory


def test_real_native_barrier_validates_contract_once(qualified):
    manifest, wrapper, _, _ = qualified
    counts = {"contract": 0, "interpreter_hash": 0, "wrapper_hash": 0}

    def profile(frame, event, _):
        if event != "call":
            return
        if frame.f_code is native.contract.__code__:
            counts["contract"] += 1
        if frame.f_code is native._hash.__code__:
            path = str(frame.f_locals["path"])
            if path == manifest["mechanism_contract"]["interpreter"]["path"]:
                counts["interpreter_hash"] += 1
            if path == str(wrapper):
                counts["wrapper_hash"] += 1

    previous = sys.getprofile()
    sys.setprofile(profile)
    try:
        assert native.qualification_problems(manifest) == []
    finally:
        sys.setprofile(previous)
    assert counts == {"contract": 1, "interpreter_hash": 1, "wrapper_hash": 1}


@pytest.mark.parametrize(
    "change", ["wrapper", "signature", "phase", "unknown", "binding", "second_call"]
)
def test_native_barrier_mutations_still_refuse(qualified, change):
    manifest, wrapper, root, directory = qualified
    assert native.qualification_problems(manifest) == []
    if change == "wrapper":
        wrapper.write_text(wrapper.read_text() + "#changed\n")
    elif change == "signature":
        row = native._read(root / "native-qualification.json")
        row["signature"] = "0" * 64
        native._write(root / "native-qualification.json", row)
    elif change == "phase":
        native._write(directory / "startup.json", {"changed": True})
    elif change == "unknown":
        native._write(root / "native-active.json", {"status": "submitted", "nonce": "b" * 32})
    elif change == "binding":
        manifest["package_digest"] = "0" * 64
    else:
        native._write(directory / "crash.json", {"exit": 0})
    assert native.qualification_problems(manifest)


@pytest.fixture
def custody_world(monkeypatch):
    counts = {"root": 0, "queue": 0, "writer": 0, "record": 0, "native_history": 0, "full": 0}
    control = {
        "expired": False,
        "wrong_record": False,
        "wrong_history": False,
        "root_refuse_at": None,
    }
    reference = {"schema": "inert-reference", "sha256": "a" * 64}
    record = {"request": {"criterion": "inert"}, "source_record_ref": reference}
    history = {
        "schema": "closer-coding-original-history/v1",
        "continuation_reference": reference,
        "start": {},
        "terminal": {},
        "terminal_ref": {},
        "original_config": {},
        "operation_link": {},
        "enrollment_boundary": {},
        "instruction_intent": {
            "request": record["request"],
            "source_record_ref": reference,
            "phase": "instruction_publication",
        },
        "admission_sha256": ctl.digest(record),
    }
    purpose = {"target": "o/r#1", "selected": {"record_sha256": "b" * 64}}
    installation = SimpleNamespace(config={"state_path": "/inert/not-read"})
    root = worker._Root(
        worker._SEAL,
        None,
        threading.get_ident(),
        os.getpid(),
        None,
        installation,
        {},
        worker._canonical({"purpose": purpose, "continuation_reference": reference}),
        {},
    )
    controller = object.__new__(ctl.IndexController)
    controller.config = SimpleNamespace(state_path="/inert/not-read")
    custody = object.__new__(ctl._Custody)
    operation = {"deadline": 10**20, "status": "submitted"}

    def root_check(self):
        counts["root"] += 1
        if control["root_refuse_at"] == counts["root"]:
            raise ValueError("fresh root refused")

    def queue_check(*_):
        counts["queue"] += 1
        return not control["expired"]

    def require_writer(*_):
        counts["writer"] += 1

    def read(*_):
        counts["record"] += 1
        return {} if control["wrong_record"] else copy.deepcopy(record)

    def native_history(*_):
        counts["native_history"] += 1
        value = copy.deepcopy(history)
        if control["wrong_history"]:
            value["admission_sha256"] = "0" * 64
        return value

    def full(*_):
        counts["full"] += 1

    monkeypatch.setattr(worker._Root, "check", root_check)
    monkeypatch.setattr(queue, "_currently_invoking", queue_check)
    monkeypatch.setattr(ctl.IndexController, "_require_writer", require_writer)
    monkeypatch.setattr(ctl.IndexController, "_full_writer_validation", full)
    monkeypatch.setattr(ctl.IndexController, "_physical_state", staticmethod(lambda *_: {}))
    monkeypatch.setattr(ctl, "ControllerConfig", lambda **_: controller.config)
    monkeypatch.setattr(ctl._Custody, "read", read)
    monkeypatch.setattr(native, "_coding_original_history_knowledge", native_history)
    monkeypatch.setattr(worker, "_native_reader", lambda name: getattr(native, name))
    monkeypatch.setattr(publication, "_require_source_pin", lambda *_: None)
    continuation = worker._Continuation(
        worker._SEAL, root, controller, custody, operation, record, history, threading.get_ident()
    )
    monkeypatch.setattr(worker._CURRENT, "continuation", continuation, raising=False)

    def invoke_source_callback(root, controller, custody, operation):
        publication._live_worker_custody(
            root, controller, custody, operation, inspect.currentframe()
        )

    # Inert actual-module frame only: public helper's identity/alias checks execute.
    import types

    callback = types.FunctionType(invoke_source_callback.__code__, worker.__dict__)
    monkeypatch.setattr(worker, "_execute", callback)
    monkeypatch.setitem(worker.__dict__, "publication", publication)
    return SimpleNamespace(
        counts=counts,
        control=control,
        invoke=lambda: callback(root, controller, custody, operation),
    )


def test_real_nested_custody_callers_validate_root_three_times(custody_world):
    custody_world.invoke()
    assert custody_world.counts == {
        "root": 3,
        "queue": 1,
        "writer": 1,
        "record": 1,
        "native_history": 1,
        "full": 1,
    }


@pytest.mark.parametrize("change", ["expired", "wrong_record", "wrong_history"])
def test_fresh_custody_mutations_still_refuse(custody_world, change):
    custody_world.control[change] = True
    with pytest.raises(ValueError):
        custody_world.invoke()
    assert custody_world.counts["full"] == 0


@pytest.mark.parametrize("boundary", [1, 2, 3])
def test_each_remaining_root_boundary_stays_fresh(custody_world, boundary):
    custody_world.control["root_refuse_at"] = boundary
    with pytest.raises(ValueError, match="fresh root refused"):
        custody_world.invoke()
    assert custody_world.counts["full"] == 0


@pytest.mark.parametrize("file_kind", ["wrapper", "policy"])
def test_source_mutation_during_receipt_read_is_refused(qualified, monkeypatch, file_kind):
    manifest, wrapper, root, _ = qualified
    path = (
        wrapper
        if file_kind == "wrapper"
        else __import__("pathlib").Path(next(iter(manifest["policy"]["toml_sha256"])))
    )
    original = native._read

    def changed_read(file):
        value = original(file)
        if file == root / "native-qualification.json":
            path.write_bytes(path.read_bytes())  # Same bytes; source ctime changed within one call.
        return value

    monkeypatch.setattr(native, "_read", changed_read)
    assert native.qualification_problems(manifest)


def test_public_binding_still_performs_fresh_contract_validation(qualified):
    manifest, wrapper, _, _ = qualified
    assert native.binding(manifest)
    wrapper.write_text(wrapper.read_text() + "#changed\n")
    with pytest.raises(mechanisms.MechanismError):
        native.binding(manifest)
