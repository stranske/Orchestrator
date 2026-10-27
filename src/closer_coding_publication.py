"""Necessary publication DATA contracts and create-only custody; no effect issuer.

The fixed observed transport remains unreachable without genuine source issuance.
ACK/readback inputs are data, not attestation. The future source tick/callback,
HostGit context and fixed native proof adapters remain deliberately unavailable.
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


def validate_policy(value):
    """Closed, explicit necessary policy shape only. Never an authority producer."""
    _exact(
        value,
        {
            "schema",
            "mode",
            "consumer_id",
            "git_executable",
            "allowed_path_prefixes",
            "trusted_context_sha256",
            *_BOUNDS,
        },
    )
    _need(
        value["schema"] == POLICY_SCHEMA and value["mode"] == "owned_existing_pr",
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
        and _sha(value["trusted_context_sha256"]),
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
    raise PublicationHeld("actual publication baseline source producer unavailable")


def retain_handoff(root, controller, custody, operation, baseline_ref, result, terminal_ref):
    caller = inspect.currentframe().f_back
    _live_worker_custody(root, controller, custody, operation, caller)
    _need(
        caller.f_locals.get("publication_baseline_ref") is baseline_ref
        and caller.f_locals.get("result") is result
        and caller.f_locals.get("terminal_ref") is terminal_ref,
        "actual baseline/result/terminal identity required",
    )
    raise PublicationHeld("actual publication handoff source producer unavailable")


def run_if_selected(campaign, document, manifest, manifest_path, pending):
    raise PublicationHeld("actual finite publication I/O admission unavailable")


def _publication_selection(controller, custody, operation, intent_ref):
    # No common callback/private-installation issuer has been implemented.
    # Neither a DATA intent nor an allocated IndexController is that issuer.
    raise PublicationHeld("actual publication callback selection unavailable")


def _host_git_context(selection):
    raise PublicationHeld("protected publication HostGit issuance unavailable")


def _selected_transport(controller, custody, operation, selection, host):
    # Command, environment, stdin, WAL ownership and readback availability must
    # come from the protected source issuer, never from the public call's JSON.
    raise PublicationHeld("source-issued publication transport descriptor unavailable")


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
    _observed_transport(controller, custody, operation, selection, host)
    raise PublicationHeld("native publication acknowledgement/readback issuer unavailable")


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


def _tree_oid(files):
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
        return hashlib.sha1(b"tree " + str(len(raw)).encode() + b"\0" + raw).hexdigest()

    return encode(root)


def validate_baseline_data(value, policy):
    """Complete bounded DATA inventory, never proof of workspace/source custody."""
    import unicodedata

    def inventory_key(path):
        # Comparison only: keep original Git bytes and refuse filesystem aliases.
        return unicodedata.normalize("NFC", unicodedata.normalize("NFC", path).casefold())

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
            None if before is None else {k: v for k, v in before.items() if k != "bytes_base64"}
        )
        _need(_bytes(expected) == _bytes(row["before"]), "candidate before bytes differ")
        _need(_bytes(before) != _bytes(row["after"]), "empty candidate change")
        if row["after"] is None:
            updated.pop(path, None)
        else:
            updated[path] = row["after"]
    unchanged = [{"path": p, "blob": old[p]} for p in sorted(old) if p not in changed]
    _need(digest(unchanged) == candidate["unchanged_tree_sha256"], "unchanged inventory differs")
    after = {
        **baseline,
        "tree_oid": candidate["tree_oid"],
        "files": [{"path": p, "blob": updated[p]} for p in sorted(updated)],
    }
    validate_baseline_data(after, policy)
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


def validate_consumer_mode(consumer_id, registration, original=_ORIGINAL_UNSUPPLIED):
    """Validate prospective policy DATA; a snapshot does not issue Git custody."""
    _need(type(registration) is dict, "exact source consumer registration required")
    current = None
    if "publication_v1" in registration:
        current = validate_policy(registration["publication_v1"])
        _need(current["consumer_id"] == consumer_id, "publication consumer differs")
    if original is not _ORIGINAL_UNSUPPLIED:
        _need(type(original) is dict, "original source snapshot required")
        admitted = original.get("publication_v1")
        _need(
            ("publication_v1" in original) == ("publication_v1" in registration),
            "publication mode cannot be added or removed after admission",
        )
        if current is not None:
            admitted = validate_policy(admitted)
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
    root.check()
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
