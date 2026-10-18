"""Goal ledger acceptance gates; all state and GitHub effects are fixture-owned."""

import json
import sqlite3
import time
from dataclasses import asdict, replace
from datetime import datetime, timezone

import pytest

import feature_goals as fg

NOW = 1_800_000_000
GOAL = fg.FEATURES[0]
pytestmark = pytest.mark.feature_goals_real


def observation(value, when, goal=GOAL):
    return {
        "feature": goal.feature,
        "goal_id": goal.goal_id,
        "as_of": when,
        "measurement": asdict(
            fg.Measurement(value, "recorded fixture", when, "scored judge query")
        ),
    }


def section(value, days=0, goal=GOAL):
    return fg.drift(
        [observation(value, NOW - days * fg.DAY, goal), observation(value, NOW, goal)],
        NOW,
        goals=[goal],
    )


def test_unmeasured_and_measured_zero_are_distinct():
    missing = section(None)
    zero = section(0.0)
    assert missing["goals"][0]["state"] == "unmeasured"
    assert zero["goals"][0]["state"] == "measured_zero"
    assert "unmeasured — would need scored judge query" in "\n".join(fg.format_lines(missing))
    assert "measured zero" in "\n".join(fg.format_lines(zero))
    assert "measured zero" not in "\n".join(fg.format_lines(missing))


def test_drained_ledger_prints_zero_drifting(monkeypatch):
    import switch_review

    drained = fg.drift(
        [observation(1.0, NOW, g) for g in fg.FEATURES if g.direction == "increase"]
        + [observation(0.0, NOW, g) for g in fg.FEATURES if g.direction != "increase"],
        NOW,
    )
    assert drained["drifting"] == 0
    assert drained["features"] == 5
    assert all(row["state"] == "ok" for row in drained["goals"])
    rendered = "\n".join(fg.format_lines(drained))
    assert "feature goals: 5 features, 0 drifting" in rendered
    assert len(drained["goals"]) == len(fg.FEATURES)
    # Actual weekly formatter, including the drained state, immediately after value chain.
    monkeypatch.setattr(switch_review, "profile_trial_summary_line", lambda: "")
    import triage_shadow

    monkeypatch.setattr(triage_shadow, "summary_line", lambda: "")
    report = {
        "as_of": NOW,
        "review_days": 7,
        "held_off": [],
        "on_but_idle": [],
        "unconditioned": [],
        "raise_count": 0,
        "feature_goals": drained,
    }
    text = switch_review.format_report(report)
    assert "## Feature goals" in text
    assert "5 features, 0 drifting" in text


def test_check_exits_3_after_14_days_silence_and_0_before(monkeypatch, capsys):
    for days, code in [(13, 0), (14, 3)]:
        monkeypatch.setattr(fg, "report", lambda: section(None, days))
        assert fg.main(["--check"]) == code
        assert f"Drift horizon: {fg.GOAL_SILENCE_DAYS} days" in capsys.readouterr().out
    monkeypatch.setattr(fg, "GOAL_SILENCE_DAYS", 13)
    monkeypatch.setattr(fg, "report", lambda: section(None, 13))
    assert fg.main(["--check"]) == 3
    assert "Drift horizon: 13 days" in capsys.readouterr().out


class RecordedGh:
    """REST pages and mutation fields, retaining state across daily invocations."""

    def __init__(self):
        self.issues = []
        self.calls = []
        self.labels = {"goal-drift", "priority:high"}

    def __call__(self, args):
        self.calls.append(args)
        assert args[0] == "api" and "graphql" not in args
        if args[1].startswith(f"repos/{fg.REPO}/issues?"):
            assert args == [
                "api",
                f"repos/{fg.REPO}/issues?labels=goal-drift&state=all&per_page=100",
                "--paginate",
                "--slurp",
            ]
            return json.dumps([self.issues[:1], self.issues[1:]])
        if args[1].startswith(f"repos/{fg.REPO}/labels/"):
            if args[1].rsplit("/", 1)[1] not in self.labels:
                raise RuntimeError("HTTP 404")
            return "{}"
        assert args[1] == "-X"
        method, endpoint = args[2:4]
        fields = args[4:]
        values = [fields[i + 1] for i in range(0, len(fields), 2)]

        def field(name):
            return next(v.split("=", 1)[1] for v in values if v.startswith(name + "="))

        if method == "POST" and endpoint.endswith("/labels"):
            self.labels.add(field("name"))
        elif method == "POST":
            assert endpoint == f"repos/{fg.REPO}/issues"
            assert field("title") == (
                "[goal-drift] experiments: multi-source feedback on sizable features — measured_zero for 14 days"
            )
            assert [v for v in values if v.startswith("labels[]=")] == [
                "labels[]=goal-drift",
                "labels[]=priority:high",
            ]
            assert "<!-- goal-drift:experiments:G1 -->" in field("body")
            self.issues.append(
                {
                    "number": 701,
                    "state": "open",
                    "body": field("body"),
                    "labels": [{"name": "goal-drift"}, {"name": "priority:high"}],
                }
            )
        else:
            assert method == "PATCH"
            issue = next(i for i in self.issues if endpoint.endswith("/" + str(i["number"])))
            issue["state"] = field("state")
            if "state=open" in values:
                issue["body"] = field("body")
                issue["labels"] = [
                    {"name": v.split("=", 1)[1]} for v in values if v.startswith("labels[]=")
                ]
        return "{}"


def test_drift_issue_is_idempotent_and_closes_on_recovery():
    gh = RecordedGh()
    bad = section(0.0, 14)
    assert fg.reconcile_issues(bad, gh=gh)["changes"] == ["created"]
    assert "<!-- goal-drift:experiments:G1 -->" in gh.issues[0]["body"]
    gh.issues[0]["labels"].append({"name": "owner:triage"})
    assert fg.reconcile_issues(bad, gh=gh)["changes"] == ["updated"]
    assert {label["name"] for label in gh.issues[0]["labels"]} == {
        "goal-drift",
        "priority:high",
        "owner:triage",
    }
    assert len(gh.issues) == 1
    assert fg.reconcile_issues(section(None), gh=gh)["changes"] == []
    assert gh.issues[0]["state"] == "open", "unknown cannot close a drift issue"
    assert fg.reconcile_issues(section(1.0), gh=gh)["changes"] == ["closed"]
    assert gh.issues[0]["state"] == "closed"
    fg.reconcile_issues(bad, gh=gh)
    assert len(gh.issues) == 1
    assert gh.issues[0]["state"] == "open"
    assert (
        sum(call[:4] == ["api", "-X", "POST", f"repos/{fg.REPO}/issues"] for call in gh.calls) == 1
    )


def test_feature_groups_multiple_drifting_goals_into_one_issue():
    gh = RecordedGh()
    goals = fg.FEATURES[:3]
    history = [observation(0.0, when, g) for g in goals for when in [NOW - 14 * fg.DAY, NOW]]
    rep = fg.drift(history, NOW, goals=goals)
    assert rep["drifting"] == 1
    fg.reconcile_issues(rep, gh=gh)
    assert len(gh.issues) == 1
    assert all(f"goal-drift:experiments:{g.goal_id}" in gh.issues[0]["body"] for g in goals)
    # Changing the oldest/selected bad goal cannot create a second issue.
    history += [observation(1.0, NOW, goals[0])]
    fg.reconcile_issues(fg.drift(history, NOW, goals=goals), gh=gh)
    assert len(gh.issues) == 1


def test_report_appends_and_recovery_resets_silence(tmp_path):
    path = tmp_path / "feature-goals-history.jsonl"

    def read(value):
        return lambda goal, now: fg.Measurement(value, "fixture", now, "query")

    assert fg.report(NOW, path=path, goals=[GOAL], measure_fn=read(0))["drifting"] == 0
    assert (
        fg.report(NOW + 14 * fg.DAY, path=path, goals=[GOAL], measure_fn=read(0))["drifting"] == 1
    )
    assert (
        fg.report(NOW + 15 * fg.DAY, path=path, goals=[GOAL], measure_fn=read(1))["drifting"] == 0
    )
    assert (
        fg.report(NOW + 16 * fg.DAY, path=path, goals=[GOAL], measure_fn=read(None))["drifting"]
        == 0
    )
    assert len(path.read_text().splitlines()) == 4


def test_wrong_direction_uses_the_horizon_and_zero_target_recovers():
    rep = fg.drift([observation(5, NOW - 14 * fg.DAY), observation(4, NOW)], NOW, goals=[GOAL])
    assert rep["goals"][0]["state"] == "wrong_direction"
    assert rep["drifting"] == 0, "a newly detected regression is not fourteen days of regression"
    zero_goal = next(g for g in fg.FEATURES if g.feature == "dedup-guard")
    assert section(0.0, 14, zero_goal)["goals"][0]["state"] == "ok"
    assert section(1.0, 14, zero_goal)["drifting"] == 1


def test_measure_missing_brain_is_unknown_and_does_not_create_it(tmp_path, monkeypatch):
    import feedback

    db = tmp_path / "missing.db"
    monkeypatch.setattr(feedback, "DB_PATH", db)
    result = fg.measure(GOAL, NOW)
    assert result.value is None
    assert "multi_source_feedback" in result.reason
    assert not db.exists()


def test_measure_empty_schema_is_zero_but_missing_table_is_unknown(tmp_path, monkeypatch):
    import feedback

    db = tmp_path / "brain.db"
    monkeypatch.setattr(feedback, "DB_PATH", db)
    with sqlite3.connect(db) as conn:
        conn.executescript(
            "CREATE TABLE runs(experiment_id TEXT,target TEXT);"
            "CREATE TABLE evaluations_v2(experiment_id TEXT,ts INT,score REAL,evaluator_id TEXT);"
        )
    assert fg.measure(GOAL, NOW).value == 0.0
    with sqlite3.connect(db) as conn:
        conn.execute("DROP TABLE evaluations_v2")
    assert fg.measure(GOAL, NOW).value is None


def test_multi_source_feedback_live_targets_and_metadata(tmp_path, monkeypatch):
    import exp_abcd
    import feedback

    db = tmp_path / "brain.db"
    monkeypatch.setattr(feedback, "DB_PATH", db)
    monkeypatch.setattr(exp_abcd, "EXP_DIR", tmp_path / "experiments")
    fixtures = {
        "a": {"subject": {"target": "stranske/Orchestrator#1 (feature)"}},
        "b": {"subject": {"target": "unresolved subject"}, "target": "stranske/Orchestrator#2"},
        "c": {"subject": {"target": "stranske/Orchestrator#3"}},
        "missing": None,
        "invalid": {"subject": None},
        "bad-json": "{",
        "excluded": {"subject": {"target": next(iter(fg.EXCLUDED_REPOS)) + "#1"}},
    }
    for experiment, meta in fixtures.items():
        directory = exp_abcd.exp_paths(experiment)
        directory.mkdir(parents=True)
        if meta is not None:
            (directory / "meta.json").write_text(
                meta if isinstance(meta, str) else json.dumps(meta)
            )
    with sqlite3.connect(db) as conn:
        conn.executescript(
            "CREATE TABLE runs(experiment_id TEXT,target TEXT);"
            "CREATE TABLE evaluations_v2(experiment_id TEXT,ts INT,score REAL,evaluator_id TEXT);"
        )
        for experiment in fixtures:
            conn.execute(
                "INSERT INTO runs VALUES(?,?)",
                (experiment, f"stranske/Orchestrator [exp {experiment} arm default member codex]"),
            )
            for judge in ("judge1", "judge2"):
                conn.execute(
                    "INSERT INTO evaluations_v2 VALUES(?,?,?,?)", (experiment, NOW, 1, judge)
                )
        conn.execute(
            "INSERT INTO runs VALUES('a','stranske/Orchestrator [exp a arm second member y]')"
        )
        conn.execute(
            "INSERT INTO runs VALUES('single','stranske/Orchestrator [exp single arm x member y]')"
        )
        conn.execute("INSERT INTO evaluations_v2 VALUES('single',?,1,'judge1')", (NOW,))
    calls = []

    def issue(args):
        calls.append(args)
        assert args[0] == "api"
        if args[1].endswith("/1"):
            return json.dumps({"body": "- [ ] goal\n" * 9, "labels": []})
        if args[1].endswith("/2"):
            return json.dumps({"body": None, "labels": [{"name": "experiment:forum"}]})
        raise RuntimeError("HTTP 404")

    monkeypatch.setattr(fg, "_gh", issue)
    result = fg.multi_source_feedback(NOW)
    assert result.value == 2.0
    assert result.reason == "unattributable 3; issue reads failed 1"
    assert calls == [["api", f"repos/stranske/Orchestrator/issues/{n}"] for n in (1, 2, 3)]
    monkeypatch.setattr(fg, "_gh", lambda args: json.dumps({"body": "small", "labels": []}))
    result = fg.multi_source_feedback(NOW)
    assert result.value == 0.0
    assert result.inactive_since == NOW - fg.GOAL_SILENCE_DAYS * fg.DAY


def test_absent_sources_have_explicit_reasons():
    # Every live goal now has a source; an unresolvable measure must still explain itself.
    goal = replace(GOAL, measure="feature_goals.py:no_such_measure")
    result = fg.measure(goal, NOW)
    assert result.value is None
    assert result.reason


def test_promotion_reuses_existing_reader(monkeypatch):
    import switch_review

    monkeypatch.setattr(
        switch_review, "_promote_age_days", lambda **kw: {"days_since_success": 17.0}
    )
    assert fg.sync_promotion(NOW).value == 17.0
    monkeypatch.setattr(
        switch_review,
        "_promote_age_days",
        lambda **kw: {"days_since_success": None, "measurement": "gh unavailable"},
    )
    assert fg.sync_promotion(NOW).value is None


def test_duplicate_population_is_paginated_and_truncation_is_unknown(monkeypatch):
    import issue_size_quality

    monkeypatch.setattr(issue_size_quality, "fleet_repos", lambda: ["owner/repo"])

    def recorded(args):
        second = "cursor=next" in args
        pr = {
            "number": 2 if second else 1,
            "createdAt": datetime.fromtimestamp(NOW, timezone.utc).isoformat(),
            "closingIssuesReferences": {
                "pageInfo": {"hasNextPage": False},
                "nodes": [{"number": 5, "repository": {"nameWithOwner": "owner/repo"}}],
            },
        }
        return json.dumps(
            {
                "data": {
                    "repository": {
                        "pullRequests": {
                            "pageInfo": {"hasNextPage": not second, "endCursor": "next"},
                            "nodes": [pr],
                        }
                    }
                }
            }
        )

    monkeypatch.setattr(fg, "_gh", recorded)
    assert fg.duplicate_prs(NOW).value == 1.0
    monkeypatch.setattr(fg, "_gh", lambda args: '{"errors":[{"message":"rate limit"}]}')
    goal = next(g for g in fg.FEATURES if g.feature == "dedup-guard")
    assert fg.measure(goal, NOW).value is None


@pytest.mark.parametrize("shed", ["graphql", "core", "none"])
@pytest.mark.parametrize("code", [0, 3, 1])
def test_daily_cadence_executes_once_after_both_budget_gates(tmp_path, shed, code):
    import subprocess

    import cadence_registry
    import paths

    row = next(r for r in cadence_registry.CADENCE_STEPS if r["key"] == "feature-goals")
    assert row["cadence_days"] == 0
    source = (paths.REPO_ROOT / "orchestrate.sh").read_text()
    block = (
        "if _cadence_due feature-goals"
        + source.split("if _cadence_due feature-goals", 1)[1].split(
            "if _cadence_due switch-review", 1
        )[0]
    )
    script = r"""ORCH=fixture
STAMP_DIR="$1"
_cadence_due() { return 0; }
_attempt_ok() { return 0; }
_gh_gate() { echo "gate:$1"; [[ "$1" != "$SHED" ]]; }
python3() { echo "measure:$*" >> "$STAMP_DIR/calls"; return "$CODE"; }
_mark_success() { echo success; }
_mark_fail() { echo failure; }
"""
    proc = subprocess.run(
        ["bash", "-c", script + block, "cadence", str(tmp_path)],
        env={"SHED": shed, "CODE": str(code)},
        capture_output=True,
        text=True,
        check=True,
    )
    calls = tmp_path / "calls"
    if shed != "none":
        assert not calls.exists()
        assert "success" not in proc.stdout and "failure" not in proc.stdout
    else:
        assert calls.read_text().splitlines() == [
            "measure:fixture/feature_goals.py --check --file-drift-issues"
        ]
        assert "gate:graphql\ngate:core" in proc.stdout
        assert ("failure" if code == 1 else "success") in proc.stdout


def test_retrospective_zero_window_can_raise_on_first_report(tmp_path):
    def read(goal, now):
        return fg.Measurement(0, "complete Brain query", now, inactive_since=now - 14 * fg.DAY)

    path = tmp_path / "history.jsonl"
    first = fg.report(NOW, path=path, goals=[GOAL], measure_fn=read)
    assert first["drifting"] == 1
    assert first["goals"][0]["days"] == 14
    later = fg.report(
        NOW + fg.DAY,
        path=path,
        goals=[GOAL],
        measure_fn=lambda g, n: fg.unknown(n, "source missing now"),
    )
    assert later["drifting"] == 1, "losing evidence cannot erase already observed silence"


def test_history_write_failure_remains_visible_and_fails_machine_check(
    tmp_path, monkeypatch, capsys
):
    path = tmp_path / "not-a-directory"
    path.write_text("blocked")
    rep = fg.report(NOW, path=path / "history.jsonl", goals=[GOAL])
    assert rep["errors"]
    assert rep["goals"][0]["state"] == "unmeasured"
    monkeypatch.setattr(fg, "report", lambda: rep)
    assert fg.main(["--check"]) == 1
    assert "history unavailable" in capsys.readouterr().out


def test_invalid_history_warns_and_truncated_last_line_still_reconciles(
    tmp_path, monkeypatch, capsys
):
    path = tmp_path / "history.jsonl"
    path.write_text('{"measurement":null}\n{"truncated":')
    rep = fg.report(NOW, path=path, goals=[GOAL], measure_fn=lambda g, n: fg.unknown(n, "query"))
    assert not rep["errors"]
    assert len(rep["warnings"]) == 2
    assert rep["goals"][0]["state"] == "unmeasured"
    assert rep["drifting"] == 0
    # The next append is a separate valid line even after a truncated record.
    rep2 = fg.report(NOW, path=path, goals=[GOAL], measure_fn=lambda g, n: fg.unknown(n, "query"))
    assert len(rep2["warnings"]) == 2
    calls = []
    monkeypatch.setattr(fg, "report", lambda: rep2)
    monkeypatch.setattr(fg, "reconcile_issues", lambda report: calls.append(report))
    assert fg.main(["--check", "--file-drift-issues"]) == 0
    assert calls == [rep2]
    output = capsys.readouterr().out
    assert "skipped 2 unparseable rows" in output
    assert "history line 1" in output and "history line 2" in output


def test_empty_roster_is_unknown(monkeypatch):
    import issue_size_quality

    monkeypatch.setattr(issue_size_quality, "fleet_repos", lambda: [])
    assert fg.duplicate_prs(NOW).value is None


def test_issue_read_failure_never_creates_or_closes():
    calls = []

    def failed(args):
        calls.append(args)
        raise RuntimeError("recorded gh rate limit")

    with pytest.raises(RuntimeError, match="rate limit"):
        fg.reconcile_issues(section(0, 14), gh=failed)
    assert len(calls) == 1
    assert calls[0][0] == "api" and "issues?labels=goal-drift" in calls[0][1]


def test_stale_measurement_cannot_close_issue(monkeypatch):
    monkeypatch.setattr(
        fg, "multi_source_feedback", lambda now: fg.Measurement(1, "fixture", now - 2 * fg.DAY)
    )
    assert fg.measure(GOAL, NOW).value is None
    assert "stale" in fg.measure(GOAL, NOW).reason


def test_drift_suppresses_nothing_due(monkeypatch):
    import switch_review
    import triage_shadow

    monkeypatch.setattr(triage_shadow, "summary_line", lambda: "")
    rep = {
        "review_days": 7,
        "held_off": [],
        "on_but_idle": [],
        "unconditioned": [],
        "raise_count": 0,
        "mirror_drift": {"status": "ok"},
        "feature_goals": section(1),
    }
    assert "Nothing due" in switch_review.format_report(rep)
    rep["feature_goals"] = section(0, 14)
    assert "Nothing due" not in switch_review.format_report(rep)

    rep["feature_goals"] = {**section(1), "errors": ["history unavailable"]}
    assert "Nothing due" not in switch_review.format_report(rep)


def test_combined_check_and_filer_measures_and_appends_once(tmp_path, monkeypatch):
    real_report = fg.report
    path = tmp_path / "history.jsonl"
    calls = []

    def measure(goal, now):
        calls.append(goal)
        return fg.Measurement(1, "fixture", now)

    monkeypatch.setattr(
        fg, "report", lambda: real_report(NOW, path=path, goals=[GOAL], measure_fn=measure)
    )
    monkeypatch.setattr(fg, "reconcile_issues", lambda report: {"changes": []})
    assert fg.main(["--check", "--file-drift-issues"]) == 0
    assert calls == [GOAL]
    assert len(path.read_text().splitlines()) == 1


def test_filer_ignores_pull_requests_and_creates_missing_labels():
    gh = RecordedGh()
    gh.labels.clear()
    gh.issues.append(
        {
            "number": 700,
            "state": "open",
            "body": "<!-- goal-drift:experiments:G1 -->",
            "pull_request": {},
        }
    )
    fg.reconcile_issues(section(0, 14), gh=gh)
    assert len(gh.issues) == 2
    assert gh.labels == {"goal-drift", "priority:high"}


def test_keepalive_window_uses_configured_horizon(monkeypatch):
    monkeypatch.setattr(fg, "GOAL_SILENCE_DAYS", 19)
    queries = []
    monkeypatch.setattr(fg, "_gh", lambda args: queries.append(args) or json.dumps({"items": []}))
    fg.keepalive_acceptance(NOW)
    start = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW - 19 * fg.DAY))
    assert any(f"merged:>={start} " in arg for arg in queries[0])


def test_producer_subject_string_is_attributed(tmp_path, monkeypatch):
    """Producer experiments store `subject` as `owner/repo#N`; the measure must link them."""
    import exp_abcd
    import feedback

    db = tmp_path / "brain.db"
    monkeypatch.setattr(feedback, "DB_PATH", db)
    monkeypatch.setattr(exp_abcd, "EXP_DIR", tmp_path / "experiments")
    experiment = "tick-1-stranske-workflows-3972"
    directory = exp_abcd.exp_paths(experiment)
    directory.mkdir(parents=True)
    (directory / "meta.json").write_text(
        json.dumps({"producer": "experiment_producer", "subject": "stranske/Workflows#3972"})
    )
    with sqlite3.connect(db) as conn:
        conn.executescript(
            "CREATE TABLE runs(experiment_id TEXT,target TEXT);"
            "CREATE TABLE evaluations_v2(experiment_id TEXT,ts INT,score REAL,evaluator_id TEXT);"
        )
        conn.execute(
            "INSERT INTO runs VALUES(?,?)",
            (experiment, "stranske/Workflows [exp x arm a member b]"),
        )
        for judge in ("codex", "cursor"):
            conn.execute("INSERT INTO evaluations_v2 VALUES(?,?,?,?)", (experiment, NOW, 8, judge))
    body = "\n".join("- [ ] task" for _ in range(12))
    monkeypatch.setattr(fg, "_gh", lambda args: json.dumps({"body": body, "labels": []}))
    result = fg.multi_source_feedback(NOW)
    assert result.value == 1.0
    assert result.reason == "unattributable 0; issue reads failed 0"


def test_random_retest_reads_producer_cells(monkeypatch):
    import experiment_producer

    state = {"cells_fresh": 4, "cells_total": 37, "last_draw_ts": NOW - 3600}
    monkeypatch.setattr(experiment_producer, "read_state", lambda: state)
    result = fg.random_retest(NOW)
    assert result.value == 4 / 37 and result.reason == "4/37 cells fresh"
    state["last_draw_ts"] = NOW - fg.PRODUCER_STATE_MAX_AGE - 1
    stale = fg.random_retest(NOW)
    assert stale.value is None and stale.reason.startswith(
        "producer not ticking: last draw 2.0 days"
    )
    monkeypatch.setattr(experiment_producer, "read_state", lambda: {})
    assert fg.random_retest(NOW).value is None


def test_comparative_routing_reads_the_shadow_report(monkeypatch):
    import comparative

    monkeypatch.setattr(
        comparative,
        "report",
        lambda *a, **k: {"classes_differing": 1, "status": "optimal", "reasons": []},
    )
    result = fg.comparative_routing(NOW)
    assert result.value == 1.0 and "status optimal" in result.reason
    monkeypatch.setattr(
        comparative,
        "report",
        lambda *a, **k: {"classes_differing": 0, "status": "not_testable_slack", "reasons": []},
    )
    result = fg.comparative_routing(NOW)
    assert result.value is None and "slack" in result.reason
    monkeypatch.setattr(
        comparative,
        "report",
        lambda *a, **k: {
            "classes_differing": 2,
            "status": "inputs_unmeasured",
            "reasons": ["thin"],
        },
    )
    assert fg.comparative_routing(NOW).value is None
    monkeypatch.setattr(
        comparative,
        "report",
        lambda *a, **k: {
            "classes_differing": None,
            "status": "inputs_unmeasured",
            "reasons": ["claude stale"],
        },
    )
    result = fg.comparative_routing(NOW)
    assert result.value is None and "claude stale" in result.reason


def _search(items, incomplete=False):
    return json.dumps({"incomplete_results": incomplete, "items": items})


def test_keepalive_acceptance_counts_prs_whose_gates_all_pass(monkeypatch):
    items = [
        {
            "repository_url": "https://api.github.com/repos/stranske/Workflows",
            "body": "x\nDeterministic gates: 2/2 passed\nCriterion 1: PASS",
        },
        {
            "repository_url": "https://api.github.com/repos/stranske/Workflows",
            "body": "Deterministic gates: 1/2 passed",
        },
        {"repository_url": "https://api.github.com/repos/stranske/Ready", "body": "no gates"},
        {
            "repository_url": "https://api.github.com/repos/stranske/Ready",
            "body": "Deterministic gates: 0/0 passed",
        },
    ]
    monkeypatch.setattr(
        fg, "KEEPALIVE_GATE_REPOS", frozenset({"stranske/Workflows", "stranske/Ready"})
    )
    monkeypatch.setattr(fg, "_gh", lambda args: _search(items))
    result = fg.keepalive_acceptance(NOW)
    assert result.value == 0.25
    assert result.reason.startswith("1/4 keepalive PRs merged since ")


def test_keepalive_acceptance_counts_only_repos_with_the_gate(monkeypatch):
    items = [
        {
            "repository_url": "https://api.github.com/repos/stranske/Workflows",
            "body": "Deterministic gates: 1/1 passed",
        },
        {"repository_url": "https://api.github.com/repos/stranske/Ready", "body": "no gates"},
        {"repository_url": "https://api.github.com/repos/stranske/Ready", "body": "no gates"},
    ]
    monkeypatch.setattr(fg, "_gh", lambda args: _search(items))
    result = fg.keepalive_acceptance(NOW)
    assert result.value == 1.0
    assert result.reason.endswith("; 2 merged in repos without the gate")
    monkeypatch.setattr(fg, "_gh", lambda args: _search(items[1:]))
    consumers_only = fg.keepalive_acceptance(NOW)
    assert consumers_only.value is None and "2 in repos without the gate" in consumers_only.reason


def test_keepalive_acceptance_is_unknown_without_a_complete_population(monkeypatch):
    monkeypatch.setattr(fg, "_gh", lambda args: _search([], incomplete=True))
    assert fg.keepalive_acceptance(NOW).value is None
    monkeypatch.setattr(fg, "_gh", lambda args: _search([]))
    empty = fg.keepalive_acceptance(NOW)
    assert (
        empty.value is None and "in gate repos (none; 0 in repos without the gate)" in empty.reason
    )


def test_keepalive_acceptance_window_never_starts_before_the_gate(monkeypatch):
    queries = []

    def search(args):
        queries.append(next(a for a in args if a.startswith("q=")))
        return _search([])

    monkeypatch.setattr(fg, "_gh", search)
    fg.keepalive_acceptance(NOW)
    assert queries == [
        f"q=org:stranske is:pr is:merged merged:>={max(time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(NOW - fg.GOAL_SILENCE_DAYS * fg.DAY)), fg.KEEPALIVE_GATE_LIVE)} label:agents:keepalive"
    ]


def test_pacing_unknown_carries_the_reports_reason(monkeypatch):
    import capacity

    monkeypatch.setattr(
        capacity,
        "pacing_report",
        lambda now: {
            "quota_incidents_while_other_ok": {
                "value": None,
                "evidence_class": "e",
                "as_of": NOW,
                "reason": "coverage 59/1008",
            },
            "usage_in_target_band": {
                "value": 0.5,
                "evidence_class": "e",
                "as_of": NOW,
                "reason": "claude in band",
            },
        },
    )
    unknown = fg.pacing_incidents(NOW)
    assert unknown.value is None and unknown.reason.endswith("coverage 59/1008")
    measured = fg.pacing_usage(NOW)
    assert measured.value == 0.5 and measured.reason == "claude in band"


def test_pacing_measure_survives_the_freshness_check(monkeypatch):
    """measure() rejects as_of > now; the pacing reader must be stamped with the ledger's clock."""
    import capacity

    monkeypatch.setattr(
        capacity,
        "pacing_report",
        lambda now: {
            "usage_in_target_band": {
                "value": 0.0,
                "evidence_class": "e",
                "as_of": int(now),
                "reason": "claude 0.98",
            }
        },
    )
    goal = replace(GOAL, measure="feature_goals.py:pacing_usage")
    result = fg.measure(goal, NOW)
    assert result.value == 0.0 and result.as_of == NOW
