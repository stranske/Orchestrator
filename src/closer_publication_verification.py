"""Confirmed publication joins the existing queue; it grants no ACT authority.

The publisher alone issues/readbacks the completion object. This module retains
its exact original scope and supplies wrappers for the existing queue callbacks.
It never starts a queue, creates a frontier, requests verification directly, or
replaces the generic verification/merge/closure actors.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import weakref
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

import closer_coding_publication as publication
import closer_correction_controller as controller_contract
import closer_correction_handoff as wire
import closer_correction_native_effects as native_effects
import closer_corrective_verification_instruction as instruction
import closer_dispatcher as native
import closer_evidence
import closer_queue as queue
import verification_events

_LOADED_SOURCE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
SCHEMA = "closer-published-pr-continuation/v1"
RESULT_SCHEMA = "closer-confirmed-existing-pr-publication/v1"
_RESULT_KEYS = {
    "schema",
    "request",
    "original",
    "findings",
    "required_tests",
    "pr",
    "commit",
    "ref",
    "provenance",
    "publication_result_reference",
    *publication._AUTHORITY,
}
_PROVENANCE_KEYS = {
    "handoff_reference",
    "coding_terminal_reference",
    "publication_origin_reference",
    "publication_result_reference",
    "io_admission_reference",
    "io_operation_sha256",
    "push_ack_sha256",
    "native_readback_sha256",
    "index_binding_sha256",
    "original_admission_sha256",
    "original_operation_sha256",
    "source_closure_sha256",
    "scope_issue",
}
_REFERENCE_SCHEMAS = {
    "handoff_reference": "closer-publication-source-handoff-reference/v1",
    "coding_terminal_reference": "closer-coding-worker-terminal-reference/v1",
    "publication_origin_reference": "closer-native-owned-pr-publication-origin-reference/v1",
    "publication_result_reference": "closer-owned-pr-publication-result-reference/v1",
    "io_admission_reference": "closer-publication-io-admission-reference/v1",
}
_RECORDS: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


class ContinuationHeld(ValueError):
    pass


class EvaluatorScopeHeld(ContinuationHeld):
    pass


def _need(ok, reason):
    if not ok:
        raise ContinuationHeld(reason)


def _exact(value, keys):
    _need(type(value) is dict and set(value) == set(keys), "publication continuation fields differ")


def _sha(value, length=64):
    return type(value) is str and re.fullmatch(r"[a-f0-9]{" + str(length) + "}", value) is not None


def _reference(value):
    _exact(value, {"schema", "name", "sha256"})
    _need(
        type(value["schema"]) is str
        and bool(value["schema"])
        and type(value["name"]) is str
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,255}\.json", value["name"])
        and _sha(value["sha256"]),
        "protected publication reference missing",
    )


def _validate_result(value, controller):
    _exact(value, _RESULT_KEYS)
    _need(value["schema"] == RESULT_SCHEMA, "confirmed publication schema differs")
    _need(
        all(value[k] is False for k in publication._AUTHORITY),
        "publication is not action authority",
    )
    request = wire.validate_request(value["request"])
    _need(
        request["incumbent"].get("kind") != "no_incumbent",
        "existing incumbent publication required",
    )
    original, pr, commit, ref, provenance = (
        value[k] for k in ("original", "pr", "commit", "ref", "provenance")
    )
    publication.stable_slot(original)
    publication.validate_owned_pr(pr)  # The original confirmed OPEN readback.
    _need(
        original["repository_id"] == pr["repository_id"] == controller.config.repository_id
        and original["epoch"] == controller.config.epoch
        and original["effect_key"] == controller_contract.effect_key(request)
        and original["request_sha256"] == controller_contract.digest(request)
        and original["consumer_id"] in controller.config.consumers
        and pr["repository"] == request["target"]["repository"] == controller.config.repository
        and value["required_tests"] == request["required_tests"],
        "original publication scope/owner/tests differ",
    )
    findings = value["findings"]
    _need(
        type(findings) is list
        and findings
        and all(type(row) is dict for row in findings)
        and any(
            row.get("id") == request["finding"]["id"]
            and row.get("evidence") == request["finding"]["evidence"]
            for row in findings
        ),
        "original publication findings missing",
    )
    _exact(commit, {"repository_id", "oid", "tree_oid", "parent_oids"})
    _exact(ref, {"repository_id", "ref", "oid", "object_type"})
    _need(
        commit["repository_id"] == ref["repository_id"] == pr["repository_id"]
        and commit["oid"] == ref["oid"] == pr["head_oid"]
        and _sha(commit["oid"], 40)
        and _sha(commit["tree_oid"], 40)
        and type(commit["parent_oids"]) is list
        and len(commit["parent_oids"]) == 1
        and _sha(commit["parent_oids"][0], 40)
        and commit["parent_oids"][0] != commit["oid"]
        and ref["ref"] == "refs/heads/" + pr["head_ref"]
        and ref["object_type"] == "commit",
        "native publication head/ref/commit differ",
    )
    _exact(provenance, _PROVENANCE_KEYS)
    for key in _PROVENANCE_KEYS - {"scope_issue"}:
        if key.endswith("_reference"):
            _reference(provenance[key])
            _need(
                provenance[key]["schema"] == _REFERENCE_SCHEMAS[key],
                "protected publication reference schema differs",
            )
        else:
            _need(_sha(provenance[key]), "publication provenance digest missing")
    _need(
        provenance["coding_terminal_reference"]["schema"]
        == "closer-coding-worker-terminal-reference/v1",
        "known original coding terminal reference required",
    )
    for key, prefix in (
        ("publication_origin_reference", "native-publication-origin-"),
        ("io_admission_reference", "publication-io-admission-"),
    ):
        _need(
            provenance[key]["name"] == prefix + provenance[key]["sha256"] + ".json",
            "protected publication reference name differs",
        )
    result_ref = value["publication_result_reference"]
    _reference(result_ref)
    _need(
        result_ref["schema"] == "closer-owned-pr-publication-result-reference/v1"
        and result_ref["name"] == "publication-result-" + result_ref["sha256"] + ".json"
        and result_ref == provenance["publication_result_reference"],
        "publication result reference differs",
    )
    _need(
        provenance["scope_issue"] == request["incumbent"]["issue"]
        and provenance["scope_issue"]["node_id"] == pr["issue_node_id"],
        "existing incumbent scope selector differs",
    )
    return copy.deepcopy(value)


def _capture_instruction(record, custody):
    """Only fixed custody names from the authenticated publication source result.

    No reference inside the footer/findings is opened. The prior source reader
    already authenticated this admission/index/native-origin digest, and the
    same live custody object is used here to preserve its original instruction.
    """
    key = record["provenance"]["original_admission_sha256"]
    admission = custody.read("admission-" + key + ".json")
    _need(
        controller_contract.digest(admission) == key
        and admission.get("schema") == "local-correction-admission/v1"
        and admission.get("request") == record["request"]
        and (admission.get("materializer") or {}).get("raw_findings") == record["findings"],
        "protected publication instruction admission differs",
    )
    incumbent = admission.get("original_incumbent_context")
    _need(type(incumbent) is dict, "protected original incumbent context missing")
    assignees = incumbent.get("assignees")
    _need(
        type(assignees) is list
        and len(assignees) == 1
        and type(assignees[0]) is dict
        and type(assignees[0].get("id")) is int
        and assignees[0]["id"] > 0
        and assignees[0].get("type") in {"User", "Bot"}
        and type(assignees[0].get("login")) is str
        and bool(assignees[0]["login"]),
        "protected original incumbent assignee differs",
    )
    native_assignee = {k: assignees[0][k] for k in ("id", "login", "type")}
    selected = {
        "kind": "local_controller_record",
        "record_sha256": key,
        "seed_selector": admission["seed_selector"],
        "owner_instruction": admission["owner_instruction"],
    }
    controller_contract.selector(selected, record["pr"]["repository"])
    owner = selected["owner_instruction"]
    _need(
        owner["issue_number"] == record["request"]["source_issue"]["number"]
        and owner["issue_node_id"] == record["request"]["source_issue"]["node_id"]
        and owner["writer_id"] == record["pr"]["writer_id"] == record["pr"]["owner_id"],
        "native source instruction parent/writer differs",
    )
    own = admission.get("own_instruction")
    _exact(own, {"proof_sha256", "phase_key"})
    _need(all(_sha(v) for v in own.values()), "native instruction origin reference missing")
    intent = custody.read("native-intent-" + own["phase_key"] + ".json")
    origin = custody.read("native-origin-" + own["proof_sha256"] + ".json")
    _need(
        controller_contract.digest(origin) == own["proof_sha256"]
        and origin.get("schema") == "local-correction-native-origin/v1"
        and origin.get("phase_key") == own["phase_key"] == intent.get("phase_key")
        and origin.get("intent_sha256") == controller_contract.digest(intent)
        and (origin.get("native") or {}).get("owner_instruction") == owner
        and intent.get("request") == record["request"]
        and intent.get("phase") == "instruction_publication",
        "protected native instruction origin differs",
    )
    _exact(intent["payload"], {"body"})
    body = intent["payload"]["body"]
    _need(
        type(body) is str and hashlib.sha256(body.encode()).hexdigest() == owner["body_sha256"],
        "protected native instruction body differs",
    )
    if instruction.PREFIX not in body:
        return {
            "owner_instruction": copy.deepcopy(owner),
            "incumbent_assignee": native_assignee,
            "payload": None,
        }  # Evidence-only legacy.
    parsed = instruction.parse(body, max_bytes=native_effects.MAX_PAYLOAD)["payload"]
    _need(
        parsed["original_request"] == record["request"]
        and parsed["raw_findings"] == record["findings"]
        and parsed["required_tests_sha256"] == wire.digest(record["required_tests"]),
        "native instruction original scope differs",
    )
    return {
        "owner_instruction": copy.deepcopy(owner),
        "incumbent_assignee": native_assignee,
        "payload": parsed,
    }


class _Continuation:
    __slots__ = ("__weakref__",)


def from_published(controller, custody, operation, confirmed_result):
    """Read the source-issued opaque result while the original writer is live."""
    reader = getattr(publication, "read_confirmed_result", None)
    _need(callable(reader), "confirmed publication source reader unavailable")
    value = reader(controller, custody, operation, confirmed_result)
    record = _validate_result(value, controller)
    record["_instruction"] = _capture_instruction(record, custody)
    continuation = _Continuation()
    _RECORDS[continuation] = record
    return continuation


def _publication_configuration_selected(binding):
    """Absence selects no feature; malformed/selected configuration stays held."""
    if binding is None:
        return False
    from closer_runtime_binding import RuntimeBinding

    if type(binding) is not RuntimeBinding or type(binding.policy) is not dict:
        return True
    if "correction_controller_installation" not in binding.policy:
        return False
    field = binding.policy["correction_controller_installation"]
    if type(field) is not dict or type(field.get("config")) is not dict:
        return True
    consumers = field["config"].get("consumers")
    if type(consumers) is not dict:
        return True
    return any(type(row) is not dict or "publication_v1" in row for row in consumers.values())


def attach_existing_root(backend, target, snapshot, operation, pipeline_context):
    """Reissue knowledge only inside this NEW admitted root's actual callback.

    The source reader and its opaque view independently verify this loaded
    function, execute_bound caller, physical completed result/index/native head
    and current queue thread. Neither serialized coordinator data nor the old
    publication capability can reconstruct this knowledge.
    """
    _need(type(backend) is native.Closer, "existing native Closer backend required")
    _need(queue._currently_invoking(target, operation), "fresh admitted queue callback required")
    selected_context = (
        getattr(backend, "_publication_verification_context", None) is not None
        or (pipeline_context.get("prepare") or {}).get("corrective_publication_hint") is not None
        or (snapshot.get("verifier") or {}).get("corrective_verification_input") is not None
    )
    if not selected_context and not _publication_configuration_selected(backend.runtime_binding):
        return None  # Existing ordinary ACT guards remain responsible for this subject.
    reader = getattr(publication, "read_for_existing_root", None)
    _need(callable(reader), "admitted publication source reader unavailable")
    view = reader(backend, target, snapshot, operation, pipeline_context)
    if view is None:
        _need(
            getattr(backend, "_publication_verification_context", None) is None,
            "bound corrective subject cannot become unrelated",
        )
        return None  # Only the source reader authenticates an unrelated subject.
    _need(
        not isinstance(view, dict) and callable(getattr(view, "read", None)),
        "source-owned admitted publication view required",
    )
    projected = view.read()  # Direct call is itself checked by the source-owned view.
    _exact(
        projected,
        {
            "schema",
            "result",
            "controller_config",
            "original_admission",
            "instruction_intent",
            "instruction_origin",
        },
    )
    _need(
        projected["schema"] == "closer-admitted-publication-knowledge/v1",
        "admitted publication knowledge schema differs",
    )
    config = projected["controller_config"]
    _exact(config, {"repository", "repository_id", "epoch", "consumers"})
    _need(
        type(config["consumers"]) is list
        and all(type(item) is str and item for item in config["consumers"])
        and config["consumers"] == sorted(set(config["consumers"])),
        "admitted publication consumer identities differ",
    )
    record = _validate_result(
        projected["result"], SimpleNamespace(config=SimpleNamespace(**config))
    )
    admission = projected["original_admission"]
    _need(
        type(admission) is dict and type(admission.get("own_instruction")) is dict,
        "protected admitted instruction missing",
    )
    own = admission["own_instruction"]
    records = {
        "admission-" + record["provenance"]["original_admission_sha256"] + ".json": admission,
        "native-intent-" + str(own.get("phase_key")) + ".json": projected["instruction_intent"],
        "native-origin-" + str(own.get("proof_sha256")) + ".json": projected["instruction_origin"],
    }
    # Fixed local DATA view only; no custody capability or arbitrary reference reads.
    record["_instruction"] = _capture_instruction(
        record, SimpleNamespace(read=lambda name: copy.deepcopy(records[name]))
    )
    _need(
        snapshot.get("target") == target and snapshot.get("head") == record["pr"]["head_oid"],
        "new root publication snapshot differs",
    )
    _need(
        _fresh(backend, target, record).get("revision") == snapshot.get("revision"),
        "new root publication observation changed",
    )
    _need(
        queue._currently_invoking(target, operation),
        "new root callback expired during source readback",
    )
    previous = getattr(backend, "_publication_verification_context", None)
    if previous is not None:
        _need(_record(previous) == record, "new root corrective binding changed")
    continuation = _Continuation()
    _RECORDS[continuation] = record
    backend._publication_verification_context = continuation
    if operation.get("action") in {"close_source", "complete"}:
        require_evaluated_scope(continuation, snapshot, backend=backend)
    return continuation


def _record(continuation):
    _need(
        type(continuation) is _Continuation and continuation in _RECORDS,
        "source continuation object required",
    )
    return copy.deepcopy(_RECORDS[continuation])


def for_coordinator(continuation):
    """Non-authorizing selection knowledge. Copies cannot reconstruct a capability."""
    record = _record(continuation)
    return {
        "schema": SCHEMA,
        "target": record["pr"]["repository"] + "#" + str(record["pr"]["pr_number"]),
        "head": record["pr"]["head_oid"],
        "request": record["request"],
        "findings": record["findings"],
        "required_tests": record["required_tests"],
        "publication_result_reference": record["publication_result_reference"],
        "next_trigger": "existing guarded PR queue observes current-head CI and source",
        "corrective_publication_hint": selection_hint(continuation),
        **{k: False for k in publication._AUTHORITY},
    }


def selection_hint(continuation):
    """Bounded planning DATA for fresh discovery; never an execution permit."""
    record = _record(continuation)
    return {
        "schema": "closer-corrective-publication-selection/v1",
        "publication_result_reference": record["publication_result_reference"],
        "request_sha256": record["request"]["request_sha256"],
        "required_tests_sha256": wire.digest(record["required_tests"]),
    }


def observe_existing_root(backend, target, pipeline_context):
    """Only plan; execution reauthenticates durable source in execute_bound."""
    observation = backend.observe(target)
    supplied = (pipeline_context.get("prepare") or {}).get("corrective_publication_hint")
    discover = getattr(publication, "discovery_hint", None)
    discovered = discover(backend, target, pipeline_context) if callable(discover) else None
    _need(
        supplied is None or supplied == discovered,
        "corrective planning hint differs from protected discovery",
    )
    hint = discovered
    if hint is None:
        return observation  # No corrective source result; ordinary route remains unchanged.
    _exact(
        hint, {"schema", "publication_result_reference", "request_sha256", "required_tests_sha256"}
    )
    _need(
        hint["schema"] == "closer-corrective-publication-selection/v1"
        and _sha(hint["request_sha256"])
        and _sha(hint["required_tests_sha256"]),
        "corrective planning hint fields differ",
    )
    _reference(hint["publication_result_reference"])
    ref = hint["publication_result_reference"]
    _need(
        ref["schema"] == "closer-owned-pr-publication-result-reference/v1"
        and ref["name"] == "publication-result-" + ref["sha256"] + ".json",
        "corrective planning result reference differs",
    )
    snapshot = observation.get("evidence") or {}
    if (
        observation.get("action") not in {"close_source", "complete"}
        or snapshot.get("state") != "MERGED"
        or snapshot.get("kind") != "pr"
    ):
        return observation  # Existing holds, pending/unknown, CI and resource selection win.
    if backend._unacknowledged_verification_request(
        snapshot
    ) is not None or backend._accepted_verification_request(snapshot):
        return {
            **observation,
            "action": "wait",
            "eligible": False,
            "reason": "existing verification request remains fenced",
            "next_trigger": "exact original verification request terminal reconciliation",
        }
    import closer_corrective_verifier_readback as readback

    repo, number = target.split("#")
    _, terminal = readback._selected_comment(
        backend,
        {"pr": {"repository": repo, "pr_number": int(number), "head_oid": snapshot.get("head")}},
        snapshot,
    )
    event = terminal.get("verification_event")
    _need(
        verification_events.valid_event(
            event, repo, int(number), snapshot.get("head"), snapshot.get("merge")
        ),
        "additional corrective material requires genuine same-PR settled event",
    )
    compact = terminal.get("corrective_verification_input")
    if type(compact) is dict:
        _exact(compact, {"schema", "verification_event", "corrective_instruction"})
        _need(
            compact["schema"] == "closer-corrective-verification-input/v1"
            and compact["verification_event"] == event,
            "settled corrective input metadata unknown",
        )
        row = compact["corrective_instruction"]
        if (
            type(row) is dict
            and row.get("request_sha256") == hint["request_sha256"]
            and row.get("required_tests_sha256") == hint["required_tests_sha256"]
        ):
            return observation  # Actual artifact coverage is still required in execute_bound.
    return {
        **observation,
        "action": "post_merge_verify",
        "eligible": True,
        "reason": "additional protected corrective test scope requires existing verification",
        "next_trigger": None,
    }


def _body(value):
    _need(
        "body" in value and (value["body"] is None or type(value["body"]) is str),
        "native body unavailable",
    )
    return value["body"] or ""


def _fresh(backend, target, record):
    expected, scope = record["pr"], record["provenance"]["scope_issue"]
    wanted = expected["repository"] + "#" + str(expected["pr_number"])
    _need(target == wanted, "publication target differs")
    observation = native.Closer.observe(backend, target)
    snapshot = observation.get("evidence") or {}
    _need(
        snapshot.get("target") == target
        and snapshot.get("kind") == "pr"
        and snapshot.get("head") == expected["head_oid"]
        and snapshot.get("head_branch") == expected["head_ref"]
        and snapshot.get("head_repository") == expected["repository"]
        and snapshot.get("state") in {"OPEN", "MERGED"}
        and snapshot.get("draft") is False
        and not snapshot.get("_local_cached"),
        "fresh published head snapshot unavailable",
    )
    repo = expected["repository"]
    pull = backend.gh.api(f"repos/{repo}/pulls/{expected['pr_number']}", method="GET")
    issue = backend.gh.api(f"repos/{repo}/issues/{scope['number']}", method="GET")
    ref = (
        backend.gh.api(
            f"repos/{repo}/git/ref/heads/{quote(expected['head_ref'], safe='/')}", method="GET"
        )
        if snapshot["state"] == "OPEN"
        else None
    )
    original_source = record["request"]["source_issue"]
    source = (
        issue
        if original_source["number"] == scope["number"]
        else backend.gh.api(f"repos/{repo}/issues/{original_source['number']}", method="GET")
    )
    _need(
        all(type(row) is dict for row in (pull, issue, source))
        and (snapshot["state"] == "MERGED" or type(ref) is dict),
        "native publication read malformed",
    )
    pull_body, issue_body, source_body = _body(pull), _body(issue), _body(source)
    owner_instruction = record["_instruction"]["owner_instruction"]
    comment = backend.gh.api(
        f"repos/{repo}/issues/comments/{owner_instruction['comment_id']}", method="GET"
    )
    _need(
        type(comment) is dict
        and comment.get("id") == owner_instruction["comment_id"]
        and comment.get("node_id") == owner_instruction["comment_node_id"]
        and comment.get("updated_at") == owner_instruction["updated_at"]
        and controller_contract.native_api_url(
            comment.get("issue_url"), repo, "/issues/" + owner_instruction["issue_number"]
        )
        and type(comment.get("body")) is str
        and hashlib.sha256(comment["body"].encode()).hexdigest() == owner_instruction["body_sha256"]
        and (comment.get("user") or {}).get("id")
        == owner_instruction["writer_id"]
        == expected["writer_id"]
        and (comment.get("user") or {}).get("login") == expected["writer_login"],
        "fresh original owner instruction changed",
    )
    _need(
        source.get("number") == int(original_source["number"])
        and source.get("node_id") == original_source["node_id"]
        and "pull_request" not in source
        and hashlib.sha256(source_body.encode()).hexdigest() == original_source["body_sha256"],
        "original source body/identity changed",
    )
    head, base, user = (pull.get(k) or {} for k in ("head", "base", "user"))
    _need(
        pull.get("number") == expected["pr_number"]
        and pull.get("node_id") == expected["pr_node_id"]
        and pull.get("draft") is False
        and (pull.get("state"), pull.get("merged"))
        == (("open", False) if snapshot["state"] == "OPEN" else ("closed", True))
        and head.get("sha") == expected["head_oid"]
        and head.get("ref") == expected["head_ref"]
        and (head.get("repo") or {}).get("id")
        == (base.get("repo") or {}).get("id")
        == expected["repository_id"]
        and base.get("ref") == expected["base_ref"]
        and user.get("id") == expected["owner_id"]
        and user.get("login") == expected["owner_login"]
        and hashlib.sha256(pull_body.encode()).hexdigest() == expected["body_sha256"]
        and (
            (
                snapshot["state"] == "OPEN"
                and ref.get("ref") == "refs/heads/" + expected["head_ref"]
                and (ref.get("object") or {}).get("sha") == expected["head_oid"]
                and (ref.get("object") or {}).get("type") == "commit"
            )
            or (
                snapshot["state"] == "MERGED"
                and _sha(snapshot.get("merge"), 40)
                and pull.get("merge_commit_sha") == snapshot["merge"]
            )
        ),
        "native published PR/ref/owner changed",
    )
    sources = snapshot.get("sources") or []
    _need(
        issue.get("number") == int(scope["number"])
        and issue.get("node_id") == scope["node_id"]
        and "pull_request" not in issue
        and hashlib.sha256(issue_body.encode()).hexdigest() == scope["body_sha256"]
        and issue.get("assignees") == [record["_instruction"]["incumbent_assignee"]]
        and any(
            row.get("node_id") == scope["node_id"] and row.get("body") == issue_body
            for row in sources
        ),
        "fresh incumbent body/owner/source changed",
    )
    return observation


@dataclass(frozen=True)
class QueueCallbacks:
    observe: Callable[[str], dict]
    execute: Callable[[str, str, dict, dict], dict]
    reconcile: Callable[[str, dict], dict]


def queue_callbacks(continuation, backend):
    """Use only inside the existing queue with its unchanged admission/policy."""
    _need(type(backend) is native.Closer, "existing native Closer backend required")
    record = _record(continuation)
    previous = getattr(backend, "_publication_verification_context", None)
    _need(previous is None or previous is continuation, "publication backend already bound")
    backend._publication_verification_context = continuation

    def observe(target):
        try:
            observation = _fresh(backend, target, record)
            if observation.get("action") in {"close_source", "complete"}:
                require_evaluated_scope(
                    continuation, observation.get("evidence") or {}, backend=backend
                )
            return observation
        except (ValueError, KeyError, TypeError, OSError, RuntimeError, AttributeError) as error:
            if isinstance(error, closer_evidence.RoundControlException):
                raise
            return {
                "target": target,
                "revision": "publication_binding_unknown",
                "action": "wait",
                "eligible": False,
                "reason": "fresh publication binding unavailable",
                "evidence": {},
                "next_trigger": "source-confirmed publication and native binding recheck",
            }

    def execute(target, action, snapshot, operation):
        _need(
            queue._currently_invoking(target, operation), "original guarded queue callback required"
        )
        try:
            current = _fresh(backend, target, record)
            _need(
                current.get("revision") == snapshot.get("revision"),
                "publication observation changed",
            )
            if action in {"close_source", "complete"}:
                require_evaluated_scope(continuation, snapshot, backend=backend)
        except (ValueError, KeyError, TypeError, OSError, RuntimeError, AttributeError) as error:
            if isinstance(error, closer_evidence.RoundControlException):
                raise
            return {
                "status": "blocked",
                "reason": "fresh publication binding unavailable",
                "next_trigger": "source-confirmed publication and native binding recheck",
            }
        return native.Closer.execute(backend, target, action, snapshot, operation)

    def reconcile(target, operation):
        try:
            _fresh(backend, target, record)
            _need(
                (operation.get("binding") or {}).get("head") == record["pr"]["head_oid"],
                "original queue head differs",
            )
        except (ValueError, KeyError, TypeError, OSError, RuntimeError, AttributeError) as error:
            if isinstance(error, closer_evidence.RoundControlException):
                raise
            return {"status": "unknown", "reason": "original publication/queue binding unresolved"}
        return native.Closer.reconcile(backend, target, operation)

    return QueueCallbacks(observe, execute, reconcile)


def material_selector(continuation):
    """Closed selector DATA from protected source knowledge; collector authorizes."""
    return _material_selector_from_record(_record(continuation))


def _material_selector_from_record(record):
    """Private protected-record projection; shape alone supplies no authority."""
    original = record.get("_instruction")
    _need(
        type(original) is dict and type(original.get("payload")) is dict,
        "legacy instruction is evidence-only",
    )
    pr = record["pr"]
    return instruction.validate_selector(
        {
            "kind": "corrective_instruction",
            "owner_instruction": copy.deepcopy(original["owner_instruction"]),
            "request_sha256": record["request"]["request_sha256"],
            "required_tests_sha256": wire.digest(record["required_tests"]),
            "corrective_pr": {
                "repository_id": pr["repository_id"],
                "number": str(pr["pr_number"]),
                "node_id": pr["pr_node_id"],
                "head_sha": pr["head_oid"],
            },
            "incumbent_assignee": {k: original["incumbent_assignee"][k] for k in ("id", "login")},
        }
    )


def bind_verifier_inputs(continuation, backend, target, snapshot, prepared):
    """The one reserved native prepare hook; never fake a repeat/predecessor.

    The deployed canonical additive corrective-scope consumer and its exact
    native input binding must be supported before this caller emits a corrective
    request. Keep the ordinary prepared request untouched on refusal.
    """
    record = _record(continuation)
    _need(type(backend) is native.Closer, "existing native Closer backend required")
    _need(
        target == record["pr"]["repository"] + "#" + str(record["pr"]["pr_number"])
        and snapshot.get("head") == record["pr"]["head_oid"]
        and snapshot.get("state") == "MERGED",
        "corrective verification subject differs",
    )
    _need(
        prepared.get("mutation_kind") == "workflow_dispatch", "existing verifier request required"
    )
    import closer_corrective_verifier_readback as readback

    try:
        readback.require_deployed(backend, record, prepared.get("fields", {}).get("ref"))
        selector = material_selector(continuation)
        previous = (
            verification_events.material_selectors(prepared["material_selectors"])
            if prepared["material_selectors"]
            else []
        )
        typed = [row for row in previous if row.get("kind") == "corrective_instruction"]
        _need(not typed or typed == [selector], "repeat corrective scope changed")
        selected = verification_events.material_selectors(previous + ([] if typed else [selector]))
    except (ValueError, KeyError, TypeError) as error:
        raise EvaluatorScopeHeld(
            "canonical corrective evaluator input consumer unavailable"
        ) from error
    value = copy.deepcopy(prepared)
    value["material_selectors"] = selected
    value["fields"]["inputs[verification_material]"] = json.dumps(selected, separators=(",", ":"))
    if snapshot.get("verifier"):
        _, terminal = readback._selected_comment(backend, record, snapshot)
        expected = _scope_binding(record)
        compact = terminal.get("corrective_verification_input")
        already_scoped = (
            type(compact) is dict
            and compact.get("corrective_instruction") == expected
            and compact.get("verification_event") == terminal.get("verification_event")
        )
        if not already_scoped:
            event = terminal.get("verification_event")
            _need(
                verification_events.valid_event(
                    event,
                    record["pr"]["repository"],
                    record["pr"]["pr_number"],
                    record["pr"]["head_oid"],
                    snapshot.get("merge"),
                ),
                "additional corrective material requires genuine same-PR settled event",
            )
            predecessor = value.get("verification_predecessor")
            _need(
                predecessor is None or predecessor == event["event_id"],
                "existing same-PR verification predecessor differs",
            )
            value["verification_predecessor"] = event["event_id"]
            value["fields"]["inputs[verification_predecessor]"] = event["event_id"]
    return value


def require_evaluated_scope(continuation, snapshot, *, backend=None):
    """Additional gate only; original native Verify and ACT guards still apply."""
    record = _record(continuation)
    _need(
        type(backend) is native.Closer,
        "authenticated corrective evaluator scope readback unavailable",
    )
    import closer_corrective_verifier_readback as readback

    try:
        retained = readback.read(backend, record, snapshot)
        expected = _validate_manifest(record, retained["manifest"], snapshot)
        _need(
            retained["compact"]["corrective_instruction"] == expected,
            "terminal corrective instruction row differs from full evaluator material",
        )
    except (ValueError, KeyError, TypeError, OSError, RuntimeError) as error:
        if isinstance(error, closer_evidence.RoundControlException):
            raise
        raise EvaluatorScopeHeld(
            "authenticated corrective evaluator scope readback unavailable"
        ) from error
    return copy.deepcopy(retained["native_origin"])


def _scope_binding(record):
    selector = _material_selector_from_record(record)
    return {
        "kind": "corrective_instruction",
        "owner_instruction": selector["owner_instruction"],
        "request_sha256": selector["request_sha256"],
        "required_tests_sha256": selector["required_tests_sha256"],
        "finding_sha256": record["request"]["finding"]["evidence"]["sha256"],
        "raw_findings_sha256": wire.digest(record["findings"]),
        "original_target": record["request"]["target"],
        "source_issue": record["request"]["source_issue"],
        "incumbent_issue": record["request"]["incumbent"]["issue"],
        "corrective_pr": selector["corrective_pr"],
        "incumbent_assignee": selector["incumbent_assignee"],
        "dispatch_actor": {"id": record["pr"]["writer_id"], "login": record["pr"]["writer_login"]},
        "authority": "native_same_principal_dispatch",
    }


def _validate_manifest(record, manifest, snapshot):
    """Pure integrity/join check, never native origin or acceptance authority.

    The caller must separately authenticate the actual native comment/run/
    attempt/artifact/source and evaluator terminal result before using this.
    """
    _exact(
        manifest,
        {"verification_event", "request", "material", "assessment_identity", "execution_binding"},
    )
    event, request, material = (manifest[k] for k in ("verification_event", "request", "material"))
    pr = record["pr"]
    _need(
        verification_events.valid_event(
            event, pr["repository"], pr["pr_number"], pr["head_oid"], snapshot.get("merge")
        ),
        "corrective verification event subject differs",
    )
    _need(
        type(request) is dict
        and request.get("schema") == "verification-request/v1"
        and type(material) is dict
        and material.get("schema") == "verification-material/v1"
        and wire.digest(request) == event["request_sha256"]
        and wire.digest(material) == event["material_sha256"],
        "complete corrective request/material digests differ",
    )
    _need(
        verification_events.execution_binding(manifest["execution_binding"]) is not None,
        "corrective evaluator execution binding differs",
    )
    assessment = manifest["assessment_identity"]
    _need(
        type(assessment) is dict
        and assessment.get("canonical") is True
        and assessment.get("mode") in {"evaluate", "compare"},
        "canonical corrective assessment required",
    )
    expected = _scope_binding(record)
    rows = material.get("required_evidence")
    _need(
        type(rows) is list
        and sum(type(row) is dict and row.get("kind") == "corrective_instruction" for row in rows)
        == 1
        and expected in rows,
        "exact original corrective test scope absent or changed",
    )
    sources = request.get("sources")
    _need(type(sources) is list, "original verification requirement sources missing")
    originals = [record["request"]["source_issue"], record["request"]["incumbent"]["issue"]]
    for original in originals:
        row = {
            "kind": "issue",
            "repository": pr["repository"],
            "number": original["number"],
            "body_sha256": original["body_sha256"],
        }
        _need(row in sources, "full original issue requirements absent from verification request")
    _need(
        {
            "kind": "pull_request",
            "repository": pr["repository"],
            "number": str(pr["pr_number"]),
            "body_sha256": pr["body_sha256"],
        }
        in sources,
        "original corrective PR requirements absent from verification request",
    )
    return copy.deepcopy(expected)
