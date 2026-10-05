"""Join closed issue task counts to closing-PR quality; no dispatch or lifecycle writes.

Extends the existing role-decomposer evidence and weekly switch review. The Brain and
fleet-shapes facts already own outcome and merge evidence; no second outcome store is built.
Only complete, independently dated observations spanning two weeks can decide the issue-level
wiring claim. Missing population, closing references or metric samples never count as zero.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import feedback
import fleet_shapes
import outcomes
import utc_epoch

BANDS = ("0", "1-4", "5-8", "9-15", "16+")
WINDOW_DAYS = 90
METRICS = ("pass", "durable", "broke_later", "verifier_non_pass")


def size_band(count: int) -> str:
    return BANDS[
        0 if count == 0 else 1 if count <= 4 else 2 if count <= 8 else 3 if count <= 15 else 4
    ]


def fleet_repos() -> list[str]:
    """Same reviewed-fleet extension the weekly value-chain collector uses."""
    from backlog import SUPPORTED_REPOS

    return list(
        dict.fromkeys(
            [
                *SUPPORTED_REPOS,
                "stranske/Orchestrator",
                "stranske/Doc-Lineage",
                "stranske/Deliverable-Render",
                "stranske/Manager-Mosaic",
            ]
        )
    )


def collect_issues(gh_fn, repos: list[str], *, now: int) -> tuple[list[dict], list[str]]:
    """Partition search by repo so its 1000-result ceiling cannot truncate the whole fleet."""
    rows: list[dict] = []
    errors: list[str] = []
    for repo in dict.fromkeys(repos):
        found, failed = _collect_repo(gh_fn, repo, now=now)
        rows.extend(found)
        errors.extend(failed)
    return rows, errors


def _collect_repo(gh_fn, repo: str, *, now: int) -> tuple[list[dict], list[str]]:
    since = datetime.fromtimestamp(now - WINDOW_DAYS * 86400, timezone.utc).date().isoformat()
    query_text = "is:issue is:closed closed:>=" + since + " repo:" + repo
    query = """query($q:String!,$cursor:String){search(query:$q,type:ISSUE,first:100,after:$cursor){
      issueCount pageInfo{hasNextPage endCursor} nodes{... on Issue{number body closedAt
      repository{nameWithOwner} closedByPullRequestsReferences(first:100){pageInfo{hasNextPage}
      nodes{number state mergedAt repository{nameWithOwner}}}}}}}"""
    rows: list[dict] = []
    errors: list[str] = []
    cursor = None
    seen = set()
    for _ in range(10):
        args = ["api", "graphql", "-f", "query=" + query, "-f", "q=" + query_text]
        if cursor:
            args += ["-f", "cursor=" + cursor]
        ok, raw, error = gh_fn(args)
        if not ok:
            errors.append(error or "closed issue population read failed")
            break
        try:
            payload = json.loads(raw)
            if payload.get("errors"):
                raise ValueError(str(payload["errors"]))
            conn = payload["data"]["search"]
            for issue in conn["nodes"]:
                if not isinstance(issue.get("body"), str):
                    raise ValueError("issue body unavailable")
                if issue["repository"]["nameWithOwner"] != repo:
                    raise ValueError("issue returned outside the declared repository")
                rows.append(issue)
                if issue["closedByPullRequestsReferences"]["pageInfo"]["hasNextPage"]:
                    errors.append(
                        f"closing references truncated for {issue['repository']['nameWithOwner']}#{issue['number']}"
                    )
            if conn["issueCount"] > 1000:
                errors.append("GitHub search population exceeds its 1000-result ceiling")
            if not conn["pageInfo"]["hasNextPage"]:
                break
            cursor = conn["pageInfo"]["endCursor"]
            if not cursor or cursor in seen:
                raise ValueError("closed issue pagination stalled")
            seen.add(cursor)
        except (KeyError, TypeError, ValueError) as exc:
            errors.append(f"closed issue population malformed: {exc}")
            break
    else:
        errors.append("closed issue population pagination incomplete")
    return rows, list(dict.fromkeys(errors))


def brain_outcomes(db: Path) -> dict[str, list[dict]]:
    """Read the canonical Brain without migrations or writes, retaining conflicting verdicts."""
    result: dict[str, list[dict]] = {}
    with sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT r.target,r.routing_metadata,o.* FROM runs r JOIN outcomes o USING(run_id) "
            "WHERE o.merged=1 AND r.source='keepalive' AND r.agent NOT IN ('','none')"
        )
        for row in rows:
            item = dict(row)
            attribution = feedback._routing_metadata_dict(item.pop("routing_metadata")).get(
                "attribution_source", ""
            )
            if attribution == "human" or str(attribution).startswith("bot:"):
                continue
            if item.get("failure_class") in feedback.LEARNING_EXCLUDED_FAILURE_CLASSES:
                continue
            result.setdefault(item.pop("target"), []).append(item)
    return result


def curve(issues: list[dict], outcomes_by_pr: dict, facts: dict, *, now: int, errors=()) -> dict:
    cells: dict[str, dict[str, Any]] = {
        band: {"issues": 0, "joined": 0, **{m: {"n": 0, "yes": 0} for m in METRICS}}
        for band in BANDS
    }
    missing = []
    seen = set()
    for issue in issues:
        ref = f"{issue['repository']['nameWithOwner']}#{issue['number']}"
        if ref in seen:
            continue
        seen.add(ref)
        count = len(re.findall(r"^\s*[-*+]\s+\[[ xX]\]", issue["body"], re.MULTILINE))
        cell = cells[size_band(count)]
        cell["issues"] += 1
        closed = utc_epoch.from_iso(issue.get("closedAt"))
        prs = issue.get("closedByPullRequestsReferences", {})
        refs = []
        if closed is not None and prs.get("pageInfo", {}).get("hasNextPage") is False:
            refs = [
                f"{p['repository']['nameWithOwner']}#{p['number']}"
                for p in prs.get("nodes", [])
                if outcomes._merged_by_close(p, closed) is True
            ]
        rows = [row for pr in refs for row in outcomes_by_pr.get(pr, [])]
        if not rows or any(not outcomes_by_pr.get(pr) for pr in refs):
            missing.append(ref)
            continue
        cell["joined"] += 1
        verdicts = {
            str(row.get("adjudicated_verdict") or row.get("verifier_verdict") or "").upper()
            for row in rows
        } - {""}
        if len(verdicts) == 1 and verdicts <= {"PASS", "FAIL", "CONCERNS"}:
            verdict = next(iter(verdicts))
            cell["pass"]["n"] += 1
            cell["pass"]["yes"] += int(verdict == "PASS")
        # Preserve the verifier's original judgment even if adjudication ultimately accepted it.
        raw_verdicts = {str(row.get("verifier_verdict") or "").upper() for row in rows} - {""}
        if len(raw_verdicts) == 1 and raw_verdicts <= {"PASS", "FAIL", "CONCERNS"}:
            cell["verifier_non_pass"]["n"] += 1
            cell["verifier_non_pass"]["yes"] += int(next(iter(raw_verdicts)) != "PASS")
        # Fleet facts own the PR merge time; young or unread merge evidence cannot grade durability.
        mature = all(
            isinstance(facts.get(pr, {}).get("merged_ts"), (int, float))
            and now - facts[pr]["merged_ts"] >= fleet_shapes.DURABILITY_MIN_AGE_DAYS * 86400
            for pr in refs
        )
        labels = {row.get("durability") for row in rows}
        checked = all(
            (row.get("durability_checked_ts") or 0) >= feedback.DURABILITY_DETECTION_SINCE
            for row in rows
        )
        if (
            mature
            and checked
            and len(labels) == 1
            and labels <= {"durable", *fleet_shapes.BAD_DURABILITY}
        ):
            label = next(iter(labels))
            for metric, yes in (
                ("durable", label == "durable"),
                ("broke_later", label in fleet_shapes.BAD_DURABILITY),
            ):
                cell[metric]["n"] += 1
                cell[metric]["yes"] += int(yes)
    bands = []
    for band, cell in cells.items():
        for metric in METRICS:
            sample = cell[metric]
            sample["rate"] = sample["yes"] / sample["n"] if sample["n"] else None
        bands.append({"band": band, **cell})
    return {
        "schema": "orchestrator.issue-size-quality/v1",
        "generated_at": now,
        "window_days": WINDOW_DAYS,
        "status": "partial" if errors else "ok",
        "errors": list(errors),
        "issue_count": len(seen),
        "missing_outcomes": missing,
        "bands": bands,
    }


def decision(observations: list[dict], *, now: int) -> str:
    scope = (
        max(observations, key=lambda r: r.get("generated_at", 0)).get("repositories")
        if observations
        else None
    )
    complete = [
        r
        for r in observations
        if r.get("status") == "ok"
        and r.get("window_days") == WINDOW_DAYS
        and r.get("schema") == "orchestrator.issue-size-quality/v1"
        and r.get("repositories") == scope
        and 0 <= now - r.get("generated_at", now + 1) <= 21 * 86400
    ]
    if (
        len(complete) < 2
        or max(r["generated_at"] for r in complete) - min(r["generated_at"] for r in complete)
        < 14 * 86400
    ):
        return "collect_two_weeks"
    latest = max(complete, key=lambda r: r["generated_at"])
    if now - latest["generated_at"] > 8 * 86400:
        return "unmeasured_stale"
    cells = {r["band"]: r for r in latest["bands"]}
    small, large = cells["1-4"], cells["16+"]
    gaps = []
    for metric in METRICS:
        a, b = small[metric]["rate"], large[metric]["rate"]
        if a is None or b is None:
            return "unmeasured_quality"
        gaps.append(a - b if metric in ("pass", "durable") else b - a)
    return (
        "file_opener_bucket_wiring_issue"
        if round(max(gaps), 10) >= 0.10
        else "retire_issue_level_claim"
    )


def format_lines(rep: dict) -> list[str]:
    lines = ["## Issue task-count delivery quality", ""]
    for cell in rep.get("bands", []):
        rates = ", ".join(
            f"{m}={'unmeasured' if cell[m]['rate'] is None else format(cell[m]['rate'], '.1%')} (n={cell[m]['n']})"
            for m in METRICS
        )
        lines.append(f"  {cell['band']}: issues={cell['issues']}, joined={cell['joined']}; {rates}")
    lines += [
        f"  decision: {rep.get('decision', 'unmeasured')}; status: {rep.get('status', 'unknown')}"
    ]
    lines.extend(f"  UNKNOWN: {error}" for error in rep.get("errors", []))
    return lines + [""]


def run(
    *,
    gh_fn,
    repos: list[str],
    state_dir: Path | None = None,
    db: Path | None = None,
    now: int | None = None,
) -> dict:
    now = int(time.time()) if now is None else now
    state = state_dir or fleet_shapes.default_state_dir()
    dest = state / "capability-program" / "size-quality.json"
    try:
        history = json.loads(dest.read_text()).get("observations", [])
    except (OSError, ValueError):
        history = []
    issues, errors = collect_issues(gh_fn, repos, now=now)
    try:
        rows = brain_outcomes(db or feedback.DB_PATH)
        facts = fleet_shapes.load_facts(state)
    except (OSError, sqlite3.Error) as exc:
        errors.append(f"Brain or fleet facts unavailable: {exc}")
        rows, facts = {}, {}
    rep = curve(issues, rows, facts, now=now, errors=errors)
    rep["repositories"] = sorted(repos)
    # One snapshot per week; repeated readers cannot manufacture two weeks of observations.
    history = [
        r
        for r in history
        if now - r.get("generated_at", 0) <= 21 * 86400
        and r.get("generated_at", 0) // (7 * 86400) != now // (7 * 86400)
    ] + [rep]
    result = {**rep, "observations": history, "decision": decision(history, now=now)}
    fleet_shapes.write_json_atomic(dest, result)
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--state-dir", type=Path)
    args = parser.parse_args(argv)
    from switch_review import _gh_call

    rep = run(gh_fn=_gh_call, repos=fleet_repos(), state_dir=args.state_dir)
    print(json.dumps(rep, indent=2) if args.json else "\n".join(format_lines(rep)))
    return 0 if rep["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
