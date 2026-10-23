"""Native legacy contracts remain original bytes; only admission views change."""

import json
import time
from pathlib import Path

import pytest
from test_closer_quarantine_closure import prepared, write

import closer_pipeline as pipeline
import closer_quarantine as census
import closer_queue as queue
import closer_resources as resources


def native_row(target="owner/repo#1", *, action="collect_evidence", capacity=True):
    operation = {
        "id": "a" * 32,
        "action": action,
        "status": "failed" if capacity else "uncertain",
        "revision": "r" * 64,
        "owner": "closer-round:" + "b" * 32,
        "binding": {
            "pr_target": target,
            "head": "a" * 40,
            "merge": None,
            "source_targets": ["owner/repo#3"],
        },
        "claim_targets": [target, "owner/repo#3"],
        "repo_keys": ["owner/repo"],
        "submitted_at": 10.0,
        "deadline": 100.0,
        "timeout_seconds": 90.0,
    }
    # Actual revision values are content digests.
    operation["revision"] = "c" * 64
    if capacity:
        operation.update(
            attempts=1,
            invoked_at=11.0,
            elapsed_seconds=1.0,
            outcome="deferred",
            next_trigger=None,
            result={"reason": "fresh usable Codex capacity required"},
        )
    else:
        operation.update(error_type="RuntimeError", next_trigger="reconcile_operation")
    return {
        "target": target,
        "status": "deferred",
        "action": "wait",
        "eligible": True,
        "revision": "unknown",
        "evidence": {},
        "requires_coordinator": False,
        "operation": operation,
        "operation_history": [],
    }


def raw_ledger(tmp_path, row):
    raw = tmp_path / "dispatch.json"
    path = Path(str(raw) + ".pipeline.json")
    write(raw, {"schema": queue.SCHEMA, "items": {row["target"]: row}, "round": {}})
    write(path, pipeline._empty())
    return raw, path


@pytest.mark.parametrize("action", ["collect_evidence", "review", "repair"])
def test_distinct_native_capacity_actions_are_not_unknown_effects(tmp_path, action):
    row = native_row(action=action)
    raw, path = raw_ledger(tmp_path, row)
    before = raw.read_bytes()
    assert census.native_queue_role(row["target"], row) == "capacity_refusal"
    result = census.scope_snapshot(path)
    assert not result["global_reasons"] and not result["quarantine_keys"]
    assert result["observations"][0]["producer_contract"] == "native_capacity_refusal"
    assert raw.read_bytes() == before


@pytest.mark.parametrize(
    "change",
    [
        {"action": "merge"},
        {"status": "uncertain"},
        {"outcome": "completed"},
        {"error_type": "RuntimeError"},
        {"result": {"reason": "fresh usable Codex capacity required", "run_id": "offload:x"}},
        {"run_id": "external"},
        {"invoked_at": None},
        {"deadline": float("nan")},
        {"attempts": 2},
    ],
)
def test_similar_capacity_text_cannot_hide_started_or_other_operations(tmp_path, change):
    row = native_row()
    row["operation"].update(change)
    raw, path = raw_ledger(tmp_path, row)
    assert census.native_queue_role(row["target"], row) is None
    assert census.scope_snapshot(path)["quarantine_keys"]


def test_capacity_mixed_history_and_private_result_stay_fenced(tmp_path):
    for index, change in enumerate(
        (
            {"operation_history": [{"status": "uncertain", "action": "merge"}]},
            {"requires_coordinator": True},
            {"provider_outcome_uncertain": {"run_id": "unknown"}},
            {"provider_outcome_uncertain": {}},
        )
    ):
        row = native_row()
        row.update(change)
        raw, path = raw_ledger(tmp_path / str(index), row)
        assert census.native_queue_role(row["target"], row) is None
        assert census.scope_snapshot(path)["quarantine_keys"]


def test_intent_free_api_placeholder_is_observation_not_effect(tmp_path):
    row = native_row()
    row.update(
        operation=None,
        operation_binding={},
        claim_targets=[row["target"]],
        repo_keys=["owner/repo"],
        observation_freshness="cached_budget_exhausted",
        deferred_reason="receipt_reserve",
        deferred_next_trigger="retry_observation_next_round",
        next_trigger="fresh complete observation",
        reason="GitHub core capacity is deferred by the rate ledger",
    )
    raw, path = raw_ledger(tmp_path, row)
    before = raw.read_bytes()
    result = census.scope_snapshot(path)
    assert (
        not result["quarantine_keys"]
        and result["observations"][0]["producer_contract"] == "native_observation_wait"
    )
    assert raw.read_bytes() == before
    row["operation_history"] = [{"status": "uncertain"}]
    write(raw, {"schema": queue.SCHEMA, "items": {row["target"]: row}, "round": {}})
    assert census.scope_snapshot(path)["quarantine_keys"]


def test_same_target_advisory_unknown_excludes_only_remote_effect_dependency(tmp_path):
    row = native_row(action="review", capacity=False)
    raw, path = raw_ledger(tmp_path, row)
    before = raw.read_bytes()
    result = census.scope_snapshot(path)
    keys = ["pr:owner/repo#1", "repo:owner/repo"]
    assert result["items"][0]["remote_advisory_only"] is True
    assert census.scope_problem(result, "owner/repo#1", keys, purpose="remote_effect") is None
    assert census.scope_problem(result, "owner/repo#1", keys, purpose="effect")
    assert (
        raw.read_bytes() == before
        and json.loads(before)["items"][row["target"]]["operation"]["status"] == "uncertain"
    )
    # A real independent advertisement remains a fence even for remote actions.
    ad = resources._key_dir(resources.scoped_root(path), "pr:owner/repo#1") / "legacy-intent.json"
    write(ad, {"key": "pr:owner/repo#1", "target": row["target"], "owner": "active-owner"})
    retained = ad.read_bytes()
    result = census.scope_snapshot(path)
    assert census.scope_problem(result, "owner/repo#1", keys, purpose="remote_effect")
    assert ad.read_bytes() == retained


@pytest.mark.parametrize(
    "action",
    ["repair", "merge", "initiate_verify", "post_merge_verify", "respond_review", "close_source"],
)
def test_uncertain_mutating_actions_never_become_advisory(tmp_path, action):
    row = native_row(action=action, capacity=False)
    _raw, path = raw_ledger(tmp_path, row)
    result = census.scope_snapshot(path)
    assert not result["items"][0]["remote_advisory_only"]
    assert census.scope_problem(
        result, "owner/repo#1", ["pr:owner/repo#1", "repo:owner/repo"], purpose="remote_effect"
    )


def blocked_fixture(tmp_path, monkeypatch, *, mixed=False):
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path / "private-state"))
    monkeypatch.setenv("HANDOFF_DIR", str(tmp_path / "handoff"))
    path, raw, snapshot, row, doc, _ref, _retained = prepared(
        tmp_path, snapshot_overrides={"target": "owner/Repo#1"} if mixed else None
    )
    if mixed:
        doc["runs"]["original"]["targets"] = {snapshot["target"]: row}
    target = snapshot["target"]
    reason = "substantive_or_ambiguous_finding"
    inner = path.parent / f"{path.stem}.actions" / f"{pipeline._digest(target)}.json"
    receipt = queue.run_round(
        inner,
        [target],
        lambda _: dict(
            target=target,
            revision=snapshot["revision"],
            action="wait",
            eligible=False,
            requires_coordinator=False,
            reason=reason,
            next_trigger=pipeline.EVIDENCE_REFUSAL_TRIGGER,
            evidence=snapshot,
        ),
        lambda *_: pytest.fail("no effect"),
        deadline=time.time() + 30,
        max_workers=1,
        max_passes=1,
    )
    row.update(status="failed", stage="act", error="act returned unsupported or missing status")
    row.pop("recovery_barrier", None)
    row["results"]["prepare"]["snapshot"] = snapshot
    row["results"]["act"] = {
        "status": "blocked",
        "reason": reason,
        "next_trigger": pipeline.EVIDENCE_REFUSAL_TRIGGER,
        "queue_receipt": receipt,
    }
    write(path, doc)
    return path, inner, row, doc


def test_actual_blocked_act_receipt_admitted_without_any_queue_lock(tmp_path, monkeypatch):
    path, inner, row, _doc = blocked_fixture(tmp_path, monkeypatch)
    before = (path.read_bytes(), inner.read_bytes())
    monkeypatch.setattr(queue, "_ledger_lock", lambda *a, **k: pytest.fail("lock inversion"))
    result = census.scope_snapshot(path)
    assert not result["global_reasons"] and not result["quarantine_keys"]
    assert str(inner) in result["source_hashes"]
    assert (path.read_bytes(), inner.read_bytes()) == before


@pytest.mark.parametrize("change", ["operation", "progress", "receipt", "revision"])
def test_blocked_act_preserves_required_exact_no_operation_proof(tmp_path, monkeypatch, change):
    path, inner, row, doc = blocked_fixture(tmp_path, monkeypatch)
    native = json.loads(inner.read_text())
    target = next(iter(native["items"]))
    if change == "operation":
        native["items"][target]["operation"] = {"status": "uncertain"}
    elif change == "progress":
        native["round"]["receipt"]["progress"] = [{"action": "merge"}]
    elif change == "receipt":
        row["results"]["act"]["queue_receipt"]["valid"] = False
        write(path, doc)
    else:
        native["round"]["source_revisions"][target] = "different"
    write(inner, native)
    assert census.scope_snapshot(path)["quarantine_keys"]


@pytest.mark.parametrize("mixed", [False, True])
def test_original_target_case_and_receipt_mutation_are_bound(tmp_path, monkeypatch, mixed):
    path, inner, _row, _doc = blocked_fixture(tmp_path, monkeypatch, mixed=mixed)
    result = census.scope_snapshot(path)
    assert not result["quarantine_keys"] and str(inner) in result["source_hashes"]
    native = json.loads(inner.read_text())
    native["round"]["receipt"]["progress"] = [{"changed": True}]
    write(inner, native)
    assert (
        census.scope_problem(result, "owner/repo#1", ["pr:owner/repo#1", "repo:owner/repo"])
        == "scope_sources_changed"
    )


@pytest.mark.parametrize("advisory", [False, True])
def test_pipeline_origin_and_raw_origin_classify_without_laundering_each_other(tmp_path, advisory):
    path, raw, snapshot, row, doc, _ref, _retained = prepared(tmp_path)
    native = native_row(
        snapshot["target"],
        action="review" if advisory else "collect_evidence",
        capacity=not advisory,
    )
    write(raw, {"schema": queue.SCHEMA, "items": {native["target"]: native}, "round": {}})
    row.update(status="uncertain", stage="reconcile")
    row["results"]["prepare"]["snapshot"] = snapshot
    import hashlib

    row["identity"] = hashlib.sha256(
        json.dumps(
            [snapshot["target"], snapshot["head"], snapshot.get("merge")], sort_keys=True
        ).encode()
    ).hexdigest()
    row.pop("recovery_barrier")
    row["results"]["reconcile"] = (
        {
            "outcome": "unknown",
            "reason": "legacy uncertain operation lacks external-run or live-owner proof; reconcile it in the original closer queue",
        }
        if advisory
        else {"outcome": "confirmed_not_started", "reason": "inner queue was never created"}
    )
    write(path, doc)
    before = (path.read_bytes(), raw.read_bytes())
    result = census.scope_snapshot(path)
    assert not result["global_reasons"]
    if advisory:
        assert len(result["items"]) == 2 and all(x["remote_advisory_only"] for x in result["items"])
        assert (
            census.scope_problem(
                result,
                "owner/repo#1",
                ["pr:owner/repo#1", "repo:owner/repo"],
                purpose="remote_effect",
            )
            is None
        )
        assert census.scope_problem(result, "owner/repo#1", ["pr:owner/repo#1", "repo:owner/repo"])
    else:
        assert not result["items"] and not result["quarantine_keys"]
    assert (path.read_bytes(), raw.read_bytes()) == before
    # Creating any inner native operation invalidates the derived pipeline exemption.
    inner = path.parent / f"{path.stem}.actions" / f'{pipeline._digest(snapshot["target"])}.json'
    write(inner, {"operation": {"status": "uncertain", "action": "merge"}})
    assert census.scope_snapshot(path)["quarantine_keys"]


def test_public_finite_reentry_dispositions_only_current_no_start_scope_refusal(tmp_path):
    from test_closer_finite_reentry import TARGET, fixture, scope_proof

    import closer_finite_reentry as retry

    resources.open_root(tmp_path / "queue.pipeline.json", pipeline_schema=pipeline.SCHEMA)
    shard, frontier, doc = fixture(tmp_path, stage="act", status="unknown")
    doc["runs"] = {
        "historical": {"targets": {TARGET: {"status": "completed", "stage": "act", "results": {}}}}
    }
    scope_proof(shard, doc)
    shared = tmp_path / "queue.pipeline.json"
    write(shared, pipeline._empty())
    raw = tmp_path / "queue"
    original = native_row(TARGET)
    write(raw, {"schema": queue.SCHEMA, "items": {TARGET: original}, "round": {}})
    before = raw.read_bytes()
    blocked = census.scope_snapshot(shared)
    assert (
        census.scope_problem(
            blocked, TARGET, ["pr:" + TARGET, "repo:owner/repo"], purpose="observation"
        )
        == "unresolved_target_shard"
    )
    proposal = retry.plan(
        shard, frontier=frontier, target=TARGET, event="native-admission-contract-repaired"
    )
    result = retry.apply(
        shard,
        frontier=frontier,
        target=TARGET,
        event="native-admission-contract-repaired",
        expected_plan_sha256=proposal["plan_sha256"],
    )
    assert result["status"] == "applied"
    state = json.loads(shard.read_text())
    row = state["frontiers"]["frozen"]["items"][TARGET]
    assert (
        row["status"] == "held"
        and row["stage_status"] == "completed"
        and "reentry_ready" not in row
    )
    assert state["frontiers"]["frozen"]["finite_frontier"] == frontier
    assert raw.read_bytes() == before
    after = census.scope_snapshot(shared)
    assert (
        census.scope_problem(
            after, TARGET, ["pr:" + TARGET, "repo:owner/repo"], purpose="remote_effect"
        )
        is None
    )


def test_compact_original_snapshot_needs_identity_not_full_raw_equality(tmp_path, monkeypatch):
    path, inner, row, doc = blocked_fixture(tmp_path, monkeypatch)
    original = row["results"]["prepare"]["snapshot"]
    row["results"]["prepare"]["snapshot"] = {
        k: original.get(k) for k in ("target", "revision", "head", "merge")
    }
    write(path, doc)
    assert not census.scope_snapshot(path)["quarantine_keys"]
    row["results"]["prepare"]["snapshot"]["head"] = "d" * 40
    write(path, doc)
    assert census.scope_snapshot(path)["quarantine_keys"]
