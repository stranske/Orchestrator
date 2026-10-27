"""Protected original source decision and later native-queue selection.

The JSON record is provenance, never an authorization flag. Selection requires
fresh installed Runtime provenance and the genuine same-thread submitted queue
callback, plus independent original native-proof/source/owner validation.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import threading
import time
import weakref
from dataclasses import dataclass, field
from pathlib import Path

SCHEMA = "closer-correction-source-decision/v1"
_LOADED_SOURCE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


class SourceRecordUnavailable(ValueError):
    pass


def _need(value, reason):
    if not value:
        raise SourceRecordUnavailable(reason)


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _source_sha():
    current = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    _need(current == _LOADED_SOURCE_SHA256, "loaded source-record code changed on disk")
    return _LOADED_SOURCE_SHA256


def _reference(path, parent_fd):
    import closer_correction_bridge as bridge

    _document, sha = bridge._read_owned_document(parent_fd, Path(path).name)
    return {
        "path": str(path),
        "sha256": sha,
        "bytes": os.stat(Path(path).name, dir_fd=parent_fd, follow_symlinks=False).st_size,
    }


def materializer_arguments(intent, result):
    """Exact original arguments; no newly selected profile, deadline or request."""
    return {
        "proof": result["proof"],
        "run_id": result["run_id"],
        "basis": result["basis"],
        "receipt_root": intent["receipt_root"],
        "profile_id": intent["profile_id"],
        "request_ref": result["request_ref"],
        "original_request": intent["original_request"],
        **{
            key: intent["inputs"][key]
            for key in ("seed_bundle", "source_body", "raw_findings", "retained_packet_ref")
        },
    }


def retained_issue_discovery(binding, target):
    """Bounded fixed-custody hint only; never native reads or authorization.

    Every automatic issue POST already has its create-only native-plan file.
    Discover that pre-write marker before a new finite action is frozen. A
    missing raw response is still owned UNKNOWN and can never select creation.
    The actual new queue callback must independently authenticate every byte.
    """
    import closer_correction_controller as ctl

    policy = getattr(binding, "policy", None)
    declared = policy.get("correction_controller_installation") if type(policy) is dict else None
    if declared is None:
        return None
    custody = None
    try:
        _need(type(declared) is dict, "controller discovery policy unavailable")
        config = ctl.ControllerConfig(**declared["config"])
        repository, sep, number = target.rpartition("#")
        _need(sep and number.isdecimal(), "controller target differs")
        if repository.casefold() != config.repository.casefold():
            return None
        _need(
            type(config.max_entries) is int
            and 0 < config.max_entries <= 128
            and type(config.max_bytes) is int
            and 0 < config.max_bytes <= 1048576,
            "controller discovery bounds invalid",
        )
        custody = ctl._Custody(config)
        _need(
            custody.read("namespace.json")
            == {
                "schema": "local-correction-controller-namespace/v1",
                "config_sha256": ctl.digest(config.source_binding()),
            },
            "controller discovery namespace differs",
        )
        names = os.listdir(custody.fd)
        _need(
            len(names) <= 64 * config.max_entries + 64, "controller discovery inventory saturated"
        )
        plans = sorted(n for n in names if n.startswith("native-plan-"))
        _need(len(plans) <= 4 * config.max_entries, "controller discovery plans saturated")
        custody._inspection_read_budget = [min(config.max_bytes * 4, 16 * 1024 * 1024)]
        matched = []
        for name in plans:
            plan = custody.read(name)
            _need(
                type(plan) is dict and plan.get("schema") == "local-correction-native-plan/v1",
                "controller discovery plan unreadable",
            )
            request = plan.get("request")
            _need(
                type(request) is dict and type(request.get("target")) is dict,
                "controller discovery target unavailable",
            )
            if (
                str(request["target"].get("repository")).casefold() != repository.casefold()
                or str(request["target"].get("pr")) != number
            ):
                continue
            if plan.get("phase") != "issue_creation":
                continue
            _need(
                name == "native-plan-" + plan["phase_key"] + ".json"
                and plan.get("key") == ctl.effect_key(request),
                "controller discovery plan identity differs",
            )
            matched.append(
                {
                    "plan_name": name,
                    "plan_sha256": ctl.digest(plan),
                    "effect_key": plan["key"],
                    "phase_key": plan["phase_key"],
                }
            )
        if not matched:
            return None
        _need(len(matched) == 1, "multiple retained issue operations require owned reconciliation")
        return {
            "schema": "closer-retained-issue-discovery/v1",
            "target": target,
            "selector": matched[0],
            "authority": False,
        }
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as error:
        return {
            "schema": "closer-retained-issue-discovery/v1",
            "target": target,
            "selector": None,
            "authority": False,
            "reason": str(error)[:500],
        }
    finally:
        if custody is not None:
            custody.close()


def operation_lifecycle_origin(controller, operation):
    """Retain original callback identity before POST; never renew old authority."""
    import closer_native_observer as native
    import closer_queue as queue

    installation = controller._source_installation()
    start = native.source_b_start(installation)
    if start is None:
        return None  # old policies retain original behavior; no recovery receipt
    invocation = getattr(queue._callback_threads, "invocation", None)
    _need(
        type(invocation) is dict
        and queue._currently_invoking(invocation["target"], operation)
        and type(invocation.get("state_path")) is str,
        "actual source queue origin unavailable before native publication",
    )
    state = controller._physical_state(invocation["state_path"], "original queue origin redirected")
    bucket = state
    frontier = operation.get("finite_frontier")
    _need(type(frontier) is dict, "cleanup recovery requires explicit original frontier")
    bucket = state["frontiers"][frontier["frontier_id"]]
    _need(
        bucket.get("policy") == frontier
        and bucket["items"][invocation["target"]]["operation"] == operation,
        "physical original callback operation changed before publication",
    )
    return {
        "schema": "closer-native-operation-lifecycle-origin/v1",
        "queue_path": invocation["state_path"],
        "target": invocation["target"],
        "operation_json": queue._canonical_bytes(operation).decode("utf-8"),
        "operation_sha256": hashlib.sha256(queue._canonical_bytes(operation)).hexdigest(),
        "native_b_start": start,
    }


def _terminal_enrollment_links(cfg, start_ref, process, custody, names):
    """Prospective marker custody correlation; never synthesize instruction plans."""
    import closer_correction_controller as ctl
    import closer_correction_enrollment_terminal as enrollment
    import closer_queue as queue

    selected = [name for name in names if name.startswith("enrollment-terminal-")]
    _need(
        len(selected)
        <= cfg["policy"]["correction_controller_installation"]["config"]["max_entries"],
        "cleanup enrollment inventory saturated",
    )
    links = []
    for name in selected:
        marker = custody.read(name)
        _need(
            type(marker) is dict and marker.get("schema") == enrollment.SCHEMA,
            "cleanup enrollment marker unreadable",
        )
        if marker.get("native_b_start") != start_ref:
            continue
        reference = enrollment._ref(marker)
        operation = json.loads(marker["operation_json"])
        frontier, target = marker["finite_frontier"], marker["target"]
        _need(
            name == reference["name"]
            and operation.get("action") == "disposition"
            and operation.get("status") == "submitted"
            and queue._canonical_bytes(operation).decode() == marker["operation_json"]
            and hashlib.sha256(queue._canonical_bytes(operation)).hexdigest()
            == marker["operation_sha256"]
            and frontier
            == operation.get("finite_frontier")
            == cfg["native_admission"]["finite_frontier"]
            and marker["instruction_effect"]["owner"].get("process_id") == process["pid"],
            "cleanup enrollment original B/operation differs",
        )
        context = json.loads(marker["context_identity_json"])
        pipeline = ctl.IndexController._physical_state(
            context["state_path"], "cleanup enrollment pipeline redirected"
        )
        bucket = pipeline["frontiers"][frontier["frontier_id"]]
        row = bucket["items"][target]
        expected_stage = {
            "pipeline_state_path": context["state_path"],
            "pipeline_run_id": bucket["run_id"],
            "work_key": row["work_key"],
            "identity": row["identity"],
            "revision": row["revision"],
            "action_state_path": str(
                Path(context["state_path"]).parent
                / (Path(context["state_path"]).stem + ".actions")
                / (queue._digest(target) + ".json")
            ),
        }
        boundary = enrollment.validate_queue_boundary(
            manifest_path=cfg["runtime_manifest"],
            state_path=marker["queue_path"],
            target=target,
            finite_frontier=frontier,
            proof_ref=reference,
            expected_stage=expected_stage,
            expected_operation_id=operation["id"],
        )
        _need(
            boundary is not None
            and boundary["native_b_start"] == start_ref
            and boundary["operation_sha256"] == marker["operation_sha256"],
            "cleanup enrollment source knowledge unavailable",
        )
        state = ctl.IndexController._physical_state(
            marker["queue_path"], "cleanup enrollment queue redirected"
        )
        retained = state["frontiers"][frontier["frontier_id"]]["items"][target]["operation"]
        _need(retained.get("status") == "completed", "cleanup enrollment outcome not known")
        links.append(
            {
                "schema": "closer-native-enrollment-operation-link/v1",
                "target": target,
                "enrollment_terminal_ref": reference,
                "operation_id": operation["id"],
                "operation_sha256": marker["operation_sha256"],
                "retained_operation_sha256": hashlib.sha256(
                    queue._canonical_bytes(retained)
                ).hexdigest(),
                "retained_status": "completed",
                "native_b_start": start_ref,
                "instruction_intent_sha256": marker["instruction_effect"]["intent_sha256"],
                "provider_completion_asserted": False,
                "provider_history": {
                    "state_path": context["state_path"],
                    "target": target,
                    "pipeline_run_id": bucket["run_id"],
                    "source_record_ref": marker["source_record_ref"],
                    "row_sha256": hashlib.sha256(queue._canonical_bytes(row)).hexdigest(),
                },
            }
        )
    return links


def terminal_operation_links(cfg, start_ref, process):
    """Read only original fixed-custody callback links for this observed B.

    This records lifecycle correlation, never provider completion or source
    acceptance. The new reconciliation callback must revalidate all providers
    and native201 evidence independently before its single confirmation CAS.
    """
    import closer_correction_controller as ctl
    import closer_queue as queue

    config = ctl.ControllerConfig(**cfg["policy"]["correction_controller_installation"]["config"])
    custody = ctl._Custody(config)
    custody._inspection_read_budget = [min(config.max_bytes * 8, 16 * 1024 * 1024)]
    try:
        _need(
            custody.read("namespace.json")
            == {
                "schema": "local-correction-controller-namespace/v1",
                "config_sha256": ctl.digest(config.source_binding()),
            },
            "cleanup source custody differs",
        )
        names = sorted(os.listdir(custody.fd))
        _need(len(names) <= 64 * config.max_entries + 64, "cleanup source inventory saturated")
        plans = [name for name in names if name.startswith("native-plan-")]
        _need(len(plans) <= 4 * config.max_entries, "cleanup source plan inventory saturated")
        links = []
        for name in plans:
            plan = custody.read(name)
            _need(type(plan) is dict, "cleanup source plan unreadable")
            origin = plan.get("native_lifecycle")
            if origin is None or plan.get("owner", {}).get("process_id") != process["pid"]:
                continue
            _need(
                type(origin) is dict and origin.get("native_b_start") == start_ref,
                "cleanup original B identity differs",
            )
            operation = json.loads(origin["operation_json"])
            target = origin["target"]
            frontier = operation.get("finite_frontier")
            _need(
                origin.get("schema") == "closer-native-operation-lifecycle-origin/v1"
                and queue._canonical_bytes(operation).decode("utf-8") == origin["operation_json"]
                and operation.get("status") == "submitted"
                and operation.get("action") == "disposition"
                and hashlib.sha256(queue._canonical_bytes(operation)).hexdigest()
                == origin.get("operation_sha256")
                == plan["owner"].get("operation_sha256")
                and operation.get("id") == plan["owner"].get("operation_id")
                and plan["owner"].get("target") == target
                and frontier == cfg["native_admission"].get("finite_frontier")
                and target in frontier["targets"]
                and name == "native-plan-" + plan["phase_key"] + ".json"
                and plan.get("phase") in {"issue_creation", "instruction_publication"}
                and plan.get("key") == ctl.effect_key(plan["request"]),
                "cleanup original operation/source plan differs",
            )
            state = ctl.IndexController._physical_state(
                origin["queue_path"], "cleanup original queue redirected"
            )
            bucket = state["frontiers"][frontier["frontier_id"]]
            row = bucket["items"][target]
            retained = row["operation"]
            _need(
                bucket.get("policy") == frontier
                and bucket.get("round", {}).get("finite_frontier") == frontier
                and type(retained) is dict,
                "cleanup original queue frontier differs",
            )
            # Only documented queue result/lifecycle additions may differ after
            # callback return. All original request/binding/deadline fields stay.
            additions = {
                "result",
                "elapsed_seconds",
                "next_trigger",
                "completed_at",
                "reconcile_next_trigger",
                "invoked_at",
            }
            _need(
                all(retained.get(k) == value for k, value in operation.items() if k != "status")
                and not (set(retained) - set(operation) - additions)
                and retained.get("status")
                in {"completed", "uncertain", "failed", "submitted", "running"}
                and type(retained.get("invoked_at")) in (int, float)
                and operation["submitted_at"] <= retained["invoked_at"] < operation["deadline"],
                "cleanup original queue operation immutable fields changed",
            )
            provider_history = None
            source_ref = plan.get("source_record_ref")
            if source_ref is not None:
                import closer_dispatcher as closer

                anchor = Path(source_ref["path"]).parent.parent.parent
                record = closer._read_controlled_receipt(source_ref, anchor)
                frame = record["original_frame"]
                old_state = ctl.IndexController._physical_state(
                    frame["state_path"], "original provider pipeline custody redirected"
                )
                old_entry = old_state["frontiers"][frame["frontier"]["frontier_id"]]
                old_row = old_entry["items"][target]
                correction = old_row["results"]["exception"]["correction"]
                _need(
                    record.get("schema") == SCHEMA
                    and record.get("issuer_source_sha256") == _source_sha()
                    and record.get("anchor") == str(anchor)
                    and frame["frontier"] == frontier
                    and old_entry["finite_frontier"] == frontier
                    and old_entry["run_id"] == frame["pipeline_run_id"]
                    and old_row["stage_results"]["exception"]["status"] == "completed"
                    and old_row["stage_results"]["exception"]["work_key"] == frame["work_key"]
                    and correction.get("source_decision_ref") == source_ref
                    and correction.get("original_result_ref") == record["native_result_ref"],
                    "original provider/source decision rows differ",
                )
                provider_history = {
                    "state_path": frame["state_path"],
                    "target": target,
                    "pipeline_run_id": frame["pipeline_run_id"],
                    "source_record_ref": source_ref,
                    "row_sha256": hashlib.sha256(queue._canonical_bytes(old_row)).hexdigest(),
                }
            links.append(
                {
                    "target": target,
                    "plan_name": name,
                    "plan_sha256": ctl.digest(plan),
                    "origin_sha256": ctl.digest(origin),
                    "operation_id": operation["id"],
                    "operation_sha256": origin["operation_sha256"],
                    "retained_operation_sha256": hashlib.sha256(
                        queue._canonical_bytes(retained)
                    ).hexdigest(),
                    "retained_status": retained["status"],
                    "provider_completion_asserted": False,
                    "provider_history": provider_history,
                }
            )
        links.extend(_terminal_enrollment_links(cfg, start_ref, process, custody, names))
        _need(len(links) <= 128, "cleanup combined operation inventory saturated")
        return links
    finally:
        custody.close()


def _ordinary_selected(materialized, original):
    rows = materialized.get("decision", {}).get("findings")
    _need(isinstance(rows, list), "source classifier decision inventory missing")
    selected = [row for row in rows if row.get("id") == original["finding"]["id"]]
    _need(
        len(selected) == 1 and selected[0].get("classification") == "ordinary_substantive",
        "selected correction requires existing qualified risk owner",
    )
    _need(
        not any(row.get("classification") in {"security", "authority_conflict"} for row in rows),
        "security or conflicting authority requires its qualified owner",
    )


def _issue_context(reader, repository, identity, required_tests, *, assigned=False, native=False):
    from datetime import datetime

    import closer_correction_controller as controller_source
    import closer_dispatcher as closer

    _need(type(reader) is closer.GitHub, "original native GitHub reader required")
    doc = reader.api("repos/" + repository + "/issues/" + identity["number"])
    _need(
        str(doc.get("number")) == identity["number"]
        and doc.get("node_id") == identity["node_id"]
        and doc.get("state") == "open"
        and not doc.get("pull_request")
        and controller_source.native_api_url(
            doc.get("url"), repository, "/issues/" + identity["number"]
        )
        and isinstance(doc.get("body"), str)
        and len(doc["body"].encode()) <= 131072
        and hashlib.sha256(doc["body"].encode()).hexdigest() == identity["body_sha256"]
        and isinstance(doc.get("updated_at"), str)
        and doc["updated_at"],
        "native source/incumbent body or version unavailable",
    )
    stamp = datetime.fromisoformat(doc["updated_at"].replace("Z", "+00:00"))
    _need(stamp.tzinfo is not None, "native source version is unzoned")
    value = {
        "repository": repository,
        **{key: identity[key] for key in ("number", "node_id", "body_sha256")},
        "updated_at": doc["updated_at"],
        "body": doc["body"],
        "required_tests": required_tests,
    }
    if assigned:
        assignees = doc.get("assignees")
        _need(
            isinstance(assignees, list) and 1 <= len(assignees) <= 32,
            "native incumbent assignees unavailable",
        )
        selected = [{key: row[key] for key in ("id", "login", "type")} for row in assignees]
        _need(
            all(
                type(row["id"]) is int
                and 0 < row["id"] <= 2**53 - 1
                and isinstance(row["login"], str)
                and 0 < len(row["login"]) <= 256
                and row["type"] in {"User", "Bot"}
                for row in selected
            )
            and len({row["id"] for row in selected}) == len(selected),
            "native incumbent assignee identity malformed",
        )
        value["assignees"] = sorted(selected, key=lambda row: row["id"])
    _need(len(_json(doc).encode()) <= 131072, "native source document exceeds bound")
    return (value, doc) if native else value


def retain_original_decision(
    *, context, preparation, intent, result, result_ref, path, parent_fd, check, github
):
    """Create once only while the actual original EXCEPTION owner is still live.

    This records a selected data decision. It does not enroll a controller,
    provision an anchor, impersonate native queue TLS or authorize any effect.
    """
    import closer_campaign as campaign
    import closer_correction_bridge as bridge
    import closer_correction_decision as decisions
    import closer_correction_owner as owner

    owner._current(context, preparation, now=time.time())
    check()
    _need(result.get("status") == "materialized", "source correction outcome unknown")
    materialized = decisions.materialize_selected(**materializer_arguments(intent, result))
    _need(
        materialized.get("status") == "materialized"
        and materialized.get("decision") == result.get("decision")
        and materialized.get("actual", {}).get("run_id") == result.get("actual", {}).get("run_id"),
        "original classifier materializer changed",
    )
    _ordinary_selected(materialized, intent["original_request"])
    original = intent["original_request"]
    original_source_context, original_source_native = _issue_context(
        github,
        original["target"]["repository"],
        original["source_issue"],
        original["required_tests"],
        native=True,
    )
    original_incumbent_context = (
        None
        if original["incumbent"].get("kind") == "no_incumbent"
        else _issue_context(
            github,
            original["target"]["repository"],
            original["incumbent"]["issue"],
            original["required_tests"],
            assigned=True,
        )
    )
    key = bridge._scope_name(intent["identity"])
    parent = Path(path)
    _need(
        str(parent.parent.parent) == intent["anchor"] and parent.name == key[:2],
        "original source custody anchor differs",
    )
    intent_ref = _reference(parent / (key + ".correction-intent.json"), parent_fd)
    link_ref = _reference(parent / (key + ".correction-result.json"), parent_fd)
    record = {
        "schema": SCHEMA,
        "issuer_source_sha256": _source_sha(),
        "identity": intent["identity"],
        "anchor": intent["anchor"],
        "original_frame": intent["original_frame"],
        "original_source_context": original_source_context,
        "original_source_native": original_source_native,
        "original_incumbent_context": original_incumbent_context,
        "snapshot_ref": preparation["snapshot_ref"],
        "packet_ref": preparation["packet_ref"],
        "intent_ref": intent_ref,
        "result_link_ref": link_ref,
        "native_result_ref": result_ref,
        "owner_semantics_sha256": bridge._owner_semantic_digest(
            intent["original_request"], intent["inputs"]
        ),
        "classifier_decision_sha256": bridge.contract.digest(materialized["decision"]),
        "classifier_run_id": materialized["actual"]["run_id"],
    }
    owner._current(context, preparation, now=time.time())
    check()
    filename = parent / (key + ".correction-source-decision.json")
    campaign._atomic_write(filename, record, create_only=True, parent_fd=parent_fd)
    check()
    return _reference(filename, parent_fd)


def original_decision_reference(path, parent_fd, identity, intent_sha):
    """Read an existing original record only; never retrofit a missing record."""
    import closer_correction_bridge as bridge

    key = bridge._scope_name(identity)
    filename = Path(path) / (key + ".correction-source-decision.json")
    try:
        record, _sha = bridge._read_owned_document(parent_fd, filename.name)
    except FileNotFoundError:
        return None
    _need(
        record.get("schema") == SCHEMA
        and record.get("identity") == identity
        and record.get("issuer_source_sha256") == _source_sha()
        and record.get("intent_ref", {}).get("sha256") == intent_sha,
        "original source decision identity changed",
    )
    return _reference(filename, parent_fd)


class _SelectionSeal:
    def __init__(self, operation):
        import closer_queue

        self.owner = None
        self.pid = os.getpid()
        self.thread = threading.get_ident()
        self.operation_sha256 = hashlib.sha256(closer_queue._canonical_bytes(operation)).hexdigest()


@dataclass(frozen=True)
class SourceSelection:
    """Opaque current selection. JSON/copying a dataclass is not an issuer."""

    _record_ref_json: str
    _pipeline_context_json: str
    _installation: object = field(repr=False)
    _issuer: object = field(default=None, repr=False, compare=False)


class _CandidateSeal:
    def __init__(self):
        self.owner = None
        self.pid = os.getpid()


@dataclass(frozen=True)
class SourceCandidate:
    """Private route metadata, never native queue/effects authority."""

    _record_ref_json: str
    _pipeline_context_json: str
    _installation: object = field(repr=False)
    _issuer: object = field(default=None, repr=False, compare=False)


def _project_context(context):
    return {
        key: context[key]
        for key in (
            "state_path",
            "action_state_path",
            "pipeline_run_id",
            "revision",
            "identity",
            "deadline",
            "finite_frontier",
            "frontier_binding",
            "work_key",
            "prepare",
            "interpret",
            "exception",
        )
    }


def _installed_current(installation):
    import closer_source_installation as installed

    _need(
        installed.validate_source_installation(installation),
        "fresh factory installation unavailable",
    )
    _need(
        installation.source_closure.get("closer_correction_source_record.py")
        == {"path": str(Path(__file__).resolve()), "sha256": _source_sha()},
        "source-record module outside actual installed closure",
    )


def candidate_for_original_act(*, context, installation):
    """Select read-only route metadata from a real completed source decision.

    This pre-queue frame does not mint an effects selection. Only the later
    actual native queue callback can mint SourceSelection.
    """
    import closer_correction_bridge as bridge

    _installed_current(installation)
    current = _current_act_frame(context, installation)
    ref = context["exception"]["correction"]["source_decision_ref"]
    prep, snapshot, packet, root, record, intent, _result, _arguments, _materialized = _local_facts(
        context, ref
    )
    seed, tests, inputs = _qualified_owner_projection(context, current, snapshot, packet, root)
    semantic = {
        "schema": intent["original_request"]["schema"],
        **{
            key: seed[key]
            for key in ("target", "source_issue", "original_verifier", "finding", "scope")
        },
        "required_tests": tests,
        "incumbent": intent["original_request"]["incumbent"],
    }
    _need(
        bridge._owner_semantic_digest(semantic, inputs) == record["owner_semantics_sha256"],
        "current source criterion/test selection changed",
    )
    seal = _CandidateSeal()
    candidate = SourceCandidate(
        _json(ref), _json(_project_context(context)), installation, _issuer=seal
    )
    seal.owner = weakref.ref(candidate)
    return candidate


def _current_act_frame(context, installation):
    """Original live pipeline ACT frame; this is not a native queue invocation."""
    import closer_correction_owner as owner
    import closer_pipeline as pipeline
    import closer_resources as resources

    target = context["prepare"]["snapshot"]["target"]
    fence, lease = resources.fence_for(context["state_path"]), resources.fence_identity(
        context["state_path"]
    )
    _need(fence is not None and lease is not None, "current ACT resource fence missing")
    fence()
    state, _raw = owner._physical_json(context["state_path"])
    frontier = context["finite_frontier"]
    entry = state.get("frontiers", {}).get(frontier["frontier_id"], {})
    row = entry.get("items", {}).get(target, {})
    binding = frontier["bindings"][target]
    _need(
        entry.get("finite_frontier") == frontier == installation.finite_frontier
        and context["frontier_binding"] == binding
        and entry.get("run_id") == context["pipeline_run_id"]
        and row.get("stage") == "act"
        and row.get("stage_status") == row.get("status") == "running"
        and row.get("scope_lease") == lease
        and row.get("identity") == context["identity"]
        and row.get("revision") == context["revision"] == binding["revision"]
        and row.get("work_key")
        == context["work_key"]
        == pipeline._digest([target, "act", context["revision"], binding])
        and row.get("stage_deadline") == context["deadline"]
        and time.time() < context["deadline"]
        and all(
            row.get("results", {}).get(key) == context.get(key)
            for key in ("prepare", "interpret", "exception")
        ),
        "current original ACT/frame identity differs",
    )
    return row


def _current_act(context, operation, installation):
    import closer_queue as queue

    target = context["prepare"]["snapshot"]["target"]
    _need(queue._currently_invoking(target, operation), "actual submitted native callback required")
    live = getattr(queue._callback_threads, "invocation", {})
    _need(
        live.get("state_path") == str(Path(context["action_state_path"]).resolve())
        and operation.get("finite_frontier") == context["finite_frontier"]
        and operation.get("revision") == context["revision"]
        and operation.get("action") == "disposition"
        and time.time() < operation["deadline"],
        "actual native queue path/action differs from original ACT callback",
    )
    return _current_act_frame(context, installation)


def _prepared(context):
    import closer_correction_owner as owner
    import closer_execution_contracts as contracts

    prep = (context.get("interpret") or {}).get("recovered_prepare") or context["prepare"]
    snapshot, raw = owner._physical_json(prep["snapshot_ref"]["path"])
    _need(
        hashlib.sha256(raw).hexdigest() == prep["snapshot_ref"]["sha256"],
        "current snapshot changed",
    )
    packet, root = contracts.load_retained_packet(prep["packet_ref"])
    return prep, snapshot, packet, root


def _qualified_owner_projection(context, row, snapshot, packet, root, *, historical=False):
    import closer_correction_bridge as bridge
    import closer_correction_owner as owner

    prep = (context.get("interpret") or {}).get("recovered_prepare") or context["prepare"]
    inputs = bridge.collect_original_inputs(snapshot, packet, prep["packet_ref"])
    basis = {
        "target": snapshot["target"],
        "revision": snapshot["revision"],
        "head": snapshot["head"],
        "base": snapshot["base"],
        "packet_sha256": prep["packet_ref"]["sha256"],
    }
    low, low_proof = owner._stage(
        context["interpret"],
        task="closer_interpret",
        basis=basis,
        operation_id=row["stage_results"]["interpret"]["work_key"],
        now=(
            time.time()
            if not historical
            else retained_provider_time(context["interpret"]["actual"]["_closer_controlled_proof"])
        ),
    )
    strong_stage = context["exception"]
    strong_basis = strong_stage["actual"]["_closer_controlled_proof"]["expected"]["basis"]
    _need(
        all(strong_basis.get(key) == value for key, value in basis.items()),
        "current STRONG source basis changed",
    )
    strong, strong_proof = owner._stage(
        strong_stage,
        task="closer_exception",
        basis=strong_basis,
        operation_id=row["stage_results"]["exception"]["work_key"],
        now=(
            time.time()
            if not historical
            else retained_provider_time(strong_stage["actual"]["_closer_controlled_proof"])
        ),
    )
    _need(
        all(
            proof["expected"].get("finite_frontier_id") == context["finite_frontier"]["frontier_id"]
            and proof["expected"].get("finite_frontier_target") == snapshot["target"]
            for proof in (low_proof, strong_proof)
        ),
        "current native LOW/STRONG frontier identity differs",
    )
    seed, tests, _named = owner._selected_defect(
        snapshot=snapshot,
        packet=packet,
        packet_ref=prep["packet_ref"],
        root=root,
        low=low,
        strong=strong,
        inputs=inputs,
    )
    return seed, tests, inputs


def _local_facts(context, ref):
    return _read_local_facts(context, ref, require_current_deadline=True)


def retained_provider_time(proof):
    """Read a bound historical timestamp; never a current effect permission.

    Only the canonical maintenance reader supplies the authenticated original
    B window through its active source-held frame. Revalidation still requires the exact supported original
    source/profile contract; a historical time cannot make source mixing valid.
    """
    import closer_coding_worker
    import closer_correction_maintenance as maintenance
    import closer_dispatcher as closer
    import dispatcher

    execution_window = closer_coding_worker.historical_window(proof)
    if execution_window is None:
        execution_window = maintenance._historical_window()
    result = closer._revalidate_controlled_proof(
        proof,
        run_id=proof["run_id"],
        basis=proof["expected"]["basis"],
        receipt_root=dispatcher.DISPATCH_LOG_DIR,
    )
    receipt = closer._read_controlled_receipt(
        proof["provider_result_ref"], dispatcher.DISPATCH_LOG_DIR
    )
    return _bounded_provider_time(proof, result, receipt, execution_window)


def _bounded_provider_time(proof, result, receipt, execution_window):
    """Pure necessary timestamp joins; this returns no authorization."""
    import math

    lower, upper = execution_window["started_at"], execution_window["finished_at"]
    started, completed = receipt["started_at"], receipt["completed_at"]
    _need(
        all(
            type(value) in (int, float) and math.isfinite(value)
            for value in (lower, upper, started, completed)
        )
        and lower <= started <= completed <= upper
        and receipt["run_id"] == result["run_id"] == proof["run_id"]
        and receipt["profile_id"] == proof["expected"]["profile_id"],
        "original provider completion outside authenticated native execution",
    )
    return completed


def retained_original_facts(context, ref):
    """Read-only original proofs, not renewal or issuance of a SourceSelection."""
    return _read_local_facts(context, ref, require_current_deadline=False)


def _read_local_facts(context, ref, *, require_current_deadline):
    from datetime import datetime

    import closer_correction_bridge as bridge
    import closer_correction_decision as decisions
    import closer_correction_owner as owner

    prep, snapshot, packet, root = _prepared(context)
    _need(
        context["exception"].get("correction", {}).get("source_decision_ref") == ref,
        "current exception source selection differs",
    )
    # Existing controlled receipt reader enforces physical no-follow/hash/bounds.
    import closer_dispatcher as closer

    record = closer._read_controlled_receipt(ref, root)
    _need(
        record.get("schema") == SCHEMA
        and record.get("issuer_source_sha256") == _source_sha()
        and record.get("anchor") == str(root)
        and record.get("original_frame", {}).get("state_path") == context["state_path"],
        "protected original source-record custody differs",
    )
    intent = closer._read_controlled_receipt(record["intent_ref"], root)
    link = closer._read_controlled_receipt(record["result_link_ref"], root)
    result = closer._read_controlled_receipt(record["native_result_ref"], root)
    _need(
        link.get("intent_sha256") == record["intent_ref"]["sha256"]
        and link.get("result_ref") == record["native_result_ref"]
        and record["identity"] == intent["identity"]
        and record["original_frame"] == intent["original_frame"],
        "original intent/result/source record joins differ",
    )
    original = intent["original_request"]
    if require_current_deadline:
        _need(
            time.time()
            < datetime.fromisoformat(
                original["original_operation"]["deadline"].replace("Z", "+00:00")
            ).timestamp(),
            "original declared correction deadline expired; owner escalation required",
        )
    original_state, _raw = owner._physical_json(record["original_frame"]["state_path"])
    frame = record["original_frame"]
    original_entry = original_state["frontiers"][frame["frontier"]["frontier_id"]]
    original_row = original_entry["items"][snapshot["target"]]
    old_results = original_row["results"]
    old_preparation = (old_results.get("interpret") or {}).get("recovered_prepare") or old_results[
        "prepare"
    ]
    _need(
        original_entry["finite_frontier"] == frame["frontier"]
        and original_entry["run_id"] == frame["pipeline_run_id"]
        and original_row["stage_results"]["exception"]["status"] == "completed"
        and original_row["stage_results"]["exception"]["work_key"] == frame["work_key"]
        and old_results["exception"]["actual"] == intent["strong_actual"]
        and old_results["exception"]["correction"].get("source_decision_ref") == ref
        and old_results["exception"]["correction"].get("original_result_ref")
        == record["native_result_ref"]
        and old_preparation["snapshot_ref"] == record["snapshot_ref"]
        and old_preparation["packet_ref"] == record["packet_ref"],
        "physical completed original exception/decision differs",
    )
    arguments = materializer_arguments(intent, result)
    materialized = (
        decisions.materialize_selected(**arguments)
        if require_current_deadline
        else decisions.read_materialized_selected(**arguments)
    )
    _need(
        materialized.get("status") == "materialized"
        and bridge.contract.digest(materialized["decision"]) == record["classifier_decision_sha256"]
        and materialized["actual"]["run_id"] == record["classifier_run_id"],
        "current original classifier proof unavailable",
    )
    _ordinary_selected(materialized, original)
    return prep, snapshot, packet, root, record, intent, result, arguments, materialized


def _facts(selection, controller, operation):
    import closer_queue

    seal = selection._issuer if type(selection) is SourceSelection else None
    _need(
        type(seal) is _SelectionSeal
        and seal.owner is not None
        and seal.owner() is selection
        and seal.pid == os.getpid()
        and seal.thread == threading.get_ident()
        and seal.operation_sha256
        == hashlib.sha256(closer_queue._canonical_bytes(operation)).hexdigest(),
        "source selection issuer missing or belongs to another native invocation",
    )
    import closer_correction_bridge as bridge
    import closer_correction_controller as controller_source
    import closer_correction_ownership as ownership
    import closer_dispatcher as closer

    installation = selection._installation
    _installed_current(installation)
    _need(
        type(controller) is controller_source.IndexController
        and controller.config.source_binding() == installation.config,
        "source controller installation differs",
    )
    projection = installation.policy_projection
    _need(
        operation.get("binding", {}).get("correction_controller_sha256")
        == projection["controller_sha256"]
        and operation.get("binding", {}).get("correction_installation_sha256")
        == projection["installation_sha256"],
        "original native operation controller policy projection differs",
    )
    controller.live(operation)
    context = json.loads(selection._pipeline_context_json)
    current = _current_act(context, operation, installation)
    prep, snapshot, packet, root = _prepared(context)
    native_binding = operation.get("binding") or {}
    _need(
        all(
            native_binding.get(key) == snapshot.get(key)
            for key in ("head", "merge", "review_revision")
        )
        and native_binding.get("pr_target") == snapshot.get("pr_target", snapshot["target"])
        and native_binding.get("source_targets")
        == [row["target"] for row in snapshot.get("sources", [])],
        "native operation current source/head/review binding differs",
    )
    ref = json.loads(selection._record_ref_json)
    _need(
        context["exception"].get("correction", {}).get("source_decision_ref") == ref,
        "original current exception does not select this source record",
    )
    prep, snapshot, packet, root, record, intent, result, arguments, materialized = _local_facts(
        context, ref
    )
    original = intent["original_request"]
    seed, tests, inputs = _qualified_owner_projection(context, current, snapshot, packet, root)

    def native_owner_transport(argv, **options):
        prefix = "repos/" + controller.config.repository
        _need(
            isinstance(argv, list)
            and len(argv) == 4
            and argv[:3] == ["api", "--method", "GET"]
            and isinstance(argv[3], str)
            and argv[3].startswith(prefix + "/")
            and not set(options) - {"resource", "timeout_s"},
            "source owner read outside original native API protocol",
        )
        controller.live(operation)
        path = argv[3][len(prefix) :]
        answer = controller.store.call("GET", path)
        controller.live(operation)
        effects = controller._native_effect_source()
        return 0, effects.project_retained_issue_ownership(
            controller,
            getattr(controller._writer_context, "custody", None),
            operation,
            original,
            path,
            answer,
        )

    native_reader = closer.GitHub(
        deadline=min(operation["deadline"], context["deadline"]), api_fn=native_owner_transport
    )
    # The original bot comment is a native observation, not a retained JSON
    # claim. An edited/deleted/foreign comment cannot keep publishing authority.
    comment_id = original["original_verifier"]["comment_id"]
    comment = controller.store.call("GET", "/issues/comments/" + str(comment_id))
    _need(
        str(comment.get("id")) == str(comment_id)
        and controller_source.native_api_url(
            comment.get("issue_url"),
            controller.config.repository,
            "/issues/" + original["target"]["pr"],
        )
        and comment.get("user", {}).get("login") == "github-actions[bot]"
        and comment.get("user", {}).get("type") == "Bot"
        and isinstance(comment.get("body"), str)
        and hashlib.sha256(comment["body"].encode()).hexdigest()
        == original["original_verifier"]["comment_sha256"],
        "original native verifier comment changed",
    )
    controller.live(operation)
    observed = ownership.collect_ownership(
        github=native_reader,
        seed=seed,
        deadline=min(operation["deadline"], context["deadline"]),
        now=time.time(),
    ).document()

    observed_source_context, observed_source_native = _issue_context(
        native_reader,
        controller.config.repository,
        original["source_issue"],
        original["required_tests"],
        native=True,
    )
    observed_incumbent_context = (
        None
        if original["incumbent"].get("kind") == "no_incumbent"
        else _issue_context(
            native_reader,
            controller.config.repository,
            original["incumbent"]["issue"],
            original["required_tests"],
            assigned=True,
        )
    )
    original_source_context = record["original_source_context"]
    original_incumbent_context = record["original_incumbent_context"]
    _need(
        {k: v for k, v in original_source_context.items() if k != "updated_at"}
        == {k: v for k, v in observed_source_context.items() if k != "updated_at"}
        and original_incumbent_context == observed_incumbent_context,
        "original source body/tests or incumbent version/ownership changed",
    )
    semantic = {
        "schema": original["schema"],
        **{
            key: seed[key]
            for key in ("target", "source_issue", "original_verifier", "finding", "scope")
        },
        "required_tests": tests,
        "incumbent": observed["incumbent"],
    }
    _need(
        bridge._owner_semantic_digest(semantic, inputs)
        == record["owner_semantics_sha256"]
        == bridge._owner_semantic_digest(original, intent["inputs"]),
        "current source criterion/test/owner semantics changed",
    )
    # Derive the archive/member selector only from the original collector row.
    artifacts = packet["sections"]["native_merge_verifier"]["content"]["artifacts"]
    candidates = [
        (artifact, receipt)
        for artifact in artifacts
        for receipt in artifact.get("receipts", [])
        if receipt.get("kind") == "native-correction-seeds"
        and str(artifact.get("run_id")) == original["original_verifier"]["run_id"]
        and str(artifact.get("run_attempt")) == original["original_verifier"]["run_attempt"]
    ]
    _need(len(candidates) == 1, "original native seed selector ambiguous")
    artifact, receipt = candidates[0]
    import closer_disposition
    import closer_evidence

    _need(
        closer_disposition._safe_raw(
            artifact["archive"], root, max_bytes=closer_evidence.MAX_ARCHIVE_BYTES
        )
        is not None,
        "original native seed archive unavailable or changed",
    )
    seed_selector = {
        "repository": original["target"]["repository"],
        "run_id": int(artifact["run_id"]),
        "run_attempt": int(artifact["run_attempt"]),
        "artifact_id": artifact["id"],
        "archive_sha256": artifact["archive"]["sha256"],
        "member_sha256": receipt["raw"]["sha256"],
    }
    controller_source.seed_selector(seed_selector, controller.config.repository)
    _current_act(context, operation, installation)
    controller.live(operation)
    return {
        "request": original,
        "raw_findings": intent["inputs"]["raw_findings"],
        "seed_selector": seed_selector,
        "materializer": arguments,
        "original_source_context": original_source_context,
        "original_source_native": record["original_source_native"],
        "original_incumbent_context": original_incumbent_context,
        "observed_source_native": observed_source_native,
        "observed_source_context": observed_source_context,
        "observed_incumbent_context": observed_incumbent_context,
        "original_frame": record["original_frame"],
        "native_ownership": observed,
        "source_record_ref": ref,
        "decision": materialized["decision"],
    }


def select_for_native_effect(*, candidate, controller, operation):
    """Mint only inside the actual source-selected native disposition callback."""
    _candidate_context(candidate)
    native_seal = _SelectionSeal(operation)
    selected = SourceSelection(
        candidate._record_ref_json,
        candidate._pipeline_context_json,
        candidate._installation,
        _issuer=native_seal,
    )
    native_seal.owner = weakref.ref(selected)
    _facts(selected, controller, operation)
    return selected


def _candidate_context(candidate):
    seal = candidate._issuer if type(candidate) is SourceCandidate else None
    _need(
        type(seal) is _CandidateSeal
        and seal.owner is not None
        and seal.owner() is candidate
        and seal.pid == os.getpid(),
        "source candidate issuer missing",
    )
    return json.loads(candidate._pipeline_context_json)


def validate_for_native_effect(selection, controller, operation):
    """Fixed source API; no passed validator, JSON flag or selfhash grants effects."""
    return _facts(selection, controller, operation)


# Diagnostic vocabulary only. These fields never grant execution or replay.
_FAILURE_STAGES = frozenset(
    "candidate_context source_installation current_act controller_setup writer_enter capability "
    "source_selection instruction_publication source_enrollment issue_creation enrollment_retention "
    "writer_exit issue_source_selection issue_enrolled_read issue_plan_capture issue_plan_persist "
    "issue_prepare_register issue_prepare_apply issue_prepared_read issue_plan_revalidate "
    "issue_remaining_before_post issue_post issue_response_validate issue_transition "
    "issue_transition_read issue_source_revalidate issue_origin_persist issue_confirm issue_replay unknown".split()
)
_FAILURE_CODES = {
    "scoped original queue operation changed or expired": "original_callback_unavailable",
    "original same-thread writer validation scope unavailable": "original_writer_unavailable",
    "full original scoped source validation failed": "full_source_validation_unavailable",
    "actual factory-issued signed source installation required": "source_installation_unavailable",
    "original native effect deadline expired": "native_effect_deadline_expired",
    "original native effect limits unavailable": "native_effect_limits_unavailable",
    "original native phase plan owner/scope changed": "native_plan_owner_changed",
    "native issue phase is not source/index prepared": "native_issue_not_prepared",
    "issue prepare index outcome unknown": "native_issue_prepare_unknown",
    "original issue stage unresolved; never repeat POST": "native_issue_unresolved_no_repeat",
    "round deadline exhausted during read-only GitHub access": "read_only_deadline_exhausted",
    "original same-thread queue callback required": "original_callback_unavailable",
    "original operation does not select this controller": "original_controller_unselected",
    "conditional outcome unknown or competing": "conditional_readback_unknown",
    "conditional write absent/competing; never repeat": "conditional_write_unconfirmed",
    "native expected commit stale": "native_write_expected_commit_stale",
    "native write acknowledgement unknown": "native_write_ack_unknown",
    "checkpoint changed before lock release": "checkpoint_readback_changed",
    "native index differs from independent current high-water": "native_index_highwater_mismatch",
    "unresolved original intent; reconcile only": "original_intent_unresolved",
    "result lost its high-water; reconcile only": "highwater_result_unresolved",
    "original write high-water changed": "original_highwater_changed",
}
_FAILURE_EXCEPTIONS = frozenset(
    "ImportError OSError ValueError TypeError KeyError RuntimeError AttributeError StopIteration SourceRecordUnavailable ControllerUnknown TimeoutExpired".split()
)


def _failure_diagnostic(error, stage):
    # Never serialize exception text, arbitrary class names, paths or arguments.
    kind = type(error).__name__
    reason = error.args[0] if len(error.args) == 1 and type(error.args[0]) is str else None
    code = _FAILURE_CODES.get(reason, "unclassified_error")
    if isinstance(error, subprocess.TimeoutExpired):
        # This constant identifies the observer's existing pre-Popen guard.
        # It is diagnostic data, never proof of terminality or replay safety.
        code = (
            "native_transport_prelaunch_timeout"
            if type(error.cmd) is str and error.cmd == "source transport before spawn"
            else "native_transport_timeout"
        )
    return {
        "stage": stage if type(stage) is str and stage in _FAILURE_STAGES else "unknown",
        "code": code,
        "exception": kind if kind in _FAILURE_EXCEPTIONS else "OtherError",
    }


def _bounded_failure_diagnostic(value):
    if (
        type(value) is dict
        and set(value) == {"stage", "code", "exception"}
        and type(value["stage"]) is str
        and value["stage"] in _FAILURE_STAGES
        and type(value["code"]) is str
        and value["code"]
        in {
            *_FAILURE_CODES.values(),
            "unclassified_error",
            "native_transport_timeout",
            "native_transport_prelaunch_timeout",
        }
        and type(value["exception"]) is str
        and value["exception"] in {*_FAILURE_EXCEPTIONS, "OtherError"}
    ):
        return dict(value)
    return None


def execute_candidate(candidate, operation):
    """Use the existing native disposition callback, without invoking a model.

    A source decision does not itself authorize an effect. The installed
    controller still checks the actual queue callback, fixed shared custody,
    source/native ownership and protected native acknowledgment under its lock.
    Any exception retains the submitted operation as uncertain; it never grants
    a known-not-started exemption or a retry of an uncertain publication.
    """
    import closer_queue as queue

    unknown = {
        "status": "uncertain",
        "reason": "correction native publication remains owned; reconcile original operation",
        "outcome_uncertain": True,
        "retry_safe": False,
        "target_complete": False,
        "act_authorized": False,
        "source_closure_authorized": False,
    }
    stage = "candidate_context"
    first_failure = None
    try:
        context = _candidate_context(candidate)
        target = context["prepare"]["snapshot"]["target"]
        _need(
            queue._currently_invoking(target, operation),
            "actual submitted native callback required",
        )
        installation = candidate._installation
        stage = "source_installation"
        _installed_current(installation)
        stage = "current_act"
        _current_act(context, operation, installation)
        import closer_correction_controller as controller_source
        import closer_correction_native_effects as native_effects

        stage = "controller_setup"
        controller = controller_source.IndexController(
            controller_source.ControllerConfig(**installation.config), installation=installation
        )
        # A missing native integration is detected before its first publication.
        # There is no dynamic caller-supplied publisher or validator port here.
        _need(
            callable(getattr(type(controller), "enroll_source_selection_locked", None)),
            "installed source enrollment owner unavailable",
        )
        stage = "writer_enter"
        with controller.fenced(operation) as custody:
            import closer_coding_continuation

            stage = "capability"
            capability = closer_coding_continuation.selected_capability(
                controller, custody, operation
            )
            stage = "source_selection"
            selection = select_for_native_effect(
                candidate=candidate, controller=controller, operation=operation
            )
            stage = "instruction_publication"
            published = native_effects.publish_first_instruction_locked(
                controller, custody, operation, selection
            )
            if published.get("status") != "native_instruction_published":
                stage = "writer_exit"
                return {**unknown, "publication": published}
            stage = "source_enrollment"
            enrolled = controller.enroll_source_selection_locked(
                custody, operation, selection, published
            )
            if enrolled.get("status") not in {
                "checkpoint_confirmed",
                "checkpoint_reconciled_only",
            }:
                stage = "writer_exit"
                return {**unknown, "publication": published, "enrollment": enrolled}
            stage = "issue_creation"
            try:
                issue = native_effects.create_source_issue_locked(
                    controller, custody, operation, selection
                )
            except (
                ImportError,
                OSError,
                ValueError,
                TypeError,
                KeyError,
                RuntimeError,
                AttributeError,
                subprocess.TimeoutExpired,
            ) as error:
                # Preserve the source entry failure even if fence exit raises too.
                first_failure = _failure_diagnostic(error, stage)
                stage = "writer_exit"
                raise
            if issue.get("status") not in {"native_effect_confirmed", "native_issue_not_required"}:
                first_failure = _bounded_failure_diagnostic(issue.get("failure_diagnostic"))
                stage = "writer_exit"
                return {
                    **unknown,
                    "publication": published,
                    "enrollment": enrolled,
                    "issue_creation": issue,
                }
            terminal = None
            if capability is not None:
                import closer_correction_enrollment_terminal as enrollment_terminal

                stage = "enrollment_retention"
                terminal = enrollment_terminal.retain_locked(
                    controller,
                    custody,
                    operation,
                    selection,
                    published=published,
                    enrolled=enrolled,
                    issue=issue,
                )
            coding_reference = None
            coding_registration_held = False
            if terminal is not None:
                # Coding-only registration cannot erase an already known
                # original source enrollment. Fatal process exits propagate.
                try:
                    import closer_coding_continuation as coding_continuation

                    coding_reference = coding_continuation.retain_after_confirmation_locked(
                        controller, custody, operation, selection, terminal
                    )
                except Exception:
                    coding_registration_held = True
            stage = "writer_exit"
            return {
                "status": "completed",
                "advanced": True,
                "reason": "original source instruction and native correction checkpoint confirmed",
                "issue_creation": issue,
                "target_complete": False,
                "retry_safe": False,
                "act_authorized": False,
                "source_closure_authorized": False,
                "publication": published,
                "enrollment": enrolled,
                **({"correction_enrollment_terminal": terminal} if terminal is not None else {}),
                **(
                    {
                        "coding_continuation": {
                            "status": "awaiting_original_terminal",
                            "reference": coding_reference,
                            "execution_authorized": False,
                            "retry_authorized": False,
                        }
                    }
                    if coding_reference is not None
                    else {}
                ),
                **(
                    {
                        "coding_continuation": {
                            "status": "held",
                            "reason": "coding_registration_unavailable",
                            "execution_authorized": False,
                            "retry_authorized": False,
                        }
                    }
                    if coding_registration_held
                    else {}
                ),
            }
    except (
        ImportError,
        OSError,
        ValueError,
        TypeError,
        KeyError,
        RuntimeError,
        AttributeError,
        subprocess.TimeoutExpired,
    ) as error:
        diagnostic = _failure_diagnostic(error, stage)
        return {
            **unknown,
            "failure_type": diagnostic["exception"],
            "failure_diagnostic": diagnostic,
            **({"first_failure_diagnostic": first_failure} if first_failure is not None else {}),
        }
