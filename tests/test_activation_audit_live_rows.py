"""The activation audit audits LIVE rows only, and still accounts for every ledger row.

A row retired on 2026-09-03 was reported `blocked` with `no_heartbeat` every day for 19 days. It was
the audit's only finding in that period, so every one of those reports was a false positive: a
retired or superseded row is not expected to fire, and the advisor and the admission report already
skipped it. These tests pin the rule where the audit applies it, and pin that a not-live row is
NAMED in the report rather than silently dropped.
"""

from __future__ import annotations

import json
import sys

import pytest

import capabilities
import capability_activation_audit as audit
import capability_advisor


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


# --------------------------------------------------------------------------- advisor reach
# `advisor_reach` probes live rows only and used to compute `regressed` as the baseline minus
# `reachable`, so a RETIRED baseline row read as a reach regression in the artifact's
# `advisor_reach` sub-report. Every baseline id now lands in exactly one of reachable / regressed /
# `baseline_not_live`, on both returns. The direct half measures the dispatcher-derived MAP and
# names a not-live target beside it; only the two fields that claim reach count live targets.

# A matcher free text reaches (`advisor_reach` probes every TASK_SIGNALS task type), and one it
# never can: a kind-shaped matcher engages only by entering its gate.
ROUTED = {"field": "task_type", "operator": "in", "value": ["testgen"]}
GATE_ONLY = {"kind": "closer_gate", "name": "high_stakes_review"}
# A fixed direct-entry map, so these tests do not move when the dispatcher's map does.
DIRECT_MAP = {"offload": "offload", "runtime_ac": "runtime-ac-checks", "testgen": "testgen-lane"}


def _row(cap_id: str, *, status: str = "wired", matcher: dict = ROUTED) -> dict:
    return {"capability_id": cap_id, "status": status, "matcher": matcher}


def _named(by_status: dict[str, list[str]]) -> set[str]:
    return {cap_id for ids in by_status.values() for cap_id in ids}


def test_a_not_live_baseline_row_is_named_not_regressed():
    ids = sorted(audit.ADVISOR_REACH_BASELINE)
    caps = {cap_id: _row(cap_id) for cap_id in ids}
    caps[ids[0]]["status"] = "retired"
    caps[ids[1]]["status"] = "superseded"
    reach = audit.advisor_reach(caps)
    assert reach["regressed"] == [], reach
    assert reach["baseline_not_live"] == {"retired": [ids[0]], "superseded": [ids[1]]}, reach
    assert set(ids[2:]) <= set(reach["reachable"]), reach


def test_every_baseline_id_lands_in_exactly_one_of_reachable_regressed_not_live():
    reachable_id, lost_id, retired_id, superseded_id, *absent = sorted(audit.ADVISOR_REACH_BASELINE)
    assert absent, "the fixture needs a baseline id with no ledger row at all"
    caps = {
        reachable_id: _row(reachable_id),
        lost_id: _row(lost_id, matcher=GATE_ONLY),
        retired_id: _row(retired_id, status="retired"),
        superseded_id: _row(superseded_id, status="superseded"),
    }
    reach = audit.advisor_reach(caps)
    buckets = [
        set(reach["reachable"]) & audit.ADVISOR_REACH_BASELINE,
        set(reach["regressed"]),
        _named(reach["baseline_not_live"]),
    ]
    assert sum(len(b) for b in buckets) == len(audit.ADVISOR_REACH_BASELINE), buckets
    assert set().union(*buckets) == audit.ADVISOR_REACH_BASELINE, buckets
    assert buckets[0] == {reachable_id}, reach
    # A live row that lost reach IS a regression, and so is an id with no ledger row: the carve-out
    # must swallow neither, or the silence it exists to break would read as a pass.
    assert buckets[1] == {lost_id, *absent}, reach
    assert reach["baseline_not_live"] == {
        "retired": [retired_id],
        "superseded": [superseded_id],
    }, reach


def test_an_unreadable_advisor_cannot_relabel_a_retirement_either(monkeypatch):
    ids = sorted(audit.ADVISOR_REACH_BASELINE)
    monkeypatch.setitem(sys.modules, "capability_advisor", None)  # the import now raises
    reach = audit.advisor_reach({ids[0]: _row(ids[0], status="retired")})
    assert "unreadable" in reach, reach
    assert reach["baseline_not_live"] == {"retired": [ids[0]]}, reach
    # Reach cannot be measured, so everything live or unknown stays regressed -- loudly.
    assert reach["regressed"] == ids[1:], reach


def test_a_not_live_map_target_is_named_beside_the_map_and_never_counted_as_reach(monkeypatch):
    monkeypatch.setattr(capability_advisor, "direct_entry", lambda: dict(DIRECT_MAP))
    caps = {
        "offload": _row("offload", status="retired", matcher=GATE_ONLY),
        "runtime-ac-checks": _row("runtime-ac-checks", matcher=GATE_ONLY),
        "testgen-lane": _row("testgen-lane", status="superseded"),
    }
    reach = audit.advisor_reach(caps)
    # THE MAP is still the map: that is what catches a dispatcher edit that narrows reach.
    assert reach["direct_entry_targets"] == ["offload", "runtime-ac-checks", "testgen-lane"]
    assert reach["direct_entry_regressed"] == [], reach
    assert reach["direct_entry_not_live"] == {
        "retired": ["offload"],
        "superseded": ["testgen-lane"],
    }, reach
    # ...but a target the advisor will not offer is not reach, through the map or at all. Before,
    # the superseded `testgen-lane` left `reachable` and reappeared in both fields via the map.
    assert reach["reachable"] == [], reach
    assert reach["direct_entry_only"] == ["runtime-ac-checks"], reach
    assert reach["total_reachable_count"] == 1, reach


@pytest.mark.parametrize("status", ["wired", "retired"])
def test_a_map_edit_regresses_the_direct_baseline_whatever_the_ledger_says(monkeypatch, status):
    """Unchanged by the not-live rule, pinned so it stays so: the map is code, the ledger is not."""
    shrunk = {task: cap for task, cap in DIRECT_MAP.items() if cap != "offload"}
    monkeypatch.setattr(capability_advisor, "direct_entry", lambda: dict(shrunk))
    reach = audit.advisor_reach({"offload": _row("offload", status=status, matcher=GATE_ONLY)})
    assert "offload" in reach["direct_entry_regressed"], reach
    assert "offload" not in reach["direct_entry_targets"], reach


def test_the_artifact_names_a_retired_baseline_row_instead_of_a_regression(tmp_path, monkeypatch):
    """End to end: the `advisor_reach` sub-report `audit()` publishes, and no row blamed for it."""
    retired_id, live_id = sorted(audit.ADVISOR_REACH_BASELINE)[:2]
    ledger = tmp_path / "capabilities.json"
    rows = {}
    for cap_id, status in ((retired_id, "retired"), (live_id, "wired")):
        row = capabilities._blank_capability(cap_id)
        row.update(status=status, matcher=ROUTED, entrypoint="silent_mod.py:run")
        rows[cap_id] = row
    capabilities.save(rows, ledger)
    tree = tmp_path / "tree"
    tree.mkdir()
    monkeypatch.setattr(audit, "HERE", tree)
    monkeypatch.setattr(audit, "sibling_checkouts", lambda: [])
    monkeypatch.setattr(
        audit, "_fleet_label_index", lambda use_cache=True: {"generated_at": 0, "repos": {}}
    )
    monkeypatch.delenv("ORCH_CAPABILITY_HEARTBEATS", raising=False)
    rep = audit.audit(path=ledger, use_cache=True)
    reach = rep["advisor_reach"]
    assert reach["baseline_not_live"] == {"retired": [retired_id]}, reach
    assert retired_id not in reach["regressed"] and live_id in reach["reachable"], reach
    assert "advisor_reach_regression" not in rep["by_defect"], rep["by_defect"]
