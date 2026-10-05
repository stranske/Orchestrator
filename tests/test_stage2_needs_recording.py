"""The planner's "needs recording" count is the population the redirect bootstrap can drain.

MEASURED 2026-10-02. `keepalive_supervisor.stage2_acquisition_plan` counted every eligible
escalated PR without a valid live-dispatch proposal as needing a Stage-2 recording. Whenever that
count was above 0, `observability_dashboard` raised the `stage2_live_candidates` warn, with one
offload-spending record command per PR.

Until #376, the redirect bootstrap judged every report every day and recorded a proposal for each,
so the count cleared within one plan run. #376's free screen judges only a lane its report shows
stalled or exited and not recommended `wait`/`collect`. Every one of the 766 reports judged since
2026-06-23 was `progress` or `running` with `wait`. So the count would hold every new escalation for
as long as it stayed escalated, and only the offload the screen refuses to buy could clear it.

The same shape already happened once, before the bootstrap existed: in July Workflows#2759 sat in
that count for 13 days until an operator spent the offload, and the verdict was `wait`.

The planner now asks the screen's own predicate (`redirect_apply.lane_refusals`). A lane the
screen refuses is counted under `lane_refused_live_candidates`, with the screen's reasons, and no
recording is requested for it. Each test pins one side of that relationship. Every store is
temporary: the Brain, the redirect corpus, the prompt directory, `HANDOFF_DIR` and the cadence
state.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from test_observability_activation import _minimal_report
from test_redirect_apply_prescreen import LANE, LIVE_SHAPE, OPEN_GATE, Judge, _must_not_apply

import cadence_registry
import feedback
import keepalive_shadow
import keepalive_supervisor as ks
import observability_dashboard as dashboard
import periodic_report
import redirect_apply as ra
import redirect_plan
import redirect_shadow

# Keepalive-state payloads that keepalive_shadow.synthesize_report turns into each lane state.
PAYLOAD_FOR = {
    "running": {"rounds_without_task_completion": 2},  # running, recommends wait
    "progress": {"last_files_changed": 3},  # progress, recommends wait
    "stalled": {"consecutive_zero_activity_rounds": 3},  # stalled, recommends inspect
}


@pytest.fixture()
def stores(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "brain.db")
    monkeypatch.setattr(redirect_plan, "PROMPT_DIR", tmp_path / "prompts")
    monkeypatch.setattr(redirect_shadow, "CORPUS_PATH", tmp_path / "corpus.jsonl")
    monkeypatch.setenv("HANDOFF_DIR", str(tmp_path / "handoff"))
    monkeypatch.delenv("ORCH_CAPABILITY_HEARTBEATS", raising=False)
    monkeypatch.delenv(ra.BOOTSTRAP_FLAG, raising=False)
    import redirect_sweep

    monkeypatch.setattr(redirect_sweep, "DEFAULT_REPORT", tmp_path / "absent-sweep.json")
    report_dir = tmp_path / "reports"
    report_dir.mkdir()
    return {
        "tmp": tmp_path,
        "corpus": tmp_path / "corpus.jsonl",
        "keepalive_corpus": tmp_path / "keepalive-shadow.jsonl",
        "report_dir": report_dir,
        "plan": tmp_path / "keepalive-supervisor-stage2-plan.json",
    }


def _escalated(targets: list[str]):
    """A `gh search` stand-in that finds exactly these open escalated keepalive PRs."""

    def runner(cmd, capture_output=True, text=True):
        out = "".join(f"{t}\n" for t in targets) if "needs-human" in cmd else ""
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")

    return runner


def _planner(stores: dict, targets: list[str]) -> dict:
    return ks.stage2_acquisition_plan(
        live_limit=max(len(targets), 1),
        historical_limit=5,
        report_dir=stores["report_dir"],
        redirect_corpus_path=stores["corpus"],
        keepalive_corpus_path=stores["keepalive_corpus"],
        runner=_escalated(targets),
    )


def _reported_lanes(monkeypatch: pytest.MonkeyPatch, lanes: dict[str, str]) -> None:
    """Independent lane reports for the shared refusal predicate.

    An explicit escalation label now correctly makes a synthesized report escalated.
    Running/progress refusal cases must use reports that actually state those states.
    The production post-escalation construction is tested separately below.
    """
    _reports(
        monkeypatch,
        [
            {
                **LANE,
                "target": target,
                "state": state,
                "recommended_action": "wait" if state in {"running", "progress"} else "inspect",
            }
            for target, state in lanes.items()
        ],
    )


def _reports(monkeypatch: pytest.MonkeyPatch, reports: list[dict]) -> None:
    """Serve explicit report states as eligible plans, including the exhaustive check."""
    by_target = {r["target"]: r for r in reports}

    def plan_target(target, *, report_dir=None, acceptance_criteria=ks.DEFAULT_AC, **_kw):
        return {
            "target": target,
            "eligible": True,
            "report": by_target[target],
            "acceptance_criteria": acceptance_criteria,
            "stage2_record_command": ["python3", "roles.py", "redirect", "--record-corpus"],
        }

    monkeypatch.setattr(ks, "plan_target", plan_target)


def _write_plan(stores: dict, plan: dict) -> None:
    """What orchestrate.sh does with the planner's --json output."""
    stores["plan"].write_text(json.dumps(plan, sort_keys=True), encoding="utf-8")


def _partition(plan: dict) -> tuple[int, int, int, int]:
    return (
        plan["eligible_live_candidate_count"],
        plan["unrecorded_live_candidate_count"],
        len(plan["already_recorded_live_targets"]),
        plan["lane_refused_live_candidate_count"],
    )


def test_the_measured_shape_needs_no_recording_and_says_why(stores, monkeypatch):
    """Both live shapes on record, `running`/`wait` and `progress`/`wait`, through the planner's
    explicit report fixtures. Before the fix this plan reported 2 unrecorded and emitted two
    record commands, one offload each, for lanes the bootstrap will never judge."""
    _reported_lanes(monkeypatch, {"o/r#1": "running", "o/r#2": "progress"})
    plan = _planner(stores, ["o/r#1", "o/r#2"])
    assert _partition(plan) == (2, 0, 0, 2), plan
    assert [c for c in plan["commands"] if c["kind"] == "live_stage2_record"] == [], plan
    refused = {row["target"]: row["blocks"] for row in plan["lane_refused_live_candidates"]}
    assert sorted(refused) == ["o/r#1", "o/r#2"], refused
    assert any("reports the lane 'running'" in b for b in refused["o/r#1"]), refused
    assert any("reports the lane 'progress'" in b for b in refused["o/r#2"]), refused
    assert all(any("recommends 'wait'" in b for b in blocks) for blocks in refused.values())
    # The refused PRs are still candidates: the bootstrap reads `plans`, and screens them itself.
    assert [p["target"] for p in plan["plans"] if p["eligible"]] == ["o/r#1", "o/r#2"], plan


def test_the_planner_and_the_screen_count_one_population(stores, monkeypatch):
    """Over every pid / state / recommendation combination, a candidate needs recording exactly
    when the bootstrap's free screen would pass its lane, and a refused one carries the screen's
    own reasons. With an open gate, no claim and nothing applied, the screen's blocks ARE its
    lane blocks, so the two lists must be equal, not merely both non-empty."""
    pids = {"absent": None, "dead": 4242, "alive": 4243}
    states = [None, "running", "progress", "stalled", "exited", "missing"]
    actions = [None, "wait", "collect", "inspect", "redirect"]
    reports = []
    for pid in pids.values():
        for state in states:
            for action in actions:
                report = {**LANE, "target": f"o/r#{len(reports)}"}
                if pid is not None:
                    report["pid"] = pid
                if state is not None:
                    report["state"] = state
                if action is not None:
                    report["recommended_action"] = action
                reports.append(report)
    # The planner probes pids with the process-table default; the screen is handed the same rule.
    monkeypatch.setattr(redirect_plan, "_pid_alive", lambda number: number == pids["alive"])
    _reports(monkeypatch, reports)
    plan = _planner(stores, [r["target"] for r in reports])

    needs = {c["target"] for c in plan["commands"] if c["kind"] == "live_stage2_record"}
    refused = {row["target"]: row["blocks"] for row in plan["lane_refused_live_candidates"]}
    for report in reports:
        screened = ra.screen_report(report, gate=OPEN_GATE, applied_targets=set(), applies_today=0)
        target = report["target"]
        assert (target in needs) is screened["passes_screen"], (report, screened)
        assert refused.get(target, []) == screened["blocks"], (report, screened)
    assert len(needs) + len(refused) == len(reports) == 90
    assert _partition(plan) == (90, len(needs), 0, len(refused)), plan
    # Not vacuous: 6 pid-less lanes (stalled|exited) and 12 dead-pid lanes (any state but
    # running|progress), each recommending nothing, inspect or redirect.
    assert len(needs) == 18, sorted(needs)


def test_one_predicate_serves_both_sides_not_a_copy(stores, monkeypatch):
    """Replace the shared lane rule and BOTH sides must follow. A planner, or a screen, that had
    kept its own copy of the rule would still agree with the other today and stop following the
    moment the rule changed; this is the test that tells a copy from a share."""
    reports = [{**LIVE_SHAPE, "target": "o/r#1"}, {**LANE, "target": "o/r#2", "state": "stalled"}]
    _reports(monkeypatch, reports)

    monkeypatch.setattr(ra, "_report_lane_blocks", lambda **_facts: ["SENTINEL"])
    plan = _planner(stores, ["o/r#1", "o/r#2"])
    assert [row["blocks"] for row in plan["lane_refused_live_candidates"]] == [["SENTINEL"]] * 2
    for report in reports:
        screened = ra.screen_report(report, gate=OPEN_GATE, applied_targets=set(), applies_today=0)
        assert screened["blocks"] == ["SENTINEL"], screened

    monkeypatch.setattr(ra, "_report_lane_blocks", lambda **_facts: [])
    plan = _planner(stores, ["o/r#1", "o/r#2"])
    assert _partition(plan) == (2, 2, 0, 0), plan
    for report in reports:
        screened = ra.screen_report(report, gate=OPEN_GATE, applied_targets=set(), applies_today=0)
        assert screened["passes_screen"] is True, screened


def test_the_bootstrap_drains_exactly_what_the_planner_counts(stores, monkeypatch):
    """End to end, through the real modules: the planner's plan is handed to the armed bootstrap
    the way orchestrate.sh hands it, the bootstrap judges exactly the targets the planner said
    need recording, and the planner's next run finds them recorded. Before the fix, the two
    running/progress lanes stayed in the count after the bootstrap ran, and nothing but a manual
    offload could take them out."""
    _reported_lanes(monkeypatch, {"o/r#1": "running", "o/r#2": "progress", "o/r#3": "stalled"})
    targets = ["o/r#1", "o/r#2", "o/r#3"]
    plan = _planner(stores, targets)
    assert _partition(plan) == (3, 1, 0, 2), plan
    needs = [c["target"] for c in plan["commands"] if c["kind"] == "live_stage2_record"]
    assert needs == ["o/r#3"], plan

    _write_plan(stores, plan)
    judge = Judge("inspect")
    applied = ra.apply_candidates(
        report_dir=stores["report_dir"],
        plan_path=stores["plan"],
        corpus_path=stores["corpus"],
        env={ra.BOOTSTRAP_FLAG: "1"},
        role_runner=judge,
        apply_runner=_must_not_apply,
        pid_checker=lambda pid: False,
    )
    assert judge.asked == needs, (judge.asked, applied)
    assert (applied["passing_screen"], applied["offloads_spent"]) == (1, 1), applied

    drained = _planner(stores, targets)
    assert _partition(drained) == (3, 0, 1, 2), drained
    assert drained["already_recorded_live_targets"] == ["o/r#3"], drained
    assert [c for c in drained["commands"] if c["kind"] == "live_stage2_record"] == []


def test_a_drained_plan_prints_zero_unrecorded_beside_the_refused(stores, monkeypatch, capsys):
    """Question 4 by construction: run the CLI the cadence step runs and read what it prints
    when nothing needs recording, both with refused lanes and with no candidate at all."""
    _reported_lanes(monkeypatch, {"o/r#1": "running", "o/r#2": "progress"})
    targets = ["o/r#1", "o/r#2"]
    monkeypatch.setattr(ks, "live_targets", lambda **kw: list(targets))
    argv = [
        "--stage2-plan",
        "--write-report-dir",
        str(stores["report_dir"]),
        "--redirect-corpus",
        str(stores["corpus"]),
        "--keepalive-corpus",
        str(stores["keepalive_corpus"]),
    ]
    assert ks.main(argv) == 0
    text = capsys.readouterr().out
    assert "live_candidates=0 unrecorded / 2 lane-refused / 2 eligible / 2 total" in text, text
    assert "lane_refused=o/r#1: the supervisor reports the lane 'running'" in text, text
    assert "live_stage2_record_command=" not in text, text

    targets.clear()
    assert ks.main(argv) == 0
    text = capsys.readouterr().out
    assert "live_candidates=0 unrecorded / 0 lane-refused / 0 eligible / 0 total" in text, text
    assert "lane_refused=" not in text, text


def test_unmeasured_is_never_zero_on_the_way_to_the_dashboard(stores, monkeypatch):
    """A plan written before the planner applied the screen cannot say which of its candidates
    were refused. It must read as UNMEASURED, never as 0, and its count must still warn, because
    it may include a lane that does need recording. Only the new plan shape can clear the warn."""
    monkeypatch.setattr(
        dashboard, "_cadence_health", lambda: cadence_registry.inspect_cadence(stores["tmp"], now=1)
    )
    _reported_lanes(monkeypatch, {"o/r#1": "running", "o/r#2": "progress"})
    _write_plan(stores, _planner(stores, ["o/r#1", "o/r#2"]))
    current = periodic_report._stage2_live_plan_summary(stores["plan"])
    assert current["lane_refused_live_candidate_count"] == 2, current
    assert current["unrecorded_live_candidate_count"] == 0, current

    old = stores["tmp"] / "old-plan.json"
    old.write_text(
        json.dumps({"generated_at": 1, "unrecorded_live_candidate_count": 2, "plans": []}),
        encoding="utf-8",
    )
    before = periodic_report._stage2_live_plan_summary(old)
    assert before["lane_refused_live_candidate_count"] is None, before
    missing = periodic_report._stage2_live_plan_summary(stores["tmp"] / "absent.json")
    assert missing["lane_refused_live_candidate_count"] is None, missing

    def built(live_plan: dict) -> tuple[list[str], str]:
        report = _minimal_report()
        report["keepalive_supervisor"] = {
            "stage2_proposal_corpus": {"live_plan": live_plan, "summary": {}}
        }
        dash = dashboard.build_dashboard(report, capacity_snapshot={})
        return [a["key"] for a in dash["alerts"]], dashboard.format_markdown(dash)

    keys, text = built(current)
    assert "stage2_live_candidates" not in keys, keys
    assert "unrecorded=0 lane_refused=2" in text, text

    keys, text = built(before)
    assert keys.count("stage2_live_candidates") == 1, keys
    assert "unrecorded=2 lane_refused=unmeasured" in text, text


def test_production_escalation_is_a_drainable_recording_candidate(stores, monkeypatch):
    def gather(target, **kwargs):
        return keepalive_shadow.normalize_signals(
            target,
            {"rounds_without_task_completion": 2},
            pr_state="OPEN",
            labels=[ks.KEEPALIVE_LABEL, "needs-human"],
        )

    monkeypatch.setattr(keepalive_shadow, "gather_signals", gather)
    plan = _planner(stores, ["o/r#1"])
    assert _partition(plan) == (1, 1, 0, 0)
    report = plan["plans"][0]["report"]
    assert report["state"] == "escalated"
    assert ra.screen_report(report, gate=OPEN_GATE, applied_targets=set(), applies_today=0)[
        "passes_screen"
    ]
