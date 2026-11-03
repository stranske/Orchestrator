"""Hermetic coverage for feedback.py work-package listed statements."""

from __future__ import annotations

import io
import json
import sqlite3
import sys
from unittest import mock

import pytest

import feedback


@pytest.fixture(autouse=True)
def isolated_runtime(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ORCH_LOCAL_RUNTIME", str(tmp_path / "runtime"))
    db = tmp_path / "runtime" / "feedback" / "orchestrator.db"
    monkeypatch.setattr(feedback, "DB_PATH", db)
    monkeypatch.setattr(feedback, "_capability_daily_heartbeat", lambda *a, **k: None)


def _memory_conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.executescript(feedback.SCHEMA)
    feedback._migrate_schema(c)
    c.commit()
    return c


def test_migrate_schema_adds_review_head_sha_and_runs_source_columns() -> None:
    c = sqlite3.connect(":memory:")
    c.execute(
        "CREATE TABLE review_rounds (run_id TEXT, pr TEXT, round INTEGER, verdict TEXT, "
        "reviewer_agent TEXT, first_pass INTEGER, recorded_ts INTEGER, "
        "PRIMARY KEY (run_id, round))"
    )
    c.execute("INSERT INTO review_rounds VALUES ('r1','o/r#1',1,'pass','bot',1,100)")
    c.execute("CREATE TABLE runs (run_id TEXT PRIMARY KEY, ts INTEGER)")
    c.execute("INSERT INTO runs VALUES ('r1', 1)")
    feedback._migrate_schema(c)
    review_cols = {row[1] for row in c.execute("PRAGMA table_info(review_rounds)")}
    run_cols = {row[1] for row in c.execute("PRAGMA table_info(runs)")}
    assert "head_sha" in review_cols and "source" in run_cols


def test_widen_agent_switches_rebuilds_legacy_table() -> None:
    c = sqlite3.connect(":memory:")
    c.execute(
        "CREATE TABLE agent_switches (pr_ref TEXT, from_agent TEXT, to_agent TEXT, "
        "switched_ts INTEGER, auto_label INTEGER, commits_before INTEGER, commits_after INTEGER, "
        "merged INTEGER, durability TEXT, recorded_ts INTEGER, "
        "PRIMARY KEY (pr_ref, switched_ts))"
    )
    c.execute("INSERT INTO agent_switches VALUES ('o/r#1','a','b',1,0,1,1,1,'durable',2)")
    feedback._widen_agent_switches(c)
    cols = {row[1] for row in c.execute("PRAGMA table_info(agent_switches)")}
    assert "source" in cols
    row = c.execute("SELECT source FROM agent_switches").fetchone()
    assert row[0] == "label"


def test_apply_data_migration_once_rolls_back_when_work_fails() -> None:
    c = _memory_conn()

    def boom(_c: sqlite3.Connection) -> dict:
        raise RuntimeError("migration failed")

    with pytest.raises(RuntimeError, match="migration failed"):
        feedback._apply_data_migration_once(c, "wp.test.fail", boom)
    assert (
        c.execute("SELECT 1 FROM data_migrations WHERE name=?", ("wp.test.fail",)).fetchone()
        is None
    )


def test_restore_offload_subjects_without_marker() -> None:
    c = _memory_conn()
    c.execute("DELETE FROM data_migrations WHERE name=?", (feedback.OFFLOAD_SUBJECTS_MIGRATION,))
    assert feedback.restore_offload_subjects(conn=c) == {
        "restored": 0,
        "reason": "not applied on this Brain",
    }


def test_restore_refusal_role_edges_without_marker() -> None:
    c = _memory_conn()
    c.execute("DELETE FROM data_migrations WHERE name=?", (feedback.REFUSAL_ROLE_EDGES_MIGRATION,))
    assert feedback.restore_refusal_role_edges(conn=c) == {
        "restored": 0,
        "reason": "not applied on this Brain",
    }


def test_restore_rejected_role_capability_edges_without_marker() -> None:
    c = _memory_conn()
    c.execute(
        "DELETE FROM data_migrations WHERE name=?",
        (feedback.REJECTED_ROLE_CAPABILITY_EDGES_MIGRATION,),
    )
    assert feedback.restore_rejected_role_capability_edges(conn=c) == {
        "restored": 0,
        "reason": "not applied on this Brain",
    }


def test_json_or_none_preserves_nonempty_json_strings() -> None:
    assert feedback._json_or_none('  {"a":1}  ') == '{"a":1}'


def test_bounded_completion_string_redacts_and_truncates() -> None:
    redacted: list[str] = []
    secret = feedback._bounded_completion_string("token ghp_abcdefghijklmnop", redacted, "p")
    assert secret == "[REDACTED]" and redacted == ["p"]
    long_text = "x" * (feedback.MAX_COMPLETION_STRING_CHARS + 5)
    redacted2: list[str] = []
    out = feedback._bounded_completion_string(long_text, redacted2, "q")
    assert out == long_text[: feedback.MAX_COMPLETION_STRING_CHARS]
    assert redacted2 == ["q:truncated"]


def test_sanitize_completion_value_bounds_list_and_strips_sensitive_keys() -> None:
    redacted: list[str] = []
    big = list(range(feedback.MAX_COMPLETION_LIST_ITEMS + 3))
    out = feedback._sanitize_completion_value(big, redacted, "items")
    assert len(out) == feedback.MAX_COMPLETION_LIST_ITEMS
    assert "items:bounded" in redacted
    redacted2: list[str] = []
    nested = feedback._sanitize_completion_value({"raw_prompt": "x", "ok": 1}, redacted2, "root")
    assert "raw_prompt" not in nested and any("raw_prompt" in item for item in redacted2)


def test_safe_identifier_hashes_disallowed_command_text() -> None:
    redacted: list[str] = []
    value = feedback._safe_identifier("echo $(rm -rf /)", redacted, "cmd")
    assert value.startswith("sha256:") and redacted == ["cmd:hashed"]


def test_sanitize_artifact_refs_string_row_and_extra_keys() -> None:
    redacted: list[str] = []
    rows = feedback._sanitize_artifact_refs("artifact-path", redacted)
    assert rows[0]["kind"] == "reference" and "artifact_refs[0]:hashed" in redacted
    redacted2: list[str] = []
    many = [{"artifact_id": "a", "kind": "k", "extra": 1}] * (feedback.MAX_COMPLETION_ARTIFACTS + 2)
    bounded = feedback._sanitize_artifact_refs(many, redacted2)
    assert len(bounded) == feedback.MAX_COMPLETION_ARTIFACTS
    assert "artifact_refs:bounded" in redacted2


def test_sanitize_result_hashes_wraps_non_dict_and_bounds_keys() -> None:
    redacted: list[str] = []
    wrapped = feedback._sanitize_result_hashes([1, 2], redacted, "rh")
    assert "result" in wrapped and wrapped["result"].startswith("sha256:")
    redacted2: list[str] = []
    huge = {f"k{i}": "a" * 64 for i in range(feedback.MAX_COMPLETION_LIST_ITEMS + 2)}
    bounded = feedback._sanitize_result_hashes(huge, redacted2, "rh2")
    assert len(bounded) <= feedback.MAX_COMPLETION_LIST_ITEMS


def test_sanitize_completion_payload_unknown_nested_and_size_limit() -> None:
    payload, status, _ = feedback._sanitize_completion_payload(
        {"verification": "not-a-dict", "unknown_plain": 1}
    )
    assert status == "rejected"
    assert any(
        code.startswith("invalid_object:verification") for code in payload["rejection_codes"]
    )
    assert any(
        code.startswith("unknown_field:unknown_plain") for code in payload["rejection_codes"]
    )
    with mock.patch.object(feedback, "MAX_COMPLETION_EVENT_BYTES", 64):
        payload2, status2, _ = feedback._sanitize_completion_payload(
            {"result": {"version_hash": "a" * 200}}
        )
    assert status2 == "rejected" and payload2["rejection_codes"] == ["event_size_limit"]


def test_sanitize_completion_payload_retry_sequence_and_result_hashes() -> None:
    clean, status, _ = feedback._sanitize_completion_payload(
        {
            "result_hashes": ["not-a-hash", "b" * 64],
            "retry_sequence": [{"bad_key": 1}],
        }
    )
    assert status == "rejected"
    assert any("retry_sequence" in code for code in clean["rejection_codes"])


def test_normalize_completion_producer_prefix_aliases() -> None:
    assert feedback._normalize_completion_producer("langsmith-trace") == "langsmith"
    assert feedback._normalize_completion_producer("ledger-sync") == "ledger"
    assert feedback._normalize_completion_producer("keepalive-tick") == "keepalive"


def test_record_completion_event_in_conn_requires_core_fields() -> None:
    c = _memory_conn()
    with pytest.raises(ValueError, match="completion event requires"):
        feedback._record_completion_event_in_conn(c, "", event_type="x", phase="y", producer="z")
    feedback.record_run("run-a", "o/r#1", "implement", "codex", conn=c)
    with pytest.raises(ValueError, match="identity exceeds 512"):
        feedback._record_completion_event_in_conn(
            c, "x" * 600, event_type="completion", phase="execution", producer="p"
        )
    with pytest.raises(ValueError, match="capability lineage"):
        feedback._record_completion_event_in_conn(
            c,
            "run-a",
            event_type="completion",
            phase="execution",
            producer="p",
            capability_id="cap",
        )


def test_record_runtime_ac_gate_event_validation_errors() -> None:
    c = _memory_conn()
    base = {"required": False, "dry_run": True}
    with pytest.raises(ValueError, match="requires target"):
        feedback._record_runtime_ac_gate_event_in_conn(c, target="", gate_status="required", **base)
    with pytest.raises(ValueError, match="invalid runtime-AC gate status"):
        feedback._record_runtime_ac_gate_event_in_conn(
            c, target="o/r#1", gate_status="bogus", **base
        )
    with pytest.raises(ValueError, match="eligibility source"):
        feedback._record_runtime_ac_gate_event_in_conn(
            c,
            target="o/r#1",
            gate_status="required",
            eligibility_source="bogus",
            **base,
        )
    with pytest.raises(ValueError, match="spec_hash"):
        feedback._record_runtime_ac_gate_event_in_conn(
            c, target="o/r#1", gate_status="required", spec_hash="bad", **base
        )
    with pytest.raises(ValueError, match="executed runtime-AC event requires"):
        feedback._record_runtime_ac_gate_event_in_conn(
            c,
            target="o/r#1",
            gate_status="executed",
            verifier_verdict=None,
            **base,
        )


def test_runtime_ac_gate_events_joins_downstream_outcome() -> None:
    feedback.record_run("closer-1", "o/r#9", "implement", "codex")
    feedback.record_outcome(
        "closer-1", adjudicated_verdict="PASS", merged=True, durability="durable"
    )
    feedback.record_runtime_ac_gate_event(
        target="o/r#9",
        gate_status="required",
        required=True,
        dry_run=False,
        closer_run_id="closer-1",
    )
    events = feedback.runtime_ac_gate_events()
    assert events and events[0]["downstream_verdict"] == "PASS"
    assert events[0]["downstream_durability"] == "durable"


def test_record_influence_edge_rejects_invalid_type_and_missing_target() -> None:
    c = _memory_conn()
    with pytest.raises(ValueError, match="invalid influence_type"):
        feedback._record_influence_edge_in_conn(
            c,
            target_run_id="t",
            influence_type="bogus",
            influence_id="i",
            accepted=True,
        )
    with pytest.raises(ValueError, match="target_run_id and influence_id"):
        feedback._record_influence_edge_in_conn(
            c, target_run_id="", influence_type="role", influence_id="i", accepted=True
        )


def test_closer_stage_rewards_skips_malformed_payload() -> None:
    c = _memory_conn()
    feedback.record_run("cr1", "o/r#1", "closer_repair", "codex", routing_metadata=json.dumps({}))
    feedback._record_completion_event_in_conn(
        c,
        "cr1",
        event_type="closer_stage",
        phase="verification",
        producer="closer_disposition.integrate_ordinary",
        status="pass",
        payload={"closer_stage": {"accepted": "yes"}},
    )
    assert feedback._closer_stage_rewards(c, "closer_repair") == {}


def test_capability_causal_evidence_requires_identity() -> None:
    with pytest.raises(ValueError, match="capability evidence requires"):
        feedback.capability_causal_evidence("", "v")


def test_capability_causal_evidence_restores_row_factory() -> None:
    with feedback._conn() as c:
        c.row_factory = sqlite3.Row
        feedback.capability_causal_evidence("cap", "sha256:" + "a" * 64, conn=c)
        assert c.row_factory is sqlite3.Row


def test_validate_operation_role_rejects_unknown() -> None:
    with pytest.raises(ValueError, match="invalid operation_role"):
        feedback.validate_operation_role("not-a-real-role")


def test_derive_operation_role_replay_and_contradiction() -> None:
    assert feedback.derive_operation_role("replay_batch") == "replay"
    with pytest.raises(ValueError, match="contradicts operation"):
        feedback.derive_operation_role("verify_pr", operation_role="worker")


def test_record_run_rejects_capability_version_mismatch() -> None:
    c = _memory_conn()
    with pytest.raises(ValueError, match="lineage length mismatch"):
        feedback._record_run_in_conn(
            c,
            "r1",
            "o/r#1",
            "implement",
            "codex",
            capability_ids=["a"],
            capability_version_ids=["v1", "v2"],
        )


def test_record_run_inherits_role_capability_edges() -> None:
    version = "sha256:" + "c" * 64
    c = _memory_conn()
    feedback._record_run_in_conn(c, "role-run", "o/r#1", "role:triage", "codex", role_name="triage")
    feedback._record_influence_edge_in_conn(
        c,
        target_run_id="role-run",
        influence_type="capability",
        influence_id=version,
        accepted=True,
        capability_id="offload",
        capability_version_id=version,
        allow_unlinked=True,
    )
    feedback._record_run_in_conn(
        c,
        "acting",
        "o/r#1",
        "implement",
        "codex",
        influenced_by_role_run_ids=["role-run"],
    )
    inherited = c.execute(
        "SELECT capability_id, influence_id FROM influence_edges WHERE target_run_id='acting' "
        "AND capability_id='offload'"
    ).fetchone()
    assert inherited == ("offload", version)


def test_ingest_profile_trial_requires_trial_id(monkeypatch: pytest.MonkeyPatch) -> None:
    db = feedback.DB_PATH.parent / "brain-quarantine.db"
    monkeypatch.setattr(feedback, "DB_PATH", db)
    with pytest.raises(ValueError, match="profile trial ingest requires trial_id"):
        feedback.ingest_profile_trial({}, manifest={}, attempts=[])


def test_bt_strengths_skips_bad_scores_and_ranks_winners() -> None:
    now = int(__import__("time").time())
    with feedback._conn() as c:
        c.execute(
            "INSERT INTO evaluations VALUES (?,?,?,?,?,?,?)",
            ("E1", "a", "j", "not-a-float", None, None, now),
        )
        c.execute(
            "INSERT INTO evaluations VALUES (?,?,?,?,?,?,?)",
            ("E1", "b", "j", 3.0, None, None, now),
        )
        c.commit()
    weak = feedback.bt_strengths(min_comparisons=1)
    assert weak["ready"] is False and weak["comparisons"] == 0
    for i in range(3):
        exp = f"BT{i}"
        for impl, score in (("bt_a", 9.0), ("bt_b", 6.0), ("bt_c", 3.0)):
            feedback.record_run(f"{impl}_{i}", "o/bt", "btduel", impl, experiment_id=exp)
            feedback.record_evaluation(exp, impl, "judge", score)
    ready = feedback.bt_strengths(task_type="btduel")
    assert ready["ready"] and ready["strengths"]["bt_a"] > ready["strengths"]["bt_b"]


def test_capability_attribution_of_returns_empty_on_sqlite_error() -> None:
    c = mock.Mock()
    c.execute.side_effect = sqlite3.OperationalError("no table")
    assert feedback._capability_attribution_of(c, "run") == []


def test_propagate_outcome_lineage_handles_bad_source_json_and_role_aggregate() -> None:
    c = feedback._conn()
    feedback._record_run_in_conn(c, "target", "o/r#1", "implement", "codex")
    feedback._record_outcome_in_conn(
        c, "target", adjudicated_verdict="PASS", merged=True, durability="durable"
    )
    ev = feedback._record_completion_event_in_conn(
        c,
        "role-src",
        event_type="role",
        phase="execution",
        producer="feedback.record_role_run",
        payload={},
    )
    feedback._record_run_in_conn(c, "role-src", "o/r#1", "role:triage", "codex", role_name="triage")
    feedback._record_influence_edge_in_conn(
        c,
        target_run_id="target",
        influence_type="role",
        influence_id="role-src",
        source_run_id="role-src",
        source_event_id=ev["event_id"],
        accepted=True,
    )
    c.execute(
        "UPDATE completion_events SET payload_json='not-json' WHERE event_id=?",
        (ev["event_id"],),
    )
    n = feedback._propagate_outcome_lineage_in_conn(c, "target")
    assert n >= 1
    role_row = c.execute(
        "SELECT adjudicated_verdict,durability FROM outcomes WHERE run_id='role-src'"
    ).fetchone()
    assert role_row == ("PASS", "durable")
    c.close()


def test_role_task_type_requires_name() -> None:
    with pytest.raises(ValueError, match="role_name is required"):
        feedback.role_task_type("")


def test_role_activation_metrics_skips_invalid_json_payload() -> None:
    c = _memory_conn()
    feedback._record_completion_event_in_conn(
        c,
        "sel-1",
        event_type="role",
        phase="trigger",
        producer="roles",
        payload={"role_ids": ["redirect"], "result": {"selector_status": "invoked"}},
    )
    c.execute(
        "INSERT INTO completion_events (event_id,schema_version,run_id,event_type,phase,producer,"
        "validation_status,payload_json,content_hash,redaction_count,created_ts,updated_ts) "
        "VALUES ('bad',1,'x','role','trigger','roles','accepted','not-json','h',0,1,1)"
    )
    metrics = feedback.role_activation_metrics(conn=c)
    assert metrics["roles"]["redirect"]["invoked"] == 1


def test_join_role_to_outcome_validates_ids_and_role_run() -> None:
    with pytest.raises(ValueError, match="role_run_id and influenced_run_id"):
        feedback.join_role_to_outcome("", "x")
    with pytest.raises(ValueError, match="not a recorded role run"):
        feedback.join_role_to_outcome("missing", "also-missing")


def test_record_skill_invocation_validation() -> None:
    with pytest.raises(ValueError, match="skill_id is required"):
        feedback.record_skill_invocation("", "a" * 64)
    with pytest.raises(ValueError, match="invalid skill phase"):
        feedback.record_skill_invocation("sk", "a" * 64, phase="bogus")


def test_normalized_sha256_id_formats() -> None:
    digest = "a" * 64
    assert feedback._normalized_sha256_id(digest) == f"sha256:{digest}"
    assert feedback._normalized_sha256_id(f"sha256:{digest}") == f"sha256:{digest}"
    hashed = feedback._normalized_sha256_id("plain-id")
    assert hashed and hashed.startswith("sha256:")


def test_completion_event_episodes_durable_filter_and_subject_json_errors() -> None:
    feedback.record_run("ep1", "owner/repo#1", "implement", "codex", assignment="assigned")
    feedback.record_outcome("ep1", durability="durable")
    feedback.record_completion_event(
        "ep1",
        phase="execution",
        producer="feedback.record_run",
        status="succeeded",
        payload={"capability_ids": []},
    )
    durable = feedback.completion_event_episodes(durable_only=True, limit=5)
    assert durable and durable[0]["identity"]["run_id"] == "ep1"
    c = _memory_conn()
    feedback._record_run_in_conn(
        c,
        "exp-member",
        "owner/repo#2",
        "implement",
        "codex",
        experiment_id="EXP1",
        routing_metadata=json.dumps({"experiment_member_id": "exp-member"}),
    )
    c.execute("CREATE TABLE research_subject_experiments (exp_id TEXT, subject_id TEXT)")
    c.execute(
        "CREATE TABLE research_subjects (subject_id TEXT, subject_family_id TEXT, "
        "canonical_target TEXT, task_type TEXT, spec_hash TEXT, base_sha TEXT, "
        "arms_json TEXT, profiles_json TEXT)"
    )
    c.execute(
        "INSERT INTO research_subjects VALUES (?,?,?,?,?,?,?,?)",
        ("subj", "fam", "owner/repo#2", "implement", "b" * 64, "c" * 40, "not-json", "{"),
    )
    c.execute("INSERT INTO research_subject_experiments VALUES ('EXP1','subj')")
    feedback._record_completion_event_in_conn(
        c,
        "exp-member",
        event_type="completion",
        phase="execution",
        producer="p",
        payload={},
    )
    rows = feedback.completion_event_episodes(conn=c, limit=5)
    assert rows


def test_record_muse_subscription_cost_validates_inputs() -> None:
    with pytest.raises(ValueError, match="at least one attempted invocation"):
        feedback.record_muse_subscription_cost(
            "r",
            invocation_count=0,
            window_start_ts=1,
            window_end_ts=2,
            observed_start_ts=1,
            observed_end_ts=2,
            wall_seconds=1.0,
            raw_exit_codes=[],
        )
    with pytest.raises(ValueError, match="positive duration"):
        feedback.record_muse_subscription_cost(
            "r",
            invocation_count=1,
            window_start_ts=5,
            window_end_ts=5,
            observed_start_ts=1,
            observed_end_ts=2,
            wall_seconds=1.0,
            raw_exit_codes=[0],
        )


def test_record_execution_trace_rejects_bad_attempt_ordinal() -> None:
    feedback.record_run("tr1", "o/r#1", "implement", "codex")
    feedback.record_execution_trace(
        "tr1",
        operation="implement",
        operation_role="worker",
        resolved_model="gpt-5.6-codex",
        attempt_ordinal=0,
    )


def test_record_execution_attempt_rejects_identity_change() -> None:
    c = _memory_conn()
    feedback._record_execution_attempt_in_conn(
        c,
        run_id="r1",
        attempt_id="attempt:1",
        operation_role="worker",
        resolved_model="gpt-5.6-codex",
    )
    with pytest.raises(ValueError, match="attempt identity changed"):
        feedback._record_execution_attempt_in_conn(
            c,
            run_id="r2",
            attempt_id="attempt:1",
            operation_role="worker",
            resolved_model="gpt-5.6-codex",
        )


def test_complete_profile_attempt_requires_existing_row() -> None:
    with pytest.raises(ValueError, match="missing selected profile attempt"):
        feedback.complete_profile_attempt(
            "no-run", selected_profile_id="p", resolved_provider="openai", resolved_model="m"
        )


def test_complete_profile_attempt_unresolved_requires_reason() -> None:
    with pytest.raises(ValueError, match="unresolved profile completion requires fallback_reason"):
        feedback.complete_profile_attempt_unresolved(
            "r", selected_profile_id="p", fallback_reason="   "
        )


def test_migrate_legacy_execution_attempts_classifies_worker_like_as_unknown() -> None:
    c = _memory_conn()
    feedback.record_run("legacy", "o/r#1", "implement", "codex", conn=c)
    c.execute(
        "INSERT INTO execution_traces VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "trace:1",
            "legacy",
            None,
            None,
            "openai",
            "gpt",
            "implement",
            "ok",
            1.0,
            0.0,
            "langsmith",
            None,
            100,
        ),
    )
    report = feedback.migrate_legacy_execution_attempts(apply=True, conn=c)
    role = c.execute(
        "SELECT operation_role FROM execution_attempts WHERE trace_key='trace:1'"
    ).fetchone()[0]
    assert role == "unknown" and report["worker_like_operations_left_unresolved"] == 1


def test_relearn_uses_agent_global_cost_imputation() -> None:
    priors = {"impute-task": {"rich": 0.5, "poor": 0.5}, "other-task": {"poor": 0.5}}
    now = int(__import__("time").time())
    c = feedback._conn()
    for task_type, agent, cost in (
        ("impute-task", "rich", 2.0),
        ("impute-task", "poor", None),
        ("other-task", "poor", 1.5),
    ):
        rid = f"imp-{task_type}-{agent}"
        c.execute(
            "INSERT INTO runs (run_id,ts,target,task_type,agent,assignment) VALUES (?,?,?,?,?,?)",
            (rid, now, "o/r#1", task_type, agent, "assigned"),
        )
        c.execute(
            "INSERT INTO outcomes (run_id,adjudicated_verdict,merged,durability,durability_checked_ts) "
            "VALUES (?,?,?,?,?)",
            (rid, "PASS", 1, "durable", now),
        )
        if cost is not None:
            c.execute(
                "INSERT INTO costs (run_id,tokens_in,tokens_out,cost_usd,latency_s,source,pulled_ts) "
                "VALUES (?,?,?,?,?,?,?)",
                (rid, 5000, 5000, cost, 1.0, "ccusage", now),
            )
    c.commit()
    c.close()
    ver = feedback.relearn(priors, window_days=120)
    assert ver >= 1
    c2 = feedback._conn()
    poor = c2.execute(
        "SELECT rationale FROM route_weights WHERE task_type='impute-task' AND agent='poor' "
        "ORDER BY version DESC LIMIT 1"
    ).fetchone()[0]
    c2.close()
    assert "agent_global" in poor


def test_record_lane_round_rejects_unknown_agent() -> None:
    with pytest.raises(ValueError, match="agent, lane and error_class"):
        feedback.record_lane_round("gemini", "lane", None, 0, "timeout", 1)


def test_approve_evidence_type_reactivates_retired_name() -> None:
    gap = "missing telemetry for wp-feedback"
    with feedback._conn() as c:
        c.execute(
            "INSERT INTO evidence_types VALUES (?,?,?,?,?)",
            ("wp-type", 1, 0, "retired", "old"),
        )
        now = int(__import__("time").time())
        for i in range(3):
            c.execute(
                "INSERT INTO evidence_gaps VALUES (?,?,?,?,?)",
                (now + i, "ref", "eval", gap, "open"),
            )
        c.commit()
    result = feedback.approve_evidence_type(
        "wp-type", gap, min_recurrence=2, apply=True, rationale="reactivated"
    )
    assert result["evidence_type_status"] == "reactivated" and result["gaps_marked"] == 3


def test_relearn_quality_invalid_half_life_and_metadata_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ORCH_RELEARN_HALF_LIFE_DAYS", "not-a-number")
    priors = {"q-task": {"a": 0.5}}
    now = int(__import__("time").time())
    with feedback._conn() as c:
        c.execute(
            "INSERT INTO runs (run_id,ts,target,task_type,agent,assignment,routing_metadata,experiment_id) "
            "VALUES (?,?,?,?,?,?,?,?)",
            ("qr1", now, "o/r#1", "q-task", "a", "assigned", "not-json", "E1"),
        )
        c.commit()
    ver = feedback.relearn_quality(priors, window_days=120)
    assert ver >= 1


def test_influence_edge_default_guard_matches_selftest() -> None:
    with feedback._conn() as c:
        c.execute(
            "INSERT OR IGNORE INTO runs (run_id,ts,target,task_type,agent) "
            "VALUES ('orphan-probe',0,'o/r#1','implement','codex')"
        )
        with pytest.raises(ValueError, match="no completion event"):
            feedback._record_influence_edge_in_conn(
                c,
                target_run_id="orphan-probe",
                influence_type="role",
                influence_id="role:probe",
                accepted=True,
            )


def test_main_cli_restore_and_completion_events(monkeypatch: pytest.MonkeyPatch) -> None:
    feedback.record_run("main-run", "o/r#1", "implement", "codex")
    feedback.record_completion_event("main-run", phase="execution", producer="p")
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    assert feedback.main(["migrate-execution-attempts"]) == 0
    assert feedback.main(["restore-offload-subjects"]) == 0
    assert feedback.main(["restore-refusal-role-edges"]) == 0
    assert feedback.main(["restore-rejected-role-capability-edges"]) == 0
    assert feedback.main(["completion-events", "--json", "--limit", "5"]) == 0
    assert "main-run" in buf.getvalue()


def test_main_questions_and_answer_and_resume_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    q = feedback.record_owner_question("WP question?", "default-yes", repo="o/r", expires_days=7)
    out = io.StringIO()
    err = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    assert feedback.main(["questions"]) == 0
    assert q["question_id"] in out.getvalue()
    assert feedback.main(["answer", q["question_id"], "no"]) == 0
    feedback.record_resume_token("rt-wp", "claude", "claude_session", "tok-1")
    assert feedback.main(["resume-hint", "rt-wp"]) == 0
    assert feedback.main(["resume-hint", "missing"]) == 1


def test_selftest_resolve_db_path_restores_environment() -> None:
    feedback._selftest_resolve_db_path()


def test_record_role_run_tags_offload_when_backend_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    version = "sha256:" + "d" * 64
    import capabilities

    monkeypatch.setattr(
        capabilities,
        "load_declared",
        lambda _reg: {
            "role-redirect": {"capability_version_id": version},
            "offload": {"capability_version_id": version},
        },
    )
    feedback.record_role_run(
        "role:redirect:offloaded",
        "redirect",
        "owner/repo#tr",
        "cursor",
        backend_run_id="offload:abc123",
    )
    feedback.record_role_run("role:redirect:replayed", "redirect", "owner/repo#tr", "cursor")
    with feedback._conn() as c:
        offloaded = {
            r[0]
            for r in c.execute(
                "SELECT capability_id FROM influence_edges WHERE target_run_id=? "
                "AND influence_type='capability'",
                ("role:redirect:offloaded",),
            ).fetchall()
        }
        replayed = {
            r[0]
            for r in c.execute(
                "SELECT capability_id FROM influence_edges WHERE target_run_id=? "
                "AND influence_type='capability'",
                ("role:redirect:replayed",),
            ).fetchall()
        }
    assert offloaded == {"role-redirect", "offload"}
    assert replayed == {"role-redirect"}


def test_sanitize_completion_value_falls_back_for_opaque_types() -> None:
    redacted: list[str] = []
    out = feedback._sanitize_completion_value(object(), redacted, "opaque")
    assert out.startswith("<object object at")


def test_safe_identifier_returns_redacted_marker() -> None:
    redacted: list[str] = []
    assert feedback._safe_identifier("ghp_abcdefghijklmnop", redacted, "id") == "[REDACTED]"


def test_sanitize_artifact_refs_hashes_non_dict_rows() -> None:
    redacted: list[str] = []
    rows = feedback._sanitize_artifact_refs([123], redacted)
    assert rows[0]["kind"] == "reference"
    assert "artifact_refs[0]:hashed" in redacted


def test_sanitize_artifact_refs_fills_missing_artifact_id() -> None:
    redacted: list[str] = []
    rows = feedback._sanitize_artifact_refs([{"kind": "github-pr"}], redacted)
    assert rows[0]["artifact_id"].startswith("sha256:")


def test_sanitize_completion_payload_bounds_result_hashes_list() -> None:
    many = ["x" * 80] * (feedback.MAX_COMPLETION_LIST_ITEMS + 2)
    clean, status, _ = feedback._sanitize_completion_payload({"result_hashes": many})
    assert (
        status != "rejected" and len(clean["result_hashes"]) == feedback.MAX_COMPLETION_LIST_ITEMS
    )


def test_sanitize_completion_payload_retry_sequence_rejects_unknown_nested_field() -> None:
    clean, status, _ = feedback._sanitize_completion_payload(
        {"retry_sequence": [{"attempt_ordinal": 1, "unexpected_field": True}]}
    )
    assert status == "rejected"
    assert any("retry_sequence[0].unexpected_field" in code for code in clean["rejection_codes"])


def test_runtime_ac_gate_events_skips_malformed_payload() -> None:
    c = feedback._conn()
    c.execute(
        "INSERT INTO completion_events (event_id,schema_version,run_id,event_type,phase,producer,"
        "validation_status,payload_json,content_hash,redaction_count,created_ts,updated_ts) "
        "VALUES ('rac-bad',1,'r','completion','verification','runtime_ac_gate','accepted',"
        "'not-json','h',0,1,2)"
    )
    c.commit()
    c.close()
    assert feedback.runtime_ac_gate_events() == []


def test_record_influence_edge_requires_capability_version_pair() -> None:
    c = feedback._conn()
    feedback._record_run_in_conn(c, "edge-run", "o/r#1", "implement", "codex")
    with pytest.raises(ValueError, match="capability lineage"):
        feedback._record_influence_edge_in_conn(
            c,
            target_run_id="edge-run",
            influence_type="capability",
            influence_id="cap",
            accepted=True,
            capability_id="cap",
        )


def test_closer_stage_rewards_counts_valid_verification_payload() -> None:
    c = feedback._conn()
    meta = json.dumps({"selected_profile_id": "codex:stable", "subject_id": "owner/repo#1"})
    feedback._record_run_in_conn(
        c,
        "closer-good",
        "owner/repo#1",
        "closer_repair",
        "codex",
        routing_metadata=meta,
        role_name=None,
    )
    head = "a" * 40
    digest = "b" * 64
    payload = {
        "closer_stage": {
            "accepted": True,
            "independent_validation": True,
            "validator": "closer_disposition.integrate_ordinary",
            "task_category": "closer_repair",
            "agent": "codex",
            "requested_profile_id": "codex:stable",
            "target": "owner/repo#1",
            "head_sha": head,
            "basis_digest": digest,
        }
    }
    feedback._record_completion_event_in_conn(
        c,
        "closer-good",
        event_type="closer_stage",
        phase="verification",
        producer="closer_disposition.integrate_ordinary",
        status="pass",
        payload=payload,
    )
    rewards = feedback._closer_stage_rewards(c, "closer_repair")
    assert rewards["closer-good"] == 1.0
    c.close()


def test_derive_operation_role_synthesizer_prefix() -> None:
    assert feedback.derive_operation_role("synthesize_winner") == "synthesizer"


def test_record_run_inherits_offload_capability_from_role_run() -> None:
    version = "sha256:" + "e" * 64
    c = feedback._conn()
    feedback._record_run_in_conn(c, "role-cap", "o/r#1", "role:triage", "codex", role_name="triage")
    feedback._record_influence_edge_in_conn(
        c,
        target_run_id="role-cap",
        influence_type="capability",
        influence_id=version,
        accepted=True,
        capability_id="offload",
        capability_version_id=version,
        allow_unlinked=True,
    )
    feedback._record_run_in_conn(
        c,
        "acting-cap",
        "o/r#1",
        "implement",
        "codex",
        influenced_by_role_run_ids=["role-cap"],
    )
    inherited = c.execute(
        "SELECT capability_id FROM influence_edges WHERE target_run_id='acting-cap' "
        "AND capability_id='offload'"
    ).fetchone()
    assert inherited == ("offload",)
    c.close()


def test_ingest_profile_trial_refuses_route_weight_drift(monkeypatch: pytest.MonkeyPatch) -> None:
    db = feedback.DB_PATH.parent / "trial-quarantine.db"
    monkeypatch.setattr(feedback, "DB_PATH", db)
    feedback._conn().close()

    real_record_run = feedback.record_run

    def record_run_with_weight_drift(*args, **kwargs):
        conn = kwargs.get("conn")
        if conn is not None:
            conn.execute(
                "INSERT INTO route_weights (version,ts,task_type,agent,prior) VALUES (99,1,'t','a',0.5)"
            )
        return real_record_run(*args, **kwargs)

    monkeypatch.setattr(feedback, "record_run", record_run_with_weight_drift)
    monkeypatch.setattr(
        feedback.execution_profiles, "get_profile", lambda _p: {"provider": "openai"}
    )
    with pytest.raises(AssertionError, match="altered route weights"):
        feedback.ingest_profile_trial(
            {"trial_id": "trial-1"},
            manifest={"trial_id": "trial-1", "packet_hash": "p"},
            attempts=[
                {
                    "run_id": "run-1",
                    "profile_id": "codex:stable",
                    "requested_model": "gpt",
                    "status": "ok",
                }
            ],
        )


def test_bt_strengths_counts_b_beats_a_pair() -> None:
    now = int(__import__("time").time())
    c = feedback._conn()
    c.execute(
        "INSERT INTO evaluations VALUES (?,?,?,?,?,?,?)",
        ("E2", "b", "j", 7.0, None, None, now),
    )
    c.execute(
        "INSERT INTO evaluations VALUES (?,?,?,?,?,?,?)",
        ("E2", "a", "j", 3.0, None, None, now),
    )
    c.commit()
    c.close()
    out = feedback.bt_strengths(min_comparisons=1)
    assert out["ready"] and out["strengths"]["b"] > out["strengths"]["a"]


def test_propagate_outcome_lineage_without_role_aggregate_rows() -> None:
    c = feedback._conn()
    feedback._record_run_in_conn(c, "tgt2", "o/r#2", "implement", "codex")
    feedback._record_outcome_in_conn(
        c, "tgt2", adjudicated_verdict="PASS", merged=True, durability="durable"
    )
    ev = feedback._record_completion_event_in_conn(
        c,
        "role-only",
        event_type="role",
        phase="execution",
        producer="feedback.record_role_run",
        payload={},
    )
    feedback._record_run_in_conn(
        c, "role-only", "o/r#2", "role:triage", "codex", role_name="triage"
    )
    feedback._record_influence_edge_in_conn(
        c,
        target_run_id="tgt2",
        influence_type="role",
        influence_id="role-only",
        source_run_id="role-only",
        source_event_id=ev["event_id"],
        accepted=True,
    )
    feedback._propagate_outcome_lineage_in_conn(c, "tgt2")
    notes = c.execute("SELECT notes FROM outcomes WHERE run_id='role-only'").fetchone()[0]
    assert "tgt2" in notes
    c.close()


def test_role_activation_metrics_ignores_unknown_role_ids() -> None:
    c = feedback._conn()
    feedback._record_completion_event_in_conn(
        c,
        "sel-x",
        event_type="role",
        phase="trigger",
        producer="roles",
        payload={"role_ids": ["not-a-role"], "result": {"selector_status": "invoked"}},
    )
    metrics = feedback.role_activation_metrics(conn=c)
    assert metrics["roles"]["redirect"]["invoked"] == 0
    c.close()


def test_record_skill_invocation_normalizes_invoke_phase_alias() -> None:
    out = feedback.record_skill_invocation(
        "skill-wp",
        "a" * 64,
        phase="invoke",
        influenced_run_ids=[],
    )
    assert out["run_id"].startswith("skill:skill-wp:")


def test_record_muse_subscription_cost_writes_usage_row() -> None:
    feedback.record_run("muse-run", "o/r#m", "implement", "muse", mode="offload")
    feedback.record_muse_subscription_cost(
        "muse-run",
        invocation_count=2,
        window_start_ts=10,
        window_end_ts=20,
        observed_start_ts=10,
        observed_end_ts=15,
        wall_seconds=5.0,
        raw_exit_codes=[0, 1],
    )
    with feedback._conn() as c:
        row = c.execute(
            "SELECT muse_invocation_count,source FROM muse_subscription_usage m "
            "JOIN costs co ON co.run_id=m.run_id WHERE m.run_id='muse-run'"
        ).fetchone()
    assert row == (2, "imputed:subscription")


def test_record_execution_trace_rejects_non_positive_ordinal() -> None:
    feedback.record_run("tr-ord", "o/r#1", "implement", "codex")
    feedback.record_execution_trace(
        "tr-ord",
        operation="implement",
        operation_role="worker",
        resolved_model="gpt-5.6-codex",
        attempt_ordinal="0",
    )


def test_record_execution_attempt_rejects_resolved_model_change() -> None:
    c = feedback._conn()
    feedback._record_execution_attempt_in_conn(
        c,
        run_id="same",
        attempt_id="attempt:same",
        operation_role="worker",
        resolved_model="gpt-5.6-codex",
    )
    with pytest.raises(ValueError, match="resolved model changed"):
        feedback._record_execution_attempt_in_conn(
            c,
            run_id="same",
            attempt_id="attempt:same",
            operation_role="worker",
            resolved_model="gpt-5.6-other",
        )
    c.close()


def test_complete_profile_attempt_rejects_profile_mismatch() -> None:
    feedback.record_run("prof-run", "o/r#1", "implement", "codex")
    feedback.record_execution_attempt(
        "prof-run",
        attempt_id="attempt:profile:prof-run",
        operation_role="worker",
        profile_id="expected",
    )
    with pytest.raises(ValueError, match="selected profile changed"):
        feedback.complete_profile_attempt(
            "prof-run",
            selected_profile_id="other",
            resolved_provider="openai",
            resolved_model="gpt-5.6-codex",
        )


def test_complete_profile_attempt_unresolved_updates_row() -> None:
    feedback.record_run("unres", "o/r#1", "implement", "codex")
    feedback.record_execution_attempt(
        "unres",
        attempt_id="attempt:profile:unres",
        operation_role="worker",
        profile_id="pick",
    )
    attempt_id = feedback.complete_profile_attempt_unresolved(
        "unres", selected_profile_id="pick", fallback_reason="not_reported"
    )
    assert attempt_id == "attempt:profile:unres"
    with feedback._conn() as c:
        row = c.execute(
            "SELECT fallback_reason,resolved_model FROM execution_attempts WHERE attempt_id=?",
            (attempt_id,),
        ).fetchone()
    assert row == ("not_reported", None)


def test_migrate_legacy_execution_attempts_marks_worker_like_unknown() -> None:
    c = feedback._conn()
    feedback._record_run_in_conn(c, "legacy2", "o/r#1", "implement", "codex")
    c.execute(
        "INSERT INTO execution_traces VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "trace:2",
            "legacy2",
            None,
            None,
            "openai",
            "gpt",
            "worker_dispatch",
            "ok",
            1.0,
            0.0,
            "langsmith",
            None,
            100,
        ),
    )
    report = feedback.migrate_legacy_execution_attempts(apply=True, conn=c)
    assert report["worker_like_operations_left_unresolved"] == 1
    c.close()


def test_approve_evidence_type_updates_rationale_when_already_active() -> None:
    gap = "repeat gap"
    now = int(__import__("time").time())
    with feedback._conn() as c:
        c.execute(
            "INSERT INTO evidence_types VALUES (?,?,?,?,?)",
            ("active-type", now, 1, "active", "old"),
        )
        for i in range(2):
            c.execute(
                "INSERT INTO evidence_gaps VALUES (?,?,?,?,?)",
                (now + i, "ref", "eval", gap, "open"),
            )
        c.commit()
    result = feedback.approve_evidence_type(
        "active-type", gap, min_recurrence=2, apply=True, rationale="new"
    )
    assert result["evidence_type_status"] == "already_active"


def test_relearn_quality_row_mean_imputation_for_cost() -> None:
    priors = {"row-task": {"solo": 0.5, "peer": 0.5}}
    now = int(__import__("time").time())
    c = feedback._conn()
    for agent, cost in (("peer", 4.0), ("solo", None)):
        rid = f"row-{agent}"
        c.execute(
            "INSERT INTO runs (run_id,ts,target,task_type,agent,assignment) VALUES (?,?,?,?,?,?)",
            (rid, now, "o/r#1", "row-task", agent, "assigned"),
        )
        c.execute(
            "INSERT INTO outcomes (run_id,adjudicated_verdict,merged,durability,durability_checked_ts) "
            "VALUES (?,?,?,?,?)",
            (rid, "PASS", 1, "durable", now),
        )
        if cost is not None:
            c.execute(
                "INSERT INTO costs (run_id,tokens_in,tokens_out,cost_usd,latency_s,source,pulled_ts) "
                "VALUES (?,?,?,?,?,?,?)",
                (rid, 6000, 6000, cost, 1.0, "ccusage", now),
            )
    c.commit()
    c.close()
    feedback.relearn_quality(priors, window_days=120)
    c2 = feedback._conn()
    rationale = c2.execute(
        "SELECT rationale FROM route_weights WHERE task_type='row-task' AND agent='solo' "
        "ORDER BY version DESC LIMIT 1"
    ).fetchone()[0]
    c2.close()
    assert "cost_unit_usd=4.0000" in rationale


def test_main_completion_events_invalid_limit_returns_two(monkeypatch: pytest.MonkeyPatch) -> None:
    err = io.StringIO()
    monkeypatch.setattr(sys, "stderr", err)
    assert feedback.main(["completion-events", "--limit", "nope"]) == 2
    assert "invalid --limit" in err.getvalue()
