#!/usr/bin/env python3
"""Durability-learned issue-quality scorer for keepalive issue PRs.

Offline analysis only: reads completed keepalive issue-work outcomes from the
feedback Brain, fetches linked issue bodies from GitHub, and reports which issue
content features correlate with durable vs reverted/abandoned PRs.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import re
import shutil
import sqlite3
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from urllib.parse import quote

import feedback
import keepalive_outcomes

DURABLE = "durable"
FAIL_DURABILITIES = {"reverted", "abandoned"}
DEFAULT_MIN_CELL_N = 10
GH_TIMEOUT_SECONDS = 8

PATH_RE = re.compile(
    r"(?<![\w./-])(?:\.github/)?[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+"
    r"(?:\.[A-Za-z0-9][A-Za-z0-9_.-]*)?(?![\w./-])"
)
ROOT_FILE_RE = re.compile(
    r"(?<![\w./-])[A-Za-z0-9_-]+\.(?:py|js|ts|tsx|jsx|yml|yaml|json|md|toml|sh|sql|css|html)(?![\w./-])"
)
MODULE_RE = re.compile(r"`([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+)`")
TEST_COMMAND_RE = re.compile(
    r"\b(?:pytest|python\s+-m\s+pytest|unittest|npm\s+test|pnpm\s+test|yarn\s+test|"
    r"npx\s+(?:jest|vitest|playwright)|jest|vitest|go\s+test|cargo\s+test|swift\s+test)\b",
    re.IGNORECASE,
)
TEST_PATH_RE = re.compile(
    r"(?<![\w./-])(?:tests?/|spec/|__tests__/)[A-Za-z0-9_./-]+"
    r"|(?<![\w./-])[A-Za-z0-9_./-]*(?:test|spec)[A-Za-z0-9_./-]*\.(?:py|js|ts|tsx|jsx)(?![\w./-])",
    re.IGNORECASE,
)
AMBIGUITY_RE = re.compile(
    r"\b(?:maybe|probably|investigate|tbd|unclear|possibly|might|should\s+probably)\b|\?",
    re.IGNORECASE,
)
LINKED_PR_RE = re.compile(
    r"(?:github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/pull/\d+|\bPR\s*#\d+|\bpull\s+request\s+#\d+)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class RunRow:
    run_id: str
    ts: int | None
    target: str
    pr_number: int | None
    durability: str


@dataclass(frozen=True)
class IssueRecord:
    run_id: str
    target: str
    repo: str
    pr_number: int
    issue_number: int
    durability: str
    body: str
    title: str | None = None
    url: str | None = None


FEATURE_LABELS = {
    "has_repro_steps": "repro steps",
    "has_acceptance_criteria": "acceptance criteria",
    "has_test_instructions": "named test/test instructions",
    "has_4_file_or_module_hints": ">=4 file/module hints",
    "has_ambiguity_markers": "ambiguity markers",
    "has_non_goals": "non-goals",
    "has_linked_prs": "linked PR refs",
    "body_length_short": "body length: short",
    "body_length_medium": "body length: medium",
    "body_length_long": "body length: long",
}


def _readonly_conn(db_path: Path | None = None) -> sqlite3.Connection:
    path = Path(db_path or feedback.DB_PATH).expanduser()
    if not path.exists():
        raise FileNotFoundError(f"feedback DB not found: {path}")
    conn = sqlite3.connect(f"file:{quote(str(path), safe='/')}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _target_parts(row: RunRow | dict) -> tuple[str, int] | None:
    target = row.target if isinstance(row, RunRow) else str(row.get("target") or "")
    pr_number = row.pr_number if isinstance(row, RunRow) else row.get("pr_number")
    if target and "#" in target:
        repo, pr_text = target.rsplit("#", 1)
        try:
            return repo, int(pr_text)
        except ValueError:
            pass
    run_id = row.run_id if isinstance(row, RunRow) else str(row.get("run_id") or "")
    match = re.match(r"^keepalive:(.+)#(\d+):[^:]+$", run_id)
    if match:
        return match.group(1), int(match.group(2))
    if target and pr_number is not None:
        repo = target.split("#", 1)[0]
        try:
            return repo, int(pr_number)
        except (TypeError, ValueError):
            return None
    return None


def select_completed_keepalive_issue_runs(
    lookback_days: int = 90,
    *,
    db_path: Path | None = None,
    now: int | None = None,
) -> list[RunRow]:
    """Read-only selection of completed keepalive issue-work runs."""
    since = int(now or time.time()) - lookback_days * 86400
    with _readonly_conn(db_path) as conn:
        run_cols = {row[1] for row in conn.execute("PRAGMA table_info(runs)").fetchall()}
        outcome_cols = {row[1] for row in conn.execute("PRAGMA table_info(outcomes)").fetchall()}
        required_run_cols = {"run_id", "ts", "target", "source", "work_type", "pr_number"}
        required_outcome_cols = {"run_id", "durability"}
        missing = (required_run_cols - run_cols) | (required_outcome_cols - outcome_cols)
        if missing:
            raise RuntimeError(f"feedback DB missing required columns: {sorted(missing)}")
        rows = conn.execute(
            "SELECT r.run_id, r.ts, r.target, r.pr_number, o.durability "
            "FROM runs r JOIN outcomes o ON r.run_id=o.run_id "
            "WHERE r.source='keepalive' AND r.work_type='issue' AND r.ts>=? "
            "AND o.durability IN ('durable','reverted','abandoned') "
            "ORDER BY r.ts DESC, r.run_id DESC",
            (since,),
        ).fetchall()
    return [
        RunRow(
            run_id=str(row["run_id"]),
            ts=row["ts"],
            target=str(row["target"] or ""),
            pr_number=row["pr_number"],
            durability=str(row["durability"]),
        )
        for row in rows
    ]


def _run_json(args: list[str], *, timeout: int = 30) -> object | None:
    return keepalive_outcomes._run_json(args, timeout=timeout)


def fetch_linked_issue_numbers(repo: str, pr_number: int) -> list[int]:
    obj = _run_json(
        [
            "gh",
            "pr",
            "view",
            str(pr_number),
            "-R",
            repo,
            "--json",
            "closingIssuesReferences",
        ],
        timeout=GH_TIMEOUT_SECONDS,
    )
    refs = obj.get("closingIssuesReferences") if isinstance(obj, dict) else []
    if not isinstance(refs, list):
        return []
    out: list[int] = []
    seen: set[int] = set()
    for ref in refs:
        if not isinstance(ref, dict) or ref.get("number") is None:
            continue
        try:
            number = int(ref["number"])
        except (TypeError, ValueError):
            continue
        if number not in seen:
            seen.add(number)
            out.append(number)
    return out


def fetch_issue_body(repo: str, issue_number: int) -> dict | None:
    obj = _run_json(
        [
            "gh",
            "issue",
            "view",
            str(issue_number),
            "-R",
            repo,
            "--json",
            "number,title,body,url",
        ],
        timeout=GH_TIMEOUT_SECONDS,
    )
    return obj if isinstance(obj, dict) else None


def _coerce_issue_payload(payload: object) -> tuple[str, str | None, str | None] | None:
    if payload is None:
        return None
    if isinstance(payload, str):
        return payload, None, None
    if isinstance(payload, dict):
        body = payload.get("body")
        if body is None:
            return None
        return str(body), payload.get("title"), payload.get("url")
    return None


def resolve_issue_records(
    rows: list[RunRow],
    *,
    linked_issue_fn=fetch_linked_issue_numbers,
    body_fetch_fn=fetch_issue_body,
    max_workers: int = 8,
) -> tuple[list[IssueRecord], list[dict]]:
    records: list[IssueRecord] = []
    skipped: list[dict] = []
    parsed_rows: list[tuple[RunRow, str, int]] = []

    for row in rows:
        parts = _target_parts(row)
        if not parts:
            skipped.append(
                {"run_id": row.run_id, "target": row.target, "reason": "could not parse repo/pr"}
            )
            continue
        repo, pr_number = parts
        parsed_rows.append((row, repo, pr_number))

    def fetch_linked(key: tuple[str, int]) -> tuple[tuple[str, int], list[int]]:
        repo, pr_number = key
        try:
            return key, linked_issue_fn(repo, pr_number) or []
        except Exception:
            return key, []

    def fetch_body(key: tuple[str, int]) -> tuple[tuple[str, int], object | None]:
        repo, issue_number = key
        try:
            return key, body_fetch_fn(repo, issue_number)
        except Exception:
            return key, None

    def parallel_fetch(keys: set[tuple[str, int]], fn) -> dict[tuple[str, int], object]:
        if not keys:
            return {}
        workers = max(1, min(max_workers, len(keys)))
        if workers == 1:
            return dict(fn(key) for key in sorted(keys))
        out: dict[tuple[str, int], object] = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(fn, key) for key in sorted(keys)]
            for fut in concurrent.futures.as_completed(futures):
                key, value = fut.result()
                out[key] = value
        return out

    linked_cache = parallel_fetch(
        {(repo, pr_number) for _, repo, pr_number in parsed_rows}, fetch_linked
    )
    body_keys: set[tuple[str, int]] = set()
    for repo, pr_number in linked_cache:
        issue_numbers = cast(list, linked_cache.get((repo, pr_number)) or [])
        if not isinstance(issue_numbers, list):
            continue
        for issue_number in issue_numbers:
            try:
                body_keys.add((repo, int(issue_number)))
            except (TypeError, ValueError):
                continue
    body_cache = parallel_fetch(body_keys, fetch_body)

    for row, repo, pr_number in parsed_rows:
        issue_numbers = cast(list, linked_cache.get((repo, pr_number)) or [])
        if not issue_numbers:
            skipped.append(
                {
                    "run_id": row.run_id,
                    "target": row.target,
                    "repo": repo,
                    "pr_number": pr_number,
                    "reason": "no closingIssuesReferences",
                }
            )
            continue
        for issue_number in issue_numbers:
            try:
                body_key = (repo, int(issue_number))
            except (TypeError, ValueError):
                skipped.append(
                    {
                        "run_id": row.run_id,
                        "target": row.target,
                        "repo": repo,
                        "pr_number": pr_number,
                        "issue_number": issue_number,
                        "reason": "invalid linked issue number",
                    }
                )
                continue
            coerced = _coerce_issue_payload(body_cache.get(body_key))
            if coerced is None:
                skipped.append(
                    {
                        "run_id": row.run_id,
                        "target": row.target,
                        "repo": repo,
                        "pr_number": pr_number,
                        "issue_number": int(issue_number),
                        "reason": "could not fetch issue body",
                    }
                )
                continue
            body, title, url = coerced
            records.append(
                IssueRecord(
                    run_id=row.run_id,
                    target=row.target or f"{repo}#{pr_number}",
                    repo=repo,
                    pr_number=pr_number,
                    issue_number=int(issue_number),
                    durability=row.durability,
                    body=body,
                    title=title,
                    url=url,
                )
            )
    return records, skipped


def _heading_present(body: str, words: str) -> bool:
    return re.search(rf"(?im)^\s*#{{0,6}}\s*(?:{words})\s*:?\s*$", body) is not None


def _file_or_module_hints(body: str) -> set[str]:
    hints = {m.group(0).strip("`.,);:") for m in PATH_RE.finditer(body)}
    hints.update(m.group(0).strip("`.,);:") for m in ROOT_FILE_RE.finditer(body))
    hints.update(m.group(1) for m in MODULE_RE.finditer(body))
    return {h for h in hints if h and not h.startswith("http")}


def _body_length_bucket(word_count: int) -> str:
    if word_count < 80:
        return "short"
    if word_count <= 250:
        return "medium"
    return "long"


def extract_issue_features(body: str) -> dict:
    text = body or ""
    low = text.lower()
    words = re.findall(r"\b\w+\b", text)
    file_hints = _file_or_module_hints(text)
    ambiguity_count = len(AMBIGUITY_RE.findall(text))
    linked_pr_count = len({m.group(0).lower() for m in LINKED_PR_RE.finditer(text)})
    bucket = _body_length_bucket(len(words))

    has_repro_steps = bool(
        re.search(r"\b(?:steps?\s+to\s+reproduce|repro(?:duction)?\s+steps?|reproduce)\b", low)
        or _heading_present(text, r"repro(?:duction)?|steps? to reproduce")
    )
    has_acceptance_criteria = bool(
        re.search(r"\b(?:acceptance criteria|definition of done|done when|success criteria)\b", low)
        or re.search(
            r"(?im)^\s*[-*]\s+\[[ xX]\]\s+.*\b(?:pass|verify|ensure|complete|done)\b", text
        )
    )
    has_test_instructions = bool(
        TEST_COMMAND_RE.search(text)
        or TEST_PATH_RE.search(text)
        or _heading_present(text, r"tests?|test instructions|verification|validation")
    )
    has_non_goals = bool(
        re.search(
            r"\b(?:non-?goals?|out of scope|not in scope|do not include|does not include)\b", low
        )
        or _heading_present(text, r"non-?goals?|out of scope")
    )

    raw = {
        "has_repro_steps": has_repro_steps,
        "has_acceptance_criteria": has_acceptance_criteria,
        "has_test_instructions": has_test_instructions,
        "file_or_module_hint_count": len(file_hints),
        "body_length_bucket": bucket,
        "word_count": len(words),
        "ambiguity_markers": ambiguity_count,
        "has_non_goals": has_non_goals,
        "linked_pr_count": linked_pr_count,
    }
    raw["scored_features"] = {
        "has_repro_steps": has_repro_steps,
        "has_acceptance_criteria": has_acceptance_criteria,
        "has_test_instructions": has_test_instructions,
        "has_4_file_or_module_hints": len(file_hints) >= 4,
        "has_ambiguity_markers": ambiguity_count > 0,
        "has_non_goals": has_non_goals,
        "has_linked_prs": linked_pr_count > 0,
        "body_length_short": bucket == "short",
        "body_length_medium": bucket == "medium",
        "body_length_long": bucket == "long",
    }
    return raw


def _cell(rows: list[tuple[bool, bool]]) -> dict:
    n = len(rows)
    durable = sum(1 for present, is_durable in rows if is_durable)
    return {
        "n": n,
        "durable": durable,
        "durable_rate": (durable / n) if n else None,
    }


def analyze_issue_records(
    records: list[IssueRecord], *, min_cell_n: int = DEFAULT_MIN_CELL_N
) -> dict:
    issue_rows = []
    all_feature_names = list(FEATURE_LABELS.keys())
    for record in records:
        raw = extract_issue_features(record.body)
        scored = raw["scored_features"]
        is_durable = record.durability == DURABLE
        issue_rows.append(
            {
                "run_id": record.run_id,
                "target": record.target,
                "repo": record.repo,
                "pr_number": record.pr_number,
                "issue_number": record.issue_number,
                "title": record.title,
                "url": record.url,
                "durability": record.durability,
                "durable": is_durable,
                "raw_features": {k: v for k, v in raw.items() if k != "scored_features"},
                "scored_features": scored,
            }
        )

    overall_n = len(issue_rows)
    overall_durable = sum(1 for row in issue_rows if row["durable"])
    baseline = (overall_durable / overall_n) if overall_n else 0.0
    feature_stats = []
    weights: dict[str, float] = {}
    for feature in all_feature_names:
        present_rows = [
            (True, row["durable"]) for row in issue_rows if row["scored_features"].get(feature)
        ]
        absent_rows = [
            (False, row["durable"]) for row in issue_rows if not row["scored_features"].get(feature)
        ]
        with_cell = _cell(present_rows)
        without_cell = _cell(absent_rows)
        with_rate = with_cell["durable_rate"]
        without_rate = without_cell["durable_rate"]
        lift = None if with_rate is None or without_rate is None else with_rate - without_rate
        powered = with_cell["n"] >= min_cell_n and without_cell["n"] >= min_cell_n
        power_flag = (
            None if powered else f"underpowered - one or more cells n<{min_cell_n}; not a finding"
        )
        weight = float(lift) if powered and lift is not None else 0.0
        weights[feature] = weight
        feature_stats.append(
            {
                "feature": feature,
                "label": FEATURE_LABELS[feature],
                "with": with_cell,
                "without": without_cell,
                "lift": lift,
                "powered": powered,
                "power_flag": power_flag,
                "weight": weight,
            }
        )

    for row in issue_rows:
        score_lift = sum(
            weights[name] for name, present in row["scored_features"].items() if present
        )
        row["score_lift"] = score_lift
        row["score"] = max(0.0, min(1.0, baseline + score_lift))

    feature_stats.sort(
        key=lambda item: (
            not item["powered"],
            -abs(item["lift"] or 0.0),
            item["feature"],
        )
    )
    underpowered = [item for item in feature_stats if not item["powered"]]
    issue_rows.sort(
        key=lambda row: (row["score"], row["target"], row["issue_number"]), reverse=True
    )
    return {
        "baseline": {
            "n": overall_n,
            "durable": overall_durable,
            "durable_rate": baseline if overall_n else None,
        },
        "min_cell_n": min_cell_n,
        "features": feature_stats,
        "feature_weights": weights,
        "underpowered": underpowered,
        "issues": issue_rows,
    }


def build_report(
    lookback_days: int = 90,
    *,
    min_cell_n: int = DEFAULT_MIN_CELL_N,
    db_path: Path | None = None,
    linked_issue_fn=fetch_linked_issue_numbers,
    body_fetch_fn=fetch_issue_body,
    max_workers: int = 8,
    now: int | None = None,
) -> dict:
    rows = select_completed_keepalive_issue_runs(lookback_days, db_path=db_path, now=now)
    records, skipped = resolve_issue_records(
        rows,
        linked_issue_fn=linked_issue_fn,
        body_fetch_fn=body_fetch_fn,
        max_workers=max_workers,
    )
    analysis = analyze_issue_records(records, min_cell_n=min_cell_n)
    analysis.update(
        {
            "lookback_days": lookback_days,
            "runs_selected": len(rows),
            "issues_analyzed": len(records),
            "skipped": skipped,
        }
    )
    return analysis


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:5.1f}%"


def _num(value: float | None) -> str:
    return "n/a" if value is None else f"{value:+.3f}"


def format_human(report: dict) -> str:
    baseline = report["baseline"]
    lines = [
        "Issue quality -> durability associations",
        f"lookback_days={report.get('lookback_days')} runs_selected={report.get('runs_selected')} "
        f"issues_analyzed={report.get('issues_analyzed')} min_cell_n={report.get('min_cell_n')}",
        f"baseline durable rate: {_pct(baseline['durable_rate'])} "
        f"({baseline['durable']}/{baseline['n']})",
        "",
        "Feature                         with n  with rate  without n  without rate  lift    power",
        "------------------------------  ------  ---------  ---------  ------------  ------  -----",
    ]
    for item in report["features"]:
        power = "OK" if item["powered"] else "UNDERPOWERED"
        lines.append(
            f"{item['label'][:30]:30}  "
            f"{item['with']['n']:6d}  {_pct(item['with']['durable_rate']):>9}  "
            f"{item['without']['n']:9d}  {_pct(item['without']['durable_rate']):>12}  "
            f"{_num(item['lift']):>6}  {power}"
        )
    lines.append("")
    if report["underpowered"]:
        lines.append("Underpowered - not findings:")
        for item in report["underpowered"]:
            lines.append(
                f"- {item['label']}: with n={item['with']['n']}, without n={item['without']['n']} "
                f"({item['power_flag']})"
            )
    else:
        lines.append("Underpowered - not findings: none")
    lines.append("")
    lines.append("Top issue scores:")
    for row in report["issues"][:10]:
        lines.append(
            f"- {row['score']:.3f} ({row['durability']}) {row['target']} issue #{row['issue_number']}"
        )
    if report.get("skipped"):
        lines.append("")
        lines.append(f"Skipped linked issue/body lookups: {len(report['skipped'])}")
    return "\n".join(lines)


def _selftest() -> None:
    old_db = feedback.DB_PATH
    tmp = tempfile.mkdtemp(prefix="issue-quality-selftest-")
    feedback.DB_PATH = Path(tmp) / "t.db"
    now = int(time.time())
    bodies: dict[int, str] = {}

    rich = """## Scope
Fix the export crash in `orchestrator.issue_writer` and `feedback.py`.

## Repro steps
1. Run `python3 issue_quality.py --json`
2. Observe the crash.

## Acceptance criteria
- [ ] Handles empty linked issues.
- [ ] Preserves read-only feedback DB behavior.

## Test instructions
Run `python3 -m pytest tests/test_issue_quality.py`.

Files: issue_quality.py feedback.py keepalive_outcomes.py tests/test_issue_quality.py.
"""
    weak = """Investigate the thing? It maybe breaks somewhere.
Probably update the code. TBD.
"""
    rare = rich + "\n## Non-goals\nDo not change routing behavior.\n"

    try:
        for i in range(24):
            pr = i + 1
            issue = 1000 + pr
            durable = i < 12
            body = rare if i < 2 else rich if durable else weak
            bodies[issue] = body
            run_id = f"keepalive:o/r#{pr}:codex"
            feedback.record_run(
                run_id,
                f"o/r#{pr}",
                "implement",
                "codex",
                mode="remote",
                pr_number=pr,
                source="keepalive",
                assignment="assigned",
                work_type="issue",
                ts=now - 10,
            )
            feedback.record_outcome(
                run_id,
                adjudicated_verdict="PASS" if durable else "FAIL",
                merged=durable,
                durability="durable" if durable else "reverted",
            )
        feedback.record_run(
            "keepalive:o/r#999:codex",
            "o/r#999",
            "implement",
            "codex",
            mode="remote",
            pr_number=999,
            source="keepalive",
            assignment="assigned",
            work_type="issue",
            ts=now - 10,
        )
        feedback.record_outcome("keepalive:o/r#999:codex", durability="pending")

        body_fetch_calls: list[tuple[str, int]] = []

        def linked_issue_fn(repo: str, pr_number: int) -> list[int]:
            assert repo == "o/r"
            return [1000 + pr_number]

        def body_fetch_fn(repo: str, issue_number: int) -> dict:
            body_fetch_calls.append((repo, issue_number))
            return {"body": bodies[issue_number], "title": f"issue {issue_number}"}

        report = build_report(
            lookback_days=90,
            min_cell_n=5,
            linked_issue_fn=linked_issue_fn,
            body_fetch_fn=body_fetch_fn,
            max_workers=4,
            now=now,
        )
        assert report["runs_selected"] == 24, report["runs_selected"]
        assert report["issues_analyzed"] == 24, report["issues_analyzed"]
        assert len(body_fetch_calls) == 24, len(body_fetch_calls)
        weights = report["feature_weights"]
        assert weights["has_acceptance_criteria"] > 0, weights
        assert weights["has_test_instructions"] > 0, weights
        assert weights["has_ambiguity_markers"] < 0, weights
        non_goals = next(item for item in report["features"] if item["feature"] == "has_non_goals")
        assert not non_goals["powered"] and non_goals["weight"] == 0.0, non_goals
        rich_issue = next(row for row in report["issues"] if row["issue_number"] == 1003)
        weak_issue = next(row for row in report["issues"] if row["issue_number"] == 1014)
        assert rich_issue["score"] > weak_issue["score"], (rich_issue, weak_issue)
        human = format_human(report)
        assert "Feature" in human and "Top issue scores" in human and "Underpowered" in human, human
        print("issue_quality.py selftest: OK")
    finally:
        feedback.DB_PATH = old_db
        shutil.rmtree(tmp, ignore_errors=True)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Score keepalive issue quality against durability outcomes."
    )
    parser.add_argument("--lookback-days", type=int, default=90)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--selftest", action="store_true")
    parser.add_argument("--min-cell-n", type=int, default=DEFAULT_MIN_CELL_N)
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args(argv)

    if args.selftest:
        _selftest()
        return 0

    report = build_report(
        lookback_days=args.lookback_days,
        min_cell_n=args.min_cell_n,
        max_workers=args.workers,
    )
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(format_human(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
