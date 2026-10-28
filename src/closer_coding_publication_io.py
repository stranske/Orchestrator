"""Distinct existing-PR publication IO; no model, acceptance or caller JSON grant.

The private scope is issued only by the actual qualified native campaign tick.
Historical coding work is immutable input, never an operation to resume.
"""

from __future__ import annotations

import contextlib
import hashlib
import inspect
import os
import sys
import threading
import time
from pathlib import Path

import closer_coding_publication as publication
import closer_correction_controller as ctl
import closer_queue as queue

ACTION = "correction_publish_existing_pr"
ADMISSION_SCHEMA = "closer-native-publication-io-admission/v1"
RESULT_SCHEMA = "closer-confirmed-existing-pr-publication/v1"
RESULT_REFERENCE_SCHEMA = "closer-owned-pr-publication-result-reference/v1"
_SOURCE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
_SEAL = object()
_CURRENT = threading.local()


def _need(ok, reason):
    if not ok:
        raise publication.PublicationHeld(reason)


def _live_frame(thread, expected):
    frame = sys._current_frames().get(thread)
    while frame is not None and frame is not expected:
        frame = frame.f_back
    return frame is expected


class _PublicationScope:
    def __init__(self, *_args, **_kwargs):
        raise TypeError("publication IO scope is source-issued only")

    def check(self):
        import closer_campaign as campaign
        import closer_native_observer as native

        _need(
            getattr(self, "seal", None) is _SEAL
            and getattr(self, "active", False)
            and self.pid == os.getpid(),
            "actual source publication IO lifetime unavailable",
        )
        _need(
            self.tick_frame.f_code is campaign.tick.__code__
            and self.tick_frame.f_globals is campaign.__dict__
            and _live_frame(self.thread, self.tick_frame)
            and self.tick_frame.f_locals.get("campaign") is self.campaign
            and self.tick_frame.f_locals.get("manifest") is self.manifest,
            "original publication tick ended or changed",
        )
        _need(
            _live_frame(self.thread, self.entry_frame)
            and self.entry_frame.f_code is native._native_entry.__code__
            and self.entry_frame.f_globals is native.__dict__
            and self.entry_frame.f_locals.get("nonce") == self.entry_nonce
            and native._CURRENT_ENTRY.get(str(self.native_root)) == self.entry_nonce
            and native.entry_active(self.manifest),
            "current native IO entry differs",
        )
        _need(
            threading.get_ident() == self.thread and getattr(_CURRENT, "scope", None) is self,
            "publication must remain on original source thread",
        )
        if self.callback_frame is not None:
            _need(
                _live_frame(self.thread, self.callback_frame)
                and self.callback_frame.f_code is _execute.__code__
                and self.callback_frame.f_globals is globals()
                and self.callback_frame.f_locals.get("scope") is self
                and self.callback_frame.f_locals.get("operation") is self.operation
                and queue._currently_invoking(self.target, self.operation),
                "actual publication queue callback differs",
            )
        self.lock.check()
        for source_frame, handle, identity, name in self.native_locks:
            _need(
                source_frame.f_locals.get("lock") is handle,
                "source-held native lock changed",
            )
            held = os.fstat(handle.fileno())
            named = (self.native_root / name).stat(follow_symlinks=False)
            _need(
                (held.st_dev, held.st_ino, held.st_ctime_ns)
                == identity
                == (named.st_dev, named.st_ino, named.st_ctime_ns),
                "source-held native lock redirected",
            )
        _need(
            _live_frame(self.thread, self.supervisor_frame)
            and self.supervisor_frame.f_code is native.native_entry.__code__
            and self.supervisor_frame.f_globals is native.__dict__,
            "actual native supervisor ended",
        )
        _need(time.time() < self.admission["expires_at"], "publication IO admission expired")
        _need(
            publication._bytes(
                ctl.IndexController._physical_state(
                    str(self.campaign.path), "publication campaign redirected"
                )
            )
            == publication._bytes(self.document),
            "publication campaign changed",
        )


@contextlib.contextmanager
def _callback_scope(scope, operation):
    _need(type(scope) is _PublicationScope, "source publication scope required")
    caller = inspect.currentframe().f_back.f_back
    _need(
        caller.f_code is _execute.__code__
        and caller.f_globals is globals()
        and caller.f_locals.get("scope") is scope
        and caller.f_locals.get("operation") is operation,
        "actual publication source callback required",
    )
    _need(
        getattr(_CURRENT, "scope", None) is scope
        and threading.get_ident() == scope.thread
        and scope.callback_frame is None,
        "nested publication callback forbidden",
    )
    scope.callback_frame, scope.callback_thread, scope.operation = (
        caller,
        threading.get_ident(),
        operation,
    )
    try:
        scope.check()
        _need(
            operation["action"] == ACTION
            and operation["finite_frontier"] == scope.frontier
            and operation["deadline"] <= scope.admission["expires_at"],
            "publication operation outside original IO admission",
        )
        yield
        scope.check()
    finally:
        scope.callback_frame = scope.callback_thread = scope.operation = None


def current_scope():
    scope = getattr(_CURRENT, "scope", None)
    _need(type(scope) is _PublicationScope, "actual publication IO scope absent")
    scope.check()
    return scope


def assert_offload_allowed():
    if getattr(_CURRENT, "scope", None) is not None:
        raise publication.PublicationHeld("publication IO cannot run a model/provider")


def _execute(scope, target, action, observed, operation):
    import closer_source_installation as installed

    _need(
        type(scope) is _PublicationScope and target == scope.target and action == ACTION,
        "exact source publication action required",
    )
    with _callback_scope(scope, operation):
        installation = installed.source_installation_for(scope.binding)
        _need(installation is not None, "publication factory installation unavailable")
        controller = ctl.IndexController(
            ctl.ControllerConfig(**installation.config), installation=installation
        )
        with controller.fenced(operation) as custody:
            scope.controller, scope.custody = controller, custody
            try:
                result = publication.execute_existing_update(
                    controller, custody, operation, scope.handoff_reference
                )
                return _complete(scope, controller, custody, operation, result)
            finally:
                scope.controller = scope.custody = None


def run_callback_round(scope):
    """Use the real durable queue; no replacement runner or mini operation."""
    _need(type(scope) is _PublicationScope, "actual source publication scope required")
    scope.check()

    def observe(target):
        scope.check()
        _need(target == scope.target, "publication queue target differs")
        return {
            "revision": scope.revision,
            "action": ACTION,
            "eligible": scope.confirmed is None,
            "requires_coordinator": False,
            "operation_binding": scope.binding_data,
            "evidence": {},
            "claim_targets": [scope.target],
            "repo_keys": [scope.config.repository],
        }

    def execute(target, action, observed, operation):
        return _execute(scope, target, action, observed, operation)

    remaining = min(scope.request_seconds, scope.admission["expires_at"] - time.time())
    _need(remaining > 0, "publication admission exhausted before submission")
    return queue.run_round(
        scope.queue_path,
        [scope.target],
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
        frontier_target=scope.target,
        request_timeout_seconds=remaining,
        _publication_inline_source=scope,
    )


class _ConfirmedPublication:
    def __init__(self, *_args, **_kwargs):
        raise TypeError("confirmed publication is source-issued only")


def read_confirmed_result(controller, custody, operation, confirmed_result):
    """Read-only knowledge for the reserved verification caller, not authority."""
    try:
        import closer_publication_verification as verification
    except ImportError:
        raise publication.PublicationHeld("source verification consumer unavailable") from None

    consumer_scope = current_scope()
    consumer_path = Path(verification.__file__)
    _need(
        consumer_scope.qualified_value["source_closure"].get(consumer_path.name)
        == {
            "path": str(consumer_path),
            "sha256": getattr(verification, "_LOADED_SOURCE_SHA256", None),
        }
        and hashlib.sha256(consumer_path.read_bytes()).hexdigest()
        == verification._LOADED_SOURCE_SHA256,
        "verification consumer outside source closure",
    )
    caller = inspect.currentframe().f_back.f_back
    _need(
        caller.f_code is verification.from_published.__code__
        and caller.f_globals is verification.__dict__
        and all(
            caller.f_locals.get(k) is v
            for k, v in {
                "controller": controller,
                "custody": custody,
                "operation": operation,
                "confirmed_result": confirmed_result,
            }.items()
        ),
        "actual publication verification consumer required",
    )
    _need(
        type(confirmed_result) is _ConfirmedPublication and confirmed_result.seal is _SEAL,
        "source-confirmed publication object required",
    )
    scope = current_scope()
    _need(
        confirmed_result.scope is scope
        and scope.controller is controller
        and scope.custody is custody
        and scope.operation is operation,
        "confirmed publication crossed IO callback",
    )
    controller._full_writer_validation(operation, custody)
    record = custody.read(confirmed_result.reference["name"])
    _need(
        publication.digest(record) == confirmed_result.reference["sha256"]
        and publication._bytes(record) == confirmed_result.record_bytes,
        "immutable confirmed publication changed",
    )
    view = publication._copy(record)
    view["publication_result_reference"] = publication._copy(confirmed_result.reference)
    view["provenance"]["publication_result_reference"] = publication._copy(
        confirmed_result.reference
    )
    return view


def _complete(scope, controller, custody, operation, result):
    """Issue the opaque verification handoff only after native/index completion."""
    caller = inspect.currentframe().f_back
    _need(
        caller.f_code is _execute.__code__
        and caller.f_globals is globals()
        and caller.f_locals.get("result") is result
        and current_scope() is scope,
        "actual publication completion callback required",
    )
    _need(
        type(result) is dict and type(result.get("publication_result_reference")) is dict,
        "source native publication result unavailable",
    )
    reference = result["publication_result_reference"]
    publication._exact(reference, {"schema", "name", "sha256"})
    _need(
        reference["schema"] == RESULT_REFERENCE_SCHEMA
        and publication._sha(reference["sha256"])
        and reference["name"] == "publication-result-" + reference["sha256"] + ".json",
        "native publication result selector differs",
    )
    record = custody.read(reference["name"])
    _need(
        record["schema"] == RESULT_SCHEMA
        and publication.digest(record) == reference["sha256"]
        and publication._bytes(record["request"])
        == publication._bytes(scope.original_record["request"])
        and publication._bytes(record["original"]) == publication._bytes(scope.original_identity)
        and publication._bytes(record["findings"])
        == publication._bytes(scope.original_record["materializer"]["raw_findings"])
        and publication._bytes(record["required_tests"])
        == publication._bytes(scope.original_record["request"]["required_tests"])
        and all(record[k] is False for k in publication._AUTHORITY),
        "confirmed native publication crossed original work",
    )
    controller._full_writer_validation(operation, custody)
    current = controller.read_bound(custody)
    entry = next(
        (
            row
            for row in current["data"]["entries"]
            if row["key"] == record["original"]["effect_key"]
        ),
        None,
    )
    origin = record["provenance"]["publication_origin_reference"]
    subject = {
        "kind": "pull",
        "number": str(record["pr"]["pr_number"]),
        "node_id": record["pr"]["pr_node_id"],
    }
    _need(
        entry is not None
        and any(
            publication._bytes(b["subject"]) == publication._bytes(subject)
            and b["proof_sha256"] == origin["sha256"]
            and publication._bytes(b["selector"])
            == publication._bytes(scope.history["admitted_selector"])
            for b in entry["bindings"]
        ),
        "native existing PR binding not confirmed",
    )
    confirmed = object.__new__(_ConfirmedPublication)
    confirmed.seal, confirmed.scope = _SEAL, scope
    confirmed.reference, confirmed.record_bytes = publication._copy(reference), publication._bytes(
        record
    )
    scope.confirmed = confirmed
    scope.verification_continuation = None
    try:
        import closer_publication_verification as verification

        consumer_path = Path(verification.__file__)
        consumer_pin = scope.qualified_value["source_closure"].get(consumer_path.name)
        _need(
            consumer_pin
            == {
                "path": str(consumer_path),
                "sha256": getattr(verification, "_LOADED_SOURCE_SHA256", None),
            }
            and hashlib.sha256(consumer_path.read_bytes()).hexdigest() == consumer_pin["sha256"],
            "source-pinned publication verification consumer unavailable",
        )
        scope.verification_continuation = verification.from_published(
            controller, custody, operation, confirmed
        )
    except Exception:
        scope.verification_held = True
    return {
        "status": "completed",
        "advanced": True,
        "publication_result_reference": publication._copy(reference),
        **{k: False for k in publication._AUTHORITY},
    }


def _find_frame(code, namespace):
    frame = inspect.currentframe().f_back
    while frame is not None:
        if frame.f_code is code and frame.f_globals is namespace:
            return frame
        frame = frame.f_back
    raise publication.PublicationHeld("actual native publication source frame unavailable")


def _known_handoff(manifest, document, config):
    """Read completed source custody, never adopt orphan or UNKNOWN work."""
    import json

    import closer_coding_continuation as continuation
    import closer_coding_session_v2 as session
    import closer_native_observer as native

    registry = continuation.validate_registry(document.get(continuation.REGISTRY_FIELD, {}))
    if not registry:
        return None
    # The original result is known, but coding has not yet been selected. This
    # registry state owns no publication handoff; leave its authentication and
    # promotion to the existing coding coordinator instead of preempting it.
    if (
        all(row["state"] == "awaiting_original_terminal" for row in registry.values())
        and document.get("workers")
        and all(
            worker.get("state") == "terminal"
            and worker.get("outcome") in {"ok", "unavailable", "scoped_continuation"}
            for worker in document["workers"]
        )
    ):
        return None
    _need(
        all(
            w.get("state") == "terminal"
            and w.get("outcome") in {"ok", "unavailable", "scoped_continuation"}
            for w in document["workers"]
        )
        and all(r["state"] == "terminal" for r in registry.values()),
        "prior worker remains live or unknown",
    )
    found = []
    custody = ctl._Custody(config)
    try:
        for row in registry.values():
            reference = row["reference"]
            knowledge = native._coding_terminal_knowledge(manifest, document, reference)
            selector = row["worker"]
            directory = native.contract(manifest)[3] / selector["nonce"]
            delivery, _delivery_ref = native._read_lifecycle_record(
                directory,
                "coding-b-delivery-" + selector["worker_id"] + ".json",
                native._existing_lifecycle_key(native.contract(manifest)[3]),
            )
            cfg = json.loads(delivery["worker_config_json"])
            operation, terminal_ref, _actual, _queue_sha = native._coding_queue_terminal(
                cfg, delivery["candidate"]
            )
            frontier = selector["finite_frontier"]
            state = ctl.IndexController._physical_state(
                selector["queue_state"], "coding publication queue redirected"
            )
            completed = state["frontiers"][frontier["frontier_id"]]["items"][
                selector["coding_target"]
            ]["operation"]
            result = completed["result"]
            handoff_ref = result.get("publication_handoff_reference")
            if handoff_ref is None:
                continue
            protected = continuation._read_protected(custody, reference)
            admission = custody.read("admission-" + protected["admission_sha256"] + ".json")
            _need(
                ctl.digest(admission) == protected["admission_sha256"], "original admission changed"
            )
            policy = publication.validate_consumer_mode(
                protected["consumer_id"],
                config.consumers[protected["consumer_id"]],
                session.snapshot_from_admission(admission),
            )
            _need(policy is not None, "publication absent from original admission")
            _need(
                admission["request"]["incumbent"].get("kind") != "no_incumbent",
                "existing incumbent required",
            )
            handoff = publication._read_handoff_data(custody, handoff_ref)
            baseline = custody.read(handoff["baseline_reference"]["name"])
            publication.validate_handoff_result_data(
                result, operation, terminal_ref, handoff, baseline
            )
            _need(
                baseline["admission_sha256"] == protected["admission_sha256"]
                and baseline["publication_policy_sha256"] == publication.digest(policy),
                "original publication baseline/policy changed",
            )
            found.append(
                {
                    "continuation_reference": reference,
                    "terminal_knowledge": knowledge,
                    "terminal_reference": terminal_ref,
                    "handoff_reference": handoff_ref,
                    "handoff": handoff,
                    "baseline": baseline,
                    "original_record": admission,
                    "consumer_id": protected["consumer_id"],
                    "admitted_selector": protected["admitted_selector"],
                    "policy": policy,
                    "operation": operation,
                    "target": selector["coding_target"],
                    "original_history": native._coding_original_history_knowledge(
                        manifest, document, config, reference
                    ),
                }
            )
        _need(len(found) <= 1, "multiple pending publications require explicit source selection")
        return found[0] if found else None
    finally:
        custody.close()


@contextlib.contextmanager
def _tick_scope(campaign, document, manifest, manifest_path, binding, value, history):
    """Distinct actual native tick, original locks and finite IO admission only."""
    import hmac

    import closer_campaign as campaign_source
    import closer_coding_continuation as continuation
    import closer_frontier_policy as frontier_policy
    import closer_native_observer as native
    import closer_runtime_binding as runtime

    frame = inspect.currentframe().f_back.f_back
    _need(
        frame.f_code is run_if_selected.__code__ and frame.f_globals is globals(),
        "source publication selector required",
    )
    tick = _find_frame(campaign_source.tick.__code__, campaign_source.__dict__)
    entry = _find_frame(native._native_entry.__code__, native.__dict__)
    supervisor = _find_frame(native.native_entry.__code__, native.__dict__)
    origin = runtime._factory_manifest_origin(binding)
    _need(
        getattr(_CURRENT, "scope", None) is None
        and type(campaign) is campaign_source.Campaign
        and tick.f_locals.get("campaign") is campaign
        and tick.f_locals.get("manifest") is manifest
        and entry.f_locals.get("request") is None
        and entry.f_locals.get("observation", {}).get("entry", {}).get("pid") == os.getpid()
        and origin is not None
        and origin.path == str(manifest_path)
        and origin.sha256 == value["manifest_sha256"]
        and document["worker_config"]["policy"] == manifest["policy"],
        "fresh qualified native publication tick required",
    )
    with continuation._campaign_lock(campaign) as lock:
        lock.readback(document)
        scope = object.__new__(_PublicationScope)
        scope.seal, scope.active = _SEAL, True
        scope.pid, scope.thread = os.getpid(), threading.get_ident()
        scope.tick_frame, scope.entry_frame = tick, entry
        scope.supervisor_frame = supervisor
        scope.campaign, scope.document, scope.manifest = (
            campaign,
            publication._copy(document),
            manifest,
        )
        scope.native_root = native.contract(manifest)[3]
        scope.entry_nonce = native._CURRENT_ENTRY[str(scope.native_root)]
        _need(entry.f_locals.get("nonce") == scope.entry_nonce, "native entry nonce changed")
        scope.lock = lock
        scope.native_locks = []
        for source_frame, name in (
            (entry, "native-entry.lock"),
            (supervisor, "native-supervisor.lock"),
        ):
            handle = source_frame.f_locals["lock"]
            meta = os.fstat(handle.fileno())
            scope.native_locks.append(
                (source_frame, handle, (meta.st_dev, meta.st_ino, meta.st_ctime_ns), name)
            )
        scope.binding, scope.qualified_value = binding, publication._copy(value)
        scope.config = ctl.ControllerConfig(**value["config"])
        scope.history = publication._copy(history)
        scope.original_record, scope.policy = history["original_record"], history["policy"]
        scope.handoff_reference = history["handoff_reference"]
        scope.original_identity = {
            "repository_id": scope.config.repository_id,
            "epoch": scope.config.epoch,
            "effect_key": ctl.effect_key(scope.original_record["request"]),
            "request_sha256": ctl.digest(scope.original_record["request"]),
            "consumer_id": history["consumer_id"],
        }
        scope.slot = publication.stable_slot(scope.original_identity)
        scope.target = history["target"]
        scope.revision = history["handoff_reference"]["sha256"]
        scope.binding_data = {
            "publication_handoff_sha256": scope.revision,
            "correction_controller_sha256": value["policy_projection"]["controller_sha256"],
            "correction_installation_sha256": value["policy_projection"]["installation_sha256"],
        }
        scope.frontier = frontier_policy.make_frontier(
            [scope.target],
            owner=document["owner"],
            frontier_id="publication-" + scope.slot,
            bindings={
                scope.target: {
                    "revision": scope.revision,
                    "action_id": frontier_policy.action_identity(
                        scope.target, scope.revision, ACTION, scope.binding_data
                    ),
                }
            },
        )
        capacity = native.capacity_contract(manifest, control_plane_only=True)
        _need(
            capacity is not None and capacity.get("schema") == "native-scoped-capacity/v2",
            "finite native IO policy required",
        )
        scope.request_seconds = min(
            scope.policy["request_timeout_seconds"], capacity["request_timeout_seconds"]
        )
        now = time.time()
        scope.admission = {
            "schema": ADMISSION_SCHEMA,
            "purpose": ACTION,
            "issued_at": now,
            "expires_at": now + scope.request_seconds,
            "request_timeout_seconds": scope.request_seconds,
            "finite_frontier": scope.frontier,
            "manifest_path": value["manifest_path"],
            "manifest_sha256": value["manifest_sha256"],
            "source_closure_sha256": ctl.digest(value["source_closure"]),
            "campaign_sha256": hashlib.sha256(queue._canonical_bytes(document)).hexdigest(),
            "native_entry_nonce": scope.entry_nonce,
            "handoff_reference": scope.handoff_reference,
            "slot_key": scope.slot,
            **{key: False for key in publication._AUTHORITY},
        }
        scope.callback_frame = scope.callback_thread = scope.operation = None
        scope.controller = scope.custody = None
        # Validate the actual source locks/lifetime before even reserving the
        # stable slot. This temporary TLS is revoked on every refusal.
        _CURRENT.scope = scope
        try:
            scope.check()
        finally:
            del _CURRENT.scope
        custody = ctl._Custody(scope.config)
        try:
            scope.custody_path = custody.path
            scope.queue_path = custody.path / ("publication-io-" + scope.slot + ".queue.json")

            envelope = {"binding": native.binding(manifest), "context": scope.admission}
            signed = {
                "record": envelope,
                "signature": native.mechanisms._signature(
                    envelope, native._existing_lifecycle_key(scope.native_root)
                ),
            }
            scope.admission_record = admission_custody_data(signed)
            scope.admission_name = (
                "publication-io-admission-" + publication.digest(scope.admission_record) + ".json"
            )
            # Stable ownership survives a crash, expiry or UNKNOWN. Never remint.
            _need(
                custody.create(
                    "publication-io-ownership-" + scope.slot + ".json",
                    {
                        "schema": ADMISSION_SCHEMA,
                        "admission_name": scope.admission_name,
                        "handoff_reference": scope.handoff_reference,
                    },
                ),
                "publication IO already owned; reconcile before any new admission",
            )
            _need(
                custody.create(scope.admission_name, scope.admission_record),
                "publication IO admission already exists",
            )
            _need(
                hmac.compare_digest(
                    read_admission_custody_data(custody.read(scope.admission_name))["signature"],
                    signed["signature"],
                ),
                "publication admission readback changed",
            )
            scope.signed_admission = signed
            scope.callback_frame = scope.callback_thread = scope.operation = None
            scope.controller = scope.custody = None
            scope.confirmed = None
            _CURRENT.scope = scope
            try:
                scope.check()
                yield scope
                scope.check()
            finally:
                scope.active = False
                if getattr(_CURRENT, "scope", None) is scope:
                    del _CURRENT.scope
        finally:
            custody.close()


def installation_snapshot(binding, value, manifest, *, native_scope=None):
    """Only the live source factory may select the separately signed IO scope."""
    import hmac

    import closer_native_observer as native
    import closer_source_installation as source

    scope = getattr(_CURRENT, "scope", None)
    if scope is None:
        return None
    frame = inspect.currentframe().f_back
    _need(
        frame.f_code is source._snapshot_current.__code__
        and frame.f_globals is source.__dict__
        and frame.f_locals.get("binding") is binding
        and frame.f_locals.get("value") is value
        and frame.f_locals.get("manifest") is manifest,
        "actual factory publication snapshot required",
    )
    scope.check()
    _need(
        binding is scope.binding
        and publication._bytes(value) == publication._bytes(scope.qualified_value)
        and manifest == scope.manifest,
        "publication factory binding changed",
    )
    custody = ctl._Custody(scope.config)
    try:
        retained = custody.read(scope.admission_name)
        signed = read_admission_custody_data(retained)
        _need(
            publication._bytes(retained) == publication._bytes(scope.admission_record)
            and publication._bytes(signed) == publication._bytes(scope.signed_admission)
            and hmac.compare_digest(
                signed["signature"],
                native.mechanisms._signature(
                    signed["record"], native._existing_lifecycle_key(scope.native_root)
                ),
            ),
            "publication IO native signature changed",
        )
    finally:
        custody.close()
    return {
        **value,
        "admission": publication._copy(scope.admission),
        "report_path": str(scope.custody_path / scope.admission_name),
    }


def run_if_selected(campaign, document, manifest, manifest_path, pending):
    """Canonical native tick only; no ordinary source/model request is revived."""
    import closer_campaign as campaign_source
    import closer_native_observer as native
    import closer_runtime_binding as runtime
    import closer_source_installation as installed

    cfg = document.get("worker_config") or {}
    consumers = (
        manifest.get("policy", {})
        .get("correction_controller_installation", {})
        .get("config", {})
        .get("consumers", {})
    )
    if not any(publication.publication_key(row) for row in consumers.values()):
        return None
    if pending.get("kind") != "reconcile":
        return None
    _need(
        native.correction_cleanup_enabled(cfg),
        "publication waits for original terminal reconciliation",
    )
    _find_frame(campaign_source.tick.__code__, campaign_source.__dict__)
    binding = runtime.binding_from_manifest(manifest_path)
    _need(binding is not None, "current publication runtime factory unavailable")
    value, qualified_manifest = installed._qualified_policy(binding)
    _need(qualified_manifest == manifest, "publication manifest changed")
    for module in (sys.modules[__name__], publication):
        path = Path(module.__file__)
        loaded = (
            _SOURCE_SHA256 if module is sys.modules[__name__] else publication._LOADED_SOURCE_SHA256
        )
        _need(
            value["source_closure"].get(path.name) == {"path": str(path), "sha256": loaded}
            and hashlib.sha256(path.read_bytes()).hexdigest() == loaded,
            "pinned publication source unavailable",
        )
    history = _known_handoff(manifest, document, ctl.ControllerConfig(**value["config"]))
    if history is None:
        return None
    with _tick_scope(campaign, document, manifest, manifest_path, binding, value, history) as scope:
        receipt = run_callback_round(scope)
        scope.check()
        followup = None
        continuation = getattr(scope, "verification_continuation", None)
        if continuation is not None:
            import closer_publication_verification as verification

            followup = verification.for_coordinator(continuation)
        return {
            "kind": "existing_pr_publication",
            "verification_followup": followup,
            "queue_receipt": receipt,
            "publication_result_reference": (
                publication._copy(scope.confirmed.reference) if scope.confirmed else None
            ),
            **{key: False for key in publication._AUTHORITY},
        }


class _IOObservation:
    def __init__(self, scope, controller, custody, operation):
        caller = inspect.currentframe().f_back
        _need(
            caller.f_code is select_existing_update.__code__ and caller.f_globals is globals(),
            "source publication observation required",
        )
        self.scope, self.controller, self.custody, self.operation = (
            scope,
            controller,
            custody,
            operation,
        )
        self.record, self.policy = scope.original_record, scope.policy
        self.check()

    @property
    def consumer_id(self):
        return self.scope.history["consumer_id"]

    @property
    def target(self):
        return self.scope.target

    @property
    def workspaces(self):
        return self.scope.document["worker_config"]["workspaces"]

    def check(self):
        scope = current_scope()
        _need(
            scope is self.scope
            and scope.controller is self.controller
            and scope.custody is self.custody
            and scope.operation is self.operation
            and scope.callback_frame is not None,
            "actual IO observation callback differs",
        )
        self.controller._full_writer_validation(self.operation, self.custody)


def _selection_check(selection, controller=None, custody=None, operation=None):
    scope = current_scope()
    _need(
        type(selection) is dict
        and selection is getattr(scope, "selection", None)
        and publication._bytes(selection) == scope.selection_bytes
        and scope.operation is not None
        and queue._currently_invoking(scope.target, scope.operation)
        and (controller is None or controller is scope.controller)
        and (custody is None or custody is scope.custody)
        and (operation is None or operation is scope.operation),
        "original publication selection/writer changed",
    )
    scope.observed.check()
    return scope


def select_existing_update(controller, custody, operation, handoff_reference):
    caller = inspect.currentframe().f_back
    _need(
        caller.f_code is publication._publication_selection.__code__
        and caller.f_globals is publication.__dict__,
        "actual publication front door required",
    )
    scope = current_scope()
    _need(
        scope.controller is controller
        and scope.custody is custody
        and scope.operation is operation
        and publication._bytes(handoff_reference) == publication._bytes(scope.handoff_reference)
        and getattr(scope, "selection", None) is None,
        "actual publication writer/handoff differs",
    )
    controller._full_writer_validation(operation, custody)
    handoff = publication._read_handoff_data(custody, handoff_reference)
    _need(
        publication._bytes(handoff) == publication._bytes(scope.history["handoff"]),
        "retained handoff changed after selection",
    )
    scope.observed = _IOObservation(scope, controller, custody, operation)
    pr = publication._native_owned_pr(scope.observed)
    baseline = scope.history["baseline"]
    _need(
        publication._bytes(pr) == publication._bytes(baseline["native_pr"]),
        "native owner/base/head changed since coding baseline",
    )
    intent = publication.prepare_intent(
        scope.original_identity,
        scope.policy,
        pr,
        handoff["candidate"],
        {
            "baseline_sha256": handoff["baseline_reference"]["sha256"],
            "handoff_sha256": handoff_reference["sha256"],
            "terminal_sha256": handoff["terminal_reference"]["sha256"],
            "source_scope_sha256": ctl.digest(scope.qualified_value),
            "original_admission_sha256": ctl.digest(scope.original_record),
            "fresh_io_admission_sha256": publication.digest(scope.admission),
            "current_operation_sha256": hashlib.sha256(
                queue._canonical_bytes(operation)
            ).hexdigest(),
        },
    )
    scope.selection = {"intent": intent}
    scope.selection_bytes = publication._bytes(scope.selection)
    return scope.selection


def host_git_context(selection):
    caller = inspect.currentframe().f_back
    _need(
        caller.f_code is publication._host_git_context.__code__
        and caller.f_globals is publication.__dict__,
        "actual publication HostGit port required",
    )
    scope = _selection_check(selection)
    _need(getattr(scope, "host", None) is None, "HostGit already issued")
    host = publication._observe_git_context(scope.observed, selection["intent"]["pr"])
    try:
        _need(
            publication._bytes(host.summary)
            == publication._bytes(scope.history["baseline"]["host_git"]),
            "original protected Git/helper context changed",
        )
        if scope.policy["schema"] == publication.ANCHOR_POLICY_SCHEMA:
            _need(
                scope.history["baseline"]["schema"] == "closer-publication-source-baseline/v2",
                "original no-copy baseline version differs",
            )
            anchor = publication._source_snapshot_seed(
                host,
                scope.history["baseline"]["baseline"],
                prior=scope.history["baseline"]["existing_objects"],
            )
            _need(
                anchor.summary() == scope.history["baseline"]["existing_objects"],
                "original existing object fingerprint changed before reentry",
            )
        scope.host = host
        return host
    except BaseException:
        host.close()
        raise


def require_io_admission(controller, custody, operation, selection, host):
    scope = _selection_check(selection, controller, custody, operation)
    _need(
        type(host) is publication._HostGitLease and host is scope.host,
        "source-owned publication HostGit required",
    )
    host.check()
    _need(
        operation["action"] == ACTION
        and operation["binding"] == scope.binding_data
        and operation["finite_frontier"] == scope.frontier,
        "publication action/admission differs",
    )
    _need(
        time.time() < operation["deadline"] <= scope.admission["expires_at"],
        "original IO deadline expired",
    )
    controller._full_writer_validation(operation, custody)
    return scope


def _reopen_or_prepare_staging(scope, custody, host):
    handoff = scope.history["handoff"]
    prior = scope.history["baseline"].get("existing_objects")
    if scope.policy["schema"] == publication.ANCHOR_POLICY_SCHEMA:
        _need(type(prior) is dict, "retained original object fingerprint unavailable")
    names = set(os.listdir(custody.fd))
    stage_name = "publication-staging-" + scope.slot
    marker_name = "publication-staging-intent-" + scope.slot + ".json"
    if handoff["staging_link_name"] in names:
        link = custody.read(handoff["staging_link_name"])
        publication._exact(
            link,
            {
                "schema",
                "handoff_reference",
                "staging_reference",
                "stable_slot",
                *publication._AUTHORITY,
            },
        )
        reference = link["staging_reference"]
        publication._exact(reference, {"schema", "name", "sha256"})
        stage = custody.read(reference["name"])
        _need(
            link["schema"] == "closer-publication-source-staging-link/v1"
            and publication._bytes(link["handoff_reference"])
            == publication._bytes(scope.handoff_reference)
            and link["stable_slot"] == scope.slot
            and reference["schema"] == "closer-publication-source-staging-reference/v1"
            and reference["name"] == "publication-source-staging-" + reference["sha256"] + ".json"
            and publication.digest(stage) == reference["sha256"]
            and stage["schema"] == "closer-publication-source-staging/v2"
            and publication._bytes(stage["handoff_reference"])
            == publication._bytes(scope.handoff_reference)
            and stage["operation_sha256"] == handoff["operation_sha256"]
            and stage["admission_sha256"] == ctl.digest(scope.original_record)
            and stage["publication_policy_sha256"] == publication.digest(scope.policy)
            and stage["candidate_sha256"] == publication.digest(handoff["candidate"])
            and all(
                record[key] is False for record in (stage, link) for key in publication._AUTHORITY
            ),
            "retained staging link/source record changed",
        )
        marker = custody.read(marker_name)
        data = publication._StagingData(custody.path / stage_name, marker)
        try:
            if scope.policy["schema"] == publication.ANCHOR_POLICY_SCHEMA:
                data.object_anchor = publication._source_snapshot_seed(
                    host, scope.history["baseline"]["baseline"], prior=prior
                )
            _need(
                publication._bytes(data.check()) == publication._bytes(stage["private_files"]),
                "retained staging physical binding changed",
            )
            return data
        except BaseException:
            data.close()
            raise
    # A partial old preparation is not a fresh preparation permission.
    _need(
        stage_name not in names and marker_name not in names,
        "partial staging already owned; reconcile without recreating",
    )
    common = Path(host.summary["topology"][1])
    config_file = next(
        (
            item
            for item in host.held
            if type(item) is publication._PhysicalFile and item.path == common / "config"
        ),
        None,
    )
    _need(config_file is not None, "held source-approved Git configuration absent")
    config_bytes = publication._held_file_bytes(config_file)
    hooks = {}
    for path in sorted((common / "hooks").iterdir()):
        _need(path.name.endswith(".sample"), "active hooks unsupported")
        item = publication._PhysicalFile(path, scope.policy["max_file_bytes"])
        try:
            hooks[path.name] = publication._held_file_bytes(item)
        finally:
            item.close()
    objects = publication._source_snapshot_seed(
        host, scope.history["baseline"]["baseline"], prior=prior
    )
    scope.check()
    return publication._build_staging_data(
        custody,
        scope.slot,
        scope.history["baseline"]["baseline"],
        handoff["candidate"],
        host.pr,
        scope.policy,
        objects,
        config_bytes,
        hooks,
    )


def prepare_backend(controller, custody, operation, selection, host):
    caller = inspect.currentframe().f_back
    _need(
        caller.f_code is publication._prepare_mutation_backend.__code__
        and caller.f_globals is publication.__dict__,
        "actual fixed staging preparation required",
    )
    scope = require_io_admission(controller, custody, operation, selection, host)
    data = _reopen_or_prepare_staging(scope, custody, host)
    try:
        marker = data.marker
        _need(
            marker["slot_key"] == scope.slot
            and marker["baseline_sha256"]
            == publication.digest(scope.history["baseline"]["baseline"])
            and marker["candidate_sha256"]
            == publication.digest(scope.history["handoff"]["candidate"])
            and marker["head_oid"] == host.pr["head_oid"],
            "private staging baseline/candidate differs",
        )
        for relative, expected in (
            ("repo.git/HEAD", "ref: refs/heads/" + host.pr["head_ref"] + "\n"),
            ("repo.git/refs/heads/" + host.pr["head_ref"], host.pr["head_oid"] + "\n"),
        ):
            item = publication._PhysicalFile(data.path / relative, 4096)
            try:
                _need(item.raw() == expected.encode(), "private initial Git ref changed")
            finally:
                item.close()
        # Unknown/partial command history cannot be reissued even with same H.
        _need(
            not any(
                name.startswith("publication-step-" + scope.slot) for name in os.listdir(custody.fd)
            ),
            "publication command history already owned",
        )
        scope.staging = data
        stage_record = {
            "schema": "closer-publication-io-staging/v1",
            "admission_sha256": publication.digest(scope.admission),
            "operation_sha256": hashlib.sha256(queue._canonical_bytes(operation)).hexdigest(),
            "handoff_reference": scope.handoff_reference,
            "files": data.check(),
            **{key: False for key in publication._AUTHORITY},
        }
        name = "publication-io-staging-" + publication.digest(stage_record) + ".json"
        _need(custody.create(name, stage_record), "IO staging already issued")
        _need(
            publication._bytes(custody.read(name)) == publication._bytes(stage_record),
            "IO staging readback differs",
        )
        return {"intent": selection["intent"], "baseline": scope.history["baseline"]["baseline"]}
    except BaseException:
        data.close()
        raise


def selected_transport(controller, custody, operation, selection, host):
    caller = inspect.currentframe().f_back
    _need(
        caller.f_code is publication._selected_transport.__code__
        and caller.f_globals is publication.__dict__,
        "actual source transport port required",
    )
    scope = require_io_admission(controller, custody, operation, selection, host)
    phase = _find_frame(publication._backend_phase_transport.__code__, publication.__dict__)
    _need(
        all(
            phase.f_locals.get(k) is v
            for k, v in {
                "controller": controller,
                "custody": custody,
                "operation": operation,
                "selection": selection,
                "host": host,
            }.items()
        ),
        "publication command crossed writer/frame",
    )
    reference = phase.f_locals["step_reference"]
    intent = custody.read(reference["name"])
    _need(
        publication.digest(intent) == reference["sha256"]
        and intent["command"] == phase.f_locals["command"]
        and intent["operation_sha256"] == publication.digest(operation)
        and intent["intent_sha256"] == publication.digest(selection["intent"])
        and publication._bytes(intent["stdin"])
        == publication._bytes(publication._stdin_identity(phase.f_locals["payload"])),
        "durable command descriptor changed",
    )
    data = scope.staging
    data.check()
    current_pr = publication._native_owned_pr(scope.observed)
    _need(
        publication._bytes(current_pr) == publication._bytes(selection["intent"]["pr"]),
        "current native PR owner/head changed before effect",
    )
    environment = dict(host.environment)
    environment.update(
        {
            "GIT_DIR": str(data.path / "repo.git"),
            "GIT_WORK_TREE": str(data.path / "worktree"),
            "GIT_INDEX_FILE": str(data.path / "repo.git/index"),
            "GIT_OBJECT_DIRECTORY": str(data.path / "repo.git/objects"),
        }
    )
    if scope.policy["schema"] == publication.ANCHOR_POLICY_SCHEMA:
        _need(
            type(data.object_anchor) is publication._ExistingObjects
            and data.object_anchor is host.object_anchor,
            "source-held original object directory unavailable",
        )
        data.object_anchor.check()
        environment.update(
            GIT_OBJECT_DIRECTORY=str(data.object_anchor.path),
            GIT_NO_LAZY_FETCH="1",
            GIT_NO_REPLACE_OBJECTS="1",
            TMPDIR=str(data.path / "native-tmp"),
        )
    return {
        "policy": scope.policy,
        "argv": phase.f_locals["command"],
        "stdin": phase.f_locals["payload"],
        "env": environment,
        "cwd": str(data.path / "worktree"),
    }


def close_selection(selection, host):
    # Descriptor cleanup must still run if the request expires after its known
    # native result. Closing our exact objects grants no new execution window.
    scope = getattr(_CURRENT, "scope", None)
    caller = inspect.currentframe().f_back
    _need(
        caller.f_code is publication.execute_existing_update.__code__
        and caller.f_globals is publication.__dict__
        and type(scope) is _PublicationScope
        and scope.seal is _SEAL
        and threading.get_ident() == scope.thread
        and scope.selection is selection
        and scope.host is host,
        "actual source cleanup objects required",
    )
    try:
        if getattr(scope, "staging", None) is not None:
            scope.staging.close()
    finally:
        host.close()


def retained_materialization(controller, custody, operation, record):
    scope = getattr(_CURRENT, "scope", None)
    if scope is None:
        return None
    caller = inspect.currentframe().f_back
    _need(
        caller.f_code is ctl.IndexController.authenticate.__code__
        and caller.f_globals is ctl.__dict__,
        "actual controller publication history reader required",
    )
    scope.check()
    _need(
        scope.controller is controller
        and scope.custody is custody
        and scope.operation is operation
        and publication._bytes(record) == publication._bytes(scope.original_record),
        "publication cannot adopt another original decision",
    )
    controller._require_writer(operation, custody)
    import closer_correction_decision as decisions

    return decisions.read_materialized_selected(**record["materializer"])


def historical_window(proof):
    scope = getattr(_CURRENT, "scope", None)
    if scope is None:
        return None
    import closer_correction_source_record as source

    caller = inspect.currentframe().f_back
    _need(
        caller.f_code is source.retained_provider_time.__code__
        and caller.f_globals is source.__dict__,
        "actual retained provider-time reader required",
    )
    scope.check()
    _need(
        scope.callback_frame is not None
        and publication._bytes(proof)
        == publication._bytes(scope.original_record["materializer"]["proof"]),
        "publication original classifier proof changed",
    )
    return publication._copy(
        scope.history["original_history"]["terminal"]["native_execution"]["phases"]["B"]
    )


def _ref_suffix(pr):
    from urllib.parse import quote

    return "/git/ref/heads/" + quote(pr["head_ref"], safe="")


def allow_native_ref_read(controller, custody, operation, method, suffix, body):
    """One source-selected corrective ref, never a new generic GET allowlist."""
    import closer_correction_native_effects as effects

    scope = current_scope()
    _selection_check(scope.selection, controller, custody)
    request_frame = inspect.currentframe().f_back
    invocation = getattr(queue._callback_threads, "invocation", None)
    _need(
        request_frame.f_code is effects.NativeHTTPTransport.request.__code__
        and request_frame.f_globals is effects.__dict__
        and type(invocation) is dict
        and operation is invocation["operation"]
        and queue._canonical_bytes(operation) == queue._canonical_bytes(scope.operation),
        "actual native transport original invocation required",
    )
    frame = _find_frame(native_readback.__code__, globals())
    return (
        frame.f_locals.get("controller") is controller
        and frame.f_locals.get("operation") is scope.operation
        and method == "GET"
        and body is None
        and suffix == _ref_suffix(scope.selection["intent"]["pr"])
    )


def native_readback(controller, custody, operation, selection, host, commit):
    caller = inspect.currentframe().f_back
    _need(
        caller.f_code is publication._native_publication_readback.__code__
        and caller.f_globals is publication.__dict__,
        "actual backend native readback required",
    )
    scope = require_io_admission(controller, custody, operation, selection, host)
    ledger = publication.ContractLedger(custody).read(scope.slot)
    _need(ledger["state"] == "push_ack", "native readback requires original push ACK")
    import closer_correction_native_effects as effects

    effects._native_writer(controller, operation)
    controller.store.repository()
    pr = publication._native_owned_pr(scope.observed)
    native_ref = controller.store.call("GET", _ref_suffix(pr))
    native_commit = controller.store.call("GET", "/git/commits/" + commit["commit_oid"])
    ref = {
        "repository_id": scope.config.repository_id,
        "ref": native_ref.get("ref"),
        "oid": native_ref.get("object", {}).get("sha"),
        "object_type": native_ref.get("object", {}).get("type"),
    }
    observed_commit = {
        "repository_id": scope.config.repository_id,
        "oid": native_commit.get("sha"),
        "tree_oid": native_commit.get("tree", {}).get("sha"),
        "parent_oids": [p.get("sha") for p in native_commit.get("parents", [])],
    }
    readback = publication.validate_readback_data(
        selection["intent"], commit, pr, ref, observed_commit
    )
    scope.native_readback = publication._copy(readback)
    return pr, ref, observed_commit


def _origin_record(scope, operation, ledger):
    ack = next(e["payload"] for e in ledger["events"] if e["kind"] == "push_ack")
    return {
        "schema": "closer-native-owned-pr-publication-origin/v1",
        "original": scope.original_identity,
        "request_sha256": ctl.digest(scope.original_record["request"]),
        "original_admission_sha256": ctl.digest(scope.original_record),
        "handoff_reference": scope.handoff_reference,
        "coding_terminal_reference": scope.history["terminal_reference"],
        "io_admission_sha256": publication.digest(scope.admission_record),
        "io_operation_sha256": hashlib.sha256(queue._canonical_bytes(operation)).hexdigest(),
        "intent_sha256": publication.digest(ledger["intent"]),
        "push_ack_sha256": publication.digest(ack),
        "readback": scope.native_readback,
        "source_closure_sha256": ctl.digest(scope.qualified_value["source_closure"]),
        **{key: False for key in publication._AUTHORITY},
    }


def validate_index_bind(controller, custody, operation, entry, action):
    import hmac

    import closer_correction_native_effects as effects
    import closer_native_observer as native

    caller = inspect.currentframe().f_back
    _need(
        caller.f_code is effects.validate_index_effect_action.__code__
        and caller.f_globals is effects.__dict__,
        "actual canonical native bind validator required",
    )
    scope = _selection_check(current_scope().selection, controller, custody, operation)
    _need(
        action.get("kind") == "bind"
        and action.get("key") == scope.original_identity["effect_key"]
        and publication._bytes(entry["request"])
        == publication._bytes(scope.original_record["request"])
        and publication._bytes(action["selector"])
        == publication._bytes(scope.history["admitted_selector"])
        and action["proof_sha256"] == scope.origin_reference["sha256"],
        "publication bind crossed original source scope",
    )
    ledger = publication.ContractLedger(custody).read(scope.slot)
    _need(ledger["state"] == "readback", "publication native result not known")
    record = custody.read(scope.origin_reference["name"])
    signature = custody.read(
        "native-publication-origin-signature-" + scope.origin_reference["sha256"] + ".json"
    )
    _need(
        ctl.digest(record) == scope.origin_reference["sha256"]
        and publication._bytes(record)
        == publication._bytes(_origin_record(scope, operation, ledger))
        and signature.get("binding") == native.binding(scope.manifest)
        and hmac.compare_digest(
            signature.get("signature", ""),
            native.mechanisms._signature(record, native._existing_lifecycle_key(scope.native_root)),
        ),
        "source-owned publication origin changed",
    )
    pr = record["readback"]["pr"]
    _need(
        action["subject"]
        == {"kind": "pull", "number": str(pr["pr_number"]), "node_id": pr["pr_node_id"]},
        "native published PR subject differs",
    )
    effects._native_writer(controller, operation)
    _need(
        publication._bytes(publication._native_owned_pr(scope.observed)) == publication._bytes(pr),
        "native published owner/head changed before canonical bind",
    )
    return True


def retain_confirmed_result(controller, custody, operation, selection, host, receipt):
    import closer_native_observer as native

    caller = inspect.currentframe().f_back
    _need(
        caller.f_code is publication.execute_existing_update.__code__
        and caller.f_globals is publication.__dict__
        and caller.f_locals.get("receipt") is receipt,
        "actual publication body result required",
    )
    scope = require_io_admission(controller, custody, operation, selection, host)
    ledger = publication.ContractLedger(custody).read(scope.slot)
    _need(
        ledger["state"] == "readback"
        and receipt["contract_consistent"] is True
        and receipt["readback_sha256"] == publication.digest(scope.native_readback),
        "publication ACK/readback custody incomplete",
    )
    origin = _origin_record(scope, operation, ledger)
    origin_sha = ctl.digest(origin)
    scope.origin_reference = {
        "schema": "closer-native-owned-pr-publication-origin-reference/v1",
        "name": "native-publication-origin-" + origin_sha + ".json",
        "sha256": origin_sha,
    }
    _need(
        custody.create(scope.origin_reference["name"], origin),
        "native publication origin already owned",
    )
    _need(
        custody.create(
            "native-publication-origin-signature-" + origin_sha + ".json",
            {
                "binding": native.binding(scope.manifest),
                "signature": native.mechanisms._signature(
                    origin, native._existing_lifecycle_key(scope.native_root)
                ),
            },
        ),
        "publication origin signature already owned",
    )
    pr = scope.native_readback["pr"]
    subject = {"kind": "pull", "number": str(pr["pr_number"]), "node_id": pr["pr_node_id"]}
    bind = {
        "kind": "bind",
        "key": scope.original_identity["effect_key"],
        "subject": subject,
        "selector": scope.history["admitted_selector"],
        "proof_sha256": origin_sha,
    }
    outcome = controller._apply_locked(bind, operation=operation, custody=custody)
    _need(
        outcome.get("status") == "checkpoint_confirmed",
        "publication index bind outcome remains unknown; never repeat push",
    )
    indexed = controller.read_bound(custody)
    entry = next((e for e in indexed["data"]["entries"] if e["key"] == bind["key"]), None)
    binding = {key: bind[key] for key in ("subject", "selector", "proof_sha256")}
    _need(
        entry is not None
        and any(publication._bytes(b) == publication._bytes(binding) for b in entry["bindings"]),
        "native publication canonical bind missing",
    )
    incumbent = scope.original_record["request"]["incumbent"]["issue"]
    provenance = {
        "handoff_reference": scope.handoff_reference,
        "coding_terminal_reference": scope.history["terminal_reference"],
        "publication_origin_reference": scope.origin_reference,
        "io_admission_reference": {
            "schema": "closer-publication-io-admission-reference/v1",
            "name": scope.admission_name,
            "sha256": publication.digest(scope.admission_record),
        },
        "io_operation_sha256": origin["io_operation_sha256"],
        "push_ack_sha256": origin["push_ack_sha256"],
        "native_readback_sha256": publication.digest(scope.native_readback),
        "index_binding_sha256": ctl.digest(binding),
        "original_admission_sha256": ctl.digest(scope.original_record),
        "original_operation_sha256": scope.history["handoff"]["operation_sha256"],
        "source_closure_sha256": ctl.digest(scope.qualified_value["source_closure"]),
        "scope_issue": {k: incumbent[k] for k in ("number", "node_id", "body_sha256")},
    }
    record = {
        "schema": RESULT_SCHEMA,
        "request": scope.original_record["request"],
        "original": scope.original_identity,
        "findings": scope.original_record["materializer"]["raw_findings"],
        "required_tests": scope.original_record["request"]["required_tests"],
        **{k: scope.native_readback[k] for k in ("pr", "commit", "ref")},
        "provenance": provenance,
        **{key: False for key in publication._AUTHORITY},
    }
    sha = publication.digest(record)
    reference = {
        "schema": RESULT_REFERENCE_SCHEMA,
        "name": "publication-result-" + sha + ".json",
        "sha256": sha,
    }
    _need(custody.create(reference["name"], record), "native publication result already retained")
    _need(
        publication._bytes(custody.read(reference["name"])) == publication._bytes(record),
        "native publication result readback changed",
    )
    return {
        "publication_result_reference": reference,
        **{key: False for key in publication._AUTHORITY},
    }


def admission_custody_data(signed):
    """Necessary storage envelope; finite clocks remain in their original domain.

    The native HMAC is not minted or verified here. No caller DATA can issue an
    admission. This merely preserves its exact existing finite-float bytes in
    the integer-only private custody grammar.
    """
    publication._exact(signed, {"record", "signature"})
    publication._exact(signed["record"], {"binding", "context"})
    context = signed["record"]["context"]
    _need(
        type(signed["record"]["binding"]) is dict
        and type(context) is dict
        and context.get("schema") == ADMISSION_SCHEMA
        and context.get("purpose") == ACTION
        and publication._sha(signed["signature"]),
        "closed native IO envelope required",
    )
    raw = queue._canonical_bytes(signed)
    _need(0 < len(raw) <= 65536, "native IO admission storage bound")
    return {
        "schema": "closer-publication-io-admission-custody/v1",
        "signed_json": raw.decode("utf-8"),
    }


def read_admission_custody_data(record):
    import json

    publication._exact(record, {"schema", "signed_json"})
    _need(
        record["schema"] == "closer-publication-io-admission-custody/v1"
        and type(record["signed_json"]) is str
        and 0 < len(record["signed_json"].encode("utf-8")) <= 65536,
        "private native IO admission storage differs",
    )
    signed = json.loads(record["signed_json"])
    expected = admission_custody_data(signed)
    _need(
        publication._bytes(expected) == publication._bytes(record),
        "native IO admission bytes are not canonical",
    )
    return signed


class _AdmittedPublicationView:
    def __init__(self, *_args, **_kwargs):
        raise TypeError("admitted publication view is source-issued only")

    def read(self):
        import closer_publication_verification as verification

        caller = inspect.currentframe().f_back
        _need(
            caller.f_code is verification.attach_existing_root.__code__
            and caller.f_globals is verification.__dict__
            and caller.f_locals.get("view") is self
            and self.thread == threading.get_ident()
            and self.pid == os.getpid()
            and not self.used,
            "actual one-shot admitted publication consumer required",
        )
        _reader_frame(self.backend, self.target, self.snapshot, self.operation, self.context)
        self.used = True
        return publication._copy(self.data)


def _nested_codes(code):
    import types

    yield code
    for value in code.co_consts:
        if isinstance(value, types.CodeType):
            yield from _nested_codes(value)


def _reader_frame(backend, target, snapshot, operation, pipeline_context):
    """The original NEW ACT queue frame, never the completed IO frame."""
    import closer_dispatcher as dispatcher

    _need(type(backend) is dispatcher.Closer, "actual native backend required")
    _need(queue._currently_invoking(target, operation), "new admitted callback required")
    codes = {
        code
        for code in _nested_codes(dispatcher._run_staged_pipeline.__code__)
        if code.co_name == "execute_bound"
    }
    frame = inspect.currentframe().f_back
    found = None
    while frame is not None:
        if frame.f_code in codes and frame.f_globals is dispatcher.__dict__:
            found = frame
            break
        frame = frame.f_back
    _need(
        found is not None
        and found.f_locals.get("current") is backend
        and found.f_locals.get("context") is pipeline_context
        and found.f_locals.get("name") == target
        and found.f_locals.get("observed") is snapshot
        and found.f_locals.get("operation") is operation,
        "original source execute_bound frame differs",
    )
    return found


def _consumer_source(installation):
    import closer_publication_verification as verification

    for module, loaded in (
        (sys.modules[__name__], _SOURCE_SHA256),
        (publication, publication._LOADED_SOURCE_SHA256),
        (verification, verification._LOADED_SOURCE_SHA256),
    ):
        path = Path(module.__file__)
        _need(
            installation.source_closure.get(path.name) == {"path": str(path), "sha256": loaded}
            and hashlib.sha256(path.read_bytes()).hexdigest() == loaded,
            "admitted publication consumer source changed",
        )


def _result_for_subject(custody, repository, number):
    """Bounded source custody discovery; no caller-selected pathname."""
    import re

    names = os.listdir(custody.fd)
    _need(len(names) <= 8192, "publication custody discovery bound")
    results = []
    for name in sorted(names):
        match = re.fullmatch(r"publication-result-([a-f0-9]{64})\.json", name)
        if match is None:
            continue
        record = custody.read(name)
        _need(publication.digest(record) == match[1], "publication result custody changed")
        pr = record.get("pr", {})
        if pr.get("repository") == repository and str(pr.get("pr_number")) == number:
            _need(record.get("schema") == RESULT_SCHEMA, "publication result schema changed")
            results.append((name, match[1], record))
    _need(len(results) <= 1, "multiple corrective publications need explicit source selection")
    return results[0] if results else None


def _confirmed_knowledge_data(controller, custody, operation, selected, manifest):
    """Authenticate durable local/native knowledge without repeating an effect."""
    import hmac

    import closer_native_observer as native

    name, sha, record = selected
    provenance, original = record["provenance"], record["original"]
    _need(
        all(record[k] is False for k in publication._AUTHORITY)
        and original["repository_id"] == controller.config.repository_id
        and original["epoch"] == controller.config.epoch
        and original["effect_key"] == ctl.effect_key(record["request"])
        and original["request_sha256"] == ctl.digest(record["request"]),
        "publication original work changed",
    )
    origin_ref = provenance["publication_origin_reference"]
    _need(
        origin_ref["name"] == "native-publication-origin-" + origin_ref["sha256"] + ".json",
        "native publication origin selector differs",
    )
    origin = custody.read(origin_ref["name"])
    signature = custody.read(
        "native-publication-origin-signature-" + origin_ref["sha256"] + ".json"
    )
    _need(
        ctl.digest(origin) == origin_ref["sha256"]
        and signature.get("binding") == native.binding(manifest)
        and hmac.compare_digest(
            signature.get("signature", ""),
            native.mechanisms._signature(
                origin, native._existing_lifecycle_key(native.contract(manifest)[3])
            ),
        )
        and origin["original"] == original
        and origin["request_sha256"] == ctl.digest(record["request"])
        and origin["readback"]
        == {
            "schema": "closer-owned-pr-readback-data/v1",
            **{k: record[k] for k in ("pr", "commit", "ref")},
            "native_publication_authorized": False,
        }
        and all(origin[k] is False for k in publication._AUTHORITY),
        "authenticated native publication origin changed",
    )
    for key in (
        "original_admission_sha256",
        "io_operation_sha256",
        "push_ack_sha256",
        "source_closure_sha256",
    ):
        _need(origin[key] == provenance[key], "publication origin provenance changed")
    ledger = publication.ContractLedger(custody).read(publication.stable_slot(original))
    _need(
        ledger["state"] == "readback"
        and publication.digest(ledger["intent"]) == origin["intent_sha256"],
        "publication result lacks known native readback",
    )
    current = controller.read_bound(custody)
    subject = {
        "kind": "pull",
        "number": str(record["pr"]["pr_number"]),
        "node_id": record["pr"]["pr_node_id"],
    }
    entry = next(
        (row for row in current["data"]["entries"] if row["key"] == original["effect_key"]), None
    )
    _need(
        entry is not None and entry["request"] == record["request"],
        "original canonical index entry changed",
    )
    bindings = [
        row
        for row in entry["bindings"]
        if row["subject"] == subject and row["proof_sha256"] == origin_ref["sha256"]
    ]
    _need(
        len(bindings) == 1 and ctl.digest(bindings[0]) == provenance["index_binding_sha256"],
        "canonical native publication bind changed",
    )
    admission = custody.read("admission-" + provenance["original_admission_sha256"] + ".json")
    _need(
        ctl.digest(admission) == provenance["original_admission_sha256"]
        and admission["request"] == record["request"]
        and admission["materializer"]["raw_findings"] == record["findings"]
        and record["required_tests"] == record["request"]["required_tests"],
        "original publication instruction scope changed",
    )
    own = admission["own_instruction"]
    intent = custody.read("native-intent-" + own["phase_key"] + ".json")
    instruction_origin = custody.read("native-origin-" + own["proof_sha256"] + ".json")
    controller._full_writer_validation(operation, custody)
    value = publication._copy(record)
    reference = {"schema": RESULT_REFERENCE_SCHEMA, "name": name, "sha256": sha}
    value["publication_result_reference"] = reference
    value["provenance"]["publication_result_reference"] = reference
    return {
        "schema": "closer-admitted-publication-knowledge/v1",
        "result": value,
        "controller_config": {
            "repository": controller.config.repository,
            "repository_id": controller.config.repository_id,
            "epoch": controller.config.epoch,
            "consumers": sorted(controller.config.consumers),
        },
        "original_admission": admission,
        "instruction_intent": intent,
        "instruction_origin": instruction_origin,
    }


def read_for_existing_root(backend, target, snapshot, operation, pipeline_context):
    import closer_campaign as campaign
    import closer_publication_verification as verification
    import closer_source_installation as installed

    caller = inspect.currentframe().f_back.f_back
    _need(
        caller.f_code is verification.attach_existing_root.__code__
        and caller.f_globals is verification.__dict__,
        "actual existing-root verification consumer required",
    )
    _reader_frame(backend, target, snapshot, operation, pipeline_context)
    installation = installed.source_installation_for(backend.runtime_binding)
    _need(installation is not None, "admitted publication installation unavailable")
    _consumer_source(installation)
    manifest_path = Path(installation.manifest_path)
    _need(
        hashlib.sha256(manifest_path.read_bytes()).hexdigest() == installation.manifest_sha256,
        "publication manifest changed",
    )
    manifest = campaign.load_manifest(manifest_path)
    controller = ctl.IndexController(
        ctl.ControllerConfig(**installation.config), installation=installation
    )
    repository, separator, number = target.rpartition("#")
    _need(
        separator and repository == controller.config.repository and number.isdecimal(),
        "publication target differs",
    )
    with controller.fenced(operation) as custody:
        selected = _result_for_subject(custody, repository, number)
        if selected is None:
            return None
        data = _confirmed_knowledge_data(controller, custody, operation, selected, manifest)
        pr = data["result"]["pr"]
        native_pr = controller.store.call("GET", "/pulls/" + number)
        _need(
            native_pr.get("id") is not None
            and native_pr.get("node_id") == pr["pr_node_id"]
            and native_pr.get("head", {}).get("sha") == pr["head_oid"] == snapshot.get("head")
            and native_pr.get("head", {}).get("ref") == pr["head_ref"]
            and native_pr.get("head", {}).get("repo", {}).get("id") == pr["repository_id"]
            and native_pr.get("base", {}).get("repo", {}).get("id") == pr["repository_id"]
            and native_pr.get("base", {}).get("ref") == pr["base_ref"]
            and native_pr.get("draft") is False
            and type(native_pr.get("body")) is str
            and hashlib.sha256(native_pr["body"].encode()).hexdigest() == pr["body_sha256"]
            and native_pr.get("user", {}).get("id") == pr["owner_id"]
            and native_pr.get("user", {}).get("login") == pr["owner_login"]
            and native_pr.get("state") in {"open", "closed"}
            and (native_pr.get("state") == "open" or native_pr.get("merged") is True),
            "current native corrective PR owner/head changed",
        )
        controller._full_writer_validation(operation, custody)
    view = object.__new__(_AdmittedPublicationView)
    view.backend, view.target, view.snapshot, view.operation, view.context = (
        backend,
        target,
        snapshot,
        operation,
        pipeline_context,
    )
    view.pid, view.thread, view.used, view.data = os.getpid(), threading.get_ident(), False, data
    return view


def discovery_hint(backend, target, pipeline_context):
    """Protected planning DATA only; ACT must reissue knowledge in its NEW root."""
    import hmac

    import closer_campaign as campaign
    import closer_dispatcher as dispatcher
    import closer_native_observer as native
    import closer_publication_verification as verification
    import closer_source_installation as installed

    caller = inspect.currentframe().f_back.f_back
    _need(
        caller.f_code is verification.observe_existing_root.__code__
        and caller.f_globals is verification.__dict__
        and caller.f_locals.get("backend") is backend
        and caller.f_locals.get("target") == target
        and caller.f_locals.get("pipeline_context") is pipeline_context
        and type(backend) is dispatcher.Closer,
        "actual existing-root observation consumer required",
    )
    installation = installed.source_installation_for(backend.runtime_binding)
    if installation is None:
        return None
    _consumer_source(installation)
    repository, separator, number = target.rpartition("#")
    _need(
        separator and repository == installation.config["repository"] and number.isdecimal(),
        "publication observation subject differs",
    )
    path = Path(installation.manifest_path)
    _need(
        hashlib.sha256(path.read_bytes()).hexdigest() == installation.manifest_sha256,
        "publication observation manifest changed",
    )
    manifest = campaign.load_manifest(path)
    custody = ctl._Custody(ctl.ControllerConfig(**installation.config))
    try:
        selected = _result_for_subject(custody, repository, number)
        if selected is None:
            return None
        name, sha, record = selected
        origin_ref = record["provenance"]["publication_origin_reference"]
        _need(
            origin_ref["name"] == "native-publication-origin-" + origin_ref["sha256"] + ".json",
            "publication observation origin selector differs",
        )
        origin = custody.read(origin_ref["name"])
        signature = custody.read(
            "native-publication-origin-signature-" + origin_ref["sha256"] + ".json"
        )
        _need(
            ctl.digest(origin) == origin_ref["sha256"]
            and signature.get("binding") == native.binding(manifest)
            and hmac.compare_digest(
                signature.get("signature", ""),
                native.mechanisms._signature(
                    origin, native._existing_lifecycle_key(native.contract(manifest)[3])
                ),
            )
            and origin["original"] == record["original"]
            and origin["request_sha256"] == ctl.digest(record["request"])
            and origin["readback"]
            == {
                "schema": "closer-owned-pr-readback-data/v1",
                **{k: record[k] for k in ("pr", "commit", "ref")},
                "native_publication_authorized": False,
            }
            and all(record[k] is False and origin[k] is False for k in publication._AUTHORITY)
            and record["required_tests"] == record["request"]["required_tests"],
            "publication observation native origin changed",
        )
        import closer_correction_handoff as wire

        return {
            "schema": "closer-corrective-publication-selection/v1",
            "publication_result_reference": {
                "schema": RESULT_REFERENCE_SCHEMA,
                "name": name,
                "sha256": sha,
            },
            "request_sha256": record["request"]["request_sha256"],
            "required_tests_sha256": wire.digest(record["required_tests"]),
        }
    finally:
        custody.close()
