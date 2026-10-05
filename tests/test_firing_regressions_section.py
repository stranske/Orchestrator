"""The weekly alarm reaches a reader with independent step and ledger evidence."""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

import capabilities
import capability_firing_monitor as monitor
import rail_exercise
import route_weights_export as export
import switch_review as switches

NOW = 1_800_000_000
DAY = 86400


def _date(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


def _render_section(section):
    """Exercise the report a reader sees, including the section's formatter wiring."""
    return switches.format_report(
        {
            "review_days": switches.REVIEW_DAYS,
            "raise_count": 0,
            "held_off": [],
            "on_but_idle": [],
            "unconditioned": [],
            "mirror_drift": {"status": "ok"},
            "firing_regressions": section,
        }
    )


def _private_ledger(tmp_path, monkeypatch, cap_id):
    """Run the real daily writer against a private ledger with a fixed clock."""
    ledger = tmp_path / "capabilities.json"
    capabilities.save({cap_id: capabilities._blank_capability(cap_id)}, ledger)
    heartbeat = capabilities.heartbeat
    beats = []

    def record_heartbeat(*args, **kwargs):
        beats.append((args, kwargs))
        return heartbeat(*args, **{**kwargs, "path": ledger})

    monkeypatch.setattr(capabilities, "heartbeat", record_heartbeat)
    monkeypatch.setattr(capabilities, "_now", lambda: NOW)
    monkeypatch.setenv("ORCH_CAPABILITY_HEARTBEATS", "1")
    return ledger, beats


def _section(tmp_path, monkeypatch, *, stamp_age=3600, last_age=30 * DAY, artifact_age=10 * DAY):
    cap = "route-weights-export"
    monkeypatch.setattr(
        capabilities, "load_declared", lambda *_a: {cap: {"last_invocation": NOW - last_age}}
    )
    (tmp_path / "capability-firing-monitor.json").write_text(
        json.dumps(
            {
                "generated_at": NOW - DAY,
                "regressed": [{"capability_id": cap}],
                "overdue": [{"capability_id": cap}],
            }
        )
    )
    for name, age in [
        (".last-route-weights-export", stamp_age),
        ("route-weights-export.json", artifact_age),
    ]:
        p = tmp_path / name
        p.write_text("{}")
        os.utime(p, (NOW - age, NOW - age))
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path))
    return switches.firing_regressions(now=NOW)


@pytest.mark.parametrize("stamp_age", [0, 3600, DAY + 12 * 3600])
@pytest.mark.parametrize("artifact_age", [60, 10 * DAY])
def test_a_silent_heartbeat_with_a_fresh_stamp_prints_heartbeat_silent_step_ran(
    tmp_path, monkeypatch, stamp_age, artifact_age
):
    section = _section(tmp_path, monkeypatch, stamp_age=stamp_age, artifact_age=artifact_age)
    text = _render_section(section)
    assert f"heartbeat silent, step ran {_date(NOW - stamp_age)}" in text
    assert f"ledger last heartbeat: {_date(NOW - 30 * DAY)}" in text
    assert f"stamp age seconds={stamp_age}" in text
    assert f"artifact mtime: {_date(NOW - artifact_age)}" in text
    assert len(section["rows"]) == 1  # Same row in two findings is rendered once.
    step = section["rows"][0]["step_evidence"][0]
    assert step["artifact_mtime"] == NOW - artifact_age
    assert step["stamp_mtime"] == NOW - stamp_age
    assert step["stamp_age_seconds"] == stamp_age


@pytest.mark.parametrize("stamp_age", [DAY + 12 * 3600 + 1, 25 * DAY])
@pytest.mark.parametrize("artifact_age", [60, 10 * DAY])
def test_a_real_stop_prints_step_last_ran(tmp_path, monkeypatch, stamp_age, artifact_age):
    section = _section(tmp_path, monkeypatch, stamp_age=stamp_age, artifact_age=artifact_age)
    text = _render_section(section)
    assert f"step last ran {_date(NOW - stamp_age)}" in text
    assert f"ledger last heartbeat: {_date(NOW - 30 * DAY)}" in text
    assert f"stamp age seconds={stamp_age}" in text
    assert f"artifact mtime: {_date(NOW - artifact_age)}" in text
    assert "heartbeat silent, step ran" not in text


def test_artifact_without_stamp_does_not_claim_the_step_ran(tmp_path, monkeypatch):
    _section(tmp_path, monkeypatch)
    (tmp_path / ".last-route-weights-export").unlink()
    text = "\n".join(
        switches._firing_lines(switches.firing_regressions(now=NOW, state_dir=tmp_path))
    )
    assert "step last ran UNKNOWN (missing)" in text
    assert "heartbeat silent, step ran" not in text


def test_new_heartbeat_does_not_repeat_a_historical_silence_claim(tmp_path, monkeypatch):
    section = _section(tmp_path, monkeypatch, last_age=60)
    text = "\n".join(switches._firing_lines(section))
    assert "heartbeat recorded since monitor snapshot" in text
    assert "heartbeat silent, step ran" not in text
    assert section["rows"][0]["findings"] == ["regressed", "overdue"]


@pytest.mark.parametrize(
    "content", [None, "broken json", '{"generated_at":1,"regressed":null,"overdue":[]}']
)
def test_missing_or_invalid_report_stays_unknown(tmp_path, content):
    if content is not None:
        (tmp_path / "capability-firing-monitor.json").write_text(content)
    section = switches.firing_regressions(now=NOW, state_dir=tmp_path)
    assert section["status"] == "unknown"
    assert "UNKNOWN" in "\n".join(switches._firing_lines(section))


def test_monitor_carries_step_evidence_without_changing_findings(tmp_path, monkeypatch):
    cap = "route-weights-export"
    monkeypatch.setattr(monitor, "STATE_DIR", tmp_path)
    monkeypatch.setattr(monitor, "_capability_heartbeat", lambda *_a: None)
    monkeypatch.setattr(monitor, "_load_history", lambda: [])
    monkeypatch.setattr(
        capabilities,
        "load_declared",
        lambda *_a: {
            cap: {
                "status": "wired",
                "last_invocation": NOW - 30 * DAY,
                "trigger_cadence": "daily",
                "matcher": {"kind": "transport"},
            }
        },
    )
    p = tmp_path / ".last-route-weights-export"
    p.touch()
    os.utime(p, (NOW - 60, NOW - 60))
    rep = monitor.review(now=NOW, environ={})
    assert rep["overdue_count"] == 1
    row = rep["overdue"][0]
    assert row["capability_id"] == cap
    assert row["step_evidence"][0]["stamp_age_seconds"] == 60
    assert rep["silence_evidence"][cap]["step_evidence"] == row["step_evidence"]


def test_review_and_formatter_consume_the_firing_section(monkeypatch):
    monkeypatch.setattr(switches, "_capability_heartbeat", lambda *_a: None)
    monkeypatch.setattr(switches, "switch_states", lambda **_k: {"held_off": [], "on_but_idle": []})
    for name in [
        "stale_runners",
        "mirror_drift",
        "fleet_gates",
        "_exploration_gate",
        "gate_expiry",
    ]:
        monkeypatch.setattr(switches, name, lambda **_k: {})
    section = {"status": "ok", "generated_at": NOW, "rows": []}
    calls = []
    monkeypatch.setattr(switches, "firing_regressions", lambda **kw: calls.append(kw) or section)
    rep = switches.review(now=NOW, path=Path("private-ledger"))
    assert calls == [{"now": NOW, "path": Path("private-ledger")}]
    assert rep["firing_regressions"] is section
    assert "Firing regressions and overdue steps" in switches.format_report(rep)
    assert rep["raise_count"] == 0


def _export(tmp_path, monkeypatch, *, publish=False, published=False):
    _, beats = _private_ledger(tmp_path, monkeypatch, "route-weights-export")
    monkeypatch.setattr(export, "build_document", lambda *_a: {"source_version": 2})
    monkeypatch.setattr(export, "publish_document", lambda *_a: published)
    monkeypatch.setenv("ORCH_ROUTE_WEIGHTS_PUBLISH", "1")
    monkeypatch.setattr(
        sys,
        "argv",
        ["route_weights_export.py", "--state-dir", str(tmp_path)]
        + (["--publish"] if publish else []),
    )
    assert export.main() == 0
    return beats


def test_route_weights_export_heartbeats_invocation_without_a_publish(
    tmp_path, monkeypatch, capsys
):
    # An unchanged artifact is the original failure case: use the real writer and
    # preserve its old timestamp while the ledger records today's invocation.
    artifact = tmp_path / "route-weights-export.json"
    document = {"source_version": 2}
    assert export.write_document(artifact, document)
    os.utime(artifact, (NOW - 10 * DAY, NOW - 10 * DAY))
    beats = _export(tmp_path, monkeypatch)
    assert capsys.readouterr().out == f"unchanged {artifact}\n"
    monkeypatch.setattr(capabilities, "_now", lambda: NOW + DAY)
    assert export.main() == 0
    assert capsys.readouterr().out == f"unchanged {artifact}\n"
    assert [a[1] for a, _ in beats] == ["invocation", "invocation"]
    row = capabilities.load(tmp_path / "capabilities.json", create=False)["route-weights-export"]
    assert row["last_invocation"] == NOW + DAY
    assert not row["last_success"]
    assert [event["type"] for event in row["event_history"]] == ["invocation", "invocation"]
    assert row["event_history"][0]["ref"] == "route_weights_export.main"
    assert row["event_history"][1]["timestamp"] == NOW + DAY
    assert artifact.stat().st_mtime == NOW - 10 * DAY


@pytest.mark.parametrize("published", [False, True])
def test_export_success_means_a_completed_publication(tmp_path, monkeypatch, published):
    beats = _export(tmp_path, monkeypatch, publish=True, published=published)
    assert [a[1] for a, _ in beats] == (["invocation", "success"] if published else ["invocation"])
    if published:
        assert beats[-1][1]["metadata"]["published"] is True
    row = capabilities.load(tmp_path / "capabilities.json", create=False)["route-weights-export"]
    assert row["last_invocation"] == NOW
    assert row["last_success"] == (NOW if published else None)


@pytest.mark.parametrize("passed", [False, True])
def test_rail_exercise_heartbeats_without_record(tmp_path, monkeypatch, capsys, passed):
    ledger, beats = _private_ledger(tmp_path, monkeypatch, "rail-exercise-cadence")
    root = tmp_path / "rail_exercises"
    folder = root / "fixture"
    (folder / "fixtures").mkdir(parents=True)
    (folder / "contract.json").write_text(
        json.dumps(
            {
                "capability_id": "fixture",
                "run": "true",
                "pass_check": "true",
                "break_case": {"run": "false" if passed else "true"},
            }
        )
    )
    monkeypatch.setattr(rail_exercise, "CONTRACT_ROOT", root)
    recorded = []
    monkeypatch.setattr(rail_exercise, "_record", lambda row: recorded.append(row) or "recorded")
    monkeypatch.setattr(sys, "argv", ["rail_exercise.py", "--json"])
    assert rail_exercise.main() == 0
    report = json.loads(capsys.readouterr().out)
    assert report["recording"] is False
    assert report["contracts"][0]["status"] == ("pass" if passed else "fail")
    assert "record" not in report["contracts"][0]
    assert recorded == []
    verdict = "success" if passed else "failure"
    assert [a[1] for a, _ in beats] == ["invocation", verdict]
    row = capabilities.load(ledger, create=False)["rail-exercise-cadence"]
    assert row["last_invocation"] == NOW
    assert row["last_success"] == (NOW if passed else None)
    assert [event["type"] for event in row["event_history"]] == ["invocation", verdict]
    assert row["event_history"][1]["timestamp"] == NOW
    assert row["event_history"][1]["metadata"]["failed"] == int(not passed)

    # Positive control: the same report records the contract verdict only when armed.
    monkeypatch.setattr(capabilities, "_now", lambda: NOW + DAY)
    monkeypatch.setattr(sys, "argv", ["rail_exercise.py", "--json", "--record"])
    assert rail_exercise.main() == 0
    report = json.loads(capsys.readouterr().out)
    assert report["recording"] is True
    assert recorded == report["contracts"]
    assert report["contracts"][0]["record"] == "recorded"
    assert [a[1] for a, _ in beats] == ["invocation", verdict, "invocation", verdict]
    row = capabilities.load(ledger, create=False)["rail-exercise-cadence"]
    assert row["last_invocation"] == NOW + DAY
    assert row["last_success"] == (NOW + DAY if passed else None)
    assert [event["type"] for event in row["event_history"]] == [
        "invocation",
        verdict,
        "invocation",
        verdict,
    ]
    assert row["event_history"][-1]["timestamp"] == NOW + DAY
