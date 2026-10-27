"""Read-only native admission for original-queue metadata observation.

This uses existing native entry/admission and private queue custody. It never
provisions a correction index, signs a new authority, registers a consumer,
starts a task, or authorizes GitHub/provider effects from serialized results.
"""

from __future__ import annotations

import copy
import fcntl
import hashlib
import inspect
import math
import os
import re
import stat
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import closer_codex_metadata as metadata
import closer_correction_controller as ctl

_SOURCE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
_SCOPES: dict[int, object] = {}
_HANDLES: dict[int, tuple] = {}
_PLAN_SCHEMA = "closer-codex-initial-consumer/v1"


def _need(value, code):
    if not value:
        raise metadata.MetadataHeld(code)


def _json_sha(value):
    import closer_queue as queue

    return hashlib.sha256(queue._canonical_bytes(value)).hexdigest()


def _file(path, limit=4 * 1024 * 1024):
    import closer_source_installation as source

    return source._physical_json(str(path), limit=limit)


def _directory(path, *, create=False):
    _need(
        path.is_absolute()
        and path.resolve(strict=False) == path
        and not any(p.is_symlink() for p in (path, *path.parents)),
        "metadata_custody_alias",
    )
    if create:
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            pass
    info = path.stat(follow_symlinks=False)
    _need(
        stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid() and not info.st_mode & 0o077,
        "metadata_custody_not_private",
    )
    return info.st_dev, info.st_ino


class _Custody:
    def __init__(self, path):
        self.path = path
        self.identity = _directory(path)
        self.fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)

    def check(self):
        _need(
            _directory(self.path) == self.identity
            and (os.fstat(self.fd).st_dev, os.fstat(self.fd).st_ino) == self.identity,
            "metadata_custody_replaced",
        )

    def read(self, name):
        self.check()
        _need(
            type(name) is str and re.fullmatch(r"[a-z0-9.-]+", name),
            "metadata_custody_name",
        )
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self.fd)
        with os.fdopen(fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            _need(
                stat.S_ISREG(before.st_mode)
                and before.st_uid == os.getuid()
                and before.st_nlink == 1
                and not before.st_mode & 0o077
                and 0 < before.st_size <= 1048576,
                "metadata_custody_file_invalid",
            )
            raw = stream.read(1048577)
            after = os.fstat(stream.fileno())
            _need(
                (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                == (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
                and len(raw) == before.st_size,
                "metadata_custody_file_changed",
            )
        return ctl.decode(raw)

    def create(self, name, value):
        self.check()
        _need(
            type(name) is str and re.fullmatch(r"[a-z0-9.-]+", name),
            "metadata_custody_name",
        )
        raw = ctl.canonical(value)
        _need(0 < len(raw) <= 1048576, "metadata_custody_file_bounds")
        try:
            fd = os.open(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=self.fd,
            )
        except FileExistsError:
            _need(self.read(name) == value, "metadata_existing_custody_differs")
            return False
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.fsync(self.fd)
        return True


def _source_policy(binding):
    import closer_campaign as campaign
    import closer_mechanisms as mechanisms
    import closer_runtime_binding as runtime

    origin = runtime._factory_manifest_origin(binding)
    _need(
        origin is not None and binding.mechanism == mechanisms.NATIVE,
        "metadata_actual_runtime_factory_required",
    )
    manifest, raw = _file(origin.path, 16 * 1024 * 1024)
    _need(
        hashlib.sha256(raw).hexdigest() == origin.sha256
        and binding.policy == manifest.get("policy"),
        "metadata_manifest_changed",
    )
    actual = campaign.load_manifest(origin.path)
    _need(
        actual == manifest and not campaign.qualify(manifest, Path(origin.path).parent),
        "metadata_manifest_not_qualified",
    )
    _need(
        manifest["modules"].get(Path(__file__).name) == _SOURCE_SHA256
        and manifest["modules"].get("closer_runtime_binding.py") == runtime._BINDING_SOURCE_SHA256
        and str(Path(__file__).parent) == manifest["module_dir"],
        "metadata_loaded_source_not_selected",
    )
    import closer_codex_appserver_bootstrap as protocol
    import closer_codex_owned_stdio as owned
    import closer_codex_registration as registration

    for module, digest in (
        (metadata, metadata._SOURCE_SHA256),
        (protocol, protocol._LOADED_SOURCE_SHA256),
        (owned, owned._LOADED_SOURCE_SHA256),
        (registration, registration._SOURCE_SHA256),
    ):
        path = Path(module.__file__)
        _need(
            str(path.parent) == manifest["module_dir"]
            and manifest["modules"].get(path.name) == digest,
            "metadata_loaded_helper_not_selected",
        )
    spec = manifest["policy"].get(metadata.POLICY)
    _need(
        type(spec) is dict
        and set(spec)
        == {
            "schema",
            "binary_path",
            "binary_sha256",
            "home",
            "codex_home",
            "private_root",
            "cwd",
            "profile_id",
        }
        and spec["schema"] == metadata.PURPOSE
        and type(spec["binary_sha256"]) is str
        and re.fullmatch(r"[0-9a-f]{64}", spec["binary_sha256"])
        and type(spec["profile_id"]) is str
        and 0 < len(spec["profile_id"]) <= 128,
        "metadata_source_policy_invalid",
    )
    plan = manifest["policy"].get("codex_initial_consumer")
    _need(
        type(plan) is dict
        and set(plan) == {"schema", "target", "consumer_id", "registration"}
        and plan["schema"] == _PLAN_SCHEMA
        and type(plan["target"]) is str
        and re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#[1-9][0-9]*", plan["target"])
        and type(plan["consumer_id"]) is str
        and bool(plan["consumer_id"])
        and type(plan["registration"]) is dict
        and "account_sha256" not in plan["registration"],
        "metadata_initial_consumer_plan_invalid",
    )
    _need(
        registration.selected_plan(manifest) == plan,
        "metadata_initial_consumer_plan_not_source_validated",
    )
    return origin, manifest, copy.deepcopy(spec), copy.deepcopy(plan)


class MetadataAdmission:
    """Factory-issued live read-only callback scope; never effect authority."""

    def __init__(self):
        raise metadata.MetadataHeld("metadata_original_admission_factory_required")

    def live(self):
        import closer_native_observer as native
        import closer_queue as queue
        import closer_runtime_binding as runtime

        _need(
            _SCOPES.get(threading.get_ident()) is self
            and self.active
            and self.thread == threading.get_ident()
            and self.pid == os.getpid(),
            "metadata_original_callback_expired",
        )
        current = getattr(queue._callback_threads, "invocation", None)
        _need(
            type(current) is dict
            and current is self.invocation
            and queue._currently_invoking(self.target, self.operation)
            and current.get("operation") is self.tls_operation
            and queue._canonical_bytes(self.operation) == self.operation_bytes,
            "metadata_original_queue_callback_changed",
        )
        origin = runtime._factory_manifest_origin(self.binding)
        _need(
            origin is self.origin
            and origin.sha256 == self.manifest_sha
            and native.entry_active(self.manifest),
            "metadata_native_entry_changed",
        )
        for name, expected in self.file_pins.items():
            _, raw = _file(name, 16 * 1024 * 1024)
            _need(
                hashlib.sha256(raw).hexdigest() == expected,
                "metadata_native_source_changed",
            )
        document, _ = _file(self.state_path)
        _need(
            all(document.get(k) == self.campaign[k] for k in ("campaign_id", "owner", "generation"))
            and document.get("release_pin") == self.manifest["package_digest"]
            and (document.get("worker_config") or {}).get("runtime_manifest") == self.origin.path,
            "metadata_campaign_state_changed",
        )
        submitted, _ = _file(self.queue_path)
        bucket = submitted.get("frontiers", {}).get(
            self.operation["finite_frontier"]["frontier_id"], {}
        )
        _need(
            bucket.get("items", {}).get(self.target, {}).get("operation") == self.operation,
            "metadata_physical_queue_operation_changed",
        )
        _need(
            self.operation["finite_frontier"] == self.admission["finite_frontier"]
            and self.target in self.admission["finite_frontier"]["targets"],
            "metadata_native_frontier_changed",
        )
        lock = os.fstat(self.lock)
        _need(
            (lock.st_dev, lock.st_ino) == self.lock_identity
            and stat.S_ISREG(lock.st_mode)
            and lock.st_uid == os.getuid()
            and not lock.st_mode & 0o077,
            "metadata_writer_lock_changed",
        )
        self.custody.check()
        _need(time.monotonic() < self.deadline, "metadata_original_window_exhausted")

    def check_native(self):
        import closer_codex_owned_stdio as owned
        import closer_native_observer as native

        self.live()
        for name, digest in self.source_pins.items():
            owned._physical(name, expected=digest)
        native.validate_admission(
            self.manifest,
            self.admission,
            for_effects=True,
            for_test="private_test" in self.manifest["mechanism_contract"],
        )
        owned._managed_route_absent()
        for key in ("home", "codex_home", "private_root", "cwd"):
            _need(
                owned._physical(self.spec[key], directory=True) == self.physical[key],
                "metadata_selection_changed",
            )
        _need(
            owned._physical(
                self.spec["binary_path"],
                expected=self.spec["binary_sha256"],
                binary=True,
            )
            == self.physical["binary"],
            "metadata_selection_changed",
        )
        _need(
            metadata._config_pins(self.spec) == self.config_pins,
            "metadata_configuration_changed",
        )


@contextmanager
def metadata_scope(binding, operation, *, purpose="codex_metadata_qualification"):
    """Only an existing genuine native admission + original queue may issue it."""
    import closer_codex_owned_stdio as owned
    import closer_native_observer as native
    import closer_queue as queue

    _need(not _SCOPES, "metadata_scope_already_active")
    origin, manifest, spec, plan = _source_policy(binding)
    import closer_codex_public_canary as public_test
    import closer_codex_registration as registration

    is_public = purpose == public_test.POLICY
    _need(is_public or purpose == metadata.POLICY, "metadata_source_purpose_invalid")
    chosen = public_test.selected_policy(manifest) if is_public else plan
    callback_module = public_test if is_public else registration
    callback_code = callback_module.execute_original.__code__
    if is_public:
        _need(
            manifest["modules"].get(Path(public_test.__file__).name) == public_test._SOURCE_SHA256
            and str(Path(public_test.__file__).parent) == manifest["module_dir"],
            "public_test_source_not_selected",
        )
    frame = inspect.currentframe().f_back
    original_callback = original_queue = None
    try:
        for _ in range(128):
            if frame is None:
                break
            if frame.f_code is callback_code and frame.f_globals is vars(callback_module):
                original_callback = frame
            if frame.f_code is queue._invoke_before_cutoff.__code__ and frame.f_globals is vars(
                queue
            ):
                original_queue = frame
            frame = frame.f_back
        _need(
            original_callback is not None
            and original_queue is not None
            and original_callback.f_locals.get("binding") is binding
            and original_callback.f_locals.get("operation") is operation
            and original_queue.f_locals.get("operation") is operation,
            "metadata_original_source_callback_required",
        )
    finally:
        del frame
    current = getattr(queue._callback_threads, "invocation", None)
    _need(
        type(current) is dict
        and queue._currently_invoking(current.get("target"), operation)
        and current["target"] == chosen["target"]
        and operation.get("action") == "disposition",
        "metadata_original_disposition_required",
    )
    expected_binding = {
        "runtime_manifest_sha256": origin.sha256,
        "codex_metadata_qualification_sha256": ctl.digest(spec),
        (
            public_test.POLICY + "_sha256" if is_public else "codex_initial_consumer_sha256"
        ): ctl.digest(chosen),
    }
    _need(
        operation.get("binding") == expected_binding,
        "metadata_original_selection_changed",
    )
    report = Path(os.environ.get("CLOSER_CAPACITY_REPORT", ""))
    root = Path(manifest["mechanism_contract"]["observation_root"])
    _need(
        report.name == "scoped-capacity.json"
        and report.parent.parent == root
        and re.fullmatch(r"[0-9a-f]{32}", report.parent.name),
        "metadata_native_report_path_invalid",
    )
    signed, signed_raw = _file(report.parent / "admission.json")
    admission = signed.get("record", {}).get("context")
    _need(
        type(admission) is dict
        and admission.get("runtime_manifest") == origin.path
        and admission.get("runtime_manifest_sha256") == origin.sha256
        and admission.get("report", {}).get("path") == str(report),
        "metadata_native_admission_changed",
    )
    native.validate_admission(
        manifest,
        admission,
        for_effects=True,
        for_test="private_test" in manifest["mechanism_contract"],
    )
    _need(native.entry_active(manifest), "metadata_native_entry_missing")
    state_path = Path(admission["campaign"]["state"])
    queue_path = Path(
        str(state_path)
        + (".codex-public-canary-queue.json" if is_public else ".codex-metadata-queue.json")
    )
    _need(
        current.get("state_path") == str(queue_path)
        and state_path.parent == Path(manifest["mechanism_contract"]["campaign_root"]),
        "metadata_fixed_queue_path_changed",
    )
    seconds = operation.get("deadline", 0) - time.time()
    _need(
        type(seconds) in (int, float)
        and math.isfinite(seconds)
        and 2 < seconds <= (120 if is_public else 35)
        and operation.get("finite_frontier") == admission.get("finite_frontier"),
        "metadata_original_budget_or_frontier_invalid",
    )
    _, report_raw = _file(report)
    evidence = Path(str(state_path) + ".evidence")
    _directory(evidence, create=True)
    custody_path = evidence / "codex-account-metadata"
    _directory(custody_path, create=True)
    custody = _Custody(custody_path)
    lock = None
    scope = None
    try:
        lock = os.open(
            "writer.lock",
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
            0o600,
            dir_fd=custody.fd,
        )
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        info = os.fstat(lock)
        scope = object.__new__(MetadataAdmission)
        scope.active, scope.thread, scope.pid = True, threading.get_ident(), os.getpid()
        scope.binding, scope.origin, scope.manifest = binding, origin, manifest
        scope.manifest_sha, scope.spec, scope.plan = origin.sha256, spec, plan
        scope.operation, scope.operation_bytes = operation, queue._canonical_bytes(operation)
        scope.invocation, scope.tls_operation = current, current["operation"]
        scope.target, scope.admission = current["target"], admission
        scope.campaign = admission["campaign"]
        scope.state_path, scope.queue_path = state_path, queue_path
        scope.custody, scope.lock, scope.lock_identity = (
            custody,
            lock,
            (info.st_dev, info.st_ino),
        )
        scope.deadline = time.monotonic() + min(115 if is_public else 30, seconds - 2)
        scope.file_pins = {
            origin.path: origin.sha256,
            str(report): hashlib.sha256(report_raw).hexdigest(),
            str(report.parent / "admission.json"): hashlib.sha256(signed_raw).hexdigest(),
        }
        scope.source_pins = {
            str(Path(manifest["module_dir"]) / name): manifest["modules"][name]
            for name in (
                Path(__file__).name,
                "closer_codex_metadata.py",
                "closer_codex_appserver_bootstrap.py",
                "closer_codex_owned_stdio.py",
                "closer_codex_registration.py",
                "closer_runtime_binding.py",
            )
        }
        if is_public:
            scope.source_pins[
                str(Path(manifest["module_dir"]) / "closer_codex_public_canary.py")
            ] = manifest["modules"]["closer_codex_public_canary.py"]
        scope.physical = {
            key: owned._physical(spec[key], directory=True)
            for key in ("home", "codex_home", "private_root", "cwd")
        }
        scope.physical["binary"] = owned._physical(
            spec["binary_path"], expected=spec["binary_sha256"], binary=True
        )
        scope.config_pins = metadata._config_pins(spec)
        _SCOPES[scope.thread] = scope
        scope.check_native()
        yield scope
        scope.live()
    finally:
        if scope is not None:
            scope.active = False
            _SCOPES.pop(scope.thread, None)
            for key, held in list(_HANDLES.items()):
                if held[1] is scope:
                    _HANDLES.pop(key, None)
        if lock is not None:
            os.close(lock)
        os.close(custody.fd)


def _transaction(scope):
    _need(type(scope) is MetadataAdmission, "metadata_original_admission_factory_required")
    scope.live()
    key = ctl.digest(
        {
            "schema": metadata.PURPOSE,
            "operation_id": scope.operation["id"],
            "manifest_sha256": scope.origin.sha256,
            "selection_sha256": ctl.digest(scope.spec),
        }
    )
    intent = {
        "schema": metadata.PURPOSE,
        "operation_id": scope.operation["id"],
        "manifest_sha256": scope.origin.sha256,
        "selection_sha256": ctl.digest(scope.spec),
        "guard_source_sha256": _SOURCE_SHA256,
        "codec_source_sha256": metadata._SOURCE_SHA256,
    }
    return key, intent


def qualify_admitted_callback(scope):
    key, intent = _transaction(scope)
    _need(
        scope.custody.create("intent-" + key + ".json", intent),
        "metadata_prior_intent_no_retry",
    )
    scope.check_native()
    metadata_deadline = min(scope.deadline, time.monotonic() + 30)
    raw, features_bytes = metadata._observe(
        "features", spec=scope.spec, deadline=metadata_deadline, check=scope.live
    )
    features = metadata.feature_values(raw)
    scope.check_native()
    protocol = metadata.AccountMetadataProtocol(deadline=metadata_deadline)
    _, account_bytes = metadata._observe(
        "metadata",
        spec=scope.spec,
        deadline=metadata_deadline,
        check=scope.live,
        protocol=protocol,
    )
    scope.check_native()
    _need(
        protocol.snapshot()["metadata_complete"] and protocol._native_account_id is not None,
        "metadata_account_incomplete",
    )
    result = {
        **intent,
        "schema": "closer-codex-admitted-metadata-result/v1",
        "account_sha256": ctl.digest(
            {
                "schema": "managed-native-account/v1",
                "account_id": protocol._native_account_id,
            }
        ),
        "profile_id": scope.spec["profile_id"],
        "configuration_sha256": metadata._pin_digest(scope.config_pins),
        "features_sha256": hashlib.sha256(raw).hexdigest(),
        "plugins_observed_disabled": features["plugins"] is False,
        "counted_features_bytes": features_bytes,
        "counted_metadata_bytes": account_bytes,
        "execution_authorized": False,
        "registration_installed": False,
        "thread_starts": 0,
        "turn_starts": 0,
        "cloud_policy_lease": False,
    }
    name = "result-" + key + ".json"
    _need(scope.custody.create(name, result), "metadata_existing_result_no_retry")
    return {"name": name, "sha256": ctl.digest(result), "execution_authorized": False}


class _AccountHandle:
    def __init__(self):
        raise metadata.MetadataHeld("metadata_protected_reader_required")


def read_admitted_account(scope, reference):
    key, intent = _transaction(scope)
    _need(
        type(reference) is dict
        and reference.get("name") == "result-" + key + ".json"
        and set(reference) == {"name", "sha256", "execution_authorized"}
        and reference["execution_authorized"] is False,
        "metadata_result_reference_invalid",
    )
    _need(
        scope.custody.read("intent-" + key + ".json") == intent,
        "metadata_intent_changed",
    )
    record = scope.custody.read(reference["name"])
    fields = set(intent) | {
        "account_sha256",
        "profile_id",
        "configuration_sha256",
        "features_sha256",
        "plugins_observed_disabled",
        "counted_features_bytes",
        "counted_metadata_bytes",
        "execution_authorized",
        "registration_installed",
        "thread_starts",
        "turn_starts",
        "cloud_policy_lease",
    }
    _need(
        type(record) is dict
        and set(record) == fields
        and ctl.digest(record) == reference["sha256"]
        and all(record.get(k) == v for k, v in intent.items() if k != "schema")
        and record.get("schema") == "closer-codex-admitted-metadata-result/v1"
        and record.get("configuration_sha256") == metadata._pin_digest(scope.config_pins)
        and record.get("profile_id") == scope.spec["profile_id"]
        and type(record.get("account_sha256")) is str
        and re.fullmatch(r"[0-9a-f]{64}", record["account_sha256"])
        and record.get("execution_authorized") is record.get("registration_installed") is False
        and record.get("plugins_observed_disabled") is True
        and type(record.get("thread_starts")) is type(record.get("turn_starts")) is int
        and record["thread_starts"] == record["turn_starts"] == 0
        and record.get("cloud_policy_lease") is False
        and type(record.get("features_sha256")) is str
        and re.fullmatch(r"[0-9a-f]{64}", record["features_sha256"])
        and all(
            type(record.get(key)) is int and 0 < record[key] <= 131072
            for key in ("counted_features_bytes", "counted_metadata_bytes")
        ),
        "metadata_protected_result_changed",
    )
    value = object.__new__(_AccountHandle)
    _HANDLES[id(value)] = (
        value,
        scope,
        copy.deepcopy(reference),
        record["account_sha256"],
    )
    return value


def retain_admitted_initial_registration(scope, handle):
    _transaction(scope)
    held = _HANDLES.get(id(handle))
    _need(
        type(handle) is _AccountHandle
        and held is not None
        and held[0] is handle
        and held[1] is scope,
        "metadata_protected_reader_required",
    )
    fresh = read_admitted_account(scope, held[2])
    fresh_row = _HANDLES.pop(id(fresh))
    _need(fresh_row[3] == held[3], "metadata_account_changed")
    plan = copy.deepcopy(scope.plan)
    registration = plan["registration"]
    _need(
        "account_sha256" not in registration
        and registration.get("profile_id") == scope.spec["profile_id"]
        and all(
            registration.get("native_bootstrap", {}).get(k) == scope.spec[k]
            for k in (
                "binary_path",
                "binary_sha256",
                "home",
                "codex_home",
                "private_root",
            )
        ),
        "metadata_initial_registration_selection_changed",
    )
    registration["account_sha256"] = held[3]
    _HANDLES.pop(id(handle))
    value = {
        "schema": "closer-codex-initial-consumer-proposal/v1",
        "target": plan["target"],
        "consumer_id": plan["consumer_id"],
        "registration": registration,
        "metadata_reference": held[2],
        "runtime_manifest_sha256": scope.origin.sha256,
        "registration_installed": False,
        "execution_authorized": False,
    }
    name = "initial-consumer-" + ctl.digest(value) + ".json"
    scope.live()
    _need(scope.custody.create(name, value), "metadata_initial_proposal_already_retained")
    return {
        "name": name,
        "sha256": ctl.digest(value),
        "execution_authorized": False,
        "registration_installed": False,
    }
