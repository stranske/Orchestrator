from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import capabilities
import capability_outcome_bridge as bridge
import capability_propensity as propensity
import feedback


def _ledger(tmp_path):
    path = tmp_path / "capabilities.json"
    cap = capabilities._blank_capability("offload")
    cap["status"] = "generated"
    cap["capability_version_id"] = "capability-version:" + "a" * 32
    capabilities.save({"offload": cap}, path)
    return path


def _keepalive(monkeypatch, tmp_path, *, run_id="keepalive:o/r#7:codex", target="o/r#7"):
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "brain.db")
    feedback.record_run(run_id, target, "implement", "codex", source="keepalive")
    feedback.record_outcome(run_id, adjudicated_verdict="PASS", merged=True, durability="durable")


def test_lane_verdict_with_deliverable_links_to_the_keepalive_run(tmp_path, monkeypatch):
    ledger = _ledger(tmp_path)
    _keepalive(monkeypatch, tmp_path)
    assert propensity.record_trigger("offload", "advice:fleet7", deliverable="O/R#7", path=ledger)
    assert propensity.record_usefulness(
        "offload",
        "advice:fleet7",
        useful=True,
        evidence="review exposed the missing edge before delivery",
        provenance=propensity.PROVENANCE_DEFAULT,
        deliverable="O/R#7",
        path=ledger,
    )
    index = bridge._fleet_verdict_index(capabilities.load(ledger, create=False))
    with feedback._conn() as conn:
        first = bridge.attribute_fleet_deliverable_edges(verdict_index=index, conn=conn)
        second = bridge.attribute_fleet_deliverable_edges(verdict_index=index, conn=conn)
        edges = conn.execute(
            "SELECT target_run_id, capability_id, capability_version_id, target_event_id "
            "FROM influence_edges WHERE influence_type='capability'"
        ).fetchall()
        usage = capabilities.usage_report(capabilities.summary(ledger), conn=conn)
    assert first["attributed"] == 1 and second["attributed"] == 0
    assert len(edges) == 1
    assert edges[0][:3] == (
        "keepalive:o/r#7:codex",
        "offload",
        "capability-version:" + "a" * 32,
    )
    assert edges[0][3] is not None
    assert (
        next(row for row in usage["rows"] if row["capability_id"] == "offload")["fleet_edges"] == 1
    )
    assert "| offload |" in capabilities.format_usage_report(usage)
    # The registered resolver is load-bearing, rather than an unused declaration.
    monkeypatch.setattr(bridge, "RESOLVERS", bridge.RESOLVERS[:-1])
    with feedback._conn() as conn:
        conn.execute("DELETE FROM influence_edges")
        assert (
            bridge.attribute_fleet_deliverable_edges(verdict_index=index, conn=conn)["attributed"]
            == 0
        )


def test_prose_mention_alone_creates_no_edge(tmp_path, monkeypatch):
    ledger = _ledger(tmp_path)
    _keepalive(monkeypatch, tmp_path)
    assert propensity.record_usefulness(
        "offload",
        "advice:proseonly",
        useful=True,
        evidence="the review helped o/r#7",
        provenance=propensity.PROVENANCE_DEFAULT,
        path=ledger,
    )
    index = bridge._fleet_verdict_index(capabilities.load(ledger, create=False))
    assert index == {}
    with feedback._conn() as conn:
        assert (
            bridge.attribute_fleet_deliverable_edges(verdict_index=index, conn=conn)["attributed"]
            == 0
        )


def test_negative_and_wrong_target_do_not_attribute(tmp_path, monkeypatch):
    ledger = _ledger(tmp_path)
    _keepalive(monkeypatch, tmp_path, target="o/r#8")
    assert propensity.record_usefulness(
        "offload",
        "advice:negative",
        useful=False,
        evidence="review missed the issue",
        provenance=propensity.PROVENANCE_DEFAULT,
        deliverable="o/r#8",
        path=ledger,
    )
    assert propensity.record_usefulness(
        "offload",
        "advice:otherpr",
        useful=True,
        evidence="review found a bug on another PR",
        provenance=propensity.PROVENANCE_DEFAULT,
        deliverable="o/r#7",
        path=ledger,
    )
    index = bridge._fleet_verdict_index(capabilities.load(ledger, create=False))
    with feedback._conn() as conn:
        assert (
            bridge.attribute_fleet_deliverable_edges(verdict_index=index, conn=conn)["attributed"]
            == 0
        )


def test_cli_verdict_is_visible_across_processes_and_requires_a_target_event(tmp_path, monkeypatch):
    ledger = _ledger(tmp_path)
    _keepalive(monkeypatch, tmp_path)
    command = [
        sys.executable,
        str(Path(propensity.__file__)),
        "useful",
        "--capability",
        "offload",
        "--experiment",
        "advice:subprocess",
        "--deliverable",
        "O/R#7",
        "--evidence",
        "the review prevented a missing capability edge",
        "--provenance",
        propensity.PROVENANCE_DEFAULT,
        "--ledger",
        str(ledger),
    ]
    trigger = subprocess.run(
        [
            sys.executable,
            str(Path(propensity.__file__)),
            "trigger",
            "--capability",
            "offload",
            "--experiment",
            "advice:subprocess",
            "--deliverable",
            "O/R#7",
            "--ledger",
            str(ledger),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(trigger.stdout)["deliverable"] == "o/r#7"
    result = subprocess.run(command, capture_output=True, text=True, check=True)
    assert json.loads(result.stdout)["deliverable"] == "o/r#7"
    index = bridge._fleet_verdict_index(capabilities.load(ledger, create=False))
    with feedback._conn() as conn:
        conn.execute("DELETE FROM completion_events WHERE run_id='keepalive:o/r#7:codex'")
        report = bridge.attribute_fleet_deliverable_edges(verdict_index=index, conn=conn)
        assert report["attributed"] == 0 and report["missing_event"] == 1
        assert conn.execute("SELECT COUNT(*) FROM influence_edges").fetchone()[0] == 0
