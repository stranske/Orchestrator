"""Fleet work shapes: merged fleet PRs grouped by what they touched, measured per agent, and consumed
by the advisor's repeated_pattern precondition and the periodic report. Bots, the owner and
unattributed rows never become a shape; a PR gh cannot return is named as missing, never mined."""

from __future__ import annotations

import inspect
import json
import time

import pytest

import capability_advisor
import feedback
import fleet_shapes
import relearn_report


@pytest.fixture
def brain(monkeypatch, tmp_path):
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path))
    fleet_shapes._LOOKUP_CACHE.clear()
    return tmp_path


def _merged(rid, repo, number, agent, *, days_ago=10, durability="durable", source=None, usd=None):
    ts = int(time.time()) - days_ago * 86400
    feedback.record_run(
        rid,
        f"{repo}#{number}",
        "implement",
        agent,
        mode="remote",
        ts=ts,
        source="keepalive",
        routing_metadata={"attribution_source": source} if source else None,
    )
    feedback.record_outcome(
        rid, adjudicated_verdict="PASS", verifier_verdict="PASS", merged=True, durability=durability
    )
    if usd is not None:
        feedback.record_cost(rid, tokens_in=1000, tokens_out=100, cost_usd=usd)
    return ts


def test_shapes_apply_the_detection_floor(brain):
    now = feedback.DURABILITY_DETECTION_SINCE + 30 * 86400
    numbers = {"before": 1, "atfloor": 2, "young": 3, "atage": 4, "fallback": 5, "pending": 6}

    def add(rid, *, age_seconds, durability, checked_ts):
        ts = now - age_seconds
        feedback.record_run(
            rid, f"o/r0#{numbers[rid]}", "implement", "codex", ts=ts, source="keepalive"
        )
        feedback.record_outcome(rid, merged=True, durability=durability)
        with feedback._conn() as c:
            c.execute(
                "UPDATE outcomes SET durability_checked_ts=? WHERE run_id=?", (checked_ts, rid)
            )

    mature = 10 * 86400
    add(
        "before",
        age_seconds=mature,
        durability="durable",
        checked_ts=feedback.DURABILITY_DETECTION_SINCE - 1,
    )
    add(
        "atfloor",
        age_seconds=mature,
        durability="durable",
        checked_ts=feedback.DURABILITY_DETECTION_SINCE,
    )
    add("young", age_seconds=7 * 86400 - 1, durability="broke_later", checked_ts=now)
    add("atage", age_seconds=7 * 86400, durability="broke_later", checked_ts=now)
    add("fallback", age_seconds=mature, durability="durable", checked_ts=None)
    add("pending", age_seconds=mature, durability="pending", checked_ts=None)

    prs = fleet_shapes.merged_agent_prs(now=now)
    by_run = {pr["ref"].split("#", 1)[1]: pr for pr in prs}
    assert len(prs) == 6
    assert by_run["1"]["durability"] == "pending"
    assert by_run["2"]["durability"] == "durable"
    assert by_run["3"]["durability"] == "pending"
    assert by_run["4"]["durability"] == "broke_later"
    assert by_run["5"]["durability"] == "durable"
    assert by_run["6"]["durability"] == "pending"

    facts = {pr["ref"]: dict(FACT) for pr in prs}
    shape = fleet_shapes.aggregate(prs, facts, now=now, window_days=60)["shapes"][0]
    cell = shape["agents"]["codex"]
    assert shape["prs"] == cell["n"] == 6
    assert (cell["durable"], cell["bad"], cell["pending"]) == (2, 1, 3)
    assert cell["broke_later_rate"] == 0.333


def test_report_window_matches_quality_learner_default(monkeypatch, capsys):
    selection_default = inspect.signature(feedback.relearn).parameters["window_days"].default
    quality_default = inspect.signature(feedback.relearn_quality).parameters["window_days"].default
    report_default = (
        inspect.signature(relearn_report.build_report).parameters["window_days"].default
    )
    assert selection_default == quality_default == report_default == feedback.RELEARN_WINDOW_DAYS

    observed = []

    def report(*, window_days, dry_run):
        observed.append((window_days, dry_run))
        return {"window_days": window_days}

    monkeypatch.setattr(relearn_report, "build_report", report)
    assert relearn_report.main(["--dry-run", "--json"]) == 0
    assert relearn_report.main(["--dry-run", "--json", "--window-days", "37"]) == 0
    capsys.readouterr()
    assert observed == [(feedback.RELEARN_WINDOW_DAYS, True), (37, True)]


FACT = {
    "title": "chore(deps): bump ruff",
    "labels": ["dependencies", "agent:codex", "status:ready"],
    "paths": ["pyproject.toml", "requirements.txt"],
    "commits": 2,
    "created_ts": 1_000_000,
    "merged_ts": 1_000_000 + 3 * 3600,
}


def _fetch_recorder(calls):
    def fetch(repo, numbers):
        calls.append((repo, tuple(numbers)))
        return {n: dict(FACT) for n in numbers if n != 404}  # 404: a PR gh cannot return

    return fetch


def test_signature_drops_queue_labels_and_classes_paths():
    sig = fleet_shapes.shape_signature(
        "Fix: guard the null path",
        ["agent:codex", "bug", "status:ready", "priority:high", "Area:API"],
        ["src/a.py", "src/b.py", "tests/test_a.py", "README.md", ".github/workflows/ci.yml"],
    )
    assert sig["commit_type"] == "fix"
    assert sig["labels"] == ["area:api", "bug"]
    assert sig["path_classes"] == ["code", "docs", "tests"]  # the three most-touched, sorted
    assert sig["key"] == "fix|area:api,bug|code+docs+tests"
    assert fleet_shapes.shape_signature("no conventional prefix", [], [])["key"] == "other||"


def test_run_groups_agent_prs_flags_recurrence_and_excludes_bots(brain):
    _merged("c1", "o/r0", 1, "codex")
    _merged("c2", "o/r0", 2, "codex", durability="broke_later")
    _merged("k1", "o/r1", 3, "claude")
    _merged("k2", "o/r1", 4, "claude", usd=0.2)
    _merged("d1", "o/r1", 404, "codex")  # deleted upstream: facts never arrive
    _merged("b1", "o/r0", 5, "none", source="bot:renovate")
    _merged("h1", "o/r1", 6, "none", source="human")
    _merged("u1", "o/r0", 7, "none")  # unattributed agent work
    _merged("old", "o/r0", 8, "codex", days_ago=90)  # outside the window
    calls: list = []
    payload = fleet_shapes.run(window_days=60, state_dir=brain, fetch_fn=_fetch_recorder(calls))
    counts = payload["counts"]
    assert counts == {
        "prs": 5,
        "with_facts": 4,
        "missing_facts": 1,
        "shapes": 1,
        "recurring": 1,
        "fetched_this_run": 4,
        "unavailable_this_run": 1,
    }
    assert payload["missing_facts"] == ["o/r1#404"]
    shape = payload["shapes"][0]
    assert shape["key"] == "chore|dependencies|config"
    assert shape["recurring"] and shape["recurring_because"] == "4 PRs across 2 repos"
    assert shape["repos"] == ["o/r0", "o/r1"]
    assert (
        shape["agents"]["codex"]["n"] == 2 and shape["agents"]["codex"]["broke_later_rate"] == 0.5
    )
    assert shape["agents"]["claude"]["broke_later_rate"] == 0.0
    assert shape["agents"]["claude"]["cost_usd_median"] == 0.2  # only one claude PR carried a cost
    assert shape["agents"]["codex"]["hours_to_merge_median"] == 3.0
    assert shape["agents"]["codex"]["commits_median"] == 2.0
    on_disk = json.loads((brain / "fleet-shapes.json").read_text())
    assert on_disk["counts"]["recurring"] == 1
    assert "chore|dependencies|config" in (brain / "fleet-shapes.md").read_text()
    facts = fleet_shapes.load_facts(brain)
    assert facts["o/r1#404"]["unavailable"] is True and len(facts) == 5


def test_second_run_reuses_the_cache_and_the_fetch_is_bounded(brain):
    for i in range(1, 4):
        _merged(f"c{i}", "o/r0", i, "codex")
    _merged("d1", "o/r0", 404, "codex")
    calls: list = []
    first = fleet_shapes.run(
        window_days=60, state_dir=brain, fetch_limit=2, fetch_fn=_fetch_recorder(calls)
    )
    assert first["counts"]["fetched_this_run"] == 2 and first["counts"]["missing_facts"] == 2
    assert sum(len(numbers) for _, numbers in calls) == 2, "the fetch budget bounds the gh reads"
    calls.clear()
    second = fleet_shapes.run(window_days=60, state_dir=brain, fetch_fn=_fetch_recorder(calls))
    asked = sorted(n for _, numbers in calls for n in numbers)
    assert asked == [3, 404], "only PRs without cached facts are fetched"
    assert second["counts"]["with_facts"] == 3 and second["counts"]["missing_facts"] == 1
    calls.clear()
    third = fleet_shapes.run(window_days=60, state_dir=brain, fetch_fn=_fetch_recorder(calls))
    assert calls == [], "a PR recorded as unavailable is not asked for again"
    assert third["counts"]["missing_facts"] == 1


def test_advisor_repeated_pattern_answers_from_fleet_shapes(brain):
    for i, repo in enumerate(("o/r0", "o/r1", "o/r2"), start=1):
        _merged(f"c{i}", repo, i, "codex")
    fleet_shapes.run(window_days=60, state_dir=brain, fetch_fn=_fetch_recorder([]))
    same_shape = {
        "title": "chore: bump black",
        "labels": ["dependencies", "agent:claude"],
        "paths": ["pyproject.toml"],
        "changedFiles": 1,
    }
    met, evidence = capability_advisor._probe_repeated_pattern(same_shape)
    assert met is True and "recurs across the fleet" in evidence and "3 PRs" in evidence
    other_shape = {
        "title": "feat: add the export button",
        "labels": ["enhancement"],
        "paths": ["src/ui/export.tsx"],
        "changedFiles": 1,
    }
    met, evidence = capability_advisor._probe_repeated_pattern(other_shape)
    assert met is False and "no recurring fleet shape" in evidence
    met, evidence = capability_advisor._probe_repeated_pattern(
        {"title": "codemod: rename the helper", "labels": [], "changedFiles": 40}
    )
    assert met is True and "codemod" in evidence, "the title keyword still counts without paths"


def test_report_names_the_unmined_state_and_then_the_shapes(brain):
    summary = fleet_shapes.summary_for_report(brain)
    assert summary["state"] == "not yet mined"
    line = fleet_shapes.render_report_lines(summary)[0]
    assert line.startswith("FLEET-SHAPES: not yet mined")
    for i, repo in enumerate(("o/r0", "o/r1", "o/r2"), start=1):
        _merged(f"c{i}", repo, i, "codex")
    fleet_shapes.run(window_days=60, state_dir=brain, fetch_fn=_fetch_recorder([]))
    lines = fleet_shapes.render_report_lines(fleet_shapes.summary_for_report(brain))
    assert "3 merged agent PRs" in lines[0] and "1 recurring" in lines[0]
    assert lines[1].strip().startswith("chore|dependencies|config: 3 PRs / 3 repos")
