"""Owner decision 2026-10-31: a 7-day triage trial, Gemini Flash-high vs Argon.

While the trial file names a window containing now, even UTC hours pin triage to gemini and
alternate Flash-high (mode "mid") with Argon (mode "full"); odd hours route normally.
"""

from __future__ import annotations

import json

import pytest

import dispatcher
import feedback
import roles

HOUR = 3600
START = 498_332 * HOUR  # hour number divisible by 4: Flash hour
FLASH = START + 60
ARGON = START + 2 * HOUR + 60
NORMAL = START + HOUR + 60


def _trial(tmp_path, start=START, end=START + 7 * 86400):
    path = tmp_path / "role-triage-flash.json"
    path.write_text(json.dumps({"start_ts": start, "end_ts": end}), encoding="utf-8")
    return path


@pytest.mark.parametrize(("now", "expected"), [(FLASH, "mid"), (ARGON, "full"), (NORMAL, None)])
def test_trial_hours_alternate_flash_and_argon_and_leave_odd_hours_alone(tmp_path, now, expected):
    assert roles.triage_trial_mode(path=_trial(tmp_path), now=now) == expected


@pytest.mark.parametrize("when", [START - 1, START + 7 * 86400])
def test_trial_is_off_outside_its_window(tmp_path, when):
    assert roles.triage_trial_mode(path=_trial(tmp_path), now=when) is None


@pytest.mark.parametrize(
    "content",
    [
        None,
        "",
        "not json",
        "{}",
        '{"start_ts": "x", "end_ts": 1}',
        '{"start_ts": 0, "end_ts": 1e309}',
        '{"start_ts": 0, "end_ts": ' + "9" * 400 + "}",
        '{"start_ts": 2e9, "end_ts": 1e9}',
    ],
)
def test_missing_or_malformed_trial_file_means_no_trial(tmp_path, content):
    path = tmp_path / "role-triage-flash.json"
    if content is not None:
        path.write_text(content, encoding="utf-8")
    assert roles.triage_trial_mode(path=path, now=FLASH) is None


def _run(monkeypatch, tmp_path, now, backend):
    monkeypatch.setattr(roles, "TRIAGE_FLASH_TRIAL_PATH", _trial(tmp_path))
    monkeypatch.setattr(roles.time, "time", lambda: float(now))
    calls, records = [], []

    def offload(agent, prompt, **kwargs):
        calls.append((agent, kwargs["mode"]))
        return {"run_id": "b1", "model": "m", "output": "nope", "exit": 0}

    monkeypatch.setattr(dispatcher, "offload", offload)
    monkeypatch.setattr(feedback, "record_role_run", lambda *a, **k: records.append(k))
    roles.run_triage_agent(
        backlog_items=[{"target": "o/r#1"}], backend=backend, dispatch=True, cap={}
    )
    return calls, records


@pytest.mark.parametrize(("now", "mode"), [(FLASH, "mid"), (ARGON, "full"), (NORMAL, "full")])
def test_gemini_triage_dispatches_and_records_the_trial_mode(monkeypatch, tmp_path, now, mode):
    calls, records = _run(monkeypatch, tmp_path, now, "gemini")
    assert calls == [("gemini", mode)]
    assert records and records[0]["reasoning_level"] == mode


def test_other_backends_keep_the_role_mode_in_trial_hours(monkeypatch, tmp_path):
    calls, _ = _run(monkeypatch, tmp_path, FLASH, "codex")
    assert calls == [("codex", "full")]


def test_trial_hour_pins_gemini_and_falls_back_without_capacity(monkeypatch, tmp_path):
    monkeypatch.setattr(roles, "TRIAGE_FLASH_TRIAL_PATH", _trial(tmp_path))
    monkeypatch.setattr(roles.time, "time", lambda: float(FLASH))
    monkeypatch.setattr(feedback, "DB_PATH", tmp_path / "feedback.db")  # selector events stay local
    monkeypatch.setenv(feedback.DB_PATH_ENV, str(tmp_path / "feedback.db"))
    picks = []

    def route(name, cap=None, *, only=None, **kw):
        picks.append(only)
        return (
            None
            if only == {"gemini"} and cap.get("no_gemini")
            else {"agent": "gemini" if only else "cursor"}
        )

    monkeypatch.setattr(roles, "route_role", route)
    monkeypatch.setattr(roles, "_shadow_gate", lambda env: True)
    seen = []

    def runner(**kw):
        seen.append(kw["backend"])
        return {"role_run_id": None, "proposal": None, "errors": ["x"]}

    roles.activate_tick_triage([{"target": "o/r#1"}], {}, runner=runner)
    roles.reset_role_invocation_counts()
    roles.activate_tick_triage([{"target": "o/r#1"}], {"no_gemini": True}, runner=runner)
    roles.reset_role_invocation_counts()
    assert seen == ["gemini", "cursor"]
    assert picks == [{"gemini"}, {"gemini"}, None]


def test_testgen_never_routes_to_gemini_even_when_draining_agy():
    import router

    assert "testgen" not in router.GEMINI_GOOD_FIT_TASKS
    testgen_agents = {entry["agent"] for entry in router.ROUTE_TABLE["testgen"]["agents"]}
    assert "gemini" not in testgen_agents

    cap_drain = {
        "agents": {
            "claude": {"state": "shed"},
            "codex": {"state": "ok"},
            "gemini": {"state": "ok", "policy": "drain"},
            "cursor": {"state": "ok"},
            "vibe": {"state": "ok"},
            "aider": {"state": "ok"},
        }
    }
    selected = router.select_agent("testgen", cap_drain)
    assert selected is not None
    assert selected["agent"] != "gemini"


def test_break_revert_testgen_in_good_fit_tasks():
    import router

    original = set(router.GEMINI_GOOD_FIT_TASKS)
    try:
        router.GEMINI_GOOD_FIT_TASKS.add("testgen")
        with pytest.raises(AssertionError):
            test_testgen_never_routes_to_gemini_even_when_draining_agy()
    finally:
        router.GEMINI_GOOD_FIT_TASKS.clear()
        router.GEMINI_GOOD_FIT_TASKS.update(original)


def test_break_revert_testgen_route_table_gemini_entry():
    """Deliberate break → revert for the ROUTE_TABLE gemini pin (#1039)."""
    import router

    original = list(router.ROUTE_TABLE["testgen"]["agents"])
    try:
        router.ROUTE_TABLE["testgen"]["agents"] = original + [
            {"agent": "gemini", "mode": "mid", "late": False},
        ]
        with pytest.raises(AssertionError):
            test_testgen_never_routes_to_gemini_even_when_draining_agy()
    finally:
        router.ROUTE_TABLE["testgen"]["agents"] = original
