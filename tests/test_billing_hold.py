"""Billing holds (owner decision 2026-11-24, Orchestrator#1045): a 402 spend refusal holds the seat until a probe works."""

import json

import pytest

import capacity
import rate_incidents
import switch_review

NOW = 1_800_000_000

VIBE_402 = (
    "Error: API error from mistral (model: mistral-vibe-cli-latest): LLM backend error [mistral]\n"
    "  status: 402 Payment Required\n  reason: Payment Required\n"
    '  body_excerpt: {"object":"error","message":"Vibe budget exhausted.",'
    '"type":"billing_vibe_budget_exhausted","code":"2304","raw_status_code":402}\n'
)


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(rate_incidents, "HANDOFF", tmp_path)
    monkeypatch.setattr(rate_incidents, "INCIDENT_FILE", tmp_path / "rate-limit-incidents.ndjson")
    monkeypatch.setattr(rate_incidents, "LOCK_FILE", tmp_path / "rate-limit-incidents.ndjson.lock")
    monkeypatch.setattr(rate_incidents, "SHED_DIR", tmp_path / "capacity-shed")
    monkeypatch.setattr(capacity, "SHED_DIR", tmp_path / "capacity-shed")
    return tmp_path / "capacity-shed"


def _refuse(agent="vibe", run="r1"):
    evidence = rate_incidents.get_structured_evidence(
        VIBE_402, agent, "dispatcher.offload", run_id=run
    )
    assert evidence["subcategory"] == "billing_exhausted"
    return rate_incidents.record_incident(
        agent=agent,
        surface="dispatcher.offload",
        category=evidence["category"],
        status="recorded",
        run_id=run,
        evidence=VIBE_402,
        extra={"subcategory": evidence["subcategory"]},
    )


def test_a_billing_refusal_holds_the_seat_with_no_expiry(ledger):
    _refuse()
    payload = json.loads((ledger / "vibe").read_text())
    assert payload["hold"] == rate_incidents.BILLING_HOLD and "expires_at" not in payload
    marker = capacity.shed_marker("vibe")
    assert marker["hold"] == "billing_probe" and marker["state"] == "manual"
    assert capacity._shed("vibe") is True
    assert "probes it with one one-line request" in capacity.shed_reason(marker)


def test_a_quota_refusal_keeps_its_timed_bench(ledger):
    rate_incidents.record_incident(
        agent="codex",
        surface="dispatcher.offload",
        category="quota",
        status="recorded",
        run_id="q1",
        evidence="429 usage limit",
        extra={"subcategory": "usage_limit"},
    )
    payload = json.loads((ledger / "codex").read_text())
    assert "hold" not in payload and isinstance(payload["expires_at"], int)


def test_the_probe_clears_an_answered_hold_and_reschedules_a_refused_one(ledger):
    _refuse()
    calls = []

    def refused(agent, prompt, **kw):
        calls.append(agent)
        return {"exit": 1, "output": VIBE_402}

    payload = json.loads((ledger / "vibe").read_text())
    not_due = rate_incidents.probe_billing_holds(now=payload["probe_after"] - 1, offload=refused)
    assert not_due == [{"agent": "vibe", "probe": "not_due", "probe_after": payload["probe_after"]}]
    assert calls == []
    rows = rate_incidents.probe_billing_holds(now=payload["probe_after"] + 1, offload=refused)
    assert rows[0]["probe"] == "still_refused" and calls == ["vibe"]
    later = json.loads((ledger / "vibe").read_text())["probe_after"]
    assert later == payload["probe_after"] + 1 + rate_incidents.BILLING_PROBE_INTERVAL_S
    rows = rate_incidents.probe_billing_holds(
        now=later, offload=lambda agent, prompt, **kw: {"exit": 0, "output": "OK"}
    )
    assert rows == [{"agent": "vibe", "probe": "cleared"}]
    assert not (ledger / "vibe").exists() and capacity._shed("vibe") is False


def test_a_hold_is_suspect_only_when_its_probe_stops(ledger):
    _refuse()
    due = json.loads((ledger / "vibe").read_text())["probe_after"]
    fresh = switch_review.capacity_shed(now=due + 3600, shed_dir=ledger)
    assert fresh["suspect"] == 0
    stale = switch_review.capacity_shed(now=due + 3 * 86400, shed_dir=ledger)
    assert stale["suspect"] == 1 and "has not run since" in stale["markers"][0]["suspect"]


def _timed_marker(ledger, agent="vibe", expires_at=None):
    ledger.mkdir(parents=True, exist_ok=True)
    (ledger / agent).write_text(
        json.dumps(
            {"category": "quota", "incident_id": "old", "expires_at": expires_at or NOW + 3600}
        )
        + "\n"
    )


def test_a_hold_supersedes_a_timed_cooldown(ledger):
    _timed_marker(ledger)
    _refuse()
    payload = json.loads((ledger / "vibe").read_text())
    assert payload["hold"] == rate_incidents.BILLING_HOLD and "expires_at" not in payload


def test_a_second_refusal_keeps_the_existing_hold_schedule(ledger):
    _refuse(run="r1")
    first = json.loads((ledger / "vibe").read_text())
    _refuse(run="r2")
    assert json.loads((ledger / "vibe").read_text()) == first


def test_a_hold_never_replaces_a_manual_marker(ledger):
    ledger.mkdir(parents=True, exist_ok=True)
    (ledger / "vibe").write_text("")
    _refuse()
    assert (ledger / "vibe").read_text() == ""


def test_a_failed_hold_publication_is_repaired_on_retry(ledger, monkeypatch):
    """Review on #1488: the row persists expires_at=None; the dedup path must republish the hold."""
    real = rate_incidents.ensure_shed
    monkeypatch.setattr(rate_incidents, "ensure_shed", lambda *a, **kw: False)
    assert _refuse()["status"] == "shed_unfulfilled"
    assert not (ledger / "vibe").exists()
    monkeypatch.setattr(rate_incidents, "ensure_shed", real)
    retry = _refuse()
    assert retry["deduped"] is True and retry["shed"] is True
    assert json.loads((ledger / "vibe").read_text())["hold"] == rate_incidents.BILLING_HOLD


def _due(ledger, agent="vibe"):
    return json.loads((ledger / agent).read_text())["probe_after"] + 1


def test_a_claude_hold_is_probed_on_its_own_seat_and_not_cleared_by_a_substitute(ledger):
    _refuse(agent="claude")
    seen = []

    def substitute(agent, prompt, **kw):
        seen.append(kw.get("mode"))
        return {"agent": "gemini", "exit": 0, "output": "OK"}

    rows = rate_incidents.probe_billing_holds(now=_due(ledger, "claude"), offload=substitute)
    assert seen and seen[0] is not None  # an explicit mode turns off offload's automatic backup
    assert rows[0]["probe"] == "still_refused" and "substitute gemini" in rows[0]["evidence"]
    assert (ledger / "claude").exists()


def test_a_marker_replaced_during_the_probe_is_not_cleared(ledger):
    _refuse()

    def answered_while_owner_stands_down(agent, prompt, **kw):
        (ledger / "vibe").write_text("")  # the owner's manual marker lands mid-probe
        return {"agent": agent, "exit": 0, "output": "OK"}

    rows = rate_incidents.probe_billing_holds(
        now=_due(ledger), offload=answered_while_owner_stands_down
    )
    assert rows == [{"agent": "vibe", "probe": "marker_changed"}]
    assert (ledger / "vibe").read_text() == ""


def test_a_failed_reschedule_keeps_the_previous_hold(ledger, monkeypatch):
    _refuse()
    before = (ledger / "vibe").read_text()

    def broken_replace(*a, **kw):
        raise OSError("disk full")

    monkeypatch.setattr(rate_incidents.os, "replace", broken_replace)
    rows = rate_incidents.probe_billing_holds(
        now=_due(ledger), offload=lambda agent, prompt, **kw: {"exit": 1, "output": VIBE_402}
    )
    assert rows[0]["probe"] == "reschedule_failed"
    assert (ledger / "vibe").read_text() == before
    assert [p.name for p in ledger.iterdir()] == ["vibe"]  # no temporary left behind
