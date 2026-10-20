"""`capability_effectiveness` reads what became of a run from `outcomes`, and sets aside what no
learner may score.

THE DEFECT (2026-10-11). `_edge_rows` read `outcome_verdict` and `durability` from the influence
edge, a copy of the target's `outcomes` row that nothing keeps current, and `_arm_stats` never read
`outcomes.failure_class`. On a copy of the live Brain after #525, all 22 of role-triage's
accepted-arm terminal outcomes and 27 of offload's 73 were in
`feedback.LEARNING_EXCLUDED_FAILURE_CLASSES`, as were all 4 counterfactual ones. 2 edges each still
read PASS/durable on outcomes a relabel had since cleared: role-triage's whole durable signal.

THE RULE THIS PINS. Verdict and durability come from `outcomes` when the report is made. A target
whose class is in the one set is counted in `excluded`, in either arm, and never scored. A
arm whose outcomes are all excluded is `excluded_only`; unresolved nonexcluded attempts stay
visible as `not_yet_measurable`. Waiting will not change the excluded subset.

Rows are written by the production writers on a controlled clock. An accepted edge is written by
`record_capability_consumption`, as `capability_lifecycle` does, so each run carries exactly one; the
control arm comes from the bridge and the triage grader (#525). The one other write is the relabel,
an UPDATE of `outcomes` that bypasses `record_outcome`: that is what the 2026-10-04 relabel
migrations amount to (they changed 53 outcomes and refreshed none of their edges), and what
`durability_sweep`'s truncated-recheck restore does."""

from __future__ import annotations

import time

import pytest

import capabilities
import capability_effectiveness as effectiveness
import capability_outcome_bridge as bridge
import feedback
import outcomes

T0 = 1_786_516_882  # 2026-08-12T06:41:22Z
TICK_META = {"status": "shadow_only", "disagreement": True}  # what tick.remote_tick passes
VERSIONS = {"role-triage": "cv:triage", "role-prompt": "cv:prompt", "offload": "cv:offload"}
EDGES = (
    "SELECT outcome_verdict, durability FROM influence_edges "
    "WHERE influence_type='capability' AND capability_id=? AND target_run_id=?"
)


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
    capabilities.save(rows, ledger)
    return ledger


def _runs(n: int, role: str) -> tuple[str, str]:
    """A role run whose proposal an offload produced, and the delegation it advised."""
    role_run, run = f"role:{role}:gemini:{n}", f"remote:o/r#{n}:gemini"
    feedback.record_role_run(role_run, role, "snapshot", "gemini", backend_run_id="offload:1")
    feedback.record_run(run, f"o/r#{n}", "implement", "gemini", mode="remote", pr_number=n)
    return role_run, run


def _consumed(n: int, cap_id: str = "role-triage", **outcome) -> str:
    """What `capability_lifecycle` records when a capability's output is accepted into a run."""
    run = f"remote:o/r#{n}:gemini"
    feedback.record_run(run, f"o/r#{n}", "implement", "gemini", mode="remote", pr_number=n)
    feedback.record_capability_consumption(
        capability_id=cap_id,
        capability_version_id=VERSIONS[cap_id],
        source_run_id=f"capability-producer:{cap_id}:{n}",
        target_run_id=run,
        accepted=True,
        receipt_ref="fixture:consumed-output",
    )
    if outcome:
        feedback.record_outcome(run, **outcome)
    return run


def _overruled(n: int, **outcome) -> str:
    """A delegation that went ahead against its triage role: the control arm (#525)."""
    role_run, run = _runs(n, "triage")
    feedback.record_influence_edge(
        target_run_id=run,
        influence_type="role",
        influence_id=role_run,
        source_run_id=role_run,
        accepted=False,
        metadata=TICK_META,
    )
    feedback.record_outcome(run, **outcome)
    outcomes.backfill_triage_disagreements(_state_fn=lambda target: {"state": "CLOSED"})
    bridge.backfill_role_capability_edges()
    return run


def _relabel(run: str, **fields) -> None:
    sets = ", ".join(f"{name}=?" for name in fields)
    with feedback._conn() as c:
        c.execute(f"UPDATE outcomes SET {sets} WHERE run_id=?", (*fields.values(), run))


def _edge(cap_id: str, run: str) -> tuple:
    with feedback._conn() as c:
        return c.execute(EDGES, (cap_id, run)).fetchone()


def _report(ledger) -> dict:
    return effectiveness.measure(path=ledger)


def _row(rep: dict, cap_id: str) -> dict:
    return next(row for row in rep["rows"] if row["capability_id"] == cap_id)


@pytest.mark.parametrize("failure_class", sorted(feedback.LEARNING_EXCLUDED_FAILURE_CLASSES))
def test_an_excluded_outcome_is_counted_never_scored(brain, failure_class):
    run = _consumed(
        1, adjudicated_verdict="FAIL", merged=0, durability="abandoned", failure_class=failure_class
    )
    # Recorded through `record_outcome`, so the edge was refreshed: only the class differs.
    assert _edge("role-triage", run) == ("FAIL", "abandoned")
    arm = _row(_report(brain), "role-triage")["accepted"]
    assert (arm["attributed"], arm["terminal"], arm["durable"]) == (1, 0, 0), arm
    assert (arm["excluded"], arm["excluded_subjects"]) == (1, 1), arm
    assert arm["status"].startswith("excluded_only"), arm["status"]


@pytest.mark.parametrize("failure_class", [None, "", "scope_mismatch"])
def test_a_failure_outside_the_set_is_still_scored(brain, failure_class):
    # "" is deliberately absent from the set: an unclassified FAIL is an attributed verdict.
    _consumed(
        1, adjudicated_verdict="FAIL", merged=0, durability="abandoned", failure_class=failure_class
    )
    arm = _row(_report(brain), "role-triage")["accepted"]
    assert (arm["terminal"], arm["durable"], arm["excluded"]) == (1, 0, 0), arm


def test_the_one_set_is_read_when_the_report_is_made(brain, monkeypatch):
    # No private copy: a class added to feedback's set is excluded here without touching this module.
    monkeypatch.setattr(
        feedback,
        "LEARNING_EXCLUDED_FAILURE_CLASSES",
        feedback.LEARNING_EXCLUDED_FAILURE_CLASSES | {"a_class_added_later"},
    )
    _consumed(
        1,
        adjudicated_verdict="FAIL",
        merged=0,
        durability="abandoned",
        failure_class="a_class_added_later",
    )
    arm = _row(_report(brain), "role-triage")["accepted"]
    assert (arm["terminal"], arm["excluded"]) == (0, 1), arm


def test_a_relabel_the_edge_never_saw_is_read_from_the_outcome(brain):
    run = _consumed(1, adjudicated_verdict="PASS", merged=1, durability="durable")
    before = _row(_report(brain), "role-triage")["accepted"]
    assert (before["terminal"], before["durable"]) == (1, 1), before
    # The 2026-10-04 relabel: no verdict, abandoned, a class no learner scores.
    _relabel(
        run,
        adjudicated_verdict=None,
        durability="abandoned",
        failure_class=feedback.UNATTRIBUTED_DELEGATION,
    )
    assert _edge("role-triage", run) == ("PASS", "durable"), "the edge never hears of it"
    after = _row(_report(brain), "role-triage")["accepted"]
    assert (after["terminal"], after["durable"], after["excluded"]) == (0, 0, 1), after


def test_a_durability_change_the_edge_never_saw_is_read_from_the_outcome(brain):
    # Not excluded, so this isolates the source: `durability_sweep`'s truncated-recheck restore
    # writes durability this way.
    run = _consumed(1, adjudicated_verdict="PASS", merged=1, durability="durable")
    _relabel(run, durability="reverted")
    assert _edge("role-triage", run) == ("PASS", "durable")
    arm = _row(_report(brain), "role-triage")["accepted"]
    assert (arm["terminal"], arm["durable"], arm["excluded"]) == (1, 0, 0), arm


def test_the_control_arm_resolves_when_its_outcome_does(brain):
    # A merge stays `pending` until the durability sweep judges it, and the grader writes a
    # counterfactual edge once. Read from the edge, this control-arm outcome stays unresolved
    # forever, however the merge turns out.
    run = _overruled(1, adjudicated_verdict="PASS", merged=1, durability="pending")
    assert _edge("role-triage", run) == ("PASS", "pending")
    before = _row(_report(brain), "role-triage")["counterfactual"]
    assert (before["attributed"], before["durable"]) == (1, 0), before
    feedback.record_outcome(run, durability="durable")  # the sweep's verdict
    assert _edge("role-triage", run) == ("PASS", "pending"), "propagation is accepted-only"
    after = _row(_report(brain), "role-triage")["counterfactual"]
    assert (after["terminal"], after["durable"], after["durable_subjects"]) == (1, 1, 1), after


def test_an_excluded_outcome_in_the_control_arm_is_counted_never_scored(brain):
    run = _overruled(
        1,
        adjudicated_verdict="FAIL",
        merged=0,
        durability="abandoned",
        failure_class=feedback.UNATTRIBUTED_DELEGATION,
    )
    assert _edge("role-triage", run) == ("UNATTRIBUTED", "abandoned")
    arm = _row(_report(brain), "role-triage")["counterfactual"]
    assert (arm["attributed"], arm["terminal"], arm["excluded"]) == (1, 0, 1), arm
    assert arm["status"].startswith("excluded_only"), arm["status"]


def test_excluded_only_is_not_awaiting_outcomes(brain):
    _consumed(
        1,
        adjudicated_verdict="FAIL",
        merged=0,
        durability="abandoned",
        failure_class=feedback.UNATTRIBUTED_CLOSING_PR,
    )
    _consumed(2, "role-prompt")  # attributed, nothing recorded yet
    rep = _report(brain)
    assert _row(rep, "role-prompt")["verdict"] == "not_yet_measurable"
    assert _row(rep, "role-triage")["verdict"].startswith("excluded_only")
    assert "role-prompt" in rep["awaiting_outcomes"]
    assert "role-triage" not in rep["awaiting_outcomes"]
    assert "role-triage" in rep["excluded_only"]
    text = effectiveness.format_report(rep)
    assert "1 excl" in text, text
    assert "every outcome is learning-excluded" in text, text


def test_a_small_sample_names_what_it_set_aside(brain):
    _consumed(
        1,
        adjudicated_verdict="FAIL",
        merged=0,
        durability="abandoned",
        failure_class=feedback.UNATTRIBUTED_DELEGATION,
    )
    _consumed(2, adjudicated_verdict="PASS", merged=1, durability="durable")
    status = _row(_report(brain), "role-triage")["accepted"]["status"]
    assert status == (
        "insufficient_evidence (1/5 distinct subjects from 1 attempts; 1 learning-excluded)"
    ), status


def test_excluded_accepted_arm_does_not_claim_the_measured_control_is_unscored(brain):
    _consumed(
        1,
        adjudicated_verdict="FAIL",
        merged=0,
        durability="abandoned",
        failure_class=feedback.UNATTRIBUTED_CLOSING_PR,
    )
    for n in range(2, 7):
        _overruled(n, adjudicated_verdict="PASS", merged=1, durability="durable")
        # Role-run associations are separate accepted edges; exclude their inherited outcomes.
        feedback.record_outcome(
            f"role:triage:gemini:{n}", failure_class=feedback.UNATTRIBUTED_CLOSING_PR
        )
    rep = _report(brain)
    row = _row(rep, "role-triage")
    assert row["counterfactual"]["durable_rate"] == 1.0
    text = effectiveness.format_report(rep)
    assert "Accepted arms resolved, but every outcome is learning-excluded" in text
    assert "so that arm is not scored" in text


def test_excluded_outcome_does_not_hide_unresolved_accepted_attempts(brain):
    _consumed(
        1,
        adjudicated_verdict="FAIL",
        merged=0,
        durability="abandoned",
        failure_class=feedback.UNATTRIBUTED_CLOSING_PR,
    )
    _consumed(2)
    rep = _report(brain)
    arm = _row(rep, "role-triage")["accepted"]
    assert arm["excluded"] == 1
    assert arm["status"] == "not_yet_measurable"
    assert "role-triage" in rep["awaiting_outcomes"]
    assert "role-triage" not in rep["excluded_only"]
