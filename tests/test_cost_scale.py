"""One cost scale for the route-weight learners (item E, 2026-09-22).

Until v65 the learners compared codex's ccusage session totals ($0.32 and 2.55M tokens per run, priced
at API list rates) with cursor's 76 LangSmith traces ($0.04 and 9.7k tokens, one call inside a run,
4% of its runs) as if both were the price of a run, and ranked cursor first for implementation while
its merged work broke later three times as often. The rule now: a cost enters the scale only from a
source that prices the WHOLE run (`feedback.COMPLETE_COST_SOURCES`), only when such rows cover at
least `feedback.MIN_COST_COVERAGE` of the agent's local runs, and a priced cost subsumes the flat token
term. A partial or sparse number is UNMEASURED, never cheap; the rationale names the reason and share.
An unmeasured agent is imputed from the measured cells of its own task-type row, and the cost term is
charged in row units (dollars over the row's mean measured cost) so the tuned penalty survives the
change of currency: the first honest preview read codex implement at $18.64 a run against $0.30 cells.
"""

from __future__ import annotations

import math

import pytest

import feedback

PRIORS = {"implement": {"codex": 0.5, "cursor": 0.5}}


@pytest.fixture
def brain(monkeypatch, tmp_path):
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "brain.db")
    monkeypatch.delenv("ORCH_RELEARN_FLEET_ROWS", raising=False)
    return tmp_path


def _run(
    rid: str,
    agent: str,
    *,
    cost: float | None = None,
    source: str = "ccusage",
    tokens: int = 50_000,
    latency: float = 120.0,
) -> None:
    feedback.record_run(rid, f"stranske/Repo#{rid[-3:]}", "implement", agent, mode="local")
    feedback.record_outcome(rid, adjudicated_verdict="PASS", merged=True, durability="durable")
    if cost is None:
        feedback.record_cost(rid, latency_s=latency, source="ledger")  # the dispatcher's own row
    else:
        feedback.record_cost(
            rid,
            tokens_in=tokens // 2,
            tokens_out=tokens - tokens // 2,
            cost_usd=cost,
            latency_s=latency,
            source=source,
        )


def _cell(version: int, agent: str) -> dict:
    with feedback._conn() as c:
        row = c.execute(
            "SELECT posterior, score, rationale, cost_per_success FROM route_weights "
            "WHERE version=? AND task_type='implement' AND agent=?",
            (version, agent),
        ).fetchone()
    return {"post": row[0], "score": row[1], "rationale": row[2], "cps": row[3]}


def test_partial_trace_cost_is_unmeasured_not_cheap(brain):
    for i in range(10):
        _run(f"cx{i:03d}", "codex", cost=0.30, source="ccusage", tokens=2_000_000)
    for i in range(10):
        _run(f"cu{i:03d}", "cursor", cost=0.04, source="langsmith", tokens=9_000)
    v = feedback.relearn_quality(PRIORS)
    cursor, codex = _cell(v, "cursor"), _cell(v, "codex")
    assert "cost unmeasured: no complete-source rows (ccusage)" in cursor["rationale"]
    assert "10 partial rows (trace/ledger) excluded" in cursor["rationale"]
    # cursor's cost and tokens are IMPUTED from the measured cells of ITS OWN ROW, not read as $0.04
    assert "mean_cost=0.3000" in cursor["rationale"] and "effort_src=rr" in cursor["rationale"]
    assert "telemetry=ok" in codex["rationale"] and "effort_src=mm" in codex["rationale"]
    assert f"cost_scale={feedback.COST_SCALE}" in cursor["rationale"]


def test_low_coverage_is_unmeasured_and_names_the_share(brain):
    for i in range(10):
        _run(f"cx{i:03d}", "codex", cost=(0.30 if i < 2 else None))
    v = feedback.relearn_quality(PRIORS)
    rationale = _cell(v, "codex")["rationale"]
    assert "cost coverage 20% < 25%" in rationale
    assert "2 complete-source rows of 10 telemetry-eligible runs" in rationale
    assert "cost_cov=20%" in rationale


def test_complete_source_cost_is_measured_with_its_coverage(brain):
    for i in range(10):
        _run(f"cx{i:03d}", "codex", cost=(0.30 if i < 8 else None))
    v = feedback.relearn_quality(PRIORS)
    cell = _cell(v, "codex")
    assert "telemetry=ok" in cell["rationale"] and "cost_cov=80%" in cell["rationale"]
    assert "effort_src=mm" in cell["rationale"]
    assert cell["cps"] == pytest.approx(0.30)  # the column stays the MEASURED mean, never imputed


def test_token_term_is_subsumed_when_cost_is_priced(brain):
    for i in range(10):
        _run(f"cx{i:03d}", "codex", cost=0.30, tokens=2_000_000, latency=180.0)
    v = feedback.relearn_quality(PRIORS)
    cell = _cell(v, "codex")
    # the only measured agent IS the row unit, so its cost is exactly 1.0 unit
    expected = cell["post"] * math.exp(
        -(feedback.LAMBDA_COST * 1.0 + feedback.LAMBDA_LATENCY_MIN * 3.0)
    )
    assert cell["score"] == pytest.approx(expected, rel=1e-6)
    assert "tokens_term=subsumed" in cell["rationale"]
    assert "cost_units=1.00" in cell["rationale"] and "cost_unit_usd=0.3000" in cell["rationale"]


def test_token_term_is_the_proxy_only_when_no_cost_exists_anywhere(brain):
    for i in range(10):
        _run(
            f"cx{i:03d}", "codex", cost=0.0, tokens=1_000_000, latency=60.0
        )  # priced at $0: unknown
    v = feedback.relearn_quality(PRIORS)
    cell = _cell(v, "codex")
    assert "tokens_term=proxy" in cell["rationale"] and "mean_tokens=1000000" in cell["rationale"]
    assert cell["score"] == pytest.approx(
        cell["post"]
        * math.exp(-(feedback.LAMBDA_TOKEN_MTOK * 1.0 + feedback.LAMBDA_LATENCY_MIN * 1.0)),
        rel=1e-6,
    )


def test_legacy_relearn_reads_only_complete_sources(brain):
    for i in range(10):
        _run(f"cx{i:03d}", "codex", cost=0.30, source="ccusage")
    for i in range(10):
        _run(f"cu{i:03d}", "cursor", cost=0.04, source="langsmith")
    v = feedback.relearn(PRIORS)
    codex, cursor = _cell(v, "codex"), _cell(v, "cursor")
    assert "cps_src=measured" in codex["rationale"] and "cost_cov=100%" in codex["rationale"]
    assert cursor["cps"] is None  # nothing MEASURED for cursor: the column is never fabricated
    assert "cps_src=global_median" in cursor["rationale"]
    assert "cost unmeasured" in cursor["rationale"]


def test_partial_rows_never_dilute_a_measured_agent(brain):
    """codex in the live Brain: 428 ccusage rows at ~$1.11 beside 705 LangSmith trace rows at $0.04.
    The agent is measured (coverage above the bar), so the per-row filter is what keeps the partial
    rows out of its mean — in both learners."""
    for i in range(8):
        _run(f"cx{i:03d}", "codex", cost=0.30, source="ccusage")
    for i in range(8, 12):
        _run(f"cx{i:03d}", "codex", cost=0.04, source="langsmith")
    v_legacy = feedback.relearn(PRIORS)
    assert _cell(v_legacy, "codex")["cps"] == pytest.approx(0.30)  # not the blended 0.213
    assert "cost_cov=67%" in _cell(v_legacy, "codex")["rationale"]
    v_quality = feedback.relearn_quality(PRIORS)
    cell = _cell(v_quality, "codex")
    assert cell["cps"] == pytest.approx(0.30) and "mean_cost=0.3000" in cell["rationale"]
