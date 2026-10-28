"""Source-resident pin consumption; native documents here are inert fixtures."""

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import closer_corrective_verifier_readback as readback

FACTS = json.loads(
    (
        Path(__file__).parent
        / "fixtures/corrective_verifier_deployed_source/canonical-source-fingerprints.json"
    ).read_text()
)


@pytest.fixture
def source_world(monkeypatch):
    repo, caller, helper = (
        FACTS[k] for k in ("caller_repository", "observed_caller_commit", "effective_helper_commit")
    )
    docs = {
        f"repos/{repo}": {"id": FACTS["caller_repository_id"], "default_branch": "main"},
        f"repos/{repo}/branches/main": {"commit": {"sha": caller}},
        f"repos/{readback.CENTRAL}/branches/main": {"commit": {"sha": helper}},
    }
    hashes, calls = copy.deepcopy(FACTS["normative_sources"]), []

    def api(endpoint, *, method="GET"):
        assert method == "GET"
        calls.append((method, endpoint))
        return copy.deepcopy(docs[endpoint])

    def content(backend, repository, path, ref):
        # This fixture tests pin consumption, not GitHub/native acquisition.
        # Existing readback tests exercise strict base64/blob/inventory binding.
        calls.append(("content", repository, path, ref))
        if repository == repo and path == readback.WORKFLOW:
            return hashes.get("caller", FACTS["caller_sha256"])
        assert repository == readback.CENTRAL and path in readback.SOURCE_FILES
        return hashes[path]

    monkeypatch.setattr(readback, "_content", content)
    return SimpleNamespace(
        backend=SimpleNamespace(gh=SimpleNamespace(api=api)),
        docs=docs,
        hashes=hashes,
        calls=calls,
        repo=repo,
        record={"pr": {"repository": repo, "repository_id": FACTS["caller_repository_id"]}},
    )


def test_original_c18_pin_is_preserved_not_relabelled_observed_run():
    assert len(readback.DEPLOYED_SOURCE_PINS) == 3
    pin = readback.DEPLOYED_SOURCE_PINS[0]
    assert pin == {
        "caller_sha256": FACTS["caller_sha256"],
        "central": FACTS["normative_sources"],
        "helper_commit": FACTS["effective_helper_commit"],
        "reusable_sha256": FACTS["normative_sources"][readback.CALLEE],
    }
    assert FACTS["observed_helper_commit"] != pin["helper_commit"]
    assert FACTS["normative_sources"] == FACTS["observed_normative_sources"]
    assert set(pin["central"]) == set(readback.SOURCE_FILES)


def test_unchanged_actual_caller_and_effective_helper_support_prepost_only(source_world):
    w = source_world
    pin = readback.require_deployed(w.backend, w.record, "main")
    assert pin == readback.DEPLOYED_SOURCE_PINS[0]
    assert all(call[0] in {"GET", "content"} for call in w.calls)
    assert not any("actions/" in str(call) for call in w.calls)
    assert set(c[1] for c in w.calls if c[0] == "content") == {w.repo, readback.CENTRAL}


def test_unchanged_caller_file_can_advance_repo_commit(source_world):
    w = source_world
    w.docs[f"repos/{w.repo}/branches/main"]["commit"]["sha"] = "a" * 40
    assert (
        readback.require_deployed(w.backend, w.record, "main") == readback.DEPLOYED_SOURCE_PINS[0]
    )


@pytest.mark.parametrize("path", readback.SOURCE_FILES)
def test_changed_normative_source_never_reuses_deployed_pin(source_world, path):
    w = source_world
    w.hashes[path] = "0" * 64
    with pytest.raises(readback.ReadbackHeld, match="source is unsupported"):
        readback.require_deployed(w.backend, w.record, "main")


@pytest.mark.parametrize(
    "change",
    [
        "caller",
        "repository_id",
        "default_branch",
        "helper_movement",
        "unregistered_historical_helper",
        "forged_record_override",
    ],
)
def test_forged_or_changed_native_source_identity_holds(source_world, change):
    w = source_world
    if change == "caller":
        w.hashes["caller"] = "0" * 64
    elif change == "repository_id":
        w.record["pr"]["repository_id"] += 1
    elif change == "default_branch":
        w.docs[f"repos/{w.repo}"]["default_branch"] = "other"
    elif change == "unregistered_historical_helper":
        w.docs[f"repos/{readback.CENTRAL}/branches/main"]["commit"]["sha"] = "e" * 40
    else:
        w.docs[f"repos/{readback.CENTRAL}/branches/main"]["commit"]["sha"] = "b" * 40
        if change == "forged_record_override":
            w.record["deployed_source_pin"] = {
                **readback.DEPLOYED_SOURCE_PINS[0],
                "helper_commit": "b" * 40,
            }
    with pytest.raises(readback.ReadbackHeld):
        readback.require_deployed(w.backend, w.record, "main")
