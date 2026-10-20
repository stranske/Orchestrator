"""Issue #828 round 2: hermetic coverage for the remaining ranked targets.

Targets (repo ranking order): keepalive_outcomes, issue_quality, experiment_recovery,
research_usage_guard. Every external process/network call is stubbed.
"""

from __future__ import annotations

import json
import socket
import sqlite3
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

import experiment_recovery
import feedback
import issue_quality as quality
import keepalive_outcomes as ko
import research_usage_guard as rug
import route_weights_export


@pytest.fixture(autouse=True)
def isolated_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("ORCH_LOCAL_RUNTIME", str(tmp_path))
    monkeypatch.setenv("ORCH_FEEDBACK_DB", str(tmp_path / "feedback.db"))
    monkeypatch.setenv("ORCH_CAPABILITIES_PATH", str(tmp_path / "capabilities.json"))
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "feedback.db")
    attempted: list[object] = []

    def forbidden(*args, **kwargs):
        attempted.append((args, kwargs))
        raise AssertionError("issue828 round2 test attempted an external process or network call")

    for name in ("run", "Popen", "call", "check_call", "check_output"):
        monkeypatch.setattr(subprocess, name, forbidden)
    for name in ("connect", "connect_ex", "sendto"):
        monkeypatch.setattr(socket.socket, name, forbidden)
    yield
    assert attempted == []


def _merged_pr(number: int, *, labels=(), head="codex/issue-1", author="bot", title="fix"):
    return {
        "number": number,
        "state": "MERGED",
        "title": title,
        "labels": [{"name": lab} for lab in labels],
        "createdAt": "2026-09-18T10:00:00Z",
        "updatedAt": "2026-09-18T12:00:00Z",
        "mergedAt": "2026-09-18T12:00:00Z",
        "closedAt": "2026-09-18T12:00:00Z",
        "headRefName": head,
        "baseRefName": "main",
        "mergeCommit": {"oid": "abc123"},
        "author": {"login": author},
        "body": "",
        "url": f"https://github.com/o/r/pull/{number}",
    }


# ----- keepalive_outcomes -------------------------------------------------


def test_work_type_and_label_author_helpers_cover_each_class() -> None:
    assert ko._work_type([], "chore(deps): bump x", "renovate[bot]") == "renovate"
    assert ko._work_type(["dependencies"], "Bump foo", None) == "renovate"
    assert ko._work_type([], "⬆ Update package", None) == "renovate"
    assert ko._work_type(["sync"], "anything", None) == "sync"
    assert ko._work_type([], "consumer hygiene pass", None) == "sync"
    assert ko._work_type(["ci"], "tooling", None) == "tooling"
    assert ko._work_type([], "ci: wire gate", None) == "tooling"
    assert ko._work_type(["docs"], "note", None) == "docs"
    assert ko._work_type([], "docs: clarify", None) == "docs"
    assert ko._work_type([], "Implement feature", None) == "issue"

    assert ko._label_names({"labels": ["a", {"name": "b"}, {"other": 1}, None]}) == ["a", "b"]
    assert ko._author_login({"author": {"login": "x"}}) == "x"
    assert ko._author_login({"author": "y"}) == "y"
    assert ko._author_login({"author": {}}) is None


def test_process_ignore_and_attach_commit_identities(monkeypatch) -> None:
    closed = {
        "number": 9,
        "state": "CLOSED",
        "labels": [],
        "author": {"login": "someone"},
        "body": "",
        "headRefName": "feature/x",
    }
    oc = {"durability": "abandoned", "notes": ""}
    monkeypatch.setattr(ko, "_gh_throttle", lambda _r: None)
    tagged = ko._maybe_mark_process_ignore(
        "o/r", closed, "renovate", oc, lambda _repo, _n: "process_ignore=duplicate"
    )
    assert tagged["notes"] == "process_ignore=duplicate"
    assert ko._maybe_mark_process_ignore("o/r", closed, "issue", oc, lambda *_: "duplicate") is oc
    assert ko._maybe_mark_process_ignore("o/r", closed, "renovate", None, lambda *_: "x") is None

    prs = [
        {"number": None},
        _merged_pr(1, labels=["agent:codex"], head="feature/x"),
        _merged_pr(2, head="feature/unresolved", author="nobody"),
    ]
    calls = []

    def evidence(repo, numbers):
        calls.append((repo, list(numbers)))
        return {2: {"commit_identities": ["claude"]}}

    assert ko._attach_commit_identities("o/r", prs, evidence_fn=evidence) == 1
    assert calls == [("o/r", [2])]
    assert prs[2]["_commit_identities"] == ["claude"]
    assert ko._attach_commit_identities("o/r", [_merged_pr(3, labels=["agent:codex"])]) == 0


def test_export_policy_dispatch_metadata_and_fetch_times(monkeypatch) -> None:
    monkeypatch.setattr(ko, "_gh_throttle", lambda _r: None)
    sha = "a" * 40
    document = {
        "schema": route_weights_export.SCHEMA,
        "source_version": "v9",
        "exploration": {"policy_version": "p1", "mode": "epsilon-greedy"},
    }
    responses = iter(
        [
            [{"sha": sha}],
            document,
            "not-a-list",
            [{"sha": "short"}],
            [{"sha": sha}],
            {"schema": "wrong"},
            [{"sha": sha}],
            {
                "schema": route_weights_export.SCHEMA,
                "exploration": {"policy_version": "", "mode": "epsilon-greedy"},
            },
        ]
    )
    monkeypatch.setattr(ko, "_run_json", lambda *_a, **_k: next(responses))
    good = ko._export_policy_at(1_700_000_000)
    assert good == {
        "policy_version": "p1",
        "exploration_mode": "epsilon-greedy",
        "route_weights_version": "v9",
        "route_weights_commit": sha,
    }
    assert ko._export_policy_at(1) is None
    assert ko._export_policy_at(2) is None
    assert ko._export_policy_at(3) is None
    assert ko._export_policy_at(4) is None

    assert ko._dispatch_policy_metadata([], lambda _ts: good) == {}
    assert ko._dispatch_policy_metadata([10], lambda _ts: None) == {}
    single = ko._dispatch_policy_metadata([10, 10], lambda _ts: good)
    assert single["policy_version"] == "p1"
    assert len(single["dispatch_policies"]) == 1
    multi = ko._dispatch_policy_metadata(
        [10, 20],
        lambda ts: {**good, "policy_version": f"p{ts}"},
    )
    assert "policy_version" not in multi
    assert len(multi["dispatch_policies"]) == 2

    pages = [
        [
            {
                "body": "<!-- keepalive-runner -->",
                "createdAt": "2026-09-18T10:00:00Z",
                "user": {"login": "github-actions[bot]"},
            }
        ]
    ]
    monkeypatch.setattr(ko, "_run_json", lambda *_a, **_k: pages)
    monkeypatch.setattr(
        ko.outcomes,
        "runner_records",
        lambda _comments, **_kwargs: [{"started_at": "2026-09-18T10:00:00Z"}],
    )
    monkeypatch.setattr(ko.outcomes.utc_epoch, "from_iso", lambda value: 100 if value else None)
    assert ko._fetch_dispatch_times("o/r", 1, "codex") == [100]
    monkeypatch.setattr(ko, "_run_json", lambda *_a, **_k: {"oops": True})
    assert ko._fetch_dispatch_times("o/r", 1, "codex") is None


def test_backfill_evidence_batch_and_attribution_apply(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "brain.db")
    feedback.record_run(
        "keepalive:o/r#11:none",
        "o/r#11",
        "implement",
        "none",
        mode="remote",
        pr_number=11,
        source="keepalive",
        routing_metadata={"attribution_source": "unresolved"},
    )
    feedback.record_run(
        "keepalive:o/r#12:none",
        "bad-target",
        "implement",
        "none",
        mode="remote",
        pr_number=12,
        source="keepalive",
        routing_metadata={},
    )
    feedback.record_run(
        "keepalive:o/r#13:none",
        "o/r#13",
        "implement",
        "none",
        mode="remote",
        pr_number=13,
        source="keepalive",
        routing_metadata={"attribution_source": "branch_prefix"},
    )

    graphql = {
        "data": {
            "repository": {
                "p11": {
                    "number": 11,
                    "headRefName": "cursor/issue-11",
                    "createdAt": "2026-09-18T10:00:00Z",
                    "author": {"login": "x"},
                    "labels": {"nodes": [{"name": "agent:cursor"}]},
                    "commits": {"nodes": []},
                    "body": "",
                }
            }
        }
    }
    monkeypatch.setattr(ko, "_run_json", lambda *_a, **_k: graphql)
    batch = ko._backfill_evidence_batch("o/r", [11])
    assert batch[11]["labels"] == ["agent:cursor"]
    assert batch[11]["head_ref"] == "cursor/issue-11"

    # Chunk failure with multiple numbers falls back to per-PR queries.
    calls = []

    def flaky(_args, **_kwargs):
        calls.append(1)
        if len(calls) == 1:
            return {"data": {"repository": None}}
        return graphql

    monkeypatch.setattr(ko, "_run_json", flaky)
    assert 11 in ko._backfill_evidence_batch("o/r", [11, 99])

    rest_calls = []

    def rest_api(path: str):
        rest_calls.append(path)
        if path.endswith("/pulls/11"):
            return {
                "labels": [{"name": "agent:cursor"}],
                "author": {"login": "cursor[bot]"},
                "body": "",
            }
        if "keepalive-loop" in path:
            return {
                "workflow_runs": [
                    {"id": 9, "pull_requests": [{"number": 11}]},
                    {"id": 8, "pull_requests": [{"number": 11}]},
                ]
            }
        if path.endswith("/jobs?per_page=100"):
            return {"jobs": [{"name": "keepalive next task (cursor)"}]}
        return None

    monkeypatch.setattr(ko, "_gh_api_json", rest_api)
    evidence = ko._backfill_evidence("o/r", 11)
    assert evidence["labels"] == ["agent:cursor"]
    assert "keepalive next task (cursor)" in evidence["job_names"]
    assert rest_calls[0].endswith("/pulls/11")

    summary = ko.backfill_attribution(
        apply=True,
        backfill_limit=10,
        _evidence_fetch_fn=lambda repo, number: {
            "labels": ["agent:codex"],
            "job_names": [],
            "author": "x",
            "summary": "",
            "head_ref": "codex/issue-11",
            "commit_identities": [],
            "created_ts": 1,
        },
    )
    assert summary["resolved"] >= 1
    assert summary["applied"] is True
    with feedback._conn() as c:
        agent = c.execute(
            "SELECT agent FROM runs WHERE run_id=?", ("keepalive:o/r#11:none",)
        ).fetchone()[0]
    assert agent == "codex"


def test_local_dispatch_bot_human_attribution_and_stamp_policy(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "brain.db")
    feedback.record_run(
        "local:o/r#5:cursor",
        "o/r#5",
        "implement",
        "cursor",
        mode="local",
        source="orchestrator",
        ts=1_000,
    )
    assert ko._agent_from_local_dispatch("o/r", "orchestrator/issue-5", 2_000) == (
        "cursor",
        "local_dispatch_join",
    )
    assert ko._agent_from_local_dispatch("o/r", "other/branch", 2_000) is None
    assert ko._bot_commit_class(["github-actions[bot]", "renovate"]) == (
        "none",
        "bot:actions",
    )
    assert ko._bot_commit_class(["claude"]) is None
    assert ko._human_class(["tim stranske", "stranske"]) == ("none", "human")
    assert ko._human_class(["tim stranske", "other"]) is None

    run_id = "keepalive:o/r#5:cursor"
    feedback.record_run(
        run_id,
        "o/r#5",
        "implement",
        "cursor",
        mode="remote",
        pr_number=5,
        source="keepalive",
        routing_metadata={"attribution_source": "branch_prefix"},
    )
    policy = {
        "policy_version": "p1",
        "exploration_mode": "epsilon-greedy",
        "route_weights_version": "v1",
        "route_weights_commit": "b" * 40,
    }
    ko._stamp_existing_dispatch_policy(
        run_id,
        "o/r",
        5,
        "cursor",
        times_fn=lambda *_a: [50],
        policy_fn=lambda _ts: policy,
        settled_ts=99,
    )
    with feedback._conn() as c:
        meta = json.loads(
            c.execute("SELECT routing_metadata FROM runs WHERE run_id=?", (run_id,)).fetchone()[0]
        )
    assert meta["policy_version"] == "p1"
    assert meta["policy_settled_ts"] == 99

    # Already settled with the same stamp is a no-op.
    ko._stamp_existing_dispatch_policy(
        run_id,
        "o/r",
        5,
        "cursor",
        times_fn=lambda *_a: (_ for _ in ()).throw(AssertionError("should not fetch")),
        policy_fn=lambda _ts: (_ for _ in ()).throw(AssertionError("should not resolve")),
        settled_ts=99,
    )


def test_outcome_for_pr_and_ingest_agent_and_non_agent_paths(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "brain.db")
    monkeypatch.setattr(ko, "_gh_throttle", lambda _r: None)

    merged = _merged_pr(21, labels=["agent:codex"], head="codex/issue-21", title="ci: fix")
    closed = {
        **_merged_pr(
            22, labels=[], head="renovate/pin", author="renovate[bot]", title="chore(deps)"
        ),
        "state": "CLOSED",
        "mergedAt": None,
    }
    open_pr = {**_merged_pr(23, labels=["agent:codex"]), "state": "OPEN", "mergedAt": None}

    monkeypatch.setattr(
        ko.durability_sweep,
        "classify_durability",
        lambda *_a, **_k: {"durability": "durable", "notes": "ok"},
    )
    monkeypatch.setattr(ko.durability_sweep, "verifier_candidate_run_ids", lambda *_a, **_k: set())
    oc = ko._outcome_for_pr(
        "o/r",
        {**merged, "_verifier_decision": {"verdict": "PASS"}},
        {"run_id": "keepalive:o/r#21:codex"},
        now=1_700_000_000,
        _revert_fn=lambda _pr: (None, "skip"),
        _fix_fn=lambda *_a, **_k: ([], True),
    )
    assert oc["durability"] == "durable"
    assert oc["merged"] is True

    summary = ko.ingest_keepalive_outcomes(
        ["o/r"],
        dry_run=False,
        include_non_agent=True,
        _pr_fetch_fn=lambda *_a, **_k: [merged, closed, open_pr],
        _closure_context_fn=lambda *_a: "Closing as a duplicate",
        _evidence_fetch_fn=lambda *_a, **_k: {},
        _verifier_fetch_fn=lambda *_a, **_k: {},
        _dispatch_times_fn=lambda *_a, **_k: [10],
        _policy_at_dispatch_fn=lambda _ts: {
            "policy_version": "p1",
            "exploration_mode": "thompson-hybrid",
            "route_weights_version": "v1",
            "route_weights_commit": "c" * 40,
        },
        _revert_fn=lambda _pr: (None, "skip"),
        _fix_fn=lambda *_a, **_k: ([], True),
        shadow_budget=0,
    )
    assert summary["prs_seen"] >= 2
    assert summary["runs_recorded"] >= 2
    assert summary["non_agent_runs_recorded"] >= 1
    with feedback._conn() as c:
        rows = c.execute("SELECT run_id, agent, work_type FROM runs").fetchall()
    agents = {row[1] for row in rows}
    assert "codex" in agents
    assert "none" in agents


def test_keepalive_main_backfill_and_ingest_cli(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        ko,
        "backfill_attribution",
        lambda **kwargs: {"examined": 1, "applied": kwargs.get("apply", False)},
    )
    assert ko.main(["--backfill-attribution", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["examined"] == 1

    monkeypatch.setattr(
        ko,
        "ingest_keepalive_outcomes",
        lambda *a, **k: {
            "repos": 0,
            "prs_seen": 0,
            "runs_recorded": 0,
            "outcomes_recorded": 0,
            "skipped_existing": 0,
            "non_agent_prs_seen": 0,
            "non_agent_runs_recorded": 0,
            "by_source": {},
            "attribution": {"let_through": 0, "blocked": 0, "by_source": {}},
            "fetch_failed_repos": [],
            "commit_identity_enriched": 0,
            "verifier_verdicts_seen": 0,
            "runtime_ac_specs_authored": 0,
            "runtime_ac_shadow_errors": [],
            "runtime_ac_checkout_failures": 0,
            "runtime_ac_checkout_errors": [],
            "replacements": {},
        },
    )
    monkeypatch.setattr(ko.outcomes, "recheck_keepalive_replacements", lambda **_k: {"changed": 0})
    assert ko.main(["--dry-run", "--json", "--repos", "o/r"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["replacement_recheck"] == {"changed": 0}


# ----- issue_quality ------------------------------------------------------


def test_issue_quality_parse_edges_parallel_fetch_and_human_main(monkeypatch, capsys) -> None:
    assert quality._target_parts({"target": "o/r#bad", "run_id": "x", "pr_number": None}) is None
    assert quality._target_parts(
        {"target": "o/r", "pr_number": "bad", "run_id": "keepalive:o/r#3:x"}
    ) == ("o/r", 3)
    assert quality._target_parts({"target": "o/r", "pr_number": "nope", "run_id": "x"}) is None

    monkeypatch.setattr(quality, "_run_json", lambda *_a, **_k: {"closingIssuesReferences": "x"})
    assert quality.fetch_linked_issue_numbers("o/r", 1) == []
    monkeypatch.setattr(quality, "_run_json", lambda *_a, **_k: ["not-dict"])
    assert quality.fetch_issue_body("o/r", 1) is None
    assert quality._coerce_issue_payload(12) is None

    rows = [
        quality.RunRow("a", 1, "o/r#1", 1, "durable"),
        quality.RunRow("b", 1, "o/r#2", 2, "durable"),
    ]

    def linked(_repo, pr):
        if pr == 1:
            raise RuntimeError("boom")
        return ["bad", 9]

    def body(_repo, issue):
        if issue == 9:
            raise RuntimeError("body boom")
        return None

    records, skipped = quality.resolve_issue_records(
        rows, linked_issue_fn=linked, body_fetch_fn=body, max_workers=2
    )
    assert records == []
    reasons = {item["reason"] for item in skipped}
    assert "no closingIssuesReferences" in reasons
    assert "could not fetch issue body" in reasons or "invalid linked issue number" in reasons

    report = {
        "baseline": {"n": 1, "durable": 1, "durable_rate": 1.0},
        "features": [
            {
                "label": "has_repro_steps",
                "with": {"n": 1, "durable_rate": 1.0},
                "without": {"n": 0, "durable_rate": None},
                "lift": None,
                "powered": False,
            }
        ],
        "underpowered": [],
        "issues": [{"score": 1.0, "durability": "durable", "target": "o/r#1", "issue_number": 1}],
        "lookback_days": 7,
        "runs_selected": 1,
        "issues_analyzed": 1,
        "min_cell_n": 10,
    }
    human = quality.format_human(report)
    assert "Underpowered - not findings: none" in human

    monkeypatch.setattr(quality, "build_report", lambda **_k: report)
    assert quality.main(["--lookback-days", "7", "--workers", "2"]) == 0
    assert "Issue quality -> durability associations" in capsys.readouterr().out


# ----- experiment_recovery ------------------------------------------------


def test_experiment_recovery_git_spec_unevaluated_and_main(tmp_path, monkeypatch, capsys) -> None:
    assert experiment_recovery._canonical_repo("owner/repo#9 [exp tick]") == "owner/repo"
    monkeypatch.setattr(experiment_recovery.provision, "REPOS_DIR", tmp_path)
    monkeypatch.setattr(experiment_recovery.provision, "repo_slug", lambda repo: "slug")
    assert experiment_recovery.repo_store("owner/repo") == tmp_path / "slug"

    monkeypatch.setattr(
        experiment_recovery.subprocess,
        "run",
        lambda *_a, **_k: (_ for _ in ()).throw(OSError("no git")),
    )
    assert experiment_recovery._git(tmp_path, "status") == ""

    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "brain.db")
    feedback.record_run(
        "exp:1",
        "owner/repo#1",
        "testgen",
        "codex",
        mode="local",
        experiment_id="tick-1-owner-repo-1",
        ts=50,
    )
    rows = experiment_recovery.unevaluated_experiments()
    assert rows[0]["exp_id"] == "tick-1-owner-repo-1"
    assert rows[0]["repo"] == "owner/repo"
    assert rows[0]["agents"] == ["codex"]

    cache: dict[str, str] = {}
    body = experiment_recovery._spec_for(
        "owner/repo#7", cache=cache, gh_fn=lambda _r, _n: "# Title\n\nBody"
    )
    assert "Body" in body
    assert experiment_recovery._spec_for("owner/repo#7", cache=cache, gh_fn=lambda *_: "x") == body
    stub = experiment_recovery._spec_for("owner/repo#8", cache={}, gh_fn=lambda *_: "")
    assert experiment_recovery.MISSING_SPEC_STUB_HEADER in stub

    monkeypatch.setattr(
        experiment_recovery,
        "arm_diff",
        lambda *_a, **_k: None,
    )
    missing = experiment_recovery.restore_experiment(
        {
            "exp_id": "tick-9-x",
            "repo": "owner/repo",
            "target": "owner/repo#9",
            "agents": ["codex"],
        },
        apply=True,
        exp_dir=tmp_path / "experiments",
    )
    assert missing["status"] == "unrecoverable"
    assert missing["applied"] is False

    monkeypatch.setattr(
        experiment_recovery,
        "plan",
        lambda **_k: {
            "experiments": 1,
            "counts": {"partial": 1},
            "arms_recovered": 1,
            "arms_missing": 1,
            "distinct_subjects": 1,
            "subject_histogram": [("subj", 1)],
            "results": [],
        },
    )
    assert experiment_recovery.main([]) == 0
    out = capsys.readouterr().out
    assert "experiments=1" in out and "subj" in out
    assert experiment_recovery.main(["--json"]) == 0
    assert json.loads(capsys.readouterr().out)["experiments"] == 1


# ----- research_usage_guard -----------------------------------------------


def test_research_usage_guard_report_main_and_config_alert(tmp_path, monkeypatch, capsys) -> None:
    db = sqlite3.connect(tmp_path / "rug.db")
    rug.ensure_schema(db)
    monkeypatch.setattr(
        rug.capabilities,
        "production_heartbeat",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("ignore")),
    )
    report_path = tmp_path / "report.json"
    written = rug.write_usage_report(report_path, conn=db)
    assert report_path.exists()
    assert written["health_status"] in {
        "OK",
        "WARN",
        "ALERT",
        "UNKNOWN",
        "OBSERVABILITY_UNAVAILABLE",
    }

    monkeypatch.setenv("ORCH_GUARD_MAX_CONSECUTIVE_SUBJECT", "not-an-int")
    result = rug.assess_and_record_opportunity(
        exp_id="tick-1-subj",
        repo="owner/repo",
        subject="subj",
        spec_text="spec",
        base_sha="abc",
        candidate_diffs={"codex": "diff"},
        env={"ORCH_RESEARCH_ARM": "1", "ORCH_GUARD_MAX_CONSECUTIVE_SUBJECT": "bad"},
        conn=db,
        now=1_700_000_000,
    )
    assert result["decision"] == "blocked_by_anomaly"
    assert result["reason"] == "guard_config_invalid"

    monkeypatch.setattr(
        rug,
        "generate_usage_report",
        lambda **_k: {
            "health_status": "ALERT",
            "window_start_ts": 1,
            "window_end_ts": 2,
            "in_window_opportunities": 0,
            "outside_window_opportunities": 0,
            "window_coverage_note": "none",
            "decision_counts": {},
            "total_admitted_prompt_bytes": 0,
            "total_admitted_evaluator_calls": 0,
            "alerts_summary": {"budget": 1},
        },
    )
    assert rug.main(["report", "--fail-on-alert"]) == 2
    text = capsys.readouterr().out
    assert "Usage Guard Report" in text
    assert rug.main(["report", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["health_status"] == "ALERT"

    out_path = tmp_path / "cli-report.json"
    monkeypatch.setattr(
        rug, "write_usage_report", lambda path: {"health_status": "OK", "path": str(path)}
    )
    assert rug.main(["report", "--write-report", str(out_path), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["health_status"] == "OK"
