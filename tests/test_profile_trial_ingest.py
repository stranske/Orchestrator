from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

import feedback
import model_profile_trial

TRIAL_FEEDBACK_TABLES = (
    "runs",
    "execution_attempts",
    "outcomes",
    "execution_traces",
    "completion_events",
    "influence_edges",
    "profile_trial_ingests",
)


def _assert_trial_feedback_tables_empty(conn) -> None:
    for table in TRIAL_FEEDBACK_TABLES:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        count = 0 if not exists else conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        assert count == 0, table


@pytest.fixture
def trial_roots(tmp_path):
    orchestrator = tmp_path / "orchestrator-source"
    workflows = tmp_path / "workflows-source"
    orchestrator.mkdir()
    workflows.mkdir()
    (orchestrator / "control.py").write_text("CONTROL = 'unchanged'\n")
    (workflows / "worker.yml").write_text("worker: read-only\n")
    return orchestrator, workflows


@pytest.fixture
def trial_manifest(trial_roots):
    return model_profile_trial.build_trial_manifest(
        *trial_roots,
        seed=14,
        now=1_000,
        capacity_state="ok",
    )


@pytest.fixture
def trial_attempt_fixture(trial_manifest):
    attempts = []
    for request in trial_manifest["requests"]:
        profile_id = request["profile_id"]
        selected = request["requested_model"]
        artifact_ref = f"workflows:trial:{profile_id}"
        attempts.append(
            {
                "run_id": request["run_id"],
                "profile_id": profile_id,
                "operation_role": "worker",
                "requested_model": request["requested_model"],
                "selected_model": selected,
                "reported_model": selected,
                "provider_resolved_provider": "openai",
                "provider_resolved_model": selected,
                "fallback_reason": None,
                "runner_version": "workflows/reusable-codex-run@profile-contract-v1",
                "cli_version": "codex-cli 0.144.0-alpha.4 (app-bundled)",
                "status": "success",
                "latency_s": 1.5,
                "tokens_in": 12,
                "tokens_out": 4,
                "artifact_ref": artifact_ref,
                "packet_hash": trial_manifest["packet_hash"],
                "acknowledged": True,
                "identity_evidence": {
                    "schema": model_profile_trial.IDENTITY_EVIDENCE_SCHEMA,
                    "version": model_profile_trial.IDENTITY_EVIDENCE_VERSION,
                    "authority": "workflows-read-only-trial-artifact/v1",
                    "artifact_ref": artifact_ref,
                    "artifact_sha256": "sha256:" + ("a" * 64),
                },
            }
        )
    terra_run = next(
        request["run_id"]
        for request in trial_manifest["requests"]
        if request["profile_id"] == "codex-5.6-terra-high"
    )
    return {
        "schema": model_profile_trial.RESULT_SCHEMA,
        "version": 1,
        "trial_id": trial_manifest["trial_id"],
        "packet_hash": trial_manifest["packet_hash"],
        "acknowledged": True,
        "attempts": attempts,
        "auxiliary_traces": [
            {
                "run_id": terra_run,
                "trace_id": "terra-evaluator-trace",
                "operation": "evaluate_pr_compare",
                "operation_role": "evaluator",
                "provider": "anthropic",
                "model": "claude-evaluator-only",
                "status": "success",
            }
        ],
    }


def _use_temp_feedback(tmp_path, monkeypatch):
    db = tmp_path / "quarantine-feedback.db"
    monkeypatch.setattr(feedback, "DB_PATH", db)
    return db


@pytest.mark.parametrize(
    ("writer", "fail_on"),
    [
        ("_record_run_in_conn", 2),
        ("_record_outcome_in_conn", 2),
        ("record_execution_trace", 1),
    ],
)
def test_ingest_is_one_transaction_and_a_mid_write_failure_leaves_nothing(
    tmp_path, monkeypatch, trial_manifest, trial_attempt_fixture, writer, fail_on
):
    _use_temp_feedback(tmp_path, monkeypatch)
    calls = {"n": 0}
    original = getattr(feedback, writer)

    def flaky(*args, **kwargs):
        calls["n"] += 1
        value = original(*args, **kwargs)
        if calls["n"] == fail_on:
            raise RuntimeError("simulated mid-write failure")
        return value

    monkeypatch.setattr(feedback, writer, flaky)
    with pytest.raises(RuntimeError, match="simulated mid-write"):
        model_profile_trial.finalize_trial(
            trial_manifest, trial_attempt_fixture, record_feedback=True, now=2_000
        )
    assert calls["n"] == fail_on
    with feedback._conn() as conn:
        for table in TRIAL_FEEDBACK_TABLES:
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0, table


def test_omitted_trial_usage_stays_null_and_explicit_zero_stays_zero(
    tmp_path, monkeypatch, trial_manifest, trial_attempt_fixture
):
    """Missing token counts are unmeasured. Storing them as zero would train the trial as free."""
    _use_temp_feedback(tmp_path, monkeypatch)
    attempts = model_profile_trial._validate_results(trial_manifest, trial_attempt_fixture)
    attempts[0]["tokens_in"] = None
    attempts[0]["tokens_out"] = None
    attempts[1]["tokens_in"] = 0
    attempts[1]["tokens_out"] = 0
    feedback.ingest_profile_trial(
        trial_attempt_fixture,
        manifest=trial_manifest,
        attempts=attempts,
        auxiliary_traces=trial_attempt_fixture["auxiliary_traces"],
        ts=2_000,
    )
    with feedback._conn() as conn:
        rows = conn.execute(
            "SELECT attempt_id, tokens_in, tokens_out FROM execution_attempts "
            "WHERE attempt_id LIKE 'attempt:trial:%' ORDER BY attempt_id"
        ).fetchall()
    by_id = {row[0]: (row[1], row[2]) for row in rows}
    assert by_id[f"attempt:trial:{attempts[0]['run_id']}"] == (None, None)
    assert by_id[f"attempt:trial:{attempts[1]['run_id']}"] == (0, 0)
    assert by_id[f"attempt:trial:{attempts[2]['run_id']}"] == (
        attempts[2]["tokens_in"],
        attempts[2]["tokens_out"],
    )


def test_ingest_is_idempotent_on_trial_id(
    tmp_path, monkeypatch, trial_manifest, trial_attempt_fixture
):
    _use_temp_feedback(tmp_path, monkeypatch)
    attempts = model_profile_trial._validate_results(trial_manifest, trial_attempt_fixture)
    first = feedback.ingest_profile_trial(
        trial_attempt_fixture,
        manifest=trial_manifest,
        attempts=attempts,
        auxiliary_traces=trial_attempt_fixture["auxiliary_traces"],
        ts=2_000,
    )
    tables = (
        "runs",
        "execution_attempts",
        "outcomes",
        "execution_traces",
        "completion_events",
        "influence_edges",
        "profile_trial_ingests",
        "route_weights",
        "route_weights_v2",
    )
    with feedback._conn() as conn:
        before = {
            table: conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
            for table in tables
        }
        outcomes = conn.execute(
            "SELECT run_id,verifier_verdict,adjudicated_verdict,merged,ci_status,durability "
            "FROM outcomes ORDER BY run_id"
        ).fetchall()
        assert outcomes == [
            (run_id, None, None, None, None, "pending")
            for run_id in sorted(attempt["run_id"] for attempt in attempts)
        ]
        assert len(before["runs"]) == len(outcomes) == 3
        worker_attempts = conn.execute(
            "SELECT run_id FROM execution_attempts WHERE operation_role='worker' ORDER BY run_id"
        ).fetchall()
        assert worker_attempts == [(row[0],) for row in outcomes]
        assert len(before["execution_attempts"]) == 4  # Three workers and the evaluator trace.
        assert len(before["execution_traces"]) == 1
    second = feedback.ingest_profile_trial(
        trial_attempt_fixture,
        manifest=trial_manifest,
        attempts=attempts,
        auxiliary_traces=trial_attempt_fixture["auxiliary_traces"],
        ts=2_001,
    )
    assert first["status"] == "ingested"
    assert second["status"] == "already_ingested"
    assert second["applied_ts"] == first["applied_ts"] == 2_000
    assert len(first["recorded_attempt_ids"]) == 3
    assert second["recorded_attempt_ids"] == []
    with feedback._conn() as conn:
        after = {
            table: conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
            for table in tables
        }
    assert after == before
    assert len(after["profile_trial_ingests"]) == 1


def test_ingest_refuses_mismatched_trial_id_and_leaves_nothing(
    tmp_path, monkeypatch, trial_manifest, trial_attempt_fixture
):
    """The marker and the rows must name one trial.

    A split id used to seal the result id and write the manifest id.
    """
    _use_temp_feedback(tmp_path, monkeypatch)
    attempts = model_profile_trial._validate_results(trial_manifest, trial_attempt_fixture)
    forged = copy.deepcopy(trial_attempt_fixture)
    forged["trial_id"] = trial_manifest["trial_id"] + "-other"
    with pytest.raises(ValueError, match="does not match the manifest"):
        feedback.ingest_profile_trial(
            forged,
            manifest=trial_manifest,
            attempts=attempts,
            auxiliary_traces=forged["auxiliary_traces"],
            ts=2_000,
        )
    with feedback._conn() as conn:
        _assert_trial_feedback_tables_empty(conn)
    sealed = feedback.ingest_profile_trial(
        trial_attempt_fixture,
        manifest=trial_manifest,
        attempts=attempts,
        auxiliary_traces=trial_attempt_fixture["auxiliary_traces"],
        ts=2_000,
    )
    assert sealed["status"] == "ingested"
    with feedback._conn() as conn:
        assert conn.execute("SELECT trial_id FROM profile_trial_ingests").fetchone()[0] == (
            trial_manifest["trial_id"]
        )
        assert conn.execute("SELECT DISTINCT target FROM runs").fetchone()[0] == (
            trial_manifest["trial_id"]
        )


def test_ingest_refuses_to_seal_an_empty_attempt_list(
    tmp_path, monkeypatch, trial_manifest, trial_attempt_fixture
):
    """Sealing a trial id that wrote no rows drops the real ingest as already applied."""
    _use_temp_feedback(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="requires attempts"):
        feedback.ingest_profile_trial(
            trial_attempt_fixture,
            manifest=trial_manifest,
            attempts=[],
            ts=2_000,
        )
    attempts = model_profile_trial._validate_results(trial_manifest, trial_attempt_fixture)
    sealed = feedback.ingest_profile_trial(
        trial_attempt_fixture,
        manifest=trial_manifest,
        attempts=attempts,
        auxiliary_traces=trial_attempt_fixture["auxiliary_traces"],
        ts=2_000,
    )
    assert sealed["status"] == "ingested"
    assert len(sealed["recorded_attempt_ids"]) == len(attempts)


def test_ingest_stores_rows_under_the_result_trial_id_when_the_manifest_omits_it(
    tmp_path, monkeypatch, trial_manifest, trial_attempt_fixture
):
    _use_temp_feedback(tmp_path, monkeypatch)
    attempts = model_profile_trial._validate_results(trial_manifest, trial_attempt_fixture)
    manifest = copy.deepcopy(trial_manifest)
    manifest.pop("trial_id")
    sealed = feedback.ingest_profile_trial(
        trial_attempt_fixture,
        manifest=manifest,
        attempts=attempts,
        auxiliary_traces=trial_attempt_fixture["auxiliary_traces"],
        ts=2_000,
    )
    assert sealed["trial_id"] == trial_attempt_fixture["trial_id"]
    with feedback._conn() as conn:
        assert conn.execute("SELECT trial_id FROM profile_trial_ingests").fetchone()[0] == (
            trial_attempt_fixture["trial_id"]
        )
        targets = {row[0] for row in conn.execute("SELECT target FROM runs")}
        metadata = [json.loads(row[0]) for row in conn.execute("SELECT routing_metadata FROM runs")]
    assert targets == {trial_attempt_fixture["trial_id"]}
    assert {row["trial_id"] for row in metadata} == {trial_attempt_fixture["trial_id"]}


def test_finalize_refuses_ingest_when_identity_is_unverified(
    tmp_path, monkeypatch, trial_manifest, trial_attempt_fixture
):
    _use_temp_feedback(tmp_path, monkeypatch)
    trial_attempt_fixture["identity_verified"] = False
    with pytest.raises(ValueError, match="identity is unverified"):
        model_profile_trial.finalize_trial(
            trial_manifest,
            trial_attempt_fixture,
            record_feedback=True,
            ingest_brain=True,
            now=2_000,
        )
    with feedback._conn() as conn:
        _assert_trial_feedback_tables_empty(conn)


def test_finalize_refuses_ingest_when_identity_verified_is_omitted(
    tmp_path, monkeypatch, trial_manifest, trial_attempt_fixture
):
    _use_temp_feedback(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="identity is unverified"):
        model_profile_trial.finalize_trial(
            trial_manifest,
            trial_attempt_fixture,
            record_feedback=True,
            ingest_brain=True,
            now=2_000,
        )
    with feedback._conn() as conn:
        _assert_trial_feedback_tables_empty(conn)


def _bind_authoritative_identity(tmp_path, trial_attempt_fixture):
    """Fake an independently authenticated provider lookup for offline ingestion tests."""
    responses = {}
    artifact_dir = tmp_path / "identity-artifacts"
    artifact_dir.mkdir()
    for ordinal, attempt in enumerate(trial_attempt_fixture["attempts"], start=1):
        response_id = f"resp_fixture{ordinal}"
        responses[response_id] = {
            "object": "response",
            "id": response_id,
            "status": "completed",
            "model": attempt["provider_resolved_model"],
            "metadata": {key: attempt[key] for key in ("run_id", "profile_id", "packet_hash")},
            "usage": {"input_tokens": attempt["tokens_in"], "output_tokens": attempt["tokens_out"]},
        }
        artifact_path = artifact_dir / f"{attempt['profile_id']}-identity.json"
        artifact_path.write_text(
            json.dumps(
                {
                    "object": "response",
                    "response_id": response_id,
                    "profile_id": attempt["profile_id"],
                    "acknowledged": True,
                    "provider_resolved_provider": attempt["provider_resolved_provider"],
                    "provider_resolved_model": attempt["provider_resolved_model"],
                }
            ),
            encoding="utf-8",
        )
        digest = "sha256:" + hashlib.sha256(artifact_path.read_bytes()).hexdigest()
        attempt["artifact_ref"] = str(artifact_path)
        attempt["identity_evidence"]["artifact_ref"] = str(artifact_path)
        attempt["identity_evidence"]["artifact_sha256"] = digest
    trial_attempt_fixture["identity_verified"] = True
    return responses.get


def test_finalize_writes_measured_quality_by_profile(
    tmp_path, monkeypatch, trial_manifest, trial_attempt_fixture
):
    _use_temp_feedback(tmp_path, monkeypatch)
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path))
    results = copy.deepcopy(trial_attempt_fixture)
    lookup = _bind_authoritative_identity(tmp_path, results)
    quality = {
        "codex-6-astra-high": 0.82,
        "codex-5.6-terra-high": 0.76,
        "codex-5.6-luna-high": 0.79,
    }
    results["quality_by_profile"] = quality
    model_profile_trial.finalize_trial(
        trial_manifest,
        results,
        record_feedback=True,
        ingest_brain=True,
        provider_response_lookup=lookup,
        now=2_000,
    )
    summary = json.loads(
        (tmp_path / "capability-program" / "profile-trial.json").read_text(encoding="utf-8")
    )
    assert summary["identity_verified"] is True
    assert summary["brain_ingest_enabled"] is True
    assert summary["quality_by_profile"] == quality


def test_local_identity_claims_require_an_independent_provider_lookup(
    tmp_path, monkeypatch, trial_manifest, trial_attempt_fixture
):
    _use_temp_feedback(tmp_path, monkeypatch)
    _bind_authoritative_identity(tmp_path, trial_attempt_fixture)
    # The fixture has matching digests, response IDs, models, and a forged true
    # flag. None of those bytes authenticate their own provenance.
    with pytest.raises(ValueError, match="identity evidence is not authoritative"):
        model_profile_trial.finalize_trial(trial_manifest, trial_attempt_fixture, ingest_brain=True)
    with feedback._conn() as conn:
        _assert_trial_feedback_tables_empty(conn)


@pytest.mark.parametrize(
    "response_id",
    [
        pytest.param(..., id="missing"),
        pytest.param(None, id="explicit-null"),
        "",
        "resp_",
        "request_123",
        "resp_bad/id",
        12,
        "resp_forged",
        True,
        [],
        {},
        "resp_ñ",
        "resp_a\n",
        "resp_a b",
        "resp_a-b",
        "resp_a_b",
        pytest.param("resp_fixture2", id="another-attempt-response-2"),
        pytest.param("resp_fixture3", id="another-attempt-response-3"),
    ],
)
def test_invalid_or_unconfirmed_response_ids_refuse_even_with_a_trusted_lookup(
    tmp_path, monkeypatch, trial_manifest, trial_attempt_fixture, response_id
):
    _use_temp_feedback(tmp_path, monkeypatch)
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path))
    lookup = _bind_authoritative_identity(tmp_path, trial_attempt_fixture)
    lookup_requests = []

    def tracked_lookup(response_id):
        lookup_requests.append(response_id)
        return lookup(response_id)

    attempt = trial_attempt_fixture["attempts"][0]
    path = Path(attempt["identity_evidence"]["artifact_ref"])
    payload = json.loads(path.read_text())
    if response_id is ...:
        payload.pop("response_id")
    else:
        payload["response_id"] = response_id
    path.write_text(json.dumps(payload))
    attempt["identity_evidence"]["artifact_sha256"] = (
        "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    )
    state_path = tmp_path / "refused-state.json"
    with pytest.raises(ValueError, match="identity evidence is not authoritative"):
        model_profile_trial.finalize_trial(
            trial_manifest,
            trial_attempt_fixture,
            state_path=state_path,
            record_feedback=True,
            ingest_brain=True,
            provider_response_lookup=tracked_lookup,
        )
    # Malformed IDs never reach the authenticated channel. Syntactically valid
    # IDs still need a response bound to this attempt, even if another exists.
    if response_id in ("resp_forged", "resp_fixture2", "resp_fixture3"):
        assert response_id in lookup_requests
    else:
        assert response_id not in lookup_requests
    assert not state_path.exists()
    assert not (tmp_path / "capability-program" / "profile-trial.json").exists()
    with feedback._conn() as conn:
        _assert_trial_feedback_tables_empty(conn)


@pytest.mark.parametrize("attempt_index", [0, 1, 2], ids=["first", "middle", "last"])
@pytest.mark.parametrize(
    "field",
    [
        "object",
        "id",
        "model",
        "status",
        "metadata.run_id",
        "metadata.profile_id",
        "metadata.packet_hash",
        "usage.input_tokens",
        "usage.output_tokens",
        "unavailable",
        "lookup-error",
    ],
)
def test_provider_response_must_independently_confirm_the_trial_measurement(
    tmp_path, monkeypatch, trial_manifest, trial_attempt_fixture, field, attempt_index
):
    """Every attempt needs confirmation before any trial feedback or state is written."""
    _use_temp_feedback(tmp_path, monkeypatch)
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path))
    lookup = _bind_authoritative_identity(tmp_path, trial_attempt_fixture)
    # Validation orders attempts by profile, independently of their launch order.
    attempts = model_profile_trial._validate_results(trial_manifest, trial_attempt_fixture)
    response_ids = [
        json.loads(Path(attempt["identity_evidence"]["artifact_ref"]).read_text())["response_id"]
        for attempt in attempts
    ]
    assert model_profile_trial._authoritative_identity_verified(
        attempts, provider_response_lookup=lookup
    )
    bad_response_id = response_ids[attempt_index]
    response = lookup(bad_response_id)
    if "." in field:
        parent, child = field.split(".")
        response[parent][child] = (
            "another-trial" if parent == "metadata" else response[parent][child] + 1
        )
    elif field not in {"unavailable", "lookup-error"}:
        response[field] = "forged"
    lookup_requests = []

    def tracked_lookup(response_id):
        lookup_requests.append(response_id)
        if response_id == bad_response_id and field == "unavailable":
            return None
        if response_id == bad_response_id and field == "lookup-error":
            raise RuntimeError("provider lookup unavailable")
        return lookup(response_id)

    error = RuntimeError if field == "lookup-error" else ValueError
    message = "provider lookup unavailable" if field == "lookup-error" else "not authoritative"
    state_path = tmp_path / "refused-state.json"
    with pytest.raises(error, match=message):
        model_profile_trial.finalize_trial(
            trial_manifest,
            trial_attempt_fixture,
            state_path=state_path,
            record_feedback=True,
            ingest_brain=True,
            provider_response_lookup=tracked_lookup,
        )
    assert lookup_requests == response_ids[: attempt_index + 1]
    assert not state_path.exists()
    assert not (tmp_path / "capability-program" / "profile-trial.json").exists()
    with feedback._conn() as conn:
        _assert_trial_feedback_tables_empty(conn)


@pytest.mark.parametrize(
    "usage",
    [
        pytest.param(..., id="missing-usage"),
        pytest.param(None, id="null-usage"),
        pytest.param([], id="list-usage"),
        pytest.param("unmeasured", id="string-usage"),
        pytest.param({}, id="empty-usage"),
        pytest.param({"input_tokens": 12}, id="missing-output"),
        pytest.param({"output_tokens": 4}, id="missing-input"),
        pytest.param({"input_tokens": 12.0, "output_tokens": 4}, id="equal-float-input"),
        pytest.param({"input_tokens": 12, "output_tokens": 4.0}, id="equal-float-output"),
        pytest.param({"input_tokens": True, "output_tokens": 4}, id="boolean-input"),
        pytest.param({"input_tokens": 12, "output_tokens": True}, id="boolean-output"),
        pytest.param({"input_tokens": -1, "output_tokens": 4}, id="negative-input"),
        pytest.param({"input_tokens": 12, "output_tokens": -1}, id="negative-output"),
        pytest.param({"input_tokens": "12", "output_tokens": 4}, id="string-input"),
        pytest.param({"input_tokens": 12, "output_tokens": "4"}, id="string-output"),
    ],
)
def test_provider_usage_must_be_measured_integer_counts_before_any_writes(
    tmp_path, monkeypatch, trial_manifest, trial_attempt_fixture, usage
):
    """Authenticated identity cannot turn missing or loosely equal usage into measurements."""
    _use_temp_feedback(tmp_path, monkeypatch)
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path))
    lookup = _bind_authoritative_identity(tmp_path, trial_attempt_fixture)
    attempts = model_profile_trial._validate_results(trial_manifest, trial_attempt_fixture)
    response_ids = [
        json.loads(Path(attempt["identity_evidence"]["artifact_ref"]).read_text())["response_id"]
        for attempt in attempts
    ]
    assert model_profile_trial._authoritative_identity_verified(
        attempts, provider_response_lookup=lookup
    )
    # Fail on the last attempt to prove that earlier valid responses never cause
    # partial feedback or published state to escape the identity gate.
    response = lookup(response_ids[-1])
    if usage is ...:
        response.pop("usage")
    else:
        response["usage"] = usage
    lookup_requests = []

    def tracked_lookup(response_id):
        lookup_requests.append(response_id)
        return lookup(response_id)

    state_path = tmp_path / "refused-state.json"
    with pytest.raises(ValueError, match="identity evidence is not authoritative"):
        model_profile_trial.finalize_trial(
            trial_manifest,
            trial_attempt_fixture,
            state_path=state_path,
            record_feedback=True,
            ingest_brain=True,
            provider_response_lookup=tracked_lookup,
        )
    assert lookup_requests == response_ids
    assert not state_path.exists()
    assert not (tmp_path / "capability-program" / "profile-trial.json").exists()
    with feedback._conn() as conn:
        _assert_trial_feedback_tables_empty(conn)


def _rebind_trial_id(manifest, results):
    """Point manifest and results at the trial id implied by the current source seals."""
    old_trial_id = manifest["trial_id"]
    new_trial_id = model_profile_trial._expected_trial_id(manifest)
    manifest["trial_id"] = new_trial_id
    for request in manifest["requests"]:
        request["run_id"] = new_trial_id + request["run_id"][len(old_trial_id) :]
    results["trial_id"] = new_trial_id
    for attempt in results["attempts"]:
        attempt["run_id"] = new_trial_id + attempt["run_id"][len(old_trial_id) :]
    for trace in results.get("auxiliary_traces") or []:
        run_id = trace.get("run_id")
        if isinstance(run_id, str) and run_id.startswith(old_trial_id):
            trace["run_id"] = new_trial_id + run_id[len(old_trial_id) :]


def test_finalize_refuses_ingest_when_source_seal_disagrees_with_entries(
    tmp_path, monkeypatch, trial_roots, trial_manifest, trial_attempt_fixture
):
    """A mutated tree plus a rewritten aggregate must not flip brain ingest.

    The aggregate is the only live comparison. Pointing it at the mutated tree
    while the sealed entries still describe the old bytes, then rebinding
    trial_id, used to look like an unchanged source and ingest anyway.
    """
    _use_temp_feedback(tmp_path, monkeypatch)
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path))
    honest = copy.deepcopy(trial_attempt_fixture)
    lookup = _bind_authoritative_identity(tmp_path, honest)
    state_path = tmp_path / "envelope.json"
    state = model_profile_trial.finalize_trial(
        trial_manifest,
        honest,
        state_path=state_path,
        record_feedback=True,
        ingest_brain=True,
        provider_response_lookup=lookup,
        now=2_000,
    )
    assert state["brain_ingest_enabled"] is True
    assert state["source_integrity"]["unchanged"] is True
    assert json.loads(state_path.read_text(encoding="utf-8"))["brain_ingest_enabled"] is True

    workflows = trial_roots[1]
    (workflows / "worker.yml").write_text("worker: mutated\n", encoding="utf-8")
    live = model_profile_trial.source_manifest(workflows)
    manifest = copy.deepcopy(trial_manifest)
    recorded = manifest["source_before"]["workflows"]
    assert recorded["entries"] != live["entries"]
    recorded["aggregate_sha256"] = live["aggregate_sha256"]
    forged = copy.deepcopy(honest)
    _rebind_trial_id(manifest, forged)
    forged_state = tmp_path / "forged-envelope.json"
    with pytest.raises(ValueError, match="aggregate does not match its entries:workflows"):
        model_profile_trial.finalize_trial(
            manifest,
            forged,
            state_path=forged_state,
            record_feedback=True,
            ingest_brain=True,
            provider_response_lookup=lookup,
            now=2_001,
        )
    assert not forged_state.exists()
    assert json.loads(state_path.read_text(encoding="utf-8"))["brain_ingest_enabled"] is True
    with feedback._conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM profile_trial_ingests").fetchone()[0] == 1
        assert (
            conn.execute("SELECT trial_id FROM profile_trial_ingests").fetchone()[0]
            == trial_manifest["trial_id"]
        )


def test_finalize_refuses_ingest_when_identity_artifact_hash_mismatches(
    tmp_path, monkeypatch, trial_manifest, trial_attempt_fixture
):
    _use_temp_feedback(tmp_path, monkeypatch)
    results = copy.deepcopy(trial_attempt_fixture)
    lookup = _bind_authoritative_identity(tmp_path, results)
    artifact_path = Path(results["attempts"][0]["identity_evidence"]["artifact_ref"])
    artifact_path.write_text('{"tampered": true}', encoding="utf-8")
    with pytest.raises(ValueError, match="identity evidence is not authoritative"):
        model_profile_trial.finalize_trial(
            trial_manifest,
            results,
            record_feedback=True,
            ingest_brain=True,
            provider_response_lookup=lookup,
            now=2_000,
        )
    with feedback._conn() as conn:
        _assert_trial_feedback_tables_empty(conn)


@pytest.mark.parametrize(
    "damage",
    ["missing", "directory", "invalid-json", "invalid-utf8", "null", "list", "oversized"],
)
def test_malformed_identity_artifact_refuses_before_lookup_or_writes(
    tmp_path, monkeypatch, trial_manifest, trial_attempt_fixture, damage
):
    _use_temp_feedback(tmp_path, monkeypatch)
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path))
    lookup = _bind_authoritative_identity(tmp_path, trial_attempt_fixture)
    attempts = model_profile_trial._validate_results(trial_manifest, trial_attempt_fixture)
    if damage == "oversized":
        evidence = attempts[0]["identity_evidence"]
        path = Path(evidence["artifact_ref"])
        at_limit = path.read_bytes().ljust(64 * 1024, b" ")
        path.write_bytes(at_limit)
        evidence["artifact_sha256"] = "sha256:" + hashlib.sha256(at_limit).hexdigest()
    assert model_profile_trial._authoritative_identity_verified(
        attempts, provider_response_lookup=lookup
    )
    attempt = next(
        row for row in trial_attempt_fixture["attempts"] if row["run_id"] == attempts[0]["run_id"]
    )
    artifact_path = Path(attempt["identity_evidence"]["artifact_ref"])
    if damage in {"missing", "directory"}:
        artifact_path.unlink()
        if damage == "directory":
            artifact_path.mkdir()
    else:
        original = artifact_path.read_bytes()
        contents = {
            "invalid-json": b"{",
            "invalid-utf8": bytes([255]),
            "null": b"null",
            "list": b"[]",
            # Keep the oversized artifact valid JSON with matching identity.
            "oversized": original + b" " * (64 * 1024),
        }[damage]
        artifact_path.write_bytes(contents)
        attempt["identity_evidence"]["artifact_sha256"] = (
            "sha256:" + hashlib.sha256(contents).hexdigest()
        )

    if damage == "oversized":
        # Refuse large evidence without first loading the entire untrusted file.
        # Count actual bytes too, so repeated small reads cannot evade the budget.
        original_open = Path.open
        bytes_read = 0

        def bounded_open(path, *args, **kwargs):
            stream = original_open(path, *args, **kwargs)
            if path == artifact_path:
                original_read = stream.read

                def bounded_read(size=-1):
                    nonlocal bytes_read
                    assert size >= 0, "identity evidence must not use an unbounded read"
                    data = original_read(size)
                    bytes_read += len(data)
                    assert bytes_read <= 64 * 1024 + 1, "identity evidence read exceeded budget"
                    return data

                monkeypatch.setattr(stream, "read", bounded_read)
            return stream

        monkeypatch.setattr(Path, "open", bounded_open)

    def unexpected_lookup(response_id):
        pytest.fail(f"malformed artifact reached provider lookup: {response_id}")

    state_path = tmp_path / "refused-state.json"
    with pytest.raises(ValueError, match="identity evidence is not authoritative"):
        model_profile_trial.finalize_trial(
            trial_manifest,
            trial_attempt_fixture,
            state_path=state_path,
            record_feedback=True,
            ingest_brain=True,
            provider_response_lookup=unexpected_lookup,
        )
    assert not state_path.exists()
    assert not (tmp_path / "capability-program" / "profile-trial.json").exists()
    with feedback._conn() as conn:
        _assert_trial_feedback_tables_empty(conn)


def test_transport_recording_does_not_block_later_verified_ingest(
    tmp_path, monkeypatch, trial_manifest, trial_attempt_fixture
):
    _use_temp_feedback(tmp_path, monkeypatch)
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path))
    results = copy.deepcopy(trial_attempt_fixture)
    lookup = _bind_authoritative_identity(tmp_path, results)
    quality = {
        "codex-6-astra-high": 0.82,
        "codex-5.6-terra-high": 0.76,
        "codex-5.6-luna-high": 0.79,
    }
    results["quality_by_profile"] = quality
    model_profile_trial.finalize_trial(
        trial_manifest,
        results,
        record_feedback=True,
        ingest_brain=False,
        now=2_000,
    )
    with feedback._conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM profile_trial_ingests").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 3
    verified = copy.deepcopy(results)
    state = model_profile_trial.finalize_trial(
        trial_manifest,
        verified,
        record_feedback=True,
        ingest_brain=True,
        provider_response_lookup=lookup,
        now=2_001,
    )
    assert state["brain_ingest_enabled"] is True
    with feedback._conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM profile_trial_ingests").fetchone()[0] == 1


def test_transport_recording_preserves_measured_program_summary(
    tmp_path, monkeypatch, trial_roots, trial_manifest, trial_attempt_fixture
):
    _use_temp_feedback(tmp_path, monkeypatch)
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path))
    program_dir = tmp_path / "capability-program"
    program_dir.mkdir(parents=True)
    preserved = {
        "trial_id": trial_manifest["trial_id"],
        "updated_at": 1_500,
        "profile_count": 3,
        "instance_count": 3,
        "identity_verified": True,
        "brain_ingest_enabled": True,
        "quality_by_profile": {
            "codex-6-astra-high": 0.9,
            "codex-5.6-terra-high": 0.8,
            "codex-5.6-luna-high": 0.85,
        },
        "cost_tokens_by_profile": {},
        "aggregate_tokens_in": 0,
        "aggregate_tokens_out": 0,
    }
    (program_dir / "profile-trial.json").write_text(json.dumps(preserved), encoding="utf-8")
    for attempt in trial_attempt_fixture["attempts"]:
        attempt.pop("tokens_in")
        attempt.pop("tokens_out")
    model_profile_trial.finalize_trial(
        trial_manifest,
        trial_attempt_fixture,
        record_feedback=True,
        ingest_brain=False,
        now=2_000,
    )
    summary = json.loads((program_dir / "profile-trial.json").read_text(encoding="utf-8"))
    assert summary["identity_verified"] is True
    assert summary["brain_ingest_enabled"] is True
    assert summary["quality_by_profile"] == preserved["quality_by_profile"]
    assert summary["updated_at"] == 2_000


def test_connection_registers_profile_trial_ingests_before_first_ingest(tmp_path, monkeypatch):
    _use_temp_feedback(tmp_path, monkeypatch)
    with feedback._conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM profile_trial_ingests").fetchone()[0] == 0


def test_verified_ingest_replaces_transport_telemetry(
    tmp_path, monkeypatch, trial_manifest, trial_attempt_fixture
):
    _use_temp_feedback(tmp_path, monkeypatch)
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path))
    transport = copy.deepcopy(trial_attempt_fixture)
    lookup = _bind_authoritative_identity(tmp_path, transport)
    model_profile_trial.finalize_trial(
        trial_manifest, transport, record_feedback=True, ingest_brain=False, now=2000
    )
    verified = copy.deepcopy(transport)
    verified["attempts"][0]["tokens_in"] = 999
    verified["attempts"][0]["tokens_out"] = 777
    response = lookup("resp_fixture1")
    response["usage"] = {"input_tokens": 999, "output_tokens": 777}
    model_profile_trial.finalize_trial(
        trial_manifest,
        verified,
        record_feedback=True,
        ingest_brain=True,
        provider_response_lookup=lookup,
        now=2001,
    )
    with feedback._conn() as conn:
        row = conn.execute(
            "SELECT tokens_in,tokens_out FROM execution_attempts WHERE attempt_id=?",
            (
                f"attempt:trial:{trial_manifest['trial_id']}:{verified['attempts'][0]['profile_id']}",
            ),
        ).fetchone()
        assert row == (999, 777)


@pytest.mark.parametrize(
    "field,value",
    [
        ("provider_resolved_provider", "anthropic"),
        ("provider_resolved_model", "wrong-model"),
        ("profile_id", "wrong-profile"),
        ("provider_resolved_model", None),
    ],
)
def test_identity_artifact_content_must_match_attempt(
    tmp_path, monkeypatch, trial_manifest, trial_attempt_fixture, field, value
):
    _use_temp_feedback(tmp_path, monkeypatch)
    results = copy.deepcopy(trial_attempt_fixture)
    lookup = _bind_authoritative_identity(tmp_path, results)
    attempt = results["attempts"][0]
    artifact = Path(attempt["identity_evidence"]["artifact_ref"])
    contents = json.loads(artifact.read_text())
    contents[field] = value
    artifact.write_text(json.dumps(contents))
    attempt["identity_evidence"]["artifact_sha256"] = (
        "sha256:" + hashlib.sha256(artifact.read_bytes()).hexdigest()
    )
    with pytest.raises(ValueError, match="identity evidence is not authoritative"):
        model_profile_trial.finalize_trial(
            trial_manifest,
            results,
            record_feedback=True,
            ingest_brain=True,
            provider_response_lookup=lookup,
            now=2000,
        )
    with feedback._conn() as conn:
        _assert_trial_feedback_tables_empty(conn)


@pytest.mark.parametrize(
    "quality",
    [
        [],
        {"unknown": 0.5},
        {"codex-6-astra-high": True},
        {"codex-6-astra-high": "invalid"},
        {"codex-6-astra-high": float("nan")},
        {"codex-6-astra-high": float("inf")},
        {"codex-6-astra-high": 1.5},
    ],
)
def test_invalid_quality_refuses_all_trial_writes(
    tmp_path, monkeypatch, trial_manifest, trial_attempt_fixture, quality
):
    _use_temp_feedback(tmp_path, monkeypatch)
    results = copy.deepcopy(trial_attempt_fixture)
    lookup = _bind_authoritative_identity(tmp_path, results)
    results["quality_by_profile"] = quality
    with pytest.raises(ValueError, match="quality_by_profile"):
        model_profile_trial.finalize_trial(
            trial_manifest,
            results,
            record_feedback=True,
            ingest_brain=True,
            provider_response_lookup=lookup,
            now=2000,
        )
    with feedback._conn() as conn:
        _assert_trial_feedback_tables_empty(conn)


def test_replacement_measurements_replace_summary_identity_and_quality(
    tmp_path, monkeypatch, trial_manifest, trial_attempt_fixture
):
    _use_temp_feedback(tmp_path, monkeypatch)
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path))
    results = copy.deepcopy(trial_attempt_fixture)
    lookup = _bind_authoritative_identity(tmp_path, results)
    results["quality_by_profile"] = {a["profile_id"]: 0.9 for a in results["attempts"]}
    model_profile_trial.finalize_trial(
        trial_manifest,
        results,
        record_feedback=True,
        provider_response_lookup=lookup,
        now=2000,
    )
    results["identity_verified"] = False
    results["quality_by_profile"] = {a["profile_id"]: 0.1 for a in results["attempts"]}
    results["attempts"][0]["tokens_in"] = 999
    model_profile_trial.finalize_trial(
        trial_manifest,
        results,
        record_feedback=True,
        now=2001,
    )
    summary = json.loads((tmp_path / "capability-program/profile-trial.json").read_text())
    assert summary["identity_verified"] is False
    assert summary["quality_by_profile"] == results["quality_by_profile"]
    assert summary["aggregate_tokens_in"] == 1023


def test_unverified_retry_preserves_committed_trial_state(
    tmp_path, monkeypatch, trial_manifest, trial_attempt_fixture
):
    _use_temp_feedback(tmp_path, monkeypatch)
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path))
    results = copy.deepcopy(trial_attempt_fixture)
    lookup = _bind_authoritative_identity(tmp_path, results)
    path = tmp_path / "trial-state.json"
    committed = model_profile_trial.finalize_trial(
        trial_manifest,
        results,
        state_path=path,
        ingest_brain=True,
        provider_response_lookup=lookup,
        now=2000,
    )
    before = path.read_bytes()
    results["identity_verified"] = False
    results["attempts"][0]["tokens_in"] = 999
    retried = model_profile_trial.finalize_trial(
        trial_manifest,
        results,
        state_path=path,
        record_feedback=True,
        now=2001,
    )
    assert retried == committed
    assert path.read_bytes() == before
    report = model_profile_trial.build_report(path)
    assert report["identity_verified"] is True
    assert report["brain_ingest_enabled"] is True
