"""Hermetic coverage for coverage-autopilot round 1138 (lane C targets)."""

from __future__ import annotations

import argparse
import importlib
import json
import os
import socket
import subprocess
import sys
import time
import types
from pathlib import Path

import pytest

import capability_activation_audit as audit
import exp_abcd
import feedback
import local_verify
import periodic_report
import repo_knowledge
import tick


@pytest.fixture(autouse=True)
def isolated_runtime(monkeypatch, tmp_path):
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ORCH_LOCAL_RUNTIME", str(tmp_path / "runtime"))
    monkeypatch.setattr(audit, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "brain.db")
    monkeypatch.setenv("HANDOFF_DIR", str(tmp_path / "handoff"))
    monkeypatch.setenv("ORCH_CAPABILITY_HEARTBEATS", "0")

    attempted = []

    def forbid_external(*args, **kwargs):
        attempted.append((args, kwargs))
        pytest.fail("round 1138 test attempted an external process or network connection")

    monkeypatch.setattr(subprocess, "run", forbid_external)
    monkeypatch.setattr(subprocess, "Popen", forbid_external)
    monkeypatch.setattr(socket, "socket", forbid_external)
    monkeypatch.setattr(socket, "create_connection", forbid_external)
    yield tmp_path
    assert attempted == []


# ----- local_verify ---------------------------------------------------------


def test_record_verdict_prefers_node_ids_over_test_paths():
    feedback.record_run("lv-1", "o/r#1", "implement", "codex")
    recorded = local_verify.record_verdict(
        "lv-1",
        {
            "verdict": "PASS",
            "reason": "nodes ok",
            "test_paths": ["tests/a.py"],
            "node_analysis": {"nodes": [{"node_id": "tests/a.py::test_x"}]},
            "green": {"cmd": "pytest"},
            "red": {"cmd": "pytest"},
        },
    )
    assert recorded["recorded"] is True
    with feedback._conn() as conn:
        outcome = conn.execute(
            "SELECT notes FROM outcomes WHERE run_id=?",
            ("lv-1",),
        ).fetchone()
    assert outcome[0].startswith("local_verify: PASS")
    with feedback._conn() as conn:
        event = conn.execute(
            "SELECT payload_json, validation_status FROM completion_events "
            "WHERE run_id=? AND event_type='verification' AND producer='local_verify'",
            ("lv-1",),
        ).fetchone()
    assert event is not None
    assert event[1] != "rejected"
    assert json.loads(event[0])["test_ids"] == ["tests/a.py::test_x"]


def test_default_test_paths_and_looks_like_test_filters(monkeypatch, tmp_path):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / "tests").mkdir()
    (worktree / "tests" / "t.py").write_text("def test_x(): pass\n")
    (worktree / "src").mkdir()
    (worktree / "src" / "m.py").write_text("x = 1\n")

    monkeypatch.setattr(
        local_verify,
        "_changed_paths",
        lambda *_a: ["tests/t.py", "src/m.py", "notes.txt"],
    )
    assert local_verify._looks_like_test("tests/t.py")
    assert not local_verify._looks_like_test("src/m.py")
    assert local_verify._default_test_paths(worktree, "main") == ["tests/t.py"]


# ----- capability_activation_audit ------------------------------------------


def test_vocabulary_gaps_skips_modules_that_fail_to_import(monkeypatch):
    real = importlib.import_module

    def selective(name, *args, **kwargs):
        if name == "adversarial":
            raise ImportError("missing module")
        return real(name, *args, **kwargs)

    monkeypatch.setattr(importlib, "import_module", selective)
    monkeypatch.setattr(
        audit, "GATE_VOCABULARIES", (("adversarial-review", "adversarial", "HIGH_STAKES_LABELS"),)
    )
    assert audit.vocabulary_gaps({"repos": {"o/r": ["risk:major"]}}) == {}


def test_prompt_templates_survives_dispatcher_import_failure(monkeypatch):
    monkeypatch.setitem(sys.modules, "dispatcher", types.SimpleNamespace())
    assert audit._prompt_templates() == set()


def test_sibling_checkouts_continues_when_worktree_listing_fails(monkeypatch, isolated_runtime):
    root = isolated_runtime / "checkout"
    root.mkdir()
    (root / "capabilities.py").write_text("# checkout\n")
    worktrees = root / ".claude" / "worktrees"
    worktrees.mkdir(parents=True)
    monkeypatch.setattr(audit, "_repo_root", lambda: root)
    monkeypatch.setattr(audit, "HERE", root / "src")
    real_iterdir = Path.iterdir

    def patched_iterdir(self):
        if self.resolve() == worktrees.resolve():
            raise OSError("cannot list worktrees")
        return real_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", patched_iterdir)
    labels = [label for _, label in audit.sibling_checkouts()]
    assert "repo root" in labels


def test_absent_entrypoint_note_returns_diagnostic_when_report_raises(monkeypatch):
    monkeypatch.setattr(
        audit,
        "absent_entrypoint_report",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("ledger unreadable")),
    )
    note = audit.absent_entrypoint_note(["cap-a"])
    assert "entrypoint-presence diagnostic unavailable" in note
    assert "RuntimeError" in note


def test_callers_of_uses_text_fallback_when_driver_does_not_parse(tmp_path, monkeypatch):
    driver = tmp_path / "broken_driver.py"
    driver.write_text("import foo\nfoo.run(\n")
    monkeypatch.setattr(audit, "DRIVER_MODULES", ("broken_driver.py",))
    monkeypatch.setattr(audit, "_driver_path", lambda name: driver)
    assert audit._callers_of("foo", {"run"}) == ["broken_driver.py:run"]
    assert audit._callers_of("foo", {"missing"}) == []


def test_heartbeat_reachable_credits_dispatcher_lane_capabilities(monkeypatch):
    fake_dispatcher = types.SimpleNamespace(TASK_TYPE_CAPABILITY={"testgen": "role-prompt"})
    monkeypatch.setitem(sys.modules, "dispatcher", fake_dispatcher)
    reach = audit.heartbeat_reachable(
        {"capability_id": "role-prompt", "entrypoint": "roles.py:run"}
    )
    assert reach["status"] == "reachable"
    assert "TASK_TYPE_CAPABILITY" in reach["via"][0]


def test_advisor_reach_tolerates_direct_entry_failure(monkeypatch):
    import capability_advisor

    monkeypatch.setattr(
        capability_advisor,
        "direct_entry",
        lambda: (_ for _ in ()).throw(RuntimeError("advisor down")),
    )
    out = audit.advisor_reach(
        {"live": {"status": "active", "matcher": {"field": "task_type", "value": ["x"]}}}
    )
    assert out["direct_entry"] == {}


def test_write_label_cache_cleans_temp_file_on_failure(monkeypatch, isolated_runtime):
    path = isolated_runtime / "state" / "labels.json"

    def fail_replace(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", fail_replace)
    audit._write_label_cache(path, {"generated_at": 1, "repos": {}, "read_at": {}, "unread": {}})
    assert not any(path.parent.glob("labels.json.*.tmp"))


def test_read_repo_labels_maps_subprocess_failures(isolated_runtime):
    def runner(argv, **kwargs):
        raise OSError("gh missing")

    labels, reason, retry = audit._read_repo_labels("o/r", runner)
    assert labels is None and "OSError" in reason and retry is False


def test_audit_capability_task_route_records_label_shortfall(isolated_runtime):
    index = {
        "repos": {"o/one": ["bug"]},
        "read_at": {"o/one": 1},
        "unknown": [],
    }
    row = audit.audit_capability(
        "task-cap",
        {"status": "active", "matcher": {"field": "task_type", "value": ["testgen"]}},
        emittable={"testgen"},
        templates={"testgen"},
        label_index=index,
    )
    assert "label_absent_from_fleet" in row["defects"]
    assert row["label_coverage"]["testgen"]["repos_with"] == 0


def test_audit_capability_external_entrypoint_notes_sibling_presence(tmp_path, monkeypatch):
    tree = tmp_path / "tree"
    tree.mkdir()
    saved = audit.HERE
    sibling = tmp_path / "sibling"
    sibling.mkdir()
    (sibling / "capabilities.py").write_text("# s\n")
    ext = sibling / "external.py"
    ext.write_text("def run():\n    pass\n")
    try:
        audit.HERE = tree
        monkeypatch.setattr(audit, "sibling_checkouts", lambda: [(sibling, "sibling")])
        monkeypatch.setattr(
            audit,
            "heartbeat_reachable",
            lambda _cap: {"status": "no_local_entrypoint", "detail": str(ext)},
        )
        monkeypatch.setattr(
            audit,
            "external_caller",
            lambda _cap: {"exists": False, "repo": "Workflows", "workflow": "ci"},
        )
        cap = {
            "status": "active",
            "matcher": {"kind": "transport"},
            "entrypoint": str(ext),
        }
        row = audit.audit_capability(
            "ext-cap",
            cap,
            emittable=set(),
            templates=set(),
            label_index={"repos": {}, "read_at": {}, "unknown": []},
        )
        joined = " ".join(row["notes"])
        assert "wait-or-merge" in joined
        assert "sibling" in joined
    finally:
        audit.HERE = saved


def test_load_history_returns_empty_on_corrupt_file(isolated_runtime):
    history = isolated_runtime / "history.json"
    history.write_text("{not json")
    assert audit.load_history(path=history) == []


def test_record_snapshot_reports_write_failures(monkeypatch, isolated_runtime):
    history = isolated_runtime / "history.json"

    def fail_write(*args, **kwargs):
        raise OSError("read-only")

    monkeypatch.setattr(Path, "write_text", fail_write)
    result = audit.record_snapshot(
        {
            "generated_at": 1,
            "total": 0,
            "reachable": 0,
            "blocked": 0,
            "not_audited_count": 0,
            "reachable_ids": [],
            "by_defect": {},
        },
        path=history,
    )
    assert result["recorded"] is False


def test_format_scorecard_progress_lists_gained_regressed_and_retired():
    rep = {
        "generated_at": 200,
        "total": 1,
        "reachable": 0,
        "blocked": 1,
        "by_entry_class": {"direct": 1},
        "by_defect": {"no_heartbeat": ["x"]},
        "rows": [
            {
                "capability_id": "x",
                "entry_class": "direct",
                "audited": True,
                "reachable": False,
                "defects": ["no_heartbeat"],
            }
        ],
    }
    prog = {
        "baseline": 100,
        "reachable_then": 1,
        "reachable_now": 0,
        "gained": ["new-cap"],
        "regressed": ["old-cap"],
        "retired_since": {"retired": ["gone-cap"]},
        "defect_delta": {"no_heartbeat": 1},
    }
    text = audit.format_scorecard(rep, prog)
    assert "GAINED:" in text and "REGRESSED:" in text
    assert "RETIRED SINCE:" in text and "defect delta:" in text


def test_audit_main_snapshot_and_json(monkeypatch, isolated_runtime, capsys):
    monkeypatch.setattr(
        audit,
        "audit",
        lambda **kw: {
            "generated_at": 1,
            "total": 0,
            "reachable": 0,
            "blocked": 0,
            "by_defect": {},
            "rows": [],
            "reachable_ids": [],
        },
    )
    monkeypatch.setattr(audit, "progress", lambda *_a, **_k: None)
    monkeypatch.setattr(audit, "record_snapshot", lambda rep: {"recorded": True})
    assert audit.main(["--json", "--snapshot", "--no-cache"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["snapshot"]["recorded"] is True


# ----- exp_abcd ----------------------------------------------------------------


def test_winner_and_harvest_rejects_all_excluded_judges():
    with pytest.raises(ValueError, match="no eligible evaluator"):
        exp_abcd._winner_and_harvest(
            {"gemini": {"scores": {"A": 10}}},
            {"gemini": {"A": "codex"}},
            reliability={"ready_judge_count": 0, "judges": {}},
        )


def test_eval_command_restores_bypass_and_state_env(monkeypatch, tmp_path):
    import adapters

    monkeypatch.setattr(adapters, "gemini_model", lambda: "gemini-test")
    monkeypatch.setenv("ORCH_MODEL_PROBE", "0")
    # Command construction must not initialize the user's installed agent runtime.
    monkeypatch.setattr(exp_abcd.dispatcher, "_agent_runtime_prelude", lambda _agent: "")
    monkeypatch.setattr(exp_abcd.dispatcher, "_auth_prelude", lambda _agent: "")

    ws = tmp_path / "ws"
    ws.mkdir()
    old_bypass = os.environ.pop("ORCH_CODEX_BYPASS_INNER_SANDBOX", None)
    try:
        os.environ["ORCH_CODEX_BYPASS_INNER_SANDBOX"] = "0"
        cmd = exp_abcd._eval_command("codex", str(tmp_path / "p.txt"), ws)
        assert 'read-only "$(cat' in cmd
        os.environ["ORCH_CODEX_BYPASS_INNER_SANDBOX"] = "1"
        bypass = exp_abcd._eval_command("codex", str(tmp_path / "p.txt"), ws)
        assert "--dangerously-bypass-approvals-and-sandbox" in bypass
    finally:
        if old_bypass is None:
            os.environ.pop("ORCH_CODEX_BYPASS_INNER_SANDBOX", None)
        else:
            os.environ["ORCH_CODEX_BYPASS_INNER_SANDBOX"] = old_bypass

    with pytest.raises(adapters.WorkspaceRefused):
        exp_abcd._eval_command("cursor", "/tmp/p.txt", "/")

    old_state = os.environ.get("ORCH_STATE_DIR")
    os.environ["ORCH_STATE_DIR"] = str(tmp_path / "state")
    try:
        cmd = exp_abcd._eval_command("vibe", str(tmp_path / "p.txt"))
        assert "scratch-workspaces" in cmd
    finally:
        if old_state is None:
            os.environ.pop("ORCH_STATE_DIR", None)
        else:
            os.environ["ORCH_STATE_DIR"] = old_state


# ----- periodic_report -------------------------------------------------------


def test_positive_int_rejects_zero():
    with pytest.raises(argparse.ArgumentTypeError, match="positive"):
        periodic_report._positive_int("0")


def test_experiment_identity_summary_tolerates_bad_run_metadata():
    feedback.record_run(
        "e1",
        "o/r#1",
        "implement",
        "codex",
        experiment_id="exp-1",
        routing_metadata="{not-json",
    )
    summary = periodic_report._experiment_identity_summary(30)
    assert summary["implementation_arms"] == []
    assert summary["missing_arm_outcomes"] == []


def test_process_action_covers_each_work_type():
    assert "Renovate" in periodic_report._process_action("renovate")
    assert "sync manifest" in periodic_report._process_action("sync")
    assert "guardrail" in periodic_report._process_action("tooling")
    assert "doc freshness" in periodic_report._process_action("docs")
    assert "recurring maintenance" in periodic_report._process_action("issue")


def test_render_fleet_six_marks_empty_agent_windows():
    section = {
        "windows": {"7": {}, "28": {}},
        "time_to_merge": {"windows": {"7": {}, "28": {}}},
    }
    line = periodic_report.render_fleet_six(section)[5]
    assert "unmeasured (no merged agent PRs)" in line


def test_format_human_includes_owner_questions_and_outcome_gaps():
    report = {
        "db_path": "/tmp/db",
        "window_days": 7,
        "dataset": {"table_counts": {}, "route_weights_version": None},
        "route_weights": {"tasks": []},
        "rail_exercise": {"available": False, "reason": "offline"},
        "experiments": {
            "implementation_arms": [
                {
                    "experiment_id": "e1",
                    "arm_id": "a",
                    "members": ["m"],
                    "agents": ["codex"],
                    "profiles": [],
                    "evaluation_observations": 1,
                    "mean_score": None,
                    "mean_member_evaluation_score": 0.0,
                }
            ],
            "missing_arm_outcomes": [
                {"experiment_id": "e2", "arm_id": "b", "members": ["m"], "reason": "no data"}
            ],
            "evaluator_identities": [],
        },
        "exploration_policy": {},
        "outcomes": {
            "window_days": 7,
            "total": 0,
            "rollup": {},
            "by_task_agent_verdict": [],
        },
        "process_improvement": {"signals": [], "work_type_rollup": []},
        "production_flow": {},
        "keepalive_supervisor": {
            "status": "hold",
            "summary": {},
            "thresholds": {},
            "recommendation": "wait",
            "stage2_proposal_corpus": {"status": "off", "summary": {}},
        },
        "costs_traces": {
            "window_days": 7,
            "costs_by_source": [],
            "trace_status_counts": [],
            "traces_by_source": [],
        },
        "hypotheses": {"status_counts": {}, "hypotheses": []},
        "owner_questions": [
            {
                "question_id": "q1",
                "repo": "o/r",
                "question": "Ship?",
                "default_action": "yes",
            }
        ],
        "evidence": {
            "window_days": 7,
            "open_gaps_by_recurrence": [],
            "proposals": [],
            "evidence_types": {"counts_by_status": {}},
            "schema_growth": {"clustered_proposals": [], "active_type_review": {"active": []}},
        },
        "dry_seams": {
            "overall": "warn",
            "status_counts": {},
            "findings": [],
            "completion_event_health": {},
            "outcome_gap_summary": {"total_runs_without_outcome": 3},
        },
        "role_activation": {
            "roles": {
                "triage": {
                    "matched": 1,
                    "invoked": 0,
                    "accepted": 0,
                    "rejected": 0,
                    "linked": 0,
                    "durable": 0,
                    "evidence_readiness": "cold",
                }
            }
        },
    }
    text = periodic_report.format_human(report)
    assert "OWNER QUESTIONS" in text
    assert "outcome gaps:" in text


def test_main_json_fleet_six_writes_markdown(monkeypatch, isolated_runtime, capsys):
    fleet = {"windows": {"7": {}, "28": {}}, "time_to_merge": {}}
    monkeypatch.setattr(
        periodic_report,
        "build_report",
        lambda **kw: {"fleet_six": fleet, "window_days": 7, "db_path": "x", "dataset": {}},
    )
    monkeypatch.setattr(periodic_report, "render_fleet_six", lambda _s: ["FLEET-6 line"])
    written = {"called": False}

    def capture(section, path=None):
        written["called"] = True
        return path or isolated_runtime / "fleet-six.md"

    monkeypatch.setattr(periodic_report, "write_fleet_six_md", capture)
    assert periodic_report.main(["--json"]) == 0
    err = capsys.readouterr().err
    assert "FLEET-6 line" in err
    assert written["called"] is True


def test_main_rejects_apply_with_snapshot_json(capsys):
    snap = Path("/tmp/snapshot.json")
    with pytest.raises(SystemExit) as exc:
        periodic_report.main(
            [
                "--snapshot-json",
                str(snap),
                "--approve-evidence-type",
                "t",
                "--from-gap",
                "gap",
                "--apply",
            ]
        )
    assert exc.value.code != 0


# ----- tick ------------------------------------------------------------------


def test_fetch_issue_body_returns_none_when_gh_fails(monkeypatch):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_a, **_k: subprocess.CompletedProcess([], 1, "", "fail"),
    )
    assert tick._fetch_issue_body("o/r#1") is None


def test_research_tick_handles_claim_race_and_meta_base_sha(
    monkeypatch, isolated_runtime, tmp_path
):
    monkeypatch.setattr(tick.claims, "claim", lambda *_a, **_k: False)
    monkeypatch.setattr(tick.claims, "release", lambda *_a, **_k: None)
    monkeypatch.setattr(tick.research_subjects, "record_event", lambda *_a, **_k: None)
    monkeypatch.setattr(tick.research_subjects, "assess_candidate", lambda **_k: {"eligible": True})
    monkeypatch.setattr(
        tick.research_subjects, "subject_identity", lambda *_a, **_k: {"subject_id": "s1"}
    )
    monkeypatch.setattr(tick, "_fetch_issue_body", lambda *_a, **_k: "spec body")
    monkeypatch.setattr(tick.capabilities, "production_heartbeat", lambda *_a, **_k: None)
    exp_dir = isolated_runtime / "exp-1"
    exp_dir.mkdir(parents=True)
    (exp_dir / "meta.json").write_text('{"base_sha": "abc"}')
    monkeypatch.setattr(tick.exp_abcd, "exp_paths", lambda _e: exp_dir)
    plan_out = {
        "planned": [
            {
                "item": {"target": "o/r#9", "task_type": "implement"},
                "task_type": "implement",
                "arms": ["codex"],
            }
        ],
        "skipped": [],
    }
    monkeypatch.setattr(tick.research_scheduler, "build_research_plan", lambda *_a, **_k: plan_out)
    monkeypatch.setenv("ORCH_RESEARCH_ARM", "1")
    out = tick.research_tick([], {}, dry_run=False, env=os.environ.copy())
    assert out["planned"][0]["active_status"] == "blocked_by_claim"


def test_remote_tick_passes_triage_influence_when_agreed(monkeypatch):
    import dispatcher

    captured = {}

    def delegate(agent, target, **kwargs):
        captured.update(agent=agent, kwargs=kwargs)
        return {"applied": True, "labels_read": True}

    monkeypatch.setattr(dispatcher, "delegate_remote", delegate)
    monkeypatch.setattr(tick.router, "select_remote_agent", lambda *_a, **_k: {"agent": "codex"})
    monkeypatch.setattr(
        tick.roles,
        "activate_tick_triage",
        lambda *_a, **_k: {
            "recommendations": {"o/r#1": {"action": "work_now"}},
            "result": {"role_run_id": "role-1"},
        },
    )
    monkeypatch.setattr(tick, "is_delegation", lambda row: True)
    monkeypatch.setenv("ORCH_DISPATCH_LANE", "1")
    tick.remote_tick(
        [{"target": "o/r#1", "task_type": "implement", "lane": "opener"}],
        {},
        dry_run=False,
        do_ingest=False,
        env=os.environ.copy(),
    )
    assert captured["kwargs"]["influenced_by_role_run_ids"] == ["role-1"]


def test_main_summary_cleans_temp_plan_on_replace_failure(monkeypatch, isolated_runtime):
    monkeypatch.setattr(tick.router, "load_capacity", lambda: {})
    monkeypatch.setattr(tick.router, "load_backlog", lambda: [])
    monkeypatch.setattr(tick.router, "learned_ranks", lambda: {})
    monkeypatch.setattr(tick, "load_research_feed", lambda: {})
    monkeypatch.setattr(tick, "backlog_age", lambda: {})
    monkeypatch.setattr(tick, "remote_tick", lambda *_a, **_k: {"chosen": []})

    def boom_replace(src, dst):
        raise OSError("replace failed")

    monkeypatch.setattr(os, "replace", boom_replace)
    state = isolated_runtime / "state"
    with pytest.raises(OSError, match="replace failed"):
        tick.main(["--summary"])
    assert list(state.glob(".tick-plan-*")) == []


# ----- repo_knowledge --------------------------------------------------------


def test_reseed_section_handles_contraindications_and_blank_keys():
    live = [{"capability": "cap-a", "reason": "old"}]
    seeded = [{"capability": "cap-a", "reason": "new"}]
    merged = repo_knowledge._reseed_section(
        live, seeded, section=repo_knowledge.CONTRAINDICATION_SECTION
    )
    assert merged[0]["reason"] == "new"
    assert repo_knowledge._reseed_section([{"text": "x"}], [{"text": ""}], section="gotchas") == [
        {"text": "x"}
    ]


def test_memory_visibility_and_playbook_records(isolated_runtime):
    reg = isolated_runtime / "registry.json"
    repo_knowledge.save(
        {
            "schema_version": repo_knowledge.SEED_SCHEMA_VERSION,
            "repos": {
                "o/r": {
                    "summary": "Repo summary",
                    "base_branch": "main",
                    "validation": [
                        {"text": "scoped", "task_types": ["implement"], "lanes": ["opener"]},
                        {"text": "other", "task_types": ["review"]},
                    ],
                    repo_knowledge.CONTRAINDICATION_SECTION: [
                        {"capability": "offload", "reason": "no"}
                    ],
                }
            },
        },
        reg,
    )
    assert (
        repo_knowledge._memory_item_visible(
            {"task_types": ["review"]}, task_type="implement", lane=None
        )
        is False
    )
    records = repo_knowledge._playbook_memory_records(
        "o/r", task_type="implement", lane="opener", path=reg
    )
    sections = {row["section"] for row in records}
    assert "summary" in sections and repo_knowledge.CONTRAINDICATION_SECTION in sections


def test_append_context_honors_playbook_kill_switch(monkeypatch, isolated_runtime):
    reg = isolated_runtime / "registry.json"
    repo_knowledge.save(
        {"schema_version": repo_knowledge.SEED_SCHEMA_VERSION, "repos": {"o/r": {"summary": "S"}}},
        reg,
    )
    monkeypatch.setenv("ORCH_REPO_PLAYBOOK", "0")
    assert repo_knowledge.append_context("base", "o/r", path=reg) == "base"


def test_main_cli_errors_for_incomplete_suggest_flags(capsys):
    assert repo_knowledge.main(["--suggest-from-review-json"]) == 2
    assert "requires a JSON file" in capsys.readouterr().err
    assert repo_knowledge.main(["--suggest-from-review-json", "x.json"]) == 2
    assert "--repo owner/repo" in capsys.readouterr().err
    assert repo_knowledge.main(["--suggest-from-pr"]) == 2
    assert repo_knowledge.main(["--approve-suggestion"]) == 2


# ----- second pass: remaining CI-missing statements -------------------------


def test_break_transcript_states_indeterminate_node_attribution():
    text = local_verify.break_transcript(
        {
            "verdict": "PASS",
            "base_ref": "main",
            "worktree": "/tmp/wt",
            "green": {"cmd": "pytest", "returncode": 0, "stdout_tail": "", "stderr_tail": ""},
            "red": {"cmd": "pytest", "returncode": 1, "stdout_tail": "fail", "stderr_tail": ""},
            "node_verdict": "INDETERMINATE",
            "hollow_nodes": [],
            "node_analysis": {"reason": "no node ids", "counts": {}},
        }
    )
    assert "Per-node attribution unavailable" in text
    assert "no node ids" in text


def test_extract_base_returns_git_archive_error(monkeypatch, tmp_path):
    failed = subprocess.CompletedProcess(["git"], 1, b"", b"archive denied\n")
    monkeypatch.setattr(local_verify, "_git", lambda *_a, **_k: failed)
    err = local_verify._extract_base(tmp_path, "main", tmp_path / "dest")
    assert err == "archive denied"


def test_pytest_failure_cause_defaults_to_test_failure_when_classifier_raises(monkeypatch):
    import testgen_gate

    def explode(*_a, **_k):
        raise RuntimeError("classifier down")

    monkeypatch.setattr(testgen_gate, "pytest_failure_cause", explode)
    cause = local_verify._pytest_failure_cause(
        {"returncode": 1, "stdout_tail": "", "stderr_tail": ""}
    )
    assert cause == {"test_failure": True, "cause": "a test failed", "remedy": ""}


def test_verify_errors_when_no_test_paths_are_detected(monkeypatch, tmp_path):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    monkeypatch.setattr(local_verify, "_changed_paths", lambda *_a, **_k: ["src/m.py"])
    result = local_verify.verify(worktree, test_cmd="pytest -q")
    assert result["verdict"] == "ERROR"
    assert result["error"] == "no test paths supplied or detected"


def test_verify_errors_when_base_extraction_fails(monkeypatch, tmp_path):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    monkeypatch.setattr(local_verify, "_changed_paths", lambda *_a, **_k: ["src/m.py"])
    monkeypatch.setattr(
        local_verify, "_run", lambda *_a, **_k: {"ok": True, "cmd": "pytest", "returncode": 0}
    )
    monkeypatch.setattr(local_verify, "_extract_base", lambda *_a, **_k: "archive failed")
    result = local_verify.verify(worktree, test_cmd="pytest -q", test_paths=["tests/t.py"])
    assert result["verdict"] == "ERROR"
    assert result["error"] == "archive failed"
    assert result["red"] is None


def test_verify_records_indeterminate_node_reason(monkeypatch, tmp_path):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    calls = {"n": 0}

    def run(*_a, **_k):
        calls["n"] += 1
        return {"ok": calls["n"] == 1, "cmd": "pytest", "returncode": 0 if calls["n"] == 1 else 1}

    monkeypatch.setattr(local_verify, "_changed_paths", lambda *_a, **_k: ["src/m.py"])
    monkeypatch.setattr(local_verify, "_run", run)
    monkeypatch.setattr(local_verify, "_extract_base", lambda *_a, **_k: None)
    monkeypatch.setattr(local_verify, "_copy_candidate_paths", lambda *_a, **_k: ["tests/t.py"])
    monkeypatch.setattr(
        local_verify,
        "_analyse_nodes",
        lambda *_a, **_k: {
            "verdict": "INDETERMINATE",
            "hollow_nodes": [],
            "reason": "parser unavailable",
            "counts": {"nodes": 1},
        },
    )
    result = local_verify.verify(worktree, test_cmd="pytest -q", test_paths=["tests/t.py"])
    assert result["verdict"] == "PASS"
    assert "per-node attribution unavailable: parser unavailable" in result["reason"]


def test_capability_heartbeat_swallows_recorder_failure(monkeypatch):
    import capabilities

    def explode(*_a, **_k):
        raise RuntimeError("heartbeat down")

    monkeypatch.setattr(capabilities, "production_heartbeat", explode)
    local_verify._capability_heartbeat()
    repo_knowledge._capability_heartbeat()


def test_sibling_checkouts_skips_root_that_does_not_resolve(monkeypatch, isolated_runtime):
    root = isolated_runtime / "checkout"
    root.mkdir()
    here = root / "src"
    here.mkdir()
    monkeypatch.setattr(audit, "_repo_root", lambda: root)
    monkeypatch.setattr(audit, "HERE", here)
    real_resolve = Path.resolve

    def patched(self, *args, **kwargs):
        if self == root:
            raise OSError("dangling")
        return real_resolve(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", patched)
    assert audit.sibling_checkouts() == []


def test_callers_of_text_matches_unparseable_wildcard_driver(tmp_path, monkeypatch):
    driver = tmp_path / "broken_driver.py"
    driver.write_text("foo.\n")
    monkeypatch.setattr(audit, "DRIVER_MODULES", ("broken_driver.py",))
    monkeypatch.setattr(audit, "_driver_path", lambda _name: driver)
    assert audit._callers_of("foo", set()) == ["broken_driver.py"]


def test_external_caller_rejects_bad_declaration_and_unnamed_workflow(tmp_path):
    rejected = audit.external_caller({"external_caller": "nope", "entrypoint": "/tmp/x.py:run"})
    assert rejected["exists"] is False
    assert "must name a path and function" in rejected["detail"]

    outside = tmp_path / "outside"
    outside.mkdir()
    entry = outside / "entry.py"
    caller = outside / "caller.py"
    entry.write_text("def run():\n    pass\n")
    caller.write_text("def run():\n    pass\n")
    invalid = audit.external_caller(
        {
            "external_caller": {"path": str(caller), "function": "not-valid"},
            "entrypoint": str(entry),
        }
    )
    assert invalid["exists"] is False
    assert "function is invalid" in invalid["detail"]

    unnamed = audit.external_caller(
        {"matcher": {"kind": "ci_workflow", "name": ""}, "entrypoint": "noslash"}
    )
    assert unnamed == {
        "exists": False,
        "detail": "matcher or entrypoint does not name a repo/workflow",
    }


def test_heartbeat_reachable_survives_dispatcher_and_bridge_failures(monkeypatch):
    monkeypatch.setitem(sys.modules, "dispatcher", types.SimpleNamespace())
    monkeypatch.setitem(sys.modules, "capability_outcome_bridge", types.SimpleNamespace())
    reach = audit.heartbeat_reachable({"capability_id": "not-a-lane", "entrypoint": ""})
    assert reach["status"] == "no_local_entrypoint"


def test_write_label_cache_ignores_unlink_failure_after_replace_failure(
    monkeypatch, isolated_runtime
):
    path = isolated_runtime / "state" / "labels.json"
    real_unlink = Path.unlink

    def fail_replace(*_a, **_k):
        raise OSError("disk full")

    def fail_unlink(self, *args, **kwargs):
        if self.name.startswith("labels.json."):
            raise OSError("unlink failed")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(os, "replace", fail_replace)
    monkeypatch.setattr(Path, "unlink", fail_unlink)
    audit._write_label_cache(path, {"generated_at": 1, "repos": {}, "read_at": {}, "unread": {}})
    assert not path.exists()


def test_audit_capability_notes_when_cross_repo_caller_has_landed(monkeypatch):
    monkeypatch.setattr(
        audit,
        "heartbeat_reachable",
        lambda _cap: {"status": "no_local_entrypoint", "detail": "Workflows/scripts/run.py"},
    )
    monkeypatch.setattr(
        audit,
        "entrypoint_presence",
        lambda _cap: {"state": audit.ENTRYPOINT_EXTERNAL, "found_in": []},
    )
    monkeypatch.setattr(
        audit,
        "external_caller",
        lambda _cap: {"exists": True, "repo": "Workflows", "workflow": "ci"},
    )
    row = audit.audit_capability(
        "ext-landed",
        {
            "status": "active",
            "matcher": {"kind": "ci_workflow", "name": "ci"},
            "entrypoint": "Workflows/scripts/run.py",
        },
        emittable=set(),
        templates=set(),
        label_index={"repos": {}, "read_at": {}, "unknown": []},
    )
    assert any(
        "caller present: Workflows/.github/workflows/ci.yml" in note for note in row["notes"]
    )
    assert "entrypoint_external" not in row["defects"]


def test_positive_int_accepts_a_positive_value():
    assert periodic_report._positive_int("4") == 4


def test_render_fleet_six_marks_unmeasured_merge_time():
    section = {
        "windows": {"7": {}, "28": {}},
        "time_to_merge": {
            "windows": {
                "7": {
                    "codex": {
                        "n_with_facts": 0,
                        "n_merged": 2,
                        "median_hours": None,
                        "p90_hours": None,
                    }
                },
                "28": {},
            }
        },
    }
    rendered = "\n".join(periodic_report.render_fleet_six(section))
    assert "codex unmeasured (0/2 facts)" in rendered


def test_process_improvement_counts_other_failure_durability():
    feedback.record_run(
        "proc-1", "o/r#1", "implement", "codex", work_type="tooling", ts=int(time.time())
    )
    feedback.record_outcome("proc-1", durability="broke_later", notes="tooling broke later")
    summary = periodic_report._process_improvement_summary(30)
    tooling = next(row for row in summary["signals"] if row["work_type"] == "tooling")
    assert tooling["other_failure_count"] == 1
    assert tooling["failure_count"] == 1


def test_keepalive_supervisor_disables_live_when_stage2_is_not_ready(monkeypatch, isolated_runtime):
    corpus = isolated_runtime / "corpus.json"
    corpus.write_text("{}")
    monkeypatch.setattr(
        periodic_report.keepalive_shadow,
        "summarize",
        lambda _path: {
            "ready_for_ab": True,
            "failure_outcomes": 9,
            "meaningful_disagreements": 4,
            "readiness_target": 10,
        },
    )
    monkeypatch.setattr(
        periodic_report,
        "_redirect_stage2_summary",
        lambda *_a, **_k: {
            "ready_for_supervised_apply": False,
            "recommendation": "collect more proposals",
            "summary": {},
        },
    )
    summary = periodic_report._keepalive_supervisor_summary(corpus)
    assert summary["status"] == "armed_for_layered_ab_review"
    assert summary["recommendation"] == ("collect more proposals; live supervisor remains disabled")


def test_hypothesis_summary_returns_empty_when_loader_fails(monkeypatch, isolated_runtime):
    def explode(*_a, **_k):
        raise json.JSONDecodeError("bad", "x", 0)

    monkeypatch.setattr(periodic_report.research_scheduler, "load_hypotheses", explode)
    summary = periodic_report._hypothesis_summary(isolated_runtime / "hypotheses.json")
    assert summary["hypotheses"] == []
    assert summary["status_counts"] == {}


def _human_report_with_remaining_branches() -> dict:
    return {
        "db_path": "/tmp/db",
        "window_days": 7,
        "dataset": {"table_counts": {"runs": 1}, "route_weights_version": 2},
        "route_weights": {
            "tasks": [
                {
                    "task_type": "implement",
                    "cold_start": True,
                    "prior_order": ["codex"],
                    "learned_order": [],
                    "note": "cold",
                },
                {
                    "task_type": "review",
                    "prior_order": ["codex"],
                    "learned_order": ["codex"],
                    "previous_order": ["gemini"],
                    "note": "learned-note",
                    "rows": [],
                },
            ]
        },
        "rail_exercise": {
            "available": True,
            "tree": "checkout",
            "totals": {
                "contracts": 2,
                "passed": 2,
                "broke-correctly": 0,
                "failed": 0,
                "skipped": 0,
            },
        },
        "experiments": {
            "implementation_arms": [],
            "missing_arm_outcomes": [],
            "evaluator_identities": [],
        },
        "exploration_policy": {},
        "outcomes": {"window_days": 7, "total": 0, "rollup": {}, "by_task_agent_verdict": []},
        "process_improvement": {"signals": [], "work_type_rollup": []},
        "production_flow": {},
        "keepalive_supervisor": {
            "status": "hold",
            "summary": {},
            "thresholds": {},
            "recommendation": "wait",
            "stage2_proposal_corpus": {"status": "off", "summary": {}},
        },
        "costs_traces": {
            "window_days": 7,
            "costs_by_source": [],
            "trace_status_counts": [],
            "traces_by_source": [],
        },
        "hypotheses": {"status_counts": {}, "hypotheses": []},
        "owner_questions": [],
        "evidence": {
            "window_days": 7,
            "open_gaps_by_recurrence": [],
            "proposals": [],
            "evidence_types": {"counts_by_status": {}},
            "schema_growth": {
                "clustered_proposals": [{"name": "gapkind", "recurrence": 4, "ref_count": 2}],
                "active_type_review": {"active": []},
            },
        },
        "dry_seams": {
            "overall": "warn",
            "status_counts": {},
            "findings": [],
            "completion_event_health": {},
            "outcome_gap_summary": {
                "total_runs_without_outcome": 2,
                "categories": [{"category": "missing", "count": 2, "actionable": 1}],
            },
        },
        "role_activation": {"roles": {}},
        "evidence_approval": {
            "applied": False,
            "name": "t",
            "gap": "g",
            "recurrence": 1,
            "min_recurrence": 3,
            "eligible": False,
            "gaps_marked": 0,
            "evidence_type_status": "proposed",
            "blocked_reason": "below recurrence",
        },
    }


def test_format_human_covers_rail_route_and_gap_branches():
    text = periodic_report.format_human(_human_report_with_remaining_branches())
    assert "rail exercise: tree=checkout" in text
    assert "implement: COLD_START" in text
    assert "note: cold" in text
    assert "note: learned-note" in text
    assert "clustered proposal: gapkind" in text
    assert "outcome gap: missing" in text
    assert "blocked: below recurrence" in text


def test_main_requires_from_gap_and_renders_snapshot_text(monkeypatch, isolated_runtime, capsys):
    with pytest.raises(SystemExit) as missing_gap:
        periodic_report.main(["--approve-evidence-type", "t"])
    assert missing_gap.value.code != 0
    assert "--approve-evidence-type requires --from-gap" in capsys.readouterr().err

    snap = isolated_runtime / "snap.json"
    snap.write_text("{}")
    monkeypatch.setattr(
        periodic_report,
        "build_report_from_snapshot",
        lambda *_a, **_k: {"snapshot_path": str(snap.resolve())},
    )
    monkeypatch.setattr(periodic_report, "format_human", lambda _report: "HUMAN-REPORT")
    assert periodic_report.main(["--snapshot-json", str(snap)]) == 0
    rendered = capsys.readouterr().out
    assert "HUMAN-REPORT" in rendered
    assert f"source: snapshot {snap.resolve()}" in rendered


def test_main_approves_evidence_from_snapshot_and_rejects_live_value_error(
    monkeypatch, isolated_runtime, capsys
):
    snap = isolated_runtime / "snap.json"
    snap.write_text("{}")
    temp_db = isolated_runtime / "snap.db"
    temp_db.write_text("")
    monkeypatch.setattr(
        periodic_report,
        "build_report_from_snapshot",
        lambda *_a, **_k: {"snapshot_path": str(snap), "fleet_six": None},
    )
    monkeypatch.setattr(periodic_report, "_load_snapshot_into_temp_db", lambda *_a, **_k: temp_db)
    monkeypatch.setattr(
        feedback, "approve_evidence_type", lambda *_a, **_k: {"applied": False, "name": "t"}
    )
    assert (
        periodic_report.main(
            [
                "--snapshot-json",
                str(snap),
                "--json",
                "--approve-evidence-type",
                "t",
                "--from-gap",
                "gap",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["evidence_approval"]["name"] == "t"
    assert temp_db.exists() is False

    monkeypatch.setattr(periodic_report, "build_report", lambda **_k: {"window_days": 7})

    def explode(*_a, **_k):
        raise ValueError("not eligible")

    monkeypatch.setattr(feedback, "approve_evidence_type", explode)
    with pytest.raises(SystemExit) as rejected:
        periodic_report.main(["--approve-evidence-type", "t", "--from-gap", "gap"])
    assert rejected.value.code != 0
    assert "not eligible" in capsys.readouterr().err


def _research_plan():
    return {
        "planned": [
            {
                "item": {"target": "o/r#9", "task_type": "implement"},
                "task_type": "implement",
                "arms": ["codex"],
                "pools": [],
            }
        ],
        "skipped": [],
        "producer_cells": [],
    }


def _stub_research_runtime(monkeypatch):
    monkeypatch.setattr(tick.claims, "reap_stale", lambda: None)
    monkeypatch.setattr(tick.claims, "active_claims", lambda: {})
    monkeypatch.setattr(tick.claims, "claim", lambda *_a, **_k: True)
    monkeypatch.setattr(tick.claims, "release", lambda *_a, **_k: None)
    monkeypatch.setattr(tick.claims, "update_metadata", lambda *_a, **_k: None)
    monkeypatch.setattr(tick.experiment_producer, "trial_defaults", lambda **_k: None)
    monkeypatch.setattr(tick.experiment_producer, "trial_verdict", lambda: None)
    monkeypatch.setattr(tick.experiment_producer, "trial_launch_hold", lambda: None)
    monkeypatch.setattr(tick.experiment_producer, "build_plan", lambda *_a, **_k: _research_plan())
    monkeypatch.setattr(tick.research_subjects, "record_event", lambda *_a, **_k: None)
    monkeypatch.setattr(tick.research_subjects, "record_subject", lambda *_a, **_k: None)
    monkeypatch.setattr(tick.research_subjects, "assess_candidate", lambda **_k: {"eligible": True})
    monkeypatch.setattr(
        tick.research_subjects,
        "subject_identity",
        lambda *_a, **_k: {"subject_id": "s1", "subject_family_id": "f1", "base_sha": "id-sha"},
    )
    monkeypatch.setattr(tick, "_fetch_issue_body", lambda *_a, **_k: "spec body")
    monkeypatch.setattr(tick.capabilities, "production_heartbeat", lambda *_a, **_k: None)


def test_research_tick_skips_when_producer_draw_does_not_fire(monkeypatch):
    _stub_research_runtime(monkeypatch)
    monkeypatch.setattr(tick.experiment_producer, "cadence_draw", lambda *_a, **_k: {"fire": False})
    monkeypatch.setenv("ORCH_EXPERIMENT_PRODUCER", "1")
    out = tick.research_tick([], {}, dry_run=False, env=os.environ.copy())
    assert out["planned"][0]["draw"] == {"fire": False}
    assert out["active"] is False
    assert "active_status" not in out["planned"][0]


def test_research_tick_treats_corrupt_meta_base_sha_as_missing(monkeypatch, isolated_runtime):
    _stub_research_runtime(monkeypatch)
    exp_dir = isolated_runtime / "exp"
    exp_dir.mkdir()
    (exp_dir / "meta.json").write_text("{")
    monkeypatch.setattr(tick.exp_abcd, "exp_paths", lambda _exp: exp_dir)
    monkeypatch.setattr(
        tick.research_scheduler, "build_research_plan", lambda *_a, **_k: _research_plan()
    )
    monkeypatch.setenv("ORCH_RESEARCH_ARM", "1")
    out = tick.research_tick(
        [], {}, dry_run=False, env=os.environ.copy(), prepare_fn=lambda *_a, **_k: {}
    )
    assert out["planned"][0]["active_status"] == "launched"
    assert out["planned"][0]["base_sha"] == "id-sha"
    assert out["launched"] == [{}]


def test_research_tick_records_frozen_test_launch_failure(monkeypatch):
    _stub_research_runtime(monkeypatch)
    monkeypatch.setattr(tick.experiment_producer, "cadence_draw", lambda *_a, **_k: {"fire": True})
    monkeypatch.setattr(tick.experiment_producer, "record_skip", lambda *_a, **_k: None)
    monkeypatch.setattr(tick.experiment_producer, "test_class", lambda _spec: "unit")
    monkeypatch.setattr(tick.experiment_producer, "issue_status", lambda *_a, **_k: {"ok": True})
    monkeypatch.setenv("ORCH_EXPERIMENT_PRODUCER", "1")

    def prepare(*_a, **_k):
        raise tick.experiment_producer.FrozenTestError("frozen tests")

    out = tick.research_tick([], {}, dry_run=False, env=os.environ.copy(), prepare_fn=prepare)
    assert out["planned"][0]["active_status"] == "failed"
    assert out["planned"][0]["error"] == "frozen tests"
    assert out["active"] is False


def test_reseed_ignores_non_dict_contraindication_identity():
    merged = repo_knowledge._reseed_section(
        ["not-a-dict"],
        [{"capability": "cap-a", "reason": "new"}],
        section=repo_knowledge.CONTRAINDICATION_SECTION,
    )
    assert merged == ["not-a-dict", {"capability": "cap-a", "reason": "new"}]


def test_migrate_replaces_superseded_string_gotcha():
    reg = {
        "schema_version": 1,
        "repos": {
            "stranske/Trend_Model_Project": {
                "summary": "instance summary",
                "gotchas": ["Use phase-3 as the base for opener work; do not assume main."],
            }
        },
    }
    assert repo_knowledge._migrate(reg) is True
    gotchas = reg["repos"]["stranske/Trend_Model_Project"]["gotchas"]
    assert gotchas[0]["text"] == repo_knowledge._TREND_BASE_GOTCHA
    assert reg["schema_version"] == repo_knowledge.SEED_SCHEMA_VERSION


def test_load_keeps_migrated_registry_when_save_fails(monkeypatch, isolated_runtime):
    path = isolated_runtime / "registry.json"
    path.write_text(json.dumps({"schema_version": 1, "repos": {}}))

    def fail_save(*_a, **_k):
        raise OSError("read-only")

    monkeypatch.setattr(repo_knowledge, "save", fail_save)
    loaded = repo_knowledge.load(path)
    assert loaded["schema_version"] == repo_knowledge.SEED_SCHEMA_VERSION
    assert "stranske/Workflows" in loaded["repos"]


def test_playbook_filters_and_empty_context(isolated_runtime):
    reg = isolated_runtime / "registry.json"
    repo_knowledge.save(
        {
            "schema_version": repo_knowledge.SEED_SCHEMA_VERSION,
            "repos": {
                "o/r": {
                    repo_knowledge.CONTRAINDICATION_SECTION: [
                        "nope",
                        {"capability": "", "reason": "missing"},
                        {"capability": "offload", "reason": "breaks", "instead": "stay local"},
                    ]
                },
                "o/empty": {"summary": "   "},
            },
        },
        reg,
    )
    assert list(repo_knowledge.contraindications_for("o/r", path=reg)) == ["offload"]
    assert repo_knowledge._applies({"lanes": ["opener"]}, task_type=None, lane="closer") is False
    lines = repo_knowledge._contraindication_lines((repo_knowledge.load(reg)["repos"]["o/r"]))
    assert lines == ["offload: breaks Instead: stay local"]
    assert repo_knowledge.context_for("o/empty", path=reg) == ""
    records = repo_knowledge._playbook_memory_records("o/r", task_type=None, lane=None, path=reg)
    assert any(row["section"] == repo_knowledge.CONTRAINDICATION_SECTION for row in records)


def test_export_limit_and_agents_update_edges(monkeypatch, isolated_runtime):
    lines = repo_knowledge._export_section_lines(
        "Gotchas", [{"text": f"rule {i}"} for i in range(3)], max_items=1
    )
    assert "2 additional approved rule" in lines[-1]
    assert repo_knowledge._limit_lines(["a", "b", "c", "d"], 2) == ["a", "b"]
    assert repo_knowledge._replace_managed_section("keep", "") == ("keep", False)

    missing = isolated_runtime / "missing-repo"
    with pytest.raises(FileNotFoundError, match="repo path not found"):
        repo_knowledge.update_agents_md(missing, repo="o/r")

    root = isolated_runtime / "repo"
    root.mkdir()
    monkeypatch.setattr(repo_knowledge, "_repo_from_git_remote", lambda _root: "")
    with pytest.raises(ValueError, match="--repo is required"):
        repo_knowledge.update_agents_md(root)

    reg = isolated_runtime / "registry.json"
    repo_knowledge.save({"schema_version": repo_knowledge.SEED_SCHEMA_VERSION, "repos": {}}, reg)
    preview = repo_knowledge.update_agents_md(root, repo="o/missing", path=reg)
    assert preview["changed"] is False
    assert preview["reason"] == "no approved repo playbook entry"


def test_validate_agents_and_current_refs(monkeypatch, isolated_runtime):
    root = isolated_runtime / "repo"
    root.mkdir()
    reg = isolated_runtime / "registry.json"
    monkeypatch.setattr(repo_knowledge, "_repo_from_git_remote", lambda _root: "")
    with pytest.raises(ValueError, match="--repo is required"):
        repo_knowledge.validate_agents_md_export(root, path=reg)

    repo_knowledge.save(
        {"schema_version": repo_knowledge.SEED_SCHEMA_VERSION, "repos": {}},
        reg,
    )
    absent = repo_knowledge.validate_agents_md_export(root, repo="o/empty", path=reg)
    assert absent["status"] == "absent"
    assert absent["errors"] == ["AGENTS.md not found"]

    (root / "AGENTS.md").write_text(
        f"{repo_knowledge.AGENTS_EXPORT_START}\nold\n{repo_knowledge.AGENTS_EXPORT_END}\n"
    )
    stale = repo_knowledge.validate_agents_md_export(root, repo="o/empty", path=reg)
    assert any("no approved playbook" in warning for warning in stale["warnings"])

    repo_knowledge.save(
        {
            "schema_version": repo_knowledge.SEED_SCHEMA_VERSION,
            "repos": {
                "o/r": {
                    "summary": "See `https://example.com/a` and `docs/missing.md`",
                    "validation": [
                        {
                            "text": "check",
                            "rule_id": "r1",
                            "current_refs": [{"path": "nope.md"}],
                        }
                    ],
                }
            },
        },
        reg,
    )
    (root / "AGENTS.md").unlink()
    missing = repo_knowledge.validate_agents_md_export(root, repo="o/r", path=reg)
    assert "AGENTS.md not found" in missing["errors"]
    assert "managed Orchestrator section missing" in missing["errors"]
    assert any("nope.md" in warning for warning in missing["warnings"])
    assert any("docs/missing.md" in warning for warning in missing["warnings"])

    not_dir = isolated_runtime / "file.txt"
    not_dir.write_text("x")
    assert repo_knowledge.validate_current_refs(not_dir, [{"path": "a"}])["errors"] == [
        "repo root not found"
    ]
    outside = isolated_runtime / "outside.txt"
    outside.write_text("secret")
    (root / "link.md").symlink_to(outside)
    escaped = repo_knowledge.validate_current_refs(root, [{"path": "link.md"}])
    assert any(error.startswith("unsafe current path:") for error in escaped["errors"])

    readme = root / "README.md"
    readme.write_text("def symbol():\n    pass\n")
    real_read = Path.read_text

    def unreadable(self, *args, **kwargs):
        if self == readme:
            raise OSError("unreadable")
        return real_read(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", unreadable)
    unread = repo_knowledge.validate_current_refs(root, [{"path": "README.md", "symbol": "symbol"}])
    assert unread["errors"] == ["unreadable current path: README.md"]


def test_suggestion_merge_memory_and_rule_edges(monkeypatch, isolated_runtime):
    assert repo_knowledge._token_similarity("a", "b") == 0.0
    target = {"evidence": "old"}
    repo_knowledge._merge_field(
        target, {"evidence": "new"}, field="evidence", list_field="evidences", joiner="; "
    )
    assert target["evidences"] == ["old", "new"]
    assert target["evidence"] == "old; new"

    suggestions: list[dict] = []
    seen: set[str] = set()
    repo_knowledge._append_suggestion(
        suggestions, seen, set(), {"candidate_text": "too short"}, max_per_repo=1
    )
    assert suggestions == []
    long = "Use the documented postgres migration path for this repository."
    repo_knowledge._append_suggestion(
        suggestions, seen, set(), {"candidate_text": long, "evidence": "e1"}, max_per_repo=1
    )
    repo_knowledge._append_suggestion(
        suggestions,
        seen,
        set(),
        {"candidate_text": "Another distinct suggestion that should be dropped at the cap."},
        max_per_repo=1,
    )
    assert len(suggestions) == 1
    assert repo_knowledge._clean_doc_line("```python") == ""
    assert repo_knowledge._clean_doc_line("| table |") == ""

    reg = isolated_runtime / "registry.json"
    repo_knowledge.save(
        {
            "schema_version": repo_knowledge.SEED_SCHEMA_VERSION,
            "repos": {
                "o/r": {
                    "summary": "Repo summary text",
                    "gotchas": [
                        {"text": ""},
                        {"text": "same meaning", "content_hash": "sha256:abc"},
                    ],
                }
            },
        },
        reg,
    )
    assert repo_knowledge.managed_rule_duplicate("o/r", "unused", "sha256:nope", path=reg) is None
    hashed = repo_knowledge.managed_rule_duplicate("o/r", "different words", "sha256:abc", path=reg)
    assert hashed["reason"] == "duplicate content hash"
    with pytest.raises(ValueError, match="invalid managed rule section"):
        repo_knowledge.install_managed_rule(
            {"repo": "o/r", "section": "not-a-section", "text": "x", "content_hash": "h"},
            path=reg,
        )

    bundle = isolated_runtime / "bundle.json"
    bundle.write_text("[]")
    with pytest.raises(ValueError, match="JSON object"):
        repo_knowledge.update_capability_bundle(bundle, {"rule_id": "r"})
    bundle.write_text(json.dumps({"orchestrator_repo_playbook_rules": []}))
    with pytest.raises(ValueError, match="playbook rules must be an object"):
        repo_knowledge.update_capability_bundle(bundle, {"rule_id": "r"})

    assert repo_knowledge._memory_match(set(), "")["score"] == 0.0
    assert repo_knowledge._memory_match(set(), "text", base=0.2)["score"] == 0.2
    assert (
        repo_knowledge._memory_item_visible({"lanes": ["opener"]}, task_type=None, lane="closer")
        is False
    )
    assert (
        repo_knowledge._memory_scope_match(
            {"task_type": "review"}, task_type="implement", lane=None
        )
        is False
    )
    assert (
        repo_knowledge._memory_scope_match({"lane": "opener"}, task_type=None, lane="closer")
        is False
    )

    skipped = repo_knowledge._run_memory_from_snapshot(
        {
            "runs": [
                {"run_id": "other", "target": "other/repo#1", "task_type": "review"},
                {"run_id": "blank", "target": "o/r#2", "task_type": "implement"},
                {"run_id": "boom", "target": "boom#3", "task_type": "implement"},
            ],
            "outcomes": [
                {"run_id": "missing", "notes": "This outcome note is long enough to keep."},
                {"run_id": "other", "notes": "This outcome note is long enough to keep."},
                {"run_id": "blank", "notes": "   "},
                {"run_id": "boom", "notes": "This outcome note is long enough to keep."},
            ],
        },
        "o/r",
    )
    assert skipped == []

    def flaky(target):
        if str(target).startswith("boom"):
            raise ValueError("bad target")
        return repo_knowledge.repo_for(str(target or "").split()[0])

    monkeypatch.setattr(repo_knowledge, "_repo_for_suggestion_target", flaky)
    raised = repo_knowledge._run_memory_from_snapshot(
        {
            "runs": [{"run_id": "boom", "target": "boom#3", "task_type": "implement"}],
            "outcomes": [{"run_id": "boom", "notes": "This outcome note is long enough to keep."}],
        },
        "o/r",
    )
    assert raised == []

    found = repo_knowledge.search_repo_memory(
        "o/r",
        task_type="implement",
        path=reg,
        snapshot={
            "runs": [{"run_id": "rev", "target": "o/r#1", "task_type": "review"}],
            "outcomes": [
                {
                    "run_id": "rev",
                    "notes": "Review-only note that must stay out of implement search.",
                }
            ],
        },
    )
    assert all(row.get("task_type") != "review" for row in found)

    with pytest.raises(ValueError, match="suggestion repo is required"):
        repo_knowledge.approve_suggestion({}, path=reg)
    with pytest.raises(ValueError, match="candidate_text is required"):
        repo_knowledge.approve_suggestion({"repo": "o/r", "candidate_text": ""}, path=reg)
    with pytest.raises(ValueError, match="section must be one of"):
        repo_knowledge.approve_suggestion(
            {
                "repo": "o/r",
                "candidate_text": "Use the documented postgres migration path for this repository.",
            },
            section="nope",
            path=reg,
        )

    suggestions_path = isolated_runtime / "suggestions.json"
    suggestions_path.write_text('{"not": "a list"}')
    with pytest.raises(ValueError, match="JSON list"):
        repo_knowledge._load_suggestion_file(suggestions_path, 0)
    suggestions_path.write_text("[]")
    with pytest.raises(IndexError, match="out of range"):
        repo_knowledge._load_suggestion_file(suggestions_path, 0)
    suggestions_path.write_text(json.dumps([{"repo": "o/r"}]))
    assert repo_knowledge._load_suggestion_file(suggestions_path, 0)["repo"] == "o/r"


def test_iter_doc_files_honors_caps_and_skip_rules(isolated_runtime):
    root = isolated_runtime / "docs-root"
    root.mkdir()
    (root / "README.md").write_text("readme")
    (root / "notes.md").write_text("notes")
    (root / "BRIEF_skip.md").write_text("skip")
    (root / "image.png").write_text("no")
    docs = root / "docs"
    docs.mkdir()
    (docs / "a.md").write_text("a")
    (docs / "BRIEF_x.md").write_text("b")
    skipped = docs / "node_modules"
    skipped.mkdir()
    (skipped / "c.md").write_text("c")

    capped = repo_knowledge._iter_doc_files(root, max_files=1, include_root_docs=True)
    assert [path.name for path in capped] == ["README.md"]
    names = {
        path.name
        for path in repo_knowledge._iter_doc_files(root, max_files=100, include_root_docs=True)
    }
    assert "README.md" in names and "notes.md" in names and "a.md" in names
    assert "BRIEF_skip.md" not in names
    assert "BRIEF_x.md" not in names
    assert "c.md" not in names
    assert "image.png" not in names
    stopped = repo_knowledge._iter_doc_files(root, max_files=2, include_root_docs=True)
    assert [path.name for path in stopped] == ["README.md", "notes.md"]


def test_main_covers_suggestion_success_and_context_usage(monkeypatch, isolated_runtime, capsys):
    monkeypatch.setattr(
        repo_knowledge,
        "suggest_from_review_json",
        lambda *_a, **kwargs: {"max": kwargs["max_per_repo"]},
    )
    assert (
        repo_knowledge.main(
            ["--suggest-from-review-json", "comments.json", "--repo", "o/r", "--max", "3"]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out) == {"max": 3}

    monkeypatch.setattr(repo_knowledge, "suggest_from_pr_comments", lambda *_a, **_k: {"pr": True})
    assert repo_knowledge.main(["--suggest-from-pr", "o/r#4", "--max", "2"]) == 0
    assert json.loads(capsys.readouterr().out) == {"pr": True}

    suggestion = isolated_runtime / "suggestions.json"
    suggestion.write_text(
        json.dumps(
            [
                {
                    "repo": "o/r",
                    "candidate_text": "Use the documented postgres migration path for this repository.",
                }
            ]
        )
    )
    monkeypatch.setattr(repo_knowledge, "approve_suggestion", lambda *_a, **_k: {"applied": False})
    assert repo_knowledge.main(["--approve-suggestion", str(suggestion), "--index", "0"]) == 0
    assert json.loads(capsys.readouterr().out)["applied"] is False

    assert repo_knowledge.main([]) == 2
    assert "usage:" in capsys.readouterr().err
    monkeypatch.setattr(repo_knowledge, "context_for", lambda *_a, **_k: "PLAYBOOK")
    assert repo_knowledge.main(["o/r", "implement", "opener"]) == 0
    assert capsys.readouterr().out.strip() == "PLAYBOOK"


def _empty_registry(path):
    repo_knowledge.save(
        {"schema_version": repo_knowledge.SEED_SCHEMA_VERSION, "repos": {}},
        path,
    )


def test_build_report_treats_corrupt_pattern_artifacts_as_empty(monkeypatch, isolated_runtime):
    import adapters

    state = isolated_runtime / "state"
    state.mkdir()
    (state / "pattern-miner-status.json").write_text("{not-json")
    (state / "pattern-miner-inventory.json").write_text("{also-not")
    spawned = []

    def runner(argv, *args, **kwargs):
        spawned.append(list(argv) if isinstance(argv, (list, tuple)) else [str(argv)])
        return subprocess.CompletedProcess(argv, 127, "", "not started")

    monkeypatch.setattr(subprocess, "run", runner)
    monkeypatch.setattr(adapters, "advertised_models", lambda *_a, **_k: [])
    report = periodic_report.build_report(window_days=1, probe_langsmith_artifacts=False)
    miner = report["pattern_miner"]
    assert miner["status"] == {}
    assert miner["inventory"]["emitted_candidate_count"] == 0
    assert miner["inventory"]["expired_candidate_count"] == 0
    assert miner["inventory"]["tombstone_count"] == 0
    assert spawned, "catalog probes must hit the stub"
    assert all(
        cmd and cmd[0] in {"codex", "claude", "cursor-agent", "npx", "agy"} for cmd in spawned
    )


def test_managed_rule_duplicate_flags_fuzzy_playbook_meaning(isolated_runtime):
    reg = isolated_runtime / "registry.json"
    existing = "Always run the documented postgres migration before deploying this service."
    repo_knowledge.save(
        {
            "schema_version": repo_knowledge.SEED_SCHEMA_VERSION,
            "repos": {
                "o/r": {
                    "gotchas": [{"text": existing, "content_hash": "sha256:existing"}],
                }
            },
        },
        reg,
    )
    queried = "Always run the documented postgres migration before deploying the service."
    assert queried.lower() != existing.lower()
    found = repo_knowledge.managed_rule_duplicate("o/r", queried, "sha256:other", path=reg)
    assert found["reason"] == "duplicate playbook meaning"
    assert found["section"] == "gotchas"
    assert found["item"]["text"] == existing


def test_repo_from_git_remote_parses_github_and_rejects_other_remotes(
    monkeypatch, isolated_runtime
):
    root = isolated_runtime / "checkout"
    root.mkdir()
    scripts = iter(
        [
            "git@github.com:octo/widgets.git\n",
            "https://github.com/octo/widgets\n",
            "\n",
            "https://gitlab.com/octo/widgets.git\n",
        ]
    )
    seen = []

    def runner(argv, **kwargs):
        seen.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, next(scripts), "")

    monkeypatch.setattr(subprocess, "run", runner)
    assert repo_knowledge._repo_from_git_remote(root) == "octo/widgets"
    assert repo_knowledge._repo_from_git_remote(root) == "octo/widgets"
    assert repo_knowledge._repo_from_git_remote(root) == ""
    assert repo_knowledge._repo_from_git_remote(root) == ""
    assert [cmd[:3] for cmd in seen] == [["git", "-C", str(root)]] * 4
    assert all(cmd[3:] == ["config", "--get", "remote.origin.url"] for cmd in seen)


def test_suggest_from_docs_requires_directory_and_repo(monkeypatch, isolated_runtime):
    with pytest.raises(FileNotFoundError, match="repo docs root not found"):
        repo_knowledge.suggest_from_docs(isolated_runtime / "missing-docs", repo="o/r")

    root = isolated_runtime / "empty-docs"
    root.mkdir()

    def runner(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, "", "no origin")

    monkeypatch.setattr(subprocess, "run", runner)
    with pytest.raises(ValueError, match="--repo is required"):
        repo_knowledge.suggest_from_docs(root)


def test_suggest_from_docs_skips_unreadable_empty_blocks_and_stops_at_cap(
    monkeypatch, isolated_runtime
):
    reg = isolated_runtime / "registry.json"
    _empty_registry(reg)
    root = isolated_runtime / "docs-repo"
    docs = root / "docs"
    docs.mkdir(parents=True)
    secret = docs / "a-secret.md"
    secret.write_text("Agents must not be read.\n")
    notes = docs / "b-notes.md"
    notes.write_text(
        "\n".join(
            [
                "- **",
                "- Agents must record the postgres migration before deploy.",
                "- Operators must never skip the rollback note before release.",
            ]
        )
        + "\n"
    )
    real_read = Path.read_text

    def unreadable(self, *args, **kwargs):
        if self == secret:
            raise OSError("unreadable")
        return real_read(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", unreadable)
    found = repo_knowledge.suggest_from_docs(root, repo="o/r", path=reg, max_per_repo=1)
    assert len(found) == 1
    assert "postgres migration" in found[0]["candidate_text"]
    assert "rollback" not in found[0]["candidate_text"]
    assert found[0]["evidence"].startswith("docs/b-notes.md:")


def test_review_payload_skips_noise_and_honors_the_cap(isolated_runtime):
    reg = isolated_runtime / "registry.json"
    _empty_registry(reg)
    payload = [
        "not-a-record",
        {"body": "Thanks for taking a look at the patch.", "path": "noise.py"},
        {
            "body": "Agents must record the postgres migration before deploy.",
            "path": "migrate.py",
            "html_url": "https://example.test/1",
        },
        {"body": "Operators must never skip the rollback note before release.", "path": "b.py"},
    ]
    found = repo_knowledge.suggest_from_review_payload(
        payload, repo="o/r", path=reg, max_per_repo=1
    )
    assert len(found) == 1
    assert "postgres migration" in found[0]["candidate_text"]
    assert found[0]["evidence"] == "migrate.py https://example.test/1"
    assert found[0]["author"] is None


def test_suggest_from_pr_comments_reports_bad_targets_and_payloads(isolated_runtime):
    reg = isolated_runtime / "registry.json"
    _empty_registry(reg)
    calls = []

    def missing_cli(argv, **kwargs):
        calls.append(list(argv))
        raise FileNotFoundError("gh")

    with pytest.raises(ValueError, match="owner/repo#N"):
        repo_knowledge.suggest_from_pr_comments("o/r", path=reg, runner=missing_cli)
    assert calls == []
    with pytest.raises(RuntimeError, match="gh CLI"):
        repo_knowledge.suggest_from_pr_comments("o/r#4", path=reg, runner=missing_cli)
    assert calls and calls[0][:2] == ["gh", "api"]

    def empty_stdout(argv, **kwargs):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, "  ", "")

    calls.clear()
    assert repo_knowledge.suggest_from_pr_comments("o/r#4", path=reg, runner=empty_stdout) == []
    assert len(calls) == 3

    def bad_json(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, "{", "")

    assert repo_knowledge.suggest_from_pr_comments("o/r#4", path=reg, runner=bad_json) == []


def test_suggest_from_snapshot_skips_outcomes_that_cannot_become_rules(
    monkeypatch, isolated_runtime
):
    reg = isolated_runtime / "registry.json"
    _empty_registry(reg)
    real = repo_knowledge._repo_for_suggestion_target

    def flaky(target):
        if str(target).startswith("bad"):
            raise ValueError("bad target")
        return real(target)

    monkeypatch.setattr(repo_knowledge, "_repo_for_suggestion_target", flaky)
    signal = "Agents must record the postgres migration before deploying."
    snapshot = isolated_runtime / "snap.json"
    snapshot.write_text(
        json.dumps(
            {
                "runs": [
                    {"run_id": "keep", "target": "o/r#1", "task_type": "implement"},
                    {"run_id": "blank", "target": "o/r#2", "task_type": "implement"},
                    {"run_id": "prose", "target": "o/r#3", "task_type": "implement"},
                    {"run_id": "bad", "target": "bad#4", "task_type": "implement"},
                ],
                "outcomes": [
                    {"run_id": "missing", "notes": signal, "adjudicated_verdict": "FAIL"},
                    {"run_id": "blank", "notes": "   ", "adjudicated_verdict": "FAIL"},
                    {
                        "run_id": "prose",
                        "notes": "The afternoon review finished on time and the notes stayed short.",
                        "adjudicated_verdict": "PASS",
                        "durability": "durable",
                    },
                    {"run_id": "bad", "notes": signal, "adjudicated_verdict": "FAIL"},
                    {"run_id": "keep", "notes": signal, "adjudicated_verdict": "FAIL"},
                ],
            }
        )
    )
    found = repo_knowledge.suggest_from_snapshot(snapshot, path=reg, max_per_repo=5)
    assert [row["run_id"] for row in found] == ["keep"]
    assert found[0]["repo"] == "o/r"
    assert found[0]["suggested_section"] == "gotchas"


def test_memory_score_is_zero_when_text_has_no_tokens():
    assert repo_knowledge._memory_match({"postgres"}, "???") == {
        "score": 0.0,
        "matched_terms": [],
        "coverage": 0.0,
    }
    score = repo_knowledge._score_memory_text(
        {"postgres", "migration"},
        "postgres migration must be documented before deploy",
    )
    assert score > 0


def test_feedback_memory_keeps_matching_notes_and_swallows_query_errors(monkeypatch):
    feedback.record_run("keep", "o/r#1", "implement", "codex")
    feedback.record_outcome(
        "keep",
        adjudicated_verdict="FAIL",
        notes="Migration must be recorded before deploy.",
    )
    feedback.record_run("other", "other/repo#2", "implement", "codex")
    feedback.record_outcome(
        "other",
        notes="This other repo note must stay out of the result.",
    )
    feedback.record_run("bad", "bad#3", "implement", "codex")
    feedback.record_outcome(
        "bad",
        notes="Agents must not keep a note whose target cannot be parsed.",
    )
    real = repo_knowledge._repo_for_suggestion_target

    def flaky(target):
        if str(target).startswith("bad"):
            raise ValueError("bad target")
        return real(target)

    monkeypatch.setattr(repo_knowledge, "_repo_for_suggestion_target", flaky)
    rows = repo_knowledge._run_memory_from_feedback("o/r")
    assert [row["run_id"] for row in rows] == ["keep"]
    assert rows[0]["repo"] == "o/r"
    assert "Migration must be recorded" in rows[0]["text"]

    def broken(*_args, **_kwargs):
        raise RuntimeError("brain unreadable")

    monkeypatch.setattr(feedback, "_conn", broken)
    assert repo_knowledge._run_memory_from_feedback("o/r") == []
