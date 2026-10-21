"""Available packet data can be assessed without converting omissions into proof."""

from contextlib import contextmanager

import pytest
from test_closer_reasoning_boundary import native_world as native_world

import adapters
import closer_dispatcher as cd
import closer_evidence as ce
import closer_resources as cr
import dispatcher
import execution_profiles


@pytest.fixture
def packet(tmp_path):
    snap = {
        "target": "acme/repo#12",
        "kind": "pr",
        "state": "OPEN",
        "head": "a" * 40,
        "base": "b" * 40,
        "sources": [
            {
                "target": "acme/repo#3",
                "body": "## Tasks\n- Preserve original task\n## Acceptance Criteria\n- Original criterion\n",
                "state": "open",
                "labels": [],
            }
        ],
        "threads": [],
        "review_revision": "r1",
        "checks": None,
    }

    def api(endpoint, **kwargs):
        if "/compare/" in endpoint:
            return {
                "status": "ahead",
                "ahead_by": 1,
                "files": [
                    {
                        "filename": "src/a.py",
                        "status": "modified",
                        "sha": "c" * 40,
                        "patch": "@@ -1 +1 @@\n-old\n+new",
                        "additions": 1,
                        "deletions": 1,
                        "changes": 2,
                    }
                ],
            }
        if "/actions/runs?" in endpoint:
            return {"workflow_runs": []}
        raise AssertionError(endpoint)

    value = ce.collect_evidence(snap, api, tmp_path, action="collect_evidence")
    assert ce.validate_packet(value)
    readiness = ce.action_evidence_readiness(snap, value, "collect_evidence", retained_dir=tmp_path)
    assert not readiness["ready"]
    return value, readiness


def test_actual_partial_packet_preserves_available_content_and_gaps(packet):
    value, readiness = packet
    context = cd.data_only_evidence_context(value, readiness=readiness)
    assert context["readiness"] == readiness
    source = context["sections"]["source"]
    assert source["content_supplied"] is True
    assert source["content"] == value["sections"]["source"]["content"]
    assert "Original criterion" in source["content"][0]["body"]
    assert source["retained"] == value["sections"]["source"]["retained"]
    ci = context["sections"]["ci_artifacts"]
    assert ci["status"] == value["sections"]["ci_artifacts"]["status"]
    assert ci["status"] != "complete"
    assert not context.get("acceptance_authority")
    assert not context.get("action_authority")


def test_omitted_section_is_not_claimed_inspected(packet):
    value, readiness = packet
    context = cd.data_only_evidence_context(value, readiness=readiness, max_bytes=1)
    source = context["sections"]["source"]
    assert source["content_supplied"] is False
    assert "content" not in source
    assert source["content_bytes"] > 1
    assert "not inspected" in source["context_gap"]
    assert source["retained"] == value["sections"]["source"]["retained"]
    assert context["readiness"] == readiness


@pytest.mark.parametrize("limit", [True, False, 0, -1, "256"])
def test_bad_context_budget_rejected(packet, limit):
    with pytest.raises(ValueError, match="byte limit"):
        cd.data_only_evidence_context(packet[0], readiness=packet[1], max_bytes=limit)


def test_invalid_packet_not_diagnosed_as_valid_missing_evidence(packet):
    value, readiness = packet
    value["head"] = "f" * 40
    with pytest.raises(ValueError, match="packet is invalid"):
        cd.data_only_evidence_context(value, readiness=readiness)


@pytest.mark.parametrize(
    "status,data_only,expected",
    [
        ("unsupported_assessment_tool_boundary", True, "transport"),
        ("exhausted", True, "capacity"),
        ("unsupported_assessment_tool_boundary", False, "capacity"),
    ],
)
def test_pool_fallback_reports_boundary_distinct_from_capacity(
    tmp_path, monkeypatch, status, data_only, expected
):
    monkeypatch.setenv("HANDOFF_DIR", str(tmp_path))
    profile = execution_profiles.get_profile("claude-sonnet-5-5-low")
    route = {
        "agent": "claude",
        "profile_id": profile["profile_id"],
        "pools": profile["capacity_pool_ids"],
    }
    picks, calls = [], []

    @contextmanager
    def busy(*args, **kwargs):
        raise cr.ResourceBlocked("fixture original profile pool held")
        yield  # pragma: no cover

    def pick(**kwargs):
        picks.append(kwargs)
        return None, status

    monkeypatch.setattr(cr, "pool_slot", busy)
    monkeypatch.setattr(cd, "_pipeline_low_route", pick)
    monkeypatch.setattr(dispatcher, "offload", lambda *a, **k: calls.append("forbidden provider"))

    def invoke(_):
        calls.append("forbidden invoke")
        raise AssertionError("provider must not start")

    exc = cd.AssessmentTransportUnavailable if expected == "transport" else cr.ResourceBlocked
    with pytest.raises(exc):
        cd._execute_profile_route(
            route, invoke, lane="low", load={}, deadline=1e20, data_only=data_only
        )
    assert calls == []
    assert len(picks) == 1
    assert picks[0].get("data_only", False) is data_only


def test_initial_route_unsupported_is_not_capacity(tmp_path, monkeypatch):
    monkeypatch.setenv("HANDOFF_DIR", str(tmp_path))
    profile = execution_profiles.get_profile("claude-sonnet-5-5-low")
    report = {
        "agents": {"claude": {"state": "ok"}},
        "profiles": {profile["profile_id"]: {"state": "ok"}},
        "pools": {pool: {"state": "ok"} for pool in profile["capacity_pool_ids"]},
    }
    monkeypatch.setattr(cd, "_fresh_capacity_report", lambda: report)
    monkeypatch.setattr(
        execution_profiles,
        "profiles_for_agent",
        lambda agent, **kwargs: [profile] if agent == "claude" else [],
    )
    monkeypatch.setattr(adapters, "data_only_problem", lambda *a: "fixture unsupported binary")
    monkeypatch.setattr(dispatcher, "offload", lambda *a, **k: pytest.fail("provider invoked"))
    route, status = cd._pipeline_low_route(with_status=True, data_only=True)
    assert route is None and status == "unsupported_assessment_tool_boundary"


def test_transport_pending_preserves_target_revision_without_capacity_claim():
    value = cd.assessment_transport_pending("acme/repo#12", "exact-revision")
    assert value["status"] == "pending"
    assert value["classification"] == "assessment_transport_unavailable"
    assert value["pending_evidence"]["target"] == "acme/repo#12"
    assert value["pending_evidence"]["revision"] == "exact-revision"
    assert value["authority"] is False
    assert "capacity" not in value.get("resource_evidence", {})


@pytest.fixture
def request_only_packet(tmp_path):
    snapshot = {
        "target": "acme/repo#12",
        "kind": "pr",
        "state": "OPEN",
        "head": "a" * 40,
        "base": "b" * 40,
        "sources": [],
        "threads": [],
        "review_revision": "r1",
        "checks": None,
        "title": "Exact local request",
        "body": "<!-- workflow-source:local_request -->\nPreserve every other trigger.\n",
    }

    def api(endpoint, **kwargs):
        if "/compare/" in endpoint:
            return {"status": "ahead", "ahead_by": 0, "files": []}
        if "/actions/runs?" in endpoint:
            return {"workflow_runs": []}
        raise AssertionError(endpoint)

    packet = ce.collect_evidence(snapshot, api, tmp_path, action="collect_evidence")
    readiness = ce.action_evidence_readiness(
        snapshot, packet, "collect_evidence", retained_dir=tmp_path
    )
    assert ce.validate_packet(packet) and not readiness["ready"]
    return packet, readiness, snapshot


def test_current_request_is_available_with_no_issue_sources(request_only_packet):
    packet, readiness, snapshot = request_only_packet
    context = cd.data_only_evidence_context(packet, readiness=readiness, snapshot=snapshot)
    request = context["current_request"]
    assert request["content_supplied"] is True
    assert request["content"] == {k: snapshot[k] for k in ("title", "body")}
    assert request["text_is_untrusted"] is True
    assert request["acceptance_authority"] is request["action_authority"] is False
    assert context["sections"]["source"]["status"] == "missing"
    assert context["readiness"] == readiness


@pytest.mark.parametrize("field,value", [("target", "acme/foreign#12"), ("head", "f" * 40)])
def test_current_request_cross_target_or_head_rejected(request_only_packet, field, value):
    packet, readiness, snapshot = request_only_packet
    snapshot[field] = value
    with pytest.raises(ValueError, match="identity differs"):
        cd.data_only_evidence_context(packet, readiness=readiness, snapshot=snapshot)


def test_current_request_credentials_redacted(request_only_packet, monkeypatch):
    import credential_redaction

    monkeypatch.delenv(credential_redaction.REDACTION_DISABLED_ENV, raising=False)
    monkeypatch.setattr(credential_redaction, "_known_values", lambda: [])
    packet, readiness, snapshot = request_only_packet
    secret = "github" + "_pat_" + "11ABCDEFG0" + "x" * 50
    snapshot["body"] += secret
    snapshot["title"] = secret
    request = cd.data_only_evidence_context(packet, readiness=readiness, snapshot=snapshot)[
        "current_request"
    ]
    assert request["redaction_applied"] is True
    assert secret not in request["content"]["body"]
    assert secret not in request["content"]["title"]


def test_current_request_oversize_is_explicitly_omitted_not_truncated(request_only_packet):
    packet, readiness, snapshot = request_only_packet
    snapshot["body"] = "original criterion\n" * 20000
    request = cd.data_only_evidence_context(packet, readiness=readiness, snapshot=snapshot)[
        "current_request"
    ]
    assert request["content_supplied"] is False
    assert "content" not in request
    assert request["content_bytes"] > 256 * 1024
    assert "not inspected" in request["context_gap"]


def test_request_and_section_content_share_one_budget(request_only_packet):
    import json

    packet, readiness, snapshot = request_only_packet
    used = sum(
        len(json.dumps(v["content"], ensure_ascii=False, sort_keys=True).encode())
        for v in packet["sections"].values()
    )
    request = cd.data_only_evidence_context(
        packet, readiness=readiness, snapshot=snapshot, max_bytes=used
    )["current_request"]
    assert request["content_supplied"] is False and "content" not in request


def test_real_native_prompt_supplies_full_nonissue_request_beyond_optional_sample(
    native_world, capsys
):
    import json

    w = native_world
    w.snapshot["sources"] = []
    w.snapshot["title"] = "Original local request"
    w.snapshot["body"] = (
        "<!-- workflow-source:local_request -->\n"
        + ("original task material;" * 600)
        + "EXACT_REQUEST_TAIL"
    )
    cd.main(w.argv)
    capsys.readouterr()
    assert w.events["provider"] == 1
    assert w.events["effect"] == 0
    prompt = w.events["prompts"][0]
    mandatory = json.JSONDecoder().raw_decode(prompt[prompt.index('{"mandatory":') :])[0][
        "mandatory"
    ]
    request = mandatory["data_only_evidence"]["current_request"]
    assert request["content_supplied"] is True
    assert request["content"]["body"] == w.snapshot["body"]
    assert request["content"]["title"] == w.snapshot["title"]
    assert "EXACT_REQUEST_TAIL" in prompt
    assert "explicit snapshot below" not in prompt
    assert all(path.read_bytes() == original for path, original in w.originals.items())
