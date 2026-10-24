"""Old incomplete reads do not veto a complete review of identical requirements."""

import copy
import json
from pathlib import Path

import pytest
from test_repeat_verification_events import decision as original_decision
from test_repeat_verification_events import event

import verifier_evidence as verifier


def decision(*args, **changes):
    value = original_decision(*args, **changes)
    value["assessment_identity"] = {
        "canonical": True,
        "mode": "evaluate",
        "provider": "anthropic",
        "model": "",
        "model2": "",
    }
    value["result_admission"] = {
        "schema": "verifier-result-admission/v1",
        "mode": "evaluate",
        "payload_sha256": "a" * 64,
        "execution_receipt_sha256": "b" * 64,
    }
    return value


def recovered_703(data):
    """Synthetic authenticated recovery marker; never modify original six records."""
    comments = data["pr"]["comments"]["nodes"]
    for comment in comments:
        match = verifier.MARKER_RE.search(comment["body"])
        if match:
            fresh = json.loads(match.group(1))
            break
    fresh.update(
        run_id="40099999999",
        result_admission=decision()["result_admission"],
        retained_revalidation={
            "schema": "retained-revalidation/v1",
            "provider_generation_performed": False,
        },
    )
    result = copy.deepcopy(data)
    result["pr"]["comments"]["nodes"].append(
        {
            "author": {"login": "github-actions[bot]"},
            "url": "https://github.com/stranske/Orchestrator/pull/703#issuecomment-9999999999",
            "body": f"<!-- {verifier.MARKER} {json.dumps(fresh)} -->",
        }
    )
    return result


def incomplete(run=1, *, material="c", status="incomplete", **changes):
    value = decision(run, verification_event=event(material), **changes)
    for key in ("verdict", "provider_verdicts", "ci_failed"):
        value.pop(key, None)
    value.update(schema=verifier.STATE_MARKER, status=status)
    return value


@pytest.mark.parametrize("verdict", ["PASS", "NON_PASS"])
def test_new_complete_review_supersedes_older_incomplete_root_without_changing_history(verdict):
    old = incomplete()
    fresh = decision(2, verdict, verification_event=event("e"))
    before = copy.deepcopy([old, fresh])
    assert verifier.applicable_decision([fresh], [old]) is fresh
    assert [old, fresh] == before


def test_complete_descendant_preserves_causal_order_after_old_incomplete_read():
    old = incomplete()
    root = decision(2, "NON_PASS", verification_event=event("e"))
    fixed = decision(
        3,
        "PASS",
        verification_event=event("f"),
        predecessor_event_id=root["verification_event"]["event_id"],
    )
    assert verifier.applicable_decision([root, fixed], [old]) is fixed
    late = {**root, "run_id": "99"}
    assert verifier.applicable_decision([root, fixed, late], [old]) is fixed


def test_new_incomplete_descendant_still_masks_prior_pass():
    old = incomplete()
    root = decision(2, "PASS", verification_event=event("e"))
    pending = incomplete(
        3, material="f", predecessor_event_id=root["verification_event"]["event_id"]
    )
    assert verifier.applicable_decision([root], [old, pending]) is None
    assert verifier.applicable_decision([root], [old, pending], include_pending=True) is pending


@pytest.mark.parametrize(
    "change",
    [
        "newer",
        "pending",
        "different_request",
        "different_subject",
        "prior_semantic",
        "missing_parent",
        "cycle",
        "legacy",
    ],
)
def test_current_requirements_and_real_conflicts_are_not_discarded(change):
    old = incomplete()
    fresh = decision(2, "PASS", verification_event=event("e"))
    decisions = [fresh]
    if change == "newer":
        old["run_id"] = "3"
    elif change == "pending":
        old["status"] = "pending"
    elif change == "different_request":
        old["verification_event"] = event(request_sha256="f" * 64)
    elif change == "different_subject":
        old["verification_event"] = event(pr="8")
    elif change == "prior_semantic":
        decisions.append(decision(1, "NON_PASS", verification_event=event()))
    elif change == "missing_parent":
        fresh["predecessor_event_id"] = "0" * 64
    elif change == "cycle":
        old["predecessor_event_id"] = old["verification_event"]["event_id"]
    else:
        decisions.append(decision(1, "NON_PASS"))
    assert verifier.applicable_decision(decisions, [old]) is None


def test_original_703_publication_stays_unusable_until_new_admitted_recovery():
    data = json.loads(
        (
            Path(__file__).parent
            / "fixtures/verification_event/older_incomplete_then_complete.json"
        ).read_text()
    )
    before = copy.deepcopy(data)
    result = verifier.decision_from_pr(data["repo"], data["pr"], include_pending=True)
    assert result["status"] == "unusable"
    recovered = recovered_703(data)
    result = verifier.decision_from_pr(recovered["repo"], recovered["pr"], include_pending=True)
    assert result["verdict"] == "PASS" and result["run_id"] == "40099999999"
    assert len(result["_lifecycle_history"]) == 7
    assert recovered["pr"]["comments"]["nodes"][:-1] == data["pr"]["comments"]["nodes"]
    assert data == before


def test_pending_attempt_of_fresh_event_does_not_resurrect_obsolete_root():
    old = incomplete()
    root = decision(2, "PASS", verification_event=event("e"))
    pending = incomplete(3, material="e", status="pending")
    assert verifier.applicable_decision([root], [old, pending]) is None
    assert verifier.applicable_decision([root], [old, pending], include_pending=True) is pending


def test_recovery_selector_is_one_string_input_and_forwarded_without_shell_interpolation():
    import yaml

    p = Path(__file__).parents[1] / ".github/workflows/agents-verifier.yml"
    data = yaml.load(p.read_text(), Loader=yaml.BaseLoader)
    inputs = data["on"]["workflow_dispatch"]["inputs"]
    assert len(inputs) <= 10  # GitHub's actual dispatch contract, not three new scalar slots.
    assert inputs["verification_recovery"]["type"] == "string"
    assert inputs["verification_recovery"]["default"] == ""
    assert (
        data["jobs"]["verifier"]["with"]["verification_recovery"]
        == "${{ inputs.verification_recovery || '' }}"
    )


def test_full_703_history_updates_current_outcome_and_retains_original_failed_reads(
    monkeypatch, tmp_path
):
    import feedback

    data = json.loads(
        (
            Path(__file__).parent
            / "fixtures/verification_event/older_incomplete_then_complete.json"
        ).read_text()
    )
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "brain.db")
    feedback.record_run("703", "stranske/Orchestrator#703", "implement", "codex")
    feedback.record_outcome("703", merged=True)
    data = recovered_703(data)
    current = verifier.decision_from_pr(data["repo"], data["pr"], include_pending=True)
    assert verifier.record_decision("703", data["repo"], 703, current)
    assert not verifier.record_decision("703", data["repo"], 703, current)
    with feedback._conn() as conn:
        assert conn.execute(
            "SELECT verifier_verdict FROM outcomes WHERE run_id='703'"
        ).fetchone() == ("PASS",)
        rows = conn.execute(
            "SELECT payload_json FROM completion_events "
            "WHERE run_id='703' AND producer='verifier_evidence'"
        ).fetchall()
    retained = [json.loads(row[0])["verification"]["decision"] for row in rows]
    assert len(retained) == 7
    assert sum(row.get("status") == "incomplete" for row in retained) == 2
    assert len({row["verification_event"]["event_id"] for row in retained}) == 2


@pytest.mark.parametrize(
    "admission",
    [
        None,
        {},
        {
            "schema": "verifier-result-admission/v1",
            "mode": "evaluate",
            "payload_sha256": "bad",
            "execution_receipt_sha256": "b" * 64,
        },
    ],
)
def test_unadmitted_terminal_cannot_supersede_incomplete_root(admission):
    old = incomplete()
    fresh = decision(2, "PASS", verification_event=event("e"))
    if admission is None:
        fresh.pop("result_admission")
    else:
        fresh["result_admission"] = admission
    assert verifier.applicable_decision([fresh], [old]) is None


@pytest.mark.parametrize(
    "assessment",
    [None, {"canonical": False, "mode": "evaluate"}, {"canonical": True, "mode": "compare"}],
)
def test_unbound_or_mismatched_admission_does_not_supersede_history(assessment):
    old = incomplete()
    fresh = decision(2, "PASS", verification_event=event("e"))
    fresh["assessment_identity"] = assessment
    assert verifier.applicable_decision([fresh], [old]) is None
