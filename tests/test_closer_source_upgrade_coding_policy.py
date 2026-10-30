"""Static source-upgrade coding capability projection; no launch or custody writes."""

from __future__ import annotations

import copy
import hashlib

import pytest

import closer_coding_session_runtime as coding_runtime
import closer_coding_session_v2 as coding_session
import closer_correction_controller as controller
import closer_native_source_upgrade as upgrade
import execution_profiles


def _budget():
    return {
        "schema": coding_session.BUDGET_SCHEMA,
        "max_runtime_ms": 10_000,
        "max_cleanup_ms": 1_000,
        "max_prompt_utf8_bytes": 20_000,
        "max_captured_output_bytes": 30_000,
        "max_retained_final_utf8_bytes": 12_000,
        "max_retained_receipt_bytes": 40_000,
        "max_outer_submissions": 1,
        "native_model_attempts": "UNKNOWN",
        "native_input_tokens": "UNKNOWN",
        "native_output_tokens": "UNKNOWN",
    }


def _publication(consumer_id):
    return {
        "schema": "closer-owned-pr-publication-policy/v1",
        "mode": "owned_existing_pr",
        "consumer_id": consumer_id,
        "git_executable": {"path": "/source-owned/git", "sha256": "a" * 64},
        "allowed_path_prefixes": ["tests/fixtures/closer_real_csv_canary/v1"],
        "trusted_context_sha256": "b" * 64,
        "max_files": 2,
        "max_file_bytes": 4096,
        "max_total_bytes": 8192,
        "max_ack_bytes": 16_384,
        "request_timeout_seconds": 120,
        "max_native_io_seconds": 15,
        "max_push_submissions": 1,
    }


def _consumer(consumer_id="test-csv-followup988-codex"):
    profile = execution_profiles.get_profile(coding_session.PROFILE)
    profile_sha = controller.source_digest(profile)
    budget = _budget()
    return {
        "runner": "existing-local-controller",
        "agent": "codex",
        "owner_login": "codex",
        "profile_id": coding_session.PROFILE,
        "profile_sha256": profile_sha,
        coding_session.POLICY_FIELD: {
            "schema": coding_session.POLICY_SCHEMA,
            "consumer_id": consumer_id,
            "agent": "codex",
            "profile_id": coding_session.PROFILE,
            "profile_sha256": profile_sha,
            "budget": budget,
        },
        coding_runtime.CURRENT_FIELD: budget,
        "publication_v1": _publication(consumer_id),
        "native_bootstrap": {
            "private_root": "/private/tmp/old-private",
            "confinement": {
                "helper_path": "/private/tmp/old-probe.py",
                "helper_sha256": "c" * 64,
            },
        },
    }


def _policy(consumers):
    return {
        "correction_controller_installation": {
            "config": {
                "source_sha": "a" * 40,
                "state_path": "/private/tmp/old.campaign.json",
                "native_transport_source_sha256": "b" * 64,
                "consumers": consumers,
            }
        }
    }


def _ownership_budget_consumer():
    consumer_id = upgrade.REVIEWED_OWNERSHIP_BUDGET_CONSUMER_ID
    consumer = _consumer(consumer_id)
    consumer.pop("publication_v1")
    consumer["publication_v2"] = {
        "schema": "closer-owned-pr-publication-policy/v2",
        "mode": "owned_existing_pr",
        "consumer_id": consumer_id,
        "git_executable": {"path": "/source-owned/git", "sha256": "a" * 64},
        "allowed_path_prefixes": list(upgrade.REVIEWED_OWNERSHIP_BUDGET_ALLOWED_PATHS),
        "trusted_context_sha256": upgrade.REVIEWED_OWNERSHIP_BUDGET_TRUSTED_CONTEXT_SHA256,
        "max_files": 2,
        "max_file_bytes": 4096,
        "max_total_bytes": 8192,
        "max_ack_bytes": 16_384,
        "request_timeout_seconds": 120,
        "max_native_io_seconds": 15,
        "max_push_submissions": 1,
        "baseline_limits": {
            "schema": "closer-publication-baseline-limits/v1",
            "max_files": 4096,
            "max_file_bytes": 80 * 1024 * 1024,
            "max_total_bytes": 128 * 1024 * 1024,
            "max_inventory_bytes": 1024 * 1024,
        },
    }
    return consumer


def _ownership_budget_policy():
    policy = _policy({upgrade.REVIEWED_OWNERSHIP_BUDGET_CONSUMER_ID: _ownership_budget_consumer()})
    config = policy["correction_controller_installation"]["config"]
    config.update(
        source_sha=upgrade.REVIEWED_OWNERSHIP_BUDGET_CANDIDATE,
        repository=upgrade.REVIEWED_OWNERSHIP_BUDGET_REPOSITORY,
        repository_id=upgrade.REVIEWED_OWNERSHIP_BUDGET_REPOSITORY_ID,
        writer_id=upgrade.REVIEWED_OWNERSHIP_BUDGET_WRITER_ID,
        writer_login=upgrade.REVIEWED_OWNERSHIP_BUDGET_WRITER_LOGIN,
        writer_type=upgrade.REVIEWED_OWNERSHIP_BUDGET_WRITER_TYPE,
    )
    return policy


def test_derives_only_the_unique_valid_source_owned_coding_consumer():
    policy = _policy({"test-csv-followup988-codex": _consumer()})

    assert upgrade._derived_coding_capability(policy) == {
        "schema": "closer-coding-worker-capability/v1",
        "consumer_id": "test-csv-followup988-codex",
    }


@pytest.mark.parametrize("damage", ["no_publication", "wrong_runner", "second_consumer"])
def test_no_capability_is_fabricated_when_factory_prerequisites_are_incomplete(damage):
    consumers = {"test-csv-followup988-codex": _consumer()}
    if damage == "no_publication":
        consumers["test-csv-followup988-codex"].pop("publication_v1")
    elif damage == "wrong_runner":
        consumers["test-csv-followup988-codex"]["runner"] = "untrusted"
    else:
        consumers["another-codex-owner"] = _consumer("another-codex-owner")

    assert upgrade._derived_coding_capability(_policy(consumers)) is None


def test_rebased_policy_allows_only_the_derived_static_capability(tmp_path):
    module_dir = tmp_path / "runtime"
    module_dir.mkdir()
    helper = module_dir / "closer_codex_confinement_probe.py"
    helper.write_text("# probe\n")
    old_policy = _policy({"test-csv-followup988-codex": _consumer()})
    new_state = tmp_path / "campaigns" / "fresh.campaign.json"
    new_state.parent.mkdir()
    expected = copy.deepcopy(old_policy)
    config = expected["correction_controller_installation"]["config"]
    config["source_sha"] = "d" * 40
    config["state_path"] = str(new_state)
    config["native_transport_source_sha256"] = "e" * 64
    bootstrap = config["consumers"]["test-csv-followup988-codex"]["native_bootstrap"]
    bootstrap["private_root"] = str(new_state.parent.parent / "controller-private")
    bootstrap["confinement"] = {
        "helper_path": str(helper),
        "helper_sha256": hashlib.sha256(helper.read_bytes()).hexdigest(),
    }
    expected["correction_coding_worker"] = {
        "schema": "closer-coding-worker-capability/v1",
        "consumer_id": "test-csv-followup988-codex",
    }
    new = {
        "candidate_sha": "d" * 40,
        "module_dir": str(module_dir),
        "modules": {
            "closer_correction_native_effects.py": "e" * 64,
            helper.name: hashlib.sha256(helper.read_bytes()).hexdigest(),
        },
        "policy": expected,
    }

    assert upgrade._rebased_policy({"policy": old_policy}, new, new_state) == expected
    changed = copy.deepcopy(new)
    changed["policy"]["correction_coding_worker"]["consumer_id"] = "different"
    with pytest.raises(upgrade.SourceUpgradeHeld, match="outside lawful rebase"):
        upgrade._rebased_policy({"policy": old_policy}, changed, new_state)


def test_ownership_budget_rebinds_only_reviewed_csv_consumer_to_controller_writer():
    policy = _ownership_budget_policy()

    rebound = upgrade._ownership_budget_rebound_coding_policy(policy)

    original = policy["correction_controller_installation"]["config"]["consumers"]
    projected = rebound["correction_controller_installation"]["config"]["consumers"]
    consumer_id = upgrade.REVIEWED_OWNERSHIP_BUDGET_CONSUMER_ID
    assert original[consumer_id]["owner_login"] == "codex"
    assert projected[consumer_id]["owner_login"] == "stranske"
    assert {
        key: value for key, value in projected[consumer_id].items() if key != "owner_login"
    } == {key: value for key, value in original[consumer_id].items() if key != "owner_login"}


def test_rebased_policy_uses_same_ownership_budget_projection_as_preparer(tmp_path):
    module_dir = tmp_path / "runtime"
    module_dir.mkdir()
    helper = module_dir / "closer_codex_confinement_probe.py"
    helper.write_text("# probe\n")
    old_policy = _ownership_budget_policy()
    new_state = tmp_path / "campaigns" / "fresh.campaign.json"
    new_state.parent.mkdir()
    expected = upgrade._ownership_budget_rebound_coding_policy(old_policy)
    config = expected["correction_controller_installation"]["config"]
    config["source_sha"] = "d" * 40
    config["state_path"] = str(new_state)
    config["native_transport_source_sha256"] = "e" * 64
    bootstrap = config["consumers"][upgrade.REVIEWED_OWNERSHIP_BUDGET_CONSUMER_ID][
        "native_bootstrap"
    ]
    bootstrap["private_root"] = str(new_state.parent.parent / "controller-private")
    bootstrap["confinement"] = {
        "helper_path": str(helper),
        "helper_sha256": hashlib.sha256(helper.read_bytes()).hexdigest(),
    }
    expected["correction_coding_worker"] = {
        "schema": "closer-coding-worker-capability/v1",
        "consumer_id": upgrade.REVIEWED_OWNERSHIP_BUDGET_CONSUMER_ID,
    }
    new = {
        "candidate_sha": "d" * 40,
        "module_dir": str(module_dir),
        "modules": {
            "closer_correction_native_effects.py": "e" * 64,
            helper.name: hashlib.sha256(helper.read_bytes()).hexdigest(),
        },
        "policy": expected,
    }

    assert upgrade._rebased_policy({"policy": old_policy}, new, new_state) == expected


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("repository", "stranske/other", "GitHub identity"),
        ("repository_id", 1, "GitHub identity"),
        ("writer_id", 1, "GitHub identity"),
        ("writer_login", "other", "GitHub identity"),
        ("writer_type", "Bot", "GitHub identity"),
    ],
)
def test_ownership_budget_rebind_rejects_different_controller_identity(field, value, message):
    policy = _ownership_budget_policy()
    policy["correction_controller_installation"]["config"][field] = value

    with pytest.raises(upgrade.SourceUpgradeHeld, match=message):
        upgrade._ownership_budget_rebound_coding_policy(policy)


@pytest.mark.parametrize("damage", ["consumer", "owner", "paths", "profile", "context"])
def test_ownership_budget_rebind_rejects_other_coding_scope(damage):
    policy = _ownership_budget_policy()
    config = policy["correction_controller_installation"]["config"]
    consumer = config["consumers"][upgrade.REVIEWED_OWNERSHIP_BUDGET_CONSUMER_ID]
    if damage == "consumer":
        config["consumers"]["another-codex-owner"] = copy.deepcopy(consumer)
    elif damage == "owner":
        consumer["owner_login"] = "stranske"
    elif damage == "paths":
        consumer["publication_v2"]["allowed_path_prefixes"] = ["tests/other.py"]
    elif damage == "context":
        consumer["publication_v2"]["trusted_context_sha256"] = "a" * 64
    else:
        consumer["coding_session_v2"]["profile_id"] = "other-profile"

    with pytest.raises(upgrade.SourceUpgradeHeld):
        upgrade._ownership_budget_rebound_coding_policy(policy)


def test_ownership_budget_rebind_does_not_apply_to_other_predecessors():
    policy = _ownership_budget_policy()
    config = policy["correction_controller_installation"]["config"]
    config["source_sha"] = "a" * 40

    assert upgrade._ownership_budget_rebound_coding_policy(policy) == policy
