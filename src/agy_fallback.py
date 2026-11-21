"""Select Antigravity's independent third-party pool from a fresh quota observation."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pacing

POLL = Path.home() / ".codex/orchestrator/usage-poll/gemini-usage.json"
MODELS = ("claude-sonnet-5-5-low", "claude-opus-5-5-low", "gpt-oss-120b-medium")


def select(*, data=None, now=None, advertised=None):
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
        found = {b.get("id"): b for b in rows if isinstance(b, dict)}
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
    if None in (gw, gh, tw, th) or not (gw == 0 or gh == 0) or tw <= 0 or th <= 0:
        return None
    if advertised is None:
        import adapters
        advertised = adapters.advertised_models("gemini")
    return next((f"agy-{model}" for model in MODELS if model in advertised), None)
