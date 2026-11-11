#!/usr/bin/env python3
"""Owner-requested live split of full-tier Gemini implementation between two models (2026-11-11).

The owner asked to split real full-tier implementation PRs between Gemini 4 Argon (the incumbent
`full` model) and Gemini 4 Carbon (the local `frontier` model) for 30 PRs and then evaluate them.

ASSIGNMENT (`assign`, called by `dispatcher.plan_dispatch`): a local opener-lane `implement` run on
the gemini seat, with no profile already chosen and the tier ceiling still allowing `full`, gets
one arm. The arm is drawn from a SHA-256 of the policy and the target, so a retry of the same issue
keeps its arm, and the assignment probability (1/len(arms)) is recorded on the run. Everything
else is untouched: hosted runs, closer continuations, capped runs (the quota cap lowers them to
Flash) and explicit `--profile` runs route exactly as before.

STOP: assignment stops when the readout has counted `target_prs` PRs from assigned issues, when
`max_assignments` issues have been assigned (a backstop in case the readout stops running), or at
`end_ts`. A missing, malformed or expired spec means no assignment: routing fails toward its
normal path, never toward a stall.

READOUT (`python3 src/gemini_full_split.py --readout`): per arm, the issues assigned, the PRs they
produced (found on GitHub by the dispatch branch), merged / durable / broke-later counts from the
feedback DB, and the pre-registered verdict. It also writes the PR count the stop rule reads.

Pass rule (written before any run): Carbon replaces Argon at `full` only if its merged-and-durable
share of PRs is at least Argon's AND its list-price cost per merged-durable PR is at most 2x
Argon's. Fewer than 8 PRs in either arm: INCONCLUSIVE. The cost leg is UNMEASURED today: lane
implement dispatches print agy's plain-text output and record no token usage (0 cost rows for
local gemini implement runs, measured 2026-11-11), so the readout reports the durability leg and
says the cost leg is unmeasured; it never promotes on durability alone.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Any

POLICY = "gemini-full-split-v1"
LOCAL_RUNTIME = Path(os.environ.get("ORCH_LOCAL_RUNTIME", Path.home() / ".codex" / "orchestrator"))
SPEC_PATH = Path(
    os.environ.get(
        "ORCH_GEMINI_FULL_SPLIT_PATH", LOCAL_RUNTIME / "trials" / "gemini-full-split.json"
    )
)
STATE_PATH = SPEC_PATH.with_name("gemini-full-split-state.json")
MIN_PRS_PER_ARM = 8


def load_spec(now: float | None = None, path: Path | None = None) -> tuple[dict | None, str]:
    """The active spec, or None with the reason it is not active."""
    import execution_profiles

    path = path or SPEC_PATH
    now = time.time() if now is None else now
    try:
        spec = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, "no spec"
    except (OSError, ValueError) as exc:
        return None, f"unreadable spec: {type(exc).__name__}"
    try:
        start, end = float(spec["start_ts"]), float(spec["end_ts"])
        arms = list(spec["arms"])
        target_prs, max_assign = int(spec["target_prs"]), int(spec["max_assignments"])
    except (KeyError, TypeError, ValueError):
        return None, "malformed spec"
    if spec.get("policy") != POLICY:
        return None, "spec names another policy"
    if not (math.isfinite(start) and math.isfinite(end) and start < end):
        return None, "spec window is not a bounded interval"
    if len(arms) < 2 or len(set(arms)) != len(arms):
        return None, "spec needs two or more distinct arms"
    for arm in arms:
        profile = execution_profiles.PROFILE_REGISTRY.get(arm)
        if not profile or profile["agent"] != "gemini":
            return None, f"arm {arm} is not a registered gemini profile"
    if target_prs < 1 or max_assign < target_prs:
        return None, "spec counts are inconsistent"
    if now < start:
        return None, "window not started"
    if now >= end:
        return None, "window ended"
    return {**spec, "arms": arms, "target_prs": target_prs, "max_assignments": max_assign}, "active"


def arm_for(target: str, arms: list[str]) -> tuple[str, int]:
    """Deterministic arm and seed for one target: a retry of the same issue keeps its arm."""
    digest = hashlib.sha256(f"{POLICY}:{target}".encode()).hexdigest()
    seed = int(digest[:12], 16)
    return arms[seed % len(arms)], seed


def _db_path() -> Path:
    import feedback

    return Path(feedback.DB_PATH)


def assigned_targets(conn: sqlite3.Connection | None = None) -> dict[str, str]:
    """{target: arm} for every issue a split run was started on."""
    own = conn is None
    conn = conn or sqlite3.connect(_db_path().resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    try:
        rows = conn.execute(
            "SELECT target, json_extract(routing_metadata,'$.selected_profile_id') FROM runs "
            "WHERE agent='gemini' AND json_extract(routing_metadata,'$.profile_policy_version')=? "
            "ORDER BY ts",
            (POLICY,),
        ).fetchall()
    finally:
        if own:
            conn.close()
    out: dict[str, str] = {}
    for target, arm in rows:
        out.setdefault(str(target), str(arm))
    return out


def pr_count() -> int | None:
    """PRs counted by the last readout, or None when the readout has never run."""
    try:
        return int(json.loads(STATE_PATH.read_text(encoding="utf-8"))["prs"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def assign(assignment: dict, *, now: float | None = None) -> tuple[dict | None, str]:
    """Arm fields for `assignment`, or None with the reason this run is outside the split."""
    import adapters
    import execution_profiles

    if assignment.get("agent") != "gemini":
        return None, "not gemini"
    if assignment.get("task_type") != "implement":
        return None, "not implement"
    if (assignment.get("lane") or "opener") != "opener":
        return None, "not an opener start"
    if assignment.get("selected_profile_id") or assignment.get("requested_model"):
        return None, "model already chosen"
    if assignment.get("mode") not in (None, "", "full"):
        return None, f"mode {assignment.get('mode')} is not full"
    if adapters.effective_tier("gemini", "full") != "full":
        return None, "tier ceiling below full (quota cap)"
    spec, why = load_spec(now)
    if spec is None:
        return None, why
    target = str(assignment.get("target") or "")
    if not target:
        return None, "no target"
    try:
        assigned = assigned_targets()
    except sqlite3.Error as exc:
        return None, f"feedback DB unreadable: {type(exc).__name__}"
    if target not in assigned:
        prs = pr_count()
        if prs is not None and prs >= spec["target_prs"]:
            return None, f"split complete: {prs}/{spec['target_prs']} PRs"
        if len(assigned) >= spec["max_assignments"]:
            return None, f"assignment backstop reached: {len(assigned)} issues"
    arm, seed = arm_for(target, spec["arms"])
    profile = execution_profiles.get_profile(arm)
    return {
        "selected_profile_id": arm,
        "requested_model": profile["requested_model"],
        "reasoning_effort": profile["reasoning_effort"],
        "permission_mode": profile["permission_mode"],
        "transport": "local",
        "profile_policy_version": POLICY,
        "profile_assignment_probability": 1.0 / len(spec["arms"]),
        "profile_rng_seed": seed,
    }, f"arm {arm}"


# ---------------------------------------------------------------------------------------------
# Readout


def _gh_json(args: list[str]) -> Any:
    done = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=60, check=True)
    return json.loads(done.stdout or "null")


def find_pr(target: str, since_ts: float, until_ts: float | None = None) -> dict | None:
    """The PR a split run opened for `target`: head branch `<prefix>/issue-N[-slug]`, created after
    the split run started and BEFORE the next run on the issue by anyone else. The prefix is
    `gemini`, or `orch` in infrastructure repos, where every agent uses `orch` -- so without the
    upper bound a later Claude or Codex retry's PR would be credited to the Gemini arm (seen on
    stranske/Orchestrator#643, 2026-11-11: Gemini refused at 18:06, Claude's PR at 19:09)."""
    repo, _, num = target.rpartition("#")
    if not repo or not num.isdigit():
        return None
    rows = (
        _gh_json(
            [
                "pr",
                "list",
                "-R",
                repo,
                "--state",
                "all",
                "--search",
                f"issue-{num} in:head",
                "--json",
                "number,headRefName,state,createdAt,mergedAt,url",
                "-L",
                "50",
            ]
        )
        or []
    )
    pattern = re.compile(rf"^(?:gemini|orch)/issue-{num}(?:$|[-_/])")
    found = [
        row
        for row in rows
        if pattern.match(row.get("headRefName") or "")
        and _iso_ts(row.get("createdAt")) >= since_ts - 600
        and (until_ts is None or _iso_ts(row.get("createdAt")) < until_ts)
    ]
    return min(found, key=lambda row: row["createdAt"]) if found else None


def _iso_ts(value: Any) -> float:
    from datetime import datetime

    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return float("-inf")


DISPATCH_LOG_DIR = Path(
    os.environ.get("ORCH_DISPATCH_LOG_DIR", Path.home() / ".codex" / "handoff" / "dispatch-logs")
)


def quota_refused(run_id: str, target: str) -> bool:
    """True when agy refused this run for quota before doing any work (its dispatch-log block
    carries RESOURCE_EXHAUSTED). Such a run never tested its model, so it is excluded."""
    slug = target.replace("/", "__").replace("#", "_")
    try:
        text = (DISPATCH_LOG_DIR / f"{slug}.gemini.log").read_text(errors="replace")
    except OSError:
        return False
    start = text.find(f"run_id={run_id}")
    if start < 0:
        return False
    end = text.find("\n=== ", start)
    block = text[start : end if end > 0 else len(text)]
    return "RESOURCE_EXHAUSTED" in block


def _outcomes(conn: sqlite3.Connection, target: str) -> dict:
    rows = conn.execute(
        "SELECT r.run_id, o.merged, o.durability, o.verifier_verdict, r.ts FROM runs r "
        "LEFT JOIN outcomes o USING(run_id) WHERE r.agent='gemini' AND r.target=? "
        "AND json_extract(r.routing_metadata,'$.profile_policy_version')=? ORDER BY r.ts",
        (target, POLICY),
    ).fetchall()
    cost_rows = [
        usd
        for (run_id, *_rest) in rows
        for (usd,) in conn.execute("SELECT cost_usd FROM costs WHERE run_id=?", (run_id,))
        if usd is not None
    ]
    run_ids = {row[0] for row in rows}
    refused = [row[0] for row in rows if quota_refused(row[0], target)]
    first_ts = rows[0][4] if rows else None
    later = conn.execute(
        "SELECT MIN(ts) FROM runs WHERE target=? AND ts>? AND run_id NOT IN (%s)"
        % ",".join("?" * len(run_ids)),
        (target, first_ts or 0, *sorted(run_ids)),
    ).fetchone()
    merged = any(row[1] == 1 for row in rows)
    durability = next((row[2] for row in reversed(rows) if row[2]), None)
    verdict = next((row[3] for row in reversed(rows) if row[3]), None)
    return {
        "runs": len(rows),
        "merged": merged,
        "durability": durability,
        "verifier": verdict,
        # None means NOT MEASURED, never zero: no cost row exists for the run.
        "cost_usd": round(sum(cost_rows), 3) if cost_rows else None,
        "first_ts": first_ts,
        "next_other_ts": later[0] if later and later[0] else None,
        "quota_refused": len(refused),
        "not_run": bool(rows) and len(refused) == len(rows),
    }


def readout(*, write_state: bool = True) -> dict:
    spec, why = load_spec(path=SPEC_PATH)
    raw_spec: dict = {}
    try:
        raw_spec = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    conn = sqlite3.connect(_db_path().resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    try:
        assigned = assigned_targets(conn)
        rows = []
        for target, arm in assigned.items():
            info = _outcomes(conn, target)
            pr = (
                None
                if info["not_run"]
                else find_pr(target, float(info["first_ts"] or 0), info["next_other_ts"])
            )
            rows.append({"target": target, "arm": arm, "pr": pr, **info})
    finally:
        conn.close()
    arms = raw_spec.get("arms") or sorted({row["arm"] for row in rows})
    per_arm = {}
    for arm in arms:
        not_run = [row for row in rows if row["arm"] == arm and row["not_run"]]
        mine = [row for row in rows if row["arm"] == arm and not row["not_run"]]
        with_pr = [row for row in mine if row["pr"]]
        merged = [row for row in with_pr if row["pr"].get("mergedAt") or row["merged"]]
        durable = [row for row in merged if row["durability"] == "durable"]
        broke = [row for row in merged if row["durability"] == "broke_later"]
        awaiting = [row for row in merged if row["durability"] in (None, "", "pending")]
        closed = [
            row
            for row in with_pr
            if row["pr"].get("state") == "CLOSED" and not row["pr"].get("mergedAt")
        ]
        open_ = [row for row in with_pr if row["pr"].get("state") == "OPEN"]
        measured = [row["cost_usd"] for row in mine if row["cost_usd"] is not None]
        cost = round(sum(measured), 2) if measured and len(measured) == len(mine) else None
        per_arm[arm] = {
            "issues": len(mine),
            "not_run_quota": len(not_run),
            "prs": len(with_pr),
            "no_pr": len(mine) - len(with_pr),
            "merged": len(merged),
            "durable": len(durable),
            "broke_later": len(broke),
            "awaiting_durability": len(awaiting),
            "closed_unmerged": len(closed),
            "open": len(open_),
            "verifier_pass": sum(
                1 for row in with_pr if str(row["verifier"] or "").upper() == "PASS"
            ),
            "cost_usd": cost,  # None = unmeasured (any run in the arm lacks a cost row)
            "cost_per_durable": (
                round(cost / len(durable), 2) if durable and cost is not None else None
            ),
            "durable_share": round(len(durable) / len(with_pr), 3) if with_pr else None,
        }
    prs = sum(arm["prs"] for arm in per_arm.values())
    resolved = sum(arm["merged"] + arm["closed_unmerged"] for arm in per_arm.values())
    awaiting = sum(arm["awaiting_durability"] for arm in per_arm.values())
    open_prs = sum(arm["open"] for arm in per_arm.values())
    verdict = _verdict(per_arm, arms)
    out = {
        "policy": POLICY,
        "spec_state": why,
        "target_prs": raw_spec.get("target_prs"),
        "prs": prs,
        "resolved_prs": resolved,
        "open_prs": open_prs,
        "awaiting_durability": awaiting,
        # Complete = the PR target is met and every counted PR is closed or has a durability verdict.
        "complete": bool(raw_spec.get("target_prs"))
        and prs >= int(raw_spec.get("target_prs") or 0)
        and open_prs == 0
        and awaiting == 0,
        "assigned_issues": len(assigned),
        "per_arm": per_arm,
        "verdict": verdict,
        "rows": rows,
    }
    if write_state:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps({"prs": prs, "ts": time.time()}) + "\n", encoding="utf-8")
    return out


def _verdict(per_arm: dict, arms: list[str]) -> str:
    """The pre-registered rule; the first arm is the incumbent."""
    if len(arms) < 2:
        return "INCONCLUSIVE: fewer than two arms"
    incumbent, *challengers = arms
    base = per_arm.get(incumbent) or {}
    parts = []
    for arm in challengers:
        row = per_arm.get(arm) or {}
        if min(base.get("prs", 0), row.get("prs", 0)) < MIN_PRS_PER_ARM:
            parts.append(
                f"{arm}: INCONCLUSIVE (PRs {row.get('prs', 0)} vs {base.get('prs', 0)}; "
                f"need {MIN_PRS_PER_ARM} each)"
            )
            continue
        share_ok = (row["durable_share"] or 0) >= (base["durable_share"] or 0)
        detail = f"durable share {row['durable_share']} vs {base['durable_share']}"
        if not share_ok:
            parts.append(f"{arm}: DOES NOT REPLACE {incumbent} ({detail})")
        elif row["cost_per_durable"] is None or base["cost_per_durable"] is None:
            parts.append(
                f"{arm}: durability leg PASSES ({detail}); cost leg UNMEASURED (no token usage "
                f"recorded for implement dispatches) -- owner decides"
            )
        elif row["cost_per_durable"] <= 2 * base["cost_per_durable"]:
            parts.append(
                f"{arm}: REPLACES {incumbent} ({detail}; cost per durable "
                f"{row['cost_per_durable']} vs {base['cost_per_durable']})"
            )
        else:
            parts.append(
                f"{arm}: DOES NOT REPLACE {incumbent} ({detail}; cost per durable "
                f"{row['cost_per_durable']} > 2x {base['cost_per_durable']})"
            )
    return "; ".join(parts)


def render(out: dict) -> str:
    lines = [
        f"gemini full split ({out['policy']}): {out['prs']}/{out['target_prs']} PRs, "
        f"{out['resolved_prs']} resolved, {out['open_prs']} open, {out['awaiting_durability']} awaiting a "
        f"durability verdict, {out['assigned_issues']} issues assigned; spec {out['spec_state']}; "
        f"{'COMPLETE' if out['complete'] else 'in progress'}",
    ]
    for arm, row in out["per_arm"].items():
        lines.append(
            f"  {arm}: issues {row['issues']} (+{row['not_run_quota']} refused for quota, excluded), PRs {row['prs']} (no PR {row['no_pr']}), merged {row['merged']}, "
            f"durable {row['durable']}, broke later {row['broke_later']}, closed unmerged {row['closed_unmerged']}, "
            f"open {row['open']}, verifier PASS {row['verifier_pass']}, cost "
            f"{'UNMEASURED' if row['cost_usd'] is None else '$' + str(row['cost_usd'])}"
        )
    lines.append(f"verdict: {out['verdict']}")
    lines.append(
        "evidence class: production use on live opener work (randomised by issue hash); "
        "durability is the feedback DB's post-merge judgement"
    )
    return "\n".join(lines)


def _selftest() -> None:
    import tempfile

    arms = ["gemini-4-argon-high", "gemini-4-carbon-high"]
    seen = {arm_for(f"o/r#{n}", arms)[0] for n in range(40)}
    assert seen == set(arms), seen  # both arms are reachable
    assert arm_for("o/r#7", arms) == arm_for("o/r#7", arms)  # a retry keeps its arm
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "spec.json"
        assert load_spec(path=path)[0] is None
        path.write_text(
            json.dumps(
                {
                    "policy": POLICY,
                    "arms": arms,
                    "start_ts": 10,
                    "end_ts": 20,
                    "target_prs": 30,
                    "max_assignments": 60,
                }
            )
        )
        assert load_spec(15, path)[0] is not None
        assert load_spec(25, path)[1] == "window ended"
        assert load_spec(5, path)[1] == "window not started"
    assert _verdict({}, arms).startswith("gemini-4-carbon-high: INCONCLUSIVE")
    print(
        "gemini_full_split.py selftest: OK (deterministic arms, bounded window, inconclusive floor)"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--selftest", action="store_true")
    parser.add_argument("--readout", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--status", action="store_true", help="spec state and counts, no GitHub calls"
    )
    args = parser.parse_args(argv)
    if args.selftest:
        _selftest()
        return 0
    if args.status:
        spec, why = load_spec()
        assigned = assigned_targets()
        prs = pr_count()
        limit = spec["target_prs"] if spec else "-"
        # Blocking and drainable together: assignment continues while PRs < target and the window is open.
        print(
            f"gemini full split: spec {why}; issues assigned {len(assigned)}; PRs (last readout) "
            f"{prs if prs is not None else 'never read'}/{limit}; drainable: "
            f"{'yes' if spec and (prs or 0) < spec['target_prs'] else 'no'}"
        )
        return 0
    out = readout()
    print(
        json.dumps({k: v for k, v in out.items() if k != "rows"}, indent=2)
        if args.json
        else render(out)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
