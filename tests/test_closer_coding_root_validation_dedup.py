"""Root-check call graph and freshness controls, without native authority.

The real accepted-purpose reader executes. Its external installation/admission/
B-start producers are synthetic local read-only facts; these tests never issue
an installed consumer, native admission, process, provider or execution permit.
"""

import copy
import hashlib
import inspect
import json
import os
import threading
from types import SimpleNamespace

import pytest

import closer_campaign as campaign
import closer_coding_worker as worker
import closer_native_observer as native
import closer_source_installation as installed


@pytest.fixture
def facts(tmp_path, monkeypatch):
    key = "a" * 64
    reference = {
        "schema": "closer-coding-continuation-reference/v1",
        "name": "coding-continuation-" + key + ".json",
        "sha256": "b" * 64,
        "stable_key": key,
        "effect_key": "c" * 64,
        "request_sha256": "d" * 64,
        "consumer_id": "codex",
    }
    purpose = {
        "schema": worker.PURPOSE_SCHEMA,
        "consumer_id": "codex",
        "target": "o/r#9",
        "which": {"kind": "issue", "number": "9", "node_id": "I9"},
        "selected": {
            "kind": "local_controller_record",
            "record_sha256": "e" * 64,
            "seed_selector": {
                "repository": "o/r",
                "run_id": 1,
                "run_attempt": 1,
                "artifact_id": 1,
                "archive_sha256": "f" * 64,
                "member_sha256": "a" * 64,
            },
            "owner_instruction": {
                "repository": "o/r",
                "issue_number": "9",
                "issue_node_id": "I9",
                "comment_id": 1,
                "comment_node_id": "C1",
                "updated_at": "2026-10-27T00:00:00Z",
                "body_sha256": "a" * 64,
                "writer_id": 1,
            },
        },
        "expected": {"key": reference["effect_key"], "request_sha256": reference["request_sha256"]},
    }
    selector = {"worker_id": "a" * 12, "reference": reference, "purpose": purpose}
    context = {"coding_continuation": selector, "nonce": "b" * 32}
    root = tmp_path / "native"
    directory = root / context["nonce"]
    directory.mkdir(parents=True)
    (directory / "admission.json").write_text('{"synthetic":true}')
    start = {
        "path": str(directory / ("worker-b-start-" + selector["worker_id"] + ".json")),
        "sha256": "c" * 64,
    }
    source = tmp_path / "physical-source.py"
    source.write_text("value = 1\n")

    def fingerprint():
        return source.stat().st_ino, source.stat().st_ctime_ns, source.read_bytes()

    original_fingerprint = fingerprint()
    state_path = tmp_path / "campaign.json"
    state_path.write_text(
        json.dumps({"workers": [{"state": "terminal", "outcome": "scoped_continuation"}]})
    )
    binding = object()
    installation = object.__new__(installed.SourceInstallation)
    installation._binding, installation._seal = binding, installed._SEAL
    installation._value = {
        "admission": context,
        "config": {"state_path": str(state_path)},
        "manifest_path": str(tmp_path / "manifest.json"),
    }
    backend = SimpleNamespace(runtime_binding=binding)
    counts = {"full_installation": 0, "native_admission": 0, "B_start": 0}
    control = {"admission_valid": True, "start": start}

    def full_validation(value, **_kwargs):
        counts["full_installation"] += 1
        return (
            value is installation
            and value._binding is binding
            and value._seal is installed._SEAL
            and fingerprint() == original_fingerprint
        )

    def admission(_manifest, actual, *, for_effects, **_kwargs):
        counts["native_admission"] += 1
        if actual != context or not for_effects or not control["admission_valid"]:
            raise ValueError("synthetic native admission expired or changed")

    def b_start(value):
        counts["B_start"] += 1
        assert value is installation
        return copy.deepcopy(control["start"])

    monkeypatch.setattr(installed, "validate_source_installation", full_validation)
    monkeypatch.setattr(campaign, "load_manifest", lambda *_: {"policy": {}})
    monkeypatch.setattr(native, "validate_admission", admission)
    monkeypatch.setattr(native, "source_b_start", b_start)
    monkeypatch.setattr(native, "contract", lambda _: (None, None, None, root))
    accepted = {
        "schema": "closer-accepted-coding-worker-purpose/v1",
        "continuation_reference": reference,
        "purpose": purpose,
        "worker_id": selector["worker_id"],
        "admission_sha256": hashlib.sha256((directory / "admission.json").read_bytes()).hexdigest(),
        "native_b_start": start,
    }
    worker.validate_accepted_purpose(accepted)
    return SimpleNamespace(
        backend=backend,
        installation=installation,
        accepted=accepted,
        start=start,
        counts=counts,
        source=source,
        control=control,
        state=state_path,
    )


def invoke(facts, *, between=None, repeats=1):
    backend = facts.backend
    root = worker._Root(
        worker._SEAL,
        inspect.currentframe(),
        threading.get_ident(),
        os.getpid(),
        backend,
        facts.installation,
        {},
        worker._canonical(facts.accepted),
        facts.start,
    )
    for index in range(repeats):
        if index and between is not None:
            between(root)
        root.check()


def test_real_accepted_reader_retains_one_full_validation_and_B_start(facts):
    invoke(facts)
    assert facts.counts == {"full_installation": 1, "native_admission": 1, "B_start": 1}


def test_repeated_checks_do_not_cache_native_or_source_permission(facts):
    invoke(facts, repeats=2)
    assert facts.counts == {"full_installation": 2, "native_admission": 2, "B_start": 2}


@pytest.mark.parametrize("mutation", ["bytes", "inode", "ctime"])
def test_each_check_revalidates_physical_source_facts(facts, mutation):
    def change(_root):
        if mutation == "bytes":
            facts.source.write_text("value = 2\n")
        elif mutation == "inode":
            replacement = facts.source.with_suffix(".replacement")
            replacement.write_bytes(facts.source.read_bytes())
            replacement.replace(facts.source)
        else:
            os.chmod(facts.source, 0o600)

    with pytest.raises(ValueError):
        invoke(facts, between=change, repeats=2)
    assert facts.counts["full_installation"] == 2


def test_native_admission_expiration_is_freshly_refused(facts):
    def expire(_root):
        facts.control["admission_valid"] = False

    with pytest.raises(ValueError, match="admission expired or changed"):
        invoke(facts, between=expire, repeats=2)
    assert facts.counts["native_admission"] == 2
    assert facts.counts["B_start"] == 1


def test_changed_authenticated_B_start_cannot_replace_original(facts):
    def change(_root):
        facts.control["start"] = {**facts.start, "sha256": "d" * 64}

    with pytest.raises(worker.WorkerHeld, match="accepted coding purpose changed"):
        invoke(facts, between=change, repeats=2)
    assert facts.counts["B_start"] == 2


@pytest.mark.parametrize("state,outcome", [("active", "ok"), ("terminal", "unknown")])
def test_current_campaign_ownership_and_uncertainty_still_refuse(facts, state, outcome):
    facts.state.write_text(json.dumps({"workers": [{"state": state, "outcome": outcome}]}))
    with pytest.raises(worker.WorkerHeld, match="prior worker remains owned or UNKNOWN"):
        invoke(facts)


def test_prior_root_frame_cannot_be_reused(facts):
    def ended():
        backend = facts.backend
        return worker._Root(
            worker._SEAL,
            inspect.currentframe(),
            threading.get_ident(),
            os.getpid(),
            backend,
            facts.installation,
            {},
            worker._canonical(facts.accepted),
            facts.start,
        )

    with pytest.raises(worker.WorkerHeld, match="root ended"):
        ended().check()
    assert facts.counts["full_installation"] == 0


@pytest.mark.parametrize("changed", ["seal", "pid", "thread"])
def test_root_identity_guards_run_before_any_native_read(facts, changed):
    backend = facts.backend
    root = worker._Root(
        worker._SEAL,
        inspect.currentframe(),
        threading.get_ident(),
        os.getpid(),
        backend,
        facts.installation,
        {},
        worker._canonical(facts.accepted),
        facts.start,
    )
    object.__setattr__(root, changed, object() if changed == "seal" else getattr(root, changed) + 1)
    with pytest.raises(worker.WorkerHeld, match="root unavailable"):
        root.check()
    assert facts.counts == {"full_installation": 0, "native_admission": 0, "B_start": 0}


@pytest.mark.parametrize("changed", ["binding", "seal"])
def test_source_installation_binding_and_seal_are_revalidated(facts, changed):
    def change(_root):
        setattr(facts.installation, "_" + changed, object())

    with pytest.raises(ValueError):
        invoke(facts, between=change, repeats=2)
    assert facts.counts["full_installation"] == 2
