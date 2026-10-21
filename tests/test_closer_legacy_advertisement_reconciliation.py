"""Explicit no-operation advertisement reconciliation; entirely synthetic ledgers."""

from __future__ import annotations

import fcntl
import hashlib
import json
import time
from pathlib import Path

import pytest

import closer_pipeline as pipeline
import closer_queue as queue
import closer_resources as resources


@pytest.fixture
def seeded(tmp_path, monkeypatch):
    monkeypatch.setenv("HANDOFF_DIR", str(tmp_path / "handoff"))
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path / "private-state"))
    target, run_id, owner = "owner/repo#31", "b" * 64, "legacy:123:456:1000.0"
    path = tmp_path / "pipeline.json"
    snapshot = {"target": target, "kind": "pr", "revision": "rev", "head": "a" * 40}
    reason = "substantive_or_ambiguous_finding"
    native_path = path.parent / f"{path.stem}.actions" / f"{pipeline._digest(target)}.json"
    receipt = queue.run_round(
        native_path,
        [target],
        lambda _: {
            "target": target,
            "revision": "rev",
            "action": "wait",
            "eligible": False,
            "requires_coordinator": False,
            "reason": reason,
            "next_trigger": pipeline.EVIDENCE_REFUSAL_TRIGGER,
            "evidence": snapshot,
        },
        lambda *_: pytest.fail("no effect permitted"),
        deadline=time.time() + 30,
        max_workers=1,
        max_passes=1,
    )
    proof = {
        "schema": pipeline.INNER_WAIT_SCHEMA,
        "target": target,
        "revision": "rev",
        "head": snapshot["head"],
        "reason": reason,
        "next_trigger": pipeline.EVIDENCE_REFUSAL_TRIGGER,
        "inner_path": str(native_path),
        "receipt_sha256": pipeline._digest(receipt),
    }
    identity = hashlib.sha256(
        json.dumps([target, snapshot["head"], None], sort_keys=True).encode()
    ).hexdigest()
    binding = {
        "pipeline_run_id": run_id,
        "revision": "rev",
        "identity": identity,
        "work_key": "work",
    }
    row = {
        "status": "deferred",
        "stage": "act",
        "revision": "rev",
        "identity": identity,
        "work_key": "work",
        "results": {
            "prepare": {"snapshot": snapshot},
            "act": {
                "status": "deferred",
                "reason": "validated_inner_wait",
                "acceptance_authority": False,
                "next_trigger": proof["next_trigger"],
                "queue_receipt": receipt,
                "resource_evidence": {"inner_wait": proof},
            },
        },
    }
    state = pipeline._empty()
    state["runs"][run_id] = {
        "created": 1001.0,
        "deadline": time.time() + 60,
        "assignment": [target, "owner/other#667"],
        "targets": {
            target: row,
            "owner/other#667": {
                "status": "uncertain",
                "stage": "reconcile",
                "operation": {"id": "unknown"},
            },
        },
        "invalid_stop_history": [{"original": "invalid; preserve"}],
    }
    with pipeline._locked(path):
        pipeline._save(path, state)
    path.with_name(path.name + ".runlock").touch()
    root = resources.scoped_root(path)
    keys = [*resources.default_resource_keys(target), "issue:owner/repo#31"]
    with resources.legacy_reserve(
        root, keys, target=target, owner=owner, deadline=time.time() + 10, causal_binding=binding
    ):
        pass
    ads = [
        resources._key_dir(root, key) / "legacy-intent.json"
        for key in keys
        if key.split(":", 1)[0] in resources.DURABLE_KINDS
    ]
    return {
        "path": path,
        "run_id": run_id,
        "owner": owner,
        "target": target,
        "native_path": native_path,
        "ads": ads,
        "binding": binding,
        "row": row,
    }


def plan(seed):
    return resources.plan_legacy_reconciliation(
        seed["path"], **{k: seed[k] for k in ("run_id", "owner", "target")}
    )


def test_expired_admission_plans_with_canonical_assignment_target(seeded, monkeypatch):
    import closer_dispatcher

    calls = []

    def record_plan(*_args, **kwargs):
        calls.append(kwargs)
        return {"eligible": False, "reasons": ["synthetic"], "advertisements": []}

    monkeypatch.setattr(resources, "plan_legacy_reconciliation", record_plan)
    mixed = "OWNER/Repo#31"
    for ad in seeded["ads"]:
        doc = json.loads(ad.read_bytes())
        doc["target"] = mixed
        resources._atomic_write(ad, doc)
    closer_dispatcher._expired_admission_diagnostics(seeded["path"])
    assert calls
    assert calls[0]["target"] == resources.canonical_target(mixed)


@pytest.mark.parametrize("corrupt", [False, True])
def test_expired_admission_reports_exact_reconciliation_capacity(seeded, corrupt):
    import closer_dispatcher

    if corrupt:
        native = json.loads(seeded["native_path"].read_bytes())
        native["items"][seeded["target"]]["operation_history"] = [{"unknown": True}]
        seeded["native_path"].write_text(json.dumps(native))
    before = {p: p.read_bytes() for p in seeded["path"].parent.rglob("*") if p.is_file()}
    receipt = closer_dispatcher._expired_admission_diagnostics(seeded["path"])
    assert receipt["blocking_reservations"] == len(seeded["ads"])
    assert receipt["drainable_reservations"] == (0 if corrupt else len(seeded["ads"]))
    assert receipt["deadlock"] is corrupt
    if corrupt:
        assert all(
            row["reasons"] == ["native_no_operation_wait_proof_mismatch"]
            for row in receipt["reservation_recovery"]
        )
    assert all(p.read_bytes() == raw for p, raw in before.items())


def apply(seed, value):
    return resources.apply_legacy_reconciliation(
        seed["path"],
        **{k: seed[k] for k in ("run_id", "owner", "target")},
        expected_plan_sha256=value["plan_sha256"],
    )


def test_readonly_plan_and_exact_apply_preserve_all_original_ledgers(seeded):
    seed = seeded
    original = {p: p.read_bytes() for p in [seed["path"], seed["native_path"], *seed["ads"]]}
    before = set(seed["path"].parent.rglob("*"))
    value = plan(seed)
    assert value["eligible"], value["reasons"]
    assert set(seed["path"].parent.rglob("*")) == before
    assert all(p.read_bytes() == raw for p, raw in original.items())
    result = apply(seed, value)
    assert result["status"] == "retired"
    audit = json.loads(Path(result["audit_path"]).read_bytes())
    assert audit == value
    for ad, retired in zip(value["advertisements"], result["retired"]):
        assert Path(retired).read_bytes() == ad["bytes"].encode()
        assert not Path(ad["path"]).exists()
    assert seed["path"].read_bytes() == original[seed["path"]]
    assert seed["native_path"].read_bytes() == original[seed["native_path"]]
    assert audit["acceptance_authority"] is audit["global_stop_authority"] is False
    assert (
        json.loads(seed["path"].read_bytes())["runs"][seed["run_id"]]["targets"]["owner/other#667"][
            "status"
        ]
        == "uncertain"
    )


@pytest.mark.parametrize(
    "field", ["causal_binding", "pipeline_run_id", "revision", "identity", "work_key"]
)
def test_missing_historical_or_mismatched_causality_never_retires(seeded, field):
    for path in seeded["ads"]:
        value = json.loads(path.read_bytes())
        if field == "causal_binding":
            value.pop(field)
        else:
            value["causal_binding"][field] = "other"
        resources._atomic_write(path, value)
    value = plan(seeded)
    assert not value["eligible"]
    assert "missing_or_mismatched_causal_run_binding" in value["reasons"]
    with pytest.raises(resources.ResourceBlocked):
        apply(seeded, value)
    assert all(path.exists() for path in seeded["ads"])


@pytest.mark.parametrize("change", ["pipeline", "native", "advertisement"])
def test_exact_plan_cas_rejects_changed_bytes(seeded, change):
    value = plan(seeded)
    path = {
        "pipeline": seeded["path"],
        "native": seeded["native_path"],
        "advertisement": seeded["ads"][0],
    }[change]
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(resources.ResourceBlocked):
        apply(seeded, value)
    assert all(path.exists() for path in seeded["ads"])


@pytest.mark.parametrize("kind", ["runlock", "state", "native", "resource"])
def test_live_holder_not_evicted(seeded, kind):
    paths = {
        "runlock": Path(str(seeded["path"]) + ".runlock"),
        "state": Path(str(seeded["path"]) + ".lock"),
        "native": Path(str(seeded["native_path"]) + ".lock"),
        "resource": seeded["ads"][0].parent / "lock",
    }
    value = plan(seeded)
    with paths[kind].open("r+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(resources.ResourceBlocked, match="live_reconciliation_holder"):
            apply(seeded, value)
    assert all(path.exists() for path in seeded["ads"])


@pytest.mark.parametrize(
    "change", ["uncertain", "history", "receipt", "revision", "barrier", "provider", "core"]
)
def test_uncertainty_and_missing_native_proof_stay_held(seeded, change):
    if change in {"uncertain", "history", "receipt"}:
        native = json.loads(seeded["native_path"].read_bytes())
        item = native["items"][seeded["target"]]
        if change == "uncertain":
            item["operation"] = {"id": "unknown", "status": "uncertain"}
        elif change == "history":
            item["operation_history"] = [{"id": "old", "status": "completed"}]
        else:
            native["round"]["receipt"]["valid"] = False
        resources._atomic_write(seeded["native_path"], native)
    elif change == "core":
        resources._atomic_write(seeded["ads"][0].parent / "intent.json", {"status": "unknown"})
    else:
        state = json.loads(seeded["path"].read_bytes())
        row = state["runs"][seeded["run_id"]]["targets"][seeded["target"]]
        if change == "revision":
            row["revision"] = "changed"
        elif change == "barrier":
            row["recovery_barrier"] = {}
        else:
            row["provider_outcome_uncertain"] = {"run_id": "unknown"}
        resources._atomic_write(seeded["path"], state)
    value = plan(seeded)
    assert not value["eligible"]
    with pytest.raises(resources.ResourceBlocked):
        apply(seeded, value)
    assert all(path.exists() for path in seeded["ads"])


def test_audit_failure_never_retires(seeded, monkeypatch):
    value = plan(seeded)
    original = resources._fsync_dir

    def fail(directory):
        if directory.name == "reconciliation-audit":
            raise OSError("synthetic audit durability failure")
        return original(directory)

    monkeypatch.setattr(resources, "_fsync_dir", fail)
    with pytest.raises(OSError, match="audit durability"):
        apply(seeded, value)
    assert all(path.exists() for path in seeded["ads"])


def test_cli_apply_requires_explicit_plan_hash(seeded, capsys):
    args = [
        "--state",
        str(seeded["path"]),
        "--run-id",
        seeded["run_id"],
        "--owner",
        seeded["owner"],
        "--target",
        seeded["target"],
    ]
    assert resources._reconciliation_main(args) == 0
    assert json.loads(capsys.readouterr().out)["eligible"]
    with pytest.raises(SystemExit) as exc:
        resources._reconciliation_main([*args, "--apply"])
    assert exc.value.code == 2
    assert all(path.exists() for path in seeded["ads"])


def test_real_scheduler_emits_causal_identity_before_callback(tmp_path, monkeypatch):
    monkeypatch.setenv("HANDOFF_DIR", str(tmp_path / "handoff"))
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path / "private-state"))
    path, target = tmp_path / "real.json", "owner/repo#5"
    observed = []

    def act(name, context):
        state = pipeline._load(path)
        run = state["runs"][context["pipeline_run_id"]]
        row = run["targets"][name]
        for key in resources.default_resource_keys(name):
            if key.split(":", 1)[0] not in resources.DURABLE_KINDS:
                continue
            ad = json.loads(
                (
                    resources._key_dir(resources.scoped_root(path), key) / "legacy-intent.json"
                ).read_bytes()
            )
            assert ad["causal_binding"] == {
                "pipeline_run_id": context["pipeline_run_id"],
                **{field: row[field] for field in ("revision", "identity", "work_key")},
            }
            observed.append(ad)
        return {"status": "uncertain", "next_trigger": "explicit_reconcile"}

    pipeline.run_pipeline(
        path,
        [target],
        prepare=lambda *_: {"revision": "rev", "identity": "identity"},
        interpret=lambda *_: {"substantive_needs": [{"kind": "merge"}]},
        exception=lambda *_: {"reviewed": True},
        act=act,
        resource_keys=lambda *_: [],
        capacity_fn=lambda: {"io": 1, "low": 1, "exception": 1},
        deadline=time.time() + 10,
    )
    assert observed
    assert all(ad["owner"].startswith("legacy:") for ad in observed)


def test_partial_archive_crash_resumes_exact_original_audit(seeded, monkeypatch):
    value = plan(seeded)
    original = resources.os.rename
    calls = []

    def crash(source, destination):
        calls.append(source)
        if len(calls) == 2:
            raise OSError("synthetic interruption after first retirement")
        return original(source, destination)

    with monkeypatch.context() as scope:
        scope.setattr(resources.os, "rename", crash)
        with pytest.raises(OSError, match="synthetic interruption"):
            apply(seeded, value)
    assert len([p for p in seeded["ads"] if p.exists()]) == len(seeded["ads"]) - 1
    result = apply(seeded, value)
    assert len(result["retired"]) == len(seeded["ads"])
    assert json.loads(Path(result["audit_path"]).read_bytes()) == value
    assert apply(seeded, value) == result


@pytest.mark.parametrize("changed", ["native", "retired"])
def test_partial_restart_refuses_changed_proof_or_archive(seeded, monkeypatch, changed):
    value = plan(seeded)
    original = resources.os.rename
    calls = []

    def crash(source, destination):
        calls.append(destination)
        if len(calls) == 2:
            raise OSError("interrupted")
        return original(source, destination)

    with monkeypatch.context() as scope:
        scope.setattr(resources.os, "rename", crash)
        with pytest.raises(OSError):
            apply(seeded, value)
    path = seeded["native_path"] if changed == "native" else calls[0]
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(resources.ResourceBlocked):
        apply(seeded, value)
    assert any(p.exists() for p in seeded["ads"])


def test_dangling_archive_destination_is_not_overwritten(seeded):
    value = plan(seeded)
    audit_dir = (
        resources._reservation_base(resources.scoped_root(seeded["path"])).parent
        / "reconciliation-audit"
    )
    audit_dir.mkdir()
    destination = audit_dir / f"{value['plan_sha256']}-0.retired"
    destination.symlink_to(audit_dir / "absent")
    with pytest.raises(resources.ResourceBlocked, match="retirement_destination_exists"):
        apply(seeded, value)
    assert destination.is_symlink()
    assert all(p.exists() for p in seeded["ads"])


def test_foreign_legacy_advertisement_on_alias_remains_held(seeded):
    first = seeded["ads"][0]
    other = resources.scoped_root(seeded["path"]) / "reservations" / first.parent.name / first.name
    other.parent.mkdir(parents=True)
    value = json.loads(first.read_bytes())
    value["owner"] = "foreign-owner"
    resources._atomic_write(other, value)
    assert not plan(seeded)["eligible"]
    assert other.exists() and first.exists()
