#!/usr/bin/env python3
"""capability_propensity.py — does triggering a capability actually help, and should we do it more?

THE OPEN LOOP THIS CLOSES. `capability_advisor` names candidate capabilities for a task and records
a `match` against each. Nothing recorded what happened NEXT: whether the candidate was actually
triggered, and whether triggering it helped. So the front door could learn WHICH capabilities match
WHICH work, but never which ones were WORTH matching — and "recommend the useful ones more often"
had no signal to stand on.

DEDUP FINDING (2026-08-22, before writing this). Checked `feedback.py` (outcomes, route_weights,
influence_edges, record_capability_consumption, capability_causal_evidence), `exp_abcd.py`,
`exploration_evidence_plan.py`, `exploration_backfill.py`, `range_lane_rollout.py`,
`capability_activation_audit.py`, `capability_firing_monitor.py`, `CAPABILITY_USEFULNESS.md`.
Findings, and why this is new but small:

* `feedback.route_weights` learns AGENT choice per task_type. There is no analogue for capability
  choice. Different decision, same shape.
* `feedback.record_capability_consumption` IS the rigorous producer->consumer edge, but it demands an
  immutable `capability_version_id` and completion events on both runs. Per the advisor's own
  docstring 0 of the capabilities carry version lineage, so that path cannot carry this signal today
  without fabricating identity — which the learning-loop rules forbid outright.
* `CAPABILITY_USEFULNESS.md` already answers "is this capability useful" — by HAND, once, dated
  2026-08-19, over six named corpora. It is a static judgment, not a loop. This module is the
  continuous version of that same question, and does not replace the analysis.
* NO NEW EVENT TYPES AND NO NEW STORE. `capabilities.EVENT_FIELDS` already has `match`,
  `invocation` and `outcome`; the advisor already writes `match` with `ref="advice:<digest>"`. This
  module writes the other two against the SAME ref and reads all three back. So the natural
  experiment is assembled from the store that already exists.

THE NATURAL EXPERIMENT, and why it is natural. Every advisory call produces a candidate SET under
one `advice:<digest>`. In the ordinary course of work some candidates get triggered and some do not,
for reasons that have nothing to do with this module. That is the experiment: same task, same
context, same candidate set, divergent treatment. Comparing outcomes within a digest needs no
randomisation and no extra work from anyone — it only needs the two missing edges to be recorded.

LATCHED-GATE DISCIPLINE (the failure mode this repo commits most). A propensity that rises with
measured usefulness is a gate whose clear path could trivially be blocked by the thing it measures:
never triggered -> no usefulness evidence -> low propensity -> never triggered. The three answers,
in writing, because a gate that cannot answer them is not ready:

  1. WHAT DECREMENTS IT? `EXPLORATION_FLOOR`. Every capability keeps a non-zero recommendation
     probability regardless of evidence, so evidence can always be acquired. Not "time passes".
  2. CAN THE DRAIN RUN WHILE THE GATE IS CLOSED? Yes, and that is the whole point: the floor applies
     hardest to capabilities with the LEAST evidence, so the population that most needs sampling is
     the population most likely to be sampled.
  3. DOES THE MEASURING WINDOW EQUAL THE DRAINING WINDOW? Yes, by construction: `WINDOW_DAYS` is
     defined once here and consumed by both `usefulness()` and `propensity()`. One constant, so it
     cannot drift into permanent debt.

And the runtime rule: `propensity()` reports its blocking quantity (`evidence_count`) and its
drainable quantity (`explorable`) in the SAME dict, so "0.05" can never read as "be patient" when it
should read "nothing can ever clear this".

WHAT THIS DELIBERATELY DOES NOT DO. It does not dispatch, does not trigger anything itself, and does
not write to the Brain. It ranks advice. Triggering stays with the caller that already had the
authority to trigger, so a bad propensity can misorder a recommendation list and nothing else.

    python3 capability_propensity.py report
    python3 capability_propensity.py experiments
    python3 capability_propensity.py --json report
    python3 capability_propensity.py --selftest
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys

import capabilities
import env_prereq

# KILL SWITCH. Off means the advisor stops ranking by propensity and falls back to its previous
# order; the recording edges still work, so turning this off never destroys evidence -- it only
# stops the evidence from steering anything.
DISABLED = os.environ.get("ORCH_CAPABILITY_PROPENSITY_DISABLED", "").strip() == "1"

# ONE constant, consumed by both the measurement and the drain. A matching pair of literals would
# drift; a shared name cannot.
WINDOW_DAYS = 90
# No capability's recommendation probability may reach zero, or it can never earn evidence again.
EXPLORATION_FLOOR = 0.05
# Beta(1,1) prior: never-triggered sits at 0.5 rather than 0, so an untried capability is optimistic
# under uncertainty instead of buried by it.
PRIOR_USEFUL, PRIOR_TOTAL = 1.0, 2.0
# An outcome heartbeat carries the verdict in metadata under this key.
USEFUL_KEY = "useful"
ADVICE_REF_PREFIX = "advice:"


def _events(cap: dict) -> list[dict]:
    return list(cap.get("event_history") or [])


def _experiment_id(event: dict) -> str | None:
    """The advisory digest this event belongs to, or None if it is not experiment-linked."""
    ref = str(event.get("ref") or "")
    return ref if ref.startswith(ADVICE_REF_PREFIX) else None


def _within_window(event: dict, *, now: int, window_days: int) -> bool:
    ts = event.get("timestamp")
    if not isinstance(ts, (int, float)):
        return False
    return (now - float(ts)) <= window_days * 86400


def experiments(*, path=None, window_days: int = WINDOW_DAYS, now: int | None = None) -> list[dict]:
    """Assemble every natural experiment: one candidate set, who was triggered, what came of it.

    Reads the ledger with `load_declared` — a WRITING load from verification code is how this repo
    once mutated the live ledger while claiming to inspect it.
    """
    caps = capabilities.load_declared(path or capabilities.REG)
    now = capabilities._now() if now is None else now
    trials: dict[str, dict] = {}
    for cap_id, cap in sorted(caps.items()):
        for event in _events(cap):
            exp = _experiment_id(event)
            if not exp or not _within_window(event, now=now, window_days=window_days):
                continue
            trial = trials.setdefault(exp, {"experiment_id": exp, "candidates": [], "triggered": [],
                                            "useful": [], "not_useful": [], "skills": set()})
            meta = event.get("metadata") or {}
            if meta.get("skill"):
                trial["skills"].add(str(meta["skill"]))
            etype = event.get("type") or event.get("event_type")
            if etype == "match" and cap_id not in trial["candidates"]:
                trial["candidates"].append(cap_id)
            elif etype == "invocation" and cap_id not in trial["triggered"]:
                trial["triggered"].append(cap_id)
            elif etype == "outcome":
                bucket = "useful" if meta.get(USEFUL_KEY) is True else "not_useful"
                if cap_id not in trial[bucket]:
                    trial[bucket].append(cap_id)
    out = []
    for trial in trials.values():
        trial["skills"] = sorted(trial["skills"])
        # The CONTROL ARM is what makes this an experiment rather than a tally: candidates that were
        # named for this exact task and NOT triggered. Reporting it is not optional -- an experiment
        # with an unreported control arm is a testimonial.
        trial["not_triggered"] = sorted(set(trial["candidates"]) - set(trial["triggered"]))
        trial["resolved"] = bool(trial["useful"] or trial["not_useful"])
        out.append(trial)
    return sorted(out, key=lambda t: t["experiment_id"])


def usefulness(*, path=None, window_days: int = WINDOW_DAYS, now: int | None = None) -> dict:
    """Per capability: how often named, how often triggered, how often it helped.

    Every rate travels with its denominator. A bare "80% useful" over 5 trials has burned this
    project before under a different name.
    """
    caps = capabilities.load_declared(path or capabilities.REG)
    rows: dict[str, dict] = {
        cap_id: {"capability_id": cap_id, "candidates": 0, "triggered": 0,
                 "useful": 0, "not_useful": 0, "status": cap.get("status")}
        for cap_id, cap in sorted(caps.items())
    }
    for trial in experiments(path=path, window_days=window_days, now=now):
        for cap_id in trial["candidates"]:
            if cap_id in rows:
                rows[cap_id]["candidates"] += 1
        for cap_id in trial["triggered"]:
            if cap_id in rows:
                rows[cap_id]["triggered"] += 1
        for key in ("useful", "not_useful"):
            for cap_id in trial[key]:
                if cap_id in rows:
                    rows[cap_id][key] += 1
    for row in rows.values():
        resolved = row["useful"] + row["not_useful"]
        row["resolved"] = resolved
        row["trigger_rate"] = (row["triggered"] / row["candidates"]) if row["candidates"] else None
        row["usefulness_rate"] = (row["useful"] / resolved) if resolved else None
    return {"window_days": window_days, "capability_count": len(rows),
            "rows": {k: v for k, v in sorted(rows.items())}}


def propensity(capability_id: str, *, path=None, window_days: int = WINDOW_DAYS,
               now: int | None = None) -> dict:
    """How strongly should this capability be recommended when it matches? With BOTH quantities.

    Posterior mean of a Beta(1,1)-Bernoulli over resolved outcomes, floored so evidence can always
    be acquired. `evidence_count` is the blocking quantity and `explorable` the drainable one, in
    one dict, because "0.05" alone reads as patience when it may mean deadlock.
    """
    stats = usefulness(path=path, window_days=window_days, now=now)["rows"]
    row = stats.get(capability_id)
    if row is None:
        raise ValueError(f"unknown capability: {capability_id}")
    resolved = row["resolved"]
    posterior = (row["useful"] + PRIOR_USEFUL) / (resolved + PRIOR_TOTAL)
    value = max(EXPLORATION_FLOOR, posterior)
    return {
        "capability_id": capability_id,
        "propensity": round(value, 4),
        "posterior_mean": round(posterior, 4),
        "floored": value > posterior,
        # BLOCKING quantity and DRAINABLE quantity, together, always.
        "evidence_count": resolved,
        # DERIVED, never asserted. This field was a hardcoded True until a break-test removed the
        # floor and it still claimed the gate was drainable -- a predicate that cannot fail is
        # decoration, and decoration is exactly what this repo's prose rules turned out to be.
        "explorable": value >= EXPLORATION_FLOOR,
        "exploration_floor": EXPLORATION_FLOOR,
        "basis": ("no resolved outcomes yet — optimistic prior plus an unconditional floor, so this "
                  "can still be sampled and can therefore still earn evidence"
                  if not resolved else
                  f"{row['useful']} of {resolved} resolved trials were useful"),
        "window_days": window_days,
    }


def rank(entries: list[dict], *, path=None, window_days: int = WINDOW_DAYS) -> list[dict]:
    """Annotate advisory candidates with propensity and order them by it. THE PRODUCTION PATH.

    One call per advisory question rather than one per candidate, so the heartbeat credits the
    decision that was actually made and the ledger does not accrue N events for one question.

    ORDER ONLY. The candidate SET is never changed, so the worst a wrong propensity can do is put a
    good suggestion second. That containment is deliberate: this module ranks advice, it does not
    decide what runs.
    """
    if DISABLED or not entries:
        return entries
    scored = []
    for entry in entries:
        prop = propensity(entry["capability_id"], path=path, window_days=window_days)
        entry["propensity"] = prop["propensity"]
        entry["propensity_basis"] = prop["basis"]
        entry["usefulness_evidence_count"] = prop["evidence_count"]
        entry["propensity_floored"] = prop["floored"]
        scored.append(entry)
    scored.sort(key=lambda e: (-e["propensity"], e["capability_id"]))
    # Credited on the executed path, not only from the CLI: a capability whose heartbeat sits behind
    # a manual command reads as dormant no matter how often production uses it.
    _capability_heartbeat("invocation", f"rank:{len(scored)}")
    with_evidence = sum(1 for e in scored if e["usefulness_evidence_count"])
    # BOTH quantities in one place: how many of these rankings rest on measurement, and how many on
    # the prior. A ranked list that does not say which is which invites being trusted too early.
    _capability_heartbeat("output", f"rank:evidence:{with_evidence}/{len(scored)}")
    return scored


def _capability_heartbeat(event_type: str, ref: str) -> None:
    """This capability's own production heartbeat. Absent one, it cannot accrue evidence of its own
    usefulness -- the exact defect `issue-readiness` and `switch-review` both shipped with."""
    try:
        capabilities.production_heartbeat("capability-propensity", event_type, ref=ref)
    except Exception:                                              # noqa: BLE001
        pass


def report(*, path=None, window_days: int = WINDOW_DAYS, now: int | None = None) -> dict:
    """The whole denominator, ranked, with the unresolved population named rather than dropped."""
    stats = usefulness(path=path, window_days=window_days, now=now)
    trials = experiments(path=path, window_days=window_days, now=now)
    ranked = []
    for cap_id, row in stats["rows"].items():
        prop = propensity(cap_id, path=path, window_days=window_days, now=now)
        ranked.append({**row, "propensity": prop["propensity"],
                       "floored": prop["floored"], "basis": prop["basis"]})
    ranked.sort(key=lambda r: (-r["propensity"], -r["resolved"], r["capability_id"]))
    resolved_caps = [r["capability_id"] for r in ranked if r["resolved"]]
    return {
        "window_days": window_days,
        "capability_count": stats["capability_count"],
        "experiment_count": len(trials),
        "resolved_experiment_count": sum(1 for t in trials if t["resolved"]),
        # THE HONEST HEADLINE. If this is 0 the loop is not learning yet, and every propensity below
        # is the prior rather than a measurement. Saying so is the difference between this and a
        # dashboard that looks informative while reporting nothing.
        "capabilities_with_evidence": len(resolved_caps),
        "capabilities_without_evidence": stats["capability_count"] - len(resolved_caps),
        "ranked": ranked,
        "experiments": trials,
    }


# --------------------------------------------------------------------------- recording the two
# missing edges. Thin on purpose: the advisor already writes `match`.

def record_trigger(capability_id: str, experiment_id: str, *, path=None,
                   metadata: dict | None = None) -> bool:
    """This candidate was actually triggered. Idempotent per (capability, experiment)."""
    if not experiment_id.startswith(ADVICE_REF_PREFIX):
        raise ValueError(f"experiment_id must start with {ADVICE_REF_PREFIX!r}: {experiment_id!r}")
    return capabilities.heartbeat(
        capability_id, "invocation", ref=experiment_id, path=path or capabilities.REG,
        idempotency_key=f"trigger:{capability_id}:{experiment_id}",
        metadata={"source": "capability_propensity", **(metadata or {})})


def record_usefulness(capability_id: str, experiment_id: str, *, useful: bool, evidence: str,
                      path=None) -> bool:
    """Did triggering it help? `evidence` is required: an unevidenced verdict is an opinion.

    The verdict must describe what the capability CHANGED, not that it ran. "It fired" is the
    un-gameable-label failure this project's learning rules exist to prevent.
    """
    if not str(evidence).strip():
        raise ValueError("a usefulness verdict requires evidence naming what changed")
    if not experiment_id.startswith(ADVICE_REF_PREFIX):
        raise ValueError(f"experiment_id must start with {ADVICE_REF_PREFIX!r}: {experiment_id!r}")
    return capabilities.heartbeat(
        capability_id, "outcome", ref=experiment_id, path=path or capabilities.REG,
        idempotency_key=f"useful:{capability_id}:{experiment_id}",
        metadata={"source": "capability_propensity", USEFUL_KEY: bool(useful),
                  "evidence": str(evidence)[:400]})


def _selftest() -> None:
    import tempfile
    from pathlib import Path

    # Synthetic ledger throughout: this must assert the MECHANISM on any machine, not this
    # instance's ledger. A reach test asserted against the live ledger earlier today and passed
    # locally while failing CI, which is the same mistake one module over.
    with tempfile.TemporaryDirectory(prefix="propensity-selftest-") as td:
        ledger = Path(td) / "capabilities.json"
        rows = {}
        for cid in ("helper", "dud", "never-tried"):
            cap = capabilities._blank_capability(cid)
            cap["status"] = "generated"
            cap["matcher"] = {"field": "task_type", "operator": "in", "value": ["testgen"]}
            rows[cid] = cap
        capabilities.save(rows, ledger)

        exp = "advice:deadbeef1234"
        for cid in ("helper", "dud", "never-tried"):
            capabilities.heartbeat(cid, "match", ref=exp, path=ledger,
                                   idempotency_key=f"m:{cid}", metadata={"skill": "repo-audit"})
        record_trigger("helper", exp, path=ledger)
        record_trigger("dud", exp, path=ledger)
        record_usefulness("helper", exp, useful=True, evidence="found 3 real defects", path=ledger)
        record_usefulness("dud", exp, useful=False, evidence="no findings, cost a round", path=ledger)

        trials = experiments(path=ledger)
        assert len(trials) == 1, trials
        t = trials[0]
        assert sorted(t["candidates"]) == ["dud", "helper", "never-tried"], t
        assert sorted(t["triggered"]) == ["dud", "helper"], t
        # THE CONTROL ARM must be reported, or this is a tally and not an experiment.
        assert t["not_triggered"] == ["never-tried"], t
        assert t["useful"] == ["helper"] and t["not_useful"] == ["dud"], t
        assert t["skills"] == ["repo-audit"], t

        u = usefulness(path=ledger)["rows"]
        assert u["helper"]["usefulness_rate"] == 1.0, u["helper"]
        assert u["dud"]["usefulness_rate"] == 0.0, u["dud"]
        # Rates must be None, never 0.0, when there is no denominator -- "0% useful" and "never
        # measured" are opposite findings that look identical once one is written as a zero.
        assert u["never-tried"]["usefulness_rate"] is None, u["never-tried"]
        assert u["never-tried"]["trigger_rate"] == 0.0, u["never-tried"]

        # USEFULNESS ORDERS THE RECOMMENDATION. This is the property the whole module exists for.
        p_helper = propensity("helper", path=ledger)["propensity"]
        p_dud = propensity("dud", path=ledger)["propensity"]
        assert p_helper > p_dud, (p_helper, p_dud)

        # THE LATCHED-GATE PROPERTY: no evidence must NOT mean no chance of being tried.
        p_new = propensity("never-tried", path=ledger)
        assert p_new["propensity"] >= EXPLORATION_FLOOR, p_new
        assert p_new["evidence_count"] == 0 and p_new["explorable"] is True, p_new
        assert "can therefore still earn evidence" in p_new["basis"], p_new
        # ...and the useless one must ALSO stay drainable, or one bad trial is a life sentence.
        assert propensity("dud", path=ledger)["propensity"] >= EXPLORATION_FLOOR
        assert propensity("dud", path=ledger)["explorable"] is True

        # Evidence is mandatory for a verdict.
        for bad in ("", "   "):
            try:
                record_usefulness("helper", exp, useful=True, evidence=bad, path=ledger)
            except ValueError:
                pass
            else:
                raise AssertionError("an unevidenced usefulness verdict must be refused")
        # An experiment id that is not an advisory digest must be refused, or the experiment
        # population silently fills with rows that belong to no trial.
        try:
            record_trigger("helper", "not-an-advice-ref", path=ledger)
        except ValueError:
            pass
        else:
            raise AssertionError("a non-advisory experiment id must be refused")

        # WINDOW: one constant drives measurement and drain, so a stale trial leaves both together.
        old = capabilities._now() + (WINDOW_DAYS + 2) * 86400
        assert experiments(path=ledger, now=old) == [], "the window must expire trials"
        r_old = report(path=ledger, now=old)
        assert r_old["capabilities_with_evidence"] == 0, r_old
        assert r_old["capability_count"] == 3, "the denominator must survive the window"

        rep = report(path=ledger)
        assert rep["experiment_count"] == 1 and rep["resolved_experiment_count"] == 1, rep
        assert rep["capabilities_with_evidence"] == 2, rep
        assert rep["capabilities_without_evidence"] == 1, rep
        assert [r["capability_id"] for r in rep["ranked"]][0] == "helper", rep["ranked"]

    # THE LANE-FACING CONTRACT. Both lane automations now call this from bash, so the shapes they
    # depend on are pinned here. A loop that can only be closed from Python cannot be closed by an
    # automation, and an experiment id the caller never receives cannot be passed back.
    import capability_advisor
    task = "resolve the unresolved review threads on this PR"
    eid = capability_advisor.experiment_id(task)
    assert eid.startswith(ADVICE_REF_PREFIX), eid
    assert eid == capability_advisor.experiment_id(task), "experiment id must be stable per task"
    assert eid != capability_advisor.experiment_id("something else"), "and task-specific"
    # advise() must HAND BACK the id it recorded under, in both the classified and unclassified
    # branches -- the caller cannot close a loop whose key it was never told.
    got = capability_advisor.advise(task, lane="closer", record=False)
    assert got["experiment_id"] == eid, (got.get("experiment_id"), eid)
    blank = capability_advisor.advise("xyzzy plugh frobnicate", record=False)
    assert blank["experiment_id"] == capability_advisor.experiment_id("xyzzy plugh frobnicate"), blank
    print("capability_propensity.py selftest: OK (natural experiments with a reported control arm, "
          "usefulness orders recommendation, no-evidence stays drainable, window shared)")


def _fmt(rep: dict) -> str:
    lines = [f"capability propensity — {rep['window_days']}d window",
             f"  experiments: {rep['experiment_count']} "
             f"({rep['resolved_experiment_count']} resolved)",
             f"  capabilities with usefulness evidence: {rep['capabilities_with_evidence']} "
             f"of {rep['capability_count']}"]
    if not rep["capabilities_with_evidence"]:
        lines.append("  NOTE: no resolved outcomes yet — every propensity below is the PRIOR, "
                     "not a measurement")
    lines.append("")
    lines.append(f"  {'capability':34s} {'prop':>6s} {'cand':>5s} {'trig':>5s} {'use':>4s} {'no':>3s}")
    for row in rep["ranked"][:60]:
        lines.append(f"  {row['capability_id']:34s} {row['propensity']:6.3f} "
                     f"{row['candidates']:5d} {row['triggered']:5d} {row['useful']:4d} "
                     f"{row['not_useful']:3d}" + ("  (floored)" if row["floored"] else ""))
    return "\n".join(lines) + "\n"


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("command", nargs="?", default="report",
                    choices=["report", "experiments", "trigger", "useful"])
    # A loop that can only be closed from Python cannot be closed by a lane, which runs bash. These
    # two subcommands are the whole reason the recording edges are reachable from an automation.
    ap.add_argument("--capability", default="", help="capability id, for trigger/useful")
    ap.add_argument("--experiment", default="", help="advice:<digest> from capability_advice")
    ap.add_argument("--evidence", default="", help="what the capability CHANGED (required by useful)")
    ap.add_argument("--not-useful", action="store_true",
                    help="record that triggering it did NOT help")
    # ISOLATION FOR PROOFS. Wiring this up, I recorded a trial into the LIVE ledger whose evidence
    # described the wiring rather than the capability's review value -- a mislabeled trial, and the
    # system's first data point. A proof belongs on a throwaway ledger; without this flag the only
    # way to demonstrate the path was to pollute the thing being demonstrated.
    ap.add_argument("--ledger", default="",
                    help="write to this ledger instead of the live one (use for demos and proofs)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--window-days", type=int, default=WINDOW_DAYS)
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)
    if args.selftest:
        _selftest()
        return 0
    if args.command in {"trigger", "useful"}:
        if not args.capability or not args.experiment:
            ap.error("--capability and --experiment are required")
        ledger = pathlib.Path(args.ledger) if args.ledger else None
        if args.command == "trigger":
            ok = record_trigger(args.capability, args.experiment, path=ledger)
        else:
            if not args.evidence.strip():
                ap.error("--evidence is required: an unevidenced verdict is an opinion, and "
                         "'it ran' is not usefulness")
            ok = record_usefulness(args.capability, args.experiment,
                                   useful=not args.not_useful, evidence=args.evidence,
                                   path=ledger)
        print(json.dumps({"recorded": bool(ok), "command": args.command,
                          "ledger": str(ledger) if ledger else "live",
                          "capability": args.capability, "experiment": args.experiment}))
        return 0
    if args.command == "experiments":
        data = experiments(window_days=args.window_days)
        print(json.dumps(data, indent=2) if args.json else
              "\n".join(f"{t['experiment_id']}  candidates={len(t['candidates'])} "
                        f"triggered={len(t['triggered'])} not_triggered={len(t['not_triggered'])} "
                        f"useful={t['useful']} skills={t['skills']}" for t in data) + "\n")
        return 0
    rep = report(window_days=args.window_days)
    _capability_heartbeat("invocation", f"cli:report:{rep['experiment_count']}")
    print(json.dumps(rep, indent=2) if args.json else _fmt(rep), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
