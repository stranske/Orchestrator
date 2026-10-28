"""Finite reviewed record wiring; no provider or live-clock dependence."""

from copy import deepcopy

import closer_dispatcher as closer
import closer_role_qualification as q
import execution_profiles


def _record():
    return next(iter(q.ROLE_QUALIFICATIONS.values()))


def test_reviewed_record_has_original_finite_observation():
    r = _record()
    assert r["qualified_at"] == 1793161032.187778
    assert r["expires_at"] == 1793247432.187778
    assert r["expires_at"] - r["qualified_at"] == 86400
    p = execution_profiles.get_profile(r["profile_identity"]["profile_id"])
    assert q.is_qualified(p, q.STRONG_CRITERION_ADJUDICATION, now=r["qualified_at"] + 1)
    assert not q.is_qualified(p, q.STRONG_CRITERION_ADJUDICATION, now=r["expires_at"])


def test_record_never_qualifies_other_roles():
    r = _record()
    p = execution_profiles.get_profile(r["profile_identity"]["profile_id"])
    assert not q.is_qualified(p, q.BOUNDED_CLASSIFICATION, now=r["qualified_at"] + 1)
    assert not q.is_qualified(p, q.CONSTRAINED_REPAIR, now=r["qualified_at"] + 1)


def test_record_identity_cannot_change_effort():
    r = _record()
    p = deepcopy(execution_profiles.get_profile(r["profile_identity"]["profile_id"]))
    p["reasoning_effort"] = "low"
    assert not q.is_qualified(p, q.STRONG_CRITERION_ADJUDICATION, now=r["qualified_at"] + 1)


def test_content_addressed_evidence_has_no_personal_paths():
    refs = _record()["evidence_refs"]
    assert len(refs) == 2
    assert all(ref["reference"].endswith(ref["sha256"]) for ref in refs)
    assert all("/Users/" not in ref["reference"] for ref in refs)


def test_record_insertion_preserves_observed_model_contract():
    descriptor = closer._controlled_model_contract("closer_exception")
    observed = _record()["production_model_contract"]
    assert (
        observed["contract_sha256"]
        == "1cb727375e9fa27344d89e02a7dbcd1881b613d2045a14fab5e02a30530ee293"
    )
    assert (
        closer._contract_digest(_record())
        == "837713af72c5a701bd1b50757458632597aaf8f482ef2f2109811b101d402482"
    )
    # A reviewed source admission is distinct from the unchanged physical row.
    support = q.execution_source_support(
        _record(),
        _record()["profile_identity"],
        descriptor,
        task_type="closer_exception",
        checked_at=_record()["qualified_at"] + 1,
    )
    assert support["admission"]["historical_contract_sha256"] == observed["contract_sha256"]
    assert support["admission"]["execution_contract_sha256"] == descriptor["contract_sha256"]
    assert support["admission"]["physical_qualification_performed"] is False
    assert "closer_role_qualification.py" not in descriptor["sources"]
