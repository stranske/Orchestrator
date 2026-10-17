"""Advisory degradation must be explicit, bounded, and independent of live state."""

import builtins
import socket
import subprocess

import pytest

import capability_advisor as advisor


@pytest.fixture(autouse=True)
def isolated_runtime(tmp_path, monkeypatch):
    for name in ("ORCH_STATE_DIR", "ORCH_LOCAL_RUNTIME"):
        monkeypatch.setenv(name, str(tmp_path))

    def forbidden(*args, **kwargs):
        pytest.fail("advisor boundary test attempted a process or network call")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


@pytest.mark.parametrize("operation", ["dispatcher", "facts", "declines"])
def test_unavailable_optional_import_degrades_without_fabricating_events(monkeypatch, operation):
    real_import = builtins.__import__
    blocked = "dispatcher" if operation == "dispatcher" else "capability_propensity"

    def importing(name, *args, **kwargs):
        if name == blocked:
            raise ImportError("optional subsystem unavailable")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", importing)
    if operation == "dispatcher":
        assert advisor._dispatcher_task_type_capability() == {}
    elif operation == "facts":
        assert advisor._record_fact_missing({}, [{"capability_id": "one"}]) == 0
    else:
        assert (
            advisor._record_auto_declines(
                {"capabilities": [{"capability_id": "one", "auto_declined": {"reason": "missing"}}]}
            )
            == 0
        )


def test_promoted_index_requires_promotion_source_and_retains_reason():
    ledger = {
        "one": {
            "event_history": [
                {
                    "metadata": {
                        "source": "binding_promotion",
                        "surface": "test",
                        "reason": "observed useful",
                    }
                },
                {"metadata": {"source": "unrelated", "surface": "wrong", "reason": "ignore"}},
            ]
        },
        "two": {
            "event_history": [{"metadata": {"source": "binding_promotion", "surface": "test"}}]
        },
    }
    assert advisor._promoted_index(ledger=ledger) == {
        "test": {"one": "observed useful", "two": "promoted from observed use"}
    }


def test_expired_consult_is_an_explicit_timeout():
    with pytest.raises(TimeoutError, match="capability phase consult exceeded"):
        advisor._consult_expired(None, None)


def test_phase_report_preserves_offered_bound_and_recorded_counts():
    report = {
        "phases": 1,
        "offered": 3,
        "recorded": 2,
        "day": "test",
        "rows": [{"surface": "test", "offered": 3, "bound": ["one"], "recorded": 2}],
    }
    text = advisor.format_phase_consult(report)
    assert "1 phase(s), 3 capability offer(s), 2 new match event(s)" in text
    assert "test: offered 3 (bound 1), recorded 2" in text


def test_surface_scan_stops_at_marker_bound(tmp_path, monkeypatch):
    (tmp_path / "one.html").write_text("one")
    (tmp_path / "two.html").write_text("two")
    monkeypatch.setattr(advisor, "_SURFACE_MAX_MARKERS", 1)
    result = advisor.detect_observable_surface(tmp_path)
    assert result["observable"] is True
    assert len(result["markers"]) == 1
    assert result["detail"] == "1 surface marker(s) found"


def test_unregistered_fact_probe_remains_unknown(monkeypatch):
    monkeypatch.setattr(advisor, "applies_to", lambda name: None)
    monkeypatch.setattr(advisor, "required_repo_fact", lambda name: "unregistered")
    monkeypatch.setattr(advisor, "required_pr_fact", lambda name: None)
    monkeypatch.setattr(advisor, "REPO_FACT_PROBES", {})
    result = advisor.evaluate_precondition("one", repository="owner/repo")
    assert result["requirement_met"] is None
    assert result["precondition_met"] is None
    assert result["unevaluated_because"] == ["no probe is registered for 'unregistered'"]


def test_alarm_cleanup_failure_preserves_completed_consult(monkeypatch):
    import signal

    calls = []

    def alarm(seconds):
        calls.append(seconds)
        if seconds == 0:
            raise OSError("cleanup unavailable")

    monkeypatch.setattr(signal, "signal", lambda *args: None)
    monkeypatch.setattr(signal, "alarm", alarm)
    monkeypatch.setattr(advisor, "consult_phases", lambda **kwargs: {"offered": 2})
    assert advisor.consult_phases_guarded(day="test") == {"offered": 2}
    assert calls == [advisor.CONSULT_BUDGET_S + 5, 0]


def test_missing_cli_task_is_rejected_before_advice(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(advisor, "advise", lambda *args, **kwargs: calls.append(args))
    with pytest.raises(SystemExit) as exc:
        advisor.main([])
    assert exc.value.code == 2
    assert calls == []
    assert "give the task in plain words" in capsys.readouterr().err
