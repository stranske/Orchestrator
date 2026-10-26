"""One local controlled correction classification under an already-owned lease.

The existing source owner must authenticate the original correction inputs.
This producer binds them to physical packet custody and the actual controlled
invocation. It never allocates a lease, authenticates remote JSON, or retries.
"""

from __future__ import annotations

import hashlib
import json
import stat
from pathlib import Path

import closer_correction_classification as contract

TASK = "closer_correction_classification"


def packet_context(ref, *, original_request, basis):
    """Read the original bounded physical packet; never accept an output root."""
    import closer_execution_contracts as execution_contracts

    path = Path(ref["path"])
    if not path.is_absolute() or path.resolve() != path or path.is_symlink():
        raise contract.ContractError("original packet physical path differs")
    # Existing packet custody reads enforce no-follow, bounded size and hash.
    packet, root = execution_contracts.load_retained_packet(ref)
    metadata = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size != ref["bytes"]:
        raise contract.ContractError("original packet size differs")
    manifest_sha = (packet.get("manifest") or {}).get("packet_sha256")
    if not (
        root.is_dir()
        and root.resolve() == root
        and path.parent.name == "manifests"
        and isinstance(manifest_sha, str)
        and len(manifest_sha) == 64
        and path.name == manifest_sha + ".json"
    ):
        raise contract.ContractError("original packet evidence root differs")
    target = original_request["target"]
    target_name = target["repository"] + "#" + target["pr"]
    if not (
        basis.get("target") == target_name
        and basis.get("head") == target["head_sha"]
        and basis.get("base") == target["base_sha"]
        and basis.get("packet_sha256") == ref["sha256"]
        and packet.get("target") == target_name
        and packet.get("head") == target["head_sha"]
        and packet.get("base") == target["base_sha"]
        and packet.get("capture_revision") == basis.get("revision")
        and isinstance(basis.get("revision"), str)
        and basis["revision"]
    ):
        raise contract.ContractError("original correction packet/basis differs")
    return packet, root


def prepare(
    *, original_request, seed_bundle, source_body, raw_findings, retained_packet_ref, basis
):
    inputs = dict(
        original_request=original_request,
        seed_bundle=seed_bundle,
        source_body=source_body,
        raw_findings=raw_findings,
        retained_packet_ref=retained_packet_ref,
    )
    request = contract.build_request(**inputs)
    _, root = packet_context(retained_packet_ref, original_request=original_request, basis=basis)
    request_id = request["classification_request_sha256"]
    if basis.get("request_id", request_id) != request_id:
        raise contract.ContractError("original classification request identity differs")
    return (
        inputs,
        request,
        contract.render_prompt(request),
        {**basis, "request_id": request_id},
        root,
    )


def _held(error, *, actual=None, request_ref=None):
    return {
        "status": "owned_unknown",
        "reason": str(error)[:1000],
        "evidence_scope": "local_controlled_execution_only",
        "act_authorized": False,
        "source_closure_authorized": False,
        "retry_safe": False,
        **({"actual": actual} if actual is not None else {}),
        **({"request_ref": request_ref} if request_ref else {}),
    }


def retain(root, value, suffix):
    """Use existing content-addressed custody and verify the exact resulting bytes."""
    import closer_dispatcher as closer
    import closer_evidence

    raw = json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    if not 0 < len(raw) <= contract.MAX_BYTES:
        raise contract.ContractError("retained correction evidence exceeds bound")
    # The supplied packet root is the sole custody root. Refuse redirected
    # existing descendants BEFORE the existing writer can create anything.
    sha = hashlib.sha256(raw).hexdigest()
    destination = root / "raw" / sha[:2] / (sha + "." + suffix)
    if root.resolve() != root or not root.is_dir():
        raise contract.ContractError("correction custody root differs")
    current = root
    for component in destination.relative_to(root).parts:
        current = current / component
        if current.is_symlink() or (current.exists() and current.resolve() != current):
            raise contract.ContractError("correction custody path redirected")
    ref = closer_evidence._retain_bytes(root, raw, suffix)
    # Existing reader checks type/no-follow/hash/size before parsing. Diagnostic
    # evidence references do not replace the independent original provider proof.
    checked = closer._read_controlled_receipt(ref, root)
    if checked != value or ref["sha256"] != hashlib.sha256(raw).hexdigest():
        raise contract.ContractError("retained correction evidence differs")
    return ref


def produce(
    *,
    original_request,
    seed_bundle,
    source_body,
    raw_findings,
    retained_packet_ref,
    profile_id,
    frontier,
    basis,
    operation_id,
    deadline,
):
    import closer_correction_decision as decisions
    import closer_dispatcher as closer
    import dispatcher

    request_ref = None
    actual = None
    try:
        inputs, request, prompt, bound_basis, root = prepare(
            original_request=original_request,
            seed_bundle=seed_bundle,
            source_body=source_body,
            raw_findings=raw_findings,
            retained_packet_ref=retained_packet_ref,
            basis=basis,
        )
        # Request retention happens before submission. Failure creates no effect.
        request_ref = retain(root, request, "correction-request.json")
        actual = closer._controlled_offload(
            agent="claude",
            prompt=prompt,
            profile_id=profile_id,
            task_type=TASK,
            operation_id=operation_id,
            frontier=frontier,
            target=bound_basis["target"],
            basis=bound_basis,
            deadline=deadline,
            mode="assess",
        )
        if not isinstance(actual, dict) or actual.get("exit") != 0:
            return _held(
                "controlled correction invocation unavailable or uncertain",
                actual=actual,
                request_ref=request_ref,
            )
        return decisions.materialize_selected(
            proof=actual.get("_closer_controlled_proof"),
            run_id=actual.get("run_id"),
            basis=bound_basis,
            receipt_root=dispatcher.DISPATCH_LOG_DIR,
            profile_id=profile_id,
            request_ref=request_ref,
            **inputs,
        )
    except (OSError, ValueError, TypeError, KeyError, RuntimeError, RecursionError) as error:
        return _held(error, actual=actual, request_ref=request_ref)
