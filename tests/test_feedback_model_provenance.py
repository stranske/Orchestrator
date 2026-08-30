import json
import sqlite3
import time

import pytest

import adapters
import env_prereq
import feedback


@pytest.fixture(autouse=True)
def _isolate_ledger_state(tmp_path, monkeypatch):
    monkeypatch.setattr(adapters, "HANDOFF", tmp_path)
    monkeypatch.setattr(adapters, "LEDGER", tmp_path / "capacity-ledger.ndjson")
    monkeypatch.setenv("HANDOFF_DIR", str(tmp_path))


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


def test_placeholder_is_never_accepted_as_a_resolved_model():
    """`SYNTHETIC_ADAPTER_MODEL_RE` only catches `agent:` tags, so a function named
    `validate_resolved_worker_model` accepted `<synthetic>`, `unknown`, `none` and `default`.

    Claude's own transcripts really do write `"model": "<synthetic>"` on some turns, so this was
    reachable from live data, not hypothetical. Admitting one makes an UNRESOLVED attempt look
    resolved, which inverts the one guarantee this table provides.
    """
    for placeholder in ("<synthetic>", "unknown", "none", "null", "default", "[unset]", "N/A"):
        with pytest.raises(ValueError):
            feedback.validate_resolved_worker_model(placeholder)

    # The refusal must stay NARROW -- a real vendor id can never be caught by it.
    for real in (
        "gpt-5.6-terra",
        "claude-opus-5",
        "mistral-medium-3.5",
        "gemini-3.1-pro",
        "composer-2.5",
        "mistral/codestral-latest",
    ):
        assert feedback.validate_resolved_worker_model(real) == real

    # And the adapter-tag rule it already had is untouched.
    for tag in ("codex:full:default", "cursor:composer-1", "agy:gemini-3.1-pro"):
        with pytest.raises(ValueError, match="synthetic adapter tag"):
            feedback.validate_resolved_worker_model(tag)


def test_cli_reported_identity_resolves_only_seats_that_actually_report(tmp_path, monkeypatch):
    """A CLI-reported model is real provenance; a seat that cannot report gets a NAMED reason.

    `execution_attempts.resolved_model` had no writer outside the quarantined trial bridge, so
    every research-claiming completion event in the system was blocked on
    `unresolved_model_provenance`. §2 permits exactly this source -- "a local Codex session rollout
    may establish CLI-reported identity" -- so the reader reads the CLI's own log.
    """
    import json

    import adapters
    import ledger_reconcile

    workspace = tmp_path / "offloads" / "20260822T000000Z-issue-1-1"
    workspace.mkdir(parents=True)
    sessions = tmp_path / "sessions" / "2026" / "08" / "22"
    sessions.mkdir(parents=True)
    rollout = sessions / "rollout-2026-08-22T00-00-00-abc.jsonl"
    rollout.write_text(
        json.dumps(
            {
                "type": "session_meta",
                "payload": {"cwd": str(workspace.resolve()), "cli_version": "0.149.0"},
            }
        )
        + "\n"
        + json.dumps({"type": "turn_context", "payload": {"model": "gpt-5.6-terra"}})
        + "\n"
    )
    monkeypatch.setattr(adapters, "CODEX_SESSIONS", tmp_path / "sessions")

    found = adapters.cli_reported_model("codex", workspace)
    assert found["model"] == "gpt-5.6-terra", found
    assert found["cli_version"] == "0.149.0"
    assert found["reason"] is None

    # NEVER GUESSED. A rollout that names no real model resolves to nothing, with a reason --
    # the requested model and the catalog are both deliberately unreachable from here.
    rollout.write_text(
        json.dumps({"type": "session_meta", "payload": {"cwd": str(workspace.resolve())}})
        + "\n"
        + json.dumps({"type": "turn_context", "payload": {"model": "<synthetic>"}})
        + "\n"
    )
    blank = adapters.cli_reported_model("codex", workspace)
    assert blank["model"] is None
    assert blank["reason"] == "codex_rollout_named_no_real_model"

    # A seat with NO reader says so by name. This list was once ("cursor", "gemini", "vibe") --
    # wrong for two of the three, because cursor and vibe both keep per-session model records and
    # the claim they did not was asserted from a truncated directory listing.
    # DERIVED, not hardcoded. This list read ("cursor","gemini","vibe") and then ("gemini","aider"),
    # and each time a seat left it once its store or log was actually read. Ask the authority.
    for agent in adapters.NO_SESSION_LOG_AGENTS:
        reason = adapters.cli_reported_model(agent, workspace)["reason"]
        assert reason.startswith("no_cli_session_log:"), (agent, reason)
        assert len(reason) > 30, f"{agent} needs a real justification, got {reason!r}"
    # And a seat WITH a store reader reports a miss against its own store, not an incapacity.
    for agent in ("cursor", "vibe", "gemini"):
        reason = adapters.cli_reported_model(agent, workspace)["reason"]
        assert reason == f"no_{agent}_session_matched_workspace", (agent, reason)

    # A run with no workspace recorded is distinguishable from a seat that cannot report.
    assert adapters.cli_reported_model("codex", None)["reason"] == "no_workspace_recorded_for_run"

    # The workspace join comes from the target the dispatcher ALREADY writes.
    rows = [{"agent": "codex", "target": f"offload:{workspace}", "event": "start", "ts": 1}]
    assert ledger_reconcile._workspace_from_rows(rows) == str(workspace)
    assert ledger_reconcile._workspace_from_rows([{"target": "stranske/Repo#1"}]) is None


def test_completion_records_cli_identity_and_never_invents_one(tmp_path, monkeypatch):
    """End-to-end: `started` -> `complete` with a real resolved model, or `unresolved` with a reason.

    DELIBERATE BREAK -> REVERT is at the bottom: disabling the reader puts the chain back exactly
    where it was (unresolved, generic reason, no exact-model claim available), which is what made
    `unresolved_model_provenance` unavoidable on 203 of 203 events.
    """
    import json

    import adapters
    import ledger_reconcile

    old_db = feedback.DB_PATH
    feedback.DB_PATH = tmp_path / "feedback.db"
    try:
        workspace = tmp_path / "offloads" / "ws-1"
        workspace.mkdir(parents=True)
        sessions = tmp_path / "sessions"
        (sessions / "2026").mkdir(parents=True)
        # The filename stamp is load-bearing: the reader narrows to the run's own window before
        # opening anything, because scanning every rollout file is what makes `codex doctor` 12.2s.
        # Derive it from the same clock the completion uses so this test is not time-of-day flaky.
        started = int(time.time())
        stamp = time.strftime("%Y-%m-%dT%H-%M-%S", time.localtime(started))
        (sessions / "2026" / f"rollout-{stamp}-x.jsonl").write_text(
            json.dumps({"type": "session_meta", "payload": {"cwd": str(workspace.resolve())}})
            + "\n"
            + json.dumps({"type": "turn_context", "payload": {"model": "gpt-5.6-terra"}})
            + "\n"
        )
        monkeypatch.setattr(adapters, "CODEX_SESSIONS", sessions)

        def _run(agent, profile_id, run_id):
            feedback.record_run(run_id, f"offload:{workspace}", "offload", agent)
            feedback.record_execution_attempt(
                run_id,
                attempt_id=f"attempt:profile:{run_id}",
                operation_role="worker",
                profile_id=profile_id,
                requested_provider="x",
                requested_model="y",
                status="started",
                source="orchestrator-profile-decision",
                started_ts=started,
            )
            ledger_reconcile.record_completion(
                run_id,
                agent,
                target=f"offload:{workspace}",
                task_type="offload",
                selected_profile_id=profile_id,
                started_ts=started,
            )
            return (
                sqlite3.connect(feedback.DB_PATH)
                .execute(
                    "SELECT status, resolved_provider, resolved_model, fallback_reason "
                    "FROM execution_attempts WHERE run_id=?",
                    (run_id,),
                )
                .fetchone()
            )

        status, provider, model, _ = _run("codex", "codex-5.6-terra-high", "r-codex")
        assert (status, provider, model) == ("complete", "openai", "gpt-5.6-terra")
        assert feedback.latest_worker_identity_for_agent("codex") is not None

        # A seat whose store holds nothing for THIS workspace stays unresolved, with the reason
        # naming which store was searched -- distinct from a seat that has no store at all.
        monkeypatch.setattr(adapters, "CURSOR_CHATS", tmp_path / "no-cursor-chats")
        status, _, model, reason = _run("cursor", "cursor-composer-2.5", "r-cursor")
        assert status == "unresolved" and model is None
        assert "no_cursor_session_matched_workspace" in reason, reason
        assert feedback.latest_worker_identity_for_agent("cursor") is None

        # DELIBERATE BREAK: reader always blank, as before the fix.
        monkeypatch.setattr(
            adapters,
            "cli_reported_model",
            lambda *a, **k: {"model": None, "cli_version": None, "source": None, "reason": None},
        )
        status, _, model, _ = _run("codex", "codex-5.6-terra-high", "r-broken")
        assert (status, model) == ("unresolved", None), "the break must restore the old behaviour"
        # REVERTED by monkeypatch teardown; the codex row above still carries real provenance.
    finally:
        feedback.DB_PATH = old_db


def test_reconcile_finds_the_profile_when_the_ledger_never_carried_one(tmp_path, monkeypatch):
    """The ledger `start` row has no `selected_profile_id` field, so the resolution branch was dead.

    Symptom in production: 250 marker backfills, 0 resolved, 0 unresolved, every run. `profile_ids`
    was built solely from ledger rows that never contained the key, so the branch that resolves a
    worker attempt could not execute at all — a gate whose drain could never run.
    """
    import ledger_reconcile

    old_db = feedback.DB_PATH
    feedback.DB_PATH = tmp_path / "feedback.db"
    try:
        feedback.record_run("r-attempt", "offload:/tmp/ws", "offload", "codex")
        feedback.record_execution_attempt(
            "r-attempt",
            attempt_id="attempt:profile:r-attempt",
            operation_role="worker",
            profile_id="codex-5.6-terra-high",
            requested_provider="openai",
            requested_model="gpt-5.6-terra",
            status="started",
            source="orchestrator-profile-decision",
            started_ts=int(time.time()),
        )
        # The attempt knows its own profile even though no ledger row ever said so.
        assert ledger_reconcile._profile_id_from_attempt("r-attempt") == "codex-5.6-terra-high"
        # A run with no worker attempt yields None rather than a guess.
        assert ledger_reconcile._profile_id_from_attempt("r-missing") is None

        # Once resolved, it is NOT offered again — otherwise reconcile would keep reopening
        # attempts that already carry provenance.
        feedback.complete_profile_attempt(
            "r-attempt",
            selected_profile_id="codex-5.6-terra-high",
            resolved_provider="openai",
            resolved_model="gpt-5.6-terra",
        )
        assert ledger_reconcile._profile_id_from_attempt("r-attempt") is None
    finally:
        feedback.DB_PATH = old_db


def test_a_resolved_attempt_cannot_also_have_fallen_back(tmp_path):
    """`resolved_model_coverage` counts `fallback_reason IS NOT NULL`, so a stale reason made a
    fully-resolved profile report coverage 1.00 AND fallback_rate 1.00 at the same time.

    That is not cosmetic: fallback_rate is how a profile's health is read, and it appeared the
    moment a late sweep began resolving attempts that had already been closed unresolved.
    """
    import execution_profiles

    old_db = feedback.DB_PATH
    feedback.DB_PATH = tmp_path / "feedback.db"
    try:
        feedback.record_run("fb-1", "offload:/tmp/ws", "offload", "codex")
        feedback.record_execution_attempt(
            "fb-1",
            attempt_id="attempt:profile:fb-1",
            operation_role="worker",
            profile_id="codex-5.6-terra-high",
            requested_provider="openai",
            requested_model="gpt-5.6-terra",
            status="started",
            started_ts=int(time.time()),
            source="orchestrator-profile-decision",
        )
        # Closed unresolved first -- which is what every pre-reader attempt did.
        feedback.complete_profile_attempt_unresolved(
            "fb-1",
            selected_profile_id="codex-5.6-terra-high",
            fallback_reason="resolved_model_not_reported_by_offload",
        )
        # Then resolved later by the sweep.
        feedback.complete_profile_attempt(
            "fb-1",
            selected_profile_id="codex-5.6-terra-high",
            resolved_provider="openai",
            resolved_model="gpt-5.6-terra",
        )
        row = (
            sqlite3.connect(feedback.DB_PATH)
            .execute(
                "SELECT resolved_model, fallback_reason FROM execution_attempts WHERE run_id='fb-1'"
            )
            .fetchone()
        )
        assert row[0] == "gpt-5.6-terra", row
        assert row[1] is None, ("a resolved attempt did not fall back", row)

        with sqlite3.connect(feedback.DB_PATH) as conn:
            cov = execution_profiles.resolved_model_coverage(conn, "codex-5.6-terra-high")
        assert cov["coverage"] == 1.0, cov
        # THE CONTRADICTION: 100% coverage with 100% fallback is what the stale field produced.
        assert cov["fallback_rate"] == 0.0, cov
    finally:
        feedback.DB_PATH = old_db


def test_every_seat_with_a_session_store_can_report_and_is_read_from_its_own_store(
    tmp_path, monkeypatch
):
    """Four seats were declared incapable of reporting a model. All four were wrong.

    The claim `cursor-agent writes no per-session model log under ~/.cursor` was asserted from an
    eight-line `find | head` — inferring a blocker instead of verifying it. Looking properly:
    cursor keeps `providerOptions.cursor.modelName` in `~/.cursor/chats/*/*/store.db` joined by the
    sibling meta's `cwd`, and vibe keeps `config.active_model` in its session meta joined by
    `environment.working_directory`. Both are the tool's own record of the run, not our `--model`
    echoed back.

    Also pins the one seat that genuinely cannot: agy gives a workspace join but records no served
    model, and it is the seat where that matters most because it is a multi-provider router whose
    requested model may not be what ran.
    """
    import json as _json
    import sqlite3 as _sq

    import adapters

    workspace = tmp_path / "ws"
    workspace.mkdir()
    resolved_ws = str(workspace.resolve())

    # --- cursor: chat store keyed by cwd, model in the provider's own options ---
    chats = tmp_path / "chats" / "hash" / "session"
    chats.mkdir(parents=True)
    (chats / "meta.json").write_text(_json.dumps({"cwd": resolved_ws, "updatedAtMs": 1_000_000}))
    store = _sq.connect(chats / "store.db")
    store.execute("CREATE TABLE blobs (id INTEGER, data BLOB)")
    store.execute(
        "INSERT INTO blobs VALUES (1, ?)",
        (b'{"providerOptions":{"cursor":{"modelName":"composer-2.5"}}}',),
    )
    store.commit()
    store.close()
    monkeypatch.setattr(adapters, "CURSOR_CHATS", tmp_path / "chats")
    got = adapters.cli_reported_model("cursor", workspace)
    assert got["model"] == "composer-2.5", got

    # --- vibe: session meta keyed by working_directory ---
    vibe = tmp_path / "vibe" / "session_1"
    vibe.mkdir(parents=True)
    (vibe / "meta.json").write_text(
        _json.dumps(
            {
                "environment": {"working_directory": resolved_ws},
                "config": {"active_model": "mistral-medium-3.5"},
                "start_time": "2026-08-22T15:00:00+00:00",
            }
        )
    )
    monkeypatch.setattr(adapters, "VIBE_SESSIONS", tmp_path / "vibe")
    got = adapters.cli_reported_model("vibe", workspace)
    assert got["model"] == "mistral-medium-3.5", got

    # NEVER OUR REQUEST. A store that names no model resolves to nothing with a reason -- the
    # requested model is unreachable from here by construction.
    (chats / "meta.json").write_text(_json.dumps({"cwd": "/somewhere/else"}))
    blank = adapters.cli_reported_model("cursor", workspace)
    assert blank["model"] is None and "no_cursor_session" in blank["reason"], blank

    # A LOG FULL OF FILENAMES IS NOT A MODEL. This is the false-positive class that made log
    # parsing look unusable: offload logs are full of `gpt-4o-mini` and `claude-fleet-list.sh`
    # because agents edit code about models.
    assert adapters.VENDOR_MODEL_RE.fullmatch("claude-fleet-list.sh") is None
    assert adapters.VENDOR_MODEL_RE.fullmatch("composer-2.5") is not None

    # The reader list and the capability answer come from ONE place, so "we have a reader" and
    # "we will look" cannot disagree -- they did, for three seats whose readers were written.
    for seat in adapters.CLI_IDENTITY_READERS:
        assert adapters.can_report_cli_identity(seat)[0] is True, seat
    for seat in adapters.NO_SESSION_LOG_AGENTS:
        capable, why = adapters.can_report_cli_identity(seat)
        assert capable is False and why, seat
    # gemini is NOT here any more: its conversation store records no model, but its per-run CLI log
    # does (`Propagating selected model override to backend: label="..."`), so the dispatcher gives
    # each gemini run its own log and resolves from it.
    assert "gemini" not in adapters.NO_SESSION_LOG_AGENTS, adapters.NO_SESSION_LOG_AGENTS
    assert (
        adapters.model_label_from_agy_log(
            "model_config_manager.go:311] Propagating selected model override to backend: "
            'label="Gemini 3.6 Flash (High)"'
        )
        == "Gemini 3.6 Flash (High)"
    )


def test_late_sweep_completes_terminal_attempts_never_one_in_flight(tmp_path, monkeypatch):
    """The sweep may finish a TERMINAL unresolved attempt; it must not finish a running one.

    The profile attempt row is written `started` BEFORE the subprocess is spawned, so an in-flight
    attempt matches every other clause of the sweep's query -- worker role, profile set,
    `resolved_model` still NULL. `cli_reported_model` reads the first model in the session log
    within a 2h window of `started_ts`, and that log exists from the moment the CLI starts, so a
    run that is still executing probes CLEAN. Before the status filter, `--apply` stamped it
    `complete` with a resolved model and a `completed_ts` taken from the sweep's own clock: the one
    row shape allowed to support an exact-model claim, minted for a worker that had not finished and
    could still fall back, retry onto another model, or fail outright.

    All three runs below share one workspace, so the probe resolves for ALL of them. The status
    filter is therefore the only thing that can protect the two ineligible rows.
    """
    import json

    import adapters
    import ledger_reconcile

    old_db = feedback.DB_PATH
    feedback.DB_PATH = tmp_path / "feedback.db"
    try:
        workspace = tmp_path / "offloads" / "ws-sweep"
        workspace.mkdir(parents=True)
        sessions = tmp_path / "sessions"
        (sessions / "2026").mkdir(parents=True)
        started = int(time.time())
        stamp = time.strftime("%Y-%m-%dT%H-%M-%S", time.localtime(started))
        (sessions / "2026" / f"rollout-{stamp}-x.jsonl").write_text(
            json.dumps({"type": "session_meta", "payload": {"cwd": str(workspace.resolve())}})
            + "\n"
            + json.dumps({"type": "turn_context", "payload": {"model": "gpt-5.6-terra"}})
            + "\n"
        )
        monkeypatch.setattr(adapters, "CODEX_SESSIONS", sessions)

        def _pending(run_id):
            """A pre-dispatch worker attempt: exactly what the dispatcher writes before spawning."""
            feedback.record_run(run_id, f"offload:{workspace}", "offload", "codex")
            feedback.record_execution_attempt(
                run_id,
                attempt_id=f"attempt:profile:{run_id}",
                operation_role="worker",
                profile_id="codex-5.6-terra-high",
                requested_provider="openai",
                requested_model="gpt-5.6-terra",
                status="started",
                source="orchestrator-profile-decision",
                started_ts=started,
            )

        def _row(run_id):
            return (
                sqlite3.connect(feedback.DB_PATH)
                .execute(
                    "SELECT status, resolved_model, completed_ts FROM execution_attempts "
                    "WHERE run_id=?",
                    (run_id,),
                )
                .fetchone()
            )

        # TERMINAL: the completion path closed it unresolved. This is the sweep's whole purpose.
        _pending("sweep-terminal")
        feedback.complete_profile_attempt_unresolved(
            "sweep-terminal",
            selected_profile_id="codex-5.6-terra-high",
            fallback_reason="resolved_model_not_reported_by_completion",
        )
        # IN FLIGHT: the worker is still running, so nothing has closed it.
        _pending("sweep-inflight")
        # TERMINAL BUT NEVER RAN: the process failed to start, so no model ever served it.
        _pending("sweep-failed")
        feedback.complete_profile_attempt_unresolved(
            "sweep-failed",
            selected_profile_id="codex-5.6-terra-high",
            fallback_reason="profile_process_start_failed",
            status="failed",
        )

        report = ledger_reconcile.resolve_unresolved_worker_attempts(apply=True)

        # The terminal unresolved row gains the identity its log always carried.
        assert _row("sweep-terminal")[:2] == ("complete", "gpt-5.6-terra")
        # The in-flight row is untouched: no resolved model, no invented completion timestamp.
        assert _row("sweep-inflight") == ("started", None, None)
        # The never-ran row keeps its terminal failure rather than borrowing a neighbour's model.
        assert _row("sweep-failed")[:2] == ("failed", None)
        assert report["resolved_by_agent"] == {"codex": 1}
        # `candidates` counts only what a reader could still drain.
        assert report["candidates"] == 1
        # The exclusions are NAMED, not silently narrowed away: `candidates: 0` next to
        # `{"started": 1}` reads as "wait for that run"; `candidates: 0` alone reads as "broken".
        assert report["excluded_not_terminal"] == {"started": 1, "failed": 1}

        # DELIBERATE BREAK: drop the status clause, restoring the pre-fix query exactly.
        #
        # This is keyed on an EXACT substring, trailing space included, so it has to fail loudly
        # when that substring stops matching. A reformatted query would make `replace` a silent
        # no-op: the filter would keep protecting the row, and the corruption assertion below would
        # fail while blaming the fix rather than this fixture. Two guards, because there are two
        # ways to go stale -- the clause moves within a query still recognisable (caught per
        # statement), or the query itself becomes unrecognisable so nothing is ever stripped
        # (caught by `stripped` after the run). Only the second covers an aliased or re-ordered
        # rewrite, which is why the per-statement assert alone is not enough.
        real_conn = feedback._conn
        clause = "AND ea.status='unresolved' "
        stripped = []

        class _Unfiltered:
            def __init__(self, c):
                self._c = c

            def execute(self, sql, *a):
                # The candidate query is the only statement carrying the clause. Identify it by
                # the FROM/JOIN and ORDER BY fragments -- the not-terminal count query shares the
                # FROM/JOIN but ends in GROUP BY, so it is not mistaken for the candidate.
                if "FROM execution_attempts ea JOIN runs r" in sql and "ORDER BY r.ts DESC" in sql:
                    assert clause in sql, (
                        f"deliberate break is STALE: the candidate query no longer contains "
                        f"{clause!r}, so stripping it does nothing and the corruption assertion "
                        f"below would blame the fix. Update this fixture. SQL: {sql!r}"
                    )
                    stripped.append(sql)
                    return self._c.execute(sql.replace(clause, ""), *a)
                return self._c.execute(sql, *a)

            def __getattr__(self, name):
                return getattr(self._c, name)

            def __enter__(self):
                self._c.__enter__()
                return self

            def __exit__(self, *exc):
                return self._c.__exit__(*exc)

        monkeypatch.setattr(feedback, "_conn", lambda: _Unfiltered(real_conn()))
        ledger_reconcile.resolve_unresolved_worker_attempts(apply=True)
        assert stripped, (
            "deliberate break never fired: nothing matched the candidate query's FROM/JOIN + "
            "ORDER BY signature, so no clause was stripped and the assertions below prove "
            "nothing. Update this fixture to match the current query."
        )
        broken_status, broken_model, broken_completed = _row("sweep-inflight")
        assert (broken_status, broken_model) == ("complete", "gpt-5.6-terra"), (
            "the break must reproduce the corruption: a running worker stamped with an "
            "exact resolved model"
        )
        assert broken_completed is not None, "and with a completion timestamp it never earned"
        # REVERTED by monkeypatch teardown; the filtered assertions above are the guard.
    finally:
        feedback.DB_PATH = old_db


def test_gemini_provenance_reads_the_per_run_log_before_the_conversation_store(
    tmp_path, monkeypatch
):
    """The precedence the code claimed in a comment but did not implement.

    `cli_reported_model` mapped `gemini` to `_agy_model_for` alone -- a conversation-store scrape
    joined by workspace and time window -- while the comment above `NO_SESSION_LOG_AGENTS` said the
    per-run agy log was primary and the store only a fallback. `model_label_from_agy_log` was never
    called from here at all. Commit fe59bc7 ("the run reports its own model") settles the direction:
    the per-run log wins, because the dispatcher gives each run its own `--log-file`, so that line
    belongs to exactly one run where a store match can pick up a neighbour's session.

    Ordering matters and is asserted here: resolution runs through `model_id_for_label`, which until
    the catalog fix returned a slug guess for every label. This test pins that the label resolves to
    the id agy ADVERTISES, not to the slug of the label.
    """
    import adapters

    workspace = tmp_path / "offloads" / "20260823T000000Z-issue-9-1"
    workspace.mkdir(parents=True)

    # agy's OWN catalog, verbatim `agy models` shape. Seeding the memo is isolation, not a skip:
    # `advertised_catalog` shells out to `agy models` on a cold cache, and that probe has already
    # leaked into two monkeypatched-`subprocess.run` selftests in this branch.
    catalog = (
        "Fetching available models...\n"
        "gemini-3.6-flash-high\tGemini 3.6 Flash (High)\n"
        "gemini-3.1-pro-high\tGemini 3.1 Pro (High)\n"
    )
    monkeypatch.setitem(
        adapters._ADVERTISED_MEMO,
        "gemini",
        {
            "ts": time.time(),
            "models": adapters.parse_model_catalog(catalog),
            "pairs": adapters.parse_model_catalog_pairs(catalog),
        },
    )
    # An EMPTY store, so a passing assertion can only have come from the log.
    monkeypatch.setattr(adapters, "AGY_HOME", tmp_path / "agy-empty")

    dispatch_log = tmp_path / "offload.gemini.1787500000000000000.log"
    agy_log = adapters.agy_log_for(dispatch_log)
    # ONE name, both ends: the dispatcher derives the same path when it rewrites agy's --log-file.
    assert agy_log == tmp_path / "offload.gemini.1787500000000000000.agy.log", agy_log
    assert adapters.agy_log_for(agy_log) == agy_log, "already-an-agy-log must not double-suffix"
    assert adapters.agy_log_for(None) is None
    agy_log.write_text(
        "I0823 20:08:19.863873 1 model_config_manager.go:311] Propagating selected "
        'model override to backend: label="Gemini 3.1 Pro (High)"\n'
        "I0823 20:08:41.101010 1 model_config_manager.go:311] Propagating selected "
        'model override to backend: label="Gemini 3.6 Flash (High)"\n'
    )

    got = adapters.cli_reported_model("gemini", workspace, log_file=str(dispatch_log))
    # LAST OCCURRENCE WINS -- the line is re-emitted as the session settles.
    assert got["model"] == "gemini-3.6-flash-high", got
    assert got["source"] == str(agy_log), got
    assert got["reason"] is None, got

    # THE LOG BEATS THE STORE. With a store that names a DIFFERENT model, the per-run log still
    # wins -- which is the whole claim, and the reason a store-only mapping was wrong.
    store_home = tmp_path / "agy"
    brain = store_home / "brain" / "conv-1" / ".system_generated" / "logs"
    brain.mkdir(parents=True)
    (brain / "transcript_full.jsonl").write_text(json.dumps({"model": "claude-sonnet-4-6"}) + "\n")
    index = store_home / "conversation_summaries.db"
    conn = sqlite3.connect(index)
    conn.execute(
        "CREATE TABLE conversation_summaries "
        "(conversation_id TEXT, workspace_uris TEXT, last_modified_time INTEGER)"
    )
    conn.execute(
        "INSERT INTO conversation_summaries VALUES (?, ?, ?)",
        ("conv-1", f"file://{workspace.resolve()}", 1_787_500_000),
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(adapters, "AGY_HOME", store_home)
    both = adapters.cli_reported_model("gemini", workspace, log_file=str(dispatch_log))
    assert both["model"] == "gemini-3.6-flash-high", ("the per-run log is primary", both)

    # THE STORE IS STILL THE FALLBACK, so removing the log does not lose the seat's provenance.
    no_log = adapters.cli_reported_model("gemini", workspace)
    assert no_log["model"] == "claude-sonnet-4-6", no_log
    assert no_log["source"] == "gemini-session-store", no_log

    # NEITHER PATH ANSWERS => a reason naming what was searched, distinguishable from a run that
    # never had a log at all.
    monkeypatch.setattr(adapters, "AGY_HOME", tmp_path / "agy-empty")
    agy_log.write_text("nothing a model could be read from\n")
    silent = adapters.cli_reported_model("gemini", workspace, log_file=str(dispatch_log))
    assert silent["model"] is None
    assert silent["reason"] == "no_gemini_model_in_run_log_or_session_store", silent
    assert (
        adapters.cli_reported_model("gemini", workspace)["reason"]
        == "no_gemini_session_matched_workspace"
    )

    # DELIBERATE BREAK: gemini mapped to the store scrape only, as before the fix. The per-run log
    # is then unreachable and the run resolves to the store's model -- or to nothing.
    agy_log.write_text(
        "model_config_manager.go:311] Propagating selected model override to backend: "
        'label="Gemini 3.6 Flash (High)"\n'
    )
    monkeypatch.setattr(adapters, "AGY_HOME", store_home)
    real_label_reader = adapters.model_label_from_agy_log
    monkeypatch.setattr(adapters, "model_label_from_agy_log", lambda _text: None)
    broken = adapters.cli_reported_model("gemini", workspace, log_file=str(dispatch_log))
    assert broken["model"] == "claude-sonnet-4-6", (
        "the break must restore the old behaviour: the store answers and the log is never read",
        broken,
    )
    # REVERTED, and the per-run log is primary again.
    monkeypatch.setattr(adapters, "model_label_from_agy_log", real_label_reader)
    assert (
        adapters.cli_reported_model("gemini", workspace, log_file=str(dispatch_log))["model"]
        == "gemini-3.6-flash-high"
    )


def test_ledger_isolation_regression(tmp_path, monkeypatch):
    """Regression test ensuring provenance tests write LEDGER into temp state and not to host disk."""
    host_ledger = adapters.HOME / ".codex" / "handoff" / "capacity-ledger.ndjson"
    host_existed_before = host_ledger.exists()
    host_bytes_before = host_ledger.read_bytes() if host_existed_before else None

    test_ledger = tmp_path / "isolated-capacity-ledger.ndjson"
    monkeypatch.setattr(adapters, "HANDOFF", tmp_path)
    monkeypatch.setattr(adapters, "LEDGER", test_ledger)
    monkeypatch.setenv("HANDOFF_DIR", str(tmp_path))

    adapters.record_ledger("codex", count=1, cost_usd=0.0)

    assert test_ledger.exists()
    assert test_ledger.stat().st_size > 0
    assert host_ledger.exists() is host_existed_before
    if host_existed_before:
        assert host_ledger.read_bytes() == host_bytes_before
