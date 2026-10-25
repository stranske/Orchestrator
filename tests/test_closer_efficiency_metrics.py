"""Causal offline summaries over retained native receipts; no authority or I/O."""

import copy
import json
import sqlite3

import pytest

import closer_stage_feedback as metrics

NOW = 10000.0
TARGET = "offline/example#12"
HEAD = "a" * 40
REVISION = "revision-a"
BINDINGS = {TARGET: {"revision": REVISION, "head": HEAD}}


def receipt(*, run="pipeline-1", classification="deterministic_merge", model=None):
    interpreted = {"classification": classification, "substantive_needs": []}
    if model:
        interpreted["actual"] = {"run_id": model, "exit": 0}
    return {
        "schema": "closer-pipeline-receipt/v1",
        "run_id": run,
        "finished_at": NOW - 5,
        "targets": {
            TARGET: {
                "revision": REVISION,
                "status": "completed",
                "results": {
                    "prepare": {
                        "revision": REVISION,
                        "snapshot": {"target": TARGET, "revision": REVISION, "head": HEAD},
                    },
                    "interpret": interpreted,
                    "act": {"status": "completed", "target_complete": True},
                },
                "metrics": {"stage_seconds": {"prepare": 1, "interpret": 0.1, "act": 2}},
            }
        },
    }


def stage_event(run="model-1", *, cost=0.25, elapsed=3.0, accepted=True, **extra):
    payload = {
        "task_category": "closer_interpret",
        "target": TARGET,
        "head_sha": HEAD,
        "capacity_ledger_run_id": run,
        "elapsed_seconds": elapsed,
        "observed_cost_usd": cost,
        "accepted": accepted,
        "independent_validation": True,
        "source_completion": False,
        **extra,
    }
    return {"run_id": run, "event_type": "closer_stage", "payload": {"closer_stage": payload}}


def native_receipt(*, round_id="native-round-1", run="native-run-1"):
    value = receipt(run=run)
    value.pop("run_id")
    value.update(round_id=round_id, run_ids={TARGET: run})
    row = value["targets"][TARGET]
    row["metrics"] = {"requests": 3}
    row["stage_results"] = {
        "prepare": {
            "started_at": NOW - 20,
            "finished_at": NOW - 19,
            "status": "completed",
            "work_key": "prepare-key",
        },
        "interpret": {
            "started_at": NOW - 19,
            "finished_at": NOW - 17,
            "status": "completed",
            "work_key": "interpret-key",
        },
        "act": {
            "started_at": NOW - 17,
            "finished_at": NOW - 14,
            "status": "completed",
            "work_key": "act-key",
        },
    }
    return value


def hold(*, hold_id="hold-1", owner="owner", status="waiting", age=4000):
    return {
        "schema": "closer-hold-followup/v1",
        "hold_id": hold_id,
        "campaign_id": "campaign-1",
        "owner": owner,
        "generation": 1,
        "target": TARGET,
        "run_id": "pipeline-1",
        "frontier": None,
        "revision": REVISION,
        "stage": "interpret",
        "source": {"state_path": "retained/state.json", "row_sha256": "b" * 64},
        "execution_state": "confirmed_not_started",
        "release_proof": {"kind": "fresh_evidence"},
        "event_selector": {"kind": "timer"},
        "created_at": NOW - age,
        "next_check_at": NOW - 1,
        "deadline": NOW + 500,
        "escalation_owner": "owner",
        "attempt_budget": 3,
        "attempts": 0,
        "status": status,
    }


def outcome(**extra):
    return {
        "schema": "closer-independent-outcome/v1",
        "run_id": "model-1",
        "target": TARGET,
        "head_sha": HEAD,
        "observed_at": NOW - 1,
        "independent": True,
        "evidence_ref": {"sha256": "c" * 64, "url": "https://example.test/outcome"},
        "false_acceptance": True,
        "rework": False,
        **extra,
    }


def summarize(receipts=(), **kwargs):
    return metrics.summarize_efficiency(receipts, now=NOW, current_bindings=BINDINGS, **kwargs)


def test_direct_guarded_branch_is_counted_without_model_or_invented_savings():
    summary = summarize([receipt()])
    assert summary["observed_branches"] == {
        "denominator": 1,
        "deterministic_zero_reasoning": 1,
        "with_retained_model_runs": 0,
        "excluded_waits": 0,
    }
    assert summary["model_runs"]["ids"] == []
    assert summary["observed_cost_usd"]["total_or_null"] is None
    assert summary["counterfactual_savings_usd"] is None
    assert summary["authority"] is False


def test_native_round_with_per_target_run_bindings_measures_two_actual_direct_branches():
    value = native_receipt()
    second = "offline/example#13"
    value["targets"][second] = copy.deepcopy(value["targets"][TARGET])
    value["targets"][second]["results"]["prepare"]["snapshot"]["target"] = second
    value["run_ids"][second] = value["run_ids"][TARGET]
    summary = metrics.summarize_efficiency(
        [value], now=NOW, current_bindings={**BINDINGS, second: BINDINGS[TARGET]}
    )
    assert summary["observed_branches"]["denominator"] == 2
    assert summary["observed_branches"]["deterministic_zero_reasoning"] == 2
    observed = summary["branch_stage_latency_seconds"]["deterministic_merge"]
    assert observed["total_or_null"] == 12
    assert observed["measured_runs"] == 2
    assert summary["model_runs"]["count"] == 0


def test_native_replayed_branch_is_deduplicated_across_rounds_and_request_counters():
    value = native_receipt()
    replay = copy.deepcopy(value)
    replay.update(round_id="native-round-2", finished_at=NOW - 4)
    replay["targets"][TARGET]["metrics"]["requests"] = 99
    summary = summarize([value, copy.deepcopy(value), replay])
    assert summary["observed_branches"]["denominator"] == 1
    assert summary["branch_stage_latency_seconds"]["deterministic_merge"]["total_or_null"] == 6


@pytest.mark.parametrize(
    "mapping", [{}, {TARGET: ""}, {TARGET: None}, {"wrong/target#1": "native-run-1"}]
)
def test_native_missing_or_wrong_target_run_mapping_is_rejected(mapping):
    value = native_receipt()
    value["run_ids"] = mapping
    summary = summarize([value])
    assert summary["observed_branches"]["denominator"] == 0
    assert summary["rejected"]["invalid_native_run_bindings"] == 1


@pytest.mark.parametrize("surface", ["row", "current_binding"])
def test_native_wrong_per_target_run_id_is_rejected(surface):
    value = native_receipt()
    bindings = copy.deepcopy(BINDINGS)
    if surface == "row":
        value["targets"][TARGET]["run_id"] = "wrong-run"
    else:
        bindings[TARGET]["run_id"] = "wrong-run"
    summary = metrics.summarize_efficiency([value], now=NOW, current_bindings=bindings)
    assert summary["observed_branches"]["denominator"] == 0
    assert summary["rejected"]["wrong_or_stale_branch_identity"] == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"started_at": NOW - 10, "finished_at": NOW - 11},
        {"finished_at": NOW},
        {"finished_at": None},
        {"status": "running"},
        {"work_key": ""},
    ],
)
def test_native_invalid_completed_stage_times_are_unmeasured(changes):
    value = native_receipt()
    value["targets"][TARGET]["stage_results"]["act"].update(changes)
    summary = summarize([value])
    assert summary["branch_stage_latency_seconds"]["deterministic_merge"]["total_or_null"] is None
    assert summary["rejected"]["invalid_or_incomplete_stage_timing"] == 1


def test_native_missing_stage_results_cannot_fabricate_legacy_duration():
    value = native_receipt()
    row = value["targets"][TARGET]
    row.pop("stage_results")
    row["metrics"]["stage_seconds"] = {"act": 100}
    summary = summarize([value])
    assert summary["branch_stage_latency_seconds"]["deterministic_merge"]["total_or_null"] is None
    assert summary["rejected"]["invalid_or_incomplete_stage_timing"] == 1


@pytest.mark.parametrize(
    "classification",
    ["capacity_deferred", "qualification_pending", "evidence_pending", "deterministic_wait"],
)
def test_waits_are_not_counted_as_avoided_model_calls(classification):
    summary = summarize([receipt(classification=classification)])
    assert summary["observed_branches"]["denominator"] == 0
    assert summary["observed_branches"]["deterministic_zero_reasoning"] == 0
    assert summary["observed_branches"]["excluded_waits"] == 1


def test_pending_action_without_confirmed_direct_progress_is_not_call_avoidance():
    value = receipt()
    value["targets"][TARGET]["results"]["act"] = {"status": "pending", "pending_kind": "ci"}
    assert summarize([value])["observed_branches"]["denominator"] == 0


def test_execution_verification_and_receipt_duplicates_count_run_and_cost_once():
    value = receipt(model="model-1", classification="relevant")
    event = stage_event()
    verified = copy.deepcopy(event)
    verified.update(phase="verification", event_id="verification-event")
    summary = summarize([value, copy.deepcopy(value)], stage_events=[event, event, verified])
    assert summary["observed_branches"]["denominator"] == 1
    assert summary["model_runs"]["ids"] == ["model-1"]
    assert summary["observed_cost_usd"]["total_or_null"] == 0.25
    assert summary["observed_latency_seconds"]["total_or_null"] == 3.0
    assert summary["observed_cost_usd"]["coverage"] == 1.0
    # Stage acceptance cannot become either a false-acceptance label or source completion.
    assert summary["independent_outcomes"]["false_acceptance_count"] is None
    assert summary["independent_outcomes"]["rework_count"] is None


def test_partial_and_zero_cost_preserve_observation_coverage():
    summary = summarize(
        [receipt(model="model-1"), receipt(run="pipeline-2", model="model-2")],
        stage_events=[stage_event(cost=0)],
    )
    assert summary["observed_cost_usd"] == {
        "total_or_null": 0.0,
        "measured_runs": 1,
        "total_runs": 2,
        "coverage": 0.5,
        "status": "partial",
    }


def test_finite_input_costs_cannot_overflow_into_nonfinite_summary():
    summary = summarize(
        [receipt(model="model-1"), receipt(run="pipeline-2", model="model-2")],
        stage_events=[stage_event(cost=1e308), stage_event("model-2", cost=1e308)],
    )
    assert summary["observed_cost_usd"]["total_or_null"] is None
    assert summary["observed_cost_usd"]["coverage"] == 1
    json.dumps(summary, allow_nan=False)


def test_conflicting_receipt_and_model_run_identities_are_rejected():
    value = receipt(model="model-1")
    conflicting = copy.deepcopy(value)
    conflicting["finished_at"] -= 1
    summary = summarize([value, conflicting])
    assert summary["observed_branches"]["denominator"] == 0
    assert summary["rejected"]["conflicting_receipt"] == 1
    other = receipt(run="pipeline-2", model="model-1")
    other["targets"][TARGET]["results"]["prepare"]["snapshot"]["head"] = "b" * 40
    summary = metrics.summarize_efficiency([value, other], now=NOW)
    assert summary["model_runs"]["count"] == 0
    assert summary["observed_branches"]["denominator"] == 0
    assert summary["rejected"]["conflicting_model_identity"] == 1


@pytest.mark.parametrize("field", ["head", "revision", "target"])
def test_wrong_or_stale_branch_identity_is_rejected(field):
    value = receipt(model="model-1")
    snapshot = value["targets"][TARGET]["results"]["prepare"]["snapshot"]
    snapshot[field] = "b" * 40 if field == "head" else "wrong"
    summary = summarize([value], stage_events=[stage_event()])
    assert summary["model_runs"]["count"] == 0
    assert summary["rejected"]["wrong_or_stale_branch_identity"] == 1


@pytest.mark.parametrize(
    "field,value", [("finished_at", NOW + 1), ("finished_at", 1), ("finished_at", float("nan"))]
)
def test_future_stale_or_nonfinite_receipts_are_rejected(field, value):
    retained = receipt()
    retained[field] = value
    summary = summarize([retained], max_age_seconds=60)
    assert summary["observed_branches"]["denominator"] == 0
    assert summary["rejected"]


def test_stage_event_wrong_head_or_run_cannot_supply_cost():
    wrong_head = stage_event(head_sha="b" * 40)
    wrong_run = stage_event("model-other")
    summary = summarize([receipt(model="model-1")], stage_events=[wrong_head, wrong_run])
    assert summary["observed_cost_usd"]["total_or_null"] is None
    assert summary["rejected"]["unbound_stage_event"] == 2


def test_nonfinite_and_conflicting_event_measurements_stay_unknown():
    summary = summarize(
        [receipt(model="model-1")],
        stage_events=[stage_event(cost=float("inf")), stage_event(cost=1), stage_event(cost=2)],
    )
    assert summary["observed_cost_usd"]["total_or_null"] is None
    assert summary["observed_latency_seconds"]["total_or_null"] == 3
    assert summary["rejected"]["invalid_stage_event"] == 1


def test_retained_payload_json_is_supported_without_database_read():
    event = stage_event()
    event["payload_json"] = json.dumps(event.pop("payload"))
    summary = summarize([receipt(model="model-1")], stage_events=[event])
    assert summary["observed_cost_usd"]["total_or_null"] == 0.25


def test_only_explicit_independent_same_run_head_labels_measure_outcomes():
    values = [outcome(), outcome(), outcome(run_id="other"), outcome(head_sha="b" * 40)]
    summary = summarize([receipt(model="model-1")], independent_outcomes=values)
    observed = summary["independent_outcomes"]
    assert observed["false_acceptance_count"] == 1
    assert observed["false_acceptance_rate"] == 1.0
    assert observed["rework_count"] == 0
    assert observed["rework_rate"] == 0
    assert summary["rejected"]["unbound_independent_outcome"] == 2


@pytest.mark.parametrize(
    "changes", [{"independent": False}, {"evidence_ref": {}}, {"observed_at": 0}]
)
def test_unverified_or_stale_outcome_labels_are_unmeasured(changes):
    summary = summarize(
        [receipt(model="model-1")], independent_outcomes=[outcome(**changes)], max_age_seconds=60
    )
    assert summary["independent_outcomes"]["false_acceptance_rate"] is None


def test_hold_ownership_age_and_due_counts_are_deduplicated():
    old = hold(owner=None)
    escalated = hold(hold_id="hold-2", status="escalated", age=100)
    released = hold(hold_id="hold-3", status="released", age=5000)
    summary = summarize(holds=[old, old, escalated, released])
    assert summary["holds"] == {
        "active": 1,
        "due": 1,
        "escalated": 1,
        "unowned": 1,
        "aged": 1,
        "oldest_age_seconds": 4000,
    }


def test_stale_and_conflicting_holds_do_not_reappear_after_duplicate():
    first = hold()
    conflicting = {**first, "owner": "different"}
    stale = {**hold(hold_id="hold-2"), "revision": "old"}
    summary = summarize(holds=[first, conflicting, first, stale])
    assert summary["holds"]["active"] == 0
    assert summary["rejected"]["conflicting_hold"] == 1
    assert summary["rejected"]["invalid_or_stale_hold"] == 1


@pytest.mark.parametrize("status", ["resolved", "consumed"])
def test_reconciled_or_consumed_hold_is_terminal_not_invalid_or_unowned(status):
    resolved = hold(status=status, owner=None, age=5000)
    resolved["execution_state"] = "action_outcome_unknown"
    summary = summarize(holds=[resolved])
    assert summary["holds"] == {
        "active": 0,
        "due": 0,
        "escalated": 0,
        "unowned": 0,
        "aged": 0,
        "oldest_age_seconds": None,
    }
    assert "invalid_or_stale_hold" not in summary["rejected"]


def test_without_fresh_binding_summary_is_explicitly_historical():
    summary = metrics.summarize_efficiency([receipt()], now=NOW)
    assert summary["freshness"] == "historical_freshness_unverified"


def test_summary_has_no_io_or_mutation(monkeypatch):
    value, event, held = receipt(model="model-1"), stage_event(), hold()
    before = copy.deepcopy([value, event, held])

    def forbidden(*_args, **_kwargs):
        raise AssertionError("summary attempted I/O")

    monkeypatch.setattr("builtins.open", forbidden)
    summarize([value], stage_events=[event], holds=[held])
    assert [value, event, held] == before


@pytest.mark.parametrize("now", [float("nan"), float("inf"), -1, True])
def test_nonfinite_or_invalid_summary_clock_is_rejected(now):
    with pytest.raises(ValueError, match="finite nonnegative"):
        metrics.summarize_efficiency([], now=now)


@pytest.fixture
def persisted_stage_brain(tmp_path, monkeypatch):
    """Use the existing stage writer and schema in a disposable Brain only."""
    import feedback

    database = tmp_path / "brain with spaces.db"
    monkeypatch.setattr(feedback, "DB_PATH", database)
    monkeypatch.setattr(feedback, "_capability_daily_heartbeat", lambda *_a, **_kw: None)
    monkeypatch.setattr(feedback.time, "time", lambda: NOW)
    for run, cost in (("model-1", 0.25), ("unrelated-model", 99)):
        with feedback._conn() as conn:
            conn.execute(
                "INSERT INTO runs(run_id,agent,task_type,routing_metadata) VALUES(?,?,?,?)",
                (
                    run,
                    "codex",
                    "closer_interpret",
                    json.dumps(
                        {
                            "selected_profile_id": "codex-6-luna-low",
                            "subject_id": TARGET,
                        }
                    ),
                ),
            )
        for validation in (None, {"status": "accepted"}):
            metrics.record_stage(
                {"run_id": run, "exit": 0, "cost_usd": cost},
                task_type="closer_interpret",
                target=TARGET,
                head=HEAD,
                agent="codex",
                profile_id="codex-6-luna-low",
                basis={"revision": REVISION},
                elapsed_seconds=3,
                validation=validation,
            )
    return database


def test_read_only_existing_stage_persistence_reaches_exact_receipt_summary(
    persisted_stage_brain, monkeypatch
):
    import feedback

    preliminary = summarize([receipt(model="model-1")])
    assert preliminary["model_runs"]["bindings"] == {
        "model-1": {"target": TARGET, "head_sha": HEAD}
    }
    before = persisted_stage_brain.read_bytes()

    def forbidden(*_args, **_kwargs):
        raise AssertionError("read-only lookup invoked the initializing Brain connection")

    monkeypatch.setattr(feedback, "_conn", forbidden)
    monkeypatch.setattr(feedback, "resolve_db_path", lambda: persisted_stage_brain)
    lookup = metrics.read_stage_events(preliminary["model_runs"]["bindings"])
    assert lookup["status"] == "complete" and lookup["returned_events"] == 2
    assert {row["run_id"] for row in lookup["events"]} == {"model-1"}
    assert {row["phase"] for row in lookup["events"]} == {"execution", "verification"}
    final = summarize([receipt(model="model-1")], stage_events=lookup["events"])
    assert final["observed_cost_usd"]["total_or_null"] == 0.25
    assert final["observed_latency_seconds"]["total_or_null"] == 3
    assert final["independent_outcomes"]["false_acceptance_rate"] is None
    assert final["independent_outcomes"]["rework_rate"] is None
    assert persisted_stage_brain.read_bytes() == before


@pytest.mark.parametrize(
    "field,value",
    [
        ("requested_profile_id", "other-profile"),
        ("agent", "claude"),
        ("task_category", "other-task"),
        ("target", "wrong/repo#1"),
        ("head_sha", "b" * 40),
        ("capacity_ledger_run_id", "unrelated-model"),
        ("observed_cost_usd", float("inf")),
    ],
)
def test_persisted_event_bindings_and_finite_measurements_are_rechecked(
    persisted_stage_brain, field, value
):
    import hashlib

    with sqlite3.connect(persisted_stage_brain) as conn:
        events = conn.execute(
            "SELECT event_id,payload_json FROM completion_events WHERE run_id='model-1'"
        ).fetchall()
        for event_id, raw in events:
            payload = json.loads(raw)
            payload["closer_stage"][field] = value
            encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
            digest = "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()
            conn.execute(
                "UPDATE completion_events SET payload_json=?,content_hash=? WHERE event_id=?",
                (encoded, digest, event_id),
            )
    lookup = metrics.read_stage_events(
        {"model-1": {"target": TARGET, "head_sha": HEAD}}, db_path=persisted_stage_brain
    )
    assert lookup["events"] == []
    assert lookup["status"] == "partial" and lookup["rejected_events"] == 2


def test_lookup_is_bounded_and_reports_truncation_and_oversized_payload(persisted_stage_brain):
    bindings = {"model-1": {"target": TARGET, "head_sha": HEAD}}
    truncated = metrics.read_stage_events(bindings, db_path=persisted_stage_brain, max_events=1)
    assert truncated["status"] == "partial" and truncated["truncated"] is True
    assert truncated["returned_events"] == 1
    oversized = metrics.read_stage_events(
        bindings, db_path=persisted_stage_brain, max_payload_bytes=1
    )
    assert oversized["status"] == "partial" and oversized["rejected_events"] == 2
    assert oversized["events"] == []


def test_empty_and_over_limit_run_sets_do_not_open_or_create_a_database(tmp_path, monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("empty or oversized run set attempted a database read")

    monkeypatch.setattr(metrics.sqlite3, "connect", forbidden)
    assert metrics.read_stage_events({})["status"] == "empty"
    lookup = metrics.read_stage_events(
        {
            "one": {"target": TARGET, "head_sha": HEAD},
            "two": {"target": TARGET, "head_sha": HEAD},
        },
        max_runs=1,
        db_path=tmp_path / "missing.db",
    )
    assert lookup["status"] == "unavailable" and lookup["truncated"] is True


def test_missing_and_old_schema_brains_remain_unavailable_without_migration(tmp_path):
    bindings = {"model-1": {"target": TARGET, "head_sha": HEAD}}
    missing = tmp_path / "missing.db"
    assert metrics.read_stage_events(bindings, db_path=missing)["status"] == "unavailable"
    assert not missing.exists()
    empty = tmp_path / "old-schema.db"
    sqlite3.connect(empty).close()
    before = empty.read_bytes()
    assert metrics.read_stage_events(bindings, db_path=empty)["status"] == "unavailable"
    assert empty.read_bytes() == before


def test_conflicting_model_identity_exposes_no_read_binding():
    one = receipt(model="model-1")
    two = receipt(run="pipeline-2", model="model-1")
    two["targets"][TARGET]["results"]["prepare"]["snapshot"]["head"] = "b" * 40
    summary = metrics.summarize_efficiency([one, two], now=NOW)
    assert summary["model_runs"]["bindings"] == {}
