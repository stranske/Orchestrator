"""Offline acceptance and failure-path proofs for the producer."""

import hashlib
import json
import random
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import exp_abcd
import experiment_producer as producer
import synthesis_promotion

ORIGINAL_GH_CALL = producer.gh_call


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ORCH_EXPERIMENT_PRODUCER", "1")
    monkeypatch.setenv("ORCH_CAPABILITIES_PATH", str(tmp_path / "capabilities.json"))
    monkeypatch.setattr(producer, "gh_call", lambda argv: {"error": "offline"})


def test_draw_rate_is_poisson_bounded():
    rng = random.Random(677)
    days = []
    for day in range(20_000):
        count = 0
        for hour in range(24):
            now = (day * 24 + hour + 1) * 3600
            count += producer.draw(now, now - 3600, 2, count, rng)["fire"]
        days.append(count)
    assert 1.82 <= sum(days) / len(days) <= 2.0
    assert max(days) <= 4


def test_coverage_cell_older_than_21_days_is_chosen_first():
    feed = [
        {"target": "a/b#1", "task_type": "implement"},
        {"target": "a/b#2", "task_type": "testgen"},
    ]
    cells = [
        {"task_type": "implement", "agent": "a", "age_days": 2},
        {"task_type": "testgen", "agent": "b", "age_days": 22},
    ]
    assert producer.choose_subject(feed, [], cells, random.Random(1)) == {
        "subject": feed[1],
        "reason": "coverage",
    }


def test_forum_and_stalest_challenger_keep_exact_arm_identity():
    old = {"target": "a/b#1", "task_type": "implement", "created_at": "2026-01-01"}
    assert producer.choose_subject([old], [old], [], random.Random(1))["reason"] == "forum"
    cells = [
        {"task_type": "implement", "agent": a, "age_days": age}
        for a, age in [("codex", 2), ("claude", 22), ("vibe", 4)]
    ]
    arms = producer.choose_arms("implement", ["codex", "vibe", "claude"], cells, random.Random(1))
    assert [a["agents"] for a in arms] == [["codex"], ["claude"], ["codex"]]
    assert len({a["arm_id"] for a in arms}) == 3
    assert "brief" in arms[2]


def test_pacing_skip_prints_blocking_and_drainable(capsys):
    cells = [{"age_days": 22, "pacing": "shed"}, {"age_days": 30, "pacing": "ok"}]
    result = producer.draw(3600, 0, 2, 0, random.Random(1), pools={"codex": "shed"}, cells=cells)
    assert result["fire"] is False
    assert (
        capsys.readouterr().err
        == "skipped: pacing codex=shed; coverage debt 2 cells > 21d, drainable 1\n"
    )
    producer.draw(3600, 0, 2, 0, random.Random(1), pools={"codex": "shed"}, cells=[])
    assert (
        capsys.readouterr().err
        == "skipped: pacing codex=shed; coverage debt 0 cells > 21d, drainable 0\n"
    )


def test_cadence_stamps_skips_resets_daily_cap(tmp_path):
    rng = SimpleNamespace(random=lambda: 0)
    producer.cadence_draw(86400, 2, rng, pools={"x": "shed"})
    state = producer.read_state()
    assert state["last_draw_ts"] == 86400
    assert state["launched_today"] == 0
    for hour in range(1, 6):
        if producer.cadence_draw(86400 + hour * 3600, 2, rng)["fire"]:
            producer.record_launch(86400 + hour * 3600)
    assert producer.read_state()["launched_today"] == 4
    producer.cadence_draw(2 * 86400, 2, rng)
    producer.record_launch(2 * 86400)
    assert producer.read_state()["launched_today"] == 1
    producer.cadence_draw(2 * 86400 + 3600, 2, rng, skip_reason="no_subject")
    assert producer.read_state()["last_draw_ts"] == 2 * 86400 + 3600
    assert producer.read_state()["skips"]["no_subject"] == 1


def test_member_push_is_refused(tmp_path, monkeypatch):
    """Check the actual spawn environment without launching a process or pushing."""
    monkeypatch.setattr(exp_abcd.adapters, "refuse_broad_workspace", lambda *a: None)
    monkeypatch.setattr(exp_abcd.adapters, "build_command", lambda *a, **k: ["stub-agent"])
    monkeypatch.setattr(exp_abcd, "_record_execution_start", lambda *a: 0)
    monkeypatch.setattr(exp_abcd, "_completion_cmd", lambda *a: "stub")
    monkeypatch.setattr(exp_abcd, "_wrapped", lambda *a: "stub")
    monkeypatch.setattr(exp_abcd, "_record_execution_complete", lambda *a: None)
    observed = []

    def popen(argv, **kwargs):
        observed.append(kwargs["env"])
        return SimpleNamespace(pid=123)

    monkeypatch.setattr(exp_abcd.subprocess, "Popen", popen)
    exp_abcd._spawn("codex", "full", "test", tmp_path, tmp_path / "member.log")
    assert len(observed) == 1
    env = observed[0]
    settings = {
        env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"]
        for i in range(int(env["GIT_CONFIG_COUNT"]))
    }
    assert settings["remote.origin.pushurl"] == "file:///dev/null/orch-no-push"


def test_member_remote_assertion_stops_on_pushed_ref(monkeypatch):
    monkeypatch.setattr(exp_abcd.provision, "ensure_canonical", lambda repo: Path("/tmp/unused"))
    assert (
        exp_abcd.assert_no_member_push(
            "a/b",
            "demo",
            run_fn=lambda *a, **k: SimpleNamespace(
                returncode=0, stdout="sha refs/heads/exp/demo-x"
            ),
        )
        == "member_pushed"
    )
    assert (
        exp_abcd.assert_no_member_push(
            "a/b", "demo", run_fn=lambda *a, **k: SimpleNamespace(returncode=0, stdout="")
        )
        is None
    )


def fixture_experiment(tmp_path, monkeypatch):
    root = tmp_path / "experiment"
    root.mkdir()
    arms, members = exp_abcd._normalize_arm_members(
        producer.choose_arms(
            "implement",
            ["codex", "claude"],
            [{"task_type": "implement", "agent": a, "age_days": 30} for a in ["codex", "claude"]],
            random.Random(2),
        )
    )
    meta = {
        "producer": "experiment_producer",
        "repo": "a/b",
        "subject": "a/b#3",
        "base": "main",
        "base_sha": "base",
        "launched_ts": 1000,
        "schema_version": 2,
        "arms": arms,
        "members": members,
        "spec_provenance": "issue_body",
    }
    (root / "spec.md").write_text("Gate: `python3 -m pytest -q tests/test_fix.py::test_fix`")
    meta["spec_sha256"] = hashlib.sha256((root / "spec.md").read_bytes()).hexdigest()
    (root / "meta.json").write_text(json.dumps(meta))
    monkeypatch.setattr(
        (
            synthesis_promotion.research_subjects
            if hasattr(synthesis_promotion, "research_subjects")
            else __import__("research_subjects")
        ),
        "mark_lifecycle",
        lambda *a, **k: None,
    )
    return root, meta, {"ranking": [(m["member_id"], 9 - n) for n, m in enumerate(members)]}


def test_all_local_verifications_fail_no_pr(tmp_path, monkeypatch):
    root, meta, evaluation = fixture_experiment(tmp_path, monkeypatch)
    calls = []
    result = synthesis_promotion.deliver_producer_winner(
        root,
        meta,
        evaluation,
        verify_fn=lambda *a, **k: {"ok": False},
        git_fn=lambda *a, **k: SimpleNamespace(returncode=0, stdout="commit", stderr=""),
        gh_fn=lambda argv: calls.append(argv) or {"stdout": ""},
    )
    assert result["status"] == "no_winner"
    assert not any("push" in c or "create" in c for c in calls)
    assert calls[-1] == [
        "gh",
        "api",
        "-X",
        "DELETE",
        "repos/a/b/issues/3/labels/status%3Ain-progress",
    ]


@pytest.mark.parametrize("failure", ["collision", "unavailable", "success"])
def test_direct_winner_delivery_retry_and_collision(tmp_path, monkeypatch, failure):
    root, meta, evaluation = fixture_experiment(tmp_path, monkeypatch)
    calls, pushes = [], []

    def gh(argv):
        calls.append(argv)
        if argv[2:4] == ["-X", "POST"] and argv[4].endswith("/pulls"):
            return {
                "stdout": json.dumps({"html_url": "https://github.com/a/b/pull/9", "number": 9})
            }
        if argv[2:4] in (["-X", "POST"], ["-X", "DELETE"]):
            return {"stdout": ""}
        if "issues/3" in argv[-1]:
            return {"stdout": json.dumps({"state": "open", "labels": []})}
        if failure == "unavailable":
            return {"error": "quota"}
        return {
            "stdout": json.dumps(
                [{"number": 8, "body": "Closes #3"}] if failure == "collision" else []
            )
        }

    def run(argv, **kwargs):
        if "push" in argv:
            pushes.append(argv)
        return SimpleNamespace(returncode=0, stdout="" if "status" in argv else "commit", stderr="")

    result = synthesis_promotion.deliver_producer_winner(
        root, meta, evaluation, verify_fn=lambda *a, **k: {"ok": True}, gh_fn=gh, git_fn=run
    )
    assert (
        result["status"]
        == {
            "collision": "discarded",
            "unavailable": "github_unavailable",
            "success": "delegated_or_pr",
        }[failure]
    )
    assert len(pushes) == int(failure == "success")
    if failure == "success":
        assert pushes[0][-1] == "HEAD:refs/heads/orch/exp-experiment"
        create = next(c for c in calls if c[2:4] == ["-X", "POST"] and c[4].endswith("/pulls"))
        assert "--draft" not in create
        assert "Closes a/b#3" in (root / "producer-pr.md").read_text()
        assert "experiment:experiment" in (root / "producer-pr.md").read_text()
        assert result["member_id"] == evaluation["ranking"][0][0]


def test_missing_frozen_spec_never_evaluates(tmp_path, monkeypatch):
    root, meta, _ = fixture_experiment(tmp_path, monkeypatch)
    (root / "spec.md").unlink()
    result = exp_abcd.producer_followup(
        root,
        meta,
        now=1100,
        min_idle_s=0,
        eval_timeout=1,
        collect_fn=lambda *a: pytest.fail("collected missing spec"),
        evaluate_fn=lambda *a: pytest.fail("paid for missing spec"),
    )
    assert result["reason"] == "missing_spec_recovery_refused"


@pytest.mark.parametrize("merged,expected", [(5, "fail"), (6, "pass")])
def test_trial_expires_once_and_requires_six_merged_winners(merged, expected):
    end, verdict = producer.trial_defaults(1000, active=True)
    assert verdict == "pending"
    assert producer.trial_defaults(1000 + 20 * 86400)[0] == end
    due = 1000 + (producer.COVERAGE_DAYS + producer.WINNER_MERGE_GRACE_DAYS) * 86400
    assert (
        producer.trial_verdict(due - 1, summary_fn=lambda: pytest.fail("early poll")) == "pending"
    )
    assert producer.trial_verdict(due, summary_fn=lambda: {"winner_merged": merged}) == expected


def test_verdict_flake_does_not_overwrite_pass(monkeypatch):
    producer.trial_defaults(1000, active=True)
    due = 1000 + 28 * 86400
    assert producer.trial_verdict(due, summary_fn=lambda: {"winner_merged": 6}) == "pass"
    calls = []
    monkeypatch.setattr(producer, "gh_call", lambda argv: calls.append(argv) or {"error": "flake"})

    def flaked_summary():
        producer.gh_call(["gh", "api", "stub"])
        return {"winner_merged": 0, "unknowns": ["merge_state_unknown"]}

    monkeypatch.setattr(producer, "producer_summary", flaked_summary)
    assert producer.trial_verdict(due + 86400) == "pass"
    assert calls == []
    assert producer.read_state()["trial_verdict"] == "pass"


@pytest.mark.parametrize("verdict", ["pass", "fail"])
def test_persisted_verdict_never_polls(monkeypatch, verdict):
    producer.save_state({"trial_verdict": verdict})
    monkeypatch.setattr(producer, "gh_call", lambda *a: pytest.fail("terminal verdict polled gh"))
    assert (
        producer.trial_verdict(summary_fn=lambda: pytest.fail("terminal verdict recomputed"))
        == verdict
    )


def test_unknown_verdict_retries_only_once_per_utc_day(capsys):
    producer.trial_defaults(1000, active=True)
    due = 1000 + 28 * 86400
    calls = []

    def summary():
        calls.append(True)
        return {"winner_merged": 0, "unknowns": ["merge_state_unknown", "merge_state_unknown"]}

    assert producer.trial_verdict(due, summary_fn=summary) == "pending"
    assert "unknown 2" in capsys.readouterr().err
    assert producer.read_state()["trial_unknown"] == 2
    assert producer.trial_verdict(due + 1, summary_fn=summary) == "pending"
    assert len(calls) == 1
    assert producer.read_state()["last_verdict_check"] == time.strftime("%Y%m%d", time.gmtime(due))
    next_day = (int(due) // 86400 + 1) * 86400
    assert producer.trial_verdict(next_day, summary_fn=summary) == "pending"
    assert len(calls) == 2
    assert (
        producer.trial_verdict(next_day + 86400, summary_fn=lambda: {"winner_merged": 6}) == "pass"
    )


def test_report_zero_values_are_present(monkeypatch):
    import switch_review

    monkeypatch.setattr(
        producer,
        "producer_summary",
        lambda: dict(
            launched=0,
            evaluated=0,
            winner_prs=0,
            winner_merged=0,
            cells_fresh=0,
            cells_total=0,
            skips={},
            unknowns=[],
        ),
    )
    assert (
        switch_review.experiment_producer_line()
        == "  experiment producer: launched 0, evaluated 0, winner_prs 0, winner_merged 0, post_window_launches=0, cells_fresh 0/0, skipped 0 (none); no_named_test=0 (file-only=0, ci-only=0, none=0), derived_tests=0, derive_failed=0; trial_end not_started, verdict pending, days_left 21; winner merge grace 7d"
    )


@pytest.mark.parametrize("enabled", ["0", "1"])
def test_producer_judges_are_two_neutral_agents_and_delivery_bypasses_synthesis(
    tmp_path, monkeypatch, enabled
):
    root, meta, ranking = fixture_experiment(tmp_path, monkeypatch)
    monkeypatch.setenv("ORCH_EXPERIMENT_PRODUCER", enabled)
    meta["launched_ts"] = time.time() - 10 * 3600
    (root / "meta.json").write_text(json.dumps(meta))
    monkeypatch.setattr(exp_abcd, "EXP_DIR", tmp_path)
    for member in meta["members"]:
        log = root / exp_abcd.exp_log_path(member["agent"], member["member_id"])
        log.write_text("done")
    monkeypatch.setattr(
        exp_abcd.research_usage_guard,
        "assess_and_record_opportunity",
        lambda **kwargs: {"eligible": True, "opportunity_id": "test"},
    )
    monkeypatch.setattr(
        exp_abcd.research_usage_guard, "update_opportunity_outcome", lambda *a, **k: None
    )
    monkeypatch.setattr(
        exp_abcd, "synthesize", lambda *a, **k: pytest.fail("producer entered synthesis")
    )
    monkeypatch.setattr(exp_abcd.judge_reliability, "summarize", lambda: {"judges": {}})
    dispatched = []

    def evaluate(repo, spec, exp_id, judges, **kwargs):
        assert Path(spec).read_text() == (root / "spec.md").read_text()
        assert len(judges) == 2
        assert not set(judges) & {m["agent"] for m in meta["members"]}
        dispatched.extend(judges)
        maps = {
            j: {chr(65 + i): member["member_id"] for i, member in enumerate(meta["members"])}
            for j in judges
        }
        (root / "eval-maps.json").write_text(json.dumps(maps))
        for j in judges:
            (root / f"eval-out-{j}.txt").write_text(json.dumps({"scores": {"A": 9, "B": 7}}))
        return {"evaluators": judges}

    delivered = []
    monkeypatch.setattr(
        exp_abcd.synthesis_promotion,
        "deliver_producer_winner",
        lambda *a, **k: delivered.append(a) or {"status": "delegated_or_pr"},
    )
    monkeypatch.setattr(exp_abcd, "assert_no_member_push", lambda *a: None)
    monkeypatch.setattr(exp_abcd, "_collected_candidate_diffs", lambda *a: {"member": "diff"})
    # Use the real followup driver, so the generic reconciliation loop is covered too.
    exp_abcd.followup(min_idle_s=0, collect_fn=lambda *a: {}, evaluate_fn=evaluate)
    assert len(dispatched) == 2
    assert len(delivered) == 1
    assert len(delivered[0][2]["ranking"]) == 2


def test_followup_member_push_stops_before_judging(tmp_path, monkeypatch):
    root, meta, _ = fixture_experiment(tmp_path, monkeypatch)
    for member in meta["members"]:
        (root / exp_abcd.exp_log_path(member["agent"], member["member_id"])).write_text("done")
    meta["launched_ts"] = time.time()
    result = exp_abcd.producer_followup(
        root,
        meta,
        now=time.time(),
        min_idle_s=0,
        eval_timeout=1,
        collect_fn=lambda *a: {},
        evaluate_fn=lambda *a, **k: pytest.fail("judged pushed member"),
        push_check_fn=lambda *a: "member_pushed",
    )
    assert result["status"] == "member_pushed"


@pytest.mark.parametrize("enabled", ["0", "1"])
def test_producer_guard_retains_missing_spec_and_budget_checks(tmp_path, monkeypatch, enabled):
    import sqlite3

    import research_usage_guard as guard

    db = sqlite3.connect(":memory:")
    root, meta, _ = fixture_experiment(tmp_path, monkeypatch)
    monkeypatch.setattr(exp_abcd, "exp_paths", lambda exp_id: root)
    monkeypatch.setattr(guard.capabilities, "production_heartbeat", lambda *a, **k: None)
    kwargs = dict(
        exp_id="experiment",
        repo="a/b",
        subject="a/b#3",
        spec_text="frozen",
        base_sha="base",
        conn=db,
        evaluator_count=2,
        evaluator_agents=["cursor", "vibe"],
        now=1000,
    )
    result = guard.assess_and_record_opportunity(
        **kwargs, env={"ORCH_RESEARCH_ARM": "0", "ORCH_EXPERIMENT_PRODUCER": enabled}
    )
    assert result["eligible"] is True
    assert (
        guard.assess_and_record_opportunity(
            **kwargs, is_missing_spec=True, env={"ORCH_EXPERIMENT_PRODUCER": "1"}
        )["eligible"]
        is False
    )
    meta["producer"] = "other"
    (root / "meta.json").write_text(json.dumps(meta))
    assert (
        guard.assess_and_record_opportunity(
            **{**kwargs, "exp_id": "other"}, env={"ORCH_EXPERIMENT_PRODUCER": "1"}
        )["reason"]
        == "research_arm_disabled"
    )


def test_real_subject_plan_keeps_reservations_and_pacing(tmp_path, monkeypatch):
    import research_scheduler

    cap = {"agents": {a: {"state": "ok"} for a in ["codex", "claude", "cursor", "vibe"]}}
    monkeypatch.setattr(
        producer,
        "route_cells",
        lambda tasks, agents, **k: [
            {"task_type": "implement", "agent": a, "age_days": 25} for a in agents
        ],
    )
    monkeypatch.setattr(
        research_scheduler, "spare_capacity", lambda *a: {a: 2 for a in cap["agents"]}
    )
    item = {
        "target": "a/b#1",
        "task_type": "implement",
        "labels": [],
        "lane": "research",
        "body": "`pytest tests/test_fix.py::test_fix`",
    }
    kwargs = dict(
        learned={"implement": {"codex": {"rank": 0}, "claude": {"rank": 1}}},
        claimed_targets=set(),
        excluded_targets=set(),
        production_reserve={"codex": 2},
        rng=random.Random(2),
    )
    plan = producer.build_plan([item], cap, **kwargs)
    assert plan["planned"][0]["pools"]["codex"] == "production_reserve"
    assert (
        producer.build_plan([item], cap, **{**kwargs, "excluded_targets": {"a/b#1"}})["planned"]
        == []
    )


@pytest.mark.parametrize("verdict,expected", [("pending", "1"), ("pass", "1"), ("fail", "0")])
def test_trial_shell_is_bash32_compatible_and_self_expiring(tmp_path, verdict, expected):
    from paths import REPO_ROOT

    source = (REPO_ROOT / "orchestrate.sh").read_text()
    start = source.index("# ORCH-ANCHOR: experiment-producer-trial")
    end = source.index(
        "unset _orch_exp_trial _orch_exp_end _orch_exp_verdict _orch_exp_default", start
    ) + len("unset _orch_exp_trial _orch_exp_end _orch_exp_verdict _orch_exp_default")
    block = source[start:end]
    prelude = f'python3() {{ echo "20261020 {verdict}"; }}; date() {{ echo 20261021; }}; '
    result = subprocess.run(
        [
            "/bin/bash",
            "-c",
            prelude
            + block
            + '\nprintf "%s %s" "$ORCH_EXPERIMENT_PRODUCER" "$ORCH_EXPERIMENT_RATE_PER_DAY"',
        ],
        env={"PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert result.stdout == f"{expected} 2"
    result = subprocess.run(
        [
            "/bin/bash",
            "-c",
            prelude.replace(verdict, "pass") + block + '\nprintf "%s" "$ORCH_EXPERIMENT_PRODUCER"',
        ],
        env={"PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
    )
    assert result.stdout == "1"


def test_direct_winner_candidate_links_through_existing_discovery(tmp_path, monkeypatch):
    import sqlite3

    root, meta, evaluation = fixture_experiment(tmp_path, monkeypatch)
    monkeypatch.setattr(synthesis_promotion, "_accepted_source_influences", lambda *a: [])
    member = meta["members"][0]
    receipt = {
        "candidate_id": "experiment:experiment",
        "local_verify": {member["member_id"]: {"ok": True, "verified_commit": "commit"}},
    }
    state = synthesis_promotion.compile_producer_candidate(root, meta, member, receipt, "commit")
    assert state["delivery_phase"] == "candidate_ready"
    assert state["synthesis"] == {}
    db_path = tmp_path / "brain.db"
    with sqlite3.connect(db_path) as db:
        db.execute("CREATE TABLE runs (run_id TEXT)")
        db.execute("INSERT INTO runs VALUES ('keepalive:a/b#9:codex')")

    def gh(argv):
        if "search/issues" in argv:
            return {
                "incomplete_results": False,
                "total_count": 1,
                "items": [
                    {
                        "number": 9,
                        "url": "https://api.github.com/repos/a/b/issues/9",
                        "pull_request": {"url": "https://api.github.com/repos/a/b/pulls/9"},
                    }
                ],
            }
        return {
            "number": 9,
            "url": "https://api.github.com/repos/a/b/pulls/9",
            "body": "Candidate ID: `experiment:experiment`",
            "base": {"repo": {"full_name": "a/b"}},
        }

    result = synthesis_promotion.default_delivery_discovery(state, gh_fn=gh, db_path=db_path)
    assert result["status"] == "linked"
    synthesis_promotion.link_delivery(
        root,
        delivery_run_id=result["run_id"],
        delivery_ref=result["ref"],
        pr_number=result["pr_number"],
    )
    assert synthesis_promotion.load_state(root)["delivery_phase"] == "delegated_or_pr"


def test_keepalive_label_retries_and_expired_subject_is_released(tmp_path, monkeypatch):
    root, meta, _ = fixture_experiment(tmp_path, monkeypatch)
    receipt = {
        "status": "delegated_or_pr",
        "pr_url": "https://github.com/a/b/pull/9",
        "pr_number": 9,
        "winner": "cursor",
        "label_result": {"error": "quota"},
    }
    (root / "producer-delivery.json").write_text(json.dumps(receipt))
    calls = []
    result = exp_abcd.producer_followup(
        root,
        meta,
        now=time.time(),
        min_idle_s=0,
        eval_timeout=1,
        collect_fn=None,
        evaluate_fn=None,
        gh_fn=lambda argv: calls.append(argv) or {"stdout": ""},
    )
    assert calls == [
        ["gh", "api", "-X", "POST", "repos/a/b/issues/9/labels", "-f", "labels[]=agent:cursor"]
    ]
    assert "error" not in result["label_result"]
    (root / "producer-delivery.json").unlink()
    result = exp_abcd.producer_followup(
        root,
        meta,
        now=meta["launched_ts"] + 72 * 3600,
        min_idle_s=0,
        eval_timeout=1,
        collect_fn=None,
        evaluate_fn=None,
        gh_fn=lambda argv: calls.append(argv) or {"stdout": ""},
    )
    assert result["reason"] == "72h_without_pr"
    assert calls[-1] == [
        "gh",
        "api",
        "-X",
        "DELETE",
        "repos/a/b/issues/3/labels/status%3Ain-progress",
    ]


def plan_fixture(monkeypatch, *, shed=True):
    import research_scheduler
    import router

    agents = ["codex", "claude", "cursor", "vibe", "muse"]
    cap = {"agents": {a: {"state": "shed" if shed and a == "claude" else "ok"} for a in agents}}
    monkeypatch.setattr(
        router, "ROUTE_TABLE", {"implement": {"agents": [{"agent": a} for a in agents]}}
    )
    monkeypatch.setattr(router, "BACKUP_AGENTS", set())
    monkeypatch.setattr(
        producer,
        "route_cells",
        lambda *a, **k: [
            {"agent": a, "task_type": "implement", "age_days": 100 if a == "claude" else 25}
            for a in agents
        ],
    )
    monkeypatch.setattr(research_scheduler, "spare_capacity", lambda *a: {a: 5 for a in agents})
    return cap, dict(
        learned={"implement": {"codex": {"rank": 0}}},
        claimed_targets=set(),
        excluded_targets=set(),
        production_reserve={},
        rng=random.Random(2),
    )


@pytest.mark.parametrize("block", [None, "lock", "unevaluated_cap", "cooldown", "fresh_warn"])
def test_shed_agent_does_not_latch_firing_launch(tmp_path, monkeypatch, capsys, block):
    import tick

    cap, kwargs = plan_fixture(monkeypatch)
    fresh_warn = block == "fresh_warn"
    if fresh_warn:
        block = None
        cap["agents"]["claude"]["state"] = "warn"
        monkeypatch.setattr(
            producer,
            "route_cells",
            lambda *a, **k: [
                {"agent": a, "task_type": "implement", "age_days": 1} for a in cap["agents"]
            ],
        )
    item = {
        "target": "a/b#3",
        "task_type": "implement",
        "body": "`pytest tests/test_fix.py::test_fix`",
    }
    monkeypatch.setattr(tick.claims, "reap_stale", lambda: None)
    monkeypatch.setattr(tick.claims, "active_claims", lambda: {})
    monkeypatch.setattr(tick.claims, "claim", lambda *a: True)
    monkeypatch.setattr(tick.claims, "release", lambda *a: None)
    monkeypatch.setattr(tick.capabilities, "production_heartbeat", lambda *a, **k: None)
    monkeypatch.setattr(tick.research_subjects, "assess_candidate", lambda **k: {"eligible": True})
    monkeypatch.setattr(tick.research_subjects, "record_subject", lambda *a, **k: None)
    monkeypatch.setattr(tick.research_subjects, "record_event", lambda *a, **k: None)
    monkeypatch.setattr(producer, "issue_status", lambda *a, **k: {"stdout": ""})
    monkeypatch.setattr(exp_abcd, "exp_paths", lambda exp_id: tmp_path / exp_id)
    monkeypatch.setattr(
        tick.research_subjects,
        "subject_identity",
        lambda *a, **k: {"subject_id": "s", "subject_family_id": "f"},
    )
    if block == "lock":
        monkeypatch.setattr(producer, "issue_status", lambda *a, **k: {"error": "no lock"})
    elif block:
        monkeypatch.setattr(
            tick.research_subjects,
            "assess_candidate",
            lambda **k: {"eligible": False, "reason": block},
        )
    rng = random.Random(2)
    rng.random = lambda: 0
    launched = []
    result = tick.research_tick(
        [item],
        cap,
        dry_run=False,
        learned=kwargs["learned"],
        rng=rng,
        issue_body_fn=lambda *a: item["body"],
        prepare_fn=lambda *a, **k: launched.append(a) or {"launched": [], "base_sha": "base"},
    )
    if block:
        assert result["active"] is False
        assert not launched
        state = producer.read_state()
        assert state["launched_today"] == 0
        assert state["skips"]["lock_add_failed" if block == "lock" else block] == 1
        return
    assert result["active"] is True
    assert len(launched) == 1
    arms = launched[0][3]
    assert {a for arm in arms for a in arm["agents"]} <= {"codex", "cursor", "vibe", "muse"}
    assert "claude" not in {a for arm in arms for a in arm["agents"]}
    assert (
        "coverage debt 0 cells > 21d, drainable 0"
        if fresh_warn
        else "coverage debt 5 cells > 21d, drainable 4"
    ) in capsys.readouterr().err
    assert producer.read_state()["launched_today"] == 1


def test_producer_named_test_admission_and_derivation(tmp_path, monkeypatch):
    assert producer.named_test("`pytest -q`") is None
    cap, kwargs = plan_fixture(monkeypatch)
    plan = producer.build_plan([{"target": "a/b#3", "body": "Implement a feature"}], cap, **kwargs)
    assert plan["planned"] == []
    assert {"target": "a/b#3", "reason": "no_named_test", "test_class": "none"} in plan["skipped"]
    for body in ("Run `tests/test_x.py`", "Run tests/test_x.py"):
        assert producer.named_test(body) == "pytest tests/test_x.py"
        admitted = producer.build_plan([{"target": "a/b#3", "body": body}], cap, **kwargs)
        assert admitted["planned"][0]["target"] == "a/b#3"
    body = "## Acceptance Criteria\n- [ ] The feature is fixed."
    admitted = producer.build_plan([{"target": "a/b#3", "body": body}], cap, **kwargs)
    assert admitted["planned"][0]["target"] == "a/b#3"
    package = tmp_path / "package.json"
    package.write_text(json.dumps({"scripts": {"test": "vitest run"}, "packageManager": "pnpm@9"}))
    assert (
        producer.named_test("Run `.github/scripts/x.test.js`", tmp_path)
        == "pnpm test -- .github/scripts/x.test.js"
    )
    package.write_text("{}")
    assert producer.named_test("Run `x.test.js`", tmp_path) is None
    (tmp_path / "x.test.js").write_text("const test = require('node:test');")
    assert producer.named_test("Run `x.test.js`", tmp_path) == "node --test x.test.js"
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "package.json").write_text(json.dumps({"scripts": {"test": "vitest run"}}))
    js_command = producer.named_test("Run frontend/src/map.test.tsx", tmp_path)
    assert js_command == "npm --prefix frontend test -- src/map.test.tsx"
    assert producer.named_test_paths(js_command) == ["frontend/src/map.test.tsx"]
    assert producer.named_test(f"`{js_command}`") == js_command
    from synthesis_promotion import _safe_command

    assert _safe_command(["node", "--test", "x.test.js"])
    assert _safe_command(["npm", "--prefix", "frontend", "test", "--", "src/map.test.tsx"])
    assert not _safe_command(["npm", "--prefix", "../other", "test"])
    producer_derivation_proof(tmp_path, monkeypatch)


@pytest.mark.parametrize("status", ["merged", "no_winner", "discarded", "expired"])
def test_five_terminal_followups_release_once(tmp_path, monkeypatch, status):
    root, meta, _ = fixture_experiment(tmp_path, monkeypatch)
    meta["launched_ts"] = time.time()
    (root / "meta.json").write_text(json.dumps(meta))
    (root / "producer-delivery.json").write_text(json.dumps({"status": status}))
    monkeypatch.setattr(exp_abcd, "EXP_DIR", tmp_path)
    calls = []
    monkeypatch.setattr(producer, "issue_status", lambda *a, **k: calls.append(a) or {"stdout": ""})
    monkeypatch.setattr(
        synthesis_promotion, "load_state", lambda *a: pytest.fail("legacy reconciliation")
    )
    for _ in range(5):
        exp_abcd.followup(collect_fn=lambda *a: pytest.fail("collected terminal experiment"))
    assert calls == [("a/b#3", False)]
    assert json.loads((root / "producer-delivery.json").read_text())["lock_released_ts"] > 0


def test_terminal_release_retries_until_success(tmp_path, monkeypatch):
    root, meta, _ = fixture_experiment(tmp_path, monkeypatch)
    (root / "producer-delivery.json").write_text(json.dumps({"status": "discarded"}))
    calls = []

    def release(*a, **k):
        calls.append(a)
        return {"error": "temporary"} if len(calls) == 1 else {"stdout": ""}

    monkeypatch.setattr(producer, "issue_status", release)
    for _ in range(5):
        exp_abcd.producer_followup(
            root, meta, now=2000, min_idle_s=0, eval_timeout=1, collect_fn=None, evaluate_fn=None
        )
    assert len(calls) == 2
    assert json.loads((root / "producer-delivery.json").read_text())["lock_released_ts"] == 2000


@pytest.mark.parametrize(
    "failure", [ValueError("broken followup"), RuntimeError("broken followup")]
)
def test_producer_exception_never_falls_through(tmp_path, monkeypatch, failure):
    root, meta, _ = fixture_experiment(tmp_path, monkeypatch)
    meta["launched_ts"] = time.time()
    (root / "meta.json").write_text(json.dumps(meta))
    monkeypatch.setattr(exp_abcd, "EXP_DIR", tmp_path)

    def broken(*a, **k):
        raise failure

    monkeypatch.setattr(exp_abcd, "producer_followup", broken)
    monkeypatch.setattr(
        synthesis_promotion, "load_state", lambda *a: pytest.fail("load_state called")
    )
    result = exp_abcd.followup()
    receipt = json.loads((root / "producer-delivery.json").read_text())
    assert receipt["error"] == "broken followup"
    assert receipt["ts"] > 0
    assert result["processed"][0]["error"] == "broken followup"


def test_producer_scan_uses_meta_age_not_directory_mtime(tmp_path, monkeypatch):
    root, meta, _ = fixture_experiment(tmp_path, monkeypatch)
    meta["launched_ts"] = time.time() - (producer.PRODUCER_FOLLOWUP_DAYS + 1) * 86400
    (root / "meta.json").write_text(json.dumps(meta))
    monkeypatch.setattr(exp_abcd, "EXP_DIR", tmp_path)
    monkeypatch.setattr(
        exp_abcd, "producer_followup", lambda *a, **k: pytest.fail("old producer scanned")
    )
    monkeypatch.setattr(
        synthesis_promotion, "load_state", lambda *a: pytest.fail("legacy reconciliation")
    )
    assert exp_abcd.followup()["processed"] == []


@pytest.mark.parametrize("timeout", [True, False])
@pytest.mark.parametrize("evaluated", [True, False])
def test_remote_unknown_never_delivers_and_retries(tmp_path, monkeypatch, timeout, evaluated):
    root, meta, ranking = fixture_experiment(tmp_path, monkeypatch)
    for member in meta["members"]:
        (root / exp_abcd.exp_log_path(member["agent"], member["member_id"])).write_text("done")
    if evaluated:
        (root / "producer-evaluation.json").write_text(json.dumps(ranking))
    monkeypatch.setattr(exp_abcd.provision, "ensure_canonical", lambda *a: tmp_path)
    checks = []

    def remote(*a, **k):
        checks.append(a)
        if timeout:
            raise subprocess.TimeoutExpired("git ls-remote", 60)
        return SimpleNamespace(returncode=1, stdout="", stderr="remote down")

    for _ in range(2):
        result = exp_abcd.producer_followup(
            root,
            meta,
            now=1100,
            min_idle_s=-(10**10),
            eval_timeout=1,
            collect_fn=lambda *a: pytest.fail("collected unknown remote"),
            evaluate_fn=lambda *a: pytest.fail("judged unknown remote"),
            deliver_fn=lambda *a, **k: pytest.fail("delivered unknown remote"),
            push_check_fn=lambda *a: exp_abcd.assert_no_member_push(*a, run_fn=remote),
        )
        assert result["status"] == "remote_unknown"
    assert len(checks) == 2


@pytest.mark.parametrize("merged,verdict", [(6, "pass"), (5, "fail")])
def test_report_writes_due_trial_verdict(monkeypatch, merged, verdict):
    import switch_review

    now = time.time()
    producer.trial_defaults(
        now - (producer.COVERAGE_DAYS + producer.WINNER_MERGE_GRACE_DAYS) * 86400 - 1, active=True
    )
    monkeypatch.setattr(
        producer,
        "producer_summary",
        lambda: dict(
            launched=6,
            evaluated=6,
            winner_prs=6,
            winner_merged=merged,
            cells_fresh=0,
            cells_total=0,
            skips={},
            unknowns=[],
        ),
    )
    line = switch_review.experiment_producer_line()
    assert f"verdict {verdict}, days_left 0" in line
    assert "winner merge grace 7d" in line
    assert producer.read_state()["trial_verdict"] == verdict


def test_shadow_does_not_start_trial():
    producer.trial_defaults(1000)
    assert "trial_start" not in producer.read_state()
    producer.trial_defaults(2000, active=True)
    assert producer.read_state()["trial_start"] == 2000
    producer.trial_defaults(3000, active=True)
    assert producer.read_state()["trial_start"] == 2000


def test_merge_grace_counts_only_trial_launches(tmp_path):
    from datetime import datetime, timezone

    start = 1000
    producer.trial_defaults(start, active=True)
    end = start + producer.COVERAGE_DAYS * 86400
    merge_end = end + producer.WINNER_MERGE_GRACE_DAYS * 86400
    merged_at = {}
    for n, (launched, merged) in enumerate(
        [
            (start, end + 1),
            (end, merge_end),
            (end - 1, merge_end + 1),
            (start - 1, end),
            (end + 1, end + 2),
        ]
    ):
        root = tmp_path / str(n)
        root.mkdir()
        (root / "meta.json").write_text(
            json.dumps({"producer": "experiment_producer", "repo": "a/b", "launched_ts": launched})
        )
        (root / "producer-delivery.json").write_text(
            json.dumps({"pr_url": "url", "pr_number": n, "scored_arms": 2})
        )
        merged_at[n] = datetime.fromtimestamp(merged, timezone.utc).isoformat()

    def gh(argv):
        return {"stdout": json.dumps({"merged_at": merged_at[int(argv[-1].rsplit("/", 1)[1])]})}

    result = producer.producer_summary(tmp_path, gh_fn=gh)
    assert result["post_window_launches"] == 1
    assert result["launched"] == 3
    assert result["winner_merged"] == 2


def test_rest_lock_mutations_and_missing_label_is_success(monkeypatch):
    add, delete = [], []
    producer.issue_status("a/b#3", True, gh_fn=lambda a: add.append(a) or {})
    producer.issue_status("a/b#3", False, gh_fn=lambda a: delete.append(a) or {})
    assert add == [
        [
            "gh",
            "api",
            "-X",
            "POST",
            "repos/a/b/issues/3/labels",
            "-f",
            "labels[]=status:in-progress",
        ]
    ]
    assert delete == [
        ["gh", "api", "-X", "DELETE", "repos/a/b/issues/3/labels/status%3Ain-progress"]
    ]
    monkeypatch.setattr(
        producer.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=1, stderr="gh: Not Found (HTTP 404)", stdout=""),
    )
    # Exercise real gh_call, bypassing the suite's injected offline boundary.
    from unittest.mock import patch

    with patch.object(producer, "gh_call", ORIGINAL_GH_CALL):
        assert "error" not in producer.issue_status("a/b#3", False)
        assert "error" in producer.issue_status("a/b#3", True)


def test_verification_gets_named_nodes_and_rejects_worktree_changes(tmp_path, monkeypatch):
    root, meta, ranking = fixture_experiment(tmp_path, monkeypatch)
    calls = []
    statuses = {}

    def git_run(argv, **kwargs):
        wt = argv[2]
        if "status" in argv:
            statuses[wt] = statuses.get(wt, 0) + 1
            out = "" if statuses[wt] == 1 else "?? generated.txt"
        else:
            out = "commit"
        return SimpleNamespace(returncode=0, stdout=out, stderr="")

    def verify(*a, **kwargs):
        calls.append(kwargs)
        return {"ok": True}

    result = synthesis_promotion.deliver_producer_winner(
        root,
        meta,
        ranking,
        verify_fn=verify,
        git_fn=git_run,
        gh_fn=lambda argv: {"stdout": ""},
    )
    assert result["status"] == "no_winner"
    assert calls and all(k["test_paths"] == ["tests/test_fix.py::test_fix"] for k in calls)
    assert all(
        v["error"] == "verification_changed_worktree" for v in result["local_verify"].values()
    )


def test_local_verify_copies_named_node_file(tmp_path, monkeypatch):
    import local_verify

    wt = tmp_path / "work"
    (wt / "tests").mkdir(parents=True)
    (wt / "tests/test_fix.py").write_text("def test_fix(): assert True\n")
    monkeypatch.setattr(local_verify, "_changed_paths", lambda *a: ["src/fix.py"])
    monkeypatch.setattr(local_verify, "_extract_base", lambda *a: None)
    runs = []

    def run(command, cwd, timeout):
        runs.append(cwd)
        assert (cwd / "tests/test_fix.py").exists()
        return {
            "ok": len(runs) == 1,
            "returncode": 0 if len(runs) == 1 else 1,
            "stdout_tail": "1 failed",
            "stderr_tail": "",
        }

    monkeypatch.setattr(local_verify, "_run", run)
    selected = []

    def nodes(root, command, paths, timeout):
        selected.extend(paths)
        return {"verdict": "PASS", "hollow_nodes": [], "counts": {"nodes": 1}}

    monkeypatch.setattr(local_verify, "_analyse_nodes", nodes)
    result = local_verify.verify(
        wt,
        test_cmd="pytest tests/test_fix.py::test_fix",
        test_paths=["tests/test_fix.py::test_fix"],
    )
    assert result["copied_test_paths"] == ["tests/test_fix.py"]
    assert selected == ["tests/test_fix.py::test_fix"]


def test_build_plan_skip_line_has_zero_drainable_for_absent_feed_classes(monkeypatch, capsys):
    import router

    cap, kwargs = plan_fixture(monkeypatch)
    for row in cap["agents"].values():
        row["state"] = "warn"
    cap["agents"]["codex"]["state"] = "ok"
    monkeypatch.setattr(
        router,
        "ROUTE_TABLE",
        {
            "implement": {"agents": [{"agent": "claude"}]},
            "testgen": {"agents": [{"agent": "codex"}]},
        },
    )
    monkeypatch.setattr(
        producer,
        "route_cells",
        lambda *a, **k: [
            {"task_type": "implement", "agent": "claude", "age_days": 25},
            {"task_type": "testgen", "agent": "codex", "age_days": 25},
        ],
    )
    item = {"target": "a/b#3", "body": "`pytest tests/test_fix.py`", "task_type": "implement"}
    plan = producer.build_plan([item], cap, **kwargs)
    assert plan["planned"] == []
    assert capsys.readouterr().err == (
        "pacing claude=warn, cursor=warn, muse=warn, vibe=warn; "
        "coverage debt 2 cells > 21d, drainable 0\n"
    )


def test_no_named_test_skip_recorded_once_per_issue_per_utc_day(monkeypatch):
    import tick

    cap, _ = plan_fixture(monkeypatch, shed=False)
    monkeypatch.setattr(tick.claims, "reap_stale", lambda: None)
    monkeypatch.setattr(tick.claims, "active_claims", lambda: {})
    events = []
    monkeypatch.setattr(tick.research_subjects, "record_event", lambda *a, **k: events.append(k))
    clock = [86400.0]
    monkeypatch.setattr(producer.time, "time", lambda: clock[0])
    items = [{"target": f"a/b#{n}", "body": "No named test"} for n in (3, 4)]
    for _ in range(2):
        tick.research_tick(items, cap, dry_run=False)
    assert producer.read_state()["skips"]["no_named_test"] == 2
    assert len(events) == 4  # two issue rejections plus one no_subject per tick
    assert producer.read_state()["skips"]["no_subject"] == 2
    clock[0] = 2 * 86400
    tick.research_tick(items, cap, dry_run=False)
    assert producer.read_state()["skips"]["no_named_test"] == 4
    assert producer.read_state()["no_named_test_classes"] == {"none": 4}
    assert producer.record_skip("no_named_test", target="a/b#5", test_class="ci-only")
    assert not producer.record_skip("no_named_test", target="a/b#5", test_class="ci-only")
    assert producer.read_state()["no_named_test_classes"] == {"none": 4, "ci-only": 1}


@pytest.mark.parametrize(
    "response", [{"error": "flake"}, {"stdout": '{"merge_state_unknown": true}'}]
)
def test_unknown_winner_merge_never_fails_trial(tmp_path, monkeypatch, response):
    producer.trial_defaults(1000, active=True)
    monkeypatch.setattr(exp_abcd, "EXP_DIR", tmp_path)
    for n in range(2):
        root = tmp_path / str(n)
        root.mkdir()
        (root / "meta.json").write_text(
            json.dumps(
                {
                    "producer": "experiment_producer",
                    "repo": "a/b",
                    "launched_ts": 1000,
                }
            )
        )
        (root / "producer-delivery.json").write_text(json.dumps({"pr_url": "url", "pr_number": n}))
    calls = []
    monkeypatch.setattr(producer, "gh_call", lambda argv: calls.append(argv) or response)
    assert producer.trial_verdict(1000 + 28 * 86400) == "pending"
    assert len(calls) == 2
    assert producer.read_state()["trial_unknown"] == 2


def test_trial_defaults_failure_is_visible_and_defaults_off():
    from paths import REPO_ROOT

    source = (REPO_ROOT / "orchestrate.sh").read_text()
    block = source[
        source.index("# ORCH-ANCHOR: experiment-producer-trial") : source.index(
            "# ORCH-ANCHOR: heartbeat-producers"
        )
    ]
    result = subprocess.run(
        [
            "/bin/bash",
            "-c",
            "python3() { return 1; }; " + block + '\nprintf "%s" "$ORCH_EXPERIMENT_PRODUCER"',
        ],
        env={"PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert result.stdout == "0"
    assert "ERROR: experiment producer --trial-defaults failed; defaulting off" in result.stderr


def test_corrupt_non_producer_meta_never_holds_the_verdict(tmp_path, monkeypatch):
    monkeypatch.setattr(exp_abcd, "EXP_DIR", tmp_path)
    (tmp_path / "legacy").mkdir()
    (tmp_path / "legacy" / "meta.json").write_text("{not json")
    (tmp_path / "other").mkdir()
    (tmp_path / "other" / "meta.json").write_text(json.dumps({"producer": "research"}))
    summary = producer.producer_summary(tmp_path, gh_fn=lambda argv: pytest.fail("polled"))
    assert summary["unknowns"] == []
    assert summary["unreadable_meta"] == 1


def test_unresolved_unknown_resolves_to_a_persisted_fail(capsys):
    producer.trial_defaults(1000, active=True)
    stuck = lambda: {"winner_merged": 2, "unknowns": ["merge_state_unknown"]}  # noqa: E731
    due = 1000 + (producer.COVERAGE_DAYS + producer.WINNER_MERGE_GRACE_DAYS) * 86400
    assert producer.trial_verdict(due, summary_fn=stuck) == "pending"
    state = producer.read_state()
    bound = due + producer.UNRESOLVED_UNKNOWN_DAYS * 86400
    assert state["resolves_on"] == time.strftime("%Y%m%d", time.gmtime(bound))
    assert "resolves fail on" in capsys.readouterr().err
    assert producer.trial_verdict(bound - 86400, summary_fn=stuck) == "pending"
    assert producer.trial_verdict(bound, summary_fn=stuck) == "fail"
    assert producer.read_state()["trial_reason"] == "unresolved_unknown"
    assert producer.trial_verdict(bound + 86400 * 400, summary_fn=stuck) == "fail"


def test_six_merged_pass_even_with_unknowns():
    producer.trial_defaults(1000, active=True)
    due = 1000 + (producer.COVERAGE_DAYS + producer.WINNER_MERGE_GRACE_DAYS) * 86400
    summary = lambda: {"winner_merged": 6, "unknowns": ["merge_state_unknown"]}  # noqa: E731
    assert producer.trial_verdict(due, summary_fn=summary) == "pass"


@pytest.mark.parametrize(
    "offset,verdict,launches,receipt",
    [
        (-1, "pending", True, ""),
        (0, "pending", False, "verdict pending (resolves by 20260212); launches held"),
        (1, "pending", False, "verdict pending (resolves by 20260212); launches held"),
        (1, "pass", True, ""),
        (
            1,
            "fail",
            False,
            "trial verdict fail (unresolved_unknown); launches stopped — owner decision",
        ),
    ],
)
def test_trial_window_gates_real_plan(monkeypatch, capsys, offset, verdict, launches, receipt):
    import research_scheduler

    start = 1767225600  # 2026-01-01 UTC
    producer.trial_defaults(start, active=True)
    state = producer.read_state()
    state.update(trial_verdict=verdict, trial_reason="unresolved_unknown")
    producer.save_state(state)
    end = producer.trial_window_end(state)
    monkeypatch.setattr(producer.time, "time", lambda: end + offset)
    # A stale shell fallback must never move the persisted trial's window.
    monkeypatch.setenv("ORCH_EXPERIMENT_TRIAL_END", "20990101")
    monkeypatch.setattr(
        producer,
        "route_cells",
        lambda *a, **k: [
            {"task_type": "implement", "agent": agent, "age_days": 25}
            for agent in ["codex", "claude"]
        ],
    )
    monkeypatch.setattr(research_scheduler, "spare_capacity", lambda *a: {"codex": 2, "claude": 2})
    monkeypatch.setattr(
        __import__("capacity"), "experiment_pacing", lambda *a: {"codex": "ok", "claude": "ok"}
    )
    plan = producer.build_plan(
        [{"target": "a/b#1", "task_type": "implement", "body": "`pytest tests/test_fix.py`"}],
        {"agents": {"codex": {}, "claude": {}}},
        learned={},
        claimed_targets=set(),
        excluded_targets=set(),
        production_reserve={},
        rng=random.Random(2),
    )
    assert bool(plan["planned"]) is launches
    output = capsys.readouterr().err
    if receipt:
        assert receipt in output
    else:
        assert "launches held" not in output
        assert "launches stopped" not in output


def test_shell_trial_end_fallback_is_used_by_hold_and_accounting(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCH_EXPERIMENT_TRIAL_END", "20260122")
    end = producer.trial_window_end({})
    assert "trial window ended 20260122" in producer.trial_launch_hold(end)
    root = tmp_path / "late"
    root.mkdir()
    (root / "meta.json").write_text(
        json.dumps(
            {
                "producer": "experiment_producer",
                "launched_ts": end + 1,
            }
        )
    )
    assert producer.producer_summary(tmp_path)["post_window_launches"] == 1


def test_held_trial_can_resolve_and_clear_without_new_launches():
    producer.trial_defaults(1000, active=True)
    end = producer.trial_window_end(producer.read_state())
    assert "launches held" in producer.trial_launch_hold(end)
    assert (
        producer.trial_verdict(
            end + producer.WINNER_MERGE_GRACE_DAYS * 86400,
            summary_fn=lambda: {"winner_merged": 6},
        )
        == "pass"
    )
    assert producer.trial_launch_hold(end + 8 * 86400) is None


def test_failed_trial_receipt_survives_switch_off_and_drain(monkeypatch, capsys):
    import tick

    producer.save_state({"trial_verdict": "fail", "trial_reason": "unresolved_unknown"})
    monkeypatch.setattr(tick.claims, "reap_stale", lambda: None)
    monkeypatch.setattr(tick.claims, "active_claims", lambda: {})
    monkeypatch.setattr(
        tick.research_scheduler,
        "build_research_plan",
        lambda *a, **k: {"planned": [], "skipped": [], "candidates": []},
    )
    tick.research_tick([], {}, dry_run=False, env={"ORCH_EXPERIMENT_PRODUCER": "0"})
    assert (
        "producer: trial verdict fail (unresolved_unknown); launches stopped — owner decision"
        in capsys.readouterr().err
    )


def producer_derivation_proof(tmp_path, monkeypatch):
    """Exercise real commits, assertion RED, and comparison seeding without agent CLIs."""
    canon = tmp_path / "repo"
    canon.mkdir()

    def git(wt, *args):
        return subprocess.run(
            ["git", "-C", str(wt), *args], check=True, capture_output=True, text=True
        ).stdout.strip()

    git(canon, "init", "-b", "main")
    git(canon, "config", "user.email", "test@example.com")
    git(canon, "config", "user.name", "Test")
    (canon / "feature.txt").write_text("old")
    git(canon, "add", ".")
    git(canon, "commit", "-m", "base")
    base = git(canon, "rev-parse", "HEAD")
    git(canon, "update-ref", "refs/remotes/origin/main", base)
    # A dependency visible ONLY in the consumer environment must collect in RED.
    import venv
    venv.EnvBuilder(system_site_packages=True, with_pip=False).create(canon / ".venv")
    consumer_python = canon / ".venv/bin/python"
    site = subprocess.check_output(
        [str(consumer_python), "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"], text=True
    ).strip()
    (Path(site) / "consumer_only_731.py").write_text("VALUE = 'fixed'\n")
    root = tmp_path / "derived-experiment"
    root.mkdir()
    monkeypatch.setattr(exp_abcd.provision, "ensure_canonical", lambda repo: canon)
    monkeypatch.setattr(exp_abcd.provision, "base_branch", lambda repo: "main")
    monkeypatch.setattr(exp_abcd, "exp_paths", lambda exp_id: root)
    monkeypatch.setattr(
        exp_abcd,
        "exp_worktree",
        lambda repo, exp_id, agent, member: tmp_path / (exp_id + "-" + member),
    )
    monkeypatch.setattr(exp_abcd.feedback, "record_run", lambda *a, **kw: None)
    # The real RED sandbox cannot nest inside Codex's sandbox. Test its command
    # and environment separately; this fixture executes only our own tiny test.
    monkeypatch.setattr(producer, "_red_sandbox", lambda argv, *a: argv)
    original_freeze = producer.freeze_test
    events = []

    def derive(agent, prompt, wt):
        assert agent == "codex" and "Acceptance Criteria" in prompt
        events.append("derive")
        (wt / "tests").mkdir()
        (wt / "tests/test_feature.py").write_text(
            "from pathlib import Path\nfrom consumer_only_731 import VALUE\ndef test_feature():\n    assert Path('feature.txt').read_text() == VALUE\n"
        )
        git(wt, "add", "tests/test_feature.py")
        git(wt, "commit", "-m", "failing acceptance test")
        (wt / "test-command.txt").write_text("pytest tests/test_feature.py")

    def freeze(*a, **kw):
        result = original_freeze(*a, **kw, derive_fn=derive)
        events.append("frozen")
        return result

    monkeypatch.setattr(producer, "freeze_test", freeze)

    def spawn(agent, mode, prompt, wt, log, **kwargs):
        assert events[0:2] == ["derive", "frozen"]
        meta = json.loads((root / "meta.json").read_text())
        assert meta["named_test"] == "pytest tests/test_feature.py"
        assert meta["test_source"] == "derived"
        assert git(wt, "rev-parse", "HEAD") == meta["test_commit"]
        assert (
            hashlib.sha256((wt / "tests/test_feature.py").read_bytes()).hexdigest()
            == meta["test_sha256"]["tests/test_feature.py"]
        )
        events.append("arm")
        return 123

    monkeypatch.setattr(exp_abcd, "_spawn", spawn)
    spec = tmp_path / "spec.md"
    spec.write_text("## Acceptance Criteria\n- [ ] feature.txt contains fixed.\n")
    arms = [{"arm_id": a, "agents": [a], "strategy": "single"} for a in ["codex", "claude"]]
    result = exp_abcd.prepare_arms(
        "a/b", str(spec), "derived", arms, producer_metadata={"producer": "experiment_producer"}
    )
    assert len(result["launched"]) == 2
    assert events == ["derive", "frozen", "arm", "arm"]
    assert result["test_source"] == "derived"
    assert not (tmp_path / "derived-test-spec").exists()
    assert not git(canon, "branch", "--list", exp_abcd.exp_branch("derived", "codex", "test-spec"))
    assert (
        git(canon, "rev-parse", "refs/experiment-tests/derived/codex")
        == json.loads((root / "meta.json").read_text())["test_commit"]
    )
    assert "1 failed" in (root / "test-spec-red.log").read_text()
    producer.record_launch(test_source="derived")
    assert producer.producer_summary(tmp_path)["derived_tests"] == 1
    meta = json.loads((root / "meta.json").read_text())
    meta["subject"] = "a/b#3"
    with monkeypatch.context() as frozen_guard:
        frozen_guard.setattr(
            exp_abcd,
            "exp_worktree",
            lambda repo, exp_id, agent, member: tmp_path / ("derived-" + member),
        )
        frozen_guard.setattr(
            __import__("research_subjects"), "mark_lifecycle", lambda *a, **k: None
        )
        for member in meta["members"]:
            test = tmp_path / ("derived-" + member["member_id"]) / "tests/test_feature.py"
            test.write_text("def test_feature(): assert True\n")
        decision = synthesis_promotion.deliver_producer_winner(
            root,
            meta,
            {"ranking": [(member["member_id"], 10) for member in meta["members"]]},
            verify_fn=lambda *a, **k: pytest.fail(
                "modified frozen test reached winner verification"
            ),
        )
        assert decision["status"] == "no_winner"

    # Missing criteria, passing tests, collection failures, implementation edits, and dirty
    # worktrees must all fail closed BEFORE a comparison process can be spawned.
    def bad_derive(kind):
        def worker(agent, prompt, wt):
            derive(agent, prompt, wt)
            path = wt / "tests/test_feature.py"
            if kind == "passing":
                path.write_text("def test_feature(): assert True\n")
            elif kind == "collection":
                path.write_text("import missing_test_dependency_731\n")
            elif kind == "implementation":
                (wt / "feature.txt").write_text("fixed")
            elif kind == "dirty":
                (wt / "uncommitted").write_text("dirty")
            if kind != "dirty":
                git(wt, "add", "tests/test_feature.py", "feature.txt")
                git(wt, "commit", "--amend", "--no-edit")

        return worker

    for kind in ["passing", "collection", "implementation", "dirty"]:
        before = events.count("arm")

        def freeze_bad(*a, **kw):
            return original_freeze(*a, **kw, derive_fn=bad_derive(kind))

        monkeypatch.setattr(producer, "freeze_test", freeze_bad)
        with pytest.raises(ValueError):
            exp_abcd.prepare_arms(
                "a/b", str(spec), kind, arms, producer_metadata={"producer": "experiment_producer"}
            )
        assert events.count("arm") == before
        assert not (tmp_path / (kind + "-test-spec")).exists()
        assert not git(canon, "branch", "--list", exp_abcd.exp_branch(kind, "codex", "test-spec"))
    # The real launcher boundary also fails closed on agent failure/timeout and stops
    # the worker process group before returning. No actual agent CLI is executed.
    for failure in ["worker-failure", "worker-timeout"]:
        with monkeypatch.context() as launcher:
            killed, accounted = [], []

            class Worker:
                pid = 731123
                waits = 0

                def wait(self, timeout=None):
                    self.waits += 1
                    if failure == "worker-timeout" and self.waits == 1:
                        raise subprocess.TimeoutExpired(["stub-agent"], timeout)
                    return 1

            def popen(argv, **kwargs):
                assert argv[:2] == ["bash", "-lc"]
                assert kwargs["start_new_session"] is True
                env = kwargs["env"]
                assert "file:///dev/null/orch-no-push" in env.values()
                return Worker()

            launcher.setattr(
                producer,
                "subprocess",
                SimpleNamespace(
                    Popen=popen,
                    TimeoutExpired=subprocess.TimeoutExpired,
                    STDOUT=subprocess.STDOUT,
                    DEVNULL=subprocess.DEVNULL,
                ),
            )
            launcher.setattr(producer.os, "killpg", lambda *a: killed.append(a))
            launcher.setattr(exp_abcd.adapters, "build_command", lambda *a, **kw: ["stub-agent"])
            launcher.setattr(
                exp_abcd.adapters, "record_ledger", lambda *a, **kw: accounted.append(kw)
            )
            launcher.setattr(producer, "freeze_test", original_freeze)
            before = events.count("arm")
            with pytest.raises(producer.FrozenTestError):
                exp_abcd.prepare_arms(
                    "a/b",
                    str(spec),
                    failure,
                    arms,
                    producer_metadata={"producer": "experiment_producer"},
                )
            assert events.count("arm") == before
            assert not (tmp_path / (failure + "-test-spec")).exists()
            assert not git(
                canon, "branch", "--list", exp_abcd.exp_branch(failure, "codex", "test-spec")
            )
            assert bool(killed) == (failure == "worker-timeout")
            assert len(accounted) == 1
    spec.write_text("No test or acceptance criteria")
    monkeypatch.setattr(producer, "freeze_test", original_freeze)
    with pytest.raises(ValueError, match="no_named_test"):
        exp_abcd.prepare_arms(
            "a/b", str(spec), "no-test", arms, producer_metadata={"producer": "experiment_producer"}
        )


def test_producer_derive_failure_dedup_and_day_rollover(monkeypatch):
    cap, kwargs = plan_fixture(monkeypatch)
    now = 1800000000
    monkeypatch.setattr(producer.time, "time", lambda: now)
    item = {"target": "a/b#3", "body": "## Acceptance Criteria\n- [ ] Fixed"}
    assert producer.record_skip("derive_failed", target=item["target"])
    assert not producer.record_skip("derive_failed", target=item["target"])
    assert producer.build_plan([item], cap, **kwargs)["planned"] == []
    state = producer.read_state()
    assert state["derive_failed"] == 1 and not state.get("skips")
    import switch_review

    assert "derive_failed=1" in switch_review.experiment_producer_line()
    now += 86400
    assert producer.build_plan([item], cap, **kwargs)["planned"]
    assert producer.record_skip("derive_failed", target=item["target"])
    assert producer.read_state()["derive_failed"] == 2


def test_producer_red_runner_and_isolation(tmp_path, monkeypatch):
    import os
    import sys

    canon = tmp_path / "consumer"
    python = canon / ".venv/bin/python"
    python.parent.mkdir(parents=True)
    python.symlink_to(sys.executable)
    assert producer._pytest_runner(canon) == [str(python), "-m", "pytest"]
    python.unlink()
    with pytest.raises(ValueError, match="selected consumer interpreter is unavailable"):
        producer._pytest_runner(canon)
    python.parent.rmdir()
    python.parent.parent.rmdir()
    (canon / "uv.lock").touch()
    assert producer._pytest_runner(canon) == ["uv", "run", "--project", str(canon), "pytest"]
    (canon / "uv.lock").unlink()
    assert producer._pytest_runner(canon)[0] == "/opt/anaconda3/bin/python3"
    for key in (
        "GH_TOKEN",
        "META_API_KEY",
        "AWS_SECRET_ACCESS_KEY",
        "GIT_CONFIG_VALUE_9",
        "PYTHONPATH",
    ):
        monkeypatch.setenv(key, "secret")
    env = producer._red_environment("codex", tmp_path)
    assert all(env.get(k) != "secret" for k in os.environ)
    assert env["HOME"] == str(tmp_path)
    assert env["GIT_CONFIG_VALUE_0"] == "file:///dev/null/orch-no-push"
    sandbox = tmp_path / "sandbox-exec"
    sandbox.touch()
    monkeypatch.setattr(exp_abcd.adapters, "SANDBOX_EXEC", str(sandbox))
    argv = producer._red_sandbox(["pytest", "tests/test_x.py"], canon, canon, tmp_path)
    assert argv[:2] == [str(sandbox), "-p"]
    assert "(deny network*)" in argv[2] and "(deny file-write*)" in argv[2]
    assert "(deny file-read*" in argv[2]
    assert argv[-2:] == ["pytest", "tests/test_x.py"]
