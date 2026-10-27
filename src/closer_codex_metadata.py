"""Original-callback Codex metadata observation, never task or consumer authority.

The native operation has no prompt, thread/start, turn/start, tools, login or
configuration-write RPC. Native config/cloud/auth loading may update its own
metadata/cache. Sequential feature observations are not a cloud-policy lease.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import math
import os
import re
import selectors
import signal
import subprocess
import threading
import time
from pathlib import Path

import closer_correction_controller as ctl
from closer_codex_appserver_bootstrap import AccountMetadataProtocol, ProtocolHeld

_SOURCE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
POLICY = "codex_metadata_qualification"
PURPOSE = "closer-codex-account-metadata/v1"
_MAX_SECONDS = 30
_MAX_BYTES = 131072
_FEATURE = re.compile(
    r"([a-z][a-z0-9_.]*)\s{2,}(under development|experimental|stable|deprecated|removed)\s{2,}(true|false)"
)


class MetadataHeld(RuntimeError):
    """Fixed codes only; never echo native diagnostics or account values."""


def _need(value, code):
    if not value:
        raise MetadataHeld(code)


def feature_values(raw):
    """Parse the real 0.160 features-list text renderer, not invented JSON."""
    _need(type(raw) is bytes and 0 < len(raw) <= 32768, "feature_bytes_invalid")
    try:
        lines = raw.decode("utf-8").splitlines()
    except UnicodeError:
        raise MetadataHeld("feature_bytes_invalid") from None
    _need(0 < len(lines) <= 512, "feature_rows_invalid")
    result = {}
    for line in lines:
        match = _FEATURE.fullmatch(line.strip())
        _need(match is not None, "feature_row_invalid")
        key, _, boolean = match.groups()
        _need(key not in result, "feature_duplicate")
        result[key] = boolean == "true"
    _need(result.get("plugins") is False, "effective_plugins_not_disabled")
    # These existing CLI overrides are defense in depth; an observed cloud
    # override is never normalized back to the desired false value.
    for key in ("apps", "remote_plugin", "hooks", "multi_agent", "multi_agent_v2"):
        _need(result.get(key, False) is False, "effective_optional_integration_enabled")
    return result


def _source_spec(controller):
    import closer_codex_owned_stdio as owned
    import closer_source_installation as source

    _need(type(controller) is ctl.IndexController, "source_controller_required")
    installation = controller._source_installation()
    for module, expected in ((Path(__file__), _SOURCE_SHA256),):
        _need(
            installation.source_closure.get(module.name)
            == {"path": str(module), "sha256": expected},
            "metadata_source_not_installed",
        )
        owned._physical(str(module), expected=expected)
    manifest, raw = source._physical_json(installation.manifest_path, limit=16 * 1024 * 1024)
    _need(
        hashlib.sha256(raw).hexdigest() == installation.manifest_sha256,
        "original_manifest_changed",
    )
    spec = manifest.get("policy", {}).get(POLICY)
    fields = {
        "schema",
        "binary_path",
        "binary_sha256",
        "home",
        "codex_home",
        "private_root",
        "cwd",
        "profile_id",
    }
    _need(
        type(spec) is dict and set(spec) == fields and spec["schema"] == PURPOSE,
        "signed_metadata_selection_missing",
    )
    _need(
        type(spec["profile_id"]) is str and 0 < len(spec["profile_id"]) <= 128,
        "metadata_profile_invalid",
    )
    _need(
        type(spec["binary_sha256"]) is str and re.fullmatch(r"[0-9a-f]{64}", spec["binary_sha256"]),
        "metadata_binary_digest_invalid",
    )
    owned._managed_route_absent()
    pins = tuple(
        owned._physical(spec[k], directory=True)
        for k in ("home", "codex_home", "private_root", "cwd")
    )
    pins += (owned._physical(spec["binary_path"], expected=spec["binary_sha256"], binary=True),)
    return spec, pins


def _source_selection(controller, operation):
    spec, pins = _source_spec(controller)
    _need(
        operation.get("action") == "disposition"
        and operation.get("binding", {}).get(POLICY) == ctl.digest(spec),
        "original_metadata_operation_not_selected",
    )
    return spec, pins


def _pin_digest(value):
    # Physical pin tuples are projected into the existing JSON-only grammar;
    # the private controller canonicalizer deliberately rejects Python tuples.
    return ctl.digest(json.loads(json.dumps(value, allow_nan=False)))


def _config_pins(spec):
    """Pin config-file presence and bytes; do not pin mutable auth/cache contents."""
    import closer_codex_owned_stdio as owned

    paths = {Path(spec["codex_home"]) / "config.toml"}
    cwd = Path(spec["cwd"])
    for parent in (cwd, *cwd.parents):
        paths.add(parent / ".codex" / "config.toml")
    result = []
    for path in sorted(paths):
        _need(
            not any(p.is_symlink() for p in (path, *path.parents)),
            "config_alias_refused",
        )
        try:
            os.lstat(path)
        except FileNotFoundError:
            result.append((str(path), None))
        else:
            result.append((str(path), owned._physical(str(path))))
    return tuple(result)


def _argv(spec, command):
    import closer_codex_owned_stdio as owned
    from closer_codex_native_confinement import _TOOL_OVERRIDES

    result = [spec["binary_path"]]
    for override in owned._FIXED_OVERRIDES + _TOOL_OVERRIDES:
        result.extend(("-c", override))
    # Native CLI explicitly rejects --strict-config for `features list`.
    result.extend(
        ("features", "list")
        if command == "features"
        else ("app-server", "--strict-config", "--stdio")
    )
    return result


def _environment(spec):
    return {
        "HOME": spec["home"],
        "CODEX_HOME": spec["codex_home"],
        "TMPDIR": spec["private_root"],
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "CODEX_INTERNAL_APP_SERVER_REMOTE_CONTROL_DISABLED": "1",
    }


def _observe(command, *, spec, deadline, check, protocol=None, owned_route=None):
    """One owned native child, bounded nonblocking I/O; no shell or retries."""
    check()
    _need(time.monotonic() < deadline, "metadata_window_exhausted")
    if owned_route is not None:
        import closer_codex_owned_stdio as owned

        owned.verify_route(owned_route)
        _need(command == "features", "owned_route_observation_not_features")
        _need(
            tuple(owned_route.provider_argv[-3:]) == ("app-server", "--strict-config", "--stdio"),
            "owned_route_command_changed",
        )
        command_argv = list(owned_route.provider_argv[:-3]) + ["features", "list"]
        environment = dict(owned_route.environment)
        cwd = owned_route.cwd
    else:
        command_argv, environment, cwd = (
            _argv(spec, command),
            _environment(spec),
            spec["cwd"],
        )
    child = subprocess.Popen(
        command_argv,
        cwd=cwd,
        env=environment,
        stdin=subprocess.PIPE if protocol else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    total, output, pending = 0, bytearray(), b""
    complete = False
    try:
        with selectors.DefaultSelector() as selector:
            for pipe, name in ((child.stdout, "stdout"), (child.stderr, "stderr")):
                os.set_blocking(pipe.fileno(), False)
                selector.register(pipe, selectors.EVENT_READ, name)
            if protocol:
                os.set_blocking(child.stdin.fileno(), False)
            while True:
                check()
                remaining = deadline - time.monotonic()
                _need(remaining > 0, "metadata_window_exhausted")
                if protocol and not pending and protocol._pending is None:
                    if protocol.snapshot()["metadata_complete"]:
                        complete = True
                        break
                    pending = protocol.next_frame(now=time.monotonic())
                if pending:
                    try:
                        count = os.write(child.stdin.fileno(), pending)
                        _need(count > 0, "metadata_write_unknown")
                        pending = pending[count:]
                    except BlockingIOError:
                        pass
                events = selector.select(min(0.05, remaining))
                for key, _ in events:
                    try:
                        raw = os.read(key.fileobj.fileno(), min(8192, _MAX_BYTES - total + 1))
                    except BlockingIOError:
                        continue
                    if not raw:
                        selector.unregister(key.fileobj)
                        continue
                    total += len(raw)
                    _need(total <= _MAX_BYTES, "metadata_output_exhausted")
                    if key.data == "stdout":
                        if protocol:
                            _need(not pending, "metadata_response_before_write_complete")
                            protocol.feed(raw, now=time.monotonic())
                        else:
                            output.extend(raw)
                if child.poll() is not None and not selector.get_map():
                    _need(
                        not protocol and child.returncode == 0,
                        "metadata_child_incomplete",
                    )
                    complete = True
                    break
        _need(complete, "metadata_child_incomplete")
        return bytes(output), total
    except (OSError, ProtocolHeld):
        raise MetadataHeld("metadata_native_exchange_held") from None
    finally:
        # Only this still-live Popen child/session can be signalled. No recovery
        # of an old PID or unknown operation occurs here.
        if child.poll() is None:
            _need(os.getpgid(child.pid) == child.pid, "metadata_child_group_changed")
            os.killpg(child.pid, signal.SIGKILL)
        try:
            child.wait(timeout=2)
        except subprocess.TimeoutExpired:
            raise MetadataHeld("metadata_cleanup_unknown") from None
        for pipe in (child.stdin, child.stdout, child.stderr):
            if pipe is not None:
                pipe.close()


def qualify_existing_callback(controller, operation):
    """Fixed original queue callback + writer; no consumer registration needed.

    Returns protected observation location only. No registration is installed,
    task launch authorized, or old uncertain intent retried by this function.
    """
    spec, pins = _source_selection(controller, operation)
    with controller.fenced(operation) as custody:
        controller._require_writer(operation, custody)
        config_pins = _config_pins(spec)
        expected = ctl.digest(spec)
        key = ctl.digest(
            {
                "schema": PURPOSE,
                "operation_id": operation["id"],
                "selection_sha256": expected,
                "controller": controller.config.source_binding(),
            }
        )
        intent_name, result_name = (
            "codex-metadata-intent-" + key + ".json",
            "codex-metadata-result-" + key + ".json",
        )
        intent = {
            "schema": PURPOSE,
            "operation_id": operation["id"],
            "selection_sha256": expected,
            "source_sha256": _SOURCE_SHA256,
        }
        _need(custody.create(intent_name, intent), "metadata_prior_intent_no_retry")
        started = time.monotonic()
        wall_remaining = operation["deadline"] - time.time()
        _need(
            type(wall_remaining) in (int, float)
            and math.isfinite(wall_remaining)
            and wall_remaining > 2,
            "metadata_window_exhausted",
        )
        deadline = started + min(_MAX_SECONDS, wall_remaining - 2)

        def check():
            controller._require_writer(operation, custody)
            fresh_spec, fresh_pins = _source_selection(controller, operation)
            _need(
                fresh_spec == spec and fresh_pins == pins and _config_pins(spec) == config_pins,
                "metadata_source_selection_changed",
            )
            _need(time.monotonic() < deadline, "metadata_window_exhausted")

        def live():
            controller._require_writer(operation, custody)
            _need(time.monotonic() < deadline, "metadata_window_exhausted")

        check()
        features_raw, features_bytes = _observe(
            "features", spec=spec, deadline=deadline, check=live
        )
        features = feature_values(features_raw)
        check()
        protocol = AccountMetadataProtocol(deadline=deadline)
        _, metadata_bytes = _observe(
            "metadata", spec=spec, deadline=deadline, check=live, protocol=protocol
        )
        check()
        _need(
            protocol.snapshot()["metadata_complete"] and protocol._native_account_id is not None,
            "metadata_identity_missing",
        )
        result = {
            "schema": "closer-codex-account-metadata-result/v1",
            "operation_id": operation["id"],
            "selection_sha256": expected,
            "source_sha256": _SOURCE_SHA256,
            "configuration_sha256": _pin_digest(config_pins),
            "profile_id": spec["profile_id"],
            "account_sha256": ctl.digest(
                {
                    "schema": "managed-native-account/v1",
                    "account_id": protocol._native_account_id,
                }
            ),
            "features_sha256": hashlib.sha256(features_raw).hexdigest(),
            "plugins_observed_disabled": features["plugins"] is False,
            "counted_features_bytes": features_bytes,
            "counted_metadata_bytes": metadata_bytes,
            "thread_starts": 0,
            "turn_starts": 0,
            "registration_installed": False,
            "execution_authorized": False,
            "cloud_policy_lease": False,
        }
        _need(custody.create(result_name, result), "metadata_result_already_exists")
        return {
            "name": result_name,
            "sha256": ctl.digest(result),
            "execution_authorized": False,
        }


def preflight_owned_route(route):
    """Fresh preinitialize observation on the original coding callback/thread.

    Uses exactly the selected appserver configuration prefix/environment/cwd.
    This consumes the same request deadline; it creates no coding turn permit.
    """
    import closer_codex_owned_stdio as owned

    caller = inspect.currentframe().f_back
    _need(
        caller.f_code is owned.prepare.__code__
        and caller.f_globals is owned.__dict__
        and caller.f_locals.get("route") is route,
        "original_owned_route_preparation_required",
    )
    owned.verify_route(route)
    controller, operation, custody = (
        route.guard.controller,
        route.guard.operation,
        route.guard.custody,
    )
    controller._require_writer(operation, custody)
    _need(
        controller.installation.source_closure.get(Path(__file__).name)
        == {"path": str(Path(__file__)), "sha256": _SOURCE_SHA256},
        "metadata_source_not_installed",
    )
    owned._physical(str(Path(__file__)), expected=_SOURCE_SHA256)
    spec = controller.config.consumers[route.guard.consumer_id]["native_bootstrap"]
    spec = {**spec, "cwd": route.cwd}
    config_pins = _config_pins(spec)
    selection = ctl.digest(
        {
            "argv": list(route.provider_argv),
            "environment": [list(pair) for pair in route.environment],
            "cwd": route.cwd,
        }
    )
    key = ctl.digest(
        {
            "schema": "closer-codex-feature-preflight/v1",
            "operation_id": operation["id"],
            "selection_sha256": selection,
        }
    )
    intent = {
        "schema": "closer-codex-feature-preflight/v1",
        "operation_id": operation["id"],
        "selection_sha256": selection,
        "source_sha256": _SOURCE_SHA256,
    }
    _need(
        custody.create("codex-features-intent-" + key + ".json", intent),
        "feature_prior_intent_no_retry",
    )
    remaining = operation["deadline"] - time.time()
    _need(
        type(remaining) in (int, float) and math.isfinite(remaining) and remaining > 2,
        "metadata_window_exhausted",
    )
    deadline = time.monotonic() + min(_MAX_SECONDS, remaining - 2)

    def live():
        controller._require_writer(operation, custody)
        _need(
            threading.get_ident() == route.thread and time.monotonic() < deadline,
            "original_metadata_thread_or_window_changed",
        )

    raw, total = _observe("features", spec=spec, deadline=deadline, check=live, owned_route=route)
    features = feature_values(raw)
    live()
    owned.verify_route(route)
    _need(_config_pins(spec) == config_pins, "metadata_source_selection_changed")
    result = {
        **intent,
        "features_sha256": hashlib.sha256(raw).hexdigest(),
        "counted_output_bytes": total,
        "plugins_observed_disabled": features["plugins"] is False,
        "cloud_policy_lease": False,
        "execution_authorized": False,
    }
    _need(
        custody.create("codex-features-result-" + key + ".json", result),
        "feature_result_already_exists",
    )


_QUALIFICATIONS: dict[int, tuple] = {}


class AccountQualification:
    """Protected-read source handle, valid only in the current writer callback."""

    def __init__(self):
        raise MetadataHeld("protected_metadata_reader_required")


def read_protected_account(controller, operation, custody, reference):
    """Issue no account/root assumption from caller JSON; reread native result."""
    _need(type(controller) is ctl.IndexController, "source_controller_required")
    controller._require_writer(operation, custody)
    spec, _ = _source_spec(controller)
    _need(
        type(reference) is dict
        and set(reference) == {"name", "sha256", "execution_authorized"}
        and reference["execution_authorized"] is False
        and type(reference["name"]) is str
        and re.fullmatch(r"codex-metadata-result-[0-9a-f]{64}\.json", reference["name"])
        and type(reference["sha256"]) is str
        and re.fullmatch(r"[0-9a-f]{64}", reference["sha256"]),
        "protected_metadata_reference_invalid",
    )
    record = custody.read(reference["name"])
    fields = {
        "schema",
        "operation_id",
        "selection_sha256",
        "source_sha256",
        "configuration_sha256",
        "profile_id",
        "account_sha256",
        "features_sha256",
        "plugins_observed_disabled",
        "counted_features_bytes",
        "counted_metadata_bytes",
        "thread_starts",
        "turn_starts",
        "registration_installed",
        "execution_authorized",
        "cloud_policy_lease",
    }
    _need(
        type(record) is dict
        and set(record) == fields
        and record["schema"] == "closer-codex-account-metadata-result/v1"
        and ctl.digest(record) == reference["sha256"]
        and record["selection_sha256"] == ctl.digest(spec)
        and record["source_sha256"] == _SOURCE_SHA256
        and record["configuration_sha256"] == _pin_digest(_config_pins(spec))
        and record["profile_id"] == spec["profile_id"]
        and record["plugins_observed_disabled"] is True
        and type(record["thread_starts"]) is type(record["turn_starts"]) is int
        and record["thread_starts"] == record["turn_starts"] == 0
        and record["registration_installed"]
        is record["execution_authorized"]
        is record["cloud_policy_lease"]
        is False,
        "protected_metadata_record_changed",
    )
    for key in ("account_sha256", "features_sha256"):
        _need(
            type(record[key]) is str and re.fullmatch(r"[0-9a-f]{64}", record[key]),
            "protected_metadata_digest_invalid",
        )
    for key in ("counted_features_bytes", "counted_metadata_bytes"):
        _need(
            type(record[key]) is int and 0 < record[key] <= _MAX_BYTES,
            "protected_metadata_count_invalid",
        )
    suffix = reference["name"].removeprefix("codex-metadata-result-").removesuffix(".json")
    intent = custody.read("codex-metadata-intent-" + suffix + ".json")
    _need(
        intent
        == {
            "schema": PURPOSE,
            "operation_id": record["operation_id"],
            "selection_sha256": ctl.digest(spec),
            "source_sha256": _SOURCE_SHA256,
        },
        "protected_metadata_intent_changed",
    )
    expected_key = ctl.digest(
        {
            "schema": PURPOSE,
            "operation_id": record["operation_id"],
            "selection_sha256": ctl.digest(spec),
            "controller": controller.config.source_binding(),
        }
    )
    _need(suffix == expected_key, "protected_metadata_transaction_changed")
    value = object.__new__(AccountQualification)
    _QUALIFICATIONS[id(value)] = (
        value,
        controller,
        operation,
        custody,
        dict(reference),
        dict(spec),
        record["account_sha256"],
        threading.get_ident(),
    )
    return value


def selected_account_sha256(value, *, native_bootstrap, profile_id, cwd):
    """Necessary initial-registration identity only; no launch permit is issued."""
    held = _QUALIFICATIONS.get(id(value))
    _need(
        type(value) is AccountQualification and held is not None and held[0] is value,
        "protected_metadata_reader_required",
    )
    _, controller, operation, custody, reference, spec, digest, thread = held
    _need(thread == threading.get_ident(), "original_metadata_reader_thread_changed")
    controller._require_writer(operation, custody)
    _need(
        type(native_bootstrap) is dict
        and profile_id == spec["profile_id"]
        and cwd == spec["cwd"]
        and all(
            native_bootstrap.get(key) == spec[key]
            for key in (
                "binary_path",
                "binary_sha256",
                "home",
                "codex_home",
                "private_root",
            )
        ),
        "initial_consumer_selection_differs_from_metadata",
    )
    # Re-read the exact protected record and source/config bindings at consumption.
    fresh = read_protected_account(controller, operation, custody, reference)
    fresh_held = _QUALIFICATIONS.pop(id(fresh))
    _need(fresh_held[6] == digest, "protected_metadata_account_changed")
    _QUALIFICATIONS.pop(id(value))
    return digest
