"""Local-only correction projection; never remote authentication or ACT authority.

The current bounded-classification task does not emit the risk/test contract
below. Its genuine results therefore remain owned_unknown. The pure projection
is an offline contract for a future reviewed, model-visible semantic version;
adding fields to a current response cannot opt that response into this contract.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from copy import deepcopy

SCHEMA = "closer-correction-decision/v1"
SEMANTIC_SCHEMA = "closer-correction-classification/v1"
MAX_BYTES = 65536
MAX_FINDINGS = 32


class DecisionUnavailable(ValueError):
    pass


def digest(value):
    raw = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")
    if len(raw) > MAX_BYTES:
        raise DecisionUnavailable("correction material exceeds bound")
    return hashlib.sha256(raw).hexdigest()


def _need(value, reason):
    if not value:
        raise DecisionUnavailable(reason)


def project_explicit_semantics(*, semantic, request, bundle, profile):
    """Validate/project a SYNTHETIC future semantic contract, not emission proof.

    Callers must not transport this output as an authenticated decision. Public
    materialize() additionally requires the original controlled task descriptor
    to declare this exact contract (no existing task currently does).
    """
    digest(semantic)
    digest(bundle)
    _need(isinstance(request, Mapping), "request unavailable")
    _need(request.get("schema") == "closer-correction-request/v1", "request schema differs")
    _need(
        request.get("request_sha256")
        == digest({k: v for k, v in request.items() if k != "request_sha256"}),
        "request digest differs",
    )
    _need(
        isinstance(semantic, dict)
        and set(semantic) == {"schema", "request_sha256", "bundle_sha256", "findings"},
        "semantic fields differ",
    )
    _need(
        semantic["schema"] == SEMANTIC_SCHEMA
        and semantic["request_sha256"] == request["request_sha256"]
        and semantic["bundle_sha256"] == digest(bundle),
        "semantic source differs",
    )
    seeds = bundle.get("seeds")
    rows = semantic["findings"]
    _need(
        isinstance(seeds, list)
        and 0 < len(seeds) <= MAX_FINDINGS
        and isinstance(rows, list)
        and len(rows) == len(seeds),
        "complete findings unavailable",
    )
    selected_seed = None
    identities = []
    expected = []
    for seed in seeds:
        _need(
            all(
                seed.get(k) == request.get(k)
                for k in ("target", "source_issue", "original_verifier", "scope")
            ),
            "raw seed source differs",
        )
        finding = seed["finding"]
        expected.append(digest(finding))
        if finding == request["finding"]:
            selected_seed = seed
    projected = []
    for row in rows:
        _need(
            isinstance(row, dict)
            and set(row) == {"id", "evidence", "classification", "required_tests"},
            "risk/test fields unavailable",
        )
        identities.append(digest({"id": row["id"], "evidence": row["evidence"]}))
        _need(
            row["classification"]
            in {"ordinary_substantive", "security", "authority_conflict", "unknown"},
            "risk classification unavailable",
        )
        _need(
            isinstance(row["required_tests"], list) and row["required_tests"],
            "required tests unavailable",
        )
        _need(
            all(
                isinstance(test, dict)
                and test.get("finding_id") == row["id"]
                and test.get("source_evidence") == row["evidence"]
                for test in row["required_tests"]
            ),
            "required tests source differs",
        )
        projected.append({k: deepcopy(row[k]) for k in ("id", "evidence", "classification")})
        projected[-1]["required_tests_sha256"] = digest(row["required_tests"])
        if (
            row["id"] == request["finding"]["id"]
            and row["evidence"] == request["finding"]["evidence"]
        ):
            _need(row["required_tests"] == request["required_tests"], "selected tests differ")
    _need(
        len(set(identities)) == len(identities)
        and sorted(identities) == sorted(expected)
        and selected_seed is not None,
        "raw findings omitted or substituted",
    )
    _need(
        isinstance(profile, dict)
        and set(profile) == {"id", "role", "qualification_ref", "qualification_sha256"}
        and profile.get("role") == "closer_interpret",
        "classification profile differs",
    )
    seed = selected_seed
    decision = {
        "schema": SCHEMA,
        "seed_effect_key": digest(
            {
                "schema": "closer-correction-effect/v1",
                "repository": seed["target"]["repository"],
                "event_id": seed["original_verifier"]["verification_event"]["event_id"],
                "finding_evidence": seed["finding"]["evidence"],
                "source_issue": seed["source_issue"],
                "scope": seed["scope"],
            }
        ),
        "source_scope_digest": digest(
            {
                "target": seed["target"],
                "source_issue": seed["source_issue"],
                "finding_evidence": seed["finding"]["evidence"],
                "scope": seed["scope"],
            }
        ),
        "request_sha256": request["request_sha256"],
        "findings": projected,
        "profile": deepcopy(profile),
    }
    return decision


def materialize(*, proof, run_id, basis, receipt_root, profile, request, bundle, now=None):
    """Revalidate original local stage proof; unsupported semantics fail closed.

    proof/basis must come from the existing authenticated original stage owner,
    never a remote request. A receipt self-hash is not remote authentication.
    This API neither reads credentials nor launches processes.
    """
    import closer_dispatcher as closer
    import closer_role_qualification as roles
    import execution_profiles

    try:
        closer._revalidate_controlled_proof(
            proof, run_id=run_id, basis=basis, receipt_root=receipt_root
        )
        expected = proof["expected"]
        _need(expected["task_type"] == "closer_interpret", "selected task differs")
        _need(
            roles.profile_identity(profile) == expected["profile_identity"],
            "profile identity differs",
        )
        canonical = execution_profiles.get_profile(profile["profile_id"])
        _need(
            roles.profile_identity(canonical) == roles.profile_identity(profile),
            "current canonical profile differs",
        )
        problems = roles.qualification_problems(profile, roles.BOUNDED_CLASSIFICATION, now=now)
        _need(not problems, "; ".join(problems))
        descriptor = expected["production_model_contract"]
        # Exact current-source descriptor is already checked by proof revalidation.
        # No caller boolean, extra response fields or sidecar can enable this.
        _need(
            descriptor.get("correction_semantic_schema") == SEMANTIC_SCHEMA,
            "selected classification task lacks validated per-finding risk and required-test contract",
        )
        # No producer/semantic validator for this task exists yet. Keep this
        # barrier even if a descriptor is accidentally expanded in isolation.
        raise DecisionUnavailable("reviewed correction semantic producer/validator not implemented")
    except (ValueError, TypeError, KeyError, OSError) as error:
        return {
            "status": "owned_unknown",
            "reason": str(error),
            "evidence_scope": "local_controlled_execution_only",
            "act_authorized": False,
            "source_closure_authorized": False,
        }
