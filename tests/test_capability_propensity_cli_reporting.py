"""Hermetic CLI coverage for capability-propensity's read-only reporting commands.

These commands are deliberately report-only: their command dispatch must preserve that
boundary while rendering the result supplied by the underlying reader.  The test replaces
every reader and heartbeat, so no live ledger, process, or network call can occur.
"""

from __future__ import annotations

import json
import socket
import subprocess

import pytest

import capability_propensity as propensity


@pytest.fixture(autouse=True)
def isolated_cli(monkeypatch, tmp_path):
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("ORCH_LOCAL_RUNTIME", str(tmp_path))
    attempted = []

    def forbid_external(*args, **kwargs):
        attempted.append(args)
        raise AssertionError("CLI tests must not start processes or network calls")

    monkeypatch.setattr(subprocess, "Popen", forbid_external)
    monkeypatch.setattr(socket, "create_connection", forbid_external)
    yield
    assert attempted == []


def test_read_only_cli_commands_render_their_injected_reports(monkeypatch, capsys):
    """Each read-only command reaches its reader without opening a live capability store."""
    calls: list[tuple[str, object]] = []
    props = [
        {
            "capability_id": "offload",
            "worth_having": True,
            "basis": ["defect_found"],
            "defect_evidence_since_repair": 1,
            "defect_evidence_total": 1,
            "repairs_recorded": 0,
            "evidence": ["PR #1"],
            "implied_fixes": ["repair the binding"],
        }
    ]
    quality = {
        "surface": "opener-lane",
        "window_days": 30,
        "bound": ["offload"],
        "offers": 1,
        "triggers": 1,
        "declines": 0,
        "finds": 0,
        "finds_by_finder_kind": {},
        "find_subjects": [],
    }
    detection = {
        "surfaces": {},
        "promotions": [],
        "demotions": [],
        "not_selected_by_design": {"count": 0, "withheld": 0, "capabilities": {}},
        "applied": [],
    }
    report = {"experiment_count": 0, "ranked": []}

    monkeypatch.setattr(
        propensity,
        "propose_repair",
        lambda *, window_days: calls.append(("repair", window_days)) or props,
    )
    monkeypatch.setattr(
        propensity,
        "binding_quality",
        lambda surface, *, window_days: calls.append(("binding-quality", (surface, window_days)))
        or quality,
    )
    monkeypatch.setattr(
        propensity,
        "detect",
        lambda *, apply_promotions: calls.append(("detect", apply_promotions)) or detection,
    )
    monkeypatch.setattr(
        propensity,
        "propose_offer_improvements",
        lambda *, window_days, as_the_tick_sees_it: calls.append(
            ("offer-improvements", (window_days, as_the_tick_sees_it))
        )
        or [{"capability_id": "offload"}],
    )
    monkeypatch.setattr(
        propensity,
        "experiments",
        lambda *, window_days: calls.append(("experiments", window_days)) or [],
    )
    monkeypatch.setattr(
        propensity,
        "report",
        lambda *, window_days: calls.append(("report", window_days)) or report,
    )
    monkeypatch.setattr(
        propensity,
        "_capability_heartbeat",
        lambda event, ref: calls.append(("heartbeat", (event, ref))),
    )

    assert propensity.main(["repair", "--json", "--window-days", "7"]) == 0
    assert json.loads(capsys.readouterr().out) == props

    assert propensity.main(["binding-quality", "--surface", "opener-lane", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == quality

    assert propensity.main(["detect", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == detection

    assert propensity.main(["offer-improvements", "--window-days", "7"]) == 0
    assert json.loads(capsys.readouterr().out) == [{"capability_id": "offload"}]

    assert propensity.main(["experiments", "--json", "--window-days", "7"]) == 0
    assert json.loads(capsys.readouterr().out) == []

    assert propensity.main(["report", "--json", "--window-days", "7"]) == 0
    assert json.loads(capsys.readouterr().out) == report

    assert calls == [
        ("repair", 7),
        ("heartbeat", ("invocation", "repair-proposals:1")),
        ("binding-quality", ("opener-lane", 90)),
        ("detect", False),
        ("heartbeat", ("invocation", "detect:0")),
        ("offer-improvements", (7, True)),
        ("experiments", 7),
        ("report", 7),
        ("heartbeat", ("invocation", "cli:report:0")),
    ]


def test_mutating_cli_commands_forward_their_explicit_mutation_inputs(
    monkeypatch, capsys, tmp_path
):
    """Migration and `detect --apply` take mutation-only paths, never the reporting default."""
    calls: list[tuple[str, object]] = []
    detection = {
        "surfaces": {},
        "promotions": [],
        "demotions": [],
        "not_selected_by_design": {"count": 0, "withheld": 0, "capabilities": {}},
        "applied": [],
    }
    ledger = tmp_path / "fixture.json"
    monkeypatch.setattr(
        propensity,
        "migrate_fixture_provenance",
        lambda *, path: calls.append(("migrate", path)) or {"changed": 0, "left": 2},
    )
    monkeypatch.setattr(
        propensity,
        "detect",
        lambda *, apply_promotions: calls.append(("detect", apply_promotions)) or detection,
    )
    monkeypatch.setattr(
        propensity,
        "_capability_heartbeat",
        lambda event, ref: calls.append(("heartbeat", (event, ref))),
    )

    assert propensity.main(["migrate-fixture-provenance", "--ledger", str(ledger), "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"changed": 0, "left": 2}

    assert propensity.main(["detect", "--json", "--apply"]) == 0
    assert json.loads(capsys.readouterr().out) == detection

    assert calls == [
        ("migrate", ledger),
        ("detect", True),
        ("heartbeat", ("invocation", "detect:0")),
    ]
