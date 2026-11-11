"""CI wall-time section of the weekly sweep: runs over ten minutes, attributed by phase."""

from __future__ import annotations

import json

import switch_review as review

NOW = 1_794_430_000


def _ts(offset_s: int) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(NOW + offset_s, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _job(name, created, started, done, steps):
    return {
        "name": name,
        "created_at": _ts(created),
        "started_at": _ts(started) if started is not None else None,
        "completed_at": _ts(done) if done is not None else None,
        "steps": [{"name": n, "started_at": _ts(a), "completed_at": _ts(b)} for n, a, b in steps],
    }


def test_the_slowest_job_is_split_into_queue_tests_upload_and_other():
    phases = review.ci_job_phases(
        [
            _job("fast", -600, -590, -500, [("Run the instrumented shard", -590, -520)]),
            _job(
                "pytest + coverage shard 1/6",
                -3000,
                -1800,
                -1000,
                [
                    ("Run the instrumented shard", -1790, -1720),
                    ("Upload printed summary, gate verdicts and worker coverage", -1720, -1090),
                ],
            ),
            _job("still queued", -100, None, None, []),
        ]
    )
    assert phases["job"] == "pytest + coverage shard 1/6"
    assert phases["queue"] == 1200 and phases["tests"] == 70 and phases["upload"] == 630
    assert phases["dominant"] == "queue"


def test_runs_over_ten_minutes_are_counted_and_unreadable_repos_are_unmeasured():
    runs = {
        "workflow_runs": [
            {
                "id": 1,
                "name": "CI",
                "run_attempt": 1,
                "run_started_at": _ts(-3000),
                "created_at": _ts(-3000),
                "updated_at": _ts(-1000),
            },
            {
                "id": 2,
                "name": "CI",
                "run_attempt": 1,
                "run_started_at": _ts(-700),
                "created_at": _ts(-700),
                "updated_at": _ts(-500),
            },
        ]
    }
    jobs = {"jobs": [_job("shard", -3000, -2900, -1000, [("Upload x", -2800, -1100)])]}

    def gh(args, **_kw):
        path = args[1]
        if path.startswith("repos/o/broken"):
            return False, "", "unmeasured: HTTP 502"
        if path.endswith("/actions/workflows?per_page=100"):
            return (
                True,
                json.dumps({"workflows": [{"id": 9, "name": "CI"}, {"id": 8, "name": "Lint"}]}),
                "",
            )
        if "/workflows/9/runs" in path:
            return True, json.dumps(runs), ""
        if "/attempts/1/jobs" in path:
            return True, json.dumps(jobs), ""
        raise AssertionError(path)

    data = review.ci_walltime(now=NOW, gh_fn=gh, repos=["o/good", "o/broken"])
    assert data["runs"] == 2 and data["over_limit"] == 1
    assert (
        data["slowest"][0]["run_id"] == 1 and data["slowest"][0]["phases"]["dominant"] == "upload"
    )
    assert "o/broken" in data["unreadable"]
    text = "\n".join(review.render_ci_walltime(data))
    assert "1 of 2 completed runs over 10 min" in text and "unmeasured repos: o/broken" in text
    assert "mostly upload" in text


def test_a_drained_window_prints_zero_runs_over_the_limit():
    data = {"runs": 40, "over_limit": 0, "limit_minutes": 10, "slowest": [], "unreadable": {}}
    assert "0 of 40 completed runs over 10 min" in "\n".join(review.render_ci_walltime(data))
