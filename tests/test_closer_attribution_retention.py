"""Small offline shared-cache and source-provenance composition regressions."""

import copy
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from test_closer_verifier_attribution import world as world

import closer_attribution_store as cache
import closer_evidence as evidence
import storage_lifecycle as storage


@pytest.fixture(autouse=True)
def isolated_default_ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCH_STORAGE_STATE_DIR", str(tmp_path / "private-storage-ledger"))


def store(tmp_path, maximum=2 * 1024**2):
    root = tmp_path / "cache"
    root.mkdir(exist_ok=True)
    ledger = storage.Reservations(tmp_path / "ledger", tmp_path, floor=0, free=lambda _: 10**9)
    return cache.Store(root, maximum=maximum, ledger=ledger)


def test_equivalent_reads_deduplicate_without_new_reservations(tmp_path):
    value = store(tmp_path)
    first = value.retain(b"public synthetic inventory", "json", reference="current")
    receipts = list((value.ledger.directory / "receipts").iterdir())
    assert value.retain(b"public synthetic inventory", "json", reference="current") == first
    assert list((value.ledger.directory / "receipts").iterdir()) == receipts
    assert len(list((value.root / "raw").rglob("*.json"))) == 1


def test_new_object_bound_preserves_previous_progress_and_other_small_case(tmp_path):
    value = store(tmp_path, maximum=200000)
    first = value.retain(b"a", "json", reference="one")
    with pytest.raises(cache.AttributionStorageWait, match="ceiling"):
        value.retain(b"x" * 100000, "zip", reference="large")
    second = value.retain(b"b", "json", reference="independent")
    assert value.path(first["sha256"] + ".json").read_bytes() == b"a"
    assert value.path(second["sha256"] + ".json").read_bytes() == b"b"


@pytest.mark.parametrize("reference", ["current", "pending", "uncertain", "legacy:unresolved"])
def test_references_remain_protected_without_retirement_authority(tmp_path, reference):
    value = store(tmp_path)
    ref = value.retain(b"protected", "zip", reference=reference)
    key = ref["sha256"] + ".zip"
    with value.locked() as index:
        assert index["objects"][key]["references"] == [reference]
    assert value.path(key).exists()
    assert not hasattr(value, "retire") and not hasattr(value, "release")


def test_expired_required_last_copy_stays_protected(tmp_path):
    value = store(tmp_path)
    ref = value.retain(b"only remaining remote evidence", "zip", reference="pending", expired=True)
    with value.locked() as index:
        assert index["objects"][ref["sha256"] + ".zip"]["expired_required"] is True
    assert value.path(ref["sha256"] + ".zip").exists()


def test_recovery_material_counts_without_claiming_reclaimed_capacity(tmp_path):
    value = store(tmp_path, maximum=200000)
    ref = value.retain(b"current", "json", reference="current")
    recovery = value.control / "recovery"
    recovery.mkdir()
    preserved = recovery / "existing-recovery.archive"
    preserved.write_bytes(b"x" * 200000)
    with pytest.raises(cache.AttributionStorageWait, match="ceiling"):
        value.retain(b"new", "json", reference="new")
    assert value.retain(b"current", "json", reference="current") == ref
    assert preserved.stat().st_size == 200000


def test_legacy_unindexed_bytes_import_with_unknown_reference(tmp_path):
    value = store(tmp_path)
    data = b"existing evidence"
    key = hashlib.sha256(data).hexdigest() + ".zip"
    value.path(key).parent.mkdir(parents=True)
    value.path(key).write_bytes(data)
    value.retain(data, "zip", reference="new")
    with value.locked() as index:
        assert index["objects"][key]["references"] == ["legacy:unresolved", "new"]


def test_concurrent_registration_preserves_both_references(tmp_path):
    value = store(tmp_path)
    with ThreadPoolExecutor(max_workers=2) as workers:
        futures = [
            workers.submit(value.retain, b"concurrent", "zip", reference=name)
            for name in ("current", "pending")
        ]
        refs = [future.result() for future in futures]
    assert refs[0] == refs[1]
    with value.locked() as index:
        assert index["objects"][refs[0]["sha256"] + ".zip"]["references"] == ["current", "pending"]


def test_failed_index_publication_releases_terminal_charge_but_preserves_evidence(
    tmp_path, monkeypatch
):
    value = store(tmp_path)
    original = storage.write_json

    def interrupt(path, record):
        if path == value.control / "index.json":
            raise OSError("synthetic interruption")
        original(path, record)

    monkeypatch.setattr(storage, "write_json", interrupt)
    with pytest.raises(OSError, match="interruption"):
        value.retain(b"interrupted", "json", reference="pending")
    ledger = json.loads((value.ledger.directory / "reservations.json").read_text())
    assert not ledger["jobs"]
    assert not list((value.root / "raw").glob("**/*"))


def test_storage_admission_includes_atomic_staging_and_existing_reservations(tmp_path):
    value = store(tmp_path)
    value.ledger.free = lambda _: 1
    with pytest.raises(storage.StorageBlocked):
        value.retain(b"admission must precede publication", "json", reference="current")
    assert not list((value.root / "raw").glob("**/*"))


@pytest.mark.parametrize(
    "title,ready",
    [
        ("Validate password: at least 8 chars", False),
        ("API token: synthetic-private-value", False),
        ("password: synthetic-value must authenticate", False),
        ("Ordinary delivery title", True),
    ],
)
def test_title_transform_preserves_original_hash_and_masking(tmp_path, title, ready):
    source = {"target": "owner/repo#1", "title": title, "body": "## Tasks\n- Preserve behavior"}
    section = evidence._source_section([source], tmp_path)
    row = section["content"][0]
    assert row["title_sha256"] == hashlib.sha256(title.encode()).hexdigest()
    assert evidence._title_provenance_valid(row, source) is ready
    assert section["status"] == ("complete" if ready else "partial")
    if "synthetic" in title:
        assert "synthetic" not in json.dumps(section)
    if title.startswith("Validate"):
        assert "[REDACTED]" in row["title"]
        assert "requirements_text" not in row["title_transform"]
        assert row["title_transform"]["meaning_status"] == "unavailable"


def test_forged_title_transform_or_source_revision_never_becomes_original(tmp_path):
    source = {
        "target": "owner/repo#1",
        "title": "Validate password: at least 8 chars",
        "body": "work",
    }
    row = evidence._source_section([source], tmp_path)["content"][0]
    forged = copy.deepcopy(row)
    forged["title_transform"]["meaning_status"] = "original"
    assert not evidence._title_provenance_valid(forged, source)
    assert not evidence._title_provenance_valid(
        row, {**source, "title": "Validate password: at least 9 chars"}
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "none",
        "forged",
        "body_drift",
        "unknown_meaning",
        "freeform",
        "empty_body",
        "redacted_body",
        "truncated_body",
    ],
)
def test_title_transform_reuses_normal_action_readiness_without_acceptance(tmp_path, mutation):
    from test_closer_pre_model_readiness import code_only_packet, reseal_section

    snapshot, packet = code_only_packet(tmp_path)
    title = (
        "API token: synthetic-private-value"
        if mutation == "unknown_meaning"
        else "Validate password: at least 8 chars"
    )
    snapshot["sources"][0]["title"] = title
    if mutation == "freeform":
        snapshot["sources"][0]["body"] = "Implement the specified fallback behavior."
    elif mutation == "empty_body":
        snapshot["sources"][0]["body"] = ""
    elif mutation == "redacted_body":
        snapshot["sources"][0]["body"] = "Use password: synthetic-private-value"
    elif mutation == "truncated_body":
        snapshot["sources"][0]["body"] = "x" * (evidence.MAX_TEXT_BYTES + 1)
    packet["bindings"]["source"] = evidence._hash(snapshot["sources"])
    packet["sections"]["source"] = evidence._source_section(
        snapshot["sources"], tmp_path / "evidence"
    )
    if mutation == "forged":
        packet["sections"]["source"]["content"][0]["title_transform"][
            "requirements_text"
        ] = "Invented criterion"
    reseal_section(packet, "source", tmp_path / "evidence")
    if mutation == "body_drift":
        snapshot["sources"][0]["body"] += "\nNew actual request"
    screen = evidence.action_evidence_readiness(
        snapshot, packet, "merge", retained_dir=tmp_path / "evidence"
    )
    assert screen["ready"] is (mutation in {"none", "unknown_meaning", "freeform"})
    if screen["ready"]:
        assert screen["unresolved_title_meaning"][0]["gap"] == "source:title_meaning_unavailable"
        assert screen["unresolved_title_meaning"][0]["acceptance_authority"] is False
        postverify = evidence.action_evidence_readiness(
            snapshot, packet, "collect_evidence", retained_dir=tmp_path / "evidence"
        )
        assert postverify["ready"] is False and postverify["mode"] == "source_transform"
    assert not screen.get("acceptance_authority")


def test_quota_wait_preserves_accounting_for_a_meaningful_retry_trigger(tmp_path):
    value = store(tmp_path, maximum=100)
    with pytest.raises(cache.AttributionStorageWait) as result:
        value.retain(b"small", "json", reference="pending")
    assert result.value.accounting["maximum_bytes"] == 100
    assert result.value.accounting["prospective_bytes"] > 100


def test_model_context_omits_title_audit_from_readiness_without_losing_meaning_gap(tmp_path):
    from test_closer_pre_model_readiness import code_only_packet, reseal_section

    import closer_dispatcher

    snapshot, packet = code_only_packet(tmp_path)
    snapshot["sources"][0]["title"] = "API token: synthetic-private-value"
    packet["bindings"]["source"] = evidence._hash(snapshot["sources"])
    packet["sections"]["source"] = evidence._source_section(
        snapshot["sources"], tmp_path / "evidence"
    )
    reseal_section(packet, "source", tmp_path / "evidence")
    readiness = evidence.action_evidence_readiness(
        snapshot, packet, "merge", retained_dir=tmp_path / "evidence"
    )
    assert readiness["ready"] is True
    before_packet, before_readiness = copy.deepcopy(packet), copy.deepcopy(readiness)
    audit = readiness["unresolved_title_meaning"][0]["transform"]
    context = closer_dispatcher.data_only_evidence_context(packet, readiness=readiness)
    projected = context["readiness"]["unresolved_title_meaning"][0]
    assert projected == {
        key: value
        for key, value in readiness["unresolved_title_meaning"][0].items()
        if key != "transform"
    }
    assert projected["gap"] == "source:title_meaning_unavailable"
    assert projected["necessity"] == "useful" and projected["acceptance_authority"] is False
    assert context["readiness_projection"]["omitted_optional_fields"] == [
        "unresolved_title_meaning[].transform"
    ]
    encoded = json.dumps(context, sort_keys=True)
    assert json.dumps(audit, sort_keys=True) not in encoded

    def contains_audit(value):
        if isinstance(value, dict):
            return bool({"transform", "title_transform"} & value.keys()) or any(
                contains_audit(field) for field in value.values()
            )
        return isinstance(value, list) and any(contains_audit(field) for field in value)

    assert not contains_audit(context)
    assert packet == before_packet and readiness == before_readiness
    assert evidence.validate_packet(packet)


def test_interrupted_exact_pending_publication_can_resume_under_same_lock(tmp_path):
    value = store(tmp_path)
    data = b"resume exact bytes"
    key = hashlib.sha256(data).hexdigest() + ".json"
    path = value.path(key)
    path.parent.mkdir(parents=True)
    path.with_name(path.name + ".pending").write_bytes(data)
    assert value.retain(data, "json", reference="pending")["sha256"] == key.split(".")[0]
    assert path.read_bytes() == data


def test_native_prewrite_storage_wait_is_typed_and_does_not_post(world, monkeypatch):
    from test_closer_verifier_attribution import TARGET, validate

    import closer_verifier_boundary as boundary

    monkeypatch.setattr(boundary, "Store", lambda root: cache.Store(root, maximum=100))
    result = world.backend.initiate_verify(TARGET, world.snapshot)
    assert result["status"] == "resource_wait"
    assert result["verifier_resource_wait"]["cause"] == "storage_capacity"
    assert (
        result["verifier_resource_wait"]["observations"][-1]["endpoint"]
        == "attribution-storage-admission"
    )
    assert not world.posts and not world.downloads
    validate(result, world.snapshot)


def test_quota_during_manifest_retention_keeps_typed_wait_and_cached_zip(world, monkeypatch):
    from test_closer_verifier_attribution import TARGET, validate

    import closer_verifier_boundary as boundary

    class ManifestRefusal(cache.Store):
        def retain(self, data, suffix, **kwargs):
            if suffix == "json" and list((self.root / "raw").glob("*/*.zip")):
                raise cache.AttributionStorageWait(
                    "synthetic manifest quota", accounting={"maximum_bytes": 1}
                )
            return super().retain(data, suffix, **kwargs)

    monkeypatch.setattr(boundary, "Store", ManifestRefusal)
    result = world.backend.initiate_verify(TARGET, world.snapshot)
    assert result["verifier_resource_wait"]["cause"] == "storage_capacity"
    assert result["verifier_resource_wait"]["requirements"] == ["synthetic manifest quota"]
    assert len(world.downloads) == 1 and not world.posts
    validate(result, world.snapshot)
    monkeypatch.setattr(boundary, "Store", cache.Store)
    assert world.backend.initiate_verify(TARGET, world.snapshot)["status"] == "completed"
    assert len(world.downloads) == 1  # retained exact ZIP reused, no repeat acquisition


def test_default_ledger_uses_existing_operational_runtime(tmp_path, monkeypatch):
    monkeypatch.delenv("ORCH_STORAGE_STATE_DIR")
    monkeypatch.setenv("ORCH_LOCAL_RUNTIME", str(tmp_path / "operational-runtime"))
    value = cache.Store(tmp_path / "cache")
    assert value.ledger.directory == tmp_path / "operational-runtime" / ".storage-lifecycle"


def test_true_interruption_keeps_reservation_for_reconciliation(tmp_path, monkeypatch):
    value = store(tmp_path)
    original = storage.write_json

    def interrupt(path, record):
        if path == value.control / "index.json":
            raise KeyboardInterrupt()
        original(path, record)

    monkeypatch.setattr(storage, "write_json", interrupt)
    with pytest.raises(KeyboardInterrupt):
        value.retain(b"interrupted", "json", reference="pending")
    ledger = json.loads((value.ledger.directory / "reservations.json").read_text())
    assert any(row["status"] == "uncertain" for row in ledger["jobs"].values())


def test_known_failed_replace_keeps_measured_partial_bytes_without_dead_charge(
    tmp_path, monkeypatch
):
    value = store(tmp_path)
    original = cache.os.replace

    def fail_replace(source, target):
        if str(source).endswith(".pending"):
            raise OSError("synthetic closed-file failure")
        return original(source, target)

    monkeypatch.setattr(cache.os, "replace", fail_replace)
    before = value.resident()
    with pytest.raises(OSError, match="closed-file"):
        value.retain(b"recoverable partial bytes", "json", reference="pending")
    assert value.resident() > before
    assert not json.loads((value.ledger.directory / "reservations.json").read_text())["jobs"]
    monkeypatch.setattr(cache.os, "replace", original)
    assert value.retain(b"recoverable partial bytes", "json", reference="pending")["bytes"] == 25


@pytest.mark.parametrize("message", ["", " " * 20, "storage refused " * 100])
def test_storage_wait_requirement_is_bounded_and_always_valid(world, monkeypatch, message):
    from test_closer_verifier_attribution import TARGET, validate

    import closer_verifier_boundary as boundary

    def refuse(_root):
        raise cache.AttributionStorageWait(message)

    monkeypatch.setattr(boundary, "Store", refuse)
    result = world.backend.initiate_verify(TARGET, world.snapshot)
    requirements = result["verifier_resource_wait"]["requirements"]
    assert result["verifier_resource_wait"]["cause"] == "storage_capacity"
    assert len(requirements) == 1 and 1 <= len(requirements[0]) <= 512
    if not message.strip():
        assert requirements == ["attribution storage admission refused"]
    assert not world.posts and not world.downloads
    validate(result, world.snapshot)


def test_supplied_retention_io_failure_is_not_reported_as_missing_manifest(world, monkeypatch):
    from test_closer_verifier_attribution import TARGET, validate

    import closer_verifier_boundary as boundary

    class ManifestIOFailure(cache.Store):
        def retain(self, data, suffix, **kwargs):
            if suffix == "json" and list((self.root / "raw").glob("*/*.zip")):
                raise OSError(28, "synthetic private path must not enter the requirement")
            return super().retain(data, suffix, **kwargs)

    monkeypatch.setattr(boundary, "Store", ManifestIOFailure)
    result = world.backend.initiate_verify(TARGET, world.snapshot)
    assert result["verifier_resource_wait"]["cause"] == "storage_capacity"
    assert result["verifier_resource_wait"]["requirements"] == [
        "attribution retention I/O unavailable"
    ]
    assert len(world.downloads) == 1 and not world.posts
    validate(result, world.snapshot)
    monkeypatch.setattr(boundary, "Store", cache.Store)
    assert world.backend.initiate_verify(TARGET, world.snapshot)["status"] == "completed"
    assert len(world.downloads) == 1


def test_default_retention_io_failure_keeps_prior_archive_parser_behavior(tmp_path, monkeypatch):
    from test_closer_verifier_attribution import archive

    def fail(*_args):
        raise OSError("synthetic default retention failure")

    monkeypatch.setattr(evidence, "_retain_bytes", fail)
    assert evidence._native_manifest_from_archive(archive(), tmp_path) == []
