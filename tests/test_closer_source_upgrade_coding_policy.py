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
