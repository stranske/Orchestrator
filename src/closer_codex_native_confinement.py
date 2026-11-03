"""Source-owned native confinement join, using the existing owned stdio child.

Profile construction is desired configuration DATA, not enforcement authority.
Only the exact owned-session observer may obtain the fixed native probe result.
Actual native enforcement remains unobserved until that source path runs.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import secrets
import stat
import threading
import tomllib
import weakref
from dataclasses import dataclass
from pathlib import Path

import closer_codex_confinement_probe as _probe_source


class ConfinementHeld(RuntimeError):
    """Fixed codes only; never raw config, credentials or native diagnostics."""


def _need(value, code):
    if not value:
        raise ConfinementHeld(code)


def _digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def _path(value):
    _need(
        type(value) is str
        and value.startswith("/")
        and "\0" not in value
        and len(value) <= 4096
        and len(Path(value).parts) <= 64,
        "physical_path_required",
    )
    p = Path(value)
    _need(
        str(p) == value and ".." not in p.parts and p.resolve(strict=True) == p,
        "canonical_physical_path_required",
    )
    _need(not any(x.is_symlink() for x in (p, *p.parents)), "physical_alias_refused")
    return p


def _within(a, b):
    return a == b or a.startswith(b.rstrip("/") + "/")


def _deny_path(value):
    """Canonical deny selector, including not-yet-created credential files."""
    _need(
        type(value) is str
        and value.startswith("/")
        and "\0" not in value
        and len(value) <= 4096
        and len(Path(value).parts) <= 64,
        "credential_selector_not_canonical",
    )
    p = Path(value)
    _need(
        str(p) == value
        and p.resolve(strict=False) == p
        and not any(x.is_symlink() for x in (p, *p.parents)),
        "credential_selector_not_canonical",
    )
    return value


def _file_pin(path, *, bound, expected=None):
    p = _path(path)
    fd = os.open(p, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        before = os.fstat(fd)
        _need(stat.S_ISREG(before.st_mode) and 0 < before.st_size <= bound, "file_bound_or_type")
        h = hashlib.sha256()
        while raw := os.read(fd, 65536):
            h.update(raw)
        after = os.fstat(fd)

        def identity(s):
            return (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)

        _need(
            identity(before) == identity(after) == identity(p.stat(follow_symlinks=False)),
            "file_changed",
        )
        _need(expected is None or h.hexdigest() == expected, "file_pin_changed")
        return (str(p), *identity(after), h.hexdigest())
    finally:
        os.close(fd)


def _directory_pin(path):
    p = _path(path)
    s = p.stat(follow_symlinks=False)
    _need(stat.S_ISDIR(s.st_mode), "physical_directory_required")
    return (str(p), s.st_dev, s.st_ino, s.st_uid, s.st_mode)


_TOOL_OVERRIDES = (
    'web_search="disabled"',
    "notify=[]",
    "features.apps=false",
    "features.plugins=false",
    "features.hooks=false",
    "features.multi_agent=false",
    "features.multi_agent_v2=false",
    "features.js_repl=false",
    "features.code_mode=false",
    "features.code_mode_host=false",
    "features.code_mode_prewarm=false",
    "features.request_permissions_tool=false",
    "features.tool_suggest=false",
    "features.skill_mcp_dependency_install=false",
    "features.view_image=false",
    "features.browser_use=false",
    "features.browser_use_external=false",
    "features.computer_use=false",
    "features.remote_plugin=false",
    "features.image_generation=false",
    "features.enable_mcp_apps=false",
    "features.memories=false",
    "features.network_proxy=false",
    "features.skill_search=false",
    "features.skill_env_var_dependency_prompt=false",
)
_PUBLIC_RUNTIME_ROOTS = frozenset(
    {"/usr/lib", "/usr/share", "/System/Library", "/opt/anaconda3/lib/python3.12"}
)
_CHECKS = frozenset(
    {
        "workspace_read_write",
        "workspace_rename_delete",
        "outside_read_denied",
        "outside_create_denied",
        "outside_rename_denied",
        "outside_delete_denied",
        "network_bind_denied",
        "network_connect_denied",
    }
)
_NATIVE_BINARY_SHA256 = "112fae7a5a1223e673c8a1791d32338f37df8b527ff1159bb8adac6c4dbf1b4b"
_PLATFORM_PUBLIC_READ_ROOTS = (
    "/Library",
    "/Applications",
    "/System",
    "/usr",
    "/bin",
    "/sbin",
    "/etc",
    "/private/etc",
    "/var/db",
    "/private/var/db",
    "/opt/homebrew/lib",
    "/usr/local/lib",
    "/dev",
)
_WITNESSES: weakref.WeakKeyDictionary[object, tuple[ConfinementProfile, dict, int]] = (
    weakref.WeakKeyDictionary()
)
_ATTEMPTS: weakref.WeakKeyDictionary[object, tuple[ConfinementProfile, str]] = (
    weakref.WeakKeyDictionary()
)
_PREPARED: weakref.WeakValueDictionary[int, ConfinementProfile] = weakref.WeakValueDictionary()
_LOADED_SHA = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def _configuration_paths(workspace, codex_home):
    # Bound all native local parent-project candidates, including absent files.
    paths = {str(Path(codex_home) / "config.toml")}
    for root in (Path(workspace), *Path(workspace).parents):
        paths.add(str(root / ".codex" / "config.toml"))
    return sorted(paths)


def _configuration_closure(workspace, codex_home):
    result = []
    for name in _configuration_paths(workspace, codex_home):
        p = Path(name)
        if not p.exists():
            _need(not p.is_symlink(), "configuration_alias_refused")
            result.append((name, None))
            continue
        pin = _file_pin(name, bound=65536)
        raw = p.read_bytes()
        _need(hashlib.sha256(raw).hexdigest() == pin[-1], "configuration_changed")
        try:
            value = tomllib.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeError):
            raise ConfinementHeld("configuration_invalid") from None
        servers = value.get("mcp_servers", {})
        _need(
            type(servers) is dict
            and all(type(v) is dict and v.get("enabled") is False for v in servers.values()),
            "mcp_configuration_not_closed",
        )
        # Profile layering must not append grants to our generated named profile.
        permissions = value.get("permissions", {})
        _need(
            type(permissions) is dict
            and not any(str(k).startswith("source-coding-") for k in permissions),
            "generated_profile_collision",
        )
        result.append((name, pin))
    return tuple(result)


@dataclass(frozen=True)
class ConfinementProfile:
    permission_profile: str
    cli_overrides: tuple
    helper: tuple
    interpreter: tuple
    native_binary: tuple
    workspace: str
    private_root: str
    codex_home: str
    custody_path: str
    home: str
    roots: tuple
    denied: tuple
    configuration: tuple
    directories: tuple


def prepare_confinement_profile(spec, *, workspace, private_root, codex_home, custody_path, home):
    """Build only closed desired policy DATA; the source owner authenticates spec."""
    _need(
        type(spec) is dict
        and set(spec)
        == {
            "interpreter_path",
            "interpreter_sha256",
            "native_binary_path",
            "native_binary_sha256",
            "helper_path",
            "helper_sha256",
            "runtime_read_roots",
            "additional_credential_roots",
        },
        "confinement_selection_invalid",
    )
    paths = (workspace, private_root, codex_home, custody_path, home)
    directories = tuple(_directory_pin(str(x)) for x in paths)
    _need(
        directories[1][3] == os.getuid() and stat.S_IMODE(directories[1][4]) == 0o700,
        "private_root_not_owned",
    )
    _need(
        all(
            type(spec[k]) is str
            and len(spec[k]) == 64
            and all(v in "0123456789abcdef" for v in spec[k])
            for k in ("interpreter_sha256", "native_binary_sha256", "helper_sha256")
        ),
        "source_file_sha256_required",
    )
    interpreter = _file_pin(
        spec["interpreter_path"], bound=512 * 1024 * 1024, expected=spec["interpreter_sha256"]
    )
    native_binary = _file_pin(
        spec["native_binary_path"], bound=512 * 1024 * 1024, expected=spec["native_binary_sha256"]
    )
    helper = _file_pin(spec["helper_path"], bound=65536, expected=spec["helper_sha256"])
    _need(Path(helper[0]).name == "closer_codex_confinement_probe.py", "fixed_helper_required")
    roots, extra = spec["runtime_read_roots"], spec["additional_credential_roots"]
    _need(
        type(roots) is list
        and 1 <= len(roots) <= 4
        and len(set(roots)) == len(roots)
        and all(x in _PUBLIC_RUNTIME_ROOTS for x in roots),
        "runtime_roots_not_closed",
    )
    _need(
        type(extra) is list
        and len(extra) <= 16
        and all(
            type(x) is str and x.startswith("/") and ".." not in Path(x).parts and "\0" not in x
            for x in extra
        ),
        "credential_roots_invalid",
    )
    extra = [_deny_path(x) for x in extra]
    denied = tuple(
        sorted(
            {
                str(home),
                str(custody_path),
                str(codex_home),
                *(
                    str(Path(home) / suffix)
                    for suffix in (
                        ".ssh",
                        ".aws",
                        ".config",
                        ".codex",
                        ".netrc",
                        ".git-credentials",
                        "Library/Keychains",
                    )
                ),
                *extra,
            }
        )
    )
    for root in denied:
        _need(
            not any(
                _within(str(x), root) or _within(root, str(x)) for x in (workspace, private_root)
            ),
            "protected_and_writable_layout_overlap",
        )
        _need(
            not any(
                _within(root, x) or _within(x, root)
                for x in (*roots, helper[0], interpreter[0], native_binary[0])
            ),
            "protected_and_readable_layout_overlap",
        )
        _need(
            not any(_within(root, x) or _within(x, root) for x in _PLATFORM_PUBLIC_READ_ROOTS),
            "protected_platform_read_overlap",
        )
    configuration = _configuration_closure(str(workspace), str(codex_home))
    protected_config = {
        value
        for path, _pin in configuration
        if any(_within(path, str(root)) for root in (workspace, private_root))
        for value in (path, str(Path(path).parent))
    }
    definition = {
        "filesystem": {
            **{
                x: "read" for x in (*roots, helper[0], interpreter[0], native_binary[0], ":minimal")
            },
            str(workspace): "write",
            str(private_root): "write",
            str(Path(private_root) / "coding-route-gh"): "read",
            str(Path(private_root) / "coding-route-bashenv.sh"): "read",
            **{x: "deny" for x in protected_config},
            **{x: "deny" for x in denied},
            **{str(Path(x) / "site-packages"): "deny" for x in roots if x.endswith("python3.12")},
            str(Path(private_root) / ".codex-denied-probes"): "deny",
        },
        "network": {
            "enabled": False,
            "allow_local_binding": False,
            "dangerously_allow_all_unix_sockets": False,
        },
    }
    # FS helpers add this platform read set and the self binary. Its exact source
    # closure must be joined at runtime; it is not a caller-provided grant.
    name = "source-coding-" + _digest(definition)[:24]
    fs = definition["filesystem"]
    table = ",".join(json.dumps(k) + "=" + json.dumps(v) for k, v in sorted(fs.items()))
    override = f"permissions.{name}.filesystem={{{table}}}"
    overrides = (
        override,
        f"permissions.{name}.network.enabled=false",
        f"permissions.{name}.network.allow_local_binding=false",
        f"permissions.{name}.network.dangerously_allow_all_unix_sockets=false",
        *_TOOL_OVERRIDES,
    )
    plan = ConfinementProfile(
        name,
        overrides,
        helper,
        interpreter,
        native_binary,
        str(workspace),
        str(private_root),
        str(codex_home),
        str(custody_path),
        str(home),
        tuple(roots),
        denied,
        configuration,
        directories,
    )
    _PREPARED[id(plan)] = plan
    return plan


def _recheck(plan):
    _need(
        type(plan) is ConfinementProfile and _PREPARED.get(id(plan)) is plan,
        "source_prepared_profile_required",
    )
    for pin in plan.directories:
        _need(_directory_pin(pin[0]) == pin, "layout_changed")
    _need(
        _file_pin(plan.helper[0], bound=65536, expected=plan.helper[-1]) == plan.helper,
        "helper_changed",
    )
    _need(
        _file_pin(plan.interpreter[0], bound=512 * 1024 * 1024, expected=plan.interpreter[-1])
        == plan.interpreter,
        "interpreter_changed",
    )
    _need(
        _file_pin(plan.native_binary[0], bound=512 * 1024 * 1024, expected=plan.native_binary[-1])
        == plan.native_binary,
        "native_binary_changed",
    )
    _need(
        _configuration_closure(plan.workspace, plan.codex_home) == plan.configuration,
        "configuration_changed",
    )


def verify_confinement_profile(profile):
    """Fresh source/config/physical checks only; never repeats a native probe."""
    _recheck(profile)


def probe_request_data(plan, allowed, outside, nonce, timeout_ms):
    """Exact request DATA; only the private source adapter may emit it."""
    _need(
        type(plan) is ConfinementProfile and _PREPARED.get(id(plan)) is plan,
        "source_prepared_profile_required",
    )
    _need(type(timeout_ms) is int and 1 <= timeout_ms <= 5000, "probe_timeout_invalid")
    _need(
        type(nonce) is str and len(nonce) == 32 and all(x in "0123456789abcdef" for x in nonce),
        "probe_nonce_invalid",
    )
    _need(
        str(Path(allowed).parent) == plan.workspace
        and Path(allowed).name == ".codex-probe-" + nonce,
        "probe_workspace_invalid",
    )
    _need(
        str(Path(outside).parent) == str(Path(plan.private_root) / ".codex-denied-probes")
        and Path(outside).name == nonce,
        "probe_outside_invalid",
    )
    return {
        "command": [plan.interpreter[0], "-I", "-S", "-B", plan.helper[0], allowed, outside, nonce],
        "permissionProfile": plan.permission_profile,
        "cwd": plan.workspace,
        "timeoutMs": timeout_ms,
        "outputBytesCap": 4096,
    }


def validate_probe_result_data(result, nonce):
    _need(
        type(result) is dict
        and set(result) == {"exitCode", "stdout", "stderr"}
        and type(result["exitCode"]) is int
        and result["exitCode"] == 0
        and result["stderr"] == ""
        and type(result["stdout"]) is str
        and len(result["stdout"].encode()) <= 4096,
        "native_probe_result_unavailable",
    )

    def pairs(rows):
        out = {}
        for k, v in rows:
            _need(k not in out, "probe_duplicate_key")
            out[k] = v
        return out

    try:
        value = json.loads(result["stdout"], object_pairs_hook=pairs)
    except (ValueError, UnicodeError):
        raise ConfinementHeld("native_probe_result_invalid") from None
    _need(
        type(value) is dict
        and set(value) == {"schema", "nonce", "checks"}
        and value["schema"] == "codex-fixed-confinement-probe/v1"
        and value["nonce"] == nonce
        and type(value["checks"]) is dict
        and set(value["checks"]) == _CHECKS
        and all(x is True for x in value["checks"].values()),
        "native_probe_checks_failed",
    )
    return {
        "schema": "codex-confinement-probe-data/v1",
        "checks": sorted(_CHECKS),
        "authority": False,
    }


def project_configuration_data(config_result, requirements_result, plan):
    """Discard all non-allowlisted native configuration, including secret values."""
    _need(
        type(config_result) is dict
        and set(config_result) <= {"config", "origins", "layers"}
        and type(config_result.get("config")) is dict
        and config_result.get("layers") is None,
        "native_configuration_shape_invalid",
    )
    _need(
        type(requirements_result) is dict and set(requirements_result) == {"requirements"},
        "native_requirements_shape_invalid",
    )
    config = config_result["config"]
    requirements = requirements_result["requirements"]
    _need(requirements is None or type(requirements) is dict, "native_requirements_shape_invalid")
    requirements = requirements or {}
    # Pinned native merge uses entry.or_insert for non-Bedrock keys: configured
    # and required openai definitions cannot replace the builtin. Still reject
    # unknown map wire shapes; never retain arbitrary provider/auth contents.
    for providers in (config.get("model_providers"), requirements.get("modelProviders")):
        _need(
            providers is None
            or type(providers) is dict
            and all(type(k) is str and type(v) is dict for k, v in providers.items()),
            "native_provider_map_shape_invalid",
        )
    _need(
        config.get("model_provider") == "openai"
        and config.get("chatgpt_base_url") == "https://chatgpt.com/backend-api/"
        and config.get("openai_base_url") == "https://chatgpt.com/backend-api/codex",
        "native_first_party_route_changed",
    )
    _need(
        requirements.get("modelProvider") in (None, "openai")
        and requirements.get("chatgptBaseUrl") in (None, "https://chatgpt.com/backend-api/"),
        "native_required_route_conflict",
    )
    features = config.get("features")
    _need(type(features) is dict, "native_feature_projection_missing")
    disabled = [
        x.removeprefix("features.").removesuffix("=false")
        for x in _TOOL_OVERRIDES
        if x.startswith("features.")
    ]
    _need(
        all(features.get(key) is False for key in disabled),
        "native_optional_tool_enabled_or_unknown",
    )
    required_features = requirements.get("featureRequirements") or {}
    _need(
        type(required_features) is dict
        and all(type(v) is bool for v in required_features.values())
        and not any(required_features.get(key) is True for key in disabled),
        "native_optional_tool_requirement_conflict",
    )
    _need(requirements.get("network") is None, "native_managed_network_requires_review")
    _need(config.get("web_search") == "disabled", "native_hosted_web_not_disabled")
    _need(config.get("notify") in (None, []), "native_notify_hook_not_disabled")
    servers = config.get("mcp_servers", {})
    _need(
        type(servers) is dict
        and all(type(v) is dict and v.get("enabled") is False for v in servers.values()),
        "native_mcp_not_disabled",
    )
    profiles = requirements.get("allowedPermissionProfiles")
    _need(
        profiles is None
        or type(profiles) is dict
        and profiles.get(plan.permission_profile) is True,
        "native_permission_profile_disallowed",
    )
    default = requirements.get("defaultPermissions")
    _need(
        default is None or default == plan.permission_profile,
        "native_permission_profile_requirement_conflict",
    )
    permissions = config.get("permissions")
    _need(
        type(permissions) is dict and type(permissions.get(plan.permission_profile)) is dict,
        "native_permission_definition_missing",
    )
    # Compare the full selected definition, rather than accepting an ID/hash.
    expected = {}
    for override in plan.cli_overrides[:4]:
        node = tomllib.loads(override)["permissions"][plan.permission_profile]
        if "filesystem" in node:
            expected["filesystem"] = node["filesystem"]
        expected.setdefault("network", {}).update(node.get("network", {}))
    _need(permissions[plan.permission_profile] == expected, "native_permission_definition_changed")
    return {
        "schema": "codex-effective-tool-surface-data/v1",
        "disabled_features": sorted(disabled),
        "web_search": "disabled",
        "enabled_mcp_servers": 0,
        "selected_policy_sha256": _digest(expected),
        "first_party_route": "fixed-openai-chatgpt",
        "authority": False,
    }


def observe_effective_confinement(session):
    """Actual same-session source observer; no detached successful issuer port."""
    try:
        import closer_codex_owned_stdio as owned

        caller = inspect.currentframe().f_back
        _need(
            caller.f_code is owned._observe_effective_confinement.__code__
            and caller.f_globals is owned.__dict__
            and type(session) is owned._OwnedSession,
            "actual_owned_confinement_context_required",
        )
        session.check()
        root, _, _ = owned._reader_live(session.reader)
        _need(
            root.installation.source_closure.get(Path(__file__).name)
            == {"path": str(Path(__file__)), "sha256": _LOADED_SHA},
            "confinement_source_pin_missing",
        )
        plan = session.route.confinement
        _recheck(plan)
        _need(
            session.route.cwd == plan.workspace
            and session.route.permission == plan.permission_profile
            and session.guard.custody.path == Path(plan.custody_path),
            "selected_confinement_context_changed",
        )
        config_result, requirements_result = owned.confinement_configuration_from_source(session)
        projection = project_configuration_data(config_result, requirements_result, plan)
        del config_result, requirements_result
        # A pure shell probe cannot by itself qualify the extra FS helper policy.
        # The real source producer is deliberately a required, separately reviewed
        # join rather than a Boolean or model-supplied evidence mapping.
        _require_builtin_helper_coverage(session, plan)
        if session in _WITNESSES:
            cached_plan, cached_projection, thread = _WITNESSES[session]
            _need(
                cached_plan is plan
                and cached_projection == projection
                and thread == threading.get_ident(),
                "confinement_session_changed",
            )
            snapshot = session.protocol.snapshot()["native_thread_selection"]
            if snapshot is not None:
                _need(
                    snapshot["active_permission_profile"]["id"] == plan.permission_profile
                    and snapshot["cwd"] == plan.workspace
                    and snapshot["workspace_roots"] == [plan.workspace],
                    "actual_thread_policy_changed",
                )
            return
        _need(
            session.phase in {"observing", "metadata"} and not session.turn_written,
            "probe_phase_invalid",
        )
        _need(session not in _ATTEMPTS, "native_probe_owned_no_repeat")
        nonce = secrets.token_hex(16)
        # The controller's original bootstrap intent already prevents resubmission.
        # This distinct create-only record owns the one command within that child.
        intent = {
            "schema": "codex-confinement-probe-intent/v1",
            "operation_sha256": _digest(session.guard.operation),
            "permission_profile": plan.permission_profile,
            "outer_argv_sha256": _digest(list(session.route.transport_argv)),
            "nonce": nonce,
            "task_authorized": False,
        }
        _ATTEMPTS[session] = (plan, nonce)
        name = "codex-confinement-probe-" + _digest(intent) + ".json"
        _need(session.guard.custody.create(name, intent), "native_probe_intent_already_owned")
        _need(
            _digest(session.guard.custody.read(name)) == _digest(intent),
            "native_probe_intent_readback_failed",
        )
        allowed = Path(plan.workspace) / (".codex-probe-" + nonce)
        denied_parent = Path(plan.private_root) / ".codex-denied-probes"
        if not denied_parent.exists():
            denied_parent.mkdir(mode=0o700)
        _directory_pin(str(denied_parent))
        _need(
            denied_parent.stat().st_uid == os.getuid()
            and stat.S_IMODE(denied_parent.stat().st_mode) == 0o700,
            "probe_parent_not_owned",
        )
        outside = denied_parent / nonce
        # These are owned transient fixture files, never genuine protected data.
        allowed.mkdir(mode=0o700)
        try:
            outside.mkdir(mode=0o700)
            try:
                for name in ("read", "rename", "delete"):
                    fd = os.open(
                        outside / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
                    )
                    try:
                        os.write(fd, b"synthetic-confinement-data")
                    finally:
                        os.close(fd)
                params = probe_request_data(plan, str(allowed), str(outside), nonce, 5000)
                response = owned.command_exec_probe_from_confinement(session, params)
                validate_probe_result_data(response, nonce)
                session.check()
                _recheck(plan)
            finally:
                for name in ("read", "rename", "delete", "create", "renamed"):
                    try:
                        (outside / name).unlink()
                    except FileNotFoundError:
                        pass
                outside.rmdir()
        finally:
            for name in ("allowed", "renamed"):
                try:
                    (allowed / name).unlink()
                except FileNotFoundError:
                    pass
            allowed.rmdir()
        _WITNESSES[session] = (plan, projection, threading.get_ident())
    except (OSError, ValueError, TypeError, KeyError, ImportError):
        raise ConfinementHeld("confinement_physical_or_source_observation_unavailable") from None


def _require_builtin_helper_coverage(session, plan):
    # Versioned source confidence: actual 0.160 local filesystem operations use
    # SandboxedFileSystem/FileSystemSandboxRunner and shared SandboxManager.
    # Its automatic :minimal and self-exe reads are already present here, so
    # they do not widen this selected policy. This is not build attestation.
    _need(
        plan.native_binary[-1] == _NATIVE_BINARY_SHA256
        and session.route.provider_argv[0] == plan.native_binary[0],
        "native_shared_enforcer_version_unqualified",
    )
    root, _, _ = __import__("closer_codex_owned_stdio")._reader_live(session.reader)
    _need(
        root.installation.source_closure.get(Path(plan.helper[0]).name)
        == {"path": plan.helper[0], "sha256": plan.helper[-1]},
        "probe_helper_source_pin_missing",
    )
    _need(
        Path(_probe_source.__file__).resolve() == Path(plan.helper[0]),
        "loaded_probe_helper_path_changed",
    )
    _need(os.uname().sysname == "Darwin", "selected_native_platform_unsupported")


HELD_CODES = frozenset(
    {
        "actual_owned_confinement_context_required",
        "actual_thread_policy_changed",
        "canonical_physical_path_required",
        "configuration_alias_refused",
        "confinement_physical_or_source_observation_unavailable",
        "configuration_changed",
        "configuration_invalid",
        "confinement_selection_invalid",
        "confinement_session_changed",
        "confinement_source_pin_missing",
        "credential_roots_invalid",
        "credential_selector_not_canonical",
        "file_bound_or_type",
        "file_changed",
        "file_pin_changed",
        "fixed_helper_required",
        "generated_profile_collision",
        "helper_changed",
        "interpreter_changed",
        "layout_changed",
        "loaded_probe_helper_path_changed",
        "mcp_configuration_not_closed",
        "native_binary_changed",
        "native_configuration_shape_invalid",
        "native_feature_projection_missing",
        "native_first_party_route_changed",
        "native_hosted_web_not_disabled",
        "native_managed_network_requires_review",
        "native_mcp_not_disabled",
        "native_notify_hook_not_disabled",
        "native_optional_tool_enabled_or_unknown",
        "native_optional_tool_requirement_conflict",
        "native_permission_definition_changed",
        "native_permission_definition_missing",
        "native_permission_profile_disallowed",
        "native_permission_profile_requirement_conflict",
        "native_probe_checks_failed",
        "native_probe_intent_already_owned",
        "native_probe_intent_readback_failed",
        "native_probe_owned_no_repeat",
        "native_probe_result_invalid",
        "native_probe_result_unavailable",
        "native_provider_map_shape_invalid",
        "native_required_route_conflict",
        "native_requirements_shape_invalid",
        "native_shared_enforcer_version_unqualified",
        "physical_alias_refused",
        "physical_directory_required",
        "physical_path_required",
        "private_root_not_owned",
        "probe_duplicate_key",
        "probe_helper_source_pin_missing",
        "probe_nonce_invalid",
        "probe_outside_invalid",
        "probe_parent_not_owned",
        "probe_phase_invalid",
        "probe_timeout_invalid",
        "probe_workspace_invalid",
        "protected_and_readable_layout_overlap",
        "protected_and_writable_layout_overlap",
        "protected_platform_read_overlap",
        "runtime_roots_not_closed",
        "selected_confinement_context_changed",
        "selected_native_platform_unsupported",
        "source_file_sha256_required",
        "source_prepared_profile_required",
    }
)
