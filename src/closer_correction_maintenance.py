"""Prospective deterministic retained-result maintenance, never ordinary work.

Local native-wrapper/controller custody only; no scheduler/remote attestation.
An actual source-held entry/tick/Campaign-lock lifetime and a fresh queue callback
are required. Neither a JSON record nor a cleanup receipt is execution authority.
"""

from __future__ import annotations

import hashlib
import inspect
import os
import stat
import threading
from contextlib import contextmanager
from pathlib import Path

PURPOSE = "retained_correction_result_reconciliation/v1"
ACTION = "disposition"  # Existing queue action; private purpose is never a model action.
_SCHEMA = "closer-correction-maintenance-admission/v1"
_SOURCE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
_SEAL = object()
_CURRENT = threading.local()
_REFUSAL = "deterministic correction maintenance forbids provider/creation/POST"


class MaintenanceUnknown(ValueError):
    pass


def _need(value, reason):
    if not value:
        raise MaintenanceUnknown(reason)


def assert_offload_allowed():
    """Earliest offload guard: no-context is unchanged, issued scope forbids it.

    Deliberately does not accept a caller parameter, import auth/provider code,
    inspect output JSON, or offer an alternate provider. A damaged/expired active
    context also refuses rather than falling back to ordinary execution.
    """
    if getattr(_CURRENT, "scope", None) is not None:
        raise MaintenanceUnknown(_REFUSAL)
    import sys

    publication_io = sys.modules.get("closer_coding_publication_io")
    if publication_io is not None:
        publication_io.assert_offload_allowed()


def assert_effect_allowed(kind):
    if getattr(_CURRENT, "scope", None) is not None and kind not in {"GET", "confirmCAS"}:
        raise MaintenanceUnknown(_REFUSAL)


def _find_frame(code, namespace):
    frame = inspect.currentframe()
    try:
        for _ in range(128):
            if frame is None:
                break
            if frame.f_code is code and frame.f_globals is namespace:
                return frame
            frame = frame.f_back
        raise MaintenanceUnknown("actual canonical native-entry/tick frame unavailable")
    finally:
        del frame


def _live_parent_frame(thread_id, code, namespace):
    """Read the still-executing original source stack, not saved JSON/context."""
    import sys

    frame = sys._current_frames().get(thread_id)
    try:
        for _ in range(128):
            if frame is None:
                break
            if frame.f_code is code and frame.f_globals is namespace:
                return frame
            frame = frame.f_back
        raise MaintenanceUnknown("original live native-entry/tick thread unavailable")
    finally:
        del frame


def _issued_queue_operation(original, captured_snapshot, supplied, expected_sha256):
    """Recognize only the callback argument and its actual source TLS copy.

    Queue intentionally JSON-copies its live snapshot. Canonical equality alone
    never admits a caller copy or a replaced TLS object. This necessary check
    grants no authority without the source frame/parent/writer scope checks.
    """
    import closer_queue as queue

    current = getattr(queue._callback_threads, "invocation", None)
    return (
        type(current) is dict
        and type(original) is dict
        and type(captured_snapshot) is dict
        and captured_snapshot is not original
        and current.get("operation") is captured_snapshot
        and (supplied is original or supplied is captured_snapshot)
        and hashlib.sha256(queue._canonical_bytes(original)).hexdigest() == expected_sha256
        and queue._currently_invoking(current.get("target"), original)
    )


class _MaintenanceScope:
    def __init__(self, *args, **kwargs):
        raise TypeError("maintenance scope is source-issued only")

    def check(self, *, native_scope=None):
        import closer_campaign as campaign
        import closer_native_observer as native

        _need(
            self.seal is _SEAL
            and self.active
            and getattr(_CURRENT, "scope", None) is self
            and self.pid == os.getpid()
            and (
                self.thread == threading.get_ident()
                or (
                    self.callback_thread == threading.get_ident()
                    and self.callback_frame
                    is _find_frame(_execute_confirmation.__code__, globals())
                    and self.callback_frame.f_locals.get("scope") is self
                    and self.callback_frame.f_locals.get("operation") is self.callback_operation
                    and _issued_queue_operation(
                        self.callback_operation,
                        self.callback_tls_operation,
                        self.callback_operation,
                        self.callback_operation_sha256,
                    )
                )
            )
            and self.tick_frame
            is _live_parent_frame(self.thread, campaign.tick.__code__, campaign.__dict__)
            and self.entry_frame
            is _live_parent_frame(self.thread, native._native_entry.__code__, native.__dict__)
            and self.supervisor_frame
            is _live_parent_frame(self.thread, native.native_entry.__code__, native.__dict__)
            and self.tick_frame.f_locals.get("campaign") is self.campaign
            and self.tick_frame.f_locals.get("manifest") is self.manifest
            and self.entry_frame.f_locals.get("nonce") == self.nonce
            and self.entry_frame.f_locals.get("observation", {}).get("entry", {}).get("pid")
            == self.pid
            and (
                native.entry_active(self.manifest)
                if native_scope is None
                else native.entry_active(self.manifest, _scope=native_scope)
            )
            and native._CURRENT_ENTRY.get(str(self.native_root)) == self.nonce,
            "original local-wrapper maintenance lifetime changed",
        )
        for frame, handle, descriptor, expected, name in self.native_locks:
            _need(
                frame.f_locals.get("lock") is handle
                and not handle.closed
                and handle.fileno() == descriptor,
                "source-held native entry lock closed",
            )
            meta = os.fstat(descriptor)
            named = (self.native_root / name).stat(follow_symlinks=False)
            _need(
                stat.S_ISREG(meta.st_mode)
                and meta.st_uid == os.getuid()
                and meta.st_nlink == 1
                and (meta.st_dev, meta.st_ino, meta.st_ctime_ns)
                == expected
                == (named.st_dev, named.st_ino, named.st_ctime_ns),
                "source-held native entry lock changed",
            )
        _need(
            hashlib.sha256(Path(__file__).read_bytes()).hexdigest() == _SOURCE_SHA256,
            "loaded maintenance source changed",
        )
        meta = os.fstat(self.lock)
        named = self.campaign.lock_path.stat(follow_symlinks=False)
        _need(
            not self.closed
            and stat.S_ISREG(meta.st_mode)
            and meta.st_uid == os.getuid()
            and meta.st_nlink == 1
            and not meta.st_mode & 0o077
            and (meta.st_dev, meta.st_ino, meta.st_ctime_ns)
            == self.lock_identity
            == (named.st_dev, named.st_ino, named.st_ctime_ns),
            "actual source-held Campaign lock changed",
        )
        _need(
            self.campaign._read_unlocked() == self.document, "maintenance Campaign record changed"
        )
        return self


@contextmanager
def _tick_scope(campaign, document, manifest, manifest_path):
    """Fixed tick-only scope; existing UNKNOWN workers are never changed here.

    Source manages the lock until scope revocation. This assumes trusted source
    does not manually unlock its own FD; inode metadata is not an OS flock proof.
    """
    import fcntl

    import closer_campaign as campaign_source
    import closer_native_observer as native

    _need(getattr(_CURRENT, "scope", None) is None, "nested maintenance scope forbidden")
    tick_frame = _find_frame(campaign_source.tick.__code__, campaign_source.__dict__)
    entry_frame = _find_frame(native._native_entry.__code__, native.__dict__)
    supervisor_frame = _find_frame(native.native_entry.__code__, native.__dict__)
    _need(
        type(campaign) is campaign_source.Campaign
        and tick_frame.f_locals.get("campaign") is campaign
        and tick_frame.f_locals.get("manifest") is manifest
        and native.correction_cleanup_enabled(document.get("worker_config") or {})
        and document["worker_config"]["policy"] == manifest["policy"]
        and native.entry_active(manifest),
        "fresh source-owned native tick maintenance required",
    )
    root = native.contract(manifest)[3]
    nonce = native._CURRENT_ENTRY[str(root)]
    _need(
        entry_frame.f_locals.get("nonce") == nonce
        and entry_frame.f_locals.get("observation", {}).get("entry", {}).get("pid") == os.getpid()
        and entry_frame.f_locals.get("request") is None,
        "maintenance cannot reuse qualification/worker/old native entry",
    )
    descriptor = os.open(campaign.lock_path, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
    scope = None
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _need(campaign._read_unlocked() == document, "Campaign changed before maintenance lock")
        qualified_root = Path(tick_frame.f_locals["root"])
        _need(
            str(qualified_root) == manifest["mechanism_contract"]["campaign_root"],
            "current tick qualification root differs",
        )
        _need(
            not campaign_source.qualify(manifest, qualified_root),
            "current source/Core qualification unavailable",
        )
        scope = object.__new__(_MaintenanceScope)
        scope.seal, scope.active, scope.closed = _SEAL, True, False
        scope.pid, scope.thread = os.getpid(), threading.get_ident()
        scope.callback_lock = threading.Lock()
        scope.callback_used = False
        scope.callback_thread = scope.callback_frame = scope.callback_operation = None
        scope.callback_tls_operation = scope.callback_operation_sha256 = None
        scope.tick_frame, scope.entry_frame = tick_frame, entry_frame
        scope.supervisor_frame = supervisor_frame
        scope.native_locks = []
        for frame, name in (
            (entry_frame, "native-entry.lock"),
            (supervisor_frame, "native-supervisor.lock"),
        ):
            handle = frame.f_locals["lock"]
            fd = handle.fileno()
            meta = os.fstat(fd)
            scope.native_locks.append(
                (frame, handle, fd, (meta.st_dev, meta.st_ino, meta.st_ctime_ns), name)
            )
        scope.campaign, scope.document, scope.manifest = campaign, document, manifest
        scope.manifest_path, scope.native_root, scope.nonce = str(manifest_path), root, nonce
        scope.lock = descriptor
        meta = os.fstat(descriptor)
        scope.lock_identity = (meta.st_dev, meta.st_ino, meta.st_ctime_ns)
        _CURRENT.scope = scope
        scope.check()
        yield scope
        scope.check()
    finally:
        if scope is not None:
            if (
                getattr(scope, "admission", None) is not None
                and native._CURRENT_ADMISSION.get(str(scope.native_root)) is scope.admission
            ):
                native._CURRENT_ADMISSION[str(scope.native_root)] = scope.original_native_admission
            scope.active, scope.closed = False, True
            scope.tick_frame = scope.entry_frame = scope.supervisor_frame = None
            scope.native_locks = []
        if getattr(_CURRENT, "scope", None) is scope:
            del _CURRENT.scope
        os.close(descriptor)


def _historical_window():
    """Actual read-only source frame, never a caller-supplied backdating value."""
    scope = getattr(_CURRENT, "scope", None)
    _need(type(scope) is _MaintenanceScope, "actual historical maintenance scope unavailable")
    scope.check()
    frame = _find_frame(_read_history.__code__, globals())
    _need(frame.f_locals.get("scope") is scope, "historical reader belongs to another scope")
    cleanup = frame.f_locals.get("cleanup")
    _need(type(cleanup) is dict, "authenticated historical cleanup unavailable")
    return cleanup["terminal"]["native_execution"]["phases"]["B"]


def _read_history(scope, hint):
    """Authenticate selected old facts locally; no native API, issuance or write."""
    import json

    import closer_correction_bridge as bridge
    import closer_correction_controller as ctl
    import closer_correction_source_record as source
    import closer_native_observer as native
    import closer_queue as queue
    import closer_role_qualification as roles

    scope.check()
    selector = hint["selector"]
    config = ctl.ControllerConfig(
        **scope.manifest["policy"]["correction_controller_installation"]["config"]
    )
    custody = ctl._Custody(config)
    try:
        plan = custody.read(selector["plan_name"])
        _need(
            ctl.digest(plan) == selector["plan_sha256"]
            and plan["key"] == selector["effect_key"]
            and plan["phase_key"] == selector["phase_key"]
            and plan["phase"] == "issue_creation",
            "selected retained issue plan changed",
        )
        origin = plan["native_lifecycle"]
        old_operation = json.loads(origin["operation_json"])
        old_target = origin["target"]
        _need(old_target.casefold() == hint["target"].casefold(), "retained native target differs")
        cleanup = native.retained_correction_cleanup(scope.manifest, scope.document, plan)
        link = cleanup["operation_link"]
        old_queue = ctl.IndexController._physical_state(
            origin["queue_path"], "old source queue redirected"
        )
        bucket = old_queue["frontiers"][old_operation["finite_frontier"]["frontier_id"]]
        retained = bucket["items"][old_target]["operation"]
        _need(
            hashlib.sha256(queue._canonical_bytes(retained)).hexdigest()
            == link["retained_operation_sha256"],
            "old native operation moved after source cleanup",
        )
        provider = link["provider_history"]
        _need(
            type(provider) is dict
            and provider["source_record_ref"] == plan["source_record_ref"]
            and provider["target"] == old_target,
            "original provider custody missing",
        )
        old_state = ctl.IndexController._physical_state(
            provider["state_path"], "old provider pipeline redirected"
        )
        old_entry = old_state["frontiers"][old_operation["finite_frontier"]["frontier_id"]]
        old_row = old_entry["items"][old_target]
        _need(
            old_entry["run_id"] == provider["pipeline_run_id"]
            and hashlib.sha256(queue._canonical_bytes(old_row)).hexdigest()
            == provider["row_sha256"],
            "original provider rows moved after terminal cleanup",
        )
        results = old_row["results"]
        context = {
            "state_path": provider["state_path"],
            "finite_frontier": old_operation["finite_frontier"],
            "pipeline_run_id": provider["pipeline_run_id"],
            "prepare": results["prepare"],
            "interpret": results["interpret"],
            "exception": results["exception"],
        }
        facts = source.retained_original_facts(context, plan["source_record_ref"])
        prep, snapshot, packet, root, record, intent, result, arguments, materialized = facts
        seed, tests, inputs = source._qualified_owner_projection(
            context, old_row, snapshot, packet, root, historical=True
        )
        original = intent["original_request"]
        semantic = {
            "schema": original["schema"],
            **{
                key: seed[key]
                for key in ("target", "source_issue", "original_verifier", "finding", "scope")
            },
            "required_tests": tests,
            "incumbent": original["incumbent"],
        }
        _need(
            bridge._owner_semantic_digest(semantic, inputs)
            == record["owner_semantics_sha256"]
            == bridge._owner_semantic_digest(original, intent["inputs"])
            and original == plan["request"],
            "retained original semantic scope changed",
        )
        proofs = source.original_provider_proofs(context, arguments)
        required_roles, run_ids = [], []
        for proof in proofs:
            expected = proof["expected"]
            finished = proof["observed"]["finished"]
            _need(
                finished.get("cleanup_terminal") is True
                and finished.get("finite_frontier_id")
                == old_operation["finite_frontier"]["frontier_id"]
                and finished.get("finite_frontier_target") == old_target
                and expected["pool_reservation"]["frontier_id"] == finished["finite_frontier_id"]
                and expected["pool_reservation"]["target"] == old_target
                and expected["pool_reservation"]["profile_id"] == expected["profile_id"]
                and proof["run_id"] == finished["run_id"],
                "selected original provider/account completion missing",
            )
            run_ids.append(proof["run_id"])
            required_roles.append(
                {
                    "observed_at": source.retained_provider_time(proof),
                    "profile_id": expected["profile_id"],
                    "role": roles.TASK_ROLES[expected["task_type"]],
                    "profile_identity": expected["profile_identity"],
                    "qualification_id": expected["qualification_id"],
                    "qualification_record_sha256": expected["qualification_record_sha256"],
                    "production_model_contract": expected["production_model_contract"],
                }
            )
        _need(len(set(run_ids)) == len(proofs), "original provider run identities overlap")
        scope.check()
        return {
            "plan": plan,
            "hint": hint,
            "cleanup": cleanup,
            "record": record,
            "context": context,
            "arguments": arguments,
            "materialized": materialized,
            "required_roles": required_roles,
            "provider_run_ids": run_ids,
            "old_operation": old_operation,
            "old_target": old_target,
        }
    finally:
        custody.close()


def _check_roles(history):
    import closer_dispatcher as closer
    import closer_role_qualification as roles
    import execution_profiles

    for expected in history["required_roles"]:
        profile = execution_profiles.get_profile(expected["profile_id"])
        current = roles.qualified_record(profile, expected["role"], now=expected["observed_at"])
        _need(
            current is not None
            and roles.is_runtime_qualified(profile, expected["role"], now=expected["observed_at"])
            and roles.profile_identity(profile) == expected["profile_identity"]
            and current["qualification_id"] == expected["qualification_id"]
            and closer._contract_digest(current) == expected["qualification_record_sha256"]
            and current.get("production_model_contract") == expected["production_model_contract"],
            "selected original provider qualification changed",
        )


def _publish(scope, hint, history, binding):
    """One new local maintenance record; never renew the old request/worker."""
    import copy
    import json
    import time

    import closer_campaign as campaign_source
    import closer_correction_controller as ctl
    import closer_frontier_policy as finite
    import closer_native_observer as native
    import closer_runtime_binding as runtime
    import closer_source_installation as installed

    scope.check()
    origin = runtime._factory_manifest_origin(binding)
    _need(origin is not None, "actual factory manifest origin unavailable")
    _check_roles(history)
    projection = installed.controller_policy_projection(binding)
    _need(type(projection) is dict, "current controller source projection unavailable")
    config = ctl.ControllerConfig(
        **scope.manifest["policy"]["correction_controller_installation"]["config"]
    )
    target = hint["target"]
    descriptor = {
        "purpose": PURPOSE,
        "plan_sha256": ctl.digest(history["plan"]),
        "cleanup": history["cleanup"]["terminal_ref"],
        "native_entry_nonce": scope.nonce,
        "controller_sha256": projection["controller_sha256"],
        "installation_sha256": projection["installation_sha256"],
    }
    identifier = hashlib.sha256(json.dumps(descriptor, sort_keys=True).encode()).hexdigest()
    revision = "correction-maintenance:" + identifier
    operation_binding = {
        "correction_controller_sha256": projection["controller_sha256"],
        "correction_installation_sha256": projection["installation_sha256"],
        "maintenance_id": identifier,
        "purpose": PURPOSE,
    }
    frontier = finite.make_frontier(
        [target],
        owner=scope.document["owner"],
        frontier_id="correction-maintenance-" + identifier,
        bindings={
            target: {
                "revision": revision,
                "action_id": finite.action_identity(target, revision, ACTION, operation_binding),
            }
        },
    )
    timeout = native.capacity_contract(scope.manifest, control_plane_only=True)[
        "request_timeout_seconds"
    ]
    _need(
        type(timeout) in (int, float) and 1 < timeout <= finite.MAX_REQUEST_TIMEOUT_SECONDS,
        "source maintenance request bound unavailable",
    )
    role_json = json.dumps(history["required_roles"], sort_keys=True, separators=(",", ":"))
    record = {
        "schema": "closer-correction-maintenance-record/v1",
        "purpose": PURPOSE,
        "id": identifier,
        "repository": config.repository,
        "epoch": config.epoch,
        "target": target,
        "selected": descriptor,
        "original_operation_sha256": history["plan"]["owner"]["operation_sha256"],
        "required_roles_sha256": hashlib.sha256(role_json.encode()).hexdigest(),
        "finite_frontier": frontier,
        "request_timeout_text": repr(float(timeout)),
        "owner": {
            "kind": "local_wrapper_controller",
            "process_id": scope.pid,
            "thread_id": str(scope.thread),
            "native_entry_nonce": scope.nonce,
        },
        "act_authorized": False,
        "remote_execution_attested": False,
        "source_closure_authorized": False,
        "provider_execution_authorized": False,
    }
    custody = ctl._Custody(config)
    try:
        name = "maintenance-" + identifier + ".json"
        _need(custody.create(name, record), "maintenance invocation already retained; never reuse")
        _need(custody.read(name) == record, "new maintenance record changed")
    finally:
        custody.close()
    doc = copy.deepcopy(scope.document)
    original_workers = copy.deepcopy(doc["workers"])
    original_targets = copy.deepcopy(doc["targets"])
    table = doc.setdefault("correction_reconciliations", {})
    _need(
        type(table) is dict and identifier not in table and len(table) < config.max_entries,
        "maintenance registration unavailable or saturated",
    )
    registration = {
        "record_name": name,
        "record_sha256": ctl.digest(record),
        "native_entry_nonce": scope.nonce,
        "status": "submitted",
    }
    table[identifier] = registration
    doc["revision"] = doc.get("revision", 0) + 1
    _need(
        doc["workers"] == original_workers and doc["targets"] == original_targets,
        "maintenance cannot replace original worker/outcome",
    )
    scope.campaign._write_unlocked(doc)
    scope.document = doc
    scope.check()
    directory = scope.native_root / scope.nonce / ("maintenance-" + identifier)
    directory.mkdir(mode=0o700)
    key = native._existing_lifecycle_key(scope.native_root)
    started = time.time()
    report = {
        "schema": "closer-correction-local-maintenance-observation/v1",
        "purpose": PURPOSE,
        "record_sha256": ctl.digest(record),
        "runtime_manifest_sha256": origin.sha256,
        "local_wrapper_entry": scope.entry_frame.f_locals["observation"],
        "remote_execution_attested": False,
        "act_authorized": False,
    }
    report_path = directory / "report.json"
    campaign_source._atomic_write(report_path, report, create_only=True)
    admission = {
        "schema": _SCHEMA,
        "purpose": PURPOSE,
        "nonce": scope.nonce,
        "id": identifier,
        "started_at": started,
        "expires_at": started + timeout,
        "record_name": name,
        "record_sha256": ctl.digest(record),
        "campaign": {
            "state": str(scope.campaign.path),
            "campaign_id": doc["campaign_id"],
            "owner": doc["owner"],
            "generation": doc["generation"],
        },
        "runtime_manifest": origin.path,
        "runtime_manifest_sha256": origin.sha256,
        "report": {"path": str(report_path), "sha256": native._hash(report_path)},
        "finite_frontier": frontier,
        "request_timeout_seconds": timeout,
        "act_authorized": False,
        "remote_execution_attested": False,
        "provider_execution_authorized": False,
        "source_closure_authorized": False,
    }
    envelope = {"binding": native.binding(scope.manifest), "context": admission}
    admission_path = directory / "admission.json"
    campaign_source._atomic_write(
        admission_path,
        {"record": envelope, "signature": native.mechanisms._signature(envelope, key)},
        create_only=True,
    )
    scope.id, scope.record, scope.registration = identifier, record, registration
    scope.record_config = config.source_binding()
    scope.hint, scope.history = copy.deepcopy(hint), history
    scope.required_roles_json = role_json
    scope.admission, scope.report_path = admission, str(report_path)
    scope.operation_binding, scope.revision, scope.frontier = operation_binding, revision, frontier
    scope.queue_path = str(directory / "queue.json")
    scope.binding = binding
    scope.original_native_admission = native._CURRENT_ADMISSION.get(str(scope.native_root))
    native._CURRENT_ADMISSION[str(scope.native_root)] = admission
    scope.selection = None
    return admission


def installation_snapshot(binding, value, manifest, *, native_scope=None):
    """Private purpose validation requires the real live source context, not JSON."""
    import copy
    import hmac
    import json
    import time

    import closer_correction_controller as ctl
    import closer_native_observer as native
    import closer_runtime_binding as runtime
    import closer_source_installation as installed

    origin = runtime._factory_manifest_origin(binding)
    scope = getattr(_CURRENT, "scope", None)
    if scope is None:
        return None
    scope.check(native_scope=native_scope)
    _need(
        getattr(scope, "admission", None) is not None,
        "new maintenance admission not source-published",
    )
    context = scope.admission
    _need(
        origin is not None
        and manifest == scope.manifest
        and value["config"] == scope.record_config
        and origin.path == scope.manifest_path
        and origin.sha256 == context["runtime_manifest_sha256"]
        and native._CURRENT_ADMISSION.get(str(scope.native_root)) is context
        and context["schema"] == _SCHEMA
        and context["purpose"] == PURPOSE
        and time.time() < context["expires_at"],
        "current private maintenance admission differs/expired",
    )
    _check_roles({"required_roles": json.loads(scope.required_roles_json)})
    record = scope.record
    config = ctl.ControllerConfig(**value["config"])
    custody = ctl._Custody(config)
    try:
        _need(
            custody.read(context["record_name"]) == record
            and ctl.digest(record) == context["record_sha256"]
            and scope.document["correction_reconciliations"][scope.id] == scope.registration,
            "protected current maintenance registration differs",
        )
    finally:
        custody.close()
    report, report_raw = installed._physical_json(scope.report_path, limit=1048576)
    signed, _raw = installed._physical_json(
        Path(scope.report_path).parent / "admission.json", limit=1048576
    )
    envelope = {"binding": native.binding(manifest, _scope=native_scope), "context": context}
    _need(
        signed.get("record") == envelope
        and hmac.compare_digest(
            str(signed.get("signature", "")),
            native.mechanisms._signature(
                envelope, native._existing_lifecycle_key(scope.native_root)
            ),
        )
        and context["report"]
        == {"path": scope.report_path, "sha256": hashlib.sha256(report_raw).hexdigest()}
        and report["record_sha256"] == context["record_sha256"]
        and record["required_roles_sha256"]
        == hashlib.sha256(scope.required_roles_json.encode()).hexdigest(),
        "private maintenance HMAC/report/role custody differs",
    )
    return {**value, "admission": copy.deepcopy(context), "report_path": scope.report_path}


class _ConfirmationSelection:
    def __init__(self, *_args, **_kwargs):
        raise TypeError("maintenance confirmation selection is source-issued only")


def _selection(controller, operation, custody):
    """Same live callback/writer only; no JSON or old invocation can select it."""
    import time

    import closer_queue as queue

    scope = getattr(_CURRENT, "scope", None)
    _need(type(scope) is _MaintenanceScope, "source maintenance scope unavailable")
    scope.check()
    chosen = getattr(scope, "selection", None)
    frame = _find_frame(_execute_confirmation.__code__, globals())
    _need(
        type(chosen) is _ConfirmationSelection
        and chosen.seal is _SEAL
        and chosen.active
        and chosen.scope is scope
        and chosen.controller is controller
        and chosen.operation is scope.callback_operation
        and _issued_queue_operation(
            chosen.operation,
            scope.callback_tls_operation,
            operation,
            scope.callback_operation_sha256,
        )
        and chosen.custody is custody
        and frame is chosen.frame
        and frame.f_locals.get("scope") is scope
        and frame.f_locals.get("operation") is chosen.operation
        and queue._currently_invoking(scope.hint["target"], chosen.operation)
        and operation.get("action") == ACTION
        and operation.get("binding") == scope.operation_binding
        and operation.get("finite_frontier") == scope.frontier
        and time.time() < operation["deadline"] <= scope.admission["expires_at"],
        "actual fresh confirm-only callback/writer selection differs or expired",
    )
    return chosen


def retained_materialization(controller, custody, operation, record):
    """Historical exact decision only inside the new zero-provider callback."""
    import copy

    if getattr(_CURRENT, "scope", None) is None:
        return None
    chosen = _selection(controller, operation, custody)
    history = chosen.history
    _need(
        record.get("materializer") == history["arguments"]
        and record.get("source_record_ref") == history["plan"]["source_record_ref"]
        and record.get("request") == history["plan"]["request"],
        "historical enrollment does not select exact protected original decision",
    )
    return copy.deepcopy(history["materialized"])


def _confirmation_action(controller, custody, operation, chosen, before):
    """Re-read original201 and fresh native joins; never plan or repeat a POST."""
    import closer_correction_controller as ctl
    import closer_correction_native_effects as effects

    scope = chosen.scope
    history = _read_history(scope, scope.hint)
    _check_roles(history)
    chosen.history = history
    plan = history["plan"]
    request = plan["request"]
    _need(request["incumbent"].get("kind") == "no_incumbent", "incumbent cannot gain issue")
    rows = [entry for entry in before["data"]["entries"] if entry["key"] == plan["key"]]
    _need(len(rows) == 1, "exact original source enrollment unavailable")
    entry = rows[0]
    stage = next((s for s in entry["stages"] if s["phase"] == "issue_creation"), None)
    _need(
        entry["request"] == request
        and entry["admitted_selector"] == plan["admitted_selector"]
        and stage is not None
        and stage["state"] in {"prepared", "outcome_unknown", "confirmed"}
        and stage["intent_sha256"] == ctl.digest(plan)
        and all(s["phase"] == "issue_creation" for s in entry["stages"])
        and not entry["bindings"],
        "only original prepared issue inventory can be reconciled",
    )
    key = plan["phase_key"]
    intent = custody.read("native-intent-" + key + ".json")
    _need(
        intent.get("schema") == "local-correction-native-intent/v1"
        and {k: v for k, v in intent.items() if k not in {"schema", "prepared_index"}}
        == {k: v for k, v in plan.items() if k != "schema"}
        and intent["owner"] == plan["owner"],
        "original prepared native intent/owner changed",
    )
    # The signed lifecycle link authenticates old owner/op facts. It does not
    # authorize new effects or extend that owner's expired execution window.
    _need(
        intent["owner"]["operation_sha256"]
        == history["cleanup"]["operation_link"]["operation_sha256"],
        "original intent is not the exact terminal observed operation",
    )
    response = effects.read_response(custody, intent)
    native = effects.validate_creation(
        response,
        phase="issue_creation",
        payload=intent["payload"],
        repository=controller.config.repository,
        config=controller.config,
    )
    effects._native_writer(controller, operation)
    effects._fresh_created(controller, native, response, "issue_creation")
    current_source = effects._capture_source(controller, operation, request)
    transition = effects.observed_issue_transition(
        intent["source_before"],
        response,
        current_source,
        request=request,
        payload=intent["payload"],
        config=controller.config,
    )
    custody.create("native-issue-transition-" + key + ".json", transition)
    effects._read_issue_transition(controller, custody, operation, request, required=True)
    proof = {
        "schema": "local-correction-native-origin/v1",
        "phase_key": key,
        "intent_sha256": ctl.digest(intent),
        "response_sha256": ctl.digest(response),
        "native": native,
    }
    custody.create("native-origin-" + ctl.digest(proof) + ".json", proof)
    selected = entry["admitted_selector"]
    record = custody.read("admission-" + selected["record_sha256"] + ".json")
    _need(ctl.digest(record) == selected["record_sha256"], "original admission changed")
    controller.authenticate(selected, request, custody, operation)
    controller.fresh_issue_context(
        request, record, {"owner_login": request["incumbent"].get("owner")}
    )
    effects._fresh_ownership(controller, operation, request)
    pull = controller.store.call("GET", "/pulls/" + request["target"]["pr"])
    _need(
        str(pull.get("number")) == request["target"]["pr"]
        and pull.get("head", {}).get("sha") == request["target"]["head_sha"]
        and pull.get("base", {}).get("sha") == request["target"]["base_sha"]
        and pull.get("merge_commit_sha") == request["target"]["merge_sha"],
        "fresh native original PR identity changed",
    )
    scope.check()
    return {
        "kind": "confirm",
        "key": entry["key"],
        "phase": "issue_creation",
        "proof_sha256": ctl.digest(proof),
        "result": native["subject"],
        "selector": selected,
    }


def validate_index_action(controller, custody, operation, before, action):
    """Private purpose accepts ONLY one freshly reconstructed confirmation."""
    if getattr(_CURRENT, "scope", None) is None:
        return False
    chosen = _selection(controller, operation, custody)
    controller._require_writer(operation, custody)
    expected = _confirmation_action(controller, custody, operation, chosen, before)
    _need(action == expected, "maintenance cannot select creation/provider/foreign action")
    chosen.action = expected
    return True


def guard_index_transport(controller, operation, custody, method, path, body):
    """No generic PUT permission: exact original source CAS frame/body/WAL only."""
    import base64

    import closer_correction_controller as ctl

    if getattr(_CURRENT, "scope", None) is None:
        return
    if method == "GET":
        return
    _need(method == "PUT", _REFUSAL)
    chosen = _selection(controller, operation, custody)
    frame = _find_frame(ctl.IndexController._apply_locked.__code__, ctl.__dict__)
    compare = _find_frame(ctl.NativeIndexStore.compare_and_swap.__code__, ctl.__dict__)
    _need(
        frame.f_locals.get("self") is controller
        and frame.f_locals.get("operation") is chosen.operation
        and frame.f_locals.get("custody") is custody
        and compare.f_locals.get("self") is controller.store
        and frame.f_locals.get("action") == chosen.action
        and compare.f_locals.get("before") == frame.f_locals.get("before")
        and compare.f_locals.get("after") == frame.f_locals.get("after_data")
        and path == "/repos/" + controller.config.repository + "/contents/" + ctl.INDEX_PATH,
        "maintenance PUT is not the fixed current confirmation CAS",
    )
    intent = frame.f_locals["intent"]
    before, after = frame.f_locals["before"], frame.f_locals["after_data"]
    _need(
        custody.read("intent-" + intent["transaction_key"] + ".json") == intent
        and custody.read("action-" + intent["transaction_key"] + ".json") == chosen.action
        and custody.read("before-" + intent["transaction_key"] + ".json") == before
        and after == ctl.transition(before["data"], chosen.action, controller.config)
        and body
        == {
            "branch": ctl.BRANCH,
            "message": "chore: checkpoint correction subject index",
            "sha": before["blob_sha"],
            "content": base64.b64encode(ctl.canonical(after)).decode(),
        }
        and not chosen.put_started,
        "maintenance CAS lacks exact new WAL or already submitted; never repeat",
    )
    chosen.put_started = True


@contextmanager
def _callback_scope(scope, target, operation):
    """One real queue thread under a live, still-locked source parent lifetime."""
    import closer_queue as queue

    _need(
        type(scope) is _MaintenanceScope
        and scope.seal is _SEAL
        and scope.active
        and scope.pid == os.getpid()
        and scope.thread != threading.get_ident()
        and getattr(_CURRENT, "scope", None) is None,
        "new maintenance callback requires the actual live source parent",
    )
    frame = _find_frame(_execute_confirmation.__code__, globals())
    _need(
        frame.f_locals.get("scope") is scope
        and frame.f_locals.get("operation") is operation
        and target == scope.hint["target"]
        and queue._currently_invoking(target, operation)
        and operation.get("action") == ACTION
        and operation.get("binding") == scope.operation_binding
        and operation.get("finite_frontier") == scope.frontier
        and scope.callback_lock.acquire(blocking=False),
        "actual private finite queue callback binding unavailable",
    )
    installed = False
    try:
        _need(not scope.callback_used, "maintenance callback already consumed; never repeat")
        scope.callback_used = True
        scope.callback_thread, scope.callback_frame = threading.get_ident(), frame
        scope.callback_operation = operation
        scope.callback_tls_operation = queue._callback_threads.invocation["operation"]
        scope.callback_operation_sha256 = hashlib.sha256(
            queue._canonical_bytes(operation)
        ).hexdigest()
        _CURRENT.scope = scope
        installed = True
        scope.check()
        yield
        scope.check()
    finally:
        if installed:
            if getattr(_CURRENT, "scope", None) is scope:
                del _CURRENT.scope
            scope.callback_thread = scope.callback_frame = scope.callback_operation = None
            scope.callback_tls_operation = scope.callback_operation_sha256 = None
        scope.callback_lock.release()


def _execute_confirmation(scope, target, action, _observed, operation):
    """One new real queue callback; zero model calls and zero creation POSTs."""
    with _callback_scope(scope, target, operation):
        return _execute_confirmation_in_callback(scope, target, action, _observed, operation)


@contextmanager
def _confirmation_writer(controller, operation, chosen):
    """Revoke the one callback selection before source releases its writer FD."""
    with controller.fenced(operation) as custody:
        chosen.custody = custody
        try:
            yield custody
        finally:
            chosen.active = False
            chosen.custody = None
            chosen.scope.selection = None


def _execute_confirmation_in_callback(scope, target, action, _observed, operation):
    import copy

    import closer_correction_controller as ctl
    import closer_queue as queue
    import closer_source_installation as installed

    _need(action == ACTION and target == scope.hint["target"], "private maintenance target differs")
    scope.check()
    _need(getattr(scope, "selection", None) is None, "maintenance callback already selected")
    installation = installed.source_installation_for(scope.binding)
    _need(installation is not None, "current source factory installation unavailable")
    controller = ctl.IndexController(
        ctl.ControllerConfig(**installation.config), installation=installation
    )
    chosen = object.__new__(_ConfirmationSelection)
    chosen.seal, chosen.active, chosen.scope = _SEAL, True, scope
    chosen.controller, chosen.operation = controller, operation
    chosen.custody, chosen.action, chosen.put_started = None, None, False
    chosen.frame = _find_frame(_execute_confirmation.__code__, globals())
    chosen.history = scope.history
    scope.selection = chosen
    result = None
    try:
        with _confirmation_writer(controller, operation, chosen) as custody:
            controller._require_writer(operation, custody)
            # Existing unresolved CAS is never resubmitted. Its own stable
            # before/action/intent remains the transaction being read back.
            anchor = controller.anchor(custody)
            names = os.listdir(custody.fd)
            _need(
                len(names) <= 64 * controller.config.max_entries + 64,
                "custody inventory exceeds bound",
            )
            pending = []
            for name in names:
                if not name.startswith("intent-") or not name.endswith(".json"):
                    continue
                key = name[len("intent-") : -len(".json")]
                result_name = "result-" + key + ".json"
                if (
                    result_name not in names
                    or custody.read(result_name)["after"]["data"]["revision"] > anchor["revision"]
                ):
                    pending.append(key)
            _need(len(pending) <= 1, "multiple unresolved index writes remain owned UNKNOWN")
            if pending:
                original_action = custody.read("action-" + pending[0] + ".json")
                current = controller.store.read()
                validate_index_action(controller, custody, operation, current, original_action)
                result = controller._reconcile_locked(
                    pending[0], operation=operation, custody=custody
                )
                _need(
                    not chosen.put_started, "read-only pending CAS reconciliation attempted a write"
                )
            else:
                current = controller.read_bound(custody)
                selected_action = _confirmation_action(
                    controller, custody, operation, chosen, current
                )
                chosen.action = copy.deepcopy(selected_action)
                result = controller._apply_locked(
                    selected_action, operation=operation, custody=custody
                )
            _need(
                result.get("status") in {"checkpoint_confirmed", "checkpoint_reconciled_only"},
                "retained native issue is still unconfirmed: "
                + str(result.get("reason", "unknown")),
            )
            final = controller.read_bound(custody)
            entry = next(
                row
                for row in final["data"]["entries"]
                if row["key"] == scope.history["plan"]["key"]
            )
            stage = next(row for row in entry["stages"] if row["phase"] == "issue_creation")
            _need(stage["state"] == "confirmed", "native issue stage was not confirmed")
            receipt = {
                "schema": "closer-correction-maintenance-result/v1",
                "purpose": PURPOSE,
                "maintenance_id": scope.id,
                "original_operation_sha256": scope.record["original_operation_sha256"],
                "current_operation_id": operation["id"],
                "current_operation_sha256": hashlib.sha256(
                    queue._canonical_bytes(operation)
                ).hexdigest(),
                "index_revision": final["data"]["revision"],
                "native_issue": stage["result"],
                "proof_sha256": stage["proof_sha256"],
                "checkpoint": result,
                "post_attempted": False,
                "provider_execution_authorized": False,
                "act_authorized": False,
                "source_closure_authorized": False,
                "remote_execution_attested": False,
            }
            custody.create("maintenance-result-" + scope.id + ".json", receipt)
            return {
                "status": "completed",
                "advanced": True,
                "target_complete": False,
                "retry_safe": False,
                "receipt": receipt,
                "act_authorized": False,
                "source_closure_authorized": False,
                "post_attempted": False,
            }
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        RuntimeError,
        AttributeError,
        StopIteration,
    ) as error:
        return {
            "status": "uncertain",
            "advanced": False,
            "target_complete": False,
            "retry_safe": False,
            "reason": str(error)[:1000],
            "checkpoint": result,
            "act_authorized": False,
            "source_closure_authorized": False,
            "post_attempted": False,
        }
    finally:
        chosen.active = False
        chosen.frame = None
        scope.selection = None


def maintenance_round_record(identifier, original_operation_sha256, receipt):
    """Private v2 envelope preserves exact queue JSON without admitting floats."""
    import closer_queue as queue

    _need(
        type(receipt) is dict and receipt.get("schema") == queue.RECEIPT_SCHEMA,
        "actual maintenance queue receipt schema differs",
    )
    raw = queue._canonical_bytes(receipt)
    _need(
        len(raw) <= queue.MAX_CALLBACK_JSON_BYTES, "maintenance queue receipt exceeds custody bound"
    )
    return {
        "schema": "closer-correction-maintenance-round/v2",
        "purpose": PURPOSE,
        "maintenance_id": identifier,
        "queue_json": raw.decode("utf-8"),
        "queue_sha256": hashlib.sha256(raw).hexdigest(),
        "queue_bytes": len(raw),
        "original_operation_sha256": original_operation_sha256,
        "remote_execution_attested": False,
        "act_authorized": False,
        "source_closure_authorized": False,
        "provider_execution_authorized": False,
    }


def _private_completion_queue_join(scope, receipt, operation):
    """Necessary immutable current-purpose joins, not domain acceptance."""
    import closer_queue as queue

    initial_fields = {
        "id",
        "action",
        "status",
        "attempts",
        "revision",
        "owner",
        "repo_keys",
        "claim_targets",
        "binding",
        "submitted_at",
        "request_started_at",
        "deadline",
        "timeout_seconds",
        "finite_frontier",
    }
    later_fields = {"result", "elapsed_seconds", "next_trigger", "completed_at", "invoked_at"}
    _need(
        type(operation) is dict
        and initial_fields <= set(operation) <= initial_fields | later_fields
        and operation["status"] == "completed"
        and operation["id"] == receipt["current_operation_id"]
        and operation["action"] == ACTION
        and operation["attempts"] == 1
        and operation["revision"] == scope.revision
        and operation["binding"] == scope.operation_binding
        and operation["finite_frontier"] == scope.frontier
        and operation.get("result", {}).get("receipt") == receipt,
        "protected private-purpose receipt/current queue differs",
    )
    submitted = {key: operation[key] for key in initial_fields}
    submitted["status"] = "submitted"
    _need(
        hashlib.sha256(queue._canonical_bytes(submitted)).hexdigest()
        == receipt["current_operation_sha256"],
        "protected private-purpose original submitted operation differs",
    )


def _private_completion_index_join(scope, custody, receipt, inspection):
    """Existing read-only WAL/high-water proof, never a native read or permit."""
    import closer_correction_controller as ctl
    import closer_correction_native_effects as effects

    _need(
        not inspection["highwater"]["pending_transactions"],
        "private confirmation still has unresolved native index write",
    )
    anchor = inspection["highwater"]["anchor"]
    result = custody.read(anchor["result_name"])
    _need(ctl.digest(result) == anchor["result_sha256"], "protected index result changed")
    data = result["after"]["data"]
    entry = next(
        (row for row in data["entries"] if row["key"] == scope.history["plan"]["key"]), None
    )
    stage = next(
        (row for row in (entry or {}).get("stages", []) if row["phase"] == "issue_creation"), None
    )
    _need(
        receipt["schema"] == "closer-correction-maintenance-result/v1"
        and receipt["purpose"] == PURPOSE
        and receipt["maintenance_id"] == scope.id
        and receipt["original_operation_sha256"] == scope.record["original_operation_sha256"]
        and receipt["checkpoint"]["status"]
        in {"checkpoint_confirmed", "checkpoint_reconciled_only"}
        and receipt["index_revision"] == data["revision"] == anchor["revision"]
        and ctl.digest(data) == anchor["state_sha256"]
        and stage is not None
        and stage["state"] == "confirmed"
        and stage["result"] == receipt["native_issue"]
        and stage["proof_sha256"] == receipt["proof_sha256"]
        and stage["intent_sha256"] == ctl.digest(scope.history["plan"])
        and all(
            receipt[key] is False
            for key in (
                "post_attempted",
                "provider_execution_authorized",
                "act_authorized",
                "source_closure_authorized",
                "remote_execution_attested",
            )
        ),
        "private inventory result is not protected confirmed issue proof",
    )
    if receipt["checkpoint"]["status"] == "checkpoint_confirmed":
        _need(
            receipt["checkpoint"]["transaction_key"] == result["intent"]["transaction_key"]
            and result["intent"]["owner"]["operation_id"] == receipt["current_operation_id"]
            and result["intent"]["owner"]["operation_sha256"]
            == receipt["current_operation_sha256"],
            "new confirmation result belongs to a different private callback",
        )
    origin = custody.read("native-origin-" + receipt["proof_sha256"] + ".json")
    plan = scope.history["plan"]
    intent = custody.read("native-intent-" + plan["phase_key"] + ".json")
    response = effects.read_response(custody, intent)
    native = effects.validate_creation(
        response,
        phase="issue_creation",
        payload=intent["payload"],
        repository=scope.record_config["repository"],
        config=ctl.ControllerConfig(**scope.record_config),
    )
    _need(
        ctl.digest(origin) == receipt["proof_sha256"]
        and origin.get("schema") == "local-correction-native-origin/v1"
        and origin["phase_key"] == plan["phase_key"]
        and origin["intent_sha256"] == ctl.digest(intent)
        and origin["response_sha256"] == ctl.digest(response)
        and origin["native"] == native
        and native["subject"] == receipt["native_issue"],
        "private completion does not retain the exact original native201",
    )


def private_purpose_observation(scope, target):
    """Finish only this protected maintenance frontier; old domain is untouched."""
    import closer_correction_controller as ctl

    scope.check()
    _need(target == scope.hint["target"], "private purpose target changed")
    base = {
        "revision": scope.revision,
        "operation_binding": scope.operation_binding,
        "evidence": {
            "purpose": PURPOSE,
            "maintenance_id": scope.id,
            "domain_complete": False,
            "act_authorized": False,
            "provider_execution_authorized": False,
            "source_closure_authorized": False,
        },
    }
    config = ctl.ControllerConfig(**scope.record_config)
    custody = ctl._Custody(config)
    try:
        try:
            receipt = custody.read("maintenance-result-" + scope.id + ".json")
        except FileNotFoundError:
            if not scope.callback_used:
                return {**base, "action": ACTION, "eligible": True}
            return {
                **base,
                "action": "wait",
                "eligible": False,
                "reason": "private native outcome UNKNOWN; read-only reconciliation only",
                "next_trigger": "owner:"
                + scope.document["owner"]
                + ";native_index_readback_or_source_change;deadline:"
                + repr(scope.admission["expires_at"]),
            }
        state = ctl.IndexController._physical_state(
            scope.queue_path, "current private queue redirected"
        )
        operation = state["frontiers"][scope.frontier["frontier_id"]]["items"][target]["operation"]
        _private_completion_queue_join(scope, receipt, operation)
        inspection = ctl.inspect_provisioned_custody(scope.record_config)
        _private_completion_index_join(scope, custody, receipt, inspection)
        scope.check()
        return {
            **base,
            "action": "complete",
            "eligible": False,
            "reason": "ONLY current private native inventory purpose confirmed; old domain UNKNOWN",
            "next_trigger": "existing_owner_domain_coordinator",
        }
    finally:
        custody.close()


def run_if_selected(campaign, document, manifest, manifest_path, pending):
    """Existing canonical tick's deterministic maintenance alternative only.

    Generic worker retirement/retry remains unchanged. No old UNKNOWN worker,
    request, target, owner or deadline is edited or renewed by this route.
    """
    import copy
    import time

    import closer_campaign as campaign_source
    import closer_correction_controller as ctl
    import closer_correction_source_record as source
    import closer_native_observer as native
    import closer_queue as queue
    import closer_runtime_binding as runtime

    cfg = document.get("worker_config") or {}
    if pending.get("kind") != "reconcile" or not native.correction_cleanup_enabled(cfg):
        return None
    try:
        _find_frame(campaign_source.tick.__code__, campaign_source.__dict__)
        binding = runtime.binding_from_manifest(manifest_path)
        _need(binding is not None, "current source/Core runtime factory unavailable")
        targets = cfg["finite_frontier"]["targets"]
        _need(
            type(targets) is list and 0 < len(targets) <= 128,
            "bounded original frontier unavailable",
        )
        hints = [
            hint
            for target in targets
            if (hint := source.retained_issue_discovery(binding, target)) is not None
        ]
        if not hints:
            return None
        _need(len(hints) == 1, "retained issue discovery is ambiguous; old work remains UNKNOWN")
        hint = hints[0]
        _need(hint.get("selector") is not None, "retained issue discovery is owned UNKNOWN")
        with _tick_scope(campaign, document, manifest, manifest_path) as scope:
            original_workers = copy.deepcopy(document["workers"])
            original_targets = copy.deepcopy(document["targets"])
            history = _read_history(scope, hint)
            _publish(scope, hint, history, binding)
            remaining = scope.admission["expires_at"] - time.time() - 1.0
            _need(remaining > 0, "maintenance request exhausted before new queue submission")

            def observe(target):
                return private_purpose_observation(scope, target)

            def execute(target, action, observed, operation):
                return _execute_confirmation(scope, target, action, observed, operation)

            receipt = queue.run_round(
                scope.queue_path,
                [hint["target"]],
                observe,
                execute,
                owner=scope.document["owner"],
                deadline=None,
                receipt_reserve_seconds=0,
                min_action_seconds=0,
                max_workers=1,
                max_passes=1,
                max_operation_retries=0,
                finite_frontier=scope.frontier,
                frontier_target=hint["target"],
                request_timeout_seconds=remaining,
            )
            scope.check()
            _need(
                scope.document["workers"] == original_workers
                and scope.document["targets"] == original_targets,
                "maintenance cannot replace original worker/outcome/targets",
            )
            custody = ctl._Custody(ctl.ControllerConfig(**scope.record_config))
            try:
                custody.create(
                    "maintenance-round-" + scope.id + ".json",
                    maintenance_round_record(
                        scope.id, scope.record["original_operation_sha256"], receipt
                    ),
                )
            finally:
                custody.close()
            return {
                "kind": "retained_correction_maintenance",
                "purpose": PURPOSE,
                "maintenance_id": scope.id,
                "queue": receipt,
                "old_worker_unchanged": True,
                "post_attempted": False,
                "act_authorized": False,
                "source_closure_authorized": False,
                "provider_execution_authorized": False,
                "remote_execution_attested": False,
            }
    except (
        ImportError,
        OSError,
        ValueError,
        TypeError,
        KeyError,
        RuntimeError,
        AttributeError,
    ) as error:
        return {
            "kind": "held",
            "purpose": PURPOSE,
            "reason": str(error)[:1000],
            "old_worker_unchanged": True,
            "post_attempted": False,
            "act_authorized": False,
            "source_closure_authorized": False,
            "provider_execution_authorized": False,
            "remote_execution_attested": False,
        }
