"""Finite controlled refusal/retry and genuine nonfinite no-lease refusal."""

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


def test_finite_cli_prelaunch_refusal_is_owned_pending_not_invalid_run(tmp_path, monkeypatch):
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


def test_finite_cli_known_no_start_releases_only_its_collection_attempt(tmp_path, monkeypatch):
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


def test_actual_nonfinite_callback_without_original_lease_is_owned_pending_and_never_invokes_broker_or_act(
    tmp_path, monkeypatch
):
    import inspect
    import time
    from types import SimpleNamespace

    import pytest
    import test_closer_dispatcher as runtime

    import closer_pipeline

    _finite_callbacks, context, calls, refreshes = recovery_callbacks(
        tmp_path, monkeypatch, recover=True, initial_missing=False
    )
    snapshot = context["prepare"]["snapshot"]
    target = snapshot["target"]
    # Reuse only the bounded private budget reader, not staged()'s finite
    # frontier/CLI claim/lease. The actual unsupported lease gate must decide.
    _finite_callbacks["_inert_caller"].temporary_campaign_guard()
    backend = runtime.closer(tmp_path, snapshot, apply=True, workers=True)
    owned = {}
    real_pipeline = closer_pipeline.run_pipeline

    def capture(_path, _targets, **callbacks):
        owned.update(callbacks)
        return {"targets": {}}

    def forbidden(*_args, **_kwargs):
        pytest.fail("nonfinite callback without original lease cannot invoke broker or ACT")

    monkeypatch.setattr(dispatcher, "offload", forbidden)
    monkeypatch.setattr(backend, "execute", forbidden)
    monkeypatch.setattr(closer_pipeline, "run_pipeline", capture)
    monkeypatch.setattr(closer, "GitHub", lambda **_kw: backend.gh)
    args = SimpleNamespace(
        state=str(tmp_path / "nonfinite.json"),
        finite_frontier=None,
        shadow=False,
        max_workers=1,
        evidence_recovery=True,
    )
    closer._run_staged_pipeline(args, [target], backend, time.time() + 60)
    assert inspect.getclosurevars(owned["interpret"]).nonlocals["finite_frontier"] is None
    monkeypatch.setattr(closer_pipeline, "run_pipeline", real_pipeline)
    import closer_resources

    assert not closer_resources.finite_pool_has_active_reservations()
    result = owned["interpret"](target, context)
    assert result["status"] == "pending" and result["authority"] is False
    assert result["classification"] == "assessment_transport_unavailable"
    assert result["pending_evidence"]["target"] == target
    assert result["pending_evidence"]["revision"] == snapshot["revision"]
    refusal = result["pending_evidence"]["transport_refusal"]
    assert (
        refusal["error"] == "reviewed controlled execution contract or original lease unavailable"
    )
    assert refusal["process_started"] is False and refusal["run_id"] is None
    assert refusal["outcome_uncertain"] is False
    assert not calls and not refreshes
    assert not _finite_callbacks["_inert_caller"].results
    assert not _finite_callbacks["_inert_caller"].world.events["ledger"]
