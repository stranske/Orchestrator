"""The redirect-apply bootstrap is a reviewed switch, and its idle row says what could drain it.

THE GAP (2026-10-02). orchestrate.sh has exported ORCH_REDIRECT_APPLY_BOOTSTRAP=1 by default since
2026-08-21, and its switch-on criterion sat in `capability_recurrence_check.SWITCH_ON_CRITERIA`. But
`switch_review.SWITCH_CAPABILITY`, the table the weekly review walks, never named it, so the review
never examined a switch that authorised nothing for 42 days: 759 metered judgements, every one
`wait`, the Stage-2 deficits pinned at 5 and 3. Two tables described one set of switches and nothing
held them together.

AND "ON, IDLE" READS AS PATIENCE. A gate must report its drainable quantity beside its blocking one,
because "ON, deficits 5/3, drainable 0" is a deadlock and "ON, idle" is not visibly one. So the
bootstrap's idle row carries `redirect_apply.status()`'s deficits and drainable count, read with
explicit inputs and read-only, while `switch_states()` stays what its docstring promises: local
reads, no gh, no heartbeat, and no Brain.

Every test reads a sandbox (a private ledger, corpus, stage-2 plan, report directory and claims
directory) behind tripwires that RECORD a gh call, a heartbeat or a Brain connection, because the
code under test swallows exceptions and a raising tripwire could be silenced.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import capabilities
import capability_propensity
import capability_recurrence_check as rc
import feedback
import redirect_apply
import redirect_shadow
import switch_review

FLAG = redirect_apply.BOOTSTRAP_FLAG
NOW = 1_800_000_000
DAY = 86400
# A keepalive lane exactly as the supervisor writes one: NO pid, and its own word that it stalled.
STALLED = {
    "target": "stranske/Fixture#1",
    "agent": "keepalive",
    "lane": "closer",
    "state": "stalled",
    "recommended_action": "inspect",
}
NOTHING_REACHED = {"gh": [], "heartbeat": [], "brain": []}


def _one_row(rows: list[dict], flag: str = FLAG) -> dict:
    matching = [row for row in rows if row["flag"] == flag]
    assert len(matching) == 1, rows
    return matching[0]


def _plan(path: Path, reports: list[dict], *, age_s: int = 3600) -> None:
    """A stage-2 plan in the shape keepalive_supervisor writes: one eligible plan per report."""
    plans = [
        {"target": rep["target"], "eligible": True, "report": rep, "acceptance_criteria": "AC"}
        for rep in reports
    ]
    payload = {"generated_at": NOW - age_s, "plans": plans}
    path.write_text(json.dumps(payload), encoding="utf-8")


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()
    }


def _save_ledger(path: Path, *, bootstrap_last_invocation: int) -> None:
    """A private ledger holding every reviewed switch's capability, the bootstrap's invocation set.

    The bootstrap's row is added by its OWN module's name, not taken from the map under test, so a
    map that lost the switch fails the tests' assertions rather than this fixture.
    """
    ids = set(switch_review.SWITCH_CAPABILITY.values()) | {redirect_apply.CAPABILITY_ID}
    rows = {c: capabilities._blank_capability(c) for c in sorted(ids)}
    rows[redirect_apply.CAPABILITY_ID]["last_invocation"] = bootstrap_last_invocation
    capabilities.save(rows, path)


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    corpus = tmp_path / "corpus.jsonl"
    plan = tmp_path / "stage2-plan.json"
    reports = tmp_path / "reports"
    reports.mkdir()
    ledger = tmp_path / "capabilities.json"
    # Idle exactly as the live row is: an old invocation, and nothing since.
    _save_ledger(ledger, bootstrap_last_invocation=NOW - 30 * DAY)
    for flag in switch_review.SWITCH_CAPABILITY:
        monkeypatch.delenv(flag, raising=False)
    monkeypatch.delenv("ORCH_CAPABILITY_HEARTBEATS", raising=False)
    monkeypatch.setenv("HANDOFF_DIR", str(tmp_path / "handoff"))  # claims: none held
    monkeypatch.setattr(
        switch_review,
        "_redirect_bootstrap_inputs",
        lambda: {"corpus_path": corpus, "plan_path": plan, "report_dir": reports},
    )
    reached: dict[str, list] = {"gh": [], "heartbeat": [], "brain": []}

    def gh(args, *, timeout_s=30):
        reached["gh"].append(args)
        return False, "", "unmeasured: this test has no network"

    def heartbeat(*args, **_kwargs):
        reached["heartbeat"].append(args)

    def brain(*_args, **_kwargs):
        reached["brain"].append("feedback._conn")
        raise AssertionError("the bootstrap's drain opened the Brain")

    monkeypatch.setattr(switch_review, "_GH_CALL_RUNNER", gh)
    monkeypatch.setattr(switch_review, "_capability_heartbeat", heartbeat)
    monkeypatch.setattr(feedback, "_conn", brain)
    return SimpleNamespace(
        root=tmp_path,
        corpus=corpus,
        plan=plan,
        reports=reports,
        ledger=ledger,
        reached=reached,
    )


def _idle_row(sandbox) -> dict:
    rows = switch_review.switch_states(now=NOW, env={FLAG: "1"}, path=sandbox.ledger)
    return _one_row(rows["on_but_idle"])


def test_the_criteria_table_and_the_review_map_name_the_same_switches():
    """The defect itself: a switch with a recorded switch-on criterion that nothing re-raises."""
    criteria, reviewed = set(rc.SWITCH_ON_CRITERIA), set(switch_review.SWITCH_CAPABILITY)
    assert not criteria - reviewed, (
        f"{sorted(criteria - reviewed)} carry a switch-on criterion that the weekly review never "
        "examines: map each in switch_review.SWITCH_CAPABILITY"
    )
    assert not reviewed - criteria, (
        f"{sorted(reviewed - criteria)} are reviewed with no recorded switch-on criterion: add one "
        "to capability_recurrence_check.SWITCH_ON_CRITERIA"
    )


def test_the_bootstrap_is_reviewed_under_the_names_its_own_module_declares():
    assert switch_review.SWITCH_CAPABILITY.get(FLAG) == redirect_apply.CAPABILITY_ID
    assert FLAG in switch_review.SWITCH_DRAIN


def test_an_idle_bootstrap_row_carries_its_deficits_beside_a_measured_zero(sandbox):
    _plan(sandbox.plan, [])
    before = _snapshot(sandbox.root)
    row = _idle_row(sandbox)
    drain = row["drain"]
    # A COUNT, asserted as one: 0 is the finding here, and a truthiness check would forbid it.
    assert drain["drainable"] is not None and drain["drainable"] == 0, drain
    assert (drain["current_candidates"], drain["population"]) == (0, "current"), drain
    gate = drain["gate"]
    assert (gate["synced_needed"], gate["disagreements_needed"]) == (10, 3), gate
    assert gate["bootstrap_needed"] is True, gate
    summary = drain["summary"]
    assert "Stage-2 deficits 10/3" in summary and "drainable 0 of 0" in summary, summary
    assert "deadlock" in summary, summary
    assert drain["inputs"] == {
        "corpus_path": str(sandbox.corpus),
        "plan_path": str(sandbox.plan),
        "report_dir": str(sandbox.reports),
    }, drain["inputs"]
    # Read-only and cheap: nothing written in the sandbox, no gh, no heartbeat, no Brain.
    assert _snapshot(sandbox.root) == before
    assert sandbox.reached == NOTHING_REACHED, sandbox.reached


def test_a_current_candidate_that_passes_the_free_screen_is_counted(sandbox):
    """A positive count: the number is read from the screen, not assumed to be zero."""
    _plan(sandbox.plan, [STALLED])
    drain = _idle_row(sandbox)["drain"]
    assert (drain["drainable"], drain["current_candidates"]) == (1, 1), drain
    assert "drainable 1 of 1" in drain["summary"], drain["summary"]
    assert "deadlock" not in drain["summary"], drain["summary"]
    assert sandbox.reached == NOTHING_REACHED, sandbox.reached
    # The sentence points the reader somewhere, and that place must name what it counts. Text-mode
    # `--screen` prints only the candidates that FAIL the screen; the JSON form lists every one.
    assert "`redirect_apply.py --screen --json`" in drain["summary"], drain["summary"]
    listed = redirect_apply.screen_candidates(
        report_dir=sandbox.reports, plan_path=sandbox.plan, corpus_path=sandbox.corpus, now=NOW
    )["candidates"]
    assert [c["target"] for c in listed if c["passes_screen"]] == [STALLED["target"]], listed


def test_an_unknown_population_is_never_a_zero(sandbox):
    # No plan at all: the population is unknown, and the reason names the file that was missing.
    drain = _idle_row(sandbox)["drain"]
    assert drain["drainable"] is None and drain["population"] == "missing", drain
    assert str(sandbox.plan) in drain["reason"], drain["reason"]
    assert "drainable UNKNOWN" in drain["summary"], drain["summary"]
    assert "drainable 0" not in drain["summary"], drain["summary"]
    # A plan older than its step allows is no population either, however many candidates it holds.
    _plan(sandbox.plan, [STALLED], age_s=10 * DAY)
    stale = _idle_row(sandbox)["drain"]
    assert stale["drainable"] is None and stale["population"] == "stale", stale
    assert "drainable UNKNOWN" in stale["summary"], stale["summary"]


def test_a_closed_gate_reads_finished_through_the_real_status(sandbox):
    """The DRAINED state, reached from a real input rather than only from the formatter: ten
    synced role outcomes, three of them disagreements, which is exactly what closes the gate."""
    events = []
    for i in range(redirect_shadow.LINKED_OUTCOME_TARGET):
        role_run = f"role:redirect:codex:{i}"
        events.append(
            {
                "kind": "redirect_proposal",
                "role_run_id": role_run,
                "target": f"o/r#{i}",
                "valid_proposal": True,
                "disagreement": i < redirect_shadow.DISAGREEMENT_OUTCOME_TARGET,
            }
        )
        events.append(
            {
                "kind": "redirect_outcome_link",
                "role_run_id": role_run,
                "accepted": True,
                "link_result": {"synced": True},
            }
        )
    sandbox.corpus.write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
    _plan(sandbox.plan, [STALLED])
    drain = _idle_row(sandbox)["drain"]
    assert drain["gate"]["bootstrap_needed"] is False, drain["gate"]
    assert drain["summary"].startswith("FINISHED"), drain["summary"]
    assert "the switch can go off" in drain["summary"], drain["summary"]
    assert "deadlock" not in drain["summary"] and "UNKNOWN" not in drain["summary"]


def test_a_failed_read_still_raises_the_row_and_says_unmeasured(sandbox, monkeypatch):
    """Fail toward motion: the switch is ON and idle whatever the drain read did."""

    def unreadable(*_args, **_kwargs):
        raise OSError("corpus unreadable")

    monkeypatch.setattr(redirect_apply, "status", unreadable)
    row = _idle_row(sandbox)
    assert row["state"] == "on", row
    drain = row["drain"]
    assert drain["drainable"] is None and drain["population"] == "unmeasured", drain
    assert drain["summary"].startswith("drain UNMEASURED — redirect_apply.status raised OSError")


def test_the_drain_is_read_only_for_a_switch_already_on_and_idle(sandbox, monkeypatch):
    """A consumer whose environment holds the switch off, or whose switch fired recently, pays
    nothing: the reader is consulted for an ON-and-idle row and for nothing else."""
    reads: list = []

    def counting_reader(env, *, now):
        reads.append(dict(env))
        return {"summary": "counted"}

    monkeypatch.setitem(switch_review.SWITCH_DRAIN, FLAG, counting_reader)
    for held_off in ({}, {FLAG: "0"}):
        rows = switch_review.switch_states(now=NOW, env=held_off, path=sandbox.ledger)
        assert FLAG in {r["flag"] for r in rows["held_off"]}, rows
    assert reads == [], "the drain was read for a switch that is OFF"

    _save_ledger(sandbox.ledger, bootstrap_last_invocation=NOW - DAY)
    rows = switch_review.switch_states(now=NOW, env={FLAG: "1"}, path=sandbox.ledger)
    assert FLAG not in {r["flag"] for r in rows["on_but_idle"]}, rows
    assert reads == [], "the drain was read for a switch that fired within the window"

    _save_ledger(sandbox.ledger, bootstrap_last_invocation=NOW - 30 * DAY)
    assert _idle_row(sandbox)["drain"] == {"summary": "counted"}
    assert len(reads) == 1, reads


def test_the_question_carries_the_pair_and_keeps_the_conservative_default(sandbox, monkeypatch):
    """The owner question, if one is ever raised, is answerable at a glance and asks nothing of
    silence: it auto-ratifies to the current position after the same seven days as every other."""
    _plan(sandbox.plan, [])
    rows = switch_review.switch_states(now=NOW, env={FLAG: "1"}, path=sandbox.ledger)
    recorded: list = []

    def record(question, default, **kwargs):
        recorded.append({"question": question, "default": default, **kwargs})
        return {"deduped": False}

    monkeypatch.setattr(feedback, "record_owner_question", record)
    out = switch_review.raise_questions(
        {"held_off": [], "on_but_idle": rows["on_but_idle"]}, dry_run=False
    )
    assert out["raised"] == [FLAG] and not out["errors"], out
    (asked,) = recorded
    assert _one_row(rows["on_but_idle"])["drain"]["summary"] in asked["question"], asked
    assert asked["default"] == "keep the current position; re-ask in a week", asked
    assert asked["expires_days"] == switch_review.QUESTION_EXPIRY_DAYS, asked
    assert asked["target"] == f"switch:{FLAG}", asked


def test_review_declares_its_switches_so_a_map_edit_rebaselines(sandbox, monkeypatch):
    """Adding a switch makes the next sweep list a new row because the MAP changed. Graded as a
    finding, that edit would mint a 'useful' verdict for switch-review; declared, it re-baselines.
    """
    monkeypatch.setattr(switch_review, "stale_runners", lambda **_k: [])
    monkeypatch.setattr(switch_review, "mirror_drift", lambda **_k: {"status": "ok"})
    monkeypatch.setattr(switch_review, "_exploration_gate", lambda: {"suspect": False})
    _plan(sandbox.plan, [])
    rep = switch_review.review(now=NOW, env={FLAG: "1"}, path=sandbox.ledger)
    population = capability_propensity.declared_population(rep)
    assert population == {"switches": sorted(switch_review.SWITCH_CAPABILITY)}, population
    assert FLAG in population["switches"], population
    findings = capability_propensity.project_findings("switch-review", rep)
    # The row is graded by identity alone, so the drain's numbers can never mint a verdict by moving.
    assert findings is not None, rep
    assert findings["on_but_idle"] == [f"flag={FLAG!r}|state='on'"], findings
    # An observation recorded before the report declared its population cannot be compared with it.
    prior = {
        "projection": capability_propensity._projection_signature("switch-review"),
        "findings": findings,
    }
    drift = capability_propensity._projection_drift("switch-review", prior, findings, population)
    assert drift and "declared finding population changed" in drift, drift
    # ...and once recorded, an unchanged map is comparable again, so real state changes still grade.
    settled = {**prior, "population": population}
    assert (
        capability_propensity._projection_drift("switch-review", settled, findings, population)
        is None
    )
