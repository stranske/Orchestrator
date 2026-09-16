"""The advisor evaluates PR-fact preconditions itself and records the decline with judge=machine.

Measured 2026-09-04..15: ~4,900 lane offers, 2,030 hand-written decline reasons — `precondition_unmet`
~700 for capabilities whose trigger condition (a stalled worker, a multi-repo change, a repeated
pattern) was absent every time, `scope_too_small` ~1,000 for capabilities offered on one-line PRs. The
entry is never removed or re-ordered (prioritise, never conceal); an unevaluable fact never declines.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import capabilities
import capability_advisor as ca

CLOSER_BOUND = (
    "adversarial-review",
    "runtime-ac-checks",
    "cross-repo-coordination",
    "offload",
    "redirect-policy",
    "redirect-plan",
)
# Bound on the opener, not the closer: present in the ledger so the closer consult proves it is
# neither offered nor declined there.
OTHER = ("deliberate-break-verifier",)


def _ledger(tmp_path: Path) -> Path:
    path = tmp_path / "capabilities.json"
    rows = {}
    for name in CLOSER_BOUND + OTHER:
        row = capabilities._blank_capability(name)
        row["status"] = "generated"
        rows[name] = row
    capabilities.save(rows, path)
    return path


SMALL_QUIET_PR = {
    "number": 1234,
    "labels": ["verify:compare"],
    "changedFiles": 1,
    "additions": 3,
    "deletions": 1,
    "title": "fix: guard",
}
BIG_STALLED_PR = {
    "number": 1234,
    "labels": ["agent:retry", "agents:keepalive"],
    "changedFiles": 7,
    "additions": 240,
    "deletions": 30,
    "title": "feat: pipeline",
}


@pytest.fixture
def facts(monkeypatch):
    holder = {"facts": SMALL_QUIET_PR, "calls": []}

    def fetch(repository, pr):
        holder["calls"].append((repository, pr))
        return holder["facts"]

    monkeypatch.setattr(ca, "PR_FACTS_FETCH", fetch)
    return holder


def _consult(tmp_path, text="closer: stranske/Repo#1234 merge and verify", **kw):
    return ca.advise(
        text,
        surface="closer-lane",
        repository="stranske/Repo",
        record=False,
        path=_ledger(tmp_path),
        **kw,
    )


def _by_id(result):
    return {e["capability_id"]: e for e in result["capabilities"]}


def test_small_quiet_pr_auto_declines_by_kind(tmp_path, facts):
    result = _consult(tmp_path)
    assert facts["calls"] == [("stranske/Repo", 1234)]  # the PR came from the task text, read once
    e = _by_id(result)
    for cid in ("runtime-ac-checks", "adversarial-review", "offload"):
        assert e[cid]["auto_declined"]["kind"] == "scope_too_small", cid
        assert "1 file(s), 4 changed line(s)" in e[cid]["auto_declined"]["reason"]
    for cid in ("redirect-policy", "redirect-plan"):
        assert e[cid]["auto_declined"]["kind"] == "precondition_unmet", cid
        assert "no stall signal" in e[cid]["auto_declined"]["reason"]
    assert e["cross-repo-coordination"]["auto_declined"]["kind"] == "precondition_unmet"
    assert "deliberate-break-verifier" not in e  # not bound here, so nothing to decline
    assert e["runtime-ac-checks"]["how_to_use"].startswith(
        "AUTO-DECLINED by the advisor (scope_too_small)"
    )
    assert result["precondition"]["pr"] == 1234
    assert set(result["precondition"]["auto_declined"]) == {
        "runtime-ac-checks",
        "adversarial-review",
        "offload",
        "redirect-policy",
        "redirect-plan",
        "cross-repo-coordination",
    }


def test_big_stalled_pr_is_offered_plainly(tmp_path, facts):
    facts["facts"] = BIG_STALLED_PR
    e = _by_id(_consult(tmp_path))
    for cid in (
        "runtime-ac-checks",
        "adversarial-review",
        "offload",
        "redirect-policy",
        "redirect-plan",
    ):
        assert "auto_declined" not in e[cid], cid
    assert e["redirect-policy"]["pr_requirement_evidence"].startswith(
        "stall signal present: agent:retry"
    )
    # a single PR is still one repository: cross-repo stays auto-declined unless the task names two
    assert e["cross-repo-coordination"]["auto_declined"]["kind"] == "precondition_unmet"


def test_nothing_is_removed_or_reordered(tmp_path, facts):
    declined = [e["capability_id"] for e in _consult(tmp_path)["capabilities"]]
    facts["facts"] = BIG_STALLED_PR
    plain = [e["capability_id"] for e in _consult(tmp_path)["capabilities"]]
    assert declined == plain and set(CLOSER_BOUND) <= set(plain)


def test_no_pr_means_unevaluated_not_declined(tmp_path, facts):
    result = _consult(tmp_path, text="closer: sweep the merge queue")
    assert facts["calls"] == []
    e = _by_id(result)
    assert not any("auto_declined" in v for v in e.values())
    assert "pr" in result["precondition"]["missing_inputs"]
    assert any("needs `pr`" in why for why in e["redirect-policy"]["unevaluated_because"])


def test_fetch_failure_declines_nothing(tmp_path, facts):
    facts["facts"] = None
    e = _by_id(_consult(tmp_path))
    assert not any("auto_declined" in v for v in e.values())


def test_explicit_pr_context_wins_over_text(tmp_path, facts):
    _consult(tmp_path, text="closer: stranske/Repo#99", context={"pr": 1234})
    assert facts["calls"] == [("stranske/Repo", 1234)]


def test_auto_declines_are_recorded_once_with_judge_machine(tmp_path, facts):
    ledger = _ledger(tmp_path)
    kw = dict(surface="closer-lane", repository="stranske/Repo", record=True, path=ledger)
    first = ca.advise("closer: stranske/Repo#1234 merge and verify", **kw)
    assert first["recorded_auto_declines"] == 6
    second = ca.advise("closer: stranske/Repo#1234 merge and verify", **kw)
    assert second["recorded_auto_declines"] == 0  # idempotent per (capability, experiment)
    row = capabilities.load(ledger)["redirect-policy"]
    declines = [
        ev
        for ev in row["event_history"]
        if isinstance(ev, dict) and (ev.get("metadata") or {}).get("decline_kind")
    ]
    assert len(declines) == 1
    md = declines[0]["metadata"]
    assert md["judge"] == "machine" and md["auto_declined"] is True
    assert md["decline_kind"] == "precondition_unmet" and md["surface"] == "closer-lane"


def test_deliberate_break_without_evaluation_nothing_declines(tmp_path, facts, monkeypatch):
    monkeypatch.setattr(ca, "PR_FACT_PROBES", {})
    e = _by_id(_consult(tmp_path))
    assert not any("auto_declined" in v for v in e.values())
