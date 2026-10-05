import copy
import json
import sqlite3

import issue_size_quality as quality
import switch_review

NOW = 1800000000


def issue(number, tasks, pr=8, merged="2026-01-01T00:00:00Z"):
    return {
        "number": number,
        "body": "\n".join("- [x] scoped task" for _ in range(tasks)),
        "closedAt": "2026-01-02T00:00:00Z",
        "repository": {"nameWithOwner": "o/r"},
        "closedByPullRequestsReferences": {
            "pageInfo": {"hasNextPage": False},
            "nodes": [
                {
                    "number": pr,
                    "state": "MERGED",
                    "mergedAt": merged,
                    "repository": {"nameWithOwner": "o/r"},
                }
            ],
        },
    }


def test_bands_join_to_outcomes_and_report_counts_with_zero_as_zero():
    rows = {
        "o/r#8": [
            {"verifier_verdict": "PASS", "durability": "durable", "durability_checked_ts": NOW}
        ]
    }
    facts = {"o/r#8": {"merged_ts": NOW - 30 * 86400}}
    rep = quality.curve([issue(1, 2), issue(1, 2), issue(2, 16, pr=9)], rows, facts, now=NOW)
    cells = {r["band"]: r for r in rep["bands"]}
    assert rep["issue_count"] == 2
    assert cells["1-4"]["joined"] == 1
    assert cells["1-4"]["pass"] == {"n": 1, "yes": 1, "rate": 1}
    assert cells["1-4"]["broke_later"] == {"n": 1, "yes": 0, "rate": 0}
    assert cells["16+"]["pass"]["rate"] is None
    text = "\n".join(quality.format_lines(rep))
    assert "broke_later=0.0% (n=1)" in text
    assert "pass=unmeasured (n=0)" in text
    assert quality.size_band(0) == "0" and quality.size_band(8) == "5-8"


def test_late_closing_reference_and_young_durability_are_not_quality_evidence():
    rows = {
        "o/r#8": [
            {"verifier_verdict": "PASS", "durability": "durable", "durability_checked_ts": NOW}
        ]
    }
    rep = quality.curve([issue(1, 2, merged="2026-01-10T00:00:00Z")], rows, {}, now=NOW)
    assert rep["missing_outcomes"] == ["o/r#1"]
    rep = quality.curve([issue(1, 2)], rows, {"o/r#8": {"merged_ts": NOW}}, now=NOW)
    assert rep["bands"][1]["durable"]["rate"] is None


def test_adjudicated_pass_does_not_erase_the_original_verifier_concern():
    rows = {"o/r#8": [{"adjudicated_verdict": "PASS", "verifier_verdict": "CONCERNS"}]}
    rep = quality.curve([issue(1, 2)], rows, {}, now=NOW)
    assert rep["bands"][1]["pass"]["rate"] == 1
    assert rep["bands"][1]["verifier_non_pass"]["rate"] == 1


def test_partial_search_and_closing_pages_remain_unknown():
    node = issue(1, 2)
    node["closedByPullRequestsReferences"]["pageInfo"]["hasNextPage"] = True
    page = {
        "data": {
            "search": {"issueCount": 1001, "nodes": [node], "pageInfo": {"hasNextPage": False}}
        }
    }
    nodes, errors = quality.collect_issues(
        lambda args: (True, json.dumps(page), ""), ["o/r"], now=NOW
    )
    assert nodes == [node]
    assert len(errors) == 2
    rep = quality.curve(nodes, {}, {}, now=NOW, errors=errors)
    assert rep["status"] == "partial"


def test_two_weeks_and_complete_metric_samples_guard_the_decision():
    rep = quality.curve([], {}, {}, now=NOW)
    for band in rep["bands"]:
        for metric in quality.METRICS:
            band[metric] = {"n": 20, "yes": 0, "rate": 0}
    older = {**copy.deepcopy(rep), "generated_at": NOW - 14 * 86400}
    assert quality.decision([rep], now=NOW) == "collect_two_weeks"
    assert quality.decision([older, rep], now=NOW) == "retire_issue_level_claim"
    rep["bands"][-1]["broke_later"]["rate"] = 0.1
    assert quality.decision([older, rep], now=NOW) == "file_opener_bucket_wiring_issue"
    rep["bands"][-1]["pass"]["rate"] = None
    assert quality.decision([older, rep], now=NOW) == "unmeasured_quality"
    assert quality.decision([{**older, "status": "partial"}, rep], now=NOW) == "collect_two_weeks"


def test_brain_join_is_read_only_and_excludes_unattributed_outcomes(tmp_path):
    db = tmp_path / "brain.db"
    with sqlite3.connect(db) as conn:
        conn.executescript(
            "CREATE TABLE runs(run_id,target,routing_metadata,source,agent); CREATE TABLE outcomes(run_id,merged,verifier_verdict,durability,failure_class);"
        )
        for i, failure in enumerate([None, "unattributed_delegation"]):
            conn.execute(
                "INSERT INTO runs VALUES(?,?,?,?,?)", (str(i), "o/r#8", "{}", "keepalive", "codex")
            )
            conn.execute(
                "INSERT INTO outcomes VALUES(?,?,?,?,?)", (str(i), 1, "PASS", "durable", failure)
            )
    before = db.read_bytes()
    assert len(quality.brain_outcomes(db)["o/r#8"]) == 1
    assert db.read_bytes() == before


def test_weekly_caller_collects_and_persists_the_curve(tmp_path, monkeypatch, capsys):
    import capability_recurrence_check
    import fleet_shapes
    import value_chain_monitor

    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("ORCH_VALUE_CHAIN_MONITOR", "0")
    monkeypatch.setattr(
        switch_review, "review", lambda **kw: {"held_off": [], "on_but_idle": [], "raise_count": 0}
    )
    monkeypatch.setattr(
        capability_recurrence_check, "as_the_tick_sees_it", lambda flag: (None, "unset")
    )
    monkeypatch.setattr(value_chain_monitor, "collect_inputs", lambda **kw: {})
    node = issue(1, 2)
    page = {
        "data": {"search": {"issueCount": 1, "nodes": [node], "pageInfo": {"hasNextPage": False}}}
    }
    monkeypatch.setattr(switch_review, "_gh_call", lambda args: (True, json.dumps(page), ""))
    monkeypatch.setattr(quality, "brain_outcomes", lambda db: {})
    monkeypatch.setattr(quality, "fleet_repos", lambda: ["o/r"])
    monkeypatch.setattr(fleet_shapes, "load_facts", lambda state: {})
    assert switch_review.main(["--json", "--env", "process"]) == 0
    assert json.loads(capsys.readouterr().out)["issue_size_quality"]["issue_count"] == 1
    stored = json.loads((tmp_path / "capability-program/size-quality.json").read_text())
    assert stored["decision"] == "collect_two_weeks"
    assert len(stored["observations"]) == 1
