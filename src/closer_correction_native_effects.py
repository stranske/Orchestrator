"""Native correction POST receipts in the original local index writer custody.

This is not a coding/provider admission or a hosted workflow activation. The
currently pinned typed auto-pilot consumer rejects correction selectors. Initial
instruction bootstrap also needs the real root-owned source enrollment producer;
this module never turns a seed or a self-hashed document into that authority.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import math
import os
import re
import subprocess
import time
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import closer_correction_controller as ctl
import closer_queue as queue

_LOADED_SOURCE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
MAX_RESPONSE = 262144
MAX_PAYLOAD = 131072
PHASES = {"issue_creation", "instruction_publication"}
_LEGACY_INSTRUCTION = object()
_TYPED_WORKFLOW = ".github/workflows/agents-auto-pilot.yml"


def _need(ok, reason):
    if not ok:
        raise ctl.ControllerUnknown(reason)


def _ordinary_native_effect_only():
    from closer_correction_maintenance import assert_effect_allowed

    assert_effect_allowed("POST")


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def semantic_request(request):
    """Identity of the normative decision, without mutable declaration limits."""
    return {
        key: request[key]
        for key in (
            "schema",
            "target",
            "source_issue",
            "original_verifier",
            "finding",
            "required_tests",
            "incumbent",
            "scope",
        )
    }


def phase_key(request, phase, subject):
    _need(phase in PHASES, "unsupported native effect phase")
    _need(subject is None or ctl.subject(subject), "native subject missing")
    return ctl.digest(
        {
            "schema": "local-correction-native-phase/v1",
            "effect_key": ctl.effect_key(request),
            "source_scope_digest": ctl.scope_digest(request),
            "phase": phase,
            "subject": subject,
        }
    )


def parse_native_response(raw, *, returncode):
    """Parse the original bounded gh --include response, never infer HTTP 201."""
    _need(
        type(raw) is str and type(returncode) is int and returncode == 0,
        "native HTTP transport outcome unknown",
    )
    encoded = raw.encode("utf-8")
    _need(0 < len(encoded) <= MAX_RESPONSE, "native HTTP response bound exceeded")
    separator = "\r\n\r\n" if "\r\n\r\n" in raw else "\n\n"
    header, found, body = raw.partition(separator)
    _need(bool(found), "native HTTP status/headers unavailable")
    lines = header.splitlines()
    status = re.fullmatch(r"HTTP/(?:1\.[01]|2(?:\.0)?) ([1-5][0-9]{2})(?: [^\r\n]*)?", lines[0])
    _need(status is not None, "native HTTP status unavailable")
    headers = {}
    for line in lines[1:]:
        _need(":" in line and not line.startswith((" ", "\t")), "native HTTP headers malformed")
        name, value = line.split(":", 1)
        name, value = name.lower(), value.strip()
        _need(
            re.fullmatch(r"[a-z0-9-]+", name) and name not in headers,
            "native HTTP duplicate or invalid header",
        )
        headers[name] = value
    request_id = headers.get("x-github-request-id", "")
    _need(
        0 < len(request_id) <= 4096 and not any(ord(c) < 32 for c in request_id),
        "native GitHub request ID unavailable",
    )
    # A second HTTP/proxy/redirect header block is not a native creation body.
    import closer_dispatcher as closer

    document = closer._strict_response_json(body)
    _need(type(document) is dict, "native response body is not an object")
    return {
        "status": int(status[1]),
        "github_request_id": request_id,
        "body": document,
        "raw_sha256": _sha(encoded),
        "raw_base64": base64.b64encode(encoded).decode(),
    }


def _creator(body, config):
    user = body.get("user") or {}
    _need(
        ctl.positive(user.get("id"))
        and user.get("id") == config.writer_id
        and user.get("login") == config.writer_login
        and user.get("type") == config.writer_type,
        "native POST creator differs from original writer",
    )
    return {key: user[key] for key in ("id", "login", "type")}


def validate_creation(response, *, phase, payload, repository, config, parent=None):
    """Original POST status + exact payload + native creator establish origin.

    A later GET can establish freshness only. It cannot substitute for this
    original response, even when a marker/body hash happens to match.
    """
    _need(response.get("status") == 201, "native creation did not return HTTP 201")
    body = response["body"]
    creator = _creator(body, config)
    _need(
        ctl.positive(body.get("id")) and isinstance(body.get("node_id"), str) and body["node_id"],
        "native created identity missing",
    )
    _need(body.get("body") == payload["body"], "native created body differs from original payload")
    if phase == "issue_creation":
        _need(
            ctl.positive(body.get("number"))
            and not body.get("pull_request")
            and body.get("state") == "open"
            and body.get("title") == payload["title"]
            and ctl.native_api_url(body.get("url"), repository, "/issues/" + str(body["number"]))
            and type(body.get("labels")) is list
            and sorted(label.get("name") for label in body["labels"]) == sorted(payload["labels"])
            and body.get("assignees") == [],
            "native created issue payload/subject differs",
        )
        return {
            "subject": {"kind": "issue", "number": str(body["number"]), "node_id": body["node_id"]},
            "creator": creator,
            "body_sha256": _sha(body["body"].encode()),
        }
    _need(
        phase == "instruction_publication" and parent is not None,
        "native instruction parent missing",
    )
    ctl.subject(parent)
    _need(
        parent["kind"] == "issue"
        and ctl.native_api_url(body.get("issue_url"), repository, "/issues/" + parent["number"])
        and ctl.native_api_url(body.get("url"), repository, "/issues/comments/" + str(body["id"]))
        and re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", str(body.get("updated_at"))),
        "native created instruction subject/version differs",
    )
    return {
        "subject": parent,
        "creator": creator,
        "owner_instruction": {
            "repository": repository,
            "issue_number": parent["number"],
            "issue_node_id": parent["node_id"],
            "comment_id": body["id"],
            "comment_node_id": body["node_id"],
            "updated_at": body["updated_at"],
            "body_sha256": _sha(body["body"].encode()),
            "writer_id": creator["id"],
        },
    }


def _payload(request, phase, finding_text, *, raw_findings=_LEGACY_INSTRUCTION):
    _need(
        type(finding_text) is str
        and _sha(finding_text.encode()) == request["finding"]["evidence"]["sha256"],
        "original source finding text unavailable",
    )
    tests = []
    for test in request["required_tests"]:
        _need(test["kind"] == "required_test_spec", "native source test specification required")
        tests.append(test["specification"])
    body = (
        "## Source-owned correction instruction\n\n"
        + "Original source: https://github.com/"
        + request["target"]["repository"]
        + "/issues/"
        + request["source_issue"]["number"]
        + "\n\n"
        + "Exact original finding:\n"
        + finding_text
        + "\n\n"
        + "## Required tests\n\n"
        + "\n\n".join(tests)
        + "\n"
    )
    if phase == "instruction_publication":
        if raw_findings is not _LEGACY_INSTRUCTION:
            import closer_corrective_verification_instruction as instruction

            body = instruction.append(
                body, request, finding_text, raw_findings, max_bytes=MAX_PAYLOAD
            )
        payload = {"body": body}
    else:
        payload = {
            "title": "[Follow-up] PR #" + request["target"]["pr"] + ": " + request["finding"]["id"],
            "body": body,
            "labels": ["follow-up"],
        }
    _need(len(ctl.canonical(payload)) <= MAX_PAYLOAD, "native payload exceeds bound")
    return payload


def _remaining(request, operation):
    declared = datetime.fromisoformat(
        request["original_operation"]["deadline"].replace("Z", "+00:00")
    )
    deadline = operation["deadline"]
    _need(
        declared.tzinfo is not None
        and type(deadline) in (int, float)
        and math.isfinite(deadline)
        and type(request["original_operation"]["attempt_budget"]) is int
        and request["original_operation"]["attempt_budget"] == 1
        and type(operation.get("attempts")) is int
        and operation["attempts"] == 1,
        "original native effect limits unavailable",
    )
    seconds = min(declared.timestamp(), deadline) - time.time()
    _need(seconds >= 2, "original native effect deadline expired")
    return min(15, int(seconds))


def _original_post(controller, custody, operation, intent, payload):
    """One original observed native gh operation; no retry/auth loader or SDK."""
    _ordinary_native_effect_only()
    import closer_resources as resources

    controller._require_writer(operation, custody)
    controller._native_effect_source()
    key = intent["phase_key"]
    name = "native-payload-" + key + ".json"
    custody.create(name, payload)
    _need(custody.read(name) == payload, "original native payload changed")
    binding = resources.finite_pool_current_binding()

    def check(_row):
        controller._full_writer_validation(operation, custody)
        current = controller.read_bound(custody)
        _need(
            current["commit_sha"] == intent["prepared_index"]["commit_sha"]
            and ctl.digest(current["data"]) == intent["prepared_index"]["state_sha256"],
            "native original prepared index moved before POST",
        )
        if intent.get("source_record_ref") is not None:
            _source_selection(controller, operation)
            _need(
                _capture_source(controller, operation, intent["request"])
                == intent["source_before"],
                "native source parent moved before first instruction POST",
            )
        _native_writer(controller, operation)
        _fresh_ownership(controller, operation, intent["request"])

    def starting(row):
        check(row)
        if binding:
            resources.finite_pool_prepare_request(row)

    def started(row):
        controller._require_writer(operation, custody)
        if binding:
            resources.finite_pool_process_started(row)

    def finished(row):
        if binding:
            resources.finite_pool_process_finished(row)
        custody.check()

    observation = {
        "source": "local-correction-native-post/v1",
        # Local observed transport bookkeeping, never a provider/GHA run ID.
        "run_id": "local_native_http:" + uuid.uuid4().hex,
        "operation_id": operation["id"],
        "agent": "github",
        "profile_id": "github-api",
    }
    if binding:
        observation.update(
            finite_frontier_id=binding["frontier_id"], finite_frontier_target=binding["target"]
        )
    return _transport_post(
        controller,
        custody,
        operation,
        intent,
        payload,
        observation=observation,
        starting=starting,
        started=started,
        finished=finished,
    )


def _transport_post(
    controller, custody, operation, intent, payload, *, observation, starting, started, finished
):
    """Native I/O port only; it issues no source/role/owner admission result."""
    _ordinary_native_effect_only()
    import dispatcher

    controller._require_writer(operation, custody)
    _need(
        custody.read("native-intent-" + intent["phase_key"] + ".json") == intent,
        "original write-ahead intent missing before native transport",
    )
    _need(
        re.fullmatch(
            r"/repos/"
            + re.escape(controller.config.repository)
            + r"/issues(?:/[1-9][0-9]*/comments)?",
            intent["endpoint"],
        ),
        "native POST endpoint outside original issue/comment scope",
    )
    command = [
        "gh",
        "api",
        "--hostname",
        "github.com",
        "--include",
        "--method",
        "POST",
        intent["endpoint"],
        "--input",
        "-",
    ]
    observation = {**observation, "provider_argv": command}
    # Existing native supervised transport; caller environment/authentication is
    # never inspected, copied, saved, provisioned or put in the receipt here.
    # Pin a private anonymous descriptor containing exactly the retained
    # payload. The CLI never reopens a caller-visible pathname after the final
    # source/index gate; ancestors cannot redirect its request body.
    anonymous = ".native-stdin-" + uuid.uuid4().hex
    descriptor = os.open(
        anonymous, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=custody.fd
    )
    os.unlink(anonymous, dir_fd=custody.fd)
    with os.fdopen(descriptor, "w+b") as stream:
        stream.write(ctl.canonical(payload))
        stream.flush()
        os.fsync(stream.fileno())
        stream.seek(0)
        result = dispatcher.run_observed_offload(
            command,
            callback=started,
            starting_callback=starting,
            finished_callback=finished,
            observation=observation,
            cwd=None,
            timeout=_remaining(intent["request"], operation),
            output_limit=MAX_RESPONSE,
            stdin=stream,
        )
    custody.check()
    return parse_native_response(result.stdout, returncode=result.returncode)


def read_response(custody, intent):
    """Read only an original protected POST response bound to this exact WAL."""
    key = intent["phase_key"]
    receipt = custody.read("native-response-" + key + ".json")
    _need(
        receipt.get("schema") == "local-correction-native-response/v1"
        and receipt.get("intent_sha256") == ctl.digest(intent),
        "original native POST response custody differs",
    )
    response = receipt["response"]
    raw = base64.b64decode(response["raw_base64"], validate=True)
    checked = parse_native_response(raw.decode(), returncode=receipt["transport_returncode"])
    _need(checked == response, "original native POST response bytes differ")
    return response


def _perform_post(controller, custody, operation, intent, payload):
    """Immutable phase WAL precedes POST; an existing intent never repeats it."""
    _ordinary_native_effect_only()
    key = intent["phase_key"]
    name = "native-intent-" + key + ".json"
    if not custody.create(name, intent):
        original = custody.read(name)
        return read_response(custody, original), original
    # Durably publish the full original owner/request/payload/index binding
    # before calling the native endpoint. Failure leaves a permanent UNKNOWN.
    try:
        response = _original_post(controller, custody, operation, intent, payload)
        custody.check()
        custody.create(
            "native-response-" + key + ".json",
            {
                "schema": "local-correction-native-response/v1",
                "intent_sha256": ctl.digest(intent),
                "transport_returncode": 0,
                "response": response,
            },
        )
        _need(read_response(custody, intent) == response, "native response readback changed")
        return response, intent
    finally:
        controller._full_writer_validation(operation, custody)


def unsupported_dispatch():
    """Pinned current Workflows typed consumer rejects before ordinary execution."""
    return ctl.held(
        "typed_autopilot_consumer_structurally_disabled_before_POST",
        workflow=_TYPED_WORKFLOW,
        post_attempted=False,
    )


def bootstrap_instruction():
    return ctl.held(
        "first_instruction_requires_actual_root_source_enrollment_before_POST", post_attempted=False
    )


def _source_selection(controller, operation):
    import inspect

    import closer_correction_source_record as source

    installation = controller._source_installation()
    path = Path(inspect.getsourcefile(source.validate_for_native_effect) or "")
    pinned = installation.source_closure.get("closer_correction_source_record.py")
    _need(
        type(pinned) is dict
        and path.is_file()
        and not path.is_symlink()
        and str(path.resolve()) == pinned.get("path")
        and pinned.get("sha256") == source._LOADED_SOURCE_SHA256
        and _sha(path.read_bytes()) == source._LOADED_SOURCE_SHA256,
        "fixed root source-record producer unavailable or changed",
    )
    selection = getattr(controller._writer_context, "native_selection", None)
    _need(type(selection) is source.SourceSelection, "actual private source selection required")
    return source.validate_for_native_effect(selection, controller, operation)


def _native_writer(controller, operation):
    transport = getattr(controller.store.request, "__self__", None)
    _need(
        type(transport) is NativeHTTPTransport and transport.controller is controller,
        "fixed installed native GitHub writer transport required",
    )
    native = transport.read_native_writer()
    _need(
        ctl.positive(native.get("id"))
        and native.get("id") == controller.config.writer_id
        and native.get("login") == controller.config.writer_login
        and native.get("type") == controller.config.writer_type,
        "current authenticated native writer identity differs before POST",
    )
    permission = controller.store.call(
        "GET", "/collaborators/" + quote(controller.config.writer_login, safe="") + "/permission"
    )
    _need(
        ctl.positive(permission.get("user", {}).get("id"))
        and permission.get("permission") in {"write", "maintain", "admin"}
        and permission.get("user", {}).get("id") == controller.config.writer_id
        and permission["user"].get("login") == controller.config.writer_login,
        "native original writer permission unavailable",
    )
    controller.live(operation)


def _fresh_ownership(controller, operation, request):
    import closer_correction_ownership as ownership
    import closer_dispatcher as closer

    prefix = "repos/" + controller.config.repository

    def native_read(argv, **_options):
        _need(
            len(argv) == 4
            and argv[:3] == ["api", "--method", "GET"]
            and argv[3].startswith(prefix + "/"),
            "native ownership read outside original repository",
        )
        controller.live(operation)
        path = argv[3][len(prefix) :]
        value = controller.store.call("GET", path)
        return 0, project_retained_issue_ownership(
            controller,
            getattr(controller._writer_context, "custody", None),
            operation,
            request,
            path,
            value,
        )

    client = closer.GitHub(deadline=operation["deadline"], api_fn=native_read)
    seed = {
        "schema": "closer-correction-seed/v1",
        **{
            key: request[key]
            for key in ("target", "source_issue", "original_verifier", "finding", "scope")
        },
    }
    observed = ownership.collect_ownership(
        github=client, seed=seed, deadline=operation["deadline"]
    ).document()
    _need(
        observed["incumbent"] == request["incumbent"],
        "current native incumbent/no-incumbent changed",
    )
    return observed


def _enrolled(controller, custody, operation, key):
    """Actual current queue/source decision and fresh native source facts."""
    _need(type(controller) is ctl.IndexController, "original native index controller required")
    _need(re.fullmatch(r"[a-f0-9]{64}", key), "native effect key malformed")
    controller.live(operation)
    before = controller.read_bound(custody)
    entry = next((e for e in before["data"]["entries"] if e["key"] == key), None)
    _need(entry is not None, "first_instruction_requires_actual_root_source_enrollment_before_POST")
    selected, request = entry["admitted_selector"], entry["request"]
    controller.authenticate(selected, request, custody, operation)
    record = custody.read("admission-" + selected["record_sha256"] + ".json")
    controller.fresh_issue_context(
        request,
        record,
        {"owner_login": request["incumbent"].get("owner", controller.config.writer_login)},
    )
    repository = controller.config.repository
    pull = controller.store.call("GET", "/pulls/" + request["target"]["pr"])
    _need(
        str(pull.get("number")) == request["target"]["pr"]
        and pull.get("head", {}).get("sha") == request["target"]["head_sha"]
        and pull.get("base", {}).get("sha") == request["target"]["base_sha"]
        and pull.get("merge_commit_sha") == request["target"]["merge_sha"],
        "native original PR source identity changed",
    )
    _native_writer(controller, operation)
    _remaining(request, operation)
    _fresh_ownership(controller, operation, request)
    finding = next(
        (r for r in record["materializer"]["raw_findings"] if r["id"] == request["finding"]["id"]),
        None,
    )
    _need(finding is not None, "actual source decision finding missing")
    _need(request["target"]["repository"] == repository, "source repository changed")
    return before, entry, finding["text"]


def _subject(entry, phase):
    request = entry["request"]
    if phase == "issue_creation":
        _need(
            request["incumbent"].get("kind") == "no_incumbent",
            "existing incumbent cannot create another issue",
        )
        return None
    if request["incumbent"].get("kind") != "no_incumbent":
        return {
            "kind": "issue",
            **{k: request["incumbent"]["issue"][k] for k in ("number", "node_id")},
        }
    stage = next((s for s in entry["stages"] if s["phase"] == "issue_creation"), None)
    _need(
        stage is not None and stage["state"] == "confirmed",
        "native created issue not index-confirmed",
    )
    return stage["result"]


def _make_plan(controller, operation, before, entry, phase, finding_text):
    request = entry["request"]
    subject = _subject(entry, phase)
    payload = _payload(request, phase, finding_text)
    endpoint = "/repos/" + controller.config.repository + "/issues"
    if subject:
        endpoint += "/" + subject["number"] + "/comments"
    owner, _ = controller.live(operation)
    plan = {
        "schema": "local-correction-native-plan/v1",
        "phase": phase,
        "phase_key": phase_key(request, phase, subject),
        "key": entry["key"],
        "request": request,
        "owner_semantics_sha256": ctl.digest(semantic_request(request)),
        "subject": subject,
        "endpoint": endpoint,
        "payload": payload,
        "payload_sha256": ctl.digest(payload),
        "owner": owner,
        "index_before": {
            "commit_sha": before["commit_sha"],
            "state_sha256": ctl.digest(before["data"]),
            "revision": before["data"]["revision"],
        },
        "admitted_selector": entry["admitted_selector"],
    }
    return plan


def plan_effect(controller, *, operation, key, phase):
    """Create only a protected source plan; returns concrete source-selection data.

    The original source owner must select the returned action in the physical
    queue registration. Returning a proposal never selects or submits it.
    """
    _ordinary_native_effect_only()
    try:
        _need(phase in PHASES, "unsupported native phase")
        with controller.fenced(operation) as custody:
            before, entry, text = _enrolled(controller, custody, operation, key)
            plan = _make_plan(controller, operation, before, entry, phase, text)
            digest = ctl.digest(plan)
            name = "native-plan-" + plan["phase_key"] + ".json"
            custody.create(name, plan)
            if phase == "issue_creation":
                action = {
                    "kind": "prepare",
                    "key": key,
                    "phase": phase,
                    "intent_sha256": digest,
                    "selector": entry["admitted_selector"],
                }
            else:
                action = {
                    "kind": "native_instruction_publication",
                    "key": key,
                    "plan_sha256": digest,
                }
            return ctl.held(
                "original source must select exact native phase plan",
                status="native_plan_retained",
                plan_name=name,
                plan_sha256=digest,
                action=action,
                post_attempted=False,
            )
    except (
        ImportError,
        OSError,
        ValueError,
        KeyError,
        TypeError,
        AttributeError,
        StopIteration,
    ) as error:
        return ctl.held(str(error), post_attempted=False)


def _plan(controller, custody, operation, before, entry, phase):
    subject = _subject(entry, phase)
    key = phase_key(entry["request"], phase, subject)
    plan = custody.read("native-plan-" + key + ".json")
    owner, registration = controller.live(operation)
    _need(
        plan.get("schema") == "local-correction-native-plan/v1"
        and plan.get("phase") == phase
        and plan.get("phase_key") == key
        and plan.get("key") == entry["key"]
        and plan.get("subject") == subject
        and plan.get("owner") == owner
        and plan.get("owner_semantics_sha256") == ctl.digest(semantic_request(entry["request"]))
        and plan.get("request") == entry["request"]
        and plan.get("admitted_selector") == entry["admitted_selector"]
        and ctl.digest(plan.get("payload")) == plan.get("payload_sha256"),
        "original native phase plan owner/scope changed",
    )
    if phase == "issue_creation":
        stage = next((s for s in entry["stages"] if s["phase"] == phase), None)
        _need(
            stage is not None
            and stage["state"] == "prepared"
            and stage["intent_sha256"] == ctl.digest(plan)
            and stage["intent_selector"] == entry["admitted_selector"],
            "native issue phase is not source/index prepared",
        )
    else:
        action = {
            "kind": "native_instruction_publication",
            "key": entry["key"],
            "plan_sha256": ctl.digest(plan),
        }
        _need(
            ctl.digest(action) in registration.get("actions", []),
            "native instruction not source-selected in original queue",
        )
        _need(
            plan["index_before"]["commit_sha"] == before["commit_sha"]
            and plan["index_before"]["state_sha256"] == ctl.digest(before["data"]),
            "native instruction original index moved",
        )
    return plan


def _fresh_created(controller, identity, response, phase):
    """A GET only rechecks the original 201 identity; it never creates origin."""
    if phase == "issue_creation":
        path = "/issues/" + identity["subject"]["number"]
        keys = ("id", "node_id", "number", "body", "title", "user", "state", "labels", "assignees")
    else:
        path = "/issues/comments/" + str(identity["owner_instruction"]["comment_id"])
        keys = ("id", "node_id", "issue_url", "body", "user", "updated_at")
    current = controller.store.call("GET", path)
    _need(
        phase != "issue_creation" or current == response["body"],
        "native created issue changed from the complete original POST201 object",
    )
    _need(
        all(
            (
                (
                    ctl.native_api_url(
                        current.get(k),
                        controller.config.repository,
                        "/issues/" + identity["subject"]["number"],
                    )
                    and ctl.native_api_url(
                        response["body"].get(k),
                        controller.config.repository,
                        "/issues/" + identity["subject"]["number"],
                    )
                )
                if k == "issue_url"
                else current.get(k) == response["body"].get(k)
            )
            for k in keys
        ),
        "native created identity/body/creator changed after original response",
    )


def execute_effect(controller, *, operation, key, phase):
    """Perform one source-prepared native phase while the existing writer holds.

    No adapter-global client changes or authority callbacks are accepted. The
    canonical controller's fixed reader validates native issue confirmation.
    """
    _ordinary_native_effect_only()
    attempted = False
    try:
        _need(phase in PHASES, "unsupported native phase")
        with controller.fenced(operation) as custody:
            before, entry, _text = _enrolled(controller, custody, operation, key)
            plan = _plan(controller, custody, operation, before, entry, phase)
            # Until the fixed canonical hook is installed/reviewed, fail before
            # creating an issue which cannot enter its original source index.
            _need(
                phase != "issue_creation" or callable(getattr(controller, "_apply_locked", None)),
                "fixed_native_index_confirmation_hook_not_installed_before_POST",
            )
            intent = {
                **plan,
                "schema": "local-correction-native-intent/v1",
                "prepared_index": {
                    "commit_sha": before["commit_sha"],
                    "state_sha256": ctl.digest(before["data"]),
                    "revision": before["data"]["revision"],
                },
            }
            controller._native_effect_source()
            attempted = True
            response, original = _perform_post(
                controller, custody, operation, intent, plan["payload"]
            )
            identity = validate_creation(
                response,
                phase=phase,
                payload=original["payload"],
                repository=controller.config.repository,
                config=controller.config,
                parent=original["subject"],
            )
            _fresh_created(controller, identity, response, phase)
            controller.live(operation)
            custody.check()
            proof = {
                "schema": "local-correction-native-origin/v1",
                "intent_sha256": ctl.digest(original),
                "phase_key": original["phase_key"],
                "response_sha256": ctl.digest(response),
                "native": identity,
            }
            custody.create("native-origin-" + ctl.digest(proof) + ".json", proof)
            if phase == "issue_creation":
                action = {
                    "kind": "confirm",
                    "key": entry["key"],
                    "phase": phase,
                    "proof_sha256": ctl.digest(proof),
                    "result": identity["subject"],
                    "selector": entry["admitted_selector"],
                }
                outcome = controller._apply_locked(action, operation=operation, custody=custody)
                _need(
                    outcome.get("status") == "checkpoint_confirmed",
                    "native created issue known but original index confirmation unknown",
                )
            return ctl.held(
                "original native POST201 retained; no coding or workflow authority",
                status="native_effect_confirmed",
                native=identity,
                proof_sha256=ctl.digest(proof),
                post_attempted=True,
            )
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        AttributeError,
        RuntimeError,
        StopIteration,
    ) as error:
        return ctl.held(str(error), post_attempted=attempted)


def validate_index_effect_action(controller, custody, operation, entry, action):
    """Fixed canonical-reader hook: only a protected original issue POST201."""
    if operation.get("action") == "correction_publish_existing_pr" and action.get("kind") == "bind":
        import closer_coding_publication_io as publication_io

        return publication_io.validate_index_bind(controller, custody, operation, entry, action)
    _need(
        action.get("kind") == "confirm" and action.get("phase") == "issue_creation",
        "native coding_PR_or_typed_autopilot_origin_producer_unavailable",
    )
    _need(
        action.get("selector") == entry["admitted_selector"], "native confirmation selector changed"
    )
    proof_sha = action["proof_sha256"]
    _need(re.fullmatch(r"[a-f0-9]{64}", proof_sha), "native origin proof identity malformed")
    proof = custody.read("native-origin-" + proof_sha + ".json")
    _need(
        ctl.digest(proof) == proof_sha
        and proof.get("schema") == "local-correction-native-origin/v1",
        "protected original native origin receipt differs",
    )
    key = phase_key(entry["request"], "issue_creation", None)
    _need(proof["phase_key"] == key, "native original issue phase differs")
    intent = custody.read("native-intent-" + key + ".json")
    response = read_response(custody, intent)
    owner, _ = controller.live(operation)
    stage = next((s for s in entry["stages"] if s["phase"] == "issue_creation"), None)
    plan = custody.read("native-plan-" + key + ".json")
    _need(
        intent.get("owner") == owner
        and intent.get("request") == entry["request"]
        and stage is not None
        and stage["intent_sha256"] == ctl.digest(plan)
        and proof["intent_sha256"] == ctl.digest(intent)
        and proof["response_sha256"] == ctl.digest(response),
        "native original owner/request/index preparation differs",
    )
    native = validate_creation(
        response,
        phase="issue_creation",
        payload=intent["payload"],
        repository=controller.config.repository,
        config=controller.config,
    )
    _need(
        native == proof["native"] and action["result"] == native["subject"],
        "native confirmation cannot adopt another issue",
    )
    _fresh_created(controller, native, response, "issue_creation")
    if intent.get("source_record_ref") is not None:
        _read_issue_transition(controller, custody, operation, entry["request"], required=True)
        _source_selection(controller, operation)
    return None


def _timeline_identity(row):
    """Identify heterogeneous native REST rows without fabricating event IDs.

    These keys only detect overlapping/ambiguous history. The transition still
    compares every original row, in order, including fields outside each key.
    """
    _need(type(row) is dict, "native subject timeline row malformed")
    event = row.get("event")
    _need(
        type(event) is str and event and event not in {"connected", "disconnected"},
        "native subject timeline event unsupported",
    )
    if event == "cross-referenced":
        source = row.get("source")
        issue = source.get("issue") if type(source) is dict else None
        actor = row.get("actor")
        stamp = row.get("created_at")
        _need(
            type(source) is dict
            and source.get("type") == "issue"
            and type(issue) is dict
            and ctl.positive(issue.get("id"))
            and type(issue.get("node_id")) is str
            and bool(issue["node_id"].strip())
            and type(actor) is dict
            and ctl.positive(actor.get("id"))
            and type(stamp) is str,
            "native subject cross-reference identity incomplete",
        )
        try:
            instant = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            _need(instant.tzinfo is not None, "native cross-reference time must be zoned")
        except ValueError as exc:
            raise ctl.ControllerUnknown("native cross-reference time malformed") from exc
        return ("cross-referenced", actor["id"], stamp, issue["id"], issue["node_id"])
    if event == "committed":
        _need(
            type(row.get("sha")) is str
            and re.fullmatch(r"[a-f0-9]{40}", row["sha"])
            and type(row.get("node_id")) is str
            and bool(row["node_id"].strip()),
            "native subject commit identity incomplete",
        )
        return ("committed", row["sha"], row["node_id"])
    _need(ctl.positive(row.get("id")), "native subject timeline identity incomplete")
    # Comment and issue-event numeric IDs are allocated by different resources.
    return ("comment" if event == "commented" else "issue-event", row["id"])


def _parent_snapshot(snapshot, request, config):
    _need(
        type(snapshot) is dict and set(snapshot) == {"parent", "timeline", "last_edited_at"},
        "bounded native parent snapshot incomplete",
    )
    parent = snapshot["parent"]
    identity = request["source_issue"]
    _need(
        type(parent) is dict
        and ctl.positive(parent.get("id"))
        and str(parent.get("number")) == identity["number"]
        and parent.get("node_id") == identity["node_id"]
        and ctl.native_api_url(
            parent.get("url"), config.repository, "/issues/" + identity["number"]
        )
        and parent.get("state") == "open"
        and not parent.get("pull_request")
        and type(parent.get("body")) is str
        and _sha(parent["body"].encode()) == identity["body_sha256"]
        and type(parent.get("updated_at")) is str
        and re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", parent["updated_at"])
        and type(parent.get("comments")) is int
        and parent["comments"] >= 0
        and type(parent.get("labels")) is list
        and type(parent.get("assignees")) is list
        and type(parent.get("title")) is str,
        "native parent identity/body/version/control fields incomplete",
    )
    edited = snapshot["last_edited_at"]
    _need(
        edited is None
        or (type(edited) is str and re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", edited)),
        "native lastEditedAt unavailable",
    )
    rows = snapshot["timeline"]
    _need(
        type(rows) is list and len(rows) <= 399,
        "native subject timeline incomplete or outside bounded history",
    )
    identities = [_timeline_identity(row) for row in rows]
    _need(
        len(set(identities)) == len(identities),
        "native subject timeline identities overlap",
    )
    return parent, rows


def observed_instruction_transition(before, response, after, *, request, payload, config):
    """Prove one observed own-comment transition, without claiming hidden history.

    Ordered source reads, the original POST201, exact full parent fields, and a
    complete bounded native timeline delta establish only this selected observed
    version. Equal second-resolution timestamps are valid. Future changes must
    independently match this retained observed snapshot; no version whitelist.
    """
    old, old_rows = _parent_snapshot(before, request, config)
    new, new_rows = _parent_snapshot(after, request, config)
    native = validate_creation(
        response,
        phase="instruction_publication",
        payload=payload,
        repository=config.repository,
        config=config,
        parent={"kind": "issue", **{k: request["source_issue"][k] for k in ("number", "node_id")}},
    )
    _need(
        before["last_edited_at"] == after["last_edited_at"],
        "native parent lastEditedAt changed during own publication",
    )
    _need(
        {k: v for k, v in old.items() if k not in {"updated_at", "comments"}}
        == {k: v for k, v in new.items() if k not in {"updated_at", "comments"}}
        and set(old) == set(new)
        and new["comments"] == old["comments"] + 1,
        "native parent body/tests/assignment/control fields changed during own publication",
    )
    _need(
        new_rows[: len(old_rows)] == old_rows and len(new_rows) == len(old_rows) + 1,
        "native observable timeline is not exactly the original history plus own comment",
    )
    event = new_rows[-1]
    comment = response["body"]
    _need(
        event.get("event") == "commented"
        and event.get("id") == comment["id"]
        and event.get("node_id") == comment["node_id"]
        and event.get("body") == comment["body"]
        and event.get("created_at") == comment.get("created_at")
        and type(comment.get("created_at")) is str
        and {k: event.get("user", {}).get(k) for k in ("id", "login", "type")} == native["creator"],
        "native timeline delta does not identify original POST201",
    )
    return {
        "schema": "local-correction-observed-own-instruction/v1",
        "before": before,
        "after": after,
        "response_sha256": ctl.digest(response),
        "owner_instruction": native["owner_instruction"],
        "request_semantics_sha256": ctl.digest(semantic_request(request)),
    }


def _capture_source(controller, operation, request):
    transport = getattr(controller.store.request, "__self__", None)
    _need(type(transport) is NativeHTTPTransport, "fixed native parent version reader unavailable")
    return transport.capture_parent_version(operation, request)


def _read_instruction_transition(controller, custody, intent, response):
    transition = custody.read("native-transition-" + intent["phase_key"] + ".json")
    _need(
        transition.get("schema") == "local-correction-observed-own-instruction/v1"
        and transition["before"] == intent.get("source_before")
        and transition
        == observed_instruction_transition(
            transition["before"],
            response,
            transition["after"],
            request=intent["request"],
            payload=intent["payload"],
            config=controller.config,
        ),
        "protected own-instruction transition unavailable or changed",
    )
    return transition


def reconcile_source_checkpoint_locked(controller, custody, operation, selection, published=None):
    """Fresh source selection plus original protected checkpoint, never a new POST."""
    controller._require_writer(operation, custody)
    controller._native_effect_source()
    _need(
        getattr(controller._writer_context, "native_selection", None) is None,
        "another native source selection active",
    )
    controller._writer_context.native_selection = selection
    try:
        facts = _source_selection(controller, operation)
        before = controller.read_bound(custody)
        entries = [
            entry
            for entry in before["data"]["entries"]
            if entry["key"] == ctl.effect_key(facts["request"])
        ]
        _need(len(entries) == 1, "exact existing source checkpoint absent")
        entry = entries[0]
        selected = entry["admitted_selector"]
        record = custody.read("admission-" + selected["record_sha256"] + ".json")
        _need(
            ctl.digest(record) == selected["record_sha256"]
            and semantic_request(record["request"]) == semantic_request(facts["request"])
            and record["seed_selector"] == facts["seed_selector"],
            "existing source checkpoint normative decision differs",
        )
        controller.authenticate(selected, entry["request"], custody, operation)
        controller.fresh_issue_context(
            entry["request"], record, {"owner_login": entry["request"]["incumbent"].get("owner")}
        )
        _fresh_ownership(controller, operation, entry["request"])
        pull = controller.store.call("GET", "/pulls/" + entry["request"]["target"]["pr"])
        _need(
            pull.get("head", {}).get("sha") == entry["request"]["target"]["head_sha"]
            and pull.get("base", {}).get("sha") == entry["request"]["target"]["base_sha"]
            and pull.get("merge_commit_sha") == entry["request"]["target"]["merge_sha"],
            "existing source checkpoint current PR source changed",
        )
        ref = record.get("own_instruction")
        _need(
            type(ref) is dict and set(ref) == {"phase_key", "proof_sha256"},
            "existing checkpoint lacks protected original instruction",
        )
        if published is not None:
            _need(
                type(published) is dict and published.get("proof_sha256") == ref["proof_sha256"],
                "existing checkpoint original receipt selector differs",
            )
        # fresh_issue_context already reloaded the original raw201 and current
        # exact native parent/timeline through validate_retained_source_version.
        _source_selection(controller, operation)
        controller._require_writer(operation, custody)
        _need(controller.read_bound(custody) == before, "existing checkpoint moved during replay")
        return ctl.held(
            "exact original source checkpoint freshly reconciled; no native POST or CAS",
            status="native_instruction_published",
            proof_sha256=ref["proof_sha256"],
            owner_instruction=record["owner_instruction"],
            post_attempted=False,
            checkpoint_reconciled=True,
        )
    finally:
        del controller._writer_context.native_selection


def publish_first_instruction_locked(controller, custody, operation, selection):
    """Concrete first POST, only after the real root source producer is installed.

    The private selection comes from the original classifier/materializer custody
    and a current native queue callback. Seed data, a dict, a marker and an
    environment credential cannot select this operation.
    """
    _ordinary_native_effect_only()
    attempted = False
    try:
        controller._require_writer(operation, custody)
        controller._native_effect_source()
        _need(
            getattr(controller._writer_context, "native_selection", None) is None,
            "another native source selection is active",
        )
        controller._writer_context.native_selection = selection
        facts = _source_selection(controller, operation)
        request = facts["request"]
        before = controller.read_bound(custody)
        if any(e["key"] == ctl.effect_key(request) for e in before["data"]["entries"]):
            del controller._writer_context.native_selection
            return reconcile_source_checkpoint_locked(controller, custody, operation, selection)
        controller.preflight_source_enrollment_locked(custody, operation)
        raw_findings = facts.get("raw_findings")
        _need(
            type(raw_findings) is list and bool(raw_findings),
            "complete original raw findings required",
        )
        raw = [r for r in raw_findings if r["id"] == request["finding"]["id"]]
        _need(len(raw) == 1, "selected original source finding ambiguous")
        subject = {
            "kind": "issue",
            **{k: request["source_issue"][k] for k in ("number", "node_id")},
        }
        phase = "instruction_publication"
        payload = _payload(request, phase, raw[0]["text"], raw_findings=raw_findings)
        source_before = _capture_source(controller, operation, request)
        _need(
            source_before["parent"] == facts.get("original_source_native"),
            "original source full native version changed before first publication",
        )
        owner, _ = controller.live(operation)
        index = {
            "commit_sha": before["commit_sha"],
            "state_sha256": ctl.digest(before["data"]),
            "revision": before["data"]["revision"],
        }
        intent = {
            "schema": "local-correction-native-intent/v1",
            "phase": phase,
            "phase_key": phase_key(request, phase, subject),
            "key": ctl.effect_key(request),
            "request": request,
            "owner_semantics_sha256": ctl.digest(semantic_request(request)),
            "subject": subject,
            "endpoint": "/repos/"
            + controller.config.repository
            + "/issues/"
            + subject["number"]
            + "/comments",
            "payload": payload,
            "payload_sha256": ctl.digest(payload),
            "owner": owner,
            "index_before": index,
            "prepared_index": index,
            "source_record_ref": facts["source_record_ref"],
            "seed_selector": facts["seed_selector"],
            "source_before": source_before,
        }
        _remaining(request, operation)
        _native_writer(controller, operation)
        _fresh_ownership(controller, operation, request)
        _source_selection(controller, operation)
        attempted = True
        response, original = _perform_post(controller, custody, operation, intent, payload)
        identity = validate_creation(
            response,
            phase=phase,
            payload=original["payload"],
            repository=controller.config.repository,
            config=controller.config,
            parent=original["subject"],
        )
        _fresh_created(controller, identity, response, phase)
        _source_selection(controller, operation)
        after = _capture_source(controller, operation, request)
        transition = observed_instruction_transition(
            original["source_before"],
            response,
            after,
            request=request,
            payload=original["payload"],
            config=controller.config,
        )
        custody.create("native-transition-" + original["phase_key"] + ".json", transition)
        _read_instruction_transition(controller, custody, original, response)
        proof = {
            "schema": "local-correction-native-origin/v1",
            "intent_sha256": ctl.digest(original),
            "phase_key": original["phase_key"],
            "response_sha256": ctl.digest(response),
            "native": identity,
        }
        custody.create("native-origin-" + ctl.digest(proof) + ".json", proof)
        return ctl.held(
            "original first instruction POST201 retained; source enrollment remains separate",
            status="native_instruction_published",
            owner_instruction=identity["owner_instruction"],
            proof_sha256=ctl.digest(proof),
            post_attempted=True,
        )
    except (
        ImportError,
        OSError,
        ValueError,
        KeyError,
        TypeError,
        AttributeError,
        RuntimeError,
    ) as error:
        return ctl.held(str(error), post_attempted=attempted)
    finally:
        writer = getattr(controller, "_writer_context", None)
        if writer is not None and getattr(writer, "native_selection", None) is selection:
            del writer.native_selection


def validate_retained_source_version(controller, custody, operation, record, native_parent):
    """Recheck only a protected own201 transition; no SourceRecord recursion."""
    controller._require_writer(operation, custody)
    controller._native_effect_source()
    reference = record.get("own_instruction")
    _need(
        type(reference) is dict
        and set(reference) == {"proof_sha256", "phase_key"}
        and all(type(v) is str and re.fullmatch(r"[a-f0-9]{64}", v) for v in reference.values()),
        "protected own instruction transition selector missing",
    )
    proof = custody.read("native-origin-" + reference["proof_sha256"] + ".json")
    intent = custody.read("native-intent-" + reference["phase_key"] + ".json")
    _need(
        ctl.digest(proof) == reference["proof_sha256"]
        and proof.get("schema") == "local-correction-native-origin/v1"
        and proof.get("phase_key") == reference["phase_key"] == intent.get("phase_key")
        and proof.get("intent_sha256") == ctl.digest(intent)
        and intent.get("phase") == "instruction_publication"
        and intent.get("request") == record["request"]
        and intent.get("source_record_ref") == record["source_record_ref"]
        and intent.get("seed_selector") == record["seed_selector"],
        "protected own instruction original source differs",
    )
    response = read_response(custody, intent)
    _need(ctl.digest(response) == proof["response_sha256"], "own instruction response changed")
    observed = _read_instruction_transition(controller, custody, intent, response)
    _need(
        observed["before"]["parent"] == record["original_source_native"]
        and observed["owner_instruction"] == record["owner_instruction"]
        and observed["owner_instruction"] == proof["native"]["owner_instruction"]
        and native_parent
        == retained_source_after(
            controller, custody, operation, record["request"], observed["after"]
        )["parent"]
        and _capture_source(controller, operation, record["request"])
        == retained_source_after(
            controller, custody, operation, record["request"], observed["after"]
        ),
        "current native source is not the exact retained own-publication version",
    )
    return observed


class NativeHTTPTransport:
    """Concrete fixed native GitHub GET/Contents-CAS port, without new auth.

    Consequential issue/comment POSTs use the separate raw original-response
    producer above. Every native request requires the same opaque source install
    and actual shared writer context; a raw config cannot activate this port.
    """

    def __init__(self, installation):
        self.installation = installation
        self.controller = None

    def bind(self, controller):
        _need(
            type(controller) is ctl.IndexController
            and controller.installation is self.installation,
            "native transport original installation differs",
        )
        self.controller = controller

    def read_native_writer(self):
        # The current User token's native API identity is read, never a secret.
        # Installation/app-only credentials which cannot answer this endpoint
        # remain explicitly unsupported; POST-creator mismatch is too late.
        controller = self.controller
        _need(type(controller) is ctl.IndexController, "native writer controller absent")
        controller._source_installation()
        controller._native_effect_source()
        current = getattr(queue._callback_threads, "invocation", None)
        _need(type(current) is dict, "native writer original callback absent")
        operation = current["operation"]
        controller._require_writer(operation, getattr(controller._writer_context, "custody", None))
        import closer_dispatcher as closer

        return closer.GitHub(deadline=operation["deadline"]).api("user", method="GET")

    def capture_parent_version(self, operation, request):
        controller = self.controller
        controller._native_effect_source()
        controller._require_writer(operation, getattr(controller._writer_context, "custody", None))
        parent_id = request["source_issue"]
        endpoint = "/issues/" + parent_id["number"]
        parent = controller.store.call("GET", endpoint)
        rows = []
        for page in range(1, 5):
            batch = controller.store.call(
                "GET", endpoint + "/timeline?per_page=100&page=" + str(page)
            )
            _need(type(batch) is list and len(batch) <= 100, "native timeline page invalid")
            rows.extend(batch)
            if len(batch) < 100:
                break
        else:
            raise ctl.ControllerUnknown("native complete subject timeline exceeds bound")
        import closer_dispatcher as closer

        # Fixed read-only GraphQL shape: the only variable is the exact native node ID.
        query = (
            "query { node(id:"
            + json.dumps(parent_id["node_id"])
            + ") { ... on Issue { id number lastEditedAt repository { nameWithOwner } } } }"
        )
        document = closer.GitHub(deadline=operation["deadline"]).api(
            "graphql", method="POST", fields={"query": query}, resource="graphql"
        )
        _need(
            type(document) is dict and not document.get("errors"),
            "native parent edit metadata missing",
        )
        node = document.get("data", {}).get("node")
        _need(
            type(node) is dict
            and node.get("id") == parent_id["node_id"]
            and str(node.get("number")) == parent_id["number"]
            and "lastEditedAt" in node
            and ctl.native_repository_name(
                node.get("repository", {}).get("nameWithOwner"), controller.config.repository
            ),
            "native parent edit identity differs",
        )
        _need(
            controller.store.call("GET", endpoint) == parent,
            "native parent changed during version capture",
        )
        result = {
            "parent": copy.deepcopy(parent),
            "timeline": copy.deepcopy(rows),
            "last_edited_at": node["lastEditedAt"],
        }
        _parent_snapshot(result, request, controller.config)
        return result

    def request(self, method, path, body, options):
        controller = self.controller
        _need(
            type(controller) is ctl.IndexController, "native transport original controller absent"
        )
        controller._source_installation()
        controller._native_effect_source()
        current = getattr(queue._callback_threads, "invocation", None)
        _need(type(current) is dict, "native transport original queue callback absent")
        operation = current["operation"]
        custody = getattr(controller._writer_context, "custody", None)
        controller._require_writer(operation, custody)
        _need(
            options == {"maxRetries": 0, "timeout": 15000},
            "native transport retry/timeout policy changed",
        )
        import closer_correction_maintenance as maintenance

        maintenance.guard_index_transport(controller, operation, custody, method, path, body)
        prefix = "/repos/" + controller.config.repository
        _need(
            type(path) is str
            and path.startswith(prefix)
            and (path == prefix or path.startswith(prefix + "/")),
            "native transport foreign repository",
        )
        if method == "PUT":
            controller._full_writer_validation(operation, custody)
            _native_writer(controller, operation)
            _need(
                path == prefix + "/contents/" + ctl.INDEX_PATH
                and type(body) is dict
                and set(body) == {"branch", "message", "sha", "content"}
                and body["branch"] == ctl.BRANCH
                and all(type(v) is str for v in body.values()),
                "native transport only permits exact Contents CAS",
            )
        else:
            suffix = path[len(prefix) :]
            allowed = suffix == "" or re.fullmatch(
                r"/(?:git/ref/heads/closer-correction-state|git/commits/[a-f0-9]{40}"
                r"|contents/\.github/closer-correction/index\.json\?ref=[a-f0-9]{40}"
                r"|pulls/[1-9][0-9]*|issues/[1-9][0-9]*|issues/comments/[1-9][0-9]*"
                r"|issues/[1-9][0-9]*/(?:timeline|events)\?(?:per_page=100&page=[1-4]"
                r"|per_page=15&page=(?:[1-9]|1[0-9]|2[0-7]))"
                r"|collaborators/[A-Za-z0-9_.%\[\]-]+/permission)",
                suffix,
            )
            if not allowed and operation.get("action") == "correction_publish_existing_pr":
                import closer_coding_publication_io as publication_io

                allowed = publication_io.allow_native_ref_read(
                    controller, custody, operation, method, suffix, body
                )
            _need(
                method == "GET" and body is None and allowed,
                "native transport unsupported read or mutation",
            )
        import closer_dispatcher as closer

        client = closer.GitHub(deadline=operation["deadline"])
        try:
            return client.api(path.lstrip("/"), method=method, fields=body)
        finally:
            if method == "PUT":
                controller._full_writer_validation(operation, custody)


def validate_instruction_origin(controller, custody, operation, selection, published):
    """Derive instruction data again from the protected original 201 response.

    The supplied returned dictionary is only a selector. It is never the native
    proof or source-decision authority accepted by the later enrollment owner.
    """
    controller._require_writer(operation, custody)
    controller._native_effect_source()
    _need(
        getattr(controller._writer_context, "native_selection", None) is None,
        "another source selection is active",
    )
    controller._writer_context.native_selection = selection
    try:
        facts = _source_selection(controller, operation)
        request = facts["request"]
        proof_sha = published.get("proof_sha256") if type(published) is dict else None
        _need(
            type(proof_sha) is str and re.fullmatch(r"[a-f0-9]{64}", proof_sha),
            "original protected instruction origin selector unavailable",
        )
        proof = custody.read("native-origin-" + proof_sha + ".json")
        _need(
            proof.get("schema") == "local-correction-native-origin/v1"
            and ctl.digest(proof) == proof_sha,
            "protected instruction origin differs",
        )
        subject = {
            "kind": "issue",
            **{k: request["source_issue"][k] for k in ("number", "node_id")},
        }
        key = phase_key(request, "instruction_publication", subject)
        intent = custody.read("native-intent-" + key + ".json")
        owner, _ = controller.live(operation)
        _need(
            intent.get("schema") == "local-correction-native-intent/v1"
            and intent.get("phase") == "instruction_publication"
            and intent.get("phase_key") == key
            and intent.get("request") == request
            and intent.get("owner") == owner
            and intent.get("source_record_ref") == facts["source_record_ref"]
            and intent.get("seed_selector") == facts["seed_selector"]
            and proof["phase_key"] == key
            and proof["intent_sha256"] == ctl.digest(intent),
            "native instruction original source/operation/phase differs",
        )
        response = read_response(custody, intent)
        _need(
            proof["response_sha256"] == ctl.digest(response), "native instruction response differs"
        )
        native = validate_creation(
            response,
            phase="instruction_publication",
            payload=intent["payload"],
            repository=controller.config.repository,
            config=controller.config,
            parent=subject,
        )
        _need(native == proof["native"], "native instruction cannot adopt a marker or later GET")
        _fresh_created(controller, native, response, "instruction_publication")
        transition = _read_instruction_transition(controller, custody, intent, response)
        expected_after = retained_source_after(
            controller, custody, operation, request, transition["after"]
        )
        _need(
            _capture_source(controller, operation, request) == expected_after,
            "native source version changed after retained own publication",
        )
        _source_selection(controller, operation)
        return native["owner_instruction"]
    finally:
        del controller._writer_context.native_selection


def observed_issue_transition(before, response, after, *, request, payload, config):
    """Only the exact newly-created issue may add one native source crossref.

    No timestamp exemption, source body rewrite, assignment or unrelated history
    may accompany this write. The created issue remains unassigned and never
    supplies an incumbent coding owner.
    """
    old, old_rows = _parent_snapshot(before, request, config)
    new, new_rows = _parent_snapshot(after, request, config)
    native = validate_creation(
        response,
        phase="issue_creation",
        payload=payload,
        repository=config.repository,
        config=config,
    )
    _need(
        request["incumbent"].get("kind") == "no_incumbent",
        "existing incumbent cannot gain a new correction issue",
    )
    _need(
        before["last_edited_at"] == after["last_edited_at"]
        and set(old) == set(new)
        and {k: v for k, v in old.items() if k != "updated_at"}
        == {k: v for k, v in new.items() if k != "updated_at"},
        "native parent changed beyond own issue cross-reference",
    )
    _need(
        new_rows[: len(old_rows)] == old_rows and len(new_rows) == len(old_rows) + 1,
        "native issue history delta is not one exact new cross-reference",
    )
    event = new_rows[-1]
    issue = (event.get("source") or {}).get("issue") or {}
    created = response["body"]
    _need(
        event.get("event") == "cross-referenced"
        and (event.get("source") or {}).get("type") == "issue"
        and event.get("actor", {}).get("id") == native["creator"]["id"]
        and event.get("created_at") == created.get("created_at")
        and type(created.get("created_at")) is str
        and all(issue.get(k) == created.get(k) for k in ("id", "node_id", "number"))
        and "pull_request" not in issue
        and ctl.native_repository_name(
            (issue.get("repository") or {}).get("full_name"), config.repository
        )
        and ctl.native_api_url(
            issue.get("url"), config.repository, "/issues/" + native["subject"]["number"]
        ),
        "native cross-reference is not the exact original issue POST201",
    )
    return {
        "schema": "local-correction-observed-own-issue/v1",
        "before": before,
        "after": after,
        "response_sha256": ctl.digest(response),
        "native": native,
        "request_semantics_sha256": ctl.digest(semantic_request(request)),
    }


def _read_issue_transition(controller, custody, operation, request, *, required=False):
    controller._require_writer(operation, custody)
    key = phase_key(request, "issue_creation", None)
    name = "native-issue-transition-" + key + ".json"
    try:
        transition = custody.read(name)
    except FileNotFoundError:
        _need(not required, "protected own issue transition unavailable")
        return None
    intent = custody.read("native-intent-" + key + ".json")
    response = read_response(custody, intent)
    _need(
        intent.get("phase") == "issue_creation"
        and intent.get("request") == request
        and intent.get("key") == ctl.effect_key(request)
        and intent.get("source_record_ref") is not None
        and intent.get("owner_semantics_sha256") == ctl.digest(semantic_request(request))
        and transition
        == observed_issue_transition(
            intent["source_before"],
            response,
            transition["after"],
            request=request,
            payload=intent["payload"],
            config=controller.config,
        ),
        "protected native own issue transition differs",
    )
    _fresh_created(controller, transition["native"], response, "issue_creation")
    _need(
        _capture_source(controller, operation, request) == transition["after"],
        "source moved after protected own issue creation",
    )
    return transition


def retained_source_after(controller, custody, operation, request, original_after):
    transition = _read_issue_transition(controller, custody, operation, request)
    if transition is None:
        return original_after
    _need(
        transition["before"] == original_after,
        "own issue does not continue exact original instruction source version",
    )
    return transition["after"]


def project_retained_issue_ownership(controller, custody, operation, request, path, value):
    """Fixed native-reader projection, only of a protected *observed* own delta.

    All actual native bytes are retained and re-read before this projection.
    Public ownership collection still has no caller-provided exclusion API.
    Other subjects, old candidates and additional changes remain UNKNOWN.
    """
    if not path.startswith("/issues/" + request["source_issue"]["number"] + "/timeline?"):
        return value
    transition = _read_issue_transition(controller, custody, operation, request)
    if transition is None:
        return value
    _need(
        path == "/issues/" + request["source_issue"]["number"] + "/timeline?per_page=100&page=1"
        and len(transition["after"]["timeline"]) < 100
        and value == transition["after"]["timeline"],
        "protected own issue ownership projection outside complete native page",
    )
    return copy.deepcopy(transition["before"]["timeline"])


def create_source_issue_locked(controller, custody, operation, selection):
    """Automatic NoInc continuation selected by the original root capability.

    It reuses the already-held original writer. There is no public action/hash
    registration shortcut and no second queue phase, model or workflow launch.
    """
    _ordinary_native_effect_only()
    attempted = False
    stage = "issue_source_selection"
    controller._require_writer(operation, custody)
    _need(
        getattr(controller._writer_context, "native_selection", None) is None,
        "another native source selection active",
    )
    controller._writer_context.native_selection = selection
    try:
        facts = _source_selection(controller, operation)
        request = facts["request"]
        if request["incumbent"].get("kind") != "no_incumbent":
            return ctl.held(
                "existing incumbent preserved; issue creation not selected",
                status="native_issue_not_required",
                post_attempted=False,
            )
        stage = "issue_enrolled_read"
        before, entry, text = _enrolled(controller, custody, operation, ctl.effect_key(request))
        index_stage = next((s for s in entry["stages"] if s["phase"] == "issue_creation"), None)
        if index_stage is not None:
            stage = "issue_replay"
            _need(
                index_stage["state"] == "confirmed",
                "original issue stage unresolved; never repeat POST",
            )
            transition = _read_issue_transition(
                controller, custody, operation, request, required=True
            )
            _need(
                index_stage["result"] == transition["native"]["subject"],
                "confirmed own issue membership changed",
            )
            return ctl.held(
                "exact protected original issue reconciled; no POST or CAS",
                status="native_effect_confirmed",
                native=transition["native"],
                post_attempted=False,
                checkpoint_reconciled=True,
            )
        stage = "issue_plan_capture"
        plan = _make_plan(controller, operation, before, entry, "issue_creation", text)
        plan.update(
            source_record_ref=facts["source_record_ref"],
            seed_selector=facts["seed_selector"],
            source_before=_capture_source(controller, operation, request),
        )
        _need(
            len(plan["source_before"]["timeline"]) < 99,
            "own issue projection requires bounded complete single-page native history before POST",
        )
        import closer_correction_source_record as source_record

        lifecycle = source_record.operation_lifecycle_origin(controller, operation)
        if lifecycle is not None:
            plan["native_lifecycle"] = lifecycle
        key = plan["phase_key"]
        stage = "issue_plan_persist"
        custody.create("native-plan-" + key + ".json", plan)
        _need(custody.read("native-plan-" + key + ".json") == plan, "original issue plan changed")
        action = {
            "kind": "prepare",
            "key": entry["key"],
            "phase": "issue_creation",
            "intent_sha256": ctl.digest(plan),
            "selector": entry["admitted_selector"],
        }
        # Fixed source-owner derivation, under original campaign+writer custody.
        stage = "issue_prepare_register"
        controller._register_source_enrollment_locked(
            custody,
            operation,
            entry["admitted_selector"]["record_sha256"],
            ctl.digest(facts["decision"]),
            action,
        )
        stage = "issue_prepare_apply"
        prepared = controller._apply_locked(action, operation=operation, custody=custody)
        _need(
            prepared.get("status") == "checkpoint_confirmed", "issue prepare index outcome unknown"
        )
        stage = "issue_prepared_read"
        before, entry, _ = _enrolled(controller, custody, operation, entry["key"])
        stage = "issue_plan_revalidate"
        _plan(controller, custody, operation, before, entry, "issue_creation")
        intent = {
            **plan,
            "schema": "local-correction-native-intent/v1",
            "prepared_index": {
                "commit_sha": before["commit_sha"],
                "state_sha256": ctl.digest(before["data"]),
                "revision": before["data"]["revision"],
            },
        }
        stage = "issue_remaining_before_post"
        _remaining(request, operation)
        stage = "issue_post"
        attempted = True
        response, original = _perform_post(controller, custody, operation, intent, plan["payload"])
        stage = "issue_response_validate"
        identity = validate_creation(
            response,
            phase="issue_creation",
            payload=original["payload"],
            repository=controller.config.repository,
            config=controller.config,
        )
        _fresh_created(controller, identity, response, "issue_creation")
        stage = "issue_transition"
        transition = observed_issue_transition(
            original["source_before"],
            response,
            _capture_source(controller, operation, request),
            request=request,
            payload=original["payload"],
            config=controller.config,
        )
        custody.create("native-issue-transition-" + key + ".json", transition)
        stage = "issue_transition_read"
        _read_issue_transition(controller, custody, operation, request, required=True)
        stage = "issue_source_revalidate"
        _source_selection(controller, operation)
        proof = {
            "schema": "local-correction-native-origin/v1",
            "phase_key": key,
            "intent_sha256": ctl.digest(original),
            "response_sha256": ctl.digest(response),
            "native": identity,
        }
        stage = "issue_origin_persist"
        custody.create("native-origin-" + ctl.digest(proof) + ".json", proof)
        stage = "issue_confirm"
        outcome = controller._apply_locked(
            {
                "kind": "confirm",
                "key": entry["key"],
                "phase": "issue_creation",
                "proof_sha256": ctl.digest(proof),
                "result": identity["subject"],
                "selector": entry["admitted_selector"],
            },
            operation=operation,
            custody=custody,
        )
        if outcome.get("status") != "checkpoint_confirmed":
            from closer_correction_source_record import _failure_diagnostic

            # Keep only a fixed diagnostic code from the exact failed source
            # outcome. Acknowledged index state never repairs this UNKNOWN.
            reason = outcome.get("reason") if type(outcome) is dict else None
            return ctl.held(
                "original issue stage remains owned; reconcile original operation",
                post_attempted=attempted,
                failure_diagnostic=_failure_diagnostic(ctl.ControllerUnknown(reason), stage),
            )
        return ctl.held(
            "original issue POST201 confirmed; no coding owner or workflow authority",
            status="native_effect_confirmed",
            native=identity,
            proof_sha256=ctl.digest(proof),
            post_attempted=True,
        )
    except (
        ImportError,
        OSError,
        ValueError,
        KeyError,
        TypeError,
        AttributeError,
        RuntimeError,
        StopIteration,
        subprocess.TimeoutExpired,
    ) as error:
        from closer_correction_source_record import _failure_diagnostic

        return ctl.held(
            "original issue stage remains owned; reconcile original operation",
            post_attempted=attempted,
            failure_diagnostic=_failure_diagnostic(error, stage),
        )
    finally:
        if getattr(controller._writer_context, "native_selection", None) is selection:
            del controller._writer_context.native_selection
