"""Real profile registry allocation, without a fixed provider or effort affinity."""

import pytest

import closer_role_qualification as roles
import execution_profiles


@pytest.fixture(autouse=True)
def synthetic_quality_boundary(monkeypatch):
    # This file tests neutral allocation, not the reviewed production cohort.
    # Its existing runtime predicate is synthetic, so quality scope is explicit
    # fixture data too. Real committed-policy causality is tested separately.
    import closer_role_selection as selection

    subjects = {
        key: {
            "profile_id": key,
            "identity": {
                name: profile[name]
                for name in ("agent", "provider", "requested_model", "reasoning_effort")
            },
            "quality_status": "guarded_eligible",
            "runtime_enrollment_granted": False,
            "limits": [],
            "quality_evidence": {"id": "synthetic-routing-fixture", "sha256": "0" * 64},
        }
        for key, profile in execution_profiles.PROFILE_REGISTRY.items()
    }
    policy = {
        "schema": selection.SCHEMA,
        "policy_version": "synthetic-neutral-routing-test",
        "subjects": subjects,
        "default_categories": {role: role for role in roles.ROLES},
        "categories": {
            role: {
                "role": role,
                "candidates": list(subjects),
                "requirements": [],
                "terminal_fallback": "fixture_hold",
            }
            for role in roles.ROLES
        },
        "fallback_rules": {},
    }
    monkeypatch.setattr(selection, "load_policy", lambda: policy)
