"""The broke-later check reads EVERY fix PR merged since the merge it judges, or says it did not.

THE DEFECT (measured 2026-10-04). `_fetch_repo_fix_prs` ran one `gh pr list --search "fix in:title"
--limit 200` per repo. An unsorted GitHub search is ordered by relevance, so in a repo with more fix
PRs than that (Workflows 1,185 all-time, Trend_Model_Project 538) the 200 came from its whole history
and a merge's own weeks were read only by chance. When nothing read named the merge and the list was
full, the check said "unknown" and the classifier recorded `durable` anyway, by an "additive" rule:
436 of the 1,367 rows judged durable since the broke-later detection floor carry that note.

THE RULE. One read per repo per run, from the oldest merge being judged (`fix_search_plan`), read
whole: one `merged:<since>..*` range qualifier, up to GitHub's 1,000-result cap, and a window at the
cap is split by merge date and read newest first. A merge the read reached is judged as before. One it
did not reach is never durable: it stays pending under DRAIN_FIX_SEARCH, read again by every run, and
is closed as `unjudgeable` / `broke_later_unchecked` (trains nothing, never a FAIL) once
FIX_SEARCH_RETRY_DAYS have passed since the first run that missed it, so it can never wait forever.

No real API: find_merge's gh answers `gh pr view` from a dict, the fix read is an injected fake that
answers a merge-date window like GitHub (truncated to its cap, newest kept), and the revert search and
base-commit scan go through a stubbed `_run_json` that finds nothing.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest

import durability_sweep
import feedback

NOW = 1_790_000_000  # 2026-09-21T14:13:20Z
DAY = 86400
RETRY = durability_sweep.FIX_SEARCH_RETRY_DAYS * DAY


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


def _fix(number: int, merged_at: int, body: str = "no reference at all") -> dict:
    return {"number": number, "title": "fix: follow-up", "body": body, "mergedAt": _iso(merged_at)}


class PrView:
    """find_merge's gh: `gh pr view N -R repo` answers from `prs`, keyed (repo, N)."""

    def __init__(self, prs: dict):
        self.prs = prs

    def __call__(self, args, **_kw):
        assert args[1:3] == ["pr", "view"], args
        number = int(args[3])
        pr = self.prs.get((args[args.index("-R") + 1], number))
        if pr:
            return pr, None
        return None, f"GraphQL: Could not resolve to a PullRequest with the number of {number}."


class FixSearch:
    """The fix-PR read's search. Answers [since, until] the way GitHub does: at most `cap` results,
    and a truncated answer keeps the newest, which is the shape that hid a merge's own weeks."""

    def __init__(self, fixes=None, *, cap: int = durability_sweep.FIX_SEARCH_CAP, fail=None):
        self.fixes = fixes or {}
        self.cap = cap
        self.fail = fail
        self.calls: list[tuple[str, int, int | None]] = []

    def __call__(self, repo, since, until=None):
        self.calls.append((repo, since, until))
        if self.fail and self.fail(repo, since, until):
            return None, False

        def merged(fix):
            return durability_sweep._parse_gh_ts(fix["mergedAt"])

        hits = [
            fix
            for fix in self.fixes.get(repo, [])
            if since <= merged(fix) and (until is None or merged(fix) <= until)
        ]
        returned = sorted(hits, key=merged, reverse=True)[: self.cap]
        return returned, len(returned) < self.cap


def _never_answers(_repo, _since, _until):
    return True


@pytest.fixture
def brain(monkeypatch, tmp_path):
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path))
    # Revert searches answer "nothing found, search complete"; the base-commit scan finds no revert.
    monkeypatch.setattr(durability_sweep, "_run_json", lambda args, **_kw: [])
    return tmp_path


def _merged(
    run_id: str, repo: str, number: int, merged_at: int, *, started: int | None = None
) -> None:
    started = merged_at - 3600 if started is None else started
    feedback.record_run(run_id, f"{repo}#{number}", "implement", "codex", mode="remote", ts=started)
    feedback.record_outcome(
        run_id,
        adjudicated_verdict="PASS",
        merged=True,
        durability="pending",
        notes="remote keepalive PR merged; durability pending sweep",
    )


def _sweep(view, search, *, now: int = NOW, **kw):
    return durability_sweep.sweep_durability(
        _gh=view, _fix_fn=search, _now=now, _verifier_fetch_fn=lambda _repo, _nums: {}, **kw
    )


def _row(run_id: str):
    with feedback._conn() as c:
        return c.execute(
            "SELECT durability, failure_class, notes FROM outcomes WHERE run_id=?", (run_id,)
        ).fetchone()


def _clocks(state_dir) -> dict:
    return json.loads((state_dir / durability_sweep.FIX_RETRY_STATE).read_text())


def _one_unread_merge(merged_at: int = NOW - 20 * DAY):
    _merged("unread", "o/r", 101, merged_at)
    return PrView({("o/r", 101): _pr(101, merged_at)}), FixSearch(fail=_never_answers)


# --- coverage: one read per repo, reaching the oldest merge it will be asked about -------------


def test_one_read_per_repo_reaches_back_to_its_oldest_merge(brain):
    """The fix that names an OLD merge landed two days after it, far from "the newest N"."""
    merges = {
        ("o/r", 101): NOW - 40 * DAY,
        ("o/r", 102): NOW - 25 * DAY,
        ("o/r", 103): NOW - 10 * DAY,
        ("o/s", 201): NOW - 20 * DAY,
    }
    for (repo, number), merged_at in merges.items():
        _merged(f"run-{number}", repo, number, merged_at)
    naming = _fix(900, NOW - 38 * DAY, "Fixes the regression introduced in #101")
    noise = [_fix(910 + i, NOW - i * DAY) for i in range(1, 9)]
    search = FixSearch({"o/r": [naming, *noise]})
    res = _sweep(PrView({key: _pr(key[1], at) for key, at in merges.items()}), search)
    assert sorted(search.calls) == [("o/r", NOW - 40 * DAY, None), ("o/s", NOW - 20 * DAY, None)]
    assert _row("run-101")[0] == "broke_later" and "#900" in _row("run-101")[2]
    assert [_row(f"run-{n}")[0] for n in (102, 103, 201)] == ["durable", "durable", "durable"]
    fix = res["fix_search"]
    assert (fix["covered"], fix["uncovered"], fix["closed"], fix["reads"]) == (4, 0, 0, 2), fix


def test_the_plan_not_the_first_row_sets_where_the_read_starts(brain):
    """Rows are judged in run order, and a run that started first can hold the NEWER merge. The read
    must still start at the oldest merge, or every older merge in the repo goes unread."""
    _merged("newer-first", "o/r", 102, NOW - 10 * DAY, started=NOW - 50 * DAY)
    _merged("older-later", "o/r", 101, NOW - 40 * DAY, started=NOW - 45 * DAY)
    search = FixSearch({"o/r": [_fix(900, NOW - 39 * DAY, "Fixes #101")]})
    view = PrView({("o/r", 102): _pr(102, NOW - 10 * DAY), ("o/r", 101): _pr(101, NOW - 40 * DAY)})
    res = _sweep(view, search)
    assert search.calls == [("o/r", NOW - 40 * DAY, None)], search.calls
    assert (_row("older-later")[0], _row("newer-first")[0]) == ("broke_later", "durable")
    assert (res["fix_search"]["covered"], res["fix_search"]["uncovered"]) == (2, 0)


def test_a_window_past_the_cap_is_split_by_merge_date_until_read_whole(brain):
    """A capped answer keeps the newest fixes and drops the one naming the merge, so a full answer
    is never taken for the whole window: it is split, and the older half is read too."""
    _merged("old", "o/r", 101, NOW - 30 * DAY)
    noise = [_fix(900 + i, NOW - i * DAY) for i in (2, 3, 4)]
    naming = _fix(999, NOW - 29 * DAY, "Repairs the regression from #101")
    search = FixSearch({"o/r": [*noise, naming]}, cap=3)
    res = _sweep(PrView({("o/r", 101): _pr(101, NOW - 30 * DAY)}), search)
    assert _row("old")[0] == "broke_later" and "#999" in _row("old")[2], _row("old")
    assert search.calls[0] == ("o/r", NOW - 30 * DAY, None)  # the whole window, first
    assert 1 < res["fix_search"]["reads"] <= durability_sweep.FIX_SEARCH_MAX_READS
    assert res["fix_search"]["repos"]["o/r"]["covered_from"] == _iso(NOW - 30 * DAY)


def test_a_failed_older_window_costs_only_the_older_merges(brain):
    """Read newest first: the merges after the last window read whole are still judged."""
    _merged("older", "o/r", 101, NOW - 30 * DAY)
    _merged("newer", "o/r", 102, NOW - 8 * DAY)
    fixes = [_fix(900, NOW - 1 * DAY), _fix(901, NOW - 2 * DAY), _fix(902, NOW - 20 * DAY)]
    search = FixSearch({"o/r": fixes}, cap=3, fail=lambda _repo, _since, until: until is not None)
    view = PrView({("o/r", 101): _pr(101, NOW - 30 * DAY), ("o/r", 102): _pr(102, NOW - 8 * DAY)})
    res = _sweep(view, search)
    assert _row("newer")[0] == "durable" and _row("older")[0] == "pending"
    assert (res["fix_search"]["covered"], res["fix_search"]["uncovered"]) == (1, 1)
    assert res["fix_search"]["repos"]["o/r"]["error"] == "fix-PR search unavailable"


def test_a_split_that_cannot_converge_spends_its_budget_never_a_search_per_pr(brain):
    """A query GitHub did not apply answers every window at the cap: bounded, and per repo."""
    calls = []

    def always_full(repo, since, until=None):
        calls.append((repo, since, until))
        return [_fix(900 + i, NOW - DAY) for i in range(3)], False

    for number, days in ((101, 30), (102, 20), (103, 10)):
        _merged(f"run-{number}", "o/r", number, NOW - days * DAY)
    view = PrView({("o/r", n): _pr(n, NOW - d * DAY) for n, d in ((101, 30), (102, 20), (103, 10))})
    res = _sweep(view, always_full)
    assert len(calls) == durability_sweep.FIX_SEARCH_MAX_READS, calls
    assert [_row(f"run-{n}")[0] for n in (101, 102, 103)] == ["pending"] * 3
    assert res["fix_search"]["uncovered"] == 3 and res["drains"]["fix_search"] == 3
    assert "spent its" in res["fix_search"]["repos"]["o/r"]["error"]


def test_a_merge_inside_grace_costs_no_read(brain):
    _merged("young", "o/r", 101, NOW - 2 * DAY)
    search = FixSearch()
    res = _sweep(PrView({("o/r", 101): _pr(101, NOW - 2 * DAY)}), search)
    assert search.calls == [] and res["drains"]["grace"] == 1
    assert (res["fix_search"]["covered"], res["fix_search"]["uncovered"]) == (0, 0)


def test_the_read_is_one_range_qualifier_up_to_githubs_cap(monkeypatch):
    seen: list[list[str]] = []

    def answer(args, **_kw):
        seen.append(args)
        return []

    monkeypatch.setattr(durability_sweep, "_run_json", answer)
    assert durability_sweep._fetch_repo_fix_prs("o/r", NOW - 40 * DAY) == ([], True)
    durability_sweep._fetch_repo_fix_prs("o/r", NOW - 40 * DAY, NOW - 20 * DAY)
    open_query, bounded_query = (args[args.index("--search") + 1] for args in seen)
    assert open_query == f"fix in:title merged:{_iso(NOW - 40 * DAY)}..*"
    assert bounded_query == f"fix in:title merged:{_iso(NOW - 40 * DAY)}..{_iso(NOW - 20 * DAY)}"
    # GitHub does not AND two `merged:` qualifiers (it answered with all 1,185 on 2026-10-04).
    assert open_query.count("merged:") == 1 and bounded_query.count("merged:") == 1
    assert seen[0][seen[0].index("--limit") + 1] == str(durability_sweep.FIX_SEARCH_CAP) == "1000"
    full_page = [{"number": n} for n in range(durability_sweep.FIX_SEARCH_CAP)]
    monkeypatch.setattr(durability_sweep, "_run_json", lambda args, **_kw: full_page)
    assert durability_sweep._fetch_repo_fix_prs("o/r", NOW)[1] is False  # full is not whole
    monkeypatch.setattr(durability_sweep, "_run_json", lambda args, **_kw: None)
    assert durability_sweep._fetch_repo_fix_prs("o/r", NOW) == (None, False)


# --- unknown is not durable, and it cannot wait forever ----------------------------------------


def test_an_unread_merge_is_pending_and_never_durable(brain):
    view, failing = _one_unread_merge()
    res = _sweep(view, failing)
    assert _row("unread")[0] == "pending"
    assert (res["skipped"], res["drainable"], res["undrainable"]) == (1, 1, 0), res
    assert res["drains"]["fix_search"] == 1
    fix = res["fix_search"]
    assert (fix["covered"], fix["uncovered"], fix["closed"]) == (0, 1, 0), fix
    closes = _iso(NOW + RETRY)[:10]
    assert res["next_fix_search_close"] == closes, res
    assert f"next unread close {closes}" in res["line"], res["line"]
    assert _clocks(brain) == {"unread": NOW}


def test_an_unread_merge_closes_unchecked_when_its_retry_window_ends(brain):
    view, failing = _one_unread_merge()
    _sweep(view, failing)  # the first run that misses it starts its clock
    last_chance = _sweep(view, failing, now=NOW + RETRY - 1)
    assert _row("unread")[0] == "pending" and last_chance["fix_search"]["closed"] == 0
    closed = _sweep(view, failing, now=NOW + RETRY)
    durability, failure_class, notes = _row("unread")
    assert (durability, failure_class) == (
        feedback.DURABILITY_UNJUDGEABLE,
        feedback.BROKE_LATER_UNCHECKED,
    )
    assert "fix-PR search did not reach this merge" in notes and "unread since" in notes, notes
    assert (closed["fix_search"]["closed"], closed["unjudgeable"], closed["skipped"]) == (1, 1, 0)
    assert "(1 closed unchecked)" in closed["line"], closed["line"]
    assert closed["line"].endswith("pending 0, fully drained"), closed["line"]
    assert _clocks(brain) == {}
    # Unknown trains nothing, and is never a failure.
    assert feedback.BROKE_LATER_UNCHECKED in feedback.LEARNING_EXCLUDED_FAILURE_CLASSES
    assert not feedback._has_outcome_evidence(durability, "PASS", None, failure_class=failure_class)


def test_a_backlog_merge_is_not_closed_by_its_first_unread_run(brain):
    """The clock starts at the first run that missed the merge, never at the merge itself."""
    view, failing = _one_unread_merge(NOW - 100 * DAY)
    res = _sweep(view, failing)
    assert _row("unread")[0] == "pending" and res["fix_search"]["closed"] == 0


def test_a_lost_or_corrupt_clock_file_restarts_the_clock_and_never_closes_early(brain):
    view, failing = _one_unread_merge()
    (brain / durability_sweep.FIX_RETRY_STATE).write_text("{not json")
    _sweep(view, failing, now=NOW + 30 * DAY)
    assert _row("unread")[0] == "pending" and _clocks(brain) == {"unread": NOW + 30 * DAY}


def test_a_merge_read_on_a_later_run_is_judged_and_its_clock_dropped(brain):
    view, failing = _one_unread_merge()
    _sweep(view, failing)
    res = _sweep(view, FixSearch(), now=NOW + DAY)
    assert _row("unread")[0] == "durable" and res["fix_search"]["covered"] == 1
    assert _clocks(brain) == {}


def test_a_dry_run_starts_no_clock(brain):
    view, failing = _one_unread_merge()
    res = _sweep(view, failing, dry_run=True)
    assert res["drains"]["fix_search"] == 1 and _row("unread")[0] == "pending"
    assert not (brain / durability_sweep.FIX_RETRY_STATE).exists()


def test_the_drained_line_states_coverage_by_count(brain):
    """Latched-gate question 4: the drained rendering, reached by construction, counts by ==."""
    empty = _sweep(PrView({}), FixSearch())
    assert empty["fix_search"] == {
        "covered": 0,
        "uncovered": 0,
        "closed": 0,
        "reads": 0,
        "repos": {},
    }
    assert empty["line"].endswith(
        "fix search covered 0, uncovered 0; pending 0, fully drained"
    ), empty["line"]
    _merged("judged", "o/r", 101, NOW - 20 * DAY)
    judged = _sweep(PrView({("o/r", 101): _pr(101, NOW - 20 * DAY)}), FixSearch())
    assert judged["line"].endswith(
        "fix search covered 1, uncovered 0; pending 0, fully drained"
    ), judged["line"]
    read = judged["fix_search"]["repos"]["o/r"]
    assert (read["since"], read["covered_from"], read["reads"], read["error"]) == (
        _iso(NOW - 20 * DAY),
        _iso(NOW - 20 * DAY),
        1,
        None,
    )


# --- keepalive ingest: the second caller, planned the same way ----------------------------------


def _keepalive_pr(number: int, merged_at: int) -> dict:
    return {
        "number": number,
        "state": "MERGED",
        "title": f"Change {number}",
        "labels": [{"name": "agent:codex"}],
        "createdAt": _iso(merged_at - 3600),
        "updatedAt": _iso(merged_at),
        "mergedAt": _iso(merged_at),
        "closedAt": _iso(merged_at),
        "headRefName": f"codex/issue-{number}",
        "baseRefName": "main",
        "mergeCommit": {"oid": f"sha{number}"},
        "author": {"login": "someone"},
        "body": "",
        "files": [{"path": "src/app.py"}],
        "reverted": False,
    }


def test_keepalive_ingest_reads_each_repo_once_from_its_oldest_merge(brain):
    """Ingest judges a merged PR past grace too, and until 2026-10-04 it passed no cache, so it ran
    one search per PR. It plans its reads exactly as the sweep does."""
    import keepalive_outcomes

    prs = [_keepalive_pr(n, NOW - days * DAY) for n, days in ((301, 10), (302, 25), (303, 2))]
    search = FixSearch({"o/r": [_fix(900, NOW - 24 * DAY, "Fixes a regression from #302")]})
    keepalive_outcomes.ingest_keepalive_outcomes(
        ["o/r"],
        _pr_fetch_fn=lambda _repo, _days: prs,
        _now=NOW,
        _fix_fn=search,
        _closure_context_fn=lambda _repo, _number: "",
        _evidence_fetch_fn=lambda _repo, _numbers: {},
    )
    assert search.calls == [("o/r", NOW - 25 * DAY, None)], search.calls
    judged = {n: _row(f"keepalive:o/r#{n}:codex")[0] for n in (301, 302, 303)}
    assert judged == {301: "durable", 302: "broke_later", 303: "pending"}, judged
