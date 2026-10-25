#!/usr/bin/env python3
"""adjudicator_live.py — the adjudicator role on NEW verifier disputes, live, in shadow (M2 live).

WHY. The retrospective (`adjudicator_retro.run`) replays old disputes from stored metadata, and by
the role's contract those decisions are metadata-only: `graded` cannot move on them. The owner kept
that strictness on 2026-10-16. What was missing was the live case: a dispute adjudicated when it is
persisted, from a case certified against the merged commit, graded when durability lands. Measured
on the value-chain monitor of 2026-10-16: 340 persisted disputes in 90 days, 78 invocations, all of
them retrospective replays, 0 live (stranske/Orchestrator#643).

WHAT. Each run reads the persisted disputes the retrospective reads (`adjudicator_retro.disputes`),
takes the newest ones this report has not seen, and for each: builds the same packet the
retrospective builds, provisions a disposable exact-head checkout of the merged commit, CERTIFIES
the case with `adjudicator_retro.qualify_collected_case` (the native cohort's own certifier) from a
deterministic inventory — every changed path, every explicit acceptance criterion mapped to the
whole diff — and asks the role once with `source="live"`. The advice is persisted beside the
dispute in `capability-program/adjudicator-live.json`; the retrospective's reconciler grades it
when `outcomes.durability` lands. A case that cannot be certified is recorded `uncertified` with
its reason and never adjudicated from metadata.

SHADOW ONLY. Nothing here writes an outcome, a label, a merge input or a close. Kill switch:
`ORCH_ADJUDICATOR_LIVE_SHADOW=0`. Per-run cap: `ORCH_ADJUDICATOR_LIVE_LIMIT` (default 3), reusing
the retrospective's 600 s dispatch budget. Dedup (CLAUDE.md §0): `roles.run_adjudicator_agent` had
one caller, the retrospective; the collector, the packet builder, the reconciler and the weekly
line are reused, not rebuilt; only the live caller and its report are new.
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable

import adjudicator_retro as retro
import feedback
import roles

LIVE_SWITCH = "ORCH_ADJUDICATOR_LIVE_SHADOW"
LIVE_LIMIT = "ORCH_ADJUDICATOR_LIVE_LIMIT"
DEFAULT_LIMIT = 3
OFF_WORDS = frozenset({"0", "false", "no", "off"})
CHECK_RUN_PAGE_SIZE = 100
CHECK_RUN_LOG_TAIL_BYTES = 4_096
EXECUTION_EVIDENCE_BYTES = 65_536
FAILED_CHECK_CONCLUSIONS = frozenset(
    {"action_required", "cancelled", "failure", "startup_failure", "stale", "timed_out"}
)


def report_path() -> Path:
    return Path(os.environ.get("ORCH_STATE_DIR", Path.home() / ".codex/orchestrator")) / (
        "capability-program/adjudicator-live.json"
    )


def enabled(env=None) -> bool:
    env = os.environ if env is None else env
    return str(env.get(LIVE_SWITCH, "1")).strip().lower() not in OFF_WORDS


def configured_limit(env=None) -> int:
    env = os.environ if env is None else env
    try:
        return max(0, int(env.get(LIVE_LIMIT, DEFAULT_LIMIT)))
    except (TypeError, ValueError):
        return DEFAULT_LIMIT


def pr_snapshot(target: str) -> dict:
    """The PR's number, body and head, read once; the body is the criterion provenance."""
    repo, _, number = target.partition("#")
    data = retro._gh_json(
        ["pr", "view", number, "--repo", repo, "--json", "number,body,headRefOid"]
    )
    return {key: data[key] for key in ("number", "body", "headRefOid")}


def _changed_paths(repository: Path, sha: str) -> list[str]:
    parent = retro._local_git(repository, "rev-parse", sha + "^1").decode().strip()
    raw = (
        retro._local_git(
            repository, "diff-tree", "--no-renames", "--name-status", "-r", "-z", parent, sha
        )
        .decode("utf-8")
        .split("\0")
    )
    entries = list(zip(raw[:-1:2], raw[1:-1:2]))
    if any(status not in {"A", "M"} for status, _path in entries):
        raise ValueError("live certification supports add/modify blobs only")
    return [path for _status, path in entries]


def _named_test_commands(criteria: list[str]) -> list[str]:
    """Return explicit test invocations or pytest node IDs, excluding ordinary code spans."""
    commands: list[str] = []
    for criterion in criteria:
        literals = re.findall(r"(?<!`)`([^`\n]+)`(?!`)", criterion)
        for block in re.findall(r"```[^\n]*\n(.*?)```", criterion, re.DOTALL):
            literals.extend(block.splitlines())
        for command in literals:
            command = command.strip()
            try:
                tokens = shlex.split(command)
            except ValueError:
                continue
            if tokens[:2] in (["uv", "run"], ["poetry", "run"], ["pipenv", "run"]):
                tokens = tokens[2:]
            executable = Path(tokens[0]).name if tokens else ""
            invocation = executable == "pytest" or (
                re.fullmatch(r"python(?:\d+(?:\.\d+)*)?", executable)
                and tokens[1:3] in (["-m", "pytest"], ["-m", "unittest"])
            )
            if (invocation or re.fullmatch(r"\S+\.py::\S+", command)) and command not in commands:
                commands.append(command)
    return commands


def _tail(value: object, limit: int) -> str:
    """Keep a valid UTF-8 log suffix within the byte budget, including its marker."""
    if limit <= 0:
        return ""
    text = str(value or "")
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    marker = "[... truncated ...]\n"
    # Prefer useful log bytes when the remaining budget cannot hold a marker and a suffix.
    if limit <= len(marker):
        marker = ""
    suffix = encoded[-(limit - len(marker)) :].decode("utf-8", errors="ignore")
    return marker + suffix


def _actions_log_endpoint(repo: str, details_url: object) -> str | None:
    if not isinstance(details_url, str):
        return None
    match = re.fullmatch(
        rf"https://github\.com/{re.escape(repo)}/actions/runs/\d+/job/(\d+)/?", details_url
    )
    if match is None:
        return None
    return f"https://api.github.com/repos/{repo}/actions/jobs/{match[1]}/logs"


def _github_log_tail(endpoint: str) -> str:
    result = subprocess.run(
        ["gh", "api", endpoint, "--method", "GET"],
        capture_output=True,
        check=True,
        timeout=120,
    )
    return _tail(result.stdout.decode("utf-8", errors="replace"), CHECK_RUN_LOG_TAIL_BYTES)


def _check_run_evidence(
    target: str,
    sha: str,
    reader: Callable[[list[str]], dict],
    log_reader: Callable[[str], str],
) -> list[dict]:
    """Read every exact-SHA check run and only bounded failure output from GitHub."""
    repo, separator, _number = target.partition("#")
    if not separator or repo.count("/") != 1:
        raise ValueError("live execution evidence requires a PR target")
    runs: list[dict] = []
    total: int | None = None
    page = 1
    log_bytes = 0
    while total is None or len(runs) < total:
        endpoint = (
            f"repos/{repo}/commits/{sha}/check-runs?filter=all"
            f"&per_page={CHECK_RUN_PAGE_SIZE}&page={page}"
        )
        response = reader(["api", endpoint])
        page_runs = response.get("check_runs") if isinstance(response, dict) else None
        if not isinstance(page_runs, list):
            raise ValueError("GitHub check-run page is malformed")
        if total is None:
            raw_total = response.get("total_count")
            if type(raw_total) is not int or raw_total < 0:
                raise ValueError("GitHub check-run total is unavailable")
            total = raw_total
        for check in page_runs:
            if not isinstance(check, dict):
                raise ValueError("GitHub check-run entry is malformed")
            if check.get("head_sha") != sha:
                raise ValueError("GitHub check-run revision differs from evaluated commit")
            name = check.get("name")
            conclusion = check.get("conclusion")
            if (
                not isinstance(name, str)
                or not name.strip()
                or not (isinstance(conclusion, str) or conclusion is None)
            ):
                raise ValueError("GitHub check-run lacks name or conclusion")
            api_url = check.get("url")
            if not isinstance(api_url, str) or not api_url.strip():
                check_id = check.get("id")
                if type(check_id) is not int:
                    raise ValueError("GitHub check-run lacks API provenance")
                api_url = f"https://api.github.com/repos/{repo}/check-runs/{check_id}"
            item: dict[str, Any] = {
                "name": name,
                "conclusion": conclusion,
                "sha": sha,
                "api_url": api_url,
            }
            if isinstance(conclusion, str) and conclusion.lower() in FAILED_CHECK_CONCLUSIONS:
                remaining = max(0, EXECUTION_EVIDENCE_BYTES - log_bytes)
                if not remaining:
                    item["log_unavailable"] = "execution evidence budget exhausted"
                    runs.append(item)
                    continue
                try:
                    detail = reader(["api", api_url])
                except (OSError, RuntimeError, subprocess.SubprocessError):
                    item["log_unavailable"] = "check-run detail unavailable"
                    runs.append(item)
                    continue
                if not isinstance(detail, dict) or detail.get("head_sha") != sha:
                    raise ValueError(
                        "GitHub check-run detail revision differs from evaluated commit"
                    )
                details_url = detail.get("details_url")
                log_endpoint = _actions_log_endpoint(repo, details_url)
                if log_endpoint is None:
                    item["log_unavailable"] = "Actions job URL unavailable"
                else:
                    item["log_provenance"] = {"api_url": log_endpoint, "sha": sha}
                    if remaining:
                        try:
                            log_tail = _tail(
                                log_reader(log_endpoint), min(remaining, CHECK_RUN_LOG_TAIL_BYTES)
                            )
                        except (OSError, RuntimeError, subprocess.SubprocessError):
                            item["log_unavailable"] = "Actions job log unavailable"
                        else:
                            item["log_tail"] = log_tail
                            log_bytes += len(log_tail.encode("utf-8"))
                    else:
                        item["log_unavailable"] = "execution evidence budget exhausted"
            runs.append(item)
            if total is not None and len(runs) >= total:
                break
        if len(runs) >= total:
            break
        if not page_runs:
            raise ValueError("GitHub check-run pagination ended before total_count")
        page += 1
    return runs


def _runtime_events_for_sha(target: str, sha: str) -> list[dict]:
    """Read matching completion events without the reporting API's global 1,000-row window."""
    events = []
    with feedback._conn() as conn:
        rows = conn.execute(
            "SELECT event_id,payload_json FROM completion_events "
            "WHERE producer='runtime_ac_gate' ORDER BY updated_ts DESC,event_id DESC"
        )
        for event_id, raw in rows:
            try:
                payload = json.loads(raw)
            except (TypeError, ValueError):
                continue
            gate = payload.get("runtime_ac_gate") if isinstance(payload, dict) else None
            if (
                isinstance(gate, dict)
                and gate.get("target") == target
                and gate.get("observed_head_sha") == sha
            ):
                events.append({**gate, "event_id": event_id, "producer": "runtime_ac_gate"})
    return events


def _command_result(command: str, events: list[dict], target: str, sha: str) -> dict | None:
    """Find the newest exact-head runtime-AC result for a documented command."""
    for event in events:
        if event.get("target") != target or event.get("observed_head_sha") != sha:
            continue
        for audit in event.get("audit_evidence") or []:
            observed = audit.get("command") if isinstance(audit, dict) else None
            if not isinstance(observed, str):
                continue
            if not _same_named_test_command(command, observed):
                continue
            return {
                "command": command,
                "status": audit.get("status"),
                "returncode": audit.get("returncode"),
                "event_id": event.get("event_id"),
                "producer": "runtime_ac_gate",
                "target": target,
                "sha": sha,
            }
    return None


def _same_named_test_command(command: str, observed: str) -> bool:
    """Match a documented command exactly, or a bare node as pytest's sole test target."""
    if observed.strip() == command:
        return True
    if any(char.isspace() for char in command):
        return False
    try:
        tokens = shlex.split(observed)
    except ValueError:
        return False
    try:
        pytest_at = tokens.index("pytest")
    except ValueError:
        return False
    return tokens[:pytest_at] in (
        [],
        ["python"],
        ["python3"],
        ["python", "-m"],
        ["python3", "-m"],
    ) and tokens[pytest_at + 1 :] == [command]


def _execution_evidence(
    row: dict,
    sha: str,
    criteria: list[str],
    *,
    body: str | None = None,
    github_reader: Callable[[list[str]], dict] | None = None,
    runtime_events_reader: Callable[[], list[dict]] | None = None,
    log_reader: Callable[[str], str] | None = None,
) -> dict:
    """Attach live execution facts while preserving explicit UNKNOWNs for absent named tests."""
    github_reader = retro._gh_json if github_reader is None else github_reader
    log_reader = _github_log_tail if log_reader is None else log_reader
    target = str(row.get("target") or "")
    commands = _named_test_commands([body] if body is not None else criteria)
    events = []
    if commands:
        events = (
            _runtime_events_for_sha(target, sha)
            if runtime_events_reader is None
            else runtime_events_reader()
        )
    if not isinstance(events, list):
        raise ValueError("runtime-AC event reader returned malformed evidence")
    command_results = []
    unknowns = []
    for command in commands:
        result = _command_result(command, events, target, sha)
        if result is None:
            unknowns.append(command)
        else:
            command_results.append(result)
    return {
        "evaluated_sha": sha,
        "runtime_ac_provenance": {
            "producer": "runtime_ac_gate",
            "target": target,
            "sha": sha,
            "source": "completion_events",
        },
        "check_runs": _check_run_evidence(target, sha, github_reader, log_reader),
        "runtime_ac_command_results": command_results,
        "unknowns": unknowns,
    }


def certify(
    row: dict,
    repository: Path,
    snapshot: dict,
    *,
    github_reader: Callable[[list[str]], dict] | None = None,
    runtime_events_reader: Callable[[], list[dict]] | None = None,
    log_reader: Callable[[str], str] | None = None,
) -> roles.CollectedAdjudicationCase:
    """Certify a live case deterministically: every changed path, every criterion to the diff.

    No judgement is made about which file proves which criterion — the role sees the whole diff
    for every criterion, and the mapping says so. Everything else is the native cohort's own
    certifier, `adjudicator_retro.qualify_collected_case`, which raises on any inventory,
    provenance or snapshot mismatch; that ValueError is the `uncertified` reason.
    """
    sha = retro._case_evaluated_sha(row)
    if sha is None:
        raise ValueError("dispute carries no exact evaluated commit")
    body = snapshot.get("body")
    if not isinstance(body, str) or not body.strip():
        raise ValueError("PR body unavailable for criterion provenance")
    paths = _changed_paths(repository, sha)
    if not paths:
        raise ValueError("merged commit changes no add/modify blobs")
    criteria = retro._criterion_texts(body)
    packet = row.get("packet")
    ground_truth = packet.get("ground_truth_evidence") if isinstance(packet, dict) else None
    if not isinstance(ground_truth, dict):
        raise ValueError("live case packet lacks ground-truth evidence")
    ground_truth["live_execution_evidence"] = _execution_evidence(
        row,
        sha,
        criteria,
        body=body,
        github_reader=github_reader,
        runtime_events_reader=runtime_events_reader,
        log_reader=log_reader,
    )
    row["collection_requirements"] = {
        "source_paths": paths,
        "acceptance": [
            {"criterion": text, "location": f"git-path:{path}"}
            for text in criteria
            for path in paths
        ],
    }
    row["inventory_provenance"] = {
        "source_criterion_tasks": body,
        "pr_body_sha256": hashlib.sha256(body.encode()).hexdigest(),
    }
    parent = retro._local_git(repository, "rev-parse", sha + "^1").decode().strip()
    qualification = {
        "comparison_base": parent,
        "snapshot": {key: snapshot[key] for key in ("number", "headRefOid", "body")},
        "criterion_mapping": [
            {
                "criterion": text,
                "evidence_locations": [f"git-path:{path}" for path in paths],
                "unknowns": [],
            }
            for text in criteria
        ],
    }
    return retro.qualify_collected_case(row, repository, qualification)


def _load_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    report = json.loads(path.read_text())
    rows = report.get("rows") if isinstance(report, dict) else None
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


MAX_ATTEMPTS = (
    3  # a case whose provisioning or role run failed is retried on later runs, this many in all
)


def _final(row: dict) -> bool:
    """A row later runs must not retry: advised, refused at certification, or out of attempts."""
    if "proposal" in row or (row.get("certification") or {}).get("status") == "uncertified":
        return True
    return int(row.get("attempts") or 1) >= MAX_ATTEMPTS


def run_live(
    *,
    limit: int | None = None,
    dispatch: bool = True,
    db: Path | None = None,
    path: Path | None = None,
    now: int | None = None,
    env=None,
    evidence_reader: Callable[[dict], dict] | None = None,
    runner: Callable[..., dict] | None = None,
    provider: Callable[[str, str], Any] | None = None,
    snapshot_reader: Callable[[str], dict] | None = None,
    github_reader: Callable[[list[str]], dict] | None = None,
    timeout: int = retro.DISPATCH_TIMEOUT_S,
) -> dict:
    """One bounded live pass; idempotent per dispute (case id = run + both verdicts)."""
    env = os.environ if env is None else env
    now = int(time.time()) if now is None else now
    path = path or report_path()
    if not enabled(env):
        return {"disabled": True, "attempted": 0, "advised": 0, "uncertified": 0}
    limit = configured_limit(env) if limit is None else max(0, limit)
    reader = evidence_reader or retro.fetch_evidence
    runner = runner or roles.run_adjudicator_agent
    provider = provider or _exact_head_checkout
    snapshot_reader = snapshot_reader or pr_snapshot
    rows = _load_rows(path)
    saved = {row.get("case_id"): row for row in rows if _final(row)}
    retrying = {row.get("case_id"): row for row in rows if not _final(row)}
    population = retro.disputes(db=db, now=now)
    attempted = advised = uncertified = 0
    for dispute in population:
        if attempted >= limit:
            break
        case_id = retro._case_id(dispute)
        if case_id in saved:
            continue
        attempted += 1
        earlier = retrying.pop(case_id, None)
        if earlier is not None:
            rows.remove(earlier)
        entry: dict[str, Any] = {
            **retro._case_identity(dispute),
            "case_id": case_id,
            "target": dispute["target"],
            "source": "live",
            "attempted_at": now,
            "first_attempted_at": (
                earlier.get("first_attempted_at", earlier.get("attempted_at")) if earlier else now
            ),
            "attempts": int(earlier.get("attempts") or 1) + 1 if earlier else 1,
            "later_truth": retro.later_truth(dispute),
            "merge_rule_verdict": (
                ("PASS" if dispute["merged"] else "FAIL") if dispute["merged"] is not None else None
            ),
            "cost_usd": None,
            "dispatch_timeout_s": timeout,
        }
        try:
            entry["packet"] = retro.build_packet(dispute, reader(dispute))
            sha = retro._case_evaluated_sha(entry)
            if sha is None:
                raise ValueError("dispute carries no exact evaluated commit")
            with provider(dispute["target"], sha) as repository:
                repository = Path(repository)
                case = certify(
                    entry,
                    repository,
                    snapshot_reader(dispute["target"]),
                    github_reader=github_reader,
                )
                entry["certification"] = {
                    "status": "certified",
                    "evaluated_sha": sha,
                    "paths": entry["collection_requirements"]["source_paths"],
                }
                # The persisted packet is the CERTIFIED one (metadata_only False, collected
                # evidence, qualification), so the retrospective's evidence floor, which
                # demotes metadata-only rows to needs_more_evidence, does not apply to a live
                # row; the raw merged-PR packet is kept beside it for provenance.
                entry["raw_packet"] = entry["packet"]
                entry["packet"] = roles._collected_snapshot(case)
                if dispatch:
                    result = runner(
                        case=case,
                        dispatch=True,
                        source="live",
                        cwd=str(repository),
                        timeout=timeout,
                    )
                    entry.update(
                        {
                            key: result.get(key)
                            for key in (
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
                            "decision", entry["decision"]
                        )
                        entry["shadow_verdict"] = retro._shadow_verdict_for_row(entry)
                        advised += 1
                    try:
                        entry["cost_usd"] = retro.measured_cost(result.get("backend_run_id"), db)
                    except (OSError, ValueError, KeyError, TypeError):
                        entry["cost_usd"] = None
        except (ValueError, KeyError, TypeError) as exc:
            if "certification" in entry:
                entry["error"] = str(exc)[:300]  # the role run failed after certification
            else:
                entry["certification"] = {"status": "uncertified", "reason": str(exc)[:300]}
                uncertified += 1
        except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
            # Provisioning, an evidence read or the role run failed, not the case: the row is
            # kept with its error and retried by later runs until MAX_ATTEMPTS, then it is final.
            entry["error"] = str(exc)[:300]
        rows.append(entry)
        if _final(entry):
            saved[case_id] = entry
    # Grade every live row against later truth and write the live report. NOT the retrospective's
    # persistence: `adjudicator_retro._row_metadata_only` treats any serialized qualification as
    # metadata-only on purpose (a JSON flag cannot prove collector issuance for the retro's own
    # report), so its writer would demote every live decision to needs_more_evidence. A live row
    # was decided on a case certified IN THIS PROCESS at dispatch time; this module records that
    # fact itself (`certification`), grades the advice itself, and labels the number `live`.
    _refresh_live_truth(rows, db)
    summary = live_summary(rows)
    _write_report(
        path,
        {
            "schema": "adjudicator-live/v1",
            "generated_at": now,
            "population": len(population),
            "summary": summary,
            "rows": rows,
        },
    )
    return {
        "disabled": False,
        "attempted": attempted,
        "advised": advised,
        "uncertified": uncertified,
        "population": len(population),
        "summary": summary,
        "rows": rows,
    }


def _refresh_live_truth(rows: list[dict], db: Path | None) -> None:
    """Later truth for every live row from the Brain's judged durability, read-only."""
    for row in rows:
        run_id = row.get("run_id")
        if not isinstance(run_id, str):
            continue
        try:
            row["later_truth"] = retro.native_later_truth(run_id, db)
        except (OSError, ValueError, TypeError):
            pass


UNCERTIFIED_CLASSES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("prompt_too_large", ("exceeds byte limit",)),
    ("no_criteria", ("no explicit acceptance criteria",)),
    ("criteria_shape", ("multiline acceptance criterion", "resume after prose")),
    (
        "collection_incomplete",
        ("incomplete collected case", "incomplete before-source", "supporting source"),
    ),
    ("diff_shape", ("add/modify", "deletion/type change", "malformed complete Git diff")),
)


def uncertified_class(reason: str) -> str:
    """Name the refusal so the weekly line says which step stops certification most."""
    for name, needles in UNCERTIFIED_CLASSES:
        if any(needle in reason for needle in needles):
            return name
    return "other"


def live_summary(rows: list[dict]) -> dict:
    """Counts for the weekly line. `graded` is a live decision with later truth; nothing else."""
    certified = [r for r in rows if (r.get("certification") or {}).get("status") == "certified"]
    decided = [r for r in certified if r.get("decision") in {"uphold_blocker", "reject_blocker"}]
    graded = [r for r in decided if r.get("later_truth") in {"PASS", "FAIL"}]
    refused = [r for r in rows if (r.get("certification") or {}).get("status") == "uncertified"]
    failed = [r for r in rows if "proposal" not in r and (r.get("error") or r.get("errors"))]
    agree = sum(retro._shadow_verdict_for_row(r) == r["later_truth"] for r in graded)
    rule = sum(r.get("merge_rule_verdict") == r["later_truth"] for r in graded)
    return {
        "cases": len(rows),
        "certified": len(certified),
        "uncertified": len(refused),
        "uncertified_reasons": dict(
            collections.Counter(
                uncertified_class(str(r["certification"].get("reason") or "")) for r in refused
            ).most_common()
        ),
        "advised": len(decided),
        "failed": len(failed),
        "retrying": sum(1 for r in failed if not _final(r)),
        "graded": len(graded),
        "agree": agree,
        "disagree": len(graded) - agree,
        "merge_rule_agree": rule,
        "cost_usd_measured": sum(1 for r in decided if isinstance(r.get("cost_usd"), (int, float))),
    }


def _write_report(path: Path, report: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = path.with_suffix(".tmp")
    staged.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    staged.replace(path)


@contextmanager
def _exact_head_checkout(target: str, sha: str):
    import provision

    with provision.temporary_exact_head_worktree(target, sha) as worktree:
        yield worktree


def weekly_line(path: Path | None = None) -> str:
    """`adjudicator live: disputes N, advised A, uncertified U (why), failed F, graded G (agree X, disagree Y)`.

    `failed F` appears only when a certified case's provisioning or role run failed (retried up
    to MAX_ATTEMPTS); `(why)` only when something was refused at certification.
    """
    try:
        report = json.loads((path or report_path()).read_text())
        rows = [row for row in report.get("rows", []) if isinstance(row, dict)]
        summary = live_summary(
            rows
        )  # the saved rows are the truth; a saved summary may predate the module
        reasons = summary.get("uncertified_reasons") or {}
        refused = (
            " (" + ", ".join(f"{name} {count}" for name, count in reasons.items()) + ")"
            if summary["uncertified"] and reasons
            else ""
        )
        failed = f", failed {summary['failed']}" if summary.get("failed") else ""
        return (
            f"adjudicator live: disputes {report.get('population', 'UNKNOWN')}, "
            f"advised {summary['advised']}, uncertified {summary['uncertified']}{refused}{failed}, "
            f"graded {summary['graded']} (agree {summary['agree']}, disagree {summary['disagree']})"
        )
    except (OSError, ValueError, KeyError, TypeError):
        return "adjudicator live: disputes UNKNOWN, advised UNKNOWN, uncertified UNKNOWN, graded UNKNOWN"


# --------------------------------------------------------------------------------------------
# selftest: offline, a toy repository, a private Brain, stubbed role and snapshot.
# --------------------------------------------------------------------------------------------

AC_BODY = (
    "Fix the thing.\n\n## Acceptance Criteria\n"
    "- Named test: `tests/test_proof.py::test_proof` passes.\n"
    "- Deliberate break: the test fails on the broken candidate.\n"
)


def _toy_repository(root: Path) -> str:
    repository = root / "repo"
    repository.mkdir()
    retro._local_git(repository, "init", "-q")
    retro._local_git(repository, "config", "user.email", "selftest@example.invalid")
    retro._local_git(repository, "config", "user.name", "Selftest")
    (repository / "proof.txt").write_text("original\n")
    retro._local_git(repository, "add", ".")
    retro._local_git(repository, "commit", "-q", "-m", "base")
    (repository / "proof.txt").write_text("delivered\n")
    retro._local_git(repository, "add", ".")
    retro._local_git(repository, "commit", "-q", "-m", "delivery")
    return retro._local_git(repository, "rev-parse", "HEAD").decode().strip()


def _evidence_for(sha: str) -> dict:
    return {
        "disputed_finding": {
            "ref": "verifier-comment",
            "body": "Missing acceptance test",
            "decision": {"evaluated_sha": sha},
        },
        "ground_truth_evidence": {
            "head_sha": sha,
            "merge_sha": sha,
            "diff_summary": [{"path": "proof.txt"}],
            "gate_runs": ["gate-run"],
        },
    }


def _selftest() -> None:
    import sqlite3
    import tempfile

    with tempfile.TemporaryDirectory(prefix="adjudicator-live-") as tmp:
        root = Path(tmp)
        sha = _toy_repository(root)
        saved_db = feedback.DB_PATH
        feedback.DB_PATH = root / "brain.db"
        try:
            now = int(time.time())
            with feedback._conn() as conn:
                conn.execute(
                    "INSERT INTO runs(run_id,ts,target) VALUES ('live-1',?,'owner/repo#1')", (now,)
                )
                conn.execute(
                    "INSERT INTO outcomes(run_id,verifier_verdict,adjudicated_verdict,merged) "
                    "VALUES ('live-1','NON_PASS','PASS',1)"
                )
            calls: list[dict] = []

            def runner(**kw):
                calls.append(kw)
                assert kw["source"] == "live" and kw["dispatch"] is True
                assert isinstance(kw["case"], roles.CollectedAdjudicationCase)
                execution = roles._collected_snapshot(kw["case"])["ground_truth_evidence"][
                    "live_execution_evidence"
                ]
                assert execution["check_runs"][0]["sha"] == sha
                assert execution["check_runs"][0]["name"] == "selftest-unit"
                assert execution["unknowns"] == ["tests/test_proof.py::test_proof"]
                return {
                    "backend_run_id": "backend-live",
                    "backend": "stub",
                    "proposal": {"decision": "uphold_blocker", "rationale": "x"},
                    "advisory_plan": {"decision": "uphold_blocker"},
                    "errors": [],
                }

            @contextmanager
            def provider(target, head):
                assert target == "owner/repo#1" and head == sha
                yield str(root / "repo")

            snapshot = {"number": 1, "headRefOid": sha, "body": AC_BODY}
            common: dict[str, Any] = dict(
                db=feedback.DB_PATH,
                path=root / "live.json",
                now=now + 1,
                env={},
                evidence_reader=lambda _row: _evidence_for(sha),
                runner=runner,
                provider=provider,
                github_reader=lambda _args: {
                    "total_count": 1,
                    "check_runs": [
                        {
                            "id": 1,
                            "head_sha": sha,
                            "name": "selftest-unit",
                            "conclusion": "success",
                        }
                    ],
                },
            )
            first = run_live(snapshot_reader=lambda _t: snapshot, **common)
            assert (first["attempted"], first["advised"], first["uncertified"]) == (1, 1, 0), first
            assert first["rows"][0]["certification"]["status"] == "certified"
            assert first["rows"][0]["decision"] == "uphold_blocker"
            second = run_live(snapshot_reader=lambda _t: snapshot, **common)
            assert second["attempted"] == 0 and len(calls) == 1, "a dispute is adjudicated once"
            with sqlite3.connect(feedback.DB_PATH) as conn:
                verdicts = conn.execute(
                    "SELECT verifier_verdict,adjudicated_verdict FROM outcomes"
                ).fetchall()
            assert verdicts == [("NON_PASS", "PASS")], "shadow never writes an outcome"
            # A body with no explicit acceptance criteria cannot be certified: recorded, no dispatch.
            with feedback._conn() as conn:
                conn.execute(
                    "INSERT INTO runs(run_id,ts,target) VALUES ('live-2',?,'owner/repo#1')",
                    (now - 1,),
                )
                conn.execute(
                    "INSERT INTO outcomes(run_id,verifier_verdict,adjudicated_verdict,merged) "
                    "VALUES ('live-2','PASS','NON_PASS',1)"
                )
            third = run_live(
                snapshot_reader=lambda _t: {**snapshot, "body": "No criteria here."}, **common
            )
            assert (third["attempted"], third["uncertified"]) == (1, 1) and len(calls) == 1, third
            assert (
                "adjudicator live: disputes 2, advised 1, uncertified 1 (no_criteria 1), graded 0"
                in weekly_line(root / "live.json")
            )
            # The kill switch writes nothing.
            off = run_live(
                snapshot_reader=lambda _t: snapshot, **{**common, "env": {LIVE_SWITCH: "0"}}
            )
            assert off["disabled"] and off["attempted"] == 0
        finally:
            feedback.DB_PATH = saved_db
    print(
        "adjudicator_live selftest OK (certified live dispute advised once; uncertified recorded; switch off writes nothing)"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help=f"per-run cap (default {LIVE_LIMIT} or {DEFAULT_LIMIT})",
    )
    parser.add_argument(
        "--no-dispatch", action="store_true", help="certify and record, never ask the role"
    )
    parser.add_argument("--line", action="store_true", help="print the weekly line and exit")
    parser.add_argument(
        "--enabled",
        action="store_true",
        help="exit 0 when the kill switch is on; the tick asks this so the switch is read once",
    )
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args(argv)
    if args.selftest:
        _selftest()
        return 0
    if args.line:
        print(weekly_line())
        return 0
    if args.enabled:
        return 0 if enabled() else 1
    result = run_live(limit=args.limit, dispatch=not args.no_dispatch)
    print(json.dumps({k: v for k, v in result.items() if k != "rows"}, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
