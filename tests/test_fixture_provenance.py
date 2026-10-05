"""Fixture proof remains visible without becoming production usefulness."""

from __future__ import annotations

import copy
import subprocess
import sys
from pathlib import Path

import capabilities
import capability_propensity as cp
import rail_exercise


def _ledger(tmp_path):
    path = tmp_path / "capabilities.json"
    capabilities.save(
        {cid: capabilities._blank_capability(cid) for cid in ("a-fixture", "z-production")}, path
    )
    return path


def _record(
    path,
    cid,
    ref,
    provenance,
    *,
    useful=True,
    evidence="a sandboxed contract passed",
    metadata=None,
):
    cp.record_trigger(cid, ref, path=path, metadata={"surface": "opener-lane"})
    cp.record_usefulness(
        cid,
        ref,
        path=path,
        useful=useful,
        provenance=provenance,
        judge=ref,
        evidence=evidence,
        metadata=metadata,
    )


def test_fixture_verdicts_never_rank(tmp_path):
    path = _ledger(tmp_path)
    before = cp.rank([{"capability_id": "a-fixture"}, {"capability_id": "z-production"}], path=path)
    for i in range(3):
        _record(path, "a-fixture", f"advice:fixture-{i}", "fixture_observed")
    after = cp.rank([{"capability_id": "a-fixture"}, {"capability_id": "z-production"}], path=path)
    assert [r["propensity"] for r in after] == [r["propensity"] for r in before]
    assert after[0]["fixture_passes"] == 3
    assert cp.provenance_weight("fixture_observed") == 0
    stats = cp.usefulness(path=path)["rows"]["a-fixture"]
    assert stats["useful"] == stats["resolved"] == stats["triggered"] == 0
    assert stats["fixture_passes"] == 3 and stats["outcome_derived"] == 0
    _record(path, "z-production", "advice:production", "machine_observed")
    ranked = cp.rank([{"capability_id": "a-fixture"}, {"capability_id": "z-production"}], path=path)
    assert ranked[0]["capability_id"] == "z-production"


def test_usage_report_splits_fixture_from_production(tmp_path, monkeypatch):
    path = _ledger(tmp_path)
    _record(path, "a-fixture", "advice:fixture", "fixture_observed")
    _record(path, "a-fixture", "advice:prod", "machine_observed")
    _record(path, "a-fixture", "advice:fixture-fail", "fixture_observed", useful=False)
    monkeypatch.setattr(capabilities, "_fleet_edge_counts", lambda **kw: {})
    report = capabilities.usage_report(
        {"path": str(path), "capabilities": capabilities.load_declared(path)}, path=path
    )
    row = next(r for r in report["rows"] if r["capability_id"] == "a-fixture")
    assert row["production_useful"] == 1 and row["fixture_passes"] == 1
    assert "production useful 1 / fixture passes 1" in capabilities.format_usage_report(report)
    assert report["capabilities_with_verdict"] == 1


def test_migration_is_idempotent_and_reports_counts(tmp_path):
    path = _ledger(tmp_path)
    _record(
        path,
        "a-fixture",
        "advice:legacy-hash",
        "machine_observed",
        evidence="contract proposers/P0/orch/exercises/a-fixture.json: stated_task=proof",
    )
    _record(path, "a-fixture", "advice:rail-exercise:contract", "self_reported", useful=False)
    _record(path, "a-fixture", "advice:already-fixture", "fixture_observed")
    _record(
        path,
        "z-production",
        "advice:production",
        "machine_observed",
        evidence="Fixed a production regression discovered using a fixture",
    )
    original = copy.deepcopy(capabilities.load_declared(path)["a-fixture"]["event_history"])
    assert cp.migrate_fixture_provenance(path=path) == {"changed": 2, "left": 2, "scanned": 4}
    corrected = capabilities.load_declared(path)["a-fixture"]["event_history"]
    assert corrected[: len(original)] == original
    assert len(corrected) == len(original) + 2
    assert cp.migrate_fixture_provenance(path=path) == {"changed": 0, "left": 4, "scanned": 4}
    assert capabilities.load_declared(path)["a-fixture"]["event_history"] == corrected
    stats = cp.usefulness(path=path)["rows"]
    assert stats["a-fixture"]["fixture_passes"] == 2
    assert stats["a-fixture"]["fixture_failures"] == 1
    assert stats["a-fixture"]["resolved"] == 0
    assert stats["z-production"]["useful"] == 1
    command = [
        sys.executable,
        str(Path(cp.__file__)),
        "migrate-fixture-provenance",
        "--ledger",
        str(path),
    ]
    run = subprocess.run(command, capture_output=True, text=True)
    assert run.returncode == 0 and "changed 0 / left 4" in run.stdout


def test_detector_ignores_fixture_selection_and_usefulness(tmp_path, monkeypatch):
    import capability_advisor as ca

    path = _ledger(tmp_path)
    _record(path, "a-fixture", "advice:fixture", "fixture_observed")
    monkeypatch.setattr(cp, "surface_records", lambda surface: [])
    monkeypatch.setattr(cp, "_under_use", lambda **kw: {})
    counts = cp.surface_decline_counts("opener-lane", path=path)
    assert counts["offered"] == counts["triggered"] == counts["declined"] == {}
    assert cp.missed_selection("opener-lane", [], path=path)["rows"] == []
    result = cp.detect(path=path)
    assert not result["promotions"] and not result["demotions"]

    # A rail consult offers a whole phase's bindings, although only one contract runs.
    # The remaining offers must not become production evidence of silent non-use.
    surface = "rail-exercise:test"
    monkeypatch.setattr(ca, "binding_for", lambda *args, **kw: {"a-fixture", "z-production"})
    for i in range(cp.DEMOTION_MIN_TRIALS):
        ref = f"advice:rail-phase-{i}"
        for cid in ("a-fixture", "z-production"):
            capabilities.heartbeat(
                cid,
                "match",
                ref=ref,
                path=path,
                metadata={"surface": surface},
            )
        cp.record_trigger("a-fixture", ref, path=path, metadata={"surface": surface})
        cp.record_usefulness(
            "a-fixture",
            ref,
            path=path,
            useful=True,
            provenance="fixture_observed",
            evidence="one contract in the phase passed",
        )
    counts = cp.surface_decline_counts(surface, path=path)
    assert counts["offered"] == counts["silent"] == counts["triggered"] == {}
    assert cp.missed_selection(surface, [], path=path)["rows"] == []
    assert cp.propose_demotions(surface, path=path) == []

    # Genuine production offers still feed the same detector.
    capabilities.heartbeat(
        "z-production",
        "match",
        ref="advice:production-offer",
        path=path,
        metadata={"surface": "opener-lane"},
    )
    counts = cp.surface_decline_counts("opener-lane", path=path)
    assert counts["offered"] == counts["silent"] == {"z-production": 1}


def test_rail_exercise_records_fixture_provenance(monkeypatch):
    import capability_advisor as ca

    calls = []
    monkeypatch.setattr(ca, "surfaces_binding", lambda ids: {"a-fixture": ["rail-exercise:test"]})
    monkeypatch.setattr(ca, "advise", lambda *args, **kw: {"experiment_id": "advice:fixture"})
    monkeypatch.setattr(cp, "record_trigger", lambda *args, **kw: True)
    monkeypatch.setattr(cp, "record_usefulness", lambda *args, **kw: calls.append(kw))
    assert (
        rail_exercise._record(
            {"capability_id": "a-fixture", "status": "pass", "reason": "passed contract"}
        )
        == "recorded"
    )
    assert calls[0]["provenance"] == "fixture_observed"
    assert calls[0]["metadata"]["source"] == "rail_exercise"


def test_corpus_headlines_exclude_fixture_only_evidence_and_preserve_production(
    tmp_path, monkeypatch
):
    path = _ledger(tmp_path)
    monkeypatch.setattr(capabilities, "_fleet_edge_counts", lambda **kw: {})
    _record(path, "a-fixture", "advice:fixture-pass", "fixture_observed")
    _record(path, "a-fixture", "advice:fixture-fail", "fixture_observed", useful=False)
    fixture = cp.report(path=path)
    assert fixture["experiment_count"] == fixture["resolved_experiment_count"] == 0
    assert fixture["verdict_count"] == fixture["verdicts_outcome_derived"] == 0
    assert fixture["verdicts_by_provenance"] == {}
    assert fixture["verdicts_self_reported_share"] is None
    assert fixture["capabilities_with_evidence"] == 0
    assert fixture["fixture_experiment_count"] == fixture["fixture_verdict_count"] == 2

    _record(path, "z-production", "advice:production", "machine_observed")
    mixed = cp.report(path=path)
    assert mixed["experiment_count"] == mixed["resolved_experiment_count"] == 1
    assert mixed["verdict_count"] == mixed["verdicts_outcome_derived"] == 1
    assert mixed["verdicts_by_provenance"] == {"machine_observed": 1}
    assert mixed["capabilities_with_evidence"] == 1
    assert mixed["fixture_experiment_count"] == mixed["fixture_verdict_count"] == 2
