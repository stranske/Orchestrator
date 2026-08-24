#!/usr/bin/env python3
"""capability_opportunity.py — for every capability: was it not useful, or never considered?

The Brain holds ~3,800 terminal outcomes and the capability ledger carried 7 links. That gap has
two very different explanations and they demand opposite responses:

  NEVER CONSIDERED — matching work ran repeatedly and the capability was never invoked. The trigger
                     is missing or unreachable. This is unfinished wiring; the fix is to wire it.
  NO MATCHING WORK — nothing in the history resembles what this capability handles. The fix (if any)
                     is a broader trigger, or accepting it is for work we simply have not done yet.
  DELIBERATELY OFF — a safety flag gates it. Not an oversight; flipping it is a separate decision.

Separating these is the whole point: "unused" is not a verdict, it is a question. This tool answers
it from the run history rather than from opinion, and never proposes retirement — an unused
capability is a capability we have not yet learned how to use.

    python3 capability_opportunity.py            # human-readable
    python3 capability_opportunity.py --json     # machine-readable
    python3 capability_opportunity.py --selftest # offline

Trigger kinds are read from each capability's declared `matcher`. Only the task_type shape can be
replayed against history exactly; the others are reported by kind with their real status, never
guessed at.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping

import capabilities
import feedback

# How a capability says it wants to be reached. Read from the declared matcher, never inferred.
TRIGGER_TASK_TYPE = "task_type"
TRIGGER_ROLE = "role"
TRIGGER_ENV_FLAG = "env_flag"
TRIGGER_INTERNAL = "internal_event"
TRIGGER_NONE = "none"

VERDICT_NEVER_CONSIDERED = "never_considered"
VERDICT_NO_MATCHING_WORK = "no_matching_work"
VERDICT_DELIBERATELY_OFF = "deliberately_off"
VERDICT_IN_USE = "in_use"
VERDICT_UNKNOWN = "unknown"


def classify_trigger(cap: dict) -> dict:
    """What kind of trigger this capability declares, and the detail needed to evaluate it."""
    matcher = cap.get("matcher") or {}
    if not matcher:
        return {"kind": TRIGGER_NONE, "detail": None}
    kind = str(matcher.get("kind") or "").lower()
    if kind == "role":
        return {"kind": TRIGGER_ROLE, "detail": matcher.get("equals")}
    if kind == "env":
        return {
            "kind": TRIGGER_ENV_FLAG,
            "detail": {"name": matcher.get("name"), "equals": matcher.get("equals")},
        }
    if kind:
        return {"kind": TRIGGER_INTERNAL, "detail": {"kind": kind, "name": matcher.get("name")}}
    if matcher.get("field") == "task_type" or "task_type" in matcher:
        values = matcher.get("value") or matcher.get("task_type")
        return {
            "kind": TRIGGER_TASK_TYPE,
            "detail": [values] if isinstance(values, str) else list(values or []),
        }
    return {"kind": TRIGGER_INTERNAL, "detail": dict(matcher)}


def _task_type_counts(conn=None) -> dict[str, int]:
    close = conn is None
    c = conn or feedback._conn()
    try:
        return {
            str(t): int(n)
            for t, n in c.execute("SELECT task_type, COUNT(*) FROM runs GROUP BY task_type")
        }
    finally:
        if close:
            c.close()


def _role_invocation_counts(conn=None) -> dict[str, int]:
    """How often each role actually ran, from run ids of the form `role:<name>:...`."""
    close = conn is None
    c = conn or feedback._conn()
    try:
        rows = c.execute(
            "SELECT task_type, COUNT(*) FROM runs WHERE task_type LIKE 'role:%' GROUP BY task_type"
        ).fetchall()
    finally:
        if close:
            c.close()
    return {str(t).split(":", 1)[1]: int(n) for t, n in rows if ":" in str(t)}


def assess(
    cap_id: str,
    cap: dict,
    *,
    task_counts: dict,
    role_counts: dict,
    env: Mapping[str, str] | None = None,
) -> dict:
    """One capability: its trigger, the work that matched it, and the resulting verdict."""
    env = os.environ if env is None else env
    trigger = classify_trigger(cap)
    invocations = int(bool(cap.get("last_invocation")))
    outcome_links = len(cap.get("outcome_links") or [])
    opportunities: int | None = None
    note = ""

    if trigger["kind"] == TRIGGER_TASK_TYPE:
        opportunities = sum(task_counts.get(t, 0) for t in (trigger["detail"] or []))
        note = f"history has {opportunities} run(s) of task_type {trigger['detail']}"
    elif trigger["kind"] == TRIGGER_ROLE:
        opportunities = role_counts.get(str(trigger["detail"]), 0)
        note = f"role ran {opportunities} time(s)"
    elif trigger["kind"] == TRIGGER_ENV_FLAG:
        name = (trigger["detail"] or {}).get("name")
        want = str((trigger["detail"] or {}).get("equals"))
        actual = env.get(str(name))
        note = f"{name}={actual!r}, trigger needs {want!r}"
    elif trigger["kind"] == TRIGGER_INTERNAL:
        note = f"internal trigger ({(trigger['detail'] or {}).get('kind') or 'custom'}) — not replayable from run history"
    else:
        note = "no matcher declared — nothing can route work here"

    # Verdict. Deliberately conservative: only claim "never considered" when matching work is
    # DEMONSTRATED in the history, and never claim a capability is useless.
    if trigger["kind"] == TRIGGER_ENV_FLAG:
        name = (trigger["detail"] or {}).get("name")
        want = str((trigger["detail"] or {}).get("equals"))
        verdict = VERDICT_IN_USE if str(env.get(str(name))) == want else VERDICT_DELIBERATELY_OFF
    elif outcome_links:
        verdict = VERDICT_IN_USE
    elif opportunities is None:
        verdict = VERDICT_UNKNOWN
    elif opportunities > 0:
        verdict = VERDICT_NEVER_CONSIDERED if not invocations else VERDICT_IN_USE
    else:
        verdict = VERDICT_NO_MATCHING_WORK

    return {
        "capability_id": cap_id,
        "status": cap.get("status"),
        "trigger_kind": trigger["kind"],
        "trigger_detail": trigger["detail"],
        "opportunities": opportunities,
        "ever_invoked": bool(invocations),
        "outcome_links": outcome_links,
        "verdict": verdict,
        "note": note,
    }


def report(*, path=None, env: Mapping[str, str] | None = None) -> dict:
    caps = capabilities.load(path or capabilities.REG)
    task_counts = _task_type_counts()
    role_counts = _role_invocation_counts()
    rows = [
        assess(cid, cap, task_counts=task_counts, role_counts=role_counts, env=env)
        for cid, cap in sorted(caps.items())
    ]
    by_verdict: dict[str, list[str]] = {}
    for row in rows:
        by_verdict.setdefault(row["verdict"], []).append(row["capability_id"])
    return {
        "total": len(rows),
        "by_verdict": {k: sorted(v) for k, v in sorted(by_verdict.items())},
        "total_runs_considered": sum(task_counts.values()),
        "rows": rows,
    }


def format_report(rep: dict) -> str:
    lines = [
        "# Capability opportunity — not useful, or never considered?",
        "",
        f"{rep['total']} capabilities assessed against {rep['total_runs_considered']} recorded runs.",
        "",
    ]
    labels = {
        VERDICT_NEVER_CONSIDERED: "NEVER CONSIDERED — matching work ran; capability never did",
        VERDICT_DELIBERATELY_OFF: "DELIBERATELY OFF — a safety flag gates it",
        VERDICT_NO_MATCHING_WORK: "NO MATCHING WORK — history holds nothing it handles",
        VERDICT_UNKNOWN: "UNKNOWN — trigger cannot be replayed from run history",
        VERDICT_IN_USE: "IN USE",
    }
    for verdict in (
        VERDICT_NEVER_CONSIDERED,
        VERDICT_DELIBERATELY_OFF,
        VERDICT_UNKNOWN,
        VERDICT_NO_MATCHING_WORK,
        VERDICT_IN_USE,
    ):
        names = rep["by_verdict"].get(verdict) or []
        lines.append(f"## {labels[verdict]}: {len(names)}")
        lines.extend(f"- {n}" for n in names)
        lines.append("")
    lines += [
        "| Capability | Trigger | Opportunities | Invoked | Links | Verdict | Note |",
        "|---|---|---:|:--:|---:|---|---|",
    ]
    for row in rep["rows"]:
        opp = "—" if row["opportunities"] is None else row["opportunities"]
        lines.append(
            f"| {row['capability_id']} | {row['trigger_kind']} | {opp} | "
            f"{'yes' if row['ever_invoked'] else 'no'} | {row['outcome_links']} | "
            f"{row['verdict']} | {row['note']} |"
        )
    return "\n".join(lines) + "\n"


def _selftest() -> None:
    tasks = {"maintenance": 12, "implement": 900, "consumer_sync_drift": 3}
    roles = {"triage": 478, "adjudicator": 0}

    def cap(**over):
        base = capabilities._blank_capability("x")
        base.update(over)
        return base

    # task_type trigger with real matching work + never invoked => NEVER CONSIDERED.
    a = assess(
        "a",
        cap(
            matcher={
                "field": "task_type",
                "operator": "in",
                "value": ["maintenance", "consumer_sync_drift"],
            }
        ),
        task_counts=tasks,
        role_counts=roles,
        env={},
    )
    assert a["trigger_kind"] == TRIGGER_TASK_TYPE and a["opportunities"] == 15, a
    assert a["verdict"] == VERDICT_NEVER_CONSIDERED, a

    # Same trigger but no such work in history => NO MATCHING WORK, not a failure to consider.
    b = assess(
        "b",
        cap(matcher={"field": "task_type", "operator": "in", "value": ["nonesuch"]}),
        task_counts=tasks,
        role_counts=roles,
        env={},
    )
    assert b["opportunities"] == 0 and b["verdict"] == VERDICT_NO_MATCHING_WORK, b

    # Env-gated: OFF is a deliberate decision, ON counts as in use.
    envcap = cap(matcher={"kind": "env", "name": "ORCH_X", "equals": "1"})
    off = assess("c", envcap, task_counts=tasks, role_counts=roles, env={})
    on = assess("c", envcap, task_counts=tasks, role_counts=roles, env={"ORCH_X": "1"})
    assert off["verdict"] == VERDICT_DELIBERATELY_OFF, off
    assert on["verdict"] == VERDICT_IN_USE, on

    # A role that ran many times but never linked an outcome is still "never considered" for
    # OUTCOME purposes only if it was never invoked; invocation counts as use.
    r = assess(
        "d",
        cap(matcher={"kind": "role", "equals": "triage"}, last_invocation=123),
        task_counts=tasks,
        role_counts=roles,
        env={},
    )
    assert r["opportunities"] == 478 and r["verdict"] == VERDICT_IN_USE, r
    r2 = assess(
        "e",
        cap(matcher={"kind": "role", "equals": "triage"}),
        task_counts=tasks,
        role_counts=roles,
        env={},
    )
    assert r2["verdict"] == VERDICT_NEVER_CONSIDERED, r2

    # No matcher at all => nothing can route work here; opportunities are UNKNOWN, never zero,
    # because "we never asked" must not be recorded as "there was no work".
    n = assess("f", cap(), task_counts=tasks, role_counts=roles, env={})
    assert n["trigger_kind"] == TRIGGER_NONE and n["opportunities"] is None, n
    assert n["verdict"] == VERDICT_UNKNOWN, n

    # No verdict may ever propose retirement.
    verdicts = {a["verdict"], b["verdict"], off["verdict"], r["verdict"], n["verdict"]}
    assert not any("retire" in v for v in verdicts), verdicts

    text = format_report(
        {
            "total": 1,
            "total_runs_considered": 5,
            "by_verdict": {VERDICT_NEVER_CONSIDERED: ["a"]},
            "rows": [a],
        }
    )
    assert "NEVER CONSIDERED" in text and "a" in text
    print(
        "capability_opportunity.py selftest: OK (task_type replay, env-gate, role, "
        "unknown-not-zero, never proposes retirement)"
    )


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)
    if args.selftest:
        _selftest()
        return 0
    rep = report()
    print(json.dumps(rep, indent=2) if args.json else format_report(rep), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
