#!/usr/bin/env python3
"""Compare the existing TriageAgent with opener ordering; never drive a worker.

The active tick calls this independently of ORCH_DISPATCH_LANE. GitHub access
is search/read only. Observations are local; a fresh ranking may be consumed behind the default-OFF
ORCH_TRIAGE_CONSUME flag. Consumption never dispatches or hides candidates. ORCH_TRIAGE_SHADOW=0 disables the caller.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

import backlog
import capabilities
import feedback
import outcomes
import roles


def corpus_path() -> Path:
    return Path(os.environ.get("ORCH_STATE_DIR", str(Path.home() / ".codex/orchestrator"))) / (
        "capability-program/triage-shadow.jsonl"
    )


RANKING_MAX_AGE_S = 7200
MIN_CONSUME_CYCLES = 20


def ranking_path() -> Path:
    return corpus_path().with_name("triage-ranking.json")


def consumed_path() -> Path:
    return corpus_path().with_name("triage-consumed.jsonl")


def linked_path() -> Path:
    return corpus_path().with_name("triage-linked.jsonl")


def _append(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(row, sort_keys=True) + "\n")


def _read_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    if any(not isinstance(row, dict) for row in rows):
        raise ValueError(f"malformed rows: {path.name}")
    return rows


def _publish_ranking(row: dict, ordered_targets: list[str]) -> None:
    path = ranking_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    ranking = {key: row[key] for key in ("ts", "role_run_id", "snapshot_sha256")}
    ranking.update(ordered_targets=ordered_targets, max_age_s=RANKING_MAX_AGE_S)
    # Replace only after complete bytes are durable; readers see one snapshot.
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=".triage-ranking-", delete=False
        ) as stream:
            tmp = Path(stream.name)
            json.dump(ranking, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)


def rule_key(item: dict) -> tuple:
    labels = set(backlog._labels(item))
    tier = next(
        (i for i, name in enumerate(("high", "normal", "low")) if "priority:" + name in labels), 3
    )
    return tier, item.get("createdAt", ""), "repo-review-approved" not in labels, item["target"]


def candidates_from_search(
    rows: list[dict],
    supported: list[str],
    *,
    scoped: set[str] | None = None,
    linked: set[str] | None = None,
) -> list[dict]:
    candidates = {}
    for row in rows:
        repo = row.get("repository") or ""
        if isinstance(repo, dict):
            repo = repo.get("nameWithOwner", "")
        labels = backlog._labels(row)
        title = row.get("title", "")
        if repo not in supported or set(labels) & {"needs-human", "agents:paused", "deeper_review"}:
            continue
        if title.startswith("⚠️ CODEX_AUTH_JSON expires"):
            continue
        target = f"{repo}#{row['number']}"
        if repo in (scoped or set()) or target in (scoped or set()) or target in (linked or set()):
            continue
        candidates[target] = {
            **row,
            "repository": repo,
            "target": target,
            "lane": "opener",
            "task_type": backlog.classify(labels),
        }
    return sorted(candidates.values(), key=rule_key)


def discover_candidates(run=None) -> list[dict]:
    """Use the opener's three searches; refuse a truncated or failed population."""
    run = run or subprocess.run
    rows = []
    for tier in ("high", "normal", "low"):
        proc = run(
            [
                "gh",
                "search",
                "issues",
                "--owner",
                "stranske",
                "--label",
                "priority:" + tier,
                "--state",
                "open",
                "--json",
                "repository,number,title,labels,createdAt,body",
                "--limit",
                "1000",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if proc.returncode:
            raise RuntimeError(proc.stderr.strip() or "issue search failed")
        found = json.loads(proc.stdout)
        if not isinstance(found, list) or len(found) >= 1000:
            raise ValueError("issue search population is malformed or truncated")
        for item in found:
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("repository"), dict)
                or not isinstance(item["repository"].get("nameWithOwner"), str)
                or not item["repository"]["nameWithOwner"]
            ):
                raise ValueError("issue search repository is UNKNOWN")
        rows.extend(found)
    # Map closing references, body references and branch source identifiers using
    # the existing intake resolver. A read failure never means "unlinked".
    linked = set()
    for repo in sorted(
        {
            row["repository"]["nameWithOwner"]
            for row in rows
            if row["repository"]["nameWithOwner"] in backlog.SUPPORTED_REPOS
        }
    ):
        proc = run(
            [
                "gh",
                "pr",
                "list",
                "--repo",
                repo,
                "--state",
                "open",
                "--limit",
                "1000",
                "--json",
                "number,body,headRefName,closingIssuesReferences",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if proc.returncode:
            raise RuntimeError(proc.stderr.strip() or "PR linkage read failed")
        prs = json.loads(proc.stdout)
        if not isinstance(prs, list) or len(prs) >= 1000:
            raise ValueError("PR linkage population is malformed or truncated")
        for pr in prs:
            linked.update(backlog._issue_targets(repo, pr))
    if backlog.scoped_blocker_source() not in ("ok", "absent"):
        raise ValueError("scoped blocker population is UNKNOWN")
    scoped = backlog.load_scoped_blockers() | {
        target
        for target, entry in backlog.scoped_blocker_entries().items()
        if isinstance(entry, dict) and entry.get("await_human")
    }
    candidates = candidates_from_search(rows, backlog.SUPPORTED_REPOS, scoped=scoped, linked=linked)
    # A still-open source already delivered through a merged closing PR is
    # verifier work, not another implementation candidate.
    eligible = []
    for item in candidates:
        owner, repo = item["repository"].split("/", 1)
        query = (
            "query($owner:String!,$repo:String!,$n:Int!){repository(owner:$owner,name:$repo){"
            "issue(number:$n){closedByPullRequestsReferences(first:100){nodes{state} "
            "pageInfo{hasNextPage}}}}}"
        )
        proc = run(
            [
                "gh",
                "api",
                "graphql",
                "-f",
                "query=" + query,
                "-f",
                "owner=" + owner,
                "-f",
                "repo=" + repo,
                "-F",
                "n=" + str(item["number"]),
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if proc.returncode:
            raise RuntimeError(proc.stderr.strip() or "merged linkage read failed")
        data = json.loads(proc.stdout)
        if not isinstance(data, dict) or data.get("errors"):
            raise ValueError("merged linkage population is UNKNOWN")
        try:
            refs = data["data"]["repository"]["issue"]["closedByPullRequestsReferences"]
            if refs["pageInfo"]["hasNextPage"] is True:
                raise ValueError("merged linkage population is truncated")
            if refs["pageInfo"]["hasNextPage"] is not False:
                raise ValueError("merged linkage pagination is UNKNOWN")
            nodes = refs["nodes"]
            if not isinstance(nodes, list) or any(
                not isinstance(pr, dict) or pr.get("state") not in {"OPEN", "CLOSED", "MERGED"}
                for pr in nodes
            ):
                raise ValueError("merged linkage states are UNKNOWN")
            if not any(pr["state"] == "MERGED" for pr in nodes):
                eligible.append(item)
        except (KeyError, TypeError) as exc:
            raise ValueError("merged linkage population is UNKNOWN") from exc
    return eligible


def record_cycle(
    items: list[dict],
    *,
    path: Path | None = None,
    runner=None,
    dispatch: bool = True,
    proposal_json: dict | None = None,
) -> dict:
    """One complete snapshot, including invalid proposals as named UNKNOWNs."""
    ordered = sorted(items, key=rule_key)
    path = path or corpus_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    runner = runner or roles.run_triage_agent
    result: dict[str, Any] = {}
    errors = []
    if ordered:
        # The role's offload gets a disposable private cwd, never the control plane.
        with tempfile.TemporaryDirectory(prefix="triage-shadow-") as private_cwd:
            try:
                result = runner(
                    backlog_items=ordered,
                    max_items=len(ordered),
                    context="Rank this opener snapshot in shadow only; no worker action.",
                    cwd=private_cwd,
                    dispatch=dispatch,
                    proposal_json=proposal_json,
                )
            except Exception as exc:
                errors = [str(exc)]
    errors.extend(result.get("errors") or [])
    proposal = result.get("proposal")
    valid = bool(proposal is not None and not errors)
    recs = (proposal or {}).get("recommendations") or []
    known = {item["target"] for item in ordered}
    rec_targets = [rec.get("target") for rec in recs]
    if valid and (set(rec_targets) != known or len(rec_targets) != len(known)):
        valid = False
        errors.append("triage proposal did not cover the exact candidate population")
    rank = []
    if valid:
        work_now = [rec for rec in recs if rec.get("action") == "work_now"]
        if any(
            not isinstance(rec.get("priority"), int) or not 1 <= rec["priority"] <= 5
            for rec in work_now
        ):
            valid = False
            errors.append("triage proposal priority must be an integer from 1 to 5")
        else:
            rank = sorted(
                work_now,
                key=lambda rec: (rec["priority"], rec["target"]),
            )
    row = {
        "schema_version": 1,
        "ts": time.time_ns(),
        "shadow": True,
        "candidate_count": len(ordered),
        "candidate_targets": [i["target"] for i in ordered],
        "rule_pick": ordered[0]["target"] if ordered else None,
        "triage_top_three": [rec["target"] for rec in rank[:3]],
        "triage_valid": valid,
        "errors": errors,
        "live_proposal": bool(
            dispatch and valid and result.get("backend_run_id") and result.get("role_run_id")
        ),
        "role_run_id": result.get("role_run_id"),
        "backend_run_id": result.get("backend_run_id"),
        "backend": result.get("backend"),
        "decision_source": result.get("decision_source"),
        "snapshot_sha256": hashlib.sha256(json.dumps(ordered, sort_keys=True).encode()).hexdigest(),
        "implementation_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "recommendations": recs if valid else [],
    }
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(row, sort_keys=True) + "\n")
    if row["triage_valid"] and row["live_proposal"]:
        ranked = [rec["target"] for rec in rank]
        _publish_ranking(row, ranked + [i["target"] for i in ordered if i["target"] not in ranked])
    return row


def summary(path: Path | None = None) -> dict:
    """Score only judged target outcomes; a merge with pending durability is unknown."""
    path = path or corpus_path()
    rows = []
    malformed = 0
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
                if (
                    not isinstance(row, dict)
                    or row.get("schema_version") != 1
                    or not isinstance(row.get("triage_top_three"), list)
                    or any(not isinstance(target, str) for target in row["triage_top_three"])
                    or (row.get("rule_pick") is not None and not isinstance(row["rule_pick"], str))
                    or type(row.get("ts")) is not int
                    or not 0 < row["ts"] <= 2**63 - 1
                ):
                    raise ValueError("unknown or malformed row schema")
                rows.append(row)
            except (ValueError, TypeError):
                malformed += 1
    totals = {
        arm: {"judged": 0, "merged_durable": 0, "no_delivery": 0} for arm in ("triage", "rule")
    }
    live_rows = [row for row in rows if row.get("triage_valid") and row.get("live_proposal")]
    if not live_rows:
        return {
            "cycles": len(rows),
            "malformed_rows": malformed,
            "valid_cycles": len(live_rows),
            "top1_agrees": sum(
                row.get("rule_pick") == next(iter(row["triage_top_three"]), None)
                for row in live_rows
            ),
            **totals,
        }
    consumed = _read_rows(consumed_path())
    linked = linked_consumptions(consumed)

    def grade_run(conn, arm: str, run_id: str | None, since_ts: float) -> None:
        if not run_id:
            totals[arm]["no_delivery"] += 1
            return
        found = conn.execute(
            "SELECT o.merged,o.durability,o.failure_class FROM outcomes o "
            "JOIN runs r ON r.run_id=o.run_id WHERE o.run_id=? AND r.ts > ?",
            (run_id, since_ts),
        ).fetchone()
        if (
            found
            and found[1] not in (None, "pending")
            and found[2] not in feedback.LEARNING_EXCLUDED_FAILURE_CLASSES
        ):
            totals[arm]["judged"] += 1
            totals[arm]["merged_durable"] += int(found[0] == 1 and found[1] == "durable")

    def rule_run_id(conn, target: str, since_ts: float) -> str | None:
        repo, _, issue = target.rpartition("#")
        if not repo or not issue.isdigit():
            return None
        covers = [
            cover
            for cover in outcomes._read_pr_covers().values()
            if cover.get("repo") == repo and int(issue) in cover.get("covers", [])
        ]
        if not covers:
            return None
        for cover in sorted(covers, key=lambda row: (row.get("ts", 0), row["pr"]), reverse=True):
            found = conn.execute(
                "SELECT run_id FROM runs WHERE target=? AND mode != 'role' AND ts > ? "
                "ORDER BY ts DESC,run_id DESC LIMIT 1",
                (f"{repo}#{cover['pr']}", since_ts),
            ).fetchone()
            if found:
                return found[0]
        return None

    with feedback._conn() as conn:
        for row in live_rows:
            # Invalid/fallback output is never a triage observation, nor a paired comparison.
            if not row.get("triage_valid") or not row.get("live_proposal"):
                continue
            triage_pick = next(iter(row.get("triage_top_three") or []), None)
            if triage_pick:
                triage_links = [
                    linked.get(consumption_key(receipt), {})
                    for receipt in consumed
                    if receipt.get("role_run_id") == row.get("role_run_id")
                    and receipt.get("picked") == triage_pick
                ]
                for link in triage_links:
                    if link.get("status") == "followed":
                        grade_run(
                            conn,
                            "triage",
                            link.get("run_id"),
                            row["ts"] / 1_000_000_000,
                        )
            rule_pick = row.get("rule_pick")
            if rule_pick:
                grade_run(
                    conn,
                    "rule",
                    rule_run_id(conn, rule_pick, row["ts"] / 1_000_000_000),
                    row["ts"] / 1_000_000_000,
                )
    return {
        "cycles": len(rows),
        "malformed_rows": malformed,
        "valid_cycles": len(live_rows),
        "top1_agrees": sum(
            row.get("rule_pick") == next(iter(row["triage_top_three"]), None) for row in live_rows
        ),
        **totals,
    }


def consumption_key(row: dict) -> str:
    """New receipts carry a unique ID; old receipts retain a stable content identity."""
    if "consumption_id" in row and (
        not isinstance(row["consumption_id"], str) or not row["consumption_id"]
    ):
        raise ValueError("invalid consumption_id")
    return (
        row.get("consumption_id")
        or "legacy:"
        + hashlib.sha256(
            json.dumps(row, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )


def linked_consumptions(consumed: list[dict]) -> dict[str, dict]:
    keys = [consumption_key(row) for row in consumed]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate consumption identity")
    result = {}
    for link in _read_rows(linked_path()):
        if "consumption_id" in link:
            key = consumption_key(link)
        else:
            matches = [row for row in consumed if row["ts"] == link["consumed_ts"]]
            if len(matches) != 1:
                raise ValueError("ambiguous legacy consumption timestamp")
            key = consumption_key(matches[0])
        if key in result:
            raise ValueError("duplicate linked consumption identity")
        result[key] = link
    return result


FOLLOWED_POOL = 30  # consumptions the opener followed; the pool that triggers the M4 evaluation


def summary_line(path: Path | None = None) -> str:
    try:
        data = summary(path)
        consumed = _read_rows(consumed_path())
        linked = linked_consumptions(consumed)
        followed = sum(
            linked.get(consumption_key(row), {}).get("status") == "followed" for row in consumed
        )
        not_followed = sum(
            linked.get(consumption_key(row), {}).get("status") == "not_followed" for row in consumed
        )
        unresolved = sum(
            linked.get(consumption_key(row), {}).get("status") == "unresolved" for row in consumed
        )
    except (OSError, UnicodeError, ValueError, KeyError, sqlite3.Error) as exc:
        return f"triage shadow: UNKNOWN — outcome evidence unreadable ({type(exc).__name__})"
    if data["malformed_rows"]:
        return (
            f"triage shadow: UNKNOWN — malformed corpus rows {data['malformed_rows']}; "
            f"valid cycles {data['valid_cycles']}"
        )

    def percent(arm):
        count = data[arm]["judged"]
        return (
            f"{100 * data[arm]['merged_durable'] / count:.1f}% ({count} judged) "
            f"[{data[arm]['no_delivery']} no delivery]"
            if count
            else f"n/a (0 judged) [{data[arm]['no_delivery']} no delivery]"
        )

    return (
        f"triage shadow: cycles {data['cycles']}, valid {data['valid_cycles']}, "
        f"triage top-1 merged+durable {percent('triage')}, rule pick {percent('rule')}, "
        f"malformed rows {data['malformed_rows']}; "
        f"consumed {len(consumed)}, followed {followed}, not followed {not_followed}, "
        f"open {len(consumed) - followed - not_followed - unresolved}; unresolved {unresolved}; "
        f"top-1 agrees with the rule pick {data['top1_agrees']} of {data['valid_cycles']} valid cycles"
        f"; pool followed {followed} of {FOLLOWED_POOL}"
    )


def consume(candidates: list[str], *, env=None, now=None) -> dict:
    """Offer an order, retaining the entire caller-filtered population; never claim work."""
    env = os.environ if env is None else env
    now = time.time() if now is None else now
    result = {
        "consumed": False,
        "reason": "flag_off",
        "ordered_targets": candidates,
        "role_run_id": None,
    }
    if env.get("ORCH_TRIAGE_CONSUME", "0") != "1":
        return result
    try:
        if (
            not isinstance(candidates, list)
            or any(not isinstance(c, str) or not c for c in candidates)
            or len(set(candidates)) != len(candidates)
        ):
            raise ValueError("invalid candidate population")
        data = summary()
        if data["malformed_rows"]:
            raise ValueError("malformed corpus")
        valid = data["valid_cycles"]
        if valid < MIN_CONSUME_CYCLES:
            result["reason"] = f"cycles_below_minimum:{valid}/{MIN_CONSUME_CYCLES}"
            return result
        if not ranking_path().exists():
            result["reason"] = "no_ranking"
            return result
        ranking = json.loads(ranking_path().read_text(encoding="utf-8"))
        if (
            not isinstance(ranking, dict)
            or type(ranking.get("ts")) is not int
            or not isinstance(ranking.get("role_run_id"), str)
            or not ranking["role_run_id"]
            or not isinstance(ranking.get("snapshot_sha256"), str)
            or not isinstance(ranking.get("ordered_targets"), list)
            or any(not isinstance(t, str) for t in ranking["ordered_targets"])
            or len(set(ranking["ordered_targets"])) != len(ranking["ordered_targets"])
        ):
            raise ValueError("invalid ranking")
        age = now - ranking["ts"] / 1_000_000_000
        if age < 0 or age > RANKING_MAX_AGE_S:
            result["reason"] = f"ranking_stale:{int(age)}/{RANKING_MAX_AGE_S}"
            return result
        if not candidates:
            result["reason"] = "no_candidates"
            return result
        version = (capabilities.load(create=False).get("role-triage") or {}).get(
            "capability_version_id"
        )
        if not version:
            raise ValueError("capability version unavailable")
        ranked = [t for t in ranking["ordered_targets"] if t in candidates]
        if not ranked:
            result["reason"] = "ranking_no_candidates"
            return result
        ordered = ranked + [t for t in candidates if t not in ranked]
        _append(
            consumed_path(),
            {
                "ts": int(now * 1_000_000_000),
                "consumption_id": uuid.uuid4().hex,
                "role_run_id": ranking["role_run_id"],
                "ranking_ts": ranking["ts"],
                "snapshot_sha256": ranking["snapshot_sha256"],
                "capability_version_id": version,
                "picked": ordered[0],
                "ordered_targets": ordered,
                "candidate_sha256": hashlib.sha256(
                    json.dumps(candidates, sort_keys=True).encode()
                ).hexdigest(),
            },
        )
        return {
            "consumed": True,
            "reason": "consumed",
            "ordered_targets": ordered,
            "role_run_id": ranking["role_run_id"],
        }
    except (OSError, ValueError, TypeError, sqlite3.Error) as exc:
        result["reason"] = f"evidence_unknown:{exc}"
        return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--selftest", action="store_true")
    parser.add_argument("--summary", action="store_true")
    parser.add_argument("--snapshot", type=Path)
    parser.add_argument("--proposal-json", type=Path)
    parser.add_argument("--backfill", action="store_true")
    parser.add_argument("--consume", action="store_true")
    parser.add_argument("--candidates", type=Path)
    args = parser.parse_args(argv)
    if args.consume:
        if not args.candidates:
            parser.error("--consume requires --candidates")
        try:
            candidates = json.loads(args.candidates.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            parser.error(str(exc))
        print(json.dumps(consume(candidates), sort_keys=True))
        return 0
    if args.selftest:
        with tempfile.TemporaryDirectory() as tmp:
            row = record_cycle([], path=Path(tmp) / "shadow.jsonl", dispatch=False)
            assert row["candidate_count"] == 0 and row["rule_pick"] is None
            assert consume(["example/repo#1"], env={})["reason"] == "flag_off"
        print("triage_shadow.py selftest: OK (empty cycle recorded, no agent or worker mutation)")
        return 0
    if args.summary:
        print(summary_line())
        return 0
    if os.environ.get("ORCH_TRIAGE_SHADOW", "1") == "0":
        print("triage shadow: disabled by ORCH_TRIAGE_SHADOW=0")
        return 0
    try:
        items = json.loads(args.snapshot.read_text()) if args.snapshot else discover_candidates()
        proposal = json.loads(args.proposal_json.read_text()) if args.proposal_json else None
        row = record_cycle(items, dispatch=proposal is None, proposal_json=proposal)
        if args.backfill:
            row["backfill"] = outcomes.backfill_triage_disagreements()
            row["consumption_links"] = outcomes.link_triage_consumption()
        print(json.dumps(row, sort_keys=True))
        return int(bool(row["errors"]))
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        print(json.dumps({"shadow": True, "error": str(exc), "population": "UNKNOWN"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
