"""Source-bound pre-inner-queue terminalization never retries an effect."""

import copy
import hashlib
import json
from pathlib import Path

import pytest
from test_closer_finite_reentry import TARGET, fixture, read, scope_proof

import closer_finite_reentry as retry
import closer_resources as resources


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))
    return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


@pytest.fixture
def boundary(tmp_path, monkeypatch):
    path, frontier, doc = fixture(tmp_path, stage="act", status="unknown")
    row = doc["frontiers"]["frozen"]["items"][TARGET]
    row["scope_lease"] = {"target": TARGET, "owner": "terminal", "epoch": 1}
    snapshot = {"target": TARGET, "revision": "rev", "head": "a" * 40}
    snapshot_ref = write(tmp_path / "snapshot.json", snapshot)
    row["results"] = {
        "prepare": {
            "revision": "rev",
            "identity": TARGET,
            "snapshot_ref": snapshot_ref,
            "native_effect_contract": {
                "kind": "native_remote_only",
                "target": TARGET,
                "revision": "rev",
                "head": "a" * 40,
                "action": "merge",
            },
            "proposal": {"action": "merge"},
        },
        "interpret": {
            "classification": "deterministic_merge",
            "authority": False,
            "substantive_needs": [],
        },
    }
    proof_path, proof = scope_proof(path, doc)
    row["error"] = sorted(retry.PRE_QUEUE_ERRORS)[0]
    proof.update(admitted=True, callback_started=False, reason=None, purpose="remote_effect")
    write(proof_path, proof)
    write(path, doc)
    # A tiny compiled, pinned source contract fixture; no provider/queue is invoked.
    source = (
        "def act():\n"
        "    raise RuntimeError('legacy running operation lacks external-run or live-owner proof; '"
    )
    source += "'reconcile it in the original closer queue')\n"
    namespace = {}
    exec(compile(source, "closer_dispatcher.py", "exec"), namespace)
    with pytest.raises(RuntimeError) as raised:
        namespace["act"]()
    row["error"] = f"RuntimeError: {raised.value}"
    write(path, doc)
    generation = tmp_path / "generation"
    generation.mkdir()
    module = generation / "closer_dispatcher.py"
    module.write_text(source)
    (generation / "orchestrate.sh").write_text("#!/bin/sh\n")
    script = generation / "scripts/install_verified_snapshot.py"
    script.parent.mkdir()
    script.write_text("# fixture installer entry, not executed\n")
    monkeypatch.setattr(
        retry, "PRE_QUEUE_SOURCE_SHA256", hashlib.sha256(module.read_bytes()).hexdigest()
    )
    package_digest = retry._snapshot_digest(generation)
    installation = {
        "stage": "installed",
        "candidate_sha": retry.PRE_QUEUE_CANDIDATE,
        "installed_generation": "generation-v1:fixture",
        "verification": {"verified": True, "exit_code": 0, "snapshot_digest": package_digest},
    }
    stdout = {
        "finite_frontier": frontier,
        "run_ids": {TARGET: doc["frontiers"]["frozen"]["run_id"]},
        "targets": {TARGET: copy.deepcopy(row)},
    }
    stdout_ref = write(tmp_path / "stdout.json", stdout)
    lifecycle = {
        "schema": "direct-finite-storage/v2",
        "status": "retired",
        "exit_code": 78,
        "source_identity": {
            "frontier": frontier,
            "installed_generation": "generation-v1:fixture",
            "qualified_module": str(module),
        },
        "native_stdout": stdout_ref["path"],
        "stdout_sha256": stdout_ref["sha256"],
    }
    receipts = {
        "installation": write(tmp_path / "installation.json", installation),
        "lifecycle": write(tmp_path / "lifecycle.json", lifecycle),
    }
    return path, frontier, doc, receipts, module


def plan(boundary, **kwargs):
    path, frontier, _doc, receipts, _module = boundary
    return retry.plan(
        path,
        frontier=frontier,
        target=TARGET,
        event="terminal-native-source",
        source_receipts=receipts,
        **kwargs,
    )


def apply(boundary, proposal):
    path, frontier, _doc, receipts, _module = boundary
    return retry.apply(
        path,
        frontier=frontier,
        target=TARGET,
        event="terminal-native-source",
        source_receipts=receipts,
        expected_plan_sha256=proposal["plan_sha256"],
    )


def test_native_prequeue_terminalizes_and_retires_only_original_owner(boundary):
    path, frontier, doc, _receipts, _module = boundary
    root = path.parent.parent.parent
    own = resources._key_dir(root, "pr:" + TARGET) / "intent.json"
    foreign = resources._key_dir(root, "branch:owner/repo@foreign") / "intent.json"
    sibling = resources._key_dir(root, "pr:owner/repo#99") / "intent.json"
    write(
        sibling,
        {
            "key": "pr:owner/repo#99",
            "target": "owner/repo#99",
            "owner": "terminal",
            "state": "unknown",
        },
    )
    original_sibling = sibling.read_bytes()
    write(own, {"key": "pr:" + TARGET, "target": TARGET, "owner": "terminal", "state": "unknown"})
    write(
        foreign,
        {
            "key": "branch:owner/repo@foreign",
            "target": TARGET,
            "owner": "foreign",
            "state": "unknown",
        },
    )
    original_foreign = foreign.read_bytes()
    proposal = plan(boundary)
    assert proposal["kind"] == "native_pre_queue_refusal"
    result = apply(boundary, proposal)
    assert result["status"] == "applied"
    row = read(path)
    assert row["status"] == "held" and row["stage_status"] == "completed"
    assert "reentry_ready" not in row and "error" not in row
    assert row["deferred_reason"] == "native_pre_queue_refusal"
    assert row["resource_evidence"]["confirmed_no_effect"] is True
    assert "confirmed_not_started" not in row["resource_evidence"]
    assert row["results"] == doc["frontiers"]["frozen"]["items"][TARGET]["results"]
    assert row["reentry_history"][0]["previous"] == doc["frontiers"]["frozen"]["items"][TARGET]
    assert not own.exists() and foreign.read_bytes() == original_foreign
    assert sibling.read_bytes() == original_sibling
    assert apply(boundary, proposal)["idempotent"] is True
    assert foreign.read_bytes() == original_foreign
    assert sibling.read_bytes() == original_sibling


@pytest.mark.parametrize(
    ("change", "refusal"),
    [
        ("missing", "native_pre_queue_source_receipts_required"),
        ("source", "unsupported_native_pre_queue_source"),
        ("stdout", "source_receipt_digest_changed"),
        ("installation", "unproven_terminal_native_source"),
        ("live", "unproven_terminal_native_source"),
        ("operation", "inner_operation_requires_reconciliation"),
        ("provider", "unresolved_provider_or_recovery_boundary"),
        ("spoof", "no_supported_unstarted_boundary"),
    ],
)
def test_unproven_prequeue_boundary_stays_unknown(boundary, change, refusal):
    path, _frontier, doc, receipts, module = boundary
    row = doc["frontiers"]["frozen"]["items"][TARGET]
    if change == "missing":
        receipts.clear()
    elif change == "source":
        module.write_text("raise RuntimeError('spoof')")
    elif change == "stdout":
        Path(
            json.loads(Path(receipts["lifecycle"]["path"]).read_text())["native_stdout"]
        ).write_text("{}")
    elif change == "installation":
        installation = json.loads(Path(receipts["installation"]["path"]).read_text())
        installation["verification"]["verified"] = False
        receipts["installation"] = write(Path(receipts["installation"]["path"]), installation)
    elif change == "live":
        lifecycle = json.loads(Path(receipts["lifecycle"]["path"]).read_text())
        lifecycle["status"] = "running"
        receipts["lifecycle"] = write(Path(receipts["lifecycle"]["path"]), lifecycle)
    elif change == "operation":
        inner = path.parent / f"{path.stem}.actions" / f"{retry.pipeline._digest(TARGET)}.json"
        write(
            inner,
            {
                "schema": "closer-queue/v1",
                "items": {TARGET: {"operation": {"status": "submitted"}}},
            },
        )
    elif change == "provider":
        row["provider_outcome_uncertain"] = {"run_id": "actual"}
        write(path, doc)
    else:
        row["error"] = "RuntimeError: an unrelated failure"
        write(path, doc)
    before = path.read_bytes()
    with pytest.raises(ValueError, match=rf"^{refusal}$"):
        plan(boundary)
    assert path.read_bytes() == before


def test_error_text_alone_never_qualifies(boundary):
    path, frontier, _doc, _receipts, _module = boundary
    with pytest.raises(ValueError, match="source_receipts_required"):
        retry.plan(path, frontier=frontier, target=TARGET, event="unsupported")


def test_live_lease_blocks_apply(boundary):
    path, _frontier, _doc, _receipts, _module = boundary
    proposal = plan(boundary)
    with retry._exclusive(path.parent / "lease.lock"):
        with pytest.raises(resources.ResourceBlocked, match="active_finite_writer"):
            apply(boundary, proposal)


def test_prequeue_cas_and_post_audit_retirement_resume(boundary, monkeypatch):
    path, _frontier, _doc, _receipts, _module = boundary
    proposal = plan(boundary)
    original = resources._release_known_shard_boundary
    monkeypatch.setattr(resources, "_release_known_shard_boundary", lambda *_a, **_k: False)
    with pytest.raises(ValueError, match="retirement_pending"):
        apply(boundary, proposal)
    assert read(path)["status"] == "held"
    monkeypatch.setattr(resources, "_release_known_shard_boundary", original)
    assert apply(boundary, proposal)["idempotent"] is True


def test_prequeue_cas_refuses_changed_row(boundary):
    path, _frontier, doc, _receipts, _module = boundary
    proposal = plan(boundary)
    doc["frontiers"]["frozen"]["items"][TARGET]["identity"] = "changed"
    write(path, doc)
    before = path.read_bytes()
    with pytest.raises(ValueError, match="native_stdout_does_not_bind_original_row"):
        apply(boundary, proposal)
    assert path.read_bytes() == before


def test_prequeue_crash_after_intent_resumes_once(boundary, monkeypatch):
    path, _frontier, _doc, _receipts, _module = boundary
    proposal = plan(boundary)
    save = retry.pipeline._save_snapshot
    monkeypatch.setattr(
        retry.pipeline, "_save_snapshot", lambda *_: (_ for _ in ()).throw(OSError("injected"))
    )
    with pytest.raises(OSError, match="injected"):
        apply(boundary, proposal)
    assert read(path)["status"] == "unknown"
    monkeypatch.setattr(retry.pipeline, "_save_snapshot", save)
    assert apply(boundary, proposal)["idempotent"] is True
    assert len(read(path)["reentry_history"]) == 1


@pytest.mark.parametrize("bad", [None, [], {"path": None, "sha256": "a" * 64}])
def test_malformed_receipts_fail_named(boundary, bad):
    _path, _frontier, _doc, receipts, _module = boundary
    receipts["lifecycle"] = bad
    with pytest.raises(ValueError, match="exact_source_receipt_required"):
        plan(boundary)


def test_public_cli_passes_hash_bound_source_receipts(boundary, tmp_path, capsys):
    path, frontier, _doc, receipts, _module = boundary
    frontier_file = tmp_path / "frontier.json"
    receipts_file = tmp_path / "receipts.json"
    write(frontier_file, frontier)
    write(receipts_file, receipts)
    argv = [
        "--state",
        str(path),
        "--frontier",
        str(frontier_file),
        "--target",
        TARGET,
        "--event",
        "public-cli",
        "--source-receipts-json",
        str(receipts_file),
    ]
    assert retry.main(argv) == 0
    proposal = json.loads(capsys.readouterr().out)
    assert proposal["kind"] == "native_pre_queue_refusal"
    assert retry.main(argv + ["--apply", "--expected-plan-sha256", proposal["plan_sha256"]]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "applied" and read(path)["status"] == "held"


def test_busy_original_intent_is_pending_until_idempotent_retirement(boundary):
    path, _frontier, _doc, _receipts, _module = boundary
    root = path.parent.parent.parent
    original = resources._key_dir(root, "pr:" + TARGET) / "intent.json"
    write(
        original, {"key": "pr:" + TARGET, "target": TARGET, "owner": "terminal", "state": "unknown"}
    )
    proposal = plan(boundary)
    with retry._exclusive(original.parent / "lock"):
        with pytest.raises(ValueError, match="retirement_pending"):
            apply(boundary, proposal)
        assert original.exists() and read(path)["status"] == "held"
    assert apply(boundary, proposal)["idempotent"] is True
    assert not original.exists()
