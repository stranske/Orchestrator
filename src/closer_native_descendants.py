"""Native-hook authentication of bounded receipts below one original cohort.

The root receipt remains the full-cohort authority boundary. Descendant JSON is
only an index: actual immutable receipts and existing stage lineage are reread.
These proofs grant observation/continuation, never effect or acceptance authority.
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import math
import os
import re
from collections.abc import Mapping
from pathlib import Path

SCHEMA = "native-descendant-receipts/v1"
FINAL_SCHEMA = "native-descendant-receipts-finalized/v1"
CONTINUATION_SCHEMA = "native-descendant-continuation/v1"
MAX_LEAVES = 1024
MAX_BYTES = 4 * 1024 * 1024
_ROUND = re.compile(r"[0-9a-f]{16,64}")


def _digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def _physical(path):
    from closer_finite_reentry import _physical as physical

    return physical(path)


def _authority(cfg):
    import closer_mechanisms as mechanisms
    import closer_native_observer as native
    from closer_campaign import ROUND_UNIT, load_manifest
    from closer_frontier_policy import validate_frontier

    manifest_path = _physical(cfg["runtime_manifest"])
    manifest = load_manifest(manifest_path)
    context = native.validate_admission(manifest, cfg["native_admission"], for_effects=False)
    frontier = validate_frontier(cfg["finite_frontier"])
    assignment = _physical(cfg["assignment"])
    pin = cfg["native_hooks"]["postrun_dependencies"]["assignment"]
    if (
        cfg.get("mechanism") != "native-handoff/v1"
        or context.get("finite_frontier") != frontier
        or str(assignment) != pin["path"]
        or hashlib.sha256(assignment.read_bytes()).hexdigest() != pin["sha256"]
        or context["assignment"]["sha256"] != pin["sha256"]
    ):
        raise ValueError("descendant original native admission differs")
    worker = cfg.get("native_worker")
    if not isinstance(worker, Mapping) or not (
        worker.get("owner") == frontier["owner"] == context["campaign"]["owner"]
        and worker.get("state") == context["campaign"]["state"]
        and worker.get("generation") == str(context["campaign"]["generation"])
        and worker.get("runtime-manifest") == str(manifest_path) == context["runtime_manifest"]
        and worker.get("mechanism") == "native-handoff/v1"
        and worker.get("target") == ROUND_UNIT
    ):
        raise ValueError("descendant receipt lacks exact original native worker")
    result = Path(worker["result"])
    if not re.fullmatch(r"[0-9a-f]{12}", result.stem) or result != Path(
        worker["state"] + ".results"
    ) / (result.stem + ".json"):
        raise ValueError("descendant original worker result identity differs")
    observer_root = native.contract(manifest)[3]
    handoff = native._read(observer_root / context["nonce"] / ("worker-" + result.stem + ".json"))
    original = handoff["record"]
    if not (
        original.get("schema") == "native-worker-handoff/v1"
        and original.get("nonce") == context["nonce"]
        and original.get("fields") == worker
        and original.get("admission") == context
        and original.get("binding") == native.binding(manifest)
        and hmac.compare_digest(
            str(handoff.get("signature", "")),
            mechanisms._signature(original, mechanisms._key(observer_root)),
        )
    ):
        raise ValueError("descendant original worker lacks signed prelaunch handoff")
    cohort = {
        "frontier": frontier,
        "assignment_sha256": pin["sha256"],
        "dispatch_state": str(cfg["dispatch_state"]),
        "runtime_binding": native.binding(manifest),
        "runtime_manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "coordinator_contract_pin": cfg.get("coordinator_contract_pin"),
        "campaign": {key: context["campaign"][key] for key in ("state", "campaign_id", "owner")},
    }
    return manifest, context, native.contract(manifest)[3], cohort


def _receipt(cfg, ref, targets, frontier):
    import closer_resources as resources

    pipeline = Path(str(cfg["dispatch_state"]) + ".pipeline.json")
    root = resources.scoped_root(pipeline)
    rid = ref.get("round_id")
    if not isinstance(rid, str) or not _ROUND.fullmatch(rid):
        raise ValueError("descendant round identity invalid")
    path = root / "rounds" / (rid + ".json")
    if str(path) != ref.get("receipt_path") or _physical(path) != path:
        raise ValueError("descendant receipt path differs")
    receipt, sha = resources.read_round_receipt(path)
    verdict = resources.validate_stop(pipeline, rid, targets)
    if not (
        receipt.get("schema") == "closer-pipeline-receipt/v1"
        and receipt.get("mode") == "resource_scoped"
        and receipt.get("round_id") == rid
        and ref.get("receipt_sha256") == sha == verdict.get("receipt_sha256")
        and receipt.get("finite_frontier") == ref.get("finite_frontier") == frontier
        and receipt.get("assignment") == sorted(targets)
        and ref.get("run_ids") == receipt.get("run_ids") == verdict.get("run_ids")
        and verdict.get("round_id") == rid
    ):
        raise ValueError("descendant immutable receipt binding differs")
    return receipt, verdict


def _ref(value):
    return {
        key: copy.deepcopy(value[key])
        for key in ("receipt_path", "receipt_sha256", "round_id", "run_ids", "finite_frontier")
    }


def _root(cfg, candidate):
    from closer_dispatcher import targets_from_assignment

    targets = targets_from_assignment(Path(cfg["assignment"]))
    frontier = cfg["finite_frontier"]
    if sorted(targets) != sorted(frontier["targets"]) or candidate.get("assignment") != sorted(
        targets
    ):
        raise ValueError("descendant TOP root must retain the full original cohort")
    receipt, verdict = _receipt(cfg, candidate, targets, frontier)
    if candidate.get("run_ids") != receipt["run_ids"]:
        raise ValueError("descendant TOP root run binding differs")
    return receipt, verdict


def _leaves(cfg, references, prior_round_ids, history):
    import closer_pipeline as pipeline
    import closer_resources as resources
    import closer_stage_continuation as stages

    if not isinstance(references, list) or len(references) > MAX_LEAVES * 8:
        raise ValueError("descendant receipt index exceeds bound")
    by_frontier = {}
    for ref in references:
        if not isinstance(ref, Mapping) or not isinstance(ref.get("finite_frontier"), Mapping):
            raise ValueError("descendant receipt index is malformed")
        key = (ref.get("target"), ref["finite_frontier"].get("frontier_id"))
        if key in by_frontier and by_frontier[key] != ref:
            raise ValueError("descendant receipt identity conflicts")
        by_frontier[key] = ref
    root = resources.scoped_root(Path(str(cfg["dispatch_state"]) + ".pipeline.json"))
    result = []
    for target in cfg["finite_frontier"]["targets"]:
        shard = resources.shard_dir(root, target) / "state.json"
        actions = shard.parent / "state.actions" / (pipeline._digest(target) + ".json")
        leaf = stages.active_leaf(shard, actions, cfg["finite_frontier"], target)
        state = pipeline._load(shard)
        for index, hop in enumerate(leaf["chain"]):
            frontier = hop["record"]["finite_frontier"]
            supplied = by_frontier.pop((target, frontier["frontier_id"]), None)
            entry = (state.get("frontiers") or {}).get(frontier["frontier_id"])
            if supplied is None or not isinstance(entry, Mapping):
                raise ValueError("active descendant lacks an actual immutable receipt")
            receipt, verdict = _receipt(cfg, supplied, [target], frontier)
            row = entry["items"][target]
            if receipt["targets"].get(target) != row or receipt["run_ids"] != {
                target: entry["run_id"]
            }:
                raise ValueError("descendant receipt no longer binds its exact original row/run")
            reference = {"target": target, **_ref(supplied)}
            historical = supplied["round_id"] in prior_round_ids
            if historical and not any(item["receipt_ref"] == reference for item in history):
                raise ValueError("historical descendant lacks finalized original worker proof")
            unknown = (
                row.get("status") == "unknown"
                or row.get("stage_status") in {"unknown", "running"}
                or row.get("provider_outcome_uncertain") not in (None, False)
                or row.get("recovery_barrier") is not None
            )
            if unknown:
                domain = "unknown"
            elif verdict.get("global_stop_valid") is True and verdict.get("valid") is True:
                domain = "completed"
            elif verdict.get("admitted_domain_valid") is True and not verdict.get(
                "admitted_domain_reasons"
            ):
                domain = "known_held"
            else:
                domain = "unknown"
            if historical and domain == "unknown":
                raise ValueError("historical UNKNOWN descendant cannot be reused")
            # receipt_refs is an append-only observation index, not causal
            # ancestry. Hash the immutable link separately from its index.
            links = [
                {key: value for key, value in part["record"].items() if key != "receipt_refs"}
                for part in leaf["chain"][: index + 1]
            ]
            result.append(
                {
                    "target": target,
                    "frontier": frontier,
                    "receipt_ref": reference,
                    "source_row_sha256": _digest(row),
                    "chain_sha256": _digest(links),
                    "active": frontier == leaf["finite_frontier"],
                    "domain": domain,
                }
            )
    if by_frontier:
        raise ValueError("descendant receipt index contains foreign/unlinked targets")
    return result


def _write_signed(path, record, root):
    import closer_mechanisms as mechanisms
    import closer_native_observer as native

    raw = json.dumps(
        {"record": record, "signature": mechanisms._signature(record, mechanisms._key(root))},
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()
    if len(raw) > MAX_BYTES:
        raise ValueError("descendant bundle exceeds bound")
    if path.exists() or path.is_symlink():
        if path.is_symlink() or native._bounded_bytes(path, MAX_BYTES) != raw:
            raise ValueError("descendant bundle identity already exists with different bytes")
    else:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    return {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest()}


def _read_signed(cfg, ref):
    import closer_mechanisms as mechanisms
    import closer_native_observer as native

    _manifest, _context, root, cohort = _authority(cfg)
    path = _physical(ref["path"])
    if path.parent.parent != root or path.name not in {
        "descendant-receipts.json",
        "descendant-receipts-finalized.json",
    }:
        raise ValueError("descendant bundle escapes original observer root")
    raw = native._bounded_bytes(path, MAX_BYTES)
    signed = json.loads(raw)
    record = signed["record"]
    if not (
        hashlib.sha256(raw).hexdigest() == ref.get("sha256")
        and record.get("cohort") == cohort
        and record.get("admission_nonce") == path.parent.name
        and hmac.compare_digest(
            str(signed.get("signature", "")), mechanisms._signature(record, mechanisms._key(root))
        )
    ):
        raise ValueError("descendant authenticated bundle differs")
    return record


def _actual_known_worker(cfg, record, ref, *, allow_unknown=False):
    import closer_campaign as campaign
    import closer_mechanisms as mechanisms
    import closer_native_observer as native

    manifest, _context, root, _cohort = _authority(cfg)
    worker = record["worker"]
    result_path = _physical(worker["result"])
    result = native._read(result_path)
    worker_id = result_path.stem
    doc = campaign.Campaign(_physical(worker["state"])).read()
    workers = (doc or {}).get("workers", [])
    if (
        not isinstance(workers, list)
        or len(workers) > MAX_LEAVES
        or any(not isinstance(item, Mapping) for item in workers)
    ):
        raise ValueError("descendant worker registry is malformed or exceeds bound")
    matches = [item for item in workers if item.get("worker_id") == worker_id]
    handoff = native._read(root / record["admission_nonce"] / ("worker-" + worker_id + ".json"))
    original = handoff["record"]
    detail = json.loads(result["detail"])
    if not (
        len(matches) == 1
        and str(result_path) == str(campaign.result_path(Path(worker["state"]), worker_id))
        and doc.get("owner") == record["cohort"]["campaign"]["owner"]
        and doc.get("campaign_id") == record["cohort"]["campaign"]["campaign_id"]
        and matches[0].get("state") == "terminal"
        and result.get("outcome") == matches[0].get("outcome")
        and result.get("outcome")
        in (
            {"ok", "scoped_continuation", "unavailable", "unknown"}
            if allow_unknown
            else {"ok", "scoped_continuation", "unavailable"}
        )
        and result.get("worker_id") == worker_id
        and result.get("target") == worker["target"]
        and result.get("mechanism") == "native-handoff/v1"
        and detail.get("descendant_bundle") == ref
        and original.get("schema") == "native-worker-handoff/v1"
        and original.get("nonce") == record["admission_nonce"]
        and original.get("fields") == worker
        and worker.get("mechanism") == "native-handoff/v1"
        and worker.get("runtime-manifest") == str(Path(cfg["runtime_manifest"]))
        and doc.get("release_pin") == manifest["package_digest"]
        and original.get("binding") == native.binding(manifest)
        and original.get("admission") == record["admission"]
        and result.get("native_admission") == record["admission"]
        and hmac.compare_digest(
            str(handoff.get("signature", "")),
            mechanisms._signature(original, mechanisms._key(root)),
        )
    ):
        raise ValueError("historical descendant lacks actual known original worker result")
    return {
        "worker_id": worker_id,
        "result_path": str(result_path),
        "result_sha256": hashlib.sha256(native._bounded_bytes(result_path, MAX_BYTES)).hexdigest(),
        "bundle_ref": dict(ref),
    }


def _validate_covered(cfg, rows):
    """Validate covered historical prefixes without asserting they remain active."""
    import closer_pipeline as pipeline
    import closer_resources as resources
    import closer_stage_continuation as stages

    root = resources.scoped_root(Path(str(cfg["dispatch_state"]) + ".pipeline.json"))
    if not isinstance(rows, list) or len(rows) > MAX_LEAVES * 8:
        raise ValueError("descendant coverage exceeds bound")
    for covered in rows:
        target = covered["target"]
        if target not in cfg["finite_frontier"]["targets"]:
            raise ValueError("descendant covered foreign target")
        shard = resources.shard_dir(root, target) / "state.json"
        actions = shard.parent / "state.actions" / (pipeline._digest(target) + ".json")
        leaf = stages.active_leaf(shard, actions, cfg["finite_frontier"], target)
        chain = leaf["chain"]
        matches = [
            i
            for i, hop in enumerate(chain)
            if hop["record"]["finite_frontier"] == covered["frontier"]
        ]
        if len(matches) != 1:
            raise ValueError("descendant historical lineage no longer exists")
        links = [
            {key: value for key, value in part["record"].items() if key != "receipt_refs"}
            for part in chain[: matches[0] + 1]
        ]
        state = pipeline._load(shard)
        entry = state["frontiers"][covered["frontier"]["frontier_id"]]
        row = entry["items"][target]
        receipt, _verdict = _receipt(cfg, covered["receipt_ref"], [target], covered["frontier"])
        if (
            covered["source_row_sha256"] != _digest(row)
            or covered["chain_sha256"] != _digest(links)
            or receipt["targets"].get(target) != row
            or receipt["run_ids"] != {target: entry["run_id"]}
        ):
            raise ValueError("descendant historical receipt/source/lineage changed")


def observed_leaves(cfg, ref, *, require_finalized=True):
    """Read actual terminal-worker evidence, never a self-signed bundle alone.

    Returns every covered prefix; consumers select active rows. UNKNOWN is only
    observable with require_finalized=False, and never supplies historical release.
    """
    record = _read_signed(cfg, ref)
    if record.get("schema") == FINAL_SCHEMA:
        sealed = _read_signed(cfg, record["sealed_bundle"])
        if sealed.get("schema") != SCHEMA or record.get("sealed_sha256") != _digest(sealed):
            raise ValueError("descendant finalized bundle no longer binds original receipt")
        if any(item["domain"] == "unknown" for item in sealed["leaves"]):
            raise ValueError("UNKNOWN descendant is not finalized continuation authority")
    else:
        if require_finalized or record.get("schema") != SCHEMA:
            raise ValueError("descendant bundle has no actual cleanup finalization")
        sealed = record
    source = _actual_known_worker(cfg, record, ref, allow_unknown=not require_finalized)
    _validate_covered(cfg, sealed["leaves"])
    return [{**copy.deepcopy(row), "source_worker": source} for row in sealed["leaves"]]


def _history(cfg):
    import closer_campaign as campaign
    import closer_native_observer as native

    worker = cfg["native_worker"]
    doc = campaign.Campaign(_physical(worker["state"])).read()
    if (
        not isinstance(doc, Mapping)
        or not isinstance(doc.get("workers"), list)
        or len(doc["workers"]) > MAX_LEAVES
        or any(not isinstance(item, Mapping) for item in doc["workers"])
    ):
        raise ValueError("descendant worker history exceeds bound")
    leaves = []
    for prior in doc.get("workers", []):
        if prior.get("state") != "terminal" or prior.get("outcome") not in {
            "ok",
            "scoped_continuation",
            "unavailable",
        }:
            continue
        path = campaign.result_path(Path(worker["state"]), prior["worker_id"])
        if not path.exists():
            continue
        try:
            result = native._read(_physical(path))
            detail = json.loads(result["detail"])
            ref = detail.get("descendant_bundle")
            if isinstance(ref, Mapping):
                leaves.extend(observed_leaves(cfg, ref))
        except (OSError, ValueError, TypeError, KeyError):
            continue  # Missing historical proof never supplies a release.
    return leaves


def seal_bundle(cfg, candidate, watermark):
    """Called only after unchanged TOP native-hook checks, before actual C."""
    refs = candidate.get("descendant_receipts", [])
    if not isinstance(refs, list) or any(not isinstance(ref, Mapping) for ref in refs):
        raise ValueError("descendant receipt index is malformed")
    # Existing root-only native paths have no descendant authority to seal.
    if cfg.get("finite_frontier") is None:
        if refs:
            raise ValueError("descendant receipt requires original finite cohort")
        return None
    if not refs and not _leaves(cfg, [], frozenset(), []):
        return None
    manifest, context, root, cohort = _authority(cfg)
    root_receipt, _verdict = _root(cfg, candidate)
    if root_receipt["round_id"] in watermark[2]:
        raise ValueError("descendant TOP root predates current B")
    watermark_path = _physical(watermark[0])
    if (
        watermark_path.read_bytes() != watermark[1]
        or watermark_path.parent != root / context["nonce"]
    ):
        raise ValueError("descendant original B watermark changed")
    refs = candidate.get("descendant_receipts", [])
    history = _history(cfg) if any(ref.get("round_id") in watermark[2] for ref in refs) else []
    leaves = _leaves(cfg, refs, watermark[2], history)
    if not leaves:
        return None
    record = {
        "schema": SCHEMA,
        "cohort": cohort,
        "admission_nonce": context["nonce"],
        "admission": context,
        "worker": dict(cfg["native_worker"]),
        "root_receipt": {"assignment": candidate["assignment"], **_ref(candidate)},
        "watermark": {
            "path": str(watermark_path),
            "sha256": hashlib.sha256(watermark[1]).hexdigest(),
        },
        "leaves": leaves,
        "aggregate": "unknown"
        if any(row["domain"] == "unknown" for row in leaves)
        else "known_held"
        if any(row["active"] and row["domain"] == "known_held" for row in leaves)
        else "completed",
        "authority": False,
    }
    return _write_signed(root / context["nonce"] / "descendant-receipts.json", record, root)


def bundle_record(cfg, ref):
    """Revalidate the signed current-bundle observation and exact leaf bytes."""
    value = _read_signed(cfg, ref)
    if value.get("schema") == FINAL_SCHEMA:
        value = _read_signed(cfg, value["sealed_bundle"])
    if value.get("schema") != SCHEMA:
        raise ValueError("descendant receipt bundle schema differs")
    _root(cfg, value["root_receipt"])
    refs = [row["receipt_ref"] for row in value["leaves"]]
    checked = _leaves(cfg, refs, frozenset(), [])
    if checked != value["leaves"]:
        raise ValueError("descendant source/receipt/lineage changed after observation")
    return value


def finalize_bundle(cfg, ref, cleanup, token):
    """Seal only actual exact-token C cleanup; UNKNOWN can never become historical."""
    record = bundle_record(cfg, ref)
    if record["aggregate"] == "unknown":
        return ref
    token_sha = hashlib.sha256(token.encode()).hexdigest()
    inode = int(token.split()[-1].rsplit(":", 1)[1])
    if not isinstance(cleanup, Mapping) or not (
        cleanup.get("schema") == "closer-native-lease-cleanup/v1"
        and cleanup.get("released") is True
        and cleanup.get("exact_token_matched") is True
        and cleanup.get("expected_token_sha256") == cleanup.get("meta_sha256") == token_sha
        and type(cleanup.get("released_lease_inode")) is int
        and cleanup.get("released_lease_inode") == inode
        and inode > 0
        and cleanup.get("lane") == "closer"
        and cleanup.get("lane_dir")
        == str(
            Path(
                cfg.get("handoff_dir")
                or os.environ.get("HANDOFF_DIR")
                or Path.home() / ".codex/handoff"
            )
            / ".lane-closer.lock"
        )
        and type(cleanup.get("released_at")) in (int, float)
        and math.isfinite(cleanup["released_at"])
    ):
        raise ValueError("descendant bundle lacks actual exact-token native cleanup")
    _manifest, context, root, _cohort = _authority(cfg)
    finalized = {
        "schema": FINAL_SCHEMA,
        "cohort": record["cohort"],
        "admission_nonce": context["nonce"],
        "admission": context,
        "worker": record["worker"],
        "sealed_bundle": ref,
        "sealed_sha256": _digest(record),
        "cleanup": dict(cleanup),
        "authority": False,
    }
    return _write_signed(
        root / context["nonce"] / "descendant-receipts-finalized.json", finalized, root
    )


def continuation_proof(cfg, candidate):
    """Aggregate exact known-held descendants, preserving full root checks."""
    ref = candidate.get("descendant_bundle")
    if not isinstance(ref, Mapping):
        return None
    try:
        if _read_signed(cfg, ref).get("schema") != FINAL_SCHEMA:
            return None
        record = bundle_record(cfg, ref)
        _root_receipt, root_verdict = _root(cfg, record["root_receipt"])
        if record["aggregate"] != "known_held" or not root_verdict.get("admitted_domain_valid"):
            return None
        handoff = Path(
            cfg.get("handoff_dir")
            or os.environ.get("HANDOFF_DIR")
            or Path.home() / ".codex/handoff"
        )
        if (handoff / ".lane-closer.lock").exists() or (handoff / ".lane-closer.lock").is_symlink():
            return None
        proof = {
            "schema": CONTINUATION_SCHEMA,
            **record["root_receipt"],
            "assignment_sha256": record["cohort"]["assignment_sha256"],
            "descendant_bundle": dict(ref),
            "domain_outcome": "known",
            "held_targets": [
                row["target"]
                for row in record["leaves"]
                if row["active"] and row["domain"] == "known_held"
            ],
            "accepted": False,
            "global_stop_valid": False,
        }
        if candidate.get("schema") == CONTINUATION_SCHEMA and candidate != proof:
            return None
        return proof
    except (OSError, ValueError, KeyError, TypeError, RuntimeError):
        return None
