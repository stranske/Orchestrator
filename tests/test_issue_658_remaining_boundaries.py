"""Exercise residual production fallback paths without a live ledger or process."""

import socket
import subprocess
from contextlib import nullcontext
from unittest.mock import Mock, call

import pytest

import capabilities
import capability_advisor as advisor
import capability_propensity
import dispatcher


@pytest.fixture(autouse=True)
def isolated_runtime(monkeypatch, tmp_path):
    """Refuse unstubbed external effects and isolate both runtime roots."""
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ORCH_LOCAL_RUNTIME", str(tmp_path / "runtime"))

    def forbidden(*args, **kwargs):
        pytest.fail("residual boundary test attempted an external process or network call")

    external_effect = Mock(side_effect=forbidden)
    for name in ("run", "Popen"):
        monkeypatch.setattr(subprocess, name, external_effect)
    for name in ("socket", "create_connection"):
        monkeypatch.setattr(socket, name, external_effect)
    for name in ("delegate", "delegate_remote", "offload"):
        monkeypatch.setattr(dispatcher, name, external_effect)
    monkeypatch.setattr(advisor.fleet_shapes, "_gh_json", external_effect)
    monkeypatch.setattr(capabilities, "REG", tmp_path / "ledger.json")
    yield
    external_effect.assert_not_called()


@pytest.mark.parametrize("observable", [None, False, True])
def test_repo_probe_memo_preserves_unknown_and_negative_results(monkeypatch, tmp_path, observable):
    """Candidates sharing a repo fact must reuse even an unknown or negative observation."""
    probe = Mock(return_value={"observable": observable, "detail": "fixture result", "markers": []})
    monkeypatch.setitem(advisor.REPO_FACT_PROBES, "fixture-surface", probe)
    for cap_id in ("fixture-one", "fixture-two"):
        monkeypatch.setitem(
            advisor.CAPABILITY_PRECONDITIONS, cap_id, {"requires": "fixture-surface"}
        )
    facts = {}
    first = advisor.evaluate_precondition("fixture-one", repo_path=str(tmp_path), facts=facts)
    second = advisor.evaluate_precondition("fixture-two", repo_path=str(tmp_path), facts=facts)

    probe.assert_called_once_with(str(tmp_path))
    for result in (first, second):
        assert result["requirement_met"] is observable
        assert result["precondition_met"] is observable
        assert result["requirement_evidence"] == probe.return_value
        assert result["suggested_decline_kind"] == (
            "precondition_unmet" if observable is False else None
        )
        assert bool(result["unevaluated_because"]) is (observable is None)


def test_repo_probe_memo_is_scoped_to_checkout_and_consult(monkeypatch, tmp_path):
    """A cached absence in one checkout cannot suppress another checkout or a later consult."""
    probe = Mock(
        side_effect=[
            {"observable": False, "detail": "no surface", "markers": []},
            {"observable": True, "detail": "has surface", "markers": ["html:index.html"]},
            {"observable": True, "detail": "new surface", "markers": ["html:new.html"]},
        ]
    )
    monkeypatch.setitem(advisor.REPO_FACT_PROBES, "fixture-surface", probe)
    monkeypatch.setitem(
        advisor.CAPABILITY_PRECONDITIONS, "fixture-one", {"requires": "fixture-surface"}
    )
    facts = {}
    first_path, other_path = str(tmp_path / "first"), str(tmp_path / "other")
    first = advisor.evaluate_precondition("fixture-one", repo_path=first_path, facts=facts)
    other = advisor.evaluate_precondition("fixture-one", repo_path=other_path, facts=facts)
    later = advisor.evaluate_precondition("fixture-one", repo_path=first_path, facts={})

    assert first["precondition_met"] is False
    assert other["precondition_met"] is True
    assert later["precondition_met"] is True
    assert [call.args for call in probe.call_args_list] == [
        (first_path,),
        (other_path,),
        (first_path,),
    ]
    assert len(facts) == 2


@pytest.mark.parametrize("failure_index", [0, 1, 2])
@pytest.mark.parametrize("explicit_ref", [False, True])
def test_missing_fact_batch_continues_after_failure_and_counts_only_new_records(
    monkeypatch, tmp_path, failure_index, explicit_ref
):
    """One unavailable write must not lose later facts or count duplicate observations."""
    ledger = tmp_path / "ledger.json"
    task = "bounded assessment"
    ref = "advice:existing-consult" if explicit_ref else advisor.experiment_id(task)
    previous_error = {"capability_id": "prior", "error_type": "ValueError"}
    advice = {"task": task, "fact_missing_record_errors": [previous_error.copy()]}
    if explicit_ref:
        advice["experiment_id"] = ref
    rows = [
        {"capability_id": "one", "fact": "size unknown"},
        {"capability_id": "two"},
        {"capability_id": "three", "fact": "labels unknown"},
    ]
    detail = f"fixture storage failure at {ledger}"
    outcomes = [True, False]
    outcomes.insert(failure_index, OSError(detail))
    record = Mock(side_effect=outcomes)
    monkeypatch.setattr(capability_propensity, "record_fact_missing", record)

    assert advisor._record_fact_missing(advice, rows, surface="fixture", path=ledger) == 1

    assert record.call_args_list == [
        call(
            row["capability_id"],
            ref,
            fact=row.get("fact", "PR facts unknown"),
            surface="fixture",
            path=ledger,
        )
        for row in rows
    ]
    expected = {
        "task": task,
        "fact_missing_record_errors": [
            previous_error,
            {
                "capability_id": rows[failure_index]["capability_id"],
                "surface": "fixture",
                "error_type": "OSError",
            },
        ],
    }
    if explicit_ref:
        expected["experiment_id"] = ref
    assert advice == expected
    assert detail not in str(advice)
    assert not ledger.exists()


@pytest.mark.parametrize(
    "matcher,trigger,reason",
    [
        ({"kind": ""}, {}, "empty_matcher_kind"),
        ({"lane": "closer"}, {"lane": "opener"}, "lane_mismatch"),
    ],
)
def test_empty_typed_and_mismatched_legacy_matchers_fail_closed(matcher, trigger, reason):
    """Malformed typed and nonmatching legacy declarations never authorize work."""
    assert capabilities._matches_trigger({"matcher": matcher}, trigger) == (False, [reason])


def test_expiry_renewal_with_invalid_prior_state_restores_observed(monkeypatch, tmp_path):
    """A malformed retirement record cannot restore a noncanonical authority state."""
    cap = {"expiry": 1, "status": "retired", "event_history": []}
    saved = []
    monkeypatch.setattr(capabilities, "_locked", lambda path: nullcontext())
    monkeypatch.setattr(
        capabilities, "_read_ledger_unlocked", lambda path: {"capabilities": {"one": cap}}
    )
    monkeypatch.setattr(
        capabilities, "_write_ledger_unlocked", lambda path, rows: saved.append(rows.copy())
    )
    monkeypatch.setattr(capabilities, "renewal_blocker", lambda *args, **kwargs: None)
    monkeypatch.setattr(capabilities, "expiry_retirement", lambda cap: {"from": "unrecognized"})
    monkeypatch.setattr(capabilities, "validate_capability", lambda *args, **kwargs: None)
    result = capabilities.renew(
        "one",
        reason="fixture evidence",
        evidence_refs=["fixture:one"],
        path=tmp_path / "ledger.json",
        timestamp=100,
    )
    assert result["status"] == "observed"
    assert cap["next_transition"] == "retired"
    assert saved[0]["one"]["status"] == "observed"
    assert cap["event_history"][0]["to"] == "observed"


def test_usage_report_explains_all_encoded_gates_without_claiming_promotion():
    """A checkable denominator is distinct from meeting the gate's bounds."""
    usage = {
        "total": 1,
        "active_last_28d": [],
        "never_invoked": ["one"],
        "capabilities_with_verdict": 0,
        "capabilities_with_outcome_link": 0,
        "gate_encoded": ["one"],
        "threshold_undefined": [],
        "rows": [],
    }
    text = capabilities.format_usage_report(usage)
    assert "All 1 gated capabilities have a machine-checkable threshold" in text
    assert "unmet bounds" in text
    assert "READY TO LIFT (gate satisfied; 1 gates are machine-checkable): 0" in text
