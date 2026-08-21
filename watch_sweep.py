#!/usr/bin/env python3
"""watch_sweep.py — the missing caller for the stall-watcher.

WHY THIS EXISTS. `watch.py` is a finished, selftested, read-only stall classifier
(running / progress / stalled / exited / missing, with root-cause hints), and `claims.py`
deliberately records `pid`/`log`/`worktree`/`lane`/`task_type` on every live claim — its
`update_metadata` docstring says outright that the dispatcher patches those fields "so automatic
watch sweeps can find pid/log/worktree/lane/task_type without guessing".

That automatic sweep was never written. So the classifier had no caller, the `stall-watcher`
capability recorded nothing, and every stall in this fleet was found by hand instead:
the `opener_cap_pressure` latch that ran 78 days while real cap usage was 2 of 5; 24 of 30 scoped
blockers expired yet still blocking, the oldest by two months; the review lane silent from
2026-07-15. Each is exactly what this classifier reports.

STRICTLY READ-ONLY. It classifies and writes one artifact. It never kills a process, releases a
claim, relabels anything, or dispatches — the same contract `watch.py` itself holds. A stall is
surfaced, never acted on, so this can run every tick with no risk of double-dispatch.

NO NEW STATE. It reads live claims from `claims.active_claims` and reuses `watch.classify_lane`.
It does not keep its own registry of what is stalled; the artifact is a snapshot, and history lives
in the tick logs.

    python3 watch_sweep.py               # human-readable
    python3 watch_sweep.py --json
    python3 watch_sweep.py --selftest
"""
from __future__ import annotations

import argparse
import json
import sys

import claims
import watch

# Statuses that mean "something needs a human or a redirect", as opposed to healthy progress.
ATTENTION_STATUSES = ("stalled", "exited", "missing")


def _capability_heartbeat(event_type: str = "invocation") -> None:
    """Record that the stall-watcher ran, at the code path a driver actually enters.

    Lazy import + never raises + inert outside an active tick, matching the sibling modules. The
    heartbeat deliberately names `stall-watcher`: this module is the watcher's caller, and crediting
    it here is what makes the capability observable at all.
    """
    try:
        import capabilities
        capabilities.production_heartbeat("stall-watcher", event_type,
                                          ref="watch_sweep.sweep")
    except Exception:
        pass


def sweep(*, stale_seconds: int | None = None, claims_fn=None, classify_fn=None) -> dict:
    """Classify every live claim. Returns a snapshot; mutates nothing."""
    _capability_heartbeat()
    active = (claims_fn or claims.active_claims)(include_meta=True)
    classify = classify_fn or watch.classify_lane
    stale = watch.DEFAULT_STALE_SECONDS if stale_seconds is None else stale_seconds

    rows, errors = [], []
    for target, meta in sorted((active or {}).items()):
        meta = meta if isinstance(meta, dict) else {}
        try:
            report = classify(
                agent=str(meta.get("agent") or ""),
                target=str(target),
                lane=str(meta.get("lane") or ""),
                task_type=str(meta.get("task_type") or ""),
                pid=meta.get("pid"),
                log=str(meta.get("log") or ""),
                worktree=str(meta.get("worktree") or ""),
                base_ref=str(meta.get("base_ref") or ""),
                stale_seconds=stale,
            )
        except Exception as exc:                       # noqa: BLE001 — a bad claim must not stop the sweep
            errors.append({"target": target, "error": str(exc)[:160]})
            continue
        rows.append({"target": target, "agent": meta.get("agent"),
                     "status": report.get("status"), "hints": report.get("hints"),
                     "next_action": report.get("next_action")})

    attention = [r for r in rows if r.get("status") in ATTENTION_STATUSES]
    return {"claims": len(rows), "needs_attention": len(attention),
            "by_status": {s: sum(1 for r in rows if r.get("status") == s)
                          for s in sorted({str(r.get("status")) for r in rows})},
            "attention": attention, "rows": rows, "errors": errors}


def format_report(rep: dict) -> str:
    lines = ["# Stall sweep — live claims classified", "",
             f"  live claims:     {rep['claims']}",
             f"  needs attention: {rep['needs_attention']}", ""]
    if rep["claims"] == 0:
        lines += ["  No live claims. Nothing to classify — this is the idle state, not a failure.",
                  ""]
    if rep["by_status"]:
        lines.append("  by status: " + ", ".join(f"{k}={v}" for k, v in rep["by_status"].items()))
        lines.append("")
    for row in rep["attention"]:
        lines.append(f"  {str(row['status']).upper():<8} {row['target']}  agent={row['agent']}")
        if row.get("next_action"):
            lines.append(f"      next: {row['next_action']}")
        for hint in (row.get("hints") or [])[:3]:
            lines.append(f"      hint: {hint}")
    if rep["errors"]:
        lines += ["", "  errors (surfaced, never swallowed):"]
        lines += [f"    {e['target']}: {e['error']}" for e in rep["errors"]]
    return "\n".join(lines) + "\n"


def _selftest() -> None:
    # A stalled claim must be surfaced.
    fake_claims = {
        "o/r#1": {"agent": "codex", "lane": "opener", "task_type": "implement",
                  "pid": 123, "log": "/tmp/a.log", "worktree": "/tmp/w"},
        "o/r#2": {"agent": "gemini", "lane": "closer", "task_type": "implement",
                  "pid": 456, "log": "/tmp/b.log", "worktree": "/tmp/w2"},
    }
    statuses = {"o/r#1": "stalled", "o/r#2": "progress"}

    def fake_classify(**kw):
        target = kw["target"]
        return {"status": statuses[target], "hints": [f"hint for {target}"],
                "next_action": "redirect" if statuses[target] == "stalled" else "wait"}

    rep = sweep(claims_fn=lambda **_k: fake_claims, classify_fn=fake_classify)
    assert rep["claims"] == 2 and rep["needs_attention"] == 1, rep
    assert rep["attention"][0]["target"] == "o/r#1", rep["attention"]
    assert rep["by_status"] == {"progress": 1, "stalled": 1}, rep["by_status"]
    text = format_report(rep)
    assert "STALLED" in text and "o/r#1" in text and "next: redirect" in text

    # `exited` and `missing` also need attention — a dead delegate is a stall by another name.
    for status in ("exited", "missing"):
        statuses["o/r#2"] = status
        r = sweep(claims_fn=lambda **_k: fake_claims, classify_fn=fake_classify)
        assert r["needs_attention"] == 2, (status, r["needs_attention"])
    statuses["o/r#2"] = "running"
    assert sweep(claims_fn=lambda **_k: fake_claims,
                 classify_fn=fake_classify)["needs_attention"] == 1

    # ONE BAD CLAIM MUST NOT STOP THE SWEEP, and the error must be surfaced rather than swallowed.
    def exploding(**kw):
        if kw["target"] == "o/r#1":
            raise RuntimeError("bad claim metadata")
        return {"status": "progress", "hints": [], "next_action": "wait"}
    rep2 = sweep(claims_fn=lambda **_k: fake_claims, classify_fn=exploding)
    assert rep2["claims"] == 1 and len(rep2["errors"]) == 1, rep2
    assert "bad claim metadata" in rep2["errors"][0]["error"]
    assert "errors (surfaced" in format_report(rep2)

    # No live claims is the IDLE state, reported as such rather than as zero problems.
    empty = sweep(claims_fn=lambda **_k: {}, classify_fn=fake_classify)
    assert empty["claims"] == 0 and empty["needs_attention"] == 0
    assert "idle state, not a failure" in format_report(empty)

    # INTEGRATION: call the REAL classifier once, so a signature change in watch.py fails here
    # rather than silently producing an all-errors sweep in production. A fake classify_fn proves
    # the sweep logic and nothing about compatibility.
    real = sweep(claims_fn=lambda **_k: {
        "o/r#9": {"agent": "codex", "lane": "opener", "task_type": "implement",
                  "pid": None, "log": "", "worktree": "", "base_ref": ""}})
    assert not real["errors"], f"real classify_lane rejected the sweep's kwargs: {real['errors']}"
    assert real["claims"] == 1, real
    # A claim with no pid/log/worktree may legitimately be UNCLASSIFIABLE (status None) — the
    # classifier refusing to guess is correct behaviour, and must not be recorded as an error.
    # What matters is that the call SUCCEEDED, which is what catches a signature drift.
    assert real["rows"][0]["status"] in (None, "missing", "stalled", "exited", "running",
                                         "progress"), real

    print("watch_sweep.py selftest: OK (stalled/exited/missing surfaced, bad claim does not stop "
          "the sweep, idle reported as idle)")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--stale-seconds", type=int, default=None)
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)
    if args.selftest:
        _selftest()
        return 0
    rep = sweep(stale_seconds=args.stale_seconds)
    print(json.dumps(rep, indent=2) if args.json else format_report(rep), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
