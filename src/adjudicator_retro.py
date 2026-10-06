#!/usr/bin/env python3
"""Replay persisted verifier disputes through the existing shadow adjudicator.

Only role-run evidence and the report are written. Outcomes remain read-only;
missing verifier, diff, gate or later durability evidence stays unmeasured.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import feedback
import fleet_shapes
import roles
import verifier_evidence


def report_path() -> Path:
    return Path(os.environ.get("ORCH_STATE_DIR", Path.home() / ".codex/orchestrator")) / (
        "capability-program/adjudicator-retro.json"
    )


def disputes(*, db: Path | None = None, now: int | None = None, days: int = 90) -> list[dict]:
    """Read the existing Brain, without schema initialization or outcome writes."""
    with sqlite3.connect(f"file:{(db or feedback.DB_PATH).resolve()}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        return [
            dict(row)
            for row in conn.execute(
                "SELECT r.run_id,r.target,r.ts,o.verifier_verdict,o.adjudicated_verdict,"
                "o.merged,o.durability,o.durability_checked_ts FROM runs r JOIN outcomes o "
                "USING(run_id) WHERE r.ts>=? AND o.verifier_verdict IS NOT NULL "
                "AND o.adjudicated_verdict IS NOT NULL "
                "AND o.verifier_verdict!=o.adjudicated_verdict ORDER BY r.ts DESC,r.run_id",
                ((now if now is not None else int(time.time())) - days * 86400,),
            )
        ]


def build_packet(row: dict, evidence: dict) -> dict:
    """Require actual finding text and merge-bound diff/gate evidence, not merge=PASS."""
    case = {
        "target": row.get("target"),
        "source": "retrospective",
        "metadata_only": True,
        "disputed_finding": evidence.get("disputed_finding"),
        "ground_truth_evidence": evidence.get("ground_truth_evidence"),
    }
    if roles._validate_adjudication_case(case):
        raise ValueError("packet requires target, disputed_finding and ground_truth_evidence")
    finding = case["disputed_finding"]
    if not isinstance(finding, dict) or not isinstance(finding.get("body"), str):
        raise ValueError("packet requires verifier finding comment text")
    if not verifier_evidence.MARKER_RE.sub("", finding["body"]).strip():
        raise ValueError("packet requires verifier finding comment text")
    ground_truth = case["ground_truth_evidence"]
    if not isinstance(ground_truth, dict):
        raise ValueError("packet requires merged diff summary and gate runs")
    diff = ground_truth.get("diff_summary")
    gates = ground_truth.get("gate_runs")
    has_diff = (
        bool(diff.strip()) if isinstance(diff, str) else isinstance(diff, list) and bool(diff)
    )
    if not has_diff:
        raise ValueError("packet requires merged diff summary")
    if not isinstance(gates, list) or not gates:
        raise ValueError("packet requires gate runs")
    return case


def _gh_json(args: list[str]) -> dict:
    # Operators may wrap the entire CLI with detached-net; no GitHub mutations occur here.
    proc = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=120)
    if proc.returncode:
        raise RuntimeError(proc.stderr.strip() or "GitHub evidence read failed")
    return json.loads(proc.stdout)


def fetch_evidence(row: dict) -> dict:
    repo, sep, number = str(row.get("target") or "").partition("#")
    if not sep or not number.isdigit() or repo.count("/") != 1:
        raise ValueError("dispute target is not a PR key")
    owner, name = repo.split("/")
    query = """query($owner:String!,$name:String!,$n:Int!){repository(owner:$owner,name:$name){
      pullRequest(number:$n){number state headRefOid mergeCommit{oid}
      files(first:100){pageInfo{hasNextPage} nodes{path additions deletions}}
      comments(last:100){pageInfo{hasPreviousPage} nodes{body url author{login}}}
      commits(last:1){nodes{commit{statusCheckRollup{contexts(first:100){
        pageInfo{hasNextPage endCursor} nodes{__typename ... on CheckRun{name conclusion detailsUrl}
        ... on StatusContext{context state targetUrl}}}}}}}}}}"""
    pr = _gh_json(
        [
            "api",
            "graphql",
            "-f",
            "query=" + query,
            "-f",
            "owner=" + owner,
            "-f",
            "name=" + name,
            "-F",
            "n=" + number,
        ]
    )["data"]["repository"]["pullRequest"]
    if not pr or pr["state"] != "MERGED":
        raise ValueError("merged PR evidence unavailable")
    if not (pr.get("mergeCommit") or {}).get("oid"):
        raise ValueError(
            "merged PR merge commit unavailable; retry when merge-bound evidence exists"
        )
    if pr["comments"]["pageInfo"]["hasPreviousPage"] or pr["files"]["pageInfo"]["hasNextPage"]:
        raise ValueError("truncated verifier comment or diff evidence")
    decision = verifier_evidence.decision_from_pr(repo, pr)
    if not decision or decision["verdict"] != row["verifier_verdict"]:
        raise ValueError("current merge-bound verifier decision missing or changed")
    comments = pr["comments"]["nodes"]
    finding = next(
        (
            c
            for c in reversed(comments)
            if (
                verifier_evidence.decision_from_pr(repo, {**pr, "comments": {"nodes": [c]}})
                == decision
            )
        ),
        None,
    )
    if finding is None:
        raise ValueError("merge-bound verifier finding comment unavailable")
    rollup = pr["commits"]["nodes"][0]["commit"]["statusCheckRollup"]
    if not rollup:
        raise ValueError("complete gate evidence unavailable")
    contexts = rollup["contexts"]
    pages = 1
    while contexts["pageInfo"]["hasNextPage"]:
        if pages >= 20:
            raise ValueError("gate evidence exceeds bounded 20-page read")
        gate_query = """query($owner:String!,$name:String!,$n:Int!,$cursor:String!){
          repository(owner:$owner,name:$name){pullRequest(number:$n){headRefOid
          commits(last:1){nodes{commit{statusCheckRollup{contexts(first:100,after:$cursor){
          pageInfo{hasNextPage endCursor} nodes{__typename ... on CheckRun{name conclusion detailsUrl}
          ... on StatusContext{context state targetUrl}}}}}}}}}}"""
        fresh = _gh_json(
            [
                "api",
                "graphql",
                "-f",
                "query=" + gate_query,
                "-f",
                "owner=" + owner,
                "-f",
                "name=" + name,
                "-F",
                "n=" + number,
                "-f",
                "cursor=" + contexts["pageInfo"]["endCursor"],
            ]
        )["data"]["repository"]["pullRequest"]
        if fresh["headRefOid"] != pr["headRefOid"]:
            raise ValueError("PR head changed while reading gate evidence")
        page = fresh["commits"]["nodes"][0]["commit"]["statusCheckRollup"]["contexts"]
        contexts["nodes"].extend(page["nodes"])
        contexts["pageInfo"] = page["pageInfo"]
        pages += 1
    gates = [
        c for c in contexts["nodes"] if "gate" in str(c.get("name", c.get("context", ""))).lower()
    ]
    if not gates:
        raise ValueError("no gate run in exact-head evidence")
    return {
        "disputed_finding": {"ref": finding["url"], "body": finding["body"], "decision": decision},
        "ground_truth_evidence": {
            "head_sha": pr["headRefOid"],
            "merge_sha": pr["mergeCommit"]["oid"],
            "diff_ref": f"https://github.com/{repo}/pull/{number}/files",
            "diff_summary": pr["files"]["nodes"],
            "gate_runs": gates,
        },
    }


def later_truth(row: dict) -> str | None:
    """Use only judged post-detection durability, the same labels fleet_shapes consumes."""
    checked = row.get("durability_checked_ts")
    if not isinstance(checked, int) or checked < feedback.DURABILITY_DETECTION_SINCE:
        return None
    durability = row.get("durability")
    if durability == "durable":
        return "PASS"
    if durability in fleet_shapes.BAD_DURABILITY:
        return "FAIL"
    return None


def measured_cost(run_id: str | None, db: Path | None = None) -> float | None:
    if not run_id:
        return None
    with sqlite3.connect(f"file:{(db or feedback.DB_PATH).resolve()}?mode=ro", uri=True) as conn:
        row = conn.execute("SELECT cost_usd,source FROM costs WHERE run_id=?", (run_id,)).fetchone()
    return row[0] if row and row[1] in feedback.COMPLETE_COST_SOURCES else None


def _refresh_saved_verdicts(rows: list[dict], db: Path | None = None) -> None:
    """Grade saved verdicts even after their disputes leave the replay window."""
    verdicts = [row for row in rows if row.get("decision")]
    if not verdicts:
        return
    with sqlite3.connect(f"file:{(db or feedback.DB_PATH).resolve()}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        for row in verdicts:
            outcome = conn.execute(
                "SELECT merged,durability,durability_checked_ts FROM outcomes WHERE run_id=?",
                (row["run_id"],),
            ).fetchone()
            row["later_truth"] = later_truth(dict(outcome)) if outcome else None
            merged = outcome["merged"] if outcome else None
            row["merge_rule_verdict"] = (
                ("PASS" if merged else "FAIL") if merged is not None else None
            )
            row["cost_usd"] = measured_cost(row.get("backend_run_id"), db)


def _apply_evidence_floor(row: dict) -> None:
    """Preserve raw historical proposals; separate their effective disposition.

    This report's producer has never supplied inspected source/artifact contents.
    Legacy rows and explicit false flags therefore cannot upgrade its evidence.
    Applying the floor is local and requires no repeated backend call.
    """
    row["metadata_only"] = True
    row["disposition"] = "needs_more_evidence"
    if row.get("shadow_verdict") is not None:
        row.setdefault("raw_shadow_verdict", row["shadow_verdict"])
    row["shadow_verdict"] = None
    row["disposition_reason"] = (
        "Retrospective diff counts and gate statuses do not establish inspected "
        "source, byte parity or complete acceptance artifacts."
    )


def summarize(rows: list[dict]) -> dict:
    # Classify independently of stored flags, including pre-floor legacy rows.
    classified = [dict(row) for row in rows]
    for row in classified:
        _apply_evidence_floor(row)
    accepted = [
        row for row in classified if row.get("disposition") in {"uphold_blocker", "reject_blocker"}
    ]
    graded = [r for r in accepted if r.get("later_truth")]
    agree = sum(r["shadow_verdict"] == r["later_truth"] for r in graded)
    baseline = sum(r["merge_rule_verdict"] == r["later_truth"] for r in graded)
    measured_costs = [r["cost_usd"] for r in rows if isinstance(r.get("cost_usd"), (int, float))]
    return {
        "cases": len(rows),
        "adjudicated": len(accepted),
        "proposed_decisions": sum(
            r.get("decision") in {"uphold_blocker", "reject_blocker"} for r in rows
        ),
        "metadata_only_cases": len(rows),
        "graded": len(graded),
        "agree": agree,
        "disagree": len(graded) - agree,
        "agreement_rate": agree / len(graded) if graded else None,
        "merge_rule_agreement_rate": baseline / len(graded) if graded else None,
        "cost_usd": sum(measured_costs) if measured_costs else None,
        "cost_measured_cases": len(measured_costs),
        "cost_per_case": sum(measured_costs) / len(measured_costs) if measured_costs else None,
    }


def compare_proposals(rows: list[dict]) -> dict:
    """Compare raw metadata proposals, without granting them effective verdicts.

    Both rates use the same cases with a binary proposal, judged later truth and
    known merge disposition. Abstentions and missing evidence stay outside that
    denominator. This measures retrospective correlation, not accepted adjudication.
    """
    verdicts = {"uphold_blocker": "FAIL", "reject_blocker": "PASS"}
    compared = []
    proposed = pending = missing_merge = abstained = 0
    for row in rows:
        decision = row.get("decision")
        verdict = verdicts.get(decision) if isinstance(decision, str) else None
        truth = row.get("later_truth")
        baseline = row.get("merge_rule_verdict")
        comparable = (
            verdict is not None and truth in {"PASS", "FAIL"} and baseline in {"PASS", "FAIL"}
        )
        row["proposal_comparison"] = {
            "verdict": verdict,
            "agrees": verdict == truth if comparable else None,
            "merge_rule_agrees": baseline == truth if comparable else None,
        }
        if verdict is not None:
            proposed += 1
            if truth not in {"PASS", "FAIL"}:
                pending += 1
            elif baseline not in {"PASS", "FAIL"}:
                missing_merge += 1
        elif row.get("decision") == "needs_more_evidence":
            abstained += 1
        if comparable:
            compared.append(row["proposal_comparison"])
    agree = sum(case["agrees"] for case in compared)
    baseline_agree = sum(case["merge_rule_agrees"] for case in compared)
    return {
        "evidence_basis": "raw_metadata_proposals",
        "cases": len(rows),
        "proposed_decisions": proposed,
        "compared": len(compared),
        "pending_truth": pending,
        "missing_merge_disposition": missing_merge,
        "abstained": abstained,
        "unassessed": len(rows) - proposed - abstained,
        "agree": agree,
        "disagree": len(compared) - agree,
        "agreement_rate": agree / len(compared) if compared else None,
        "merge_rule_agree": baseline_agree,
        "merge_rule_disagree": len(compared) - baseline_agree,
        "merge_rule_agreement_rate": baseline_agree / len(compared) if compared else None,
    }


def run(
    *,
    dispatch: bool = False,
    limit: int = 5,
    path: Path | None = None,
    db: Path | None = None,
    evidence_reader=None,
    runner=None,
    retry: bool = False,
) -> dict:
    """Bounded, resumable shadow run; invalid packets are recorded and never dispatched."""
    path = path or report_path()
    reader = evidence_reader or fetch_evidence
    runner = runner or roles.run_adjudicator_agent
    previous = json.loads(path.read_text()) if path.exists() else {}
    saved = {r["case_id"]: r for r in previous.get("rows", [])}
    _refresh_saved_verdicts(list(saved.values()), db)
    population = disputes(db=db)
    attempted = 0
    # A paid verdict can outlive the replay window while its Brain write is
    # pending. Repair only the original role record, never redispatch that case.
    if dispatch and retry:
        for entry in saved.values():
            record = entry.get("role_record")
            if not entry.get("role_record_error") or not record:
                continue
            if attempted >= max(0, limit):
                break
            attempted += 1
            try:
                feedback.record_role_run(**record)
                entry["role_run_id"] = record["run_id"]
                entry["role_record_error"] = None
            except Exception as exc:
                entry["role_record_error"] = str(exc)
            _persist_report(path, list(saved.values()), len(population))
    for row in population:
        identity = {k: row[k] for k in ("run_id", "verifier_verdict", "adjudicated_verdict")}
        case_id = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        old = saved.get(case_id)
        if old and (old.get("decision") or not retry):
            if old.get("decision"):
                old["later_truth"] = later_truth(row)
                old["cost_usd"] = measured_cost(old.get("backend_run_id"), db)
            continue
        if attempted >= max(0, limit):
            continue
        attempted += 1
        entry = {
            **identity,
            "case_id": case_id,
            "target": row["target"],
            "later_truth": later_truth(row),
            "merge_rule_verdict": (
                ("PASS" if row["merged"] else "FAIL") if row["merged"] is not None else None
            ),
            "cost_usd": None,
        }
        try:
            packet = build_packet(row, reader(row))
            entry["packet"] = packet
            if dispatch:
                path.parent.mkdir(parents=True, exist_ok=True)
                result = runner(
                    case=packet,
                    dispatch=True,
                    source="retrospective",
                    cwd=str(path.parent),
                    timeout=180,
                )
                entry.update(
                    {
                        k: result.get(k)
                        for k in (
                            "role_run_id",
                            "backend_run_id",
                            "backend",
                            "errors",
                            "role_record_error",
                            "role_record",
                        )
                    }
                )
                if result.get("proposal") and not result.get("errors"):
                    entry["decision"] = result["proposal"]["decision"]
                    entry["proposal"] = result["proposal"]
                    entry["disposition"] = (result.get("advisory_plan") or {}).get(
                        "decision", "needs_more_evidence"
                    )
                    _apply_evidence_floor(entry)
                entry["cost_usd"] = measured_cost(result.get("backend_run_id"), db)
        except (
            ValueError,
            KeyError,
            TypeError,
            RuntimeError,
            OSError,
            sqlite3.Error,
            subprocess.TimeoutExpired,
        ) as exc:
            entry["error"] = str(exc)
        saved[case_id] = entry
        # Persist every attempt so an interrupted batch does not repeat successful paid calls.
        _persist_report(path, list(saved.values()), len(population))
    # Saved cases can gain judged durability or measured cost without a new
    # paid attempt. Publish those refreshes for the weekly file reader too.
    return _persist_report(path, list(saved.values()), len(population))


def _persist_report(path: Path, rows: list[dict], population: int) -> dict:
    """Atomically publish the same shadow evidence returned to the caller."""
    for row in rows:
        _apply_evidence_floor(row)
    comparison = compare_proposals(rows)
    summary = summarize(rows)
    comparison.update(
        {key: summary[key] for key in ("cost_usd", "cost_measured_cases", "cost_per_case")}
    )
    report = {
        "generated_at": int(time.time()),
        "shadow": True,
        "source": "retrospective",
        "population": population,
        "rows": rows,
        "summary": summary,
        "proposal_comparison": comparison,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=path.name + ".",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(json.dumps(report, indent=2) + "\n")
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return report


def weekly_line(path: Path | None = None) -> str:
    try:
        summary = json.loads((path or report_path()).read_text())["summary"]
        cost = summary["cost_usd"]
        return (
            f"adjudicator shadow: cases {summary['cases']}, agree {summary['agree']}, "
            f"disagree {summary['disagree']}, cost {cost if cost is not None else 'UNKNOWN'} "
            f"(graded {summary['graded']}; costs measured {summary['cost_measured_cases']})"
        )
    except (OSError, ValueError, KeyError, TypeError):
        return "adjudicator shadow: cases UNKNOWN, agree UNKNOWN, disagree UNKNOWN, cost UNKNOWN"


def _selftest() -> None:
    try:
        build_packet({"target": "owner/repo#1"}, {})
    except ValueError:
        pass
    else:
        raise AssertionError("absent evidence was accepted")
    assert summarize([])["agreement_rate"] is None
    assert later_truth({"durability": "pending"}) is None
    print("adjudicator_retro selftest: 3 checks passed")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--selftest", action="store_true")
    parser.add_argument(
        "--dispatch",
        action="store_true",
        help="Run router-chosen shadow role; never apply verdicts",
    )
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument(
        "--retry",
        action="store_true",
        help="Repair pending Brain records without redispatch; retry failed evidence attempts",
    )
    args = parser.parse_args()
    if args.selftest:
        _selftest()
        return 0
    result = run(dispatch=args.dispatch, limit=args.limit, retry=args.retry)
    print(json.dumps(result["summary"], indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
