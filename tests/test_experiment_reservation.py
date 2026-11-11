"""Opener intake reservation (owner decision 2026-11-11): experiments get subjects at the design rate."""

import random

import pytest

import experiment_producer as ep

NOW = 1_800_000_000.0
BODY = "## Tasks\n- [ ] x\n\n## Acceptance Criteria\n- [ ] `pytest tests/test_x.py` passes\n"
OK_CAP = {
    "agents": {"codex": {"state": "ok"}, "claude": {"state": "ok"}, "cursor": {"state": "ok"}}
}


@pytest.fixture(autouse=True)
def state_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("ORCH_EXPERIMENT_TRIAL_END", "0")
    return tmp_path


def offer(target, now=NOW, body=BODY, cap=OK_CAP):
    return ep.reservation_offer(target, body, now=now, rate_per_day=2, cap=cap)


def test_reservation_takes_one_issue_per_token_at_the_design_rate():
    assert offer("o/r#1")["take"] is True
    assert offer("o/r#2", NOW + 60) == {"take": False, "reason": "no_token", "tokens": 0.001}
    assert offer("o/r#2", NOW + 12 * 3600)["take"] is True  # 2/day: one token every 12 h
    assert (
        offer("o/r#2", NOW + 12 * 3600 + 5)["reason"] == "already_reserved"
    )  # still inside its 3 h hold
    state = ep.read_state()
    assert state["reserve_offers"] == {"taken": 2, "no_token": 1}


def test_tokens_bank_up_to_the_daily_cap_only():
    offer("o/r#1")
    taken = [
        offer(f"o/r#{n}", NOW + 10 * 86400 + n)["take"] for n in range(2, 2 + ep.DAILY_CAP + 2)
    ]
    assert taken == [True] * ep.DAILY_CAP + [False, False]


def test_refusals_name_the_binding_limit():
    assert offer("o/r#1", body="## Tasks\n- [ ] x\n")["reason"] == "no_named_test"
    one_ok = {"agents": {"codex": {"state": "ok"}, "claude": {"state": "cutoff"}}}
    assert offer("o/r#1", cap=one_ok)["reason"] == "pacing ok=1"
    ep.save_state({"trial_verdict": "fail"})
    assert offer("o/r#1")["reason"] == "trial_hold"


def test_a_reserved_subject_fires_without_the_bernoulli_draw_but_pacing_still_holds():
    rng = random.Random(0)
    assert ep.draw(NOW, NOW, 2, 0, rng, force=True)["fire"] is True
    assert ep.draw(NOW, NOW, 2, 0, rng, force=False)["fire"] is False  # zero elapsed: p = 0
    held = ep.draw(NOW, NOW, 2, 0, rng, pools={"codex": "shed"}, force=True)
    assert held["fire"] is False and held["reason"].startswith("pacing")
    assert ep.draw(NOW, NOW, 2, ep.DAILY_CAP, rng, force=True)["reason"] == "daily_cap"


def test_choose_subject_prefers_the_reserved_issue():
    feed = [{"target": "o/r#1", "labels": []}, {"target": "o/r#2", "labels": [ep.RESERVED]}]
    assert ep.choose_subject(feed, [], [], random.Random(0)) == {
        "subject": feed[1],
        "reason": "reserved",
    }
