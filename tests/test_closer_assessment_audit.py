"""Typed per-PR judgment survives explicitly unsupported optional producer audit."""

import hashlib
import json
from pathlib import Path

import pytest
from test_closer_evidence_recovery import (
    LogGitHub,
    declared_unknown_assessment,
    log_request,
    recovery_callbacks,
)
from test_closer_reasoning_boundary import controlled_world, native_world  # noqa: F401

import closer_dispatcher as d
import closer_evidence_recovery as recovery
import closer_stage_feedback
import dispatcher as transport


def test_retained_assessment_is_not_invalidated_by_incomplete_producer_audit(tmp_path, monkeypatch):
    callbacks, context, _calls, refreshes = recovery_callbacks(
        tmp_path, monkeypatch, recover=True, initial_missing=False
    )
    context["prepare"]["drift_audit_candidate"] = True
    snap = context["prepare"]["snapshot"]
    # Preserve this old synthetic fixture verbatim; its free-form locators do
    # not acquire supported collection identities under the new typed carrier.
    old = json.loads(
        (Path(__file__).parent / "fixtures/closer-assessment-audit-incomplete.json").read_text()
    )
    feedback, prompts = [], []
    monkeypatch.setattr(closer_stage_feedback, "record_stage", lambda *a, **kw: feedback.append(kw))

    def offload(agent, prompt, **kwargs):
        prompts.append(prompt)
        return callbacks["_inert_caller"].response(
            {"exit": 0, "output": json.dumps(declared_unknown_assessment(prompt))},
            agent,
            prompt,
            **kwargs,
        )

    monkeypatch.setattr(transport, "offload", offload)
    result = callbacks["interpret"](snap["target"], context)
    assert feedback[-1]["structurally_invalid"] is False
    assert result["proposal"]["semantic_assessment"]["schema"] == "closer-semantic-assessment/v2"
    assert result["producer_audit"]["status"] == "audit_unavailable_current_contract"
    assert result["producer_audit"]["summary"] is None
    assert result["drift_review_at"] is None and result["producer_sha256"] is None
    assert result["authority"] is False and not refreshes and len(prompts) == 1
    assert '"drift_audit"' not in prompts[0]
    with pytest.raises(ValueError):
        recovery.validate_requests(old, snap, {}, {"requirements": []})


def test_incomplete_audit_preserves_existing_typed_recovery_route(tmp_path, monkeypatch):
    callbacks, context, calls, refreshes = recovery_callbacks(
        tmp_path, monkeypatch, recover=True, initial_missing=False
    )
    prep = context["prepare"]
    prep["drift_audit_candidate"] = True
    snap = prep["snapshot"]
    prep["recovery_requests"] = recovery.validate_requests(
        {"evidence_requests": [log_request(snap)]}, snap, {}, {"requirements": []}
    )
    gh = d.GitHub(deadline=context["deadline"])
    log_gh = LogGitHub(snap)
    monkeypatch.setattr(gh, "api", log_gh.api)
    monkeypatch.setattr(gh, "read_text", log_gh.read_text, raising=False)
    result = callbacks["interpret"](snap["target"], context)
    assert len(calls) == 1 and len(log_gh.reads) == 3
    assert callbacks["_host_plans"][0]["source"] == "existing_readiness_and_admitted_requests"
    assert result["evidence_recovery"]["outcome"] == "recovered"
    assert result["producer_audit"]["status"] == "audit_unavailable_current_contract"
    assert result["authority"] is False


def test_tool_free_audit_prompt_never_requests_file_commands(native_world, capsys):  # noqa: F811
    from test_closer_reasoning_boundary import invoke, protected_unchanged

    w = native_world
    invoke(w, capsys)
    prompt = w.events["prompts"][0]
    assert "Inspect retained files with read-only commands" not in prompt
    assert "TOOL-FREE SOURCE-SCOPED SEMANTIC REVIEW" in prompt
    assert '"drift_audit"' not in prompt
    assert "drift_review_complete" not in prompt
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
    callbacks, context, calls, _refreshes = recovery_callbacks(
        tmp_path,
        monkeypatch,
        recover=True,
        initial_missing=False,
    )
    prep = context["prepare"]
    prep["drift_audit_candidate"] = True
    snap = prep["snapshot"]
    feedback = []
    monkeypatch.setattr(closer_stage_feedback, "record_stage", lambda *a, **kw: feedback.append(kw))
    if boundary in {"core_mismatch", "wrong_head"}:
        # A NEW deliberately inconsistent physical snapshot tests the actual
        # source/packet identity boundary, not a forbidden model audit field.
        source = json.loads(Path(prep["snapshot_ref"]["path"]).read_bytes())
        if boundary == "core_mismatch":
            source["sources"][0]["body"] += "\n- Deliberately changed original requirement"
        else:
            source["head"] = "f" * 40
        path = tmp_path / "new-inconsistent-snapshot.json"
        path.write_text(json.dumps(source, sort_keys=True))
        prep["snapshot_ref"] = {
            "path": str(path),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
        result = callbacks["interpret"](snap["target"], context)
        if boundary == "wrong_head":
            assert not calls
            assert result["classification"] == "evidence_pending"
        else:
            # The old packet cannot be assessed as the changed original BODY.
            # A real host source refresh first produces a NEW packet; only then
            # one typed all-UNKNOWN answer can examine the new obligation.
            assert len(calls) == 1
            assert _refreshes == [{"source"}]
            assert (
                callbacks["_host_plans"][0]["source"] == "existing_readiness_and_admitted_requests"
            )
            assert result["evidence_recovery"]["outcome"] == "recovered"
            assert result["recovered_prepare"]["packet_ref"] != prep["packet_ref"]
            assert result.get("integration_status") != "accepted"
            assert result["producer_audit"]["status"] == "audit_unavailable_current_contract"
            assert result["drift_review_at"] is None
        assert result["authority"] is False
        return

    def offload(agent, prompt, **kwargs):
        response = declared_unknown_assessment(prompt)
        if boundary == "bad_envelope":
            response["findings"] = "not an array"
        elif boundary == "bad_audit":
            response["drift_mismatches"] = {"malformed": True}
        elif boundary == "bad_summary":
            response["drift_summary"] = {"not": "a string"}
        elif boundary == "huge_summary":
            response["drift_summary"] = '雪\n"' * 3000
        elif boundary == "inline_summary":
            response["drift_summary"] = "\n" * 7000
        return callbacks["_inert_caller"].response(
            {"exit": 0, "output": json.dumps(response)}, agent, prompt, **kwargs
        )

    monkeypatch.setattr(transport, "offload", offload)
    result = callbacks["interpret"](snap["target"], context)
    assert result["authority"] is False
    if boundary in {"huge_summary", "inline_summary"} and "producer_audit" not in result:
        # Actual immutable transport limits can refuse this invented legacy
        # carrier before semantic parsing; never recast it as a completed audit.
        assert result["status"] == "uncertain"
        assert result["provider_outcome"]["actual"]["retry_safe"] is False
        assert result.get("drift_review_at") is None
        return
    assert result["producer_audit"]["status"] == "audit_unavailable_current_contract"
    assert result["producer_audit"]["summary"] is None
    assert result["drift_review_at"] is None and result["producer_sha256"] is None
    if boundary == "complete":
        assert feedback[-1]["structurally_invalid"] is False
        assert (
            result["proposal"]["semantic_assessment"]["schema"] == "closer-semantic-assessment/v2"
        )
    else:
        # Extra root keys remain invalid: unsupported audit data cannot become
        # acceptance/collection authority or a fabricated completed audit.
        assert feedback[-1]["structurally_invalid"] is True
        assert result.get("integration_status") != "accepted"
