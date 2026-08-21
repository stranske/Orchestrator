#!/usr/bin/env python3
"""features.py — recognize reusable orchestrator features as they emerge, and promote them.

The orchestrator keeps inventing ad-hoc structures (A/B/C/D harness, stall-watcher, offload,
adversarial review...). Without a system they stay one-off scripts and the wheel gets reinvented.
This is the RULE OF THREE made explicit: log each reusable structure, and when it recurs, promote it
up a maturity ladder ad-hoc -> reused -> hardened (a selftested module, like exp_abcd.py). Design: §C
of EVAL_AND_TESTING.md. Promotion/hardening is itself a capacity-aware job (research_scheduler.py).

Recognition heuristic (run at task end): "Did I build a structure that solves a problem a FUTURE task
will hit? Is this the 2nd/3rd time?" -> record_use(). Promotion candidates surface automatically.
"""
from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

ORCH = Path(__file__).resolve().parent
REG = Path(os.environ.get("ORCH_FEATURES_PATH", ORCH / "experiments" / "features.json"))
LADDER = ["ad-hoc", "reused", "hardened"]

# Seeded with what already emerged building this system — honest current state.
SEED = {
    "feedback-store":      {"problem": "retain decisions/outcomes/cost + learn over time", "maturity": "hardened",
                            "module": "feedback.py", "uses": ["scorecard-eval"]},
    "abcd-experiment":     {"problem": "unbiased comparative-advantage by N-way same-spec + cross-eval",
                            "maturity": "hardened", "module": "exp_abcd.py", "uses": ["scorecard-eval"]},
    "offload":             {"problem": "delegate token-heavy reading/design to a cheaper agent, get result back",
                            "maturity": "hardened", "module": "dispatcher.offload", "uses": ["scorecard-spec-gen"]},
    "research-scheduler":  {"problem": "run science only on spare capacity, ranked by info/cost",
                            "maturity": "hardened", "module": "research_scheduler.py", "uses": []},
    "stall-watcher":       {"problem": "monitor detached agents for progress, stalls, and changed-path drift",
                            "maturity": "hardened", "module": "watch.py",
                            "uses": ["scorecard-eval x2", "exp_abcd.status", "semantic-drift"]},
    "frontend-verifier":   {"problem": "verify frontend behavior through deterministic accessibility-tree assertions",
                            "maturity": "hardened", "module": "frontend_verify.py",
                            "uses": ["live-demo", "trip-planner-runtime"]},
    "testgen-lane":        {"problem": "accept generated pytest tests only after collect, non-regression, reliability, and coverage-delta gates",
                            "maturity": "hardened", "module": "testgen_lane.py/testgen_gate.py",
                            "uses": ["inv-man-workflow-validation-live"]},
    "agy-runtime-isolation": {"problem": "run Antigravity from Codex with real-home auth and writable runtime project/app data",
                              "maturity": "hardened", "module": "adapters.py/dispatcher.py",
                              "uses": ["inv-man-testgen-offload"]},
    "windowed-capacity-policy": {"problem": "model no-usage-API prepaid seats with soft windows and router policy hints",
                                 "maturity": "hardened", "module": "capacity.py/router.py",
                                 "uses": ["agy-capacity-policy"]},
    "repo-playbook":      {"problem": "inject durable repo gotchas and definition-of-done rules into delegated prompts",
                           "maturity": "hardened", "module": "repo_knowledge.py",
                           "uses": ["durable-repo-knowledge", "snapshot-suggestions", "approval-controls",
                                    "docs-comments-mining", "suggestion-clustering"]},
    "redirect-policy":    {"problem": "turn watch reports plus attempt history into advisory retry/decompose decisions",
                           "maturity": "hardened", "module": "redirect_policy.py",
                           "uses": ["smarter-stall-redirection"]},
    "redirect-plan":      {"problem": "convert redirect/decompose decisions into safe recovery commands, prompts, and guarded apply",
                           "maturity": "hardened", "module": "redirect_plan.py",
                           "uses": ["smarter-stall-redirection", "guarded-redirect-apply"]},
    "deliberate-break-verifier": {"problem": "catch hollow tests by requiring candidate checks to fail on base code",
                                  "maturity": "hardened", "module": "local_verify.py",
                                  "uses": ["trustworthy-verification", "feedback-label-wiring"]},
    "epic-decomposition": {"problem": "turn vague goals into structured subtask plans with re-decomposition triggers",
                           "maturity": "hardened", "module": "epic_lane.py", "uses": ["epic-lane-v0"]},
    "codemod-campaign": {"problem": "plan and validate cross-file structural refactor campaigns with safe dry-run artifacts",
                         "maturity": "hardened", "module": "codemod_lane.py", "uses": ["codemod-lane-v0"]},
    "cross-repo-coordination": {"problem": "plan source and consumer repo changes with dry-run barrier artifacts",
                                "maturity": "hardened", "module": "cross_repo_lane.py",
                                "uses": ["cross-repo-lane-v0"]},
    "runtime-ac-checks": {"problem": "plan, execute, gate, and enforce AC-bound runtime verification evidence",
                          "maturity": "hardened",
                          "module": "runtime_ac.py/runtime_ac_gate.py/runtime_ac_panel.py/merge_guard.py",
                          "uses": ["runtime-ac-v0", "runtime-ac-runner", "runtime-ac-tick-hook",
                                   "runtime-ac-merge-guard", "runtime-ac-panel",
                                   "runtime-ac-panel-dispatch"]},
    "adversarial-review":  {"problem": "refute-mode + minority-veto ensemble for high-stakes correctness",
                            "maturity": "hardened", "module": "adversarial.py", "uses": []},
}


def load(path: Path = REG) -> dict:
    if path.exists():
        return json.loads(path.read_text())
    path.parent.mkdir(parents=True, exist_ok=True)
    seed = {k: {**v, "first_seen": int(time.time()), "count": len(v.get("uses", []))} for k, v in SEED.items()}
    path.write_text(json.dumps(seed, indent=2))
    return seed


def save(reg: dict, path: Path = REG) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(reg, indent=2))


def _read_registry(path: Path, *, create: bool) -> dict:
    if create or path.exists():
        return load(path)
    return {}


def record_use(
    name: str,
    where: str,
    problem: str = "",
    path: Path = REG,
    *,
    module: str | None = None,
    maturity: str | None = None,
) -> dict:
    """Log a (re)use of a feature. Auto-advances maturity ad-hoc->reused on the 2nd use. Hardening
    (-> module) stays a deliberate promotion (mark_hardened) so we don't claim code that isn't written."""
    if maturity is not None and maturity not in LADDER:
        raise ValueError(f"invalid maturity: {maturity}")
    reg = load(path)
    f = reg.get(name) or {"problem": problem, "maturity": "ad-hoc", "module": None,
                          "uses": [], "first_seen": int(time.time()), "count": 0}
    if problem and not f.get("problem"):
        f["problem"] = problem
    if module:
        f["module"] = module
    if where not in f["uses"]:
        f["uses"].append(where)
    f["count"] = len(f["uses"])
    if f["maturity"] == "ad-hoc" and f["count"] >= 2:
        f["maturity"] = "reused"
    if maturity:
        f["maturity"] = maturity
    reg[name] = f; save(reg, path)
    return f


def promotion_candidates(path: Path = REG, *, create: bool = True) -> list:
    """Features used >=3 times but not yet hardened into a selftested module — the rule of three firing."""
    reg = _read_registry(path, create=create)
    return [{"name": n, "count": f["count"], "problem": f["problem"]}
            for n, f in reg.items() if f["maturity"] != "hardened" and f["count"] >= 3]


def mark_hardened(name: str, module: str, path: Path = REG) -> None:
    reg = load(path)
    if name not in reg:
        raise ValueError(f"unknown feature: {name}; record it before hardening")
    reg[name]["maturity"] = "hardened"
    reg[name]["module"] = module
    save(reg, path)


def summary(path: Path = REG, *, create: bool = True) -> dict:
    reg = _read_registry(path, create=create)
    counts = {m: 0 for m in LADDER}
    for item in reg.values():
        maturity = item.get("maturity", "ad-hoc")
        counts[maturity] = counts.get(maturity, 0) + 1
    candidates = promotion_candidates(path, create=create)
    lifecycle = {
        "path": None,
        "total": 0,
        "counts_by_status": {},
        "active_without_edges": [],
    }
    try:
        import capabilities

        cap_report = capabilities.summary(create=False)
        lifecycle = {
            "path": cap_report.get("path"),
            "total": cap_report.get("total", 0),
            "counts_by_status": cap_report.get("counts_by_status", {}),
            "active_without_edges": cap_report.get("active_without_edges", []),
        }
    except Exception as exc:
        lifecycle["error"] = str(exc)
    return {
        "path": str(path),
        "total": len(reg),
        "counts_by_maturity": counts,
        "activation_authority": "capabilities",
        "lifecycle": lifecycle,
        "promotion_candidates": candidates,
        "top_reused": [
            {
                "name": name,
                "count": item.get("count", 0),
                "maturity": item.get("maturity"),
                "module": item.get("module"),
                "problem": item.get("problem"),
            }
            for name, item in sorted(
                reg.items(),
                key=lambda kv: (-int(kv[1].get("count", 0)), kv[0]),
            )[:10]
        ],
    }


def _selftest():
    p = Path("/tmp/__features_selftest.json"); p.unlink(missing_ok=True)
    reg = load(p)
    assert reg["abcd-experiment"]["maturity"] == "hardened", reg["abcd-experiment"]
    # a new ad-hoc structure: first use stays ad-hoc, second use auto-promotes to reused
    f1 = record_use("diff-anonymizer", "scorecard-eval", "anonymize candidates for unbiased judging", p)
    assert f1["maturity"] == "ad-hoc" and f1["count"] == 1, f1
    f2 = record_use("diff-anonymizer", "future-exp-2", path=p)
    assert f2["maturity"] == "reused" and f2["count"] == 2, f2
    # rule of three: a 3rd use makes it a promotion candidate
    record_use("diff-anonymizer", "future-exp-3", path=p)
    cands = [c["name"] for c in promotion_candidates(p)]
    assert "diff-anonymizer" in cands, cands
    s = summary(p)
    assert s["counts_by_maturity"]["reused"] >= 1 and s["promotion_candidates"], s
    missing = p.with_name("__features_missing_selftest.json")
    missing.unlink(missing_ok=True)
    empty = summary(missing, create=False)
    assert empty["total"] == 0 and not missing.exists(), empty
    reflected = record_use("reflection-cli", "task-end", "record reusable feature uses", p,
                           module="features.py", maturity="hardened")
    assert reflected["maturity"] == "hardened" and reflected["module"] == "features.py", reflected
    try:
        mark_hardened("missing-feature", "missing.py", p)
        raise AssertionError("expected unknown feature harden to fail")
    except ValueError as exc:
        assert "unknown feature" in str(exc), exc
    mark_hardened("diff-anonymizer", "exp_abcd._anonymize", p)
    assert "diff-anonymizer" not in [c["name"] for c in promotion_candidates(p)], "hardened -> not a candidate"
    p.unlink(missing_ok=True)
    print("features.py selftest: OK (seed maturity, reflection record, summary, promotion candidates, harden)")


def _capability_heartbeat(event_type: str = "invocation") -> None:
    """Record that this capability ran, at its own code path.

    Infrastructure and lane capabilities are not always ROUTED to — they are entered directly — so
    each records use where it actually executes. Lazy import (capabilities imports feedback, and
    several of these are imported BY capabilities' dependencies), never raises (recording use must
    not be able to prevent the work), and inert outside an active tick via
    ORCH_CAPABILITY_HEARTBEATS. (2026-08-09)
    """
    try:
        import capabilities
        capabilities.production_heartbeat("feature-reflection-cli", event_type, ref="features.main")
    except Exception:
        pass


def main(argv):
    _capability_heartbeat()
    if "--selftest" in argv:
        _selftest(); return 0
    parser = argparse.ArgumentParser(description="Record and inspect reusable Orchestrator feature patterns.")
    sub = parser.add_subparsers(dest="cmd")
    rec = sub.add_parser("record", help="Record the end-of-task reflection for a reusable feature.")
    rec.add_argument("--name", required=True)
    rec.add_argument("--where", required=True)
    rec.add_argument("--problem", default="")
    rec.add_argument("--module", default="")
    rec.add_argument("--maturity", choices=LADDER)
    rec.add_argument("--json", action="store_true", dest="as_json")
    harden = sub.add_parser("harden", help="Mark a feature as hardened into a module.")
    harden.add_argument("--name", required=True)
    harden.add_argument("--module", required=True)
    harden.add_argument("--json", action="store_true", dest="as_json")
    candidates = sub.add_parser("candidates", help="Show rule-of-three promotion candidates.")
    candidates.add_argument("--json", action="store_true", dest="as_json")
    summ = sub.add_parser("summary", help="Show registry maturity counts and promotion candidates.")
    summ.add_argument("--json", action="store_true", dest="as_json")
    sub.add_parser("list", help="List the feature registry.")
    args = parser.parse_args(argv)
    if args.cmd == "record":
        item = record_use(args.name, args.where, args.problem, module=args.module or None,
                          maturity=args.maturity)
        if args.as_json:
            print(json.dumps({"name": args.name, **item}, indent=2))
        else:
            print(f"recorded {args.name}: maturity={item['maturity']} count={item['count']}")
        return 0
    if args.cmd == "harden":
        try:
            mark_hardened(args.name, args.module)
        except ValueError as exc:
            parser.error(str(exc))
        out = {"name": args.name, "maturity": "hardened", "module": args.module}
        if args.as_json:
            print(json.dumps(out, indent=2))
        else:
            print(f"hardened {args.name}: {args.module}")
        return 0
    if args.cmd == "candidates":
        cands = promotion_candidates()
        if args.as_json:
            print(json.dumps(cands, indent=2))
        else:
            if not cands:
                print("No promotion candidates.")
            for c in cands:
                print(f"  - {c['name']} (used {c['count']}x): {c['problem']}")
        return 0
    if args.cmd == "summary":
        out = summary()
        if args.as_json:
            print(json.dumps(out, indent=2))
        else:
            counts = out["counts_by_maturity"]
            print(
                f"features: total={out['total']} ad-hoc={counts.get('ad-hoc', 0)} "
                f"reused={counts.get('reused', 0)} hardened={counts.get('hardened', 0)} "
                f"promotion_candidates={len(out['promotion_candidates'])}"
            )
            for c in out["promotion_candidates"]:
                print(f"  - {c['name']} (used {c['count']}x): {c['problem']}")
        return 0
    reg = load()
    for name, f in sorted(reg.items(), key=lambda kv: LADDER.index(kv[1]["maturity"])):
        mod = f.get("module") or "(inline)"
        print(f"  {f['maturity']:9} x{f['count']:<2} {name:20} {mod:24} — {f['problem']}")
    cands = promotion_candidates()
    if cands:
        print("\nPROMOTION CANDIDATES (rule of three):")
        for c in cands:
            print(f"  - {c['name']} (used {c['count']}x): {c['problem']}")
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(main(sys.argv[1:]))
