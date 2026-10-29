"""One-use fresh finite campaigns from authenticated no-B evidence.

This module never changes the retained campaign or its unavailable/UNKNOWN
result. It only records a new current plan and creates a distinct campaign;
the later native entry still performs its ordinary admission and capacity gates.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
from pathlib import Path


class ReadmissionHeld(RuntimeError):
    """The evidence is not sufficient for a new finite generation."""


def _digest(raw):
    return hashlib.sha256(raw).hexdigest()


def _canonical(path):
    path = Path(path).absolute()
    if path.is_symlink() or path.resolve() != path:
        raise ReadmissionHeld("aliased path is not accepted")
    return path


def _json(raw, what):
    try:
        value = json.loads(raw)
    except (TypeError, ValueError, UnicodeDecodeError) as exc:
        raise ReadmissionHeld(f"{what} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise ReadmissionHeld(f"{what} must be an object")
    return value


def prospective_reference(manifest_path, campaign_state, worker_id):
    """Authenticate a retained v2 no-start result without changing any bytes."""
    import closer_campaign as campaign
    import closer_native_observer as native

    state_path = _canonical(campaign_state)
    manifest_path = _canonical(manifest_path)
    try:
        manifest = campaign.load_manifest(manifest_path)
        state_raw = state_path.read_bytes()
        old = _json(state_raw, "original campaign")
        if campaign.Campaign(state_path).read() != old:
            raise ValueError("campaign changed while read")
        worker = next(
            (row for row in old.get("workers", []) if row.get("worker_id") == worker_id), None
        )
        if not isinstance(worker, dict):
            raise ValueError("worker absent")
        result_path = campaign.result_path(state_path, worker_id)
        raw = result_path.read_bytes()
        result = _json(raw, "original result")
        if (
            result.get("outcome") != "unavailable"
            or result.get("worker_id") != worker_id
            or result.get("target") != worker.get("target")
            or worker.get("outcome") != "unavailable"
            or worker.get("detail") != result.get("detail")
        ):
            raise ValueError("result does not exactly join its retained worker")
        cfg = native.result_admission_config(
            old["worker_config"], result["native_admission"], state_path, old, worker
        )
        proof = _json(result["detail"], "original detail").get("native_no_start")
        if (
            not isinstance(proof, dict)
            or proof.get("schema") != "native-pre-dispatch-no-start/v2"
            or native.pre_dispatch_no_start_proof(manifest, cfg, proof) != proof
        ):
            raise ValueError("v2 no-start proof does not validate")
    except (KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
        raise ReadmissionHeld("original result lacks signed prospective v2 no-start proof") from exc
    return {
        "manifest": manifest,
        "manifest_path": str(manifest_path),
        "state": old,
        "state_path": str(state_path),
        "state_sha256": _digest(state_raw),
        "worker": worker,
        "result_path": str(result_path),
        "result": result,
        "result_raw": raw,
        "result_sha256": _digest(raw),
        "proof": proof,
    }


def _new_runtime(manifest_path, worker_config):
    """Bind a plan to a newly supplied source package, never the old runtime."""
    import closer_campaign as campaign
    import closer_native_observer as native

    manifest_path = _canonical(manifest_path)
    manifest_raw = manifest_path.read_bytes()
    manifest = campaign.load_manifest(manifest_path)
    native.contract(manifest)
    if not isinstance(worker_config, dict):
        raise ReadmissionHeld("new worker configuration is required")
    cfg = json.loads(json.dumps(worker_config, sort_keys=True))
    configured = cfg.get("runtime_manifest")
    if not isinstance(configured, str) or _canonical(configured) != manifest_path:
        raise ReadmissionHeld("new worker configuration does not pin the new manifest")
    return manifest_path, manifest, manifest_raw, cfg


@contextlib.contextmanager
def _native_locks(root):
    """Match native-entry's locks before re-reading evidence and consuming it."""
    root = _canonical(root)
    streams = []
    try:
        for name in ("native-supervisor.lock", "native-entry.lock"):
            stream = (root / name).open("a+")
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            streams.append(stream)
        yield
    finally:
        for stream in reversed(streams):
            stream.close()


def _plan(reference, new_manifest_path, new_manifest_raw, cfg, destination, frontier):
    payload = {
        "schema": "closer-native-readmission-plan/v1",
        "authority": False,
        "original": {
            "state": reference["state_path"],
            "state_sha256": reference["state_sha256"],
            "worker_id": reference["worker"]["worker_id"],
            "result": reference["result_path"],
            "result_sha256": reference["result_sha256"],
            "proof_sha256": _digest(json.dumps(reference["proof"], sort_keys=True).encode()),
        },
        "new": {
            "manifest": str(new_manifest_path),
            "manifest_sha256": _digest(new_manifest_raw),
            "worker_config_sha256": _digest(json.dumps(cfg, sort_keys=True).encode()),
            "state": str(destination),
            "finite_frontier": frontier,
        },
        "max_fresh_generations": 1,
    }
    return payload, _digest(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())


def apply_prospective(
    manifest_path,
    campaign_state,
    worker_id,
    new_manifest_path,
    new_worker_config,
    new_state,
    frontier,
    *,
    confirm_plan_sha256,
):
    """Consume one signed v2 no-start proof and create a fresh campaign record.

    This has no admission authority. The new campaign must subsequently pass
    normal native-entry, capacity, ownership, and effect checks under its new
    manifest. The retained source campaign/result are read-only evidence.
    """
    import closer_campaign as campaign
    import closer_native_observer as native
    from closer_frontier_policy import validate_frontier

    reference = prospective_reference(manifest_path, campaign_state, worker_id)
    new_manifest_path, new_manifest, new_manifest_raw, cfg = _new_runtime(
        new_manifest_path, new_worker_config
    )
    try:
        fresh = validate_frontier(frontier, targets=frontier.get("targets"))
        old_frontier = validate_frontier(reference["state"]["worker_config"]["finite_frontier"])
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise ReadmissionHeld("fresh finite frontier is invalid") from exc
    if fresh["targets"] != old_frontier["targets"]:
        raise ReadmissionHeld("fresh finite frontier target scope differs")
    if fresh == old_frontier:
        raise ReadmissionHeld("fresh frontier reuses consumed finite authority")
    destination = _canonical(new_state)
    if destination.exists():
        raise ReadmissionHeld("new campaign path must be absent")
    plan, plan_sha256 = _plan(
        reference, new_manifest_path, new_manifest_raw, cfg, destination, fresh
    )
    if confirm_plan_sha256 != plan_sha256:
        raise ReadmissionHeld("explicit confirmation does not bind the complete fresh plan")

    old_root = native.contract(reference["manifest"])[3]
    new_root = native.contract(new_manifest)[3]
    nonce = reference["proof"].get("admission_nonce")
    if not isinstance(nonce, str) or not nonce:
        raise ReadmissionHeld("proof has no admission identity")
    reservation = old_root / nonce / ("readmission-" + reference["result_sha256"] + ".json")
    token = {
        "schema": "closer-native-readmission/v1",
        "authority": False,
        "plan": plan,
        "plan_sha256": plan_sha256,
    }

    old_campaign = campaign.Campaign(reference["state_path"])
    with old_campaign._locked():
        with contextlib.ExitStack() as stack:
            for root in sorted({old_root, new_root}, key=str):
                stack.enter_context(_native_locks(root))
            renewed = prospective_reference(manifest_path, campaign_state, worker_id)
            if (
                renewed["state_sha256"] != reference["state_sha256"]
                or renewed["result_sha256"] != reference["result_sha256"]
                or renewed["proof"] != reference["proof"]
            ):
                raise ReadmissionHeld("retained evidence changed before consumption")
            try:
                native._immutable(reservation, json.dumps(token, sort_keys=True).encode())
            except FileExistsError as exc:
                raise ReadmissionHeld("prospective proof already consumed") from exc
            campaign.Campaign(destination).begin(
                reference["state"]["campaign_id"] + ":readmission:" + plan_sha256[:12],
                list(fresh["targets"]),
                owner=reference["state"]["owner"],
                release_pin=new_manifest["package_digest"],
                worker_config={**cfg, "finite_frontier": fresh},
            )
    return {
        "reservation": str(reservation),
        "new_state": str(destination),
        "plan_sha256": plan_sha256,
        "authority": False,
    }


def historical_reference(reference, manifest_path, campaign_state, worker_id):
    """Strict deferred reader for the separately reviewed reconciliation producer."""
    from closer_retained_reconciliation import validate

    record = validate(reference, manifest_path, campaign_state, worker_id)
    decision = record.get("decision") if isinstance(record, dict) else None
    if (
        record.get("schema") != "native-retained-no-b-reconciliation/v1"
        or record.get("authority") is not False
        or not isinstance(decision, dict)
        or decision.get("status") != "confirmed_no_b_current_observation"
        or decision.get("authorize_fresh_prepare") is not True
        or decision.get("old_outcome") != "unknown_preserved"
        or decision.get("max_fresh_generations") != 1
    ):
        raise ReadmissionHeld("retained reconciliation does not authorize one fresh preparation")
    return record


def apply_historical(
    reference,
    manifest_path,
    campaign_state,
    worker_id,
    new_manifest_path,
    new_worker_config,
    new_state,
    frontier,
    *,
    confirm_plan_sha256,
):
    """Consume a sealed current no-B reconciliation into one fresh campaign.

    The reconciliation producer owns the comprehensive old custody/lane lock
    inventory.  This consumer acquires precisely that inventory before it calls
    its validator; it does not reinterpret an old UNKNOWN as a no-start proof.
    """
    import closer_retained_reconciliation as reconciliation

    import closer_campaign as campaign
    import closer_native_observer as native
    from closer_frontier_policy import validate_frontier

    new_manifest_path, new_manifest, new_manifest_raw, cfg = _new_runtime(
        new_manifest_path, new_worker_config
    )
    destination = _canonical(new_state)
    if destination.exists():
        raise ReadmissionHeld("new campaign path must be absent")
    with reconciliation.host._hold(reconciliation.lock_rows(manifest_path, campaign_state)):
        record = historical_reference(reference, manifest_path, campaign_state, worker_id)
        original = record.get("original", {})
        try:
            state_path = _canonical(original["state"])
            state_raw = state_path.read_bytes()
            old = _json(state_raw, "retained campaign")
            next(row for row in old["workers"] if row.get("worker_id") == worker_id)
            result_path = campaign.result_path(state_path, worker_id)
            result_raw = result_path.read_bytes()
            old_frontier = validate_frontier(original["finite_frontier"])
            fresh = validate_frontier(frontier, targets=frontier.get("targets"))
            if (
                original["worker_id"] != worker_id
                or original["result"]["sha256"] != _digest(result_raw)
                or original["revision"] != old["revision"]
                or fresh["targets"] != old_frontier["targets"]
                or fresh == old_frontier
            ):
                raise ValueError("retained reconciliation no longer joins current bytes")
        except (KeyError, OSError, StopIteration, TypeError, ValueError) as exc:
            raise ReadmissionHeld("retained reconciliation cannot bind a fresh campaign") from exc
        plan = {
            "schema": "closer-native-readmission-plan/v1",
            "authority": False,
            "reconciliation": reference,
            "old_result_sha256": original["result"]["sha256"],
            "new_manifest": str(new_manifest_path),
            "new_manifest_sha256": _digest(new_manifest_raw),
            "new_worker_config_sha256": _digest(json.dumps(cfg, sort_keys=True).encode()),
            "new_state": str(destination),
            "finite_frontier": fresh,
            "max_fresh_generations": 1,
        }
        plan_sha256 = _digest(json.dumps(plan, sort_keys=True, separators=(",", ":")).encode())
        if confirm_plan_sha256 != plan_sha256:
            raise ReadmissionHeld("explicit confirmation does not bind the complete fresh plan")
        root = native.contract(campaign.load_manifest(manifest_path))[3]
        nonce = original["admission_nonce"]
        reservation = (
            root / nonce / ("readmission-reconciled-" + original["result"]["sha256"] + ".json")
        )
        receipt = {
            "schema": "closer-native-readmission/v1",
            "authority": False,
            "plan": plan,
            "plan_sha256": plan_sha256,
        }
        try:
            native._immutable(reservation, json.dumps(receipt, sort_keys=True).encode())
        except FileExistsError as exc:
            raise ReadmissionHeld("retained reconciliation already consumed") from exc
        campaign.Campaign(destination).begin(
            old["campaign_id"] + ":readmission:" + plan_sha256[:12],
            list(fresh["targets"]),
            owner=old["owner"],
            release_pin=new_manifest["package_digest"],
            worker_config={**cfg, "finite_frontier": fresh},
        )
    return {
        "reservation": str(reservation),
        "new_state": str(destination),
        "plan_sha256": plan_sha256,
        "authority": False,
    }
