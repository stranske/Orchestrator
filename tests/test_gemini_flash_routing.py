"""Owner decisions 2026-10-31 on Gemini Flash routing.

1. Testgen is off Gemini Flash: lowest first-pass mutation pinning of every agent in the
   2026-10-24/25 coverage campaign, and output-token-limit failures on batched testgen.
2. A 7-day triage trial: while the trial file names a window containing now, gemini-routed
   triage alternates by UTC hour between Flash-high (mode "mid") and Argon (the role's "full").
"""

from __future__ import annotations

import json

import pytest

import dispatcher
import feedback
import roles
import router

HOUR = 3600
START = 498_334 * HOUR  # an even UTC hour number (498334), so EVEN maps to Flash
EVEN = START + 60
ODD = START + HOUR + 60


def _trial(tmp_path, start=START, end=START + 7 * 86400):
    path = tmp_path / "role-triage-flash.json"
    path.write_text(json.dumps({"start_ts": start, "end_ts": end}), encoding="utf-8")
    return path


def test_testgen_never_routes_to_gemini_even_when_draining_agy():
    assert "testgen" not in router.GEMINI_GOOD_FIT_TASKS
    assert all(entry["agent"] != "gemini" for entry in router.ROUTE_TABLE["testgen"]["agents"])


@pytest.mark.parametrize(
    ("backend", "now", "expected"),
    [("gemini", EVEN, "mid"), ("gemini", ODD, None), ("codex", EVEN, None), ("cursor", ODD, None)],
)
def test_trial_alternates_gemini_triage_by_hour(tmp_path, backend, now, expected):
    assert roles.triage_trial_mode(backend, path=_trial(tmp_path), now=now) == expected


@pytest.mark.parametrize("when", [START - 1, START + 7 * 86400])
def test_trial_is_off_outside_its_window(tmp_path, when):
    assert roles.triage_trial_mode("gemini", path=_trial(tmp_path), now=when) is None


@pytest.mark.parametrize("content", [None, "", "not json", "{}", '{"start_ts": "x", "end_ts": 1}'])
def test_missing_or_malformed_trial_file_means_no_trial(tmp_path, content):
    path = tmp_path / "role-triage-flash.json"
    if content is not None:
        path.write_text(content, encoding="utf-8")
    assert roles.triage_trial_mode("gemini", path=path, now=EVEN) is None


def test_triage_dispatch_uses_and_records_the_trial_mode(monkeypatch, tmp_path):
    monkeypatch.setattr(roles, "TRIAGE_FLASH_TRIAL_PATH", _trial(tmp_path))
    monkeypatch.setattr(roles.time, "time", lambda: float(EVEN))
    calls, records = [], []

    def offload(agent, prompt, **kwargs):
        calls.append((agent, kwargs["mode"]))
        return {"run_id": "b1", "model": "gemini-3.8-flash-high", "output": "nope", "exit": 0}

    monkeypatch.setattr(dispatcher, "offload", offload)
    monkeypatch.setattr(feedback, "record_role_run", lambda *a, **k: records.append(k))
    roles.run_triage_agent(
        backlog_items=[{"target": "o/r#1"}], backend="gemini", dispatch=True, cap={}
    )
    assert calls == [("gemini", "mid")]
    assert records and records[0]["reasoning_level"] == "mid"
