"""Recorded gh reader contracts plus adverse controls for issue #672.

gh.json was recorded on 2026-10-17; its provenance includes the exact commands.
Only fields consumed by the readers are retained. Modified copies below are explicit
controls, not additional live evidence. No test calls GitHub or the installed cap tool.
"""

import base64
import copy
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

import switch_review as m

FIXTURES = Path(__file__).parent / "fixtures" / "switch_review_fleet"


@pytest.fixture
def recording():
    return json.loads((FIXTURES / "gh.json").read_text())


@pytest.fixture
def cap_json():
    return json.loads((FIXTURES / "cap.json").read_text())


def replay(recording):
    records = {tuple(row["args"]): row["stdout"] for row in recording["gh"]}

    def gh(args):
        assert tuple(args) in records, f"unrecorded query: {args}"
        return True, records[tuple(args)], ""

    return gh


def cap_reader(payload):
    def run(command, **kwargs):
        assert command[:1] == [sys.executable]
        assert command[1].endswith("/.codex/bin/opener-cap-health.py")
        assert command[2] == "--json"
        assert m._FLEET_EXCLUDED_REPO not in command
        assert kwargs["timeout"] == m.FLEET_CAP_TIMEOUT_S
        return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")

    return run


def measure(recording, cap_json, gh=None, cap=None):
    return m.fleet_delivery_measures(
        now=recording["recorded_at"],
        repos=[recording["repo"]],
        gh_fn=gh or replay(recording),
        cap_fn=cap or cap_reader(cap_json),
    )


def test_recorded_readers(recording, cap_json, monkeypatch):
    monkeypatch.setenv("OPENER_CAP", "8")
    rows = measure(recording, cap_json)
    assert len(rows) == 5
    assert all(row["evidence_class"] != "unmeasured" for row in rows), rows
    assert rows[0]["value"].startswith("273/495 (55.2%) completed; 1 still running over 48h")
    assert "labeled=unmeasured; synchronize=unmeasured" in rows[0]["value"]
    assert "github.event.action" in rows[0]["value"]
    assert "min" in rows[1]["value"]
    assert "Portable-Alpha-Extension-Model#2379" in rows[2]["value"]
    assert "3 open across 3 canaries" in rows[2]["value"]
    assert "2 opener-owned / cap 8; human-held exempt=0; coverage exempt=0" in rows[3]["value"]
    assert "opened=" in rows[4]["value"] and "merged=" in rows[4]["value"]
    rendered = m.format_report(
        {
            "review_days": 7,
            "raise_count": 0,
            "unconditioned": [],
            "held_off": [],
            "on_but_idle": [],
            "fleet_delivery_measures": rows,
        }
    )
    assert "## Fleet delivery measures" in rendered
    for row in rows:
        assert f"{row['name']}: {row['value']} [{row['evidence_class']}]" in rendered


def test_failed_run_read_is_unmeasured(recording, cap_json):
    recorded = replay(recording)

    def gh(args):
        if "actions/workflows/" in args[1]:
            return False, "", "HTTP 403 rate limit"
        return recorded(args)

    rows = measure(recording, cap_json, gh=gh)
    assert rows[0]["value"] == "unmeasured — gh HTTP 403 rate limit"
    assert rows[0]["evidence_class"] == "unmeasured"
    assert all(row["evidence_class"] != "unmeasured" for row in rows[1:])


@pytest.mark.parametrize("reader,indices", [("graphql", [1, 4]), ("canaries", [2])])
def test_independent_read_failures(recording, cap_json, reader, indices):
    recorded = replay(recording)

    def gh(args):
        if (reader == "graphql" and args[1] == "graphql") or (
            reader == "canaries" and "consumer_sync_canaries.json" in args[1]
        ):
            return False, "", "HTTP 502 fixture"
        return recorded(args)

    rows = measure(recording, cap_json, gh=gh)
    assert [i for i, row in enumerate(rows) if row["evidence_class"] == "unmeasured"] == indices
    assert all("gh HTTP 502 fixture" in rows[i]["value"] for i in indices)


def test_all_failures_still_print_five_lines(recording, cap_json):
    def unavailable(*args, **kwargs):
        raise FileNotFoundError("fixture executable absent")

    rows = measure(recording, cap_json, gh=unavailable, cap=unavailable)
    text = m.format_report(
        {
            "review_days": 7,
            "raise_count": 0,
            "unconditioned": [],
            "held_off": [],
            "on_but_idle": [],
            "fleet_delivery_measures": rows,
        }
    )
    assert len(rows) == 5
    assert text.count("[unmeasured]") == 5
    assert all(row["value"].startswith("unmeasured — ") for row in rows)


@pytest.mark.parametrize("payload", ["not JSON", "null", "{}"])
def test_malformed_read_is_not_zero(recording, cap_json, payload):
    recorded = replay(recording)

    def gh(args):
        return (True, payload, "") if "actions/workflows/" in args[1] else recorded(args)

    assert measure(recording, cap_json, gh=gh)[0]["evidence_class"] == "unmeasured"


def test_partial_run_population_is_unmeasured(recording, cap_json):
    recorded = replay(recording)

    def gh(args):
        if "actions/workflows/" in args[1]:
            return True, json.dumps({"total_count": 1, "workflow_runs": []}), ""
        return recorded(args)

    assert "incomplete population" in measure(recording, cap_json, gh=gh)[0]["value"]


def test_second_canary_repo_failure_does_not_report_partial_oldest(recording, cap_json):
    recorded = replay(recording)

    def gh(args):
        if "repos/stranske/trip-planner/pulls" in args[1]:
            return False, "", "canary unavailable"
        return recorded(args)

    row = measure(recording, cap_json, gh=gh)[2]
    assert row["evidence_class"] == "unmeasured"
    assert "canary unavailable" in row["value"]


def test_empty_success_is_explicit_not_a_failure(recording, cap_json):
    recorded = replay(recording)

    def gh(args):
        if "actions/workflows/" in args[1]:
            return True, '{"workflow_runs":[],"total_count":0}', ""
        if "/pulls?" in args[1]:
            return True, "[]", ""
        return recorded(args)

    rows = measure(recording, cap_json, gh=gh)
    assert "no completed runs (n=0); 0 still running" in rows[0]["value"]
    assert rows[2]["value"] == "none open across 3 canaries (n=0)"


def test_cap_exemptions_and_failures(recording, cap_json, monkeypatch):
    monkeypatch.setenv("OPENER_CAP", "4")
    changed = {**cap_json, "human_held_exempt_count": 3, "coverage_exempt_count": 2}
    value = measure(recording, changed)[3]["value"]
    assert "2 opener-owned / cap 4; human-held exempt=3; coverage exempt=2" in value
    changed["human_held_exempt_error"] = "blocker read failed"
    assert (
        "unmeasured — opener-cap-health.py blocker read failed"
        == measure(recording, changed)[3]["value"]
    )


@pytest.mark.parametrize("kind", ["exit", "timeout", "missing", "negative", "bool"])
def test_cap_errors_are_unmeasured(recording, cap_json, kind):
    def cap(command, **kwargs):
        payload = dict(cap_json)
        if kind == "timeout":
            raise subprocess.TimeoutExpired(command, 30)
        if kind == "exit":
            return subprocess.CompletedProcess(command, 2, "", "gh unavailable")
        if kind == "missing":
            payload.pop("total_opener_owned")
        else:
            payload["total_opener_owned"] = -1 if kind == "negative" else True
        return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")

    assert measure(recording, cap_json, cap=cap)[3]["evidence_class"] == "unmeasured"


def iso(stamp):
    return datetime.fromtimestamp(stamp, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connection(nodes):
    return {"nodes": nodes, "pageInfo": {"hasNextPage": False}}


def sample_pr(now, *, created=None, merged=None, closed=None, approved=True):
    return {
        "number": 7,
        "repository": {"nameWithOwner": "o/r"},
        "body": "Closes #3",
        "headRefName": "codex/issue-3",
        "labels": connection([]),
        "createdAt": iso(now - 3600 if created is None else created),
        "mergedAt": iso(merged) if merged is not None else None,
        "closingIssuesReferences": connection(
            [
                {
                    "number": 3,
                    "repository": {"nameWithOwner": "o/r"},
                    "closedAt": iso(closed) if closed is not None else None,
                    "labels": connection([{"name": "repo-review-approved"}] if approved else []),
                }
            ]
        ),
    }


def test_latency_population_and_true_median():
    now = 1800000000
    prs = [
        sample_pr(now, merged=now - 3600, closed=now - 3000),
        sample_pr(now, merged=now - 3600, closed=now - 1800),
        sample_pr(now, merged=now - 3600),
        sample_pr(now, merged=now - 3600, closed=now - 3700),
        sample_pr(now, merged=now - 180000, closed=now - 179900),
    ]
    value = m._fleet_close_latency(now=now, prs=prs, gh=None)
    assert value == "20.00 min (48h merges; n=2, still open=1, closed before merge=1)"


def test_reopened_issue_missing_from_graphql_is_read_explicitly():
    now = 1800000000
    pr = sample_pr(now, merged=now - 3600)
    pr["closingIssuesReferences"] = connection([])
    calls = []

    def gh(args):
        calls.append(args)
        return True, '{"number":3,"closedAt":null}', ""

    assert "still open=1" in m._fleet_close_latency(now=now, prs=[pr], gh=gh)
    assert calls == [["issue", "view", "3", "--repo", "o/r", "--json", "number,closedAt"]]
    assert "still open=0" in m._fleet_close_latency(now=now, prs=[], gh=None)


def test_flow_uses_issue_label_counts_each_pr_once_and_includes_old_openings():
    now = 1800000000
    old = sample_pr(now, created=now - 200000, merged=now - 60, closed=now)
    new = sample_pr(now)
    new["closingIssuesReferences"]["nodes"] *= 2
    human = sample_pr(now, approved=False)
    value = m._fleet_released_flow(now=now, prs=[old, new, human])
    assert "48h mean opened=0.5/day merged=0.5/day" in value
    assert "current issue labels" in value
    assert "opened=0 merged=0" in value


@pytest.mark.parametrize("problem", ["limit", "stalled", "labels", "references", "partial"])
def test_search_cannot_silently_truncate(problem):
    pr = sample_pr(1800000000)
    conn = {**connection([pr]), "issueCount": 1}
    if problem == "limit":
        conn["issueCount"] = 1001
    elif problem == "stalled":
        conn["pageInfo"] = {"hasNextPage": True, "endCursor": None}
    elif problem == "labels":
        pr["labels"]["pageInfo"]["hasNextPage"] = True
    elif problem == "references":
        pr["closingIssuesReferences"]["pageInfo"]["hasNextPage"] = True
    else:
        conn["issueCount"] = 2

    with pytest.raises(ValueError):
        m._fleet_agent_prs(
            now=1800000000,
            repos=["o/r"],
            gh=lambda args: (True, json.dumps({"data": {"search": conn}}), ""),
        )


def test_agent_cohort_excludes_human_and_sync_prs():
    now = 1800000000
    agent = sample_pr(now)
    labeled = copy.deepcopy(agent)
    labeled["headRefName"] = "arbitrary"
    labeled["labels"] = connection([{"name": "agent:muse"}])
    human = copy.deepcopy(agent)
    human["headRefName"] = "feat/manual"
    human["labels"] = connection([{"name": "agent:needs-attention"}])
    sync = copy.deepcopy(human)
    sync["headRefName"] = "sync/workflows-candidate"
    conn = {**connection([agent, labeled, human, sync]), "issueCount": 4}
    result = m._fleet_agent_prs(
        now=now, repos=["o/r"], gh=lambda args: (True, json.dumps({"data": {"search": conn}}), "")
    )
    assert result == [agent, labeled]


def test_exclusion_applies_before_queries(recording, cap_json):
    calls = []

    def gh(args):
        calls.append(args)
        return False, "", "fixture read unavailable"

    m.fleet_delivery_measures(
        now=recording["recorded_at"],
        gh_fn=gh,
        cap_fn=cap_reader(cap_json),
        repos=[recording["repo"], m._FLEET_EXCLUDED_REPO],
    )
    assert calls
    assert all(m._FLEET_EXCLUDED_REPO not in " ".join(args) for args in calls)


def test_cli_collects_and_writes_section(recording, cap_json, monkeypatch, tmp_path, capsys):
    rows = measure(recording, cap_json)
    monkeypatch.setattr(m, "fleet_delivery_measures", lambda: rows)
    monkeypatch.setattr(
        m,
        "review",
        lambda **kwargs: {
            "review_days": 7,
            "raise_count": 0,
            "unconditioned": [],
            "held_off": [],
            "on_but_idle": [],
        },
    )
    monkeypatch.setattr(m, "credential_exposure", lambda: {})
    monkeypatch.setenv("ORCH_VALUE_CHAIN_MONITOR", "0")
    monkeypatch.setenv("ORCH_DISABLE_STEPS", "issue-size-quality")
    import adversarial

    monkeypatch.setattr(adversarial, "record_shape_measurement", lambda *args, **kwargs: False)
    dest = tmp_path / "report.md"
    assert m.main(["--env", "process", "--text-out", str(dest)]) == 0
    assert "## Fleet delivery measures" in capsys.readouterr().out
    assert all(row["name"] in dest.read_text() for row in rows)


@pytest.mark.parametrize(
    "conclusions,expected",
    [
        (["cancelled", "success", None], "1/2 (50.0%) completed; 1 still running"),
        ([None, None], "no completed runs (n=0); 2 still running"),
        ([], "no completed runs (n=0); 0 still running"),
    ],
)
def test_gate_cancellation_share_uses_only_completed_outcomes(conclusions, expected):
    now = 1800000000
    runs = [
        {"conclusion": conclusion, "event": "pull_request", "created_at": "2027-01-15T08:00:00Z"}
        for conclusion in conclusions
    ]
    assert m._fleet_gate_cancellations(
        now=now,
        gh=lambda _args: (True, json.dumps({"total_count": len(runs), "workflow_runs": runs}), ""),
    ).startswith(expected)


def test_fetched_registry_filters_excluded_repository(monkeypatch):
    import consumer_sync_artifact_ingest

    monkeypatch.setattr(consumer_sync_artifact_ingest, "TEST_REGISTRY", None)
    registry = "REGISTERED_CONSUMER_REPOS:\n  stranske/Template\n  " + m._FLEET_EXCLUDED_REPO + "\n"
    payload = json.dumps({"content": base64.b64encode(registry.encode()).decode()})
    calls = []

    def gh(args):
        calls.append(args)
        return True, payload, ""

    repos, reason = m._registered_consumer_repos(gh_fn=gh)
    assert reason == ""
    assert repos == ["stranske/Template"]
    assert len(calls) == 1
    assert calls[0][0] == "api"
    assert calls[0][1].endswith("/maint-68-sync-consumer-repos.yml")
