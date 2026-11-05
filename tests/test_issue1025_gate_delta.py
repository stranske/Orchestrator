"""Cover Gate-missing statements on the #1025 ranked targets.

The baseline is main's combined coverage (run 41942498552, branch_coverage false).
Each test below executes statements that payload still lists as missing. Selftest
bodies and the two migration row-loss guards stay out: the first only run inside
``--selftest``, and the guards refuse a copy that dropped rows, which a single
connection cannot provoke without rewriting the migration.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

import capability_advisor
import capability_propensity as propensity
import capability_recurrence_check as recurrence
import capabilities
import feedback


@pytest.fixture(autouse=True)
def isolated_runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ORCH_LOCAL_RUNTIME", str(tmp_path / "runtime"))
    monkeypatch.delenv("ORCH_CAPABILITY_HEARTBEATS", raising=False)
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "feedback.db")
    monkeypatch.setattr(feedback, "_capability_daily_heartbeat", lambda *_a, **_k: None)
    monkeypatch.setattr(propensity, "_capability_heartbeat", lambda *_a, **_k: None)


def _raises(message: str):
    def _boom(*_a, **_k):
        raise ValueError(message)

    return _boom


def test_widen_agent_switches_commits_its_own_transaction() -> None:
    """A committed connection is not already in a transaction, so the rebuild commits."""
    c = sqlite3.connect(":memory:")
    c.execute(
        "CREATE TABLE agent_switches (pr_ref TEXT, from_agent TEXT, to_agent TEXT, "
        "switched_ts INTEGER, auto_label INTEGER, commits_before INTEGER, commits_after INTEGER, "
        "merged INTEGER, durability TEXT, recorded_ts INTEGER, "
        "PRIMARY KEY (pr_ref, switched_ts))"
    )
    c.execute("INSERT INTO agent_switches VALUES ('o/r#1','a','b',1,0,1,1,1,'durable',2)")
    c.commit()
    assert not c.in_transaction

    feedback._widen_agent_switches(c)

    assert not c.in_transaction
    assert c.execute("SELECT source FROM agent_switches").fetchone()[0] == "label"


def test_widen_agent_switches_rolls_back_a_failed_rebuild() -> None:
    """A legacy table missing a copied column aborts the rebuild and rolls it back."""
    c = sqlite3.connect(":memory:")
    c.execute(
        "CREATE TABLE agent_switches (pr_ref TEXT, from_agent TEXT, to_agent TEXT, "
        "switched_ts INTEGER, auto_label INTEGER, recorded_ts INTEGER)"
    )
    c.execute("INSERT INTO agent_switches VALUES ('o/r#1','a','b',1,0,2)")
    c.commit()

    with pytest.raises(sqlite3.OperationalError):
        feedback._widen_agent_switches(c)

    assert not c.in_transaction
    assert "source" not in {row[1] for row in c.execute("PRAGMA table_info(agent_switches)")}


def test_retry_sequence_bounds_and_rejects_a_non_object() -> None:
    """Too many retries are truncated, and a non-object row is rejected rather than stored."""
    many = [{"attempt_ordinal": i, "operation_role": "worker"} for i in range(40)]
    clean, status, redaction_count = feedback._sanitize_completion_payload({"retry_sequence": many})
    assert status == "redacted"
    assert redaction_count >= 1
    assert len(clean["retry_sequence"]) == feedback.MAX_COMPLETION_LIST_ITEMS
    assert "retry_sequence:bounded" in clean["redacted_fields"]

    rejected, bad_status, _count = feedback._sanitize_completion_payload(
        {"retry_sequence": ["not-a-row"]}
    )
    assert bad_status == "rejected"
    assert "invalid_object:retry_sequence[0]" in rejected["rejection_codes"]


def test_closer_stage_rewards_skip_a_repeat_and_a_broken_payload() -> None:
    """A repeated run and a foreign producer are skipped; unreadable JSON is not a zero."""
    c = sqlite3.connect(":memory:")
    c.executescript(feedback.SCHEMA)
    feedback._migrate_schema(c)
    sha = "ab" * 20
    digest = "cd" * 32
    stage = {
        "accepted": True,
        "independent_validation": True,
        "validator": "closer_disposition.integrate_ordinary",
        "task_category": "closer_repair",
        "agent": "codex",
        "requested_profile_id": "profile-1",
        "target": "Owner/Repo#1",
        "head_sha": sha,
        "basis_digest": digest,
    }
    meta = json.dumps({"selected_profile_id": "profile-1", "subject_id": "owner/repo#1"})
    for run_id in ("stage-ok", "stage-foreign", "stage-broken"):
        c.execute(
            "INSERT INTO runs (run_id, ts, target, task_type, agent, routing_metadata) "
            "VALUES (?,?,?,?,?,?)",
            (run_id, 1, "owner/repo#1", "closer_repair", "codex", meta),
        )

    def _event(event_id: str, run_id: str, producer: str, payload: str) -> None:
        c.execute(
            "INSERT INTO completion_events (event_id, schema_version, run_id, event_type, phase, "
            "producer, status, validation_status, payload_json, content_hash, created_ts, updated_ts) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                event_id,
                1,
                run_id,
                "closer_stage",
                "verification",
                producer,
                "pass",
                "accepted",
                payload,
                "hash",
                1,
                1,
            ),
        )

    raw = json.dumps({"closer_stage": stage})
    _event("e-ok", "stage-ok", "closer_disposition.integrate_ordinary", raw)
    _event("e-repeat", "stage-ok", "closer_disposition.integrate_ordinary", raw)
    _event("e-foreign", "stage-foreign", "other-producer", raw)
    _event("e-broken", "stage-broken", "closer_disposition.integrate_ordinary", "{")

    assert feedback._closer_stage_rewards(c, "closer_repair") == {"stage-ok": 1.0}


def test_inherited_capability_already_declared_is_not_recorded_twice() -> None:
    """A capability the acting run already named is not copied again off the role run."""
    feedback.record_run(
        "role-1",
        "owner/repo#1",
        "triage",
        "codex",
        role_name="triage",
        capability_ids=["role-triage"],
        capability_version_ids=["ver-a"],
    )
    feedback.record_run(
        "act-1",
        "owner/repo#1",
        "implement",
        "codex",
        capability_ids=["role-triage"],
        capability_version_ids=["ver-a"],
        influenced_by_role_run_ids=["role-1"],
    )

    with feedback._conn() as c:
        role_edges = c.execute(
            "SELECT COUNT(*) FROM influence_edges WHERE target_run_id='role-1' "
            "AND influence_type='capability' AND capability_id='role-triage'"
        ).fetchone()[0]
        copied = c.execute(
            "SELECT COUNT(*) FROM influence_edges WHERE target_run_id='act-1' "
            "AND influence_type='capability' AND capability_id='role-triage' "
            "AND source_run_id='role-1'"
        ).fetchone()[0]
    assert role_edges == 1
    assert copied == 0


def test_completion_episode_derives_a_subject_from_the_arm_alone() -> None:
    """No subject row and no arm list still derives identity from the single arm id."""
    feedback.record_run(
        "ep-arm",
        "owner/repo#9",
        "implement",
        "codex",
        routing_metadata={
            "spec_hash": "ab" * 32,
            "base_sha": "cd" * 20,
            "arm_id": "arm-1",
        },
    )
    feedback.record_completion_event("ep-arm", phase="execution", producer="pytest", status="pass")

    rows = feedback.completion_event_episodes(limit=5)
    match = [row for row in rows if row["identity"]["run_id"] == "ep-arm"]
    assert match
    assert match[0]["identity"]["subject_id"]
    assert match[0]["identity"]["subject_arms"] == ["arm-1"]


def test_completion_episode_drops_an_unreadable_payload() -> None:
    """A payload that is not JSON becomes an empty object, and the episode is still returned."""
    feedback.record_run("ep-bad", "owner/repo#9", "implement", "codex")
    feedback.record_completion_event("ep-bad", phase="execution", producer="pytest", status="pass")
    with feedback._conn() as c:
        c.execute(
            "UPDATE completion_events SET payload_json=? WHERE run_id=?",
            ("{", "ep-bad"),
        )

    rows = feedback.completion_event_episodes(limit=5)
    match = [row for row in rows if row["identity"]["run_id"] == "ep-bad"]
    assert match


def test_muse_cost_rejects_unordered_bounds_and_a_short_exit_list() -> None:
    with pytest.raises(ValueError, match="observed bounds must be ordered"):
        feedback.record_muse_subscription_cost(
            "muse-1",
            invocation_count=1,
            window_start_ts=1,
            window_end_ts=2,
            observed_start_ts=5,
            observed_end_ts=4,
            wall_seconds=1.0,
            raw_exit_codes=[0],
        )
    with pytest.raises(ValueError, match="one-for-one"):
        feedback.record_muse_subscription_cost(
            "muse-1",
            invocation_count=2,
            window_start_ts=1,
            window_end_ts=2,
            observed_start_ts=1,
            observed_end_ts=2,
            wall_seconds=1.0,
            raw_exit_codes=[0],
        )


def test_muse_cost_requires_a_muse_offload_run() -> None:
    feedback.record_run("not-muse", "owner/repo#1", "implement", "codex")
    with pytest.raises(ValueError, match="muse offload"):
        feedback.record_muse_subscription_cost(
            "not-muse",
            invocation_count=1,
            window_start_ts=1,
            window_end_ts=2,
            observed_start_ts=1,
            observed_end_ts=2,
            wall_seconds=1.0,
            raw_exit_codes=[0],
        )


def test_bad_attempt_ordinal_is_dropped_rather_than_stored() -> None:
    feedback.record_run("ord-1", "owner/repo#1", "implement", "codex")
    feedback.record_execution_trace(
        "ord-1",
        operation="implement",
        operation_role="worker",
        resolved_model="gpt-5.6-codex",
        attempt_ordinal="not-a-number",
    )
    with feedback._conn() as c:
        feedback._record_execution_attempt_in_conn(
            c,
            run_id="ord-1",
            attempt_id="attempt:bad-ordinal",
            attempt_ordinal="also-not",
            operation_role="worker",
            resolved_model="gpt-5.6-codex",
        )
        ordinal = c.execute(
            "SELECT attempt_ordinal FROM execution_attempts WHERE attempt_id=?",
            ("attempt:bad-ordinal",),
        ).fetchone()[0]
    assert ordinal == 1


def test_unresolved_profile_attempt_names_each_refusal() -> None:
    feedback.record_run("prof-1", "owner/repo#1", "implement", "codex")
    with pytest.raises(ValueError, match="missing selected profile"):
        feedback.complete_profile_attempt_unresolved("prof-1", selected_profile_id="profile-a")

    with feedback._conn() as c:
        feedback._record_execution_attempt_in_conn(
            c,
            run_id="prof-1",
            attempt_id="attempt:profile:prof-1",
            operation_role="worker",
            profile_id="profile-a",
            resolved_model="gpt-5.6-codex",
            status="running",
        )
    with pytest.raises(ValueError, match="selected profile changed"):
        feedback.complete_profile_attempt_unresolved("prof-1", selected_profile_id="profile-b")

    assert (
        feedback.complete_profile_attempt_unresolved("prof-1", selected_profile_id="profile-a")
        == "attempt:profile:prof-1"
    )


def test_relearn_quality_imputes_agent_mean_and_global_median(monkeypatch) -> None:
    """One measured cell supplies the agent's other task, and a silent agent uses the median."""
    import research_subjects

    monkeypatch.setattr(feedback, "model_epoch_floor", lambda *_a, **_k: 0)

    def _weights(**_k):
        raise RuntimeError("subject weights unavailable")

    monkeypatch.setattr(research_subjects, "effective_evidence_weights", _weights)
    now = int(__import__("time").time())
    feedback.record_run("m-impl", "o/r#1", "implement", "measured", ts=now)
    feedback.record_outcome("m-impl", adjudicated_verdict="PASS", merged=True, durability="durable")
    feedback.record_run("m-rev", "o/r#2", "review", "measured", ts=now)
    feedback.record_outcome("m-rev", adjudicated_verdict="PASS", merged=True, durability="durable")
    feedback.record_run("s-tri", "o/r#3", "triage", "silent", ts=now)
    feedback.record_outcome("s-tri", adjudicated_verdict="FAIL", durability="reverted")
    with feedback._conn() as c:
        c.execute(
            "INSERT INTO costs (run_id,tokens_in,tokens_out,cost_usd,latency_s,source,pulled_ts) "
            "VALUES (?,?,?,?,?,?,?)",
            ("m-impl", 3000, 1000, 1.5, 12.0, "ccusage", now),
        )

    version = feedback.relearn_quality(
        {"implement": {"measured": 0.5}, "review": {"measured": 0.5}, "triage": {"silent": 0.5}},
        window_days=30,
    )

    with feedback._conn() as c:
        notes = {
            (task, agent): note
            for task, agent, note in c.execute(
                "SELECT task_type, agent, rationale FROM route_weights WHERE version=?",
                (version,),
            )
        }
    assert "effort_src=a" in notes[("review", "measured")]
    assert "effort_src=g" in notes[("triage", "silent")]


def test_propensity_formatter_lists_repair_proposals_and_the_overflow() -> None:
    proposals = [
        {
            "capability_id": f"cap-{i:02d}",
            "worth_having": i == 0,
            "defect_evidence_since_repair": 2,
            "defect_evidence_total": 3,
            "repairs_recorded": i,
            "evidence": ["evidence line that a reader can check"],
        }
        for i in range(11)
    ]
    text = propensity._fmt(
        {
            "capabilities_with_outcome_link": None,
            "capability_count": 2,
            "window_days": 90,
            "experiment_count": 1,
            "resolved_experiment_count": 0,
            "fixture_experiment_count": 0,
            "fixture_verdict_count": 0,
            "capabilities_with_evidence": 1,
            "verdict_count": 1,
            "verdicts_by_provenance": "",
            "verdicts_outcome_derived": 0,
            "verdicts_self_reported": 0,
            "verdicts_self_reported_share": None,
            "capabilities_with_outcome_derived_evidence": 0,
            "capabilities_with_multiple_judge_arms": 0,
            "repair_proposal_count": 11,
            "repair_proposals_worth_having": 1,
            "repairs_recorded": 1,
            "find_count": 0,
            "finds_by_finder_kind": "",
            "decline_count": 0,
            "capabilities_declined_with_reason": 0,
            "decline_demotable_count": 0,
            "declines_by_kind": "",
            "ranked": [
                {
                    "capability_id": "feedback-store",
                    "propensity": 0.5,
                    "candidates": 1,
                    "triggered": 1,
                    "useful": 1,
                    "not_useful": 0,
                    "declined": 0,
                    "floored": False,
                }
            ],
            "repair_proposals": proposals,
        }
    )
    assert "REPAIR PROPOSALS" in text
    assert "WORTH HAVING" in text
    assert "no useful verdict yet" in text
    assert "and 1 more" in text
    assert "Brain unreadable" in text


def test_dry_run_is_rejected_outside_tick_evidence() -> None:
    with pytest.raises(SystemExit) as caught:
        propensity.main(["report", "--dry-run"])
    assert caught.value.code == 2


def test_cli_error_paths_exit_before_writing(monkeypatch, capsys) -> None:
    monkeypatch.setattr(propensity, "record_find", _raises("defect required"))
    with pytest.raises(SystemExit) as caught:
        propensity.main(["find", "--defect", "x", "--artifact", "file:1"])
    assert caught.value.code == 2
    assert "defect required" in capsys.readouterr().err

    with pytest.raises(SystemExit) as missing_cap:
        propensity.main(["record-repair"])
    assert missing_cap.value.code == 2

    monkeypatch.setattr(propensity, "record_repair", _raises("fix required"))
    with pytest.raises(SystemExit) as repair_error:
        propensity.main(["record-repair", "--capability", "feedback-store", "--fix", ""])
    assert repair_error.value.code == 2

    with pytest.raises(SystemExit) as surface:
        propensity.main(["binding-quality"])
    assert surface.value.code == 2

    with pytest.raises(SystemExit) as reoffer:
        propensity.main(["reoffer"])
    assert reoffer.value.code == 2

    with pytest.raises(SystemExit) as consult:
        propensity.main(["consult-outcome"])
    assert consult.value.code == 2

    with pytest.raises(SystemExit) as late:
        propensity.main(
            ["late-outcome", "--capability", "feedback-store", "--experiment", "advice:abc"]
        )
    assert late.value.code == 2
    assert "--direction is required" in capsys.readouterr().err

    with pytest.raises(SystemExit) as pair:
        propensity.main(["trigger"])
    assert pair.value.code == 2

    with pytest.raises(SystemExit) as evidence:
        propensity.main(["useful", "--capability", "feedback-store", "--experiment", "advice:abc"])
    assert evidence.value.code == 2
    assert "--evidence is required" in capsys.readouterr().err

    monkeypatch.setattr(propensity, "record_trigger", _raises("bad experiment"))
    with pytest.raises(SystemExit) as trigger:
        propensity.main(["trigger", "--capability", "feedback-store", "--experiment", "advice:abc"])
    assert trigger.value.code == 2


def test_cli_text_commands_render_repairs_bindings_and_detection(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        propensity,
        "propose_repair",
        lambda **_k: [
            {
                "capability_id": "feedback-store",
                "worth_having": False,
                "basis": ["defect"],
                "defect_evidence_since_repair": 2,
                "defect_evidence_total": 3,
                "repairs_recorded": 1,
                "evidence": ["a checked evidence line"],
                "implied_fixes": ["restore the refused row"],
            }
        ],
    )
    assert propensity.main(["repair"]) == 0
    repair = capsys.readouterr().out
    assert "no useful verdict yet" in repair
    assert "fix: restore the refused row" in repair

    monkeypatch.setattr(propensity, "record_repair", lambda *_a, **_k: True)
    assert propensity.main(["record-repair", "--capability", "feedback-store", "--fix", "x"]) == 0
    assert '"recorded": true' in capsys.readouterr().out

    monkeypatch.setattr(
        propensity,
        "binding_quality",
        lambda *_a, **_k: {
            "surface": "repo-audit",
            "window_days": 90,
            "bound": ["feedback-store"],
            "offers": 1,
            "triggers": 0,
            "declines": 2,
            "finds": 1,
            "finds_by_finder_kind": {"surface": 1},
            "find_subjects": ["feedback.py"],
        },
    )
    assert propensity.main(["binding-quality", "--surface", "repo-audit"]) == 0
    assert "binding quality — repo-audit" in capsys.readouterr().out

    monkeypatch.setattr(
        propensity,
        "detect",
        lambda **_k: {
            "surfaces": {
                "repo-audit": {"records": 2, "bound": ["a"], "finds": 1, "fact_missing": 1}
            },
            "promotions": [
                {"surface": "repo-audit", "capability_id": "feedback-store", "reason": "hand work"}
            ],
            "demotions": [
                {"surface": "repo-audit", "capability_id": "old-cap", "reason": "never used"}
            ],
            "not_selected_by_design": {
                "count": 1,
                "withheld": 2,
                "capabilities": {
                    "observer": {
                        "reason": "observer",
                        "surfaces": ["tick"],
                        "promotions_withheld": 1,
                        "demotions_withheld": 1,
                    }
                },
            },
            "applied": ["feedback-store@repo-audit"],
        },
    )
    assert propensity.main(["detect"]) == 0
    detected = capsys.readouterr().out
    assert "PROMOTIONS proposed" in detected
    assert "DEMOTIONS proposed" in detected
    assert "NOT SELECTED BY DESIGN" in detected
    assert "APPLIED" in detected


def test_cli_recording_commands_report_refusals_and_the_verdict_remedy(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        propensity,
        "record_reoffer",
        _raises("kind is not reofferable"),
    )
    with pytest.raises(SystemExit) as reoffer:
        propensity.main(
            [
                "reoffer",
                "--capability",
                "feedback-store",
                "--experiment",
                "advice:abc",
                "--kind",
                "wrong_match",
            ]
        )
    assert reoffer.value.code == 2

    monkeypatch.setattr(propensity, "record_reoffer", lambda *_a, **_k: {"reoffered": False})
    assert (
        propensity.main(
            [
                "reoffer",
                "--capability",
                "feedback-store",
                "--experiment",
                "advice:abc",
                "--kind",
                "wrong_match",
            ]
        )
        == 3
    )

    monkeypatch.setattr(propensity, "record_consult_outcome", lambda *_a, **_k: {"recorded": False})
    assert (
        propensity.main(
            [
                "consult-outcome",
                "--experiment",
                "advice:abc",
                "--direction",
                "failed",
                "--evidence",
                "the consult missed",
            ]
        )
        == 3
    )

    monkeypatch.setattr(
        propensity,
        "record_late_outcome",
        lambda *_a, **_k: {"attached": False},
    )
    assert (
        propensity.main(
            [
                "late-outcome",
                "--capability",
                "feedback-store",
                "--experiment",
                "advice:abc",
                "--direction",
                "refutes",
            ]
        )
        == 3
    )
    assert '"attached": false' in capsys.readouterr().out

    monkeypatch.setattr(propensity, "record_usefulness", lambda *_a, **_k: False)
    assert (
        propensity.main(
            [
                "useful",
                "--capability",
                "feedback-store",
                "--experiment",
                "advice:abc",
                "--evidence",
                "the diff landed",
                "--deliverable",
                "owner/repo#9",
                "--provenance",
                "self_reported",
            ]
        )
        == 0
    )
    body = json.loads(capsys.readouterr().out)
    assert body["deliverable"] == "owner/repo#9"
    assert "late-outcome" in body["remedy"]


def test_declared_facts_and_offer_report_survive_a_missing_advisor(monkeypatch) -> None:
    monkeypatch.setitem(__import__("sys").modules, "capability_advisor", None)
    assert propensity.declared_facts("feedback-store", switches={"held_off": []}) == {}
    report = propensity.propose_offer_improvements()
    assert report["measured"] is False


def test_readers_use_the_trial_the_assembler_returns(monkeypatch) -> None:
    monkeypatch.setattr(
        propensity,
        "experiments",
        lambda **_k: [
            {
                "experiment_id": "advice:other",
                "late_outcomes": {},
                "late_outcome_orphans": {},
                "decline_reasons": {},
            },
            {
                "experiment_id": "advice:abc",
                "late_outcomes": {},
                "late_outcome_orphans": {
                    "feedback-store": {"deliverable": "batch", "direction": "refutes"}
                },
                "decline_reasons": {"feedback-store": "wrong tool"},
            },
        ],
    )
    assert propensity.existing_late_outcome("feedback-store", "advice:missing") is None
    found = propensity.existing_late_outcome("feedback-store", "advice:abc", deliverable="batch")
    assert found["direction"] == "refutes"
    assert propensity.decline_reason_recorded("feedback-store", "advice:abc") == "wrong tool"


def test_surface_records_skip_an_unreadable_file_and_split_the_rest(
    monkeypatch, tmp_path: Path
) -> None:
    root = tmp_path / "rounds"
    root.mkdir()
    (root / "a.md").write_text("round one\n\n---\nround two\n", encoding="utf-8")
    (root / "b.md").mkdir()
    monkeypatch.setenv("ORCH_RECORDS_REPO_AUDIT", str(root / "*.md"))

    rounds = propensity.surface_records("repo-audit")
    assert any("round one" in round_ for round_ in rounds)
    assert any("round two" in round_ for round_ in rounds)


def test_under_use_treats_a_matcher_failure_as_unknown_and_counts_real_rows(monkeypatch) -> None:
    import capability_matcher_proposals as proposals

    def _boom(**_k):
        raise RuntimeError("matcher down")

    monkeypatch.setattr(proposals, "evaluate", _boom)
    assert propensity._under_use(path=Path("/tmp/unused-ledger")) == {}

    monkeypatch.setattr(
        proposals,
        "evaluate",
        lambda **_k: {
            "rows": [
                {
                    "should_have_been_used": True,
                    "capability_id": "feedback-store",
                    "historical_matches": 4,
                },
                {"should_have_been_used": False, "capability_id": "other"},
            ]
        },
    )
    assert propensity._under_use(path=Path("/tmp/unused-ledger")) == {"feedback-store": 4}


def test_propose_bindings_mentions_brain_under_use(monkeypatch) -> None:
    monkeypatch.setattr(
        propensity,
        "missed_selection",
        lambda *_a, **_k: {
            "rows": [
                {
                    "capability_id": "feedback-store",
                    "already_bound": False,
                    "hand_work": 3,
                    "useful_here": 0,
                    "under_use_runs": 6,
                    "surface": "repo-audit",
                }
            ]
        },
    )
    rows = propensity.propose_bindings("repo-audit", ["a record"])
    assert rows and "6 runs" in rows[0]["reason"]


def test_record_promotion_requires_a_reason_and_otherwise_writes_the_ledger(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="must record why"):
        propensity.record_promotion("feedback-store", "repo-audit", "  ")
    ledger = tmp_path / "capabilities.json"
    capabilities.save({"feedback-store": capabilities._blank_capability("feedback-store")}, ledger)
    assert propensity.record_promotion(
        "feedback-store", "repo-audit", "the surface did this by hand", path=ledger
    )


def test_detect_apply_skips_a_full_surface_then_records(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(propensity, "SURFACE_RECORD_GLOBS", {})
    monkeypatch.setattr(capability_advisor, "SURFACE_BINDINGS", {"only-surface": []})
    monkeypatch.setattr(propensity, "observed_surfaces", lambda **_k: set())
    monkeypatch.setattr(propensity, "finds", lambda **_k: [])
    monkeypatch.setattr(propensity, "_ledger_rows_for_detect", lambda _path: {})
    monkeypatch.setattr(propensity, "surface_records", lambda _surface: ["one record"])
    monkeypatch.setattr(propensity, "propose_demotions", lambda *_a, **_k: [])
    monkeypatch.setattr(
        propensity,
        "surface_decline_counts",
        lambda *_a, **_k: {"declined": {}, "declined_demotable": {}, "declines_by_kind": {}},
    )
    monkeypatch.setattr(propensity, "surface_fact_missing_total", lambda *_a, **_k: 0)
    monkeypatch.setattr(
        propensity,
        "propose_bindings",
        lambda surface, _recs, path=None: [
            {"capability_id": "cap-full", "surface": surface, "reason": "first"},
            {"capability_id": "cap-open", "surface": surface, "reason": "second"},
        ],
    )
    seen = {"n": 0}

    def _binding_for(_surface, path=None):
        seen["n"] += 1
        return ["x"] * 10 if seen["n"] == 2 else []

    monkeypatch.setattr(capability_advisor, "binding_for", _binding_for)
    monkeypatch.setattr(propensity, "record_promotion", lambda *_a, **_k: True)

    rep = propensity.detect(path=tmp_path / "ledger.json", apply_promotions=True)
    assert rep["promotions"][0]["skipped"].startswith("surface already")
    assert rep["applied"] == [{"capability_id": "cap-open", "surface": "only-surface"}]


def test_trailing_window_read_failure_is_unknown(monkeypatch) -> None:
    import value_chain_monitor as monitor

    def _boom(*_a, **_k):
        raise OSError("brain unreadable")

    monkeypatch.setattr(monitor, "acted_on_index", _boom)
    assert capabilities.acting_in_trailing_window("feedback-store") is None


def test_tick_env_names_an_incomplete_preview_and_a_timeout(monkeypatch, tmp_path: Path) -> None:
    script = tmp_path / "orchestrate.sh"
    script.write_text(
        "_gh_gate() {\n  true\n}\n# ORCH-ANCHOR: exploration-window-start\necho preview\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(recurrence, "ORCHESTRATE", script)
    flags, diag = recurrence._tick_env_attempt()
    assert flags == {}
    assert diag["detail"] == "exploration preview block is incomplete"

    script.write_text("echo ORCH_EXAMPLE=1\n", encoding="utf-8")

    def _timeout(*_a, **_k):
        raise subprocess.TimeoutExpired(cmd="bash", timeout=1)

    monkeypatch.setattr(recurrence.subprocess, "run", _timeout)
    flags, diag = recurrence._tick_env_attempt()
    assert flags == {}
    assert diag["reason"] == "timeout"


def test_tick_env_status_refreshes_when_no_diagnosis_is_cached(monkeypatch) -> None:
    monkeypatch.setattr(recurrence, "_TICK_ENV_DIAG", None)
    monkeypatch.setattr(recurrence, "tick_env", lambda **_k: {"ORCH_EXAMPLE": "1"})
    assert recurrence.tick_env_status() == {}


def test_thompson_review_failure_is_a_named_prerequisite(monkeypatch) -> None:
    import exploration_review

    monkeypatch.setattr(
        recurrence, "_predicate_flag", lambda *_a, **_k: {"fires": False, "detail": {}}
    )

    def _boom():
        raise RuntimeError("no route weights")

    monkeypatch.setattr(exploration_review, "build_report", _boom)
    detail = recurrence._check_thompson()["detail"]
    assert "review_unavailable" in detail
    assert "switch_on_when" in detail


def test_adjudicator_and_reference_gate_swallow_a_store_failure(monkeypatch) -> None:
    monkeypatch.setattr(
        recurrence, "_predicate_flag", lambda *_a, **_k: {"fires": True, "detail": {}}
    )

    def _boom(*_a, **_k):
        raise sqlite3.OperationalError("brain locked")

    monkeypatch.setattr(feedback, "_conn", _boom)
    adjudicator = recurrence._check_adjudicator()
    assert adjudicator["detail"]["recorded_disagreements"] == 0

    monkeypatch.setattr(capabilities, "load", _boom)
    gate = recurrence._check_reference_sync_gate()
    assert gate["fires"] is False
    assert "error" in gate["detail"]


def test_fetch_issue_returns_none_on_gh_failure_and_bad_json(monkeypatch) -> None:
    class _Proc:
        def __init__(self, code: int, stdout: str) -> None:
            self.returncode = code
            self.stdout = stdout

    monkeypatch.setattr(recurrence.subprocess, "run", lambda *_a, **_k: _Proc(1, ""))
    assert recurrence._fetch_issue("Orchestrator", 1025) is None
    monkeypatch.setattr(recurrence.subprocess, "run", lambda *_a, **_k: _Proc(0, "{"))
    assert recurrence._fetch_issue("Orchestrator", 1025) is None


def test_replay_classify_keeps_going_when_label_repair_fails_and_when_it_applies(
    monkeypatch,
) -> None:
    fixture = {
        "capability": "codemod-campaign",
        "kind": "classify",
        "expect_task_type": "codemod",
        "labels": ["bug"],
        "title": "Legacy removal",
        "source": "frozen",
        "apply_label_repair": True,
    }
    monkeypatch.setattr(recurrence, "FIXTURES", (fixture,))
    monkeypatch.setattr(recurrence, "PREDICATE_FIXTURES", ())
    monkeypatch.setattr(recurrence.backlog, "classify", lambda _labels: "codemod")
    import issue_readiness

    def _boom(_item):
        raise RuntimeError("repair down")

    monkeypatch.setattr(issue_readiness, "task_label_for", _boom)
    failed = recurrence.replay(offline=True)
    assert failed["rows"][0]["label_repair_applied"] is None
    assert failed["rows"][0]["fires"] is True

    monkeypatch.setattr(issue_readiness, "task_label_for", lambda _item: "area:codemod")
    applied = recurrence.replay(offline=True)
    assert applied["rows"][0]["label_repair_applied"] == "area:codemod"
    assert applied["rows"][0]["fires"] is True


def test_readers_return_nothing_when_the_trial_has_no_such_record(monkeypatch) -> None:
    monkeypatch.setattr(
        propensity,
        "experiments",
        lambda **_k: [
            {
                "experiment_id": "advice:abc",
                "declined": [],
                "decline_kinds": {},
                "reoffers": {},
            }
        ],
    )
    assert propensity.decline_reason_recorded("feedback-store", "advice:missing") == ""
    assert propensity.decline_kind_recorded("feedback-store", "advice:abc") is None
    assert propensity.existing_reoffer("feedback-store", "advice:missing") is None


def test_consult_outcome_refuses_a_blank_field_and_a_bad_slug() -> None:
    blank = propensity.record_consult_outcome(
        "advice:abc",
        direction="landed",
        evidence="  ",
        corroboration="owner/repo#1",
        provenance="outcome_corroborated",
    )
    assert blank["recorded"] is False
    assert "evidence" in blank["reason"]
    slug = propensity.record_consult_outcome(
        "advice:abc",
        direction="landed",
        evidence="the caller did the work",
        corroboration="owner/repo#1",
        provenance="outcome_corroborated",
        deliverable="Not A Slug",
    )
    assert slug["recorded"] is False
    assert "kebab-case" in slug["reason"]


def test_tick_artifact_and_state_helpers_name_failures(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setitem(__import__("sys").modules, "cadence_registry", None)
    assert propensity.tick_artifact("switch-review", state_dir=tmp_path) is None

    target = tmp_path / propensity.TICK_EVIDENCE_STATE
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("[1, 2]", encoding="utf-8")
    assert propensity._load_tick_state(tmp_path)["capabilities"] == {}

    def _refuse_replace(_src, _dst):
        raise OSError("replace refused")

    monkeypatch.setattr(propensity.os, "replace", _refuse_replace)
    with pytest.raises(OSError, match="replace refused"):
        propensity._write_json_atomic(tmp_path / "out.json", {"ok": True})
    assert not list(tmp_path.glob("out.json.*.tmp"))


def test_finding_projection_keeps_a_scalar_and_an_unfielded_dict() -> None:
    identity = propensity._finding_identity({"b": ["x"], "a": 1}, ())
    assert "a=1" in identity and "b=" in identity
    projected = propensity.project_findings(
        "capability-firing-monitor",
        {"never_fired": [{"capability_id": "feedback-store"}], "regressed": "not-a-list"},
    )
    assert projected is not None
    assert projected["regressed"] == ["not-a-list"]


def test_finds_and_repair_markers_skip_events_outside_the_window(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(capabilities, "load_declared", lambda _path: {"feedback-store": {}})
    monkeypatch.setattr(
        propensity,
        "_events",
        lambda _cap: [
            {"metadata": {"source": propensity.FIND_SOURCE}, "timestamp": 1},
            {"metadata": {"source": propensity.REPAIR_SOURCE}, "timestamp": 1},
        ],
    )
    monkeypatch.setattr(propensity, "_within_window", lambda *_a, **_k: False)
    assert propensity.finds(path=tmp_path / "ledger.json") == []
    assert propensity.repair_markers(path=tmp_path / "ledger.json") == {}


def test_live_tripwire_records_a_matching_open_and_swallows_a_bad_target() -> None:
    wire = {"roots": ["/tmp/issue-1025-live"], "patterns": [], "reached": []}
    propensity._LIVE_TRIPWIRES.append(wire)
    try:
        propensity._live_tripwire_hook("not-an-event", ("/tmp/issue-1025-live/a",))
        assert wire["reached"] == []
        propensity._live_tripwire_hook("open", (None,))
        propensity._live_tripwire_hook("open", (3,))
        propensity._live_tripwire_hook("open", ("/tmp/issue-1025-live/a.txt",))
        assert any(item.startswith("open ") for item in wire["reached"])

        class _Bad:
            def __fspath__(self):
                raise RuntimeError("not a path")

        propensity._live_tripwire_hook("open", (_Bad(),))
    finally:
        propensity._LIVE_TRIPWIRES.remove(wire)


def test_format_tick_evidence_states_each_population() -> None:
    assert propensity.format_tick_evidence({"error": "brain locked"}).startswith(
        "  [tick-evidence] error:"
    )
    quiet = propensity.format_tick_evidence({"consulted": False, "evaluated": [], "baselined": []})
    assert "nothing regenerated" in quiet
    text = propensity.format_tick_evidence(
        {
            "consulted": True,
            "evaluated": [
                {"capability_id": "useful-cap", "graded": True, "useful": True},
                {"capability_id": "quiet-cap", "graded": True, "useful": False},
            ],
            "baselined": [{"capability_id": "base-cap"}],
            "advice_capabilities": ["useful-cap"],
            "verdicts_recorded": 1,
            "gradable": [],
            "awaiting_regeneration": [],
        }
    )
    assert "USEFUL(output changed): useful-cap" in text
    assert "not useful(identical output): quiet-cap" in text
    assert "baselined: base-cap" in text


def test_fact_missing_refuses_a_blank_fact_and_a_bare_experiment() -> None:
    with pytest.raises(ValueError, match="missing fact"):
        propensity.record_fact_missing("feedback-store", "advice:abc", fact="  ")
    with pytest.raises(ValueError, match="advice:"):
        propensity.record_fact_missing("feedback-store", "not-advice", fact="diff size")


def test_cli_find_decline_and_late_outcome_finish_their_exits(monkeypatch, capsys) -> None:
    monkeypatch.setattr(propensity, "record_find", lambda **_k: {"recorded": True, "find_id": "f1"})
    assert (
        propensity.main(
            [
                "find",
                "--defect",
                "the hash was blank",
                "--artifact",
                "feedback.py:1772",
                "--surface",
                "repo-audit",
            ]
        )
        == 0
    )
    found = json.loads(capsys.readouterr().out)
    assert found["ledger"] == "live"
    assert found["recorded"] is True

    monkeypatch.setattr(propensity, "record_late_outcome", _raises("direction refused"))
    with pytest.raises(SystemExit) as late:
        propensity.main(
            [
                "late-outcome",
                "--capability",
                "feedback-store",
                "--experiment",
                "advice:abc",
                "--direction",
                "refutes",
            ]
        )
    assert late.value.code == 2

    with pytest.raises(SystemExit) as decline:
        propensity.main(
            [
                "decline",
                "--capability",
                "feedback-store",
                "--experiment",
                "advice:abc",
                "--reason",
                "  ",
            ]
        )
    assert decline.value.code == 2

    monkeypatch.setattr(propensity, "classify_decline_kind", lambda *_a, **_k: "wrong_match")
    monkeypatch.setattr(propensity, "record_decline", lambda *_a, **_k: True)
    assert (
        propensity.main(
            [
                "decline",
                "--capability",
                "feedback-store",
                "--experiment",
                "advice:abc",
                "--reason",
                "a different tool did the work",
                "--surface",
                "repo-audit",
            ]
        )
        == 0
    )
    body = json.loads(capsys.readouterr().out)
    assert body["recorded"] is True
    assert body["affects_propensity"] is False
