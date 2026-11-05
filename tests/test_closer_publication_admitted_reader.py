"""Pure mocked reader joins and actual custody/refusal controls, never a Root grant."""

import contextlib
import copy
import hashlib
import os
import sys
from types import SimpleNamespace

import pytest
from test_closer_coding_publication import custody as custody
from test_closer_coding_publication import values

import closer_coding_publication as pub
import closer_coding_publication_io as io
import closer_correction_controller as ctl
import closer_native_observer as native


def fixture(custody, monkeypatch):
    request = {
        "target": {"repository": "acme/repo"},
        "original_verifier": {"verification_event": {"event_id": "e"}},
        "finding": {"evidence": {"ref": "supplied"}},
        "source_issue": {"number": "1"},
        "scope": "existing",
    }
    identity = {
        "repository_id": 17,
        "epoch": "epoch",
        "effect_key": ctl.effect_key(request),
        "request_sha256": ctl.digest(request),
        "consumer_id": "coding-owner",
    }
    required = [{"id": "original test"}]
    request["required_tests"] = required
    identity["request_sha256"] = ctl.digest(request)
    admission = {
        "request": request,
        "materializer": {"raw_findings": [{"id": "original finding"}]},
        "own_instruction": {"phase_key": "p", "proof_sha256": "c" * 64},
    }
    admission_sha = ctl.digest(admission)
    intent = {"native": "instruction intent"}
    instruction_origin = {"native": "instruction result"}
    pr = values()[0]["pr"]
    readback = {
        "schema": "closer-owned-pr-readback-data/v1",
        "pr": pr,
        "commit": {"oid": pr["head_oid"]},
        "ref": {"oid": pr["head_oid"]},
        "native_publication_authorized": False,
    }
    ledger = {"state": "readback", "intent": {"source": "fixed"}, "events": []}
    origin = {
        "original": identity,
        "request_sha256": ctl.digest(request),
        "original_admission_sha256": admission_sha,
        "io_operation_sha256": "d" * 64,
        "push_ack_sha256": "e" * 64,
        "source_closure_sha256": "f" * 64,
        "intent_sha256": pub.digest(ledger["intent"]),
        "readback": readback,
        **{k: False for k in pub._AUTHORITY},
    }
    origin_sha = ctl.digest(origin)
    origin_ref = {"name": "native-publication-origin-" + origin_sha + ".json", "sha256": origin_sha}
    binding = {
        "subject": {"kind": "pull", "number": str(pr["pr_number"]), "node_id": pr["pr_node_id"]},
        "proof_sha256": origin_sha,
        "selector": {"native": "admitted"},
    }
    record = {
        "schema": io.RESULT_SCHEMA,
        "original": identity,
        "request": request,
        "findings": admission["materializer"]["raw_findings"],
        "required_tests": required,
        **{k: readback[k] for k in ("pr", "commit", "ref")},
        "provenance": {
            "publication_origin_reference": origin_ref,
            "index_binding_sha256": ctl.digest(binding),
            **{
                k: origin[k]
                for k in (
                    "original_admission_sha256",
                    "io_operation_sha256",
                    "push_ack_sha256",
                    "source_closure_sha256",
                )
            },
        },
        **{k: False for k in pub._AUTHORITY},
    }
    native_root = custody.path / "native-key"
    native_root.mkdir(mode=0o700)
    (native_root / "supervisor.key").write_bytes(b"k" * 32)
    (native_root / "supervisor.key").chmod(0o600)
    monkeypatch.setattr(native, "contract", lambda manifest: (None, None, None, native_root))
    monkeypatch.setattr(native, "binding", lambda manifest: {"source": "mock-binding"})
    origin_signature = {
        "binding": native.binding({}),
        "signature": native.mechanisms._signature(origin, b"k" * 32),
    }
    for name, value in [
        (origin_ref["name"], origin),
        ("native-publication-origin-signature-" + origin_sha + ".json", origin_signature),
        ("admission-" + admission_sha + ".json", admission),
        ("native-intent-p.json", intent),
        ("native-origin-" + "c" * 64 + ".json", instruction_origin),
    ]:
        custody.create(name, value)

    class FakeLedger:
        def __init__(self, actual):
            assert actual is custody

        def read(self, key):
            assert key == pub.stable_slot(identity)
            return copy.deepcopy(ledger)

    monkeypatch.setattr(pub, "ContractLedger", FakeLedger)
    index = {
        "data": {
            "entries": [{"key": identity["effect_key"], "request": request, "bindings": [binding]}]
        }
    }
    controller = SimpleNamespace(
        config=SimpleNamespace(
            repository="acme/repo", repository_id=17, epoch="epoch", consumers={"coding-owner": {}}
        ),
        read_bound=lambda actual: copy.deepcopy(index),
        _full_writer_validation=lambda operation, actual: None,
    )
    name = "publication-result-" + pub.digest(record) + ".json"
    custody.create(name, record)
    return record, (name, pub.digest(record), record), controller, ledger, index


def test_durable_reader_pure_join_preserves_original_scope(custody, monkeypatch):
    record, selected, controller, _, _ = fixture(custody, monkeypatch)
    data = io._confirmed_knowledge_data(controller, custody, {}, selected, {})
    assert data["schema"] == "closer-admitted-publication-knowledge/v1"
    assert data["result"]["request"] == record["request"]
    assert data["original_admission"]["materializer"]["raw_findings"] == record["findings"]
    assert all(data["result"][k] is False for k in pub._AUTHORITY)


@pytest.mark.parametrize(
    "mutation",
    ["unknown", "index", "authority", "tests", "request", "epoch", "origin", "signature"],
)
def test_reader_joins_hold_unknown_foreign_or_changed_data(custody, monkeypatch, mutation):
    record, selected, controller, ledger, index = fixture(custody, monkeypatch)
    if mutation == "unknown":
        ledger["state"] = "unknown"
    elif mutation == "index":
        index["data"]["entries"][0]["bindings"] = []
    elif mutation == "authority":
        record[next(iter(pub._AUTHORITY))] = True
    elif mutation == "tests":
        record["required_tests"] = []
    elif mutation == "request":
        record["request"] = {**record["request"], "scope": "changed"}
    elif mutation == "epoch":
        controller.config.epoch = "changed"
    elif mutation == "origin":
        record["provenance"]["push_ack_sha256"] = "0" * 64
    else:
        name = (
            "native-publication-origin-signature-"
            + record["provenance"]["publication_origin_reference"]["sha256"]
            + ".json"
        )
        value = custody.read(name)
        value["signature"] = "0" * 64
        # This is a newly mocked corruption, not an attempt to reopen real custody.
        (custody.path / name).write_bytes(ctl.canonical(value))
    with pytest.raises(pub.PublicationHeld):
        io._confirmed_knowledge_data(controller, custody, {}, selected, {})


def test_source_custody_scan_returns_only_exact_subject(custody):
    value = {"schema": io.RESULT_SCHEMA, "pr": {"repository": "acme/repo", "pr_number": 9}}
    sha = pub.digest(value)
    name = "publication-result-" + sha + ".json"
    custody.create(name, value)
    assert io._result_for_subject(custody, "acme/repo", "9") == (name, sha, value)
    assert io._result_for_subject(custody, "Acme/Repo", "9") == (name, sha, value)
    assert io._result_for_subject(custody, "acme/repo", "8") is None
    assert io._result_for_subject(custody, "other/repo", "9") is None
    other = {**value, "different": True}
    custody.create("publication-result-" + pub.digest(other) + ".json", other)
    with pytest.raises(pub.PublicationHeld, match="multiple"):
        io._result_for_subject(custody, "acme/repo", "9")


def test_scan_rejects_hash_corruption(custody):
    custody.create("publication-result-" + "a" * 64 + ".json", {"bad": "digest"})
    with pytest.raises(pub.PublicationHeld, match="custody changed"):
        io._result_for_subject(custody, "acme/repo", "9")


@pytest.mark.parametrize(
    "target,allowed",
    [("Acme/Repo#9", True), ("acme/repo#9", True), ("other/repo#9", False)],
)
def test_real_discovery_call_accepts_repository_case_only(tmp_path, monkeypatch, target, allowed):
    import closer_campaign as campaign
    import closer_dispatcher as dispatcher
    import closer_publication_verification as verification
    import closer_source_installation as installed

    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_bytes(b"{}")
    installation = SimpleNamespace(
        config={"repository": "acme/repo"},
        manifest_path=str(manifest_path),
        manifest_sha256=hashlib.sha256(b"{}").hexdigest(),
    )
    monkeypatch.setattr(installed, "source_installation_for", lambda _: installation)
    monkeypatch.setattr(io, "_consumer_source", lambda _: None)
    monkeypatch.setattr(campaign, "load_manifest", lambda _: {})
    monkeypatch.setattr(ctl, "ControllerConfig", lambda **value: SimpleNamespace(**value))
    custody_path = tmp_path / "custody"
    custody_path.mkdir()

    def custody(_):
        fd = os.open(custody_path, os.O_RDONLY)
        return SimpleNamespace(fd=fd, close=lambda: os.close(fd))

    monkeypatch.setattr(ctl, "_Custody", custody)
    backend = object.__new__(dispatcher.Closer)
    backend.runtime_binding = object()
    observation = {"target": target, "action": "wait"}
    backend.observe = lambda _: observation
    if allowed:
        assert verification.observe_existing_root(backend, target, {}) is observation
    else:
        with pytest.raises(pub.PublicationHeld, match="publication observation subject differs"):
            verification.observe_existing_root(backend, target, {})


@pytest.mark.parametrize(
    "target,allowed",
    [("Acme/Repo#9", True), ("acme/repo#9", True), ("other/repo#9", False)],
)
def test_admitted_reader_call_accepts_repository_case_only(tmp_path, monkeypatch, target, allowed):
    import closer_campaign as campaign
    import closer_dispatcher as dispatcher
    import closer_publication_verification as verification
    import closer_queue as queue
    import closer_source_installation as installed

    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_bytes(b"{}")
    installation = SimpleNamespace(
        config={"repository": "acme/repo"},
        manifest_path=str(manifest_path),
        manifest_sha256=hashlib.sha256(b"{}").hexdigest(),
    )
    monkeypatch.setattr(installed, "source_installation_for", lambda _: installation)
    monkeypatch.setattr(io, "_consumer_source", lambda _: None)
    monkeypatch.setattr(io, "_reader_frame", lambda *_: None)
    monkeypatch.setattr(campaign, "load_manifest", lambda _: {})
    monkeypatch.setattr(ctl, "ControllerConfig", lambda **value: SimpleNamespace(**value))
    custody_path = tmp_path / "custody"
    custody_path.mkdir()

    @contextlib.contextmanager
    def custody():
        fd = os.open(custody_path, os.O_RDONLY)
        try:
            yield SimpleNamespace(fd=fd)
        finally:
            os.close(fd)

    monkeypatch.setattr(
        ctl,
        "IndexController",
        lambda config, **_: SimpleNamespace(config=config, fenced=lambda _: custody()),
    )
    backend = object.__new__(dispatcher.Closer)
    backend.runtime_binding = object()
    operation = {"status": "submitted", "id": "inert-current", "deadline": 5}
    with queue._callback_invocation(target, operation, lambda: 0):
        if allowed:
            assert verification.attach_existing_root(backend, target, {}, operation, {}) is None
        else:
            with pytest.raises(pub.PublicationHeld, match="publication target differs"):
                verification.attach_existing_root(backend, target, {}, operation, {})


def test_copied_json_outside_original_callback_cannot_issue_reader(monkeypatch):
    verification = SimpleNamespace(attach_existing_root=lambda: None)
    monkeypatch.setitem(sys.modules, "closer_publication_verification", verification)
    with pytest.raises(pub.PublicationHeld, match="consumer"):
        pub.read_for_existing_root({}, "acme/repo#9", {}, {}, {})


def test_opaque_view_cannot_be_constructed_or_read_from_copied_json(monkeypatch):
    with pytest.raises(TypeError):
        io._AdmittedPublicationView()
    verification = SimpleNamespace(attach_existing_root=lambda: None)
    monkeypatch.setitem(sys.modules, "closer_publication_verification", verification)
    view = object.__new__(io._AdmittedPublicationView)
    with pytest.raises(pub.PublicationHeld, match="one-shot"):
        view.read()
