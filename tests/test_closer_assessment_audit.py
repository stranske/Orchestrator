"""Per-PR interpretation remains usable when a bounded producer audit is pending."""

import json
from pathlib import Path

import pytest
from test_closer_evidence_recovery import LogGitHub, log_request, recovery_callbacks
from test_closer_reasoning_boundary import native_world  # noqa: F401

import closer_dispatcher as d
import closer_evidence_recovery as recovery
import closer_stage_feedback
import dispatcher as transport


def test_retained_assessment_is_not_invalidated_by_incomplete_producer_audit(tmp_path, monkeypatch):
    callbacks, context, calls, refreshes = recovery_callbacks(
        tmp_path, monkeypatch, recover=True, initial_missing=False
    )
    prep = context["prepare"]
    prep.update(drift_audit_candidate=True, producer_sha256="producer")
    snap = prep["snapshot"]
    proposal = json.loads(
        (Path(__file__).parent / "fixtures/closer-assessment-audit-incomplete.json").read_text()
    )
    proposal.update(target=snap["target"], head=snap["head"], base=snap["base"])
    feedback = []
    monkeypatch.setattr(closer_stage_feedback, "record_stage", lambda *a, **kw: feedback.append(kw))
    monkeypatch.setattr(
        transport,
        "offload",
        lambda *a, **kw: callbacks["_inert_caller"].response(
            {"exit": 0, "output": json.dumps(proposal)}, *a, **kw
        ),
    )
    result = callbacks["interpret"](snap["target"], context)
    assert feedback[-1]["structurally_invalid"] is False
    assert result["proposal"]["evidence_requests"] == proposal["evidence_requests"]
    assert result["producer_audit"]["status"] == "pending"
    assert result["drift_review_at"] is None and result["producer_sha256"] is None
    assert result["authority"] is False and not refreshes
    # The retained model used free-form locators; audit admission must not invent
    # supported retrieval identities or weaken the existing request validator.
    with pytest.raises(ValueError):
        recovery.validate_requests(proposal, snap, {}, {"requirements": []})


def test_incomplete_audit_preserves_existing_typed_recovery_route(tmp_path, monkeypatch):
    callbacks, context, calls, refreshes = recovery_callbacks(
        tmp_path, monkeypatch, recover=True, initial_missing=False
    )
    prep = context["prepare"]
    prep.update(drift_audit_candidate=True, producer_sha256="producer")
    snap = prep["snapshot"]
    gh = d.GitHub(deadline=context["deadline"])
    log_gh = LogGitHub(snap)
    monkeypatch.setattr(gh, "api", log_gh.api)
    monkeypatch.setattr(gh, "read_text", log_gh.read_text, raising=False)
    prompts = []

    def offload(_agent, prompt, **_kw):
        prompts.append(prompt)
        response = dict(
            target=snap["target"],
            head=snap["head"],
            base=snap["base"],
            classification="uncertain",
            substantive_needs=["Acceptance still needs judgment"],
            findings=[],
            decision={"criteria": []},
            drift_review_complete=False,
            drift_mismatches=[],
            drift_summary="Only inline content available",
        )
        if len(prompts) == 1:
            response["evidence_requests"] = [log_request(snap)]
        return callbacks["_inert_caller"].response(
            {"exit": 0, "output": json.dumps(response)}, _agent, prompt, **_kw
        )

    monkeypatch.setattr(transport, "offload", offload)
    result = callbacks["interpret"](snap["target"], context)
    assert len(prompts) == 2 and len(log_gh.reads) == 3
    assert result["evidence_recovery"]["outcome"] == "recovered"
    assert result["authority"] is False


def test_tool_free_audit_prompt_never_requests_file_commands(native_world, capsys):  # noqa: F811
    from test_closer_reasoning_boundary import invoke, protected_unchanged

    w = native_world
    w.response.update(drift_review_complete=False, drift_summary="Only supplied content inspected")
    invoke(w, capsys)
    prompt = w.events["prompts"][0]
    assert "Inspect retained files with read-only commands" not in prompt
    assert "Inspect only supplied inline content" in prompt
    assert "incomplete producer audit does not invalidate" in prompt
    protected_unchanged(w)


@pytest.mark.parametrize(
    "boundary",
    [
        "complete",
        "core_mismatch",
        "wrong_head",
        "bad_envelope",
        "bad_audit",
        "bad_summary",
        "huge_summary",
        "inline_summary",
    ],
)
def test_audit_separation_preserves_binding_and_core_gap_guards(tmp_path, monkeypatch, boundary):
    callbacks, context, _, _ = recovery_callbacks(
        tmp_path, monkeypatch, recover=True, initial_missing=False
    )
    context["prepare"].update(drift_audit_candidate=True, producer_sha256="producer")
    snap = context["prepare"]["snapshot"]
    response = {
        "target": snap["target"],
        "head": snap["head"],
        "base": snap["base"],
        "classification": "uncertain",
        "substantive_needs": ["Acceptance remains unproven"],
        "findings": [],
        "decision": {"criteria": []},
        "drift_review_complete": True,
        "drift_mismatches": [],
        "drift_summary": "Supplied samples inspected",
    }
    if boundary == "core_mismatch":
        response.update(drift_review_complete=False, drift_mismatches=["source:original_body"])
    elif boundary == "bad_summary":
        response.update(drift_review_complete=False, drift_summary={"not": "a string"})
    elif boundary == "inline_summary":
        response.update(drift_review_complete=False, drift_summary=("\n" * 7000))
    elif boundary == "huge_summary":
        response.update(drift_review_complete=False, drift_summary=('雪\n"' * 30000))
    elif boundary == "bad_audit":
        response["drift_mismatches"] = {"malformed": True}
    elif boundary == "wrong_head":
        response["head"] = "different-head"
    elif boundary == "bad_envelope":
        response["findings"] = "not an array"
    feedback = []
    monkeypatch.setattr(closer_stage_feedback, "record_stage", lambda *a, **kw: feedback.append(kw))
    monkeypatch.setattr(
        transport,
        "offload",
        lambda *a, **kw: callbacks["_inert_caller"].response(
            {"exit": 0, "output": json.dumps(response)}, *a, **kw
        ),
    )
    result = callbacks["interpret"](snap["target"], context)
    assert result["authority"] is False
    if boundary == "complete":
        assert result["producer_audit"]["status"] == "complete"
        assert result["drift_review_at"] and result["producer_sha256"] == "producer"
    elif boundary in {"bad_summary", "huge_summary", "inline_summary"}:
        import closer_pipeline

        assert result["producer_audit"]["status"] == "pending"
        assert result["drift_review"] == result["producer_audit"]["summary"]
        assert result["drift_review"] is None or isinstance(result["drift_review"], str)
        closer_pipeline._bounded(result, limit=closer_pipeline.CALLBACK_RESULT_BYTES)
        if boundary == "inline_summary":
            assert "proposal_ref" not in result
            assert result["proposal"]["drift_summary"] == response["drift_summary"]
            assert result["producer_audit"]["summary_truncated"] is True
    elif boundary == "bad_audit":
        assert feedback[-1]["structurally_invalid"] is False
        assert result["producer_audit"]["status"] == "pending"
        assert result["drift_review_at"] is None
    elif boundary == "core_mismatch":
        assert "semantic_drift_mismatch:source:original_body" in result["substantive_needs"]
        assert result["producer_audit"]["status"] == "pending"
        assert result["drift_review_at"] is None and result["producer_sha256"] is None
    else:
        assert feedback[-1]["structurally_invalid"] is True
        assert result["producer_audit"]["status"] == "pending"
        assert result["drift_review_at"] is None
