"""Exact current and retained native source versions; no execution authority."""

import copy
import json
from pathlib import Path

import pytest
from test_corrective_verifier_deployed_pins import source_world as original_source_world

import closer_corrective_verifier_readback as readback

FACTS = json.loads(
    (
        Path(__file__).parent
        / "fixtures/corrective_verifier_deployed_source/current-and-retained-fingerprints.json"
    ).read_text()
)


@pytest.fixture
def source_world(monkeypatch):
    return original_source_world.__wrapped__(monkeypatch)


def select(world, pin):
    world.docs[f"repos/{readback.CENTRAL}/branches/main"]["commit"]["sha"] = pin["helper_commit"]
    world.hashes.update(copy.deepcopy(pin["central"]))


@pytest.mark.parametrize("index", [0, 1, 2, 3])
def test_only_four_explicit_native_versions_are_registered(source_world, index):
    assert list(readback.DEPLOYED_SOURCE_PINS) == FACTS["pins"]
    assert len({pin["helper_commit"] for pin in readback.DEPLOYED_SOURCE_PINS}) == 4
    pin = FACTS["pins"][index]
    select(source_world, pin)
    assert readback.require_deployed(source_world.backend, source_world.record, "main") == pin


@pytest.mark.parametrize("path", readback.SOURCE_FILES)
def test_current_helper_changed_source_still_holds(source_world, path):
    select(source_world, FACTS["pins"][-1])
    source_world.hashes[path] = "0" * 64
    with pytest.raises(readback.ReadbackHeld, match="source is unsupported"):
        readback.require_deployed(source_world.backend, source_world.record, "main")


def test_future_helper_with_same_content_is_not_inferred(source_world):
    select(source_world, FACTS["pins"][-1])
    source_world.docs[f"repos/{readback.CENTRAL}/branches/main"]["commit"]["sha"] = "f" * 40
    with pytest.raises(readback.ReadbackHeld, match="source is unsupported"):
        readback.require_deployed(source_world.backend, source_world.record, "main")


def test_historical_375_cannot_use_current_collector_fingerprint(source_world):
    select(source_world, FACTS["pins"][1])
    path = ".github/scripts/agents_verifier_context.js"
    source_world.hashes[path] = FACTS["pins"][-1]["central"][path]
    with pytest.raises(readback.ReadbackHeld, match="source is unsupported"):
        readback.require_deployed(source_world.backend, source_world.record, "main")


def test_without_current_registration_current_helper_remains_held(source_world, monkeypatch):
    select(source_world, FACTS["pins"][-1])
    monkeypatch.setattr(readback, "DEPLOYED_SOURCE_PINS", tuple(FACTS["pins"][:-1]))
    with pytest.raises(readback.ReadbackHeld, match="source is unsupported"):
        readback.require_deployed(source_world.backend, source_world.record, "main")
