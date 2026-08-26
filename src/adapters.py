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
import sqlite3
import subprocess
import sys
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

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
    "codex": {"cheap": "gpt-5.6-luna", "mid": "gpt-5.6-terra", "full": "gpt-5.6-sol"},
    "claude": {"cheap": "claude-haiku-4-5", "mid": "claude-sonnet-5", "full": "claude-opus-5"},
    "gemini": {
        "cheap": "gemini-3.6-flash-low",
        "mid": "gemini-3.6-flash-high",
        "full": "gemini-3.1-pro-high",
    },
    "cursor": {},
    "vibe": {},
    "aider": {},
}
MODEL_TIER_NAMES = ("cheap", "mid", "full")  # ordered cheap -> expensive; the ceiling relies on it

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
    "claude": "mid",  # owner policy 2026-08-09: routine claude work runs Sonnet 5, not Opus 5
}
CURSOR_FRONTIER_DEFAULT = None  # require explicit 'frontier:<model>'; bare 'frontier' is unsafe
# Owner policy: cursor runs Composer and only Composer. Pinned by id because omitting --model
# selects `auto`, which is NOT Composer — it routes across every frontier model cursor advertises.
CURSOR_COMPOSER_MODEL = (
    os.environ.get("ORCH_CURSOR_COMPOSER_MODEL", "composer-2.5").strip() or "composer-2.5"
)

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
        "cmd": ["codex", "login", "status"],
        "strength": "presence",
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
        "cmd": ["claude", "auth", "status"],
        "strength": "presence",
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
AUTH_CACHE_TTL_S = int(os.environ.get("ORCH_AUTH_CACHE_TTL_S") or 900)  # 15m
_AUTH_MEMO: dict = {}
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")  # CLIs colourize; strip before matching/reporting
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
_ADVERTISED_MEMO: dict = {}  # per-agent in-process memo; one CLI probe per agent per run

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


def parse_model_catalog_pairs(text: str) -> dict[str, str]:
    """`{human label -> model id}` from a CLI catalog listing.

    The reverse of `parse_model_catalog`, needed because the CLIs report the LABEL at runtime and
    the id in their catalog: cursor's stream-json says `"model": "Composer 2.5"` while its catalog
    says `composer-2.5 - Composer 2.5 (current)`, and agy's log says
    `label="Gemini 3.7 Flash (High)"` against a catalog of `gemini-3.7-flash-high`. Parenthetical
    status suffixes are dropped so `(current)` does not become part of the key.
    """
    pairs: dict[str, str] = {}
    for line in (text or "").splitlines():
        if "\t" in line:
            head, _, label = line.partition("\t")
        elif " - " in line:
            head, _, label = line.partition(" - ")
        else:
            continue
        head, label = head.strip(), label.strip()
        if not head or " " in head or head.startswith("-") or not label:
            continue
        # BOTH FORMS, because the trailing parenthetical means opposite things per vendor: agy's
        # `(High)` is a TIER and part of the id, while cursor's `(current)` is status. Keying both
        # the full label and the stripped one lets each match without guessing which it is.
        pairs.setdefault(label.lower(), head)
        bare = re.sub(r"\s*\([^)]*\)\s*$", "", label).strip().lower()
        if bare:
            pairs.setdefault(bare, head)
    return pairs


# Catalog entries that are ROUTING TAGS, not model identities. cursor advertises `auto` in the same
# list as its 200-odd real ids, and recording `auto` as provider-resolved identity is precisely the
# "generic trace model is not provider resolution" CLAUDE.md section 2 forbids.
CATALOG_ROUTING_TAGS = frozenset({"auto", "default", "cli-default"})


def _catalog_model_id(candidate: str | None) -> str | None:
    """A catalog-sourced model id, or None when it is a routing tag rather than an identity.

    THE CATALOG IS ITS OWN VALIDATION. The id came out of the CLI's advertised list, so it exists by
    construction; what it still needs is the identity/tag distinction and the placeholder check the
    rest of the provenance path already applies (`_first_real_model` -> `feedback`).

    NOT `VENDOR_MODEL_RE`, and this is measured rather than argued: that regex is an allowlist of
    vendor families for guarding strings we did NOT get from an authority (a slug we formed, a
    transcript we grepped), and against the live `cursor-agent --list-models` on 2026-08-23 it
    rejects 42 of 204 REAL ids -- every `claude-fable-*`, `cursor-grok-*`, `kimi-*` and `glm-*`,
    plus the version-first `claude-4.6-opus-*` spelling. Using it as the catalog's validator would
    trade fabricated ids for lost ones and re-break on the next vendor family -- the same
    maintenance-treadmill shape as the bug above. `auto` is the ONE rejection it got right, and that
    one is nameable.
    """
    value = str(candidate or "").strip()
    if not value or value.lower() in CATALOG_ROUTING_TAGS:
        return None
    return _first_real_model([value])


def model_id_for_label(agent: str, label: str) -> str | None:
    """Turn a CLI's human model label into its model id, or None if it cannot be trusted.

    THE CATALOG IS THE AUTHORITY, and it has to be the CLI's RAW catalog to be one. This built its
    lookup as `parse_model_catalog_pairs("\\n".join(f"{mid}\\t{mid}" for mid in
    advertised_models(agent)))` -- a map of id->id, because `advertised_models` returns only ids.
    `catalog.get(text.lower())` was then handed a human LABEL, which can never be an id key, so the
    catalog branch was DEAD for every real label, the "prefers the CLI's own catalog" claim was
    inoperative, and every call fell through to the slug heuristic. Measured against the live
    `cursor-agent --list-models` on 2026-08-23, 4 of 5 real labels resolved wrongly: `Codex 5.3
    High` and `Claude Fable 5 1M Thinking (NO ZDR)` resolved to None (provenance simply lost), and
    `Claude Opus 5 1M Thinking` / `GPT-5.6 Sol 1M High` resolved to `claude-opus-5-1m-thinking` /
    `gpt-5.6-sol-1m-high` -- ids that DO NOT EXIST, written into
    `execution_attempts.resolved_model` as provider-resolved identity. Only `Composer 2.5`, whose
    label happens to slug into its own id, worked.

    Precedence, and the last two rungs are the point:
      1. the catalog's own `label -> id` pair (`advertised_catalog`);
      2. the label IS an advertised id -- some CLIs report the id, and the catalog confirms it;
      3. REFUSE, when the catalog was readable and lists neither. The slug would then be a guess
         the authority contradicts, and `VENDOR_MODEL_RE` cannot catch it --
         `claude-opus-5-1m-thinking` is perfectly vendor-shaped and perfectly fictional. CLAUDE.md
         section 2 forbids exactly this: a fabricated identity is worse than a skipped event.
      4. the vendor slug (`Gemini 3.7 Flash (High)` -> `gemini-3.7-flash-high`), ONLY while the
         catalog is UNKNOWN (probe off, CLI missing, auth failed), because an unreadable catalog
         must not cost us provenance we can still name. Still regex-guarded, so a chatty log line
         cannot become an id.

    Rungs 1-2 are validated by `_catalog_model_id`, NOT by `VENDOR_MODEL_RE`. See the note there:
    shape-matching an id the CLI itself advertised is both redundant and wrong.
    """
    text = str(label or "").strip()
    if not text:
        return None
    try:
        catalog = advertised_catalog(agent)
    except Exception:  # noqa: BLE001 - a probe failure must not block resolution
        catalog = {"models": [], "pairs": {}}
    direct = _catalog_model_id(catalog["pairs"].get(text.lower()))
    if direct:
        return direct
    if catalog["models"]:
        # The catalog ANSWERED. Accept the label only if it is itself an advertised id, then stop --
        # rung 3. `models` is the readability test, not `pairs`: both come from one probe, and
        # non-empty ids mean the CLI was read.
        exact = {mid.lower(): mid for mid in catalog["models"]}.get(text.lower())
        return _catalog_model_id(exact)
    slug = re.sub(r"[^a-z0-9.]+", "-", re.sub(r"\s*\([^)]*\)\s*$", "", text).lower()).strip("-")
    stripped = re.sub(r"\s*\(([^)]*)\)\s*$", r"-\1", text).lower()
    stripped = re.sub(r"[^a-z0-9.]+", "-", stripped).strip("-")
    for candidate in (stripped, slug):
        if candidate and VENDOR_MODEL_RE.fullmatch(candidate):
            return candidate
    return None


AGY_MODEL_LABEL_RE = re.compile(
    r"Propagating selected model override to backend:\s*label=\"([^\"]+)\""
)
# The per-run agy log's suffix, defined ONCE and consumed by both ends: `dispatcher` rewrites agy's
# `--log-file` to it, `cli_reported_model` reads it back. A reader that spells the writer's filename
# itself silently stops resolving the day the writer changes it, and the symptom is a model that
# never resolves -- which is exactly how the first version of that rewrite sat inert.
AGY_LOG_SUFFIX = ".agy.log"


def agy_log_for(log_file: str | Path | None) -> Path | None:
    """The per-run agy log for a dispatch log, or None when there is no log to derive it from.

    Accepts the agy log itself, so a caller that already holds it is not made to derive it twice and
    cannot accidentally produce `...agy.agy.log`.
    """
    if not log_file:
        return None
    path = Path(str(log_file)).expanduser()
    if path.name.endswith(AGY_LOG_SUFFIX):
        return path
    try:
        return path.with_suffix(AGY_LOG_SUFFIX)
    except ValueError:  # a name `with_suffix` refuses (empty, or a trailing dot)
        return path.with_name(path.name + AGY_LOG_SUFFIX)


def model_label_from_agy_log(text: str) -> str | None:
    """The model agy told its backend to use, from agy's own CLI log.

    agy's structured output does NOT carry the model -- `--output-format json` and `stream-json`
    both give only conversation_id, cwd and usage -- but its log does:

        model_config_manager.go:311] Propagating selected model override to backend:
            label="Gemini 3.7 Flash (High)"

    That is a LABEL, not an id; `model_id_for_label` maps it through `agy models`. Last occurrence
    wins, because the line is emitted repeatedly as the session settles and the final one is what
    the turn actually ran with.
    """
    found = AGY_MODEL_LABEL_RE.findall(text or "")
    return found[-1].strip() if found else None


def observed_model_from_stream(text: str) -> dict[str, Any]:
    """Model, session id and final text from a `stream-json` transcript the run itself printed.

    THE STANDARD ANSWER, and the reason it beats every store-scraping heuristic: the tool reports
    the model it actually used in its own stdout, so there is no workspace to match, no time window
    to guess, and no chance of attributing another session's model to this run. It is the CLI form
    of OpenTelemetry's `gen_ai.response.model` -- the model that SERVED the request, as distinct
    from `gen_ai.request.model`, which is what `--model` asked for.

    cursor emits `{"type":"system","subtype":"init",...,"model":"Composer 2.5"}` followed by a
    terminal `{"type":"result",...,"result":"..."}`. Returns the label verbatim; mapping it to an id
    is `model_id_for_label`'s job.
    """
    out: dict[str, Any] = {"model_label": None, "session_id": None, "result_text": None}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        out["session_id"] = out["session_id"] or row.get("session_id")
        if row.get("type") == "system" and row.get("subtype") == "init":
            out["model_label"] = row.get("model") or out["model_label"]
        elif row.get("type") == "result":
            value = row.get("result")
            if isinstance(value, str):
                out["result_text"] = value
    return out


parse_agy_models = parse_model_catalog  # back-compat alias


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
                line = line[len("export ") :].strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            env[key.strip()] = value.strip().strip('"').strip("'")
    except OSError:
        return None
    return env


def _cached_catalog(agent: str, now: float, cache_path: Path, *, need_pairs: bool) -> dict | None:
    """A fresh cached catalog, or None when the cache cannot answer THIS question.

    Hydrates the memo from disk BEFORE deciding, so an id-only caller still warms the memo for the
    next one, and so a legacy blob's absent `pairs` key survives into the memo rather than being
    flattened to an empty dict that would then look like a real answer.
    """
    memo = _ADVERTISED_MEMO.get(agent) or {}
    if memo.get("models") and now - float(memo.get("ts") or 0) <= GEMINI_MODEL_CACHE_TTL_S:
        if not need_pairs or "pairs" in memo:
            return {"models": list(memo["models"]), "pairs": dict(memo.get("pairs") or {})}
        return None  # memo predates the pairs migration; re-probe rather than answer blind
    try:
        cached = json.loads(cache_path.read_text())
    except (OSError, ValueError, TypeError):
        return None
    try:
        if now - float(cached.get("ts") or 0) > GEMINI_MODEL_CACHE_TTL_S:
            return None
    except (TypeError, ValueError):
        return None
    models = [str(m) for m in (cached.get("models") or [])]
    if not models:
        return None
    entry: dict = {"ts": cached.get("ts"), "models": models}
    if "pairs" in cached:
        entry["pairs"] = {str(k): str(v) for k, v in (cached.get("pairs") or {}).items()}
    _ADVERTISED_MEMO[agent] = entry
    if need_pairs and "pairs" not in entry:
        return None
    return {"models": models, "pairs": dict(entry.get("pairs") or {})}


def _advertised_catalog(agent: str, *, refresh: bool, timeout_s: int, need_pairs: bool) -> dict:
    """The installed CLI's own model catalog: `{"models": [ids], "pairs": {label: id}}`.

    ONE probe and ONE cache behind both projections, because "which ids exist" and "which label
    means which id" are the same question asked twice and must never disagree — they did, and the
    label half was answered with `id -> id` (see `model_id_for_label`).

    Both empty means UNKNOWN, never "nothing is advertised".

    CACHE MIGRATION, STATED RATHER THAN SILENT (this file is on the provenance path). The on-disk
    blob gains a `pairs` key ALONGSIDE the unchanged `models` list, so every existing reader of
    `models` — including `capacity`'s preflight and the `test_capacity_profiles` prerequisite
    check — is unaffected. A blob written before this carries no `pairs` KEY (presence, not
    truthiness, so a label-less catalog still caches and cannot cause a probe per call): it serves
    id requests from cache, while a PAIRS request treats it as a miss and re-probes, rewriting it in
    the new shape. The migration therefore self-heals within one TTL per agent, and until it does
    `model_id_for_label` degrades to the slug heuristic it already used — never to a wrong id.
    """
    probe = MODEL_CATALOG_PROBES.get(agent)
    if not probe or not _model_probe_enabled():
        return {"models": [], "pairs": {}}
    now = time.time()
    cache_path = _catalog_cache_path(agent)
    if not refresh:
        hit = _cached_catalog(agent, now, cache_path, need_pairs=need_pairs)
        if hit is not None:
            return hit
    try:
        proc = subprocess.run(
            probe,
            capture_output=True,
            text=True,
            env=_probe_env(agent),
            timeout=timeout_s,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return {"models": [], "pairs": {}}  # CLI missing/hung => unknown, not "unavailable"
    if proc.returncode != 0:
        return {"models": [], "pairs": {}}  # includes 'Authentication required' => unknown
    models = parse_agy_models(proc.stdout)
    if not models:
        return {"models": [], "pairs": {}}
    # THE RAW TEXT, parsed twice. Rebuilding pairs from `models` is what broke the resolver: the
    # labels only exist in the CLI's own output, so they have to be kept while it is in hand.
    pairs = parse_model_catalog_pairs(proc.stdout)
    _ADVERTISED_MEMO[agent] = {"ts": int(now), "models": models, "pairs": pairs}
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps({"ts": int(now), "models": models, "pairs": pairs}))
    except OSError:
        pass  # cache is an optimization, never a hard dependency
    return {"models": list(models), "pairs": dict(pairs)}


def advertised_catalog(agent: str, *, refresh: bool = False, timeout_s: int = 30) -> dict:
    """`{"models": [ids], "pairs": {label: id}}` the installed CLI advertises.

    Both empty means UNKNOWN. The `pairs` half is what `model_id_for_label` always needed and never
    had: the CLIs report a LABEL at runtime (`"model":"Composer 2.5"`,
    `label="Gemini 3.7 Flash (High)"`) and the id only in their catalog.
    """
    return _advertised_catalog(agent, refresh=refresh, timeout_s=timeout_s, need_pairs=True)


def advertised_models(agent: str, *, refresh: bool = False, timeout_s: int = 30) -> list[str]:
    """Model ids the installed CLI for `agent` actually offers, or [] when it can't be read.

    Catalog probes are ~3s network calls, so results are cached on disk per agent (TTL) and shared
    by the dispatcher and capacity's preflight. An EMPTY list means UNKNOWN, never "nothing is
    advertised" — callers must not read it as evidence that a model is missing. Agents with no
    probe registered always return [] and are therefore never judged unresolvable.

    Ids ONLY. For label resolution use `advertised_catalog` — building pairs out of this list
    yields `id -> id`, which is the bug `model_id_for_label` documents.
    """
    return _advertised_catalog(agent, refresh=refresh, timeout_s=timeout_s, need_pairs=False)[
        "models"
    ]


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
        return {
            "agent": agent,
            "authenticated": True,
            "checked": False,
            "strength": None,
            "reason": "no auth probe for this agent",
        }
    probe, strength = spec["cmd"], spec["strength"]
    now = time.time()
    memo = _AUTH_MEMO.get(agent)
    if not refresh and memo and now - float(memo.get("ts") or 0) <= AUTH_CACHE_TTL_S:
        return dict(memo["result"])
    env = dict(_probe_env(agent) or os.environ)
    env.update(AUTH_PROBE_ENV.get(agent) or {})
    try:
        proc = subprocess.run(
            probe,
            capture_output=True,
            text=True,
            env=env,
            timeout=timeout_s,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {
            "agent": agent,
            "authenticated": True,
            "checked": False,
            "strength": strength,
            "reason": f"auth probe could not run ({type(exc).__name__})",
        }
    # OUTPUT-driven, not returncode-driven: cursor-agent exits 0 while printing "Not logged in"
    # and while warning that the API key is invalid, so trusting the exit status reads a dead
    # credential as healthy. The failure text is the reliable signal.
    blob = _ANSI_RE.sub("", f"{proc.stdout or ''}\n{proc.stderr or ''}").strip()
    if _AUTH_FAIL_RE.search(blob):
        # A failure signal is trustworthy from EITHER strength: presence probes still detect an
        # absent credential, they just cannot vouch that a present one still works.
        result = {
            "agent": agent,
            "authenticated": False,
            "checked": True,
            "strength": strength,
            "reason": next(
                (ln.strip() for ln in blob.splitlines() if _AUTH_FAIL_RE.search(ln)), "auth failure"
            )[:160],
        }
    elif proc.returncode == 0 and blob:
        result = {
            "agent": agent,
            "authenticated": True,
            "checked": True,
            "strength": strength,
            "reason": (
                blob.splitlines()[0][:120]
                if strength == "validates"
                else "credential present (presence-only check): " + " ".join(blob.split())[:80]
            ),
        }
    else:
        return {
            "agent": agent,
            "authenticated": True,
            "checked": False,
            "strength": strength,
            "reason": f"auth probe exited {proc.returncode} without a usable signal",
        }
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
    tier = next(
        (i for i, t in enumerate(("high", "medium", "low")) if model_id.endswith("-" + t)), 3
    )
    return (family, -major, -minor, tier, model_id)


def _tier_override(agent: str, tier: str) -> tuple[str, str] | tuple[None, None]:
    """Operator pin for one (agent, tier), as (value, env-var-name)."""
    name = f"ORCH_{agent.upper()}_MODEL_{tier.upper()}"
    value = os.environ.get(name, "").strip()
    if value:
        return value, name
    if agent == "gemini" and tier == "full":  # legacy single-knob override, kept working
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
        return {
            **base,
            "model": None,
            "advertised": [],
            "resolvable": True,
            "reason": f"no pinned model for {agent}/{tier}; CLI default applies",
        }
    advertised = advertised_models(agent, refresh=refresh)
    if not advertised:
        return {
            **base,
            "model": pinned,
            "advertised": [],
            "resolvable": True,
            "reason": f"{agent} model catalog unavailable; using {pinned} unverified",
        }
    if pinned in advertised:
        return {
            **base,
            "model": pinned,
            "advertised": advertised,
            "resolvable": True,
            "reason": f"{pinned} is advertised by {agent}",
        }
    if override:
        return {
            **base,
            "model": override,
            "advertised": advertised,
            "resolvable": False,
            "reason": (
                f"{env_name}={override!r} is not advertised by {agent} "
                f"(offered: {', '.join(advertised)})"
            ),
        }
    picked = _auto_resolve(agent, tier, pinned, advertised)
    if not picked:
        return {
            **base,
            "model": pinned,
            "advertised": advertised,
            "resolvable": False,
            "reason": (
                f"pinned {pinned} is not advertised by {agent} and no sibling matched "
                f"(offered: {', '.join(advertised)})"
            ),
        }
    return {
        **base,
        "model": picked,
        "source": "auto_from_catalog",
        "advertised": advertised,
        "resolvable": True,
        "reason": f"pinned {pinned} no longer advertised by {agent}; auto-resolved to {picked}",
    }


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
# EVERY SEAT HERE WAS WRONG. This dict once listed cursor, gemini, vibe and aider as keeping no
# per-session model log, and that was asserted from an eight-line `find | head` -- inferring a
# blocker instead of verifying it. Looking properly, four of them do:
#
#   cursor  ~/.cursor/chats/<h>/<session>/store.db   providerOptions.cursor.modelName
#           joined by the sibling meta.json's `cwd` -- provider-side, not our request echoed back
#   vibe    ~/.vibe/logs/session/<id>/meta.json      config.active_model
#           joined by environment.working_directory, with start_time/end_time
#   gemini  conversation_summaries.db -> brain/<conversation_id>/.../transcript_full.jsonl
#           joined by workspace_uris; the transcript names what actually served
#
# The gemini case is why this matters more than tidiness: agy is a multi-provider ROUTER, and a
# real conversation there was served by `claude-sonnet-4-6` while our profile requests
# `gemini-3.1-pro-high`. The requested model is genuinely not what ran, so any scheme that recorded
# the request as the resolved model would fabricate the attribution -- and model rotation on that
# seat would be measuring the wrong thing entirely.
# The seats a reader exists for. ONE list, consumed by both `can_report_cli_identity` and
# `cli_reported_model`, so "we have a reader" and "we will look" can never disagree -- they did,
# and the answer was False for three seats whose readers were already written.
CLI_IDENTITY_READERS = ("codex", "claude", "cursor", "vibe", "gemini")
CURSOR_CHATS = Path(os.environ.get("ORCH_CURSOR_CHATS_DIR", HOME / ".cursor" / "chats"))
VIBE_SESSIONS = Path(os.environ.get("ORCH_VIBE_SESSIONS_DIR", HOME / ".vibe" / "logs" / "session"))
AGY_HOME = Path(os.environ.get("ORCH_AGY_HOME", HOME / ".gemini" / "antigravity-cli"))
# Only aider is left, and only because nothing has been found for it yet -- stated as an absence of
# evidence, not as a property of the tool.
# GEMINI IS NO LONGER HERE. Its conversation STORE genuinely records no model -- that part was
# verified -- but its CLI LOG does:
#   `model_config_manager.go:311] Propagating selected model override to backend: label="..."`
# and `--log-file` lets the dispatcher give each run its own log, so the join is direct rather than
# a workspace-and-window guess. `_agy_model_for` stays as a store fallback; the per-run log is the
# primary path. The lesson is the recurring one: "the tool does not record it" needed to mean "I
# read every place it could record it", and the first pass had only read the store.
NO_SESSION_LOG_AGENTS = {
    # PROBED, and the finding is specific rather than "nothing found". aider's `--analytics-log`
    # records `launched` / `repo` / `auto_commits` / `exit` with NO model in any properties, and its
    # stdout prints `Model: mistral/codestral-latest`, which is the requested alias echoed back --
    # and that alias FLOATS, so writing it as resolved identity is precisely the `--model` copy §2
    # forbids. `--llm-history-file` might carry the provider's own response metadata, but settling
    # that needs a real paid call on a seat with zero runs in the last week, which is not worth
    # spending to learn. Named so the next reader starts from the finding, not from scratch.
    "aider": "analytics log carries no model; stdout echoes the floating `codestral-latest` alias, "
    "which is the request. Unsettled: --llm-history-file (needs a paid call)",
}
# A real vendor model id, used to pick the model out of a transcript that also mentions filenames,
# branch names and prose. Deliberately an ALLOWLIST of vendor families: `_first_real_model` would
# happily accept `claude-fleet-list.sh` from a log otherwise.
VENDOR_MODEL_RE = re.compile(
    r"\b("
    r"gpt-[0-9][A-Za-z0-9.-]*"
    r"|o[0-9]-[A-Za-z0-9.-]+"
    r"|claude-(?:opus|sonnet|haiku)-[0-9][A-Za-z0-9.-]*"
    r"|gemini-[0-9][A-Za-z0-9.-]*"
    r"|composer-[0-9][A-Za-z0-9.-]*"
    r"|(?:mistral|magistral|devstral|codestral)[A-Za-z0-9.-]*"
    r"|gpt-oss[A-Za-z0-9.-]*"
    r")\b"
)
ROLLOUT_TS_RE = re.compile(r"rollout-(\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2})")


def can_report_cli_identity(agent: str) -> tuple[bool, str | None]:
    """Can this seat's CLI ever establish a resolved model? (capable, reason_it_cannot).

    THE SINGLE AUTHORITY, because the answer decides whether it is honest to write a
    `operation_role='worker'` execution attempt at all. A worker attempt exists to carry model
    provenance; recording one for a seat that can never supply it produces a row asserting the one
    thing it cannot establish, and those rows accumulate forever -- cursor alone offloads ~230 times
    every two days.

    This is a STATIC property of the seat, so it belongs in one place and must never be re-derived
    per run as a prose `fallback_reason` on thousands of rows. Adding a reader for a seat (a session
    log, a transcript) flips this to True and the attempts start being worth recording -- that is
    the drain, and it does not require any of the dead rows to be cleared first.
    """
    key = str(agent or "").strip().lower()
    if key in NO_SESSION_LOG_AGENTS:
        return False, f"no_cli_session_log:{NO_SESSION_LOG_AGENTS[key]}"
    if key in CLI_IDENTITY_READERS:
        return True, None
    return False, f"no_cli_identity_reader_for_agent:{key or 'unknown'}"


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


def _cursor_model_for(workspace: str, started_ts: int | None, window_s: int) -> str | None:
    """Model recorded in cursor's own chat store for the session that ran in `workspace`.

    `meta.json` carries `cwd` and millisecond timestamps; the sibling `store.db` holds the message
    blobs, where the provider's own `providerOptions.cursor.modelName` names what served. That is
    provider-side identity, not the `--model` we asked for.
    """
    target = str(Path(workspace).expanduser().resolve())
    for meta_path in sorted(CURSOR_CHATS.glob("*/*/meta.json"), reverse=True):
        try:
            meta = json.loads(meta_path.read_text(errors="ignore") or "{}")
        except (OSError, json.JSONDecodeError):
            continue
        if str(meta.get("cwd") or "") != target:
            continue
        if started_ts is not None:
            stamp = int((meta.get("updatedAtMs") or meta.get("createdAtMs") or 0) / 1000)
            if stamp and abs(stamp - started_ts) > window_s:
                continue
        store = meta_path.parent / "store.db"
        if not store.exists():
            continue
        try:
            conn = sqlite3.connect(f"file:{store}?mode=ro", uri=True)
            try:
                rows = conn.execute("SELECT data FROM blobs").fetchall()
            finally:
                conn.close()
        except sqlite3.Error:
            continue
        for (blob,) in rows:
            text = (
                blob.decode("utf-8", "ignore")
                if isinstance(blob, (bytes, bytearray))
                else str(blob)
            )
            found = re.search(r'"modelName"\s*:\s*"([^"]+)"', text)
            if found and VENDOR_MODEL_RE.fullmatch(found.group(1)):
                return found.group(1)
    return None


def _vibe_model_for(workspace: str, started_ts: int | None, window_s: int) -> str | None:
    """Model recorded in vibe's own session meta for the session that ran in `workspace`.

    `environment.working_directory` is the join and `config.active_model` is the model the CLI
    itself had active for that session -- read from vibe's record of the run, not from our request.
    """
    target = str(Path(workspace).expanduser().resolve())
    for meta_path in sorted(VIBE_SESSIONS.glob("*/meta.json"), reverse=True):
        try:
            meta = json.loads(meta_path.read_text(errors="ignore") or "{}")
        except (OSError, json.JSONDecodeError):
            continue
        env = meta.get("environment") or {}
        if str(env.get("working_directory") or "") != target:
            continue
        if started_ts is not None:
            stamp = _iso_to_epoch(meta.get("start_time"))
            if stamp and abs(stamp - started_ts) > window_s:
                continue
        model = str((meta.get("config") or {}).get("active_model") or "").strip()
        if model and VENDOR_MODEL_RE.fullmatch(model):
            return model
    return None


def _agy_model_for(workspace: str, started_ts: int | None, window_s: int) -> str | None:
    """Model that actually served an agy conversation in `workspace`.

    THE SEAT WHERE THIS MATTERS MOST. agy is a multi-provider router: a real conversation was served
    by `claude-sonnet-4-6` while the profile requests `gemini-3.1-pro-high`. `conversation_summaries`
    maps `workspace_uris` to a `conversation_id`, and that id is the brain directory holding the
    transcript which names the model.
    """
    target = str(Path(workspace).expanduser().resolve())
    index = AGY_HOME / "conversation_summaries.db"
    if not index.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{index}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                "SELECT conversation_id, workspace_uris, last_modified_time "
                "FROM conversation_summaries ORDER BY last_modified_time DESC"
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    for conversation_id, uris, modified in rows:
        if target not in str(uris or ""):
            continue
        if started_ts is not None and modified:
            # last_modified_time is microseconds in this store; normalise defensively.
            stamp = int(modified)
            while stamp > 10_000_000_000:
                stamp //= 1000
            if stamp and abs(stamp - started_ts) > window_s:
                continue
        logs = AGY_HOME / "brain" / str(conversation_id) / ".system_generated" / "logs"
        for name in ("transcript_full.jsonl", "transcript.jsonl"):
            model = _first_real_model(VENDOR_MODEL_RE.findall(_read_head(logs / name)))
            if model:
                return model
    return None


def _read_head(path: Path, limit_bytes: int = 4_000_000) -> str:
    try:
        with path.open("r", errors="ignore") as handle:
            return handle.read(limit_bytes)
    except OSError:
        return ""


def _iso_to_epoch(value: Any) -> int | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        from datetime import datetime

        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
    except (ValueError, TypeError):
        return None


def cli_reported_model(
    agent: str,
    workspace: str | Path | None,
    *,
    started_ts: int | None = None,
    window_s: int = 7200,
    log_file: str | Path | None = None,
) -> dict:
    """Identity the agent's own CLI recorded for the run in `workspace`.

    Always returns a dict, never None, and always says why when it says nothing:
    ``{"model": str | None, "cli_version": str | None, "source": str | None, "reason": str | None}``.
    An unresolved answer with a named reason is the whole point -- silence here was indistinguishable
    from "no such run".

    `log_file` is THIS run's dispatch log. It is what makes the gemini answer per-run rather than a
    workspace-and-window guess (see that branch); callers without one still get the store fallback.
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
    if agent == "gemini":
        # PER-RUN LOG FIRST, STORE SECOND -- the precedence the comment above
        # `NO_SESSION_LOG_AGENTS` has always claimed while this function did the opposite: `gemini`
        # mapped only to `_agy_model_for` and `model_label_from_agy_log` was never called from here,
        # so a gemini run resolved from the conversation store ONLY. Commit fe59bc7 ("the run
        # reports its own model") settles which way to fix it: agy's log line belongs to THIS run,
        # because the
        # dispatcher gives each run its own `--log-file`, where the store has to be matched by
        # workspace AND time window and can pick up a neighbour's session.
        agy_log = agy_log_for(log_file)
        if agy_log is not None:
            label = model_label_from_agy_log(_read_head(agy_log))
            # `model_id_for_label` resolves against agy's OWN catalog. That resolver had to be fixed
            # FIRST: while it built an id->id map it always fell through to a slug guess, so routing
            # gemini provenance through it would have persisted a heuristic as provider-resolved
            # identity -- CLAUDE.md section 2's exact prohibition.
            model = model_id_for_label("gemini", label) if label else None
            if model:
                return {
                    "model": model,
                    "cli_version": None,
                    "source": str(agy_log),
                    "reason": None,
                }
        model = _agy_model_for(str(workspace), started_ts, window_s)
        if model:
            return {
                "model": model,
                "cli_version": None,
                "source": "gemini-session-store",
                "reason": None,
            }
        # The reason names what was actually SEARCHED, so a run dispatched without a log is
        # distinguishable from one whose log named nothing.
        return {
            **blank,
            "reason": (
                "no_gemini_model_in_run_log_or_session_store"
                if agy_log is not None
                else "no_gemini_session_matched_workspace"
            ),
        }
    if agent in ("cursor", "vibe"):
        reader = {"cursor": _cursor_model_for, "vibe": _vibe_model_for}[agent]
        model = reader(str(workspace), started_ts, window_s)
        if not model:
            return {**blank, "reason": f"no_{agent}_session_matched_workspace"}
        return {
            "model": model,
            "cli_version": None,
            "source": f"{agent}-session-store",
            "reason": None,
        }
    if agent == "claude":
        directory = _claude_project_dir(workspace)
        if not directory.is_dir():
            return {**blank, "reason": "no_claude_transcript_dir_for_workspace"}
        newest = sorted(directory.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
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
            raise ValueError(
                f"profile {selected_profile['profile_id']} does not support {transport}"
            )
        immutable = {"reasoning_effort": reasoning_effort, "requested_model": requested_model}
        for key, override in immutable.items():
            if override is not None and override != selected_profile[key]:
                raise ValueError(
                    f"{key} override contradicts immutable profile {selected_profile['profile_id']}"
                )
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
        cmd = [
            profile_codex_binary() if selected_profile else "codex",
            "exec",
            "--skip-git-repo-check",
        ]
        if cwd is not None:
            cmd += ["--cd", str(Path(cwd).expanduser().resolve())]
        # The outer-seat bypass may preserve a workspace-write profile because
        # the parent seatbelt remains authoritative. It must never defeat an
        # explicit read-only narrowing: in that case keep the child sandbox and
        # fail closed if macOS refuses nested seatbelt application.
        if codex_bypass_inner_sandbox() and mode != "assess" and permission_mode != "read-only":
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
        # OFFLOADS ASK FOR stream-json SO THE RUN REPORTS ITS OWN MODEL. cursor's `system/init`
        # event carries `model` (plus cwd and session_id) in the offload's own stdout -- the CLI
        # form of `gen_ai.response.model`. That is exact and per-run, where scraping
        # `~/.cursor/chats` needs a workspace match and a time window and cannot distinguish two
        # runs in the same directory: all 24 cursor offloads shared `/private/tmp`, so no session
        # was attributable to any of them.
        #
        # Scoped to `transport="offload"` on purpose. The long-running dispatch path writes a log
        # that other code parses, and reshaping that output is a separate, riskier change.
        cursor_format = "stream-json" if transport == "offload" else "text"
        cmd = [
            "cursor-agent",
            "-p",
            prompt,
            "--force",
            "--output-format",
            cursor_format,
            "--trust",
            "--workspace",
            ".",
        ]
        if requested_model:
            cmd += ["--model", requested_model]
        elif mode and mode.startswith("frontier") and ":" in mode:
            cmd += [
                "--model",
                mode.split(":", 1)[1],
            ]  # explicit opt-in -> spends the metered mid-tier pool
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
        return [
            str(AIDER_BIN),
            "--model",
            "mistral/codestral-latest",
            "--message",
            prompt,
            "--yes-always",
            "--no-stream",
        ]
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

            capabilities.daily_heartbeat(
                "agy-runtime-isolation", "invocation", ref="adapters.build_command:gemini"
            )
        except Exception:
            pass
        cmd = ["agy", "--gemini_dir", gemini_dir]
        model = requested_model or _tier_model("gemini", mode) or gemini_model()
        if model:
            cmd += ["--model", model]
        return cmd + [
            "--print",
            prompt,
            "--dangerously-skip-permissions",
            "--add-dir",
            str(workspace),
            "--print-timeout",
            "40m",
            "--log-file",
            log_file,
        ]
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


def dispatch(
    agent: str,
    prompt: str,
    mode: str | None = None,
    cwd: str = ".",
    env: dict | None = None,
    timeout: int = 2400,
    *,
    profile=None,
    transport: str | None = None,
    permission_mode: str | None = None,
    reasoning_effort: str | None = None,
    requested_model: str | None = None,
) -> dict:
    """Run an agent on a task; outcome is judged by git SIDE-EFFECTS, not stdout
    (per design: a commit that touches a file, not a self-claimed 'done').
    Records one consumption row; the caller reconciles real cost_usd afterward.
    """
    cmd = build_command(
        agent,
        prompt,
        mode,
        cwd=cwd,
        profile=profile,
        transport=transport,
        permission_mode=permission_mode,
        reasoning_effort=reasoning_effort,
        requested_model=requested_model,
    )
    proc = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)
    status = subprocess.run(
        ["git", "-C", cwd, "status", "--porcelain"], capture_output=True, text=True
    ).stdout.strip()
    selected = execution_profiles.get_profile(profile) if profile is not None else None
    # Telemetry is fail-open, but return its structured classification to callers.
    combined_output = f"{proc.stderr or ''}"
    evidence_result = None
    try:
        import rate_incidents

        if proc.returncode != 0 or rate_incidents.stdout_carries_capacity_evidence(
            proc.stdout or ""
        ):
            combined_output = f"{proc.stdout or ''}\n{combined_output}"
        evidence_result = rate_incidents.get_structured_evidence(
            error_text=combined_output,
            agent=agent,
            surface="adapters.dispatch",
            target=str(Path(cwd).expanduser().resolve()),
        )
    except Exception as exc:
        print(f"warn: rate-incident classification failed for {agent}: {exc}", file=sys.stderr)
    if evidence_result and evidence_result.get("is_authoritative"):
        try:
            rate_incidents.record_incident(
                agent=agent,
                surface="adapters.dispatch",
                category=evidence_result["category"],
                status="recorded",
                target=str(Path(cwd).expanduser().resolve()),
                run_id=f"sync-dispatch:{agent}:{time.time_ns()}",
                evidence=combined_output,
                extra={"subcategory": evidence_result["subcategory"], "exit_code": proc.returncode},
            )
        except Exception as exc:
            print(f"warn: rate-incident recording failed for {agent}: {exc}", file=sys.stderr)
    record_ledger(
        agent,
        count=1,
        cost_usd=0.0,
        selected_profile_id=selected.get("profile_id") if selected else None,
        requested_model=selected.get("requested_model") if selected else requested_model,
    )
    # Add rate_incident_evidence to result for caller use
    result = {
        "agent": agent,
        "mode": mode,
        "exit": proc.returncode,
        "selected_profile_id": selected.get("profile_id") if selected else None,
        "changed_files": bool(status),
        "stdout_tail": (proc.stdout or "")[-2000:],
        "stderr_tail": (proc.stderr or "")[-1000:],
    }
    # Include rate-incident classification in result for router
    if evidence_result is not None:
        result["rate_incident_evidence"] = evidence_result
    return result


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
    import env_prereq  # imported here: env_prereq reads this module

    gaps = gaps if gaps is not None else []
    c = build_command("cursor", "do x")
    # Composer is PINNED, not implied: omitting --model selects `auto`, which routes across every
    # frontier model cursor sells. Owner policy is Composer only (2026-08-08).
    assert c[0] == "cursor-agent" and c[c.index("--model") + 1] == CURSOR_COMPOSER_MODEL, c
    assert (
        "--trust" in c and c[c.index("--workspace") + 1] == "."
    ), c  # headless workspace, no prompt
    cf = build_command("cursor", "do x", mode="frontier:gpt-5.5")
    assert "--model" in cf and "gpt-5.5" in cf, cf  # explicit opt-in draws the pool
    cb = build_command("cursor", "do x", mode="frontier")
    assert (
        cb[cb.index("--model") + 1] == CURSOR_COMPOSER_MODEL
    ), cb  # bare 'frontier' => Composer, no blind spend
    for tier in MODEL_TIER_NAMES:  # tiers never escape the policy
        ct = build_command("cursor", "do x", mode=tier)
        assert ct[ct.index("--model") + 1] == CURSOR_COMPOSER_MODEL, (tier, ct)
    a = build_command("aider", "do x")
    assert a[0].endswith("/aider-venv/bin/aider"), a  # isolated venv binary
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
                f"{_agent}: a limit must say whether it is permanent or pending",
                _spec["limit"],
            )
        else:
            # A validating probe must NOT carry a limit: that would be a contradiction in the record.
            assert not _spec.get("limit"), (_agent, _spec)
    assert AUTH_PROBES["cursor"]["strength"] == "validates", "cursor round-trips; do not downgrade"
    assert (
        AUTH_PROBES["gemini"]["strength"] == "validates"
    ), "agy models round-trips; do not downgrade"

    v = build_command("vibe", "do x")
    assert v[0] == "vibe" and "--auto-approve" in v and "--prompt" in v, v  # subscription headless
    assert (
        "--trust" in v
    ), "vibe must --trust the cwd or it silently ignores AGENTS.md (2026-06-15 fix)"
    assert "exec" in build_command("codex", "x")
    ca = build_command("codex", "x", mode="assess")
    assert (
        ca[:5] == ["codex", "exec", "--skip-git-repo-check", "--sandbox", "read-only"]
        and "--json" not in ca
    ), ca
    # Three-tier GPT-5.6 family: Luna (cheap) / Terra (mid) / Sol (full). Codex has no catalog
    # probe, so these resolve straight from MODEL_TIERS without a subprocess.
    for tier, expected in (
        ("cheap", "gpt-5.6-luna"),
        ("mid", "gpt-5.6-terra"),
        ("full", "gpt-5.6-sol"),
    ):
        cc = build_command("codex", "x", mode=tier)
        assert cc[cc.index("--model") + 1] == expected, (tier, cc)
        assert model_identity("codex", tier) == expected, tier
    # Non-tier modes still pass NO --model and keep the legacy lane tag.
    assert "--model" not in build_command(
        "codex", "x", mode="assess"
    ), "assess must not pin a model"
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
            assert (
                cmd[cmd.index("-c") + 1]
                == f'model_reasoning_effort="{profile["reasoning_effort"]}"'
            ), cmd
            profile_commands[profile["profile_id"]] = cmd
            assess = build_command(
                "codex",
                "x",
                mode="assess",
                profile=profile,
                transport="offload",
                permission_mode="read-only",
            )
            assert assess[assess.index("--sandbox") + 1] == "read-only", assess
            assert (
                "--json" not in assess
                and assess[assess.index("--model") + 1] == profile["requested_model"]
            ), assess
        assert {cmd[cmd.index("--model") + 1] for cmd in profile_commands.values()} == {
            "gpt-5.6-sol",
            "gpt-5.6-terra",
            "gpt-5.6-luna",
        }
    env_prereq.report_gaps("adapters.py", gaps)
    codex_cwd = HOME / ".codex" / "orchestrator" / "worktrees" / "selftest"
    ccwd = build_command("codex", "x", cwd=codex_cwd)
    assert "--cd" in ccwd and ccwd[ccwd.index("--cd") + 1] == str(codex_cwd), ccwd
    os.environ["CODEX_SANDBOX"] = "seatbelt"
    nested = build_command("codex", "x", cwd=codex_cwd)
    assert (
        "--dangerously-bypass-approvals-and-sandbox" in nested and "--sandbox" not in nested
    ), nested
    assert "--cd" in nested and nested[nested.index("--cd") + 1] == str(codex_cwd), nested
    os.environ["ORCH_CODEX_BYPASS_INNER_SANDBOX"] = "0"
    forced_sandbox = build_command("codex", "x", cwd=codex_cwd)
    assert (
        "--sandbox" in forced_sandbox
        and "--dangerously-bypass-approvals-and-sandbox" not in forced_sandbox
    ), forced_sandbox
    os.environ.pop("ORCH_CODEX_BYPASS_INNER_SANDBOX", None)
    os.environ.pop("CODEX_SANDBOX", None)
    # Claude 5 family: Haiku 4.5 (cheap) / Sonnet 5 (mid) / Opus 5 (full) — but the seat is CAPPED
    # at mid (scarce weekly), so the `full` lane really dispatches Sonnet 5. Expectations are
    # written post-ceiling because that is what reaches the CLI.
    assert MODEL_TIERS["claude"]["full"] == "claude-opus-5", "frontier option must stay defined"
    assert tier_ceiling("claude") == "mid" and effective_tier("claude", "full") == "mid"
    for tier, expected in (
        ("cheap", "claude-haiku-4-5"),
        ("mid", "claude-sonnet-5"),
        ("full", "claude-sonnet-5"),
    ):
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
        assert (
            g[0] == "agy"
            and "--print" in g
            and "--print-timeout" in g
            and "--dangerously-skip-permissions" in g
        ), g
        assert "--model" in g and g[g.index("--model") + 1] == DEFAULT_GEMINI_MODEL, g
        assert model_identity("gemini") == f"agy:{DEFAULT_GEMINI_MODEL}"
        assert agy_advertised_models() == [], "kill-switch must suppress the CLI probe entirely"
        unprobed = gemini_model_health()
        assert unprobed["resolvable"] and unprobed["source"] == "pinned_default", unprobed
        # THE ask: cheap/mid ride Flash, only full pays for Pro. Flash is both newer (3.6 vs 3.1)
        # and far cheaper in compute units on this metered seat.
        for tier, expected in (
            ("cheap", "gemini-3.6-flash-low"),
            ("mid", "gemini-3.6-flash-high"),
            ("full", "gemini-3.1-pro-high"),
        ):
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
    # LABEL -> ID, AGAINST THE CLI'S RAW CATALOG. The resolver used to build its lookup from
    # `advertised_models()`, which returns only ids, so the map was `id -> id` and a human LABEL
    # could never key it: the catalog branch was dead and every call fell through to the slug.
    # Fixtures are verbatim `cursor-agent --list-models` / `agy models` lines, because the whole
    # point is that the real catalog's labels do NOT slug into their ids.
    cursor_catalog = (
        "Fetching available models...\n"
        "auto - Auto (default)\n"
        "composer-2.5 - Composer 2.5 (current)\n"
        "gpt-5.3-codex-high - Codex 5.3 High\n"
        "claude-opus-5-thinking-high - Claude Opus 5 1M Thinking\n"
        "claude-fable-5-thinking-high - Claude Fable 5 1M Thinking (NO ZDR)\n"
    )
    import shutil as _probe_shutil
    import tempfile as _probe_tmp

    old_memo_pairs = dict(_ADVERTISED_MEMO)
    old_probe_flag = os.environ.pop("ORCH_MODEL_PROBE", None)
    try:
        # ISOLATION, NOT A SKIP: a memo entry means `_advertised_catalog` answers from memory, so
        # nothing here shells out to `cursor-agent` (the probe-leak this file has already been
        # bitten by twice). The memo needs the probe flag ON: the kill-switch short-circuits first.
        _ADVERTISED_MEMO["cursor"] = {
            "ts": time.time(),
            "models": parse_model_catalog(cursor_catalog),
            "pairs": parse_model_catalog_pairs(cursor_catalog),
        }
        # Every one of these was WRONG before the fix, verified against the live catalog on
        # 2026-08-23: two resolved to None and two to vendor-shaped ids that do not exist.
        for label, expected in (
            ("Composer 2.5", "composer-2.5"),
            ("Codex 5.3 High", "gpt-5.3-codex-high"),
            ("Claude Opus 5 1M Thinking", "claude-opus-5-thinking-high"),
            ("Claude Fable 5 1M Thinking (NO ZDR)", "claude-fable-5-thinking-high"),
            ("composer-2.5", "composer-2.5"),  # the CLI may report the id; the catalog confirms it
        ):
            assert model_id_for_label("cursor", label) == expected, (label, expected)
        # A ROUTING TAG IS NOT AN IDENTITY. cursor advertises `auto` beside its real ids.
        assert model_id_for_label("cursor", "Auto (default)") is None
        assert model_id_for_label("cursor", "auto") is None
        # THE ACCEPTANCE CASE. A vendor-renamed LABEL against an unchanged id: the slug
        # (`composer-pro-2.5`) is not advertised, so only the catalog can answer.
        _ADVERTISED_MEMO["cursor"] = {
            "ts": time.time(),
            "models": ["composer-2.5"],
            "pairs": parse_model_catalog_pairs("composer-2.5 - Composer Pro 2.5 (current)"),
        }
        assert model_id_for_label("cursor", "Composer Pro 2.5") == "composer-2.5"
        # DELIBERATE BREAK: the old id->id construction, which is what made the branch dead.
        _broken = {"models": ["composer-2.5"], "pairs": {"composer-2.5": "composer-2.5"}}
        _real_catalog = advertised_catalog
        try:
            globals()["advertised_catalog"] = lambda *_a, **_k: dict(_broken)
            assert (
                model_id_for_label("cursor", "Composer Pro 2.5") is None
            ), "the break must restore the pre-fix behaviour: a label cannot key an id->id map"
        finally:
            globals()["advertised_catalog"] = _real_catalog  # REVERTED
        assert model_id_for_label("cursor", "Composer Pro 2.5") == "composer-2.5"
        # A READABLE CATALOG THAT LISTS NEITHER REFUSES, rather than persisting a guess it
        # contradicts -- `VENDOR_MODEL_RE` cannot catch a fictional-but-vendor-shaped id.
        assert model_id_for_label("cursor", "Composer Ultra 9.9") is None
        assert (
            VENDOR_MODEL_RE.fullmatch("claude-opus-5-1m-thinking") is not None
        ), "the shape guard genuinely cannot do the catalog's job"
    finally:
        _ADVERTISED_MEMO.clear()
        _ADVERTISED_MEMO.update(old_memo_pairs)
        if old_probe_flag is not None:
            os.environ["ORCH_MODEL_PROBE"] = old_probe_flag
    # THE PROBE MUST KEEP THE LABELS. The cases above seed the memo, so they exercise the
    # RESOLVER but not the wiring the finding was actually about -- and with only those, rebuilding
    # `pairs` from `models` (the pre-fix construction) leaves the selftest green. Drive the real
    # `subprocess.run` seam with a verbatim catalog and assert on the pairs it stored.
    _probed_cache = Path(_probe_tmp.mkdtemp(prefix="adapters-catalog-probe-"))
    _old_runtime, _old_run = AGENT_RUNTIME, subprocess.run
    try:
        globals()["AGENT_RUNTIME"] = _probed_cache

        class _CatalogCompleted:
            returncode = 0
            stdout = cursor_catalog
            stderr = ""

        globals()["subprocess"].run = lambda *_a, **_k: _CatalogCompleted()
        _ADVERTISED_MEMO.pop("cursor", None)
        probed = advertised_catalog("cursor", refresh=True)
        assert probed["models"][:2] == ["auto", "composer-2.5"], probed["models"]
        assert probed["pairs"]["composer 2.5"] == "composer-2.5", probed["pairs"]
        assert probed["pairs"]["codex 5.3 high"] == "gpt-5.3-codex-high", probed["pairs"]
        # THE REGRESSION GUARD: an id->id map has ids for keys and no label keys at all.
        assert "composer-2.5" not in probed["pairs"], (
            "pairs must be keyed by LABEL; an id key means the id->id construction is back",
            probed["pairs"],
        )
        assert model_id_for_label("cursor", "Codex 5.3 High") == "gpt-5.3-codex-high"
        # And the pairs reach DISK, so the next process resolves labels without re-probing.
        _on_disk = json.loads(_catalog_cache_path("cursor").read_text())
        assert _on_disk["models"] == probed["models"], _on_disk
        assert _on_disk["pairs"]["claude opus 5 1m thinking"] == "claude-opus-5-thinking-high"
        # `models` is byte-compatible with the pre-migration blob every other reader still uses.
        assert set(_on_disk) == {"ts", "models", "pairs"}, _on_disk
    finally:
        globals()["AGENT_RUNTIME"] = _old_runtime
        globals()["subprocess"].run = _old_run
        _ADVERTISED_MEMO.pop("cursor", None)
        _ADVERTISED_MEMO.update(old_memo_pairs)
        _probe_shutil.rmtree(_probed_cache, ignore_errors=True)
    # AN UNKNOWN CATALOG KEEPS THE SLUG, so an unreadable CLI costs no provenance we can still
    # name. Removing the probe entry is how "UNKNOWN" is expressed without a subprocess.
    old_probes = dict(MODEL_CATALOG_PROBES)
    try:
        MODEL_CATALOG_PROBES.pop("cursor", None)
        assert model_id_for_label("cursor", "Composer 2.5") == "composer-2.5"
        assert model_id_for_label("cursor", "Gemini 3.7 Flash (High)") == "gemini-3.7-flash-high"
        assert model_id_for_label("cursor", "some log prose") is None
    finally:
        MODEL_CATALOG_PROBES.clear()
        MODEL_CATALOG_PROBES.update(old_probes)
    # THE CACHE MIGRATION IS EXPLICIT. A blob written before `pairs` existed still answers id
    # questions from cache, and is a MISS for label questions rather than answering blind.
    _cache_dir = Path(_probe_tmp.mkdtemp(prefix="adapters-catalog-cache-"))
    try:
        legacy = _cache_dir / "legacy.json"
        legacy.write_text(json.dumps({"ts": int(time.time()), "models": ["composer-2.5"]}))
        _ADVERTISED_MEMO.pop("cursor", None)
        assert _cached_catalog("cursor", time.time(), legacy, need_pairs=False) == {
            "models": ["composer-2.5"],
            "pairs": {},
        }
        _ADVERTISED_MEMO.pop("cursor", None)
        assert _cached_catalog("cursor", time.time(), legacy, need_pairs=True) is None
        # And the memo hydrated from that legacy blob must not then LOOK like a pairs answer.
        assert "pairs" not in (_ADVERTISED_MEMO.get("cursor") or {}), _ADVERTISED_MEMO.get("cursor")
        assert _cached_catalog("cursor", time.time(), legacy, need_pairs=True) is None
        migrated = _cache_dir / "migrated.json"
        migrated.write_text(
            json.dumps(
                {
                    "ts": int(time.time()),
                    "models": ["composer-2.5"],
                    "pairs": {"composer 2.5": "composer-2.5"},
                }
            )
        )
        _ADVERTISED_MEMO.pop("cursor", None)
        assert _cached_catalog("cursor", time.time(), migrated, need_pairs=True) == {
            "models": ["composer-2.5"],
            "pairs": {"composer 2.5": "composer-2.5"},
        }
    finally:
        _ADVERTISED_MEMO.pop("cursor", None)
        _ADVERTISED_MEMO.update(old_memo_pairs)
        _probe_shutil.rmtree(_cache_dir, ignore_errors=True)
    # Rename survival: pinned model gone => auto-pick the newest Pro/high seat, never die.
    old_memo = dict(_ADVERTISED_MEMO)
    try:
        _ADVERTISED_MEMO["gemini"] = {
            "ts": time.time(),
            "models": [
                "gemini-4.0-flash-high",
                "gemini-3.9-pro-low",
                "gemini-3.9-pro-high",
                "claude-sonnet-4-6",
            ],
        }
        renamed = gemini_model_health()
        assert renamed["model"] == "gemini-3.9-pro-high", renamed  # pro > flash, high > low
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
    assert (
        "--gemini_dir" in g and "agent-runtime/gemini/.gemini" in g[g.index("--gemini_dir") + 1]
    ), g
    assert g[g.index("--add-dir") + 1] == str(
        gemini_cwd
    ), g  # writes land in exact worktree, not stale/project cwd
    assert (
        "--log-file" in g and "agent-runtime/gemini/logs/agy.log" in g[g.index("--log-file") + 1]
    ), g
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
        build_command("bogus", "x")
        raise AssertionError("expected ValueError")
    except ValueError:
        pass
    print(
        "adapters.py selftest: OK (cursor composer/explicit-frontier/safe-bare-frontier, "
        "vibe subscription, codex/claude cheap-model map, aider venv, gemini lane-ready)"
    )


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    else:
        print("usage: adapters.py --selftest   (dispatch() is invoked by router.py)")
