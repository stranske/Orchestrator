"""Synthetic role verdict for historical capacity/transport fixtures only.

Those tests already replace provider and lifecycle admission boundaries. They
now explicitly supply the role boundary too; they do not qualify any model.
Real role-record and route-selection causality lives in the role test modules.
"""

import closer_role_qualification as roles


def fixture_role_verdict(profile, role):
    # Preserve the historical test cohort, never production qualification.
    effort = profile.get("reasoning_effort")
    return role in roles.ROLES and effort in (
        {"none", "minimal", "low"} if role == roles.BOUNDED_CLASSIFICATION else {"medium", "high"}
    )


def install_synthetic_role_verdict(monkeypatch):
    import time

    from test_closer_role_qualification import record

    import closer_dispatcher
    import execution_profiles

    # These historical fixture callers also need an explicit synthetic launch
    # contract now that routing checks it before reaching their inert broker.
    # Keep the real descriptor/limits validation; this registry is test-local.
    descriptor = closer_dispatcher._controlled_model_contract("closer_interpret")
    records = {}
    for profile in execution_profiles.PROFILE_REGISTRY.values():
        if not fixture_role_verdict(profile, roles.BOUNDED_CLASSIFICATION):
            continue
        value = record(profile, roles.BOUNDED_CLASSIFICATION)
        key = profile["profile_id"] + ":synthetic-classification"
        value.update(
            qualification_id=key,
            qualified_at=time.time() - 10,
            expires_at=time.time() + 600,
            production_model_contract=descriptor,
            execution_limits={
                "schema": "closer-data-only-limits/v1",
                "role": roles.BOUNDED_CLASSIFICATION,
                "model_contract_sha256": descriptor["contract_sha256"],
                "max_prompt_bytes": 65536,
                "max_final_bytes": 8192,
                "max_native_result_bytes": 65536,
                "max_output_tokens": 2048,
                "max_turns": 1,
                "timeout_seconds": 45,
            },
        )
        records[key] = value
    monkeypatch.setattr(roles, "ROLE_QUALIFICATIONS", records)
    monkeypatch.setattr(roles, "is_runtime_qualified", fixture_role_verdict)
