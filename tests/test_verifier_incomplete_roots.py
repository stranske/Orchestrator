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
    import re

    source = (Path(__file__).parents[1] / ".github/workflows/agents-verifier.yml").read_text()
    # This non-package repository has no PyYAML runtime dependency. Check the
    # actual authored mapping's fixed indentation; GitHub validates YAML syntax.
    dispatch = re.search(r"(?ms)^  workflow_dispatch:\n    inputs:\n(.*?)(?=^[^ ]|\Z)", source)
    assert dispatch is not None
    inputs = re.findall(r"(?m)^      ([a-z_]+):$", dispatch[1])
    assert len(inputs) == len(set(inputs)) and len(inputs) <= 10
    assert "verification_recovery" in inputs
    recovery = re.search(r"(?m)^      verification_recovery:\n((?:        [^\n]*\n)+)", dispatch[1])
    assert recovery is not None
    assert "        type: string\n" in recovery[1]
    assert "        default: ''\n" in recovery[1]
    assert "      verification_recovery: ${{ inputs.verification_recovery || '' }}\n" in source


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


@pytest.mark.parametrize(
    "admission",
    [
        None,
        {
            "schema": "verifier-result-admission/v1",
            "mode": "evaluate",
            "payload_sha256": "bad",
            "execution_receipt_sha256": "b" * 64,
        },
    ],
)
def test_malformed_admitted_legacy_marker_cannot_resurrect_older_pass(admission):
    prior = original_decision(1, "PASS")
    invalid = original_decision(2, "PASS", result_admission=admission)
    pr = {
        "number": 7,
        "headRefOid": "a" * 40,
        "mergeCommit": {"oid": "b" * 40},
        "comments": {
            "nodes": [
                {
                    "author": {"login": "github-actions[bot]"},
                    "url": "https://github.com/o/r/pull/7#issuecomment-1",
                    "body": "\n".join(
                        f"<!-- {verifier.MARKER} {json.dumps(row)} -->" for row in [prior, invalid]
                    ),
                }
            ]
        },
    }
    assert verifier.decision_from_pr("o/r", pr) is None
    assert verifier.decision_from_pr("o/r", pr, include_pending=True)["status"] == "unusable"


def test_foreign_admission_failure_does_not_mask_this_prs_valid_legacy_pass():
    prior = original_decision(1, "PASS")
    foreign = original_decision(2, "PASS", pr="8", result_admission=None)
    pr = {
        "number": 7,
        "headRefOid": "a" * 40,
        "mergeCommit": {"oid": "b" * 40},
        "comments": {
            "nodes": [
                {
                    "author": {"login": "github-actions[bot]"},
                    "url": "https://github.com/o/r/pull/7#issuecomment-1",
                    "body": "\n".join(
                        f"<!-- {verifier.MARKER} {json.dumps(row)} -->" for row in [prior, foreign]
                    ),
                }
            ]
        },
    }
    assert verifier.decision_from_pr("o/r", pr)["verdict"] == "PASS"


def test_compare_admission_supersedes_older_incomplete_root():
    old = incomplete()
    fresh = decision(2, "PASS", verification_event=event("e"), provider_verdicts=["PASS", "PASS"])
    fresh["assessment_identity"].update(mode="compare", provider="")
    fresh["result_admission"]["mode"] = "compare"
    assert verifier.applicable_decision([fresh], [old]) is fresh


def pr_records(rows):
    return {
        "number": 7,
        "headRefOid": "a" * 40,
        "mergeCommit": {"oid": "b" * 40},
        "comments": {
            "nodes": [
                {
                    "author": {"login": "github-actions[bot]"},
                    "url": f"https://github.com/o/r/pull/7#issuecomment-{index}",
                    "body": f"<!-- {verifier.MARKER} {json.dumps(row)} -->",
                }
                for index, row in enumerate(rows, 1)
            ]
        },
    }


def malformed_marker(kind):
    if kind == "legacy":
        return original_decision(2, "PASS", result_admission=None)
    value = decision(2, "PASS", verification_event=event("e"))
    value["verification_event"]["event_id"] = "malformed"
    return value


@pytest.mark.parametrize("bad_kind", ["legacy", "event"])
@pytest.mark.parametrize("repair_kind", ["legacy", "event"])
@pytest.mark.parametrize("same_run_retry", [False, True])
def test_fresh_valid_repair_supersedes_ordered_invalid_observation(
    bad_kind, repair_kind, same_run_retry
):
    prior = original_decision(1, "PASS")
    bad = malformed_marker(bad_kind)
    fresh = decision(2 if same_run_retry else 3, "PASS", run_attempt="2" if same_run_retry else "1")
    if repair_kind == "event":
        fresh.update(
            verification_event=event("f"),
            predecessor_event_id=verifier.verification_events.legacy_id(prior),
        )
    records = [prior, bad, fresh]
    before = copy.deepcopy(records)
    selected = verifier.decision_from_pr("o/r", pr_records(records), include_pending=True)
    assert selected["verdict"] == "PASS" and selected["run_id"] == fresh["run_id"]
    observations = [
        row
        for row in selected["_lifecycle_history"]
        if row["schema"] == verifier.OBSERVATION_SCHEMA
    ]
    assert len(observations) == 1 and observations[0]["run_id"] == "2"
    assert "verdict" not in observations[0]
    assert observations[0]["source_url"].endswith("#issuecomment-2")
    assert records == before


def test_observation_ingestion_cannot_resurrect_stale_pass_or_erase_fresh_repair(
    monkeypatch, tmp_path
):
    import feedback

    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "brain.db")
    feedback.record_run("r", "o/r#7", "implement", "codex", source="keepalive")
    feedback.record_outcome("r", merged=True)
    old = original_decision(1, "PASS")
    bad = malformed_marker("legacy")
    fresh = decision(
        3,
        "PASS",
        verification_event=event("f"),
        predecessor_event_id=verifier.verification_events.legacy_id(old),
    )

    def parse(records):
        return verifier.decision_from_pr("o/r", pr_records(records), include_pending=True)

    def current():
        with feedback._conn() as connection:
            return connection.execute(
                "SELECT verifier_verdict FROM outcomes WHERE run_id='r'"
            ).fetchone()[0]

    assert verifier.record_decision("r", "o/r", 7, parse([old]))
    verifier.record_decision("r", "o/r", 7, parse([old, bad]))
    assert current() is None
    verifier.record_decision("r", "o/r", 7, parse([old]))
    assert current() is None  # Out-of-order old input cannot erase the observation.
    assert verifier.record_decision("r", "o/r", 7, parse([old, bad, fresh]))
    assert current() == "PASS"
    verifier.record_decision("r", "o/r", 7, parse([old, bad]))
    assert current() == "PASS"  # An old invalid observation cannot erase its repair.
    with feedback._conn() as connection:
        rows = connection.execute(
            "SELECT payload_json FROM completion_events WHERE run_id='r' AND producer='verifier_evidence'"
        ).fetchall()
    history = [json.loads(row[0])["verification"]["decision"] for row in rows]
    assert len(history) == 3
    assert sum(row["schema"] == verifier.OBSERVATION_SCHEMA for row in history) == 1
