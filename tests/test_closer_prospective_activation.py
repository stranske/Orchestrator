"""Private-fixture deployment boundaries; these are not live qualification receipts."""

import json
import signal
import time
from copy import deepcopy
from pathlib import Path

import pytest
import test_closer_native_observer as native_fixtures

import closer_campaign as campaign
import closer_frontier_policy as frontier
import closer_mechanisms as mechanisms
import closer_native_observer as native
import closer_resources as resources

host = native_fixtures.host


def test_changed_canonical_wrapper_does_not_rebind_old_manifest(host):
    manifest, manifest_path, wrapper = host
    manifest_bytes = manifest_path.read_bytes()
    native.contract(manifest)
    wrapper.write_text(wrapper.read_text() + "\n# prospective wrapper change\n")
    with pytest.raises(mechanisms.MechanismError, match="wrapper pin changed"):
        native.contract(manifest)
    assert manifest_path.read_bytes() == manifest_bytes


def test_existing_native_wrapper_has_no_supported_renderer_retarget(host, tmp_path):
    manifest, _, wrapper = host
    before = wrapper.read_bytes()
    with pytest.raises(mechanisms.MechanismError, match="already has a native entry block"):
        native.render_cursor_wrapper(
            before.decode(),
            manifest_path=tmp_path / "prospective.manifest.json",
            campaign_root=tmp_path / "prospective-campaigns",
            interpreter=Path(manifest["runtime"]["python"]),
            launcher=Path(manifest["module_dir"]) / "closer_launcher.py",
        )
    assert wrapper.read_bytes() == before


def test_new_manifest_path_requires_exact_new_wrapper_entry(host, tmp_path):
    manifest, old_path, wrapper = host
    before = wrapper.read_bytes()
    native._check_wrapper_entry(manifest, old_path)
    with pytest.raises(mechanisms.MechanismError, match="exact pinned entry"):
        native._check_wrapper_entry(manifest, tmp_path / "prospective.manifest.json")
    assert wrapper.read_bytes() == before


def test_native_tick_without_observed_wrapper_entry_never_mutates_history(host):
    manifest, path, _ = host
    root = Path(manifest["mechanism_contract"]["campaign_root"])
    old = historical_unknown(root)
    before = old.path.read_bytes()
    observation = Path(manifest["mechanism_contract"]["observation_root"])
    native._write(observation / "native-active.json", {"status": "terminal"})

    def forbidden(*args, **kwargs):
        pytest.fail("unobserved scheduler reached qualification or worker launch")

    report = campaign.tick(root, path, launcher=forbidden, qualifier=forbidden)
    assert report == [{"kind": "held", "reason": "native tick requires observed wrapper entry"}]
    assert old.path.read_bytes() == before


def historical_unknown(tmp_path):
    old = campaign.Campaign(tmp_path / "old.campaign.json")
    old.begin("old", ["owner/old#1"], owner="closer", release_pin="a" * 64)

    def unknown(doc):
        doc["targets"][campaign.ROUND_UNIT] = "reconcile"
        doc["workers"] = [{"worker_id": "abcdef012345", "state": "terminal", "outcome": "unknown"}]

    old.update(unknown)
    return old


def test_prospective_campaign_requires_new_record_and_keeps_old_unknown(tmp_path):
    old = historical_unknown(tmp_path)
    before = old.path.read_bytes()
    with pytest.raises(campaign.CampaignError, match="pinned to a different release"):
        old.begin("old", ["owner/old#1"], owner="closer", release_pin="b" * 64)
    assert old.path.read_bytes() == before
    targets = ["owner/new#2"]
    descriptor = frontier.make_frontier(
        targets,
        owner="closer",
        frontier_id="prospective-1",
        bindings={targets[0]: {"revision": "new-exact-head", "action_id": "c" * 64}},
    )
    new = campaign.Campaign(tmp_path / "prospective.campaign.json")
    fresh = new.begin(
        "prospective",
        targets,
        owner="closer",
        release_pin="b" * 64,
        worker_config={"finite_frontier": descriptor},
    )
    assert fresh["worker_config"]["finite_frontier"] == descriptor
    assert fresh["release_pin"] == "b" * 64
    assert campaign.next_action(fresh)["kind"] == "launch"
    assert campaign.next_action(old.read())["kind"] == "reconcile"
    assert campaign.promotion_owner(old.read(), "b" * 64)["may_write"] is False
    assert old.path.read_bytes() == before
    assert (
        native.pre_dispatch_no_start_proof({}, {}, {"detail": "B never started; C completed"})
        is None
    )
    assert old.path.read_bytes() == before


def test_new_campaign_does_not_release_old_workspace_hold(tmp_path):
    old = historical_unknown(tmp_path)
    old_bytes = old.path.read_bytes()
    root = tmp_path / "dispatch.scoped"
    held, independent = tmp_path / "old-checkout", tmp_path / "new-checkout"
    held.mkdir()
    independent.mkdir()
    key = "worktree:" + str(held)
    with resources.reserve(
        root, [key], target="owner/old#1", owner="old", deadline=time.time() + 2
    ):
        pass
    prior = {p: p.read_bytes() for p in root.parent.rglob("intent.json")}
    with pytest.raises(resources.ResourceBlocked, match="unresolved effect"):
        with resources.reserve(
            root, [key], target="owner/new#2", owner="new", deadline=time.time() + 2
        ):
            pytest.fail("new campaign reused an unresolved historical checkout")
    with resources.reserve(
        root,
        ["worktree:" + str(independent)],
        target="owner/new#2",
        owner="new",
        deadline=time.time() + 2,
    ):
        pass
    assert all(p.read_bytes() == content for p, content in prior.items())
    assert old.path.read_bytes() == old_bytes


@pytest.mark.parametrize("changed", ["package", "policy", "mechanism"])
def test_previous_native_qualification_cannot_credit_prospective_pin(host, changed):
    manifest, _, _ = host
    root = native.contract(manifest)[3]
    mechanisms._key(root, create=True)
    nonce = "a" * 32
    directory = root / nonce
    directory.mkdir()
    native._write(directory / "startup.json", {"fixture": "startup"})
    native._write(directory / "child-journal.json", {"fixture": "handoff"})
    native._write(directory / "crash.json", {"exit": -signal.SIGKILL})
    original_binding = native.binding(manifest)
    native._write(
        directory / "child-result.json",
        {"binding": original_binding, "nonce": nonce, "no_effect": True},
    )
    names = {
        "startup": "startup.json",
        "handoff": "child-journal.json",
        "crash_restart": "crash.json",
        "result_recording": "child-result.json",
    }
    record = {
        "schema": native.OBSERVATION,
        "kind": "native-wrapper-qualification",
        "binding": original_binding,
        "passed": True,
        "desktop_qualified": False,
        "source": "owned-native-wrapper-process",
        "actor": "cursor",
        "scheduler_origin": "unobserved",
        "nonce": nonce,
        "phases": {
            phase: {"file": name, "sha256": native._hash(directory / name)}
            for phase, name in names.items()
        },
    }
    native._signed(root / "native-qualification.json", record, root)
    native._write(root / "native-active.json", {"status": "terminal"})
    assert native.qualification_problems(manifest) == []
    prior = {p: p.read_bytes() for p in root.rglob("*.json")}
    prospective = deepcopy(manifest)
    if changed == "package":
        prospective["package_digest"] = "f" * 64
    elif changed == "policy":
        # An additional policy field changes the signed identity, not the active
        # canonical policy files or their pause safeguards.
        prospective["policy"]["prospective_revision"] = 2
    else:
        prospective["mechanism_contract"]["prospective_revision"] = 2
    problems = native.qualification_problems(prospective)
    assert any("another capability or pin" in problem for problem in problems)
    assert all(p.read_bytes() == content for p, content in prior.items())
    assert (
        json.loads((root / "native-qualification.json").read_text())["record"]["binding"]
        == original_binding
    )
