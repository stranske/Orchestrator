"""Exact local collector joins for the existing correction producer.

The retained seed is data, never authority. Original source/comment/report
observations must independently agree; required tests and ownership are absent
until an existing source owner supplies its independently validated decision.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from contextlib import contextmanager

import closer_correction_classification as contract
import closer_disposition as disposition
import closer_evidence as evidence_store
import closer_execution_contracts as execution
import verification_events


class CorrectionInputsUnavailable(ValueError):
    """Owned collection/owner wait, never a synthesized request or retry."""


def _need(value, reason):
    if not value:
        raise CorrectionInputsUnavailable(reason)


def collect_original_inputs(snapshot, packet, packet_ref):
    """Join only an actual packet's bounded native seed/report receipts.

    This returns source data and explicit native-to-seed identities. It does not
    create required tests, hold/operation IDs, ownership or an original request.
    """
    from closer_dispatcher import _read_controlled_receipt

    checked, root = execution.load_retained_packet(packet_ref)
    _need(checked == packet, "correction_original_packet_changed")
    marker = snapshot.get("verifier") or {}
    origin = snapshot.get("verifier_origin") or {}
    subject = snapshot.get("pr_target", snapshot.get("target"))
    _need(isinstance(subject, str) and "#" in subject, "correction_subject_missing")
    repo, number = subject.split("#")
    _need(
        marker.get("verdict") == "NON_PASS"
        and verification_events.valid_event(
            marker.get("verification_event"),
            repo,
            number,
            snapshot.get("head"),
            snapshot.get("merge"),
        ),
        "correction_original_event_missing",
    )
    _need(
        packet.get("target") == snapshot.get("target")
        and packet.get("head") == snapshot.get("head")
        and packet.get("base") == snapshot.get("base")
        and packet.get("capture_revision") == snapshot.get("revision"),
        "correction_original_packet_identity_changed",
    )
    _need(
        origin.get("schema") == "closer-terminal-comment-origin/v1"
        and origin.get("author") == "github-actions[bot]"
        and origin.get("actor_type") == "Bot",
        "correction_original_comment_missing",
    )
    section = packet.get("sections", {}).get("native_merge_verifier") or {}
    _need(section.get("status") == "complete", "correction_native_capture_incomplete")
    artifacts = (section.get("content") or {}).get("artifacts") or []
    candidates = []
    for artifact in artifacts:
        if not (
            artifact.get("status") == "complete"
            and str(artifact.get("run_id")) == str(marker.get("run_id"))
            and str(artifact.get("run_attempt")) == str(marker.get("run_attempt"))
            and artifact.get("evaluated_sha") == snapshot.get("merge")
        ):
            continue
        seeds = [
            r for r in artifact.get("receipts", []) if r.get("kind") == "native-correction-seeds"
        ]
        reports = [
            r
            for r in artifact.get("receipts", [])
            if r.get("kind") == "native-comparison" and r.get("path") == "comparison.json"
        ]
        events = [
            r
            for r in artifact.get("receipts", [])
            if r.get("kind") == "native-verification-event-manifest"
            and r.get("path") == "verification-event-manifest.json"
        ]
        if seeds:
            _need(
                len(seeds) == len(reports) == len(events) == 1,
                "correction_native_members_ambiguous",
            )
            candidates.append((artifact, seeds[0], reports[0], events[0]))
    _need(len(candidates) == 1, "correction_seed_capture_missing_or_ambiguous")
    artifact, seed_ref, report_ref, event_ref = candidates[0]
    bundle = _read_controlled_receipt(seed_ref.get("raw"), root)
    report = _read_controlled_receipt(report_ref.get("raw"), root)
    _need(
        contract._exact(
            bundle,
            "schema verification_event assessment_identity result_sha256 seeds bundle_sha256",
        )
        and bundle.get("schema") == "closer-correction-seed-bundle/v1"
        and contract._sealed(bundle, "bundle_sha256")
        and bundle.get("verification_event") == marker["verification_event"]
        and bundle.get("result_sha256") == report_ref["raw"]["sha256"],
        "correction_seed_original_result_changed",
    )
    # Input snapshot and verification request/material are distinct producer
    # documents. The event manifest must come from this SAME selected archive.
    manifest = _read_controlled_receipt(event_ref.get("raw"), root)
    _need(
        manifest.get("verification_event") == bundle["verification_event"]
        and manifest.get("assessment_identity") == bundle.get("assessment_identity")
        and contract.digest(manifest.get("request"))
        == bundle["verification_event"]["request_sha256"]
        and contract.digest(manifest.get("material"))
        == bundle["verification_event"]["material_sha256"],
        "correction_original_manifest_changed",
    )
    source_rows = [row for row in snapshot.get("sources", []) if row.get("kind") == "issue"]
    _need(len(source_rows) == 1, "correction_original_source_ambiguous")
    source = source_rows[0]
    source_section = packet.get("sections", {}).get("source") or {}
    source_content = source_section.get("content") or []
    _need(
        source_section.get("status") == "complete"
        and packet.get("bindings", {}).get("source")
        == evidence_store._hash(snapshot.get("sources"))
        and len(source_content) == 1
        and source_content[0].get("body") == source.get("body")
        and source_content[0].get("credential_pattern_present") is False
        and source_content[0].get("redaction_applied") is False
        and (source_content[0].get("body_retained") or {}).get("truncated") is False,
        "correction_original_source_capture_incomplete",
    )
    _need(
        isinstance(source.get("body"), str)
        and isinstance(source.get("node_id"), str)
        and source["node_id"],
        "correction_source_node_missing",
    )
    source_repo, source_number = source["target"].split("#")
    _need(source_repo.lower() == repo.lower(), "correction_source_repository_changed")
    _need(
        (source.get("provenance") or {}).get("body_sha256")
        == hashlib.sha256(source["body"].encode()).hexdigest(),
        "correction_source_provenance_changed",
    )
    source_identity = {
        "number": source_number,
        "node_id": source["node_id"],
        "body_sha256": hashlib.sha256(source["body"].encode()).hexdigest(),
    }
    target = {
        "repository": repo.lower(),
        "pr": number,
        "head_sha": snapshot["head"],
        "base_sha": snapshot["base"],
        "merge_sha": snapshot["merge"],
    }
    original_verifier = {
        "verification_event": marker["verification_event"],
        "run_id": str(marker["run_id"]),
        "run_attempt": str(marker["run_attempt"]),
        **{key: origin.get(key) for key in ("comment_id", "comment_sha256", "decision_sha256")},
    }
    providers = report.get("results") if isinstance(report, Mapping) else None
    _need(
        isinstance(providers, list) and 0 < len(providers) <= 20,
        "correction_original_results_missing",
    )
    raw_rows, mapping = [], []
    native = disposition.pending_nonpass_findings(snapshot, packet)
    seeds = bundle.get("seeds")
    _need(
        isinstance(seeds, list) and 0 < len(seeds) <= contract.MAX_FINDINGS,
        "correction_seed_bound_exceeded",
    )
    expected = []
    for provider_index, provider in enumerate(providers):
        _need(
            isinstance(provider, dict)
            and provider.get("used_llm") is True
            and not provider.get("error")
            and provider.get("verdict") in {"PASS", "CONCERNS", "FAIL"},
            "correction_original_provider_unavailable",
        )
        concerns = provider.get("concerns")
        _need(
            isinstance(concerns, list) and len(concerns) <= 100,
            "correction_original_concerns_missing",
        )
        for concern_index, text in enumerate(concerns):
            _need(
                isinstance(text, str) and 0 < len(text.encode()) <= 65536,
                "correction_original_concern_invalid",
            )
            evidence = {
                "ref": f"comparison.json#/results/{provider_index}/concerns/{concern_index}",
                "sha256": hashlib.sha256(text.encode()).hexdigest(),
            }
            finding = {"id": "raw-" + contract.digest(evidence), "evidence": evidence}
            expected.append(
                {
                    "schema": "closer-correction-seed/v1",
                    "target": target,
                    "source_issue": source_identity,
                    "original_verifier": original_verifier,
                    "finding": finding,
                    "scope": "substantive_implementation",
                }
            )
            matches = [
                row
                for row in native
                if row["raw"].get("source") == "native_provider_concern"
                and row["raw"].get("artifact_id") == artifact.get("id")
                and row["raw"].get("report_sha256") == report_ref["raw"]["sha256"]
                and row["raw"].get("raw", {}).get("id")
                == f"result-{provider_index}-concern-{concern_index}"
                and row["raw"]["raw"].get("text") == text
            ]
            _need(len(matches) == 1, "correction_native_finding_mapping_missing")
            raw_rows.append({**finding, "text": text})
            mapping.append(
                {
                    "native_finding_id": matches[0]["finding_id"],
                    "seed_finding_id": finding["id"],
                    "evidence": evidence,
                }
            )
    _need(seeds == expected, "correction_seed_source_or_inventory_changed")
    _need(
        [p["verdict"] for p in providers] == marker.get("provider_verdicts"),
        "correction_original_provider_verdict_changed",
    )
    return {
        "seed_bundle": bundle,
        "source_body": source["body"],
        "raw_findings": raw_rows,
        "native_seed_mapping": mapping,
        "retained_packet_ref": packet_ref,
        "evidence_scope": "local_collector_join_only",
        "remote_authenticated": False,
    }


def collect_substage(snapshot, packet, packet_ref):
    """Original interpret callback's bounded, nonauthorizing correction substage.

    The verifier's real seed intentionally contains no future tests, incumbent
    observation or hold budget. Those absent source-owner decisions are an owned
    UNKNOWN; model/caller text cannot fill them or authorize producer submission.
    """
    from closer_correction_producer import retain

    held = {
        "status": "owned_unknown",
        "evidence_scope": "local_collector_join_only",
        "remote_authenticated": False,
        "act_authorized": False,
        "source_closure_authorized": False,
        "retry_safe": False,
    }
    try:
        inputs = collect_original_inputs(snapshot, packet, packet_ref)
        _packet, root = execution.load_retained_packet(packet_ref)
        ref = retain(root, inputs, "correction-original-inputs.json")
        return {
            **held,
            "reason": "correction_source_owner_decision_unavailable",
            "missing_facts": [
                "independently_source_bound_required_test_decision",
                "observed_original_incumbent_or_confirmed_no_incumbent",
                "source_declared_original_operation_correlation_limits",
            ],
            "original_inputs_ref": ref,
            "native_seed_mapping": inputs["native_seed_mapping"],
        }
    except (ImportError, OSError, ValueError, TypeError, KeyError, RecursionError) as error:
        return {
            **held,
            "reason": str(error)[:512],
            "missing_facts": ["original_correction_collector_inputs"],
        }


def effect_identity(seed):
    """Frozen semantic identity; profiles, limits and later callback IDs are absent.

    Matches the existing selected-decision projection. This hash is only a
    local custody key, never authentication or a remote/global rollback anchor.
    """
    effect = contract.digest(
        {
            "schema": "closer-correction-effect/v1",
            "repository": seed["target"]["repository"],
            "event_id": seed["original_verifier"]["verification_event"]["event_id"],
            "finding_evidence": seed["finding"]["evidence"],
            "source_issue": seed["source_issue"],
            "scope": seed["scope"],
        }
    )
    scope = contract.digest(
        {
            "target": seed["target"],
            "source_issue": seed["source_issue"],
            "finding_evidence": seed["finding"]["evidence"],
            "scope": seed["scope"],
        }
    )
    return {"seed_effect_key": effect, "source_scope_digest": scope}


def _owner_semantic_digest(original, inputs):
    """Compare owner meaning, excluding later operation limits and custody paths.

    A stable seed fences another call; it does not make another normative test
    specification interchangeable with the original decision. This projection
    is a replay join only and cannot authenticate a supplied request.
    """
    selected = original["finding"]["id"]
    raw = [row for row in inputs["raw_findings"] if row["id"] == selected]
    mapping = [row for row in inputs["native_seed_mapping"] if row["seed_finding_id"] == selected]
    _need(len(raw) == len(mapping) == 1, "correction_owner_selected_finding_ambiguous")
    return contract.digest(
        {
            "owner_request": {
                key: original[key]
                for key in (
                    "schema",
                    "target",
                    "source_issue",
                    "original_verifier",
                    "finding",
                    "scope",
                    "required_tests",
                    "incumbent",
                )
            },
            "selected_raw_finding": raw[0],
            "selected_native_mapping": mapping[0],
        }
    )


def _owned_unknown(reason, *, actual=None, **detail):
    return {
        "status": "owned_unknown",
        "reason": str(reason)[:512],
        "remote_authenticated": False,
        "act_authorized": False,
        "source_closure_authorized": False,
        "retry_safe": False,
        **({"actual": actual} if isinstance(actual, dict) and actual.get("run_id") else {}),
        **detail,
    }


def _same_inode(first, second):
    return (first.st_dev, first.st_ino) == (second.st_dev, second.st_ino)


def _scope_name(identity):
    return contract.digest({"schema": "closer-correction-custody/v1", **identity})


@contextmanager
def _custody_parent(root, key, *, create):
    """Borrow a fixed source-owned original root; never follow renamed ancestors."""
    import os
    import stat
    from pathlib import Path

    root = Path(root)
    _need(root.is_absolute() and root.resolve() == root, "correction_custody_root_aliased")
    handles, edges = [], []
    try:
        current = os.open(root.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        handles.append(current)
        for part in root.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
            handles.append(child)
            edges.append((current, part, child))
            current = child
        original = os.fstat(current)
        _need(
            original.st_uid == os.getuid() and not original.st_mode & 0o022,
            "correction_custody_root_not_owned",
        )
        path = root
        for part in ("raw", key[:2]):
            try:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
            except FileNotFoundError:
                if not create:
                    yield None
                    return
                os.mkdir(part, mode=0o700, dir_fd=current)
                os.fsync(current)
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
            handles.append(child)
            edges.append((current, part, child))
            current = child
            path = path / part
            metadata = os.fstat(child)
            _need(
                stat.S_ISDIR(metadata.st_mode)
                and metadata.st_uid == os.getuid()
                and not metadata.st_mode & 0o022,
                "correction_custody_child_not_owned",
            )

        def check():
            for parent, name, held in edges:
                named = os.stat(name, dir_fd=parent, follow_symlinks=False)
                _need(
                    stat.S_ISDIR(named.st_mode) and _same_inode(named, os.fstat(held)),
                    "correction_custody_parent_moved",
                )

        check()
        yield path, current, check
    finally:
        for handle in reversed(handles):
            os.close(handle)


def _read_owned_document(parent_fd, name):
    """Read a bounded custody selector relative to its still-held directory."""
    import os
    import stat

    from closer_dispatcher import _strict_response_json

    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd)
    try:
        before = os.fstat(descriptor)
        _need(
            stat.S_ISREG(before.st_mode)
            and before.st_uid == os.getuid()
            and not before.st_mode & 0o022
            and 0 < before.st_size <= 512 * 1024,
            "correction_custody_document_invalid",
        )
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            raw = stream.read(before.st_size + 1)
        _need(
            len(raw) == before.st_size
            and _same_inode(before, os.stat(name, dir_fd=parent_fd, follow_symlinks=False)),
            "correction_custody_document_changed",
        )
        return _strict_response_json(raw.decode()), hashlib.sha256(raw).hexdigest()
    finally:
        os.close(descriptor)


def run_controlled_correction(
    *, context, preparation, stage_result, github, retained_dir, route_load
):
    """One local classifier inside the original still-owned exception callback.

    The fixed original state evidence root is a local namespace, not a remote or
    global rollback anchor. Migration must not recreate it or clear unknowns.
    """
    import os
    import time
    from datetime import datetime
    from pathlib import Path

    import closer_campaign as campaign
    import closer_correction_decision as decisions
    import closer_correction_owner as owner
    import closer_correction_producer as producer
    import closer_dispatcher as closer
    import closer_resources
    import dispatcher

    held_intent = None
    observed_actual = None
    try:
        # This source-owned method re-reads actual LOW/STRONG native proofs,
        # current exception/fence and bounded native ownership. No JSON opt-in.
        decision = owner.build_owner_request(
            context=context, preparation=preparation, stage_result=stage_result, github=github
        )
        if decision.get("status") != "ready":
            return _owned_unknown(decision.get("reason") or "correction_owner_proof_unavailable")
        inputs = decision["original_inputs"]
        packet, packet_root = execution.load_retained_packet(inputs["retained_packet_ref"])
        anchor = Path(retained_dir)
        _need(
            anchor.is_absolute() and anchor.resolve() == anchor and packet_root == anchor,
            "correction_original_state_anchor_differs",
        )
        original = decision["original_request"]
        declared_deadline = datetime.fromisoformat(
            original["original_operation"]["deadline"].replace("Z", "+00:00")
        )
        _need(declared_deadline.tzinfo is not None, "correction_declared_deadline_unzoned")
        correction_deadline = min(context["deadline"], declared_deadline.timestamp())
        seed = next(
            row for row in inputs["seed_bundle"]["seeds"] if row["finding"] == original["finding"]
        )
        identity = effect_identity(seed)
        key = _scope_name(identity)
        intent_name = key + ".correction-intent.json"
        link_name = key + ".correction-result.json"

        def current():
            owner._current(context, preparation, now=time.time())

        def replay(parent_fd, check):
            intent, intent_sha = _read_owned_document(parent_fd, intent_name)
            _need(
                intent.get("schema") == "closer-correction-local-intent/v1"
                and intent.get("identity") == identity
                and intent.get("anchor") == str(anchor),
                "correction_existing_intent_scope_changed",
            )
            _need(
                _owner_semantic_digest(intent["original_request"], intent["inputs"])
                == _owner_semantic_digest(original, inputs),
                "correction_original_owner_semantics_changed",
            )
            check()
            current()
            try:
                link, _sha = _read_owned_document(parent_fd, link_name)
            except FileNotFoundError:
                return _owned_unknown(
                    "correction_original_intent_result_missing", intent_identity=identity
                )
            _need(
                link.get("schema") == "closer-correction-local-result/v1"
                and link.get("intent_sha256") == intent_sha,
                "correction_original_result_link_changed",
            )
            old = closer._read_controlled_receipt(link["result_ref"], anchor)
            if old.get("status") != "materialized":
                return _owned_unknown(
                    "correction_original_outcome_unknown",
                    actual=old.get("actual"),
                    intent_identity=identity,
                    original_result_ref=link["result_ref"],
                )
            replayed = decisions.materialize_selected(
                proof=old["proof"],
                run_id=old["run_id"],
                basis=old["basis"],
                receipt_root=intent["receipt_root"],
                profile_id=intent["profile_id"],
                request_ref=old["request_ref"],
                original_request=intent["original_request"],
                **{
                    k: intent["inputs"][k]
                    for k in ("seed_bundle", "source_body", "raw_findings", "retained_packet_ref")
                },
            )
            check()
            current()
            import closer_correction_source_record as source_record

            source_ref = source_record.original_decision_reference(
                Path(anchor) / "raw" / key[:2], parent_fd, identity, intent_sha
            )
            return {
                **replayed,
                **({"source_decision_ref": source_ref} if source_ref else {}),
                "intent_identity": identity,
                "original_result_ref": link["result_ref"],
                "replayed": True,
            }

        with _custody_parent(anchor, key, create=False) as existing:
            if existing is not None:
                _path, parent_fd, check = existing
                try:
                    _read_owned_document(parent_fd, intent_name)
                except FileNotFoundError:
                    pass
                else:
                    held_intent = identity
                    return replay(parent_fd, check)
        source_invitation = decision["private_builder_proof"].get("low_proof_kind") == (
            "source_native_conservative_inventory"
        )
        if not source_invitation:
            route, status = closer._pipeline_low_route(
                load=route_load, data_only=True, task_type=producer.TASK, with_status=True
            )
            if route is None:
                return _owned_unknown("correction_task_route_" + str(status))

        def invoke(chosen):
            nonlocal held_intent, observed_actual
            current()
            _need(time.time() < correction_deadline, "correction_declared_deadline_expired")
            # The original producer independently reconstructs exact prompt/basis.
            if source_invitation:
                basis = stage_result["actual"]["_closer_controlled_proof"]["expected"]["basis"]
            else:
                _values, _request, _prompt, basis, _root = producer.prepare(
                    original_request=original,
                    **{
                        k: inputs[k]
                        for k in (
                            "seed_bundle",
                            "source_body",
                            "raw_findings",
                            "retained_packet_ref",
                        )
                    },
                    basis={
                        "target": preparation["snapshot"]["target"],
                        "revision": preparation["revision"],
                        "head": packet["head"],
                        "base": packet["base"],
                        "packet_sha256": inputs["retained_packet_ref"]["sha256"],
                    },
                )
            operation_id = contract.digest([context["work_key"], "correction", identity])
            intent = {
                "schema": "closer-correction-local-intent/v1",
                **({"kind": "source_corrective_invitation"} if source_invitation else {}),
                "identity": identity,
                "anchor": str(anchor),
                "original_request": original,
                "inputs": inputs,
                "profile_id": chosen["profile_id"],
                "receipt_root": str(Path(dispatcher.DISPATCH_LOG_DIR).resolve()),
                "basis": basis,
                "operation_id": operation_id,
                "owner_decision_proof": decision["private_builder_proof"],
                "ownership": decision["ownership"],
                "strong_actual": stage_result["actual"],
                "strong_requested_profile": stage_result["requested_profile"],
                "strong_requested_agent": stage_result["requested_agent"],
                "original_frame": {
                    "state_path": context["state_path"],
                    "frontier": context["finite_frontier"],
                    "pipeline_run_id": context["pipeline_run_id"],
                    "work_key": context["work_key"],
                    "deadline": context["deadline"],
                    "scope_lease": closer_resources.fence_identity(context["state_path"]),
                },
            }
            with _custody_parent(anchor, key, create=True) as held:
                path, parent_fd, check = held
                check()
                current()
                try:
                    campaign._atomic_write(
                        path / intent_name, intent, create_only=True, parent_fd=parent_fd
                    )
                except FileExistsError:
                    held_intent = identity
                    return replay(parent_fd, check)
                held_intent = identity
                _stored, intent_sha = _read_owned_document(parent_fd, intent_name)
                _need(_stored == intent, "correction_intent_readback_changed")
                check()
                current()
                _need(time.time() < correction_deadline, "correction_declared_deadline_expired")
                # A fresh source route/pool and fresh controlled options are used;
                # the original LOW/STRONG one-shot callbacks are never borrowed.
                if source_invitation:
                    import closer_conservative_intake as intake

                    result = intake.produce_invitation(
                        context=context,
                        preparation=preparation,
                        stage_result=stage_result,
                        decision=decision,
                    )
                else:
                    result = producer.produce(
                        original_request=original,
                        **{
                            k: inputs[k]
                            for k in (
                                "seed_bundle",
                                "source_body",
                                "raw_findings",
                                "retained_packet_ref",
                            )
                        },
                        profile_id=chosen["profile_id"],
                        frontier=context["finite_frontier"],
                        basis=basis,
                        operation_id=operation_id,
                        deadline=correction_deadline,
                    )
                observed_actual = result.get("actual") if isinstance(result, dict) else None
                check()
                # Outcome persistence is not a new invocation or authority.
                # Retain it even if cleanup consumed the last deadline margin.
                result_name = key + ".correction-native-result.json"
                campaign._atomic_write(
                    path / result_name, result, create_only=True, parent_fd=parent_fd
                )
                persisted, result_sha = _read_owned_document(parent_fd, result_name)
                _need(persisted == result, "correction_native_result_readback_changed")
                ref = {
                    "path": str(path / result_name),
                    "sha256": result_sha,
                    "bytes": os.stat(result_name, dir_fd=parent_fd, follow_symlinks=False).st_size,
                }
                campaign._atomic_write(
                    path / link_name,
                    {
                        "schema": "closer-correction-local-result/v1",
                        "intent_sha256": intent_sha,
                        "result_ref": ref,
                    },
                    create_only=True,
                    parent_fd=parent_fd,
                )
                check()
                source_ref = None
                if result.get("status") == "materialized":
                    import closer_correction_source_record as source_record

                    try:
                        source_ref = source_record.retain_original_decision(
                            context=context,
                            preparation=preparation,
                            intent=intent,
                            result=result,
                            result_ref=ref,
                            path=path,
                            parent_fd=parent_fd,
                            check=check,
                            github=github,
                        )
                    except (OSError, ValueError, TypeError, KeyError, RuntimeError) as error:
                        # Data classification is retained even when additional
                        # native publication provenance is absent. No retrofit,
                        # source authority, provider retry or generic ACT follows.
                        return {
                            **result,
                            "intent_identity": identity,
                            "original_result_ref": ref,
                            "source_decision_status": "owned_unknown",
                            "source_decision_reason": str(error)[:512],
                        }
                return {
                    **result,
                    **({"source_decision_ref": source_ref} if source_ref else {}),
                    "intent_identity": identity,
                    "original_result_ref": ref,
                }

        if source_invitation:
            # No LOW/classifier slot, risk label or provider completion is made up.
            return invoke({"profile_id": stage_result["requested_profile"]})
        result, _chosen = closer._execute_profile_route(
            route,
            invoke,
            lane="low",
            load=route_load,
            deadline=correction_deadline,
            data_only=True,
            task_type=producer.TASK,
            finite_frontier=context["finite_frontier"],
            frontier_target=preparation["snapshot"]["target"],
        )
        return result
    except (
        ImportError,
        OSError,
        ValueError,
        KeyError,
        TypeError,
        RuntimeError,
        RecursionError,
        AttributeError,
        OverflowError,
    ) as error:
        return _owned_unknown(
            type(error).__name__ + ": " + str(error),
            actual=observed_actual,
            **({"intent_identity": held_intent} if held_intent else {}),
        )
