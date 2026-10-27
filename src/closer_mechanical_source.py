"""Source-owned mechanical CI facts; no model command or write qualification.

The production transport is the existing native GitHub reader. Its documents
and exact raw check log are retained in the existing evidence custody. Parsed
negative CI findings are NOT added to any acceptance proof index. Source hints
carry no repair permit; a future repair callback must recollect the same facts
and independently qualify its exact boundary under the original operation.
"""

from __future__ import annotations

import base64
import hashlib
import importlib.metadata
import json
import math
import re
import sysconfig
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from urllib.parse import quote

import closer_disposition as disposition
import closer_evidence as evidence
import closer_execution_contracts as contracts
import closer_local_tool_confinement as confinement
import closer_mechanical_catalog as catalog

# Independently read canonical Workflows source at 045948bc. A different callee
# stays unsupported until its actual source is reviewed; @main is not a pin.
WORKFLOWS_CI_SHA256 = "173119d790ffcc0d6c9a84f341eeb13ee8c56f4b9c7b4946c1f1a67f809cd7f4"
WORKFLOW_PATH = ".github/workflows/ci.yml"
CALLEE_PATH = ".github/workflows/reusable-10-ci-python.yml"
MAX_DOCUMENT_BYTES = 256 * 1024
MAX_LOG_BYTES = 64 * 1024
MAX_RUNS = 8
MAX_JOBS = 100
_SOURCE_ISSUER = object()
_HOST_ISSUER = object()
SOURCE_ROOT = Path(__file__).resolve().parents[1]


class SourceUnavailable(ValueError):
    pass


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _retain(root, raw, suffix):
    ref = evidence._retain_bytes(root, raw, suffix)
    if disposition._safe_raw(ref, root, max_bytes=MAX_DOCUMENT_BYTES) != raw:
        raise SourceUnavailable("source_custody_readback_UNMET")
    return ref


def _document(gh, endpoint, root, refs):
    value = gh.api(endpoint, method="GET")
    raw = _json(value)
    if len(raw) > MAX_DOCUMENT_BYTES:
        raise SourceUnavailable("source_document_bound_UNMET")
    refs.append(_retain(root, raw, "json"))
    return value


def _content(gh, repo, path, head, root, refs):
    value = _document(gh, f"repos/{repo}/contents/{quote(path, safe='/')}?ref={head}", root, refs)
    if (
        not isinstance(value, dict)
        or value.get("encoding") != "base64"
        or value.get("type") != "file"
    ):
        raise SourceUnavailable("source_file_content_UNMET")
    try:
        raw = base64.b64decode(value["content"].replace("\n", ""), validate=True)
    except (ValueError, TypeError, KeyError) as exc:
        raise SourceUnavailable("source_file_encoding_UNMET") from exc
    if (
        len(raw) > MAX_DOCUMENT_BYTES
        or value.get("size") != len(raw)
        or value.get("sha") != hashlib.sha1(f"blob {len(raw)}\0".encode() + raw).hexdigest()
    ):
        raise SourceUnavailable("source_file_blob_identity_UNMET")
    return raw


def parse_diagnostics(operation, text, paths):
    """Parse data only, with no command interpretation or path inference."""
    if not isinstance(text, str) or len(text.encode()) > MAX_LOG_BYTES:
        raise SourceUnavailable("source_check_log_bound_UNMET")
    if not isinstance(paths, list) or not 1 <= len(paths) <= 5 or len(set(paths)) != len(paths):
        raise SourceUnavailable("source_check_paths_UNMET")
    if any(
        not isinstance(path, str)
        or not re.fullmatch(r"[A-Za-z0-9_./-]+\.py", path)
        or path.startswith("/")
        or ".." in path.split("/")
        for path in paths
    ):
        raise SourceUnavailable("source_check_paths_UNMET")
    result = []
    for line in text.splitlines():
        # GitHub's timestamps are transport, not part of a tool diagnostic.
        line = re.sub(r"^\d{4}-\d\d-\d\dT[0-9:.]+Z ", "", line)
        if operation == "format":
            match = re.fullmatch(r"would reformat ([A-Za-z0-9_./-]+\.py)", line)
            if match and match[1] in paths:
                result.append({"path": match[1], "code": "black-format", "message": line})
        elif operation == "lint_fix":
            match = re.fullmatch(r"([A-Za-z0-9_./-]+\.py):(\d+):(\d+): ([A-Z]+\d+) (.+)", line)
            if match and match[1] in paths and match[4].startswith(("E4", "E7", "E9", "F", "I")):
                result.append({"path": match[1], "code": match[4], "message": line})
    if not result or {row["path"] for row in result} != set(paths):
        raise SourceUnavailable("exact_source_check_diagnostic_paths_UNMET")
    return result


def _installation(policy):
    """Read fixed installed package metadata/bytes, never PATH/login/CLI probes."""
    dist = importlib.metadata.distribution(policy.tool)
    if dist.version != policy.version:
        raise SourceUnavailable("installed_tool_version_UNMET")
    binary = confinement._canonical(
        Path(sysconfig.get_path("scripts")) / policy.tool, directory=False
    )
    return {
        "executable": str(binary),
        "executable_sha256": confinement._hash(binary),
        # Distribution metadata does not establish transitive runtime reads.
        "missing": "independent_installed_tool_read_closure_UNMET",
    }


@dataclass(frozen=True)
class SourceRegistration:
    target: str
    head: str
    revision: str
    source_packet_sha256: str
    source_packet_ref_json: str
    retained_root: str
    tests_json: str
    hints_json: str
    source_refs_json: str
    missing: tuple[str, ...]
    _issuer: object = field(default=None, repr=False, compare=False)

    @property
    def registered_tests(self):
        return json.loads(self.tests_json)

    @property
    def model_hints(self):
        return json.loads(self.hints_json)

    @property
    def qualified(self):
        return False  # No installed confinement proof/issuer exists in this source.


def registration_matches(registration, snapshot, packet_ref):
    if type(registration) is not SourceRegistration or registration._issuer is not _SOURCE_ISSUER:
        return False
    try:
        original_ref = json.loads(registration.source_packet_ref_json)
        if original_ref != packet_ref:
            return False
        packet, root = contracts.load_retained_packet(original_ref)
        if str(root.resolve(strict=True)) != registration.retained_root:
            return False
        contracts._context(snapshot, packet, original_ref, root)
        refs = json.loads(registration.source_refs_json)
        if (
            not isinstance(refs, list)
            or len(refs) > 256
            or any(
                disposition._safe_raw(ref, root, max_bytes=MAX_DOCUMENT_BYTES) is None
                for ref in refs
            )
        ):
            return False
        inventory = {
            row["finding_id"]: row["raw"]
            for row in disposition.pending_nonpass_findings(snapshot, packet)
        }
        native_index = disposition._native_resolution_index(
            packet, root, snapshot.get("verifier") or {}
        )
        native_report_hashes = {key[1] for key in native_index}
        for test in registration.registered_tests.values():
            finding = inventory.get(test["finding_id"])
            if finding is None:
                return False
            if finding.get("source") in {"native_provider_verdict", "native_provider_concern"}:
                if finding.get("report_sha256") not in native_report_hashes:
                    return False

        return (
            registration.target == snapshot["target"]
            and registration.head == snapshot["head"]
            and registration.revision == snapshot["revision"]
            and registration.source_packet_sha256 == packet_ref["sha256"]
            and bool(registration.registered_tests)
            and all(
                row["finding_id"] in inventory for row in registration.registered_tests.values()
            )
        )
    except (OSError, ValueError, KeyError, TypeError, AttributeError, contracts.ContractError):
        return False


def collect_source_registration(backend, target, snapshot, prepared_inputs, original_operation):
    """Read-only registration for current source facts, independent of a future ACT id.

    original_operation is the real interpretation context (deadline/owner), not
    model JSON. It conveys no write authority and never enters the test identity.
    """
    tests, hints, refs, missing = {}, [], [], []
    packet_ref = prepared_inputs.get("packet_ref") or {}
    try:
        import closer_frontier_policy

        frontier = closer_frontier_policy.validate_frontier(
            getattr(backend, "finite_frontier", None)
        )
        if target not in frontier["targets"] or not frontier.get("owner"):
            raise SourceUnavailable("original_finite_source_owner_UNMET")
        if frontier["bindings"][target]["revision"] != snapshot.get("revision"):
            raise SourceUnavailable("original_source_frontier_binding_UNMET")
        deadline = original_operation.get("deadline")
        if (
            type(deadline) not in {int, float}
            or not math.isfinite(deadline)
            or deadline <= time.time()
        ):
            raise SourceUnavailable("original_source_collection_deadline_UNMET")
        backend.gh.deadline = min(backend.gh.deadline, deadline)
        endpoint_cache = {}
        original_gh = backend.gh

        class ReadOnlySource:
            def api(self, endpoint, *, method="GET"):
                if method != "GET" or time.time() >= deadline:
                    raise SourceUnavailable("original_source_read_deadline_UNMET")
                if endpoint not in endpoint_cache:
                    if len(endpoint_cache) >= 32:
                        raise SourceUnavailable("source_read_request_bound_UNMET")
                    endpoint_cache[endpoint] = original_gh.api(endpoint, method="GET")
                return endpoint_cache[endpoint]

            def read_text(self, endpoint):
                if time.time() >= deadline:
                    raise SourceUnavailable("original_source_read_deadline_UNMET")
                if endpoint not in endpoint_cache:
                    if len(endpoint_cache) >= 32:
                        raise SourceUnavailable("source_read_request_bound_UNMET")
                    endpoint_cache[endpoint] = original_gh.read_text(endpoint)
                return endpoint_cache[endpoint]

        gh = ReadOnlySource()
        packet, root = contracts.load_retained_packet(packet_ref)
        import closer_finite_reentry as custody

        snapshot_ref = prepared_inputs["snapshot_ref"]
        captured, captured_sha = custody._read(snapshot_ref["path"])
        if captured_sha != snapshot_ref.get("sha256") or any(
            captured.get(key) != snapshot.get(key)
            for key in ("target", "head", "revision", "sources", "verifier")
        ):
            raise SourceUnavailable("original_source_snapshot_identity_UNMET")
        if (
            snapshot.get("target") != target
            or packet.get("target") != target
            or snapshot.get("head") != packet.get("head")
            or not snapshot.get("revision")
            or str(root.resolve()) != str(Path(prepared_inputs["retained_dir"]).resolve())
            or packet != prepared_inputs.get("packet")
        ):
            raise SourceUnavailable("original_source_packet_identity_UNMET")
        import closer_dispatcher

        workspace = closer_dispatcher.selected_workspace(backend.workspaces, target)
        if not workspace:
            raise SourceUnavailable("explicit_source_workspace_UNMET")
        repo = snapshot.get("pr_target", target).split("#", 1)[0]
        for path in catalog.SOURCE_FILES:
            captured_file = _content(gh, repo, path, snapshot["head"], root, refs)
            if catalog._read_source(Path(workspace) / path) != captured_file:
                raise SourceUnavailable("source_policy_original_head_bytes_UNMET")
        policies, _ = catalog.compile_catalog(Path(workspace))
        metadata = _document(gh, f"repos/{repo}", root, refs)
        branch_name = metadata.get("default_branch")
        if metadata.get("full_name") != repo or not isinstance(branch_name, str) or not branch_name:
            raise SourceUnavailable("canonical_source_branch_UNMET")
        branch = _document(gh, f"repos/{repo}/branches/{quote(branch_name, safe='')}", root, refs)
        canonical_sha = (branch.get("commit") or {}).get("sha")
        if branch.get("name") != branch_name or not re.fullmatch(
            r"[0-9a-f]{40}", str(canonical_sha)
        ):
            raise SourceUnavailable("canonical_source_commit_UNMET")
        canonical_workflow = _content(gh, repo, WORKFLOW_PATH, canonical_sha, root, refs)
        ci = packet["sections"]["ci_artifacts"].get("content") or {}
        runs = ci.get("runs") or []
        if not isinstance(runs, list) or len(runs) > MAX_RUNS:
            raise SourceUnavailable("source_CI_run_inventory_bound_UNMET")
        repo = snapshot.get("pr_target", target).split("#", 1)[0]
        owners = contracts._owners(snapshot)
        if len(owners) != 1:
            raise SourceUnavailable("exact_original_source_owner_UNMET")
        # Failure identities come only from the retained native finding inventory.
        findings = disposition.pending_nonpass_findings(snapshot, packet)
        if len(findings) > 20:
            raise SourceUnavailable("source_finding_inventory_bound_UNMET")
        for finding in findings:
            raw_finding = finding.get("raw")
            if not isinstance(raw_finding, str):
                raw_finding = _json(raw_finding).decode()
            for policy in policies:
                for run_hint in runs:
                    if not isinstance(run_hint, dict) or type(run_hint.get("id")) is not int:
                        continue
                    run_id = run_hint["id"]
                    run = _document(gh, f"repos/{repo}/actions/runs/{run_id}", root, refs)
                    attempt = run.get("run_attempt")
                    if (
                        run.get("id") != run_id
                        or run.get("head_sha") != snapshot["head"]
                        or type(attempt) is not int
                        or attempt < 1
                        or run_hint.get("run_attempt") != attempt
                        or run.get("status") != "completed"
                        or run.get("conclusion") != "failure"
                        or run.get("path") != WORKFLOW_PATH
                        or (run.get("repository") or {}).get("full_name") != repo
                    ):
                        continue
                    caller = _content(gh, repo, WORKFLOW_PATH, snapshot["head"], root, refs)
                    if (
                        hashlib.sha256(caller).hexdigest()
                        != hashlib.sha256(canonical_workflow).hexdigest()
                    ):
                        raise SourceUnavailable("source_workflow_pin_UNMET")
                    callees = [
                        r
                        for r in run.get("referenced_workflows", [])
                        if isinstance(r, dict)
                        and r.get("path", "").startswith("stranske/Workflows/" + CALLEE_PATH + "@")
                        and re.fullmatch(r"[0-9a-f]{40}", str(r.get("sha", "")))
                    ]
                    if len(callees) != 1:
                        raise SourceUnavailable("source_reusable_workflow_identity_UNMET")
                    callee = _content(
                        gh, "stranske/Workflows", CALLEE_PATH, callees[0]["sha"], root, refs
                    )
                    if hashlib.sha256(callee).hexdigest() != WORKFLOWS_CI_SHA256:
                        raise SourceUnavailable("source_reusable_workflow_pin_UNMET")
                    jobs_doc = _document(
                        gh,
                        f"repos/{repo}/actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100",
                        root,
                        refs,
                    )
                    jobs = jobs_doc.get("jobs")
                    if (
                        not isinstance(jobs, list)
                        or jobs_doc.get("total_count") != len(jobs)
                        or len(jobs) > MAX_JOBS
                    ):
                        raise SourceUnavailable("complete_source_job_inventory_UNMET")
                    name = "lint-format" if policy.operation == "format" else "lint-ruff"
                    for job in jobs:
                        if (
                            job.get("name") not in {name, "python ci / " + name}
                            or job.get("conclusion") != "failure"
                            or job.get("status") != "completed"
                            or job.get("head_sha") != snapshot["head"]
                        ):
                            continue
                        check_url = job.get("check_run_url", "")
                        match = re.fullmatch(
                            r"https://api.github.com/repos/"
                            + re.escape(repo)
                            + r"/check-runs/([1-9][0-9]*)",
                            check_url,
                        )
                        if not match or type(job.get("id")) is not int:
                            continue
                        check = _document(gh, f"repos/{repo}/check-runs/{match[1]}", root, refs)
                        if (
                            check.get("id") != int(match[1])
                            or check.get("head_sha") != snapshot["head"]
                            or (check.get("app") or {}).get("slug") != "github-actions"
                            or check.get("conclusion") != "failure"
                            or check.get("status") != "completed"
                            or check.get("name") not in {name, "python ci / " + name}
                        ):
                            continue
                        log = gh.read_text(f"repos/{repo}/actions/jobs/{job['id']}/logs")
                        if not isinstance(log, str) or len(log.encode()) > MAX_LOG_BYTES:
                            raise SourceUnavailable("source_check_log_bound_UNMET")
                        log_ref = _retain(root, log.encode(), "txt")
                        refs.append(log_ref)
                        # Require exact original finding text to name the same diagnostic.
                        paths = sorted(set(re.findall(r"[A-Za-z0-9_./-]+\.py", raw_finding)))
                        if not paths:
                            # A generic native verdict has no mechanical path claim;
                            # leave that finding untouched and inspect later rows.
                            continue
                        diagnostics = parse_diagnostics(policy.operation, log, paths)
                        if not all(row["message"] in raw_finding for row in diagnostics):
                            continue
                        installed = _installation(policy)
                        missing.append(installed["missing"])
                        ci_finding = {
                            "schema": "closer-collected-tool-finding/v1",
                            "target": target,
                            "head": snapshot["head"],
                            "finding_id": finding["finding_id"],
                            "source_owner": owners[0],
                            "paths": paths,
                            "operation": policy.operation,
                            "check_name": "python ci / " + name,
                            "run_id": run_id,
                            "run_attempt": attempt,
                            "check_id": int(match[1]),
                            "source_receipt_sha256": log_ref["sha256"],
                            "collector_source_sha256": confinement._hash(Path(__file__).resolve()),
                        }
                        test_id = "source-tool-check:" + contracts._digest(ci_finding)
                        test = {
                            "schema": catalog.TOOL_CHECK_SCHEMA,
                            "test_id": test_id,
                            "target": target,
                            "head": snapshot["head"],
                            "finding_id": finding["finding_id"],
                            "kind": "tool_check",
                            "class_policy": policy.descriptor(),
                            "policy_sha256": catalog.digest(policy.descriptor()),
                            "executable": installed["executable"],
                            "executable_sha256": installed["executable_sha256"],
                            "ci_finding": ci_finding,
                        }
                        if test_id in tests and tests[test_id] != test:
                            raise SourceUnavailable("ambiguous_source_registration_UNMET")
                        tests[test_id] = test
                        hints.append(
                            {
                                "finding_id": finding["finding_id"],
                                "operation": policy.operation,
                                "paths": paths,
                                "source_owner": owners[0],
                                "required_test": {
                                    "test_id": test_id,
                                    "identity_sha256": contracts._digest(test),
                                },
                                "source_refs": [log_ref],
                                "qualification": "UNMET",
                            }
                        )
        if time.time() >= deadline:
            raise SourceUnavailable("original_source_collection_deadline_UNMET")
        if not tests:
            missing.append("authenticated_source_CI_finding_registration_UNMET")
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        AttributeError,
        importlib.metadata.PackageNotFoundError,
        RuntimeError,
    ) as exc:
        missing.append(
            str(exc)
            if isinstance(exc, SourceUnavailable)
            else f"source_registration_UNMET:{type(exc).__name__}"
        )
        tests, hints = {}, []
    missing.extend(
        [
            "independent_installed_confinement_qualification_UNMET",
            "independent_readonly_tool_check_qualification_UNMET",
        ]
    )
    return SourceRegistration(
        target,
        snapshot.get("head"),
        snapshot.get("revision"),
        packet_ref.get("sha256"),
        _json(packet_ref).decode(),
        str(Path(prepared_inputs.get("retained_dir", ".")).resolve()),
        _json(tests).decode(),
        _json(hints).decode(),
        _json(refs).decode(),
        tuple(dict.fromkeys(missing)),
        _issuer=_SOURCE_ISSUER,
    )


@dataclass(frozen=True)
class SourceHostCapability:
    """Private installed-source validator result, never model JSON or a boolean.

    No issuer is installed by this source: the OS diagnostic is unresolved.
    Future issuance must validate actual source/template/binary/read closure and
    confinement evidence. BoundaryPermits are minted only for a live repair op.
    """

    registration_sha256: str
    source_refs: tuple[tuple[str, str], ...]
    runtime_read_roots: tuple[str, ...] = ()
    qualify_boundary: Callable | None = None
    _issuer: object = field(default=None, repr=False, compare=False)


def source_host_capability(registration, snapshot, plan):
    """No positive installed source-host proof currently exists; NEVER infer one."""
    return None


def capability_matches(capability, registration, snapshot, plan):
    return (
        type(capability) is SourceHostCapability
        and capability._issuer is _HOST_ISSUER
        and type(registration) is SourceRegistration
        and registration._issuer is _SOURCE_ISSUER
        and capability.registration_sha256 == contracts._digest(registration.tests_json)
        and bool(capability.source_refs)
        and bool(capability.runtime_read_roots)
        and callable(capability.qualify_boundary)
        and registration.target == snapshot.get("target")
        and registration.head == snapshot.get("head")
        and plan.get("required_test", {}).get("test_id") in registration.registered_tests
    )


def original_preparation(backend, target, snapshot, operation):
    """Recover ONLY the exact live callback's linked original PREPARE, never latest."""
    import closer_finite_reentry as custody
    import closer_pipeline
    import closer_queue

    if (
        not closer_queue._currently_invoking(target, operation)
        or operation.get("action") != "repair"
    ):
        raise SourceUnavailable("original_live_repair_operation_UNMET")
    frontier = operation.get("finite_frontier")
    if (
        not isinstance(frontier, dict)
        or frontier != getattr(backend, "finite_frontier", None)
        or target not in frontier.get("targets", [])
        or not frontier.get("owner")
    ):
        raise SourceUnavailable("original_repair_frontier_owner_UNMET")
    inner = Path(backend.state_path).resolve(strict=True)
    if inner.name != closer_pipeline._digest(target) + ".json" or not inner.parent.name.endswith(
        ".actions"
    ):
        raise SourceUnavailable("original_inner_outer_queue_link_UNMET")
    outer = inner.parent.parent / (inner.parent.name[: -len(".actions")] + ".json")
    state, _ = custody._read(outer)
    entry = state.get("frontiers", {}).get(frontier["frontier_id"], {})
    row = entry.get("items", {}).get(target, {})
    prep = row.get("results", {}).get("prepare") or {}
    if (
        entry.get("finite_frontier") != frontier
        or row.get("revision") != snapshot.get("revision")
        or prep.get("revision") != snapshot.get("revision")
    ):
        raise SourceUnavailable("original_source_PREPARE_frontier_UNMET")
    retained_snapshot, digest = custody._read(prep["snapshot_ref"]["path"])
    packet, root = contracts.load_retained_packet(prep["packet_ref"])
    if digest != prep["snapshot_ref"].get("sha256") or any(
        retained_snapshot.get(k) != snapshot.get(k)
        for k in ("target", "head", "base", "revision", "sources", "verifier")
    ):
        raise SourceUnavailable("original_source_snapshot_custody_UNMET")
    return {
        "packet": packet,
        "packet_ref": prep["packet_ref"],
        "retained_dir": root,
        "snapshot_ref": prep["snapshot_ref"],
    }


def _tracked_at_head(backend, snapshot, paths, workspace, retained_dir):
    repo = snapshot.get("pr_target", snapshot["target"]).split("#", 1)[0]
    refs, tracked = [], {}
    for path in sorted(paths):
        raw = _content(backend.gh, repo, path, snapshot["head"], retained_dir, refs)
        local = catalog._read_source(Path(workspace) / path)
        if local != raw:
            raise SourceUnavailable("original_head_workspace_bytes_UNMET")
        tracked[path] = hashlib.sha256(raw).hexdigest()
    return tracked


def collect_postimage_check(
    request,
    writer_boundary,
    postimages,
    *,
    capability,
    registration,
    test,
    plan,
    operation,
    tracked,
    retained_dir,
):
    """Execute only an independently qualified READ-ONLY check; never reinterpret exit0.

    Current source_host_capability never issues a capability, so this cannot run
    on an unqualified host. The observed wrapper argv is retained verbatim.
    """
    import closer_mechanical_executor as engine
    import closer_queue

    if (
        type(capability) is not SourceHostCapability
        or capability._issuer is not _HOST_ISSUER
        or not closer_queue._currently_invoking(test["target"], operation)
    ):
        return None
    policy = catalog.validate_tool_check_identity(test, plan)
    root = Path(writer_boundary.workspace)
    remaining = operation["deadline"] - time.time()
    if remaining < 2:
        return None
    with tempfile.TemporaryDirectory(
        prefix="mechanical-readonly-check-", dir=Path(writer_boundary.scratch).parent
    ) as scratch:
        check = confinement.prepare_boundary(
            workspace=root,
            write_paths=[],
            scratch=Path(scratch).resolve(),
            runtime_read_roots=list(capability.runtime_read_roots),
            argv=[
                *policy.argv(test["executable"], root, check=True),
                *(str(root / path) for path in plan["paths"]),
            ],
            timeout_seconds=min(30, int(remaining)),
            max_output_bytes=65536,
            operation_id=operation["id"],
            read_only=True,
        )
        permit = capability.qualify_boundary(request, check)
        if (
            type(permit) is not engine.BoundaryPermit
            or permit.context_sha256 != request["context_sha256"]
            or permit.boundary_sha256 != check.descriptor_sha256
            or not engine._references(permit.evidence_refs)
        ):
            return None
        observed = confinement.run_confined_tool(
            check,
            starting_callback=lambda _row: _require_live_check(test["target"], operation),
            started_callback=lambda _row: _require_live_check(test["target"], operation),
            finished_callback=lambda _row: _require_live_check(test["target"], operation),
        )
        _require_live_check(test["target"], operation)
        process = observed.get("process_observation") or {}
        if (
            observed.get("mode") != "read_only"
            or observed.get("status") != "observed"
            or observed.get("returncode") != 0
            or process.get("cleanup_terminal") is not True
            or type(process.get("pid")) is not int
            or not process.get("os_started")
        ):
            return None
        stdout, stderr = observed.get("stdout"), observed.get("stderr")
        if (
            not isinstance(stdout, str)
            or not isinstance(stderr, str)
            or process.get("invalid_utf8_output")
        ):
            return None
        if policy.operation == "format":
            summary = re.findall(r"(?:^|\n)([1-9][0-9]*) files? would be left unchanged\.", stderr)
            if (
                stdout.strip()
                or len(summary) != 1
                or int(summary[0]) != len(plan["paths"])
                or "would reformat" in stderr
            ):
                return None
        elif stdout.strip() != "All checks passed!" or stderr.strip():
            return None
        # A separate check is identified by actual local OS facts, NEVER as a
        # GitHub/provider/model completion or an accounting run in those stores.
        run_id = "local_tool_check:" + contracts._digest(
            [operation["id"], check.descriptor_sha256, process["pid"], process["os_started"]]
        )
        process = {**process, "run_id": run_id, "context_sha256": request["context_sha256"]}
        row = {
            "schema": "closer-independent-tool-check/v1",
            "context_sha256": request["context_sha256"],
            "boundary_sha256": writer_boundary.descriptor_sha256,
            "check_boundary_sha256": check.descriptor_sha256,
            "operation_id": operation["id"],
            "required_test_sha256": contracts._digest(test),
            "target": test["target"],
            "head": test["head"],
            "finding_id": test["finding_id"],
            "ci_finding_sha256": contracts._digest(test["ci_finding"]),
            "source_policy_sha256": test["policy_sha256"],
            "collector_source_sha256": confinement._hash(Path(__file__).resolve()),
            "collector_id": "closer_mechanical_source.collect_postimage_check",
            "run_id": run_id,
            "process_observation": process,
            "tool": policy.tool,
            "tool_version": policy.version,
            "executable_sha256": test["executable_sha256"],
            "argv": list(check.argv),
            "source_refs": [list(ref) for ref in policy.source_refs],
            "preimages": [[path, tracked[path]] for path, _sha in postimages],
            "postimages": [list(ref) for ref in postimages],
            "outcome": "passed",
            "diagnostics": [],
        }
        ref = _retain(retained_dir, _json(row), "json")
        return engine.RegisteredToolCheckObservation(
            request["context_sha256"],
            writer_boundary.descriptor_sha256,
            contracts._digest(test),
            run_id,
            process,
            _json(row),
            row["collector_id"],
            ((ref["path"], ref["sha256"]),),
            check,
            permit,
        )


def _require_live_check(target, operation):
    import closer_queue

    if not closer_queue._currently_invoking(target, operation):
        raise SourceUnavailable("original_check_callback_or_deadline_UNMET")


def repair_from_source(backend, target, snapshot, operation, plan):
    """Real root adapter; fresh source facts never bypass the installed OS gate."""
    try:
        if not isinstance(plan, dict):
            raise SourceUnavailable("original_mechanical_plan_UNMET")
        inputs = original_preparation(backend, target, snapshot, operation)
        registration = collect_source_registration(backend, target, snapshot, inputs, operation)
        if not registration_matches(registration, snapshot, inputs["packet_ref"]):
            reason = (
                registration.missing[0] if registration.missing else "source_registration_UNMET"
            )
            raise SourceUnavailable(reason)
        selected = plan.get("required_test") or {}
        test = registration.registered_tests.get(selected.get("test_id"))
        if test is None or selected.get("identity_sha256") != contracts._digest(test):
            raise SourceUnavailable("original_plan_source_required_check_changed")
        capability = source_host_capability(registration, snapshot, plan)
        if not capability_matches(capability, registration, snapshot, plan):
            raise SourceUnavailable("independent_installed_source_host_qualification_UNMET")
        import closer_dispatcher
        import closer_mechanical_executor as engine

        workspace = closer_dispatcher.selected_workspace(backend.workspaces, target)
        if not workspace:
            raise SourceUnavailable("explicit_original_workspace_UNMET")
        policy = catalog.validate_tool_check_identity(test, plan)
        tracked = _tracked_at_head(
            backend,
            snapshot,
            {*plan["paths"], *catalog.SOURCE_FILES},
            workspace,
            inputs["retained_dir"],
        )
        # Recompile the actual tracked configuration, not an ambient caller's flags.
        if policy not in catalog.compile_catalog(Path(workspace))[0]:
            raise SourceUnavailable("original_tracked_source_policy_UNMET")
        tool = engine.ToolRegistration(
            selected["test_id"],
            target,
            snapshot["head"],
            plan["source_owner"],
            plan["finding_id"],
            plan["operation"],
            tuple(plan["paths"]),
            contracts._digest(test),
            plan["expected_failure"],
            policy.argv(test["executable"], Path(workspace).resolve()),
            test["executable_sha256"],
            capability.runtime_read_roots,
            30,
            65536,
        )

        def resolve(request):
            _require_live_check(target, operation)
            if (
                request.get("operation_id") != operation["id"]
                or request.get("finding_id") != plan["finding_id"]
            ):
                return None
            return engine.OwnerAdmission(tool, registration.registered_tests)

        owner = engine.OwnerCapability(
            resolve,
            capability.qualify_boundary,
            lambda request, boundary, postimages: collect_postimage_check(
                request,
                boundary,
                postimages,
                capability=capability,
                registration=registration,
                test=test,
                plan=plan,
                operation=operation,
                tracked=tracked,
                retained_dir=inputs["retained_dir"],
            ),
        )
        with tempfile.TemporaryDirectory(
            prefix="mechanical-original-", dir=Path(backend.state_path).parent
        ) as scratch:
            ticket = engine.prepare_mechanical_execution(
                plan,
                snapshot=snapshot,
                packet=inputs["packet"],
                packet_ref=inputs["packet_ref"],
                retained_dir=inputs["retained_dir"],
                workspace=workspace,
                tracked_paths=tracked,
                scratch=Path(scratch).resolve(),
                operation=operation,
                owner=owner,
            )
            result = engine.submit_mechanical(
                ticket,
                operation=operation,
                starting_callback=lambda _row: _require_live_check(target, operation),
                started_callback=lambda _row: _require_live_check(target, operation),
                finished_callback=lambda _row: _require_live_check(target, operation),
            )
            if result.get("status") == "not_started":
                return {"status": "not_started", "reason": result["reason"], "mechanical": result}
            proof = (
                engine.observe_required_test(ticket, operation=operation)
                if result.get("status") == "held_unknown"
                else None
            )
            return {
                "status": "uncertain",
                "reason": "original_mechanical_effect_requires_reconciliation",
                "outcome_uncertain": True,
                "retry_safe": False,
                "mechanical": result,
                "independent_tool_check": proof,
                "authority": False,
            }
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        result = contracts.deterministic_refusal(
            contracts.ContractError(str(exc), owner_required=True), snapshot
        )
        import closer_queue

        current = getattr(closer_queue._callback_threads, "invocation", {})
        submission = (
            current.get("_closer_mechanical_submitted") if isinstance(current, dict) else None
        )
        known_no_start = closer_queue._currently_invoking(target, operation) and (
            submission is None
            or (
                isinstance(submission, dict)
                and submission.get("operation_id") == operation.get("id")
                and submission.get("outcome") == "not_started"
            )
        )
        result["status"] = "not_started" if known_no_start else "uncertain"
        result["outcome_uncertain"] = not known_no_start
        result["retry_safe"] = False
        if known_no_start:
            result["process_started"] = False
        result["qualification"] = "UNMET"
        result["operation_id"] = operation.get("id")
        return result
