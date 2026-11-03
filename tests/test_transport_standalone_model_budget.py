"""Standalone measured-time regression: real inert processes, controlled clock."""

import json
import sys
import time
import types
from pathlib import Path

import pytest

import transport_qualification_launcher as q

FIXTURE = Path(__file__).parent / "fixtures/transport_qualification_launcher/inert_child.py"


class Clock:
    def __init__(self):
        self.now = 0.0
        self.in_capture = False
        self.capture_started = 0.0

    def monotonic(self):
        return self.now + (time.monotonic() - self.capture_started if self.in_capture else 0.0)


class Lease:
    def check(self, pool, run):
        assert pool == q.POOL and run


def measured_wave(tmp_path, monkeypatch, durations):
    binary = tmp_path / "success"
    binary.write_bytes(f"#!{sys.executable}\n".encode() + FIXTURE.read_bytes())
    binary.chmod(0o700)
    prepared = q.synthetic_wave(binary)
    clock, captures, deadlines = Clock(), [], []
    real_capture = q._capture

    def forbidden(*args, **kwargs):
        raise AssertionError("native runtime forbidden")

    def supervised(*args, **kwargs):
        deadlines.append((clock.now, kwargs["deadline"]))
        clock.capture_started = time.monotonic()
        clock.in_capture = True
        try:
            # Keep real Popen, pipes, group cleanup and started/finished callbacks.
            result = real_capture(*args, **kwargs)
            captures.append(result["exit_code"])
            return result
        finally:
            clock.in_capture = False
            clock.now += durations[len(deadlines) - 1]

    monkeypatch.setattr(q.NativeRuntime, "command", forbidden)
    monkeypatch.setattr(
        q,
        "time",
        types.SimpleNamespace(monotonic=clock.monotonic, time=time.time, sleep=time.sleep),
    )
    monkeypatch.setattr(q, "_capture", supervised)
    report = q.run_wave(
        prepared,
        run_id="standalone-budget",
        receipt_parent=tmp_path,
        held_lease=Lease(),
        execute=True,
        oauth_token="synthetic-token",
        runtime=q.FixtureRuntime(binary, q.file_sha(binary)),
        _preflight_clock=lambda: 0.0,
    )
    records = [
        json.loads((Path(report["receipt_directory"]) / n).read_text()) for n in report["receipts"]
    ]
    return report, records, captures, deadlines


@pytest.mark.parametrize("overrun_at", [1, 8])
def test_real_inert_capture_overrun_stops_first_or_final_cell(tmp_path, monkeypatch, overrun_at):
    durations = [45.0] * 8
    durations[overrun_at - 1] = 46.0
    report, records, captures, deadlines = measured_wave(tmp_path, monkeypatch, durations)
    assert report["status"] == "stopped"
    assert report["cli_launch_count"] == len(captures) == overrun_at
    assert captures == [0] * overrun_at  # successful real children cannot mask supervision overrun
    assert report["model_supervision_seconds"] == sum(durations[:overrun_at])
    assert report["model_supervision_seconds_max"] == 360
    assert records[-1]["cell_model_supervision_seconds"] == 46
    assert records[-1]["wave_model_supervision_seconds"] == sum(durations[:overrun_at])
    assert records[-1]["model_budget_reason"] == "per_submission_model_time_exceeded"
    assert records[-1]["failure_stage"] == "model_supervision_budget"
    assert report["outer_retries"] == report["fallbacks"] == 0
    assert all(deadline <= start + min(45, 360 - start) for start, deadline in deadlines)
    if overrun_at == 1:
        assert not (Path(report["receipt_directory"]) / "attempt-2-starting.json").exists()


def test_exact_45_times_eight_is_inclusive_and_capture_deadlines_are_bounded(tmp_path, monkeypatch):
    report, records, captures, deadlines = measured_wave(tmp_path, monkeypatch, [45.0] * 8)
    assert report["status"] == "completed_unverified_calibration"
    assert report["cli_launch_count"] == len(captures) == 8 and captures == [0] * 8
    assert report["model_supervision_seconds"] == 360
    assert all(row["cell_model_supervision_seconds"] == 45 for row in records)
    for index, (row, (start, deadline)) in enumerate(zip(records, deadlines, strict=True)):
        remaining = 360 - index * 45
        budgets = row["actual_config"]["budgets"]
        assert budgets["wave_model_seconds_max"] == 360
        assert budgets["wave_model_seconds_remaining"] == remaining
        assert deadline == start + min(45, remaining) - 0.5
        assert budgets["capture_deadline_monotonic"] == deadline
        assert row["actual_config_sha256"] == q.digest(row["actual_config"])
        assert row["actual_execution_binding_sha256"] == q.digest(row["actual_execution_binding"])


def test_admission_and_final_success_budget_boundaries_are_distinct():
    assert q._model_budget_reason(45, 360) is None
    assert q._model_budget_reason(None, 360, before_admission=True) == "wave_model_time_exhausted"
    assert q._model_budget_reason(46, 46) == "per_submission_model_time_exceeded"
    assert q._model_budget_reason(None, 361) == "wave_model_time_exceeded"
