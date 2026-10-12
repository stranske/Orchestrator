"""A later fix PR makes a merge `broke_later` only when it STATES that it repairs it.

THE DEFECT (measured 2026-10-12). `_fix_followup_status` counted any fix-titled later PR whose title
or body contained `#N`. The live Brain held 134 broke_later rows over 129 (merge, naming PR) pairs.
Read against the sentence around each reference, 87 pairs named the merge as a sibling, a rebase
base, a coverage-round target or a "Related Issues/PRs" link, 14 said nothing either way, and 28
stated a repair. Travel-Plan-Permission #1534 and #1561 were two of the siblings: the PRs that
"named" them say "does not replace that work" and "does not stack on #1561".

THE RULE. Each sentence naming N is read as a repair (repairs / follows up / reverts / corrects
it), not a repair (rebased onto it, independent of it, a bare link line), or unstated. Any stated
repair is broke_later. Otherwise a merge some fix PR named without saying how is UNKNOWN: it closes
as `unjudgeable` / `broke_later_unstated`, trains nothing, and is never durable. Rows recorded on
the old rule are judged again by the sweep itself, over the window their verdict covered.

The bodies below are the real paragraphs of the four PRs the measurement started from.
"""

from __future__ import annotations

import pytest
from test_durability_fix_search_coverage import (
    DAY,
    NOW,
    RETRY,
    FixSearch,
    PrView,
    _iso,
    _merged,
    _never_answers,
    _pr,
    _row,
    _sweep,
)

import durability_sweep
import feedback


@pytest.fixture
def brain(monkeypatch, tmp_path):
    """The coverage suite's store: a temp Brain, temp state, and revert checks that find nothing."""
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(durability_sweep, "_run_json", lambda args, **_kw: [])
    return tmp_path


TPP = "stranske/Travel-Plan-Permission"
TMP = "stranske/Trend_Model_Project"

# The PR that named the merge, as GitHub returns it: (repo, named, fix number, title, body, verdict).
SIBLING_1534 = (
    TPP,
    1534,
    1535,
    "fix: validate local and hosted receipt delivery in expense exports",
    "Review coordination: #1534 independently hardens literal spreadsheet text in the same export "
    "module. This PR targets main directly and does not replace that work. Preserve #1534 when "
    "reconciling the branches after it merges. Keepalive owns current-head CI; closer owns final "
    "review, merge, and verify:compare.",
)
SIBLING_1561 = (
    TPP,
    1561,
    1562,
    "fix: refresh manager reviews when requested changes are resubmitted",
    "Keepalive owns asynchronous CI and review. Closer should re-read exact-head checks and threads "
    "after the review window before merge and verify:compare. This branch is based on main and "
    "does not stack on #1561.",
)
REPAIR_1549 = (
    TPP,
    1549,
    1553,
    "fix(pdf): preserve literal approval-table text (#1540 follow-up)",
    "Closes #1540. Bounded follow-up to merged #1549 and its [provider comparison concern]"
    "(https://github.com/stranske/Travel-Plan-Permission/pull/1549#issuecomment-5562317588).",
)
REPAIR_5993 = (
    TMP,
    5993,
    6005,
    "fix(portfolio): restore the max_active_positions guarantee #5993 traded away",
    "Closes #6003.\n\n## What was actually wrong\n\n#6003 asked me to restore the assertion #5993 "
    "relaxed, and to *measure* how many periods the transitional rebalance needs before "
    "`max_active_positions` is reached (Task 1). The measurement is the finding:",
)


def _text(case) -> str:
    return f"{case[3]}\n\n{case[4]}"


@pytest.mark.parametrize(
    "case, relation",
    [
        (SIBLING_1534, durability_sweep.REF_UNSTATED),
        (SIBLING_1561, durability_sweep.REF_NOT_REPAIR),
        (REPAIR_1549, durability_sweep.REF_REPAIR),
        (REPAIR_5993, durability_sweep.REF_REPAIR),
    ],
    ids=["TPP#1534-sibling", "TPP#1561-sibling", "TPP#1549-follow-up", "TMP#5993-traded-away"],
)
def test_the_four_measured_bodies_read_as_their_sentences_say(case, relation):
    repo, named, _fix, _title, _body = case
    assert durability_sweep.reference_relation(_text(case), named, repo)[0] == relation


@pytest.mark.parametrize(
    "body",
    [
        # Each is the shape of a real live false positive.
        "### Related Issues/PRs\n- [#3302](https://github.com/o/r/issues/3302)\n- [#3301](x)",
        "the merge result after #352 landed mid-review; rebased onto #352",
        "Stacked on #600 (issue #596). GitHub retargets this to `main` when #600 merges.",
        "The issue is pre-existing since #198 and was deferred from PR #664.",
        "#3728 independently repairs the verifier code findings.",
    ],
)
def test_sibling_and_bookkeeping_mentions_are_never_a_repair(body):
    for number in (3302, 352, 600, 664, 3728):
        relation, _sentence = durability_sweep.reference_relation(body, number, "o/r")
        assert relation != durability_sweep.REF_REPAIR, (number, body)


def test_another_repositorys_number_is_not_this_one():
    """Fine-Art-Archive#703 was 'named' by "the same class Pension-Data hit in its #703 arc"."""
    text = "Follow-up to Pension-Data#703, which broke the gate."
    assert durability_sweep.reference_relation(text, 703, "o/Fine-Art-Archive") == (None, "")
    assert durability_sweep.reference_relation(text, 703, "o/Pension-Data")[0] == "repair"


def test_the_unstated_class_trains_nothing():
    assert feedback.BROKE_LATER_UNSTATED in feedback.LEARNING_EXCLUDED_FAILURE_CLASSES


def _fix_pr(case, merged_at: int) -> dict:
    return {"number": case[2], "title": case[3], "body": case[4], "mergedAt": _iso(merged_at)}


def _view(*cases, merged_at: int) -> PrView:
    return PrView({(case[0], case[1]): _pr(case[1], merged_at) for case in cases})


def _search(*cases, merged_at: int) -> FixSearch:
    fixes: dict[str, list] = {}
    for case in cases:
        fixes.setdefault(case[0], []).append(_fix_pr(case, merged_at))
    return FixSearch(fixes)


ALL = (SIBLING_1534, SIBLING_1561, REPAIR_1549, REPAIR_5993)


def test_a_pending_merge_is_judged_by_what_the_fix_pr_states(brain):
    for case in ALL:
        _merged(f"run-{case[1]}", case[0], case[1], NOW - 40 * DAY)
    res = _sweep(_view(*ALL, merged_at=NOW - 40 * DAY), _search(*ALL, merged_at=NOW - 39 * DAY))
    assert _row("run-1534")[:2] == (
        feedback.DURABILITY_UNJUDGEABLE,
        feedback.BROKE_LATER_UNSTATED,
    )
    assert "without stating a repair" in _row("run-1534")[2]
    assert _row("run-1561")[0] == "durable"
    for named, fix in ((1549, 1553), (5993, 6005)):
        durability, _class, notes = _row(f"run-{named}")
        assert durability == "broke_later" and f"#{fix} states it repairs" in notes, notes
    assert (res["broke_later"], res["durable"], res["fix_search"]["unstated"]) == (2, 1, 1)
    assert "named without a stated repair 1" in res["line"], res["line"]


# --- the rows the bare-reference rule already wrote ---------------------------------------------


def _bare(run_id: str, case, merged_at: int, judged_at: int) -> None:
    """A row the old rule recorded broke_later because the fix PR's text contained `#N`."""
    repo, named, fix = case[:3]
    feedback.record_run(
        run_id, f"{repo}#{named}", "implement", "codex", mode="remote", ts=merged_at - 3600
    )
    feedback.record_outcome(
        run_id,
        adjudicated_verdict="PASS",
        merged=True,
        durability="broke_later",
        notes=f"durability_sweep: later fix PR #{fix} names this change; held 1d before that",
    )
    with feedback._conn() as c:
        c.execute("UPDATE outcomes SET durability_checked_ts=? WHERE run_id=?", (judged_at, run_id))


def test_bare_reference_rows_are_judged_again_and_the_population_drains(brain):
    for case in ALL:
        _bare(f"old-{case[1]}", case, NOW - 40 * DAY, NOW - 30 * DAY)
    view = _view(*ALL, merged_at=NOW - 40 * DAY)
    search = _search(*ALL, merged_at=NOW - 39 * DAY)
    res = _sweep(view, search)
    fixed = res["restated"]
    assert (fixed["selected"], fixed["broke_later"], fixed["durable"], fixed["closed"]) == (
        4,
        2,
        1,
        1,
    )
    assert _row("old-1534")[:2] == (feedback.DURABILITY_UNJUDGEABLE, feedback.BROKE_LATER_UNSTATED)
    durable, _class, notes = _row("old-1561")
    assert durable == "durable" and "was broke_later on a bare reference" in notes, notes
    for named in (1549, 5993):
        durability, _class, notes = _row(f"old-{named}")
        assert durability == "broke_later" and "states it repairs" in notes, notes
        assert durability_sweep.BARE_REFERENCE_NOTE not in notes, notes
    assert "bare-reference re-judged 4 (broke_later 2, durable 1" in res["line"], res["line"]
    again = _sweep(view, search)
    assert again["restated"]["selected"] == 0 and "; bare-reference 0;" in again["line"]


def test_the_re_check_counts_only_fixes_merged_by_the_verdicts_moment(brain):
    """A stated repair that merged after the verdict was written is a different question."""
    _bare("old", REPAIR_1549, NOW - 40 * DAY, NOW - 30 * DAY)
    search = FixSearch({TPP: [_fix_pr(REPAIR_1549, NOW - 20 * DAY)]})
    _sweep(_view(REPAIR_1549, merged_at=NOW - 40 * DAY), search)
    assert _row("old")[0] == "durable", _row("old")


def test_an_unread_re_check_waits_then_closes_unchecked(brain):
    _bare("old", REPAIR_1549, NOW - 40 * DAY, NOW - 30 * DAY)
    view = _view(REPAIR_1549, merged_at=NOW - 40 * DAY)
    failing = FixSearch(fail=_never_answers)
    first = _sweep(view, failing)
    assert first["restated"]["waiting"] == 1 and _row("old")[0] == "broke_later"
    closed = _sweep(view, failing, now=NOW + RETRY)
    assert _row("old")[:2] == (feedback.DURABILITY_UNJUDGEABLE, feedback.BROKE_LATER_UNCHECKED)
    assert closed["restated"]["closed"] == 1


def test_the_re_check_can_be_undone_and_held_off(brain, monkeypatch):
    _bare("old", SIBLING_1561, NOW - 40 * DAY, NOW - 30 * DAY)
    _sweep(
        _view(SIBLING_1561, merged_at=NOW - 40 * DAY),
        _search(SIBLING_1561, merged_at=NOW - 39 * DAY),
    )
    assert _row("old")[0] == "durable"
    assert durability_sweep.undo_truncated_recheck()["restored"] == 1
    assert _row("old")[0] == "broke_later" and "names this change" in _row("old")[2]
    monkeypatch.setenv(durability_sweep.STATED_RECHECK_SWITCH, "0")
    assert durability_sweep._bare_reference_runs() == []
