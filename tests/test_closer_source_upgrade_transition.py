"""Public transition entrypoint contracts.

The old signed cohort is an external source validator boundary; these tests
ensure callers cannot accidentally use the historical variadic apply surface.
"""

from __future__ import annotations

import inspect

import closer_native_source_upgrade as upgrade


def test_public_transition_has_exact_non_variadic_api():
    expected = (
        "old_manifest", "old_state", "worker_id", "new_manifest", "new_state",
        "restore_receipt", "new_worker_config", "frontier", "new_release_journal",
    )
    assert tuple(inspect.signature(upgrade.plan).parameters) == expected
    assert tuple(inspect.signature(upgrade.apply).parameters) == (*expected, "expected_plan_sha256")
    assert tuple(inspect.signature(upgrade.prepare_held).parameters) == (
        "seal_path", "confirm_plan_sha256"
    )


def test_sealed_plan_digest_detects_full_body_tamper():
    plan = {"schema": upgrade.SCHEMA, "old": {"result_sha256": "a" * 64}, "new": {"state": "/x"}}
    digest = upgrade._plan_sha(plan)
    sealed = {**plan, "plan_sha256": digest, "stage": "sealed_pre_cas", "at": 1.0}
    assert upgrade._plan_sha(sealed) == digest
    sealed["old"]["result_sha256"] = "b" * 64
    assert upgrade._plan_sha(sealed) != digest
