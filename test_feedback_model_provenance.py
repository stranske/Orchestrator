import time
import sqlite3

import pytest

import env_prereq
import feedback


def test_explicit_worker_role_cannot_override_evaluator_operation(tmp_path):
    old_db = feedback.DB_PATH
    feedback.DB_PATH = tmp_path / "feedback.db"
    try:
        feedback.record_run("role-conflict", "o/r#0", "implement", "codex")
        with pytest.raises(ValueError, match="contradicts operation"):
            feedback.record_execution_trace(
                "role-conflict",
                trace_id="judge",
                provider="anthropic",
                model="claude-judge",
                operation="evaluate_pr_compare",
                operation_role="worker",
                status="success",
            )
        assert feedback.resolved_worker_model_for_run("role-conflict") is None
    finally:
        feedback.DB_PATH = old_db


def test_partial_execution_attempt_schema_migrates_before_indexes(tmp_path):
    old_db = feedback.DB_PATH
    feedback.DB_PATH = tmp_path / "feedback.db"
    try:
        with sqlite3.connect(feedback.DB_PATH) as conn:
            conn.execute("CREATE TABLE execution_attempts (attempt_id TEXT PRIMARY KEY)")
        with feedback._conn() as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(execution_attempts)")}
            indexes = {row[1] for row in conn.execute("PRAGMA index_list(execution_attempts)")}
        assert {"operation_role", "status", "profile_id", "resolved_model"} <= columns
        assert {
            "idx_execution_attempts_run_role",
            "idx_execution_attempts_profile",
        } <= indexes
    finally:
        feedback.DB_PATH = old_db


def test_synthetic_adapter_tag_never_resolves_worker_model(tmp_path):
    old_db = feedback.DB_PATH
    feedback.DB_PATH = tmp_path / "feedback.db"
    try:
        feedback.record_run("synthetic", "o/r#tag", "implement", "codex")
        feedback.record_execution_trace(
            "synthetic",
            trace_id="worker-trace",
            model="codex:full:default",
            operation="implement",
            operation_role="worker",
            status="success",
        )
        assert feedback.resolved_worker_model_for_run("synthetic") is None
        for synthetic in ("codex:full:default", "cursor:composer-auto", "vibe:default"):
            with pytest.raises(ValueError, match="synthetic adapter tag"):
                feedback.record_execution_attempt(
                    "synthetic",
                    operation_role="worker",
                    resolved_model=synthetic,
                    status="success",
                )
    finally:
        feedback.DB_PATH = old_db


def test_trace_attempt_ordinals_do_not_overwrite(tmp_path):
    old_db = feedback.DB_PATH
    feedback.DB_PATH = tmp_path / "feedback.db"
    try:
        feedback.record_run("fallbacks", "o/r#fallback", "implement", "codex")
        for ordinal in (1, 2):
            feedback.record_execution_trace(
                "fallbacks",
                trace_id="shared-trace",
                model="gpt-judge",
                operation="evaluate_pr_compare",
                status="success",
                attempt_ordinal=ordinal,
            )
        with feedback._conn() as conn:
            rows = conn.execute(
                "SELECT attempt_ordinal FROM execution_attempts "
                "WHERE run_id='fallbacks' ORDER BY attempt_ordinal"
            ).fetchall()
        assert rows == [(1,), (2,)]
    finally:
        feedback.DB_PATH = old_db


def test_missing_fallback_ordinals_are_allocated_transactionally(tmp_path):
    old_db = feedback.DB_PATH
    feedback.DB_PATH = tmp_path / "feedback.db"
    try:
        feedback.record_run("fallback-auto", "o/r#fallback-auto", "implement", "codex")
        for status in ("failed", "success"):
            feedback.record_execution_trace(
                "fallback-auto",
                trace_id="same-trace",
                model="gpt-judge",
                operation="evaluate_pr_compare",
                status=status,
                attempt_ordinal=None,
            )
        with feedback._conn() as conn:
            rows = conn.execute(
                "SELECT attempt_ordinal,status FROM execution_attempts "
                "WHERE run_id='fallback-auto' ORDER BY attempt_ordinal"
            ).fetchall()
        assert rows == [(1, "failed"), (2, "success")]
    finally:
        feedback.DB_PATH = old_db


def test_legacy_parent_evaluation_does_not_duplicate_across_exact_members(tmp_path):
    old_db = feedback.DB_PATH
    feedback.DB_PATH = tmp_path / "feedback.db"
    try:
        for member in ("m1", "m2"):
            feedback.record_run(
                f"E:{member}",
                "o/r#arms",
                "arm-dedupe",
                "codex",
                experiment_id="E",
                routing_metadata={
                    "experiment_arm_id": member,
                    "experiment_member_id": member,
                },
            )
        feedback.record_evaluation("E", "codex", "judge", 8.0)
        version = feedback.relearn_quality({"arm-dedupe": {"codex": 0.5}})
        row = feedback.current_weights("arm-dedupe", version)[0]
        assert row["n_obs"] == 0
    finally:
        feedback.DB_PATH = old_db


def test_worker_coverage_excludes_known_nonworker_runs(tmp_path):
    old_db = feedback.DB_PATH
    feedback.DB_PATH = tmp_path / "feedback.db"
    try:
        feedback.record_run("worker", "o/r#worker", "implement", "codex")
        feedback.record_execution_attempt(
            "worker",
            operation_role="worker",
            resolved_provider="openai",
            resolved_model="gpt-worker",
            status="success",
        )
        feedback.record_run("judge-run", "o/r#judge", "evaluate_pr", "claude")
        feedback.record_execution_attempt(
            "judge-run",
            operation_role="evaluator",
            resolved_provider="anthropic",
            resolved_model="claude-judge",
            status="success",
        )
        report = feedback.worker_model_provenance_summary()
        assert report["runs_total"] == 2
        assert report["eligible_worker_runs"] == 1
        assert report["excluded_nonworker_runs"] == 1
        assert report["resolved_worker_runs"] == 1
        assert report["unknown_worker_runs"] == 0
    finally:
        feedback.DB_PATH = old_db


def test_pre_migration_collision_is_not_reported_healthy(tmp_path):
    old_db = feedback.DB_PATH
    feedback.DB_PATH = tmp_path / "feedback.db"
    try:
        feedback.record_run(
            "legacy-collision",
            "o/r#legacy-collision",
            "implement",
            "codex",
            model="claude-judge",
        )
        with feedback._conn() as conn:
            conn.execute(
                "INSERT INTO execution_traces VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "legacy:unmigrated",
                    "legacy-collision",
                    "judge",
                    None,
                    "anthropic",
                    "claude-judge",
                    "evaluate_pr_compare",
                    "success",
                    1.0,
                    0.1,
                    "legacy",
                    "legacy.ndjson:1",
                    int(time.time()),
                ),
            )
        report = feedback.worker_model_provenance_summary()
        assert report["unmigrated_legacy_trace_rows"] == 1
        assert report["legacy_migration_complete"] is False
        assert report["legacy_worker_nonworker_model_collision_runs"] == 1
    finally:
        feedback.DB_PATH = old_db


def test_evaluator_trace_cannot_resolve_worker_model(tmp_path):
    old_db = feedback.DB_PATH
    feedback.DB_PATH = tmp_path / "feedback.db"
    try:
        feedback.record_run("codex-worker", "stranske/Workflows#1", "implement", "codex")
        feedback.record_execution_trace(
            "codex-worker",
            trace_id="claude-judge",
            provider="anthropic",
            model="claude-sonnet-4-6",
            operation="evaluate_pr_compare",
            status="success",
        )
        feedback.record_execution_trace(
            "codex-worker",
            trace_id="gpt-judge",
            provider="openai",
            model="gpt-5.6",
            operation="evaluate_pr_compare",
            status="success",
        )

        assert (
            feedback.resolved_worker_model_for_run("codex-worker") is None
        ), "evaluator trace resolved worker model"
        with feedback._conn() as conn:
            roles = conn.execute(
                "SELECT DISTINCT operation_role FROM execution_attempts "
                "WHERE run_id='codex-worker'"
            ).fetchall()
            legacy_model = conn.execute(
                "SELECT model FROM runs WHERE run_id='codex-worker'"
            ).fetchone()[0]
        assert roles == [("evaluator",)]
        assert legacy_model is None
    finally:
        feedback.DB_PATH = old_db


def test_successful_worker_attempt_is_the_only_resolver(tmp_path):
    old_db = feedback.DB_PATH
    feedback.DB_PATH = tmp_path / "feedback.db"
    try:
        feedback.record_run("worker", "o/r#2", "implement", "codex")
        feedback.record_execution_attempt(
            "worker",
            attempt_id="failed-worker",
            operation_role="worker",
            profile_id="openai/gpt-new/high",
            resolved_provider="openai",
            resolved_model="gpt-new",
            status="failed",
        )
        assert feedback.resolved_worker_model_for_run("worker") is None
        feedback.record_execution_attempt(
            "worker",
            attempt_id="successful-worker",
            attempt_ordinal=2,
            operation_role="worker",
            profile_id="openai/gpt-new/high",
            requested_provider="openai",
            requested_model="gpt-new",
            resolved_provider="openai",
            resolved_model="gpt-new-2026-07-01",
            status="success",
        )
        assert feedback.resolved_worker_model_for_run("worker") == "gpt-new-2026-07-01"
    finally:
        feedback.DB_PATH = old_db


def test_unresolved_worker_provenance_remains_agent_level_evidence(tmp_path):
    old_db = feedback.DB_PATH
    feedback.DB_PATH = tmp_path / "feedback.db"
    try:
        feedback.record_run("unresolved", "o/r#4", "profile-safe", "codex")
        feedback.record_execution_trace(
            "unresolved",
            trace_id="judge-only",
            provider="anthropic",
            model="claude-judge",
            operation="evaluate_pr_compare",
            status="success",
        )
        feedback.record_outcome(
            "unresolved",
            adjudicated_verdict="PASS",
            merged=True,
            durability="durable",
        )
        feedback.record_run("current", "o/r#5", "profile-safe", "codex")
        feedback.record_execution_attempt(
            "current",
            operation_role="worker",
            profile_id="openai/gpt-worker/high",
            resolved_provider="openai",
            resolved_model="gpt-worker",
            status="success",
        )

        version = feedback.relearn_quality({"profile-safe": {"codex": 0.5}})
        weight = feedback.current_weights("profile-safe", version)[0]
        assert weight["n_obs"] == 1
        assert weight["posterior"] > 0.5
        with feedback._conn() as conn:
            rationale = conn.execute(
                "SELECT rationale FROM route_weights WHERE version=? "
                "AND task_type='profile-safe' AND agent='codex'",
                (version,),
            ).fetchone()[0]
        assert "superseded_model_runs=0" in rationale
    finally:
        feedback.DB_PATH = old_db


def test_legacy_migration_is_additive_and_conservative(tmp_path):
    old_db = feedback.DB_PATH
    feedback.DB_PATH = tmp_path / "feedback.db"
    try:
        feedback.record_run("legacy", "o/r#3", "implement", "codex", model="claude-sonnet-4-6")
        with feedback._conn() as conn:
            conn.execute(
                "INSERT INTO execution_traces VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "legacy:evaluator",
                    "legacy",
                    "trace-legacy",
                    None,
                    "anthropic",
                    "claude-sonnet-4-6",
                    "evaluate_pr_compare",
                    "success",
                    1.0,
                    0.1,
                    "legacy",
                    "fixture:1",
                    int(time.time()),
                ),
            )
            before = {
                "runs": conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0],
                "traces": conn.execute("SELECT COUNT(*) FROM execution_traces").fetchone()[0],
            }

        report = feedback.migrate_legacy_execution_attempts(apply=True)
        with feedback._conn() as conn:
            after = {
                "runs": conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0],
                "traces": conn.execute("SELECT COUNT(*) FROM execution_traces").fetchone()[0],
            }
            role = conn.execute(
                "SELECT operation_role FROM execution_attempts "
                "WHERE trace_key='legacy:evaluator'"
            ).fetchone()[0]
        assert before == after
        assert report["legacy_rows_preserved"] is True
        assert report["legacy_worker_nonworker_model_collision_runs"] == 1
        assert role == "evaluator"
        assert feedback.resolved_worker_model_for_run("legacy") is None
    finally:
        feedback.DB_PATH = old_db


def test_multi_capability_run_records_one_edge_each(tmp_path):
    """A work process using SEVERAL capabilities must attribute to all of them.

    `direct_capability_id` on the completion event is a single column, so it is set only in the
    one-capability case — which meant a run declaring two capabilities recorded attribution for
    neither. Edges are the many-to-many surface (2026-08-09).
    """
    # The Brain here is a fresh tmp DB, but the LEDGER is not: the edge writer resolves each
    # capability's version lineage from it and refuses an edge without one (all-or-nothing, so a
    # capability is never credited with a borrowed version). A ledger row with no lineage is the
    # unregistered case, which the sibling test asserts produces no attribution.
    env_prereq.require(
        env_prereq.ledger_rows_absent("adversarial-review", "testgen-lane"),
        env_prereq.ledger_version_lineage_absent("adversarial-review", "testgen-lane"),
    )
    old_db = feedback.DB_PATH
    feedback.DB_PATH = tmp_path / "feedback.db"
    try:
        feedback.record_run(
            "multi",
            "o/r#1",
            "review",
            "codex",
            capability_ids=["adversarial-review", "testgen-lane"],
        )
        with feedback._conn() as c:
            rows = c.execute(
                "SELECT capability_id, capability_version_id FROM influence_edges "
                "WHERE target_run_id='multi' AND influence_type='capability' "
                "ORDER BY capability_id"
            ).fetchall()
        assert [r[0] for r in rows] == ["adversarial-review", "testgen-lane"], rows
        assert all(r[1] for r in rows), "every capability edge needs its version lineage"
    finally:
        feedback.DB_PATH = old_db


def test_unversioned_capability_is_unattributed_not_misattributed(tmp_path):
    """All-or-nothing version resolution: a capability without lineage must not be recorded with
    a borrowed or invented version, and must not misalign the id/version pairing."""
    old_db = feedback.DB_PATH
    feedback.DB_PATH = tmp_path / "feedback.db"
    try:
        feedback.record_run(
            "mixed",
            "o/r#2",
            "review",
            "codex",
            capability_ids=["adversarial-review", "no-such-capability"],
        )
        with feedback._conn() as c:
            rows = c.execute(
                "SELECT capability_id FROM influence_edges WHERE target_run_id='mixed'"
            ).fetchall()
        assert rows == [], "partial lineage must yield no attribution rather than a wrong one"
    finally:
        feedback.DB_PATH = old_db


def test_role_run_creates_a_capability_tagged_edge(tmp_path):
    """Roles are the one production path that knows its own capability, so they are where
    capability attribution starts. Until 2026-08-11 the id went only into a completion-event
    payload and never to record_run, so 81 influence edges carried 0 capability tags and
    reconcile_causal_lifecycle — which reads exactly those tags — saw no evidence ever.
    """
    # Same prerequisite as the multi-capability case: no version lineage in the ledger, no edge.
    env_prereq.require(
        env_prereq.ledger_rows_absent("role-triage"),
        env_prereq.ledger_version_lineage_absent("role-triage"),
    )
    old_db = feedback.DB_PATH
    feedback.DB_PATH = tmp_path / "feedback.db"
    try:
        feedback.record_role_run(
            "role:triage:gemini:1", "triage", "stranske/Workflows#1", "gemini", action="propose"
        )
        with feedback._conn() as c:
            rows = c.execute(
                "SELECT influence_type, capability_id, capability_version_id "
                "FROM influence_edges WHERE target_run_id='role:triage:gemini:1'"
            ).fetchall()
        assert rows, "a role run must produce an influence edge"
        assert any(r[0] == "capability" and r[1] == "role-triage" for r in rows), rows
        # Version lineage is required alongside identity, else the edge writer refuses it.
        tagged = [r for r in rows if r[1] == "role-triage"]
        assert all(r[2] for r in tagged), "capability edge needs its version id"
    finally:
        feedback.DB_PATH = old_db
