"""Fixed source literals remain diagnostics; UNKNOWN and retry fences unchanged."""

import json

import pytest
from test_closer_queue import _run, _snapshot

import closer_correction_controller as ctl
import closer_queue as queue


def inactive_guard():
    guard = object.__new__(ctl._ProviderGuard)
    guard.reader, guard.active, guard.used = None, False, False
    guard.starting({})


def capture(callback):
    try:
        callback()
    except Exception as error:
        return queue._fixed_controller_refusal(error)
    raise AssertionError("expected actual refusal")


@pytest.mark.parametrize(
    "callback,stage,reason",
    [
        (
            inactive_guard,
            "_ProviderGuard.starting",
            "original before-provider claim inactive or consumed",
        ),
        (
            lambda: ctl.NativeConsumerReader().read_native_identity(),
            "NativeConsumerReader.read_native_identity",
            "installed managed native account reader unsupported",
        ),
    ],
)
def test_actual_registered_guard_literal_retained(callback, stage, reason):
    value = capture(callback)
    assert value["stage"] == stage and value["fixed_reason"] == reason
    assert value["module"] == "closer_correction_controller" and value["line"] > 0
    assert value["authority"] is False and value["retry_authorized"] is False


@pytest.mark.parametrize(
    "payload",
    [
        "credential-secret-never-export",
        "original before-provider claim inactive or consumed",
        "x" * 161,
        "line\nbreak",
        {"secret": "never-stringify"},
    ],
)
def test_unregistered_raising_frames_and_arbitrary_payloads_omitted(payload):
    def external():
        raise ctl.ControllerUnknown(payload)

    assert capture(external) is None
    assert capture(lambda: ctl.need(False, payload)) is None


def test_subclass_and_unraised_exception_are_not_accepted():
    class Other(ctl.ControllerUnknown):
        pass

    assert capture(lambda: (_ for _ in ()).throw(Other("secret"))) is None
    assert queue._fixed_controller_refusal(ctl.ControllerUnknown("secret")) is None


def test_bounded_traceback_does_not_walk_unlimited_frames():
    def recurse(n):
        if n:
            return recurse(n - 1)
        inactive_guard()

    assert capture(lambda: recurse(70)) is None


def test_diagnostic_failure_preserves_original_uncertain_outcome(tmp_path, monkeypatch):
    path = tmp_path / "queue.json"
    calls = []

    def execute(*_):
        calls.append(True)
        inactive_guard()

    def broken(*_):
        raise RuntimeError("diagnostic broken")

    # Registered source identity lookup failure is caught inside the helper.
    monkeypatch.setattr(queue.inspect, "isfunction", broken)
    first = _run(
        path, ["org/repo#1"], lambda _: _snapshot("same", "merge"), execute, owner="round-1"
    )
    assert not first["valid"] and len(calls) == 1
    operation = json.loads(path.read_text())["items"]["org/repo#1"]["operation"]
    assert operation["status"] == "uncertain" and operation["attempts"] == 1
    assert operation["error_type"] == "ControllerUnknown"


def test_actual_execute_catch_keeps_literal_and_unknown_no_retry(tmp_path):
    path = tmp_path / "queue.json"
    calls = []
    submitted = []

    def execute(_target, _action, _snapshot, operation):
        calls.append(True)
        submitted.append(dict(operation))
        inactive_guard()

    first = _run(
        path, ["org/repo#1"], lambda _: _snapshot("same", "merge"), execute, owner="round-1"
    )
    state = json.loads(path.read_text())
    operation = state["items"]["org/repo#1"]["operation"]
    assert operation["status"] == "uncertain" and operation["attempts"] == 1
    assert operation["error_type"] == "ControllerUnknown"
    assert all(operation[key] == value for key, value in submitted[0].items() if key != "status")
    event = next(e for e in state["round"]["events"] if e["kind"] == "execute_uncertain")
    assert (
        event["fixed_refusal"]["fixed_reason"]
        == "original before-provider claim inactive or consumed"
    )
    assert (
        not event["fixed_refusal"]["authority"] and not event["fixed_refusal"]["retry_authorized"]
    )
    second = _run(
        path,
        ["org/repo#1"],
        lambda _: _snapshot("same", "merge"),
        execute,
        reconcile=lambda *_: {"status": "unknown"},
        owner="round-1",
    )
    assert len(calls) == 1 and second["accounting"][0]["reason"] == "operation_uncertain"
    assert not first["valid"]


def test_unknown_dynamic_message_never_enters_durable_event(tmp_path):
    path = tmp_path / "queue.json"
    secret = "credential-secret-never-export"

    def execute(*_):
        raise ctl.ControllerUnknown(secret)

    _run(path, ["org/repo#1"], lambda _: _snapshot("same", "merge"), execute, owner="round-1")
    raw = path.read_text()
    assert secret not in raw
    event = next(e for e in json.loads(raw)["round"]["events"] if e["kind"] == "execute_uncertain")
    assert "fixed_refusal" not in event


def test_missing_controller_registry_omits_diagnostic(monkeypatch):
    import sys
    from types import SimpleNamespace

    with pytest.raises(ctl.ControllerUnknown) as caught:
        inactive_guard()
    monkeypatch.setitem(sys.modules, "closer_correction_controller", SimpleNamespace())
    assert queue._fixed_controller_refusal(caught.value) is None


@pytest.mark.parametrize(
    "payload",
    ["credential-secret-never-export", "x" * 200, "line\nbreak", {"secret": "never stringify"}],
)
def test_dynamic_registered_guard_retains_stage_not_message(payload):
    value = capture(lambda: ctl.IndexController._physical_state("relative", payload))
    assert value["stage"] == "IndexController._physical_state"
    assert value["line"] > 0 and value["module"] == "closer_correction_controller"
    assert value["fixed_reason"] == "UNCLASSIFIED_SOURCE_REFUSAL"
    assert value["reason_kind"] == "stage_only"
    assert not value["authority"] and not value["retry_authorized"]
    assert "secret" not in json.dumps(value) and "line\nbreak" not in json.dumps(value)


def test_dynamic_stage_survives_actual_queue_catch_without_secret(tmp_path):
    path = tmp_path / "queue.json"

    def execute(*_):
        ctl.IndexController._physical_state("relative", "credential-secret-never-export")

    _run(path, ["org/repo#1"], lambda _: _snapshot("same", "merge"), execute, owner="round-1")
    raw = path.read_text()
    assert "credential-secret-never-export" not in raw
    state = json.loads(raw)
    event = next(e for e in state["round"]["events"] if e["kind"] == "execute_uncertain")
    assert event["fixed_refusal"]["stage"] == "IndexController._physical_state"
    assert event["fixed_refusal"]["reason_kind"] == "stage_only"
    assert state["items"]["org/repo#1"]["operation"]["status"] == "uncertain"
