#!/usr/bin/env python3
"""dispatcher.py — execute a routing-decision.json. The router PLANS + claims; the
dispatcher EXECUTES: it spawns each assignment's agent DETACHED (like the legacy
relay, so the tick returns fast), wrapped so the claim is released and the ledger
recorded when the agent exits, and it writes the orchestrator heartbeat that the
legacy cron yield-guard reads.

Integration seams (clearly stubbed for v0, wired against existing lane infra later):
  - WORKTREE: where each repo is checked out for the agent to work in. Defaults to
    $ORCH_WORKTREE_BASE/<owner__repo>; falls back to $HOME with a note if absent.
  - PROMPTS: per-task-type templates. Real runs should inject the issue/PR body
    (gh) into {target_detail}; v0 leaves it minimal.

`--dry-run` prints what it WOULD spawn (no processes, no side effects beyond the
heartbeat unless --no-heartbeat). `--selftest` is fully offline.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

import adapters
import claims
import execution_profiles
import feedback
import provision
import repo_knowledge

ORCH_DIR = Path(__file__).resolve().parent
REAL_HOME = Path.home()
LOCAL_RUNTIME = Path(os.environ.get("ORCH_LOCAL_RUNTIME", REAL_HOME / ".codex" / "orchestrator"))
HANDOFF = Path(os.environ.get("HANDOFF_DIR", Path.home() / ".codex" / "handoff"))
DECISION_JSON = HANDOFF / "routing-decision.json"
HEARTBEAT_JSON = HANDOFF / "orchestrator.json"
DISPATCH_LOG_DIR = HANDOFF / "dispatch-logs"
CLAIMS_PY = ORCH_DIR / "claims.py"
OFFLOAD_DIR = Path(
    os.environ.get("ORCH_OFFLOAD_DIR", Path.home() / ".codex" / "orchestrator" / "offloads")
)
AGENT_RUNTIME_DIR = Path(os.environ.get("ORCH_AGENT_RUNTIME_DIR", LOCAL_RUNTIME / "agent-runtime"))


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


DEFAULT_OFFLOAD_TIMEOUT = _env_int("ORCH_DEFAULT_OFFLOAD_TIMEOUT", 1800)
DEFAULT_GEMINI_OFFLOAD_TIMEOUT = _env_int("ORCH_GEMINI_OFFLOAD_TIMEOUT", 600)

# Per-agent credential files the detached wrapper sources before running the agent (PATH
# only makes the binary resolve — agents still need auth). codex (~/.codex/auth.json) and
# vibe (~/.vibe/.env) auto-load their own creds, so they need no explicit source here.
AUTH_FILES = {
    "cursor": str(REAL_HOME / ".cursor" / "cursor-agent.env"),  # exports CURSOR_API_KEY
    "claude": str(
        REAL_HOME / ".codex" / "handoff" / ".claude-oauth-token"
    ),  # exports CLAUDE_CODE_OAUTH_TOKEN
    "aider": str(REAL_HOME / ".codex" / "handoff" / "aider.env"),  # exports MISTRAL_API_KEY
}

CRITICAL_EVALUATOR_DIRECTIVE = """CRITICAL-EVALUATOR STANCE (non-negotiable): Your job is correct judgment, not agreement. Evaluate
claims, designs, and instructions on the merits before agreeing — including the orchestrator's and the
user's. When something is wrong, weaker than an alternative, or missing, say so plainly and lead with the
strongest objection. Separate "this is correct" from "I'll do as asked." State your confidence and what
would change your mind; flag what you are unsure of. Do not soften a real problem to be agreeable, and do
not manufacture disagreement to seem rigorous — calibrated dissent, not maximal."""

AGENT_PERSONAS = {
    "cursor": "You are Cursor (composer). Strong on well-specified, mechanical changes. Keep diffs minimal and scoped — never reformat or rewrite unrelated code. Match the surrounding style exactly.",
    "vibe": "You are Vibe (Devstral/Mistral), a flat-rate implementer lane. State your assumptions explicitly, prefer the smallest change that satisfies the spec, and report what you did NOT do.",
    "gemini": "You are Gemini via Antigravity (agy): large-context reasoning and big reads. CRITICAL: return the full deliverable in STDOUT — do NOT write it only to an artifact file the orchestrator cannot read. Don't spend this metered seat on trivial mechanical work.",
    "codex": "You are Codex, a reasoning + implementation seat. Verify before claiming done; surface risks and unknowns rather than papering over them.",
    "aider": "You are Aider, running from an isolated venv. Make focused, test-backed edits; report exactly what changed.",
}

TESTGEN_READ_ONLY_GATE_GUARD = (
    "Do not edit `testgen_gate.py`, `testgen_lane.py`, or other Orchestrator "
    "gate/helper files; they are acceptance infrastructure. If the gate appears "
    "wrong, stop and report the failing check instead of changing the gate. "
    "Before committing, run `git diff --name-only` and confirm no Orchestrator "
    "gate/helper file changed."
)

# task_type -> the capability whose schema that task's prompt literally cites.
#
# These lane "capabilities" are PROMPT-SCHEMA CONTRACTS, not locally-executed code paths. The
# dispatcher never runs `cross_repo_lane.py`; it hands an agent a prompt saying "produce strict JSON
# matching cross_repo_lane.py" and the agent returns conforming output. So their heartbeats could
# never fire from a dispatch, and the inventory reported `no_matching_work` for capabilities whose
# work-type was demonstrably being routed (measured 2026-08-19: 1 cross_repo dispatch, 2 testgen,
# yet last_match=None on all of them).
#
# A `match` is recorded here, NOT an `invocation`. The honest claim is that work of this type was
# routed and the capability's contract shaped the prompt — the module itself did not run. That
# moves these from a FALSE "no matching work" to a TRUE `matched_not_invoked`, which is the state
# the inventory's next-action text already handles correctly. Only mappings where the template
# actually names the module are listed; `review` is deliberately absent because its prompt does not
# cite `adversarial.py`.
TASK_TYPE_CAPABILITY = {
    "testgen": "testgen-lane",
    "epic": "epic-decomposition",
    "codemod": "codemod-campaign",
    "cross_repo": "cross-repo-coordination",
    "runtime_ac": "runtime-ac-checks",
}

PROMPT_TEMPLATES = {
    "implement": (
        "Work {target} to completion: satisfy ALL its acceptance criteria and complete "
        "EVERY task in its checklist. Your goal is the issue's criteria, not a reviewer's "
        "approval. {target_detail}"
    ),
    "testgen": (
        "Generate focused pytest coverage for {target}. Add tests only unless a tiny, clearly "
        "explained production-code testability fix is unavoidable. Before committing, run the "
        "Orchestrator test-generation acceptance gate from testgen_gate.py (or the exact "
        "testgen_lane.py prompt command if provided), iterate until it passes, and include the "
        f"gate command/result in the PR body. {TESTGEN_READ_ONLY_GATE_GUARD} "
        "{target_detail}"
    ),
    "mechanical": (
        "Apply the mechanical change for {target} (formatting / lint / dependency bump / "
        "docstrings / codemod as specified). Keep the diff minimal and strictly in-scope. "
        "{target_detail}"
    ),
    "polish": (
        "Apply the small, bounded improvements noted for {target} as ONE focused follow-up PR. "
        "Do not expand scope. {target_detail}"
    ),
    "review": (
        "Review {target} against its acceptance criteria and emit a STRUCTURED, ADVISORY "
        "(non-gating) verdict: tasks_complete, each acceptance criterion met/unmet with "
        "evidence (file:line or test), and improvements[]. {target_detail}"
    ),
    "epic": (
        "Build or validate an Orchestrator epic decomposition plan for {target}. Produce strict "
        "JSON matching epic_lane.py: epic metadata, dispatchable subtasks, integration order, "
        "and re-decomposition triggers. Do not implement the subtasks in this planning pass. "
        "{target_detail}"
    ),
    "codemod": (
        "Author, validate, or review a cross-file codemod/refactor campaign for {target}. "
        "Produce strict JSON matching codemod_lane.py, run only dry-run/review_before_run "
        "commands from the plan, and do not auto-apply mutating codemods or open batched PRs "
        "without explicit approval. {target_detail}"
    ),
    "cross_repo": (
        "Author, validate, or review a cross-repo coordinated-change plan for {target}. "
        "Produce strict JSON matching cross_repo_lane.py, and generate a dry-run rollout "
        "plan with planned source/consumer work items, barrier ordering, and dispatch-ready "
        "prompts. Do not create branches, labels, issues, PRs, or merges in this planning pass. "
        "{target_detail}"
    ),
    "runtime_ac": (
        "Turn the goal/issue for {target} into a structured runtime acceptance-criteria "
        "verification spec. Produce strict JSON matching runtime_ac.py: verification metadata, "
        "AC-bound evidence_required lists, checks (frontend, command, deliberate_break, manual), "
        "non_regression checks, and verdict_policy. Generate a dry-run verification plan with "
        "review-before-run commands. Set verification.target to the exact target and "
        "verification.repo to its exact owner/repo. Save the final JSON artifact, then run "
        "runtime_ac_gate.py --materialize-range-spec <artifact> --target {target} --json so "
        "the next closer gate reads the same validated path and hash; report the terminal "
        "materialization result. Do not execute arbitrary project commands or mutate repositories. "
        "{target_detail}"
    ),
}


def write_heartbeat() -> dict:
    """Tell the legacy cron yield-guard the orchestrator is live this tick."""
    HANDOFF.mkdir(parents=True, exist_ok=True)
    hb = {"generated_at": int(time.time()), "pid": os.getpid()}
    HEARTBEAT_JSON.write_text(json.dumps(hb) + "\n")
    return hb


# item 16h (2026-07-08): interrupt-as-data, non-blocking by contract. Agents surface PRODUCT-level
# decisions as data and keep working with their stated default; ledger_reconcile harvests the line
# into feedback.owner_questions, defaults auto-ratify at expiry (owner: no mounting backlog), and
# resolved decisions are injected back into future prompts below.
OWNER_QUESTION_PROTOCOL = (
    "If a PRODUCT-level decision genuinely needs the owner (naming, user-facing defaults, scope "
    "trade-offs — never code style or implementation detail), print ONE line exactly:\n"
    'OWNER_QUESTION: {"question": "<one sentence>", "default": "<the choice you are making>", '
    '"expires_days": 7}\n'
    "then PROCEED WITH YOUR STATED DEFAULT — never stop or wait for an answer."
)


def _owner_decision_block(target: str) -> str:
    repo = target.split("#")[0].split(" ")[0] if "/" in target else None
    try:
        decisions = feedback.owner_decisions_for(repo=repo, target=target)
    except Exception:
        decisions = []
    if not decisions:
        return ""
    lines = "\n".join(f"- {d['question']} -> {d['decision']}" for d in decisions)
    return f"OWNER DECISIONS (follow these; they override defaults):\n{lines}"


def _lane_capability_match(task_type: str) -> None:
    """Record that a lane capability's prompt-schema contract was routed work of its type.

    Lazy import + never raises + inert outside an active tick, matching the sibling modules. See
    TASK_TYPE_CAPABILITY for why this records `match` rather than `invocation`.
    """
    capability_id = TASK_TYPE_CAPABILITY.get(task_type)
    if not capability_id:
        return
    try:
        import capabilities

        capabilities.production_heartbeat(
            capability_id,
            "match",
            ref=f"dispatcher.build_prompt:{task_type}",
            metadata={"task_type": task_type},
        )
    except Exception:
        pass


def build_prompt(
    task_type: str, target: str, target_detail: str = "", lane: str | None = None
) -> str:
    target_detail = repo_knowledge.append_context(
        target_detail,
        target,
        task_type=task_type,
        lane=lane,
    )
    decisions = _owner_decision_block(target)
    if decisions:
        target_detail = f"{target_detail}\n\n{decisions}".strip()
    _lane_capability_match(task_type)
    tmpl = PROMPT_TEMPLATES.get(task_type)
    if not tmpl:
        body = f"Work {target} ({task_type}). {target_detail}".strip()
    else:
        body = tmpl.format(target=target, target_detail=target_detail).strip()
    return f"{body}\n\n{OWNER_QUESTION_PROTOCOL}"


def _is_git_worktree(cwd: str | Path) -> bool:
    res = subprocess.run(
        ["git", "-C", str(cwd), "rev-parse", "--is-inside-work-tree"],
        capture_output=True,
        text=True,
    )
    return res.returncode == 0 and res.stdout.strip() == "true"


def _agent_preamble(agent: str) -> str:
    persona = AGENT_PERSONAS.get(agent, "")
    return (
        f"{persona}\n\n{CRITICAL_EVALUATOR_DIRECTIVE}" if persona else CRITICAL_EVALUATOR_DIRECTIVE
    )


def _offload_prompt(prompt: str, cwd: str | Path, agent: str | None = None) -> str:
    rules = [
        "OFFLOAD WORKSPACE RULES:",
        "- This is a synchronous offload; return the useful result to the orchestrator in stdout.",
        (
            "- Do not return progress updates, future-tense promises, or 'I will inspect later' status. "
            "Finish the requested deliverable before exiting."
        ),
        (
            "- If you cannot complete the offload in this invocation, print exactly "
            "OFFLOAD_INCOMPLETE: <reason> and stop."
        ),
        "- Do not run git commit, git push, or gh pr create from an offload.",
        "- If you edit files, keep changes inside the current workspace and report changed paths.",
        (
            "- Product-level owner decision needed? Print one line "
            'OWNER_QUESTION: {"question": "...", "default": "...", "expires_days": 7} '
            "and PROCEED with your default — never wait."
        ),
    ]
    if not _is_git_worktree(cwd):
        rules.insert(
            1, "- Non-git workspace: do not try to create commits, branches, pushes, or PRs."
        )
    return f"{_agent_preamble(agent or '')}\n\n{prompt.rstrip()}\n\n" + "\n".join(rules)


def _offload_ignore(_dir: str, names: list[str]) -> set[str]:
    heavy = {
        ".git",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".next",
        "dist",
        "build",
    }
    return {name for name in names if name in heavy or name.endswith(".pyc")}


def _isolate_offload_cwd(cwd: str | Path) -> Path:
    src = Path(cwd).expanduser().resolve()
    if not src.is_dir():
        raise FileNotFoundError(f"offload cwd is not a directory: {src}")
    OFFLOAD_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    base = OFFLOAD_DIR / f"{stamp}-{src.name or 'workspace'}-{os.getpid()}"
    dest = base
    i = 1
    while dest.exists():
        i += 1
        dest = OFFLOAD_DIR / f"{base.name}-{i}"
    shutil.copytree(src, dest, ignore=_offload_ignore)
    return dest


_OFFLOAD_PROGRESS_ONLY_PATTERNS = (
    r"\bi (?:am|'m) waiting for\b",
    r"\bwaiting for (?:the )?(?:pytest|test|tests|suite|command|run|process)\b",
    r"\bwaiting for .+\bto finish\b",
    r"\bwaiting for .+\brunning as `?task-[\w-]+`?\b",
    r"\bi will (?:inspect|check|review|continue|look at)\b.*\b(?:as soon as|when|after)\b",
    r"\bwill inspect (?:the )?results\b",
    r"\bwhen (?:it|the task|the command|the tests?|the suite) (?:completes?|finishes?)\b",
    r"\bno active tools are needed at the moment\b",
    r"\bstill running\b",
)


_TRANSIENT_NETWORK_PATTERNS = (
    r"connection reset",
    r"reset by peer",
    r"i/o timeout",
    r"connection refused",
    r"\bEOF\b",
    r"TLS handshake",
    r"temporarily unavailable",
    r"\b50[234]\b",
    r"broken pipe",
    r"network is unreachable",
)

_AUTH_USAGE_PATTERNS = (
    r"unauthorized",
    r"\b401\b",
    r"\b403\b",
    r"invalid_grant",
    r"\blogin\b",
    r"quota",
    r"permission denied",
)


def _is_transient_network_failure(
    error: str | None, agent_log_tail: str | None, stderr: str | None
) -> bool:
    text = "\n".join(str(part or "") for part in (error, agent_log_tail, stderr))
    if not text.strip():
        return False
    if any(re.search(pattern, text, re.IGNORECASE) for pattern in _AUTH_USAGE_PATTERNS):
        return False
    return any(re.search(pattern, text, re.IGNORECASE) for pattern in _TRANSIENT_NETWORK_PATTERNS)


def _offload_incomplete_reason(output: str, *, progress_only: bool = True) -> str | None:
    """Detect offload responses that are statuses, not deliverables.

    The confirmed Gemini failure was a short stdout-only promise to inspect test results later while the
    process exited 0. Treat explicit OFFLOAD_INCOMPLETE markers from any agent as failures, and treat
    short progress-only prose as failures for lanes where that heuristic is enabled.
    """
    text = (output or "").strip()
    if not text:
        return "agent returned no stdout"
    normalized = re.sub(r"\s+", " ", text.lower())
    if "offload_incomplete:" in normalized:
        return "agent reported OFFLOAD_INCOMPLETE"
    if not progress_only or len(normalized) > 800:
        return None
    for pattern in _OFFLOAD_PROGRESS_ONLY_PATTERNS:
        if re.search(pattern, normalized):
            return "agent returned progress-only stdout instead of a deliverable"
    return None


def _path_prefix() -> str:
    """PATH for child agent CLIs, independent of any per-agent HOME override."""
    return (
        f'export PATH="/opt/homebrew/bin:{REAL_HOME}/.local/bin:' f'{REAL_HOME}/.cursor/bin:$PATH"'
    )


# Proxy env hygiene for the agent subshell. ROOT CAUSE (diagnosed 2026-06-20): an in-session
# `dispatcher.py offload` (Bash tool of a Claude seat) inherits the AMBIENT shell environment and
# passes it straight to the agent CLI (no env= on subprocess) — unlike the launchd fleet, which runs
# in a clean controlled env. If that ambient env carries a stray *_PROXY pointing somewhere
# unreachable, codex (ChatGPT backend) and gemini/agy (Antigravity backend) — both outbound HTTPS —
# BLOCK at connect(): ~0% CPU, zero output, clean timeout (exit 124). cursor/vibe (API-key HTTPS)
# hit the same trap but the symptom was only ever observed on codex/gemini. The hang was per-SESSION
# (one session 6/6 hung while a concurrent session 0/6 hung) — the signature of inherited env, not
# contention/auth/desktop-app/concurrency (all of those were ruled out empirically). A dead
# HTTPS_PROXY reproduces the exact symptom; scrubbing it restores reliable output. The launchd fleet
# proves this machine needs no proxy to reach the agent backends, so we strip the proxy family for
# the agent subshell to match the fleet's clean env. Genuinely-proxied machines set ORCH_KEEP_PROXY=1.
_PROXY_VARS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "FTP_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "ftp_proxy",
    "no_proxy",
)


def _net_hygiene_prelude() -> str:
    """`unset` the inherited proxy family so a stray/dead *_PROXY can't blackhole the agent's HTTPS.
    No-op when ORCH_KEEP_PROXY=1 (machines that truly require a proxy to reach the agent backends).
    """
    if os.environ.get("ORCH_KEEP_PROXY") == "1":
        return ""
    return "unset " + " ".join(_PROXY_VARS) + "; "


def _suspicious_net_env() -> list[str]:
    """Ambient network-affecting env vars present at dispatch — surfaced on a 0-byte timeout so an
    env-induced hang is self-diagnosing instead of mysterious (proxy is the confirmed culprit class;
    CA-bundle / NODE_OPTIONS overrides are reported too because they can also wedge a TLS/agent start).
    """
    names = _PROXY_VARS + (
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
        "NODE_EXTRA_CA_CERTS",
        "NODE_OPTIONS",
    )
    return [f"{n}={os.environ[n]}" for n in names if os.environ.get(n)]


def _runtime_link(src: Path, dst: Path) -> None:
    """Link a real-home config/credential file into a writable runtime home."""
    if not src.exists() or dst.exists() or dst.is_symlink():
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        dst.symlink_to(src)
    except FileExistsError:
        pass


def _write_vibe_runtime_config(src: Path, dst: Path, session_dir: Path) -> None:
    """Copy Vibe config while forcing session logs into the runtime home."""
    if not src.exists():
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    in_session_logging = False
    saw_section = False
    saw_save_dir = False
    out: list[str] = []
    for line in src.read_text().splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            if in_session_logging and not saw_save_dir:
                out.append(f'save_dir = "{session_dir}"')
                saw_save_dir = True
            in_session_logging = stripped == "[session_logging]"
            saw_section = saw_section or in_session_logging
        if in_session_logging and stripped.startswith("save_dir"):
            out.append(f'save_dir = "{session_dir}"')
            saw_save_dir = True
            continue
        out.append(line)
    if in_session_logging and not saw_save_dir:
        out.append(f'save_dir = "{session_dir}"')
        saw_save_dir = True
    if not saw_section:
        out.extend(["", "[session_logging]", f'save_dir = "{session_dir}"', "enabled = true"])
    if dst.exists() or dst.is_symlink():
        dst.unlink()
    dst.write_text("\n".join(out) + "\n")


def _ensure_agent_runtime(agent: str) -> Path:
    """Create writable per-agent state roots under ~/.codex/orchestrator.

    Codex runs with a restricted writable root. Some child CLIs try to create
    logs/projects under their real-home dotdirs (`~/.cursor`, `~/.vibe`), which
    fails before the agent can do useful work. Keep credentials/config readable
    from the real home, but redirect mutable agent state into the orchestrator
    runtime.
    """
    base = AGENT_RUNTIME_DIR / agent
    base.mkdir(parents=True, exist_ok=True)
    for sub in (".cache", ".config", ".local/share", "tmp"):
        (base / sub).mkdir(parents=True, exist_ok=True)

    if agent == "cursor":
        cursor_home = base / ".cursor"
        for sub in ("projects", "chats", "ai-tracking"):
            (cursor_home / sub).mkdir(parents=True, exist_ok=True)
        (base / "config").mkdir(parents=True, exist_ok=True)
        (base / "node-compile-cache").mkdir(parents=True, exist_ok=True)
        _runtime_link(REAL_HOME / ".cursor" / "cursor-agent.env", cursor_home / "cursor-agent.env")
        _runtime_link(
            REAL_HOME / ".cursor" / "cli-config.json", base / "config" / "cli-config.json"
        )
        _runtime_link(
            REAL_HOME / ".cursor" / "agent-cli-state.json", base / "config" / "agent-cli-state.json"
        )
    elif agent == "vibe":
        vibe_home = base / ".vibe"
        session_dir = vibe_home / "logs" / "session"
        session_dir.mkdir(parents=True, exist_ok=True)
        _runtime_link(REAL_HOME / ".vibe" / ".env", vibe_home / ".env")
        _write_vibe_runtime_config(
            REAL_HOME / ".vibe" / "config.toml", vibe_home / "config.toml", session_dir
        )
        _runtime_link(
            REAL_HOME / ".vibe" / "trusted_folders.toml", vibe_home / "trusted_folders.toml"
        )
    elif agent == "gemini":
        (base / "logs").mkdir(parents=True, exist_ok=True)
        gemini_dir = base / ".gemini"
        for sub in (
            "config/projects",
            "antigravity-cli/bin",
            "antigravity-cli/brain",
            "antigravity-cli/builtin",
            "antigravity-cli/cache",
            "antigravity-cli/conversations",
            "antigravity-cli/implicit",
            "antigravity-cli/knowledge",
            "antigravity-cli/log",
            "antigravity/knowledge",
        ):
            (gemini_dir / sub).mkdir(parents=True, exist_ok=True)
    return base


def _agent_runtime_prelude(agent: str) -> str:
    """Shell exports for the child agent only; run this inside a subshell."""
    base = _ensure_agent_runtime(agent)
    exports = [
        f"export ORCH_REAL_HOME={shlex.quote(str(REAL_HOME))}",
        f"export ORCH_AGENT_RUNTIME={shlex.quote(str(base))}",
        f"export XDG_CACHE_HOME={shlex.quote(str(base / '.cache'))}",
        f"export XDG_CONFIG_HOME={shlex.quote(str(base / '.config'))}",
        f"export XDG_DATA_HOME={shlex.quote(str(base / '.local/share'))}",
        f"export TMPDIR={shlex.quote(str(base / 'tmp'))}",
    ]
    if agent == "cursor":
        # Cursor's login-keychain probe fails in restricted Codex subprocesses. Keep HOME real for
        # shell semantics, source CURSOR_API_KEY from the real auth file, and force Cursor to keep
        # only per-run credentials in memory while mutable data/cache live in the Orchestrator runtime.
        exports.extend(
            [
                "export AGENT_CLI_CREDENTIAL_STORE=memory",
                f"export CURSOR_DATA_DIR={shlex.quote(str(base / '.cursor'))}",
                f"export CURSOR_CONFIG_DIR={shlex.quote(str(base / 'config'))}",
                f"export NODE_COMPILE_CACHE={shlex.quote(str(base / 'node-compile-cache'))}",
            ]
        )
    elif agent == "vibe":
        exports.append(f"export VIBE_HOME={shlex.quote(str(base / '.vibe'))}")
    return "; ".join(exports) + "; "


def _auth_prelude(agent: str) -> str:
    auth_file = AUTH_FILES.get(agent)
    if not auth_file:
        return ""
    quoted = shlex.quote(auth_file)
    return f"set -a; [ -f {quoted} ] && . {quoted}; set +a; "


def _routing_metadata(assignment: dict) -> dict | None:
    keys = {
        "reason",
        "exploration",
        "exploration_mode",
        "capacity_state",
        "capacity_policy",
        "selected_profile_id",
        "profile_decision",
        "requested_profile_id",
    }
    if not any(key in assignment for key in keys):
        return None
    metadata = {
        "source": "router_assignment",
        "reason": assignment.get("reason"),
        "exploration": bool(assignment.get("exploration")),
        "exploration_mode": assignment.get("exploration_mode") or "",
        "capacity_state": assignment.get("capacity_state"),
        "capacity_policy": assignment.get("capacity_policy"),
        "selected_profile_id": assignment.get("selected_profile_id"),
        "requested_model": assignment.get("requested_model"),
        "reasoning_effort": assignment.get("reasoning_effort"),
        "permission_mode": assignment.get("permission_mode"),
        "transport": assignment.get("transport"),
        "profile_policy_version": assignment.get("profile_policy_version"),
        "profile_assignment_probability": assignment.get("profile_assignment_probability"),
        "profile_rng_seed": assignment.get("profile_rng_seed"),
        "profile_decision_id": (assignment.get("profile_decision") or {}).get("decision_id"),
        "profile_attempt_ids": assignment.get("profile_attempt_ids") or [],
        "capability_decision": assignment.get("capability_decision"),
        "eligible_capability_ids": assignment.get("eligible_capability_ids") or [],
        "capability_rejection_reasons": assignment.get("capability_rejection_reasons") or {},
        "selected_capability_id": assignment.get("selected_capability_id"),
        "selected_capability_version_id": assignment.get("selected_capability_version_id"),
        "capability_policy_version": assignment.get("capability_policy_version"),
        "capability_assignment_probability": assignment.get("capability_assignment_probability"),
        "capability_rng_seed": assignment.get("capability_rng_seed"),
        "capability_fallback": assignment.get("capability_fallback"),
        "requested_profile_id": assignment.get("requested_profile_id"),
    }
    return {key: value for key, value in metadata.items() if value not in (None, "", [], {})}


def plan_dispatch(assignment: dict, *, dry_run: bool = False) -> dict | None:
    """Resolve an assignment into a concrete spawnable command.

    dry_run: PREDICT the worktree path (no clone/worktree, no side effects).
    active : actually PROVISION a local-disk worktree (provision.py) so the agent has a
             real, pushable checkout. A provision failure returns an {error} dict (the
             assignment is skipped, claim is freed by the caller) rather than crashing the tick.
    """
    agent, mode = assignment["agent"], assignment.get("mode")
    target, task_type = assignment["target"], assignment["task_type"]
    lane = assignment.get("lane") or "opener"
    # An assessing orchestrator passes its OWN crafted prompt; fall back to the template only
    # when none is given (the deterministic/router path).
    if assignment.get("prompt"):
        prompt = repo_knowledge.append_context(
            assignment["prompt"],
            target,
            task_type=task_type,
            lane=lane,
        )
    else:
        prompt = build_prompt(task_type, target, lane=lane)
    profile_id = assignment.get("selected_profile_id")
    selected_profile = execution_profiles.get_profile(profile_id) if profile_id else None
    model = adapters.model_identity(agent, mode, selected_profile)
    if dry_run:
        cwd = provision.worktree_path(target, lane)
        worktree_missing = not (cwd / ".git").exists()
    else:
        try:
            cwd = provision.provision(target, lane)
            worktree_missing = False
        except Exception as exc:  # clone/worktree/gh failure — skip this assignment
            return {"error": f"provision failed: {exc}", "target": target, "agent": agent}
    role_activation = None
    try:
        # Local import avoids making deterministic dispatcher module import-time
        # dependent on the optional role layer (roles itself imports dispatcher).
        import roles

        role_activation = roles.activate_dispatch_roles(
            assignment, prompt, cwd=str(cwd), dry_run=dry_run
        )
        prompt = role_activation["prompt"]
        assignment = dict(assignment)
        assignment["influenced_by_role_run_ids"] = list(
            dict.fromkeys(
                list(assignment.get("influenced_by_role_run_ids") or [])
                + list(role_activation.get("accepted_role_run_ids") or [])
            )
        )
        assignment["rejected_role_run_ids"] = list(
            dict.fromkeys(
                list(assignment.get("rejected_role_run_ids") or [])
                + list(role_activation.get("rejected_role_run_ids") or [])
            )
        )
    except Exception as exc:
        # Role shadowing must never weaken or stop the deterministic dispatch rail.
        role_activation = {"error": str(exc), "prompt": prompt}
    # OUTSIDE the role try/except on purpose: capability tagging is deterministic-rail bookkeeping
    # and must not be lost because the optional role layer raised. `assignment` may still be the
    # caller's dict here, so copy before mutating.
    assignment = dict(assignment)
    assignment["capability_ids"] = list(
        dict.fromkeys(
            list(assignment.get("capability_ids") or [])
            + _exercised_capability_ids(assignment, agent)
        )
    )
    prompt = f"{_agent_preamble(agent)}\n\n{prompt}"
    if agent == "gemini":
        prompt = _gemini_workspace_prompt(prompt, cwd)
    try:
        if selected_profile:
            argv = adapters.build_command(
                agent,
                prompt,
                mode,
                cwd=cwd,
                profile=selected_profile,
                transport=assignment.get("transport") or "local",
                permission_mode=assignment.get("permission_mode"),
                reasoning_effort=assignment.get("reasoning_effort"),
                requested_model=assignment.get("requested_model"),
            )
        else:
            argv = adapters.build_command(agent, prompt, mode, cwd=cwd)
    except ValueError:
        return None  # unknown agent — skip gracefully
    # Detached wrapper, in order: (1) PATH fix so local tools (agy, vibe, cursor-agent) resolve
    # without depending on the child agent's HOME; (2) run the agent in a subshell with writable
    # per-agent state/cache/log dirs; (3) ALWAYS release the claim outside that subshell so
    # claims.py still sees the real HOME/HANDOFF defaults.
    path_prefix = _path_prefix()
    # `set -a` auto-EXPORTS what the env file sets — the files are bare KEY=value (no `export`),
    # so a plain `. file` would set a shell var the child agent never inherits (the auth failure
    # the first demo hit). set -a around the source exports CURSOR_API_KEY / tokens to the agent.
    auth_prelude = _auth_prelude(agent)
    agent_prelude = _agent_runtime_prelude(agent)
    release = shlex.join(["python3", str(CLAIMS_PY), "release", target, agent])
    # _net_hygiene_prelude lives INSIDE the agent subshell (release runs outside it, on the real env).
    wrapped = f"{path_prefix}; ({_net_hygiene_prelude()}{agent_prelude}{auth_prelude}{shlex.join(argv)}); {release}"
    return {
        "agent": agent,
        "mode": mode,
        "target": target,
        "lane": lane,
        "task_type": task_type,
        "model": model,
        "cwd": str(cwd),
        "argv": argv,
        "wrapped": wrapped,
        "worktree_missing": worktree_missing,
        "feedback_mode": assignment.get("feedback_mode"),
        "selected_profile_id": profile_id,
        "requested_model": selected_profile.get("requested_model") if selected_profile else None,
        "profile_policy_version": assignment.get("profile_policy_version"),
        "profile_assignment_probability": assignment.get("profile_assignment_probability"),
        "profile_decision": assignment.get("profile_decision"),
        "routing_metadata": _routing_metadata(assignment),
        "role_activation": role_activation,
        "influenced_by_role_run_ids": list(assignment.get("influenced_by_role_run_ids") or []),
        "rejected_role_run_ids": list(assignment.get("rejected_role_run_ids") or []),
        "influenced_by_skill_event_ids": list(
            assignment.get("influenced_by_skill_event_ids") or []
        ),
        "influenced_by_workflow_ids": list(assignment.get("influenced_by_workflow_ids") or []),
        "capability_ids": list(assignment.get("capability_ids") or []),
        "capability_version_ids": list(assignment.get("capability_version_ids") or []),
        "acceptance_gate_ids": list(assignment.get("acceptance_gate_ids") or []),
    }


# Infrastructure capabilities this dispatch actually EXERCISES, tagged onto the run so the
# outcome can come back to them. Not a heuristic and not the entrypoint-string guess
# capability_outcome_bridge deliberately refuses: each condition below is the same condition the
# capability's own heartbeat fires on, so a tag means "this ran" for exactly the reason the ledger
# already records. Without the tag the capability records that it RAN and never how the work turned
# out, which is what put both of these in `invoked_without_outcomes` with zero edges. (2026-08-21)
def _exercised_capability_ids(assignment: dict, agent: str) -> list[str]:
    out: list[str] = []
    # adapters.build_command: "The agy seat's runtime isolation (--gemini_dir + absolute
    # --add-dir) IS this capability" — so every gemini dispatch exercises it.
    if agent == "gemini":
        out.append("agy-runtime-isolation")
    # router.py tags this only when Thompson sampling ACTUALLY chose a challenger, not merely when
    # the flag is set. Mirror that condition exactly; a routing choice influences the run it routed.
    if (
        assignment.get("exploration")
        and str(assignment.get("exploration_mode") or "") == "thompson-hybrid"
    ):
        out.append("thompson-hybrid-routing")
    return out


def _spawn(d: dict) -> int:
    DISPATCH_LOG_DIR.mkdir(parents=True, exist_ok=True)
    safe = d["target"].replace("/", "__").replace("#", "_")
    logf = DISPATCH_LOG_DIR / f"{safe}.{d['agent']}.log"
    run_id = d.get("run_id") or f"{safe}-{d['agent']}-{time.time_ns()}"
    d["run_id"] = run_id
    started_ts = int(time.time())
    d["started_ts"] = started_ts
    selected_profile = (
        execution_profiles.get_profile(d["selected_profile_id"])
        if d.get("selected_profile_id")
        else None
    )
    profile_attempt_id = f"attempt:profile:{run_id}" if selected_profile else None
    if profile_attempt_id:
        routing_metadata = dict(d.get("routing_metadata") or {})
        routing_metadata["profile_attempt_ids"] = [profile_attempt_id]
        d["routing_metadata"] = routing_metadata
    lineage_required = any(
        d.get(key)
        for key in (
            "influenced_by_role_run_ids",
            "rejected_role_run_ids",
            "influenced_by_skill_event_ids",
            "influenced_by_workflow_ids",
            "capability_ids",
            "capability_version_ids",
            "acceptance_gate_ids",
        )
    )
    routing_metadata = d.get("routing_metadata") or {}
    if isinstance(routing_metadata, dict) and any(
        routing_metadata.get(key)
        for key in (
            "profile_id",
            "selected_profile_id",
            "requested_profile_id",
            "arm_id",
            "member_id",
        )
    ):
        lineage_required = True
    run_args = (
        run_id,
        d["target"],
        d.get("task_type") or "delegated",
        d["agent"],
    )
    run_kwargs = {
        "mode": d.get("feedback_mode") or d.get("mode"),
        "reasoning_level": d.get("mode"),
        "model": d.get("model"),
        "routing_metadata": d.get("routing_metadata"),
        "influenced_by_role_run_ids": d.get("influenced_by_role_run_ids"),
        "influenced_by_skill_event_ids": d.get("influenced_by_skill_event_ids"),
        "influenced_by_workflow_ids": d.get("influenced_by_workflow_ids"),
        "capability_ids": d.get("capability_ids"),
        "capability_version_ids": d.get("capability_version_ids"),
        "acceptance_gate_ids": d.get("acceptance_gate_ids"),
    }
    if selected_profile:
        # Profile attribution is causal learning data. Persist the run before
        # the attempt or process so a DB failure cannot launch unjoinable work
        # or leave an orphan execution_attempt.
        feedback.record_run(*run_args, **run_kwargs)
    else:
        try:
            feedback.record_run(*run_args, **run_kwargs)
        except Exception as exc:
            adapters.record_ledger(
                d["agent"],
                count=0,
                cost_usd=0.0,
                event="telemetry_error",
                run_id=run_id,
                target=d["target"],
                mode=d.get("mode"),
                model=d.get("model"),
                task_type=d.get("task_type"),
                log_file=str(logf),
                started_ts=started_ts,
                error_hash=hashlib.sha256(str(exc).encode()).hexdigest(),
            )
            if lineage_required:
                raise RuntimeError(
                    f"refusing dispatch without required completion lineage for {run_id}"
                ) from exc
    for rejected_role_run_id in d.get("rejected_role_run_ids") or []:
        feedback.record_influence_edge(
            target_run_id=run_id,
            influence_type="role",
            influence_id=rejected_role_run_id,
            source_run_id=rejected_role_run_id,
            accepted=False,
            metadata={"status": "rejected", "disagreement": True},
        )
    if d.get("profile_decision"):
        # Profile telemetry is causal routing evidence, not best-effort logging.
        # Refuse to start if the replayable envelope cannot be retained.
        feedback.record_profile_decision(d["profile_decision"])
    # SAME INVARIANT AS THE OFFLOAD PATH, applied here before this path can grow the same defect.
    # It is latent today (no caller supplies `selected_profile_id`, so there are 0 worker attempts
    # on non-offload runs) -- but the shape is identical, and gemini, cursor and vibe all dispatch
    # through here, so the first caller to assign them a profile would start writing rows that can
    # never resolve. Fixing one path and leaving its twin is how this defect comes back.
    if selected_profile and not adapters.can_report_cli_identity(d["agent"])[0]:
        print(
            f"note: {d['agent']} keeps profile {selected_profile['profile_id']} but records no "
            f"worker attempt ({adapters.can_report_cli_identity(d['agent'])[1]})",
            file=sys.stderr,
        )
    elif selected_profile:
        feedback.record_execution_attempt(
            run_id,
            attempt_id=profile_attempt_id,
            operation_role="worker",
            profile_id=selected_profile["profile_id"],
            requested_provider=selected_profile["provider"],
            requested_model=selected_profile["requested_model"],
            status="started",
            source="orchestrator-profile-decision",
            started_ts=started_ts,
        )
        if d.get("profile_decision"):
            feedback.attach_profile_attempt_to_decision(
                d["profile_decision"]["decision_id"], profile_attempt_id
            )
    adapters.record_ledger(
        d["agent"],
        count=1,
        cost_usd=0.0,
        event="start",
        run_id=run_id,
        target=d["target"],
        mode=d.get("mode"),
        model=d.get("model"),
        task_type=d.get("task_type"),
        log_file=str(logf),
        started_ts=started_ts,
        selected_profile_id=d.get("selected_profile_id"),
        requested_model=d.get("requested_model"),
        policy_version=d.get("profile_policy_version"),
        propensity=d.get("profile_assignment_probability"),
    )
    complete_cmd = shlex.join(
        [
            "python3",
            str(ORCH_DIR / "ledger_reconcile.py"),
            "complete",
            "--run-id",
            run_id,
            "--agent",
            d["agent"],
            "--target",
            d["target"],
            "--mode",
            str(d.get("mode") or ""),
            "--task-type",
            str(d.get("task_type") or ""),
            "--log-file",
            str(logf),
            "--started-ts",
            str(started_ts),
            "--selected-profile-id",
            str(d.get("selected_profile_id") or ""),
            "--requested-model",
            str(d.get("requested_model") or ""),
            "--policy-version",
            str(d.get("profile_policy_version") or ""),
            "--propensity",
            str(d.get("profile_assignment_probability") or 0.0),
        ]
    )
    # Marker BEFORE the python completion: the python step gets SIGKILLed in the wild (audit F2);
    # the microsecond printf survives and ledger_reconcile backfills latency/exit from it.
    marker_cmd = adapters.done_marker_cmd(run_id, logf, "orch_dispatch_rc")
    wrapped = (
        f"{d['wrapped']}; orch_dispatch_rc=$?; {marker_cmd}; {complete_cmd}; exit $orch_dispatch_rc"
    )
    with logf.open("a") as fh:
        fh.write(
            f"=== {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} dispatch "
            f"{d['agent']}/{d['mode']} -> {d['target']} [{d['task_type']}] "
            f"cwd={d['cwd']} run_id={run_id} ===\n"
        )
        try:
            proc = subprocess.Popen(
                ["bash", "-lc", wrapped],
                cwd=d["cwd"],
                stdout=fh,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
        except Exception as exc:
            # Only close what was opened -- an unreportable seat has no attempt row to complete.
            if selected_profile and adapters.can_report_cli_identity(d["agent"])[0]:
                feedback.complete_profile_attempt_unresolved(
                    run_id,
                    selected_profile_id=selected_profile["profile_id"],
                    fallback_reason="profile_process_start_failed",
                    status="failed",
                    completed_ts=int(time.time()),
                )
            adapters.record_ledger(
                d["agent"],
                count=0,
                cost_usd=0.0,
                event="complete",
                run_id=run_id,
                target=d["target"],
                selected_profile_id=d.get("selected_profile_id"),
                requested_model=d.get("requested_model"),
                exit=126,
                error=f"process start failed: {exc}",
            )
            raise
    try:
        stamped = claims.update_metadata(
            d["target"],
            d["agent"],
            refresh_ts=True,
            pid=proc.pid,
            log=str(logf),
            worktree=d.get("cwd"),
            lane=d.get("lane"),
            task_type=d.get("task_type"),
            mode=d.get("mode"),
            run_id=run_id,
            started_ts=started_ts,
        )
        if not stamped:
            with logf.open("a") as fh:
                fh.write(
                    "warn: claim metadata update returned false; redirect_sweep may use original claim metadata\n"
                )
    except Exception as exc:
        with logf.open("a") as fh:
            fh.write(f"warn: claim metadata update failed: {exc}\n")
    return proc.pid


def run(decision: dict, *, dry_run: bool = False, heartbeat: bool = True) -> dict:
    if heartbeat and not dry_run:
        write_heartbeat()
    launched, skipped = [], []
    for a in decision.get("assignments", []):
        d = plan_dispatch(a, dry_run=dry_run)
        if d is None:
            skipped.append({"assignment": a, "reason": "unknown agent / unbuildable"})
            continue
        if "error" in d:
            skipped.append({"assignment": a, "reason": d["error"]})
            continue
        if dry_run:
            launched.append({**d, "pid": None})
        else:
            d["pid"] = _spawn(d)
            launched.append(d)
    return {"dry_run": dry_run, "launched": launched, "skipped": skipped, "count": len(launched)}


def delegate(
    agent: str,
    target: str,
    lane: str,
    prompt: str,
    mode: str | None = None,
    task_type: str = "implement",
    profile_id: str | None = None,
    *,
    influenced_by_role_run_ids=None,
    influenced_by_skill_event_ids=None,
    influenced_by_workflow_ids=None,
    capability_ids=None,
    capability_version_ids=None,
    acceptance_gate_ids=None,
) -> dict:
    """One ad-hoc delegation for the assessing orchestrator seat: claim + provision + spawn
    ONE cheap agent with the orchestrator's OWN crafted prompt (detached, PATH+auth+release
    wrapper). Returns {pid, log, worktree} to monitor, or {error}. This is the seat's hand —
    it decides WHO/WHAT/HOW (the prompt); this just executes safely. `task_type` is recorded for
    the feedback loop (the REAL kind of work, not a generic 'delegated')."""
    if mode is None:
        mode = "composer" if agent == "cursor" else "full"
    claims.reap_stale()
    if not claims.claim(target, agent):
        h = claims.holder(target)
        return {"error": f"target already claimed by {h.get('agent') if h else 'another agent'}"}
    a = {
        "agent": agent,
        "target": target,
        "lane": lane,
        "task_type": task_type,
        "mode": mode,
        "prompt": prompt,
        "feedback_mode": "local",
        "influenced_by_role_run_ids": list(influenced_by_role_run_ids or []),
        "influenced_by_skill_event_ids": list(influenced_by_skill_event_ids or []),
        "influenced_by_workflow_ids": list(influenced_by_workflow_ids or []),
        "capability_ids": list(capability_ids or []),
        "capability_version_ids": list(capability_version_ids or []),
        "acceptance_gate_ids": list(acceptance_gate_ids or []),
    }
    if profile_id:
        profile = execution_profiles.get_profile(profile_id)
        a.update(
            {
                "selected_profile_id": profile_id,
                "requested_model": profile["requested_model"],
                "reasoning_effort": profile["reasoning_effort"],
                "permission_mode": profile["permission_mode"],
                "transport": "local",
                "profile_policy_version": execution_profiles.PROFILE_POLICY_VERSION,
                "profile_assignment_probability": 1.0,
            }
        )
    d = plan_dispatch(a, dry_run=False)
    if d is None:
        claims.release(target, agent)
        return {"error": f"unknown/unbuildable agent: {agent}"}
    if "error" in d:
        claims.release(target, agent)
        return d
    safe = target.replace("/", "__").replace("#", "_")
    d["pid"] = _spawn(d)
    # Close the feedback loop for LOCAL delegations: re-record the decision keyed by mode='local'
    # (so outcomes.ingest_outcomes(mode='local') discovers it and resolves the resulting PR by the
    # deterministic orchestrator/issue-N branch) with the REAL task_type; keep the agent-mode in
    # reasoning_level. INSERT OR REPLACE overwrites the generic row _spawn just wrote. Without this,
    # local-agent delegations never get a success/failure signal — only remote ones did.
    if not profile_id:
        try:
            feedback.record_run(
                d["run_id"],
                target,
                task_type,
                agent,
                mode="local",
                reasoning_level=mode,
                model=d.get("model"),
                routing_metadata=d.get("routing_metadata"),
                influenced_by_role_run_ids=d.get("influenced_by_role_run_ids"),
                influenced_by_skill_event_ids=d.get("influenced_by_skill_event_ids"),
                influenced_by_workflow_ids=d.get("influenced_by_workflow_ids"),
                capability_ids=d.get("capability_ids"),
                capability_version_ids=d.get("capability_version_ids"),
                acceptance_gate_ids=d.get("acceptance_gate_ids"),
            )
        except Exception:
            pass
    return {
        "pid": d["pid"],
        "log": str(DISPATCH_LOG_DIR / f"{safe}.{agent}.log"),
        "worktree": d["cwd"],
        "agent": agent,
        "target": target,
        "mode": mode,
        "run_id": d["run_id"],
    }


def _default_offload_timeout(agent: str, requested: int | None) -> int:
    if requested is not None:
        return requested
    if agent == "gemini":
        return DEFAULT_GEMINI_OFFLOAD_TIMEOUT
    return DEFAULT_OFFLOAD_TIMEOUT


def _gemini_workspace_prompt(prompt: str, cwd: str | Path) -> str:
    """Pin Gemini's file operations to the orchestrator-provisioned workspace.

    agy has previously narrated success while writing outside the experiment
    worktree. The adapter's absolute --add-dir is the enforcement mechanism;
    this instruction keeps the model from intentionally choosing another
    checkout that happens to contain the same repo.
    """
    workspace = Path(cwd).expanduser().resolve()
    return (
        f"{prompt.rstrip()}\n\n"
        f"GEMINI WORKSPACE: use exactly {workspace} for all file reads, edits, tests, "
        "and shell commands. Do not read from or modify any other checkout of this repo, "
        "including Dropbox/CloudStorage checkouts or the process launch directory if it differs."
    )


def _align_gemini_print_timeout(argv: list[str], timeout: int) -> None:
    """Keep agy's print wait below the outer subprocess timeout for offloads."""
    if "--print-timeout" not in argv:
        return
    budget = max(1, timeout - 15)
    minutes = max(1, (budget + 59) // 60)
    argv[argv.index("--print-timeout") + 1] = f"{minutes}m"


def _argv_flag_value(argv: list[str], flag: str) -> str | None:
    try:
        return argv[argv.index(flag) + 1]
    except (ValueError, IndexError):
        return None


def _agent_log_tail_from_argv(argv: list[str], cwd: str | Path, *, max_chars: int = 2400) -> str:
    """Read a bounded agent log tail when the CLI hides the real error from stdout/stderr."""
    log_file = _argv_flag_value(argv, "--log-file")
    if not log_file:
        return ""
    path = Path(log_file).expanduser()
    if not path.is_absolute():
        path = Path(cwd) / path
    try:
        return path.read_text(errors="replace")[-max_chars:]
    except OSError:
        return ""


def _capability_heartbeat(event_type: str, *, agent: str, mode: str | None) -> None:
    """Record that the `offload` transport capability ran, at the code path where it executes.

    Matches the lazy-import idiom used by the 12 sibling lane/infra modules: `capabilities` imports
    `feedback`, and modules in this cluster are imported BY `capabilities`' dependencies, so a
    module-level import risks a cycle. Never raises — recording use must not be able to prevent the
    work — and inert outside an active tick via ORCH_CAPABILITY_HEARTBEATS.

    HISTORY (2026-08-19): this call previously referenced `capabilities.production_heartbeat`
    without any import of `capabilities` in this module, so it raised NameError on EVERY offload and
    the surrounding `except Exception: pass` swallowed it. Result: zero recorded invocations across
    ~196 offloads/week, and an inventory that read `no_matching_work` for a capability in constant
    use. dispatcher.py was the only module in the cluster not using this helper form, which is
    precisely why it was the only one silently broken. `_selftest` now asserts the call is REACHED,
    not merely that it does not raise.
    """
    try:
        import capabilities

        capabilities.production_heartbeat(
            "offload", event_type, ref=f"{agent}:{mode}", metadata={"agent": agent, "mode": mode}
        )
    except Exception:
        pass


def _select_offload_profile(agent: str, mode: str | None) -> dict | None:
    """Deterministically pick an execution profile for an offload, or None if none applies.

    Uses the same `select_profile` contract the router uses, so the choice is replayable and the
    routing decision is recorded rather than implicit. Returns None -- leaving behaviour exactly as
    it was -- when the agent has no profile supporting the offload transport, so this cannot change
    how an unprofiled seat runs.

    Deliberately NOT exploration: an offload is a read, and its profile choice should be stable for
    the same agent so the resulting worker attempts accumulate against one identity instead of
    smearing across three.
    """
    try:
        profiles = execution_profiles.profiles_for_agent(agent, transport="offload")
        if not profiles:
            return None
        # HONOUR THE TIER THE CODEBASE ALREADY CHOSE. `DEFAULT_OFFLOAD_TIER` is "mid" with a comment
        # that had already diagnosed this exact waste -- "a codex offload burned Sol and a gemini
        # offload burned Pro" -- and selecting from ALL offload-capable profiles silently overrode
        # it: with only a Pro profile registered, every gemini read was routed to the reasoning
        # tier, and with three codex profiles it picked whichever sorted first.
        #
        # An offload is an advisory READ. Where the seat has a tier ladder, take the rung the ladder
        # names for that tier; a single-lane seat (cursor, vibe, aider) has no ladder and keeps its
        # one profile. Falling back to the full candidate set means a seat whose tier model has no
        # registered profile behaves exactly as before rather than losing its profile entirely.
        tier_model = adapters.resolve_model(agent, adapters.DEFAULT_OFFLOAD_TIER)
        tiered = [p for p in profiles if tier_model and p["requested_model"] == tier_model]
        candidate_ids = sorted(profile["profile_id"] for profile in (tiered or profiles))
        envelope = execution_profiles.select_profile(
            "offload",
            None,
            candidate_ids,
            rng_seed=0,
            scores={pid: 0.0 for pid in candidate_ids},
            exploration=False,
            exploration_policy="deterministic-offload-profile",
        )
        selected = envelope.get("selected_profile_id")
        return execution_profiles.get_profile(selected) if selected else None
    except Exception as exc:  # provenance must never prevent the work
        print(f"warn: offload profile selection failed for {agent}: {exc}", file=sys.stderr)
        return None


def offload(
    agent: str,
    prompt: str,
    cwd: str = ".",
    mode: str | None = None,
    timeout: int | None = None,
    isolate: bool = False,
    profile_id: str | None = None,
    research_round: str | None = None,
) -> dict:
    """SYNCHRONOUS offload for token conservation.

    By default this runs in `cwd`. With isolate=True, it first copies `cwd` to a persistent local
    offload workspace so multiple code-building offloads can run in parallel without same-dir races.
    The isolated result is NOT auto-merged; the orchestrator reviews and integrates deliberately.

    Runs a cheaper agent and RETURNS its output to the orchestrating seat — no claim, no PR.
    This is how the seat offloads token-heavy READING (e.g. summarize 200 pages →
    gemini's huge context) and gets back only the result, spending the cheap agent's capacity
    instead of its own. Records a ledger row (it consumes the agent's budget).

    `research_round` binds this offload to a multi-agent research round (see
    `research_subjects.record_research_round`). An audit or study that fans work out to several
    agents is comparable evidence, but only if the runs carry the round as their experiment_id:
    without it each agent is an unrelated run against an ephemeral temp path, which is why
    thousands of offload runs across six agents produced nothing the learner could compare."""
    # KILL SWITCH. Added 2026-08-21 because the admission gate was literally right: nothing could
    # stop the fleet's most-used capability (~196 runs/week) without a code change. That is not
    # theoretical -- on 2026-08-08 the gemini model pin rotted and EVERY offload to that seat exited
    # 1 while `capacity.py` still reported `state: ok`; the only lever was per-seat capacity
    # shedding, which is a shed, not a stop. Checked FIRST, before provisioning, model resolution or
    # any ledger write, so a disabled offload spends nothing at all.
    if os.environ.get("ORCH_OFFLOAD_DISABLED", "").strip() == "1":
        return {
            "error": "offload disabled by ORCH_OFFLOAD_DISABLED=1",
            "agent": agent,
            "disabled": True,
            "exit": None,
            "run_id": None,
        }
    if mode is None:
        # Offloads are advisory READS, but this defaulted to 'full' for years — so a codex offload
        # burned Sol (flagship) and a gemini offload burned 3.1 Pro, contradicting the "runs a
        # cheaper agent" contract above. The mid tier is the right home for read/summarize work;
        # ORCH_OFFLOAD_TIER overrides. Cursor stays on Composer: it has no verified tier pins
        # (CLI unauthenticated) and frontier draws the metered mid-tier pool (LOCAL_POLICY.md). (2026-08-08)
        mode = "composer" if agent == "cursor" else adapters.DEFAULT_OFFLOAD_TIER
    # Infrastructure capabilities are never ROUTED to — they run as part of the work — so they
    # record use at their own code path rather than through a matcher. Inert outside an active
    # tick (ORCH_CAPABILITY_HEARTBEATS), so tests and manual runs stay silent. Never allowed to
    # raise: recording that a capability ran must not be able to prevent the work. (2026-08-09)
    _capability_heartbeat("invocation", agent=agent, mode=mode)
    timeout = _default_offload_timeout(agent, timeout)
    # DELIBERATELY no write_heartbeat() here. The heartbeat makes the legacy opener/closer cron lanes
    # YIELD for 15 min (handoff-prerun.sh yield-guard), which is only warranted for fleet-MUTATING
    # dispatch — run()/delegate(), orchestrate.sh --active, orchestrate-seat.sh. An offload is read-only
    # w.r.t. the fleet (no claim, no PR, no label), so there is nothing to double-dispatch and no reason
    # to freeze the autopilot. The self-heartbeat briefly added 2026-06-20 was REVERTED: its premise (a
    # concurrent cron tick starves the offload to a 0-byte timeout) was disproven — the 0-byte hang was
    # an inherited *_PROXY (see _net_hygiene_prelude), and concurrent agent CLI runs do NOT serialize. A
    # driving seat already owns its heartbeat via orchestrate-seat.sh; a standalone/library offload
    # (e.g. repo-audit) must not silently halt opener+closer. Do not re-add without a fleet-mutation reason.
    try:
        source_cwd = Path(cwd).expanduser().resolve()
        run_cwd = _isolate_offload_cwd(source_cwd) if isolate else source_cwd
    except Exception as exc:
        return {"agent": agent, "exit": 2, "output": "", "error": str(exc)}
    proc_cwd = source_cwd if agent == "gemini" and isolate else run_cwd
    if agent == "gemini" and isolate:
        prompt = (
            f"{prompt.rstrip()}\n\n"
            f"GEMINI ISOLATED WORKSPACE: use {run_cwd} as the workspace for all file reads, "
            f"file edits, tests, and shell commands. Do not modify the process cwd {source_cwd}; "
            "it is only used as Antigravity's launch directory; project/app data is redirected "
            "under the Orchestrator runtime."
        )
    prepared_prompt = _offload_prompt(prompt, run_cwd, agent)
    profile = execution_profiles.get_profile(profile_id) if profile_id else None
    if profile is None:
        # SELECT one when the caller did not name it. This single line is why no worker execution
        # attempt had ever been recorded: the worker-attempt write below is guarded on `profile`,
        # `profile` came only from an explicit `profile_id`, and no caller passed one -- so 1,488
        # offload runs produced zero provenance, every completion event failed
        # `unresolved_model_provenance`, and no episode could ever be complete.
        #
        # Agents with no profile registered for this transport are UNCHANGED: the selection is
        # skipped entirely, so nothing that worked before starts behaving differently.
        profile = _select_offload_profile(agent, mode)
    # One name for the profile that is ACTUALLY in force, so the ledger, the routing metadata and
    # the worker attempt cannot disagree about which profile ran.
    effective_profile_id = profile["profile_id"] if profile else None
    # WHY THIS ROW WOULD EXIST, resolved once per offload. Two different reasons justify a worker
    # attempt, and keying on only one of them was wrong in both directions:
    #
    #   * the run is EVIDENCE -- bound to a registered research round, where the attempt is the
    #     join that lets the round become a minable episode. True for any seat, because the round's
    #     arms are whichever agents actually did the work.
    #   * the seat can RESOLVE -- its CLI records what served the run, so the row carries real model
    #     provenance and feeds drift detection. True for any run, round-bound or not.
    #
    # Gating on `can_report` alone (as first written) silenced exactly the case this is for: a
    # gemini- or cursor-armed audit round could no longer produce an episode at all, which broke the
    # round-registration path built for it hours earlier. Gating on nothing produced the junk: an
    # unbound production offload by a seat that can never resolve leaves a row asserting the one
    # thing it cannot establish, 230 times per two days, with nothing to decrement it.
    #
    # The pair is the predicate. An unbound offload by an unreportable seat is the only case that
    # writes nothing, and it is the only case where the row could never be used for anything.
    can_report, no_report_reason = adapters.can_report_cli_identity(agent)
    is_evidence = bool(research_round)
    record_worker_attempt = can_report or is_evidence
    # Set from the run's OWN stream-json output when the tool reports it; the completion path below
    # prefers it over any store probe. Declared here so both live in the same scope.
    observed_model: str | None = None
    argv = (
        adapters.build_command(
            agent,
            prepared_prompt,
            mode,
            cwd=run_cwd,
            profile=profile,
            transport="offload",
        )
        if profile
        else adapters.build_command(agent, prepared_prompt, mode, cwd=run_cwd, transport="offload")
    )  # raises ValueError on unknown agent
    if agent == "gemini" and "--add-dir" in argv:
        argv[argv.index("--add-dir") + 1] = str(run_cwd)
        _align_gemini_print_timeout(argv, timeout)
    # A PER-RUN agy LOG, because agy reports its model there and nowhere else. Its structured output
    # carries only conversation_id/cwd/usage, but its log says
    # `Propagating selected model override to backend: label="..."`. The default `--log-file` is one
    # SHARED path for every gemini run, which is unattributable for exactly the reason cursor's
    # reused `/private/tmp` was: a line in a shared file belongs to no particular run. Rewriting the
    # value here follows the `--add-dir` precedent directly above and needs no signature change.
    # Both of these must be settled BEFORE `wrapped` is composed, because `wrapped` freezes argv
    # into a shell string: an argv edit after that point changes nothing that runs. The first
    # version of this rewrite sat below and was therefore inert -- the run still wrote to the shared
    # log, and the only symptom was a model that never resolved.
    DISPATCH_LOG_DIR.mkdir(parents=True, exist_ok=True)
    logf = DISPATCH_LOG_DIR / f"offload.{agent}.{time.time_ns()}.log"
    agy_log: Path | None = None
    if agent == "gemini" and "--log-file" in argv:
        agy_log = logf.with_suffix(".agy.log")
        argv[argv.index("--log-file") + 1] = str(agy_log)
    auth_prelude = _auth_prelude(agent)
    agent_prelude = _agent_runtime_prelude(agent)
    wrapped = (
        f"{_path_prefix()}; {_net_hygiene_prelude()}{agent_prelude}{auth_prelude}{shlex.join(argv)}"
    )
    run_id = f"offload:{agent}:{time.time_ns()}"
    target = f"offload:{run_cwd}"
    task_type = "offload"
    model = adapters.model_identity(agent, mode, profile)
    started_ts = int(time.time())
    adapters.record_ledger(
        agent,
        count=1,
        cost_usd=0.0,
        event="start",
        run_id=run_id,
        target=target,
        mode=mode,
        model=model,
        task_type=task_type,
        log_file=str(logf),
        started_ts=started_ts,
        # THE EFFECTIVE profile, not the caller's argument. `profile_id` is the parameter, which is
        # None whenever the profile was chosen internally -- i.e. always, since no caller passes
        # one. So the ledger recorded `selected_profile_id: null` beside `propensity: 1.0` and a
        # policy version, describing a decision with no subject, and `reconcile` builds its
        # `profile_ids` set from exactly this field: that is why the branch which resolves a worker
        # attempt could never fire (250 marker backfills, 0 resolved, every run). The attempt row
        # below always used `profile["profile_id"]`, so the run disagreed with itself.
        selected_profile_id=effective_profile_id,
        requested_model=profile.get("requested_model") if profile else None,
        policy_version=execution_profiles.PROFILE_POLICY_VERSION if profile else None,
        propensity=1.0 if profile else None,
    )

    def _record_offload_run() -> None:
        feedback.record_run(
            run_id,
            target,
            task_type,
            agent,
            mode="offload",
            experiment_id=research_round or None,
            reasoning_level=(profile.get("reasoning_effort") if profile else mode),
            model=model,
            routing_metadata=(
                {
                    "selected_profile_id": effective_profile_id,
                    "requested_model": profile.get("requested_model"),
                    "transport": "offload",
                    "profile_policy_version": execution_profiles.PROFILE_POLICY_VERSION,
                    "profile_assignment_probability": 1.0,
                }
                if profile
                else None
            ),
        )
        # A WORKER ATTEMPT IS A PROVENANCE RECORD, so only a seat that can supply provenance may
        # write one. Recording it for every profiled seat produced rows asserting the single thing
        # they can never establish: cursor wrote 23 in five hours, each completed `unresolved` with
        # the same prose reason re-cached per row, and at ~230 offloads every two days it never
        # stops. Nothing decrements that -- it is an unbounded backlog whose drain is the very
        # capability the seat lacks, and it dragged `resolved_model_coverage` for the profile to a
        # permanent 0.00 over a denominator that only grows.
        #
        # The profile still applies: it is what puts the bare vendor id on the command line, and the
        # ledger row and routing metadata still record which profile was in force, so the request
        # stays fully auditable. What is withheld is only the claim we cannot back.
        #
        # Not a silent skip. The reason is a static property of the seat held in ONE place
        # (`adapters.can_report_cli_identity`), reported by `mining_coverage` per seat, so adding a
        # reader for a seat flips it on and the attempts begin -- that is the drain, and it needs
        # none of the existing rows cleared first.
        if profile and record_worker_attempt:
            feedback.record_execution_attempt(
                run_id,
                attempt_id=f"attempt:profile:{run_id}",
                operation_role="worker",
                profile_id=profile["profile_id"],
                requested_provider=profile["provider"],
                requested_model=profile["requested_model"],
                status="started",
                source="orchestrator-profile-decision",
                started_ts=started_ts,
            )
        elif profile:
            print(
                f"note: {agent} offload keeps its profile but records no worker attempt "
                f"({no_report_reason}; not bound to a research round)",
                file=sys.stderr,
            )

    if profile:
        _record_offload_run()  # fail closed before subprocess execution
    else:
        try:
            _record_offload_run()
        except Exception:
            pass
    with logf.open("a") as fh:
        fh.write(
            f"=== {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} offload "
            f"{agent}/{mode} cwd={run_cwd} process_cwd={proc_cwd} timeout={timeout}s "
            f"run_id={run_id} ===\n"
        )
    max_network_retries = max(0, _env_int("ORCH_OFFLOAD_NETWORK_RETRIES", 1))
    retry_backoff_s = max(0.0, _env_float("ORCH_OFFLOAD_RETRY_BACKOFF_S", 3.0))
    complete_written = False

    def _record_complete(exit_code: int | None = None, error: str | None = None) -> None:
        nonlocal complete_written
        if complete_written:
            return
        adapters.record_ledger(
            agent,
            count=0,
            cost_usd=0.0,
            event="complete",
            run_id=run_id,
            target=target,
            mode=mode,
            task_type=task_type,
            log_file=str(logf),
            started_ts=started_ts,
            exit=exit_code,
            error=error,
            selected_profile_id=profile_id,
            requested_model=profile.get("requested_model") if profile else None,
            policy_version=execution_profiles.PROFILE_POLICY_VERSION if profile else None,
            propensity=1.0 if profile else None,
        )
        # Only close what was opened -- reaching for a row deliberately not created is how a
        # "stop recording this" change quietly becomes a crash.
        if profile and record_worker_attempt:
            # RESOLVE HERE, NOT IN A LATER SWEEP. This closed every attempt `unresolved` on the
            # grounds that the CLI's completion output carries no model -- true of stdout, but the
            # CLI has just written its OWN session record, and this is the moment we know the agent,
            # the workspace and the run window. Resolving post-hoc instead left every attempt
            # stranded until a sweep noticed, made that sweep the mechanism rather than a backstop,
            # and let a row sit unresolved forever if the sweep never ran.
            #
            # Still never a fallback to the requested model: a seat whose store does not name what
            # served closes unresolved with the reason, exactly as before.
            # The run's own report wins: it is exact for THIS run, where a store probe has to
            # match a workspace and a window and can pick a neighbour's session.
            probe = (
                {"model": observed_model, "reason": None}
                if observed_model
                else adapters.cli_reported_model(agent, run_cwd, started_ts=started_ts)
            )
            if probe.get("model"):
                feedback.complete_profile_attempt(
                    run_id,
                    selected_profile_id=profile["profile_id"],
                    resolved_provider=profile["provider"],
                    resolved_model=probe["model"],
                    completed_ts=int(time.time()),
                )
            else:
                feedback.complete_profile_attempt_unresolved(
                    run_id,
                    selected_profile_id=profile["profile_id"],
                    fallback_reason=(
                        f"resolved_model_not_reported_by_offload:"
                        f"{probe.get('reason') or 'unknown'}"
                    )[:200],
                    completed_ts=int(time.time()),
                )
        # Instant meter (2026-07-03 audit F2): don't wait a day for ledger_reconcile to pair the
        # ndjson events — the offload runs synchronously, so latency is known RIGHT NOW. Write the
        # costs row immediately; the daily reconcile recomputes the same values (INSERT OR REPLACE,
        # idempotent) and richer sources (ccusage/langsmith) still take precedence at their passes.
        try:
            feedback.record_cost(
                run_id,
                latency_s=float(max(0, int(time.time()) - started_ts)),
                source="ledger",
            )
        except Exception:
            pass  # metering must never fail the offload itself
        complete_written = True

    attempt_stderr = ""

    def _run_offload_attempt() -> dict:
        # `nonlocal`, because the model this attempt observes must reach `_record_complete` in the
        # ENCLOSING scope. Without it the assignment below made a local that the completion never
        # saw: the parse found `composer-2.5` and the attempt still closed `unresolved`, which is
        # the most deceptive possible failure -- the reader worked and the row said it had not.
        nonlocal observed_model
        nonlocal attempt_stderr
        attempt_stderr = ""
        try:
            # stdin=DEVNULL (matches _spawn): never let an agent CLI block reading an inherited pipe/TTY.
            proc = subprocess.run(
                ["bash", "-lc", wrapped],
                cwd=str(proc_cwd),
                capture_output=True,
                text=True,
                timeout=timeout,
                stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired:
            # A 0-byte timeout is the env-induced-hang signature; surface the ambient network env so the
            # cause is legible (proxy is scrubbed by default — if one shows here, ORCH_KEEP_PROXY=1 is set).
            suspicious = _suspicious_net_env()
            hint = f" | ambient net env at dispatch: {'; '.join(suspicious)}" if suspicious else ""
            error = f"timed out after {timeout}s{hint}"
            agent_log_tail = _agent_log_tail_from_argv(argv, proc_cwd) if agent == "gemini" else ""
            if agent_log_tail:
                error = f"{error}; agy log tail captured"
            with logf.open("a") as fh:
                fh.write(f"[orchestrator] offload marked failed: timed out after {timeout}s\n")
                if suspicious:
                    fh.write(
                        "Suspect inherited env (proxy/CA/NODE_OPTIONS):\n  "
                        + "\n  ".join(suspicious)
                        + "\n"
                    )
                if agent_log_tail:
                    fh.write("[agent log tail]\n")
                    fh.write(agent_log_tail)
                    if not agent_log_tail.endswith("\n"):
                        fh.write("\n")
            out = {
                "agent": agent,
                "exit": 124,
                "output": "",
                "error": error,
                "cwd": str(run_cwd),
                "process_cwd": str(proc_cwd),
                "isolated_cwd": str(run_cwd) if isolate else None,
                # Surfaced so a CALLER can record which model it spent. Without this a role run
                # had no way to record one at all: 450 local role runs carried model=NULL, i.e.
                # unattributable spend. This is a telemetry field, NOT a provenance claim -- it
                # may be a synthetic adapter tag, so it must never be read as a
                # provider-resolved identity for a worker attempt.
                "model": model,
                "run_id": run_id,
                "log": str(logf),
            }
            if agent_log_tail:
                out["agent_log_tail"] = agent_log_tail
            return out
        except OSError as exc:
            return {
                "agent": agent,
                "exit": 126,
                "output": "",
                "error": f"process start failed: {exc}",
                "cwd": str(run_cwd),
                "process_cwd": str(proc_cwd),
                "isolated_cwd": str(run_cwd) if isolate else None,
                "run_id": run_id,
                "log": str(logf),
            }
        except KeyboardInterrupt:
            error = "interrupted by orchestrator"
            _record_complete(exit_code=130, error=error)
            with logf.open("a") as fh:
                fh.write(f"[orchestrator] offload marked failed: {error}\n")
            raise
        attempt_stderr = proc.stderr or ""
        # THE RUN REPORTED ITS OWN MODEL. When the transport asked for stream-json, the tool's
        # `system/init` event names the model that actually served -- `gen_ai.response.model` in
        # OpenTelemetry's terms, as opposed to the `--model` we requested. Read it here, from THIS
        # run's stdout, so there is no workspace to match and no time window to guess: the reason
        # all 24 cursor offloads were unresolvable is that they shared `/private/tmp`, so no session
        # in cursor's own store was attributable to any single one of them.
        #
        # The stream is then reduced to its final result text before anyone sees it, so every
        # existing caller keeps the plain-text contract it already expects.
        # agy reports in its LOG, not its stdout, so read the per-run log this run was given.
        if agy_log is not None and observed_model is None:
            try:
                agy_text = agy_log.read_text(errors="replace")
            except OSError:
                agy_text = ""
            agy_label = adapters.model_label_from_agy_log(agy_text)
            if agy_label:
                observed_model = adapters.model_id_for_label(agent, agy_label)
        if proc.stdout and '"subtype":"init"' in proc.stdout:
            stream = adapters.observed_model_from_stream(proc.stdout)
            label = stream.get("model_label")
            if label:
                observed_model = adapters.model_id_for_label(agent, label)
            if stream.get("result_text") is not None:
                proc = subprocess.CompletedProcess(
                    getattr(proc, "args", argv),
                    proc.returncode,
                    stream["result_text"],
                    proc.stderr,
                )
        with logf.open("a") as fh:
            if proc.stdout:
                fh.write(proc.stdout)
                if not proc.stdout.endswith("\n"):
                    fh.write("\n")
            if proc.stderr:
                fh.write("[stderr]\n")
                fh.write(proc.stderr)
                if not proc.stderr.endswith("\n"):
                    fh.write("\n")
        raw_exit = proc.returncode
        error = None
        exit_code = raw_exit
        if raw_exit != 0:
            error = f"agent exited {raw_exit}"
        else:
            incomplete = _offload_incomplete_reason(
                proc.stdout or "", progress_only=(agent == "gemini")
            )
            if incomplete:
                error = incomplete
                exit_code = 70
        agent_log_tail = ""
        if error and agent == "gemini":
            agent_log_tail = _agent_log_tail_from_argv(argv, proc_cwd)
            if agent_log_tail and error == "agent returned no stdout":
                error = "agent returned no stdout; agy log tail captured"
        if error:
            with logf.open("a") as fh:
                fh.write(f"[orchestrator] offload marked failed: {error}\n")
                if agent_log_tail:
                    fh.write("[agent log tail]\n")
                    fh.write(agent_log_tail)
                    if not agent_log_tail.endswith("\n"):
                        fh.write("\n")
        out = {
            "agent": agent,
            "exit": exit_code,
            "output": (proc.stdout or "").strip(),
            "model": model,
            "stderr_tail": (proc.stderr or "")[-800:],
            "cwd": str(run_cwd),
            "process_cwd": str(proc_cwd),
            "isolated_cwd": str(run_cwd) if isolate else None,
            "run_id": run_id,
            "log": str(logf),
        }
        if agent_log_tail:
            out["agent_log_tail"] = agent_log_tail
        if raw_exit != exit_code:
            out["raw_exit"] = raw_exit
        if error:
            out["error"] = error
        return out

    out = {}
    attempts = 0
    for attempt in range(1, max_network_retries + 2):
        attempts = attempt
        out = _run_offload_attempt()
        out["attempts"] = attempts
        if out.get("exit") == 0:
            break
        if attempt > max_network_retries:
            break
        if not _is_transient_network_failure(
            out.get("error"), out.get("agent_log_tail", ""), attempt_stderr
        ):
            break
        error_for_log = out.get("error") or f"exit {out.get('exit')}"
        with logf.open("a") as fh:
            fh.write(
                f"[orchestrator] transient network failure ({error_for_log}); "
                f"retry {attempt}/{max_network_retries} after {retry_backoff_s}s\n"
            )
        if retry_backoff_s:
            time.sleep(retry_backoff_s)
    if attempts > 1:
        out["retried"] = True
    _record_complete(exit_code=out.get("exit"), error=out.get("error"))
    return out


# Agents the GitHub keepalive can run REMOTELY via an `agent:<name>` label (label-driven; runs
# reusable-<name>-run.yml on a GitHub runner — REMOTE capacity, not this machine). This is how the cron
# orchestrator drives opener/closer work cheaply: it CHOOSES the agent (its value-add) + labels; the
# keepalive executes. The choice is informed by the route-table + learned weights. See PLANNING.md.
REMOTE_AGENTS = {"cursor", "codex", "claude", "gemini"}


def _remote_label_cmd(target: str, agent: str) -> list:
    # The /issues/{n}/labels API works for BOTH issues and PRs (PRs are issues in the labels API), so the
    # opener applies agent:<X> to a ready ISSUE (LABELS.md: drives intake -> creates the PR) OR a fresh PR
    # (keepalive runs it). Same label, one command for both — resolves the issue-vs-PR gap.
    repo, num = provision.parse_target(target)
    return [
        "gh",
        "api",
        "--method",
        "POST",
        f"repos/{repo}/issues/{num}/labels",
        "-f",
        f"labels[]=agent:{agent}",
    ]


def _target_labels(target: str) -> set:
    """Live: label names on the target ISSUE or PR via the issues API (works for both). Empty set on error."""
    repo, num = provision.parse_target(target)
    r = subprocess.run(
        ["gh", "api", f"repos/{repo}/issues/{num}", "--jq", ".labels[].name"],
        capture_output=True,
        text=True,
    )
    return set(r.stdout.split()) if r.returncode == 0 else set()


def _remote_skip_reason(labels: set, agent: str) -> str | None:
    """Gate #4 rails (pure): don't fight the keepalive/delegation policy, and DON'T re-assign work already
    in the agent pipeline. Skip if the PR is paused, OR already carries ANY `agent:*` label (it's already
    assigned/in-flight — the orchestrator only remote-delegates FRESH, unassigned items; in-flight PRs are
    the delegation policy's / closer's job). Lesson from the first supervised tick: it added agent:codex on
    top of an existing agent:claude — that double-assignment is what this guards against."""
    if "agents:paused" in labels:
        return "agents:paused (lane paused — respect it)"
    assigned = sorted(l for l in labels if l.startswith("agent:"))
    if assigned:
        return f"already in agent pipeline ({','.join(assigned)}) — not re-delegating"
    return None


def delegate_remote(
    agent: str,
    target: str,
    *,
    task_type: str = "implement",
    rationale: str = "",
    dry_run: bool = False,
    labels: set | None = None,
    influenced_by_role_run_ids=None,
    influenced_by_skill_event_ids=None,
    influenced_by_workflow_ids=None,
    capability_ids=None,
    acceptance_gate_ids=None,
) -> dict:
    """Drive the REMOTE keepalive instead of a local agent: apply `agent:<agent>` to the target PR so
    GitHub runs reusable-<agent>-run.yml on a runner. The orchestrator's CHOICE of agent is the value
    (route-table + learned weights); execution + capacity are remote. Records the decision (mode=remote)
    so keepalive outcomes join the feedback loop by PR later. Cooperates with the rails (gate #4): skips
    a paused/already-owned PR. Does NOT spawn or claim locally. `labels` overrides the live lookup (tests).
    """
    if agent not in REMOTE_AGENTS:
        return {
            "error": f"'{agent}' is not a keepalive-runnable remote agent {sorted(REMOTE_AGENTS)}"
        }
    repo, num = provision.parse_target(target)
    if num is None:
        return {"error": f"remote delegation needs a PR number: {target!r}"}
    cmd = _remote_label_cmd(target, agent)
    lbls = (
        labels if labels is not None else _target_labels(target)
    )  # fetch (read-only) so dry-run shadows the rails too
    skip = _remote_skip_reason(lbls, agent)
    if dry_run:
        return {
            "target": target,
            "agent": agent,
            "label": f"agent:{agent}",
            "cmd": cmd,
            "dry_run": True,
            "skip": skip,
        }
    if skip:  # rails: respect pause / existing ownership
        return {"target": target, "agent": agent, "applied": False, "skip": skip}
    res = subprocess.run(cmd, capture_output=True, text=True)
    applied = res.returncode == 0
    try:  # decision capture — keepalive outcomes (merge/durability) join this by PR later
        feedback.record_run(
            f"remote:{repo}#{num}:{agent}",
            target,
            task_type,
            agent,
            mode="remote",
            rationale=rationale or "remote keepalive delegation via agent label",
            pr_number=num,
            model=adapters.model_identity(agent, None),
            influenced_by_role_run_ids=influenced_by_role_run_ids,
            influenced_by_skill_event_ids=influenced_by_skill_event_ids,
            influenced_by_workflow_ids=influenced_by_workflow_ids,
            capability_ids=capability_ids,
            acceptance_gate_ids=acceptance_gate_ids,
        )
    except Exception:
        pass
    return {
        "target": target,
        "agent": agent,
        "label": f"agent:{agent}",
        "applied": applied,
        "stderr": (res.stderr or "")[-300:] if not applied else "",
    }


def load_decision() -> dict:
    try:
        return json.loads(DECISION_JSON.read_text())
    except Exception:
        return {"assignments": []}


# ---------------------------------------------------------------------------
def _selftest() -> None:
    import tempfile

    tmp = tempfile.mkdtemp(prefix="dispatcher-selftest-")
    os.environ["HANDOFF_DIR"] = tmp
    # rebind module paths that were computed at import against the real HANDOFF
    global HANDOFF, HEARTBEAT_JSON, DISPATCH_LOG_DIR, OFFLOAD_DIR, AGENT_RUNTIME_DIR
    old_agent_runtime_dir = AGENT_RUNTIME_DIR
    old_feedback_db = feedback.DB_PATH
    old_adapters_handoff, old_adapters_ledger = adapters.HANDOFF, adapters.LEDGER
    HANDOFF = Path(tmp)
    HEARTBEAT_JSON = HANDOFF / "orchestrator.json"
    DISPATCH_LOG_DIR = HANDOFF / "dispatch-logs"
    OFFLOAD_DIR = HANDOFF / "offloads"
    AGENT_RUNTIME_DIR = HANDOFF / "agent-runtime"
    adapters.HANDOFF = HANDOFF  # so record_ledger writes into tmp
    adapters.LEDGER = HANDOFF / "capacity-ledger.ndjson"
    feedback.DB_PATH = HANDOFF / "feedback" / "orchestrator.db"
    try:
        for agent, persona in AGENT_PERSONAS.items():
            preamble = _agent_preamble(agent)
            assert persona in preamble and CRITICAL_EVALUATOR_DIRECTIVE in preamble, preamble
            shown = _offload_prompt("SHOW-PROMPT TASK", HANDOFF, agent)
            assert persona in shown and CRITICAL_EVALUATOR_DIRECTIVE in shown, shown

        decision = {
            "assignments": [
                {
                    "agent": "cursor",
                    "mode": "composer",
                    "target": "stranske/Repo#1",
                    "task_type": "mechanical",
                    "lane": "closer",
                    "reason": "mechanical→cursor/composer (ok) via thompson-hybrid exploration",
                    "exploration": True,
                    "exploration_mode": "thompson-hybrid",
                    "capacity_state": "ok",
                    "capacity_policy": "",
                },
                {
                    "agent": "claude",
                    "mode": "full",
                    "target": "stranske/Repo#2",
                    "task_type": "implement",
                    "lane": "opener",
                },
                {
                    "agent": "vibe",
                    "mode": "full",
                    "target": "stranske/Repo#3",
                    "task_type": "review",
                    "lane": "closer",
                },
                {
                    "agent": "bogus",
                    "mode": "full",
                    "target": "stranske/Repo#4",
                    "task_type": "mechanical",
                    "lane": "closer",
                },
            ]
        }
        out = run(decision, dry_run=True, heartbeat=False)
        assert out["count"] == 3, out  # 3 dispatchable
        assert len(out["skipped"]) == 1 and out["skipped"][0]["assignment"]["agent"] == "bogus"

        by_t = {d["target"]: d for d in out["launched"]}
        # correct agent argv from adapters
        assert by_t["stranske/Repo#1"]["argv"][0] == "cursor-agent"
        assert by_t["stranske/Repo#2"]["argv"][0] == "claude"
        assert by_t["stranske/Repo#3"]["argv"][0] == "vibe"
        assert by_t["stranske/Repo#1"]["lane"] == "closer", by_t["stranske/Repo#1"]
        assert by_t["stranske/Repo#2"]["lane"] == "opener", by_t["stranske/Repo#2"]
        # Composer is pinned by id (owner policy): omitting --model would select `auto`, which
        # routes across every frontier model cursor sells.
        assert by_t["stranske/Repo#1"]["model"] == f"cursor:{adapters.CURSOR_COMPOSER_MODEL}", by_t[
            "stranske/Repo#1"
        ]
        # Tiered since 2026-08-08: claude pins an exact model instead of a generic lane tag. The
        # seat is capped at `mid` (scarce weekly), so a 'full' assignment records Sonnet 5.
        expected_claude = adapters.MODEL_TIERS["claude"][adapters.effective_tier("claude", "full")]
        assert by_t["stranske/Repo#2"]["model"] == expected_claude, by_t["stranske/Repo#2"]
        assert by_t["stranske/Repo#1"]["routing_metadata"] == {
            "source": "router_assignment",
            "reason": "mechanical→cursor/composer (ok) via thompson-hybrid exploration",
            "exploration": True,
            "exploration_mode": "thompson-hybrid",
            "capacity_state": "ok",
        }, by_t["stranske/Repo#1"]["routing_metadata"]
        for target, agent in (("stranske/Repo#1", "cursor"), ("stranske/Repo#3", "vibe")):
            prompt_blob = " ".join(by_t[target]["argv"])
            assert AGENT_PERSONAS[agent] in prompt_blob, prompt_blob
            assert CRITICAL_EVALUATOR_DIRECTIVE in prompt_blob, prompt_blob
        # prompt content reflects task type (implement says "acceptance criteria")
        impl_argv = by_t["stranske/Repo#2"]["argv"]
        assert any("acceptance criteria" in tok for tok in impl_argv), impl_argv
        testgen = plan_dispatch(
            {
                "agent": "codex",
                "target": "o/r#8",
                "task_type": "testgen",
                "mode": "full",
                "lane": "opener",
            },
            dry_run=True,
        )
        assert any("test-generation acceptance gate" in tok for tok in testgen["argv"]), testgen[
            "argv"
        ]
        assert any("testgen_gate.py" in tok for tok in testgen["argv"]), testgen["argv"]
        epic = plan_dispatch(
            {
                "agent": "gemini",
                "target": "o/r#9",
                "task_type": "epic",
                "mode": "full",
                "lane": "opener",
            },
            dry_run=True,
        )
        assert any("epic decomposition plan" in tok for tok in epic["argv"]), epic["argv"]
        assert any("Do not implement the subtasks" in tok for tok in epic["argv"]), epic["argv"]
        epic_prompt = " ".join(epic["argv"])
        assert AGENT_PERSONAS["gemini"] in epic_prompt, epic["argv"]
        assert CRITICAL_EVALUATOR_DIRECTIVE in epic_prompt, epic["argv"]
        codemod = plan_dispatch(
            {
                "agent": "cursor",
                "target": "o/r#10",
                "task_type": "codemod",
                "mode": "composer",
                "lane": "opener",
            },
            dry_run=True,
        )
        assert any("codemod/refactor campaign" in tok for tok in codemod["argv"]), codemod["argv"]
        assert any("codemod_lane.py" in tok for tok in codemod["argv"]), codemod["argv"]
        cross_repo = plan_dispatch(
            {
                "agent": "gemini",
                "target": "o/r#11",
                "task_type": "cross_repo",
                "mode": "full",
                "lane": "opener",
            },
            dry_run=True,
        )
        assert any(
            "cross-repo coordinated-change plan" in tok for tok in cross_repo["argv"]
        ), cross_repo["argv"]
        assert any("cross_repo_lane.py" in tok for tok in cross_repo["argv"]), cross_repo["argv"]
        runtime_ac = plan_dispatch(
            {
                "agent": "gemini",
                "target": "o/r#12",
                "task_type": "runtime_ac",
                "mode": "full",
                "lane": "opener",
            },
            dry_run=True,
        )
        assert any("runtime acceptance-criteria" in tok for tok in runtime_ac["argv"]), runtime_ac[
            "argv"
        ]
        assert any("runtime_ac.py" in tok for tok in runtime_ac["argv"]), runtime_ac["argv"]
        trend = plan_dispatch(
            {
                "agent": "codex",
                "target": "stranske/Trend_Model_Project#9",
                "task_type": "implement",
                "mode": "full",
                "lane": "opener",
            },
            dry_run=True,
        )
        trend_prompt = " ".join(trend["argv"])
        assert "REPO PLAYBOOK (stranske/Trend_Model_Project)" in trend_prompt, trend["argv"]
        assert "phase-3" in trend_prompt and "ruff check" in trend_prompt, trend["argv"]
        # review prompt is advisory/non-gating
        rev_argv = by_t["stranske/Repo#3"]["argv"]
        assert any("non-gating" in tok.lower() for tok in rev_argv), rev_argv
        # wrapper prepends a PATH fix (local-bin tools) + always releases the claim afterward
        # (target is shlex-quoted for the shell, so it ends '... release <target> cursor')
        w = by_t["stranske/Repo#1"]["wrapped"]
        assert f"{REAL_HOME}/.local/bin" in w, w  # PATH fix independent of child HOME
        assert "ORCH_AGENT_RUNTIME" in w and "agent-runtime/cursor" in w, w
        assert "AGENT_CLI_CREDENTIAL_STORE=memory" in w and "CURSOR_DATA_DIR=" in w, w
        assert (
            "CURSOR_CONFIG_DIR=" in w and "NODE_COMPILE_CACHE=" in w and "export HOME=" not in w
        ), w
        assert "cursor-agent.env" in w and "$HOME/.cursor" not in w and "set -a" in w, w
        assert (
            "claims.py" in w
            and " release " in w
            and "Repo#1" in w
            and w.rstrip().endswith("cursor")
        ), w
        # net hygiene: the proxy family is unset BEFORE the agent runs (inside the subshell) so a stray
        # *_PROXY can't blackhole the agent's HTTPS (the in-session offload-hang root cause, 2026-06-20).
        assert "unset " in w and "HTTPS_PROXY" in w and "ALL_PROXY" in w, w
        assert w.index("unset ") < w.index("cursor-agent"), w
        assert (
            _net_hygiene_prelude().startswith("unset ") and "HTTPS_PROXY" in _net_hygiene_prelude()
        ), _net_hygiene_prelude()
        os.environ["ORCH_KEEP_PROXY"] = "1"
        assert (
            _net_hygiene_prelude() == ""
        ), "ORCH_KEEP_PROXY=1 must preserve the inherited proxy env"
        os.environ.pop("ORCH_KEEP_PROXY", None)
        # claude sources its oauth token; vibe needs no explicit auth source (~/.vibe auto-loads)
        assert ".claude-oauth-token" in by_t["stranske/Repo#2"]["wrapped"], by_t["stranske/Repo#2"][
            "wrapped"
        ]
        assert (
            "cursor-agent.env" not in by_t["stranske/Repo#3"]["wrapped"]
        ), "vibe needs no sourced auth file"
        assert "VIBE_HOME=" in by_t["stranske/Repo#3"]["wrapped"], by_t["stranske/Repo#3"][
            "wrapped"
        ]
        vibe_cfg = AGENT_RUNTIME_DIR / "vibe" / ".vibe" / "config.toml"
        if (REAL_HOME / ".vibe" / "config.toml").exists():
            assert (
                f'save_dir = "{AGENT_RUNTIME_DIR / "vibe" / ".vibe" / "logs" / "session"}"'
                in vibe_cfg.read_text()
            ), vibe_cfg.read_text()
            assert (
                str(REAL_HOME / ".vibe" / "logs" / "session") not in vibe_cfg.read_text()
            ), vibe_cfg.read_text()
        gemini_dispatch = plan_dispatch(
            {
                "agent": "gemini",
                "target": "o/r#8",
                "task_type": "implement",
                "mode": "full",
                "lane": "opener",
                "prompt": "Summarize only.",
            },
            dry_run=True,
        )
        assert "--model" in gemini_dispatch["argv"], gemini_dispatch["argv"]
        assert "--log-file" in gemini_dispatch["argv"], gemini_dispatch["argv"]
        assert "--gemini_dir" in gemini_dispatch["argv"], gemini_dispatch["argv"]
        assert (
            "agent-runtime/gemini/.gemini"
            in gemini_dispatch["argv"][gemini_dispatch["argv"].index("--gemini_dir") + 1]
        ), gemini_dispatch["argv"]
        assert (
            gemini_dispatch["argv"][gemini_dispatch["argv"].index("--add-dir") + 1]
            == gemini_dispatch["cwd"]
        ), gemini_dispatch["argv"]
        gemini_prompt = gemini_dispatch["argv"][gemini_dispatch["argv"].index("--print") + 1]
        assert (
            "GEMINI WORKSPACE:" in gemini_prompt and gemini_dispatch["cwd"] in gemini_prompt
        ), gemini_prompt
        # worktree falls back to HOME when absent (seam flagged)
        assert by_t["stranske/Repo#1"]["worktree_missing"] is True

        # heartbeat write is real + fresh
        hb = write_heartbeat()
        loaded = json.loads(HEARTBEAT_JSON.read_text())
        assert loaded["generated_at"] == hb["generated_at"] and "pid" in loaded

        # empty decision => nothing launched, no crash
        assert run({"assignments": []}, dry_run=True, heartbeat=False)["count"] == 0

        # The offload capability heartbeat must ACTUALLY FIRE — not merely "not raise".
        #
        # This exists because `capabilities` was missing from this module's imports while offload()
        # called `capabilities.production_heartbeat(...)` inside `try/except Exception: pass`. Every
        # offload raised NameError and swallowed it, so the `offload` capability logged zero
        # invocations across ~196 offloads/week and read `no_matching_work` in the inventory. A test
        # asserting only "the caller doesn't break" passes happily against that bug; the assertion
        # has to be that the call is REACHED. Patching through the module global also means this
        # fails if the import is ever removed again, which is exactly the regression to catch.
        _hb_calls = []
        import capabilities as _caps  # lazy, same as the helper

        _real_hb = _caps.production_heartbeat
        _saved_flag = os.environ.get("ORCH_CAPABILITY_HEARTBEATS")
        try:
            _caps.production_heartbeat = lambda *a, **k: _hb_calls.append((a, k)) or True
            os.environ["ORCH_CAPABILITY_HEARTBEATS"] = "1"
            try:
                offload("definitely-not-an-agent", "probe", cwd=tmp, timeout=1)
            except Exception:
                pass  # the bogus agent is expected to fail AFTER the heartbeat
            assert _hb_calls, "offload did not reach capabilities.production_heartbeat"
            assert _hb_calls[0][0][0] == "offload", _hb_calls
            assert _hb_calls[0][0][1] == "invocation", _hb_calls

            # KILL SWITCH: it must stop offload BEFORE anything is spent. Asserting "no heartbeat
            # fired" is what proves nothing ran -- the heartbeat is the first thing offload does
            # after the guard. A guard that refuses only after provisioning is a late abort, and the
            # whole reason this switch exists is the 2026-08-08 case where a dead seat kept being
            # dispatched to while capacity read `ok`.
            _hb_calls.clear()
            os.environ["ORCH_OFFLOAD_DISABLED"] = "1"
            try:
                _off = offload("definitely-not-an-agent", "probe", cwd=tmp, timeout=1)
            finally:
                os.environ.pop("ORCH_OFFLOAD_DISABLED", None)
            assert _off.get("disabled") is True, _off
            assert "ORCH_OFFLOAD_DISABLED" in str(_off.get("error")), _off
            assert _off.get("run_id") is None, "a disabled offload must not record a run"
            assert not _hb_calls, "disabled offload still reached the heartbeat -- it spent work"
            # ...and the guard must be OFF by default, or the fleet's transport is dead on arrival.
            _hb_calls.clear()
            try:
                offload("definitely-not-an-agent", "probe", cwd=tmp, timeout=1)
            except Exception:
                pass
            assert _hb_calls, "guard leaked: offload refused with the flag unset"

            # Lane prompt-schema capabilities must be credited with a MATCH when their task type is
            # routed. They are never executed locally (the dispatcher hands their schema to an
            # agent), so without this they read as `no_matching_work` while their work type is
            # actively being dispatched.
            for _tt, _cap in TASK_TYPE_CAPABILITY.items():
                _hb_calls.clear()
                build_prompt(_tt, "o/r#1")
                assert _hb_calls, f"{_tt} did not credit {_cap}"
                # Position-independent: `repo-playbook` legitimately fires first now, because
                # build_prompt -> repo_knowledge.append_context credits it on every prompt. An
                # index-0 assertion would break every time another capability starts recording,
                # which is the opposite of what this test is for.
                matching = [c for c in _hb_calls if c[0][0] == _cap]
                assert matching, (_tt, _cap, _hb_calls)
                # `match`, never `invocation` — the lane module itself did not run.
                assert matching[0][0][1] == "match", (_tt, matching)
            # Unmapped task types must credit no LANE capability (no fabricated routing evidence).
            # `repo-playbook` legitimately fires here — repo_knowledge.append_context runs on every
            # prompt — so the assertion is about the lane map, not about total silence.
            _lane_caps = set(TASK_TYPE_CAPABILITY.values())
            for _tt in ("implement", "review"):
                _hb_calls.clear()
                build_prompt(_tt, "o/r#1")
                credited = {c[0][0] for c in _hb_calls}
                assert not (credited & _lane_caps), (_tt, credited)
        finally:
            _caps.production_heartbeat = _real_hb
            if _saved_flag is None:
                os.environ.pop("ORCH_CAPABILITY_HEARTBEATS", None)
            else:
                os.environ["ORCH_CAPABILITY_HEARTBEATS"] = _saved_flag

        # delegate path: the orchestrator's OWN prompt overrides the template
        custom = plan_dispatch(
            {
                "agent": "vibe",
                "target": "o/r#1",
                "task_type": "delegated",
                "mode": "full",
                "lane": "opener",
                "prompt": "ORCHESTRATOR-CRAFTED PROMPT with issue context",
            },
            dry_run=True,
        )
        assert any("ORCHESTRATOR-CRAFTED PROMPT" in tok for tok in custom["argv"]), custom["argv"]
        custom_known = plan_dispatch(
            {
                "agent": "vibe",
                "target": "stranske/Counter_Risk#5",
                "task_type": "implement",
                "mode": "full",
                "lane": "closer",
                "prompt": "ORCHESTRATOR-CRAFTED PROMPT",
            },
            dry_run=True,
        )
        custom_known_prompt = " ".join(custom_known["argv"])
        assert "ORCHESTRATOR-CRAFTED PROMPT" in custom_known_prompt, custom_known["argv"]
        assert (
            "REPO PLAYBOOK (stranske/Counter_Risk)" in custom_known_prompt
            and "Black" in custom_known_prompt
        ), custom_known["argv"]
        for agent in AGENT_PERSONAS:
            mode = "composer" if agent == "cursor" else "full"
            delegated = plan_dispatch(
                {
                    "agent": agent,
                    "target": "o/r#77",
                    "task_type": "delegated",
                    "mode": mode,
                    "lane": "opener",
                    "prompt": "Delegated task.",
                },
                dry_run=True,
            )
            delegated_prompt = " ".join(delegated["argv"])
            assert AGENT_PERSONAS[agent] in delegated_prompt, delegated["argv"]
            assert CRITICAL_EVALUATOR_DIRECTIVE in delegated_prompt, delegated["argv"]

        # offload ergonomics: non-git prompt says not to commit, and isolation copies cwd to a safe workspace.
        nongit = HANDOFF / "nongit-workspace"
        nongit.mkdir()
        (nongit / "module.py").write_text("VALUE = 1\n")
        prepared = _offload_prompt("Implement a small proposal.", nongit, "cursor")
        assert (
            AGENT_PERSONAS["cursor"] in prepared and CRITICAL_EVALUATOR_DIRECTIVE in prepared
        ), prepared
        assert "Non-git workspace" in prepared and "Do not run git commit" in prepared, prepared
        assert "OFFLOAD_INCOMPLETE" in prepared and "I will inspect later" in prepared, prepared
        isolated = _isolate_offload_cwd(nongit)
        assert (
            isolated != nongit and (isolated / "module.py").read_text() == "VALUE = 1\n"
        ), isolated
        assert _default_offload_timeout("cursor", None) == DEFAULT_OFFLOAD_TIMEOUT
        assert _default_offload_timeout("gemini", None) == DEFAULT_GEMINI_OFFLOAD_TIMEOUT
        assert _default_offload_timeout("gemini", 123) == 123
        assert _offload_incomplete_reason("OFFLOAD_INCOMPLETE: command timed out") is not None
        assert (
            _offload_incomplete_reason(
                "I am waiting for the pytest suite execution to finish. I will inspect the results as soon as it completes."
            )
            is not None
        )
        assert (
            _offload_incomplete_reason(
                "No active tools are needed at the moment. Waiting for the full product verification check "
                "running as `task-85` to finish."
            )
            is not None
        )
        assert (
            _offload_incomplete_reason("Reviewed three files and found no actionable issues.")
            is None
        )
        assert _is_transient_network_failure("connection reset by peer", "", "")
        assert not _is_transient_network_failure("401 unauthorized", "", "")
        assert not _is_transient_network_failure("", "", "")
        assert not _is_transient_network_failure(
            "", "neither PlanModel nor RequestedModel specified", ""
        )
        captured = {}
        old_build_command = adapters.build_command
        old_subprocess_run = subprocess.run

        class Completed:
            returncode = 0
            stdout = '{"usage":{"input_tokens":7,"output_tokens":3}}\nOFFLOAD RESULT\n'
            stderr = ""

        class ProbeCompleted:
            """An incidental subprocess a double must NOT mistake for the run under test."""

            returncode = 0
            stdout = ""
            stderr = ""

        class WaitingCompleted:
            returncode = 0
            stdout = (
                "I am waiting for the pytest suite execution to finish in the offload workspace. "
                "I will inspect the results as soon as the task completes.\n"
            )
            stderr = ""

        class EmptyCompleted:
            returncode = 0
            stdout = ""
            stderr = ""

        def fake_build_command(agent, prompt, mode, cwd=None, **kwargs):
            # `**kwargs` is load-bearing, not defensive slack: every agent now has a registered
            # execution profile, so offload passes `profile=` for any seat. A stub with a narrower
            # signature raises TypeError inside offload() and the failure looks like a dispatch bug
            # rather than a stale test double. Captured so the profile stays assertable.
            captured["prompt"] = prompt
            captured["mode"] = mode
            captured["build_cwd"] = cwd
            captured["profile"] = kwargs.get("profile")
            return ["printf", "OFFLOAD RESULT"]

        def fake_run(cmd, cwd=None, **_kwargs):
            captured["cmd"] = cmd
            captured["cwd"] = cwd
            captured["kwargs"] = _kwargs
            return Completed()

        try:
            adapters.build_command = fake_build_command
            subprocess.run = fake_run
            off = offload(
                "cursor",
                "Make a parallel-safe code proposal.",
                cwd=str(nongit),
                isolate=True,
                timeout=1,
            )
        finally:
            adapters.build_command = old_build_command
            subprocess.run = old_subprocess_run
        assert (
            off["exit"] == 0 and off["isolated_cwd"] and off["output"].endswith("OFFLOAD RESULT")
        ), off
        assert off["run_id"].startswith("offload:cursor:") and Path(off["log"]).exists(), off
        assert (
            captured["cwd"] == off["isolated_cwd"] and "Non-git workspace" in captured["prompt"]
        ), captured
        assert captured["build_cwd"] == Path(off["isolated_cwd"]), captured
        assert (
            AGENT_PERSONAS["cursor"] in captured["prompt"]
            and CRITICAL_EVALUATOR_DIRECTIVE in captured["prompt"]
        ), captured
        assert "ORCH_AGENT_RUNTIME" in " ".join(captured["cmd"]), captured["cmd"]
        # offload also scrubs the proxy family and never inherits a blocking stdin (root-cause fix 2026-06-20)
        assert "unset " in captured["cmd"][2] and "HTTPS_PROXY" in captured["cmd"][2], captured[
            "cmd"
        ]
        assert captured["kwargs"].get("stdin") == subprocess.DEVNULL, captured["kwargs"]
        assert isinstance(_suspicious_net_env(), list), "timeout diagnostic helper returns a list"
        ledger_rows = [json.loads(line) for line in adapters.LEDGER.read_text().splitlines()]
        off_rows = [row for row in ledger_rows if row.get("run_id") == off["run_id"]]
        assert [row.get("event") for row in off_rows] == ["start", "complete"], off_rows
        with feedback._conn() as c:
            off_run = c.execute(
                "SELECT task_type, agent, mode FROM runs WHERE run_id=?", (off["run_id"],)
            ).fetchone()
        assert off_run == ("offload", "cursor", "offload"), off_run

        # WORKER PROVENANCE, asserted BEHAVIOURALLY through offload() rather than by calling the
        # selector directly. Testing `_select_offload_profile` alone passes even if offload stops
        # using it -- a mechanism asserted against itself. This asserts the wiring: a profiled
        # agent must leave a worker execution attempt behind, because that row is the only thing
        # that can ever resolve model provenance, and without it no episode can complete.
        try:
            adapters.build_command = fake_build_command
            subprocess.run = fake_run
            codex_off = offload("codex", "Summarize this file.", cwd=str(nongit), timeout=1)
        finally:
            adapters.build_command = old_build_command
            subprocess.run = old_subprocess_run
        with feedback._conn() as c:
            worker = c.execute(
                "SELECT profile_id, operation_role FROM execution_attempts "
                "WHERE run_id=? AND operation_role='worker'",
                (codex_off["run_id"],),
            ).fetchone()
        assert captured.get("profile"), "the selected profile must reach build_command"
        assert worker and worker[0], (
            "a profiled offload must record a worker execution attempt; without it "
            "resolved_worker_identity_for_run can never return a row",
            codex_off,
        )

        # THE RUN MUST NOT DISAGREE WITH ITSELF. The attempt row always carried
        # `profile["profile_id"]`, but the ledger and the routing metadata were handed the caller's
        # `profile_id` argument -- None whenever the profile was chosen internally, which is always.
        # `reconcile` builds its `profile_ids` set from the LEDGER field, so a null there is why the
        # branch that resolves a worker attempt could never fire. Assert all three agree.
        with feedback._conn() as c:
            meta_json = c.execute(
                "SELECT routing_metadata FROM runs WHERE run_id=?", (codex_off["run_id"],)
            ).fetchone()
        routed = json.loads(meta_json[0]) if meta_json and meta_json[0] else {}
        assert routed.get("selected_profile_id") == worker[0], (
            "routing metadata must name the profile that actually ran",
            routed,
            worker,
        )
        # Read the ledger the way reconcile does, through its own reader -- a `hasattr` fallback
        # here would make this assertion pass when the field is absent, which is the vacuous-test
        # shape this file has already been burned by three times.
        import ledger_reconcile as _lr

        ledger_rows, _ = _lr._read_ledger(_lr._ledger_path())
        ledger_ids = {
            row.get("selected_profile_id")
            for row in ledger_rows
            if row.get("run_id") == codex_off["run_id"] and row.get("event") == "start"
        }
        assert ledger_ids == {worker[0]}, (
            "the ledger start row must name the profile reconcile will look for",
            ledger_ids,
        )

        # A SEAT THAT CANNOT REPORT PROVENANCE RECORDS NO WORKER ATTEMPT. This assertion used to
        # demand the opposite -- "a profiled seat must record a worker attempt" -- and that was the
        # bug: gemini, cursor, vibe and aider keep no per-session model log, so every attempt they
        # wrote completed `unresolved` and could never become anything else. Cursor produced 23 in
        # five hours at ~230 offloads per two days, each re-caching the same prose reason, dragging
        # the profile's resolved-model coverage to a permanent 0.00 over a denominator that only
        # grows. Nothing decrements that; the only thing that would is the capability the seat
        # lacks. A row asserting the one fact it cannot establish is worse than no row.
        #
        # The profile still applies and the ledger still names it, so the REQUEST stays auditable.
        # Only the unbackable claim is withheld.
        # DERIVED FROM THE AUTHORITY, not a hardcoded seat. Four times now an assertion here named a
        # specific agent as unable to report, and four times that stopped being true once its store
        # or log was actually read -- so the test enforced a stale belief instead of catching it.
        # Ask `NO_SESSION_LOG_AGENTS` who cannot report, and if the answer is nobody, say so and
        # skip rather than inventing a case.
        mute_seat = next(
            (
                a
                for a in adapters.NO_SESSION_LOG_AGENTS
                if execution_profiles.profiles_for_agent(a, transport="offload")
            ),
            None,
        )
        assert (
            mute_seat
        ), "no seat lacks a reader any more -- delete this case rather than faking one"
        try:
            adapters.build_command = fake_build_command
            subprocess.run = fake_run
            gem_off = offload(mute_seat, "Summarize this file.", cwd=str(nongit), timeout=1)
        finally:
            adapters.build_command = old_build_command
            subprocess.run = old_subprocess_run
        with feedback._conn() as c:
            gem_rows = c.execute(
                "SELECT resolved_model FROM execution_attempts "
                "WHERE run_id=? AND operation_role='worker'",
                (gem_off["run_id"],),
            ).fetchall()
        assert gem_rows == [], (
            "a seat that can never resolve a model must not write a worker attempt",
            mute_seat,
            gem_rows,
        )
        assert (
            feedback.resolved_worker_identity_for_run(gem_off["run_id"]) is None
        ), "no attempt means no worker identity, which is the honest answer here"
        # The profile is still in force and still recorded, so withholding the claim has not cost
        # the audit trail: the run still says which profile ran.
        with feedback._conn() as c:
            gem_meta = c.execute(
                "SELECT routing_metadata FROM runs WHERE run_id=?", (gem_off["run_id"],)
            ).fetchone()
        gem_routed = json.loads(gem_meta[0]) if gem_meta and gem_meta[0] else {}
        assert gem_routed.get("selected_profile_id"), (
            "the profile must still be recorded even when no worker attempt is",
            gem_routed,
        )
        # And the reason is a STATIC property of the seat, from one authority -- never prose
        # re-cached on thousands of rows.
        ok, why = adapters.can_report_cli_identity(mute_seat)
        assert ok is False and why and "no_cli_session_log" in why, (mute_seat, ok, why)
        for seat in adapters.CLI_IDENTITY_READERS:
            assert adapters.can_report_cli_identity(seat)[0] is True, seat

        # ALL FOUR CELLS OF THE PREDICATE. Keying on either half alone was wrong in both
        # directions: `can_report` alone silenced the gemini-armed audit round this exists to make
        # minable, and no gate at all produced 23 permanently-dead rows in five hours. A row is
        # justified when the run is EVIDENCE (bound to a registered round, any seat) or when the
        # seat can RESOLVE (any run). Only unbound-and-unreportable writes nothing.
        def _attempts_for(seat: str, *, round_id: str | None) -> list:
            try:
                adapters.build_command = fake_build_command
                subprocess.run = fake_run
                res = offload(
                    seat, "Summarize.", cwd=str(nongit), timeout=1, research_round=round_id
                )
            finally:
                adapters.build_command = old_build_command
                subprocess.run = old_subprocess_run
            with feedback._conn() as c:
                return c.execute(
                    "SELECT profile_id FROM execution_attempts WHERE run_id=? "
                    "AND operation_role='worker'",
                    (res["run_id"],),
                ).fetchall()

        # The unreportable seat comes from the AUTHORITY, never a hardcoded name: gemini sat here
        # until its per-run CLI log turned out to name the model, and then this cell was asserting
        # something false.
        mute = next(
            (
                a
                for a in adapters.NO_SESSION_LOG_AGENTS
                if execution_profiles.profiles_for_agent(a, transport="offload")
            ),
            None,
        )
        assert mute, "no seat lacks a reader any more -- delete this case rather than faking one"
        # unreportable + evidence -> RECORDED, because the round needs the join to mine at all.
        assert _attempts_for(mute, round_id="domain/audit-x:review-corpus:2026-08-22"), (
            "a round-bound offload must record the attempt its episode joins on",
            mute,
        )
        # unreportable + no evidence -> nothing. This is the only silent cell, and the only one
        # where the row could never be used for anything.
        assert _attempts_for(mute, round_id=None) == [], ("the junk case must stay silent", mute)
        # reportable + no evidence -> RECORDED, because it resolves and feeds drift detection.
        assert _attempts_for(
            "codex", round_id=None
        ), "a resolvable seat's attempt is real provenance even unbound"

        # THE RUN'S OWN stream-json REPORT RESOLVES THE MODEL, with no store to search. This is the
        # standard answer (`gen_ai.response.model`): the tool prints the model it actually used, so
        # there is no workspace to match and no window to guess. It is what makes cursor resolvable
        # at all -- every one of its 24 real offloads shared `/private/tmp`, so no session in
        # cursor's own chat store was attributable to any single run.
        class _StreamCompleted:
            returncode = 0
            stdout = (
                '{"type":"system","subtype":"init","cwd":"/x","session_id":"s1",'
                '"model":"Composer 2.5"}\n'
                '{"type":"result","subtype":"success","result":"THE ANSWER","session_id":"s1"}\n'
            )
            stderr = ""

        def fake_stream_run(cmd, cwd=None, **_kwargs):
            return _StreamCompleted()

        try:
            adapters.build_command = fake_build_command
            subprocess.run = fake_stream_run
            streamed = offload("cursor", "Summarize.", cwd=str(nongit), timeout=1)
        finally:
            adapters.build_command = old_build_command
            subprocess.run = old_subprocess_run
        # The caller keeps its plain-text contract: the stream is reduced to the final result.
        assert streamed["output"].strip() == "THE ANSWER", streamed["output"]
        with feedback._conn() as c:
            row = c.execute(
                "SELECT resolved_provider, resolved_model, status, fallback_reason "
                "FROM execution_attempts WHERE run_id=? AND operation_role='worker'",
                (streamed["run_id"],),
            ).fetchone()
        # THE MODEL CAME FROM THE RUN ITSELF -- and `nonlocal` is what carries it out of
        # `_run_offload_attempt` to the completion. Without it the parse found `composer-2.5` and
        # the attempt still closed `unresolved`: the reader worked and the row denied it, which is
        # the most deceptive failure available here.
        assert row is not None, "a resolvable seat must record the attempt"
        assert row[1] == "composer-2.5", ("the run's own reported model must reach the row", row)
        assert row[2] == "complete" and row[3] is None, row
        # And the label -> id mapping never invents one.
        assert adapters.model_id_for_label("cursor", "Composer 2.5") == "composer-2.5"
        assert adapters.model_id_for_label("cursor", "some log prose") is None

        # THE OFFLOAD MUST ACTUALLY ASK FOR THE STREAM. Asserted on the real argv, because the
        # double above supplies stream-json stdout whatever was requested -- so without this,
        # reverting the transport to `text` left the test passing while nothing would ever be
        # reported in production. A double that answers a question the code did not ask is the
        # vacuous shape this file keeps re-growing.
        offload_argv = adapters.build_command(
            "cursor", "p", "composer", cwd=str(nongit), transport="offload"
        )
        assert (
            offload_argv[offload_argv.index("--output-format") + 1] == "stream-json"
        ), offload_argv
        # And the long-running dispatch path is deliberately UNCHANGED: its output is parsed
        # elsewhere, so reshaping it is a separate and riskier change.
        local_argv = adapters.build_command(
            "cursor", "p", "composer", cwd=str(nongit), transport="local"
        )
        assert local_argv[local_argv.index("--output-format") + 1] == "text", local_argv
        import ledger_reconcile

        dry_cost = ledger_reconcile.reconcile(adapters.LEDGER, dry_run=True)
        cost_by_run = {row["run_id"]: row for row in dry_cost["costs"]}
        assert cost_by_run[off["run_id"]]["tokens_in"] == 7, dry_cost
        assert cost_by_run[off["run_id"]]["tokens_out"] == 3, dry_cost

        captured.clear()

        def _write_agy_log(cmd, cwd, text):
            """Write where the COMMAND says, as the real CLI does.

            These doubles used to write a fixed `agy.log` beside the workspace. That silently
            stopped being the path once offload began giving each run its own agy log -- and a
            double writing somewhere the code no longer reads proves nothing, which is how the
            retry and log-tail assertions both went quiet at once.
            """
            wrapped_cmd = cmd[-1] if isinstance(cmd, list) else str(cmd)
            parts = shlex.split(wrapped_cmd)
            if "--log-file" not in parts:
                return
            target = Path(parts[parts.index("--log-file") + 1])
            if not target.is_absolute():
                target = Path(cwd or ".") / target
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)

        def fake_gemini_build_command(agent, prompt, mode, cwd=None, **_kwargs):
            # `**_kwargs`: gemini has a registered profile now, so offload passes `profile=`.
            captured["prompt"] = prompt
            captured["mode"] = mode
            captured["build_cwd"] = cwd
            # Model comes from the adapter constant, never a second hardcoded copy: the literal
            # that used to sit here outlived the real model by a Google rename (2026-08-08).
            return [
                "agy",
                "--gemini_dir",
                "/tmp/gemini",
                "--model",
                adapters.DEFAULT_GEMINI_MODEL,
                "--print",
                prompt,
                "--add-dir",
                ".",
                "--print-timeout",
                "40m",
                "--log-file",
                "agy.log",
            ]

        try:
            adapters.build_command = fake_gemini_build_command
            subprocess.run = fake_run
            gem = offload(
                "gemini", "Inspect the isolated copy.", cwd=str(nongit), isolate=True, timeout=1
            )
        finally:
            adapters.build_command = old_build_command
            subprocess.run = old_subprocess_run
        assert (
            gem["exit"] == 0 and gem["isolated_cwd"] and gem["process_cwd"] == str(nongit.resolve())
        ), gem
        assert (
            "GEMINI ISOLATED WORKSPACE" in captured["prompt"]
            and gem["isolated_cwd"] in captured["prompt"]
        ), captured
        assert (
            AGENT_PERSONAS["gemini"] in captured["prompt"]
            and CRITICAL_EVALUATOR_DIRECTIVE in captured["prompt"]
        ), captured
        assert f"--add-dir {shlex.quote(gem['isolated_cwd'])}" in " ".join(
            captured["cmd"]
        ), captured["cmd"]
        assert "--print-timeout 1m" in " ".join(captured["cmd"]), captured["cmd"]

        captured.clear()

        def fake_waiting_run(cmd, cwd=None, **_kwargs):
            captured["cmd"] = cmd
            captured["cwd"] = cwd
            captured["kwargs"] = _kwargs
            return WaitingCompleted()

        try:
            adapters.build_command = fake_gemini_build_command
            subprocess.run = fake_waiting_run
            bad_gem = offload(
                "gemini", "Run tests and report JSON.", cwd=str(nongit), isolate=True, timeout=1
            )
        finally:
            adapters.build_command = old_build_command
            subprocess.run = old_subprocess_run
        assert bad_gem["exit"] != 0 and bad_gem["raw_exit"] == 0, bad_gem
        assert "progress-only" in bad_gem["error"], bad_gem
        assert "offload marked failed" in Path(bad_gem["log"]).read_text(), bad_gem

        captured.clear()

        def fake_empty_run(cmd, cwd=None, **_kwargs):
            captured["cmd"] = cmd
            captured["cwd"] = cwd
            captured["kwargs"] = _kwargs
            # WRITE WHERE THE COMMAND SAYS, as the real CLI does. This used to write a fixed
            # `agy.log` beside the workspace, which silently stopped being the path once offload
            # started giving each run its OWN agy log -- a double that writes somewhere the code
            # no longer reads proves nothing.
            _write_agy_log(
                cmd,
                cwd,
                "failed to construct executor: neither PlanModel nor RequestedModel specified\n",
            )
            return EmptyCompleted()

        try:
            adapters.build_command = fake_gemini_build_command
            subprocess.run = fake_empty_run
            empty_gem = offload("gemini", "Say READY.", cwd=str(nongit), isolate=True, timeout=1)
        finally:
            adapters.build_command = old_build_command
            subprocess.run = old_subprocess_run
        assert empty_gem["exit"] == 70 and empty_gem["raw_exit"] == 0, empty_gem
        assert "agy log tail" in empty_gem["error"], empty_gem
        assert "neither PlanModel" in empty_gem["agent_log_tail"], empty_gem
        assert "[agent log tail]" in Path(empty_gem["log"]).read_text(), empty_gem

        captured.clear()
        retry_calls = {"count": 0}
        old_retry_backoff = os.environ.get("ORCH_OFFLOAD_RETRY_BACKOFF_S")
        old_network_retries = os.environ.get("ORCH_OFFLOAD_NETWORK_RETRIES")

        def fake_reset_then_success_run(cmd, cwd=None, **_kwargs):
            captured["cmd"] = cmd
            captured["cwd"] = cwd
            captured["kwargs"] = _kwargs
            if cmd[:2] != ["bash", "-lc"]:
                return Completed()
            retry_calls["count"] += 1
            if retry_calls["count"] == 1:
                _write_agy_log(cmd, cwd, "rpc failed: connection reset by peer\n")
                return EmptyCompleted()
            return Completed()

        try:
            os.environ["ORCH_OFFLOAD_RETRY_BACKOFF_S"] = "0"
            os.environ["ORCH_OFFLOAD_NETWORK_RETRIES"] = "1"
            adapters.build_command = fake_gemini_build_command
            subprocess.run = fake_reset_then_success_run
            retry_gem = offload(
                "gemini",
                "Say READY after a transient reset.",
                cwd=str(nongit),
                isolate=True,
                timeout=1,
            )
        finally:
            adapters.build_command = old_build_command
            subprocess.run = old_subprocess_run
            if old_retry_backoff is None:
                os.environ.pop("ORCH_OFFLOAD_RETRY_BACKOFF_S", None)
            else:
                os.environ["ORCH_OFFLOAD_RETRY_BACKOFF_S"] = old_retry_backoff
            if old_network_retries is None:
                os.environ.pop("ORCH_OFFLOAD_NETWORK_RETRIES", None)
            else:
                os.environ["ORCH_OFFLOAD_NETWORK_RETRIES"] = old_network_retries
        assert (
            retry_gem["exit"] == 0 and retry_gem["attempts"] == 2 and retry_gem["retried"] is True
        ), retry_gem
        assert retry_calls["count"] == 2, retry_calls

        captured.clear()

        def fake_timeout_run(cmd, cwd=None, **_kwargs):
            captured["cmd"] = cmd
            captured["cwd"] = cwd
            captured["kwargs"] = _kwargs
            if cmd[:2] == ["bash", "-lc"]:
                raise subprocess.TimeoutExpired(cmd, timeout=_kwargs.get("timeout"))
            return Completed()

        try:
            adapters.build_command = fake_build_command
            subprocess.run = fake_timeout_run
            timed_out = offload("cursor", "Timeout this offload.", cwd=str(nongit), timeout=1)
        finally:
            adapters.build_command = old_build_command
            subprocess.run = old_subprocess_run
        assert timed_out["exit"] == 124 and "timed out after 1s" in timed_out["error"], timed_out
        assert (
            "offload marked failed: timed out after 1s" in Path(timed_out["log"]).read_text()
        ), timed_out

        def fake_interrupt_run(cmd, cwd=None, **_kwargs):
            captured["cmd"] = cmd
            captured["cwd"] = cwd
            captured["kwargs"] = _kwargs
            if cmd[:2] == ["bash", "-lc"]:
                raise KeyboardInterrupt()
            return Completed()

        try:
            adapters.build_command = fake_build_command
            subprocess.run = fake_interrupt_run
            try:
                offload("cursor", "Interrupt this offload.", cwd=str(nongit), timeout=1)
                assert False, "KeyboardInterrupt should propagate after recording completion"
            except KeyboardInterrupt:
                pass
        finally:
            adapters.build_command = old_build_command
            subprocess.run = old_subprocess_run
        ledger_rows = [json.loads(line) for line in adapters.LEDGER.read_text().splitlines()]
        interrupted = [
            row for row in ledger_rows if row.get("error") == "interrupted by orchestrator"
        ]
        assert (
            interrupted
            and interrupted[-1]["event"] == "complete"
            and interrupted[-1]["exit"] == 130
        ), ledger_rows

        # delegate_remote: label-command + guards (dry_run => no live gh, no feedback write)
        _rlc = _remote_label_cmd(
            "stranske/Workflows#42", "cursor"
        )  # issues/labels API -> works for issue OR PR
        assert (
            _rlc[:3] == ["gh", "api", "--method"]
            and "repos/stranske/Workflows/issues/42/labels" in _rlc
            and "labels[]=agent:cursor" in _rlc
        ), _rlc
        dr = delegate_remote("cursor", "stranske/Workflows#42", dry_run=True, labels=set())
        assert (
            dr["label"] == "agent:cursor" and dr["dry_run"] and "labels[]=agent:cursor" in dr["cmd"]
        ), dr
        assert "error" in delegate_remote(
            "bogus", "o/r#1", dry_run=True
        ), "unknown remote agent rejected"
        assert "error" in delegate_remote(
            "cursor", "o/r", dry_run=True
        ), "remote delegation needs a PR number"
        # gate #4 rails: skip a paused PR, or one ALREADY in the agent pipeline (any agent:* label)
        assert "paused" in _remote_skip_reason({"agents:paused"}, "cursor")
        assert _remote_skip_reason({"agent:cursor"}, "cursor") is not None  # same agent -> skip
        assert (
            _remote_skip_reason({"agent:claude"}, "cursor") is not None
        )  # DIFFERENT agent -> skip (no double-assign)
        assert (
            _remote_skip_reason({"size:S", "needs-review"}, "cursor") is None
        )  # no agent:* -> fresh, delegate
        drp = delegate_remote(
            "cursor", "stranske/Workflows#42", dry_run=True, labels={"agents:paused"}
        )
        assert drp["skip"] and "paused" in drp["skip"], drp
        dra = delegate_remote("cursor", "o/r#9", dry_run=True, labels={"agent:claude"})
        assert dra["skip"] and "agent pipeline" in dra["skip"], dra

        # Exercised-capability tagging: each condition must mirror the capability's own heartbeat
        # condition, and must NOT fire otherwise — a tag that fires too widely credits a capability
        # for work it never touched, which is the one thing capability attribution refuses.
        assert _exercised_capability_ids({}, "gemini") == ["agy-runtime-isolation"]
        assert _exercised_capability_ids({}, "codex") == []
        thompson = {"exploration": True, "exploration_mode": "thompson-hybrid"}
        assert _exercised_capability_ids(thompson, "codex") == ["thompson-hybrid-routing"]
        # Flag set but no challenger actually chosen -> router does not heartbeat, so neither do we.
        assert (
            _exercised_capability_ids(
                {"exploration": False, "exploration_mode": "thompson-hybrid"}, "codex"
            )
            == []
        )
        # A different exploration mode is not Thompson.
        assert (
            _exercised_capability_ids(
                {"exploration": True, "exploration_mode": "epsilon-greedy"}, "codex"
            )
            == []
        )
        assert _exercised_capability_ids(thompson, "gemini") == [
            "agy-runtime-isolation",
            "thompson-hybrid-routing",
        ]

        # --- offload profile selection: the line that unblocked worker provenance ---
        # No worker execution attempt had ever been recorded, because the attempt write is guarded
        # on `profile` and `profile` came only from an explicit profile_id no caller passed.
        codex_profile = _select_offload_profile("codex", "mid")
        assert codex_profile and codex_profile["agent"] == "codex", codex_profile
        assert codex_profile["profile_id"] in {
            "codex-5.6-sol-high",
            "codex-5.6-terra-high",
            "codex-5.6-luna-high",
        }, codex_profile
        # Deterministic: the same agent must resolve to the SAME profile every time, or worker
        # attempts smear across three identities instead of accumulating against one.
        assert _select_offload_profile("codex", "mid")["profile_id"] == codex_profile["profile_id"]
        # Every real seat now has a registered profile, so every seat can record a worker attempt.
        # (Before this, only codex could, which is why provenance covered one agent of six.)
        for seat in ("claude", "gemini", "cursor", "vibe", "aider"):
            assert _select_offload_profile(seat, "mid"), f"{seat} must select a profile"
        # An agent with NO registered profile still selects nothing -- selection may not invent one.
        assert _select_offload_profile("definitely-not-an-agent", "mid") is None

        # AN OFFLOAD IS A READ, SO IT TAKES THE MID RUNG -- never the reasoning tier just because a
        # profile happens to exist there. `DEFAULT_OFFLOAD_TIER` and the comment beside it had
        # already diagnosed this ("a codex offload burned Sol and a gemini offload burned Pro"), and
        # registering one top-tier profile per seat silently overrode it: every gemini read was
        # routed to Pro.
        #
        # gemini is the case that matters, because its two lines are NOT one version ladder: Flash
        # (3.7/3.6/3.5) is the fast mid tier and Pro (3.1) is the higher-end reasoning tier, so
        # `3.7 > 3.1` is newer Flash rather than better than Pro. Both are legitimate; the task
        # decides, and an advisory read is not the task for Pro.
        for seat in ("codex", "claude", "gemini"):
            wanted = adapters.resolve_model(seat, adapters.DEFAULT_OFFLOAD_TIER)
            chosen = _select_offload_profile(seat, "offload")
            assert wanted, f"{seat} must have a tier ladder"
            assert chosen and chosen["requested_model"] == wanted, (seat, wanted, chosen)
        # And the reasoning tier is still REGISTERED, so full-tier work can still ask for it -- the
        # fix is choosing per task, not deleting the expensive rung.
        gemini_ids = {p["profile_id"] for p in execution_profiles.profiles_for_agent("gemini")}
        assert "gemini-3.1-pro-high" in gemini_ids, gemini_ids
        assert "gemini-3.6-flash-high" in gemini_ids, gemini_ids

        # AGY REPORTS IN ITS LOG, and the log must be PER RUN. agy's structured output carries only
        # conversation_id/cwd/usage; its log carries
        # `Propagating selected model override to backend: label="..."`. The default `--log-file` is
        # one shared path for every gemini run, so a line in it belongs to no particular run -- the
        # same non-attributability that made cursor's reused `/private/tmp` useless.
        def fake_agy_run(cmd, cwd=None, **_kwargs):
            # ISOLATION, NOT A SKIP. `adapters.advertised_models` shells out to `agy models` when
            # its disk cache (`agent-runtime/gemini/advertised-models.json`) is cold -- the normal
            # state on every machine except this instance, where the cache is warm and the probe
            # never fires. That probe landed INSIDE this patch window and overwrote `captured`, so
            # the per-run-log assertion below compared against `['agy', 'models']` and failed on a
            # bare runner while passing here. Record only the run under test (offload always shells
            # through `bash -lc <wrapped>`); let an incidental probe return empty and change nothing.
            if not (isinstance(cmd, list) and list(cmd[:2]) == ["bash", "-lc"]):
                return ProbeCompleted()
            captured["cmd"] = cmd  # asserted below; a stale `captured` proves the wrong command
            captured["cwd"] = cwd
            _write_agy_log(
                cmd,
                cwd,
                "I0822 20:08:19.863873 1 model_config_manager.go:311] Propagating selected "
                'model override to backend: label="Gemini 3.6 Flash (High)"\n',
            )
            return Completed()

        try:
            adapters.build_command = fake_gemini_build_command
            subprocess.run = fake_agy_run
            agy_off = offload("gemini", "Summarize.", cwd=str(nongit), timeout=1)
        finally:
            adapters.build_command = old_build_command
            subprocess.run = old_subprocess_run
        with feedback._conn() as c:
            agy_row = c.execute(
                "SELECT resolved_provider, resolved_model, status, fallback_reason "
                "FROM execution_attempts WHERE run_id=? AND operation_role='worker'",
                (agy_off["run_id"],),
            ).fetchone()
        assert agy_row is not None, "gemini can report, so the attempt must be recorded"
        assert agy_row[1] == "gemini-3.6-flash-high", ("agy's own log names the model", agy_row)
        assert agy_row[2] == "complete" and agy_row[3] is None, agy_row
        # The log path handed to agy must be THIS run's, never the shared default.
        # The message stays SHORT and names what it saw: this assertion fired on a Linux/3.14
        # runner while passing on the owner's Mac, and the bare `captured["cmd"]` dump was
        # truncated out of the CI log, so the failure could not be attributed from the log alone.
        _cmd = captured["cmd"]
        _joined = " ".join(_cmd) if isinstance(_cmd, list) else str(_cmd)
        assert "--log-file" in _joined, (
            f"agy argv lost its per-run log: type={type(_cmd).__name__} "
            f"len={len(_cmd)} head={str(_cmd)[:200]!r}"
        )
        assert "agent-runtime/gemini/logs/agy.log" not in _joined, (
            "a shared agy log cannot attribute a model to one run",
            captured["cmd"],
        )
        # And a label that is not a model never becomes one.
        assert adapters.model_label_from_agy_log("nothing to see") is None
        assert adapters.model_id_for_label("gemini", "some prose") is None

        # A single-lane seat has no ladder and must keep its one profile rather than losing it.
        for seat in ("cursor", "vibe"):
            assert adapters.resolve_model(seat, adapters.DEFAULT_OFFLOAD_TIER) is None, seat
            assert _select_offload_profile(seat, "offload"), seat

        print(
            "dispatcher.py selftest: OK (plan→argv via adapters, task-type prompts, "
            "claim-release wrapper, worktree-seam fallback, offload no-commit guard + isolation, "
            "offload run_id ledger reconciliation + Gemini progress-only/log-tail fail-closed, heartbeat, bogus-agent skip, "
            "delegate_remote label + guards, proxy-env scrub + ORCH_KEEP_PROXY + stdin=DEVNULL, "
            "offload profile selection)"
        )
    finally:
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)
        os.environ.pop("HANDOFF_DIR", None)
        AGENT_RUNTIME_DIR = old_agent_runtime_dir
        feedback.DB_PATH = old_feedback_db
        adapters.HANDOFF = old_adapters_handoff
        adapters.LEDGER = old_adapters_ledger


def main(argv: list[str]) -> int:
    if argv and argv[0] == "--show-prompt":
        if len(argv) < 2:
            print("usage: dispatcher.py --show-prompt <agent>", file=sys.stderr)
            return 2
        print(_offload_prompt("SHOW-PROMPT TASK", Path.cwd(), argv[1]))
        return 0
    if argv and argv[0] == "review-corpus":
        # Deterministic partitioning/validation around the existing synchronous offload
        # transport.  Pass this module's function explicitly so partitioned_review does
        # not create another dispatcher, claim, routing, or agent-execution seam.
        import partitioned_review

        return partitioned_review.main(argv[1:], offload_fn=offload)
    if "--selftest" in argv:
        _selftest()
        return 0
    if argv and argv[0] == "delegate":  # the orchestrator seat's hand
        import argparse

        p = argparse.ArgumentParser(prog="dispatcher.py delegate")
        p.add_argument("--agent", required=True)
        p.add_argument("--target", required=True)
        p.add_argument("--lane", default="opener")
        p.add_argument("--mode")
        p.add_argument("--task-type", default="implement")
        p.add_argument(
            "--influenced-by-role-run-id",
            action="append",
            default=[],
            help="accepted advisory role run to stamp onto this downstream dispatch",
        )
        g = p.add_mutually_exclusive_group(required=True)
        g.add_argument(
            "--prompt",
            help="inline orchestrator-authored prompt; preferred for compact one-off work",
        )
        g.add_argument("--prompt-file", help="path to a large or reusable prompt brief")
        ns = p.parse_args(argv[1:])
        prompt = ns.prompt if ns.prompt is not None else Path(ns.prompt_file).read_text()
        out = delegate(
            ns.agent,
            ns.target,
            ns.lane,
            prompt,
            ns.mode,
            task_type=ns.task_type,
            influenced_by_role_run_ids=ns.influenced_by_role_run_id,
        )
        print(json.dumps(out, default=str))
        return 0 if "error" not in out else 1
    if argv and argv[0] == "offload":  # read/summarize/research -> cheap agent -> result back
        import argparse

        p = argparse.ArgumentParser(prog="dispatcher.py offload")
        p.add_argument("--agent", required=True)
        p.add_argument("--cwd", default=".")
        p.add_argument("--mode")
        p.add_argument("--timeout", type=int, default=None)
        p.add_argument(
            "--isolate",
            "--worktree-isolation",
            action="store_true",
            dest="isolate",
            help="copy cwd to a persistent local offload workspace before running",
        )
        g = p.add_mutually_exclusive_group(required=True)
        g.add_argument("--prompt", help="inline prompt; preferred for bounded offloads and reviews")
        g.add_argument("--prompt-file", help="path to a large or reusable prompt brief")
        ns = p.parse_args(argv[1:])
        prompt = ns.prompt if ns.prompt is not None else Path(ns.prompt_file).read_text()
        try:
            out = offload(
                ns.agent, prompt, cwd=ns.cwd, mode=ns.mode, timeout=ns.timeout, isolate=ns.isolate
            )
        except KeyboardInterrupt:
            out = {
                "agent": ns.agent,
                "exit": 130,
                "output": "",
                "error": "interrupted by orchestrator",
            }
        print(out["output"] if out.get("output") else json.dumps(out, default=str))
        if out.get("error"):
            print(f"[orchestrator] offload error: {out['error']}", file=sys.stderr)
        return 0 if out["exit"] == 0 and not out.get("error") else 1
    dry = "--dry-run" in argv
    no_hb = "--no-heartbeat" in argv
    out = run(load_decision(), dry_run=dry, heartbeat=not no_hb)
    print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
