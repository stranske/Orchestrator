#!/usr/bin/env python3
"""Turn keepalive Brain outcomes into weekly issue-candidate evidence.

This is read-only over the feedback DB in normal operation. It surfaces human
triage seeds from dynamic keepalive outcomes, plus routing/quality signals for
the weekly repo-review automation.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote

import feedback
import keepalive_outcomes
import repo_knowledge

DURABILITY_BUCKETS = ("durable", "pending", "abandoned", "reverted")
PROCESS_WORK_TYPES = {"renovate", "sync", "tooling", "docs"}
KNOWLEDGE_TASK_TYPES = (None, "implement", "mechanical", "testgen", "fix", "docs", "test", "review")
FAILURE_SIGNAL_RE = re.compile(
    r"\b(recurr(?:ing|ed)|failure|fail(?:ed|ing)?|broken|broke|regression|"
    r"revert(?:ed)?|abandon(?:ed)?|gotcha|avoid|do not|don't|never|missing|"
    r"wrong|fragile|blocked|churn)\b",
    re.IGNORECASE,
)


def _readonly_conn(db_path: Path | None = None) -> sqlite3.Connection:
    path = Path(db_path or feedback.DB_PATH).expanduser()
    if not path.exists():
        raise FileNotFoundError(f"feedback DB not found: {path}")
    conn = sqlite3.connect(f"file:{quote(str(path), safe='/')}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _repo_rows(repo: str, lookback_days: int, *, db_path: Path | None = None) -> list[dict]:
    since = int(time.time()) - lookback_days * 86400
    target_prefix = f"{repo}#%"
    run_prefix = f"keepalive:{repo}#%"
    with _readonly_conn(db_path) as conn:
        run_cols = {row[1] for row in conn.execute("PRAGMA table_info(runs)").fetchall()}
        work_type_expr = "r.work_type" if "work_type" in run_cols else "NULL"
        rows = conn.execute(
            "SELECT r.run_id, r.ts, r.target, r.agent, r.pr_number, "
            f"{work_type_expr} AS work_type, o.durability, COALESCE(o.notes,'') AS notes "
            "FROM runs r JOIN outcomes o ON r.run_id=o.run_id "
            "WHERE r.source='keepalive' AND r.ts>=? "
            "AND (r.target LIKE ? OR r.run_id LIKE ?) "
            "ORDER BY r.ts DESC, r.run_id DESC",
            (since, target_prefix, run_prefix),
        ).fetchall()
    return [dict(row) for row in rows]


def _pr_number(row: dict) -> int | None:
    if row.get("pr_number") is not None:
        return int(row["pr_number"])
    for value in (row.get("target"), row.get("run_id")):
        match = re.search(r"#(\d+)", str(value or ""))
        if match:
            return int(match.group(1))
    return None


def _row_work_type(row: dict) -> str:
    return row.get("work_type") or "issue"


def _run_json(args: list[str], *, timeout: int = 30) -> object | None:
    try:
        res = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if res.returncode != 0:
        return None
    try:
        return json.loads(res.stdout)
    except json.JSONDecodeError:
        return None


def _fetch_pr_title(repo: str, pr_number: int) -> str | None:
    obj = _run_json(["gh", "pr", "view", str(pr_number), "--repo", repo, "--json", "title"])
    return obj.get("title") if isinstance(obj, dict) and obj.get("title") else None


def _issue_search(repo: str, query: str) -> list[dict]:
    arr = _run_json(
        [
            "gh",
            "issue",
            "list",
            "--repo",
            repo,
            "--state",
            "open",
            "--search",
            query,
            "--limit",
            "20",
            "--json",
            "number,title",
        ]
    )
    return arr if isinstance(arr, list) else []


def _linked_issues(repo: str, pr: int) -> list[dict]:
    obj = _run_json(
        [
            "gh",
            "pr",
            "view",
            str(pr),
            "--repo",
            repo,
            "--json",
            "closingIssuesReferences",
        ]
    )
    refs = obj.get("closingIssuesReferences") if isinstance(obj, dict) else []
    if not isinstance(refs, list):
        return []
    issues = []
    seen: set[int] = set()
    for ref in refs:
        if not isinstance(ref, dict) or ref.get("number") is None:
            continue
        try:
            issue_number = int(ref["number"])
        except (TypeError, ValueError):
            continue
        if issue_number in seen:
            continue
        seen.add(issue_number)
        issue = _run_json(
            [
                "gh",
                "issue",
                "view",
                str(issue_number),
                "--repo",
                repo,
                "--json",
                "number,state",
            ]
        )
        if isinstance(issue, dict) and issue.get("number") is not None:
            try:
                fetched_number = int(issue["number"])
            except (TypeError, ValueError):
                continue
            issues.append({"number": fetched_number, "state": str(issue.get("state") or "")})
    return issues


def _contains_pr_ref(text: str, pr_number: int | None) -> bool:
    if pr_number is None:
        return False
    return re.search(rf"(?<!\d)#?{pr_number}(?!\d)", text or "") is not None


def _tokens(text: str) -> set[str]:
    stop = {"the", "and", "for", "was", "then", "with", "that", "this", "from", "open"}
    return {
        token
        for token in re.findall(r"[a-z0-9][a-z0-9_-]*", (text or "").lower())
        if len(token) >= 4 and token not in stop
    }


def _dedupe_candidate(
    candidate: dict,
    repo: str,
    issue_search_fn=_issue_search,
    linked_issues_fn=_linked_issues,
) -> None:
    evidence = candidate.get("evidence") or {}
    pr_number = evidence.get("pr")
    if pr_number is not None:
        for issue in linked_issues_fn(repo, int(pr_number)) or []:
            if str(issue.get("state") or "").upper() == "OPEN":
                if issue.get("number") is None:
                    continue
                try:
                    issue_number = int(issue["number"])
                except (TypeError, ValueError):
                    continue
                candidate["possible_duplicate"] = True
                candidate["dup_issue"] = issue_number
                return
    title = str(evidence.get("title") or "")
    queries = []
    if pr_number is not None:
        queries.append(f"PR #{pr_number}")
    title_terms = " ".join(sorted(_tokens(title))[:6])
    if title_terms:
        queries.append(title_terms)
    if not queries:
        queries.append(" ".join(sorted(_tokens(candidate["seed"]))[:6]))
    issues = []
    seen_issue_numbers = set()
    for query in queries:
        for issue in issue_search_fn(repo, query) or []:
            issue_number = issue.get("number")
            if issue_number in seen_issue_numbers:
                continue
            seen_issue_numbers.add(issue_number)
            issues.append(issue)
    seed_tokens = _tokens(title) or _tokens(candidate["seed"])
    # Matching below is on `issue_title` and on `candidate["seed"]` -- deliberately NOT on a
    # combined f"#{issue_number} {issue_title}". Prepending the issue's OWN number widens the
    # match by exactly one condition, issue_number == pr_number, and that condition is dead twice
    # over: GitHub draws issue and PR numbers from a single per-repo sequence so the two can never
    # be equal, and the seed disjunct already covers it anyway, since every seed built in
    # evidence_for_repo opens with "PR #{pr_number}". If it ever did fire it would claim
    # duplication from a bare numeric coincidence. Note `_contains_pr_ref` already matches a bare
    # "#N" inside a title (the "#" in `#?` is optional), so the combined form buys nothing there
    # either. To genuinely match more, widen the TEXT searched -- title -> title+body, as
    # durability_sweep does. The selftest pins both halves of this.
    for issue in issues or []:
        issue_number = issue.get("number")
        issue_title = str(issue.get("title") or "")
        if _contains_pr_ref(issue_title, pr_number) or _contains_pr_ref(
            candidate["seed"], issue_number
        ):
            candidate["possible_duplicate"] = True
            candidate["dup_issue"] = issue_number
            return
        if title and issue_title and seed_tokens:
            overlap = len(seed_tokens & _tokens(issue_title)) / max(1, len(seed_tokens))
            if overlap >= 0.6:
                candidate["possible_duplicate"] = True
                candidate["dup_issue"] = issue_number
                return
    candidate["possible_duplicate"] = False
    candidate["dup_issue"] = None


def _gotcha_lines(context: str) -> list[str]:
    lines = context.splitlines()
    out: list[str] = []
    in_gotchas = False
    for line in lines:
        stripped = line.strip()
        if stripped == "- Known gotchas:":
            in_gotchas = True
            continue
        if in_gotchas and stripped.startswith("- ") and not line.startswith("  - "):
            break
        if in_gotchas and line.startswith("  - "):
            text = stripped[2:].strip()
            if FAILURE_SIGNAL_RE.search(text):
                out.append(text)
    if out:
        return out
    return [line.strip(" -") for line in lines if FAILURE_SIGNAL_RE.search(line)]


def _knowledge_candidates(repo: str, *, knowledge_path: Path | None = None) -> list[dict]:
    patterns = []
    seen: set[str] = set()
    base_kwargs: dict[str, Any] = {"path": knowledge_path} if knowledge_path is not None else {}
    for task_type in KNOWLEDGE_TASK_TYPES:
        kwargs = dict(base_kwargs)
        if task_type is not None:
            kwargs["task_type"] = task_type
        context = repo_knowledge.context_for(repo, **kwargs)
        for line in _gotcha_lines(context):
            key = line.lower()
            if key and key not in seen:
                patterns.append(line)
                seen.add(key)
    return [
        {
            "type": "recurring_failure",
            "severity": "MED",
            "seed": f"Recurring failure pattern: {pattern} -- consider a structural fix or CI guard.",
            "evidence": {"repo": repo, "pattern": pattern, "source": "repo_knowledge.context_for"},
            "possible_duplicate": False,
            "dup_issue": None,
        }
        for pattern in patterns
    ]


def _signals(rows: list[dict]) -> dict:
    by_agent: dict[str, dict[str, int]] = {}
    durable = 0
    for row in rows:
        agent = row.get("agent") or "unknown"
        durability = row.get("durability") or "pending"
        counts = by_agent.setdefault(agent, {bucket: 0 for bucket in DURABILITY_BUCKETS})
        if durability in counts:
            counts[durability] += 1
        if durability == "durable":
            durable += 1
    return {
        "durable_rate": (durable / len(rows)) if rows else None,
        "by_agent": by_agent,
    }


def _process_signal_seed(repo: str, work_type: str, count: int, lookback_days: int) -> str:
    if work_type == "renovate":
        tail = "renovate config likely too aggressive; consider pinning/grouping/scheduling."
    elif work_type == "sync":
        tail = (
            "sync automation may be stale or noisy; inspect template, mirror, and drift handling."
        )
    elif work_type == "tooling":
        tail = "tooling or CI workflow changes are unstable; tighten validation before rollout."
    else:
        tail = "docs workflow is churning; check ownership, templates, and review expectations."
    noun = "PR" if count == 1 else "PRs"
    return (
        f"{count} {work_type} {noun} reverted or abandoned in {lookback_days}d on {repo} -- {tail}"
    )


def _process_signals(
    repo: str,
    rows: list[dict],
    lookback_days: int,
    title_cache: dict[int, str | None],
    title_fetch_fn,
) -> list[dict]:
    grouped: dict[str, dict[str, object]] = {}
    seen: set[tuple[str, object]] = set()
    for row in rows:
        work_type = _row_work_type(row)
        durability = row.get("durability")
        if work_type not in PROCESS_WORK_TYPES or durability not in {"reverted", "abandoned"}:
            continue
        if keepalive_outcomes.process_suppression_reason(row.get("notes")):
            continue
        pr_number = _pr_number(row)
        key = (work_type, pr_number if pr_number is not None else row.get("run_id"))
        if key in seen:
            continue
        seen.add(key)
        title = None
        if pr_number is not None:
            if pr_number not in title_cache:
                title_cache[pr_number] = title_fetch_fn(repo, pr_number)
            title = title_cache[pr_number]
        item = {
            "run_id": row.get("run_id"),
            "pr": pr_number,
            "agent": row.get("agent") or "unknown",
            "durability": durability,
        }
        if title:
            item["title"] = title
        bucket: dict[str, Any] = grouped.setdefault(
            work_type, {"work_type": work_type, "prs": [], "has_revert": False}
        )
        bucket["prs"].append(item)
        if durability == "reverted":
            bucket["has_revert"] = True

    signals = []
    for work_type in sorted(grouped):
        bucket = grouped[work_type]
        prs = cast(list, bucket["prs"] or [])
        count = len(prs)
        signals.append(
            {
                "work_type": work_type,
                "count": count,
                "severity": "HIGH" if bucket["has_revert"] else "MED",
                "prs": prs,
                "seed": _process_signal_seed(repo, work_type, count, lookback_days),
            }
        )
    return signals


def evidence_for_repo(
    repo: str,
    *,
    lookback_days: int = 30,
    db_path: Path | None = None,
    title_fetch_fn=_fetch_pr_title,
    issue_search_fn=_issue_search,
    linked_issues_fn=_linked_issues,
    knowledge_path: Path | None = None,
) -> dict:
    rows = _repo_rows(repo, lookback_days, db_path=db_path)
    candidates: list[dict] = []
    title_cache: dict[int, str | None] = {}
    process_signals = _process_signals(repo, rows, lookback_days, title_cache, title_fetch_fn)

    for row in rows:
        if _row_work_type(row) != "issue":
            continue
        durability = row.get("durability")
        if durability not in {"reverted", "abandoned"}:
            continue
        pr_number = _pr_number(row)
        title = None
        if pr_number is not None:
            if pr_number not in title_cache:
                title_cache[pr_number] = title_fetch_fn(repo, pr_number)
            title = title_cache[pr_number]
        agent = row.get("agent") or "unknown"
        evidence = {
            "run_id": row.get("run_id"),
            "pr": pr_number,
            "agent": agent,
            "durability": durability,
        }
        if title:
            evidence["title"] = title
        if durability == "reverted":
            seed = (
                f"PR #{pr_number} by {agent} merged then was REVERTED on {repo} -- "
                "the change actively broke something; open a durable-fix + regression-test issue."
            )
            severity = "HIGH"
            ctype = "reversal"
        else:
            shown_title = title or "title unavailable"
            seed = (
                f"PR #{pr_number} by {agent} ('{shown_title}') was abandoned (closed unmerged); "
                "the underlying need may be unresolved -- review whether it still requires solving."
            )
            severity = "MED"
            ctype = "abandoned"
        candidate = {
            "type": ctype,
            "severity": severity,
            "seed": seed,
            "evidence": evidence,
            "possible_duplicate": False,
            "dup_issue": None,
        }
        _dedupe_candidate(
            candidate,
            repo,
            issue_search_fn=issue_search_fn,
            linked_issues_fn=linked_issues_fn,
        )
        candidates.append(candidate)

    for candidate in _knowledge_candidates(repo, knowledge_path=knowledge_path):
        _dedupe_candidate(
            candidate,
            repo,
            issue_search_fn=issue_search_fn,
            linked_issues_fn=linked_issues_fn,
        )
        candidates.append(candidate)

    return {
        "repo": repo,
        "lookback_days": lookback_days,
        "candidates": candidates,
        "process_signals": process_signals,
        "signals": _signals(rows),
    }


def _human_summary(result: dict) -> str:
    lines = [
        f"{result['repo']} ({result['lookback_days']}d): "
        f"{len(result['candidates'])} issue candidate(s), "
        f"{len(result.get('process_signals', []))} process signal(s), "
        f"durable_rate={result['signals']['durable_rate']}"
    ]
    lines.append("Issue candidates:")
    for candidate in result["candidates"]:
        dup = (
            f" possible duplicate #{candidate['dup_issue']}"
            if candidate.get("possible_duplicate")
            else ""
        )
        lines.append(f"- {candidate['severity']} {candidate['type']}: {candidate['seed']}{dup}")
    if not result["candidates"]:
        lines.append("- No dynamic issue candidates.")
    lines.append("Process signals:")
    for signal in result.get("process_signals", []):
        lines.append(f"- {signal['severity']} {signal['work_type']}: {signal['seed']}")
    if not result.get("process_signals"):
        lines.append("- No process signals.")
    return "\n".join(lines)


def _selftest() -> None:
    tmp = tempfile.mkdtemp(prefix="keepalive-evidence-selftest-")
    old_db = feedback.DB_PATH
    old_knowledge = repo_knowledge.REG
    feedback.DB_PATH = Path(tmp) / "t.db"
    knowledge_path = Path(tmp) / "repo_knowledge.json"
    now = int(time.time())
    try:
        rows = [
            ("keepalive:o/r#1:codex", "o/r#1", "codex", 1, "reverted", "issue"),
            ("keepalive:o/r#2:claude", "o/r#2", "claude", 2, "reverted", "renovate"),
            ("keepalive:o/r#3:cursor", "o/r#3", "cursor", 3, "abandoned", "sync"),
            ("keepalive:o/r#4:codex", "o/r#4", "codex", 4, "abandoned", None),
            ("keepalive:o/r#5:codex", "o/r#5", "codex", 5, "durable", "issue"),
            ("keepalive:o/r#6:claude", "o/r#6", "claude", 6, "pending", "issue", None),
            (
                "keepalive:o/r#7:cursor",
                "o/r#7",
                "cursor",
                7,
                "abandoned",
                "sync",
                "remote keepalive PR closed unmerged; process_ignore=duplicate_or_superseded",
            ),
            ("keepalive:clean/repo#1:codex", "clean/repo#1", "codex", 1, "durable", "issue"),
            ("keepalive:clean/repo#2:claude", "clean/repo#2", "claude", 2, "durable", "sync"),
        ]
        rows = [row if len(row) == 7 else (*row, None) for row in rows]
        for run_id, target, agent, pr_number, durability, work_type, notes in rows:
            feedback.record_run(
                run_id,
                target,
                "implement",
                agent,
                mode="remote",
                pr_number=pr_number,
                ts=now - 3600,
                source="keepalive",
                work_type=work_type,
            )
            feedback.record_outcome(
                run_id,
                adjudicated_verdict="PASS" if durability == "durable" else None,
                merged=durability != "abandoned",
                durability=durability,
                notes=notes,
            )

        knowledge_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "repos": {
                        "o/r": {
                            "summary": "Fixture repo.",
                            "gotchas": [
                                "Recurring CI failure: migration drift caused failed keepalive runs.",
                            ],
                        }
                    },
                }
            )
        )

        fetched_titles: list[int] = []

        def title_fetch(repo: str, pr_number: int) -> str:
            fetched_titles.append(pr_number)
            return {
                1: "Reverted issue change",
                2: "chore(deps): update package",
                3: "Template sync",
                4: "Superseded approach",
            }[pr_number]

        issue_queries: list[str] = []

        def issue_search(repo: str, query: str) -> list[dict]:
            issue_queries.append(query)
            if "PR #4" in query:
                return [{"number": 77, "title": "Follow up for abandoned PR #4"}]
            return []

        def linked_issues(repo: str, pr: int) -> list[dict]:
            return {
                1: [{"number": 88, "state": "OPEN"}],
                4: [{"number": 99, "state": "CLOSED"}],
            }.get(pr, [])

        result = evidence_for_repo(
            "o/r",
            lookback_days=30,
            title_fetch_fn=title_fetch,
            issue_search_fn=issue_search,
            linked_issues_fn=linked_issues,
            knowledge_path=knowledge_path,
        )
        types = [candidate["type"] for candidate in result["candidates"]]
        assert types.count("reversal") == 1, result
        assert types.count("abandoned") == 1, result
        assert types.count("recurring_failure") == 1, result
        assert all(
            candidate["evidence"].get("pr") not in {2, 3, 5} for candidate in result["candidates"]
        ), result
        process_by_type = {signal["work_type"]: signal for signal in result["process_signals"]}
        assert sorted(process_by_type) == ["renovate", "sync"], result["process_signals"]
        assert process_by_type["renovate"]["count"] == 1, process_by_type
        assert process_by_type["renovate"]["severity"] == "HIGH", process_by_type
        assert process_by_type["renovate"]["prs"][0]["pr"] == 2, process_by_type
        assert process_by_type["sync"]["count"] == 1, process_by_type
        assert process_by_type["sync"]["severity"] == "MED", process_by_type
        assert process_by_type["sync"]["prs"][0]["pr"] == 3, process_by_type
        assert sorted(fetched_titles) == [1, 2, 3, 4], fetched_titles
        dupes = [
            candidate for candidate in result["candidates"] if candidate.get("possible_duplicate")
        ]
        assert {candidate["dup_issue"] for candidate in dupes} == {77, 88}, dupes
        linked_dupe = [candidate for candidate in dupes if candidate["dup_issue"] == 88]
        assert len(linked_dupe) == 1 and linked_dupe[0]["type"] == "reversal", dupes
        assert all("PR #1" not in query for query in issue_queries), issue_queries
        term_dupe = [candidate for candidate in dupes if candidate["dup_issue"] == 77]
        assert len(term_dupe) == 1 and term_dupe[0]["evidence"].get("pr") == 4, dupes
        closed_linked = [
            candidate for candidate in result["candidates"] if candidate["evidence"].get("pr") == 4
        ]
        assert len(closed_linked) == 1 and closed_linked[0]["possible_duplicate"], closed_linked
        assert abs(result["signals"]["durable_rate"] - (1 / 7)) < 1e-9, result["signals"]
        assert result["signals"]["by_agent"]["codex"] == {
            "durable": 1,
            "pending": 0,
            "abandoned": 1,
            "reverted": 1,
        }, result["signals"]

        clean = evidence_for_repo(
            "clean/repo",
            lookback_days=30,
            title_fetch_fn=title_fetch,
            issue_search_fn=lambda _repo, _query: [],
            linked_issues_fn=lambda _repo, _pr: [],
            knowledge_path=knowledge_path,
        )
        assert clean["candidates"] == [], clean
        assert clean["process_signals"] == [], clean
        assert clean["signals"]["durable_rate"] == 1.0, clean["signals"]

        # _dedupe_candidate deliberately does NOT search a combined "#{issue_number} {title}".
        # Two assertions pin that decision so a later lint or tidy pass cannot quietly wire it in.
        # 1. The isolated case the combined form -- and only the combined form -- would match: an
        #    issue whose own number equals the candidate PR's, with an unrelated title and a seed
        #    that does not name the number. It must NOT be called a duplicate.
        collision = {
            "type": "reversal",
            "severity": "HIGH",
            "seed": "A merged change was REVERTED; open a durable-fix + regression-test issue.",
            "evidence": {"run_id": "x", "pr": 4242, "agent": "codex", "durability": "reverted"},
            "possible_duplicate": False,
            "dup_issue": None,
        }
        _dedupe_candidate(
            collision,
            "o/r",
            issue_search_fn=lambda _repo, _query: [
                {"number": 4242, "title": "unrelated maintenance chore"}
            ],
            linked_issues_fn=lambda _repo, _pr: [],
        )
        assert collision["possible_duplicate"] is False, collision
        assert collision["dup_issue"] is None, collision
        # 2. ...and that stays a no-op in production only because every seed evidence_for_repo
        #    builds opens with "PR #<pr>", so the seed disjunct already covers issue_number ==
        #    pr_number. If a seed ever stops naming its PR, the decision needs re-examining.
        seeded = [
            candidate
            for candidate in result["candidates"]
            if (candidate.get("evidence") or {}).get("pr") is not None
        ]
        assert len(seeded) == 2, seeded
        for candidate in seeded:
            assert _contains_pr_ref(candidate["seed"], candidate["evidence"]["pr"]), candidate

        print("keepalive_evidence.py selftest: OK")
    finally:
        feedback.DB_PATH = old_db
        repo_knowledge.REG = old_knowledge
        shutil.rmtree(tmp, ignore_errors=True)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Emit keepalive-derived issue candidate evidence.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--repo", help="owner/repo to inspect")
    group.add_argument(
        "--all-active", action="store_true", help="inspect every active repo in the registry"
    )
    parser.add_argument("--lookback-days", type=int, default=30)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args(argv)

    if args.selftest:
        _selftest()
        return 0
    if not args.repo and not args.all_active:
        parser.error("one of --repo or --all-active is required")

    if args.all_active:
        repos = keepalive_outcomes._active_repos()
        results = [evidence_for_repo(repo, lookback_days=args.lookback_days) for repo in repos]
        payload = {"lookback_days": args.lookback_days, "repos": results}
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            print("\n\n".join(_human_summary(result) for result in results))
        return 0

    result = evidence_for_repo(args.repo, lookback_days=args.lookback_days)
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(_human_summary(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
