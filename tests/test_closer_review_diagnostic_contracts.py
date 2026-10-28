"""Optional diagnostic failures must preserve refusal and never expose exception text."""

import builtins
import json

import pytest
from test_closer_role_selection import pick
from test_closer_sonnet_role_admission import run, world  # noqa: F401
from test_dispatcher_failure_contracts import fake_offload, isolated  # noqa: F401

import closer_claude_role_admission as admission
import closer_dispatcher as closer
import closer_queue as queue
import closer_role_selection as selection
import dispatcher

SECRET = "SYNTHETIC_CREDENTIAL_NEVER_DIAGNOSTIC"


@pytest.mark.parametrize("failure", ["import", "lookup"])
def test_optional_queue_diagnostic_cannot_replace_process_start_failure(
    fake_offload, monkeypatch, failure  # noqa: F811 - shared inert fixture
):
    def refused(*_a, **_kw):
        raise OSError("fixture executable missing")

    def unavailable(*_a, **_kw):
        raise RuntimeError(SECRET)

    monkeypatch.setattr(dispatcher.subprocess, "run", refused)
    if failure == "lookup":
        monkeypatch.setattr(queue, "_native_read_failure", unavailable)
    else:
        original_import = builtins.__import__

        def import_without_queue(name, *args, **kwargs):
            if name == "closer_queue":
                raise ImportError(SECRET)
            return original_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", import_without_queue)
    result = dispatcher.offload("cursor", "inspect", timeout=1, max_network_retries=0)
    assert result["exit"] == 126 and result["attempts"] == 1
    assert "fixture executable missing" in result["error"]
    assert SECRET not in json.dumps(result)


def test_policy_load_failure_logs_only_fixed_context_and_type(monkeypatch, caplog):
    def unavailable():
        raise ValueError(SECRET)

    monkeypatch.setattr(selection, "load_policy", unavailable)
    assert pick(only={"codex"}) is None
    assert "closer selection policy unavailable (ValueError)" in caplog.text
    assert SECRET not in caplog.text


def test_role_admission_failure_reports_fixed_phase_without_exception_text(
    world, monkeypatch  # noqa: F811 - shared inert fixture
):
    def held(*_a, **_kw):
        raise closer.ControlledOriginUnavailable(SECRET)

    monkeypatch.setattr(closer, "_controlled_launch_inputs", held)
    value = run(world)
    report = admission._read(value["report_ref"])
    assert not value["capability_pass"] and not world.calls
    assert report["failure_type"] == "ControlledOriginUnavailable"
    assert report["failure_phase"] == "guarded_launch_setup"
    assert SECRET not in json.dumps(report)
