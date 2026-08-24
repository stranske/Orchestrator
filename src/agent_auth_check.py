#!/usr/bin/env python3
"""agent_auth_check.py — is every seat's credential actually usable, right now?

Answers the question the fleet cannot answer for itself until a dispatch already failed. Two
seats broke silently in one week (gemini's model pin rotted; both cursor and claude *looked*
broken from a bare shell) and in every case `capacity.py` still reported `ok`. The common cause:
the fleet authenticates by SOURCING a per-agent credential FILE, so a plain `cursor-agent status`
in your terminal tests something different from what the lane actually runs.

This checks the way the fleet does, and never prints secret values.

    python3 agent_auth_check.py            # human-readable table
    python3 agent_auth_check.py --json     # machine-readable

Exit code: 0 = nothing known-broken, 1 = at least one seat is definitively unusable.
UNKNOWN is never an error — an unreachable check is not evidence of a bad credential.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import adapters

HOME = Path.home()

# Credential files the fleet sources at dispatch time (dispatcher.AGENT_ENV_FILES). Refreshing a
# seat means rewriting the FILE — an interactive CLI login writes to the CLI's own credential
# store, which the headless lane deliberately does not read (it sets
# AGENT_CLI_CREDENTIAL_STORE=memory), so "I logged in" does NOT mean "the fleet can dispatch".
CREDENTIAL_FILES = {
    "cursor": (HOME / ".cursor" / "cursor-agent.env", "CURSOR_API_KEY"),
    "claude": (HOME / ".codex" / "handoff" / ".claude-oauth-token", "CLAUDE_CODE_OAUTH_TOKEN"),
    "aider": (HOME / ".codex" / "handoff" / "aider.env", "MISTRAL_API_KEY"),
    # vibe has NO non-billing CLI check at all: `vibe -p` is programmatic mode and bills, and
    # every other entry point is the TUI. Its credential is a plain env file, so the file itself
    # is the only free signal available — weaker than a probe, but strictly better than silence.
    "vibe": (HOME / ".vibe" / ".env", "MISTRAL_API_KEY"),
}
REFRESH_HINT = {
    "cursor": (
        "cursor.com → Dashboard → Integrations → API Keys → create key, then write "
        "CURSOR_API_KEY=<key> to ~/.cursor/cursor-agent.env (chmod 600)"
    ),
    "claude": (
        "run `claude setup-token` and write the result as "
        "CLAUDE_CODE_OAUTH_TOKEN=<token> to ~/.codex/handoff/.claude-oauth-token"
    ),
    "aider": "console.mistral.ai → API keys, then set MISTRAL_API_KEY in ~/.codex/handoff/aider.env",
    "vibe": "console.mistral.ai → API keys, then set MISTRAL_API_KEY in ~/.vibe/.env (chmod 600)",
}
AGENTS = ("cursor", "codex", "claude", "gemini", "vibe", "aider")


# Bounded, like TICK_ENV_ATTEMPTS: enough to clear a blip, few enough that a real problem still
# surfaces promptly rather than being retried into silence.
CRED_READ_ATTEMPTS = int(os.environ.get("ORCH_CRED_READ_ATTEMPTS", "3"))


def _credential_file_state(agent: str) -> dict:
    """Presence/shape of the credential file — never its contents."""
    entry = CREDENTIAL_FILES.get(agent)
    if not entry:
        return {"path": None, "present": None, "expected_key": None, "key_present": None}
    path, key = entry
    if not path.is_file():
        return {"path": str(path), "present": False, "expected_key": key, "key_present": False}
    # BOUNDED RETRY, and it names WHY it failed. This tree lives on a cloud-sync mount that
    # intercepts every open(), so a read can fail transiently on a file that plainly exists -- and an
    # undeterminable key made `check()` return UNKNOWN, which
    # `test_every_seat_has_some_free_signal` reads as a credential defect. That is a machine fault
    # reported as a tree fault, the exact failure `tick_env` was hardened against ("a red pointing at
    # the tree for a fault in the machine... teaches the reader to re-run until green, which is how a
    # real red gets ignored"). Observed once on 2026-08-21 during a full verify.py run under load.
    #
    # A DETERMINISTIC defect is NOT retried: `path.is_file()` already returned False above for a
    # genuinely absent credential, so everything reaching here is a file that exists and a read that
    # did not work -- which is the retryable class by construction.
    text, read_error = None, None
    for attempt in range(CRED_READ_ATTEMPTS):
        try:
            text = path.read_text()
            break
        except OSError as exc:
            read_error = f"{type(exc).__name__}: {exc}"
            if attempt + 1 < CRED_READ_ATTEMPTS:
                time.sleep(0.2 * (attempt + 1))
    if text is None:
        return {
            "path": str(path),
            "present": True,
            "expected_key": key,
            "key_present": None,
            "read_error": read_error,
            "reason_class": "environment",
        }
    return {
        "path": str(path),
        "present": True,
        "expected_key": key,
        "key_present": any(
            line.strip().lstrip("export ").startswith(key + "=") for line in text.splitlines()
        ),
    }


def check(agent: str, *, refresh: bool = True) -> dict:
    """One seat: credential file + non-billing auth probe + model dispatchability."""
    auth = adapters.auth_health(agent, refresh=refresh)
    tiers = {}
    broken_tier = None
    for tier in adapters.MODEL_TIER_NAMES:
        health = adapters.model_health(agent, tier)
        tiers[tier] = health["model"]
        if not health.get("resolvable", True) and broken_tier is None:
            broken_tier = (tier, health["reason"])
    strength = auth.get("strength")
    cred = _credential_file_state(agent)
    if auth.get("checked") and not auth.get("authenticated", True):
        verdict, detail = "BROKEN", f"auth: {auth['reason']}"
    elif broken_tier:
        verdict, detail = "BROKEN", f"model ({broken_tier[0]}): {broken_tier[1]}"
    elif auth.get("checked") and strength == "validates":
        verdict, detail = "OK", auth["reason"]
    elif auth.get("checked"):
        # Presence-only: a credential exists but nothing proved it still works. Reporting this as
        # OK is the false assurance that let a revoked claude token look healthy.
        verdict, detail = "PRESENT", auth["reason"]
    elif cred["present"] is False:
        # No CLI probe, but we know the file the fleet sources is absent — that IS a hard failure.
        verdict, detail = "BROKEN", f"credential file missing: {cred['path']}"
    elif cred["key_present"] is False:
        verdict, detail = "BROKEN", f"{cred['expected_key']} not set in {cred['path']}"
    elif cred["key_present"]:
        # Weakest tier: only the credential FILE was inspected, no CLI was asked.
        verdict = "CONFIGURED"
        detail = f"{cred['expected_key']} present in {cred['path']} (file check only, no probe)"
        strength = strength or "config"
    elif cred.get("reason_class") == "environment":
        # UNDETERMINABLE, and it says which side. The credential file EXISTS and the read failed
        # after CRED_READ_ATTEMPTS -- a fact about the machine, not the seat. Still reported as
        # UNKNOWN (deliberately NOT softened to a pass: an assertion weakened into a skip stops
        # catching the absent credential it exists for), but the detail now names where to look.
        verdict = "UNKNOWN"
        detail = (
            f"could not read {cred['path']} after {CRED_READ_ATTEMPTS} attempts "
            f"({cred.get('read_error')}) -- this is an ENVIRONMENT fault (the file exists), "
            "not a missing credential; re-check the volume before changing any config"
        )
    else:
        verdict, detail = "UNKNOWN", auth["reason"]
    return {
        "agent": agent,
        "verdict": verdict,
        "detail": detail,
        "tier_models": tiers,
        "auth_strength": strength,
        "credential_file": cred,
        "reason_class": cred.get("reason_class") or ("ok" if verdict != "UNKNOWN" else "tree"),
        "refresh_hint": REFRESH_HINT.get(agent),
    }


def run(agents=AGENTS) -> list[dict]:
    return [check(a) for a in agents]


def _render(rows: list[dict]) -> str:
    out = []
    for row in rows:
        cred = row["credential_file"]
        if cred["present"] is False:
            cred_note = f"  credential file MISSING: {cred['path']}"
        elif cred["key_present"] is False:
            cred_note = f"  credential file lacks {cred['expected_key']}: {cred['path']}"
        else:
            cred_note = ""
        out.append(f"{row['verdict']:8} {row['agent']:8} {row['detail']}")
        if cred_note:
            out.append(cred_note)
        if row["verdict"] == "BROKEN" and row["refresh_hint"]:
            out.append(f"  fix: {row['refresh_hint']}")
    order = ("BROKEN", "OK", "PRESENT", "CONFIGURED", "UNKNOWN")
    tally = {v: sum(1 for r in rows if r["verdict"] == v) for v in order}
    out.append("")
    out.append(
        f"{tally['BROKEN']} broken, {tally['OK']} verified, {tally['PRESENT']} present-only, "
        f"{tally['CONFIGURED']} file-only, {tally['UNKNOWN']} unknown"
    )
    out.append("  confidence, strongest first:")
    out.append("    OK         = credential round-tripped to the server and works")
    out.append("    PRESENT    = the CLI says a credential exists; nothing proved it still works")
    out.append("    CONFIGURED = only the credential file was inspected; no CLI was asked")
    out.append("    UNKNOWN    = no free signal at all; never treated as a failure")
    return "\n".join(out)


def _selftest() -> None:
    rows = [
        {
            "agent": "a",
            "verdict": "OK",
            "detail": "fine",
            "tier_models": {},
            "credential_file": {
                "present": True,
                "key_present": True,
                "path": "/x",
                "expected_key": "K",
            },
            "refresh_hint": None,
        },
        {
            "agent": "b",
            "verdict": "BROKEN",
            "detail": "auth: nope",
            "tier_models": {},
            "credential_file": {
                "present": False,
                "key_present": False,
                "path": "/y",
                "expected_key": "K",
            },
            "refresh_hint": "do the thing",
        },
    ]
    text = _render(rows)
    assert "1 broken" in text and "credential file MISSING" in text and "do the thing" in text, text
    # UNKNOWN must never be counted as broken — the whole point of the unknown-vs-failure rule.
    unknown = [
        {
            "agent": "c",
            "verdict": "UNKNOWN",
            "detail": "no probe",
            "tier_models": {},
            "credential_file": {
                "present": None,
                "key_present": None,
                "path": None,
                "expected_key": None,
            },
            "refresh_hint": None,
        }
    ]
    assert "0 broken" in _render(unknown), _render(unknown)
    # A real run must not raise and must classify every seat.
    live = run()
    assert {r["verdict"] for r in live} <= {
        "OK",
        "BROKEN",
        "PRESENT",
        "CONFIGURED",
        "UNKNOWN",
    }, live
    assert {r["agent"] for r in live} == set(AGENTS), live
    # A MACHINE FAULT MUST NOT READ AS A CONFIG DEFECT. On a cloud-sync mount a read can fail on a
    # file that plainly exists; that produced a bare UNKNOWN indistinguishable from "no credential".
    import tempfile as _tf

    with _tf.TemporaryDirectory(prefix="cred-state-") as _td:
        _p = Path(_td) / "seat.env"
        _p.write_text("export MISTRAL_API_KEY=abc\n")
        _saved = CREDENTIAL_FILES.get("vibe")
        _real_read = Path.read_text
        try:
            CREDENTIAL_FILES["vibe"] = (_p, "MISTRAL_API_KEY")
            _ok = _credential_file_state("vibe")
            assert _ok["key_present"] is True and not _ok.get("reason_class"), _ok
            _calls = []

            def _boom(self, *a, **k):
                if self == _p:
                    _calls.append(1)
                    raise OSError("Resource temporarily unavailable")
                return _real_read(self, *a, **k)

            Path.read_text = _boom
            _bad = _credential_file_state("vibe")
            assert _bad["key_present"] is None, _bad
            assert _bad["reason_class"] == "environment", _bad
            # NOT self-referential: `len(_calls) == CRED_READ_ATTEMPTS` alone passes happily when the
            # constant is 1, i.e. when the retry has been removed. Assert the retry EXISTS too.
            assert CRED_READ_ATTEMPTS >= 2, "the retry is the point; one attempt is no retry"
            assert len(_calls) == CRED_READ_ATTEMPTS, (len(_calls), CRED_READ_ATTEMPTS)
            # A file that is genuinely ABSENT stays a hard failure and is NOT retried.
            Path.read_text = _real_read
            CREDENTIAL_FILES["vibe"] = (Path(_td) / "nope.env", "MISTRAL_API_KEY")
            _gone = _credential_file_state("vibe")
            assert _gone["present"] is False and not _gone.get("reason_class"), _gone
        finally:
            Path.read_text = _real_read
            if _saved is not None:
                CREDENTIAL_FILES["vibe"] = _saved

    print("agent_auth_check.py selftest: OK (render, unknown-is-not-broken, live sweep)")


def main(argv) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--agent", action="append", help="check only these agents (repeatable)")
    args = ap.parse_args(argv)
    if args.selftest:
        _selftest()
        return 0
    rows = run(args.agent or AGENTS)
    print(json.dumps(rows, indent=2) if args.json else _render(rows))
    return 1 if any(r["verdict"] == "BROKEN" for r in rows) else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
