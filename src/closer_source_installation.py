"""Factory-issued optional correction installation from signed native policy.

No registration, provisioning, credentials, effects, or authority from arguments.
A validated object still requires the current original queue/frame joins at its
consumer. Python process provenance is not a sandbox against trusted host code.
"""

from __future__ import annotations

import copy
import hashlib
import inspect
import json
import os
import re
import stat
import threading
from contextlib import closing, contextmanager
from pathlib import Path

SCHEMA = "closer-correction-controller-installation/v1"
_SOURCE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
_REQUIRED = frozenset(
    name + ".py"
    for name in (
        "closer_source_installation",
        "closer_correction_maintenance",
        "closer_runtime_binding",
        "closer_native_observer",
        "closer_correction_controller",
        "closer_correction_handoff",
        "closer_correction_native_effects",
        "closer_correction_source_record",
        "closer_correction_bridge",
        "closer_correction_owner",
        "closer_correction_ownership",
        "closer_correction_classification",
        "closer_correction_producer",
        "closer_correction_decision",
        "closer_correction_enrollment_terminal",
        "closer_coding_continuation",
        "closer_coding_worker",
        "closer_coding_session_runtime",
        "closer_coding_session_v2",
        "closer_dispatcher",
        "closer_queue",
        "closer_resources",
        "closer_frontier_policy",
        "closer_campaign",
        "closer_execution_contracts",
        "closer_role_qualification",
    )
)
_CONFIG_FIELDS = frozenset(
    "state_path repository repository_id epoch bootstrap_source_sha source_sha writer_id writer_login writer_type bootstrap_anchor_sha256 max_bytes max_entries max_subjects consumers native_transport_entrypoint native_transport_source_sha256".split()
)
_SEAL = object()
_SHA = re.compile(r"[0-9a-f]{64}")
_COMMIT = re.compile(r"[0-9a-f]{40}")


def _need(ok, reason):
    if not ok:
        raise ValueError(reason)


def _digest(value):
    # Existing Workflows-compatible bounded canonical grammar. On this domain
    # its bytes match the controller's canonical digest, including Unicode.
    from closer_correction_handoff import digest

    return digest(value)


def _physical_json(name, *, limit):
    """Bounded original canonical regular bytes; never follow aliases or FIFOs."""
    path = Path(name)
    _need(
        path.is_absolute()
        and path.resolve(strict=True) == path
        and not any(p.is_symlink() for p in (path, *path.parents)),
        "physical path unavailable",
    )
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        _need(
            stat.S_ISREG(before.st_mode)
            and before.st_nlink == 1
            and before.st_uid == os.getuid()
            and not before.st_mode & 0o022
            and 0 < before.st_size <= limit,
            "physical bytes outside bound",
        )
        raw = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
        _need(
            (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            == (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            and len(raw) == before.st_size,
            "physical bytes changed",
        )
    from closer_model_contracts import strict_response_json

    value = strict_response_json(raw.decode("utf-8"))
    _need(type(value) is dict, "physical object required")
    return value, raw


class SourceInstallation:
    """Opaque factory selection; consumer must fresh-validate before each use."""

    __slots__ = ("_binding", "_value", "_seal")

    def __init__(self, *args, **kwargs):
        raise TypeError("SourceInstallation is issued by source_installation_for only")

    @property
    def config(self):
        return copy.deepcopy(self._value["config"])

    @property
    def admission(self):
        return copy.deepcopy(self._value["admission"])

    @property
    def finite_frontier(self):
        return copy.deepcopy(self._value["admission"]["finite_frontier"])

    @property
    def request_timeout_seconds(self):
        return self._value["admission"]["request_timeout_seconds"]

    @property
    def manifest_path(self):
        return self._value["manifest_path"]

    @property
    def manifest_sha256(self):
        return self._value["manifest_sha256"]

    @property
    def module_dir(self):
        return self._value["module_dir"]

    @property
    def policy_projection(self):
        return copy.deepcopy(self._value["policy_projection"])

    @property
    def source_closure(self):
        return copy.deepcopy(self._value["source_closure"])


def _config(value, manifest, expected_state):
    _need(
        type(value) is dict and set(value) == _CONFIG_FIELDS, "fixed controller config unavailable"
    )
    _need(value["state_path"] == expected_state, "per-target controller root refused")
    _need(
        isinstance(value["repository"], str)
        and value["repository"] == value["repository"].lower()
        and re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value["repository"]),
        "repository invalid",
    )
    _need(
        all(
            type(value[k]) is int and value[k] > 0
            for k in ("repository_id", "writer_id", "max_bytes", "max_entries", "max_subjects")
        )
        and value["max_bytes"] <= 1048576,
        "controller integer bounds invalid",
    )
    _need(
        all(
            isinstance(value[k], str) and _SHA.fullmatch(value[k])
            for k in ("epoch", "bootstrap_anchor_sha256", "native_transport_source_sha256")
        )
        and all(
            isinstance(value[k], str) and _COMMIT.fullmatch(value[k])
            for k in ("source_sha", "bootstrap_source_sha")
        )
        and value["source_sha"] == manifest["candidate_sha"],
        "controller epoch/source differs",
    )
    _need(
        value["writer_type"] in {"User", "Bot"}
        and isinstance(value["writer_login"], str)
        and re.fullmatch(r"[A-Za-z0-9_\[\]-]{1,100}", value["writer_login"])
        and type(value["consumers"]) is dict,
        "native writer or consumer config invalid",
    )
    _need(
        value["native_transport_entrypoint"]
        == "closer_correction_native_effects.NativeHTTPTransport.request"
        and value["native_transport_source_sha256"]
        == manifest["modules"]["closer_correction_native_effects.py"],
        "native transport closure differs",
    )
    return copy.deepcopy(value)


def _qualified_policy(binding, *, _scope=None, _native_scope=None):
    import closer_mechanisms as mechanisms
    import closer_runtime_binding as runtime
    from closer_campaign import load_manifest, qualify

    origin = runtime._factory_manifest_origin(binding)
    _need(
        origin is not None and binding.mechanism == mechanisms.NATIVE,
        "factory native origin unavailable",
    )
    original, raw = _physical_json(origin.path, limit=16 * 1024 * 1024)
    _need(hashlib.sha256(raw).hexdigest() == origin.sha256, "factory manifest bytes changed")
    if _scope is None:
        manifest = load_manifest(origin.path)
        problems = qualify(manifest, Path(origin.path).parent)
    else:
        _need(
            type(_scope) is _OriginalWriterScope and _scope.seal is _SCOPE_SEAL,
            "source-issued original writer scope required",
        )
        _need(
            type(_native_scope) is _NativeRuntimeRead and _native_scope.scope is _scope,
            "original snapshot native read context required",
        )
        _native_scope.check(_scope.manifest)
        from closer_campaign import _current_qualification_problems, validate_manifest

        manifest = original
        _need(manifest == _scope.manifest, "original scoped manifest changed")
        problems = validate_manifest(manifest) + _current_qualification_problems(
            manifest, Path(origin.path).parent, _scope=_native_scope
        )
    _need(
        manifest == original and not problems and binding.policy == manifest.get("policy"),
        "current manifest qualification unavailable",
    )
    field = manifest["policy"].get("correction_controller_installation")
    _need(
        type(field) is dict and set(field) == {"schema", "config"} and field["schema"] == SCHEMA,
        "optional installation absent",
    )
    declared_state = field["config"]["state_path"]
    root = Path(manifest["mechanism_contract"]["campaign_root"])
    _need(
        isinstance(declared_state, str)
        and Path(declared_state).is_absolute()
        and Path(declared_state).parent == root
        and Path(declared_state).name.endswith(".campaign.json"),
        "qualified shared campaign root differs",
    )
    config = _config(field["config"], manifest, declared_state)
    state, _state_raw = _physical_json(config["state_path"], limit=4 * 1024 * 1024)
    _need(
        isinstance(state.get("campaign_id"), str)
        and bool(state["campaign_id"])
        and isinstance(state.get("owner"), str)
        and bool(state["owner"])
        and type(state.get("generation")) is int
        and state["generation"] > 0
        and state.get("release_pin") == manifest["package_digest"]
        and (state.get("worker_config") or {}).get("runtime_manifest") == origin.path
        and (state.get("correction_controller") or {}).get("config") == config,
        "shared qualified campaign installation differs",
    )
    base = Path(manifest["module_dir"])
    _need(
        base.is_absolute()
        and base.resolve(strict=True) == base
        and not any(p.is_symlink() for p in (base, *base.parents))
        and _REQUIRED <= set(manifest["modules"])
        and len(manifest["modules"]) <= 512,
        "fixed source closure incomplete",
    )
    actual = set()
    for path in base.iterdir():
        _need(not path.is_symlink() and not path.is_dir(), "source directory entry unsupported")
        if path.suffix in {".py", ".pyc", ".pyo"}:
            actual.add(path.name)
    _need(actual == set(manifest["modules"]), "source executable inventory changed")
    if _scope is None:
        closure = {}
        reserved = 0
        for name, expected in manifest["modules"].items():
            _need(
                Path(name).name == name
                and name.endswith(".py")
                and isinstance(expected, str)
                and _SHA.fullmatch(expected),
                "source closure entry invalid",
            )
            path = base / name
            _need(
                path.resolve(strict=True) == path and not path.is_symlink(),
                "source path redirected",
            )
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                _need(
                    stat.S_ISREG(info.st_mode)
                    and info.st_nlink == 1
                    and 0 < info.st_size <= 4 * 1024 * 1024,
                    "source bytes invalid",
                )
                reserved += info.st_size
                _need(reserved <= 64 * 1024 * 1024, "source closure aggregate exceeded")
                data = stream.read(info.st_size + 1)
            _need(
                len(data) == info.st_size and hashlib.sha256(data).hexdigest() == expected,
                "source bytes changed",
            )
            closure[name] = {"path": str(path), "sha256": expected}
    else:
        _native_scope.check(manifest)
        closure = copy.deepcopy(_scope.original_value["source_closure"])
    _need(
        closure["closer_runtime_binding.py"]["sha256"] == runtime._BINDING_SOURCE_SHA256
        and closure["closer_source_installation.py"]["path"] == str(Path(__file__))
        and closure["closer_source_installation.py"]["sha256"] == _SOURCE_SHA256,
        "loaded source installation differs",
    )
    custody = (
        Path(config["state_path"] + ".evidence")
        / f"correction-index-{config['repository_id']}-{config['epoch']}"
    )
    _need(
        custody.resolve(strict=True) == custody
        and not any(p.is_symlink() for p in (custody, *custody.parents))
        and custody.stat().st_uid == os.getuid()
        and custody.stat().st_mode & 0o777 == 0o700,
        "shared custody unavailable",
    )
    namespace, _ = _physical_json(custody / "namespace.json", limit=16384)
    _need(
        namespace
        == {"schema": "local-correction-controller-namespace/v1", "config_sha256": _digest(config)},
        "shared namespace differs",
    )
    anchor, _ = _physical_json(custody / "highwater-0000000000000000.json", limit=16384)
    _need(
        set(anchor)
        == {
            "revision",
            "commit_sha",
            "blob_sha",
            "state_sha256",
            "previous_anchor_sha256",
            "result_name",
            "result_sha256",
        }
        and anchor["revision"] == 0
        and type(anchor["revision"]) is int
        and all(
            isinstance(anchor[k], str) and _COMMIT.fullmatch(anchor[k])
            for k in ("commit_sha", "blob_sha")
        )
        and isinstance(anchor["state_sha256"], str)
        and _SHA.fullmatch(anchor["state_sha256"])
        and all(
            anchor[k] is None for k in ("previous_anchor_sha256", "result_name", "result_sha256")
        )
        and _digest(anchor) == config["bootstrap_anchor_sha256"],
        "preprovisioned anchor missing or changed",
    )
    import importlib

    controller = importlib.import_module("closer_correction_controller")
    controller_pin = closure["closer_correction_controller.py"]
    _need(
        str(Path(controller.__file__)) == controller_pin["path"]
        and controller._LOADED_SOURCE_SHA256 == controller_pin["sha256"],
        "loaded controller inspector source differs",
    )
    inspected = controller.inspect_provisioned_custody(config)
    stable = inspected["identity"]
    _need(
        type(stable) is dict
        and set(stable)
        == {
            "custody_path",
            "namespace",
            "config_sha256",
            "bootstrap_anchor_sha256",
            "device",
            "inode",
        }
        and all(
            isinstance(stable[key], str) and re.fullmatch(r"[0-9]{1,30}", stable[key])
            for key in ("device", "inode")
        )
        and int(stable["inode"]) > 0
        and stable.get("namespace") == custody.name
        and stable.get("custody_path") == str(custody)
        and stable.get("config_sha256") == _digest(config)
        and stable.get("bootstrap_anchor_sha256") == config["bootstrap_anchor_sha256"],
        "shared custody inspector identity differs",
    )
    # This opaque source digest is correlation, not Workflows decision proof.
    # Exclude ALL mutable progress/current invocation fields deliberately.
    projected = {
        "schema": "closer-correction-controller-policy-projection/v1",
        "repository": config["repository"],
        "controller_sha256": _digest(config),
        "installation_sha256": hashlib.sha256(
            json.dumps(
                {
                    "manifest_path": origin.path,
                    "manifest_sha256": origin.sha256,
                    "config": config,
                    "source_closure": closure,
                    "custody": stable,
                },
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest(),
        "purpose": "frontier-correlation-only",
    }
    return {
        "config": config,
        "manifest_path": origin.path,
        "manifest_sha256": origin.sha256,
        "source_closure": closure,
        "module_dir": str(base),
        "policy_projection": projected,
    }, manifest


def coding_tick_knowledge_for(binding):
    """Existing qualified policy knowledge under the actual helper tick only."""
    import closer_coding_continuation as continuation

    frame = inspect.currentframe().f_back
    try:
        _need(
            frame.f_code is continuation.read_tick_selected.__code__
            and frame.f_globals is continuation.__dict__,
            "actual coding tick knowledge reader required",
        )
        scope = frame.f_locals.get("scope")
        _need(
            type(scope) is continuation._CodingScope and scope.binding is binding,
            "source coding tick binding differs",
        )
        scope.check()
        value, _manifest = _qualified_policy(binding)
        _need(
            value["manifest_path"] == scope.manifest_path
            and value["manifest_sha256"] == scope.manifest_sha256
            and value["config"]["state_path"] == str(scope.campaign.path),
            "coding tick knowledge campaign changed",
        )
        scope.check()
        return copy.deepcopy(value)
    finally:
        del frame


def controller_policy_projection(binding):
    """Non-authorizing correlation BEFORE a current invocation/frontier exists.

    Requires deployment qualification, full source/config/custody validation.
    Never issues SourceInstallation or bypasses its signed current admission.
    """
    try:
        value, _manifest = _qualified_policy(binding)
        return copy.deepcopy(value["policy_projection"])
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, AttributeError, ImportError):
        return None


def _snapshot(binding, *, report_path=None, _scope=None):
    if _scope is None:
        return _snapshot_current(binding, report_path=report_path)
    # This exact source frame owns a short read-only digest token. It cannot
    # authorize an effect, escape the call, or reuse any mutable observation.
    with _native_runtime_reads(_scope, binding, inspect.currentframe()) as native_scope:
        return _snapshot_current(
            binding, report_path=report_path, _scope=_scope, _native_scope=native_scope
        )


def _coding_snapshot_membership(value, manifest, context, state):
    """Only the actual qualified factory snapshot can select a registry worker."""
    import closer_coding_continuation as continuation
    import closer_correction_controller as ctl
    import closer_native_observer as native

    frame = inspect.currentframe().f_back
    try:
        _need(
            frame.f_code is _snapshot_current.__code__
            and frame.f_globals is globals()
            and all(
                frame.f_locals.get(k) is v
                for k, v in {
                    "value": value,
                    "manifest": manifest,
                    "context": context,
                    "state": state,
                }.items()
            ),
            "actual coding installation snapshot required",
        )
        selector = context["coding_continuation"]
        registry = continuation.validate_registry(state.get(continuation.REGISTRY_FIELD))
        row = registry.get(selector["reference"]["stable_key"])
        _need(
            row is not None
            and row["state"] in {"launch_intent", "submitted", "active"}
            and continuation._canonical(row["worker"]) == continuation._canonical(selector)
            and all(
                w.get("state") == "terminal"
                and w.get("outcome") in {"ok", "unavailable", "scoped_continuation"}
                for w in state["workers"]
            ),
            "coding source registry membership or prior workers changed",
        )
        config = ctl.ControllerConfig(**value["config"])
        with closing(ctl._Custody(config)) as custody:
            record = continuation._read_protected(custody, selector["reference"])
        _need(
            record["controller_config_sha256"] == _digest(value["config"])
            and record["manifest_path"] == value["manifest_path"]
            and record["manifest_sha256"] == value["manifest_sha256"]
            and record["source_closure_sha256"] == ctl.digest(value["source_closure"]),
            "coding installation original source binding differs",
        )
        native._validate_coding_selector(
            selector, state, config, record, value["policy_projection"]
        )
        capability = continuation.validate_capability(manifest["policy"][continuation.POLICY_FIELD])
        _need(capability["consumer_id"] == record["consumer_id"], "coding static consumer changed")
        return True
    finally:
        del frame


def _snapshot_current(binding, *, report_path=None, _scope=None, _native_scope=None):
    import closer_native_observer as native

    value, manifest = _qualified_policy(binding, _scope=_scope, _native_scope=_native_scope)
    import closer_correction_maintenance as maintenance

    private = maintenance.installation_snapshot(
        binding, value, manifest, native_scope=_native_scope
    )
    if private is not None:
        return private
    # Optional module exists only in a source-pinned publication installation.
    # Absent mode retains the ordinary snapshot and its exact report semantics.
    import sys

    publication_io = sys.modules.get("closer_coding_publication_io")
    if publication_io is not None and getattr(publication_io._CURRENT, "scope", None) is not None:
        path = Path(publication_io.__file__)
        pinned = value["source_closure"].get(path.name)
        _need(
            pinned == {"path": str(path), "sha256": publication_io._SOURCE_SHA256}
            and hashlib.sha256(path.read_bytes()).hexdigest() == publication_io._SOURCE_SHA256,
            "loaded publication IO source differs",
        )
        private = publication_io.installation_snapshot(
            binding, value, manifest, native_scope=_native_scope
        )
        if private is not None:
            return private
    selector = report_path if report_path is not None else os.environ.get("CLOSER_CAPACITY_REPORT")
    _need(isinstance(selector, str) and selector, "current native observation selector absent")
    report_file = Path(selector)
    observation_root = Path(manifest["mechanism_contract"]["observation_root"])
    _need(
        report_file.name == "scoped-capacity.json"
        and report_file.parent.parent == observation_root
        and re.fullmatch(r"[0-9a-f]{32}", report_file.parent.name),
        "selected native observation path is outside qualified scope",
    )
    report, report_raw = _physical_json(report_file, limit=4 * 1024 * 1024)
    signed, _signed_raw = _physical_json(
        report_file.parent / "admission.json", limit=4 * 1024 * 1024
    )
    context = signed.get("record", {}).get("context")
    _need(
        type(context) is dict and context.get("schema") == "native-invocation-admission/v2",
        "full finite native admission unavailable",
    )
    native.validate_admission(manifest, context, for_effects=True, _scope=_native_scope)
    _need(
        context["runtime_manifest"] == value["manifest_path"]
        and context["runtime_manifest_sha256"] == value["manifest_sha256"]
        and context["report"]
        == {"path": str(report_file), "sha256": hashlib.sha256(report_raw).hexdigest()}
        and report.get("runtime_manifest_sha256") == value["manifest_sha256"]
        and signed["record"]["binding"] == native.binding(manifest, _scope=_native_scope),
        "selected native observation differs",
    )
    state, _state_raw = _physical_json(value["config"]["state_path"], limit=4 * 1024 * 1024)
    _need(
        value["config"]["state_path"] == context["campaign"]["state"]
        and all(
            state.get(k) == context["campaign"][k] for k in ("campaign_id", "owner", "generation")
        )
        and (
            _coding_snapshot_membership(value, manifest, context, state)
            if "coding_continuation" in context
            else (state.get("worker_config") or {}).get("finite_frontier")
            == context["finite_frontier"]
        )
        and (state.get("correction_controller") or {}).get("config") == value["config"],
        "current original campaign admission differs",
    )
    return {**value, "admission": copy.deepcopy(context), "report_path": str(report_file)}


def source_installation_for(binding):
    try:
        value = _snapshot(binding)
        issued = object.__new__(SourceInstallation)
        issued._binding, issued._value, issued._seal = binding, value, _SEAL
        return issued
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, AttributeError, ImportError):
        return None


def validate_source_installation(value, *, _scope=None):
    try:
        return (
            type(value) is SourceInstallation
            and value._seal is _SEAL
            and (
                _scope is None
                or (type(_scope) is _OriginalWriterScope and _scope.installation is value)
            )
            and _snapshot(value._binding, report_path=value._value["report_path"], _scope=_scope)
            == value._value
        )
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, AttributeError, ImportError):
        return False


_SCOPE_SEAL = object()
_SCOPE_MAX_FILES = 1024
_SCOPE_MAX_DIRECTORIES = 256
_SCOPE_MAX_BYTES = 96 * 1024 * 1024


def _file_identity(info):
    return (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
        info.st_mode,
        info.st_uid,
        info.st_nlink,
    )


def _directory_identity(info):
    # Mutable descendants do not make an ancestor a new directory. Replacing,
    # aliasing or changing ownership/permissions of any ancestor does.
    return info.st_dev, info.st_ino, info.st_mode, info.st_uid


def _closed_inventory(root, *, recursive):
    result = []
    pending = [(os.fspath(root), "")]
    directories = 0
    while pending:
        directory, prefix = pending.pop()
        directories += 1
        _need(directories <= _SCOPE_MAX_DIRECTORIES, "scoped directory inventory exceeds bound")
        with os.scandir(directory) as iterator:
            entries = list(iterator)
        _need(len(entries) + len(result) <= _SCOPE_MAX_FILES, "scoped inventory exceeds bound")
        for entry in entries:
            # Read fresh no-follow metadata, rather than DirEntry's cached
            # d_type/stat or repeated Python Path construction/type probes.
            info = os.lstat(entry.path)
            _need(not stat.S_ISLNK(info.st_mode), "scoped inventory contains an alias")
            relative = prefix + entry.name
            result.append(relative)
            if stat.S_ISDIR(info.st_mode):
                _need(recursive, "scoped runtime gained a directory")
                pending.append((entry.path, relative + "/"))
            else:
                _need(stat.S_ISREG(info.st_mode), "scoped inventory contains a special file")
    return tuple(sorted(result))


class _PhysicalPackagePins:
    """Open immutable inputs; these fingerprints never authorize an effect."""

    def __init__(self):
        self.files = {}
        self.hashes = {}
        self.ancestors = {}
        self.file_ancestors = {}
        self.inventories = []
        self.bytes = 0
        self.closed = False

    def pin(self, path, expected=None):
        path = Path(path)
        _need(
            not self.closed and path.is_absolute() and path.resolve(strict=True) == path,
            "scoped immutable path redirected",
        )
        if str(path) in self.files:
            _need(
                expected is None or self.hashes[str(path)] == expected,
                "scoped duplicate physical pin has a different content binding",
            )
            return
        _need(len(self.files) < _SCOPE_MAX_FILES, "scoped file descriptor bound exceeded")
        ancestor_names = tuple(map(os.fspath, path.parents))
        for ancestor in ancestor_names:
            info = os.lstat(ancestor)
            _need(stat.S_ISDIR(info.st_mode), "scoped immutable ancestor aliased")
            identity = _directory_identity(info)
            _need(
                ancestor not in self.ancestors or self.ancestors[ancestor] == identity,
                "scoped immutable ancestor changed",
            )
            self.ancestors[ancestor] = identity
            _need(len(self.ancestors) <= _SCOPE_MAX_DIRECTORIES, "scoped ancestor bound exceeded")
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(descriptor)
            _need(
                stat.S_ISREG(info.st_mode)
                and info.st_nlink == 1
                and info.st_uid == os.getuid()
                and not info.st_mode & 0o022
                and 0 < info.st_size <= 16 * 1024 * 1024,
                "scoped immutable input is not an owned bounded regular file",
            )
            _need(
                self.bytes + info.st_size <= _SCOPE_MAX_BYTES, "scoped immutable bytes exceed bound"
            )
            chunks = []
            left = info.st_size + 1
            while left:
                chunk = os.read(descriptor, min(left, 1024 * 1024))
                if not chunk:
                    break
                chunks.append(chunk)
                left -= len(chunk)
            raw = b"".join(chunks)
            _need(
                len(raw) == info.st_size
                and (expected is None or hashlib.sha256(raw).hexdigest() == expected)
                and _file_identity(os.fstat(descriptor)) == _file_identity(info)
                and _file_identity(path.lstat()) == _file_identity(info),
                "scoped immutable bytes changed while pinning",
            )
            self.files[str(path)] = descriptor, _file_identity(info)
            self.file_ancestors[str(path)] = ancestor_names
            self.hashes[str(path)] = hashlib.sha256(raw).hexdigest()
            self.bytes += info.st_size
            descriptor = None
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def inventory(self, root, *, recursive):
        self.inventories.append((root, recursive, _closed_inventory(root, recursive=recursive)))

    def check(self):
        _need(not self.closed, "closed package pins cannot be reused")
        for name, expected in self.ancestors.items():
            current = os.lstat(name)
            _need(
                stat.S_ISDIR(current.st_mode) and _directory_identity(current) == expected,
                "scoped immutable ancestor changed",
            )
        for name, (descriptor, expected) in self.files.items():
            _need(
                _file_identity(os.fstat(descriptor)) == expected
                and _file_identity(os.lstat(name)) == expected,
                "scoped immutable physical input changed",
            )
        for root, recursive, expected in self.inventories:
            _need(
                _closed_inventory(root, recursive=recursive) == expected,
                "scoped closed executable inventory changed",
            )

    def check_selected(self, names):
        _need(not self.closed, "closed package pins cannot be reused")
        checked = set()
        for name in names:
            _need(name in self.files, "unbound selected physical input")
            descriptor, expected = self.files[name]
            _need(
                _file_identity(os.fstat(descriptor)) == expected
                and _file_identity(os.lstat(name)) == expected,
                "selected native physical input changed",
            )
            for key in self.file_ancestors[name]:
                if key not in checked:
                    checked.add(key)
                    current = os.lstat(key)
                    _need(
                        key in self.ancestors
                        and stat.S_ISDIR(current.st_mode)
                        and _directory_identity(current) == self.ancestors[key],
                        "selected native ancestor changed",
                    )

    def close(self):
        if not self.closed:
            self.closed = True
            errors = []
            for descriptor, _ in self.files.values():
                try:
                    os.close(descriptor)
                except OSError as error:
                    errors.append(error)
            self.files.clear()
            self.hashes.clear()
            self.file_ancestors.clear()
            _need(not errors, "scoped descriptor cleanup was incomplete")


class _OriginalWriterScope:
    """Unserializable optimization, bound to one actual original writer lifetime.

    The source-managed fenced context owns the flock until this scope is
    inactive and its pins close. Descriptor fingerprints do not prove OS lock
    ownership against arbitrary trusted host code manually unlocking that FD.
    Mutable admission/state/native observations are never stored as permission.
    """

    def __init__(self, *args, **kwargs):
        raise TypeError("original writer scope is source-issued only")

    def check_context(self, binding):
        import closer_correction_controller as ctl
        import closer_queue as queue

        _need(
            type(self) is _OriginalWriterScope
            and self.seal is _SCOPE_SEAL
            and not self.closed
            and self.pid == os.getpid()
            and self.thread == threading.get_ident()
            and self.installation._binding is binding
            and type(self.controller) is ctl.IndexController
            and self.controller.installation is self.installation
            and getattr(self.controller._writer_context, "validation_scope", None) is self
            and getattr(self.controller._writer_context, "custody", None) is self.custody
            and type(self.custody) is ctl._Custody,
            "original same-thread writer validation scope unavailable",
        )
        _need(
            self.installation._value == self.original_value,
            "issued installation metadata changed within original writer scope",
        )
        current = getattr(queue._callback_threads, "invocation", None)
        _need(
            type(current) is dict
            and current.get("target") == self.target
            and current.get("state_path") == self.queue_path
            and queue._currently_invoking(self.target, self.operation)
            and queue._canonical_bytes(self.operation) == self.operation_bytes,
            "scoped original queue operation changed or expired",
        )
        submitted = self.controller._physical_state(
            self.queue_path, "scoped physical original queue redirected"
        )
        bucket = submitted
        if self.operation.get("finite_frontier") is not None:
            bucket = submitted["frontiers"][self.operation["finite_frontier"]["frontier_id"]]
        _need(
            bucket.get("items", {}).get(self.target, {}).get("operation") == self.operation,
            "scoped physical original queue operation changed",
        )
        self.custody.check()
        _need(
            _file_identity(os.fstat(self.lock)) == self.lock_identity
            and _file_identity(
                os.stat("writer.lock", dir_fd=self.custody.fd, follow_symlinks=False)
            )
            == self.lock_identity,
            "original scoped writer lock changed",
        )

    def check(self, binding):
        self.check_context(binding)
        self.pins.check()

    def full(self):
        self.check(self.installation._binding)
        _need(
            validate_source_installation(self.installation),
            "full original scoped source validation failed",
        )
        self.check(self.installation._binding)

    def close(self):
        self.closed = True
        active = getattr(self, "native_read_context", None)
        if active is not None:
            active.active, active.frame = False, None
            self.native_read_context = None
        self.pins.close()


@contextmanager
def original_writer_validation_scope(controller, operation, custody, lock):
    """Only the already-held fixed controller writer may reuse static work."""
    import closer_correction_controller as ctl
    import closer_native_observer as native
    import closer_queue as queue
    import closer_runtime_binding as runtime

    _need(
        type(controller) is ctl.IndexController
        and getattr(controller._writer_context, "validation_scope", None) is None,
        "nested or foreign writer validation scope refused",
    )
    controller._require_writer(operation, custody)
    installation = controller.installation
    _need(
        type(installation) is SourceInstallation and validate_source_installation(installation),
        "full original factory validation unavailable",
    )
    manifest, raw = _physical_json(installation.manifest_path, limit=16 * 1024 * 1024)
    _need(
        hashlib.sha256(raw).hexdigest() == installation.manifest_sha256,
        "original scoped manifest changed",
    )
    scope = object.__new__(_OriginalWriterScope)
    scope.seal, scope.closed = _SCOPE_SEAL, False
    scope.controller, scope.installation = controller, installation
    scope.original_value = copy.deepcopy(installation._value)
    scope.operation, scope.operation_bytes = operation, queue._canonical_bytes(operation)
    scope.pid, scope.thread = os.getpid(), threading.get_ident()
    current = queue._callback_threads.invocation
    scope.target, scope.queue_path = current["target"], current["state_path"]
    scope.custody, scope.lock = custody, lock
    scope.lock_identity = _file_identity(os.fstat(lock))
    scope.manifest = copy.deepcopy(manifest)
    scope.pins = _PhysicalPackagePins()
    scope.native_read_context = None
    try:
        scope.pins.pin(Path(installation.manifest_path), installation.manifest_sha256)
        for name, row in installation.source_closure.items():
            scope.pins.pin(Path(row["path"]), row["sha256"])
        base = Path(manifest["module_dir"])
        scope.pins.inventory(base, recursive=False)
        public = manifest.get("public_runtime_descriptor")
        if public:
            scope.pins.pin(base / public)
        core = Path(manifest["portable"]["package_root"]).resolve(strict=True)
        scope.pins.pin(core / "closer-core-package.json")
        descriptor, _raw = _physical_json(core / "closer-core-package.json", limit=1048576)
        _need(
            set(descriptor.get("files", {})) == set(runtime.CORE_FILES),
            "scoped Core inventory differs",
        )
        for name, sha in descriptor["files"].items():
            scope.pins.pin(core / name, sha)
        scope.pins.inventory(core, recursive=True)
        _native_value, wrapper, interpreter, native_root = native.contract(manifest)
        scope.native_runtime_files = {
            str(wrapper): _native_value["wrapper"]["sha256"],
            str(interpreter): _native_value["interpreter"]["sha256"],
        }
        for name, expected in scope.native_runtime_files.items():
            scope.pins.pin(Path(name), expected)
        scope.pins.pin(native_root / "native-qualification.json")
        signed, _raw = _physical_json(
            native_root / "native-qualification.json", limit=4 * 1024 * 1024
        )
        record = signed["record"]
        for row in record["phases"].values():
            _need(Path(row["file"]).name == row["file"], "scoped qualification phase path invalid")
            scope.pins.pin(native_root / record["nonce"] / row["file"], row["sha256"])
        controller._writer_context.validation_scope = scope
        scope.full()
        try:
            yield scope
        finally:
            scope.full()
    finally:
        if getattr(controller._writer_context, "validation_scope", None) is scope:
            del controller._writer_context.validation_scope
        scope.close()


class _NativeRuntimeRead:
    def __init__(self, *args, **kwargs):
        raise TypeError("native digest read context is source-issued only")

    def check(self, manifest):
        _need(
            type(self) is _NativeRuntimeRead
            and self.seal is _SCOPE_SEAL
            and self.active
            and self.frame is not None
            and self.frame.f_code is _snapshot.__code__
            and self.frame.f_globals is globals()
            and self.scope.native_read_context is self,
            "original native digest read context unavailable",
        )
        self.scope.check_context(self.scope.installation._binding)
        _need(manifest == self.scope.manifest, "scoped native manifest changed")
        cursor = inspect.currentframe()
        found = False
        try:
            for _ in range(128):
                if cursor is self.frame:
                    found = True
                    break
                if cursor is None:
                    break
                cursor = cursor.f_back
        finally:
            del cursor
        _need(found, "native digest context escaped original snapshot frame")
        self.scope.pins.check_selected(self.scope.native_runtime_files)


@contextmanager
def _native_runtime_reads(scope, binding, frame):
    _need(
        type(scope) is _OriginalWriterScope
        and scope.seal is _SCOPE_SEAL
        and frame is not None
        and frame.f_code is _snapshot.__code__
        and frame.f_globals is globals()
        and frame.f_locals.get("_scope") is scope
        and frame.f_locals.get("binding") is binding,
        "fixed original snapshot frame required for native digest reuse",
    )
    scope.check(binding)
    _need(scope.native_read_context is None, "nested native digest read context refused")
    token = object.__new__(_NativeRuntimeRead)
    token.seal, token.scope, token.frame, token.active = _SCOPE_SEAL, scope, frame, True
    scope.native_read_context = token
    try:
        yield token
    finally:
        token.active, token.frame = False, None
        if scope.native_read_context is token:
            scope.native_read_context = None
        scope.check(binding)


def _scoped_native_runtime_digests(token, manifest):
    """Two physical native digests, only inside an actual checked snapshot.

    No global/TLS discovery, serialized permit, caller digest or baseline renewal.
    All157 pins are checked by the enclosing snapshot before and after this
    read-only frame; each native contract freshly checks these two files and
    the original queue/writer context, plus its unchanged mutable predicates.
    """
    _need(
        type(token) is _NativeRuntimeRead and token.seal is _SCOPE_SEAL,
        "source-issued native digest read context required",
    )
    token.check(manifest)
    scope = token.scope
    value = manifest["mechanism_contract"]
    expected = {value[k]["path"]: value[k]["sha256"] for k in ("wrapper", "interpreter")}
    _need(
        expected == scope.native_runtime_files
        and all(scope.pins.hashes.get(name) == sha for name, sha in expected.items()),
        "native digests differ from original physically pinned runtime",
    )
    return {name: scope.pins.hashes[name] for name in expected}
