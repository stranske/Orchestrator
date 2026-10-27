"""Inactive local adapter for Workflows b9de1194's private correction index.

Only an original same-thread queue callback with source-enrolled controller
configuration can use this adapter. The fixed state.evidence namespace and its
initial high-water must already be provisioned by that controller's source owner.
There is no bootstrap/reset API, credential loader or installed provider.
The optional final guard only gates the existing source offload; it never
constructs a coding task or starts a provider itself.
Local custody is not a global anchor across arbitrary controller roots. Loss of
this custody holds; exclusive-writer enforcement covers this fixed namespace.
"""

from __future__ import annotations

import base64
import copy
import fcntl
import hashlib
import inspect
import json
import math
import os
import re
import stat
import threading
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

import closer_correction_handoff as wire
import closer_queue as queue

SOURCE_CONTRACT = "b9de11947cc3092996774c6ce8f55b6373f1c520"
SCHEMA = "closer-correction-subject-index/v1"
BRANCH = "closer-correction-state"
_LOADED_SOURCE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
INDEX_PATH = ".github/closer-correction/index.json"
_INSPECTION_MAX_READ_BYTES = 64 * 1024 * 1024
PHASES = {"issue_creation", "autopilot_dispatch", "coding_pr_creation"}
_SHA = re.compile(r"[a-f0-9]{64}\Z")
_COMMIT = re.compile(r"[a-f0-9]{40}\Z")


class ControllerUnknown(ValueError):
    """Original custody or native identity is unknown; never authorize retry."""


def need(value, reason):
    if not value:
        raise ControllerUnknown(reason)


def canonical(value):
    """JS canonical bytes; validate a private ordered tree before one encode."""
    count = 0

    def ordered(item, depth=0):
        nonlocal count
        count += 1
        need(depth <= 32 and count <= 50000, "private JSON structure exceeds bounds")
        if type(item) is dict:
            need(all(type(k) is str for k in item), "private JSON key is not text")
            result = {}
            for key in sorted(item, key=lambda k: k.encode("utf-16-be")):
                checked_key = ordered(key, depth + 1)
                checked_value = ordered(item[key], depth + 1)
                result[checked_key] = checked_value
            return result
        if type(item) is list:
            return [ordered(v, depth + 1) for v in item]
        need(
            item is None or type(item) in {str, bool, int},
            "private JSON scalar unsupported",
        )
        if type(item) is int:
            need(abs(item) <= 2**53 - 1, "private JSON integer exceeds JS safe range")
        return item

    raw = json.dumps(
        ordered(value), ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    need(0 < len(raw) <= 1048576, "private JSON byte bound exceeded")
    return raw


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def source_digest(value):
    """Opaque Core identity; never passed off as the JS private JSON digest."""
    return hashlib.sha256(queue._canonical_bytes(value)).hexdigest()


def decode(raw):
    def pairs(rows):
        value = {}
        for k, v in rows:
            need(k not in value, "duplicate private JSON field")
            value[k] = v
        return value

    value = json.loads(raw, object_pairs_hook=pairs)
    need(canonical(value) == raw, "private JSON bytes are not canonical")
    return value


def exact(value, keys):
    need(
        type(value) is dict and set(value) == set(keys.split()),
        "unexpected private fields",
    )


def positive(value):
    return type(value) is int and 0 < value <= 2**53 - 1


def subject(value):
    exact(value, "kind number node_id")
    need(
        value["kind"] in {"issue", "pull"}
        and re.fullmatch(r"[1-9][0-9]{0,15}", value["number"])
        and type(value["node_id"]) is str
        and 0 < len(value["node_id"]) <= 256,
        "native subject identity missing",
    )
    return value["kind"] + ":" + value["node_id"]


def native_repository_name(value, canonical_wire_repo):
    return (
        type(value) is str
        and type(canonical_wire_repo) is str
        and canonical_wire_repo == canonical_wire_repo.lower()
        and re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value) is not None
        and value.casefold() == canonical_wire_repo.casefold()
    )


def native_api_url(value, canonical_wire_repo, suffix):
    """Only owner/repo spelling is case-insensitive; every other URI bit is exact."""
    if type(value) is not str or type(suffix) is not str:
        return False
    try:
        uri = urlsplit(value)
        if (
            uri.scheme != "https"
            or uri.netloc != "api.github.com"
            or uri.query
            or uri.fragment
            or uri.username is not None
            or uri.password is not None
        ):
            return False
        pieces = uri.path.split("/")
        return (
            len(pieces) >= 4
            and pieces[:2] == ["", "repos"]
            and native_repository_name(pieces[2] + "/" + pieces[3], canonical_wire_repo)
            and uri.path == "/repos/" + pieces[2] + "/" + pieces[3] + suffix
            and (not suffix or suffix.startswith("/"))
        )
    except (ValueError, TypeError):
        return False


def seed_selector(value, repository):
    exact(value, "repository run_id run_attempt artifact_id archive_sha256 member_sha256")
    need(
        value["repository"] == repository
        and all(positive(value[k]) for k in ("run_id", "run_attempt", "artifact_id"))
        and all(_SHA.fullmatch(value[k]) for k in ("archive_sha256", "member_sha256")),
        "original seed selector differs",
    )


def selector(value, repository):
    exact(value, "kind record_sha256 seed_selector owner_instruction")
    need(
        value["kind"] == "local_controller_record" and _SHA.fullmatch(value["record_sha256"]),
        "protected local record selector required",
    )
    seed_selector(value["seed_selector"], repository)
    row = value["owner_instruction"]
    exact(
        row,
        "repository issue_number issue_node_id comment_id comment_node_id updated_at body_sha256 writer_id",
    )
    need(
        row["repository"] == repository
        and re.fullmatch(r"[1-9][0-9]{0,15}", row["issue_number"])
        and positive(row["comment_id"])
        and positive(row["writer_id"])
        and all(
            type(row[k]) is str and 0 < len(row[k]) <= 256
            for k in ("issue_node_id", "comment_node_id")
        )
        and re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", row["updated_at"])
        and _SHA.fullmatch(row["body_sha256"]),
        "native instruction selector differs",
    )


def effect_key(request):
    return digest(
        {
            "schema": "closer-correction-effect/v1",
            "repository": request["target"]["repository"],
            "event_id": request["original_verifier"]["verification_event"]["event_id"],
            "finding_evidence": request["finding"]["evidence"],
            "source_issue": request["source_issue"],
            "scope": request["scope"],
        }
    )


def scope_digest(request):
    return digest(
        {
            "target": request["target"],
            "source_issue": request["source_issue"],
            "finding_evidence": request["finding"]["evidence"],
            "scope": request["scope"],
        }
    )


@dataclass(frozen=True)
class ControllerConfig:
    state_path: str
    repository: str
    repository_id: int
    epoch: str
    bootstrap_source_sha: str
    source_sha: str
    writer_id: int
    writer_login: str
    writer_type: str
    bootstrap_anchor_sha256: str
    max_bytes: int
    max_entries: int
    max_subjects: int
    consumers: dict = field(default_factory=dict)
    native_transport_entrypoint: str = ""
    native_transport_source_sha256: str = ""

    @property
    def namespace(self):
        return f"correction-index-{self.repository_id}-{self.epoch}"

    def source_binding(self):
        return asdict(self)


def validate_index(data, config):
    exact(
        data,
        "schema repository repository_id epoch bootstrap_source_sha revision previous_sha256 last_action entries",
    )
    need(
        data["schema"] == SCHEMA
        and data["repository"] == config.repository
        and data["repository_id"] == config.repository_id
        and data["epoch"] == config.epoch
        and data["bootstrap_source_sha"] == config.bootstrap_source_sha,
        "index repository/epoch/bootstrap differs",
    )
    need(
        type(data["revision"]) is int
        and data["revision"] >= 0
        and type(data["entries"]) is list
        and len(data["entries"]) <= config.max_entries,
        "index revision/count saturated",
    )
    need(
        (
            data["revision"] == 0
            and data["previous_sha256"] is None
            and data["last_action"] is None
            and not data["entries"]
        )
        or (
            data["revision"] > 0
            and _SHA.fullmatch(data["previous_sha256"] or "")
            and type(data["last_action"]) is dict
        ),
        "index predecessor missing",
    )
    keys, subjects, numbers = set(), set(), {}
    count = 0
    for entry in data["entries"]:
        exact(
            entry,
            "key request source_scope_digest admitted_selector subjects bindings stages",
        )
        request = entry["request"]
        wire.validate_request(request)
        need(
            request["target"]["repository"] == config.repository
            and entry["key"] == effect_key(request)
            and entry["source_scope_digest"] == scope_digest(request)
            and entry["key"] not in keys,
            "index request/key differs",
        )
        keys.add(entry["key"])
        selector(entry["admitted_selector"], config.repository)
        need(
            all(type(entry[k]) is list for k in ("subjects", "bindings", "stages"))
            and len(entry["subjects"]) >= 2,
            "subject enrollment incomplete",
        )
        for row in entry["subjects"]:
            key = subject(row)
            need(
                key not in subjects and numbers.get(row["number"], key) == key,
                "competing native subject membership",
            )
            subjects.add(key)
            numbers[row["number"]] = key
            count += 1
        source_pulls = [
            s
            for s in entry["subjects"]
            if s["kind"] == "pull" and s["number"] == request["target"]["pr"]
        ]
        need(len(source_pulls) == 1, "source PR identity missing")
        initial = [
            source_pulls[0],
            {
                "kind": "issue",
                "number": request["source_issue"]["number"],
                "node_id": request["source_issue"]["node_id"],
            },
        ]
        if request["incumbent"].get("kind") != "no_incumbent":
            initial.append(
                {
                    "kind": "issue",
                    "number": request["incumbent"]["issue"]["number"],
                    "node_id": request["incumbent"]["issue"]["node_id"],
                }
            )
        need(all(s in entry["subjects"] for s in initial), "source/incumbent omitted")
        for binding in entry["bindings"]:
            exact(binding, "subject selector proof_sha256")
            subject(binding["subject"])
            selector(binding["selector"], config.repository)
            need(
                binding["subject"] in entry["subjects"] and _SHA.fullmatch(binding["proof_sha256"]),
                "subject binding changed",
            )
        phases = set()
        for stage in entry["stages"]:
            exact(
                stage,
                "phase state intent_sha256 intent_selector proof_sha256 result result_selector",
            )
            need(
                stage["phase"] in PHASES
                and stage["phase"] not in phases
                and _SHA.fullmatch(stage["intent_sha256"])
                and stage["state"] in {"prepared", "outcome_unknown", "confirmed"},
                "invalid stage",
            )
            phases.add(stage["phase"])
            selector(stage["intent_selector"], config.repository)
            if stage["state"] == "confirmed":
                selector(stage["result_selector"], config.repository)
                need(_SHA.fullmatch(stage["proof_sha256"]), "native proof missing")
                if stage["phase"] == "autopilot_dispatch":
                    exact(stage["result"], "workflow run_id run_attempt issue")
                    subject(stage["result"]["issue"])
                    need(
                        stage["result"]["workflow"] == ".github/workflows/agents-auto-pilot.yml"
                        and all(
                            re.fullmatch(r"[1-9][0-9]{0,15}", stage["result"][k])
                            for k in ("run_id", "run_attempt")
                        )
                        and stage["result"]["issue"] in entry["subjects"]
                        and stage["result"]["issue"]["kind"] == "issue",
                        "native continuation identity missing",
                    )
                else:
                    subject(stage["result"])
                    need(
                        stage["result"] in entry["subjects"]
                        and stage["result"]["kind"]
                        == ("issue" if stage["phase"] == "issue_creation" else "pull"),
                        "native creation identity missing",
                    )
            else:
                need(
                    all(stage[k] is None for k in ("proof_sha256", "result", "result_selector")),
                    "unconfirmed stage claims result",
                )
        allowed = (
            initial
            + [b["subject"] for b in entry["bindings"]]
            + [
                s["result"]
                for s in entry["stages"]
                if s["state"] == "confirmed" and s["phase"] != "autopilot_dispatch"
            ]
        )
        need(
            all(s in allowed for s in entry["subjects"]),
            "additional subject lacks native proof",
        )
    need(
        count <= config.max_subjects and len(canonical(data)) <= config.max_bytes,
        "index saturation; no truncation",
    )
    return data


def transition(data, action, config):
    validate_index(data, config)
    result = copy.deepcopy(data)
    entries = result["entries"]
    kind = action.get("kind")
    if kind == "enroll":
        exact(action, "kind request admitted_selector subjects")
        request = action["request"]
        wire.validate_request(request)
        selector(action["admitted_selector"], config.repository)
        rows = action["subjects"]
        need(type(rows) is list, "initial subjects missing")
        for s in rows:
            subject(s)
        pulls = [s for s in rows if s["kind"] == "pull" and s["number"] == request["target"]["pr"]]
        need(len(pulls) == 1, "exact source PR unavailable")
        expected = [
            pulls[0],
            {
                "kind": "issue",
                **{k: request["source_issue"][k] for k in ("number", "node_id")},
            },
        ]
        if request["incumbent"].get("kind") != "no_incumbent":
            expected.append(
                {
                    "kind": "issue",
                    **{k: request["incumbent"]["issue"][k] for k in ("number", "node_id")},
                }
            )
        expected = list({subject(s): s for s in expected}.values())
        need(
            len(rows) == len(expected) and all(s in rows for s in expected),
            "initial scope omitted or expanded",
        )
        existing = next((e for e in entries if e["key"] == effect_key(request)), None)
        if existing:
            need(
                existing["request"] == request and all(s in existing["subjects"] for s in rows),
                "enrollment rebound",
            )
            return data
        entries.append(
            {
                "key": effect_key(request),
                "request": copy.deepcopy(request),
                "source_scope_digest": scope_digest(request),
                "admitted_selector": copy.deepcopy(action["admitted_selector"]),
                "subjects": copy.deepcopy(rows),
                "bindings": [],
                "stages": [],
            }
        )
    else:
        entry = next((e for e in entries if e["key"] == action.get("key")), None)
        need(entry is not None, "effect not enrolled")
        if kind == "bind":
            exact(action, "kind key subject selector proof_sha256")
            key = subject(action["subject"])
            selector(action["selector"], config.repository)
            need(
                action["subject"]["kind"] == "pull" and _SHA.fullmatch(action["proof_sha256"]),
                "native coding PR proof missing",
            )
            binding = {k: copy.deepcopy(action[k]) for k in ("subject", "selector", "proof_sha256")}
            old = next((s for s in entry["subjects"] if subject(s) == key), None)
            if old:
                need(
                    old == action["subject"] and binding in entry["bindings"],
                    "native binding rebound",
                )
                return data
            entry["subjects"].append(copy.deepcopy(action["subject"]))
            entry["bindings"].append(binding)
        else:
            phase = action.get("phase")
            need(phase in PHASES, "unsupported phase")
            stage = next((s for s in entry["stages"] if s["phase"] == phase), None)
            if kind == "prepare":
                exact(action, "kind key phase intent_sha256 selector")
                selector(action["selector"], config.repository)
                need(
                    stage is None and _SHA.fullmatch(action["intent_sha256"]),
                    "stage exists; reconcile never retry",
                )
                no_owner = entry["request"]["incumbent"].get("kind") == "no_incumbent"
                need(
                    phase != "issue_creation" or no_owner,
                    "existing owner cannot create issue",
                )
                need(
                    phase == "issue_creation"
                    or not no_owner
                    or any(
                        s["phase"] == "issue_creation" and s["state"] == "confirmed"
                        for s in entry["stages"]
                    ),
                    "confirmed issue required",
                )
                entry["stages"].append(
                    {
                        "phase": phase,
                        "state": "prepared",
                        "intent_sha256": action["intent_sha256"],
                        "intent_selector": copy.deepcopy(action["selector"]),
                        "proof_sha256": None,
                        "result": None,
                        "result_selector": None,
                    }
                )
            elif kind == "unknown":
                exact(action, "kind key phase")
                need(
                    stage is not None and stage["state"] != "confirmed",
                    "unknown cannot erase confirmed identity",
                )
                if stage["state"] == "outcome_unknown":
                    return data
                stage["state"] = "outcome_unknown"
            elif kind == "confirm":
                exact(action, "kind key phase proof_sha256 result selector")
                selector(action["selector"], config.repository)
                need(
                    stage is not None and _SHA.fullmatch(action["proof_sha256"]),
                    "original intent missing",
                )
                if stage["state"] == "confirmed":
                    need(
                        stage["result"] == action["result"]
                        and stage["proof_sha256"] == action["proof_sha256"]
                        and stage["result_selector"] == action["selector"],
                        "native result rebound",
                    )
                    return data
                if phase == "autopilot_dispatch":
                    exact(action["result"], "workflow run_id run_attempt issue")
                    incumbent = entry["request"]["incumbent"]
                    issue = (
                        next(
                            (
                                s["result"]
                                for s in entry["stages"]
                                if s["phase"] == "issue_creation" and s["state"] == "confirmed"
                            ),
                            None,
                        )
                        if incumbent.get("kind") == "no_incumbent"
                        else {
                            "kind": "issue",
                            **{k: incumbent["issue"][k] for k in ("number", "node_id")},
                        }
                    )
                    need(action["result"]["issue"] == issue, "continuation issue differs")
                else:
                    subject(action["result"])
                    need(
                        action["result"]["kind"]
                        == ("issue" if phase == "issue_creation" else "pull")
                        and not any(
                            subject(s) == subject(action["result"]) for s in entry["subjects"]
                        ),
                        "creation cannot adopt existing subject",
                    )
                    entry["subjects"].append(copy.deepcopy(action["result"]))
                stage.update(
                    state="confirmed",
                    proof_sha256=action["proof_sha256"],
                    result=copy.deepcopy(action["result"]),
                    result_selector=copy.deepcopy(action["selector"]),
                )
            else:
                raise ControllerUnknown("unsupported index operation")
    result.update(
        revision=data["revision"] + 1,
        previous_sha256=digest(data),
        last_action=copy.deepcopy(action),
    )
    return validate_index(result, config)


def write_intent(before, after, action, config, owner):
    stable = {
        "schema": "closer-correction-index-write-key/v1",
        "repository": config.repository,
        "repository_id": config.repository_id,
        "epoch": config.epoch,
        "expected_commit_sha": before["commit_sha"],
        "expected_blob_sha": before["blob_sha"],
        "before_sha256": digest(before["data"]),
        "after_sha256": digest(after),
        "action_sha256": digest(action),
    }
    return {
        **stable,
        "schema": "closer-correction-index-write-intent/v1",
        "transaction_key": digest(stable),
        "owner": owner,
    }


def blob_sha(raw):
    return hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()


def _conditional_write_diagnostic(error):
    """Bounded source diagnostics only; never raw transport/env/credential text."""
    name = type(error).__name__
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,79}", name):
        name = "Exception"
    safe_reasons = {
        "maintenance PUT is not the fixed current confirmation CAS",
        "maintenance CAS lacks exact new WAL or already submitted; never repeat",
        "actual canonical native-entry/tick frame unavailable",
        "actual fresh confirm-only callback/writer selection differs or expired",
        "native expected commit stale",
        "native transport only permits exact Contents CAS",
        "installed source-owned native transport absent or changed",
    }
    reason = (
        str(error) if type(error).__name__ in {"ControllerUnknown", "MaintenanceUnknown"} else ""
    )
    return {
        "schema": "local-correction-conditional-write-diagnostic/v1",
        "exception_type": name,
        "reason": reason if reason in safe_reasons else "conditional native transport raised",
        "known_not_written": False,
        "retry_authorized": False,
    }


class NativeIndexStore:
    """Fixed native Contents CAS; injected HTTP only, no credentials or retry."""

    def __init__(self, config, request: Callable, *, controller=None):
        need(callable(request), "native HTTP transport absent")
        self.config, self.request, self.controller = config, request, controller

    def call(self, method, path, body=None):
        need(
            type(self.controller) is IndexController and self.controller.store is self,
            "native store requires original installed controller",
        )
        current = getattr(queue._callback_threads, "invocation", None)
        need(type(current) is dict, "native store original callback absent")
        self.controller._require_writer(
            current["operation"],
            getattr(self.controller._writer_context, "custody", None),
        )
        source = self.request.__func__ if inspect.ismethod(self.request) else self.request
        source_path = Path(inspect.getsourcefile(source) or "")
        need(
            inspect.isfunction(source)
            and source.__module__ + "." + source.__qualname__
            == self.config.native_transport_entrypoint
            and source_path.is_file()
            and not source_path.is_symlink()
            and hashlib.sha256(source_path.read_bytes()).hexdigest()
            == self.config.native_transport_source_sha256,
            "installed source-owned native transport absent or changed",
        )
        return self.request(
            method,
            "/repos/" + self.config.repository + path,
            body,
            {"maxRetries": 0, "timeout": 15000},
        )

    def repository(self):
        row = self.call("GET", "")
        need(
            positive(row.get("id"))
            and row["id"] == self.config.repository_id
            and native_repository_name(row.get("full_name"), self.config.repository),
            "native repository identity changed",
        )

    def head(self):
        row = self.call("GET", "/git/ref/heads/" + BRANCH)
        need(
            row.get("ref") == "refs/heads/" + BRANCH
            and native_api_url(row.get("url"), self.config.repository, "/git/refs/heads/" + BRANCH)
            and row.get("object", {}).get("type") == "commit"
            and _COMMIT.fullmatch(row["object"].get("sha", "")),
            "native branch identity changed",
        )
        return row["object"]["sha"]

    def content(self, commit):
        row = self.call("GET", "/contents/" + INDEX_PATH + "?ref=" + commit)
        need(
            row.get("type") == "file"
            and row.get("path") == INDEX_PATH
            and row.get("encoding") == "base64"
            and type(row.get("size")) is int
            and 0 < row["size"] <= self.config.max_bytes,
            "native contents malformed",
        )
        encoded = row["content"].replace("\n", "").replace("\r", "")
        raw = base64.b64decode(encoded, validate=True)
        need(
            base64.b64encode(raw).decode() == encoded
            and len(raw) == row["size"]
            and blob_sha(raw) == row.get("sha"),
            "native blob bytes differ",
        )
        return raw, row["sha"]

    def read(self):
        self.repository()
        head = self.head()
        row = self.call("GET", "/git/commits/" + head)
        need(
            row.get("sha") == head and len(row.get("parents", [])) == 1,
            "native parent missing",
        )
        parent = row["parents"][0]["sha"]
        need(_COMMIT.fullmatch(parent) and parent != head, "native parent malformed")
        raw, blob = self.content(head)
        data = validate_index(decode(raw), self.config)
        prior = None
        try:
            before, before_blob = self.content(parent)
            prior = {
                "commit_sha": parent,
                "blob_sha": before_blob,
                "data": validate_index(decode(before), self.config),
            }
        except Exception as error:
            if getattr(error, "status", None) != 404:
                raise
        if prior:
            need(
                data == prior["data"]
                or (
                    data["revision"] == prior["data"]["revision"] + 1
                    and data["previous_sha256"] == digest(prior["data"])
                    and transition(prior["data"], data["last_action"], self.config) == data
                ),
                "native parent transition changed",
            )
        else:
            need(data["revision"] == 0, "missing prior index is not bootstrap")
        need(self.head() == head, "native branch moved during read")
        return {
            "repository": self.config.repository,
            "branch": BRANCH,
            "path": INDEX_PATH,
            "commit_sha": head,
            "parent_commit_sha": parent,
            "blob_sha": blob,
            "data": data,
            "parent": prior,
        }

    def compare_and_swap(self, before, after):
        need(self.head() == before["commit_sha"], "native expected commit stale")
        raw = canonical(after)
        row = self.call(
            "PUT",
            "/contents/" + INDEX_PATH,
            {
                "branch": BRANCH,
                "message": "chore: checkpoint correction subject index",
                "sha": before["blob_sha"],
                "content": base64.b64encode(raw).decode(),
            },
        )
        need(
            row.get("content", {}).get("sha") == blob_sha(raw)
            and row.get("content", {}).get("path") == INDEX_PATH
            and row.get("content", {}).get("type") == "file"
            and _COMMIT.fullmatch(row.get("commit", {}).get("sha", "")),
            "native write acknowledgement unknown",
        )
        return row["commit"]["sha"], row["content"]["sha"]


class _Custody:
    """All names are fixed or digest-derived, relative to one physical dir fd."""

    def __init__(self, config):
        state = Path(config.state_path)
        root = Path(str(state) + ".evidence")
        need(
            state.is_absolute() and state.resolve(strict=True) == state,
            "fixed controller state path missing",
        )
        for path in (root, root / config.namespace):
            need(
                path.resolve(strict=True) == path
                and not any(p.is_symlink() for p in (path, *path.parents)),
                "controller custody path redirected",
            )
        self.path = root / config.namespace
        self.fd = os.open(self.path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        self.identity = os.fstat(self.fd)
        need(
            self.identity.st_uid == os.getuid() and not self.identity.st_mode & 0o022,
            "controller custody ownership changed",
        )

    def close(self):
        os.close(self.fd)

    def check(self):
        current = self.path.stat(follow_symlinks=False)
        need(
            not self.path.is_symlink()
            and (current.st_dev, current.st_ino) == (self.identity.st_dev, self.identity.st_ino),
            "controller custody directory replaced",
        )

    def read(self, name):
        self.check()
        need(re.fullmatch(r"[a-z0-9.-]+", name), "custody name invalid")
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self.fd)
        with os.fdopen(fd, "rb") as stream:
            meta = os.fstat(stream.fileno())
            need(
                stat.S_ISREG(meta.st_mode)
                and meta.st_uid == os.getuid()
                and meta.st_nlink == 1
                and not meta.st_mode & 0o077
                and 0 < meta.st_size <= 1048576,
                "custody file ownership/type/bounds changed",
            )
            budget = getattr(self, "_inspection_read_budget", None)
            if budget is not None:
                need(
                    meta.st_size <= budget[0],
                    "read-only custody aggregate byte budget exceeded",
                )
                budget[0] -= meta.st_size
            raw = stream.read(1048577)
            need(len(raw) == meta.st_size, "custody file changed")
        return decode(raw)

    def create(self, name, value):
        self.check()
        need(re.fullmatch(r"[a-z0-9.-]+", name), "custody name invalid")
        raw = canonical(value)
        try:
            fd = os.open(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=self.fd,
            )
        except FileExistsError:
            need(self.read(name) == value, "existing custody bytes differ")
            return False
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.fsync(self.fd)
        return True


class IndexController:
    """Concrete same-thread callback, local writer lock, custody and native joins."""

    def __init__(
        self,
        config: ControllerConfig,
        request: Callable | None = None,
        *,
        installation=None,
    ):
        need(type(config) is ControllerConfig, "source controller configuration missing")
        need(
            re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", config.repository)
            and config.repository == config.repository.lower()
            and positive(config.repository_id)
            and _SHA.fullmatch(config.epoch)
            and _SHA.fullmatch(config.bootstrap_anchor_sha256)
            and _COMMIT.fullmatch(config.source_sha)
            and _COMMIT.fullmatch(config.bootstrap_source_sha)
            and positive(config.writer_id)
            and config.writer_type in {"User", "Bot"}
            and bool(config.writer_login)
            and all(
                positive(v) for v in (config.max_bytes, config.max_entries, config.max_subjects)
            )
            and config.max_bytes <= 1048576,
            "source controller configuration invalid",
        )
        self.config = copy.deepcopy(config)
        self.installation = installation
        self._writer_context = threading.local()
        if request is None:
            import closer_correction_native_effects as effects

            native = effects.NativeHTTPTransport(installation)
            self.store = NativeIndexStore(self.config, native.request, controller=self)
            native.bind(self)
        else:
            self.store = NativeIndexStore(self.config, request, controller=self)

    def _source_installation(self):
        try:
            import closer_source_installation as source
        except ImportError as error:
            raise ControllerUnknown("source-owned installation factory unavailable") from error
        need(
            source.validate_source_installation(
                self.installation,
                _scope=getattr(self._writer_context, "validation_scope", None),
            )
            and self.installation.config == self.config.source_binding(),
            "actual factory-issued signed source installation required",
        )
        pinned = self.installation.source_closure.get("closer_correction_controller.py")
        path = Path(__file__)
        need(
            type(pinned) is dict
            and str(path.resolve()) == pinned.get("path")
            and pinned.get("sha256") == _LOADED_SOURCE_SHA256
            and hashlib.sha256(path.read_bytes()).hexdigest() == _LOADED_SOURCE_SHA256,
            "loaded native controller source differs from signed installation",
        )
        return self.installation

    @staticmethod
    def _physical_state(path, reason):
        path = Path(path)
        need(
            path.is_absolute()
            and path.resolve(strict=True) == path
            and not any(p.is_symlink() for p in (path, *path.parents)),
            reason,
        )
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            meta = os.fstat(stream.fileno())
            need(
                stat.S_ISREG(meta.st_mode)
                and meta.st_uid == os.getuid()
                and not meta.st_mode & 0o022
                and 0 < meta.st_size <= 4 * 1048576,
                reason,
            )
            raw = stream.read(4 * 1048576 + 1)
            need(len(raw) == meta.st_size, reason)
            return json.loads(raw)

    def live(self, operation):
        installation = self._source_installation()
        current = getattr(queue._callback_threads, "invocation", None)
        need(
            type(current) is dict and queue._currently_invoking(current.get("target"), operation),
            "original same-thread queue callback required",
        )
        frontier = installation.finite_frontier
        need(
            type(frontier) is dict
            and operation.get("finite_frontier") == frontier
            and current["target"] in frontier.get("targets", [])
            and current["target"].split("#", 1)[0].casefold() == self.config.repository.casefold(),
            "original queue is outside authenticated full source frontier",
        )
        timeout = installation.request_timeout_seconds
        started, deadline = operation.get("request_started_at"), operation.get("deadline")
        need(
            all(
                type(v) in (int, float) and math.isfinite(v)
                for v in (timeout, started, deadline, operation.get("timeout_seconds"))
            )
            and 0 < deadline - started <= timeout
            and 0 <= operation["timeout_seconds"] <= timeout,
            "original queue request exceeds authenticated source budget",
        )
        projection = getattr(installation, "policy_projection", None)
        need(
            type(projection) is dict
            and projection.get("purpose") == "frontier-correlation-only"
            and projection.get("controller_sha256") == digest(self.config.source_binding())
            and projection.get("installation_sha256")
            == operation.get("binding", {}).get("correction_installation_sha256")
            and operation.get("binding", {}).get("correction_controller_sha256")
            == projection["controller_sha256"],
            "original operation does not select this controller",
        )
        # Shared source anchor and per-target queue provenance are distinct.
        # The latter is stamped only by the real queue submit path, not args.
        state = self._physical_state(self.config.state_path, "shared controller state redirected")
        need(
            type(current.get("state_path")) is str,
            "actual queue callback state path unavailable",
        )
        submitted = self._physical_state(
            current["state_path"], "actual callback queue state redirected"
        )
        registration = state.get("correction_controller")
        need(
            type(registration) is dict
            and registration.get("config") == self.config.source_binding(),
            "actual controller state binding absent",
        )
        bucket = submitted
        if operation.get("finite_frontier") is not None:
            bucket = submitted["frontiers"][operation["finite_frontier"]["frontier_id"]]
        row = bucket.get("items", {}).get(current["target"], {})
        need(
            row.get("operation") == operation
            and source_digest(row["operation"]) == source_digest(operation),
            "physical original queue operation differs",
        )
        return {
            "kind": "local_controller",
            "operation_id": operation["id"],
            "operation_sha256": hashlib.sha256(queue._canonical_bytes(operation)).hexdigest(),
            "process_id": os.getpid(),
            "thread_id": str(threading.get_ident()),
            "target": current["target"],
        }, registration

    @contextmanager
    def fenced(self, operation):
        self.live(operation)
        custody = _Custody(self.config)
        lock = None
        try:
            need(
                custody.read("namespace.json")
                == {
                    "schema": "local-correction-controller-namespace/v1",
                    "config_sha256": digest(self.config.source_binding()),
                },
                "provisioned namespace differs",
            )
            lock = os.open(
                "writer.lock",
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
                0o600,
                dir_fd=custody.fd,
            )
            meta = os.fstat(lock)
            need(
                stat.S_ISREG(meta.st_mode)
                and meta.st_uid == os.getuid()
                and meta.st_nlink == 1
                and not meta.st_mode & 0o077,
                "writer lock redirected",
            )
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.live(operation)
            need(
                getattr(self._writer_context, "custody", None) is None,
                "original writer context already active",
            )
            self._writer_context.custody = custody
            try:
                import closer_source_installation as source

                with source.original_writer_validation_scope(self, operation, custody, lock):
                    yield custody
                    self.live(operation)
            finally:
                del self._writer_context.custody
        finally:
            if lock is not None:
                os.close(lock)
            custody.close()

    def _require_writer(self, operation, custody):
        need(
            type(custody) is _Custody and getattr(self._writer_context, "custody", None) is custody,
            "exact original same-thread writer custody required",
        )
        self.live(operation)
        custody.check()

    def _full_writer_validation(self, operation, custody):
        """Full original factory checks at every destructive native boundary."""
        self._require_writer(operation, custody)
        scope = getattr(self._writer_context, "validation_scope", None)
        if scope is None:
            self._source_installation()
        else:
            scope.full()
        self._require_writer(operation, custody)

    def _native_effect_source(self):
        import closer_correction_native_effects as effects

        installation = self._source_installation()
        path = Path(inspect.getsourcefile(effects.validate_index_effect_action) or "")
        pinned = installation.source_closure.get("closer_correction_native_effects.py")
        need(
            type(pinned) is dict
            and path.is_file()
            and not path.is_symlink()
            and str(path.resolve()) == pinned.get("path")
            and pinned.get("sha256") == effects._LOADED_SOURCE_SHA256
            and hashlib.sha256(path.read_bytes()).hexdigest() == effects._LOADED_SOURCE_SHA256,
            "fixed installed native effect reader source unavailable or changed",
        )
        return effects

    def anchor(self, custody):
        names = sorted(n for n in os.listdir(custody.fd) if n.startswith("highwater-"))
        need(
            names and names == [f"highwater-{i:016d}.json" for i in range(len(names))],
            "high-water loss or gap",
        )
        previous = None
        for i, name in enumerate(names):
            row = custody.read(name)
            exact(
                row,
                "revision commit_sha blob_sha state_sha256 previous_anchor_sha256 result_name result_sha256",
            )
            need(
                row["revision"] == i
                and _COMMIT.fullmatch(row["commit_sha"])
                and _COMMIT.fullmatch(row["blob_sha"])
                and _SHA.fullmatch(row["state_sha256"]),
                "high-water identity differs",
            )
            if i == 0:
                need(
                    digest(row) == self.config.bootstrap_anchor_sha256
                    and row["previous_anchor_sha256"] is None
                    and row["result_name"] is None
                    and row["result_sha256"] is None,
                    "independent provisioned anchor differs",
                )
            else:
                need(
                    row["previous_anchor_sha256"] == digest(previous)
                    and digest(custody.read(row["result_name"])) == row["result_sha256"],
                    "high-water result custody missing",
                )
            previous = row
        # A result/intent beyond the last high-water is an unresolved transaction,
        # not permission to accept a rolled-back index or repeat its write.
        return previous

    def read_bound(self, custody):
        anchor = self.anchor(custody)
        current = self.store.read()
        need(
            (
                anchor["commit_sha"],
                anchor["blob_sha"],
                anchor["state_sha256"],
                anchor["revision"],
            )
            == (
                current["commit_sha"],
                current["blob_sha"],
                digest(current["data"]),
                current["data"]["revision"],
            ),
            "native index differs from independent current high-water",
        )
        names = os.listdir(custody.fd)
        for name in names:
            if name.startswith("intent-"):
                key = name[len("intent-") : -len(".json")]
                need(
                    "result-" + key + ".json" in names,
                    "unresolved original intent; reconcile only",
                )
            if name.startswith("result-"):
                result = custody.read(name)
                need(
                    result["after"]["data"]["revision"] <= anchor["revision"],
                    "result lost its high-water; reconcile only",
                )
        return current

    def authenticate(self, selected, request, custody, operation):
        selector(selected, self.config.repository)
        _, registration = self.live(operation)
        key = selected["record_sha256"]
        need(
            key in registration.get("admission_records", {}),
            "record not source-enrolled in actual controller",
        )
        record = custody.read("admission-" + key + ".json")
        need(
            digest(record) == key
            and record.get("schema") == "local-correction-admission/v1"
            and record.get("request") == request
            and record.get("seed_selector") == selected["seed_selector"]
            and record.get("owner_instruction") == selected["owner_instruction"],
            "protected original admission differs",
        )
        import closer_correction_decision as decisions
        import dispatcher

        arguments = record["materializer"]
        need(
            arguments.get("original_request") == request
            and arguments.get("receipt_root") == str(dispatcher.DISPATCH_LOG_DIR),
            "source decision receipt context differs",
        )
        import closer_correction_maintenance as maintenance

        materialized = maintenance.retained_materialization(self, custody, operation, record)
        if materialized is None:
            import closer_coding_worker

            materialized = closer_coding_worker.retained_materialization(
                self, custody, operation, record
            )
        if materialized is None:
            materialized = decisions.materialize_selected(**arguments)
        need(
            materialized.get("status") == "materialized"
            and digest(materialized["decision"]) == registration["admission_records"][key]
            and materialized["decision"]["request_sha256"] == request["request_sha256"],
            "original source decision unavailable",
        )
        native = selected["owner_instruction"]
        self.store.repository()
        parent = self.store.call("GET", "/issues/" + native["issue_number"])
        comment = self.store.call("GET", "/issues/comments/" + str(native["comment_id"]))
        need(
            str(parent.get("number")) == native["issue_number"]
            and parent.get("node_id") == native["issue_node_id"]
            and native_api_url(
                parent.get("url"),
                self.config.repository,
                "/issues/" + native["issue_number"],
            )
            and parent.get("state") == "open"
            and comment.get("id") == native["comment_id"]
            and comment.get("node_id") == native["comment_node_id"]
            and native_api_url(
                comment.get("issue_url"),
                self.config.repository,
                "/issues/" + native["issue_number"],
            )
            and comment.get("updated_at") == native["updated_at"]
            and type(comment.get("body")) is str
            and hashlib.sha256(comment["body"].encode()).hexdigest() == native["body_sha256"]
            and comment.get("user", {}).get("id") == native["writer_id"] == self.config.writer_id
            and comment["user"].get("login") == self.config.writer_login
            and comment["user"].get("type") == self.config.writer_type,
            "fresh native owner instruction identity/version/body/writer differs",
        )
        self.live(operation)
        return materialized

    def fresh_issue_context(self, request, record, registration):
        """Require fresh full native bodies, versions and existing writer assignment.

        Full-body equality deliberately holds on every requirement edit instead
        of guessing whether a changed Markdown section is semantically relevant.
        The incumbent snapshot is distinct from the original source issue.
        """
        contexts = []
        source = record.get("original_source_context")
        incumbent = request["incumbent"]
        original_incumbent = record.get("original_incumbent_context")
        expected = [(source, request["source_issue"], False)]
        need(
            type(source) is dict and source.get("body") == record["materializer"]["source_body"],
            "immutable original source context missing",
        )
        if incumbent.get("kind") == "no_incumbent":
            need(original_incumbent is None, "unexpected original incumbent context")
        else:
            need(
                incumbent["owner"] == registration["owner_login"]
                and type(original_incumbent) is dict,
                "immutable original incumbent context/owner missing",
            )
            expected.append((original_incumbent, incumbent["issue"], True))
        for context, identity, assigned in expected:
            exact(
                context,
                "repository number node_id updated_at body_sha256 body required_tests"
                + (" assignees" if assigned else ""),
            )
            need(
                context["repository"] == self.config.repository
                and all(context[k] == identity[k] for k in ("number", "node_id", "body_sha256"))
                and type(context["body"]) is str
                and hashlib.sha256(context["body"].encode()).hexdigest() == identity["body_sha256"]
                and context["required_tests"] == request["required_tests"]
                and type(context["updated_at"]) is str
                and context["updated_at"],
                "immutable original issue body/requirements differ",
            )
            native = self.store.call("GET", "/issues/" + identity["number"])
            need(
                str(native.get("number")) == identity["number"]
                and native.get("node_id") == identity["node_id"]
                and native_api_url(
                    native.get("url"),
                    self.config.repository,
                    "/issues/" + identity["number"],
                )
                and native.get("state") == "open"
                and not native.get("pull_request")
                and type(native.get("body")) is str
                and native["body"] == context["body"]
                and (
                    native.get("updated_at") == context["updated_at"]
                    or (not assigned and record.get("own_instruction") is not None)
                ),
                "fresh native source/incumbent body/version/requirements unavailable or changed",
            )
            if not assigned and record.get("own_instruction") is not None:
                current = getattr(queue._callback_threads, "invocation", None)
                need(type(current) is dict, "native source version original callback unavailable")
                effects = self._native_effect_source()
                effects.validate_retained_source_version(
                    self,
                    getattr(self._writer_context, "custody", None),
                    current["operation"],
                    record,
                    native,
                )
            if assigned:

                def assignments(rows):
                    need(
                        type(rows) is list and 1 <= len(rows) <= 32,
                        "native incumbent assignees unavailable",
                    )
                    selected = []
                    for row in rows:
                        need(
                            type(row) is dict
                            and positive(row.get("id"))
                            and type(row.get("login")) is str
                            and row["login"]
                            and row.get("type") in {"User", "Bot"},
                            "native incumbent assignee identity unavailable",
                        )
                        selected.append({k: row[k] for k in ("id", "login", "type")})
                    need(
                        len({r["id"] for r in selected}) == len(selected),
                        "duplicate incumbent assignee",
                    )
                    return sorted(selected, key=lambda row: row["id"])

                original = assignments(context["assignees"])
                need(
                    assignments(native.get("assignees")) == original
                    and len(original) == 1
                    and original[0]["login"] == incumbent["owner"],
                    "fresh native incumbent assignment changed",
                )
            contexts.append(context)
        return source, original_incumbent

    def preflight_source_enrollment_locked(self, custody, operation):
        """Refuse unsupported shared campaign storage before first native POST."""
        import closer_campaign as campaign

        self._require_writer(operation, custody)
        store = campaign.Campaign(self.config.state_path)
        lock = os.open(
            store.lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600
        )
        try:
            meta = os.fstat(lock)
            need(
                stat.S_ISREG(meta.st_mode)
                and meta.st_uid == os.getuid()
                and meta.st_nlink == 1
                and not meta.st_mode & 0o077,
                "shared campaign lock redirected before publication",
            )
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._require_writer(operation, custody)
            doc = store._read_unlocked()
            need(
                type(doc) is dict
                and doc
                == self._physical_state(
                    self.config.state_path, "shared campaign changed before publication"
                ),
                "shared campaign unavailable before publication",
            )
            registration = doc.get("correction_controller")
            need(
                type(registration) is dict
                and registration.get("config") == self.config.source_binding()
                and type(registration.get("admission_records", {})) is dict
                and type(registration.get("actions", [])) is list
                and len(registration.get("admission_records", {})) < self.config.max_entries
                and len(registration.get("actions", [])) < 4 * self.config.max_entries,
                "shared source enrollment metadata unavailable before publication",
            )
            self._require_writer(operation, custody)
        finally:
            os.close(lock)

    def _register_source_enrollment_locked(
        self, custody, operation, record_key, decision_sha, action
    ):
        """Update source-owned metadata using the existing campaign lock protocol.

        This private method is reached only after fixed SourceSelection and
        original native-201 validation. The campaign lock is nonblocking; a
        competing writer holds rather than extending this original operation.
        """
        from closer_correction_maintenance import assert_offload_allowed

        assert_offload_allowed()
        import closer_campaign as campaign

        self._require_writer(operation, custody)
        path = Path(self.config.state_path)
        store = campaign.Campaign(path)
        lock = os.open(
            store.lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600
        )
        try:
            meta = os.fstat(lock)
            need(
                stat.S_ISREG(meta.st_mode)
                and meta.st_uid == os.getuid()
                and meta.st_nlink == 1
                and not meta.st_mode & 0o077,
                "shared campaign lock redirected",
            )
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._require_writer(operation, custody)
            doc = store._read_unlocked()
            need(
                type(doc) is dict and doc == self._physical_state(path, "shared campaign changed"),
                "shared campaign unavailable",
            )
            registration = doc.get("correction_controller")
            need(
                type(registration) is dict
                and registration.get("config") == self.config.source_binding(),
                "shared controller installation changed",
            )
            records = registration.setdefault("admission_records", {})
            actions = registration.setdefault("actions", [])
            need(
                type(records) is dict
                and type(actions) is list
                and len(records) <= self.config.max_entries
                and (record_key in records or len(records) < self.config.max_entries)
                and len(actions) <= 4 * self.config.max_entries
                and (digest(action) in actions or len(actions) < 4 * self.config.max_entries),
                "shared source enrollment metadata saturated",
            )
            need(
                record_key not in records or records[record_key] == decision_sha,
                "source admission decision rebound",
            )
            changed = record_key not in records or digest(action) not in actions
            records[record_key] = decision_sha
            if digest(action) not in actions:
                actions.append(digest(action))
            if changed:
                doc["revision"] = doc.get("revision", 0) + 1
                store._write_unlocked(doc)
                directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            self._require_writer(operation, custody)
        finally:
            os.close(lock)

    def enroll_source_selection_locked(self, custody, operation, selection, published):
        """Enroll only the fixed original source decision plus protected POST201.

        The returned publication dict is merely a physical receipt selector. All
        request, subjects, tests and local action hashes are derived again by the
        installed source validators while this exact writer is held.
        """
        from closer_correction_maintenance import assert_offload_allowed

        assert_offload_allowed()
        try:
            self._require_writer(operation, custody)
            effects = self._native_effect_source()
            # The publisher may have selected an already-confirmed protected
            # checkpoint. Re-derive it here; never trust its returned flag.
            before = self.read_bound(custody)
            need(
                getattr(self._writer_context, "native_selection", None) is None,
                "another source selection active",
            )
            self._writer_context.native_selection = selection
            try:
                current_facts = effects._source_selection(self, operation)
            finally:
                del self._writer_context.native_selection
            if any(
                entry["key"] == effect_key(current_facts["request"])
                for entry in before["data"]["entries"]
            ):
                effects.reconcile_source_checkpoint_locked(
                    self, custody, operation, selection, published
                )
                return held(
                    "exact original source checkpoint reconciled; no CAS",
                    status="checkpoint_reconciled_only",
                )
            instruction = effects.validate_instruction_origin(
                self, custody, operation, selection, published
            )
            need(
                getattr(self._writer_context, "native_selection", None) is None,
                "another source selection active",
            )
            self._writer_context.native_selection = selection
            try:
                facts = effects._source_selection(self, operation)
            finally:
                del self._writer_context.native_selection
            request = facts["request"]
            wire.validate_request(request)
            key = effect_key(request)
            before = self.read_bound(custody)
            existing = next(
                (entry for entry in before["data"]["entries"] if entry["key"] == key), None
            )
            need(existing is None, "existing enrollment must reconcile original source checkpoint")
            record = {
                "schema": "local-correction-admission/v1",
                "request": request,
                "seed_selector": facts["seed_selector"],
                "owner_instruction": instruction,
                "materializer": facts["materializer"],
                "original_source_context": facts["original_source_context"],
                "original_source_native": facts["original_source_native"],
                "original_incumbent_context": facts["original_incumbent_context"],
                "source_record_ref": facts["source_record_ref"],
                "own_instruction": {
                    "proof_sha256": published["proof_sha256"],
                    "phase_key": effects.phase_key(
                        request,
                        "instruction_publication",
                        {
                            "kind": "issue",
                            **{k: request["source_issue"][k] for k in ("number", "node_id")},
                        },
                    ),
                },
            }
            # Only this NEW source enrollment may freeze a declared v2 policy.
            # Empty/legacy consumers do not acquire a new dependency or budget.
            if any(
                type(value) is dict and "coding_session_v2" in value
                for value in self.config.consumers.values()
            ):
                import closer_coding_session_v2 as session_v2

                policy_path = Path(session_v2.__file__)
                need(
                    policy_path.is_absolute()
                    and policy_path.resolve(strict=True) == policy_path
                    and not any(p.is_symlink() for p in (policy_path, *policy_path.parents))
                    and self.installation.source_closure.get("closer_coding_session_v2.py")
                    == {"path": str(policy_path), "sha256": session_v2._LOADED_SOURCE_SHA256}
                    and hashlib.sha256(policy_path.read_bytes()).hexdigest()
                    == session_v2._LOADED_SOURCE_SHA256,
                    "trusted v2 enrollment policy source unavailable",
                )
                draft = session_v2.prepare_admission_policy(self.config.consumers)
                if draft.snapshot_utf8 is not None:
                    record[session_v2.ADMISSION_FIELD] = json.loads(draft.snapshot_utf8)
            record_key = digest(record)
            selected = {
                "kind": "local_controller_record",
                "record_sha256": record_key,
                "seed_selector": record["seed_selector"],
                "owner_instruction": instruction,
            }
            selector(selected, self.config.repository)
            pull = self.store.call("GET", "/pulls/" + request["target"]["pr"])
            need(
                str(pull.get("number")) == request["target"]["pr"]
                and type(pull.get("node_id")) is str
                and pull["node_id"]
                and pull.get("head", {}).get("sha") == request["target"]["head_sha"]
                and pull.get("base", {}).get("sha") == request["target"]["base_sha"]
                and pull.get("merge_commit_sha") == request["target"]["merge_sha"],
                "native source PR head/base/merge changed before enrollment",
            )
            subjects = [
                {"kind": "pull", "number": request["target"]["pr"], "node_id": pull["node_id"]},
                {"kind": "issue", **{k: request["source_issue"][k] for k in ("number", "node_id")}},
            ]
            if request["incumbent"].get("kind") != "no_incumbent":
                subjects.append(
                    {
                        "kind": "issue",
                        **{k: request["incumbent"]["issue"][k] for k in ("number", "node_id")},
                    }
                )
            action = {
                "kind": "enroll",
                "request": request,
                "admitted_selector": selected,
                "subjects": subjects,
            }
            # Validate exact state transition and current native versions before
            # changing either source metadata or the shared native index.
            transition(before["data"], action, self.config)
            self.fresh_issue_context(
                request, record, {"owner_login": request["incumbent"].get("owner")}
            )
            custody.create("admission-" + record_key + ".json", record)
            need(
                custody.read("admission-" + record_key + ".json") == record,
                "protected source admission changed",
            )
            self._register_source_enrollment_locked(
                custody, operation, record_key, digest(facts["decision"]), action
            )
            return self._apply_locked(action, operation=operation, custody=custody)
        except (
            ImportError,
            OSError,
            ValueError,
            KeyError,
            TypeError,
            AttributeError,
            RuntimeError,
        ) as error:
            return held(str(error))

    def _action(self, action, before, custody, operation):
        import closer_correction_maintenance as maintenance

        if maintenance.validate_index_action(self, custody, operation, before, action):
            return
        if action.get("kind") == "enroll":
            selected, request = action["admitted_selector"], action["request"]
        else:
            entry = next(
                (e for e in before["data"]["entries"] if e["key"] == action.get("key")),
                None,
            )
            need(entry is not None, "original enrollment absent")
            request = entry["request"]
            selected = action.get("selector", entry["admitted_selector"])
        self.authenticate(selected, request, custody, operation)
        native_confirmation = action.get("kind") in {"bind", "confirm"}
        if native_confirmation:
            self._require_writer(operation, custody)
            effects = self._native_effect_source()
            effects.validate_index_effect_action(self, custody, operation, entry, action)
        if action.get("kind") == "enroll":
            for identity in action["subjects"]:
                endpoint = "/pulls/" if identity["kind"] == "pull" else "/issues/"
                native = self.store.call("GET", endpoint + identity["number"])
                need(
                    str(native.get("number")) == identity["number"]
                    and native.get("node_id") == identity["node_id"]
                    and (identity["kind"] == "pull" or not native.get("pull_request")),
                    "fresh native enrollment identity differs",
                )
                if identity["kind"] == "pull":
                    need(
                        native.get("head", {}).get("sha") == request["target"]["head_sha"]
                        and native.get("base", {}).get("sha") == request["target"]["base_sha"],
                        "fresh source head/base differs",
                    )
        _, registration = self.live(operation)
        need(
            native_confirmation or digest(action) in registration.get("actions", []),
            "action not source-selected in original controller state",
        )

    def confirm(self, before, after, action, intent, custody):
        need(
            after["parent_commit_sha"] == before["commit_sha"]
            and after["data"] == transition(before["data"], action, self.config),
            "conditional write absent/competing; never repeat",
        )
        anchor = self.anchor(custody)
        target = {
            "revision": after["data"]["revision"],
            "commit_sha": after["commit_sha"],
            "blob_sha": after["blob_sha"],
            "state_sha256": digest(after["data"]),
        }
        if anchor["revision"] == target["revision"]:
            need(
                all(anchor[k] == v for k, v in target.items()),
                "existing high-water rebound",
            )
            return
        need(
            anchor["commit_sha"] == before["commit_sha"]
            and anchor["state_sha256"] == digest(before["data"]),
            "original write high-water changed",
        )
        result = {
            "schema": "local-correction-index-result/v1",
            "before": before,
            "after": after,
            "action": action,
            "intent": intent,
        }
        name = "result-" + intent["transaction_key"] + ".json"
        custody.create(name, result)
        custody.create(
            f"highwater-{target['revision']:016d}.json",
            {
                **target,
                "previous_anchor_sha256": digest(anchor),
                "result_name": name,
                "result_sha256": digest(result),
            },
        )

    def apply(self, action, *, operation):
        try:
            with self.fenced(operation) as custody:
                return self._apply_locked(action, operation=operation, custody=custody)
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as error:
            return held(str(error))

    def _apply_locked(self, action, *, operation, custody):
        attempted = False
        compare_diagnostic = None
        try:
            self._require_writer(operation, custody)
            action = copy.deepcopy(action)
            before = self.read_bound(custody)
            self._action(action, before, custody, operation)
            after_data = transition(before["data"], action, self.config)
            if after_data == before["data"]:
                return held("exact existing checkpoint", status="checkpoint_reconciled_only")
            owner, _ = self.live(operation)
            intent = write_intent(before, after_data, action, self.config, owner)
            key = intent["transaction_key"]
            custody.create("before-" + key + ".json", before)
            custody.create("action-" + key + ".json", action)
            need(
                custody.create("intent-" + key + ".json", intent),
                "original intent exists; reconcile never resubmit",
            )
            self._full_writer_validation(operation, custody)
            custody.check()
            attempted = True
            acknowledgment = None
            try:
                acknowledgment = self.store.compare_and_swap(before, after_data)
            except Exception as error:
                compare_diagnostic = _conditional_write_diagnostic(error)
            self._full_writer_validation(operation, custody)
            after = self.store.read()
            need(
                after["data"] == after_data
                and after["parent_commit_sha"] == before["commit_sha"]
                and (
                    acknowledgment is None
                    or acknowledgment == (after["commit_sha"], after["blob_sha"])
                ),
                "conditional outcome unknown or competing",
            )
            self._action(action, after, custody, operation)
            self.confirm(before, after, action, intent, custody)
            need(
                self.read_bound(custody) == after,
                "checkpoint changed before lock release",
            )
            return held(
                "exact checkpoint confirmed",
                status="checkpoint_confirmed",
                write_attempted=True,
                transaction_key=key,
                revision=after_data["revision"],
                commit_sha=after["commit_sha"],
            )
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as error:
            facts = (
                {}
                if compare_diagnostic is None
                else {"conditional_write_diagnostic": compare_diagnostic}
            )
            return held(str(error), write_attempted=attempted, **facts)

    def reconcile(self, transaction_key, *, operation):
        try:
            with self.fenced(operation) as custody:
                return self._reconcile_locked(transaction_key, operation=operation, custody=custody)
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as error:
            return held(str(error))

    def _reconcile_locked(self, transaction_key, *, operation, custody):
        """Exact existing writer only; read back an old CAS without another PUT."""
        try:
            self._require_writer(operation, custody)
            need(_SHA.fullmatch(transaction_key), "original transaction key missing")
            intent = custody.read("intent-" + transaction_key + ".json")
            before = custody.read("before-" + transaction_key + ".json")
            action = custody.read("action-" + transaction_key + ".json")
            need(
                intent["transaction_key"] == transaction_key
                and write_intent(
                    before,
                    transition(before["data"], action, self.config),
                    action,
                    self.config,
                    intent["owner"],
                )
                == intent,
                "original before/action/intent differs",
            )
            after = self.store.read()
            self._action(action, after, custody, operation)
            self.confirm(before, after, action, intent, custody)
            need(self.read_bound(custody) == after, "reconciled checkpoint changed")
            return held(
                "original write reconciled; no CAS",
                status="checkpoint_reconciled_only",
            )
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as error:
            return held(str(error))

    @contextmanager
    def final_provider_guard(
        self, *, operation, consumer_id, selected, which, expected, reader=None
    ):
        """Reserve only at the actual existing offload's final before-Popen seam.

        No reader is installed by default. Its account observation is distinct
        from profile policy/presence probes. The writer lock stays held until the
        existing caller exits this context; no coding/provider callback is added.
        """
        from closer_correction_maintenance import assert_offload_allowed

        assert_offload_allowed()
        with self.fenced(operation) as custody:
            guard = _ProviderGuard(
                self, custody, operation, consumer_id, selected, which, expected, reader
            )
            try:
                yield guard.starting
            finally:
                import closer_coding_session_runtime

                try:
                    closer_coding_session_runtime.release_guard(guard)
                finally:
                    guard.active = False


def held(reason, *, status="owned_unknown", write_attempted=False, **facts):
    return {
        "status": status,
        "reason": reason,
        "write_attempted": write_attempted,
        "execution_authorized": False,
        "retry_authorized": False,
        "act_authorized": False,
        "source_closure_authorized": False,
        **facts,
    }


class NativeConsumerReader:
    """Installed source dependency, not a caller boolean or auth-presence upgrade.

    A reviewed managed coding consumer must implement the actual nonsecret native
    account read. No implementation, credentials or probing command is supplied.
    The controller pins that implementation's source before every read.
    """

    def read_native_identity(self):
        raise ControllerUnknown("installed managed native account reader unsupported")

    def verify_installed_confinement(self, *, command, cwd, env, custody_path):
        """Concrete installed source must verify enforced filesystem/network and
        credential isolation at this boundary. Presence/policy/JSON are insufficient.
        The inactive base implementation cannot authorize a coding child.
        """
        raise ControllerUnknown(
            "installed coding child credential/filesystem/network isolation unsupported"
        )

    def verify_execution_limits(self, *, command, prompt, input_tokens, output_tokens, effective):
        """Installed consumer must verify actual delivered limits, not declarations."""
        raise ControllerUnknown("installed coding execution-limit enforcement unsupported")

    def read_native_budget(self):
        raise ControllerUnknown("installed native coding budget reader unsupported")

    def estimate_final_input_tokens(self, prompt):
        raise ControllerUnknown("installed final prompt estimator unsupported")

    def observe(self, registration):
        entry = type(self).__module__ + "." + type(self).__qualname__
        path = Path(inspect.getsourcefile(type(self)) or "")
        need(
            entry == registration["reader_entrypoint"]
            and path.is_file()
            and not path.is_symlink()
            and hashlib.sha256(path.read_bytes()).hexdigest()
            == registration["reader_source_sha256"],
            "installed consumer reader source differs",
        )
        identity = self.read_native_identity()
        exact(identity, "account_id profile_id")
        need(
            type(identity["account_id"]) is str
            and identity["account_id"]
            and identity["profile_id"] == registration["profile_id"],
            "native consumer account identity unavailable",
        )
        return digest(
            {
                "schema": "managed-native-account/v1",
                "account_id": identity["account_id"],
            }
        )


class _ProviderGuard:
    def __init__(
        self,
        controller,
        custody,
        operation,
        consumer_id,
        selected,
        which,
        expected,
        reader,
    ):
        self.controller, self.custody, self.operation = (
            controller,
            custody,
            copy.deepcopy(operation),
        )
        self.consumer_id, self.selected, self.which, self.expected = (
            consumer_id,
            copy.deepcopy(selected),
            copy.deepcopy(which),
            copy.deepcopy(expected),
        )
        self.reader, self.used, self.active = reader, False, True
        self.pid, self.thread = os.getpid(), threading.get_ident()

    def starting(self, observation):
        import closer_resources as resources
        import dispatcher
        import execution_profiles

        need(
            self.active and not self.used,
            "original before-provider claim inactive or consumed",
        )
        self.used = True
        need(
            os.getpid() == self.pid and threading.get_ident() == self.thread,
            "provider claim crossed original thread",
        )
        frame = inspect.currentframe().f_back
        need(
            frame.f_code is dispatcher.run_observed_offload.__code__
            and frame.f_locals.get("starting_callback") == self.starting
            and frame.f_locals.get("observation") is observation,
            "actual existing before-Popen callback required",
        )
        owner, _ = self.controller.live(self.operation)
        registration = self.controller.config.consumers.get(self.consumer_id)
        need(
            type(registration) is dict and isinstance(self.reader, NativeConsumerReader),
            "installed native coding consumer/account reader absent",
        )
        need(
            registration.get("runner") == "existing-local-controller"
            and (
                ("coding_session_v2" in registration and "attempt_budget" not in registration)
                or (
                    "coding_session_v2" not in registration
                    and registration.get("attempt_budget") == 1
                )
            )
            and registration.get("owner_login")
            and (
                _SHA.fullmatch(registration.get("policy_ceiling_sha256", ""))
                or "coding_session_v2" in registration
            ),
            "source coding policy ceiling absent",
        )
        profile = execution_profiles.get_profile(registration["profile_id"])
        need(
            source_digest(profile) == registration["profile_sha256"]
            and profile["agent"] == registration["agent"]
            and self.reader.observe(registration) == registration["account_sha256"],
            "current native consumer account/profile changed",
        )
        need(
            digest(frame.f_locals["command"]) == registration["transport_argv_sha256"],
            "actual coding transport differs from source consumer",
        )
        self.reader.verify_installed_confinement(
            command=frame.f_locals["command"],
            cwd=frame.f_locals["cwd"],
            env=frame.f_locals.get("env"),
            custody_path=self.custody.path,
        )
        current = self.controller.read_bound(self.custody)
        subject(self.which)
        entry = next((e for e in current["data"]["entries"] if self.which in e["subjects"]), None)
        need(
            entry is not None
            and self.expected
            == {
                "key": entry["key"],
                "request_sha256": entry["request"]["request_sha256"],
            },
            "exact typed consumer membership missing",
        )
        need(
            not any(
                s["state"] != "confirmed" for e in current["data"]["entries"] for s in e["stages"]
            ),
            "unresolved correction intent blocks final consumer",
        )
        incumbent = entry["request"]["incumbent"]
        need(
            registration["agent"] != "codex" or incumbent.get("kind") != "no_incumbent",
            "source Codex coding requires existing native incumbent assignment",
        )
        need(
            incumbent.get("kind") == "no_incumbent"
            or incumbent["owner"] == registration["owner_login"],
            "current incumbent owner changed",
        )
        self.controller.authenticate(self.selected, entry["request"], self.custody, self.operation)
        need(
            self.selected == entry["admitted_selector"]
            or any(
                b["selector"] == self.selected and b["subject"] == self.which
                for b in entry["bindings"]
            ),
            "consumer instruction/subject binding changed",
        )
        record = self.custody.read("admission-" + self.selected["record_sha256"] + ".json")
        source_context, incumbent_context = self.controller.fresh_issue_context(
            entry["request"], record, registration
        )
        import closer_coding_session_v2 as session_contract

        is_session_v2 = (
            session_contract.POLICY_FIELD in registration
            or session_contract.ADMISSION_FIELD in record
        )
        if is_session_v2:
            import closer_coding_session_runtime

            import credential_redaction

            need(
                not credential_redaction.disabled(),
                "v2 coding requires enabled credential redaction",
            )
            need(registration["agent"] == "codex", "unsupported v2 coding consumer")
            draft = session_contract.prepare_admission_policy(self.controller.config.consumers)
            need(draft.snapshot_utf8 is not None, "current v2 source policy unavailable")
            original_session = session_contract.snapshot_from_admission(record)
            policy_session = session_contract.validate_policy(
                registration[session_contract.POLICY_FIELD]
            )
            need(
                original_session["consumer_id"] == self.consumer_id
                and policy_session["consumer_id"] == self.consumer_id
                and original_session["profile_sha256"] == registration["profile_sha256"]
                and policy_session["profile_sha256"] == registration["profile_sha256"],
                "original/current v2 consumer differs",
            )
            current_budget = session_contract.validate_budget(
                registration.get(closer_coding_session_runtime.CURRENT_FIELD)
            )
            effective = session_contract.minimum_budget(
                original_session["budget"], current_budget, policy_session["budget"]
            )
        else:
            original_budget = record.get("original_coding_budget")
            current_budget = self.reader.read_native_budget()
            policy = registration.get("policy_ceiling")
            keys = "max_prompt_bytes max_input_tokens max_output_tokens max_attempts"
            for limits in (original_budget, current_budget, policy):
                exact(limits, keys)
                need(
                    all(positive(value) for value in limits.values())
                    and limits["max_attempts"] == 1,
                    "actual original/current coding budget unavailable",
                )
            need(
                digest(policy) == registration["policy_ceiling_sha256"],
                "numeric policy ceiling differs",
            )
            effective = {
                key: min(original_budget[key], current_budget[key], policy[key]) for key in policy
            }
        command = frame.f_locals["command"]
        if registration["agent"] == "codex":
            import closer_coding_delivery

            source_modules = (dispatcher, closer_coding_delivery)
            if is_session_v2:
                import closer_coding_route_v2

                source_modules += (closer_coding_session_runtime, closer_coding_route_v2)
            for module in source_modules:
                source_path = Path(module.__file__)
                pinned = self.controller.installation.source_closure.get(module.__name__ + ".py")
                need(
                    type(pinned) is dict
                    and source_path.is_absolute()
                    and source_path.resolve(strict=True) == source_path
                    and not any(p.is_symlink() for p in (source_path, *source_path.parents))
                    and pinned == {"path": str(source_path), "sha256": module._LOADED_SOURCE_SHA256}
                    and hashlib.sha256(source_path.read_bytes()).hexdigest()
                    == module._LOADED_SOURCE_SHA256,
                    "loaded coding delivery source closure unavailable or changed",
                )
            if is_session_v2:
                validator_path = Path(session_contract.__file__)
                need(
                    self.controller.installation.source_closure.get("closer_coding_session_v2.py")
                    == {
                        "path": str(validator_path),
                        "sha256": closer_coding_session_runtime._VALIDATOR_SOURCE_SHA256,
                    }
                    and hashlib.sha256(validator_path.read_bytes()).hexdigest()
                    == closer_coding_session_runtime._VALIDATOR_SOURCE_SHA256,
                    "loaded v2 session validator source closure changed",
                )
            delivery = closer_coding_delivery.require_coding_delivery(frame)
            need(
                registration["profile_id"] == closer_coding_delivery.SELECTED_PROFILE
                and delivery.profile_json
                == json.dumps(profile, sort_keys=True, separators=(",", ":")),
                "exact source coding delivery profile differs",
            )
            prompt = delivery.prompt_utf8.decode("utf-8")
        else:
            delivery = registration.get("prompt_argv_index")
            need(
                type(delivery) is int
                and 0 <= delivery < len(command)
                and type(command[delivery]) is str,
                "exact installed final prompt delivery unavailable",
            )
            prompt = command[delivery]
        original = record["materializer"]
        payload = {
            "original_request": entry["request"],
            "source_body": original["source_body"],
            "original_incumbent_context": incumbent_context,
            "raw_findings": original["raw_findings"],
            "required_tests": entry["request"]["required_tests"],
        }
        need(
            canonical(payload).decode() in prompt,
            "final prompt omitted original body/raw finding/required tests",
        )
        if is_session_v2:
            need(
                len(prompt.encode("utf-8")) <= effective["max_prompt_utf8_bytes"],
                "final v2 coding prompt exceeds original/current ceiling",
            )
        else:
            estimate = self.reader.estimate_final_input_tokens(prompt)
            need(
                positive(estimate)
                and len(prompt.encode()) <= effective["max_prompt_bytes"]
                and estimate <= effective["max_input_tokens"]
                and registration.get("output_tokens") is not None
                and positive(registration["output_tokens"])
                and registration["output_tokens"] <= effective["max_output_tokens"],
                "final prompt estimate/output exceeds original/current ceiling",
            )
            self.reader.verify_execution_limits(
                command=command,
                prompt=prompt,
                input_tokens=estimate,
                output_tokens=registration["output_tokens"],
                effective=effective,
            )
        binding = resources.finite_pool_current_binding()
        need(
            binding
            == {
                "frontier_id": self.operation["finite_frontier"]["frontier_id"],
                "target": owner["target"],
            },
            "original finite pool missing",
        )
        need(
            observation.get("agent") == registration["agent"]
            and observation.get("profile_id") == registration["profile_id"]
            and observation.get("finite_frontier_id") == binding["frontier_id"]
            and observation.get("finite_frontier_target") == binding["target"]
            and isinstance(observation.get("run_id"), str)
            and observation["run_id"],
            "actual final provider identity differs",
        )
        slots = resources._pool_slots()
        need(
            slots
            and all(
                s["record"].get("agent") == profile["agent"]
                and s["record"].get("profile_id") == profile["profile_id"]
                and s["record"].get("lane")
                in {hashlib.sha256(p.encode()).hexdigest() for p in profile["capacity_pool_ids"]}
                for s in slots
            ),
            "held finite pool/account profile mismatch",
        )
        stable = {
            "schema": "local-correction-final-provider-key/v1",
            "repository_id": self.controller.config.repository_id,
            "epoch": self.controller.config.epoch,
            "key": entry["key"],
            "request_sha256": entry["request"]["request_sha256"],
            "consumer_id": self.consumer_id,
        }
        need(
            self.custody.create(
                "provider-intent-" + digest(stable) + ".json",
                {
                    **stable,
                    "index_commit": current["commit_sha"],
                    "selector": self.selected,
                    "account_sha256": registration["account_sha256"],
                    "profile_sha256": registration["profile_sha256"],
                    **(
                        {"coding_session_policy_sha256": digest(policy_session)}
                        if is_session_v2
                        else {"policy_ceiling_sha256": registration["policy_ceiling_sha256"]}
                    ),
                },
            ),
            "original provider intent exists; never retry",
        )
        self.controller.live(self.operation)
        need(
            self.controller.read_bound(self.custody) == current,
            "index changed before actual provider reservation",
        )
        # Reobserve the concrete installed account/limits and enforced child boundary
        # after native joins; no serialized permit survives to this last source seam.
        need(
            self.reader.observe(registration) == registration["account_sha256"]
            and (
                session_contract.validate_budget(
                    registration.get(closer_coding_session_runtime.CURRENT_FIELD)
                )
                if is_session_v2
                else self.reader.read_native_budget()
            )
            == current_budget,
            "native account/budget changed at final reservation",
        )
        self.reader.verify_installed_confinement(
            command=frame.f_locals["command"],
            cwd=frame.f_locals["cwd"],
            env=frame.f_locals.get("env"),
            custody_path=self.custody.path,
        )
        need(
            self.controller.fresh_issue_context(entry["request"], record, registration)
            == (source_context, incumbent_context),
            "issue context changed at final reservation",
        )
        self.custody.check()
        self.controller.live(self.operation)
        if registration["agent"] == "codex":
            source_modules = (dispatcher, closer_coding_delivery)
            if is_session_v2:
                source_modules += (closer_coding_session_runtime,)
            for module in source_modules:
                source_path = Path(module.__file__)
                pinned = self.controller.installation.source_closure.get(module.__name__ + ".py")
                need(
                    type(pinned) is dict
                    and source_path.is_absolute()
                    and source_path.resolve(strict=True) == source_path
                    and not any(p.is_symlink() for p in (source_path, *source_path.parents))
                    and pinned == {"path": str(source_path), "sha256": module._LOADED_SOURCE_SHA256}
                    and hashlib.sha256(source_path.read_bytes()).hexdigest()
                    == module._LOADED_SOURCE_SHA256,
                    "loaded coding delivery source closure unavailable or changed",
                )
            if is_session_v2:
                validator_path = Path(session_contract.__file__)
                need(
                    self.controller.installation.source_closure.get("closer_coding_session_v2.py")
                    == {
                        "path": str(validator_path),
                        "sha256": closer_coding_session_runtime._VALIDATOR_SOURCE_SHA256,
                    }
                    and hashlib.sha256(validator_path.read_bytes()).hexdigest()
                    == closer_coding_session_runtime._VALIDATOR_SOURCE_SHA256,
                    "loaded v2 session validator source closure changed",
                )
            need(
                closer_coding_delivery.require_coding_delivery(frame) == delivery,
                "source coding delivery changed at final reservation",
            )
        if is_session_v2:
            closer_coding_session_runtime.issue_before_popen(frame)
        resources.finite_pool_prepare_request(observation)


def _inspection_has_no_native_transport(*_args, **_kwargs):
    raise ControllerUnknown("read-only custody inspection cannot perform native I/O")


def inspect_provisioned_custody(config_dict):
    """Read existing local custody only; correlation metadata grants no authority.

    Structural pending transactions are returned as diagnostics, not erased or
    retried. Rejecting every in-progress intent here would invalidate the very
    original writer which just published its write-ahead record. Native/action
    admission still uses read_bound/reconcile under the actual writer fence.
    """
    need(type(config_dict) is dict, "source controller config missing")
    config = ControllerConfig(**copy.deepcopy(config_dict))
    controller = IndexController(config, _inspection_has_no_native_transport)
    custody = _Custody(config)
    custody._inspection_read_budget = [_INSPECTION_MAX_READ_BYTES]
    try:
        names = sorted(os.listdir(custody.fd))
        need(len(names) <= 16384, "local controller custody inventory exceeds bound")
        need(
            custody.read("namespace.json")
            == {
                "schema": "local-correction-controller-namespace/v1",
                "config_sha256": digest(config.source_binding()),
            },
            "provisioned namespace differs",
        )
        anchor = controller.anchor(custody)
        highwaters = [n for n in names if n.startswith("highwater-")]
        previous = None
        for name in highwaters:
            row = custody.read(name)
            if previous is not None:
                result = custody.read(row["result_name"])
                exact(result, "schema before after action intent")
                before, after, action, intent = (
                    result[k] for k in ("before", "after", "action", "intent")
                )
                need(
                    result["schema"] == "local-correction-index-result/v1"
                    and validate_index(before["data"], config) == before["data"]
                    and validate_index(after["data"], config) == after["data"]
                    and after["data"] == transition(before["data"], action, config)
                    and after["parent_commit_sha"] == before["commit_sha"]
                    and (
                        before["data"]["revision"],
                        before["commit_sha"],
                        before["blob_sha"],
                        digest(before["data"]),
                    )
                    == (
                        previous["revision"],
                        previous["commit_sha"],
                        previous["blob_sha"],
                        previous["state_sha256"],
                    )
                    and (
                        after["data"]["revision"],
                        after["commit_sha"],
                        after["blob_sha"],
                        digest(after["data"]),
                    )
                    == (
                        row["revision"],
                        row["commit_sha"],
                        row["blob_sha"],
                        row["state_sha256"],
                    )
                    and write_intent(before, after["data"], action, config, intent["owner"])
                    == intent
                    and row["result_name"] == "result-" + intent["transaction_key"] + ".json",
                    "local high-water native transition/owner intent differs",
                )
                key = intent["transaction_key"]
                need(
                    custody.read("before-" + key + ".json") == before
                    and custody.read("action-" + key + ".json") == action
                    and custody.read("intent-" + key + ".json") == intent,
                    "local high-water original transaction custody missing",
                )
            previous = row
        pending = []
        for name in names:
            if name.startswith("intent-"):
                match = re.fullmatch(r"intent-([a-f0-9]{64})\.json", name)
                need(match is not None, "local original intent name invalid")
                key = match[1]
                intent = custody.read(name)
                before = custody.read("before-" + key + ".json")
                action = custody.read("action-" + key + ".json")
                need(
                    intent
                    == write_intent(
                        before,
                        transition(before["data"], action, config),
                        action,
                        config,
                        intent["owner"],
                    ),
                    "local original pending intent changed",
                )
                if "result-" + key + ".json" not in names:
                    pending.append(key)
            elif name.startswith("result-"):
                result = custody.read(name)
                if result["after"]["data"]["revision"] > anchor["revision"]:
                    pending.append(result["intent"]["transaction_key"])
        custody.check()
        need(
            sorted(os.listdir(custody.fd)) == names and controller.anchor(custody) == anchor,
            "local custody changed during read-only inspection",
        )
        return {
            "identity": {
                "custody_path": str(custody.path),
                "namespace": config.namespace,
                "config_sha256": digest(config.source_binding()),
                "bootstrap_anchor_sha256": config.bootstrap_anchor_sha256,
                "device": str(custody.identity.st_dev),
                "inode": str(custody.identity.st_ino),
            },
            "highwater": {
                "anchor": anchor,
                "pending_transactions": sorted(set(pending)),
            },
        }
    finally:
        custody.close()
