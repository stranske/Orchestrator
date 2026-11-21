"""Select Antigravity's independent third-party pool from a fresh quota observation."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pacing

POLL = Path.home() / ".codex/orchestrator/usage-poll/gemini-usage.json"
MODELS = ("claude-sonnet-5-5-high", "claude-opus-5-5-medium", "gpt-oss-120b-medium")


def weekly_drain(*, data=None, now=None):
    now = time.time() if now is None else now
    if data is None:
        try:
            data = json.loads(POLL.read_text())
        except (OSError, ValueError):
            return False
    if not isinstance(data, dict):
        return False
    ts = pacing.timestamp(data.get("ts"))
    if ts is None or not 0 <= now - ts <= pacing.STALE_AFTER_SECONDS:
        return False
    groups = data.get("groups")
    if not isinstance(groups, list):
        return False
    group = [g for g in groups if isinstance(g, dict) and g.get("name") == "Claude and GPT models"]
    if len(group) != 1 or not isinstance(group[0].get("buckets"), list):
        return False
    rows = [b for b in group[0]["buckets"] if isinstance(b, dict) and b.get("id") == "3p-weekly"]
    if len(rows) != 1:
        return False
    reset = pacing.timestamp(rows[0].get("reset_time"))
    return reset is not None and 0 < reset - now <= 24 * 3600


def select(*, data=None, now=None, advertised=None, purpose="gemini", claude_data=None):
    """Return a profile ID only for fresh, unambiguous, usable third-party quota."""
    now = time.time() if now is None else now
    if data is None:
        try:
            data = json.loads(POLL.read_text())
        except (OSError, ValueError):
            return None
    if not isinstance(data, dict):
        return None
    ts = pacing.timestamp(data.get("ts"))
    if ts is None or not 0 <= now - ts <= pacing.STALE_AFTER_SECONDS:
        return None
    groups = data.get("groups")
    if not isinstance(groups, list):
        return None

    def buckets(name):
        matches = [g for g in groups if isinstance(g, dict) and g.get("name") == name]
        if len(matches) != 1 or not isinstance(matches[0].get("buckets"), list):
            return None
        rows = matches[0]["buckets"]
        if any(not isinstance(b, dict) or not isinstance(b.get("id"), str) for b in rows):
            return None
        found = {b["id"]: b for b in rows}
        if len(found) != len(rows):
            return None
        return found

    gem, third = buckets("Gemini Models"), buckets("Claude and GPT models")
    if gem is None or third is None:
        return None

    def remaining(rows, key):
        row = rows.get(key)
        if not isinstance(row, dict):
            return None
        value = row.get("remaining_fraction")
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
            return None
        end = pacing.timestamp(row.get("reset_time"))
        if end is not None and end <= now:
            return None
        return value

    gw, gh = remaining(gem, "gemini-weekly"), remaining(gem, "gemini-5h")
    tw, th = remaining(third, "3p-weekly"), remaining(third, "3p-5h")
    if None in (gw, gh, tw, th) or tw <= 0 or th <= 0:
        return None
    if purpose == "gemini":
        if not (gw == 0 or gh == 0):
            return None
    elif purpose == "partner":
        import pacing_usage

        if claude_data is None:
            claude_data = pacing_usage.read_json(pacing_usage.POLL_DIR / "claude-usage.json")
        claude = pacing_usage.polled_usage("claude", now, data=claude_data)
        session = claude_data.get("session") if isinstance(claude_data, dict) else None
        session_end = (
            pacing.timestamp(session.get("resets_at")) if isinstance(session, dict) else None
        )
        exhausted = bool(
            claude["poll"].get("fresh")
            and (
                claude.get("used_fraction") == 1
                or (
                    claude.get("short_used_fraction") == 1
                    and session_end is not None
                    and session_end > now
                )
            )
        )
        draining = weekly_drain(data=data, now=now)
        if not (exhausted or draining):
            return None
    else:
        raise ValueError("unknown Antigravity fallback purpose")
    import adapters
    import execution_profiles

    if advertised is None:
        advertised = adapters.advertised_models("gemini")
    claude_cap = adapters.tier_ceiling("claude") if purpose == "partner" else None
    for model in MODELS:
        if model in advertised:
            if claude_cap is not None:
                if model.startswith("gpt-") or claude_cap == "cheap":
                    continue
                if model.startswith("claude-opus-") and claude_cap == "mid":
                    continue
            profile = execution_profiles.get_profile(f"agy-{model}")
            if adapters.automatic_profile_within_cap(profile):
                return profile["profile_id"]
    return None


def claude_backup_for_dispatch():
    """Recheck the separate pool and AGY seat immediately before a dispatch."""
    import router

    cap = router.load_capacity()
    gem = cap.get("agents", {}).get("gemini", {})
    if not isinstance(gem, dict) or (gem.get("auth") or {}).get("authenticated") is False:
        return None
    if gem.get("state") not in {"ok", "warn"} and gem.get("policy") not in {
        "weekly-polled-cap",
        "window-polled-cap",
    }:
        return None
    return select(purpose="partner")
