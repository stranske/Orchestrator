"""Exact, explicit re-entry for proven unstarted finite Closer requests.

This repairs scheduling state only. Normal dispatcher admission, interpretation,
queue claims and effect guards still run; historical state is never rewritten.
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import fcntl
import hashlib
import json
import time
from pathlib import Path

import closer_pipeline as pipeline
import closer_resources as resources
from closer_frontier_policy import validate_frontier

SCHEMA = "closer-finite-reentry/v1"
# Versioned source contract: these exact legacy refusals precede the inner queue.
PRE_QUEUE_CANDIDATE = "15e35271e5005fe47288e600e2d0876f7de0f7c8"
PRE_QUEUE_SOURCE_SHA256 = "8b6295ba8065255b085a6952ec8dc405e1a98cc8f8dd8e97c2b25632a475975d"
PREWRITE_CANDIDATE = "6eee61c574408b0a278518b5fda13c45ff2eecee"
PREWRITE_SOURCE_SHA256 = "925ae7e6458d45ef08548bf70589d08b1cbd744f93c13cd44a444c83db011068"
PREWRITE_QUEUE_SHA256 = "e82a41e1e5bf9ed88ac50841a8bcc75c08c82e78f14d1058afbfb5873d8216b3"
PREWRITE_PIPELINE_SHA256 = "e4f5a14d3acd3b0c822c5e182342f05411ecb3c9ce6022c672764da2528d09f1"
# Second immutable legacy return contract: terminal unlinked manual-run refusal.
PREWRITE_TERMINAL_CANDIDATE = "8f31a1d1136cb8d27fe2b4bce0eab9a9b4c75a3e"
PREWRITE_TERMINAL_SOURCE_SHA256 = "087b04a3c9db6f72e2586d7ef104710295c9cacb1886c6c4c874b7ed67ef3dc8"
PREWRITE_TERMINAL_QUEUE_SHA256 = "982e77c188cd938bbf00d0f25bf8962799fa6cf0bda3ba1b99f41098abf6164d"
PREWRITE_TERMINAL_PIPELINE_SHA256 = (
    "a1c69515a1947bd9a0a705ac69543a180aa1db98d7c3120c2026639da0c8b7a0"
)
PREWRITE_ERROR = "RuntimeError: inner closer queue did not prove a valid continuation boundary"
PRE_QUEUE_ERRORS = frozenset(
    f"RuntimeError: legacy {status} operation lacks external-run or live-owner proof; "
    "reconcile it in the original closer queue"
    for status in ("submitted", "running", "uncertain")
)


def _digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def _physical(path):
    path = Path(path).absolute()
    if path.resolve() != path or path.is_symlink():
        raise ValueError("aliased_path")
    return path


def _read(path):
    path = _physical(path)
    with path.open("rb") as stream:
        raw = stream.read(8 * 1024 * 1024 + 1)
        if len(raw) > 8 * 1024 * 1024:
            raise ValueError("oversized_record")
    try:
        value = json.loads(raw)
    except (RecursionError, UnicodeDecodeError) as exc:
        raise ValueError("invalid_or_overdeep_json_record") from exc
    if not isinstance(value, dict):
        raise ValueError("record_must_be_object")
    pending = [(value, 0)]
    nodes = 0
    while pending:
        child, depth = pending.pop()
        nodes += 1
        if depth > 128 or nodes > 100_000:
            raise ValueError("record_structure_bound")
        if isinstance(child, dict):
            pending.extend((item, depth + 1) for item in child.values())
        elif isinstance(child, list):
            pending.extend((item, depth + 1) for item in child)
    return value, hashlib.sha256(raw).hexdigest()


def _entry(state, frontier):
    entries = state.get("frontiers")
    if not isinstance(entries, dict):
        raise ValueError("malformed_finite_frontiers")
    entry = entries.get(frontier["frontier_id"])
    if not isinstance(entry, dict) or not isinstance(entry.get("items"), dict):
        raise ValueError("malformed_finite_entry")
    return entry


def _inner_boundary(path, target):
    inner = path.parent / f"{path.stem}.actions" / f"{pipeline._digest(target)}.json"
    if not inner.exists():
        _physical(inner)
        return {"path": str(inner), "sha256": None}
    doc, sha = _read(inner)
    if (
        not isinstance(doc, dict)
        or doc.get("schema") != "closer-queue/v1"
        or not isinstance(doc.get("items", {}), dict)
    ):
        raise ValueError("unreadable_inner_queue")

    # A pre-callback refusal must have no effect history at all, not merely an
    # absent current operation. Completed/foreign operations require real reconciliation.
    def inspect(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if (
                    key
                    in {
                        "operation",
                        "operation_history",
                        "provider_outcome_uncertain",
                        "recovery_barrier",
                        "action_history",
                    }
                    and child
                ):
                    raise ValueError("inner_operation_requires_reconciliation")
                if key == "status" and child is not None and not isinstance(child, str):
                    raise ValueError("malformed_inner_status")
                if (
                    key == "status"
                    and isinstance(child, str)
                    and child
                    in {
                        "unknown",
                        "uncertain",
                        "running",
                        "submitted",
                        "completed",
                        "complete",
                    }
                ):
                    raise ValueError("inner_outcome_requires_reconciliation")
                inspect(child)
        elif isinstance(value, list):
            for child in value:
                inspect(child)

    inspect(doc)
    return {"path": str(inner), "sha256": sha}


def _scope_proof(root, path, target, entry, row, *, caller_path, admitted=False):
    expected_binding = {
        "state_path": str(path),
        "pipeline_run_id": entry["run_id"],
        "revision": row.get("revision"),
        "identity": row.get("identity", target),
        "work_key": row.get("work_key"),
    }
    candidates = []
    retained = row.get("scope_refusal")
    if retained is not None and (
        not isinstance(retained, dict) or not isinstance(retained.get("path"), str)
    ):
        raise ValueError("malformed_scope_refusal_reference")
    paths = (
        [Path(retained["path"])]
        if isinstance(retained, dict) and retained.get("path")
        else list((root / "scope-admissions").glob("*/*.json"))
    )
    if len(paths) > 4096:
        raise ValueError("scope_proof_inventory_bound")
    for candidate in paths:
        candidate = _physical(candidate)
        if not candidate.is_relative_to(root / "scope-admissions"):
            raise ValueError("scope_proof_outside_admission_inventory")
        proof, sha = _read(candidate)
        if isinstance(retained, dict) and sha != retained.get("sha256"):
            raise ValueError("scope_proof_digest_changed")
        binding = proof.get("binding")
        # Only this exact request can contribute a proof. An unrelated record's
        # obsolete/aliased workspace must not poison the current target's plan.
        if (
            proof.get("schema") != "closer-scoped-admission/v1"
            or proof.get("target") != target
            or proof.get("stage") != row.get("stage")
            or not isinstance(binding, dict)
            or {key: value for key, value in binding.items() if key != "state_path"}
            != {key: value for key, value in expected_binding.items() if key != "state_path"}
        ):
            continue
        normalized_binding = None
        if isinstance(binding, dict) and isinstance(binding.get("state_path"), str):
            producer_path = Path(binding["state_path"])
            # A relative producer path needs the same explicit caller context.
            # Never guess an unavailable original working directory.
            known_context = producer_path.is_absolute() or (
                not Path(caller_path).is_absolute() and producer_path == Path(caller_path)
            )
            if known_context and _physical(producer_path) == path:
                normalized_binding = {**binding, "state_path": str(path)}
        if (
            proof.get("schema") == "closer-scoped-admission/v1"
            and proof.get("target") == target
            and proof.get("stage") == row.get("stage")
            and normalized_binding == expected_binding
            and proof.get("admitted") is admitted
            # The producer persists this admission snapshot before invocation;
            # even admitted=True retains callback_started=False. It alone does
            # not prove no effect; admitted mode requires the full source chain.
            and proof.get("callback_started") is False
            and (not admitted or proof.get("purpose") == "remote_effect")
            and type(proof.get("observed_at")) in {int, float}
            and type(row.get("stage_started_at")) in {int, float}
            and type(row.get("stage_deadline")) in {int, float}
            and row.get("stage_started_at", float("inf"))
            <= proof["observed_at"]
            <= row.get("stage_deadline", float("-inf"))
            and (
                (admitted and proof.get("reason") is None)
                or (
                    not admitted
                    and isinstance(proof.get("reason"), str)
                    and proof["reason"]
                    # Match the coordinator's exact retained error format.
                    and row.get("error") == "ScopeBlocked: " + proof["reason"][:200]
                )
            )
            and proof.get("input_sha256")
            == _digest(
                {
                    k: row.get("results", {})[k]
                    for k in pipeline.STAGES
                    if k in row.get("results", {})
                }
            )
        ):
            candidates.append({"path": str(candidate), "sha256": sha})
    if len(candidates) != 1:
        raise ValueError("missing_or_ambiguous_exact_pre_callback_proof")
    return candidates[0]


def _receipt(ref):
    if (
        not isinstance(ref, dict)
        or set(ref) != {"path", "sha256"}
        or not isinstance(ref.get("path"), str)
        or not ref["path"]
        or not isinstance(ref.get("sha256"), str)
        or len(ref["sha256"]) != 64
    ):
        raise ValueError("exact_source_receipt_required")
    value, digest = _read(ref["path"])
    if digest != ref["sha256"]:
        raise ValueError("source_receipt_digest_changed")
    return value


def _snapshot_digest(directory):
    # Load only the currently shipped installer, never receipt-selected code.
    base = Path(__file__).resolve().parent
    locations = [
        base / "scripts/install_verified_snapshot.py",
        base.parent / "scripts/install_verified_snapshot.py",
    ]
    script = next((path for path in locations if path.is_file()), None)
    if script is None:
        raise ValueError("trusted_snapshot_installer_unavailable")
    script = _physical(script)
    # Execute the shipped module without importlib's optional bytecode-cache
    # writes: read-only reconciliation must not mutate an installed generation.
    namespace = {"__file__": str(script), "__name__": "_reentry_trusted_installer"}
    exec(compile(script.read_bytes(), str(script), "exec"), namespace)
    return namespace["snapshot_digest"](directory)


def _prewrite_contract(candidate):
    # Resolve constants at call time so short, byte-pinned fixtures exercise the
    # same production proof chain without cloning a distribution.
    if candidate == PREWRITE_CANDIDATE:
        return {
            "candidate": PREWRITE_CANDIDATE,
            "source": PREWRITE_SOURCE_SHA256,
            "queue": PREWRITE_QUEUE_SHA256,
            "pipeline": PREWRITE_PIPELINE_SHA256,
            "reason": "existing or unattributable active verifier run",
            "source_alias": False,
        }
    if candidate == PREWRITE_TERMINAL_CANDIDATE:
        return {
            "candidate": PREWRITE_TERMINAL_CANDIDATE,
            "source": PREWRITE_TERMINAL_SOURCE_SHA256,
            "queue": PREWRITE_TERMINAL_QUEUE_SHA256,
            "pipeline": PREWRITE_TERMINAL_PIPELINE_SHA256,
            "reason": "collect existing verifier result before spending again",
            "source_alias": True,
        }
    raise ValueError("unproven_terminal_native_source")


def _native_pre_queue_source(
    path, frontier, target, entry, row, lease, receipts, *, prewrite=False
):
    if not isinstance(receipts, dict) or set(receipts) != {"lifecycle", "installation"}:
        raise ValueError("native_pre_queue_source_receipts_required")
    lifecycle = _receipt(receipts["lifecycle"])
    installation = _receipt(receipts["installation"])
    source = lifecycle.get("source_identity")
    verification = installation.get("verification")
    contract = _prewrite_contract(installation.get("candidate_sha")) if prewrite else None
    if (
        lifecycle.get("schema") != "direct-finite-storage/v2"
        or lifecycle.get("status") != "retired"
        or type(lifecycle.get("exit_code")) is not int
        or lifecycle["exit_code"] != 78
        or not isinstance(source, dict)
        or source.get("frontier") != frontier
        or installation.get("stage") != "installed"
        or installation.get("candidate_sha")
        != (contract["candidate"] if prewrite else PRE_QUEUE_CANDIDATE)
        or not isinstance(verification, dict)
        or verification.get("verified") is not True
        or type(verification.get("exit_code")) is not int
        or verification["exit_code"] != 0
        or not isinstance(source.get("installed_generation"), str)
        or not source["installed_generation"].startswith("generation-v1:")
        or source["installed_generation"] != installation.get("installed_generation")
    ):
        raise ValueError("unproven_terminal_native_source")
    if not isinstance(source.get("qualified_module"), str):
        raise ValueError("missing_native_source_module")
    module = _physical(source["qualified_module"])
    if (
        module.name != "closer_dispatcher.py"
        or not module.is_file()
        or module.stat().st_size > 2 * 1024 * 1024
        or hashlib.sha256(module.read_bytes()).hexdigest()
        != (contract["source"] if prewrite else PRE_QUEUE_SOURCE_SHA256)
    ):
        raise ValueError("unsupported_native_pre_queue_source")
    if prewrite:
        for name, expected in {
            "closer_queue.py": contract["queue"],
            "closer_pipeline.py": contract["pipeline"],
        }.items():
            selected = _physical(module.parent / name)
            if (
                not selected.is_file()
                or hashlib.sha256(selected.read_bytes()).hexdigest() != expected
            ):
                raise ValueError("unsupported_native_prewrite_consumer_source")
    if _snapshot_digest(module.parent) != verification.get("snapshot_digest"):
        raise ValueError("native_package_differs_from_verified_installation")
    stdout = _receipt(
        {"path": lifecycle.get("native_stdout"), "sha256": lifecycle.get("stdout_sha256")}
    )
    if (
        stdout.get("finite_frontier") != frontier
        or not isinstance(stdout.get("run_ids"), dict)
        or not isinstance(stdout.get("targets"), dict)
        or stdout["run_ids"].get(target) != entry["run_id"]
        or stdout["targets"].get(target) != row
    ):
        raise ValueError("native_stdout_does_not_bind_original_row")
    if prewrite:
        _native_prewrite_command(lifecycle, frontier)
    expected_lease = {
        "target": resources.canonical_target(target),
        "owner": lease["owner"],
        "epoch": lease["epoch"],
    }
    results = row["results"]
    prep, interpretation = results.get("prepare", {}), results.get("interpret", {})
    snapshot = _receipt(prep.get("snapshot_ref"))
    contract = {
        "kind": "native_remote_only",
        "target": target,
        "revision": row.get("revision"),
        "head": snapshot.get("head"),
        "action": "post_merge_verify" if prewrite else "merge",
    }
    if (
        row.get("status") != "unknown"
        or row.get("stage") != "act"
        or (
            row.get("error") != PREWRITE_ERROR
            if prewrite
            else row.get("error") not in PRE_QUEUE_ERRORS
        )
        or row.get("scope_lease") != expected_lease
        or "act" in results
        or "exception" in results
        or frontier["bindings"][target]["revision"] != row.get("revision")
        or snapshot.get("target") != target
        or snapshot.get("revision") != row.get("revision")
        or not isinstance(snapshot.get("head"), str)
        or len(snapshot["head"]) != 40
        or prep.get("revision") != row.get("revision")
        or prep.get("identity") != row.get("identity")
        or prep.get("native_effect_contract") != contract
        or not isinstance(prep.get("proposal"), dict)
        or prep["proposal"].get("action") != contract["action"]
        or interpretation.get("classification")
        != ("deterministic_post_merge_verify" if prewrite else "deterministic_merge")
        or interpretation.get("authority") is not False
        or interpretation.get("substantive_needs") != []
    ):
        raise ValueError("native_pre_queue_boundary_not_proven")
    return copy.deepcopy(receipts)


def _native_prewrite_command(lifecycle, frontier):
    """Only the pinned native CLI route can attest this legacy return branch."""
    source = lifecycle["source_identity"]
    argv = source.get("logical_command")
    if (
        not isinstance(argv, list)
        or not all(isinstance(arg, str) for arg in argv)
        or len(argv) < 3
        or Path(argv[1]).name != "closer_dispatcher.py"
        or argv[2] != "run"
        or hashlib.sha256(
            json.dumps([argv[0], source["qualified_module"], *argv[2:]]).encode()
        ).hexdigest()
        != lifecycle.get("argv_sha256")
    ):
        raise ValueError("unproven_native_prewrite_command")
    switches = {"--pipeline", "--apply", "--workers"}
    values = {
        "--assignment",
        "--state",
        "--finite-frontier-json",
        "--request-timeout-seconds",
        "--coordinator-mode",
        "--coordinator-contract-pin",
        "--max-workers",
    }
    parsed = {}
    index = 3
    while index < len(argv):
        flag = argv[index]
        if flag in parsed or flag not in switches | values:
            raise ValueError("unsupported_native_prewrite_command")
        if flag in switches:
            parsed[flag] = True
            index += 1
        else:
            if index + 1 >= len(argv):
                raise ValueError("unsupported_native_prewrite_command")
            parsed[flag] = argv[index + 1]
            index += 2
    if (
        set(parsed) != switches | values
        or parsed["--coordinator-mode"] != "resource_scoped"
        or parsed["--max-workers"] != "1"
        or json.loads(parsed["--finite-frontier-json"]) != frontier
        or not isinstance(json.loads(parsed["--coordinator-contract-pin"]), dict)
    ):
        raise ValueError("unsupported_native_prewrite_command")


def _native_prewrite_inner(path, frontier, target, row, *, contract):
    """One exact returned prewrite refusal; unrelated history is never consumed."""
    inner = path.parent / f"{path.stem}.actions" / f"{pipeline._digest(target)}.json"
    doc, sha = _read(inner)
    buckets = doc.get("frontiers")
    bucket = buckets.get(frontier["frontier_id"]) if isinstance(buckets, dict) else None
    items = bucket.get("items") if isinstance(bucket, dict) else None
    item = items.get(target) if isinstance(items, dict) else None
    round_state = bucket.get("round") if isinstance(bucket, dict) else None
    op = item.get("operation") if isinstance(item, dict) else None
    prep = row["results"]["prepare"]
    snapshot = _receipt(prep["snapshot_ref"])
    binding = op.get("binding") if isinstance(op, dict) else None
    subject = target
    if contract["source_alias"]:
        subject = snapshot.get("pr_target") or target
        try:
            resources.canonical_target(subject)
            match = resources._TARGET_RE.fullmatch(subject)
            if not match or int(match.group(3)) <= 0:
                raise ValueError("not a GitHub PR identity")
            if snapshot.get("kind") != "pr" or snapshot.get("state") != "MERGED":
                raise ValueError("not an exact merged PR snapshot")
        except (ValueError, TypeError, AttributeError):
            raise ValueError("native_prewrite_subject_association_not_proven") from None
        if subject != target and not (
            snapshot.get("kind") == "pr"
            and snapshot.get("state") == "MERGED"
            and isinstance(snapshot.get("sources"), list)
            and any(
                isinstance(source, dict)
                and source.get("target") == target
                and source.get("kind") != "pr_request"
                and isinstance(source.get("body"), str)
                and source["body"].strip()
                for source in snapshot["sources"]
            )
        ):
            raise ValueError("native_prewrite_subject_association_not_proven")
    if (
        doc.get("schema") != "closer-queue/v1"
        or not isinstance(bucket, dict)
        or bucket.get("policy") != frontier
        or not isinstance(item, dict)
        or not isinstance(op, dict)
        or not isinstance(binding, dict)
        or not isinstance(op.get("result"), dict)
        or not isinstance(round_state, dict)
        or item.get("operation_history") != []
        or op.get("status") != "failed"
        or op.get("outcome") != "deferred"
        or op.get("action") != "post_merge_verify"
        or op.get("finite_frontier") != frontier
        or op.get("revision") != row.get("revision")
        or op.get("attempts") != 1
        or not isinstance(op.get("id"), str)
        or not op["id"]
        or set(op.get("result", {})) != {"reason", "run_id"}
        or op["result"].get("reason") != contract["reason"]
        or type(op["result"].get("run_id")) is not int
        or op["result"]["run_id"] <= 0
        or binding.get("pr_target") != subject
        or binding.get("head") != snapshot.get("head")
        or binding.get("merge") != snapshot.get("merge")
        or not isinstance(snapshot.get("merge"), str)
        or len(snapshot["merge"]) != 40
        or item.get("revision") != row.get("revision")
        or item.get("deferred_reason") != "operation_failed"
        or round_state.get("status") != "finished"
        or round_state.get("finite_frontier") != frontier
        or round_state.get("frontier_target") != target
        or type(op.get("invoked_at")) not in {int, float}
        or type(op.get("elapsed_seconds")) not in {int, float}
        or not (
            row["stage_started_at"]
            <= op["invoked_at"]
            <= op["invoked_at"] + op["elapsed_seconds"]
            <= row["stage_deadline"]
        )
    ):
        raise ValueError("native_prewrite_inner_boundary_not_proven")

    receipt = round_state.get("receipt")
    events = round_state.get("events")
    submitted = (
        [
            event
            for event in events
            if isinstance(event, dict) and event.get("kind") == "operation_submitted"
        ]
        if isinstance(events, list)
        else []
    )
    if (
        type(op.get("attempts")) is not int
        or not isinstance(op.get("owner"), str)
        or op["owner"] != item.get("owner")
        or op["owner"] != round_state.get("owner")
        or type(op.get("submitted_at")) not in {int, float}
        or type(op.get("request_started_at")) not in {int, float}
        or not (
            row["stage_started_at"]
            <= op["submitted_at"]
            <= op["request_started_at"]
            <= op["invoked_at"]
        )
        or len(submitted) != 1
        or submitted[0].get("target") != target
        or submitted[0].get("action") != "post_merge_verify"
        or submitted[0].get("operation_id") != op["id"]
        or not isinstance(receipt, dict)
        or receipt.get("valid") is not False
        or receipt.get("stop_valid") is not False
        or receipt.get("reasons") != ["eligible_without_resource_reason:" + target]
        or type(receipt.get("finished_at")) not in {int, float}
        or not (
            op["invoked_at"] + op["elapsed_seconds"]
            <= receipt["finished_at"]
            <= row["stage_deadline"]
        )
        or receipt.get("accounting")
        != [
            {
                "target": target,
                "action": "post_merge_verify",
                "eligible": True,
                "next_trigger": "inspect_operation_failure",
                "observation_freshness": "fresh",
                "operation_id": op["id"],
                "operation_status": "failed",
                "reason": "operation_failed",
                "requires_coordinator": False,
                "revision": row["revision"],
                "status": "deferred",
            }
        ]
    ):
        raise ValueError("native_prewrite_intent_receipt_not_proven")

    # Even a matching result must not mask a provider/write uncertainty marker
    # anywhere in this queue. Other frontier effects remain untouched.
    def inspect(value):
        if isinstance(value, dict):
            if any(
                value.get(key)
                for key in (
                    "provider_outcome_uncertain",
                    "recovery_barrier",
                    "outcome_uncertain",
                    "error_type",
                )
            ) or value.get("status") in ("unknown", "uncertain", "running", "submitted"):
                raise ValueError("inner_operation_requires_reconciliation")
            for child in value.values():
                inspect(child)
        elif isinstance(value, list):
            for child in value:
                inspect(child)

    inspect(doc)
    boundary = {
        "path": str(inner),
        "sha256": sha,
        "operation_id": op["id"],
        "operation_sha256": _digest(op),
        "next_trigger": (
            f"github-verifier-result:{subject}:{op['result']['run_id']}:identity-reconciled"
            if contract["source_alias"]
            else f"github-verifier-resource:{target.split('#')[0]}:{op['result']['run_id']}:terminal"
        ),
    }
    if contract["source_alias"]:
        boundary.update(subject_pr=subject, source_contract=contract["candidate"])
    return boundary


def _retire_native_boundary(root, path, target, frontier, record):
    if record["plan"].get("kind") not in {
        "native_pre_queue_refusal",
        "native_prewrite_verifier_refusal",
    }:
        return
    state, _ = _read(path)
    if _digest(_entry(state, frontier)["items"][target]) != record["after_sha256"]:
        raise ValueError("reconciled_row_changed_before_retirement")
    if record["plan"].get("kind") == "native_prewrite_verifier_refusal":
        inner = record["plan"]["inner_boundary"]
        if _read(inner["path"])[1] != inner["sha256"]:
            raise ValueError("reconciled_inner_queue_changed_before_retirement")
    if not resources._release_known_shard_boundary(
        root,
        path,
        target,
        owner=record["before"]["scope_lease"]["owner"],
        now=time.time,
        run_id=state["frontiers"][frontier["frontier_id"]]["run_id"],
        finite_frontier=frontier,
    ):
        raise ValueError("known_native_boundary_retirement_pending")
    # The release helper can skip a contested/unreadable intent. A durable
    # terminal row is not evidence that every original-owner intent retired.
    for base in resources._reservation_bases(root):
        for directory in sorted(base.iterdir()) if base.is_dir() else []:
            intent = resources._read_intent(directory / "intent.json")
            if intent is not None and (
                intent.get("owner") == record["before"]["scope_lease"]["owner"]
                and intent.get("target") == resources.canonical_target(target)
            ):
                raise ValueError("known_native_boundary_retirement_pending")


def plan(state_path, *, frontier, target, event, source_receipts=None):
    """Read-only plan. A trigger is explicit evidence, never new action authority."""
    path = _physical(state_path)
    root = _physical(path.parent.parent.parent)
    if (
        path.name != "state.json"
        or path.parent.parent.name != "targets"
        or not root.name.endswith(".scoped")
    ):
        raise ValueError("finite_reentry_requires_target_shard")
    frontier = validate_frontier(frontier)
    if target not in frontier["targets"] or path.parent != resources.shard_dir(root, target):
        raise ValueError("target_shard_identity_mismatch")
    if not isinstance(event, str) or not event.strip() or len(event) > 512:
        raise ValueError("explicit_bounded_retry_event_required")
    state, _ = _read(path)
    lease, lease_sha = _read(path.parent / "lease.json")
    if (
        lease.get("target") != resources.canonical_target(target)
        or lease.get("finite_frontier") != frontier
        or type(lease.get("epoch")) is not int
        or lease["epoch"] < 1
        or not isinstance(lease.get("owner"), str)
        or not lease["owner"]
    ):
        raise ValueError("finite_lease_identity_changed")
    entry = _entry(state, frontier)
    if (
        not isinstance(entry, dict)
        or entry.get("finite_frontier") != frontier
        or entry.get("schema") != "closer-pipeline-frontier/v2"
        or entry.get("run_id")
        != pipeline._digest(["finite-frontier/v2", frontier["frontier_id"], frontier["owner"]])[:32]
    ):
        raise ValueError("finite_frontier_identity_changed")
    row = entry.get("items", {}).get(target)
    if (
        not isinstance(row, dict)
        or row.get("recovery_barrier") is not None
        or row.get("provider_outcome_uncertain") not in (None, False)
    ):
        raise ValueError("unresolved_provider_or_recovery_boundary")
    if not isinstance(row.get("status"), str) or row["status"] not in {"held", "unknown"}:
        raise ValueError("row_not_retryable")
    results = row.get("results", {})
    if not isinstance(results, dict) or any(
        key in results and not isinstance(results[key], dict) for key in pipeline.STAGES
    ):
        raise ValueError("malformed_callback_results")
    if any(
        result.get("status") is not None and not isinstance(result.get("status"), str)
        for result in results.values()
        if isinstance(result, dict)
    ):
        raise ValueError("malformed_callback_status")
    history = row.get("reentry_history", [])
    if not isinstance(history, list) or any(not isinstance(item, dict) for item in history):
        raise ValueError("malformed_reentry_history")
    if row.get("error") is not None and not isinstance(row["error"], str):
        raise ValueError("malformed_request_error")
    if any(
        isinstance(results.get(k), dict)
        and (
            results[k].get("outcome_uncertain")
            or results[k].get("status") in {"unknown", "uncertain", "running", "submitted"}
        )
        for k in pipeline.STAGES
    ):
        raise ValueError("unresolved_callback_result")
    stage, proof, transport = row.get("stage"), None, None
    if not isinstance(stage, str):
        raise ValueError("malformed_request_stage")
    kind = None
    if (
        row.get("status") == "held"
        and stage == "interpret"
        and results.get("interpret", {}).get("classification") == "assessment_transport_unavailable"
    ):
        import adapters

        problem = adapters.data_only_problem("claude", "assess")
        if problem:
            raise ValueError("assessment_transport_still_unqualified:" + problem)
        transport = {
            "path": adapters._data_only_claude_binary(),
            "sha256": adapters.DATA_ONLY_CLAUDE_SHA256,
        }
        kind = "qualified_assessment_transport"
    elif (
        row.get("status") == "held"
        and stage == "prepare"
        and row.get("error") == "ResourceBlocked: no io pool slot before the deadline"
    ):
        kind = "unstarted_prepare_pool"
    elif row.get("error") == PREWRITE_ERROR:
        source_receipts = _native_pre_queue_source(
            path, frontier, target, entry, row, lease, source_receipts, prewrite=True
        )
        proof = _scope_proof(root, path, target, entry, row, caller_path=state_path, admitted=True)
        kind = "native_prewrite_verifier_refusal"
    elif row.get("error") in PRE_QUEUE_ERRORS:
        source_receipts = _native_pre_queue_source(
            path, frontier, target, entry, row, lease, source_receipts
        )
        proof = _scope_proof(root, path, target, entry, row, caller_path=state_path, admitted=True)
        kind = "native_pre_queue_refusal"
    elif (row.get("error") or "").startswith("ScopeBlocked: "):
        proof = _scope_proof(root, path, target, entry, row, caller_path=state_path)
        kind = "pre_callback_scope_refusal"
    else:
        raise ValueError("no_supported_unstarted_boundary")
    if stage not in {"prepare", "interpret", "act", "exception"}:
        raise ValueError("unsupported_retry_stage")
    if stage == "exception" and "exception" in results:
        raise ValueError("exception_callback_result_requires_reconciliation")
    inner = (
        _native_prewrite_inner(
            path,
            frontier,
            target,
            row,
            contract=_prewrite_contract(_receipt(source_receipts["installation"])["candidate_sha"]),
        )
        if kind == "native_prewrite_verifier_refusal"
        else _inner_boundary(path, target)
    )
    trigger_fingerprint = _digest(
        {
            "kind": kind,
            "stage": stage,
            "transport": transport,
            "retained_results": results,
            "scope_reason": row.get("error") if proof else None,
        }
    )
    if any(
        item.get("trigger_fingerprint") == trigger_fingerprint
        for item in row.get("reentry_history", [])
    ):
        raise ValueError("unchanged_boundary_already_retried")
    result = {
        "schema": SCHEMA,
        "state_path": str(path),
        "target": target,
        "frontier": frontier,
        "event": event,
        "row_sha256": _digest(row),
        "kind": kind,
        "stage": stage,
        "scope_proof": proof,
        "transport": transport,
        "lease_sha256": lease_sha,
        "trigger_fingerprint": trigger_fingerprint,
        "inner_boundary": inner,
        "acceptance_authority": False,
        "global_stop_authority": False,
    }
    if kind in {"native_pre_queue_refusal", "native_prewrite_verifier_refusal"}:
        result["source_receipts"] = source_receipts
    return {**result, "plan_sha256": _digest(result)}


@contextlib.contextmanager
def _exclusive(path):
    path = _physical(path)
    with path.open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise resources.ResourceBlocked("active_finite_writer") from exc
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def apply(state_path, *, frontier, target, event, expected_plan_sha256, source_receipts=None):
    """CAS one target under existing target/run/inventory/state ownership locks."""
    path = _physical(state_path)
    frontier = validate_frontier(frontier)
    root = _physical(path.parent.parent.parent)
    key = _digest([frontier, target, event])
    audit = root / "reentry-audit" / (key + ".json")
    with _exclusive(path.with_name(path.name + ".runlock")), _exclusive(path.parent / "lease.lock"):
        with resources.inventory_lock(root, exclusive=True, deadline=time.time() + 10):
            with pipeline._locked(path, timeout_seconds=10):
                if audit.exists():
                    record, _ = _read(audit)
                    if not isinstance(record.get("plan"), dict):
                        raise ValueError("malformed_reentry_audit_plan")
                    if (
                        not isinstance(record.get("before"), dict)
                        or not isinstance(record.get("after"), dict)
                        or _digest(record["after"]) != record.get("after_sha256")
                    ):
                        raise ValueError("malformed_reentry_audit_rows")
                    if record.get("plan", {}).get("plan_sha256") != expected_plan_sha256:
                        raise ValueError("retry_event_already_used_for_another_plan")
                    state, _ = _read(path)
                    row = _entry(state, frontier)["items"][target]
                    if record.get("status") == "prepared":
                        if _digest(row) == _digest(record.get("before")):
                            fresh = plan(
                                state_path,
                                frontier=frontier,
                                target=target,
                                event=event,
                                source_receipts=source_receipts,
                            )
                            if fresh["plan_sha256"] != expected_plan_sha256:
                                raise ValueError("prepared_reentry_inputs_changed")
                            state["frontiers"][frontier["frontier_id"]]["items"][target] = record[
                                "after"
                            ]
                            pipeline._save_snapshot(path, state)
                        elif _digest(row) != record.get("after_sha256"):
                            raise ValueError("prepared_reentry_state_uncertain")
                        record["status"] = "applied"
                        resources._atomic_write(audit, record)
                    elif record.get("status") != "applied":
                        raise ValueError("invalid_reentry_audit")
                    _retire_native_boundary(root, path, target, frontier, record)
                    return {"status": "applied", "idempotent": True, "audit": str(audit)}
                proposal = plan(
                    state_path,
                    frontier=frontier,
                    target=target,
                    event=event,
                    source_receipts=source_receipts,
                )
                if proposal["plan_sha256"] != expected_plan_sha256:
                    raise ValueError("reentry_plan_changed")
                state, _ = _read(path)
                entry = state["frontiers"][frontier["frontier_id"]]
                before = copy.deepcopy(entry["items"][target])
                row = entry["items"][target]
                history = row.setdefault("reentry_history", [])
                prior_request = {
                    key: value for key, value in before.items() if key != "reentry_history"
                }
                history.append(
                    {
                        "event": event,
                        "plan_sha256": expected_plan_sha256,
                        "trigger_fingerprint": proposal["trigger_fingerprint"],
                        "previous": copy.deepcopy(prior_request),
                        "at": time.time(),
                    }
                )
                if len(history) > 32:
                    raise ValueError("reentry_history_bound")
                row.pop("error", None)
                row.pop("deferred_next_trigger", None)
                row.pop("scope_refusal", None)
                if proposal["stage"] in {"act", "exception"}:
                    # No effect occurred, but the real historical scope is still
                    # unavailable. Reconciliation cannot turn that into readiness.
                    row.update(
                        status="held",
                        stage_status="completed",
                        deferred_reason="scope_quarantined",
                        deferred_next_trigger="reconcile_scope_before_retry",
                    )
                    row.pop("reentry_ready", None)
                    row["scope_proof"] = proposal["scope_proof"]
                    row["resource_evidence"] = {
                        "scope_refusal": proposal["scope_proof"],
                        "confirmed_not_started": True,
                    }
                    if proposal["kind"] == "native_pre_queue_refusal":
                        # ACT was entered. Only the inner operation is proven
                        # absent; never relabel its admitted callback as unstarted.
                        row.update(
                            deferred_reason="native_pre_queue_refusal",
                            deferred_next_trigger="reconcile_legacy_dependency_before_retry",
                        )
                        row["resource_evidence"] = {
                            "pre_queue_admission": proposal["scope_proof"],
                            "source_receipts": proposal["source_receipts"],
                            "confirmed_no_effect": True,
                        }
                    elif proposal["kind"] == "native_prewrite_verifier_refusal":
                        # The callback ran and returned before POST. Preserve the
                        # original failed inner operation byte for byte; this is
                        # terminal classification, never a retry permission.
                        row.update(
                            deferred_reason="native_prewrite_verifier_refusal",
                            deferred_next_trigger=proposal["inner_boundary"]["next_trigger"],
                        )
                        row["resource_evidence"] = {
                            "prewrite_admission": proposal["scope_proof"],
                            "source_receipts": proposal["source_receipts"],
                            "inner_boundary": proposal["inner_boundary"],
                            "confirmed_no_effect": True,
                        }
                else:
                    # Keep a known no-effect boundary until the normal target
                    # admission and new lease succeed. A bare pending row would
                    # correctly be censused as an abandoned unresolved owner.
                    row.update(status="held", stage_status="queued")
                    row["reentry_ready"] = {"event": event, "plan_sha256": expected_plan_sha256}
                    if proposal["kind"] == "qualified_assessment_transport":
                        row.get("results", {}).pop("interpret", None)
                record = {
                    "schema": SCHEMA,
                    "plan": proposal,
                    "before": before,
                    "after": copy.deepcopy(row),
                    "after_sha256": _digest(row),
                    "status": "prepared",
                    "at": time.time(),
                }
                audit.parent.mkdir(parents=True, exist_ok=True)
                resources._atomic_write(audit, record)
                pipeline._save_snapshot(path, state)
                record["status"] = "applied"
                resources._atomic_write(audit, record)
                _retire_native_boundary(root, path, target, frontier, record)
                return {
                    "status": "applied",
                    "idempotent": False,
                    "audit": str(audit),
                    "stage": row["stage"],
                    "row_status": row["status"],
                }


def ready(state_path, frontier, target, row):
    """Validate the exact audited hold before its production lease consumes it."""
    marker = row.get("reentry_ready")
    if (
        not isinstance(marker, dict)
        or row.get("status") != "held"
        or row.get("stage_status") != "queued"
    ):
        raise ValueError("invalid_reentry_ready_boundary")
    path = _physical(state_path)
    root = path.parent.parent.parent
    # The resource fence registry deliberately uses the lease's caller-form
    # pathname; physical paths remain mandatory for filesystem/audit checks.
    identity = resources.fence_identity(Path(state_path))
    fence = resources.fence_for(Path(state_path))
    if identity is None or fence is None:
        raise ValueError("reentry_requires_live_production_lease")
    fence()
    lease, _ = _read(path.parent / "lease.json")
    if lease.get("finite_frontier") != frontier or identity != {
        "target": resources.canonical_target(target),
        "owner": lease.get("owner"),
        "epoch": lease.get("epoch"),
    }:
        raise ValueError("reentry_live_lease_identity_changed")
    key = _digest([frontier, target, marker.get("event")])
    audit, _ = _read(root / "reentry-audit" / (key + ".json"))
    proposal = audit.get("plan", {})
    if not isinstance(proposal, dict):
        raise ValueError("malformed_reentry_audit_plan")
    if (
        audit.get("schema") != SCHEMA
        or audit.get("status") != "applied"
        or proposal.get("frontier") != frontier
        or proposal.get("target") != target
        or proposal.get("state_path") != str(path)
        or proposal.get("plan_sha256") != marker.get("plan_sha256")
        or audit.get("after_sha256") != _digest(row)
        or not isinstance(row.get("stage"), str)
        or row["stage"] not in {"prepare", "interpret"}
    ):
        raise ValueError("reentry_audit_does_not_bind_ready_row")
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", required=True, help="exact finite target shard state.json")
    parser.add_argument("--frontier", required=True, help="frozen finite-frontier/v2 JSON file")
    parser.add_argument("--target", required=True)
    parser.add_argument("--event", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-plan-sha256")
    parser.add_argument(
        "--source-receipts-json", help="hash-bound native lifecycle/installation refs JSON"
    )
    args = parser.parse_args(argv)
    if args.apply and not args.expected_plan_sha256:
        parser.error("--apply requires --expected-plan-sha256")
    try:
        frontier, _ = _read(args.frontier)
        common = {"frontier": frontier, "target": args.target, "event": args.event}
        if args.source_receipts_json:
            common["source_receipts"], _ = _read(args.source_receipts_json)
        result = (
            apply(args.state, **common, expected_plan_sha256=args.expected_plan_sha256)
            if args.apply
            else plan(args.state, **common)
        )
        print(json.dumps(result, sort_keys=True))
        return 0
    except (OSError, ValueError, KeyError, resources.ResourceBlocked) as exc:
        print(json.dumps({"status": "held", "reason": str(exc), "acceptance_authority": False}))
        return 78


if __name__ == "__main__":
    raise SystemExit(main())
