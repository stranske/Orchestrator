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

import contextlib
import functools
import hashlib
import inspect
import json
import math
import os
import re
import selectors
import shlex
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypedDict

import adapters
import claims
import credential_redaction
import execution_profiles
import feedback
import provision
import repo_knowledge
import worktree_git


class _DataOnlyCommandOptions(TypedDict, total=False):
    data_only: bool


class _CompletionPersistencePending(RuntimeError):
    """The provider finished, but its exact profile completion still needs accounting."""

    def __init__(self, completion: dict):
        super().__init__("completed offload profile accounting is pending")
        self.completion = completion


class _ProviderResultRetentionPending(RuntimeError):
    """One actual invocation could not establish its durable result receipt."""

    def __init__(self, result: dict):
        super().__init__("provider result retention is pending; do not repeat invocation")
        self.result = result


def _retain_provider_result(
    log_file: Path,
    result: dict,
    *,
    profile_id: str | None,
    attempt: int,
    started_at: float,
    completed_at: float,
    run_cwd: Path,
    process_cwd: Path,
) -> Path:
    """Create an immutable per-invocation result before exception-prone enrichment.

    This receipt proves observed transport facts, never resolved model identity.
    The explicit redaction opt-out keeps its existing return/log semantics, but
    does not authorize another copy of unmasked free text in this new receipt.
    """
    original = json.dumps(result, sort_keys=True).encode()
    disabled = credential_redaction.disabled()
    receipt = {
        "schema": "offload-provider-result/v1",
        "run_id": result["run_id"],
        "agent": result["agent"],
        "profile_id": profile_id,
        "cwd": str(run_cwd),
        "process_cwd": str(process_cwd),
        "log": str(log_file),
        "attempt": attempt,
        "started_at": started_at,
        "completed_at": completed_at,
        "exit": result["exit"],
        "raw_exit": result.get("raw_exit", result["exit"]),
        "outcome_uncertain": result.get("outcome_uncertain") is True,
        "full_result_retained": not disabled,
        "provider_result_sha256": hashlib.sha256(original).hexdigest(),
    }
    if disabled:
        receipt.update(
            retention_kind="bound_metadata_only",
            omitted_free_text=True,
            reason="credential_redaction_disabled",
        )
    else:
        # Redact every string, including nested provider metadata. This is the
        # existing redaction policy; no new credential source is consulted.
        def masked(value):
            if isinstance(value, str):
                return credential_redaction.redact(value)
            if isinstance(value, dict):
                return {key: masked(item) for key, item in value.items()}
            if isinstance(value, list):
                return [masked(item) for item in value]
            return value

        receipt["result"] = masked(result)
        receipt["retention_kind"] = "redacted_provider_result"
        receipt["retained_result_sha256"] = hashlib.sha256(
            json.dumps(receipt["result"], sort_keys=True).encode()
        ).hexdigest()
    raw = json.dumps(receipt, sort_keys=True).encode()
    destination = log_file.with_suffix(f".provider-result-{attempt}.json")
    temporary = None
    try:
        fd, temporary = tempfile.mkstemp(prefix=".provider-result-", dir=log_file.parent)
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        # link is create-only. A collision is a barrier, never an overwrite.
        os.link(temporary, destination)
        directory = os.open(log_file.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary is not None:
            # A private temporary orphan is recoverable. Cleanup must never
            # shadow either the original write failure or a committed receipt.
            with contextlib.suppress(OSError):
                os.unlink(temporary)
    return destination


def _persist_profile_completion(write, run_id: str, **fields) -> None:
    """Retry only one idempotent UPDATE, never a provider or completion ledger append."""
    deadline = time.monotonic() + 2.0
    for attempt in range(8):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            write(run_id, **fields, lock_timeout_s=min(0.2, remaining))
            return
        except sqlite3.OperationalError as exc:
            code = getattr(exc, "sqlite_errorcode", None)
            locked = (
                isinstance(code, int)
                and (code & 255) in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}
            ) or str(exc).lower() in {"database is locked", "database table is locked"}
            if not locked:
                raise
        remaining = deadline - time.monotonic()
        if attempt < 7 and remaining > 0:
            time.sleep(min(0.05, remaining))
    raise _CompletionPersistencePending(
        {"operation": write.__name__, "run_id": run_id, "fields": fields, "reason": "sqlite_busy"}
    )


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
# Infrastructure delegates must not consume the registry-prefix opener slots.
INFRA_REPOS = {"stranske/Workflows", "stranske/Orchestrator"}


def _dispatch_branch(target: str, lane: str, agent: str, prefix: str | None) -> str:
    repo, num = provision.parse_target(target)
    prefix = prefix if prefix is not None else ("orch" if repo in INFRA_REPOS else agent)
    prefix = prefix.rstrip("/")
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_-]*(?:/[A-Za-z0-9_][A-Za-z0-9_-]*)*", prefix):
        raise ValueError("branch prefix must contain nonempty alphanumeric, '_' or '-' segments")
    return f"{prefix}/issue-{num}" if num is not None else f"{prefix}/{lane}"


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
    "muse": "You are Muse Code (Meta), running headless in a sandbox on a subscription seat with a limited rolling allowance. Make focused, test-backed edits, spend few steps, and report exactly what changed and what you did NOT do.",
}

TESTGEN_READ_ONLY_GATE_GUARD = (
    "Do not edit `testgen_gate.py`, `testgen_lane.py`, or other Orchestrator "
    "gate/helper files; they are acceptance infrastructure. If the gate appears "
    "wrong, stop and report the failing check instead of changing the gate. "
    "Before committing, run `git diff --name-only` and confirm no Orchestrator "
    "gate/helper file changed."
)

# Prompt construction records matching work; an actual local delegate additionally
# records invocation for the three delivery lanes below, after the worker starts.
# A dry-run plan, refused claim, or unbuildable command never invokes a lane.
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
    TASK_TYPE_CAPABILITY: prompt construction is matching, not worker execution.
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


def _is_git_worktree(cwd: str | Path, *, timeout: float | None = None) -> bool:
    try:
        res = subprocess.run(
            worktree_git.hardened_argv(cwd, ["rev-parse", "--is-inside-work-tree"]),
            capture_output=True,
            text=True,
            timeout=max(0.01, min(5.0, timeout)) if timeout is not None else None,
        )
    except subprocess.TimeoutExpired:
        if timeout is not None:
            raise TimeoutError(
                "owned request window expired checking workspace provenance"
            ) from None
        return False
    return res.returncode == 0 and res.stdout.strip() == "true"


def _agent_preamble(agent: str) -> str:
    persona = AGENT_PERSONAS.get(agent, "")
    return (
        f"{persona}\n\n{CRITICAL_EVALUATOR_DIRECTIVE}" if persona else CRITICAL_EVALUATOR_DIRECTIVE
    )


# What a read-only agent (an offload, a reviewer, an evaluator) must not do on GitHub. Since the gh
# config pin (#461), an agent started from a session holds its dispatcher's gh, which can write;
# before it, gh failed closed there. Such agents need READS: every gh call that stopped at the login
# error in 90 days of offload logs was a read. Owner decision 2026-10-05. Offloads and evaluators
# read this one sentence, so the two cannot drift apart.
GH_READ_ONLY_RULE = (
    "On GitHub, do not open, merge or close pull requests, close issues, edit labels, or post "
    "comments or reviews. Reading with gh is fine."
)


def _offload_prompt(
    prompt: str, cwd: str | Path, agent: str | None = None, *, timeout: float | None = None
) -> str:
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
        f"- Do not run git commit or git push from an offload. {GH_READ_ONLY_RULE}",
        "- If you edit files, keep changes inside the current workspace and report changed paths.",
        (
            "- Product-level owner decision needed? Print one line "
            'OWNER_QUESTION: {"question": "...", "default": "...", "expires_days": 7} '
            "and PROCEED with your default — never wait."
        ),
    ]
    is_worktree = (
        _is_git_worktree(cwd, timeout=timeout) if timeout is not None else _is_git_worktree(cwd)
    )
    if not is_worktree:
        rules.insert(
            1, "- Non-git workspace: do not try to create commits, branches, pushes, or PRs."
        )
    return f"{_agent_preamble(agent or '')}\n\n{prompt.rstrip()}\n\n" + "\n".join(rules)


def _relocated_workspace_rule(relocated: dict) -> str:
    """The offload rule that tells a relocated run where it is and why (adapters.agent_workspace)."""
    return (
        f"- Workspace: {relocated['workspace']} is an empty scratch directory of your own. The "
        f"requested workspace was refused because {relocated['reason']}. Read other files by "
        "absolute path; write only inside your workspace."
    )


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


def _remove_owned_offload_partial(partial: Path, identity: tuple[int, int]) -> bool:
    """Bound failed-copy deletion; a replaced path is never cleanup authority."""
    try:
        current = partial.lstat()
    except FileNotFoundError:
        return True
    if partial.is_symlink() or (current.st_dev, current.st_ino) != identity:
        return False
    cleanup = (
        "import os,shutil,stat,sys\n"
        "path,device,inode=sys.argv[1:]\n"
        "current=os.lstat(path)\n"
        "if not stat.S_ISDIR(current.st_mode) or (current.st_dev,current.st_ino)!=(int(device),int(inode)): sys.exit(3)\n"
        "shutil.rmtree(path)\n"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", cleanup, str(partial), *map(str, identity)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        child.communicate(timeout=2)
    except subprocess.TimeoutExpired:
        # One attempt against this helper's private group. A denial propagates;
        # no alternate signal or in-process rmtree may bypass that fence.
        with contextlib.suppress(ProcessLookupError):
            os.killpg(child.pid, signal.SIGKILL)
        with contextlib.suppress(subprocess.TimeoutExpired):
            child.communicate(timeout=2)
        return False
    return child.returncode == 0 and not partial.exists() and not partial.is_symlink()


def _offload_copier_terminal(proc) -> bool:
    if proc.poll() is None:
        return False
    try:
        os.killpg(proc.pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        raise
    except OSError:
        return False
    return False


def _isolate_offload_cwd(cwd: str | Path, *, timeout: float | None = None) -> Path:
    if timeout is not None and timeout <= 0:
        raise TimeoutError("bounded offload workspace copy has no request time remaining")
    src = adapters.workspace_path(cwd)
    if not src.is_dir():
        raise FileNotFoundError(f"offload cwd is not a directory: {src}")
    OFFLOAD_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    base = OFFLOAD_DIR / f"{stamp}-{src.name or 'workspace'}-{os.getpid()}"
    dest = base
    i = 1
    while dest.exists() or (
        timeout is not None and dest.with_name(dest.name + ".partial").exists()
    ):
        i += 1
        dest = OFFLOAD_DIR / f"{base.name}-{i}"
    if timeout is None:
        shutil.copytree(src, dest, ignore=_offload_ignore)
    else:
        partial = dest.with_name(dest.name + ".partial")
        partial.mkdir(mode=0o700)  # exclusive operation-owned destination
        stat = partial.lstat()
        partial_identity = (stat.st_dev, stat.st_ino)
        copier = (
            "import json,shutil,sys\n"
            "src,dst,raw=sys.argv[1:]\n"
            "heavy=set(json.loads(raw))\n"
            "def ignore(_directory,names): return {name for name in names if name in heavy or name.endswith('.pyc')}\n"
            "shutil.copytree(src,dst,ignore=ignore,dirs_exist_ok=True)\n"
        )
        proc = subprocess.Popen(
            [
                sys.executable,
                "-c",
                copier,
                str(src),
                str(partial),
                json.dumps(
                    sorted(
                        {
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
                    )
                ),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            _stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
            try:
                proc.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                raise TimeoutError(
                    "bounded offload workspace copy exceeded request window; "
                    f"copier terminality unverified; partial retained: {partial}"
                ) from None
            removed = _offload_copier_terminal(proc) and _remove_owned_offload_partial(
                partial, partial_identity
            )
            retained = (
                "" if removed else f"; copier/cleanup unverified; partial retained: {partial}"
            )
            raise TimeoutError(
                "bounded offload workspace copy exceeded request window" + retained
            ) from None
        if proc.returncode != 0:
            removed = _offload_copier_terminal(proc) and _remove_owned_offload_partial(
                partial, partial_identity
            )
            retained = (
                "" if removed else f"; copier/cleanup unverified; partial retained: {partial}"
            )
            raise RuntimeError(
                f"bounded offload workspace copy failed: {(stderr or '')[:500]}" + retained
            )
        os.replace(partial, dest)
    # Canonical like `src`: offload writes this path into gemini's `--add-dir` over the adapter's
    # own resolved value, so an OFFLOAD_DIR behind a symlink handed agy's guard, the GEMINI
    # ISOLATED WORKSPACE line and the run's target the unresolved spelling. (2026-10-02)
    return adapters.workspace_path(dest)


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


def _offload_incomplete_reason(
    output: str, *, progress_only: bool = True, codex_stream: bool = False
) -> str | None:
    """Detect offload responses that are statuses, not deliverables.

    The confirmed Gemini failure was a short stdout-only promise to inspect test results later while the
    process exited 0. Treat explicit OFFLOAD_INCOMPLETE markers from any agent as failures, and treat
    short progress-only prose as failures for lanes where that heuristic is enabled.

    `codex_stream` says `output` is codex's `exec --json` stream, which is also every command the
    agent ran and every file it read, so the marker is read only from what the agent SAID LAST: the
    rule tells it to print the marker and stop. Read whole, a doc that quotes the marker failed a
    finished run. On 2026-10-05, 30 of the 51 codex offloads recorded as exit 70 carried it only in
    command output, all 30 with `turn.completed`; the repo-audit skill's references quote it. The 12
    that said it in a message said it in their last one. A stream in which the agent said nothing
    has no deliverable, as empty stdout has none, and says so in its own words.
    """
    text = (output or "").strip()
    if not text:
        return "agent returned no stdout"
    if codex_stream:
        import rate_incidents

        said = rate_incidents.codex_agent_messages(text.splitlines())
        if not said:
            return "agent returned no message"
        text = said[-1]
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
    return f'export PATH="/opt/homebrew/bin:{REAL_HOME}/.local/bin:{REAL_HOME}/.cursor/bin:$PATH"'


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
    # A proxy URL can carry `user:password@`, and these lines go to the log and the caller. The
    # value is known to be a URL, so its whole userinfo is masked at any length, before the shapes.
    return [
        credential_redaction.redact(credential_redaction.mask_url_userinfo(f"{n}={os.environ[n]}"))
        for n in names
        if os.environ.get(n)
    ]


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
    elif agent == "muse":
        (base / ".local/state").mkdir(parents=True, exist_ok=True)
        # The wrapper moves XDG_CONFIG_HOME here, and Muse reads its LOGIN RECORD from
        # `$XDG_CONFIG_HOME/muse/auth.json` (found by a live offload 2026-10-15: "missing meta
        # credentials"). Link the owner's record in; the token itself stays in the macOS keychain,
        # reachable through the one Keychains directory alias in the private HOME. Linked,
        # never copied, so re-login is picked up without importing personal Muse settings.
        adapters.ensure_muse_auth_link(
            REAL_HOME / ".config" / "muse" / "auth.json", base / ".config" / "muse" / "auth.json"
        )
        adapters.ensure_muse_keychain_link(REAL_HOME, base)
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


# Kill switch for the gh config pin in `_agent_runtime_prelude`. Set to 1 and the prelude is built
# exactly as it was before the pin existed. Inherited explicit config or token authentication still
# works; otherwise gh reads the empty runtime config and stops at "gh auth login".
AGENT_GH_CONFIG_DISABLED_ENV = "ORCH_AGENT_GH_CONFIG_DISABLED"
# A token ASSIGNMENT, the one thing an agent's wrapper must never contain. A bare mention is allowed:
# `unset GH_TOKEN GITHUB_TOKEN` removes a token, and a guard that forbade the name would refuse it.
# The prelude and its selftest read this one pattern, so the two checks cannot drift apart.
TOKEN_ASSIGNMENT = re.compile(r"\b(?:GH_TOKEN|GITHUB_TOKEN)=")


def agent_gh_config_disabled() -> bool:
    """The kill switch, read the way ORCH_OFFLOAD_DISABLED is: a CRLF env file's `1\\r` counts."""
    return os.environ.get(AGENT_GH_CONFIG_DISABLED_ENV, "").strip() == "1"


def gh_config_dir() -> Path:
    """The directory gh reads its hosts file from in THIS process, by gh's own rule.

    `GH_CONFIG_DIR`, else `$XDG_CONFIG_HOME/gh`, else `~/.config/gh` (gh `config.ConfigDir`). The
    dispatcher reads it from its own environment, before the agent prelude moves `XDG_CONFIG_HOME`,
    so a dispatched agent's gh resolves the same account as the gh of whoever dispatched it.
    """
    explicit = os.environ.get("GH_CONFIG_DIR")
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if explicit:
        config = Path(explicit)
    elif xdg:
        config = Path(xdg) / "gh"
    else:
        config = REAL_HOME / ".config" / "gh"
    # Child wrappers run in a different cwd. Anchor the parent's relative config before export;
    # absolute() preserves symlink/.. spelling and does not require the directory to exist.
    return config.absolute()


# THE gh WRITE GUARD (owner decision 2026-10-05). With the pin above, an unsandboxed agent's
# `gh auth logout` deletes the owner's keychain token and empties the real hosts file, and
# `gh config set` rewrites the owner's config, for the whole machine. These are refused inside a
# dispatched agent; every other gh command runs unchanged. A wrapper first on PATH alone is not
# enough: measured 2026-10-05, a login shell (codex runs every command with `zsh -lc`, cursor's
# shell re-applies the login PATH) puts /opt/homebrew/bin back in front. So the same check is also
# a `gh` shell FUNCTION, which outranks PATH: bash reads it from BASH_ENV, zsh from a proxy
# ZDOTDIR whose files first run the owner's real dotfiles. Bypassable on purpose by gh's full path.
AGENT_GH_GUARD_DISABLED_ENV = "ORCH_AGENT_GH_GUARD_DISABLED"
GH_GUARD_REFUSED = (
    "auth logout",
    "auth login",
    "auth switch",
    "auth refresh",
    "config set",
    "alias set",
    "alias delete",
    "alias import",
)
GH_GUARD_EXIT = 77  # EX_NOPERM
_ZSH_DOTFILES = (".zshenv", ".zprofile", ".zshrc", ".zlogin", ".zlogout")


def agent_gh_guard_disabled() -> bool:
    return os.environ.get(AGENT_GH_GUARD_DISABLED_ENV, "").strip() == "1"


def _gh_guard_check(leave: str) -> str:
    """The refusal, as POSIX shell that sh, bash and zsh all read. `leave` is exit or return.

    It scans EVERY argument rather than `$1 $2`: options may come first (`gh -R o/r auth logout`)
    or between the group and its subcommand (`gh auth --hostname h logout`). A decided subcommand
    anywhere after its group refuses; a false refusal is cheap, a missed one is not.
    """
    groups = sorted({pair.split()[0] for pair in GH_GUARD_REFUSED})
    pattern = "|".join(f'"{pair}"' for pair in GH_GUARD_REFUSED)
    return (
        "__orch_gh_group=; __orch_gh_hit=\n"
        'for __orch_gh_arg in "$@"; do\n'
        f'  case "$__orch_gh_group $__orch_gh_arg" in {pattern}) '
        '__orch_gh_hit="$__orch_gh_group $__orch_gh_arg"; break ;; esac\n'
        f'  case "$__orch_gh_arg" in {"|".join(groups)}) '
        '[ -n "$__orch_gh_group" ] || __orch_gh_group="$__orch_gh_arg" ;; esac\n'
        "done\n"
        'if [ -n "$__orch_gh_hit" ]; then\n'
        "  printf '%s\\n' \"orchestrator gh guard: 'gh $__orch_gh_hit' is refused inside a "
        "dispatched agent: it would change the owner's gh login or config for the whole machine.\" >&2\n"
        f"  unset __orch_gh_group __orch_gh_hit __orch_gh_arg; {leave} {GH_GUARD_EXIT}\n"
        "fi\n"
        "unset __orch_gh_group __orch_gh_hit __orch_gh_arg\n"
    )


def _gh_guard_function() -> str:
    # zsh expands an owner's alias while parsing a function definition; remove it
    # after sourcing startup files so the function can enforce refusal precedence.
    return (
        "unalias gh 2>/dev/null || :\ngh() {\n" + _gh_guard_check("return") + 'command gh "$@"\n}\n'
    )


def _gh_guard_files(guard: Path) -> dict[Path, str]:
    wrapper = (
        "#!/bin/sh\n# Written by dispatcher.py: the gh write guard. Do not edit.\n"
        + _gh_guard_check("exit")
        + "set -f\n"
        + 'self=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)\n'
        + "IFS=:\n"
        + "for d in $PATH; do\n"
        + '  [ -n "$d" ] || continue\n'
        + '  r=$(CDPATH= cd -- "$d" 2>/dev/null && pwd -P) || continue\n'
        + '  [ "$r" = "$self" ] && continue\n'
        + '  if [ -x "$d/gh" ] && [ ! -d "$d/gh" ]; then exec "$d/gh" "$@"; fi\n'
        + "done\n"
        + "echo 'orchestrator gh guard: no gh found on PATH' >&2\n"
        + "exit 127\n"
    )
    bashenv = (
        "# Written by dispatcher.py: the gh write guard for bash. Do not edit.\n"
        'if [ -n "${ORCH_GH_GUARD_PREV_BASH_ENV:-}" ] '
        '&& [ "$ORCH_GH_GUARD_PREV_BASH_ENV" != "$BASH_ENV" ]; then\n'
        '  . "$ORCH_GH_GUARD_PREV_BASH_ENV"\nfi\n' + _gh_guard_function()
    )
    files = {guard / "bin" / "gh": wrapper, guard / "bashenv.sh": bashenv}
    for name in _ZSH_DOTFILES:
        # Run the owner's own file first, then (re)define the function so it wins.
        files[guard / "zdotdir" / name] = (
            "# Written by dispatcher.py: the gh write guard for zsh. Do not edit.\n"
            '__orch_gh_real="${ORCH_GH_GUARD_REAL_ZDOTDIR:-$HOME}"\n'
            f'if [ "$__orch_gh_real" != "$ZDOTDIR" ] && [ -f "$__orch_gh_real/{name}" ]; then\n'
            f'  source "$__orch_gh_real/{name}"\nfi\n' + _gh_guard_function()
        )
    return files


def _ensure_gh_guard() -> Path:
    """Write the guard files under the agent runtime; replace atomically, only when they differ."""
    guard = AGENT_RUNTIME_DIR / "gh-guard"
    for path, text in _gh_guard_files(guard).items():
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            if path.read_text() == text:
                continue
        except OSError:
            pass
        # One temp file per writer: concurrent offloads in one process share a pid.
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write(text)
            os.chmod(tmp, 0o755 if path.name == "gh" else 0o644)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    return guard


def _gh_guard_exports() -> list[str]:
    guard = _ensure_gh_guard()
    proxy, bashenv = str(guard / "zdotdir"), str(guard / "bashenv.sh")
    # A dispatcher run INSIDE a guarded agent already sees the proxy and our BASH_ENV; carry the
    # owner's originals through instead, or the proxy would source itself.
    zdotdir = os.environ.get("ZDOTDIR")
    if zdotdir == proxy:
        zdotdir = os.environ.get("ORCH_GH_GUARD_REAL_ZDOTDIR")
    prev = os.environ.get("BASH_ENV")
    if prev == bashenv:
        prev = os.environ.get("ORCH_GH_GUARD_PREV_BASH_ENV")
    exports = [
        f"export ORCH_GH_GUARD_REAL_ZDOTDIR={shlex.quote(zdotdir or str(REAL_HOME))}",
        f"export ZDOTDIR={shlex.quote(proxy)}",
        (
            f"export ORCH_GH_GUARD_PREV_BASH_ENV={shlex.quote(prev)}"
            if prev
            else "unset ORCH_GH_GUARD_PREV_BASH_ENV"
        ),
        f"export BASH_ENV={shlex.quote(bashenv)}",
        f'export PATH={shlex.quote(str(guard / "bin"))}:"$PATH"',
    ]
    return exports


def _agent_runtime_prelude(agent: str) -> str:
    """Shell exports for the child agent only; run this inside a subshell."""
    from storage_lifecycle import owned_agent_tmpdir

    owned_tmp = owned_agent_tmpdir(agent)
    base = _ensure_agent_runtime(agent)
    exports = [
        f"export ORCH_REAL_HOME={shlex.quote(str(REAL_HOME))}",
        f"export ORCH_AGENT_RUNTIME={shlex.quote(str(base))}",
        f"export XDG_CACHE_HOME={shlex.quote(str(base / '.cache'))}",
        f"export XDG_CONFIG_HOME={shlex.quote(str(base / '.config'))}",
        f"export XDG_DATA_HOME={shlex.quote(str(base / '.local/share'))}",
        f"export TMPDIR={shlex.quote(str(owned_tmp if owned_tmp is not None else base / 'tmp'))}",
    ]
    # gh honours XDG_CONFIG_HOME, so the redirect above hid its hosts file from every agent whose
    # dispatcher exported neither GH_CONFIG_DIR nor GH_TOKEN (a delegate or offload started from a
    # session, a lane or the check-in runner; the tick's orchestrate.sh exports both, and its
    # children inherit them): gh printed "gh auth login" and exited 4 before it asked the keyring.
    # Measured 2026-10-04 over 90 days of dispatch logs: 58 of the 58 delegated codex runs that
    # called gh failed that way on their first call, and 25 then read the token through
    # `git credential fill` instead. The pin is a PATH, never a token. gh still reads the token
    # where that config says, the keyring here, which codex's workspace-write sandbox (network on)
    # reaches; the agent gets no credential it could not already read. It works both ways, though:
    # gh's own writes (`auth logout`, `auth switch`, `config set`) now reach that config and that
    # keyring entry too, exactly as they would from the dispatcher's shell.
    if not agent_gh_config_disabled():
        exports.append(f"export GH_CONFIG_DIR={shlex.quote(str(gh_config_dir()))}")
    if not agent_gh_guard_disabled():
        exports.extend(_gh_guard_exports())
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
    elif agent == "muse":
        # The seat is the owner's SUBSCRIPTION LOGIN. `META_API_KEY` always overrides that login
        # (`muse login --help`), and a Default-Project key metered PAY-AS-YOU-GO (measured on the live
        # console 2026-10-15; the `Muse Code`-project key is untested), so a
        # key inherited from a session, lane or launchd environment would silently turn subscription
        # use into API spend. Unset it by NAME only: the value is never read or written here.
        # A real native trial proved private HOME works when only Library/Keychains is
        # aliased to the existing credential store. Personal Muse configuration stays private.
        exports.append(f"export HOME={shlex.quote(str(base))}")
        exports.append(f"export XDG_STATE_HOME={shlex.quote(str(base / '.local/state'))}")
        exports.append("unset META_API_KEY")
        exports.append(
            f"export MUSE_AUTH_PATH={shlex.quote(str(base / '.config' / 'muse' / 'auth.json'))}"
        )
    return "; ".join(exports) + "; "


def _record_muse_usage(started: float, lane: str, rc: object, seconds: float) -> None:
    """Append one TSV row per `muse exec`: epoch_start, lane, exit_code, whole seconds.

    Muse exposes no usage readout (no token/usage/quota field in `--json`, and the console lags up
    to an hour), so the only machine-readable consumption signal is our own exec count against the
    plan's rolling 5-hour allowance. `muse-usage.sh` reads this file. The measurement must never
    fail the run it measures, so a write error is reported on stderr and otherwise ignored.
    """
    try:
        log = Path(
            os.environ.get("ORCH_MUSE_USAGE_LOG")
            or (REAL_HOME / ".codex" / "handoff" / "muse-usage.log")
        )
        with log.open("a") as fh:
            fh.write(f"{int(started)}\t{lane}\t{rc}\t{int(seconds)}\n")
    except OSError as exc:
        # Nonfatal, but never silent: this row is the attempt's only consumption record.
        print(
            f"[orchestrator] muse usage not recorded ({type(exc).__name__}: {exc})", file=sys.stderr
        )
        return


def _muse_observation_window(observed_start_ts: int) -> tuple[int, int]:
    """Return the rolling five-hour interval observed at invocation start.

    The subscription advertises a rolling five-hour allowance but no machine-readable reset epoch.
    These bounds end at the actual local observation and do not invent a fixed provider reset.
    The feedback row separately retains the invocation's execution bounds.
    """
    duration_s = 5 * 60 * 60
    end = int(observed_start_ts)
    return end - duration_s, end


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
        "selected_capability_id",
        "selected_capability_version_id",
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
        "pacing_decision": assignment.get("pacing_decision"),
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
        "capability_dispatch_result": assignment.get("capability_dispatch_result"),
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
    from storage_lifecycle import StorageBlocked, owned_agent_tmpdir

    try:
        owned_agent_tmpdir(agent)
    except StorageBlocked as exc:
        return {
            "error": str(exc),
            "agent": agent,
            "storage_context_refused": True,
            "process_started": False,
            "outcome_uncertain": False,
            "next_trigger": "reconcile_owned_storage_context",
        }
    target, task_type = assignment["target"], assignment["task_type"]
    lane = assignment.get("lane") or "opener"
    branch = None
    if lane != "closer":
        try:
            branch = _dispatch_branch(target, lane, agent, assignment.get("branch_prefix"))
        except ValueError as exc:
            return {"error": str(exc), "target": target, "agent": agent}
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
    if assignment.get("review_loop"):
        prompt += (
            "\nREVIEW-LOOP HANDOFF: Reuse the existing source issue branch/PR when repairing. "
            "Do not merge. Once the ready-for-review PR is published, print exactly "
            "PR_URL=https://github.com/<owner>/<repo>/pull/<number> on its own line. "
            "Independent exact-head review runs after your writer exits; ordinary CI and "
            "review gates still apply."
        )
    if branch:
        prompt += (
            f"\n\nBRANCH POLICY: Use the provisioned branch (default `{branch}`) for this work "
            "and its PR; retain this prefix "
            "if adding a descriptive slug. This dispatch policy overrides branch naming in "
            "the task brief. Do not switch to another agent prefix."
        )
    profile_id = assignment.get("selected_profile_id")
    selected_profile = execution_profiles.get_profile(profile_id) if profile_id else None
    model = adapters.model_identity(agent, mode, selected_profile)
    if dry_run:
        cwd = provision.worktree_path(target, lane)
        worktree_missing = not (cwd / ".git").exists()
    else:
        try:
            cwd = (
                provision.provision(target, lane, branch)
                if branch
                else provision.provision(target, lane)
            )
            worktree_missing = False
        except Exception as exc:  # clone/worktree/gh failure — skip this assignment
            return {"error": f"provision failed: {exc}", "target": target, "agent": agent}
    # ONE spelling of the workspace for everything below: the roles, the gemini prompt, the
    # adapter's argv, the spawn cwd, the claim's `worktree`. The adapter resolves gemini's
    # `--add-dir`, so the provisioned path, unresolved, named the same directory with a second
    # string whenever the runtime sat behind a symlink, and the selftest's `--add-dir == cwd`
    # assertion failed under every macOS scratch runtime. (2026-10-02)
    cwd = adapters.workspace_path(cwd)
    # This agent's job is to commit in the provisioned worktree, so a scratch directory cannot stand
    # in for it the way it does for an offload. A worktree that is, or holds, `/`, the home dir or
    # the control plane means provisioning is broken: skip it like any provision failure, loudly.
    refused = adapters.broad_workspace_reason(cwd)
    if refused:
        return {
            "error": f"provisioned workspace refused: {refused}",
            "target": target,
            "agent": agent,
        }
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
    if assignment.get("selected_capability_id"):
        assignment["capability_fallback"] = "baseline"
        assignment["capability_dispatch_result"] = {
            "invoked": False,
            "accepted": False,
            "reason": "dry_run" if dry_run else "not_executed",
        }
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
        # The provisioned worktree is where this agent commits and pushes (provision.py), so a
        # sandboxed codex run is granted that worktree's git dir (adapters.codex_worktree_git_roots).
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
                commits_in_worktree=True,
            )
        else:
            argv = adapters.build_command(
                agent,
                prompt,
                mode,
                cwd=cwd,
                reasoning_effort=assignment.get("reasoning_effort"),
                requested_model=assignment.get("requested_model"),
                commits_in_worktree=True,
            )
    except ValueError:
        return None  # unknown agent — skip gracefully
    # Detached wrapper, in order: (1) PATH fix so local tools (agy, vibe, cursor-agent) resolve
    # without depending on the child agent's HOME; (2) run the agent in a subshell with writable
    # per-agent state/cache/log dirs. The subshell is LAST on purpose: `_spawn` reads the status of
    # this string as the agent's, and only then releases the claim, outside the subshell, so
    # claims.py still sees the real HOME/HANDOFF defaults.
    path_prefix = _path_prefix()
    # `set -a` auto-EXPORTS what the env file sets — the files are bare KEY=value (no `export`),
    # so a plain `. file` would set a shell var the child agent never inherits (the auth failure
    # the first demo hit). set -a around the source exports CURSOR_API_KEY / tokens to the agent.
    auth_prelude = _auth_prelude(agent)
    try:
        agent_prelude = _agent_runtime_prelude(agent)
    except StorageBlocked as exc:
        return {
            "error": str(exc),
            "agent": agent,
            "storage_context_refused": True,
            "process_started": False,
            "outcome_uncertain": False,
            "next_trigger": "reconcile_owned_storage_context",
        }
    # _net_hygiene_prelude lives INSIDE the agent subshell (release runs outside it, on the real env).
    wrapped = (
        f"{path_prefix}; ({_net_hygiene_prelude()}{agent_prelude}{auth_prelude}{shlex.join(argv)})"
    )
    return {
        "agent": agent,
        "mode": mode,
        "target": target,
        "lane": lane,
        "branch": branch,
        "task_type": task_type,
        "model": model,
        "cwd": str(cwd),
        "argv": argv,
        "wrapped": wrapped,
        "worktree_missing": worktree_missing,
        "feedback_mode": assignment.get("feedback_mode"),
        "review_loop": bool(assignment.get("review_loop")),
        "review_round": assignment.get("review_round", 1),
        "review_run_id": assignment.get("review_run_id"),
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
def _invoke_selected_compiled_capability(d: dict) -> dict | None:
    """Invoke supported selections after recording the run; keep refusal metadata explicit."""
    routing = dict(d.get("routing_metadata") or {})
    cap_id = routing.get("selected_capability_id")
    if not cap_id:
        return None
    version_id = routing.get("selected_capability_version_id")
    # Older or external callers may still forward a selection as exercised.
    # Remove it before invocation; only a successful receipt can add it back.
    ids = list(d.get("capability_ids") or [])
    versions = list(d.get("capability_version_ids") or [])
    if cap_id in ids:
        if versions and len(ids) != len(versions):
            raise ValueError("capability identity/version lineage length mismatch")
        d["capability_ids"] = [cid for cid in ids if cid != cap_id]
        d["capability_version_ids"] = [
            version for cid, version in zip(ids, versions) if cid != cap_id
        ]

    def fallback(reason: str, *, invoked: bool = False) -> dict:
        result = {"invoked": invoked, "accepted": False, "reason": reason}
        routing["capability_dispatch_result"] = result
        routing["capability_fallback"] = "baseline"
        d["routing_metadata"] = routing
        return result

    try:
        import capabilities
        import capability_lifecycle
        import capability_targets

        ledger = capabilities.load(capabilities.REG, create=False)
        cap = ledger.get(cap_id)
        if not cap:
            return fallback("unknown_capability")
        if not version_id or cap.get("capability_version_id") != version_id:
            return fallback("selected_version_mismatch")
        binding = capability_targets.get_binding(
            version_id, registry_path=capability_lifecycle.DEFAULT_TARGET_REGISTRY
        )
        if not binding:
            return fallback("target_binding_missing")
        # Workflow rails have a bounded producer and typed output consumer. Other
        # target kinds need their own dispatch inputs and consumption seams.
        if binding.get("kind") != "workflow":
            return fallback("unsupported_target_kind")
    except Exception as exc:
        return fallback(f"target_lookup_failed:{type(exc).__name__}")
    target = str(d.get("target") or "")
    # Fail before any producer runs or consumption edge is written: a missing
    # infrastructure version must not leave an accepted compiled edge for a
    # run that never starts (issue #639).
    ids = list(d.get("capability_ids") or [])
    versions = list(d.get("capability_version_ids") or [])
    if not versions:
        versions = [str((ledger.get(cid) or {}).get("capability_version_id") or "") for cid in ids]
    if len(ids) != len(versions):
        raise ValueError("capability identity/version lineage length mismatch")
    missing_versions = [cid for cid, version in zip(ids, versions) if cid != cap_id and not version]
    if missing_versions:
        raise ValueError(f"missing capability versions for {missing_versions}")
    trigger = {
        "target": target,
        "task_type": d.get("task_type"),
        "repository": target.split("#")[0] if "#" in target else target,
        "lane": d.get("lane") or "opener",
    }
    try:
        result = capability_lifecycle.invoke_compiled_target(
            str(cap_id),
            trigger=trigger,
            target_run_id=str(d["run_id"]),
            ledger_path=capabilities.REG,
            target_registry_path=capability_lifecycle.DEFAULT_TARGET_REGISTRY,
            inputs={},
            expected_version_id=str(version_id),
        )
    except Exception as exc:
        return fallback(f"invoke_failed:{type(exc).__name__}")
    if not result.get("invoked") or not result.get("accepted"):
        return fallback(
            str(result.get("reason") or "output_not_consumed"),
            invoked=bool(result.get("invoked")),
        )
    edge = result.get("causal_edge") or {}
    if not result.get("consumer_ref") or not edge.get("source_event_id"):
        return fallback("consumption_receipt_missing", invoked=True)
    # Append the compiled pair only after a successful receipt.
    pairs = [(cid, version) for cid, version in zip(ids, versions) if cid != cap_id]
    pairs.append((str(cap_id), str(version_id)))
    d["capability_ids"] = [cid for cid, _ in pairs]
    d["capability_version_ids"] = [version for _, version in pairs]
    routing.pop("capability_fallback", None)
    routing["capability_dispatch_result"] = {
        "invoked": True,
        "accepted": True,
        "capability_id": cap_id,
        "capability_version_id": version_id,
        "invocation_id": result["invocation_id"],
        "consumer_ref": result["consumer_ref"],
        **edge,
    }
    d["routing_metadata"] = routing
    return result


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


# Only lanes whose work is executed by delegate, not every prompt-schema match.
DELEGATE_LANE_CAPABILITIES = {
    key: TASK_TYPE_CAPABILITY[key] for key in ("testgen", "codemod", "cross_repo")
}


def _delegate_lane_heartbeat(d: dict) -> None:
    """Credit a started local delivery worker, including standalone delegate CLI calls.

    The tick-only production_heartbeat gate cannot observe coverage-autopilot's
    standalone calls. This execution edge supplies its own production authority:
    delegate calls it only after _spawn returns, with the run identity and target.
    Recording failure never prevents the already started worker from continuing.
    """
    capability_id = DELEGATE_LANE_CAPABILITIES.get(str(d.get("task_type") or ""))
    if not capability_id:
        return
    try:
        import capabilities

        capabilities.heartbeat(
            capability_id,
            "invocation",
            ref=d["target"],
            metadata={
                "source": "dispatcher.delegate",
                "run_id": d["run_id"],
                "task_type": d["task_type"],
                "agent": d["agent"],
            },
            path=capabilities.REG,
            idempotency_key=f"delegate:{d['run_id']}:{capability_id}",
        )
    except Exception as exc:
        print(f"warn: delegate lane heartbeat failed for {capability_id}: {exc}", file=sys.stderr)


def _spawn(d: dict) -> int:
    DISPATCH_LOG_DIR.mkdir(parents=True, exist_ok=True)
    safe = d["target"].replace("/", "__").replace("#", "_")
    logf = DISPATCH_LOG_DIR / f"{safe}.{d['agent']}.log"
    run_id = d.get("run_id") or f"{safe}-{d['agent']}-{time.time_ns()}"
    d["run_id"] = run_id
    started_ts = int(time.time())
    d["started_ts"] = started_ts
    if d.get("review_loop"):
        metadata = dict(d.get("routing_metadata") or {})
        metadata.update(review_loop=True, review_run_id=d.get("review_run_id") or run_id)
        d["routing_metadata"] = metadata
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
    lineage_required = bool(d.get("review_loop")) or any(
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
        try:
            feedback.record_run(*run_args, **run_kwargs)
            _invoke_selected_compiled_capability(d)
            if (d.get("routing_metadata") or {}).get("selected_capability_id"):
                feedback.record_run(
                    *run_args,
                    **{
                        **run_kwargs,
                        "routing_metadata": d["routing_metadata"],
                        "capability_ids": d.get("capability_ids"),
                        "capability_version_ids": d.get("capability_version_ids"),
                    },
                )
        except Exception:
            claims.release(d["target"], d["agent"])
            raise
    else:
        try:
            feedback.record_run(*run_args, **run_kwargs)
            _invoke_selected_compiled_capability(d)
            if (d.get("routing_metadata") or {}).get("selected_capability_id"):
                feedback.record_run(
                    *run_args,
                    **{
                        **run_kwargs,
                        "routing_metadata": d["routing_metadata"],
                        "capability_ids": d.get("capability_ids"),
                        "capability_version_ids": d.get("capability_version_ids"),
                    },
                )
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
                claims.release(d["target"], d["agent"])
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
        profile_decision = d.get("profile_decision")
        if profile_decision:
            assert profile_attempt_id is not None, "selected profiles create an attempt id"
            assert isinstance(profile_decision, dict), "profile_decision is a decision record"
            decision_id = profile_decision["decision_id"]
            assert isinstance(decision_id, str), "profile_decision decision_id is persisted as text"
            feedback.attach_profile_attempt_to_decision(decision_id, profile_attempt_id)
    adapters.record_ledger(
        d["agent"],
        count=1,
        cost_usd=0.0,
        event="start",
        pacing_decision=d.get("pacing_decision"),
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
    # The steps' interpreter is named, never looked up on the login shell's PATH: that lookup
    # killed both steps in the rc=137 runs (adapters.wrapper_python has the evidence).
    step_python = adapters.wrapper_python()
    complete_cmd = shlex.join(
        [
            step_python,
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
            # The worktree the agent ran in: completion records the branches it pushed from there,
            # which outcome ingest walks before the name-pattern candidates (pushed_branches.py).
            "--workspace",
            str(d.get("cwd") or ""),
        ]
    )
    # Marker BEFORE the python completion: the python step was SIGKILLed in the wild (audit F2).
    # Here the cause, found 2026-10-05, was the PATH lookup `step_python` replaces. The marker stays
    # first so that ANY later death of the step still leaves latency/exit for reconcile to backfill.
    marker_cmd = adapters.done_marker_cmd(
        run_id, logf, "orch_dispatch_rc", release_rc_var="orch_release_rc"
    )
    # The AGENT's status is read first, straight after `d["wrapped"]` (the agent subshell is its
    # last command), and only then is the claim released. The release has a status of its own (0
    # it found the claim, 1 it was gone, 137 it was SIGKILLed), and until 2026-10-04 `$?` was read
    # AFTER it, so the marker's rc, `complete --exit-code` and this wrapper's exit all reported the
    # release: 106 of 106 live dispatch markers, 31 of them a release SIGKILLed after the agent had
    # finished, which reconcile then classed as the agent's signal death. The release still always
    # runs, and its status rides in the marker as `release_rc`, beside the agent's, never as it.
    # `release_rc` 137 is what counts a step killed again from here on.
    release = shlex.join([step_python, str(CLAIMS_PY), "release", d["target"], d["agent"]])
    review_after = ""
    if d.get("review_loop"):
        # This is deliberately after release and completion.  A PR URL is not
        # terminal evidence; review_loop independently rechecks claim state.
        review_after = (
            "; "
            + shlex.join(
                [
                    step_python,
                    str(ORCH_DIR / "review_loop.py"),
                    "after-writer",
                    "--target",
                    d["target"],
                    "--implementer",
                    d["agent"],
                    "--lane",
                    d["lane"],
                    "--run-id",
                    str(d.get("review_run_id") or run_id),
                    "--writer-run-id",
                    run_id,
                    "--log",
                    str(logf),
                    "--round",
                    str(d.get("review_round", 1)),
                ]
            )
            + ' --writer-exit "$orch_dispatch_rc"'
        )
    wrapped = (
        f"{d['wrapped']}; orch_dispatch_rc=$?; {release}; orch_release_rc=$?; {marker_cmd}; "
        f'{complete_cmd} --exit-code "$orch_dispatch_rc"'
        + review_after
        + "; exit $orch_dispatch_rc"
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
    if d["agent"] == "muse":
        # A delegated run is detached, so its exit and duration are not known here; the row counts
        # the exec (what the plan's allowance is spent on) with rc "spawned" and 0 seconds.
        _record_muse_usage(started_ts, "delegate", "spawned", 0)
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


_linked_pr_read_error: ContextVar[str] = ContextVar("linked_pr_read_error", default="")
LINKED_PR_READ_TIMEOUT_S = 30


def linked_open_prs(target: str) -> list[int] | None:
    """Read all open PR links before a local claim; incomplete evidence is unknown.

    One paginated REST listing supplies bodies/branches and one paginated GraphQL
    operation supplies closing references. Never fall back to a GraphQL PR list.
    The diagnostic is context-local so concurrent delegates cannot swap reasons.
    """
    _linked_pr_read_error.set("")
    match = re.fullmatch(r"([^/\s]+)/([^/#\s]+)#([1-9][0-9]*)", target)
    if not match:
        _linked_pr_read_error.set("expected owner/repo#N")
        return None
    owner, name, number = match.groups()
    repo = f"{owner}/{name}"

    def read(args: list[str]) -> list:
        result = subprocess.run(
            ["gh", "api", *args, "--paginate", "--slurp"],
            capture_output=True,
            text=True,
            timeout=LINKED_PR_READ_TIMEOUT_S,
            check=False,
        )
        if result.returncode:
            raise ValueError(f"gh exit {result.returncode}: {result.stderr.strip()}")
        pages = json.loads(result.stdout)
        if not isinstance(pages, list) or not pages:
            raise ValueError("missing GitHub response pages")
        return pages

    def pr_number(value: Any) -> int:
        if type(value) is not int or value <= 0:
            raise ValueError("invalid PR/issue number")
        return value

    try:
        rest_pages = read([f"repos/{repo}/pulls?state=open&per_page=100"])
        linked = set()
        rest_numbers = set()
        closing = re.compile(
            r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s*:?\s*"
            rf"(?:#|{re.escape(repo)}#|https://github\.com/{re.escape(repo)}/issues/)"
            rf"{number}\b",
            re.IGNORECASE,
        )
        prefixes = "|".join(
            re.escape(prefix)
            for prefix in sorted({*AGENT_PERSONAS, "claude", "orch", "orchestrator"})
        )
        branch = re.compile(rf"^(?:{prefixes})/issue-{number}(-.*)?$")
        for page in rest_pages:
            if not isinstance(page, list):
                raise ValueError("invalid REST PR page")
            for pr in page:
                n = pr_number(pr["number"])
                body, head = pr["body"], pr["head"]["ref"]
                if (body is not None and not isinstance(body, str)) or not isinstance(head, str):
                    raise ValueError("invalid PR body or head branch")
                rest_numbers.add(n)
                if closing.search(body or "") or branch.fullmatch(head):
                    linked.add(n)
        query = """query($owner:String!,$name:String!,$endCursor:String){
          repository(owner:$owner,name:$name){
            pullRequests(states:OPEN,first:100,after:$endCursor){
              pageInfo{hasNextPage endCursor}
              nodes{number closingIssuesReferences(first:100){
                pageInfo{hasNextPage} nodes{number repository{nameWithOwner}}
              }}
            }
          }
        }"""
        graph_pages = read(
            ["graphql", "-f", "query=" + query, "-f", "owner=" + owner, "-f", "name=" + name]
        )
        graph_numbers = set()
        cursors = set()
        for index, page in enumerate(graph_pages):
            if page.get("errors"):
                raise ValueError(f"GraphQL errors: {page['errors']}")
            connection = page["data"]["repository"]["pullRequests"]
            info = connection["pageInfo"]
            if type(info["hasNextPage"]) is not bool:
                raise ValueError("invalid GraphQL pagination")
            if info["hasNextPage"] != (index < len(graph_pages) - 1):
                raise ValueError("incomplete GraphQL PR pagination")
            if info["hasNextPage"]:
                cursor = info["endCursor"]
                if not isinstance(cursor, str) or not cursor or cursor in cursors:
                    raise ValueError("invalid GraphQL cursor")
                cursors.add(cursor)
            if not isinstance(connection["nodes"], list):
                raise ValueError("invalid GraphQL PR nodes")
            for pr in connection["nodes"]:
                n = pr_number(pr["number"])
                graph_numbers.add(n)
                refs = pr["closingIssuesReferences"]
                if refs["pageInfo"]["hasNextPage"] is not False:
                    raise ValueError(f"incomplete closingIssuesReferences for PR #{n}")
                if not isinstance(refs["nodes"], list):
                    raise ValueError("invalid closing reference nodes")
                for ref in refs["nodes"]:
                    ref_number = pr_number(ref["number"])
                    ref_repo = ref["repository"]["nameWithOwner"]
                    if not isinstance(ref_repo, str):
                        raise ValueError("invalid closing reference repository")
                    if ref_number == int(number) and ref_repo.casefold() == repo.casefold():
                        linked.add(n)
        if rest_numbers != graph_numbers:
            raise ValueError("open PR population changed between REST and GraphQL; retry")
        return sorted(linked)
    except (
        OSError,
        subprocess.SubprocessError,
        ValueError,
        KeyError,
        TypeError,
        AttributeError,
    ) as exc:
        _linked_pr_read_error.set(str(exc))
        return None


def delegate(
    agent: str,
    target: str,
    lane: str,
    prompt: str,
    mode: str | None = None,
    task_type: str = "implement",
    profile_id: str | None = None,
    *,
    branch_prefix: str | None = None,
    influenced_by_role_run_ids=None,
    influenced_by_skill_event_ids=None,
    influenced_by_workflow_ids=None,
    capability_ids=None,
    capability_version_ids=None,
    acceptance_gate_ids=None,
    review_loop: bool = False,
    review_round: int = 1,
    review_run_id: str | None = None,
    continue_pr: int | None = None,
) -> dict:
    """One ad-hoc delegation for the assessing orchestrator seat: claim + provision + spawn
    ONE cheap agent with the orchestrator's OWN crafted prompt (detached, PATH+auth+release
    wrapper). Returns {pid, log, worktree} to monitor, or {error}. This is the seat's hand —
    it decides WHO/WHAT/HOW (the prompt); this just executes safely. `task_type` is recorded for
    the feedback loop (the REAL kind of work, not a generic 'delegated')."""
    if review_loop:
        from review_loop import REVIEW_LOOP_MAX_ROUNDS

        if (
            isinstance(review_round, bool)
            or not isinstance(review_round, int)
            or not 1 <= review_round <= REVIEW_LOOP_MAX_ROUNDS
        ):
            return {"error": "review round outside bounded review loop"}
    explicit_mode = mode
    if mode is None:
        mode = (
            "composer"
            if agent == "cursor"
            else ("mid" if agent == "codex" and lane == "closer" else "full")
        )
    codex_override_effort = None
    if agent == "codex" and profile_id is None:
        default_profile_id = execution_profiles.default_codex_delegate_profile(
            task_type, lane, explicit_mode
        )
        if execution_profiles.codex_operator_tier_override(mode):
            codex_override_effort = execution_profiles.get_profile(default_profile_id)[
                "reasoning_effort"
            ]
        else:
            profile_id = default_profile_id
    if profile_id:
        try:
            requested_profile = execution_profiles.get_profile(profile_id)
        except ValueError as exc:
            return {"error": str(exc)}
        if requested_profile["agent"] != agent:
            return {"error": f"profile {profile_id} does not belong to {agent}"}
    linked = linked_open_prs(target)
    if linked is None:
        reason = _linked_pr_read_error.get() or "GitHub linked-PR read failed"
        return {"error": f"linked: unknown ({reason}); retry when GitHub access recovers"}
    if (linked or continue_pr is not None) and (
        type(continue_pr) is not int or continue_pr not in linked
    ):
        if not linked:
            return {
                "error": f"linked: continue_pr={continue_pr!r} is not an open PR linked to {target}"
            }
        names = ", ".join(f"#{n}" for n in linked)
        return {
            "error": f"linked: open PR {names or '(none)'} close {target}; "
            "pass continue_pr=<N> to work on one of them"
        }
    claims.reap_stale()
    if not claims.claim(target, agent):
        h = claims.holder(target)
        return {"error": f"target already claimed by {(h or {}).get('agent') or 'another agent'}"}
    a: dict[str, object] = {
        "agent": agent,
        "target": target,
        "lane": lane,
        "task_type": task_type,
        "mode": mode,
        "prompt": prompt,
        "feedback_mode": "local",
        "branch_prefix": branch_prefix,
        "influenced_by_role_run_ids": list(influenced_by_role_run_ids or []),
        "influenced_by_skill_event_ids": list(influenced_by_skill_event_ids or []),
        "influenced_by_workflow_ids": list(influenced_by_workflow_ids or []),
        "capability_ids": list(capability_ids or []),
        "capability_version_ids": list(capability_version_ids or []),
        "acceptance_gate_ids": list(acceptance_gate_ids or []),
        "review_loop": review_loop,
        "review_round": review_round,
        "review_run_id": review_run_id,
    }
    if codex_override_effort:
        a["reasoning_effort"] = codex_override_effort
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
    lane_capability = DELEGATE_LANE_CAPABILITIES.get(str(d.get("task_type") or ""))
    if lane_capability:
        d["capability_ids"] = list(dict.fromkeys([*d.get("capability_ids", []), lane_capability]))
    safe = target.replace("/", "__").replace("#", "_")
    d["pid"] = _spawn(d)
    _delegate_lane_heartbeat(d)
    # Close the feedback loop for LOCAL delegations: re-record the decision keyed by mode='local'
    # (so outcomes.ingest_outcomes(mode='local') discovers it and resolves the resulting PR by the
    # recorded pushed branch, with deterministic candidates as fallback) with the REAL task_type;
    # keep the agent-mode in
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
        # A PR from this dispatch is deliberately ineligible for a normal
        # merge path until review_loop has produced MERGE-WHEN-GREEN.
        **({"review_loop_status": "pending"} if review_loop else {}),
    }


def _default_offload_timeout(agent: str, requested: float | None) -> float:
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
    workspace = adapters.workspace_path(cwd)
    return (
        f"{prompt.rstrip()}\n\n"
        f"GEMINI WORKSPACE: use exactly {workspace} for all file reads, edits, tests, "
        "and shell commands. Do not read from or modify any other checkout of this repo, "
        "including Dropbox/CloudStorage checkouts or the process launch directory if it differs."
    )


def _align_gemini_print_timeout(argv: list[str], timeout: float) -> None:
    """Keep agy's print wait inside the outer deadline, retaining a receipt margin.

    agy accepts duration values in seconds (the CLI help documents ``0s`` as its
    wait-forever default). Rounding this budget down to whole minutes made a 119s
    dispatch self-timeout after only 60s, while the outer subprocess still had 59s
    left to wait for a process that had already abandoned its in-progress turn.
    """
    if "--print-timeout" not in argv:
        return
    budget = max(1, timeout - 15)
    argv[argv.index("--print-timeout") + 1] = f"{budget}s"


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
        text = path.read_text(errors="replace")
    except OSError:
        return ""
    # Masked WHOLE and then cut: a tail cut first could split a token and keep a piece of it.
    return credential_redaction.redact(text)[-max_chars:]


def _agy_reports_turn_in_progress(log_text: str) -> bool:
    """Recognize agy's explicit partial-turn timeout diagnostic, not generic timeouts.

    This is evidence that the CLI stopped waiting while its provider turn was still
    active. It does not establish success, failure, cancellation, or served identity.
    """
    return bool(
        re.search(
            r"\bprint timeout after [^\r\n;]{1,48} with turn in progress; returning partial output\b",
            log_text or "",
            flags=re.IGNORECASE,
        )
    )


def _public_model_metadata(
    agent: str,
    requested_model: str | None,
    observed_model: str | None,
    observed_source: str | None,
) -> dict[str, str | None]:
    """Separate requested/CLI-selected labels from evidence of the served model."""
    if agent == "gemini":
        cli_selected = observed_model if observed_source == "agy_log_selected_override" else None
        return {
            "requested_model": requested_model,
            "served_model": None,
            "served_model_status": "unknown",
            "cli_selected_model": cli_selected,
            "cli_selected_model_source": (
                "agy_log_selected_override" if cli_selected is not None else None
            ),
        }
    return {
        "requested_model": requested_model,
        "served_model": observed_model,
        "served_model_status": "observed" if observed_model else "unknown",
    }


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
        profiles = [
            p
            for p in execution_profiles.profiles_for_agent(agent, transport="offload")
            if adapters.automatic_profile_within_cap(p)
        ]
        if not profiles:
            return None
        if agent == "codex" and execution_profiles.codex_operator_tier_override(mode):
            return None
        if agent == "codex":
            preferred_profile = execution_profiles.get_profile(
                execution_profiles.default_codex_profile("offload", mode)
            )
            return (
                preferred_profile
                if adapters.automatic_profile_within_cap(preferred_profile)
                else None
            )
        # HONOUR THE TIER THE CODEBASE ALREADY CHOSE. `DEFAULT_OFFLOAD_TIER` is "mid" with a comment
        # that had already diagnosed this exact waste -- "a codex offload burned Sol and a gemini
        # offload burned Pro" -- and selecting from ALL offload-capable profiles silently overrode
        # it: with only a Pro profile registered, every gemini read was routed to the reasoning
        # tier, and with three codex profiles it picked whichever sorted first.
        #
        # Claude/Gemini tiers are spend policies. An automatic offload must use a
        # profile for the live resolved tier model, or refuse before dispatch.
        # Other agents retain the legacy fallback for their unprofiled/single-lane
        # paths.
        tier = mode if mode in adapters.MODEL_TIER_NAMES else adapters.DEFAULT_OFFLOAD_TIER
        if agent == "claude":
            tier = adapters.effective_tier(agent, tier)
        if agent == "gemini":
            tier = adapters.effective_tier(agent, tier)
            health = adapters.model_health(agent, tier)
            if not health["resolvable"]:
                return None
            tier_model = health["model"]
        else:
            tier_model = adapters.resolve_model(agent, tier)
        tiered = [p for p in profiles if tier_model and p["requested_model"] == tier_model]
        if agent in {"claude", "gemini"} and not tiered:
            return None
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


# How many names one offload tries before it reports the log directory as unusable. A name is taken
# only by a file already on disk and each retry moves one nanosecond on, so reaching this needs that
# many consecutive names to exist.
OFFLOAD_LOG_CLAIM_ATTEMPTS = 1000
# A shell wrapper repeats an inline prompt in its ``bash -lc`` argument. Keep normal Gemini print
# mode for compact, already-qualified prompts, but transport a large collected packet through agy's
# documented NDJSON stdin protocol before either exec boundary approaches ARG_MAX.
GEMINI_STREAM_PROMPT_THRESHOLD_BYTES = 64 * 1024


def _claim_offload_log(agent: str) -> tuple[str, Path]:
    """One offload's run_id and log file, claimed together so that no other offload holds either.

    Both used to be read from `time.time_ns()` separately, and that clock is microsecond-granular on
    macOS (every value ends in 000), so two offloads started in the same microsecond shared them.
    Measured from the capacity ledger on 2026-10-05: 4 of 4,517 offload logs were written by two
    runs, and 3 offload run_ids were carried by 7 runs. A shared log holds both run headers before
    either run's output, so `ledger_reconcile._log_segment` read nothing for the run whose header
    came first and both outputs for the other. A shared run_id is one `runs` row in the Brain
    (INSERT OR REPLACE), so the second run replaced the first.

    The log is created with O_EXCL, so a name belongs to whichever offload creates it first, in this
    process or another one, and a name already on disk moves the claim one nanosecond on. The run_id
    is built from the same number, so it is as unique as the log name, and either one names the
    other.
    """
    DISPATCH_LOG_DIR.mkdir(parents=True, exist_ok=True)
    ns = time.time_ns()
    for _ in range(OFFLOAD_LOG_CLAIM_ATTEMPTS):
        logf = DISPATCH_LOG_DIR / f"offload.{agent}.{ns}.log"
        try:
            os.close(os.open(logf, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666))
        except FileExistsError:
            ns += 1
            continue
        return f"offload:{agent}:{ns}", logf
    raise RuntimeError(
        f"no free offload log name in {DISPATCH_LOG_DIR}: offload.{agent}.<ns>.log was taken "
        f"for {OFFLOAD_LOG_CLAIM_ATTEMPTS} consecutive values ending at {ns - 1}"
    )


def _offload_subject(
    subject_id: str | None, research_round: str | None
) -> tuple[str | None, str | None]:
    """(subject, source) for one offload run: its registered research round's subject, else the
    caller's `subject_id`, else (None, None).

    The round wins because `feedback`'s completion-identity path already keys a round-bound run by
    its round's subject; a second rule here would let the two disagree about one run. Never read
    from the prompt or notes: an unnamed run stays unkeyed. The round lookup must not prevent the
    work, so an unreadable registry reads as no round."""
    caller = str(subject_id or "").strip() or None
    if research_round:
        try:
            import research_subjects  # local: research_subjects imports feedback

            identity = research_subjects.round_identity(research_round)
        except Exception:
            identity = None
        if identity and identity.get("subject_id"):
            round_subject = str(identity["subject_id"])
            if caller and caller != round_subject:
                print(
                    f"note: offload subject {caller!r} ignored; research round "
                    f"{research_round!r} is registered to {round_subject}",
                    file=sys.stderr,
                )
            return round_subject, "research_round"
    return (caller, "caller") if caller else (None, None)


def _public_process_observation(observation):
    """Preserve process evidence without persisting raw prompt-bearing argv."""
    if not isinstance(observation, dict):
        return None
    public = {k: v for k, v in observation.items() if k not in {"provider_argv", "transport_argv"}}
    for name in ("provider_argv", "transport_argv"):
        argv = observation.get(name)
        if isinstance(argv, list) and all(isinstance(arg, str) for arg in argv):
            public[name + "_argc"] = len(argv)
            public[name + "_sha256"] = hashlib.sha256(
                b"".join(os.fsencode(arg) + b"\0" for arg in argv)
            ).hexdigest()
    return public


class ObservedOffloadFailure(RuntimeError):
    """A transport was spawned before trusted observation failed; never prelaunch."""

    def __init__(self, reason, observation, returncode, stdout, stderr):
        super().__init__(reason)
        self.observation, self.returncode = observation, returncode
        self.stdout, self.stderr = stdout, stderr


class _ObservedOutputLimit(RuntimeError):
    pass


@dataclass
class _ControlledDataOnly:
    limits: dict
    profile: dict
    root: Path
    cwd: Path
    operation_id: str
    env: dict[str, str] = field(repr=False)
    token: str = field(repr=False)
    observation: dict | None = None
    execution: dict | None = None
    native_sources: dict = field(default_factory=dict)
    pool_reservation: dict = field(default_factory=dict)
    runtime_cleanup_attempted: bool = False


_CONTROLLED_DATA_ONLY: ContextVar[_ControlledDataOnly | None] = ContextVar(
    "controlled_data_only_execution", default=None
)


def _controlled_module_sha(module) -> str:
    source = getattr(module, "__file__", None)
    if not isinstance(source, str):
        raise ValueError("controlled source module identity unavailable")
    with Path(source).open("rb") as stream:
        raw = stream.read(8_000_001)
    if not raw or len(raw) > 8_000_000:
        raise ValueError("controlled source module exceeds bound")
    return hashlib.sha256(raw).hexdigest()


def _controlled_cleanup_result(context: _ControlledDataOnly, result: dict) -> None:
    """Finalize owned filesystem cleanup before immutable capture retention."""
    if context.runtime_cleanup_attempted:
        return
    context.runtime_cleanup_attempted = True
    try:
        shutil.rmtree(context.root)
    except OSError as error:
        result["data_only_private_runtime"] = {
            "path": str(context.root),
            "cleanup": "held_owned_cleanup_unverified",
            "error_type": type(error).__name__,
        }
        result["semantic_promotion_blocked"] = True
        result["retry_safe"] = False
    else:
        result["data_only_private_runtime"] = {
            "path": str(context.root),
            "cleanup": "removed_owned_private_runtime",
        }


def _controlled_data_only_entry(function):
    """Keep the existing accounting/supervisor body inside an explicit runtime.

    The supplier is an integration dependency, not a credential reader. It must
    return a context manager yielding an already-authorized existing OAuth token.
    No environment fallback, shell sourcing, login or alternate auth is allowed.
    """
    signature = inspect.signature(function)

    @functools.wraps(function)
    def invoke(*args, **kwargs):
        bound = signature.bind(*args, **kwargs)
        values = bound.arguments
        if values.get("data_only") is not True:
            if any(
                values.get(key) is not None
                for key in ("data_only_limits", "trusted_auth_supplier", "data_only_operation_id")
            ):
                return {
                    "agent": values.get("agent"),
                    "exit": 2,
                    "output": "",
                    "run_id": None,
                    "error": "controlled data-only inputs require data_only=True",
                    "process_started": False,
                    "outcome_uncertain": False,
                }
            return function(*args, **kwargs)

        def refused(reason):
            return {
                "agent": values.get("agent"),
                "exit": 2,
                "output": "",
                "run_id": None,
                "error": reason,
                "process_started": False,
                "outcome_uncertain": False,
                "unsupported_assessment_tool_boundary": True,
            }

        deadline = values.get("request_deadline_mono")
        if deadline is not None and (
            type(deadline) not in (int, float)
            or not math.isfinite(deadline)
            or deadline <= time.monotonic()
        ):
            return refused("original controlled request deadline exhausted or invalid")

        # The existing global kill switch must precede private runtime/auth.
        if os.environ.get("ORCH_OFFLOAD_DISABLED", "").strip() == "1":
            return {**refused("offload disabled by ORCH_OFFLOAD_DISABLED=1"), "disabled": True}

        import closer_data_only_execution as execution
        import closer_resources
        import closer_role_qualification as roles

        try:
            limits = execution.validate_limits(values.get("data_only_limits"))
            supplier = values.get("trusted_auth_supplier")
            if not callable(supplier):
                raise ValueError("explicit trusted_auth_supplier is required")
            if values.get("agent") != "claude" or values.get("mode") != "assess":
                raise ValueError("controlled data-only requires explicit Claude assess")
            if values.get("write_boundary") is not None:
                raise ValueError("controlled data-only cannot inherit a generic write boundary")
            if values.get("max_network_retries") not in (None, 0):
                raise ValueError("data-only transport forbids automatic retries")
            declared_timeout = values.get("timeout")
            if declared_timeout is not None and (
                type(declared_timeout) not in (int, float) or not 0 < declared_timeout <= 86400
            ):
                raise ValueError("invalid explicit controlled timeout")
            expected_role = {
                "closer_interpret": "bounded_classification",
                "closer_exception": "strong_criterion_adjudication",
            }.get(values.get("task_type"))
            if expected_role != limits["role"]:
                raise ValueError("explicit task and execution limits role differ")
            if not values.get("profile_id"):
                raise ValueError("controlled data-only requires an explicit registered profile")
            profile = roles.profile_identity(execution_profiles.get_profile(values["profile_id"]))
            execution.profile_identity(profile)
            # Size admission is deliberately hash-free: the borrowed token must
            # be rejected from input before any prompt digest can be retained.
            prompt = values["prompt"]
            if (
                type(prompt) is not str
                or not prompt
                or len(prompt.encode("utf-8")) > limits["max_prompt_bytes"]
            ):
                raise ValueError("complete controlled prompt exceeds explicit admission")
            operation_id = values.get("data_only_operation_id")
            if (
                type(operation_id) is not str
                or not operation_id
                or len(operation_id) > 256
                or any(ord(c) < 32 for c in operation_id)
            ):
                raise ValueError("exact original data-only operation identity is required")
            if not all(
                callable(values.get(key))
                for key in (
                    "process_starting_callback",
                    "process_started_callback",
                    "process_finished_callback",
                )
            ):
                raise ValueError("existing observed process lifecycle callbacks are required")
            if not closer_resources.finite_pool_has_active_reservations():
                raise ValueError("original durable finite shared-pool reservation is required")
            pool_binding = closer_resources.finite_pool_current_binding()
            if pool_binding != {
                "frontier_id": values.get("finite_frontier_id"),
                "target": values.get("finite_frontier_target"),
            }:
                raise ValueError("original finite pool binding differs")
            # A frontier/target alone does not prove the actual account lane.
            slots = [
                item
                for item in closer_resources._pool_slots()
                if item["record"].get("lane") == hashlib.sha256(b"claude-subscription").hexdigest()
            ]
            if len(slots) != 1:
                raise ValueError("original Claude subscription lane is not held")
            slot = slots[0]
            record = slot["record"]
            if (
                slot["handle"].closed
                or record.get("agent") != "claude"
                or record.get("profile_id") != profile["profile_id"]
                or record.get("frontier_id") != values.get("finite_frontier_id")
                or record.get("target") != values.get("finite_frontier_target")
            ):
                raise ValueError("original pool profile or owner binding differs")
            pool_reservation = {
                key: record[key]
                for key in (
                    "reservation_id",
                    "lane",
                    "frontier_id",
                    "target",
                    "agent",
                    "profile_id",
                )
            }
            # Verify dependency availability before any runtime/credential effect.
            from transport_saved_result import normalize_claude_result

            if not callable(normalize_claude_result):
                raise ValueError("reviewed native-result collector unavailable")
        except (ImportError, OSError, ValueError, TypeError, KeyError):
            return refused("controlled data-only contract/auth/lifecycle admission unavailable")

        from storage_lifecycle import owned_agent_tmpdir

        root = None
        context = None
        token = ""
        entered_body = False
        result = None
        try:
            parent = owned_agent_tmpdir("claude")
            root = Path(tempfile.mkdtemp(prefix="controlled-data-only.", dir=parent)).resolve()
            for leaf in ("home", "cache", "config", "tmp", "claude-config", "cwd"):
                (root / leaf).mkdir(mode=0o700)
            cwd = root / "cwd"
            with supplier() as token:
                if type(token) is not str or not token:
                    raise ValueError("trusted borrower supplied no literal existing token")
                if any(type(value) is str and token in value for value in values.values()):
                    raise ValueError("borrowed credential entered retained caller metadata")
                env = execution.isolated_environment(
                    str(root), cwd=str(cwd), existing_oauth_token=token, limits=limits
                )
                context = _ControlledDataOnly(limits, profile, root, cwd, operation_id, env, token)
                context.pool_reservation = pool_reservation
                import transport_qualification
                import transport_saved_result

                context.native_sources = {
                    name: _controlled_module_sha(module)
                    for name, module in (
                        ("transport_saved_result.py", transport_saved_result),
                        ("transport_qualification.py", transport_qualification),
                    )
                }
                marker = _CONTROLLED_DATA_ONLY.set(context)
                try:
                    entered_body = True
                    result = function(*args, **kwargs)
                    return result
                finally:
                    _CONTROLLED_DATA_ONLY.reset(marker)
                    context.env.clear()
                    context.token = ""
        except Exception as error:
            if not entered_body:
                return refused("controlled auth/runtime unavailable:" + type(error).__name__)
            raise
        finally:
            token = ""
            # Preserve an uncertain invocation's private runtime for the existing
            # owner/reconciler. A new call never reuses it or repeats its process.
            if root is not None and not (context and context.runtime_cleanup_attempted):
                if not entered_body or (result is not None and not result.get("outcome_uncertain")):
                    try:
                        shutil.rmtree(root)
                    except OSError as cleanup_error:
                        if result is not None:
                            result["data_only_private_runtime"] = {
                                "path": str(root),
                                "cleanup": "held_owned_cleanup_unverified",
                                "error_type": type(cleanup_error).__name__,
                            }
                            result["semantic_promotion_blocked"] = True
                            result["retry_safe"] = False
                elif result is not None:
                    result["data_only_private_runtime"] = {
                        "path": str(root),
                        "cleanup": "deferred_original_outcome_unknown",
                    }

    return invoke


def _finite_pool_fence(observation):
    """Return only trusted process identity for an unverified finite cleanup."""
    if not isinstance(observation, dict) or observation.get("cleanup_terminal") is not False:
        return None
    pgid = observation.get("process_group", observation.get("pid"))
    if (
        observation.get("source") != "trusted-popen-transport/v1"
        or type(pgid) is not int
        or pgid <= 1
        or observation.get("pid") != pgid
        or not isinstance(observation.get("os_started"), str)
        or not observation.get("os_started")
        or not isinstance(observation.get("run_id"), str)
        or not observation.get("run_id")
        or not isinstance(observation.get("finite_frontier_id"), str)
        or not observation.get("finite_frontier_id")
        or not isinstance(observation.get("finite_frontier_target"), str)
        or not observation.get("finite_frontier_target")
    ):
        return None
    return {
        "process_group": pgid,
        "run_id": observation["run_id"],
        "agent": observation.get("agent"),
        "profile_id": observation.get("profile_id"),
        "process_birth": observation["os_started"],
        "frontier_id": observation.get("finite_frontier_id"),
        "target": observation.get("finite_frontier_target"),
        "cleanup_terminal": False,
        "cleanup_error": observation.get("cleanup_error"),
    }


def run_observed_offload(
    command,
    *,
    callback,
    observation,
    cwd,
    timeout,
    stdin,
    finished_callback=None,
    starting_callback=None,
    env=None,
    output_limit=None,
):
    """Optional observed transport; default offloads retain subprocess.run.

    The Popen handle names the transport shell, not a claimed provider process.
    Its private process group permits bounded cleanup without signalling outsiders.
    """
    started = time.monotonic()
    if starting_callback is not None:
        starting_callback(observation)
    try:
        child = subprocess.Popen(
            command,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            stdin=stdin,
            start_new_session=True,
            **({"env": env} if env is not None else {}),
        )
    except BaseException:
        if starting_callback is not None:
            import closer_resources

            closer_resources.finite_pool_no_start(observation)
        raise
    observed = {
        **observation,
        "source": "trusted-popen-transport/v1",
        "pid": child.pid,
        "process_group": child.pid,
        "transport_argv": list(command),
    }
    controlled_context = _CONTROLLED_DATA_ONLY.get() if env is not None else None
    if controlled_context is not None:
        controlled_context.observation = observed
    token = env.get("CLAUDE_CODE_OAUTH_TOKEN", "") if env is not None else ""

    def safe_output(value):
        if isinstance(value, bytes):
            try:
                text = value.decode("utf-8")
            except UnicodeError:
                observed["invalid_utf8_output"] = True
                text = value.decode("utf-8", errors="replace")
        else:
            text = value or ""
        if token and token in text:
            observed["borrowed_credential_output"] = True
            return text.replace(token, "[REDACTED]")
        return text

    captured = {"stdout": bytearray(), "stderr": bytearray()}

    def bounded_communicate(timeout):
        deadline = time.monotonic() + timeout
        count = 0
        with selectors.DefaultSelector() as selector:
            for pipe, kind in ((child.stdout, "stdout"), (child.stderr, "stderr")):
                os.set_blocking(pipe.fileno(), False)
                selector.register(pipe, selectors.EVENT_READ, kind)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(command, timeout)
                for key, _ in selector.select(min(0.05, remaining)):
                    chunk = os.read(key.fileobj.fileno(), min(8192, output_limit - count + 1))
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    room = output_limit - count
                    captured[key.data].extend(chunk[:room])
                    count += len(chunk)
                    chunk = b""
                    if count > output_limit:
                        raise _ObservedOutputLimit("controlled native output exceeds bound")
        child.wait(timeout=max(0.001, deadline - time.monotonic()))
        return tuple(safe_output(bytes(captured[k])) for k in ("stdout", "stderr"))

    def stop_known_group():
        cleanup_error = None
        try:
            # The trusted spawn observer can veto cleanup after an exact denial
            # elsewhere in this same supervision. Never route around that denial.
            if observed.get("cleanup_veto") is not None:
                veto = observed["cleanup_veto"]
                observed["cleanup_error"] = {
                    **veto,
                    "process_group": child.pid,
                    "source": "trusted-supervisor-cleanup-veto/v1",
                }
            else:
                os.killpg(child.pid, 9)
        except OSError as exc:
            cleanup_error = {
                "type": type(exc).__name__,
                "errno": exc.errno,
                "reason": str(exc),
                "process_group": child.pid,
            }
            observed["cleanup_error"] = cleanup_error
        latest = ("", "")
        group_absent_proven = False
        stop_deadline = time.monotonic() + (0.25 if cleanup_error else 2.0)
        while time.monotonic() < stop_deadline:
            try:
                if output_limit is not None:
                    # Controlled output remains bounded even during failed cleanup.
                    # Never switch back to communicate()'s unbounded buffers.
                    child.wait(timeout=min(0.2, max(0.01, stop_deadline - time.monotonic())))
                    latest = tuple(safe_output(bytes(captured[k])) for k in ("stdout", "stderr"))
                else:
                    latest = child.communicate(
                        timeout=min(0.2, max(0.01, stop_deadline - time.monotonic()))
                    )
            except subprocess.TimeoutExpired as pending:

                def decoded(value):
                    return (
                        value.decode(errors="replace") if isinstance(value, bytes) else value or ""
                    )

                latest = (decoded(pending.output), decoded(pending.stderr))
                if cleanup_error:
                    break
                continue
            try:
                # The session/process group belongs exclusively to this Popen.
                # Keep the callback (and its resource lease) alive until the
                # whole owned group is gone, even when pipe holders outlive the
                # direct shell process.
                os.killpg(child.pid, 0)
            except ProcessLookupError:
                group_absent_proven = True
                break
            except OSError:
                # An unreadable liveness result is not terminal proof. Bound
                # cleanup and persist an uncertainty fence instead of waiting
                # indefinitely on a denied or unreadable process-group probe.
                cleanup_error = cleanup_error or {
                    "type": "LivenessUnverified",
                    "errno": None,
                    "reason": "owned process-group liveness could not be verified",
                    "process_group": child.pid,
                }
                observed["cleanup_error"] = cleanup_error
                break
            time.sleep(0.05)
        if not group_absent_proven:
            try:
                os.killpg(child.pid, 0)
            except ProcessLookupError:
                group_absent_proven = True
            except OSError:
                pass
        observed["cleanup_terminal"] = group_absent_proven and child.poll() is not None
        observed["transport_returncode"] = child.returncode
        return tuple(safe_output(value) for value in latest)

    try:
        try:
            birth_timeout = 5 if timeout is None else min(5, timeout - (time.monotonic() - started))
            if birth_timeout <= 0:
                raise subprocess.TimeoutExpired(command, timeout)
            birth = subprocess.run(
                ["ps", "-p", str(child.pid), "-o", "lstart="],
                capture_output=True,
                text=True,
                timeout=birth_timeout,
                check=True,
            )
            observed["os_started"] = birth.stdout.strip()
            if not observed["os_started"]:
                raise RuntimeError("transport birth identity unavailable")
            callback(observed)
        except BaseException as exc:
            stdout, stderr = stop_known_group()
            if observed.get("cleanup_terminal") is True and finished_callback is not None:
                finished_callback(observed)
            if not isinstance(exc, Exception):
                raise
            detail = (
                "original observation timeout exhausted"
                if isinstance(exc, subprocess.TimeoutExpired)
                else str(exc)
            )
            raise ObservedOffloadFailure(
                f"trusted process observation failed after spawn: {type(exc).__name__}: {detail}",
                observed,
                child.poll(),
                stdout,
                stderr,
            ) from exc
        try:
            remaining = None if timeout is None else max(0, timeout - (time.monotonic() - started))
            stdout, stderr = (
                bounded_communicate(remaining)
                if output_limit is not None
                else child.communicate(timeout=remaining)
            )
        except _ObservedOutputLimit as exc:
            stop_known_group()
            if observed.get("cleanup_terminal") is True and finished_callback is not None:
                finished_callback(observed)
            raise ObservedOffloadFailure(
                str(exc),
                observed,
                child.poll(),
                safe_output(bytes(captured["stdout"])),
                safe_output(bytes(captured["stderr"])),
            ) from exc
        except subprocess.TimeoutExpired as exc:
            stdout, stderr = stop_known_group()
            if observed.get("cleanup_terminal") is True and finished_callback is not None:
                finished_callback(observed)
            expired = subprocess.TimeoutExpired(command, timeout, output=stdout, stderr=stderr)
            expired.observation = observed
            expired.raw_exit = child.poll()
            raise expired from exc
        except BaseException:
            stop_known_group()
            raise
        try:
            os.killpg(child.pid, 0)
        except ProcessLookupError:
            observed["cleanup_terminal"] = child.poll() is not None
            if observed["cleanup_terminal"] and finished_callback is not None:
                finished_callback(observed)
        except OSError as exc:
            observed["cleanup_error"] = {
                "type": type(exc).__name__,
                "errno": exc.errno,
                "reason": "owned process-group liveness could not be verified",
                "process_group": child.pid,
            }
            stdout, stderr = stop_known_group()
            if observed.get("cleanup_terminal") is True and finished_callback is not None:
                finished_callback(observed)
            else:
                raise ObservedOffloadFailure(
                    "owned process group termination could not be verified",
                    observed,
                    child.returncode,
                    stdout,
                    stderr,
                )
        else:
            stdout, stderr = stop_known_group()
            if observed.get("cleanup_terminal") is True and finished_callback is not None:
                finished_callback(observed)
            else:
                raise ObservedOffloadFailure(
                    "owned process group remained after transport completion",
                    observed,
                    child.returncode,
                    stdout,
                    stderr,
                )
        result = subprocess.CompletedProcess(
            command, child.returncode, safe_output(stdout), safe_output(stderr)
        )
        if env is not None:
            result.observation = observed
        return result
    finally:
        token = ""
        if env is not None:
            env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
        for buffer in captured.values():
            buffer.clear()
        for pipe in (child.stdout, child.stderr):
            if pipe is not None:
                pipe.close()


@_controlled_data_only_entry
def offload(
    agent: str,
    prompt: str,
    cwd: str = ".",
    mode: str | None = None,
    timeout: float | None = None,
    isolate: bool = False,
    profile_id: str | None = None,
    research_round: str | None = None,
    task_type: str | None = None,
    write_boundary: dict | None = None,
    subject_id: str | None = None,
    attempt_receipt_callback=None,
    max_network_retries: int | None = None,
    attempt_started_callback=None,
    process_started_callback=None,
    process_starting_callback=None,
    request_deadline_mono: float | None = None,
    finite_frontier_id: str | None = None,
    finite_frontier_target: str | None = None,
    process_finished_callback=None,
    data_only: bool = False,
    data_only_limits: dict | None = None,
    trusted_auth_supplier=None,
    data_only_operation_id: str | None = None,
) -> dict:
    """SYNCHRONOUS offload for token conservation.

    By default this runs in `cwd`. With isolate=True, it first copies `cwd` to a persistent local
    offload workspace so multiple code-building offloads can run in parallel without same-dir races.
    The isolated result is NOT auto-merged; the orchestrator reviews and integrates deliberately.
    A `cwd` that is, or holds, `/`, the home dir or the control plane is never granted: the run gets
    a fresh scratch directory instead (`adapters.agent_workspace`), and the result's
    `workspace_relocated` says so.

    Runs a cheaper agent and RETURNS its output to the orchestrating seat — no claim, no PR.
    The seat spends that agent's capacity instead of its own and gets back only the result.
    Records a ledger row (it consumes the agent's budget).

    THE WHOLE RANGE OF LLM WORK GOES THROUGH HERE, not just reading. The agent gets a real
    workspace and a shell: it writes and edits code across files, runs builds, tests and linters
    and reads their exit codes, iterates until something passes, debugs, refactors, reviews,
    researches. Big-context reading (summarize 200 pages -> gemini) is ONE use, and naming only
    that use in this docstring is part of how callers came to believe it was the only one.
    `mode="assess"` narrows to a no-write sandbox when that is what you want; the default does not.

    Exactly two things do not transfer, and neither is about how big or code-shaped the task is:
    FLEET ACTIONS (claim/PR/label — that is `delegate`, below), and FIRST-PERSON OBSERVATION,
    since the caller receives a report rather than having watched the thing happen.

    `research_round` binds this offload to a multi-agent research round (see
    `research_subjects.record_research_round`). An audit or study that fans work out to several
    agents is comparable evidence, but only if the runs carry the round as their experiment_id:
    without it each agent is an unrelated run against an ephemeral temp path, which is why
    thousands of offload runs across six agents produced nothing the learner could compare.

    `write_boundary` = {"protect": [dirs], "allow": [dirs]} bounds where the agent may write: nowhere
    under `protect` except the run's own cwd and the `allow` roots (never a `.git` beneath one).
    codex is held by its own sandbox (`--add-dir` per root); cursor, agy, vibe, aider and claude run
    under a seatbelt profile (`adapters.apply_write_boundary`). A boundary that cannot be enforced
    refuses the run (exit 2) rather than running it unbounded. None changes nothing.

    `task_type` names what the work IS (review, mechanical, implement, ...) so the run the learner
    reads is ranked under that type. None keeps the historical literal "offload", under which every
    offload run before 2026-09-20 was recorded regardless of the work it did.

    `subject_id` names the WORK this run was for (`owner/repo#N` for an issue or PR, the repo for a
    repo audit), recorded as routing metadata `subject_id` so both capability readers count it as a
    subject. A registered `research_round` supplies its own subject and wins. With neither, the run
    stays unkeyed and its subject falls back to `offload:<cwd>`, which pools every run from one
    directory; that is a statement that nothing named the work, never a guess at it."""
    # This opt-in is checked before any workspace, log, telemetry or provider work.
    if type(data_only) is not bool:
        return {
            "agent": agent,
            "exit": 2,
            "output": "",
            "run_id": None,
            "error": "data_only must be boolean",
        }
    controlled = _CONTROLLED_DATA_ONLY.get() if data_only else None
    if data_only and controlled is None:
        return {
            "agent": agent,
            "exit": 2,
            "output": "",
            "run_id": None,
            "error": "controlled data-only context unavailable",
            "process_started": False,
            "outcome_uncertain": False,
        }
    if data_only:
        problem = adapters.data_only_problem(agent, mode)
        if problem is None and max_network_retries not in (None, 0):
            problem = "data-only transport forbids automatic retries"
        if problem:
            return {
                "agent": agent,
                "exit": 2,
                "output": "",
                "run_id": None,
                "error": problem,
                "process_started": False,
                "outcome_uncertain": False,
                "unsupported_assessment_tool_boundary": True,
            }
        max_network_retries = 0
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
    from storage_lifecycle import StorageBlocked, owned_agent_tmpdir

    try:
        owned_agent_tmpdir(agent)
    except StorageBlocked as exc:
        return {
            "agent": agent,
            "exit": 2,
            "output": "",
            "run_id": None,
            "error": str(exc),
            "storage_context_refused": True,
            "process_started": False,
            "outcome_uncertain": False,
            "next_trigger": "reconcile_owned_storage_context",
        }
    if mode is None:
        # Offloads are advisory READS, but this defaulted to 'full' for years — so a codex offload
        # burned Sol (flagship) and a gemini offload burned 3.1 Pro, contradicting the "runs a
        # cheaper agent" contract above. The mid tier is the right home for read/summarize work;
        # ORCH_OFFLOAD_TIER overrides. Cursor stays on Composer: it has no verified tier pins
        # (CLI unauthenticated) and frontier draws the metered mid-tier pool (LOCAL_POLICY.md). (2026-08-08)
        mode = "composer" if agent == "cursor" else adapters.DEFAULT_OFFLOAD_TIER
    automatic_profile = None
    if agent == "gemini" and not profile_id:
        automatic_profile = _select_offload_profile(agent, mode)
        if automatic_profile is None:
            return {
                "agent": agent,
                "exit": 2,
                "output": "",
                "error": f"Gemini {mode} offload profile is unavailable or unresolved",
                "run_id": None,
            }
    # Infrastructure capabilities are never ROUTED to — they run as part of the work — so they
    # record use at their own code path rather than through a matcher. Inert outside an active
    # tick (ORCH_CAPABILITY_HEARTBEATS), so tests and manual runs stay silent. Never allowed to
    # raise: recording that a capability ran must not be able to prevent the work. (2026-08-09)
    _capability_heartbeat("invocation", agent=agent, mode=mode)
    timeout = (
        min(timeout, controlled.limits["timeout_seconds"])
        if controlled is not None and timeout is not None
        else (
            controlled.limits["timeout_seconds"]
            if controlled is not None
            else _default_offload_timeout(agent, timeout)
        )
    )

    def remaining_request_time() -> float | None:
        if request_deadline_mono is None:
            return timeout
        remaining = request_deadline_mono - time.monotonic()
        if timeout is not None:
            remaining = min(remaining, float(timeout))
        return remaining

    if request_deadline_mono is not None:
        timeout = remaining_request_time()
        if timeout is None or timeout <= 0:
            return {
                "agent": agent,
                "exit": 124,
                "output": "",
                "error": "owned request window expired before offload preparation",
                "run_id": None,
                "outcome_uncertain": False,
            }
    # DELIBERATELY no write_heartbeat() here. The heartbeat makes the legacy opener/closer cron lanes
    # YIELD for 15 min (handoff-prerun.sh yield-guard), which is only warranted for fleet-MUTATING
    # dispatch — run()/delegate(), orchestrate.sh --active, orchestrate-seat.sh. An offload is read-only
    # w.r.t. the fleet (no claim, no PR, no label), so there is nothing to double-dispatch and no reason
    # to freeze the autopilot. The self-heartbeat briefly added 2026-06-20 was REVERTED: its premise (a
    # concurrent cron tick starves the offload to a 0-byte timeout) was disproven — the 0-byte hang was
    # an inherited *_PROXY (see _net_hygiene_prelude), and concurrent agent CLI runs do NOT serialize. A
    # driving seat already owns its heartbeat via orchestrate-seat.sh; a standalone/library offload
    # (e.g. repo-audit) must not silently halt opener+closer. Do not re-add without a fleet-mutation reason.
    # A workspace that is, or holds, `/`, the home dir or the control plane is replaced by a fresh
    # per-run scratch directory (adapters.agent_workspace), BEFORE isolation copies it: the tick runs
    # with cwd `/`, so a role's `cwd="."` handed codex, agy and cursor the whole disk 1,521 times,
    # and an isolated copy of `/` would copy the disk. The run still starts, logged loudly.
    try:
        source_cwd, relocated = (
            (controlled.cwd, None)
            if controlled is not None
            else adapters.agent_workspace(cwd, agent=agent)
        )
        # A relocated run's scratch dir is already its own, empty and unshared, so it is not copied:
        # a copy would tell the agent one directory and grant it another.
        copied = isolate and relocated is None and controlled is None
        run_cwd = (
            _isolate_offload_cwd(
                source_cwd,
                timeout=remaining_request_time() if request_deadline_mono is not None else None,
            )
            if copied
            else source_cwd
        )
    except Exception as exc:
        return {"agent": agent, "exit": 2, "output": "", "error": str(exc)}
    if relocated:
        print(
            f"warn: {agent} offload workspace relocated: {relocated['reason']}; "
            f"it runs in its own scratch workspace {relocated['workspace']}",
            file=sys.stderr,
        )
    proc_cwd = source_cwd if agent == "gemini" and copied else run_cwd
    if agent == "gemini" and copied:
        prompt = (
            f"{prompt.rstrip()}\n\n"
            f"GEMINI ISOLATED WORKSPACE: use {run_cwd} as the workspace for all file reads, "
            f"file edits, tests, and shell commands. Do not modify the process cwd {source_cwd}; "
            "it is only used as Antigravity's launch directory; project/app data is redirected "
            "under the Orchestrator runtime."
        )
    boundary: tuple[list[str], list[str]] | None = None
    if write_boundary is not None:
        try:
            boundary = adapters.write_boundary_roots(
                write_boundary.get("protect") or [],
                write_boundary.get("allow") or [],
                run_cwd,
                agent=agent,
            )
            unenforceable = adapters.write_boundary_unenforceable(agent)
            if unenforceable:
                raise adapters.WorkspaceRefused(unenforceable)
        except Exception as exc:
            return {"agent": agent, "exit": 2, "output": "", "error": str(exc), "run_id": None}
        prompt = (
            f"{prompt.rstrip()}\n\nWRITE BOUNDARY: you may create or change files only under "
            + ", ".join(boundary[1])
            + " and the system temp dir (never a .git inside them). Everything under "
            + ", ".join(boundary[0])
            + " is otherwise read-only to you, and a write there fails with 'Operation not permitted'."
        )
    timeout = remaining_request_time()
    if timeout is None or timeout <= 0:
        return {
            "agent": agent,
            "exit": 124,
            "output": "",
            "error": "owned request window expired during offload preparation",
            "cwd": str(run_cwd),
            "process_cwd": str(proc_cwd),
            "isolated_cwd": str(run_cwd) if isolate else None,
            "run_id": None,
            "outcome_uncertain": False,
        }
    prepared_prompt = (
        prompt
        if data_only
        else (
            _offload_prompt(prompt, run_cwd, agent, timeout=timeout)
            if request_deadline_mono is not None
            else _offload_prompt(prompt, run_cwd, agent)
        )
    )
    if relocated and not data_only:
        prepared_prompt = f"{prepared_prompt}\n{_relocated_workspace_rule(relocated)}"
    profile = execution_profiles.get_profile(profile_id) if profile_id else automatic_profile
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
    observed_model_source: str | None = None
    # Gemini has a documented NDJSON prompt transport. Keep a potentially megabyte-scale packet
    # out of bash's ``-lc`` argument and the provider's argv while leaving the established compact
    # print-mode transport untouched. Other adapters retain their qualified transport until their
    # own stdin protocol is covered by an equivalent regression.
    prompt_via_stdin = data_only or (
        agent == "gemini"
        and len(prepared_prompt.encode("utf-8")) > GEMINI_STREAM_PROMPT_THRESHOLD_BYTES
    )
    command_prompt = "" if prompt_via_stdin else prepared_prompt
    # Refuse missing login before allocating a private prompt or recording a started invocation.
    try:
        muse_prelude = (
            _agent_runtime_prelude(agent) if agent == "muse" and controlled is None else None
        )
    except StorageBlocked as exc:
        return {
            "agent": agent,
            "exit": 2,
            "output": "",
            "run_id": None,
            "error": str(exc),
            "storage_context_refused": True,
            "process_started": False,
            "outcome_uncertain": False,
            "next_trigger": "reconcile_owned_storage_context",
        }
    muse_prompt = adapters.muse_prompt_file(command_prompt) if agent == "muse" else None
    data_only_options: _DataOnlyCommandOptions = {"data_only": True} if data_only else {}
    argv = (
        adapters.build_command(
            agent,
            command_prompt,
            mode,
            cwd=run_cwd,
            profile=profile,
            transport="offload",
            prompt_via_stdin=prompt_via_stdin,
            **data_only_options,
            prompt_file=muse_prompt,
            write_roots=boundary[1][1:] if boundary and agent == "codex" else None,
        )
        if profile
        else adapters.build_command(
            agent,
            command_prompt,
            mode,
            cwd=run_cwd,
            transport="offload",
            reasoning_effort=(
                execution_profiles.get_profile(
                    execution_profiles.default_codex_profile("offload", mode)
                )["reasoning_effort"]
                if agent == "codex"
                else None
            ),
            prompt_via_stdin=prompt_via_stdin,
            **data_only_options,
            prompt_file=muse_prompt,
            write_roots=boundary[1][1:] if boundary and agent == "codex" else None,
        )
    )  # raises ValueError on unknown agent
    if controlled is not None:
        import closer_data_only_execution as execution
        import closer_role_qualification as roles

        if profile is None or roles.profile_identity(profile) != controlled.profile:
            raise ValueError("registered execution profile drifted during controlled preparation")
        expected_argv = execution.expected_argv(
            argv[0], controlled.profile, limits=controlled.limits
        )
        if argv not in (expected_argv, expected_argv[:-4]):
            raise ValueError("adapter argv differs from controlled execution contract")
        argv = expected_argv
        sources = {
            "adapters.py": _controlled_module_sha(adapters),
            "dispatcher.py": _controlled_module_sha(sys.modules[__name__]),
            "closer_data_only_execution.py": _controlled_module_sha(execution),
        }
        controlled_timeout = remaining_request_time()
        if controlled_timeout is None or controlled_timeout <= 0:
            raise ValueError("controlled request window expired before log allocation")
        controlled.execution = execution.validate_launch(
            agent=agent,
            profile=controlled.profile,
            binary_path=argv[0],
            binary_sha256=adapters.DATA_ONLY_CLAUDE_SHA256,
            argv=argv,
            env=controlled.env,
            cwd=str(proc_cwd),
            private_root=str(controlled.root),
            response_protocol=execution.NATIVE_JSON,
            source_sha256=sources,
            limits=controlled.limits,
            prompt=prepared_prompt,
            timeout_seconds=controlled_timeout,
        )
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
    # The run_id comes from the same claim, so the log and the run cannot be paired with another
    # offload's.
    run_id, logf = _claim_offload_log(agent)
    agy_log: Path | None = None
    if agent == "gemini" and "--log-file" in argv:
        # `adapters.agy_log_for` owns the name, because `adapters.cli_reported_model` reads it back.
        # Two literals would drift, and the symptom of drift here is a model that never resolves.
        agy_log = adapters.agy_log_for(logf)
        argv[argv.index("--log-file") + 1] = str(agy_log)
    if boundary is not None:
        try:
            argv = adapters.apply_write_boundary(agent, argv, *boundary)
        except Exception as exc:
            return {"agent": agent, "exit": 2, "output": "", "error": str(exc), "run_id": None}
    auth_prelude = "" if controlled is not None else _auth_prelude(agent)
    try:
        agent_prelude = (
            ""
            if controlled is not None
            else muse_prelude if muse_prelude is not None else _agent_runtime_prelude(agent)
        )
    except StorageBlocked as exc:
        if muse_prompt is not None:
            muse_prompt.unlink(missing_ok=True)
        return {
            "agent": agent,
            "exit": 2,
            "output": "",
            "run_id": None,
            "error": str(exc),
            "storage_context_refused": True,
            "process_started": False,
            "outcome_uncertain": False,
            "next_trigger": "reconcile_owned_storage_context",
        }
    wrapped = (
        ""
        if controlled is not None
        else (
            f"{_path_prefix()}; {_net_hygiene_prelude()}{agent_prelude}{auth_prelude}{shlex.join(argv)}"
        )
    )
    stream_prompt = (
        prepared_prompt
        if data_only
        else adapters.gemini_stream_prompt(prepared_prompt) if prompt_via_stdin else None
    )
    if agent == "gemini" or data_only:
        content_bytes = prepared_prompt.encode("utf-8")
        transport_payload = stream_prompt if stream_prompt is not None else prepared_prompt
        transport_bytes = transport_payload.encode("utf-8")
        request_metadata = {
            "transport": (
                "claude-native-json-stdin/v1"
                if data_only
                else "agy-stream-json-stdin" if stream_prompt is not None else "agy-print-argument"
            ),
            "content_utf8_bytes": len(content_bytes),
            "content_sha256": hashlib.sha256(content_bytes).hexdigest(),
            "transport_utf8_bytes": len(transport_bytes),
            "transport_sha256": hashlib.sha256(transport_bytes).hexdigest(),
            **(
                {"data_only": True, "cli_sha256": adapters.DATA_ONLY_CLAUDE_SHA256}
                if data_only
                else {}
            ),
        }
        if controlled is not None:
            request_metadata["execution"] = controlled.execution
    else:
        request_metadata = None
    target = f"offload:{run_cwd}"
    subject, subject_source = _offload_subject(subject_id, research_round)
    task_type = task_type or "offload"
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
        # Present only on a relocated run, so relocations are countable from the ledger alone.
        workspace_relocated_from=relocated["requested"] if relocated else None,
    )

    def _record_offload_run() -> None:
        feedback.record_run(
            run_id,
            target,
            task_type,
            agent,
            mode="offload",
            # THE RAIL TAGS RIDE EVERY RAIL. `_exercised_capability_ids` was wired into
            # plan_dispatch only, while gemini's actual traffic flows through THIS path — measured
            # 2026-08-29: 340 gemini runs in 8 days, 0 via the delegation rail, and ZERO carried
            # the confinement tag anywhere in the store, falsifying the ledger's own
            # "tags every gemini dispatch" claim exactly as its note predicted. The empty
            # assignment is correct here: offload has no assignment dict, and the helper's
            # agent-keyed half is the part that must not be lost.
            capability_ids=_exercised_capability_ids({}, agent) or None,
            experiment_id=research_round or None,
            reasoning_level=(profile.get("reasoning_effort") if profile else mode),
            model=model,
            routing_metadata=(
                {
                    **(
                        {
                            "selected_profile_id": effective_profile_id,
                            "requested_model": profile.get("requested_model"),
                            "transport": "offload",
                            "profile_policy_version": execution_profiles.PROFILE_POLICY_VERSION,
                            "profile_assignment_probability": 1.0,
                        }
                        if profile
                        else {}
                    ),
                    **(
                        {"subject_id": subject, "subject_source": subject_source} if subject else {}
                    ),
                }
                or None
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
        try:
            _record_offload_run()  # fail closed before subprocess execution
        except BaseException:
            if muse_prompt is not None:
                muse_prompt.unlink(missing_ok=True)
            raise
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
        if relocated:
            fh.write(
                f"[orchestrator] WORKSPACE RELOCATED: {relocated['reason']}; this run writes only "
                f"in its own scratch workspace {relocated['workspace']}\n"
            )
    max_network_retries = (
        max(0, _env_int("ORCH_OFFLOAD_NETWORK_RETRIES", 1))
        if max_network_retries is None
        else max(0, max_network_retries)
    )
    retry_backoff_s = max(0.0, _env_float("ORCH_OFFLOAD_RETRY_BACKOFF_S", 3.0))
    complete_written = False
    # One record for every actual `muse exec` attempt, including a refusal/crash that causes a
    # retry.  This is intentionally distinct from the final logical offload result.
    muse_attempts: list[dict[str, float | int]] = []
    # Credentials masked in this run's output, by kind, across attempts (credential_redaction).
    redaction_counts: dict[str, int] = {}

    def _notify_attempt(callback, response: dict) -> None:
        try:
            callback(response)
        finally:
            # agy writes its own log, independently of the scrubbed stdout.
            # Cleanup must survive callback errors, including interruption;
            # completion effects remain unreachable if the callback raises.
            if agy_log is not None:
                scrub = credential_redaction.scrub_file(agy_log)
                for kind, count in (scrub.get("kinds") or {}).items():
                    redaction_counts[kind] = redaction_counts.get(kind, 0) + count

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
            credentials_redacted=sum(redaction_counts.values()) or None,
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
            probe: dict[str, str | None] = (
                {"model": observed_model, "reason": None}
                if observed_model
                else (
                    {"model": None, "reason": "controlled native model identity unavailable"}
                    if controlled is not None
                    else adapters.cli_reported_model(
                        agent, run_cwd, started_ts=started_ts, log_file=str(logf)
                    )
                )
            )
            resolved_model = probe.get("model")
            completed_ts = int(time.time())
            if resolved_model:
                _persist_profile_completion(
                    feedback.complete_profile_attempt,
                    run_id,
                    selected_profile_id=profile["profile_id"],
                    resolved_provider=profile["provider"],
                    resolved_model=resolved_model,
                    completed_ts=completed_ts,
                )
            else:
                _persist_profile_completion(
                    feedback.complete_profile_attempt_unresolved,
                    run_id,
                    selected_profile_id=profile["profile_id"],
                    fallback_reason=(
                        f"resolved_model_not_reported_by_offload:{probe.get('reason') or 'unknown'}"
                    )[:200],
                    completed_ts=completed_ts,
                )
        # Instant meter (2026-07-03 audit F2): don't wait a day for ledger_reconcile to pair the
        # ndjson events — the offload runs synchronously, so latency is known RIGHT NOW. Muse is an
        # exception only in what can be measured: it has a subscription login and no token/dollar
        # receipt, so write its execution facts and clearly-labelled flat subscription imputation.
        try:
            if agent == "muse" and muse_attempts:
                observed_start = int(min(row["started_ts"] for row in muse_attempts))
                observed_end = int(max(row["ended_ts"] for row in muse_attempts))
                window_start, window_end = _muse_observation_window(observed_start)
                feedback.record_muse_subscription_cost(
                    run_id,
                    invocation_count=len(muse_attempts),
                    window_start_ts=window_start,
                    window_end_ts=window_end,
                    observed_start_ts=observed_start,
                    observed_end_ts=observed_end,
                    wall_seconds=sum(float(row["wall_seconds"]) for row in muse_attempts),
                    raw_exit_codes=[int(row["raw_exit"]) for row in muse_attempts],
                )
            else:
                feedback.record_cost(
                    run_id,
                    latency_s=float(max(0, int(time.time()) - started_ts)),
                    source="ledger",
                )
        except Exception as exc:
            # Preserve the deliverable, but make a missing subscription receipt observable.
            if agent == "muse":
                print(
                    f"warning: Muse subscription accounting failed for {run_id}: {exc}",
                    file=sys.stderr,
                )
            # Metering must never fail the offload itself.
        complete_written = True

    attempt_stderr = ""

    def _cleanup_result_artifacts() -> list[str]:
        # New retention barriers still owe the existing prompt removal/log scrub.
        # Cleanup failures must not erase the provider's original uncertain result.
        errors = []
        if muse_prompt is not None:
            try:
                muse_prompt.unlink(missing_ok=True)
            except OSError as exc:
                errors.append("muse_prompt:" + type(exc).__name__)
        if agy_log is not None:
            try:
                scrub = credential_redaction.scrub_file(agy_log)
                if scrub.get("status") in {"error", "too_large"}:
                    errors.append("agy_log:" + str(scrub["status"]))
            except OSError as exc:
                errors.append("agy_log:" + type(exc).__name__)
        return errors

    def _retain_attempt_result(result: dict) -> None:
        if controlled is not None and not result.get("outcome_uncertain"):
            _controlled_cleanup_result(controlled, result)
        try:
            receipt_path = _retain_provider_result(
                logf,
                result,
                profile_id=effective_profile_id,
                attempt=attempt,
                started_at=attempt_started,
                completed_at=time.time(),
                run_cwd=run_cwd,
                process_cwd=proc_cwd,
            )
            if controlled is not None:
                with receipt_path.open("rb") as receipt_stream:
                    receipt_bytes = receipt_stream.read(1_048_577)
                    receipt_stat = os.fstat(receipt_stream.fileno())
                if len(receipt_bytes) > 1_048_576 or receipt_stat.st_size != len(receipt_bytes):
                    raise ValueError("persisted provider receipt exceeds bound or changed")
                result["provider_result_ref"] = {
                    "path": str(receipt_path),
                    "sha256": hashlib.sha256(receipt_bytes).hexdigest(),
                    "bytes": len(receipt_bytes),
                }
        except (OSError, ValueError, TypeError) as exc:
            actual_exit = result.get("raw_exit", result.get("exit"))
            held = {
                **result,
                "exit": 70,
                "raw_exit": actual_exit,
                "provider_completed": actual_exit == 0 and not result.get("outcome_uncertain"),
                "outcome_uncertain": True,
                "outcome_status": "uncertain",
                "retry_safe": False,
                "error": "provider_result_retention_failed:" + type(exc).__name__,
            }
            if cleanup_errors := _cleanup_result_artifacts():
                held["cleanup_errors"] = cleanup_errors
            raise _ProviderResultRetentionPending(held) from exc

    data_only_cleanup_errors = []

    def _run_offload_attempt() -> dict:
        # `nonlocal`, because the model this attempt observes must reach `_record_complete` in the
        # ENCLOSING scope. Without it the assignment below made a local that the completion never
        # saw: the parse found `composer-2.5` and the attempt still closed `unresolved`, which is
        # the most deceptive possible failure -- the reader worked and the row said it had not.
        nonlocal observed_model, observed_model_source
        nonlocal attempt_stderr
        error: str | None
        attempt_stderr = ""
        stream_path: Path | None = None
        stream_identity = None

        def _cleanup_stream() -> None:
            if stream_path is not None:
                try:
                    stream_path.unlink(missing_ok=True)
                except OSError as error:
                    if data_only:
                        # A failed unlink must not silently leave the input behind.
                        # Scrub only our original regular inode; never a replacement.
                        scrubbed = False
                        try:
                            fd = os.open(stream_path, os.O_WRONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                            try:
                                info = os.fstat(fd)
                                if (info.st_dev, info.st_ino) != stream_identity:
                                    raise OSError("private input inode changed")
                                os.ftruncate(fd, 0)
                                os.fsync(fd)
                                scrubbed = True
                            finally:
                                os.close(fd)
                        except OSError:
                            pass
                        data_only_cleanup_errors.append(
                            {
                                "error": type(error).__name__,
                                "input_scrubbed": scrubbed,
                                "path": str(stream_path),
                            }
                        )

        try:
            attempt_wrapped = wrapped
            if stream_prompt is not None:
                # mkstemp creates mode 0600. The shell only receives this controlled path as a
                # redirection target, so prompt bytes never become shell syntax or argv bytes.
                fd, raw_path = tempfile.mkstemp(
                    prefix=f"offload-prompt.{agent}.",
                    suffix=".txt" if data_only else ".ndjson",
                    dir=DISPATCH_LOG_DIR,
                )
                stream_path = Path(raw_path)
                info = os.fstat(fd)
                stream_identity = (info.st_dev, info.st_ino)
                with os.fdopen(fd, "w", encoding="utf-8", newline="") as prompt_file:
                    prompt_file.write(stream_prompt)
            if data_only:
                try:
                    problem = adapters.data_only_problem(agent, mode)
                    if not problem and adapters._data_only_claude_binary() != argv[0]:
                        problem = "data-only CLI identity changed before dispatch"
                except (OSError, ValueError) as exc:
                    problem = f"{type(exc).__name__}: {exc}"
                if problem:
                    # The start ledger already exists, but no process callback
                    # or spawn occurred. Return through normal result retention
                    # and completion rather than leaving a stranded start row.
                    _cleanup_stream()
                    return {
                        "agent": agent,
                        "exit": 2,
                        "output": "",
                        "error": problem,
                        "run_id": run_id,
                        "model": model,
                        "log": str(logf),
                        "cwd": str(run_cwd),
                        "process_cwd": str(proc_cwd),
                        "request_metadata": request_metadata,
                        "process_started": False,
                        "outcome_uncertain": False,
                        "unsupported_assessment_tool_boundary": True,
                    }
            if attempt_started_callback is not None:
                _notify_attempt(attempt_started_callback, {"run_id": run_id, "agent": agent})
        except BaseException:
            # Start notification is not a provider result. Its failure must not
            # enter the subprocess OSError/interrupt completion handlers below.
            _cleanup_stream()
            raise
        try:
            attempt_timeout = remaining_request_time()
            if attempt_timeout is not None and attempt_timeout <= 0:
                raise subprocess.TimeoutExpired("owned offload request", 0)
            if controlled is not None:
                import closer_data_only_execution as execution

                if stream_path is None:
                    raise ValueError("controlled stdin payload is missing")
                if (
                    controlled.execution is None
                    or request_metadata is None
                    or attempt_timeout is None
                ):
                    raise ValueError("controlled execution binding unavailable")
                controlled.execution = execution.validate_launch(
                    agent=agent,
                    profile=controlled.profile,
                    binary_path=argv[0],
                    binary_sha256=adapters.DATA_ONLY_CLAUDE_SHA256,
                    argv=argv,
                    env=controlled.env,
                    cwd=str(proc_cwd),
                    private_root=str(controlled.root),
                    response_protocol=execution.NATIVE_JSON,
                    source_sha256=controlled.execution["binding"]["source_sha256"],
                    limits=controlled.limits,
                    prompt=prepared_prompt,
                    timeout_seconds=attempt_timeout,
                )
                request_metadata["execution"] = controlled.execution
                with stream_path.open("rb") as private_stdin:
                    proc = run_observed_offload(
                        argv,
                        callback=process_started_callback,
                        observation={
                            "run_id": run_id,
                            "agent": agent,
                            "profile_id": profile_id,
                            "provider_argv": list(argv),
                            "operation_id": controlled.operation_id,
                            "task_type": task_type,
                            "finite_frontier_id": finite_frontier_id,
                            "finite_frontier_target": finite_frontier_target,
                            "data_only_execution_binding_sha256": controlled.execution[
                                "binding_sha256"
                            ],
                        },
                        cwd=str(proc_cwd),
                        timeout=attempt_timeout,
                        stdin=private_stdin,
                        env=controlled.env,
                        output_limit=controlled.limits["max_native_result_bytes"],
                        finished_callback=process_finished_callback,
                        starting_callback=process_starting_callback,
                    )
            elif process_started_callback is None:
                proc = subprocess.run(
                    [
                        "bash",
                        "-lc",
                        (
                            f"{attempt_wrapped} < {shlex.quote(str(stream_path))}"
                            if stream_path is not None
                            else attempt_wrapped
                        ),
                    ],
                    cwd=str(proc_cwd),
                    capture_output=True,
                    text=True,
                    timeout=attempt_timeout,
                    # The shell redirection above replaces this with the private NDJSON file for a
                    # large Gemini packet. Every other adapter must still never inherit a caller TTY.
                    stdin=subprocess.DEVNULL,
                )
            else:
                proc = run_observed_offload(
                    [
                        "bash",
                        "-lc",
                        (
                            f"{attempt_wrapped} < {shlex.quote(str(stream_path))}"
                            if stream_path is not None
                            else attempt_wrapped
                        ),
                    ],
                    callback=process_started_callback,
                    observation={
                        "run_id": run_id,
                        "agent": agent,
                        "profile_id": profile_id,
                        "provider_argv": list(argv),
                        **(
                            {"finite_frontier_id": finite_frontier_id} if finite_frontier_id else {}
                        ),
                        **(
                            {"finite_frontier_target": finite_frontier_target}
                            if finite_frontier_target
                            else {}
                        ),
                    },
                    cwd=str(proc_cwd),
                    timeout=attempt_timeout,
                    stdin=subprocess.DEVNULL,
                    finished_callback=process_finished_callback,
                    starting_callback=process_starting_callback,
                )
        except ObservedOffloadFailure as exc:
            return {
                "agent": agent,
                "exit": 70,
                "raw_exit": exc.returncode,
                "output": (
                    "" if controlled is not None else credential_redaction.redact(exc.stdout or "")
                ),
                **(
                    {"partial_native_stdout": credential_redaction.redact(exc.stdout or "")}
                    if controlled is not None
                    else {}
                ),
                "stderr_tail": credential_redaction.redact(exc.stderr or "")[-800:],
                "error": credential_redaction.redact(str(exc)),
                "run_id": run_id,
                "log": str(logf),
                "process_started": True,
                "process_observation": _public_process_observation(exc.observation),
                "outcome_uncertain": True,
                "outcome_status": "uncertain",
                "retry_safe": False,
                **(
                    {"finite_pool_fence": fence}
                    if (fence := _finite_pool_fence(exc.observation))
                    else {}
                ),
            }
        except subprocess.TimeoutExpired as timeout_error:
            # A 0-byte timeout is the env-induced-hang signature; surface the ambient network env so the
            # cause is legible (proxy is scrubbed by default — if one shows here, ORCH_KEEP_PROXY=1 is set).
            suspicious = _suspicious_net_env()
            hint = f" | ambient net env at dispatch: {'; '.join(suspicious)}" if suspicious else ""
            error = f"timed out after {timeout}s{hint}"
            agent_log_tail = _agent_log_tail_from_argv(argv, proc_cwd) if agent == "gemini" else ""
            turn_uncertain = agent == "gemini" and _agy_reports_turn_in_progress(agent_log_tail)
            if agent_log_tail:
                error = f"{error}; agy log tail captured"
            if turn_uncertain:
                error = "agy timed out while its provider turn was still in progress; outcome is uncertain"
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
                "request_metadata": request_metadata,
                "run_id": run_id,
                "log": str(logf),
                **(
                    {"finite_pool_fence": fence}
                    if (fence := _finite_pool_fence(getattr(timeout_error, "observation", None)))
                    else {}
                ),
            }
            out.update(_public_model_metadata(agent, model, observed_model, observed_model_source))
            if turn_uncertain or data_only:
                out.update(
                    {
                        "outcome_status": "uncertain",
                        "outcome_uncertain": True,
                        "retry_safe": False,
                    }
                )
            if process_started_callback is not None:

                def safe_partial(value):
                    if isinstance(value, bytes):
                        value = value.decode(errors="replace")
                    return credential_redaction.redact(value or "")

                out.update(
                    process_started=True,
                    outcome_uncertain=True,
                    outcome_status="uncertain",
                    retry_safe=False,
                    partial_stdout=safe_partial(timeout_error.output),
                    partial_stderr=safe_partial(timeout_error.stderr),
                    raw_exit=getattr(timeout_error, "raw_exit", None),
                    process_observation=_public_process_observation(
                        getattr(timeout_error, "observation", None)
                    ),
                )
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
                **(
                    {
                        "process_started": controlled.observation is not None,
                        "outcome_uncertain": controlled.observation is not None,
                        "retry_safe": False,
                        "process_observation": _public_process_observation(controlled.observation),
                        **(
                            {"finite_pool_fence": fence}
                            if (fence := _finite_pool_fence(controlled.observation))
                            else {}
                        ),
                    }
                    if controlled is not None
                    else {}
                ),
            }
        except KeyboardInterrupt as interruption:
            error = "interrupted by orchestrator"
            interrupted = {
                "agent": agent,
                "exit": 130,
                "output": "",
                "error": error,
                "run_id": run_id,
                "model": model,
                "log": str(logf),
                "cwd": str(run_cwd),
                "process_cwd": str(proc_cwd),
            }
            if data_only:
                interrupted.update(
                    outcome_uncertain=True,
                    outcome_status="uncertain",
                    retry_safe=False,
                    request_metadata=request_metadata,
                )
            try:
                _retain_attempt_result(interrupted)
                if cleanup_errors := _cleanup_result_artifacts():
                    interrupted["cleanup_errors"] = cleanup_errors
                if attempt_receipt_callback is not None:
                    _notify_attempt(attempt_receipt_callback, interrupted)
                _record_complete(exit_code=130, error=error)
                with logf.open("a") as fh:
                    fh.write(f"[orchestrator] offload marked failed: {error}\n")
            except _ProviderResultRetentionPending as pending:
                # Cancellation remains cancellation even when result storage failed.
                # Preserve the exact held receipt for caller reconciliation, never retry.
                setattr(interruption, "held_result", pending.result)
                raise interruption from pending
            except Exception as completion_error:
                # A callback/accounting/log error cannot consume operator cancellation.
                setattr(interruption, "completion_error", type(completion_error).__name__)
                raise interruption from completion_error
            raise
        finally:
            _cleanup_stream()
        # MASKED BEFORE ANYTHING READS IT. Everything below -- the stream parse, the log, the
        # incident classifier and the dict handed back to the caller -- sees only this text. A vibe
        # offload of 2026-09-06 printed the live GitHub token, and this function wrote it to a
        # world-readable log and returned it to the calling session, which kept a copy of its own.
        stdout, out_counts = credential_redaction.redact_with_counts(proc.stdout or "")
        stderr, err_counts = credential_redaction.redact_with_counts(proc.stderr or "")
        for kind, n in (*out_counts.items(), *err_counts.items()):
            redaction_counts[kind] = redaction_counts.get(kind, 0) + n
        if out_counts or err_counts:
            proc = subprocess.CompletedProcess(
                getattr(proc, "args", argv), proc.returncode, stdout, stderr
            )
        attempt_stderr = proc.stderr or ""
        if controlled is not None:
            import closer_data_only_execution as execution
            import transport_qualification
            import transport_saved_result

            native_text = proc.stdout or ""
            observation = controlled.observation or {}
            origin = {
                "schema": "closer-data-only-runtime-origin/v1",
                "source": "existing-dispatcher-observed-supervisor",
                "operation_id": controlled.operation_id,
                "operation_binding_source": "explicit_trusted_caller_must_match_expected_operation",
                "run_id": run_id,
                "pool_id": "claude-subscription",
                "pool_reservation": controlled.pool_reservation,
                "profile_id": profile_id,
                "task_type": task_type,
                "finite_frontier_id": finite_frontier_id,
                "finite_frontier_target": finite_frontier_target,
                "process_observation": _public_process_observation(observation),
                "execution": controlled.execution,
                "collector_sources": dict(controlled.native_sources),
                "native_stdout_sha256": hashlib.sha256(native_text.encode()).hexdigest(),
                "native_stdout_bytes": len(native_text.encode()),
                "stdout_scope": "captured_UTF8_text_redacted_before_parse_and_retention",
                "native_json": native_text,
                "authority": False,
                "classification_validated": False,
            }
            native_error = None
            normalized = None
            final_text = ""
            try:
                if proc.returncode != 0 or observation.get("cleanup_terminal") is not True:
                    raise ValueError("native transport did not complete with positive cleanup")
                if observation.get("borrowed_credential_output") or observation.get(
                    "invalid_utf8_output"
                ):
                    raise ValueError("native output required credential or encoding redaction")
                current_sources = {
                    name: _controlled_module_sha(module)
                    for name, module in (
                        ("transport_saved_result.py", transport_saved_result),
                        ("transport_qualification.py", transport_qualification),
                    )
                }
                if current_sources != controlled.native_sources:
                    raise ValueError("collector source drifted during observed invocation")
                launch_sources = {
                    "adapters.py": _controlled_module_sha(adapters),
                    "dispatcher.py": _controlled_module_sha(sys.modules[__name__]),
                    "closer_data_only_execution.py": _controlled_module_sha(execution),
                }
                if (
                    controlled.execution is None
                    or launch_sources != controlled.execution["binding"]["source_sha256"]
                ):
                    raise ValueError("controlled launch source drifted during invocation")
                execution.validate_payload(
                    native_text, limits=controlled.limits, kind="native_result"
                )
                normalized = transport_saved_result.normalize_claude_result(
                    native_text,
                    run_id=run_id,
                    pool_id="claude-subscription",
                    requested_model=controlled.profile["requested_model"],
                    requested_effort=controlled.profile["reasoning_effort"],
                )
                native = transport_qualification.strict_final(native_text)
                if (
                    normalized["response"]["num_turns"] != controlled.limits["max_turns"]
                    or native.get("permission_denials", []) != []
                    or native["modelUsage"][controlled.profile["requested_model"]].get(
                        "webSearchRequests", 0
                    )
                    != 0
                    or any(k in native for k in ("resume", "resumed", "resumed_session_id"))
                ):
                    raise ValueError("native turn/tool/resume evidence differs from contract")
                final_text = normalized["response"]["final"]
                origin["final"] = execution.validate_payload(
                    final_text, limits=controlled.limits, kind="final"
                )
                origin["normalized_native_result"] = normalized
                observed_model = normalized["observed"]["model"]
                observed_model_source = normalized["observed"]["identity_source"]
            except (OSError, ValueError, TypeError, KeyError) as exc:
                native_error = "controlled native result rejected:" + type(exc).__name__
            with logf.open("a") as fh:
                fh.write("[controlled native JSON; redacted]\n" + native_text + "\n")
            return {
                "agent": agent,
                "exit": 70 if native_error else 0,
                "raw_exit": proc.returncode,
                "output": "" if native_error else final_text,
                "error": native_error,
                "model": model,
                "usage": {},
                "request_metadata": request_metadata,
                "data_only_origin": origin,
                "stderr_tail": (proc.stderr or "")[-800:],
                "cwd": str(run_cwd),
                "process_cwd": str(proc_cwd),
                "run_id": run_id,
                "log": str(logf),
                "process_started": True,
                "process_observation": _public_process_observation(observation),
                **_public_model_metadata(agent, model, observed_model, observed_model_source),
                **(
                    {"outcome_uncertain": True, "outcome_status": "uncertain", "retry_safe": False}
                    if native_error
                    else {}
                ),
            }
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
                if observed_model:
                    observed_model_source = "agy_log_selected_override"
        # Capture native terminal-event usage BEFORE stream reduction discards it.
        # Only CLI completion events count; JSON in an assistant deliverable does not.
        import ledger_reconcile

        reported_in = reported_out = 0
        has_reported_usage = False
        # The transport request, never the shape of assistant text, identifies
        # the CLI event protocol. Plain-text adapters cannot mint receipts.
        stream_json = any(
            argv[i : i + 2] == ["--output-format", "stream-json"] for i in range(len(argv) - 1)
        )
        terminal_types = (
            {"turn.completed"}
            if agent == "codex" and "--json" in argv
            else {"result"} if agent in {"cursor", "gemini"} and stream_json else set()
        )
        for line in (proc.stdout or "").splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if (
                isinstance(event, dict)
                and event.get("type") in terminal_types
                and isinstance(event.get("usage"), dict)
            ):
                tin, tout = ledger_reconcile._usage_from_obj(event)
                reported_in += tin
                reported_out += tout
                # An explicit zero count is still a native receipt. Empty or
                # null usage must retain its missing-telemetry meaning.
                has_reported_usage |= bool(tin or tout) or any(
                    type(event["usage"].get(key)) is int and event["usage"][key] == 0
                    for key in ("input_tokens", "output_tokens")
                )
        reported_usage = (
            {"input_tokens": reported_in, "output_tokens": reported_out}
            if has_reported_usage
            else {}
        )
        if agent == "muse":
            # Retain the redacted native protocol before reducing it to the deliverable. This
            # distinguishes an unsupported event shape from an empty model response and keeps
            # each actual retry inspectable in the existing per-run log.
            with logf.open("a") as fh:
                fh.write("[muse native JSONL]\n")
                fh.write(proc.stdout or "")
                fh.write("\n[end muse native JSONL]\n")
            # JSONL lifecycle records never satisfy an offload deliverable.  Preserve only an
            # explicit terminal response; an unfamiliar event schema fails the existing nonempty
            # completion gate rather than being mistaken for agent work.
            proc = subprocess.CompletedProcess(
                getattr(proc, "args", argv),
                proc.returncode,
                adapters.muse_terminal_text(proc.stdout or ""),
                proc.stderr,
            )
        # The large-prompt transport uses agy's matching NDJSON output. Preserve the existing
        # offload return contract by handing callers its final text response, as print mode does.
        if prompt_via_stdin and agent == "gemini":
            response = adapters.gemini_stream_response(proc.stdout or "")
            proc = subprocess.CompletedProcess(
                getattr(proc, "args", argv),
                proc.returncode if response is not None or proc.returncode else 70,
                response if response is not None else "",
                (
                    proc.stderr
                    if response is not None
                    else ((proc.stderr or "") + "\nGemini stream has no final string response")
                ),
            )
        if proc.stdout and '"subtype":"init"' in proc.stdout:
            stream = adapters.observed_model_from_stream(proc.stdout)
            label = stream.get("model_label")
            if label:
                observed_model = adapters.model_id_for_label(agent, label)
                if observed_model:
                    observed_model_source = "agy_stream_init_model_label"
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
            error = (
                "Gemini stream has no final string response"
                if prompt_via_stdin and agent == "gemini" and response is None
                else f"agent exited {raw_exit}"
            )
        else:
            incomplete = _offload_incomplete_reason(
                proc.stdout or "",
                progress_only=(agent == "gemini"),
                # From the argv, never the text: codex prints its `exec --json` stream whenever the
                # run asked for one (every mode but `assess`, `adapters.build_command`), while a
                # text deliverable may itself hold JSON lines (91 of 321 codex text stdouts did).
                codex_stream=(agent == "codex" and "--json" in argv),
            )
            if incomplete:
                error = incomplete
                exit_code = 70
        # Always inspect this invocation's own agy log before accepting a successful
        # stdout. The CLI can return 0 and nonempty partial prose while its provider
        # turn is still running; checking only the error path treated that as success.
        agent_log_tail = ""
        if agent == "gemini":
            agent_log_tail = _agent_log_tail_from_argv(argv, proc_cwd)
            if agent_log_tail and error == "agent returned no stdout":
                error = "agent returned no stdout; agy log tail captured"
        turn_uncertain = agent == "gemini" and _agy_reports_turn_in_progress(agent_log_tail)
        if turn_uncertain:
            error = "agy stopped waiting while its provider turn was still in progress; outcome is uncertain"
            exit_code = 70
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
            "request_metadata": request_metadata,
            "usage": reported_usage,
            "stderr_tail": (proc.stderr or "")[-800:],
            "cwd": str(run_cwd),
            "process_cwd": str(proc_cwd),
            "isolated_cwd": str(run_cwd) if isolate else None,
            "run_id": run_id,
            "log": str(logf),
        }
        out.update(_public_model_metadata(agent, model, observed_model, observed_model_source))
        if agent_log_tail:
            out["agent_log_tail"] = agent_log_tail
        if raw_exit != exit_code:
            out["raw_exit"] = raw_exit
        if error:
            out["error"] = error
        if turn_uncertain:
            out.update(
                {
                    "outcome_status": "uncertain",
                    "outcome_uncertain": True,
                    "retry_safe": False,
                }
            )
        return out

    out = {}
    attempts = 0
    total_in = total_out = 0
    has_reported_usage = False
    for attempt in range(1, max_network_retries + 2):
        attempts = attempt
        attempt_started = time.time()
        try:
            out = _run_offload_attempt()
            if data_only:
                out["request_metadata"] = request_metadata
                if data_only_cleanup_errors:
                    out.update(
                        raw_exit=out.get("raw_exit", out.get("exit")),
                        exit=70,
                        error="data-only private input cleanup failed",
                        cleanup_errors=data_only_cleanup_errors,
                    )
                if (
                    out.get("exit") != 0
                    and not (
                        controlled is not None
                        and out.get("process_started") is False
                        and out.get("outcome_uncertain") is False
                    )
                    and not (
                        out.get("exit") == 2
                        and out.get("process_started") is False
                        and out.get("unsupported_assessment_tool_boundary") is True
                    )
                ):
                    out.update(outcome_uncertain=True, outcome_status="uncertain", retry_safe=False)
            _retain_attempt_result(out)
        except _ProviderResultRetentionPending as exc:
            # Provider effects already happened: no callback, enrichment or retry.
            return exc.result
        attempt_ended = time.time()
        # Receipt consumers that reserve an external spend boundary must see
        # every actual invocation before retry, ledger completion, Brain or
        # cost effects.  Callback failure intentionally aborts this run.
        if attempt_receipt_callback is not None:
            _notify_attempt(attempt_receipt_callback, out)
            out["attempt_receipt_callback_called"] = True
        if agent == "muse":
            raw_exit = out.get("raw_exit", out.get("exit", 126))
            try:
                raw_exit = int(raw_exit)
            except (TypeError, ValueError):
                raw_exit = 126
            muse_attempts.append(
                {
                    "started_ts": int(attempt_started),
                    "ended_ts": int(attempt_ended),
                    "wall_seconds": max(0.0, attempt_ended - attempt_started),
                    "raw_exit": raw_exit,
                }
            )
            _record_muse_usage(
                attempt_started, "offload", raw_exit, attempt_ended - attempt_started
            )
        usage = out.get("usage") or {}
        has_reported_usage |= bool(usage)
        total_in += usage.get("input_tokens", 0)
        total_out += usage.get("output_tokens", 0)
        out["usage"] = (
            {"input_tokens": total_in, "output_tokens": total_out} if has_reported_usage else {}
        )
        out["attempts"] = attempts
        if out.get("exit") == 0:
            break
        if out.get("outcome_uncertain") is True:
            # The provider turn may still be running. A retry would overlap it and can
            # spend twice; leave reconciliation to the caller instead.
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
    if relocated:
        out["workspace_relocated"] = relocated
    # agy writes its per-run log itself, at the path this function chose, so it is scrubbed whole
    # once the run is over. Until this point that file held whatever agy printed.
    if agy_log is not None:
        scrub = credential_redaction.scrub_file(agy_log)
        for kind, n in (scrub.get("kinds") or {}).items():
            redaction_counts[kind] = redaction_counts.get(kind, 0) + n
    redaction_note = None
    if credential_redaction.disabled():
        out["credential_redaction"] = "disabled"
        redaction_note = (
            f"credential redaction DISABLED by {credential_redaction.REDACTION_DISABLED_ENV}=1: "
            "this output was logged and returned unmasked"
        )
    elif redaction_counts:
        out["credentials_redacted"] = sum(redaction_counts.values())
        redaction_note = (
            "credentials masked before this output was logged or returned: "
            + credential_redaction.describe(redaction_counts)
        )
    if redaction_note:
        with logf.open("a") as fh:
            fh.write(f"[orchestrator] {redaction_note}\n")
    if muse_prompt is not None:
        muse_prompt.unlink(missing_ok=True)
    provider_result = dict(out)
    try:
        _record_complete(exit_code=out.get("exit"), error=out.get("error"))
    except _CompletionPersistencePending as exc:
        # Preserve the already-redacted actual result before handing a conservative
        # accounting barrier to callers. This branch is outside the provider loop.
        actual_exit = out.get("raw_exit", out.get("exit"))
        provider_completed = (
            type(actual_exit) is int
            and actual_exit == 0
            and out.get("outcome_uncertain") is not True
        )
        pending = {
            "schema": "offload-completion-persistence/v1",
            "status": "pending",
            "provider_completed": provider_completed,
            **exc.completion,
        }
        raw = json.dumps({"result": out, "completion": pending}, sort_keys=True).encode()
        receipt_path = logf.with_suffix(".completion-pending.json")
        temporary = None
        try:
            fd, temporary = tempfile.mkstemp(prefix=".completion-", dir=logf.parent)
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, receipt_path)
            pending["receipt"] = {
                "path": str(receipt_path),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "bytes": len(raw),
            }
        except OSError as retention_error:
            # The native redacted log already exists. Accounting/retention failure
            # must still return a barrier rather than lose the run identity.
            pending["receipt_error"] = type(retention_error).__name__
        finally:
            if temporary is not None:
                try:
                    Path(temporary).unlink(missing_ok=True)
                except OSError:
                    pass
        out.update(
            raw_exit=out.get("raw_exit", out.get("exit")),
            exit=70,
            error="offload profile completion persistence is pending",
            completion_persistence=pending,
            provider_completed=provider_completed,
            outcome_status="uncertain",
            outcome_uncertain=True,
            retry_safe=False,
        )
    # Telemetry is fail-open. Classify the actual result, stderr, and per-run agent log.
    evidence_result = None
    reset_at = None
    try:
        import rate_incidents

        failure_parts = [
            provider_result.get("error"),
            provider_result.get("stderr_tail"),
            provider_result.get("agent_log_tail"),
        ]
        stdout = str(provider_result.get("output") or "")
        # A FAILED run's stdout is error evidence less codex's own record of its work: two codex
        # offloads that printed a doc about quotas became quota incidents and shed the seat
        # (2026-09-19, 09-20). A successful run's stdout counts only through the strict envelope.
        if provider_result.get("exit") != 0:
            failure_parts.insert(0, rate_incidents.failed_stdout_evidence(agent, stdout))
        elif rate_incidents.stdout_carries_capacity_evidence(stdout):
            failure_parts.insert(0, stdout)
        combined_output = "\n".join(str(part or "") for part in failure_parts)
        evidence_result = rate_incidents.get_structured_evidence(
            error_text=combined_output,
            agent=agent,
            surface="dispatcher.offload",
            run_id=run_id,
            target=target,
        )
        out["rate_incident_evidence"] = evidence_result
        # The provider's stated reset, from codex's harness events alone. Without it the two
        # refusals of 2026-09-25 that named Sep 28 shed for the 6 h cooldown, and this record, the
        # first of a run's two observers, is the one whose shed stands.
        reset_lines = combined_output.splitlines() if agent == "gemini" else stdout.splitlines()
        reset_at = rate_incidents.provider_reset_at(agent, reset_lines)
    except Exception as exc:
        print(f"warn: rate-incident classification failed for {agent}: {exc}", file=sys.stderr)
    if evidence_result and evidence_result.get("is_authoritative"):
        try:
            rate_incidents.record_incident(
                agent=agent,
                surface="dispatcher.offload",
                category=evidence_result["category"],
                status="recorded",
                target=target,
                run_id=run_id,
                evidence=combined_output,
                reset_at=reset_at,
                extra={
                    "subcategory": evidence_result["subcategory"],
                    "exit_code": provider_result.get("exit"),
                    "attempts": attempts,
                },
            )
        except Exception as exc:
            print(f"warn: rate-incident recording failed for {agent}: {exc}", file=sys.stderr)
    return out


# Agents the GitHub keepalive can run REMOTELY via an `agent:<name>` label (label-driven; runs
# reusable-<name>-run.yml on a GitHub runner — REMOTE capacity, not this machine). This is how the cron
# orchestrator drives opener/closer work cheaply: it CHOOSES the agent (its value-add) + labels; the
# keepalive executes. The choice is informed by the route-table + learned weights. See PLANNING.md.
REMOTE_AGENTS = {"cursor", "codex", "claude", "gemini"}
# How long the remote rail waits for GitHub to answer a target's label read. A read that does not
# answer in time is unknown like any other failed read, so the target is refused and the next tick
# asks again; without a bound, a hung `gh` hung the tick, and launchd starts no tick while one runs.
LABEL_READ_TIMEOUT_S = 30


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


def _target_labels(target: str) -> tuple[set[str] | None, str]:
    """Live: label names on the target ISSUE or PR via the issues API (works for both).

    Three answers, never two. GitHub answered with the issue's label list: `(names, "")`, where an
    EMPTY set is its answer "no labels". It did not answer (gh could not run, exited non-zero, timed
    out, or printed something that is not an issue with a label list): `(None, why)`. Until
    2026-10-04 that third answer was an empty set, so the skip rail read a failed read as
    "unassigned" and labelled two PRs that carried agent:codex (Trend_Model_Project#5913, #5944)."""
    repo, num = provision.parse_target(target)
    try:
        r = subprocess.run(
            ["gh", "api", f"repos/{repo}/issues/{num}"],
            capture_output=True,
            text=True,
            timeout=LABEL_READ_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return None, f"gh did not answer within {LABEL_READ_TIMEOUT_S}s"
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"gh could not run: {type(exc).__name__}: {exc}"[:200]
    if r.returncode != 0:
        err = " ".join((r.stderr or r.stdout or "").split())
        return None, f"gh exit {r.returncode}: {err}"[:200]
    try:
        issue = json.loads(r.stdout)
    except ValueError:
        return None, "gh printed output that is not JSON"
    labels = issue.get("labels") if isinstance(issue, dict) else None
    # Every element must be a label with a name. Skipping a malformed one would drop the very
    # agent:* label this read exists to find, which is the same unknown-as-no one level down.
    if not isinstance(labels, list) or not all(
        isinstance(lab, dict) and isinstance(lab.get("name"), str) for lab in labels
    ):
        return None, "gh printed no label list"
    return {lab["name"] for lab in labels}, ""


def _remote_skip_reason(labels: set | None, agent: str, unread: str = "") -> str | None:
    """Gate #4 rails (pure): don't fight the keepalive/delegation policy, and DON'T re-assign work already
    in the agent pipeline. Skip if the PR is paused, OR already carries ANY `agent:*` label (it's already
    assigned/in-flight — the orchestrator only remote-delegates FRESH, unassigned items; in-flight PRs are
    the delegation policy's / closer's job). Lesson from the first supervised tick: it added agent:codex on
    top of an existing agent:claude — that double-assignment is what this guards against.

    `labels` of None means the read did not answer (`unread` says why), so ownership is UNKNOWN, and
    that is refused too: "no agent:* label" is a fact only an answered read can establish. The next
    tick reads again, so a refusal lasts exactly as long as GitHub cannot answer for the target."""
    if labels is None:
        return f"labels unread ({unread or 'no answer'}) — ownership unknown, not delegating"
    if "agents:paused" in labels:
        return "agents:paused (lane paused — respect it)"
    assigned = sorted(lab for lab in labels if lab.startswith("agent:"))
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
    a paused/already-owned PR, and one whose labels could not be read, in dry-run exactly as in an
    active tick. Does NOT spawn or claim locally. `labels` overrides the live lookup (tests).
    `labels_read` in the result says whether ownership was known; the tick counts the False ones.
    """
    if agent not in REMOTE_AGENTS:
        return {
            "error": f"'{agent}' is not a keepalive-runnable remote agent {sorted(REMOTE_AGENTS)}"
        }
    repo, num = provision.parse_target(target)
    if num is None:
        return {"error": f"remote delegation needs a PR number: {target!r}"}
    cmd = _remote_label_cmd(target, agent)
    lbls: set | None  # None: GitHub did not answer, so ownership is unknown
    if labels is not None:
        lbls, unread = labels, ""
    else:  # fetch (read-only) so dry-run shadows the rails too
        lbls, unread = _target_labels(target)
    skip = _remote_skip_reason(lbls, agent, unread)
    labels_read = lbls is not None
    if dry_run:
        return {
            "target": target,
            "agent": agent,
            "label": f"agent:{agent}",
            "cmd": cmd,
            "dry_run": True,
            "skip": skip,
            "labels_read": labels_read,
        }
    if skip:  # rails: respect pause / existing or unknown ownership
        return {
            "target": target,
            "agent": agent,
            "applied": False,
            "skip": skip,
            "labels_read": labels_read,
        }
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
        "labels_read": labels_read,
    }


def load_decision() -> dict:
    try:
        return json.loads(DECISION_JSON.read_text())
    except Exception:
        return {"assignments": []}


# ---------------------------------------------------------------------------
def _selftest_spawn_reads_the_agents_status(plan: dict, root: Path) -> None:
    """Run `_spawn`'s own wrapper with a stub agent (exit 7), claim release (exit 3) and completion
    step, and read what it recorded. The marker's rc, `complete --exit-code` and the wrapper's exit
    are the AGENT's; the release still runs after a failed agent, and its status is `release_rc`."""
    global CLAIMS_PY, ORCH_DIR
    stubs = root / "spawn-stubs"
    stubs.mkdir()
    calls = stubs / "calls.log"
    for name, step, rc in (("claims.py", "release", 3), ("ledger_reconcile.py", "complete", 0)):
        (stubs / name).write_text(
            f"import sys\nopen({str(calls)!r}, 'a').write({step!r} + ' ' + ' '.join(sys.argv[2:])"
            f" + '\\n')\nsys.exit({rc})\n"
        )
    seen: dict = {}

    class _Proc:
        pid = 4242

    def _capture(argv, **_kw):
        seen["wrapped"] = argv[-1]
        return _Proc()

    real_popen, real_claims, real_orch = subprocess.Popen, CLAIMS_PY, ORCH_DIR
    setattr(subprocess, "Popen", _capture)  # scoped to the one `_spawn` call below
    CLAIMS_PY, ORCH_DIR = stubs / "claims.py", stubs
    try:
        _spawn({**plan, "run_id": "selftest-agent-rc", "cwd": str(stubs), "wrapped": "(exit 7)"})
    finally:
        setattr(subprocess, "Popen", real_popen)
        CLAIMS_PY, ORCH_DIR = real_claims, real_orch
    ran = subprocess.run(["bash", "-c", seen["wrapped"]], cwd=stubs, capture_output=True, text=True)
    marker = json.loads((DISPATCH_LOG_DIR / "done" / "selftest-agent-rc.json").read_text())
    steps = calls.read_text().splitlines()
    assert ran.returncode == 7, ("the wrapper exits with the agent's status", ran)
    assert marker["rc"] == 7 and marker["rc_of"] == adapters.MARKER_RC_OF_AGENT, marker
    assert marker["release_rc"] == 3, ("the release keeps its own status", marker)
    assert [s.split()[0] for s in steps] == ["release", "complete"], steps
    assert steps[0].endswith(f"{plan['target']} {plan['agent']}"), steps
    assert "--exit-code 7" in steps[1], ("complete is told the agent's status", steps)


def _selftest_gemini_refusal() -> None:
    from unittest.mock import patch

    def forbidden(*_args, **_kwargs):
        raise AssertionError("unavailable automatic Gemini profile reached execution")

    with (
        patch.dict(os.environ, {"ORCH_OFFLOAD_DISABLED": "0"}),
        patch.object(execution_profiles, "profiles_for_agent", return_value=[]),
        patch.object(adapters, "agent_workspace", side_effect=forbidden),
        patch.object(sys.modules[__name__], "_capability_heartbeat", side_effect=forbidden),
        patch.object(subprocess, "run", side_effect=forbidden),
    ):
        result = offload("gemini", "selftest refusal", mode="mid")
        assert result["exit"] == 2 and result["run_id"] is None, result


def _selftest_capability_dispatch(root: Path) -> None:
    """Exercise real spawn and writer seams with offline producer/process doubles."""
    from types import SimpleNamespace
    from unittest.mock import patch

    import capabilities
    import capability_lifecycle
    import capability_targets

    cap_id, version = "selftest-compiled", "cv:selftest-compiled"
    ledger = {
        cap_id: {"capability_version_id": version},
        "thompson-hybrid-routing": {"capability_version_id": "cv:infra"},
    }
    receipt = {
        "invoked": True,
        "accepted": True,
        "consumer_ref": "receipt:selftest",
        "invocation_id": "invoke:selftest",
        "causal_edge": {"source_event_id": "event:selftest"},
    }
    plan: dict = {
        "run_id": "selftest-capability",
        "target": "owner/repo#639",
        "task_type": "implement",
        "agent": "codex",
        "mode": "full",
        "cwd": str(root),
        "wrapped": "true",
        "capability_ids": ["thompson-hybrid-routing"],
        "capability_version_ids": ["cv:infra"],
        "routing_metadata": {
            "selected_capability_id": cap_id,
            "selected_capability_version_id": version,
        },
    }
    with (
        patch.object(capabilities, "load", return_value=ledger),
        patch.object(capability_targets, "get_binding", return_value={"kind": "workflow"}),
        patch.object(
            capability_lifecycle, "invoke_compiled_target", return_value=receipt
        ) as invoke,
        patch.object(subprocess, "Popen", return_value=SimpleNamespace(pid=639)),
        patch.object(claims, "update_metadata", return_value=True),
        patch.object(sys.modules[__name__], "_agent_runtime_prelude", return_value=""),
    ):
        assert _spawn(plan) == 639
        invoke.assert_called_once()
        assert plan["capability_ids"] == ["thompson-hybrid-routing", cap_id], plan
        assert plan["capability_version_ids"] == ["cv:infra", version], plan
        with feedback._conn() as conn:
            payloads = conn.execute(
                "SELECT phase,payload_json FROM completion_events WHERE run_id=? AND event_type='decision'",
                (plan["run_id"],),
            ).fetchall()
        # The pre-invocation envelopes remain immutable history. Both subsequent
        # decision phases must carry the exercised pair.
        assert {row[0] for row in payloads if cap_id in json.loads(row[1])["capability_ids"]} == {
            "trigger",
            "decision",
        }, payloads
        fallback_plan = {
            **plan,
            "run_id": "selftest-capability-fallback",
            "routing_metadata": dict(plan["routing_metadata"]),
        }
        with patch.object(capability_targets, "get_binding", return_value=None):
            assert _spawn(fallback_plan) == 639
        assert cap_id not in fallback_plan["capability_ids"], fallback_plan
        assert (
            fallback_plan["routing_metadata"]["capability_dispatch_result"]["reason"]
            == "target_binding_missing"
        )
        invoke.assert_called_once()
        invoke.reset_mock()
        bad = {**plan, "capability_ids": ["missing-infra"], "capability_version_ids": []}
        try:
            _invoke_selected_compiled_capability(bad)
        except ValueError as exc:
            assert "missing capability versions" in str(exc), exc
        else:
            raise AssertionError("missing infrastructure version was silently dropped")
        invoke.assert_not_called()


def _selftest_delegate_linked_pr() -> None:
    from unittest.mock import patch

    # Exercise the pre-claim edge without GitHub, state cleanup or a worker.
    with (
        patch.object(sys.modules[__name__], "linked_open_prs", return_value=[734, 736]),
        patch.object(claims, "reap_stale") as reap,
        patch.object(claims, "claim", return_value=False) as claim,
        patch.object(claims, "holder", return_value={"agent": "incumbent"}),
    ):
        refused = delegate("cursor", "o/r#731", "opener", "bounded")
        assert "open PR #734, #736" in refused["error"], refused
        claim.assert_not_called()
        reap.assert_not_called()
        continued = delegate("cursor", "o/r#731", "opener", "fix", continue_pr=736)
        assert "already claimed" in continued["error"], continued
        claim.assert_called_once_with("o/r#731", "cursor")


def _selftest() -> None:
    import inspect

    # Wiring pin; behavioral stderr/log reset coverage also exercises offload in pytest.
    assert "combined_output.splitlines()" in inspect.getsource(offload)
    import tempfile

    for repo in INFRA_REPOS:
        assert _dispatch_branch(f"{repo}#7", "opener", "codex", None) == "orch/issue-7"
    assert _dispatch_branch("o/product#7", "opener", "gemini", None) == "gemini/issue-7"
    assert (
        _dispatch_branch("stranske/Workflows#7", "opener", "codex", "custom/") == "custom/issue-7"
    )
    _selftest_gemini_refusal()
    _selftest_delegate_linked_pr()

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
    _saved_muse_link = adapters.ensure_muse_auth_link
    _selftest_muse_login = HANDOFF / "muse-auth-fixture.json"
    _selftest_muse_login.write_text("{}")
    # All selftest launches are offline doubles; never depend on a personal login record.
    adapters.ensure_muse_auth_link = lambda auth_src, auth_dst: _saved_muse_link(
        _selftest_muse_login, auth_dst
    )
    try:
        _selftest_capability_dispatch(HANDOFF)
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
        # seat is capped at `mid` (scarce weekly), so a 'full' assignment records Sonnet 5.5.
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
        assert testgen is not None, "known testgen agent is dispatchable"
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
        assert epic is not None, "known epic agent is dispatchable"
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
        assert codemod is not None, "known codemod agent is dispatchable"
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
        assert cross_repo is not None, "known cross_repo agent is dispatchable"
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
        assert runtime_ac is not None, "known runtime_ac agent is dispatchable"
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
        assert trend is not None, "known trend dispatch agent is dispatchable"
        trend_prompt = " ".join(trend["argv"])
        assert "REPO PLAYBOOK (stranske/Trend_Model_Project)" in trend_prompt, trend["argv"]
        assert "Base branch: main" in trend_prompt and "ruff check" in trend_prompt, trend["argv"]
        normalized_trend_prompt = trend_prompt.lower().replace("`", "")
        assert "phase-3 is the default branch" not in normalized_trend_prompt, trend["argv"]
        assert "there is no main" not in normalized_trend_prompt, trend["argv"]
        # review prompt is advisory/non-gating
        rev_argv = by_t["stranske/Repo#3"]["argv"]
        assert any("non-gating" in tok.lower() for tok in rev_argv), rev_argv
        # wrapper prepends a PATH fix (local-bin tools) and ENDS with the agent subshell: `_spawn`
        # reads that status as the agent's and only then releases the claim (checked by running it)
        w = by_t["stranske/Repo#1"]["wrapped"]
        assert f"{REAL_HOME}/.local/bin" in w, w  # PATH fix independent of child HOME
        assert "ORCH_AGENT_RUNTIME" in w and "agent-runtime/cursor" in w, w
        assert "AGENT_CLI_CREDENTIAL_STORE=memory" in w and "CURSOR_DATA_DIR=" in w, w
        assert (
            "CURSOR_CONFIG_DIR=" in w and "NODE_COMPILE_CACHE=" in w and "export HOME=" not in w
        ), w
        assert "cursor-agent.env" in w and "$HOME/.cursor" not in w and "set -a" in w, w
        # gh keeps the dispatcher's config through the XDG redirect: a path, never a token.
        if agent_gh_config_disabled():
            assert "export GH_CONFIG_DIR=" not in w, w
        else:
            gh_pin = f"export GH_CONFIG_DIR={shlex.quote(str(gh_config_dir()))};"
            assert w.count(gh_pin) == 1 and w.index(gh_pin) < w.index("cursor-agent -p"), w
        # Never echo `w` here: a failure means it may hold a live token.
        assert not TOKEN_ASSIGNMENT.search(w), "a token is assigned in the wrapped command"
        assert w.rstrip().endswith(")") and f"{CLAIMS_PY}" not in w, w
        _selftest_spawn_reads_the_agents_status(by_t["stranske/Repo#1"], Path(tmp))
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
        assert gemini_dispatch is not None, "known gemini agent is dispatchable"
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

        def _fake_heartbeat(*args: Any, **kwargs: Any) -> bool:
            _hb_calls.append((args, kwargs))
            return True

        try:
            _caps.production_heartbeat = _fake_heartbeat
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
        assert custom is not None, "known delegated agent is dispatchable"
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
        assert custom_known is not None, "known delegated agent is dispatchable"
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
            assert delegated is not None, f"{agent} is a known dispatchable agent"
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
        # A codex stream: the marker counts only in what the agent said last.
        read_doc = json.dumps(
            {
                "type": "item.completed",
                "item": {
                    "type": "command_execution",
                    "aggregated_output": "OFFLOAD_INCOMPLETE: cannot write outside the roots",
                },
            }
        )

        def said(text: str) -> str:
            return json.dumps(
                {"type": "item.completed", "item": {"type": "agent_message", "text": text}}
            )

        finished = "\n".join([read_doc, said("Audit complete; report staged.")])
        assert _offload_incomplete_reason(finished) is not None, "text reading is unchanged"
        assert _offload_incomplete_reason(finished, codex_stream=True) is None
        stopped = "\n".join([read_doc, said("OFFLOAD_INCOMPLETE: gh is unauthenticated")])
        assert (
            _offload_incomplete_reason(stopped, codex_stream=True)
            == "agent reported OFFLOAD_INCOMPLETE"
        )
        assert _offload_incomplete_reason(read_doc, codex_stream=True) == (
            "agent returned no message"
        )
        assert _is_transient_network_failure("connection reset by peer", "", "")
        assert not _is_transient_network_failure("401 unauthorized", "", "")
        assert not _is_transient_network_failure("", "", "")
        assert not _is_transient_network_failure(
            "", "neither PlanModel nor RequestedModel specified", ""
        )
        captured = {}
        # The selftest intentionally monkeypatches these module attributes with lightweight
        # runtime doubles; their signatures are deliberately narrower than production callables.
        adapter_module: Any = adapters
        subprocess_module: Any = subprocess
        old_build_command = adapter_module.build_command
        old_subprocess_run = subprocess_module.run

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
            captured["commits_in_worktree"] = kwargs.get("commits_in_worktree")
            return ["printf", "OFFLOAD RESULT"]

        def fake_run(cmd, cwd=None, **_kwargs):
            captured["cmd"] = cmd
            captured["cwd"] = cwd
            captured["kwargs"] = _kwargs
            return Completed()

        try:
            adapter_module.build_command = fake_build_command
            subprocess_module.run = fake_run
            off = offload(
                "cursor",
                "Make a parallel-safe code proposal.",
                cwd=str(nongit),
                isolate=True,
                timeout=1,
            )
        finally:
            adapter_module.build_command = old_build_command
            subprocess_module.run = old_subprocess_run
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
            adapter_module.build_command = fake_build_command
            subprocess_module.run = fake_run
            codex_off = offload("codex", "Summarize this file.", cwd=str(nongit), timeout=1)
        finally:
            adapter_module.build_command = old_build_command
            subprocess_module.run = old_subprocess_run
        with feedback._conn() as c:
            worker = c.execute(
                "SELECT profile_id, operation_role FROM execution_attempts "
                "WHERE run_id=? AND operation_role='worker'",
                (codex_off["run_id"],),
            ).fetchone()
        assert captured.get("profile"), "the selected profile must reach build_command"
        # An offload is told never to commit, so it must never ask for the worktree git grant.
        assert not captured.get("commits_in_worktree"), captured
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
            adapter_module.build_command = fake_build_command
            subprocess_module.run = fake_run
            gem_off = offload(mute_seat, "Summarize this file.", cwd=str(nongit), timeout=1)
        finally:
            adapter_module.build_command = old_build_command
            subprocess_module.run = old_subprocess_run
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
                adapter_module.build_command = fake_build_command
                subprocess_module.run = fake_run
                res = offload(
                    seat, "Summarize.", cwd=str(nongit), timeout=1, research_round=round_id
                )
            finally:
                adapter_module.build_command = old_build_command
                subprocess_module.run = old_subprocess_run
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
            adapter_module.build_command = fake_build_command
            subprocess_module.run = fake_stream_run
            streamed = offload("cursor", "Summarize.", cwd=str(nongit), timeout=1)
        finally:
            adapter_module.build_command = old_build_command
            subprocess_module.run = old_subprocess_run
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
            adapter_module.build_command = fake_gemini_build_command
            subprocess_module.run = fake_run
            gem = offload(
                "gemini", "Inspect the isolated copy.", cwd=str(nongit), isolate=True, timeout=1
            )
        finally:
            adapter_module.build_command = old_build_command
            subprocess_module.run = old_subprocess_run
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
        assert "--print-timeout 1s" in " ".join(captured["cmd"]), captured["cmd"]

        captured.clear()

        def fake_waiting_run(cmd, cwd=None, **_kwargs):
            captured["cmd"] = cmd
            captured["cwd"] = cwd
            captured["kwargs"] = _kwargs
            return WaitingCompleted()

        try:
            adapter_module.build_command = fake_gemini_build_command
            subprocess_module.run = fake_waiting_run
            bad_gem = offload(
                "gemini", "Run tests and report JSON.", cwd=str(nongit), isolate=True, timeout=1
            )
        finally:
            adapter_module.build_command = old_build_command
            subprocess_module.run = old_subprocess_run
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
            adapter_module.build_command = fake_gemini_build_command
            subprocess_module.run = fake_empty_run
            empty_gem = offload("gemini", "Say READY.", cwd=str(nongit), isolate=True, timeout=1)
        finally:
            adapter_module.build_command = old_build_command
            subprocess_module.run = old_subprocess_run
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
            adapter_module.build_command = fake_gemini_build_command
            subprocess_module.run = fake_reset_then_success_run
            retry_gem = offload(
                "gemini",
                "Say READY after a transient reset.",
                cwd=str(nongit),
                isolate=True,
                timeout=1,
            )
            legacy_calls = retry_calls["count"]
            retry_calls["count"] = 0
            native_receipts = []
            native_gem = offload(
                "gemini",
                "One reserved native invocation.",
                cwd=str(nongit),
                isolate=True,
                timeout=1,
                max_network_retries=0,
                attempt_receipt_callback=lambda receipt: native_receipts.append(dict(receipt)),
            )
            assert native_gem["attempts"] == retry_calls["count"] == len(native_receipts) == 1
            assert native_gem["exit"] != 0 and native_gem["attempt_receipt_callback_called"]
        finally:
            adapter_module.build_command = old_build_command
            subprocess_module.run = old_subprocess_run
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
        assert legacy_calls == 2, legacy_calls

        # A zero exit with partial prose is still uncertain when this run's own agy
        # log says its provider turn is in progress. Do not spend a retry on top of it.
        captured.clear()
        uncertain_calls = {"count": 0}
        old_network_retries = os.environ.get("ORCH_OFFLOAD_NETWORK_RETRIES")

        class PartialTurnCompleted:
            returncode = 0
            stdout = "Partial answer while the provider is still working."
            stderr = ""

        def fake_partial_turn_run(cmd, cwd=None, **_kwargs):
            captured["cmd"] = cmd
            captured["cwd"] = cwd
            if cmd[:2] == ["bash", "-lc"]:
                uncertain_calls["count"] += 1
                _write_agy_log(
                    cmd,
                    cwd,
                    "Print timeout after 60s with turn in progress; returning partial output\n",
                )
                return PartialTurnCompleted()
            return Completed()

        try:
            os.environ["ORCH_OFFLOAD_NETWORK_RETRIES"] = "1"
            adapter_module.build_command = fake_gemini_build_command
            subprocess_module.run = fake_partial_turn_run
            uncertain_gem = offload(
                "gemini",
                "Return a bounded answer.",
                cwd=str(nongit),
                isolate=True,
                timeout=1,
            )
        finally:
            adapter_module.build_command = old_build_command
            subprocess_module.run = old_subprocess_run
            if old_network_retries is None:
                os.environ.pop("ORCH_OFFLOAD_NETWORK_RETRIES", None)
            else:
                os.environ["ORCH_OFFLOAD_NETWORK_RETRIES"] = old_network_retries
        assert uncertain_calls["count"] == 1, uncertain_calls
        assert uncertain_gem["exit"] != 0 and uncertain_gem["raw_exit"] == 0, uncertain_gem
        assert uncertain_gem["outcome_uncertain"] is True, uncertain_gem
        assert uncertain_gem["retry_safe"] is False, uncertain_gem
        assert uncertain_gem["outcome_status"] == "uncertain", uncertain_gem
        assert uncertain_gem["attempts"] == 1 and "retried" not in uncertain_gem, uncertain_gem

        captured.clear()

        def fake_timeout_run(cmd, cwd=None, **_kwargs):
            captured["cmd"] = cmd
            captured["cwd"] = cwd
            captured["kwargs"] = _kwargs
            if cmd[:2] == ["bash", "-lc"]:
                raise subprocess.TimeoutExpired(cmd, timeout=_kwargs.get("timeout"))
            return Completed()

        try:
            adapter_module.build_command = fake_build_command
            subprocess_module.run = fake_timeout_run
            timed_out = offload("cursor", "Timeout this offload.", cwd=str(nongit), timeout=1)
        finally:
            adapter_module.build_command = old_build_command
            subprocess_module.run = old_subprocess_run
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
            adapter_module.build_command = fake_build_command
            subprocess_module.run = fake_interrupt_run
            try:
                offload("cursor", "Interrupt this offload.", cwd=str(nongit), timeout=1)
                assert False, "KeyboardInterrupt should propagate after recording completion"
            except KeyboardInterrupt:
                pass
        finally:
            adapter_module.build_command = old_build_command
            subprocess_module.run = old_subprocess_run
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
        paused_reason = _remote_skip_reason({"agents:paused"}, "cursor")
        assert paused_reason is not None, "agents:paused must be skipped"
        assert "paused" in paused_reason
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
        # An UNANSWERED label read is refused, never read as "no labels" (2026-10-04): a failed read
        # used to return an empty set, and the rail labelled two PRs that carried agent:codex.
        unread_reason = _remote_skip_reason(None, "cursor", "gh exit 1: HTTP 502")
        assert unread_reason and "labels unread" in unread_reason, unread_reason
        assert "HTTP 502" in unread_reason, "the refusal names why the read did not answer"
        assert _remote_skip_reason(set(), "cursor") is None, "an ANSWERED empty list delegates"
        subprocess_any: Any = subprocess
        real_subprocess_run = subprocess_any.run
        label_answer: dict[str, Any] = {}
        label_posts: list = []

        def label_run(cmd, *_args, **_kwargs):
            if "--method" in cmd:  # the label WRITE; the read never carries a method
                label_posts.append(cmd)
                return subprocess.CompletedProcess(cmd, 0, "", "")
            if isinstance(label_answer["now"], BaseException):
                raise label_answer["now"]
            return label_answer["now"]

        unanswered = {
            "exit": subprocess.CompletedProcess([], 1, "", "gh: Server Error (HTTP 502)"),
            "timeout": subprocess.TimeoutExpired(["gh"], LABEL_READ_TIMEOUT_S),
            "no gh": FileNotFoundError(2, "No such file or directory", "gh"),
            "not json": subprocess.CompletedProcess([], 0, "<html>busy</html>", ""),
            "no list": subprocess.CompletedProcess([], 0, '{"message": "Moved"}', ""),
            "nameless": subprocess.CompletedProcess([], 0, '{"labels": [{"id": 1}]}', ""),
        }
        try:
            subprocess_any.run = label_run
            for why, answer in unanswered.items():
                label_answer["now"] = answer
                got, unread = _target_labels("o/r#7")
                assert got is None and unread, (why, got, unread)
            label_answer["now"] = subprocess.CompletedProcess([], 0, '{"labels": []}', "")
            assert _target_labels("o/r#7") == (set(), ""), "GitHub's 'no labels' is an answer"
            label_answer["now"] = subprocess.CompletedProcess(
                [], 0, '{"labels": [{"name": "status: ready"}, {"name": "agent:codex"}]}', ""
            )
            spaced = _target_labels("o/r#7")
            assert spaced == ({"status: ready", "agent:codex"}, ""), "a spaced name stays whole"
            label_answer["now"] = unanswered["exit"]
            dru = delegate_remote("cursor", "o/r#7", dry_run=True)
            assert dru["skip"] and "labels unread" in dru["skip"], dru
            assert dru["labels_read"] is False, dru
            aru = delegate_remote("cursor", "o/r#7")
            assert aru["applied"] is False and "labels unread" in aru["skip"], aru
            assert label_posts == [], f"an unread target was labelled: {label_posts}"
        finally:
            subprocess_any.run = real_subprocess_run

        # Exercised-capability tagging: each condition must mirror the capability's own heartbeat
        # condition, and must NOT fire otherwise — a tag that fires too widely credits a capability
        # for work it never touched, which is the one thing capability attribution refuses.
        # WIRING PIN (smallest fragment, count == 1): the offload record_run must pass the
        # rail-exercised ids. This stopped being true silently once before — the helper existed,
        # plan_dispatch consumed it, and the rail 340 of 340 gemini runs actually used did not.
        needle = "capability_ids=_exercised_capability_ids({}, " + "agent)"
        import pathlib as _pathlib

        src = _pathlib.Path(__file__).read_text()
        assert src.count(needle) == 1, f"offload rail tag wiring: {src.count(needle)} matches"
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
            p["profile_id"]
            for p in execution_profiles.profiles_for_agent("codex", transport="offload")
        }, codex_profile
        # Deterministic: the same agent must resolve to the SAME profile every time, or worker
        # attempts smear across three identities instead of accumulating against one.
        repeated_codex_profile = _select_offload_profile("codex", "mid")
        assert repeated_codex_profile is not None, "codex has a deterministic offload profile"
        assert repeated_codex_profile["profile_id"] == codex_profile["profile_id"]
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
        # Gemini cheap/mid ride 3.8 Flash and only full is Argon High. An advisory read takes
        # mid; explicit cheap/full requests retain their tier.
        for seat in ("codex", "claude", "gemini"):
            wanted = adapters.resolve_model(seat, adapters.DEFAULT_OFFLOAD_TIER)
            chosen = _select_offload_profile(seat, "offload")
            assert wanted, f"{seat} must have a tier ladder"
            assert chosen and chosen["requested_model"] == wanted, (seat, wanted, chosen)
        # And the reasoning tier is still REGISTERED, so full-tier work can still ask for it -- the
        # fix is choosing per task, not deleting the expensive rung.
        gemini_ids = {p["profile_id"] for p in execution_profiles.profiles_for_agent("gemini")}
        assert "gemini-4-argon-high" in gemini_ids, gemini_ids
        assert "gemini-3.8-flash-high" in gemini_ids, gemini_ids
        # The cheap rung is registered too, so the ladder the router names is fully reachable.
        assert "gemini-3.8-flash-low" in gemini_ids, gemini_ids

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

        old_advertised_catalog = adapter_module.advertised_catalog
        try:
            adapter_module.build_command = fake_gemini_build_command
            # This fixture reports a historical model, so its authority must be historical too.
            # The host's current catalog correctly refuses an old label it no longer advertises.
            adapter_module.advertised_catalog = lambda agent, **kwargs: (
                {
                    "models": ["gemini-3.6-flash-high"],
                    "pairs": {"gemini 3.6 flash (high)": "gemini-3.6-flash-high"},
                }
                if agent == "gemini"
                else old_advertised_catalog(agent, **kwargs)
            )
            subprocess_module.run = fake_agy_run
            agy_off = offload("gemini", "Summarize.", cwd=str(nongit), timeout=1)
        finally:
            adapter_module.build_command = old_build_command
            adapter_module.advertised_catalog = old_advertised_catalog
            subprocess_module.run = old_subprocess_run
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

        # `--help`, `-h`, a typo'd flag or a stray word must never reach run(): stub run() and
        # load_decision() to raise, so any fall-through fails the selftest instead of dispatching.
        _g = globals()
        _old_run, _old_load = _g["run"], _g["load_decision"]

        def _never(*_a, **_k):
            raise AssertionError("main() reached run() on input that must not dispatch")

        _calls: list[dict] = []

        def _patched_run(decision, *, dry_run=False, heartbeat=True):
            _calls.append({"dry_run": dry_run, "heartbeat": heartbeat})
            return {"launched": [], "skipped": [], "patched": True}

        try:
            import contextlib
            import io

            _g["run"], _g["load_decision"] = _never, _never
            for _argv in (
                ["--help"],
                ["-h"],
                ["--dry-run", "--help"],
                ["--no-heartbeat", "-h"],
            ):
                _out, _err = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(_out), contextlib.redirect_stderr(_err):
                    _rc = main(_argv)
                assert _rc == 0, ("help must exit 0 without dispatching", _argv, _rc)
                assert _USAGE.strip() in _out.getvalue(), (_argv, _out.getvalue())
                assert "unrecognised" not in _err.getvalue().lower(), (_argv, _err.getvalue())
            for _argv in (
                ["--bogus"],
                ["frobnicate"],
                ["--dry-run", "--hlep"],
                ["--selftest", "--bogus"],
            ):
                _out, _err = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(_out), contextlib.redirect_stderr(_err):
                    _rc = main(_argv)
                assert _rc == 2, ("unknown input must be refused with exit 2", _argv, _rc)
                assert "unrecognised" in _err.getvalue().lower(), (_argv, _err.getvalue())
                assert _USAGE.strip() in _err.getvalue(), (_argv, _err.getvalue())
            _g["run"], _g["load_decision"] = _patched_run, lambda: {}
            assert main(["--dry-run", "--no-heartbeat"]) == 0
            assert _calls == [{"dry_run": True, "heartbeat": False}], _calls
        finally:
            _g["run"], _g["load_decision"] = _old_run, _old_load

        # The CLI notice is observable behavior; exercise it without dispatching.
        _old_delegate = globals()["delegate"]
        _old_shadow = os.environ.get("ORCH_ROLE_SHADOW")
        try:
            globals()["delegate"] = lambda *a, **kw: {"ok": True}
            for _setting in (None, "0", "1"):
                os.environ.pop("ORCH_ROLE_SHADOW", None)
                if _setting is not None:
                    os.environ["ORCH_ROLE_SHADOW"] = _setting
                _out, _err = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(_out), contextlib.redirect_stderr(_err):
                    assert (
                        main(
                            [
                                "delegate",
                                "--agent",
                                "codex",
                                "--target",
                                "owner/repo#1",
                                "--prompt",
                                "Work",
                            ]
                        )
                        == 0
                    )
                assert _err.getvalue() == (
                    "role activation off (ORCH_ROLE_SHADOW unset)\n" if _setting is None else ""
                ), (_setting, _err.getvalue())
        finally:
            globals()["delegate"] = _old_delegate
            os.environ.pop("ORCH_ROLE_SHADOW", None)
            if _old_shadow is not None:
                os.environ["ORCH_ROLE_SHADOW"] = _old_shadow

        # muse (#612): one TSV row per exec, a failed write is REPORTED, and both launch paths
        # (offload attempt, delegate spawn) call the recorder. Break demo: remove either call.
        import io as _mio
        import tempfile as _mtf
        from contextlib import redirect_stderr as _mre

        _old_log = os.environ.get("ORCH_MUSE_USAGE_LOG")
        try:
            with _mtf.TemporaryDirectory(prefix="muse-usage-") as _mtd:
                os.environ["ORCH_MUSE_USAGE_LOG"] = str(Path(_mtd) / "u.log")
                _record_muse_usage(time.time() - 5, "offload", 0, 5.4)
                _rows = (Path(_mtd) / "u.log").read_text().splitlines()
                assert len(_rows) == 1 and _rows[0].split("\t")[1:] == ["offload", "0", "5"], _rows
                os.environ["ORCH_MUSE_USAGE_LOG"] = str(Path(_mtd) / "no" / "such" / "u.log")
                _mbuf = _mio.StringIO()
                with _mre(_mbuf):
                    _record_muse_usage(time.time(), "offload", 0, 1)
                assert "muse usage not recorded" in _mbuf.getvalue(), _mbuf.getvalue()
        finally:
            os.environ.pop("ORCH_MUSE_USAGE_LOG", None)
            if _old_log is not None:
                os.environ["ORCH_MUSE_USAGE_LOG"] = _old_log
        _msrc = Path(__file__).read_text()
        import re as _mre2

        _mn = "_record_muse" + "_usage("
        assert _msrc.count("def " + _mn) == 1, "one definition"
        _mgap = r"\s*\n(?:\s*#[^\n]*\n)*\s*"
        _attempt_src = _msrc[_msrc.index("for attempt in range(1, max_network_retries + 2):") :]
        assert (
            len(
                _mre2.findall(
                    r'if agent == "muse":' + r"[\s\S]{0,900}?" + _mre2.escape(_mn), _attempt_src
                )
            )
            == 1
        )
        assert (
            len(_mre2.findall(r'if d\["agent"\] == "muse":' + _mgap + _mre2.escape(_mn), _msrc))
            == 1
        )
        _saved_muse_home = REAL_HOME
        try:
            with _mtf.TemporaryDirectory(prefix="muse-wrapper-selftest-") as _fixture:
                globals()["REAL_HOME"] = Path(_fixture)
                _login = REAL_HOME / ".config/muse/auth.json"
                _login.parent.mkdir(parents=True)
                _login.write_text("{}")
                (REAL_HOME / "Library/Keychains").mkdir(parents=True)
                assert "unset META_API_KEY" in _agent_runtime_prelude("muse")
        finally:
            globals()["REAL_HOME"] = _saved_muse_home

        print(
            "dispatcher.py selftest: OK (plan→argv via adapters, task-type prompts, "
            "claim-release wrapper, worktree-seam fallback, offload no-commit guard + isolation, "
            "offload run_id ledger reconciliation + Gemini progress-only/log-tail fail-closed, heartbeat, bogus-agent skip, "
            "delegate_remote label + guards (an unread label list refuses), "
            "proxy-env scrub + ORCH_KEEP_PROXY + stdin=DEVNULL, "
            "offload profile selection, help/typo never dispatches)"
        )
    finally:
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)
        os.environ.pop("HANDOFF_DIR", None)
        adapters.ensure_muse_auth_link = _saved_muse_link
        AGENT_RUNTIME_DIR = old_agent_runtime_dir
        feedback.DB_PATH = old_feedback_db
        adapters.HANDOFF = old_adapters_handoff
        adapters.LEDGER = old_adapters_ledger


_RUN_FLAGS = frozenset({"--dry-run", "--no-heartbeat"})
_HELP_FLAGS = frozenset({"-h", "--help"})
_NO_VERB_FLAGS = _RUN_FLAGS | _HELP_FLAGS
_USAGE = """usage: dispatcher.py [--dry-run] [--no-heartbeat]
       dispatcher.py delegate ... | offload ... | review-corpus ... | --show-prompt <agent>
       dispatcher.py --selftest | --help

With no verb it EXECUTES routing-decision.json: spawns each assignment's agent detached and
writes the lane-yield heartbeat. --dry-run prints what it would spawn; --no-heartbeat skips
the heartbeat. Anything unrecognised is refused (exit 2) rather than dispatched."""


def main(argv: list[str]) -> int:
    # A bare `dispatcher.py --help` used to fall through to the LIVE run below — nothing
    # launched that day, but it wrote the heartbeat the lanes yield to (2026-09-02). Help and
    # typos must never dispatch: unknown input is refused loudly, the safe direction for the
    # one command that spawns agents.
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
    if argv == ["--selftest"]:
        _selftest()
        return 0
    if argv and argv[0] == "delegate":  # the orchestrator seat's hand
        import argparse

        p = argparse.ArgumentParser(prog="dispatcher.py delegate")
        p.add_argument("--agent", required=True)
        p.add_argument("--target", required=True)
        p.add_argument("--lane", default="opener")
        p.add_argument(
            "--branch-prefix", help="override opener prefix (infra: orch/; otherwise agent/)"
        )
        p.add_argument("--mode")
        p.add_argument("--profile-id", help="explicit immutable execution profile")
        p.add_argument("--task-type", default="implement")
        p.add_argument("--continue-pr", type=int, help="continue this confirmed linked open PR")
        p.add_argument(
            "--review-loop",
            action="store_true",
            help="require independent exact-head review after terminal writer release",
        )
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
        if "ORCH_ROLE_SHADOW" not in os.environ:
            print("role activation off (ORCH_ROLE_SHADOW unset)", file=sys.stderr)
        prompt = ns.prompt if ns.prompt is not None else Path(ns.prompt_file).read_text()
        review_kwargs: dict[str, Any] = {"review_loop": True} if ns.review_loop else {}
        out = delegate(
            ns.agent,
            ns.target,
            ns.lane,
            prompt,
            ns.mode,
            task_type=ns.task_type,
            profile_id=ns.profile_id,
            continue_pr=ns.continue_pr,
            influenced_by_role_run_ids=ns.influenced_by_role_run_id,
            **review_kwargs,
            **({"branch_prefix": ns.branch_prefix} if ns.branch_prefix is not None else {}),
        )
        print(json.dumps(out, default=str))
        return 0 if "error" not in out else 1
    if argv and argv[0] == "offload":  # read/summarize/research -> cheap agent -> result back
        import argparse

        p = argparse.ArgumentParser(prog="dispatcher.py offload")
        p.add_argument("--agent", required=True)
        p.add_argument("--cwd", default=".")
        p.add_argument("--mode")
        p.add_argument("--profile-id", help="explicit immutable execution profile")
        p.add_argument("--timeout", type=int, default=None)
        p.add_argument(
            "--research-round",
            help="bind this offload to a multi-agent research round (its experiment_id); see "
            "research_subjects.record_research_round — without it the run is not comparable evidence",
        )
        p.add_argument(
            "--task-type",
            help="what the work is (review, mechanical, implement, ...); default keeps the literal offload",
        )
        p.add_argument(
            "--subject",
            help="the work this run is for (owner/repo#N, or the repo for a repo audit); without it "
            "the run's subject is its cwd, which pools every run from that directory",
        )
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
                ns.agent,
                prompt,
                cwd=ns.cwd,
                mode=ns.mode,
                timeout=ns.timeout,
                isolate=ns.isolate,
                profile_id=ns.profile_id,
                research_round=ns.research_round,
                task_type=ns.task_type,
                subject_id=ns.subject,
            )
        except KeyboardInterrupt as interruption:
            out = {
                "agent": ns.agent,
                "exit": 130,
                "output": "",
                "error": "interrupted by orchestrator",
            }
            held = getattr(interruption, "held_result", None)
            if (
                isinstance(held, dict)
                and isinstance(held.get("run_id"), str)
                and held["run_id"]
                and held.get("outcome_status") == "uncertain"
                and held.get("outcome_uncertain") is True
                and held.get("retry_safe") is False
                and held.get("exit") == 70
                and held.get("raw_exit") == 130
            ):
                out.update(
                    run_id=held["run_id"],
                    held_result=held,
                    raw_exit=130,
                    outcome_status="uncertain",
                    outcome_uncertain=True,
                    retry_safe=False,
                )
        print(out["output"] if out.get("output") else json.dumps(out, default=str))
        if out.get("error"):
            print(f"[orchestrator] offload error: {out['error']}", file=sys.stderr)
        return 0 if out["exit"] == 0 and not out.get("error") else 1
    if argv and all(a in _NO_VERB_FLAGS for a in argv) and ("-h" in argv or "--help" in argv):
        print(_USAGE)
        return 0
    unknown = [a for a in argv if a not in _NO_VERB_FLAGS]
    if unknown:
        print(
            f"dispatcher.py: unrecognised argument(s) {unknown!r}; nothing dispatched.",
            file=sys.stderr,
        )
        print(_USAGE, file=sys.stderr)
        return 2
    dry = "--dry-run" in argv
    no_hb = "--no-heartbeat" in argv
    out = run(load_decision(), dry_run=dry, heartbeat=not no_hb)
    print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
