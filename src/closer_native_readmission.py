"""One-use fresh finite campaigns from authenticated no-B evidence.

This module never changes the retained campaign or its unavailable/UNKNOWN
result. It only records a new current plan and creates a distinct campaign;
the later native entry still performs its ordinary admission and capacity gates.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import sys
import time
from pathlib import Path

import closer_host_restore_contract as host


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
        state_raw = host._read(state_path)[1]
        old = _json(state_raw, "original campaign")
        worker = next(
            (row for row in old.get("workers", []) if row.get("worker_id") == worker_id), None
        )
        if not isinstance(worker, dict):
            raise ValueError("worker absent")
        if worker.get("state") != "terminal" or old.get("targets") != {
            campaign.ROUND_UNIT: "reconcile"
        }:
            raise ValueError("original worker still owns its attempt")
        if worker.get("pid"):
            host._dead(worker["pid"])
        result_path = campaign.result_path(state_path, worker_id)
        raw = host._read(result_path)[1]
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
    import closer_runtime_binding as binding

    manifest_path = _canonical(manifest_path)
    manifest_raw = manifest_path.read_bytes()
    manifest = campaign.load_manifest(manifest_path)
    native.contract(manifest)
    for module in (native, binding):
        source = Path(module.__file__).resolve()
        if manifest.get("modules", {}).get(source.name) != _digest(host._read(source)[1]):
            raise ReadmissionHeld("new runtime does not contain the current native refusal repair")
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
    with host._hold(
        [
            {"path": str(root / name), "must_exist": True}
            for name in ("native-entry.lock", "native-supervisor.lock")
        ]
    ):
        yield


def _guard_fresh(manifest, cfg, destination, fresh, old_frontier):
    controlled = manifest["mechanism_contract"].get("controller_demand") is not None
    same_bindings = fresh.get("bindings") == old_frontier.get("bindings")
    same_revisions = {target: row["revision"] for target, row in fresh["bindings"].items()} == {
        target: row["revision"] for target, row in old_frontier["bindings"].items()
    }
    if fresh["targets"] != old_frontier["targets"] or not (
        same_bindings or (controlled and same_revisions)
    ):
        raise ReadmissionHeld("fresh finite frontier target scope differs")
    if fresh["frontier_id"] == old_frontier["frontier_id"]:
        raise ReadmissionHeld("fresh frontier reuses consumed finite authority")
    if destination.parent != _canonical(manifest["mechanism_contract"]["campaign_root"]):
        raise ReadmissionHeld("new campaign is outside the current native root")
    if not destination.name.endswith(".campaign.json"):
        raise ReadmissionHeld("new native campaign requires a campaign.json filename")
    _initialization(manifest, destination)
    if controlled and (
        fresh["owner"] != old_frontier["owner"]
        or manifest["mechanism_contract"]["controller_demand"]["owner"] != fresh["owner"]
    ):
        raise ReadmissionHeld("controlled finite frontier owner changed")
    if cfg.get("granularity", "round") != "round" or cfg.get("mechanism") != "native-handoff/v1":
        raise ReadmissionHeld("fresh campaign requires the native whole-round mechanism")
    host._lane_absent()
    import claims

    if any(claims.holder(target) for target in fresh["targets"]):
        raise ReadmissionHeld("current target writer is still held")


def _initialization(manifest, destination):
    demand = manifest["mechanism_contract"].get("controller_demand")
    if demand is None:
        return None
    config = (manifest.get("policy", {}).get("correction_controller_installation") or {}).get(
        "config"
    )
    if (
        not isinstance(demand, dict)
        or demand.get("schema") != "closer-controller-demand/v1"
        or demand.get("campaign_state") != str(destination)
        or any(
            not isinstance(demand.get(key), str) or not demand[key]
            for key in ("campaign_id", "owner")
        )
        or not isinstance(config, dict)
        or config.get("state_path") != str(destination)
        or not isinstance(config.get("bootstrap_anchor_sha256"), str)
        or len(config["bootstrap_anchor_sha256"]) != 64
    ):
        raise ReadmissionHeld("controlled campaign bootstrap identity/config differs")
    return {
        "hold": {
            "kind": "source_initialization",
            "reason": "actual native qualification and qualified source demand not yet admitted",
            "bootstrap_anchor_sha256": config["bootstrap_anchor_sha256"],
        },
        "correction_controller": {"config": config},
    }


def _begin_fresh(campaign_module, destination, manifest, cfg, fresh, old, plan_sha256):
    initialization = _initialization(manifest, destination)
    demand = manifest["mechanism_contract"].get("controller_demand") or {}
    return campaign_module.Campaign(destination).begin(
        demand.get("campaign_id", old["campaign_id"] + ":readmission:" + plan_sha256[:12]),
        list(fresh["targets"]),
        owner=demand.get("owner", old["owner"]),
        release_pin=manifest["package_digest"],
        worker_config={**cfg, "finite_frontier": fresh},
        _source_initialization=initialization,
    )


def admit_initialized(manifest_path, state_path, assignment_bytes, request):
    """Admit qualified typed demand while atomically clearing only the initial hold."""
    import closer_campaign as campaign
    import closer_correction_controller as controller
    import closer_runtime_binding as binding
    import closer_source_installation as installation

    manifest_path = _canonical(manifest_path)
    state_path = _canonical(state_path)
    manifest = campaign.load_manifest(manifest_path)
    manifest_raw = host._read(manifest_path)[1]
    initialization = _initialization(manifest, state_path)
    if initialization is None:
        raise ReadmissionHeld("initial admission requires a controlled source campaign")
    coord = Path.home() / ".codex/handoff/.lane-closer.lock.coord.lock"
    inspection = controller.inspect_provisioned_custody(
        initialization["correction_controller"]["config"]
    )
    custody_writer = Path(inspection["identity"]["custody_path"]) / "writer.lock"
    with _hold_rows(
        _new_lock_rows(manifest)
        + [
            {"path": str(coord), "must_exist": True},
            {"path": str(custody_writer), "must_exist": True},
        ]
    ):
        host._lane_absent()
        if host._read(manifest_path)[1] != manifest_raw:
            raise ReadmissionHeld("manifest changed before source demand admission")
        current_inspection = controller.inspect_provisioned_custody(
            initialization["correction_controller"]["config"]
        )
        if current_inspection != inspection or inspection["highwater"]["pending_transactions"]:
            raise ReadmissionHeld("current controller custody changed or has pending work")
        problems = campaign.qualify(manifest, manifest_path.parent)
        if problems:
            raise ReadmissionHeld("current native qualification failed: " + repr(problems))
        runtime_binding = binding.binding_from_manifest(manifest_path)
        projection = installation.controller_policy_projection(runtime_binding)
        if (
            not isinstance(projection, dict)
            or projection.get("purpose") != "frontier-correlation-only"
        ):
            raise ReadmissionHeld("qualified source installation projection is unavailable")
        current = campaign.Campaign(state_path).read()
        if current is None:
            raise ReadmissionHeld("one-use fresh preparation has not created the initial campaign")
        if request.get("targets") != current.get("assignment_targets"):
            raise ReadmissionHeld("completed demand differs from the exact finite target scope")
        import closer_dispatcher as dispatcher
        import closer_frontier_policy as policy
        import closer_queue as queue

        cfg = current["worker_config"]
        frontier = cfg["finite_frontier"]
        backend = dispatcher.Closer(
            dispatcher.GitHub(deadline=time.time() + 30),
            runtime_binding=runtime_binding,
            state_path=cfg["dispatch_state"],
            apply=False,
            workers=True,
        )
        observations = {
            target: queue._observation(backend.observe(target), target)
            for target in current["assignment_targets"]
        }
        observed_frontier = policy.make_frontier_from_observations(
            observations, owner=frontier["owner"], frontier_id=frontier["frontier_id"]
        )
        if observed_frontier != frontier:
            raise ReadmissionHeld(
                "current qualified source observations differ from the prepared frontier"
            )
        if host._read(manifest_path)[1] != manifest_raw:
            raise ReadmissionHeld("manifest changed during source demand admission")
        demand = manifest["mechanism_contract"]["controller_demand"]
        root = _canonical(manifest["mechanism_contract"]["observation_root"])
        package = {
            "package_digest": manifest["package_digest"],
            "manifest_sha256": _digest(host._read(manifest_path)[1]),
            "qualification_digest": _digest(host._read(root / "native-qualification.json")[1]),
        }
        admitted = campaign.Campaign(state_path).begin(
            demand["campaign_id"],
            current["assignment_targets"],
            owner=demand["owner"],
            release_pin=manifest["package_digest"],
            worker_config=current["worker_config"],
            demand_assignment_bytes=assignment_bytes,
            demand_request=request,
            qualified_package=package,
            _source_initialization=initialization,
        )
    return {
        "authority": False,
        "status": "source_demand_admitted",
        "state": str(state_path),
        "revision": admitted["revision"],
        "request_id": admitted["demand_admission"]["request_id"],
    }


def _new_lock_rows(manifest):
    root = _canonical(manifest["mechanism_contract"]["observation_root"])
    return [
        {"path": str(root / name), "must_exist": True}
        for name in ("native-entry.lock", "native-supervisor.lock")
    ]


def _hold_rows(rows):
    merged = {}
    for row in rows:
        path = str(_canonical(row["path"]))
        merged[path] = merged.get(path, False) or row["must_exist"]
    return host._hold([{"path": path, "must_exist": merged[path]} for path in sorted(merged)])


def _plan(reference, new_manifest_path, new_manifest_raw, cfg, destination, frontier):
    payload = {
        "schema": "closer-native-readmission-plan/v1",
        "producer_sha256": _digest(host._read(Path(__file__).resolve())[1]),
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
            "worker_config_sha256": _digest(
                json.dumps({**cfg, "finite_frontier": frontier}, sort_keys=True).encode()
            ),
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
    confirm_plan_sha256=None,
    _plan_only=False,
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
    destination = _canonical(new_state)
    _guard_fresh(new_manifest, cfg, destination, fresh, old_frontier)
    if destination.exists():
        raise ReadmissionHeld("new campaign path must be absent")
    plan, plan_sha256 = _plan(
        reference, new_manifest_path, new_manifest_raw, cfg, destination, fresh
    )
    if _plan_only:
        return {"plan": plan, "plan_sha256": plan_sha256, "authority": False}
    if confirm_plan_sha256 != plan_sha256:
        raise ReadmissionHeld("explicit confirmation does not bind the complete fresh plan")

    old_root = native.contract(reference["manifest"])[3]
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

    rows = (
        _new_lock_rows(reference["manifest"])
        + _new_lock_rows(new_manifest)
        + [
            {"path": str(reference["state_path"]) + ".lock", "must_exist": True},
            {
                "path": str(Path.home() / ".codex/handoff/.lane-closer.lock.coord.lock"),
                "must_exist": True,
            },
        ]
    )
    with _hold_rows(rows):
        renewed = prospective_reference(manifest_path, campaign_state, worker_id)
        _guard_fresh(new_manifest, cfg, destination, fresh, old_frontier)
        if destination.exists():
            raise ReadmissionHeld("new campaign path must be absent")
        if _new_runtime(new_manifest_path, cfg)[2] != new_manifest_raw:
            raise ReadmissionHeld("new runtime changed before consumption")
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
        _begin_fresh(
            campaign, destination, new_manifest, cfg, fresh, reference["state"], plan_sha256
        )
    return {
        "reservation": str(reservation),
        "new_state": str(destination),
        "plan_sha256": plan_sha256,
        "authority": False,
    }


def plan_prospective(*args, **kwargs):
    return apply_prospective(*args, **kwargs, _plan_only=True)


def plan_historical(*args, **kwargs):
    return apply_historical(*args, **kwargs, _plan_only=True)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "admit":
        parser = argparse.ArgumentParser(description=admit_initialized.__doc__)
        for name in ("manifest", "state", "assignment", "request"):
            parser.add_argument("--" + name, required=True, type=Path)
        args = parser.parse_args(argv[1:])
        try:
            result = admit_initialized(
                args.manifest,
                args.state,
                host._read(args.assignment)[1],
                host._obj(args.request)[2],
            )
        except (OSError, RuntimeError, ValueError, KeyError, TypeError) as exc:
            print(json.dumps({"status": "held", "authority": False, "reason": str(exc)}))
            return 2
        print(json.dumps(result, sort_keys=True))
        return 0
    if argv == ["--selftest"]:
        first = {"schema": "closer-native-readmission-plan/v1", "authority": False, "new": "first"}
        second = {**first, "new": "second"}
        assert _digest(json.dumps(first, sort_keys=True).encode()) != _digest(
            json.dumps(second, sort_keys=True).encode()
        )
        try:
            _json(b"[]", "selftest")
        except ReadmissionHeld:
            pass
        else:
            raise AssertionError("non-object plan accepted")
        print(json.dumps({"selftest": "ok", "authority": False}))
        return 0
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("plan", "apply"))
    parser.add_argument("--kind", choices=("prospective", "historical"), required=True)
    for name in (
        "manifest",
        "campaign",
        "new-manifest",
        "new-worker-config",
        "new-state",
        "frontier",
    ):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--worker", required=True)
    parser.add_argument("--reconciliation", type=Path)
    parser.add_argument("--expected-plan-sha256")
    args = parser.parse_args(argv)
    if args.mode == "apply" and not args.expected_plan_sha256:
        parser.error("apply requires --expected-plan-sha256")
    if args.kind == "historical" and not args.reconciliation:
        parser.error("historical requires --reconciliation")
    try:
        cfg = host._obj(args.new_worker_config)[2]
        frontier = host._obj(args.frontier)[2]
        inputs = [
            args.manifest,
            args.campaign,
            args.worker,
            args.new_manifest,
            cfg,
            args.new_state,
            frontier,
        ]
        if args.kind == "historical":
            inputs.insert(0, host._obj(args.reconciliation)[2])
        function = apply_historical if args.kind == "historical" else apply_prospective
        result = function(
            *inputs, confirm_plan_sha256=args.expected_plan_sha256, _plan_only=args.mode == "plan"
        )
    except (ReadmissionHeld, OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
        print(json.dumps({"status": "held", "authority": False, "reason": str(exc)}))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


def historical_reference(reference, manifest_path, campaign_state, worker_id):
    """Strict deferred reader for the separately reviewed reconciliation producer."""
    from closer_retained_reconciliation import validate

    try:
        record = validate(reference, manifest_path, campaign_state, worker_id)
    except (OSError, ValueError, RuntimeError) as exc:
        raise ReadmissionHeld(
            "retained reconciliation is no longer current or already consumed"
        ) from exc
    if not isinstance(record, dict):
        raise ReadmissionHeld("retained reconciliation is not an object")
    decision = record.get("decision") if isinstance(record, dict) else None
    if (
        record.get("schema") != "native-retained-no-b-reconciliation/v1"
        or record.get("authority") is not False
        or not isinstance(decision, dict)
        or decision.get("status") != "confirmed_no_b_current_observation"
        or decision.get("authorize_fresh_prepare") is not True
        or decision.get("old_outcome") != "unknown_preserved"
        or type(decision.get("max_fresh_generations")) is not int
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
    confirm_plan_sha256=None,
    _plan_only=False,
):
    """Consume a sealed current no-B reconciliation into one fresh campaign.

    The reconciliation producer owns the comprehensive old custody/lane lock
    inventory.  This consumer acquires precisely that inventory before it calls
    its validator; it does not reinterpret an old UNKNOWN as a no-start proof.
    """
    import closer_campaign as campaign
    import closer_native_observer as native
    import closer_retained_reconciliation as reconciliation
    from closer_frontier_policy import validate_frontier

    new_manifest_path, new_manifest, new_manifest_raw, cfg = _new_runtime(
        new_manifest_path, new_worker_config
    )
    destination = _canonical(new_state)
    if destination.exists():
        raise ReadmissionHeld("new campaign path must be absent")
    with _hold_rows(
        reconciliation.lock_rows(manifest_path, campaign_state) + _new_lock_rows(new_manifest)
    ):
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
            ):
                raise ValueError("retained reconciliation no longer joins current bytes")
        except (KeyError, OSError, StopIteration, TypeError, ValueError) as exc:
            raise ReadmissionHeld("retained reconciliation cannot bind a fresh campaign") from exc
        _guard_fresh(new_manifest, cfg, destination, fresh, old_frontier)
        if destination.exists():
            raise ReadmissionHeld("new campaign path must be absent")
        old_manifest = _json(host._read(_canonical(manifest_path))[1], "historical manifest")
        if old_manifest["package_digest"] == new_manifest["package_digest"]:
            raise ReadmissionHeld("historical readmission requires the repaired runtime")
        plan = {
            "schema": "closer-native-readmission-plan/v1",
            "producer_sha256": _digest(host._read(Path(__file__).resolve())[1]),
            "authority": False,
            "reconciliation": reference,
            "old_result_sha256": original["result"]["sha256"],
            "new_manifest": str(new_manifest_path),
            "new_manifest_sha256": _digest(new_manifest_raw),
            "new_worker_config_sha256": _digest(
                json.dumps({**cfg, "finite_frontier": fresh}, sort_keys=True).encode()
            ),
            "new_state": str(destination),
            "finite_frontier": fresh,
            "max_fresh_generations": 1,
        }
        plan_sha256 = _digest(json.dumps(plan, sort_keys=True, separators=(",", ":")).encode())
        if _plan_only:
            return {"plan": plan, "plan_sha256": plan_sha256, "authority": False}
        if confirm_plan_sha256 != plan_sha256:
            raise ReadmissionHeld("explicit confirmation does not bind the complete fresh plan")
        if _new_runtime(new_manifest_path, cfg)[2] != new_manifest_raw:
            raise ReadmissionHeld("new runtime changed before consumption")
        root = _canonical(old_manifest["mechanism_contract"]["observation_root"])
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
        _begin_fresh(campaign, destination, new_manifest, cfg, fresh, old, plan_sha256)
    return {
        "reservation": str(reservation),
        "new_state": str(destination),
        "plan_sha256": plan_sha256,
        "authority": False,
    }


if __name__ == "__main__":
    raise SystemExit(main())
