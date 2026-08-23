#!/usr/bin/env python3
"""capacity.py — local capacity reader for the multi-agent coding orchestrator.

Emits a per-agent remaining-capacity STATE (4-state enum: ok|warn|shed|unknown),
modeled from (design: PR #2350 §4 + Rev-2/2b corrections):
  - ccusage active 5h-block PROJECTED TOKENS for the subscription seats it tracks
    (Claude Team-Max; Codex ChatGPT-Pro). NB ccusage -j has no %% field, so these seats
    are OK (usable) by default with 429-shed as the authoritative limiter; an OPTIONAL
    per-seat block_token_limit (env) enables early warn/shed. [ccusage reports one shared
    active block — per-tool Codex/Claude split is a v1 TODO]
  - a local consumption ledger (estimated units / $ spend) for count/dollar/windowed agents
  - a 429-shed override flag (authoritative when present)

Corrections baked in:
  - cursor draws a METERED MONTHLY POOL (NOT free/unlimited — a costly modeling error, 2026-06-14:
    one day of A/B/C/D runs, ~9 cursor-agent calls incl. 72K-token cross-evals, EXHAUSTED it).
    Probe `cursor-agent models` -> 'No models available' == pool spent == shed. The user already pays
    for GPT-5.5 via codex, so cursor is DEPRIORITIZED in the route table, never the default lane.
  - gemini/Antigravity AI Pro == prepaid/windowed compute: local 5h and weekly
    soft budgets, with actual 429-shed as the authoritative limiter
  - aider/Codestral == TWO-TIER (plan_then_credit): a flat Mistral plan is the
    PRIMARY pool (use-it-or-lose-it), the pay-go API credit is the BACKSTOP (amounts in LOCAL_POLICY.md)
    used only after the plan is exhausted. NB: Mistral's docs say Le Chat Pro
    does NOT cover API calls (aider uses the API) — if the user's paid tier is
    the chat-only Pro, set plan_limit=0 and aider degrades to pure-credit.

Read-only and safe; `--selftest` runs fully offline. Simple-first + legible by
design (PR #2350 §11 anti-over-engineering): no scoring, no learning here — this
module only answers "does agent X have headroom right now?".
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import execution_profiles

HANDOFF = Path(os.environ.get("HANDOFF_DIR", Path.home() / ".codex" / "handoff"))
LEDGER = HANDOFF / "capacity-ledger.ndjson"  # one JSON object/line: {ts, agent, count, cost_usd}
OUT = HANDOFF / "capacity.json"
SHED_DIR = HANDOFF / "capacity-shed"  # touch <agent> here on an observed 429 -> forces shed

OK, WARN, SHED, UNKNOWN = "ok", "warn", "shed", "unknown"


# Static capacity model. Limit numbers are the public plan caps (PR #2350 Rev-2b);
# the unpublished Claude/Codex 5h ceilings are read live from ccusage instead.
def _env_int_or_none(name: str):
    v = os.environ.get(name)
    if not v:
        return None
    try:
        return int(v)
    except ValueError:
        return None


def _env_float_or_none(name: str):
    v = os.environ.get(name)
    if not v:
        return None
    try:
        return float(v)
    except ValueError:
        return None


# ── Economic-model re-verification (monthly `orchestrator-capacity-recheck`) ──────────────────
# 2026-08-01: all seats re-verified against current public pricing/limits; NO cap-VALUE changes
#   needed again this cycle (the soft caps / plan_limit below are local placeholders, not published
#   numbers, so there is nothing verified to swap in). Confirmed this cycle:
#   - codex  = ChatGPT Pro tier (LOCAL_POLICY.md), rolling 5h + weekly. Turbulent month, NET ZERO for this
#              model: 5h window was temporarily REMOVED 2026-07-12 (Plus/Pro/Business, after GPT-5.6
#              Sol demand doubled traffic in 48h), then RESTORED 2026-07-30 and again stacks with the
#              weekly cap — so window="5h" below stays correct. Current model is GPT-5.6 Sol (was
#              GPT-5.5); Plus ranges ~15-90 msg/5h Sol, 20-110 Terra, 50-280 Luna, Pro = 5×. A
#              2026-07-29 efficiency fix stretches typical Sol usage ~18%. NEW (2026-06-12): BANKED
#              rate-limit resets — Go/Plus/Pro/Business get 1 free reset, bankable, 30-day validity,
#              not purchasable. A banked reset can clear a real 5h exhaustion out-of-band, so an
#              observed shed is not always time-bound. [ccusage; 429-shed authoritative]
#   - claude = Max plan, 5h limits doubled 2026-05-06 (peak-hour throttle removed). The weekly +50%
#              was NOT reverted on 2026-07-13 as last cycle predicted: extended to 07-19, then to
#              2026-08-19 for Pro/Max/Team + seat-based Enterprise. ACTIVE NOW, expires 2026-08-19 —
#              i.e. the weekly ceiling may tighten BEFORE the next monthly recheck. Harmless: no cap
#              value is hard-wired here and 429-shed is authoritative. [ccusage]
#   - cursor = Pro / Pro+ / Ultra tiers; the mid tier is ~3x Pro's credits (LOCAL_POLICY.md).
#              First-party Composer 2.5 bills $0.50/M in, $2.50/M out — far under frontier rates,
#              which is why Auto+Composer stays the cheap usable lane. The 2026-06 two-pool split
#              (Composer/Auto vs Third-Party API) is unchanged.
#   - gemini = Antigravity AI Pro, compute-metered since 2026-05-17 (weighs request complexity, tools
#              in use, and accumulated chat-history length), 5h refresh + weekly ceiling. No cap change
#              since May's "tripled twice" (~9× the post-nerf floor). TIER RESTRUCTURE this cycle:
#              Pro $20 baseline, AI Ultra $100 (~5× Pro), Ultra Max $200 (~20× Pro, cut from $249.99);
#              rate limits for Flash/Pro models UNIFIED into one pool drawn down per API pricing; AI
#              credits REMOVED from base plans (quotas raised instead) and are now OVERAGE-ONLY, with
#              an Always/Never auto-spend setting. Google publishes NO numeric quotas → 429-shed stays
#              authoritative and the caps below stay local estimates. 2026-06-18: Gemini CLI and the
#              Code Assist IDE extensions STOPPED serving AI Pro/Ultra/free — the closed `agy`
#              Antigravity CLI is now the only lane (hence no `gemini` CLI on this box).
#   - vibe   = Mistral Le Chat Pro $14.99/mo, flat "all-day coding in the CLI, IDE, and on web",
#              fair-use with no published numbers. Pro still does NOT cover API calls (keeps aider on
#              the separate Codestral credit). Team $24.99/user (min $50); Education $5.99. Unchanged.
#   - aider  = Codestral API $0.30/$0.90 per 1M tok (in/out), 256K ctx; pay-go credit backstop (amount in LOCAL_POLICY.md)
#              unchanged. plan_limit below remains a placeholder — no published Mistral API tier cap.
#   CLI versions this cycle: agy 1.1.9 (was 1.0.13), cursor-agent 2026.07.23 (was 2026.06.29),
#   vibe 2.15.0, codex-cli 0.145.0 (was 0.142.4), claude 2.1.177, aider 0.86.2 (venv), ccusage 20.0.11.
#   agy 1.1.9 keeps full headless capability (-p/--print, --output-format json|stream-json,
#   --dangerously-skip-permissions) and ADDS --effort low|medium|high, --json-schema, --mode, and
#   --sandbox. NB --effort is a direct COMPUTE lever on a compute-metered seat (see the router/adapters
#   proposal in the 2026-08-01 recheck report). See ~/.claude/.../memory/orchestrator_local_build.md.
AGENTS = {
    # block_token_limit (optional): projected-tokens ceiling for the active 5h block. Unset =>
    # the seat is OK (usable) and 429-shed is the authoritative limiter; set it (env, from your
    # observed ccusage peaks) to get early warn/shed. ccusage has NO % in JSON, so this is the lever.
    "codex": {
        "account": "chatgpt-pro",
        "model": "ccusage",
        "window": "5h",
        "block_token_limit": _env_int_or_none("CODEX_BLOCK_TOKEN_LIMIT"),
    },
    "claude": {
        "account": "claude-team-max",
        "model": "ccusage",
        "window": "5h+weekly",
        "block_token_limit": _env_int_or_none("CLAUDE_BLOCK_TOKEN_LIMIT"),
    },
    "cursor": {
        "account": "cursor-pro-plus",
        "model": "metered",
        "window": "monthly",
    },  # METERED, NOT free
    # gemini/Antigravity: a REASONING seat, COMPUTE-METERED (not a fixed chat count — weighs request
    # complexity + accumulated chat-history length). Public behavior is a 5h refresh window plus a
    # broader weekly budget for AI Pro/Ultra. No usage API → these are LOCAL SOFT GUARDS using ledger
    # rows as estimated units; observed 429/rate-limit remains AUTHORITATIVE.
    "gemini": {
        "account": "antigravity-ai-pro",
        "model": "windowed_prepaid",
        "window_soft_cap": (
            _env_float_or_none("GEMINI_WINDOW_SOFT_UNITS")
            or _env_float_or_none("GEMINI_5H_SOFT_CAP")
            or 8.0
        ),
        "weekly_soft_cap": (
            _env_float_or_none("GEMINI_WEEKLY_SOFT_UNITS")
            or _env_float_or_none("GEMINI_WEEKLY_SOFT_CAP")
            or 280.0
        ),
        "reserve_fraction": _env_float_or_none("GEMINI_RESERVE_FRACTION") or 0.25,
        "drain_minutes": _env_int_or_none("GEMINI_DRAIN_MINUTES") or 90,
        "window": "5h+weekly",
    },
    # vibe (Mistral) — PRIMARY Mistral lane: Le Chat Pro/Team sub = subscription login +
    # "all-day coding" flat allowance + PAYG overflow. No usage API, so 429-shed is
    # authoritative (modeled like cursor's flat, but a SEPARATE account).
    "vibe": {"account": "mistral-vibe-sub", "model": "flat", "window": "subscription"},
    # aider — BACKUP-ONLY as of 2026-06-21 (owner directive): router.BACKUP_AGENTS holds it OUT of
    # routine auto-selection; reachable only on explicit demand (`--agent aider` / `only={"aider"}`).
    # Capacity is still tracked so backup use respects the credit. Optional API fallback (Codestral API
    # burns the pay-go credit; the Vibe sub does NOT cover API calls). Two-tier: flat plan (count/day) then $25.
    # plan_limit PLACEHOLDER pending the real Mistral API tier cap; 0 => pure-credit.
    "aider": {
        "account": "mistral-codestral",
        "model": "plan_then_credit",
        "plan_limit": 2000,
        "plan_window": "daily",
        "credit_usd": 25.0,
        "credit_window": "monthly",
        "window": "daily",
    },
}
WARN_FRAC = 0.8
_WINDOW_SECONDS = {"daily": 86400, "monthly": 30 * 86400, "5h": 5 * 3600, "weekly": 7 * 86400}


def agent_tiers() -> dict[str, str]:
    """Authoritative research tiers, derived from real agent/pool policy."""
    out = {
        "cursor": "metered",
        "codex": "metered",
        "claude": "metered",
        "gemini": "metered",
        "vibe": "flat",
        "aider": "paygo",
    }
    for pool in execution_profiles.CAPACITY_POOLS.values():
        if pool.get("agent"):
            out[str(pool["agent"])] = str(pool["tier"])
    return out


def profile_pool_ids(registry: dict | None = None) -> dict[str, list[str]]:
    registry = registry or execution_profiles.PROFILE_REGISTRY
    return {
        profile_id: list(profile.get("capacity_pool_ids") or [])
        for profile_id, profile in registry.items()
    }


def debit_profile_pools(events: list[dict], registry: dict | None = None) -> dict[str, float]:
    """Debit real pools once per profile event; never create per-model balances."""
    mappings = profile_pool_ids(registry)
    usage: dict[str, float] = {}
    for event in events:
        if event.get("event") == "complete":
            continue
        try:
            units = float(event.get("units", event.get("count", 1)) or 0)
        except (TypeError, ValueError):
            units = 0.0
        for pool_id in mappings.get(str(event.get("selected_profile_id") or ""), []):
            usage[pool_id] = usage.get(pool_id, 0.0) + units
    return usage


def profile_pool_usage_from_ledger(
    path: Path | None = None, *, now: float | None = None, registry: dict | None = None
) -> dict[str, float]:
    """Read bounded-window profile starts from the capacity ledger."""
    path = path or LEDGER
    if not path.exists():
        return {}
    now = float(now or time.time())
    rows = []
    mappings = profile_pool_ids(registry)
    for line in path.read_text().splitlines():
        try:
            row = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        profile_id = str(row.get("selected_profile_id") or "")
        if profile_id not in mappings or row.get("event") == "complete":
            continue
        ts = row.get("ts")
        if not isinstance(ts, (int, float)) or ts > now:
            continue
        # Codex's shared subscription has a rolling 5h plus weekly boundary.
        # Retain the broader active window for shared-pool burn reporting.
        horizons = [
            _WINDOW_SECONDS["weekly"]
            for pool_id in mappings[profile_id]
            if "weekly" in str((execution_profiles.CAPACITY_POOLS.get(pool_id) or {}).get("window"))
        ] or [_WINDOW_SECONDS["5h"]]
        if now - ts <= max(horizons):
            rows.append(row)
    return debit_profile_pools(rows, registry)


def profile_capacity_snapshot(
    agent_snapshot: dict,
    *,
    pool_usage: dict[str, float] | None = None,
    pool_limits: dict[str, float] | None = None,
    registry: dict | None = None,
) -> dict:
    """Project shared pool state onto profiles without multiplying balances."""
    registry = registry or execution_profiles.PROFILE_REGISTRY
    usage = pool_usage or {}
    limits = pool_limits or {}
    pools = {}
    for pool_id, definition in execution_profiles.CAPACITY_POOLS.items():
        used = float(usage.get(pool_id, 0.0))
        limit = limits.get(pool_id)
        agent_state = ((agent_snapshot.get("agents") or {}).get(definition.get("agent")) or {}).get(
            "state", UNKNOWN
        )
        exhausted = limit is not None and used >= float(limit)
        pools[pool_id] = {
            **definition,
            "used": used,
            "limit": limit,
            "state": SHED if exhausted else agent_state,
        }
    profiles = {}
    for profile_id, profile in registry.items():
        mapped = list(profile.get("capacity_pool_ids") or [])
        states = [(pools.get(pool_id) or {}).get("state", UNKNOWN) for pool_id in mapped]
        state = (
            SHED
            if SHED in states
            else UNKNOWN if UNKNOWN in states else WARN if WARN in states else OK
        )
        profiles[profile_id] = {"state": state, "capacity_pool_ids": mapped}
    return {"pools": pools, "profiles": profiles}


def _ccusage_active_block(timeout_s: int = 30):
    """Return the active 5h-block dict from ccusage (or None).

    IMPORTANT: ccusage's `-j` output exposes NO percentage field (verified 2026-06-14;
    even `--token-limit max` only adds it to the table display, not JSON). So we return
    the raw block — {projection:{totalTokens,totalCost,remainingMinutes}, totalTokens,
    costUSD, burnRate, ...} — and compute() reasons over projected tokens vs an OPTIONAL
    configured ceiling. ccusage is installed globally (fast); npx is the fallback.
    """
    exe = shutil.which("ccusage")
    cmd = (
        [exe, "blocks", "--active", "-j"]
        if exe
        else ["npx", "-y", "ccusage@latest", "blocks", "--active", "-j"]
    )
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s).stdout
        data = json.loads(out)
    except Exception:
        return None
    for b in data.get("blocks") or data.get("data") or []:
        if b.get("isActive"):
            return b
    return None


def _ledger_usage(agent: str, window: str):
    """(cost_usd, count) for agent within the current window from the ledger."""
    if not LEDGER.exists():
        return 0.0, 0
    horizon = _WINDOW_SECONDS.get(window, 86400)
    now = time.time()
    cost, cnt = 0.0, 0
    for line in LEDGER.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get("agent") != agent:
            continue
        ts = r.get("ts", 0)
        if isinstance(ts, (int, float)) and ts <= now and (now - ts) <= horizon:
            cost += float(r.get("cost_usd", 0) or 0)
            cnt += int(r.get("count", 1) or 0)
    return cost, cnt


def _estimate_gemini_units(row: dict) -> float:
    """Estimate compute units for a Gemini ledger row.

    Delegation runs represent a full workspace write / multi-turn loop:
      - implement / testgen: 4.0 units (high complexity)
      - review / polish / mechanical: 2.0 units (medium complexity)
    Offload runs (no run_id or default): 1.0 unit (single turn/lightweight)
    """
    if row.get("event") == "complete":
        return 0.0  # Only count at start to prevent double counting

    task_type = row.get("task_type")
    if task_type in ("implement", "testgen"):
        base = 4.0
    elif task_type in ("review", "polish", "mechanical"):
        base = 2.0
    else:
        base = 1.0
    try:
        count = max(1, int(row.get("count", 1) or 1))
    except (TypeError, ValueError):
        count = 1
    return base * count


def _shed(agent: str) -> bool:
    return (SHED_DIR / agent).exists()


# NB: `cursor-agent models` lists MODEL AVAILABILITY, not remaining QUOTA — it is NOT a reliable
# exhaustion probe (returns models even when the monthly pool is spent). Like the other no-usage-API
# seats, cursor relies on the 429-shed flag (authoritative) + route-table DEPRIORITIZATION. To pause
# cursor when the pool is known-spent, touch SHED_DIR/cursor (and remove it when the cycle resets).


def _model_health(agent: str, tier: str = "full"):
    """Model-resolvability preflight for one seat/tier, or None when it can't be evaluated.

    Headroom is meaningless if every dispatch dies on model selection: on 2026-08-08 a rotted
    `--model gemini-2.5-pro` pin made EVERY gemini offload exit 1 while this file still reported
    `state: ok` with plenty of units left. Lazy import keeps capacity independent of adapters, and
    any failure returns None (unknown) so an offline box never sheds a working seat.
    """
    try:
        import adapters

        return adapters.model_health(agent, tier)
    except Exception:
        return None


def _unresolvable_tier(agent: str):
    """First (tier, health) whose pinned model the agent's own CLI does not offer, else None.

    Any tier being undispatchable is a seat-level fault: the router picks the mode, so a broken
    `cheap` pin fails just as hard as a broken `full` one. Agents with no catalog probe can never
    reach this — their health always reports resolvable.
    """
    try:
        import adapters

        tiers = adapters.MODEL_TIER_NAMES
    except Exception:
        return None
    for tier in tiers:
        health = _model_health(agent, tier)
        if health and not health.get("resolvable", True):
            return tier, health
    return None


def _gemini_model_health():
    """Back-compat shim for the gemini full tier (kept for existing callers/selftests)."""
    return _model_health("gemini", "full")


def _auth_health(agent: str):
    """Non-billing credential preflight, or None when it can't be evaluated."""
    try:
        import adapters

        return adapters.auth_health(agent)
    except Exception:
        return None


def _tier_models(agent: str) -> dict:
    """{tier: model-or-None} a dispatch would ACTUALLY send, ceiling applied.

    Reports post-ceiling so the snapshot never advertises a model routine routing won't spend:
    a capped seat shows its ceiling model for every tier at or above the cap.
    """
    try:
        import adapters

        return {
            t: adapters.resolve_model(agent, adapters.effective_tier(agent, t))
            for t in adapters.MODEL_TIER_NAMES
        }
    except Exception:
        return {}


def _tier_ceiling(agent: str):
    try:
        import adapters

        return adapters.tier_ceiling(agent)
    except Exception:
        return None


def compute(agent: str, cfg: dict, ccusage_block):
    """Return (state, reason[, meta]) for one agent. 429-shed is authoritative."""
    if _shed(agent):
        return SHED, "observed 429 / rate-limit shed flag set"
    # Dispatchability gate BEFORE any budget math: a seat whose configured model its own CLI does
    # not offer is unusable at ANY headroom. SHED (not WARN) because router._pick drops
    # shed/unknown outright — reporting `ok` here is what hid the 2026-08-08 gemini breakage.
    # Credential gate, same reasoning as the model gate below: a seat that cannot authenticate
    # burns nothing and completes nothing, so headroom is meaningless. Only an EXPLICIT auth
    # failure sheds; an unrunnable probe leaves the seat alone.
    auth = _auth_health(agent)
    if auth and auth.get("checked") and not auth.get("authenticated", True):
        return (
            SHED,
            f"not authenticated: {auth.get('reason')}",
            {
                "policy": "auth-failed",
                "availability": "unavailable_auth_failed",
                "next_action": (
                    f"Refresh {agent} credentials, then re-check with "
                    f"`python3 capacity.py --json`. The fleet reads the credential FILE, "
                    f"not an interactive login session."
                ),
            },
        )
    unresolvable = _unresolvable_tier(agent)
    if unresolvable:
        tier, health = unresolvable
        return (
            SHED,
            f"model not dispatchable ({tier} tier): {health.get('reason')}",
            {
                "policy": "model-unresolvable",
                "availability": "unavailable_model_unresolved",
                "next_action": (
                    f"Point ORCH_{agent.upper()}_MODEL_{tier.upper()} at a model the "
                    f"{agent} CLI lists (or fix adapters.MODEL_TIERS[{agent!r}]"
                    f"[{tier!r}]); headroom is unusable until then."
                ),
                "unresolvable_tier": tier,
                "configured_model": health.get("model"),
                "advertised_models": health.get("advertised") or [],
            },
        )
    model = cfg["model"]
    if model == "metered":
        # cursor draws a metered monthly pool, but the Auto bucket has headroom even when the API
        # sub-bucket is spent (verified: headless `--model auto` runs with API at 100%). So it's a
        # normal usable cheap lane; no reliable quota API, so 429-shed stays authoritative.
        return (
            OK,
            "METERED monthly pool, usable via Auto bucket (use --model auto); 429-shed authoritative",
        )
    if model == "flat":
        return OK, "subscription all-day (flat); 429-shed authoritative; PAYG overflow"
    if model == "ccusage":
        # ccusage -j has no %, so reason over projected tokens vs an OPTIONAL ceiling.
        # No data / no ceiling => OK (usable) with 429-shed as the authoritative limiter —
        # NOT 'unknown', which would wrongly hide the premium seats from the router.
        if ccusage_block is None:
            return OK, "ccusage unavailable; 429-shed authoritative"
        proj = ccusage_block.get("projection") or {}
        proj_tokens = proj.get("totalTokens") or ccusage_block.get("totalTokens") or 0
        proj_cost = proj.get("totalCost")
        cost_tag = f", ~${proj_cost:.0f}" if isinstance(proj_cost, (int, float)) else ""
        limit = cfg.get("block_token_limit")
        if limit:
            frac = proj_tokens / limit
            tag = f"5h proj {proj_tokens / 1e6:.0f}M/{limit / 1e6:.0f}M tok{cost_tag}"
            if frac >= 1.0:
                return SHED, tag
            if frac >= WARN_FRAC:
                return WARN, tag
            return OK, tag
        return (
            OK,
            f"5h proj {proj_tokens / 1e6:.0f}M tok{cost_tag} (no cap set; 429-shed authoritative)",
        )
    if model == "count":
        _, cnt = _ledger_usage(agent, cfg["window"])
        lim = cfg["limit"]
        if cnt >= lim:
            return SHED, f"{cnt}/{lim} requests this {cfg['window']}"
        if cnt >= WARN_FRAC * lim:
            return WARN, f"{cnt}/{lim} requests this {cfg['window']}"
        return OK, f"{cnt}/{lim} requests this {cfg['window']}"
    if model == "dollar":
        cost, _ = _ledger_usage(agent, cfg["window"])
        lim = cfg["limit_usd"]
        if cost >= lim:
            return SHED, f"${cost:.2f}/${lim:.0f} this {cfg['window']}"
        if cost >= WARN_FRAC * lim:
            return WARN, f"${cost:.2f}/${lim:.0f} this {cfg['window']}"
        return OK, f"${cost:.2f}/${lim:.0f} this {cfg['window']}"
    if model == "plan_then_credit":
        # Primary flat plan (count window), then fall to the $ API-credit backstop.
        # Use-it-or-lose-it: stays OK while the plan has headroom; only the credit
        # tier (genuine pay-go) is reported as WARN so the router prefers it last.
        _, cnt = _ledger_usage(agent, cfg["plan_window"])
        plim = cfg["plan_limit"]
        cost, _ = _ledger_usage(agent, cfg["credit_window"])
        clim = cfg["credit_usd"]
        if plim > 0 and cnt < WARN_FRAC * plim:
            return OK, f"plan {cnt}/{plim} req this {cfg['plan_window']}"
        if plim > 0 and cnt < plim:
            return WARN, f"plan {cnt}/{plim} req this {cfg['plan_window']} (near plan cap)"
        # plan exhausted (or plim==0 => chat-only Pro) -> API-credit backstop
        if cost >= clim:
            return SHED, f"plan + ${clim:.0f} API credit both exhausted"
        return WARN, f"plan spent; on API credit ${cost:.2f}/${clim:.0f}"
    if model == "windowed_prepaid":
        # Resolvability was already gated above for every agent; here we only REPORT what a
        # dispatch would actually send, per tier, so model drift is visible in the snapshot.
        configured_model = (_model_health(agent, "full") or {}).get("model")
        now = time.time()
        window_seconds = _WINDOW_SECONDS["5h"]
        weekly_seconds = _WINDOW_SECONDS["weekly"]
        used_5h = 0.0
        used_weekly = 0.0
        used_this_block = 0.0

        block_start = now - (now % window_seconds)

        if LEDGER.exists():
            for line in LEDGER.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if r.get("agent") != agent:
                    continue
                ts = r.get("ts", 0)
                if not isinstance(ts, (int, float)):
                    continue
                if ts > now:
                    continue
                if r.get("event") == "complete":
                    continue

                units = _estimate_gemini_units(r)

                if (now - ts) <= window_seconds:
                    used_5h += units
                if (now - ts) <= weekly_seconds:
                    used_weekly += units
                if ts >= block_start:
                    used_this_block += units

        w_cap = cfg["window_soft_cap"]
        wk_cap = cfg["weekly_soft_cap"]
        reserve_fraction = max(0.0, min(0.9, float(cfg.get("reserve_fraction") or 0.0)))
        reserve_units = w_cap * reserve_fraction
        drain_seconds = max(0, int(cfg.get("drain_minutes") or 0) * 60)
        drain_start = max(0, window_seconds - drain_seconds)
        elapsed = int(now - block_start)
        minutes_to_refresh = int(round((window_seconds - elapsed) / 60))
        availability = "usable"
        next_action = (
            "Use Gemini for substantial, self-contained reasoning work when it is the best fit."
        )

        if elapsed >= drain_start and used_this_block < w_cap:
            policy = "drain"
        elif reserve_units and used_this_block >= max(0.0, w_cap - reserve_units):
            policy = "reserve"
        else:
            policy = "steady"

        if used_weekly >= wk_cap:
            state = WARN
            policy = "weekly-soft-cap"
            availability = "usable_soft_constrained"
            reason = (
                f"usable but weekly soft-budget constrained: weekly soft budget reached "
                f"{used_weekly:.1f}/{wk_cap:.0f} estimated units; no 429/shed flag present"
            )
            next_action = (
                "Prefer ok seats; use Gemini only for substantial good-fit work. "
                "Do not treat it as broken unless a 429/shed/auth failure appears."
            )
        elif used_5h >= w_cap:
            state = WARN
            policy = "window-soft-cap"
            availability = "usable_soft_constrained"
            reason = (
                f"usable but 5h soft-budget constrained: 5h soft budget reached "
                f"{used_5h:.1f}/{w_cap:.0f} estimated units; "
                f"~{minutes_to_refresh} min to window refresh; no 429/shed flag present"
            )
            next_action = (
                "Prefer ok seats until the 5h window refreshes; use Gemini only for substantial "
                "good-fit work. Do not treat it as broken unless a 429/shed/auth failure appears."
            )
        elif used_weekly >= WARN_FRAC * wk_cap:
            state = WARN
            availability = "usable_soft_constrained"
            reason = f"usable but near weekly soft budget: {used_weekly:.1f}/{wk_cap:.0f} estimated units"
            next_action = (
                "Prefer ok seats for marginal work; keep Gemini for substantial good-fit tasks."
            )
        elif used_5h >= WARN_FRAC * w_cap:
            state = WARN
            availability = "usable_soft_constrained"
            reason = (
                f"usable but near 5h soft budget: {used_5h:.1f}/{w_cap:.0f} estimated units; "
                f"~{minutes_to_refresh} min to window refresh"
            )
            next_action = (
                "Prefer ok seats for marginal work; keep Gemini for substantial good-fit tasks."
            )
        elif policy == "reserve":
            state = WARN
            availability = "usable_reserve"
            reason = (
                f"reserve mode: {used_5h:.1f}/{w_cap:.0f} estimated units (5h), "
                f"{used_weekly:.1f}/{wk_cap:.0f} weekly; hold ~{reserve_units:.1f} unit(s) for later"
            )
            next_action = (
                "Prefer normal ok seats; keep Gemini available for substantial fallback work."
            )
        else:
            state = OK
            suffix = (
                "drain mode: spend suitable AGY work before 5h refresh"
                if policy == "drain"
                else "steady windowed compute"
            )
            if policy == "drain":
                availability = "usable_drain"
                next_action = "Spend suitable Gemini work before the 5h window refresh wastes remaining headroom."
            reason = (
                f"{used_5h:.1f}/{w_cap:.0f} estimated units (5h), "
                f"{used_weekly:.1f}/{wk_cap:.0f} weekly; {suffix}"
            )

        return (
            state,
            reason,
            {
                "policy": policy,
                "availability": availability,
                "next_action": next_action,
                "configured_model": configured_model,  # what a dispatch would actually send to agy
                "used_5h": used_5h,
                "used_weekly": used_weekly,
                "used_this_block": used_this_block,
                "estimated_units_5h": used_5h,
                "estimated_units_weekly": used_weekly,
                "window_soft_cap": w_cap,
                "weekly_soft_cap": wk_cap,
                "soft_units_5h": w_cap,
                "soft_units_weekly": wk_cap,
                "elapsed_in_window": elapsed,
                "minutes_to_window_refresh": minutes_to_refresh,
            },
        )
    return UNKNOWN, "no capacity model for agent"


def _capability_heartbeat(event_type: str, **kw) -> None:
    """Record that the windowed-capacity policy ran. Lazy import + never raises: capacity is the
    first thing the tick computes, and a capability-ledger problem must not be able to stop it."""
    try:
        import capabilities

        capabilities.production_heartbeat("windowed-capacity-policy", event_type, **kw)
    except Exception:
        pass


def build(ccusage_block=None) -> dict:
    # Infrastructure capability: exercised once per tick as a PHASE, never selected for a task, so
    # it records use here rather than through a routing matcher. Inert outside an active tick.
    _capability_heartbeat("invocation", ref="capacity.build")
    if ccusage_block is None:
        ccusage_block = _ccusage_active_block()
    agents = {}
    for agent, cfg in AGENTS.items():
        res = compute(agent, cfg, ccusage_block)
        meta = {}
        if len(res) == 3:
            state, reason, meta = res
        else:
            state, reason = res
        agents[agent] = {
            "state": state,
            "reason": reason,
            "account": cfg["account"],
            "window": cfg["window"],
            # What a dispatch would actually send, per tier — so a rotted pin is visible in the
            # snapshot itself rather than only at dispatch time. None => CLI default applies.
            "tier_models": _tier_models(agent),
            "tier_ceiling": _tier_ceiling(agent),  # None => uncapped; else routine spend stops here
            "auth": (_auth_health(agent) or {}),  # {} => not evaluable; never read as a failure
            **(meta or {}),
        }
    proj = (ccusage_block or {}).get("projection") or {}
    base = {
        "generated_at": int(time.time()),
        "ccusage_active": (
            {"proj_tokens": proj.get("totalTokens"), "proj_cost": proj.get("totalCost")}
            if ccusage_block
            else None
        ),
        "agents": agents,
    }
    base.update(
        profile_capacity_snapshot(
            base,
            pool_usage=profile_pool_usage_from_ledger(),
        )
    )
    return base


def _selftest():
    def blk(toks):
        return {"isActive": True, "projection": {"totalTokens": toks, "totalCost": 100.0}}

    # ccusage seat with NO ceiling => OK regardless of token volume (429-shed is the limiter)
    assert compute("codex", AGENTS["codex"], blk(50_000_000))[0] == OK
    # ccusage unavailable => OK (NOT unknown) — the fix that keeps the premium seats usable
    assert compute("codex", AGENTS["codex"], None)[0] == OK
    # WITH a configured ceiling, projected tokens drive ok/warn/shed
    capped = dict(AGENTS["codex"], block_token_limit=100_000_000)
    assert compute("codex", capped, blk(50_000_000))[0] == OK
    assert compute("codex", capped, blk(85_000_000))[0] == WARN
    assert compute("codex", capped, blk(120_000_000))[0] == SHED
    # cursor is METERED now: OK unless 429-shed (no reliable quota API); route table deprioritizes it
    cur_state, cur_reason = compute("cursor", AGENTS["cursor"], None)
    assert cur_state == OK and "METERED" in cur_reason, (
        cur_state,
        cur_reason,
    )  # usable metered lane (Auto bucket)
    assert compute("vibe", AGENTS["vibe"], None)[0] == OK  # vibe flat sub is ok unless shed
    # Unit estimation tests
    assert _estimate_gemini_units({"task_type": "implement"}) == 4.0
    assert _estimate_gemini_units({"task_type": "testgen"}) == 4.0
    assert _estimate_gemini_units({"task_type": "review"}) == 2.0
    assert _estimate_gemini_units({"task_type": "polish"}) == 2.0
    assert _estimate_gemini_units({"task_type": "mechanical"}) == 2.0
    assert _estimate_gemini_units({"event": "complete"}) == 0.0
    assert _estimate_gemini_units({}) == 1.0

    # Gemini prepaid compute tests using temporary ledger and pinned time.
    import tempfile

    global LEDGER
    old_ledger = LEDGER
    tmp = Path(tempfile.mkdtemp(prefix="capacity-gemini-selftest-"))
    LEDGER = tmp / "capacity-ledger.ndjson"
    original_time = time.time
    original_health = _model_health
    original_auth = _auth_health
    try:
        # Auth gate: an EXPLICIT credential failure sheds the seat regardless of headroom.
        globals()["_auth_health"] = lambda agent: {
            "agent": agent,
            "authenticated": False,
            "checked": True,
            "reason": "Error: Authentication required. Please run 'agent login'",
        }
        st, rs, mt = compute("cursor", AGENTS["cursor"], None)
        assert st == SHED and mt["availability"] == "unavailable_auth_failed", (st, rs, mt)
        assert "not authenticated" in rs, rs
        # An UNRUNNABLE probe must not shed: checked=False is unknown, not failure.
        globals()["_auth_health"] = lambda agent: {
            "agent": agent,
            "authenticated": True,
            "checked": False,
            "reason": "no probe",
        }
        assert compute("cursor", AGENTS["cursor"], None)[0] == OK, "unknown auth must not shed"
        globals()["_auth_health"] = lambda agent: None
        assert compute("cursor", AGENTS["cursor"], None)[0] == OK, "None auth must not shed"
        # Pin the model preflight so the budget cases below stay offline+deterministic.
        globals()["_model_health"] = lambda agent, tier="full": {
            "agent": agent,
            "tier": tier,
            "model": "gemini-3.1-pro-high",
            "resolvable": True,
            "advertised": ["gemini-3.1-pro-high"],
            "reason": "selftest",
            "source": "pinned_default",
        }
        gem_cfg = dict(
            AGENTS["gemini"],
            window_soft_cap=8.0,
            weekly_soft_cap=20.0,
            reserve_fraction=0.25,
            drain_minutes=90,
        )
        fixed_now = 1_800_000.0
        block_start = fixed_now - (fixed_now % _WINDOW_SECONDS["5h"])
        early_now = block_start + 1200
        time.time = lambda: early_now

        # Empty ledger -> OK, steady.
        state, reason, meta = compute("gemini", gem_cfg, None)
        assert state == OK and meta["policy"] == "steady", (state, meta)

        # Future timestamps are ignored instead of poisoning the current window.
        LEDGER.write_text(
            json.dumps(
                {"ts": early_now + 3600, "agent": "gemini", "count": 1, "task_type": "implement"}
            )
            + "\n"
        )
        state, reason, meta = compute("gemini", gem_cfg, None)
        assert state == OK and meta["used_5h"] == 0.0, (state, reason, meta)

        # Early window at reserve threshold -> WARN/reserve.
        recs = [
            {"ts": block_start + 100, "agent": "gemini", "count": 1, "task_type": "implement"},
            {"ts": block_start + 200, "agent": "gemini", "count": 1, "task_type": "review"},
        ]
        LEDGER.write_text("\n".join(json.dumps(r) for r in recs) + "\n")
        state, reason, meta = compute("gemini", gem_cfg, None)
        assert state == WARN and meta["policy"] == "reserve", (state, reason, meta)
        assert meta["used_this_block"] == 6.0

        # Late window with remaining budget -> OK/drain, so the router can spend suitable work.
        time.time = lambda: block_start + _WINDOW_SECONDS["5h"] - 60
        state, reason, meta = compute("gemini", gem_cfg, None)
        assert state == OK and meta["policy"] == "drain", (state, reason, meta)

        # 5h soft cap reached -> WARN/window-soft-cap, not SHED; 429 flag is the hard limiter.
        recs = [
            {"ts": block_start + 1000 * i, "agent": "gemini", "count": 1, "task_type": "implement"}
            for i in range(2)
        ]
        LEDGER.write_text("\n".join(json.dumps(r) for r in recs) + "\n")
        state, reason, meta = compute("gemini", gem_cfg, None)
        assert state == WARN and meta["policy"] == "window-soft-cap", (state, reason, meta)

        # Weekly soft cap reached outside the current 5h window -> WARN/weekly-soft-cap.
        old_ts = early_now - (_WINDOW_SECONDS["5h"] + 3600)
        recs = [
            {"ts": old_ts - 60 * i, "agent": "gemini", "count": 1, "task_type": "implement"}
            for i in range(5)
        ]
        LEDGER.write_text("\n".join(json.dumps(r) for r in recs) + "\n")
        time.time = lambda: early_now
        state, reason, meta = compute("gemini", gem_cfg, None)
        assert state == WARN and meta["policy"] == "weekly-soft-cap", (state, reason, meta)

        # The healthy path names the model a dispatch would actually send (drift is visible).
        LEDGER.write_text("")
        state, reason, meta = compute("gemini", gem_cfg, None)
        assert state == OK and meta["configured_model"] == "gemini-3.1-pro-high", meta

        # PREFLIGHT (2026-08-08): an unresolvable model sheds the seat even on an EMPTY ledger,
        # where the budget math alone would have said `ok`. This is the regression that let a
        # rotted gemini-2.5-pro pin report full headroom while every dispatch exited 1.
        def _broken(agent, tier="full"):
            if tier != "cheap":
                return {
                    "agent": agent,
                    "tier": tier,
                    "model": "gemini-3.1-pro-high",
                    "resolvable": True,
                    "advertised": ["gemini-3.1-pro-high"],
                    "reason": "ok",
                    "source": "pinned_default",
                }
            return {
                "agent": agent,
                "tier": tier,
                "model": "gemini-2.5-pro",
                "resolvable": False,
                "advertised": ["gemini-3.1-pro-high"],
                "source": "pinned_default",
                "reason": "gemini-2.5-pro is not advertised by gemini",
            }

        globals()["_model_health"] = _broken
        state, reason, meta = compute("gemini", gem_cfg, None)
        assert state == SHED, (state, reason, meta)  # not OK, and not merely WARN
        assert meta["availability"] == "unavailable_model_unresolved", meta
        assert meta["unresolvable_tier"] == "cheap", meta  # ANY broken tier sheds the seat
        assert meta["configured_model"] == "gemini-2.5-pro" and "not advertised" in reason, (
            reason,
            meta,
        )
        # The gate is seat-level, not gemini-special: it applies to every agent uniformly.
        state_c, reason_c, meta_c = compute("codex", AGENTS["codex"], None)
        assert state_c == SHED and meta_c["unresolvable_tier"] == "cheap", (state_c, meta_c)
        # An unreadable model list must NOT shed a working seat (offline box / CLI absent).
        globals()["_model_health"] = lambda agent, tier="full": None
        assert (
            compute("gemini", gem_cfg, None)[0] == OK
        ), "unknown model list must not shed the seat"
        assert (
            compute("codex", AGENTS["codex"], None)[0] == OK
        ), "unknown must not shed codex either"
    finally:
        globals()["_model_health"] = original_health
        globals()["_auth_health"] = original_auth
        time.time = original_time
        LEDGER = old_ledger
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)
    # aider two-tier: empty ledger => plan headroom => OK
    assert compute("aider", AGENTS["aider"], None)[0] == OK
    # chat-only Pro (plan_limit=0) with credit left => WARN (pay-go backstop only)
    chat_only = dict(AGENTS["aider"], plan_limit=0)
    assert compute("aider", chat_only, None)[0] == WARN, compute("aider", chat_only, None)
    # plan exhausted + credit exhausted => SHED (simulate via plan_limit=0 + a spent ledger
    # is integration-tested; here assert the all-spent branch with a zero credit budget)
    broke = dict(AGENTS["aider"], plan_limit=0, credit_usd=0.0)
    assert compute("aider", broke, None)[0] == SHED, compute("aider", broke, None)
    # shed override wins
    SHED_DIR.mkdir(parents=True, exist_ok=True)
    flag = SHED_DIR / "codex"
    created = not flag.exists()
    flag.touch()
    try:
        assert compute("codex", AGENTS["codex"], None)[0] == SHED  # 429-shed wins even with no data
    finally:
        if created:
            flag.unlink()
    print(
        "capacity.py selftest: OK (4-state enum, shed override, METERED cursor pool, count/dollar/windowed-prepaid capacity)"
    )


def main(argv):
    if "--selftest" in argv:
        _selftest()
        return 0
    HANDOFF.mkdir(parents=True, exist_ok=True)
    snap = build()
    OUT.write_text(json.dumps(snap, indent=2) + "\n")
    print(json.dumps(snap, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
