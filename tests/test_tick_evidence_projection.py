"""The tick's output-change verdict for the activation audit: findings, not the roster.

The audit's production record read 4 useful, 24 not useful, all graded by the tick. Seven of the
not-useful verdicts compared nothing but `reachable_ids`, the list of HEALTHY capabilities, and two of
the useful ones were other sessions registering rows. So the projection grades `by_defect` alone, and
a projection that changes re-baselines instead of grading: otherwise this very repair would have
minted a "useful" verdict for the audit on its first tick.
"""

from __future__ import annotations

import json
import os

import capabilities
import capability_advisor
import capability_propensity as cp

CAP = "capability-activation-audit"


def test_a_registration_alone_does_not_change_the_audits_finding_set():
    before = cp.project_findings(CAP, {"by_defect": {"x": ["one"]}, "reachable_ids": ["a", "b"]})
    after = cp.project_findings(
        CAP, {"by_defect": {"x": ["one"]}, "reachable_ids": ["a", "b", "c"]}
    )
    assert before == after
    assert sum(len(v) for v in after.values()) == 1


def test_a_defect_resolving_does_change_it():
    assert cp.project_findings(CAP, {"by_defect": {"no_heartbeat": ["x"]}}) != cp.project_findings(
        CAP, {"by_defect": {}}
    )


def _fixture(tmp_path, monkeypatch, projection):
    ledger = tmp_path / "capabilities.json"
    row = capabilities._blank_capability("obs")
    row.update(status="wired", matcher={"kind": "tick_phase", "name": "obs"})
    capabilities.save({"obs": row}, ledger)
    state = tmp_path / "state"
    state.mkdir()
    steps = {"obs": {"key": "obs", "artifact": "obs.json", "cadence_days": 0}}
    monkeypatch.setattr(cp, "TICK_FINDING_FIELDS", {"obs": projection})
    # setitem, not assignment: monkeypatch puts the REAL tick binding back after the test, so no
    # later test in this process is offered a synthetic surface.
    monkeypatch.setitem(
        capability_advisor.SURFACE_BINDINGS, cp.TICK_SURFACE, {"obs": "synthetic observer"}
    )

    def write(payload, mtime):
        artifact = state / "obs.json"
        artifact.write_text(json.dumps(payload), encoding="utf-8")
        os.utime(artifact, (mtime, mtime))

    return ledger, state, steps, write


def test_a_changed_projection_rebaselines_instead_of_minting_a_verdict(tmp_path, monkeypatch):
    ledger, state, steps, write = _fixture(tmp_path, monkeypatch, {"a": ("id",), "b": ("id",)})
    now = 1_700_000_000
    write({"a": [{"id": "a"}], "b": [{"id": "b"}]}, now)
    cp.tick_evidence(now=now, state_dir=state, path=ledger, steps=steps)
    write({"a": [{"id": "a"}], "b": [{"id": "b"}]}, now + 1)
    second = cp.tick_evidence(now=now + 86400, state_dir=state, path=ledger, steps=steps)
    assert second["evaluated"][0]["useful"] is False
    monkeypatch.setattr(cp, "TICK_FINDING_FIELDS", {"obs": {"a": ("id",)}})
    write({"a": [{"id": "a"}], "b": [{"id": "changed"}]}, now + 2)
    changed = cp.tick_evidence(now=now + 2 * 86400, state_dir=state, path=ledger, steps=steps)
    assert changed["baselined"][0]["rebaselined"] is True
    assert changed["evaluated"] == [] and changed["verdicts_recorded"] == 0
    write({"a": [{"id": "a"}], "b": [{"id": "changed"}]}, now + 3)
    third = cp.tick_evidence(now=now + 3 * 86400, state_dir=state, path=ledger, steps=steps)
    assert third["evaluated"][0]["useful"] is False
    write({"a": [{"id": "new"}], "b": [{"id": "changed"}]}, now + 4)
    fourth = cp.tick_evidence(now=now + 4 * 86400, state_dir=state, path=ledger, steps=steps)
    assert fourth["evaluated"][0]["useful"] is True


def test_a_report_dropping_a_declared_key_rebaselines(tmp_path, monkeypatch):
    ledger, state, steps, write = _fixture(tmp_path, monkeypatch, {"a": ("id",), "b": ("id",)})
    now = 1_700_000_000
    write({"a": [{"id": "a"}], "b": [{"id": "b"}]}, now)
    cp.tick_evidence(now=now, state_dir=state, path=ledger, steps=steps)
    write({"a": [{"id": "a"}]}, now + 1)
    out = cp.tick_evidence(now=now + 86400, state_dir=state, path=ledger, steps=steps)
    assert out["baselined"][0]["rebaselined"] is True
    assert out["verdicts_recorded"] == 0


def test_a_legacy_state_entry_with_unchanged_keys_is_still_graded(tmp_path, monkeypatch):
    ledger, state, steps, write = _fixture(tmp_path, monkeypatch, {"a": ("id",), "b": ("id",)})
    now = 1_700_000_000
    write({"a": [{"id": "a"}], "b": [{"id": "b"}]}, now)
    cp.tick_evidence(now=now, state_dir=state, path=ledger, steps=steps)
    state_path = state / cp.TICK_EVIDENCE_STATE
    blob = json.loads(state_path.read_text(encoding="utf-8"))
    del blob["capabilities"]["obs"]["projection"]
    state_path.write_text(json.dumps(blob), encoding="utf-8")
    write({"a": [{"id": "changed"}], "b": [{"id": "b"}]}, now + 1)
    out = cp.tick_evidence(now=now + 86400, state_dir=state, path=ledger, steps=steps)
    assert out["evaluated"][0]["useful"] is True
