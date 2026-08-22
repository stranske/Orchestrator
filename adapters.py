#!/usr/bin/env python3
"""adapters.py — uniform dispatch interface over the heterogeneous agent CLIs.

build_command(agent, prompt, mode, cwd=...) -> argv ; dispatch(...) -> result + ledger row.
Reflects PR #2350 Rev-2b/2c/2d:
  - cursor has TWO modes: 'composer' (free/unlimited, the default) and
    'frontier:<model>' which draws the metered mid-tier pool (LOCAL_POLICY.md). Per Rev-2d the router
    sequences free-first then drains the prepaid frontier pool LATER in the cycle
    (use-it-or-lose-it), so the adapter just executes whichever mode it's handed.
  - aider runs from its ISOLATED venv (Rev-2c), never base/--user.
  - gemini via agy (Antigravity) is LANE-READY as of 1.0.8 — headless `-p` print mode
    verified, auth auto-loads from the real macOS keychain/home context while mutable app data is
    redirected with `--gemini_dir`; now wired into the route table.
Simple/legible (design §11). `--selftest` validates command construction offline.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import time
from collections.abc import Iterable
from pathlib import Path

import execution_profiles

HOME = Path.home()
HANDOFF = Path(os.environ.get("HANDOFF_DIR", HOME / ".codex" / "handoff"))
LEDGER = HANDOFF / "capacity-ledger.ndjson"
AIDER_BIN = HOME / ".codex" / "orchestrator" / "aider-venv" / "bin" / "aider"  # isolated (Rev-2c)
LOCAL_RUNTIME = Path(os.environ.get("ORCH_LOCAL_RUNTIME", HOME / ".codex" / "orchestrator"))
AGENT_RUNTIME = Path(os.environ.get("ORCH_AGENT_RUNTIME_DIR", LOCAL_RUNTIME / "agent-runtime"))

# Router emits a `mode` token per assignment: cursor -> 'composer'|'frontier'[:model];
# others -> 'cheap'|'mid'|'full'. A tier with no pinned id passes NO --model and lets the agent
# default (safe, never wrong). Every pinned id is validated against the CLI's own catalog where a
# probe exists (see MODEL_CATALOG_PROBES) so a vendor rename degrades instead of killing the seat.
#
# Tier research 2026-08-08 (verified against live CLIs + vendor docs):
#   codex  — GPT-5.6 ships a genuine 3-tier family: Sol (flagship $5/$30), Terra (workhorse
#            $2.50/$15, ~GPT-5.5 class), Luna (fastest/cheapest $1/$6). GA 2026-07-09.
#   claude — Claude 5 family: Opus 5 flagship, Sonnet 5 mid, Haiku 4.5 cheap.
#   gemini — agy offers Pro only at 3.1; the 3.5/3.6 generations are Flash-only. Flash is BOTH
#            newer and far cheaper on this compute-metered seat, so cheap/mid ride 3.6 Flash and
#            only `full` pays for 3.1 Pro.
#   vibe   — Mistral merged its coding line into ONE model (mistral-medium-3.5, 256k, replaced
#            Devstral 2 as the Vibe default). No tiers to pin; the CLI default is correct.
#   aider  — 'mistral/codestral-latest' is a floating alias that self-upgrades; pinning would be
#            a downgrade.
#   cursor — has real tiers, but `cursor-agent --list-models` needs auth that is currently absent
#            on this box. Left EMPTY deliberately: cursor draws a metered mid-tier pool (LOCAL_POLICY.md) and the historic
#            burn came from an unpinned frontier default, so guessed ids are the one thing not to
#            ship here. Populate from the live probe once `agent login` is restored.
# vibe is a single-model lane, so it has no tier map -- but the model IS known. Kept as a named
# constant so `test_vibe_model_matches_local_config` can check it against the CLI's own config and
# fail if either drifts. Empty MODEL_TIERS below means "no tiers to pin", never "model unknown".
VIBE_MODEL = "mistral-medium-3.5"

MODEL_TIERS: dict[str, dict[str, str]] = {
    "codex":  {"cheap": "gpt-5.6-luna", "mid": "gpt-5.6-terra", "full": "gpt-5.6-sol"},
    "claude": {"cheap": "claude-haiku-4-5", "mid": "claude-sonnet-5", "full": "claude-opus-5"},
    "gemini": {"cheap": "gemini-3.6-flash-low", "mid": "gemini-3.6-flash-high",
               "full": "gemini-3.1-pro-high"},
    "cursor": {},
    "vibe": {},
    "aider": {},
}
MODEL_TIER_NAMES = ("cheap", "mid", "full")     # ordered cheap -> expensive; the ceiling relies on it

# A seat can be high quality AND capacity-scarce — claude's weekly is frequently the binding
# constraint on this fleet. Rather than lying about what the family offers (which would delete the
# frontier option entirely), the tier map stays HONEST and a CEILING caps what routine routing may
# actually spend: a request for `full` on a capped seat quietly executes at the ceiling instead.
# The frontier model therefore remains permanently available — reachable by raising the ceiling
# (ORCH_CLAUDE_MAX_TIER=full) or naming it directly (ORCH_CLAUDE_MODEL_FULL / requested_model /
# an explicit profile) — without routine work spending it. To stop dispatching a seat ALTOGETHER
# and keep it purely for orchestration, use the existing 429-shed switch instead of a ceiling:
#   touch ~/.codex/handoff/capacity-shed/claude     (remove the file to re-enable)
AGENT_TIER_CEILING: dict[str, str] = {
    "claude": "mid",     # owner policy 2026-08-09: routine claude work runs Sonnet 5, not Opus 5
}
CURSOR_FRONTIER_DEFAULT = None      # require explicit 'frontier:<model>'; bare 'frontier' is unsafe
# Owner policy: cursor runs Composer and only Composer. Pinned by id because omitting --model
# selects `auto`, which is NOT Composer — it routes across every frontier model cursor advertises.
CURSOR_COMPOSER_MODEL = os.environ.get("ORCH_CURSOR_COMPOSER_MODEL", "composer-2.5").strip() or "composer-2.5"

# Agents whose CLI can enumerate its own models. Used to validate a pin before dispatch and to
# auto-resolve a replacement after a vendor rename. Agents absent here are never "unresolvable":
# an unprobeable CLI is UNKNOWN, and unknown must never shed a working seat.
MODEL_CATALOG_PROBES: dict[str, list[str]] = {
    "gemini": ["agy", "models"],
    "cursor": ["cursor-agent", "--list-models"],
}
# Credential files the probe must load, mirroring what dispatcher's wrapper sources at dispatch
# time (dispatcher.AGENT_ENV_FILES owns the dispatch-side copy; adapters can't import it without a
# cycle). Without this a probe run from a scrubbed/launchd env just reports 'Authentication
# required' and the catalog reads as UNKNOWN — safe, but useless.
MODEL_PROBE_ENV_FILES: dict[str, Path] = {
    "cursor": HOME / ".cursor" / "cursor-agent.env",
}

# Cheap, NON-BILLING "am I logged in?" checks. A lapsed credential fails exactly like the 2026-08-08
# model rot did — every dispatch dies, capacity keeps saying `ok` — so it gets the same pre-dispatch
# gate. Only commands that spend no tokens belong here: `claude -p` / `codex exec` would bill on
# every capacity poll, so those seats stay unchecked (documented, not silently omitted). gemini needs
# no entry: `agy models` is already a server call, so the catalog probe fails closed on bad auth.
# Probes differ in STRENGTH, and conflating them is false assurance:
#   "validates" — the command round-trips the credential to the server, so a dead key is caught.
#   "presence"  — the command only confirms a credential EXISTS. Verified 2026-08-09:
#                 `claude auth status` reports {"loggedIn": true} for a deliberately bogus token,
#                 and `cursor-agent status` reports "logged in" with a dead key. A presence probe
#                 catches a MISSING credential and nothing more; it would NOT have caught the
#                 revoked-token case, so it must never be reported as a clean bill of health.
# Only NON-BILLING commands may appear here (guarded by a test): `claude -p` / `codex exec` would
# bill on every capacity poll. gemini needs no entry — `agy models` is already a server call, so
# the catalog probe fails closed on bad auth.
AUTH_PROBES: dict[str, dict] = {
    # cursor: --list-models exercises the key (proven: an invalid key is rejected). NOT `status`,
    # which answers about the interactive session the headless lane never reads.
    "cursor": {"cmd": ["cursor-agent", "--list-models"], "strength": "validates"},
    # codex: 0.1s vs 12.2s for `codex doctor` (which scans every rollout file). PRESENCE IS
    # PERMANENT here, investigated and closed 2026-08-22 — not a pending upgrade. The CLI exposes
    # no non-billing round-trip: its whole command set is exec/review/login/logout/mcp/plugin/
    # app-server/doctor/sandbox/debug/apply/resume, `login` has only `status`, and the sole
    # server-touching alternative is `exec`, which BILLS. `doctor` is local and 12.2s.
    "codex": {
        "cmd": ["codex", "login", "status"], "strength": "presence",
        "limit": "permanent: codex exposes no non-billing round-trip (only `login status`, "
                 "local `doctor`, or billing `exec`) — re-probed 2026-08-22",
    },
    # claude: PRESENCE IS PERMANENT here too, for a different and subtler reason. `auth status`
    # returns account identity (email, orgId, orgName, subscriptionType), which is strictly more
    # than "a credential file exists" — but a claude.ai OAuth token can carry those in its own
    # claims, so the output does not DISTINGUISH a local decode from a server round-trip. Proving
    # the difference means presenting an INVALID credential to the live seat, which is not a safe
    # experiment on the owner's working auth. Upgrading on the strength of plausible-looking output
    # is exactly what the original note forbade.
    "claude": {
        "cmd": ["claude", "auth", "status"], "strength": "presence",
        "limit": "permanent: `auth status` returns token-claim identity that cannot be shown to "
                 "require a server round-trip without invalidating live credentials — "
                 "investigated 2026-08-22",
    },
    # gemini: `agy models` is a server round-trip AND non-billing, so it genuinely validates.
    # Same command as the catalog probe; the caches are separate but both are TTL'd.
    "gemini": {"cmd": ["agy", "models"], "strength": "validates"},
}
# Extra env the probe must set to reproduce the fleet's credential path exactly.
AUTH_PROBE_ENV: dict[str, dict[str, str]] = {
    "cursor": {"AGENT_CLI_CREDENTIAL_STORE": "memory"},
}
AUTH_CACHE_TTL_S = int(os.environ.get("ORCH_AUTH_CACHE_TTL_S") or 900)   # 15m
_AUTH_MEMO: dict = {}
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")   # CLIs colourize; strip before matching/reporting
# Matched against a FAILED probe's output. Anything else non-zero (CLI missing, timeout, network)
# stays UNKNOWN — an unreachable check must never shed a working seat.
_AUTH_FAIL_RE = re.compile(
    r"(authentication required|not authenticated|unauthorized|forbidden|\b40[13]\b"
    r"|log ?in required|please run .?(agent )?login|token .*(revoked|expired)|not logged in"
    r"|api key .{0,20}invalid|invalid api key)",
    re.IGNORECASE,
)

# agy's model ids carry their own effort tier (`gemini-3.1-pro-high`), which is why a bare family
# name is rejected: `--model gemini-2.5-pro` died with `invalid model selection ... not recognized
# as a known model or custom model in settings`. FIX 2026-08-08: the pin below had rotted through a
# Google rename and killed EVERY gemini offload (exit 1), while capacity.py still advertised the seat
# as `ok` — so the pin is now VALIDATED against `agy models` at dispatch time and only used verbatim
# when that list can't be read. Pro tier stays the default: gemini is the fleet's big-context seat.
DEFAULT_GEMINI_MODEL = MODEL_TIERS["gemini"]["full"]
GEMINI_MODEL_CACHE = AGENT_RUNTIME / "gemini" / "advertised-models.json"
GEMINI_MODEL_CACHE_TTL_S = int(os.environ.get("ORCH_GEMINI_MODEL_CACHE_TTL_S") or 21600)  # 6h
_GEMINI_VERSION_RE = re.compile(r"(\d+)\.(\d+)")
_ADVERTISED_MEMO: dict = {}         # per-agent in-process memo; one CLI probe per agent per run

# Offloads are advisory READS ("summarize 200 pages"), not flagship reasoning — but offload() has
# always defaulted to mode='full', so a codex offload burned Sol and a gemini offload burned Pro.
# The mid tier is the right home for that work; override per-run with ORCH_OFFLOAD_TIER.
DEFAULT_OFFLOAD_TIER = os.environ.get("ORCH_OFFLOAD_TIER", "mid").strip() or "mid"
CODEX_PROFILE_BIN = Path(
    os.environ.get(
        "ORCH_CODEX_PROFILE_BIN",
        "/Applications/ChatGPT.app/Contents/Resources/codex",
    )
)


def profile_codex_binary() -> str:
    """Use the version-capable bundled CLI for exact profiles or fail closed."""
    if not CODEX_PROFILE_BIN.is_file():
        raise RuntimeError(
            "exact Codex profiles require ORCH_CODEX_PROFILE_BIN pointing to a "
            "version-capable Codex binary; refusing PATH fallback"
        )
    return str(CODEX_PROFILE_BIN)


def codex_bypass_inner_sandbox() -> bool:
    """Whether child Codex should avoid applying a second local sandbox.

    Detached Codex runs launched from an interactive Codex seat already inherit
    the outer seatbelt sandbox. Applying another Codex seatbelt inside that
    process fails on macOS with ``sandbox_apply: Operation not permitted`` before
    even simple shell commands can run. Cron/launchd runs do not carry
    ``CODEX_SANDBOX``, so they keep the normal Codex sandbox unless explicitly
    overridden.
    """
    override = os.environ.get("ORCH_CODEX_BYPASS_INNER_SANDBOX")
    if override is not None:
        return override.strip().lower() not in {"0", "false", "no", "off"}
    return bool(os.environ.get("CODEX_SANDBOX"))


def parse_model_catalog(text: str) -> list[str]:
    """Model ids from a CLI catalog listing.

    Handles the two shapes the fleet's CLIs emit, and REQUIRES a separator so prose can't be
    mistaken for an id:
      agy    — '<id>\\t<Human Label>'
      cursor — '<id> - <Human Label>'
    Banners ('Fetching available models...', 'Available models') carry neither separator and are
    dropped, as is any candidate id containing a space. A chattier CLI release degrades to an
    empty list — i.e. UNKNOWN — rather than to a bogus model id.
    """
    ids = []
    for line in (text or "").splitlines():
        if "\t" in line:
            head = line.split("\t", 1)[0]
        elif " - " in line:
            head = line.split(" - ", 1)[0]
        else:
            continue
        head = head.strip()
        if not head or " " in head or head.startswith("-"):
            continue
        ids.append(head)
    return ids


parse_agy_models = parse_model_catalog          # back-compat alias


def _model_probe_enabled() -> bool:
    """Kill-switch for the CLI catalog probes; off => pinned models only, no subprocess."""
    flag = os.environ.get("ORCH_MODEL_PROBE", os.environ.get("ORCH_GEMINI_MODEL_PROBE", "1"))
    return flag.strip().lower() not in {"0", "false", "no", "off"}


def _catalog_cache_path(agent: str) -> Path:
    return AGENT_RUNTIME / agent / "advertised-models.json"


def _probe_env(agent: str) -> dict | None:
    """os.environ plus the agent's credential file, or None when there is nothing to add.

    Parses the same bare `KEY=value` / `export KEY=value` files the dispatcher sources. Values are
    never logged; a malformed or unreadable file degrades to the ambient environment.
    """
    path = MODEL_PROBE_ENV_FILES.get(agent)
    if not path or not path.is_file():
        return None
    env = dict(os.environ)
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if line.startswith("export "):
                line = line[len("export "):].strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            env[key.strip()] = value.strip().strip('"').strip("'")
    except OSError:
        return None
    return env


def advertised_models(agent: str, *, refresh: bool = False, timeout_s: int = 30) -> list[str]:
    """Model ids the installed CLI for `agent` actually offers, or [] when it can't be read.

    Catalog probes are ~3s network calls, so results are cached on disk per agent (TTL) and shared
    by the dispatcher and capacity's preflight. An EMPTY list means UNKNOWN, never "nothing is
    advertised" — callers must not read it as evidence that a model is missing. Agents with no
    probe registered always return [] and are therefore never judged unresolvable.
    """
    probe = MODEL_CATALOG_PROBES.get(agent)
    if not probe or not _model_probe_enabled():
        return []
    now = time.time()
    cache_path = _catalog_cache_path(agent)
    if not refresh:
        memo = (_ADVERTISED_MEMO.get(agent) or {})
        if memo.get("models") and now - float(memo.get("ts") or 0) <= GEMINI_MODEL_CACHE_TTL_S:
            return list(memo["models"])
        try:
            cached = json.loads(cache_path.read_text())
            if now - float(cached.get("ts") or 0) <= GEMINI_MODEL_CACHE_TTL_S:
                models = [str(m) for m in (cached.get("models") or [])]
                if models:
                    _ADVERTISED_MEMO[agent] = {"ts": cached.get("ts"), "models": models}
                    return models
        except (OSError, ValueError, TypeError):
            pass
    try:
        proc = subprocess.run(probe, capture_output=True, text=True, env=_probe_env(agent),
                              timeout=timeout_s, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return []                            # CLI missing/hung => unknown, not "unavailable"
    if proc.returncode != 0:
        return []                            # includes 'Authentication required' => unknown
    models = parse_agy_models(proc.stdout)
    if not models:
        return []
    _ADVERTISED_MEMO[agent] = {"ts": int(now), "models": models}
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps({"ts": int(now), "models": models}))
    except OSError:
        pass                                 # cache is an optimization, never a hard dependency
    return models


def agy_advertised_models(*, refresh: bool = False, timeout_s: int = 30) -> list[str]:
    """Back-compat shim for the gemini-only probe."""
    return advertised_models("gemini", refresh=refresh, timeout_s=timeout_s)


def auth_health(agent: str, *, refresh: bool = False, timeout_s: int = 30) -> dict:
    """Whether `agent`'s CLI can authenticate, WITHOUT spending tokens.

    Returns {agent, authenticated, checked, reason}. `checked=False` means no probe exists or the
    probe could not run — `authenticated` is then True by convention, because an unrunnable check
    is not evidence of a bad credential and must never shed a working seat. Only an explicit
    auth-failure signal in a failed probe's output flips `authenticated` to False.
    """
    spec = AUTH_PROBES.get(agent)
    if not spec or not _model_probe_enabled():
        return {"agent": agent, "authenticated": True, "checked": False, "strength": None,
                "reason": "no auth probe for this agent"}
    probe, strength = spec["cmd"], spec["strength"]
    now = time.time()
    memo = _AUTH_MEMO.get(agent)
    if not refresh and memo and now - float(memo.get("ts") or 0) <= AUTH_CACHE_TTL_S:
        return dict(memo["result"])
    env = dict(_probe_env(agent) or os.environ)
    env.update(AUTH_PROBE_ENV.get(agent) or {})
    try:
        proc = subprocess.run(probe, capture_output=True, text=True, env=env,
                              timeout=timeout_s, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"agent": agent, "authenticated": True, "checked": False, "strength": strength,
                "reason": f"auth probe could not run ({type(exc).__name__})"}
    # OUTPUT-driven, not returncode-driven: cursor-agent exits 0 while printing "Not logged in"
    # and while warning that the API key is invalid, so trusting the exit status reads a dead
    # credential as healthy. The failure text is the reliable signal.
    blob = _ANSI_RE.sub("", f"{proc.stdout or ''}\n{proc.stderr or ''}").strip()
    if _AUTH_FAIL_RE.search(blob):
        # A failure signal is trustworthy from EITHER strength: presence probes still detect an
        # absent credential, they just cannot vouch that a present one still works.
        result = {"agent": agent, "authenticated": False, "checked": True, "strength": strength,
                  "reason": next((ln.strip() for ln in blob.splitlines()
                                  if _AUTH_FAIL_RE.search(ln)), "auth failure")[:160]}
    elif proc.returncode == 0 and blob:
        result = {"agent": agent, "authenticated": True, "checked": True, "strength": strength,
                  "reason": (blob.splitlines()[0][:120] if strength == "validates"
                             else "credential present (presence-only check): "
                                  + " ".join(blob.split())[:80])}
    else:
        return {"agent": agent, "authenticated": True, "checked": False, "strength": strength,
                "reason": f"auth probe exited {proc.returncode} without a usable signal"}
    _AUTH_MEMO[agent] = {"ts": now, "result": result}
    return dict(result)


def _rank_gemini_model(model_id: str) -> tuple:
    """Sort key for auto-picking a replacement seat model (lower wins).

    Pro before Flash (gemini is the large-context read seat), newest version first, then the
    highest effort tier. Only used when the pinned default has been renamed out from under us.
    """
    match = _GEMINI_VERSION_RE.search(model_id)
    major, minor = (int(match.group(1)), int(match.group(2))) if match else (0, 0)
    family = 0 if "pro" in model_id else 1
    tier = next((i for i, t in enumerate(("high", "medium", "low")) if model_id.endswith("-" + t)), 3)
    return (family, -major, -minor, tier, model_id)


def _tier_override(agent: str, tier: str) -> tuple[str, str] | tuple[None, None]:
    """Operator pin for one (agent, tier), as (value, env-var-name)."""
    name = f"ORCH_{agent.upper()}_MODEL_{tier.upper()}"
    value = os.environ.get(name, "").strip()
    if value:
        return value, name
    if agent == "gemini" and tier == "full":      # legacy single-knob override, kept working
        legacy = os.environ.get("ORCH_GEMINI_MODEL", "").strip()
        if legacy:
            return legacy, "ORCH_GEMINI_MODEL"
    return None, None


def model_health(agent: str, tier: str = "full", *, refresh: bool = False) -> dict:
    """Resolve the model for one (agent, tier) AND report whether the CLI actually offers it.

    Returns {agent, tier, model, source, advertised, resolvable, reason}. Rules that matter:
      - `model=None` means "pass no --model, let the CLI default" — always resolvable, never a fault.
      - `advertised == []` means the catalog could NOT be read (no probe, CLI missing, offline,
        unauthenticated, probe disabled). `resolvable` stays True: an unreadable list is not
        evidence of a bad pin, and must never shed a working seat.
      - An explicit env override is honoured verbatim (operator intent wins) but reported
        unresolvable when the catalog contradicts it, so the seat surfaces instead of exiting 1.
      - Otherwise a renamed-away pin auto-resolves to the closest live sibling.
    """
    tiers = MODEL_TIERS.get(agent) or {}
    override, env_name = _tier_override(agent, tier)
    pinned = override or tiers.get(tier)
    source = f"env:{env_name}" if override else ("pinned_default" if pinned else "cli_default")
    base = {"agent": agent, "tier": tier, "source": source}
    if not pinned:
        return {**base, "model": None, "advertised": [], "resolvable": True,
                "reason": f"no pinned model for {agent}/{tier}; CLI default applies"}
    advertised = advertised_models(agent, refresh=refresh)
    if not advertised:
        return {**base, "model": pinned, "advertised": [], "resolvable": True,
                "reason": f"{agent} model catalog unavailable; using {pinned} unverified"}
    if pinned in advertised:
        return {**base, "model": pinned, "advertised": advertised, "resolvable": True,
                "reason": f"{pinned} is advertised by {agent}"}
    if override:
        return {**base, "model": override, "advertised": advertised, "resolvable": False,
                "reason": (f"{env_name}={override!r} is not advertised by {agent} "
                           f"(offered: {', '.join(advertised)})")}
    picked = _auto_resolve(agent, tier, pinned, advertised)
    if not picked:
        return {**base, "model": pinned, "advertised": advertised, "resolvable": False,
                "reason": (f"pinned {pinned} is not advertised by {agent} and no sibling matched "
                           f"(offered: {', '.join(advertised)})")}
    return {**base, "model": picked, "source": "auto_from_catalog", "advertised": advertised,
            "resolvable": True,
            "reason": f"pinned {pinned} no longer advertised by {agent}; auto-resolved to {picked}"}


def _auto_resolve(agent: str, tier: str, pinned: str, advertised: list[str]) -> str | None:
    """Closest live sibling for a renamed-away pin, or None when nothing plausibly matches.

    Only gemini has a ranking rule today (its ids encode family+version+effort). For other agents
    a rename is reported rather than guessed: silently swapping a metered seat's model is worse
    than saying the pin is stale.
    """
    if agent != "gemini":
        return None
    family = "pro" if "pro" in pinned else "flash" if "flash" in pinned else None
    candidates = [m for m in advertised if m.startswith("gemini-")]
    if not candidates:
        return None
    same_family = [m for m in candidates if family and family in m]
    return sorted(same_family or candidates, key=_rank_gemini_model)[0]


def resolve_model(agent: str, tier: str = "full") -> str | None:
    """Model id to pass as --model for one (agent, tier), or None to accept the CLI default."""
    return model_health(agent, tier)["model"]


def tier_ceiling(agent: str) -> str | None:
    """Highest tier routine routing may spend on this seat, or None for uncapped."""
    value = os.environ.get(f"ORCH_{agent.upper()}_MAX_TIER", "").strip().lower()
    if not value:
        value = AGENT_TIER_CEILING.get(agent, "")
    return value if value in MODEL_TIER_NAMES else None


def effective_tier(agent: str, tier: str) -> str:
    """`tier`, clamped down to the seat's ceiling. Never raises a tier, only lowers it."""
    cap = tier_ceiling(agent)
    if not cap or tier not in MODEL_TIER_NAMES:
        return tier
    return cap if MODEL_TIER_NAMES.index(tier) > MODEL_TIER_NAMES.index(cap) else tier


def _tier_model(agent: str, mode: str | None) -> str | None:
    """Model for a router `mode` token, or None to leave --model off.

    Only the three tier tokens select a model. Anything else (None, 'assess', 'offload',
    'composer', 'frontier') deliberately falls through to the CLI default, preserving the
    long-standing "never pin what we haven't verified" behaviour for non-tier modes.
    An explicit `requested_model`/profile bypasses this entirely, so the ceiling constrains
    ROUTINE routing without ever making the frontier model unreachable.
    """
    if mode not in MODEL_TIER_NAMES:
        return None
    return resolve_model(agent, effective_tier(agent, mode))


def gemini_model_health(*, refresh: bool = False) -> dict:
    """Back-compat shim: health of the gemini seat's full (Pro) tier."""
    return model_health("gemini", "full", refresh=refresh)


def gemini_model() -> str:
    """Model passed to agy print mode for the full tier.

    Antigravity 1.0.10 can fail print mode with no stdout when neither PlanModel nor RequestedModel
    is specified, so this is always explicit. Resolved against `agy models` (cached) so a Google
    rename degrades to a live sibling instead of killing the seat.
    """
    return resolve_model("gemini", "full") or DEFAULT_GEMINI_MODEL


def model_identity(agent: str, mode: str | None = None, profile=None) -> str | None:
    """Best-effort stable model/mode tag for drift detection.

    Some CLIs hide the exact default model. In those cases record the adapter's
    chosen lane/mode rather than guessing a vendor model string; that is still
    enough to notice a switch from one routed capability path to another.
    """
    if profile is not None:
        selected = execution_profiles.get_profile(profile)
        if selected["agent"] != agent:
            raise ValueError(f"profile {selected['profile_id']} does not belong to {agent}")
        return selected["requested_model"]
    if agent in ("codex", "claude"):
        tiered = _tier_model(agent, mode)
        if tiered:
            # Records the id we ACTUALLY sent instead of a generic lane tag. This lands in
            # `runs.model` only — it is NOT provenance: research_scheduler.model_drifted() reads
            # execution_attempts.resolved_model and deliberately ignores legacy runs.model tags,
            # and feedback.record_execution_attempt still rejects adapter tags outright.
            return tiered
        return f"{agent}:{mode or 'full'}:default"
    if agent == "cursor":
        # The `cursor:` prefix STAYS. Tried stripping it 2026-08-22 and reverted: it is not
        # ignorance about the model -- `--model` is sent bare and `CURSOR_COMPOSER_MODEL` names it --
        # it records WHICH SEAT served the work, and this seat is metered separately. `runs.model`
        # is a drift tag, so the router belongs in it; this module's own selftest asserts the prefix
        # and four other modules depend on it. The bare vendor id lives on the execution PROFILE's
        # `requested_model`, which is what a provider-resolved model is compared against.
        if mode and mode.startswith("frontier") and ":" in mode:
            return f"cursor:{mode.split(':', 1)[1]}"
        return f"cursor:{CURSOR_COMPOSER_MODEL}"
    if agent == "vibe":
        # NAMED, not a lane tag. `vibe:default` said nothing, which made this seat look permanently
        # unidentifiable when the model is simply single-lane. Read from the CLI's own config on
        # 2026-08-22: `active_model = "mistral-medium-3.5"`, matching the 2026-08-08 tier research.
        #
        # This is the REQUESTED identity. Note the config maps that alias to provider id
        # `mistral-vibe-cli-latest`, which FLOATS -- so an immutable resolved identity still has to
        # come from the provider's own response, exactly as for aider's `codestral-latest`. Naming
        # the request honestly is an improvement; it is not a resolution claim.
        return VIBE_MODEL
    if agent == "aider":
        return "mistral/codestral-latest"
    if agent == "gemini":
        # The `agy:` prefix STAYS, same reasoning as cursor. agy is a multi-provider ROUTER -- its
        # advertised-models probe lists gemini, claude AND gpt-oss ids -- so recording the router
        # beside the model is real provenance, not a placeholder for an unknown.
        return f"agy:{_tier_model('gemini', mode) or gemini_model() or 'default'}"
    return None


# ---------------------------------------------------------------------------
# CLI-REPORTED EXECUTION IDENTITY
#
# `model_identity()` above is REQUEST-side -- it says what we asked for, lands in `runs.model`, and
# is explicitly not provenance. That left `execution_attempts.resolved_model` with no writer at all
# outside the quarantined trial bridge: 1,374 attempts, 25 of them `worker`, and NONE resolved, so
# `unresolved_model_provenance` blocked every research-claiming completion event in the system
# (203 of 203 on 2026-08-22). The only legal way out is what §2 already names -- "a local Codex
# session rollout may establish CLI-reported identity" -- so read the identity the CLI itself
# recorded for the run, and read it from the CLI's own log rather than inferring it.
#
# WHAT THIS DELIBERATELY DOES NOT DO: it never falls back to the requested model, the catalog, or a
# lane tag. A run whose CLI left no identity returns a NAMED reason and stays unresolved. Grepping a
# run's stdout for a model-shaped string was tried and rejected on 2026-08-22: the offload logs are
# full of `gpt-4o-mini` and `CostModel` because the AGENT WAS EDITING CODE ABOUT MODELS, and a
# fabricated identity is worse than a skipped event.
CODEX_SESSIONS = Path(os.environ.get("ORCH_CODEX_SESSIONS_DIR", HOME / ".codex" / "sessions"))
CLAUDE_PROJECTS = Path(os.environ.get("ORCH_CLAUDE_PROJECTS_DIR", HOME / ".claude" / "projects"))
# Seats whose CLI leaves no per-session identity log we can read. Named, not silently absent, so
# per-agent mining coverage can say WHY a seat is unminable instead of showing an empty count.
NO_SESSION_LOG_AGENTS = {
    "cursor": "cursor-agent writes no per-session model log under ~/.cursor",
    "gemini": "agy/antigravity writes no per-session model log we can join to a run",
    "vibe": "vibe writes no per-session transcript; its model is single-lane config, not a report",
    "aider": "aider writes no per-session model log; codestral-latest floats anyway",
}
ROLLOUT_TS_RE = re.compile(r"rollout-(\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2})")


def _claude_project_dir(workspace: str | Path) -> Path:
    """Claude mangles a cwd into a directory name by replacing `/` and `.` with `-`."""
    resolved = str(Path(workspace).expanduser().resolve())
    return CLAUDE_PROJECTS / re.sub(r"[/.]", "-", resolved)


def _first_real_model(values: Iterable[str]) -> str | None:
    """First value that survives the resolved-model contract, else None."""
    import feedback  # local: adapters is imported by feedback's callers, not the reverse

    for value in values:
        try:
            accepted = feedback.validate_resolved_worker_model(value)
        except ValueError:
            continue  # `<synthetic>`, `codex:full:default` -- a marker, not an identity
        if accepted:
            return accepted
    return None


def _models_in_jsonl(path: Path, *, limit_bytes: int = 4_000_000) -> list[str]:
    """Every `"model": "..."` string in a JSONL log, newest occurrence last.

    Read as text on purpose. These logs are large and deeply nested (codex records the model at
    seven different paths), and a regex over the raw line is both cheaper and more robust to the
    CLI moving the field than walking a schema we do not own.
    """
    try:
        with path.open("r", errors="ignore") as handle:
            blob = handle.read(limit_bytes)
    except OSError:
        return []
    return re.findall(r'"model"\s*:\s*"([^"]+)"', blob)


def _codex_rollout_for(workspace: str | Path, started_ts: int | None, window_s: int) -> Path | None:
    """The rollout file whose session ran in `workspace`.

    Bounded on purpose: `codex doctor` takes 12.2s because it scans every rollout file, and this
    runs inside reconcile. The filename carries a wall-clock stamp, so a run with a known start is
    narrowed to its own window before any file is opened.
    """
    target = str(Path(workspace).expanduser().resolve())
    candidates = sorted(CODEX_SESSIONS.rglob("rollout-*.jsonl"), reverse=True)
    for path in candidates:
        if started_ts is not None:
            match = ROLLOUT_TS_RE.search(path.name)
            if match:
                try:
                    stamp = time.mktime(time.strptime(match.group(1), "%Y-%m-%dT%H-%M-%S"))
                except ValueError:
                    stamp = None
                if stamp is not None and abs(stamp - started_ts) > window_s:
                    continue
        try:
            with path.open("r", errors="ignore") as handle:
                head = handle.readline()
        except OSError:
            continue
        if target in head:
            return path
    return None


def cli_reported_model(
    agent: str,
    workspace: str | Path | None,
    *,
    started_ts: int | None = None,
    window_s: int = 7200,
) -> dict:
    """Identity the agent's own CLI recorded for the run in `workspace`.

    Always returns a dict, never None, and always says why when it says nothing:
    ``{"model": str | None, "cli_version": str | None, "source": str | None, "reason": str | None}``.
    An unresolved answer with a named reason is the whole point -- silence here was indistinguishable
    from "no such run".
    """
    blank = {"model": None, "cli_version": None, "source": None, "reason": None}
    if agent in NO_SESSION_LOG_AGENTS:
        return {**blank, "reason": f"no_cli_session_log:{NO_SESSION_LOG_AGENTS[agent]}"}
    if not workspace:
        return {**blank, "reason": "no_workspace_recorded_for_run"}
    if agent == "codex":
        path = _codex_rollout_for(workspace, started_ts, window_s)
        if path is None:
            return {**blank, "reason": "no_codex_rollout_matched_workspace"}
        model = _first_real_model(_models_in_jsonl(path))
        if not model:
            return {**blank, "reason": "codex_rollout_named_no_real_model"}
        version = None
        try:
            meta = json.loads(path.open("r", errors="ignore").readline() or "{}")
            version = ((meta.get("payload") or {}) or {}).get("cli_version")
        except (OSError, json.JSONDecodeError):
            version = None
        return {"model": model, "cli_version": version, "source": str(path), "reason": None}
    if agent == "claude":
        directory = _claude_project_dir(workspace)
        if not directory.is_dir():
            return {**blank, "reason": "no_claude_transcript_dir_for_workspace"}
        newest = sorted(
            directory.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True
        )
        for path in newest[:4]:
            model = _first_real_model(_models_in_jsonl(path))
            if model:
                return {
                    "model": model,
                    "cli_version": None,
                    "source": str(path),
                    "reason": None,
                }
        return {**blank, "reason": "claude_transcript_named_no_real_model"}
    return {**blank, "reason": f"no_cli_identity_reader_for_agent:{agent}"}


def build_command(
    agent: str,
    prompt: str,
    mode: str | None = None,
    out_file: str | None = None,
    cwd: str | Path | None = None,
    *,
    profile=None,
    transport: str | None = None,
    permission_mode: str | None = None,
    reasoning_effort: str | None = None,
    requested_model: str | None = None,
):
    """Construct the CLI argv for one agent. `mode` is agent-specific.

    cursor: mode None/'composer' -> Composer/Auto (free, no --model);
            'frontier:<model>'   -> draws the metered mid-tier pool with that frontier model.
    """
    selected_profile = execution_profiles.get_profile(profile) if profile is not None else None
    if selected_profile:
        if selected_profile["agent"] != agent:
            raise ValueError(f"profile {selected_profile['profile_id']} does not belong to {agent}")
        if transport and transport not in selected_profile["transport_support"]:
            raise ValueError(f"profile {selected_profile['profile_id']} does not support {transport}")
        immutable = {"reasoning_effort": reasoning_effort, "requested_model": requested_model}
        for key, override in immutable.items():
            if override is not None and override != selected_profile[key]:
                raise ValueError(f"{key} override contradicts immutable profile {selected_profile['profile_id']}")
        profile_permission = selected_profile["permission_mode"]
        if permission_mode is None:
            permission_mode = profile_permission
        elif permission_mode != profile_permission and permission_mode != "read-only":
            raise ValueError(
                f"permission override may narrow to read-only but cannot widen profile {selected_profile['profile_id']}"
            )
        reasoning_effort = selected_profile["reasoning_effort"]
        requested_model = selected_profile["requested_model"]
        mode = mode if mode is not None else selected_profile.get("legacy_adapter_mode")
    if agent == "codex":
        cmd = [profile_codex_binary() if selected_profile else "codex", "exec", "--skip-git-repo-check"]
        if cwd is not None:
            cmd += ["--cd", str(Path(cwd).expanduser().resolve())]
        # The outer-seat bypass may preserve a workspace-write profile because
        # the parent seatbelt remains authoritative. It must never defeat an
        # explicit read-only narrowing: in that case keep the child sandbox and
        # fail closed if macOS refuses nested seatbelt application.
        if (
            codex_bypass_inner_sandbox()
            and mode != "assess"
            and permission_mode != "read-only"
        ):
            cmd += ["--dangerously-bypass-approvals-and-sandbox"]
        else:
            sandbox = "read-only" if mode == "assess" else (permission_mode or "workspace-write")
            cmd += ["--sandbox", sandbox]
        if mode != "assess":
            cmd += ["--json"]
        if requested_model:
            cmd += ["--model", requested_model]
        else:
            tiered = _tier_model("codex", mode)
            if tiered:
                cmd += ["--model", tiered]
        if reasoning_effort:
            cmd += ["-c", f'model_reasoning_effort="{reasoning_effort}"']
        if out_file:
            cmd += ["--output-last-message", out_file]
        return cmd + [prompt]
    if agent == "claude":
        cmd = ["claude", "-p", prompt, "--dangerously-skip-permissions"]
        if requested_model:
            cmd += ["--model", requested_model]
        else:
            tiered = _tier_model("claude", mode)
            if tiered:
                cmd += ["--model", tiered]
        return cmd
    if agent == "cursor":
        cmd = [
            "cursor-agent", "-p", prompt,
            "--force", "--output-format", "text",
            "--trust", "--workspace", ".",
        ]
        if requested_model:
            cmd += ["--model", requested_model]
        elif mode and mode.startswith("frontier") and ":" in mode:
            cmd += ["--model", mode.split(":", 1)[1]]   # explicit opt-in -> spends the metered mid-tier pool
        else:
            # OWNER POLICY 2026-08-08: Composer ONLY, never frontier. Passing no --model does NOT
            # mean Composer — it means `auto`, which selects across all 193 advertised models
            # (Opus 5, GPT-5.3-codex-xhigh, Grok 4.5 ...). Pinning composer explicitly is the
            # difference between "probably cheap" and "Composer, guaranteed". Bare 'frontier'
            # lands here too, so a stray frontier hint can never blind-spend the pool.
            cmd += ["--model", CURSOR_COMPOSER_MODEL]
        return cmd
    if agent == "vibe":
        # Mistral Vibe CLI on subscription login (flat all-day; PAYG overflow). Headless:
        # --prompt + --auto-approve + --output text. FIX 2026-06-15: add --trust. Without it vibe runs
        # UNTRUSTED in the worktree and SILENTLY IGNORES the repo's AGENTS.md / project config (warns
        # "<dir> is not trusted; project configuration will be ignored") — losing convention context.
        # --trust trusts the cwd for this invocation only (the spawner sets cwd to the target worktree).
        return ["vibe", "--prompt", prompt, "--auto-approve", "--output", "text", "--trust"]
    if agent == "aider":
        return [str(AIDER_BIN), "--model", "mistral/codestral-latest", "--message", prompt,
                "--yes-always", "--no-stream"]
    if agent == "gemini":
        # agy (Antigravity): -p/--print runs headless; auth auto-loads from ~/.gemini; generous
        # --print-timeout so it doesn't self-terminate (default 5m). FIX 2026-06-15: agy's -p mode
        # WRITES ONLY to dirs in its workspace — without `--add-dir <cwd>` it falls back to an internal
        # scratch workspace, leaving the real cwd EMPTY while narrating success (the false-success bug
        # seen in scorecard-eval/invman1). Use the absolute cwd so Antigravity cannot resolve a
        # relative "." against a stale launch/project context and write to another checkout. The
        # spawner still sets cwd to the target worktree; the absolute add-dir is the hard guard.
        #
        # FIX 2026-06-17: keep HOME real for macOS keychain auth, but redirect Antigravity's mutable
        # Gemini/app-data root with the hidden `--gemini_dir` flag. Without this, new/unregistered
        # repos try to create ~/.gemini/config/projects and print mode fails with "no active
        # conversation" under Codex's restricted writable roots.
        #
        # FIX 2026-06-22: Antigravity 1.0.10 can exit 0 with no stdout and only log
        # "neither PlanModel nor RequestedModel specified" unless print mode receives an explicit
        # model. Default to Gemini Pro for large-context review work; ORCH_GEMINI_MODEL overrides.
        gemini_dir = os.environ.get(
            "ORCH_GEMINI_DIR",
            str(AGENT_RUNTIME / "gemini" / ".gemini"),
        )
        log_file = os.environ.get(
            "ORCH_GEMINI_LOG_FILE",
            str(AGENT_RUNTIME / "gemini" / "logs" / "agy.log"),
        )
        workspace = Path(cwd or ".").expanduser().resolve()
        # Tier-aware since 2026-08-08: cheap/mid ride 3.6 Flash (newer generation AND far fewer
        # compute units on this metered seat); only `full` pays for 3.1 Pro. Non-tier modes keep
        # the full Pro seat, because agy print mode REQUIRES an explicit model (see above).
        # The agy seat's runtime isolation (--gemini_dir + absolute --add-dir) IS this capability,
        # so it records use exactly where it is applied. Daily-coalesced: build_command runs on
        # every dispatch. Lazy import (capabilities imports feedback), never raises, and inert
        # outside an active tick. (2026-08-09)
        try:
            import capabilities
            capabilities.daily_heartbeat("agy-runtime-isolation", "invocation",
                                         ref="adapters.build_command:gemini")
        except Exception:
            pass
        cmd = ["agy", "--gemini_dir", gemini_dir]
        model = requested_model or _tier_model("gemini", mode) or gemini_model()
        if model:
            cmd += ["--model", model]
        return cmd + ["--print", prompt, "--dangerously-skip-permissions",
                      "--add-dir", str(workspace), "--print-timeout", "40m", "--log-file", log_file]
    raise ValueError(f"unknown agent: {agent}")


def record_ledger(agent: str, count: int = 1, cost_usd: float = 0.0, **extra) -> None:
    """Append a consumption row that capacity.py reads (count/dollar windows)."""
    HANDOFF.mkdir(parents=True, exist_ok=True)
    rec = {"ts": int(time.time()), "agent": agent, "count": count, "cost_usd": round(cost_usd, 6)}
    rec.update({k: v for k, v in extra.items() if v is not None})
    with LEDGER.open("a") as f:
        f.write(json.dumps(rec) + "\n")


def done_marker_cmd(run_id: str, log_file, rc_var: str) -> str:
    """Shell snippet for a completion marker, placed BEFORE the python ledger step in detached
    wrappers. The python completion (`ledger_reconcile.py complete`) takes seconds to start and has
    been observed SIGKILLed mid-write (522x in experiment logs, 2026-07-03 audit F2) — the agent's
    work survived but its exit/latency telemetry died, starving the Brain's cost plane. printf
    finishes in microseconds and survives; ledger_reconcile.reconcile() backfills a synthetic
    completion from the marker whenever the ndjson complete event is missing.
    Marker: <log dir>/done/<run_id>.json with {"run_id","rc","ts"}; `rc_var` names the shell
    variable the wrapper set from the agent command's $? immediately beforehand."""
    done_dir = Path(str(log_file)).parent / "done"
    marker = done_dir / f"{run_id}.json"
    return (
        f"mkdir -p {shlex.quote(str(done_dir))} && "
        f'printf \'{{"run_id":"%s","rc":%s,"ts":%s}}\\n\' '
        f'{shlex.quote(run_id)} "${{{rc_var}:-999}}" "$(date +%s)" '
        f"> {shlex.quote(str(marker))}"
    )


def dispatch(agent: str, prompt: str, mode: str | None = None, cwd: str = ".",
             env: dict | None = None, timeout: int = 2400, *, profile=None,
             transport: str | None = None, permission_mode: str | None = None,
             reasoning_effort: str | None = None, requested_model: str | None = None) -> dict:
    """Run an agent on a task; outcome is judged by git SIDE-EFFECTS, not stdout
    (per design: a commit that touches a file, not a self-claimed 'done').
    Records one consumption row; the caller reconciles real cost_usd afterward.
    """
    cmd = build_command(
        agent, prompt, mode, cwd=cwd, profile=profile, transport=transport,
        permission_mode=permission_mode, reasoning_effort=reasoning_effort,
        requested_model=requested_model,
    )
    proc = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)
    status = subprocess.run(["git", "-C", cwd, "status", "--porcelain"],
                            capture_output=True, text=True).stdout.strip()
    selected = execution_profiles.get_profile(profile) if profile is not None else None
    record_ledger(
        agent, count=1, cost_usd=0.0,
        selected_profile_id=selected.get("profile_id") if selected else None,
        requested_model=selected.get("requested_model") if selected else requested_model,
    )
    return {
        "agent": agent, "mode": mode, "exit": proc.returncode,
        "selected_profile_id": selected.get("profile_id") if selected else None,
        "changed_files": bool(status),
        "stdout_tail": (proc.stdout or "")[-2000:],
        "stderr_tail": (proc.stderr or "")[-1000:],
    }


def _selftest():
    old_codex_sandbox = os.environ.pop("CODEX_SANDBOX", None)
    old_codex_bypass = os.environ.pop("ORCH_CODEX_BYPASS_INNER_SANDBOX", None)
    try:
        _selftest_inner(gaps=[])
    finally:
        if old_codex_sandbox is not None:
            os.environ["CODEX_SANDBOX"] = old_codex_sandbox
        if old_codex_bypass is not None:
            os.environ["ORCH_CODEX_BYPASS_INNER_SANDBOX"] = old_codex_bypass


def _selftest_inner(*, gaps: list[str] | None = None):
    import env_prereq                       # imported here: env_prereq reads this module
    gaps = gaps if gaps is not None else []
    c = build_command("cursor", "do x")
    # Composer is PINNED, not implied: omitting --model selects `auto`, which routes across every
    # frontier model cursor sells. Owner policy is Composer only (2026-08-08).
    assert c[0] == "cursor-agent" and c[c.index("--model") + 1] == CURSOR_COMPOSER_MODEL, c
    assert "--trust" in c and c[c.index("--workspace") + 1] == ".", c  # headless workspace, no prompt
    cf = build_command("cursor", "do x", mode="frontier:gpt-5.5")
    assert "--model" in cf and "gpt-5.5" in cf, cf                    # explicit opt-in draws the pool
    cb = build_command("cursor", "do x", mode="frontier")
    assert cb[cb.index("--model") + 1] == CURSOR_COMPOSER_MODEL, cb   # bare 'frontier' => Composer, no blind spend
    for tier in MODEL_TIER_NAMES:                                    # tiers never escape the policy
        ct = build_command("cursor", "do x", mode=tier)
        assert ct[ct.index("--model") + 1] == CURSOR_COMPOSER_MODEL, (tier, ct)
    a = build_command("aider", "do x")
    assert a[0].endswith("/aider-venv/bin/aider"), a                 # isolated venv binary
    assert "mistral/codestral-latest" in a, a
    # A PRESENCE-ONLY PROBE MUST SAY WHY, PERMANENTLY OR NOT. `presence` means the probe proves a
    # credential EXISTS, not that the server accepts it, so every one is a known weakness. Without a
    # documented limit such a probe reads as an unfinished upgrade forever and gets re-investigated
    # every audit -- which is what happened to codex/claude between 2026-08-09 and 2026-08-22. A new
    # presence-only probe therefore cannot be added without stating its limit.
    for _agent, _spec in AUTH_PROBES.items():
        if _spec["strength"] == "presence":
            assert _spec.get("limit"), f"{_agent}: presence-only probe needs a documented limit"
            assert "permanent" in _spec["limit"] or "pending" in _spec["limit"], (
                f"{_agent}: a limit must say whether it is permanent or pending", _spec["limit"])
        else:
            # A validating probe must NOT carry a limit: that would be a contradiction in the record.
            assert not _spec.get("limit"), (_agent, _spec)
    assert AUTH_PROBES["cursor"]["strength"] == "validates", "cursor round-trips; do not downgrade"
    assert AUTH_PROBES["gemini"]["strength"] == "validates", "agy models round-trips; do not downgrade"

    v = build_command("vibe", "do x")
    assert v[0] == "vibe" and "--auto-approve" in v and "--prompt" in v, v   # subscription headless
    assert "--trust" in v, "vibe must --trust the cwd or it silently ignores AGENTS.md (2026-06-15 fix)"
    assert "exec" in build_command("codex", "x")
    ca = build_command("codex", "x", mode="assess")
    assert ca[:5] == ["codex", "exec", "--skip-git-repo-check", "--sandbox", "read-only"] and "--json" not in ca, ca
    # Three-tier GPT-5.6 family: Luna (cheap) / Terra (mid) / Sol (full). Codex has no catalog
    # probe, so these resolve straight from MODEL_TIERS without a subprocess.
    for tier, expected in (("cheap", "gpt-5.6-luna"), ("mid", "gpt-5.6-terra"), ("full", "gpt-5.6-sol")):
        cc = build_command("codex", "x", mode=tier)
        assert cc[cc.index("--model") + 1] == expected, (tier, cc)
        assert model_identity("codex", tier) == expected, tier
    # Non-tier modes still pass NO --model and keep the legacy lane tag.
    assert "--model" not in build_command("codex", "x", mode="assess"), "assess must not pin a model"
    assert model_identity("codex", None) == "codex:full:default"
    # An EXACT profile resolves the version-capable Codex binary and `profile_codex_binary()`
    # fails closed rather than falling back to PATH — deliberately, since a profile that cannot
    # pin its version is not an exact profile. So this SECTION needs that binary installed; the
    # default lives inside a macOS app bundle and cannot exist on a Linux runner. Everything else
    # in this selftest runs anywhere.
    if env_prereq.runnable(gaps, env_prereq.codex_profile_binary_absent()):
        profile_commands = {}
        for profile in execution_profiles.profiles_for_agent("codex"):
            cmd = build_command("codex", "x", mode="full", profile=profile, transport="local")
            assert cmd[0] == str(CODEX_PROFILE_BIN), cmd
            assert cmd[cmd.index("--model") + 1] == profile["requested_model"], cmd
            assert cmd[cmd.index("--sandbox") + 1] == "workspace-write", cmd
            assert cmd[cmd.index("-c") + 1] == f'model_reasoning_effort="{profile["reasoning_effort"]}"', cmd
            profile_commands[profile["profile_id"]] = cmd
            assess = build_command(
                "codex", "x", mode="assess", profile=profile, transport="offload",
                permission_mode="read-only",
            )
            assert assess[assess.index("--sandbox") + 1] == "read-only", assess
            assert "--json" not in assess and assess[assess.index("--model") + 1] == profile["requested_model"], assess
        assert {
            cmd[cmd.index("--model") + 1] for cmd in profile_commands.values()
        } == {"gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"}
    env_prereq.report_gaps("adapters.py", gaps)
    codex_cwd = HOME / ".codex" / "orchestrator" / "worktrees" / "selftest"
    ccwd = build_command("codex", "x", cwd=codex_cwd)
    assert "--cd" in ccwd and ccwd[ccwd.index("--cd") + 1] == str(codex_cwd), ccwd
    os.environ["CODEX_SANDBOX"] = "seatbelt"
    nested = build_command("codex", "x", cwd=codex_cwd)
    assert "--dangerously-bypass-approvals-and-sandbox" in nested and "--sandbox" not in nested, nested
    assert "--cd" in nested and nested[nested.index("--cd") + 1] == str(codex_cwd), nested
    os.environ["ORCH_CODEX_BYPASS_INNER_SANDBOX"] = "0"
    forced_sandbox = build_command("codex", "x", cwd=codex_cwd)
    assert "--sandbox" in forced_sandbox and "--dangerously-bypass-approvals-and-sandbox" not in forced_sandbox, forced_sandbox
    os.environ.pop("ORCH_CODEX_BYPASS_INNER_SANDBOX", None)
    os.environ.pop("CODEX_SANDBOX", None)
    # Claude 5 family: Haiku 4.5 (cheap) / Sonnet 5 (mid) / Opus 5 (full) — but the seat is CAPPED
    # at mid (scarce weekly), so the `full` lane really dispatches Sonnet 5. Expectations are
    # written post-ceiling because that is what reaches the CLI.
    assert MODEL_TIERS["claude"]["full"] == "claude-opus-5", "frontier option must stay defined"
    assert tier_ceiling("claude") == "mid" and effective_tier("claude", "full") == "mid"
    for tier, expected in (("cheap", "claude-haiku-4-5"), ("mid", "claude-sonnet-5"),
                           ("full", "claude-sonnet-5")):
        cl = build_command("claude", "x", mode=tier)
        assert cl[cl.index("--model") + 1] == expected, (tier, cl)
        assert model_identity("claude", tier) == expected, tier
    # Raising the ceiling reaches the frontier model — the option is capped, never deleted.
    os.environ["ORCH_CLAUDE_MAX_TIER"] = "full"
    try:
        uncapped = build_command("claude", "x", mode="full")
        assert uncapped[uncapped.index("--model") + 1] == "claude-opus-5", uncapped
    finally:
        os.environ.pop("ORCH_CLAUDE_MAX_TIER", None)
    assert model_identity("cursor", "frontier:gpt-5.5") == "cursor:gpt-5.5"
    assert model_identity("cursor", "frontier") == f"cursor:{CURSOR_COMPOSER_MODEL}"
    assert model_identity("cursor", None) == f"cursor:{CURSOR_COMPOSER_MODEL}"
    assert "--dangerously-skip-permissions" in build_command("claude", "x")
    gemini_cwd = HOME / ".codex" / "orchestrator" / "worktrees" / "gemini-selftest"
    # Probe OFF => offline/deterministic: the pinned default must be what reaches the CLI.
    old_probe = os.environ.get("ORCH_MODEL_PROBE")
    old_override = os.environ.pop("ORCH_GEMINI_MODEL", None)
    os.environ["ORCH_MODEL_PROBE"] = "0"
    try:
        g = build_command("gemini", "x", cwd=gemini_cwd)
        assert g[0] == "agy" and "--print" in g and "--print-timeout" in g and "--dangerously-skip-permissions" in g, g
        assert "--model" in g and g[g.index("--model") + 1] == DEFAULT_GEMINI_MODEL, g
        assert model_identity("gemini") == f"agy:{DEFAULT_GEMINI_MODEL}"
        assert agy_advertised_models() == [], "kill-switch must suppress the CLI probe entirely"
        unprobed = gemini_model_health()
        assert unprobed["resolvable"] and unprobed["source"] == "pinned_default", unprobed
        # THE ask: cheap/mid ride Flash, only full pays for Pro. Flash is both newer (3.6 vs 3.1)
        # and far cheaper in compute units on this metered seat.
        for tier, expected in (("cheap", "gemini-3.6-flash-low"), ("mid", "gemini-3.6-flash-high"),
                               ("full", "gemini-3.1-pro-high")):
            gt = build_command("gemini", "x", mode=tier, cwd=gemini_cwd)
            assert gt[gt.index("--model") + 1] == expected, (tier, gt)
            assert model_identity("gemini", tier) == f"agy:{expected}", tier
        # agy print mode REQUIRES a model, so a non-tier mode must still send the full pin.
        gnone = build_command("gemini", "x", mode="offload", cwd=gemini_cwd)
        assert gnone[gnone.index("--model") + 1] == DEFAULT_GEMINI_MODEL, gnone
        # Agents with no tier pins keep passing no --model (vibe/aider are single-model lanes).
        assert resolve_model("vibe", "cheap") is None and resolve_model("aider", "full") is None
        assert "--model" not in build_command("vibe", "x", mode="cheap")
    finally:
        if old_probe is None:
            os.environ.pop("ORCH_MODEL_PROBE", None)
        else:
            os.environ["ORCH_MODEL_PROBE"] = old_probe
        if old_override is not None:
            os.environ["ORCH_GEMINI_MODEL"] = old_override
    # `agy models` emits '<id>\t<label>' rows behind a prose banner; only bare ids survive.
    parsed = parse_agy_models(
        "Fetching available models...\n"
        "gemini-3.1-pro-high\tGemini 3.1 Pro (High)\n"
        "gemini-3.6-flash-low\tGemini 3.6 Flash (Low)\n"
    )
    assert parsed == ["gemini-3.1-pro-high", "gemini-3.6-flash-low"], parsed
    # Rename survival: pinned model gone => auto-pick the newest Pro/high seat, never die.
    old_memo = dict(_ADVERTISED_MEMO)
    try:
        _ADVERTISED_MEMO["gemini"] = {"ts": time.time(), "models": [
            "gemini-4.0-flash-high", "gemini-3.9-pro-low", "gemini-3.9-pro-high", "claude-sonnet-4-6",
        ]}
        renamed = gemini_model_health()
        assert renamed["model"] == "gemini-3.9-pro-high", renamed   # pro > flash, high > low
        assert renamed["resolvable"] and renamed["source"] == "auto_from_catalog", renamed
        # Auto-resolution stays inside the tier's own family: a renamed Flash tier must not
        # silently promote itself to the pricier Pro seat.
        flash = model_health("gemini", "cheap")
        assert flash["model"] == "gemini-4.0-flash-high", flash
        # A pinned model that IS advertised is used verbatim, with no auto-pick.
        _ADVERTISED_MEMO["gemini"]["models"] = [DEFAULT_GEMINI_MODEL, "gemini-3.6-flash-low"]
        assert gemini_model_health()["source"] == "pinned_default", gemini_model_health()
        assert gemini_model() == DEFAULT_GEMINI_MODEL
        # An operator override is honoured verbatim but reported UNRESOLVABLE when agy lacks it,
        # so capacity.py sheds the seat instead of letting every dispatch exit 1 (2026-08-08).
        os.environ["ORCH_GEMINI_MODEL"] = "gemini-2.5-pro"
        try:
            bad = gemini_model_health()
            assert bad["model"] == "gemini-2.5-pro" and not bad["resolvable"], bad
            assert "not advertised" in bad["reason"], bad
        finally:
            os.environ.pop("ORCH_GEMINI_MODEL", None)
            if old_override is not None:
                os.environ["ORCH_GEMINI_MODEL"] = old_override
    finally:
        _ADVERTISED_MEMO.clear()
        _ADVERTISED_MEMO.update(old_memo)
    assert "--gemini_dir" in g and "agent-runtime/gemini/.gemini" in g[g.index("--gemini_dir") + 1], g
    assert g[g.index("--add-dir") + 1] == str(gemini_cwd), g   # writes land in exact worktree, not stale/project cwd
    assert "--log-file" in g and "agent-runtime/gemini/logs/agy.log" in g[g.index("--log-file") + 1], g
    old_handoff, old_ledger = HANDOFF, LEDGER
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="adapters-ledger-selftest-"))
    try:
        globals()["HANDOFF"] = tmp
        globals()["LEDGER"] = tmp / "capacity-ledger.ndjson"
        record_ledger("codex", run_id="run-1", target="o/r#1", event="start")
        row = json.loads(LEDGER.read_text().strip())
        assert row["run_id"] == "run-1" and row["event"] == "start" and row["count"] == 1, row
    finally:
        globals()["HANDOFF"] = old_handoff
        globals()["LEDGER"] = old_ledger
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
    try:
        build_command("bogus", "x"); raise AssertionError("expected ValueError")
    except ValueError:
        pass
    print("adapters.py selftest: OK (cursor composer/explicit-frontier/safe-bare-frontier, "
          "vibe subscription, codex/claude cheap-model map, aider venv, gemini lane-ready)")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    else:
        print("usage: adapters.py --selftest   (dispatch() is invoked by router.py)")
