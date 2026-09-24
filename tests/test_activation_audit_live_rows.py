"""The activation audit audits LIVE rows only, and still accounts for every ledger row.

A row retired on 2026-09-03 was reported `blocked` with `no_heartbeat` every day for 19 days. It was
the audit's only finding in that period, so every one of those reports was a false positive: a
retired or superseded row is not expected to fire, and the advisor and the admission report already
skipped it. These tests pin the rule where the audit applies it, and pin that a not-live row is
NAMED in the report rather than silently dropped.
"""

from __future__ import annotations

import json

import pytest

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


# --------------------------------------------------------------------------- progress
# `progress()` compares `reachable_ids` with the last snapshot's. A not-live row is excluded from
# `reachable_ids`, so a row that was reachable and has since been RETIRED left the set through a
# lifecycle action -- it did not regress. And that reading is only sound against a snapshot drawn
# from the same live-row population, which a snapshot now records.


def _history(tmp_path, snapshot: dict):
    path = tmp_path / "history.json"
    path.write_text(json.dumps({"snapshots": [snapshot]}), encoding="utf-8")
    return path


def _was_reachable(rep: dict, ids: list[str]) -> dict:
    """The report as it would have read when `ids` were live and reachable."""
    return dict(
        rep, generated_at=rep["generated_at"] - 86400, reachable=len(ids), reachable_ids=ids
    )


def test_a_row_retired_since_the_last_snapshot_is_not_a_regression(tmp_path, monkeypatch):
    rep = _report(tmp_path, monkeypatch)
    path = tmp_path / "history.json"
    then = ["live-silent", "retired-silent", "superseded-silent"]
    assert audit.record_snapshot(_was_reachable(rep, then), path=path)["recorded"]
    prog = audit.progress(rep, path=path)
    assert prog["baseline"] == rep["generated_at"] - 86400, prog
    # The live row that lost reachability is still a regression, reported beside the others.
    assert prog["regressed"] == ["live-silent"], prog
    assert prog["retired_since"] == {
        "retired": ["retired-silent"],
        "superseded": ["superseded-silent"],
    }, prog
    assert prog["gained"] == [], prog


def test_the_snapshot_records_its_population_so_the_next_run_is_comparable(tmp_path, monkeypatch):
    rep = _report(tmp_path, monkeypatch)
    path = tmp_path / "history.json"
    assert audit.record_snapshot(rep, path=path)["recorded"]
    (last,) = audit.load_history(path)
    key = capabilities.FINDING_POPULATION_KEY
    assert last[key] == rep[key] == capabilities.live_finding_population(), last
    # FULLY DRAINED reads as a comparison with nothing in it, not as an unknown baseline.
    prog = audit.progress(rep, path=path)
    assert prog["baseline"] == rep["generated_at"], prog
    assert (prog["gained"], prog["regressed"], prog["retired_since"]) == ([], [], {}), prog


@pytest.mark.parametrize(
    "population",
    [None, {"excluded_statuses": ["retired"]}],
    ids=["predates-population-recording", "different-population"],
)
def test_a_snapshot_from_another_population_is_an_unknown_baseline(
    tmp_path, monkeypatch, population
):
    rep = _report(tmp_path, monkeypatch)
    snapshot = {
        "generated_at": rep["generated_at"] - 86400,
        "total": 3,
        "reachable": 2,
        "blocked": 1,
        "reachable_ids": ["live-silent", "retired-silent"],
        "by_defect": {},
    }
    if population is not None:
        snapshot[capabilities.FINDING_POPULATION_KEY] = population
    prog = audit.progress(rep, path=_history(tmp_path, snapshot))
    assert prog["baseline"] is None, prog
    assert prog["baseline_unknown"]["snapshot_at"] == snapshot["generated_at"], prog
    # Nothing is compared against it: no guessed regression, no guessed retirement.
    assert not {"gained", "regressed", "retired_since"} & set(prog), prog
    assert "unknown baseline" in audit.format_scorecard(rep, prog)


@pytest.mark.parametrize(
    "snapshot_declares", [False, True], ids=["neither-declares", "only-the-snapshot-declares"]
)
def test_a_report_that_declares_no_population_is_never_compared(
    tmp_path, monkeypatch, snapshot_declares
):
    # An undeclared population never MATCHES, not even another undeclared one: assuming it does is
    # the same guess as comparing against a snapshot that predates the record.
    key = capabilities.FINDING_POPULATION_KEY
    rep = {k: v for k, v in _report(tmp_path, monkeypatch).items() if k != key}
    snapshot = {
        "generated_at": rep["generated_at"] - 86400,
        "total": 1,
        "reachable": 1,
        "blocked": 0,
        "reachable_ids": ["live-silent"],
        "by_defect": {},
    }
    if snapshot_declares:
        snapshot[key] = capabilities.live_finding_population()
    prog = audit.progress(rep, path=_history(tmp_path, snapshot))
    assert prog["baseline"] is None, prog
    assert not {"gained", "regressed", "retired_since"} & set(prog), prog
    # It names the missing declaration, rather than promising a next snapshot that cannot help.
    assert "declares no population" in prog["detail"], prog["detail"]


def test_the_scorecard_says_retired_since_not_regressed(tmp_path, monkeypatch):
    rep = _report(tmp_path, monkeypatch)
    path = tmp_path / "history.json"
    audit.record_snapshot(_was_reachable(rep, ["retired-silent"]), path=path)
    text = audit.format_scorecard(rep, audit.progress(rep, path=path))
    retired = next(line for line in text.splitlines() if "RETIRED SINCE" in line)
    assert "retired-silent" in retired, retired
    assert not any("REGRESSED" in line for line in text.splitlines()), text
