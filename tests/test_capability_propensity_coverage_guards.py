"""Hermetic coverage for propensity's conservative refusal and fallback paths."""

from __future__ import annotations

import socket
import subprocess
import sys

import pytest

import capability_propensity as propensity


@pytest.fixture(autouse=True)
def isolated_runtime_and_external_calls(monkeypatch, tmp_path):
    attempted = []

    def forbid_external(*args, **kwargs):
        attempted.append((args, kwargs))
        pytest.fail("propensity coverage test attempted external I/O")

    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ORCH_LOCAL_RUNTIME", str(tmp_path / "runtime"))
    monkeypatch.setattr(subprocess, "run", forbid_external)
    monkeypatch.setattr(subprocess, "Popen", forbid_external)
    monkeypatch.setattr(socket, "socket", forbid_external)
    monkeypatch.setattr(socket, "create_connection", forbid_external)
    yield
    assert attempted == []


def test_window_weighting_and_propensity_refuse_unknown_or_unavailable_inputs(monkeypatch):
    assert propensity._within_window({"timestamp": "unknown"}, now=100, window_days=1) is False
    assert propensity._within_window({"timestamp": 0}, now=100, window_days=1) is True

    monkeypatch.setitem(sys.modules, "research_subjects", None)
    weighted = propensity._weigh_verdicts([(True, "machine_observed", "judge", "kind")])
    assert weighted["n_eff"] == propensity.provenance_weight("machine_observed")

    monkeypatch.setattr(propensity, "usefulness", lambda **_kwargs: {"rows": {}})
    with pytest.raises(ValueError, match="unknown capability"):
        propensity.propensity("missing")


def test_heartbeat_and_deliverable_validation_fail_closed(monkeypatch):
    monkeypatch.setattr(
        propensity.capabilities,
        "production_heartbeat",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("offline")),
    )
    assert propensity._capability_heartbeat("output", "test") is None
    with pytest.raises(ValueError, match="owner/repo#positive"):
        propensity._fleet_deliverable("not a deliverable")


def test_late_outcome_lookup_does_not_cross_experiment_boundaries(monkeypatch):
    monkeypatch.setattr(
        propensity,
        "experiments",
        lambda **_kwargs: [
            {
                "experiment_id": "advice:other",
                "late_outcomes": {"cap": {"direction": "landed"}},
                "late_outcome_orphans": {},
                "useful": ["cap"],
                "not_useful": [],
            },
            {
                "experiment_id": "advice:wanted",
                "late_outcomes": {},
                "late_outcome_orphans": {},
                "useful": [],
                "not_useful": [],
            },
        ],
    )
    assert propensity.existing_late_outcome("cap", "advice:wanted") is None
    assert propensity.verdict_in_window("cap", "advice:wanted") is None


def test_offer_improvements_keeps_bad_binding_rows_and_unreoffered_declines_visible(monkeypatch):
    import capability_advisor

    monkeypatch.setattr(
        capability_advisor,
        "SURFACE_BINDINGS",
        {"one": ["documented", ("undocumented", "reason")], "bad": 3},
    )
    monkeypatch.setattr(propensity, "read_switches", lambda **_kwargs: {})
    monkeypatch.setattr(
        propensity,
        "declared_facts",
        lambda cap_id, **_kwargs: {"how_to_use": "yes"} if cap_id == "documented" else {},
    )
    monkeypatch.setattr(
        propensity,
        "experiments",
        lambda **_kwargs: [
            {
                "decline_kinds": {"undocumented": "offer_too_thin", "other": "wrong_match"},
                "reoffers": {"other": {"facts": {}}},
            },
            {"decline_kinds": {"undocumented": "wrong_match"}, "reoffers": {}},
        ],
    )
    result = propensity.propose_offer_improvements(window_days=7)
    assert result["declared_nothing"] == ["undocumented"]
    assert result["said_so_by_capability"] == {"undocumented": 1}
    assert result["demotable_declines_awaiting_their_reoffer_round"] == 1


def test_switch_and_declared_fact_fallbacks_preserve_unknowns(monkeypatch):
    import switch_review

    monkeypatch.setattr(
        switch_review,
        "switch_states",
        lambda **_kwargs: (_ for _ in ()).throw(OSError("unavailable")),
    )
    switches = propensity.read_switches()
    assert switches["held_off"] == []
    assert switches["on_but_idle"] == []
    assert "unreadable" in switches

    class BrokenRows:
        def get(self, _key, _default=None):
            raise TypeError("bad rows")

    assert propensity.declared_facts("anything", switches=BrokenRows()) == {}


@pytest.mark.parametrize(
    ("decline_kind", "expected"),
    [("not-a-kind", "unknown decline kind"), ("scope_too_small", "structural reason")],
)
def test_reoffer_refuses_unknown_and_structural_declines(decline_kind, expected):
    if decline_kind == "not-a-kind":
        with pytest.raises(ValueError, match=expected):
            propensity.record_reoffer("cap", "advice:one", decline_kind=decline_kind)
    else:
        result = propensity.record_reoffer("cap", "advice:one", decline_kind=decline_kind)
        assert result["reoffered"] is False and expected in result["reason"]


def test_consult_and_late_outcome_validation_refuse_bad_inputs(tmp_path):
    assert (
        propensity.record_consult_outcome(
            "wrong",
            direction="landed",
            evidence="PR #1",
            corroboration="run",
            provenance="machine_observed",
            path=tmp_path / "capabilities.json",
        )["recorded"]
        is False
    )
    assert (
        propensity.record_consult_outcome(
            "advice:one",
            direction="unknown",
            evidence="PR #1",
            corroboration="run",
            provenance="machine_observed",
            path=tmp_path / "capabilities.json",
        )["recorded"]
        is False
    )
    with pytest.raises(ValueError, match="requires evidence"):
        propensity.record_late_outcome(
            "cap",
            "advice:one",
            direction="landed",
            evidence="",
            provenance="machine_observed",
            corroboration="run",
        )
    with pytest.raises(ValueError, match="experiment_id"):
        propensity.record_late_outcome(
            "cap",
            "wrong",
            direction="landed",
            evidence="PR #1",
            provenance="machine_observed",
            corroboration="run",
        )


def test_unreadable_outcome_lines_are_reported_not_dropped(tmp_path):
    ledger = tmp_path / "capabilities.json"
    store = propensity.consult_outcomes_path(ledger)
    store.write_text('{"direction":"landed"}\nnot-json\n')
    rows = propensity.load_consult_outcomes(ledger)
    assert rows[0] == {"direction": "landed"}
    assert rows[-1]["direction"] == "unreadable"
    assert rows[-1]["unreadable_lines"] == 1
