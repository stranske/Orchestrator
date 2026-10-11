"""`value_chain_monitor`'s `outcome` step reads what became of a run from `outcomes`, and sets aside
what no learner may score.

THE DEFECT (2026-10-11). `_brain` read `SELECT * FROM influence_edges`, and `report` counted
`outcome` from the edge's own `outcome_verdict` and `durability`: a copy of the target's `outcomes`
row that nothing keeps current, read with no `failure_class` at all. On a copy of the live Brain,
role-triage's two counted outcomes were edges still reading PASS/durable on outcomes the 2026-10-04
relabels had cleared to `unattributed_delegation` (Trend_Model_Project#5913 and #5944), and they
made role-triage the only capability whose value chain read `works`. offload counted the same two.

THE RULE THIS PINS. `_brain` replaces the edge's copied columns with the outcome's, read when the
report is made. An acted-on run whose class is in `feedback.LEARNING_EXCLUDED_FAILURE_CLASSES` is
counted in `outcome_excluded` and never in `outcome`. When EVERY acted-on run is excluded the first
break is `outcome_excluded`, not `no_outcome`: waiting will not change it, attribution has to. An
unread Brain stays unmeasured, never zero.

Rows are written by the production writers on a controlled clock: `record_run`, then
`record_capability_consumption` for the accepted edge as `capability_lifecycle` does, then
`record_outcome`. The out-of-band writes are `feedback.mark_transient_infra` itself, and an UPDATE
of `outcomes` that bypasses `record_outcome`, which is what the 2026-10-04 relabel migrations and
`durability_sweep`'s truncated-recheck restore amount to."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import time

import pytest

import adversarial
import capabilities
import feedback
import switch_review
import value_chain_monitor as monitor

T0 = 1_786_516_882  # 2026-08-12T06:41:22Z
NOW = T0 + 60
CAP = "role-triage"
VERSIONS = {CAP: "cv:triage", "offload": "cv:offload"}
EDGE = (
    "SELECT outcome_verdict, durability FROM influence_edges "
    "WHERE influence_type='capability' AND capability_id=? AND target_run_id=?"
)
OPEN_ISSUE = {"createdAt": "2026-08-12T00:00:00Z", "state": "OPEN", "body": ""}


@pytest.fixture
def brain(monkeypatch, tmp_path):
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(feedback, "_capability_daily_heartbeat", lambda *a, **k: None)
    monkeypatch.setattr(
        feedback,
        "_resolve_capability_versions",
        lambda ids: [VERSIONS[i] for i in ids] if all(i in VERSIONS for i in ids) else [],
    )
    monkeypatch.setattr(time, "time", lambda: float(T0))
    ledger = tmp_path / "capabilities.json"
    rows = {}
    for cap_id in VERSIONS:
        rows[cap_id] = capabilities._blank_capability(cap_id)
        rows[cap_id]["status"] = "shadow"
        # Invoked and usable in the window, so the first break is decided at the last two steps.
        rows[cap_id]["event_history"] = [
            {"timestamp": T0, "type": "invocation"},
            {"timestamp": T0, "type": "success"},
        ]
    capabilities.save(rows, ledger)
    return ledger


def _consumed(n: int, cap_id: str = CAP, **outcome) -> str:
    """What `capability_lifecycle` records when a capability's output is accepted into a run."""
    run = f"remote:o/r#{n}:gemini"
    feedback.record_run(run, f"o/r#{n}", "implement", "gemini", mode="remote", pr_number=n)
    feedback.record_capability_consumption(
        capability_id=cap_id,
        capability_version_id=VERSIONS[cap_id],
        source_run_id=f"capability-producer:{cap_id}:{n}",
        target_run_id=run,
        accepted=True,
    )
    if outcome:
        feedback.record_outcome(run, **outcome)
    return run


def _relabel(run: str, **fields) -> None:
    sets = ", ".join(f"{name}=?" for name in fields)
    with feedback._conn() as c:
        c.execute(f"UPDATE outcomes SET {sets} WHERE run_id=?", (*fields.values(), run))


def _edge(run: str, cap_id: str = CAP) -> tuple:
    with feedback._conn() as c:
        return c.execute(EDGE, (cap_id, run)).fetchone()


def _section(ledger, db=None) -> dict:
    inputs = monitor._brain(db or feedback.DB_PATH, NOW - monitor.WINDOW_DAYS * 86400, NOW)
    inputs["fleet_issues"] = [OPEN_ISSUE]  # role-triage's demand: open issues
    inputs["cadence_inputs"] = {"offload": 1}  # offload declares no situation query
    return monitor.report(now=NOW, path=ledger, env={}, inputs=inputs)


def _row(ledger, cap_id: str = CAP, db=None) -> dict:
    return next(r for r in _section(ledger, db)["rows"] if r["capability"] == cap_id)


def _steps(row: dict) -> tuple:
    return row["acted_on"], row["outcome"], row["outcome_excluded"], row["first_break"]


@pytest.mark.parametrize("failure_class", sorted(feedback.LEARNING_EXCLUDED_FAILURE_CLASSES))
def test_an_excluded_outcome_is_counted_never_scored(brain, failure_class):
    # Recorded through `record_outcome`, so the edge was refreshed: only the class differs, and a
    # durable PASS is the strongest outcome there is.
    run = _consumed(
        1, adjudicated_verdict="PASS", merged=1, durability="durable", failure_class=failure_class
    )
    assert _edge(run) == ("PASS", "durable")
    assert _steps(_row(brain)) == (1, 0, 1, "outcome_excluded")


@pytest.mark.parametrize("failure_class", [None, "", "scope_mismatch"])
def test_an_outcome_outside_the_set_is_still_scored(brain, failure_class):
    # "" is deliberately absent from the set: an unclassified FAIL is an attributed verdict.
    _consumed(
        1,
        adjudicated_verdict="FAIL",
        merged=1,
        durability="broke_later",
        failure_class=failure_class,
    )
    assert _steps(_row(brain)) == (1, 1, 0, "works")


def test_the_one_set_is_read_when_the_report_is_made(brain, monkeypatch):
    # No private copy: a class added to feedback's set is excluded here without touching this module.
    monkeypatch.setattr(
        feedback,
        "LEARNING_EXCLUDED_FAILURE_CLASSES",
        feedback.LEARNING_EXCLUDED_FAILURE_CLASSES | {"a_class_added_later"},
    )
    _consumed(
        1,
        adjudicated_verdict="PASS",
        merged=1,
        durability="durable",
        failure_class="a_class_added_later",
    )
    assert _steps(_row(brain)) == (1, 0, 1, "outcome_excluded")


def test_a_relabel_the_edge_never_saw_is_read_from_the_outcome(brain):
    run = _consumed(1, adjudicated_verdict="PASS", merged=1, durability="durable")
    assert _steps(_row(brain)) == (1, 1, 0, "works")
    # The 2026-10-04 relabel, as it landed on Trend_Model_Project#5913 and #5944: no verdict,
    # abandoned, a class no learner scores.
    _relabel(
        run,
        adjudicated_verdict=None,
        durability="abandoned",
        failure_class=feedback.UNATTRIBUTED_DELEGATION,
    )
    assert _edge(run) == ("PASS", "durable"), "the edge never hears of it"
    assert _steps(_row(brain)) == (1, 0, 1, "outcome_excluded")


def test_mark_transient_infra_is_read_without_refreshing_the_edge(brain):
    run = _consumed(1, adjudicated_verdict="FAIL", merged=0, durability="reopened")
    assert _steps(_row(brain)) == (1, 1, 0, "works")
    assert feedback.mark_transient_infra(run, "agent killed by signal")
    assert _edge(run) == ("FAIL", "reopened"), "the writer updates `outcomes` alone"
    assert _steps(_row(brain)) == (1, 0, 1, "outcome_excluded")


@pytest.mark.parametrize(
    "before, after, counted",
    [("pending", "durable", (0, 1)), ("durable", "pending", (1, 0))],
)
def test_a_durability_change_the_edge_never_saw_is_read_from_the_outcome(
    brain, before, after, counted
):
    # No class involved, so this isolates the SOURCE: `durability_sweep`'s truncated-recheck
    # restore writes durability this way.
    run = _consumed(1, adjudicated_verdict="PASS", merged=1, durability=before)
    assert _row(brain)["outcome"] == counted[0]
    _relabel(run, durability=after)
    assert _edge(run) == ("PASS", before)
    row = _row(brain)
    assert (row["outcome"], row["outcome_excluded"]) == (counted[1], 0), row


def test_excluded_only_is_its_own_first_break(brain):
    excluded = {
        "adjudicated_verdict": None,
        "merged": 0,
        "durability": "abandoned",
        "failure_class": feedback.UNATTRIBUTED_DELEGATION,
    }
    _consumed(1, **excluded)
    pending = _consumed(2)  # acted on, no outcome yet: waiting CAN still change this one
    assert _steps(_row(brain)) == (2, 0, 1, "no_outcome")
    feedback.record_outcome(pending, **excluded)
    assert _steps(_row(brain)) == (2, 0, 2, "outcome_excluded")
    _consumed(3, adjudicated_verdict="PASS", merged=1, durability="durable")
    assert _steps(_row(brain)) == (3, 1, 2, "works")


def test_other_capabilities_and_arms_are_untouched(brain):
    _consumed(
        1,
        adjudicated_verdict="PASS",
        merged=1,
        durability="durable",
        failure_class="transient_infra",
    )
    _consumed(2, cap_id="offload", adjudicated_verdict="PASS", merged=1, durability="durable")
    assert _steps(_row(brain, "offload")) == (1, 1, 0, "works")
    assert _steps(_row(brain)) == (1, 0, 1, "outcome_excluded")


def test_an_unread_outcome_table_is_unmeasured_never_the_edge_copy(brain, tmp_path):
    _consumed(1, adjudicated_verdict="PASS", merged=1, durability="durable")
    broken = tmp_path / "no-outcomes.db"
    shutil.copy(feedback.DB_PATH, broken)
    with sqlite3.connect(broken) as c:
        c.execute("DROP TABLE outcomes")
    section = _section(brain, broken)
    row = next(r for r in section["rows"] if r["capability"] == CAP)
    # The edge still reads PASS/durable, and that copy must not stand in for the outcome.
    assert _steps(row) == (None, None, None, None), row
    assert any("Brain evidence unavailable" in e for e in section["errors"])
    text = "\n".join(monitor.format_lines(section))
    assert "outcome None; first_break unmeasured" in text
    assert "excluded" not in text


def test_the_report_prints_the_excluded_count_and_writes_nothing(brain):
    _consumed(
        1,
        adjudicated_verdict="PASS",
        merged=1,
        durability="durable",
        failure_class=feedback.UNATTRIBUTED_CLOSING_PR,
    )
    _consumed(2, cap_id="offload", adjudicated_verdict="PASS", merged=1, durability="durable")
    before = hashlib.sha256(feedback.DB_PATH.read_bytes()).hexdigest()
    ledger_before = brain.read_bytes()
    lines = monitor.format_lines(_section(brain))
    assert hashlib.sha256(feedback.DB_PATH.read_bytes()).hexdigest() == before
    assert brain.read_bytes() == ledger_before
    triage = next(line for line in lines if line.startswith(f"  {CAP}:"))
    assert "outcome 0 (+1 excluded); first_break outcome_excluded" in triage, triage
    offload = next(line for line in lines if line.startswith("  offload:"))
    assert "outcome 1; first_break works" in offload, offload
    assert sum("LEARNING_EXCLUDED_FAILURE_CLASSES" in line for line in lines) == 1
    # Nothing set aside anywhere: no count and no footnote.
    _relabel("remote:o/r#1:gemini", failure_class=None)
    assert not any("excluded" in line for line in monitor.format_lines(_section(brain)))


def test_the_weekly_consumer_carries_the_count(brain, monkeypatch):
    """The real caller: `switch_review.main` collects inputs, `review` builds the section, and
    `format_report` prints it into the weekly artifact."""
    _consumed(
        1,
        adjudicated_verdict="PASS",
        merged=1,
        durability="durable",
        failure_class=feedback.UNATTRIBUTED_DELEGATION,
    )
    monkeypatch.setenv("ORCH_CREDENTIAL_EXPOSURE_SCAN", "0")
    monkeypatch.setenv(
        "ORCH_DISABLE_STEPS", os.environ.get("ORCH_DISABLE_STEPS", "") + ",issue-size-quality"
    )
    monkeypatch.setattr(adversarial, "record_shape_measurement", lambda *a, **kw: False)
    monkeypatch.setattr(
        switch_review,
        "adversarial_shape_population",
        lambda **kw: {"status": "unknown", "reason": "unit-test fixture"},
    )
    for name, value in (
        ("switch_states", lambda **kw: {"held_off": [], "on_but_idle": []}),
        ("stale_runners", lambda: []),
        ("mirror_drift", lambda: {"status": "ok"}),
        ("fleet_gates", lambda **kw: {}),
        ("_exploration_gate", lambda: {}),
        ("gate_expiry", lambda **kw: {}),
    ):
        monkeypatch.setattr(switch_review, name, value)

    def gh(args):
        page = {"nodes": [{**OPEN_ISSUE, "number": 1}], "pageInfo": {"hasNextPage": False}}
        return True, json.dumps({"data": {"repository": {"issues": page}}}), ""

    inputs = monitor.collect_inputs(now=NOW, gh_fn=gh, repos=["o/r"], db=feedback.DB_PATH)
    rep = switch_review.review(now=NOW, env={}, path=brain, value_chain_inputs=inputs)
    row = next(r for r in rep["value_chain"]["rows"] if r["capability"] == CAP)
    assert _steps(row) == (1, 0, 1, "outcome_excluded"), row
    text = switch_review.format_report(rep)
    assert "outcome 0 (+1 excluded); first_break outcome_excluded" in text
