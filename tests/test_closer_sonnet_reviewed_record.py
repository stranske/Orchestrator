"""Finite reviewed record wiring; no provider or live-clock dependence."""

from copy import deepcopy

import closer_dispatcher as closer
import closer_role_qualification as q
import execution_profiles


def _record():
    return next(iter(q.ROLE_QUALIFICATIONS.values()))


def _historical_registry():
    record = _record()
    return {record["qualification_id"]: record}


def test_reviewed_record_has_original_finite_observation():
    r = _record()
    assert r["qualified_at"] == 1793161032.187778
    assert r["expires_at"] == 1793247432.187778
    assert r["expires_at"] - r["qualified_at"] == 86400
    p = execution_profiles.get_profile(r["profile_identity"]["profile_id"])
    assert q.is_qualified(
        p,
        q.STRONG_CRITERION_ADJUDICATION,
        now=r["qualified_at"] + 1,
        records=_historical_registry(),
    )
    assert not q.is_qualified(
        p,
        q.STRONG_CRITERION_ADJUDICATION,
        now=r["expires_at"],
        records=_historical_registry(),
    )


def test_record_never_qualifies_other_roles():
    r = _record()
    p = execution_profiles.get_profile(r["profile_identity"]["profile_id"])
    assert not q.is_qualified(p, q.BOUNDED_CLASSIFICATION, now=r["qualified_at"] + 1)
    assert not q.is_qualified(p, q.CONSTRAINED_REPAIR, now=r["qualified_at"] + 1)


def test_record_identity_cannot_change_effort():
    r = _record()
    p = deepcopy(execution_profiles.get_profile(r["profile_identity"]["profile_id"]))
    p["reasoning_effort"] = "low"
    assert not q.is_qualified(
        p,
        q.STRONG_CRITERION_ADJUDICATION,
        now=r["qualified_at"] + 1,
        records=_historical_registry(),
    )


def test_content_addressed_evidence_has_no_personal_paths():
    refs = _record()["evidence_refs"]
    assert len(refs) == 2
    assert all(ref["reference"].endswith(ref["sha256"]) for ref in refs)
    assert all("/Users/" not in ref["reference"] for ref in refs)


def test_record_insertion_preserves_observed_model_contract():
    descriptor = closer._controlled_model_contract("closer_exception")
    record = q.ROLE_QUALIFICATIONS[
        "sonnet-current-role-qualification:4bb25aaa8f76ab3b0997e7ffb6d757d3ffa541a04ad39a0cf3fbde3b5a21de89"
    ]
    observed = record["production_model_contract"]
    assert (
        observed["contract_sha256"]
        == "d78018220041ac17165dec79b98f67dec29945d7b55c115551d64948509c4f7d"
    )
    support = q.execution_source_support(
        record,
        record["profile_identity"],
        descriptor,
        task_type="closer_exception",
        checked_at=record["qualified_at"] + 1,
    )
    assert support["limits"] == record["execution_limits"]
    assert support["admission"] is None
    assert "closer_role_qualification.py" not in descriptor["sources"]
