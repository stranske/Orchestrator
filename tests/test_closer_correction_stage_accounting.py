"""Execution-only accounting through actual inert runs and a private Brain.

Source/verifier inputs and role/auth remain synthetic. No risk correctness,
ordinary integration, marginal billing or remote authority is inferred.
"""

import json

import pytest
from closer_inert_dispatcher_support import LOW
from test_closer_correction_producer import controlled_world as _controlled_world
from test_closer_correction_producer import local as _producer_local
from test_closer_correction_producer import replay
from test_closer_correction_producer import semantic_inputs as _semantic_inputs

import closer_correction_producer as producer
import closer_role_qualification as roles
import closer_stage_feedback as stage
import dispatcher
import feedback

controlled_world = _controlled_world
semantic_inputs = _semantic_inputs
local = _producer_local
_REAL_RECORD_RUN = feedback.record_run


@pytest.fixture
def private_brain(local, monkeypatch):
    path = local.world.root / "private-brain" / "accounting.db"
    monkeypatch.setattr(feedback, "DB_PATH", path)
    monkeypatch.setattr(feedback, "_capability_daily_heartbeat", lambda *a, **k: None)
    # The base inert fixture suppresses feedback writes. Restore the real run
    # writer here so the actual broker, not a test-created run, supplies identity.
    monkeypatch.setattr(feedback, "record_run", _REAL_RECORD_RUN)
    with feedback._conn():
        pass
    return path


def events():
    with feedback._conn() as conn:
        rows = conn.execute(
            "SELECT event_id,run_id,phase,status,payload_json FROM completion_events WHERE event_type='closer_stage'"
        ).fetchall()
    return [(row[0], row[1], row[2], row[3], json.loads(row[4])["closer_stage"]) for row in rows]


def test_actual_run_is_joined_once_with_unknown_quality_and_replay_is_read_only(
    local, private_brain, monkeypatch
):
    with local.lease():
        result = local.run()
    assert result["status"] == "materialized", result
    recorded = events()
    assert len(recorded) == 1
    event_id, run_id, phase, status, payload = recorded[0]
    assert run_id == result["run_id"] == local.caller.results[0]["run_id"]
    assert phase == "execution" and status == "unknown"
    assert payload["task_category"] == producer.TASK
    assert payload["requested_profile_id"] == LOW
    assert payload["target"] == local.basis["target"]
    assert payload["capacity_ledger_run_id"] == run_id
    assert payload["accepted"] is None
    assert payload["independent_validation"] is False
    assert payload["validation_status"] is None
    assert payload["source_completion"] is False
    assert payload["guarded_operation_success"] is None
    assert payload["actual_marginal_dollars"] is None
    assert payload["opportunity_cost"] is None
    assert payload["elapsed_seconds"] > 0
    with feedback._conn() as conn:
        row = conn.execute(
            "SELECT agent,task_type,routing_metadata FROM runs WHERE run_id=?", (run_id,)
        ).fetchone()
        assert row[0] == "claude" and row[1] == producer.TASK
        metadata = json.loads(row[2])
        assert metadata["selected_profile_id"] == LOW
        assert metadata["subject_id"] == local.basis["target"]
        assert feedback._closer_stage_rewards(conn, producer.TASK) == {}
    lookup = stage.read_stage_events(
        {run_id: {"target": local.basis["target"], "head_sha": local.basis["head"]}},
        db_path=private_brain,
    )
    assert lookup["status"] == "complete" and lookup["returned_events"] == 1
    assert lookup["events"][0]["run_id"] == run_id
    assert lookup["events"][0]["payload"]["closer_stage"]["task_category"] == producer.TASK
    # Same actual run+basis cannot create a second execution event.
    for _ in range(2):
        stage.record_stage(
            local.caller.results[0],
            task_type=producer.TASK,
            target=local.basis["target"],
            head=local.basis["head"],
            agent="claude",
            profile_id=LOW,
            basis=result["basis"],
            elapsed_seconds=payload["elapsed_seconds"],
            validation=None,
        )
    assert events() == recorded
    monkeypatch.setattr(dispatcher, "offload", lambda *a, **k: pytest.fail("replay invoked broker"))
    assert replay(local, result)["status"] == "materialized"
    assert events() == recorded
    assert len(local.caller.results) == 1
    # Preserve private original ledger/captured proof for independent review.
    (local.world.root / "stage-accounting-review.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "event_id": event_id,
                "db_path": str(private_brain),
                "provider_result_ref": result["proof"]["provider_result_ref"],
                "request_ref": result["request_ref"],
                "basis": result["basis"],
                "event": recorded[0],
            },
            sort_keys=True,
        )
    )


def test_true_prestart_refusal_has_no_stage_event_or_run(local, private_brain, monkeypatch):
    monkeypatch.setattr(roles, "ROLE_QUALIFICATIONS", {})
    with local.lease():
        result = local.run()
    assert result["status"] == "owned_unknown"
    assert result["actual"]["process_started"] is False
    assert events() == []
    with feedback._conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
    assert local.world.events["auth"] == 0


def test_actual_native_unknown_is_execution_once_without_quality_or_retry(
    local, private_brain, monkeypatch
):
    def broker(agent, prompt, **options):
        return local.caller.response(
            {"exit": 0, "output": json.dumps(local.answer), "outcome_uncertain": True},
            agent,
            prompt,
            **options
        )

    monkeypatch.setattr(dispatcher, "offload", broker)
    with local.lease():
        result = local.run()
    assert result["status"] == "owned_unknown" and result["retry_safe"] is False
    assert len(local.caller.results) == 1
    recorded = events()
    assert len(recorded) == 1
    assert recorded[0][1] == result["actual"]["run_id"]
    assert recorded[0][2:4] == ("execution", "unknown")
    assert recorded[0][4]["accepted"] is None
    assert recorded[0][4]["independent_validation"] is False


@pytest.mark.parametrize(
    "field,value",
    [
        ("selected_profile_id", None),
        ("selected_profile_id", "foreign-profile"),
        ("subject_id", "foreign/repo#3"),
    ],
)
def test_missing_profile_or_wrong_target_ledger_never_gets_attributed(
    local, private_brain, monkeypatch, field, value
):
    def mismatched_run(*args, **kwargs):
        _REAL_RECORD_RUN(*args, **kwargs)
        run_id = args[0] if args else kwargs["run_id"]
        with feedback._conn() as conn:
            row = conn.execute(
                "SELECT routing_metadata FROM runs WHERE run_id=?", (run_id,)
            ).fetchone()
            metadata = json.loads(row[0])
            if value is None:
                metadata.pop(field, None)
            else:
                metadata[field] = value
            conn.execute(
                "UPDATE runs SET routing_metadata=? WHERE run_id=?", (json.dumps(metadata), run_id)
            )

    monkeypatch.setattr(feedback, "record_run", mismatched_run)
    with local.lease():
        result = local.run()
    assert result["status"] == "materialized", result
    assert events() == []
    assert len(local.caller.results) == 1


@pytest.mark.parametrize("fault", ["writer", "database"])
def test_telemetry_failure_cannot_change_semantics_or_repeat_invocation(
    local, private_brain, monkeypatch, fault
):
    if fault == "writer":

        def unavailable(*a, **k):
            raise RuntimeError("injected telemetry failure")

        monkeypatch.setattr(stage, "record_stage", unavailable)
    else:
        real = stage.record_stage

        def unavailable(*a, **k):
            with monkeypatch.context() as patch:
                patch.setattr(
                    feedback,
                    "_conn",
                    lambda *a, **k: (_ for _ in ()).throw(OSError("private DB unavailable")),
                )
                return real(*a, **k)

        monkeypatch.setattr(stage, "record_stage", unavailable)
    with local.lease():
        result = local.run()
    assert result["status"] == "materialized", result
    assert not result["act_authorized"] and result["retry_safe"] is False
    assert len(local.caller.results) == 1
    assert events() == []


def owner_envelope(local, result):
    """Synthetic metrics-unit placement, not an authenticated pipeline receipt.

    The real source owner must persist this result in its authenticated branch.
    This fixture only exercises the unchanged pure summary's exact stage shape;
    all model run IDs below came from the actual inert broker's private ledger.
    """
    import time

    target = local.basis["target"]
    revision = local.basis["revision"]
    finished = time.time()
    receipt = {
        "schema": "closer-pipeline-receipt/v1",
        "run_id": "synthetic-owner-branch-unit",
        "finished_at": finished,
        "targets": {
            target: {
                "revision": revision,
                "status": "pending",
                "results": {
                    "prepare": {
                        "revision": revision,
                        "snapshot": {
                            "target": target,
                            "revision": revision,
                            "head": local.basis["head"],
                        },
                    },
                    "interpret": result,
                },
            }
        },
    }
    return receipt, finished, {target: {"revision": revision, "head": local.basis["head"]}}


@pytest.mark.parametrize("kind", ["success", "semantic_refusal", "native_unknown"])
def test_exact_owner_stage_placement_discovers_original_once_without_quality(
    local, private_brain, monkeypatch, kind
):
    if kind == "semantic_refusal":
        local.answer["findings"][0]["classification"] = "relevant"
    elif kind == "native_unknown":

        def broker(agent, prompt, **options):
            return local.caller.response(
                {"exit": 0, "output": json.dumps(local.answer), "outcome_uncertain": True},
                agent,
                prompt,
                **options
            )

        monkeypatch.setattr(dispatcher, "offload", broker)
    with local.lease():
        result = local.run()
    assert result["status"] == ("materialized" if kind == "success" else "owned_unknown"), result
    actual = result["actual"]
    assert actual["run_id"] == local.caller.results[0]["run_id"]
    assert actual["profile_id"] == LOW
    assert "_closer_controlled_proof" not in actual
    assert "data_only_origin" not in actual
    assert len(json.dumps(actual).encode()) < 1024
    if kind != "native_unknown":
        assert result["proof"]["run_id"] == actual["run_id"]
    receipt, now, bindings = owner_envelope(local, result)
    preliminary = stage.summarize_efficiency([receipt, receipt], now=now, current_bindings=bindings)
    assert preliminary["model_runs"]["ids"] == [actual["run_id"]]
    lookup = stage.read_stage_events(preliminary["model_runs"]["bindings"], db_path=private_brain)
    assert lookup["returned_events"] == 1, lookup
    summary = stage.summarize_efficiency(
        [receipt, receipt], stage_events=lookup["events"], now=now, current_bindings=bindings
    )
    assert summary["model_runs"]["count"] == 1
    assert summary["model_runs"]["stage_event_coverage"] == 1
    assert summary["observed_latency_seconds"]["measured_runs"] == 1
    assert summary["independent_outcomes"]["false_acceptance_rate"] is None
    assert summary["independent_outcomes"]["rework_rate"] is None
    assert events()[0][4]["accepted"] is None
    assert events()[0][4]["independent_validation"] is False
    # A different branch target/head cannot acquire this actual run's event.
    wrong = stage.read_stage_events(
        {actual["run_id"]: {"target": "foreign/repo#1", "head_sha": local.basis["head"]}},
        db_path=private_brain,
    )
    assert wrong["events"] == []


def test_prestart_bookkeeping_id_is_not_a_discoverable_model_run(local, private_brain, monkeypatch):
    import closer_dispatcher

    # Unit-only known-no-spawn shape; no origin/qualification claim. Broker is
    # never called. A bookkeeping ID cannot become a measured invocation.
    monkeypatch.setattr(
        closer_dispatcher,
        "_controlled_offload",
        lambda **k: {"exit": 2, "process_started": False, "run_id": "bookkeeping-only"},
    )
    with local.lease():
        result = local.run()
    assert "run_id" not in result["actual"]
    assert result["original_run_id"] == "bookkeeping-only"
    receipt, now, bindings = owner_envelope(local, result)
    summary = stage.summarize_efficiency([receipt], now=now, current_bindings=bindings)
    assert summary["model_runs"]["count"] == 0
    assert events() == [] and local.world.events["auth"] == 0


@pytest.mark.parametrize("fault", ["profile", "target", "receipt_hash", "origin"])
def test_unknown_accounting_refuses_foreign_physical_identity(
    local, private_brain, monkeypatch, fault
):
    import copy

    def broker(agent, prompt, **options):
        return local.caller.response(
            {"exit": 0, "output": json.dumps(local.answer), "outcome_uncertain": True},
            agent,
            prompt,
            **options
        )

    monkeypatch.setattr(dispatcher, "offload", broker)
    with local.lease():
        result = local.run()
    assert result["status"] == "owned_unknown"
    actual = copy.deepcopy(local.caller.results[0])
    profile = LOW
    basis = copy.deepcopy(local.basis)
    if fault == "profile":
        profile = "foreign-profile"
    elif fault == "target":
        basis["target"] = "foreign/repo#3"
    elif fault == "receipt_hash":
        actual["provider_result_ref"]["sha256"] = "0" * 64
    else:
        actual["data_only_origin"]["task_type"] = "closer_interpret"
    assert (
        producer.original_accounting_actual(
            actual,
            profile_id=profile,
            basis=basis,
            frontier=local.frontier,
            receipt_root=dispatcher.DISPATCH_LOG_DIR,
        )
        is None
    )
    assert len(local.caller.results) == 1
