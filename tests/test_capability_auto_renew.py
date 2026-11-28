"""Auto-renew on outcomes (owner decision 2026-11-28): a delivering row holds itself, the rest expire.

Every ledger row carries an expiry by design, and until this change every producing capability needed a
hand renewal every GATED_TTL_DAYS: route-weights-export (value-chain `works`, outcome 93, success 81)
was 4.4 days from retirement with nothing to renew it. These pin that a row whose value-chain monitor
row shows success > 0 renews itself through `renew`, with the row quoted as evidence; a row with no
outcomes is left to expire; a row already retired by its expiry comes back; and the line prints the
renewed / refused / no-outcome counts together. Every ledger is a temporary file.
"""

from __future__ import annotations

import json
from pathlib import Path

import capabilities

DAY = 86400
GATE = "role-redirect"
NOW = 2_000_000_000


def _gate_row(**fields) -> dict:
    row = capabilities._blank_capability(GATE)
    row.update(json.loads(json.dumps(capabilities.KNOWN_GATES[GATE])))
    row.update({"next_transition": "retired", **fields})
    return row


def _plain_row(cap_id: str, **fields) -> dict:
    row = capabilities._blank_capability(cap_id)
    row.update({"status": "wired", **fields})
    return row


def _ledger(tmp_path: Path, rows: dict) -> Path:
    path = tmp_path / "capabilities.json"
    capabilities.save(rows, path)
    return path


def _vc(cap_id: str, success, acted=5) -> dict:
    return {
        "capability": cap_id,
        "situation_count": 124,
        "invocation_count": 38,
        "usable": 56,
        "acted_on": acted,
        "outcome": 93,
        "outcome_success": success,
        "first_break": "works" if success else "no_success",
    }


def test_a_delivering_row_inside_the_window_renews_with_the_monitor_row_as_evidence(tmp_path):
    ledger = _ledger(tmp_path, {"export": _plain_row("export", expiry=NOW + 4 * DAY)})
    out = capabilities.auto_renew_on_outcomes(
        [_vc("export", 81)], now=NOW, horizon_days=14, path=ledger
    )
    assert [r["capability_id"] for r in out["renewed"]] == ["export"]
    row = json.loads(ledger.read_text())["capabilities"]["export"]
    assert row["expiry"] == NOW + capabilities.GATED_TTL_DAYS * DAY
    event = [e for e in row["event_history"] if e["type"] == capabilities.RENEWAL_EVENT][-1]
    assert event["reason"].startswith(capabilities.AUTO_RENEW_REASON)
    assert event["evidence_refs"] == [
        "value-chain-monitor:export demand 124; invocation 38; usable 56; acted_on 5; "
        "outcome 93; success 81; first_break works"
    ]
    assert "renewed 1; refused 0; no outcome 0" in out["line"]
    assert out["delivering_inside_window"] == 0


def test_a_row_without_outcomes_still_expires_and_is_counted(tmp_path):
    ledger = _ledger(
        tmp_path,
        {
            "idle": _plain_row("idle", expiry=NOW + 3 * DAY),
            "absent": _plain_row("absent", expiry=NOW + 3 * DAY),  # no monitor row at all
        },
    )
    out = capabilities.auto_renew_on_outcomes(
        [_vc("idle", 0)], now=NOW, horizon_days=14, path=ledger
    )
    assert out["renewed"] == [] and len(out["no_outcome"]) == 2
    assert json.loads(ledger.read_text())["capabilities"]["idle"]["expiry"] == NOW + 3 * DAY
    assert "renewed 0; refused 0; no outcome 2" in out["line"]


def test_a_row_its_expiry_retired_comes_back_on_its_outcomes(tmp_path):
    """The drain must run while the gate is closed: retirement must not hide the evidence."""
    ledger = _ledger(tmp_path, {GATE: _gate_row(expiry=NOW - DAY, activation_deadline=NOW - DAY)})
    capabilities.sweep(ledger, now=NOW)  # the timeout, as it runs live
    assert capabilities.load_declared(ledger)[GATE]["status"] == "retired"
    out = capabilities.auto_renew_on_outcomes([_vc(GATE, 3)], now=NOW, horizon_days=14, path=ledger)
    assert out["renewed"][0]["revived_from"] == "retired"
    assert (
        capabilities.load_declared(ledger)[GATE]["status"]
        == capabilities.KNOWN_GATES[GATE]["status"]
    )


def test_rows_outside_the_notice_window_are_left_alone_and_a_second_sweep_does_nothing(tmp_path):
    ledger = _ledger(
        tmp_path,
        {
            "far": _plain_row("far", expiry=NOW + 40 * DAY),
            "near": _plain_row("near", expiry=NOW + 2 * DAY),
        },
    )
    rows = [_vc("far", 9), _vc("near", 9)]
    out = capabilities.auto_renew_on_outcomes(rows, now=NOW, horizon_days=14, path=ledger)
    assert [r["capability_id"] for r in out["renewed"]] == ["near"]
    again = capabilities.auto_renew_on_outcomes(rows, now=NOW + DAY, horizon_days=14, path=ledger)
    assert again["renewed"] == [] and again["refused"] == []  # renewals never stack weekly


def test_the_kill_switch_makes_it_report_only(tmp_path):
    ledger = _ledger(tmp_path, {"export": _plain_row("export", expiry=NOW + 4 * DAY)})
    out = capabilities.auto_renew_on_outcomes(
        [_vc("export", 81)],
        now=NOW,
        horizon_days=14,
        path=ledger,
        env={capabilities.AUTO_RENEW_SWITCH: "0"},
    )
    assert out["renewed"] == [] and out["delivering_inside_window"] == 1
    assert json.loads(ledger.read_text())["capabilities"]["export"]["expiry"] == NOW + 4 * DAY
    assert "ORCH_CAPABILITY_AUTO_RENEW=0" in out["line"]


def test_fully_drained_prints_zeros_not_nothing(tmp_path):
    ledger = _ledger(tmp_path, {"far": _plain_row("far", expiry=NOW + 60 * DAY)})
    out = capabilities.auto_renew_on_outcomes([], now=NOW, horizon_days=14, path=ledger)
    assert out["line"] == (
        "auto-renew on outcomes: renewed 0; refused 0; no outcome 0 (expire on schedule); "
        "delivering inside the 14d notice window and not renewed 0"
    )


def test_the_monitor_reads_an_expiry_retired_row_only_when_asked(tmp_path):
    import value_chain_monitor

    ledger = _ledger(tmp_path, {"plain": _plain_row("plain", expiry=NOW - DAY)})
    capabilities.sweep(ledger, now=NOW)
    plain = value_chain_monitor.report(now=NOW, path=ledger, env={}, inputs={})
    assert [r["capability"] for r in plain["rows"]] == []
    asked = value_chain_monitor.report(
        now=NOW, path=ledger, env={}, inputs={}, include_expired=True
    )
    assert [(r["capability"], r.get("retired_by_expiry")) for r in asked["rows"]] == [
        ("plain", True)
    ]
