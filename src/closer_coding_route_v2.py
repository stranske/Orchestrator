"""Prospective typed-only route custody; the actual installed facts port HOLDS.

No serialized object, account identity alone, or caller config grants a route.
This module does not attest billing credits, top-up state, or child confinement.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import shlex
import threading
from dataclasses import dataclass
from pathlib import Path

_LOADED_SOURCE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
CHANNEL = "codex-source-route-final-argv/v2"
BASH = "/bin/bash"
_BASE_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"


class RouteHeld(RuntimeError):
    pass


@dataclass(frozen=True)
class _InstalledFacts:
    """Private source port values, not a supported installed attestation yet."""

    binary_path: str
    binary_sha256: str
    bash_sha256: str
    gh_path: str
    gh_sha256: str
    home: str
    codex_home: str
    private_root: str
    provider: str
    authentication: str
    effective_config_utf8: bytes
    account_sha256: str


@dataclass(frozen=True)
class _RouteSeal:
    guard: object
    origin: object
    facts: _InstalledFacts
    profile_utf8: bytes
    provider_argv: tuple[str, ...]
    transport_argv: tuple[str, ...]
    environment: tuple[tuple[str, str], ...]
    cwd: str
    files: tuple[tuple[str, str], ...]
    thread: int


_PREPARED = {}


def _read_installed_route_facts(guard):
    # No current supported source reader proves effective layered config/auth
    # selection, exact bootstrap inputs and account route. Do not consult owner
    # credentials/config/history or turn source JSON into a positive substitute.
    raise RouteHeld("installed effective coding route evidence unavailable")


def _need(value):
    if not value:
        raise RouteHeld("source coding route unavailable or changed")


def _physical(path, expected=None):
    _need(type(path) is str)
    p = Path(path)
    _need(p.is_absolute() and p.resolve(strict=True) == p)
    _need(not any(part.is_symlink() for part in (p, *p.parents)))
    if expected is not None:
        _need(p.is_file() and hashlib.sha256(p.read_bytes()).hexdigest() == expected)
    else:
        _need(p.is_dir())
    return p


def _validate(facts, guard, argv, profile):
    import closer_coding_session_v2 as contract
    import execution_profiles

    _need(type(facts) is _InstalledFacts)
    _need(profile == execution_profiles.get_profile(contract.PROFILE))
    _need(type(argv) is list and argv and argv[0] == facts.binary_path)
    _physical(facts.binary_path, facts.binary_sha256)
    _physical(BASH, facts.bash_sha256)
    _physical(facts.gh_path, facts.gh_sha256)
    for path in (facts.home, facts.codex_home, facts.private_root):
        _physical(path)
    _need(facts.provider == "openai" and facts.authentication == "chatgpt")
    _need(
        facts.account_sha256
        == guard.controller.config.consumers[guard.consumer_id]["account_sha256"]
    )
    _need(type(facts.effective_config_utf8) is bytes)
    try:
        config = json.loads(facts.effective_config_utf8)
    except (ValueError, UnicodeError):
        raise RouteHeld("effective coding configuration evidence invalid") from None
    # This is a prospective exact private source observation contract. It is not
    # a config.toml parser or a claim that this evidence facility is installed.
    _need(
        config
        == {
            "provider": "openai",
            "authentication": "chatgpt",
            "openai_base_url": None,
            "chatgpt_base_url": None,
            "custom_provider": None,
            "external_auth": None,
            "profile_id": contract.PROFILE,
        }
    )


def prepare(frame, argv, cwd, profile):
    """Only canonical typed preparation may mint the opaque ephemeral seal."""
    import closer_coding_session_runtime as runtime
    import dispatcher

    if not runtime.history_restricted(frame):
        return None
    _need(inspect.currentframe().f_back is frame)
    _need(
        frame.f_code is dispatcher._CODING_DELIVERY_OFFLOAD_CODE
        and frame.f_globals is dispatcher.__dict__
    )
    key = (threading.get_ident(), frame)
    guard = runtime._HISTORY_RESTRICTIONS.get(key)
    _need(guard is not None and guard.active and key not in _PREPARED)
    _need(frame.f_locals.get("agent") == "codex" and frame.f_locals.get("data_only") is False)
    facts = _read_installed_route_facts(guard)
    _validate(facts, guard, argv, profile)
    _physical(cwd)
    # Explicitly select the supported built-in provider. This supported config
    # override alone is NOT effective-config/auth/account-route proof.
    provider_argv = tuple(argv[:-1] + ["-c", 'model_provider="openai"', argv[-1]])
    root = Path(facts.private_root)
    proxy = root / "coding-route-gh"
    proxy.mkdir(mode=0o700, exist_ok=True)
    _physical(str(proxy))
    wrapper = (
        "#!/bin/sh\n"
        + dispatcher._gh_guard_check("exit")
        + "exec "
        + shlex.quote(facts.gh_path)
        + ' "$@"\n'
    )
    gh = proxy / "gh"
    # Never chain an inherited BASH_ENV or source owner startup files.
    bashenv = root / "coding-route-bashenv.sh"
    function = (
        "gh() {\n"
        + dispatcher._gh_guard_check("return")
        + "command "
        + shlex.quote(facts.gh_path)
        + ' "$@"\n}\nexport -f gh\n'
    )
    for path, text, mode in ((gh, wrapper, 0o700), (bashenv, function, 0o600)):
        _need(not path.is_symlink() and (not path.exists() or path.is_file()))
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
        os.chmod(path, mode)
    files = tuple(
        (str(path), hashlib.sha256(path.read_bytes()).hexdigest()) for path in (gh, bashenv)
    )
    environment = tuple(
        sorted(
            {
                "HOME": facts.home,
                "CODEX_HOME": facts.codex_home,
                "TMPDIR": facts.private_root,
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "PATH": str(proxy) + ":" + _BASE_PATH,
                "BASH_ENV": str(bashenv),
            }.items()
        )
    )
    script = "set -e\nexec " + shlex.join(provider_argv)
    seal = _RouteSeal(
        guard,
        frame,
        facts,
        json.dumps(profile, sort_keys=True, separators=(",", ":")).encode(),
        provider_argv,
        (BASH, "--noprofile", "--norc", "-c", script),
        environment,
        cwd,
        files,
        threading.get_ident(),
    )
    _PREPARED[key] = seal
    return seal


def _verify(seal):
    _need(type(seal) is _RouteSeal and seal.thread == threading.get_ident())
    _need(_PREPARED.get((seal.thread, seal.origin)) is seal and seal.guard.active)
    _need(_read_installed_route_facts(seal.guard) == seal.facts)
    _validate(seal.facts, seal.guard, list(seal.provider_argv), json.loads(seal.profile_utf8))
    for path, digest in seal.files:
        _physical(path, digest)


def capture(frame):
    """Return binding only for the actual original run/attempt/offload frames."""
    import dispatcher

    attempt = frame.f_back
    origin = attempt.f_back if attempt else None
    _need(
        frame.f_code is dispatcher._CODING_DELIVERY_RUN_CODE
        and frame.f_globals is dispatcher.__dict__
    )
    _need(
        attempt is not None
        and attempt.f_code is dispatcher._CODING_DELIVERY_ATTEMPT_CODE
        and attempt.f_globals is dispatcher.__dict__
    )
    _need(
        origin is not None
        and origin.f_code is dispatcher._CODING_DELIVERY_OFFLOAD_CODE
        and origin.f_globals is dispatcher.__dict__
    )
    seal = _PREPARED.get((threading.get_ident(), origin))
    _verify(seal)
    _need(origin.f_locals.get("coding_route") is seal)
    _need(tuple(origin.f_locals.get("argv", ())) == seal.provider_argv)
    _need(frame.f_locals.get("command") == list(seal.transport_argv))
    _need(
        frame.f_locals.get("env") == dict(seal.environment)
        and frame.f_locals.get("cwd") == seal.cwd
    )
    return seal


def release_guard(guard):
    import closer_coding_session_runtime as runtime

    caller = inspect.currentframe().f_back
    _need(caller.f_code is runtime.release_guard.__code__ and caller.f_globals is runtime.__dict__)
    for key, seal in tuple(_PREPARED.items()):
        if seal.guard is guard:
            _PREPARED.pop(key, None)
