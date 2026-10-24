"""Hermetic coverage for propensity's conservative refusal and fallback paths."""

from __future__ import annotations

import re
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


def test_window_weighting_and_propensity_refuse_unknown_or_unavailable_inputs(
    monkeypatch, tmp_path
):
    day = 86400
    # An unknown or missing timestamp is outside every window; timestamp 0 is a real one.
    assert propensity._within_window({"timestamp": "unknown"}, now=100, window_days=1) is False
    assert propensity._within_window({}, now=100, window_days=1) is False
    assert propensity._within_window({"timestamp": 0}, now=100, window_days=1) is True
    # The window edge and its scale, with values on both sides: an event exactly `window_days`
    # old is inside, one second older is not.
    for days in (1, 3):
        assert propensity._within_window({"timestamp": 0}, now=days * day, window_days=days) is True
        assert (
            propensity._within_window({"timestamp": 0}, now=days * day + 1, window_days=days)
            is False
        )

    # Without `research_subjects` the correlation discount falls back to a local reciprocal:
    # verdicts from one judge arm share one weight and a second arm adds its own. A single
    # verdict cannot tell `1 / len(arm)` from `1.0`, so use arms of three and of two.
    monkeypatch.setitem(sys.modules, "research_subjects", None)
    weight = propensity.provenance_weight("machine_observed")
    weighted = propensity._weigh_verdicts(
        [
            (True, "machine_observed", "judge-a", "kind"),
            (True, "machine_observed", "judge-a", "kind"),
            (False, "machine_observed", "judge-a", "kind"),
            (True, "machine_observed", "judge-b", "kind"),
            (False, "machine_observed", "judge-b", "kind"),
        ]
    )
    assert weighted["n_eff"] == pytest.approx(2 * weight)
    assert weighted["effective_useful"] == pytest.approx(weight * (2 / 3 + 1 / 2), abs=1e-4)
    assert weighted["independent_arms"] == 2
    assert weighted["judge_arms"] == ["judge-a", "judge-b"]

    # An unknown capability is named and refused; a known one is answered, so the refusal means
    # "not in the ledger" and not "propensity refuses everything".
    ledger = tmp_path / "capabilities.json"
    known = propensity.capabilities._blank_capability("known-cap")
    known["status"] = "generated"
    propensity.capabilities.save({"known-cap": known}, ledger)
    assert propensity.propensity("known-cap", path=ledger)["capability_id"] == "known-cap"
    with pytest.raises(ValueError, match=r"^unknown capability: missing$"):
        propensity.propensity("missing", path=ledger)


def test_heartbeat_and_deliverable_validation_fail_closed(monkeypatch):
    attempts = []

    def offline(*args, **kwargs):
        attempts.append((args, kwargs))
        raise OSError("offline")

    monkeypatch.setattr(propensity.capabilities, "production_heartbeat", offline)
    assert propensity._capability_heartbeat("output", "test") is None
    # The heartbeat was really attempted, once, under this capability's own id. A swallow that
    # never has anything to swallow would pass even if the call did not exist.
    assert attempts == [(("capability-propensity", "output"), {"ref": "test"})]

    refusal = re.escape("--deliverable for trigger/useful must be owner/repo#positive-PR-number")
    for bad in (
        "not a deliverable",
        "",
        "   ",
        "o/r",  # no PR number
        "o/r#0",  # not positive
        "o/r#01",  # leading zero
        "o/r#1x",  # trailing text
        "/r#1",  # no owner
        "o/#1",  # no repo
        "o#1",  # no separator
    ):
        with pytest.raises(ValueError, match=refusal):
            propensity._fleet_deliverable(bad)
    # A valid key is accepted, stripped and lower-cased.
    assert propensity._fleet_deliverable("  Owner/Repo-X.y_z#12  ") == "owner/repo-x.y_z#12"


def test_late_outcome_lookup_does_not_cross_experiment_boundaries(monkeypatch):
    other = {
        "experiment_id": "advice:other",
        "late_outcomes": {"cap": {"direction": "corroborates"}},
        "late_outcome_orphans": {"orphan-cap": {"direction": "refutes"}},
        "useful": ["cap"],
        "not_useful": ["orphan-cap"],
    }
    wanted = {
        "experiment_id": "advice:wanted",
        "late_outcomes": {},
        "late_outcome_orphans": {},
        "useful": [],
        "not_useful": [],
    }
    monkeypatch.setattr(propensity, "experiments", lambda **_kwargs: [other, wanted])

    # Nothing that belongs to the OTHER experiment is visible from the wanted one, whether it is
    # an attached late outcome, an orphan or a verdict bucket...
    for cap in ("cap", "orphan-cap"):
        assert propensity.existing_late_outcome(cap, "advice:wanted") is None
        assert propensity.verdict_in_window(cap, "advice:wanted") is None
    # ...and the same lookups DO answer for the experiment that owns them, so a None above means
    # "not in this experiment" and not "these lookups never find anything".
    assert propensity.existing_late_outcome("cap", "advice:other") == {"direction": "corroborates"}
    assert propensity.existing_late_outcome("orphan-cap", "advice:other") == {
        "direction": "refutes"
    }
    assert propensity.verdict_in_window("cap", "advice:other") == "useful"
    assert propensity.verdict_in_window("orphan-cap", "advice:other") == "not_useful"


def test_offer_improvements_keeps_bad_binding_rows_and_unreoffered_declines_visible(monkeypatch):
    import capability_advisor

    monkeypatch.setattr(
        capability_advisor,
        "SURFACE_BINDINGS",
        {
            "one": ["documented", ("undocumented", "reason")],
            "bad": 3,
            "empty": {"__none__": "deliberately empty"},
        },
    )
    monkeypatch.setattr(propensity, "read_switches", lambda **_kwargs: {})
    monkeypatch.setattr(
        propensity,
        "declared_facts",
        lambda cap_id, **_kwargs: {"how_to_use": "yes"} if cap_id == "documented" else {},
    )
    asked = []

    def fake_experiments(**kwargs):
        asked.append(kwargs)
        return [
            {
                "decline_kinds": {"undocumented": "offer_too_thin", "other": "wrong_match"},
                "reoffers": {"other": {"facts": {}}},
            },
            {
                "decline_kinds": {"undocumented": "wrong_match", "third": "wrong_match"},
                "reoffers": {},
            },
        ]

    monkeypatch.setattr(propensity, "experiments", fake_experiments)

    result = propensity.propose_offer_improvements(window_days=7)

    assert asked == [{"path": None, "window_days": 7}]
    assert result["measured"] is True
    assert result["window_days"] == 7
    # The malformed row is skipped, the tuple row is unpacked and the deliberately-empty marker
    # is not a capability.
    assert result["bound_capabilities"] == 2
    assert result["declared_nothing"] == ["undocumented"]
    assert result["offers_that_declare_nothing"] == 1
    assert result["said_so_by_capability"] == {"undocumented": 1}
    assert result["declines_saying_the_offer_was_thin"] == 1
    # Held: the two wrong_match declines no re-offer has answered (undocumented, third). "other"
    # was answered, and offer_too_thin is not demotable, so neither is held.
    assert result["demotable_declines_awaiting_their_reoffer_round"] == 2


def test_switch_and_declared_fact_fallbacks_preserve_unknowns(monkeypatch):
    import switch_review

    monkeypatch.setattr(
        switch_review,
        "switch_states",
        lambda **_kwargs: (_ for _ in ()).throw(OSError("unavailable")),
    )
    # An unreadable source answers with no rows AND says why: unknown is never "none held".
    assert propensity.read_switches() == {
        "held_off": [],
        "on_but_idle": [],
        "unreadable": "OSError: unavailable",
    }

    class BrokenRows:
        def get(self, _key, _default=None):
            raise TypeError("bad rows")

    # `declared_facts` uses the switch read it was handed and never makes a second one, and it
    # absorbs malformed rows.
    monkeypatch.setattr(
        propensity,
        "read_switches",
        lambda **_kwargs: pytest.fail("declared_facts re-read the switches it was handed"),
    )
    assert propensity.declared_facts("anything", switches=BrokenRows()) == {}
    held = {"held_off": [{"capability": "cap-x", "flag": "ORCH_X", "state": "off"}]}
    assert propensity.declared_facts("cap-x", switches=held) == {
        "gate_flag": "ORCH_X",
        "gate_state": "off",
    }


@pytest.mark.parametrize(
    ("decline_kind", "expected"),
    [("not-a-kind", "unknown decline kind"), ("scope_too_small", "structural reason")],
)
def test_reoffer_refuses_unknown_and_structural_declines(tmp_path, decline_kind, expected):
    ledger = tmp_path / "capabilities.json"
    if decline_kind == "not-a-kind":
        with pytest.raises(ValueError, match=expected):
            propensity.record_reoffer("cap", "advice:one", decline_kind=decline_kind, path=ledger)
    else:
        result = propensity.record_reoffer(
            "cap", "advice:one", decline_kind=decline_kind, path=ledger
        )
        row = propensity.DECLINE_KINDS[decline_kind]
        assert result["reoffered"] is False
        assert expected in result["reason"]
        assert (result["capability"], result["experiment"]) == ("cap", "advice:one")
        assert result["kind_fix"] == row["fix"]
        assert row["task_shape"][:200] in result["remedy"]


def test_consult_and_late_outcome_validation_refuse_bad_inputs(tmp_path):
    ledger = tmp_path / "capabilities.json"
    consult = {
        "direction": "landed",
        "evidence": "PR #1",
        "corroboration": "run",
        "provenance": "machine_observed",
        "path": ledger,
    }

    # Start from the opposite state: a fully valid consult outcome IS recorded...
    recorded = propensity.record_consult_outcome("advice:good", **consult)
    assert recorded["recorded"] is True
    assert (recorded["experiment"], recorded["direction"]) == ("advice:good", "landed")
    # ...and each bad input is refused by its own guard, in that guard's own words.
    wrong_id = propensity.record_consult_outcome("wrong", **consult)
    assert wrong_id["recorded"] is False
    assert wrong_id["reason"] == "experiment_id must start with 'advice:'"
    bad_direction = propensity.record_consult_outcome(
        "advice:one", **{**consult, "direction": "unknown"}
    )
    assert bad_direction["recorded"] is False
    assert bad_direction["reason"] == "direction must be one of ('landed', 'failed')"
    # A refusal writes nothing: only the one valid outcome is in the store.
    stored = propensity.load_consult_outcomes(ledger)
    assert [row["experiment_id"] for row in stored] == ["advice:good"]

    late = {
        "direction": "corroborates",
        "provenance": "machine_observed",
        "corroboration": "run",
        "path": ledger,
    }
    # Valid input passes every validation and is refused only for having no verdict to attach to.
    attached = propensity.record_late_outcome("cap", "advice:one", evidence="PR #1", **late)
    assert attached["attached"] is False
    assert "no in-window verdict" in attached["reason"]
    for evidence in ("", "   "):
        with pytest.raises(ValueError, match="a late outcome requires evidence"):
            propensity.record_late_outcome("cap", "advice:one", evidence=evidence, **late)
    with pytest.raises(ValueError, match="experiment_id must start with 'advice:': 'wrong'"):
        propensity.record_late_outcome("cap", "wrong", evidence="PR #1", **late)
    # `landed` is the consult vocabulary; a late outcome says corroborates or refutes.
    with pytest.raises(ValueError, match="unknown late-outcome direction 'landed'"):
        propensity.record_late_outcome(
            "cap", "advice:one", evidence="PR #1", **{**late, "direction": "landed"}
        )


def test_unreadable_outcome_lines_are_reported_not_dropped(tmp_path):
    ledger = tmp_path / "capabilities.json"
    store = propensity.consult_outcomes_path(ledger)
    # Two readable rows, two unreadable lines, and blank lines, which are neither.
    store.write_text('{"direction":"landed"}\n\nnot-json\n   \n{"direction":"failed"}\n{broken\n')
    rows = propensity.load_consult_outcomes(ledger)
    assert rows[:2] == [{"direction": "landed"}, {"direction": "failed"}]
    assert rows[-1] == {
        "idempotency_key": "",
        "experiment_id": "",
        "direction": "unreadable",
        "unreadable_lines": 2,
    }
    assert len(rows) == 3
