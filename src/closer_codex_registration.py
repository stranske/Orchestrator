"""Metadata-only original queue caller; emits a prospective registration, not authority.

This branch runs under the existing authenticated native wrapper tick. It never
creates an account, changes the installed controller config, or starts a task.
The source owner must separately provision and qualify the subsequent release.
"""

from __future__ import annotations

import copy
import hashlib
import inspect
import os
from pathlib import Path

import closer_codex_metadata as metadata
import closer_correction_controller as ctl

POLICY = "codex_initial_consumer"
SCHEMA = "closer-codex-initial-consumer/v1"
_SOURCE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


class RegistrationHeld(ValueError):
    pass


def need(value, reason):
    if not value:
        raise RegistrationHeld(reason)


def selected_plan(manifest):
    """Pure closed DATA validation; only the original caller authenticates policy."""
    plan = (manifest.get("policy") or {}).get(POLICY)
    if plan is None:
        return None
    need(
        type(plan) is dict and set(plan) == {"schema", "target", "consumer_id", "registration"},
        "initial_consumer_policy_fields",
    )
    need(
        plan["schema"] == SCHEMA
        and type(plan["consumer_id"]) is str
        and 0 < len(plan["consumer_id"]) <= 128
        and type(plan["target"]) is str,
        "initial_consumer_policy_identity",
    )
    import closer_resources

    need(
        closer_resources.canonical_target(plan["target"]) == plan["target"],
        "initial_consumer_target",
    )
    registration = plan["registration"]
    need(
        type(registration) is dict
        and "account_sha256" not in registration
        and registration.get("agent") == "codex"
        and registration.get("runner") == "existing-local-controller"
        and registration.get("transport_binding") == "codex-source-owned-appserver-stdio/v1"
        and registration.get("reader_entrypoint")
        == "closer_correction_controller.NativeConsumerReader"
        and type(registration.get("native_bootstrap")) is dict,
        "initial_consumer_template",
    )
    import closer_coding_session_v2 as sessions
    import execution_profiles

    profile = execution_profiles.get_profile(registration.get("profile_id"))
    need(
        profile is not None
        and profile.get("agent") == "codex"
        and registration.get("profile_sha256") == ctl.source_digest(profile)
        and registration.get("reader_source_sha256") == ctl._LOADED_SOURCE_SHA256,
        "initial_consumer_source_profile",
    )
    draft = sessions.prepare_admission_policy({plan["consumer_id"]: registration})
    need(draft.snapshot_utf8 is not None, "initial_consumer_v2_policy")
    return copy.deepcopy(plan)


def operation_observation(manifest, target, manifest_sha256):
    """The prospective revision/binding also freezes the full native frontier."""
    plan = selected_plan(manifest)
    need(plan is not None and target == plan["target"], "initial_consumer_target")
    spec = manifest["policy"].get(metadata.POLICY)
    need(type(spec) is dict, "metadata_selection_missing")
    need(
        type(manifest_sha256) is str
        and len(manifest_sha256) == 64
        and all(c in "0123456789abcdef" for c in manifest_sha256),
        "metadata_manifest_digest",
    )
    binding = {
        "runtime_manifest_sha256": manifest_sha256,
        metadata.POLICY + "_sha256": ctl.digest(spec),
        POLICY + "_sha256": ctl.digest(plan),
    }
    return {
        "target": target,
        "action": "disposition",
        "eligible": True,
        "revision": "codex-initial-account:" + ctl.digest([spec, plan]),
        "operation_binding": binding,
        "reason": "source-selected metadata-only first account observation",
        "next_trigger": None,
        "evidence": {
            "purpose": SCHEMA,
            "model_generation": False,
            "remote_mutations": False,
        },
    }


def execute_original(binding, manifest, manifest_sha256, target, action, observation, operation):
    """The real queue and read-only original writer precede every native child."""
    import closer_queue as queue

    need(
        action == "disposition" and queue._currently_invoking(target, operation),
        "original_metadata_callback_required",
    )
    expected = operation_observation(manifest, target, manifest_sha256)
    need(
        observation["revision"] == expected["revision"]
        and operation["binding"] == expected["operation_binding"],
        "original_metadata_selection_changed",
    )
    import closer_codex_metadata_admission as admitted

    with admitted.metadata_scope(binding, operation) as guard:
        reference = admitted.qualify_admitted_callback(guard)
        handle = admitted.read_admitted_account(guard, reference)
        proposal = admitted.retain_admitted_initial_registration(guard, handle)
        return {
            "status": "completed",
            "purpose": SCHEMA,
            "registration_proposal": proposal,
            "metadata_reference": reference,
            "target_complete": False,
            "execution_authorized": False,
            "registration_installed": False,
        }


def run_from_native_admission(campaign, document, manifest, manifest_path, admission):
    """One existing native tick branch before route selection or worker launch."""
    import closer_campaign as campaign_source
    import closer_frontier_policy as finite
    import closer_native_observer as native
    import closer_queue as queue
    import closer_runtime_binding as runtime
    import closer_source_installation as source

    frame = inspect.currentframe().f_back
    need(
        frame.f_code is campaign_source.tick.__code__
        and frame.f_globals is vars(campaign_source)
        and frame.f_locals.get("campaign") is campaign
        and frame.f_locals.get("doc") is document
        and frame.f_locals.get("manifest") is manifest
        and frame.f_locals.get("admission") is admission,
        "original_native_tick_required",
    )
    plan = selected_plan(manifest)
    need(
        plan is not None and native.entry_active(manifest),
        "native_metadata_policy_not_selected",
    )
    native.validate_admission(
        manifest,
        admission,
        for_effects=True,
        for_test="private_test" in manifest["mechanism_contract"],
    )
    need(
        admission["campaign"]["state"] == str(campaign.path)
        and admission["runtime_manifest"] == str(Path(manifest_path).resolve()),
        "native_metadata_campaign_changed",
    )
    previous = os.environ.get("CLOSER_CAPACITY_REPORT")
    os.environ["CLOSER_CAPACITY_REPORT"] = admission["report"]["path"]
    try:
        binding = runtime.binding_from_manifest(manifest_path)
        need(binding is not None, "qualified_native_metadata_binding_missing")
        origin = runtime._factory_manifest_origin(binding)
        need(origin is not None, "native_metadata_manifest_origin_missing")
        physical, raw = source._physical_json(origin.path, limit=16 * 1024 * 1024)
        need(
            physical == manifest and hashlib.sha256(raw).hexdigest() == origin.sha256,
            "native_metadata_manifest_changed",
        )
        need(
            Path(__file__).resolve() == Path(manifest["module_dir"]) / Path(__file__).name
            and manifest["modules"].get(Path(__file__).name) == _SOURCE_SHA256
            and hashlib.sha256(Path(__file__).read_bytes()).hexdigest() == _SOURCE_SHA256,
            "initial_registration_source_not_installed",
        )
        target = plan["target"]
        observation = operation_observation(manifest, target, origin.sha256)
        frontier = admission["finite_frontier"]
        need(
            target in frontier["targets"]
            and frontier["bindings"][target]
            == {
                "revision": observation["revision"],
                "action_id": finite.action_identity(
                    target,
                    observation["revision"],
                    "disposition",
                    observation["operation_binding"],
                ),
            },
            "native_metadata_frontier_not_selected",
        )

        completed = None

        def observe(selected):
            need(selected == target, "native_metadata_target_changed")
            if completed is not None:
                return {
                    **copy.deepcopy(observation),
                    "action": "complete",
                    "eligible": False,
                    "reason": "this metadata-only purpose produced a protected proposal",
                }
            return copy.deepcopy(observation)

        def execute(selected, action, observed, operation):
            nonlocal completed
            completed = execute_original(
                binding, manifest, origin.sha256, selected, action, observed, operation
            )
            return completed

        queue_path = str(campaign.path) + ".codex-metadata-queue.json"
        receipt = queue.run_round(
            queue_path,
            [target],
            observe,
            execute,
            owner=document["owner"],
            deadline=None,
            receipt_reserve_seconds=0,
            max_workers=1,
            max_passes=1,
            max_operation_retries=0,
            finite_frontier=frontier,
            frontier_target=target,
            request_timeout_seconds=min(35.0, admission["request_timeout_seconds"]),
        )
        public_test_result = None
        if completed is not None and "codex_public_coding_test" in manifest.get("policy", {}):
            import closer_codex_public_canary

            public_test_result = closer_codex_public_canary.run_from_native_admission(
                binding, manifest, origin.sha256, admission, campaign, document
            )
        return {
            "public_coding_test": public_test_result,
            "kind": "codex_initial_account_qualification",
            "purpose": SCHEMA,
            "queue": receipt,
            "metadata_result": copy.deepcopy(completed),
            "metadata_model_generations": 0,
            "repository_mutations": 0,
            "registration_installed": False,
            "execution_authorized": False,
        }
    finally:
        if previous is None:
            os.environ.pop("CLOSER_CAPACITY_REPORT", None)
        else:
            os.environ["CLOSER_CAPACITY_REPORT"] = previous
