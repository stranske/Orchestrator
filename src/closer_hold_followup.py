"""Bounded follow-up consumers in the existing campaign controller.

The journal is campaign state, not a second scheduler or source of authority.
Unknown effects are observed and escalated; only audited known-no-start work
can be rearmed through normal native admission and fresh PREPARE.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, TypeGuard

SCHEMA = "closer-hold-followup/v1"
STATES = frozenset({"waiting", "checking", "released", "resolved", "consumed", "escalated"})
EXECUTION_STATES = frozenset(
    {"confirmed_not_started", "provider_outcome_unknown", "action_outcome_unknown"}
)
MAX_HOLDS = 1024
MAX_CHECKS = 2


def _number(value: Any) -> TypeGuard[int | float]:
    return type(value) in (int, float) and math.isfinite(value)


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip()) and value == value.strip()


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def policy(doc: Mapping[str, Any]) -> dict[str, Any]:
    """Scheduling bounds are not repair permits; expiry never grants authority."""
    value = (doc.get("worker_config") or {}).get("hold_followup_policy") or {}
    if not isinstance(value, Mapping) or not _text(doc.get("owner")):
        raise ValueError("hold follow-up needs the existing campaign owner")
    result = {
        "check_interval_seconds": value.get("check_interval_seconds", 900),
        "deadline_seconds": value.get("deadline_seconds", 86400),
        "attempt_budget": value.get("attempt_budget", 96),
        "escalation_owner": value.get("escalation_owner", doc["owner"]),
    }
    if not (
        _number(result["check_interval_seconds"])
        and 1 <= result["check_interval_seconds"] <= 86400
        and _number(result["deadline_seconds"])
        and result["check_interval_seconds"] <= result["deadline_seconds"] <= 30 * 86400
        and type(result["attempt_budget"]) is int
        and 1 <= result["attempt_budget"] <= 4096
        and _text(result["escalation_owner"])
    ):
        raise ValueError("invalid hold follow-up policy")
    return result


def validate_hold(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return False
    return bool(
        value.get("schema") == SCHEMA
        and all(
            _text(value.get(key))
            for key in (
                "hold_id",
                "campaign_id",
                "owner",
                "release_pin",
                "config_sha256",
                "target",
                "run_id",
                "revision",
                "stage",
                "escalation_owner",
            )
        )
        and type(value.get("generation")) is int
        and value["generation"] >= 1
        and value.get("execution_state") in EXECUTION_STATES
        and value.get("status") in STATES
        and isinstance(value.get("source"), Mapping)
        and isinstance(value.get("release_proof"), Mapping)
        and isinstance(value.get("event_selector"), Mapping)
        and _number(value.get("created_at"))
        and _number(value.get("next_check_at"))
        and _number(value.get("deadline"))
        and value["created_at"] <= value["next_check_at"]
        and value["created_at"] < value["deadline"]
        and type(value.get("attempt_budget")) is int
        and 1 <= value["attempt_budget"] <= 4096
        and type(value.get("attempts")) is int
        and 0 <= value["attempts"] <= value["attempt_budget"]
    )


def summarize_holds(doc: Mapping[str, Any], now: float | None = None) -> dict[str, Any]:
    current = time.time() if now is None else now
    rows = (doc.get("hold_followups") or {}).values()
    valid = [row for row in rows if validate_hold(row)]
    active = [row for row in valid if row["status"] in {"waiting", "checking"}]
    return {
        "active": len(active),
        "due": sum(current >= row["next_check_at"] for row in active),
        "unknown": sum(row["execution_state"] != "confirmed_not_started" for row in active),
        "escalated": sum(row["status"] == "escalated" for row in valid),
        "unowned": sum(not _text(row.get("owner")) for row in rows if isinstance(row, Mapping)),
        "oldest_age_seconds": max(
            (max(0.0, current - row["created_at"]) for row in active), default=0.0
        ),
    }


def _assignment_path():
    return (
        Path.home()
        / ".codex/automations/reviewed-repo-automation-controller/closer-assignment.json"
    )


def _paths(doc: Mapping[str, Any]) -> tuple[Path, dict[str, Any]] | None:
    import closer_dispatcher as dispatcher
    import closer_frontier_policy as frontiers
    import closer_resources as resources

    cfg = doc.get("worker_config") or {}
    if not cfg.get("finite_frontier") or not _text(cfg.get("dispatch_state")):
        return None
    frontier = frontiers.validate_frontier(
        cfg["finite_frontier"], targets=doc.get("assignment_targets")
    )
    if frontier["owner"] != doc["owner"]:
        raise ValueError("hold frontier owner differs from campaign")
    import closer_finite_reentry as reentry

    # The dispatch file need not exist yet, but its existing ancestors must be
    # physical and its absolute identity cannot silently resolve through aliases.
    dispatch = Path(cfg["dispatch_state"])
    if not dispatch.is_absolute() or dispatch != dispatch.resolve():
        raise ValueError("hold dispatch state must have a canonical absolute identity")
    ancestor = dispatch.parent
    while not ancestor.exists():
        ancestor = ancestor.parent
    reentry._physical(ancestor)
    pipeline = dispatcher._pipeline_state_path(str(dispatch))
    return resources.scoped_root(pipeline), frontier


def _source_row(record: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    import closer_finite_reentry as reentry
    import closer_resources as resources

    source = record["source"]
    path = reentry._physical(source["state_path"])
    root = path.parent.parent.parent
    if path.name != "state.json" or path.parent != resources.shard_dir(root, record["target"]):
        raise ValueError("hold source shard identity changed")
    document, _sha = reentry._read(path)
    entry = (document.get("frontiers") or {}).get(record["frontier"]["frontier_id"])
    if not isinstance(entry, dict) or entry.get("run_id") != record["run_id"]:
        raise ValueError("hold source run changed")
    if entry.get("finite_frontier") != record["frontier"]:
        raise ValueError("hold source frontier changed")
    row = (entry.get("items") or {}).get(record["target"])
    expected_revision = record["frontier"]["bindings"][record["target"]]["revision"]
    if (
        not isinstance(row, dict)
        or _digest(row) != source["row_sha256"]
        or record["revision"] != expected_revision
        or row.get("revision") not in (None, expected_revision)
    ):
        raise ValueError("hold source row or revision changed")
    return entry, row


def _boundary(row: Mapping[str, Any]) -> tuple[str, dict[str, Any]] | None:
    results = row.get("results") or {}
    if not isinstance(results, Mapping):
        return None
    outcome = row.get("provider_outcome_uncertain")
    if isinstance(outcome, Mapping):
        run_id = (outcome.get("actual") or {}).get("run_id")
        if _text(run_id):
            return "provider_outcome_unknown", {
                "kind": "provider_terminal",
                "provider_run_id": run_id,
            }
    if row.get("status") == "unknown" or row.get("stage_status") in {"unknown", "running"}:
        return "action_outcome_unknown", {"kind": "operation_terminal"}
    current = results.get(row.get("stage"), {})
    if not isinstance(current, Mapping):
        return None
    if current.get("classification") == "role_qualification_unavailable":
        return "confirmed_not_started", {
            "kind": "role_qualified",
            "role": (current.get("pending_evidence") or {}).get("role"),
        }
    if row.get("status") == "capacity_blocked":
        return "confirmed_not_started", {"kind": "capacity_available"}
    if row.get("deferred_reason") == "accepted_verifier_request":
        return "confirmed_not_started", {"kind": "accepted_verifier_terminal"}
    if current.get("pending_kind") == "ci":
        return "confirmed_not_started", {
            "kind": "ci_terminal",
            "check_run_id": (current.get("pending_evidence") or {}).get("check_run_id"),
        }
    return None


def _covered_leaves(campaign, doc, manifest=None):
    """Select only actual known native worker coverage, never nested driver JSON."""
    import closer_campaign as campaigns
    import closer_native_descendants as descendants
    import closer_native_observer as native

    if manifest is None:
        manifest = campaigns.load_manifest(doc["worker_config"]["runtime_manifest"])
    path, worker, _proof = _terminal_worker_proof(campaign, doc, manifest)
    result = native._read(path)
    cfg = native.result_admission_config(
        doc["worker_config"], result["native_admission"], campaign.path, doc, worker
    )
    detail = json.loads(result["detail"])
    ref = detail.get("descendant_bundle")
    if not isinstance(ref, Mapping):
        return {}
    covered = {}
    for leaf in descendants.observed_leaves(cfg, ref):
        if leaf.get("active") is not True:
            continue
        target = leaf.get("target")
        if target in covered or target not in doc["assignment_targets"]:
            raise ValueError("native descendant coverage is ambiguous or outside cohort")
        if leaf.get("domain") != "known_held" or not isinstance(leaf.get("source_worker"), Mapping):
            raise ValueError("native descendant lacks known original worker coverage")
        covered[target] = leaf
    return covered


def _validate_coverage_bytes(coverage):
    """Immutable original bytes only; audited reentry may have changed live row."""
    import closer_finite_reentry as reentry

    worker = coverage["source_worker"]
    for reference in (
        {"path": worker["result_path"], "sha256": worker["result_sha256"]},
        worker["bundle_ref"],
        {
            "path": coverage["receipt_ref"]["receipt_path"],
            "sha256": coverage["receipt_ref"]["receipt_sha256"],
        },
    ):
        _value, sha = reentry._read(reference["path"])
        if sha != reference["sha256"]:
            raise ValueError("original descendant authenticated bytes changed")


def _inherit_no_start_lineage(candidate, journal, doc, current):
    """Reblocked same-cohort work cannot purchase another deadline or attempts."""
    if candidate["execution_state"] != "confirmed_not_started" or candidate["release_proof"][
        "kind"
    ] not in {"capacity_available", "role_qualified"}:
        return
    previous = []
    for held in journal.values():
        if not (
            held.get("status") == "consumed"
            and held.get("execution_state") == "confirmed_not_started"
            and held.get("release_proof", {}).get("kind")
            in {"capacity_available", "role_qualified"}
            and held.get("observer_generation") == candidate["generation"]
            and all(
                held.get(key) == candidate.get(key)
                for key in (
                    "campaign_id",
                    "owner",
                    "release_pin",
                    "config_sha256",
                    "target",
                    "run_id",
                    "frontier",
                )
            )
        ):
            continue
        transitions = [
            entry
            for entry in doc.get("hold_followup_transitions", [])
            if entry.get("next_generation") == candidate["generation"]
            and entry.get("previous_generation") == held.get("generation")
            and entry.get("owner") == candidate["owner"]
            and entry.get("release_pin") == candidate["release_pin"]
            and any(
                release.get("hold_id") == held["hold_id"] for release in entry.get("releases", [])
            )
        ]
        if len(transitions) != 1:
            raise ValueError("reblocked hold lacks its exact original rearm transition")
        previous.append(held)
    if not previous:
        return
    if len(previous) != 1:
        raise ValueError("reblocked hold has conflicting original lineages")
    held = previous[0]
    candidate.update(
        lineage_id=held.get("lineage_id", held["hold_id"]),
        predecessor_hold_id=held["hold_id"],
        **{
            key: held[key]
            for key in (
                "created_at",
                "next_check_at",
                "deadline",
                "attempt_budget",
                "attempts",
                "escalation_owner",
            )
        },
    )
    if current >= candidate["deadline"] or candidate["attempts"] >= candidate["attempt_budget"]:
        candidate.update(
            status="escalated",
            escalated_at=current,
            escalation_reason="inherited_deadline_or_attempt_budget",
        )


def capture(campaign: Any, *, now: float | None = None) -> dict[str, Any]:
    """Bind actual retained pipeline rows to this campaign's durable owner."""
    import closer_finite_reentry as reentry
    import closer_resources as resources

    current = time.time() if now is None else now
    doc = campaign.read()
    if doc is None:
        raise ValueError("campaign disappeared")
    settings = policy(doc)
    located = _paths(doc)
    if located is None:
        return doc
    root, root_frontier = located
    try:
        covered = _covered_leaves(campaign, doc)
    except (OSError, ValueError, KeyError, TypeError, RuntimeError):
        covered = {}  # Missing descendant authority cannot select a raw leaf.
    candidates = []
    for target in root_frontier["targets"]:
        coverage = covered.get(target)
        frontier = coverage["frontier"] if coverage is not None else root_frontier
        path = resources.shard_dir(root, target) / "state.json"
        if not path.exists():
            continue
        state, _sha = reentry._read(path)
        entry = (state.get("frontiers") or {}).get(frontier["frontier_id"], {})
        row = (entry.get("items") or {}).get(target)
        if not isinstance(row, Mapping) or row.get("status") not in {
            "held",
            "unknown",
            "capacity_blocked",
        }:
            continue
        if coverage is not None and _digest(row) != coverage["source_row_sha256"]:
            raise ValueError("native covered descendant row changed during capture")
        boundary = _boundary(row)
        if boundary is None:
            continue
        execution, release = boundary
        run_id = entry.get("run_id")
        if not _text(run_id):
            continue
        hold_id = _digest(
            [
                doc["campaign_id"],
                doc["owner"],
                doc.get("generation", 1),
                frontier,
                target,
                run_id,
                row.get("stage"),
                release,
            ]
        )
        operation = None
        queue_path = path.parent / f"{path.stem}.actions" / f"{_digest(target)}.json"
        if queue_path.is_file():
            import closer_queue as queue

            queued = queue.state_for_frontier(
                queue._read_state(queue_path),
                frontier["frontier_id"],
                expected_policy=frontier,
                allow_missing=True,
            )
            operation = ((queued or {}).get("items") or {}).get(target, {}).get("operation")
            if not isinstance(operation, Mapping):
                operation = None
        candidates.append(
            {
                "schema": SCHEMA,
                "hold_id": hold_id,
                "campaign_id": doc["campaign_id"],
                "owner": doc["owner"],
                "release_pin": doc["release_pin"],
                "config_sha256": _digest(doc.get("worker_config") or {}),
                "generation": doc.get("generation", 1),
                "target": target,
                "run_id": run_id,
                "frontier": frontier,
                "revision": row.get("revision") or frontier["bindings"][target]["revision"],
                "stage": row.get("stage"),
                "execution_state": execution,
                "source": {
                    "state_path": str(path.resolve()),
                    "row_sha256": _digest(row),
                    "kind": "pipeline",
                    **(
                        {"descendant_coverage": copy.deepcopy(coverage)}
                        if coverage is not None
                        else {}
                    ),
                    "operation_id": (operation or {}).get("id"),
                    "operation_revision": (operation or {}).get("revision"),
                    "callback_id": row.get("work_key")
                    or _digest([run_id, target, row.get("stage")]),
                    "provider_run_id": release.get("provider_run_id"),
                },
                "release_proof": release,
                "event_selector": {
                    "kind": release["kind"],
                    "event_id": (
                        (row.get("results") or {}).get(row.get("stage"), {}).get("pending_evidence")
                        or {}
                    ).get("event_id", hold_id),
                    "target": target,
                    "revision": row.get("revision") or frontier["bindings"][target]["revision"],
                    "role": release.get("role"),
                },
                "created_at": current,
                "next_check_at": current,
                "deadline": current + settings["deadline_seconds"],
                "escalation_owner": settings["escalation_owner"],
                "attempt_budget": settings["attempt_budget"],
                "attempts": 0,
                "status": "waiting",
            }
        )

    frontier = root_frontier
    # A crashed native wrapper may never have reached a pipeline row. It still
    # receives a bounded, owned follow-up for its ACTUAL durable worker identity.
    for worker in doc.get("workers", []):
        if (
            worker.get("state") != "unknown"
            and not (worker.get("state") == "terminal" and worker.get("outcome") == "unknown")
        ) or not _text(worker.get("worker_id")):
            continue
        for target in frontier["targets"]:
            if worker.get("target") not in {target, "__round__"}:
                import closer_campaign as campaigns

                if worker.get("target") != campaigns.ROUND_UNIT:
                    continue
            hold_id = _digest([doc["campaign_id"], doc["owner"], worker["worker_id"], target])
            candidates.append(
                {
                    "schema": SCHEMA,
                    "hold_id": hold_id,
                    "campaign_id": doc["campaign_id"],
                    "owner": doc["owner"],
                    "release_pin": doc["release_pin"],
                    "config_sha256": _digest(doc.get("worker_config") or {}),
                    "generation": doc.get("generation", 1),
                    "target": target,
                    "run_id": worker["worker_id"],
                    "frontier": frontier,
                    "revision": frontier["bindings"][target]["revision"],
                    "stage": "native_worker",
                    "execution_state": "action_outcome_unknown",
                    "source": {
                        "kind": "campaign_worker",
                        "worker_id": worker["worker_id"],
                        "worker_target": worker["target"],
                        "row_sha256": _digest(worker),
                    },
                    "release_proof": {"kind": "native_worker_terminal"},
                    "event_selector": {
                        "kind": "native_worker_terminal",
                        "event_id": worker["worker_id"],
                    },
                    "created_at": current,
                    "next_check_at": current,
                    "deadline": current + settings["deadline_seconds"],
                    "escalation_owner": settings["escalation_owner"],
                    "attempt_budget": settings["attempt_budget"],
                    "attempts": 0,
                    "status": "waiting",
                }
            )

    def persist(value):
        if any(
            value.get(key) != doc.get(key)
            for key in ("owner", "generation", "release_pin", "worker_config")
        ):
            raise ValueError("campaign owner context changed during hold capture")
        journal = value.setdefault("hold_followups", {})
        if not isinstance(journal, dict) or len(journal) > MAX_HOLDS:
            raise ValueError("hold journal is invalid or exceeds bound")
        for candidate in candidates:
            if not validate_hold(candidate):
                raise ValueError("retained hold has invalid identity")
            existing = journal.get(candidate["hold_id"])
            # A cohort rearm advances observation ownership, never the original
            # identity/deadline/budget. Do not recapture unchanged older holds.
            if existing is None:
                existing = next(
                    (
                        row
                        for row in journal.values()
                        if row.get("target") == candidate["target"]
                        and row.get("run_id") == candidate["run_id"]
                        and row.get("stage") == candidate["stage"]
                        and row.get("release_proof") == candidate["release_proof"]
                        and row.get("source") == candidate["source"]
                    ),
                    None,
                )
            if existing is not None:
                if not validate_hold(existing):
                    raise ValueError("durable hold is malformed")
                continue  # Never reset original deadline, identity or attempt budget.
            if len(journal) >= MAX_HOLDS:
                raise ValueError("hold journal exceeds bound")
            _inherit_no_start_lineage(candidate, journal, value, current)
            if not validate_hold(candidate):
                raise ValueError("inherited hold bounds are malformed")
            journal[candidate["hold_id"]] = candidate

    return campaign.update(persist)


def _event_fingerprint(record: Mapping[str, Any]) -> str:
    import closer_role_qualification as roles

    if record["release_proof"]["kind"] == "role_qualified":
        return _digest(roles.ROLE_QUALIFICATIONS)
    return record["source"]["row_sha256"]


def _probe(
    record: Mapping[str, Any],
    manifest: Mapping[str, Any],
    manifest_path: str,
    doc: Mapping[str, Any],
    *,
    campaign: Any = None,
) -> dict[str, Any]:
    """Deterministic follow-up only; may settle the original Core fence via CAS.

    Never calls execute/offload/provider-submission APIs."""
    if record.get("reentry_intent") is not None:
        return _apply_no_start(record, manifest, manifest_path, doc, campaign)
    if record["source"].get("kind") == "campaign_worker":
        source = record["source"]
        workers = [
            worker
            for worker in doc.get("workers", [])
            if worker.get("worker_id") == source["worker_id"]
            and worker.get("target") == source["worker_target"]
        ]
        if len(workers) != 1:
            raise ValueError("unknown native worker identity changed")
        worker = workers[0]
        # The existing campaign reconciler authenticates native terminal proof;
        # observing its conclusion retires this watch, never replays a callback.
        terminal = worker.get("state") == "terminal" and worker.get("outcome") in {
            "ok",
            "scoped_continuation",
            "unavailable",
        }
        return {
            "status": "resolved" if terminal else "waiting",
            "reason": (
                "existing native reconciliation result retained"
                if terminal
                else "original native wrapper outcome still unknown"
            ),
            "worker_id": source["worker_id"],
            "authority": False,
        }
    _entry, row = _source_row(record)
    if record["execution_state"] == "provider_outcome_unknown":
        # No trustworthy asynchronous provider-finalization API exists here.
        # Keep the exact run owned and observable; do not invent a terminal proof.
        return {
            "status": "waiting",
            "reason": "exact provider run still needs terminal reconciliation",
            "provider_run_id": record["source"].get("provider_run_id"),
        }
    if record["execution_state"] == "action_outcome_unknown":
        return _probe_action(record, manifest_path, doc)
    kind = record["release_proof"]["kind"]
    if kind in {"role_qualified", "capacity_available"}:
        return _apply_no_start(record, manifest, manifest_path, doc, campaign)
    if kind in {"accepted_verifier_terminal", "ci_terminal"}:
        return _probe_ci(record, manifest_path, doc, row, campaign=campaign, manifest=manifest)
    return {"status": "waiting", "reason": "unsupported release proof"}


def _terminal_worker_proof(campaign: Any, doc: Mapping[str, Any], manifest: Mapping[str, Any]):
    """Validate only the actual owned last worker's original native result."""
    import closer_campaign as campaigns
    import closer_finite_reentry as reentry
    import closer_mechanisms as mechanisms
    import closer_native_observer as native
    from closer_runtime_binding import scoped_continuation_proof

    workers = doc.get("workers") or []
    if not workers or any(
        worker.get("state") != "terminal"
        or worker.get("outcome") not in {"ok", "scoped_continuation", "unavailable"}
        for worker in workers
    ):
        raise ValueError("original native worker is not conclusively terminal")
    last = workers[-1]
    path = reentry._physical(campaigns.result_path(campaign.path, last["worker_id"]))
    result = native._read(path)
    if not (
        result.get("worker_id") == last["worker_id"]
        and result.get("target") == last["target"]
        and result.get("mechanism") == mechanisms.NATIVE
        and result.get("outcome") == last["outcome"]
    ):
        raise ValueError("original native result identity differs")
    cfg = native.result_admission_config(
        doc["worker_config"], result["native_admission"], campaign.path, doc, last
    )
    detail = json.loads(result["detail"])
    proof = None
    if last["outcome"] == "scoped_continuation":
        candidate = detail.get("proof")
        if scoped_continuation_proof(cfg, candidate) == candidate:
            proof = candidate
    elif last["outcome"] == "ok":
        candidate = detail.get("completed_round")
        if native.completed_round_proof(dict(manifest), cfg, candidate) == candidate:
            proof = candidate
    else:
        candidate = detail.get("native_no_start")
        if native.pre_dispatch_no_start_proof(dict(manifest), cfg, candidate) == candidate:
            proof = candidate
    if not isinstance(proof, Mapping):
        raise ValueError("original native terminal proof is unavailable")
    return path, last, proof


def validated_receipts(
    campaign: Any, doc: Mapping[str, Any], manifest: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """Read one exact owned authenticated round receipt; never scan arbitrary files."""
    import closer_native_observer as native

    try:
        result_path, worker, proof = _terminal_worker_proof(campaign, doc, manifest)
        if proof.get("schema") not in {
            "native-completed-round/v1",
            "native-scoped-continuation/v1",
            "native-descendant-continuation/v1",
        }:
            return []
        raw = native._bounded_bytes(Path(proof["receipt_path"]), 32 * 1024 * 1024)
        if not raw or hashlib.sha256(raw).hexdigest() != proof["receipt_sha256"]:
            return []
        receipt = json.loads(raw)
        if (
            not isinstance(receipt, dict)
            or receipt.get("round_id") != proof["round_id"]
            or receipt.get("run_ids") != proof["run_ids"]
        ):
            return []
        receipts = [receipt]
        try:
            import closer_native_descendants as descendants
            import closer_resources as resources

            result = native._read(result_path)
            detail = json.loads(result["detail"])
            ref = detail.get("descendant_bundle")
            if isinstance(ref, Mapping):
                cfg = native.result_admission_config(
                    doc["worker_config"], result["native_admission"], campaign.path, doc, worker
                )
                seen = {proof["receipt_sha256"]}
                for leaf in descendants.observed_leaves(cfg, ref):
                    reference = leaf["receipt_ref"]
                    child, sha = resources.read_round_receipt(Path(reference["receipt_path"]))
                    if not (
                        sha == reference["receipt_sha256"]
                        and child.get("round_id") == reference["round_id"]
                        and child.get("run_ids") == reference["run_ids"]
                        and child.get("finite_frontier") == leaf["frontier"]
                    ):
                        raise ValueError("observed descendant receipt changed")
                    if sha not in seen:
                        receipts.append(child)
                        seen.add(sha)
        except (OSError, ValueError, RuntimeError, KeyError, TypeError):
            return [receipt]  # Partial telemetry never supplies missing branch facts.
        return receipts
    except (OSError, ValueError, RuntimeError, KeyError, TypeError):
        return []  # Missing receipt evidence remains unknown, never fabricated.


def _native_terminal_boundary(
    campaign: Any, doc: Mapping[str, Any], manifest: Mapping[str, Any], record: Mapping[str, Any]
) -> dict[str, Any]:
    """Authenticate the original terminal native worker BEFORE changing a held row."""
    import closer_campaign as campaigns
    import closer_mechanisms as mechanisms
    import closer_native_observer as native

    _assert_bound(doc, record)
    if not _probe_allowed(doc) or doc.get("hold") or doc.get("release_record"):
        raise ValueError("campaign is denied, active, or in a release transition")
    if not native.entry_active(dict(manifest)):
        raise ValueError("follow-up lacks the current observed native entry")
    if campaigns.native_barrier(dict(doc)):
        raise ValueError("native writer admission remains held")
    from claims import holder

    if holder(record["target"]):
        raise ValueError("exact target still has a live or uncertain owner")
    path, last, proof = _terminal_worker_proof(campaign, doc, manifest)
    _source_row(record)  # Exact original held row must still exist under that proof.
    coverage = record["source"].get("descendant_coverage")
    if coverage is not None:
        actual = _covered_leaves(campaign, doc, manifest).get(record["target"])
        if actual != coverage or actual.get("frontier") != record["frontier"]:
            raise ValueError("original native proof does not cover this exact descendant")
        _validate_coverage_bytes(coverage)
    elif record["frontier"] != doc["worker_config"]["finite_frontier"]:
        raise ValueError("nonroot hold lacks authenticated descendant coverage")
    body = {
        **({"descendant_coverage_sha256": _digest(coverage)} if coverage is not None else {}),
        "schema": "closer-hold-terminal-boundary/v1",
        "binding": native.binding(dict(manifest)),
        "campaign": native._campaign_identity(campaign.path, doc),
        "hold_id": record["hold_id"],
        "source_row_sha256": record["source"]["row_sha256"],
        "result": {"path": str(path.resolve()), "sha256": native._hash(path)},
        "worker_id": last["worker_id"],
        "proof_sha256": _digest(proof),
    }
    root = native.contract(dict(manifest))[3]
    return {"record": body, "signature": mechanisms._signature(body, mechanisms._key(root))}


def _validate_terminal_boundary(
    campaign: Any,
    doc: Mapping[str, Any],
    manifest: Mapping[str, Any],
    record: Mapping[str, Any],
    boundary: Any,
) -> None:
    import hmac

    import closer_campaign as campaigns
    import closer_finite_reentry as reentry
    import closer_mechanisms as mechanisms
    import closer_native_observer as native
    from claims import holder

    _assert_bound(doc, record)
    if (
        not _probe_allowed(doc)
        or not native.entry_active(dict(manifest))
        or campaigns.native_barrier(dict(doc))
        or holder(record["target"])
    ):
        raise ValueError("current native follow-up entry is unavailable")
    if not isinstance(boundary, Mapping) or not isinstance(boundary.get("record"), Mapping):
        raise ValueError("held release lacks original terminal boundary")
    body = boundary["record"]
    if not (
        body.get("schema") == "closer-hold-terminal-boundary/v1"
        and body.get("binding") == native.binding(dict(manifest))
        and body.get("campaign") == native._campaign_identity(campaign.path, doc)
        and body.get("hold_id") == record["hold_id"]
        and body.get("source_row_sha256") == record["source"]["row_sha256"]
        and body.get("result", {}).get("sha256")
        == native._hash(reentry._physical(body["result"]["path"]))
        and hmac.compare_digest(
            str(boundary.get("signature", "")),
            mechanisms._signature(dict(body), mechanisms._key(native.contract(dict(manifest))[3])),
        )
    ):
        raise ValueError("original native terminal boundary changed")
    coverage = record["source"].get("descendant_coverage")
    if coverage is not None:
        if body.get("descendant_coverage_sha256") != _digest(coverage):
            raise ValueError("original signed descendant coverage differs")
        _validate_coverage_bytes(coverage)
    elif record["frontier"] != doc["worker_config"]["finite_frontier"]:
        raise ValueError("nonroot replay lacks signed descendant coverage")
    workers = doc.get("workers") or []
    if (
        not workers
        or any(
            worker.get("state") != "terminal"
            or worker.get("outcome") not in {"ok", "scoped_continuation", "unavailable"}
            for worker in workers
        )
        or workers[-1].get("worker_id") != body["worker_id"]
    ):
        raise ValueError("native original worker boundary is no longer terminal")


def _apply_no_start(
    record: Mapping[str, Any],
    manifest: Mapping[str, Any],
    manifest_path: str,
    doc: Mapping[str, Any],
    campaign: Any,
) -> dict[str, Any]:
    import closer_finite_reentry as reentry

    if campaign is None or record["execution_state"] != "confirmed_not_started":
        raise ValueError("no-start release requires its bound campaign")
    intent = record.get("reentry_intent")
    if intent is None:
        boundary = _native_terminal_boundary(campaign, doc, manifest, record)
        event = "hold-followup-" + record["last_check_id"]
        receipts = {
            "followup_manifest": {
                "path": str(Path(manifest_path).resolve()),
                "sha256": hashlib.sha256(Path(manifest_path).read_bytes()).hexdigest(),
            },
            "capacity_observation_window": {
                "started_at": record["last_check_at"],
                "expires_at": min(record["deadline"], record["last_check_at"] + 120),
            },
        }
        common = {
            "frontier": record["frontier"],
            "target": record["target"],
            "event": event,
            "assignment_path": str(_assignment_path()),
            "source_receipts": receipts,
        }
        proposal = reentry.plan(record["source"]["state_path"], **common)
        intent = {
            "event": event,
            "plan_sha256": proposal["plan_sha256"],
            "source_receipts": receipts,
            "terminal_boundary": boundary,
        }

        def persist(value):
            row = value["hold_followups"][record["hold_id"]]
            _assert_bound(value, row)
            if (
                row.get("status") != "checking"
                or row.get("last_check_id") != record["last_check_id"]
                or row.get("reentry_intent") is not None
                or not _probe_allowed(value)
                or value.get("workers") != doc.get("workers")
            ):
                raise ValueError("hold changed before reentry intent")
            row["reentry_intent"] = intent

        doc = campaign.update(persist)
    else:
        _validate_terminal_boundary(
            campaign, doc, manifest, record, intent.get("terminal_boundary")
        )
        path = reentry._physical(record["source"]["state_path"])
        audit_path = (
            path.parent.parent.parent
            / "reentry-audit"
            / (_digest([record["frontier"], record["target"], intent["event"]]) + ".json")
        )
        if audit_path.exists():
            audit, _ = reentry._read(audit_path)
            current, _ = reentry._read(path)
            row = current["frontiers"][record["frontier"]["frontier_id"]]["items"][record["target"]]
            if audit.get("plan", {}).get("plan_sha256") != intent["plan_sha256"] or _digest(
                row
            ) not in {audit.get("after_sha256"), _digest(audit.get("before"))}:
                raise ValueError("reentry audit no longer binds this interrupted check")
    applied: dict[str, Any] = {}

    def apply_bound(value):
        held = value["hold_followups"][record["hold_id"]]
        _assert_bound(value, held)
        if (
            held.get("status") != "checking"
            or held.get("last_check_id") != record["last_check_id"]
            or held.get("reentry_intent") != intent
        ):
            raise ValueError("hold changed before local reentry")
        _validate_terminal_boundary(campaign, value, manifest, held, intent["terminal_boundary"])
        if time.time() >= held["deadline"]:
            raise ValueError("hold deadline expired before local reentry")
        # Serialize campaign writer admission/denial with this LOCAL audited
        # scheduling mutation. Source reentry separately locks the exact shard.
        applied.update(
            reentry.apply(
                record["source"]["state_path"],
                frontier=record["frontier"],
                target=record["target"],
                event=intent["event"],
                assignment_path=str(_assignment_path()),
                source_receipts=intent["source_receipts"],
                expected_plan_sha256=intent["plan_sha256"],
            )
        )

    campaign.update(apply_bound)
    return {
        "status": "released",
        "proof": {
            "reentry": applied,
            "plan_sha256": intent["plan_sha256"],
            "terminal_boundary": intent["terminal_boundary"],
        },
    }


def _backend(manifest_path: str, doc: Mapping[str, Any]):
    import closer_dispatcher as dispatcher
    from closer_runtime_binding import binding_from_manifest

    cfg = doc.get("worker_config") or {}
    return dispatcher.Closer(
        dispatcher.GitHub(deadline=time.time() + 120),
        state_path=Path(cfg["dispatch_state"]),
        apply=False,
        # Evaluate the ordinary successor route without suppressing NON_PASS
        # applicability merely because this collector performs no execution.
        # apply=False remains the execution fence; fresh native PREPARE still
        # owns role qualification, capacity and all guarded action admission.
        workers=True,
        runtime_binding=binding_from_manifest(manifest_path),
        require_runtime_binding=True,
    )


def _probe_action(
    record: Mapping[str, Any], manifest_path: str, doc: Mapping[str, Any]
) -> dict[str, Any]:
    import closer_pipeline as pipeline
    import closer_queue as queue

    source = Path(record["source"]["state_path"])
    path = source.parent / f"{source.stem}.actions" / f"{pipeline._digest(record['target'])}.json"
    document = queue._read_state(path)
    state = queue.state_for_frontier(
        document, record["frontier"]["frontier_id"], expected_policy=record["frontier"]
    )
    operation = ((state or {}).get("items") or {}).get(record["target"], {}).get("operation")
    if not isinstance(operation, Mapping) or not _text(operation.get("id")):
        return {"status": "waiting", "reason": "unknown effect lacks exact queue operation"}
    expected = record["source"].get("operation_id")
    revision = record["source"].get("operation_revision")
    if not _text(expected) or not _text(revision):
        return {
            "status": "waiting",
            "reason": "unknown callback lacks its original exact operation identity",
        }
    if expected != operation["id"] or revision != operation.get("revision"):
        raise ValueError("held queue operation identity changed")
    result = queue.reconcile_only(
        path,
        record["target"],
        expected,
        revision,
        frontier_id=record["frontier"]["frontier_id"],
        reconcile=_backend(manifest_path, doc).reconcile,
    )
    # This may settle the ORIGINAL Core fence. It does not authenticate a new
    # outer receipt or permit replay of the unknown callback.
    return {
        "status": "waiting",
        "queue_reconciliation": result,
        "reason": "original effect reconciliation retained; outer callback remains owned",
    }


def _probe_ci_no_effect(record, manifest_path, doc, row, *, campaign, manifest):
    import closer_ci_followup as ci
    import closer_finite_reentry as reentry
    import closer_frontier_policy as frontiers
    import closer_resources as resources

    # This function also serves direct/restart callers. Validate the retained
    # source join before constructing a backend or asking CI to read anything.
    try:
        origin = record.get("source")
        target = record.get("target")
        frontier = frontiers.validate_frontier(record.get("frontier"))
        if not (
            isinstance(origin, Mapping)
            and origin.get("kind") == "pipeline"
            and _text(origin.get("state_path"))
            and Path(origin["state_path"]).is_absolute()
            and target in frontier["targets"]
            and record.get("revision") == frontier["bindings"][target]["revision"]
            and record.get("execution_state") == "confirmed_not_started"
            and record.get("run_id")
            == _digest(["finite-frontier/v2", frontier["frontier_id"], frontier["owner"]])[:32]
            and row.get("revision") == record["revision"]
            and row.get("provider_outcome_uncertain") in (None, False)
            and row.get("recovery_barrier") is None
            and origin.get("row_sha256") == _digest(row)
        ):
            raise ValueError("missing original CI source identity")
        source = reentry._physical(origin["state_path"])
        root = source.parent.parent.parent
        if source.name != "state.json" or source.parent != resources.shard_dir(root, target):
            raise ValueError("original CI source shard differs")
        ci.pending_evidence(row)  # Pure typed original no-effect ACT boundary.
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return {"status": "waiting", "reason": "CI original source binding is missing or invalid"}
    action_path = source.parent / f"{source.stem}.actions" / f"{_digest(record['target'])}.json"
    read = ci.read_terminal(
        _backend(manifest_path, doc),
        action_path,
        record["target"],
        record["frontier"],
        row,
        state_path=source,
    )
    if read["status"] != "ready":
        return read
    if campaign is None or manifest is None:
        return {
            "status": "waiting",
            "reason": "CI terminal follow-up requires its bound native campaign",
        }
    existing = record.get("ci_followup_intent")
    if existing is not None:
        _validate_ci_intent(campaign, doc, manifest, record, existing)
        if read["proof"] != existing.get("terminal"):
            raise ValueError("CI terminal changed after durable intent")
        intent = existing
    else:
        intent = {
            "schema": "closer-ci-terminal-intent/v1",
            "terminal": read["proof"],
            "source": copy.deepcopy(record["source"]),
            **{
                key: record[key] for key in ("campaign_id", "owner", "release_pin", "config_sha256")
            },
            "generation": doc.get("generation", 1),
            "terminal_boundary": _native_terminal_boundary(campaign, doc, manifest, record),
        }

        def persist(value):
            held = value["hold_followups"][record["hold_id"]]
            _assert_bound(value, held)
            if (
                held.get("status") != "checking"
                or held.get("last_check_id") != record["last_check_id"]
                or held.get("ci_followup_intent") is not None
                or not _probe_allowed(value)
                or value.get("workers") != doc.get("workers")
            ):
                raise ValueError("campaign changed before CI follow-up intent")
            _source_row(held)
            _validate_ci_intent(campaign, value, manifest, held, intent)
            held["ci_followup_intent"] = intent

        campaign.update(persist)
    return {
        "status": "released",
        "proof": {
            "kind": "ci_no_effect_terminal",
            "terminal": read["proof"],
            "intent_sha256": _digest(intent),
            "authority": False,
        },
    }


def _validate_ci_intent(campaign, doc, manifest, record, intent):
    if not (
        isinstance(intent, Mapping)
        and intent.get("schema") == "closer-ci-terminal-intent/v1"
        and all(
            intent.get(key) == record.get(key)
            for key in ("campaign_id", "owner", "release_pin", "config_sha256")
        )
        and intent.get("generation") == doc.get("generation", 1)
        and intent.get("source") == record["source"]
        and isinstance(intent.get("terminal"), Mapping)
        and intent["terminal"].get("schema") == "closer-ci-terminal-followup/v1"
        and intent["terminal"].get("target") == record["target"]
        and intent["terminal"].get("original_wait", {}).get("confirmed_no_effect") is True
    ):
        raise ValueError("CI follow-up intent identity changed")
    _validate_terminal_boundary(campaign, doc, manifest, record, intent.get("terminal_boundary"))


def _probe_ci(
    record: Mapping[str, Any],
    manifest_path: str,
    doc: Mapping[str, Any],
    row: Mapping[str, Any],
    *,
    campaign: Any = None,
    manifest: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    import closer_pipeline as pipeline
    import closer_queue as queue
    import closer_verifier_followup as followup

    if record["release_proof"]["kind"] == "ci_terminal":
        return _probe_ci_no_effect(
            record, manifest_path, doc, row, campaign=campaign, manifest=manifest
        )
    if record["release_proof"]["kind"] != "accepted_verifier_terminal":
        return {"status": "waiting", "reason": "fresh CI requires a normally admitted new frontier"}
    snapshot = _backend(manifest_path, doc).observe(record["target"])
    snapshot["observed_at"] = time.time()
    source = Path(record["source"]["state_path"])
    action_path = (
        source.parent / f"{source.stem}.actions" / f"{pipeline._digest(record['target'])}.json"
    )
    action = (row.get("results") or {}).get("act") or {}
    accepted = queue.accepted_verifier_wait_proof(
        action_path,
        record["target"],
        frontier=record["frontier"],
        receipt=action.get("queue_receipt"),
        evidence=action.get("pending_evidence"),
    )
    if accepted is None:
        raise ValueError("CI hold lacks original accepted request proof")
    # PREPARE's compact snapshot intentionally omits merge. The authenticated
    # original queue request, rather than that summary, binds both identities.
    if any(snapshot.get(name) != accepted.get(name) for name in ("head", "merge")):
        raise ValueError("CI follow-up head or merge identity changed")
    terminal = followup.terminal_proof(action_path, record["target"], accepted, snapshot)
    if terminal is None:
        return {
            "status": "waiting",
            "reason": "original sent correlation lacks a current authenticated terminal event/run/attempt",
        }
    if campaign is None or manifest is None:
        return {
            "status": "waiting",
            "reason": "terminal follow-up requires bound campaign admission",
        }
    existing = record.get("terminal_followup_intent")
    if existing is not None:
        if existing.get("terminal") != terminal or existing.get("accepted_request") != accepted:
            raise ValueError("terminal follow-up identity changed after durable intent")
        _validate_terminal_intent(campaign, doc, manifest, record, existing)
        intent = existing
    else:
        intent = {
            "schema": "closer-verifier-terminal-intent/v1",
            "terminal": terminal,
            "accepted_request": accepted,
            "source": copy.deepcopy(record["source"]),
            "campaign_id": record["campaign_id"],
            "owner": record["owner"],
            "generation": doc.get("generation", 1),
            "release_pin": record["release_pin"],
            "config_sha256": record["config_sha256"],
            "terminal_boundary": _native_terminal_boundary(campaign, doc, manifest, record),
        }

        def persist(value):
            held = value["hold_followups"][record["hold_id"]]
            _assert_bound(value, held)
            if (
                held.get("status") != "checking"
                or held.get("last_check_id") != record["last_check_id"]
                or held.get("terminal_followup_intent") is not None
                or not _probe_allowed(value)
                or value.get("workers") != doc.get("workers")
            ):
                raise ValueError("campaign changed before terminal follow-up intent")
            _source_row(held)
            _validate_terminal_boundary(
                campaign, value, manifest, held, intent["terminal_boundary"]
            )
            held["terminal_followup_intent"] = intent

        campaign.update(persist)
    return {
        "status": "released",
        "proof": {
            "kind": "accepted_verifier_terminal",
            "terminal": terminal,
            "intent_sha256": _digest(intent),
            "authority": False,
        },
    }


def _assert_bound(doc: Mapping[str, Any], record: Mapping[str, Any]) -> None:
    if (
        doc.get("campaign_id") != record["campaign_id"]
        or doc.get("owner") != record["owner"]
        or doc.get("generation", 1) != record.get("observer_generation", record["generation"])
        or doc.get("release_pin") != record["release_pin"]
        or _digest(doc.get("worker_config") or {}) != record["config_sha256"]
    ):
        raise ValueError("hold campaign owner, generation, pin or configuration changed")


def _probe_allowed(doc: Mapping[str, Any]) -> bool:
    return bool(
        doc.get("phase") == "work"
        and not doc.get("hold")
        and not doc.get("release_record")
        and all(
            worker.get("state") not in {"active", "launched"} for worker in doc.get("workers", [])
        )
    )


def _validate_terminal_intent(campaign, doc, manifest, record, intent):
    if not isinstance(intent, Mapping) or not (
        intent.get("schema") == "closer-verifier-terminal-intent/v1"
        and all(
            intent.get(key) == record.get(key)
            for key in ("campaign_id", "owner", "release_pin", "config_sha256")
        )
        and intent.get("generation") == doc.get("generation", 1)
        and intent.get("source") == record["source"]
        and isinstance(intent.get("terminal"), Mapping)
        and isinstance(intent.get("accepted_request"), Mapping)
        and intent["terminal"].get("operation_id") == intent["accepted_request"].get("operation_id")
        and intent["terminal"].get("request_basis") == intent["accepted_request"].get("event_id")
    ):
        raise ValueError("terminal follow-up intent identity changed")
    _validate_terminal_boundary(campaign, doc, manifest, record, intent.get("terminal_boundary"))


def _observe_io_capacity(state_path, frontier, target, row, source_receipts):
    import closer_finite_reentry as reentry
    import closer_pipeline as pipeline
    import closer_resources as resources

    path = reentry._physical(state_path)
    root = path.parent.parent.parent
    context = row.get("shared_lane_context")
    if not isinstance(context, Mapping):
        raise ValueError("original shared IO bound is unavailable")
    body = {key: value for key, value in context.items() if key != "sha256"}
    bounds = body.get("bounds")
    if not (
        path.name == "state.json"
        and path.parent == resources.shard_dir(root, target)
        and body.get("schema") == "closer-shared-lane-context/v1"
        and body.get("root") == str(root)
        and body.get("state_path") == str(path)
        and body.get("target") == target
        and body.get("frontier_sha256") == _digest(frontier)
        and context.get("sha256") == _digest(body)
        and isinstance(bounds, Mapping)
        and set(bounds) == set(pipeline.CEILINGS)
        and all(
            type(bounds[key]) is int and 1 <= bounds[key] <= pipeline.CEILINGS[key]
            for key in bounds
        )
    ):
        raise ValueError("original shared IO context is malformed or changed")
    document, _ = reentry._read(path)
    entry = (document.get("frontiers") or {}).get(frontier["frontier_id"], {})
    current_row = (entry.get("items") or {}).get(target)
    if (
        document.get("schema") != pipeline.SCHEMA
        or entry.get("schema") != "closer-pipeline-frontier/v2"
        or entry.get("finite_frontier") != frontier
        or entry.get("run_id")
        != _digest(["finite-frontier/v2", frontier["frontier_id"], frontier["owner"]])[:32]
        or not isinstance(current_row, Mapping)
    ):
        raise ValueError("IO observation differs from exact retained capacity row")
    if current_row != row:
        # Plan/apply/ready/rearm recheck the same original no-start release.
        # Only its exact applied fresh-PREPARE audit may replace that old row.
        ready = current_row.get("reentry_ready") or {}
        if not _text(ready.get("event")):
            raise ValueError("IO original row has no exact applied reentry audit")
        audit_path = (
            root / "reentry-audit" / (_digest([frontier, target, ready["event"]]) + ".json")
        )
        audit, _ = reentry._read(audit_path)
        if not (
            audit.get("status") == "applied"
            and audit.get("before") == row
            and audit.get("after_sha256") == _digest(current_row)
            and audit.get("plan", {}).get("plan_sha256") == ready.get("plan_sha256")
            and current_row.get("shared_lane_context") == context
        ):
            raise ValueError("IO original row no longer matches its applied reentry audit")
    window = source_receipts.get("capacity_observation_window") or {}
    start, end, current = window.get("started_at"), window.get("expires_at"), time.time()
    if not (_number(start) and _number(end) and start <= current < end <= start + 120):
        raise ValueError("original IO observation window is unavailable or expired")
    # Reuse the actual scheduler's flock/fence logic. One nonblocking pass,
    # without a finite reservation or provider/child launch, observes a slot.
    reentry._physical(root / "pools" / "io")
    for index in range(bounds["io"]):
        reentry._physical(root / "pools" / "io" / f"slot-{index}")
        reentry._physical(root / "pools" / "io" / f"slot-{index}.json")
    with resources.pool_slot(root, "io", bounds["io"], deadline=current):
        if not start <= time.time() < end:
            raise ValueError("original IO observation window expired during check")
    return {
        "schema": "closer-io-availability-observation/v1",
        "root": str(root),
        "lane": "io",
        "bound": bounds["io"],
        "original_context_sha256": context["sha256"],
        "observation_window": dict(window),
        "available": True,
    }


def release_probe(
    state_path: str | Path,
    frontier: Mapping[str, Any],
    target: str,
    row: Mapping[str, Any],
    assignment_path: str | Path,
    source_receipts: Any,
) -> dict[str, Any]:
    """Recompute a known-no-start release predicate from current pinned sources."""
    import closer_campaign as campaigns
    import closer_dispatcher as dispatcher
    import closer_finite_reentry as reentry
    import closer_native_observer as native
    import closer_operational_capacity as scoped
    import closer_role_qualification as roles
    import execution_profiles

    canonical = _assignment_path()
    assignment = reentry._physical(assignment_path)
    if assignment != canonical or dispatcher.assignment_dependency(assignment, target) is not None:
        raise ValueError("hold release requires current active canonical assignment")
    manifest_ref = (
        source_receipts.get("followup_manifest") if isinstance(source_receipts, Mapping) else None
    )
    if not isinstance(manifest_ref, Mapping) or not _text(manifest_ref.get("path")):
        raise ValueError("hold release requires pinned current native runtime")
    manifest_path = reentry._physical(manifest_ref["path"])
    if hashlib.sha256(manifest_path.read_bytes()).hexdigest() != manifest_ref.get("sha256"):
        raise ValueError("hold release runtime manifest changed")
    manifest = campaigns.load_manifest(manifest_path)
    if campaigns.qualify(manifest, manifest_path.parent):
        raise ValueError("hold release runtime is not qualified")
    policy_value = native.capacity_contract(manifest, control_plane_only=True)
    if policy_value is None:
        raise ValueError("hold release lacks native capacity scope")
    stage = row.get("stage")
    if not isinstance(stage, str):
        raise ValueError("hold stage identity is missing")
    result = (row.get("results") or {}).get(stage, {})
    expected_role = {
        "interpret": roles.BOUNDED_CLASSIFICATION,
        "exception": roles.STRONG_CRITERION_ADJUDICATION,
    }.get(stage)
    role_hold = result.get("classification") == "role_qualification_unavailable"
    if role_hold:
        pending = result.get("pending_evidence") or {}
        requested = pending.get("role")
        allowed = (
            {roles.BOUNDED_CLASSIFICATION}
            if stage == "interpret"
            else set(roles.ROLES) if stage in {"exception", "act"} else set()
        )
        revision = frontier["bindings"][target]["revision"]
        if (
            requested not in allowed
            or pending.get("target") != target
            or pending.get("revision") != revision
            or pending.get("event_id")
            != f"closer-role-qualification:{target}:{revision}:{requested}"
        ):
            raise ValueError("hold role event differs from its original target/revision/stage")
        expected_role = requested
    if not role_hold and row.get("status") != "capacity_blocked":
        raise ValueError("hold is not a supported no-start role/capacity boundary")
    if stage not in {"prepare", "interpret", "exception", "act"}:
        raise ValueError("unsupported no-start stage")
    eligible = []
    strict = native.capacity_contract(manifest) if expected_role is not None else policy_value
    producer = {
        "schema": scoped.SCHEMA,
        "owner": strict["owner"],
        "profile_ids": strict["profile_ids"] if expected_role is not None else [],
        "bounded_trial_agents": strict["bounded_trial_agents"] if expected_role is not None else [],
        "original_deadline": time.time() + 120,
        "runtime_manifest": str(manifest_path),
        "runtime_manifest_sha256": manifest_ref["sha256"],
        **({"control_plane_only": True} if expected_role is None else {}),
    }
    report = scoped.evaluate(producer)
    if not scoped.validate_report(report, now=time.time()):
        raise ValueError("hold capacity observation is not current")
    capacity_inputs = report["input_sha256"]
    io_observation = None
    if expected_role is None:
        io_observation = _observe_io_capacity(state_path, frontier, target, row, source_receipts)
    else:
        for profile_id in strict["profile_ids"]:
            profile = execution_profiles.get_profile(profile_id)
            if dispatcher._profile_exception_qualified(
                report,
                profile,
                repair=expected_role == roles.CONSTRAINED_REPAIR,
                substantive=expected_role == roles.STRONG_CRITERION_ADJUDICATION,
            ):
                eligible.append(
                    {"profile_id": profile_id, "identity_sha256": roles.identity_digest(profile)}
                )
        if not eligible:
            raise ValueError("role or model pool remains unavailable")
    return {
        "schema": "closer-hold-release-proof/v1",
        "target": target,
        "kind": "qualified_role_followup" if role_hold else "available_capacity_followup",
        "stage": stage,
        "role": expected_role,
        "manifest": dict(manifest_ref),
        "assignment_sha256": hashlib.sha256(assignment.read_bytes()).hexdigest(),
        "eligible_profiles": sorted(eligible, key=lambda item: item["profile_id"]),
        "capacity_inputs": capacity_inputs,
        **({"io_observation": io_observation} if io_observation is not None else {}),
        "acceptance_authority": False,
    }


def rearm(
    campaign: Any, doc: dict[str, Any], manifest: Mapping[str, Any], *, now: float | None = None
) -> dict[str, Any]:
    """Rearm the SAME full native cohort for no-start or correlated terminal follow-up.

    A completed ACT is preserved. Its next native coordinator uses the existing
    stage-successor route and fresh PREPARE, never replay of the original effect.
    """
    import closer_campaign as campaigns
    import closer_finite_reentry as reentry

    current = time.time() if now is None else now
    if not _probe_allowed(doc) or campaigns.granularity(doc) != "round":
        return doc
    workers = doc.get("workers") or []
    if not workers or any(
        worker.get("state") != "terminal"
        or worker.get("outcome") not in {"ok", "scoped_continuation", "unavailable"}
        for worker in workers
    ):
        return doc
    ready = []
    for record in (doc.get("hold_followups") or {}).values():
        if (
            record.get("status") != "released"
            or record["execution_state"] != "confirmed_not_started"
            or current >= record["deadline"]
        ):
            continue
        _assert_bound(doc, record)
        ci_intent = record.get("ci_followup_intent")
        if isinstance(ci_intent, Mapping):
            import closer_ci_followup as ci

            _validate_ci_intent(campaign, doc, manifest, record, ci_intent)
            _entry, original_row = _source_row(record)
            source = Path(record["source"]["state_path"])
            action_path = (
                source.parent / f"{source.stem}.actions" / f"{_digest(record['target'])}.json"
            )
            read = ci.read_terminal(
                _backend(str(doc["worker_config"]["runtime_manifest"]), doc),
                action_path,
                record["target"],
                record["frontier"],
                original_row,
                state_path=source,
            )
            if read["status"] != "ready" or read["proof"] != ci_intent["terminal"]:
                raise ValueError("CI terminal or original no-effect proof changed before rearm")
            ready.append(
                {
                    "hold_id": record["hold_id"],
                    "kind": "ci_no_effect_terminal",
                    "intent_sha256": _digest(ci_intent),
                }
            )
            continue
        terminal_intent = record.get("terminal_followup_intent")
        if isinstance(terminal_intent, Mapping):
            import closer_verifier_followup as followup

            _validate_terminal_intent(campaign, doc, manifest, record, terminal_intent)
            _entry, original_row = _source_row(record)
            snapshot = _backend(str(doc["worker_config"]["runtime_manifest"]), doc).observe(
                record["target"]
            )
            snapshot["observed_at"] = time.time()
            source = Path(record["source"]["state_path"])
            action_path = (
                source.parent / f"{source.stem}.actions" / f"{_digest(record['target'])}.json"
            )
            import closer_queue as queue

            original_action = (original_row.get("results") or {}).get("act") or {}
            fresh_request = queue.accepted_verifier_wait_proof(
                action_path,
                record["target"],
                frontier=record["frontier"],
                receipt=original_action.get("queue_receipt"),
                evidence=original_action.get("pending_evidence"),
            )
            if fresh_request != terminal_intent["accepted_request"]:
                raise ValueError("original accepted request changed before native rearm")
            fresh_terminal = followup.terminal_proof(
                action_path, record["target"], fresh_request, snapshot
            )
            if fresh_terminal is None or fresh_terminal != terminal_intent.get("terminal"):
                raise ValueError("terminal result or fresh successor binding changed before rearm")
            ready.append(
                {
                    "hold_id": record["hold_id"],
                    "kind": "accepted_verifier_terminal",
                    "intent_sha256": _digest(terminal_intent),
                }
            )
            continue
        intent = record.get("reentry_intent")
        if not isinstance(intent, Mapping):
            # Only a concrete durable release intent may rearm this frontier.
            # Missing historical correlation remains owned waiting work.
            continue
        _validate_terminal_boundary(
            campaign, doc, manifest, record, intent.get("terminal_boundary")
        )
        path = reentry._physical(record["source"]["state_path"])
        audit_path = (
            path.parent.parent.parent
            / "reentry-audit"
            / (_digest([record["frontier"], record["target"], intent["event"]]) + ".json")
        )
        audit, audit_sha = reentry._read(audit_path)
        document, _ = reentry._read(path)
        row = document["frontiers"][record["frontier"]["frontier_id"]]["items"][record["target"]]
        if not (
            audit.get("status") == "applied"
            and audit.get("plan", {}).get("plan_sha256") == intent["plan_sha256"]
            and _digest(audit.get("before")) == record["source"]["row_sha256"]
            and _digest(row) == audit.get("after_sha256")
            and row.get("stage") == "prepare"
            and row.get("results") == {}
            and row.get("reentry_ready", {}).get("plan_sha256") == intent["plan_sha256"]
        ):
            raise ValueError("native hold release lacks exact applied fresh-PREPARE audit")
        fresh = release_probe(
            path,
            record["frontier"],
            record["target"],
            audit["before"],
            audit["plan"]["assignment_path"],
            audit["plan"]["source_receipts"],
        )
        if fresh != audit["plan"].get("followup_proof"):
            raise ValueError("native hold source/runtime release inputs changed")
        ready.append(
            {"hold_id": record["hold_id"], "audit": str(audit_path), "audit_sha256": audit_sha}
        )
    if not ready:
        return doc
    expected_revision = doc["revision"]
    generation = doc.get("generation", 1)

    def transition(value):
        if (
            value["revision"] != expected_revision
            or value.get("workers") != workers
            or not _probe_allowed(value)
            or value.get("generation", 1) != generation
        ):
            raise ValueError("native hold owner changed before rearm")
        # Final CAS checks the immutable audit bytes and current exact target
        # row under campaign admission serialization, before opening a cohort.
        for released in ready:
            record = value["hold_followups"][released["hold_id"]]
            if released.get("kind") == "ci_no_effect_terminal":
                intent = record["ci_followup_intent"]
                _validate_ci_intent(campaign, value, manifest, record, intent)
                _source_row(record)
                if _digest(intent) != released["intent_sha256"]:
                    raise ValueError("CI sidecar changed before native rearm")
                continue
            if released.get("kind") == "accepted_verifier_terminal":
                terminal_intent = record["terminal_followup_intent"]
                _validate_terminal_intent(campaign, value, manifest, record, terminal_intent)
                _source_row(record)
                if _digest(terminal_intent) != released["intent_sha256"]:
                    raise ValueError("terminal sidecar changed before native rearm")
                continue
            intent = record["reentry_intent"]
            _validate_terminal_boundary(
                campaign, value, manifest, record, intent["terminal_boundary"]
            )
            audit, sha = reentry._read(reentry._physical(released["audit"]))
            state, _ = reentry._read(reentry._physical(record["source"]["state_path"]))
            source_row = state["frontiers"][record["frontier"]["frontier_id"]]["items"][
                record["target"]
            ]
            if sha != released["audit_sha256"] or _digest(source_row) != audit.get("after_sha256"):
                raise ValueError("audited hold source changed before native rearm")
        final_now = time.time()
        expired = [
            value["hold_followups"][row["hold_id"]]
            for row in ready
            if final_now >= value["hold_followups"][row["hold_id"]]["deadline"]
        ]
        if expired:
            for held in expired:
                held.update(
                    status="escalated",
                    escalated_at=final_now,
                    escalation_reason="deadline_before_native_rearm",
                )
            return  # Expiry closes this scheduling release; it grants no cohort.
        next_generation = generation + 1
        value.setdefault("hold_followup_transitions", []).append(
            {
                "schema": "native-hold-followup-transition/v1",
                "previous_generation": generation,
                "next_generation": next_generation,
                "owner": value["owner"],
                "release_pin": value["release_pin"],
                "releases": ready,
                "at": current,
                "accepted": False,
            }
        )
        value["generation"] = next_generation
        value["targets"][campaigns.ROUND_UNIT] = "pending"
        consumed = {row["hold_id"] for row in ready}
        for hold_id, record in value.get("hold_followups", {}).items():
            if record.get("status") in {"waiting", "checking", "released"}:
                _assert_bound({**value, "generation": generation}, record)
                record["observer_generation"] = next_generation
                if hold_id in consumed:
                    record["status"] = "consumed"
                    record["rearmed_at"] = current

    return campaign.update(transition)


def consume(
    campaign: Any, manifest: Mapping[str, Any], manifest_path: str, *, now: float | None = None
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """One scheduled bounded pass; no provider submission or ordinary effect API.

    Reconciliation may settle only an original exact Core fence. Deadline and
    attempt exhaustion only escalate. An interrupted check keeps its original
    check identity/attempt budget; reentry intent recovery is idempotent.
    """
    current = time.time() if now is None else now
    if not _number(current):
        raise ValueError("invalid follow-up clock")
    doc = capture(campaign, now=current)
    settings = policy(doc)
    reports: list[dict[str, Any]] = []
    journal = doc.get("hold_followups") or {}
    if any(not validate_hold(row) for row in journal.values()):
        raise ValueError("malformed durable hold")
    # Expiry is processed for the entire bounded journal, independently of the
    # remote probe cap. Among probes, oldest checked/due work goes first.
    ordered = sorted(
        journal,
        key=lambda key: (
            not (
                current >= journal[key]["deadline"]
                or (
                    journal[key]["attempts"] >= journal[key]["attempt_budget"]
                    and journal[key]["status"] == "waiting"
                )
            ),
            journal[key].get("last_check_at", journal[key]["created_at"]),
            journal[key]["next_check_at"],
            key,
        ),
    )
    probes = 0
    for hold_id in ordered:
        prior = copy.deepcopy(doc["hold_followups"][hold_id])
        if prior["status"] not in {"waiting", "checking", "released"}:
            continue
        if prior["status"] == "released" and current < prior["deadline"]:
            continue  # Await native rearm; never probe/reapply a released row.
        _assert_bound(doc, prior)
        fingerprint = _event_fingerprint(prior)
        due = (
            prior["status"] == "checking"
            or current >= prior["next_check_at"]
            or fingerprint != prior.get("last_event_fingerprint")
        )
        expired = current >= prior["deadline"] or (
            prior["attempts"] >= prior["attempt_budget"] and prior["status"] == "waiting"
        )
        if not due and not expired:
            continue
        if not expired and (probes >= MAX_CHECKS or not _probe_allowed(doc)):
            continue
        recovering = prior["status"] == "checking"
        check_id = (
            prior.get("last_check_id")
            if recovering
            else _digest([hold_id, prior["attempts"], fingerprint, prior["next_check_at"]])
        )
        if not _text(check_id):
            raise ValueError("interrupted check lacks durable identity")

        def intent(value):
            row = value["hold_followups"][hold_id]
            _assert_bound(value, row)
            if row != prior:
                raise ValueError("hold changed before check")
            if expired:
                row.update(
                    status="escalated",
                    escalated_at=current,
                    escalation_reason="deadline_or_attempt_budget",
                    last_check_id=check_id,
                )
            elif not recovering:
                row.update(
                    status="checking",
                    attempts=row["attempts"] + 1,
                    last_check_id=check_id,
                    last_check_at=current,
                    last_event_fingerprint=fingerprint,
                    next_check_at=min(
                        row["deadline"], current + settings["check_interval_seconds"]
                    ),
                )

        doc = campaign.update(intent)
        checking = copy.deepcopy(doc["hold_followups"][hold_id])
        if expired:
            reports.append(
                {
                    "kind": "hold_escalated",
                    "hold_id": hold_id,
                    "owner": checking["escalation_owner"],
                    "execution_state": checking["execution_state"],
                    "authority": False,
                }
            )
            continue
        probes += 1
        try:
            answer = _probe(checking, manifest, manifest_path, doc, campaign=campaign)
        except (OSError, ValueError, TypeError, KeyError, RuntimeError) as exc:
            answer = {"status": "waiting", "reason": str(exc)[:500]}
        allowed = (
            {"waiting", "resolved"}
            if checking["execution_state"] != "confirmed_not_started"
            else {"waiting", "released", "escalated"}
        )
        if answer.get("status") not in allowed:
            answer = {"status": "waiting", "reason": "probe cannot release uncertain effects"}

        def finish(value):
            row = value["hold_followups"][hold_id]
            _assert_bound(value, row)
            if (
                row.get("last_check_id") != check_id
                or row.get("status") != "checking"
                or (answer["status"] == "released" and not _probe_allowed(value))
            ):
                raise ValueError("hold changed during follow-up")
            # Only the concrete reentry probe may add its durable intent while
            # this check is running. All other concurrent row mutations refuse.
            sidecars = {"reentry_intent", "terminal_followup_intent", "ci_followup_intent"}
            base = {k: v for k, v in row.items() if k not in sidecars}
            expected = {k: v for k, v in checking.items() if k not in sidecars}
            if base != expected:
                raise ValueError("hold changed during follow-up read")
            row.update(status=answer["status"], last_result=answer)
            if answer["status"] in {"released", "resolved"}:
                row["finished_at"] = current
            elif answer["status"] == "escalated":
                row["escalated_at"] = current
                row["escalation_reason"] = answer["escalation_reason"]

        doc = campaign.update(finish)
        reports.append(
            {
                "kind": "hold_checked",
                "hold_id": hold_id,
                "status": answer["status"],
                "execution_state": checking["execution_state"],
                "owner": checking["owner"],
                "authority": False,
            }
        )
    return doc, reports
