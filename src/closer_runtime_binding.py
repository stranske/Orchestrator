"""Production Runtime/Core binding. No network or credential discovery at import.

Public Core descriptor and its closed file set are immutable authority inputs;
package_root, policy locations and interpreter are private host bindings. Desktop
qualification requires a registered observer, never a caller-written receipt.
"""

from __future__ import annotations

import builtins
import hashlib
import json
import math
import os
import re
import sys
import tempfile
import threading
import time
import tomllib
import types
import urllib.error
import uuid
from http.client import HTTPMessage
from pathlib import Path
from typing import Any, Callable, Protocol, cast

# Bind child bootstrap to the source present when this module was loaded,
# never to a replacement discovered at transport construction/request time.
_BINDING_SOURCE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()

CORE_FILES = (
    "scripts/closer_runtime.py",
    "scripts/closer_operation_github.py",
    "scripts/check_checks_reported.py",
    "scripts/api_client.py",
    "scripts/reusable_ci_scope.py",
    "fixtures/closer-operation/v1/operation.schema.json",
    ".github/workflows/reusable-10-ci-python.yml",
    ".github/workflows/selftest-reusable-ci.yml",
    ".github/workflows/maint-62-integration-consumer.yml",
)
CORE_DEPENDENCIES = {"jsonschema": ">=4.22.0", "PyYAML": "6.0.3", "requests": "2.34.2"}
DESKTOP_UNSUPPORTED = "desktop observer unsupported: no registered desktop app/run metadata reader"


class BindingError(RuntimeError):
    pass


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class DesktopObserver(Protocol):
    """Installed controller implementation must read native events/tool results itself.

    observe receives a request identity, not claimed success. It returns native app,
    run, thread and phase artifact metadata obtained independently of the receipt.
    Implementations belong to the authorized desktop controller, not JSON input.
    """

    def observe(self, request_id: str, runner_id: str) -> dict[str, Any]: ...


_OBSERVERS: dict[str, DesktopObserver] = {}
_POLICY_READERS: dict[str, Callable[[], dict[str, Any]]] = {}


def register_observer(name: str, observer: DesktopObserver) -> None:
    if not name or name in _OBSERVERS:
        raise BindingError("observer identity missing or already registered")
    _OBSERVERS[name] = observer


def register_policy_reader(name: str, reader: Callable[[], dict[str, Any]]) -> None:
    if not name or name in _POLICY_READERS:
        raise BindingError("policy reader identity missing or already registered")
    _POLICY_READERS[name] = reader


def qualification_request(manifest: dict[str, Any], runners: list[str]) -> dict[str, Any]:
    """Controller consumes this on a scheduled tick; does not launch an app here."""
    from closer_campaign import RUNNER_PHASES, sha256_json

    binding = {
        k: manifest.get(k)
        for k in (
            "package_digest",
            "candidate_sha",
            "command_digest",
            "runtime_digest",
            "state_digest",
            "state_versions",
        )
    }
    return {
        "schema": "closer-desktop-request/v1",
        "request_id": sha256_json([binding, sorted(runners)]),
        "binding": binding,
        "runners": sorted(runners),
        "phases": list(RUNNER_PHASES),
    }


def desktop_problems(
    manifest: dict[str, Any], receipt: dict[str, Any], observer=None, *, enabled_runners=None
) -> list[str]:
    from closer_campaign import RUNNER_PHASES

    # An object passed directly is an integration seam for the controller/test,
    # never deserialized executable code or a receipt-selected module path.
    reader = observer or _OBSERVERS.get(receipt.get("observer_id", ""))
    if reader is None:
        return [DESKTOP_UNSUPPORTED]
    try:
        observed = reader.observe(receipt["request_id"], receipt["runner_id"])
        if enabled_runners is not None and sorted(observed["enabled_runners"]) != sorted(
            enabled_runners
        ):
            return ["desktop observation enabled runners differ from qualification request"]
        if receipt["runner_id"] not in observed["enabled_runners"]:
            return ["desktop runner is not independently observed enabled"]
        binding = qualification_request(manifest, observed["enabled_runners"])
        if receipt["request_id"] != binding["request_id"]:
            return ["desktop qualification request differs from pinned package/runners"]
        for key in ("app_id", "run_id", "thread_id", "producer_id"):
            if not isinstance(observed.get(key), str) or not observed[key]:
                return [f"independent desktop metadata lacks {key}"]
        if (
            observed.get("source") != "native-desktop-events"
            or observed.get("runner_id") != receipt["runner_id"]
        ):
            return ["desktop metadata is not a matching native observation"]
        for key, value in binding["binding"].items():
            if observed.get(key) != value or receipt.get(key) != value:
                return [f"desktop observation {key} differs from pin"]
        for phase in RUNNER_PHASES:
            native = observed["phases"][phase]
            ref = native["evidence"]
            if (
                native.get("passed") is not True
                or not native.get("native_event_id")
                or native != receipt["phases"][phase]
                or _hash(Path(ref["path"])) != ref["sha256"]
            ):
                return [f"desktop phase {phase} lacks independently observed hash-bound artifact"]
    except Exception as exc:
        return [f"desktop observation unverifiable: {type(exc).__name__}"]
    return []


def policy_problem(
    policy: dict[str, Any], *, now: float | None = None, mechanism: str | None = None
) -> str | None:
    """Configured worker pause wins over controller ACTIVE; unknown always holds.

    A fresh registered app reader can reuse an existing controller readback. It
    must bind its readback to hashes of the currently configured worker TOMLs.
    Self-reported JSON does not register an observer or grant authorization.
    """
    if mechanism not in (None, "desktop-scheduler/v1", "cli-offload/v1", "native-handoff/v1"):
        return "scheduled writes held: execution mechanism has no supported policy reader"
    if mechanism == "native-handoff/v1":
        from closer_native_observer import policy_problem as native_policy_problem

        return native_policy_problem(policy)
    if policy.get("schema") == "native-configured-policy/v1":
        return "scheduled writes held: native policy cannot authorize another mechanism"
    if policy.get("schema") == "cli-configured-policy/v1":
        if mechanism != "cli-offload/v1":
            return "scheduled writes held: CLI policy cannot authorize another mechanism"
        from closer_mechanisms import configured_policy_problem

        return configured_policy_problem(policy)
    if mechanism == "cli-offload/v1":
        return "scheduled writes held: CLI configured policy contract missing"
    paths = policy.get("worker_tomls")
    if not isinstance(paths, list) or not paths:
        return "scheduled writes held: worker pause policy missing"
    digests = {}
    try:
        for name in paths:
            path = Path(name)
            doc = tomllib.loads(path.read_text())
            if doc.get("status") != "ACTIVE":
                return f"scheduled writes held: configured worker {path.name} is paused or unknown"
            digests[str(path)] = _hash(path)
    except (OSError, ValueError, TypeError):
        return "scheduled writes held: configured worker policy unreadable"
    reader = _POLICY_READERS.get(policy.get("reader_id", ""))
    if reader is None:
        return "scheduled writes held: app automation_view unsupported; no verified policy reader"
    try:
        receipt = reader()
        age = (time.time() if now is None else now) - receipt["observed_at"]
        if not 0 <= age <= 300:
            return "scheduled writes held: app policy readback stale"
        if (
            receipt.get("source") != "controller-app-readback"
            or receipt.get("worker_toml_digests") != digests
            or receipt.get("controller_status") != "ACTIVE"
            or receipt.get("worker_statuses") != ["ACTIVE"] * len(paths)
        ):
            return "scheduled writes held: app worker/controller policy paused or unknown"
    except Exception:
        return "scheduled writes held: app policy readback unavailable"
    return None


def policy_for_mechanism(policy: dict[str, Any], *, mechanism: str | None = None) -> str | None:
    """Keep the original Desktop reader call unchanged; explicit CLI is separate."""
    if mechanism in (None, "desktop-scheduler/v1"):
        return policy_problem(policy)
    return policy_problem(policy, mechanism=mechanism)


def dependency_problems() -> list[str]:
    """Declared Core dependencies are a production capability, never auto-installed."""
    from importlib import metadata

    problems = []
    for name, required in CORE_DEPENDENCIES.items():
        try:
            actual = metadata.version(name)
        except metadata.PackageNotFoundError:
            problems.append(f"Core dependency missing: {name} {required}")
            continue
        if name == "jsonschema":
            parts = actual.split(".")
            compatible = (
                len(parts) >= 2
                and all(part.isdigit() for part in parts[:2])
                and tuple(map(int, parts[:2])) >= (4, 22)
            )
        else:
            compatible = actual == required
        if not compatible:
            problems.append(f"Core dependency unsupported: {name} {actual}; requires {required}")
    return problems


# Core namespaces and legacy helper sys.path restoration are process-global.
# Hold this only while executing verified module initialization, never Core effects.
_CORE_IMPORT_LOCK = threading.RLock()
_CORE_MISSING = object()


class CoreBundle:
    """Load a closed hash-bound Core package under a private import namespace.

    No generic `scripts` or `api_client` aliases, sys.path edits, or host import
    fallback. Core's own absolute imports are resolved within its verified bundle.
    """

    def __init__(self, pin: dict[str, Any]):
        self.root = Path(pin["package_root"]).resolve()
        descriptor = json.loads((self.root / "closer-core-package.json").read_text())
        files = descriptor.get("files") or {}
        expected = _digest(
            [
                "closer-core",
                "Workflows",
                "1",
                "closer-operation/v1",
                descriptor.get("source_commit"),
                sorted(files.items()),
                sorted(CORE_DEPENDENCIES.items()),
            ]
        )
        if (
            descriptor.get("schema_version") != 1
            or descriptor.get("package_kind") != "closer-core"
            or re.fullmatch(r"[0-9a-f]{40}", str(descriptor.get("source_commit", ""))) is None
            or set(files) != set(CORE_FILES)
            or descriptor.get("owner") != "Workflows"
            or descriptor.get("contract") != "closer-operation/v1"
            or descriptor.get("core_version") != "1"
            or descriptor.get("entrypoints") != {"runtime": "scripts/closer_runtime.py"}
            or descriptor.get("dependencies") != CORE_DEPENDENCIES
            or descriptor.get("package_digest") != expected
            or pin.get("package_digest") != expected
        ):
            raise BindingError("Core package descriptor/pin incompatible")
        self.sources = {}
        actual_files = {
            str(path.relative_to(self.root))
            for path in self.root.rglob("*")
            if path.is_file() or path.is_symlink()
        }
        if actual_files != set(CORE_FILES) | {"closer-core-package.json"}:
            raise BindingError("Core tree differs from closed package files")
        for name, digest in files.items():
            path = self.root / name
            data = path.read_bytes()
            if (
                not path.resolve().is_relative_to(self.root)
                or hashlib.sha256(data).hexdigest() != digest
            ):
                raise BindingError(f"Core package digest mismatch: {name}")
            self.sources[name] = data
        self.namespace = "_closer_core_" + expected
        self.loaded: dict[str, types.ModuleType] = {}
        self._loading: dict[str, types.ModuleType] = {}
        package = types.ModuleType(self.namespace)
        package.__path__ = [str(self.root / "scripts")]
        self.package = package
        with _CORE_IMPORT_LOCK:
            previous = {
                key: value
                for key, value in sys.modules.items()
                if key == self.namespace or key.startswith(self.namespace + ".")
            }
            sys.modules[self.namespace] = package
            try:
                self.runtime = self.load("closer_runtime")
                self.github = self.load("closer_operation_github")
            except BaseException:
                # A failed bundle must not discard another root's successful aliases.
                owned = {package, *self.loaded.values(), *self._loading.values()}
                for key, value in list(sys.modules.items()):
                    if (key == self.namespace or key.startswith(self.namespace + ".")) and (
                        value in owned
                    ):
                        if key in previous:
                            sys.modules[key] = previous[key]
                        else:
                            del sys.modules[key]
                self.loaded.clear()
                raise

    def load(self, name: str) -> types.ModuleType:
        with _CORE_IMPORT_LOCK:
            if name in self.loaded:
                return self.loaded[name]
            if name in self._loading:
                # Only the initializing thread can reenter this recursive lock;
                # preserve ordinary same-thread circular-import semantics.
                return self._loading[name]
            relative = f"scripts/{name}.py"
            if relative not in CORE_FILES:
                raise BindingError(f"Core import outside closed package: {name}")
            module = types.ModuleType(self.namespace + "." + name)
            path = self.root / relative
            module.__file__ = str(path)
            module.__package__ = self.namespace
            module.__dict__["__builtins__"] = {**vars(builtins), "__import__": self.import_owned}
            previous_module = sys.modules.get(module.__name__, _CORE_MISSING)
            previous_package = sys.modules.get(self.namespace, _CORE_MISSING)
            previous_child = getattr(self.package, name, _CORE_MISSING)
            self._loading[name] = module
            # Python decorators (including dataclasses) require an import-time
            # sys.modules entry. It is never returned as completed by load().
            sys.modules[self.namespace] = self.package
            sys.modules[module.__name__] = module
            setattr(self.package, name, module)
            original_path = list(sys.path)
            try:
                exec(compile(self.sources[relative], str(path), "exec"), module.__dict__)
            except BaseException:
                for key, owned, previous in (
                    (module.__name__, module, previous_module),
                    (self.namespace, self.package, previous_package),
                ):
                    if sys.modules.get(key) is owned:
                        if previous is _CORE_MISSING:
                            sys.modules.pop(key, None)
                        else:
                            sys.modules[key] = cast(types.ModuleType, previous)
                if getattr(self.package, name, _CORE_MISSING) is module:
                    if previous_child is _CORE_MISSING:
                        delattr(self.package, name)
                    else:
                        setattr(self.package, name, previous_child)
                raise
            finally:
                self._loading.pop(name, None)
                sys.path[:] = original_path  # legacy Core helper prepends its script directory
            self.loaded[name] = module
            return module

    def import_owned(self, name, globals=None, locals=None, fromlist=(), level=0):
        if level == 0 and (name == "scripts" or name.startswith("scripts.")):
            if name == "scripts":
                for child in fromlist:
                    self.load(child)
                return self.package
            module = self.load(name.split(".", 1)[1])
            return module if fromlist else self.package
        if level == 0 and f"scripts/{name}.py" in CORE_FILES:
            return self.load(name)
        return builtins.__import__(name, globals, locals, fromlist, level)


_CORE_HTTP_PROTOCOL_BYTES = 2 * 100_000_000  # preserve Core's existing Git blob allowance
_CORE_HTTP_CHILD = r"""
import math,signal,sys,time
try:
    deadline=float(sys.argv[1])
    remaining=deadline-time.monotonic()
    if not math.isfinite(deadline) or not 0 < remaining <= 30:
        raise ValueError("original Core HTTP deadline expired or invalid")
    # Default SIGALRM terminates even a stuck response read and survives loss of
    # the parent supervisor. No new duration is minted in this child.
    signal.signal(signal.SIGALRM,signal.SIG_DFL)
    remaining=deadline-time.monotonic()
    if remaining <= 0:
        raise TimeoutError("original Core HTTP deadline expired")
    signal.setitimer(signal.ITIMER_REAL,remaining)
except BaseException:
    sys.stdout.write('{"schema":"core-http-result/v1","status":"unknown"}')
    raise SystemExit(0)
import hashlib,json,types,urllib.error
from pathlib import Path
packet=json.load(sys.stdin)
try:
    if packet.get("request_deadline_mono") != deadline:
        raise ValueError("original Core HTTP deadline differs")
    path=Path(packet["loader_path"])
    source=path.read_bytes()
    if hashlib.sha256(source).hexdigest()!=packet["loader_sha256"]:
        raise ValueError("binding source changed")
    module=types.ModuleType("_owned_core_http_binding")
    module.__file__=str(path)
    sys.modules[module.__name__]=module
    exec(compile(source,str(path),"exec"),module.__dict__)
    bundle=module.CoreBundle(packet["pin"])
    try:
        if time.monotonic() >= deadline:
            raise TimeoutError("original Core HTTP deadline expired before HTTP")
        value=bundle.github.NativeGitHubTransport()(packet["method"],packet["path"],packet["body"])
        result={"schema":"core-http-result/v1","status":"ok","value":value}
    except urllib.error.HTTPError as error:
        result={"schema":"core-http-result/v1","status":"http_error","code":error.code,
                "rate_limited":bool(bundle.github._rate_limited(error)),
                "headers":{key:value for key,value in (error.headers or {}).items()
                           if key.lower() in {"retry-after","x-ratelimit-remaining","x-ratelimit-reset","x-ratelimit-resource"}}}
        error.close()
    except bundle.runtime.PolicyDenied:
        result={"schema":"core-http-result/v1","status":"denied"}
    raw=json.dumps(result,separators=(",",":"))
    if len(raw.encode())>packet["max_bytes"]:
        raise ValueError("Core HTTP protocol too large")
except BaseException:
    raw='{"schema":"core-http-result/v1","status":"unknown"}'
sys.stdout.write(raw)
"""


class _CoreHTTPTransport:
    """One cancellable default Core HTTP operation; never a frontier-wide timer."""

    def __init__(self, bundle: CoreBundle):
        self.bundle = bundle

    def __call__(self, method, path, body):
        import closer_resources
        import dispatcher

        try:
            bootstrap_matches = _hash(Path(__file__)) == _BINDING_SOURCE_SHA256
        except OSError as exc:
            raise BindingError("Core HTTP binding source changed") from exc
        if not bootstrap_matches:
            raise BindingError("Core HTTP binding source changed")
        if not self.bundle.github._allowed_native_call(method, path):
            raise self.bundle.runtime.PolicyDenied("unsupported native API operation")
        # Keep the existing Core socket limit, additionally enforcing TOTAL time
        # in an owned process. The callback's original remaining window can only
        # shorten this request; it never renews an expired callback.
        started = time.monotonic()
        request_limit = started + min(30.0, float(self.bundle.load("api_client").DEFAULT_TIMEOUT))
        sampled_mono = time.monotonic()
        remaining = closer_resources.finite_request_remaining()
        if remaining is not None:
            request_limit = min(request_limit, sampled_mono + remaining)
        timeout = request_limit - time.monotonic()
        if not math.isfinite(timeout) or timeout <= 0:
            raise TimeoutError("Core HTTP original request window expired")
        binding = closer_resources.finite_pool_current_binding()
        observation = {
            "run_id": "core-http-" + uuid.uuid4().hex,
            "agent": "github",
            "profile_id": "github-api",
            **(
                {
                    "finite_frontier_id": binding["frontier_id"],
                    "finite_frontier_target": binding["target"],
                }
                if binding
                else {}
            ),
        }
        packet = {
            "loader_path": str(Path(__file__).resolve()),
            "loader_sha256": _BINDING_SOURCE_SHA256,
            "pin": {
                "package_root": str(self.bundle.root),
                "package_digest": self.bundle.namespace.removeprefix("_closer_core_"),
            },
            "method": method,
            "path": path,
            "body": body,
            "max_bytes": _CORE_HTTP_PROTOCOL_BYTES,
        }
        # Anonymous mode-0600 owned input carries no credentials. The child uses
        # the existing GH_TOKEN/GITHUB_TOKEN environment, exactly as native Core.
        with tempfile.TemporaryFile(mode="w+") as request_input:
            sampled_mono = time.monotonic()
            current = closer_resources.finite_request_remaining()
            if current is not None:
                request_limit = min(request_limit, sampled_mono + current)
            packet["request_deadline_mono"] = request_limit
            json.dump(packet, request_input)
            if request_input.tell() > _CORE_HTTP_PROTOCOL_BYTES:
                raise BindingError("Core HTTP request protocol too large")
            request_input.seek(0)
            # Serialization/setup consumes this same request, not a new window.
            timeout = request_limit - time.monotonic()
            if timeout <= 0:
                raise TimeoutError("Core HTTP original request window expired")
            done = dispatcher.run_observed_offload(
                [sys.executable, "-c", _CORE_HTTP_CHILD, str(request_limit)],
                callback=(
                    closer_resources.finite_pool_process_started if binding else lambda _: None
                ),
                starting_callback=closer_resources.finite_pool_prepare_request if binding else None,
                finished_callback=(
                    closer_resources.finite_pool_process_finished if binding else None
                ),
                observation=observation,
                cwd=None,
                timeout=timeout,
                stdin=request_input,
            )
        if done.returncode != 0 or len((done.stdout or "").encode()) > _CORE_HTTP_PROTOCOL_BYTES:
            raise BindingError("Core HTTP request result unavailable")
        try:
            result = json.loads(done.stdout)
        except (TypeError, ValueError) as exc:
            raise BindingError("Core HTTP request protocol invalid") from exc
        if not isinstance(result, dict) or result.get("schema") != "core-http-result/v1":
            raise BindingError("Core HTTP request protocol invalid")
        if result.get("status") == "denied":
            raise self.bundle.runtime.PolicyDenied("native Core HTTP denied; no fallback")
        if result.get("status") == "http_error":
            code, headers = result.get("code"), result.get("headers")
            limited = result.get("rate_limited")
            if (
                type(code) is not int
                or not 400 <= code <= 599
                or not isinstance(headers, dict)
                or type(limited) is not bool
                or any(
                    not isinstance(key, str) or not isinstance(value, str)
                    for key, value in headers.items()
                )
            ):
                raise BindingError("Core HTTP error protocol invalid")
            http_headers = HTTPMessage()
            for key, value in headers.items():
                http_headers[key] = value
            reason = (
                "native Core HTTP rate limit exceeded" if limited else "native Core HTTP refused"
            )
            error = urllib.error.HTTPError(path, code, reason, http_headers, None)
            error.close()
            raise error
        if result.get("status") != "ok":
            raise BindingError("Core HTTP request outcome unknown; reconcile original operation")
        return result.get("value")


class RuntimeBinding:
    def __init__(
        self,
        bundle: CoreBundle,
        policy: dict[str, Any],
        *,
        transport=None,
        mechanism: str | None = None,
    ):
        self.bundle, self.policy = bundle, policy
        self.mechanism = mechanism
        self.transport = transport
        self.adapters: dict[str, Any] = {}

    def _adapter(self, repository):
        from closer_campaign import PinnedCoreAdapter

        repository = repository.lower()
        adapter = self.adapters.get(repository)
        if adapter is None:
            token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
            if self.transport is None and not token:
                raise BindingError("shared Core requires independently supplied env token")
            request = self.transport or _CoreHTTPTransport(self.bundle)
            store = self.bundle.github.GitHubContentsOperationStore(request, repository)
            adapter = PinnedCoreAdapter(self.bundle.runtime, ".", store=store)
            self.adapters[repository] = adapter
        if not adapter.shared:
            raise BindingError("local Core store cannot authorize remote effects")
        return adapter

    def _native_pr(self, repository, number):
        """Fixed GET observation, separate from the authority Contents transport."""
        path = f"repos/{repository}/pulls/{number}"
        if self.transport is not None:  # trusted offline transport injection
            return self.transport("GET", "/" + path, None)
        producer = self.bundle.runtime._load_producer()
        return producer.Evidence(producer.native_gh_pages).one(path)

    def _recover(self, adapter, record, holder, *, native_result=None):
        """Reconcile the stored operation, never a spec rebuilt from a fresh snapshot."""
        from closer_campaign import sha256_json

        core = self.bundle.runtime
        if record.get("denial") or record.get("status") == "denied":
            return {"status": "blocked", "reason": "Core denied/no_fallback fence"}
        binding = (record.get("mutation_intent") or {}).get("native_binding")
        if (
            not isinstance(binding, dict)
            or record["guard_versions"].get("native_binding") != sha256_json(binding)
            or binding.get("head") != record["head_sha"]
            or binding.get("criteria_fingerprint") != record["criteria_fingerprint"]
            or binding.get("review_revision") != record["review_revision"]
            or binding.get("pr_target") != f"{record['repository']}#{record['target']['number']}"
            or (binding.get("action") == "merge" and record["action"] != "merge")
            or (
                binding.get("action") in {"initiate_verify", "post_merge_verify"}
                and record["action"] != "verify_request"
            )
            or binding.get("action") not in {"merge", "initiate_verify", "post_merge_verify"}
        ):
            return {"status": "unknown", "reason": "original native binding unavailable"}
        if record["status"] == "succeeded":
            return {"status": "completed", "core_reconciled": True, "already_completed": True}
        rejection_proof = getattr(core, "verification_request_not_accepted", None)
        if callable(rejection_proof) and rejection_proof(record):
            return {
                "status": "not_started",
                "core_rejected_not_accepted": True,
                "reason": "exact verification POST was authoritatively rejected (HTTP 422)",
                "next_trigger": "compatible reviewed consumer workflow and valid event inputs",
            }
        if record["status"] not in {"intent_recorded", "submitted", "uncertain"}:
            return {"status": "unknown", "reason": "Core operation has no submitted effect"}
        try:
            pr = self._native_pr(record["repository"], record["target"]["number"])
            observed = (
                (pr.get("head") or {}).get("sha") == binding["head"]
                and pr.get("merged") is True
                and re.fullmatch(r"[0-9a-f]{40}", str(pr.get("merge_commit_sha", ""))) is not None
            )
            if record["action"] == "verify_request":
                observed = (
                    observed
                    and re.fullmatch(r"[0-9a-f]{40}", str(binding.get("merge", ""))) is not None
                    and pr.get("merge_commit_sha") == binding["merge"]
                    and any(
                        isinstance(label, dict) and label.get("name") == "verify:compare"
                        for label in pr.get("labels", [])
                    )
                )
            mutation = binding.get("verification_mutation")
            if record["action"] == "verify_request" and not isinstance(mutation, dict):
                return {
                    "status": "unknown",
                    "reason": "legacy verification mutation kind is not established",
                }
            if record["action"] == "verify_request" and isinstance(mutation, dict):
                if mutation.get("mutation_kind") == "workflow_dispatch":
                    # A pre-existing label cannot witness a fresh workflow POST.
                    acknowledgement = (native_result or {}).get("verification_post_accepted")
                    observed = (
                        (pr.get("head") or {}).get("sha") == binding["head"]
                        and pr.get("merged") is True
                        and pr.get("merge_commit_sha") == binding["merge"]
                        and acknowledgement == mutation
                        and (native_result or {}).get("status") == "completed"
                    )
                elif mutation.get("mutation_kind") != "label":
                    observed = False
            if not observed:
                return {"status": "unknown", "reason": "original native effect not observed"}
            spec = core.spec_from_record(record)
            acknowledged_dispatch = (
                record["action"] == "verify_request"
                and isinstance(mutation, dict)
                and mutation.get("mutation_kind") == "workflow_dispatch"
            )
            observation = {"pr": pr, "accepted_request": mutation} if acknowledged_dispatch else pr
            evidence = core.ReconciliationEvidence(
                operation_key=record["operation_key"],
                target_fence_key=record["target_fence_key"],
                head_sha=record["head_sha"],
                action=record["action"],
                result="desired_result_observed",
                source=(
                    f"repos/{record['repository']}/actions/workflows/agents-verifier.yml/dispatches"
                    if acknowledged_dispatch
                    else f"repos/{record['repository']}/pulls/{record['target']['number']}"
                ),
                observation_digest=_digest(observation),
                context=json.dumps(
                    {
                        "native_binding": binding,
                        "merge": pr.get("merge_commit_sha"),
                        **({"accepted_request": mutation} if acknowledged_dispatch else {}),
                    },
                    sort_keys=True,
                ),
            )
            core.reconcile_unknown(
                adapter.store,
                record["target_fence_key"],
                expected_revision=record["lease"]["revision"],
                expected_epoch=record["lease"]["epoch"],
                spec=spec,
                executor=core.ExecutorIdentity("orchestrator_local", holder),
                evidence=evidence,
            )
        except Exception as exc:
            return {
                "status": "unknown",
                "reason": f"Core readback unresolved: {type(exc).__name__}",
            }
        return {"status": "completed", "core_reconciled": True, "evidence": pr}

    def reconcile(self, target, operation):
        """Native Closer.reconcile integration seam; never submits a native effect.

        The operation's retained original binding is checked against the Core intent.
        Readback can drain an expired attempt; it never grants execution time or resets denial.
        """
        problem = policy_for_mechanism(self.policy, mechanism=self.mechanism)
        if problem:
            return {"status": "blocked", "reason": problem}
        action = operation.get("action")
        canonical = {
            "merge": "merge",
            "initiate_verify": "verify_request",
            "post_merge_verify": "verify_request",
        }.get(action)
        original = operation.get("binding") or {}
        try:
            pr_target = original.get("pr_target", target).lower()
            repository, number = pr_target.split("#")
            adapter = self._adapter(repository)
            core = self.bundle.runtime
            # Target/action alone selects the retained record. Its spec is reconstructed
            # only by _recover after the original queue binding has matched.
            spec = core.OperationSpec(
                repository,
                int(number),
                canonical,
                "0" * 40,
                None,
                "0" * 64,
                "lookup",
                {"authority": 2},
            )
            record = adapter.store.read(core.canonical_target_fence_key(spec))
            if record and (record.get("denial") or record["status"] == "denied"):
                return {"status": "blocked", "reason": "Core denied/no_fallback fence"}
            native = ((record or {}).get("mutation_intent") or {}).get("native_binding") or {}
            if (
                canonical is None
                or not record
                or native.get("action") != action
                or original.get("head") != native.get("head")
                or original.get("review_revision") != native.get("review_revision")
                or (
                    native.get("queue_operation_id") is not None
                    and operation.get("id") != native["queue_operation_id"]
                )
                or (
                    operation.get("revision") is not None
                    and operation["revision"] != native.get("revision")
                )
                or original.get("source_targets", []) != native.get("source_targets", [])
                or (canonical == "verify_request" and original.get("merge") != native.get("merge"))
            ):
                return {"status": "unknown", "reason": "original queue/Core binding differs"}
            return self._recover(adapter, record, operation["owner"])
        except Exception as exc:
            return {
                "status": "unknown",
                "reason": f"Core reconciliation unavailable: {type(exc).__name__}",
            }

    def execute(
        self, target, action, snapshot, operation, native_effect, *, verification_mutation=None
    ):
        from closer_campaign import (
            ActionSpec,
            VerificationRequestNotAccepted,
            sha256_json,
            submit_effect,
        )

        dependencies = dependency_problems()
        if dependencies:
            return {"status": "blocked", "reason": "; ".join(dependencies)}
        deadline = operation.get("deadline")
        if (
            not isinstance(deadline, (int, float))
            or isinstance(deadline, bool)
            or not time.time() < deadline < float("inf")
        ):
            return {
                "status": "blocked",
                "reason": "original native action deadline missing or exhausted",
            }
        reason = policy_for_mechanism(self.policy, mechanism=self.mechanism)
        if reason:
            return {"status": "blocked", "reason": reason}
        canonical = {
            "merge": "merge",
            "initiate_verify": "verify_request",
            "post_merge_verify": "verify_request",
        }.get(action)
        if canonical is None:
            return {"status": "blocked", "reason": f"Core capability unsupported: {action}"}
        pr_target = snapshot.get("pr_target", target)
        try:
            repository, number = pr_target.split("#")
            adapter = self._adapter(repository)
            native_binding = {
                "action": action,
                "pr_target": f"{repository.lower()}#{int(number)}",
                "head": snapshot["head"],
                "merge": snapshot.get("merge"),
                "criteria": snapshot.get("criteria", snapshot.get("revision")),
                "criteria_fingerprint": sha256_json(
                    snapshot.get("criteria", snapshot.get("revision"))
                ),
                "review_revision": snapshot["review_revision"],
                "revision": snapshot.get("revision"),
                "queue_operation_id": operation.get("id"),
                "source_targets": [row["target"] for row in snapshot.get("sources", [])],
                "sources": snapshot.get("sources", []),
            }
            if canonical == "verify_request":
                if verification_mutation is None:
                    if "verify:compare" in snapshot.get("labels", []):
                        return {
                            "status": "blocked",
                            "reason": "repeat verification requires an exact prepared mutation",
                        }
                    mutation = {"mutation_kind": "label", "request_basis": None}
                else:
                    mutation = {
                        "mutation_kind": verification_mutation.get("mutation_kind"),
                        "request_basis": verification_mutation.get("request_basis"),
                    }
                    if mutation["mutation_kind"] not in {
                        "label",
                        "workflow_dispatch",
                    } or not re.fullmatch(r"[0-9a-f]{64}", str(mutation["request_basis"])):
                        return {
                            "status": "blocked",
                            "reason": "invalid prepared verification mutation",
                        }
                correlation = (
                    (verification_mutation or {})
                    .get("fields", {})
                    .get("inputs[verification_request_basis]")
                )
                if correlation is not None:
                    if (
                        mutation["mutation_kind"] != "workflow_dispatch"
                        or correlation != mutation["request_basis"]
                    ):
                        return {
                            "status": "not_started",
                            "reason": "verification correlation input differs",
                        }
                    mutation["correlation_input"] = correlation
                    import closer_verifier_followup

                    native_binding["verification_source_requirements"] = (
                        closer_verifier_followup.source_requirements(snapshot)
                    )
                if mutation["mutation_kind"] == "workflow_dispatch" and not (
                    callable(getattr(self.bundle.runtime, "reject_not_accepted", None))
                    and callable(getattr(self.bundle.runtime, "VerificationRequestRejected", None))
                    and callable(
                        getattr(self.bundle.runtime, "verification_request_not_accepted", None)
                    )
                ):
                    return {
                        "status": "not_started",
                        "reason": "qualified Core lacks typed verification rejection support",
                        "next_trigger": "reviewed compatible Core publication",
                    }
                native_binding["verification_mutation"] = mutation
            spec = ActionSpec(
                repository,
                int(number),
                canonical,
                snapshot["head"],
                sha256_json(snapshot.get("criteria", snapshot.get("revision"))),
                snapshot["review_revision"],
                snapshot.get("base"),
                blocking_labels=tuple(snapshot.get("labels") or []),
                native_binding=native_binding,
            )
        except (KeyError, ValueError, TypeError, BindingError) as exc:
            return {"status": "blocked", "reason": f"Core binding unavailable: {exc}"}
        # Exact same completed operation is idempotent; a different spec cannot
        # borrow the old observation, including changed criteria or merge identity.
        try:
            ospec = adapter._spec(spec)
            existing = adapter.store.read(adapter.target_fence_key(spec))
            if existing is not None and existing.get("status") in {
                "intent_recorded",
                "submitted",
                "uncertain",
                "succeeded",
                "denied",
            }:
                if existing.get("denial") or existing["status"] == "denied":
                    return {"status": "blocked", "reason": "Core denied/no_fallback fence"}
                if existing["operation_key"] == self.bundle.runtime.canonical_operation_key(ospec):
                    return self._recover(adapter, existing, operation["owner"])
                if existing["status"] != "succeeded":
                    return {"status": "unknown", "reason": "prior Core spec remains unresolved"}
        except Exception as exc:
            return {"status": "blocked", "reason": f"Core read unavailable: {type(exc).__name__}"}
        result: dict[str, Any] = {}

        class NativePort:
            def submit(self, operation_key, action_doc):
                if time.time() >= deadline:
                    raise TimeoutError("original native action deadline exhausted before effect")
                result.update(native_effect())  # all original native guards still execute
                rejection = result.get("verification_rejected")
                if rejection is not None:
                    raise VerificationRequestNotAccepted(rejection)
                # Anything after submission remains fenced pending independent readback.
                return "ok" if result.get("status") == "completed" else "unknown"

        outcome = submit_effect(adapter, NativePort(), spec, {}, holder=operation["owner"])
        if outcome == "not_accepted":
            return {**result, "core_rejected_not_accepted": True}
        if outcome in {"ok", "unknown"}:
            try:
                record = adapter.store.read(adapter.target_fence_key(spec))
                recovered = (
                    self._recover(adapter, record, operation["owner"], native_result=result)
                    if record
                    else {}
                )
            except Exception:
                recovered = {}
            if recovered.get("status") == "completed":
                # Retain correlation only from this ACTUAL acknowledged POST and
                # its exact succeeded original Core intent. An old succeeded
                # fence without this native_result never gets upgraded here.
                acknowledged = result.get("verification_post_accepted")
                try:
                    settled = adapter.store.read(adapter.target_fence_key(spec))
                    original = (settled.get("mutation_intent") or {}).get("native_binding")
                    if (
                        isinstance(acknowledged, dict)
                        and acknowledged.get("mutation_kind") == "workflow_dispatch"
                        and acknowledged.get("correlation_input")
                        == acknowledged.get("request_basis")
                        and re.fullmatch(
                            r"[0-9a-f]{64}", str(acknowledged.get("correlation_input"))
                        )
                        and settled.get("status") == "succeeded"
                        and settled.get("action") == "verify_request"
                        and isinstance(original, dict)
                        and original.get("queue_operation_id") == operation["id"]
                        and original.get("verification_mutation") == acknowledged
                        and settled["guard_versions"].get("native_binding") == sha256_json(original)
                        and settled["operation_key"]
                        == self.bundle.runtime.canonical_operation_key(ospec)
                    ):
                        result["core_request_correlation"] = {
                            "schema": "closer-core-request-correlation/v1",
                            "operation_id": operation["id"],
                            "core_operation_key": settled["operation_key"],
                            "core_target_fence_key": settled["target_fence_key"],
                            "core_package_digest": self.bundle.namespace.removeprefix(
                                "_closer_core_"
                            ),
                            "native_binding": original,
                            "native_binding_sha256": sha256_json(original),
                        }
                except (OSError, ValueError, KeyError, TypeError, AttributeError):
                    pass  # Missing proof is unknown correlation, not resend authority.
                return {**result, **recovered}
        return {
            "status": "unknown" if outcome in {"ok", "unknown"} else "blocked",
            "reason": f"shared Core fence: {outcome}",
            "native_result": result,
        }


def binding_from_manifest(path: str | Path) -> RuntimeBinding | None:
    from closer_campaign import load_manifest

    try:
        manifest = load_manifest(path)
        from closer_campaign import qualify

        if qualify(manifest, Path(path).parent):
            return None
        from closer_mechanisms import mechanism

        return RuntimeBinding(
            CoreBundle(manifest["portable"]),
            manifest.get("policy") or {},
            mechanism=mechanism(manifest),
        )
    except (OSError, ValueError, KeyError, TypeError, BindingError):
        return None  # caller requires binding and holds remote actions; local work continues


class NativeRoundHooks(Protocol):
    """Native helper migration seam. Token-safe cleanup is a required capability.

    Existing handoff-postrun.sh has an unconditional rm -rf EXIT trap and cannot
    implement this contract. A replacement must compare the original exact token
    at cleanup itself, not just in the caller before spawning it.
    """

    def prerun(self, deadline: float) -> tuple[int, str | None]: ...
    def owns(self, token: str) -> bool: ...
    def postrun(self, token: str, deadline: float) -> int: ...


class PinnedNativeHooks:
    """Supported native script adapter; never patches or installs those scripts.

    Both helper artifacts must implement HANDOFF_EXACT_TOKEN_V1: prerun refuses
    any existing lane (no age-based reaping), and postrun compares the complete
    expected meta token inside cleanup. Structured cleanup additionally requires
    the pinned renderer protocol and this invocation's nonce and credential.
    Paths, hashes and actor identity are private, independently provisioned config.
    """

    CAPABILITY = "HANDOFF_EXACT_TOKEN_V1"

    def __init__(self, cfg: dict[str, Any], *, runner, now=time.time):
        pin = cfg.get("native_hooks") or {}
        self.cfg, self.runner, self.now = cfg, runner, now
        self.bounded = pin.get("bounded_no_new_relay") is True
        self.dependencies = {}
        self.assignment_source = (
            Path.home()
            / ".codex/automations/reviewed-repo-automation-controller/closer-assignment.json"
        )
        self.interpreter_anchor = Path(sys.executable)
        self.dispatcher_anchor = Path.home() / ".codex/orchestrator-mirror/closer_dispatcher.py"
        if self.bounded:
            dependencies = pin.get("postrun_dependencies") or {}
            admitted_assignment = (
                Path(cfg["dispatch_state"]).parent / "native-assignment-snapshot.json"
            )
            if cfg.get("native_admission") is not None:
                from closer_campaign import load_manifest
                from closer_native_observer import validate_admission

                manifest = load_manifest(cfg["runtime_manifest"])
                context = validate_admission(manifest, cfg["native_admission"], for_effects=True)
                if (
                    cfg.get("mechanism") != "native-handoff/v1"
                    or str(Path(cfg["runtime_manifest"]).resolve()) != context["runtime_manifest"]
                    or (
                        context["schema"] == "native-invocation-admission/v1"
                        and cfg.get("original_deadline") != context["original_deadline"]
                    )
                    or (
                        context["schema"] == "native-invocation-admission/v2"
                        and (
                            cfg.get("finite_frontier") != context["finite_frontier"]
                            or cfg.get("request_timeout_seconds")
                            != context["request_timeout_seconds"]
                            or any(
                                key in cfg
                                for key in (
                                    "original_deadline",
                                    "budget_seconds",
                                    "dispatch_budget_seconds",
                                )
                            )
                        )
                    )
                    or cfg.get("capacity_report") != context["report"]["path"]
                    or dependencies.get("assignment")
                    != {key: context["assignment"][key] for key in ("path", "sha256")}
                ):
                    raise BindingError("native admission cfg differs from signed invocation")
                admitted_assignment = Path(context["assignment"]["path"])
                if "demand_assignment" in context:
                    self.assignment_source = Path(context["demand_assignment"]["path"])
            canonical = {
                "interpreter": self.interpreter_anchor.resolve(),
                "dispatcher": self.dispatcher_anchor.resolve(),
                "assignment": admitted_assignment,
            }
            for name, expected in canonical.items():
                entry = dependencies.get(name) or {}
                path = Path(entry.get("path", ""))
                if (
                    path != expected
                    or path.resolve() != expected
                    or path.is_symlink()
                    or not path.is_file()
                    or _hash(path) != entry.get("sha256")
                ):
                    raise BindingError("native postrun physical dependency pin mismatch: " + name)
                self.dependencies[name] = (path, entry["sha256"])
            if _hash(canonical["dispatcher"]) != _hash(
                Path(__file__).with_name("closer_dispatcher.py")
            ):
                raise BindingError("native installed dispatcher differs from pinned Runtime")
            if cfg.get("assignment") != str(canonical["assignment"]):
                raise BindingError("native B/C must share immutable admitted assignment snapshot")
        self.scripts = {}
        module_pin = pin.get("lease_module") or {}
        self.lease_module = Path(module_pin.get("path", ""))
        self.lease_digest = module_pin.get("sha256")
        if (
            self.lease_module.resolve()
            != Path(__file__).with_name("closer_native_lease.py").resolve()
            or not self.lease_module.is_file()
            or _hash(self.lease_module) != self.lease_digest
        ):
            raise BindingError("native exact-token lease module missing or outside pinned Runtime")
        self.script_digests = {}
        for phase in ("prerun", "postrun"):
            entry = pin.get(phase) or {}
            path = Path(entry.get("path", ""))
            try:
                data = path.read_bytes()
            except OSError as exc:
                raise BindingError(
                    "native ACTION A/C adapter unsupported: exact-token helper pin missing"
                ) from exc
            if (
                hashlib.sha256(data).hexdigest() != entry.get("sha256")
                or self.CAPABILITY.encode() not in data
                or (
                    self.bounded
                    and (
                        (
                            phase == "postrun"
                            and (
                                b"CLOSER_BOUNDED_NO_RELAY_V1" not in data
                                or (
                                    cfg.get("native_admission") is not None
                                    and b"CLOSER_NAMED_ROUND_V1" not in data
                                )
                            )
                        )
                        or (phase == "prerun" and b"CLOSER_NATIVE_ADMISSION_V1" not in data)
                    )
                )
            ):
                raise BindingError(
                    "native ACTION A/C adapter unsupported: installed helpers are not pinned "
                    "for exact-token cleanup; preserving lane and successor owners"
                )
            self.scripts[phase] = path
            self.script_digests[phase] = entry["sha256"]
        actor = cfg.get("handoff_agent")
        if actor not in {"codex", "claude_code", "cursor", "gemini", "muse"}:
            raise BindingError("native ACTION A/C actor identity missing/unsupported")
        self.env = {
            **os.environ,
            "HANDOFF_AGENT": actor,
            "HANDOFF_LANE": "closer",
            "HANDOFF_REFUSE_EXISTING_LANE": "1",
            "CLOSER_ROUND_STATE": str(cfg["dispatch_state"]),
        }
        self.env.pop("CLOSER_POSTRUN_ROUND_ID", None)
        self.env["CLOSER_POSTRUN_REQUIRE_NAMED_ROUND"] = (
            "1" if cfg.get("native_admission") is not None else "0"
        )
        handoff = Path(
            cfg.get("handoff_dir")
            or os.environ.get("HANDOFF_DIR")
            or Path.home() / ".codex/handoff"
        )
        self.lane = handoff / ".lane-closer.lock"
        self.env["HANDOFF_LANE_MUTEX_DIR"] = str(self.lane)
        self.env["HANDOFF_HANDOFF_ROOT"] = str(handoff)
        self.env["CLOSER_NATIVE_LEASE_PY"] = str(self.lease_module)
        self.env["CLOSER_NATIVE_LEASE_INTERPRETER"] = (
            str(self.dependencies["interpreter"][0]) if self.bounded else sys.executable
        )
        if cfg.get("capacity_report"):
            self.env["CLOSER_CAPACITY_REPORT"] = str(cfg["capacity_report"])
        if self.bounded:
            self.env.update(
                CLOSER_NO_NEW_RELAY="1",
                CLOSER_POSTRUN_PYTHON=str(self.dependencies["interpreter"][0]),
                CLOSER_POSTRUN_DISPATCHER=str(self.dependencies["dispatcher"][0]),
                CLOSER_POSTRUN_ASSIGNMENT=str(self.dependencies["assignment"][0]),
            )

    def _pins_match(self) -> bool:
        try:
            if self.bounded:
                path, digest = self.dependencies["interpreter"]
                if (
                    self.interpreter_anchor.resolve() != path
                    or path.is_symlink()
                    or _hash(path) != digest
                ):
                    return False
            return _hash(self.lease_module) == self.lease_digest and all(
                _hash(path) == self.script_digests[phase] for phase, path in self.scripts.items()
            )
        except OSError:
            return False

    def _dependencies_match(self, *, admission=False):
        try:
            valid = all(
                not path.is_symlink() and path.resolve() == path and _hash(path) == digest
                for path, digest in self.dependencies.values()
            )
            if self.bounded:
                valid = (
                    valid
                    and self.dispatcher_anchor.resolve() == self.dependencies["dispatcher"][0]
                    and self.interpreter_anchor.resolve() == self.dependencies["interpreter"][0]
                )
            if admission and self.bounded:
                valid = (
                    valid
                    and not self.assignment_source.is_symlink()
                    and self.assignment_source.resolve() == self.assignment_source
                    and _hash(self.assignment_source) == self.dependencies["assignment"][1]
                )
            return valid
        except OSError:
            return False

    def prerun(self, deadline: float) -> tuple[int, str | None]:
        if not self._pins_match() or not self._dependencies_match(admission=True):
            return 65, None
        if self.lane.exists():
            return 65, None  # unknown or expired owner is still an owner
        if self.bounded:
            import secrets

            self.env["CLOSER_ADMISSION_NONCE"] = secrets.token_hex(24)
        result = self.runner(
            [str(self.scripts["prerun"]), "closer"],
            env=self.env,
            capture_output=True,
            check=False,
            timeout=max(0.001, deadline - self.now()),
        )
        if result.returncode != 0:
            return result.returncode, None
        try:
            token = (self.lane / "meta").read_text()
        except OSError:
            return 65, None

        def decoded(value):
            return (
                value.decode("utf-8", errors="replace")
                if isinstance(value, bytes)
                else str(value or "")
            )

        output = decoded(getattr(result, "stdout", None)) + decoded(getattr(result, "stderr", None))
        if self.bounded:
            prefix = "CLOSER_NATIVE_ADMISSION_V1 " + self.env["CLOSER_ADMISSION_NONCE"] + " "
            records = [line for line in output.splitlines() if line.startswith(prefix)]
            if records != [prefix + "ordinary"]:
                return 78, token
        elif any(marker in output for marker in ("RELAY-REVIEW:", "RELAY-HALTED:")):
            return 78, token  # still owned: run_native_round must attempt C exactly once
        return (0, token) if token else (65, None)

    def owns(self, token: str) -> bool:
        try:
            return bool(token) and (self.lane / "meta").read_text() == token
        except OSError:
            return False

    def begin_terminal_receipt(self, cfg: dict[str, Any]) -> None:
        """Freeze prior immutable round identities immediately before this B submission.

        The exact-token lane excludes another normal B. This census never deletes
        history and cannot be refreshed after an unknown submission/restart.
        """
        import stat

        import closer_mechanisms as mechanisms
        import closer_native_observer as native
        import closer_resources
        from closer_campaign import load_manifest

        self.terminal_watermark = None
        manifest = load_manifest(cfg["runtime_manifest"])
        context = native.validate_admission(manifest, cfg["native_admission"], for_effects=True)
        pipeline = Path(str(cfg["dispatch_state"]) + ".pipeline.json")
        directory = closer_resources.scoped_root(pipeline) / "rounds"
        names: list[str] = []
        try:
            fd = os.open(directory, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_DIRECTORY)
        except FileNotFoundError:
            pass  # No prior scoped rounds; B may create the original directory.
        else:
            try:
                with os.scandir(fd) as entries:
                    for entry in entries:
                        if len(names) >= 10000:
                            raise BindingError("prior scoped receipt census exceeds bound")
                        if not re.fullmatch(
                            r"[0-9a-f]{16,64}\.json", entry.name
                        ) or not stat.S_ISREG(entry.stat(follow_symlinks=False).st_mode):
                            raise BindingError(
                                "prior scoped receipt identity is not immutable/regular"
                            )
                        names.append(entry.name[:-5])
            finally:
                os.close(fd)
        record = {
            "schema": "native-b-receipt-watermark/v1",
            "admission_nonce": context["nonce"],
            "dispatch_state": str(cfg["dispatch_state"]),
            "assignment_sha256": context["assignment"]["sha256"],
            "prior_round_ids": sorted(names),
        }
        root = native.contract(manifest)[3]
        path = root / context["nonce"] / "action-b-receipt-watermark.json"
        signed = {
            "record": record,
            "signature": mechanisms._signature(record, mechanisms._key(root)),
        }
        raw = json.dumps(signed, sort_keys=True, separators=(",", ":")).encode()
        # O_EXCL forbids reuse, including dangling symlinks, after any prior B.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        parent_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
        self.terminal_watermark = (path, raw, frozenset(names))

    def set_terminal_receipt(self, done, cfg: dict[str, Any]) -> bool:
        """Bind C to independently validated terminal B bytes, never a latest round."""
        import closer_resources
        from closer_dispatcher import targets_from_assignment
        from closer_native_observer import _bounded_bytes

        self.env = getattr(self, "env", {})
        self.env.pop("CLOSER_POSTRUN_ROUND_ID", None)
        self.terminal_receipt_problem = None
        self.descendant_bundle = None
        self.descendant_bundle_problem: str | None = None
        self.descendant_cfg: dict[str, Any] | None = None
        try:
            if done.returncode not in {0, 78}:
                raise ValueError("ACTION B did not return a supported terminal receipt")
            raw = done.stdout
            if not isinstance(raw, (str, bytes)) or len(raw) > 32 * 1024 * 1024:
                raise ValueError("terminal receipt missing or oversized")
            candidate = json.loads(raw)
            if not isinstance(candidate, dict):
                raise ValueError("terminal receipt is not an object")
            round_id = candidate.get("round_id")
            if (
                candidate.get("schema") != "closer-pipeline-receipt/v1"
                or candidate.get("mode") != "resource_scoped"
                or not isinstance(round_id, str)
                or not re.fullmatch(r"[0-9a-f]{16,64}", round_id)
            ):
                raise ValueError("terminal receipt schema/mode/round identity mismatch")
            watermark = getattr(self, "terminal_watermark", None)
            if watermark is None or _bounded_bytes(watermark[0], 2 * 1024 * 1024) != watermark[1]:
                raise ValueError("ACTION B immutable pre-submission receipt watermark unavailable")
            if round_id in watermark[2]:
                raise ValueError("terminal receipt predates this ACTION B")
            assignment = Path(cfg["assignment"])
            expected = cfg["native_hooks"]["postrun_dependencies"]["assignment"]
            if (
                str(assignment) != expected["path"]
                or assignment.is_symlink()
                or _hash(assignment) != expected["sha256"]
            ):
                raise ValueError("terminal receipt assignment pin mismatch")
            pipeline = Path(str(cfg["dispatch_state"]) + ".pipeline.json")
            path = closer_resources.scoped_root(pipeline) / "rounds" / (round_id + ".json")
            if candidate.get("receipt_path") != str(path):
                raise ValueError("terminal receipt path mismatch")
            receipt, digest = closer_resources.read_round_receipt(path)
            frontier = cfg.get("finite_frontier")
            if (
                receipt.get("finite_frontier") != frontier
                or candidate.get("finite_frontier") != frontier
            ):
                raise ValueError("terminal receipt finite frontier binding differs")
            targets = targets_from_assignment(assignment)
            verdict = closer_resources.validate_stop(pipeline, round_id, targets)
            if (
                receipt.get("schema") != candidate["schema"]
                or receipt.get("mode") != candidate["mode"]
                or receipt.get("round_id") != round_id
                or candidate.get("receipt_sha256") != digest
                or verdict.get("receipt_sha256") != digest
                or verdict.get("round_id") != round_id
                or sorted(receipt.get("assignment", [])) != sorted(targets)
                or candidate.get("assignment") != receipt.get("assignment")
                or candidate.get("run_ids") != receipt.get("run_ids")
                or verdict.get("run_ids") != receipt.get("run_ids")
            ):
                raise ValueError("terminal receipt bytes/assignment/run binding mismatch")
            from closer_native_descendants import seal_bundle

            self.descendant_bundle = seal_bundle(cfg, candidate, watermark)
            self.descendant_cfg = cfg
            self.env["CLOSER_POSTRUN_ROUND_ID"] = round_id
            return True
        except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
            self.terminal_receipt_problem = {
                "type": type(exc).__name__,
                "errno": getattr(exc, "errno", None),
                "reason": str(exc)[:512],
            }
            return False

    def postrun(self, token: str, deadline: float) -> int:
        if not self._pins_match():
            return 65
        if not self.owns(token):
            return 65  # C attempt refuses before launching cleanup against a successor
        self.assignment_source_changed = False
        if self.bounded:
            try:
                self.assignment_source_changed = (
                    _hash(self.assignment_source) != self.dependencies["assignment"][1]
                )
            except OSError:
                self.assignment_source_changed = True
        result = self.runner(
            [str(self.scripts["postrun"]), "closer", "no_op", "campaign_round"],
            env={
                **self.env,
                "HANDOFF_EXPECTED_LANE_TOKEN": token,
                "CLOSER_POSTRUN_DEPENDENCIES_VALID": "1" if self._dependencies_match() else "0",
                "CLOSER_POSTRUN_ASSIGNMENT_SOURCE_CHANGED": (
                    "1" if self.assignment_source_changed else "0"
                ),
            },
            capture_output=True,
            check=False,
            timeout=max(0.001, deadline - self.now()),
        )
        self.postrun_result = {
            "code": result.returncode,
            "stdout": getattr(result, "stdout", ""),
            "stderr": getattr(result, "stderr", ""),
            "dependencies_valid": self._dependencies_match(),
        }
        self.postrun_result["cleanup"] = (
            self.cleanup_receipt(token, result) if self._pins_match() else None
        )
        self.postrun_result["refusal"] = self.postrun_refusal(result)
        if getattr(self, "descendant_bundle", None) is not None:
            try:
                from closer_native_descendants import finalize_bundle

                stderr = self.postrun_result["stderr"]
                if isinstance(stderr, bytes):
                    stderr = stderr.decode("utf-8", errors="replace")
                if not self.postrun_result["dependencies_valid"] or not (
                    result.returncode == 0
                    or (
                        result.returncode == 65
                        and stderr.strip()
                        == "handoff-postrun: closer stop rejected; preserve ledger and resume actionable work"
                    )
                ):
                    raise ValueError("descendant actual C did not reach reviewed cleanup boundary")
                self.descendant_bundle = finalize_bundle(
                    self.descendant_cfg,
                    self.descendant_bundle,
                    self.postrun_result["cleanup"],
                    token,
                )
            except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
                self.descendant_bundle_problem = str(exc)[:512]
        return result.returncode

    def postrun_refusal(self, result):
        output = getattr(result, "stdout", "")
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="replace")
        nonce = self.env.get("CLOSER_ADMISSION_NONCE", "")
        if not re.fullmatch(r"[0-9a-f]{48}", nonce) or not isinstance(output, str):
            return None
        prefix = "CLOSER_NATIVE_POSTRUN_REFUSAL_V1 "
        markers = [line for line in output.splitlines() if line.startswith(prefix)]
        expected = prefix + nonce + " terminal_named_receipt_unavailable"
        return "terminal_named_receipt_unavailable" if markers == [expected] else None

    def cleanup_receipt(self, token, result):
        """Accept only one nonce-bound exact-credential removal from pinned C."""
        output = getattr(result, "stdout", "")
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="replace")
        nonce = self.env.get("CLOSER_ADMISSION_NONCE", "")
        if not re.fullmatch(r"[0-9a-f]{48}", nonce) or not isinstance(output, str):
            return None
        prefix = "CLOSER_NATIVE_CLEANUP_V1 " + nonce + " "
        markers = [
            line for line in output.splitlines() if line.startswith("CLOSER_NATIVE_CLEANUP_V1 ")
        ]
        if len(markers) != 1 or not markers[0].startswith(prefix):
            return None
        try:
            receipt = json.loads(markers[0][len(prefix) :])
            digest = hashlib.sha256(token.encode()).hexdigest()
            inode = int(token.split()[-1].rsplit(":", 1)[1])
            if (
                receipt.get("schema") != "closer-native-lease-cleanup/v1"
                or receipt.get("lane") != "closer"
                or receipt.get("lane_dir") != str(self.lane)
                or receipt.get("expected_token_sha256") != digest
                or receipt.get("meta_sha256") != digest
                or receipt.get("released") is not True
                or receipt.get("exact_token_matched") is not True
                or type(receipt.get("released_lease_inode")) is not int
                or receipt["released_lease_inode"] != inode
                or inode <= 0
                or isinstance(receipt.get("released_at"), bool)
                or not isinstance(receipt.get("released_at"), (int, float))
                or not math.isfinite(receipt["released_at"])
            ):
                return None
            return receipt
        except (ValueError, TypeError, AttributeError, IndexError):
            return None


def scoped_continuation_proof(cfg, candidate):
    """Revalidate a terminal round's known/no-start/contained domain, never global stop."""
    import closer_resources
    from closer_dispatcher import targets_from_assignment

    if cfg.get("mechanism") != "native-handoff/v1" or not isinstance(candidate, dict):
        return None
    if candidate.get("schema") == "native-descendant-continuation/v1":
        from closer_native_descendants import continuation_proof

        return continuation_proof(cfg, candidate)
    try:
        assignment = Path(cfg["assignment"])
        expected = cfg["native_hooks"]["postrun_dependencies"]["assignment"]
        if (
            str(assignment) != expected["path"]
            or assignment.is_symlink()
            or _hash(assignment) != expected["sha256"]
        ):
            return None
        round_id = candidate["round_id"]
        if not isinstance(round_id, str) or not re.fullmatch(r"[0-9a-f]{16,64}", round_id):
            return None
        pipeline = Path(str(cfg["dispatch_state"]) + ".pipeline.json")
        receipt = closer_resources.scoped_root(pipeline) / "rounds" / (round_id + ".json")
        if receipt.is_symlink() or candidate["receipt_path"] != str(receipt):
            return None
        frontier = cfg.get("finite_frontier")
        if candidate.get("finite_frontier") != frontier:
            return None
        if frontier is not None:
            actual_receipt, _digest = closer_resources.read_round_receipt(receipt)
            if actual_receipt.get("finite_frontier") != frontier:
                return None
        targets = targets_from_assignment(assignment)
        verdict = closer_resources.validate_stop(pipeline, round_id, targets)
        receipt_hash = verdict.get("receipt_sha256")
        if (
            not isinstance(receipt_hash, str)
            or not re.fullmatch(r"[0-9a-f]{64}", receipt_hash)
            or candidate.get("receipt_sha256") != receipt_hash
        ):
            return None
        if verdict.get("round_id") != round_id or verdict.get("global_stop_valid") is not False:
            return None
        if (
            verdict.get("admitted_domain_valid") is True
            and verdict.get("run_ids")
            and not verdict.get("admitted_domain_reasons")
        ):
            domain_outcome = "known"
        elif verdict.get("not_started_domain_valid") is True and verdict.get("run_ids") == {}:
            domain_outcome = "not_started"
        elif (
            verdict.get("scoped_containment_valid") is True
            and verdict.get("quarantined_admitted_items")
            and verdict.get("run_ids")
        ):
            domain_outcome = "quarantined"
        else:
            return None
        if candidate.get("run_ids") != verdict["run_ids"]:
            return None
        handoff = Path(
            cfg.get("handoff_dir")
            or os.environ.get("HANDOFF_DIR")
            or Path.home() / ".codex/handoff"
        )
        lane = handoff / ".lane-closer.lock"
        if lane.exists() or lane.is_symlink():
            return None
        proof = {
            "schema": "native-scoped-continuation/v1",
            "round_id": round_id,
            "receipt_path": str(receipt),
            "receipt_sha256": receipt_hash,
            "assignment_sha256": expected["sha256"],
            "run_ids": verdict["run_ids"],
            "held_targets": verdict.get("held_targets", []),
            "global_stop_valid": False,
            "accepted": False,
            "domain_outcome": domain_outcome,
        }
        if frontier is not None:
            proof["finite_frontier"] = frontier
        if domain_outcome == "quarantined":
            proof["quarantined_admitted_items"] = verdict["quarantined_admitted_items"]
        return proof
    except (OSError, ValueError, KeyError, TypeError, RuntimeError):
        return None


def _finite_native_frontier(cfg):
    """Only an explicit signed v2 admission selects no aggregate work cutoff."""
    from closer_frontier_policy import request_window, validate_frontier

    candidate = cfg.get("finite_frontier")
    context = cfg.get("native_admission") or {}
    if candidate is None:
        if context.get("schema") == "native-invocation-admission/v2":
            raise BindingError("native finite frontier missing from worker config")
        return None
    try:
        frontier = validate_frontier(candidate)
        timeout = cfg.get("request_timeout_seconds")
        window = request_window(0, timeout)
        if (
            frontier != candidate
            or context.get("schema") != "native-invocation-admission/v2"
            or context.get("finite_frontier") != frontier
            or context.get("request_timeout_seconds") != timeout
            or cfg.get("mechanism") != "native-handoff/v1"
            or any(
                key in cfg
                for key in ("original_deadline", "budget_seconds", "dispatch_budget_seconds")
            )
            or not math.isfinite(window["deadline"])
            or timeout > 120
        ):
            raise ValueError("finite frontier/config admission differs")
        return frontier, float(timeout)
    except (ValueError, TypeError, KeyError) as exc:
        raise BindingError("native finite frontier requires exact signed v2 admission") from exc


def _finite_dispatch_command(command, frontier, timeout):
    """Reject ambiguous flags or any attempt to mix legacy aggregate authority."""
    for flag in ("--finite-frontier-json", "--request-timeout-seconds"):
        if command.count(flag) != 1 or any(
            arg != flag and arg.startswith("--") and flag.startswith(arg.partition("=")[0])
            for arg in command
        ):
            raise BindingError("native finite dispatcher flags missing or ambiguous")
    if any(
        arg.startswith("--") and "--budget-seconds".startswith(arg.partition("=")[0])
        for arg in command
    ):
        raise BindingError("native finite dispatcher cannot mix aggregate budget")
    try:
        descriptor = json.loads(command[command.index("--finite-frontier-json") + 1])
        request_timeout = float(command[command.index("--request-timeout-seconds") + 1])
        if descriptor != frontier or request_timeout != timeout:
            raise ValueError("finite dispatcher descriptor/timeout differs")
    except (IndexError, ValueError, TypeError) as exc:
        raise BindingError("native finite dispatcher binding differs") from exc


def run_native_round(
    command: list[str],
    cfg: dict[str, Any],
    *,
    runner,
    hooks: NativeRoundHooks | None = None,
    now=time.time,
) -> tuple[str, str]:
    """One A/B/C with receipt reserve. No helper fallback and no lease reaping.

    Only exact pinned native adapters may supply cleanup and no-start evidence.
    Missing, stale, or unsupported proof stays unknown after possible effects.
    Test doubles exercise timeout/lease-loss behavior without a native run.
    """
    import subprocess

    entered = now()
    if hooks is None:
        try:
            hooks = PinnedNativeHooks(cfg, runner=runner, now=now)
        except (BindingError, KeyError, TypeError) as exc:
            return "unavailable", str(exc)
    started = now()
    try:
        finite = _finite_native_frontier(cfg)
        if finite:
            _finite_dispatch_command(command, *finite)
            deadline = started + finite[1]
            if not math.isfinite(deadline):
                raise BindingError("native finite request deadline overflow")
            reserve = 0.0
        else:
            deadline = min(
                started + float(cfg.get("budget_seconds", 600)),
                float(cfg.get("original_deadline", started + 600)),
            )
            if isinstance(hooks, PinnedNativeHooks) and hooks.bounded and deadline - started > 900:
                return "unavailable", "bounded native hook policy exceeds original 900-second limit"
            reserve = max(15.0, float(cfg.get("receipt_reserve_seconds", 300)))
            if deadline - started <= reserve:
                return "unavailable", "native round has no action budget after receipt reserve"
    except BindingError as exc:
        return "unavailable", str(exc)
    if isinstance(hooks, PinnedNativeHooks) and hooks.bounded:
        interpreter = hooks.dependencies["interpreter"][0]
        if not command or Path(command[0]) not in {hooks.interpreter_anchor, interpreter}:
            return "unavailable", "native B requires pinned Runtime Python interpreter"
        command = [str(interpreter), *command[1:]]
    if isinstance(hooks, PinnedNativeHooks):
        hooks.descendant_bundle = None
        hooks.descendant_bundle_problem = None
        hooks.descendant_cfg = None
        hooks.env = getattr(hooks, "env", {})
        hooks.env.pop("CLOSER_POSTRUN_ROUND_ID", None)
        hooks.env["CLOSER_POSTRUN_REQUIRE_NAMED_ROUND"] = (
            "1" if cfg.get("native_admission") is not None else "0"
        )
    token = None
    terminal_b = None
    receipt_bound = False
    dispatcher_started = False
    pre_dispatch_refusal = False
    postrun_completed = False
    phases: dict[str, dict[str, Any]] = {
        "A": {"attempted": True, "started_at": None, "finished_at": None},
        "B": {"started": False, "started_at": None, "finished_at": None},
        "C": {"attempted": False, "started_at": None, "finished_at": None},
    }

    def execution_report():
        return {
            "schema": "native-round-execution/v1",
            "admission_nonce": cfg["native_admission"]["nonce"],
            "worker": cfg.get("native_worker"),
            "admission_started_at": cfg["native_admission"]["started_at"],
            "runtime_binding_entered_at": entered,
            "runtime_binding_ready_at": started,
            "phases": phases,
        }

    def reported(text):
        if cfg.get("native_admission") is None:
            return text
        try:
            value = json.loads(text)
            if not isinstance(value, dict):
                raise ValueError
        except (ValueError, TypeError):
            value = {"actual_detail": text}
        value["native_execution"] = execution_report()
        if isinstance(hooks, PinnedNativeHooks) and getattr(hooks, "descendant_bundle", None):
            value["descendant_bundle"] = hooks.descendant_bundle
        return json.dumps(value, sort_keys=True)

    outcome, detail = "unknown", "native prerun outcome unknown"
    try:
        phases["A"]["started_at"] = now()
        code, token = hooks.prerun(deadline - reserve)
        phases["A"].update(exit_code=code, admitted=code == 0 and bool(token), finished_at=now())
        if token:
            phases["A"]["token_sha256"] = hashlib.sha256(token.encode()).hexdigest()
            try:
                phases["A"]["lease_inode"] = int(token.split()[-1].rsplit(":", 1)[1])
            except (ValueError, IndexError):
                phases["A"]["lease_inode"] = None
        if code != 0 or not token:
            outcome = "unknown" if token else "unavailable"
            detail = f"ACTION A refused ({code}); no dispatcher started"
        elif (
            isinstance(hooks, PinnedNativeHooks)
            and hooks.bounded
            and (
                command.count("--assignment") != 1
                or any(
                    arg != "--assignment"
                    and arg.startswith("--")
                    and "--assignment".startswith(arg.partition("=")[0])
                    for arg in command
                )
                or command[command.index("--assignment") + 1 : command.index("--assignment") + 2]
                != [cfg["assignment"]]
            )
        ):
            outcome, detail = "unavailable", "ACTION B assignment differs from admitted snapshot"
        elif isinstance(hooks, PinnedNativeHooks) and not hooks._dependencies_match():
            outcome, detail = (
                "unknown",
                "native dependency pins changed after ACTION A; cleanup only",
            )
        elif not hooks.owns(token):
            outcome, detail = "unknown", "native lease lost before dispatcher"
        else:
            # B owns only its original safe action deadline, never a fresh budget.
            try:
                if finite:
                    _finite_dispatch_command(command, *finite)
                elif cfg.get("native_admission") is not None:
                    if command.count("--budget-seconds") != 1:
                        raise BindingError("native original B budget argument missing or ambiguous")
                    index = command.index("--budget-seconds") + 1
                    inner = math.floor(deadline - reserve - now() - 300 - 15)
                    if inner <= 0:
                        raise BindingError("native original budget exhausted during ACTION A")
                    command = [*command]
                    command[index] = str(min(int(command[index]), inner))
                if cfg.get("native_admission") is not None:
                    if not isinstance(hooks, PinnedNativeHooks):
                        raise BindingError("native B receipt watermark requires pinned hooks")
                    hooks.begin_terminal_receipt(cfg)
                dispatcher_started = True
                phases["B"].update(started=True, started_at=now())
                done = runner(
                    command,
                    capture_output=True,
                    check=False,
                    # B is the finite frontier coordinator, not one I/O request.
                    # Its adapters enforce each request's original finite window.
                    timeout=None if finite else max(0.001, deadline - reserve - now()),
                    **(
                        {
                            "env": {
                                **os.environ,
                                "CLOSER_CAPACITY_REPORT": str(cfg["capacity_report"]),
                            }
                        }
                        if cfg.get("capacity_report")
                        else {}
                    ),
                )
                terminal_b = done
                phases["B"].update(exit_code=done.returncode, finished_at=now())
                outcome = "ok" if done.returncode == 0 else "unknown"
                detail = f"dispatcher exit {done.returncode}"
                if cfg.get("native_admission") is not None:
                    receipt_bound = isinstance(
                        hooks, PinnedNativeHooks
                    ) and hooks.set_terminal_receipt(done, cfg)
                    if not receipt_bound:
                        outcome = "unknown"
                        detail += "; terminal named receipt refused: " + json.dumps(
                            getattr(hooks, "terminal_receipt_problem", None), sort_keys=True
                        )
            except BindingError as exc:
                phases["B"]["checked_at"] = now()
                pre_dispatch_refusal = not dispatcher_started
                outcome, detail = (
                    ("unavailable", f"dispatcher not started: {exc}")
                    if pre_dispatch_refusal
                    else (
                        "unknown",
                        "dispatcher binding failure after submission; effect outcome unknown",
                    )
                )
            except subprocess.TimeoutExpired:
                outcome, detail = "unknown", "dispatcher timeout; effect outcome unknown"
            except PermissionError as exc:
                outcome, detail = "denied", str(exc)
            except Exception as exc:
                outcome, detail = "unknown", f"dispatcher outcome unknown: {type(exc).__name__}"
            finally:
                if dispatcher_started and phases["B"]["finished_at"] is None:
                    phases["B"]["finished_at"] = now()
    except PermissionError as exc:
        outcome, detail = "denied", str(exc)
        phases["A" if "exit_code" not in phases["A"] else "B"]["denial"] = str(exc)
    except Exception as exc:
        outcome, detail = "unknown", f"native phase outcome unknown: {type(exc).__name__}"
        phases["A" if "exit_code" not in phases["A"] else "B"]["error"] = type(exc).__name__
    finally:
        if phases["A"]["finished_at"] is None:
            phases["A"]["finished_at"] = now()
        if token:
            # C gets one attempt, even after B timeout. The hook itself must check
            # exact token at cleanup; a caller-side check alone is insufficient.
            try:
                phases["C"]["attempted"] = True
                phases["C"]["started_at"] = now()
                cleanup_deadline = now() + finite[1] if finite else deadline
                if not math.isfinite(cleanup_deadline):
                    raise BindingError("native finite cleanup deadline overflow")
                code = hooks.postrun(token, cleanup_deadline)
                postrun_completed = True
            except PermissionError as exc:
                code = 65
                outcome = "denied"
                phases["C"]["denial"] = str(exc)
                detail += f"; ACTION C denied: {type(exc).__name__}: {exc}"
            except Exception as exc:
                code = 65
                phases["C"]["error"] = type(exc).__name__
                detail += f"; ACTION C unknown: {type(exc).__name__}"
            if getattr(hooks, "assignment_source_changed", False):
                detail += "; controller assignment changed; validation bound to admitted snapshot"
            if code != 0:
                if outcome != "denied":
                    outcome = "unknown"
                detail += f"; ACTION C{code}; preserve no_matching_pipeline_run and ledger"
            postrun = getattr(hooks, "postrun_result", {})
            phases["C"].update(
                exit_code=code,
                dependencies_valid=postrun.get("dependencies_valid") is True,
                cleanup=postrun.get("cleanup"),
                finished_at=now(),
            )
            if postrun.get("refusal") == "terminal_named_receipt_unavailable":
                phases["C"]["refusal"] = postrun["refusal"]
    if (
        cfg.get("native_admission") is not None
        and isinstance(hooks, PinnedNativeHooks)
        and hooks.bounded
        and pre_dispatch_refusal
        and not dispatcher_started
        and terminal_b is None
        and postrun_completed
        and outcome != "denied"
    ):
        from closer_campaign import load_manifest
        from closer_native_observer import pre_dispatch_no_start_proof

        phases["B"].update(reason="pre_dispatch_binding_refusal")
        try:
            proof = pre_dispatch_no_start_proof(
                load_manifest(cfg["runtime_manifest"]),
                cfg,
                {"phases": phases, "execution": execution_report()},
                record_boundary=True,
            )
        except (OSError, ValueError, RuntimeError, KeyError, TypeError):
            proof = None
        if proof:
            return "unavailable", reported(
                json.dumps({"native_no_start": proof, "actual_detail": detail})
            )
        if outcome != "denied":
            outcome = "unknown"
    if isinstance(hooks, PinnedNativeHooks) and getattr(hooks, "descendant_bundle", None):
        # TOP completion can never erase a descendant's unknown effect or hold.
        try:
            from closer_native_descendants import bundle_record, continuation_proof

            descendants = bundle_record(cfg, hooks.descendant_bundle)
            if getattr(hooks, "descendant_bundle_problem", None):
                raise ValueError(hooks.descendant_bundle_problem)
            if descendants["aggregate"] == "unknown":
                raise ValueError("descendant effect outcome unknown; preserve original owner")
            if descendants["aggregate"] == "known_held":
                proof = continuation_proof(cfg, {"descendant_bundle": hooks.descendant_bundle})
                if proof is None or outcome == "denied":
                    raise ValueError(
                        "descendant known hold lacks actual cleanup continuation proof"
                    )
                return "scoped_continuation", reported(
                    json.dumps(
                        {
                            "proof": proof,
                            "actual_detail": detail,
                            "action_b_exit": terminal_b.returncode if terminal_b else None,
                            "action_c_exit": getattr(hooks, "postrun_result", {}).get("code"),
                        }
                    )
                )
        except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
            return ("denied" if outcome == "denied" else "unknown"), reported(
                detail + "; descendant receipt boundary unavailable: " + str(exc)[:512]
            )
    if (
        cfg.get("mechanism") == "native-handoff/v1"
        and isinstance(hooks, PinnedNativeHooks)
        and hooks.bounded
        and token
        and terminal_b is not None
        and terminal_b.returncode in {0, 78}
        and (cfg.get("native_admission") is None or receipt_bound)
        and outcome != "denied"
    ):
        c = getattr(hooks, "postrun_result", {})
        stderr = c.get("stderr", "")
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")
        # Only the reviewed normal global stop gate refusal is eligible. Missing
        # dependencies, arbitrary C failures, live lanes and unknown B stay held.
        if (
            c.get("code") == 65
            and c.get("dependencies_valid") is True
            and stderr.strip()
            == "handoff-postrun: closer stop rejected; preserve ledger and resume actionable work"
        ):
            try:
                raw = terminal_b.stdout
                candidate = json.loads(raw)
            except (ValueError, TypeError):
                candidate = None
            proof = scoped_continuation_proof(cfg, candidate)
            if proof:
                return (
                    "unavailable"
                    if proof["domain_outcome"] == "not_started"
                    else "scoped_continuation"
                ), reported(
                    json.dumps(
                        {
                            "proof": proof,
                            "actual_detail": detail,
                            "action_b_exit": terminal_b.returncode,
                            "action_c_exit": 65,
                        },
                        sort_keys=True,
                    )
                )
    if cfg.get("native_admission") is not None and outcome == "ok":
        from closer_campaign import load_manifest
        from closer_native_observer import completed_round_proof

        c = getattr(hooks, "postrun_result", {})
        try:
            candidate = json.loads(terminal_b.stdout) if terminal_b is not None else None
            proof = (
                completed_round_proof(
                    load_manifest(cfg["runtime_manifest"]), cfg, candidate, record_boundary=True
                )
                if isinstance(hooks, PinnedNativeHooks)
                and hooks.bounded
                and token
                and terminal_b is not None
                and terminal_b.returncode == 0
                and c.get("code") == 0
                and c.get("dependencies_valid") is True
                else None
            )
        except (OSError, ValueError, RuntimeError, KeyError, TypeError):
            proof = None
        if proof is None:
            return "unknown", reported(detail + "; native exact successful-round proof unavailable")
        return "ok", reported(
            json.dumps(
                {
                    "completed_round": proof,
                    "actual_detail": detail,
                    "action_b_exit": 0,
                    "action_c_exit": 0,
                },
                sort_keys=True,
            )
        )
    if (
        cfg.get("native_admission") is not None
        and phases["A"].get("admitted") is True
        and phases["B"].get("started") is False
        and phases["C"].get("attempted") is True
        and outcome == "unavailable"
    ):
        outcome = "unknown"  # No submitted B does not itself prove native cleanup.
    return outcome, reported(detail)


def selftest() -> None:
    """Normal repository discovery; isolated policy/observer and native lease checks."""
    import subprocess
    import tempfile

    assert policy_problem({}) is not None
    assert desktop_problems({}, {"kind": "desktop-runner"}) == [DESKTOP_UNSUPPORTED]
    with tempfile.TemporaryDirectory() as directory:
        worker = Path(directory) / "worker.toml"
        worker.write_text('status = "PAUSED"\n')
        assert "paused" in str(policy_problem({"worker_tomls": [str(worker)]}))
    events = []

    class Hooks:
        def prerun(self, deadline):
            events.append("A")
            return 0, "original"

        def owns(self, token):
            return token == "original"

        def postrun(self, token, deadline):
            events.append("C")
            return 65

    def timeout(command, **kwargs):
        events.append("B")
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    outcome, detail = run_native_round(["offline-test"], {}, runner=timeout, hooks=Hooks())
    assert outcome == "unknown" and events == ["A", "B", "C"] and "C65" in detail
    print("closer_runtime_binding selftest: PASS (offline; no host qualification attempted)")


if __name__ == "__main__":
    if sys.argv[1:] == ["--selftest"]:
        selftest()
    else:
        raise SystemExit("supported command: --selftest")
