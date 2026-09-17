"""The lane relay's receiver choice, as a rail: learned order only where the ground truth agrees.

Until 2026-09-15 the relay chose "codex unless shed, else claude" and the learner's weights reached
no live decision. The weights, when read, ranked cursor first for implementation on implausible cost
telemetry while cursor's merged work regressed three times as often as codex's — so following them
blindly would have routed implementation to the agent with the worst durability. These tests pin the
gate: a learned candidate replaces the default only with RECEIVER_MIN_N resolved merges and a
broke-later rate within RECEIVER_DURABILITY_TOLERANCE of the default's.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time

import pytest

import feedback
import paths
import router


@pytest.fixture
def brain(monkeypatch, tmp_path):
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(router, "CAPACITY_JSON", tmp_path / "capacity.json")
    return tmp_path


def _cap(**states):
    return {"agents": {a: {"state": s} for a, s in states.items()}}


def _learned(order):
    return {"implement": {a: {"rank": i, "n_obs": 50} for i, a in enumerate(order)}}


def _dur(**cells):
    return {a: {"resolved": n, "bad": b, "bad_rate": b / n} for a, (n, b) in cells.items()}


def test_default_rule_without_learned_weights(brain):
    r = router.recommend_receiver("implement", cap=_cap(codex="ok", claude="ok"), learned={})
    assert (r["agent"], r["source"]) == ("codex", "default")


def test_shed_default_falls_to_next_default(brain):
    r = router.recommend_receiver("implement", cap=_cap(codex="shed", claude="ok"), learned={})
    assert r["agent"] == "claude"
    r = router.recommend_receiver("implement", cap=_cap(codex="shed", claude="shed"), learned={})
    assert r["agent"] is None and "shed" in r["reason"]


def test_learned_candidate_with_worse_durability_is_refused(brain):
    r = router.recommend_receiver(
        "implement",
        cap=_cap(codex="ok", claude="ok", cursor="ok"),
        allowed=("codex", "claude", "cursor"),
        learned=_learned(["cursor", "claude", "codex"]),
        durability=_dur(cursor=(31, 7), codex=(221, 15)),
    )
    assert (r["agent"], r["source"]) == ("codex", "default")
    assert "22.6%" in r["reason"] and "6.8%" in r["reason"]


def test_learned_candidate_with_too_few_merges_is_refused(brain):
    r = router.recommend_receiver(
        "implement",
        cap=_cap(codex="ok", claude="ok"),
        learned=_learned(["claude", "codex"]),
        durability=_dur(claude=(4, 0), codex=(221, 15)),
    )
    assert r["agent"] == "codex" and "< 20" in r["reason"]


def test_learned_candidate_with_equal_durability_is_followed(brain):
    r = router.recommend_receiver(
        "implement",
        cap=_cap(codex="ok", claude="ok"),
        learned=_learned(["claude", "codex"]),
        durability=_dur(claude=(40, 2), codex=(221, 15)),
    )
    assert (r["agent"], r["source"]) == ("claude", "learned")


def test_guard_is_the_only_thing_refusing_cursor(brain, monkeypatch):
    """Deliberate break: with the tolerance widened to 100% the worse agent is followed."""
    monkeypatch.setattr(router, "RECEIVER_DURABILITY_TOLERANCE", 1.0)
    r = router.recommend_receiver(
        "implement",
        cap=_cap(codex="ok", cursor="ok"),
        allowed=("codex", "cursor"),
        learned=_learned(["cursor", "codex"]),
        durability=_dur(cursor=(31, 7), codex=(221, 15)),
    )
    assert r["agent"] == "cursor"


def test_durability_table_excludes_bots_and_the_owner(brain):
    now = int(time.time())
    old = now - 20 * 86400
    feedback.record_run(
        "a1", "o/r#1", "implement", "codex", mode="remote", ts=old, source="keepalive"
    )
    feedback.record_outcome("a1", adjudicated_verdict="PASS", merged=True, durability="durable")
    feedback.record_run(
        "a2", "o/r#2", "implement", "codex", mode="remote", ts=old, source="keepalive"
    )
    feedback.record_outcome("a2", adjudicated_verdict="PASS", merged=True, durability="broke_later")
    feedback.record_run(
        "b1",
        "o/r#3",
        "implement",
        "none",
        mode="remote",
        ts=old,
        source="keepalive",
        routing_metadata={"attribution_source": "bot:template-sync"},
    )
    feedback.record_outcome("b1", adjudicated_verdict="PASS", merged=True, durability="durable")
    feedback.record_run(
        "p1", "o/r#4", "implement", "codex", mode="remote", ts=old, source="keepalive"
    )
    feedback.record_outcome("p1", adjudicated_verdict="PASS", merged=True, durability="pending")
    table = router.merged_durability_by_agent(now=now)
    assert table == {"codex": {"resolved": 2, "bad": 1, "bad_rate": 0.5}}


def test_cli_prints_json_for_the_relay(brain):
    (brain / "capacity.json").write_text(json.dumps(_cap(codex="shed", claude="ok")))
    env = {
        "ORCH_LOCAL_RUNTIME": str(brain),
        "ORCH_FEEDBACK_DB": str(brain / "t.db"),
        "HANDOFF_DIR": str(brain),
        "PATH": "/usr/bin:/bin:/opt/anaconda3/bin",
    }
    out = subprocess.run(
        [
            sys.executable,
            str(paths.MODULE_DIR / "router.py"),
            "--recommend-receiver",
            "implement",
            "--allowed",
            "codex,claude",
        ],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(paths.REPO_ROOT),
    )
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout)["agent"] == "claude"


def test_durability_table_ignores_rows_judged_before_broke_later_existed(brain, monkeypatch):
    """A row whose durability was checked before the sweep could produce broke_later holds "durable"
    under a weaker definition (nothing looked for the fix PR), so the rail must not count it.
    Deliberate break: with the floor at 0 the old row is counted again — the floor is what excludes it.
    """
    now = int(time.time())
    old = now - 20 * 86400
    for run_id, target in (("j1", "o/r#11"), ("j2", "o/r#12")):
        feedback.record_run(
            run_id, target, "implement", "codex", mode="remote", ts=old, source="keepalive"
        )
        feedback.record_outcome(
            run_id, adjudicated_verdict="PASS", merged=True, durability="durable"
        )
    judged_before_detection = router.DURABILITY_DETECTION_SINCE - 86400
    with feedback._conn() as c:
        c.execute(
            "UPDATE outcomes SET durability_checked_ts=? WHERE run_id='j2'",
            (judged_before_detection,),
        )
    table = router.merged_durability_by_agent(now=now)
    assert table == {"codex": {"resolved": 1, "bad": 0, "bad_rate": 0.0}}
    monkeypatch.setattr(router, "DURABILITY_DETECTION_SINCE", 0)
    assert router.merged_durability_by_agent(now=now)["codex"]["resolved"] == 2


def test_every_verdict_names_its_population(brain):
    r = router.recommend_receiver("implement", cap=_cap(codex="ok", claude="ok"), learned={})
    assert router.DURABILITY_DETECTION_SINCE_DATE in r["population"]
    assert ">=7d" in r["population"] and "90d" in r["population"]
    r = router.recommend_receiver(
        "implement",
        cap=_cap(codex="ok", claude="ok"),
        learned=_learned(["claude", "codex"]),
        durability=_dur(claude=(40, 2), codex=(221, 15)),
    )
    assert r["source"] == "learned" and r["population"] == router.receiver_population()
