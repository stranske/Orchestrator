"""The merge credits no remote delegation: outcome ingest decides it with its guard (2026-10-04).

THE DEFECT. `merge_guard.record_merge_outcome` recorded PASS, merged and durability pending for the
latest `mode='remote'` run on the merged target. Keepalive runs and remote delegations share that
mode, so on a labelled PR the latest one can be an `orchestrator_remote` delegation. Outcome ingest
never re-decides a run already recorded as merged and pending durability (it files it under
`pending_durability`), so that PASS bypassed the attribution guard #439 added
(`outcomes._delegated_pr_state`: the delegation's own PR, settled after the label, with a completed
round of the delegated agent's runner since then). Latent when fixed: the 2 outcome rows the merge
had written were keepalive runs, but each of the 23 delegation runs was the latest remote run on its
target, and 2 of those targets are PRs (Trend#5913, Trend#5944).

THE RULE. The merge credits the latest remote run that is not a delegation: a keepalive run IS its
PR, which is ingest's own rule. A delegation gets no outcome from the merge. One with no outcome row
stays in ingest's pending set and is named in `deferred_to_ingest`; one already recorded (its PR
closed, then reopened and merged) is named in `delegations_already_recorded`, because ingest never
re-decides a recorded run. Both sides read ONE predicate, `outcomes.needs_delegation_guard`. No real
API: `subprocess.run` is replaced by a stub.
"""

from __future__ import annotations

import json
import subprocess

import pytest

import feedback
import merge_guard
import outcomes

LABEL = 1_790_000_000  # when the delegation's label was applied (runs.ts)
KEEPALIVE = "keepalive:o/r#5913:codex"
DELEGATION = "remote:o/r#5913:gemini"


@pytest.fixture
def brain(monkeypatch, tmp_path):
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path))
    return tmp_path


def _record_at(run_id: str, agent: str, ts: int) -> None:
    feedback.record_run(run_id, "o/r#5913", "implement", agent, mode="remote")
    with feedback._conn() as c:
        c.execute("UPDATE runs SET ts=? WHERE run_id=?", (ts, run_id))


def _trend_5913_shape() -> None:
    """Trend#5913: codex's keepalive run, then a later delegation that labelled gemini onto it."""
    _record_at(KEEPALIVE, "codex", LABEL - 3600)
    _record_at(DELEGATION, "gemini", LABEL)


def _row(run_id: str):
    with feedback._conn() as c:
        return c.execute(
            "SELECT merged, adjudicated_verdict, durability, failure_class, notes "
            "FROM outcomes WHERE run_id=?",
            (run_id,),
        ).fetchone()


def _stub_gh(monkeypatch, answers: dict) -> list[str]:
    """`pr view` and the PR's `comments`, by verb; an unexpected call fails the test."""
    calls: list[str] = []

    def fake_run(argv, capture_output=True, text=True, **_kw):
        key = "comments" if argv[1] == "api" else " ".join(argv[1:3])
        calls.append(key)
        if key not in answers:
            raise AssertionError(f"unexpected gh call: {argv}")
        return subprocess.CompletedProcess(argv, *answers[key])

    monkeypatch.setattr(outcomes.subprocess, "run", fake_run)
    return calls


def _merged_pr() -> tuple:
    merged_at = outcomes._iso(LABEL + 3000)
    pr = {
        "number": 5913,
        "headRefName": "codex/issue-5858",
        "state": "MERGED",
        "mergedAt": merged_at,
        "closedAt": merged_at,
    }
    return (0, json.dumps(pr), "")


def test_the_latest_delegation_is_deferred_and_the_keepalive_run_is_credited(brain) -> None:
    _trend_5913_shape()

    result = merge_guard.record_merge_outcome("o/r#5913")

    assert result == {
        "recorded": True,
        "run_id": KEEPALIVE,
        "deferred_to_ingest": [DELEGATION],
        "delegations_already_recorded": [],
    }
    merged, verdict, durability, failure_class, notes = _row(KEEPALIVE)
    assert (merged, verdict, durability, failure_class) == (1, "PASS", "pending", None)
    assert notes.startswith("merge_guard:")
    assert _row(DELEGATION) is None


def test_the_deferred_delegation_is_decided_by_ingest_with_the_guard(brain, monkeypatch) -> None:
    """End to end: the merge leaves the delegation to ingest, and ingest's guard finds no gemini
    round on the PR, so the run ends unattributed rather than as the merge's PASS."""
    _trend_5913_shape()
    merge_guard.record_merge_outcome("o/r#5913")
    pending = {run["run_id"]: run for run in outcomes._pending_runs("remote")}
    assert pending[DELEGATION]["has_outcome"] is False

    calls = _stub_gh(monkeypatch, {"pr view": _merged_pr(), "comments": (0, "[[]]", "")})
    summary = outcomes.ingest_outcomes("remote")

    assert (summary["recorded"], summary["unattributed"], summary["pending_durability"]) == (
        1,
        1,
        1,
    ), summary
    assert calls == ["pr view", "comments"]
    merged, verdict, durability, failure_class, notes = _row(DELEGATION)
    assert (merged, verdict, durability) == (None, None, "abandoned")
    assert failure_class == feedback.UNATTRIBUTED_DELEGATION and "gemini" in notes
    # The keepalive run keeps the merge's PASS: ingest does not re-decide a pending-durability row.
    assert _row(KEEPALIVE)[:3] == (1, "PASS", "pending")


def test_only_a_delegation_on_the_target_records_nothing(brain) -> None:
    _record_at(DELEGATION, "gemini", LABEL)

    result = merge_guard.record_merge_outcome("o/r#5913")

    assert result["recorded"] is False
    assert "credits no remote delegation" in result["reason"]
    assert result["deferred_to_ingest"] == [DELEGATION]
    assert _row(DELEGATION) is None


def test_a_delegation_already_recorded_is_named_and_left_alone(brain) -> None:
    """Its PR closed unmerged, so ingest ended the delegation; the PR then reopened and merged.
    Ingest never re-decides a recorded run, so calling it deferred would be false."""
    _trend_5913_shape()
    feedback.record_outcome(
        DELEGATION,
        adjudicated_verdict="FAIL",
        merged=False,
        durability="abandoned",
        notes="remote delegation's PR #5913 closed unmerged",
    )

    result = merge_guard.record_merge_outcome("o/r#5913")

    assert result == {
        "recorded": True,
        "run_id": KEEPALIVE,
        "deferred_to_ingest": [],
        "delegations_already_recorded": [DELEGATION],
    }
    assert _row(DELEGATION)[:3] == (0, "FAIL", "abandoned")
    assert DELEGATION not in {run["run_id"] for run in outcomes._pending_runs("remote")}


def test_a_failed_write_still_reports_the_runs_it_read() -> None:
    runs = [
        {"run_id": DELEGATION, "source": "orchestrator_remote", "ts": 2, "has_outcome": False},
        {"run_id": KEEPALIVE, "source": "keepalive", "ts": 1, "has_outcome": False},
    ]

    def locked(run_id, **kwargs):
        raise RuntimeError("database is locked")

    result = merge_guard.record_merge_outcome(
        "o/r#5913", remote_runs_fn=lambda target, mode=None: runs, record_outcome_fn=locked
    )

    assert result == {
        "recorded": False,
        "run_id": KEEPALIVE,
        "error": "database is locked",
        "deferred_to_ingest": [DELEGATION],
        "delegations_already_recorded": [],
    }


def test_no_remote_run_reports_a_measured_empty_deferral(brain) -> None:
    result = merge_guard.record_merge_outcome("o/r#5913")

    assert result == {
        "recorded": False,
        "reason": "no remote run_id found for target",
        "deferred_to_ingest": [],
        "delegations_already_recorded": [],
    }


def _locked_store(target, mode=None):
    raise RuntimeError("database is locked")


@pytest.mark.parametrize(
    ("runs_fn", "error"),
    [(_locked_store, "database is locked"), (lambda target, mode=None: [{}], "'run_id'")],
    ids=["store-raises", "row-without-run-id"],
)
def test_unreadable_runs_report_unknown_lists_and_never_raise(runs_fn, error: str) -> None:
    """After a merge nothing may raise: the caller must still see that the merge ran."""
    result = merge_guard.record_merge_outcome("o/r#5913", remote_runs_fn=runs_fn)

    assert result == {
        "recorded": False,
        "error": error,
        "deferred_to_ingest": None,
        "delegations_already_recorded": None,
    }


def test_one_predicate_decides_both_the_merge_and_ingest(brain, monkeypatch) -> None:
    """Swap the predicate and BOTH callers follow it, so the two can never disagree."""
    _trend_5913_shape()
    monkeypatch.setattr(outcomes, "needs_delegation_guard", lambda source: source == "keepalive")

    merge = merge_guard.record_merge_outcome(
        "o/r#5913", record_outcome_fn=lambda run_id, **kwargs: None
    )
    assert (merge["run_id"], merge["deferred_to_ingest"]) == (DELEGATION, [KEEPALIVE]), merge

    routed: dict[str, list[str]] = {"guard": [], "direct": []}
    monkeypatch.setattr(
        outcomes,
        "_delegated_pr_state",
        lambda target, agent, ts: routed["guard"].append(agent),
    )
    monkeypatch.setattr(outcomes, "_pr_state", lambda target, agent: routed["direct"].append(agent))
    outcomes.ingest_outcomes("remote")
    assert routed == {"guard": ["codex"], "direct": ["gemini"]}, routed


def test_the_merge_path_credits_through_the_same_rule(brain) -> None:
    """`guarded_merge` reaches the outcome patch with the store's own run list."""
    _trend_5913_shape()

    result = merge_guard.guarded_merge(
        "o/r#5913",
        expected_head="abc123",
        confirm_merge=True,
        metadata_fn=lambda target: {
            "target": target,
            "labels": [],
            "title": "t",
            "state": "OPEN",
            "is_draft": False,
        },
        gate_fn=lambda item, **kwargs: None,
        preflight_fn=lambda target, expected_head: {"blocked": False, "head": expected_head},
        merge_fn=lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, 0, "merged", ""),
    )

    assert result["merge_executed"] is True and result["blocked"] is False, result
    assert result["outcome"] == {
        "recorded": True,
        "run_id": KEEPALIVE,
        "deferred_to_ingest": [DELEGATION],
        "delegations_already_recorded": [],
    }
    assert _row(DELEGATION) is None
