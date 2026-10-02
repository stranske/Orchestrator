"""redirect_apply screens for free before it pays the judge, and never reads an unknown lane as dead.

MEASURED 2026-10-02. The daily `redirect-apply-link` step ran one metered RedirectAgent offload
(600 s timeout) per file in the keepalive supervisor's report directory: 759 judgements from
2026-08-21, every plan `wait`, none authorised, while the Stage-2 deficits sat at 5 and 3 for 42
days. Three relationships were wrong, and each test below pins one of them:

  * POPULATION. Nothing prunes the report directory, so all 30 files were for PRs closed between
    2026-06-23 and 2026-09-20; the supervisor's own run minutes earlier had found 0 candidates.
    Candidates are now its latest stage-2 plan, and a stale or missing plan is UNKNOWN, never 0.
  * LIVENESS. A keepalive report carries no pid, and `pid is None` read as dead, so "apply never
    kills a live lane" held only for local lanes, which this step never reads. A lane the
    supervisor called `running` was one `redirect` verdict away from release-claim + delegate.
  * IDENTICAL INPUT. 766 live judgements covered 39 distinct reports; one was judged 36 times.

Everything runs against temporary stores: the Brain, the redirect corpus, the prompt directory and
`HANDOFF_DIR` (claims) all point into `tmp_path`, and heartbeats stay off.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

import feedback
import redirect_apply as ra
import redirect_plan
import redirect_shadow
import roles

OPEN_GATE = {"bootstrap_needed": True, "disagreements_needed": 3}
# A keepalive lane exactly as keepalive_supervisor.build_redirect_report writes it: no pid at all.
LANE = {"agent": "keepalive", "lane": "closer", "task_type": "implement"}
# The shape of every one of the 30 live reports on 2026-10-02 (one of them verbatim but the target).
LIVE_SHAPE = {
    **LANE,
    "drift": {"findings": [{"kind": "broad_churn"}], "severity": "medium"},
    "errors": [],
    "hints": [{"kind": "auth"}],
    "keepalive_supervisor": {
        "live_action_enabled": False,
        "signals_summary": {
            "consecutive_no_progress": 0,
            "failure_count": 0,
            "gate_conclusion": "success",
            "has_marker": True,
            "rounds_without_task_completion": 2,
        },
        "stage": "supervised_candidate",
    },
    "policy_decision": {
        "action": "inspect",
        "advisory": True,
        "confidence": "medium",
        "reason": "lane is active but drift signals require scope review",
    },
    "recommended_action": "wait",
    "signals": {"has_worktree_changes": True},
    "state": "running",
}


@pytest.fixture()
def stores(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "brain.db")
    monkeypatch.setattr(redirect_plan, "PROMPT_DIR", tmp_path / "prompts")
    monkeypatch.setattr(redirect_shadow, "CORPUS_PATH", tmp_path / "corpus.jsonl")
    monkeypatch.setenv("HANDOFF_DIR", str(tmp_path / "handoff"))
    monkeypatch.delenv("ORCH_CAPABILITY_HEARTBEATS", raising=False)
    monkeypatch.delenv(ra.BOOTSTRAP_FLAG, raising=False)
    report_dir = tmp_path / "reports"
    report_dir.mkdir()
    return {
        "tmp": tmp_path,
        "corpus": tmp_path / "corpus.jsonl",
        "report_dir": report_dir,
        "plan": tmp_path / "keepalive-supervisor-stage2-plan.json",
        "now": int(time.time()),
    }


def _must_not_apply(*_a, **_k):
    raise AssertionError("apply_plan was reached when it must not be")


class Judge:
    """A role runner that answers with a fixed verdict and records every target it was asked."""

    def __init__(self, action: str) -> None:
        self.asked: list[str] = []
        self.verdict = {
            "action": action,
            "reason": "fixture verdict",
            "confidence": "high",
            "corrected_prompt": "Retry with fresh auth, keep scope, validate, push, open PR.",
            "switch_agent": "codex",
        }

    def __call__(self, report, acceptance_criteria, **kwargs):
        self.asked.append(report.get("target"))
        return roles.run_redirect_agent(
            report, acceptance_criteria, proposal_json=self.verdict, **kwargs
        )


def _write_reports(report_dir: Path, reports: list[dict]) -> None:
    for report in reports:
        name = report["target"].replace("/", "__").replace("#", "__")
        (report_dir / f"{name}.keepalive-supervisor-report.json").write_text(json.dumps(report))


def _run(stores: dict, judge: Judge, **overrides) -> dict:
    kwargs = {
        "report_dir": stores["report_dir"],
        "plan_path": stores["plan"],
        "corpus_path": stores["corpus"],
        "env": {ra.BOOTSTRAP_FLAG: "1"},
        "role_runner": judge,
        "apply_runner": _must_not_apply,
        "pid_checker": lambda pid: False,
        "now": stores["now"],
    }
    kwargs.update(overrides)
    return ra.apply_candidates(**kwargs)


def _stamped(target: str) -> dict:
    return {
        "action": "redirect",
        "target": target,
        "prompt_text": "x",
        "prompt_file": "f",
        "accepted_role_run_id": "role:redirect:codex:1",
        "steps": [{"id": "delegate-retry", "commands": [["python3", "d.py"]]}],
    }


def _authorize(target: str, **lane) -> dict:
    return ra.authorize(
        plan_obj=_stamped(target),
        role_run_id="role:redirect:codex:1",
        decision_source="redirect_agent",
        errors=[],
        claim_holder=None,
        prior_agent="keepalive",
        gate=OPEN_GATE,
        applied_targets=set(),
        applies_today=0,
        flag_on=True,
        **lane,
    )


def test_pidless_running_lane_is_refused_even_when_the_judge_says_redirect(stores):
    """The safety gap itself: before the fix this lane was AUTHORISED (pid None read as dead)."""
    judge = Judge("redirect")
    report = {**LANE, "target": "o/r#11", "state": "running", "recommended_action": "inspect"}
    out = ra.apply_one(
        report=report,
        acceptance_criteria="AC",
        backend="codex",
        corpus_path=stores["corpus"],
        env={ra.BOOTSTRAP_FLAG: "1"},
        role_runner=judge,
        pid_checker=lambda pid: False,
        apply_runner=_must_not_apply,
    )
    assert judge.asked == ["o/r#11"]
    assert out["authorization"]["allowed"] is False, out["authorization"]
    assert out["apply_result"] is None
    assert any("reports the lane 'running'" in b for b in out["authorization"]["blocks"])


def test_pidless_lane_nothing_shows_dead_never_reaches_apply_plan(stores):
    """End to end, the lane whose liveness NOTHING shows: no pid, and a state that is neither live
    nor stalled. Every other rule passes and the flag is on, so only the UNKNOWN rule stands
    between a `redirect` verdict and release-claim + delegate — `_must_not_apply` raises if reached.
    """
    judge = Judge("redirect")
    report = {**LANE, "target": "o/r#10", "state": "missing", "recommended_action": "inspect"}
    out = ra.apply_one(
        report=report,
        acceptance_criteria="AC",
        backend="codex",
        corpus_path=stores["corpus"],
        env={ra.BOOTSTRAP_FLAG: "1"},
        role_runner=judge,
        pid_checker=lambda pid: False,
        apply_runner=_must_not_apply,
    )
    assert out["authorization"]["allowed"] is False, out["authorization"]
    assert out["authorization"]["would_mutate"] is False, out["authorization"]
    assert any("liveness is UNKNOWN" in b for b in out["authorization"]["blocks"]), out


def test_unknown_liveness_is_a_refusal_and_only_the_supervisors_word_lifts_it():
    assert ra.lane_liveness({"target": "o/r#1"}) is None
    assert ra.lane_liveness({"target": "o/r#1", "pid": "not-a-pid"}) is None
    assert ra.lane_liveness({"target": "o/r#1", "pid": 0}) is None
    assert ra.lane_liveness({"target": "o/r#1", "pid": 7}, pid_checker=lambda pid: False) is False
    assert ra.lane_liveness({"target": "o/r#1", "pid": 7}, pid_checker=lambda pid: True) is True

    for state in ("missing", "", None, "some-new-state"):
        refused = _authorize("o/r#12", pid_alive=None, lane_state=state, recommended_action=None)
        assert refused["allowed"] is False, (state, refused)
        assert any("liveness is UNKNOWN" in b for b in refused["blocks"]), (state, refused)
    for state in sorted(ra.REPORTED_NOT_LIVE_STATES):
        lifted = _authorize(
            "o/r#13", pid_alive=None, lane_state=state, recommended_action="inspect"
        )
        assert lifted["allowed"] is True, (state, lifted)
    for state in sorted(ra.REPORTED_LIVE_STATES):
        live = _authorize("o/r#14", pid_alive=None, lane_state=state, recommended_action="inspect")
        assert live["allowed"] is False and any("live lane is never" in b for b in live["blocks"])
    for action in sorted(ra.NO_REDIRECT_RECOMMENDATIONS):
        idle = _authorize("o/r#15", pid_alive=None, lane_state="stalled", recommended_action=action)
        assert idle["allowed"] is False and any(
            f"recommends {action!r}" in b for b in idle["blocks"]
        )


def test_live_pid_message_is_the_one_seven_rail_exercise_harnesses_grep_for():
    """`redirect-apply-authorize-harness.py` is copied into seven rail-exercise contracts and
    asserts this exact block text; a reworded refusal would turn all seven red at once."""
    out = _authorize("o/r#16", pid_alive=True)
    assert "prior process is still alive — apply never kills a live lane" in out["blocks"], out


def test_free_screen_and_authorisation_agree_on_every_lane(stores):
    """ONE predicate serves both sides, so for a fully valid plan the authorisation's blocks are
    exactly the screen's. Checked over every pid / state / recommendation combination."""
    pids = {"absent": None, "dead": 4242, "alive": 4243}
    states = [None, "running", "progress", "stalled", "exited", "missing"]
    actions = [None, "wait", "collect", "inspect", "redirect"]
    checked = 0
    for pid_name, pid in pids.items():
        for state in states:
            for action in actions:
                report = {**LANE, "target": "o/r#40"}
                if pid is not None:
                    report["pid"] = pid
                if state is not None:
                    report["state"] = state
                if action is not None:
                    report["recommended_action"] = action

                def alive(number: int) -> bool:
                    return number == pids["alive"]

                screened = ra.screen_report(
                    report,
                    gate=OPEN_GATE,
                    applied_targets=set(),
                    applies_today=0,
                    pid_checker=alive,
                )
                authorised = _authorize(
                    "o/r#40",
                    pid_alive=ra.lane_liveness(report, pid_checker=alive),
                    lane_state=report.get("state"),
                    recommended_action=report.get("recommended_action"),
                )
                assert screened["blocks"] == authorised["blocks"], (pid_name, state, action)
                assert screened["passes_screen"] is authorised["allowed"], (pid_name, state, action)
                checked += 1
    assert checked == len(pids) * len(states) * len(actions)


def test_a_stale_report_on_disk_is_counted_and_never_judged(stores):
    """A PR that was stalled when its report was written and has closed since: it would pass every
    lane rule, so only the POPULATION rule keeps the judge from being paid to look at it."""
    stale = {**LANE, "target": "o/r#20", "state": "stalled", "recommended_action": "inspect"}
    current = {**LANE, "target": "o/r#21", "state": "stalled", "recommended_action": "inspect"}
    _write_reports(stores["report_dir"], [stale, current])
    ra._write_stage2_plan(stores["plan"], [current], generated_at=stores["now"])
    judge = Judge("inspect")
    out = _run(stores, judge)
    assert judge.asked == ["o/r#21"], judge.asked
    assert (out["reports_seen"], out["current_candidates"], out["stale_reports"]) == (2, 1, 1)
    assert out["stale_targets"] == ["o/r#20"], out
    assert (out["passing_screen"], out["offloads_spent"]) == (1, 1), out


def test_the_measured_incident_spends_nothing(stores):
    """2026-10-02 replayed: 30 report files shaped like the live ones, and a supervisor plan that
    found no live candidate. The old step spent 30 offloads here; the screen spends none."""
    reports = [{**LIVE_SHAPE, "target": f"stranske/Repo#{n}"} for n in range(30)]
    _write_reports(stores["report_dir"], reports)
    ra._write_stage2_plan(stores["plan"], [], generated_at=stores["now"])
    judge = Judge("wait")
    out = _run(stores, judge)
    assert judge.asked == []
    assert (out["reports_seen"], out["stale_reports"], out["current_candidates"]) == (30, 30, 0)
    assert (out["passing_screen"], out["offloads_spent"], out["authorized"]) == (0, 0, 0)
    text = "\n".join(ra.format_apply(out, flag_on=True))
    assert text.startswith("offloads_spent=0 authorized=0 applied=0"), text
    assert "could authorise: 0 of 0 current candidates" in text, text

    # And had all 30 still been current, the LANE rules alone would have screened every one out:
    # the supervisor called each `running` and recommended `wait`.
    ra._write_stage2_plan(stores["plan"], reports, generated_at=stores["now"])
    out = _run(stores, judge)
    assert judge.asked == [] and out["offloads_spent"] == 0 and out["passing_screen"] == 0, out
    assert out["current_candidates"] == 30 and out["stale_reports"] == 0, out


def test_a_screened_out_candidate_spends_no_offload(stores):
    active = {**LANE, "target": "o/r#22", "state": "running", "recommended_action": "wait"}
    ra._write_stage2_plan(stores["plan"], [active], generated_at=stores["now"])
    judge = Judge("redirect")
    out = _run(stores, judge)
    assert judge.asked == [] and out["offloads_spent"] == 0, out
    (row,) = out["results"]
    assert row["offloaded"] is False and row["authorized"] is False, row
    assert "screened out (no offload)" in "\n".join(ra.format_apply(out, flag_on=True))


def test_identical_input_is_judged_once_and_a_changed_report_is_judged_again(stores):
    current = {**LANE, "target": "o/r#23", "state": "stalled", "recommended_action": "inspect"}
    ra._write_stage2_plan(stores["plan"], [current], generated_at=stores["now"])
    judge = Judge("inspect")
    assert _run(stores, judge)["offloads_spent"] == 1
    again = _run(stores, judge)
    assert judge.asked == ["o/r#23"] and again["offloads_spent"] == 0, again
    assert any("already judged 'inspect'" in b for r in again["results"] for b in r["blocks"])

    changed = {**current, "hints": [{"kind": "auth"}]}
    ra._write_stage2_plan(stores["plan"], [changed], generated_at=stores["now"])
    assert _run(stores, judge)["offloads_spent"] == 1
    assert judge.asked == ["o/r#23", "o/r#23"], judge.asked


def test_an_applyable_verdict_does_not_suppress_a_rejudge(stores):
    """Only an answer no plan can apply suppresses the re-ask. A `redirect` that was refused for a
    reason of the day (here the daily bound) must be askable again, or the dedupe is a latch."""
    current = {**LANE, "target": "o/r#24", "state": "stalled", "recommended_action": "inspect"}
    report_key = ra.judgement_key(current, "AC")
    redirect_shadow._append_event(
        {
            "kind": "redirect_proposal",
            "source": "live-dispatch",
            "valid_proposal": True,
            "proposal_action": "redirect",
            "target": report_key[0],
            "report_sha256": report_key[1],
            "acceptance_criteria_sha256": report_key[2],
            "ts": stores["now"] - 3600,
        },
        stores["corpus"],
    )
    judged, last = ra.judged_inputs(stores["corpus"])
    verdict = ra.screen_report(
        current,
        gate=OPEN_GATE,
        applied_targets=set(),
        applies_today=0,
        judged=judged,
        acceptance_criteria="AC",
    )
    assert verdict["passes_screen"] is True, verdict
    assert last == {"o/r#24": stores["now"] - 3600}


def test_the_cap_defers_without_starving_anyone(stores):
    many = [
        {**LANE, "target": f"o/r#3{i}", "state": "stalled", "recommended_action": "inspect"}
        for i in range(5)
    ]
    ra._write_stage2_plan(stores["plan"], many, generated_at=stores["now"])
    judge = Judge("inspect")
    first = _run(stores, judge, max_offloads=3)
    assert len(judge.asked) == 3 and first["deferred_by_cap"] == 2, first
    assert "deferred by the per-run cap: 2" in "\n".join(ra.format_apply(first, flag_on=True))
    judged_first = set(judge.asked)

    changed = [{**rep, "hints": [{"kind": "auth"}]} for rep in many]
    ra._write_stage2_plan(stores["plan"], changed, generated_at=stores["now"])
    judge.asked.clear()
    _run(stores, judge, max_offloads=2)
    assert set(judge.asked) == {rep["target"] for rep in many} - judged_first, judge.asked


def test_an_unknown_population_is_none_and_an_empty_one_is_zero(stores):
    current = {**LANE, "target": "o/r#25", "state": "stalled", "recommended_action": "inspect"}
    judge = Judge("inspect")

    missing = _run(stores, judge, plan_path=stores["tmp"] / "absent.json")
    assert missing["passing_screen"] is None and missing["population"]["status"] == "missing"

    stores["plan"].write_text("{not json")
    unreadable = _run(stores, judge)
    assert unreadable["passing_screen"] is None, unreadable
    assert unreadable["population"]["status"] == "unreadable", unreadable

    max_age = ra.stage2_population(stores["plan"])["max_age_s"]
    ra._write_stage2_plan(stores["plan"], [current], generated_at=stores["now"] - max_age - 1)
    stale = _run(stores, judge)
    assert stale["passing_screen"] is None and stale["population"]["status"] == "stale", stale
    assert judge.asked == []
    assert ra.format_apply(stale, flag_on=True)[1].startswith("  could authorise: UNKNOWN — ")

    ra._write_stage2_plan(stores["plan"], [], generated_at=stores["now"])
    empty = _run(stores, judge)
    assert empty["passing_screen"] == 0 and empty["population"]["status"] == "current", empty


def test_the_stale_bound_is_the_cadence_registrys_own_rule():
    """One rule for "this step's artifact no longer describes the present", shared with the
    cadence report, so a plan the report calls stale is never a population here."""
    import cadence_registry

    step = cadence_registry.STEP_BY_KEY[ra.STAGE2_PLAN_STEP]
    assert ra.stage2_population(Path("/nonexistent/plan.json"))["max_age_s"] == (
        cadence_registry.stale_after_seconds(step)
    )
    assert ra.default_stage2_plan_path().name == step["artifact"]


def test_a_drained_gate_says_finished_through_the_real_gate_state(stores, monkeypatch):
    """Question 4 by construction: drive the real `gate_state` to zero deficits and read what
    `--status` prints, so the FINISHED branch is proven reachable, not merely written."""
    drained = {
        "synced_role_outcomes": redirect_shadow.LINKED_OUTCOME_TARGET,
        "linked_disagreements": redirect_shadow.DISAGREEMENT_OUTCOME_TARGET,
        "ready_for_supervised_apply": True,
        "valid_proposals": redirect_shadow.READINESS_TARGET,
    }
    monkeypatch.setattr(redirect_shadow, "summarize", lambda *_a, **_k: drained)
    ra._write_stage2_plan(stores["plan"], [], generated_at=stores["now"])
    out = ra.status(
        stores["corpus"], env={}, report_dir=stores["report_dir"], plan_path=stores["plan"]
    )
    lines = ra.format_status(out)
    assert out["gate"]["bootstrap_needed"] is False, out["gate"]
    assert any("FINISHED" in line for line in lines), lines
    assert out["drainable"] == 0 and "drainable: 0 of 0 current candidates" in lines[2], lines
