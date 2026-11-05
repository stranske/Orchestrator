"""Hermetic coverage for coverage-autopilot round 1138 (lane C targets)."""

from __future__ import annotations

import argparse
import importlib
import json
import os
import socket
import subprocess
import sys
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
    driver.write_text("import foo\nfoo.bar(\n")
    monkeypatch.setattr(audit, "DRIVER_MODULES", ("broken_driver.py",))
    monkeypatch.setattr(audit, "_driver_path", lambda name: driver)
    assert audit._callers_of("foo", {"run"}) == []


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
    assert "implementation_arms" in summary


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
