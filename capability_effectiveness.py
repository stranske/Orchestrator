#!/usr/bin/env python3
"""capability_effectiveness.py — is using a capability actually making outcomes better?

The measurable question, at last. Everything upstream established that capabilities RUN
(heartbeats) and that their work is ATTRIBUTED (capability-tagged influence edges). This asks the
only question that justifies any of it: when a capability influenced a run, did the run turn out
better than when it did not?

WHY NOT "GATES LIFTING". Gate promotion is unreachable as a metric: of 14 gated capabilities, 3
carry an encoded threshold with a `requires` clause naming an observation the causal record cannot
supply (a rejected counterfactual, a role disagreement, an exploration-review recommendation), and
11 state their threshold as prose no code can check. `ready_to_lift` therefore stays 0 no matter how
good the evidence is. Effectiveness has to be measured directly.

THE MEASURE. `influence_edges` carries, per (capability, run): `accepted` (the capability's
contribution was taken up), `counterfactual` (it was considered and rejected — the natural control
arm), `outcome_verdict`, and `durability`. So:

    durable_rate(accepted)  vs  durable_rate(counterfactual)

is a real within-capability comparison rather than a cross-agent guess.

REFUSES TO MISLEAD. A rate computed from one or two observations is noise wearing a percentage sign,
and this subsystem has already produced several confident numbers that were artifacts. So every rate
carries its denominator, anything under MIN_SAMPLE reports `insufficient_evidence` instead of a
figure, and a capability with no terminal outcomes is reported as "not yet measurable" — never as 0%.

    python3 capability_effectiveness.py            # human-readable
    python3 capability_effectiveness.py --json
    python3 capability_effectiveness.py --selftest
"""
from __future__ import annotations

import argparse
import json
import sys

import capabilities
import feedback

# Below this many terminal outcomes, report the count and refuse the rate.
MIN_SAMPLE = 5


def _edge_rows(conn=None) -> list[dict]:
    close = conn is None
    c = conn or feedback._conn()
    try:
        # `target_event_id IS NOT NULL` is the SAME GUARD `capability_causal_evidence` already
        # applies (`feedback.py:1430`: consumed requires accepted AND target_event_id AND an
        # accepted validation status). An edge with no completion-event envelope can never be
        # causally consumed, so treating it as measurable evidence here made two consumers of one
        # table disagree about what counts.
        #
        # THE HARM, MEASURED 2026-08-21. 296 edges carry a NULL target_event_id — advisory role runs
        # (`role:triage:*`, `role:redirect:*`) are written to `runs` but never emit a completion
        # envelope, so `_latest_completion_event_id` returns None and the edge is inert by design.
        # Five of them carry `accepted=1, verdict=PASS, durability=durable`: the links created BY
        # HAND with the link-outcome CLI during the Stage-2 deadlock. They were 5 of `offload`'s 5
        # durable outcomes — i.e. its ENTIRE durable signal was hand-made and envelope-less, giving
        # `durable_rate: 0.059` off no causally-linked evidence at all. Nothing consumed that yet
        # only because the verdict was still `no_control_arm`; the moment offload gains a
        # counterfactual arm it becomes a lift computation. Fixed before that, not after.
        #
        # Filtered in SQL rather than in `_arm_stats` so `attributed` (the numerator's denominator)
        # excludes them too — counting an edge as attributed while refusing it as terminal would
        # report a capability as busier than its measurable evidence supports.
        rows = c.execute(
            """SELECT capability_id, accepted, counterfactual, outcome_verdict, durability,
                      target_run_id, created_ts
               FROM influence_edges
               WHERE capability_id IS NOT NULL AND target_event_id IS NOT NULL"""
        ).fetchall()
    finally:
        if close:
            c.close()
    return [
        {"capability_id": r[0], "accepted": bool(r[1]), "counterfactual": bool(r[2]),
         "verdict": r[3], "durability": r[4], "run_id": r[5], "ts": r[6]}
        for r in rows
    ]


def _target_of(run_id: str) -> str:
    """The work SUBJECT behind a run id, e.g. `remote:stranske/Workflows#2819:cursor` -> the issue.

    Run ids are per-attempt; the subject is what repeats. Used to stop N retries of one stuck issue
    counting as N independent observations.
    """
    parts = str(run_id or "").split(":")
    return parts[1] if len(parts) > 2 else str(run_id or "")


def _arm_stats(edges: list[dict]) -> dict:
    """Terminal-outcome counts and a durable rate, or a refusal when the sample is too small.

    COUNTS DISTINCT SUBJECTS, NOT ATTEMPTS. Repeated attempts on the same target are correlated, not
    independent evidence — the same rule the learning store already applies to research subjects. It
    matters concretely: on 2026-08-18 role-triage's first measurable sample was 77 terminal outcomes
    of which 51 were retries of just TWO stuck targets (`Workflows#2819` x33, `#2710` x18) left over
    from the scoped-blocker latch. Counting attempts gave a confident 0% durable rate that was really
    "the fleet kept retrying two blocked issues" — precisely the environment-noise-as-incapability
    error the learning rules forbid. Deduplicating by subject turns that into an honest
    `insufficient_evidence`.
    """
    terminal = [e for e in edges if e["verdict"] in ("PASS", "FAIL") or e["durability"] in
                ("durable", "abandoned", "reverted", "reopened")]
    durable = [e for e in terminal if e["durability"] == "durable"]
    # Distinct subjects — a target counts as durable if ANY attempt on it landed durably.
    durable_subjects = {_target_of(e["run_id"]) for e in durable}
    terminal_subjects = {_target_of(e["run_id"]) for e in terminal}
    out = {"attributed": len(edges), "terminal": len(terminal), "durable": len(durable),
           "terminal_subjects": len(terminal_subjects),
           "durable_subjects": len(durable_subjects),
           "attempts_per_subject": (round(len(terminal) / len(terminal_subjects), 2)
                                    if terminal_subjects else None)}
    if not terminal:
        out["durable_rate"] = None
        out["status"] = "not_yet_measurable"          # NOT 0% — nothing has resolved yet
    elif len(terminal_subjects) < MIN_SAMPLE:
        out["durable_rate"] = None
        out["status"] = (f"insufficient_evidence ({len(terminal_subjects)}/{MIN_SAMPLE} distinct "
                         f"subjects from {len(terminal)} attempts)")
    else:
        out["durable_rate"] = round(len(durable_subjects) / len(terminal_subjects), 3)
        out["status"] = "measured"
    return out


def measure(*, path=None, conn=None) -> dict:
    caps = capabilities.load(path or capabilities.REG)
    rows = _edge_rows(conn)
    by_cap: dict[str, list[dict]] = {}
    for row in rows:
        by_cap.setdefault(row["capability_id"], []).append(row)

    out = []
    for cap_id, cap in sorted(caps.items()):
        edges = by_cap.get(cap_id, [])
        accepted = [e for e in edges if e["accepted"] and not e["counterfactual"]]
        control = [e for e in edges if e["counterfactual"]]
        usage = capabilities.usage_rate(cap)
        entry = {
            "capability_id": cap_id,
            "status": cap.get("status"),
            "invocations_per_week": usage["invocations_per_week"],
            "attributed_edges": len(edges),
            "accepted": _arm_stats(accepted),
            "counterfactual": _arm_stats(control),
        }
        a, c = entry["accepted"], entry["counterfactual"]
        if a["durable_rate"] is not None and c["durable_rate"] is not None:
            entry["lift"] = round(a["durable_rate"] - c["durable_rate"], 3)
            entry["verdict"] = ("helps" if entry["lift"] > 0
                                else "hurts" if entry["lift"] < 0 else "neutral")
        else:
            entry["lift"] = None
            # No control arm is the COMMON case and must not read as a result.
            entry["verdict"] = ("no_control_arm" if a["durable_rate"] is not None
                                else a["status"])
        out.append(entry)

    attributed = [e for e in out if e["attributed_edges"]]
    return {
        "min_sample": MIN_SAMPLE,
        "capabilities": len(out),
        "with_attribution": len(attributed),
        "measured": [e["capability_id"] for e in out if e["verdict"] in
                     ("helps", "hurts", "neutral")],
        "awaiting_outcomes": [e["capability_id"] for e in attributed
                              if e["accepted"]["status"] == "not_yet_measurable"],
        "rows": out,
    }


def format_report(rep: dict) -> str:
    lines = [
        "# Capability effectiveness — does using it improve outcomes?", "",
        f"{rep['with_attribution']} of {rep['capabilities']} capabilities have attributed evidence; "
        f"{len(rep['measured'])} have enough of it to state a rate "
        f"(minimum {rep['min_sample']} terminal outcomes).", "",
    ]
    if not rep["with_attribution"]:
        lines += ["No capability has attributed evidence yet. Nothing to measure — this is the",
                  "honest state, not a zero score.", ""]
    lines += ["| Capability | Inv/wk | Edges | Accepted (dur/term) | Control | Lift | Verdict |",
              "|---|---:|---:|---|---|---:|---|"]
    for row in rep["rows"]:
        if not row["attributed_edges"]:
            continue
        a, c = row["accepted"], row["counterfactual"]
        arm = (f"{a['durable_subjects']}/{a['terminal_subjects']} subj "
               f"({a['terminal']} att)") if a["terminal"] else "—"
        ctl = (f"{c['durable_subjects']}/{c['terminal_subjects']} subj") if c["terminal"] else "—"
        lift = "—" if row["lift"] is None else f"{row['lift']:+.3f}"
        lines.append(f"| {row['capability_id']} | {row['invocations_per_week']} | "
                     f"{row['attributed_edges']} | {arm} | {ctl} | {lift} | {row['verdict']} |")
    if rep["awaiting_outcomes"]:
        lines += ["", "Awaiting outcome resolution (edges exist, nothing terminal yet): "
                  + ", ".join(rep["awaiting_outcomes"])]
    return "\n".join(lines) + "\n"


def _selftest() -> None:
    # A capability with a real sample on both arms yields a lift.
    edges = (
        [{"capability_id": "c1", "accepted": True, "counterfactual": False,
          "verdict": "PASS", "durability": "durable", "run_id": f"a{i}", "ts": 0} for i in range(4)]
        + [{"capability_id": "c1", "accepted": True, "counterfactual": False,
            "verdict": "FAIL", "durability": "abandoned", "run_id": "a9", "ts": 0}]
        + [{"capability_id": "c1", "accepted": False, "counterfactual": True,
            "verdict": "PASS", "durability": "durable", "run_id": f"b{i}", "ts": 0} for i in range(2)]
        + [{"capability_id": "c1", "accepted": False, "counterfactual": True,
            "verdict": "FAIL", "durability": "abandoned", "run_id": f"c{i}", "ts": 0} for i in range(3)]
    )
    a = _arm_stats([e for e in edges if e["accepted"]])
    assert a["terminal"] == 5 and a["durable"] == 4 and a["durable_rate"] == 0.8, a
    c = _arm_stats([e for e in edges if e["counterfactual"]])
    assert c["terminal"] == 5 and c["durable_rate"] == 0.4, c

    # AN EDGE WITH NO COMPLETION ENVELOPE IS NOT MEASURABLE EVIDENCE. `capability_causal_evidence`
    # already refuses to consume one (feedback.py:1430); this module must agree, or the same table
    # means two different things to two consumers. Regression for the live case: 5 hand-created
    # `link-outcome` edges targeting advisory `role:redirect:*` runs were 100% of `offload`'s
    # durable signal at a reported rate of 0.059, with no causal link behind any of them.
    import sqlite3 as _sqlite3
    _c = _sqlite3.connect(":memory:")
    _c.executescript(feedback.SCHEMA)
    feedback._migrate_schema(_c)
    for _eid, _tev in (("e-linked", "ev-1"), ("e-orphan", None)):
        _c.execute(
            "INSERT INTO influence_edges (edge_id,schema_version,influence_type,influence_id,"
            "source_run_id,target_event_id,target_run_id,capability_id,accepted,counterfactual,"
            "outcome_verdict,durability,created_ts,metadata_hash) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (_eid, 1, "capability", "offload", "src", _tev, f"remote:o/r#{_eid}:codex", "offload",
             1, 0, "PASS", "durable", 0, "h"),
        )
    _rows = _edge_rows(_c)
    _ids = {r["run_id"] for r in _rows}
    assert any("e-linked" in i for i in _ids), _ids
    # The orphan must be absent from `attributed` as well as from `terminal` — reporting a
    # capability as busy on evidence it cannot be measured by is the same lie one layer up.
    assert not any("e-orphan" in i for i in _ids), _ids
    assert _arm_stats(_rows)["attributed"] == 1, _arm_stats(_rows)
    _c.close()

    # CORRELATED ATTEMPTS MUST NOT INFLATE THE SAMPLE. 30 retries of one stuck target is one
    # observation, not 30 — the confound that produced a false 0% for role-triage on 2026-08-18.
    stuck = [{"capability_id": "c2", "accepted": True, "counterfactual": False,
              "verdict": "FAIL", "durability": "abandoned",
              "run_id": f"remote:o/r#2819:cursor:{i}", "ts": 0} for i in range(30)]
    s = _arm_stats(stuck)
    assert s["terminal"] == 30 and s["terminal_subjects"] == 1, s
    assert s["durable_rate"] is None, "30 attempts at ONE target must not yield a rate"
    assert "1/5 distinct subjects from 30 attempts" in s["status"], s["status"]
    assert s["attempts_per_subject"] == 30.0, s
    # Five DISTINCT targets do earn a rate.
    spread = [{"capability_id": "c3", "accepted": True, "counterfactual": False,
               "verdict": "FAIL", "durability": "abandoned",
               "run_id": f"remote:o/r#{900+i}:cursor:1", "ts": 0} for i in range(5)]
    sp = _arm_stats(spread)
    assert sp["terminal_subjects"] == 5 and sp["durable_rate"] == 0.0, sp
    assert sp["status"] == "measured", sp
    # A subject counts durable if ANY attempt on it landed durably.
    mixed = spread + [{"capability_id": "c3", "accepted": True, "counterfactual": False,
                       "verdict": "PASS", "durability": "durable",
                       "run_id": "remote:o/r#900:codex:2", "ts": 0}]
    mx = _arm_stats(mixed)
    assert mx["terminal_subjects"] == 5 and mx["durable_subjects"] == 1, mx
    assert mx["durable_rate"] == 0.2, mx
    assert _target_of("remote:stranske/Workflows#2819:cursor") == "stranske/Workflows#2819"
    assert _target_of("plain-run-id") == "plain-run-id"

    # REFUSALS: a small sample reports its denominator, never a rate.
    small = _arm_stats(edges[:2])
    assert small["durable_rate"] is None and "insufficient_evidence" in small["status"], small
    # No terminal outcome is "not yet measurable", NEVER 0%.
    pending = _arm_stats([{"capability_id": "c", "accepted": True, "counterfactual": False,
                           "verdict": None, "durability": None, "run_id": "p", "ts": 0}])
    assert pending["durable_rate"] is None and pending["status"] == "not_yet_measurable", pending
    assert pending["durable"] == 0 and pending["attributed"] == 1, pending

    # An accepted arm with no control must not be reported as a comparison.
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory(prefix="cap-eff-selftest-") as td:
        ledger = Path(td) / "capabilities.json"
        rec = capabilities._blank_capability("c1")
        rec["status"] = "shadow"
        capabilities.save({"c1": rec}, ledger)

        class _Cursor:
            def __init__(self, rows): self._rows = rows
            def fetchall(self): return self._rows

        class FakeConn:
            def execute(self, *_a, **_k):
                return _Cursor([(e["capability_id"], e["accepted"], e["counterfactual"],
                                 e["verdict"], e["durability"], e["run_id"], e["ts"])
                                for e in edges if e["accepted"]])
            def close(self): pass
        rep = measure(path=ledger, conn=FakeConn())
        row = rep["rows"][0]
        assert row["lift"] is None and row["verdict"] == "no_control_arm", row
        assert rep["measured"] == [], "no control arm is not a measured verdict"
        text = format_report(rep)
        assert "no_control_arm" in text

        # Empty evidence must say so rather than score zero.
        class EmptyConn:
            def execute(self, *_a, **_k): return _Cursor([])
            def close(self): pass
        empty = measure(path=ledger, conn=EmptyConn())
        assert empty["with_attribution"] == 0
        assert "not a zero score" in format_report(empty)

    print("capability_effectiveness.py selftest: OK (lift on real samples, refuses small samples, "
          "pending != 0%, no-control-arm is not a verdict)")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)
    if args.selftest:
        _selftest()
        return 0
    rep = measure()
    print(json.dumps(rep, indent=2) if args.json else format_report(rep), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
