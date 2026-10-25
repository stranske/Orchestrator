"""A no-evaluation retry keeps its authenticated edge without rewriting history."""

import copy
import json
from pathlib import Path

import pytest
from test_repeat_verification_events import decision, event

import feedback
import verification_events as events
import verifier_evidence as verifier


def history():
    old = decision()
    current = event()
    pending = decision(2, verification_event=current, predecessor_event_id=events.legacy_id(old))
    for key in ("verdict", "provider_verdicts", "ci_failed"):
        pending.pop(key)
    pending.update(
        schema=verifier.STATE_MARKER,
        status="incomplete",
        retry_kind="known_no_evaluation",
        retryable=True,
        failure_cause="validated_no_evaluation",
        assessment_identity={
            "mode": "evaluate",
            "provider": "anthropic",
            "model": "claude-sonnet-5-5",
            "model2": "",
            "canonical": True,
        },
    )
    reviewed = decision(3, "PASS", verification_event=current)
    return old, pending, reviewed


def test_exact_public_714_retry_retains_original_parent_and_full_review():
    fixture = (
        Path(__file__).parent
        / "fixtures/verification_event/714_same_event_retry_missing_parent.json"
    )
    pr = json.loads(fixture.read_text())
    before = copy.deepcopy(pr)
    result = verifier.decision_from_pr("stranske/Orchestrator", pr)
    assert result and result["verdict"] == "PASS"
    assert result["run_id"] == "40155786809"
    assert len(result["_lifecycle_history"]) == 10
    assert pr == before


def test_public_retry_projection_preserves_source_scope_and_is_idempotent(monkeypatch, tmp_path):
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "brain.db")
    fixture = (
        Path(__file__).parent
        / "fixtures/verification_event/714_same_event_retry_missing_parent.json"
    )
    pr = json.loads(fixture.read_text())
    result = verifier.decision_from_pr("stranske/Orchestrator", pr)
    feedback.record_run(
        "pr-owner", "stranske/Orchestrator#714", "implement", "codex", source="keepalive"
    )
    feedback.record_outcome("pr-owner", merged=True, verifier_verdict="NON_PASS")
    feedback.record_run("source-owner", "stranske/Orchestrator#713", "implement", "codex")
    feedback.record_outcome("source-owner", merged=True, notes="original source scope")
    assert verifier.record_decision("pr-owner", "stranske/Orchestrator", 714, result)
    assert not verifier.record_decision("pr-owner", "stranske/Orchestrator", 714, result)
    with feedback._conn() as connection:
        assert connection.execute(
            "SELECT verifier_verdict FROM outcomes WHERE run_id='pr-owner'"
        ).fetchone() == ("PASS",)
        assert connection.execute(
            "SELECT verifier_verdict,notes FROM outcomes WHERE run_id='source-owner'"
        ).fetchone() == (None, "original source scope")
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM completion_events WHERE producer='verifier_evidence'"
            ).fetchone()[0]
            >= 10
        )


def test_newer_pending_retry_still_masks_the_recovered_public_pass():
    fixture = (
        Path(__file__).parent
        / "fixtures/verification_event/714_same_event_retry_missing_parent.json"
    )
    pr = json.loads(fixture.read_text())
    pending = json.loads(verifier.STATE_MARKER_RE.search(pr["comments"]["nodes"][-2]["body"])[1])
    pending.update(schema=verifier.STATE_MARKER, status="pending", run_id="40155786810")
    pr["comments"]["nodes"].append(
        {
            "author": {"login": "github-actions[bot]"},
            "url": "https://github.com/stranske/Orchestrator/pull/714#issuecomment-9999999999",
            "body": f"<!-- {verifier.STATE_MARKER} {json.dumps(pending)} -->",
        }
    )
    assert verifier.decision_from_pr("stranske/Orchestrator", pr) is None
    assert (
        verifier.decision_from_pr("stranske/Orchestrator", pr, include_pending=True)["status"]
        == "pending"
    )


@pytest.mark.parametrize("reverse", [False, True])
def test_known_no_evaluation_retry_can_omit_parent_without_new_authority(reverse):
    old, pending, reviewed = history()
    records = [old, reviewed]
    before = copy.deepcopy([records, pending])
    assert (
        verifier.applicable_decision(records[::-1] if reverse else records, [pending]) is reviewed
    )
    assert [records, pending] == before


@pytest.mark.parametrize("malformed", [{}, [], 7])
def test_malformed_parent_is_unusable_rather_than_crashing_the_reader(malformed):
    old, pending, reviewed = history()
    pending["predecessor_event_id"] = malformed
    assert verifier.applicable_decision([old, reviewed], [pending]) is None


@pytest.mark.parametrize("assessment", [None, "advisory", "invalid_mode", {}, []])
def test_only_explicit_valid_canonical_assessment_can_supply_the_retry_edge(assessment):
    old, pending, reviewed = history()
    if assessment is None:
        pending.pop("assessment_identity")
    elif assessment == "advisory":
        pending["assessment_identity"]["canonical"] = False
    elif isinstance(assessment, (dict, list)):
        pending["assessment_identity"]["mode"] = assessment
    else:
        pending["assessment_identity"]["mode"] = "advisory"
    assert verifier.applicable_decision([old, reviewed], [pending]) is None


@pytest.mark.parametrize(
    "kind",
    [
        "conflicting_edge",
        "originally_root",
        "same_attempt",
        "uncertain_prior",
        "terminal_prior",
        "missing_ancestor",
        "competing_material",
    ],
)
def test_missing_retry_parent_never_repairs_real_causal_ambiguity(kind):
    old, pending, reviewed = history()
    decisions = [old, reviewed]
    states = [pending]
    if kind == "conflicting_edge":
        reviewed["predecessor_event_id"] = "f" * 64
    elif kind == "originally_root":
        pending.pop("predecessor_event_id")
        reviewed["predecessor_event_id"] = events.legacy_id(old)
    elif kind == "same_attempt":
        reviewed["run_id"] = "2"
    elif kind == "uncertain_prior":
        pending.update(retry_kind="uncertain_execution", retryable=False)
    elif kind == "terminal_prior":
        decisions.append(
            decision(
                2,
                "NON_PASS",
                verification_event=event(),
                predecessor_event_id=events.legacy_id(old),
            )
        )
    elif kind == "missing_ancestor":
        pending["predecessor_event_id"] = "f" * 64
    else:
        decisions.append(
            decision(
                4, "PASS", verification_event=event("e"), predecessor_event_id=events.legacy_id(old)
            )
        )
    assert verifier.applicable_decision(decisions, states) is None
