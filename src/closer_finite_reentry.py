"""Exact recovery of proven unstarted requests or known accepted-request receipts.

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
import os
import threading
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
# Immutable producer/consumer contract for an invoked guard's preflight refusal.
MERGE_GUARD_CANDIDATE = "f16b568c6e3f5876bb2f91900e21b11820114b6a"
MERGE_GUARD_SOURCE_SHA256 = "315d88c1b3067ff58de28a60b0a3ba08c73734d7faf543afe7fd3bef1de28bca"
MERGE_GUARD_QUEUE_SHA256 = "6364b944e4b4dcab1d725eda6a5fcaf39ab1c7cf3b9f00200785966d848a81c7"
MERGE_GUARD_PIPELINE_SHA256 = "0371e7970fb88a595e64ab6d23f14011e7b378846e5a78ec55688888e64790e7"
MERGE_GUARD_SCRIPT_SHA256 = "6e3c6d682cba153e07141df4f3c3dae87dae876acc4ab8c4d54e6af78b1f45e4"
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
    if candidate == MERGE_GUARD_CANDIDATE:
        return {
            "candidate": candidate,
            "source": MERGE_GUARD_SOURCE_SHA256,
            "queue": MERGE_GUARD_QUEUE_SHA256,
            "pipeline": MERGE_GUARD_PIPELINE_SHA256,
            "guard": MERGE_GUARD_SCRIPT_SHA256,
            "action": "merge",
            "source_alias": False,
        }
    raise ValueError("unproven_terminal_native_source")


def _native_pre_queue_source(
    path, frontier, target, entry, row, lease, receipts, *, prewrite=False
):
    if not isinstance(receipts, dict) or set(receipts) not in (
        {"lifecycle", "installation"},
        {"lifecycle", "installation", "invocation"},
    ):
        raise ValueError("native_pre_queue_source_receipts_required")
    lifecycle = _receipt(receipts["lifecycle"])
    installation = _receipt(receipts["installation"])
    source = lifecycle.get("source_identity")
    verification = installation.get("verification")
    contract = _prewrite_contract(installation.get("candidate_sha")) if prewrite else None
    merge_refusal = bool(contract and contract.get("action") == "merge")
    if merge_refusal:
        if not isinstance(source, dict) or not isinstance(verification, dict):
            raise ValueError("unproven_native_merge_guard_installation")
        # This released caller recorded a hash-bound installed readback and a
        # separately retained argv, rather than duplicating them in lifecycle.
        readback = _receipt(
            {
                "path": source.get("installed_receipt") if isinstance(source, dict) else None,
                "sha256": (
                    source.get("installed_receipt_sha256") if isinstance(source, dict) else None
                ),
            }
        )
        invocation = _receipt(receipts.get("invocation"))
        if (
            readback.get("installed_source") != contract["candidate"]
            or readback.get("installed_generation") != installation.get("installed_generation")
            or readback.get("snapshot_digest") != verification.get("snapshot_digest")
            or readback.get("matches_strict_verified_payload") is not True
            or source.get("snapshot_digest") != readback.get("snapshot_digest")
            or Path(source.get("qualified_module", "")).parent.name != readback.get("mirror_target")
        ):
            raise ValueError("unproven_native_merge_guard_installation")
        # Local projection only: original lifecycle/invocation bytes stay intact.
        lifecycle = copy.deepcopy(lifecycle)
        source = lifecycle["source_identity"]
        source["installed_generation"] = readback["installed_generation"]
        source["logical_command"] = invocation.get("command")
    elif set(receipts) != {"lifecycle", "installation"}:
        raise ValueError("unexpected_native_source_receipt")
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
            **({"merge_guard.py": contract["guard"]} if merge_refusal else {}),
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
        "action": "post_merge_verify" if prewrite and not merge_refusal else "merge",
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
        != (
            "deterministic_post_merge_verify"
            if prewrite and not merge_refusal
            else "deterministic_merge"
        )
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
    merge_refusal = contract.get("action") == "merge"
    action = "merge" if merge_refusal else "post_merge_verify"
    if merge_refusal:
        import merge_guard

        if (
            not isinstance(op, dict)
            or not isinstance(op.get("result"), dict)
            or not merge_guard.preflight_refusal(op["result"].get("result"), subject)
        ):
            raise ValueError("native_merge_guard_preflight_not_proven")
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
        or op.get("outcome") != ("blocked" if merge_refusal else "deferred")
        or op.get("action") != action
        or op.get("finite_frontier") != frontier
        or op.get("revision") != row.get("revision")
        or op.get("attempts") != 1
        or not isinstance(op.get("id"), str)
        or not op["id"]
        or (
            set(op.get("result", {})) != {"result"}
            if merge_refusal
            else (
                set(op.get("result", {})) != {"reason", "run_id"}
                or op["result"].get("reason") != contract["reason"]
                or type(op["result"].get("run_id")) is not int
                or op["result"]["run_id"] <= 0
            )
        )
        or binding.get("pr_target") != subject
        or binding.get("head") != snapshot.get("head")
        or binding.get("merge") != snapshot.get("merge")
        or (
            snapshot.get("merge") is not None
            if merge_refusal
            else not isinstance(snapshot.get("merge"), str) or len(snapshot["merge"]) != 40
        )
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
        or submitted[0].get("action") != action
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
                "action": action,
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
            "refresh_merge_guard_before_action"
            if merge_refusal
            else (
                f"github-verifier-result:{subject}:{op['result']['run_id']}:identity-reconciled"
                if contract["source_alias"]
                else f"github-verifier-resource:{target.split('#')[0]}:{op['result']['run_id']}:terminal"
            )
        ),
    }
    if contract["source_alias"]:
        boundary.update(subject_pr=subject, source_contract=contract["candidate"])
    return boundary


def _retire_native_boundary(root, path, target, frontier, record):
    if record["plan"].get("kind") not in {
        "native_pre_queue_refusal",
        "native_prewrite_verifier_refusal",
        "accepted_verifier_request_receipt",
        "native_merge_guard_refusal",
    }:
        return
    state, _ = _read(path)
    if _digest(_entry(state, frontier)["items"][target]) != record["after_sha256"]:
        raise ValueError("reconciled_row_changed_before_retirement")
    if record["plan"].get("kind") in {
        "native_prewrite_verifier_refusal",
        "accepted_verifier_request_receipt",
        "native_merge_guard_refusal",
    }:
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


def _accepted_request_boundary(path, frontier, target, row):
    """Reclassify an accepted POST receipt, without retrying the callback."""
    import closer_queue

    action = row.get("results", {}).get("act") or {}
    if not (
        row.get("status") == "unknown"
        and row.get("stage") == "act"
        and row.get("stage_status") == "completed"
        and row.get("error") == "action wait lacks exact verified no-effect queue proof"
        and action.get("status") == "pending"
        and action.get("pending_kind") == "external_event"
        and isinstance(action.get("next_trigger"), str)
        and action["next_trigger"].strip()
    ):
        raise ValueError("not_an_accepted_request_receipt_boundary")
    inner = _physical(path.parent / f"{path.stem}.actions" / f"{pipeline._digest(target)}.json")
    _document, sha = _read(inner)
    proof = closer_queue.accepted_verifier_wait_proof(
        inner,
        target,
        frontier=frontier,
        receipt=action.get("queue_receipt"),
        evidence=action.get("pending_evidence"),
    )
    if proof is None:
        raise ValueError("accepted_request_receipt_not_authenticated")
    return {"path": str(inner), "sha256": sha, "accepted_request": proof}


def _scope_readmission(root, path, frontier, target, entry, row, *, inventory_owner=None):
    """Prove exact no-start lineage and fresh admission, never effect authority."""
    import closer_dispatcher
    import closer_quarantine

    history = row.get("reentry_history")
    if not isinstance(history, list) or not history or not isinstance(history[-1], dict):
        raise ValueError("scope_readmission_requires_applied_audit")
    previous = history[-1]
    origin_path = (
        root / "reentry-audit" / (_digest([frontier, target, previous.get("event")]) + ".json")
    )
    origin, origin_sha = _read(origin_path)
    proposal = origin.get("plan", {})
    before = origin.get("before", {})
    if (
        origin.get("schema") != SCHEMA
        or origin.get("status") != "applied"
        or not isinstance(proposal, dict)
        or not isinstance(before, dict)
        or proposal.get("kind") != "pre_callback_scope_refusal"
        or proposal.get("stage") != "act"
        or proposal.get("frontier") != frontier
        or proposal.get("target") != target
        or proposal.get("state_path") != str(path)
        or proposal.get("plan_sha256") != previous.get("plan_sha256")
        or _digest({k: v for k, v in proposal.items() if k != "plan_sha256"})
        != proposal.get("plan_sha256")
        or origin.get("after") != row
        or origin.get("after_sha256") != _digest(row)
        or row.get("status") != "held"
        or row.get("stage") != "act"
        or row.get("stage_status") != "completed"
        or row.get("deferred_reason") != "scope_quarantined"
        or row.get("resource_evidence", {}).get("confirmed_not_started") is not True
        or row.get("scope_proof") != proposal.get("scope_proof")
        or row.get("revision") != frontier["bindings"][target]["revision"]
        or any(key in row.get("results", {}) for key in ("act", "exception", "reconcile"))
    ):
        raise ValueError("scope_readmission_audit_does_not_bind_current_hold")
    proof_ref = _scope_proof(root, path, target, entry, before, caller_path=path)
    proof = _receipt(proof_ref)
    if proof_ref != proposal.get("scope_proof") or not resources.valid_scope_refusal(
        path, target, row, entry["run_id"]
    ):
        raise ValueError("scope_readmission_refusal_proof_changed")
    keys = proof.get("keys")
    if (
        proof.get("purpose") != "remote_effect"
        or not isinstance(keys, list)
        or not keys
        or keys != sorted(set(keys))
        or proof.get("reason") not in {"quarantined_resource:" + key for key in keys}
    ):
        raise ValueError("unsupported_scope_readmission_refusal")
    results = row["results"]
    prep = results.get("prepare", {})
    context = {**results, "revision": row["revision"]}
    snapshot = _receipt(prep.get("snapshot_ref"))
    if not closer_dispatcher._remote_only_effect(target, context):
        raise ValueError("scope_readmission_requires_bound_native_remote_effect")
    old_scope = proof["snapshot"]
    workspaces = {
        "schema": closer_quarantine.WORKSPACE_SCHEMA,
        "historical": old_scope.get("workspaces", {}),
        "execution": old_scope.get("execution_workspaces", {}),
    }
    derived = closer_dispatcher._prepared_resource_keys(
        target, prep, snapshot, {}, infer_workspace=False
    )
    expected = resources._scope_keys(target, context, lambda *_: derived, infer_workspaces=False)
    if keys != expected:
        raise ValueError("scope_readmission_prepared_keys_changed")
    inner = _inner_boundary(path, target)
    if inner != proposal.get("inner_boundary"):
        raise ValueError("scope_readmission_inner_boundary_changed")
    current = closer_quarantine.scope_snapshot(
        old_scope["state_path"], workspaces=workspaces, inventory_owner=inventory_owner
    )
    problem = closer_quarantine.scope_problem(
        current, target, keys, purpose="remote_effect", inventory_owner=inventory_owner
    )
    if problem:
        raise ValueError("scope_readmission_still_refused:" + problem)
    return {
        "origin_audit": {"path": str(origin_path), "sha256": origin_sha},
        "scope_proof": proof_ref,
        "inner_boundary": inner,
        "keys": keys,
        "purpose": "remote_effect",
        "fresh_scope_admitted": True,
    }


_GUARD_READ_SEAL = object()
_guard_now = time.time


class _GuardAdmission:
    """Caller-owned READ result; cannot be supplied by serialized plan inputs."""

    def __init__(self, path, row, assignment, value):
        self.seal = _GUARD_READ_SEAL
        self.pid, self.thread = os.getpid(), threading.get_ident()
        self.path, self.row_sha = str(path), _digest(row)
        self.assignment = str(_physical(assignment))
        self.assignment_sha = _read(assignment)[1]
        self.value, self.read_at = value, time.monotonic()


def _guard_admission(path, target, row, assignment, frontier):
    """Fresh original guard admission; no effect / verdict or source-equivalence grant."""
    import closer_dispatcher as dispatcher
    import closer_frontier_policy
    import merge_guard

    if assignment is None:
        raise ValueError("guard_readmission_requires_current_assignment")
    rows = dispatcher.assignment_rows_from_file(assignment)
    expected = row["results"]["prepare"].get("assignment_fingerprint")
    if target not in rows or dispatcher.assignment_dependency(
        assignment, target, expected_fingerprint=expected
    ):
        raise ValueError("guard_readmission_assignment_changed")
    backend = dispatcher.Closer(
        dispatcher.GitHub(
            deadline=time.time() + closer_frontier_policy.MAX_REQUEST_TIMEOUT_SECONDS
        ),
        state_path=path.parent / f"{path.stem}.actions" / f"{pipeline._digest(target)}.json",
        apply=False,
        workers=False,
    )
    backend.finite_frontier = frontier
    observed = backend.observe(target)
    head = row["results"]["prepare"]["native_effect_contract"]["head"]
    if (
        observed.get("eligible") is not True
        or observed.get("action") != "merge"
        or observed.get("revision") != row.get("revision")
        or observed.get("head") != head
    ):
        raise ValueError("guard_readmission_requires_fresh_immutable_binding")
    report = merge_guard.read_only_admission(observed.get("pr_target", target), head)
    if not (
        report.get("blocked") is False
        and report.get("dry_run") is True
        and report.get("admission_complete") is True
        and report.get("remote_only_guard_final", {}).get("remote_only") is True
        and report.get("remote_only_guard_final", {}).get("runtime_ac_required") is False
    ):
        raise ValueError("guard_readmission_still_refused")
    final = report["final_preflight"]
    value = {
        "target": target,
        "head": head,
        "revision": observed["revision"],
        "assignment_row_sha256": _digest(rows[target]),
        "preflight": {
            k: final.get(k)
            for k in (
                "head",
                "active_threads",
                "check_state",
                "review_not_before",
                "review_anchor",
                "review_floor_remaining_seconds",
            )
        },
        "metadata_sha256": _digest(report["metadata"]),
        "remote_only_guard": report["remote_only_guard_final"],
        "review_loop": report["review_loop_final"],
        "guard_source_sha256": hashlib.sha256(Path(merge_guard.__file__).read_bytes()).hexdigest(),
    }
    return _GuardAdmission(path, row, assignment, value)


def _guard_readmission(
    root, path, frontier, target, entry, row, assignment, *, read=None, inventory_owner=None
):
    import closer_dispatcher as dispatcher
    import closer_frontier_policy
    import closer_quarantine as quarantine
    import closer_queue as queue

    act = row.get("results", {}).get("act", {})
    if not (
        row.get("status") == "held"
        and row.get("stage") == "act"
        and row.get("stage_status") == "completed"
        and not row.get("error")
        and row.get("deferred_reason") == "merge_guard_refused"
        and act.get("status") == "deferred"
        and act.get("reason") == "merge_guard_refused"
    ):
        raise ValueError("not_a_typed_guard_hold")
    inner = _physical(path.parent / f"{path.stem}.actions" / f"{pipeline._digest(target)}.json")
    doc, sha = _read(inner)
    state = queue.state_for_frontier(doc, frontier["frontier_id"], expected_policy=frontier)
    receipt = act.get("queue_receipt")
    if not isinstance(state, dict) or not isinstance(receipt, dict):
        raise ValueError("guard_readmission_missing_receipt")
    checked = queue._validate_data(
        state, receipt, assignment_targets=[target], now_value=time.time(), holder_fn=lambda _: None
    )
    item = state["items"][target]
    operation = item.get("operation")
    proof = queue.merge_guard_refusal_proof(operation, target)
    if (
        not checked.get("stop_valid")
        or proof is None
        or state["round"].get("receipt") != receipt
        or operation.get("finite_frontier") != frontier
        or operation.get("revision") != frontier["bindings"][target]["revision"]
        or closer_frontier_policy.action_identity(
            target, operation["revision"], "merge", operation["binding"]
        )
        != frontier["bindings"][target]["action_id"]
        or any(
            queue.merge_guard_refusal_proof(old, target) is None
            for old in item.get("operation_history", [])
        )
    ):
        raise ValueError("guard_readmission_effect_boundary_unproven")
    before_callback = {**row, "results": {k: v for k, v in row["results"].items() if k != "act"}}
    original = _scope_proof(
        root, path, target, entry, before_callback, caller_path=path, admitted=True
    )
    original_scope = _receipt(original)
    context = {**before_callback["results"], "revision": row["revision"]}
    if not dispatcher._remote_only_effect(target, context):
        raise ValueError("guard_readmission_not_remote_only")
    snapshot = _receipt(context["prepare"]["snapshot_ref"])
    derived = dispatcher._prepared_resource_keys(
        target, context["prepare"], snapshot, {}, infer_workspace=False
    )
    keys = resources._scope_keys(target, context, lambda *_: derived, infer_workspaces=False)
    if keys != original_scope.get("keys"):
        raise ValueError("guard_readmission_prepared_keys_changed")
    old_scope = original_scope["snapshot"]
    current = quarantine.scope_snapshot(
        old_scope["state_path"],
        workspaces={
            "schema": quarantine.WORKSPACE_SCHEMA,
            "historical": old_scope.get("workspaces", {}),
            "execution": old_scope.get("execution_workspaces", {}),
        },
        inventory_owner=inventory_owner,
    )
    problem = quarantine.scope_problem(
        current, target, keys, purpose="remote_effect", inventory_owner=inventory_owner
    )
    if problem:
        raise ValueError("guard_readmission_scope_refused:" + problem)
    if read is None:
        read = _guard_admission(path, target, row, assignment, frontier)
    if not (
        type(read) is _GuardAdmission
        and read.seal is _GUARD_READ_SEAL
        and read.pid == os.getpid()
        and read.thread == threading.get_ident()
        and read.path == str(path)
        and read.row_sha == _digest(row)
        and read.assignment == str(_physical(assignment))
        and read.assignment_sha == _read(assignment)[1]
        and 0
        <= time.monotonic() - read.read_at
        <= closer_frontier_policy.MAX_REQUEST_TIMEOUT_SECONDS
    ):
        raise ValueError("guard_readmission_read_not_current")
    return {
        "original_admission": original,
        "inner_boundary": {"path": str(inner), "sha256": sha},
        "operation_id": operation["id"],
        "guard_admission": read.value,
        "keys": keys,
        "purpose": "remote_effect",
    }


def plan(
    state_path,
    *,
    frontier,
    target,
    event,
    source_receipts=None,
    assignment_path=None,
    _inventory_owner=None,
    _guard_read=None,
):
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
    if not isinstance(row.get("status"), str) or row["status"] not in {
        "held",
        "unknown",
        "capacity_blocked",
    }:
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
    readmission = None
    guard_readmission = None
    followup_proof = None
    current_result = results.get(stage, {})
    if row.get("stage_status") not in {"running", "unknown"} and (
        row.get("status") == "capacity_blocked"
        or current_result.get("classification") == "role_qualification_unavailable"
    ):
        import closer_hold_followup

        if assignment_path is None:
            raise ValueError("hold_followup_requires_current_assignment")
        followup_proof = closer_hold_followup.release_probe(
            path, frontier, target, row, assignment_path, source_receipts
        )
        kind = followup_proof["kind"]
    elif row.get("deferred_reason") == "merge_guard_refused" and stage == "act":
        guard_readmission = _guard_readmission(
            root,
            path,
            frontier,
            target,
            entry,
            row,
            assignment_path,
            read=_guard_read,
            inventory_owner=_inventory_owner,
        )
        inner = guard_readmission["inner_boundary"]
        kind = "native_guard_readmission"
    elif (
        row.get("status") == "held"
        and stage == "act"
        and row.get("stage_status") == "completed"
        and row.get("deferred_reason") == "scope_quarantined"
        and not row.get("error")
    ):
        readmission = _scope_readmission(
            root, path, frontier, target, entry, row, inventory_owner=_inventory_owner
        )
        proof = readmission["scope_proof"]
        kind = "scope_no_callback_readmission"
    elif row.get("error") == "action wait lacks exact verified no-effect queue proof":
        inner = _accepted_request_boundary(path, frontier, target, row)
        kind = "accepted_verifier_request_receipt"
    elif (
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
        kind = (
            "native_merge_guard_refusal"
            if _prewrite_contract(_receipt(source_receipts["installation"])["candidate_sha"]).get(
                "action"
            )
            == "merge"
            else "native_prewrite_verifier_refusal"
        )
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
    if stage == "exception" and "exception" in results and followup_proof is None:
        raise ValueError("exception_callback_result_requires_reconciliation")
    inner = (
        _native_prewrite_inner(
            path,
            frontier,
            target,
            row,
            contract=_prewrite_contract(_receipt(source_receipts["installation"])["candidate_sha"]),
        )
        if kind in {"native_prewrite_verifier_refusal", "native_merge_guard_refusal"}
        else (
            inner
            if kind in {"accepted_verifier_request_receipt", "native_guard_readmission"}
            else _inner_boundary(path, target)
        )
    )
    trigger_fingerprint = _digest(
        {
            "kind": kind,
            "stage": stage,
            "transport": transport,
            "retained_results": results,
            "scope_reason": row.get("error") if proof else None,
            **({"followup_proof": followup_proof} if followup_proof else {}),
            **({"scope_readmission": readmission} if readmission else {}),
            **({"guard_readmission": guard_readmission} if guard_readmission else {}),
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
    if followup_proof:
        result["followup_proof"] = followup_proof
        result["source_receipts"] = source_receipts
        result["assignment_path"] = str(_physical(assignment_path))
    if readmission:
        result["scope_readmission"] = readmission
    if guard_readmission:
        result["guard_readmission"] = guard_readmission
        result["assignment_path"] = str(_physical(assignment_path))
    if kind in {
        "native_pre_queue_refusal",
        "native_prewrite_verifier_refusal",
        "native_merge_guard_refusal",
    }:
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


@contextlib.contextmanager
def _accepted_receipt_transaction(path, frontier, target, audit):
    """Keep inner bytes stable through outer commit, audit and retirement."""
    import closer_queue

    state, _ = _read(path)
    row = _entry(state, frontier)["items"][target]
    prior = _read(audit)[0] if audit.exists() else {}
    proposal = prior.get("plan")
    if proposal is not None and not isinstance(proposal, dict):
        raise ValueError("malformed_reentry_audit_plan")
    proposal = proposal or {}
    accepted = proposal.get("kind") == "accepted_verifier_request_receipt"
    scope_readmission = proposal.get("kind") == "scope_no_callback_readmission" or (
        row.get("stage") == "act"
        and row.get("status") == "held"
        and row.get("deferred_reason") == "scope_quarantined"
        and not row.get("error")
    )
    guard_readmission = (
        proposal.get("kind") == "native_guard_readmission"
        or row.get("deferred_reason") == "merge_guard_refused"
    )
    if (
        not accepted
        and not scope_readmission
        and not guard_readmission
        and row.get("error") != "action wait lacks exact verified no-effect queue proof"
    ):
        yield
        return
    inner = _physical(path.parent / f"{path.stem}.actions" / f"{pipeline._digest(target)}.json")
    if not inner.is_file() and not scope_readmission:
        raise ValueError("accepted_request_inner_queue_missing")
    with closer_queue._ledger_lock(inner, cutoff=time.time() + 1, now=time.time) as locked:
        if not locked:
            raise resources.ResourceBlocked("active_accepted_request_queue_writer")
        if (accepted or proposal.get("kind") == "native_guard_readmission") and _read(inner)[
            1
        ] != proposal["inner_boundary"]["sha256"]:
            raise ValueError("accepted_request_queue_changed_before_commit")
        yield


def apply(
    state_path,
    *,
    frontier,
    target,
    event,
    expected_plan_sha256,
    source_receipts=None,
    assignment_path=None,
):
    """CAS one target under existing target/run/inventory/state ownership locks."""
    path = _physical(state_path)
    frontier = validate_frontier(frontier)
    root = _physical(path.parent.parent.parent)
    key = _digest([frontier, target, event])
    audit = root / "reentry-audit" / (key + ".json")
    guard_read = None
    pending_state, _ = _read(path)
    pending_row = _entry(pending_state, frontier)["items"][target]
    if pending_row.get("deferred_reason") == "merge_guard_refused" and (
        not audit.exists() or _read(audit)[0].get("status") == "prepared"
    ):
        guard_read = _guard_admission(path, target, pending_row, assignment_path, frontier)
    with _exclusive(path.with_name(path.name + ".runlock")), _exclusive(path.parent / "lease.lock"):
        with resources.inventory_lock(
            root, exclusive=True, deadline=time.time() + 10
        ) as inventory_owner:
            with pipeline._locked(path, timeout_seconds=10), _accepted_receipt_transaction(
                path, frontier, target, audit
            ):
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
                                assignment_path=assignment_path,
                                _guard_read=guard_read,
                                _inventory_owner=inventory_owner,
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
                    assignment_path=assignment_path,
                    _guard_read=guard_read,
                    _inventory_owner=inventory_owner,
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
                if proposal["kind"] == "accepted_verifier_request_receipt":
                    # An effect DID occur. Correct only the outer classification;
                    # preserve the original callback and prohibit automatic replay.
                    action = row["results"]["act"]
                    row.update(
                        status="held",
                        stage_status="completed",
                        deferred_reason="accepted_verifier_request",
                        deferred_next_trigger=action["next_trigger"],
                        queue_receipt=copy.deepcopy(action["queue_receipt"]),
                        resource_evidence={
                            "accepted_verifier_request": copy.deepcopy(
                                proposal["inner_boundary"]["accepted_request"]
                            )
                        },
                    )
                    row.pop("reentry_ready", None)
                elif proposal["kind"] in {"qualified_role_followup", "available_capacity_followup"}:
                    # This is a proven no-effect scheduling release, never reuse
                    # old PASS/readiness. A normal live lease must run PREPARE.
                    row.update(status="held", stage="prepare", stage_status="queued", results={})
                    row["reentry_ready"] = {
                        "event": event,
                        "plan_sha256": expected_plan_sha256,
                        "kind": proposal["kind"],
                    }
                elif proposal["kind"] in {
                    "scope_no_callback_readmission",
                    "native_guard_readmission",
                }:
                    row.update(status="held", stage_status="queued")
                    row["reentry_ready"] = {
                        "event": event,
                        "plan_sha256": expected_plan_sha256,
                        "kind": proposal["kind"],
                    }
                elif proposal["stage"] in {"act", "exception"}:
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
                    elif proposal["kind"] in {
                        "native_prewrite_verifier_refusal",
                        "native_merge_guard_refusal",
                    }:
                        # The callback ran and returned before POST. Preserve the
                        # original failed inner operation byte for byte; this is
                        # terminal classification, never a retry permission.
                        row.update(
                            deferred_reason=proposal["kind"],
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
                if proposal["kind"] == "native_guard_readmission":
                    import closer_evidence

                    boundary = proposal["inner_boundary"]
                    raw = Path(boundary["path"]).read_bytes()
                    if hashlib.sha256(raw).hexdigest() != boundary["sha256"]:
                        raise ValueError("guard_queue_changed_before_audit")
                    ref = closer_evidence._retain_bytes(
                        root / "reentry-audit" / "inner", raw, "json"
                    )
                    record["inner_before"] = {k: ref[k] for k in ("path", "sha256")}
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


def guard_queue_readmission(context, inner_path, frontier, target, inner_sha):
    """Consume only an audited modern no-command retry in its live native ACT."""
    if not isinstance(context, dict) or context.get("finite_frontier") != frontier:
        return False
    marker = context.get("guard_readmission")
    if not isinstance(marker, dict) or marker.get("kind") != "native_guard_readmission":
        return False
    try:
        path = _physical(context["state_path"])
        root = path.parent.parent.parent
        expected_inner = path.parent / f"{path.stem}.actions" / f"{pipeline._digest(target)}.json"
        if _physical(inner_path) != expected_inner:
            return False
        audit_path = (
            root / "reentry-audit" / (_digest([frontier, target, marker["event"]]) + ".json")
        )
        audit, _ = _read(audit_path)
        proposal = audit["plan"]
        state, _ = _read(path)
        row = _entry(state, frontier)["items"][target]
        identity = resources.fence_identity(path)
        fence = resources.fence_for(path)
        if identity is None or fence is None:
            return False
        fence()
        return bool(
            audit.get("schema") == SCHEMA
            and audit.get("status") == "applied"
            and proposal.get("kind") == "native_guard_readmission"
            and proposal.get("frontier") == frontier
            and proposal.get("target") == target
            and proposal.get("state_path") == str(path)
            and proposal.get("plan_sha256") == marker.get("plan_sha256")
            and _digest({k: v for k, v in proposal.items() if k != "plan_sha256"})
            == proposal["plan_sha256"]
            and proposal["inner_boundary"] == {"path": str(inner_path), "sha256": inner_sha}
            and audit.get("after_sha256") == _digest(audit["after"])
            and _read(audit["inner_before"]["path"])[1]
            == inner_sha
            == audit["inner_before"]["sha256"]
            and row.get("status") == "running"
            and row.get("stage") == "act"
            and row.get("stage_status") == "running"
            and row.get("scope_readmission_consumed") == marker
            and row.get("work_key") == context.get("work_key")
            and identity is not None
            and row.get("scope_lease") == identity
            and row.get("recovery_barrier") is None
            and row.get("provider_outcome_uncertain") in (None, False)
            and row.get("reentry_history", [])[-1].get("plan_sha256") == proposal["plan_sha256"]
            and all(
                row["results"].get(k) == audit["before"]["results"].get(k)
                for k in ("prepare", "interpret")
            )
        )
    except (OSError, ValueError, TypeError, KeyError, IndexError, resources.ResourceBlocked):
        return False


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
        or (
            row["stage"] not in {"prepare", "interpret"}
            and not (
                row["stage"] == "act"
                and proposal.get("kind")
                in {"scope_no_callback_readmission", "native_guard_readmission"}
                and marker.get("kind") == proposal.get("kind")
            )
        )
    ):
        raise ValueError("reentry_audit_does_not_bind_ready_row")
    if proposal.get("kind") in {"qualified_role_followup", "available_capacity_followup"}:
        import closer_hold_followup

        fresh = closer_hold_followup.release_probe(
            path,
            frontier,
            target,
            audit["before"],
            proposal["assignment_path"],
            proposal["source_receipts"],
        )
        if fresh != proposal.get("followup_proof"):
            raise ValueError("hold_followup_current_binding_changed")
    if proposal.get("kind") == "native_guard_readmission":
        state, _ = _read(path)
        fresh = _guard_readmission(
            root,
            path,
            frontier,
            target,
            _entry(state, frontier),
            audit["before"],
            proposal.get("assignment_path"),
        )
        if fresh != proposal.get("guard_readmission"):
            raise ValueError("guard_readmission_current_binding_changed")
    if proposal.get("kind") == "scope_no_callback_readmission":
        if _digest({k: v for k, v in proposal.items() if k != "plan_sha256"}) != proposal.get(
            "plan_sha256"
        ):
            raise ValueError("scope_readmission_plan_changed")
        state, _ = _read(path)
        entry = _entry(state, frontier)
        before = audit.get("before")
        if not isinstance(before, dict):
            raise ValueError("scope_readmission_missing_origin")
        fresh = _scope_readmission(root, path, frontier, target, entry, before)
        expected = proposal.get("scope_readmission", {})
        # The new row/lease changes census bytes. Require fresh success with the
        # same exact lineage and aliases, not a stale snapshot hash equality.
        if any(
            fresh.get(key) != expected.get(key)
            for key in ("origin_audit", "scope_proof", "inner_boundary", "keys", "purpose")
        ):
            raise ValueError("scope_readmission_binding_changed")
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", required=True, help="exact finite target shard state.json")
    parser.add_argument("--frontier", required=True, help="frozen finite-frontier/v2 JSON file")
    parser.add_argument("--target", required=True)
    parser.add_argument("--event", required=True)
    parser.add_argument("--assignment", help="current canonical assignment for guard readmission")
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
        if args.assignment:
            common["assignment_path"] = args.assignment
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
