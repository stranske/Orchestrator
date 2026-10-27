"""Private two-phase extension of the existing dispatcher-owned Codex transport.

No runner, user RPC, serialized permit or successful confinement producer exists
here. Supported first-party metadata bootstrap is distinct from task admission.
"""

from __future__ import annotations

import ctypes
import hashlib
import inspect
import json
import os
import selectors
import stat
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import closer_correction_controller as ctl
from closer_codex_appserver_bootstrap import BootstrapProtocol, ProtocolHeld

_LOADED_SOURCE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
CHANNEL = "codex-source-owned-appserver-stdio/v1"
ENTRYPOINT = __name__ + ".SourceReader"
_READERS: dict[int, tuple] = {}
_ROUTES: dict[tuple[int, object], BootstrapRoute] = {}
_SESSIONS: dict[tuple[int, object], _OwnedSession] = {}
_REMOTE_DISABLE = "CODEX_INTERNAL_APP_SERVER_REMOTE_CONTROL_DISABLED"
_FIXED_OVERRIDES = (
    'model_provider="openai"',
    'chatgpt_base_url="https://chatgpt.com/backend-api/"',
    'openai_base_url="https://chatgpt.com/backend-api/codex"',
    "features.respect_system_proxy=false",
    "features.system_proxy_fallback=false",
)


class BootstrapHeld(RuntimeError):
    """Fixed diagnostics only; native text/config/account values never echoed."""


def _need(value, code="owned_bootstrap_source_unavailable"):
    if not value:
        raise BootstrapHeld(code)


def _physical(name, *, directory=False, expected=None, binary=False):
    _need(type(name) is str and name.startswith("/"))
    p = Path(name)
    _need(p.resolve(strict=True) == p and not any(v.is_symlink() for v in (p, *p.parents)))
    before = p.stat()
    if directory:
        _need(stat.S_ISDIR(before.st_mode))
        return (str(p), before.st_dev, before.st_ino)
    _need(stat.S_ISREG(before.st_mode) and before.st_size <= (512 if binary else 8) * 1024 * 1024)
    h = hashlib.sha256()
    with p.open("rb") as stream:
        while chunk := stream.read(65536):
            h.update(chunk)
        after = os.fstat(stream.fileno())
    _need(
        (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        == (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    )
    _need(expected is None or h.hexdigest() == expected)
    return (str(p), before.st_dev, before.st_ino, before.st_size, h.hexdigest())


def _managed_route_absent():
    """Fresh fixed-key presence/forced checks; no config/preference values read.

    This intentionally supports the reviewed absent-managed-layer installation
    only. A present or forced policy holds instead of disabling or interpreting it.
    """
    for name in ("/etc/codex/managed_config.toml", "/etc/codex/requirements.toml"):
        try:
            os.lstat(name)
        except FileNotFoundError:
            continue
        except OSError:
            raise BootstrapHeld("managed_route_observation_unavailable") from None
        raise BootstrapHeld("managed_route_requires_review")
    try:
        cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        ptr = ctypes.c_void_p
        cf.CFStringCreateWithCString.argtypes = (ptr, ctypes.c_char_p, ctypes.c_uint32)
        cf.CFStringCreateWithCString.restype = ptr
        cf.CFPreferencesAppValueIsForced.argtypes = (ptr, ptr)
        cf.CFPreferencesAppValueIsForced.restype = ctypes.c_ubyte
        cf.CFRelease.argtypes = (ptr,)
        domain = cf.CFStringCreateWithCString(None, b"com.openai.codex", 0x08000100)
        _need(domain, "managed_route_observation_unavailable")
        try:
            for name in (b"config_toml_base64", b"requirements_toml_base64"):
                key = cf.CFStringCreateWithCString(None, name, 0x08000100)
                _need(key, "managed_route_observation_unavailable")
                try:
                    _need(
                        not cf.CFPreferencesAppValueIsForced(key, domain),
                        "managed_route_requires_review",
                    )
                finally:
                    cf.CFRelease(key)
        finally:
            cf.CFRelease(domain)
    except (OSError, AttributeError):
        raise BootstrapHeld("managed_route_observation_unavailable") from None
    return ("managed-files-absent", "exact-forced-keys-false")


def _reader_live(reader):
    value = _READERS.get(id(reader))
    _need(value is not None and value[0] is reader and value[4] == threading.get_ident())
    _, root, controller, operation, _, registration_utf8 = value
    root.check()
    _need(
        ctl.canonical(controller.config.consumers[root.purpose["consumer_id"]])
        == registration_utf8,
        "original_consumer_registration_changed",
    )
    _need(controller is reader._controller and operation is reader._operation)
    pinned = root.installation.source_closure.get(Path(__file__).name)
    _need(pinned == {"path": str(Path(__file__)), "sha256": _LOADED_SOURCE_SHA256})
    _physical(str(Path(__file__)), expected=_LOADED_SOURCE_SHA256)
    return root, controller, operation


class SourceReader(ctl.NativeConsumerReader):
    """Only factory-issued actual-source instances can observe an owned session."""

    def __init__(self):
        raise BootstrapHeld("actual_worker_reader_factory_required")

    def read_native_identity(self):
        _reader_live(self)
        session = next((s for s in _SESSIONS.values() if s.reader is self), None)
        _need(session is not None and session.phase in {"metadata", "final", "turn"})
        session.check()
        _refresh_identity(session)
        _need(session.protocol.snapshot()["native_claim_identity_match_present"])
        return {
            "account_id": session.protocol._native_account_id,
            "profile_id": session.route.profile["profile_id"],
        }

    def verify_installed_confinement(self, *, command, cwd, env, custody_path):
        _reader_live(self)
        session = next((s for s in _SESSIONS.values() if s.reader is self), None)
        _need(session is not None)
        session.check()
        _need(
            list(session.route.transport_argv) == command
            and cwd == session.route.cwd
            and env == dict(session.route.environment)
            and custody_path == session.guard.custody.path
        )
        _observe_effective_confinement(session)


def _observe_effective_confinement(session):
    import closer_codex_native_confinement as source

    try:
        return source.observe_effective_confinement(session)
    except source.ConfinementHeld as error:
        code = str(error)
        fixed = code if code in source.HELD_CODES else "source_held"
        raise BootstrapHeld("confinement_" + fixed) from None


def reader_from_worker(root, controller, operation):
    import closer_coding_worker as worker

    frame = inspect.currentframe().f_back
    _need(
        frame.f_code is worker._execute.__code__
        and frame.f_globals is worker.__dict__
        and frame.f_locals.get("root") is root
        and frame.f_locals.get("controller") is controller
        and frame.f_locals.get("operation") is operation
    )
    root.check()
    registration = controller.config.consumers[root.purpose["consumer_id"]]
    if registration.get("reader_entrypoint") != ENTRYPOINT:
        return ctl.NativeConsumerReader()
    _need(registration.get("reader_source_sha256") == _LOADED_SOURCE_SHA256)
    reader = object.__new__(SourceReader)
    reader._controller, reader._operation = controller, operation
    _need(
        registration.get("transport_binding") == CHANNEL
        and "transport_argv_sha256" not in registration,
        "private_source_transport_binding_required",
    )
    _READERS[id(reader)] = (
        reader,
        root,
        controller,
        operation,
        threading.get_ident(),
        ctl.canonical(registration),
    )
    _reader_live(reader)
    return reader


def selected(guard):
    return type(getattr(guard, "reader", None)) is SourceReader and id(guard.reader) in _READERS


@dataclass(frozen=True)
class BootstrapRoute:
    guard: object
    origin: object
    profile_utf8: bytes
    provider_argv: tuple
    transport_argv: tuple
    environment: tuple
    cwd: str
    files: tuple
    physical: tuple
    permission: str
    confinement: object
    thread: int

    @property
    def profile(self):
        return json.loads(self.profile_utf8)


def prepare(frame, guard, argv, cwd, profile):
    import closer_coding_route_v2 as route_source
    import closer_coding_session_runtime as runtime
    import dispatcher

    caller = inspect.currentframe().f_back
    _need(
        caller.f_code is route_source.prepare.__code__
        and caller.f_globals is route_source.__dict__
        and caller.f_back is frame
    )
    _need(
        frame.f_code is dispatcher._CODING_DELIVERY_OFFLOAD_CODE
        and frame.f_globals is dispatcher.__dict__
        and runtime._HISTORY_RESTRICTIONS.get((threading.get_ident(), frame)) is guard
    )
    _reader_live(guard.reader)
    reg = guard.controller.config.consumers[guard.consumer_id]
    import closer_coding_session_v2 as session_contract
    import execution_profiles

    _need(
        profile == execution_profiles.get_profile(session_contract.PROFILE)
        and ctl.source_digest(profile) == reg["profile_sha256"]
    )
    spec = reg.get("native_bootstrap")
    _need(
        type(spec) is dict
        and set(spec)
        == {
            "binary_path",
            "binary_sha256",
            "home",
            "codex_home",
            "private_root",
            "gh_path",
            "gh_sha256",
            "confinement",
        },
        "native_bootstrap_selection_unavailable",
    )
    _need(argv[0] == spec["binary_path"])
    _managed_route_absent()
    physical = tuple(
        _physical(spec[n], directory=True) for n in ("home", "codex_home", "private_root")
    )
    physical += (
        _physical(cwd, directory=True),
        _physical(spec["binary_path"], expected=spec["binary_sha256"], binary=True),
        _physical(spec["gh_path"], expected=spec["gh_sha256"], binary=True),
    )
    # Keep the existing source-owned GitHub shell write guard in descendants.
    root = Path(spec["private_root"])
    proxy = root / "coding-route-gh"
    proxy.mkdir(mode=0o700, exist_ok=True)
    _physical(str(proxy), directory=True)
    import shlex

    gh, bashenv = proxy / "gh", root / "coding-route-bashenv.sh"
    texts = (
        (
            gh,
            "#!/bin/sh\n"
            + dispatcher._gh_guard_check("exit")
            + "exec "
            + shlex.quote(spec["gh_path"])
            + ' "$@"\n',
            0o700,
        ),
        (
            bashenv,
            "gh() {\n"
            + dispatcher._gh_guard_check("return")
            + "command "
            + shlex.quote(spec["gh_path"])
            + ' "$@"\n}\nexport -f gh\n',
            0o600,
        ),
    )
    files = []
    for path, text, mode in texts:
        _need(not path.is_symlink() and (not path.exists() or path.is_file()))
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, mode)
        with os.fdopen(fd, "w") as stream:
            stream.write(text)
        os.chmod(path, mode)
        files.append(_physical(str(path)))
    import closer_codex_native_confinement as confinement_source

    _pin_confinement_source(guard, confinement_source)
    for member in guard.controller.installation.source_closure.values():
        source_path = Path(member["path"])
        _need(
            not any(
                source_path == Path(writable) or Path(writable) in source_path.parents
                for writable in (cwd, spec["private_root"])
            ),
            "coding_writable_root_overlaps_source_installation",
        )
    confinement = confinement_source.prepare_confinement_profile(
        spec["confinement"],
        workspace=cwd,
        private_root=spec["private_root"],
        codex_home=spec["codex_home"],
        custody_path=guard.custody.path,
        home=spec["home"],
    )
    _need(
        type(confinement) is confinement_source.ConfinementProfile
        and confinement.native_binary[0] == spec["binary_path"]
        and confinement.native_binary[-1] == spec["binary_sha256"]
    )
    native = [spec["binary_path"]]
    for override in _FIXED_OVERRIDES + confinement.cli_overrides:
        native.extend(("-c", override))
    native.extend(("app-server", "--strict-config", "--stdio"))
    environment = tuple(
        sorted(
            {
                "HOME": spec["home"],
                "CODEX_HOME": spec["codex_home"],
                "TMPDIR": spec["private_root"],
                "PATH": str(proxy) + ":/usr/bin:/bin:/usr/sbin:/sbin",
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "BASH_ENV": str(bashenv),
                _REMOTE_DISABLE: "1",
            }.items()
        )
    )
    route = BootstrapRoute(
        guard,
        frame,
        json.dumps(profile, sort_keys=True, separators=(",", ":")).encode(),
        tuple(native),
        tuple(native),
        environment,
        cwd,
        tuple(files),
        physical,
        confinement.permission_profile,
        confinement,
        threading.get_ident(),
    )
    _need((route.thread, frame) not in _ROUTES)
    _ROUTES[(route.thread, frame)] = route
    import closer_codex_metadata as metadata_source

    metadata_source.preflight_owned_route(route)
    return route


def verify_route(route):
    _need(
        type(route) is BootstrapRoute
        and route.thread == threading.get_ident()
        and _ROUTES.get((route.thread, route.origin)) is route
        and route.guard.active
    )
    _reader_live(route.guard.reader)
    import closer_codex_native_confinement as confinement_source

    confinement_source.verify_confinement_profile(route.confinement)
    _managed_route_absent()
    for value in route.physical + route.files:
        if len(value) == 3:
            _need(_physical(value[0], directory=True) == value)
        else:
            _need(
                _physical(
                    value[0],
                    expected=value[-1],
                    binary=value[0] in {route.physical[-2][0], route.physical[-1][0]},
                )
                == value
            )


def route_for_run(frame):
    import dispatcher

    _need(
        frame.f_code is dispatcher._CODING_DELIVERY_RUN_CODE
        and frame.f_globals is dispatcher.__dict__
    )
    attempt = frame.f_back
    _need(
        attempt is not None
        and attempt.f_code is dispatcher._CODING_DELIVERY_ATTEMPT_CODE
        and attempt.f_globals is dispatcher.__dict__
    )
    origin = attempt.f_back
    _need(
        origin is not None
        and origin.f_code is dispatcher._CODING_DELIVERY_OFFLOAD_CODE
        and origin.f_globals is dispatcher.__dict__
    )
    route = _ROUTES.get((threading.get_ident(), origin))
    if route is None:
        return None
    verify_route(route)
    _need(
        origin.f_locals.get("coding_route") is route
        and tuple(origin.f_locals.get("argv", ())) == route.provider_argv
        and frame.f_locals.get("command") == list(route.transport_argv)
        and frame.f_locals.get("env") == dict(route.environment)
        and frame.f_locals.get("cwd") == route.cwd
    )
    return route


def _stable(guard, entry):
    return {
        "schema": "local-correction-bootstrap-key/v1",
        "repository_id": guard.controller.config.repository_id,
        "epoch": guard.controller.config.epoch,
        "key": entry["key"],
        "request_sha256": entry["request"]["request_sha256"],
        "consumer_id": guard.consumer_id,
    }


def reserve_from_guard(guard, frame, observation):
    """Called only by the original starting method; never consumes its used bit."""
    import closer_resources as resources
    import dispatcher

    if not selected(guard):
        return False
    caller = inspect.currentframe().f_back
    _need(
        caller.f_code is ctl._ProviderGuard.starting.__code__
        and caller.f_globals is ctl.__dict__
        and caller.f_back is frame
        and caller.f_locals.get("self") is guard
        and frame.f_code is dispatcher._CODING_DELIVERY_RUN_CODE
        and frame.f_locals.get("starting_callback") == guard.starting
    )
    _reader_live(guard.reader)
    key = (threading.get_ident(), frame)
    existing = _SESSIONS.get(key)
    if existing is not None:
        existing.check()
        _need(existing.phase == "metadata" and not guard.used)
        return False  # The unchanged substantive guard must now run in full.
    _need(
        guard.active
        and not guard.used
        and guard.thread == threading.get_ident()
        and guard.pid == os.getpid()
    )
    guard.controller._require_writer(guard.operation, guard.custody)
    owner, _ = guard.controller.live(guard.operation)
    current = guard.controller.read_bound(guard.custody)
    entry = next((e for e in current["data"]["entries"] if guard.which in e["subjects"]), None)
    _need(
        entry is not None
        and guard.expected
        == {"key": entry["key"], "request_sha256": entry["request"]["request_sha256"]}
    )
    _need(all(s["state"] == "confirmed" for e in current["data"]["entries"] for s in e["stages"]))
    _need(entry["request"]["incumbent"].get("kind") != "no_incumbent")
    _need(
        entry["request"]["incumbent"]["owner"]
        == guard.controller.config.consumers[guard.consumer_id]["owner_login"]
    )
    guard.controller.authenticate(guard.selected, entry["request"], guard.custody, guard.operation)
    _need(
        guard.selected == entry["admitted_selector"]
        or any(
            b["selector"] == guard.selected and b["subject"] == guard.which
            for b in entry["bindings"]
        )
    )
    binding = resources.finite_pool_current_binding()
    _need(
        binding
        == {
            "frontier_id": guard.operation["finite_frontier"]["frontier_id"],
            "target": owner["target"],
        }
    )
    route = route_for_run(frame)
    _need(route is not None and route.guard is guard)
    _need(
        observation.get("finite_frontier_id") == binding["frontier_id"]
        and observation.get("finite_frontier_target") == binding["target"]
        and observation.get("profile_id") == route.profile["profile_id"]
        and observation.get("agent") == "codex"
        and observation.get("run_id")
    )
    stable = _stable(guard, entry)
    # The same correction/consumer key survives worker/campaign/queue changes.
    provider = {**stable, "schema": "local-correction-final-provider-key/v1"}
    _need(
        not (
            Path(guard.custody.path) / ("provider-intent-" + ctl.digest(provider) + ".json")
        ).exists(),
        "original_provider_intent_exists_no_repeat",
    )
    _need(
        guard.custody.create(
            "bootstrap-intent-" + ctl.digest(stable) + ".json",
            {
                **stable,
                "index_commit": current["commit_sha"],
                "selector": guard.selected,
                "transport_sha256": ctl.digest(list(route.transport_argv)),
                "task_authorized": False,
            },
        ),
        "original_bootstrap_intent_exists_no_repeat",
    )
    _SESSIONS[key] = _OwnedSession(frame, guard, route)
    return True


class _OwnedSession:
    def __init__(self, frame, guard, route):
        self.frame, self.guard, self.route, self.reader = (
            frame,
            guard,
            route,
            guard.reader,
        )
        self.thread, self.pid = threading.get_ident(), os.getpid()
        self.phase, self.child, self.protocol, self.turn_written = (
            "reserved",
            None,
            None,
            False,
        )
        self.final_plan = None
        self.refresh_count = 0
        self.native_buffer = b""
        self.stderr_closed = False

    def check(self):
        _need(
            self.thread == threading.get_ident()
            and self.pid == os.getpid()
            and _SESSIONS.get((self.thread, self.frame)) is self
            and self.guard.active
        )
        _reader_live(self.reader)
        self.guard.controller._require_writer(self.guard.operation, self.guard.custody)
        self.guard.controller.live(self.guard.operation)
        verify_route(self.route)


def reserved(frame, guard):
    s = _SESSIONS.get((threading.get_ident(), frame))
    return s is not None and s.guard is guard and s.phase == "reserved" and not guard.used


def accept_final_guard(frame, guard, plan):
    caller = inspect.currentframe().f_back
    import closer_coding_session_runtime as runtime

    _need(
        caller.f_code is runtime.issue_before_popen.__code__
        and caller.f_globals is runtime.__dict__
    )
    s = _SESSIONS.get((threading.get_ident(), frame))
    _need(s is not None and s.guard is guard and guard.used and s.phase == "metadata")
    s.check()
    _need(
        s.child is frame.f_locals.get("child")
        and s.child is not None
        and s.child.poll() is None
        and plan.run is frame
        and plan.guard is guard
    )
    s.final_plan, s.phase = plan, "final"


def session_for_run(frame):
    s = _SESSIONS.get((threading.get_ident(), frame))
    if s is not None:
        s.check()
    return s


def release_guard(guard):
    import closer_coding_session_runtime as runtime

    caller = inspect.currentframe().f_back
    _need(caller.f_code is runtime.release_guard.__code__ and caller.f_globals is runtime.__dict__)
    for key, s in tuple(_SESSIONS.items()):
        if s.guard is guard:
            _SESSIONS.pop(key, None)
    for key, r in tuple(_ROUTES.items()):
        if r.guard is guard:
            _ROUTES.pop(key, None)
    _READERS.pop(id(guard.reader), None)


def _actual_session(frame):
    import dispatcher

    # Public source entrypoints below additionally require their direct caller to
    # be the original dispatcher frame; this helper never issues a capability.
    _need(
        frame.f_code is dispatcher._CODING_DELIVERY_RUN_CODE
        and frame.f_globals is dispatcher.__dict__
    )
    s = session_for_run(frame)
    _need(s is not None and s.frame is frame)
    return s


def _io_live(s, *, allow_exit=False):
    _need(
        s.thread == threading.get_ident()
        and s.pid == os.getpid()
        and _SESSIONS.get((s.thread, s.frame)) is s
        and s.guard.active
    )
    s.guard.controller._require_writer(s.guard.operation, s.guard.custody)
    import closer_coding_session_runtime as runtime

    plan = runtime._plan(s.frame)
    _need(
        plan is not None
        and plan.run is s.frame
        and plan.guard is s.guard
        and s.child is s.frame.f_locals.get("child")
        and (allow_exit or s.child.poll() is None)
    )
    remaining = plan.deadline_mono - time.monotonic()
    _need(remaining > 0, "original_bootstrap_window_exhausted")
    return remaining


def _write_bytes(s, raw):
    _need(type(raw) is bytes and raw)
    pending = memoryview(raw)
    while pending:
        remaining = _io_live(s)
        with selectors.DefaultSelector() as selector:
            selector.register(s.child.stdin, selectors.EVENT_WRITE)
            if not selector.select(min(0.05, remaining)):
                continue
        try:
            count = os.write(s.child.stdin.fileno(), pending[:65536])
        except BlockingIOError:
            continue
        except OSError:
            raise BootstrapHeld("owned_stdio_write_unavailable") from None
        _need(count > 0, "owned_stdio_partial_write_unknown")
        pending = pending[count:]
        s.outbound_count += count


def _read_bytes(s):
    remaining = _io_live(s)
    with selectors.DefaultSelector() as selector:
        for pipe, name in ((s.child.stdout, "stdout"), (s.child.stderr, "stderr")):
            if name != "stderr" or not s.stderr_closed:
                selector.register(pipe, selectors.EVENT_READ, name)
        ready = selector.select(min(0.05, remaining))
        for key, _ in ready:
            room = s.output_limit - s.stdout_count - s.stderr_count
            try:
                raw = os.read(key.fileobj.fileno(), min(8192, room + 1))
            except BlockingIOError:
                continue
            except OSError:
                raise BootstrapHeld("owned_stdio_read_unavailable") from None
            if not raw and key.data == "stderr":
                s.stderr_closed = True
                continue
            _need(raw, "owned_stdio_eof_before_completion")
            if key.data == "stderr":
                s.stderr_count += len(raw)
            else:
                s.stdout_count += len(raw)
            _need(
                s.stdout_count + s.stderr_count <= s.output_limit,
                "owned_stdio_output_exhausted",
            )
            if key.data == "stdout":
                return raw
            # Raw native diagnostics are counted and discarded, never retained.
    return b""


def bootstrap_from_run(frame):
    """Actual source-owned child only; final guard remains in the run frame."""
    _need(inspect.currentframe().f_back is frame)
    s = _actual_session(frame)
    _need(s.phase == "reserved" and s.child is None and not s.guard.used)
    import closer_coding_session_runtime as runtime

    plan = runtime._plan(frame)
    _need(plan is not None and plan.guard is s.guard and plan.run is frame)
    facts = runtime._FACTS[(plan.thread, plan.origin)]
    observed = frame.f_locals.get("observed")
    child = frame.f_locals.get("child")
    _need(
        facts["outer_submissions"] == 1
        and facts["spawn_observed"]
        and observed.get("source") == "trusted-popen-transport/v1"
        and observed.get("pid") == child.pid
        and observed.get("process_group") == child.pid
        and type(observed.get("os_started")) is str
        and bool(observed["os_started"])
    )
    s.child, s.phase = child, "observing"
    budget = json.loads(plan.budget_utf8)
    s.stdout_count = s.stderr_count = s.outbound_count = 0
    s.output_limit = budget["max_captured_output_bytes"]
    for pipe in (child.stdin, child.stdout, child.stderr):
        _need(pipe is not None)
        os.set_blocking(pipe.fileno(), False)
    profile = s.route.profile
    s.protocol = BootstrapProtocol(
        model=profile["requested_model"],
        provider="openai",
        effort=profile["reasoning_effort"],
        permission_profile=s.route.permission,
        cwd=s.route.cwd,
        workspace_roots=[s.route.cwd],
        deadline=plan.deadline_mono,
        max_frame_bytes=min(65536, s.output_limit),
        max_total_bytes=min(4 * 1024 * 1024, s.output_limit),
        max_notifications=128,
    )
    from closer_codex_stdio_exchange import BootstrapExchange, StdioHeld

    s.exchange = BootstrapExchange(s.protocol)
    try:
        while not s.protocol.snapshot()["ready_for_source_admission"]:
            _io_live(s)
            # The private codec has a fixed step list, no arbitrary RPC input.
            # Require actual enforcement before thread/start can prewarm.
            if s.protocol._pending is None:
                if s.protocol._index in {2, 4}:
                    _observe_effective_confinement(s)
                s.exchange.request(now=time.monotonic())
                while s.exchange.snapshot()["pending_write_bytes"]:
                    outgoing = s.exchange.writable(65536, now=time.monotonic())
                    _write_bytes(s, outgoing)
                    s.exchange.written(len(outgoing), now=time.monotonic())
            while s.protocol._pending is not None:
                raw = _next_native_frame(s)
                s.exchange.received(raw, now=time.monotonic())
    except (ProtocolHeld, StdioHeld):
        raise BootstrapHeld("native_bootstrap_protocol_held") from None
    s.phase = "metadata"
    return None  # Only actual run frame can now call its original starting guard.


def write_first_turn_from_run(frame):
    _need(inspect.currentframe().f_back is frame)
    s = _actual_session(frame)
    import closer_coding_session_runtime as runtime

    plan = runtime._plan(frame)
    _need(
        s.phase == "final"
        and s.final_plan is plan
        and s.guard.used
        and not s.turn_written
        and plan.deadline_mono > time.monotonic()
    )
    s.check()
    # The substantive guard has just completed its fresh native identity,
    # confinement and original issue-context checks. Do not add another blocking
    # metadata round after that final original-context admission.
    _io_live(s)
    prompt = plan.delivery.prompt_utf8.decode("utf-8")
    _need(len(plan.delivery.prompt_utf8) <= json.loads(plan.budget_utf8)["max_prompt_utf8_bytes"])
    params = {
        "threadId": s.protocol._thread["id"],
        "input": [{"type": "text", "text": prompt}],
        "model": s.route.profile["requested_model"],
        "effort": s.route.profile["reasoning_effort"],
    }
    raw = (
        json.dumps(
            {"id": 1000, "method": "turn/start", "params": params},
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode()
    # Consume BEFORE the first byte. A partial/unknown write never receives a
    # replacement token, process, worker or attempt.
    s.turn_written, s.phase = True, "turn"
    _write_bytes(s, raw)
    return None


def finish_turn_from_run(frame):
    _need(inspect.currentframe().f_back is frame)
    s = _actual_session(frame)
    _need(s.phase == "turn" and s.turn_written)
    from closer_codex_appserver_bootstrap import TurnResponseData

    parsed = TurnResponseData(thread_id=s.protocol._thread["id"], max_bytes=s.output_limit)
    while not parsed.complete:
        raw = _next_native_frame(s)
        try:
            parsed.feed(raw)
        except ProtocolHeld:
            raise BootstrapHeld("native_turn_response_held") from None
    s.phase = "completed"
    s.child.stdin.close()
    s.child.stdin = None
    with selectors.DefaultSelector() as drain:
        for pipe, name in ((s.child.stdout, "stdout"), (s.child.stderr, "stderr")):
            drain.register(pipe, selectors.EVENT_READ, name)
        while drain.get_map():
            remaining = _io_live(s, allow_exit=True)
            for key, _ in drain.select(min(0.05, remaining)):
                room = s.output_limit - s.stdout_count - s.stderr_count
                chunk = os.read(key.fileobj.fileno(), min(8192, room + 1))
                if not chunk:
                    drain.unregister(key.fileobj)
                    continue
                if key.data == "stdout":
                    s.stdout_count += len(chunk)
                else:
                    s.stderr_count += len(chunk)
                _need(
                    s.stdout_count + s.stderr_count <= s.output_limit,
                    "owned_stdio_output_exhausted",
                )
    s.child.wait(timeout=_io_live(s, allow_exit=True))
    return parsed.text(), ""


def counted_bytes(frame):
    s = _SESSIONS.get((threading.get_ident(), frame))
    if s is None or not hasattr(s, "stdout_count"):
        return None
    return s.stdout_count, s.stderr_count


def _refresh_identity(s):
    """Fixed fresh native pair, not cached JSON, during actual final admission."""
    _need(s.phase in {"metadata", "final"} and s.refresh_count < 3)
    s.check()
    s.refresh_count += 1
    p = s.protocol
    _need(p._pending is None and p.snapshot()["ready_for_source_admission"])
    p._ready = False
    for method, params in (
        ("account/read", {"refreshToken": False}),
        (
            "account/rateLimits/read",
            {"supportsLunaReserve": False, "excludeResetCreditDetails": True},
        ),
    ):
        p._id += 1
        raw = (
            json.dumps({"id": p._id, "method": method, "params": params}, separators=(",", ":"))
            + "\n"
        ).encode()
        p._count(len(raw))
        p._pending = (p._id, method, 6)
        _write_bytes(s, raw)
        while p._pending is not None:
            chunk = _next_native_frame(s)
            try:
                p.feed(chunk, now=time.monotonic())
            except ProtocolHeld:
                raise BootstrapHeld("native_final_identity_changed") from None
    _need(p.snapshot()["ready_for_source_admission"])


def _confinement_source_call(s):
    import closer_codex_native_confinement as source

    caller = inspect.currentframe().f_back.f_back
    _need(
        caller.f_code is source.observe_effective_confinement.__code__
        and caller.f_globals is source.__dict__
        and caller.f_locals.get("session") is s
    )
    path = Path(source.__file__)
    pin = s.guard.controller.installation.source_closure.get(path.name)
    _need(pin == {"path": str(path), "sha256": source._LOADED_SHA})
    _physical(str(path), expected=source._LOADED_SHA)
    _need(s.phase in {"observing", "metadata", "final"} and not s.turn_written)
    s.check()


def _private_confinement_rpc(s, method, params):
    """Closed source callers only; raw results stay private transient DATA."""
    _need(method in {"config/read", "configRequirements/read", "command/exec"})
    _need(s.protocol is not None and s.protocol._pending is None)
    s.rpc_count = getattr(s, "rpc_count", 0) + 1
    _need(s.rpc_count <= 12, "confinement_metadata_call_bound_exhausted")
    request_id = 100000 + s.rpc_count
    raw = (
        json.dumps(
            {"id": request_id, "method": method, "params": params},
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode()
    _need(len(raw) <= min(65536, s.output_limit), "confinement_request_bound_exhausted")
    _write_bytes(s, raw)
    while True:
        line = _next_native_frame(s)
        try:
            from closer_codex_appserver_bootstrap import _constant, _pairs

            value = json.loads(line.decode(), object_pairs_hook=_pairs, parse_constant=_constant)
        except (ValueError, UnicodeError, ProtocolHeld):
            raise BootstrapHeld("confinement_native_frame_invalid") from None
        _need(type(value) is dict)
        if "method" in value:
            _need("id" not in value, "native_server_request_unsupported")
            try:
                s.protocol.feed(line, now=time.monotonic())
            except ProtocolHeld:
                raise BootstrapHeld("confinement_native_context_changed") from None
            continue
        _need(
            type(value.get("id")) is int
            and value["id"] == request_id
            and "error" not in value
            and type(value.get("result")) is dict,
            "confinement_native_response_unavailable",
        )
        return value["result"]


def confinement_configuration_from_source(session):
    _confinement_source_call(session)
    configuration = _private_confinement_rpc(
        session, "config/read", {"includeLayers": False, "cwd": session.route.cwd}
    )
    requirements = _private_confinement_rpc(session, "configRequirements/read", {})
    return configuration, requirements  # Caller immediately projects/discards.


def command_exec_probe_from_confinement(session, params):
    _confinement_source_call(session)
    _need(
        type(params) is dict
        and set(params) == {"command", "cwd", "permissionProfile", "timeoutMs", "outputBytesCap"}
    )
    profile = session.route.confinement
    _need(
        params["permissionProfile"] == session.route.permission
        and params["cwd"] == session.route.cwd
    )
    command = params["command"]
    _need(
        type(command) is list
        and 5 <= len(command) <= 32
        and all(type(v) is str and len(v.encode()) <= 65536 and "\0" not in v for v in command)
        and command[:5] == [profile.interpreter[0], "-I", "-S", "-B", profile.helper[0]]
    )
    remaining = _io_live(session)
    _need(
        type(params["timeoutMs"]) is int
        and 0 < params["timeoutMs"] <= int(remaining * 1000)
        and type(params["outputBytesCap"]) is int
        and 0
        < params["outputBytesCap"]
        <= session.output_limit - session.stdout_count - session.stderr_count
    )
    _need(
        not getattr(session, "probe_submitted", False),
        "native_confinement_probe_already_submitted",
    )
    # No repeat after partial/unknown native command delivery, even same session.
    session.probe_submitted = True
    return _private_confinement_rpc(session, "command/exec", params)


def require_final_transport_from_guard(guard, frame):
    caller = inspect.currentframe().f_back
    _need(
        caller.f_code is ctl._ProviderGuard.starting.__code__
        and caller.f_globals is ctl.__dict__
        and caller.f_back is frame
        and caller.f_locals.get("self") is guard
    )
    _need(selected(guard))
    _reader_live(guard.reader)
    route = route_for_run(frame)
    s = _SESSIONS.get((threading.get_ident(), frame))
    _need(
        route is not None
        and s is not None
        and s.route is route
        and route.guard is guard
        and s.phase == "metadata"
        and s.child is frame.f_locals.get("child")
    )
    reg = guard.controller.config.consumers[guard.consumer_id]
    _need(reg.get("transport_binding") == CHANNEL and "transport_argv_sha256" not in reg)
    import closer_coding_delivery as delivery

    observed = delivery._capture(frame)
    _need(
        observed is not None
        and observed.channel == CHANNEL
        and observed.transport_argv == route.transport_argv
        and observed.provider_argv == route.provider_argv
        and observed.cwd == route.cwd
        and observed.env == route.environment
    )
    return None


def _next_native_frame(s):
    while b"\n" not in s.native_buffer:
        s.native_buffer += _read_bytes(s)
        _need(len(s.native_buffer) <= s.output_limit, "native_frame_bound_exhausted")
    line, s.native_buffer = s.native_buffer.split(b"\n", 1)
    return line + b"\n"


def clear_transient_native_data(frame):
    _need(inspect.currentframe().f_back is frame)
    s = _SESSIONS.get((threading.get_ident(), frame))
    if s is not None:
        s.native_buffer = b""
        if s.protocol is not None:
            s.protocol._buffer = b""
            s.protocol._account = None
            s.protocol._native_account_id = None


def _pin_confinement_source(guard, module):
    path = Path(module.__file__)
    _need(
        guard.controller.installation.source_closure.get(path.name)
        == {"path": str(path), "sha256": module._LOADED_SHA},
        "confinement_source_installation_changed",
    )
    _physical(str(path), expected=module._LOADED_SHA)
