"""Synthetic role evidence never qualifies a production model or invokes a provider."""

from copy import deepcopy

import pytest

import closer_role_qualification as roles

NOW = 1000.0


@pytest.fixture
def profile(monkeypatch):
    import execution_profiles

    value = {
        "profile_id": "offline-candidate-low",
        "agent": "claude",
        "provider": "offline-provider",
        "requested_model": "offline-model",
        "reasoning_effort": "low",
        "permission_mode": "workspace-write",
        "adapter_version": "offline-adapter-v1",
        "prompt_version": "offline-prompt-v1",
        "transport_support": ["offload", "local"],
        "capacity_pool_ids": ["offline-pool"],
        "lifecycle_status": "active",
    }
    monkeypatch.setitem(execution_profiles.PROFILE_REGISTRY, value["profile_id"], deepcopy(value))
    return value


def record(profile, role=roles.STRONG_CRITERION_ADJUDICATION):
    return {
        "schema": roles.SCHEMA,
        "status": "qualified",
        "qualification_id": "offline-qualification",
        "role": role,
        "profile_identity": roles.profile_identity(profile),
        "profile_identity_sha256": roles.identity_digest(profile),
        "qualified_at": NOW - 10,
        "expires_at": NOW + 10,
        "transport": "offload",
        "evidence_refs": [{"reference": "offline:fixture-result", "sha256": "e" * 64}],
        "named_criterion_only": True,
        "confinement": {
            "data_only": True,
            "tools": False,
            "mcp": False,
            "hooks": False,
            "project_context": False,
            "adapter_build_sha256": "b" * 64,
        },
        "repair_capabilities": {
            "mechanical_only": True,
            "substantive_edits": False,
            "isolated_workspace": True,
            "path_allowlist_enforced": True,
            "operation_allowlist_enforced": True,
            "test_required": True,
            "allowlisted_operations": ["format"],
        },
    }


def qualified(profile, value, role=roles.STRONG_CRITERION_ADJUDICATION, **kwargs):
    return roles.is_qualified(
        profile, role, now=NOW, records={"offline-qualification": value}, **kwargs
    )


def test_empty_registry_does_not_fabricate_qualification(profile):
    assert not roles.is_qualified(profile, roles.STRONG_CRITERION_ADJUDICATION, now=NOW, records={})


@pytest.mark.parametrize("role", sorted(roles.ROLES))
def test_complete_role_specific_fixture_qualifies(profile, role):
    assert qualified(profile, record(profile, role), role)


def test_low_can_qualify_for_strong_role_without_effort_promotion(profile):
    assert qualified(profile, record(profile))
    high = dict(profile, reasoning_effort="high")
    assert not qualified(high, record(profile))
    assert not roles.is_qualified(high, roles.STRONG_CRITERION_ADJUDICATION, now=NOW, records={})


@pytest.mark.parametrize(
    "field,value",
    [
        ("profile_id", "other-profile"),
        ("agent", "codex"),
        ("provider", "other-provider"),
        ("requested_model", "other-model"),
        ("reasoning_effort", "high"),
        ("permission_mode", "read-only"),
        ("adapter_version", "offline-adapter-v2"),
        ("prompt_version", "offline-prompt-v2"),
        ("lifecycle_status", "retired"),
        ("transport_support", ["offload", "experiment"]),
        ("capacity_pool_ids", ["other-pool"]),
        ("requires_explicit_selection", True),
    ],
)
def test_every_execution_identity_change_invalidates_evidence(profile, field, value):
    assert not qualified(dict(profile, **{field: value}), record(profile))


def test_identity_order_is_canonical_and_no_extra_metadata_grants_a_role(profile):
    reverse = deepcopy(profile)
    reverse["transport_support"].reverse()
    assert roles.identity_digest(profile) == roles.identity_digest(reverse)
    candidate = dict(profile, roles_candidate=list(roles.ROLES), role_qualified=True)
    assert not roles.is_qualified(
        candidate, roles.STRONG_CRITERION_ADJUDICATION, records={}, now=NOW
    )


@pytest.mark.parametrize(
    "field", ["schema", "status", "qualification_id", "profile_identity_sha256", "transport"]
)
def test_record_binding_is_required(profile, field):
    value = record(profile)
    value[field] = "wrong"
    assert not qualified(profile, value)


@pytest.mark.parametrize("field", ["qualified_at", "expires_at"])
@pytest.mark.parametrize("value", [True, None, "1000", float("nan"), float("inf"), 10**1000])
def test_nonfinite_or_untyped_original_times_are_invalid(profile, field, value):
    evidence = record(profile)
    evidence[field] = value
    assert not qualified(profile, evidence)


@pytest.mark.parametrize(
    "started,expires", [(NOW + 1, NOW + 10), (NOW - 10, NOW), (NOW, NOW), (-1, NOW + 10)]
)
def test_future_expired_and_reversed_time_windows_do_not_qualify(profile, started, expires):
    value = record(profile)
    value.update(qualified_at=started, expires_at=expires)
    assert not qualified(profile, value)


@pytest.mark.parametrize("field", ["tools", "mcp", "hooks", "project_context"])
@pytest.mark.parametrize(
    "role", [roles.BOUNDED_CLASSIFICATION, roles.STRONG_CRITERION_ADJUDICATION]
)
def test_each_ambient_context_boundary_must_be_explicitly_absent(profile, field, role):
    value = record(profile, role)
    value["confinement"][field] = True
    assert not qualified(profile, value, role)


def test_transport_build_and_named_criterion_evidence_are_required(profile):
    value = record(profile)
    del value["confinement"]["adapter_build_sha256"]
    assert not qualified(profile, value)
    value = record(profile)
    value["named_criterion_only"] = False
    assert not qualified(profile, value)


@pytest.mark.parametrize(
    "field",
    [
        "mechanical_only",
        "isolated_workspace",
        "path_allowlist_enforced",
        "operation_allowlist_enforced",
        "test_required",
    ],
)
def test_repair_requires_each_mechanical_confinement_capability(profile, field):
    value = record(profile, roles.CONSTRAINED_REPAIR)
    value["repair_capabilities"][field] = False
    assert not qualified(profile, value, roles.CONSTRAINED_REPAIR)


@pytest.mark.parametrize(
    "operations", [[], ["implement_feature"], ["format", "format"], [True], "format"]
)
def test_unknown_or_ambiguous_repair_operations_are_rejected(profile, operations):
    value = record(profile, roles.CONSTRAINED_REPAIR)
    value["repair_capabilities"]["allowlisted_operations"] = operations
    assert not qualified(profile, value, roles.CONSTRAINED_REPAIR)


def test_substantive_repair_or_readonly_repair_profile_never_qualifies(profile):
    value = record(profile, roles.CONSTRAINED_REPAIR)
    value["repair_capabilities"]["substantive_edits"] = True
    assert not qualified(profile, value, roles.CONSTRAINED_REPAIR)
    readonly = dict(profile, permission_mode="read-only")
    assert not qualified(
        readonly, record(readonly, roles.CONSTRAINED_REPAIR), roles.CONSTRAINED_REPAIR
    )


@pytest.mark.parametrize(
    "refs", [None, [], [{"reference": "offline:fixture", "sha256": "bad"}], [{"sha256": "e" * 64}]]
)
def test_bound_evidence_reference_is_mandatory(profile, refs):
    value = record(profile)
    value["evidence_refs"] = refs
    assert not qualified(profile, value)


def test_ambiguous_duplicate_and_malformed_registry_records_are_rejected(profile):
    value = record(profile)
    records = {"offline-qualification": value, "other": dict(value, qualification_id="other")}
    assert not roles.is_qualified(
        profile, roles.STRONG_CRITERION_ADJUDICATION, records=records, now=NOW
    )
    records = {"offline-qualification": value, "malformed": None}
    assert not roles.is_qualified(
        profile, roles.STRONG_CRITERION_ADJUDICATION, records=records, now=NOW
    )


@pytest.mark.parametrize("role", [None, [], "security_authority", "*"])
def test_unknown_roles_never_infer_authority(profile, role):
    assert roles.qualification_problems(profile, role, now=NOW)


@pytest.mark.parametrize("bad_time", [True, -1, float("nan"), float("inf"), 10**1000])
def test_observation_time_is_finite_and_typed(profile, bad_time):
    assert not roles.is_qualified(profile, roles.BOUNDED_CLASSIFICATION, now=bad_time)


def test_profile_missing_transport_or_identity_fields_is_invalid(profile):
    for field in ("requested_model", "adapter_version", "transport_support", "capacity_pool_ids"):
        value = dict(profile)
        del value[field]
        with pytest.raises(ValueError):
            roles.profile_identity(value)
        assert roles.qualification_problems(value, roles.BOUNDED_CLASSIFICATION, now=NOW)
    with pytest.raises(ValueError, match="Aider"):
        roles.profile_identity(dict(profile, agent="aider"))


def test_qualified_record_is_the_validated_unique_record_and_not_mutable_authority(profile):
    evidence = record(profile)
    records = {"offline-qualification": evidence}
    result = roles.qualified_record(
        profile, roles.STRONG_CRITERION_ADJUDICATION, now=NOW, records=records
    )
    assert result == evidence
    assert result is not evidence
    result["confinement"]["adapter_build_sha256"] = "f" * 64
    assert evidence["confinement"]["adapter_build_sha256"] == "b" * 64


def test_qualified_record_does_not_relax_rejection_paths(profile):
    evidence = record(profile)
    role = roles.STRONG_CRITERION_ADJUDICATION
    records = {"offline-qualification": evidence}
    assert roles.qualified_record(profile, role, now=NOW + 10, records=records) is None
    assert (
        roles.qualified_record(
            dict(profile, reasoning_effort="high"), role, now=NOW, records=records
        )
        is None
    )
    assert roles.qualified_record(profile, role, now=NOW, records={}) is None
    records["duplicate"] = dict(evidence, qualification_id="duplicate")
    assert roles.qualified_record(profile, role, now=NOW, records=records) is None


def test_valid_expired_history_selects_one_current_record(profile):
    old = record(profile)
    old.update(qualified_at=NOW - 20, expires_at=NOW - 10)
    current = record(profile)
    current.update(qualification_id="current", qualified_at=NOW - 1, expires_at=NOW + 10)
    records = {"offline-qualification": old, "current": current}

    assert roles.is_qualified(
        profile, roles.STRONG_CRITERION_ADJUDICATION, now=NOW, records=records
    )
    assert (
        roles.qualified_record(
            profile, roles.STRONG_CRITERION_ADJUDICATION, now=NOW, records=records
        )
        == current
    )


def test_expired_history_alone_or_two_live_records_stays_unqualified(profile):
    expired = record(profile)
    expired.update(qualified_at=NOW - 20, expires_at=NOW - 10)
    assert not roles.is_qualified(
        profile,
        roles.STRONG_CRITERION_ADJUDICATION,
        now=NOW,
        records={"offline-qualification": expired},
    )
    live = record(profile)
    live.update(qualification_id="live")
    assert not roles.is_qualified(
        profile,
        roles.STRONG_CRITERION_ADJUDICATION,
        now=NOW,
        records={"offline-qualification": record(profile), "live": live},
    )


def test_invalid_expired_or_future_match_blocks_current_record(profile):
    current = record(profile)
    current.update(qualification_id="current")
    malformed_expired = record(profile)
    malformed_expired.update(qualification_id="old", qualified_at=NOW - 20, expires_at=NOW - 10)
    malformed_expired["evidence_refs"] = []
    future = record(profile)
    future.update(qualification_id="future", qualified_at=NOW + 1, expires_at=NOW + 10)
    for conflicting in (malformed_expired, future):
        records = {"current": current, conflicting["qualification_id"]: conflicting}
        assert not roles.is_qualified(
            profile, roles.STRONG_CRITERION_ADJUDICATION, now=NOW, records=records
        )
        assert (
            roles.qualified_record(
                profile, roles.STRONG_CRITERION_ADJUDICATION, now=NOW, records=records
            )
            is None
        )


def test_reviewed_current_strong_record_has_exact_scope_and_preserves_old_row():
    import closer_dispatcher as dispatcher
    import execution_profiles

    old_id = "sonnet-current-role-qualification:3e7ba0521480a5d44d6a081e5a43b8895aeded9eba9080dbe8b8ebdca98fb009"
    current_id = "sonnet-current-role-qualification:c256f1adb15405e8aa82e2baab729c38f1a0edb9994c529079e99ec3d56358f2"
    old = roles.ROLE_QUALIFICATIONS[old_id]
    current = roles.ROLE_QUALIFICATIONS[current_id]
    now = current["qualified_at"] + 1

    assert (
        roles._source_digest(old)
        == "837713af72c5a701bd1b50757458632597aaf8f482ef2f2109811b101d402482"
    )
    assert (
        roles._source_digest(current)
        == "ce2e65fd701578b5b68192dacce4b1551830770f00d4d7817ce9f9c834a4728f"
    )
    assert current["expires_at"] == current["qualified_at"] + 86400
    assert current["execution_limits"]["model_contract_sha256"] == (
        "239df39539465467e89dc47c852f17330ea3700a485806ef69b21933c52af92a"
    )
    assert (
        current["production_model_contract"]["contract_sha256"]
        == current["execution_limits"]["model_contract_sha256"]
    )
    assert current["production_model_contract"] == dispatcher._controlled_model_contract(
        "closer_exception"
    )
    assert current["evidence_refs"] == [
        {
            "reference": "retained-native-evidence:sha256:82ff806f22863e3f5fca5a942c2cdc666f100a6cfbb922f7f066b8e807ffb20e",
            "sha256": "82ff806f22863e3f5fca5a942c2cdc666f100a6cfbb922f7f066b8e807ffb20e",
        },
        {
            "reference": "reviewed-model-quality:sha256:7cb79862271c926351461d7fb1de13b789f753e523345acede096dd70d88c3e0",
            "sha256": "7cb79862271c926351461d7fb1de13b789f753e523345acede096dd70d88c3e0",
        },
    ]
    profile = execution_profiles.PROFILE_REGISTRY["claude-sonnet-5-5-high-v2"]
    assert roles.qualified_record(profile, roles.STRONG_CRITERION_ADJUDICATION, now=now) == current


def test_runtime_qualification_requires_the_same_supported_installed_build(profile, monkeypatch):
    import adapters

    value = record(profile)
    records = {"offline-qualification": value}
    calls = []
    monkeypatch.setattr(adapters, "DATA_ONLY_CLAUDE_SHA256", "b" * 64)
    monkeypatch.setattr(adapters, "data_only_problem", lambda *args: calls.append(args))
    assert roles.is_runtime_qualified(
        profile, roles.STRONG_CRITERION_ADJUDICATION, now=NOW, records=records
    )
    assert calls == [("claude", "assess")]

    # Both build A and build B could independently support confinement. Role
    # evidence for build A does not qualify the different currently pinned B.
    monkeypatch.setattr(adapters, "DATA_ONLY_CLAUDE_SHA256", "c" * 64)
    calls.clear()
    assert not roles.is_runtime_qualified(
        profile, roles.STRONG_CRITERION_ADJUDICATION, now=NOW, records=records
    )
    assert calls == []


@pytest.mark.parametrize(
    "role", [roles.BOUNDED_CLASSIFICATION, roles.STRONG_CRITERION_ADJUDICATION]
)
def test_runtime_build_match_without_installed_transport_confinement_is_not_qualified(
    profile, role, monkeypatch
):
    import adapters

    monkeypatch.setattr(adapters, "DATA_ONLY_CLAUDE_SHA256", "b" * 64)
    monkeypatch.setattr(adapters, "data_only_problem", lambda *args: "offline unsupported build")
    assert not roles.is_runtime_qualified(
        profile, role, now=NOW, records={"offline-qualification": record(profile, role)}
    )


def test_missing_role_evidence_does_not_probe_a_binary(profile, monkeypatch):
    import adapters

    def forbidden(*args):
        pytest.fail("missing role evidence must avoid installed binary probes")

    monkeypatch.setattr(adapters, "data_only_problem", forbidden)
    assert not roles.is_runtime_qualified(
        profile, roles.BOUNDED_CLASSIFICATION, now=NOW, records={}
    )
    assert not roles.is_runtime_qualified(
        dict(profile, reasoning_effort="high"),
        roles.STRONG_CRITERION_ADJUDICATION,
        now=NOW,
        records={"offline-qualification": record(profile)},
    )


def test_missing_supported_build_pin_does_not_probe_or_qualify(profile, monkeypatch):
    import adapters

    monkeypatch.delattr(adapters, "DATA_ONLY_CLAUDE_SHA256")
    monkeypatch.setattr(adapters, "data_only_problem", lambda *args: pytest.fail("unbound probe"))
    assert not roles.is_runtime_qualified(
        profile,
        roles.STRONG_CRITERION_ADJUDICATION,
        now=NOW,
        records={"offline-qualification": record(profile)},
    )


@pytest.mark.parametrize("agent", ["codex", "gemini", "cursor", "vibe", "muse"])
def test_candidate_build_hash_does_not_enable_an_unsupported_confinement_transport(
    profile, agent, monkeypatch
):
    import adapters
    import execution_profiles

    candidate = dict(profile, agent=agent)
    monkeypatch.setitem(execution_profiles.PROFILE_REGISTRY, profile["profile_id"], candidate)
    monkeypatch.setattr(adapters, "DATA_ONLY_CLAUDE_SHA256", "b" * 64)
    monkeypatch.setattr(
        adapters, "data_only_problem", lambda *args: pytest.fail("unsupported probe")
    )
    assert not roles.is_runtime_qualified(
        candidate,
        roles.BOUNDED_CLASSIFICATION,
        now=NOW,
        records={"offline-qualification": record(candidate, roles.BOUNDED_CLASSIFICATION)},
    )


def test_constrained_repair_uses_its_mechanical_contract_without_data_only_probe(
    profile, monkeypatch
):
    import adapters

    monkeypatch.setattr(adapters, "data_only_problem", lambda *args: pytest.fail("repair probe"))
    role = roles.CONSTRAINED_REPAIR
    value = record(profile, role)
    assert roles.is_runtime_qualified(
        profile, role, now=NOW, records={"offline-qualification": value}
    )
    value["repair_capabilities"]["substantive_edits"] = True
    assert not roles.is_runtime_qualified(
        profile, role, now=NOW, records={"offline-qualification": value}
    )


@pytest.mark.parametrize("error", [OSError, ValueError, RuntimeError])
def test_runtime_probe_failure_does_not_qualify(profile, error, monkeypatch):
    import adapters

    def failed(*args):
        raise error("offline probe failure")

    monkeypatch.setattr(adapters, "DATA_ONLY_CLAUDE_SHA256", "b" * 64)
    monkeypatch.setattr(adapters, "data_only_problem", failed)
    assert not roles.is_runtime_qualified(
        profile,
        roles.STRONG_CRITERION_ADJUDICATION,
        now=NOW,
        records={"offline-qualification": record(profile)},
    )


@pytest.mark.parametrize("status", ["retired", "disabled", None, "", True])
def test_only_active_profile_lifecycle_can_have_a_role_identity(profile, status):
    with pytest.raises(ValueError):
        roles.profile_identity(dict(profile, lifecycle_status=status))


@pytest.mark.parametrize("role", sorted(roles.ROLES))
@pytest.mark.parametrize(
    "change", ["unregistered", "retirement-map", "retired-status", "registry-drift"]
)
def test_runtime_rechecks_current_registration_and_retirement_before_any_probe(
    profile, role, change, monkeypatch
):
    import adapters
    import execution_profiles

    evidence = record(profile, role)
    monkeypatch.setattr(adapters, "DATA_ONLY_CLAUDE_SHA256", "b" * 64)
    monkeypatch.setattr(
        adapters, "data_only_problem", lambda *args: pytest.fail("stale-profile probe")
    )
    if change == "unregistered":
        monkeypatch.delitem(execution_profiles.PROFILE_REGISTRY, profile["profile_id"])
    elif change == "retirement-map":
        monkeypatch.setitem(
            execution_profiles.PROFILE_RETIREMENTS, profile["profile_id"], "successor"
        )
    elif change == "retired-status":
        monkeypatch.setitem(
            execution_profiles.PROFILE_REGISTRY,
            profile["profile_id"],
            dict(profile, lifecycle_status="retired"),
        )
    else:
        monkeypatch.setitem(
            execution_profiles.PROFILE_REGISTRY,
            profile["profile_id"],
            dict(profile, requested_model="other-model"),
        )
    assert not roles.is_runtime_qualified(
        profile, role, now=NOW, records={"offline-qualification": evidence}
    )


def test_profile_retired_after_record_creation_cannot_reuse_it(profile, monkeypatch):
    import adapters

    evidence = record(profile)
    profile["lifecycle_status"] = "retired"
    monkeypatch.setattr(adapters, "data_only_problem", lambda *args: pytest.fail("retired probe"))
    assert not roles.is_runtime_qualified(
        profile,
        roles.STRONG_CRITERION_ADJUDICATION,
        now=NOW,
        records={"offline-qualification": evidence},
    )
