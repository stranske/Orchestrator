"""Round-bound offload evidence reports BOTH quantities — runs bound to a research round and runs
actually scored — and the offload CLI can bind a round and name the work's task type.

Written 2026-09-20: 3,285 offload runs over four months carried no round and had no outcome row,
and no line anywhere said so. The research program built as the Orchestrator's test bed had been
feeding the learner nothing; its driver called the CLI, which could not even pass `research_round`.
"""

from __future__ import annotations

import inspect
import json

import pytest

import dispatcher
import feedback
import research_subjects as rs


@pytest.fixture
def brain(monkeypatch, tmp_path):
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "brain.db")
    return tmp_path


def test_report_names_the_unbound_case_instead_of_printing_zero(brain):
    feedback.record_run("offload:codex:1", "offload:/tmp/x", "offload", "codex", mode="offload")
    rep = rs.rounds_report(30)
    assert (rep["offload_runs"], rep["bound_runs"], rep["rounds"], rep["scored"]) == (1, 0, 0, 0)
    line = rs.rounds_headline(rep)
    assert "none of 1 offload runs carries a research round" in line and "research_round=" in line


def test_report_counts_bound_scored_infra_and_unregistered_rounds(brain):
    rid, _ = rs.record_research_round(
        "stranske/Repo", "d-audit", "2026-09-20T20", "spec", ["cursor"], task_type="review"
    )
    feedback.record_run(
        "offload:cursor:1", "offload:/tmp/x", "review", "cursor", mode="offload", experiment_id=rid
    )
    feedback.record_outcome("offload:cursor:1", adjudicated_verdict="PASS")
    feedback.record_run(
        "offload:codex:2", "offload:/tmp/x", "review", "codex", mode="offload", experiment_id=rid
    )
    feedback.record_outcome("offload:codex:2", adjudicated_verdict="FAIL")
    assert feedback.mark_transient_infra("offload:codex:2", "timed out")
    # bound to a round nobody registered, and not yet scored
    feedback.record_run(
        "offload:gemini:3",
        "offload:/tmp/x",
        "offload",
        "gemini",
        mode="offload",
        experiment_id="research-program:d3-unblock-sweep:2026-09-20T20",
    )
    feedback.record_run(
        "offload:vibe:4", "offload:/tmp/x", "offload", "vibe", mode="offload"
    )  # unbound
    rep = rs.rounds_report(30)
    assert (rep["offload_runs"], rep["bound_runs"], rep["rounds"]) == (4, 3, 2)
    assert (rep["scored"], rep["pass"], rep["fail"], rep["infra"], rep["unscored"]) == (
        2,
        1,
        0,
        1,
        1,
    )
    assert rep["unregistered_rounds"] == ["research-program:d3-unblock-sweep:2026-09-20T20"]
    assert rep["by_kind"]["d-audit"]["runs"] == 2 and rep["by_kind"]["d-audit"]["agents"] == {
        "cursor": 1,
        "codex": 1,
    }
    assert rep["by_kind"]["d3-unblock-sweep"]["scored"] == 0
    line = rs.rounds_headline(rep)
    assert "2 rounds over 3 of 4 offload runs" in line and "unscored 1" in line
    assert "1 round id(s) unregistered" in line and "d-audit 2 runs/2 scored" in line


def test_cli_prints_the_headline_and_the_json(brain, capsys):
    assert rs.main(["rounds-report", "--headline"]) == 0
    assert capsys.readouterr().out.startswith("  ROUNDS (30d)")
    assert rs.main(["rounds-report", "--json", "--window-days", "7"]) == 0
    assert '"window_days": 7' in capsys.readouterr().out


def test_offload_cli_passes_the_round_and_task_type_through(monkeypatch):
    seen: dict = {}

    def fake(agent, prompt, **kw):
        seen.clear()
        seen.update(kw, agent=agent, prompt=prompt)
        return {"agent": agent, "exit": 0, "run_id": "offload:codex:1"}

    monkeypatch.setattr(dispatcher, "offload", fake)
    rc = dispatcher.main(
        [
            "offload",
            "--agent",
            "codex",
            "--prompt",
            "x",
            "--research-round",
            "stranske/repo:d-audit:2026-09-20T20",
            "--task-type",
            "review",
        ]
    )
    assert rc == 0
    assert (
        seen["research_round"] == "stranske/repo:d-audit:2026-09-20T20"
        and seen["task_type"] == "review"
    )
    assert dispatcher.main(["offload", "--agent", "codex", "--prompt", "x"]) == 0
    assert (
        seen["research_round"] is None and seen["task_type"] is None
    ), "unbound stays the explicit default"


def test_offload_records_the_task_type_it_is_given():
    """The parameter must reach the run the learner reads. WIRING PIN (smallest fragment, count == 1):
    the literal that used to hardcode every offload run's type must now defer to the argument."""
    assert "task_type" in inspect.signature(dispatcher.offload).parameters
    src = open(dispatcher.__file__).read()
    needle = "task_type = task_type or " + '"offload"'
    assert src.count(needle) == 1, "one run-type decision, deferring to the caller"
    assert src.count('task_type = "offload"') == 0, "the unconditional literal is gone"


def test_report_section_survives_an_unreadable_brain_and_is_wired_into_the_periodic_report(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "brain.db")
    lines = rs.render_report_lines(rs.summary_for_report())
    assert lines and lines[0].startswith("ROUNDS (30d): none of 0 offload runs")
    assert rs.render_report_lines({"window_days": 30, "error": "OperationalError: locked"}) == [
        "ROUNDS: UNMEASURABLE — OperationalError: locked"
    ]
    import periodic_report

    src = open(periodic_report.__file__).read()
    # WIRING PIN (smallest fragment, count == 1): the section is built AND rendered.
    assert src.count('"rounds": research_subjects.summary_for_report()') == 1
    assert src.count('research_subjects.render_report_lines(report["rounds"])') == 1


def _round_with_filed_finding(tmp_path):
    rid, identity = rs.record_research_round(
        "stranske/Repo", "d-audit", "2026-09-20T20", "spec", ["cursor"], task_type="review"
    )
    feedback.record_run(
        "offload:cursor:1", "offload:/tmp/x", "review", "cursor", mode="offload", experiment_id=rid
    )
    feedback.record_outcome("offload:cursor:1", adjudicated_verdict="PASS")
    rs.record_finding_issue(rid, "stranske/Repo#41", arm="cursor", identity=identity)
    # the fleet implemented #41 through PR #77; the Brain knows the PR run, never an issue run
    feedback.record_run(
        "keepalive:stranske/Repo#77:codex",
        "stranske/Repo#77",
        "implement",
        "codex",
        mode="remote",
        source="keepalive",
    )
    feedback.record_outcome(
        "keepalive:stranske/Repo#77:codex",
        adjudicated_verdict="PASS",
        merged=True,
        durability="durable",
    )
    return rid


def test_a_pr_run_still_cannot_be_bound_to_an_issue_without_the_closure_fact(brain):
    rid = _round_with_filed_finding(brain)
    with pytest.raises(ValueError, match="a binding may not re-target a finding"):
        rs.record_finding_implementation(
            rid, "stranske/Repo#41", "keepalive:stranske/Repo#77:codex"
        )
    with pytest.raises(ValueError, match="requires closure_source"):
        rs.record_finding_implementation(
            rid,
            "stranske/Repo#41",
            "keepalive:stranske/Repo#77:codex",
            closed_by="stranske/Repo#77",
        )
    with pytest.raises(ValueError, match="not the issue itself"):
        rs.record_finding_implementation(
            rid,
            "stranske/Repo#41",
            "keepalive:stranske/Repo#77:codex",
            closed_by="stranske/Repo#41",
            closure_source="github:closedByPullRequestsReferences",
        )


def test_a_github_closure_binds_the_pr_run_and_the_round_inherits_its_durability(brain):
    rid = _round_with_filed_finding(brain)
    before = rs.resolve_round_durability(rid)
    assert before["unresolved"] == 1 and before["unresolved_by_reason"] == {"no_outcome_run": 1}
    rs.record_finding_implementation(
        rid,
        "stranske/Repo#41",
        "keepalive:stranske/Repo#77:codex",
        closed_by="stranske/Repo#77",
        closure_source="github:closedByPullRequestsReferences",
    )
    with feedback._conn() as c:
        meta = json.loads(
            c.execute(
                "SELECT metadata_json FROM research_subject_events WHERE exp_id=? AND decision='finding_implemented'",
                (rid,),
            ).fetchone()[0]
        )
    assert (
        meta["closed_by"] == "stranske/repo#77"
        and meta["closure_source"] == "github:closedByPullRequestsReferences"
    )
    assert meta["arm"] == "cursor", "the arm is inherited from the filing, never re-attributed"
    after = rs.resolve_round_durability(rid, apply_edges=True)
    assert after["resolved"] == 1 and after["per_arm_durability"] == {"cursor": {"durable": 1}}
    assert after["edges_written"] == 1
    with feedback._conn() as c:
        assert (
            c.execute(
                "SELECT COUNT(*) FROM influence_edges WHERE influence_id=?", (rid,)
            ).fetchone()[0]
            == 1
        )


def test_cli_finding_implemented_accepts_the_closure_flags(brain, capsys):
    rid = _round_with_filed_finding(brain)
    rc = rs.main(
        [
            "finding-implemented",
            "--round-id",
            rid,
            "--issue",
            "stranske/Repo#41",
            "--run-id",
            "keepalive:stranske/Repo#77:codex",
            "--closed-by-pr",
            "stranske/Repo#77",
            "--closure-source",
            "github:closedByPullRequestsReferences",
        ]
    )
    assert rc == 0 and '"recorded": true' in capsys.readouterr().out
