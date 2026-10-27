"""Publication selection cannot own an unallocated coding continuation.

These controls issue no admission: successful None leaves the existing coding
coordinator responsible for original native terminal and registration proof.
"""

import copy

import pytest

import closer_coding_continuation as continuation
import closer_coding_publication as publication
import closer_coding_publication_io as io
import closer_correction_controller as controller


def document(state="awaiting_original_terminal", worker=None):
    key = "a" * 64
    return {
        "workers": [{"state": "terminal", "outcome": "scoped_continuation"}],
        "coding_continuations": {
            key: {
                "schema": continuation.REGISTRY_SCHEMA,
                "reference": {
                    "schema": continuation.REFERENCE_SCHEMA,
                    "name": "coding-continuation-" + key + ".json",
                    "sha256": "b" * 64,
                    "stable_key": key,
                    "effect_key": "c" * 64,
                    "request_sha256": "d" * 64,
                    "consumer_id": "coding-owner",
                },
                "state": state,
                "revision": 1 if state == "awaiting_original_terminal" else 2,
                "worker": worker,
            }
        },
    }


@pytest.mark.parametrize("outcome", ["ok", "unavailable", "scoped_continuation"])
def test_known_original_without_allocated_coding_leaves_coding_selection(outcome, monkeypatch):
    value = document()
    value["workers"][0]["outcome"] = outcome
    before = copy.deepcopy(value)
    calls = []
    monkeypatch.setattr(controller, "_Custody", lambda *_: calls.append("custody"))
    assert io._known_handoff({}, value, None) is None
    assert value == before
    assert calls == []


@pytest.mark.parametrize(
    "workers",
    [
        [],
        [{"state": "active", "outcome": "scoped_continuation"}],
        [{"state": "terminal", "outcome": "unknown"}],
        [{"state": "terminal", "outcome": "uncertain"}],
        [{"state": "submitted", "outcome": "ok"}],
    ],
)
def test_unknown_or_live_original_is_not_skipped(workers):
    value = document()
    value["workers"] = workers
    with pytest.raises(publication.PublicationHeld, match="prior worker remains live or unknown"):
        io._known_handoff({}, value, None)


@pytest.mark.parametrize("state", ["admitted", "launch_intent", "submitted", "active"])
def test_allocated_coding_still_blocks_publication(state):
    with pytest.raises(publication.PublicationHeld, match="prior worker remains live or unknown"):
        io._known_handoff({}, document(state, {"nonce": "allocated"}), None)


def test_mixed_terminal_and_awaiting_registry_remains_held():
    value = document()
    second = document("terminal", {"nonce": "known"})["coding_continuations"]["a" * 64]
    second["reference"]["stable_key"] = "e" * 64
    second["reference"]["name"] = "coding-continuation-" + "e" * 64 + ".json"
    value["coding_continuations"]["e" * 64] = second
    with pytest.raises(publication.PublicationHeld, match="prior worker remains live or unknown"):
        io._known_handoff({}, value, None)


def test_awaiting_cannot_claim_a_worker():
    with pytest.raises(continuation.ContinuationHeld, match="cannot carry a worker"):
        io._known_handoff({}, document(worker={"nonce": "not-allowed"}), None)


def test_terminal_registry_reaches_existing_authenticated_reader(monkeypatch):
    class ReadBoundary(Exception):
        pass

    calls = []

    def existing_reader(config):
        calls.append(config)
        raise ReadBoundary

    config = object()
    monkeypatch.setattr(controller, "_Custody", existing_reader)
    with pytest.raises(ReadBoundary):
        io._known_handoff({}, document("terminal", {"nonce": "known"}), config)
    assert calls == [config]
