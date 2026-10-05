"""A row GitHub never answers about is asked again for RETRY_HORIZON_DAYS, then closed; never forever.

THE LATCH (found 2026-10-04, latent: no row sat in it on that day's dry run). Two paths left a merged
row pending under DRAIN_RETRY, "the next run asks again", with no horizon:
1. `find_merge` answers `unanswered` whenever `gh pr view` fails with anything but GraphQL's "Could
   not resolve to a PullRequest". A renamed or deleted repository fails that way on every run (gh
   prints "Could not resolve to a Repository", measured), and so does lost access;
2. `classify_durability`'s revert check returns `_revert_drain(note)`, DRAIN_RETRY whenever the
   revert search or the base-commit scan went unanswered.
A pending row scores as a provisional PASS (`feedback._is_success('pending', 'PASS')`), so such a row
trained the router as a win forever, while every run counted it "drainable (retry)": an
over-reporting drainable count, the shape of latched-gate instance #10.

THE RULE. ONE horizon, RETRY_HORIZON_DAYS, on ONE clock per row: the one the fix-PR read already had.
It starts at the first run that left the row unanswered, never at the merge, and it lives only while
the row stays unanswered. Past the horizon a lookup or a revert check that never answered closes as
`unjudgeable` / `unjudgeable_merge` (trains nothing, never a FAIL), which is also how an ANSWERED
revert check that cannot decide closes. Every run prints the next retry close beside drainable.

No real API: find_merge's gh answers by argv, the fix-PR read is injected, and the revert search and
base-commit scan go through a stubbed `_run_json`.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest

import durability_sweep
import feedback

NOW = 1_790_000_000  # 2026-09-21T14:13:20Z
DAY = 86400
HORIZON = durability_sweep.RETRY_HORIZON_DAYS * DAY
# What gh printed on 2026-10-04 for `gh pr view 1 -R stranske/<a repository that does not exist>`.
REPO_GONE = "GraphQL: Could not resolve to a Repository with the name 'o/gone'. (repository)"
RATE_LIMITED = "API rate limit exceeded for user ID 23046322."


def _iso(ts: int) -> str:
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _pr(number: int, merged_at: int) -> dict:
    return {
        "number": number,
        "state": "MERGED",
        "mergedAt": _iso(merged_at),
        "mergeCommit": {"oid": f"sha{number}"},
        "baseRefName": "main",
        "headRefName": f"head-{number}",
        "files": [{"path": "src/app.py"}],
    }


class Gh:
    """find_merge's gh: `gh pr view N -R repo` answers from `prs`, keyed (repo, N), or fails with
    `fail[N]`, the text gh printed. Any other number is not a pull request."""

    def __init__(self, prs: dict | None = None, fail: dict | None = None):
        self.prs = prs or {}
        self.fail = fail or {}

    def __call__(self, args, **_kw):
        assert args[1:3] == ["pr", "view"], args
        number = int(args[3])
        if number in self.fail:
            return None, self.fail[number]
        pr = self.prs.get((args[args.index("-R") + 1], number))
        if pr:
            return pr, None
        return None, f"GraphQL: Could not resolve to a PullRequest with the number of {number}."


def _fix_read_whole(_repo, _since, _until=None):
    return [], True  # every fix PR since the merge read, none names it


def _fix_read_fails(_repo, _since, _until=None):
    return None, False


def _no_answer(_args, **_kw):
    return None  # the revert search and the base-commit scan both go unanswered


@pytest.fixture
def brain(monkeypatch, tmp_path):
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path))
    # The revert search answers "nothing found, search complete"; the base scan finds no revert.
    monkeypatch.setattr(durability_sweep, "_run_json", lambda args, **_kw: [])
    return tmp_path


def _merged(run_id: str, repo: str, number: int, merged_at: int) -> None:
    feedback.record_run(
        run_id, f"{repo}#{number}", "implement", "codex", mode="remote", ts=merged_at - 3600
    )
    feedback.record_outcome(
        run_id,
        adjudicated_verdict="PASS",
        merged=True,
        durability="pending",
        notes="remote keepalive PR merged; durability pending sweep",
    )


def _sweep(gh, *, now: int = NOW, fix=_fix_read_whole, **kw):
    return durability_sweep.sweep_durability(
        _gh=gh, _fix_fn=fix, _now=now, _verifier_fetch_fn=lambda _repo, _nums: {}, **kw
    )


def _row(run_id: str):
    with feedback._conn() as c:
        return c.execute(
            "SELECT durability, failure_class, notes FROM outcomes WHERE run_id=?", (run_id,)
        ).fetchone()


def _clocks(state_dir) -> dict:
    return json.loads((state_dir / durability_sweep.RETRY_CLOCKS).read_text())


def _skip_reason(res: dict, run_id: str) -> str:
    (detail,) = [d for d in res["details"] if d["run_id"] == run_id and d["action"] == "skip"]
    return detail["reason"]


# --- path 1: a merge lookup GitHub never answers ------------------------------------------------


def test_a_lookup_github_never_answers_waits_then_closes_at_the_horizon(brain):
    _merged("gone", "o/gone", 30, NOW - 20 * DAY)
    gh = Gh(fail={30: REPO_GONE})
    first = _sweep(gh)
    # Pending scores as a provisional PASS, so a retry with no end is a win with no end.
    assert _row("gone")[0] == "pending" and feedback._is_success("pending", "PASS")
    assert (first["skipped"], first["drainable"], first["drains"]["retry"]) == (1, 1, 1), first
    closes = _iso(NOW + HORIZON)[:10]
    assert (first["next_retry_close"], first["next_grace_drain"]) == (closes, None), first
    assert first["line"].endswith(
        "pending 1, drainable 1 (grace 0, retry 1, fix_search 0, acting_run 0, "
        f"next retry close {closes}), undrainable 0"
    ), first["line"]
    assert _clocks(brain) == {"gone": NOW}

    last_chance = _sweep(gh, now=NOW + HORIZON - 1)
    assert _row("gone")[0] == "pending" and last_chance["retry_closed"] == 0, last_chance
    assert _clocks(brain) == {"gone": NOW}  # asking again never restarts the clock

    closed = _sweep(gh, now=NOW + HORIZON)
    durability, failure_class, notes = _row("gone")
    assert (durability, failure_class) == (
        feedback.DURABILITY_UNJUDGEABLE,
        feedback.UNJUDGEABLE_MERGE,
    )
    assert "Could not resolve to a Repository" in notes, notes
    assert f"unanswered since {_iso(NOW)}" in notes, notes
    assert (closed["retry_closed"], closed["unjudgeable"], closed["skipped"]) == (1, 1, 0), closed
    days = durability_sweep.RETRY_HORIZON_DAYS
    assert f"closed unjudgeable 1 (1 unanswered for {days}d);" in closed["line"], closed["line"]
    assert closed["line"].endswith("pending 0, fully drained"), closed["line"]
    assert _clocks(brain) == {}
    # Unknown trains nothing and is never a failure; and the row is terminal.
    assert feedback.UNJUDGEABLE_MERGE in feedback.LEARNING_EXCLUDED_FAILURE_CLASSES
    assert not feedback._has_outcome_evidence(durability, "PASS", None, failure_class=failure_class)
    assert _sweep(gh, now=NOW + HORIZON + DAY)["checked"] == 0


# --- path 2: a revert check that never answers --------------------------------------------------


def test_a_revert_check_that_never_answers_waits_then_closes_as_unjudgeable_merge(
    brain, monkeypatch
):
    monkeypatch.setattr(durability_sweep, "_run_json", _no_answer)
    _merged("unreverted", "o/r", 40, NOW - 20 * DAY)
    gh = Gh(prs={("o/r", 40): _pr(40, NOW - 20 * DAY)})
    first = _sweep(gh)
    assert _row("unreverted")[0] == "pending" and first["drains"]["retry"] == 1, first
    # The fix-PR read covered the merge; only the revert check is unanswered.
    assert (first["fix_search"]["covered"], first["fix_search"]["uncovered"]) == (1, 0)
    assert _skip_reason(first, "unreverted") == (
        f"{durability_sweep.REVERT_SEARCH_FAILED}; {durability_sweep.BASE_SCAN_FAILED}"
    )
    closed = _sweep(gh, now=NOW + HORIZON)
    durability, failure_class, notes = _row("unreverted")
    assert (durability, failure_class) == (
        feedback.DURABILITY_UNJUDGEABLE,
        feedback.UNJUDGEABLE_MERGE,
    )
    assert durability_sweep.REVERT_SEARCH_FAILED in notes and "unanswered since" in notes, notes
    assert (closed["retry_closed"], closed["fix_search"]["closed"]) == (1, 0), closed
    assert closed["line"].endswith("pending 0, fully drained"), closed["line"]


def test_an_answered_revert_check_that_cannot_decide_closes_at_once_in_the_same_class(
    brain, monkeypatch
):
    """Why the unanswered one closes as unjudgeable_merge: it is where this one already goes."""

    def both_past_their_limits(args, **_kw):
        if args[:3] == ["gh", "pr", "list"]:
            return [
                {"number": 900 + i, "title": "Revert unrelated", "body": "Refs #999"}
                for i in range(durability_sweep.MAX_REVERT_PRS)
            ]
        return [
            {"sha": f"c{i}", "commit": {"message": "ordinary"}}
            for i in range(durability_sweep.MAX_BASE_COMMITS)
        ]

    monkeypatch.setattr(durability_sweep, "_run_json", both_past_their_limits)
    _merged("undecidable", "o/r", 41, NOW - 20 * DAY)
    res = _sweep(Gh(prs={("o/r", 41): _pr(41, NOW - 20 * DAY)}))
    assert _row("undecidable")[:2] == (feedback.DURABILITY_UNJUDGEABLE, feedback.UNJUDGEABLE_MERGE)
    assert (res["retry_closed"], res["skipped"], res["unjudgeable"]) == (0, 0, 1), res


# --- the clock: from the first unanswered run, never the merge, and only while unanswered ------


@pytest.mark.parametrize("path", ["lookup", "revert"])
def test_a_backlog_rows_first_unanswered_run_cannot_close_it(brain, monkeypatch, path):
    merged_at = NOW - 200 * DAY
    _merged("backlog", "o/r", 42, merged_at)
    if path == "lookup":
        gh = Gh(fail={42: RATE_LIMITED})
    else:
        monkeypatch.setattr(durability_sweep, "_run_json", _no_answer)
        gh = Gh(prs={("o/r", 42): _pr(42, merged_at)})
    res = _sweep(gh)
    assert _row("backlog")[0] == "pending" and res["drains"]["retry"] == 1, res
    assert res["retry_closed"] == 0 and _clocks(brain) == {"backlog": NOW}


def test_a_clock_does_not_survive_an_answered_run_into_the_next_miss(brain, monkeypatch):
    """Missed inside grace, answered while still in grace, missed again past both the grace and
    the first clock's close: the second miss starts a new clock. One kept through grace would close
    the row on its first unanswered revert check."""
    merged_at = NOW - 2 * DAY
    pr = _pr(50, merged_at)
    _merged("young", "o/r", 50, merged_at)
    _sweep(Gh(fail={50: RATE_LIMITED}))
    assert _clocks(brain) == {"young": NOW}
    graced = _sweep(Gh(prs={("o/r", 50): pr}), now=NOW + DAY)
    assert graced["drains"]["grace"] == 1 and _clocks(brain) == {}, graced
    monkeypatch.setattr(durability_sweep, "_run_json", _no_answer)
    later = NOW + 8 * DAY  # past grace, and past where the first clock would have closed
    res = _sweep(Gh(prs={("o/r", 50): pr}), now=later)
    assert _row("young")[0] == "pending" and res["retry_closed"] == 0, res
    assert _clocks(brain) == {"young": later}


def test_a_lost_or_corrupt_clock_file_only_retries_longer(brain):
    _merged("gone", "o/gone", 30, NOW - 20 * DAY)
    gh = Gh(fail={30: REPO_GONE})
    _sweep(gh)
    (brain / durability_sweep.RETRY_CLOCKS).write_text("{not json")
    res = _sweep(gh, now=NOW + 30 * DAY)
    assert _row("gone")[0] == "pending" and res["retry_closed"] == 0, res
    assert _clocks(brain) == {"gone": NOW + 30 * DAY}


def test_a_dry_run_starts_no_clock_and_closes_nothing(brain):
    _merged("gone", "o/gone", 30, NOW - 20 * DAY)
    gh = Gh(fail={30: REPO_GONE})
    assert _sweep(gh, dry_run=True)["drains"]["retry"] == 1
    assert not (brain / durability_sweep.RETRY_CLOCKS).exists()
    _sweep(gh)
    would = _sweep(gh, now=NOW + HORIZON, dry_run=True)
    assert would["retry_closed"] == 1 and _row("gone")[0] == "pending", would
    assert _clocks(brain) == {"gone": NOW}


# --- one horizon for every unanswered drain ---------------------------------------------------


def test_one_horizon_closes_a_lookup_and_an_unread_merge_on_the_same_run(brain):
    _merged("gone", "o/gone", 30, NOW - 20 * DAY)
    _merged("unread", "o/r", 31, NOW - 20 * DAY)
    gh = Gh(prs={("o/r", 31): _pr(31, NOW - 20 * DAY)}, fail={30: REPO_GONE})
    first = _sweep(gh, fix=_fix_read_fails)
    closes = _iso(NOW + HORIZON)[:10]
    assert (first["next_retry_close"], first["next_fix_search_close"]) == (closes, closes)
    assert f"next retry close {closes}, next unread close {closes}" in first["line"], first["line"]
    assert _clocks(brain) == {"gone": NOW, "unread": NOW}
    before = _sweep(gh, now=NOW + HORIZON - 1, fix=_fix_read_fails)
    assert (before["retry_closed"], before["fix_search"]["closed"]) == (0, 0), before
    closed = _sweep(gh, now=NOW + HORIZON, fix=_fix_read_fails)
    assert (closed["retry_closed"], closed["fix_search"]["closed"]) == (1, 1), closed
    assert _row("gone")[1] == feedback.UNJUDGEABLE_MERGE
    assert _row("unread")[1] == feedback.BROKE_LATER_UNCHECKED
    assert closed["line"].endswith("pending 0, fully drained"), closed["line"]


def test_a_role_run_waiting_on_an_unanswered_acting_run_drains_with_it(brain):
    feedback.record_role_run("role:triage:gemini:1", "triage", "triage:1-items", "gemini")
    feedback.record_run(
        "acting",
        "o/gone#20",
        "testgen",
        "gemini",
        mode="remote",
        ts=NOW - 40 * DAY,
        influenced_by_role_run_ids=["role:triage:gemini:1"],
    )
    feedback.record_outcome("acting", adjudicated_verdict="PASS", merged=True, durability="pending")
    gh = Gh(fail={20: REPO_GONE})
    first = _sweep(gh)
    assert first["drains"] == {"grace": 0, "retry": 1, "fix_search": 0, "acting_run": 1}, first
    closed = _sweep(gh, now=NOW + HORIZON)
    assert (closed["retry_closed"], closed["lineage_resolved"], closed["skipped"]) == (1, 1, 0)
    assert _row("role:triage:gemini:1")[:2] == ("unjudgeable", feedback.UNJUDGEABLE_MERGE)
    assert closed["line"].endswith("pending 0, fully drained"), closed["line"]
