"""Non-finite interpretation uses the same confined route and refusal contract."""

import time

from closer_role_support import install_synthetic_role_verdict
from test_closer_batch_interpretation import OfflineDelivery, callbacks_for
from test_closer_disposition import TARGET
from test_closer_evidence_recovery import recovery_callbacks

import adapters
import closer_dispatcher as closer
import closer_evidence_recovery as recovery
import dispatcher
import execution_profiles


def test_nonfinite_tool_boundary_refusal_is_owned_pending_not_invalid_run(tmp_path, monkeypatch):
    install_synthetic_role_verdict(monkeypatch)
    delivery = OfflineDelivery(tmp_path, monkeypatch)
    profile = execution_profiles.get_profile("claude-sonnet-5-5-low")
    monkeypatch.setattr(adapters, "data_only_problem", lambda *_: None)
    report = {
        "generated_at": time.time(),
        "agents": {"claude": {"state": "ok"}},
        "profiles": {profile["profile_id"]: {"state": "ok"}},
        "pools": {pool: {"state": "ok"} for pool in profile["capacity_pool_ids"]},
    }
    monkeypatch.setattr(closer, "_fresh_capacity_report", lambda: report)
    routed, invoked = [], []

    def route(**kwargs):
        routed.append(kwargs)
        return {
            "agent": "claude",
            "profile_id": profile["profile_id"],
            "mode": "assess",
            "pools": profile["capacity_pool_ids"],
        }, "available"

    def refused(_agent, prompt, **kwargs):
        invoked.append((prompt, kwargs))
        return {
            "exit": 2,
            "run_id": None,
            "unsupported_assessment_tool_boundary": True,
            "process_started": False,
            "outcome_uncertain": False,
        }

    monkeypatch.setattr(closer, "_pipeline_low_route", route)
    monkeypatch.setattr(dispatcher, "offload", refused)
    callbacks, context = callbacks_for(delivery, monkeypatch)
    result = callbacks["interpret"](TARGET, context)
    assert routed and all(call["data_only"] is True for call in routed)
    assert len(invoked) == 1 and invoked[0][1]["data_only"] is True
    assert "mandatory" in invoked[0][0] and "data_only_evidence" in invoked[0][0]
    assert result["classification"] == "assessment_transport_unavailable"
    assert result["pending_evidence"]["transport_refusal"]["run_id"] is None
    assert result["authority"] is False


def test_known_not_started_transport_refusal_releases_only_its_collection_attempt(
    tmp_path, monkeypatch
):
    callbacks, context, calls, refreshes = recovery_callbacks(tmp_path, monkeypatch, recover=True)
    target = context["prepare"]["snapshot"]["target"]
    original_offload = dispatcher.offload
    refusals = []

    def refused(*args, **kwargs):
        refusals.append(kwargs)
        return {
            "exit": 2,
            "run_id": None,
            "unsupported_assessment_tool_boundary": True,
            "process_started": False,
            "outcome_uncertain": False,
        }

    monkeypatch.setattr(dispatcher, "offload", refused)
    first = callbacks["interpret"](target, context)
    assert first["classification"] == "assessment_transport_unavailable"
    assert len(refusals) == 1 and refusals[0]["data_only"] is True
    assert not calls and not refreshes
    snapshot = context["prepare"]["snapshot"]
    store = recovery.RecoveryStore(tmp_path / "state.json.evidence")
    with store.locked(recovery.digest(recovery.binding(snapshot))) as journal:
        assert len(journal["attempts"]) == 1
        assert next(iter(journal["attempts"].values()))["state"] == "not_started"

    # The same unchanged packet can now use the restored confined transport;
    # no new observation or artificial retry token is needed for a proven
    # no-spawn refusal. The real recovery journal and rescreen remain active.
    monkeypatch.setattr(dispatcher, "offload", original_offload)
    second = callbacks["interpret"](target, context)
    assert second["evidence_recovery"]["outcome"] == "recovered"
    assert len(calls) == 2 and refreshes == [{"diff_files"}]
    assert second["authority"] is False
