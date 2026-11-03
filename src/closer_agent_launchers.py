"""Concrete Closer agent CLI bindings (inventory, pin verification, guarded CLI run).

CLI execution uses the existing ``dispatcher.offload`` path with registered immutable
execution profiles and ``adapters.build_command`` — not a second agent lifecycle.
Desktop app scheduling and native handoff rounds are classified separately; an installed
CLI does not qualify desktop automation.
"""

from __future__ import annotations

import argparse
import json
import shlex
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import adapters
import execution_profiles
from execution_profiles import PROFILE_RETIREMENTS

APPROVED_AGENTS: tuple[str, ...] = (
    "codex",
    "claude",
    "gemini",
    "muse",
    "cursor",
    "vibe",
)
EXCLUDED_AGENTS: frozenset[str] = frozenset({"aider"})

MECHANISM_CLI_OFFLOAD = "cli_offload"
MECHANISM_DESKTOP_NATIVE_HANDOFF = "desktop_native_handoff"
MECHANISM_DESKTOP_APP_SCHEDULER = "desktop_app_scheduler"

NATIVE_HANDOFF_ACTORS: frozenset[str] = frozenset(
    {"codex", "claude_code", "cursor", "gemini", "muse"}
)

CLI_PRESENCE_PROBE: dict[str, list[str]] = {
    "codex": ["codex", "--version"],
    "claude": ["claude", "--version"],
    "gemini": ["agy", "--version"],
    "cursor": ["cursor-agent", "--version"],
    "vibe": ["vibe", "--version"],
    "muse": ["muse", "--version"],
}

DESKTOP_NATIVE_LIMITATION = (
    "PinnedNativeHooks requires HANDOFF_EXACT_TOKEN_V1 helpers and an allowlisted "
    "handoff_agent; installed postrun lacks token-safe cleanup. CLI offload is a "
    "separate supported path and is not evidence of desktop scheduling."
)
DESKTOP_SCHEDULER_UNSUPPORTED = (
    "No supported desktop application scheduler adapter is installed for this runtime."
)

# Public host binding convention (``closer_distribution_package.host_binding_from_public``).
HOST_LAUNCHER_ENTRY = "run"


@dataclass(frozen=True)
class AgentBinding:
    agent: str
    cli_mechanism: str
    registered_profile_ids: tuple[str, ...]
    native_handoff_actor: str | None
    native_handoff_actor_known: bool
    desktop_native_installed: bool
    desktop_native_qualified: bool
    desktop_native_supported: bool
    desktop_scheduler_supported: bool
    notes: tuple[str, ...] = ()


@dataclass
class Readiness:
    agent: str
    profile_id: str
    ready: bool
    cli_installed: bool
    cli_probe: str | None
    profile_valid: bool
    desktop_native_qualified: bool
    reasons: list[str] = field(default_factory=list)


@dataclass
class LaunchPlan:
    agent: str
    profile_id: str
    mechanism: str
    mode: str
    argv_preview: list[str]
    manifest_checks: list[str]
    launcher_argv: list[str]
    bound_request_prompt: str
    delegate_prompt_hint: str | None
    manifest_fingerprint: str
    warnings: list[str] = field(default_factory=list)


def _normalize_agent(agent: str) -> str:
    return str(agent or "").strip().casefold()


def native_handoff_actor(agent: str) -> str | None:
    key = _normalize_agent(agent)
    if key == "claude":
        return "claude_code"
    if key in {"codex", "cursor", "gemini", "muse"}:
        return key
    if key == "vibe":
        return None
    return None


def refuse_agent(agent: str) -> str | None:
    key = _normalize_agent(agent)
    if not key:
        return "missing agent"
    if key in EXCLUDED_AGENTS:
        return f"agent {key!r} is excluded from the approved closer pool"
    if key not in APPROVED_AGENTS:
        return f"unknown agent {key!r}"
    return None


def offload_profiles(agent: str) -> list[dict[str, Any]]:
    reason = refuse_agent(agent)
    if reason:
        return []
    return execution_profiles.profiles_for_agent(agent, transport="offload")


def profile_offload_mode(profile: Mapping[str, Any]) -> str:
    agent = _normalize_agent(profile["agent"])
    if agent == "cursor":
        return "composer"
    effort = profile.get("reasoning_effort")
    if isinstance(effort, str) and effort:
        return effort
    return adapters.DEFAULT_OFFLOAD_TIER


def validate_profile(agent: str, profile_id: str) -> list[str]:
    problems: list[str] = []
    agent_reason = refuse_agent(agent)
    if agent_reason:
        problems.append(agent_reason)
        return problems
    if profile_id in PROFILE_RETIREMENTS:
        problems.append(f"profile {profile_id!r} is retired")
        return problems
    key = _normalize_agent(agent)
    try:
        profile = execution_profiles.get_profile(profile_id)
    except ValueError as exc:
        problems.append(str(exc))
        return problems
    if _normalize_agent(profile["agent"]) != key:
        problems.append(f"profile {profile_id!r} belongs to {profile['agent']!r}, not {agent!r}")
    if profile.get("lifecycle_status") != "active":
        problems.append(f"profile {profile_id!r} is not active")
    if "offload" not in profile.get("transport_support", []):
        problems.append(f"profile {profile_id!r} does not support offload transport")
    return problems


def _desktop_native_probe(
    agent: str,
    *,
    probe: Callable[[str, str | None], tuple[bool, bool]] | None = None,
) -> tuple[bool, bool, bool]:
    """Return (actor_known, installed, qualified). Defaults: known from allowlist only."""
    actor = native_handoff_actor(agent)
    known = bool(actor and actor in NATIVE_HANDOFF_ACTORS)
    if not known or probe is None:
        return known, False, False
    try:
        installed, qualified = probe(agent, actor)
    except Exception:
        return known, False, False
    return known, bool(installed), bool(qualified)


def binding_for(
    agent: str,
    *,
    desktop_probe: Callable[[str, str | None], tuple[bool, bool]] | None = None,
) -> AgentBinding | None:
    reason = refuse_agent(agent)
    if reason:
        return None
    key = _normalize_agent(agent)
    profiles = tuple(p["profile_id"] for p in offload_profiles(key))
    actor = native_handoff_actor(key)
    known, installed, qualified = _desktop_native_probe(key, probe=desktop_probe)
    notes: list[str] = []
    if key == "vibe":
        notes.append("vibe is not in PinnedNativeHooks handoff_agent allowlist")
    if not qualified:
        notes.append(DESKTOP_NATIVE_LIMITATION)
    return AgentBinding(
        agent=key,
        cli_mechanism=MECHANISM_CLI_OFFLOAD,
        registered_profile_ids=profiles,
        native_handoff_actor=actor,
        native_handoff_actor_known=known,
        desktop_native_installed=installed,
        desktop_native_qualified=qualified,
        desktop_native_supported=qualified,
        desktop_scheduler_supported=False,
        notes=tuple(notes),
    )


def inventory(
    *,
    desktop_probe: Callable[[str, str | None], tuple[bool, bool]] | None = None,
) -> dict[str, Any]:
    bindings = []
    for agent in APPROVED_AGENTS:
        row = binding_for(agent, desktop_probe=desktop_probe)
        if row:
            bindings.append(asdict(row))
    return {
        "approved_agents": list(APPROVED_AGENTS),
        "excluded_agents": sorted(EXCLUDED_AGENTS),
        "mechanisms": {
            "cli": MECHANISM_CLI_OFFLOAD,
            "desktop_native": MECHANISM_DESKTOP_NATIVE_HANDOFF,
            "desktop_scheduler": MECHANISM_DESKTOP_APP_SCHEDULER,
        },
        "bindings": bindings,
        "desktop_scheduler": DESKTOP_SCHEDULER_UNSUPPORTED,
    }


def _probe_cli(agent: str, *, runner: Callable[..., Any] | None = None) -> tuple[bool, str | None]:
    key = _normalize_agent(agent)
    probe = CLI_PRESENCE_PROBE.get(key)
    if not probe:
        return False, f"no readonly CLI probe registered for {key}"
    binary = probe[0]
    if not shutil.which(binary):
        return False, f"CLI binary not on PATH: {binary}"
    run = runner or subprocess.run
    try:
        proc = run(
            probe,
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"CLI probe failed: {exc}"
    if proc.returncode != 0:
        fallback = [binary, "--help"]
        try:
            proc = run(fallback, capture_output=True, text=True, timeout=8, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return False, f"CLI probe failed: {exc}"
        if proc.returncode != 0:
            return False, f"CLI {binary} rejected --version and --help (exit {proc.returncode})"
    return True, None


def readiness(
    agent: str,
    profile_id: str,
    *,
    runner: Callable[..., Any] | None = None,
    desktop_probe: Callable[[str, str | None], tuple[bool, bool]] | None = None,
) -> Readiness:
    problems = validate_profile(agent, profile_id)
    installed, probe_detail = _probe_cli(agent, runner=runner)
    _, _, desktop_qualified = _desktop_native_probe(agent, probe=desktop_probe)
    reasons = list(problems)
    if not installed and probe_detail:
        reasons.append(probe_detail)
    return Readiness(
        agent=_normalize_agent(agent),
        profile_id=profile_id,
        ready=not problems and installed,
        cli_installed=installed,
        cli_probe=probe_detail,
        profile_valid=not problems,
        desktop_native_qualified=desktop_qualified,
        reasons=reasons,
    )


def expected_host_launcher_command(
    manifest: Mapping[str, Any],
    *,
    manifest_path: str | Path,
    root: str | Path,
) -> list[str]:
    """Pinned argv for ``closer_launcher.py run`` per host binding convention."""
    runtime = manifest.get("runtime") or {}
    python = runtime.get("python")
    launcher = manifest.get("launcher") or {}
    launcher_path = launcher.get("path")
    if not isinstance(python, str) or not python:
        raise ValueError("manifest runtime.python missing")
    if not isinstance(launcher_path, str) or not launcher_path:
        raise ValueError("manifest launcher.path missing")
    mp = Path(manifest_path).expanduser().resolve()
    rt = Path(root).expanduser().resolve()
    command = [
        python,
        str(Path(launcher_path).expanduser().resolve()),
        HOST_LAUNCHER_ENTRY,
        "--manifest",
        str(mp),
        "--root",
        str(rt),
    ]
    from closer_mechanisms import mechanism

    selected = mechanism(dict(manifest))
    if selected != "desktop-scheduler/v1":
        command.extend(["--mechanism", selected])
    return command


def _legacy_direct_tick_command(manifest: Mapping[str, Any]) -> list[str]:
    command = manifest.get("command")
    return list(command) if isinstance(command, list) else []


def validate_launcher_argv(
    argv: Sequence[str],
    manifest: Mapping[str, Any],
    *,
    manifest_path: str | Path,
    root: str | Path,
) -> list[str]:
    problems: list[str] = []
    if isinstance(argv, (str, bytes)) or not all(isinstance(x, str) for x in argv):
        return ["launcher argv must contain only strings"]
    argv_list = list(argv)
    try:
        expected = expected_host_launcher_command(manifest, manifest_path=manifest_path, root=root)
    except ValueError as exc:
        problems.append(str(exc))
        return problems
    if argv_list != expected:
        problems.append("launcher argv does not match pinned host runtime command")
    legacy = _legacy_direct_tick_command(manifest)
    if legacy and legacy != expected and argv_list == legacy:
        problems.append(
            "legacy closer_campaign.py tick command is not the shared launcher host binding"
        )
    if len(argv_list) != len(expected):
        problems.append("launcher argv length mismatch")
    for forbidden in ("-c", "-m"):
        if forbidden in argv_list[1:3]:
            problems.append(f"launcher argv forbids interpreter flag {forbidden}")
    pinned_cmd = manifest.get("command")
    if isinstance(pinned_cmd, list) and pinned_cmd != expected:
        problems.append("manifest command field does not match host launcher binding")
    return problems


def load_manifest_file(path: str | Path) -> tuple[dict[str, Any], list[str]]:
    import closer_campaign as campaign

    mp = Path(path).expanduser()
    if not mp.is_file():
        return {}, [f"manifest path is not a readable file: {mp}"]
    try:
        return dict(campaign.load_manifest(mp)), []
    except Exception as exc:
        return {}, [f"manifest load failed: {exc}"]


def _manifest_consistency(supplied: Mapping[str, Any], loaded: Mapping[str, Any]) -> list[str]:
    if dict(supplied) != dict(loaded):
        return ["supplied manifest differs from file at manifest path"]
    return []


def verify_runtime_manifest(
    manifest: Mapping[str, Any],
    *,
    manifest_path: str | Path,
    root: str | Path,
    launcher_argv: Sequence[str] | None = None,
    own_launcher_path: str | Path | None = None,
) -> list[str]:
    """Pin/manifest/command checks before any provider or launcher effect."""
    import closer_launcher

    problems: list[str] = []
    loaded, load_problems = load_manifest_file(manifest_path)
    problems.extend(load_problems)
    if load_problems:
        return problems
    problems.extend(_manifest_consistency(manifest, loaded))
    effective = loaded
    own = str(own_launcher_path or Path(__file__).with_name("closer_launcher.py"))
    problems.extend(closer_launcher.pin_problems(dict(effective), own_path=own))
    import closer_campaign as campaign

    problems.extend(campaign.verify_modules(dict(effective)))
    command = list(effective.get("command") or [])
    from closer_campaign import sha256_json

    if command and effective.get("command_digest") != sha256_json(command):
        problems.append("command digest does not match command")
    if effective.get("state_digest") != sha256_json(effective.get("state_versions")):
        problems.append("state digest does not match state versions")
    if effective.get("runtime_digest") != sha256_json(effective.get("runtime")):
        problems.append("runtime digest does not match runtime")
    try:
        expected = expected_host_launcher_command(effective, manifest_path=manifest_path, root=root)
    except ValueError as exc:
        problems.append(str(exc))
        expected = []
    if expected and command and command != expected:
        problems.append("manifest command is not the host launcher binding")
    argv = list(launcher_argv) if launcher_argv is not None else expected
    if argv:
        problems.extend(
            validate_launcher_argv(argv, effective, manifest_path=manifest_path, root=root)
        )
    return problems


def preflight_runtime(
    manifest_path: str | Path,
    root: str | Path,
    *,
    manifest: Mapping[str, Any] | None = None,
    launcher_argv: Sequence[str] | None = None,
    own_launcher_path: str | Path | None = None,
    qualify_fn: Callable[[dict[str, Any], str | Path], list[str]] | None = None,
    policy_fn: Callable[[dict[str, Any]], str | None] | None = None,
    native_probe: Callable[[dict[str, Any]], str | None] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Policy, qualify, and native barriers after pin/manifest checks."""
    loaded, problems = load_manifest_file(manifest_path)
    if problems:
        return {}, problems
    if manifest is not None:
        problems.extend(_manifest_consistency(manifest, loaded))
    problems.extend(
        verify_runtime_manifest(
            loaded,
            manifest_path=manifest_path,
            root=root,
            launcher_argv=launcher_argv,
            own_launcher_path=own_launcher_path,
        )
    )
    if problems:
        return loaded, problems
    import closer_campaign as campaign
    from closer_mechanisms import mechanism
    from closer_runtime_binding import policy_problem

    try:
        selected_mechanism = mechanism(loaded)
        hold = (
            policy_fn(loaded.get("policy") or {})
            if policy_fn
            else policy_problem(loaded.get("policy") or {}, mechanism=selected_mechanism)
        )
    except Exception as exc:
        hold = f"policy check failed: {exc}"
    if hold:
        problems.append(f"runtime policy held: {hold}")
        return loaded, problems
    qualifier = qualify_fn or campaign.qualify
    try:
        unqualified = qualifier(loaded, root)
    except Exception as exc:
        unqualified = [f"qualify failed: {exc}"]
    if unqualified:
        problems.append("runtime not qualified")
        problems.extend(unqualified)
        return loaded, problems
    probe = native_probe or campaign.native_barrier
    try:
        barrier = probe(
            {
                "worker_config": loaded.get("worker_config") or {},
                "targets": {},
                "workers": [],
            }
        )
    except Exception as exc:
        barrier = f"native barrier check failed: {exc}"
    if barrier:
        problems.append(f"native barrier: {barrier}")
    return loaded, problems


def build_bound_request_prompt(user_prompt: str, launcher_argv: Sequence[str]) -> str:
    bound = " ".join(shlex.quote(x) for x in launcher_argv)
    return (
        f"{user_prompt.rstrip()}\n\n"
        "CLOSER RUNTIME BINDING: invoke only this verified launcher command; "
        "it does not grant queue actions, promotion, or new authority:\n"
        f"{bound}\n"
    )


def plan_cli_run(
    agent: str,
    profile_id: str,
    prompt: str,
    *,
    cwd: str | Path = ".",
    manifest_path: str | Path,
    root: str | Path,
    manifest: Mapping[str, Any] | None = None,
    launcher_argv: Sequence[str] | None = None,
    own_launcher_path: str | Path | None = None,
    qualify_fn: Callable[[dict[str, Any], str | Path], list[str]] | None = None,
    policy_fn: Callable[[dict[str, Any]], str | None] | None = None,
    native_probe: Callable[[dict[str, Any]], str | None] | None = None,
) -> tuple[LaunchPlan | None, list[str]]:
    problems = validate_profile(agent, profile_id)
    if problems:
        return None, problems
    loaded, preflight = preflight_runtime(
        manifest_path,
        root,
        manifest=manifest,
        launcher_argv=launcher_argv,
        own_launcher_path=own_launcher_path,
        qualify_fn=qualify_fn,
        policy_fn=policy_fn,
        native_probe=native_probe,
    )
    if preflight:
        return None, preflight
    from closer_mechanisms import CLI, mechanism

    if mechanism(loaded) == "native-handoff/v1":
        return None, ["native host capability requires its observed wrapper entry, not CLI offload"]
    if mechanism(loaded) == CLI and (
        _normalize_agent(agent) != loaded["mechanism_contract"]["agent"]
        or profile_id != loaded["mechanism_contract"]["profile_id"]
    ):
        return None, ["selected agent/profile differs from CLI-qualified execution contract"]
    profile = execution_profiles.get_profile(profile_id)
    mode = profile_offload_mode(profile)
    bound_cmd = expected_host_launcher_command(loaded, manifest_path=manifest_path, root=root)
    bound_prompt = build_bound_request_prompt(prompt, bound_cmd)
    try:
        argv = adapters.build_command(
            profile["agent"],
            bound_prompt,
            mode,
            cwd=cwd,
            profile=profile,
            transport="offload",
        )
    except (RuntimeError, OSError, ValueError) as exc:
        return None, [f"CLI command preparation refused: {exc}"]
    hint = (
        "Invoke only this hash-verified runtime launcher command; it does not grant "
        "queue actions or promotion:\n" + " ".join(shlex.quote(x) for x in bound_cmd)
    )
    from closer_campaign import sha256_json

    plan = LaunchPlan(
        agent=profile["agent"],
        profile_id=profile["profile_id"],
        mechanism="cli-offload/v1",
        mode=mode,
        argv_preview=argv,
        manifest_checks=[],
        launcher_argv=bound_cmd,
        bound_request_prompt=bound_prompt,
        delegate_prompt_hint=hint,
        manifest_fingerprint=sha256_json(loaded),
    )
    return plan, []


def _known_prelaunch_refusal(result: Mapping[str, Any]) -> bool:
    """Only an explicit dispatcher refusal without conflicting execution evidence proves no start."""
    return (
        result.get("disabled") is True
        and not any(
            result.get(key) for key in ("run_id", "provider_completed", "outcome_uncertain")
        )
        and result.get("raw_exit") is None
        and result.get("retry_safe") is not False
    )


def classify_offload_result(result: Mapping[str, Any]) -> tuple[str, str]:
    """Preserve uncertainty; process exit codes alone never prove prelaunch refusal."""
    pending = result.get("completion_persistence")
    if (
        result.get("outcome_uncertain") is True
        or result.get("retry_safe") is False
        or isinstance(pending, dict)
        and pending.get("status") == "pending"
    ):
        return "unknown", str(result.get("error") or "cli offload outcome requires reconciliation")
    if _known_prelaunch_refusal(result):
        return "denied", str(result.get("error") or "offload disabled before submission")
    if result.get("exit") == 0:
        return "ok", "cli offload exit 0; runtime acceptance remains unverified"
    return "unknown", str(result.get("error") or "cli offload outcome unknown")


def _retain_offload_fields(result: Mapping[str, Any]) -> dict[str, Any]:
    return dict(result)


def run_cli(
    agent: str,
    profile_id: str,
    prompt: str,
    *,
    manifest_path: str | Path,
    root: str | Path,
    cwd: str | Path = ".",
    timeout: int | None = 30,
    manifest: Mapping[str, Any] | None = None,
    launcher_argv: Sequence[str] | None = None,
    own_launcher_path: str | Path | None = None,
    task_type: str = "closer_interpret",
    offload_fn: Callable[..., dict[str, Any]] | None = None,
    qualify_fn: Callable[[dict[str, Any], str | Path], list[str]] | None = None,
    policy_fn: Callable[[dict[str, Any]], str | None] | None = None,
    native_probe: Callable[[dict[str, Any]], str | None] | None = None,
) -> dict[str, Any]:
    """Guarded CLI path: manifest/pin/policy/qualify/native checks, then ``dispatcher.offload``."""
    if type(timeout) is not int or not 1 <= timeout <= 900:
        return {
            "outcome": "denied",
            "detail": "timeout must be a positive bounded value",
            "ran": False,
            "provider_started": False,
            "prelaunch_refused": True,
            "profile_id": profile_id,
            "transport": "cli_offload",
        }
    profile_problems = validate_profile(agent, profile_id)
    if profile_problems:
        return {
            "outcome": "denied",
            "detail": "; ".join(profile_problems),
            "ran": False,
            "provider_started": False,
            "prelaunch_refused": True,
            "profile_id": profile_id,
            "transport": "cli_offload",
        }
    plan, preflight = plan_cli_run(
        agent,
        profile_id,
        prompt,
        cwd=cwd,
        manifest_path=manifest_path,
        root=root,
        manifest=manifest,
        launcher_argv=launcher_argv,
        own_launcher_path=own_launcher_path,
        qualify_fn=qualify_fn,
        policy_fn=policy_fn,
        native_probe=native_probe,
    )
    if plan is None:
        return {
            "outcome": "denied",
            "detail": "; ".join(preflight),
            "ran": False,
            "provider_started": False,
            "prelaunch_refused": True,
            "profile_id": profile_id,
            "transport": "cli_offload",
            "manifest_problems": preflight,
        }
    if offload_fn is None:
        import dispatcher

        offload_fn = dispatcher.offload
    # Recheck after command preparation, immediately before submission. The child
    # launcher independently verifies the immutable package again before effects.
    from closer_campaign import sha256_json

    current, problems = preflight_runtime(
        manifest_path,
        root,
        manifest=manifest,
        launcher_argv=launcher_argv,
        own_launcher_path=own_launcher_path,
        qualify_fn=qualify_fn,
        policy_fn=policy_fn,
        native_probe=native_probe,
    )
    if sha256_json(current) != plan.manifest_fingerprint:
        problems.append("manifest changed after launch preparation")
    if problems:
        return {
            "outcome": "denied",
            "detail": "; ".join(problems),
            "ran": False,
            "provider_started": False,
            "prelaunch_refused": True,
            "profile_id": plan.profile_id,
            "transport": "cli_offload",
            "manifest_problems": problems,
        }
    dispatch_prompt = plan.bound_request_prompt
    try:
        result = offload_fn(
            plan.agent,
            dispatch_prompt,
            cwd=str(cwd),
            mode=plan.mode,
            timeout=timeout,
            profile_id=plan.profile_id,
            task_type=task_type,
        )
    except Exception as exc:
        return {
            "outcome": "unknown",
            "detail": f"cli offload raised: {exc}",
            "ran": None,
            "provider_started": None,
            "prelaunch_refused": False,
            "profile_id": plan.profile_id,
            "agent": plan.agent,
            "transport": "cli_offload",
            "plan": asdict(plan),
        }
    refused = _known_prelaunch_refusal(result)
    provider_started = (
        False
        if refused
        else (
            True
            if result.get("provider_completed") is True or result.get("raw_exit") is not None
            else None
        )
    )
    outcome, detail = classify_offload_result(result)
    return {
        "outcome": outcome,
        "detail": detail,
        "ran": provider_started,
        "provider_started": provider_started,
        "prelaunch_refused": refused,
        "queue_action_accepted": False,
        "desktop_qualified": False,
        "runtime_result_verified": False,
        "profile_id": plan.profile_id,
        "agent": plan.agent,
        "mode": plan.mode,
        "transport": "cli_offload",
        "runtime_action": "dispatcher.offload",
        "offload": _retain_offload_fields(result),
        "plan": asdict(plan),
    }


def _selftest() -> int:
    failures = 0
    inv = inventory()
    if len(inv["bindings"]) != len(APPROVED_AGENTS):
        print("FAIL: inventory missing approved agents")
        failures += 1
    vibe = next(b for b in inv["bindings"] if b["agent"] == "vibe")
    if vibe["desktop_native_supported"]:
        print("FAIL: vibe must not claim native handoff support")
        failures += 1
    if vibe["native_handoff_actor_known"]:
        print("FAIL: vibe actor must not be marked known for handoff")
        failures += 1
    bad_plan, bad = plan_cli_run(
        "codex",
        "claude-sonnet-5-5-high",
        "x",
        manifest_path="/nonexistent",
        root="/tmp",
    )
    if bad_plan is not None or not bad:
        print("FAIL: agent/profile mismatch should fail closed")
        failures += 1
    print(f"selftest: {failures} failures")
    return 1 if failures else 0


def main(
    argv: Sequence[str] | None = None, *, required_manifest_path: str | Path | None = None
) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("inventory", help="print agent CLI/desktop binding inventory")
    plan_p = sub.add_parser("plan", help="build a guarded CLI launch plan")
    plan_p.add_argument("--agent", required=True)
    plan_p.add_argument("--profile", required=True)
    plan_p.add_argument("--prompt", required=True)
    plan_p.add_argument("--manifest", required=True)
    plan_p.add_argument("--root", required=True)
    plan_p.add_argument("--launcher-argv", nargs="+")
    ready_p = sub.add_parser("readiness", help="CLI presence + profile validity")
    ready_p.add_argument("--agent", required=True)
    ready_p.add_argument("--profile", required=True)
    run_p = sub.add_parser("run", help="run guarded cli offload (real provider)")
    run_p.add_argument("--agent", required=True)
    run_p.add_argument("--profile", required=True)
    run_p.add_argument("--prompt", required=True)
    run_p.add_argument("--manifest", required=True)
    run_p.add_argument("--root", required=True)
    run_p.add_argument("--cwd", default=".")
    run_p.add_argument("--timeout", type=int, default=30)
    run_p.add_argument("--launcher-argv", nargs="+")
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.command == "inventory":
        print(json.dumps(inventory(), indent=2, sort_keys=True))
        return 0
    if args.command == "readiness":
        print(json.dumps(asdict(readiness(args.agent, args.profile)), indent=2, sort_keys=True))
        return 0
    if (
        required_manifest_path is not None
        and Path(args.manifest).resolve() != Path(required_manifest_path).resolve()
    ):
        print(json.dumps({"problems": ["inner manifest differs from verified launcher manifest"]}))
        return 78
    manifest, problems = load_manifest_file(args.manifest)
    if problems:
        print(json.dumps({"problems": problems}))
        return 78
    if args.command == "plan":
        plan, problems = plan_cli_run(
            args.agent,
            args.profile,
            args.prompt,
            manifest_path=args.manifest,
            root=args.root,
            manifest=manifest,
            launcher_argv=getattr(args, "launcher_argv", None),
        )
        payload = {"problems": problems, "plan": asdict(plan) if plan else None}
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 1 if problems else 0
    if args.command == "run":
        out = run_cli(
            args.agent,
            args.profile,
            args.prompt,
            manifest_path=args.manifest,
            root=args.root,
            cwd=args.cwd,
            timeout=args.timeout,
            manifest=manifest,
            launcher_argv=getattr(args, "launcher_argv", None),
        )
        print(json.dumps(out, indent=2, sort_keys=True))
        return 0 if out["outcome"] == "ok" else 1
    return 2


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        raise SystemExit(_selftest())
    raise SystemExit(main())
