#!/usr/bin/env python3
"""capability_matcher_proposals.py — proposed triggers for capabilities that have none, with the
historical evidence for each.

Answers the question the opportunity report could not: not "CAN work be routed here" (mechanical:
is there a matcher) but "SHOULD work have been routed here" (substantive: did the fleet repeatedly
do work this capability exists to handle, while never invoking it).

Method: each proposal is a HYPOTHESIS about what work a capability serves, written as a real
matcher and then scored against all recorded runs. The count is the evidence — "codemod-campaign
would have matched 38 past runs and was never invoked" is a finding; "codemod-campaign is unused"
is not. Proposals are declared here rather than inferred, because the mapping from a module to the
work it serves is a judgement that should be reviewable in one place.

TWO KINDS, and conflating them is the mistake to avoid:
  work_routed    — a lane that handles a recognisable class of work. "Should work have been routed
                   here" is a real question, and the historical count answers it.
  infrastructure — always-on plumbing exercised by many runs rather than selected for a task
                   (the feedback store, the capacity model, the offload transport). Asking whether
                   work "should have been routed" to these is a category error; they need an
                   internal-event trigger, not a task matcher, and their count is context, not a gap.

    python3 capability_matcher_proposals.py             # proposals + evidence
    python3 capability_matcher_proposals.py --json
    python3 capability_matcher_proposals.py --apply     # write matchers into the ledger
    python3 capability_matcher_proposals.py --selftest

`--apply` writes ONLY work_routed matchers and never overwrites an existing one; infrastructure
proposals are recorded as advisory notes because their triggers are code-level, not task-level.
"""
from __future__ import annotations

import argparse
import json
import sys

import capabilities
import feedback

WORK_ROUTED = "work_routed"
INFRASTRUCTURE = "infrastructure"


def _m(*task_types: str) -> dict:
    """The one matcher shape `_matches_trigger` actually evaluates against a trigger."""
    return {"field": "task_type", "operator": "in", "value": list(task_types)}


# Each entry: the capability, what its entrypoint actually does, the work it should serve, and the
# matcher expressing that. Rationales are drawn from the module docstrings, not guessed.
PROPOSALS: dict[str, dict] = {
    # ---- work-routed lanes: "should work have been routed here?" is answerable ----------------
    "codemod-campaign": {
        "kind": WORK_ROUTED, "matcher": _m("codemod"),
        "rationale": "codemod_lane.py builds codemod/refactor campaign plans; the fleet ran codemod work.",
    },
    "cross-repo-coordination": {
        "kind": WORK_ROUTED, "matcher": _m("cross_repo"),
        "rationale": "cross_repo_lane.py builds coordinated source+consumer change plans.",
    },
    "epic-decomposition": {
        "kind": WORK_ROUTED, "matcher": _m("epic"),
        "rationale": "epic_lane.py turns a vague goal into a structured subtask plan.",
    },
    "testgen-lane": {
        "kind": WORK_ROUTED, "matcher": _m("testgen"),
        "rationale": "testgen_lane.py builds gate-backed prompts for generated-test work.",
    },
    "frontend-verifier": {
        "kind": WORK_ROUTED, "matcher": _m("ux_review"),
        "rationale": "frontend_verify.py performs vision-free frontend/UI verification; ux_review is that work.",
    },
    "adversarial-review": {
        "kind": WORK_ROUTED, "matcher": _m("review"),
        "rationale": "adversarial.py is the refute-mode review panel; review is the work class it serves.",
    },
    "deliberate-break-verifier": {
        "kind": WORK_ROUTED, "matcher": _m("testgen", "runtime_ac"),
        "rationale": "local_verify.py verifies delegated work by deliberate break — the gate-backed classes.",
    },
    "docs-drift-fix-agent": {
        "kind": WORK_ROUTED, "matcher": _m("docs"),
        "rationale": "docs drift repair; the fleet records a docs task_type and a docs work_type.",
    },
    # ---- always-on infrastructure: a task matcher would misrepresent how these are reached -----
    "feedback-store": {
        "kind": INFRASTRUCTURE, "matcher": {"kind": "feedback_event", "name": "record_run"},
        "rationale": "feedback.py IS the Brain — exercised by every recorded run, never selected for a task.",
    },
    "windowed-capacity-policy": {
        "kind": INFRASTRUCTURE, "matcher": {"kind": "tick_phase", "name": "capacity"},
        "rationale": "capacity.py/router.py compute seat policy every tick; not routed work.",
    },
    "agy-runtime-isolation": {
        "kind": INFRASTRUCTURE, "matcher": {"kind": "adapter", "name": "gemini"},
        "rationale": "adapters/dispatcher runtime isolation for the agy seat; a property of dispatch.",
    },
    "offload": {
        "kind": INFRASTRUCTURE, "matcher": {"kind": "transport", "name": "offload"},
        "rationale": "dispatcher.offload is a transport mode, exercised by offload runs themselves.",
    },
    "abcd-experiment": {
        "kind": INFRASTRUCTURE, "matcher": {"kind": "experiment_phase", "name": "abcd"},
        "rationale": "exp_abcd.py runs experiments on its own cadence; already self-attributing.",
    },
    "research-scheduler": {
        "kind": INFRASTRUCTURE, "matcher": {"kind": "tick_phase", "name": "research"},
        "rationale": "research_scheduler.py is the capacity-aware research arm, driven by hunger not task type.",
    },
    "stall-watcher": {
        "kind": INFRASTRUCTURE, "matcher": {"kind": "tick_phase", "name": "watch"},
        "rationale": "watch.py monitors in-flight lanes; triggered by lane state, not by a task class.",
    },
    "redirect-plan": {
        "kind": INFRASTRUCTURE, "matcher": {"kind": "lane_event", "name": "redirect_decision"},
        "rationale": "redirect_plan.py builds execution plans once a redirect decision exists.",
    },
    "redirect-policy": {
        "kind": INFRASTRUCTURE, "matcher": {"kind": "lane_event", "name": "stall_detected"},
        "rationale": "redirect_policy.py advises retry/decompose for watched lanes on stall.",
    },
    "repo-playbook": {
        "kind": INFRASTRUCTURE, "matcher": {"kind": "prompt_phase", "name": "delegation"},
        "rationale": "repo_knowledge.py injects per-repo playbook snippets into delegation prompts.",
    },
    "feature-reflection-cli": {
        "kind": INFRASTRUCTURE, "matcher": {"kind": "tick_phase", "name": "reflection"},
        "rationale": "features.py/periodic_report.py promote emergent features on a reporting cadence.",
    },
}


def _task_counts(conn=None) -> dict[str, int]:
    close = conn is None
    c = conn or feedback._conn()
    try:
        return {str(t): int(n) for t, n in
                c.execute("SELECT task_type, COUNT(*) FROM runs GROUP BY task_type")}
    finally:
        if close:
            c.close()


def score(proposal: dict, task_counts: dict) -> int | None:
    """Historical runs this matcher would have matched, or None when not task-routed."""
    matcher = proposal["matcher"]
    if matcher.get("field") != "task_type":
        return None
    return sum(task_counts.get(t, 0) for t in (matcher.get("value") or []))


def evaluate(*, path=None, task_counts: dict | None = None) -> dict:
    caps = capabilities.load(path or capabilities.REG)
    counts = task_counts if task_counts is not None else _task_counts()
    rows = []
    for cap_id, proposal in sorted(PROPOSALS.items()):
        cap = caps.get(cap_id) or {}
        matched = score(proposal, counts)
        invoked = bool(cap.get("last_invocation"))
        # SHOULD-HAVE: demonstrated work of this class ran, and the capability never did.
        should_have = bool(matched and not invoked) if matched is not None else None
        rows.append({
            "capability_id": cap_id, "kind": proposal["kind"],
            "matcher": proposal["matcher"], "rationale": proposal["rationale"],
            "historical_matches": matched, "ever_invoked": invoked,
            "should_have_been_used": should_have,
            "already_has_matcher": bool(cap.get("matcher")),
            "in_ledger": cap_id in caps,
        })
    return {
        "total": len(rows),
        "work_routed": [r["capability_id"] for r in rows if r["kind"] == WORK_ROUTED],
        "infrastructure": [r["capability_id"] for r in rows if r["kind"] == INFRASTRUCTURE],
        "should_have_been_used": [r["capability_id"] for r in rows if r["should_have_been_used"]],
        "missed_run_total": sum(r["historical_matches"] or 0 for r in rows
                                if r["should_have_been_used"]),
        "rows": rows,
    }


def apply_matchers(rep: dict, *, path=None, dry_run: bool = False,
                   include_infrastructure: bool = False) -> dict:
    """Write proposed matchers into the ledger. Never overwrites an existing matcher.

    Infrastructure matchers are opt-in (`include_infrastructure`). They were withheld while
    `_matches_trigger` could not evaluate `kind` shapes — applying them then would have made those
    capabilities match every routing decision. Now that kinds are evaluated against a same-named
    trigger field and fail closed when the caller supplies no such field, they are safe: an
    infrastructure capability matches only when the orchestrator actually reports that phase.
    """
    ledger = path or capabilities.REG
    caps = capabilities.load(ledger)
    written, skipped = [], []
    allowed = {WORK_ROUTED} | ({INFRASTRUCTURE} if include_infrastructure else set())
    for row in rep["rows"]:
        cap = caps.get(row["capability_id"])
        if cap is None or row["kind"] not in allowed or row["already_has_matcher"]:
            skipped.append(row["capability_id"])
            continue
        if not dry_run:
            cap["matcher"] = row["matcher"]
            capabilities.validate_capability(cap)
        written.append(row["capability_id"])
    if written and not dry_run:
        capabilities.save(caps, ledger)
    return {"written": written, "skipped": skipped, "dry_run": dry_run}


def format_report(rep: dict) -> str:
    lines = [
        "# Proposed capability triggers, with historical evidence", "",
        f"{len(rep['work_routed'])} work-routed · {len(rep['infrastructure'])} infrastructure", "",
        f"## SHOULD HAVE BEEN USED: {len(rep['should_have_been_used'])} "
        f"({rep['missed_run_total']} runs of matching work ran while the capability never did)", "",
    ]
    for row in rep["rows"]:
        if row["should_have_been_used"]:
            lines.append(f"- **{row['capability_id']}** — {row['historical_matches']} matching runs. "
                         f"{row['rationale']}")
    lines += ["", "| Capability | Kind | Would have matched | Invoked | Should have | Matcher |",
              "|---|---|---:|:--:|:--:|---|"]
    for row in rep["rows"]:
        m = row["historical_matches"]
        lines.append(
            f"| {row['capability_id']} | {row['kind']} | {'n/a' if m is None else m} | "
            f"{'yes' if row['ever_invoked'] else 'no'} | "
            f"{'—' if row['should_have_been_used'] is None else ('YES' if row['should_have_been_used'] else 'no')} | "
            f"`{json.dumps(row['matcher'])}` |")
    return "\n".join(lines) + "\n"


def _selftest() -> None:
    import tempfile
    from pathlib import Path

    counts = {"codemod": 38, "testgen": 83, "ux_review": 105, "review": 914, "epic": 6,
              "cross_repo": 6, "docs": 1, "runtime_ac": 25}

    # Task-routed proposals score against history; infrastructure ones deliberately do not.
    assert score(PROPOSALS["codemod-campaign"], counts) == 38
    assert score(PROPOSALS["deliberate-break-verifier"], counts) == 83 + 25
    assert score(PROPOSALS["feedback-store"], counts) is None, "infrastructure must not be task-scored"

    # Every proposal must name a kind and a rationale — no unexplained matcher may ship.
    for cid, p in PROPOSALS.items():
        assert p["kind"] in (WORK_ROUTED, INFRASTRUCTURE), cid
        assert p["rationale"].strip(), cid
        if p["kind"] == WORK_ROUTED:
            assert p["matcher"].get("field") == "task_type", f"{cid} work-routed must be task-routed"

    with tempfile.TemporaryDirectory(prefix="matcher-proposal-selftest-") as td:
        ledger = Path(td) / "capabilities.json"
        fresh = capabilities._blank_capability("codemod-campaign")
        fresh["status"] = "generated"
        existing = capabilities._blank_capability("testgen-lane")
        existing["status"] = "generated"
        existing["matcher"] = {"field": "task_type", "operator": "in", "value": ["preexisting"]}
        capabilities.save({"codemod-campaign": fresh, "testgen-lane": existing}, ledger)

        rep = evaluate(path=ledger, task_counts=counts)
        row = next(r for r in rep["rows"] if r["capability_id"] == "codemod-campaign")
        assert row["should_have_been_used"] is True and row["historical_matches"] == 38, row
        infra = next(r for r in rep["rows"] if r["capability_id"] == "feedback-store")
        assert infra["should_have_been_used"] is None, "infrastructure is not a should-have gap"

        dry = apply_matchers(rep, path=ledger, dry_run=True)
        assert capabilities.load(ledger)["codemod-campaign"]["matcher"] is None, "dry-run wrote!"
        out = apply_matchers(rep, path=ledger)
        assert out["written"] == ["codemod-campaign"], out
        after = capabilities.load(ledger)
        assert after["codemod-campaign"]["matcher"] == _m("codemod"), after["codemod-campaign"]
        # An existing matcher is never clobbered.
        assert after["testgen-lane"]["matcher"]["value"] == ["preexisting"], after["testgen-lane"]
        assert "testgen-lane" in out["skipped"], out

    print("capability_matcher_proposals.py selftest: OK (task scoring, infrastructure excluded, "
          "dry-run inert, existing matchers preserved)")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--include-infrastructure", action="store_true",
                    help="also write kind-shaped infrastructure matchers")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)
    if args.selftest:
        _selftest()
        return 0
    rep = evaluate()
    if args.apply:
        print(json.dumps(apply_matchers(
            rep, include_infrastructure=args.include_infrastructure), indent=2))
        return 0
    print(json.dumps(rep, indent=2) if args.json else format_report(rep), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
