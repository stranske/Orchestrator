"""relearn() must not let near-empty cost telemetry read as cheap work.

Live Brain, 2026-09-15: cursor's cost rows averaged ~209 tokens per run against codex's millions,
every row with cost_usd > 0, so relearn() scored cursor's implementation cell on a cost_per_success of
0.057 and ranked it first — while its merged work regressed three times as often as codex's. The
2026-07-03 rule covered ABSENT telemetry ("missing cost must not read as free"); this covers IMPLAUSIBLE
telemetry the same way: the agent's cells are unmeasured and the existing imputation applies.
"""

from __future__ import annotations

import time

import pytest

import feedback

PRIORS = {"implement": {"codex": 0.6, "cursor": 0.6}}


def _seed(tokens_per_run: dict[str, int], cost_per_run: dict[str, float], runs: int = 6) -> None:
    now = int(time.time())
    for agent, toks in tokens_per_run.items():
        for i in range(runs):
            rid = f"{agent}-{i}"
            feedback.record_run(
                rid, f"o/r#{i}", "implement", agent, mode="remote", ts=now - 3600 * (i + 1)
            )
            feedback.record_outcome(
                rid, adjudicated_verdict="PASS", merged=True, durability="durable"
            )
            feedback.record_cost(rid, tokens_in=toks, tokens_out=0, cost_usd=cost_per_run[agent])


@pytest.fixture
def brain(monkeypatch, tmp_path):
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "t.db")
    return tmp_path


def _weights():
    return {row["agent"]: row for row in feedback.current_weights("implement")}


def _rationale(agent: str) -> str:
    with feedback._conn() as c:
        row = c.execute(
            "SELECT rationale FROM route_weights WHERE task_type='implement' AND agent=? "
            "ORDER BY version DESC LIMIT 1",
            (agent,),
        ).fetchone()
    return str(row[0]) if row else ""


def test_implausible_telemetry_is_imputed_not_measured(brain):
    _seed({"codex": 400_000, "cursor": 200}, {"codex": 2.0, "cursor": 0.01})
    feedback.relearn(PRIORS, window_days=30)
    w = _weights()
    assert "telemetry implausible" in _rationale("cursor") and "median 200" in _rationale("cursor")
    assert "telemetry implausible" not in _rationale("codex")
    # cursor's stored cost_per_success is NULL (nothing fabricated), and its score is computed on the
    # imputed cps — codex's — so equal success rates give equal scores instead of a 200x gap.
    with feedback._conn() as c:
        cps = dict(
            c.execute(
                "SELECT agent, cost_per_success FROM route_weights WHERE task_type='implement' "
                "AND version=(SELECT MAX(version) FROM route_weights)"
            ).fetchall()
        )
    assert cps["cursor"] is None and cps["codex"] == pytest.approx(2.0)
    assert w["cursor"]["score"] == pytest.approx(w["codex"]["score"])


def test_real_telemetry_is_unchanged(brain):
    _seed({"codex": 400_000, "cursor": 50_000}, {"codex": 2.0, "cursor": 0.5})
    feedback.relearn(PRIORS, window_days=30)
    w = _weights()
    assert "telemetry implausible" not in _rationale("cursor")
    assert w["cursor"]["score"] > w["codex"]["score"]  # genuinely cheaper, and measured as such


def test_deliberate_break_rule_disabled_restores_the_inflated_score(brain, monkeypatch):
    monkeypatch.setattr(feedback, "PLAUSIBLE_TOKENS_PER_RUN", 0)
    _seed({"codex": 400_000, "cursor": 200}, {"codex": 2.0, "cursor": 0.01})
    feedback.relearn(PRIORS, window_days=30)
    w = _weights()
    assert w["cursor"]["score"] > 50 * w["codex"]["score"]
    assert "telemetry implausible" not in _rationale("cursor")


def test_costs_without_token_telemetry_stay_measured(brain):
    """A ledger cost that reports no tokens at all says nothing about plausibility."""
    _seed({"codex": 400_000, "cursor": 0}, {"codex": 2.0, "cursor": 0.5})
    feedback.relearn(PRIORS, window_days=30)
    assert "telemetry implausible" not in _rationale("cursor")
    with feedback._conn() as c:
        cps = dict(
            c.execute(
                "SELECT agent, cost_per_success FROM route_weights WHERE task_type='implement' "
                "AND version=(SELECT MAX(version) FROM route_weights)"
            ).fetchall()
        )
    assert cps["cursor"] == pytest.approx(0.5)
