"""Production Runtime/Core binding. No network or credential discovery at import.

Public Core descriptor and its closed file set are immutable authority inputs;
package_root, policy locations and interpreter are private host bindings. Desktop
qualification requires a registered observer, never a caller-written receipt.
"""

from __future__ import annotations

import builtins
import hashlib
import json
import os
import re
import sys
import time
import tomllib
import types
from pathlib import Path
from typing import Any, Callable, Protocol

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
        package = types.ModuleType(self.namespace)
        package.__path__ = [str(self.root / "scripts")]
        sys.modules[self.namespace] = package
        self.package = package
        self.runtime = self.load("closer_runtime")
        self.github = self.load("closer_operation_github")

    def load(self, name: str) -> types.ModuleType:
        if name in self.loaded:
            return self.loaded[name]
        relative = f"scripts/{name}.py"
        if relative not in CORE_FILES:
            raise BindingError(f"Core import outside closed package: {name}")
        module = types.ModuleType(self.namespace + "." + name)
        path = self.root / relative
        module.__file__ = str(path)
        module.__package__ = self.namespace
        module.__dict__["__builtins__"] = {**vars(builtins), "__import__": self.import_owned}
        self.loaded[name] = module
        sys.modules[module.__name__] = module
        setattr(self.package, name, module)
        original_path = list(sys.path)
        try:
            exec(compile(self.sources[relative], str(path), "exec"), module.__dict__)
        finally:
            sys.path[:] = original_path  # legacy Core helper prepends its script directory
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
            request = self.transport or self.bundle.github.NativeGitHubTransport(token)
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

    def _recover(self, adapter, record, holder):
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
            if not observed:
                return {"status": "unknown", "reason": "original native effect not observed"}
            spec = core.spec_from_record(record)
            evidence = core.ReconciliationEvidence(
                operation_key=record["operation_key"],
                target_fence_key=record["target_fence_key"],
                head_sha=record["head_sha"],
                action=record["action"],
                result="desired_result_observed",
                source=f"repos/{record['repository']}/pulls/{record['target']['number']}",
                observation_digest=_digest(pr),
                context=json.dumps(
                    {"native_binding": binding, "merge": pr.get("merge_commit_sha")}, sort_keys=True
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

    def execute(self, target, action, snapshot, operation, native_effect):
        from closer_campaign import ActionSpec, sha256_json, submit_effect

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
                # Anything after submission remains fenced pending independent readback.
                return "ok" if result.get("status") == "completed" else "unknown"

        outcome = submit_effect(adapter, NativePort(), spec, {}, holder=operation["owner"])
        if outcome in {"ok", "unknown"}:
            try:
                record = adapter.store.read(adapter.target_fence_key(spec))
                recovered = self._recover(adapter, record, operation["owner"]) if record else {}
            except Exception:
                recovered = {}
            if recovered.get("status") == "completed":
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
    expected meta token inside cleanup. Current installed helpers lack it.
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
            canonical = {
                "interpreter": self.interpreter_anchor.resolve(),
                "dispatcher": self.dispatcher_anchor.resolve(),
                "assignment": Path(cfg["dispatch_state"]).parent
                / "native-assignment-snapshot.json",
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
                        (phase == "postrun" and b"CLOSER_BOUNDED_NO_RELAY_V1" not in data)
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
        return result.returncode


def scoped_continuation_proof(cfg, candidate):
    """Revalidate one known terminal admitted domain, never certify global stop."""
    import closer_resources
    from closer_dispatcher import targets_from_assignment

    if cfg.get("mechanism") != "native-handoff/v1" or not isinstance(candidate, dict):
        return None
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
        receipt_hash = _hash(receipt)
        if candidate.get("receipt_sha256", receipt_hash) != receipt_hash:
            return None
        targets = targets_from_assignment(assignment)
        verdict = closer_resources.validate_stop(pipeline, round_id, targets)
        if (
            verdict.get("round_id") != round_id
            or verdict.get("admitted_domain_valid") is not True
            or not verdict.get("run_ids")
            or verdict.get("admitted_domain_reasons")
            or verdict.get("global_stop_valid") is not False
        ):
            return None
        if candidate.get("run_ids", verdict["run_ids"]) != verdict["run_ids"]:
            return None
        handoff = Path(
            cfg.get("handoff_dir")
            or os.environ.get("HANDOFF_DIR")
            or Path.home() / ".codex/handoff"
        )
        lane = handoff / ".lane-closer.lock"
        if lane.exists() or lane.is_symlink():
            return None
        return {
            "schema": "native-scoped-continuation/v1",
            "round_id": round_id,
            "receipt_path": str(receipt),
            "receipt_sha256": receipt_hash,
            "assignment_sha256": expected["sha256"],
            "run_ids": verdict["run_ids"],
            "held_targets": verdict.get("held_targets", []),
            "global_stop_valid": False,
            "accepted": False,
        }
    except (OSError, ValueError, KeyError, TypeError, RuntimeError):
        return None


def run_native_round(
    command: list[str],
    cfg: dict[str, Any],
    *,
    runner,
    hooks: NativeRoundHooks | None = None,
    now=time.time,
) -> tuple[str, str]:
    """One A/B/C with receipt reserve. No helper fallback and no lease reaping.

    The installed native helpers currently lack token-safe cleanup. Until a
    supported adapter is supplied by the owning native runtime, hold explicitly.
    Test doubles exercise timeout/lease-loss behavior without a native run.
    """
    import subprocess

    if hooks is None:
        try:
            hooks = PinnedNativeHooks(cfg, runner=runner, now=now)
        except (BindingError, KeyError, TypeError) as exc:
            return "unavailable", str(exc)
    started = now()
    deadline = min(
        started + float(cfg.get("budget_seconds", 600)),
        float(cfg.get("original_deadline", started + 600)),
    )
    if isinstance(hooks, PinnedNativeHooks) and hooks.bounded and deadline - started > 900:
        return "unavailable", "bounded native hook policy exceeds original 900-second limit"
    reserve = max(15.0, float(cfg.get("receipt_reserve_seconds", 300)))
    if deadline - started <= reserve:
        return "unavailable", "native round has no action budget after receipt reserve"
    if isinstance(hooks, PinnedNativeHooks) and hooks.bounded:
        interpreter = hooks.dependencies["interpreter"][0]
        if not command or Path(command[0]) not in {hooks.interpreter_anchor, interpreter}:
            return "unavailable", "native B requires pinned Runtime Python interpreter"
        command = [str(interpreter), *command[1:]]
    token = None
    terminal_b = None
    outcome, detail = "unknown", "native prerun outcome unknown"
    try:
        code, token = hooks.prerun(deadline - reserve)
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
                done = runner(
                    command,
                    capture_output=True,
                    check=False,
                    timeout=max(0.001, deadline - reserve - now()),
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
                outcome = "ok" if done.returncode == 0 else "unknown"
                detail = f"dispatcher exit {done.returncode}"
            except subprocess.TimeoutExpired:
                outcome, detail = "unknown", "dispatcher timeout; effect outcome unknown"
            except PermissionError as exc:
                outcome, detail = "denied", str(exc)
            except Exception as exc:
                outcome, detail = "unknown", f"dispatcher outcome unknown: {type(exc).__name__}"
    finally:
        if token:
            # C gets one attempt, even after B timeout. The hook itself must check
            # exact token at cleanup; a caller-side check alone is insufficient.
            try:
                code = hooks.postrun(token, deadline)
            except Exception as exc:
                code = 65
                detail += f"; ACTION C unknown: {type(exc).__name__}"
            if getattr(hooks, "assignment_source_changed", False):
                detail += "; controller assignment changed; validation bound to admitted snapshot"
            if code != 0:
                outcome = "unknown"
                detail += f"; ACTION C{code}; preserve no_matching_pipeline_run and ledger"
    if (
        cfg.get("mechanism") == "native-handoff/v1"
        and isinstance(hooks, PinnedNativeHooks)
        and hooks.bounded
        and token
        and terminal_b is not None
        and terminal_b.returncode in {0, 78}
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
                return "scoped_continuation", json.dumps(
                    {
                        "proof": proof,
                        "actual_detail": detail,
                        "action_b_exit": terminal_b.returncode,
                        "action_c_exit": 65,
                    },
                    sort_keys=True,
                )
    return outcome, detail


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
