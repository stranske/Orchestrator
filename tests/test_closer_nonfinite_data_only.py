"""Legacy assessment refusal regressions through the owned controlled CLI boundary."""

from closer_inert_dispatcher_support import REAL_BROKER
from test_closer_evidence_recovery import recovery_callbacks

import closer_dispatcher as closer
import closer_evidence_recovery as recovery
import dispatcher


def actual_prelaunch_refusal(monkeypatch, agent, prompt, **kwargs):
    """Actual broker refuses unavailable provenance before any process/ledger start."""

    def unavailable(_module):
        raise OSError("synthetic prelaunch provenance read unavailable")

    with monkeypatch.context() as local:
        local.setattr(dispatcher, "_controlled_module_sha", unavailable)
        return REAL_BROKER(agent, prompt, **kwargs)


def test_nonfinite_tool_boundary_refusal_is_owned_pending_not_invalid_run(tmp_path, monkeypatch):
    callbacks, context, calls, refreshes = recovery_callbacks(
        tmp_path, monkeypatch, recover=True, initial_missing=False
    )
    target = context["prepare"]["snapshot"]["target"]
    routed, invoked = [], []
    original_route = closer._pipeline_low_route

    def route(**kwargs):
        routed.append(kwargs)
        return original_route(**kwargs)

    def refused(agent, prompt, **kwargs):
        invoked.append((prompt, kwargs))
        return actual_prelaunch_refusal(monkeypatch, agent, prompt, **kwargs)

    monkeypatch.setattr(closer, "_pipeline_low_route", route)
    monkeypatch.setattr(dispatcher, "offload", refused)
    result = callbacks["interpret"](target, context)
    assert routed and all(call["data_only"] is True for call in routed)
    assert len(invoked) == 1 and invoked[0][1]["data_only"] is True
    assert "mandatory" in invoked[0][0] and "data_only_evidence" in invoked[0][0]
    assert result["classification"] == "assessment_transport_unavailable"
    assert result["pending_evidence"]["transport_refusal"]["run_id"] is None
    assert result["authority"] is False
    assert not calls and not refreshes
    assert not callbacks["_inert_caller"].world.events["ledger"]


def test_known_not_started_transport_refusal_releases_only_its_collection_attempt(
    tmp_path, monkeypatch
):
    callbacks, context, calls, refreshes = recovery_callbacks(tmp_path, monkeypatch, recover=True)
    target = context["prepare"]["snapshot"]["target"]
    original_offload = dispatcher.offload
    refusals = []

    def refused(agent, prompt, **kwargs):
        refusals.append(kwargs)
        return actual_prelaunch_refusal(monkeypatch, agent, prompt, **kwargs)

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
