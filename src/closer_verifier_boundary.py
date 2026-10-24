"""Bound native verifier identity reads before the verify-label mutation.

These receipts confer no acceptance or mutation authority. They separate a
known prewrite collection gap from an unknown effect and retain exact reads.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import stat
import time
from datetime import datetime
from pathlib import Path

import closer_evidence as evidence
from closer_attribution_store import AttributionStorageWait, Store

SCHEMA = "closer-verifier-prewrite-read/v1"
CAUSES = frozenset(
    {
        "active_inventory_incomplete",
        "active_identity_incomplete",
        "history_incomplete",
        "merge_time_missing",
        "existing_delivery",
        "delivery_revision_mismatch",
        "attribution_pending",
        "state_changed",
        "read_unavailable",
        "capacity_deferred",
        "request_deadline",
        "storage_capacity",
    }
)
SHA = re.compile(r"[0-9a-f]{40}")


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def logical(value):
    """Only semantic input progress belongs in a re-entry trigger."""
    if isinstance(value, list):
        return [logical(x) for x in value]
    if not isinstance(value, dict):
        return value
    if "workflow_runs" in value and "total_count" in value:
        return {
            "total_count": value.get("total_count"),
            "workflow_runs": [
                logical_run(r) for r in value.get("workflow_runs", []) if isinstance(r, dict)
            ],
        }
    if "artifacts" in value and "total_count" in value:
        return {
            "total_count": value.get("total_count"),
            "artifacts": [
                {
                    k: logical(a.get(k))
                    for k in ("id", "name", "digest", "expired", "size_in_bytes", "workflow_run")
                }
                for a in value.get("artifacts", [])
                if isinstance(a, dict)
            ],
        }
    if "target" in value and "revision" in value:
        return {
            k: value.get(k)
            for k in ("target", "pr_target", "revision", "head", "merge", "state", "labels")
        }
    if "path" in value and "sha256" in value:
        return {k: value.get(k) for k in ("sha256", "bytes")}
    return {
        k: logical(v)
        for k, v in value.items()
        if not k.endswith("_url")
        and k not in {"url", "updated_at", "observed_at", "expires_at", "node_id"}
    }


def logical_run(run):
    linked = run.get("pull_requests")
    associations = (
        sorted(p["number"] for p in linked)
        if isinstance(linked, list)
        and all(
            isinstance(p, dict) and type(p.get("number")) is int and p["number"] > 0 for p in linked
        )
        else logical(linked)
    )
    return {
        k: logical(run.get(k))
        for k in (
            "id",
            "run_attempt",
            "head_sha",
            "status",
            "conclusion",
            "event",
            "path",
            "created_at",
        )
    } | {
        "repository": (run.get("repository") or {}).get("full_name"),
        "pull_requests": associations,
    }


def observation(
    endpoint, value=None, *, error=None, root=None, reference="legacy:unresolved", deadline=None
):
    result = {
        "endpoint": endpoint,
        "status": "unavailable" if error else "observed",
        "sha256": digest({"error_type": type(error).__name__} if error else logical(value)),
    }
    if root is not None and error is None:
        result["raw"] = Store(root, deadline=deadline).retain(
            json.dumps(logical(value), sort_keys=True).encode(), "json", reference=reference
        )
    return result


def pending(snapshot, subject, cause, reads, requirements, *, control=None):
    proof = {
        "schema": SCHEMA,
        "boundary": "before_verify_label_post",
        "target": snapshot["target"],
        "subject_pr": subject,
        "revision": snapshot["revision"],
        "head": snapshot["head"],
        "merge": snapshot["merge"],
        "cause": cause,
        "observed_at": time.time(),
        "observations": reads[-16:],
        "requirements": requirements,
        "blocking_run": None,
    }
    if control is not None:
        proof["control"] = control
    proof["sha256"] = digest(proof)
    return {
        "status": "resource_wait",
        "reason": "verifier_resource_wait",
        "verifier_resource_wait": proof,
        "next_trigger": event(proof),
    }


def event(proof):
    control = proof.get("control")
    if proof["cause"] == "capacity_deferred" and isinstance(control, dict):
        reset = control.get("reset_at_epoch")
        trigger = (
            str(math.ceil(reset))
            if type(reset) in {int, float} and math.isfinite(reset)
            else "authoritative-capacity-recovery"
        )
        return f"github-verifier-capacity:{proof['subject_pr']}:{control['resource']}:{trigger}"
    if proof["cause"] == "request_deadline":
        return (
            f"github-verifier-request:{proof['subject_pr']}:{proof['revision']}:fresh-owned-request"
        )
    progress = digest(
        {
            "cause": proof["cause"],
            "observations": [
                {k: r[k] for k in ("endpoint", "status", "sha256")} for r in proof["observations"]
            ],
            "requirements": proof["requirements"],
        }
    )
    return f"github-verifier-evidence:{proof['subject_pr']}:{progress}:fresh-read-progress"


def validate(operation, target):
    try:
        proof = operation["result"]["verifier_resource_wait"]
        binding = operation["binding"]
        reads = proof["observations"]
        requirements = proof["requirements"]
        if (
            operation.get("status") != "resource_wait"
            or operation.get("action") not in {"post_merge_verify", "initiate_verify"}
            or proof.get("schema") != SCHEMA
            or proof.get("boundary") != "before_verify_label_post"
            or proof.get("sha256") != digest({k: v for k, v in proof.items() if k != "sha256"})
            or proof.get("target") != target
            or proof.get("subject_pr") != binding.get("pr_target", target)
            or proof.get("revision") != operation.get("revision")
            or proof.get("head") != binding.get("head")
            or proof.get("merge") != binding.get("merge")
            or not SHA.fullmatch(proof["head"])
            or not SHA.fullmatch(proof["merge"])
            or proof.get("cause") not in CAUSES
            or proof.get("blocking_run") is not None
            or not isinstance(reads, list)
            or not 1 <= len(reads) <= 16
            or any(
                not isinstance(r, dict)
                or set(r)
                not in ({"endpoint", "status", "sha256"}, {"endpoint", "status", "sha256", "raw"})
                or not isinstance(r["endpoint"], str)
                or not 1 <= len(r["endpoint"]) <= 1024
                or r["status"] not in {"observed", "unavailable"}
                or not re.fullmatch("[0-9a-f]{64}", r["sha256"])
                for r in reads
            )
            or not isinstance(requirements, list)
            or not 1 <= len(requirements) <= 8
            or any(not isinstance(r, str) or not 1 <= len(r) <= 512 for r in requirements)
            or type(proof.get("observed_at")) not in {int, float}
            or not math.isfinite(proof["observed_at"])
            or not operation["invoked_at"] <= proof["observed_at"] <= operation["completed_at"]
        ):
            return None
        control = proof.get("control")
        if proof["cause"] == "capacity_deferred":
            if (
                not isinstance(control, dict)
                or control.get("kind") != "capacity"
                or control.get("resource") not in {"core", "graphql"}
                or not isinstance(control.get("throttle"), dict)
                or control["throttle"].get("action") != "defer"
                or type(control.get("observed_at")) not in {int, float}
                or not math.isfinite(control["observed_at"])
                or (
                    control.get("reset_at_epoch") is not None
                    and (
                        type(control["reset_at_epoch"]) not in {int, float}
                        or not math.isfinite(control["reset_at_epoch"])
                    )
                )
            ):
                return None
        elif proof["cause"] == "request_deadline":
            if (
                not isinstance(control, dict)
                or control.get("kind") != "request_deadline"
                or any(
                    type(control.get(k)) not in {int, float} or not math.isfinite(control[k])
                    for k in ("deadline", "remaining_seconds")
                )
            ):
                return None
        elif control is not None:
            return None
        return proof
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


def timestamp(value):
    if not isinstance(value, str) or not value:
        raise ValueError("missing verifier time")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("unqualified verifier timezone")
    return parsed.timestamp()


def explicitly_foreign(run, subject):
    """A native PR-event association is authoritative; manual inputs are not."""
    repo, number = subject.rsplit("#", 1)
    linked = run.get("pull_requests") if isinstance(run, dict) else None
    return (
        isinstance(linked, list)
        and bool(linked)
        and all(
            isinstance(p, dict) and type(p.get("number")) is int and p["number"] > 0 for p in linked
        )
        and int(number) not in [p["number"] for p in linked]
        and run.get("event") == "pull_request_target"
        and run.get("path") == ".github/workflows/agents-verifier.yml"
        and run.get("repository", {}).get("full_name", "").lower() == repo.lower()
        and run.get("status") == "completed"
    )


def unique_members(pairs):
    """Ambiguous manifest identities cannot authorize skipping a run."""
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate native manifest key")
        result[key] = value
    return result


def attribute(
    run,
    snapshot,
    api,
    artifact_fetch,
    root: Path,
    *,
    deadline,
    reads,
    acquisition_budget=None,
    retain_reads=True,
):
    """Only an intact native capture may prove a different PR's manual run."""
    read_root = root if retain_reads else None
    reference = digest([snapshot["target"], snapshot.get("revision"), "verifier-attribution"])
    subject = snapshot.get("pr_target") or snapshot["target"]
    repo, number = subject.rsplit("#", 1)
    rid = run.get("id")
    attempt = run.get("run_attempt")
    if (
        type(rid) is not int
        or rid <= 0
        or type(attempt) is not int
        or attempt < 1
        or run.get("repository", {}).get("full_name", "").lower() != repo.lower()
        or run.get("path") != ".github/workflows/agents-verifier.yml"
        or run.get("event") not in {"workflow_dispatch", "pull_request_target"}
        or run.get("status") != "completed"
    ):
        return "pending", "complete native run identity"
    endpoint = f"repos/{repo}/actions/runs/{rid}/artifacts?per_page=100"
    try:
        inventory = api(endpoint)
        reads.append(
            observation(endpoint, inventory, root=read_root, reference=reference, deadline=deadline)
        )
        rows = inventory.get("artifacts")
        if (
            not isinstance(rows, list)
            or inventory.get("total_count") != len(rows)
            or len(rows) > evidence.MAX_ARTIFACTS
        ):
            return "pending", f"complete artifact inventory for run {rid}"
        # Both native modes publish the same production input identity. The
        # report family is not identity or acceptance authority; dual captures
        # remain ambiguous rather than preferring one provider/mode's report.
        capture_names = evidence.native_capture_artifact_names(rid)
        candidates = [a for a in rows if isinstance(a, dict) and a.get("name") in capture_names]
        if len(candidates) != 1:
            return "pending", f"unambiguous native verifier capture for run {rid}"
        a = candidates[0]
        owner = a.get("workflow_run") or {}
        expected = a.get("digest")
        if (
            owner.get("id") != rid
            or owner.get("head_sha") != run.get("head_sha")
            or not isinstance(expected, str)
            or not re.fullmatch("sha256:[0-9a-f]{64}", expected)
            or type(a.get("size_in_bytes")) is not int
            or not 0 < a["size_in_bytes"] <= evidence.MAX_ARCHIVE_BYTES
        ):
            return "pending", f"exact server-bound verifier archive for run {rid}"
        sha = expected.split(":")[1]
        cached = root / "raw" / sha[:2] / (sha + ".zip")
        if cached.exists() or cached.is_symlink():
            metadata = cached.lstat()
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_size > evidence.MAX_ARCHIVE_BYTES
                or not cached.resolve().is_relative_to(root.resolve())
            ):
                return "pending", f"bounded regular retained archive for run {rid}"
            with cached.open("rb") as stream:
                data = stream.read(evidence.MAX_ARCHIVE_BYTES + 1)
        else:
            if a.get("expired"):
                return "pending", f"verified retained archive for expired run {rid}"
            if acquisition_budget is not None:
                if acquisition_budget["remaining"] <= 0:
                    return (
                        "pending",
                        "continue verifier attribution after retained acquisition progress",
                    )
                acquisition_budget["remaining"] -= 1
            data = artifact_fetch(a, deadline=deadline, max_bytes=evidence.MAX_ARCHIVE_BYTES)
        if (
            not isinstance(data, bytes)
            or len(data) > evidence.MAX_ARCHIVE_BYTES
            or hashlib.sha256(data).hexdigest() != sha
        ):
            return "pending", f"archive digest verification for run {rid}"
        store = Store(root, deadline=deadline)
        archive_ref = store.retain(data, "zip", reference=reference, expired=bool(a.get("expired")))
        parsed = evidence._native_manifest_from_archive(
            data,
            root,
            retain=lambda _root, value, suffix: store.retain(value, suffix, reference=reference),
        )
        reads.append(
            observation(
                f"archive:{a['id']}:{expected}",
                {
                    "archive": archive_ref,
                    "manifests": [
                        {
                            "manifest": {
                                k: r["manifest"].get(k)
                                for k in (
                                    "schema",
                                    "capture_kind",
                                    "repository",
                                    "pr",
                                    "merge_sha",
                                    "source_run_id",
                                    "source_run_attempt",
                                    "context_sha256",
                                    "diff_sha256",
                                    "diff_summary_sha256",
                                )
                            },
                            "raw": r["raw"],
                            "omissions": r.get("omissions", []),
                        }
                        for r in parsed
                    ],
                },
                root=read_root,
                reference=reference,
                deadline=deadline,
            )
        )
        if len(parsed) != 1:
            return "pending", f"unambiguous intact input manifest for run {rid}"
        capture = parsed[0]
        manifest = capture["manifest"]
        # Attribution needs the authenticated producer identity, not a complete
        # acceptance report. Missing optional capture files remain retained gaps;
        # present contradictory hashes or ambiguous manifest keys are not usable.
        with Path(capture["raw"]["path"]).open("rb") as stream:
            raw_manifest = stream.read(evidence.MAX_NATIVE_INPUT_BYTES + 1)
        if (
            len(raw_manifest) > evidence.MAX_NATIVE_INPUT_BYTES
            or json.loads(raw_manifest, object_pairs_hook=unique_members) != manifest
        ):
            return "pending", f"unambiguous bounded input manifest for run {rid}"
        hash_keys = ("context_sha256", "diff_sha256", "diff_summary_sha256")
        if (
            manifest.get("schema") != "workflows-verifier-input-snapshot/v1"
            or manifest.get("capture_kind") != "production"
            or manifest.get("repository", "").lower() != repo.lower()
            or str(manifest.get("source_run_id")) != str(rid)
            or type(manifest.get("pr")) is not int
            or manifest["pr"] <= 0
            or not SHA.fullmatch(str(manifest.get("merge_sha", "")))
            or (
                (attempt != 1 or "source_run_attempt" in manifest)
                and str(manifest.get("source_run_attempt")) != str(attempt)
            )
            or any(
                key in manifest and not re.fullmatch("[0-9a-f]{64}", str(manifest[key]))
                for key in hash_keys
            )
            or any(
                str(omission.get("reason", "")).endswith(" mismatch")
                for omission in capture.get("omissions", [])
            )
        ):
            return "pending", f"consistent server-bound input manifest for run {rid}"
        linked = run.get("pull_requests")
        if (
            not isinstance(linked, list)
            or any(
                not isinstance(p, dict) or type(p.get("number")) is not int or p["number"] <= 0
                for p in linked
            )
            or (linked and manifest["pr"] not in [p["number"] for p in linked])
        ):
            return "pending", f"reconcile contradictory native PR association for run {rid}"
        if manifest["pr"] != int(number):
            return "foreign", None
        if manifest["merge_sha"] != snapshot["merge"]:
            return "mismatch", f"current merge attribution for same PR run {rid}"
        return "same", f"collect existing verifier result for run {rid}"
    except evidence.NativeInputCaptureError as exc:
        reads.append(observation(endpoint, error=exc))
        return "pending", f"{exc}; recover sealed input for run {rid}"
    except Exception as exc:
        if isinstance(exc, AttributionStorageWait):
            raise
        if evidence._is_round_control_exception(exc):
            raise
        reads.append(observation(endpoint, error=exc))
        return (
            "pending",
            f"retry exact verifier attribution read for run {rid} after transport/resource recovery",
        )


def cached_archive(root, sha):
    """Reuse acquired archive bytes only under an explicit caller-owned root."""
    if not isinstance(sha, str) or not re.fullmatch("[0-9a-f]{64}", sha):
        return None
    path = Path(root) / "raw" / sha[:2] / (sha + ".zip")
    if not path.exists() and not path.is_symlink():
        return None
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_size > evidence.MAX_ARCHIVE_BYTES
        or not path.resolve().is_relative_to(Path(root).resolve())
    ):
        raise ValueError("unsafe retained verifier archive")
    with path.open("rb") as stream:
        data = stream.read(evidence.MAX_ARCHIVE_BYTES + 1)
    if len(data) > evidence.MAX_ARCHIVE_BYTES or hashlib.sha256(data).hexdigest() != sha:
        raise ValueError("retained verifier archive digest mismatch")
    return data


class HistoryIncomplete(ValueError):
    """The current server page cannot establish relevant history coverage."""


def history_runs(repo, merge_time, read):
    """Walk the existing unfiltered REST history until the merge cutoff."""
    page = 1
    seen = set()
    consumed = 0
    expected_total = None
    oldest = None
    while True:
        endpoint = f"repos/{repo}/actions/workflows/agents-verifier.yml/runs?per_page=100"
        if page > 1:
            endpoint += f"&page={page}"
        doc = read(endpoint)
        rows = doc.get("workflow_runs")
        total = doc.get("total_count")
        if (
            not isinstance(rows, list)
            or len(rows) > 100
            or type(total) is not int
            or total < 0
            or total < consumed + len(rows)
            or (expected_total is not None and total != expected_total)
        ):
            raise HistoryIncomplete("consistent complete verifier history pages")
        expected_total = total
        try:
            times = [timestamp(r["created_at"]) for r in rows]
        except (ValueError, TypeError, KeyError):
            raise HistoryIncomplete("complete verifier run timestamps") from None
        if times != sorted(times, reverse=True) or (
            oldest is not None and times and max(times) > oldest
        ):
            raise HistoryIncomplete("stable newest-first verifier history pages")
        if not rows and consumed < total:
            raise HistoryIncomplete("nonempty remaining verifier history page")
        for run, created in zip(rows, times, strict=True):
            key = (run.get("id"), run.get("run_attempt"))
            if key in seen:
                raise HistoryIncomplete("nonrepeating verifier history pages")
            seen.add(key)
            if created < merge_time:
                return
            yield run
        consumed += len(rows)
        if consumed >= total:
            return
        oldest = min(times) if times else oldest
        page += 1
