"""A consumer pass reads the switch rows ONCE, and a declared-fact lookup never runs the sweep.

`capability_propensity.declared_facts` echoes a capability's gate (flag, state, criterion) from
`switch_review`, which owns the flag -> capability mapping. It read that by calling
`switch_review.review()` TWICE per lookup, and it is called once per capability: for every bound
capability in `propose_offer_improvements`, and up to twice inside one `record_reoffer`. But
`review()` is the weekly sweep. Beside the switch rows it runs `fleet_gates` — one pull-request
listing per consumer repo and one GraphQL review-thread query per open canary — plus git, ps, a
Brain read and a heartbeat. So one `offer-improvements` pass ran the sweep twice for every bound
capability, and `capability_propensity --selftest` ran it twenty times against real GitHub on any
machine with an authenticated `gh`, which is how a selftest came to take minutes inside `verify.py`
while staying fast on CI, where `gh` fails at once.

The fix reads `switch_review.switch_states()` — the rows alone, still owned by `switch_review` —
once per pass, and hands it to every lookup. These tests count BOTH accessors, so they hold
whichever one a pass uses, and the counting tests run (and fail on the count) against the old code
too: `switch_states` did not exist there, hence `raising=False`.
"""

from __future__ import annotations

import json

import pytest

import capabilities
import capability_advisor
import capability_propensity as cp
import switch_review

GATE_ROWS = {
    "held_off": [
        {
            "flag": "ORCH_FIXTURE_GATE",
            "capability": "fixture-gated",
            "state": "off",
            "criterion": "the fixture's switch-on criterion",
        }
    ],
    "on_but_idle": [],
}
GATE_FACTS = {
    "gate_flag": "ORCH_FIXTURE_GATE",
    "gate_state": "off",
    "gate_criterion": "the fixture's switch-on criterion",
}
# Eight words or more, so a decline can QUOTE it and `_quoted` can recognise the quote.
QUOTABLE = "run fixture_tool.py --json and it reports the fixture surface and nothing else"


@pytest.fixture
def reads(monkeypatch):
    """Answer every switch read from GATE_ROWS and count each accessor separately."""
    calls = {"review": 0, "switch_states": 0}

    def answer(name):
        def read(**_kwargs):
            calls[name] += 1
            return json.loads(json.dumps(GATE_ROWS))

        return read

    monkeypatch.setattr(switch_review, "review", answer("review"))
    monkeypatch.setattr(switch_review, "switch_states", answer("switch_states"), raising=False)
    return calls


def _ledger(tmp_path, *capability_ids):
    path = tmp_path / "capabilities.json"
    capabilities.save({c: capabilities._blank_capability(c) for c in capability_ids}, path)
    return path


def test_an_offer_improvements_pass_reads_the_switch_rows_once(reads, tmp_path):
    ledger = _ledger(tmp_path)
    rep = cp.propose_offer_improvements(path=ledger)
    assert rep["measured"] is True, rep
    # N lookups, or "at most one read" below would hold trivially.
    assert rep["bound_capabilities"] > 1, rep["bound_capabilities"]
    assert reads["review"] + reads["switch_states"] <= 1, (
        f"{rep['bound_capabilities']} declared-fact lookups in one pass made {reads} switch reads; "
        "the pass must read once and hand the rows to every lookup"
    )


def test_one_reoffer_is_one_switch_read_and_still_echoes_the_gate(reads, tmp_path):
    ledger = _ledger(tmp_path, "fixture-gated")
    exp = "advice:fixture000001"
    cp.record_decline(
        "fixture-gated",
        exp,
        reason="env-gated off behind some flag I could not name",
        surface="fixture-surface",
        kind="gated_off",
        path=ledger,
    )
    res = cp.record_reoffer("fixture-gated", exp, decline_kind="gated_off", path=ledger)
    assert res["reoffered"] is True, res
    assert res["facts"] == GATE_FACTS, res["facts"]
    assert reads["review"] + reads["switch_states"] == 1, reads


def test_a_reoffer_making_both_lookups_still_reads_once(reads, tmp_path, monkeypatch):
    """The caller-already-had-it branch looks the facts up TWICE, so it separates a shared read from
    two private ones: every declared fact is quoted back, the undelivered set is empty, and the
    second lookup asks whether anything was declared at all."""
    monkeypatch.setitem(capability_advisor.HOW_TO_USE, "fixture-quoted", QUOTABLE)
    ledger = _ledger(tmp_path, "fixture-quoted")
    exp = "advice:fixture000002"
    cp.record_decline(
        "fixture-quoted",
        exp,
        reason=f"I read the guidance: {QUOTABLE} — and it does not fit this target.",
        surface="fixture-surface",
        kind="wrong_match",
        path=ledger,
    )
    res = cp.record_reoffer("fixture-quoted", exp, decline_kind="wrong_match", path=ledger)
    assert res.get("caller_already_had_the_facts") is True, res
    assert reads["review"] + reads["switch_states"] == 1, reads


def test_a_declared_fact_lookup_never_runs_the_sweep_or_reaches_github(monkeypatch, tmp_path):
    """Nothing stubbed on the read path: the REAL mapping and the real row logic, over a private
    ledger. Only the sweep's local side effects are held still, so the old code reaches its `gh`
    calls quickly and deterministically instead of reading this machine's processes, mirror and
    Brain, and every `gh` call lands on `switch_review`'s own injection seam instead of the network.
    """
    gh_calls: list = []

    def no_network(args, *, timeout_s=30):
        gh_calls.append(args)
        return False, "", "unmeasured: this test has no network"

    monkeypatch.setattr(switch_review, "_GH_CALL_RUNNER", no_network)
    monkeypatch.setattr(switch_review, "stale_runners", lambda **_k: [])
    monkeypatch.setattr(switch_review, "mirror_drift", lambda **_k: {"status": "ok"})
    monkeypatch.setattr(switch_review, "_exploration_gate", lambda: {"suspect": False})
    monkeypatch.delenv("ORCH_CAPABILITY_HEARTBEATS", raising=False)
    for flag in switch_review.SWITCH_CAPABILITY:
        monkeypatch.delenv(flag, raising=False)
    ledger = _ledger(tmp_path, *switch_review.SWITCH_CAPABILITY.values())
    monkeypatch.setattr(capabilities, "REG", ledger)

    flag, capability = sorted(switch_review.SWITCH_CAPABILITY.items())[0]
    facts = cp.declared_facts(capability)

    assert gh_calls == [], (
        f"a declared-fact lookup attempted {len(gh_calls)} gh call(s), first {gh_calls[:1]}: it "
        "ran the switch SWEEP, which lists every consumer repo's pull requests over the network"
    )
    # And the gate still arrives, from the real mapping: the fix must not have cut the fact off.
    assert facts.get("gate_flag") == flag, facts
    assert facts.get("gate_state") == "off", facts
