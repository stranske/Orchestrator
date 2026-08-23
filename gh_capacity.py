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

Read-only and safe; fail-open everywhere (probe failure or no data => OK/proceed, never a false halt).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

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
    _append_ledger([_ratelimit_row_from_headers(headers, fallback_resource=resource)])
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
    out = {"generated_at": int(time.time()), "probe_ok": resources is not None, "resources": {}}
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
            slept.append(s)

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

        print(
            "gh_capacity.py selftest: OK (4-state per resource, read-time ledger, probe/gh_run "
            "header feed, pace/defer throttle, env-gated throttle_if_enabled, fail-open gate)"
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
    HANDOFF.mkdir(parents=True, exist_ok=True)
    snap = build()
    OUT.write_text(json.dumps(snap, indent=2) + "\n")
    print(json.dumps(snap, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
