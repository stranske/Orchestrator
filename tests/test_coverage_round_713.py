"""Hermetic boundary coverage for coverage-autopilot round 713."""

from __future__ import annotations

import json
import sys
import types

import pytest

import capability_activation_audit as audit
import feedback
import periodic_report


@pytest.fixture
def isolated_runtime(monkeypatch, tmp_path):
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ORCH_LOCAL_RUNTIME", str(tmp_path / "runtime"))
    monkeypatch.setattr(audit, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "brain.db")
    return tmp_path


def test_label_cache_round_trip_and_invalid_input(isolated_runtime):
    path = isolated_runtime / "state" / "labels.json"
    audit._write_label_cache(
        path,
        {"generated_at": 42, "repos": {"o/r": ["Bug"]}, "read_at": {}, "unread": {}},
    )
    assert audit._load_label_cache(path) == {
        "repos": {"o/r": ["Bug"]},
        "read_at": {"o/r": 42.0},
        "unread": {},
    }
    path.write_text("not json")
    assert audit._load_label_cache(path) == {"repos": {}, "read_at": {}, "unread": {}}


def test_vocabulary_gaps_excludes_deliberate_labels(monkeypatch, isolated_runtime):
    module = types.SimpleNamespace(HIGH_STAKES_LABELS={"risk:high"})
    monkeypatch.setitem(sys.modules, "adversarial", module)
    monkeypatch.setattr(
        audit, "GATE_VOCABULARIES", (("adversarial-review", "adversarial", "HIGH_STAKES_LABELS"),)
    )
    assert audit.vocabulary_gaps({"repos": {"o/r": ["risk:major", "risk:low"]}}) == {
        "adversarial-review": ["risk:major"]
    }


@pytest.mark.parametrize("raw", ["0", "-1", "not-a-number"])
def test_positive_int_rejects_non_positive_and_invalid_values(raw, isolated_runtime):
    with pytest.raises(Exception):
        periodic_report._positive_int(raw)


def test_snapshot_report_is_isolated_and_removes_its_temp_database(monkeypatch, isolated_runtime):
    snapshot = isolated_runtime / "snapshot.json"
    snapshot.write_text(json.dumps(feedback.snapshot_json()))
    original = feedback.DB_PATH
    captured = {}

    def fake_build_report(**kwargs):
        captured["db"] = feedback.DB_PATH
        captured["kwargs"] = kwargs
        return {"ok": True}

    monkeypatch.setattr(periodic_report, "build_report", fake_build_report)
    report = periodic_report.build_report_from_snapshot(
        snapshot, window_days=7, min_gap_recurrence=2
    )

    assert report["source"] == "snapshot"
    assert report["snapshot_path"] == str(snapshot)
    assert captured["db"] != original and not captured["db"].exists()
    assert feedback.DB_PATH == original
    # Snapshot replay must preserve the offline default rather than probe live artifacts.
    assert captured["kwargs"]["probe_langsmith_artifacts"] is False
