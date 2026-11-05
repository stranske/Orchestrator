"""Ranked coverage chunk for Orchestrator #1169 (synthesis_promotion, codemod_lane)."""

from __future__ import annotations

import copy
import json
import os
import sqlite3
import subprocess
from pathlib import Path

import pytest

import capabilities
import codemod_lane as lane
import feedback
import synthesis_promotion as promotion


@pytest.fixture(autouse=True)
def isolated_runtime(tmp_path, monkeypatch):
    state = tmp_path / "state"
    runtime = tmp_path / "runtime"
    state.mkdir()
    runtime.mkdir()
    monkeypatch.setenv("ORCH_STATE_DIR", str(state))
    monkeypatch.setenv("ORCH_LOCAL_RUNTIME", str(runtime))
    monkeypatch.delenv("ORCH_FEEDBACK_DB", raising=False)
    monkeypatch.delenv("ORCH_CAPABILITIES_PATH", raising=False)
    db = runtime / "feedback" / "orchestrator.db"
    db.parent.mkdir(parents=True)
    monkeypatch.setattr(feedback, "LOCAL_RUNTIME", runtime)
    monkeypatch.setattr(feedback, "DB_PATH", db)
    monkeypatch.setattr(capabilities, "LOCAL_RUNTIME", runtime)
    monkeypatch.setattr(capabilities, "REG", runtime / "capabilities.json")
    started = []

    def forbid(*args, **kwargs):
        started.append((args, kwargs))
        raise AssertionError("coverage tests must not start external work")

    monkeypatch.setattr("subprocess.run", forbid)
    monkeypatch.setattr("subprocess.Popen", forbid)
    yield
    assert started == []


def _completed(
    rc: int = 0, stdout: str = "", stderr: str = "", *, returncode: int | None = None
) -> subprocess.CompletedProcess:
    code = returncode if returncode is not None else rc
    return subprocess.CompletedProcess([], code, stdout, stderr)


def _valid_campaign() -> dict:
    return json.loads(json.dumps(lane.CAMPAIGN_SCHEMA_EXAMPLE))


def _evaluated_state(tmp_path: Path) -> dict:
    exp = tmp_path / "exp-a"
    exp.mkdir()
    (exp / "meta.json").write_text(
        json.dumps(
            {
                "repo": "owner/repo",
                "base": "main",
                "base_sha": "abc",
                "agents": ["codex"],
                "exp_id": "exp-a",
            }
        )
    )
    return promotion.ensure_evaluated_state(exp, now=100)


# --- codemod_lane ---


def test_build_authoring_prompt_rejects_empty_goal_and_reads_context_file(tmp_path) -> None:
    with pytest.raises(ValueError, match="goal must be non-empty"):
        lane.build_authoring_prompt(goal="   ")
    ctx = tmp_path / "ctx.md"
    ctx.write_text("from audit")
    prompt = lane.build_authoring_prompt(goal="rename symbols", context_file=ctx)
    assert "from audit" in prompt
    assert "rename symbols" in prompt


def test_validate_campaign_reports_structural_errors() -> None:
    assert lane.validate_campaign([]) == ["campaign document must be a JSON object"]
    base = _valid_campaign()
    bad = copy.deepcopy(base)
    bad["campaign"]["id"] = "Bad Slug"
    assert any("lowercase slug" in e for e in lane.validate_campaign(bad))
    bad = copy.deepcopy(base)
    bad["recipe"]["tool"] = "sed"
    assert any("recipe.tool must be one of" in e for e in lane.validate_campaign(bad))
    bad = copy.deepcopy(base)
    bad["recipe"]["match"] = 123
    assert any("recipe.match must be a string" in e for e in lane.validate_campaign(bad))
    bad = copy.deepcopy(base)
    bad["rollout"]["batch_size"] = 0
    assert any("rollout.batch_size must be >= 1" in e for e in lane.validate_campaign(bad))
    bad = copy.deepcopy(base)
    bad["acceptance"] = []
    assert any("acceptance must be a non-empty list" in e for e in lane.validate_campaign(bad))


def test_gitignore_add_only_contract_errors() -> None:
    campaign = json.loads(
        (
            Path(__file__).resolve().parents[1] / "campaigns/gitignore-caches-2026-10.json"
        ).read_text()
    )
    broken = copy.deepcopy(campaign)
    broken["ignore_entries"] = list(lane.IGNORE_ENTRIES) + ["extra/"]
    assert any("exactly the five" in e for e in lane.validate_campaign(broken))
    broken = copy.deepcopy(campaign)
    broken["scope"]["include_globs"] = ["**/*"]
    assert any("only .gitignore" in e for e in lane.validate_campaign(broken))
    broken = copy.deepcopy(campaign)
    broken["delegate_prompt"] = "Append lines safely."
    assert any("only missing lines" in e for e in lane.validate_campaign(broken))


def test_parse_campaign_json_and_delegation_warnings() -> None:
    assert lane.parse_campaign_json('```json\n{"x": 1}\n```') == {"x": 1}
    with pytest.raises(ValueError, match="must decode to an object"):
        lane.parse_campaign_json("[1]")
    valid = _valid_campaign()
    plan = lane.build_dry_run_plan(valid)
    plan["warnings"] = ["preview omitted"]
    text = lane.build_delegation_prompt(valid, plan)
    assert "Plan warnings:" in text and "preview omitted" in text


def test_recipe_dry_run_branches() -> None:
    scope = {"include_globs": ["**/*.py"], "exclude_globs": ["tests/**"]}
    jsc, _ = lane._recipe_dry_run_command(
        {
            "tool": "jscodeshift",
            "rule_file": "codemod.js",
            "language": "javascript",
            "summary": "x",
            "risk_level": "low",
        },
        scope,
    )
    assert jsc and "jscodeshift" in jsc and "--dry" in jsc
    openrewrite, _ = lane._recipe_dry_run_command(
        {
            "tool": "openrewrite",
            "rule_file": "com.example.Fix",
            "language": "java",
            "summary": "x",
            "risk_level": "low",
        },
        scope,
    )
    assert openrewrite and "rewrite:dryRun" in openrewrite
    unsafe, warn = lane._recipe_dry_run_command(
        {
            "tool": "custom",
            "command_template": "tool --apply src/",
            "language": "python",
            "summary": "x",
            "risk_level": "high",
        },
        scope,
    )
    assert unsafe is None and warn
    assert lane._template_is_safe_dry_run("bad ' quote") is False


def test_campaign_measure_target_mismatch_and_sqlite_error(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = feedback.DB_PATH
    with sqlite3.connect(db) as conn:
        conn.executescript(feedback.SCHEMA)
        conn.execute("INSERT INTO runs(run_id,target,pr_number) VALUES ('r','owner/repo#91',90)")
        conn.execute("INSERT INTO outcomes(run_id,merged,durability) VALUES ('r',1,'durable')")
        conn.execute("INSERT INTO costs(run_id,cost_usd,source) VALUES ('r',1.0,'ccusage')")
    result = lane.campaign_measure("owner/repo#7", [{"number": 91}])
    assert "target/number mismatch" in result["measurement"]

    def broken_connect(*_a, **_k):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(sqlite3, "connect", broken_connect)
    assert lane.campaign_measure("owner/repo#7", [])["measurement"].startswith("UNKNOWN:")


def test_campaign_backlog_validates_program_and_builds_items(tmp_path, monkeypatch) -> None:
    campaign = json.loads(
        (
            Path(__file__).resolve().parents[1] / "campaigns/gitignore-caches-2026-10.json"
        ).read_text()
    )
    program = {
        "campaign_id": campaign["campaign"]["id"],
        "repos": {
            r: {
                "target": f"{r}#42",
                "state": "filed",
                "merged": None,
                "durable": None,
                "cost_usd": None,
            }
            for r in campaign["campaign"]["repos"]
        },
    }
    lane._write_program(lane.campaign_program_path(campaign), program)
    marker = f"<!-- codemod-campaign:{campaign['campaign']['id']} -->"

    def gh(args):
        if args[:2] == ["issue", "view"]:
            return {
                "number": 42,
                "state": "OPEN",
                "title": "Add ignores",
                "body": marker + "\nbody",
                "labels": [],
            }
        if args[:2] == ["pr", "list"]:
            return []
        raise AssertionError(args)

    import backlog

    monkeypatch.setattr(backlog, "load_scoped_blockers", lambda: {})
    backlog_out = lane.campaign_backlog(campaign, gh=gh)
    assert backlog_out["items"] and backlog_out["items"][0]["target"].endswith("#42")

    with pytest.raises(ValueError, match="invalid campaign"):
        lane.campaign_backlog({"campaign": {"id": "x"}}, gh=gh)


def test_codemod_main_cli_paths(tmp_path, monkeypatch, capsys) -> None:
    campaign_path = tmp_path / "c.json"
    campaign_path.write_text(json.dumps(_valid_campaign()))
    assert lane.main(["--validate", str(campaign_path), "--json"]) == 0
    assert "valid" in capsys.readouterr().out
    assert lane.main(["--plan", str(campaign_path), "--json", "--emit-delegation-prompt"]) == 0
    out = capsys.readouterr().out
    assert "delegation_prompt" in out
    assert lane.main(["--goal", "rename handler", "--json"]) == 0
    goal_file = tmp_path / "goal.txt"
    goal_file.write_text("from file")
    assert lane.main(["--goal-file", str(goal_file)]) == 0
    bad = tmp_path / "bad.json"
    bad.write_text("{")
    assert lane.main(["--validate", str(bad)]) == 2


# --- synthesis_promotion ---


def test_validate_state_rejects_corrupt_promotions(tmp_path) -> None:
    state = _evaluated_state(tmp_path)
    for mutator, match in [
        (lambda s: s.update(schema_version=9), "unsupported synthesis-promotion schema"),
        (lambda s: s.update(delivery_phase="bogus"), "invalid synthesis delivery phase"),
        (
            lambda s: s.update(canonical_state="active"),
            "phase/canonical state mismatch",
        ),
        (
            lambda s: s.update(publication={"direct_publication_allowed": True}),
            "must prohibit direct publication",
        ),
        (
            lambda s: s.__setitem__(
                "phase_history",
                [{"event_id": "dup"}, {"event_id": "dup"}],
            ),
            "phase history is not exactly-once",
        ),
    ]:
        broken = copy.deepcopy(state)
        mutator(broken)
        with pytest.raises(ValueError, match=match):
            promotion.validate_state(broken)


def test_ensure_evaluated_state_is_idempotent(tmp_path) -> None:
    exp = tmp_path / "exp-b"
    exp.mkdir()
    (exp / "meta.json").write_text(json.dumps({"exp_id": "exp-b", "repo": "o/r", "agents": []}))
    first = promotion.ensure_evaluated_state(exp, now=50)
    second = promotion.ensure_evaluated_state(exp, now=999)
    assert second["experiment_id"] == first["experiment_id"]
    assert second["created_ts"] == first["created_ts"]


def test_accepted_source_influences_reads_brain() -> None:
    db = feedback.DB_PATH
    with sqlite3.connect(db) as conn:
        conn.executescript(feedback.SCHEMA)
    feedback.record_completion_event(
        "run-1",
        event_type="completion",
        phase="execution",
        producer="fixture",
        status="ok",
        payload={
            "capability_ids": ["cap-a"],
            "skill_ids": ["sk"],
            "workflow_ids": ["wf"],
        },
    )
    feedback.record_influence_edge(
        target_run_id="run-1",
        influence_type="role",
        influence_id="r1",
        source_run_id="src-0",
        accepted=True,
        acceptance_gate_id="gate",
    )
    rows = promotion._accepted_source_influences(["run-1"])
    kinds = {row["influence_type"] for row in rows}
    assert {"role", "capability", "skill", "workflow"}.issubset(kinds)
    assert promotion._accepted_source_influences([]) == []
    assert promotion._accepted_source_influences(["missing"]) == []


def test_completion_probe_pending_pid_and_unreadable_head(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worktree = tmp_path / "wt"
    worktree.mkdir()
    state = {
        "synthesis": {
            "pid": os.getpid(),
            "worktree": str(worktree),
            "log": str(tmp_path / "s.log"),
        }
    }
    assert promotion.default_completion_probe(state)["status"] == "pending"

    log = tmp_path / "logs" / "synthesis.log"
    marker = log.parent / "done" / "run-1.json"
    marker.parent.mkdir(parents=True)
    marker.write_text(json.dumps({"rc": 0}))
    state = {
        "synthesis": {
            "current_run_id": "run-1",
            "log": str(log),
            "worktree": str(worktree),
            "launch_head": "old",
        }
    }
    monkeypatch.setattr(
        promotion,
        "_git",
        lambda *_: _completed(rc=1),
    )
    assert (
        promotion.default_completion_probe(state)["reason"] == "synthesis worktree HEAD unreadable"
    )


def test_safe_command_and_run_gate_timeout(tmp_path, monkeypatch) -> None:
    assert promotion._safe_command(["git", "diff", "--check"]) is True
    assert promotion._safe_command(["node", "--test", "x.test.js"]) is True
    assert promotion._safe_command(["npm", "--prefix", "pkg", "run", "test"]) is True
    assert promotion._safe_command(["python3.11", "-m", "pytest", "t.py"]) is True
    assert promotion._safe_command(["uv", "run", "pytest"]) is True
    assert promotion._safe_command(["npm", "run", "lint"]) is True
    assert promotion._safe_command([]) is False

    def timeout_run(*_a, **_k):
        raise subprocess.TimeoutExpired(cmd="pytest", timeout=1)

    monkeypatch.setattr(promotion.subprocess, "run", timeout_run)
    result = promotion._run_gate(["pytest", "t.py"], tmp_path, timeout=1)
    assert result["ok"] is False and result.get("transient") is True


def test_verify_synthesis_local_shlex_runtime_and_prefix_gates(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    exp = tmp_path / "exp-v"
    exp.mkdir()
    (exp / "meta.json").write_text(json.dumps({"base_sha": "base", "repo": "o/r"}))
    (exp / "spec.md").write_text("needs deliberate-break proof")
    (exp / "promotion-verification.json").write_text(
        json.dumps(
            {
                "allowed_path_prefixes": ["src"],
                "local_verify": {"test_cmd": "echo bad", "test_paths": []},
                "runtime_ac": {"spec_path": "missing-spec.json"},
            }
        )
    )
    state = {"synthesis": {"worktree": str(repo)}}
    monkeypatch.setattr(
        promotion,
        "_git",
        lambda _wt, args: _completed(
            stdout="lib/x.py\n" if "name-only" in args else "diff",
            rc=0,
        ),
    )
    monkeypatch.setattr(
        promotion,
        "_run_gate",
        lambda *_a, **_k: {"ok": True, "argv": ["git", "diff", "--check"]},
    )
    out = promotion.verify_synthesis(state, exp)
    assert out["passed"] is False
    assert out["evidence"]["local_verify"]["verdict"] == "ERROR"
    assert out["evidence"]["runtime_ac"]["verdict"] == "ERROR"

    (exp / "promotion-verification.json").write_text(
        json.dumps({"local_verify": {"test_cmd": "pytest t.py", "test_paths": ["t.py"]}})
    )
    (exp / "spec.md").write_text("no deliberate break required")

    def pass_local(*_a, **_k):
        return {"verdict": "PASS", "hollow_nodes": "not-a-list"}

    monkeypatch.setattr(promotion.local_verify, "verify", pass_local)
    out2 = promotion.verify_synthesis(state, exp)
    assert out2["evidence"]["local_verify"]["hollow_nodes"] == []


def test_default_delivery_discovery_statuses(tmp_path, monkeypatch) -> None:
    state = {"repo": "owner/repo", "candidate": {"candidate_id": "synth-candidate:abc"}}
    assert promotion.default_delivery_discovery({})["status"] == "no_candidate"

    def fail_search(_argv):
        raise RuntimeError("search down")

    assert promotion.default_delivery_discovery(state, gh_fn=fail_search)["status"] == "unavailable"

    def empty_search(argv):
        assert argv[:2] == ["gh", "api"]
        return {
            "total_count": 0,
            "incomplete_results": False,
            "items": [],
        }

    assert promotion.default_delivery_discovery(state, gh_fn=empty_search)["status"] == "no_pr"

    db = feedback.DB_PATH
    with sqlite3.connect(db) as conn:
        conn.executescript(feedback.SCHEMA)

    def one_pr_search(argv):
        if "search/issues" in argv:
            return {
                "total_count": 1,
                "incomplete_results": False,
                "items": [
                    {
                        "number": 7,
                        "pull_request": {"url": "https://api.github.com/repos/owner/repo/pulls/7"},
                    }
                ],
            }
        return {
            "number": 7,
            "url": "https://api.github.com/repos/owner/repo/pulls/7",
            "body": "synth-candidate:abc",
            "base": {"repo": {"full_name": "owner/repo"}},
        }

    pending = promotion.default_delivery_discovery(state, gh_fn=one_pr_search)
    assert pending["status"] == "record_pending"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO runs(run_id,target,pr_number) VALUES ('keepalive:owner/repo#7:codex','o/r#7',7)"
        )
    linked = promotion.default_delivery_discovery(state, gh_fn=one_pr_search, db_path=db)
    assert linked["status"] == "linked"


def test_shadow_reconcile_and_dry_run_reconcile(tmp_path) -> None:
    state = _evaluated_state(tmp_path)
    state["expires_ts"] = 50
    plan = promotion._shadow_reconcile_plan(state, now=100)
    assert plan[0]["to"] == "discarded"
    state["delivery_phase"] = "durable"
    assert promotion._shadow_reconcile_plan(state, now=100)[0]["action"] == "none"
    exp = tmp_path / "exp-a"
    out = promotion.reconcile(exp, dry_run=True, now=100)
    assert out["mode"] == "shadow" and out["would_do"]


def test_mirror_outcome_discarded_and_mirror_once(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(feedback, "record_outcome", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(
        feedback,
        "record_influence_edge",
        lambda **k: {"edge_id": "edge-1"},
    )
    monkeypatch.setattr(feedback, "record_completion_event", lambda *a, **k: None)
    state = {
        "delivery_phase": "discarded",
        "experiment_id": "exp-x",
        "synthesis": {"root_run_id": "run-synth"},
        "outcome": {"merged": False},
        "lineage": {"members": [{"source_run_id": "src-1", "member_id": "m1"}]},
        "verification": {"evidence_hash": "eh"},
        "mirrored_events": [],
    }
    result = promotion.default_mirror_outcome(state, "discarded")
    assert result["recorded"] is True
    state["mirrored_events"] = ["evt"]
    again = promotion._mirror_once(state, "evt", promotion.default_mirror_outcome)
    assert again["mirrored_events"] == ["evt"]


def test_apply_delivery_link_and_compile_candidate_conflict(tmp_path) -> None:
    state = _evaluated_state(tmp_path)
    state["delivery_phase"] = "candidate_ready"
    state["canonical_state"] = promotion.PHASE_TO_CANONICAL["candidate_ready"]
    state["candidate"] = {"candidate_id": "c1", "body_hash": "h1"}
    with pytest.raises(ValueError, match="delivery link requires"):
        promotion._apply_delivery_link(
            state, delivery_run_id="", delivery_ref="o/r#1", pr_number=None, now=1
        )
    linked = promotion._apply_delivery_link(
        state,
        delivery_run_id="keepalive:o/r#1:codex",
        delivery_ref="o/r#1",
        pr_number=1,
        now=200,
    )
    assert linked["delivery_phase"] == "delegated_or_pr"

    exp = tmp_path / "exp-c"
    exp.mkdir()
    (exp / "meta.json").write_text(json.dumps({"exp_id": "exp-c", "repo": "o/r", "agents": []}))
    state2 = promotion.ensure_evaluated_state(exp, now=10)
    state2["delivery_phase"] = "synth_verified"
    state2["canonical_state"] = promotion.PHASE_TO_CANONICAL["synth_verified"]
    state2["verification"] = {"evidence_hash": "ev"}
    state2["synthesis"] = {"commit": "deadbeef", "run_ids": []}
    (exp / promotion.CANDIDATE_JSON).write_text(
        json.dumps({"candidate_id": "other", "body_hash": "x"})
    )
    with pytest.raises(ValueError, match="conflicts with deterministic candidate"):
        promotion.compile_candidate(state2, exp, now=300)


def test_break_caveat_and_main_cli(tmp_path, capsys, monkeypatch) -> None:
    hollow = promotion._break_caveat({"hollow_nodes": ["tests/test_x.py"], "node_verdict": "PASS"})
    assert "NOT a clean per-node proof" in hollow
    assert promotion._break_caveat({"node_verdict": "FAIL"}) != ""

    exp = tmp_path / "exp-cli"
    exp.mkdir()
    (exp / "meta.json").write_text(json.dumps({"exp_id": "exp-cli", "repo": "o/r", "agents": []}))
    assert promotion.main(["init", str(exp)]) == 0
    assert promotion.main(["status", str(exp)]) == 0
    assert promotion.main(["reconcile", str(exp)]) == 0
    monkeypatch.setenv(promotion.FOLLOWUP_SHIP_GATE_ENV, "0")
    assert promotion.main(["reconcile", str(exp), "--live"]) == 2
    assert "refusing --live reconcile" in capsys.readouterr().err
