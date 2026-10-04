"""The ship-gate stamp may hold launches once per FINISH, never once per tick.

`exp_abcd.followup` launches at most one synthesis per `SHIP_GATE_HOLD_S`, keyed on the age of
`experiments/.last-ship-gate`. Until 2026-10-04 every run re-touched that stamp for every promotion
that was ALREADY terminal, so with finished promotions on disk the stamp never aged: no launch was
available at the start of any run from 2026-07-09 on, and 254 of 256 evaluated candidates expired
unlaunched after their 14-day TTL. These tests pin the mechanism, not the incident: a run that
finishes nothing leaves the stamp alone; the hold is per finish and survives a second run; an old
stamp releases exactly one launch; a new finish holds the gate once; and the summary line always
carries both of the gate's numbers, with "unmeasured" spelled differently from zero.
"""

from __future__ import annotations

import json
import os
import time

import pytest

import adapters
import exp_abcd
import feedback
import synthesis_promotion


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(adapters, "HANDOFF", tmp_path)
    monkeypatch.setattr(adapters, "LEDGER", tmp_path / "capacity-ledger.ndjson")
    monkeypatch.setenv("HANDOFF_DIR", str(tmp_path))
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "feedback.db")
    monkeypatch.setattr(exp_abcd, "EXP_DIR", tmp_path / "experiments")
    monkeypatch.setenv("ORCH_EXP_DIR", str(tmp_path / "experiments"))
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path))
    (tmp_path / "experiments").mkdir()


def _promotion_dir(name: str, *, evaluated_at: int, ttl_days: int = 14):
    """An experiment sitting in `evaluated`; no spec.md, so followup's discovery loop skips it."""
    edir = exp_abcd.EXP_DIR / name
    edir.mkdir()
    (edir / "meta.json").write_text(
        json.dumps({"schema_version": 2, "repo": "owner/repo", "exp_id": name, "agents": ["codex"]})
    )
    synthesis_promotion.ensure_evaluated_state(edir, now=evaluated_at, ttl_days=ttl_days)
    return edir


def _finished_dir(name: str, *, now: int):
    """A promotion that expired and was discarded on an earlier run, and held the gate then."""
    edir = _promotion_dir(name, evaluated_at=now - 20 * 86400)
    synthesis_promotion.reconcile(edir, now=now - 5 * 86400)
    assert synthesis_promotion.load_state(edir)["delivery_phase"] == "discarded"
    (edir / "ship-gate.json").write_text("{}\n")
    return edir


def _stamp(age_s: float):
    stamp = exp_abcd.ship_gate_stamp()
    stamp.touch()
    then = time.time() - age_s
    os.utime(stamp, (then, then))
    return stamp


def _launcher(calls: list):
    def fake_synthesize(repo, exp_id):
        calls.append(exp_id)
        return {"pid": 4242, "worktree": str(exp_abcd.EXP_DIR / "wt"), "run_id": f"{exp_id}:synth"}

    return fake_synthesize


def _followup(calls: list) -> dict:
    return exp_abcd.followup(
        max_experiments=0,
        synthesize_fn=_launcher(calls),
        promotion_completion_fn=lambda state: {"status": "pending"},
    )


def test_a_run_that_finishes_nothing_leaves_the_stamp_alone():
    now = int(time.time())
    _finished_dir("done", now=now)
    stamp = _stamp(2 * 3600)
    before = stamp.stat().st_mtime
    calls: list = []
    out = _followup(calls)
    # The mechanism of the latch: an already-finished promotion must not re-hold the gate.
    assert stamp.stat().st_mtime == before, "a finished promotion re-touched the ship-gate stamp"
    assert out["ship_gate"]["finished"] == 0
    assert out["ship_gate"]["evaluated"] == 0 and out["ship_gate"]["launchable"] == 0
    assert calls == []


def test_the_hold_is_per_finish_so_a_second_run_can_still_launch():
    """The production shape in miniature: finished promotions on disk, then a new candidate.

    Before the fix the first run re-touched the stamp, so the second run saw a fresh stamp and
    held — every hour, for 87 days. Now the first run leaves the old stamp old.
    """
    now = int(time.time())
    _finished_dir("done", now=now)
    _stamp(26 * 3600)
    calls: list = []
    _followup(calls)
    assert calls == []
    _promotion_dir("pending", evaluated_at=now - 3600)
    out = _followup(calls)
    assert calls == ["pending"], out["ship_gate"]
    assert out["ship_gate"] == {
        **out["ship_gate"],
        "evaluated": 1,
        "launchable": 1,
        "launched": 1,
        "finished": 0,
        "inflight": True,
    }


def test_an_old_stamp_releases_exactly_one_launch_and_a_launch_is_not_a_finish():
    now = int(time.time())
    _promotion_dir("first", evaluated_at=now - 7200)
    _promotion_dir("second", evaluated_at=now - 3600)
    stamp = _stamp(26 * 3600)
    before = stamp.stat().st_mtime
    calls: list = []
    out = _followup(calls)
    assert len(calls) == 1, calls
    gate = out["ship_gate"]
    assert gate["launch_available_at_start"] is True
    assert (gate["evaluated"], gate["launched"], gate["finished"]) == (2, 1, 0), gate
    # Only the first candidate seen could launch; the second found the window already used.
    assert gate["launchable"] == 1, gate
    assert stamp.stat().st_mtime == before, "a launch is not a finish and must not hold the gate"
    # While the synthesis is in flight nothing else launches, and the run says so.
    again = _followup(calls)
    assert len(calls) == 1
    assert again["ship_gate"]["inflight"] is True and again["ship_gate"]["launched"] == 0


def test_a_new_finish_holds_the_gate_once():
    now = int(time.time())
    # Evaluated 20 days ago with a 14-day TTL: this run is the one that discards it.
    edir = _promotion_dir("expired", evaluated_at=now - 20 * 86400)
    stamp = exp_abcd.ship_gate_stamp()
    assert not stamp.exists()
    calls: list = []
    out = _followup(calls)
    assert synthesis_promotion.load_state(edir)["delivery_phase"] == "discarded"
    assert out["ship_gate"]["finished"] == 1 and stamp.exists()
    assert json.loads((edir / "ship-gate.json").read_text())["verdict"] == "discard"
    held_at = stamp.stat().st_mtime
    # The same finish, seen again on the next run, does not hold a second time.
    again = _followup(calls)
    assert again["ship_gate"]["finished"] == 0
    assert stamp.stat().st_mtime == held_at
    assert calls == []


def test_a_new_finish_holds_later_candidates_in_the_same_run():
    now = int(time.time())
    pending = _promotion_dir("pending", evaluated_at=now - 3600)
    expired = _promotion_dir("expired", evaluated_at=now - 20 * 86400)
    # followup visits newest directories first: expire one before visiting the live candidate.
    os.utime(expired, (now, now))
    os.utime(pending, (now - 1, now - 1))
    _stamp(26 * 3600)
    calls: list = []

    out = _followup(calls)

    assert synthesis_promotion.load_state(expired)["delivery_phase"] == "discarded"
    assert calls == [], "a new finish must hold the gate before the next candidate is visited"
    assert synthesis_promotion.load_state(pending)["delivery_phase"] == "evaluated"
    assert out["ship_gate"]["finished"] == 1
    assert out["ship_gate"]["evaluated"] == 2


def test_a_held_evaluated_candidate_is_queued_without_inflight_work():
    _promotion_dir("held", evaluated_at=int(time.time()) - 3600)
    _stamp(3600)
    calls: list = []

    gate = _followup(calls)["ship_gate"]

    assert calls == []
    assert gate["evaluated"] == 1 and gate["launchable"] == 0
    assert gate["inflight"] is False


def test_zero_evaluated_summary_retains_finishes_and_inflight_work():
    line = exp_abcd.ship_gate_line(
        {
            "ship_gate": {
                "evaluated": 0,
                "launchable": 0,
                "launched": 0,
                "finished": 2,
                "inflight": True,
                "stamp_age_s": None,
            }
        }
    )

    assert "evaluated 0, launchable 0" in line
    assert "finished 2" in line and "inflight yes" in line


def test_the_summary_line_carries_both_numbers_and_never_spells_unmeasured_as_zero():
    held = {
        "ship_gate": {
            "stamp_age_s": 3600,
            "hold_s": exp_abcd.SHIP_GATE_HOLD_S,
            "evaluated": 3,
            "launchable": 0,
            "launched": 0,
            "finished": 0,
            "inflight": False,
        }
    }
    line = exp_abcd.ship_gate_line(held)
    assert "evaluated 3, launchable 0" in line and "stamp 1.0h old, holds 24h" in line
    drained = {"ship_gate": {**held["ship_gate"], "evaluated": 0}}
    assert "evaluated 0" in exp_abcd.ship_gate_line(drained)
    assert "nothing to launch" in exp_abcd.ship_gate_line(drained)
    # followup returns early, before the gate, when there is no experiments directory at all.
    unmeasured = exp_abcd.ship_gate_line({"processed": [], "promotions": []})
    assert "unmeasured" in unmeasured and "evaluated 0" not in unmeasured


@pytest.mark.parametrize("payload", ["{", '{"schema_version": -1}', "[]"])
def test_invalid_promotion_blocks_launches_but_other_promotions_reconcile(payload):
    now = int(time.time())
    pending = _promotion_dir("pending", evaluated_at=now - 3600)
    expired = _promotion_dir("expired", evaluated_at=now - 20 * 86400)
    broken = _promotion_dir("broken", evaluated_at=now - 3600)
    state_path = synthesis_promotion.state_path(broken)
    state_path.write_text(payload)
    os.utime(pending, (now, now))
    os.utime(expired, (now - 1, now - 1))
    _stamp(26 * 3600)
    calls: list = []

    out = _followup(calls)

    assert calls == [], "an unreadable promotion cannot prove synthesis launch safety"
    assert synthesis_promotion.load_state(pending)["delivery_phase"] == "evaluated"
    assert synthesis_promotion.load_state(expired)["delivery_phase"] == "discarded"
    failures = [row for row in out["promotions"] if row["exp_id"] == "broken"]
    assert len(failures) == 1 and failures[0]["error"]
    assert state_path.read_text() == payload, "preserve the invalid state for diagnosis"
    assert out["ship_gate"]["launchable"] == 0


@pytest.mark.parametrize("phase", ["candidate_ready", "delegated_or_pr", "merged"])
@pytest.mark.parametrize("delivery_first", [True, False])
def test_delivery_waiting_does_not_hold_synthesis_after_stamp_ages(phase, delivery_first):
    now = int(time.time())
    delivery = _promotion_dir("delivery", evaluated_at=now - 3600)
    state = synthesis_promotion.load_state(delivery)
    for next_phase in synthesis_promotion.DELIVERY_PHASES[1:]:
        state, _ = synthesis_promotion.transition(state, next_phase, reason="fixture", now=now)
        if next_phase == phase:
            break
    state["candidate_expires_ts"] = now + 86400
    state["retry"]["next_retry_ts"] = now + 3600
    synthesis_promotion.state_path(delivery).write_text(json.dumps(state))
    (delivery / "ship-gate.json").write_text("{}\n")
    pending = _promotion_dir("pending", evaluated_at=now - 3600)
    os.utime(delivery, (now if delivery_first else now - 1,) * 2)
    os.utime(pending, (now - 1 if delivery_first else now,) * 2)
    stamp = _stamp(26 * 3600)
    before = stamp.stat().st_mtime
    calls: list = []

    gate = _followup(calls)["ship_gate"]

    assert calls == ["pending"], "delivery waiting must not extend the synthesis hold"
    assert gate["evaluated"] == gate["launchable"] == gate["launched"] == 1
    assert gate["inflight"] is True
    assert synthesis_promotion.load_state(delivery)["delivery_phase"] == phase
    assert stamp.stat().st_mtime == before


@pytest.mark.parametrize("held", [False, True])
def test_new_evaluation_reports_counts_with_open_or_held_gate(held, monkeypatch):
    edir = exp_abcd.EXP_DIR / "new-evaluation"
    edir.mkdir()
    (edir / "meta.json").write_text(
        json.dumps({"repo": "owner/repo", "exp_id": edir.name, "agents": ["codex"]})
    )
    (edir / "spec.md").write_text("Implement a bounded change with a regression check.")
    log = edir / "codex.log"
    log.write_text("finished\n")
    os.utime(log, (time.time() - 3600,) * 2)
    _stamp(3600 if held else 26 * 3600)
    monkeypatch.setenv("ORCH_FOLLOWUP_SHIP_GATE", "1")
    monkeypatch.setenv("ORCH_RESEARCH_ARM", "1")
    calls: list = []
    evaluations: list = []

    def evaluate(repo, spec_path, exp_id, evaluators, timeout):
        evaluations.append(exp_id)
        return {"evaluators": evaluators, "objective_anchors": None}

    out = exp_abcd.followup(
        collect_fn=lambda *args: {
            "diffs": {"codex": {"bytes": 24, "diff": "diff --git a/a b/a\n+x\n"}}
        },
        evaluate_fn=evaluate,
        synthesize_fn=_launcher(calls),
        subject_lifecycle_fn=lambda *args, **kwargs: None,
        promotion_completion_fn=lambda state: {"status": "pending"},
    )

    assert evaluations == [edir.name], out
    assert out["ship_gate"]["evaluated"] == 1
    assert out["ship_gate"]["launchable"] == out["ship_gate"]["launched"] == int(not held)
    assert calls == ([] if held else [edir.name])
    assert synthesis_promotion.load_state(edir)["delivery_phase"] == (
        "evaluated" if held else "synth_running"
    )


def test_followup_cli_prints_one_summary_line_or_default_json(monkeypatch, capsys):
    result = _followup([])
    monkeypatch.setattr(exp_abcd, "followup", lambda **kwargs: result)

    assert exp_abcd.main(["followup", "--summary-line"]) == 0
    summary = capsys.readouterr().out
    assert len(summary.splitlines()) == 1
    assert "ship-gate: evaluated 0, launchable 0" in summary
    assert exp_abcd.main(["followup"]) == 0
    default = json.loads(capsys.readouterr().out)
    assert default["ship_gate"]["evaluated"] == default["ship_gate"]["launchable"] == 0
