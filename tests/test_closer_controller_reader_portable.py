"""Loaded-reader data-source controls; no installed/native account is queried."""

import hashlib
import importlib.util
import sys
from pathlib import Path
from uuid import uuid4

import pytest

import closer_correction_controller as ctl

FIXTURE = Path(__file__).parent / "fixtures/closer_controller/inert_reader.py.txt"


@pytest.fixture
def loaded_reader(tmp_path):
    path = tmp_path / "inert_reader.py"
    path.write_bytes(FIXTURE.read_bytes())
    name = "inert_source_reader_" + uuid4().hex
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
        cls = module.SyntheticReader
        registration = dict(
            reader_entrypoint=cls.__module__ + "." + cls.__qualname__,
            reader_source_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
            profile_id="synthetic-profile",
        )
        yield module, path, registration
    finally:
        sys.modules.pop(name, None)


def test_source_matched_loaded_reader_returns_only_synthetic_account_digest(loaded_reader):
    module, _, registration = loaded_reader
    assert module.SyntheticReader().observe(registration) == ctl.digest(
        {"schema": "managed-native-account/v1", "account_id": "synthetic-account"}
    )
    assert module.CALLS == ["synthetic-read"]


@pytest.mark.parametrize("registration_uses_new_disk", [False, True])
def test_changed_disk_cannot_authorize_old_loaded_reader(loaded_reader, registration_uses_new_disk):
    module, path, registration = loaded_reader
    path.write_bytes(path.read_bytes() + b"\n# changed after class load\n")
    if registration_uses_new_disk:
        registration["reader_source_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(ctl.ControllerUnknown, match="installed consumer reader source differs"):
        module.SyntheticReader().observe(registration)
    assert module.CALLS == []


@pytest.mark.parametrize("where", ["instance", "class"])
def test_reader_method_replacement_refuses_before_any_identity_read(
    loaded_reader, monkeypatch, where
):
    module, _, registration = loaded_reader
    reader = module.SyntheticReader()
    calls = []

    def forged(*_args):
        calls.append("forged")
        return {"account_id": "forged", "profile_id": "synthetic-profile"}

    target = reader if where == "instance" else type(reader)
    monkeypatch.setattr(target, "read_native_identity", forged)
    # Caller-provided hash attributes cannot replace the source-owned definition.
    target._source_sha256 = registration["reader_source_sha256"]
    with pytest.raises(ctl.ControllerUnknown, match="installed consumer reader source differs"):
        reader.observe(registration)
    assert calls == module.CALLS == []


def test_in_place_method_code_replacement_is_not_hidden_by_same_function_object(loaded_reader):
    module, _, registration = loaded_reader
    method = module.SyntheticReader.read_native_identity
    original = method.__code__

    def replacement(self):
        return {"account_id": "forged", "profile_id": "synthetic-profile"}

    try:
        method.__code__ = replacement.__code__
        with pytest.raises(ctl.ControllerUnknown, match="installed consumer reader source differs"):
            module.SyntheticReader().observe(registration)
        assert module.CALLS == []
    finally:
        method.__code__ = original


def test_definition_pin_requires_loaded_method_to_match_declared_source(loaded_reader):
    module, _, registration = loaded_reader
    calls = []

    def forged(self):
        calls.append("forged")
        return {"account_id": "forged", "profile_id": "synthetic-profile"}

    # Claim the same class/module/method identity and the exact file hash. The
    # loaded function's executable code still does not match that source file.
    forged.__qualname__ = module.SyntheticReader.read_native_identity.__qualname__
    forged_class = type(
        "SyntheticReader",
        (ctl.NativeConsumerReader,),
        {"__module__": module.__name__, "read_native_identity": forged},
    )
    with pytest.raises(ctl.ControllerUnknown, match="installed consumer reader source differs"):
        forged_class().observe(registration)
    assert calls == module.CALLS == []


def test_default_reader_remains_unsupported_even_with_matching_source_pin():
    registration = dict(
        reader_entrypoint="closer_correction_controller.NativeConsumerReader",
        reader_source_sha256=ctl._LOADED_SOURCE_SHA256,
        profile_id="synthetic-profile",
    )
    with pytest.raises(
        ctl.ControllerUnknown, match="installed managed native account reader unsupported"
    ):
        ctl.NativeConsumerReader().observe(registration)


def test_disk_changed_before_class_definition_cannot_pin_old_module_globals(tmp_path):
    path = tmp_path / "changed_before_definition.py"
    original = FIXTURE.read_bytes()
    loaded_code = compile(original, str(path), "exec", dont_inherit=True)
    # Only module-level data changes; identity-method code itself is identical.
    changed = original.replace(b"CALLS = []", b"CALLS = ['new disk version']")
    assert changed != original
    path.write_bytes(changed)
    name = "inert_source_reader_" + uuid4().hex
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        exec(loaded_code, module.__dict__)
        registration = dict(
            reader_entrypoint=name + ".SyntheticReader",
            reader_source_sha256=hashlib.sha256(changed).hexdigest(),
            profile_id="synthetic-profile",
        )
        with pytest.raises(ctl.ControllerUnknown, match="installed consumer reader source differs"):
            module.SyntheticReader().observe(registration)
        assert module.CALLS == []
    finally:
        sys.modules.pop(name, None)


@pytest.mark.parametrize("error", [PermissionError, OSError])
def test_source_reread_error_refuses_before_identity(loaded_reader, monkeypatch, error):
    module, path, registration = loaded_reader
    original = Path.read_bytes

    def unreadable(candidate):
        if candidate == path:
            raise error("synthetic source reread failure")
        return original(candidate)

    monkeypatch.setattr(Path, "read_bytes", unreadable)
    with pytest.raises(ctl.ControllerUnknown, match="installed consumer reader source differs"):
        module.SyntheticReader().observe(registration)
    assert module.CALLS == []


def test_source_disappearing_after_file_check_refuses_before_identity(loaded_reader, monkeypatch):
    module, path, registration = loaded_reader
    original = Path.is_file

    def disappearing(candidate):
        present = original(candidate)
        if candidate == path and present:
            path.unlink()
        return present

    monkeypatch.setattr(Path, "is_file", disappearing)
    with pytest.raises(ctl.ControllerUnknown, match="installed consumer reader source differs"):
        module.SyntheticReader().observe(registration)
    assert module.CALLS == []
