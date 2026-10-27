"""Offline refusal contracts: a proved non-action wait never grants acceptance.

Synthetic owner/repo references are deliberate. No live ledgers, providers, or
GitHub are read by this regression suite; actual-receipt reconciliation is a
separate, explicitly copied diagnostic.
"""

from __future__ import annotations

import copy
import hashlib
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from closer_inert_dispatcher_support import InertCaller
from test_closer_dispatcher import FakeGitHub, closer
from test_closer_disposition import TARGET, fixture
from test_dispatcher_controlled_data_only import world as _controlled_world

import claims
import closer_dispatcher as adapter
import closer_disposition
import closer_pipeline as pipeline
import closer_queue as queue

controlled_world = _controlled_world


@pytest.fixture
def inert_caller(controlled_world, monkeypatch):
    return InertCaller(controlled_world, monkeypatch)


def retain_snapshot(prepared, snapshot, root):
    path = root / "original-act-snapshot.json"
    path.write_text(json.dumps(snapshot, sort_keys=True, separators=(",", ":")))
    prepared["snapshot_ref"] = {
        "path": str(path),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


REASONS = ("substantive_or_ambiguous_finding", "source_coverage_incomplete")
TRIGGER = "source owner supplies current-head proof or resolves the named finding"


@pytest.fixture(autouse=True)
def offline_only(monkeypatch, tmp_path):
    monkeypatch.setenv("HANDOFF_DIR", str(tmp_path / "handoff"))
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path / "brain"))
    for method in ("claim", "release", "update_metadata"):
        monkeypatch.setattr(claims, method, lambda *a, **kw: True)
    monkeypatch.setattr(claims, "holder", lambda *a, **kw: None)
    monkeypatch.setattr(adapter.gh_capacity, "throttle", lambda *a, **kw: {"action": "allow"})

    def forbidden(*a, **kw):
        pytest.fail("external provider/subprocess/GitHub transport forbidden")

    monkeypatch.setattr(adapter.subprocess, "run", forbidden)
    monkeypatch.setattr(adapter.subprocess, "Popen", forbidden)
    monkeypatch.setattr(adapter.gh_capacity, "gh_run", forbidden)


def native_refusal(tmp_path, reason):
    target = "owner/repo#31"
    path = tmp_path / "pipeline.json"
    snapshot = {"target": target, "kind": "pr", "revision": "refusal-rev", "head": "a" * 40}
    inner_path = path.parent / f"{path.stem}.actions" / f"{pipeline._digest(target)}.json"
    receipt = queue.run_round(
        inner_path,
        [target],
        lambda _: {
            "target": target,
            "revision": snapshot["revision"],
            "action": "wait",
            "eligible": False,
            "requires_coordinator": False,
            "reason": reason,
            "next_trigger": TRIGGER,
            "evidence": copy.deepcopy(snapshot),
        },
        lambda *_: pytest.fail("a refusal cannot invoke a guarded action"),
        deadline=time.time() + 30,
        max_workers=1,
        max_passes=1,
    )
    proof = {
        "schema": pipeline.INNER_WAIT_SCHEMA,
        "target": target,
        "revision": snapshot["revision"],
        "head": snapshot["head"],
        "reason": reason,
        "next_trigger": TRIGGER,
        "inner_path": str(inner_path),
        "receipt_sha256": pipeline._digest(receipt),
    }
    return target, path, snapshot, receipt, proof


@pytest.mark.parametrize("reason", REASONS)
def test_real_adapter_refusal_persists_and_restart_has_no_offload_or_action(
    tmp_path, monkeypatch, reason, inert_caller
):
    # Produce each reason through the real original-criteria integrator, then use
    # the actual adapter act callback, native queue and outer pipeline together.
    snap, _packet, ref, prep, interpreted = fixture(tmp_path / "packet")
    # An unresolved acceptance finding must hold post-verifier source closure;
    # it is no longer a reason to route an otherwise green premerge PR to a model.
    import json

    import verifier_evidence

    merge = "d" * 40
    marker = {
        "schema": verifier_evidence.MARKER,
        "repo": "owner/repo",
        "pr": 12,
        "head_sha": snap["head"],
        "evaluated_sha": merge,
        "run_id": "34",
        "run_attempt": "1",
        "provider_verdicts": ["PASS"],
        "verdict": "PASS",
        "ci_failed": False,
    }
    native_pass = verifier_evidence.decision_from_pr(
        "owner/repo",
        {
            "number": 12,
            "state": "MERGED",
            "headRefOid": snap["head"],
            "mergeCommit": {"oid": merge},
            "comments": {
                "nodes": [
                    {
                        "author": {"login": "github-actions[bot]"},
                        "url": "https://github.com/owner/repo/pull/12#issuecomment-34",
                        "body": "<!-- "
                        + verifier_evidence.MARKER
                        + " "
                        + json.dumps(marker)
                        + " -->",
                    }
                ]
            },
        },
    )
    assert native_pass and native_pass["evaluated_sha"] == merge
    snap.update(
        state="MERGED",
        merge=merge,
        verifier=native_pass,
        draft=False,
        mergeable="MERGEABLE",
        labels=[],
        active_threads=0,
    )
    if reason == "substantive_or_ambiguous_finding":
        interpreted["proposal"].update(
            classification="uncertain", substantive_needs=["Original criterion is ambiguous"]
        )
    else:
        interpreted["proposal"]["decision"]["criteria"] = []
    interpreted = inert_caller.interpretation(snap, _packet, ref, interpreted["proposal"])
    integration = closer_disposition.integrate_ordinary(TARGET, snap, ref, prep, interpreted)
    assert integration["reason"] == reason
    assert integration["status"] in {"exception", "wait"}
    assert not integration.get("disposition")
    prep = {
        **prep,
        "identity": TARGET,
        "snapshot": snap,
        "packet_ref": ref,
        "proposal": {"action": "collect_evidence", "eligible": True},
        "semantic_cache_key": "closer-evidence-semantic/v1:" + "5" * 64,
    }
    monkeypatch.setattr(adapter, "GitHub", lambda **kw: FakeGitHub(snap))
    real_run = pipeline.run_pipeline
    path = adapter._pipeline_state_path(tmp_path / "state.json")
    reads = []

    def capture(_path, _targets, **callbacks):
        return callbacks

    def adapter_callbacks():
        retain_snapshot(prep, snap, tmp_path)
        backend = closer(tmp_path, snap, apply=True, workers=True)
        monkeypatch.setattr(backend, "campaign_admission", lambda *a, **kw: {"allowed": True})
        with monkeypatch.context() as scope:
            scope.setattr(pipeline, "run_pipeline", capture)
            result = adapter._run_staged_pipeline(
                SimpleNamespace(state=str(tmp_path / "state.json"), shadow=False, max_workers=1),
                [TARGET],
                backend,
                time.time() + 300,
            )
        return result, backend

    callbacks, backend = adapter_callbacks()

    def retained_assessment(*_):
        reads.append("retained completed assessment")
        return {**interpreted, "substantive_needs": []}

    def run(callbacks, interpretation):
        return real_run(
            path,
            [TARGET],
            prepare=lambda *_: copy.deepcopy(prep),
            interpret=interpretation,
            exception=lambda *_: pytest.fail("retained assessment needs no new provider"),
            act=callbacks["act"],
            reconcile=callbacks["reconcile"],
            capacity_fn=lambda: {"io": 1, "low": 1, "exception": 1},
            deadline=time.time() + 10,
        )

    first = run(callbacks, retained_assessment)
    row = first["targets"][TARGET]
    assert row["status"] == "deferred"
    assert row["deferred_reason"] == "validated_inner_wait"
    assert row["results"]["act"]["acceptance_authority"] is False
    assert pipeline.validate_stop(path, [TARGET])["stop_valid"]
    inner_path = path.parent / f"{path.stem}.actions" / f"{pipeline._digest(TARGET)}.json"
    original_inner = queue._read_state(inner_path)
    assert original_inner["items"][TARGET].get("operation") is None
    assert original_inner["items"][TARGET].get("operation_history", []) == []
    assert original_inner["round"]["receipt"]["progress"] == []
    assert not backend.dispositions

    # Rebuild the adapter/backend, so continuation cannot rely on an in-memory
    # advisory. Its real interpreter must find the durable source-bound wait.
    # A changed semantic cache key forces the real interpreter callback rather
    # than allowing the previous assessment cache to hide a provider replay.
    prep["semantic_cache_key"] = "closer-evidence-semantic/v1:" + "6" * 64
    restarted, fresh_backend = adapter_callbacks()
    second = run(restarted, restarted["interpret"])
    assert second["targets"][TARGET]["deferred_reason"] == "validated_inner_wait"
    assert pipeline.validate_stop(path, [TARGET])["stop_valid"]
    assert reads == ["retained completed assessment"]
    after = queue._read_state(inner_path)
    assert after["items"][TARGET].get("operation") is None
    assert after["items"][TARGET].get("operation_history", []) == []
    assert after["round"]["receipt"]["progress"] == []
    assert not fresh_backend.dispositions
    assert not backend.gh.calls and not fresh_backend.gh.calls


@pytest.mark.parametrize("reason", REASONS)
@pytest.mark.parametrize(
    "tamper",
    [
        None,
        "head",
        "revision",
        "hash",
        "unknown_reason",
        "trigger",
        "operation",
        "completed_operation",
        "operation_history",
        "progress",
        "eligible_remaining",
        "receipt",
    ],
)
def test_refusal_proof_is_exact_and_has_no_operation(tmp_path, reason, tamper):
    target, path, snapshot, receipt, proof = native_refusal(tmp_path, reason)
    original = queue._read_state(Path(proof["inner_path"]))
    inner = copy.deepcopy(original)
    if tamper == "head":
        snapshot["head"] = "b" * 40
    elif tamper == "revision":
        snapshot["revision"] = "different"
    elif tamper == "hash":
        proof["receipt_sha256"] = "0" * 64
    elif tamper == "unknown_reason":
        proof["reason"] = reason = "arbitrary blocked reason"
    elif tamper == "trigger":
        proof["next_trigger"] = "retry automatically"
    elif tamper in {"operation", "completed_operation"}:
        inner["items"][target]["operation"] = {
            "id": "untrusted-op",
            "status": "completed" if tamper.startswith("completed") else "uncertain",
        }
    elif tamper == "operation_history":
        inner["items"][target]["operation_history"] = [{"id": "old-op", "status": "completed"}]
    elif tamper == "progress":
        inner["round"]["receipt"]["progress"] = [{"target": target}]
    elif tamper == "eligible_remaining":
        inner["round"]["receipt"]["eligible_remaining"] = [target]
    elif tamper == "receipt":
        inner["round"]["receipt"]["valid"] = False
    queue._atomic_write(Path(proof["inner_path"]), inner)
    assert pipeline._valid_inner_wait(
        path, target, "refusal-rev", snapshot, reason, TRIGGER, proof
    ) is (tamper is None)
    # The original native refusal remains demonstrably non-mutating in fixture
    # memory, even when an adversarial copied ledger is rejected.
    assert original["items"][target].get("operation") is None
    assert receipt["progress"] == []


def compacted_legacy_refusal(tmp_path, reason):
    """Produce the old failed callback, then seed its documented compact/audit format.

    Compaction is historical input, not a claimed operation or accepted decision.
    Every action/receipt in that input came from the real native queue above.
    """
    target, path, snapshot, receipt, proof = native_refusal(tmp_path, reason)
    prepared = {
        "revision": snapshot["revision"],
        "identity": adapter.digest([target, snapshot["head"], snapshot.get("merge")]),
        "snapshot": snapshot,
    }
    first = pipeline.run_pipeline(
        path,
        [target],
        prepare=lambda *_: copy.deepcopy(prepared),
        interpret=lambda *_: {"substantive_needs": []},
        exception=lambda *_: pytest.fail("no provider required"),
        act=lambda *_: {
            "status": "blocked",
            "reason": reason,
            "next_trigger": TRIGGER,
            "queue_receipt": receipt,
        },
        capacity_fn=lambda: {"io": 1, "low": 1, "exception": 1},
        deadline=time.time() + 2,
    )
    assert first["targets"][target]["status"] == "failed"
    doc = pipeline._load(path)
    run_id, run = next(iter(doc["runs"].items()))
    old = copy.deepcopy(run["targets"][target])
    audit_row = {
        "item_sha256": pipeline._digest(old),
        "revision": old["revision"],
        "identity": old["identity"],
        "work_key": old["work_key"],
        "status": "failed",
        "stage": "act",
        "error": old["error"],
        "act_result": copy.deepcopy(old["results"]["act"]),
        "act_receipt_sha256": pipeline._digest(receipt),
        "recovery": "quarantine",
    }
    run["invalid_stop_history"] = [
        {
            "schema": "closer-expired-invalid-round-recovery/v1",
            "source_run_id": run_id,
            "targets": {target: audit_row},
        }
    ]
    row = run["targets"][target]
    row["results"] = {}
    row["recovery_barrier"] = {
        "schema": "closer-unverified-act-barrier/v1",
        "source_run_id": run_id,
        "item_sha256": audit_row["item_sha256"],
        "revision": old["revision"],
        "identity": old["identity"],
        "work_key": old["work_key"],
        "act_result_sha256": pipeline._digest(audit_row["act_result"]),
    }
    pipeline._save(path, doc)
    return target, path, snapshot, proof, doc, old, prepared


@pytest.mark.parametrize("reason", REASONS)
def test_compacted_refusal_recovery_preserves_audit_and_survives_two_cadences(tmp_path, reason):
    target, path, snapshot, proof, doc, old, prepared = compacted_legacy_refusal(tmp_path, reason)
    run = next(iter(doc["runs"].values()))
    compact = copy.deepcopy(run["targets"][target])
    origin = copy.deepcopy(run["invalid_stop_history"])
    inner_before = open(proof["inner_path"], "rb").read()
    derived = pipeline._restored_compact_refusal(path, target, compact, origin)
    assert derived is not None
    assert derived["status"] == "failed" and derived["error"] == old["error"]
    assert derived["results"]["act"] == old["results"]["act"]
    assert compact == run["targets"][target] and compact["results"] == {}
    clock = max(time.time(), run["deadline"] + 1)
    for _ in range(2):
        clock += 10
        receipt = pipeline.run_pipeline(
            path,
            [target],
            prepare=lambda *_: copy.deepcopy(prepared),
            interpret=lambda *_: {
                "classification": "deterministic_inner_wait",
                "authority": False,
                "substantive_needs": [],
                "inner_wait_proof": proof,
            },
            exception=lambda *_: pytest.fail("refusal recovery cannot dispatch a provider"),
            act=lambda *_: {
                "status": "deferred",
                "reason": "validated_inner_wait",
                "next_trigger": TRIGGER,
                "resource_evidence": {"inner_wait": proof},
            },
            capacity_fn=lambda: {"io": 1, "low": 1, "exception": 1},
            deadline=clock + 5,
            now=lambda: clock,
        )
        assert receipt["targets"][target]["status"] == "deferred"
        assert pipeline.validate_stop(path, [target], now=lambda: clock)["stop_valid"]
        current = next(iter(pipeline._load(path)["runs"].values()))
        assert origin[0] in current["invalid_stop_history"]
        assert current["targets"][target]["results"]["act"]["status"] == "deferred"
        assert not current["targets"][target].get("known_completed_action")
    assert open(proof["inner_path"], "rb").read() == inner_before


@pytest.mark.parametrize(
    "tamper",
    [
        "act_hash",
        "item_hash",
        "source",
        "identity",
        "revision",
        "work_key",
        "audit_act",
        "native_operation",
        "native_head",
        "missing_audit",
        "source_missing",
        "source_null",
        "item_hash_missing",
        "item_hash_null",
        "act_hash_missing",
        "act_hash_null",
        "provider_outcome_uncertain",
        "known_completed_action",
    ],
)
def test_compacted_refusal_requires_immutable_origin_and_current_native_proof(tmp_path, tamper):
    target, path, _snapshot, proof, doc, _old, _prep = compacted_legacy_refusal(
        tmp_path, REASONS[0]
    )
    run = next(iter(doc["runs"].values()))
    item = run["targets"][target]
    history = run["invalid_stop_history"]
    if tamper in {"act_hash", "item_hash"}:
        item["recovery_barrier"]["act_result_sha256" if tamper == "act_hash" else "item_sha256"] = (
            "0" * 64
        )
    elif tamper == "source":
        item["recovery_barrier"]["source_run_id"] = "unrelated-run"
    elif tamper in {"identity", "revision", "work_key"}:
        item[tamper] = "unrelated"
    elif tamper == "audit_act":
        history[0]["targets"][target]["act_result"]["reason"] = "arbitrary blocked reason"
    elif tamper == "missing_audit":
        history = []
    elif tamper in {"provider_outcome_uncertain", "known_completed_action"}:
        item[tamper] = (
            {"actual": {"run_id": "uncertain-provider"}} if tamper.startswith("provider") else True
        )
    elif tamper.endswith(("_missing", "_null")):
        prefix, action = tamper.rsplit("_", 1)
        key = {
            "source": "source_run_id",
            "item_hash": "item_sha256",
            "act_hash": "act_result_sha256",
        }[prefix]
        # Tamper both sides together: equality of two absent/null values cannot
        # replace actual immutable provenance.
        if action == "missing":
            item["recovery_barrier"].pop(key)
        else:
            item["recovery_barrier"][key] = None
        audit_key = (
            "source_run_id"
            if key == "source_run_id"
            else "item_sha256" if key == "item_sha256" else None
        )
        if audit_key:
            holder = history[0] if audit_key == "source_run_id" else history[0]["targets"][target]
            if action == "missing":
                holder.pop(audit_key)
            else:
                holder[audit_key] = None
    else:
        inner = queue._read_state(Path(proof["inner_path"]))
        if tamper == "native_operation":
            inner["items"][target]["operation"] = {"id": "uncertain-op", "status": "uncertain"}
        else:
            inner["items"][target]["evidence"]["head"] = "b" * 40
        queue._atomic_write(Path(proof["inner_path"]), inner)
    before = copy.deepcopy(item)
    assert pipeline._restored_compact_refusal(path, target, item, history) is None
    assert item == before  # failed inputs cannot rewrite the preserved failure


@pytest.mark.parametrize("reason", REASONS)
@pytest.mark.parametrize("marker", ["provider_outcome_uncertain", "known_completed_action"])
def test_retained_refusal_cannot_override_conflicting_outcome_marker(tmp_path, reason, marker):
    target, path, snapshot, receipt, _proof = native_refusal(tmp_path, reason)
    item = {
        "status": "failed",
        "stage": "act",
        "revision": snapshot["revision"],
        "error": "act returned unsupported or missing status",
        "results": {
            "prepare": {"snapshot": snapshot},
            "act": {
                "status": "blocked",
                "reason": reason,
                "next_trigger": TRIGGER,
                "queue_receipt": receipt,
            },
        },
        marker: (
            {"actual": {"run_id": "uncertain-provider"}} if marker.startswith("provider") else True
        ),
    }
    assert not pipeline._legacy_failed_act_wait_proof(path, target, item)
