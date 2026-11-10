"""Tests for issue #1317: Research admission cap filters non-A/B experiment IDs and checks evaluations_v2."""

import json
import sqlite3
import time

import pytest
from src import research_subjects

import exp_abcd


@pytest.fixture
def conn():
    """Create an in-memory SQLite database with the required tables."""
    db = sqlite3.connect(":memory:")
    # Create runs table
    db.execute(
        "CREATE TABLE runs (run_id TEXT, ts REAL, agent TEXT, task_type TEXT, target TEXT, experiment_id TEXT)"
    )
    # Create evaluations table
    db.execute(
        "CREATE TABLE evaluations (experiment_id TEXT, implementer TEXT, evaluator TEXT, score REAL, ts REAL)"
    )
    # Create evaluations_v2 table
    db.execute("""CREATE TABLE evaluations_v2 (
            experiment_id TEXT,
            implementer_arm_id TEXT, implementer_member_id TEXT,
            implementer_profile_id TEXT, implementation_agent TEXT,
            evaluator_id TEXT, evaluator_arm_id TEXT, evaluator_profile_id TEXT,
            evaluator_agent TEXT,
            score REAL, rank INTEGER, verdict TEXT, ts INTEGER,
            PRIMARY KEY (experiment_id, implementer_member_id, evaluator_id)
        )""")
    yield db
    db.close()


@pytest.fixture
def exp_dir(tmp_path):
    """Create a temporary experiments directory and patch exp_abcd.EXP_DIR."""
    exp_dir = tmp_path / "experiments"
    exp_dir.mkdir()

    # Save and patch EXP_DIR
    original_exp_dir = exp_abcd.EXP_DIR
    exp_abcd.EXP_DIR = exp_dir

    yield exp_dir

    # Restore original EXP_DIR
    exp_abcd.EXP_DIR = original_exp_dir


def test_non_ab_experiment_ids_are_not_counted(conn, exp_dir):
    """Test that research-program:* experiment IDs without experiments dir are not counted.

    This is the main fix for issue #1317: non-A/B research-program IDs should not be counted
    as pending experiments because they don't have meta.json in the experiments directory.
    """
    # Create a research-program experiment ID that has runs but no experiments dir
    research_program_exp_id = "research-program:d3-unblock-sweep:2026-10-27T13"

    # Insert a run for this experiment (within the evaluable window)
    now = time.time()
    conn.execute(
        "INSERT INTO runs (run_id, ts, agent, task_type, target, experiment_id) VALUES (?, ?, ?, ?, ?, ?)",
        ("run-1", now, "codex", "implement", "Owner/Repo#1", research_program_exp_id),
    )

    # Do NOT create an experiments dir for this ID (simulating a non-A/B experiment)

    # Call unevaluated_experiment_ids
    result = research_subjects.unevaluated_experiment_ids(conn, now=now)

    # The research-program ID should NOT be in the pending IDs because it has no meta.json
    assert research_program_exp_id not in result["ids"], (
        f"Non-A/B experiment ID {research_program_exp_id} should not be counted as pending. "
        f"Pending IDs: {result['ids']}"
    )

    # Verify the counts
    assert result["total"] == 0, f"Expected 0 total pending, got {result['total']}"
    assert result["drainable"] == 0, f"Expected 0 drainable, got {result['drainable']}"
    assert result["unreadable"] == 0, f"Expected 0 unreadable, got {result['unreadable']}"


def test_non_ab_experiment_with_no_exp_dir(conn, exp_dir):
    """Test that an experiment without an experiments/<id> directory is not counted."""
    now = time.time()

    # Create a run for an experiment that has no experiments dir
    exp_id = "exp-no-dir"
    conn.execute(
        "INSERT INTO runs (run_id, ts, agent, task_type, target, experiment_id) VALUES (?, ?, ?, ?, ?, ?)",
        ("run-1", now, "codex", "implement", "Owner/Repo#1", exp_id),
    )

    # Do NOT create an experiments/<exp_id> directory

    result = research_subjects.unevaluated_experiment_ids(conn, now=now)

    # The experiment should NOT be in the pending IDs because it has no meta.json
    assert exp_id not in result["ids"], (
        f"Experiment ID {exp_id} with no experiments dir should not be counted as pending. "
        f"Pending IDs: {result['ids']}"
    )


def test_ab_experiment_with_meta_json_is_counted(conn, exp_dir):
    """Test that an A/B experiment with meta.json IS counted as pending."""
    now = time.time()

    # Create an A/B experiment
    exp_id = "exp-ab-001"
    exp_path = exp_dir / exp_id
    exp_path.mkdir()

    # Create a valid meta.json
    meta_path = exp_path / "meta.json"
    meta_path.write_text(
        json.dumps(
            {
                "experiment_id": exp_id,
                "task_type": "implement",
                "target": "Owner/Repo#1",
            }
        )
    )

    # Create a valid spec.md (required for drainable by followup)
    spec_path = exp_path / "spec.md"
    spec_path.write_text("# Test spec")

    # Insert a run for this experiment
    conn.execute(
        "INSERT INTO runs (run_id, ts, agent, task_type, target, experiment_id) VALUES (?, ?, ?, ?, ?, ?)",
        ("run-1", now, "codex", "implement", "Owner/Repo#1", exp_id),
    )

    result = research_subjects.unevaluated_experiment_ids(conn, now=now)

    # The experiment should be in the pending IDs
    assert exp_id in result["ids"], (
        f"A/B experiment ID {exp_id} with valid meta.json should be counted as pending. "
        f"Pending IDs: {result['ids']}"
    )
    assert result["total"] == 1, f"Expected 1 total pending, got {result['total']}"
    assert result["drainable"] == 1, f"Expected 1 drainable, got {result['drainable']}"
    assert result["unreadable"] == 0, f"Expected 0 unreadable, got {result['unreadable']}"


def test_evaluations_v2_row_clears_pending(conn, exp_dir):
    """Test that an experiment with a row in evaluations_v2 is NOT considered pending.

    This is part of the fix for issue #1317: the function should check both evaluations
    and evaluations_v2 tables.
    """
    now = time.time()

    # Create an A/B experiment
    exp_id = "exp-evaluated-v2"
    exp_path = exp_dir / exp_id
    exp_path.mkdir()

    # Create a valid meta.json
    meta_path = exp_path / "meta.json"
    meta_path.write_text(
        json.dumps(
            {
                "experiment_id": exp_id,
                "task_type": "implement",
                "target": "Owner/Repo#1",
            }
        )
    )

    # Insert a run for this experiment
    conn.execute(
        "INSERT INTO runs (run_id, ts, agent, task_type, target, experiment_id) VALUES (?, ?, ?, ?, ?, ?)",
        ("run-1", now, "codex", "implement", "Owner/Repo#1", exp_id),
    )

    # Insert a row in evaluations_v2 (not evaluations)
    conn.execute(
        """INSERT INTO evaluations_v2 (
            experiment_id, implementer_arm_id, implementer_member_id,
            implementer_profile_id, implementation_agent,
            evaluator_id, evaluator_arm_id, evaluator_profile_id,
            evaluator_agent, score, rank, verdict, ts
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            exp_id,
            "arm-1",
            "member-1",
            "profile-1",
            "codex",
            "eval-1",
            "arm-2",
            "profile-2",
            "cursor",
            7.0,
            1,
            "PASS",
            int(now),
        ),
    )

    result = research_subjects.unevaluated_experiment_ids(conn, now=now)

    # The experiment should NOT be in the pending IDs because it has an evaluations_v2 row
    assert exp_id not in result["ids"], (
        f"Experiment ID {exp_id} with evaluations_v2 row should not be pending. "
        f"Pending IDs: {result['ids']}"
    )
    assert result["total"] == 0, f"Expected 0 total pending, got {result['total']}"


def test_evaluations_table_row_clears_pending(conn, exp_dir):
    """Test that an experiment with a row in evaluations (legacy) is NOT considered pending."""
    now = time.time()

    # Create an A/B experiment
    exp_id = "exp-evaluated-v1"
    exp_path = exp_dir / exp_id
    exp_path.mkdir()

    # Create a valid meta.json
    meta_path = exp_path / "meta.json"
    meta_path.write_text(
        json.dumps(
            {
                "experiment_id": exp_id,
                "task_type": "implement",
                "target": "Owner/Repo#1",
            }
        )
    )

    # Insert a run for this experiment
    conn.execute(
        "INSERT INTO runs (run_id, ts, agent, task_type, target, experiment_id) VALUES (?, ?, ?, ?, ?, ?)",
        ("run-1", now, "codex", "implement", "Owner/Repo#1", exp_id),
    )

    # Insert a row in evaluations (legacy table)
    conn.execute(
        "INSERT INTO evaluations (experiment_id, implementer, evaluator, score, ts) VALUES (?, ?, ?, ?, ?)",
        (exp_id, "codex", "cursor", 7.0, now),
    )

    result = research_subjects.unevaluated_experiment_ids(conn, now=now)

    # The experiment should NOT be in the pending IDs because it has an evaluations row
    assert exp_id not in result["ids"], (
        f"Experiment ID {exp_id} with evaluations row should not be pending. "
        f"Pending IDs: {result['ids']}"
    )


def test_unreadable_meta_is_counted_and_reported(conn, exp_dir):
    """Test that an experiment with an unreadable meta.json is counted as pending and reported.

    This is part of the fix for issue #1317: unreadable meta.json files should be counted
    as pending but tracked separately in the unreadable tally.
    """
    now = time.time()

    # Create an A/B experiment
    exp_id = "exp-unreadable-meta"
    exp_path = exp_dir / exp_id
    exp_path.mkdir()

    # Create an INVALID meta.json (unreadable JSON)
    meta_path = exp_path / "meta.json"
    meta_path.write_text("{ invalid json }")  # This is not valid JSON

    # Insert a run for this experiment
    conn.execute(
        "INSERT INTO runs (run_id, ts, agent, task_type, target, experiment_id) VALUES (?, ?, ?, ?, ?, ?)",
        ("run-1", now, "codex", "implement", "Owner/Repo#1", exp_id),
    )

    result = research_subjects.unevaluated_experiment_ids(conn, now=now)

    # The experiment should be in the pending IDs (because it has a meta.json file, even if unreadable)
    assert exp_id in result["ids"], (
        f"Experiment ID {exp_id} with unreadable meta.json should still be counted as pending. "
        f"Pending IDs: {result['ids']}"
    )

    # But it should be tracked as unreadable
    assert result["unreadable"] == 1, (
        f"Expected 1 unreadable, got {result['unreadable']}. " f"Result: {result}"
    )

    # And it should NOT be drainable
    assert result["drainable"] == 0, (
        f"Expected 0 drainable (since meta is unreadable), got {result['drainable']}. "
        f"Result: {result}"
    )

    assert result["total"] == 1, f"Expected 1 total pending, got {result['total']}"


def test_mixed_experiments(conn, exp_dir):
    """Test a mix of different experiment states."""
    now = time.time()

    # 1. Research-program experiment (no exp dir) - should NOT be counted
    research_exp_id = "research-program:test"
    conn.execute(
        "INSERT INTO runs (run_id, ts, agent, task_type, target, experiment_id) VALUES (?, ?, ?, ?, ?, ?)",
        ("run-research", now, "codex", "implement", "Owner/Repo#1", research_exp_id),
    )

    # 2. A/B experiment with valid meta.json - should be counted as drainable
    ab_exp_id = "exp-ab-valid"
    exp_path_ab = exp_dir / ab_exp_id
    exp_path_ab.mkdir()
    meta_path_ab = exp_path_ab / "meta.json"
    meta_path_ab.write_text(json.dumps({"experiment_id": ab_exp_id}))
    # Create spec.md (required for drainable by followup)
    spec_path_ab = exp_path_ab / "spec.md"
    spec_path_ab.write_text("# Test spec")
    conn.execute(
        "INSERT INTO runs (run_id, ts, agent, task_type, target, experiment_id) VALUES (?, ?, ?, ?, ?, ?)",
        ("run-ab", now, "codex", "implement", "Owner/Repo#1", ab_exp_id),
    )

    # 3. A/B experiment with unreadable meta.json - should be counted as unreadable
    unreadable_exp_id = "exp-ab-unreadable"
    exp_path_unreadable = exp_dir / unreadable_exp_id
    exp_path_unreadable.mkdir()
    meta_path_unreadable = exp_path_unreadable / "meta.json"
    meta_path_unreadable.write_text("not valid json")
    conn.execute(
        "INSERT INTO runs (run_id, ts, agent, task_type, target, experiment_id) VALUES (?, ?, ?, ?, ?, ?)",
        ("run-unreadable", now, "codex", "implement", "Owner/Repo#1", unreadable_exp_id),
    )

    # 4. A/B experiment with evaluations_v2 row - should NOT be counted
    evaluated_exp_id = "exp-ab-evaluated"
    exp_path_evaluated = exp_dir / evaluated_exp_id
    exp_path_evaluated.mkdir()
    meta_path_evaluated = exp_path_evaluated / "meta.json"
    meta_path_evaluated.write_text(json.dumps({"experiment_id": evaluated_exp_id}))
    conn.execute(
        "INSERT INTO runs (run_id, ts, agent, task_type, target, experiment_id) VALUES (?, ?, ?, ?, ?, ?)",
        ("run-evaluated", now, "codex", "implement", "Owner/Repo#1", evaluated_exp_id),
    )
    conn.execute(
        """INSERT INTO evaluations_v2 (
            experiment_id, implementer_arm_id, implementer_member_id,
            implementer_profile_id, implementation_agent,
            evaluator_id, evaluator_arm_id, evaluator_profile_id,
            evaluator_agent, score, rank, verdict, ts
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            evaluated_exp_id,
            "arm-1",
            "member-1",
            "profile-1",
            "codex",
            "eval-1",
            "arm-2",
            "profile-2",
            "cursor",
            7.0,
            1,
            "PASS",
            int(now),
        ),
    )

    result = research_subjects.unevaluated_experiment_ids(conn, now=now)

    # Check the results
    assert research_exp_id not in result["ids"], "Research-program ID should not be counted"
    assert ab_exp_id in result["ids"], "A/B with valid meta should be counted"
    assert unreadable_exp_id in result["ids"], "A/B with unreadable meta should be counted"
    assert evaluated_exp_id not in result["ids"], "Evaluated experiment should not be counted"

    assert result["total"] == 2, f"Expected 2 total pending, got {result['total']}"
    assert result["drainable"] == 1, f"Expected 1 drainable, got {result['drainable']}"
    assert result["unreadable"] == 1, f"Expected 1 unreadable, got {result['unreadable']}"


# Deliberate break test - this is for the deliberate-break -> revert test
# The test will be modified by the test runner to break and then revert
def test_deliberate_break_meta_json_restriction(conn, exp_dir):
    """Deliberate break: drop the meta.json restriction to verify the test catches it.

    This test is designed to FAIL when the meta.json restriction is removed.
    It will be used for the deliberate-break -> revert acceptance criterion.
    """
    now = time.time()

    # Create a research-program experiment without an experiments dir
    research_exp_id = "research-program:deliberate-break"
    conn.execute(
        "INSERT INTO runs (run_id, ts, agent, task_type, target, experiment_id) VALUES (?, ?, ?, ?, ?, ?)",
        ("run-1", now, "codex", "implement", "Owner/Repo#1", research_exp_id),
    )

    result = research_subjects.unevaluated_experiment_ids(conn, now=now)

    # This test SHOULD FAIL if the meta.json restriction is removed
    # (i.e., if research-program IDs without exp dir are counted)
    # The fix ensures they are NOT counted
    assert research_exp_id not in result["ids"], (
        "DELIBERATE BREAK CHECK: research-program ID should NOT be counted. "
        f"If this fails, the meta.json restriction has been removed. "
        f"Pending IDs: {result['ids']}"
    )
