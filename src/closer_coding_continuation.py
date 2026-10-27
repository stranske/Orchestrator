"""Prospective coding continuation selection; no serialized execution permit.

The static capability is fixed before the original correction. Dynamic
references are issued by the original source callback, never manifest edits.
"""

from __future__ import annotations

import contextlib
import copy
import hashlib
import inspect
import json
import os
import re
import stat
import threading
from pathlib import Path

CAPABILITY_SCHEMA = "closer-coding-worker-capability/v1"
POLICY_FIELD = "correction_coding_worker"


class ContinuationHeld(ValueError):
    pass


def validate_capability(value):
    if not (
        type(value) is dict
        and set(value) == {"schema", "consumer_id"}
        and value["schema"] == CAPABILITY_SCHEMA
        and type(value["schema"]) is str
        and type(value["consumer_id"]) is str
        and value["consumer_id"]
        and value["consumer_id"] == value["consumer_id"].strip()
    ):
        raise ContinuationHeld("exact static source coding capability required")
    return copy.deepcopy(value)


def selected_capability(controller, custody, operation):
    """Selection from already validated current source policy, not admission."""
    controller._require_writer(operation, custody)
    installation = controller._source_installation()
    value = installation._binding.policy.get(POLICY_FIELD)
    if value is None:
        return None
    capability = validate_capability(value)
    if capability["consumer_id"] not in controller.config.consumers:
        raise ContinuationHeld("selected source coding consumer unavailable")
    return capability


# These records are selectors and knowledge only. No validator below issues an
# admission, renews a callback, or accepts a terminal status as execution proof.
RECORD_SCHEMA = "closer-coding-continuation/v1"
REFERENCE_SCHEMA = "closer-coding-continuation-reference/v1"
REGISTRY_SCHEMA = "closer-coding-continuation-registry-row/v1"
WAL_SCHEMA = "closer-coding-continuation-registration/v1"
REGISTRY_FIELD = "coding_continuations"
MAX_RECORDS = 64
STATES = frozenset(
    {"awaiting_original_terminal", "admitted", "launch_intent", "submitted", "active", "terminal"}
)
_SHA = re.compile(r"[a-f0-9]{64}\Z")
_CURRENT = threading.local()
_SEAL = object()
_LOADED_SOURCE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
_AUTHORITY = (
    "act_authorized",
    "provider_authorized",
    "retry_authorized",
    "acceptance_authorized",
    "source_closure_authorized",
    "global_stop_valid",
    "target_complete",
    "domain_completed",
)
_REFERENCE_FIELDS = {
    "schema",
    "name",
    "sha256",
    "stable_key",
    "effect_key",
    "request_sha256",
    "consumer_id",
}
_RECORD_FIELDS = {
    "schema",
    "issuer_source_sha256",
    "stable_intent",
    "stable_key",
    "effect_key",
    "request_sha256",
    "consumer_id",
    "owner_login",
    "controller_config_sha256",
    "manifest_path",
    "manifest_sha256",
    "source_closure_sha256",
    "enrollment_terminal_ref",
    "admitted_selector",
    "admission_sha256",
    "original_snapshot",
    "request_json",
    "operation_json",
    "operation_sha256",
    "native_b_start",
    "checkpoint",
    *_AUTHORITY,
}


def _need(value, reason):
    if not value:
        raise ContinuationHeld(reason)


def _canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()


def _digest(value):
    return hashlib.sha256(_canonical(value)).hexdigest()


def _sha(value):
    return type(value) is str and _SHA.fullmatch(value) is not None


def _text(value):
    return type(value) is str and 0 < len(value) <= 4096 and value == value.strip()


def stable_intent(*, repository_id, epoch, effect_key, request_sha256, consumer_id):
    """Exact existing _ProviderGuard key; no attempt/PID/thread/frontier input.

    Returning these data does not reserve the intent. The existing final guard
    remains its sole create-only consumer at the actual before-Popen boundary.
    """
    _need(
        type(repository_id) is int
        and repository_id > 0
        and _text(epoch)
        and _sha(effect_key)
        and _sha(request_sha256)
        and _text(consumer_id),
        "stable original coding identity unavailable",
    )
    return {
        "schema": "local-correction-final-provider-key/v1",
        "repository_id": repository_id,
        "epoch": epoch,
        "key": effect_key,
        "request_sha256": request_sha256,
        "consumer_id": consumer_id,
    }


def _validate_intent(value):
    _need(
        type(value) is dict
        and set(value)
        == {"schema", "repository_id", "epoch", "key", "request_sha256", "consumer_id"},
        "exact original stable provider identity required",
    )
    expected = stable_intent(
        repository_id=value["repository_id"],
        epoch=value["epoch"],
        effect_key=value["key"],
        request_sha256=value["request_sha256"],
        consumer_id=value["consumer_id"],
    )
    _need(value == expected, "stable provider identity differs")
    return expected


def validate_reference(value):
    """Necessary detached shape only, never protected issuance or authority."""
    _need(
        type(value) is dict and set(value) == _REFERENCE_FIELDS,
        "exact coding continuation reference required",
    )
    _need(
        value["schema"] == REFERENCE_SCHEMA
        and all(_sha(value[k]) for k in ("sha256", "stable_key", "effect_key", "request_sha256"))
        and _text(value["consumer_id"])
        and value["name"] == "coding-continuation-" + value["stable_key"] + ".json",
        "coding continuation reference identity differs",
    )
    return copy.deepcopy(value)


def _reference(record):
    return {
        "schema": REFERENCE_SCHEMA,
        "name": "coding-continuation-" + record["stable_key"] + ".json",
        "sha256": _digest(record),
        **{k: record[k] for k in ("stable_key", "effect_key", "request_sha256", "consumer_id")},
    }


def validate_record(value):
    """Immutable record integrity only. Readers additionally require real scope/custody."""
    import closer_coding_session_v2 as session
    import closer_correction_enrollment_terminal as enrollment
    import closer_correction_handoff as handoff
    import closer_queue as queue

    _need(
        type(value) is dict and set(value) == _RECORD_FIELDS and value["schema"] == RECORD_SCHEMA,
        "exact coding continuation record required",
    )
    intent = _validate_intent(value["stable_intent"])
    _need(
        value["stable_key"] == _digest(intent)
        and value["effect_key"] == intent["key"]
        and value["request_sha256"] == intent["request_sha256"]
        and value["consumer_id"] == intent["consumer_id"]
        and _text(value["owner_login"])
        and all(
            _sha(value[k])
            for k in (
                "issuer_source_sha256",
                "controller_config_sha256",
                "manifest_sha256",
                "source_closure_sha256",
                "admission_sha256",
                "operation_sha256",
            )
        )
        and all(value[k] is False for k in _AUTHORITY),
        "coding record identity/authority differs",
    )
    marker = enrollment._reference(value["enrollment_terminal_ref"])
    _need(
        marker["effect_key"] == value["effect_key"]
        and marker["request_sha256"] == value["request_sha256"]
        and marker["operation_sha256"] == value["operation_sha256"],
        "original enrollment reference differs",
    )
    snapshot = session.validate_admission_snapshot(value["original_snapshot"])
    _need(snapshot["consumer_id"] == value["consumer_id"], "original coding consumer differs")
    for name in ("request_json", "operation_json"):
        _need(
            type(value[name]) is str and len(value[name].encode()) <= 1048576,
            "bounded original immutable JSON required",
        )
        try:
            parsed = json.loads(value[name])
        except (ValueError, TypeError):
            raise ContinuationHeld("original immutable JSON malformed") from None
        encoded = queue._canonical_bytes(parsed) if name == "operation_json" else _canonical(parsed)
        _need(
            type(parsed) is dict and encoded.decode() == value[name],
            "canonical original immutable JSON required",
        )
        if name == "operation_json":
            _need(
                hashlib.sha256(encoded).hexdigest() == value["operation_sha256"],
                "original operation digest differs",
            )
        else:
            _need(
                parsed.get("request_sha256") == value["request_sha256"]
                and type(parsed.get("incumbent")) is dict
                and handoff._incumbent(parsed["incumbent"])
                and parsed.get("incumbent", {}).get("owner") == value["owner_login"],
                "original existing incumbent ownership differs",
            )
    _need(
        _text(value["manifest_path"])
        and Path(value["manifest_path"]).is_absolute()
        and all(
            type(value[k]) is dict and value[k]
            for k in ("admitted_selector", "native_b_start", "checkpoint")
        ),
        "original source selectors missing",
    )
    _need(len(_canonical(value)) <= 1048576, "coding record bound exceeded")
    return copy.deepcopy(value)


def validate_wal(value):
    """Necessary immutable registration intent shape, never a scheduling permit."""
    import closer_correction_enrollment_terminal as enrollment

    _need(
        type(value) is dict
        and set(value)
        == {
            "schema",
            "reference",
            "enrollment_terminal_ref",
            "controller_config_sha256",
            "operation_sha256",
        }
        and value["schema"] == WAL_SCHEMA,
        "exact coding registration WAL required",
    )
    ref = validate_reference(value["reference"])
    marker = enrollment._reference(value["enrollment_terminal_ref"])
    _need(
        all(_sha(value[k]) for k in ("controller_config_sha256", "operation_sha256"))
        and marker["effect_key"] == ref["effect_key"]
        and marker["request_sha256"] == ref["request_sha256"]
        and marker["operation_sha256"] == value["operation_sha256"],
        "coding registration WAL identities differ",
    )
    return copy.deepcopy(value)


def validate_registry(value):
    """Bounded separate registry data; a status never proves terminality."""
    _need(type(value) is dict and len(value) <= MAX_RECORDS, "coding registry malformed or full")
    for key, row in value.items():
        _need(
            _sha(key)
            and type(row) is dict
            and set(row) == {"schema", "reference", "state", "revision", "worker"}
            and row["schema"] == REGISTRY_SCHEMA
            and type(row["state"]) is str
            and row["state"] in STATES
            and type(row["revision"]) is int
            and 0 < row["revision"] <= 2**53 - 1,
            "coding registry row malformed",
        )
        ref = validate_reference(row["reference"])
        _need(ref["stable_key"] == key, "coding registry key differs")
        if row["state"] == "awaiting_original_terminal":
            _need(
                row["revision"] == 1 and row["worker"] is None,
                "awaiting original terminal cannot carry a worker",
            )
        else:
            _need(
                type(row["worker"]) is dict and row["worker"],
                "accepted coding worker selector absent",
            )
    return copy.deepcopy(value)


def validate_transition(before, after):
    """Necessary state graph only. This function writes nothing and grants nothing."""
    allowed = {
        "awaiting_original_terminal": {"admitted"},
        "admitted": {"launch_intent"},
        "launch_intent": {"submitted", "active", "terminal"},
        "submitted": {"active", "terminal"},
        "active": {"terminal"},
        "terminal": set(),
    }
    for row in (before, after):
        ref = validate_reference(row["reference"])
        validate_registry({ref["stable_key"]: row})
    _need(
        _canonical(before["reference"]) == _canonical(after["reference"])
        and after["state"] in allowed[before["state"]]
        and (
            before["worker"] is None or _canonical(before["worker"]) == _canonical(after["worker"])
        )
        and after["revision"] == before["revision"] + 1,
        "coding continuation cannot reset/retry or change original identity",
    )
    return copy.deepcopy(after)


def _closure(installation):
    import closer_coding_session_runtime as runtime
    import closer_coding_session_v2 as session
    import closer_correction_controller as ctl
    import closer_correction_enrollment_terminal as enrollment
    import closer_correction_source_record as source

    enrollment._closure(installation)
    for module in (__import__(__name__), ctl, source, session, runtime):
        path = Path(module.__file__)
        _need(
            path.is_absolute()
            and path.resolve(strict=True) == path
            and not any(p.is_symlink() for p in (path, *path.parents))
            and installation.source_closure.get(path.name)
            == {"path": str(path), "sha256": module._LOADED_SOURCE_SHA256}
            and hashlib.sha256(path.read_bytes()).hexdigest() == module._LOADED_SOURCE_SHA256,
            "current coding continuation source closure changed",
        )


def _read_protected(custody, reference):
    ref = validate_reference(reference)
    record = validate_record(custody.read(ref["name"]))
    wal = validate_wal(custody.read("coding-continuation-register-" + ref["stable_key"] + ".json"))
    _need(
        wal["reference"] == ref
        and wal["enrollment_terminal_ref"] == record["enrollment_terminal_ref"]
        and wal["controller_config_sha256"] == record["controller_config_sha256"]
        and wal["operation_sha256"] == record["operation_sha256"],
        "coding registration WAL differs",
    )
    _need(
        _reference(record) == ref and record["issuer_source_sha256"] == _LOADED_SOURCE_SHA256,
        "protected coding continuation changed",
    )
    return record


class _CampaignLock:
    """Non-authorizing physical continuity checks for this held lock lifetime."""

    def __init__(self, campaign, fd, identity, parents):
        self.campaign = campaign
        self.fd = fd
        self.identity = identity
        self.parents = parents
        self.closed = False

    @staticmethod
    def _parents(path):
        # Directory ctime changes on legitimate sibling/registry writes. Bind
        # directory identity/ownership/mode instead, without following links.
        result = []
        for parent in path.parents:
            meta = parent.stat(follow_symlinks=False)
            _need(stat.S_ISDIR(meta.st_mode), "campaign lock parent redirected")
            result.append((str(parent), meta.st_dev, meta.st_ino, meta.st_uid, meta.st_mode))
        return tuple(result)

    def check(self):
        _need(not self.closed, "campaign lock lifetime ended")
        path = self.campaign.lock_path
        try:
            _need(
                path.is_absolute()
                and path.resolve(strict=True) == path
                and self._parents(path) == self.parents,
                "campaign lock parent redirected",
            )
            held = os.fstat(self.fd)
            named = path.stat(follow_symlinks=False)
            for meta in (held, named):
                _need(
                    stat.S_ISREG(meta.st_mode)
                    and meta.st_uid == os.getuid()
                    and meta.st_nlink == 1
                    and not meta.st_mode & 0o077,
                    "campaign lock ownership/type/links changed",
                )
            _need(
                (held.st_dev, held.st_ino, held.st_ctime_ns)
                == self.identity
                == (named.st_dev, named.st_ino, named.st_ctime_ns),
                "campaign named lock identity changed",
            )
            _need(self._parents(path) == self.parents, "campaign lock parent redirected")
        except OSError:
            raise ContinuationHeld("campaign named lock unavailable or changed") from None
        return self

    def readback(self, expected):
        """Necessary data readback only; never validates a source issuer."""
        self.check()
        current = self.campaign._read_unlocked()
        self.check()
        # Python equality aliases bool/int values; immutable JSON does not.
        _need(
            _canonical(current) == _canonical(expected), "coding campaign registry readback differs"
        )
        return current


@contextlib.contextmanager
def _campaign_lock(campaign):
    """Nonblocking; bind the held fd to the same nofollow named inode throughout."""
    import fcntl

    path = campaign.lock_path
    _need(
        path.is_absolute()
        and path.parent.resolve(strict=True) == path.parent
        and not any(p.is_symlink() for p in (path, *path.parents)),
        "campaign lock redirected",
    )
    parents = _CampaignLock._parents(path)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    lease = None
    try:
        meta = os.fstat(fd)
        _need(
            stat.S_ISREG(meta.st_mode)
            and meta.st_uid == os.getuid()
            and meta.st_nlink == 1
            and not meta.st_mode & 0o077,
            "campaign lock ownership changed",
        )
        identity = (meta.st_dev, meta.st_ino, meta.st_ctime_ns)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ContinuationHeld("coding campaign remains owned") from None
        lease = _CampaignLock(campaign, fd, identity, parents)
        lease.check()  # Detect replacement between open/flock and entry.
        try:
            yield lease
        except Exception:
            lease.check()  # Refuse changed locks even on ordinary error exits.
            raise
        except BaseException:
            # Preserve cancellation/fatal exits; a continuity error cannot turn
            # them into a coding-only exception that a future hook contains.
            try:
                lease.check()
            except Exception:
                pass
            raise
        else:
            lease.check()
    finally:
        if lease is not None:
            lease.closed = True
        os.close(fd)


def retain_after_confirmation_locked(
    controller, custody, operation, selected, enrollment_terminal_ref
):
    """Future original callback hook; currently not wired by public v1 callers.

    selected is the genuine source SourceSelection, not the static capability.
    Marker -> WAL -> immutable record -> awaiting registry precedes scheduling.
    """
    import closer_campaign as campaign_source
    import closer_coding_session_runtime as runtime
    import closer_coding_session_v2 as session
    import closer_correction_controller as ctl
    import closer_correction_enrollment_terminal as enrollment
    import closer_correction_source_record as source

    frame = inspect.currentframe().f_back
    try:
        _need(
            frame.f_code is source.execute_candidate.__code__
            and frame.f_globals is source.__dict__
            and all(
                frame.f_locals.get(k) is v
                for k, v in {
                    "controller": controller,
                    "custody": custody,
                    "operation": operation,
                    "selection": selected,
                    "terminal": enrollment_terminal_ref,
                }.items()
            ),
            "actual original confirmed source callback required",
        )
    finally:
        del frame
    _need(
        type(controller) is ctl.IndexController and type(custody) is ctl._Custody,
        "actual original writer custody required",
    )
    controller._require_writer(operation, custody)
    installation = controller._source_installation()
    _closure(installation)
    capability = selected_capability(controller, custody, operation)
    _need(capability is not None, "coding capability absent")
    facts = source.validate_for_native_effect(selected, controller, operation)
    request = facts["request"]
    before, entry, admission = enrollment._entry(controller, custody, operation, request)
    marker_ref = enrollment._reference(enrollment_terminal_ref)
    marker = custody.read(marker_ref["name"])
    origin = source.operation_lifecycle_origin(controller, operation)
    _need(
        enrollment._ref(marker) == marker_ref
        and marker["schema"] == enrollment.SCHEMA
        and marker["issuer_source_sha256"] == enrollment._LOADED_SOURCE_SHA256
        and marker["controller_config"] == controller.config.source_binding()
        and marker["source_record_ref"] == facts["source_record_ref"]
        and marker["admitted_selector"] == entry["admitted_selector"]
        and marker["admission_sha256"] == ctl.digest(admission)
        and marker["entry_sha256"] == ctl.digest(entry)
        and marker["checkpoint"] == controller.anchor(custody)
        and marker["operation_json"] == origin["operation_json"]
        and marker["operation_sha256"] == origin["operation_sha256"]
        and marker["native_b_start"] == origin["native_b_start"]
        and all(marker[k] is False for k in enrollment._FALSE),
        "protected original enrollment marker/source joins differ",
    )
    consumer_id = capability["consumer_id"]
    consumer = controller.config.consumers[consumer_id]
    _need(
        type(consumer) is dict
        and consumer.get("runner") == "existing-local-controller"
        and consumer.get("agent") == "codex",
        "existing source coding consumer required",
    )
    snapshot = session.snapshot_from_admission(admission)
    policy = session.validate_policy(consumer.get(session.POLICY_FIELD))
    current = session.validate_budget(consumer.get(runtime.CURRENT_FIELD))
    session.minimum_budget(snapshot["budget"], current, policy["budget"])
    import execution_profiles

    profile = execution_profiles.get_profile(session.PROFILE)
    _need(
        snapshot["consumer_id"] == policy["consumer_id"] == consumer_id
        and snapshot["profile_id"] == policy["profile_id"] == session.PROFILE
        and snapshot["profile_sha256"]
        == policy["profile_sha256"]
        == consumer.get("profile_sha256")
        == ctl.source_digest(profile)
        and consumer.get("profile_id") == session.PROFILE
        and request["incumbent"].get("kind") != "no_incumbent"
        and request["incumbent"].get("owner") == consumer.get("owner_login"),
        "coding requires exact original existing incumbent owner and profile",
    )
    # This existing reader validates exactly one current assignee against the
    # immutable source/incumbent context. NoInc never supplies a coding owner.
    controller.fresh_issue_context(request, admission, consumer)
    intent = stable_intent(
        repository_id=controller.config.repository_id,
        epoch=controller.config.epoch,
        effect_key=ctl.effect_key(request),
        request_sha256=request["request_sha256"],
        consumer_id=consumer_id,
    )
    key = _digest(intent)
    try:
        custody.read("provider-intent-" + key + ".json")
    except FileNotFoundError:
        pass
    else:
        raise ContinuationHeld("original provider intent consumed; never repeat")
    record = {
        "schema": RECORD_SCHEMA,
        "issuer_source_sha256": _LOADED_SOURCE_SHA256,
        "stable_intent": intent,
        "stable_key": key,
        "effect_key": intent["key"],
        "request_sha256": intent["request_sha256"],
        "consumer_id": consumer_id,
        "owner_login": consumer["owner_login"],
        "controller_config_sha256": ctl.digest(controller.config.source_binding()),
        "manifest_path": installation.manifest_path,
        "manifest_sha256": installation.manifest_sha256,
        "source_closure_sha256": ctl.digest(installation.source_closure),
        "enrollment_terminal_ref": marker_ref,
        "admitted_selector": entry["admitted_selector"],
        "admission_sha256": ctl.digest(admission),
        "original_snapshot": snapshot,
        "request_json": _canonical(request).decode(),
        "operation_json": origin["operation_json"],
        "operation_sha256": origin["operation_sha256"],
        "native_b_start": origin["native_b_start"],
        "checkpoint": marker["checkpoint"],
        **dict.fromkeys(_AUTHORITY, False),
    }
    validate_record(record)
    ref = _reference(record)
    # WAL/record may survive a crash before registry commit, but never become
    # an issuance capability after this actual original callback expires.
    wal = {
        "schema": WAL_SCHEMA,
        "reference": ref,
        "enrollment_terminal_ref": marker_ref,
        "controller_config_sha256": record["controller_config_sha256"],
        "operation_sha256": record["operation_sha256"],
    }
    validate_wal(wal)
    campaign = campaign_source.Campaign(controller.config.state_path)
    with _campaign_lock(campaign) as locked:
        locked.check()
        controller._require_writer(operation, custody)
        document = campaign._read_unlocked()
        locked.check()
        _need(
            type(document) is dict
            and document["worker_config"]["policy"] == installation._binding.policy,
            "original campaign policy changed",
        )
        registry = validate_registry(document.get(REGISTRY_FIELD, {}))
        row = {
            "schema": REGISTRY_SCHEMA,
            "reference": ref,
            "state": "awaiting_original_terminal",
            "revision": 1,
            "worker": None,
        }
        if key in registry:
            _need(registry[key] == row, "existing coding continuation differs or already advanced")
        else:
            _need(len(registry) < MAX_RECORDS, "coding continuation registry full")
        locked.check()
        controller._require_writer(operation, custody)
        custody.create("coding-continuation-register-" + key + ".json", wal)
        custody.create(ref["name"], record)
        _need(custody.read(ref["name"]) == record, "coding continuation custody differs")
        locked.check()
        expected_document = document
        if key not in registry:
            registry[key] = row
            updated = copy.deepcopy(document)
            updated[REGISTRY_FIELD] = registry
            updated["revision"] += 1
            locked.check()
            campaign._write_unlocked(updated)
            locked.check()
            expected_document = updated
        locked.readback(expected_document)
        controller._require_writer(operation, custody)
        locked.check()
    return copy.deepcopy(ref)


class _CodingScope:
    """Source-held read scope, never an admission or caller-constructed actor."""

    def __init__(self, *args, **kwargs):
        raise TypeError("coding scope is source-issued only")

    @property
    def document(self):
        return json.loads(self._document_utf8)

    def _check_context(self):
        # Only these source functions may inspect the temporary post-CAS
        # lifetime. This is not a public check(ignore_document=True) mode.
        caller = inspect.currentframe().f_back
        try:
            _need(
                caller.f_globals is globals()
                and caller.f_code
                in {
                    _CodingScope.check.__code__,
                    registry_transition.__wrapped__.__code__,
                    _transition_snapshot.__code__,
                },
                "internal coding scope lifetime reader required",
            )
        finally:
            del caller
        import closer_campaign as campaign
        import closer_native_observer as native
        import closer_runtime_binding as runtime

        _need(
            getattr(self, "seal", None) is _SEAL
            and getattr(_CURRENT, "scope", None) is self
            and self.thread == threading.get_ident()
            and self.pid == os.getpid()
            and not self.closed,
            "actual same-thread coding scope required",
        )
        for frame, code, namespace in (
            (self.tick, campaign.tick.__code__, campaign.__dict__),
            (self.entry, native._native_entry.__code__, native.__dict__),
            (self.supervisor, native.native_entry.__code__, native.__dict__),
        ):
            cursor = inspect.currentframe()
            try:
                while cursor is not None and cursor is not frame:
                    cursor = cursor.f_back
                _need(
                    cursor is frame and frame.f_code is code and frame.f_globals is namespace,
                    "original coding source lifetime ended",
                )
            finally:
                del cursor
        _need(
            self.tick.f_locals.get("campaign") is self.campaign
            and self.tick.f_locals.get("manifest") is self.manifest
            and self.entry.f_locals.get("nonce") == self.nonce
            and native.entry_active(self.manifest)
            and native._CURRENT_ENTRY.get(str(self.native_root)) == self.nonce,
            "coding source tick/campaign changed",
        )
        for frame, handle, fd, identity, name in self.native_locks:
            _need(
                frame.f_locals.get("lock") is handle
                and not handle.closed
                and handle.fileno() == fd,
                "coding native lock ended",
            )
            meta = os.fstat(fd)
            named = (self.native_root / name).stat(follow_symlinks=False)
            _need(
                stat.S_ISREG(meta.st_mode)
                and meta.st_uid == os.getuid()
                and meta.st_nlink == 1
                and (meta.st_dev, meta.st_ino, meta.st_ctime_ns)
                == identity
                == (named.st_dev, named.st_ino, named.st_ctime_ns),
                "coding native lock changed",
            )
        self.campaign_lock.check()
        meta = os.fstat(self.lock)
        named = self.campaign.lock_path.stat(follow_symlinks=False)
        _need(
            stat.S_ISREG(meta.st_mode)
            and meta.st_uid == os.getuid()
            and meta.st_nlink == 1
            and not meta.st_mode & 0o077
            and (meta.st_dev, meta.st_ino, meta.st_ctime_ns)
            == self.lock_identity
            == (named.st_dev, named.st_ino, named.st_ctime_ns),
            "coding campaign lock changed",
        )
        _need(
            hashlib.sha256(Path(__file__).read_bytes()).hexdigest() == _LOADED_SOURCE_SHA256,
            "coding continuation source changed",
        )
        origin = runtime._factory_manifest_origin(self.binding)
        _need(
            origin is not None
            and origin.path == self.manifest_path
            and origin.sha256 == self.manifest_sha256,
            "coding tick factory binding changed",
        )
        return self

    def check(self):
        self._check_context()
        _need(self._transition is None, "coding registry transition not yet adopted")
        self.campaign_lock.readback(self.document)
        return self


def _find_frame(code, namespace):
    frame = inspect.currentframe().f_back
    while frame is not None:
        if frame.f_code is code and frame.f_globals is namespace:
            return frame
        frame = frame.f_back
    raise ContinuationHeld("actual coding native-entry/tick frame required")


@contextlib.contextmanager
def _coding_scope(campaign, document, manifest, manifest_path):
    import closer_campaign as campaign_source
    import closer_native_observer as native
    import closer_runtime_binding as runtime

    _need(
        type(campaign) is campaign_source.Campaign and getattr(_CURRENT, "scope", None) is None,
        "coding scope unavailable or nested",
    )
    tick = _find_frame(campaign_source.tick.__code__, campaign_source.__dict__)
    entry = _find_frame(native._native_entry.__code__, native.__dict__)
    supervisor = _find_frame(native.native_entry.__code__, native.__dict__)
    _need(
        tick.f_locals.get("campaign") is campaign
        and tick.f_locals.get("manifest") is manifest
        and entry.f_locals.get("request") is None
        and entry.f_locals.get("observation", {}).get("entry", {}).get("pid") == os.getpid()
        and str(manifest_path) == str(tick.f_locals.get("manifest_path"))
        and document["worker_config"]["policy"] == manifest["policy"]
        and native.entry_active(manifest),
        "fresh source-owned coding native tick required",
    )
    root = native.contract(manifest)[3]
    nonce = native._CURRENT_ENTRY[str(root)]
    _need(entry.f_locals.get("nonce") == nonce, "coding native entry differs")
    with _campaign_lock(campaign) as campaign_lock:
        campaign_lock.readback(document)
        lock = campaign_lock.fd
        _need(
            not campaign_source.qualify(manifest, Path(tick.f_locals["root"])),
            "current coding source/Core qualification unavailable",
        )
        binding = runtime.binding_from_manifest(str(manifest_path))
        origin = runtime._factory_manifest_origin(binding) if binding is not None else None
        _need(
            origin is not None and origin.path == str(manifest_path),
            "source-owned coding tick factory binding unavailable",
        )
        scope = object.__new__(_CodingScope)
        scope.binding, scope.manifest_sha256 = binding, origin.sha256
        scope._document_utf8, scope._transition = _canonical(document), None
        scope.seal, scope.closed = _SEAL, False
        scope.pid, scope.thread = os.getpid(), threading.get_ident()
        scope.tick, scope.entry, scope.supervisor = tick, entry, supervisor
        scope.campaign, scope.manifest = campaign, manifest
        scope.manifest_path, scope.native_root, scope.nonce = str(manifest_path), root, nonce
        scope.campaign_lock = campaign_lock
        scope.lock = lock
        meta = os.fstat(lock)
        scope.lock_identity = (meta.st_dev, meta.st_ino, meta.st_ctime_ns)
        scope.native_locks = []
        for frame, name in ((entry, "native-entry.lock"), (supervisor, "native-supervisor.lock")):
            handle = frame.f_locals["lock"]
            meta = os.fstat(handle.fileno())
            scope.native_locks.append(
                (frame, handle, handle.fileno(), (meta.st_dev, meta.st_ino, meta.st_ctime_ns), name)
            )
        _CURRENT.scope = scope
        try:
            scope.check()
            yield scope
            scope.check()
        finally:
            scope.closed = True
            del _CURRENT.scope


def read_selected(scope, controller, reference):
    """Protected local record reader. No original terminal/native admission credit."""
    import closer_correction_controller as ctl

    _need(type(scope) is _CodingScope, "actual coding source scope required")
    scope.check()
    _need(
        type(controller) is ctl.IndexController
        and controller.config.state_path == str(scope.campaign.path),
        "coding controller belongs to another campaign",
    )
    installation = controller._source_installation()
    _closure(installation)
    with contextlib.closing(ctl._Custody(controller.config)) as custody:
        record = _read_protected(custody, reference)
        _need(
            record["controller_config_sha256"] == ctl.digest(controller.config.source_binding())
            and record["manifest_path"] == scope.manifest_path
            and record["manifest_sha256"] == installation.manifest_sha256,
            "original coding source binding changed",
        )
        registry = validate_registry(scope.document.get(REGISTRY_FIELD, {}))
        row = registry.get(record["stable_key"])
        _need(
            row is not None and row["reference"] == reference, "coding registry/reference differs"
        )
        scope.check()
        return copy.deepcopy(record)


def read_accepted_purpose(scope, controller, reference):
    """Unwired signed purpose remains held; record/status is not a substitute.

    The separate native admission/handoff/B/C producer and worker readers are
    absent at df90. Common integration must implement/review them before this
    interface may return a positive. It deliberately accepts no supplied reader.
    """
    read_selected(scope, controller, reference)
    raise ContinuationHeld("signed coding purpose/native terminal integration unavailable")


def run_if_selected(campaign, document, manifest, manifest_path, pending):
    """Future tick selector only; never launches or recycles ordinary workers."""
    capability = (manifest.get("policy") or {}).get(POLICY_FIELD)
    if capability is None:
        return None
    validate_capability(capability)
    registry = validate_registry(document.get(REGISTRY_FIELD, {}))
    if not registry:
        return None
    _need(pending.get("kind") == "reconcile", "coding selection cannot replace ordinary work")
    with _coding_scope(campaign, document, manifest, manifest_path):
        # Original C/result authentication and signed coding purpose are reserved
        # common-writer integration, not a registry-JSON readiness predicate.
        raise ContinuationHeld("original terminal/signed coding scheduling integration unavailable")


def read_terminal(scope, controller, reference):
    """No registry status or pipeline receipt aliases a protected coding terminal."""
    read_accepted_purpose(scope, controller, reference)
    raise ContinuationHeld("protected coding terminal/native C reader unavailable")


def _registry_delta(before, after, reference):
    """Necessary detached CAS shape only; native proof is separately mandatory."""
    ref = validate_reference(reference)
    _need(
        type(before) is dict
        and type(after) is dict
        and type(before.get("revision")) is int
        and 0 <= before["revision"] < 2**53 - 1
        and type(after.get("revision")) is int
        and after["revision"] == before["revision"] + 1,
        "coding campaign revision must advance exactly once",
    )
    old = validate_registry(before.get(REGISTRY_FIELD))
    new = validate_registry(after.get(REGISTRY_FIELD))
    key = ref["stable_key"]
    _need(
        set(old) == set(new) and key in old and old[key]["reference"] == ref,
        "coding transition must select one existing stable reference",
    )
    _need(
        all(_canonical(old[k]) == _canonical(new[k]) for k in old if k != key)
        and _canonical({k: v for k, v in before.items() if k not in {REGISTRY_FIELD, "revision"}})
        == _canonical({k: v for k, v in after.items() if k not in {REGISTRY_FIELD, "revision"}}),
        "coding transition changed unrelated campaign data",
    )
    validate_transition(old[key], new[key])
    return _canonical(after)


def _source_function(module, name):
    function = getattr(module, name, None)
    _need(
        callable(function)
        and getattr(function, "__globals__", None) is module.__dict__
        and getattr(function, "__code__", None) is not None,
        "fixed coding source dependency unavailable",
    )
    return function


def _knowledge_pins(value):
    """Check source pins from the actual qualified-policy factory, not a permit."""
    import closer_coding_session_runtime as runtime
    import closer_coding_session_v2 as session
    import closer_correction_controller as ctl
    import closer_correction_enrollment_terminal as enrollment
    import closer_correction_source_record as source
    import closer_source_installation as installed

    _need(
        type(value) is dict
        and set(value)
        == {
            "config",
            "manifest_path",
            "manifest_sha256",
            "source_closure",
            "module_dir",
            "policy_projection",
        },
        "exact tick knowledge value required",
    )
    for module in (__import__(__name__), ctl, enrollment, source, session, runtime, installed):
        path = Path(module.__file__)
        loaded = getattr(module, "_LOADED_SOURCE_SHA256", getattr(module, "_SOURCE_SHA256", None))
        _need(
            _sha(loaded)
            and path.is_absolute()
            and path.resolve(strict=True) == path
            and not any(p.is_symlink() for p in (path, *path.parents))
            and value["source_closure"].get(path.name) == {"path": str(path), "sha256": loaded}
            and hashlib.sha256(path.read_bytes()).hexdigest() == loaded,
            "qualified coding knowledge source closure changed",
        )


def read_tick_selected(scope, reference):
    """Tick-only protected knowledge. Never mint an execution SourceInstallation."""
    _need(type(scope) is _CodingScope, "actual coding tick source scope required")
    scope.check()
    ref = validate_reference(reference)
    import closer_correction_controller as ctl
    import closer_source_installation as installed

    reader = _source_function(installed, "coding_tick_knowledge_for")
    value = reader(scope.binding)  # Fixed actual source factory; no supplied config/reader.
    scope.check()
    _knowledge_pins(value)
    config = ctl.ControllerConfig(**value["config"])
    _need(
        config.state_path == str(scope.campaign.path)
        and value["manifest_path"] == scope.manifest_path
        and value["manifest_sha256"] == scope.manifest_sha256,
        "coding tick knowledge belongs to another campaign/manifest",
    )
    with contextlib.closing(ctl._Custody(config)) as custody:
        record = _read_protected(custody, ref)
        _need(
            record["controller_config_sha256"] == ctl.digest(config.source_binding())
            and record["manifest_path"] == value["manifest_path"]
            and record["manifest_sha256"] == value["manifest_sha256"]
            and record["source_closure_sha256"] == ctl.digest(value["source_closure"]),
            "protected tick knowledge binding differs",
        )
        registry = validate_registry(scope.document.get(REGISTRY_FIELD))
        _need(
            ref["stable_key"] in registry and registry[ref["stable_key"]]["reference"] == ref,
            "protected tick registry/reference differs",
        )
        scope.check()
        return copy.deepcopy(record)


class _RegistryTransition:
    """Private ephemeral metadata; authority requires actual frames and native proofs."""

    def __init__(self, frame, reference, old_utf8):
        self.frame, self.reference_utf8, self.old_utf8 = frame, _canonical(reference), old_utf8
        self.new_utf8 = None
        self.thread, self.pid = threading.get_ident(), os.getpid()
        self.validating = False
        self.observed = False


def _require_native_transition_observation(context):
    """Necessary observation gate only; not an issuer or native proof validator."""
    _need(
        type(context) is _RegistryTransition
        and context.validating is True
        and context.observed is True,
        "native transition snapshot was not observed",
    )


@contextlib.contextmanager
def registry_transition(scope, reference):
    """Adopt one authenticated source CAS; never accept a replacement mapping."""
    _need(type(scope) is _CodingScope, "actual coding tick source scope required")
    scope.check()
    ref = validate_reference(reference)
    import closer_native_observer as native

    validator = _source_function(native, "validate_coding_registry_transition")
    read_tick_selected(scope, ref)
    old_utf8 = scope._document_utf8
    old = json.loads(old_utf8)
    context = _RegistryTransition(inspect.currentframe(), ref, old_utf8)
    _need(scope._transition is None, "nested coding transition forbidden")
    scope._transition = context
    try:
        yield
        scope._check_context()
        scope.campaign_lock.check()
        current = scope.campaign._read_unlocked()
        scope.campaign_lock.check()
        context.new_utf8 = _registry_delta(old, current, ref)
        new = json.loads(context.new_utf8)
        context.validating = True
        result = validator(scope, old, new)
        _require_native_transition_observation(context)
        _need(
            result is None and _canonical(old) == old_utf8 and _canonical(new) == context.new_utf8,
            "fixed coding transition validation changed data",
        )
        scope._check_context()
        scope.campaign_lock.readback(json.loads(context.new_utf8))
        scope._document_utf8 = context.new_utf8
    finally:
        context.validating = False
        context.frame = None
        scope._transition = None
    scope.check()


def _transition_snapshot(scope):
    """Read selectors only from the exact fixed native post-CAS validator frame."""
    _need(type(scope) is _CodingScope, "actual coding tick source scope required")
    import closer_native_observer as native

    validator = _source_function(native, "validate_coding_registry_transition")
    caller = inspect.currentframe().f_back
    try:
        context = scope._transition
        _need(
            type(context) is _RegistryTransition
            and context.validating
            and context.thread == threading.get_ident()
            and context.pid == os.getpid()
            and caller.f_code is validator.__code__
            and caller.f_globals is native.__dict__
            and caller.f_locals.get("scope") is scope
            and _canonical(caller.f_locals.get("old_document")) == context.old_utf8
            and _canonical(caller.f_locals.get("new_document")) == context.new_utf8,
            "actual registered native transition validation required",
        )
        cursor = caller
        while cursor is not None and cursor is not context.frame:
            cursor = cursor.f_back
        _need(
            cursor is context.frame
            and cursor.f_code is registry_transition.__wrapped__.__code__
            and cursor.f_globals is globals(),
            "source transition lifetime ended",
        )
        scope._check_context()
        scope.campaign_lock.readback(json.loads(context.new_utf8))
        context.observed = True  # Only this actual native frame/TLS read may set it.
        return {
            "reference": json.loads(context.reference_utf8),
            "old_document_sha256": hashlib.sha256(context.old_utf8).hexdigest(),
            "new_document_sha256": hashlib.sha256(context.new_utf8).hexdigest(),
        }
    finally:
        del caller


def read_worker_selected(root, controller, reference):
    """Accepted actual worker only; no raw purpose JSON or tick knowledge fallback."""
    import closer_coding_worker as worker
    import closer_correction_controller as ctl

    _need(type(root) is worker._Root, "actual accepted coding worker root required")
    accessor = vars(worker._Root).get("continuation_reference")
    _need(
        isinstance(accessor, property)
        and accessor.fset is None
        and accessor.fget is not None
        and getattr(accessor.fget, "__globals__", None) is worker.__dict__,
        "source-checked worker continuation accessor unavailable",
    )
    root.check()
    ref = validate_reference(reference)
    _need(
        validate_reference(root.continuation_reference) == ref,
        "accepted worker continuation reference differs",
    )
    _need(
        type(controller) is ctl.IndexController and controller.installation is root.installation,
        "accepted worker controller installation identity differs",
    )
    installation = controller._source_installation()
    _need(installation is root.installation, "accepted worker installation identity differs")
    _closure(installation)
    worker_path = Path(worker.__file__)
    _need(
        worker_path.is_absolute()
        and worker_path.resolve(strict=True) == worker_path
        and not any(p.is_symlink() for p in (worker_path, *worker_path.parents))
        and installation.source_closure.get(worker_path.name)
        == {"path": str(worker_path), "sha256": worker._LOADED_SOURCE_SHA256}
        and hashlib.sha256(worker_path.read_bytes()).hexdigest() == worker._LOADED_SOURCE_SHA256,
        "accepted worker loaded source changed",
    )
    with contextlib.closing(ctl._Custody(controller.config)) as custody:
        record = _read_protected(custody, ref)
        _need(
            record["controller_config_sha256"] == ctl.digest(controller.config.source_binding())
            and record["manifest_path"] == installation.manifest_path
            and record["manifest_sha256"] == installation.manifest_sha256
            and record["source_closure_sha256"] == ctl.digest(installation.source_closure),
            "accepted worker protected continuation binding differs",
        )
        document = ctl.IndexController._physical_state(
            controller.config.state_path, "coding worker campaign redirected"
        )
        registry = validate_registry(document.get(REGISTRY_FIELD))
        _need(
            ref["stable_key"] in registry and registry[ref["stable_key"]]["reference"] == ref,
            "accepted worker registry/reference differs",
        )
        root.check()
        return copy.deepcopy(record)
