"""Legacy prewrite result proof terminalizes without replaying its inner operation."""

import copy
import hashlib
import json
from pathlib import Path

import pytest
from test_closer_finite_reentry import TARGET, read
from test_closer_prequeue_reconciliation import boundary as prequeue_boundary  # noqa: F401
from test_closer_prequeue_reconciliation import write

import closer_finite_reentry as retry
import closer_resources as resources


@pytest.fixture
def prewrite(request, monkeypatch):
    path, frontier, doc, receipts, module = request.getfixturevalue("prequeue_boundary")
    row = doc["frontiers"]["frozen"]["items"][TARGET]
    prep = row["results"]["prepare"]
    snapshot = retry._receipt(prep["snapshot_ref"])
    snapshot["merge"] = "b" * 40
    prep["snapshot_ref"] = write(Path(prep["snapshot_ref"]["path"]), snapshot)
    prep["native_effect_contract"]["action"] = "post_merge_verify"
    prep["proposal"]["action"] = "post_merge_verify"
    row["results"]["interpret"]["classification"] = "deterministic_post_merge_verify"
    row["error"] = retry.PREWRITE_ERROR
    proof_path = next((path.parent.parent.parent / "scope-admissions").glob("*/*.json"))
    proof = json.loads(proof_path.read_text())
    proof["input_sha256"] = retry._digest(row["results"])
    proof.update(admitted=True, callback_started=False, reason=None, purpose="remote_effect")
    write(proof_path, proof)
    write(path, doc)
    source = "def act():\n    return {'status': 'deferred', 'reason': 'existing or unattributable active verifier run', 'run_id': 99}\n    raise AssertionError('POST must not run')\n"
    ns = {}
    exec(compile(source, "closer_dispatcher.py", "exec"), ns)
    result = ns["act"]()
    module.write_text(source)
    monkeypatch.setattr(
        retry, "PREWRITE_SOURCE_SHA256", hashlib.sha256(module.read_bytes()).hexdigest()
    )
    for name, constant in {
        "closer_queue.py": "PREWRITE_QUEUE_SHA256",
        "closer_pipeline.py": "PREWRITE_PIPELINE_SHA256",
    }.items():
        consumer = module.parent / name
        consumer.write_text("# pinned legacy consumer fixture\n")
        monkeypatch.setattr(retry, constant, hashlib.sha256(consumer.read_bytes()).hexdigest())
    installation = retry._receipt(receipts["installation"])
    installation["candidate_sha"] = retry.PREWRITE_CANDIDATE
    installation["verification"]["snapshot_digest"] = retry._snapshot_digest(module.parent)
    receipts["installation"] = write(Path(receipts["installation"]["path"]), installation)
    lifecycle = retry._receipt(receipts["lifecycle"])
    argv = [
        "python",
        "/mirror/closer_dispatcher.py",
        "run",
        "--assignment",
        "/assignment",
        "--state",
        "/state",
        "--finite-frontier-json",
        json.dumps(frontier),
        "--request-timeout-seconds",
        "120",
        "--pipeline",
        "--apply",
        "--workers",
        "--coordinator-mode",
        "resource_scoped",
        "--coordinator-contract-pin",
        "{}",
        "--max-workers",
        "1",
    ]
    lifecycle["source_identity"]["logical_command"] = argv
    lifecycle["argv_sha256"] = hashlib.sha256(
        json.dumps([argv[0], str(module), *argv[2:]]).encode()
    ).hexdigest()
    stdout = retry._receipt(
        {"path": lifecycle["native_stdout"], "sha256": lifecycle["stdout_sha256"]}
    )
    stdout["targets"][TARGET] = copy.deepcopy(row)
    lifecycle["stdout_sha256"] = write(Path(lifecycle["native_stdout"]), stdout)["sha256"]
    receipts["lifecycle"] = write(Path(receipts["lifecycle"]["path"]), lifecycle)
    operation = {
        "id": "op",
        "action": "post_merge_verify",
        "status": "failed",
        "outcome": "deferred",
        "revision": "rev",
        "attempts": 1,
        "finite_frontier": frontier,
        "invoked_at": 110.0,
        "elapsed_seconds": 2.0,
        "binding": {"pr_target": TARGET, "head": "a" * 40, "merge": "b" * 40},
        "result": {k: v for k, v in result.items() if k != "status"},
    }
    bucket = {
        "policy": frontier,
        "items": {
            TARGET: {
                "revision": "rev",
                "deferred_reason": "operation_failed",
                "operation": operation,
                "operation_history": [],
            }
        },
        "round": {"status": "finished", "finite_frontier": frontier, "frontier_target": TARGET},
    }
    operation.update(owner="round-owner", submitted_at=109.0, request_started_at=109.5)
    bucket["items"][TARGET]["owner"] = "round-owner"
    bucket["round"].update(
        owner="round-owner",
        events=[
            {
                "kind": "operation_submitted",
                "target": TARGET,
                "action": "post_merge_verify",
                "operation_id": "op",
            }
        ],
        receipt={
            "valid": False,
            "stop_valid": False,
            "finished_at": 114.0,
            "reasons": ["eligible_without_resource_reason:" + TARGET],
            "accounting": [
                {
                    "target": TARGET,
                    "action": "post_merge_verify",
                    "eligible": True,
                    "next_trigger": "inspect_operation_failure",
                    "observation_freshness": "fresh",
                    "operation_id": "op",
                    "operation_status": "failed",
                    "reason": "operation_failed",
                    "requires_coordinator": False,
                    "revision": "rev",
                    "status": "deferred",
                }
            ],
        },
    )
    inner = path.parent / "state.actions" / (retry.pipeline._digest(TARGET) + ".json")
    write(
        inner,
        {
            "schema": "closer-queue/v1",
            "items": {},
            "frontiers": {
                "frozen": bucket,
                "historical": {"operation": {"status": "completed", "action": "merge"}},
            },
        },
    )
    return path, frontier, doc, receipts, module, inner


def plan(value):
    path, frontier, _doc, receipts, _module, _inner = value
    return retry.plan(
        path,
        frontier=frontier,
        target=TARGET,
        event="audited-native-prewrite",
        source_receipts=receipts,
    )


def apply(value, proposal):
    path, frontier, _doc, receipts, _module, _inner = value
    return retry.apply(
        path,
        frontier=frontier,
        target=TARGET,
        event="audited-native-prewrite",
        source_receipts=receipts,
        expected_plan_sha256=proposal["plan_sha256"],
    )


def test_prewrite_hold_preserves_inner_and_foreign_effects(prewrite):
    path, _frontier, doc, _receipts, _module, inner = prewrite
    root = path.parent.parent.parent
    own = resources._key_dir(root, "pr:" + TARGET) / "intent.json"
    foreign = resources._key_dir(root, "branch:owner/repo@foreign") / "intent.json"
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
    retained_inner, retained_foreign = inner.read_bytes(), foreign.read_bytes()
    proposal = plan(prewrite)
    assert proposal["kind"] == "native_prewrite_verifier_refusal"
    assert apply(prewrite, proposal)["status"] == "applied"
    row = read(path)
    assert row["status"] == "held" and row["stage_status"] == "completed"
    assert row["deferred_reason"] == "native_prewrite_verifier_refusal"
    assert row["deferred_next_trigger"] == "github-verifier-resource:owner/repo:99:terminal"
    assert row["resource_evidence"]["confirmed_no_effect"] is True
    assert "confirmed_not_started" not in row["resource_evidence"]
    assert "reentry_ready" not in row and "act" not in row["results"]
    assert row["results"] == doc["frontiers"]["frozen"]["items"][TARGET]["results"]
    assert inner.read_bytes() == retained_inner and foreign.read_bytes() == retained_foreign
    assert not own.exists()
    assert apply(prewrite, proposal)["idempotent"] is True
    assert inner.read_bytes() == retained_inner


@pytest.mark.parametrize(
    "change",
    ["postwrite", "history", "result", "target", "merge", "window", "provider", "foreign_active"],
)
def test_unproven_inner_return_is_never_terminalized(prewrite, change):
    path, _frontier, _doc, _receipts, _module, inner = prewrite
    q = json.loads(inner.read_text())
    item = q["frontiers"]["frozen"]["items"][TARGET]
    op = item["operation"]
    if change == "postwrite":
        op.update(status="uncertain", outcome="timeout")
    elif change == "history":
        item["operation_history"] = [{"status": "completed"}]
    elif change == "result":
        op["result"]["reason"] = "some other deferred result"
    elif change == "target":
        op["binding"]["pr_target"] = "owner/repo#99"
    elif change == "merge":
        op["binding"]["merge"] = "c" * 40
    elif change == "window":
        op["invoked_at"] = 1.0
    elif change == "provider":
        op["provider_outcome_uncertain"] = True
    else:
        q["frontiers"]["historical"]["operation"]["status"] = "running"
    write(inner, q)
    before = path.read_bytes()
    with pytest.raises(
        ValueError,
        match="^native_prewrite_inner_boundary_not_proven$|^inner_operation_requires_reconciliation$",
    ):
        plan(prewrite)
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "change", ["source", "command", "generation", "stdout", "scope", "missing"]
)
def test_prewrite_source_chain_is_required(prewrite, change):
    path, _frontier, _doc, receipts, module, _inner = prewrite
    if change == "source":
        module.write_text("spoof")
    elif change == "missing":
        receipts.clear()
    elif change == "scope":
        root = path.parent.parent.parent
        for p in (root / "scope-admissions").glob("*/*.json"):
            proof = json.loads(p.read_text())
            proof["admitted"] = False
            write(p, proof)
    elif change == "stdout":
        lifecycle = retry._receipt(receipts["lifecycle"])
        Path(lifecycle["native_stdout"]).write_text("{}")
    else:
        lifecycle = retry._receipt(receipts["lifecycle"])
        if change == "generation":
            lifecycle["source_identity"]["installed_generation"] = "generation-v1:foreign"
        else:
            argv = lifecycle["source_identity"]["logical_command"]
            argv.extend(["--runtime-binding", "foreign"])
            lifecycle["argv_sha256"] = hashlib.sha256(
                json.dumps([argv[0], str(module), *argv[2:]]).encode()
            ).hexdigest()
        receipts["lifecycle"] = write(Path(receipts["lifecycle"]["path"]), lifecycle)
    with pytest.raises(ValueError, match="source|command|proof"):
        plan(prewrite)
    assert read(path)["status"] == "unknown"


def test_prewrite_audit_crash_and_inner_cas(prewrite, monkeypatch):
    path, _frontier, _doc, _receipts, _module, inner = prewrite
    proposal = plan(prewrite)
    save = retry.pipeline._save_snapshot
    monkeypatch.setattr(
        retry.pipeline, "_save_snapshot", lambda *_: (_ for _ in ()).throw(OSError("crash"))
    )
    with pytest.raises(OSError, match="crash"):
        apply(prewrite, proposal)
    monkeypatch.setattr(retry.pipeline, "_save_snapshot", save)
    assert read(path)["status"] == "unknown"
    assert apply(prewrite, proposal)["idempotent"] is True
    q = json.loads(inner.read_text())
    q["changed"] = True
    write(inner, q)
    with pytest.raises(ValueError, match="reconciled_inner_queue_changed_before_retirement"):
        apply(prewrite, proposal)
    assert len(read(path)["reentry_history"]) == 1


def test_prewrite_busy_original_owner_retirement_is_recoverable(prewrite):
    path, _frontier, _doc, _receipts, _module, inner = prewrite
    root = path.parent.parent.parent
    own = resources._key_dir(root, "pr:" + TARGET) / "intent.json"
    write(own, {"key": "pr:" + TARGET, "target": TARGET, "owner": "terminal", "state": "unknown"})
    retained = inner.read_bytes()
    proposal = plan(prewrite)
    with retry._exclusive(own.parent / "lock"):
        with pytest.raises(ValueError, match="^known_native_boundary_retirement_pending$"):
            apply(prewrite, proposal)
        assert read(path)["status"] == "held" and own.exists()
    assert apply(prewrite, proposal)["idempotent"] is True
    assert not own.exists() and inner.read_bytes() == retained


@pytest.mark.parametrize("bad", [None, [], "deferred"])
def test_malformed_result_cannot_authorize_legacy_classification(prewrite, bad):
    _path, _frontier, _doc, _receipts, _module, inner = prewrite
    q = json.loads(inner.read_text())
    q["frontiers"]["frozen"]["items"][TARGET]["operation"]["result"] = bad
    write(inner, q)
    with pytest.raises(ValueError, match="^native_prewrite_inner_boundary_not_proven$"):
        plan(prewrite)


def test_public_prewrite_plan_apply_preserves_failed_operation(prewrite, tmp_path, capsys):
    path, frontier, _doc, receipts, _module, inner = prewrite
    frozen, refs = tmp_path / "frontier.json", tmp_path / "receipts.json"
    write(frozen, frontier)
    write(refs, receipts)
    argv = [
        "--state",
        str(path),
        "--frontier",
        str(frozen),
        "--target",
        TARGET,
        "--event",
        "public-prewrite",
        "--source-receipts-json",
        str(refs),
    ]
    retained = inner.read_bytes()
    assert retry.main(argv) == 0
    proposal = json.loads(capsys.readouterr().out)
    assert proposal["kind"] == "native_prewrite_verifier_refusal"
    assert retry.main(argv + ["--apply", "--expected-plan-sha256", proposal["plan_sha256"]]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "applied"
    assert inner.read_bytes() == retained and read(path)["status"] == "held"


@pytest.mark.parametrize("change", ["intent_window", "event", "receipt", "owner"])
def test_prewrite_intent_and_terminal_receipt_are_exact(prewrite, change):
    _path, _frontier, _doc, _receipts, _module, inner = prewrite
    q = json.loads(inner.read_text())
    bucket = q["frontiers"]["frozen"]
    if change == "intent_window":
        bucket["items"][TARGET]["operation"]["submitted_at"] = 99.0
    elif change == "event":
        bucket["round"]["events"][0]["operation_id"] = "foreign"
    elif change == "receipt":
        bucket["round"]["receipt"]["reasons"].append("some_other_failure")
    else:
        bucket["round"]["owner"] = "foreign"
    write(inner, q)
    with pytest.raises(ValueError, match="^native_prewrite_intent_receipt_not_proven$"):
        plan(prewrite)


TERMINAL_CANDIDATE = "8f31a1d1136cb8d27fe2b4bce0eab9a9b4c75a3e"
SUBJECT = "owner/repo#848"
TERMINAL_REASON = "collect existing verifier result before spending again"


@pytest.fixture
def terminal_prewrite(prewrite, monkeypatch):
    path, frontier, doc, receipts, module, inner = prewrite
    row = doc["frontiers"]["frozen"]["items"][TARGET]
    prep = row["results"]["prepare"]
    snapshot = retry._receipt(prep["snapshot_ref"])
    snapshot.update(
        pr_target=SUBJECT,
        kind="pr",
        state="MERGED",
        sources=[{"target": TARGET, "kind": "issue", "body": "Original request", "state": "open"}],
    )
    prep["snapshot_ref"] = write(Path(prep["snapshot_ref"]["path"]), snapshot)
    # Production PREPARE also carries a reduced summary lacking subject/merge.
    # Only the hash-bound full snapshot may supply those fields.
    prep["snapshot"] = {"target": TARGET, "kind": "pr", "head": "a" * 40}
    q = json.loads(inner.read_text())
    operation = q["frontiers"]["frozen"]["items"][TARGET]["operation"]
    operation["binding"]["pr_target"] = SUBJECT
    operation["result"] = {"reason": TERMINAL_REASON, "run_id": 39672760878}
    write(inner, q)
    source = "def act():\n    return {'status': 'deferred', 'reason': 'collect existing verifier result before spending again', 'run_id': 39672760878}\n    raise AssertionError('POST must not run')\n"
    namespace = {}
    exec(compile(source, "closer_dispatcher.py", "exec"), namespace)
    assert namespace["act"]()["status"] == "deferred"
    module.write_text(source)
    for filename, constant in {
        "closer_dispatcher.py": "PREWRITE_TERMINAL_SOURCE_SHA256",
        "closer_queue.py": "PREWRITE_TERMINAL_QUEUE_SHA256",
        "closer_pipeline.py": "PREWRITE_TERMINAL_PIPELINE_SHA256",
    }.items():
        monkeypatch.setattr(
            retry,
            constant,
            hashlib.sha256((module.parent / filename).read_bytes()).hexdigest(),
            raising=False,
        )
    installation = retry._receipt(receipts["installation"])
    installation["candidate_sha"] = TERMINAL_CANDIDATE
    installation["verification"]["snapshot_digest"] = retry._snapshot_digest(module.parent)
    receipts["installation"] = write(Path(receipts["installation"]["path"]), installation)
    proof_path = next((path.parent.parent.parent / "scope-admissions").glob("*/*.json"))
    proof = json.loads(proof_path.read_text())
    proof["input_sha256"] = retry._digest(row["results"])
    write(proof_path, proof)
    write(path, doc)
    lifecycle = retry._receipt(receipts["lifecycle"])
    stdout = retry._receipt(
        {"path": lifecycle["native_stdout"], "sha256": lifecycle["stdout_sha256"]}
    )
    stdout["targets"][TARGET] = copy.deepcopy(row)
    lifecycle["stdout_sha256"] = write(Path(lifecycle["native_stdout"]), stdout)["sha256"]
    receipts["lifecycle"] = write(Path(receipts["lifecycle"]["path"]), lifecycle)
    return path, frontier, doc, receipts, module, inner


def test_terminal_result_alias_public_classification_never_replays(terminal_prewrite):
    path, _frontier, doc, _receipts, _module, inner = terminal_prewrite
    retained = inner.read_bytes()
    proposal = plan(terminal_prewrite)
    assert proposal["kind"] == "native_prewrite_verifier_refusal"
    assert apply(terminal_prewrite, proposal)["status"] == "applied"
    row = read(path)
    assert row["status"] == "held" and row["stage_status"] == "completed"
    assert row["resource_evidence"]["confirmed_no_effect"] is True
    assert "reentry_ready" not in row and "act" not in row["results"]
    assert inner.read_bytes() == retained
    assert row["results"] == doc["frontiers"]["frozen"]["items"][TARGET]["results"]
    assert (
        row["deferred_next_trigger"]
        == f"github-verifier-result:{SUBJECT}:39672760878:identity-reconciled"
    )


@pytest.mark.parametrize(
    "change",
    [
        "source",
        "queue",
        "pipeline",
        "generation",
        "candidate",
        "subject",
        "source_association",
        "result",
        "postwrite",
        "receipt",
        "owner",
        "foreign_uncertain",
    ],
)
def test_terminal_result_refusal_requires_exact_complete_proof(terminal_prewrite, change):
    path, _frontier, _doc, receipts, module, inner = terminal_prewrite
    before = path.read_bytes()
    q = json.loads(inner.read_text())
    bucket = q["frontiers"]["frozen"]
    op = bucket["items"][TARGET]["operation"]
    if change in {"source", "queue", "pipeline"}:
        name = {
            "source": "closer_dispatcher.py",
            "queue": "closer_queue.py",
            "pipeline": "closer_pipeline.py",
        }[change]
        (module.parent / name).write_text("# source changed after qualification\n")
    elif change in {"generation", "candidate"}:
        install = retry._receipt(receipts["installation"])
        install["installed_generation" if change == "generation" else "candidate_sha"] = "foreign"
        receipts["installation"] = write(Path(receipts["installation"]["path"]), install)
    elif change == "subject":
        op["binding"]["pr_target"] = "owner/repo#999"
    elif change == "source_association":
        row = read(path)
        ref = row["results"]["prepare"]["snapshot_ref"]
        snapshot = retry._receipt(ref)
        snapshot["sources"] = []
        # Mutating the retained snapshot cannot retain its content-addressed proof.
        Path(ref["path"]).write_text(json.dumps(snapshot))
    elif change == "result":
        op["result"]["reason"] = "verification state changed; reuse existing request"
    elif change == "postwrite":
        op.update(status="uncertain", outcome="timeout")
        op["result"]["label_posted"] = True
    elif change == "receipt":
        bucket["round"]["receipt"]["accounting"][0]["operation_id"] = "foreign"
    elif change == "owner":
        bucket["round"]["owner"] = "foreign"
    else:
        q["frontiers"]["historical"]["operation"].update(status="unknown", outcome_uncertain=True)
    if change in {"subject", "result", "postwrite", "receipt", "owner", "foreign_uncertain"}:
        write(inner, q)
    retained_inner = inner.read_bytes()
    with pytest.raises(ValueError):
        plan(terminal_prewrite)
    assert path.read_bytes() == before
    assert inner.read_bytes() == retained_inner


def test_new_source_cannot_reclassify_old_active_reason(terminal_prewrite):
    _path, _frontier, _doc, _receipts, _module, inner = terminal_prewrite
    q = json.loads(inner.read_text())
    q["frontiers"]["frozen"]["items"][TARGET]["operation"]["result"][
        "reason"
    ] = "existing or unattributable active verifier run"
    write(inner, q)
    with pytest.raises(ValueError, match="native_prewrite_inner_boundary_not_proven"):
        plan(terminal_prewrite)


def test_terminal_result_plan_is_cas_bound_and_idempotent(terminal_prewrite):
    path, _frontier, _doc, _receipts, _module, inner = terminal_prewrite
    proposal = plan(terminal_prewrite)
    original = inner.read_bytes()
    q = json.loads(original)
    q["frontiers"]["frozen"]["items"][TARGET]["operation"]["result"]["run_id"] += 1
    write(inner, q)
    with pytest.raises(ValueError, match="reentry_plan_changed"):
        apply(terminal_prewrite, proposal)
    assert read(path)["status"] == "unknown"
    inner.write_bytes(original)
    assert apply(terminal_prewrite, proposal)["status"] == "applied"
    assert apply(terminal_prewrite, proposal)["idempotent"] is True
    assert inner.read_bytes() == original
