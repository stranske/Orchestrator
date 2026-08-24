#!/usr/bin/env python3
"""claims.py — per-TARGET claim ledger for concurrent multi-agent dispatch.

WHY (see PR #2350 piece-2 bake-off): the legacy handoff per-LANE round mutex
(`.lane-<lane>.lock` in handoff-prerun.sh) serializes one round per lane. That is
too coarse for concurrent multi-agent dispatch, AND it cannot key on target — the
target is emergent DURING the round, not known at prerun time. Coarser keys (e.g.
`(lane,agent)`) would re-introduce the same-target dedup race the mutex exists to
prevent.

So the orchestrator assigns DISTINCT targets to concurrent agents and guards
same-target collision with an atomic per-target claim here.

INVARIANT: at most one in-flight round per TARGET (a repo, or `owner/repo#N`),
regardless of agent or lane. Different targets run concurrently. Mirrors the
proven mkdir-atomic + (pid-alive OR age<ttl) self-heal pattern from handoff.sh's
acquire_lock, re-keyed on target, in testable Python — but hardened against two
concurrency bugs a sequential test misses:
  - a freshly mkdir'd claim whose `meta` is not yet written must read as HELD
    (in-progress), not stale — else a racer rmtree's a live claim => 2 winners.
    Fix: no-meta dirs are held-by-dir-mtime until ttl.
  - stale TAKEOVER inside claim() is inherently racy (a 60-way concurrent
    selftest proved 2 winners: taker B renames out taker A's freshly-recreated
    LIVE claim). So claim() NEVER reaps — it is pure atomic mkdir. Staleness is
    handled by reap_stale(), which the orchestrator (a singleton planner) runs
    single-threaded at tick-start before claiming. Reaping is thus race-free by
    construction, and claim() stays trivially correct under concurrency.

`--selftest` runs fully offline in a temp dir (sequential + a real 60-way
multiprocess race) and never touches the live handoff state.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

CLAIM_TTL_DEFAULT = 1800  # seconds; matches the legacy LANE_MUTEX_MAX self-heal window
# item 13 (2026-07-08 audit): claims hardening against the two verified races.
# REAP_GRACE_SECONDS covers the router→dispatcher handoff window — the planner stamps its OWN pid,
# exits, and the dispatcher restamps the child pid moments later; a dead-pid claim younger than
# this is mid-handoff, not stale. REAP_LOCK_TTL_SECONDS bounds the reap mutex so a crashed reaper
# never wedges reaping forever.
REAP_GRACE_SECONDS = 600
REAP_LOCK_TTL_SECONDS = 120


def _handoff_dir() -> Path:
    return Path(os.environ.get("HANDOFF_DIR", Path.home() / ".codex" / "handoff"))


def _claims_dir() -> Path:
    return _handoff_dir() / "claims"


def _slug(target: str) -> str:
    """Filesystem-safe slug for 'stranske/Repo#123' -> 'stranske__repo_123'."""
    s = target.strip().lower().replace("/", "__")
    s = re.sub(r"[^a-z0-9_.-]+", "_", s)
    return s.strip("_") or "_"


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _meta_pids(meta: dict) -> list[int]:
    """Return the stamped process ids this claim depends on."""
    values: list[int] = []
    raw_many = meta.get("pids")
    if isinstance(raw_many, list):
        for value in raw_many:
            try:
                pid = int(value)
            except (TypeError, ValueError):
                continue
            if pid > 0:
                values.append(pid)
    try:
        pid = int(meta.get("pid", 0) or 0)
    except (TypeError, ValueError):
        pid = 0
    if pid > 0:
        values.append(pid)
    return values


def _read_meta(path: Path) -> dict | None:
    try:
        return json.loads((path / "meta").read_text())
    except Exception:
        return None


def _is_held(path: Path, ttl: int, now: float) -> bool:
    """Is this claim dir still held?

    With meta and pid(s): any stamped pid alive. Dead stamped processes are
                 stale immediately; otherwise a failed release can block real
                 work until the full TTL.
    With meta but no pid: younger than ttl.
    Without meta (claim created but not yet stamped, OR crashed pre-stamp):
                 held until the DIR's own mtime ages past ttl. This is what
                 closes the fresh-claim TOCTOU — a racer never rmtree's a live
                 in-progress claim.
    """
    meta = _read_meta(path)
    if meta is not None:
        pids = _meta_pids(meta)
        if pids:
            return any(_pid_alive(pid) for pid in pids)
        ts = float(meta.get("ts", 0) or 0)
        return (now - ts) < ttl
    try:
        return (now - path.stat().st_mtime) < ttl
    except OSError:
        return False


def claim(target: str, agent: str, *, ttl: int = CLAIM_TTL_DEFAULT, pid: int | None = None) -> bool:
    """Atomically claim `target` for `agent`. Returns True if held by us afterward.

    PURE atomic mkdir — no takeover (see module docstring): fresh -> True;
    same-agent re-claim (incl. recovering its OWN crashed claim) -> True
    (idempotent re-stamp); a dir held by ANOTHER agent -> False. Stale claims by
    other agents read as False here and are cleared by reap_stale() at the next
    tick-start — keeping claim() race-free under concurrent callers.
    """
    cdir = _claims_dir()
    cdir.mkdir(parents=True, exist_ok=True)
    path = cdir / _slug(target)
    pid = os.getpid() if pid is None else pid

    def _stamp() -> None:
        (path / "meta").write_text(
            json.dumps({"target": target, "agent": agent, "pid": pid, "ts": time.time()})
        )

    try:
        path.mkdir()  # atomic on POSIX — exactly one concurrent caller wins
        _stamp()
        return True
    except FileExistsError:
        pass

    meta = _read_meta(path)
    if meta is not None and meta.get("agent") == agent:
        # Same-agent re-claim is CRASH RECOVERY only (item 13): if a stamped pid other than ours
        # is still ALIVE, a concurrent same-agent planner already owns this target — returning
        # True here was the audited double-win race (two planners choosing the same deterministic
        # agent both believed they held it). Dead pids (or our own) restamp as before.
        live_others = [p for p in _meta_pids(meta) if p != pid and _pid_alive(p)]
        if live_others:
            return False
        _stamp()  # idempotent re-claim / self-recovery by the same agent
        return True
    return False  # held by another agent (or in-progress); stale ones are reaped, not stolen


def reap_stale(*, ttl: int = CLAIM_TTL_DEFAULT) -> list[str]:
    """Remove claim dirs no longer held; return the targets reaped (logged, not silent).

    MUST be called single-threaded — the orchestrator is the sole caller and runs it
    once at tick-start before planning, so the rmtree-then-claim sequence is race-free
    (unlike an in-claim takeover). Reaps: dead-pid+aged claims, unstamped dirs past ttl,
    and any leftover scratch.
    """
    cdir = _claims_dir()
    if not cdir.is_dir():
        return []
    now = time.time()
    # item 13: exclusive reap mutex — the single-threaded invariant above is documented but NOT
    # enforced by the callers (tick, router.plan, dispatcher.delegate, interactive runs all reap).
    # An atomic mkdir lock serializes them; a fresh lock means another reaper is mid-pass, and
    # reaping is idempotent housekeeping, so skipping this pass is always safe. Stale locks
    # (crashed reaper) are taken over after REAP_LOCK_TTL_SECONDS.
    lock = cdir / ".reap.lock"
    try:
        lock.mkdir()
    except FileExistsError:
        try:
            lock_age = now - lock.stat().st_mtime
        except OSError:
            lock_age = 0.0
        if lock_age < REAP_LOCK_TTL_SECONDS:
            return []
        shutil.rmtree(lock, ignore_errors=True)
        try:
            lock.mkdir()
        except FileExistsError:
            return []  # lost the takeover race — the winner reaps
    try:
        reaped: list[str] = []
        for path in list(cdir.iterdir()):
            if not path.is_dir() or path.name == ".reap.lock":
                continue
            if _is_held(path, ttl, now):
                continue
            meta = _read_meta(path)
            # item 13 grace: never reap a YOUNG claim even with a dead stamped pid — that is the
            # router→dispatcher handoff window, not staleness.
            age_ref = float((meta or {}).get("ts", 0) or 0)
            if not age_ref:
                try:
                    age_ref = path.stat().st_mtime
                except OSError:
                    age_ref = 0.0
            if age_ref and now - age_ref < REAP_GRACE_SECONDS:
                continue
            reaped.append(meta["target"] if meta and "target" in meta else path.name)
            shutil.rmtree(path, ignore_errors=True)
        return reaped
    finally:
        shutil.rmtree(lock, ignore_errors=True)


def release(target: str, agent: str | None = None) -> bool:
    """Release a claim. If `agent` is given, only release a claim that agent holds."""
    path = _claims_dir() / _slug(target)
    if not path.exists():
        return False
    if agent is not None:
        meta = _read_meta(path)
        if meta is not None and meta.get("agent") not in (agent, None):
            return False
    shutil.rmtree(path, ignore_errors=True)
    return True


def update_metadata(
    target: str, agent: str | None = None, *, refresh_ts: bool = False, **fields
) -> bool:
    """Patch metadata for a live claim held by agent.

    Dispatcher uses this after spawning the real child process so automatic watch sweeps can find
    pid/log/worktree/lane/task_type without guessing. This never creates or steals a claim.
    """
    path = _claims_dir() / _slug(target)
    if not path.exists():
        return False
    meta = _read_meta(path)
    if meta is None:
        return False
    if agent is not None and meta.get("agent") not in (agent, None):
        return False
    for key, value in fields.items():
        if value is not None:
            meta[key] = value
    if refresh_ts:
        meta["ts"] = time.time()
    meta["updated_ts"] = time.time()
    (path / "meta").write_text(json.dumps(meta))
    return True


def holder(target: str, *, ttl: int = CLAIM_TTL_DEFAULT) -> dict | None:
    """Live holder meta for `target`, or None if free/stale."""
    path = _claims_dir() / _slug(target)
    if not path.exists() or not _is_held(path, ttl, time.time()):
        return None
    return _read_meta(path)


def active_claims(*, ttl: int = CLAIM_TTL_DEFAULT, include_meta: bool = False) -> dict:
    """All live, stamped claims: {target: {agent, age_s}}. For the planner + dashboard."""
    cdir = _claims_dir()
    out: dict = {}
    if not cdir.is_dir():
        return out
    now = time.time()
    for path in cdir.iterdir():
        if not path.is_dir() or ".dead." in path.name:
            continue
        if not _is_held(path, ttl, now):
            continue
        meta = _read_meta(path)
        if meta:  # skip unstamped in-progress dirs (target not recoverable from slug)
            base = dict(meta) if include_meta else {"agent": meta.get("agent")}
            base["age_s"] = int(now - float(meta.get("ts", now)))
            out[meta["target"]] = base
    return out


# --- test helpers (module-level so multiprocessing 'fork' workers can call them) ------
def _race_claim(args: tuple) -> bool:
    target, idx = args
    return claim(target, f"agent-{idx}")


def _concurrent_tests() -> None:
    import multiprocessing as mp

    ctx = mp.get_context("fork")  # fork inherits os.environ[HANDOFF_DIR]; no threads here
    N = 60

    # same target, N distinct agents racing -> EXACTLY ONE winner
    with ctx.Pool(24) as p:
        res = p.map(_race_claim, [("race/Same#1", i) for i in range(N)])
    assert sum(res) == 1, f"same-target race: expected 1 winner, got {sum(res)}"

    # N distinct targets -> ALL win (concurrency works)
    with ctx.Pool(24) as p:
        res = p.map(_race_claim, [(f"race/Distinct#{i}", i) for i in range(N)])
    assert sum(res) == N, f"distinct-target race: expected {N} winners, got {sum(res)}"

    # concurrent on a STALE target: claim() must NOT steal it -> 0 winners (race-free;
    # the pre-reap-split bug showed 2). The single-threaded reaper then clears it.
    stale = _claims_dir() / _slug("race/Stale#1")
    stale.mkdir(parents=True, exist_ok=True)
    (stale / "meta").write_text(
        json.dumps(
            {
                "target": "race/Stale#1",
                "agent": "dead",
                "pid": 2147480000,
                "ts": time.time() - 99999,
            }
        )
    )
    with ctx.Pool(24) as p:
        res = p.map(_race_claim, [("race/Stale#1", i) for i in range(N)])
    assert sum(res) == 0, f"stale target: claim() must not steal, got {sum(res)} winners"
    assert "race/Stale#1" in reap_stale(), "reaper must clear the stale claim"
    assert claim("race/Stale#1", "claude") is True, "claimable after single-threaded reap"


def _selftest() -> None:
    import tempfile

    tmp = tempfile.mkdtemp(prefix="claims-selftest-")
    os.environ["HANDOFF_DIR"] = tmp
    try:
        T1, T2, T3 = "stranske/Repo#1", "stranske/Repo#2", "stranske/Other#9"

        assert claim(T1, "codex") is True
        assert claim(T1, "claude") is False, "same-target collision must be blocked"
        assert claim(T2, "claude") is True
        assert claim(T1, "codex") is True, "idempotent same-agent re-claim"
        held = holder(T1)
        assert held and held["agent"] == "codex"
        assert set(active_claims()) == {T1, T2}, active_claims()
        assert (
            update_metadata(T1, "codex", lane="opener", task_type="implement", pid=os.getpid())
            is True
        )
        meta_claims = active_claims(include_meta=True)
        assert (
            meta_claims[T1]["lane"] == "opener" and meta_claims[T1]["task_type"] == "implement"
        ), meta_claims
        public_claim = active_claims()[T1]
        assert public_claim["agent"] == "codex" and "lane" not in public_claim, active_claims()
        assert update_metadata(T1, "claude", lane="closer") is False
        assert release(T1, "codex") is True
        assert holder(T1) is None
        assert claim(T1, "claude") is True
        assert release(T1, "codex") is False, "wrong-agent release must be refused"
        held = holder(T1)
        assert held and held["agent"] == "claude"

        # stale claim owned by 'codex' (dead pid, old ts)
        stale = _claims_dir() / _slug(T3)
        stale.mkdir(parents=True)
        (stale / "meta").write_text(
            json.dumps(
                {"target": T3, "agent": "codex", "pid": 2147480000, "ts": time.time() - 99999}
            )
        )
        assert holder(T3) is None, "dead+old claim reads as free"
        assert claim(T3, "claude") is False, "claim() must NOT steal another agent's stale claim"
        assert claim(T3, "codex") is True, "same agent self-recovers its own stale claim"
        # foreign stale claim is cleared by the reaper, not by claim()
        release(T3)
        stale.mkdir(parents=True)
        (stale / "meta").write_text(
            json.dumps(
                {"target": T3, "agent": "codex", "pid": 2147480000, "ts": time.time() - 99999}
            )
        )
        assert T3 in reap_stale(), "reaper clears a foreign stale claim"
        assert claim(T3, "claude") is True, "claimable after reap"

        release(T3)
        stale.mkdir(parents=True)
        (stale / "meta").write_text(
            json.dumps({"target": T3, "agent": "codex", "pid": 2147480000, "ts": time.time()})
        )
        assert holder(T3) is None, "dead stamped pid must be stale even before ttl"
        # item 13: a YOUNG dead-pid claim is the router→dispatcher handoff window (the planner's
        # pid died before the dispatcher restamped the child) — the reaper grants
        # REAP_GRACE_SECONDS before clearing it (the audited premature-reap race).
        assert T3 not in reap_stale(), "grace protects the planner→dispatcher pid-stamp handoff"
        (stale / "meta").write_text(
            json.dumps(
                {
                    "target": T3,
                    "agent": "codex",
                    "pid": 2147480000,
                    "ts": time.time() - (REAP_GRACE_SECONDS + 5),
                }
            )
        )
        assert T3 in reap_stale(), "reaper clears dead stamped pid once past the grace window"

        # item 13: same-agent DOUBLE-claim — a LIVE stamped pid other than ours must block the
        # second win (two concurrent planners choosing the same agent both returned True before).
        release(T3)
        assert claim(T3, "codex", pid=os.getppid()) is True  # concurrent planner, alive
        assert (
            claim(T3, "codex", pid=os.getpid()) is False
        ), "live same-agent claim must not double-win"
        release(T3)
        assert claim(T3, "codex", pid=2147480000) is True  # crashed planner (dead pid)
        assert claim(T3, "codex", pid=os.getpid()) is True, "dead-pid self-recovery preserved"

        # item 13: reap mutex — a FRESH lock means another reaper is mid-pass (skip; reaping is
        # idempotent housekeeping); a STALE lock (crashed reaper) is taken over.
        release(T3)
        stale.mkdir(parents=True)
        (stale / "meta").write_text(
            json.dumps(
                {"target": T3, "agent": "codex", "pid": 2147480000, "ts": time.time() - 99999}
            )
        )
        reap_lock = Path(tmp) / "claims" / ".reap.lock"
        reap_lock.mkdir()
        assert reap_stale() == [], "fresh reap lock skips the pass"
        lock_old = time.time() - (REAP_LOCK_TTL_SECONDS + 5)
        os.utime(reap_lock, (lock_old, lock_old))
        assert T3 in reap_stale(), "stale reap lock is taken over"

        release(T3)
        stale.mkdir(parents=True)
        (stale / "meta").write_text(
            json.dumps(
                {
                    "target": T3,
                    "agent": "research",
                    "pids": [os.getpid(), 2147480000],
                    "ts": time.time(),
                }
            )
        )
        held = holder(T3)
        assert (
            held and held["agent"] == "research"
        ), "any live child pid keeps a research claim held"
        release(T3)

        # no-meta TOCTOU guard: a fresh (unstamped) dir reads as HELD, not stale
        nm = _claims_dir() / _slug("nometa/T")
        nm.mkdir(parents=True)  # mkdir'd but never stamped
        assert (
            _is_held(nm, CLAIM_TTL_DEFAULT, time.time()) is True
        ), "unstamped fresh dir must be held"

        _concurrent_tests()
        print(
            "claims.py selftest: OK (atomic per-target claim; same-target block; "
            "idempotent re-claim/self-recovery; agent-guarded release; single-threaded "
            "reap_stale; race-free under 60-way concurrency incl. stale targets)"
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        os.environ.pop("HANDOFF_DIR", None)


def main(argv: list[str]) -> int:
    if "--selftest" in argv:
        _selftest()
        return 0
    if argv and argv[0] == "claim":
        ok = claim(argv[1], argv[2])
        print(json.dumps({"claimed": ok, "target": argv[1], "agent": argv[2]}))
        return 0 if ok else 1
    if argv and argv[0] == "release":  # used by the dispatcher's detached exit-wrapper
        agent = argv[2] if len(argv) > 2 else None
        ok = release(argv[1], agent)
        print(json.dumps({"released": ok, "target": argv[1]}))
        return 0 if ok else 1
    if argv and argv[0] == "reap":
        print(json.dumps({"reaped": reap_stale()}))
        return 0
    print(json.dumps({"active_claims": active_claims()}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
