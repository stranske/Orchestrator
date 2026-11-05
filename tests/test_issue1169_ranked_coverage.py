"""Ranked coverage chunk for Orchestrator #1169 (synthesis_promotion, codemod_lane)."""

from __future__ import annotations

import base64
import copy
import hashlib
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
    monkeypatch.delenv("ORCH_CAPABILITY_HEARTBEATS", raising=False)
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


def _promotion_at(tmp_path: Path, name: str, phase: str, *, now: int = 10):
    exp = tmp_path / name
    exp.mkdir()
    (exp / "meta.json").write_text(
        json.dumps(
            {
                "exp_id": name,
                "repo": "owner/repo",
                "agents": ["codex"],
                "base": "main",
                "base_sha": "b",
            }
        )
    )
    (exp / "spec.md").write_text("ordinary scope\n")
    state = promotion.ensure_evaluated_state(exp, now=now)
    order = [
        "synth_running",
        "synth_complete",
        "synth_verified",
        "candidate_ready",
        "delegated_or_pr",
        "merged",
        "durable",
    ]
    if phase != "evaluated":
        for step in order:
            state, changed = promotion.transition(state, step, reason=f"to-{step}", now=now)
            assert changed, step
            if step == phase:
                break
        promotion._atomic_json(promotion.state_path(exp), state)
        state = promotion.load_state(exp)
    return exp, state


def _gitignore_campaign() -> dict:
    campaign = json.loads(
        (
            Path(__file__).resolve().parents[1] / "campaigns/gitignore-caches-2026-10.json"
        ).read_text()
    )
    campaign["campaign"]["repos"] = ["owner/repo"]
    return campaign


def _git_fn(
    *,
    fail: bool = False,
    heads: tuple[str, ...] = ("aaa", "aaa"),
    statuses: tuple[str, ...] = ("", ""),
    diff_text: str = "diff\n",
    push_rc: int = 0,
    branch: str = "",
):
    cursor = {"head": 0, "status": 0}

    def run(argv, **kwargs):
        if fail:
            return subprocess.CompletedProcess(argv, 1, "", "git failed")
        args = argv[3:]
        if args == ["rev-parse", "HEAD"]:
            idx = min(cursor["head"], len(heads) - 1)
            cursor["head"] += 1
            return subprocess.CompletedProcess(argv, 0, heads[idx] + "\n", "")
        if args == ["status", "--porcelain"]:
            idx = min(cursor["status"], len(statuses) - 1)
            cursor["status"] += 1
            return subprocess.CompletedProcess(argv, 0, statuses[idx], "")
        if args[:2] == ["merge-base", "--is-ancestor"]:
            return subprocess.CompletedProcess(argv, 0, "", "")
        if args[:2] == ["diff", "--no-ext-diff"]:
            return subprocess.CompletedProcess(argv, 0, diff_text, "")
        if args == ["branch", "--show-current"]:
            return subprocess.CompletedProcess(argv, 0, branch + "\n", "")
        if args[:2] == ["branch", "-m"]:
            return subprocess.CompletedProcess(argv, 0, "", "")
        if args[:1] == ["push"]:
            stderr = "" if push_rc == 0 else "rejected"
            return subprocess.CompletedProcess(argv, push_rc, "", stderr)
        raise AssertionError(args)

    return run


def _install_producer_fakes(monkeypatch, worktree: Path) -> None:
    import exp_abcd
    import experiment_producer as producer
    import research_subjects

    member = {"member_id": "m1", "agent": "codex", "legacy": False}
    monkeypatch.setattr(exp_abcd, "experiment_members", lambda _meta: [member])
    monkeypatch.setattr(exp_abcd, "exp_worktree", lambda *_a, **_k: worktree)
    monkeypatch.setattr(research_subjects, "mark_lifecycle", lambda *_a, **_k: None)
    monkeypatch.setattr(producer, "release_lock", lambda *_a, **_k: None)
    monkeypatch.setattr(producer, "named_test_paths", lambda _command: ["tests/test_x.py"])


def _delivery_meta(root: Path) -> dict:
    return {
        "exp_id": root.name,
        "repo": "owner/repo",
        "subject": "owner/repo#9",
        "base": "main",
        "base_sha": "basesha",
        "named_test": "pytest tests/test_x.py",
        "members": [{"member_id": "m1", "agent": "codex"}],
    }


def test_atomic_text_removes_temp_file_when_replace_fails(tmp_path, monkeypatch) -> None:
    target = tmp_path / "out.txt"

    def boom(*_args, **_kwargs):
        raise OSError("replace failed")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError, match="replace failed"):
        promotion._atomic_text(target, "hello")
    assert list(tmp_path.glob(".out.txt.*.tmp")) == []
    assert not target.exists()


def test_promotion_guards_reject_illegal_states(tmp_path, monkeypatch) -> None:
    exp, state = _promotion_at(tmp_path, "guards", "evaluated")
    same, changed = promotion.transition(state, "evaluated", reason="noop", now=2)
    assert changed is False and same["delivery_phase"] == "evaluated"
    with pytest.raises(ValueError, match="invalid synthesis promotion transition"):
        promotion.transition(state, "durable", reason="skip", now=2)

    reason = "same-reason"
    event_id = (
        "promotion:"
        + hashlib.sha256(
            f"{state['experiment_id']}|evaluated|synth_running|{reason}".encode()
        ).hexdigest()[:24]
    )
    seeded = copy.deepcopy(state)
    seeded["phase_history"].append(
        {
            "event_id": event_id,
            "from": "evaluated",
            "to": "synth_running",
            "canonical_state": promotion.PHASE_TO_CANONICAL["synth_running"],
            "reason": reason,
            "ts": 1,
        }
    )
    replayed, replay_changed = promotion.transition(seeded, "synth_running", reason=reason, now=3)
    assert replay_changed is False and replayed["delivery_phase"] == "evaluated"

    original_states = capabilities.CANONICAL_STATES
    monkeypatch.setattr(
        capabilities,
        "CANONICAL_STATES",
        tuple(item for item in original_states if item != "generated"),
    )
    with pytest.raises(ValueError, match="maps outside canonical capability states"):
        promotion.validate_state(state)

    monkeypatch.setattr(capabilities, "CANONICAL_STATES", original_states)
    _exp, complete = _promotion_at(tmp_path, "guards-target", "synth_complete")
    monkeypatch.setattr(
        capabilities,
        "CANONICAL_STATES",
        tuple(item for item in original_states if item != "validated"),
    )
    with pytest.raises(ValueError, match="does not map to a canonical state"):
        promotion.transition(complete, "synth_verified", reason="map", now=4)


def test_corrupt_completion_payload_is_skipped(tmp_path) -> None:
    db = feedback.DB_PATH
    with sqlite3.connect(db) as conn:
        conn.executescript(feedback.SCHEMA)
        conn.execute(
            "INSERT INTO completion_events (event_id,schema_version,run_id,event_type,phase,"
            "producer,validation_status,payload_json,content_hash,created_ts,updated_ts) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            ("bad", 1, "run-1", "completion", "execution", "fixture", "accepted", "{", "h", 1, 1),
        )
    assert promotion._accepted_source_influences(["run-1"]) == []


def test_completion_probe_dead_pid_is_interrupted() -> None:
    for pid in ("nope", 2**30):
        probed = promotion.default_completion_probe({"synthesis": {"pid": pid}})
        assert probed["status"] == "interrupted"
        assert probed["reason"] == "synthesis exited without completion marker"


def test_verification_config_shlex_and_runtime_pass(tmp_path, monkeypatch) -> None:
    exp, _state = _promotion_at(tmp_path, "verify-edges", "evaluated")
    (exp / "promotion-verification.json").write_text("[]")
    with pytest.raises(ValueError, match="must be an object"):
        promotion.verify_synthesis({"synthesis": {"worktree": str(exp)}}, exp)

    monkeypatch.setattr(promotion, "_git", lambda *_a, **_k: _completed(stdout="src/a.py\n"))
    monkeypatch.setattr(promotion, "_run_gate", lambda *_a, **_k: {"ok": True})
    monkeypatch.setattr(
        promotion.runtime_ac,
        "run_verification",
        lambda *_a, **_k: {"gate": {"verdict": "PASS"}},
    )
    (exp / "rt.json").write_text('{"checks": []}')
    (exp / "promotion-verification.json").write_text(
        json.dumps({"runtime_ac": {"spec_path": "rt.json"}})
    )
    passed = promotion.verify_synthesis({"synthesis": {"worktree": str(exp)}}, exp)
    assert passed["evidence"]["local_verify"]["verdict"] == "NOT_APPLICABLE"
    assert passed["evidence"]["runtime_ac"]["verdict"] == "PASS"

    (exp / "promotion-verification.json").write_text(
        json.dumps({"local_verify": {"test_cmd": 'echo "', "test_paths": []}})
    )
    broken = promotion.verify_synthesis({"synthesis": {"worktree": str(exp)}}, exp)
    assert broken["evidence"]["local_verify"]["verdict"] == "ERROR"
    assert broken["evidence"]["local_verify"]["test_cmd"] == 'echo "'


def test_producer_candidate_reuse_and_discovery_errors(tmp_path) -> None:
    root = tmp_path / "producer"
    root.mkdir()
    meta = {
        "exp_id": root.name,
        "repo": "owner/repo",
        "agents": [],
        "members": [{"member_id": "m1", "agent": "codex"}],
    }
    member = {"member_id": "m1", "agent": "codex"}
    receipt = {"candidate_id": "experiment:producer", "local_verify": {"m1": {"ok": True}}}
    ready = promotion.compile_producer_candidate(root, meta, member, receipt, "abc")
    assert ready["delivery_phase"] == "candidate_ready"
    again = promotion.compile_producer_candidate(root, meta, member, receipt, "abc")
    assert again["delivery_phase"] == "candidate_ready"
    assert again["candidate"]["candidate_id"] == ready["candidate"]["candidate_id"]

    def gh_down(_argv):
        return {"error": "down"}

    unavailable = promotion.reconcile_producer_delivery(root, gh_fn=gh_down)
    assert "delivery_discovery:unavailable" in unavailable["actions"]

    def gh_empty(_argv):
        return {"stdout": json.dumps({"total_count": 0, "incomplete_results": False, "items": []})}

    quiet = promotion.reconcile_producer_delivery(root, gh_fn=gh_empty)
    assert "delivery_discovery:no_pr" in quiet["actions"]


def test_deliver_producer_winner_stops_on_each_fail_closed_edge(tmp_path, monkeypatch) -> None:
    worktree = tmp_path / "wt"
    worktree.mkdir()
    _install_producer_fakes(monkeypatch, worktree)
    calls: list[list[str]] = []

    def deliver(root: Path, mode: str, **git_kwargs):
        meta = _delivery_meta(root)
        branch = f"orch/exp-{root.name}"
        candidate = f"experiment:{root.name}"

        def gh(argv):
            calls.append(argv)
            text = " ".join(argv)
            if "/pulls?" in text:
                if mode == "not_list":
                    return {"stdout": "{}"}
                if mode == "pages":
                    if "page=2" in text:
                        return {"stdout": "[]"}
                    page = [
                        {"number": i, "body": "untagged", "html_url": f"https://example/{i}"}
                        for i in range(100)
                    ]
                    return {"stdout": json.dumps(page)}
                if mode == "named":
                    return {
                        "stdout": json.dumps(
                            [{"number": 3, "body": candidate, "html_url": "https://example/3"}]
                        )
                    }
                return {"stdout": "[]"}
            if "POST" in argv and any(arg.endswith("/pulls") and "?" not in arg for arg in argv):
                if mode == "pub":
                    return {"error": "rate"}
                if mode == "badpr":
                    return {"stdout": json.dumps({"number": 3})}
                return {"stdout": json.dumps({"html_url": "https://example/3", "number": 3})}
            if "/issues/" in text:
                if mode == "issue_down":
                    return {"error": "down"}
                if mode == "bad_json":
                    return {"stdout": "not-json"}
                if mode == "closed":
                    return {"stdout": json.dumps({"state": "closed", "labels": []})}
                return {"stdout": json.dumps({"state": "open", "labels": []})}
            raise AssertionError(argv)

        ranking = [("absent", 1)] if mode == "missing" else [("m1", 1)]
        return promotion.deliver_producer_winner(
            root,
            meta,
            {"ranking": ranking},
            verify_fn=lambda *_a, **_k: {"ok": True},
            git_fn=_git_fn(branch=branch, **git_kwargs),
            gh_fn=gh,
        )

    def root(name: str) -> Path:
        path = tmp_path / name
        path.mkdir()
        return path

    early = root("early")
    (early / "producer-delivery.json").write_text(json.dumps({"pr_url": "https://example/pr"}))
    assert deliver(early, "missing")["pr_url"] == "https://example/pr"

    assert deliver(root("missing"), "missing")["status"] == "no_winner"
    assert deliver(root("gitfail"), "gitfail", fail=True)["status"] == "no_winner"
    down = deliver(root("issuedown"), "issue_down")
    assert down["status"] == "github_unavailable" and down["error"] == "down"
    closed = deliver(root("closed"), "closed")
    assert closed["status"] == "discarded" and closed["reason"] == "subject_closed"
    invalid = deliver(root("notlist"), "not_list")
    assert invalid["error"] == "invalid PR population"
    named = deliver(root("named"), "named")
    assert named["status"] == "delegated_or_pr" and named["pr_number"] == 3
    dirty = deliver(root("pages"), "pages", statuses=("", "", " M a.py\n"))
    assert dirty["reason"] == "winner_worktree_dirty"
    assert any("page=2" in " ".join(call) for call in calls)
    stale = deliver(root("stale"), "stale", heads=("aaa", "bbb"))
    assert stale["status"] == "verification_stale"
    secret = deliver(root("secret"), "secret", diff_text="token ghp_abcdefghij\n")
    assert secret["reason"] == "secret_in_winner_diff"
    pushed = deliver(root("push"), "push", push_rc=1)
    assert pushed["status"] == "push_failed"
    pending = deliver(root("pub"), "pub")
    assert pending["status"] == "publication_pending" and pending["error"] == "rate"
    bad = deliver(root("badpr"), "badpr")
    assert bad["error"] == "PR URL unavailable"
    garbled = deliver(root("badjson"), "bad_json")
    assert garbled["status"] == "github_unavailable" and "Expecting value" in garbled["error"]


def test_compile_link_and_outcome_edges(tmp_path, monkeypatch) -> None:
    exp, state = _promotion_at(tmp_path, "compile-early", "evaluated")
    with pytest.raises(ValueError, match="requires synth_verified"):
        promotion.compile_candidate(state, exp, now=1)

    exp, verified = _promotion_at(tmp_path, "compile-reuse", "synth_verified")
    verified["verification"] = {"evidence_hash": "ev"}
    verified["synthesis"] = {"commit": "abc", "run_ids": ["r"]}
    _first, candidate = promotion.compile_candidate(verified, exp, now=20)
    path = exp / promotion.CANDIDATE_JSON
    stored = json.loads(path.read_text())
    stored["preserved"] = True
    path.write_text(json.dumps(stored))
    _second, reused = promotion.compile_candidate(verified, exp, now=21)
    assert reused["preserved"] is True and reused["candidate_id"] == candidate["candidate_id"]

    with pytest.raises(ValueError, match="requires a candidate_ready"):
        promotion._apply_delivery_link(
            state, delivery_run_id="run", delivery_ref="owner/repo#1", pr_number=1, now=1
        )
    _ready_exp, ready = _promotion_at(tmp_path, "link", "candidate_ready")
    linked = promotion._apply_delivery_link(
        ready,
        delivery_run_id="keepalive:owner/repo#1:codex",
        delivery_ref="owner/repo#1",
        pr_number=1,
        now=2,
    )
    with pytest.raises(ValueError, match="conflicting delivery link"):
        promotion._apply_delivery_link(
            linked,
            delivery_run_id="other",
            delivery_ref="owner/repo#2",
            pr_number=2,
            now=3,
        )
    bare = tmp_path / "bare"
    bare.mkdir()
    with pytest.raises(ValueError, match="run id and issue/PR reference"):
        promotion.link_delivery(bare, delivery_run_id="", delivery_ref="owner/repo#1")
    with pytest.raises(ValueError, match="requires a candidate_ready"):
        promotion.link_delivery(bare, delivery_run_id="run", delivery_ref="owner/repo#1")

    def gh(argv):
        if "search/issues" in argv:
            return {
                "total_count": 1,
                "incomplete_results": False,
                "items": [
                    {
                        "number": 4,
                        "pull_request": {"url": "https://api.github.com/repos/owner/repo/pulls/4"},
                    }
                ],
            }
        return {
            "number": 4,
            "url": "https://api.github.com/repos/owner/repo/pulls/4",
            "body": "synth-candidate:abc",
            "base": {"repo": {"full_name": "owner/repo"}},
        }

    pending = promotion.default_delivery_discovery(
        {"repo": "owner/repo", "candidate": {"candidate_id": "synth-candidate:abc"}},
        gh_fn=gh,
        db_path=tmp_path / "missing.db",
    )
    assert pending["status"] == "record_pending" and pending["pr_number"] == 4
    assert promotion.default_outcome_lookup({}) is None
    assert promotion.default_outcome_lookup({"delivery": {"run_id": "none"}}) is None

    def failed_run(*_args, **_kwargs):
        return _completed(rc=1, stderr="gh: denied")

    monkeypatch.setattr(promotion.subprocess, "run", failed_run)
    with pytest.raises(RuntimeError, match="gh: denied"):
        promotion._gh_json(["gh", "api", "user"])

    def empty_run(*_args, **_kwargs):
        return _completed(stdout="")

    monkeypatch.setattr(promotion.subprocess, "run", empty_run)
    assert promotion._gh_json(["gh", "api", "user"]) is None

    monkeypatch.setattr(feedback, "record_outcome", lambda *_a, **_k: {"ok": True})
    monkeypatch.setattr(feedback, "record_influence_edge", lambda **_k: {"edge_id": "e1"})
    monkeypatch.setattr(feedback, "record_completion_event", lambda *_a, **_k: None)
    mirrored = promotion.default_mirror_outcome(
        {
            "delivery_phase": "merged",
            "experiment_id": "exp",
            "synthesis": {"root_run_id": "synth", "base": "m1"},
            "outcome": {"merged": True},
            "lineage": {
                "members": [
                    {"member_id": "skip"},
                    {"member_id": "m1", "source_run_id": "src-1"},
                ]
            },
            "verification": {"evidence_hash": "eh"},
            "candidate": {"candidate_id": "c"},
            "delivery": {"ref": "owner/repo#1", "pr_number": 1},
        },
        "merged",
    )
    assert mirrored["edge_ids"] == ["e1"]


def test_shadow_plan_and_reconcile_failure_paths(tmp_path, monkeypatch) -> None:
    _exp, evaluated = _promotion_at(tmp_path, "backoff", "evaluated", now=10)
    evaluated["retry"] = {"count": 1, "next_retry_ts": 500, "failures": []}
    assert promotion._shadow_reconcile_plan(evaluated, now=30)[0]["reason"] == "retry_backoff"

    _stale_exp, stale = _promotion_at(tmp_path, "stale-cand", "candidate_ready", now=10)
    stale["expires_ts"] = 10**12
    stale["candidate_expires_ts"] = 20
    assert promotion._shadow_reconcile_plan(stale, now=30)[0]["reason"] == "stale_candidate_retired"

    _wait_exp, waiting = _promotion_at(tmp_path, "wait-link", "candidate_ready", now=10)
    assert (
        promotion._shadow_reconcile_plan(waiting, now=30)[0]["reason"]
        == "awaiting_external_delivery_link"
    )

    injectors = promotion._shadow_reconcile_injectors()
    assert injectors["launch_fn"]()["blocked"] is True
    assert injectors["completion_fn"]({})["status"] == "pending"
    assert injectors["resume_fn"]({})["blocked"] is True
    assert injectors["verify_fn"]({}, tmp_path)["passed"] is False
    assert injectors["outcome_lookup_fn"]({}) is None
    assert injectors["mirror_fn"]({}, "evt")["recorded"] is False

    missing = tmp_path / "missing-promo"
    missing.mkdir()
    with pytest.raises(ValueError, match="not initialized"):
        promotion.reconcile(missing, dry_run=True, now=1)
    with pytest.raises(ValueError, match="not initialized"):
        promotion.reconcile(missing, now=1)

    def mirror(*_args, **_kwargs):
        return {"recorded": False}

    discard_exp, _state = _promotion_at(tmp_path, "discard-launch", "evaluated")
    discarded = promotion.reconcile(
        discard_exp,
        launch_fn=lambda: {"discard": True, "gate": "usefulness", "ranking": []},
        now=40,
        mirror_fn=mirror,
    )
    assert discarded["actions"] == ["discarded_by_usefulness_gate"]
    assert discarded["state"]["delivery_phase"] == "discarded"

    blocked_exp, _state = _promotion_at(tmp_path, "blocked-launch", "evaluated")

    def blocked():
        return {"blocked": True, "reason": "full"}

    for tick in (100, 500):
        retrying = promotion.reconcile(blocked_exp, launch_fn=blocked, now=tick, mirror_fn=mirror)
        assert retrying["state"]["delivery_phase"] == "evaluated"
        assert "synthesis_launch_retry" in retrying["actions"]
    exhausted = promotion.reconcile(blocked_exp, launch_fn=blocked, now=1200, mirror_fn=mirror)
    assert exhausted["state"]["delivery_phase"] == "discarded"

    monkeypatch.setattr(promotion, "_git", lambda *_a, **_k: _completed(stdout="abc\n"))
    work = tmp_path / "work"
    work.mkdir()

    def launch():
        return {
            "pid": 4242,
            "run_id": "exp:synth",
            "worktree": str(work),
            "log": str(tmp_path / "s.log"),
            "base": "codex",
            "synth_agent": "codex",
        }

    running_exp, _state = _promotion_at(tmp_path, "running", "evaluated")
    launched = promotion.reconcile(running_exp, launch_fn=launch, now=200, mirror_fn=mirror)
    assert launched["state"]["delivery_phase"] == "synth_running"
    required = promotion.reconcile(
        running_exp,
        completion_fn=lambda _state: {"status": "interrupted", "reason": "lost"},
        now=201,
        mirror_fn=mirror,
    )
    assert "synthesis_resume_required" in required["actions"]
    failed_resume = promotion.reconcile(
        running_exp,
        completion_fn=lambda _state: {"status": "interrupted", "reason": "lost"},
        resume_fn=lambda _state: {},
        now=600,
        mirror_fn=mirror,
    )
    assert "synthesis_resume_failed" in failed_resume["actions"]
    retired = promotion.reconcile(
        running_exp,
        completion_fn=lambda _state: {"status": "interrupted", "reason": "lost"},
        now=1300,
        mirror_fn=mirror,
    )
    assert retired["state"]["delivery_phase"] == "discarded"
    assert "synthesis_retired_after_interruptions" in retired["actions"]

    failed_exp, _state = _promotion_at(tmp_path, "failed-synth", "evaluated")
    promotion.reconcile(failed_exp, launch_fn=launch, now=300, mirror_fn=mirror)
    failed = promotion.reconcile(
        failed_exp,
        completion_fn=lambda _state: {"status": "failed", "reason": "no commit"},
        now=301,
        mirror_fn=mirror,
    )
    assert failed["actions"] == ["synthesis_discarded"]

    unknown_exp, _state = _promotion_at(tmp_path, "unknown-synth", "evaluated")
    promotion.reconcile(unknown_exp, launch_fn=launch, now=400, mirror_fn=mirror)
    with pytest.raises(ValueError, match="unknown synthesis completion status"):
        promotion.reconcile(
            unknown_exp,
            completion_fn=lambda _state: {"status": "bogus"},
            now=401,
            mirror_fn=mirror,
        )

    flake_exp, _state = _promotion_at(tmp_path, "flake", "evaluated")
    promotion.reconcile(flake_exp, launch_fn=launch, now=500, mirror_fn=mirror)

    def flake(_state, _root):
        return {"passed": False, "transient": True, "failure_reason": "flake"}

    for tick in (501, 900):
        interim = promotion.reconcile(
            flake_exp,
            completion_fn=lambda _state: {"status": "complete", "commit": "abc"},
            verify_fn=flake,
            now=tick,
            mirror_fn=mirror,
        )
        assert interim["state"]["delivery_phase"] == "synth_complete"
    gave_up = promotion.reconcile(
        flake_exp,
        verify_fn=flake,
        now=2000,
        mirror_fn=mirror,
    )
    assert gave_up["state"]["delivery_phase"] == "discarded"
    assert "verification_failed" in gave_up["actions"]


def test_promotion_cli_live_gate_link_and_heartbeat(tmp_path, capsys, monkeypatch) -> None:
    def explode(*_args, **_kwargs):
        raise RuntimeError("heartbeat down")

    monkeypatch.setattr(capabilities, "production_heartbeat", explode)
    monkeypatch.setattr(promotion, "selftest", lambda: None)
    assert promotion.main(["selftest"]) == 0

    exp, _state = _promotion_at(tmp_path, "cli-live", "candidate_ready")
    monkeypatch.delenv(promotion.FOLLOWUP_SHIP_GATE_ENV, raising=False)
    assert promotion.main(["reconcile", str(exp), "--live"]) == 2
    assert "sole launch and delivery controller" in capsys.readouterr().err
    assert (
        promotion.main(
            [
                "link-delivery",
                str(exp),
                "--run-id",
                "keepalive:owner/repo#4:codex",
                "--ref",
                "owner/repo#4",
                "--pr-number",
                "4",
            ]
        )
        == 0
    )
    assert "delegated_or_pr" in capsys.readouterr().out


def test_codemod_validation_recipe_and_plan_edges() -> None:
    base = _valid_campaign()
    broken = copy.deepcopy(base)
    broken["campaign"]["repos"] = "owner/repo"
    assert any("must be a list" in err for err in lane.validate_campaign(broken))
    broken = copy.deepcopy(base)
    broken["campaign"]["repos"] = ["  "]
    assert any("must be a non-empty string" in err for err in lane.validate_campaign(broken))
    broken = copy.deepcopy(base)
    broken["campaign"] = []
    assert any("must be an object" in err for err in lane.validate_campaign(broken))
    broken = copy.deepcopy(base)
    del broken["scope"]
    assert any("is required" in err for err in lane.validate_campaign(broken))
    for field, value, text in (
        ("id", "", "campaign.id must be a non-empty string"),
        ("title", "  ", "campaign.title must be a non-empty string"),
        ("goal", "", "campaign.goal must be a non-empty string"),
    ):
        broken = copy.deepcopy(base)
        broken["campaign"][field] = value
        assert any(text in err for err in lane.validate_campaign(broken))
    broken = copy.deepcopy(base)
    broken["recipe"]["language"] = ""
    broken["recipe"]["summary"] = " "
    broken["recipe"]["risk_level"] = "severe"
    broken["recipe"]["rewrite"] = 1
    broken["recipe"]["rule_file"] = 2
    broken["recipe"]["command_template"] = 3
    found = " ".join(lane.validate_campaign(broken))
    assert "recipe.language must be a non-empty string" in found
    assert "recipe.summary must be a non-empty string" in found
    assert "recipe.risk_level must be one of" in found
    assert "recipe.rewrite must be a string" in found
    assert "recipe.rule_file must be a string" in found
    assert "recipe.command_template must be a string" in found
    broken = copy.deepcopy(base)
    broken["rollout"]["branch_prefix"] = " "
    broken["rollout"]["pr_strategy"] = "weekly"
    broken["delegate_prompt"] = ""
    broken["scope"]["max_files"] = True
    found = " ".join(lane.validate_campaign(broken))
    assert "branch_prefix must be a non-empty string" in found
    assert "pr_strategy must be one of" in found
    assert "delegate_prompt must be a non-empty string" in found
    assert "scope.max_files must be an integer" in found

    add_only = _gitignore_campaign()
    add_only["rollout"] = []
    add_only["ignore_entries"] = []
    add_only_errors = lane.validate_campaign(add_only)
    assert any("must be an object" in err for err in add_only_errors)
    assert not any("exactly the five" in err for err in add_only_errors)

    scope = {"include_globs": ["src/**"], "exclude_globs": []}
    rule, warning = lane._recipe_dry_run_command(
        {"tool": "ast-grep", "rule_file": "rules/r.yml", "language": "python"},
        scope,
    )
    assert warning is None and "ast-grep" in rule and "--rule" in rule
    safe_js, warning = lane._recipe_dry_run_command(
        {
            "tool": "jscodeshift",
            "command_template": "jscodeshift -t t.js --dry src",
            "language": "js",
        },
        scope,
    )
    assert warning is None and safe_js.startswith("jscodeshift")
    unsafe_js, warning = lane._recipe_dry_run_command(
        {"tool": "jscodeshift", "command_template": "jscodeshift --apply src", "language": "js"},
        scope,
    )
    assert unsafe_js is None and "jscodeshift" in warning
    safe_rewrite, warning = lane._recipe_dry_run_command(
        {
            "tool": "openrewrite",
            "command_template": "mvn -DdryRun rewrite:dryRun",
            "language": "java",
        },
        scope,
    )
    assert warning is None and "dryRun" in safe_rewrite
    unsafe_rewrite, warning = lane._recipe_dry_run_command(
        {"tool": "openrewrite", "command_template": "mvn rewrite:run", "language": "java"},
        scope,
    )
    assert unsafe_rewrite is None and "openrewrite" in warning
    assert lane._template_is_safe_dry_run("mvn rewrite:run") is False
    missing, warning = lane._recipe_dry_run_command(
        {"tool": "ast-grep", "language": "python"},
        scope,
    )
    assert missing is None and "no dry-run command" in warning
    with pytest.raises(ValueError, match="invalid campaign"):
        lane.build_dry_run_plan({"campaign": {}})
    with pytest.raises(ValueError, match="invalid campaign"):
        lane.build_delegation_prompt({"campaign": {}})


def test_codemod_filing_backlog_and_cli(tmp_path, monkeypatch, capsys) -> None:
    def failed(*args, **kwargs):
        return subprocess.CompletedProcess(args[0], 1, "", "gh: denied")

    monkeypatch.setattr(lane.subprocess, "run", failed)
    with pytest.raises(RuntimeError, match="denied"):
        lane._gh(["api", "user"])

    def ok(*args, **kwargs):
        return subprocess.CompletedProcess(args[0], 0, '{"encoding":"utf-8","content":""}', "")

    monkeypatch.setattr(lane.subprocess, "run", ok)
    with pytest.raises(ValueError, match="unreadable"):
        lane._repo_file("owner/repo", ".gitignore", lane._gh)

    campaign = _gitignore_campaign()
    program_path = lane.campaign_program_path(campaign)
    lane._write_program(program_path, {"campaign_id": "someone-else"})
    with pytest.raises(ValueError, match="receipt identity mismatch"):
        lane.file_targets(campaign, gh=lambda args: (_ for _ in ()).throw(AssertionError(args)))

    marker = f"<!-- codemod-campaign:{campaign['campaign']['id']} -->"
    payload = base64.b64encode(b"README\n").decode()

    def gh_closed(args):
        if args[:1] == ["api"] and args[1].endswith("/contents/.gitignore"):
            return {"encoding": "base64", "content": payload}
        if args[:1] == ["issue"]:
            return [
                {
                    "number": 1,
                    "body": marker,
                    "state": "CLOSED",
                    "url": "https://example/1",
                }
            ]
        raise AssertionError(args)

    program_path.unlink()
    with pytest.raises(ValueError, match="closed campaign issue"):
        lane.file_targets(campaign, gh=gh_closed)

    def gh_ambiguous(args):
        if args[:1] == ["api"] and args[1].endswith("/contents/.gitignore"):
            return {"encoding": "base64", "content": payload}
        if args[:1] == ["issue"]:
            return [
                {"number": 1, "body": marker, "state": "OPEN", "url": "https://example/1"},
                {"number": 2, "body": marker, "state": "OPEN", "url": "https://example/2"},
            ]
        raise AssertionError(args)

    with pytest.raises(ValueError, match="ambiguous or incomplete"):
        lane.file_targets(campaign, gh=gh_ambiguous)

    import backlog

    monkeypatch.setattr(backlog, "load_scoped_blockers", lambda: {})
    lane._write_program(program_path, {"campaign_id": "other"})
    with pytest.raises(ValueError, match="receipt identity mismatch"):
        lane.campaign_backlog(campaign, gh=lambda args: (_ for _ in ()).throw(AssertionError(args)))
    lane._write_program(program_path, {"campaign_id": campaign["campaign"]["id"], "repos": {}})
    with pytest.raises(ValueError, match="not filed"):
        lane.campaign_backlog(campaign, gh=lambda args: [])
    lane._write_program(
        program_path,
        {
            "campaign_id": campaign["campaign"]["id"],
            "repos": {"owner/repo": {"state": "filed"}},
        },
    )
    with pytest.raises(ValueError, match="missing target receipt"):
        lane.campaign_backlog(campaign, gh=lambda args: [])
    lane._write_program(
        program_path,
        {
            "campaign_id": campaign["campaign"]["id"],
            "repos": {"owner/repo": {"state": "filed", "target": "owner/repo#7"}},
        },
    )

    def gh_backlog(args):
        if args[:2] == ["issue", "view"]:
            return {
                "number": 7,
                "state": "OPEN",
                "title": "ignores",
                "body": marker + "\n",
                "labels": [],
            }
        if args[:2] == ["pr", "list"]:
            return [
                {"number": i, "state": "OPEN", "closingIssuesReferences": []} for i in range(100)
            ]
        raise AssertionError(args)

    with pytest.raises(ValueError, match="incomplete PR linkage"):
        lane.campaign_backlog(campaign, gh=gh_backlog)

    def explode(*_args, **_kwargs):
        raise RuntimeError("heartbeat down")

    monkeypatch.setattr(capabilities, "production_heartbeat", explode)
    assert lane.main(["--goal", "rename symbols"]) == 0
    assert "refactor-campaign" in capsys.readouterr().out

    bad = tmp_path / "bad.json"
    bad.write_text("{")
    assert lane.main(["--file-targets", str(bad)]) == 1
    valid = tmp_path / "valid.json"
    valid.write_text(json.dumps(_valid_campaign()))
    assert lane.main(["--validate", str(valid)]) == 0
    assert "Validation PASSED" in capsys.readouterr().out
    invalid = copy.deepcopy(_valid_campaign())
    invalid["campaign"]["id"] = "Bad Slug"
    invalid_path = tmp_path / "invalid.json"
    invalid_path.write_text(json.dumps(invalid))
    assert lane.main(["--validate", str(invalid_path)]) == 1
    assert "Validation FAILED" in capsys.readouterr().err
    assert lane.main(["--plan", str(bad)]) == 1
    assert lane.main(["--plan", str(valid), "--emit-delegation-prompt"]) == 0
    planned = capsys.readouterr().out
    assert "delegation prompt" in planned and "Manual review required" in planned

    ready = _gitignore_campaign()
    ready.pop("source_target", None)
    ready_path = tmp_path / "ready.json"
    ready_path.write_text(json.dumps(ready))
    encoded = base64.b64encode(("\n".join(lane.IGNORE_ENTRIES) + "\n").encode()).decode()

    def serve_complete(*args, **kwargs):
        return subprocess.CompletedProcess(
            args[0],
            0,
            json.dumps({"encoding": "base64", "content": encoded}),
            "",
        )

    monkeypatch.setattr(lane.subprocess, "run", serve_complete)
    lane.campaign_program_path(ready).unlink(missing_ok=True)
    assert lane.main(["--file-targets", str(ready_path)]) == 0
    assert "already-complete" in capsys.readouterr().out


def test_codemod_selftest_truncation_does_not_spawn(monkeypatch) -> None:
    seen = []

    def fake_run(cmd, **kwargs):
        seen.append(cmd)
        text = cmd if isinstance(cmd, str) else " ".join(cmd)
        if "cap=50" in text:
            stdout = "\n".join(f"f{i}.py" for i in range(7)) + "\n"
        elif "cap=5" in text:
            stdout = (
                "\n".join(f"f{i}.py" for i in range(5))
                + "\n# TRUNCATED: 7 files in scope, listed 5\n"
            )
        else:
            raise AssertionError(text)
        return subprocess.CompletedProcess(cmd, 0, stdout, "")

    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr("subprocess.run", fake_run)
    lane._selftest()
    assert seen and all(isinstance(cmd, str) and cmd.startswith("rg --files") for cmd in seen)
