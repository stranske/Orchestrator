"""The activation audit audits LIVE rows only, and still accounts for every ledger row.

A row retired on 2026-09-03 was reported `blocked` with `no_heartbeat` every day for 19 days. It was
the audit's only finding in that period, so every one of those reports was a false positive: a
retired or superseded row is not expected to fire, and the advisor and the admission report already
skipped it. These tests pin the rule where the audit applies it, and pin that a not-live row is
NAMED in the report rather than silently dropped.
"""

from __future__ import annotations

import capabilities
import capability_activation_audit as audit


def _report(tmp_path, monkeypatch):
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "silent_mod.py").write_text("def run():\n    return 1\n", encoding="utf-8")
    ledger = tmp_path / "capabilities.json"
    rows = {}
    for cap_id, status in (
        ("live-silent", "wired"),
        ("retired-silent", "retired"),
        ("superseded-silent", "superseded"),
    ):
        row = capabilities._blank_capability(cap_id)
        row.update(
            status=status,
            matcher={"kind": "tick_phase", "name": cap_id},
            entrypoint="silent_mod.py:run",
        )
        rows[cap_id] = row
    capabilities.save(rows, ledger)
    monkeypatch.setattr(audit, "HERE", tree)
    monkeypatch.setattr(audit, "sibling_checkouts", lambda: [])
    monkeypatch.setattr(
        audit, "_fleet_label_index", lambda use_cache=True: {"generated_at": 0, "repos": {}}
    )
    monkeypatch.delenv("ORCH_CAPABILITY_HEARTBEATS", raising=False)
    return audit.audit(path=ledger, use_cache=True)


def test_a_retired_or_superseded_row_is_never_reported_blocked(tmp_path, monkeypatch):
    rep = _report(tmp_path, monkeypatch)
    assert rep["by_defect"] == {"no_heartbeat": ["live-silent"]}
    assert (rep["total"], rep["blocked"], rep["reachable"]) == (1, 1, 0)
    assert rep["reachable_ids"] == []


def test_the_whole_ledger_is_still_accounted_for(tmp_path, monkeypatch):
    rep = _report(tmp_path, monkeypatch)
    assert rep["ledger_total"] == 3
    assert rep["not_audited"] == {
        "retired": ["retired-silent"],
        "superseded": ["superseded-silent"],
    }
    assert rep["not_audited_count"] == 2
    rows = {row["capability_id"]: row for row in rep["rows"]}
    assert set(rows) == {"live-silent", "retired-silent", "superseded-silent"}
    assert rows["retired-silent"]["audited"] is False
    assert rows["retired-silent"]["reachable"] is None
    assert rows["retired-silent"]["defects"] == []
    assert "retired" in rows["retired-silent"]["not_audited_because"]
    assert rows["live-silent"]["audited"] is True


def test_the_scorecard_names_not_live_rows_without_calling_them_blocked(tmp_path, monkeypatch):
    scorecard = audit.format_scorecard(_report(tmp_path, monkeypatch))
    assert "retired-silent" in scorecard and "NOT LIVE" in scorecard
    blocked = scorecard.split("## Blocked, by defect", 1)[1].split("## Per capability", 1)[0]
    assert "live-silent" in blocked
    assert "retired-silent" not in blocked


def test_the_audit_declares_the_population_it_audits(tmp_path, monkeypatch):
    """The tick re-baselines when this changes, so a change to NOT_LIVE_STATES mints no verdict."""
    rep = _report(tmp_path, monkeypatch)
    assert rep[capabilities.FINDING_POPULATION_KEY] == capabilities.live_finding_population()


def test_not_live_states_are_canonical_lifecycle_states():
    assert capabilities.NOT_LIVE_STATES
    assert capabilities.NOT_LIVE_STATES <= set(capabilities.CANONICAL_STATES)
