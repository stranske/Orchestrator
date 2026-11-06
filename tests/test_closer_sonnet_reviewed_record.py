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
        "sonnet-current-role-qualification:3684a85ab6fbd09214c56898b42ee63ac6246d6ea741f615310f0c54b5c90cfe"
    ]
    profile = execution_profiles.get_profile(record["profile_identity"]["profile_id"])
    assert (
        q.qualified_record(
            profile,
            q.STRONG_CRITERION_ADJUDICATION,
            now=record["qualified_at"] + 1,
            records={record["qualification_id"]: record},
        )
        == record
    )
    assert not q.is_qualified(
        profile,
        q.STRONG_CRITERION_ADJUDICATION,
        now=record["expires_at"],
        records={record["qualification_id"]: record},
    )
    observed = record["production_model_contract"]
    assert (
        observed["contract_sha256"]
        == "264d7ad6fad590c9d33fae722b43dd74a3de4d6490c7856b876471ae1a3f60a2"
    )
    support = q.execution_source_support(
        record,
        record["profile_identity"],
        observed,
        task_type="closer_exception",
        checked_at=record["qualified_at"] + 1,
    )
    assert support["limits"] == record["execution_limits"]
    assert support["admission"] is None
    assert "closer_role_qualification.py" not in descriptor["sources"]
