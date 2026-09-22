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
    feedback.record_cost(rid, tokens_in=tokens, tokens_out=0, cost_usd=usd)


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
