"""Validate an ordinary closer assessment against retained, current evidence.

The low-cost reviewer supplies the semantic mapping. This rail verifies its
identity, completeness, and cited raw proof; it never infers a PASS from a hash.
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

import closer_evidence


def _result(
    status: str, reason: str, *, disposition: dict | None = None, raw_findings: list | None = None
) -> dict:
    return {
        "status": status,
        "reason": reason,
        "disposition": disposition,
        "raw_findings": raw_findings or [],
    }


def _safe_raw(ref: Any, root: Path) -> bytes | None:
    if not isinstance(ref, dict) or not isinstance(ref.get("path"), str):
        return None
    if not isinstance(ref.get("sha256"), str) or not closer_evidence.SHA256.fullmatch(
        ref["sha256"]
    ):
        return None
    path = Path(ref["path"])
    if not path.is_absolute():
        return None
    try:
        if (root / "raw").is_symlink():
            return None
        raw_root = (root / "raw").resolve(strict=True)
        raw_root.relative_to(root.resolve(strict=True))
        path.resolve(strict=True).relative_to(raw_root)
        # A platform alias such as /tmp -> /private/tmp may precede the
        # evidence root. Reject links within the retained tree, including
        # links that point back inside it.
        for part in (path, *path.parents):
            if part.is_symlink():
                try:
                    part.parent.resolve(strict=True).relative_to(raw_root)
                except ValueError:
                    continue
                return None
        if not path.is_file() or path.stat().st_size > closer_evidence.MAX_SECTION_BYTES:
            return None
        data = path.read_bytes()
    except (OSError, ValueError):
        return None
    if len(data) != ref.get("bytes") or hashlib.sha256(data).hexdigest() != ref["sha256"]:
        return None
    return data


def _requirements(body: str) -> list[str]:
    # Keep the same original Tasks/AC grammar as the existing acceptance guard.
    import closer_dispatcher

    return closer_dispatcher.requirements(body)


def _expected(snapshot: Mapping[str, Any]) -> list[tuple[str, str]] | None:
    sources = snapshot.get("sources")
    if not isinstance(sources, list) or not sources:
        return None
    expected = []
    for source in sources:
        if (
            not isinstance(source, dict)
            or not isinstance(source.get("target"), str)
            or not isinstance(source.get("body"), str)
        ):
            return None
        criteria = _requirements(source["body"])
        if not criteria:
            return None
        expected.extend(
            [
                (source["target"], source["body"]),
                *((source["target"], criterion) for criterion in criteria),
            ]
        )
    return expected


def _receipt_index(packet: dict, root: Path) -> dict[tuple[str, str], dict]:
    """Index raw, typed, current-revision implementation and test proof."""
    index = {}
    diff = packet["sections"]["diff_files"].get("content") or {}
    if not isinstance(diff, dict):
        diff = {}
    if diff.get("base") == packet["base"] and diff.get("head") == packet["head"]:
        for file in diff.get("files", []) if isinstance(diff.get("files"), list) else []:
            if not isinstance(file, dict) or not closer_evidence.validate_patch_reference(
                file, base=packet["base"], head=packet["head"]
            ):
                continue
            binding = file.get("patch_binding") or {}
            if binding.get("filename") != file.get("filename") or binding.get(
                "blob_sha"
            ) != file.get("blob_sha"):
                continue
            ref = file.get("patch_retained")
            data = _safe_raw(ref, root)
            if (
                not isinstance(ref, dict)
                or not isinstance(ref.get("path"), str)
                or not isinstance(ref.get("sha256"), str)
                or data is None
                or not isinstance(file.get("patch"), str)
                or data != file["patch"].encode("utf-8")
                or hashlib.sha256(data).hexdigest() != file.get("patch_sha256")
            ):
                continue
            index[(ref["path"], ref["sha256"])] = {
                "kind": "codepatch",
                "filename": file.get("filename"),
                "blob_sha": file.get("blob_sha"),
                "base": packet["base"],
                "head": packet["head"],
            }
    section = packet["sections"]["ci_artifacts"]
    content = section.get("content") or {}
    if not isinstance(content, dict):
        content = {}
    runs = (
        {
            r["id"]: r
            for r in content.get("runs", [])
            if isinstance(r, dict) and isinstance(r.get("id"), int)
        }
        if isinstance(content.get("runs"), list)
        else {}
    )
    for artifact in (
        content.get("artifacts", []) if isinstance(content.get("artifacts"), list) else []
    ):
        if not isinstance(artifact, dict) or artifact.get("status") != "complete":
            continue
        if not isinstance(artifact.get("run_id"), int):
            continue
        run = runs.get(artifact.get("run_id"))
        if not isinstance(run, dict) or any(
            (
                run.get("run_attempt") != artifact.get("run_attempt"),
                run.get("head_sha") != packet["head"],
                artifact.get("head_sha") != packet["head"],
                run.get("status") != "completed",
                run.get("conclusion") != "success",
            )
        ):
            continue
        for receipt in (
            artifact.get("receipts", []) if isinstance(artifact.get("receipts"), list) else []
        ):
            if not isinstance(receipt, dict) or receipt.get("kind") not in {
                "junit",
                "test-summary",
                "coverage-summary",
            }:
                continue
            kind = receipt["kind"]
            if kind == "junit" and not (
                all(
                    type(receipt.get(k)) is int and receipt[k] >= 0
                    for k in ("tests", "failures", "errors", "skipped")
                )
                and receipt["tests"] > 0
                and receipt["failures"] == receipt["errors"] == 0
                and receipt["tests"] > receipt["failures"] + receipt["errors"] + receipt["skipped"]
            ):
                continue
            if kind == "test-summary" and not (
                all(
                    type(receipt.get(k)) is int and receipt[k] >= 0
                    for k in ("total", "passed", "failed", "skipped")
                )
                and receipt["total"] > 0
                and receipt["passed"] > 0
                and receipt["failed"] == 0
                and receipt["total"] == receipt["passed"] + receipt["failed"] + receipt["skipped"]
            ):
                continue
            if kind == "coverage-summary" and not (
                all(
                    type(receipt.get(k)) in {int, float} and math.isfinite(receipt[k])
                    for k in ("count", "covered")
                )
                and receipt["count"] > 0
                and 0 < receipt["covered"] <= receipt["count"]
                and (
                    receipt.get("percent_covered") is None
                    or (
                        type(receipt["percent_covered"]) in {int, float}
                        and math.isfinite(receipt["percent_covered"])
                        and 0 <= receipt["percent_covered"] <= 100
                    )
                )
            ):
                continue
            ref = receipt.get("raw")
            if (
                not isinstance(ref, dict)
                or not isinstance(ref.get("path"), str)
                or not isinstance(ref.get("sha256"), str)
                or _safe_raw(ref, root) is None
            ):
                continue
            index[(ref["path"], ref["sha256"])] = {
                "artifact_id": artifact.get("id"),
                "run_id": run.get("id"),
                "run_attempt": run.get("run_attempt"),
                "head_sha": packet["head"],
                "kind": receipt["kind"],
                "receipt_path": receipt.get("path"),
            }
    return index


def _native_resolution_index(packet: dict, root: Path, verifier: Mapping[str, Any]) -> dict:
    """Native input receipts can explain NON_PASS only; never prove acceptance."""
    section = packet["sections"].get("native_merge_verifier") or {}
    if not isinstance(section, dict):
        return {}
    content = section.get("content") or {}
    provenance = section.get("provenance") or {}
    if not isinstance(content, dict) or not isinstance(provenance, dict):
        return {}
    run = content.get("run") or {}
    manifest = content.get("input_manifest") or {}
    if not isinstance(run, dict) or not isinstance(manifest, dict):
        return {}
    if (
        section.get("status") not in {"complete", "partial"}
        or content.get("evidence_type") != "evaluated-merge-verifier-non-pass"
        or content.get("verifier") != verifier
        or provenance.get("acceptance_proof") is not False
        or provenance.get("head_sha") != packet["head"]
        or provenance.get("evaluated_sha") != verifier.get("evaluated_sha")
        or str(run.get("id")) != str(verifier.get("run_id"))
        or str(run.get("run_attempt")) != str(verifier.get("run_attempt"))
        or run.get("event") not in {"pull_request_target", "workflow_dispatch"}
        or run.get("path") != ".github/workflows/agents-verifier.yml"
        or run.get("status") != "completed"
        or manifest.get("merge_sha") != verifier.get("evaluated_sha")
    ):
        return {}
    ref = manifest.get("raw")
    data = _safe_raw(ref, root)
    if data is None:
        return {}
    try:
        raw_manifest = json.loads(data)
    except (ValueError, UnicodeDecodeError):
        return {}
    if (
        not isinstance(raw_manifest, dict)
        or any(
            raw_manifest.get(k) != manifest.get(k)
            for k in ("repository", "pr", "merge_sha", "source_run_id")
        )
        or str(raw_manifest.get("source_run_id")) != str(run.get("id"))
    ):
        return {}
    index = {}
    for artifact in (
        content.get("artifacts", []) if isinstance(content.get("artifacts"), list) else []
    ):
        if (
            not isinstance(artifact, dict)
            or artifact.get("status") != "complete"
            or str(artifact.get("run_id")) != str(run.get("id"))
            or str(artifact.get("run_attempt")) != str(run.get("run_attempt"))
            or artifact.get("evaluated_sha") != verifier.get("evaluated_sha")
        ):
            continue
        for container in (
            artifact.get("receipts", []) if isinstance(artifact.get("receipts"), list) else []
        ):
            if (
                not isinstance(container, dict)
                or container.get("kind") != "native-input-manifest"
                or container.get("raw") != ref
                or container.get("manifest") != raw_manifest
                or container.get("omissions")
            ):
                continue
            for receipt in artifact["receipts"]:
                if not isinstance(receipt, dict) or receipt.get("kind") not in {
                    "native-comparison",
                    "native-comparison-comment",
                }:
                    continue
                if (
                    str(receipt.get("run_id")) != str(run.get("id"))
                    or str(receipt.get("run_attempt")) != str(run.get("run_attempt"))
                    or receipt.get("evaluated_sha") != verifier.get("evaluated_sha")
                ):
                    continue
                if receipt["kind"] == "native-comparison" and not receipt.get("findings"):
                    continue
                proof_ref = receipt.get("raw")
                if (
                    not isinstance(proof_ref, dict)
                    or not isinstance(proof_ref.get("path"), str)
                    or not isinstance(proof_ref.get("sha256"), str)
                    or _safe_raw(proof_ref, root) is None
                ):
                    continue
                index[(proof_ref["path"], proof_ref["sha256"])] = {
                    "kind": receipt["kind"],
                    "artifact_id": artifact.get("id"),
                    "run_id": run.get("id"),
                    "run_attempt": run.get("run_attempt"),
                    "evaluated_sha": verifier.get("evaluated_sha"),
                    "head_sha": packet["head"],
                    "receipt_path": receipt.get("path", "comparison.json"),
                }
    return index


def _proofs_valid(refs: Any, proofs: Any, index: dict, root: Path) -> bool:
    if (
        not isinstance(refs, list)
        or not refs
        or not isinstance(proofs, list)
        or len(refs) != len(proofs)
    ):
        return False
    for ref, proof in zip(refs, proofs):
        if not isinstance(ref, dict) or not isinstance(proof, dict):
            return False
        if not isinstance(ref.get("path"), str) or not isinstance(ref.get("sha256"), str):
            return False
        member = index.get((ref.get("path"), ref.get("sha256")))
        data = _safe_raw(ref, root)
        excerpt, relevance = proof.get("excerpt"), proof.get("relevance")
        if (
            member is None
            or data is None
            or any(proof.get(k) != v for k, v in member.items())
            or proof.get("evidence_sha256") != ref.get("sha256")
            or not isinstance(excerpt, str)
            or len(excerpt.strip()) < 4
            or excerpt.encode() not in data
            or not isinstance(relevance, str)
            or len(relevance.strip()) < 12
        ):
            return False
    return True


def _finding_id(finding: dict) -> str:
    return hashlib.sha256(
        json.dumps(finding, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def pending_nonpass_findings(snapshot: Mapping[str, Any], packet: Mapping[str, Any]) -> list[dict]:
    """Expose immutable NON_PASS rows with stable IDs for the reviewing agent."""
    findings = []
    verifier = snapshot.get("verifier")
    if isinstance(verifier, dict) and verifier.get("verdict") not in {None, "PASS"}:
        findings.append(
            {"source": "native_verifier", "verdict": verifier.get("verdict"), "raw": verifier}
        )
    previous = ((packet.get("sections") or {}).get("prior_dispositions") or {}).get("content") or {}
    if isinstance(previous, dict) and isinstance(previous.get("criteria"), list):
        findings.extend(
            {"source": "prior_disposition", "raw": row}
            for row in previous.get("criteria", [])
            if isinstance(row, dict) and row.get("result") not in {None, "PASS"}
        )
    return [{"finding_id": _finding_id(f), "raw": f} for f in findings]


def _ordinary_nonpass(
    proposal: dict,
    findings: list[dict],
    index: dict,
    root: Path,
    packet: dict,
    snapshot: Mapping[str, Any],
) -> dict:
    raw = [{"finding_id": _finding_id(f), "raw": f} for f in findings]
    resolutions = proposal.get("resolutions")
    if (
        not isinstance(resolutions, list)
        or len(resolutions) != len(raw)
        or not all(isinstance(r, dict) for r in resolutions)
    ):
        return _result("wait", "nonpass_findings_not_accounted", raw_findings=raw)
    resolution_ids: list[str] = []
    for resolution in resolutions:
        finding_id = resolution.get("finding_id")
        if not isinstance(finding_id, str):
            return _result("wait", "nonpass_finding_identity_changed", raw_findings=raw)
        resolution_ids.append(finding_id)
    if sorted(resolution_ids) != sorted(str(f["finding_id"]) for f in raw):
        return _result("wait", "nonpass_finding_identity_changed", raw_findings=raw)
    repair_plan = None
    for resolution in resolutions:
        status = resolution.get("status")
        if status not in {
            "unresolved_missing_evidence",
            "unresolved_mechanical",
            "resolved_duplicate",
            "resolved_nonissue",
        }:
            return _result("exception", "nonpass_resolution_ambiguous", raw_findings=raw)
        action = resolution.get("next_guarded_action")
        if action not in {"wait", "collect_evidence", "repair"}:
            return _result("exception", "nonpass_action_outside_guard", raw_findings=raw)
        if status.startswith("resolved_") and not _proofs_valid(
            resolution.get("evidence"), resolution.get("proofs"), index, root
        ):
            return _result("wait", "nonpass_resolution_proof_missing", raw_findings=raw)
        if status.startswith("unresolved_") and not isinstance(resolution.get("reason"), str):
            return _result("wait", "nonpass_unresolved_reason_missing", raw_findings=raw)
        if action == "repair":
            plan = resolution.get("repair_plan")
            files = (packet["sections"]["diff_files"].get("content") or {}).get("files") or []
            changed_paths = {
                f.get("filename") for f in files if isinstance(f, dict) and f.get("patch_retained")
            }
            source_owners = {
                s.get("target") for s in snapshot.get("sources", []) if isinstance(s, dict)
            }
            paths = plan.get("paths") if isinstance(plan, dict) else None
            if (
                status != "unresolved_mechanical"
                or not isinstance(paths, list)
                or not 1 <= len(paths) <= 5
                or any(
                    not isinstance(p, str)
                    or not p
                    or p not in changed_paths
                    or PurePosixPath(p).is_absolute()
                    or ".." in PurePosixPath(p).parts
                    or any(part in {".git", ".codex", ".aws"} for part in PurePosixPath(p).parts)
                    for p in paths
                )
                or plan.get("head") != packet["head"]
                or plan.get("source_owner") not in source_owners
                or not isinstance(plan.get("prompt"), str)
                or not 1 <= len(plan["prompt"].strip()) <= 2000
                or not isinstance(plan.get("expected_failure"), str)
                or not 1 <= len(plan["expected_failure"].strip()) <= 500
                or not _proofs_valid(
                    resolution.get("evidence"), resolution.get("proofs"), index, root
                )
            ):
                return _result("wait", "mechanical_repair_plan_unbound", raw_findings=raw)
            if repair_plan is not None and repair_plan != plan:
                return _result("exception", "conflicting_mechanical_repair_plans", raw_findings=raw)
            repair_plan = plan
    disposition = {
        "head": packet["head"],
        "base": packet["base"],
        "review_revision": snapshot.get("review_revision"),
        "source_digest": __import__("closer_dispatcher").source_digest(snapshot["sources"]),
        "raw_verdicts": raw,
        "resolutions": resolutions,
        "next_guarded_actions": [r["next_guarded_action"] for r in resolutions],
        "repair": repair_plan,
        "acceptance_authority": False,
    }
    return _result(
        "ordinary_nonpass",
        "nonpass_findings_accounted_without_pass_authority",
        disposition=disposition,
        raw_findings=raw,
    )


def integrate_ordinary(
    target: str,
    snapshot: Mapping[str, Any],
    packet_ref: Mapping[str, Any],
    prep: Mapping[str, Any],
    interpretation: Mapping[str, Any],
) -> dict:
    """Return ``accepted`` with a guarded disposition, or ``wait``/``exception``.

    Expected model JSON: ``{target, head, base, classification, substantive_needs,
    findings, decision:{criteria:[{source, criterion, result:'PASS', evidence:[raw_ref],
    proofs:[{evidence_sha256, artifact_id, run_id, run_attempt, head_sha,
    receipt_path, kind, excerpt, relevance}]}]}}``. ``excerpt`` is an exact
    nonempty span of the raw receipt; ``relevance`` is the reviewer's stated
    semantic connection to that criterion, retained for later adjudication.
    """
    actual = interpretation.get("actual") if isinstance(interpretation, Mapping) else None
    proposal = interpretation.get("proposal") if isinstance(interpretation, Mapping) else None
    raw_findings = (
        list(proposal.get("findings", []))
        if isinstance(proposal, dict) and isinstance(proposal.get("findings"), list)
        else []
    )
    verifier = snapshot.get("verifier") if isinstance(snapshot, Mapping) else None
    if (
        not isinstance(actual, dict)
        or actual.get("exit") != 0
        or not isinstance(actual.get("run_id"), str)
        or not actual["run_id"]
    ):
        return _result("wait", "worker_execution_unverified", raw_findings=raw_findings)
    if not isinstance(proposal, dict):
        return _result("exception", "worker_json_invalid", raw_findings=raw_findings)
    if (
        not isinstance(proposal.get("substantive_needs"), list)
        or not isinstance(proposal.get("findings"), list)
        or any(not isinstance(f, dict) for f in proposal["findings"])
    ):
        return _result("exception", "worker_json_invalid", raw_findings=raw_findings)
    if proposal.get("classification") == "uncertain" or proposal.get("substantive_needs"):
        return _result("exception", "substantive_or_ambiguous_finding", raw_findings=raw_findings)
    if proposal.get("classification") not in {"relevant", "duplicate", "unrelated"}:
        return _result("exception", "classification_invalid", raw_findings=raw_findings)
    if proposal.get("classification") in {"unrelated"}:
        return _result("exception", "unrelated_evidence_or_finding", raw_findings=raw_findings)
    if any(
        isinstance(f, dict)
        and (
            any(
                str(f.get(k, "")).lower() in {"security", "critical", "high", "vulnerability"}
                for k in ("severity", "category", "type")
            )
        )
        for f in raw_findings
    ):
        return _result("exception", "high_risk_finding", raw_findings=raw_findings)
    if not isinstance(packet_ref, Mapping) or not isinstance(packet_ref.get("path"), str):
        return _result("wait", "packet_reference_missing", raw_findings=raw_findings)
    path = Path(packet_ref["path"])
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 8_000_000:
            raise ValueError("unsafe packet")
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != packet_ref.get("sha256"):
            raise ValueError("packet hash mismatch")
        packet = json.loads(raw)
    except (OSError, ValueError, TypeError):
        return _result("wait", "packet_missing_or_changed", raw_findings=raw_findings)
    if not closer_evidence.validate_packet(packet):
        return _result("wait", "packet_invalid", raw_findings=raw_findings)
    current_producer = hashlib.sha256(Path(closer_evidence.__file__).read_bytes()).hexdigest()
    if (
        packet["producer_sha256"] != current_producer
        or prep.get("producer_sha256") != current_producer
    ):
        return _result("wait", "producer_changed", raw_findings=raw_findings)
    if any(
        (
            packet.get("target") != target,
            snapshot.get("target") != target,
            packet.get("subject_pr") != snapshot.get("pr_target", target),
            packet.get("head") != snapshot.get("head"),
            packet.get("base") != snapshot.get("base"),
            proposal.get("target") != target,
            proposal.get("head") != packet.get("head"),
            proposal.get("base") != packet.get("base"),
            prep.get("revision") != snapshot.get("revision"),
        )
    ):
        return _result("wait", "target_or_revision_changed", raw_findings=raw_findings)
    bindings = packet["bindings"]
    if (
        bindings.get("source") != closer_evidence._hash(snapshot.get("sources"))
        or bindings.get("reviews") != closer_evidence._hash(snapshot.get("review_revision"))
        or bindings.get("identity")
        != closer_evidence._hash(
            {
                "target": target,
                "subject_pr": packet.get("subject_pr"),
                "head": packet["head"],
                "base": packet["base"],
            }
        )
        or (
            (prep.get("evidence_summary") or {}).get("section_digests") != packet["section_digests"]
        )
    ):
        return _result("wait", "evidence_revision_changed", raw_findings=raw_findings)
    previous = packet["sections"]["prior_dispositions"].get("content") or {}
    prior_findings = previous.get("raw_findings", []) if isinstance(previous, dict) else []
    if isinstance(prior_findings, list):
        raw_findings = prior_findings + raw_findings
    nonpass = [item["raw"] for item in pending_nonpass_findings(snapshot, packet)]
    nonpass += [
        f
        for f in raw_findings
        if isinstance(f, dict)
        and str(f.get("verdict", f.get("result", ""))).upper() in {"NON_PASS", "FAIL", "FAILED"}
    ]
    root = path.parent.parent
    index = _receipt_index(packet, root)
    if nonpass:
        if isinstance(verifier, dict):
            index = {**index, **_native_resolution_index(packet, root, verifier)}
        return _ordinary_nonpass(proposal, nonpass, index, root, packet, snapshot)
    expected = _expected(snapshot)
    decision = proposal.get("decision")
    rows = decision.get("criteria") if isinstance(decision, dict) else None
    if (
        expected is None
        or not isinstance(rows, list)
        or len(rows) != len(expected)
        or not all(isinstance(r, dict) for r in rows)
    ):
        return _result("wait", "source_coverage_incomplete", raw_findings=raw_findings)
    if any(
        not isinstance(r.get("source"), str) or not isinstance(r.get("criterion"), str)
        for r in rows
    ):
        return _result("wait", "source_coverage_incomplete", raw_findings=raw_findings)
    if sorted((r["source"], r["criterion"]) for r in rows) != sorted(expected):
        return _result("wait", "source_coverage_incomplete", raw_findings=raw_findings)
    if not index:
        return _result("wait", "current_head_raw_proof_missing", raw_findings=raw_findings)
    for row in rows:
        if row.get("result") != "PASS":
            return _result("exception", "nonpass_finding", raw_findings=raw_findings + [row])
        if not _proofs_valid(row.get("evidence"), row.get("proofs"), index, root):
            return _result(
                "wait", "criterion_proof_unbound_or_unrelated", raw_findings=raw_findings
            )
    disposition = {
        "reviewer": f"pipeline:{actual['run_id']}",
        "head": packet["head"],
        "review_revision": snapshot.get("review_revision"),
        "source_digest": __import__("closer_dispatcher").source_digest(snapshot["sources"]),
        "criteria": rows,
        "raw_findings": raw_findings,
        "evidence_packet": dict(packet_ref),
        "producer_sha256": current_producer,
    }
    return _result(
        "accepted", "ordinary_proof_validated", disposition=disposition, raw_findings=raw_findings
    )
