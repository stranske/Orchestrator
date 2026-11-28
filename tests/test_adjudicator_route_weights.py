"""The lane judges' backend pick reads the weights their graded runs feed (2026-11-28).

The closer and opener judges call roles.route_role("adjudicator") and record the run as
task_type "review"; review_outcomes.py grades those runs and the weekly relearn folds the grades
into the `review` weights. route_role used to read `role:adjudicator` first, a bucket nothing
writes outcomes into, so its seeded priors (0 observations) decided every judge pick.
"""

from __future__ import annotations

import pytest

import feedback
import roles
import router

CAP = {
    "agents": {
        "codex": {"state": "ok"},
        "gemini": {"state": "ok"},
        "claude": {"state": "ok"},
        "muse": {"state": "shed"},
    }
}


@pytest.fixture(autouse=True)
def isolated_runtime(monkeypatch, tmp_path):
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ORCH_LOCAL_RUNTIME", str(tmp_path / "runtime"))
    monkeypatch.setenv("ORCH_MODEL_PROBE", "0")
    monkeypatch.delenv("ORCH_CAPABILITY_HEARTBEATS", raising=False)
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "runtime" / "brain.db")
    monkeypatch.setattr(feedback, "current_weights", lambda *args, **kwargs: [])

    def refuse():
        raise AssertionError("pass cap=")

    monkeypatch.setattr(router, "load_capacity", refuse)


def _ranked(*agents, n_obs):
    return {agent: {"rank": i, "n_obs": n_obs[agent]} for i, agent in enumerate(agents)}


SEEDED_PRIORS = _ranked("gemini", "codex", "claude", n_obs={"gemini": 0, "codex": 0, "claude": 0})


@pytest.mark.parametrize(
    ("review_order", "chosen", "n_obs"),
    [(("codex", "gemini", "claude"), "codex", 433), (("gemini", "codex", "claude"), "gemini", 121)],
)
def test_the_judge_pick_follows_the_review_weights_not_the_seeded_role_priors(
    review_order, chosen, n_obs
):
    learned = {
        "review": _ranked(*review_order, n_obs={"codex": 433, "gemini": 121, "claude": 104}),
        "role:adjudicator": SEEDED_PRIORS,
    }
    pick = roles.route_role("adjudicator", cap=CAP, learned=learned, exploration_rate=0.0)
    assert pick["agent"] == chosen
    assert (pick["learned_bucket"], pick["learned_n_obs"]) == ("review", n_obs)


def test_the_judge_never_reads_the_role_bucket_from_the_brain(monkeypatch):
    asked = []

    def weights(task_type, *args, **kwargs):
        asked.append(task_type)
        return [{"agent": "gemini", "posterior": 0.55, "score": 0.55, "n_obs": 0}]

    monkeypatch.setattr(feedback, "current_weights", weights)
    learned = {"review": _ranked("codex", "gemini", n_obs={"codex": 433, "gemini": 121})}
    pick = roles.route_role("adjudicator", cap=CAP, learned=learned, exploration_rate=0.0)
    assert pick["agent"] == "codex" and "role:adjudicator" not in asked


def test_other_roles_keep_their_own_bucket_first():
    learned = {
        "role:redirect": _ranked("gemini", "codex", n_obs={"gemini": 0, "codex": 0}),
        "review": _ranked("codex", "gemini", n_obs={"codex": 433, "gemini": 121}),
    }
    pick = roles.route_role("redirect", cap=CAP, learned=learned, exploration_rate=0.0)
    assert (pick["agent"], pick["learned_bucket"], pick["learned_n_obs"]) == (
        "gemini",
        "role:redirect",
        0,
    )


def test_a_pick_with_no_learned_weights_names_no_bucket():
    pick = roles.route_role("adjudicator", cap=CAP, learned={}, exploration_rate=0.0)
    assert pick["learned_bucket"] is None and pick["learned_n_obs"] == 0


def test_the_run_record_keeps_the_bucket_that_ranked_a_role_pick():
    learned = {"review": _ranked("codex", "gemini", n_obs={"codex": 433, "gemini": 121})}
    pick = roles.route_role("adjudicator", cap=CAP, learned=learned, exploration_rate=0.0)
    record = router.route_decision_record(pick)
    assert record["chosen"] == "codex"
    assert (record["learned_bucket"], record["learned_n_obs"]) == ("review", 433)


def test_a_plain_pick_without_a_draw_or_a_bucket_still_records_nothing():
    assert router.route_decision_record({"agent": "codex", "route_source": "route_weights"}) is None
