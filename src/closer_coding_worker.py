"""One prospective coding operation under an ordinarily admitted native WORKER.

The pinned source purpose selects this path. It is not a JSON execution permit.
The separate accepted coding worker has its own signed purpose and scope.
Installed native account, route and confinement readers remain unsupported.
"""

from __future__ import annotations

import contextlib
import copy
import hashlib
import inspect
import json
import math
import os
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

PURPOSE_SCHEMA = "closer-coding-worker-purpose/v1"
PURPOSE_FIELD = "correction_coding_worker"
ACTION = "correction_coding"
TERMINAL_SCHEMA = "closer-coding-worker-terminal/v1"
_SEAL = object()
_CURRENT = threading.local()
_LOADED_SOURCE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


class WorkerHeld(ValueError):
    pass


def _need(value, reason):
    if not value:
        raise WorkerHeld(reason)


def validate_purpose(value):
    import closer_correction_controller as ctl

    _need(
        type(value) is dict
        and set(value) == {"schema", "consumer_id", "target", "which", "selected", "expected"},
        "exact prospective coding purpose required",
    )
    _need(value["schema"] == PURPOSE_SCHEMA, "unsupported coding worker purpose")
    _need(type(value["consumer_id"]) is str and value["consumer_id"], "consumer missing")
    ctl.subject(value["which"])
    _need(value["which"]["kind"] == "issue", "coding requires incumbent issue")
    _need(
        type(value["target"]) is str and value["target"].endswith("#" + value["which"]["number"]),
        "coding target differs",
    )
    ctl.exact(value["expected"], "key request_sha256")
    _need(
        all(type(v) is str and ctl._SHA.fullmatch(v) for v in value["expected"].values()),
        "original correction identity missing",
    )
    ctl.selector(value["selected"], value["target"].split("#", 1)[0])
    return copy.deepcopy(value)


def _canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()


def _native_reader(name):
    import closer_native_observer as native

    reader = getattr(native, name, None)
    _need(
        callable(reader)
        and getattr(reader, "__globals__", None) is native.__dict__
        and getattr(reader, "__code__", None) is not None,
        "fixed accepted coding native reader unavailable",
    )
    return reader


def validate_accepted_purpose(value):
    """Necessary envelope integrity only; only the actual native reader issues it."""
    import closer_coding_continuation as continuation
    import closer_correction_controller as ctl

    _need(
        type(value) is dict
        and set(value)
        == {
            "schema",
            "continuation_reference",
            "purpose",
            "worker_id",
            "admission_sha256",
            "native_b_start",
        },
        "exact accepted coding purpose required",
    )
    _need(
        value["schema"] == "closer-accepted-coding-worker-purpose/v1",
        "accepted coding purpose schema differs",
    )
    ref = continuation.validate_reference(value["continuation_reference"])
    purpose = validate_purpose(value["purpose"])
    _need(
        purpose["consumer_id"] == ref["consumer_id"]
        and purpose["expected"]
        == {"key": ref["effect_key"], "request_sha256": ref["request_sha256"]},
        "accepted original correction identity differs",
    )
    _need(
        type(value["worker_id"]) is str
        and value["worker_id"]
        and value["worker_id"] == value["worker_id"].strip()
        and type(value["admission_sha256"]) is str
        and ctl._SHA.fullmatch(value["admission_sha256"]),
        "accepted fresh native identity missing",
    )
    start = value["native_b_start"]
    _need(
        type(start) is dict
        and set(start) == {"path", "sha256"}
        and type(start["path"]) is str
        and Path(start["path"]).is_absolute()
        and type(start["sha256"]) is str
        and ctl._SHA.fullmatch(start["sha256"]),
        "accepted fresh B-start reference differs",
    )
    return copy.deepcopy(value)


def request_seconds(timeout, *, remaining, session_ms):
    _need(
        type(timeout) in (int, float) and math.isfinite(timeout) and 0 < timeout <= 120,
        "signed request timeout missing or unsupported",
    )
    _need(
        type(remaining) in (int, float) and math.isfinite(remaining) and remaining > 0,
        "actual new callback expired",
    )
    _need(type(session_ms) is int and session_ms > 0, "explicit session runtime missing")
    return min(timeout, remaining, session_ms / 1000)


def validate_terminal_observation(value):
    """Necessary local CLI facts only; serialized data never grants admission."""
    import closer_coding_session_v2 as session

    fields = {
        "schema",
        "outer_submissions",
        "spawn_observed",
        "cli_completion_known",
        "owned_group_terminal",
        "captured_stdout_bytes",
        "captured_stderr_bytes",
        "local_elapsed_ms",
        "native_model_attempts",
        "native_input_tokens",
        "native_output_tokens",
        "budget",
    }
    _need(
        type(value) is dict
        and set(value) == fields
        and value["schema"] == "closer-coding-session-observation/v2",
        "exact local coding terminal observation required",
    )
    _need(
        type(value["outer_submissions"]) is int
        and value["outer_submissions"] == 1
        and all(
            value[key] is True
            for key in ("spawn_observed", "cli_completion_known", "owned_group_terminal")
        ),
        "local coding process terminality unknown",
    )
    limits = session.validate_budget(value["budget"])
    _need(
        all(
            type(value[key]) is int and value[key] >= 0
            for key in ("captured_stdout_bytes", "captured_stderr_bytes", "local_elapsed_ms")
        ),
        "local coding observation count invalid",
    )
    _need(
        value["captured_stdout_bytes"] + value["captured_stderr_bytes"]
        <= limits["max_captured_output_bytes"],
        "coding captured output exceeded bound",
    )
    _need(
        all(value[key] == "UNKNOWN" and type(value[key]) is str for key in session.UNKNOWN_FIELDS),
        "local terminality cannot assert native usage",
    )
    return copy.deepcopy(value)


@dataclass(frozen=True)
class _Root:
    seal: object
    frame: object
    thread: int
    pid: int
    backend: object
    installation: object
    manifest: dict
    accepted_utf8: bytes
    start_ref: dict

    @property
    def purpose(self):
        return json.loads(self.accepted_utf8)["purpose"]

    @property
    def continuation_reference(self):
        self.check()
        return json.loads(self.accepted_utf8)["continuation_reference"]

    def check(self):
        _need(
            self.seal is _SEAL and self.pid == os.getpid() and self.thread == threading.get_ident(),
            "source worker root unavailable",
        )
        frame = sys._current_frames().get(self.thread)
        while frame is not None and frame is not self.frame:
            frame = frame.f_back
        _need(
            frame is self.frame and frame.f_locals.get("backend") is self.backend,
            "original source worker root ended",
        )
        accepted = validate_accepted_purpose(
            _native_reader("accepted_coding_worker_purpose")(self.installation._binding)
        )
        _need(
            _canonical(accepted) == self.accepted_utf8
            and _canonical(accepted["native_b_start"]) == _canonical(self.start_ref),
            "accepted coding purpose changed",
        )
        import closer_correction_controller as ctl

        # The accepted native reader above already performs the fresh full
        # installation/admission/B-start checks. Read campaign state only after
        # that authentication; do not repeat those same checks beforehand.
        doc = ctl.IndexController._physical_state(
            self.installation.config["state_path"], "current coding campaign redirected"
        )
        _need(
            all(
                w.get("state") == "terminal"
                and w.get("outcome") in {"ok", "unavailable", "scoped_continuation"}
                for w in doc["workers"]
            ),
            "prior worker remains owned or UNKNOWN",
        )


def _issue_root(args, backend, targets):
    import closer_campaign as campaign
    import closer_dispatcher as closer
    import closer_native_observer as native
    import closer_source_installation as installed

    frame = inspect.currentframe().f_back.f_back
    _need(
        frame.f_code is closer._main_admitted.__code__
        and frame.f_globals is closer.__dict__
        and frame.f_locals.get("backend") is backend
        and frame.f_locals.get("args") is args,
        "actual admitted dispatcher WORKER root required",
    )
    _need(
        type(backend) is closer.Closer and backend.offload_fn is None,
        "source coding backend required",
    )
    installation = installed.source_installation_for(backend.runtime_binding)
    _need(installation is not None, "current native source installation unavailable")
    manifest = campaign.load_manifest(installation.manifest_path)
    import closer_coding_continuation as continuation

    capability = continuation.validate_capability(manifest.get("policy", {}).get(PURPOSE_FIELD))
    accepted = validate_accepted_purpose(
        _native_reader("accepted_coding_worker_purpose")(installation._binding)
    )
    purpose = accepted["purpose"]
    _need(
        capability["consumer_id"] == purpose["consumer_id"],
        "accepted coding consumer differs from static capability",
    )
    _need(
        args.apply is True and args.workers is True and not args.shadow,
        "coding requires actual worker execution",
    )
    _need(
        targets == [purpose["target"]] and installation.finite_frontier == args.finite_frontier,
        "source coding assignment/frontier differs",
    )
    _need(
        installation.source_closure.get("closer_coding_worker.py")
        == {"path": str(Path(__file__)), "sha256": _LOADED_SOURCE_SHA256}
        and hashlib.sha256(Path(__file__).read_bytes()).hexdigest() == _LOADED_SOURCE_SHA256,
        "coding worker source outside current closure",
    )
    start = native.source_b_start(installation)
    _need(start is not None, "actual accepted native worker start unavailable")
    return _Root(
        _SEAL,
        frame,
        threading.get_ident(),
        os.getpid(),
        backend,
        installation,
        manifest,
        _canonical(accepted),
        start,
    )


def run_from_root(args, backend, targets):
    """Called only by the existing dispatcher WORKER root, before staged roles."""
    # This is selection only. An ordinary dispatcher with no explicit source
    # coding selection retains its incumbent route and never needs installation.
    if getattr(args, "coding_worker", False) is False:
        import closer_dispatcher as closer

        frame = inspect.currentframe().f_back
        _need(
            frame.f_code is closer._main_admitted.__code__ and frame.f_globals is closer.__dict__,
            "actual admitted dispatcher WORKER root required",
        )
        return None
    root = _issue_root(args, backend, targets)
    if root is None:
        return None
    root.check()
    return _run_queue(root, args)


def _run_queue(root, args):
    import closer_correction_controller as ctl
    import closer_frontier_policy as frontier_policy
    import closer_queue as queue
    import closer_resources as resources

    root.check()
    purpose = root.purpose
    installation = root.installation
    config = ctl.ControllerConfig(**installation.config)
    controller = ctl.IndexController(config, installation=installation)
    _preflight_original(root, controller)
    target = purpose["target"]
    frontier = installation.finite_frontier
    timeout = installation.request_timeout_seconds
    request_seconds(timeout, remaining=timeout, session_ms=math.ceil(timeout * 1000))
    # Correlate this operation with the original source installation. These are
    # ordinary queue bindings, never model-supplied authority.
    binding = {
        "pr_target": target,
        "source_targets": [],
        "correction_controller_sha256": ctl.digest(config.source_binding()),
        "correction_installation_sha256": installation.policy_projection["installation_sha256"],
        "coding_worker_purpose_sha256": ctl.digest(purpose),
        "coding_continuation_sha256": root.continuation_reference["sha256"],
    }
    revision = frontier["bindings"][target]["revision"]
    _need(
        frontier_policy.action_identity(target, revision, ACTION, binding)
        == frontier["bindings"][target]["action_id"],
        "coding frontier action differs",
    )

    def observe(which):
        root.check()
        _need(which == target, "coding queue target differs")
        return {
            "target": target,
            "revision": revision,
            "action": ACTION,
            "eligible": True,
            "requires_coordinator": False,
            "operation_binding": binding,
            "claim_targets": [target],
            "repo_keys": [config.repository],
            "evidence": {},
        }

    def execute(which, action, observed, operation):
        root.check()
        _need(
            which == target
            and action == ACTION
            and _canonical(operation["binding"]) == _canonical(binding),
            "actual coding callback differs",
        )
        return _execute(root, controller, operation)

    import execution_profiles

    profile = execution_profiles.get_profile("codex-6.1-sol-high")
    pools = sorted(set(profile.get("capacity_pool_ids") or []))
    _need(pools, "coding profile capacity pools unavailable")
    pool_root = (
        Path(os.environ.get("HANDOFF_DIR", Path.home() / ".codex/handoff"))
        / "closer-profile-execution"
    )
    # Identical physical shared pools to existing profile execution. This source
    # coding admission grants no classification/adjudication/repair role credit.
    with resources.inventory_request(time.time() + timeout), contextlib.ExitStack() as slots:
        try:
            for pool in pools:
                name = hashlib.sha256(pool.encode()).hexdigest()
                slots.enter_context(
                    resources.pool_slot(
                        pool_root,
                        name,
                        1,
                        deadline=time.time(),
                        finite_frontier=True,
                        reservation={
                            "reservation_id": hashlib.sha256(
                                f"{pool_root.resolve()}\0{name}\0{frontier['frontier_id']}\0{target}\0{profile['profile_id']}".encode()
                            ).hexdigest(),
                            "target": target,
                            "frontier_id": frontier["frontier_id"],
                            "agent": profile["agent"],
                            "profile_id": profile["profile_id"],
                        },
                    )
                )
            # Revalidate the signed current native source/account-capacity scope
            # after all exclusive profile pools are held.
            root.check()
        except BaseException:
            resources.finite_pool_no_start()
            raise
        return queue.run_round(
            args.state,
            [target],
            observe,
            execute,
            owner=frontier["owner"],
            deadline=None,
            receipt_reserve_seconds=0,
            min_action_seconds=0,
            max_workers=1,
            max_passes=1,
            max_operation_retries=0,
            finite_frontier=frontier,
            frontier_target=target,
            request_timeout_seconds=timeout,
            _inline_source=root,
        )


def _preflight_original(root, controller):
    """Necessary read-only refusals precede creation of a new queue operation.

    This does not authenticate native membership or grant execution: the actual
    callback and final guard perform those joins under writer custody.
    """
    import closer_coding_continuation as continuation
    import closer_coding_session_runtime as runtime
    import closer_coding_session_v2 as session
    import closer_correction_controller as ctl

    protected = continuation.read_worker_selected(root, controller, root.continuation_reference)
    purpose = root.purpose
    _need(
        _canonical(protected["admitted_selector"]) == _canonical(purpose["selected"]),
        "accepted original selector differs",
    )
    consumer = controller.config.consumers.get(purpose["consumer_id"])
    _need(type(consumer) is dict, "source coding consumer unavailable")
    policy = session.validate_policy(consumer.get(session.POLICY_FIELD))
    current = session.validate_budget(consumer.get(runtime.CURRENT_FIELD))
    _need(policy["consumer_id"] == purpose["consumer_id"], "coding consumer policy differs")
    custody = ctl._Custody(controller.config)
    try:
        record = custody.read("admission-" + purpose["selected"]["record_sha256"] + ".json")
        _need(
            ctl.digest(record) == purpose["selected"]["record_sha256"],
            "original protected coding admission changed",
        )
        original = session.snapshot_from_admission(record)
        _need(original["consumer_id"] == purpose["consumer_id"], "original coding consumer differs")
        session.minimum_budget(original["budget"], current, policy["budget"])
        request = record["request"]
        _need(
            ctl.effect_key(request) == purpose["expected"]["key"]
            and request["request_sha256"] == purpose["expected"]["request_sha256"],
            "original coding correction identity differs",
        )
        incumbent = request["incumbent"]
        _need(
            incumbent.get("kind") != "no_incumbent"
            and incumbent["owner"] == consumer.get("owner_login")
            and incumbent["issue"]["number"] == purpose["which"]["number"]
            and incumbent["issue"]["node_id"] == purpose["which"]["node_id"],
            "coding requires exact existing incumbent owner",
        )
        _original_history(root, controller, custody, record)
    finally:
        custody.close()


@dataclass(frozen=True)
class _Continuation:
    seal: object
    root: _Root
    controller: object
    custody: object
    operation: dict
    record: dict
    cleanup: dict
    thread: int

    def check(self):
        import closer_queue as queue

        _need(
            getattr(_CURRENT, "continuation", None) is self
            and self.seal is _SEAL
            and self.thread == threading.get_ident(),
            "actual worker history scope required",
        )
        self.root.check()
        _need(
            time.time() < self.operation["deadline"]
            and queue._currently_invoking(self.root.purpose["target"], self.operation),
            "current worker callback expired or changed",
        )
        self.controller._require_writer(self.operation, self.custody)
        key = self.root.purpose["selected"]["record_sha256"]
        _need(
            _canonical(self.custody.read("admission-" + key + ".json")) == _canonical(self.record),
            "immutable original coding context changed",
        )
        _need(
            _canonical(_original_history(self.root, self.controller, self.custody, self.record))
            == _canonical(self.cleanup),
            "original historical custody changed",
        )


@contextlib.contextmanager
def _history_scope(root, controller, custody, operation, record):
    """New worker custody, never the zero-provider maintenance issuer."""

    _need(getattr(_CURRENT, "continuation", None) is None, "nested worker history forbidden")
    controller._require_writer(operation, custody)
    cleanup = _original_history(root, controller, custody, record)
    scope = _Continuation(
        _SEAL,
        root,
        controller,
        custody,
        copy.deepcopy(operation),
        copy.deepcopy(record),
        cleanup,
        threading.get_ident(),
    )
    _CURRENT.continuation = scope
    try:
        scope.check()
        yield scope
        scope.check()
    finally:
        if getattr(_CURRENT, "continuation", None) is scope:
            del _CURRENT.continuation


def _original_history(root, controller, custody, record):
    """Re-read original native/queue/provider joins, including final reservation."""
    import closer_correction_controller as ctl

    # The exact native reader below owns fresh entry/exit Root checks.
    reference = json.loads(root.accepted_utf8)["continuation_reference"]
    history = _native_reader("coding_original_history")(root.installation, reference)
    _need(
        type(history) is dict
        and set(history)
        == {
            "schema",
            "continuation_reference",
            "start",
            "terminal",
            "terminal_ref",
            "original_config",
            "operation_link",
            "enrollment_boundary",
            "instruction_intent",
            "admission_sha256",
        }
        and history["schema"] == "closer-coding-original-history/v1",
        "exact original coding history required",
    )
    _need(
        _canonical(history["continuation_reference"]) == _canonical(reference)
        and history["admission_sha256"] == ctl.digest(record)
        and _canonical(history["instruction_intent"]["request"]) == _canonical(record["request"])
        and history["instruction_intent"]["phase"] == "instruction_publication"
        and _canonical(history["instruction_intent"]["source_record_ref"])
        == _canonical(record["source_record_ref"]),
        "original instruction admission/history differs",
    )
    return copy.deepcopy(history)


def historical_window(proof):
    scope = getattr(_CURRENT, "continuation", None)
    if scope is None:
        return None
    _need(type(scope) is _Continuation, "worker historical issuer differs")
    scope.check()
    _need(
        _canonical(proof) == _canonical(scope.record["materializer"]["proof"]),
        "original classifier proof differs",
    )
    return copy.deepcopy(scope.cleanup["terminal"]["native_execution"]["phases"]["B"])


def retained_materialization(controller, custody, operation, record):
    scope = getattr(_CURRENT, "continuation", None)
    if scope is None:
        return None
    scope.check()
    _need(
        controller is scope.controller
        and custody is scope.custody
        and _canonical(operation) == _canonical(scope.operation)
        and _canonical(record) == _canonical(scope.record),
        "worker continuation crossed original correction/callback",
    )
    import closer_correction_decision as decisions

    return decisions.read_materialized_selected(**scope.record["materializer"])


def _execute(root, controller, operation):
    """Source callback connects the actual existing coding transport and guard."""
    import closer_coding_session_runtime as runtime
    import closer_coding_session_v2 as session
    import closer_correction_controller as ctl
    import closer_queue as queue
    import closer_resources as resources
    import dispatcher

    queue._coding_timing_point("coding_entry")
    root.check()
    purpose = root.purpose
    consumer = controller.config.consumers.get(purpose["consumer_id"])
    _need(type(consumer) is dict, "source coding consumer unavailable")
    policy = session.validate_policy(consumer.get(session.POLICY_FIELD))
    current = session.validate_budget(consumer.get(runtime.CURRENT_FIELD))
    _need(
        consumer.get("profile_id") == session.PROFILE and consumer.get("agent") == "codex",
        "source coding profile differs",
    )
    import closer_dispatcher as closer

    workspace = closer.selected_workspace(root.backend.workspaces, purpose["target"])
    _need(
        type(workspace) is str and Path(workspace).is_dir(), "authorized coding workspace missing"
    )
    # Only source-owned native readers may be selected. The base dependency
    # intentionally refuses account, route, spending and confinement proof.
    import closer_codex_owned_stdio as owned_stdio

    reader = owned_stdio.reader_from_worker(root, controller, operation)
    with controller.final_provider_guard(
        operation=operation,
        consumer_id=purpose["consumer_id"],
        selected=purpose["selected"],
        which=purpose["which"],
        expected=purpose["expected"],
        reader=reader,
    ) as starting:
        guard = starting.__self__
        record = guard.custody.read("admission-" + purpose["selected"]["record_sha256"] + ".json")
        with _history_scope(root, controller, guard.custody, operation, record):
            original = session.snapshot_from_admission(record)
            publication_policy = None
            publication_baseline_ref = None
            publication_ref = None
            publication_held = False
            if {"publication_v1", "publication_v2"} & (set(consumer) | set(original)):
                import closer_coding_publication as publication

                publication_policy = publication.validate_consumer_mode(
                    purpose["consumer_id"], consumer, original
                )
            limits = session.minimum_budget(original["budget"], current, policy["budget"])
            _need(
                record["request"]["incumbent"].get("kind") != "no_incumbent",
                "existing native incumbent required",
            )
            _need(
                record["request"]["incumbent"]["issue"]["node_id"] == purpose["which"]["node_id"],
                "coding subject is not original incumbent",
            )
            source, incumbent = controller.fresh_issue_context(record["request"], record, consumer)
            payload = {
                "original_request": record["request"],
                "source_body": source["body"],
                "original_incumbent_context": incumbent,
                "raw_findings": record["materializer"]["raw_findings"],
                "required_tests": record["request"]["required_tests"],
            }
            prompt = (
                "Implement this exact source-owned correction and required tests.\n"
                + ctl.canonical(payload).decode()
            )
            if publication_policy is not None:
                queue._coding_timing_point("baseline_start")
                publication_baseline_ref = publication.retain_baseline(
                    root, controller, guard.custody, operation
                )
                queue._coding_timing_point("baseline_end")
            try:
                timeout = request_seconds(
                    root.installation.request_timeout_seconds,
                    remaining=operation["deadline"] - time.time(),
                    session_ms=limits["max_runtime_ms"],
                )
                # Final guard authenticates the exact original record/context again and
                # reserves the stable correction/consumer key at actual before-Popen.
                result = dispatcher.offload(
                    "codex",
                    prompt,
                    cwd=workspace,
                    profile_id=session.PROFILE,
                    task_type=ACTION,
                    mode="full",
                    data_only=False,
                    isolate=publication_baseline_ref is None,
                    timeout=timeout,
                    max_network_retries=0,
                    subject_id=purpose["target"],
                    request_deadline_mono=time.monotonic() + timeout,
                    process_starting_callback=starting,
                    process_started_callback=resources.finite_pool_process_started,
                    process_finished_callback=resources.finite_pool_process_finished,
                    finite_frontier_id=root.installation.finite_frontier["frontier_id"],
                    finite_frontier_target=purpose["target"],
                )
                terminal_ref = retain_terminal(
                    root, controller, guard.custody, operation, guard, result
                )
                if publication_policy is not None:
                    try:
                        publication_ref = publication.retain_handoff(
                            root,
                            controller,
                            guard.custody,
                            operation,
                            publication_baseline_ref,
                            result,
                            terminal_ref,
                        )
                    except Exception:
                        # Coding is already known. Delivery refusal cannot erase it,
                        # rerun the provider or mutate its actual result.
                        publication_held = True
            finally:
                if publication_baseline_ref is not None:
                    try:
                        publication.close_baseline(publication_baseline_ref)
                    except Exception:
                        # Cleanup uncertainty holds delivery, but cannot erase a
                        # known coding terminal or replace a provider exception.
                        publication_held = True
        return {
            "status": "completed",
            "coding_terminal_reference": terminal_ref,
            "actual": result,
            **(
                {"publication_handoff_reference": publication_ref}
                if publication_ref is not None
                else {}
            ),
            **({"publication_handoff_held": True} if publication_held else {}),
            **{key: False for key in _NO_AUTHORITY},
        }


_NO_AUTHORITY = frozenset(
    {
        "target_complete",
        "domain_completed",
        "acceptance_authorized",
        "retry_authorized",
        "act_authorized",
        "provider_authorized",
        "source_closure_authorized",
        "global_stop_valid",
    }
)
TERMINAL_REFERENCE_SCHEMA = "closer-coding-worker-terminal-reference/v1"


def retain_terminal(root, controller, custody, operation, guard, result):
    """Retain only the actual returned local v2 observation while source custody lives."""
    import closer_coding_continuation as continuation
    import closer_coding_session_runtime as runtime
    import closer_coding_session_v2 as session
    import closer_correction_controller as ctl
    import dispatcher

    caller = inspect.currentframe().f_back
    _need(
        caller.f_code is _execute.__code__
        and caller.f_globals is globals()
        and all(
            caller.f_locals.get(k) is v
            for k, v in {
                "root": root,
                "controller": controller,
                "operation": operation,
                "guard": guard,
                "result": result,
            }.items()
        ),
        "actual returned coding source frame required",
    )
    scope = getattr(_CURRENT, "continuation", None)
    _need(
        type(scope) is _Continuation
        and scope.root is root
        and scope.controller is controller
        and scope.custody is custody
        and _canonical(scope.operation) == _canonical(operation),
        "actual live coding history custody required",
    )
    scope.check()
    _need(
        type(guard) is ctl._ProviderGuard
        and guard.active is True
        and guard.used is True
        and guard.controller is controller
        and guard.custody is custody
        and guard.thread == threading.get_ident()
        and guard.pid == os.getpid()
        and _canonical(guard.operation) == _canonical(operation),
        "actual consumed coding provider guard required",
    )
    controller._require_writer(operation, custody)
    plans = [p for p in runtime._OFFLOADS.values() if p.guard is guard]
    _need(
        len(plans) == 1 and type(plans[0]) is runtime._Plan,
        "actual coding runtime plan unavailable",
    )
    plan = plans[0]
    key = (threading.get_ident(), plan.origin)
    _need(
        plan.thread == threading.get_ident()
        and runtime._OFFLOADS.get(key) is plan
        and plan.origin.f_code is dispatcher._CODING_DELIVERY_OFFLOAD_CODE
        and plan.origin.f_globals is dispatcher.__dict__
        and plan.origin.f_locals.get("out") is result
        and plan.run.f_code is dispatcher._CODING_DELIVERY_RUN_CODE
        and plan.run.f_globals is dispatcher.__dict__
        and runtime._RUNS.get((plan.thread, plan.run)) is plan
        and (plan.thread, plan.run) in runtime._APPLIED,
        "coding result lacks actual source runtime identity",
    )
    consumer = controller.config.consumers[root.purpose["consumer_id"]]
    original = session.snapshot_from_admission(scope.record)
    policy = session.validate_policy(consumer[session.POLICY_FIELD])
    limits = session.minimum_budget(
        original["budget"], consumer[runtime.CURRENT_FIELD], policy["budget"]
    )
    _need(
        _canonical(json.loads(plan.budget_utf8)) == _canonical(limits),
        "actual coding runtime limits changed",
    )
    observation = validate_terminal_observation(result.get("coding_session_v2"))
    expected = {
        "schema": "closer-coding-session-observation/v2",
        **runtime._FACTS[key],
        **{key: "UNKNOWN" for key in session.UNKNOWN_FIELDS},
        "budget": limits,
    }
    _need(
        _canonical(observation) == _canonical(expected)
        and result.get("usage") == {}
        and observation["local_elapsed_ms"] <= limits["max_runtime_ms"] + limits["max_cleanup_ms"]
        and len(plan.delivery.prompt_utf8) <= limits["max_prompt_utf8_bytes"],
        "returned coding result differs from actual bounded observations",
    )
    runtime._require_redaction()
    text_fields = ("output", "stderr_tail", "partial_stdout", "partial_stderr", "error")
    runtime.check_text(plan.origin, *(result.get(k, "") or "" for k in text_fields))
    runtime.check_serialized(plan.origin, json.dumps(result, sort_keys=True).encode("utf-8"))
    protected = continuation.read_worker_selected(root, controller, root.continuation_reference)
    _need(
        protected["admission_sha256"] == ctl.digest(scope.record)
        and protected["stable_key"] == root.continuation_reference["stable_key"],
        "terminal original continuation changed",
    )
    accepted = json.loads(root.accepted_utf8)
    record = {
        "schema": TERMINAL_SCHEMA,
        "continuation_reference": root.continuation_reference,
        "native_worker": {"worker_id": accepted["worker_id"], "native_b_start": root.start_ref},
        "original": {
            "admission_sha256": protected["admission_sha256"],
            "operation_sha256": protected["operation_sha256"],
        },
        "operation_sha256": _queue_digest(operation),
        "stable_key": protected["stable_key"],
        "observation": observation,
        "result_sha256": _queue_digest(result),
        "budget": limits,
        **{key: False for key in _NO_AUTHORITY},
    }
    validate_terminal_record(record)
    digest = ctl.digest(record)
    name = "coding-worker-terminal-" + digest + ".json"
    scope.check()
    _need(custody.create(name, record), "coding terminal already retained")
    _need(_canonical(custody.read(name)) == _canonical(record), "coding terminal readback differs")
    scope.check()
    return {"schema": TERMINAL_REFERENCE_SCHEMA, "name": name, "sha256": digest}


def read_terminal_for_native_c(manifest, campaign_document, cfg, reference):
    """Knowledge only; a C context cannot borrow the worker execution installation."""
    caller = inspect.currentframe().f_back
    reader = _native_reader("coding_terminal_proof")
    _need(
        caller.f_code is reader.__code__ and caller.f_globals is reader.__globals__,
        "actual native coding C reader required",
    )
    import closer_coding_continuation as continuation
    import closer_correction_controller as ctl
    import closer_mechanisms as mechanisms
    import closer_native_observer as native

    context = _native_reader("coding_terminal_reader_context")(
        manifest, campaign_document, cfg, reference
    )
    _need(
        type(context) is dict
        and set(context)
        == {
            "schema",
            "config",
            "accepted_purpose",
            "original_history",
            "operation",
            "manifest_sha256",
            "worker_config_sha256",
        }
        and context["schema"] == "closer-coding-terminal-reader-context/v1",
        "exact native C knowledge context required",
    )
    accepted = validate_accepted_purpose(context["accepted_purpose"])
    ref = continuation.validate_reference(accepted["continuation_reference"])
    _need(
        context["worker_config_sha256"] == mechanisms.digest(cfg)
        and context["manifest_sha256"]
        == hashlib.sha256(
            native._bounded_bytes(cfg["runtime_manifest"], 16 * 1024 * 1024)
        ).hexdigest(),
        "native C signed config/physical manifest differs",
    )
    worker_path = Path(__file__)
    _need(
        Path(manifest["module_dir"]) / worker_path.name == worker_path
        and worker_path.resolve(strict=True) == worker_path
        and not any(p.is_symlink() for p in (worker_path, *worker_path.parents))
        and manifest["modules"].get(worker_path.name) == _LOADED_SOURCE_SHA256
        and hashlib.sha256(worker_path.read_bytes()).hexdigest() == _LOADED_SOURCE_SHA256,
        "native C coding worker source changed",
    )
    terminal_ref = validate_terminal_reference(reference)
    config = ctl.ControllerConfig(**context["config"])
    with contextlib.closing(ctl._Custody(config)) as custody:
        record = validate_terminal_record(custody.read(terminal_ref["name"]))
        _need(ctl.digest(record) == terminal_ref["sha256"], "protected coding terminal changed")
        protected = continuation._read_protected(custody, ref)
        history = context["original_history"]
        _need(
            type(history) is dict
            and set(history)
            == {
                "schema",
                "continuation_reference",
                "start",
                "terminal",
                "terminal_ref",
                "original_config",
                "operation_link",
                "enrollment_boundary",
                "instruction_intent",
                "admission_sha256",
            }
            and history["schema"] == "closer-coding-original-history/v1",
            "native C original history differs",
        )
        _need(
            _canonical(history["continuation_reference"]) == _canonical(ref)
            and _canonical(record["continuation_reference"]) == _canonical(ref)
            and record["stable_key"] == ref["stable_key"]
            and record["native_worker"]["worker_id"] == accepted["worker_id"]
            and _canonical(record["native_worker"]["native_b_start"])
            == _canonical(accepted["native_b_start"])
            and record["original"]["admission_sha256"]
            == history["admission_sha256"]
            == protected["admission_sha256"]
            and record["original"]["operation_sha256"] == protected["operation_sha256"]
            and record["operation_sha256"] == _queue_digest(context["operation"])
            and context["operation"]["status"] == "submitted"
            and context["operation"]["action"] == ACTION
            and protected["controller_config_sha256"] == ctl.digest(config.source_binding())
            and protected["manifest_sha256"] == context["manifest_sha256"],
            "native C original/fresh coding identities differ",
        )
        _need(
            _canonical(custody.read(terminal_ref["name"])) == _canonical(record),
            "native C terminal custody changed during read",
        )
        return copy.deepcopy(record)


def _queue_digest(value):
    """Existing queue/source-origin JSON domain includes finite callback clocks."""
    import closer_queue as queue

    return hashlib.sha256(queue._canonical_bytes(value)).hexdigest()


def validate_terminal_reference(value):
    import closer_correction_controller as ctl

    _need(
        type(value) is dict
        and set(value) == {"schema", "name", "sha256"}
        and value["schema"] == TERMINAL_REFERENCE_SCHEMA
        and type(value["sha256"]) is str
        and ctl._SHA.fullmatch(value["sha256"])
        and value["name"] == "coding-worker-terminal-" + value["sha256"] + ".json",
        "exact protected coding terminal reference required",
    )
    return copy.deepcopy(value)


def validate_terminal_record(value):
    """Detached necessary integrity only; protected native C joins remain mandatory."""
    import closer_coding_continuation as continuation
    import closer_coding_session_v2 as session
    import closer_correction_controller as ctl

    _need(
        type(value) is dict
        and set(value)
        == {
            "schema",
            "continuation_reference",
            "native_worker",
            "original",
            "operation_sha256",
            "stable_key",
            "observation",
            "result_sha256",
            "budget",
            *_NO_AUTHORITY,
        }
        and value["schema"] == TERMINAL_SCHEMA
        and all(value[k] is False for k in _NO_AUTHORITY),
        "exact nonauthorizing coding terminal record required",
    )
    ref = continuation.validate_reference(value["continuation_reference"])
    _need(
        value["stable_key"] == ref["stable_key"]
        and all(
            type(value[k]) is str and ctl._SHA.fullmatch(value[k])
            for k in ("operation_sha256", "result_sha256")
        ),
        "coding terminal operation/result identity differs",
    )
    original = value["original"]
    _need(
        type(original) is dict
        and set(original) == {"admission_sha256", "operation_sha256"}
        and all(type(v) is str and ctl._SHA.fullmatch(v) for v in original.values()),
        "coding terminal original identity differs",
    )
    worker = value["native_worker"]
    _need(
        type(worker) is dict
        and set(worker) == {"worker_id", "native_b_start"}
        and type(worker["worker_id"]) is str
        and worker["worker_id"]
        and worker["worker_id"] == worker["worker_id"].strip(),
        "coding terminal fresh worker identity differs",
    )
    start = worker["native_b_start"]
    _need(
        type(start) is dict
        and set(start) == {"path", "sha256"}
        and type(start["path"]) is str
        and Path(start["path"]).is_absolute()
        and type(start["sha256"]) is str
        and ctl._SHA.fullmatch(start["sha256"]),
        "coding terminal B-start identity differs",
    )
    observation = validate_terminal_observation(value["observation"])
    budget = session.validate_budget(value["budget"])
    _need(
        _canonical(observation["budget"]) == _canonical(budget)
        and observation["local_elapsed_ms"] <= budget["max_runtime_ms"] + budget["max_cleanup_ms"],
        "coding terminal observation/budget differs",
    )
    _need(
        len(ctl.canonical(value)) <= budget["max_retained_receipt_bytes"],
        "protected coding terminal exceeds retained receipt bound",
    )
    return copy.deepcopy(value)
