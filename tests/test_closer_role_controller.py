"""Actual native round admission remains independent of model-role availability."""

from test_closer_native_capacity import capacity_host as capacity_host
from test_closer_native_observer import host as host

import closer_campaign as campaign
import closer_mechanisms as mechanisms
import closer_native_observer as native


def tick_without_model(fixture, launches):
    _manifest, path, obj, *_ = fixture
    return campaign.tick(
        obj.path.parent,
        path,
        mechanism=mechanisms.NATIVE,
        qualifier=lambda *_: [],
        route_fn=lambda *_: {"hold": "no qualified model roles"},
        native_probe=lambda *_: None,
        launcher=lambda argv, _log: launches.append(argv) or 60001,
        pid_alive=lambda *_: True,
        max_steps=1,
    )


def test_actual_tick_launches_native_round_without_a_model_route(capacity_host):
    launches = []
    result = tick_without_model(capacity_host, launches)
    assert result[0]["kind"] == "launched", result
    assert len(launches) == 1
    assert "--agent" not in launches[0] and "--profile-id" not in launches[0]
    assert "worker" in launches[0]


def test_stale_signed_native_admission_cannot_launch_zero_model_round(capacity_host, monkeypatch):
    manifest, path, obj, doc, _source, clock, _invocation = capacity_host
    admission = native.publish_admission(manifest, path, obj.path, doc)
    clock[0] += 601
    monkeypatch.setattr(native, "publish_admission", lambda *_: admission)
    launches = []
    result = tick_without_model(capacity_host, launches)
    assert result[0]["kind"] == "held", result
    assert "expired" in result[0]["reason"]
    assert launches == []


def test_absent_native_admission_does_not_grant_zero_model_launch(capacity_host, monkeypatch):
    monkeypatch.setattr(native, "publish_admission", lambda *_: None)
    launches = []
    result = tick_without_model(capacity_host, launches)
    assert result[0]["kind"] == "wait", result
    assert result[0]["reason"] == "capacity_exhausted"
    assert launches == []
