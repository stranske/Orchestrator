#!/usr/bin/env python3
"""Deterministic end-to-end coverage for causal experiment arm identity."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import exp_abcd
import feedback
import judge_reliability
import objective_anchor
import periodic_report
import strategy_experiment


def _v2_meta(exp_id: str, arms: list[dict], repo: str = "o/r") -> dict:
    normalized, members = exp_abcd._normalize_arm_members(arms)
    return {
        "schema_version": 2,
        "repo": repo,
        "base": "main",
        "base_sha": "base-sha",
        "exp_id": exp_id,
        "task_type": "implement",
        "agents": list(dict.fromkeys(member["agent"] for member in members)),
        "arms": normalized,
        "members": members,
    }


def test_same_agent_arms_do_not_collide(tmp_path, monkeypatch) -> None:
    """The named deliberate-break test covers every prepared artifact identity."""
    exp_id = "same-agent-arms"
    arms = [
        {
            "arm_id": f"arm-{idx}",
            "strategy": "single",
            "agents": ["codex"],
            "profile_id": f"codex-profile-{idx}",
        }
        for idx in range(1, 4)
    ]
    spec = tmp_path / "spec.md"
    spec.write_text("frozen")
    canon = tmp_path / "canonical"
    canon.mkdir()
    monkeypatch.setattr(exp_abcd, "EXP_DIR", tmp_path / "experiments")
    monkeypatch.setattr(exp_abcd.provision, "WORKTREES_DIR", tmp_path / "worktrees")
    monkeypatch.setattr(exp_abcd.provision, "ensure_canonical", lambda _repo: canon)
    monkeypatch.setattr(exp_abcd.provision, "base_branch", lambda _repo: "main")

    def fake_run(argv, check=False):
        if "worktree" in argv and "add" in argv:
            worktree = Path(argv[-2])
            (worktree / ".git").mkdir(parents=True)
        return SimpleNamespace(stdout="base-sha\n", stderr="", returncode=0)

    recorded = []
    monkeypatch.setattr(exp_abcd.provision, "_run", fake_run)
    monkeypatch.setattr(exp_abcd.feedback, "record_run", lambda *a, **kw: recorded.append((a, kw)))
    monkeypatch.setattr(exp_abcd, "_spawn", lambda *a, **kw: len(recorded) + 100)
    result = exp_abcd.prepare_arms("o/r", str(spec), exp_id, arms)

    for key in ("worktree", "branch", "log", "diff", "run_id", "member_id"):
        values = [row[key] for row in result["launched"]]
        assert len(set(values)) == 3, f"arm artifacts collided for codex: {key}={values}"
    assert len(recorded) == 3
    for _args, kwargs in recorded:
        routing = kwargs["routing_metadata"]
        assert routing["experiment_arm_id"] and routing["experiment_member_id"]
        assert routing["profile_id"].startswith("codex-profile-")
    meta = json.loads((exp_abcd.exp_paths(exp_id) / "meta.json").read_text())
    assert [m["member_id"] for m in exp_abcd.experiment_members(meta)] == [
        row["member_id"] for row in result["launched"]
    ]


def test_parallel_members_have_independent_phase_artifacts(tmp_path, monkeypatch) -> None:
    exp_id = "parallel-members"
    meta = _v2_meta(
        exp_id,
        [{"arm_id": "parallel", "strategy": "parallel", "agents": ["claude", "cursor"]}],
    )
    edir = tmp_path / exp_id
    edir.mkdir()
    (edir / "meta.json").write_text(json.dumps(meta))
    monkeypatch.setattr(exp_abcd, "EXP_DIR", tmp_path)
    monkeypatch.setattr(exp_abcd.provision, "WORKTREES_DIR", tmp_path / "worktrees")
    watched = []

    def fake_watch(**kwargs):
        watched.append((kwargs["agent"], kwargs["worktree"], kwargs["log"]))
        return {"signals": {}, "state": "exited", "recommended_action": "collect", "hints": []}

    def fake_run(argv, check=False):
        if "merge-base" in argv:
            return SimpleNamespace(stdout="base\n", stderr="", returncode=0)
        if "diff" in argv:
            token = Path(argv[2]).name
            return SimpleNamespace(
                stdout=f"diff --git a/{token} b/{token}\n--- a/{token}\n+++ b/{token}\n",
                stderr="",
                returncode=0,
            )
        return SimpleNamespace(stdout="", stderr="", returncode=0)

    monkeypatch.setattr(exp_abcd.watch, "classify_lane", fake_watch)
    monkeypatch.setattr(exp_abcd.provision, "_run", fake_run)
    status = exp_abcd.status(exp_id)
    collected = exp_abcd.collect("o/r", exp_id)
    assert len(status["members"]) == len(watched) == 2
    assert len({row[1] for row in watched}) == len({row[2] for row in watched}) == 2
    assert set(collected["diffs"]) == {member["member_id"] for member in meta["members"]}
    assert len({row["path"] for row in collected["diffs"].values()}) == 2


def _write_experiment(
    root: Path, exp_id: str, meta: dict, *, log_names: list[str], idle_s: int = 3600
) -> Path:
    """One experiment directory as the launchers leave it, aged so followup sees it as idle."""
    import os
    import time

    edir = root / exp_id
    edir.mkdir(parents=True)
    (edir / "meta.json").write_text(json.dumps(meta))
    (edir / "spec.md").write_text("SPEC")
    old = time.time() - idle_s
    for name in log_names:
        log = edir / name
        log.write_text("done")
        os.utime(log, (old, old))
    os.utime(edir / "meta.json", (old, old))
    return edir


def test_followup_collects_and_evaluates_a_v2_manifest(tmp_path, monkeypatch) -> None:
    """The v2 lifecycle's missing half: eligibility was measured per AGENT, not per MEMBER.

    `research_v2_arms()` feeds v2 manifests to the tick and backfill launchers and `prepare_arms()`
    writes one log per member (`<member_id>.log`), but followup listed `meta["agents"]` and looked
    for `<agent>.log`. Those names never coincide for a v2 member, so every v2 experiment failed the
    `all(log.exists())` gate, never reached collect/evaluate, and produced no `evaluations_v2`
    evidence at all -- the exact outcome the v2 identity work existed to prevent.

    Deliberate break for this gate: restore `logs = [edir / f"{a}.log" for a in meta["agents"]]` and
    the v2 experiment below is skipped while the legacy one still passes, which is precisely the
    half-fixed state that made this invisible.
    """
    monkeypatch.setenv("ORCH_FOLLOWUP_SHIP_GATE", "0")
    monkeypatch.setenv("ORCH_RESEARCH_ARM", "1")
    monkeypatch.setattr(exp_abcd, "EXP_DIR", tmp_path)
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "brain.db")
    monkeypatch.setattr(exp_abcd.capabilities, "production_heartbeat", lambda *a, **kw: None)

    # The manifest the production launchers build, via the same function they call.
    v2_meta = _v2_meta("v2-exp", exp_abcd.research_v2_arms(["codex", "cursor"]))
    members = exp_abcd.experiment_members(v2_meta)
    assert [m["legacy"] for m in members] == [False, False], members
    member_logs = [exp_abcd.exp_log_path(m["agent"], m["member_id"]) for m in members]
    # The names really are distinct from the agent names, which is why the old gate never matched.
    assert not ({"codex.log", "cursor.log"} & set(member_logs)), member_logs
    _write_experiment(tmp_path, "v2-exp", v2_meta, log_names=member_logs)

    # A LEGACY experiment alongside it: a recovered directory has no members, and its log IS
    # `<agent>.log`. Both shapes must stay eligible through one code path.
    _write_experiment(
        tmp_path,
        "legacy-exp",
        {"repo": "o/r", "base": "main", "agents": ["codex"]},
        log_names=["codex.log"],
    )

    collected: list[str] = []
    evaluated: list[str] = []

    def fake_collect(repo, exp_id):
        collected.append(exp_id)
        diffs = {
            m["member_id"]: {"bytes": 40}
            for m in exp_abcd.experiment_members(
                json.loads((tmp_path / exp_id / "meta.json").read_text())
            )
        }
        return {"exp_id": exp_id, "diffs": diffs}

    def fake_evaluate(repo, spec_file, exp_id, evs, timeout=600):
        evaluated.append(exp_id)
        (tmp_path / exp_id / "eval-maps.json").write_text("{}")
        return {"exp_id": exp_id, "evaluators": ["claude"], "objective_anchors": {"anchored": []}}

    out = exp_abcd.followup(
        max_experiments=2,
        collect_fn=fake_collect,
        evaluate_fn=fake_evaluate,
        subject_lifecycle_fn=lambda *a, **kw: True,
    )
    assert sorted(collected) == ["legacy-exp", "v2-exp"], out
    assert sorted(evaluated) == ["legacy-exp", "v2-exp"], out
    assert out["eligible"] == 2, out
    assert {row["exp_id"]: row["evaluated"] for row in out["processed"]} == {
        "v2-exp": True,
        "legacy-exp": True,
    }, out
    # Idempotent afterwards, by the same eval-maps.json contract the legacy path uses.
    again = exp_abcd.followup(
        max_experiments=2,
        collect_fn=fake_collect,
        evaluate_fn=fake_evaluate,
        subject_lifecycle_fn=lambda *a, **kw: True,
    )
    assert again["processed"] == [] and again["eligible"] == 0, again


def test_branch_recovery_resolves_the_v2_member_branch(tmp_path, monkeypatch) -> None:
    """A reclaimed v2 worktree recovers from `exp/<exp_id>-<member_id>`, not `exp/<exp_id>-<agent>`.

    `collect()` falls back to the shared per-repo branch store when the worktree is gone, and that
    fallback is what keeps an aged experiment's evidence alive. It passed only the agent, so for a
    v2 member it asked for a branch that cannot exist, read that as "the branch is gone", and
    followup then wrote `followup-skip.json` -- marking evidence permanently lost while the commits
    sat intact in the store.
    """
    import experiment_recovery

    exp_id = "v2-recovery"
    meta = _v2_meta(exp_id, exp_abcd.research_v2_arms(["codex"]))
    member = exp_abcd.experiment_members(meta)[0]
    edir = tmp_path / exp_id
    edir.mkdir()
    (edir / "meta.json").write_text(json.dumps(meta))
    monkeypatch.setattr(exp_abcd, "EXP_DIR", tmp_path)
    # No worktree on disk -> the branch-recovery path is the one under test.
    monkeypatch.setattr(exp_abcd.provision, "WORKTREES_DIR", tmp_path / "gone")
    store = tmp_path / "store"
    store.mkdir()
    monkeypatch.setattr(experiment_recovery, "repo_store", lambda _repo: store)
    asked: list[tuple] = []

    def fake_git(_store, *args, timeout=None):
        asked.append(args)
        if args[0] == "rev-parse":
            # Only the real member branch exists in the store.
            return (
                "sha\n"
                if args[-1] == exp_abcd.exp_branch(exp_id, "codex", member["member_id"])
                else ""
            )
        if args[0] == "merge-base":
            return "mergebase\n"
        return "diff --git a/x b/x\n"

    monkeypatch.setattr(experiment_recovery, "_git", fake_git)
    got = exp_abcd.collect("o/r", exp_id)
    assert got["diffs"][member["member_id"]]["source"] == "branch", got
    assert got["diffs"][member["member_id"]]["bytes"] > 0, got
    branches = [args[-1] for args in asked if args[0] == "rev-parse"]
    assert branches == [exp_abcd.exp_branch(exp_id, "codex", member["member_id"])], branches
    assert exp_abcd.exp_branch(exp_id, "codex") not in branches, branches


def test_arm_evaluate_dual_writes_exact_v2_and_parent_legacy(tmp_path, monkeypatch) -> None:
    exp_id = "evaluate-v2"
    meta = _v2_meta(
        exp_id,
        [
            {"arm_id": "sol", "agents": ["codex"], "profile_id": "sol"},
            {"arm_id": "terra", "agents": ["codex"], "profile_id": "terra"},
        ],
    )
    edir = tmp_path / exp_id
    edir.mkdir()
    (edir / "meta.json").write_text(json.dumps(meta))
    spec = edir / "spec.md"
    spec.write_text("frozen")
    for member in meta["members"]:
        (edir / exp_abcd.exp_diff_path(member["agent"], member["member_id"])).write_text(
            f"diff --git a/{member['member_id']} b/{member['member_id']}\n"
        )
    monkeypatch.setattr(exp_abcd, "EXP_DIR", tmp_path)
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "feedback.db")
    monkeypatch.setenv("ORCH_OBJECTIVE_ANCHOR", "0")
    monkeypatch.setattr(exp_abcd, "_record_execution_start", lambda *a, **kw: 1)
    monkeypatch.setattr(exp_abcd, "_record_execution_complete", lambda *a, **kw: None)
    # ISOLATION, not a skip. FakePopen below replaces subprocess.Popen for the whole call, and
    # `_eval_command` resolves each seat's model on the way — which spawns a CLI catalog probe
    # when the advertised-model cache is cold. That probe then hits FakePopen, whose `stdout` is
    # PIPE (an int), and dies on `stdout.write`. Warm cache here, cold on a fresh machine: it is
    # why this test was red on the first CI run and green locally.
    #
    # ORCH_MODEL_PROBE=0 is adapters' own documented kill-switch — catalog probes off, pinned
    # models only, no subprocess — so the test becomes hermetic instead of skipping. It asserts
    # exactly what it asserted before, and now asserts it on every machine.
    monkeypatch.setenv("ORCH_MODEL_PROBE", "0")
    monkeypatch.setattr(exp_abcd.adapters, "_ADVERTISED_MEMO", {})

    class FakePopen:
        def __init__(self, *_args, stdout=None, **_kwargs):
            stdout.write(json.dumps({"scores": {"A": 8.0, "B": 6.0}, "notes": {}}))
            stdout.flush()

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

        def kill(self):
            raise AssertionError("completed deterministic evaluator must not be killed")

    monkeypatch.setattr(exp_abcd.subprocess, "Popen", FakePopen)
    result = exp_abcd.evaluate(
        "o/r", str(spec), exp_id, ["claude", "codex", "cursor", "vibe"], timeout=1
    )
    assert set(result["implementers"]) == {member["member_id"] for member in meta["members"]}
    with feedback._conn() as c:
        exact = c.execute(
            "SELECT implementer_arm_id, implementer_member_id, implementer_profile_id, "
            "evaluator_id, implementation_agent FROM evaluations_v2 ORDER BY 1,4"
        ).fetchall()
        legacy = c.execute(
            "SELECT implementer, evaluator, score FROM evaluations ORDER BY evaluator"
        ).fetchall()
    assert len(exact) == 8 and {row[1] for row in exact} == {
        member["member_id"] for member in meta["members"]
    }
    assert {row[2] for row in exact} == {"sol", "terra"}
    assert len(legacy) == 4 and {row[0] for row in legacy} == {"codex"}
    assert all(row[2] == 7.0 for row in legacy), legacy


def test_neutral_judge_from_eval_maps_changes_synthesis_winner(tmp_path, monkeypatch) -> None:
    exp_id = "neutral-judge"
    edir = tmp_path / exp_id
    edir.mkdir()
    monkeypatch.setattr(exp_abcd, "EXP_DIR", tmp_path)
    meta = {"repo": "o/r", "base": "main", "agents": ["claude", "codex"], "exp_id": exp_id}
    maps = {
        "claude": {"A": "claude", "B": "codex"},
        "codex": {"A": "claude", "B": "codex"},
        "cursor": {"A": "claude", "B": "codex"},
    }
    verdicts = {
        "claude": {"scores": {"A": 9, "B": 5}, "notes": {}},
        "codex": {"scores": {"A": 9, "B": 5}, "notes": {}},
        "cursor": {"scores": {"A": 0, "B": 10}, "notes": {}},
    }
    (edir / "meta.json").write_text(json.dumps(meta))
    (edir / "eval-maps.json").write_text(json.dumps(maps))
    for judge, verdict in verdicts.items():
        (edir / f"eval-out-{judge}.txt").write_text(json.dumps(verdict))
    reliability = {
        "judges": {judge: {"ready": True, "weight": 1.0} for judge in maps},
        "ready_judge_count": 3,
    }
    without_neutral = exp_abcd._winner_and_harvest(
        {k: verdicts[k] for k in ("claude", "codex")},
        {k: maps[k] for k in ("claude", "codex")},
        reliability=reliability,
    )
    assert without_neutral["winner"] == "claude"
    result = exp_abcd.synthesize(
        "o/r",
        exp_id,
        gate={"decision": "discard"},
        reliability=reliability,
    )
    assert result["discard"] is True
    assert result["ranking"][0][0] == "codex", result


def test_legacy_metadata_reader_is_agent_only() -> None:
    members = exp_abcd.experiment_members({"agents": ["codex", "cursor"]})
    assert members == [
        {
            "arm_id": None,
            "member_id": "codex",
            "agent": "codex",
            "profile_id": None,
            "strategy": "legacy_agent",
            "legacy": True,
        },
        {
            "arm_id": None,
            "member_id": "cursor",
            "agent": "cursor",
            "profile_id": None,
            "strategy": "legacy_agent",
            "legacy": True,
        },
    ]


def test_strategy_shared_member_and_synthesis_cost_are_arm_scoped(tmp_path) -> None:
    spec = tmp_path / "spec.md"
    spec.write_text("frozen")
    plan = strategy_experiment.build_strategy_plan(
        "o/r",
        str(spec),
        "strategy-cost",
        [
            {"strategy": "single", "agents": ["claude"]},
            {"strategy": "parallel", "agents": ["claude", "cursor"], "synthesize": True},
        ],
        exp_dir=tmp_path / "experiments",
    )
    claude_ids = [
        member["member_id"]
        for arm in plan["arms"]
        for member in arm["members"]
        if member["agent"] == "claude"
    ]
    assert len(set(claude_ids)) == 2
    parallel = plan["arms"][1]
    rows = [
        {"run_id": run_id, "tokens_in": 10, "tokens_out": 2, "cost_usd": 0.5, "latency_s": 3}
        for run_id in parallel["attempt_run_ids"]
    ]
    cost = strategy_experiment.strategy_arm_costs(plan, rows)[parallel["arm_id"]]
    assert cost["observed_attempt_count"] == 3
    assert cost["tokens_in"] == 30 and cost["tokens_out"] == 6
    assert cost["cost_usd"] == 1.5 and cost["latency_s"] == 9


def test_objective_anchor_uses_exact_member_and_profile(tmp_path, monkeypatch) -> None:
    exp_id = "anchor-v2"
    meta = _v2_meta(
        exp_id,
        [
            {"arm_id": "sol", "agents": ["codex"], "profile_id": "sol-profile"},
            {"arm_id": "terra", "agents": ["codex"], "profile_id": "terra-profile"},
        ],
    )
    edir = tmp_path / "experiments" / exp_id
    edir.mkdir(parents=True)
    (edir / "meta.json").write_text(json.dumps(meta))
    for member in meta["members"]:
        (edir / exp_abcd.exp_diff_path(member["agent"], member["member_id"])).write_text(
            "+++ b/x.py\n"
        )
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "feedback.db")
    result = objective_anchor.anchor_experiment(
        exp_id,
        exp_dir=tmp_path / "experiments",
        signals_fn=lambda *_args: {
            "applies": True,
            "compile_ok": True,
            "probe": {"patched_pass": True, "base_pass": False},
        },
    )
    assert len(result["anchored"]) == 2
    with feedback._conn() as c:
        rows = c.execute("SELECT ref, human_verdict FROM human_calibration ORDER BY ref").fetchall()
    assert {row[0] for row in rows} == {
        f"{exp_id}:{member['member_id']}" for member in meta["members"]
    }
    payloads = [json.loads(row[1]) for row in rows]
    assert {row["profile_id"] for row in payloads} == {"sol-profile", "terra-profile"}
    assert all(
        row["agent"] == "codex" and row["member_id"] == row["implementer"] for row in payloads
    )


def test_judge_reliability_prefers_exact_then_agent_fallback(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "feedback.db")
    for exp, member, evaluator_id, score in (
        ("E1", "member-sol", "profile-sol", 8.0),
        ("E2", "member-terra", "profile-terra", 7.0),
    ):
        feedback.record_evaluation_v2(
            experiment_id=exp,
            implementer_arm_id=member.split("-")[-1],
            implementer_member_id=member,
            implementer_profile_id=member.split("-")[-1],
            implementation_agent="worker",
            evaluator_id=evaluator_id,
            evaluator_profile_id=evaluator_id,
            evaluator_agent="codex",
            score=score,
        )
        feedback.record_human_calibration(
            f"{exp}:{member}",
            json.dumps({"experiment_id": exp, "implementer": member, "score": score}),
        )
    report = judge_reliability.summarize(min_comparisons=2, min_experiments=2)
    assert report["evaluator_fallbacks"] == {
        "profile-sol": "codex",
        "profile-terra": "codex",
    }
    assert report["judges"]["profile-sol"]["ready"] is False
    assert report["agent_fallback_judges"]["codex"]["ready"] is True
    weights = judge_reliability.weights_from_summary(report, evaluators=["profile-sol"])
    assert weights["profile-sol"] == report["agent_fallback_judges"]["codex"]["weight"]


def test_feedback_v2_rejects_incomplete_identity(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "feedback.db")
    try:
        feedback.record_evaluation_v2(
            experiment_id="E",
            implementer_arm_id="A",
            implementer_member_id="",
            implementation_agent="codex",
            evaluator_id="judge",
            evaluator_agent="claude",
            score=8,
        )
    except ValueError as exc:
        assert "exact" in str(exc)
    else:
        raise AssertionError("incomplete causal identity was accepted")


def test_feedback_migrates_arm_only_prototype_without_losing_row() -> None:
    c = sqlite3.connect(":memory:")
    try:
        c.executescript(feedback.SCHEMA)
        c.execute("DROP TABLE evaluations_v2")
        c.execute(
            "CREATE TABLE evaluations_v2 ("
            "experiment_id TEXT, implementer_arm TEXT, evaluator_arm TEXT, profile_id TEXT, "
            "score REAL, rank INTEGER, verdict TEXT, ts INTEGER, arm_id TEXT, "
            "implementation_agent TEXT, evaluator_agent TEXT, "
            "PRIMARY KEY (experiment_id, implementer_arm, evaluator_arm))"
        )
        c.execute(
            "INSERT INTO evaluations_v2 VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            ("E", "arm-a", "judge-a", "profile-a", 8.0, 1, None, 1, "arm-a", "codex", "claude"),
        )
        feedback._migrate_schema(c)
        cols = {row[1] for row in c.execute("PRAGMA table_info(evaluations_v2)")}
        assert {"implementer_member_id", "evaluator_id", "evaluator_profile_id"}.issubset(cols)
        row = c.execute(
            "SELECT experiment_id, implementer_arm_id, implementer_member_id, evaluator_id "
            "FROM evaluations_v2"
        ).fetchone()
        assert row == ("E", "arm-a", "arm-a", "judge-a")
    finally:
        c.close()


def test_feedback_migrates_partial_target_schema_without_losing_row() -> None:
    c = sqlite3.connect(":memory:")
    try:
        c.executescript(feedback.SCHEMA)
        c.execute("DROP TABLE evaluations_v2")
        c.execute(
            "CREATE TABLE evaluations_v2 (experiment_id TEXT, implementer_arm_id TEXT, "
            "implementer_member_id TEXT, evaluator_id TEXT, score REAL)"
        )
        c.execute(
            "INSERT INTO evaluations_v2 VALUES (?,?,?,?,?)",
            ("E", "parallel", "member-1", "judge-1", 8.0),
        )
        feedback._migrate_schema(c)
        rows = c.execute(
            "SELECT experiment_id,implementer_arm_id,implementer_member_id,evaluator_id,score "
            "FROM evaluations_v2"
        ).fetchall()
        assert rows == [("E", "parallel", "member-1", "judge-1", 8.0)]
    finally:
        c.close()


def test_periodic_report_distinguishes_arms_shared_agent_and_missing_outcome(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "feedback.db")
    for arm_id, member_id in (("sol", "sol-member"), ("terra", "terra-member")):
        feedback.record_run(
            f"E:member:{member_id}",
            "o/r [exp E]",
            "implement",
            "codex",
            experiment_id="E",
            routing_metadata={
                "experiment_arm_id": arm_id,
                "experiment_member_id": member_id,
                "profile_id": arm_id,
            },
        )
    feedback.record_evaluation_v2(
        experiment_id="E",
        implementer_arm_id="sol",
        implementer_member_id="sol-member",
        implementer_profile_id="sol",
        implementation_agent="codex",
        evaluator_id="judge",
        evaluator_agent="claude",
        score=8,
    )
    report = periodic_report._experiment_identity_summary(30)
    assert [row["arm_id"] for row in report["implementation_arms"]] == ["sol"]
    assert report["shared_agents_across_arms"] == {"codex": ["E:sol", "E:terra"]}
    assert report["missing_arm_outcomes"] == [
        {
            "experiment_id": "E",
            "arm_id": "terra",
            "members": ["terra-member"],
            "reason": "no_exact_arm_evaluation",
        }
    ]


def test_periodic_report_marks_partial_parallel_arm_incomplete(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "feedback.db")
    for member_id in ("parallel-1", "parallel-2"):
        feedback.record_run(
            f"P:{member_id}",
            "o/r [exp P]",
            "implement",
            "codex",
            experiment_id="P",
            routing_metadata={
                "experiment_arm_id": "parallel",
                "experiment_member_id": member_id,
            },
        )
    feedback.record_evaluation_v2(
        experiment_id="P",
        implementer_arm_id="parallel",
        implementer_member_id="parallel-1",
        implementation_agent="codex",
        evaluator_id="judge",
        evaluator_agent="claude",
        score=8,
    )
    report = periodic_report._experiment_identity_summary(30)
    arm = report["implementation_arms"][0]
    assert arm["arm_outcome_complete"] is False
    assert arm["mean_score"] is None
    assert arm["mean_member_evaluation_score"] == 8.0
    assert arm["missing_members"] == ["parallel-2"]
    assert report["missing_arm_outcomes"] == [
        {
            "experiment_id": "P",
            "arm_id": "parallel",
            "members": ["parallel-1", "parallel-2"],
            "missing_members": ["parallel-2"],
            "reason": "incomplete_exact_member_evaluation",
        }
    ]
