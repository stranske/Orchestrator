"""Pure private-wrapper source contracts. No wrapper/worker process is launched."""

import copy
import hashlib
from pathlib import Path

import pytest

import closer_mechanisms as mechanisms
import closer_native_observer as native


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def case(tmp_path, monkeypatch):
    root = (tmp_path / "observation").resolve()
    root.mkdir(mode=0o700)
    campaigns = (tmp_path / "campaigns").resolve()
    campaigns.mkdir(mode=0o700)
    manifest_path = (tmp_path / "metadata.manifest.json").resolve()
    python = (tmp_path / "pinned-python").resolve()
    python.write_bytes(b"not executed")
    python.chmod(0o500)
    launcher = str((tmp_path / "closer_launcher.py").resolve())
    wrapper = root / "private-test-wrapper.sh"
    wrapper.write_text(
        native.private_test_wrapper_text(str(manifest_path), str(campaigns), str(python), launcher)
    )
    wrapper.chmod(0o500)
    value = {
        "schema": mechanisms.CONTRACT,
        "mechanism": mechanisms.NATIVE,
        "actor": "cursor",
        "private_test": copy.deepcopy(native.PRIVATE_TEST),
        "wrapper": {"path": str(wrapper), "sha256": _sha(wrapper)},
        "interpreter": {"path": str(python), "sha256": _sha(python)},
        "observation_root": str(root),
        "campaign_root": str(campaigns),
        "capacity_publication": {
            "schema": "native-scoped-capacity/v2",
            "owner": "test-owner",
            "execution_policy": "finite-frontier/v2",
            "request_timeout_seconds": 35,
            "freshness_seconds": 120,
            "profile_ids": [],
            "bounded_trial_agents": [],
        },
    }
    manifest = {
        "mechanism_contract": value,
        "runtime": {"python": str(python)},
        "modules": {"closer_native_observer.py": "a" * 64},
        "command": [str(python), "closer_campaign.py", "tick", "--manifest", str(manifest_path)],
        "launcher": {"path": launcher},
        "policy": {"codex_metadata_qualification": {}, "codex_initial_consumer": {}},
    }
    monkeypatch.setattr(native, "policy_problem", lambda _: None)
    return manifest, wrapper, root, manifest_path


def test_exact_private_metadata_source_uses_existing_entry_contract(case):
    manifest, wrapper, root, _ = case
    _, actual, _, observed = native.contract(manifest)
    assert actual == wrapper and observed == root
    assert wrapper.stat().st_mode & 0o222 == 0
    assert wrapper.read_text().count(native.MARKER) == 1
    assert wrapper.read_text().endswith("exit 78\n")


@pytest.mark.parametrize(
    "damage",
    [
        "foreign_path",
        "symlink",
        "writable",
        "root_writable",
        "pin",
        "purpose",
        "extra_purpose",
        "missing_policy",
        "index",
        "model_capacity",
        "source",
        "entry",
        "manifest_path",
    ],
)
def test_private_wrapper_refuses_foreign_weak_or_changed_source(case, damage, tmp_path):
    manifest, wrapper, root, _path = case
    value = manifest["mechanism_contract"]
    if damage == "foreign_path":
        foreign = tmp_path / "foreign.sh"
        foreign.write_bytes(wrapper.read_bytes())
        foreign.chmod(0o500)
        value["wrapper"]["path"] = str(foreign.resolve())
    elif damage == "symlink":
        foreign = root / "foreign.sh"
        wrapper.rename(foreign)
        wrapper.symlink_to(foreign)
    elif damage == "writable":
        wrapper.chmod(0o700)
    elif damage == "root_writable":
        root.chmod(0o755)
    elif damage == "pin":
        value["wrapper"]["sha256"] = "b" * 64
    elif damage == "purpose":
        value["private_test"]["purpose"] = "production-closure"
    elif damage == "extra_purpose":
        value["private_test"]["allow"] = True
    elif damage == "missing_policy":
        manifest["policy"].pop("codex_initial_consumer")
    elif damage == "index":
        manifest["policy"]["correction_controller_installation"] = {}
    elif damage == "model_capacity":
        value["capacity_publication"]["profile_ids"] = ["codex-6.1-sol-high"]
    elif damage in ("source", "entry"):
        wrapper.chmod(0o700)
        text = wrapper.read_text()
        text = (
            text + "arbitrary_provider\n"
            if damage == "source"
            else text.replace("native-entry", "native-status")
        )
        wrapper.write_text(text)
        wrapper.chmod(0o500)
        value["wrapper"]["sha256"] = _sha(wrapper)
    else:
        manifest["command"][-1] = str(tmp_path / "different-manifest.json")
    with pytest.raises((mechanisms.MechanismError, OSError, ValueError)):
        native.contract(manifest)


def test_unselected_private_path_is_not_a_new_production_allowlist(case):
    manifest, *_ = case
    manifest["mechanism_contract"].pop("private_test")
    with pytest.raises(mechanisms.MechanismError, match="native wrapper/actor is not supported"):
        native.contract(manifest)


def test_private_source_cannot_authorize_any_worker_even_with_other_flags(case):
    manifest, *_ = case
    with pytest.raises(mechanisms.MechanismError, match="cannot authorize a worker"):
        native.authorize_worker(manifest, ["synthetic-not-executed"])


def test_canonical_production_path_is_unchanged(case, monkeypatch):
    manifest, _wrapper, _root, _path = case
    manifest["mechanism_contract"].pop("private_test")
    canonical = Path.home() / ".codex/bin/cursor-lane.sh"
    # Pure DATA port: don't read/write or invoke the installed host wrapper.
    manifest["mechanism_contract"]["wrapper"] = {"path": str(canonical), "sha256": "f" * 64}
    original_hash = native._hash
    monkeypatch.setattr(
        native, "_hash", lambda p: "f" * 64 if Path(p) == canonical else original_hash(p)
    )
    assert native.contract(manifest)[1] == canonical


def test_private_admission_cannot_enter_ordinary_business_effects(case):
    manifest, *_ = case
    with pytest.raises(mechanisms.MechanismError, match="cannot authorize business effects"):
        native.validate_admission(manifest, {}, for_effects=True)


def test_test_flag_cannot_reclassify_ordinary_production_admission(case):
    manifest, *_ = case
    manifest["mechanism_contract"].pop("private_test")
    with pytest.raises(mechanisms.MechanismError, match="requires private TEST source"):
        native.validate_admission(manifest, {}, for_effects=True, for_test=True)


@pytest.mark.parametrize("damage", ["foreign_scope", "changed_pin", "world_writable", "alias"])
def test_private_assignment_is_closed_owned_and_source_bound(case, damage):
    import json

    manifest, _, root, _ = case
    manifest["policy"]["codex_initial_consumer"]["target"] = "public/startup#1"
    assignment = root / "private-test-assignment.json"
    doc = {
        "schema": "closer-private-native-test-assignment/v1",
        "purpose": "codex-account-metadata-only",
        "targets": [{"target": "public/startup#1"}],
    }
    assignment.write_text(json.dumps(doc))
    assignment.chmod(0o600)
    manifest["mechanism_contract"]["private_test_assignment_sha256"] = _sha(assignment)
    assert native.private_test_assignment_source(manifest) == assignment
    if damage == "foreign_scope":
        doc["targets"].append({"target": "foreign/repo#2"})
        assignment.write_text(json.dumps(doc))
        manifest["mechanism_contract"]["private_test_assignment_sha256"] = _sha(assignment)
    elif damage == "changed_pin":
        manifest["mechanism_contract"]["private_test_assignment_sha256"] = "f" * 64
    elif damage == "world_writable":
        assignment.chmod(0o666)
    else:
        other = root / "other-assignment.json"
        assignment.rename(other)
        assignment.symlink_to(other)
    with pytest.raises((mechanisms.MechanismError, OSError, ValueError)):
        native.private_test_assignment_source(manifest)


def test_private_wrapper_keeps_existing_host_opt_in_requirement(case, monkeypatch):
    manifest, _, _, path = case
    enabled = Path.home() / ".codex/handoff/closer-native-handoff.enabled"
    original = Path.is_file
    monkeypatch.setattr(Path, "is_file", lambda p: False if p == enabled else original(p))
    with pytest.raises(mechanisms.MechanismError, match="opt-in is absent"):
        native._check_wrapper_entry(manifest, path)
