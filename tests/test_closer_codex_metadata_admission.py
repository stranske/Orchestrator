"""Read-only metadata DATA/custody controls; no live native admission is mocked as real."""

from types import SimpleNamespace

import pytest

import closer_codex_metadata as metadata
import closer_codex_metadata_admission as admitted
import closer_correction_controller as ctl


@pytest.mark.parametrize("binding", [None, {}, object()])
def test_source_scope_rejects_caller_objects_without_native_factory(binding):
    with pytest.raises(
        metadata.MetadataHeld, match="metadata_actual_runtime_factory_required"
    ), admitted.metadata_scope(binding, {}):
        pytest.fail("no scope expected")


def test_forged_or_serialized_scope_cannot_run_native_metadata():
    for value in ({"active": True}, object.__new__(admitted.MetadataAdmission)):
        with pytest.raises(metadata.MetadataHeld):
            admitted.qualify_admitted_callback(value)


def test_source_custody_is_create_only_and_private(tmp_path):
    path = tmp_path.resolve() / "metadata"
    path.mkdir(mode=0o700)
    custody = admitted._Custody(path)
    try:
        assert custody.create("same.json", {"value": 1})
        assert custody.create("same.json", {"value": 1}) is False
        with pytest.raises(metadata.MetadataHeld, match="metadata_existing_custody_differs"):
            custody.create("same.json", {"value": 2})
        assert (path / "same.json").stat().st_mode & 0o777 == 0o600
        with pytest.raises(metadata.MetadataHeld, match="metadata_custody_name"):
            custody.create("../escape.json", {})
    finally:
        __import__("os").close(custody.fd)


def data_scope(tmp_path, monkeypatch):
    """Only exercise protected format/one-shot handles, not native authority."""
    root = tmp_path.resolve() / "metadata"
    root.mkdir(mode=0o700)
    custody = admitted._Custody(root)
    scope = object.__new__(admitted.MetadataAdmission)
    spec = {
        "schema": metadata.PURPOSE,
        "binary_path": "/synthetic/codex",
        "binary_sha256": "a" * 64,
        "home": "/synthetic/home",
        "codex_home": "/synthetic/codex-home",
        "private_root": "/synthetic/private",
        "cwd": "/synthetic/workspace",
        "profile_id": "synthetic-profile",
    }
    bootstrap = {
        key: spec[key]
        for key in (
            "binary_path",
            "binary_sha256",
            "home",
            "codex_home",
            "private_root",
        )
    }
    scope.operation = {"id": "synthetic-original"}
    scope.origin = SimpleNamespace(sha256="b" * 64)
    scope.spec = spec
    scope.plan = {
        "schema": "closer-codex-initial-consumer/v1",
        "target": "example/repo#1",
        "consumer_id": "synthetic-consumer",
        "registration": {
            "profile_id": spec["profile_id"],
            "native_bootstrap": bootstrap,
        },
    }
    scope.config_pins = ()
    scope.custody = custody
    scope.live = lambda: None
    key, intent = admitted._transaction(scope)
    custody.create("intent-" + key + ".json", intent)
    record = {
        **intent,
        "schema": "closer-codex-admitted-metadata-result/v1",
        "account_sha256": "c" * 64,
        "profile_id": spec["profile_id"],
        "configuration_sha256": metadata._pin_digest(()),
        "features_sha256": "d" * 64,
        "plugins_observed_disabled": True,
        "counted_features_bytes": 100,
        "counted_metadata_bytes": 200,
        "execution_authorized": False,
        "registration_installed": False,
        "thread_starts": 0,
        "turn_starts": 0,
        "cloud_policy_lease": False,
    }
    name = "result-" + key + ".json"
    custody.create(name, record)
    ref = {"name": name, "sha256": ctl.digest(record), "execution_authorized": False}
    return scope, ref, record


def test_data_scope_retains_proposal_without_installing_registration(tmp_path, monkeypatch):
    scope, reference, _ = data_scope(tmp_path, monkeypatch)
    try:
        handle = admitted.read_admitted_account(scope, reference)
        proposal_ref = admitted.retain_admitted_initial_registration(scope, handle)
        proposal = scope.custody.read(proposal_ref["name"])
        assert proposal["registration"]["account_sha256"] == "c" * 64
        assert proposal["registration_installed"] is False
        assert proposal["execution_authorized"] is False
        assert "account_sha256" not in scope.plan["registration"]
        with pytest.raises(metadata.MetadataHeld, match="metadata_protected_reader_required"):
            admitted.retain_admitted_initial_registration(scope, handle)
    finally:
        __import__("os").close(scope.custody.fd)


@pytest.mark.parametrize(
    "field,value",
    [
        ("thread_starts", 1),
        ("thread_starts", False),
        ("turn_starts", 1),
        ("execution_authorized", True),
        ("registration_installed", True),
        ("cloud_policy_lease", True),
        ("plugins_observed_disabled", False),
        ("account_sha256", "not-a-digest"),
        ("counted_metadata_bytes", 131073),
        ("configuration_sha256", "e" * 64),
        ("source_authority", True),
    ],
)
def test_data_scope_rejects_changed_protected_result(tmp_path, monkeypatch, field, value):
    scope, reference, record = data_scope(tmp_path, monkeypatch)
    try:
        record[field] = value
        path = scope.custody.path / reference["name"]
        path.write_bytes(ctl.canonical(record))
        reference["sha256"] = ctl.digest(record)
        with pytest.raises(metadata.MetadataHeld, match="metadata_protected_result_changed"):
            admitted.read_admitted_account(scope, reference)
    finally:
        __import__("os").close(scope.custody.fd)


def test_data_scope_rejects_account_hash_prefill_and_selected_route_change(tmp_path, monkeypatch):
    scope, reference, _ = data_scope(tmp_path, monkeypatch)
    try:
        handle = admitted.read_admitted_account(scope, reference)
        scope.plan["registration"]["account_sha256"] = "e" * 64
        with pytest.raises(
            metadata.MetadataHeld,
            match="metadata_initial_registration_selection_changed",
        ):
            admitted.retain_admitted_initial_registration(scope, handle)
    finally:
        __import__("os").close(scope.custody.fd)


def test_data_scope_copied_json_does_not_become_account_handle(tmp_path, monkeypatch):
    scope, _, _ = data_scope(tmp_path, monkeypatch)
    try:
        with pytest.raises(metadata.MetadataHeld, match="metadata_protected_reader_required"):
            admitted.retain_admitted_initial_registration(scope, {"account_sha256": "c" * 64})
    finally:
        __import__("os").close(scope.custody.fd)
