#!/usr/bin/env python3
"""gh_capacity.py — GitHub REST API rate-budget capacity layer for the orchestrator.

Item #8 P2 (IMPROVEMENT_BACKLOG.md). `capacity.py` models the LLM-seat quota; this models the
GitHub API rate budget the local lanes SHARE through one token (`~/.codex/credentials/gh_cli_token`).
gh-heavy ops — `durability_sweep`, keepalive backfill/ingest, `langsmith_fetch` — can hit the REST
**search limit (30/min)** (or core 5000/hr, graphql 5000pts/hr) and throttle or error. This gives
them graceful degradation instead.

Mirrors `capacity.py`'s shape on purpose (PR #2350 §4 / §11 anti-over-engineering): a 4-state enum
per resource, a READ-TIME reduction over an append-only NDJSON ledger (design §4.2: "append-only
event log; never rewritten"), fail-open, `--selftest` fully offline. No scoring, no learning here —
it only answers "does the shared gh budget have headroom for resource X right now?".

Signals, in priority (cf. capacity.py's "429 is authoritative" inversion):
  1. `x-ratelimit-*` response headers from REAL `gh api` calls, fed for FREE by `gh_run()` (no extra
     probe) — the read-time ledger, exactly capacity.json's pattern.
  2. `probe()`: `gh api rate_limit` (a FREE endpoint that does not count against any budget) seeds /
     refreshes the ledger on demand (cold start, the `orchestrate.sh` tick gate, the snapshot).
Both append the same ledger rows; `state()`/`throttle()` read the most recent row per resource and
reason over remaining-vs-limit and the window reset (past reset => the window refilled => OK).

`throttle(resource)`: paces (sleep to glide under the per-window rate when LOW) or defers (when SHED,
returns action='defer' rather than blocking for up to an hour) so rate-heavy ops degrade gracefully.
`orchestrate.sh`'s `--gate <resource>` skips a SHED step this tick (stamp untouched -> retried).

`--auth-preflight` (`auth_preflight()`): the `--active` tick's first question, asked with REAL calls.
It tells "GitHub cannot answer now" (rate-limited, unreachable) from "the token is missing or refused",
which `gh auth status` reports with one exit code. Only the second aborts the tick.

Read-only and safe; fail-open everywhere (probe failure or no data => OK/proceed, never a false halt).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

HANDOFF = Path(os.environ.get("HANDOFF_DIR", Path.home() / ".codex" / "handoff"))
GH_LEDGER = (
    HANDOFF / "gh-rate-ledger.ndjson"
)  # append-only: {ts, resource, remaining, limit, reset, used, source}
OUT = HANDOFF / "gh-capacity.json"

OK, LOW, SHED, UNKNOWN = "ok", "low", "shed", "unknown"

# GitHub's documented fixed windows per resource (seconds) — used to derive a safe pacing interval.
WINDOW_SECONDS = {
    "core": 3600,
    "search": 60,
    "graphql": 3600,
    "code_search": 60,
    "integration_manifest": 3600,
}


def _env_int(name: str, default: int) -> int:
    v = os.environ.get(name)
    if v:
        try:
            return int(v)
        except ValueError:
            pass
    return default


# Absolute remaining-floor reserve per resource: defends the TINY search budget, where a fraction is
# meaningless (5% of 30 ~= 1). The fraction thresholds below handle the big core/graphql pools.
RESERVE = {
    "search": _env_int("GH_RESERVE_SEARCH", 3),
    "core": _env_int("GH_RESERVE_CORE", 100),
    "graphql": _env_int("GH_RESERVE_GRAPHQL", 100),
}
DEFAULT_RESERVE = 5
LOW_FRAC = 0.25  # remaining below 25% of limit => LOW (pace)
SHED_FRAC = 0.05  # remaining below 5% of limit (or <= the reserve floor) => SHED (defer)
MAX_PACE_S = 10.0  # cap one throttle pace/short-defer sleep so a cron tick never stalls
GATE_SHED_EXIT = 75  # --gate exit code when the resource is SHED (0 otherwise, fail-open)
TRACKED = ("core", "search", "graphql")


def _append_ledger(rows: list[dict]) -> None:
    """Append-only (single '>>'), never rewritten — no two-writer lost-update race (design §4.2)."""
    rows = [r for r in rows if r]
    if not rows:
        return
    HANDOFF.mkdir(parents=True, exist_ok=True)
    with GH_LEDGER.open("a") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def _latest_ledger(resource: str) -> dict | None:
    """Most recent ledger row for `resource` — the read-time reduction (cf. capacity._ledger_usage)."""
    if not GH_LEDGER.exists():
        return None
    latest = None
    for line in GH_LEDGER.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get("resource") != resource:
            continue
        ts = r.get("ts", 0)
        if not isinstance(ts, (int, float)):
            continue
        if latest is None or ts >= latest.get("ts", 0):
            latest = r
    return latest


def probe(*, timeout_s: int = 10, runner=subprocess.run) -> dict | None:
    """Read `gh api rate_limit` (FREE; does not count against any budget) and seed the ledger with
    one row per resource. Returns the `resources` dict or None on failure (fail-open: callers proceed).
    """
    try:
        r = runner(["gh", "api", "rate_limit"], capture_output=True, text=True, timeout=timeout_s)
    except Exception:
        return None
    if getattr(r, "returncode", 1) != 0:
        return None
    try:
        data = json.loads(r.stdout)
    except Exception:
        return None
    resources = data.get("resources") or {}
    now = time.time()
    rows = []
    for name, res in resources.items():
        if not isinstance(res, dict):
            continue
        rows.append(
            {
                "ts": now,
                "resource": name,
                "remaining": res.get("remaining"),
                "limit": res.get("limit"),
                "reset": res.get("reset"),
                "used": res.get("used"),
                "source": "probe",
            }
        )
    _append_ledger(rows)
    return resources


def _split_headers_body(out: str) -> tuple[dict, str]:
    """Split a `gh api --include` response into (lowercased headers, body). Single header block
    (our calls don't redirect); first blank line separates headers from the JSON body."""
    if not out:
        return {}, ""
    sep = "\r\n\r\n" if "\r\n\r\n" in out else "\n\n"
    parts = out.split(sep, 1)
    if len(parts) == 1:
        return {}, out
    headers = {}
    for line in parts[0].splitlines():
        if ":" in line and not line.startswith("HTTP"):
            k, _, v = line.partition(":")
            headers[k.strip().lower()] = v.strip()
    return headers, parts[1]


def _ratelimit_row_from_headers(headers: dict, *, fallback_resource: str) -> dict | None:
    rem, lim = headers.get("x-ratelimit-remaining"), headers.get("x-ratelimit-limit")
    if rem is None or lim is None:
        return None

    def _int(x):
        try:
            return int(x)
        except (TypeError, ValueError):
            return None

    return {
        "ts": time.time(),
        "resource": headers.get("x-ratelimit-resource") or fallback_resource,
        "remaining": _int(rem),
        "limit": _int(lim),
        "reset": _int(headers.get("x-ratelimit-reset")),
        "used": _int(headers.get("x-ratelimit-used")),
        "source": "call",
    }


def gh_run(args: list[str], *, resource: str = "core", timeout_s: int = 30, runner=subprocess.run):
    """Run a `gh api` call with header capture, feed the rate ledger from `x-ratelimit-*` for FREE,
    and return (returncode, parsed_body). `args` is the gh argv WITHOUT a leading 'gh'. Only `gh api`
    surfaces rate headers, so this is the per-call ledger feed for CORE/GRAPHQL work; SEARCH budget is
    tracked via probe() (the CLI's `gh pr list`/`gh search` do not expose the headers)."""
    cmd = ["gh"] + list(args)
    if "api" in cmd and "--include" not in cmd and "-i" not in cmd:
        cmd.insert(cmd.index("api") + 1, "--include")
    try:
        r = runner(cmd, capture_output=True, text=True, timeout=timeout_s)
    except Exception:
        return 1, None
    headers, body = _split_headers_body(getattr(r, "stdout", "") or "")
    row = _ratelimit_row_from_headers(headers, fallback_resource=resource)
    if row is not None:
        _append_ledger([row])
    parsed = None
    if body:
        try:
            parsed = json.loads(body)
        except Exception:
            parsed = body
    return getattr(r, "returncode", 1), parsed


def state(resource: str, *, _row: dict | None = None) -> tuple[str, dict]:
    """4-state read-time verdict for one resource (pure: reads the ledger, makes NO gh calls)."""
    row = _row if _row is not None else _latest_ledger(resource)
    now = time.time()
    if not row or row.get("remaining") is None or row.get("limit") is None:
        return UNKNOWN, {
            "resource": resource,
            "pace_s": 0.0,
            "reset_in_s": 0,
            "reason": "no rate data; proceed (fail-open)",
        }
    remaining, limit = row["remaining"], row.get("limit") or 0
    reset = row.get("reset") or 0
    reset_in = max(0, int(reset - now)) if reset else 0
    # Past the reset => the window refilled; the stale `remaining` no longer applies.
    if reset and reset <= now:
        return OK, {
            "resource": resource,
            "remaining": limit,
            "limit": limit,
            "reset_in_s": 0,
            "pace_s": 0.0,
            "reason": f"window reset; full budget ({limit})",
        }
    reserve = RESERVE.get(resource, DEFAULT_RESERVE)
    frac = (remaining / limit) if limit else 0.0
    window = WINDOW_SECONDS.get(resource, 3600)
    meta = {
        "resource": resource,
        "remaining": remaining,
        "limit": limit,
        "reset_in_s": reset_in,
        "window_s": window,
        "pace_s": 0.0,
    }
    if remaining <= reserve or frac <= SHED_FRAC:
        meta["reason"] = (
            f"{remaining}/{limit} remaining (<= reserve {reserve} or {SHED_FRAC:.0%}); "
            f"defer ~{reset_in}s to reset"
        )
        return SHED, meta
    if frac <= LOW_FRAC:
        # Glide the remaining usable calls across the rest of the window, defending the reserve.
        usable = max(1, remaining - reserve)
        pace = (reset_in / usable) if reset_in else (window / max(1, limit))
        meta["pace_s"] = round(min(MAX_PACE_S, max(0.0, pace)), 3)
        meta["reason"] = f"{remaining}/{limit} remaining ({frac:.0%}); pace ~{meta['pace_s']}s/call"
        return LOW, meta
    meta["reason"] = f"{remaining}/{limit} remaining ({frac:.0%}); ok"
    return OK, meta


def throttle(resource: str, *, sleeper=time.sleep, allow_wait_s: float = MAX_PACE_S) -> dict:
    """Pace (LOW) or defer (SHED) against the shared gh budget. Makes NO gh calls — pure ledger read
    plus an optional bounded sleep. SHED with a long reset returns action='defer' (caller skips)
    rather than blocking; SHED with a short reset (<= allow_wait_s, e.g. search's 60s window) waits.
    """
    st, meta = state(resource)
    out = {"resource": resource, "state": st, "action": "proceed", "slept_s": 0.0, **meta}
    if st == SHED:
        reset_in = meta.get("reset_in_s", 0)
        if 0 < reset_in <= allow_wait_s:
            sleeper(reset_in + 0.5)
            out.update(action="waited", slept_s=reset_in + 0.5)
        else:
            out["action"] = "defer"
        return out
    if st == LOW:
        pace = min(allow_wait_s, meta.get("pace_s", 0.0) or 0.0)
        if pace > 0:
            sleeper(pace)
            out.update(action="paced", slept_s=pace)
        return out
    return out  # OK / UNKNOWN -> proceed (fail-open)


def throttle_if_enabled(resource: str, **kw) -> dict | None:
    """In-loop throttle for the gh-heavy lanes, ACTIVE only when ORCH_GH_THROTTLE=1 (orchestrate.sh
    sets it for the cron context). No-op otherwise, so manual runs and the consumer module selftests
    stay hermetic — no ledger read, no sleep, no gh. Fail-open on any error."""
    if os.environ.get("ORCH_GH_THROTTLE") != "1":
        return None
    try:
        return throttle(resource, **kw)
    except Exception:
        return None


def build(*, runner=subprocess.run) -> dict:
    """Probe + snapshot the tracked resources (what `gh_capacity.py` with no args writes/prints)."""
    resources = probe(runner=runner)
    out: dict[str, Any] = {
        "generated_at": int(time.time()),
        "probe_ok": resources is not None,
        "resources": {},
    }
    for name in TRACKED:
        st, meta = state(name)
        out["resources"][name] = {"state": st, **meta}
    return out


def _gate(resource: str, *, runner=subprocess.run) -> int:
    """orchestrate.sh hook: always re-probe (FREE rate_limit endpoint) for a real-time, cross-step
    view of the SHARED budget, then exit non-zero ONLY when SHED. Fail-open: UNKNOWN/OK/LOW => 0, so a
    broken probe (or budget an earlier step already used, leaving stale data) never silently halts the
    cadence — a SHED step defers to the next tick (stamp untouched)."""
    probe(runner=runner)
    st, meta = state(resource)
    print(f"gh_capacity gate[{resource}]: {st} — {meta.get('reason', '')}", file=sys.stderr)
    return GATE_SHED_EXIT if st == SHED else 0


# --- Auth preflight: the --active tick's first question ------------------------------------------
# `gh auth status` cannot answer it. It exits 1, printing "The token in GH_TOKEN is invalid.", for a
# missing token, a 401, a 403 rate limit, a secondary limit, a 502 and an unreachable host alike
# (measured 2026-10-02 against a local stub). On 2026-10-02T15:40Z the owner's per-user REST budget
# was exhausted (Actions jobs failed on it from 15:39:02Z to 15:42:39Z), and the tick read "cannot
# measure now" as "measured: not authenticated". It aborted, so nothing below the preflight ran: no
# heartbeats, no cadence steps, no monitors. Its log also named the wrong cause.
#
# So the preflight asks with REAL calls, one REST and one GraphQL, the two budgets the tick spends.
# It never reads `rate_limit`: that endpoint is exempt from the limit, and it has reported a fresh
# 5000/5000 while every real call failed. It has three exits, and only positive evidence that the
# credentials are missing or refused aborts. Everything it cannot measure defers instead.
PREFLIGHT_OK = 0
PREFLIGHT_DEFER = (
    GATE_SHED_EXIT  # token present, GitHub cannot answer now: GitHub-dependent steps defer
)
PREFLIGHT_UNAUTHENTICATED = (
    77  # EX_NOPERM: no token (gh exit 4), HTTP 401, or a 403 that is no rate limit
)
PREFLIGHT_EXITS = {
    "ok": PREFLIGHT_OK,
    "rate_limited": PREFLIGHT_DEFER,
    "unavailable": PREFLIGHT_DEFER,
    "unauthenticated": PREFLIGHT_UNAUTHENTICATED,
}
PREFLIGHT_TIMEOUT_S = 20
PREFLIGHT_DEFERS = "GitHub-dependent steps defer, local steps run"
_STATUS_LINE_RE = re.compile(r"^HTTP/\S+\s+(\d{3})\b")
_TOKEN_RE = re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9_]{16,}|github_pat_[A-Za-z0-9_]{20,})")


def _as_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _first_line(text: str, limit: int = 160) -> str:
    """The first non-blank line, token-shaped strings redacted: this text goes to the tick log."""
    for line in (text or "").splitlines():
        if line.strip():
            return _TOKEN_RE.sub("<redacted>", line.strip())[:limit]
    return ""


def _preflight_call(args: list[str], *, runner, timeout_s: int) -> dict:
    """One `gh api --include` call, reduced to what the classifier reads. Never raises."""
    cmd = ["gh", "api", "--include", *args]
    try:
        r = runner(cmd, capture_output=True, text=True, timeout=timeout_s)
    except FileNotFoundError:
        return {"transport_error": "gh CLI not found on PATH"}
    except subprocess.TimeoutExpired:
        return {"transport_error": f"no answer within {timeout_s}s"}
    except Exception as exc:  # noqa: BLE001 -- a transport failure is a measurement, not a crash
        return {"transport_error": f"{type(exc).__name__}: {exc}"}
    out = getattr(r, "stdout", "") or ""
    status = _STATUS_LINE_RE.match(out.lstrip().split("\n", 1)[0].strip())
    headers, body = _split_headers_body(out)
    try:
        parsed = json.loads(body) if body else None
    except ValueError:
        parsed = None
    return {
        "rc": getattr(r, "returncode", 1),
        "status": int(status.group(1)) if status else None,
        "headers": headers,
        "body": parsed,
        "stderr": getattr(r, "stderr", "") or "",
    }


def _api_message(call: dict, *, full: bool = False) -> str:
    """GitHub's own words: the REST `message`, else the first GraphQL error, else gh's stderr.

    Cut after the first full sentence for the log line, because GitHub appends support boilerplate
    and a request id. A sentence shorter than 24 characters is kept whole with what follows it, so
    "Sorry. Your account was suspended." survives. `full=True` keeps all of it for the predicate."""
    body = call.get("body")
    text = ""
    if isinstance(body, dict):
        errors = body.get("errors")
        if body.get("message"):
            text = str(body["message"])
        elif isinstance(errors, list) and errors and isinstance(errors[0], dict):
            text = str(errors[0].get("message") or errors[0].get("type") or "")
    text = text or call.get("stderr", "")
    if full:
        return _TOKEN_RE.sub("<redacted>", text)
    line = _first_line(text, limit=400)
    cut = line.find(". ", 24)
    return (line[: cut + 1] if cut != -1 else line)[:160]


def _rate_limited(call: dict, *, now: float) -> tuple[bool, int | None]:
    """(is this answer a rate limit, the epoch it lifts at or None). PURE.

    The fleet's Actions predicate (`isRateLimitError` in agents-auto-pilot.yml): HTTP 429, a 403 that
    names a rate limit, or no budget remaining. Plus a Retry-After header (the secondary limits) and
    GraphQL's RATE_LIMITED error, which arrives on an HTTP 200."""
    status, headers, body = call.get("status"), call.get("headers") or {}, call.get("body")
    remaining = _as_int(headers.get("x-ratelimit-remaining"))
    retry_after = _as_int(headers.get("retry-after"))
    errors = body.get("errors") if isinstance(body, dict) else None
    graphql_limited = isinstance(errors, list) and any(
        isinstance(e, dict) and str(e.get("type") or "").upper() == "RATE_LIMITED" for e in errors
    )
    names_limit = "rate limit" in _api_message(call, full=True).lower()
    limited = (
        status == 429
        or (status == 403 and (names_limit or retry_after is not None))
        or (remaining is not None and remaining <= 0)
        or graphql_limited
    )
    if not limited:
        return False, None
    if retry_after is not None:
        return True, int(now) + retry_after
    return True, _as_int(headers.get("x-ratelimit-reset"))


def _classify_call(call: dict, *, now: float) -> dict:
    """One answer -> {verdict, detail, reset, resource, remaining, limit}. PURE.

    A verdict of `unauthenticated` needs positive evidence: gh's own exit 4 (no credential at all),
    an HTTP 401, or a 403 that is not a rate limit. A transport failure, a timeout, a 5xx or an answer
    this cannot read is `unavailable`: it could not be measured, which is not a "no"."""
    headers = call.get("headers") or {}
    out = {
        "resource": headers.get("x-ratelimit-resource"),
        "remaining": _as_int(headers.get("x-ratelimit-remaining")),
        "limit": _as_int(headers.get("x-ratelimit-limit")),
        "reset": None,
    }
    if "transport_error" in call:
        return {**out, "verdict": "unavailable", "detail": call["transport_error"]}
    rc, status = call.get("rc"), call.get("status")
    if rc == 4:
        detail = "no token: gh exited 4, authentication required"
        return {**out, "verdict": "unauthenticated", "detail": detail}
    if status is None:
        if rc == 0:
            return {**out, "verdict": "ok", "detail": "gh succeeded"}
        detail = _first_line(call.get("stderr", "")) or f"gh exited {rc} with no HTTP response"
        return {**out, "verdict": "unavailable", "detail": detail}
    if status == 401:
        detail = f"HTTP 401: {_api_message(call) or 'Bad credentials'}"
        return {**out, "verdict": "unauthenticated", "detail": detail}
    limited, reset = _rate_limited(call, now=now)
    if limited:
        detail = f"HTTP {status}: {_api_message(call)}"
        return {**out, "verdict": "rate_limited", "reset": reset, "detail": detail}
    if status == 403:
        detail = f"HTTP 403, not a rate limit: {_api_message(call)}"
        return {**out, "verdict": "unauthenticated", "detail": detail}
    if rc == 0 and 200 <= status < 300:
        return {**out, "verdict": "ok", "detail": f"HTTP {status}"}
    return {**out, "verdict": "unavailable", "detail": f"HTTP {status}: {_api_message(call)}"}


def _budget(check: dict, fallback: str) -> str:
    resource = check.get("resource") or fallback
    if check.get("remaining") is None or check.get("limit") is None:
        return f"{resource} budget not reported"
    return f"{resource} {check['remaining']}/{check['limit']}"


def _iso(epoch: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def auth_preflight(
    *,
    runner=subprocess.run,
    timeout_s: int = PREFLIGHT_TIMEOUT_S,
    now: float | None = None,
    record: bool = True,
) -> dict:
    """Is the token present and accepted, and can GitHub answer right now? Returns the verdict, the
    exit code orchestrate.sh switches on, and the one line it prints.

    REST is asked first, because a refused credential there needs no second call. GraphQL is asked
    only when REST answered. Each real answer also feeds the rate ledger, tagged `preflight`, so the
    true budget is on record every tick beside the `rate_limit` probe's figure."""
    now = time.time() if now is None else now
    rest_call = _preflight_call(["user"], runner=runner, timeout_s=timeout_s)
    calls = [("rest", rest_call)]
    checks = [("rest", _classify_call(rest_call, now=now))]
    if checks[0][1]["verdict"] == "ok":
        query = ["graphql", "-f", "query={ viewer { login } }"]
        calls.append(("graphql", _preflight_call(query, runner=runner, timeout_s=timeout_s)))
        checks.append(("graphql", _classify_call(calls[-1][1], now=now)))
    if record:
        rows = []
        for name, call in calls:
            row = _ratelimit_row_from_headers(call.get("headers") or {}, fallback_resource=name)
            if row is not None:
                rows.append({**row, "source": "preflight"})
        try:
            _append_ledger(rows)
        except OSError:
            pass  # the ledger is evidence, never a reason to misreport the verdict
    name, decisive = next(((n, c) for n, c in checks if c["verdict"] != "ok"), checks[-1])
    verdict = decisive["verdict"]
    if verdict == "ok":
        body = rest_call.get("body")
        login = body.get("login") if isinstance(body, dict) else None
        budgets = ", ".join(_budget(c, n) for n, c in checks)
        line = f"gh: authenticated as {login or 'an unnamed account'} ({budgets})"
    elif verdict == "rate_limited":
        until = f"until {_iso(decisive['reset'])}" if decisive["reset"] else "(reset not reported)"
        detail = f"{_budget(decisive, name)}; {decisive['detail']}"
        line = f"gh rate-limited {until}: {PREFLIGHT_DEFERS} ({detail})"
    elif verdict == "unavailable":
        line = f"gh unavailable: {PREFLIGHT_DEFERS} ({name}: {decisive['detail']})"
    else:
        line = f"gh not authenticated ({name}: {decisive['detail']})"
    return {
        "verdict": verdict,
        "exit": PREFLIGHT_EXITS[verdict],
        "line": line,
        "checks": dict(checks),
    }


def _selftest():
    import shutil
    import tempfile

    global GH_LEDGER
    saved_ledger = GH_LEDGER
    original_time = time.time
    tmp = Path(tempfile.mkdtemp(prefix="gh-capacity-selftest-"))
    GH_LEDGER = tmp / "gh-rate-ledger.ndjson"
    now = 1_800_000.0
    time.time = lambda: now
    try:
        # 1. No ledger data => UNKNOWN (fail-open: callers proceed).
        assert state("search")[0] == UNKNOWN, state("search")
        assert throttle("search")["action"] == "proceed"

        # 2. state() thresholds via crafted rows (search: limit 30, reserve 3).
        def row(resource, remaining, limit, reset_in):
            return {
                "ts": now,
                "resource": resource,
                "remaining": remaining,
                "limit": limit,
                "reset": now + reset_in,
                "source": "probe",
            }

        assert state("search", _row=row("search", 20, 30, 40))[0] == OK
        st, meta = state("search", _row=row("search", 6, 30, 15))  # 20% -> LOW
        assert st == LOW and 0 < meta["pace_s"] <= MAX_PACE_S, (st, meta)
        assert state("search", _row=row("search", 2, 30, 40))[0] == SHED  # <= reserve 3
        assert state("core", _row=row("core", 40, 5000, 1800))[0] == SHED  # < 5% of 5000
        assert state("core", _row=row("core", 4000, 5000, 1800))[0] == OK
        # Past the reset => window refilled => OK regardless of the stale remaining.
        assert state("search", _row=row("search", 0, 30, -10))[0] == OK

        # 3. throttle: SHED long-reset defers (no sleep); SHED short-reset waits; LOW paces.
        slept = []

        def sl(s):
            return slept.append(s)

        # craft via the ledger so throttle()'s internal state() read picks it up
        _append_ledger([row("search", 1, 30, 1800)])  # SHED, long reset
        assert throttle("search", sleeper=sl)["action"] == "defer" and not slept
        GH_LEDGER.write_text("")  # reset ledger
        _append_ledger([row("search", 1, 30, 5)])  # SHED, short reset (<= MAX_PACE_S)
        d = throttle("search", sleeper=sl)
        assert d["action"] == "waited" and slept and abs(slept[-1] - 5.5) < 1e-6, (d, slept)
        GH_LEDGER.write_text("")
        slept.clear()
        _append_ledger([row("search", 6, 30, 15)])  # LOW
        d = throttle("search", sleeper=sl)
        assert d["action"] == "paced" and slept and slept[-1] == d["slept_s"], (d, slept)

        # 4. probe() parses `gh api rate_limit` JSON and seeds the ledger.
        GH_LEDGER.write_text("")

        class FakeProbe:
            returncode = 0
            stdout = json.dumps(
                {
                    "resources": {
                        "core": {
                            "limit": 5000,
                            "remaining": 4900,
                            "reset": int(now + 3600),
                            "used": 100,
                        },
                        "search": {"limit": 30, "remaining": 4, "reset": int(now + 30), "used": 26},
                    }
                }
            )

        res = probe(runner=lambda *a, **k: FakeProbe())
        assert res and res["search"]["remaining"] == 4
        assert state("core")[0] == OK, state("core")  # 4900/5000 -> OK
        assert state("search")[0] == LOW, state(
            "search"
        )  # 4/30 = 13% (>5%, >reserve 3) -> LOW (pace)

        # 5. gh_run() parses --include headers, feeds a 'call' row, returns the parsed body.
        GH_LEDGER.write_text("")
        inc = (
            "HTTP/2.0 200 OK\r\n"
            "x-ratelimit-limit: 5000\r\nx-ratelimit-remaining: 4321\r\n"
            f"x-ratelimit-reset: {int(now + 3600)}\r\nx-ratelimit-resource: core\r\n"
            "x-ratelimit-used: 679\r\n\r\n"
            '{"sha": "abc"}'
        )

        class FakeApi:
            returncode = 0
            stdout = inc

        rc, body = gh_run(
            ["api", "repos/o/r/commits"], resource="core", runner=lambda *a, **k: FakeApi()
        )
        assert rc == 0 and body == {"sha": "abc"}, (rc, body)
        latest = _latest_ledger("core")
        assert latest["remaining"] == 4321 and latest["source"] == "call", latest
        # gh_run inserted --include after 'api'
        captured = {}
        gh_run(["api", "x"], runner=lambda cmd, **k: captured.setdefault("cmd", cmd) or FakeApi())
        assert captured["cmd"][:3] == ["gh", "api", "--include"], captured

        # 6. throttle_if_enabled is a no-op unless ORCH_GH_THROTTLE=1 (hermetic for consumer tests).
        os.environ.pop("ORCH_GH_THROTTLE", None)
        assert throttle_if_enabled("search") is None
        os.environ["ORCH_GH_THROTTLE"] = "1"
        try:
            GH_LEDGER.write_text("")
            assert throttle_if_enabled("search") == {
                "resource": "search",
                "state": UNKNOWN,
                "action": "proceed",
                "slept_s": 0.0,
                "pace_s": 0.0,
                "reset_in_s": 0,
                "reason": "no rate data; proceed (fail-open)",
            }
        finally:
            os.environ.pop("ORCH_GH_THROTTLE", None)

        # 7. _gate: 0 for OK/UNKNOWN/LOW (fail-open), GATE_SHED_EXIT only for SHED.
        GH_LEDGER.write_text("")

        # UNKNOWN + probe disabled (runner returns failure) => fail-open 0
        class FailRunner:
            returncode = 1
            stdout = ""

        assert _gate("graphql", runner=lambda *a, **k: FailRunner()) == 0
        _append_ledger([row("search", 1, 30, 1800)])  # SHED
        assert _gate("search", runner=lambda *a, **k: FailRunner()) == GATE_SHED_EXIT
        _append_ledger([row("search", 25, 30, 50)])  # fresh OK row (newer ts not needed; same now)
        # newest row wins by ts; write an explicitly newer one
        _append_ledger([{**row("search", 25, 30, 50), "ts": now + 1}])
        assert _gate("search", runner=lambda *a, **k: FailRunner()) == 0

        # 8. auth_preflight: the answers measured from gh 2.94.0 against a local stub, 2026-10-02.
        #    Only positive evidence of a missing or refused credential may exit 77; anything that
        #    could not be measured exits 75 (defer), because "cannot measure" is not "no".
        reset = int(now) + 300

        def answer(status, body, headers=None, rc=None, stderr=""):
            head = "".join(f"{k}: {v}\r\n" for k, v in (headers or {}).items())
            return {
                "returncode": (0 if status < 400 else 1) if rc is None else rc,
                "stdout": f"HTTP/2.0 {status} X\r\n{head}\r\n{json.dumps(body)}",
                "stderr": stderr,
            }

        def budget(remaining, resource="core"):
            return {
                "X-Ratelimit-Limit": "5000",
                "X-Ratelimit-Remaining": str(remaining),
                "X-Ratelimit-Reset": str(reset),
                "X-Ratelimit-Resource": resource,
            }

        rest_ok = answer(200, {"login": "stub-user"}, budget(4321))
        gql_ok = answer(200, {"data": {"viewer": {"login": "stub-user"}}}, budget(4990, "graphql"))

        def gh(rest, gql=gql_ok, seen=None):
            def run(cmd, **_):
                if seen is not None:
                    seen.append(cmd)
                if isinstance(rest, BaseException) or isinstance(rest, type):
                    raise rest if isinstance(rest, BaseException) else rest()
                r = gql if "graphql" in cmd else rest
                return type("R", (), r)()

            return run

        def verdict(rest, gql=gql_ok):
            return auth_preflight(runner=gh(rest, gql), now=now, record=False)

        seen: list = []
        ok = auth_preflight(runner=gh(rest_ok, seen=seen), now=now, record=False)
        assert ok["exit"] == PREFLIGHT_OK and ok["verdict"] == "ok", ok
        assert (
            ok["line"] == "gh: authenticated as stub-user (core 4321/5000, graphql 4990/5000)"
        ), ok
        assert [c[:4] for c in seen] == [
            ["gh", "api", "--include", "user"],
            ["gh", "api", "--include", "graphql"],
        ], seen
        assert all("rate_limit" not in c for c in seen), "the exempt endpoint must never be asked"

        limit_msg = {"message": "API rate limit exceeded for user ID 1. If you reach out ..."}
        primary = verdict(answer(403, limit_msg, budget(0)))
        assert primary["exit"] == PREFLIGHT_DEFER and primary["verdict"] == "rate_limited", primary
        assert primary["line"] == (
            f"gh rate-limited until {_iso(reset)}: GitHub-dependent steps defer, local steps run "
            "(core 0/5000; HTTP 403: API rate limit exceeded for user ID 1.)"
        ), primary["line"]
        assert "graphql" not in primary["checks"], "a rate-limited REST answer needs no 2nd call"
        secondary = verdict(
            answer(
                403, {"message": "You have exceeded a secondary rate limit."}, {"Retry-After": "60"}
            )
        )
        assert secondary["verdict"] == "rate_limited" and secondary["checks"]["rest"]["reset"] == (
            int(now) + 60
        ), secondary
        assert verdict(answer(429, {"message": "Too Many Requests"}))["verdict"] == "rate_limited"
        gql_limited = answer(
            200,
            {"errors": [{"type": "RATE_LIMITED", "message": "API rate limit exceeded"}]},
            budget(0, "graphql"),
            rc=1,
        )
        g = verdict(rest_ok, gql_limited)
        assert g["exit"] == PREFLIGHT_DEFER and "(graphql 0/5000; " in g["line"], g
        for unavailable in (
            answer(502, {"message": "Server Error"}),
            {
                "returncode": 1,
                "stdout": "",
                "stderr": 'Get "https://api.github.com/user": dial tcp',
            },
            FileNotFoundError,
            subprocess.TimeoutExpired("gh", 20),
        ):
            v = verdict(unavailable)
            assert v["exit"] == PREFLIGHT_DEFER and v["verdict"] == "unavailable", (unavailable, v)
            assert v["line"].startswith("gh unavailable: GitHub-dependent steps defer"), v["line"]
        no_token = {"returncode": 4, "stdout": "", "stderr": "To get started with GitHub CLI"}
        for refused in (
            no_token,
            answer(401, {"message": "Bad credentials"}),
            answer(403, {"message": "Sorry. Your account was suspended."}, budget(4000)),
        ):
            v = verdict(refused)
            assert v["exit"] == PREFLIGHT_UNAUTHENTICATED, (refused, v)
            assert v["line"].startswith("gh not authenticated (rest: "), v["line"]
        # The log keeps GitHub's first full sentence, never a fragment of one.
        assert v["line"] == (
            "gh not authenticated (rest: HTTP 403, not a rate limit: Sorry. Your account was suspended.)"
        ), v["line"]
        bad_gql = verdict(rest_ok, answer(401, {"message": "Bad credentials"}))
        assert bad_gql["exit"] == PREFLIGHT_UNAUTHENTICATED, bad_gql
        # Never a token in the line, whatever an error message carries.
        leaky = verdict(answer(401, {"message": "token gho_" + "a" * 36 + " is not valid"}))
        assert "gho_" not in leaky["line"] and "<redacted>" in leaky["line"], leaky["line"]
        # The real answers feed the ledger, tagged; the exempt probe's figure stays beside them.
        GH_LEDGER.write_text("")
        auth_preflight(runner=gh(rest_ok), now=now)
        assert _latest_ledger("core")["source"] == "preflight", _latest_ledger("core")
        assert _latest_ledger("graphql")["remaining"] == 4990, _latest_ledger("graphql")

        print(
            "gh_capacity.py selftest: OK (4-state per resource, read-time ledger, probe/gh_run "
            "header feed, pace/defer throttle, env-gated throttle_if_enabled, fail-open gate, "
            "auth preflight: rate-limited/unavailable defer, only refused credentials abort)"
        )
    finally:
        time.time = original_time
        GH_LEDGER = saved_ledger
        shutil.rmtree(tmp, ignore_errors=True)


def main(argv: list[str]) -> int:
    if "--selftest" in argv:
        _selftest()
        return 0
    if "--gate" in argv:
        i = argv.index("--gate")
        resource = argv[i + 1] if i + 1 < len(argv) else "core"
        return _gate(resource)
    if "--auth-preflight" in argv:
        result = auth_preflight()
        print(result["line"])
        return result["exit"]
    HANDOFF.mkdir(parents=True, exist_ok=True)
    snap = build()
    OUT.write_text(json.dumps(snap, indent=2) + "\n")
    print(json.dumps(snap, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
