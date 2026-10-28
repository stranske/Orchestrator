"""Necessary publication DATA contracts and create-only custody; no effect issuer.

The fixed observed transport remains unreachable without genuine source issuance.
ACK/readback inputs alone are data, not attestation. Source baseline/handoff
requires the actual coding callback and a supported observed Git/helper context.
The delivery tick, mutation descriptor and native publication origin remain held.
"""

from __future__ import annotations

import base64
import hashlib
import inspect
import json
import os
import re
import time
from pathlib import Path, PurePosixPath

import closer_correction_controller as ctl
import closer_queue as queue

_LOADED_SOURCE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()

POLICY_SCHEMA = "closer-owned-pr-publication-policy/v1"
ANCHOR_POLICY_SCHEMA = "closer-owned-pr-publication-policy/v2"
INTENT_SCHEMA = "closer-owned-pr-publication-contract-intent/v1"
EVENT_SCHEMA = "closer-owned-pr-publication-contract-event/v1"
_SHA = re.compile(r"[a-f0-9]{64}\Z")
_OID = re.compile(r"[a-f0-9]{40}\Z")
_AUTHORITY = (
    "git_execution_authorized",
    "native_publication_authorized",
    "provider_execution_authorized",
    "act_authorized",
    "global_stop_valid",
    "acceptance_authorized",
    "domain_completed",
    "merge_authorized",
    "source_closure_authorized",
    "retry_authorized",
)
_BOUNDS = (
    "max_files",
    "max_file_bytes",
    "max_total_bytes",
    "max_ack_bytes",
    "request_timeout_seconds",
    "max_native_io_seconds",
    "max_push_submissions",
)


class PublicationHeld(ValueError):
    pass


def _need(ok, reason):
    if not ok:
        raise PublicationHeld(reason)


def _bytes(value):
    try:
        return queue._canonical_bytes(value)
    except (ValueError, TypeError, OverflowError) as error:
        raise PublicationHeld("publication contract is not finite JSON") from error


def digest(value):
    return hashlib.sha256(_bytes(value)).hexdigest()


def _copy(value):
    return json.loads(_bytes(value))


def _exact(value, keys):
    _need(type(value) is dict and set(value) == set(keys), "publication contract fields differ")


def _sha(value):
    return type(value) is str and _SHA.fullmatch(value) is not None


def _oid(value):
    return type(value) is str and _OID.fullmatch(value) is not None


def _positive(value):
    return type(value) is int and 0 < value <= 2**53 - 1


def _path(value):
    _need(type(value) is str and value and "\\" not in value, "publication path invalid")
    path = PurePosixPath(value)
    _need(
        bool(path.parts)
        and not path.is_absolute()
        and str(path) == value
        and all(
            part.casefold() not in {".", "..", ".git", ".codex", ".ssh"}
            and not part.casefold().startswith(".env")
            and part.casefold() not in {".gitattributes", ".gitconfig", ".gitmodules"}
            for part in path.parts
        )
        and not any(ord(char) < 32 or ord(char) == 127 for char in value),
        "publication path escapes or changes protected control data",
    )
    return value


def validate_policy(value, *, _prepare_context=False):
    """Closed, explicit necessary policy shape only. Never an authority producer."""
    anchor_mode = type(value) is dict and value.get("schema") == ANCHOR_POLICY_SCHEMA
    _exact(
        value,
        {
            "schema",
            "mode",
            "consumer_id",
            "git_executable",
            "allowed_path_prefixes",
            *({"trusted_context_sha256"} if not _prepare_context else set()),
            *_BOUNDS,
            *({"baseline_limits"} if anchor_mode else set()),
            *(
                {"git_environment_v1"}
                if type(value) is dict and "git_environment_v1" in value
                else set()
            ),
        },
    )
    _need(
        value["schema"] in {POLICY_SCHEMA, ANCHOR_POLICY_SCHEMA}
        and value["mode"] == "owned_existing_pr",
        "publication mode unsupported",
    )
    _need(type(value["consumer_id"]) is str and bool(value["consumer_id"]), "consumer missing")
    _exact(value["git_executable"], {"path", "sha256"})
    executable = value["git_executable"]
    _need(
        type(executable["path"]) is str
        and executable["path"].startswith("/")
        and str(PurePosixPath(executable["path"])) == executable["path"]
        and ".." not in PurePosixPath(executable["path"]).parts
        and not any(ord(c) < 32 for c in executable["path"])
        and _sha(executable["sha256"])
        and (_prepare_context or _sha(value["trusted_context_sha256"])),
        "source Git/context pin missing",
    )
    prefixes = value["allowed_path_prefixes"]
    _need(
        type(prefixes) is list
        and prefixes
        and all(type(p) is str for p in prefixes)
        and len(set(prefixes)) == len(prefixes),
        "explicit publication paths missing",
    )
    for prefix in prefixes:
        _path(prefix)
    _need(
        all(_positive(value[k]) for k in _BOUNDS)
        and value["request_timeout_seconds"] <= 120
        and value["max_native_io_seconds"] <= 15
        and value["max_native_io_seconds"] <= value["request_timeout_seconds"]
        and value["max_file_bytes"] <= value["max_total_bytes"]
        and value["max_push_submissions"] == 1,
        "publication numeric bounds invalid",
    )
    if anchor_mode:
        _baseline_limits(value["baseline_limits"])
    if "git_environment_v1" in value:
        _need(anchor_mode, "private Git context requires explicit v2 publication")
        _private_git_spec(value["git_environment_v1"])
    return _copy(value)


def stable_slot(original):
    """Original correction/consumer slot, excluding all attempt/target revisions."""
    _exact(original, {"repository_id", "epoch", "effect_key", "request_sha256", "consumer_id"})
    _need(
        _positive(original["repository_id"])
        and _sha(original["effect_key"])
        and _sha(original["request_sha256"])
        and all(type(original[k]) is str and original[k] for k in ("epoch", "consumer_id")),
        "original publication identity invalid",
    )
    return digest(
        {
            "schema": "closer-correction-publication-slot/v1",
            "phase": "coding_pr_delivery",
            **original,
        }
    )


def validate_owned_pr(value):
    _exact(
        value,
        {
            "repository",
            "repository_id",
            "pr_number",
            "pr_node_id",
            "issue_node_id",
            "owner_login",
            "owner_id",
            "writer_login",
            "writer_id",
            "head_repo_id",
            "base_repo_id",
            "head_ref",
            "head_oid",
            "base_ref",
            "base_oid",
            "body_sha256",
            "state",
            "is_draft",
            "is_merged",
            "closing_issue_node_ids",
        },
    )
    _need(
        type(value["repository"]) is str
        and re.fullmatch(r"[a-z0-9_.-]+/[a-z0-9_.-]+", value["repository"]),
        "canonical repository missing",
    )
    _need(
        all(
            _positive(value[k])
            for k in (
                "repository_id",
                "pr_number",
                "owner_id",
                "writer_id",
                "head_repo_id",
                "base_repo_id",
            )
        )
        and value["head_repo_id"] == value["base_repo_id"] == value["repository_id"]
        and value["owner_id"] == value["writer_id"],
        "publication native owner/repository differs",
    )
    _need(
        all(
            type(value[k]) is str and value[k]
            for k in ("pr_node_id", "issue_node_id", "owner_login", "writer_login")
        )
        and value["owner_login"] == value["writer_login"]
        and value["state"] == "open"
        and value["is_draft"] is False
        and value["is_merged"] is False
        and value["closing_issue_node_ids"] == [value["issue_node_id"]],
        "unique owned open corrective PR missing",
    )
    for key in ("head_ref", "base_ref"):
        ref = value[key]
        _need(
            type(ref) is str
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", ref)
            and not any(x in ref for x in ("..", "//", "@{"))
            and not ref.endswith(("/", ".", ".lock")),
            "native branch ref invalid",
        )
    _need(
        value["head_ref"] != value["base_ref"]
        and value["head_ref"] != ctl.BRANCH
        and _oid(value["head_oid"])
        and _oid(value["base_oid"])
        and _sha(value["body_sha256"]),
        "native base/head binding invalid",
    )
    return _copy(value)


def blob_oid(raw):
    _need(type(raw) is bytes, "raw publication blob bytes required")
    return hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()


def validate_candidate(value, policy):
    policy = validate_policy(policy)
    _exact(
        value,
        {"base_oid", "old_head_oid", "old_tree_oid", "tree_oid", "files", "unchanged_tree_sha256"},
    )
    _need(
        all(_oid(value[k]) for k in ("base_oid", "old_head_oid", "old_tree_oid", "tree_oid"))
        and _sha(value["unchanged_tree_sha256"]),
        "candidate tree identity missing",
    )
    rows = value["files"]
    _need(type(rows) is list and 0 < len(rows) <= policy["max_files"], "candidate file bound")
    seen, total = set(), 0
    for row in rows:
        _exact(row, {"path", "before", "after"})
        path = _path(row["path"])
        _need(
            path not in seen
            and any(path == p or path.startswith(p + "/") for p in policy["allowed_path_prefixes"]),
            "candidate path outside source scope",
        )
        seen.add(path)
        _need(row["before"] is not None or row["after"] is not None, "empty file delta")
        for side in ("before", "after"):
            blob = row[side]
            if blob is None:
                continue
            _exact(
                blob,
                {"mode", "git_oid", "sha256", *({"bytes_base64"} if side == "after" else set())},
            )
            _need(
                type(blob["mode"]) is int
                and blob["mode"] in {100644, 100755}
                and _oid(blob["git_oid"])
                and _sha(blob["sha256"]),
                "regular blob identity invalid",
            )
            if side == "after":
                encoded = blob["bytes_base64"]
                _need(
                    type(encoded) is str
                    and len(encoded) <= 4 * ((policy["max_file_bytes"] + 2) // 3),
                    "candidate encoded blob bound",
                )
                try:
                    raw = base64.b64decode(encoded, validate=True)
                except (ValueError, TypeError) as error:
                    raise PublicationHeld("candidate bytes malformed") from error
                total += len(raw)
                _need(
                    len(raw) <= policy["max_file_bytes"]
                    and blob_oid(raw) == blob["git_oid"]
                    and hashlib.sha256(raw).hexdigest() == blob["sha256"],
                    "candidate byte identity differs",
                )
    _need(total <= policy["max_total_bytes"], "candidate aggregate byte bound")
    return _copy(value)


def prepare_intent(original, policy, pr, candidate, provenance):
    slot_key = stable_slot(original)
    policy, pr = validate_policy(policy), validate_owned_pr(pr)
    candidate = validate_candidate(candidate, policy)
    _exact(
        provenance,
        {
            "baseline_sha256",
            "handoff_sha256",
            "terminal_sha256",
            "source_scope_sha256",
            "original_admission_sha256",
            "fresh_io_admission_sha256",
            "current_operation_sha256",
        },
    )
    _need(
        all(_sha(v) for v in provenance.values())
        and original["consumer_id"] == policy["consumer_id"]
        and original["repository_id"] == pr["repository_id"]
        and candidate["base_oid"] == pr["base_oid"]
        and candidate["old_head_oid"] == pr["head_oid"],
        "publication original/native/candidate join differs",
    )
    return {
        "schema": INTENT_SCHEMA,
        "slot_key": slot_key,
        "original": _copy(original),
        "policy": policy,
        "pr": pr,
        "candidate": candidate,
        "provenance": _copy(provenance),
        **{k: False for k in _AUTHORITY},
    }


def validate_intent(value):
    _exact(
        value,
        {"schema", "slot_key", "original", "policy", "pr", "candidate", "provenance", *_AUTHORITY},
    )
    expected = prepare_intent(
        value["original"], value["policy"], value["pr"], value["candidate"], value["provenance"]
    )
    _need(
        value["schema"] == INTENT_SCHEMA and _bytes(value) == _bytes(expected),
        "contract intent changed",
    )
    return expected


def validate_commit_data(intent, value):
    intent = validate_intent(intent)
    _exact(
        value,
        {
            "commit_oid",
            "tree_oid",
            "parent_oids",
            "candidate_sha256",
            "returncode",
            "group_terminal",
        },
    )
    _need(
        _oid(value["commit_oid"])
        and value["commit_oid"] != intent["pr"]["head_oid"]
        and value["tree_oid"] == intent["candidate"]["tree_oid"]
        and value["parent_oids"] == [intent["pr"]["head_oid"]]
        and value["candidate_sha256"] == digest(intent["candidate"])
        and type(value["returncode"]) is int
        and value["returncode"] == 0
        and value["group_terminal"] is True,
        "single-parent exact candidate commit not observed",
    )
    return _copy(value)


def push_command_data(intent, commit):
    intent, commit = validate_intent(intent), validate_commit_data(intent, commit)
    pr = intent["pr"]
    ref = "refs/heads/" + pr["head_ref"]
    return {
        "schema": "closer-owned-pr-publication-command-data/v1",
        "argv": [
            intent["policy"]["git_executable"]["path"],
            "push",
            "--porcelain",
            "--verify",
            "--force-with-lease=" + ref + ":" + pr["head_oid"],
            "https://github.com/" + pr["repository"] + ".git",
            commit["commit_oid"] + ":" + ref,
        ],
        "expected_host_context_sha256": intent["policy"]["trusted_context_sha256"],
        "git_execution_authorized": False,
    }


def parse_push_ack_data(intent, commit, raw, returncode):
    command = push_command_data(intent, commit)
    _need(
        type(raw) is bytes
        and 0 < len(raw) <= intent["policy"]["max_ack_bytes"]
        and type(returncode) is int
        and returncode == 0,
        "original push acknowledgement unavailable",
    )
    try:
        lines = raw.decode("utf-8").splitlines()
    except UnicodeError as error:
        raise PublicationHeld("push acknowledgement encoding invalid") from error
    _need(
        len(lines) == 3 and lines[0] == "To " + command["argv"][-2] and lines[2] == "Done",
        "push acknowledgement destination differs",
    )
    fields = lines[1].split("\t")
    _need(
        len(fields) == 3 and fields[0] == " " and fields[1] == command["argv"][-1],
        "push acknowledgement is not one expected fast-forward ref",
    )
    match = re.fullmatch(r"([a-f0-9]{1,40})\.\.([a-f0-9]{1,40})", fields[2])
    _need(
        match is not None
        and intent["pr"]["head_oid"].startswith(match[1])
        and commit["commit_oid"].startswith(match[2]),
        "push acknowledgement heads differ",
    )
    return {
        "schema": "closer-owned-pr-push-ack-data/v1",
        "raw_base64": base64.b64encode(raw).decode(),
        "raw_sha256": hashlib.sha256(raw).hexdigest(),
        "commit_oid": commit["commit_oid"],
        "command_sha256": digest(command),
        "native_publication_authorized": False,
    }


def validate_readback_data(intent, commit, pr, ref, native_commit):
    intent, commit = validate_intent(intent), validate_commit_data(intent, commit)
    pr = validate_owned_pr(pr)
    _exact(ref, {"repository_id", "ref", "oid", "object_type"})
    _exact(native_commit, {"repository_id", "oid", "tree_oid", "parent_oids"})
    before = {**intent["pr"], "head_oid": commit["commit_oid"]}
    _need(
        _bytes(pr) == _bytes(before)
        and _bytes(ref)
        == _bytes(
            {
                "repository_id": pr["repository_id"],
                "ref": "refs/heads/" + pr["head_ref"],
                "oid": commit["commit_oid"],
                "object_type": "commit",
            }
        )
        and _bytes(native_commit)
        == _bytes(
            {
                "repository_id": pr["repository_id"],
                "oid": commit["commit_oid"],
                "tree_oid": commit["tree_oid"],
                "parent_oids": commit["parent_oids"],
            }
        ),
        "native PR/ref/commit readback differs",
    )
    return {
        "schema": "closer-owned-pr-readback-data/v1",
        "pr": pr,
        "ref": _copy(ref),
        "commit": _copy(native_commit),
        "native_publication_authorized": False,
    }


class ContractLedger:
    """Create-only necessary state machine in real custody. No external executor."""

    _NEXT = {
        "intent": {"commit_started"},
        "commit_started": {"commit_ack", "unknown"},
        "commit_ack": {"push_started"},
        "push_started": {"push_ack", "unknown"},
        "push_ack": {"readback", "unknown"},
        "readback": set(),
        "unknown": set(),
    }

    def __init__(self, custody):
        _need(type(custody) is ctl._Custody, "existing owned controller custody required")
        custody.check()
        self.custody = custody

    def begin(self, intent):
        intent = validate_intent(intent)
        name = "publication-contract-intent-" + intent["slot_key"] + ".json"
        names = os.listdir(self.custody.fd)
        _need(
            not any(
                n.startswith("publication-contract-" + intent["slot_key"] + "-") for n in names
            ),
            "publication slot history exists; never recreate intent",
        )
        _need(self.custody.create(name, intent), "publication slot already owned; read only")
        _need(
            _bytes(self.custody.read(name)) == _bytes(intent), "publication intent readback differs"
        )
        return intent["slot_key"]

    def read(self, key):
        _need(_sha(key), "publication slot malformed")
        intent = validate_intent(self.custody.read("publication-contract-intent-" + key + ".json"))
        _need(intent["slot_key"] == key, "publication custody slot differs")
        names = os.listdir(self.custody.fd)
        prefix = "publication-contract-" + key + "-"
        selected = [name for name in names if name.startswith(prefix)]
        event_names = sorted(
            name for name in selected if re.fullmatch(re.escape(prefix) + r"[0-9]{2}\.json", name)
        )
        _need(
            event_names == [f"{prefix}{i:02}.json" for i in range(1, len(event_names) + 1)]
            and len(event_names) <= 6
            and all(
                name in event_names
                or name in {prefix + "attempt-commit.json", prefix + "attempt-push.json"}
                for name in selected
            ),
            "publication custody history lost or malformed",
        )
        events, previous, state = [], digest(intent), "intent"
        for seq in range(1, len(event_names) + 1):
            name = f"publication-contract-{key}-{seq:02}.json"
            try:
                event = self.custody.read(name)
            except FileNotFoundError as error:
                raise PublicationHeld("publication event disappeared during read") from error
            _exact(
                event,
                {
                    "schema",
                    "slot_key",
                    "intent_sha256",
                    "sequence",
                    "previous_sha256",
                    "kind",
                    "payload",
                },
            )
            _need(
                event["schema"] == EVENT_SCHEMA
                and event["slot_key"] == key
                and event["intent_sha256"] == digest(intent)
                and type(event["sequence"]) is int
                and event["sequence"] == seq
                and event["previous_sha256"] == previous
                and event["kind"] in self._NEXT[state],
                "publication event chain invalid",
            )
            kind, payload = event["kind"], event["payload"]
            commit = next((e["payload"] for e in events if e["kind"] == "commit_ack"), None)
            if kind == "commit_ack":
                validate_commit_data(intent, payload)
            elif kind == "push_started":
                _need(
                    _bytes(payload) == _bytes(push_command_data(intent, commit)),
                    "push command custody changed",
                )
            elif kind == "push_ack":
                _need(type(payload) is dict, "push acknowledgement custody malformed")
                try:
                    raw = base64.b64decode(payload["raw_base64"], validate=True)
                except (ValueError, TypeError, KeyError) as error:
                    raise PublicationHeld("push acknowledgement custody malformed") from error
                _need(
                    _bytes(payload) == _bytes(parse_push_ack_data(intent, commit, raw, 0)),
                    "push acknowledgement custody changed",
                )
            elif kind == "readback":
                _need(
                    type(payload) is dict
                    and _bytes(payload)
                    == _bytes(
                        validate_readback_data(
                            intent, commit, payload["pr"], payload["ref"], payload["commit"]
                        )
                    ),
                    "readback custody changed",
                )
            elif kind == "unknown":
                _exact(payload, {"code"})
                _need(
                    type(payload["code"]) is str
                    and payload["code"]
                    in {
                        "transport_timeout",
                        "missing_ack",
                        "readback_unavailable",
                        "source_changed",
                    },
                    "unknown event code malformed",
                )
            else:
                _exact(payload, {})
            state, previous = kind, digest(event)
            events.append(event)
        return {"intent": intent, "events": events, "state": state, "last_sha256": previous}

    def _append(self, key, kind, payload):
        current = self.read(key)
        _need(kind in self._NEXT[current["state"]], "publication phase already owned; never repeat")
        event = {
            "schema": EVENT_SCHEMA,
            "slot_key": key,
            "intent_sha256": digest(current["intent"]),
            "sequence": len(current["events"]) + 1,
            "previous_sha256": current["last_sha256"],
            "kind": kind,
            "payload": _copy(payload),
        }
        name = f"publication-contract-{key}-{event['sequence']:02}.json"
        _need(self.custody.create(name, event), "publication event already owned")
        _need(
            _bytes(self.custody.read(name)) == _bytes(event), "publication event readback differs"
        )
        return self.read(key)

    def _reserve(self, key, phase):
        current = self.read(key)
        marker = {
            "schema": "closer-publication-attempt-data/v1",
            "slot_key": key,
            "intent_sha256": digest(current["intent"]),
            "phase": phase,
        }
        _need(
            self.custody.create(f"publication-contract-{key}-attempt-{phase}.json", marker),
            "publication attempt already owned; never repeat",
        )

    def commit_started(self, key):
        _need(self.read(key)["state"] == "intent", "commit attempt already owned")
        self._reserve(key, "commit")
        return self._append(key, "commit_started", {})

    def commit_ack(self, key, value):
        current = self.read(key)
        return self._append(key, "commit_ack", validate_commit_data(current["intent"], value))

    def push_started(self, key):
        current = self.read(key)
        commits = [e["payload"] for e in current["events"] if e["kind"] == "commit_ack"]
        _need(len(commits) == 1, "original commit acknowledgement unavailable")
        command = push_command_data(current["intent"], commits[0])
        _need(current["state"] == "commit_ack", "push attempt already owned")
        self._reserve(key, "push")
        self._append(key, "push_started", command)
        return command

    def push_ack(self, key, raw, returncode):
        current = self.read(key)
        commit = next(e["payload"] for e in current["events"] if e["kind"] == "commit_ack")
        return self._append(
            key, "push_ack", parse_push_ack_data(current["intent"], commit, raw, returncode)
        )

    def readback(self, key, pr, ref, native_commit):
        current = self.read(key)
        commit = next(e["payload"] for e in current["events"] if e["kind"] == "commit_ack")
        payload = validate_readback_data(current["intent"], commit, pr, ref, native_commit)
        self._append(key, "readback", payload)
        return {
            "schema": "closer-owned-pr-publication-contract-receipt/v1",
            "slot_key": key,
            "intent_sha256": digest(current["intent"]),
            "readback_sha256": digest(payload),
            "contract_consistent": True,
            **{k: False for k in _AUTHORITY},
        }

    def unknown(self, key, code):
        _need(
            type(code) is str
            and code
            in {"transport_timeout", "missing_ack", "readback_unavailable", "source_changed"},
            "fixed publication failure code required",
        )
        return self._append(key, "unknown", {"code": code})


def retain_baseline(root, controller, custody, operation):
    _live_worker_custody(root, controller, custody, operation, inspect.currentframe().f_back)
    observed = _SourceObservation(
        root, controller, custody, operation, inspect.currentframe().f_back
    )
    host = _observe_git_context(observed, _native_owned_pr(observed))
    try:
        baseline = _capture_baseline(host)
        return _BaselineLease(observed, host, baseline)
    except BaseException:
        host.close()
        raise


def validate_coding_workspace(origin, cwd):
    """Bind the actual owned native route to its authenticated publication worktree."""
    import closer_coding_worker as worker
    import dispatcher

    wrapper = origin.f_back
    caller = wrapper.f_back if wrapper is not None else None
    closure = dict(
        zip(dispatcher.offload.__code__.co_freevars, dispatcher.offload.__closure__ or ())
    )
    signature = closure.get("signature")
    _need(
        origin.f_code is dispatcher._CODING_DELIVERY_OFFLOAD_CODE
        and origin.f_globals is dispatcher.__dict__
        and wrapper is not None
        and wrapper.f_code is dispatcher.offload.__code__
        and wrapper.f_globals is dispatcher.__dict__
        and wrapper.f_locals.get("function") is dispatcher.offload.__wrapped__
        and signature is not None
        and wrapper.f_locals.get("signature") is signature.cell_contents
        and caller is not None
        and caller.f_code is worker._execute.__code__
        and caller.f_globals is worker.__dict__,
        "actual coding workspace source unavailable",
    )
    bound = wrapper.f_locals.get("bound")
    values = wrapper.f_locals.get("values")
    _need(
        type(bound) is inspect.BoundArguments
        and bound.signature is signature.cell_contents
        and bound.arguments is values
        and signature.cell_contents.bind(
            *wrapper.f_locals["args"], **wrapper.f_locals["kwargs"]
        ).arguments
        == values
        and values.get("agent") == origin.f_locals.get("agent") == "codex"
        and values.get("cwd") == origin.f_locals.get("cwd") == caller.f_locals.get("workspace")
        and values.get("data_only") is False,
        "actual coding workspace invocation differs",
    )
    baseline = caller.f_locals.get("publication_baseline_ref")
    policy = caller.f_locals.get("publication_policy")
    if baseline is None and policy is None:
        return
    _need(
        type(baseline) is _BaselineLease and policy is not None,
        "actual source baseline lease required",
    )
    baseline.check()
    observed = baseline.observed
    _need(
        observed.caller is caller
        and observed.root is caller.f_locals.get("root")
        and observed.controller is caller.f_locals.get("controller")
        and observed.operation is caller.f_locals.get("operation")
        and observed.custody is caller.f_locals.get("guard").custody
        and baseline.host.observed is observed
        and Path(cwd) == baseline.host.workspace == Path(caller.f_locals["workspace"]),
        "native coding cwd differs from authenticated publication worktree",
    )


def retain_handoff(root, controller, custody, operation, baseline_ref, result, terminal_ref):
    caller = inspect.currentframe().f_back
    _live_worker_custody(root, controller, custody, operation, caller)
    _need(
        caller.f_locals.get("publication_baseline_ref") is baseline_ref
        and caller.f_locals.get("result") is result
        and caller.f_locals.get("terminal_ref") is terminal_ref,
        "actual baseline/result/terminal identity required",
    )
    _need(type(baseline_ref) is _BaselineLease, "actual source baseline lease required")
    baseline_ref.check()
    import closer_coding_worker as worker

    terminal = custody.read(terminal_ref["name"])
    worker.validate_terminal_record(terminal)
    _need(
        terminal_ref["schema"] == worker.TERMINAL_REFERENCE_SCHEMA
        and terminal_ref["sha256"] == ctl.digest(terminal)
        and terminal_ref["name"] == "coding-worker-terminal-" + terminal_ref["sha256"] + ".json"
        and terminal["operation_sha256"] == digest(operation)
        and terminal["result_sha256"] == digest(result)
        and _bytes(terminal["continuation_reference"]) == _bytes(root.continuation_reference)
        and terminal["original"]["admission_sha256"] == ctl.digest(baseline_ref.observed.record)
        and terminal["stable_key"] == root.continuation_reference["stable_key"]
        and _bytes(terminal["native_worker"]["native_b_start"]) == _bytes(root.start_ref)
        and terminal["native_worker"]["worker_id"] == json.loads(root.accepted_utf8)["worker_id"],
        "actual coding terminal or returned result differs",
    )
    pr = _native_owned_pr(baseline_ref.observed)
    _need(_bytes(pr) == _bytes(baseline_ref.host.pr), "native corrective PR changed after coding")
    candidate = _capture_candidate(baseline_ref.host, baseline_ref.baseline)
    record = {
        "schema": "closer-publication-source-handoff/v3",
        "baseline_reference": baseline_ref.reference,
        "staging_link_name": "publication-staging-link-"
        + stable_slot(_original_slot(baseline_ref.observed))
        + ".json",
        "candidate": candidate,
        "terminal_reference": _copy(terminal_ref),
        "result_sha256": digest(result),
        "operation_sha256": digest(operation),
        "stable_slot": stable_slot(_original_slot(baseline_ref.observed)),
        **{k: False for k in _AUTHORITY},
    }
    name = "publication-source-handoff-" + digest(record) + ".json"
    baseline_ref.check()
    _need(custody.create(name, record), "publication handoff already owned")
    _need(_bytes(custody.read(name)) == _bytes(record), "publication handoff readback differs")
    baseline_ref.check()
    handoff_reference = {
        "schema": "closer-publication-source-handoff-reference/v1",
        "name": name,
        "sha256": digest(record),
    }
    # Exact known work is durable and issued before optional local preparation.
    # A later genuine IO phase may consume this handoff without another model.
    preparation_stage = "staging_preparation_held"
    try:
        staging_reference = _issue_private_staging(
            baseline_ref, candidate, terminal_ref, handoff_reference
        )
        preparation_stage = "staging_link_held"
        _link_staging_data(custody, handoff_reference, staging_reference)
    except Exception:
        try:
            _retain_staging_hold_data(custody, handoff_reference, preparation_stage)
        except Exception:
            pass  # Diagnostics must never erase already-issued known work.
    return handoff_reference


def run_if_selected(campaign, document, manifest, manifest_path, pending):
    import closer_coding_publication_io as publication_io

    return publication_io.run_if_selected(campaign, document, manifest, manifest_path, pending)


def _publication_selection(controller, custody, operation, intent_ref):
    import closer_coding_publication_io as publication_io

    return publication_io.select_existing_update(controller, custody, operation, intent_ref)


def _host_git_context(selection):
    import closer_coding_publication_io as publication_io

    return publication_io.host_git_context(selection)


def _selected_transport(controller, custody, operation, selection, host):
    import closer_coding_publication_io as publication_io

    return publication_io.selected_transport(controller, custody, operation, selection, host)


def _require_publication_io_admission(controller, custody, operation, selection, host):
    import closer_coding_publication_io as publication_io

    return publication_io.require_io_admission(controller, custody, operation, selection, host)


def _remaining_transport_seconds(operation, policy, now):
    deadline = operation.get("deadline") if type(operation) is dict else None
    _need(type(deadline) in (int, float), "original operation deadline unavailable")
    _need(type(now) in (int, float), "current observation time unavailable")
    import math

    _need(math.isfinite(deadline) and math.isfinite(now), "finite clock required")
    remaining = min(
        deadline - now, policy["max_native_io_seconds"], policy["request_timeout_seconds"]
    )
    _need(remaining > 0, "original publication deadline exhausted")
    return remaining


def _transport_observation(command, observation):
    _need(type(observation) is dict, "owned process observation unavailable")
    _need(observation.get("source") == "trusted-popen-transport/v1", "wrong transport source")
    _need(_bytes(observation.get("transport_argv")) == _bytes(command), "transport argv changed")
    pid = observation.get("pid")
    _need(type(pid) is int and pid > 0, "owned process pid unavailable")
    _need(type(observation.get("process_group")) is int, "owned process group unavailable")
    _need(observation["process_group"] == pid, "transport group differs")
    _need(
        type(observation.get("os_started")) is str and observation["os_started"],
        "owned process birth unavailable",
    )


def _observed_transport(controller, custody, operation, selection, host):
    """Fixed existing subprocess adapter; all source issuance remains mandatory.

    The descriptor issuer is deliberately unavailable. Mechanical call controls
    exercise the call expression only, not a fabricated successful authority path.
    A transport exception escapes with the already-owned WAL intact; no retry.
    """
    import dispatcher

    descriptor = _selected_transport(controller, custody, operation, selection, host)
    policy = validate_policy(descriptor["policy"])
    command = tuple(descriptor["argv"])
    environment = _copy(descriptor["env"])
    stdin = descriptor["stdin"]
    cwd = descriptor["cwd"]
    phase_frame = inspect.currentframe().f_back
    try:
        if phase_frame.f_code is _backend_phase_transport.__code__:
            _need(
                phase_frame.f_globals is globals()
                and all(
                    phase_frame.f_locals.get(k) is v
                    for k, v in {
                        "controller": controller,
                        "custody": custody,
                        "operation": operation,
                        "selection": selection,
                        "host": host,
                    }.items()
                )
                and _bytes(list(command)) == _bytes(phase_frame.f_locals["command"])
                and _bytes(_stdin_identity(stdin))
                == _bytes(_stdin_identity(phase_frame.f_locals["payload"]))
                and _bytes(policy) == _bytes(selection["intent"]["policy"]),
                "fixed backend phase descriptor differs",
            )
    finally:
        del phase_frame
    frozen = _bytes([command, environment, _stdin_identity(stdin), cwd])
    observation = {}
    owned = []

    def starting(actual):
        _need(actual is observation, "prelaunch observation identity changed")
        # This must rejoin the live source callback/writer, current owner/head,
        # HostGit fd/config, WAL and native readback capability on every effect.
        current = _selected_transport(controller, custody, operation, selection, host)
        _need(
            _bytes(
                [
                    tuple(current["argv"]),
                    current["env"],
                    _stdin_identity(current["stdin"]),
                    current["cwd"],
                ]
            )
            == frozen,
            "publication descriptor changed",
        )
        _remaining_transport_seconds(operation, policy, time.time())

    def started(actual):
        _need(not owned, "transport observed twice")
        _transport_observation(command, actual)
        owned.append(actual)

    def finished(actual):
        _need(len(owned) == 1 and actual is owned[0], "transport observation identity changed")
        _transport_observation(command, actual)
        _need(actual.get("cleanup_terminal") is True, "transport cleanup unknown")

    timeout = _remaining_transport_seconds(operation, policy, time.time())
    result = dispatcher.run_observed_offload(
        list(command),
        callback=started,
        observation=observation,
        cwd=cwd,
        timeout=timeout,
        stdin=_transport_stdin(stdin),
        finished_callback=finished,
        starting_callback=starting,
        env=environment,
        output_limit=policy["max_ack_bytes"],
    )
    _need(len(owned) == 1, "owned transport observation unavailable")
    finished(owned[0])
    return result, owned[0]


def execute_existing_update(controller, custody, operation, intent_ref):
    """Future source-only front door; unavailable issuers hold before all ports.

    No runner, command, environment, callback or authority JSON is accepted here.
    A completed transport alone cannot issue a native delivery receipt.
    """
    _need(type(controller) is ctl.IndexController, "actual source controller required")
    _need(type(custody) is ctl._Custody, "actual source custody required")
    selection = _publication_selection(controller, custody, operation, intent_ref)
    host = _host_git_context(selection)
    import closer_coding_publication_io as publication_io

    confirmed = None
    try:
        prepared = _prepare_mutation_backend(controller, custody, operation, selection, host)
        receipt = _execute_prepared_backend(
            controller, custody, operation, selection, host, prepared
        )
        confirmed = publication_io.retain_confirmed_result(
            controller, custody, operation, selection, host, receipt
        )
    finally:
        try:
            publication_io.close_selection(selection, host)
        except Exception:
            if confirmed is None:
                raise
            # Native ACK/readback/bind are already durable. Cleanup cannot
            # erase the returned selector or repeat this mutation sequence.
    return confirmed


def _raw_blob(value, policy):
    _exact(value, {"mode", "git_oid", "sha256", "bytes_base64"})
    _need(
        type(value["mode"]) is int and value["mode"] in {100644, 100755},
        "regular baseline blob required",
    )
    encoded = value["bytes_base64"]
    _need(
        type(encoded) is str and len(encoded) <= 4 * ((policy["max_file_bytes"] + 2) // 3),
        "baseline byte bound",
    )
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (TypeError, ValueError) as error:
        raise PublicationHeld("baseline bytes malformed") from error
    _need(
        len(raw) <= policy["max_file_bytes"]
        and blob_oid(raw) == value["git_oid"]
        and hashlib.sha256(raw).hexdigest() == value["sha256"],
        "baseline bytes differ",
    )
    return raw


def _tree_oid(files, *, objects=None):
    """SHA1 Git tree encoding over already-validated regular raw blob entries."""
    root = {}
    for row in files:
        current = root
        parts = row["path"].split("/")
        for part in parts[:-1]:
            _need(part not in current or type(current[part]) is dict, "file/directory conflict")
            current = current.setdefault(part, {})
        _need(parts[-1] not in current, "duplicate tree path")
        current[parts[-1]] = (row["blob"]["mode"], row["blob"]["git_oid"])

    def encode(node):
        entries = []
        for name, value in node.items():
            directory = type(value) is dict
            mode, oid = (40000, encode(value)) if directory else value
            entries.append(
                (
                    name.encode("utf-8") + (b"/" if directory else b""),
                    str(mode).encode() + b" " + name.encode("utf-8") + b"\0" + bytes.fromhex(oid),
                )
            )
        raw = b"".join(value for _, value in sorted(entries))
        framed = b"tree " + str(len(raw)).encode() + b"\0" + raw
        oid = hashlib.sha1(framed).hexdigest()
        if objects is not None:
            objects[oid] = framed
        return oid

    return encode(root)


def validate_baseline_data(value, policy):
    """Complete bounded DATA inventory, never proof of workspace/source custody."""
    import unicodedata

    def inventory_key(path):
        # Comparison only: keep original Git bytes and refuse filesystem aliases.
        return unicodedata.normalize("NFC", unicodedata.normalize("NFC", path).casefold())

    if type(policy) is dict and policy.get("schema") == ANCHOR_POLICY_SCHEMA:
        import unicodedata

        policy = validate_policy(policy)
        bound = _inventory_policy(policy)
        _exact(value, {"schema", "base_oid", "head_oid", "tree_oid", "files"})
        _need(
            value["schema"] == "closer-publication-baseline-data/v2"
            and all(_oid(value[k]) for k in ("base_oid", "head_oid", "tree_oid")),
            "baseline Git anchor differs",
        )
        rows, total, seen, prefixes = value["files"], 0, set(), {}
        _need(
            type(rows) is list and len(rows) <= bound["max_files"], "baseline inventory count bound"
        )
        for row in rows:
            _exact(row, {"path", "blob"})
            path = _inventory_path(row["path"], policy)
            key = unicodedata.normalize("NFC", path).casefold()
            _need(key not in seen, "baseline path alias")
            seen.add(key)
            parts = path.split("/")
            for end in range(1, len(parts) + 1):
                part = "/".join(parts[:end])
                normalized = unicodedata.normalize("NFC", part).casefold()
                _need(prefixes.setdefault(normalized, part) == part, "baseline directory alias")
            blob = row["blob"]
            _exact(blob, {"mode", "git_oid", "sha256", "size"})
            _need(
                type(blob["mode"]) is int
                and blob["mode"] in {100644, 100755}
                and _oid(blob["git_oid"])
                and _sha(blob["sha256"])
                and type(blob["size"]) is int
                and 0 <= blob["size"] <= bound["max_file_bytes"],
                "baseline blob metadata differs",
            )
            total += blob["size"]
        _need(
            total <= bound["max_total_bytes"]
            and [r["path"] for r in rows] == sorted(r["path"] for r in rows)
            and not any(
                "/".join(k.split("/")[:end]) in seen
                for k in seen
                for end in range(1, len(k.split("/")))
            ),
            "baseline bytes/order/prefix bound",
        )
        _need(
            _tree_oid(rows) == value["tree_oid"] and len(_bytes(value)) <= bound["max_ack_bytes"],
            "complete baseline tree/envelope differs",
        )
        return _copy(value)
    policy = validate_policy(policy)
    _exact(value, {"schema", "base_oid", "head_oid", "tree_oid", "files"})
    _need(
        value["schema"] == "closer-publication-baseline-data/v1"
        and all(_oid(value[k]) for k in ("base_oid", "head_oid", "tree_oid")),
        "baseline identity unavailable",
    )
    rows = value["files"]
    _need(type(rows) is list and len(rows) <= policy["max_files"], "baseline inventory bound")
    seen, total = set(), 0
    spellings = {}
    for row in rows:
        _exact(row, {"path", "blob"})
        path = _path(row["path"])
        _need(inventory_key(path) not in seen, "case/normalization-aliased baseline path")
        seen.add(inventory_key(path))
        parts = path.split("/")
        for i in range(1, len(parts) + 1):
            prefix = "/".join(parts[:i])
            _need(
                spellings.setdefault(inventory_key(prefix), prefix) == prefix,
                "case/normalization-aliased directory",
            )
        total += len(_raw_blob(row["blob"], policy))
    _need(total <= policy["max_total_bytes"], "baseline aggregate byte bound")
    _need(
        [r["path"] for r in rows] == sorted(r["path"] for r in rows),
        "canonical baseline order required",
    )
    # Prefix aliases must also hold on case-insensitive target filesystems.
    for path in seen:
        _need(
            not any("/".join(path.split("/")[:i]) in seen for i in range(1, len(path.split("/")))),
            "file/directory alias",
        )
    _need(_tree_oid(rows) == value["tree_oid"], "baseline tree differs")
    return _copy(value)


def validate_candidate_against_baseline(baseline, candidate, policy):
    baseline = validate_baseline_data(baseline, policy)
    candidate = validate_candidate(candidate, policy)
    _need(
        candidate["base_oid"] == baseline["base_oid"]
        and candidate["old_head_oid"] == baseline["head_oid"]
        and candidate["old_tree_oid"] == baseline["tree_oid"],
        "candidate baseline differs",
    )
    old = {r["path"]: r["blob"] for r in baseline["files"]}
    updated = _copy(old)
    changed = set()
    for row in candidate["files"]:
        path = row["path"]
        changed.add(path)
        before = old.get(path)
        expected = (
            None
            if before is None
            else {k: v for k, v in before.items() if k not in {"bytes_base64", "size"}}
        )
        _need(_bytes(expected) == _bytes(row["before"]), "candidate before bytes differ")
        _need(_bytes(before) != _bytes(row["after"]), "empty candidate change")
        if row["after"] is None:
            updated.pop(path, None)
        else:
            updated[path] = (
                {k: row["after"][k] for k in ("mode", "git_oid", "sha256")}
                | {"size": len(_raw_blob(row["after"], policy))}
                if policy["schema"] == ANCHOR_POLICY_SCHEMA
                else row["after"]
            )
    unchanged = [{"path": p, "blob": old[p]} for p in sorted(old) if p not in changed]
    _need(digest(unchanged) == candidate["unchanged_tree_sha256"], "unchanged inventory differs")
    after = {
        **baseline,
        "tree_oid": candidate["tree_oid"],
        "files": [{"path": p, "blob": updated[p]} for p in sorted(updated)],
    }
    validate_baseline_data(after, policy)
    _need(len(_bytes(candidate)) <= 1048576, "complete candidate exceeds custody bound")
    return candidate


def retain_baseline_data(custody, value, policy):
    _need(type(custody) is ctl._Custody, "actual private byte custody required")
    value = validate_baseline_data(value, policy)
    name = "publication-baseline-data-" + digest(value) + ".json"
    _need(custody.create(name, value), "baseline data already retained")
    _need(_bytes(custody.read(name)) == _bytes(value), "baseline custody readback differs")
    return {
        "schema": "closer-publication-baseline-data-reference/v1",
        "name": name,
        "sha256": digest(value),
    }


def retain_payload_data(custody, raw, policy):
    _need(type(custody) is ctl._Custody, "actual private byte custody required")
    policy = validate_policy(policy)
    _need(type(raw) is bytes and len(raw) <= policy["max_total_bytes"], "raw payload bound")
    value = {
        "schema": "closer-publication-stdin-data/v1",
        "size": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "bytes_base64": base64.b64encode(raw).decode("ascii"),
    }
    name = "publication-stdin-data-" + digest(value) + ".json"
    _need(custody.create(name, value), "payload data already retained")
    _need(_bytes(custody.read(name)) == _bytes(value), "payload custody readback differs")
    return {
        "schema": "closer-publication-stdin-data-reference/v1",
        "name": name,
        "sha256": digest(value),
    }


class _PayloadData:
    """Unlinked private raw stdin descriptor DATA; no Git execution capability."""

    def __init__(self, custody, reference, policy):
        import tempfile

        _need(type(custody) is ctl._Custody, "actual private byte custody required")
        policy = validate_policy(policy)
        _exact(reference, {"schema", "name", "sha256"})
        _need(
            reference["schema"] == "closer-publication-stdin-data-reference/v1"
            and _sha(reference["sha256"])
            and reference["name"] == "publication-stdin-data-" + reference["sha256"] + ".json",
            "payload reference differs",
        )
        value = custody.read(reference["name"])
        _exact(value, {"schema", "size", "sha256", "bytes_base64"})
        _need(
            digest(value) == reference["sha256"]
            and value["schema"] == "closer-publication-stdin-data/v1"
            and type(value["size"]) is int
            and 0 <= value["size"] <= policy["max_total_bytes"],
            "payload record differs",
        )
        try:
            raw = base64.b64decode(value["bytes_base64"], validate=True)
        except (TypeError, ValueError) as error:
            raise PublicationHeld("payload encoding invalid") from error
        _need(
            len(raw) == value["size"] and hashlib.sha256(raw).hexdigest() == value["sha256"],
            "payload bytes differ",
        )
        self._limit = policy["max_total_bytes"]
        self.stream = tempfile.TemporaryFile(mode="w+b")
        try:
            os.fchmod(self.stream.fileno(), 0o600)
            self.stream.write(raw)
            self.stream.flush()
            self.stream.seek(0)
            self.identity = self._snapshot()
        except BaseException:
            self.stream.close()
            raise

    def _snapshot(self):
        import stat

        _need(not self.stream.closed and self.stream.tell() == 0, "stdin descriptor not at origin")
        meta = os.fstat(self.stream.fileno())
        _need(
            stat.S_ISREG(meta.st_mode)
            and meta.st_uid == os.getuid()
            and stat.S_IMODE(meta.st_mode) == 0o600
            and meta.st_nlink == 0
            and 0 <= meta.st_size <= self._limit,
            "private stdin descriptor changed",
        )
        raw = self.stream.read()
        self.stream.seek(0)
        _need(len(raw) == meta.st_size, "stdin descriptor bytes changed")
        return {
            "fd": self.stream.fileno(),
            "dev": meta.st_dev,
            "ino": meta.st_ino,
            "size": meta.st_size,
            "sha256": hashlib.sha256(raw).hexdigest(),
        }

    def check(self):
        _need(
            _bytes(self._snapshot()) == _bytes(self.identity), "stdin identity or payload changed"
        )
        return _copy(self.identity)

    def close(self):
        self.stream.close()


def _stdin_identity(value):
    if value is None:
        return None
    _need(type(value) is _PayloadData, "source-selected private stdin descriptor required")
    return value.check()


def _transport_stdin(value):
    _stdin_identity(value)
    return None if value is None else value.stream


def read_baseline_data(custody, reference, policy):
    _need(type(custody) is ctl._Custody, "actual private byte custody required")
    _exact(reference, {"schema", "name", "sha256"})
    _need(
        reference["schema"] == "closer-publication-baseline-data-reference/v1"
        and _sha(reference["sha256"])
        and reference["name"] == "publication-baseline-data-" + reference["sha256"] + ".json",
        "baseline reference differs",
    )
    value = validate_baseline_data(custody.read(reference["name"]), policy)
    _need(digest(value) == reference["sha256"], "baseline custody digest differs")
    return value


def staging_payload_data(baseline, candidate, policy):
    """Closed Git input bytes only; caller cannot promote them to execution authority.

    The future HostGit must isolate its index and object store, import each exact
    raw blob, initialize from old_tree_oid, apply this NUL-delimited index input,
    and verify write-tree against candidate.tree_oid before normal commit hooks.
    """
    candidate = validate_candidate_against_baseline(baseline, candidate, policy)
    blobs, index = [], bytearray()
    for row in sorted(candidate["files"], key=lambda row: row["path"]):
        after = row["after"]
        if after is None:
            mode, oid = 0, "0" * 40
        else:
            raw = _raw_blob(after, policy)
            mode, oid = after["mode"], after["git_oid"]
            blobs.append(
                {
                    "git_oid": oid,
                    "size": len(raw),
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "bytes_base64": base64.b64encode(raw).decode("ascii"),
                }
            )
        index.extend(
            str(mode).encode("ascii")
            + b" "
            + oid.encode("ascii")
            + b"\t"
            + row["path"].encode("utf-8")
            + b"\0"
        )
    return {
        "schema": "closer-publication-staging-payload-data/v1",
        "baseline_sha256": digest(baseline),
        "candidate_sha256": digest(candidate),
        "initial_tree_oid": baseline["tree_oid"],
        "expected_tree_oid": candidate["tree_oid"],
        "blobs": blobs,
        "index_bytes_base64": base64.b64encode(index).decode("ascii"),
        "index_sha256": hashlib.sha256(index).hexdigest(),
        "git_execution_authorized": False,
    }


_ORIGINAL_UNSUPPLIED = object()


def publication_key(value):
    _need(type(value) is dict, "publication registration/snapshot must be an object")
    keys = set(value) & {"publication_v1", "publication_v2"}
    _need(len(keys) <= 1, "mixed publication policy versions unsupported")
    return next(iter(keys), None)


def validate_consumer_mode(consumer_id, registration, original=_ORIGINAL_UNSUPPLIED):
    """Closed prospective migration; never upgrade an original source admission."""
    key = publication_key(registration)
    current = None
    if key is not None:
        current = validate_policy(registration[key])
        _need(current["consumer_id"] == consumer_id, "publication consumer differs")
        _need(
            current["schema"]
            == (POLICY_SCHEMA if key == "publication_v1" else ANCHOR_POLICY_SCHEMA),
            "publication policy version/key differs",
        )
    if original is not _ORIGINAL_UNSUPPLIED:
        _need(
            publication_key(original) == key,
            "publication mode cannot be added, removed or migrated after admission",
        )
        if current is not None:
            admitted = validate_policy(original[key])
            _need(_bytes(admitted) == _bytes(current), "original publication policy differs")
    return current


def _live_worker_custody(root, controller, custody, operation, caller):
    import closer_coding_worker as worker

    _need(
        caller.f_code is worker._execute.__code__
        and caller.f_globals is worker.__dict__
        and caller.f_locals.get("root") is root
        and caller.f_locals.get("controller") is controller
        and caller.f_locals.get("operation") is operation,
        "actual publication coding callback unavailable",
    )
    _need(
        type(controller) is ctl.IndexController and type(custody) is ctl._Custody,
        "actual controller writer custody required",
    )
    _need(type(root) is worker._Root, "actual source coding root required")
    # Exact scope.check below begins with this same Root validation.
    scope = getattr(worker._CURRENT, "continuation", None)
    _need(
        type(scope) is worker._Continuation
        and scope.root is root
        and scope.controller is controller
        and scope.custody is custody
        and _bytes(scope.operation) == _bytes(operation),
        "live original coding custody differs",
    )
    scope.check()
    controller._full_writer_validation(operation, custody)
    _require_source_pin(root.installation)


def _require_source_pin(installation):
    path = Path(__file__)
    _need(
        path.is_absolute()
        and path.resolve(strict=True) == path
        and not any(p.is_symlink() for p in (path, *path.parents))
        and installation.source_closure.get(path.name)
        == {"path": str(path), "sha256": _LOADED_SOURCE_SHA256}
        and hashlib.sha256(path.read_bytes()).hexdigest() == _LOADED_SOURCE_SHA256,
        "pinned publication source unavailable",
    )


_SOURCE_SEAL = object()


class _SourceObservation:
    """Actual callback/frame ownership for the fixed observation ports only."""

    def __init__(self, root, controller, custody, operation, caller):
        frame = inspect.currentframe().f_back
        _need(
            frame.f_code in {retain_baseline.__code__, retain_handoff.__code__}
            and frame.f_globals is globals(),
            "source observation constructor unavailable",
        )
        self.seal = _SOURCE_SEAL
        self.root, self.controller, self.custody = root, controller, custody
        self.operation, self.caller = operation, caller
        self.check()
        import closer_coding_session_v2 as session
        import closer_coding_worker as worker

        scope = worker._CURRENT.continuation
        original = session.snapshot_from_admission(scope.record)
        self.record = scope.record
        self.policy = validate_consumer_mode(
            root.purpose["consumer_id"],
            controller.config.consumers[root.purpose["consumer_id"]],
            original,
        )
        _need(self.policy is not None, "source publication mode absent")

    @property
    def consumer_id(self):
        return self.root.purpose["consumer_id"]

    @property
    def target(self):
        return self.root.purpose["target"]

    @property
    def workspaces(self):
        return self.root.backend.workspaces

    def check(self):
        _need(self.seal is _SOURCE_SEAL, "source observation seal differs")
        frame = inspect.currentframe()
        while frame is not None and frame is not self.caller:
            frame = frame.f_back
        _need(frame is self.caller, "original publication callback ended")
        _live_worker_custody(self.root, self.controller, self.custody, self.operation, self.caller)


def _native_owned_pr(observed):
    import closer_correction_native_effects as effects
    import closer_dispatcher as closer

    observed.check()
    request = observed.record["request"]
    incumbent = request["incumbent"]
    _need(incumbent.get("kind") != "no_incumbent", "publication requires native incumbent")
    registration = observed.controller.config.consumers[observed.consumer_id]
    observed.controller.fresh_issue_context(request, observed.record, registration)
    effects._native_writer(observed.controller, observed.operation)
    target = observed.controller.config.repository + "#" + incumbent["issue"]["number"]
    snapshot = closer.GitHub(
        deadline=min(
            observed.operation["deadline"], time.time() + observed.policy["max_native_io_seconds"]
        )
    ).snapshot(target)
    links = snapshot.get("delivery_links") or {}
    _need(
        links.get("status") == "resolved"
        and links.get("complete") is True
        and links.get("method") == "unique_link"
        and links.get("candidates") == [links.get("selected")],
        "unique native corrective PR unavailable",
    )
    pr_target = links["selected"]
    _need(
        type(pr_target) is str
        and pr_target.split("#", 1)[0] == observed.controller.config.repository
        and pr_target == snapshot.get("pr_target")
        and snapshot.get("kind") == "pr",
        "native corrective PR repository differs",
    )
    sources = snapshot.get("sources")
    _need(
        type(sources) is list
        and len(sources) == 1
        and sources[0].get("target") == target
        and sources[0].get("node_id") == incumbent["issue"]["node_id"]
        and sources[0].get("provenance", {}).get("via") == "closingIssuesReferences",
        "reciprocal native closing-issue linkage unavailable",
    )
    number = pr_target.split("#")[1]
    _need(re.fullmatch(r"[1-9][0-9]*", number) is not None, "native PR number invalid")
    pull = observed.controller.store.call("GET", "/pulls/" + number)
    pr = _owned_pr_projection(
        pull, snapshot, incumbent["issue"]["node_id"], observed.controller.config
    )
    observed.check()
    return pr


def _owned_pr_projection(pull, snapshot, issue_node_id, config):
    _need(type(pull) is dict and type(snapshot) is dict, "native PR read unavailable")
    head, base, author = pull.get("head") or {}, pull.get("base") or {}, pull.get("user") or {}
    _need(
        snapshot.get("pr_target") == config.repository + "#" + str(pull.get("number"))
        and head.get("sha") == snapshot.get("head")
        and base.get("sha") == snapshot.get("base")
        and head.get("ref") == snapshot.get("head_branch")
        and (head.get("repo") or {}).get("full_name") == snapshot.get("head_repository")
        and pull.get("body") == snapshot.get("body")
        and snapshot.get("state") == "OPEN"
        and snapshot.get("draft") is False
        and pull.get("merged") is False,
        "native corrective PR changed during read",
    )
    pr = dict(
        repository=config.repository,
        repository_id=config.repository_id,
        pr_number=pull.get("number"),
        pr_node_id=pull.get("node_id"),
        issue_node_id=issue_node_id,
        owner_login=author.get("login"),
        owner_id=author.get("id"),
        writer_login=config.writer_login,
        writer_id=config.writer_id,
        head_repo_id=(head.get("repo") or {}).get("id"),
        base_repo_id=(base.get("repo") or {}).get("id"),
        head_ref=head.get("ref"),
        head_oid=head.get("sha"),
        base_ref=base.get("ref"),
        base_oid=base.get("sha"),
        body_sha256=hashlib.sha256((pull.get("body") or "").encode()).hexdigest(),
        state=pull.get("state"),
        is_draft=pull.get("draft"),
        is_merged=pull.get("merged"),
        closing_issue_node_ids=[issue_node_id],
    )
    return validate_owned_pr(pr)


class _PhysicalFile:
    """Private held fd and named identity; metadata-only mode never reads secrets."""

    def __init__(self, path, bound, *, metadata_only=False):
        import stat

        self.path, self.bound, self.metadata_only = Path(path), bound, metadata_only
        _need(
            self.path.is_absolute()
            and self.path.resolve(strict=True) == self.path
            and not any(p.is_symlink() for p in (self.path, *self.path.parents)),
            "physical publication file redirected",
        )
        self.fd = None
        self.ancestors = _AncestorFDs(self.path)
        try:
            self.fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            meta = os.fstat(self.fd)
            _need(
                stat.S_ISREG(meta.st_mode)
                and meta.st_uid in {0, os.getuid()}
                and meta.st_nlink == 1
                and not meta.st_mode & 0o022
                and 0 <= meta.st_size <= bound,
                "publication file ownership or bounds differ",
            )
            self.identity = self._identity(meta)
            self.sha256 = None if metadata_only else self._hash()
            self.check()
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _identity(meta):
        return (
            meta.st_dev,
            meta.st_ino,
            meta.st_uid,
            meta.st_mode,
            meta.st_nlink,
            meta.st_size,
            meta.st_mtime_ns,
            meta.st_ctime_ns,
        )

    def raw(self):
        _need(not self.metadata_only and self.fd is not None, "metadata-only file cannot be read")
        os.lseek(self.fd, 0, os.SEEK_SET)
        raw = os.read(self.fd, self.bound + 1)
        _need(len(raw) <= self.bound, "publication file byte bound")
        return raw

    def _hash(self):
        _need(not self.metadata_only and self.fd is not None, "metadata-only file cannot be read")
        os.lseek(self.fd, 0, os.SEEK_SET)
        value, count = hashlib.sha256(), 0
        while True:
            chunk = os.read(self.fd, 65536)
            if not chunk:
                break
            count += len(chunk)
            _need(count <= self.bound, "publication file byte bound")
            value.update(chunk)
        return value.hexdigest()

    def check(self):
        self.ancestors.check()
        _need(
            self.fd is not None
            and self._identity(os.fstat(self.fd)) == self.identity
            and self._identity(self.path.stat(follow_symlinks=False)) == self.identity,
            "publication file descriptor or named identity changed",
        )
        if not self.metadata_only and self.sha256 is not None:
            _need(
                self._hash() == self.sha256
                and self._identity(os.fstat(self.fd)) == self.identity
                and self._identity(self.path.stat(follow_symlinks=False)) == self.identity,
                "publication file bytes changed",
            )
        return {
            "path": str(self.path),
            "identity": [str(value) for value in self.identity],
            "sha256": self.sha256,
        }

    def close(self):
        errors = []
        if self.fd is not None:
            fd, self.fd = self.fd, None
            try:
                os.close(fd)
            except Exception:
                errors.append(True)
        try:
            self.ancestors.close()
        except Exception:
            errors.append(True)
        _need(not errors, "publication file/ancestor cleanup unknown")


def _configuration_names(raw, bound):
    """Git's documented scope-NUL/origin-NUL/key-NUL name-only wire shape."""
    _need(
        type(raw) is bytes and len(raw) <= bound and (not raw or raw.endswith(b"\0")),
        "bounded config-name observation unavailable",
    )
    try:
        parts = raw[:-1].decode("utf-8").split("\0") if raw else []
    except UnicodeError:
        raise PublicationHeld("config-name encoding unsupported") from None
    _need(len(parts) % 3 == 0, "config-name observation shape unsupported")
    rows = []
    for i in range(0, len(parts), 3):
        scope, origin, key = parts[i : i + 3]
        _need(
            scope in {"system", "global", "local"}
            and origin.startswith("file:")
            and (
                key
                in {
                    "core.repositoryformatversion",
                    "core.filemode",
                    "core.bare",
                    "core.logallrefupdates",
                    "core.ignorecase",
                    "core.precomposeunicode",
                    "remote.origin.url",
                    "remote.origin.fetch",
                    "user.name",
                    "user.email",
                    "credential.helper",
                    "credential.https://github.com.helper",
                }
                or re.fullmatch(r"branch\.[A-Za-z0-9][A-Za-z0-9._/-]*\.(?:remote|merge)", key)
                is not None
            ),
            "unsupported config provenance or executable setting",
        )
        rows.append((scope, origin[5:], key))
    return rows


def _read_command(kind, pr=None):
    fixed = {
        "config_names": [
            "config",
            "--null",
            "--show-scope",
            "--show-origin",
            "--name-only",
            "--list",
        ],
        "system_paths": ["var", "GIT_CONFIG_SYSTEM"],
        "global_paths": ["var", "GIT_CONFIG_GLOBAL"],
        "helper_shell": ["var", "GIT_SHELL_PATH"],
        "helpers": ["config", "--null", "--get-all", "credential.helper"],
        "object_inventory": ["cat-file", "--batch-check=%(objectname) %(objecttype) %(objectsize)"],
        "github_helpers": ["config", "--null", "--get-all", "credential.https://github.com.helper"],
        "head": ["rev-parse", "--verify", "HEAD^{commit}"],
        "branch": ["symbolic-ref", "HEAD"],
        "tree": ["ls-tree", "-r", "-z", "--full-tree", pr["head_oid"]] if pr else None,
        "index": ["ls-files", "--stage", "-z"],
        "index_flags": ["ls-files", "-v", "-z"],
        "commit": ["cat-file", "commit", pr["head_oid"]] if pr else None,
    }
    _need(kind in fixed and fixed[kind] is not None, "fixed Git observation kind unsupported")
    return fixed[kind]


def _close_physical_controls(items):
    errors = []
    for item in reversed(items):
        try:
            item.close()
        except Exception:
            errors.append(True)
    _need(not errors, "publication physical control cleanup unknown")


def _private_git_spec(value):
    _exact(value, {"schema", "home", "config_sha256"})
    _need(
        value["schema"] == "closer-publication-private-git-environment/v1"
        and type(value["home"]) is str
        and Path(value["home"]).is_absolute()
        and str(Path(value["home"])) == value["home"]
        and ".." not in Path(value["home"]).parts
        and _sha(value["config_sha256"]),
        "private Git environment differs",
    )
    return _copy(value)


def private_git_environment_data(policy, original=None, *, held=None):
    """Necessary child environment DATA; never a permit or native principal proof.

    Root HOME and account/model routes are unchanged. Only Git children get the
    reviewed private global config; their fixed GH helper uses the original
    existing GitHub config directory explicitly, without reading token values.
    """
    import shutil
    import stat

    original = dict(os.environ) if original is None else dict(original)
    _need(
        not any(
            k.startswith(("GIT_", "DYLD_", "LD_"))
            or k in {"BASH_ENV", "ENV", "SHELLOPTS", "BASHOPTS", "CDPATH", "IFS"}
            for k in original
        ),
        "inherited Git/runtime override unsupported",
    )
    spec = policy.get("git_environment_v1")
    if spec is None:
        return original
    spec = _private_git_spec(spec)
    own = []
    try:
        home = Path(spec["home"])
        for path, entries in (
            (home, {".gitconfig", ".config"}),
            (home / ".config", {"git"}),
            (home / ".config/git", set()),
        ):
            item = _PhysicalDirectory(path, closed_entries=True)
            own.append(item)
            meta = path.stat(follow_symlinks=False)
            _need(
                meta.st_uid == os.getuid()
                and stat.S_IMODE(meta.st_mode) == 0o700
                and set(os.listdir(path)) == entries,
                "private Git home or closed config inventory differs",
            )
        config = _PhysicalFile(home / ".gitconfig", 65536)
        own.append(config)
        _need(config.sha256 == spec["config_sha256"], "private Git config differs")
        gh = shutil.which("gh", path=original.get("PATH"))
        _need(type(gh) is str, "existing fixed GitHub helper unavailable")
        route = _fixed_helper_route(
            b"!gh auth git-credential\0", str(Path(gh).resolve(strict=True)), original
        )
        result = {
            **original,
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "GH_CONFIG_DIR": route["config_directory"],
            # Override only inside this source-owned Git child. HOME alone
            # leaves executable system configuration active on hosted runners.
            "GIT_CONFIG_SYSTEM": str(home / ".gitconfig"),
            "GIT_CONFIG_GLOBAL": str(home / ".gitconfig"),
        }
        if held is not None:
            held.extend(own)
            own = []
        return result
    except OSError:
        raise PublicationHeld("private Git environment unavailable") from None
    finally:
        _close_physical_controls(own)


def _fixed_git_read(observed, workspace, kind, *, pr=None, files=(), missing=(), object_input=None):
    import dispatcher

    command = [
        observed.policy["git_executable"]["path"],
        "-C",
        str(workspace),
        *_read_command(kind, pr),
    ]
    original_environment = dict(os.environ)
    source_env = private_git_environment_data(observed.policy, original_environment)
    if observed.policy["schema"] == ANCHOR_POLICY_SCHEMA:
        source_env.update(GIT_NO_LAZY_FETCH="1", GIT_NO_REPLACE_OBJECTS="1")
    _need(
        (kind == "object_inventory") == (object_input is not None),
        "fixed object inventory input differs",
    )
    if object_input is not None:
        _need(
            type(object_input) is bytes
            and len(object_input) <= _inventory_policy(observed.policy)["max_ack_bytes"]
            and re.fullmatch(rb"(?:[a-f0-9]{40}\n)+", object_input) is not None,
            "bounded closed object IDs required",
        )
    ownership = []

    def starting(_):
        observed.check()
        _need(
            original_environment == dict(os.environ)
            and {
                k: v
                for k, v in source_env.items()
                if k not in {"GIT_NO_LAZY_FETCH", "GIT_NO_REPLACE_OBJECTS"}
            }
            == private_git_environment_data(observed.policy, original_environment),
            "publication observation environment changed",
        )
        if object_input is not None:
            import stat

            meta = os.fstat(private_input.fileno())
            _need(
                stat.S_ISREG(meta.st_mode)
                and meta.st_uid == os.getuid()
                and meta.st_nlink == 0
                and not meta.st_mode & 0o077
                and meta.st_size == len(object_input)
                and os.pread(private_input.fileno(), len(object_input) + 1, 0) == object_input,
                "fixed native object stdin changed",
            )
        for item in files:
            item.check()
        _need(
            not any(Path(p).exists() or Path(p).is_symlink() for p in missing),
            "previously absent configuration appeared",
        )

    def started(actual):
        _transport_observation(command, actual)
        _need(not ownership, "Git observation repeated")
        ownership.append(actual)

    def finished(actual):
        _need(len(ownership) == 1 and ownership[0] is actual, "Git observation identity changed")
        _transport_observation(command, actual)
        _need(actual.get("cleanup_terminal") is True, "Git observation cleanup unknown")

    private_input = None
    try:
        if object_input is not None:
            import tempfile

            private_input = tempfile.TemporaryFile(mode="w+b", dir=observed.custody.path)
            private_input.write(object_input)
            private_input.flush()
            private_input.seek(0)
        result = dispatcher.run_observed_offload(
            command,
            callback=started,
            observation={},
            cwd=str(workspace),
            timeout=_remaining_transport_seconds(observed.operation, observed.policy, time.time()),
            stdin=private_input,
            starting_callback=starting,
            finished_callback=finished,
            env=source_env,
            output_limit=(
                _inventory_policy(observed.policy)["max_ack_bytes"]
                if kind in {"tree", "index", "index_flags", "object_inventory"}
                else observed.policy["max_ack_bytes"]
            ),
        )
        _need(len(ownership) == 1, "Git observation unavailable")
        finished(ownership[0])
        _need(
            type(result.returncode) is int and result.returncode == 0,
            "Git observation unsuccessful",
        )
        _need(not ownership[0].get("invalid_utf8_output"), "non-UTF8 Git observation unsupported")
        raw = result.stdout.encode("utf-8")
        _need(
            len(raw)
            <= (
                _inventory_policy(observed.policy)["max_ack_bytes"]
                if kind in {"tree", "index", "index_flags", "object_inventory"}
                else observed.policy["max_ack_bytes"]
            ),
            "Git observation output bound",
        )
        return raw
    except Exception:
        # Never expose native argv/config output or subprocess error bodies.
        raise PublicationHeld("fixed Git observation unavailable") from None
    finally:
        if private_input is not None:
            private_input.close()


def _fixed_helper_route(helper_values, gh_path, environment):
    """Necessary nonsecret route shape only; no returned principal assertion."""
    import shlex

    _need(
        type(helper_values) is bytes and helper_values.endswith(b"\0"),
        "fixed Git helper route unavailable",
    )
    values = helper_values[:-1].split(b"\0")
    expected = {
        b"!gh auth git-credential",
        ("!" + shlex.quote(str(gh_path)) + " auth git-credential").encode(),
    }
    # Git's empty helper entry clears all earlier helpers. Only the exact
    # single source-selected GH helper may remain after that reset; without a
    # reset, any additional helper is still unsupported. No earlier helper
    # value is returned, logged, or executed by this observation.
    selected = values
    if b"" in values:
        reset = len(values) - 1 - values[::-1].index(b"")
        selected = values[reset + 1 :]
    _need(
        len(values) <= 32 and len(selected) == 1 and selected[0] in expected,
        "fixed Git helper route unsupported",
    )
    _need(
        environment.get("GH_HOST", "github.com") == "github.com"
        and not any(
            k in environment
            for k in (
                "GH_ENTERPRISE_TOKEN",
                "GITHUB_ENTERPRISE_TOKEN",
                "GH_DEBUG",
                "DEBUG",
                "GH_PATH",
            )
        ),
        "fixed Git helper environment unsupported",
    )
    explicit = environment.get("GH_CONFIG_DIR")
    if explicit is not None:
        directory = explicit
    elif "XDG_CONFIG_HOME" in environment:
        _need(
            type(environment["XDG_CONFIG_HOME"]) is str
            and Path(environment["XDG_CONFIG_HOME"]).is_absolute(),
            "native XDG configuration path unavailable",
        )
        directory = str(Path(environment["XDG_CONFIG_HOME"]) / "gh")
    else:
        home = environment.get("HOME")
        _need(type(home) is str and Path(home).is_absolute(), "native helper HOME unavailable")
        directory = str(Path(home) / ".config" / "gh")
    _need(
        type(directory) is str
        and Path(directory).is_absolute()
        and str(Path(directory)) == directory
        and ".." not in Path(directory).parts,
        "fixed Git helper configuration path unsupported",
    )
    slots = [key for key in ("GH_TOKEN", "GITHUB_TOKEN") if key in environment]
    _need(
        len(slots) <= 1
        and all(type(environment[k]) is str and bool(environment[k]) for k in slots),
        "ambiguous Git credential route unsupported",
    )
    # Existing stored auth is consumed only by the pinned native GH helper/API.
    # This data contains only route names; no token/keychain value is read here.
    # Existing environment token values remain inherited, never hashed/recorded.
    return {
        "schema": "closer-publication-helper-route-data/v1",
        "host": "github.com",
        "gh_path": str(gh_path),
        "config_directory": directory,
        "credential_slot": slots[0] if slots else "stored_github_cli",
        "native_principal_proven": False,
    }


def _collect_git_context_data(workspace, repository, policy, read, environment):
    """Same canonical physical/parser inventory for runtime and preparatory DATA.

    This collector cannot create HostGit/native-principal/Root authority. The
    runtime passes its original observed read transport and retains all handles.
    """
    import shutil

    import worktree_git

    _need(
        type(workspace) is str
        and not worktree_git.disabled()
        and worktree_git.is_provisioned(workspace),
        "provisioned publication workspace unavailable",
    )
    pins = worktree_git.expected_pins(workspace, repository)
    _need(type(pins) is tuple and len(pins) == 3, "publication Git topology refused")
    git_dir, common, worktree = pins
    held = []
    try:
        for path in pins:
            held.append(_PhysicalDirectory(path))
        binary = _PhysicalFile(policy["git_executable"]["path"], 256 * 1048576)
        held.append(binary)
        _need(
            binary.sha256 == policy["git_executable"]["sha256"],
            "pinned Git binary differs",
        )
        for path in (
            worktree / ".git",
            git_dir / "gitdir",
            git_dir / "commondir",
            git_dir / "HEAD",
        ):
            held.append(_PhysicalFile(path, policy["max_total_bytes"]))
        child_environment = private_git_environment_data(policy, environment, held=held)
        names = _configuration_names(
            read("config_names", files=held),
            policy["max_ack_bytes"],
        )
        # Includes, executable settings and nonfile sources were refused above.
        # Explicit paths from the selected Git cover absent global/system files too.
        paths = {str(common / "config")}
        for kind in ("system_paths", "global_paths"):
            raw = read(kind, files=held)
            try:
                paths.update(line for line in raw.decode().splitlines() if line)
            except UnicodeError:
                raise PublicationHeld("Git configuration path encoding unsupported") from None
        _need(all(Path(p).is_absolute() for p in paths), "configuration origin path unsupported")
        normalized = []
        missing = []
        for path in sorted(paths):
            target = Path(path)
            if target.exists() or target.is_symlink():
                item = _PhysicalFile(target, policy["max_total_bytes"])
                held.append(item)
                normalized.append(item.check())
            else:
                _need(
                    target.parent.is_dir() and not target.parent.is_symlink(),
                    "absent configuration parent unavailable",
                )
                held.append(_PhysicalDirectory(target.parent))
                missing.append(str(target))
        allowed = {str(Path(p)) for p in paths}
        _need(
            all(
                str((worktree / p).resolve() if not Path(p).is_absolute() else Path(p)) in allowed
                for _, p, _ in names
            ),
            "unobserved configuration origin",
        )
        # Empty/default hook directories only in this first supported closure.
        hooks = common / "hooks"
        held.append(_PhysicalDirectory(hooks, closed_entries=True))
        _need(
            hooks.is_dir()
            and not hooks.is_symlink()
            and all(
                p.is_file() and not p.is_symlink() and p.name.endswith(".sample")
                for p in hooks.iterdir()
            ),
            "configured active hooks require source closure",
        )
        unsupported_paths = (
            common / "info" / "attributes",
            common / "info" / "grafts",
            common / "objects" / "info" / "alternates",
            common / "refs" / "replace",
            common / "shallow",
            git_dir / "config.worktree",
        )
        _need(
            not any(p.exists() or p.is_symlink() for p in unsupported_paths),
            "Git replacement/filter metadata unsupported",
        )
        missing.extend(str(p) for p in unsupported_paths)
        _need(
            not (common / "info" / "attributes").exists()
            and not (common / "info" / "grafts").exists()
            and not (common / "objects" / "info" / "alternates").exists()
            and not (common / "refs" / "replace").exists(),
            "Git replacement/filter metadata unsupported",
        )
        source = {
            "schema": "closer-publication-hostgit-observation/v1",
            "git": binary.check(),
            "topology": [str(p) for p in pins],
            "config_files": normalized,
            "absent_config_files": missing,
            "config_name_inventory_sha256": digest(names),
        }
        raw_shell = read("helper_shell", files=held, missing=missing)
        try:
            shell_path = Path(raw_shell.decode("utf-8").strip())
        except UnicodeError:
            raise PublicationHeld("native helper shell encoding unsupported") from None
        shell_file = _PhysicalFile(shell_path, 256 * 1048576)
        held.append(shell_file)
        source["helper_shell"] = shell_file.check()
        gh = shutil.which("gh", path=environment.get("PATH"))
        _need(type(gh) is str, "native Git helper executable unavailable")
        gh_file = _PhysicalFile(Path(gh).resolve(strict=True), 256 * 1048576)
        held.append(gh_file)
        import shlex

        fixed_helper = ("!" + shlex.quote(str(gh_file.path)) + " auth git-credential").encode()
        route = _fixed_helper_route(b"\0" + fixed_helper + b"\0", gh_file.path, environment)
        auth_root = Path(route["config_directory"])
        held.append(_PhysicalDirectory(auth_root))
        for name in ("hosts.yml", "config.yml"):
            path = auth_root / name
            if path.exists() or path.is_symlink():
                held.append(_PhysicalFile(path, 1048576, metadata_only=True))
            else:
                _need(name == "config.yml", "native stored account metadata unavailable")
                missing.append(str(path))
        # API and helper must retain the same PATH selection and nonsecret auth
        # file identities. No credential content is read or retained.
        _need(
            len(environment.get("PATH", "").encode()) <= policy["max_ack_bytes"],
            "native helper PATH bound",
        )
        for directory in environment.get("PATH", "").split(os.pathsep):
            _need(
                directory and Path(directory).is_absolute(), "relative Git helper PATH unsupported"
            )
            held.append(_PhysicalDirectory(directory, immutable_children=True))
        source.update(
            gh=gh_file.check(),
            helper_route=route,
            auth_metadata=[
                item.check()
                for item in held
                if isinstance(item, _PhysicalFile) and item.metadata_only
            ],
            environment_sha256=digest(
                {k: environment.get(k) for k in ("PATH", "GH_CONFIG_DIR", "GH_HOST")}
            ),
        )
        if "git_environment_v1" in policy:
            source["private_git_environment"] = _copy(policy["git_environment_v1"])
            source["environment_sha256"] = digest(
                {
                    k: child_environment.get(k)
                    for k in ("PATH", "GH_CONFIG_DIR", "GH_HOST", "HOME", "XDG_CONFIG_HOME")
                }
            )
        result = (source, held, missing, route, gh_file)
        held = []  # Physical handles transfer to native producer or DATA-only inspection.
        return result
    finally:
        errors = []
        for item in reversed(held):
            try:
                item.close()
            except Exception:
                errors.append(True)
        _need(not errors, "publication observation cleanup unknown")


def inspect_git_context_data(workspace, repository, policy):
    """Read-only actual physical DATA, no permit or native principal attestation.

    The input deliberately omits trusted_context_sha256: this is the source
    inspection that computes it before original admission, not a placeholder.
    """
    import dispatcher

    policy = validate_policy(policy, _prepare_context=True)
    original = dict(os.environ)

    def read(kind, *, files=(), missing=()):
        environment = private_git_environment_data(policy, original)
        for item in files:
            item.check()
        _need(
            not any(Path(p).exists() or Path(p).is_symlink() for p in missing),
            "preparatory absent Git configuration changed",
        )
        result = dispatcher.run_observed_offload(
            [policy["git_executable"]["path"], "-C", str(workspace), *_read_command(kind)],
            callback=lambda _: None,
            observation={},
            cwd=str(workspace),
            stdin=None,
            env=environment,
            output_limit=policy["max_ack_bytes"],
            timeout=policy["max_native_io_seconds"],
        )
        _need(
            dict(os.environ) == original
            and result.returncode == 0
            and type(result.stdout) is str
            and len(result.stdout.encode("utf-8")) <= policy["max_ack_bytes"],
            "preparatory fixed Git observation unavailable",
        )
        for item in files:
            item.check()
        return result.stdout.encode("utf-8")

    facts, held, _missing, _route, _gh = _collect_git_context_data(
        workspace, repository, policy, read, original
    )
    try:
        return {
            "schema": "closer-publication-hostgit-preparation-data/v1",
            "trusted_context_sha256": digest(facts),
            "observed": facts,
            "native_principal_proven": False,
            "runtime_authority": False,
        }
    finally:
        _close_physical_controls(held)


def _observe_git_context(observed, pr):
    import closer_dispatcher as closer

    observed.check()
    workspace = closer.selected_workspace(observed.workspaces, observed.target)

    def read(kind, *, files=(), missing=()):
        return _fixed_git_read(observed, workspace, kind, files=files, missing=missing)

    environment = dict(os.environ)
    source, held, missing, route, gh_file = _collect_git_context_data(
        workspace, observed.controller.config.repository, observed.policy, read, environment
    )
    try:
        _need(
            digest(source) == observed.policy["trusted_context_sha256"],
            "source-approved Git configuration provenance differs",
        )
        route_proof = _observe_helper_api_route(observed, route, gh_file, held, environment)
        source["approved_context_sha256"] = observed.policy["trusted_context_sha256"]
        source["native_helper_principal"] = route_proof
        result = _HostGitLease(observed, pr, workspace, held, source, missing, environment)
        held = []
        return result
    finally:
        _close_physical_controls(held)


class _HostGitLease:
    """Only the actual context producer can construct this; no JSON constructor."""

    def __init__(self, observed, pr, workspace, held, summary, missing, environment):
        caller = inspect.currentframe().f_back
        _need(
            caller.f_code is _observe_git_context.__code__ and caller.f_globals is globals(),
            "source HostGit constructor unavailable",
        )
        self.observed, self.pr, self.workspace = observed, pr, Path(workspace)
        self.held, self.summary, self.missing = held, _copy(summary), tuple(missing)
        self.original_environment = dict(environment)
        self.environment = private_git_environment_data(observed.policy, environment)
        self.closed = False
        self.object_anchor = None

    def check(self):
        _need(not self.closed, "HostGit lease closed")
        self.observed.check()
        if self.object_anchor is not None:
            self.object_anchor.check()
        _need(
            dict(os.environ) == self.original_environment,
            "publication native route environment changed",
        )
        _need(
            self.environment
            == private_git_environment_data(self.observed.policy, self.original_environment),
            "publication child Git environment changed",
        )
        for item in self.held:
            item.check()
        _need(
            not any(Path(p).exists() or Path(p).is_symlink() for p in self.missing),
            "previously absent Git configuration appeared",
        )

    def read_lifetime(self):
        """Read-only loop uses the existing writer context, not effect authority.

        Full source/config/native checks bracket the complete inventory and
        still run at every native phase. Per-file reads retain their own
        descriptor/ancestor checks and this original queue/writer lifetime.
        """
        import closer_source_installation as installed

        _need(not self.closed, "HostGit lifetime ended")
        scope = getattr(self.observed.controller._writer_context, "validation_scope", None)
        _need(
            type(scope) is installed._OriginalWriterScope, "original source writer scope required"
        )
        scope.check_context(self.observed.controller.installation._binding)

    def read(self, kind):
        self.check()
        return _fixed_git_read(
            self.observed, self.workspace, kind, pr=self.pr, files=self.held, missing=self.missing
        )

    def close(self):
        if not self.closed:
            self.closed = True
            errors = []
            if self.object_anchor is not None:
                self.object_anchor.close()
            for item in reversed(self.held):
                try:
                    item.close()
                except Exception:
                    errors.append(True)
            _need(not errors, "publication descriptor cleanup unknown")


_BASELINE_CAPS = {
    "max_files": 4096,
    "max_file_bytes": 80 * 1048576,
    "max_total_bytes": 128 * 1048576,
    "max_inventory_bytes": 1048576,
}


def _baseline_limits(value):
    _exact(value, {"schema", *_BASELINE_CAPS})
    _need(
        value["schema"] == "closer-publication-baseline-limits/v1"
        and all(
            type(value[k]) is int and 0 < value[k] <= maximum
            for k, maximum in _BASELINE_CAPS.items()
        )
        and value["max_file_bytes"] <= value["max_total_bytes"],
        "baseline inventory limits differ",
    )
    return _copy(value)


def _inventory_policy(policy):
    if policy.get("schema") == ANCHOR_POLICY_SCHEMA:
        limits = policy["baseline_limits"]
        return {
            **policy,
            **{k: limits[k] for k in ("max_files", "max_file_bytes", "max_total_bytes")},
            "max_ack_bytes": limits["max_inventory_bytes"],
        }
    return policy


def _inventory_path(path, policy):
    if policy.get("schema") == ANCHOR_POLICY_SCHEMA and path.split("/")[-1] == ".gitattributes":
        _path(path + ".immutable-inventory")
        return path
    return _path(path)


def _inventory_blob(path, policy):
    """One bounded physical read; no unchanged contents retained anywhere."""
    item = _PhysicalFile(path, policy["max_file_bytes"], metadata_only=True)
    try:
        size = os.fstat(item.fd).st_size
        sha, git, count = hashlib.sha256(), hashlib.sha1(b"blob " + str(size).encode() + b"\0"), 0
        while True:
            chunk = os.read(item.fd, 65536)
            if not chunk:
                break
            count += len(chunk)
            _need(count <= size, "baseline file grew")
            sha.update(chunk)
            git.update(chunk)
        item.check()
        _need(count == size, "baseline file changed size")
        return {
            "mode": 100755 if os.fstat(item.fd).st_mode & 0o111 else 100644,
            "git_oid": git.hexdigest(),
            "sha256": sha.hexdigest(),
            "size": size,
        }
    finally:
        item.close()


def _tree_records(raw, policy):
    policy = _inventory_policy(policy)
    _need(
        type(raw) is bytes
        and len(raw) <= policy["max_ack_bytes"]
        and (not raw or raw.endswith(b"\0")),
        "bounded Git tree unavailable",
    )
    result = {}
    for entry in raw[:-1].split(b"\0") if raw else []:
        try:
            metadata, path = entry.split(b"\t", 1)
            mode, kind, oid = metadata.decode("ascii").split(" ")
            path = path.decode("utf-8")
        except (ValueError, UnicodeError):
            raise PublicationHeld("Git tree record unsupported") from None
        _need(
            mode in {"100644", "100755"} and kind == "blob" and _oid(oid),
            "regular Git tree required",
        )
        path = _inventory_path(path, policy)
        _need(path not in result and len(result) < policy["max_files"], "Git tree inventory bound")
        result[path] = (int(mode), oid)
    return result


def _index_records(raw, policy):
    policy = _inventory_policy(policy)
    _need(
        type(raw) is bytes
        and len(raw) <= policy["max_ack_bytes"]
        and (not raw or raw.endswith(b"\0")),
        "bounded Git index unavailable",
    )
    projected = bytearray()
    for entry in raw[:-1].split(b"\0") if raw else []:
        try:
            metadata, path = entry.split(b"\t", 1)
            mode, oid, stage = metadata.split(b" ")
        except ValueError:
            raise PublicationHeld("Git index record unsupported") from None
        _need(stage == b"0", "unmerged Git index unavailable")
        projected.extend(mode + b" blob " + oid + b"\t" + path + b"\0")
    return _tree_records(bytes(projected), policy)


def _physical_inventory(workspace, policy, check, *, original_paths, allow_new, full_check=None):
    policy = _inventory_policy(policy)
    rows, total, entries = [], 0, 0
    if full_check is not None:
        full_check()
    root = Path(workspace)
    _need(root.resolve(strict=True) == root and not root.is_symlink(), "workspace redirected")

    def visit(directory):
        nonlocal total, entries
        with os.scandir(directory) as stream:
            for entry in stream:
                if Path(directory) == root and entry.name == ".git":
                    continue
                entries += 1
                _need(entries <= 2 * policy["max_files"], "physical inventory entry bound")
                check()
                _need(not entry.is_symlink(), "symlink candidate path unsupported")
                if entry.is_dir(follow_symlinks=False):
                    visit(Path(directory) / entry.name)
                else:
                    _need(entry.is_file(follow_symlinks=False), "nonregular candidate path")
                    path = Path(directory) / entry.name
                    relative = _inventory_path(path.relative_to(root).as_posix(), policy)
                    _physical_path_scope(relative, original_paths, policy, allow_new)
                    if policy.get("schema") == ANCHOR_POLICY_SCHEMA:
                        blob = _inventory_blob(path, policy)
                        total += blob["size"]
                        _need(
                            total <= policy["max_total_bytes"] and len(rows) < policy["max_files"],
                            "physical inventory bound",
                        )
                        rows.append({"path": relative, "blob": blob})
                        continue
                    item = _PhysicalFile(path, policy["max_file_bytes"])
                    try:
                        raw = item.raw()
                        item.check()
                        total += len(raw)
                        _need(
                            total <= policy["max_total_bytes"] and len(rows) < policy["max_files"],
                            "candidate physical byte bound",
                        )
                        mode = 100755 if os.fstat(item.fd).st_mode & 0o111 else 100644
                        rows.append(
                            {
                                "path": relative,
                                "blob": {
                                    "mode": mode,
                                    "git_oid": blob_oid(raw),
                                    "sha256": hashlib.sha256(raw).hexdigest(),
                                    "bytes_base64": base64.b64encode(raw).decode("ascii"),
                                },
                            }
                        )
                    finally:
                        item.close()

    try:
        visit(root)
    finally:
        (full_check or check)()
    return sorted(rows, key=lambda row: row["path"])


def _capture_baseline(host):
    host.check()
    _need(
        host.read("head").strip().decode("ascii") == host.pr["head_oid"]
        and host.read("branch").strip().decode("utf-8") == "refs/heads/" + host.pr["head_ref"],
        "workspace is not native corrective head",
    )
    policy = host.observed.policy
    committed_tree = _commit_tree(host.read("commit"), host.pr["head_oid"], policy)
    tree = _tree_records(host.read("tree"), policy)
    _need(
        _index_flags(host.read("index_flags"), policy) == set(tree),
        "special original Git index flags unsupported",
    )
    _need(
        _index_records(host.read("index"), policy) == tree,
        "original index differs from native tree",
    )
    rows = _physical_inventory(
        host.workspace,
        policy,
        host.read_lifetime if policy["schema"] == ANCHOR_POLICY_SCHEMA else host.check,
        full_check=host.check if policy["schema"] == ANCHOR_POLICY_SCHEMA else None,
        original_paths=set(tree),
        allow_new=False,
    )
    _need(
        {r["path"]: (r["blob"]["mode"], r["blob"]["git_oid"]) for r in rows} == tree,
        "workspace bytes are not clean native head",
    )
    baseline = {
        "schema": (
            "closer-publication-baseline-data/v2"
            if policy["schema"] == ANCHOR_POLICY_SCHEMA
            else "closer-publication-baseline-data/v1"
        ),
        "base_oid": host.pr["base_oid"],
        "head_oid": host.pr["head_oid"],
        "tree_oid": _tree_oid(rows),
        "files": rows,
    }
    _need(baseline["tree_oid"] == committed_tree, "native commit tree and captured bytes differ")
    baseline = validate_baseline_data(baseline, policy)
    if policy["schema"] == ANCHOR_POLICY_SCHEMA:
        host.object_anchor = _borrow_existing_objects(host, baseline)
    return baseline


def _original_slot(observed):
    record = observed.record
    request = record["request"]
    return {
        "repository_id": observed.controller.config.repository_id,
        "epoch": observed.controller.config.epoch,
        "effect_key": ctl.effect_key(request),
        "request_sha256": ctl.digest(request),
        "consumer_id": observed.consumer_id,
    }


class _BaselineLease:
    def __init__(self, observed, host, baseline):
        caller = inspect.currentframe().f_back
        _need(
            caller.f_code is retain_baseline.__code__ and caller.f_globals is globals(),
            "source baseline constructor unavailable",
        )
        self.observed, self.host, self.baseline = observed, host, _copy(baseline)
        self.closed = False
        self.staging = None
        record = {
            "schema": (
                "closer-publication-source-baseline/v2"
                if observed.policy["schema"] == ANCHOR_POLICY_SCHEMA
                else "closer-publication-source-baseline/v1"
            ),
            "baseline": self.baseline,
            "native_pr": host.pr,
            "host_git": host.summary,
            "operation_sha256": digest(observed.operation),
            "admission_sha256": ctl.digest(observed.record),
            "publication_policy_sha256": digest(observed.policy),
            "stable_slot": stable_slot(_original_slot(observed)),
            **{k: False for k in _AUTHORITY},
        }
        if observed.policy["schema"] == ANCHOR_POLICY_SCHEMA:
            record["existing_objects"] = host.object_anchor.summary()
        _need(
            len(_bytes(record)) <= 1048576,
            "complete source baseline envelope exceeds custody bound",
        )
        name = "publication-source-baseline-" + digest(record) + ".json"
        observed.check()
        _need(observed.custody.create(name, record), "source baseline already owned")
        _need(
            _bytes(observed.custody.read(name)) == _bytes(record),
            "source baseline readback differs",
        )
        self._reference = {
            "schema": "closer-publication-source-baseline-reference/v1",
            "name": name,
            "sha256": digest(record),
        }
        self._record_bytes = _bytes(record)

    @property
    def reference(self):
        self.check()
        return _copy(self._reference)

    def check(self):
        _need(not self.closed, "source baseline lease closed")
        self.host.check()
        _need(
            _bytes(self.observed.custody.read(self._reference["name"])) == self._record_bytes,
            "source baseline custody changed",
        )

    def close(self):
        if not self.closed:
            self.closed = True
            try:
                if self.staging is not None:
                    self.staging.close()
            finally:
                self.host.close()


def _capture_candidate(host, baseline):
    host.check()
    _need(
        host.read("head").strip().decode("ascii") == baseline["head_oid"],
        "model changed corrective HEAD",
    )
    _need(
        _index_records(host.read("index"), host.observed.policy)
        == {r["path"]: (r["blob"]["mode"], r["blob"]["git_oid"]) for r in baseline["files"]},
        "model changed protected baseline index",
    )
    _need(
        _index_flags(host.read("index_flags"), host.observed.policy)
        == {r["path"] for r in baseline["files"]},
        "model changed special index flags",
    )
    rows = _physical_inventory(
        host.workspace,
        host.observed.policy,
        (
            host.read_lifetime
            if host.observed.policy["schema"] == ANCHOR_POLICY_SCHEMA
            else host.check
        ),
        full_check=host.check if host.observed.policy["schema"] == ANCHOR_POLICY_SCHEMA else None,
        original_paths={r["path"] for r in baseline["files"]},
        allow_new=True,
    )
    old = {r["path"]: r["blob"] for r in baseline["files"]}
    new = {r["path"]: r["blob"] for r in rows}
    paths = sorted(p for p in old.keys() | new.keys() if _bytes(old.get(p)) != _bytes(new.get(p)))
    if host.observed.policy["schema"] == ANCHOR_POLICY_SCHEMA:
        changed = []
        for path in paths:
            _path(path)
            after = None
            if path in new:
                item = _PhysicalFile(host.workspace / path, host.observed.policy["max_file_bytes"])
                try:
                    raw = item.raw()
                    item.check()
                    _need(
                        len(raw) == new[path]["size"]
                        and blob_oid(raw) == new[path]["git_oid"]
                        and hashlib.sha256(raw).hexdigest() == new[path]["sha256"],
                        "changed bytes differ",
                    )
                    after = {k: new[path][k] for k in ("mode", "git_oid", "sha256")} | {
                        "bytes_base64": base64.b64encode(raw).decode("ascii")
                    }
                finally:
                    item.close()
            changed.append(
                {
                    "path": path,
                    "before": (
                        None
                        if path not in old
                        else {k: old[path][k] for k in ("mode", "git_oid", "sha256")}
                    ),
                    "after": after,
                }
            )
    else:
        changed = [
            {
                "path": p,
                "before": (
                    None
                    if p not in old
                    else {k: v for k, v in old[p].items() if k != "bytes_base64"}
                ),
                "after": new.get(p),
            }
            for p in paths
        ]
    candidate = {
        "base_oid": baseline["base_oid"],
        "old_head_oid": baseline["head_oid"],
        "old_tree_oid": baseline["tree_oid"],
        "tree_oid": _tree_oid(rows),
        "files": changed,
        "unchanged_tree_sha256": digest([r for r in baseline["files"] if r["path"] not in paths]),
    }
    return validate_candidate_against_baseline(baseline, candidate, host.observed.policy)


def close_baseline(value):
    """Descriptor cleanup only; failure must hold delivery, never erase coding."""
    _need(type(value) is _BaselineLease, "actual source baseline cleanup required")
    value.close()


def _observe_helper_api_route(observed, route, gh_file, held, environment):
    """One fixed existing gh helper/API route under protected metadata continuity."""
    import shutil

    import closer_correction_native_effects as effects

    caller = inspect.currentframe().f_back
    _need(
        caller.f_code is _observe_git_context.__code__ and caller.f_globals is globals(),
        "actual helper-route observation source required",
    )

    def check():
        observed.check()
        _need(dict(os.environ) == environment, "native helper environment changed")
        for item in held:
            item.check()
        selected = shutil.which("gh")
        _need(
            type(selected) is str
            and Path(selected).resolve(strict=True) == gh_file.path
            and route["gh_path"] == str(gh_file.path),
            "native helper executable selection changed",
        )

    check()
    _require_fixed_gh_protocol(observed, gh_file)
    effects._native_writer(observed.controller, observed.operation)
    transport = getattr(observed.controller.store.request, "__self__", None)
    _need(
        type(transport) is effects.NativeHTTPTransport
        and transport.controller is observed.controller,
        "fixed native API transport unavailable",
    )
    native = transport.read_native_writer()
    check()
    _need(
        native.get("id") == observed.controller.config.writer_id
        and native.get("login") == observed.controller.config.writer_login
        and native.get("type") == observed.controller.config.writer_type,
        "native helper/API principal differs",
    )
    # This receipt is constructed only here, from the actual source frame,
    # fixed helper/config route, protected executable and source-native API read.
    return {
        "schema": "closer-publication-fixed-gh-route/v1",
        "host": "github.com",
        "gh_sha256": gh_file.sha256,
        "config_directory": route["config_directory"],
        "writer_id": native["id"],
        "writer_login": native["login"],
        "credential_slot": route["credential_slot"],
    }


class _PhysicalDirectory:
    def __init__(self, path, *, closed_entries=False, immutable_children=False):
        import stat

        self.path, self.closed_entries = Path(path), closed_entries
        self.immutable_children = immutable_children
        _need(
            self.path.is_absolute()
            and self.path.resolve(strict=True) == self.path
            and not any(p.is_symlink() for p in (self.path, *self.path.parents)),
            "publication directory redirected",
        )
        self.fd = None
        self.ancestors = _AncestorFDs(self.path)
        try:
            self.fd = os.open(self.path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            meta = os.fstat(self.fd)
            _need(
                stat.S_ISDIR(meta.st_mode)
                and meta.st_uid in {0, os.getuid()}
                and not meta.st_mode & 0o022,
                "publication directory ownership changed",
            )
            self.identity = (meta.st_dev, meta.st_ino, meta.st_uid, meta.st_mode)
            self.child_identity = (meta.st_mtime_ns, meta.st_ctime_ns)
            self.entries = tuple(sorted(os.listdir(self.fd))) if closed_entries else None
            self.check()
        except BaseException:
            self.close()
            raise

    def check(self):
        self.ancestors.check()
        _need(self.fd is not None, "publication directory closed")
        for meta in (os.fstat(self.fd), self.path.stat(follow_symlinks=False)):
            _need(
                (meta.st_dev, meta.st_ino, meta.st_uid, meta.st_mode) == self.identity,
                "publication directory named identity changed",
            )
        if self.immutable_children:
            meta = os.fstat(self.fd)
            _need(
                (meta.st_mtime_ns, meta.st_ctime_ns) == self.child_identity,
                "native helper PATH directory changed",
            )
        if self.closed_entries:
            _need(tuple(sorted(os.listdir(self.fd))) == self.entries, "publication hooks changed")
        return {"path": str(self.path), "identity": [str(v) for v in self.identity]}

    def close(self):
        errors = []
        if self.fd is not None:
            fd, self.fd = self.fd, None
            try:
                os.close(fd)
            except Exception:
                errors.append(True)
        try:
            self.ancestors.close()
        except Exception:
            errors.append(True)
        _need(not errors, "publication directory/ancestor cleanup unknown")


def _commit_tree(raw, head, policy):
    _need(
        type(raw) is bytes and len(raw) <= policy["max_ack_bytes"] and _oid(head),
        "bounded native commit bytes unavailable",
    )
    _need(
        hashlib.sha1(b"commit " + str(len(raw)).encode() + b"\0" + raw).hexdigest() == head,
        "native Git commit object hash differs",
    )
    tree_line = raw.split(b"\n", 1)[0]
    _need(
        re.fullmatch(rb"tree [a-f0-9]{40}", tree_line) is not None,
        "native commit tree header invalid",
    )
    return tree_line[5:].decode("ascii")


def _index_flags(raw, policy):
    policy = _inventory_policy(policy)
    _need(
        type(raw) is bytes
        and len(raw) <= policy["max_ack_bytes"]
        and (not raw or raw.endswith(b"\0")),
        "bounded Git index flags unavailable",
    )
    paths = set()
    for entry in raw[:-1].split(b"\0") if raw else []:
        _need(entry.startswith(b"H "), "assume-unchanged or skip-worktree index unsupported")
        try:
            path = _inventory_path(entry[2:].decode("utf-8"), policy)
        except UnicodeError:
            raise PublicationHeld("Git index path encoding unsupported") from None
        _need(path not in paths and len(paths) < policy["max_files"], "Git index flag bound")
        paths.add(path)
    return paths


def _physical_path_scope(relative, original_paths, policy, allow_new):
    if relative not in original_paths:
        _path(relative)
    _need(
        relative in original_paths
        or (
            allow_new is True
            and any(
                relative == prefix or relative.startswith(prefix + "/")
                for prefix in policy["allowed_path_prefixes"]
            )
        ),
        "untracked or out-of-scope file refused before reading bytes",
    )


class _AncestorFDs:
    """Keep every original ancestor inode alive; reject live symlink substitution."""

    def __init__(self, path):
        import stat

        self.held = []
        try:
            for ancestor in reversed(Path(path).parents):
                fd = os.open(ancestor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    meta = os.fstat(fd)
                    _need(stat.S_ISDIR(meta.st_mode), "physical directory ancestor required")
                    identity = (meta.st_dev, meta.st_ino, meta.st_uid, meta.st_mode)
                    self.held.append((ancestor, fd, identity))
                except BaseException:
                    os.close(fd)
                    raise
            self.check()
        except BaseException:
            self.close()
            raise

    def check(self):
        import stat

        _need(bool(self.held), "publication ancestor descriptors closed")
        for path, fd, identity in self.held:
            for meta in (os.fstat(fd), path.stat(follow_symlinks=False)):
                _need(
                    stat.S_ISDIR(meta.st_mode)
                    and (meta.st_dev, meta.st_ino, meta.st_uid, meta.st_mode) == identity,
                    "publication ancestor redirected or replaced",
                )

    def close(self):
        errors = []
        while self.held:
            _path, fd, _identity = self.held.pop()
            try:
                os.close(fd)
            except Exception:
                errors.append(True)
        _need(not errors, "publication ancestor cleanup unknown")


_SUPPORTED_GH_PROTOCOL = {
    "schema": "closer-source-gh-api-helper-protocol/v1",
    "release": "2.94.0",
    "path": "/opt/homebrew/Cellar/gh/2.94.0/bin/gh",
    "size": 37841954,
    "sha256": "2ef6c63bc52dc32bc42366215f91a51b009950791806209fd7f6fc7f5b668ba2",
    "host": "github.com",
    "token_slots": ["GH_TOKEN", "GITHUB_TOKEN"],
    "upstream": [
        "https://raw.githubusercontent.com/cli/cli/v2.94.0/pkg/cmd/auth/gitcredential/helper.go",
        "https://raw.githubusercontent.com/cli/cli/v2.94.0/internal/config/config.go",
        "https://raw.githubusercontent.com/cli/cli/v2.94.0/api/http_client.go",
        "https://raw.githubusercontent.com/cli/cli/v2.94.0/go.mod",
        "https://raw.githubusercontent.com/cli/go-gh/v2.13.0/pkg/auth/auth.go",
    ],
}


def _gh_protocol_data(path, size, sha256):
    """Exact source enrollment, not a build attestation or principal observation."""
    policy = _SUPPORTED_GH_PROTOCOL
    _need(
        path == policy["path"]
        and type(size) is int
        and size == policy["size"]
        and sha256 == policy["sha256"],
        "selected gh release has no enrolled API/helper protocol",
    )
    return _copy(policy)


def _require_fixed_gh_protocol(observed, gh_file):
    caller = inspect.currentframe().f_back
    _need(
        caller.f_code is _observe_helper_api_route.__code__
        and caller.f_globals is globals()
        and caller.f_locals.get("observed") is observed
        and caller.f_locals.get("gh_file") is gh_file
        and type(gh_file) is _PhysicalFile,
        "actual fixed helper/API protocol producer required",
    )
    observed.check()
    gh_file.check()
    _need(
        not any(name in os.environ for name in ("GH_HTTP_UNIX_SOCKET", "GH_PATH")),
        "native GitHub API route override unsupported",
    )
    protocol = _gh_protocol_data(str(gh_file.path), os.fstat(gh_file.fd).st_size, gh_file.sha256)
    _observe_no_api_socket(observed, gh_file)
    return protocol


def _observe_no_api_socket(observed, gh_file):
    """Fixed nonsecret local config observation; no token/config content read."""
    import dispatcher

    command = [str(gh_file.path), "config", "get", "http_unix_socket", "--host", "github.com"]
    environment = dict(os.environ)
    owned = []

    def starting(_):
        observed.check()
        gh_file.check()
        _need(dict(os.environ) == environment, "helper configuration environment changed")

    def started(actual):
        _transport_observation(command, actual)
        _need(not owned, "configuration observation repeated")
        owned.append(actual)

    def finished(actual):
        _need(
            len(owned) == 1 and actual is owned[0] and actual.get("cleanup_terminal") is True,
            "configuration observation cleanup unknown",
        )

    result = dispatcher.run_observed_offload(
        command,
        callback=started,
        observation={},
        cwd=str(gh_file.path.parent),
        timeout=_remaining_transport_seconds(observed.operation, observed.policy, time.time()),
        stdin=None,
        starting_callback=starting,
        finished_callback=finished,
        env=environment,
        output_limit=1024,
    )
    _need(len(owned) == 1, "configuration process observation absent")
    finished(owned[0])
    stdout, stderr = result.stdout, result.stderr
    _need(
        type(stdout) is str and type(stderr) is str, "configuration observation shape unsupported"
    )
    _need(
        (
            type(result.returncode) is int
            and result.returncode == 0
            and not stdout.strip()
            and not stderr.strip()
        )
        or (
            type(result.returncode) is int
            and result.returncode == 1
            and not stdout.strip()
            and stderr.strip() == 'could not find key "http_unix_socket"'
        ),
        "native GitHub API socket route must be absent",
    )


def _prepare_mutation_backend(controller, custody, operation, selection, host):
    import closer_coding_publication_io as publication_io

    return publication_io.prepare_backend(controller, custody, operation, selection, host)


def _native_publication_readback(controller, custody, operation, selection, host, commit):
    import closer_coding_publication_io as publication_io

    return publication_io.native_readback(controller, custody, operation, selection, host, commit)


def _backend_command_data(intent, kind, *, oid=None):
    """Closed command DATA; no path, flags, remote or hook overrides accepted."""
    intent = validate_intent(intent)
    git = intent["policy"]["git_executable"]["path"]
    fixed = {
        "read_tree": ["read-tree", intent["candidate"]["old_tree_oid"]],
        "hash_blob": ["hash-object", "-w", "--stdin"],
        "update_index": ["update-index", "-z", "--index-info"],
        "write_tree": ["write-tree"],
        "commit": ["commit", "--file=-"],
        "head": ["rev-parse", "--verify", "HEAD^{commit}"],
    }
    if kind == "commit_object":
        _need(_oid(oid), "fixed full commit object required")
        return [git, "cat-file", "commit", oid]
    _need(kind in fixed and oid is None, "publication command unsupported")
    return [git, *fixed[kind]]


def _backend_output_data(result, policy):
    _need(type(result.returncode) is int and result.returncode == 0, "Git command failed")
    raw = result.stdout
    if type(raw) is str:
        raw = raw.encode("utf-8")
    _need(type(raw) is bytes and len(raw) <= policy["max_ack_bytes"], "Git output bound")
    return raw


def _backend_oid_data(raw):
    _need(
        type(raw) is bytes and re.fullmatch(rb"[a-f0-9]{40}\n", raw),
        "exact Git object output required",
    )
    return raw[:-1].decode("ascii")


def _backend_commit_data(intent, oid, raw):
    _need(
        type(raw) is bytes and 0 < len(raw) <= intent["policy"]["max_ack_bytes"],
        "commit object bound",
    )
    _need(
        hashlib.sha1(b"commit " + str(len(raw)).encode() + b"\0" + raw).hexdigest() == oid,
        "commit object hash differs",
    )
    headers = raw.split(b"\n\n", 1)[0].splitlines()
    trees = [line[5:].decode("ascii") for line in headers if line.startswith(b"tree ")]
    parents = [line[7:].decode("ascii") for line in headers if line.startswith(b"parent ")]
    _need(
        trees == [intent["candidate"]["tree_oid"]] and parents == [intent["pr"]["head_oid"]],
        "commit tree or single parent differs",
    )
    return validate_commit_data(
        intent,
        {
            "commit_oid": oid,
            "tree_oid": trees[0],
            "parent_oids": parents,
            "candidate_sha256": digest(intent["candidate"]),
            "returncode": 0,
            "group_terminal": True,
        },
    )


def _backend_phase_transport(controller, custody, operation, selection, host, phase, command, raw):
    """Exact source phase locals for the still-held descriptor issuer.

    _selected_transport must bind this caller, phase, command, private staging,
    payload descriptor identity, source writer and reserved WAL before Popen.
    This function never accepts a runner or changes the operation deadline.
    """
    policy = validate_policy(selection["intent"]["policy"])
    payload = None
    try:
        if raw is not None:
            # Reusing immutable payload DATA is not repeating a Git phase.
            # Equal blobs or a commit-message-shaped blob may share bytes.
            _need(type(raw) is bytes and len(raw) <= policy["max_total_bytes"], "raw payload bound")
            value = {
                "schema": "closer-publication-stdin-data/v1",
                "size": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "bytes_base64": base64.b64encode(raw).decode("ascii"),
            }
            value_sha = digest(value)
            name = "publication-stdin-data-" + value_sha + ".json"
            custody.create(name, value)
            _need(_bytes(custody.read(name)) == _bytes(value), "backend payload custody differs")
            reference = {
                "schema": "closer-publication-stdin-data-reference/v1",
                "name": name,
                "sha256": value_sha,
            }
            payload = _PayloadData(custody, reference, policy)
        caller = inspect.currentframe().f_back
        body = caller.f_back if caller is not None else None
        _need(
            body is not None
            and body.f_code is _execute_prepared_backend.__code__
            and body.f_globals is globals()
            and caller.f_globals is globals()
            and getattr(body.f_locals.get("step"), "__code__", None) is caller.f_code
            and all(
                body.f_locals.get(k) is v
                for k, v in {
                    "controller": controller,
                    "custody": custody,
                    "operation": operation,
                    "selection": selection,
                    "host": host,
                }.items()
            ),
            "source-issued publication transport descriptor unavailable",
        )
        step_reference = _retain_step_intent_data(
            custody,
            selection["intent"],
            body.f_locals["prepared"]["baseline"],
            operation,
            caller.f_locals["sequence"],
            phase,
            command,
            _stdin_identity(payload),
        )
        result, observation = _observed_transport(controller, custody, operation, selection, host)
        _transport_observation(command, observation)
        _need(observation.get("cleanup_terminal") is True, "publication phase cleanup unknown")
        _retain_step_result_data(custody, step_reference, result, observation, policy)
        return _backend_output_data(result, policy)
    finally:
        if payload is not None:
            payload.close()


def _execute_prepared_backend(controller, custody, operation, selection, host, prepared):
    """Finite backend body, unreachable until genuine source producers exist.

    A local commit never grants native delivery. Every failure after reservation
    leaves an owned non-repeatable phase; only ACK plus exact native readback
    can produce the existing all-false contract receipt, not acceptance credit.
    """
    _exact(prepared, {"intent", "baseline"})
    intent = validate_intent(prepared["intent"])
    _need(_bytes(selection["intent"]) == _bytes(intent), "selected publication intent differs")
    staging = staging_payload_data(prepared["baseline"], intent["candidate"], intent["policy"])
    ledger = ContractLedger(custody)
    key = ledger.begin(intent)
    ledger.commit_started(key)  # Own the entire local mutation sequence first.
    stage = "commit_started"
    sequence = 0

    def step(phase, command, raw=None):
        nonlocal sequence
        sequence += 1
        return _backend_phase_transport(
            controller, custody, operation, selection, host, phase, command, raw
        )

    try:
        step("read_tree", _backend_command_data(intent, "read_tree"))
        for blob in staging["blobs"]:
            raw = base64.b64decode(blob["bytes_base64"], validate=True)
            observed = _backend_oid_data(
                step("hash_blob", _backend_command_data(intent, "hash_blob"), raw)
            )
            _need(observed == blob["git_oid"], "staged blob object differs")
        step(
            "update_index",
            _backend_command_data(intent, "update_index"),
            base64.b64decode(staging["index_bytes_base64"], validate=True),
        )
        tree = _backend_oid_data(step("write_tree", _backend_command_data(intent, "write_tree")))
        _need(tree == staging["expected_tree_oid"], "staged tree differs")
        step("commit", _backend_command_data(intent, "commit"), b"Apply authenticated correction\n")
        oid = _backend_oid_data(step("head", _backend_command_data(intent, "head")))
        raw = step("commit_object", _backend_command_data(intent, "commit_object", oid=oid))
        commit = _backend_commit_data(intent, oid, raw)
        ledger.commit_ack(key, commit)
        command = ledger.push_started(key)  # Marker survives prelaunch failure.
        stage = "push_started"
        ack = step("push", command["argv"])
        ledger.push_ack(key, ack, 0)
        stage = "push_ack"
        pr, ref, native_commit = _native_publication_readback(
            controller, custody, operation, selection, host, commit
        )
        return ledger.readback(key, pr, ref, native_commit)
    except BaseException:
        # If recording UNKNOWN itself fails, the create-only attempt/start
        # history still prevents the same mutation sequence from restarting.
        if ledger.read(key)["state"] in {"commit_started", "push_started", "push_ack"}:
            ledger.unknown(key, "readback_unavailable" if stage == "push_ack" else "source_changed")
        raise


def _loose_object_data(oid, framed, policy):
    """Raw verified Git object DATA; never an object-source authority assertion."""
    _need(
        _oid(oid) and type(framed) is bytes and 0 < len(framed) <= policy["max_file_bytes"],
        "loose object bound",
    )
    _need(hashlib.sha1(framed).hexdigest() == oid, "loose object hash differs")
    header, separator, raw = framed.partition(b"\0")
    match = re.fullmatch(rb"(commit|tree|blob) ([0-9]+)", header)
    _need(
        separator and match is not None and int(match[2]) == len(raw),
        "loose object framing differs",
    )
    kind = match[1].decode("ascii")
    links = []
    if kind == "commit":
        lines = raw.split(b"\n\n", 1)[0].splitlines()
        trees = [row[5:] for row in lines if row.startswith(b"tree ")]
        parents = [row[7:] for row in lines if row.startswith(b"parent ")]
        _need(
            len(trees) == 1 and all(re.fullmatch(rb"[a-f0-9]{40}", x) for x in trees + parents),
            "seed commit references invalid",
        )
        links = [x.decode("ascii") for x in trees + parents]
    elif kind == "tree":
        offset, names = 0, set()
        while offset < len(raw):
            end = raw.find(b"\0", offset)
            _need(end >= 0 and end + 21 <= len(raw), "seed tree framing invalid")
            mode, split, name = raw[offset:end].partition(b" ")
            _need(
                split
                and mode in {b"40000", b"100644", b"100755"}
                and b"/" not in name
                and name not in names,
                "seed tree mode/name unsupported",
            )
            try:
                _path(name.decode("utf-8"))
            except UnicodeError:
                raise PublicationHeld("seed tree path encoding unsupported") from None
            names.add(name)
            links.append(raw[end + 1 : end + 21].hex())
            offset = end + 21
    return kind, raw, links


def _validate_seed_data(objects, head, policy):
    policy = validate_policy(policy)
    _need(
        type(objects) is dict and objects and len(objects) <= policy["max_files"] and _oid(head),
        "complete bounded seed required",
    )
    _need(
        sum(len(raw) for raw in objects.values() if type(raw) is bytes)
        <= policy["max_total_bytes"],
        "seed total byte bound",
    )
    pending, seen = [head], set()
    while pending:
        oid = pending.pop()
        if oid in seen:
            continue
        _need(oid in objects, "packed or missing object source unsupported")
        kind, _, links = _loose_object_data(oid, objects[oid], policy)
        _need(kind != "commit" or oid == head, "seed contains historical commit")
        if oid == head:
            links = links[:1]  # H is the explicit shallow boundary; never copy ancestors.
        seen.add(oid)
        pending.extend(links)
    _need(seen == set(objects), "seed contains unbound objects")
    _need(
        _loose_object_data(head, objects[head], policy)[0] == "commit",
        "native seed head is not commit",
    )
    return {oid: objects[oid] for oid in sorted(objects)}


def _held_file_bytes(item):
    _need(
        type(item) is _PhysicalFile and not item.metadata_only,
        "held nonsecret source file required",
    )
    item.check()
    raw = os.pread(item.fd, item.bound + 1, 0)
    _need(
        len(raw) <= item.bound and hashlib.sha256(raw).hexdigest() == item.sha256,
        "held source bytes changed",
    )
    item.check()
    return raw


def _seed_snapshot_data(baseline, commit_raw, policy):
    """Reconstruct exact H/tree DATA from the already bounded full baseline.

    Object storage layout is irrelevant. The trusted existing Git commit read
    works for both packed and loose sources; no pack reader or new transport is
    introduced. Ancestors are deliberately excluded at the native H boundary.
    """
    policy = validate_policy(policy)
    baseline = validate_baseline_data(baseline, policy)
    _need(type(commit_raw) is bytes, "native head commit bytes required")
    _need(
        _commit_tree(commit_raw, baseline["head_oid"], policy) == baseline["tree_oid"],
        "native head commit differs from bounded baseline",
    )
    objects = {}
    for row in baseline["files"]:
        raw = _raw_blob(row["blob"], policy)
        objects[row["blob"]["git_oid"]] = b"blob " + str(len(raw)).encode() + b"\0" + raw
    _need(
        _tree_oid(baseline["files"], objects=objects) == baseline["tree_oid"],
        "reconstructed native tree differs",
    )
    objects[baseline["head_oid"]] = b"commit " + str(len(commit_raw)).encode() + b"\0" + commit_raw
    return _validate_seed_data(objects, baseline["head_oid"], policy)


class _ExistingObjects:
    """Read-only ownership of selected existing Git objects, never a publisher."""

    def __init__(
        self, path, baseline, commit_raw, policy, *, prior=None, read_context=None, deadline=None
    ):
        baseline = validate_baseline_data(baseline, policy)
        self.path, self.baseline, self.closed = Path(path), _copy(baseline), False
        self.read_context = read_context
        _need(
            deadline is None or (type(deadline) in (int, float) and 0 < deadline < float("inf")),
            "original object read deadline invalid",
        )
        self.deadline = deadline
        self.directory = _PhysicalDirectory(self.path)
        self.held, self.absent, self.identities, self.prefixes, self.loose_prefixes = (
            [],
            [],
            {},
            {},
            {},
        )
        try:
            trees = {}
            _need(
                _tree_oid(baseline["files"], objects=trees) == baseline["tree_oid"]
                and _commit_tree(commit_raw, baseline["head_oid"], policy) == baseline["tree_oid"],
                "existing object anchor differs",
            )
            self.expected = {
                r["blob"]["git_oid"]: ("blob", r["blob"]["size"]) for r in baseline["files"]
            }
            self.expected.update(
                {oid: ("tree", len(raw.split(b"\0", 1)[1])) for oid, raw in trees.items()}
            )
            self.expected[baseline["head_oid"]] = ("commit", len(commit_raw))
            self.payload_sha256 = {
                r["blob"]["git_oid"]: r["blob"]["sha256"] for r in baseline["files"]
            }
            self.payload_sha256.update(
                {
                    oid: hashlib.sha256(raw.split(b"\0", 1)[1]).hexdigest()
                    for oid, raw in trees.items()
                }
            )
            self.payload_sha256[baseline["head_oid"]] = hashlib.sha256(commit_raw).hexdigest()
            _need(
                len(self.expected_output()) <= policy["baseline_limits"]["max_inventory_bytes"],
                "closed native object metadata byte bound",
            )
            self.pack = _PhysicalDirectory(self.path / "pack")
            self.pack_names = self._pack_inventory()
            for name in self.pack_names:
                meta = os.stat(name, dir_fd=self.pack.fd, follow_symlinks=False)
                self.identities["pack/" + name] = self._regular_identity(meta)
            for oid in self.expected:
                prefix, name = oid[:2], oid[2:]
                fd = self._prefix(prefix)
                try:
                    try:
                        meta = (
                            os.stat(name, dir_fd=fd, follow_symlinks=False)
                            if fd is not None
                            else None
                        )
                    except FileNotFoundError:
                        meta = None
                    if meta is not None:
                        self.identities[prefix + "/" + name] = self._regular_identity(meta)
                        self.loose_prefixes[prefix] = self.prefixes[prefix]
                    else:
                        self.absent.append(self.path / prefix / name)
                finally:
                    if fd is not None:
                        os.close(fd)
            info_fd = self._prefix("info")
            try:
                for name in ("alternates", "http-alternates"):
                    if info_fd is not None:
                        try:
                            os.stat(name, dir_fd=info_fd, follow_symlinks=False)
                        except FileNotFoundError:
                            pass
                        else:
                            raise PublicationHeld("external object sources unsupported")
                    self.absent.append(self.path / "info" / name)
            finally:
                if info_fd is not None:
                    os.close(info_fd)
            current = dict(self.identities)
            metadata_names = {
                name for name in current if name.startswith("pack/") and not name.endswith(".pack")
            }
            if prior is not None:
                _exact(
                    prior,
                    {
                        "schema",
                        "path",
                        "head_oid",
                        "tree_oid",
                        "baseline_sha256",
                        "physical_sha256",
                        "physical_files",
                        "metadata_sha256",
                    },
                )
                _need(
                    prior["schema"] == "closer-existing-git-object-anchor/v2"
                    and prior["path"] == str(self.path)
                    and prior["head_oid"] == baseline["head_oid"]
                    and prior["tree_oid"] == baseline["tree_oid"]
                    and prior["baseline_sha256"] == digest(baseline),
                    "original object fingerprint binding differs",
                )
                original, metadata = prior["physical_files"], prior["metadata_sha256"]
                _need(
                    type(original) is dict
                    and set(original) == set(current)
                    and all(
                        type(row) is list
                        and len(row) == 8
                        and all(type(v) is str and re.fullmatch(r"-?[0-9]{1,24}", v) for v in row)
                        for row in original.values()
                    )
                    and all(
                        tuple(int(v) for v in original[name][:6]) == row[:6]
                        for name, row in current.items()
                    )
                    and type(metadata) is dict
                    and set(metadata) == metadata_names
                    and all(_sha(v) for v in metadata.values()),
                    "original object physical fingerprint differs",
                )
                self.identities = {
                    name: tuple(int(v) for v in row) for name, row in original.items()
                }
                self.metadata_sha256 = dict(metadata)
            else:
                self.metadata_sha256 = {}
                for name in metadata_names:
                    self.metadata_sha256[name] = self._file_content(
                        name, current[name], metadata_only=True
                    )
            # Preserve the ORIGINAL fingerprints through live and cold reentry.
            # Only this in-memory cache advances after verified Git freshening.
            self.checked_identities = dict(self.identities)
            self.physical_sha256 = digest(
                {
                    "odb": self.directory.identity,
                    "pack": self.pack.identity,
                    "pack_names": self.pack_names,
                    "files": self.identities,
                    "metadata_sha256": self.metadata_sha256,
                    "loose_prefixes": self.loose_prefixes,
                }
            )
            if prior is not None:
                _need(
                    self.physical_sha256 == prior["physical_sha256"],
                    "original object fingerprint digest differs",
                )
            self.check()
        except BaseException:
            self.close()
            raise

    def _file_content(self, relative, expected, *, metadata_only=False):
        """Bounded rare-change verification, never an unchanged body snapshot.

        Loose payloads bind the original SHA256 and native OID. Pack content
        binds its native Git container OID; index/sidecar bytes bind an original
        SHA256. Every stream is bounded by the ORIGINAL authenticated stat size
        and original operation deadline. History containers are not HEAD payloads;
        loose decompressed payloads still bind the validated HEAD size and hashes.
        Normal unchanged barriers perform metadata checks only.
        """
        import zlib

        if self.read_context is not None:
            self.read_context()

        def read_clock():
            _need(
                self.deadline is None or time.time() < self.deadline,
                "original object read deadline exhausted",
            )

        read_clock()
        prefix, name = relative.split("/", 1)
        directory = os.dup(self.pack.fd) if prefix == "pack" else self._prefix(prefix)
        fd = None
        try:
            _need(directory is not None, "original object directory unavailable")
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            _need(
                self._regular_identity(os.fstat(fd)) == expected,
                "bounded original object bytes unavailable",
            )
            count = 0

            def bounded_chunk():
                nonlocal count
                read_clock()
                raw = os.read(fd, min(65536, expected[5] - count + 1))
                count += len(raw)
                _need(count <= expected[5], "original object byte bound changed")
                return raw

            if metadata_only:
                sha = hashlib.sha256()
                while chunk := bounded_chunk():
                    sha.update(chunk)
                result = sha.hexdigest()
            elif prefix == "pack":
                _need(
                    name.endswith(".pack") and expected[5] >= 32, "original native pack malformed"
                )
                remaining = expected[5] - 20
                sha = hashlib.sha1(usedforsecurity=False)
                first = True
                while remaining:
                    read_clock()
                    chunk = os.read(fd, min(65536, remaining))
                    _need(bool(chunk), "original native pack truncated")
                    if first:
                        _need(
                            chunk.startswith(b"PACK")
                            and int.from_bytes(chunk[4:8], "big") in {2, 3},
                            "original native pack header differs",
                        )
                        first = False
                    sha.update(chunk)
                    remaining -= len(chunk)
                trailer = os.read(fd, 21)
                _need(
                    len(trailer) == 20 and trailer.hex() == name[5:45] == sha.hexdigest(),
                    "original native pack content differs",
                )
                result = sha.hexdigest()
            else:
                oid = prefix + name
                kind, size = self.expected[oid]
                decoder, header, payload_bytes = zlib.decompressobj(), bytearray(), 0
                git_sha, payload_sha = hashlib.sha1(usedforsecurity=False), hashlib.sha256()
                opened = False
                while chunk := bounded_chunk():
                    pending = chunk
                    while pending:
                        raw = decoder.decompress(pending, 65536)
                        pending = decoder.unconsumed_tail
                        git_sha.update(raw)
                        if not opened:
                            stop = raw.find(b"\0")
                            header.extend(raw if stop < 0 else raw[:stop])
                            _need(len(header) <= 128, "original loose object header bound")
                            if stop < 0:
                                continue
                            _need(
                                bytes(header) == (kind + " " + str(size)).encode(),
                                "original loose object header differs",
                            )
                            opened, raw = True, raw[stop + 1 :]
                        payload_bytes += len(raw)
                        _need(payload_bytes <= size, "original loose object payload bound")
                        payload_sha.update(raw)
                _need(
                    opened
                    and decoder.eof
                    and not decoder.unused_data
                    and not decoder.unconsumed_tail
                    and payload_bytes == size
                    and git_sha.hexdigest() == oid
                    and payload_sha.hexdigest() == self.payload_sha256[oid],
                    "original loose object content differs",
                )
                result = payload_sha.hexdigest()
            _need(
                self._regular_identity(os.fstat(fd)) == expected
                and self._regular_identity(os.stat(name, dir_fd=directory, follow_symlinks=False))
                == expected,
                "original object changed during content verification",
            )
            return result
        except zlib.error:
            raise PublicationHeld("original loose object content differs") from None
        finally:
            if fd is not None:
                os.close(fd)
            if directory is not None:
                os.close(directory)
            if self.read_context is not None:
                self.read_context()

    def _pack_inventory(self):
        names = []
        with os.scandir(self.pack.fd) as stream:
            for entry in stream:
                _need(
                    len(names) < 256
                    and re.fullmatch(
                        r"pack-[a-f0-9]{40}\.(?:pack|idx|rev|bitmap|mtimes|keep)", entry.name
                    ),
                    "unsupported existing pack inventory",
                )
                names.append(entry.name)
        return tuple(sorted(names))

    @staticmethod
    def _regular_identity(meta):
        import stat

        _need(
            stat.S_ISREG(meta.st_mode)
            and meta.st_uid in {0, os.getuid()}
            and meta.st_nlink == 1
            and not meta.st_mode & 0o022,
            "existing object file ownership/type differs",
        )
        return _PhysicalFile._identity(meta)

    def _prefix(self, prefix):
        import stat

        try:
            fd = os.open(
                prefix, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=self.directory.fd
            )
        except FileNotFoundError:
            _need(prefix not in self.prefixes, "existing object prefix missing")
            return None
        meta = os.fstat(fd)
        identity = (meta.st_dev, meta.st_ino, meta.st_uid, meta.st_mode)
        try:
            _need(
                stat.S_ISDIR(meta.st_mode)
                and meta.st_uid in {0, os.getuid()}
                and not meta.st_mode & 0o022
                and self.prefixes.setdefault(prefix, identity) == identity,
                "existing object prefix changed",
            )
            return fd
        except BaseException:
            os.close(fd)
            raise

    def check(self):
        _need(not self.closed, "existing object anchor closed")
        self.directory.check()
        self.pack.check()
        _need(
            self._pack_inventory() == self.pack_names,
            "selected existing pack inventory changed",
        )
        for relative, expected in self.identities.items():
            prefix, name = relative.split("/", 1)
            fd = os.dup(self.pack.fd) if prefix == "pack" else self._prefix(prefix)
            try:
                _need(fd is not None, "selected existing object directory changed")
                current = self._regular_identity(os.stat(name, dir_fd=fd, follow_symlinks=False))
                _need(current[:6] == expected[:6], "selected existing object file changed")
                if current != self.checked_identities[relative]:
                    content = self._file_content(
                        relative, current, metadata_only=relative in self.metadata_sha256
                    )
                    if relative in self.metadata_sha256:
                        _need(
                            content == self.metadata_sha256[relative],
                            "original object sidecar content differs",
                        )
                    self.checked_identities[relative] = current
            finally:
                if fd is not None:
                    os.close(fd)
        for path in self.absent:
            fd = self._prefix(path.parent.name)
            try:
                if fd is not None:
                    try:
                        os.stat(path.name, dir_fd=fd, follow_symlinks=False)
                    except FileNotFoundError:
                        pass
                    else:
                        raise PublicationHeld("existing object source topology changed")
            finally:
                if fd is not None:
                    os.close(fd)

    def expected_output(self):
        return b"".join(
            (oid + " " + kind + " " + str(size) + "\n").encode()
            for oid, (kind, size) in sorted(self.expected.items())
        )

    def summary(self):
        self.check()
        return {
            "schema": "closer-existing-git-object-anchor/v2",
            "path": str(self.path),
            "head_oid": self.baseline["head_oid"],
            "tree_oid": self.baseline["tree_oid"],
            "baseline_sha256": digest(self.baseline),
            "physical_sha256": self.physical_sha256,
            "physical_files": {
                name: [str(v) for v in row] for name, row in self.identities.items()
            },
            "metadata_sha256": dict(self.metadata_sha256),
        }

    def close(self):
        if not self.closed:
            self.closed = True
            for item in reversed(
                [self.directory, *([self.pack] if hasattr(self, "pack") else []), *self.held]
            ):
                item.close()


def _borrow_existing_objects(host, baseline, *, prior=None):
    _need(type(host) is _HostGitLease, "actual authenticated HostGit required")
    host.check()
    anchor = _ExistingObjects(
        Path(host.summary["topology"][1]) / "objects",
        baseline,
        host.read("commit"),
        host.observed.policy,
        prior=prior,
        read_context=host.read_lifetime,
        deadline=host.observed.operation["deadline"],
    )
    try:
        raw = _fixed_git_read(
            host.observed,
            host.workspace,
            "object_inventory",
            files=[*host.held, anchor],
            missing=host.missing,
            object_input=b"".join((oid + "\n").encode() for oid in sorted(anchor.expected)),
        )
        _need(raw == anchor.expected_output(), "native existing object closure missing or changed")
        host.check()
        return anchor
    except BaseException:
        anchor.close()
        raise


def _source_snapshot_seed(host, baseline, *, prior=None):
    """Read fixed native H under the live HostGit lease; no history or writes."""
    _need(type(host) is _HostGitLease, "source HostGit seed required")
    host.check()
    _need(baseline["head_oid"] == host.pr["head_oid"], "source seed head changed")
    if host.observed.policy["schema"] == ANCHOR_POLICY_SCHEMA:
        if host.object_anchor is None:
            host.object_anchor = _borrow_existing_objects(host, baseline, prior=prior)
        _need(host.object_anchor.baseline == baseline, "original existing object anchor differs")
        objects = host.object_anchor
    else:
        objects = _seed_snapshot_data(baseline, host.read("commit"), host.observed.policy)
    host.check()
    return objects


def _stage_write_data(root_fd, relative, raw, mode=0o600):
    """Create-only fd-relative DATA writer; no Git process or original repo write."""
    import stat

    parts = PurePosixPath(relative).parts
    _need(
        parts
        and str(PurePosixPath(relative)) == relative
        and not relative.startswith("/")
        and all(p not in {".", ".."} for p in parts),
        "private staging path differs",
    )
    current = os.dup(root_fd)
    try:
        for part in parts[:-1]:
            try:
                os.mkdir(part, 0o700, dir_fd=current)
            except FileExistsError:
                pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
            meta = os.fstat(child)
            if not (
                stat.S_ISDIR(meta.st_mode)
                and meta.st_uid == os.getuid()
                and stat.S_IMODE(meta.st_mode) == 0o700
            ):
                os.close(child)
                raise PublicationHeld("private staging parent differs")
            os.close(current)
            current = child
        fd = os.open(
            parts[-1], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode, dir_fd=current
        )
        try:
            view = memoryview(raw)
            while view:
                count = os.write(fd, view)
                _need(count > 0, "private staging write incomplete")
                view = view[count:]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.fsync(current)
    finally:
        os.close(current)


def _private_helper_suffix():
    import shlex

    helper = "!" + shlex.quote(_SUPPORTED_GH_PROTOCOL["path"]) + " auth git-credential"
    return ("\n[credential]\n\thelper =\n\thelper = " + helper + "\n").encode()


def _build_existing_object_staging(
    custody, key, baseline, candidate, pr, policy, anchor, config, hooks
):
    _need(
        type(custody) is ctl._Custody and _sha(key) and type(anchor) is _ExistingObjects,
        "existing source object staging required",
    )
    baseline = validate_baseline_data(baseline, policy)
    candidate = validate_candidate_against_baseline(baseline, candidate, policy)
    pr = validate_owned_pr(pr)
    _need(
        anchor.baseline == baseline
        and pr["head_oid"] == baseline["head_oid"]
        and pr["base_oid"] == baseline["base_oid"],
        "existing staging source anchor differs",
    )
    _path(pr["head_ref"])
    _need(
        all(not p.endswith(".lock") for p in pr["head_ref"].split("/")),
        "private ref path unsupported",
    )
    _need(
        type(config) is bytes
        and 0 < len(config) <= policy["max_file_bytes"]
        and b"\0" not in config,
        "bounded source config required",
    )
    _need(
        type(hooks) is dict
        and len(hooks) <= policy["max_files"]
        and all(
            re.fullmatch(r"[A-Za-z0-9_.-]+\.sample", k)
            and type(v) is bytes
            and len(v) <= policy["max_file_bytes"]
            for k, v in hooks.items()
        ),
        "sample-only hooks required",
    )
    original_config_sha = hashlib.sha256(config).hexdigest()
    config += b"\n[gc]\n\tauto = 0\n[maintenance]\n\tauto = false\n" + _private_helper_suffix()
    _need(
        len(config) + sum(map(len, hooks.values())) <= policy["max_total_bytes"],
        "private control metadata bound",
    )
    marker = {
        "schema": "closer-publication-staging-intent-data/v3",
        "slot_key": key,
        "baseline_sha256": digest(baseline),
        "candidate_sha256": digest(candidate),
        "head_oid": pr["head_oid"],
        "config_sha256": hashlib.sha256(config).hexdigest(),
        "original_config_sha256": original_config_sha,
        "hooks": {k: hashlib.sha256(v).hexdigest() for k, v in hooks.items()},
        "objects": {},
        "existing_objects": anchor.summary(),
        **{k: False for k in _AUTHORITY},
    }
    _need(len(_bytes(marker)) <= 1048576, "complete private staging envelope exceeds bound")
    _need(
        custody.create("publication-staging-intent-" + key + ".json", marker),
        "private staging already owned; never recreate",
    )
    custody.check()
    name = "publication-staging-" + key
    os.mkdir(name, 0o700, dir_fd=custody.fd)
    root_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=custody.fd)
    try:
        for relative in (
            "repo.git",
            "worktree",
            "native-tmp",
            "repo.git/objects",
            "repo.git/hooks",
        ):
            parent, _, leaf = relative.rpartition("/")
            fd = (
                os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
                if parent
                else os.dup(root_fd)
            )
            try:
                os.mkdir(leaf, 0o700, dir_fd=fd)
            finally:
                os.close(fd)
        _stage_write_data(root_fd, "repo.git/config", config)
        _stage_write_data(
            root_fd, "repo.git/HEAD", ("ref: refs/heads/" + pr["head_ref"] + "\n").encode()
        )
        _stage_write_data(
            root_fd, "repo.git/refs/heads/" + pr["head_ref"], (pr["head_oid"] + "\n").encode()
        )
        for name, raw in hooks.items():
            _stage_write_data(root_fd, "repo.git/hooks/" + name, raw)
        os.fsync(root_fd)
        anchor.check()
        result = _StagingData(custody.path / ("publication-staging-" + key), marker)
        result.object_anchor = anchor
        result.check()
        return result
    finally:
        os.close(root_fd)


def _build_staging_data(custody, key, baseline, candidate, pr, policy, objects, config, hooks):
    """Physical private staging DATA only; its reference grants no execution."""
    if policy.get("schema") == ANCHOR_POLICY_SCHEMA:
        return _build_existing_object_staging(
            custody, key, baseline, candidate, pr, policy, objects, config, hooks
        )
    import zlib

    _need(type(custody) is ctl._Custody and _sha(key), "actual private staging custody required")
    custody.check()
    policy, pr = validate_policy(policy), validate_owned_pr(pr)
    baseline = validate_baseline_data(baseline, policy)
    candidate = validate_candidate_against_baseline(baseline, candidate, policy)
    _need(
        pr["head_oid"] == baseline["head_oid"] and pr["base_oid"] == baseline["base_oid"],
        "staging native baseline differs",
    )
    _path(pr["head_ref"])
    _need(
        all(not part.endswith(".lock") for part in pr["head_ref"].split("/")),
        "staging ref path unsupported",
    )
    objects = _validate_seed_data(objects, pr["head_oid"], policy)
    _need(
        _commit_tree(
            _loose_object_data(pr["head_oid"], objects[pr["head_oid"]], policy)[1],
            pr["head_oid"],
            policy,
        )
        == baseline["tree_oid"],
        "seed head tree differs",
    )
    _need(
        type(config) is bytes
        and 0 < len(config) <= policy["max_file_bytes"]
        and b"\0" not in config,
        "bounded source config required",
    )
    original_config_sha256 = hashlib.sha256(config).hexdigest()
    config += _private_helper_suffix()
    _need(len(config) <= policy["max_file_bytes"], "derived private configuration byte bound")
    _need(
        type(hooks) is dict
        and len(hooks) <= policy["max_files"]
        and all(
            type(k) is str
            and re.fullmatch(r"[A-Za-z0-9_.-]+\.sample", k)
            and type(v) is bytes
            and len(v) <= policy["max_file_bytes"]
            for k, v in hooks.items()
        ),
        "closed sample-only hook DATA required",
    )
    _need(
        len(config) + sum(map(len, hooks.values())) + sum(map(len, objects.values()))
        <= policy["max_total_bytes"],
        "private metadata total bound",
    )
    marker = {
        "schema": "closer-publication-staging-intent-data/v2",
        "slot_key": key,
        "baseline_sha256": digest(baseline),
        "candidate_sha256": digest(candidate),
        "head_oid": pr["head_oid"],
        "config_sha256": hashlib.sha256(config).hexdigest(),
        "original_config_sha256": original_config_sha256,
        "hooks": {k: hashlib.sha256(v).hexdigest() for k, v in hooks.items()},
        "objects": {k: hashlib.sha256(v).hexdigest() for k, v in objects.items()},
        **{k: False for k in _AUTHORITY},
    }
    _need(
        custody.create("publication-staging-intent-" + key + ".json", marker),
        "private staging already owned; never recreate",
    )
    _need(
        _bytes(custody.read("publication-staging-intent-" + key + ".json")) == _bytes(marker),
        "private staging intent readback differs",
    )
    name = "publication-staging-" + key
    os.mkdir(name, 0o700, dir_fd=custody.fd)
    root_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=custody.fd)
    try:
        for directory in ("repo.git", "worktree", "repo.git/objects", "repo.git/hooks"):
            parent, _, leaf = directory.rpartition("/")
            parent_fd = (
                os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
                if parent
                else os.dup(root_fd)
            )
            try:
                os.mkdir(leaf, 0o700, dir_fd=parent_fd)
            finally:
                os.close(parent_fd)
        _stage_write_data(root_fd, "repo.git/config", config)
        _stage_write_data(root_fd, "repo.git/shallow", (pr["head_oid"] + "\n").encode())
        _stage_write_data(
            root_fd, "repo.git/HEAD", ("ref: refs/heads/" + pr["head_ref"] + "\n").encode()
        )
        _stage_write_data(
            root_fd, "repo.git/refs/heads/" + pr["head_ref"], (pr["head_oid"] + "\n").encode()
        )
        for oid, framed in objects.items():
            _stage_write_data(
                root_fd, "repo.git/objects/" + oid[:2] + "/" + oid[2:], zlib.compress(framed)
            )
        for hook, raw in hooks.items():
            _stage_write_data(root_fd, "repo.git/hooks/" + hook, raw)
        files = {row["path"]: row["blob"] for row in baseline["files"]}
        for row in candidate["files"]:
            if row["after"] is None:
                files.pop(row["path"])
            else:
                files[row["path"]] = row["after"]
        for path, blob in files.items():
            _stage_write_data(
                root_fd,
                "worktree/" + _path(path),
                _raw_blob(blob, policy),
                0o700 if blob["mode"] == 100755 else 0o600,
            )
        os.fsync(root_fd)
        result = _StagingData(custody.path / name, marker)
        _need(
            os.fstat(root_fd).st_ino == os.fstat(result.directory.fd).st_ino
            and os.fstat(root_fd).st_dev == os.fstat(result.directory.fd).st_dev,
            "private staging named directory changed",
        )
        custody.check()
        return result
    finally:
        os.close(root_fd)


class _StagingData:
    """Owned physical files and immutable controls; never a Git capability."""

    def __init__(self, path, marker):
        _need(
            type(marker) is dict
            and marker.get("schema")
            in {
                "closer-publication-staging-intent-data/v2",
                "closer-publication-staging-intent-data/v3",
            }
            and _sha(marker.get("original_config_sha256")),
            "private staging lacks source-selected helper policy",
        )
        self.path, self.marker, self.closed = Path(path), _copy(marker), False
        self.object_anchor = None
        self.directory = _PhysicalDirectory(self.path)
        self.controls = []
        self.directories = []
        try:
            for name in (
                "repo.git",
                "worktree",
                "repo.git/hooks",
                "repo.git/objects",
                "repo.git/refs/heads",
            ):
                self.directories.append(_PhysicalDirectory(self.path / name))
            expected = {
                "repo.git/config": marker["config_sha256"],
                **(
                    {
                        "repo.git/shallow": hashlib.sha256(
                            (marker["head_oid"] + "\n").encode()
                        ).hexdigest()
                    }
                    if marker["schema"] == "closer-publication-staging-intent-data/v2"
                    else {}
                ),
                **{"repo.git/hooks/" + k: v for k, v in marker["hooks"].items()},
            }
            for path, value in expected.items():
                item = _PhysicalFile(self.path / path, 1048576)
                self.controls.append(item)
                _need(item.sha256 == value, "private trusted control copy differs")
                if path == "repo.git/config":
                    _need(
                        item.raw().endswith(_private_helper_suffix()),
                        "private fixed helper suffix changed",
                    )
            self.check()
        except BaseException:
            self.close()
            raise

    def check(self):
        _need(not self.closed, "private staging closed")
        self.directory.check()
        if self.object_anchor is not None:
            self.object_anchor.check()
            _need(
                self.object_anchor.summary() == self.marker["existing_objects"],
                "private ODB binding differs",
            )
        for item in self.directories:
            item.check()
        for item in self.controls:
            item.check()
        _need(
            set(os.listdir(self.path / "repo.git/hooks")) == set(self.marker["hooks"]),
            "private hook closure changed",
        )
        return {
            "schema": "closer-publication-staging-data/v1",
            "path": str(self.path),
            "marker_sha256": digest(self.marker),
            "git_execution_authorized": False,
        }

    def close(self):
        if not self.closed:
            self.closed = True
            errors = []
            for item in reversed([self.directory, *self.directories, *self.controls]):
                try:
                    item.close()
                except Exception:
                    errors.append(True)
            _need(not errors, "private staging cleanup unknown")


def _issue_private_staging(baseline_ref, candidate, terminal_ref, handoff_ref=None):
    """Source callback issues local staging custody, never Git permission."""
    caller = inspect.currentframe().f_back
    _need(
        caller.f_code is retain_handoff.__code__
        and caller.f_globals is globals()
        and caller.f_locals.get("baseline_ref") is baseline_ref
        and caller.f_locals.get("candidate") is candidate
        and caller.f_locals.get("terminal_ref") is terminal_ref
        and caller.f_locals.get("handoff_reference") is handoff_ref,
        "actual known-result staging producer required",
    )
    _need(
        type(baseline_ref) is _BaselineLease and baseline_ref.staging is None,
        "actual unused baseline staging lifetime required",
    )
    baseline_ref.check()
    observed, host = baseline_ref.observed, baseline_ref.host
    _need(
        type(host) is _HostGitLease and "native_helper_principal" in host.summary,
        "source-proven HostGit staging context required",
    )
    terminal = observed.custody.read(terminal_ref["name"])
    import closer_coding_worker as worker

    worker.validate_terminal_record(terminal)
    result = caller.f_locals.get("result")
    handoff = _read_handoff_data(observed.custody, handoff_ref)
    _need(
        handoff["result_sha256"] == digest(result)
        and _bytes(handoff["candidate"]) == _bytes(candidate)
        and _bytes(handoff["baseline_reference"]) == _bytes(baseline_ref.reference)
        and _bytes(handoff["terminal_reference"]) == _bytes(terminal_ref),
        "issued handoff staging join differs",
    )
    _need(
        terminal_ref["sha256"] == ctl.digest(terminal)
        and terminal["result_sha256"] == digest(result)
        and terminal["operation_sha256"] == digest(observed.operation),
        "known coding terminal staging join differs",
    )
    common = Path(host.summary["topology"][1])
    config_file = next(
        (
            item
            for item in host.held
            if type(item) is _PhysicalFile and item.path == common / "config"
        ),
        None,
    )
    _need(config_file is not None, "held approved local configuration unavailable")
    config = _held_file_bytes(config_file)
    hooks = {}
    for path in sorted((common / "hooks").iterdir()):
        _need(path.name.endswith(".sample"), "active original hooks unsupported")
        item = _PhysicalFile(path, observed.policy["max_file_bytes"])
        try:
            hooks[path.name] = _held_file_bytes(item)
        finally:
            item.close()
    objects = _source_snapshot_seed(host, baseline_ref.baseline)
    observed.check()
    key = stable_slot(_original_slot(observed))
    data = _build_staging_data(
        observed.custody,
        key,
        baseline_ref.baseline,
        candidate,
        host.pr,
        observed.policy,
        objects,
        config,
        hooks,
    )
    try:
        lease = _SourceStagingLease(baseline_ref, data, candidate, terminal_ref, handoff_ref)
        baseline_ref.staging = lease
        return lease.reference
    except BaseException:
        data.close()
        raise


class _SourceStagingLease:
    """Actual callback/writer-bound local file custody, not a mutation capability."""

    def __init__(self, baseline, data, candidate, terminal_ref, handoff_ref=None):
        caller = inspect.currentframe().f_back
        _need(
            caller.f_code is _issue_private_staging.__code__
            and caller.f_globals is globals()
            and caller.f_locals.get("baseline_ref") is baseline
            and caller.f_locals.get("data") is data
            and caller.f_locals.get("candidate") is candidate
            and caller.f_locals.get("terminal_ref") is terminal_ref
            and caller.f_locals.get("handoff_ref") is handoff_ref,
            "actual source staging constructor required",
        )
        self.baseline, self.data, self.closed = baseline, data, False
        observed = baseline.observed
        record = {
            "schema": "closer-publication-source-staging/v2",
            "slot_key": stable_slot(_original_slot(observed)),
            "baseline_reference": baseline.reference,
            "handoff_reference": _copy(handoff_ref),
            "candidate_sha256": digest(candidate),
            "terminal_reference": _copy(terminal_ref),
            "operation_sha256": digest(observed.operation),
            "admission_sha256": ctl.digest(observed.record),
            "publication_policy_sha256": digest(observed.policy),
            "private_files": data.check(),
            **{k: False for k in _AUTHORITY},
        }
        name = "publication-source-staging-" + digest(record) + ".json"
        observed.check()
        _need(observed.custody.create(name, record), "source staging already retained")
        _need(
            _bytes(observed.custody.read(name)) == _bytes(record), "source staging readback differs"
        )
        self.record_bytes = _bytes(record)
        self._reference = {
            "schema": "closer-publication-source-staging-reference/v1",
            "name": name,
            "sha256": digest(record),
        }
        self.check()

    @property
    def reference(self):
        self.check()
        return _copy(self._reference)

    def check(self):
        _need(not self.closed, "source staging lifetime ended")
        self.baseline.check()
        self.data.check()
        observed = self.baseline.observed
        _need(
            _bytes(observed.custody.read(self._reference["name"])) == self.record_bytes,
            "immutable source staging changed",
        )

    def close(self):
        if not self.closed:
            self.closed = True
            self.data.close()


def _step_data_name(key, ordinal, suffix):
    _need(
        _sha(key)
        and type(ordinal) is int
        and 1 <= ordinal <= 9999
        and suffix in {"intent", "result"},
        "step custody selector invalid",
    )
    return f"publication-step-{key}-{ordinal:04}-{suffix}.json"


def _retain_step_intent_data(custody, intent, baseline, operation, ordinal, phase, command, stdin):
    """Create-only closed per-command DATA intent, before the effect port."""
    _need(type(custody) is ctl._Custody, "actual step custody required")
    intent = validate_intent(intent)
    staging = staging_payload_data(baseline, intent["candidate"], intent["policy"])
    phases = (
        ["read_tree"]
        + ["hash_blob"] * len(staging["blobs"])
        + ["update_index", "write_tree", "commit", "head", "commit_object", "push"]
    )
    _need(
        type(ordinal) is int and 1 <= ordinal <= len(phases) and phases[ordinal - 1] == phase,
        "step ordinal/phase differs",
    )
    key = intent["slot_key"]
    previous = digest(intent)
    previous_result = None
    if ordinal > 1:
        previous_result = custody.read(_step_data_name(key, ordinal - 1, "result"))
        previous_intent = custody.read(_step_data_name(key, ordinal - 1, "intent"))
        previous_stdout = base64.b64decode(previous_result["stdout_base64"], validate=True)
        _need(
            previous_result["schema"] == "closer-publication-step-result-data/v1"
            and type(previous_result["ordinal"]) is int
            and previous_result["ordinal"] == ordinal - 1
            and previous_result["slot_key"] == key
            and type(previous_result["returncode"]) is int
            and previous_result["returncode"] == 0
            and previous_result["cleanup_terminal"] is True
            and previous_result["intent_sha256"] == digest(previous_intent)
            and previous_intent["operation_sha256"] == digest(operation)
            and previous_intent["intent_sha256"] == digest(intent)
            and hashlib.sha256(previous_stdout).hexdigest() == previous_result["stdout_sha256"]
            and all(previous_result[k] is False for k in _AUTHORITY),
            "previous step has no known successful result",
        )
        previous = digest(previous_result)
    if phase == "push":
        ledger = ContractLedger(custody).read(key)
        _need(ledger["state"] == "push_started", "push step has no original attempt")
        commit = next(e["payload"] for e in ledger["events"] if e["kind"] == "commit_ack")
        expected = push_command_data(intent, commit)["argv"]
    elif phase == "commit_object":
        previous_output = base64.b64decode(previous_result["stdout_base64"], validate=True)
        expected = _backend_command_data(intent, phase, oid=_backend_oid_data(previous_output))
    else:
        expected = _backend_command_data(intent, phase)
    _need(_bytes(command) == _bytes(expected), "closed step command differs")
    raw = None
    if phase == "hash_blob":
        raw = base64.b64decode(staging["blobs"][ordinal - 2]["bytes_base64"], validate=True)
    elif phase == "update_index":
        raw = base64.b64decode(staging["index_bytes_base64"], validate=True)
    elif phase == "commit":
        raw = b"Apply authenticated correction\n"
    if raw is None:
        _need(stdin is None, "unexpected step stdin")
    else:
        _exact(stdin, {"fd", "dev", "ino", "size", "sha256"})
        _need(
            all(type(stdin[k]) is int and stdin[k] >= 0 for k in ("fd", "dev", "ino", "size"))
            and stdin["size"] == len(raw)
            and stdin["sha256"] == hashlib.sha256(raw).hexdigest(),
            "exact private step stdin differs",
        )
    value = {
        "schema": "closer-publication-step-intent-data/v1",
        "slot_key": key,
        "ordinal": ordinal,
        "phase": phase,
        "operation_sha256": digest(operation),
        "intent_sha256": digest(intent),
        "previous_result_sha256": previous,
        "command": _copy(command),
        "stdin": _copy(stdin),
        **{k: False for k in _AUTHORITY},
    }
    name = _step_data_name(key, ordinal, "intent")
    _need(custody.create(name, value), "publication command already owned; never repeat")
    _need(_bytes(custody.read(name)) == _bytes(value), "step intent readback differs")
    return {"name": name, "sha256": digest(value), "slot_key": key, "ordinal": ordinal}


def _retain_step_result_data(custody, reference, result, observation, policy):
    _need(type(custody) is ctl._Custody, "actual result custody required")
    _exact(reference, {"name", "sha256", "slot_key", "ordinal"})
    name = _step_data_name(reference["slot_key"], reference["ordinal"], "intent")
    _need(reference["name"] == name, "step intent reference differs")
    intent = custody.read(name)
    _need(digest(intent) == reference["sha256"], "immutable command intent changed")
    _transport_observation(intent["command"], observation)
    _need(
        observation.get("cleanup_terminal") is True and type(result.returncode) is int,
        "known step process result required",
    )
    raw = result.stdout.encode("utf-8") if type(result.stdout) is str else result.stdout
    _need(type(raw) is bytes and len(raw) <= policy["max_ack_bytes"], "step stdout bound")
    value = {
        "schema": "closer-publication-step-result-data/v1",
        "slot_key": reference["slot_key"],
        "ordinal": reference["ordinal"],
        "intent_sha256": reference["sha256"],
        "returncode": result.returncode,
        "cleanup_terminal": True,
        "process": {k: observation[k] for k in ("pid", "process_group", "os_started")},
        "stdout_base64": base64.b64encode(raw).decode("ascii"),
        "stdout_sha256": hashlib.sha256(raw).hexdigest(),
        **{k: False for k in _AUTHORITY},
    }
    result_name = _step_data_name(reference["slot_key"], reference["ordinal"], "result")
    _need(custody.create(result_name, value), "step result already retained")
    _need(_bytes(custody.read(result_name)) == _bytes(value), "step result readback differs")
    return value


def _read_handoff_data(custody, reference):
    _need(type(custody) is ctl._Custody, "actual handoff custody required")
    _exact(reference, {"schema", "name", "sha256"})
    _need(
        reference["schema"] == "closer-publication-source-handoff-reference/v1"
        and _sha(reference["sha256"])
        and reference["name"] == "publication-source-handoff-" + reference["sha256"] + ".json",
        "handoff reference differs",
    )
    record = custody.read(reference["name"])
    _exact(
        record,
        {
            "schema",
            "baseline_reference",
            "staging_link_name",
            "candidate",
            "terminal_reference",
            "result_sha256",
            "operation_sha256",
            "stable_slot",
            *_AUTHORITY,
        },
    )
    _need(
        record["schema"] == "closer-publication-source-handoff/v3"
        and digest(record) == reference["sha256"]
        and all(record[k] is False for k in _AUTHORITY)
        and _sha(record["stable_slot"])
        and record["staging_link_name"]
        == "publication-staging-link-" + record["stable_slot"] + ".json",
        "immutable known handoff differs",
    )
    return record


def _link_staging_data(custody, handoff_ref, staging_ref):
    """A separate create-only link never rewrites known handoff DATA."""
    handoff = _read_handoff_data(custody, handoff_ref)
    _exact(staging_ref, {"schema", "name", "sha256"})
    _need(
        staging_ref["schema"] == "closer-publication-source-staging-reference/v1"
        and _sha(staging_ref["sha256"])
        and staging_ref["name"] == "publication-source-staging-" + staging_ref["sha256"] + ".json",
        "staging link reference differs",
    )
    staging = custody.read(staging_ref["name"])
    _exact(
        staging,
        {
            "schema",
            "slot_key",
            "baseline_reference",
            "handoff_reference",
            "candidate_sha256",
            "terminal_reference",
            "operation_sha256",
            "admission_sha256",
            "publication_policy_sha256",
            "private_files",
            *_AUTHORITY,
        },
    )
    _need(
        staging["schema"] == "closer-publication-source-staging/v2"
        and digest(staging) == staging_ref["sha256"]
        and staging["slot_key"] == handoff["stable_slot"]
        and _bytes(staging["handoff_reference"]) == _bytes(handoff_ref)
        and _bytes(staging["baseline_reference"]) == _bytes(handoff["baseline_reference"])
        and _bytes(staging["terminal_reference"]) == _bytes(handoff["terminal_reference"])
        and staging["candidate_sha256"] == digest(handoff["candidate"])
        and staging["operation_sha256"] == handoff["operation_sha256"]
        and all(staging[k] is False for k in _AUTHORITY),
        "source staging link joins differ",
    )
    link = {
        "schema": "closer-publication-source-staging-link/v1",
        "handoff_reference": _copy(handoff_ref),
        "staging_reference": _copy(staging_ref),
        "stable_slot": handoff["stable_slot"],
        **{k: False for k in _AUTHORITY},
    }
    name = handoff["staging_link_name"]
    _need(custody.create(name, link), "staging link already owned; never replace")
    _need(_bytes(custody.read(name)) == _bytes(link), "staging link readback differs")
    return {"name": name, "sha256": digest(link)}


def _retain_staging_hold_data(custody, handoff_ref, code):
    handoff = _read_handoff_data(custody, handoff_ref)
    _need(
        code in {"staging_preparation_held", "staging_link_held"},
        "fixed staging hold code required",
    )
    value = {
        "schema": "closer-publication-staging-hold-data/v1",
        "handoff_reference": _copy(handoff_ref),
        "stable_slot": handoff["stable_slot"],
        "code": code,
        **{k: False for k in _AUTHORITY},
    }
    name = "publication-staging-hold-" + handoff_ref["sha256"] + ".json"
    _need(custody.create(name, value), "staging hold already retained")
    _need(_bytes(custody.read(name)) == _bytes(value), "staging hold readback differs")
    return value


def read_confirmed_result(controller, custody, operation, confirmed_result):
    """Fixed source knowledge reader; public mappings cannot issue publication."""
    import closer_coding_publication_io as publication_io

    return publication_io.read_confirmed_result(controller, custody, operation, confirmed_result)


def validate_handoff_result_data(result, operation, terminal_reference, handoff, baseline):
    """Closed retained DATA joins; this function grants no publication authority."""
    import closer_coding_worker as worker

    base = {"coding_terminal_reference", "actual", *worker._NO_AUTHORITY}
    allowed = base | {"publication_handoff_reference", "publication_handoff_held"}
    _need(type(result) is dict and base <= set(result) <= allowed, "coding result shape differs")
    _need(all(result[k] is False for k in worker._NO_AUTHORITY), "coding result authority differs")
    _need(
        _bytes(result["coding_terminal_reference"]) == _bytes(terminal_reference),
        "publication coding terminal differs",
    )
    if "publication_handoff_held" in result:
        _need(result["publication_handoff_held"] is True, "publication hold must be explicit")
    reference = result.get("publication_handoff_reference")
    if reference is None:
        _need(handoff is None and baseline is None, "orphan publication handoff cannot be adopted")
        return None
    _exact(reference, {"schema", "name", "sha256"})
    _need(
        reference["schema"] == "closer-publication-source-handoff-reference/v1"
        and reference["sha256"] == digest(handoff)
        and reference["name"] == "publication-source-handoff-" + reference["sha256"] + ".json"
        and handoff["schema"] == "closer-publication-source-handoff/v3"
        and handoff["terminal_reference"] == terminal_reference
        and handoff["operation_sha256"] == digest(operation)
        and handoff["result_sha256"] == digest(result["actual"])
        and handoff["baseline_reference"]["sha256"] == digest(baseline)
        and baseline["schema"]
        in {"closer-publication-source-baseline/v1", "closer-publication-source-baseline/v2"}
        and baseline["operation_sha256"] == handoff["operation_sha256"]
        and baseline["stable_slot"] == handoff["stable_slot"]
        and all(row[k] is False for row in (handoff, baseline) for k in _AUTHORITY),
        "returned publication handoff does not bind actual coding result",
    )
    return _copy(reference)


def validate_native_coding_result(cfg, operation, result):
    """Actual native terminal reader validates optional original publication DATA."""
    import closer_coding_continuation as continuation
    import closer_coding_session_v2 as session
    import closer_native_observer as native

    frame = inspect.currentframe().f_back
    _need(
        frame.f_code is native._coding_queue_terminal.__code__
        and frame.f_globals is native.__dict__
        and frame.f_locals.get("cfg") is cfg
        and frame.f_locals.get("original") is operation
        and frame.f_locals.get("result") is result,
        "actual native coding result reader required",
    )
    config = ctl.ControllerConfig(**cfg["policy"]["correction_controller_installation"]["config"])
    custody = ctl._Custody(config)
    try:
        selector = cfg["native_admission"]["coding_continuation"]
        protected = continuation._read_protected(custody, selector["reference"])
        original = custody.read("admission-" + protected["admission_sha256"] + ".json")
        _need(ctl.digest(original) == protected["admission_sha256"], "original admission changed")
        policy = validate_consumer_mode(
            protected["consumer_id"],
            config.consumers[protected["consumer_id"]],
            session.snapshot_from_admission(original),
        )
        _need(policy is not None, "publication was absent from original admission")
        ref = result.get("publication_handoff_reference")
        handoff = _read_handoff_data(custody, ref) if ref is not None else None
        baseline = custody.read(handoff["baseline_reference"]["name"]) if handoff else None
        retained = validate_handoff_result_data(
            result,
            operation,
            result["coding_terminal_reference"],
            handoff,
            baseline,
        )
        if baseline is not None:
            _need(
                baseline["admission_sha256"] == ctl.digest(original)
                and baseline["publication_policy_sha256"] == digest(policy),
                "publication baseline original policy differs",
            )
        return retained
    finally:
        custody.close()


def read_for_existing_root(backend, target, snapshot, operation, pipeline_context):
    import closer_coding_publication_io as publication_io

    return publication_io.read_for_existing_root(
        backend, target, snapshot, operation, pipeline_context
    )


def discovery_hint(backend, target, pipeline_context):
    import closer_coding_publication_io as publication_io

    return publication_io.discovery_hint(backend, target, pipeline_context)
