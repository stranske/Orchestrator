"""The six-number fleet section: every figure carries its denominator, bots and the owner are excluded,
and what the Brain cannot answer is named as unmeasured rather than printed as zero."""

from __future__ import annotations

import time

import pytest

import feedback
import fleet_shapes
import periodic_report


@pytest.fixture
def brain(monkeypatch, tmp_path):
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path))
    return tmp_path


def _run(
    rid,
    agent,
    *,
    days_ago,
    merged=True,
    durability="durable",
    verifier="PASS",
    tokens=50_000,
    usd=1.0,
    source=None,
    cost_source="ledger",
):
    ts = int(time.time()) - days_ago * 86400
    feedback.record_run(
        rid,
        f"o/r#{rid}",
        "implement",
        agent,
        mode="remote",
        ts=ts,
        source="keepalive",
        routing_metadata={"attribution_source": source} if source else None,
    )
    feedback.record_outcome(
        rid,
        adjudicated_verdict="PASS",
        verifier_verdict=verifier,
        merged=merged,
        durability=durability,
    )
    # `cost_source` is the `costs.source` tag (ccusage/langsmith/ledger) — unrelated to `source`
    # above, which is routing-metadata attribution (bot/human). Default "ledger" matches
    # `record_cost`'s own default, so every pre-existing call site here is unaffected.
    feedback.record_cost(rid, tokens_in=tokens, tokens_out=0, cost_usd=usd, source=cost_source)


def test_fleet_six_numbers_and_exclusions(brain):
    _run("c1", "codex", days_ago=2)
    _run("c2", "codex", days_ago=10, durability="broke_later", verifier="FAIL")
    _run("c3", "codex", days_ago=12)
    _run("u1", "cursor", days_ago=3, tokens=200, usd=0.01)
    _run("n1", "none", days_ago=4, verifier="")  # agent work, unattributed
    _run("b1", "none", days_ago=1, source="bot:template-sync")
    _run("h1", "none", days_ago=1, source="human")
    s = periodic_report._fleet_six_summary()
    w7, w28 = s["windows"]["7"], s["windows"]["28"]
    assert w7["merged_per_day"] == {
        "value": round(3 / 7, 2),
        "merged": 3,
        "merged_including_bots_and_owner": 5,
    }
    assert w28["merged_per_day"]["merged"] == 5
    assert w7["verifier_pass_rate"] == {
        "value": 1.0,
        "passed": 2,
        "with_verdict": 2,
        "merged_without_verdict": 1,
    }
    assert (
        w28["verifier_pass_rate"]["passed"] == 3 and w28["verifier_pass_rate"]["with_verdict"] == 4
    )
    assert w28["broke_later_by_agent"] == {"codex": {"resolved": 2, "bad": 1, "bad_rate": 0.5}}
    assert "unmeasured" in w7["broke_later_by_agent"]
    assert w28["cost_per_merged_by_agent"]["codex"]["merged"] == 3
    assert w28["cost_per_merged_by_agent"]["cursor"]["telemetry"].startswith("implausible")
    assert "none" not in {a for a in w28["cost_per_merged_by_agent"] if a in ("bot", "human")}
    assert w7["unattributed_share"] == {"value": round(1 / 3, 3), "unresolved": 1, "merged": 3}
    assert (
        s["time_to_merge"]["value"] is None
        and "no merge timestamp" in s["time_to_merge"]["unmeasured"]
    )


def test_render_and_markdown(brain):
    _run("c1", "codex", days_ago=2)
    s = periodic_report._fleet_six_summary()
    lines = periodic_report.render_fleet_six(s)
    assert len(lines) == 6 and all(line.startswith("FLEET-6 ") for line in lines)
    assert "time-to-merge: unmeasured" in lines[5]
    path = periodic_report.write_fleet_six_md(s)
    assert path == brain / "fleet-six.md"
    text = path.read_text()
    assert text.startswith("# Fleet six") and text.count("- FLEET-6") == 6


def test_empty_brain_is_named_not_zeroed(brain):
    s = periodic_report._fleet_six_summary()
    w7 = s["windows"]["7"]
    assert w7["merged_per_day"]["merged"] == 0
    assert (
        w7["verifier_pass_rate"]["value"] is None
        and "no verifier verdict" in w7["verifier_pass_rate"]["unmeasured"]
    )
    assert "unmeasured" in w7["broke_later_by_agent"]
    assert w7["unattributed_share"]["value"] is None
    lines = periodic_report.render_fleet_six(s)
    assert any("unmeasured" in line for line in lines[1:5])


def test_time_to_merge_reads_the_shape_facts(brain):
    now = int(time.time())
    _run("1", "codex", days_ago=1)
    _run("2", "codex", days_ago=2)
    fleet_shapes.save_facts(
        brain,
        {
            "o/r#1": {"created_ts": now - 86400 - 7200, "merged_ts": now - 86400},
            "o/r#2": {"created_ts": now - 2 * 86400 - 14400, "merged_ts": now - 2 * 86400},
        },
        now=now,
    )
    summary = periodic_report._fleet_six_summary(now=now)["time_to_merge"]["windows"]["7"]
    assert summary["codex"] == {
        "median_hours": 3.0,
        "p90_hours": 3.8,
        "n_with_facts": 2,
        "n_merged": 2,
    }
    assert (
        "codex median 3.0 h, p90 3.8 h (2/2 facts)"
        in periodic_report.render_fleet_six(periodic_report._fleet_six_summary(now=now))[5]
    )
    _run("3", "codex", days_ago=3)
    partial = periodic_report._fleet_six_summary(now=now)["time_to_merge"]["windows"]["7"]
    assert partial["codex"]["n_with_facts"] == 2
    assert partial["codex"]["n_merged"] == 3


def test_time_to_merge_names_the_absence_without_a_cache(brain):
    _run("c1", "codex", days_ago=1)
    summary = periodic_report._fleet_six_summary()
    assert "no merge timestamp" in summary["time_to_merge"]["unmeasured"]
    assert "time-to-merge: unmeasured" in periodic_report.render_fleet_six(summary)[5]


def test_time_to_merge_handles_unmeasured_second_window(brain):
    section = {
        "windows": {"7": {}, "28": {}},
        "time_to_merge": {
            "windows": {
                "7": {
                    "codex": {
                        "median_hours": 2.0,
                        "p90_hours": 2.0,
                        "n_with_facts": 1,
                        "n_merged": 1,
                    }
                },
                "28": {"value": None, "unmeasured": "facts unavailable"},
            }
        },
    }
    assert "28d unmeasured (facts unavailable)" in periodic_report.render_fleet_six(section)[5]


def test_time_to_merge_uses_one_facts_snapshot(brain, monkeypatch):
    now = int(time.time())
    _run("4", "codex", days_ago=1)
    fleet_shapes.save_facts(
        brain,
        {"o/r#4": {"created_ts": now - 86400 - 7200, "merged_ts": now - 86400}},
        now=now,
    )
    load = fleet_shapes.load_facts
    reads = []

    def count_load(state_dir):
        reads.append(state_dir)
        return load(state_dir)

    monkeypatch.setattr(fleet_shapes, "load_facts", count_load)
    summary = periodic_report._fleet_six_summary(now=now)["time_to_merge"]["windows"]
    assert len(reads) == 1
    assert summary["7"]["codex"]["median_hours"] == 2.0
    assert summary["28"]["codex"]["median_hours"] == 2.0


def test_cost_per_merged_uses_one_cost_scale(brain):
    """$/merged must rest on feedback.COMPLETE_COST_SOURCES alone (2026-09-22) — the same scale
    the route-weight learners use since PR #320 — never a blend with a LangSmith per-call trace
    or a bare ledger row. Unfiltered, 3 merged codex runs (two $1.00 ccusage rows, one $0.04
    langsmith row) sum to $2.04 over 3 merges = $0.68/merged: a real number, but priced on two
    different scales at once. Filtered to the two ccusage rows alone: $2.00/2 = $1.00/merged,
    with coverage (2 complete rows of 3 merged runs = 67%) printed beside it, never folded in.
    """
    _run("s1", "codex", days_ago=1, usd=1.0, cost_source="ccusage")
    _run("s2", "codex", days_ago=1, usd=1.0, cost_source="ccusage")
    _run("s3", "codex", days_ago=1, usd=0.04, cost_source="langsmith")
    s = periodic_report._fleet_six_summary()
    cell = s["windows"]["28"]["cost_per_merged_by_agent"]["codex"]
    assert cell["merged"] == 3, cell
    assert cell["complete_rows"] == 2, cell
    assert cell["coverage"] == round(2 / 3, 4), cell
    assert cell["usd_per_merged"] == 1.00, cell
    assert "unmeasured" not in cell, cell

    lines = periodic_report.render_fleet_six(s)
    cost_line = next(line for line in lines if line.startswith("FLEET-6 cost per merged"))
    assert "codex $1.00/merged (3 merged, coverage 67%)" in cost_line, cost_line
    assert "0.68" not in cost_line, cost_line


def test_cost_per_merged_below_coverage_floor_is_unmeasured(brain):
    """feedback.MIN_COST_COVERAGE is 0.25. One ccusage row of 5 merged runs is 20% coverage —
    genuinely under the floor (not the 1-of-4 = 25% boundary, which would equal it rather than
    fall short) — so the report must print `unmeasured` naming the shortfall instead of a dollar
    figure computed from a single priced row standing in for four unpriced ones."""
    _run("u1", "codex", days_ago=1, usd=2.0, cost_source="ccusage")
    for rid in ("u2", "u3", "u4", "u5"):
        _run(rid, "codex", days_ago=1)  # cost_source defaults to "ledger" — partial, excluded

    s = periodic_report._fleet_six_summary()
    cell = s["windows"]["28"]["cost_per_merged_by_agent"]["codex"]
    assert cell["merged"] == 5, cell
    assert cell["complete_rows"] == 1, cell
    assert cell["coverage"] == round(1 / 5, 4), cell
    assert cell["usd_per_merged"] is None, cell
    assert "coverage 20% < 25%" in cell["unmeasured"], cell

    lines = periodic_report.render_fleet_six(s)
    cost_line = next(line for line in lines if line.startswith("FLEET-6 cost per merged"))
    assert "codex unmeasured (coverage 20% < 25%" in cost_line, cost_line
