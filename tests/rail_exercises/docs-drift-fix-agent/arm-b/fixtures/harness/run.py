#!/usr/bin/env python3
import json, os, sys, sqlite3, random
from pathlib import Path

EX2 = Path(__file__).resolve().parents[2]
FIX = Path(__file__).resolve().parents[1]
ORCH = EX2.parent
SRC = ORCH / "src"
sys.path.insert(0, str(SRC))

rail = sys.argv[1]
mode = sys.argv[2] if len(sys.argv) > 2 else "pass"

if rail == "completion-event-lineage":
    import completion_event_adapter as cea
    raw = json.loads((FIX / ("completion_event_break.json" if mode == "break" else "completion_event_valid.json")).read_text())
    try:
        ev = cea.adapt_completion_event_envelope(raw)
        print(f"EXERCISE_PASS:phase={ev.phase}")
    except cea.EnvelopeError as exc:
        print(f"EXERCISE_BREAK:{exc.reasons[0]}")

elif rail == "evidence-acquisition":
    import evidence_acquisition as ea
    ledger = json.loads((FIX / ("evidence_ledger_off.json" if mode == "break" else "evidence_ledger_ok.json")).read_text())
    candidates = json.loads((FIX / "evidence_candidates.json").read_text())
    result = ea.plan(candidates, ledger=ledger, now=1_700_000_000, env={})
    print(f"EXERCISE_{'BREAK' if mode == 'break' else 'PASS'}:state={result['state']}")

elif rail == "feature-reflection-cli":
    import feature_scan
    root = FIX / "feature_scan_root"
    if mode == "break":
        (root / "widget_lab.py").write_text("def _selftest():\n    pass\n", encoding="utf-8")
    rep = feature_scan.scan(path=FIX / "feature_registry.json", root=root)
    print(f"EXERCISE_{'BREAK' if mode == 'break' else 'PASS'}:unlogged_count={rep['unlogged_count']}")

elif rail == "feedback-store":
    import feedback
    tmp = Path(os.environ.get("P2_TMP", "/tmp/p2_feedback_tmp"))
    feedback.DB_PATH = tmp / "brain.db"
    feedback.record_run("p2-run-1", "fixture/repo#1", "implement", "cursor", ts=1_700_000_000)
    snap = feedback.snapshot_json(tmp / "snap.json")
    print(f"EXERCISE_{'BREAK' if mode == 'break' else 'PASS'}:runs={snap['rows']['runs']}")

elif rail == "issue-readiness":
    import issue_readiness
    issue = json.loads((FIX / ("issue_vague.json" if mode == "break" else "issue_actionable.json")).read_text())
    verdict = issue_readiness.classify_issue(issue)
    print(f"EXERCISE_{'BREAK' if mode == 'break' else 'PASS'}:verdict={verdict['verdict']}")

elif rail == "research-scheduler":
    import feedback
    import research_scheduler as rs
    feedback.DB_PATH = Path(os.environ.get("P2_TMP", "/tmp/p2_rs_tmp")) / "brain.db"
    live_cap = {
        "agents": {
            "cursor": {"state": "ok"},
            "codex": {"state": "ok"},
            "vibe": {"state": "ok"},
        }
    }
    items = [
        {
            "target": "fixture/repo#9",
            "task_type": "implement",
            "lane": "opener",
            "title": "P2 fixture research item",
        }
    ]
    learned = {
        "implement": {
            "cursor": {"posterior": 0.55, "n_obs": 1},
            "codex": {"posterior": 0.80, "n_obs": 4},
        }
    }
    plan = rs.build_research_plan(
        items, live_cap, learned=learned, budget=3, max_jobs=1, rng=random.Random(7)
    )
    status = plan["status"]
    if mode == "break":
        shed_cap = {"agents": {agent: {"state": "shed"} for agent in live_cap["agents"]}}
        plan2 = rs.build_research_plan(items, shed_cap, learned=learned, budget=1)
        status = plan2["status"]
    print(f"EXERCISE_{'BREAK' if mode == 'break' else 'PASS'}:status={status}")

elif rail == "redirect-apply-bootstrap":
    import redirect_apply as ra
    corpus = Path(os.environ.get("P2_CORPUS", "/tmp/p2_redirect_corpus.jsonl"))
    env = {"ORCH_REDIRECT_APPLY_BOOTSTRAP": "1" if mode == "break" else "0"}
    out = ra.status(corpus, env=env)
    label = "BREAK" if mode == "break" else "PASS"
    print(f"EXERCISE_{label}:flag_on={str(out['flag_on']).lower()}")

elif rail == "research-usage-guard":
    import feedback
    import research_usage_guard as rug
    db = sqlite3.connect(":memory:")
    rug.ensure_schema(db)
    if mode == "break":
        env = {"ORCH_RESEARCH_ARM": "1", "ORCH_GUARD_MAX_EVAL_CALLS_24H": "99"}
    else:
        env = {
            "ORCH_RESEARCH_ARM": "1",
            "ORCH_GUARD_MAX_EVAL_CALLS_24H": "1",
            "ORCH_GUARD_MAX_EVAL_CALLS_1H": "1",
        }
    now = 1_700_000_000
    first = rug.assess_and_record_opportunity(
        exp_id="p2-exp-1", repo="fixture/repo", subject="subj-a", spec_text="spec",
        base_sha="sha1", evaluator_agents=["vibe"], env=env, conn=db, now=now,
    )
    second = rug.assess_and_record_opportunity(
        exp_id="p2-exp-2", repo="fixture/repo", subject="subj-b", spec_text="spec2",
        base_sha="sha2", evaluator_agents=["vibe"], env=env, conn=db, now=now + 10,
    )
    if mode == "break":
        print(f"EXERCISE_BREAK:second={second['eligible']}")
    else:
        print(f"EXERCISE_PASS:first={first['eligible']},second={second['eligible']}")

elif rail == "synthesis-promotion":
    import synthesis_promotion as sp
    root = FIX / "synthesis_exp"
    sp.ensure_evaluated_state(root, now=100)
    if mode == "break":
        out = sp.reconcile(
            root,
            launch_fn=lambda: {"discard": True, "gate": {}, "ranking": {}},
            now=101,
        )
    else:
        out = sp.reconcile(root, now=101)
    action = out["actions"][0] if out["actions"] else "none"
    label = "BREAK" if mode == "break" else "PASS"
    print(f"EXERCISE_{label}:action={action}")

elif rail == "agy-runtime-isolation":
    import adapters
    agent = "cursor" if mode == "break" else "gemini"
    cmd = adapters.build_command(agent, "fixture prompt", cwd=FIX)
    has_dir = "--gemini_dir" in cmd
    label = "BREAK" if mode == "break" else "PASS"
    print(f"EXERCISE_{label}:gemini_dir={'yes' if has_dir else 'no'}")

else:
    print(f"unknown rail {rail}", file=sys.stderr)
    sys.exit(2)
