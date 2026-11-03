"""Actual scheduled consumer joins only retained model-run telemetry."""

from test_closer_efficiency_metrics import HEAD, TARGET, receipt, stage_event
from test_closer_native_capacity import capacity_host as capacity_host
from test_closer_native_observer import host as host
from test_closer_role_controller import tick_without_model

import closer_hold_followup as holds
import closer_stage_feedback as feedback


def test_actual_tick_reads_exact_retained_model_events_before_reporting(capacity_host, monkeypatch):
    retained = receipt(model="model-1", classification="ambiguous")
    retained["finished_at"] = capacity_host[5][0] - 5
    monkeypatch.setattr(holds, "validated_receipts", lambda *_: [retained])
    seen = []

    def read(bindings):
        seen.append(bindings)
        return {
            "events": [stage_event()],
            "status": "available",
            "requested_runs": 1,
            "returned_events": 1,
            "rejected_events": 0,
            "truncated": False,
            "authority": False,
        }

    monkeypatch.setattr(feedback, "read_stage_events", read)
    result = tick_without_model(capacity_host, [])
    summary = next(row["efficiency"] for row in result if row["kind"] == "hold_followup_summary")
    assert seen == [{"model-1": {"target": TARGET, "head_sha": HEAD}}]
    assert summary["observed_cost_usd"]["total_or_null"] == 0.25
    assert summary["observed_latency_seconds"]["total_or_null"] == 3.0
    assert summary["stage_event_collection"]["authority"] is False
    assert summary["independent_outcomes"]["measured_runs"] == 0


def test_telemetry_failure_cannot_block_normally_admitted_tick(capacity_host, monkeypatch):
    retained = receipt(model="model-1", classification="ambiguous")
    retained["finished_at"] = capacity_host[5][0] - 5
    monkeypatch.setattr(holds, "validated_receipts", lambda *_: [retained])

    def unavailable(_bindings):
        raise RuntimeError("telemetry unavailable")

    monkeypatch.setattr(feedback, "read_stage_events", unavailable)
    launched = []
    result = tick_without_model(capacity_host, launched)
    assert any(
        row["kind"] == "efficiency_unavailable" and row["authority"] is False for row in result
    )
    assert any(row["kind"] == "launched" for row in result)
    assert len(launched) == 1
