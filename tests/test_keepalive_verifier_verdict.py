"""Fleet verifier outcomes require trusted, exact-merge evidence."""

from __future__ import annotations

import json

import durability_sweep
import feedback
import keepalive_outcomes
import verifier_evidence

HEAD = "a" * 40
MERGE = "b" * 40


def _decision(verdict="PASS", *, ci_failed=False, providers=None, **changes):
    decision = {
        "schema": verifier_evidence.MARKER,
        "repo": "o/r",
        "pr": "7",
        "head_sha": HEAD,
        "evaluated_sha": MERGE,
        "run_id": "123",
        "run_attempt": "1",
        "verdict": verdict,
        "ci_failed": ci_failed,
        "provider_verdicts": providers or ["PASS"],
    }
    decision.update(changes)
    return decision


def _evidence_pr(decision=None, *, author="github-actions", head=HEAD, merge=MERGE):
    body = (
        f"## Provider Comparison Report\n<!-- {verifier_evidence.MARKER} "
        f"{json.dumps(decision)} -->"
        if decision
        else "Green CI, no verifier decision"
    )
    return {
        "number": 7,
        "headRefOid": head,
        "mergeCommit": {"oid": merge},
        "comments": {
            "nodes": [
                {
                    "author": {"login": author},
                    "url": "https://github.com/o/r/pull/7#issuecomment-123",
                    "body": body,
                }
            ]
        },
    }


def _merged_pr():
    return {
        "number": 7,
        "state": "MERGED",
        "title": "Fix import",
        "labels": [{"name": "agent:codex"}],
        "createdAt": "2026-09-18T10:00:00Z",
        "updatedAt": "2026-09-18T12:00:00Z",
        "mergedAt": "2026-09-18T12:00:00Z",
        "closedAt": "2026-09-18T12:00:00Z",
        "headRefName": "codex/issue-7-import",
        "baseRefName": "main",
        "mergeCommit": {"oid": MERGE},
        "author": {"login": "stranske"},
        "body": "Closes #6",
        "url": "https://github.com/o/r/pull/7",
    }


def test_verifier_verdict_lands_on_the_outcome_row(monkeypatch, tmp_path):
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "brain.db")
    marker = verifier_evidence.decision_from_pr("o/r", _evidence_pr(_decision()))
    assert marker and marker["verdict"] == "PASS"
    result = keepalive_outcomes.ingest_keepalive_outcomes(
        ["o/r"],
        _pr_fetch_fn=lambda _repo, _days: [_merged_pr()],
        _verifier_fetch_fn=lambda _repo, _numbers: {7: marker},
        _revert_fn=lambda _pr: (False, "not reverted"),
        _now=1790000000,
    )
    assert result["verifier_verdicts_seen"] == 1
    with feedback._conn() as c:
        row = c.execute("SELECT verifier_verdict FROM outcomes").fetchone()
    assert row and row[0] == "PASS"


def test_green_ci_alone_is_not_a_verdict():
    assert verifier_evidence.decision_from_pr("o/r", _evidence_pr()) is None


def test_wrong_identity_spoof_and_provider_mismatch_stay_unknown():
    assert (
        verifier_evidence.decision_from_pr("o/r", _evidence_pr(_decision(), head="c" * 40)) is None
    )
    assert (
        verifier_evidence.decision_from_pr("o/r", _evidence_pr(_decision(), author="stranske"))
        is None
    )
    assert (
        verifier_evidence.decision_from_pr("o/r", _evidence_pr(_decision("PASS", ci_failed=True)))
        is None
    )
    assert (
        verifier_evidence.decision_from_pr(
            "o/r", _evidence_pr(_decision("PASS", providers=["PASS", "CONCERNS"]))
        )
        is None
    )


def test_late_verifier_verdict_updates_already_durable_row(monkeypatch, tmp_path):
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "brain.db")
    feedback.record_run("run-7", "o/r#7", "implement", "codex", source="keepalive")
    feedback.record_outcome("run-7", merged=True, durability="durable")
    marker = verifier_evidence.decision_from_pr(
        "o/r", _evidence_pr(_decision("NON_PASS", providers=["CONCERNS"]))
    )
    assert marker
    result = durability_sweep.sweep_durability(
        _state_fn=lambda _target: None,
        _verifier_fetch_fn=lambda _repo, _numbers: {7: marker},
    )
    assert result["verifier_checked"] == result["verifier_recorded"] == 1
    with feedback._conn() as c:
        row = c.execute("SELECT verifier_verdict,durability FROM outcomes").fetchone()
    assert row == ("NON_PASS", "durable")
    assert not feedback._is_success("durable", "PASS", "NON_PASS")
