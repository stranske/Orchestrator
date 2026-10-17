"""Goal ledger acceptance gates; all state and GitHub effects are fixture-owned."""

import json
import sqlite3
from dataclasses import asdict, replace
from datetime import datetime, timezone

import pytest

import feature_goals as fg

NOW = 1_800_000_000
GOAL = fg.FEATURES[0]


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
    """Recorded gh issue JSON shape, retaining mutations across daily invocations."""

    def __init__(self):
        self.issues = []
        self.calls = []

    def __call__(self, args):
        self.calls.append(args)
        command = args[:2]
        if command == ["issue", "list"]:
            return json.dumps(self.issues)
        if command == ["issue", "create"]:
            self.issues.append(
                {"number": 701, "state": "OPEN", "body": args[args.index("--body") + 1]}
            )
            return "https://github.com/stranske/Orchestrator/issues/701\n"
        if command == ["issue", "edit"]:
            self.issues[0]["body"] = args[args.index("--body") + 1]
        elif command == ["issue", "close"]:
            self.issues[0]["state"] = "CLOSED"
        elif command == ["issue", "reopen"]:
            self.issues[0]["state"] = "OPEN"
        elif command == ["label", "list"]:
            return '[{"name":"goal-drift"},{"name":"priority:high"}]'
        else:
            raise AssertionError(args)
        return ""


def test_drift_issue_is_idempotent_and_closes_on_recovery():
    gh = RecordedGh()
    bad = section(0.0, 14)
    assert fg.reconcile_issues(bad, gh=gh)["changes"] == ["created"]
    assert "<!-- goal-drift:experiments:G1 -->" in gh.issues[0]["body"]
    assert fg.reconcile_issues(bad, gh=gh)["changes"] == ["updated"]
    assert len(gh.issues) == 1
    assert fg.reconcile_issues(section(None), gh=gh)["changes"] == []
    assert gh.issues[0]["state"] == "OPEN", "unknown cannot close a drift issue"
    assert fg.reconcile_issues(section(1.0), gh=gh)["changes"] == ["closed"]
    assert gh.issues[0]["state"] == "CLOSED"
    fg.reconcile_issues(bad, gh=gh)
    assert len(gh.issues) == 1
    assert gh.issues[0]["state"] == "OPEN"
    assert sum(call[:2] == ["issue", "create"] for call in gh.calls) == 1


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


def test_multi_source_feedback_requires_two_judges_and_sizable_issue(tmp_path, monkeypatch):
    import feedback

    db = tmp_path / "brain.db"
    monkeypatch.setattr(feedback, "DB_PATH", db)
    with sqlite3.connect(db) as conn:
        conn.executescript(
            "CREATE TABLE runs(experiment_id TEXT,target TEXT);"
            "CREATE TABLE evaluations_v2(experiment_id TEXT,ts INT,score REAL,evaluator_id TEXT);"
        )
        conn.executemany(
            "INSERT INTO runs VALUES(?,?)",
            [("a", "stranske/Orchestrator#1"), ("b", "stranske/Orchestrator#2")],
        )
        conn.executemany(
            "INSERT INTO evaluations_v2 VALUES(?,?,?,?)",
            [("a", NOW, 1, "judge1"), ("a", NOW, 2, "judge2"), ("b", NOW, 1, "judge1")],
        )
    monkeypatch.setattr(
        fg, "_gh", lambda args: json.dumps({"body": "- [ ] goal\n" * 9, "labels": []})
    )
    assert fg.measure(GOAL, NOW).value == 1.0
    monkeypatch.setattr(fg, "_gh", lambda args: json.dumps({"body": "- [ ] small", "labels": []}))
    assert fg.measure(GOAL, NOW).value == 0.0


@pytest.mark.parametrize(
    "symbol",
    [
        "comparative_routing",
        "random_retest",
        "pacing_usage",
        "pacing_incidents",
        "pacing_share",
        "keepalive_acceptance",
    ],
)
def test_absent_sources_have_explicit_reasons(symbol):
    goal = replace(GOAL, measure=f"feature_goals.py:{symbol}")
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


def test_daily_cadence_check_files_and_reconciles_recovery():
    import cadence_registry
    import paths

    row = next(r for r in cadence_registry.CADENCE_STEPS if r["key"] == "feature-goals")
    assert row["cadence_days"] == 0
    source = (paths.REPO_ROOT / "orchestrate.sh").read_text()
    assert source.count("if _cadence_due feature-goals") == 1
    block = source.split("if _cadence_due feature-goals", 1)[1].split(
        "if _cadence_due switch-review", 1
    )[0]
    assert "--check" in block and "--file-drift-issues" in block
    assert '"$feature_goals_rc" == "0"' in block
    assert '"$feature_goals_rc" == "3"' in block


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


def test_invalid_history_does_not_crash_or_fabricate_zero(tmp_path):
    path = tmp_path / "history.jsonl"
    path.write_text('{"feature":"experiments","goal_id":"G1","as_of":0,"measurement":null}\n')
    rep = fg.report(NOW, path=path, goals=[GOAL], measure_fn=lambda g, n: fg.unknown(n, "query"))
    assert rep["errors"]
    assert rep["goals"][0]["state"] == "unmeasured"
    assert rep["drifting"] == 0


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
    assert calls[0][:2] == ["issue", "list"]


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
