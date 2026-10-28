"""Prospective semantic advice bridge; retained source identity stays host-owned.

No actions, collection plans, tools or qualification are issued here. Each read
rebuilds the numbered catalog from the original physical packet and raw files.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import closer_disposition as disposition
import closer_execution_contracts as contracts

SCHEMA = "closer-semantic-assessment/v2"
PROOF_GAPS = frozenset(
    {"evidence_absent", "evidence_inconclusive", "observed_execution_required", "source_conflict"}
)


class AssessmentError(contracts.ContractError):
    pass


def _need(ok, reason):
    if not ok:
        raise AssessmentError(reason)


def _text(value, minimum=1, maximum=2000):
    return isinstance(value, str) and minimum <= len(value.strip()) <= maximum


def _members(index):
    for key in sorted(index):
        rows = index[key] if isinstance(index[key], list) else [index[key]]
        for row in sorted(rows, key=lambda value: json.dumps(value, sort_keys=True)):
            yield key, row


def catalog_digest(view):
    """Identity of supplied public content, never an admission/semantic permit."""
    return hashlib.sha256(
        json.dumps(
            view, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


def _ci_visible(member, ref, raw_text, projection):
    """Reuse the existing source-owned CI view; never move catalog numbers."""
    if member.get("kind") not in {"junit", "test-summary", "coverage-summary"}:
        return raw_text, True, None
    matched = [
        row
        for row in projection["receipts"]
        if isinstance(row.get("ref"), dict)
        and all(row["ref"].get(key) == ref[key] for key in ("path", "sha256", "bytes"))
        and row.get("receipt_kind") == member["kind"]
        and row.get("receipt_path") == member["receipt_path"]
        and all(
            row.get(key) == member[key]
            for key in ("artifact_id", "run_id", "run_attempt", "head_sha")
        )
    ]
    row = matched[0] if matched else {}
    supplied = row.get("content_supplied") is True
    text = row.get("content", "") if supplied else ""
    redacted = row.get("redaction_applied") is True
    citable = supplied and not redacted
    if citable and text == raw_text and not row.get("truncated"):
        return text, True, None  # Existing complete unredacted view is unchanged.
    visibility = {
        "schema": "closer-semantic-visible-catalog/v1",
        "source": projection["schema"],
        "limits": copy.deepcopy(projection["limits"]),
        "total_bytes": ref["bytes"],
        "supplied_bytes": len(text.encode("utf-8")),
        "raw_prefix_bytes": row.get("raw_prefix_bytes", 0),
        "content_supplied": supplied,
        "truncated": bool(row.get("truncated")) or not supplied,
        "redaction_applied": redacted,
        "citable": citable,
        "omission_reason": (
            "credential-redacted context only; this entry is not citable"
            if redacted
            else row.get("omission_reason", "receipt count bound; content not supplied to model")
        ),
    }
    return text, citable, visibility


def _bound_failed_ci(packet, root, verifier):
    """Recognize only retained typed failure at the exact native boundary.

    This is not an acceptance index or a claim about required-check topology.
    A different current-head run, or the verifier job's own success, supplies
    no explanation for the native decision's ci_failed flag.
    """
    evidence = disposition.closer_evidence
    section = packet["sections"]["ci_artifacts"]
    content = section.get("content") or {}
    if (
        section.get("status") not in {"complete", "partial", "truncated"}
        or section.get("provenance", {}).get("head") != verifier["evaluated_sha"]
        or not isinstance(content, dict)
        or not evidence._retained_section_payload_valid(section)
        or disposition._safe_raw(section.get("retained"), root) is None
    ):
        return False
    runs = content.get("runs", [])
    artifacts = content.get("artifacts", [])
    if not isinstance(runs, list) or not isinstance(artifacts, list):
        return False
    for run in runs:
        if not isinstance(run, dict) or not (
            str(run.get("id")) == str(verifier["run_id"])
            and str(run.get("run_attempt")) == str(verifier["run_attempt"])
            and run.get("head_sha") == verifier["evaluated_sha"]
            and run.get("status") == "completed"
            and run.get("conclusion") == "failure"
            and run.get("association_basis") in {"explicit_pull_requests", "unique_exact_pr_head"}
        ):
            continue
        for artifact in artifacts:
            if not isinstance(artifact, dict) or not (
                artifact.get("status") == "complete"
                and type(artifact.get("id")) is int
                and artifact["id"] > 0
                and artifact.get("run_id") == run.get("id")
                and artifact.get("run_attempt") == run.get("run_attempt")
                and artifact.get("head_sha") == run["head_sha"]
            ):
                continue
            receipts = artifact.get("receipts", [])
            for receipt in receipts if isinstance(receipts, list) else []:
                if not isinstance(receipt, dict):
                    continue
                data = (
                    disposition._safe_junit_raw(receipt.get("raw"), root)
                    if receipt.get("kind") == "junit"
                    else disposition._safe_raw(receipt.get("raw"), root)
                )
                if data is None:
                    continue
                if receipt.get("kind") == "junit":
                    parsed = evidence._junit_receipt(data)
                    failed = parsed is not None and parsed["failures"] + parsed["errors"] > 0
                elif receipt.get("kind") == "test-summary":
                    try:
                        parsed = evidence._recognized_json(json.loads(data))
                    except (ValueError, UnicodeError):
                        continue
                    failed = (
                        parsed is not None
                        and parsed.get("kind") == "test-summary"
                        and parsed["failed"] > 0
                    )
                else:
                    continue
                if failed and all(receipt.get(key) == value for key, value in parsed.items()):
                    return True
    return False


def host_residual_holds(snapshot, packet, packet_ref, retained_dir):
    """Conservative structured OTHER holds; absence never proves compatibility."""
    _identity, root = contracts._context(snapshot, packet, packet_ref, retained_dir)
    import verifier_evidence

    verifier = snapshot.get("verifier")
    subject = packet.get("subject_pr")
    if (
        not isinstance(subject, str)
        or "#" not in subject
        or subject != disposition.closer_evidence._subject_target(snapshot)
    ):
        return []
    repo, number = subject.rsplit("#", 1)
    if not number.isdigit() or not verifier_evidence.decision_matches_identity(
        verifier, repo, int(number), snapshot.get("head"), snapshot.get("merge")
    ):
        return []
    metadata = disposition._native_section_metadata(packet, verifier)
    if metadata is None or metadata[2].get("head_sha") != verifier["head_sha"]:
        return []
    section = metadata[0]
    if (
        not disposition.closer_evidence._retained_section_valid(section, allow_partial=True)
        or disposition._safe_raw(section.get("retained"), root) is None
    ):
        return []
    index = disposition._native_resolution_index(packet, root, verifier)
    # The existing retained native manifest/index validates the source boundary;
    # presentation rows or prose cannot mint a comparison verdict.
    holds = []
    if verifier.get("provider_verdicts") == ["FAIL"]:
        for (path, sha), member in index.items():
            if member.get("kind") != "native-comparison":
                continue
            data = disposition._safe_raw(
                {"path": path, "sha256": sha, "bytes": Path(path).stat().st_size}, root
            )
            try:
                report = json.loads(data) if data is not None else None
            except (ValueError, UnicodeError):
                continue
            results = report.get("results") if isinstance(report, dict) else None
            if (
                isinstance(results, list)
                and len(results) == 1
                and isinstance(results[0], dict)
                and results[0].get("verdict") == "CONCERNS"
            ):
                holds.append("native_provider_FAIL_CONCERNS_mismatch")
                break
    if verifier.get("ci_failed") is True and not _bound_failed_ci(packet, root, verifier):
        holds.append("native_ci_failed_without_same_boundary_failed_receipt")
    return holds


def _merge_host_residuals(state, holds):
    """Preserve independent host holds regardless of the model's omissions."""
    if not holds:
        return state
    return {
        **state,
        "classification": "uncertain",
        "substantive_needs": list(
            dict.fromkeys([*state["substantive_needs"], *("host_OTHER:" + x for x in holds)])
        ),
        "semantic_readiness": "held",
    }


def _build(snapshot, packet, packet_ref, retained_dir):
    identity, root = contracts._context(snapshot, packet, packet_ref, retained_dir)
    expected = disposition._expected(snapshot)
    _need(expected is not None, "original_obligation_inventory_unavailable")
    findings = disposition.pending_nonpass_findings(snapshot, packet)
    # Match integrate_ordinary's complete NON_PASS union, including retained
    # raw findings as well as prior criteria/current native rows. Keep occurrence
    # multiplicity; equal IDs with different original content are ambiguous.
    previous = packet["sections"]["prior_dispositions"].get("content") or {}
    previous_raw = previous.get("raw_findings", []) if isinstance(previous, dict) else []
    if isinstance(previous_raw, list):
        findings += [
            {"finding_id": disposition._finding_id(row), "raw": copy.deepcopy(row)}
            for row in previous_raw
            if isinstance(row, dict)
            and str(row.get("verdict", row.get("result", ""))).upper()
            in {"NON_PASS", "FAIL", "FAILED"}
        ]
    originals = {}
    for finding in findings:
        key = finding["finding_id"]
        _need(
            key not in originals or originals[key] == finding["raw"],
            "original_finding_identity_collision",
        )
        originals[key] = copy.deepcopy(finding["raw"])
    # Catalog numbers must never slide onto another proof when an original raw
    # file disappears or changes. Validate physical retained refs before the
    # existing index can omit an invalid member and renumber its successors.
    pending = [packet]
    visited = 0
    while pending:
        visited += 1
        _need(visited <= 100000, "catalog_packet_inventory_exceeds_bound")
        value = pending.pop()
        if isinstance(value, dict):
            if (
                {"path", "sha256", "bytes"} <= set(value)
                and isinstance(value.get("path"), str)
                and Path(value["path"]).is_absolute()
            ):
                _need(
                    disposition._safe_raw(
                        value, root, max_bytes=disposition.closer_evidence.MAX_UNCOMPRESSED_BYTES
                    )
                    is not None,
                    "catalog_raw_changed",
                )
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)
    ordinary = disposition._receipt_index(packet, root)
    verifier = snapshot.get("verifier")
    native = (
        disposition._native_resolution_index(packet, root, verifier)
        if isinstance(verifier, dict)
        else {}
    )
    # Source-known NON_PASS defines a nonaccepting accounting question; a
    # missing native resolution receipt is a proof gap, not a demand to assess
    # every previously unassessed acceptance obligation. This mode grants no
    # authority and cannot resolve a finding without its own valid proof.
    known_nonpass = bool(findings)
    mode = "known_NONPASS_accounting" if known_nonpass else "ordinary_acceptance"
    required = [] if known_nonpass else list(range(1, len(expected) + 1))
    # Same physical/member/total/count/redaction policy as the original model
    # context. Complete safe-JUnit/index admission above remains unchanged.
    from closer_dispatcher import _data_only_ci_receipt_inputs

    ci_projection = _data_only_ci_receipt_inputs(
        packet["sections"]["ci_artifacts"], retained_dir=root, max_bytes=32 * 1024
    )
    entries = []
    # Keep namespaces distinct even if the same raw reference has two meanings.
    for purpose, index in (("ordinary", ordinary), ("native_resolution", native)):
        for (path, sha), member in _members(index):
            ref = {"path": path, "sha256": sha, "bytes": Path(path).stat().st_size}
            reader = (
                disposition._safe_junit_raw
                if member.get("kind") == "junit"
                else disposition._safe_raw
            )
            raw = reader(ref, root)
            _need(raw is not None, "catalog_raw_changed")
            try:
                content = raw.decode("utf-8")
            except UnicodeError as error:
                raise AssessmentError("catalog_raw_not_utf8") from error
            visible, citable, visibility = _ci_visible(member, ref, content, ci_projection)
            entries.append(
                {
                    "ref": ref,
                    "member": copy.deepcopy(member),
                    "purpose": purpose,
                    "content": visible,
                    "citable": citable,
                    "visibility": visibility,
                }
            )
    view = {
        "mode": mode,
        "host_residual_holds": host_residual_holds(snapshot, packet, packet_ref, root),
        "required_obligation_slots": required,
        "required_finding_slots": list(range(1, len(findings) + 1)),
        "obligations": [
            {"slot": n, "source": source, "text": text}
            for n, (source, text) in enumerate(expected, 1)
        ],
        "findings": [
            {
                "slot": n,
                "raw": copy.deepcopy(row["raw"]),
                "proof_scope": "ordinary_or_native_resolution",
            }
            for n, row in enumerate(findings, 1)
        ],
        "catalog": [
            {
                "ref": n,
                "kind": row["member"]["kind"],
                "purpose": row["purpose"],
                "context": {
                    key: row["member"][key]
                    for key in ("filename", "receipt_path")
                    if key in row["member"]
                },
                "content": row["content"],
                **({"visibility": copy.deepcopy(row["visibility"])} if row["visibility"] else {}),
            }
            for n, row in enumerate(entries, 1)
        ],
    }
    return identity, root, expected, findings, entries, view


def catalog_input(snapshot, packet, packet_ref, retained_dir):
    """Return visible source inventory/content; never expected semantic answers."""
    return _build(snapshot, packet, packet_ref, retained_dir)[-1]


def _citations(values, entries, root, *, required=False, purpose=None):
    _need(
        isinstance(values, list) and (1 if required else 0) <= len(values) <= 8, "citations_invalid"
    )
    selected, refs, proofs, seen = [], [], [], set()
    for value in values:
        _need(
            isinstance(value, dict) and set(value) == {"ref", "excerpt", "relevance"},
            "citation_keys_invalid",
        )
        number = value["ref"]
        _need(
            type(number) is int and 1 <= number <= len(entries) and number not in seen,
            "citation_ref_invalid",
        )
        seen.add(number)
        row = entries[number - 1]
        _need(purpose is None or row["purpose"] == purpose, "citation_purpose_invalid")
        _need(
            _text(value["excerpt"], 4, 2_000_000) and _text(value["relevance"], 12),
            "citation_semantics_missing",
        )
        _need(
            row["citable"] and value["excerpt"] in row["content"],
            "citation_outside_supplied_visible_content",
        )
        ref = copy.deepcopy(row["ref"])
        proof = {
            **copy.deepcopy(row["member"]),
            "evidence_sha256": ref["sha256"],
            "excerpt": value["excerpt"],
            "relevance": value["relevance"],
        }
        # Exactly the selected member, never a search for another supporting proof.
        index = {(ref["path"], ref["sha256"]): row["member"]}
        _need(disposition._proofs_valid([ref], [proof], index, root), "citation_proof_unbound")
        _need(ref not in refs, "citation_same_physical_ref_repeated")
        refs.append(ref)
        proofs.append(proof)
        selected.append(copy.deepcopy(value))
    return refs, proofs


def _rows(values, count, statuses, entries, root, *, required=None):
    required = set(range(1, count + 1)) if required is None else set(required)
    _need(isinstance(values, list) and len(values) <= count, "coverage_incomplete")
    mapped = {}
    for row in values:
        _need(isinstance(row, dict), "assessment_row_invalid")
        status = row.get("status")
        keys = {"slot", "meaning", "status", "citations", "explanation"}
        if status == "unknown":
            keys.add("gap")
        _need(set(row) == keys, "assessment_row_keys_invalid")
        slot = row["slot"]
        _need(
            type(slot) is int and 1 <= slot <= count and slot not in mapped,
            "assessment_slot_invalid",
        )
        _need(type(status) is str and status in statuses, "assessment_status_invalid")
        _need(
            row["meaning"] in ("understood", "unknown") and _text(row["explanation"]),
            "assessment_meaning_invalid",
        )
        if status == "unknown":
            _need(
                (row["meaning"] == "unknown" and row["gap"] == "obligation_ambiguous")
                or (
                    row["meaning"] == "understood"
                    and isinstance(row["gap"], str)
                    and row["gap"] in PROOF_GAPS
                ),
                "assessment_gap_invalid",
            )
        else:
            _need(row["meaning"] == "understood", "known_outcome_requires_understood_meaning")
        refs, proofs = _citations(row["citations"], entries, root, required=status != "unknown")
        mapped[slot] = (row, refs, proofs)
    _need(required <= set(mapped), "coverage_incomplete")
    return mapped


def _assessment_state(response, findings, *, required_obligation_slots):
    """Derived facts only; semantic judgments come from authenticated role rows."""
    rows = response["obligations"] + response["findings"]
    meaning_unknown = any(row["meaning"] == "unknown" for row in rows)
    incomplete = any(row["status"] == "unknown" for row in rows)
    unmet = any(row["status"] in {"unsatisfied", "confirmed"} for row in rows)
    concerns = response["global_concerns"]
    needs = ["semantic_meaning_unknown"] if meaning_unknown else []
    if (
        any(
            row["status"] == "unknown" and row["slot"] in required_obligation_slots
            for row in response["obligations"]
        )
        and not meaning_unknown
    ):
        needs.append("semantic_acceptance_proof_gap")
    if concerns:
        needs.append("semantic_global_concern")
    if any(row["status"] == "confirmed" for row in response["findings"]):
        needs.append("known_unmet_finding_requires_independently_admitted_source_route")
    if contracts.owner_review_required(findings):
        needs.append("original_finding_requires_qualified_owner")
    return {
        "classification": "uncertain" if meaning_unknown else "relevant",
        "substantive_needs": needs,
        "semantic_summary": {
            "meaning_unknown": meaning_unknown,
            "proof_incomplete": incomplete,
            "known_unmet": unmet,
            "global_concern": bool(concerns),
        },
        "semantic_readiness": (
            "held"
            if concerns
            else (
                "knowledge_incomplete"
                if incomplete
                else (
                    "known_unmet"
                    if unmet
                    else "nonaccepting_accounting" if findings else "acceptance_candidate_only"
                )
            )
        ),
    }


def materialize(
    response, *, snapshot, packet, packet_ref, retained_dir, expected_catalog_sha256=None
):
    """Validate model semantics and attach only original selected host metadata."""
    _need(
        isinstance(response, dict)
        and set(response) == {"schema", "obligations", "findings", "global_concerns"}
        and response.get("schema") == SCHEMA,
        "assessment_schema_invalid",
    )
    _need(
        len(json.dumps(response, ensure_ascii=False).encode()) <= contracts.MAX_BYTES,
        "assessment_exceeds_bound",
    )
    identity, root, expected, findings, entries, _view = _build(
        snapshot, packet, packet_ref, retained_dir
    )
    _need(
        expected_catalog_sha256 is None or expected_catalog_sha256 == catalog_digest(_view),
        "supplied_catalog_view_changed",
    )
    obligations = _rows(
        response["obligations"],
        len(expected),
        {"satisfied", "unsatisfied", "unknown"},
        entries,
        root,
        required=_view["required_obligation_slots"],
    )
    meanings = _rows(
        response["findings"],
        len(findings),
        {"confirmed", "duplicate", "nonissue", "unknown"},
        entries,
        root,
    )
    concerns = response["global_concerns"]
    _need(isinstance(concerns, list) and len(concerns) <= 20, "global_concerns_invalid")
    for concern in concerns:
        _need(
            isinstance(concern, dict) and set(concern) == {"kind", "explanation", "citations"},
            "concern_keys_invalid",
        )
        _need(
            concern["kind"] in ("security", "authority_conflict", "ownership", "ambiguity")
            and _text(concern["explanation"]),
            "concern_invalid",
        )
        _citations(concern["citations"], entries, root)
    state = _merge_host_residuals(
        _assessment_state(
            response, findings, required_obligation_slots=_view["required_obligation_slots"]
        ),
        _view["host_residual_holds"],
    )
    criteria = []
    for slot, (source, text) in enumerate(expected, 1):
        if slot not in obligations:
            continue  # Host-only UNASSESSED, never an observed semantic result.
        row, refs, proofs = obligations[slot]
        # A native input record is not acceptance proof even if semantically cited.
        if row["status"] == "satisfied":
            _citations(row["citations"], entries, root, required=True, purpose="ordinary")
        criteria.append(
            {
                "source": source,
                "criterion": text,
                "result": (
                    "PASS"
                    if row["status"] == "satisfied"
                    else (
                        "UNKNOWN"
                        if row["status"] == "unknown"
                        and slot not in _view["required_obligation_slots"]
                        else "FAIL"
                    )
                ),
                "evidence": refs,
                "proofs": proofs,
            }
        )
    resolutions = []
    for slot, original in enumerate(findings, 1):
        row, refs, proofs = meanings[slot]
        if row["status"] in {"duplicate", "nonissue"}:
            refs, proofs = _citations(row["citations"], entries, root, required=True)
            # Match the unchanged ordinary NON_PASS host index exactly. Native
            # input proofs are resolution-only; ordinary code/CI can also support
            # a finding when the reviewer supplies its semantic connection.
            resolution_index = disposition._receipt_index(packet, root)
            verifier = snapshot.get("verifier")
            if isinstance(verifier, dict):
                resolution_index = {
                    **resolution_index,
                    **disposition._native_resolution_index(packet, root, verifier),
                }
            _need(
                disposition._proofs_valid(refs, proofs, resolution_index, root),
                "finding_resolution_proof_unbound",
            )
            resolutions.append(
                {
                    "finding_id": original["finding_id"],
                    "status": "resolved_" + row["status"],
                    "next_guarded_action": "wait",
                    "evidence": refs,
                    "proofs": proofs,
                }
            )
        elif row["status"] == "unknown" and row["meaning"] == "understood":
            resolutions.append(
                {
                    "finding_id": original["finding_id"],
                    "status": "unresolved_missing_evidence",
                    "next_guarded_action": "wait",
                    "reason": row["explanation"],
                }
            )
    # Recheck physical context/raw catalog after all semantic validation.
    _need(
        _build(snapshot, packet, packet_ref, retained_dir)[-1] == _view,
        "assessment_context_changed",
    )
    return {
        "target": identity["target"],
        "head": identity["head"],
        "base": identity["base"],
        **state,
        "findings": [],
        "resolutions": resolutions,
        "semantic_original_findings": copy.deepcopy(findings),
        "semantic_mode": _view["mode"],
        "host_residual_holds": copy.deepcopy(_view["host_residual_holds"]),
        "unassessed_obligation_slots": [
            n for n in range(1, len(expected) + 1) if n not in obligations
        ],
        "decision": {"criteria": criteria},
        "semantic_assessment": copy.deepcopy(response),
        "authority": False,
    }


def adjudication_candidate(
    response, *, snapshot, packet, packet_ref, retained_dir, expected_catalog_sha256=None
):
    """Host-select at most one original dispute; this does not admit a worker.

    Actual EXCEPTION role, callback fence and resource admission stay at the
    caller. Empty evidence is a collection dependency, never a free STRONG call.
    """
    original = materialize(
        response,
        snapshot=snapshot,
        packet=packet,
        packet_ref=packet_ref,
        retained_dir=retained_dir,
        expected_catalog_sha256=expected_catalog_sha256,
    )
    _identity, _root, expected, findings, _entries, _view = _build(
        snapshot, packet, packet_ref, retained_dir
    )
    _need(
        expected_catalog_sha256 is None or catalog_digest(_view) == expected_catalog_sha256,
        "supplied_catalog_view_changed",
    )
    if contracts.owner_review_required([original, findings]) or any(
        concern["kind"] in {"security", "authority_conflict", "ownership"}
        for concern in response["global_concerns"]
    ):
        return original, {
            "status": "pending",
            "reason": "qualified_source_owner_required",
            "authority": False,
        }
    candidates = []
    for section, rows in (
        ("obligations", response["obligations"]),
        ("findings", response["findings"]),
    ):
        required = set(
            _view["required_obligation_slots"]
            if section == "obligations"
            else _view["required_finding_slots"]
        )
        for row in sorted(rows, key=lambda item: item["slot"]):
            if row["slot"] not in required or row["status"] != "unknown":
                continue
            slot = row["slot"]
            if section == "obligations":
                source, criterion = expected[slot - 1]
                basis = {"source": source, "criterion": criterion}
                selected = original["decision"]["criteria"]
                matches = [
                    item
                    for item in selected
                    if item["source"] == source and item["criterion"] == criterion
                ]
                question = "Does the supplied evidence satisfy the named original criterion?"
            else:
                basis = {"finding_id": findings[slot - 1]["finding_id"]}
                matches = [
                    item
                    for item in original["resolutions"]
                    if item["finding_id"] == basis["finding_id"]
                ]
                question = "Does the supplied evidence resolve the named original finding?"
            candidates.append((section, slot, basis, matches, question))
    if not candidates:
        return original, {"status": "not_needed", "authority": False}
    # First original slot only. Neither output order nor a failed request can
    # cause a second selection/call within this callback.
    section, slot, basis, matches, question = candidates[0]
    pending = {"status": "pending", "section": section, "slot": slot, "authority": False}
    typed_row = next(item for item in response[section] if item["slot"] == slot)
    if typed_row["gap"] in {"evidence_absent", "observed_execution_required"}:
        # Valid code/context refs do not manufacture missing observed execution
        # or provenance. The host collector still needs its own existing exact
        # rail/locator; no second slot is selected as a substitute here.
        return original, {
            **pending,
            "reason": "semantic_gap_requires_existing_collection_selector",
            "gap": typed_row["gap"],
            "basis": copy.deepcopy(basis),
            "binding": copy.deepcopy(_identity),
        }
    if section == "findings":
        occurrences = [item for item in findings if item["finding_id"] == basis["finding_id"]]
        if len(occurrences) != 1:
            return original, {**pending, "reason": "original_dispute_basis_missing_or_ambiguous"}
        selected_row = next(item for item in response["findings"] if item["slot"] == slot)
        if selected_row["meaning"] == "unknown" and not matches:
            try:
                # Only the genuine current, unique source finding can name this
                # question. Priorraw-only/foreign/risk bases remain refused.
                contracts._basis(basis, snapshot, packet)
            except contracts.ContractError as error:
                return original, {**pending, "reason": error.reason}
            refs, proofs = _citations(selected_row["citations"], _entries, _root)
            # Question scaffolding ONLY. This is deliberately outside the old
            # accepted resolution enum; LOW meaning/global needs stay unknown.
            placeholder = {
                "finding_id": basis["finding_id"],
                "status": "pending_semantic_adjudication",
                "next_guarded_action": "wait",
                "reason": selected_row["explanation"],
                "evidence": refs,
                "proofs": proofs,
            }
            original["resolutions"].append(placeholder)
            matches = [placeholder]
    if len(matches) != 1:
        return original, {**pending, "reason": "original_dispute_basis_missing_or_ambiguous"}
    if section == "findings" and not matches[0].get("evidence"):
        refs, proofs = _citations(typed_row["citations"], _entries, _root)
        matches[0]["evidence"], matches[0]["proofs"] = refs, proofs
    refs = matches[0].get("evidence", [])
    if not refs:
        return original, {**pending, "reason": "exact_retained_dispute_evidence_required"}
    try:
        request = contracts.build_adjudication_request(
            snapshot,
            packet,
            packet_ref,
            basis=basis,
            question=question,
            evidence_refs=refs,
            retained_dir=retained_dir,
        )
        contracts.build_adjudication_prompt(original, request, retained_dir=retained_dir)
    except contracts.ContractError as error:
        return original, {**pending, "reason": error.reason}
    original["adjudication_request"] = {
        "basis": basis,
        "question": question,
        "evidence_refs": copy.deepcopy(refs),
    }
    return original, {
        "status": "candidate",
        "section": section,
        "slot": slot,
        "request": request,
        "authority": False,
    }


def reconcile_selected(
    response,
    request,
    answer,
    *,
    snapshot,
    packet,
    packet_ref,
    retained_dir,
    strong_actual,
    expected_catalog_sha256,
):
    """Authenticate role-specific STRONG advice; never rewrite original LOW.

    LOW view is protected separately. STRONG may see more of a selected raw ref
    through its unchanged source builder, but can cite ONLY its own supplied
    per-ref content. Derived evidence is STRONG provenance, not LOW observation.
    """
    import closer_data_only_execution as execution
    import closer_dispatcher as closer
    import closer_model_contracts
    import dispatcher

    _need(isinstance(expected_catalog_sha256, str), "protected_low_catalog_view_required")
    original, candidate = adjudication_candidate(
        response,
        snapshot=snapshot,
        packet=packet,
        packet_ref=packet_ref,
        retained_dir=retained_dir,
        expected_catalog_sha256=expected_catalog_sha256,
    )
    _need(
        candidate.get("status") == "candidate" and candidate["request"] == request,
        "original_scoped_candidate_changed",
    )
    identity, root, _expected, findings, entries, view = _build(
        snapshot, packet, packet_ref, retained_dir
    )
    _need(catalog_digest(view) == expected_catalog_sha256, "supplied_catalog_view_changed")
    _need(isinstance(strong_actual, dict), "actual_scoped_receipt_required")
    proof = strong_actual.get("_closer_controlled_proof")
    _need(
        isinstance(proof, dict)
        and proof.get("expected", {}).get("task_type") == "closer_exception",
        "actual_scoped_receipt_required",
    )
    try:
        proved = closer._revalidate_controlled_proof(
            proof,
            run_id=strong_actual.get("run_id"),
            basis={**identity, "request_id": request["request_id"]},
            receipt_root=dispatcher.DISPATCH_LOG_DIR,
        )
    except (OSError, ValueError, TypeError, KeyError) as error:
        raise contracts.ContractError("actual_scoped_receipt_unavailable") from error
    agent = proof["expected"]["profile_identity"]["agent"]
    _need(
        closer._worker_proposal(proved, agent=agent) == answer,
        "scoped_answer_differs_from_actual_receipt",
    )
    prompt = contracts.build_adjudication_prompt(original, request, retained_dir=root)
    _need(
        proof["expected"]["prompt"]
        == execution.validate_payload(prompt, limits=proof["expected"]["limits"], kind="prompt"),
        "actual_scoped_prompt_differs_from_source_builder",
    )
    supplied = closer_model_contracts.strict_response_json(prompt.split("\n", 1)[1])[
        "supplied_evidence"
    ]
    validated = contracts.validate_adjudication_response(
        request,
        answer,
        snapshot=snapshot,
        packet=packet,
        packet_ref=packet_ref,
        retained_dir=retained_dir,
    )
    for ref, value in zip(validated["evidence"], validated["proofs"]):
        visible = [item for item in supplied if item["ref"] == ref]
        _need(
            len(visible) == 1 and value["excerpt"] in visible[0]["content"],
            "scoped_citation_outside_actual_supplied_content",
        )
    # Unchanged canonical selector/proof guard remains the only merge primitive.
    merged = contracts.merge_selected_response(original, request, validated)
    if validated["outcome"] == "unresolved_mechanical":
        raise contracts.ContractError(
            "scoped_mechanical_outcome_requires_independent_registered_plan", owner_required=True
        )
    working = copy.deepcopy(response)
    row = next(item for item in working[candidate["section"]] if item["slot"] == candidate["slot"])
    row["explanation"] = validated["reason"]
    outcomes = {
        "satisfied": "satisfied",
        "unsatisfied": "unsatisfied",
        "resolved_duplicate": "duplicate",
        "resolved_nonissue": "nonissue",
    }
    if validated["outcome"] in outcomes:
        row["status"], row["meaning"] = outcomes[validated["outcome"]], "understood"
        row.pop("gap", None)
    # Insufficient advice leaves original meaning/gap unchanged, not fabricated
    # evidence_absent or confirmed. Only the actual STRONG reason is appended.
    row["citations"] = []
    for ref, value in zip(validated["evidence"], validated["proofs"]):
        matches = [
            n
            for n, entry in enumerate(entries, 1)
            if entry["ref"] == ref
            and all(value.get(key) == member for key, member in entry["member"].items())
        ]
        _need(len(matches) == 1, "scoped_catalog_member_missing_or_ambiguous")
        row["citations"].append(
            {"ref": matches[0], "excerpt": value["excerpt"], "relevance": value["relevance"]}
        )
    old_state = _assessment_state(
        response, findings, required_obligation_slots=view["required_obligation_slots"]
    )
    state = _assessment_state(
        working, findings, required_obligation_slots=view["required_obligation_slots"]
    )
    independent = [
        need for need in original["substantive_needs"] if need not in old_state["substantive_needs"]
    ]
    merged.update(_merge_host_residuals(state, view["host_residual_holds"]))
    merged["substantive_needs"] = list(dict.fromkeys(independent + merged["substantive_needs"]))
    merged["semantic_assessment"] = copy.deepcopy(response)
    merged["semantic_reconciliation"] = {
        "section": candidate["section"],
        "slot": candidate["slot"],
        "original_low_catalog_sha256": expected_catalog_sha256,
        "derived_role": "strong_criterion_adjudication",
        "request": copy.deepcopy(request),
        "answer": copy.deepcopy(validated),
        "actual": copy.deepcopy(strong_actual),
        "actual_prompt": copy.deepcopy(proof["expected"]["prompt"]),
        "derived_assessment": working,
        "selection_consumed": True,
        "authority": False,
    }
    _need(
        catalog_digest(_build(snapshot, packet, packet_ref, retained_dir)[-1])
        == expected_catalog_sha256,
        "supplied_catalog_view_changed",
    )
    return merged


def host_collection_plan(snapshot, packet, screen, *, existing_requests=(), basis=None):
    """Select existing necessary reads, never infer a locator from semantic prose.

    This is internal host data, not a model response or source/effect permit.
    The current callback, old request validator/collector and RecoveryStore own
    execution, actual job/head checks, budgets and no-progress retry decisions.
    """
    import closer_evidence_recovery as recovery

    allowed = {}
    for row in screen.get("requirements", []):
        _need(
            isinstance(row, dict) and isinstance(row.get("id"), str),
            "host_readiness_requirement_invalid",
        )
        _need(
            row["id"] not in allowed or allowed[row["id"]] == row,
            "host_readiness_requirement_ambiguous",
        )
        allowed[row["id"]] = row

    def collection_basis(value):
        _need(isinstance(value, dict), "host_collection_basis_unavailable")
        if set(value) in ({"source", "criterion"}, {"source", "criterion_sha256"}):
            expected = disposition._expected(snapshot) or []
            digest = (
                hashlib.sha256(value["criterion"].encode()).hexdigest()
                if isinstance(value.get("criterion"), str)
                else value.get("criterion_sha256")
            )
            _need(
                any(
                    source == value.get("source")
                    and hashlib.sha256(text.encode()).hexdigest() == digest
                    for source, text in expected
                ),
                "host_collection_basis_unavailable",
            )
            return {"source": value["source"], "criterion_sha256": digest}
        _need(
            set(value) == {"finding_id"}
            and any(
                row["finding_id"] == value["finding_id"]
                for row in disposition.pending_nonpass_findings(snapshot, packet)
            ),
            "host_collection_basis_unavailable",
        )
        return {"finding_id": value["finding_id"]}

    selected_basis = collection_basis(basis) if basis is not None else None
    requests = []
    for row in recovery.required(screen):
        if row.get("origin") != "readiness_rail" or row.get("section") not in recovery.SECTIONS:
            continue
        if selected_basis is not None:
            continue  # A semantic gap alone cannot promote a global rail.
        requests.append(
            {"requirement_id": row["id"], "necessity": "necessary", "reason": row["reason"]}
        )
    _need(isinstance(existing_requests, (list, tuple)), "host_existing_request_inventory_invalid")
    for row in existing_requests:
        _need(isinstance(row, dict), "host_existing_request_invalid")
        if row.get("necessity") != "necessary":
            continue
        current = allowed.get(row.get("id"))
        if current is not None and current.get("satisfied"):
            continue
        if (
            row.get("id") in allowed
            and row.get("section") in recovery.SECTIONS
            and selected_basis is None
        ):
            requests.append(
                {
                    "requirement_id": row["id"],
                    "necessity": "necessary",
                    "reason": row["reason"],
                    "retrieval_section": row["section"],
                }
            )
        elif isinstance(row.get("locator"), dict) and row["locator"].get("kind") == "job_log":
            if selected_basis is not None:
                row_basis = collection_basis(row.get("basis"))
                if row_basis != selected_basis:
                    continue
            requests.append(
                {
                    key: copy.deepcopy(row.get(key))
                    for key in ("necessity", "reason", "locator", "basis", "question")
                }
            )
    unique = []
    for row in requests:
        if row not in unique:
            unique.append(row)
    _need(len(unique) <= 20, "host_collection_request_bound")
    plan = {
        "schema": "closer-host-necessary-collection-plan/v1",
        "source": "existing_readiness_and_admitted_requests",
        "evidence_requests": unique,
        "authority": False,
    }
    validated = recovery.validate_requests(plan, snapshot, packet, screen)
    # Existing necessity is authoritative. Never collect an optional suggestion.
    _need(
        all(row["necessity"] == "necessary" for row in validated),
        "host_collection_necessity_changed",
    )
    return plan
