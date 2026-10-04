"""The consult-trial ref prefix has ONE definition, and every writer and reader follows it.

`capabilities.ADVICE_REF_PREFIX` is minted into ids by `capability_advisor.experiment_id`, recorded
as the advisor's `match` ref and idempotency key, carried by `capability_propensity`'s trigger and
verdicts, and read by `capability_outcome_bridge` to join a lane's verdict to the fleet run it names.
Until 2026-10-04 the advisor and the bridge each held their own `"advice:"` literal. Behaviour was
identical while the spellings agreed, which is why no test could tell: the day the definition
changed, the advisor's offers would have landed under a ref no experiment reads and the bridge would
have stopped joining verdicts, with nothing failing.

So the first test RESPELLS the definition and runs the real writers and the real reader end to end.
`capability_propensity` binds the constant at import, an alias of the same object that a real edit
to `capabilities.py` moves too, so it is respelt alongside. Every other site must follow on its own,
and a copied literal fails by name. The second test pins today's bytes, because the live ledger's
offer keys must keep deduping.
"""

from __future__ import annotations

import hashlib

import pytest

import capabilities
import capability_advisor as advisor
import capability_outcome_bridge as bridge
import capability_propensity as propensity

RESPELT = "trial:"
VERSION = "capability-version:" + "a" * 32
TASK = "audit the retry helper for unbounded backoff"
OFFER = {"task": TASK, "capabilities": [{"capability_id": "offload"}]}


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    """A private ledger with one versioned capability, so a verdict can name a deliverable."""
    path = tmp_path / "capabilities.json"
    cap = capabilities._blank_capability("offload")
    cap["status"] = "generated"
    cap["capability_version_id"] = VERSION
    capabilities.save({"offload": cap}, path)
    monkeypatch.delenv("ORCH_CAPABILITY_HEARTBEATS", raising=False)
    return path


def _offers(ledger) -> list[dict]:
    row = capabilities.load(ledger, create=False)["offload"]
    return [e for e in row.get("event_history") or [] if e.get("type") == "match"]


def test_a_respelt_prefix_reaches_every_writer_and_the_bridge(ledger, monkeypatch):
    monkeypatch.setattr(capabilities, "ADVICE_REF_PREFIX", RESPELT)
    monkeypatch.setattr(propensity, "ADVICE_REF_PREFIX", RESPELT)
    digest = hashlib.sha1(TASK.encode()).hexdigest()[:12]

    exp = advisor.experiment_id(TASK)
    assert (
        exp == RESPELT + digest
    ), f"capability_advisor.experiment_id holds its own prefix: {exp!r}"
    assert advisor._record_matches(OFFER, path=ledger) == 1
    (offer,) = _offers(ledger)
    assert offer["ref"] == exp, f"the advisor's offer is not recorded under the id: {offer}"
    key = offer["idempotency_key"]
    assert key == f"{RESPELT}offload:{digest}", f"the offer key holds its own prefix: {key!r}"
    trial = [t for t in propensity.experiments(path=ledger) if t["experiment_id"] == exp]
    assert trial and trial[0]["candidates"] == ["offload"], "no experiment can find the offer"

    assert propensity.record_trigger("offload", exp, deliverable="O/R#7", path=ledger)
    assert propensity.record_usefulness(
        "offload",
        exp,
        useful=True,
        evidence="review exposed the missing edge before delivery",
        provenance=propensity.PROVENANCE_DEFAULT,
        deliverable="O/R#7",
        path=ledger,
    )
    index = bridge._fleet_verdict_index(capabilities.load(ledger, create=False))
    expected = {"o/r#7": [{"capability_id": "offload", "version_id": VERSION, "verdict_ref": exp}]}
    assert index == expected, f"the outcome bridge holds its own prefix: {index}"


def test_todays_ids_and_offer_keys_are_byte_identical(ledger):
    """The live ledger keys every offer `advice:<capability>:<digest>`. A change that moved today's
    bytes would make every repeated consult record a second offer for one task."""
    digest = hashlib.sha1(TASK.encode()).hexdigest()[:12]
    assert advisor.experiment_id(TASK) == "advice:" + digest
    assert advisor._record_matches(OFFER, path=ledger) == 1
    (offer,) = _offers(ledger)
    assert (offer["ref"], offer["idempotency_key"]) == (
        "advice:" + digest,
        f"advice:offload:{digest}",
    )
    assert advisor._record_matches(OFFER, path=ledger) == 0, "a repeated consult recorded twice"
