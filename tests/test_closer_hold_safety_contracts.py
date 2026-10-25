"""Causal hold safety contracts; all remote/provider boundaries are synthetic."""

from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest

import closer_campaign as campaigns
import closer_hold_followup as holds
import closer_pipeline as pipeline
import closer_queue as queue
import closer_resources as resources
from closer_frontier_policy import make_frontier

NOW = 1000.0
PIN = "c" * 64
TARGET = "offline/safety#1"


def world(tmp_path, *, kind="provider", count=1, attempt_budget=96):
    """Use actual campaign/capture and exact finite shard/queue identities."""
    targets = [f"offline/safety#{number}" for number in range(1, count + 1)]
    frontier = make_frontier(
        targets,
        owner="offline-owner",
        frontier_id="offline-hold-frontier",
        bindings={target: {"revision": "revision-1", "action_id": "a" * 64} for target in targets},
    )
    assignment = tmp_path / "assignment.json"
    assignment.write_text(json.dumps({"targets": [{"target": target} for target in targets]}))
    root = resources.scoped_root(tmp_path / "queue.pipeline.json")
    paths = {}
    for target in targets:
        path = resources.shard_dir(root, target) / "state.json"
        path.parent.mkdir(parents=True)
        row = {
            "status": "held" if kind in {"role", "accepted"} else "unknown",
            "stage": "interpret" if kind in {"role", "provider"} else "act",
            "stage_status": "completed",
            "revision": "revision-1",
            "identity": target,
            "work_key": "offline-request",
            "stage_started_at": 10.0,
            "stage_deadline": 20.0,
            "results": {
                "prepare": {
                    "revision": "revision-1",
                    "snapshot": {"target": target, "head": "a" * 40, "merge": "b" * 40},
                }
            },
        }
        if kind == "provider":
            row["provider_outcome_uncertain"] = {"actual": {"run_id": "offline-provider-run"}}
        elif kind == "role":
            row["results"]["interpret"] = {
                "status": "pending",
                "classification": "role_qualification_unavailable",
                "pending_evidence": {"role": "bounded_classification"},
            }
        else:
            row["results"]["act"] = {
                "operation_id": "operation-original",
                "pending_evidence": {
                    "operation_id": "operation-original",
                    "target": target,
                    "head": "a" * 40,
                    "merge": "b" * 40,
                    "event_id": "e" * 64,
                },
                "queue_receipt": {"offline": "receipt"},
            }
            if kind == "accepted":
                row["deferred_reason"] = "accepted_verifier_request"
        entry = {
            "schema": "closer-pipeline-frontier/v2",
            "finite_frontier": copy.deepcopy(frontier),
            "run_id": pipeline._digest(
                ["finite-frontier/v2", frontier["frontier_id"], frontier["owner"]]
            )[:32],
            "items": {target: row},
        }
        path.write_text(
            json.dumps(
                {
                    "schema": pipeline.SCHEMA,
                    "runs": {},
                    "carry": {},
                    "cache": {},
                    "frontiers": {frontier["frontier_id"]: entry},
                }
            )
        )
        (path.parent / "lease.json").write_text(
            json.dumps(
                {
                    "target": target,
                    "owner": "offline-terminal",
                    "epoch": 1,
                    "finite_frontier": frontier,
                }
            )
        )
        if kind in {"action", "accepted"}:
            action_path = path.parent / "state.actions" / (pipeline._digest(target) + ".json")
            action_path.parent.mkdir(parents=True)
            action_path.write_text(
                json.dumps(
                    {
                        "schema": queue.SCHEMA,
                        "items": {},
                        "round": {},
                        "frontiers": {
                            frontier["frontier_id"]: {
                                "policy": frontier,
                                "round": {},
                                "items": {
                                    target: {
                                        "operation": {
                                            "id": "operation-original",
                                            "revision": "revision-1",
                                            "status": "uncertain",
                                            "action": "post_merge_verify",
                                            "owner": "offline-original-writer",
                                            "binding": {"head": "a" * 40, "merge": "b" * 40},
                                        }
                                    }
                                },
                            }
                        },
                    }
                )
            )
        paths[target] = path
    obj = campaigns.Campaign(tmp_path / "offline.campaign.json")
    obj.begin(
        "offline-safety-campaign",
        targets,
        owner=frontier["owner"],
        release_pin=PIN,
        worker_config={
            "finite_frontier": frontier,
            "dispatch_state": str(tmp_path / "queue"),
            "assignment": str(assignment),
            "hold_followup_policy": {
                "check_interval_seconds": 900,
                "deadline_seconds": 86400,
                "attempt_budget": attempt_budget,
            },
        },
    )
    doc = holds.capture(obj, now=NOW)
    assert len(doc["hold_followups"]) == count
    return obj, paths, {"package_digest": PIN}


def only_hold(obj):
    return next(iter(obj.read()["hold_followups"].values()))


def successful_no_start_world(tmp_path, monkeypatch):
    """Keep reentry plan/apply/audit real; stub only bounded authority oracles."""
    obj, paths, manifest = world(tmp_path, kind="role", attempt_budget=1)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest))
    monkeypatch.setattr(holds.time, "time", lambda: NOW)
    monkeypatch.setenv("HOME", str(tmp_path / "offline-home"))

    def terminal(doc):
        doc["workers"] = [
            {
                "worker_id": "offline-known-terminal-worker",
                "target": campaigns.ROUND_UNIT,
                "state": "terminal",
                "outcome": "ok",
                "pid": None,
            }
        ]
        doc["targets"][campaigns.ROUND_UNIT] = "done"

    obj.update(terminal)
    boundary = {"record": {"worker_id": "offline-known-terminal-worker"}, "signature": "offline"}

    def validate(campaign, doc, manifest, record, proof):
        holds._assert_bound(doc, record)
        if (
            proof != boundary
            or not holds._probe_allowed(doc)
            or any(worker.get("state") != "terminal" for worker in doc.get("workers", []))
        ):
            raise ValueError("offline terminal oracle refused changed authority")

    def observe(campaign, doc, manifest, record):
        validate(campaign, doc, manifest, record, boundary)
        holds._source_row(record)
        return copy.deepcopy(boundary)

    monkeypatch.setattr(holds, "_native_terminal_boundary", observe)
    monkeypatch.setattr(holds, "_validate_terminal_boundary", validate)
    monkeypatch.setattr(
        holds,
        "release_probe",
        lambda *args: {
            "kind": "qualified_role_followup",
            "same_bound_sources": "offline-current-admission",
            "acceptance_authority": False,
        },
    )
    return obj, paths, manifest, manifest_path


def consume_or_refuse(obj, manifest, *, now=NOW):
    try:
        return holds.consume(obj, manifest, "offline-manifest.json", now=now)
    except (ValueError, RuntimeError):
        # An explicit pre-probe ownership refusal is safe; no effect or budget
        # reset may occur merely because source context has changed.
        return obj.read(), []


@pytest.mark.parametrize(
    "changed",
    ["missing-original-id", "missing-original-revision", "different-id", "different-revision"],
)
def test_unknown_action_never_adopts_another_operation(tmp_path, monkeypatch, changed):
    obj, paths, _manifest = world(tmp_path, kind="action")
    record = only_hold(obj)
    path = paths[TARGET].parent / "state.actions" / (pipeline._digest(TARGET) + ".json")
    document = json.loads(path.read_text())
    operation = document["frontiers"][record["frontier"]["frontier_id"]]["items"][TARGET][
        "operation"
    ]
    if changed == "missing-original-id":
        record["source"]["operation_id"] = None
    elif changed == "missing-original-revision":
        record["source"]["operation_revision"] = None
    elif changed == "different-id":
        operation["id"] = "operation-newer"
    else:
        operation["revision"] = "revision-newer"
    path.write_text(json.dumps(document))
    calls = []
    monkeypatch.setattr(
        queue,
        "reconcile_only",
        lambda *a, **k: calls.append((a, k)) or {"status": "completed"},
        raising=False,
    )
    monkeypatch.setattr(
        holds, "_backend", lambda *a: SimpleNamespace(reconcile=lambda *a: {"status": "completed"})
    )
    try:
        result = holds._probe_action(record, "offline-manifest.json", obj.read())
    except ValueError:
        result = {"status": "waiting"}
    assert result["status"] != "resolved"
    assert calls == []


def test_accepted_post_and_general_green_ci_do_not_prove_verifier_terminal(tmp_path, monkeypatch):
    obj, paths, _manifest = world(tmp_path, kind="accepted")
    record = only_hold(obj)
    row = json.loads(paths[TARGET].read_text())["frontiers"][record["frontier"]["frontier_id"]][
        "items"
    ][TARGET]
    snapshot = {
        "target": TARGET,
        "revision": "revision-1",
        "head": "a" * 40,
        "merge": "b" * 40,
        "checks": "SUCCESS",
        "observed_at": NOW,
        "verifier_lifecycle": {"status": "pending", "run_id": "original-run", "run_attempt": 1},
    }
    monkeypatch.setattr(holds, "_backend", lambda *a: SimpleNamespace(observe=lambda *a: snapshot))
    monkeypatch.setattr(
        queue,
        "accepted_verifier_wait_proof",
        lambda *a, **k: {
            "schema": "closer-accepted-verifier-wait/v1",
            "operation_id": "operation-original",
        },
    )
    result = holds._probe_ci(record, "offline-manifest.json", obj.read(), row)
    assert result["status"] != "released"


def test_same_head_terminal_marker_cannot_replace_original_request_terminal_bridge(
    tmp_path, monkeypatch
):
    obj, paths, _manifest = world(tmp_path, kind="accepted")
    record = only_hold(obj)
    row = json.loads(paths[TARGET].read_text())["frontiers"][record["frontier"]["frontier_id"]][
        "items"
    ][TARGET]
    request_basis = "e" * 64
    # Deliberately make superficially matching marker fields as strong as
    # possible. A request-basis digest still is not an authenticated bridge to
    # this evaluator event/run/attempt, even if someone reuses the same text.
    marker = {
        "schema": "verifier-corpus-decision/v1",
        "verdict": "PASS",
        "evaluated_sha": "b" * 40,
        "head_sha": "a" * 40,
        "verification_event": {"event_id": request_basis},
        "run_id": "50001",
        "run_attempt": 1,
    }
    snapshot = {
        "target": TARGET,
        "revision": "revision-1",
        "head": "a" * 40,
        "merge": "b" * 40,
        "checks": "SUCCESS",
        "observed_at": NOW,
        "verifier": marker,
    }
    proof = {
        "schema": "closer-accepted-verifier-wait/v1",
        "operation_id": "operation-original",
        "head": "a" * 40,
        "merge": "b" * 40,
        "event_id": request_basis,
    }
    monkeypatch.setattr(
        holds,
        "_backend",
        lambda *args, **kwargs: SimpleNamespace(observe=lambda *args, **kwargs: snapshot),
    )
    monkeypatch.setattr(queue, "accepted_verifier_wait_proof", lambda *args, **kwargs: proof)
    result = holds._probe_ci(record, "offline-manifest.json", obj.read(), row)
    assert result["status"] != "released"


@pytest.mark.parametrize("changed", ["pin", "config", "generation"])
def test_source_context_drift_cannot_probe_or_release_original_hold(tmp_path, monkeypatch, changed):
    obj, _paths, manifest = world(tmp_path)
    original = only_hold(obj)
    if changed == "pin":
        obj.update(lambda doc: doc.update(release_pin="d" * 64))
    elif changed == "generation":
        obj.update(lambda doc: doc.update(generation=doc["generation"] + 1))
    else:
        obj.update(
            lambda doc: doc["worker_config"].update(dispatch_state=str(tmp_path / "other-queue"))
        )
    calls = []
    monkeypatch.setattr(
        holds, "_probe", lambda *args, **kwargs: calls.append(args) or {"status": "resolved"}
    )
    after, _reports = consume_or_refuse(obj, manifest)
    retained = after["hold_followups"][original["hold_id"]]
    assert retained["status"] not in {"released", "resolved"}
    assert retained["deadline"] == original["deadline"]
    assert calls == []


def test_capture_and_restart_preserve_original_budget_deadline_and_attempts(tmp_path, monkeypatch):
    obj, _paths, manifest = world(tmp_path)
    monkeypatch.setattr(holds, "_probe", lambda *args, **kwargs: {"status": "waiting"})
    holds.consume(obj, manifest, "offline-manifest.json", now=NOW)
    before = only_hold(obj)
    assert before["attempts"] == 1
    restarted = campaigns.Campaign(obj.path)
    for at in (NOW + 1, NOW + 100, NOW + 1000):
        holds.capture(restarted, now=at)
        after = only_hold(restarted)
        assert {
            key: after[key] for key in ("created_at", "deadline", "attempt_budget", "attempts")
        } == {key: before[key] for key in ("created_at", "deadline", "attempt_budget", "attempts")}


@pytest.mark.parametrize("changed", ["pin", "config", "generation"])
def test_source_context_drift_during_probe_cannot_commit_terminal_result(
    tmp_path, monkeypatch, changed
):
    obj, _paths, manifest = world(tmp_path)
    original = only_hold(obj)
    calls = []

    def drift(*args, **kwargs):
        calls.append("probe")
        if changed == "pin":
            obj.update(lambda doc: doc.update(release_pin="d" * 64))
        elif changed == "generation":
            obj.update(lambda doc: doc.update(generation=doc["generation"] + 1))
        else:
            obj.update(
                lambda doc: doc["worker_config"].update(
                    dispatch_state=str(tmp_path / "other-queue")
                )
            )
        return {"status": "resolved"}

    monkeypatch.setattr(holds, "_probe", drift)
    after, _reports = consume_or_refuse(obj, manifest)
    retained = after["hold_followups"][original["hold_id"]]
    assert calls == ["probe"]
    assert retained["status"] not in {"released", "resolved"}
    assert retained["deadline"] == original["deadline"]


def test_recapture_does_not_renew_original_bounds_after_policy_change(tmp_path):
    obj, _paths, _manifest = world(tmp_path)
    original = only_hold(obj)
    obj.update(
        lambda doc: doc["worker_config"]["hold_followup_policy"].update(
            deadline_seconds=172800, attempt_budget=4096
        )
    )
    try:
        holds.capture(campaigns.Campaign(obj.path), now=NOW + 10000)
    except ValueError:
        pass
    doc = obj.read()
    retained = doc["hold_followups"][original["hold_id"]]
    assert len(doc["hold_followups"]) == 1
    assert retained["created_at"] == original["created_at"]
    assert retained["deadline"] == original["deadline"]
    assert retained["attempt_budget"] == original["attempt_budget"]


@pytest.mark.parametrize("exhausted", ["deadline", "attempts"])
def test_exhaustion_escalates_without_probing_or_granting_authority(
    tmp_path, monkeypatch, exhausted
):
    obj, _paths, manifest = world(tmp_path)
    original = only_hold(obj)

    def exhaust(doc):
        row = doc["hold_followups"][original["hold_id"]]
        row["last_event_fingerprint"] = holds._event_fingerprint(row)
        row["next_check_at"] = row["deadline"]
        if exhausted == "attempts":
            row["attempts"] = row["attempt_budget"]

    obj.update(exhaust)
    calls = []
    monkeypatch.setattr(
        holds, "_probe", lambda *args, **kwargs: calls.append(args) or {"status": "resolved"}
    )
    now = original["deadline"] + 1 if exhausted == "deadline" else NOW + 1
    after, reports = holds.consume(obj, manifest, "offline-manifest.json", now=now)
    retained = after["hold_followups"][original["hold_id"]]
    assert retained["status"] == "escalated"
    assert retained["deadline"] == original["deadline"]
    assert retained["attempt_budget"] == original["attempt_budget"]
    assert calls == []
    assert reports and all(report.get("authority") is False for report in reports)


def test_unknown_effect_probe_cannot_release_a_retry(tmp_path, monkeypatch):
    obj, _paths, manifest = world(tmp_path)
    original = only_hold(obj)
    monkeypatch.setattr(holds, "_probe", lambda *args, **kwargs: {"status": "released"})
    after, _reports = holds.consume(obj, manifest, "offline-manifest.json", now=NOW)
    retained = after["hold_followups"][original["hold_id"]]
    assert retained["execution_state"] == "provider_outcome_unknown"
    assert retained["status"] == "waiting"
    assert retained["deadline"] == original["deadline"]


def test_interrupted_unknown_probe_restart_preserves_original_unknown_and_budget(
    tmp_path, monkeypatch
):
    obj, _paths, manifest = world(tmp_path)
    original = only_hold(obj)

    def crash(*args, **kwargs):
        raise SystemExit("offline process death after persisted check intent")

    monkeypatch.setattr(holds, "_probe", crash)
    with pytest.raises(SystemExit):
        holds.consume(obj, manifest, "offline-manifest.json", now=NOW)
    interrupted = only_hold(obj)
    assert interrupted["status"] == "checking"
    assert interrupted["attempts"] == 1
    assert interrupted["execution_state"] == "provider_outcome_unknown"
    restarted = campaigns.Campaign(obj.path)
    monkeypatch.setattr(holds, "_probe", lambda *args, **kwargs: {"status": "waiting"})
    holds.consume(
        restarted, manifest, "offline-manifest.json", now=interrupted["next_check_at"] + 1
    )
    after = only_hold(restarted)
    assert after["deadline"] == original["deadline"]
    assert after["attempt_budget"] == original["attempt_budget"]
    assert 1 <= after["attempts"] <= 2
    assert after["execution_state"] == "provider_outcome_unknown"
    assert after["status"] == "waiting"


def test_expired_hold_precedes_lexically_earlier_due_probes(tmp_path, monkeypatch):
    obj, _paths, manifest = world(tmp_path, count=3)
    ids = sorted(obj.read()["hold_followups"])
    expired_id = ids[-1]

    def expire_last(doc):
        for row in doc["hold_followups"].values():
            row["last_event_fingerprint"] = holds._event_fingerprint(row)
            row["next_check_at"] = NOW
        doc["hold_followups"][expired_id]["deadline"] = NOW + 1

    obj.update(expire_last)
    probes = []
    monkeypatch.setattr(
        holds,
        "_probe",
        lambda row, *args, **kwargs: probes.append(row["hold_id"]) or {"status": "waiting"},
    )
    after, _reports = holds.consume(obj, manifest, "offline-manifest.json", now=NOW + 2)
    assert after["hold_followups"][expired_id]["status"] == "escalated"
    assert expired_id not in probes


def test_oldest_due_hold_is_checked_before_recently_probed_lexical_predecessors(
    tmp_path, monkeypatch
):
    obj, _paths, manifest = world(tmp_path, count=3)
    ids = sorted(obj.read()["hold_followups"])
    oldest = ids[-1]

    def schedule(doc):
        for key, row in doc["hold_followups"].items():
            row["last_event_fingerprint"] = holds._event_fingerprint(row)
            row["next_check_at"] = NOW
            row["last_check_at"] = NOW - 100 if key == oldest else NOW - 1

    obj.update(schedule)
    probes = []
    monkeypatch.setattr(
        holds,
        "_probe",
        lambda row, *args, **kwargs: probes.append(row["hold_id"]) or {"status": "waiting"},
    )
    holds.consume(obj, manifest, "offline-manifest.json", now=NOW + 1)
    assert probes and probes[0] == oldest


def test_actual_apply_then_process_death_recovers_same_audit_without_new_attempt(
    tmp_path, monkeypatch
):
    import closer_finite_reentry as reentry

    obj, paths, manifest, manifest_path = successful_no_start_world(tmp_path, monkeypatch)
    original = only_hold(obj)
    original_apply = reentry.apply
    calls = []

    def apply_then_die(*args, **kwargs):
        result = original_apply(*args, **kwargs)
        calls.append((kwargs["event"], kwargs["expected_plan_sha256"]))
        if len(calls) == 1:
            raise SystemExit("offline death after durable audit, before hold finish")
        assert result["idempotent"] is True
        return result

    monkeypatch.setattr(reentry, "apply", apply_then_die)
    with pytest.raises(SystemExit):
        holds.consume(obj, manifest, str(manifest_path), now=NOW)
    interrupted = only_hold(obj)
    assert interrupted["status"] == "checking"
    assert interrupted["attempts"] == 1
    assert interrupted["reentry_intent"]["event"] == calls[0][0]
    before_recovery = paths[TARGET].read_bytes()
    row = json.loads(before_recovery)["frontiers"][original["frontier"]["frontier_id"]]["items"][
        TARGET
    ]
    assert row["stage"] == "prepare" and row["results"] == {}
    assert len(row["reentry_history"]) == 1

    restarted = campaigns.Campaign(obj.path)
    after, _reports = holds.consume(restarted, manifest, str(manifest_path), now=NOW + 1)
    recovered = after["hold_followups"][original["hold_id"]]
    assert recovered["status"] == "released"
    assert recovered["attempts"] == 1
    assert recovered["deadline"] == original["deadline"]
    assert recovered["attempt_budget"] == original["attempt_budget"]
    assert calls == [calls[0], calls[0]]
    assert paths[TARGET].read_bytes() == before_recovery
    audits = list((paths[TARGET].parent.parent.parent / "reentry-audit").glob("*.json"))
    assert len(audits) == 1
    assert json.loads(audits[0].read_text())["status"] == "applied"
    lease = resources.acquire(
        paths[TARGET].parent.parent.parent,
        TARGET,
        owner="offline-fresh-native-admission",
        finite_frontier=original["frontier"],
    )
    try:
        with resources.fenced(lease):
            assert reentry.ready(paths[TARGET], original["frontier"], TARGET, row)
    finally:
        lease.release()


def test_audited_rearm_advances_only_observer_generation_once_and_preserves_bounds(
    tmp_path, monkeypatch
):
    obj, _paths, manifest, manifest_path = successful_no_start_world(tmp_path, monkeypatch)
    original = only_hold(obj)
    doc, _reports = holds.consume(obj, manifest, str(manifest_path), now=NOW)
    assert only_hold(obj)["status"] == "released"
    rearmed = holds.rearm(obj, doc, manifest, now=NOW + 1)
    retained = rearmed["hold_followups"][original["hold_id"]]
    assert rearmed["generation"] == original["generation"] + 1
    assert retained["generation"] == original["generation"]
    assert retained["observer_generation"] == rearmed["generation"]
    assert retained["status"] == "consumed"
    assert retained["deadline"] == original["deadline"]
    assert retained["attempt_budget"] == original["attempt_budget"]
    assert retained["attempts"] == 1
    assert rearmed["targets"][campaigns.ROUND_UNIT] == "pending"
    assert len(rearmed["hold_followup_transitions"]) == 1
    again = holds.rearm(obj, obj.read(), manifest, now=NOW + 2)
    assert again["generation"] == rearmed["generation"]
    assert len(again["hold_followup_transitions"]) == 1
    holds.capture(campaigns.Campaign(obj.path), now=NOW + 3)
    assert len(obj.read()["hold_followups"]) == 1


@pytest.mark.parametrize("change", ["denial", "live-worker"])
def test_native_authority_change_after_plan_prevents_local_apply(tmp_path, monkeypatch, change):
    import closer_finite_reentry as reentry

    obj, paths, manifest, manifest_path = successful_no_start_world(tmp_path, monkeypatch)
    original_bytes = paths[TARGET].read_bytes()
    original_plan = reentry.plan

    def plan_then_change(*args, **kwargs):
        result = original_plan(*args, **kwargs)
        if change == "denial":
            obj.update(lambda doc: doc.update(hold={"kind": "denied", "reason": "offline denial"}))
        else:
            obj.update(lambda doc: doc["workers"][-1].update(state="active"))
        return result

    monkeypatch.setattr(reentry, "plan", plan_then_change)
    monkeypatch.setattr(
        reentry, "apply", lambda *a, **k: pytest.fail("changed authority cannot apply")
    )
    holds.consume(obj, manifest, str(manifest_path), now=NOW)
    assert only_hold(obj)["status"] == "waiting"
    assert paths[TARGET].read_bytes() == original_bytes


def test_rearm_refuses_source_row_changed_after_applied_audit(tmp_path, monkeypatch):
    obj, paths, manifest, manifest_path = successful_no_start_world(tmp_path, monkeypatch)
    original = only_hold(obj)
    doc, _reports = holds.consume(obj, manifest, str(manifest_path), now=NOW)
    assert only_hold(obj)["status"] == "released"
    state = json.loads(paths[TARGET].read_text())
    row = state["frontiers"][original["frontier"]["frontier_id"]]["items"][TARGET]
    row["results"] = {"prepare": {"unexpected": "later writer"}}
    paths[TARGET].write_text(json.dumps(state))
    with pytest.raises(ValueError, match="applied fresh-PREPARE audit"):
        holds.rearm(obj, doc, manifest, now=NOW + 1)
    after = obj.read()
    assert after["generation"] == original["generation"]
    assert after["targets"][campaigns.ROUND_UNIT] == "done"
    assert not after.get("hold_followup_transitions")


def test_deadline_expiring_during_final_rearm_read_cannot_open_new_generation(
    tmp_path, monkeypatch
):
    obj, _paths, manifest, manifest_path = successful_no_start_world(tmp_path, monkeypatch)
    original = only_hold(obj)
    doc, _reports = holds.consume(obj, manifest, str(manifest_path), now=NOW)
    assert only_hold(obj)["status"] == "released"
    original_probe = holds.release_probe
    clock = [NOW + 1]
    monkeypatch.setattr(holds.time, "time", lambda: clock[0])

    def observe_then_expire(*args):
        proof = original_probe(*args)
        clock[0] = original["deadline"] + 1
        return proof

    monkeypatch.setattr(holds, "release_probe", observe_then_expire)
    try:
        holds.rearm(obj, doc, manifest, now=NOW + 1)
    except ValueError:
        pass
    after = obj.read()
    assert after["generation"] == original["generation"]
    assert after["targets"][campaigns.ROUND_UNIT] == "done"
    assert not after.get("hold_followup_transitions")
    retained = after["hold_followups"][original["hold_id"]]
    assert retained["status"] == "escalated"
    assert retained["deadline"] == original["deadline"]


def test_released_hold_expiry_escalates_without_replaying_applied_audit(tmp_path, monkeypatch):
    import closer_finite_reentry as reentry

    obj, paths, manifest, manifest_path = successful_no_start_world(tmp_path, monkeypatch)
    original = only_hold(obj)
    holds.consume(obj, manifest, str(manifest_path), now=NOW)
    assert only_hold(obj)["status"] == "released"
    applied_bytes = paths[TARGET].read_bytes()
    monkeypatch.setattr(
        reentry, "apply", lambda *a, **k: pytest.fail("expired release cannot reapply")
    )
    after, _reports = holds.consume(
        campaigns.Campaign(obj.path), manifest, str(manifest_path), now=original["deadline"] + 1
    )
    retained = after["hold_followups"][original["hold_id"]]
    assert retained["status"] == "escalated"
    assert retained["deadline"] == original["deadline"]
    assert retained["attempts"] == 1
    assert paths[TARGET].read_bytes() == applied_bytes
    assert after["generation"] == original["generation"]
