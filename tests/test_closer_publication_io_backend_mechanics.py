"""Real queue plus detached fixed body, with external Git/API effect ports mocked.

This is mechanical coverage, not a genuine tick/SourceInstallation/issuer positive.
No source validator or source capability factory is replaced with success.
"""

import copy
import threading
import time

import pytest
from test_closer_coding_publication import custody as custody
from test_closer_publication_mutation_body import detached_body, inputs, ports
from test_closer_queue import _run, _snapshot

import closer_coding_publication as pub
import closer_coding_publication_io as io
import closer_correction_controller as ctl
import closer_queue as queue


def test_real_queue_reserves_before_simulated_push_and_keeps_callback_tls(tmp_path, custody):
    prepared, raw, oid = inputs()
    git, api, phases = ports(prepared, raw, oid)
    path = tmp_path / "new-queue.json"
    state = [None]
    owner_thread = threading.get_ident()
    callback_threads = []

    def observed_git(controller, actual_custody, operation, selection, host, phase, argv, payload):
        assert queue._currently_invoking("acme/repo#9", operation)
        physical = ctl.IndexController._physical_state(path, "test queue")
        assert physical["items"]["acme/repo#9"]["operation"]["status"] == "submitted"
        callback_threads.append(threading.get_ident())
        return git(controller, actual_custody, operation, selection, host, phase, argv, payload)

    body = detached_body(observed_git, api)

    def observe(_):
        return _snapshot("known-H", io.ACTION, eligible=state[0] is None)

    def execute(target, action, snapshot, operation):
        assert action == io.ACTION
        state[0] = body(None, custody, operation, {"intent": prepared["intent"]}, None, prepared)
        return {"status": "completed", "advanced": True, "contract": state[0]}

    result = _run(path, ["acme/repo#9"], observe, execute, max_operation_retries=0)
    assert phases.count("push") == 1 and phases[-1] == "native_readback"
    assert state[0]["contract_consistent"] is True
    assert all(state[0][key] is False for key in pub._AUTHORITY)
    assert result["valid"] and result["eligible_remaining"] == []
    assert callback_threads and all(thread != owner_thread for thread in callback_threads)
    # The ordinary executor above is deliberate mechanics, never the private
    # publication source selector or a claimed same-thread admission positive.


@pytest.mark.parametrize("failure", ["push", "native_readback"])
def test_unknown_mechanical_publication_never_repeats_on_next_queue_round(
    tmp_path, custody, failure
):
    prepared, raw, oid = inputs()
    git, api, phases = ports(prepared, raw, oid, fail=failure)
    body = detached_body(git, api)
    path = tmp_path / "new-unknown-queue.json"

    def observe(_):
        return _snapshot("known-H", io.ACTION, eligible=True)

    def execute(target, action, snapshot, operation):
        return body(None, custody, operation, {"intent": prepared["intent"]}, None, prepared)

    first = _run(path, ["acme/repo#9"], observe, execute, max_operation_retries=0)
    before = list(phases)
    second = _run(path, ["acme/repo#9"], observe, execute, max_operation_retries=0)
    assert phases == before
    assert phases.count("push") == 1
    assert not first["stop_valid"] and not second["stop_valid"]
    assert pub.ContractLedger(custody).read(prepared["intent"]["slot_key"])["state"] == "unknown"


def test_genuine_source_front_door_refuses_unissued_context_before_external_ports(
    custody, monkeypatch
):
    import closer_dispatcher
    import dispatcher

    calls = []

    def forbidden(*args, **kwargs):
        calls.append(True)
        raise AssertionError("external effect was reached")

    monkeypatch.setattr(dispatcher, "run_observed_offload", forbidden)
    monkeypatch.setattr(closer_dispatcher.GitHub, "api", forbidden)
    controller = object.__new__(ctl.IndexController)
    before = sorted(__import__("os").listdir(custody.fd))
    with pytest.raises(pub.PublicationHeld, match="actual publication IO scope absent"):
        pub.execute_existing_update(controller, custody, {"deadline": time.time() + 10}, {})
    assert calls == []
    assert sorted(__import__("os").listdir(custody.fd)) == before


def test_exact_enrolled_gh_protocol_is_necessary_data_only():
    supported = pub._SUPPORTED_GH_PROTOCOL
    value = pub._gh_protocol_data(supported["path"], supported["size"], supported["sha256"])
    assert value == supported and value is not supported
    value["upstream"].append("not-issued")
    assert "not-issued" not in supported["upstream"]
    for field in ("path", "size", "sha256"):
        bad = copy.deepcopy(supported)
        bad[field] = bad[field] + 1 if field == "size" else bad[field] + "x"
        with pytest.raises(pub.PublicationHeld):
            pub._gh_protocol_data(bad["path"], bad["size"], bad["sha256"])


def test_caller_protocol_mapping_cannot_issue_installed_gh_semantics():
    with pytest.raises(pub.PublicationHeld, match="actual fixed helper/API protocol producer"):
        pub._require_fixed_gh_protocol({}, {})


def test_absent_publication_mode_keeps_tick_unselected_without_effects(tmp_path, monkeypatch):
    import closer_runtime_binding as runtime

    def forbidden(*args, **kwargs):
        raise AssertionError("factory should not be called in absent mode")

    monkeypatch.setattr(runtime, "binding_from_manifest", forbidden)
    assert io.run_if_selected(None, {}, {"policy": {}}, tmp_path / "unused.json", {}) is None
    assert not list(tmp_path.iterdir())
