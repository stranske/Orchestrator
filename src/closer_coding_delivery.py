"""Live source observation of one existing coding transport, never a launch permit.

The shell and provider argv are observed separately; no shell parsing or caller
observation JSON establishes prompt custody. No account, confinement, or native
execution-limit qualification is supplied here.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from types import FrameType

# Source provenance only; this Python global is not an OS security boundary.
_LOADED_SOURCE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
SELECTED_PROFILE = "codex-6.1-sol-high"


@dataclass(frozen=True)
class CodingDelivery:
    prompt_utf8: bytes
    provider_argv: tuple[str, ...]
    transport_argv: tuple[str, ...]
    cwd: str
    profile_json: str
    # Existing coding shell deliberately inherits its environment. This does
    # not assert that any effective environment is isolated or credential-free.
    env: tuple[tuple[str, str], ...] | None = None
    channel: str = "codex-final-argv/v1"
    stdin: int = subprocess.DEVNULL


def _capture(frame: FrameType) -> CodingDelivery | None:
    import dispatcher
    import execution_profiles

    if (
        frame.f_code is not dispatcher._CODING_DELIVERY_RUN_CODE
        or frame.f_globals is not dispatcher.__dict__
    ):
        return None
    attempt = frame.f_back
    if (
        attempt is None
        or attempt.f_code is not dispatcher._CODING_DELIVERY_ATTEMPT_CODE
        or attempt.f_globals is not dispatcher.__dict__
    ):
        return None
    origin = attempt.f_back
    if (
        origin is None
        or origin.f_code is not dispatcher._CODING_DELIVERY_OFFLOAD_CODE
        or origin.f_globals is not dispatcher.__dict__
    ):
        return None
    state, run, actual = origin.f_locals, attempt.f_locals, frame.f_locals
    if (
        state.get("agent") != "codex"
        or state.get("effective_profile_id") != SELECTED_PROFILE
        or state.get("data_only") is not False
        or state.get("controlled") is not None
        or state.get("prompt_via_stdin") is not False
        or state.get("stream_prompt") is not None
        or run.get("stream_path") is not None
        or run.get("stream_identity") is not None
        or actual.get("stdin") != subprocess.DEVNULL
        or actual.get("starting_callback") is not state.get("process_starting_callback")
        or actual.get("callback") is not state.get("process_started_callback")
    ):
        return None
    route = state.get("coding_route")
    if route is not None:
        import closer_coding_route_v2

        try:
            route = closer_coding_route_v2.capture(frame)
        except (ValueError, RuntimeError, OSError):
            return None
        transport = list(route.transport_argv)
        environment = route.environment
        channel = closer_coding_route_v2.CHANNEL
    else:
        if actual.get("env") is not None:
            return None
        transport = ["bash", "-lc", state.get("wrapped")]
        environment, channel = None, "codex-final-argv/v1"
    profile = state.get("profile")
    if profile != execution_profiles.get_profile(SELECTED_PROFILE):
        return None
    prompt, argv = state.get("prepared_prompt"), state.get("argv")
    wrapped = state.get("wrapped")
    if (
        type(prompt) is not str
        or type(argv) is not list
        or not argv
        or any(type(part) is not str for part in argv)
        or argv[-1] != prompt
        or state.get("command_prompt") != prompt
        or type(wrapped) is not str
        or run.get("attempt_wrapped") != wrapped
        or actual.get("command") != transport
        or actual.get("cwd") != str(state.get("proc_cwd"))
    ):
        return None
    prepared = state.get("_coding_prepared")
    expected = (
        prompt.encode("utf-8"),
        tuple(argv),
        wrapped,
        actual["cwd"],
        json.dumps(profile, sort_keys=True, separators=(",", ":")),
    )
    if type(prepared) is not tuple or prepared != expected:
        return None
    return CodingDelivery(
        prompt.encode("utf-8"),
        tuple(argv),
        tuple(actual["command"]),
        actual["cwd"],
        json.dumps(profile, sort_keys=True, separators=(",", ":")),
        env=environment,
        channel=channel,
    )


# Entries exist only across the original callback / Popen boundary. The frame
# itself and current thread form the key; no serialized record can be replayed.
_ACTIVE: dict[tuple[int, FrameType], CodingDelivery | None] = {}


def _begin(frame: FrameType) -> None:
    current = inspect.currentframe()
    caller = current.f_back if current is not None else None
    del current  # Do not retain an inspection frame's reference to itself.
    if caller is None or caller is not frame:
        raise ValueError("original coding transport frame required")
    key = (threading.get_ident(), frame)
    if key in _ACTIVE:
        raise ValueError("coding delivery observation already active")
    _ACTIVE[key] = _capture(frame)


def _verify(frame: FrameType) -> None:
    current = inspect.currentframe()
    caller = current.f_back if current is not None else None
    del current  # Do not retain an inspection frame's reference to itself.
    if caller is None or caller is not frame:
        raise ValueError("original coding transport frame required")
    key = (threading.get_ident(), frame)
    if key not in _ACTIVE:
        raise ValueError("source coding delivery observation missing or changed")
    held = _ACTIVE[key]
    if held is not None and _capture(frame) != held:
        raise ValueError("source coding delivery changed during before-Popen checks")


def _revoke(frame: FrameType) -> None:
    current = inspect.currentframe()
    caller = current.f_back if current is not None else None
    del current  # Do not retain an inspection frame's reference to itself.
    if caller is None or caller is not frame:
        raise ValueError("original coding transport frame required")
    _ACTIVE.pop((threading.get_ident(), frame), None)


def require_coding_delivery(frame: FrameType) -> CodingDelivery:
    """Read only the currently executing original before-Popen callback frame.

    A saved/copied delivery or a frame supplied from another call is no authority.
    Consumers must retain the callback frame and call this again after their own
    final checks. The dispatcher also rechecks immediately before Popen.
    """
    current = inspect.currentframe()
    caller = current.f_back if current is not None else None
    del current  # Do not retain an inspection frame's reference to itself.
    if caller is None or caller.f_back is not frame:
        raise ValueError("live original before-Popen coding callback required")
    held = _ACTIVE.get((threading.get_ident(), frame))
    if type(held) is not CodingDelivery or _capture(frame) != held:
        raise ValueError("exact source coding prompt delivery unavailable or changed")
    return held
