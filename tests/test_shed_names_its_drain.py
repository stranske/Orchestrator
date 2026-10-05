"""A shed seat says what clears it, and a refused codex offload sheds until the provider's reset.

THE REPORT (2026-10-05). The shed branch of the capacity decision printed "observed 429 /
rate-limit shed flag set" for every shed, although the marker `rate_incidents.ensure_shed` writes
holds `expires_at`, `incident_id`, `category` and `reset_at`. A 6 h cooldown, a provider's reset
four days out, and a marker placed by hand that nothing will ever clear all read alike. The reason
now names the expiry in UTC, the incident and its category, and the drain; a marker with no
readable expiry says it is manual and how to remove it. Unreadable still means shed, and unknown is
never reported as expired.

THE LENGTH. `dispatcher.offload` recorded a refusal with no `reset_at`, so a codex refusal that
named its reset shed for `ORCH_PROVIDER_COOLDOWN_S` (6 h) instead. Measured:
offload:codex:1790322148167312000 and offload:codex:1790322306091352000 (2026-09-25 07:42Z and
07:45Z) were refused with "try again at Sep 28th, 2026 8:26 PM" and recorded with no reset, so each
shed for the cooldown. The reconciler's own reading could not repair it: both observers share one
incident, and the offload records first. The reset now comes from one reader,
`rate_incidents.provider_reset_at`, used by every recorder, which reads codex's harness error events
and never the agent's work.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import adapters
import capacity
import dispatcher
import ledger_reconcile
import rate_incidents
import switch_review

COOLDOWN_S = 6 * 60 * 60
WARNING = "Under-development features enabled: chronicle."


def _clock(when: datetime) -> str:
    """A local wall-clock time spelled the way codex spells its reset."""
    return f"{when:%b} {when.day}th, {when.year} {when:%I:%M %p}"


def _future(days: float) -> datetime:
    return (datetime.now() + timedelta(days=days)).replace(second=0, microsecond=0)


def _refusal(when: datetime) -> str:
    return (
        "You’ve hit your usage limit. Visit https://chatgpt.com/codex/settings/usage to purchase "
        f"more credits or try again at {_clock(when)}."
    )


def _event(kind: str, item_type: str | None = None, **fields) -> str:
    if item_type is None:
        return json.dumps({"type": kind, **fields}, ensure_ascii=False)
    event = {"type": kind, "item": {"id": "item_1", "type": item_type, **fields}}
    return json.dumps(event, ensure_ascii=False)


HEAD = [
    _event("thread.started", thread_id="t"),
    _event("item.completed", "error", message=WARNING),
    _event("turn.started"),
]


def _refused(message: str) -> list[str]:
    """The measured shape (offload:codex:1790322148167312000): refused before any work."""
    return [
        *HEAD,
        _event("error", message=message),
        _event("turn.failed", error={"message": message}),
    ]


def _worked(output: str) -> list[str]:
    return [
        _event("item.started", "command_execution", command="sed -n 1,80p NOTES.md"),
        _event(
            "item.completed",
            "command_execution",
            command="sed -n 1,80p NOTES.md",
            aggregated_output=output,
            exit_code=0,
            status="completed",
        ),
        _event("item.completed", "agent_message", text=f"Read the notes: {output}"),
    ]


def _utc(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


@pytest.fixture
def stores(tmp_path, monkeypatch):
    """Private incident authority, shed markers and offload logs; no agent CLI, Brain or ledger."""
    monkeypatch.setattr(rate_incidents, "HANDOFF", tmp_path)
    monkeypatch.setattr(rate_incidents, "INCIDENT_FILE", tmp_path / "rate-limit-incidents.ndjson")
    monkeypatch.setattr(rate_incidents, "LOCK_FILE", tmp_path / "rate-limit-incidents.ndjson.lock")
    monkeypatch.setattr(rate_incidents, "SHED_DIR", tmp_path / "capacity-shed")
    monkeypatch.setattr(capacity, "SHED_DIR", tmp_path / "capacity-shed")
    monkeypatch.setenv("ORCH_PROVIDER_COOLDOWN_S", str(COOLDOWN_S))
    # The decision past the shed branch reads health probes; pin them so a drained seat stays
    # offline and prints the same ordinary reason on every machine.
    monkeypatch.setattr(capacity, "_auth_health", lambda agent: None)
    monkeypatch.setattr(capacity, "_model_health", lambda agent, tier="full": None)
    monkeypatch.setattr(dispatcher, "DISPATCH_LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(dispatcher, "AGENT_RUNTIME_DIR", tmp_path / "agent-runtime")
    monkeypatch.setattr(dispatcher, "_capability_heartbeat", lambda *args, **kwargs: None)
    monkeypatch.setattr(dispatcher, "_default_offload_timeout", lambda *args, **kwargs: 1)
    monkeypatch.setattr(dispatcher, "_offload_prompt", lambda prompt, *args: prompt)
    monkeypatch.setattr(dispatcher, "_select_offload_profile", lambda *args: None)
    monkeypatch.setattr(dispatcher, "_agent_log_tail_from_argv", lambda *args, **kwargs: "")
    monkeypatch.setattr(
        dispatcher.adapters, "can_report_cli_identity", lambda *args: (False, "test")
    )
    monkeypatch.setattr(dispatcher.adapters, "build_command", lambda *args, **kwargs: ["agent"])
    monkeypatch.setattr(dispatcher.adapters, "model_identity", lambda *args, **kwargs: "test-model")
    monkeypatch.setattr(dispatcher.adapters, "record_ledger", lambda *args, **kwargs: None)
    monkeypatch.setattr(dispatcher.feedback, "record_run", lambda *args, **kwargs: None)
    monkeypatch.setattr(dispatcher.feedback, "record_cost", lambda *args, **kwargs: None)
    monkeypatch.setenv("ORCH_OFFLOAD_NETWORK_RETRIES", "0")
    return tmp_path


def _offload(monkeypatch, stores, agent: str, returncode: int, stdout: str, stderr: str = ""):
    monkeypatch.setattr(
        dispatcher.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(["agent"], returncode, stdout, stderr),
    )
    return dispatcher.offload(agent, "test", cwd=str(stores))


def _incidents() -> list[dict]:
    if not rate_incidents.INCIDENT_FILE.exists():
        return []
    return [json.loads(line) for line in rate_incidents.INCIDENT_FILE.read_text().splitlines()]


def _marker(agent: str = "codex") -> dict:
    return json.loads((rate_incidents.SHED_DIR / agent).read_text())


def _reason(agent: str = "codex") -> tuple[str, str]:
    state, reason, _meta = capacity.compute(agent, capacity.AGENTS[agent], None)
    return state, reason


# ---- THE LENGTH: a refused codex offload sheds until the provider's stated reset -------------


def test_a_refused_codex_offload_sheds_until_the_providers_reset(monkeypatch, stores):
    """End to end, the measured shape: offload -> incident -> marker -> what the seat prints."""
    when = _future(days=3)
    reset_at = int(when.timestamp())
    result = _offload(monkeypatch, stores, "codex", 1, "\n".join(_refused(_refusal(when))))
    (incident,) = _incidents()
    assert incident["surface"] == "dispatcher.offload" and incident["category"] == "quota", incident
    assert incident["reset_at"] == reset_at, incident
    assert _marker()["expires_at"] == reset_at > incident["ts"] + COOLDOWN_S, _marker()
    state, reason = _reason()
    assert state == capacity.SHED, (state, reason)
    assert reason == (
        f"shed until {_utc(reset_at)} (the provider's stated reset) by quota incident "
        f"{incident['incident_id']}; the first capacity read after that clears it"
    ), reason
    assert result["exit"] == 1


def test_the_gate_opens_at_the_reset_and_not_before(monkeypatch, stores):
    """Latched-gate question 3: the window measured (the provider's) is the window drained."""
    when = _future(days=2)
    reset_at = int(when.timestamp())
    _offload(monkeypatch, stores, "codex", 1, "\n".join(_refused(_refusal(when))))
    monkeypatch.setattr(capacity.time, "time", lambda: reset_at - 1)
    assert capacity._shed("codex") is True
    monkeypatch.setattr(capacity.time, "time", lambda: reset_at)
    assert capacity._shed("codex") is False and not (rate_incidents.SHED_DIR / "codex").exists()


def test_a_limit_hit_after_work_carries_its_reset(monkeypatch, stores):
    when = _future(days=1)
    lines = [*HEAD, *_worked("12 passed"), *_refused(_refusal(when))[3:]]
    _offload(monkeypatch, stores, "codex", 1, "\n".join(lines))
    assert [row["reset_at"] for row in _incidents()] == [int(when.timestamp())]


def test_a_reset_quoted_in_the_agents_own_work_is_never_read(monkeypatch, stores):
    """The agent read a doc that quotes a far-off reset; the harness's refusal named none."""
    quoted = _refusal(_future(days=40))
    lines = [*HEAD, *_worked(quoted), *_refused("You’ve hit your usage limit.")[3:]]
    _offload(monkeypatch, stores, "codex", 1, "\n".join(lines))
    (incident,) = _incidents()
    assert "reset_at" not in incident, incident
    assert _marker()["expires_at"] == incident["ts"] + COOLDOWN_S, _marker()
    assert "a cooldown; no provider reset recorded" in _reason()[1]


@pytest.mark.parametrize("where", ["stdout", "stderr"])
def test_assess_mode_text_is_never_read_for_a_reset(monkeypatch, stores, where):
    """Without `--json` a codex run's stdout is its final text and its stderr can carry its
    transcript, so a plain line may be the agent's own work: classified as before, never a reset."""
    text = _refusal(_future(days=40))
    stdout, stderr = (text, "") if where == "stdout" else ("", text)
    _offload(monkeypatch, stores, "codex", 1, stdout, stderr)
    (incident,) = _incidents()
    assert incident["category"] == "quota" and "reset_at" not in incident, incident


@pytest.mark.parametrize("agent", ["claude", "cursor", "gemini", "vibe"])
def test_another_agent_never_carries_a_reset(monkeypatch, stores, agent):
    _offload(monkeypatch, stores, agent, 1, _refusal(_future(days=40)))
    (incident,) = _incidents()
    assert incident["agent"] == agent and "reset_at" not in incident, incident


@pytest.mark.parametrize(
    "shape", ["refused-before-work", "limit-after-work"], ids=lambda shape: shape
)
def test_both_observers_of_a_run_set_the_same_reset(monkeypatch, stores, shape):
    """Each observer, when it is the one to record first, sets the same shed. Until 2026-10-05 the
    offload set none and the reconciler only a refusal's, so the shed a run left depended on which
    observer reached it first, and the offload always did."""
    when = _future(days=2)
    refusal = _refused(_refusal(when))
    lines = refusal if shape == "refused-before-work" else [*HEAD, *_worked("ok"), *refusal[3:]]
    result = _offload(monkeypatch, stores, "codex", 1, "\n".join(lines))
    (offload_incident,) = _incidents()
    segment = ledger_reconcile._log_segment(Path(result["log"]), result["run_id"])
    assert segment, "the offload must have written its own log segment"
    # A fresh authority, so the reconciler is the first observer rather than a deduped second one.
    second = stores / "second"
    second.mkdir()
    monkeypatch.setattr(rate_incidents, "INCIDENT_FILE", second / "rate-limit-incidents.ndjson")
    ledger_reconcile._classify_run_log_segment(
        segment, "codex", result["run_id"], shed=False, successful=False
    )
    (reconcile_incident,) = _incidents()
    assert reconcile_incident["surface"] == "ledger_reconcile.completion", reconcile_incident
    assert reconcile_incident["reset_at"] == offload_incident["reset_at"] == int(when.timestamp())


def test_the_synchronous_adapter_passes_the_reset(monkeypatch, stores):
    """adapters.dispatch has no production caller; it is the third recorder, so it reads alike."""
    when = _future(days=2)
    calls = iter(
        (
            subprocess.CompletedProcess(["agent"], 1, "\n".join(_refused(_refusal(when))), ""),
            subprocess.CompletedProcess(["git"], 0, "", ""),
        )
    )
    monkeypatch.setattr(adapters, "build_command", lambda *args, **kwargs: ["agent"])
    monkeypatch.setattr(adapters.subprocess, "run", lambda *args, **kwargs: next(calls))
    monkeypatch.setattr(adapters, "record_ledger", lambda *args, **kwargs: None)
    adapters.dispatch("codex", "test", cwd=str(stores))
    assert [row["reset_at"] for row in _incidents()] == [int(when.timestamp())]


@pytest.mark.parametrize(
    "line,read",
    [
        (_event("error", message=_refusal(datetime(2099, 1, 5, 3, 11))), True),
        (_event("turn.failed", error={"message": _refusal(datetime(2099, 1, 5, 3, 11))}), True),
        (_event("turn.failed", error=_refusal(datetime(2099, 1, 5, 3, 11))), True),
        (_event("item.completed", "error", message=_refusal(datetime(2099, 1, 5, 3, 11))), True),
        (
            _event("item.completed", "agent_message", text=_refusal(datetime(2099, 1, 5, 3, 11))),
            False,
        ),
        (_worked(_refusal(datetime(2099, 1, 5, 3, 11)))[1], False),
        (_refusal(datetime(2099, 1, 5, 3, 11)), False),
    ],
    ids=[
        "error-event",
        "turn-failed",
        "turn-failed-string",
        "error-item",
        "agent-message",
        "command-output",
        "plain-line",
    ],
)
def test_the_reset_is_read_from_harness_error_events_only(line, read):
    expected = int(datetime(2099, 1, 5, 3, 11).timestamp()) if read else None
    assert rate_incidents.provider_reset_at("codex", [line]) == expected
    assert rate_incidents.provider_reset_at("claude", [line]) is None


def test_the_latest_stated_reset_binds_and_a_past_one_is_none():
    early, late = datetime(2099, 1, 5, 3, 11), datetime(2099, 1, 7, 9, 30)
    lines = [_event("error", message=_refusal(early)), _event("error", message=_refusal(late))]
    assert rate_incidents.provider_reset_at("codex", lines) == int(late.timestamp())
    past = [_event("error", message=_refusal(datetime(2020, 1, 5, 3, 11)))]
    assert rate_incidents.provider_reset_at("codex", past) is None


# ---- THE REPORT: a shed seat says what clears it ---------------------------------------------


def test_a_cooldown_with_no_stated_reset_says_so(stores):
    now = int(time.time())
    recorded = rate_incidents.record_incident(
        agent="codex", surface="test", category="rate_limit", run_id="r1", timestamp=now
    )
    state, reason = _reason()
    assert state == capacity.SHED and reason == (
        f"shed until {_utc(now + COOLDOWN_S)} (a cooldown; no provider reset recorded) by "
        f"rate_limit incident {recorded['incident_id']}; the first capacity read after that "
        "clears it"
    ), reason


def test_a_cooldown_that_outlasts_the_reset_names_both(stores):
    now = int(time.time())
    rate_incidents.record_incident(
        agent="codex",
        surface="test",
        category="quota",
        run_id="r1",
        timestamp=now,
        reset_at=now + 60,
    )
    reason = _reason()[1]
    assert f"shed until {_utc(now + COOLDOWN_S)}" in reason, reason
    assert f"a cooldown, which outlasts the provider's stated reset {_utc(now + 60)}" in reason


@pytest.mark.parametrize(
    "raw,why",
    [
        ("", "empty, a legacy touch marker"),
        ("not json", "not JSON"),
        ("[1, 2]", "JSON that is not an object"),
        ('{"incident_id": "i9", "category": "quota"}', "no expires_at, from quota incident i9"),
        ('{"expires_at": true}', "expires_at True is not a finite number"),
        ('{"expires_at": NaN}', "expires_at nan is not a finite number"),
        ('{"expires_at": "tomorrow"}', "expires_at 'tomorrow' is not a finite number"),
    ],
    ids=["legacy-touch", "not-json", "not-an-object", "no-expiry", "boolean", "nan", "text"],
)
def test_a_marker_with_no_readable_expiry_says_it_is_manual_and_how_to_clear_it(stores, raw, why):
    rate_incidents.SHED_DIR.mkdir(parents=True)
    marker = rate_incidents.SHED_DIR / "codex"
    marker.write_text(raw)
    state, reason = _reason()
    assert state == capacity.SHED, (state, reason)
    assert reason == (
        f"shed by a manual marker with no expiry ({why}): nothing clears it automatically; "
        f"remove {marker} to re-enable codex"
    ), reason
    assert marker.exists(), "a manual marker must never be removed by a read"


@pytest.mark.parametrize("kind", ["directory", "not-utf8"])
def test_an_unreadable_marker_holds_the_seat_and_never_reads_as_expired(stores, kind):
    rate_incidents.SHED_DIR.mkdir(parents=True)
    marker = rate_incidents.SHED_DIR / "codex"
    if kind == "directory":
        marker.mkdir()
    else:
        marker.write_bytes(b"\xff\xfe\x00")
    found = capacity.shed_marker("codex")
    assert found is not None and found["state"] == "unreadable", found
    state, reason = _reason()
    assert state == capacity.SHED, (state, reason)
    assert reason.startswith("shed by a marker that cannot be read ("), reason
    assert reason.endswith(f"nothing clears it automatically; remove {marker} to re-enable codex")
    assert marker.exists()


def test_a_drained_seat_prints_exactly_what_a_never_shed_seat_prints(stores):
    """Latched-gate question 4, by construction: drained has words, and they are the seat's own."""
    never_state, never_reason = _reason()
    rate_incidents.SHED_DIR.mkdir(parents=True)
    marker = rate_incidents.SHED_DIR / "codex"
    marker.write_text(json.dumps({"expires_at": time.time() - 1, "incident_id": "old"}))
    drained_state, drained_reason = _reason()
    assert (
        (drained_state, drained_reason)
        == (never_state, never_reason)
        == (
            capacity.OK,
            "ccusage unavailable; 429-shed authoritative",
        )
    )
    assert not marker.exists()


def test_the_reader_never_removes_and_the_gate_does(stores):
    """The weekly sweep reads through `shed_marker`; only the gate's own read drains."""
    rate_incidents.SHED_DIR.mkdir(parents=True)
    marker = rate_incidents.SHED_DIR / "codex"
    marker.write_text(json.dumps({"expires_at": time.time() - 1}))
    assert capacity.shed_marker("codex")["state"] == "expired" and marker.exists()
    assert capacity._shed("codex") is False and not marker.exists()
    assert capacity.shed_marker("codex") is None


def test_an_expired_marker_that_cannot_be_removed_still_holds_and_says_so(stores, monkeypatch):
    rate_incidents.SHED_DIR.mkdir(parents=True)
    marker = rate_incidents.SHED_DIR / "codex"
    expired_at = time.time() - 1
    marker.write_text(json.dumps({"expires_at": expired_at}))

    def refuse(self, *args, **kwargs):
        raise PermissionError("read-only")

    monkeypatch.setattr(Path, "unlink", refuse)
    state, reason = _reason()
    assert state == capacity.SHED, (state, reason)
    assert reason.startswith(f"shed: its marker expired {_utc(expired_at)} and is still on disk")


def test_a_marker_gone_between_the_check_and_the_reason_says_so(stores, monkeypatch):
    monkeypatch.setattr(capacity, "_shed", lambda agent: True)
    state, reason = _reason()
    assert state == capacity.SHED and "was gone when this reason was read" in reason, reason
    assert "until" not in reason and "expired" not in reason, reason


# ---- THE WEEKLY SWEEP: one reader, FYI only ----------------------------------------------------


def test_the_sweep_prints_the_sentence_the_seat_prints(stores):
    """One rendering: the sweep and the gate cannot describe the same marker two ways."""
    rate_incidents.record_incident(
        agent="codex",
        surface="test",
        category="quota",
        run_id="r1",
        reset_at=int(time.time()) + 90_000,
    )
    section = switch_review.capacity_shed()
    (row,) = section["markers"]
    assert row["reason"] == _reason()[1] and row["suspect"] is None, row
    assert section["suspect"] == 0, section


def test_a_hand_placed_marker_past_the_horizon_is_suspect(stores):
    rate_incidents.SHED_DIR.mkdir(parents=True)
    marker = rate_incidents.SHED_DIR / "claude"
    marker.touch()
    old = time.time() - (switch_review.SHED_MANUAL_HORIZON_DAYS + 1) * 86400
    os.utime(marker, (old, old))
    section = switch_review.capacity_shed()
    assert section["suspect"] == 1, section
    assert "nothing clears it but removing it" in section["markers"][0]["suspect"], section
    fresh = time.time() - (switch_review.SHED_MANUAL_HORIZON_DAYS - 1) * 86400
    os.utime(marker, (fresh, fresh))
    assert switch_review.capacity_shed()["suspect"] == 0


def _report(shed: dict | None) -> str:
    rep = {
        "generated_at": 0,
        "review_days": switch_review.REVIEW_DAYS,
        "held_off": [],
        "on_but_idle": [],
        "unconditioned": [],
        "stale_runners": [],
        "mirror_drift": {"status": "ok"},
        "raise_count": 0,
    }
    if shed is not None:
        rep["capacity_shed"] = shed
    return switch_review.format_report(rep)


def test_nothing_due_is_never_printed_over_a_suspect_or_unmeasured_shed(stores, tmp_path):
    drained = switch_review.capacity_shed(shed_dir=tmp_path / "never-written")
    assert "no seat is shed" in _report(drained) and "Nothing due" in _report(drained)
    assert "Nothing due" in _report(None), "a report from before the section existed still reads"
    (tmp_path / "a-file").write_text("")
    unmeasured = switch_review.capacity_shed(shed_dir=tmp_path / "a-file")
    assert "NOT MEASURED" in _report(unmeasured) and "Nothing due" not in _report(unmeasured)
    rate_incidents.SHED_DIR.mkdir(parents=True)
    (rate_incidents.SHED_DIR / "codex").write_text(json.dumps({"expires_at": time.time() - 2e5}))
    stuck = switch_review.capacity_shed()
    assert stuck["suspect"] == 1 and "Nothing due" not in _report(stuck), stuck


def test_the_section_is_fyi_only_and_rides_the_review(stores, monkeypatch):
    monkeypatch.setattr(switch_review, "stale_runners", lambda **_: [])
    monkeypatch.setattr(switch_review, "mirror_drift", lambda **_: {"status": "ok"})
    monkeypatch.setattr(switch_review, "fleet_gates", lambda **_: {"suspect": False})
    monkeypatch.setattr(switch_review, "_exploration_gate", lambda: {"suspect": False})
    monkeypatch.setattr(switch_review, "_capability_heartbeat", lambda *_a, **_k: None)
    monkeypatch.setattr(switch_review, "gate_expiry", lambda **_: {"status": "ok"})
    monkeypatch.setattr(switch_review, "firing_regressions", lambda **_: {"status": "unknown"})
    monkeypatch.setattr(
        switch_review, "switch_states", lambda **_: {"held_off": [], "on_but_idle": []}
    )
    rate_incidents.SHED_DIR.mkdir(parents=True)
    (rate_incidents.SHED_DIR / "codex").write_text("")
    os.utime(rate_incidents.SHED_DIR / "codex", (1, 1))
    rep = switch_review.review(env={"ORCH_VALUE_CHAIN_MONITOR": "0"})
    assert rep["capacity_shed"]["suspect"] == 1, rep["capacity_shed"]
    assert rep["raise_count"] == 0, "a suspect marker is FYI, never a question"
    assert (rate_incidents.SHED_DIR / "codex").exists(), "a review removed a marker"
