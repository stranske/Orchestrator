"""Inactive private-index data primitives and unsupported native-reader interface.

This foundation validates and transforms data only. The operational controller,
native store, custody, provider guard and inspection paths remain separately
unpublished with their actual dependencies. There is no bootstrap, credential
loader, installed account reader, enrollment or native/provider execution here.
Hashes and data selectors provide no authentication or action authority.
"""

from __future__ import annotations

import copy
import hashlib
import inspect
import json
import re
import types
import weakref
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import closer_correction_handoff as wire
import closer_queue as queue

SOURCE_CONTRACT = "b9de11947cc3092996774c6ce8f55b6373f1c520"
SCHEMA = "closer-correction-subject-index/v1"
BRANCH = "closer-correction-state"
_LOADED_SOURCE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
INDEX_PATH = ".github/closer-correction/index.json"
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


def matches(pattern, value):
    return type(value) is str and re.fullmatch(pattern, value) is not None


def subject(value):
    exact(value, "kind number node_id")
    need(
        type(value["kind"]) is str
        and value["kind"] in {"issue", "pull"}
        and matches(r"[1-9][0-9]{0,15}", value["number"])
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
        and matches(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value)
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
        and all(matches(_SHA, value[k]) for k in ("archive_sha256", "member_sha256")),
        "original seed selector differs",
    )


def selector(value, repository):
    exact(value, "kind record_sha256 seed_selector owner_instruction")
    need(
        value["kind"] == "local_controller_record" and matches(_SHA, value["record_sha256"]),
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
        and matches(r"[1-9][0-9]{0,15}", row["issue_number"])
        and positive(row["comment_id"])
        and positive(row["writer_id"])
        and all(
            type(row[k]) is str and 0 < len(row[k]) <= 256
            for k in ("issue_node_id", "comment_node_id")
        )
        and matches(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", row["updated_at"])
        and matches(_SHA, row["body_sha256"]),
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
            and matches(_SHA, data["previous_sha256"])
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
                binding["subject"] in entry["subjects"] and matches(_SHA, binding["proof_sha256"]),
                "subject binding changed",
            )
        phases = set()
        for stage in entry["stages"]:
            exact(
                stage,
                "phase state intent_sha256 intent_selector proof_sha256 result result_selector",
            )
            need(
                type(stage["phase"]) is str
                and stage["phase"] in PHASES
                and stage["phase"] not in phases
                and matches(_SHA, stage["intent_sha256"])
                and type(stage["state"]) is str
                and stage["state"] in {"prepared", "outcome_unknown", "confirmed"},
                "invalid stage",
            )
            phases.add(stage["phase"])
            selector(stage["intent_selector"], config.repository)
            if stage["state"] == "confirmed":
                selector(stage["result_selector"], config.repository)
                need(matches(_SHA, stage["proof_sha256"]), "native proof missing")
                if stage["phase"] == "autopilot_dispatch":
                    exact(stage["result"], "workflow run_id run_attempt issue")
                    subject(stage["result"]["issue"])
                    need(
                        stage["result"]["workflow"] == ".github/workflows/agents-auto-pilot.yml"
                        and all(
                            matches(r"[1-9][0-9]{0,15}", stage["result"][k])
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
                action["subject"]["kind"] == "pull" and matches(_SHA, action["proof_sha256"]),
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
            need(type(phase) is str and phase in PHASES, "unsupported phase")
            stage = next((s for s in entry["stages"] if s["phase"] == phase), None)
            if kind == "prepare":
                exact(action, "kind key phase intent_sha256 selector")
                selector(action["selector"], config.repository)
                need(
                    stage is None and matches(_SHA, action["intent_sha256"]),
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
                    stage is not None and matches(_SHA, action["proof_sha256"]),
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


@dataclass(frozen=True)
class _ReaderDefinition:
    path: Path
    sha256: str
    method: types.FunctionType
    code: types.CodeType


_READER_DEFINITIONS: weakref.WeakKeyDictionary[type, _ReaderDefinition] = (
    weakref.WeakKeyDictionary()
)


def _pin_reader_definition(cls):
    """Bind retained source to the actual loaded method, not a class hash claim."""
    try:
        path = Path(inspect.getsourcefile(cls) or "")
        if path.is_symlink() or not path.is_file():
            return
        raw = path.read_bytes()
        method = inspect.getattr_static(cls, "read_native_identity")
        if not isinstance(method, types.FunctionType):
            return
        # This interface takes only self. Mutable caller closures/defaults cannot
        # substitute an account observation for the source-defined reader.
        if method.__closure__ or method.__defaults__ or method.__kwdefaults__:
            return

        def codes(code):
            yield code
            for child in code.co_consts:
                if isinstance(child, types.CodeType):
                    yield from codes(child)

        compiled = compile(raw, str(path), "exec", dont_inherit=True)
        # Class-definition hashing alone can approve new disk bytes while old
        # module globals/code are running. Require the actual importing module
        # frame to match those bytes; runtime/dynamic class claims stay held.
        frame = inspect.currentframe()
        try:
            while frame is not None and not (
                frame.f_code.co_name == "<module>" and frame.f_code.co_filename == str(path)
            ):
                frame = frame.f_back
            if frame is None or frame.f_code != compiled:
                return
        finally:
            del frame
        expected = [code for code in codes(compiled) if code.co_qualname == method.__qualname__]
        if len(expected) != 1 or method.__code__ != expected[0]:
            return
        _READER_DEFINITIONS[cls] = _ReaderDefinition(
            path, hashlib.sha256(raw).hexdigest(), method, method.__code__
        )
    except (OSError, TypeError, ValueError, SyntaxError, AttributeError):
        # Unsupported loaders or unavailable source remain unqualified.
        return


class NativeConsumerReader:
    """Installed source dependency, not a caller boolean or auth-presence upgrade.

    A reviewed managed coding consumer must implement the actual nonsecret native
    account read. No implementation, credentials or probing command is supplied.
    The controller pins that implementation's source before every read.
    """

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        _pin_reader_definition(cls)

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
        cls = type(self)
        definition = _READER_DEFINITIONS.get(cls)
        entry = cls.__module__ + "." + cls.__qualname__
        need(
            definition is not None
            and entry == registration["reader_entrypoint"]
            and definition.sha256 == registration["reader_source_sha256"]
            and definition.path.is_file()
            and not definition.path.is_symlink()
            and hashlib.sha256(definition.path.read_bytes()).hexdigest() == definition.sha256
            and inspect.getattr_static(cls, "read_native_identity") is definition.method
            and inspect.getattr_static(self, "read_native_identity") is definition.method
            and definition.method.__code__ is definition.code
            and not definition.method.__closure__
            and not definition.method.__defaults__
            and not definition.method.__kwdefaults__,
            "installed consumer reader source differs",
        )
        identity = definition.method(self)
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


_pin_reader_definition(NativeConsumerReader)
